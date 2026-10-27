import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping, NamedTuple

from sqlalchemy import Select, literal_column, or_, select

from mediaforce.core.db import DBClient
from mediaforce.core.db import DBRow
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.encoding.encode_queue import DISPLAY_ENCODE_JOB_KINDS, encode_run_manifest_rel_paths, \
    unfinished_breakdown_groups, unfinished_breakdown_summary
from mediaforce.encoding.plain_failure import plain_failure_summary
from mediaforce.library.item_presence import library_item_is_missing
from mediaforce.library.media_scopes import MediaScope, media_scope_from_prefix, normalize_scope_prefix, \
    path_matches_scope, resolve_media_scope, resolve_media_scopes, scope_rel_path_filter, scopes_overlap
from mediaforce.library.remake_intents import remake_intent
from mediaforce.library.staged_validation import staged_validation_outcome

WorkflowItemState = Literal[
    "missing",
    "encode_candidate",
    "held",
    # The owner chose to keep it original for its motion pattern; it is settled, not work.
    "kept_original",
    "encoding",
    "ready_to_validate",
    "ready_to_promote",
    "complete",
    "blocked",
    "idle",
]
WorkflowLane = Literal[
    "none",
    "encode",
    "validate",
    "promote",
    "processing",
    "attention",
    "complete",
    "blocked",
    "mixed",
]
WorkflowTone = Literal["idle", "ready", "active", "attention", "success"]
WorkflowActionKind = Literal[
    "none",
    "queue_encode",
    "validate_outputs",
    "promote_outputs",
    "monitor_encode",
    "open_ops",
    "review_scope",
]
WorkflowPayload = dict[str, Any]

ENCODE_CANDIDATE_STATUSES = frozenset({"discovered", "planned", "validated"})
PROCESSING_JOB_STATUSES = frozenset({"queued", "running", "retry_backoff"})
ATTENTION_JOB_STATUSES = frozenset({"failed", "stopped", "needs_attention"})
_PROCESSING_JOB_DETAILS = {
    "queued": "Waiting its turn to compress.",
    "running": "Compressing now.",
    "retry_backoff": "Trying again soon.",
}
JOB_STATUSES_FOR_WORKFLOW = PROCESSING_JOB_STATUSES | ATTENTION_JOB_STATUSES | {"completed"}
UNFINISHED_JOB_STATUSES = PROCESSING_JOB_STATUSES | ATTENTION_JOB_STATUSES
PREFIX_QUERY_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class WorkflowNextAction:
    kind: WorkflowActionKind
    label: str
    enabled: bool
    target_prefix: str

    def to_payload(self) -> WorkflowPayload:
        return {
            "kind": self.kind,
            "label": self.label,
            "enabled": self.enabled,
            "target_prefix": self.target_prefix,
        }


@dataclass(frozen=True, slots=True)
class EncodeEligibility:
    eligible: bool
    blocker: str | None = None
    blocked: bool = False
    # Left out by the queue's motion-pattern gate rather than by policy.
    motion_held: bool = False
    # Kept original by the owner's choice for its motion pattern.
    kept_original: bool = False


class ScopeJobLane(NamedTuple):
    """The encode lane jobs give a scope, and the run that decided it."""
    lane: WorkflowLane
    detail: str
    job_id: str


@dataclass(frozen=True, slots=True)
class ScopeJobStates:
    """The encode lane from every job touching a scope, and from only the jobs at or inside it.

    A job over a wider scope, such as a whole show, cannot be working on a season whose files are all
    held or already finished, so for such a season only its own jobs count.
    """
    overlapping: ScopeJobLane | None
    own: ScopeJobLane | None


NO_SCOPE_JOBS = ScopeJobStates(overlapping=None, own=None)
# Files no job from a wider scope can still be working on: held, kept original, finished, or gone from disk.
OUT_OF_WIDER_JOB_STATES = frozenset({"held", "kept_original", "complete", "missing"})


@dataclass(frozen=True, slots=True)
class ItemWorkflowState:
    item_id: int
    rel_path: str
    status: str
    state: WorkflowItemState
    lane: WorkflowLane
    has_staged_output: bool
    blocker: str | None = None
    motion_held: bool = False

    def to_payload(self) -> WorkflowPayload:
        return {
            "item_id": self.item_id,
            "rel_path": self.rel_path,
            "status": self.status,
            "state": self.state,
            "lane": self.lane,
            "has_staged_output": self.has_staged_output,
            "blocker": self.blocker,
            "motion_held": self.motion_held,
        }


@dataclass(frozen=True, slots=True)
class FolderWorkflowState:
    prefix: str
    state: str
    primary_lane: WorkflowLane
    label: str
    tone: WorkflowTone
    detail: str
    counts: dict[str, int]
    lane_counts: dict[str, int]
    state_counts: dict[str, int]
    next_action: WorkflowNextAction
    blockers: list[str]
    items: tuple[ItemWorkflowState, ...]
    # The encode run whose state set the lane, when a job did.
    source_job_id: str | None = None

    def to_payload(self) -> WorkflowPayload:
        return {
            "prefix": self.prefix,
            "state": self.state,
            "primary_lane": self.primary_lane,
            "label": self.label,
            "tone": self.tone,
            "detail": self.detail,
            "counts": dict(self.counts),
            "lane_counts": dict(self.lane_counts),
            "state_counts": dict(self.state_counts),
            "next_action": self.next_action.to_payload(),
            "blockers": list(self.blockers),
            "source_job_id": self.source_job_id,
        }


def build_folder_workflow_state(
        connection: DBClient,
        prefix: str,
        *,
        candidate_eligibility: Mapping[int, EncodeEligibility] | None = None,
        library_types: Mapping[str, str] | None = None,
) -> FolderWorkflowState:
    scope = resolve_media_scope(connection, prefix, library_types=library_types)
    item_states = tuple(
        _derive_item_workflow_state(row, candidate_eligibility)
        for row in _load_item_rows(connection, scope)
    )
    job_states = _load_encode_job_state(connection, scope, settled_paths=_settled_paths(item_states))
    return _build_folder_state(scope.prefix, item_states, job_states=job_states)


def build_folder_workflow_states(
        connection: DBClient,
        prefixes: list[str],
        *,
        candidate_eligibility: Mapping[int, EncodeEligibility] | None = None,
        library_types: Mapping[str, str] | None = None,
) -> dict[str, FolderWorkflowState]:
    normalized_prefixes = list(dict.fromkeys(normalize_prefix(prefix) for prefix in prefixes))
    if not normalized_prefixes:
        return {}
    scopes = resolve_media_scopes(connection, normalized_prefixes, library_types=library_types)
    return build_folder_workflow_states_for_scopes(
        connection,
        scopes,
        candidate_eligibility=candidate_eligibility,
    )


def build_folder_workflow_states_for_scopes(
        connection: DBClient,
        scopes: list[MediaScope],
        *,
        candidate_eligibility: Mapping[int, EncodeEligibility] | None = None,
) -> dict[str, FolderWorkflowState]:
    if not scopes:
        return {}
    rows = _load_item_rows_for_scopes(connection, scopes)
    rows_by_prefix = _group_item_rows_by_scope(scopes, rows)
    states_by_row = {id(row): _derive_item_workflow_state(row, candidate_eligibility) for row in rows}
    job_states = _load_encode_job_states(connection, scopes, settled_paths=_settled_paths(states_by_row.values()))
    result: dict[str, FolderWorkflowState] = {}
    for scope in scopes:
        item_states = tuple(states_by_row[id(row)] for row in rows_by_prefix[scope.prefix])
        result[scope.prefix] = _build_folder_state(
            scope.prefix,
            item_states,
            job_states=job_states.get(scope.prefix, NO_SCOPE_JOBS),
        )
    return result


def _group_item_rows_by_scope(
        scopes: list[MediaScope],
        rows: list[DBRow],
) -> dict[str, list[DBRow]]:
    rows_by_prefix = {scope.prefix: [] for scope in scopes}
    exact_prefixes = {scope.prefix for scope in scopes if scope.match == "exact_item"}
    descendant_prefixes = {scope.prefix for scope in scopes if scope.match == "descendants"}
    for row in rows:
        rel_path = normalize_scope_prefix(str(row["rel_path"]))
        if "" in descendant_prefixes:
            rows_by_prefix[""].append(row)
        if rel_path in exact_prefixes:
            rows_by_prefix[rel_path].append(row)
        ancestor = ""
        for segment in rel_path.split("/")[:-1]:
            ancestor = segment if not ancestor else f"{ancestor}/{segment}"
            if ancestor in descendant_prefixes:
                rows_by_prefix[ancestor].append(row)
    return rows_by_prefix


def normalize_prefix(prefix: str) -> str:
    return normalize_scope_prefix(prefix)


def derive_item_workflow_state(
        row: Mapping[str, Any],
        *,
        encode_eligible: bool = True,
        policy_blocker: str | None = None,
        encode_blocked: bool = False,
        motion_held: bool = False,
        kept_original: bool = False,
) -> ItemWorkflowState:
    status = str(row["status"] or "idle")
    # A remake that already removed its finished copy waits to be queued, not on that copy's old check.
    has_staged_output = (row["staged_library_item_id"] is not None and row["staging_path"] is not None
                         and remake_intent(row).get("state") != "removed")
    promoted_at = row["promoted_at"]
    validation = staged_validation_outcome(row.get("validation_json")) if has_staged_output else None
    state: WorkflowItemState
    lane: WorkflowLane
    blocker: str | None = None
    held_for_motion = False

    if library_item_is_missing(row):
        state = "missing"
        lane = "none"
    elif status == "promoted" or promoted_at is not None:
        state = "complete"
        lane = "complete"
    elif status == "encoding":
        state = "encoding"
        lane = "processing"
    elif validation == "remaking":
        # Mediaforce remakes a failed copy by itself; that is encoding work, not a question for the owner.
        state = "encode_candidate"
        lane = "encode"
        blocker = "Its compressed copy didn't pass its check, so Mediaforce is making it again."
    elif validation is not None:
        state = "blocked"
        lane = "blocked"
        blocker = (
            "A finished file is much smaller than expected; keep it or make it again."
            if validation == "size_held"
            else "A finished file didn't pass its check."
        )
    elif kept_original:
        state = "kept_original"
        lane = "none"
        blocker = policy_blocker
    elif not encode_eligible:
        state = "blocked" if encode_blocked else "held"
        lane = "blocked" if encode_blocked else "none"
        blocker = policy_blocker or "This item is excluded from the current production scope."
        held_for_motion = motion_held and not encode_blocked
    elif has_staged_output and status == "encoded":
        state = "ready_to_validate"
        lane = "validate"
    elif has_staged_output and status == "validated":
        state = "ready_to_promote"
        lane = "promote"
    elif status == "encoded":
        state = "blocked"
        lane = "blocked"
        blocker = "Encoded item is missing its staged output."
    elif status in ENCODE_CANDIDATE_STATUSES:
        state = "encode_candidate"
        lane = "encode"
    else:
        state = "idle"
        lane = "none"

    return ItemWorkflowState(
        item_id=int(row["item_id"]),
        rel_path=str(row["rel_path"]),
        status=status,
        state=state,
        lane=lane,
        has_staged_output=has_staged_output,
        blocker=blocker,
        motion_held=held_for_motion,
    )


def _derive_item_workflow_state(
        row: DBRow,
        candidate_eligibility: Mapping[int, EncodeEligibility] | None,
) -> ItemWorkflowState:
    eligibility = (
        candidate_eligibility.get(int(row["item_id"]), EncodeEligibility(eligible=True))
        if candidate_eligibility is not None
        else EncodeEligibility(eligible=True)
    )
    return derive_item_workflow_state(
        row,
        encode_eligible=eligibility.eligible,
        policy_blocker=eligibility.blocker,
        encode_blocked=eligibility.blocked,
        motion_held=eligibility.motion_held,
        kept_original=eligibility.kept_original,
    )


def _item_rows_query() -> Select[Any]:
    return (
        select(
            library_items.c.id.label("item_id"),
            library_items.c.rel_path,
            library_items.c.status,
            library_items.c.missing_since,
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.staging_path,
            staged_artifacts.c.validated_at,
            staged_artifacts.c.validation_json,
            staged_artifacts.c.promoted_at,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .order_by(library_items.c.rel_path)
    )


def _load_item_rows(connection: DBClient, scope: MediaScope) -> list[DBRow]:
    query = _item_rows_query().where(scope_rel_path_filter(library_items.c.rel_path, scope))
    return list(connection.execute(query).mappings().fetchall())


def _load_item_rows_for_scopes(connection: DBClient, scopes: list[MediaScope]) -> list[DBRow]:
    if len(scopes) <= PREFIX_QUERY_BATCH_SIZE:
        query = _item_rows_query().where(
            or_(*(scope_rel_path_filter(library_items.c.rel_path, scope) for scope in scopes))
        )
        return list(connection.execute(query).mappings().fetchall())
    # SQLite tests every row against every range in a long OR, so many scopes read their library roots
    # once and keep the rows a scope holds.
    query = _item_rows_query().where(
        or_(*(scope_rel_path_filter(library_items.c.rel_path, root) for root in sorted({scope.root for scope in scopes})))
    )
    in_scopes = _rel_path_in_scopes(scopes)
    return [row for row in connection.execute(query).mappings() if in_scopes(str(row["rel_path"]))]


def _rel_path_in_scopes(scopes: list[MediaScope]) -> Callable[[str], bool]:
    """Whether a stored path falls in any scope, as the scope's own SQL filter decides."""
    if any(not scope.prefix for scope in scopes):
        return lambda _rel_path: True
    exact = {scope.prefix for scope in scopes if scope.match == "exact_item"}
    descendants = {scope.prefix for scope in scopes if scope.match != "exact_item"}

    return lambda rel_path: rel_path in exact or any(
        ancestor in descendants for ancestor in _path_ancestors(rel_path)
    )


def _build_folder_state(
        prefix: str,
        items: tuple[ItemWorkflowState, ...],
        *,
        job_states: ScopeJobStates,
) -> FolderWorkflowState:
    lane_counts = Counter(item.lane for item in items)
    state_counts = Counter(item.state for item in items)
    blockers = list(dict.fromkeys(
        item.blocker
        for item in items
        if item.blocker and item.state == "blocked"
    ))
    # A scope whose files are all missing keeps the wider job's state rather than reading as finished.
    wider_jobs_apply = (
        not items
        or any(item.state not in OUT_OF_WIDER_JOB_STATES for item in items)
        or all(item.state == "missing" for item in items)
    )
    job_state = job_states.overlapping if wider_jobs_apply else job_states.own
    job_lane = job_state.lane if job_state is not None else None
    job_detail = job_state.detail if job_state is not None else None
    counts = {
        "items": len(items),
        "encode_candidates": state_counts.get("encode_candidate", 0),
        "held": state_counts.get("held", 0),
        "motion_held": sum(item.motion_held for item in items),
        "kept_original": state_counts.get("kept_original", 0),
        "ready_to_validate": state_counts.get("ready_to_validate", 0),
        "ready_to_promote": state_counts.get("ready_to_promote", 0),
        "processing": lane_counts.get("processing", 0),
        "complete": lane_counts.get("complete", 0),
        "blocked": lane_counts.get("blocked", 0),
    }
    state, lane, label, tone, detail, action = _folder_summary(
        prefix,
        counts,
        lane_counts,
        blockers,
        job_lane=job_lane,
        job_detail=job_detail,
    )
    return FolderWorkflowState(
        prefix=prefix,
        state=state,
        primary_lane=lane,
        label=label,
        tone=tone,
        detail=detail,
        counts=counts,
        lane_counts=dict(lane_counts),
        state_counts=dict(state_counts),
        next_action=action,
        blockers=blockers,
        items=items,
        source_job_id=job_state.job_id if job_state is not None and lane == job_state.lane else None,
    )


def _folder_summary(
        prefix: str,
        counts: dict[str, int],
        lane_counts: Counter[str],
        blockers: list[str],
        *,
        job_lane: WorkflowLane | None,
        job_detail: str | None,
) -> tuple[str, WorkflowLane, str, WorkflowTone, str, WorkflowNextAction]:
    item_count = counts["items"]
    if item_count == 0:
        return (
            "idle",
            "none",
            "No matching items",
            "idle",
            "No library items match this scope.",
            WorkflowNextAction("none", "No action", False, prefix),
        )
    if job_lane == "processing":
        return (
            "processing",
            "processing",
            "Processing",
            "active",
            job_detail or "Encode work is active for this scope.",
            WorkflowNextAction("monitor_encode", "Monitor encode", True, prefix),
        )
    if job_lane == "attention":
        return (
            "needs_attention",
            "attention",
            "Needs attention",
            "attention",
            job_detail or "Encode work needs operator attention.",
            WorkflowNextAction("open_ops", "Open Ops", True, prefix),
        )
    if blockers:
        return (
            "blocked",
            "blocked",
            "Needs attention",
            "attention",
            blockers[0],
            WorkflowNextAction("open_ops", "Open Ops", True, prefix),
        )
    if lane_counts.get("processing", 0) > 0:
        return (
            "processing",
            "processing",
            "Processing",
            "active",
            "Encode work is active for this scope.",
            WorkflowNextAction("monitor_encode", "Monitor encode", True, prefix),
        )

    candidate_lanes: tuple[WorkflowLane, ...] = ("validate", "promote", "encode")
    actionable_lanes = [lane for lane in candidate_lanes if lane_counts.get(lane, 0) > 0]
    if len(actionable_lanes) > 1:
        return (
            "mixed",
            actionable_lanes[0],
            "Mixed work",
            "ready",
            _mixed_detail(counts),
            _mixed_next_action(prefix, actionable_lanes[0]),
        )
    if lane_counts.get("validate", 0) > 0:
        return (
            "ready_to_validate",
            "validate",
            "Ready to validate",
            "ready",
            f"{counts['ready_to_validate']} encoded output(s) need validation.",
            WorkflowNextAction("validate_outputs", "Validate outputs", True, prefix),
        )
    if lane_counts.get("promote", 0) > 0:
        return (
            "ready_to_promote",
            "promote",
            "Ready to promote",
            "ready",
            f"{counts['ready_to_promote']} validated output(s) are ready to promote.",
            WorkflowNextAction("promote_outputs", "Promote outputs", True, prefix),
        )
    if lane_counts.get("encode", 0) > 0:
        return (
            "encode_candidates",
            "encode",
            "Ready to encode",
            "ready",
            f"{counts['encode_candidates']} item(s) can be queued for encoding.",
            WorkflowNextAction("queue_encode", "Queue encode", True, prefix),
        )
    if counts["held"] > 0:
        return (
            "held",
            "none",
            "Protected",
            "idle",
            f"{counts['held']} item(s) stay original for their motion pattern."
            if counts["held"] == counts["motion_held"]
            else f"{counts['held']} item(s) are held by the library lifecycle policy.",
            WorkflowNextAction("review_scope", "Review protection", True, prefix),
        )
    return (
        "complete",
        "complete",
        "Complete",
        "success",
        f"{counts['kept_original']} item(s) stay original by your choice; nothing else is left to do."
        if counts["kept_original"]
        else "No remaining workflow action is available for this scope.",
        WorkflowNextAction("none", "No action", False, prefix),
    )


def _mixed_detail(counts: dict[str, int]) -> str:
    parts: list[str] = []
    if counts["ready_to_validate"]:
        parts.append(f"{counts['ready_to_validate']} to validate")
    if counts["ready_to_promote"]:
        parts.append(f"{counts['ready_to_promote']} to promote")
    if counts["encode_candidates"]:
        parts.append(f"{counts['encode_candidates']} to encode")
    return ", ".join(parts) if parts else "Multiple workflow states are present."


def _mixed_next_action(prefix: str, lane: WorkflowLane) -> WorkflowNextAction:
    if lane == "validate":
        return WorkflowNextAction("validate_outputs", "Validate ready outputs", True, prefix)
    if lane == "promote":
        return WorkflowNextAction("promote_outputs", "Promote ready outputs", True, prefix)
    if lane == "encode":
        return WorkflowNextAction("queue_encode", "Queue remaining encodes", True, prefix)
    return WorkflowNextAction("review_scope", "Review scope", True, prefix)


def _settled_paths(items: Iterable[ItemWorkflowState]) -> frozenset[str]:
    """Files no run's problem can still be about: published, kept original, or gone from disk."""
    return frozenset(item.rel_path for item in items if item.state in {"complete", "kept_original", "missing"})


def _load_encode_job_state(
        connection: DBClient,
        scope: MediaScope,
        *,
        settled_paths: frozenset[str] = frozenset(),
) -> ScopeJobStates:
    rows = _workflow_encode_job_rows(connection)
    return _scope_job_states(
        scope,
        [row for row in rows if scopes_overlap(scope, str(row["prefix"] or ""))],
        _manifest_files_lookup(),
        settled_paths,
    )


def _load_encode_job_states(
        connection: DBClient,
        scopes: list[MediaScope],
        *,
        settled_paths: frozenset[str] = frozenset(),
) -> dict[str, ScopeJobStates]:
    rows = _workflow_encode_job_rows(connection)
    part_files = _manifest_files_lookup()
    overlapping_rows = _overlapping_job_rows(rows)
    return {
        scope.prefix: _scope_job_states(scope, overlapping_rows(scope), part_files, settled_paths)
        for scope in scopes
    }


def _overlapping_job_rows(rows: list[DBRow]) -> Callable[[MediaScope], list[DBRow]]:
    """The jobs overlapping each scope, in job order, as `scopes_overlap` decides.

    A job covers its prefix and everything under it, so it overlaps a scope at, above or (for a scope
    that covers its descendants) below the scope's prefix. Looking jobs up by prefix keeps a library
    of seasons from testing every job against every season.
    """
    at_prefix: dict[str, list[int]] = {}
    under_prefix: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        job_prefix = media_scope_from_prefix(str(row["prefix"] or ""), match="descendants").prefix
        at_prefix.setdefault(job_prefix, []).append(index)
        for ancestor in _path_ancestors(job_prefix):
            under_prefix.setdefault(ancestor, []).append(index)

    def overlapping(scope: MediaScope) -> list[DBRow]:
        if not scope.prefix:
            return list(rows)
        indexes = {*at_prefix.get("", ()), *at_prefix.get(scope.prefix, ())}
        for ancestor in _path_ancestors(scope.prefix):
            indexes.update(at_prefix.get(ancestor, ()))
        if scope.match == "descendants":
            indexes.update(under_prefix.get(scope.prefix, ()))
        return [rows[index] for index in sorted(indexes)]

    return overlapping


def _path_ancestors(prefix: str) -> Iterator[str]:
    separator = prefix.find("/")
    while separator >= 0:
        yield prefix[:separator]
        separator = prefix.find("/", separator + 1)


def _manifest_files_lookup() -> Callable[[DBRow], tuple[str, ...]]:
    """The files a job is for, read once per job for every scope.

    Only the manifest says everything a part covers; the file its progress names may be one of
    several. A job without a readable manifest maps to no files, which keeps the older, wider reading.
    """
    files_by_job: dict[str, tuple[str, ...]] = {}

    def files(row: DBRow) -> tuple[str, ...]:
        job_id = str(row["job_id"])
        if job_id not in files_by_job:
            files_by_job[job_id] = tuple(encode_run_manifest_rel_paths({
                "manifest_path": row["manifest_path"],
                "manifest_indexes": _json_list(row["manifest_indexes_json"]),
            }))
        return files_by_job[job_id]

    return files


def _json_list(raw: Any) -> list[Any] | None:
    try:
        value = json.loads(str(raw)) if raw else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def _scope_job_states(
        scope: MediaScope,
        overlapping_rows: list[DBRow],
        part_files: Callable[[DBRow], tuple[str, ...]],
        settled_paths: frozenset[str] = frozenset(),
) -> ScopeJobStates:
    own_rows = [row for row in overlapping_rows if not _job_is_wider(scope, str(row["prefix"] or ""))]
    return ScopeJobStates(
        overlapping=_encode_job_workflow_state(
            overlapping_rows,
            counts_for_scope=_wider_run_filter(scope, overlapping_rows, part_files),
            attention_for_scope=_wider_run_attention(scope, overlapping_rows, part_files, settled_paths),
        ),
        own=_encode_job_workflow_state(
            own_rows,
            attention_for_scope=_wider_run_attention(scope, own_rows, part_files, settled_paths),
        ),
    )


def _wider_run_filter(
        scope: MediaScope,
        overlapping_rows: list[DBRow],
        part_files: Callable[[DBRow], tuple[str, ...]],
) -> Callable[[DBRow], bool]:
    """Whether a job is working on the scope.

    A run over a whole show is working on a season only while one of its unfinished parts is for a
    file in that season. When any of the run's unfinished parts cannot be traced to its files, it
    counts, as before.
    """
    parts_by_parent = _parts_by_parent(overlapping_rows)

    def part_in_scope(part: DBRow) -> bool | None:
        files = part_files(part)
        if not files:
            return None
        return any(path_matches_scope(rel_path, scope) for rel_path in files)

    def counts_for_scope(row: DBRow) -> bool:
        if not _job_is_wider(scope, str(row["prefix"] or "")):
            return True
        if row["job_kind"] == "shard":
            return part_in_scope(row) is not False
        if row["job_kind"] == "folder" and str(row["job_id"]) in parts_by_parent:
            answers = [
                part_in_scope(part)
                for part in parts_by_parent[str(row["job_id"])]
                if part["status"] in UNFINISHED_JOB_STATUSES
            ]
            return None in answers or any(answers)
        return True

    return counts_for_scope


def _wider_run_attention(
        scope: MediaScope,
        overlapping_rows: list[DBRow],
        part_files: Callable[[DBRow], tuple[str, ...]],
        settled_paths: frozenset[str] = frozenset(),
) -> Callable[[DBRow], DBRow | None]:
    """Which run's problem the scope shows, given the newest run, which needs attention.

    A run over a whole show needs attention for a season only while one of the season's files still
    has a problem in it. Each file takes its own newest part across runs, as on the Episodes list, so
    a newer run that encoded the file settles an older run's problem with it, and a newer run that
    never reached the file leaves that problem showing. A file since published or gone from disk,
    such as one the owner kept, has no problem left. A run's own scope keeps a problem that no file
    accounts for. When any part in question cannot be traced to its files, the newest run speaks for
    the scope, as before.
    """

    def attention_run(latest: DBRow) -> DBRow | None:
        wider = _job_is_wider(scope, str(latest["prefix"] or ""))
        runs = [row for row in overlapping_rows if row["job_kind"] in DISPLAY_ENCODE_JOB_KINDS]
        # Runs older than the oldest one that needs attention cannot bring a problem back.
        oldest = max(index for index, run in enumerate(runs) if run["status"] in ATTENTION_JOB_STATUSES)
        parts_by_parent = _parts_by_parent(overlapping_rows)
        newest_by_file: dict[str, tuple[str, DBRow]] = {}
        for run in runs[:oldest + 1]:
            # Within a run, a file's later part (a retry) comes first and replaces its earlier one.
            for part in parts_by_parent.get(str(run["job_id"])) or [run]:
                files = part_files(part)
                if not files:
                    return latest
                for rel_path in files:
                    if path_matches_scope(rel_path, scope):
                        newest_by_file.setdefault(rel_path, (str(part["status"]), run))
        problems = {
            rel_path: run for rel_path, (status, run) in newest_by_file.items() if status in ATTENTION_JOB_STATUSES
        }
        open_problem = next((run for rel_path, run in problems.items() if rel_path not in settled_paths), None)
        if not wider and not problems:
            return latest
        return open_problem

    return attention_run


def _parts_by_parent(rows: list[DBRow]) -> dict[str, list[DBRow]]:
    parts_by_parent: dict[str, list[DBRow]] = {}
    for row in rows:
        if row["job_kind"] == "shard" and row["parent_job_id"]:
            parts_by_parent.setdefault(str(row["parent_job_id"]), []).append(row)
    return parts_by_parent


def _job_is_wider(scope: MediaScope, job_prefix: str) -> bool:
    job_prefix = normalize_scope_prefix(job_prefix)
    if job_prefix == scope.prefix:
        return False
    return not job_prefix or scope.prefix.startswith(f"{job_prefix}/")


def _workflow_encode_job_rows(connection: DBClient) -> list[DBRow]:
    return list(connection.execute(
        select(
            encode_jobs.c.job_id,
            encode_jobs.c.parent_job_id,
            encode_jobs.c.prefix,
            encode_jobs.c.job_kind,
            encode_jobs.c.status,
            encode_jobs.c.error,
            encode_jobs.c.progress_json,
            encode_jobs.c.manifest_path,
            encode_jobs.c.manifest_indexes_json,
        )
        .where(encode_jobs.c.status.in_(JOB_STATUSES_FOR_WORKFLOW))
        .order_by(encode_jobs.c.created_at.desc(), literal_column("rowid").desc())
    ).mappings().fetchall())


def _encode_job_workflow_state(
        overlapping_rows: list[DBRow],
        *,
        counts_for_scope: Callable[[DBRow], bool] = lambda _row: True,
        attention_for_scope: Callable[[DBRow], DBRow | None] = lambda latest: latest,
) -> ScopeJobLane | None:
    """The scope's encode lane, naming every reason its files are not finished.

    Any active job, including a folder's queued or running part, keeps the scope working. Whether
    work needs the owner comes from the newest folder or single job, which summarizes its parts; a
    part that just finished must not hide it. Only a job that counts for the scope keeps it working.
    When the newest job needs the owner, `attention_for_scope` names the job whose problem the scope
    shows, or None when none of its files has one.
    """
    display_rows = [row for row in overlapping_rows if row["job_kind"] in DISPLAY_ENCODE_JOB_KINDS]
    active = next(
        (
            row
            for rows in (display_rows, overlapping_rows)
            for row in rows
            if row["status"] in PROCESSING_JOB_STATUSES and counts_for_scope(row)
        ),
        None,
    )
    latest = display_rows[0] if display_rows else None
    groups = unfinished_breakdown_groups(latest["progress_json"]) if latest is not None else []
    if active is not None:
        detail = _PROCESSING_JOB_DETAILS[active["status"]]
        owner_groups = [group for group in groups if group.get("needs_owner")]
        if owner_groups:
            detail = f"{detail} Needs you: {unfinished_breakdown_summary(owner_groups)}."
        return ScopeJobLane("processing", detail, str(active["parent_job_id"] or active["job_id"]))
    if latest is not None and latest["status"] in ATTENTION_JOB_STATUSES:
        run = attention_for_scope(latest)
        if run is None:
            return None
        groups = unfinished_breakdown_groups(run["progress_json"])
        breakdown = unfinished_breakdown_summary(groups)
        detail = (
            f"Needs you: {breakdown}."
            if breakdown
            else plain_failure_summary(run["error"]) or "This needs you to take a look."
        )
        return ScopeJobLane("attention", detail, str(run["job_id"]))
    return None
