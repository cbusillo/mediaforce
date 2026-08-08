"""Canonical public AV1 protocol-v4 manifest revision 3.

Revision 3 is an immutable public overlay on the historical revision-2
manifest. It binds the completed public invocation closure and ordinal-window
budget without creating owner-only artifacts, reading media, or granting any
authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_CONTRACT_VERSION,
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    build_av1_v4_r3_all_closure_payloads,
    av1_v4_r3_public_contract_payload,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
    AV1_V4R3_OW_SETUP_SECONDS,
    AV1_V4R3_OW_TEARDOWN_SECONDS,
    AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX,
    AV1_V4R3_OW_WINDOW_SECONDS_MAX,
)


AV1_V4R3_MANIFEST_SCHEMA = "mediaforce.av1_cold_start_v4r3_manifest"
AV1_V4R3_MANIFEST_SCHEMA_VERSION = 1
AV1_V4R3_MANIFEST_STATE = "draft_unapproved"
AV1_V4R3_MANIFEST_DRAFTED_AT = "2026-08-07T23:46:50Z"
AV1_V4R3_MANIFEST_REVISED_AT = AV1_V4R3_MANIFEST_DRAFTED_AT
AV1_V4R3_MANIFEST_VALID_UNTIL = AV1_VALIDATION_V4_VALID_UNTIL
AV1_V4R3_MANIFEST_ID = "av1vmanifest4r3_85deed0228e73855916a0cc4d565274a"
AV1_V4R3_MANIFEST_PAYLOAD_SHA256 = (
    "sha256:2aade590da2f8eb4158e00c2a3fe1343b6d28fca3f726c5bb0bbd1ef789ea767"
)
AV1_V4R3_MANIFEST_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:manifest:identity:v1"
)
AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:path-privacy:key-id:v1"
)
AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:path-privacy:instance-path:v1"
)
AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:path-privacy:source-path:v1"
)

_MANIFEST_ID_RE = re.compile(r"av1vmanifest4r3_[0-9a-f]{32}\Z")
_PAYLOAD_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1V4R3ManifestError(ValueError):
    """Raised when the canonical revision-3 manifest is invalid."""


def build_av1_v4r3_manifest_payload() -> dict[str, Any]:
    """Build the canonical public revision-3 manifest payload."""

    semantic = _manifest_semantic_payload()
    manifest_id = av1_v4r3_manifest_id(semantic)
    if manifest_id != AV1_V4R3_MANIFEST_ID:
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest ID constant is stale")
    payload = {**semantic, "manifest_id": manifest_id}
    payload["payload_sha256"] = f"sha256:{stable_json_hash(payload)}"
    if payload["payload_sha256"] != AV1_V4R3_MANIFEST_PAYLOAD_SHA256:
        raise AV1V4R3ManifestError(
            "AV1 v4 r3 manifest payload digest constant is stale"
        )
    assert_av1_v4r3_manifest(payload)
    return payload


def av1_v4r3_manifest_id(semantic_payload: Mapping[str, Any]) -> str:
    """Return the revision-scoped identity for one semantic payload."""

    digest = stable_json_hash(
        {
            "domain": AV1_V4R3_MANIFEST_IDENTITY_DOMAIN,
            "payload": dict(semantic_payload),
        }
    )
    return f"av1vmanifest4r3_{digest[:32]}"


def assert_av1_v4r3_manifest(payload: Mapping[str, Any]) -> None:
    """Validate one canonical revision-3 manifest mapping."""

    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest payload is invalid")
    _assert_no_private_text(materialized)
    manifest_id = str(materialized.get("manifest_id") or "")
    payload_sha256 = str(materialized.get("payload_sha256") or "")
    if not _MANIFEST_ID_RE.fullmatch(manifest_id):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest ID is invalid")
    if not _PAYLOAD_SHA256_RE.fullmatch(payload_sha256):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest payload digest is invalid")
    if manifest_id != AV1_V4R3_MANIFEST_ID:
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest ID is not frozen")
    if payload_sha256 != AV1_V4R3_MANIFEST_PAYLOAD_SHA256:
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest payload digest is not frozen")
    semantic = {
        key: value
        for key, value in materialized.items()
        if key not in {"manifest_id", "payload_sha256"}
    }
    expected_semantic = _manifest_semantic_payload()
    if canonical_json_bytes(semantic) != canonical_json_bytes(expected_semantic):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest semantic payload is not frozen")
    if manifest_id != av1_v4r3_manifest_id(semantic):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest ID does not match its payload")
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if payload_sha256 != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest payload SHA-256 does not match")


def serialize_av1_v4r3_manifest(payload: Mapping[str, Any]) -> bytes:
    """Return canonical JSON bytes for a validated revision-3 manifest."""

    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_manifest(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_manifest(data: bytes) -> dict[str, Any]:
    """Load canonical revision-3 manifest bytes without filesystem access."""

    if not isinstance(data, bytes):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest input must be bytes")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest must be a JSON object")
    if data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3ManifestError("AV1 v4 r3 manifest bytes are not canonical")
    assert_av1_v4r3_manifest(payload)
    return payload


def _manifest_semantic_payload() -> dict[str, Any]:
    closures = list(build_av1_v4_r3_all_closure_payloads())
    return {
        "schema": AV1_V4R3_MANIFEST_SCHEMA,
        "schema_version": AV1_V4R3_MANIFEST_SCHEMA_VERSION,
        "contract_version": AV1_V4_R3_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_V4R3_MANIFEST_STATE,
        "execution_ready": False,
        "drafted_at": AV1_V4R3_MANIFEST_DRAFTED_AT,
        "revised_at": AV1_V4R3_MANIFEST_REVISED_AT,
        "valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "governance_issue": "#334",
        "implementation_issue": "#360",
        "workstream_issue": "#355",
        "parent_issue": "#273",
        "supersedes_manifest_revision": {
            "revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
            "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
            "payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        },
        "revision_2_base_binding": {
            "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
            "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
            "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
            "payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
            "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
            "source_order": list(AV1_VALIDATION_V4_SOURCE_IDS),
            "configuration_order": list(AV1_VALIDATION_V4_CONFIGURATIONS),
            "reuse_as_revision_3_authority_allowed": False,
        },
        "revision_3_path_privacy": {
            "algorithm": "hmac_sha256_v1",
            "key_bytes_min": 32,
            "single_key_with_domain_separation": True,
            "normalized_path_format": "absolute_posix_canonical",
            "key_id_domain": AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
            "key_id_prefix": "av1vpathkey4r3_",
            "instance_path_domain": AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
            "instance_path_id_prefix": "av1vpath4r3_",
            "source_path_domain": AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
            "source_path_id_prefix": "av1vsource4r3_",
            "selection_or_partition_use_allowed": False,
        },
        "revision_3_invocation_contract": av1_v4_r3_public_contract_payload(),
        "closure_ids": [str(closure["closure_id"]) for closure in closures],
        "ordinal_window_policy": {
            "ordinal_count": AV1_V4R3_OW_ORDINAL_COUNT,
            "setup_seconds": AV1_V4R3_OW_SETUP_SECONDS,
            "traversal_seconds_max": AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX,
            "teardown_seconds": AV1_V4R3_OW_TEARDOWN_SECONDS,
            "window_seconds_max": AV1_V4R3_OW_WINDOW_SECONDS_MAX,
            "aggregate_seconds_max": AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
            "public_run_seconds_max": AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
            "strict_increasing_consumption_required": True,
            "retry_allowed": False,
            "resume_allowed": False,
            "substitution_allowed": False,
            "reordering_allowed": False,
            "backfill_allowed": False,
            "lapse_or_failure_terminal": True,
            "owner_registry_binding_required_later": True,
        },
        "fresh_chain_requirements": {
            "distinct_revision_3_registry_required": True,
            "revision_2_artifact_reuse_allowed": False,
            "required_artifact_order": [
                "preparation_grant",
                "path_privacy_key",
                "preparation_bundle",
                "preparation_freeze",
                "qualification_request",
                "ordinal_window_plan",
                "execution_preflight",
            ],
            "media_bytes_must_not_be_read": True,
            "execution_preflight_must_remain_non_authorizing": True,
        },
        **{field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)},
    }


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R3ManifestError(
                "AV1 v4 r3 manifest contains private path-like text"
            )
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for nested in value:
            _assert_no_private_text(nested)
