"""Rebuild missed approval boundaries from saved approval files without inventing measurements."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import content_intent_boundary_observations, staged_artifacts
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.tuning.content_intent_observations import (
    ContentIntentBoundaryObservation,
    append_content_intent_boundary_observation,
    build_visual_content_intent_observation,
)


ADVICE_SUFFIX = ".advice.json"
# A promotion receipt needs the source and toolchain identity captured while encoding.
PRODUCTION_OUTCOME_SKIP_REASON = "encode_identity_not_captured"

BackfillStatus = Literal["recorded", "would_record", "already_recorded", "skipped"]


@dataclass(frozen=True, slots=True)
class ApprovalBackfillEntry:
    advice_file: str
    status: BackfillStatus
    reason: str | None
    observation_id: str | None


@dataclass(slots=True)
class ContentIntentBackfillReport:
    applied: bool
    entries: list[ApprovalBackfillEntry] = field(default_factory=list)
    production_outcome_skips: Counter[str] = field(default_factory=Counter)

    def to_payload(self) -> dict[str, Any]:
        statuses = Counter(entry.status for entry in self.entries)
        skip_reasons = Counter(
            entry.reason or "unknown" for entry in self.entries if entry.status == "skipped"
        )
        return {
            "applied": self.applied,
            "approvals_seen": len(self.entries),
            "boundary_observations": {
                "recorded": statuses["recorded"],
                "would_record": statuses["would_record"],
                "already_recorded": statuses["already_recorded"],
                "skipped": dict(sorted(skip_reasons.items())),
            },
            "production_outcomes": {
                "recorded": 0,
                "skipped": dict(sorted(self.production_outcome_skips.items())),
            },
            "entries": [
                {
                    "advice_file": entry.advice_file,
                    "status": entry.status,
                    "reason": entry.reason,
                    "observation_id": entry.observation_id,
                }
                for entry in self.entries
            ],
        }


def backfill_content_intent_approvals(
        connection: DBClient,
        web_state_dir: Path,
        *,
        apply: bool,
) -> ContentIntentBackfillReport:
    """Append each approval the runtime dropped; re-running finds the same rows already recorded."""
    report = ContentIntentBackfillReport(applied=apply)
    published_item_ids: set[int] = set()
    for advice_path in sorted(web_state_dir.glob(f"*{ADVICE_SUFFIX}")):
        advice = _read_json_object(advice_path)
        if not advice.get("operator_approved_at"):
            continue
        calibration_path = advice_path.with_name(
            advice_path.name.removesuffix(ADVICE_SUFFIX) + ".json"
        )
        calibration = _read_json_object(calibration_path)
        entry = _backfill_one(connection, advice_path.name, advice, calibration, apply=apply)
        report.entries.append(entry)
        library_item_id = int_value(object_dict(calibration.get("sample_item")).get("library_item_id"))
        if library_item_id not in published_item_ids and _published_without_lineage(connection, library_item_id):
            published_item_ids.add(library_item_id)
            report.production_outcome_skips[PRODUCTION_OUTCOME_SKIP_REASON] += 1
    return report


def _backfill_one(
        connection: DBClient,
        advice_name: str,
        advice: Mapping[str, Any],
        calibration: Mapping[str, Any],
        *,
        apply: bool,
) -> ApprovalBackfillEntry:
    def skipped(reason: str) -> ApprovalBackfillEntry:
        return ApprovalBackfillEntry(advice_name, "skipped", reason, None)

    if not calibration:
        return skipped("calibration_missing")
    job_id = str(calibration.get("job_id") or "").strip()
    accepted_at = str(calibration.get("accepted_at") or "").strip()
    if not accepted_at or not job_id or calibration.get("accepted_sample_job_id") != job_id:
        return skipped("calibration_replaced_since_approval")
    if calibration.get("draft_hash") != calibration.get("accepted_draft_hash"):
        return skipped("calibration_changed_since_approval")
    approval_record = _approval_record(advice, job_id)
    if approval_record is None:
        return skipped("approval_record_missing")
    result = build_visual_content_intent_observation(
        prefix=str(approval_record.get("prefix") or ""),
        sample_item=object_dict(calibration.get("sample_item")),
        calibration=calibration,
        verdict="approved",
        concern_tags=[str(tag) for tag in object_list(approval_record.get("tags"))] or ["other"],
        evidence_ids=[str(value) for value in object_list(approval_record.get("evidence_ids"))],
        moment_indexes=[int_value(value) for value in object_list(approval_record.get("moment_indexes"))],
        recorded_at=accepted_at,
        recorded_by="approval_file_backfill",
    )
    observation = result.observation
    if observation is None:
        return skipped(result.exclusion_reason or "unknown_reason")
    persisted_observation_id = _latest_series_observation_id(connection, observation)
    if persisted_observation_id is not None:
        return ApprovalBackfillEntry(advice_name, "already_recorded", None, persisted_observation_id)
    if not apply:
        return ApprovalBackfillEntry(advice_name, "would_record", None, observation.observation_id)
    append_content_intent_boundary_observation(connection, observation)
    return ApprovalBackfillEntry(advice_name, "recorded", None, observation.observation_id)


def _approval_record(advice: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
    matches = [
        record
        for record in (object_dict(value) for value in object_list(advice.get("quality_risk_records")))
        if record.get("kind") == "post_test"
        and record.get("verdict") == "approved"
        and str(record.get("sample_job_id") or "") == job_id
    ]
    return matches[-1] if matches else None


def _latest_series_observation_id(
        connection: DBClient,
        observation: ContentIntentBoundaryObservation,
) -> str | None:
    # A native approval of the same review shares the series but not the candidate's provenance
    # or ID, and later corrections supersede it; report the row that is actually current.
    return connection.execute(
        select(content_intent_boundary_observations.c.observation_id)
        .where(content_intent_boundary_observations.c.series_id == observation.series_id)
        .order_by(content_intent_boundary_observations.c.revision.desc())
        .limit(1)
    ).scalar_one_or_none()


def _published_without_lineage(connection: DBClient, library_item_id: int) -> bool:
    if library_item_id <= 0:
        return False
    return bool(
        connection.execute(
            select(func.count())
            .select_from(staged_artifacts)
            .where(staged_artifacts.c.library_item_id == library_item_id)
            .where(staged_artifacts.c.promoted_at.is_not(None))
            .where(staged_artifacts.c.target_lineage_json.is_(None))
        ).scalar_one()
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        return object_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
