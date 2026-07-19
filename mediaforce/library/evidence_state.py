import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.type_defs import int_value, object_dict
from mediaforce.core.utils import timestamp
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, CADENCE_SCHEMA_VERSION, CADENCE_TOOL_NAME, \
    CADENCE_TOOL_VERSION, cadence_policy_snapshot
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND, MEDIA_FINGERPRINT_SCHEMA_VERSION, \
    MEDIA_FINGERPRINT_TOOL_NAME, MEDIA_FINGERPRINT_TOOL_VERSION, media_fingerprint_policy_snapshot

EvidenceKind = Literal["cadence_analysis", "media_fingerprint"]

EVIDENCE_STATE_CURRENT = "current"
EVIDENCE_STATE_ANALYSIS_REQUIRED = "analysis_required"
EVIDENCE_STATE_CLASSIFICATION_REQUIRED = "classification_required"

EVIDENCE_REASON_MISSING = "missing"
EVIDENCE_REASON_MALFORMED = "malformed"
EVIDENCE_REASON_RETRY_REQUIRED = "retry_required"
EVIDENCE_REASON_SCHEMA_CHANGED = "schema_changed"
EVIDENCE_REASON_TOOL_CHANGED = "tool_changed"
EVIDENCE_REASON_UNKNOWN = "unknown"
EVIDENCE_REASON_SOURCE_CHANGED = "source_changed"
EVIDENCE_REASON_POLICY_CHANGED = "policy_changed"

EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    CADENCE_EVIDENCE_KIND,
    MEDIA_FINGERPRINT_EVIDENCE_KIND,
)

_PROJECTION_COLUMNS = (
    "state",
    "reason",
    "summary_sha256",
    "source_fingerprint",
    "summary_schema_version",
    "analyzer_name",
    "analyzer_version",
    "analyzer_runtime_version",
    "policy_hash",
    "decision_status",
    "updated_at",
)


@dataclass(frozen=True, slots=True)
class EvidenceStateProjection:
    evidence_kind: EvidenceKind
    state: str
    reason: str | None
    summary_sha256: str | None
    source_fingerprint: str | None
    summary_schema_version: int | None
    analyzer_name: str | None
    analyzer_version: str | None
    analyzer_runtime_version: str | None
    policy_hash: str
    decision_status: str | None
    updated_at: str

    def to_values(self, library_item_id: int) -> dict[str, Any]:
        return {
            "library_item_id": library_item_id,
            "evidence_kind": self.evidence_kind,
            "state": self.state,
            "reason": self.reason,
            "summary_sha256": self.summary_sha256,
            "source_fingerprint": self.source_fingerprint,
            "summary_schema_version": self.summary_schema_version,
            "analyzer_name": self.analyzer_name,
            "analyzer_version": self.analyzer_version,
            "analyzer_runtime_version": self.analyzer_runtime_version,
            "policy_hash": self.policy_hash,
            "decision_status": self.decision_status,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EvidenceStateRebuildStats:
    item_count: int
    row_count: int
    current_count: int
    analysis_required_count: int
    classification_required_count: int
    reason_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _EvidenceSpec:
    evidence_kind: EvidenceKind
    column_name: str
    schema_version: int
    analyzer_name: str
    analyzer_version: str
    policy_snapshot: Callable[[], Mapping[str, Any]]


_EVIDENCE_SPECS: dict[EvidenceKind, _EvidenceSpec] = {
    CADENCE_EVIDENCE_KIND: _EvidenceSpec(
        evidence_kind=CADENCE_EVIDENCE_KIND,
        column_name="cadence_summary_json",
        schema_version=CADENCE_SCHEMA_VERSION,
        analyzer_name=CADENCE_TOOL_NAME,
        analyzer_version=CADENCE_TOOL_VERSION,
        policy_snapshot=cadence_policy_snapshot,
    ),
    MEDIA_FINGERPRINT_EVIDENCE_KIND: _EvidenceSpec(
        evidence_kind=MEDIA_FINGERPRINT_EVIDENCE_KIND,
        column_name="media_fingerprint_json",
        schema_version=MEDIA_FINGERPRINT_SCHEMA_VERSION,
        analyzer_name=MEDIA_FINGERPRINT_TOOL_NAME,
        analyzer_version=MEDIA_FINGERPRINT_TOOL_VERSION,
        policy_snapshot=media_fingerprint_policy_snapshot,
    ),
}


def parse_evidence_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def project_evidence_state(
        evidence_kind: EvidenceKind,
        summary_json: object,
        *,
        source_fingerprint: str | None,
        previous_state: Mapping[str, Any] | None = None,
        preserve_source_identity: bool = True,
        preserve_policy_identity: bool = True,
        current_policy_hash: str | None = None,
        updated_at: str | None = None,
) -> EvidenceStateProjection:
    spec = _evidence_spec(evidence_kind)
    projected_at = updated_at or timestamp()
    policy_hash = current_policy_hash or stable_policy_hash(spec.policy_snapshot())
    raw_summary = summary_json if isinstance(summary_json, str) else None
    summary_hash = _summary_hash(raw_summary)
    payload, base_state, base_reason = _canonical_state(raw_summary, spec)
    analysis = object_dict(payload.get("analysis")) if payload is not None else {}
    decision = object_dict(payload.get("decision")) if payload is not None else {}
    tool = object_dict(analysis.get("tool"))
    schema_version = _optional_int(payload.get("schema_version")) if payload is not None else None
    analyzer_name = _optional_text(tool.get("name"))
    analyzer_version = _optional_text(tool.get("version"))
    analyzer_runtime_version = _optional_text(tool.get("ffmpeg_version"))
    decision_status = _optional_text(decision.get("status"))
    previous = object_dict(previous_state)
    same_summary = bool(
        previous
        and summary_hash is not None
        and str(previous.get("summary_sha256") or "") == summary_hash
    )
    bound_source_fingerprint = (
        _optional_text(previous.get("source_fingerprint"))
        if same_summary and preserve_source_identity
        else _optional_text(source_fingerprint)
    )
    bound_policy_hash = (
        str(previous.get("policy_hash") or policy_hash)
        if same_summary and preserve_policy_identity
        else policy_hash
    )
    state = base_state
    reason = base_reason
    if base_state == EVIDENCE_STATE_CURRENT and same_summary:
        current_source_fingerprint = _optional_text(source_fingerprint)
        if bound_source_fingerprint != current_source_fingerprint:
            state = EVIDENCE_STATE_ANALYSIS_REQUIRED
            reason = EVIDENCE_REASON_SOURCE_CHANGED
        elif bound_policy_hash != policy_hash:
            state = EVIDENCE_STATE_CLASSIFICATION_REQUIRED
            reason = EVIDENCE_REASON_POLICY_CHANGED

    return EvidenceStateProjection(
        evidence_kind=evidence_kind,
        state=state,
        reason=reason,
        summary_sha256=summary_hash,
        source_fingerprint=bound_source_fingerprint,
        summary_schema_version=schema_version,
        analyzer_name=analyzer_name,
        analyzer_version=analyzer_version,
        analyzer_runtime_version=analyzer_runtime_version,
        policy_hash=bound_policy_hash,
        decision_status=decision_status,
        updated_at=projected_at,
    )


def sync_library_item_evidence_states(
        connection: DBClient,
        item: Mapping[str, Any],
        *,
        preserve_source_identity: bool = True,
        preserve_policy_identity: bool = True,
        reset_attempt_state: bool = False,
        updated_at: str | None = None,
) -> list[EvidenceStateProjection]:
    library_item_id = int(item["id"])
    previous_by_kind = (
        load_library_item_evidence_states(connection, library_item_id)
        if preserve_source_identity or preserve_policy_identity
        else {}
    )
    source_fingerprint = _item_source_fingerprint(item)
    projections = [
        project_evidence_state(
            evidence_kind,
            item.get(_evidence_spec(evidence_kind).column_name),
            source_fingerprint=source_fingerprint,
            previous_state=previous_by_kind.get(evidence_kind),
            preserve_source_identity=preserve_source_identity,
            preserve_policy_identity=preserve_policy_identity,
            updated_at=updated_at,
        )
        for evidence_kind in EVIDENCE_KINDS
    ]
    _upsert_projections(
        connection,
        library_item_id,
        projections,
        reset_attempt_state=reset_attempt_state,
    )
    return projections


def sync_library_item_evidence_state(
        connection: DBClient,
        item: Mapping[str, Any],
        evidence_kind: EvidenceKind,
        *,
        preserve_source_identity: bool = True,
        preserve_policy_identity: bool = True,
        reset_attempt_state: bool = False,
        updated_at: str | None = None,
) -> EvidenceStateProjection:
    library_item_id = int(item["id"])
    previous_state = None
    if preserve_source_identity or preserve_policy_identity:
        previous_state = load_library_item_evidence_states(connection, library_item_id).get(evidence_kind)
    projection = project_evidence_state(
        evidence_kind,
        item.get(_evidence_spec(evidence_kind).column_name),
        source_fingerprint=_item_source_fingerprint(item),
        previous_state=previous_state,
        preserve_source_identity=preserve_source_identity,
        preserve_policy_identity=preserve_policy_identity,
        updated_at=updated_at,
    )
    _upsert_projections(
        connection,
        library_item_id,
        [projection],
        reset_attempt_state=reset_attempt_state,
    )
    return projection


def rebuild_library_item_evidence_states(
        connection: DBClient,
        *,
        library_item_ids: Sequence[int] | None = None,
        batch_size: int = 500,
) -> EvidenceStateRebuildStats:
    return _project_library_item_evidence_states(
        connection,
        library_item_ids=library_item_ids,
        batch_size=batch_size,
        preserve_identities=True,
    )


def _project_library_item_evidence_states(
        connection: DBClient,
        *,
        library_item_ids: Sequence[int] | None,
        batch_size: int,
        preserve_identities: bool,
) -> EvidenceStateRebuildStats:
    query = select(
        library_items.c.id,
        library_items.c.fingerprint,
        library_items.c.content_version_fingerprint,
        library_items.c.cadence_summary_json,
        library_items.c.media_fingerprint_json,
    ).order_by(library_items.c.id)
    normalized_ids = sorted({int(item_id) for item_id in library_item_ids or []})
    if library_item_ids is not None:
        if not normalized_ids:
            return EvidenceStateRebuildStats(0, 0, 0, 0, 0, {})
        query = query.where(library_items.c.id.in_(normalized_ids))

    item_count = 0
    row_count = 0
    state_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    item_batch: list[Mapping[str, Any]] = []
    projected_at = timestamp()
    current_policy_hashes = {
        evidence_kind: stable_policy_hash(_evidence_spec(evidence_kind).policy_snapshot())
        for evidence_kind in EVIDENCE_KINDS
    }
    normalized_batch_size = max(1, batch_size)
    for item in connection.execute(query).mappings():
        item_batch.append(item)
        if len(item_batch) < normalized_batch_size:
            continue
        item_count_delta, row_count_delta = _project_item_batch(
            connection,
            item_batch,
            preserve_identities=preserve_identities,
            projected_at=projected_at,
            current_policy_hashes=current_policy_hashes,
            state_counts=state_counts,
            reason_counts=reason_counts,
        )
        item_count += item_count_delta
        row_count += row_count_delta
        item_batch.clear()
    if item_batch:
        item_count_delta, row_count_delta = _project_item_batch(
            connection,
            item_batch,
            preserve_identities=preserve_identities,
            projected_at=projected_at,
            current_policy_hashes=current_policy_hashes,
            state_counts=state_counts,
            reason_counts=reason_counts,
        )
        item_count += item_count_delta
        row_count += row_count_delta

    return EvidenceStateRebuildStats(
        item_count=item_count,
        row_count=row_count,
        current_count=state_counts[EVIDENCE_STATE_CURRENT],
        analysis_required_count=state_counts[EVIDENCE_STATE_ANALYSIS_REQUIRED],
        classification_required_count=state_counts[EVIDENCE_STATE_CLASSIFICATION_REQUIRED],
        reason_counts=dict(sorted(reason_counts.items())),
    )


def _project_item_batch(
        connection: DBClient,
        items: Sequence[Mapping[str, Any]],
        *,
        preserve_identities: bool,
        projected_at: str,
        current_policy_hashes: Mapping[EvidenceKind, str],
        state_counts: Counter[str],
        reason_counts: Counter[str],
) -> tuple[int, int]:
    previous_by_item: dict[int, dict[str, dict[str, Any]]] = {}
    if preserve_identities:
        item_ids = [int(item["id"]) for item in items]
        previous_rows = connection.execute(
            select(library_item_evidence_state)
            .where(library_item_evidence_state.c.library_item_id.in_(item_ids))
        ).mappings().fetchall()
        for row in previous_rows:
            library_item_id = int(row["library_item_id"])
            previous_by_item.setdefault(library_item_id, {})[str(row["evidence_kind"])] = dict(row)

    values: list[dict[str, Any]] = []
    for item in items:
        library_item_id = int(item["id"])
        source_fingerprint = _item_source_fingerprint(item)
        for evidence_kind in EVIDENCE_KINDS:
            projection = project_evidence_state(
                evidence_kind,
                item.get(_evidence_spec(evidence_kind).column_name),
                source_fingerprint=source_fingerprint,
                previous_state=previous_by_item.get(library_item_id, {}).get(evidence_kind),
                current_policy_hash=current_policy_hashes[evidence_kind],
                updated_at=projected_at,
            )
            values.append(projection.to_values(library_item_id))
            state_counts[projection.state] += 1
            if projection.reason:
                reason_counts[projection.reason] += 1
    _upsert_projection_values(connection, values)
    return len(items), len(values)


def load_library_item_evidence_states(
        connection: DBClient,
        library_item_id: int,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        select(library_item_evidence_state)
        .where(library_item_evidence_state.c.library_item_id == library_item_id)
        .order_by(library_item_evidence_state.c.evidence_kind)
    ).mappings().fetchall()
    return {str(row["evidence_kind"]): dict(row) for row in rows}


def summarize_evidence_states(connection: DBClient) -> dict[str, Any]:
    rows = connection.execute(
        select(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
            func.count().label("item_count"),
        )
        .group_by(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
        )
        .order_by(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
        )
    ).mappings().fetchall()
    by_kind: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        evidence_kind = str(row["evidence_kind"])
        state = str(row["state"])
        reason = _optional_text(row["reason"])
        item_count = int(row["item_count"] or 0)
        total += item_count
        kind_payload = by_kind.setdefault(evidence_kind, {"total": 0, "states": {}, "reasons": {}})
        kind_payload["total"] += item_count
        kind_payload["states"][state] = kind_payload["states"].get(state, 0) + item_count
        if reason:
            kind_payload["reasons"][reason] = kind_payload["reasons"].get(reason, 0) + item_count
    return {"total": total, "by_kind": by_kind}


def _canonical_state(
        raw_summary: str | None,
        spec: _EvidenceSpec,
) -> tuple[dict[str, Any] | None, str, str | None]:
    if raw_summary is None or not raw_summary.strip():
        return None, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MISSING
    try:
        parsed = json.loads(raw_summary)
    except json.JSONDecodeError:
        return None, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    if not isinstance(parsed, dict):
        return None, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    if not parsed:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MISSING
    if parsed.get("retry_required") is True:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_RETRY_REQUIRED
    if int_value(parsed.get("schema_version")) != spec.schema_version:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_SCHEMA_CHANGED

    analysis_value = parsed.get("analysis")
    decision_value = parsed.get("decision")
    if not isinstance(analysis_value, Mapping) or not isinstance(decision_value, Mapping):
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    analysis = object_dict(analysis_value)
    decision = object_dict(decision_value)
    tool_value = analysis.get("tool")
    if not isinstance(tool_value, Mapping):
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    tool = object_dict(tool_value)
    analyzer_name = _optional_text(tool.get("name"))
    analyzer_version = _optional_text(tool.get("version"))
    decision_status = _optional_text(decision.get("status"))
    if not analyzer_name or not analyzer_version or not decision_status:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    if spec.evidence_kind == CADENCE_EVIDENCE_KIND:
        classification = _optional_text(decision.get("classification"))
        if decision_status not in {"resolved", "blocked"} or classification not in {
            "progressive",
            "tff",
            "bff",
            "telecine",
            "mixed",
            "unknown",
        }:
            return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    elif decision_status not in {"measured", "unknown"}:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_MALFORMED
    if analyzer_name != spec.analyzer_name or analyzer_version != spec.analyzer_version:
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_TOOL_CHANGED
    if not _measurements_are_usable(spec.evidence_kind, parsed, analysis, decision):
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_UNKNOWN
    if _decision_is_unknown(spec.evidence_kind, decision):
        return parsed, EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_REASON_UNKNOWN
    return parsed, EVIDENCE_STATE_CURRENT, None


def _decision_is_unknown(evidence_kind: EvidenceKind, decision: Mapping[str, Any]) -> bool:
    if evidence_kind == CADENCE_EVIDENCE_KIND:
        return str(decision.get("classification") or "") == "unknown"
    return str(decision.get("status") or "") == "unknown"


def _measurements_are_usable(
        evidence_kind: EvidenceKind,
        payload: Mapping[str, Any],
        analysis: Mapping[str, Any],
        decision: Mapping[str, Any],
) -> bool:
    sampled_frames = int_value(analysis.get("sampled_frames"))
    if evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND:
        return sampled_frames >= 90 and bool(object_dict(analysis.get("aggregate")))
    classification = str(decision.get("classification") or "")
    if sampled_frames <= 0:
        probe = object_dict(payload.get("probe"))
        return (
            classification == "progressive"
            and str(probe.get("field_order") or "").lower() == "progressive"
            and not bool(probe.get("idet_required"))
        )
    measured_frames = sum(
        int_value(analysis.get(key))
        for key in ("tff_frames", "bff_frames", "progressive_frames", "undetermined_frames")
    )
    return sampled_frames >= 120 and measured_frames == sampled_frames


def _upsert_projections(
        connection: DBClient,
        library_item_id: int,
        projections: Sequence[EvidenceStateProjection],
        *,
        reset_attempt_state: bool,
) -> None:
    values = [projection.to_values(library_item_id) for projection in projections]
    _upsert_projection_values(
        connection,
        values,
        reset_attempt_state=reset_attempt_state,
    )


def _upsert_projection_values(
        connection: DBClient,
        values: Sequence[Mapping[str, Any]],
        *,
        reset_attempt_state: bool = False,
) -> None:
    if not values:
        return
    statement = sqlite_insert(library_item_evidence_state)
    update_values = {
        column_name: getattr(statement.excluded, column_name)
        for column_name in _PROJECTION_COLUMNS
    }
    if reset_attempt_state:
        update_values.update(
            {
                "attempt_count": 0,
                "retry_not_before": None,
                "last_attempt_at": None,
                "last_error": None,
            }
        )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[
                library_item_evidence_state.c.library_item_id,
                library_item_evidence_state.c.evidence_kind,
            ],
            set_=update_values,
        ),
        [dict(value) for value in values],
    )


def _evidence_spec(evidence_kind: EvidenceKind) -> _EvidenceSpec:
    try:
        return _EVIDENCE_SPECS[evidence_kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported evidence kind: {evidence_kind}") from exc


def _item_source_fingerprint(item: Mapping[str, Any]) -> str | None:
    return (
        _optional_text(item.get("content_version_fingerprint"))
        or _optional_text(item.get("fingerprint"))
    )


def _summary_hash(raw_summary: str | None) -> str | None:
    if raw_summary is None or not raw_summary.strip():
        return None
    return f"sha256:{hashlib.sha256(raw_summary.encode(), usedforsecurity=False).hexdigest()}"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int_value(value)
