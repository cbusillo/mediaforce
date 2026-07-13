from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sqlalchemy import or_, select

from mediaforce.core.db import DBClient
from mediaforce.core.db import DBRow
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.library.media_scopes import MediaScope, normalize_scope_prefix, path_matches_scope, \
    resolve_media_scope, resolve_media_scopes, scope_rel_path_filter, scopes_overlap

WorkflowItemState = Literal[
    "missing",
    "encode_candidate",
    "held",
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
JOB_STATUSES_FOR_WORKFLOW = PROCESSING_JOB_STATUSES | ATTENTION_JOB_STATUSES | {"completed"}
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
class ItemWorkflowState:
    item_id: int
    rel_path: str
    status: str
    state: WorkflowItemState
    lane: WorkflowLane
    has_staged_output: bool
    blocker: str | None = None

    def to_payload(self) -> WorkflowPayload:
        return {
            "item_id": self.item_id,
            "rel_path": self.rel_path,
            "status": self.status,
            "state": self.state,
            "lane": self.lane,
            "has_staged_output": self.has_staged_output,
            "blocker": self.blocker,
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
        }


def build_folder_workflow_state(
        connection: DBClient,
        prefix: str,
        *,
        candidate_eligibility: dict[int, tuple[bool, str | None]] | None = None,
        library_types: Mapping[str, str] | None = None,
) -> FolderWorkflowState:
    scope = resolve_media_scope(connection, prefix, library_types=library_types)
    item_states = tuple(
        derive_item_workflow_state(
            row,
            encode_eligible=(candidate_eligibility or {}).get(int(row["item_id"]), (True, None))[0],
            policy_blocker=(candidate_eligibility or {}).get(int(row["item_id"]), (True, None))[1],
        )
        for row in _load_item_rows(connection, scope)
    )
    job_state = _load_encode_job_state(connection, scope)
    return _build_folder_state(scope.prefix, item_states, job_state=job_state)


def build_folder_workflow_states(
        connection: DBClient,
        prefixes: list[str],
        *,
        candidate_eligibility: dict[int, tuple[bool, str | None]] | None = None,
        library_types: Mapping[str, str] | None = None,
) -> dict[str, FolderWorkflowState]:
    normalized_prefixes = list(dict.fromkeys(normalize_prefix(prefix) for prefix in prefixes))
    if not normalized_prefixes:
        return {}
    scopes = resolve_media_scopes(connection, normalized_prefixes, library_types=library_types)
    rows = _load_item_rows_for_scopes(connection, scopes)
    job_states = _load_encode_job_states(connection, scopes)
    result: dict[str, FolderWorkflowState] = {}
    for scope in scopes:
        item_states = tuple(
            derive_item_workflow_state(
                row,
                encode_eligible=(candidate_eligibility or {}).get(int(row["item_id"]), (True, None))[0],
                policy_blocker=(candidate_eligibility or {}).get(int(row["item_id"]), (True, None))[1],
            )
            for row in rows
            if path_matches_scope(str(row["rel_path"]), scope)
        )
        result[scope.prefix] = _build_folder_state(
            scope.prefix,
            item_states,
            job_state=job_states.get(scope.prefix),
        )
    return result


def normalize_prefix(prefix: str) -> str:
    return normalize_scope_prefix(prefix)


def derive_item_workflow_state(
        row: DBRow,
        *,
        encode_eligible: bool = True,
        policy_blocker: str | None = None,
) -> ItemWorkflowState:
    status = str(row["status"] or "idle")
    has_staged_output = row["staged_library_item_id"] is not None and row["staging_path"] is not None
    promoted_at = row["promoted_at"]
    state: WorkflowItemState
    lane: WorkflowLane
    blocker: str | None = None

    if status == "missing":
        state = "missing"
        lane = "none"
    elif status == "promoted" or promoted_at is not None:
        state = "complete"
        lane = "complete"
    elif status == "encoding":
        state = "encoding"
        lane = "processing"
    elif not encode_eligible:
        state = "held"
        lane = "none"
        blocker = policy_blocker or "This item is excluded from the current production scope."
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
    )


def _load_item_rows(connection: DBClient, scope: MediaScope) -> list[DBRow]:
    query = (
        select(
            library_items.c.id.label("item_id"),
            library_items.c.rel_path,
            library_items.c.status,
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.staging_path,
            staged_artifacts.c.validated_at,
            staged_artifacts.c.promoted_at,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .order_by(library_items.c.rel_path)
    )
    return connection.execute(query).mappings().fetchall()


def _load_item_rows_for_scopes(connection: DBClient, scopes: list[MediaScope]) -> list[DBRow]:
    rows_by_key: dict[tuple[int, str | None], DBRow] = {}
    for scope_batch in _chunks(scopes, PREFIX_QUERY_BATCH_SIZE):
        query = (
            select(
                library_items.c.id.label("item_id"),
                library_items.c.rel_path,
                library_items.c.status,
                staged_artifacts.c.library_item_id.label("staged_library_item_id"),
                staged_artifacts.c.staging_path,
                staged_artifacts.c.validated_at,
                staged_artifacts.c.promoted_at,
            )
            .select_from(
                library_items.outerjoin(
                    staged_artifacts,
                    staged_artifacts.c.library_item_id == library_items.c.id,
                )
            )
            .where(or_(*(scope_rel_path_filter(library_items.c.rel_path, scope) for scope in scope_batch)))
            .order_by(library_items.c.rel_path)
        )
        for row in connection.execute(query).mappings().fetchall():
            rows_by_key[(int(row["item_id"]), row["staging_path"])] = row
    return sorted(rows_by_key.values(), key=lambda row: str(row["rel_path"]))


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _build_folder_state(
        prefix: str,
        items: tuple[ItemWorkflowState, ...],
        *,
        job_state: tuple[WorkflowLane, str] | None,
) -> FolderWorkflowState:
    lane_counts = Counter(item.lane for item in items)
    state_counts = Counter(item.state for item in items)
    blockers = list(dict.fromkeys(
        item.blocker
        for item in items
        if item.blocker and item.state == "blocked"
    ))
    job_lane = job_state[0] if job_state is not None else None
    job_detail = job_state[1] if job_state is not None else None
    counts = {
        "items": len(items),
        "encode_candidates": state_counts.get("encode_candidate", 0),
        "held": state_counts.get("held", 0),
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

    actionable_lanes = [lane for lane in ("validate", "promote", "encode") if lane_counts.get(lane, 0) > 0]
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
            f"{counts['held']} item(s) are held by the library lifecycle policy.",
            WorkflowNextAction("review_scope", "Review protection", True, prefix),
        )
    return (
        "complete",
        "complete",
        "Complete",
        "success",
        "No remaining workflow action is available for this scope.",
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


def _load_encode_job_state(connection: DBClient, scope: MediaScope) -> tuple[WorkflowLane, str] | None:
    rows = connection.execute(
        select(encode_jobs.c.prefix, encode_jobs.c.status, encode_jobs.c.error)
        .where(encode_jobs.c.status.in_(JOB_STATUSES_FOR_WORKFLOW))
        .order_by(encode_jobs.c.updated_at.desc(), encode_jobs.c.created_at.desc())
    ).mappings().fetchall()
    overlapping_rows = [row for row in rows if scopes_overlap(scope, str(row["prefix"] or ""))]
    active = next((row for row in overlapping_rows if row["status"] in PROCESSING_JOB_STATUSES), None)
    if active is not None:
        return "processing", f"Encode job is {active['status']} for {active['prefix']}."
    latest = overlapping_rows[0] if overlapping_rows else None
    if latest is not None and latest["status"] in ATTENTION_JOB_STATUSES:
        error = str(latest["error"] or "Encode job needs operator attention.")
        return "attention", f"Encode job is {latest['status']} for {latest['prefix']}: {error}"
    return None


def _load_encode_job_states(connection: DBClient, scopes: list[MediaScope]) -> dict[str, tuple[WorkflowLane, str] | None]:
    rows = connection.execute(
        select(encode_jobs.c.prefix, encode_jobs.c.status, encode_jobs.c.error)
        .where(encode_jobs.c.status.in_(JOB_STATUSES_FOR_WORKFLOW))
        .order_by(encode_jobs.c.updated_at.desc(), encode_jobs.c.created_at.desc())
    ).mappings().fetchall()
    result: dict[str, tuple[WorkflowLane, str] | None] = {}
    for scope in scopes:
        overlapping_rows = [row for row in rows if scopes_overlap(scope, str(row["prefix"] or ""))]
        active = next((row for row in overlapping_rows if row["status"] in PROCESSING_JOB_STATUSES), None)
        if active is not None:
            result[scope.prefix] = "processing", f"Encode job is {active['status']} for {active['prefix']}."
            continue
        latest = overlapping_rows[0] if overlapping_rows else None
        if latest is not None and latest["status"] in ATTENTION_JOB_STATUSES:
            error = str(latest["error"] or "Encode job needs operator attention.")
            result[scope.prefix] = "attention", f"Encode job is {latest['status']} for {latest['prefix']}: {error}"
            continue
        result[scope.prefix] = None
    return result
