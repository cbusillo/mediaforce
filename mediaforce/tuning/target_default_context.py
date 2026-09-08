"""Validate the current context for review-only target-default evidence."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import content_intent_boundary_observations
from mediaforce.core.evidence import stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.compression_intent import compression_intent_from_item
from mediaforce.tuning.content_intent_observations import (
    ContentIntentReplayContext,
    content_intent_boundary_compatibility_from_payload,
    content_intent_replay_context,
    content_intent_stream_plan_id,
    load_current_content_intent_boundary_observations,
)
from mediaforce.tuning.stream_budget import StreamBudgetIdentityError, StreamBudgetLedger
from mediaforce.tuning.target_defaults import load_target_default_report


TARGET_DEFAULT_EVIDENCE_SCHEMA_VERSION = 1


def unavailable_target_default_evidence(reason: str) -> dict[str, Any]:
    return {
        "schema_version": TARGET_DEFAULT_EVIDENCE_SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "report": None,
    }


def load_target_default_evidence(
        db_path: Path,
        *,
        budget_item: Mapping[str, Any],
        calibration: Mapping[str, Any] | None,
        advice_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep advisory database availability separate from the Studio page."""
    try:
        with open_readonly_db(db_path) as connection:
            return build_target_default_evidence(
                connection, budget_item=budget_item,
                calibration=calibration, advice_state=advice_state,
            )
    except (SQLAlchemyError, OSError):
        return unavailable_target_default_evidence("evidence_unavailable")


def build_target_default_evidence(
        connection: DBClient,
        *,
        budget_item: Mapping[str, Any],
        calibration: Mapping[str, Any] | None,
        advice_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a report only when its exact current source context is intact."""
    if not calibration:
        return unavailable_target_default_evidence("calibration_missing")

    observation_id = str(
        object_dict(object_dict(advice_state).get("content_intent_boundary_observation"))
        .get("observation_id")
        or ""
    ).strip()
    if not observation_id:
        return unavailable_target_default_evidence("boundary_observation_missing")

    reference = connection.execute(
        select(content_intent_boundary_observations).where(
            content_intent_boundary_observations.c.observation_id == observation_id,
        ),
    ).mappings().one_or_none()
    if reference is None:
        return unavailable_target_default_evidence("boundary_observation_missing")

    current = load_current_content_intent_boundary_observations(
        connection,
        intent_semantic_id=str(reference["intent_semantic_id"]),
        compatibility_key=str(reference["compatibility_key"]),
        include_withdrawn=True,
    )
    current_reference = next(
        (row for row in current if str(row.get("observation_id") or "") == observation_id),
        None,
    )
    if current_reference is None:
        return unavailable_target_default_evidence("boundary_observation_not_current")
    if str(current_reference.get("disposition") or "") != "active":
        return unavailable_target_default_evidence("boundary_observation_withdrawn")
    if not bool(current_reference.get("personalization_eligible")):
        return unavailable_target_default_evidence("boundary_observation_ineligible")

    sample_item = object_dict(calibration.get("sample_item"))
    if not sample_item:
        return unavailable_target_default_evidence("calibration_sample_missing")

    reason = _validate_bindings(
        current_reference,
        budget_item=budget_item,
        calibration=calibration,
        sample_item=sample_item,
    )
    if reason is not None:
        return unavailable_target_default_evidence(reason)

    try:
        report = load_target_default_report(
            connection,
            observation_id=observation_id,
        )
    except (ValueError, KeyError, TypeError):
        return unavailable_target_default_evidence("boundary_observation_not_current")
    return {
        "schema_version": TARGET_DEFAULT_EVIDENCE_SCHEMA_VERSION,
        "status": "available",
        "reason": None,
        "report": report.to_payload(),
    }


def _validate_bindings(
        observation: Mapping[str, Any],
        *,
        budget_item: Mapping[str, Any],
        calibration: Mapping[str, Any],
        sample_item: Mapping[str, Any],
) -> str | None:
    observed_duration = observation.get("duration_seconds")
    if budget_item.get("duration_seconds") != observed_duration:
        return "duration_mismatch"
    if sample_item.get("duration_seconds") != observed_duration:
        return "calibration_duration_mismatch"

    current_rel_path = str(budget_item.get("rel_path") or "").strip()
    if not current_rel_path or str(observation.get("source_rel_path") or "") != current_rel_path:
        return "source_rel_path_mismatch"

    current_source_id = stable_source_id(budget_item)
    if str(observation.get("source_id") or "") != current_source_id:
        return "source_id_mismatch"
    if str(sample_item.get("rel_path") or "").strip() != current_rel_path:
        return "calibration_source_mismatch"
    if stable_source_id(sample_item) != current_source_id:
        return "calibration_source_mismatch"

    current_source_fingerprint = _text(budget_item.get("source_fingerprint"))
    observed_source_fingerprint = _text(observation.get("source_fingerprint"))
    if not current_source_fingerprint or observed_source_fingerprint != current_source_fingerprint:
        return "source_fingerprint_mismatch"
    if _text(sample_item.get("source_fingerprint")) != current_source_fingerprint:
        return "calibration_source_mismatch"

    current_content_fingerprint = _text(budget_item.get("content_version_fingerprint"))
    if not current_content_fingerprint or _text(observation.get("content_fingerprint")) != current_content_fingerprint:
        return "content_version_mismatch"
    if _text(sample_item.get("content_version_fingerprint")) != current_content_fingerprint:
        return "calibration_content_mismatch"

    current_context = _replay_context(budget_item)
    sample_context = _replay_context(sample_item)
    if current_context is None:
        return "content_profile_missing"
    if sample_context is None:
        return "calibration_content_missing"
    if _text(observation.get("content_id")) != current_context.content_id:
        return "content_version_mismatch"
    if _text(observation.get("content_profile_id")) != current_context.content_profile_id:
        return "content_profile_mismatch"
    if sample_context.content_id != current_context.content_id:
        return "calibration_content_mismatch"
    if sample_context.content_profile_id != current_context.content_profile_id:
        return "calibration_content_mismatch"

    if _text(observation.get("job_id")) != _text(calibration.get("job_id")):
        return "calibration_job_mismatch"
    if _text(observation.get("artifact_fingerprint")) != _text(
            calibration.get("review_artifact_fingerprint")
    ):
        return "calibration_artifact_mismatch"

    current_policy = object_dict(budget_item.get("resolved_policy"))
    calibration_policy = object_dict(calibration.get("policy"))
    if not current_policy or not calibration_policy:
        return "policy_missing"
    current_policy_hash = stable_policy_hash(current_policy)
    if current_policy_hash != stable_policy_hash(calibration_policy):
        return "policy_hash_mismatch"
    if _text(observation.get("policy_hash")) != current_policy_hash:
        return "policy_hash_mismatch"
    if stable_policy_hash(object_dict(sample_item.get("resolved_policy"))) != current_policy_hash:
        return "calibration_policy_mismatch"

    current_intent = compression_intent_from_item(budget_item)
    calibration_intent = compression_intent_from_item(sample_item)
    if current_intent.requires_confirmation or not current_intent.confirmed:
        return "intent_unconfirmed"
    if calibration_intent.requires_confirmation or not calibration_intent.confirmed:
        return "calibration_intent_unconfirmed"
    if _text(observation.get("intent_semantic_id")) != current_intent.semantic_id:
        return "intent_mismatch"
    if calibration_intent.semantic_id != current_intent.semantic_id:
        return "calibration_intent_mismatch"

    sample_result = object_dict(calibration.get("sample_result"))
    compatibility_payload = object_dict(sample_result.get("content_intent_compatibility"))
    try:
        compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
    except (TypeError, ValueError):
        return "compatibility_missing"
    if _text(observation.get("compatibility_key")) != compatibility.compatibility_key:
        return "compatibility_mismatch"

    current_ledger = _ledger(budget_item.get("stream_budget_ledger"))
    calibration_ledger = _ledger(sample_item.get("stream_budget_ledger"))
    if current_ledger is None:
        return "stream_ledger_missing"
    if calibration_ledger is None:
        return "calibration_stream_ledger_missing"
    try:
        current_ledger.validate_item(budget_item)
    except (StreamBudgetIdentityError, ValueError):
        return "stream_ledger_mismatch"
    try:
        calibration_ledger.validate_item(sample_item)
    except (StreamBudgetIdentityError, ValueError):
        return "calibration_stream_ledger_mismatch"
    if not _same_ledger_identity(current_ledger, calibration_ledger):
        return "stream_ledger_mismatch"
    if content_intent_stream_plan_id(current_ledger.to_payload()) != compatibility.stream_plan_id:
        return "stream_plan_mismatch"
    return None


def _ledger(value: Any) -> StreamBudgetLedger | None:
    try:
        return StreamBudgetLedger.from_payload(object_dict(value))
    except (StreamBudgetIdentityError, TypeError, ValueError):
        return None


def _same_ledger_identity(left: StreamBudgetLedger, right: StreamBudgetLedger) -> bool:
    return (
        left.ledger_id == right.ledger_id
        and left.source_id == right.source_id
        and left.source_fingerprint == right.source_fingerprint
        and left.policy_hash == right.policy_hash
        and content_intent_stream_plan_id(left.to_payload())
        == content_intent_stream_plan_id(right.to_payload())
    )


def _replay_context(item: Mapping[str, Any]) -> ContentIntentReplayContext | None:
    if str(object_dict(item.get("media_fingerprint_decision")).get("status") or "") != "measured":
        return None
    try:
        return content_intent_replay_context(item)
    except ValueError:
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()
