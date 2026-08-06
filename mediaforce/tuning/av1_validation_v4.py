from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list


AV1_VALIDATION_V4_MANIFEST_SCHEMA = "mediaforce.av1_cold_start_v4_manifest"
AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SCHEMA = (
    "mediaforce.av1_cold_start_v4_discovery_public"
)
AV1_VALIDATION_V4_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PROTOCOL_VERSION = 4
AV1_VALIDATION_V4_EXPERIMENT_ID = "av1_cold_start_v4"
AV1_VALIDATION_V4_CONTRACT_VERSION = "acsv4"
AV1_VALIDATION_V4_MANIFEST_ID = "av1vmanifest4_63f7d31daaf390fae657a42123a88f15"
AV1_VALIDATION_V4_PAYLOAD_SHA256 = (
    "sha256:91b1c85c0c51544e1e7a2352d68b1025a838b9f5f3dad0deb5c3b9175e9ed589"
)
AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256 = (
    "sha256:cf4c771992bed9eaddc94ef415ebfe16e9d5582016831b7412abf28503b6c697"
)
AV1_VALIDATION_V4_DRAFTED_AT = "2026-08-06T18:53:35Z"
AV1_VALIDATION_V4_VALID_UNTIL = "2027-02-02T18:53:35Z"
AV1_VALIDATION_V4_SOURCE_IDS = (
    "av1v4_animation_primary_sintel",
    "av1v4_animation_confirmation_cosmos_laundromat",
    "av1v4_live_action_primary_tears_of_steel",
    "av1v4_live_action_confirmation_nasa_earth_views",
)
AV1_VALIDATION_V4_CONFIGURATIONS = (
    "balanced_full_search_baseline",
    "balanced_frozen_search_hint",
)
AV1_VALIDATION_V4_STAGES = (
    "ingest",
    "fingerprint",
    "intent",
    "decode",
    "quality_search",
    "encode",
    "validation",
    "review",
    "cleanup",
)
AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS = frozenset({
    "activation_authorized",
    "derivation_authorized",
    "dogfood_authorized",
    "empirical_authority_conferred",
    "evidence_creation_authorized",
    "evidence_eligible",
    "holdout_authorized",
    "key_creation_authorized",
    "manifest_freeze_authorized",
    "media_ingest_authorized",
    "media_publication_authorized",
    "media_read_authorized",
    "partition_construction_authorized",
    "private_inventory_read_authorized",
    "public_bundle_activation_allowed",
    "public_traversal_authorized",
    "publication_authorized",
    "qualification_execution_authorized",
    "retry_authorized",
    "runtime_execution_authorized",
})

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANIFEST_ID_RE = re.compile(r"av1vmanifest4_[0-9a-f]{32}\Z")
_FORBIDDEN_PRIVATE_KEYS = frozenset({
    "ffprobe_path",
    "media_root",
    "source_path",
    "workspace",
})
_FORBIDDEN_PATH_PREFIXES = ("/Users/", "/Volumes/", "/opt/homebrew/")


class AV1ValidationV4Error(ValueError):
    pass


def load_av1_validation_manifest_v4(
    path: Path,
    *,
    discovery_public_path: Path | None = None,
) -> dict[str, Any]:
    payload = _load_canonical_payload(path, "AV1 v4 manifest")
    assert_av1_validation_manifest_v4(payload)
    if discovery_public_path is not None:
        discovery = load_av1_validation_v4_discovery_public(discovery_public_path)
        digest = _sha256_bytes(discovery_public_path.read_bytes())
        if digest != payload["discovery_public_sha256"]:
            raise AV1ValidationV4Error(
                "AV1 v4 discovery projection does not match the manifest"
            )
        if discovery["sources"] != payload["sources"]:
            raise AV1ValidationV4Error(
                "AV1 v4 discovery sources do not match the manifest"
            )
    return payload


def load_av1_validation_v4_discovery_public(path: Path) -> dict[str, Any]:
    payload = _load_canonical_payload(path, "AV1 v4 discovery projection")
    _assert_discovery_public(payload)
    return payload


def serialize_av1_validation_manifest_v4(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_manifest_v4(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def assert_av1_validation_manifest_v4(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4Error("AV1 v4 manifest payload is invalid")
    _assert_identity(materialized)
    _assert_no_private_paths(materialized)
    _assert_false_authorities(materialized)
    _assert_sources(materialized)
    _assert_matrix(materialized)
    _assert_invocation(materialized)
    _assert_rights(materialized)
    _assert_resource_limits(materialized)
    _assert_preparation_boundary(materialized)


def assert_av1_validation_manifest_v4_current(
    payload: Mapping[str, Any],
    *,
    as_of: datetime | None = None,
) -> None:
    assert_av1_validation_manifest_v4(payload)
    current = as_of or datetime.now(UTC)
    if current.tzinfo is None:
        raise AV1ValidationV4Error("AV1 v4 activity checks require timezone-aware time")
    drafted_at = _parse_timestamp(str(payload["drafted_at"]))
    valid_until = _parse_timestamp(str(payload["valid_until"]))
    if current < drafted_at or current >= valid_until:
        raise AV1ValidationV4Error("AV1 v4 draft is not active at the requested time")


def av1_validation_v4_manifest_id(semantic_payload: Mapping[str, Any]) -> str:
    digest = stable_json_hash({
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "kind": "manifest",
        "payload": dict(semantic_payload),
    })
    return f"av1vmanifest4_{digest[:32]}"


def _load_canonical_payload(path: Path, label: str) -> dict[str, Any]:
    if not isinstance(path, Path):
        raise AV1ValidationV4Error(f"{label} path must be a pathlib.Path")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4Error(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise AV1ValidationV4Error(f"{label} must contain a JSON object")
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4Error(f"{label} bytes are not canonical")
    return payload


def _assert_identity(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema": AV1_VALIDATION_V4_MANIFEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "contract_version": AV1_VALIDATION_V4_CONTRACT_VERSION,
        "state": "draft_unapproved",
        "drafted_at": AV1_VALIDATION_V4_DRAFTED_AT,
        "valid_until": AV1_VALIDATION_V4_VALID_UNTIL,
        "governance_issue": "#334",
        "implementation_issue": "#334",
        "parent_issue": "#273",
        "qualification_authority": "av1_v4_qualification",
        "artifact_namespace": "av1_v4_qualification",
        "supersedes_protocol_id": "av1vprotocol3_ba85a44eef70b857d678b236bb1b4afc",
        "supersedes_payload_sha256": "sha256:d17606e4920846de810ab467d63a194f6a9b9138f6d8416a3ff3e0416c37a590",
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise AV1ValidationV4Error(f"AV1 v4 manifest {key} is not frozen")
    drafted_at = _parse_timestamp(str(payload["drafted_at"]))
    valid_until = _parse_timestamp(str(payload["valid_until"]))
    if valid_until <= drafted_at:
        raise AV1ValidationV4Error("AV1 v4 validity interval is invalid")
    manifest_id = str(payload.get("manifest_id") or "")
    if not _MANIFEST_ID_RE.fullmatch(manifest_id):
        raise AV1ValidationV4Error("AV1 v4 manifest ID is invalid")
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"manifest_id", "payload_sha256"}
    }
    if manifest_id != av1_validation_v4_manifest_id(semantic):
        raise AV1ValidationV4Error("AV1 v4 manifest ID does not match its payload")
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get("payload_sha256") != expected_sha:
        raise AV1ValidationV4Error("AV1 v4 payload SHA-256 does not match")
    for key in (
        "authorization_sha256",
        "discovery_public_sha256",
        "discovery_result_sha256",
        "supersedes_payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(key) or "")):
            raise AV1ValidationV4Error(f"AV1 v4 {key} is invalid")


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1ValidationV4Error(
                f"AV1 v4 draft cannot authorize {field}"
            )


def _assert_sources(payload: Mapping[str, Any]) -> None:
    sources = object_list(payload.get("sources"))
    if len(sources) != 4:
        raise AV1ValidationV4Error("AV1 v4 requires exactly four sources")
    source_order = tuple(str(value) for value in object_list(payload.get("source_order")))
    if source_order != AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1ValidationV4Error("AV1 v4 source order is not frozen")
    materialized = [object_dict(source) for source in sources]
    if any(not source for source in materialized):
        raise AV1ValidationV4Error("AV1 v4 source payload is invalid")
    if tuple(str(source["asset_id"]) for source in materialized) != source_order:
        raise AV1ValidationV4Error("AV1 v4 source array order does not match")
    expected_pairs = {
        ("animation_content", "primary"),
        ("animation_content", "confirmation"),
        ("live_action_content", "primary"),
        ("live_action_content", "confirmation"),
    }
    actual_pairs = {(source.get("class"), source.get("role")) for source in materialized}
    if actual_pairs != expected_pairs:
        raise AV1ValidationV4Error("AV1 v4 class/role coverage is incomplete")
    for order, source in enumerate(materialized):
        if source.get("order") != order:
            raise AV1ValidationV4Error("AV1 v4 source ordinal is invalid")
        if source.get("qualification_video_stream_index") != 0:
            raise AV1ValidationV4Error("AV1 v4 qualification must use video stream zero")
        if not str(source.get("fetch_url") or "").startswith("https://"):
            raise AV1ValidationV4Error("AV1 v4 fetch URL must use HTTPS")
        for field in ("media_sha256", "probe_sha256"):
            if not _SHA256_RE.fullmatch(str(source.get(field) or "")):
                raise AV1ValidationV4Error(f"AV1 v4 source {field} is invalid")
        if not isinstance(source.get("media_bytes"), int) or source["media_bytes"] <= 0:
            raise AV1ValidationV4Error("AV1 v4 source byte length is invalid")
        streams = [object_dict(stream) for stream in object_list(source.get("observed_streams"))]
        if not streams or not streams[0]:
            raise AV1ValidationV4Error("AV1 v4 source stream inventory is absent")
        video = streams[0]
        if (
            video.get("index") != 0
            or video.get("codec_type") != "video"
            or video.get("codec_name") != "h264"
            or video.get("pix_fmt") != "yuv420p"
            or video.get("bits_per_raw_sample") != "8"
        ):
            raise AV1ValidationV4Error("AV1 v4 source video stream is incompatible")
    _assert_source_special_cases(materialized)


def _assert_source_special_cases(sources: Sequence[Mapping[str, Any]]) -> None:
    by_id = {str(source["asset_id"]): source for source in sources}
    sintel = by_id[AV1_VALIDATION_V4_SOURCE_IDS[0]]
    md5 = object_dict(sintel.get("publisher_declared_md5"))
    if (
        md5.get("expected") != "89b52d27a509629dbbdf22bb69a8250f"
        or md5.get("computed") != md5.get("expected")
        or md5.get("status") != "match"
    ):
        raise AV1ValidationV4Error("AV1 v4 Sintel publisher checksum is invalid")
    tears = by_id[AV1_VALIDATION_V4_SOURCE_IDS[2]]
    archive = object_dict(tears.get("outer_archive"))
    if (
        archive.get("entry_count") != 1
        or archive.get("entry_name") != "ToS-4k-1920.mov"
        or archive.get("safe_path") is not True
        or archive.get("crc_test") != "pass"
        or not _SHA256_RE.fullmatch(str(archive.get("sha256") or ""))
    ):
        raise AV1ValidationV4Error("AV1 v4 Tears archive binding is invalid")
    nasa = by_id[AV1_VALIDATION_V4_SOURCE_IDS[3]]
    constraint = object_dict(nasa.get("stream_constraint"))
    if (
        constraint.get("allowed_stream_indexes") != [0]
        or constraint.get("excluded_stream_indexes") != [1]
        or constraint.get("excluded_stream_types") != ["audio"]
    ):
        raise AV1ValidationV4Error("AV1 v4 NASA audio exclusion is not frozen")
    streams = [object_dict(stream) for stream in object_list(nasa.get("observed_streams"))]
    if len(streams) != 2 or streams[1].get("codec_type") != "audio":
        raise AV1ValidationV4Error("AV1 v4 NASA stream inventory is invalid")


def _assert_matrix(payload: Mapping[str, Any]) -> None:
    matrix = object_dict(payload.get("qualification_matrix"))
    if (
        matrix.get("classes") != ["animation_content", "live_action_content"]
        or matrix.get("roles") != ["primary", "confirmation"]
        or tuple(matrix.get("configurations") or ()) != AV1_VALIDATION_V4_CONFIGURATIONS
        or tuple(matrix.get("stages") or ()) != AV1_VALIDATION_V4_STAGES
        or matrix.get("traversal_count") != 8
    ):
        raise AV1ValidationV4Error("AV1 v4 qualification matrix is invalid")
    for flag in (
        "all_traversals_required",
        "primary_before_confirmation",
    ):
        if matrix.get(flag) is not True:
            raise AV1ValidationV4Error(f"AV1 v4 matrix {flag} must be true")
    for flag in (
        "adaptation_after_first_outcome_allowed",
        "substitution_allowed",
        "favorable_subset_allowed",
    ):
        if matrix.get(flag) is not False:
            raise AV1ValidationV4Error(f"AV1 v4 matrix {flag} must be false")
    expected_assets = (
        AV1_VALIDATION_V4_SOURCE_IDS[0],
        AV1_VALIDATION_V4_SOURCE_IDS[2],
        AV1_VALIDATION_V4_SOURCE_IDS[1],
        AV1_VALIDATION_V4_SOURCE_IDS[3],
    )
    expected = [
        (ordinal, asset_id, configuration)
        for ordinal, (asset_id, configuration) in enumerate(
            (
                (asset_id, configuration)
                for asset_id in expected_assets
                for configuration in AV1_VALIDATION_V4_CONFIGURATIONS
            ),
            start=1,
        )
    ]
    actual = [
        (
            item.get("ordinal"),
            item.get("asset_id"),
            item.get("configuration"),
        )
        for item in (object_dict(value) for value in object_list(matrix.get("traversals")))
    ]
    if actual != expected:
        raise AV1ValidationV4Error("AV1 v4 traversal order is not frozen")


def _assert_invocation(payload: Mapping[str, Any]) -> None:
    invocation = object_dict(payload.get("qualification_invocation"))
    policy = object_dict(invocation.get("video_policy"))
    if invocation.get("search_schema") != (
        "mediaforce.av1_cold_start_v4_qualification_search_invocation"
    ) or invocation.get("search_contract_version") != "av1vq4s1":
        raise AV1ValidationV4Error("AV1 v4 search contract binding is invalid")
    if invocation.get("qualification_source") != "av1_cold_start_v4_qualification":
        raise AV1ValidationV4Error("AV1 v4 qualification source is invalid")
    if invocation.get("planner_bypassed") is not True:
        raise AV1ValidationV4Error("AV1 v4 planner bypass must be frozen")
    if invocation.get("stream_budget_target_size_path_required") is not True:
        raise AV1ValidationV4Error("AV1 v4 target-size route must be required")
    if policy != {
        "compression_intent_schema_version": 1,
        "compression_intent": "balanced",
        "compression_intent_source": "operator",
        "compression_intent_confirmed": True,
        "min_crf": 10,
        "max_crf": 45,
    }:
        raise AV1ValidationV4Error("AV1 v4 balanced policy is not frozen")
    baseline = object_dict(invocation.get("baseline"))
    guided = object_dict(invocation.get("guided"))
    if baseline != {"warm_start": None, "expected_search_signature_id": None}:
        raise AV1ValidationV4Error("AV1 v4 baseline identity is invalid")
    if (
        guided.get("source") != "av1_cold_start_v4_qualification"
        or guided.get("warm_start_required") is not True
        or guided.get("expected_search_signature_matches_warm_start") is not True
    ):
        raise AV1ValidationV4Error("AV1 v4 guided identity is invalid")
    for field in (
        "concrete_invocation_digests_preparation_required",
        "invocation_digests_must_differ",
        "base_config_digest_must_match",
    ):
        if invocation.get(field) is not True:
            raise AV1ValidationV4Error(f"AV1 v4 invocation {field} must be true")


def _assert_rights(payload: Mapping[str, Any]) -> None:
    rights = object_dict(payload.get("rights_constraints"))
    if (
        rights.get("no_media_redistribution") is not True
        or rights.get("attribution_required") is not True
        or rights.get("nasa_video_stream_only") is not True
        or rights.get("nasa_allowed_stream_indexes") != [0]
        or rights.get("nasa_excluded_stream_indexes") != [1]
        or rights.get("cosmos_license_precedence")
        != "official_blender_gooseberry_title_grant"
    ):
        raise AV1ValidationV4Error("AV1 v4 rights constraints are invalid")


def _assert_resource_limits(payload: Mapping[str, Any]) -> None:
    limits = object_dict(payload.get("resource_limits"))
    expected = {
        "transferred_object_body_bytes_per_source_max": 1_250_000_000,
        "extracted_media_bytes_per_source_max": 1_500_000_000,
        "successful_body_bytes_total_max": 3_000_000_000,
        "cumulative_network_body_bytes_max": 4_250_000_000,
        "working_free_bytes_min": 35_000_000_000,
        "working_reserved_cleanup_bytes": 10_000_000_000,
        "source_fetch_seconds_max": 14_400,
        "discovery_total_seconds_max": 43_200,
        "traversal_seconds_max": 14_400,
        "public_run_seconds_max": 129_600,
    }
    if limits != expected:
        raise AV1ValidationV4Error("AV1 v4 resource limits are not frozen")


def _assert_preparation_boundary(payload: Mapping[str, Any]) -> None:
    requirements = object_dict(payload.get("preparation_requirements"))
    if requirements.get("required_before_owner_freeze") is not True:
        raise AV1ValidationV4Error("AV1 v4 preparation must precede owner freeze")
    if requirements.get("media_bytes_must_not_be_read") is not True:
        raise AV1ValidationV4Error("AV1 v4 preparation must not read media bytes")
    if requirements.get("subprocess_execution_allowed") is not False:
        raise AV1ValidationV4Error("AV1 v4 preparation cannot execute subprocesses")
    for field in (
        "repository_commit_and_tree",
        "effective_config_sha256",
        "ffmpeg_sha256_and_version",
        "ffprobe_sha256_and_version",
        "ab_av1_sha256_and_version",
        "dedicated_instance_path_hmac_ids",
        "source_path_hmac_ids",
        "runtime_compatibility_id",
        "guided_warm_start_identity",
        "baseline_and_guided_invocation_sha256",
        "qualification_key_id",
    ):
        if requirements.get(field) is not True:
            raise AV1ValidationV4Error(
                f"AV1 v4 preparation requirement {field} is absent"
            )


def _assert_discovery_public(payload: Mapping[str, Any]) -> None:
    if (
        payload.get("schema") != AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SCHEMA
        or payload.get("schema_version") != AV1_VALIDATION_V4_SCHEMA_VERSION
        or payload.get("protocol_version") != AV1_VALIDATION_V4_PROTOCOL_VERSION
        or payload.get("experiment_id") != AV1_VALIDATION_V4_EXPERIMENT_ID
        or payload.get("status") != "pass"
        or payload.get("source_count") != 4
    ):
        raise AV1ValidationV4Error("AV1 v4 discovery projection identity is invalid")
    _assert_no_private_paths(payload)
    _assert_false_authorities(payload)
    sources = object_list(payload.get("sources"))
    if tuple(str(object_dict(source).get("asset_id")) for source in sources) != (
        AV1_VALIDATION_V4_SOURCE_IDS
    ):
        raise AV1ValidationV4Error("AV1 v4 discovery source order is invalid")


def _assert_no_private_paths(payload: Mapping[str, Any]) -> None:
    def visit(value: Any, key: str | None = None) -> None:
        if key in _FORBIDDEN_PRIVATE_KEYS:
            raise AV1ValidationV4Error(f"AV1 v4 payload exposes private key {key}")
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.startswith(_FORBIDDEN_PATH_PREFIXES):
            raise AV1ValidationV4Error("AV1 v4 payload exposes a machine-local path")

    visit(payload)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4Error("AV1 v4 timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV4Error("AV1 v4 timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
