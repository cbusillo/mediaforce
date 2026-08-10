"""Pure non-authorizing contract for AV1 protocol-v4 revision 4."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from types import MappingProxyType
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)


AV1_V4R4_PROTOCOL_VERSION = 4
AV1_V4R4_MANIFEST_REVISION = 4
AV1_V4R4_EXPERIMENT_ID = "av1_cold_start_v4"
AV1_V4R4_CONTRACT_SCHEMA = "mediaforce.av1_cold_start_v4r4_contract"
AV1_V4R4_CONTRACT_SCHEMA_VERSION = 1
AV1_V4R4_CONTRACT_VERSION = "av1v4r4cartography1"
AV1_V4R4_MANIFEST_STATE = "owner_approved_implementation_only"
AV1_V4R4_MANIFEST_APPROVED_AT = "2026-08-10T00:03:00Z"
AV1_V4R4_MANIFEST_VALID_UNTIL = "2027-02-02T18:53:35Z"
AV1_V4R4_MANIFEST_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:4:manifest:identity:v1"
)
AV1_V4R4_POLICY_VALUES_SCHEMA = "mediaforce.av1_cold_start_v4r4_policy_values"
AV1_V4R4_POLICY_VALUES_SCHEMA_VERSION = 1
AV1_V4R4_POLICY_VALUES_CONTRACT_VERSION = "av1v4r4policy1"
AV1_V4R4_IDENTITY_DOMAIN_PREFIX = "mediaforce:av1:protocol:4:revision:4:"
AV1_V4R4_MANIFEST_ID = "av1vmanifest4r4_7d3d62b272d048e7ac4aaa397eace2a0"
AV1_V4R4_MANIFEST_PAYLOAD_SHA256 = (
    "sha256:bb7c1d865a618a1b0ba4ccd5b63895d8c3ecc0c3384e0fe359cf0626cb959b67"
)
AV1_V4R4_POLICY_VALUES_SHA256 = (
    "sha256:99b151e2f234d1c820eb6097213ecf8ce9afc1ceda8ea46635ddeaa8bc6f5037"
)

AV1_V4R4_DISPOSITIONS = (
    "selected_success",
    "bounded_quality_conflict",
    "fatal_failure",
)
AV1_V4R4_ADVANCING_DISPOSITIONS = frozenset(
    {"selected_success", "bounded_quality_conflict"}
)
AV1_V4R4_CONFLICT_REASONS = (
    "all_candidates_violate_quality_floor",
    "target_band_violates_quality_floor",
    "target_requires_crossing_quality_floor",
)
AV1_V4R4_QUALITY_GAP_BANDS = (
    "within_relax_step",
    "within_target_floor_span",
    "within_two_target_floor_spans",
    "beyond_two_target_floor_spans",
)
AV1_V4R4_SIZE_GAP_BANDS = (
    "below_sample_lower_bound",
    "within_final_tolerance_above_target",
    "within_projection_tolerance_above_target",
    "beyond_projection_tolerance_within_source_cap",
    "over_source_cap",
    "no_quality_safe_candidate",
)
AV1_V4R4_LEVER_CLASSES = (
    "measurement_or_quality_corridor_investigation",
    "quality_corridor_investigation",
    "multi_axis_policy_required",
    "search_or_intent_semantics_investigation",
    "source_cap_or_resolution_intent",
    "content_class_target_normalization",
    "projection_or_tolerance_calibration",
)
AV1_V4R4_TERMINATION_KINDS = (
    "traversal_complete",
    "fatal_failure",
    "incomplete_seal",
)
AV1_V4R4_VERDICTS = (
    "policy_satisfied",
    "policy_conflicted",
    "indeterminate",
)
AV1_V4R4_HYPOTHESIS_RESULTS = ("supported", "falsified", "not_applicable")
AV1_V4R4_MAP_RESULTS = ("invalid", "policy_satisfied", *AV1_V4R4_LEVER_CLASSES)
AV1_V4R4_GUIDED_PROBE_STATUSES = ("size_band_miss",)

AV1_V4R4_SOURCE_LAYOUT = (
    MappingProxyType(
        {
            "asset_id": "av1v4_animation_primary_sintel",
            "content_class": "animation_content",
            "role": "primary",
            "media_sha256": (
                "sha256:97f1dbc66231df42ad49bd8c29aa174b8f48933058e47e7157d4ba63d93a8efa"
            ),
            "duration_seconds": 888.032,
            "media_bytes": 1_180_090_590,
            "target_size_bytes": 98_670_222,
            "source_cap_total_bytes": 944_072_472,
        }
    ),
    MappingProxyType(
        {
            "asset_id": "av1v4_live_action_primary_tears_of_steel",
            "content_class": "live_action_content",
            "role": "primary",
            "media_sha256": (
                "sha256:bd2b5bc6c16d4085034f47ef7e4b3938afe86b4eec4ac3cf2685367d3b0b23b0"
            ),
            "duration_seconds": 734.166667,
            "media_bytes": 738_876_331,
            "target_size_bytes": 81_574_074,
            "source_cap_total_bytes": 591_101_064,
        }
    ),
    MappingProxyType(
        {
            "asset_id": "av1v4_animation_confirmation_cosmos_laundromat",
            "content_class": "animation_content",
            "role": "confirmation",
            "media_sha256": (
                "sha256:96ace08a4d61878cfb7cc8b850b7f5aa7ba304de45017eaf832ccd3d52893130"
            ),
            "duration_seconds": 730.069333,
            "media_bytes": 491_245_034,
            "target_size_bytes": 81_118_815,
            "source_cap_total_bytes": 392_996_027,
        }
    ),
    MappingProxyType(
        {
            "asset_id": "av1v4_live_action_confirmation_nasa_earth_views",
            "content_class": "live_action_content",
            "role": "confirmation",
            "media_sha256": (
                "sha256:184b9ebaf6624b219455b0d4a72967e05d3e6cebc034ac8036fcc0198a17a8ef"
            ),
            "duration_seconds": 200.750563,
            "media_bytes": 234_140_076,
            "target_size_bytes": 22_305_618,
            "source_cap_total_bytes": 187_312_060,
        }
    ),
)
AV1_V4R4_CONFIGURATIONS = (
    "balanced_full_search_baseline",
    "balanced_frozen_search_hint",
)
AV1_V4R4_ORDINAL_COUNT = len(AV1_V4R4_SOURCE_LAYOUT) * len(AV1_V4R4_CONFIGURATIONS)
AV1_V4R4_POSITIVE_CONTROLS: Mapping[int, Mapping[str, Any]] = MappingProxyType(
    {
        1: MappingProxyType(
            {
                "disposition": "selected_success",
                "guided_probe_status": None,
                "guided_fallback_selected": None,
                "conflict_reason": None,
            }
        ),
        2: MappingProxyType(
            {
                "disposition": "selected_success",
                "guided_probe_status": "size_band_miss",
                "guided_fallback_selected": True,
                "conflict_reason": None,
            }
        ),
        3: MappingProxyType(
            {
                "disposition": "bounded_quality_conflict",
                "guided_probe_status": None,
                "guided_fallback_selected": None,
                "conflict_reason": "target_band_violates_quality_floor",
            }
        ),
    }
)

AV1_V4R4_POLICY_VALUES: Mapping[str, Any] = MappingProxyType(
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

_MANIFEST_ID_RE = re.compile(r"av1vmanifest4r4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PURPOSE_RE = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class AV1V4R4ContractError(ValueError):
    """Raised when a revision-4 pure contract is invalid."""


def av1_v4r4_identity_domain(purpose: str) -> str:
    if not isinstance(purpose, str) or not _PURPOSE_RE.fullmatch(purpose):
        raise AV1V4R4ContractError("AV1 v4 r4 identity purpose is invalid")
    return f"{AV1_V4R4_IDENTITY_DOMAIN_PREFIX}{purpose}:v1"


def av1_v4r4_policy_values_payload() -> dict[str, Any]:
    return {
        "schema": AV1_V4R4_POLICY_VALUES_SCHEMA,
        "schema_version": AV1_V4R4_POLICY_VALUES_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_POLICY_VALUES_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "values": dict(AV1_V4R4_POLICY_VALUES),
    }


def assert_av1_v4r4_policy_values(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    expected = av1_v4r4_policy_values_payload()
    if materialized != expected:
        raise AV1V4R4ContractError("AV1 v4 r4 policy values are not frozen")
    digest = f"sha256:{stable_json_hash(object_dict(materialized.get('values')))}"
    if digest != AV1_V4R4_POLICY_VALUES_SHA256:
        raise AV1V4R4ContractError("AV1 v4 r4 policy values digest is stale")


def assert_av1_v4r4_runtime_policy(
    *,
    metric_name: Any,
    metric_target: Any,
    minimum_metric_score: Any,
    relax_step: Any,
    sample_projection_tolerance_percent: Any,
    final_output_tolerance_percent: Any,
    source_cap_percent: Any,
) -> None:
    expected = AV1_V4R4_POLICY_VALUES
    bindings = {
        "metric_name": (metric_name, expected["quality_metric"]),
        "metric_target": (metric_target, expected["target_vmaf"]),
        "minimum_metric_score": (
            minimum_metric_score,
            expected["min_target_vmaf"],
        ),
        "relax_step": (relax_step, expected["target_relax_step_vmaf"]),
        "sample_projection_tolerance_percent": (
            sample_projection_tolerance_percent,
            expected["sample_projection_tolerance_percent"],
        ),
        "final_output_tolerance_percent": (
            final_output_tolerance_percent,
            expected["final_output_tolerance_percent"],
        ),
        "source_cap_percent": (source_cap_percent, expected["max_encoded_percent"]),
    }
    for label, (actual, frozen) in bindings.items():
        if type(actual) is not type(frozen) or actual != frozen:
            raise AV1V4R4ContractError(
                f"AV1 v4 r4 runtime {label} does not match frozen policy"
            )


def av1_v4r4_ordinal_layout() -> list[dict[str, Any]]:
    ordinals: list[dict[str, Any]] = []
    ordinal = 1
    for source in AV1_V4R4_SOURCE_LAYOUT:
        asset_id = str(source["asset_id"])
        content_class = str(source["content_class"])
        for configuration in AV1_V4R4_CONFIGURATIONS:
            ordinals.append(
                {
                    "ordinal": ordinal,
                    "asset_id": asset_id,
                    "content_class": content_class,
                    "role": source["role"],
                    "configuration": configuration,
                    "target_size_bytes": source["target_size_bytes"],
                    "source_cap_total_bytes": source["source_cap_total_bytes"],
                    "warm_start": _warm_start_payload(
                        asset_id=asset_id,
                        content_class=content_class,
                        configuration=configuration,
                    ),
                }
            )
            ordinal += 1
    return ordinals


def build_av1_v4r4_contract_payload() -> dict[str, Any]:
    semantic = _contract_semantic_payload()
    manifest_id = av1_v4r4_manifest_id(semantic)
    if manifest_id != AV1_V4R4_MANIFEST_ID:
        raise AV1V4R4ContractError("AV1 v4 r4 manifest ID constant is stale")
    payload = {**semantic, "manifest_id": manifest_id}
    payload["payload_sha256"] = f"sha256:{stable_json_hash(payload)}"
    if payload["payload_sha256"] != AV1_V4R4_MANIFEST_PAYLOAD_SHA256:
        raise AV1V4R4ContractError("AV1 v4 r4 manifest digest constant is stale")
    assert_av1_v4r4_contract_payload(payload)
    return payload


def av1_v4r4_manifest_id(semantic_payload: Mapping[str, Any]) -> str:
    digest = stable_json_hash(
        {
            "domain": AV1_V4R4_MANIFEST_IDENTITY_DOMAIN,
            "payload": dict(semantic_payload),
        }
    )
    return f"av1vmanifest4r4_{digest[:32]}"


def assert_av1_v4r4_contract_payload(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    expected_keys = set(_contract_semantic_payload()) | {
        "manifest_id",
        "payload_sha256",
    }
    if set(materialized) != expected_keys:
        raise AV1V4R4ContractError("AV1 v4 r4 contract shape is invalid")
    _assert_no_private_text(materialized)
    if materialized.get(
        "manifest_id"
    ) != AV1_V4R4_MANIFEST_ID or not _MANIFEST_ID_RE.fullmatch(
        str(materialized.get("manifest_id") or "")
    ):
        raise AV1V4R4ContractError("AV1 v4 r4 manifest ID is invalid")
    if materialized.get(
        "payload_sha256"
    ) != AV1_V4R4_MANIFEST_PAYLOAD_SHA256 or not _SHA256_RE.fullmatch(
        str(materialized.get("payload_sha256") or "")
    ):
        raise AV1V4R4ContractError("AV1 v4 r4 manifest digest is invalid")
    if any(
        materialized.get(field) is not False
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    ):
        raise AV1V4R4ContractError("AV1 v4 r4 contract cannot confer authority")
    semantic = {
        key: value
        for key, value in materialized.items()
        if key not in {"manifest_id", "payload_sha256"}
    }
    if semantic != _contract_semantic_payload():
        raise AV1V4R4ContractError("AV1 v4 r4 contract semantics changed")
    if av1_v4r4_manifest_id(semantic) != materialized["manifest_id"]:
        raise AV1V4R4ContractError("AV1 v4 r4 manifest identity is invalid")
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized["payload_sha256"] != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R4ContractError("AV1 v4 r4 manifest digest does not match")
    assert_av1_v4r4_policy_values(materialized["policy_values"])
    _assert_source_layout(materialized["source_layout"])
    _assert_ordinal_layout(materialized["ordinal_layout"])


def serialize_av1_v4r4_contract_payload(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r4_contract_payload(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r4_contract_payload(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R4ContractError("AV1 v4 r4 contract bytes are unreadable") from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R4ContractError("AV1 v4 r4 contract bytes are not canonical")
    assert_av1_v4r4_contract_payload(payload)
    return payload


def assert_av1_v4r4_repository(repository: Mapping[str, Any]) -> None:
    materialized = object_dict(repository)
    if set(materialized) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(materialized.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R4ContractError("AV1 v4 r4 repository identity is invalid")


def _contract_semantic_payload() -> dict[str, Any]:
    return {
        "schema": AV1_V4R4_CONTRACT_SCHEMA,
        "schema_version": AV1_V4R4_CONTRACT_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "state": AV1_V4R4_MANIFEST_STATE,
        "approved_at": AV1_V4R4_MANIFEST_APPROVED_AT,
        "valid_until": AV1_V4R4_MANIFEST_VALID_UNTIL,
        "governance_issue": "#405",
        "implementation_issue": "#406",
        "policy_values": av1_v4r4_policy_values_payload(),
        "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
        "source_layout": [dict(source) for source in AV1_V4R4_SOURCE_LAYOUT],
        "configuration_order": list(AV1_V4R4_CONFIGURATIONS),
        "ordinal_layout": av1_v4r4_ordinal_layout(),
        "positive_controls": {
            str(ordinal): dict(expectation)
            for ordinal, expectation in AV1_V4R4_POSITIVE_CONTROLS.items()
        },
        "diagnostic_vocabulary": {
            "dispositions": list(AV1_V4R4_DISPOSITIONS),
            "advancing_dispositions": sorted(AV1_V4R4_ADVANCING_DISPOSITIONS),
            "conflict_reasons": list(AV1_V4R4_CONFLICT_REASONS),
            "quality_gap_bands": list(AV1_V4R4_QUALITY_GAP_BANDS),
            "size_gap_bands": list(AV1_V4R4_SIZE_GAP_BANDS),
            "lever_classes": list(AV1_V4R4_LEVER_CLASSES),
            "termination_kinds": list(AV1_V4R4_TERMINATION_KINDS),
            "verdicts": list(AV1_V4R4_VERDICTS),
            "hypothesis_results": list(AV1_V4R4_HYPOTHESIS_RESULTS),
            "map_results": list(AV1_V4R4_MAP_RESULTS),
            "guided_probe_statuses": list(AV1_V4R4_GUIDED_PROBE_STATUSES),
        },
        "lineage_guards": {
            "revision_3_artifact_reuse_allowed": False,
            "revision_3_key_reuse_allowed": False,
            "revision_3_registry_reuse_allowed": False,
            "revision_3_hmac_domain_reuse_allowed": False,
            "revision_3_outcome_conversion_allowed": False,
            "revision_3_retry_or_resume_allowed": False,
            "policy_values_equal_revision_3_required": True,
            "source_set_equal_revision_3_required": True,
            "identity_envelopes_must_differ": True,
        },
        "execution_boundary": {
            "implementation_only": True,
            "preparation_requires_later_owner_gate": True,
            "each_ordinal_requires_fresh_owner_gate": True,
            "policy_successor_requires_later_owner_gate": True,
            "dogfood_requires_later_owner_gate": True,
        },
        **{field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)},
    }


def _warm_start_payload(
    *,
    asset_id: str,
    content_class: str,
    configuration: str,
) -> dict[str, Any] | None:
    if configuration == "balanced_full_search_baseline":
        return None
    return {
        "requested_crf": 28.0,
        "candidate_crf": 28,
        "search_signature_id": "av1v4r4sig_"
        + stable_json_hash(
            {
                "domain": av1_v4r4_identity_domain("warm-start-signature"),
                "asset_id": asset_id,
            }
        )[:32],
        "cohort_id": "av1v4r4cohort_"
        + stable_json_hash(
            {
                "domain": av1_v4r4_identity_domain("warm-start-cohort"),
                "content_class": content_class,
            }
        )[:32],
        "source": "av1_cold_start_v4r4_qualification",
    }


def _assert_source_layout(value: Any) -> None:
    sources = object_list(value)
    if sources != [dict(source) for source in AV1_V4R4_SOURCE_LAYOUT]:
        raise AV1V4R4ContractError("AV1 v4 r4 source layout is invalid")
    if len({str(source["asset_id"]) for source in sources}) != len(sources):
        raise AV1V4R4ContractError("AV1 v4 r4 source IDs must be distinct")


def _assert_ordinal_layout(value: Any) -> None:
    ordinals = object_list(value)
    if ordinals != av1_v4r4_ordinal_layout():
        raise AV1V4R4ContractError("AV1 v4 r4 ordinal layout is invalid")
    if [ordinal["ordinal"] for ordinal in ordinals] != list(
        range(1, AV1_V4R4_ORDINAL_COUNT + 1)
    ):
        raise AV1V4R4ContractError("AV1 v4 r4 ordinals are not contiguous")
    warm_ids = [
        value
        for ordinal in ordinals
        if (warm_start := object_dict(ordinal.get("warm_start")))
        for value in (
            warm_start.get("search_signature_id"),
            warm_start.get("cohort_id"),
        )
    ]
    if any(not isinstance(value, str) or "4r3" in value for value in warm_ids):
        raise AV1V4R4ContractError("AV1 v4 r4 warm-start identity is invalid")


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R4ContractError("AV1 v4 r4 contract contains machine-local text")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_text(child)
