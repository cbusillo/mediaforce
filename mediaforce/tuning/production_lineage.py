"""Capture advisory exact-item lineage; hashes detect mutation, not malicious rewriting."""

import json
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import content_intent_boundary_observations
from mediaforce.core.evidence import (
    stable_json_hash,
    stable_policy_hash,
    stable_source_id,
)
from mediaforce.core.type_defs import object_dict
from mediaforce.core.utils import (
    content_version_fingerprint,
    file_fingerprint,
    file_stat_signature,
)
from mediaforce.encoding.quality import quality_toolchain_identity
from mediaforce.tuning.compression_intent import compression_intent_from_item
from mediaforce.tuning.content_intent_observations import (
    build_content_intent_boundary_compatibility,
    content_intent_boundary_observation_from_values,
    content_intent_frame_rate,
    content_intent_stream_plan_id,
)
from mediaforce.tuning.target_default_context import current_target_boundary

LOGGER = logging.getLogger(__name__)
LINEAGE_SCHEMA_VERSION = 1
OUTPUT_FINGERPRINT_ALGORITHM = "content_version_sampled_sha1_v1"
OUTPUT_IDENTITY_LIMITATION = (
    "Size and sampled head/middle/tail content identity; not a full-file digest."
)


def lineage_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return object_dict(value)


def seal_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    return {**body, "payload_sha256": stable_json_hash(body)}


def valid_lineage_hash(payload: Mapping[str, Any]) -> bool:
    return (
        bool(payload)
        and payload.get("payload_sha256") == seal_lineage(payload)["payload_sha256"]
    )


def production_item_identity(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "library_item_id": item.get("library_item_id"),
        "source_id": stable_source_id(item),
        "source_rel_path": item.get("rel_path"),
        "source_fingerprint": item.get("source_fingerprint"),
        "content_fingerprint": item.get("content_version_fingerprint"),
        "duration_seconds": item.get("duration_seconds"),
        "policy_hash": stable_policy_hash(object_dict(item.get("resolved_policy"))),
        "intent_semantic_id": compression_intent_from_item(item).semantic_id,
        "stream_ledger_hash": stable_json_hash(
            object_dict(item.get("stream_budget_ledger"))
        ),
        "size_goal": object_dict(
            object_dict(item.get("resolved_operator_intent")).get("size_goal")
        ),
    }


def approved_boundary_reason(row: Mapping[str, Any]) -> str | None:
    try:
        observation = content_intent_boundary_observation_from_values(row)
    except (ValueError, TypeError, KeyError):
        return "boundary_integrity_invalid"
    if observation.authority != "runtime_native":
        return "boundary_not_runtime_native"
    if observation.disposition != "active" or not observation.personalization_eligible:
        return "boundary_not_active_eligible"
    if (
        observation.verdict != "acceptable"
        or observation.boundary_kind != "upper_bound"
    ):
        return "boundary_not_approved"
    if not observation.quality_floor_met:
        return "boundary_quality_floor_not_met"
    return None


def current_approved_boundary(
    connection: DBClient, observation_id: str
) -> tuple[dict[str, Any], str | None]:
    row = (
        connection.execute(
            select(content_intent_boundary_observations).where(
                content_intent_boundary_observations.c.observation_id == observation_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return {}, "boundary_missing"
    latest = connection.execute(
        select(content_intent_boundary_observations.c.observation_id)
        .where(
            content_intent_boundary_observations.c.series_id == row["series_id"],
        )
        .order_by(content_intent_boundary_observations.c.revision.desc())
        .limit(1)
    ).scalar_one()
    if latest != observation_id:
        return dict(row), "boundary_superseded_or_withdrawn"
    return dict(row), approved_boundary_reason(row)


def attach_target_lineage(
    connection: DBClient,
    *,
    manifest: dict[str, Any],
    calibration: Mapping[str, Any],
    advice_state: Mapping[str, Any],
    approval_contract: Mapping[str, Any] | None,
) -> None:
    """One reviewed item can create one capsule. Advisory failures never admit or block work."""
    for item in manifest["items"]:
        item.pop("target_lineage", None)
    try:
        sample = object_dict(calibration.get("sample_item"))
        matches = [
            (index, item)
            for index, item in enumerate(manifest["items"])
            if item.get("library_item_id") == sample.get("library_item_id")
        ]
        if len(matches) != 1 or not approval_contract:
            return
        index, item = matches[0]
        if (
            not calibration.get("accepted_at")
            or not calibration.get("accepted_draft_hash")
            or calibration.get("accepted_sample_job_id") != calibration.get("job_id")
            or approval_contract.get("sample_job_id") != calibration.get("job_id")
            or approval_contract.get("policy_hash")
            != calibration.get("accepted_policy_hash")
        ):
            return
        row, reason = current_target_boundary(
            connection,
            budget_item=item,
            calibration=calibration,
            advice_state=advice_state,
        )
        if (
            row is None
            or reason is not None
            or approved_boundary_reason(row) is not None
        ):
            return
        identity = production_item_identity(item)
        sample_crf = lineage_object(row["assessment_json"]).get("chosen_crf")
        if not finite_number(sample_crf) or not 0 <= sample_crf <= 63:
            return
        item["target_lineage"] = seal_lineage(
            {
                "schema_version": LINEAGE_SCHEMA_VERSION,
                "observation_id": row["observation_id"],
                "observation_hash": row["payload_sha256"],
                "sample_job_id": row["job_id"],
                "sample_artifact_fingerprint": row["artifact_fingerprint"],
                "sample_crf": sample_crf,
                "compatibility_key": row["compatibility_key"],
                "compatibility": lineage_object(row["compatibility_json"]),
                "item_identity": identity,
                "approval_contract": dict(approval_contract),
                "accepted_at": calibration["accepted_at"],
                "manifest_run_id": manifest["run_id"],
                "item_index": index,
            }
        )
    except Exception as exc:
        LOGGER.warning("Target lineage unavailable at admission: %s", exc)


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def capture_production_identity(
    item: Mapping[str, Any],
    source_path: Path,
    *,
    quality_metric: str,
    host: dict[str, Any] | None,
    process_controller: Any,
) -> dict[str, Any] | None:
    if not item.get("target_lineage"):
        return None
    try:
        source_stat = source_path.stat()
        fingerprint = content_version_fingerprint(source_path, source_stat)
        if file_stat_signature(source_path.stat()) != file_stat_signature(source_stat):
            return {
                "status": "unavailable",
                "reason": "source_changed_during_identity_capture",
            }
        return {
            "status": "available",
            "item_identity": production_item_identity(item),
            "source_fingerprint": file_fingerprint(
                source_path, source_stat, item.get("duration_seconds")
            ),
            "content_fingerprint": fingerprint,
            "toolchain": quality_toolchain_identity(
                quality_metric=quality_metric,
                host=host,
                process_controller=process_controller,
            ),
        }
    except Exception as exc:
        # Advisory probes must not discard completed encodes. The encode path
        # retains its own process-controller cancellation and deadline checks.
        LOGGER.warning("Target lineage production identity unavailable: %s", exc)
        return {"status": "unavailable", "reason": "production_identity_unavailable"}


def completed_target_lineage(
    *,
    item: Mapping[str, Any],
    manifest_run_id: str,
    index: int,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    context: Mapping[str, Any],
    chosen_crf: float,
) -> str | None:
    capsule = object_dict(item.get("target_lineage"))
    if not capsule:
        return None
    try:
        if (
            not valid_lineage_hash(capsule)
            or capsule.get("schema_version") != LINEAGE_SCHEMA_VERSION
        ):
            return None
        if (
            capsule.get("manifest_run_id") != manifest_run_id
            or capsule.get("item_index") != index
        ):
            return None
        compatibility = None
        identity = object_dict(after)
        toolchain = object_dict(identity.get("toolchain"))
        if toolchain.get("status") == "available" and context:
            compatibility = build_content_intent_boundary_compatibility(
                encoder=str(context["encoder"]),
                encoder_version=str(toolchain["encoder_version"]),
                encoder_runtime_version=str(toolchain["encoder_runtime_version"]),
                encoder_runtime_signature_id=str(
                    toolchain["encoder_runtime_signature_id"]
                ),
                quality_tool=str(toolchain["quality_tool"]),
                quality_tool_version=str(toolchain["quality_tool_version"]),
                metric_runtime_signature_id=str(
                    toolchain["metric_runtime_signature_id"]
                ),
                preset=int(context["preset"]),
                pixel_format=str(context["pixel_format"]),
                encoder_parameters=str(context.get("encoder_parameters") or "").split(
                    ":"
                )
                if context.get("encoder_parameters")
                else [],
                output_width=int(context["output_width"]),
                output_height=int(context["output_height"]),
                frame_rate=content_intent_frame_rate(item) or "",
                cadence_transform=str(
                    object_dict(item.get("cadence_decision")).get("transform") or "none"
                ),
                video_filter=context.get("video_filter"),
                output_container=str(context["output_container"]),
                stream_plan_id=content_intent_stream_plan_id(
                    object_dict(item.get("stream_budget_ledger"))
                )
                or "",
                measurement_basis="sample_projection",
                quality_metric=str(context["metric"]),
                quality_target=float(context["target"]),
                minimum_quality_score=float(context["minimum_quality_score"]),
            ).to_payload()
        return json.dumps(
            seal_lineage(
                {
                    "schema_version": LINEAGE_SCHEMA_VERSION,
                    "capsule": capsule,
                    "identity_before": before,
                    "identity_after": after,
                    "compatibility": compatibility,
                    "chosen_crf": chosen_crf,
                }
            ),
            separators=(",", ":"),
        )
    except Exception as exc:
        LOGGER.warning("Target lineage completion evidence unavailable: %s", exc)
        return None


def validated_target_identity(
    stage_row: Mapping[str, Any], item: Mapping[str, Any], path: Path
) -> dict[str, Any] | None:
    if not stage_row.get("target_lineage_json"):
        return None
    try:
        stat = path.stat()
        fingerprint = content_version_fingerprint(path, stat)
        if file_stat_signature(path.stat()) != file_stat_signature(stat):
            return None
        return {
            "algorithm": OUTPUT_FINGERPRINT_ALGORITHM,
            "limitation": OUTPUT_IDENTITY_LIMITATION,
            "content_fingerprint": fingerprint,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "item_identity": production_item_identity(item),
            "lineage_hash": lineage_object(stage_row["target_lineage_json"]).get(
                "payload_sha256"
            ),
            "encode_identity": encode_lineage_identity(stage_row),
        }
    except Exception as exc:
        LOGGER.warning("Target lineage validation identity unavailable: %s", exc)
        return None


def encode_lineage_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "library_item_id",
            "manifest_run_id",
            "item_index",
            "encode_job_id",
            "encode_started_at",
            "encode_completed_at",
            "staging_fingerprint",
        )
    }
