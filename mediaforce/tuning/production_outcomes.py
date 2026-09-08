"""Append-only promotion receipts and read-only target-learning eligibility."""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import (
    library_items,
    staged_artifacts,
    target_production_outcomes,
)
from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.content_intent_observations import (
    content_intent_boundary_compatibility_from_payload,
)
from mediaforce.tuning.production_lineage import (
    LINEAGE_SCHEMA_VERSION,
    OUTPUT_FINGERPRINT_ALGORITHM,
    OUTPUT_IDENTITY_LIMITATION,
    current_approved_boundary,
    encode_lineage_identity,
    finite_number,
    lineage_object,
    production_item_identity,
    valid_lineage_hash,
)


LOGGER = logging.getLogger(__name__)


def prepare_target_production_outcome(
    stage_row: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Prepare advisory evidence before any promotion filesystem mutation."""
    try:
        return _prepare_target_production_outcome(stage_row, item)
    except Exception as exc:
        LOGGER.warning("Target production receipt unavailable: %s", exc)
        return None


def _prepare_target_production_outcome(
    stage_row: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any] | None:
    lineage = lineage_object(stage_row.get("target_lineage_json"))
    capsule = lineage_object(lineage.get("capsule"))
    identity = encode_lineage_identity(stage_row)
    if (
        not valid_lineage_hash(lineage)
        or not valid_lineage_hash(capsule)
        or lineage.get("schema_version") != LINEAGE_SCHEMA_VERSION
        or capsule.get("schema_version") != LINEAGE_SCHEMA_VERSION
        or not isinstance(capsule.get("observation_id"), str)
        or not capsule["observation_id"]
        or not isinstance(identity.get("library_item_id"), int)
        or not identity.get("encode_job_id")
        or not identity.get("manifest_run_id")
        or not isinstance(identity.get("item_index"), int)
    ):
        return None
    payload = {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "lineage": lineage,
        "encode_identity": identity,
        "promotion_item_identity": production_item_identity(item),
        "quality_metric": stage_row.get("quality_metric"),
        "quality_target": stage_row.get("quality_target"),
        "quality_score": stage_row.get("quality_score"),
        "chosen_crf": stage_row.get("chosen_crf"),
        "encode_command_json": stage_row.get("encode_command_json"),
        "validation": lineage_object(stage_row.get("validation_json")),
        "staged_at": stage_row.get("staged_at"),
        "validated_at": stage_row.get("validated_at"),
        "fingerprint_algorithm": OUTPUT_FINGERPRINT_ALGORITHM,
        "identity_limitation": OUTPUT_IDENTITY_LIMITATION,
    }
    # Freeze JSON values and reject nonfinite or unserializable evidence before moves.
    return json.loads(json.dumps(payload, allow_nan=False))


def append_target_production_outcome(
    connection: DBClient,
    *,
    prepared: Mapping[str, Any] | None,
    promoted_at: str,
    promoted_path: str,
    promoted_content_fingerprint: str,
    promoted_size_bytes: int,
    promoted_mtime_ns: int,
) -> None:
    """Participate in promotion's transaction; a conflicting receipt must roll it back."""
    if prepared is None:
        return
    identity = prepared["encode_identity"]
    capsule = prepared["lineage"]["capsule"]
    natural_key = {
        "library_item_id": identity["library_item_id"],
        "manifest_run_id": identity["manifest_run_id"],
        "item_index": identity["item_index"],
        "encode_job_id": identity["encode_job_id"],
        "promoted_content_fingerprint": promoted_content_fingerprint,
    }
    payload = {
        **prepared,
        "promoted_at": promoted_at,
        "promoted_path": promoted_path,
        "promoted_content_fingerprint": promoted_content_fingerprint,
        "promoted_size_bytes": promoted_size_bytes,
        "promoted_mtime_ns": promoted_mtime_ns,
    }
    receipt_id = f"tpo1_{stable_json_hash(natural_key)}"
    values = {
        **natural_key,
        "receipt_id": receipt_id,
        "observation_id": capsule["observation_id"],
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "payload_sha256": stable_json_hash(payload),
        "recorded_at": promoted_at,
    }
    connection.execute(
        sqlite_insert(target_production_outcomes)
        .values(**values)
        .on_conflict_do_nothing()
    )
    stored_hash = connection.execute(
        select(target_production_outcomes.c.payload_sha256).where(
            target_production_outcomes.c.receipt_id == receipt_id,
        )
    ).scalar_one()
    if stored_hash != values["payload_sha256"]:
        raise ValueError(
            "A target production receipt already exists with different immutable evidence"
        )


def load_target_production_report(
    connection: DBClient, *, observation_id: str
) -> dict[str, Any]:
    """Eligibility is re-proved now. This report never adopts or changes a target."""
    boundary, boundary_reason = current_approved_boundary(connection, observation_id)
    receipts = connection.execute(
        select(target_production_outcomes)
        .where(
            target_production_outcomes.c.observation_id == observation_id,
        )
        .order_by(
            target_production_outcomes.c.recorded_at,
            target_production_outcomes.c.receipt_id,
        )
    ).mappings()
    outcomes = []
    for receipt in receipts:
        payload = lineage_object(receipt["payload_json"])
        reason = boundary_reason or _receipt_integrity_reason(receipt, payload)
        if reason is None:
            reason = _production_eligibility_reason(payload, boundary)
        if reason is None:
            reason = _current_output_reason(connection, receipt, payload)
        outcomes.append(
            {
                "receipt_id": receipt["receipt_id"],
                "eligible": reason is None,
                "reason": reason,
                "actual_output_bytes": payload.get("promoted_size_bytes"),
                "promoted_at": payload.get("promoted_at"),
                "encode_identity": payload.get("encode_identity"),
                "sample_crf": lineage_object(
                    lineage_object(payload.get("lineage")).get("capsule")
                ).get("sample_crf"),
                "production_crf": payload.get("chosen_crf"),
            }
        )
    eligible = sum(outcome["eligible"] for outcome in outcomes)
    return {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "mode": "evidence_only",
        "observation_id": observation_id,
        "eligible_outcome_count": eligible,
        "fallback_reason": boundary_reason
        or (
            "no_linked_production_outcome"
            if not outcomes
            else "no_eligible_linked_production_outcome"
            if not eligible
            else None
        ),
        "fingerprint_algorithm": OUTPUT_FINGERPRINT_ALGORITHM,
        "identity_limitation": OUTPUT_IDENTITY_LIMITATION,
        "adoption_enabled": False,
        "outcomes": outcomes,
    }


def _receipt_integrity_reason(
    receipt: Mapping[str, Any], payload: Mapping[str, Any]
) -> str | None:
    if payload.get("schema_version") != LINEAGE_SCHEMA_VERSION:
        return "unsupported_receipt_version"
    if receipt["payload_sha256"] != stable_json_hash(payload):
        return "receipt_integrity_invalid"
    lineage = lineage_object(payload.get("lineage"))
    capsule = lineage_object(lineage.get("capsule"))
    if (
        lineage.get("schema_version") != LINEAGE_SCHEMA_VERSION
        or capsule.get("schema_version") != LINEAGE_SCHEMA_VERSION
        or not valid_lineage_hash(lineage)
        or not valid_lineage_hash(capsule)
        or capsule.get("observation_id") != receipt["observation_id"]
    ):
        return "lineage_integrity_invalid"
    identity = lineage_object(payload.get("encode_identity"))
    if any(
        identity.get(key) != receipt[key]
        for key in ("library_item_id", "manifest_run_id", "item_index", "encode_job_id")
    ):
        return "receipt_encode_identity_mismatch"
    if (
        payload.get("promoted_content_fingerprint")
        != receipt["promoted_content_fingerprint"]
    ):
        return "receipt_output_identity_mismatch"
    return None


def _production_eligibility_reason(
    payload: Mapping[str, Any], boundary: Mapping[str, Any]
) -> str | None:
    lineage = lineage_object(payload.get("lineage"))
    capsule = lineage_object(lineage.get("capsule"))
    if capsule.get("observation_hash") != boundary["payload_sha256"]:
        return "boundary_hash_mismatch"
    if (
        capsule.get("sample_job_id") != boundary["job_id"]
        or capsule.get("sample_artifact_fingerprint")
        != boundary["artifact_fingerprint"]
    ):
        return "sample_artifact_mismatch"
    expected = lineage_object(capsule.get("item_identity"))
    observed_fields = (
        "library_item_id",
        "source_id",
        "source_rel_path",
        "source_fingerprint",
        "content_fingerprint",
        "duration_seconds",
        "policy_hash",
        "intent_semantic_id",
    )
    if any(expected.get(key) != boundary[key] for key in observed_fields):
        return "boundary_source_context_mismatch"
    identity = lineage_object(payload.get("encode_identity"))
    if any(
        capsule.get(key) != identity.get(key)
        for key in ("manifest_run_id", "item_index")
    ):
        return "manifest_item_mismatch"
    if identity.get("library_item_id") != expected.get("library_item_id"):
        return "manifest_item_mismatch"
    before, after = (
        lineage_object(lineage.get("identity_before")),
        lineage_object(lineage.get("identity_after")),
    )
    if before.get("status") != "available" or after.get("status") != "available":
        return "production_identity_unavailable"
    if before != after:
        return "source_or_toolchain_changed_during_encode"
    if (
        after.get("item_identity") != expected
        or payload.get("promotion_item_identity") != expected
    ):
        return "production_item_context_mismatch"
    if any(
        after.get(key) != expected.get(key)
        for key in ("source_fingerprint", "content_fingerprint")
    ):
        return "production_source_changed"
    if lineage_object(after.get("toolchain")).get("status") != "available":
        return "production_toolchain_unavailable"
    try:
        compatibility = content_intent_boundary_compatibility_from_payload(
            lineage_object(lineage.get("compatibility"))
        )
    except (ValueError, TypeError, KeyError):
        return "production_compatibility_missing"
    if compatibility.compatibility_key != boundary["compatibility_key"]:
        return "production_compatibility_mismatch"
    sample_crf = lineage_object(boundary["assessment_json"]).get("chosen_crf")
    if (
        not finite_number(sample_crf)
        or not finite_number(payload.get("chosen_crf"))
        or payload["chosen_crf"] != sample_crf
        or lineage.get("chosen_crf") != sample_crf
        or capsule.get("sample_crf") != sample_crf
    ):
        return "production_crf_differs_from_approved_sample"
    if (
        payload.get("quality_metric") != boundary["quality_metric"]
        or payload.get("quality_target") != boundary["quality_target"]
    ):
        return "production_quality_contract_mismatch"
    if (
        not finite_number(payload.get("quality_score"))
        or payload["quality_score"] < boundary["minimum_quality_score"]
    ):
        return "production_quality_floor_not_met"
    validation = lineage_object(payload.get("validation"))
    if validation.get("passed") is not True:
        return "production_validation_not_passed"
    goal = lineage_object(expected.get("size_goal"))
    final_goal = lineage_object(validation.get("final_size_goal"))
    lower, upper, actual = (
        goal.get("final_lower_bound_bytes"),
        goal.get("final_upper_bound_bytes"),
        payload.get("promoted_size_bytes"),
    )
    if (
        not all(finite_number(value) and value > 0 for value in (lower, upper, actual))
        or lower > upper
        or not lower <= actual <= upper
        or final_goal.get("lower_bound_bytes") != lower
        or final_goal.get("upper_bound_bytes") != upper
        or final_goal.get("accepted_under_target") is not False
        or validation.get("staged_size_bytes") != actual
    ):
        return "production_not_strictly_in_band"
    output = lineage_object(validation.get("target_lineage_identity"))
    if (
        output.get("algorithm") != OUTPUT_FINGERPRINT_ALGORITHM
        or payload.get("fingerprint_algorithm") != OUTPUT_FINGERPRINT_ALGORITHM
        or not output.get("content_fingerprint")
        or output.get("content_fingerprint")
        != payload.get("promoted_content_fingerprint")
        or output.get("size_bytes") != actual
        or output.get("mtime_ns") != payload.get("promoted_mtime_ns")
        or output.get("lineage_hash") != lineage.get("payload_sha256")
        or output.get("encode_identity") != identity
        or output.get("item_identity") != expected
    ):
        return "validated_output_identity_mismatch"
    timestamps = [
        capsule.get("accepted_at"),
        identity.get("encode_started_at"),
        identity.get("encode_completed_at"),
        payload.get("staged_at"),
        payload.get("validated_at"),
        payload.get("promoted_at"),
    ]
    try:
        parsed = [datetime.fromisoformat(str(value)) for value in timestamps]
        if (
            any(value.tzinfo is None for value in parsed)
            or parsed != sorted(parsed)
            or parsed[-1] > datetime.now(UTC)
        ):
            return "production_timestamp_order_invalid"
    except (ValueError, TypeError):
        return "production_timestamp_order_invalid"
    return None


def _current_output_reason(
    connection: DBClient, receipt: Mapping[str, Any], payload: Mapping[str, Any]
) -> str | None:
    item = (
        connection.execute(
            select(library_items).where(
                library_items.c.id == receipt["library_item_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if item is None or item["status"] != "promoted":
        return "current_output_not_promoted"
    if (
        item["content_version_fingerprint"] != payload["promoted_content_fingerprint"]
        or item["source_path"] != payload["promoted_path"]
        or item["size_bytes"] != payload["promoted_size_bytes"]
        or item["mtime_ns"] != payload["promoted_mtime_ns"]
        or item["content_version_changed_at"] != payload["promoted_at"]
    ):
        return "current_output_changed"
    stage = (
        connection.execute(
            select(staged_artifacts).where(
                staged_artifacts.c.library_item_id == receipt["library_item_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if stage is None or encode_lineage_identity(stage) != payload["encode_identity"]:
        return "production_artifact_replaced"
    if (
        stage["promoted_at"] != payload["promoted_at"]
        or stage["updated_at"] != payload["promoted_at"]
        or stage["validated_at"] != payload["validated_at"]
        or lineage_object(stage["validation_json"]) != payload["validation"]
        or lineage_object(stage["target_lineage_json"]) != payload["lineage"]
    ):
        return "production_artifact_changed"
    return None
