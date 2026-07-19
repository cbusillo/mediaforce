"""Add rebuildable per-item evidence state.

Revision ID: 20260719_0011
Revises: 20260712_0010
Create Date: 2026-07-19 14:30:00
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260719_0011"
down_revision = "20260712_0010"
branch_labels = None
depends_on = None

_CADENCE_KIND = "cadence_analysis"
_FINGERPRINT_KIND = "media_fingerprint"
_CADENCE_POLICY_HASH = "sha256:6f3581cacb06b5a41606db639cdf3731ae987dfc1fe2af6bd8cdc838e249340a"
_FINGERPRINT_POLICY_HASH = "sha256:622106f9a69c2ca6886c0bba2d0b9590267a5e282a0c98b4f84f2214e35a7450"
_SPECS = {
    _CADENCE_KIND: {
        "column": "cadence_summary_json",
        "schema_version": 1,
        "analyzer_name": "mediaforce.ffmpeg_idet",
        "analyzer_version": "1",
        "policy_hash": _CADENCE_POLICY_HASH,
    },
    _FINGERPRINT_KIND: {
        "column": "media_fingerprint_json",
        "schema_version": 1,
        "analyzer_name": "mediaforce.media_fingerprint",
        "analyzer_version": "1",
        "policy_hash": _FINGERPRINT_POLICY_HASH,
    },
}


def upgrade() -> None:
    if not _has_table("library_item_evidence_state"):
        op.create_table(
            "library_item_evidence_state",
            sa.Column(
                "library_item_id",
                sa.Integer(),
                sa.ForeignKey("library_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("evidence_kind", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("summary_sha256", sa.Text(), nullable=True),
            sa.Column("source_fingerprint", sa.Text(), nullable=True),
            sa.Column("summary_schema_version", sa.Integer(), nullable=True),
            sa.Column("analyzer_name", sa.Text(), nullable=True),
            sa.Column("analyzer_version", sa.Text(), nullable=True),
            sa.Column("analyzer_runtime_version", sa.Text(), nullable=True),
            sa.Column("policy_hash", sa.Text(), nullable=False),
            sa.Column("decision_status", sa.Text(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_not_before", sa.Text(), nullable=True),
            sa.Column("last_attempt_at", sa.Text(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("library_item_id", "evidence_kind"),
        )
    if not _has_index("library_item_evidence_state", "idx_library_item_evidence_state_kind_state"):
        op.create_index(
            "idx_library_item_evidence_state_kind_state",
            "library_item_evidence_state",
            ["evidence_kind", "state", "reason"],
        )
    if not _has_index("library_item_evidence_state", "idx_library_item_evidence_state_work_ready"):
        op.create_index(
            "idx_library_item_evidence_state_work_ready",
            "library_item_evidence_state",
            ["state", "retry_not_before", "evidence_kind"],
        )
    _backfill_existing_items()


def downgrade() -> None:
    if _has_table("library_item_evidence_state"):
        op.drop_table("library_item_evidence_state")


def _backfill_existing_items() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, fingerprint, content_version_fingerprint, cadence_summary_json, "
            "media_fingerprint_json FROM library_items ORDER BY id"
        )
    ).mappings().fetchall()
    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    pending: list[dict[str, Any]] = []
    insert_statement = sa.text(
        "INSERT OR IGNORE INTO library_item_evidence_state ("
        "library_item_id, evidence_kind, state, reason, summary_sha256, source_fingerprint, "
        "summary_schema_version, analyzer_name, analyzer_version, analyzer_runtime_version, "
        "policy_hash, decision_status, attempt_count, updated_at"
        ") VALUES ("
        ":library_item_id, :evidence_kind, :state, :reason, :summary_sha256, :source_fingerprint, "
        ":summary_schema_version, :analyzer_name, :analyzer_version, :analyzer_runtime_version, "
        ":policy_hash, :decision_status, 0, :updated_at"
        ")"
    )
    for row in rows:
        source_fingerprint = (
            _optional_text(row.get("content_version_fingerprint"))
            or _optional_text(row.get("fingerprint"))
        )
        for evidence_kind, spec in _SPECS.items():
            pending.append(
                {
                    "library_item_id": int(row["id"]),
                    **_project_state(
                        evidence_kind,
                        row.get(str(spec["column"])),
                        source_fingerprint=source_fingerprint,
                        spec=spec,
                        updated_at=updated_at,
                    ),
                }
            )
        if len(pending) >= 500:
            bind.execute(insert_statement, pending)
            pending.clear()
    if pending:
        bind.execute(insert_statement, pending)


def _project_state(
        evidence_kind: str,
        summary_json: object,
        *,
        source_fingerprint: str | None,
        spec: Mapping[str, object],
        updated_at: str,
) -> dict[str, Any]:
    raw_summary = summary_json if isinstance(summary_json, str) else None
    summary_sha256 = _summary_hash(raw_summary)
    payload, state, reason = _canonical_state(raw_summary, evidence_kind, spec)
    analysis = _object_dict(payload.get("analysis")) if payload is not None else {}
    decision = _object_dict(payload.get("decision")) if payload is not None else {}
    tool = _object_dict(analysis.get("tool"))
    return {
        "evidence_kind": evidence_kind,
        "state": state,
        "reason": reason,
        "summary_sha256": summary_sha256,
        "source_fingerprint": source_fingerprint,
        "summary_schema_version": _optional_int(payload.get("schema_version")) if payload is not None else None,
        "analyzer_name": _optional_text(tool.get("name")),
        "analyzer_version": _optional_text(tool.get("version")),
        "analyzer_runtime_version": _optional_text(tool.get("ffmpeg_version")),
        "policy_hash": str(spec["policy_hash"]),
        "decision_status": _optional_text(decision.get("status")),
        "updated_at": updated_at,
    }


def _canonical_state(
        raw_summary: str | None,
        evidence_kind: str,
        spec: Mapping[str, object],
) -> tuple[dict[str, Any] | None, str, str | None]:
    if raw_summary is None or not raw_summary.strip():
        return None, "analysis_required", "missing"
    try:
        parsed = json.loads(raw_summary)
    except json.JSONDecodeError:
        return None, "analysis_required", "malformed"
    if not isinstance(parsed, dict):
        return None, "analysis_required", "malformed"
    if not parsed:
        return parsed, "analysis_required", "missing"
    if parsed.get("retry_required") is True:
        return parsed, "analysis_required", "retry_required"
    if _int_value(parsed.get("schema_version")) != int(spec["schema_version"]):
        return parsed, "analysis_required", "schema_changed"
    analysis_value = parsed.get("analysis")
    decision_value = parsed.get("decision")
    if not isinstance(analysis_value, Mapping) or not isinstance(decision_value, Mapping):
        return parsed, "analysis_required", "malformed"
    analysis = _object_dict(analysis_value)
    decision = _object_dict(decision_value)
    tool_value = analysis.get("tool")
    if not isinstance(tool_value, Mapping):
        return parsed, "analysis_required", "malformed"
    tool = _object_dict(tool_value)
    analyzer_name = _optional_text(tool.get("name"))
    analyzer_version = _optional_text(tool.get("version"))
    decision_status = _optional_text(decision.get("status"))
    if not analyzer_name or not analyzer_version or not decision_status:
        return parsed, "analysis_required", "malformed"
    if evidence_kind == _CADENCE_KIND:
        classification = _optional_text(decision.get("classification"))
        if decision_status not in {"resolved", "blocked"} or classification not in {
            "progressive",
            "tff",
            "bff",
            "telecine",
            "mixed",
            "unknown",
        }:
            return parsed, "analysis_required", "malformed"
    elif decision_status not in {"measured", "unknown"}:
        return parsed, "analysis_required", "malformed"
    if analyzer_name != spec["analyzer_name"] or analyzer_version != spec["analyzer_version"]:
        return parsed, "analysis_required", "tool_changed"
    if not _measurements_are_usable(evidence_kind, parsed, analysis, decision):
        return parsed, "analysis_required", "unknown"
    if evidence_kind == _CADENCE_KIND:
        unknown = str(decision.get("classification") or "") == "unknown"
    else:
        unknown = str(decision.get("status") or "") == "unknown"
    if unknown:
        return parsed, "analysis_required", "unknown"
    return parsed, "current", None


def _measurements_are_usable(
        evidence_kind: str,
        payload: Mapping[str, Any],
        analysis: Mapping[str, Any],
        decision: Mapping[str, Any],
) -> bool:
    sampled_frames = _int_value(analysis.get("sampled_frames"))
    if evidence_kind == _FINGERPRINT_KIND:
        return sampled_frames >= 90 and bool(_object_dict(analysis.get("aggregate")))
    classification = str(decision.get("classification") or "")
    if sampled_frames <= 0:
        probe = _object_dict(payload.get("probe"))
        return (
            classification == "progressive"
            and str(probe.get("field_order") or "").lower() == "progressive"
            and not bool(probe.get("idet_required"))
        )
    measured_frames = sum(
        _int_value(analysis.get(key))
        for key in ("tff_frames", "bff_frames", "progressive_frames", "undetermined_frames")
    )
    return sampled_frames >= 120 and measured_frames == sampled_frames


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return any(
        str(index.get("name") or "") == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )


def _object_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


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
    return _int_value(value)


def _int_value(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
