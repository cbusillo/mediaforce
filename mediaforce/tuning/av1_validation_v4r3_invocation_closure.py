"""Protocol v4 revision-3 public invocation-closure contract.

The module freezes the public production-search inputs that revision 2 left
unresolved. It performs no I/O and imports no media/runtime execution surface;
callers must supply private path, stream-plan, ledger, and quality-temp HMAC
bindings before any real qualification run can be execution-ready.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac as _hmac_module
from types import MappingProxyType
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_LAYOUT,
    AV1_VALIDATION_V4_SOURCE_STREAM_SELECTION,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    av1_validation_v4_guided_warm_start_identities,
)


AV1_V4_R3_MANIFEST_REVISION = 3
AV1_V4_R3_PROTOCOL_VERSION = 4
AV1_V4_R3_CONTRACT_VERSION = "acsv4r3"
AV1_V4_R3_SCHEMA = "mediaforce.av1_cold_start_v4r3_invocation_closure"
AV1_V4_R3_SCHEMA_VERSION = 1
AV1_V4_R3_BINDING_STATE = "requires_private_preparation"
AV1_V4_R3_OUTPUT_CONTAINER = "mkv"
AV1_V4_R3_WARM_START_CRF = 28
AV1_V4_R3_SOURCE_VIDEO_BITRATE_BPS: None = None

AV1_V4_R3_QUALITY_TEMP_KEY_ID_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:quality-temp:key-id:v1"
)
AV1_V4_R3_QUALITY_TEMP_HMAC_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:quality-temp:instance:v1"
)
AV1_V4_R3_CLOSURE_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:invocation-closure:identity:v1"
)
AV1_V4_R3_TARGET_SIZE_GOAL_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:target-size-goal:v1"
)
AV1_V4_R3_LEDGER_CLOSURE_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:ledger-closure:v1"
)
AV1_V4_R3_WARM_START_SIGNATURE_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:warm-start:search-signature:v1"
)
AV1_V4_R3_WARM_START_COHORT_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:warm-start:content-class-cohort:v1"
)

AV1_V4_R3_REVISION_2_BASE_VIDEO_POLICY = MappingProxyType(
    {
        "compression_intent_schema_version": 1,
        "compression_intent": "balanced",
        "compression_intent_source": "operator",
        "compression_intent_confirmed": True,
        "min_crf": 10,
        "max_crf": 45,
    }
)

AV1_V4_R3_FULL_VIDEO_POLICY = MappingProxyType(
    {
        "encoder": "libsvtav1",
        "pixel_format": "yuv420p10le",
        "preset": 4,
        "crf_search": True,
        "quality_metric": "vmaf",
        "target_vmaf": 85.0,
        "target_xpsnr": 41.0,
        "min_target_vmaf": 80.0,
        "min_target_xpsnr": 35.0,
        "target_size_mb": 300,
        "target_size_bytes": 300_000_000,
        "target_runtime_minutes": 45,
        "size_goal_schema_version": 1,
        "size_goal_mode": "normalized",
        "size_goal_source": "config_default",
        "sample_projection_tolerance_percent": 10,
        "final_output_tolerance_percent": 5,
        "compression_intent_schema_version": 1,
        "compression_intent": "balanced",
        "compression_intent_source": "operator",
        "compression_intent_confirmed": True,
        "decision_model": "size_first_review",
        "quality_engine": "ab_av1_fast_sample",
        "target_relax_step_vmaf": 0.5,
        "target_relax_step_xpsnr": 1.0,
        "sample_every": "8m",
        "sample_duration": "20s",
        "min_crf": 10,
        "max_crf": 45,
        "target_search_max_crf": 63,
        "max_encoded_percent": 80,
        "default_grain": 8,
        "grain_denoise": 0,
        "thorough": True,
        "max_height": 1080,
        "resolution_intent_mode": "max_height",
        "resolution_intent_source": "config_default",
        "downsample_algorithm": "lanczos",
        "black_bar_handling": "off",
        "black_bar_detect_samples": 3,
        "black_bar_detect_seconds": 12,
        "crop": "",
    }
)

AV1_V4_R3_SOURCE_MANIFEST_FACTS = MappingProxyType(
    {
        "av1v4_animation_primary_sintel": MappingProxyType(
            {
                "duration_seconds": 888.032,
                "media_bytes": 1_180_090_590,
                "width": 1920,
                "height": 818,
                "codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": tuple(range(1, 12)),
            }
        ),
        "av1v4_animation_confirmation_cosmos_laundromat": MappingProxyType(
            {
                "duration_seconds": 730.069333,
                "media_bytes": 491_245_034,
                "width": 2048,
                "height": 858,
                "codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
            }
        ),
        "av1v4_live_action_primary_tears_of_steel": MappingProxyType(
            {
                "duration_seconds": 734.166667,
                "media_bytes": 738_876_331,
                "width": 1920,
                "height": 800,
                "codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
            }
        ),
        "av1v4_live_action_confirmation_nasa_earth_views": MappingProxyType(
            {
                "duration_seconds": 200.750563,
                "media_bytes": 234_140_076,
                "width": 1280,
                "height": 720,
                "codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
            }
        ),
    }
)

AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER = (
    "av1v4_animation_primary_sintel",
    "av1v4_live_action_primary_tears_of_steel",
    "av1v4_animation_confirmation_cosmos_laundromat",
    "av1v4_live_action_confirmation_nasa_earth_views",
)
AV1_V4_R3_REQUIRED_PRIVATE_BINDINGS = (
    "source_path_hmac_id",
    "dedicated_instance_hmac_id",
    "quality_temp_hmac_id",
    "quality_temp_key_id",
    "production_stream_plan_id",
    "stream_budget_ledger_id",
)
AV1_V4_R3_QUALITY_TEMP_KEY_BYTES_MIN = 32
AV1_V4_R3_QUALITY_TEMP_ROLE = "quality_temp"


class AV1V4R3InvocationClosureError(ValueError):
    """Raised when a revision-3 public closure invariant is violated."""


def av1_v4_r3_resolved_video_policy() -> dict[str, Any]:
    """Return the complete frozen r3 production video policy."""

    return dict(AV1_V4_R3_FULL_VIDEO_POLICY)


def av1_v4_r3_guided_warm_start_identities() -> dict[str, dict[str, Any]]:
    """Return revision-3 warm-start identities without reusing revision 2."""

    identities: dict[str, dict[str, Any]] = {}
    cohort_ids: dict[str, str] = {}
    for asset_id, content_class, _role in AV1_VALIDATION_V4_SOURCE_LAYOUT:
        cohort_id = cohort_ids.setdefault(
            content_class,
            "acsh1_"
            + stable_json_hash(
                {
                    "domain": AV1_V4_R3_WARM_START_COHORT_DOMAIN,
                    "content_class": content_class,
                }
            )[:32],
        )
        identities[asset_id] = {
            "requested_crf": float(AV1_V4_R3_WARM_START_CRF),
            "candidate_crf": AV1_V4_R3_WARM_START_CRF,
            "search_signature_id": "acss1_"
            + stable_json_hash(
                {
                    "domain": AV1_V4_R3_WARM_START_SIGNATURE_DOMAIN,
                    "asset_id": asset_id,
                }
            )[:32],
            "cohort_id": cohort_id,
            "source": "av1_cold_start_v4_qualification",
            "confidence": None,
            "provenance_id": None,
            "review_risks": [],
        }
    return identities


def av1_v4_r3_derive_target_bytes(asset_id: str) -> int:
    """Return normalized per-source target bytes for a known v4 source."""

    facts = _source_facts(asset_id)
    return int(
        round(
            AV1_V4_R3_FULL_VIDEO_POLICY["target_size_bytes"]
            * float(facts["duration_seconds"])
            / (float(AV1_V4_R3_FULL_VIDEO_POLICY["target_runtime_minutes"]) * 60.0)
        )
    )


def av1_v4_r3_bounds(value: int, tolerance_percent: float) -> dict[str, int]:
    """Return inclusive integer bounds for production size tolerance checks."""

    delta = value * tolerance_percent / 100.0
    return {
        "lower_bound_bytes": int(round(value - delta)),
        "upper_bound_bytes": int(round(value + delta)),
    }


def av1_v4_r3_resolved_size_goal_payload(asset_id: str) -> dict[str, Any]:
    """Build the deterministic target-size goal payload for one source."""

    facts = _source_facts(asset_id)
    target_bytes = av1_v4_r3_derive_target_bytes(asset_id)
    payload = {
        "schema_version": 1,
        "asset_id": asset_id,
        "mode": "normalized",
        "source": "config_default",
        "reference_target_size_bytes": AV1_V4_R3_FULL_VIDEO_POLICY["target_size_bytes"],
        "reference_target_size_mb": AV1_V4_R3_FULL_VIDEO_POLICY["target_size_mb"],
        "reference_runtime_minutes": AV1_V4_R3_FULL_VIDEO_POLICY[
            "target_runtime_minutes"
        ],
        "source_duration_seconds": facts["duration_seconds"],
        "target_size_bytes": target_bytes,
        "sample_projection_tolerance_percent": AV1_V4_R3_FULL_VIDEO_POLICY[
            "sample_projection_tolerance_percent"
        ],
        "final_output_tolerance_percent": AV1_V4_R3_FULL_VIDEO_POLICY[
            "final_output_tolerance_percent"
        ],
    }
    size_goal_id = _stable_prefixed_id(
        "av1v4r3size_",
        {**payload, "domain": AV1_V4_R3_TARGET_SIZE_GOAL_DOMAIN},
    )
    return {
        **payload,
        "sample_bounds": av1_v4_r3_bounds(
            target_bytes,
            float(payload["sample_projection_tolerance_percent"]),
        ),
        "final_bounds": av1_v4_r3_bounds(
            target_bytes,
            float(payload["final_output_tolerance_percent"]),
        ),
        "size_goal_id": size_goal_id,
    }


def av1_v4_r3_all_resolved_size_goal_payloads() -> dict[str, dict[str, Any]]:
    """Return deterministic size-goal payloads for all four sources."""

    return {
        asset_id: av1_v4_r3_resolved_size_goal_payload(asset_id)
        for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
    }


def av1_v4_r3_transform_plan_payload() -> dict[str, Any]:
    """Return the exact null-cadence transform plan payload production builds."""

    payload = {
        "schema_version": 1,
        "cadence_evidence_id": None,
        "cadence_class": None,
        "cadence_transform": None,
        "video_filter": None,
    }
    return {**payload, "transform_plan_id": f"tp1_{stable_json_hash(payload)[:32]}"}


def av1_v4_r3_quality_temp_key_id(key: bytes) -> str:
    """Derive a public key-identity token without exposing key bytes."""

    _validate_quality_temp_key(key)
    return (
        "av1vqtkey4r3_"
        + _hmac_hex(
            key,
            {
                "domain": AV1_V4_R3_QUALITY_TEMP_KEY_ID_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
            },
        )[:32]
    )


def av1_v4_r3_quality_temp_hmac_id(
    key: bytes,
    *,
    asset_id: str,
    normalized_path: str,
) -> str:
    """Return a privacy-safe HMAC ID for a quality-temp directory path."""

    _validate_quality_temp_key(key)
    if asset_id not in AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1V4R3InvocationClosureError(
            f"Quality-temp HMAC ID requires a valid source asset_id, got {asset_id!r}"
        )
    return (
        "av1vqtemp4r3_"
        + _hmac_hex(
            key,
            {
                "domain": AV1_V4_R3_QUALITY_TEMP_HMAC_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
                "role": AV1_V4_R3_QUALITY_TEMP_ROLE,
                "asset_id": asset_id,
                "normalized_path": _canonical_absolute_posix_path(normalized_path),
            },
        )[:32]
    )


def av1_v4_r3_stream_ledger_closure_payload(asset_id: str) -> dict[str, Any]:
    """Build the honest unresolved public stream-ledger closure contract."""

    facts = _source_facts(asset_id)
    size_goal = av1_v4_r3_resolved_size_goal_payload(asset_id)
    payload = {
        "schema_version": 1,
        "asset_id": asset_id,
        "binding_state": AV1_V4_R3_BINDING_STATE,
        "execution_ready": False,
        "output_container": AV1_V4_R3_OUTPUT_CONTAINER,
        "video_stream_index": facts["video_stream_index"],
        "excluded_stream_indexes": list(facts["excluded_stream_indexes"]),
        "non_video_streams_excluded": True,
        "non_video_target_bytes": 0,
        "source_media_bytes": facts["media_bytes"],
        "source_duration_seconds": facts["duration_seconds"],
        "source_video_bitrate_bps": AV1_V4_R3_SOURCE_VIDEO_BITRATE_BPS,
        "target_video_bytes": size_goal["target_size_bytes"],
        "total_target_bytes": size_goal["target_size_bytes"],
        "size_goal_id": size_goal["size_goal_id"],
        "production_source_identity_required": True,
        "production_stream_plan_id": None,
        "stream_budget_ledger_id": None,
        "quality_temp_hmac_id": None,
        "quality_temp_key_id": None,
        "required_private_bindings": list(AV1_V4_R3_REQUIRED_PRIVATE_BINDINGS),
    }
    ledger_closure_id = _stable_prefixed_id(
        "av1v4r3ledger_",
        {**payload, "domain": AV1_V4_R3_LEDGER_CLOSURE_DOMAIN},
    )
    return {
        **payload,
        "ledger_closure_id": ledger_closure_id,
    }


def av1_v4_r3_source_closure_payload(
    asset_id: str,
    configuration: str,
    ordinal: int,
) -> dict[str, Any]:
    """Build the public unresolved closure for one traversal."""

    if configuration not in AV1_VALIDATION_V4_CONFIGURATIONS:
        raise AV1V4R3InvocationClosureError(f"Unknown configuration: {configuration!r}")
    facts = _source_facts(asset_id)
    size_goal = av1_v4_r3_resolved_size_goal_payload(asset_id)
    stream_ledger = av1_v4_r3_stream_ledger_closure_payload(asset_id)
    transform_plan = av1_v4_r3_transform_plan_payload()
    warm_start = av1_v4_r3_guided_warm_start_identities()[asset_id]
    if configuration == "balanced_full_search_baseline":
        warm_start_payload = None
    else:
        warm_start_payload = dict(warm_start)
    payload = {
        "schema": AV1_V4_R3_SCHEMA,
        "schema_version": AV1_V4_R3_SCHEMA_VERSION,
        "contract_version": AV1_V4_R3_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "supersedes_manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "supersedes_manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "supersedes_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "asset_id": asset_id,
        "configuration": configuration,
        "ordinal": ordinal,
        "binding_state": AV1_V4_R3_BINDING_STATE,
        "execution_ready": False,
        "source_facts": _public_source_facts(facts),
        "video_policy": av1_v4_r3_resolved_video_policy(),
        "warm_start": warm_start_payload,
        "size_goal": size_goal,
        "transform_plan": transform_plan,
        "stream_ledger_closure": stream_ledger,
        "quality_search_adapter_contract": av1_v4_r3_quality_search_adapter_contract(),
        **av1_v4_r3_false_authority_payload(),
    }
    closure_id = _stable_prefixed_id(
        "av1vclosure4r3_",
        {**payload, "domain": AV1_V4_R3_CLOSURE_IDENTITY_DOMAIN},
    )
    return {
        **payload,
        "closure_id": closure_id,
    }


def build_av1_v4_r3_all_closure_payloads() -> tuple[dict[str, Any], ...]:
    """Return all eight r3 closures in manifest traversal order."""

    closures: list[dict[str, Any]] = []
    ordinal = 1
    for asset_id in AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER:
        for configuration in AV1_VALIDATION_V4_CONFIGURATIONS:
            closures.append(
                av1_v4_r3_source_closure_payload(asset_id, configuration, ordinal)
            )
            ordinal += 1
    return tuple(closures)


def av1_v4_r3_quality_search_adapter_contract() -> dict[str, Any]:
    """Describe the required private adapter without importing runtime code."""

    return {
        "adapter_required": True,
        "binding_state": AV1_V4_R3_BINDING_STATE,
        "execution_ready": False,
        "module": "mediaforce.execution",
        "callable": "search_quality_for_source",
        "must_build_stream_budget_ledger": True,
        "must_pass_stream_budget_ledger": True,
        "must_pass_quality_temp_dir": True,
        "transform_plan_derivation": (
            "mediaforce.encoding.quality_search._transform_plan_payload"
        ),
        "must_verify_returned_transform_plan_id": (
            av1_v4_r3_transform_plan_payload()["transform_plan_id"]
        ),
        "sample_encode_support_provided_by_seam": True,
        "requires_real_target_size_trace": True,
        "required_target_size_trace_field": "target_size_trace",
        "must_verify_returned_ledger_id": True,
        "required_private_bindings": list(AV1_V4_R3_REQUIRED_PRIVATE_BINDINGS),
    }


def av1_v4_r3_false_authority_payload() -> dict[str, bool]:
    """Return every v4 authority field set to false for public r3 payloads."""

    return {field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)}


def av1_v4_r3_public_contract_payload() -> dict[str, Any]:
    """Return the top-level public non-executing r3 contract payload."""

    return {
        "schema": AV1_V4_R3_SCHEMA,
        "schema_version": AV1_V4_R3_SCHEMA_VERSION,
        "contract_version": AV1_V4_R3_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "supersedes_manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "supersedes_manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "supersedes_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "binding_state": AV1_V4_R3_BINDING_STATE,
        "execution_ready": False,
        "owner_decision": {
            "target_size_path_intent_frozen_in_revision_2": True,
            "target_size_value_frozen_in_revision_2": False,
            "production_profile_approved_for_revision_3": True,
        },
        "video_policy": av1_v4_r3_resolved_video_policy(),
        "source_facts": {
            asset_id: _public_source_facts(facts)
            for asset_id, facts in AV1_V4_R3_SOURCE_MANIFEST_FACTS.items()
        },
        "size_goals": av1_v4_r3_all_resolved_size_goal_payloads(),
        "transform_plan": av1_v4_r3_transform_plan_payload(),
        "quality_search_adapter_contract": av1_v4_r3_quality_search_adapter_contract(),
        "closures": list(build_av1_v4_r3_all_closure_payloads()),
        **av1_v4_r3_false_authority_payload(),
    }


def validate_av1_v4_r3_source_facts_against_revision_2_manifest(
    revision_2_manifest: Mapping[str, Any],
) -> None:
    """Validate hardcoded public source facts against a caller-supplied mapping."""

    sources = _source_mapping(revision_2_manifest)
    for asset_id in AV1_VALIDATION_V4_SOURCE_IDS:
        source = object_dict(sources.get(asset_id))
        if not source:
            raise AV1V4R3InvocationClosureError(
                f"Revision-2 manifest is missing source {asset_id!r}"
            )
        expected = AV1_V4_R3_SOURCE_MANIFEST_FACTS[asset_id]
        observed_video = _manifest_video_stream(source, asset_id)
        actual = {
            "duration_seconds": source.get("duration_seconds"),
            "media_bytes": source.get("media_bytes"),
            "width": observed_video.get("width"),
            "height": observed_video.get("height"),
            "codec": observed_video.get("codec_name"),
            "video_stream_index": source.get("qualification_video_stream_index"),
            "excluded_stream_indexes": _manifest_excluded_stream_indexes(source),
        }
        if actual != _public_source_facts(expected):
            raise AV1V4R3InvocationClosureError(
                f"Revision-3 hardcoded source facts do not match source {asset_id!r}"
            )


def assert_av1_v4_r3_protocol_v4_invariants() -> None:
    """Assert that r3 keeps protocol-v4 identity while completing bindings."""

    if AV1_V4_R3_PROTOCOL_VERSION != AV1_VALIDATION_V4_PROTOCOL_VERSION:
        raise AV1V4R3InvocationClosureError("Revision 3 protocol version changed")
    if AV1_VALIDATION_V4_EXPERIMENT_ID != "av1_cold_start_v4":
        raise AV1V4R3InvocationClosureError("Revision 3 experiment ID changed")
    if AV1_V4_R3_MANIFEST_REVISION != AV1_VALIDATION_V4_MANIFEST_REVISION + 1:
        raise AV1V4R3InvocationClosureError("Revision 3 must supersede revision 2")
    if set(AV1_V4_R3_SOURCE_MANIFEST_FACTS) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1V4R3InvocationClosureError("Revision 3 source set changed")
    if len(build_av1_v4_r3_all_closure_payloads()) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1V4R3InvocationClosureError("Revision 3 traversal count changed")
    for key, value in AV1_V4_R3_REVISION_2_BASE_VIDEO_POLICY.items():
        if AV1_V4_R3_FULL_VIDEO_POLICY.get(key) != value:
            raise AV1V4R3InvocationClosureError(
                f"Revision 3 changed revision-2 policy key {key!r}"
            )
    for asset_id, selection in AV1_VALIDATION_V4_SOURCE_STREAM_SELECTION.items():
        facts = AV1_V4_R3_SOURCE_MANIFEST_FACTS[asset_id]
        if facts["video_stream_index"] != selection["video_stream_index"] or facts[
            "excluded_stream_indexes"
        ] != tuple(selection["excluded_stream_indexes"]):
            raise AV1V4R3InvocationClosureError(
                "Revision 3 stream selection differs from revision 2"
            )
    if AV1_V4_R3_SOURCE_VIDEO_BITRATE_BPS is not None:
        raise AV1V4R3InvocationClosureError("Source video bitrate must remain null")
    revision_2_warm_starts = av1_validation_v4_guided_warm_start_identities()
    revision_3_warm_starts = av1_v4_r3_guided_warm_start_identities()
    for asset_id in AV1_VALIDATION_V4_SOURCE_IDS:
        if (
            revision_2_warm_starts[asset_id]["search_signature_id"]
            == revision_3_warm_starts[asset_id]["search_signature_id"]
            or revision_2_warm_starts[asset_id]["cohort_id"]
            == revision_3_warm_starts[asset_id]["cohort_id"]
        ):
            raise AV1V4R3InvocationClosureError(
                "Revision 3 must not reuse revision-2 warm-start identities"
            )


def _source_facts(asset_id: str) -> Mapping[str, Any]:
    facts = AV1_V4_R3_SOURCE_MANIFEST_FACTS.get(asset_id)
    if facts is None:
        raise AV1V4R3InvocationClosureError(f"Unknown source asset_id: {asset_id!r}")
    return facts


def _public_source_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "duration_seconds": facts["duration_seconds"],
        "media_bytes": facts["media_bytes"],
        "width": facts["width"],
        "height": facts["height"],
        "codec": facts["codec"],
        "video_stream_index": facts["video_stream_index"],
        "excluded_stream_indexes": list(facts["excluded_stream_indexes"]),
    }


def _source_mapping(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if "sources" in manifest:
        return {
            str(source.get("asset_id") or ""): source
            for source in (
                object_dict(value) for value in object_list(manifest.get("sources"))
            )
        }
    return {str(key): object_dict(value) for key, value in manifest.items()}


def _manifest_video_stream(source: Mapping[str, Any], asset_id: str) -> dict[str, Any]:
    video_index = int(source.get("qualification_video_stream_index", -1))
    for stream in (
        object_dict(value) for value in object_list(source.get("observed_streams"))
    ):
        if stream.get("index") == video_index and stream.get("codec_type") == "video":
            return stream
    raise AV1V4R3InvocationClosureError(
        f"Revision-2 manifest source {asset_id!r} is missing its video stream"
    )


def _manifest_excluded_stream_indexes(source: Mapping[str, Any]) -> list[int]:
    constraint = object_dict(source.get("stream_constraint"))
    if constraint:
        return [
            int(value)
            for value in object_list(constraint.get("excluded_stream_indexes"))
        ]
    selected = int(source.get("qualification_video_stream_index", -1))
    return [
        int(stream["index"])
        for stream in (
            object_dict(value) for value in object_list(source.get("observed_streams"))
        )
        if int(stream.get("index", -1)) != selected
    ]


def _validate_quality_temp_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < AV1_V4_R3_QUALITY_TEMP_KEY_BYTES_MIN:
        raise AV1V4R3InvocationClosureError(
            "AV1 v4 r3 quality-temp key must be at least "
            f"{AV1_V4_R3_QUALITY_TEMP_KEY_BYTES_MIN} bytes"
        )


def _hmac_hex(key: bytes, payload: Mapping[str, Any]) -> str:
    return _hmac_module.new(
        key, canonical_json_bytes(payload), hashlib.sha256
    ).hexdigest()


def _stable_prefixed_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}{stable_json_hash(payload)[:32]}"


def _canonical_absolute_posix_path(value: str) -> str:
    if not isinstance(value, str):
        raise AV1V4R3InvocationClosureError("Quality-temp path must be text")
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not value.startswith("/")
        or value == "/"
        or "//" in value
        or value.endswith("/")
        or "/../" in value
        or value.endswith("/..")
        or "/./" in value
        or value.endswith("/.")
    ):
        raise AV1V4R3InvocationClosureError(
            "AV1 v4 r3 quality-temp path must be a canonical absolute POSIX path"
        )
    return value


assert_av1_v4_r3_protocol_v4_invariants()
