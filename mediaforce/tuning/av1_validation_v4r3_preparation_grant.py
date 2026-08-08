"""Pure single-use preparation-grant contract for AV1 protocol-v4 revision 3."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_APPROVED_AT,
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    AV1_V4R3_RIGHTS_REAFFIRMED_AT,
    AV1_V4R3_RIGHTS_STATE,
    assert_av1_v4r3_rights_attestation,
)


AV1_V4R3_PREPARATION_GRANT_SCHEMA = "mediaforce.av1_cold_start_v4r3_preparation_grant"
AV1_V4R3_PREPARATION_GRANT_SCHEMA_VERSION = 1
AV1_V4R3_PREPARATION_GRANT_CONTRACT_VERSION = "av1v4r3pgg1"
AV1_V4R3_PREPARATION_GRANT_STATE = "owner_authorized"
AV1_V4R3_PREPARATION_GRANT_AUTHORITY = (
    "av1_v4r3_path_privacy_key_and_non_media_preparation"
)
AV1_V4R3_PREPARATION_GRANT_MAX_SECONDS = 86_400
AV1_V4R3_PREPARATION_GRANT_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:preparation-grant:identity:v1"
)
AV1_V4R3_PREPARATION_REGISTRY_TOKEN_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:preparation:consumption-registry-token:v1"
)
AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN = (
    "av1v4r3prepregistry_3ce2006c94283bc748b142997b5acc0d"
)

_GRANT_ID_RE = re.compile(r"av1vprepgrant4r3_[0-9a-f]{32}\Z")
_REGISTRY_TOKEN_RE = re.compile(r"av1v4r3prepregistry_[0-9a-f]{32}\Z")
_RIGHTS_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_PAYLOAD_DEPTH = 32
_MAX_PAYLOAD_NODES = 10_000
_OPERATION_SCOPE = {
    "ab_av1_version_probe_argv": ["--version"],
    "binary_hashing_authorized": True,
    "builder_subprocess_authorized": False,
    "config_hashing_authorized": True,
    "ffmpeg_version_probe_argv": ["-version"],
    "ffprobe_version_probe_argv": ["-version"],
    "grant_reuse_authorized": False,
    "invocation_digest_derivation_authorized": True,
    "key_bytes": 32,
    "key_file_create_exclusive_required": True,
    "key_file_mode": "0600",
    "key_material_disclosure_authorized": False,
    "key_persistence": "machine_local_only",
    "media_bytes_read_authorized": False,
    "media_path_probe_authorized": False,
    "media_processing_subprocess_authorized": False,
    "network_access_authorized": False,
    "path_identity_derivation_authorized": True,
    "path_privacy_key_creation_authorized": True,
    "path_privacy_key_selection_or_partition_use_authorized": False,
    "prepared_unfrozen_record_creation_authorized": True,
    "repository_identity_measurement_authorized": True,
    "runtime_compatibility_derivation_authorized": True,
    "single_use_authorized": True,
    "tool_version_probe_subprocess_authorized": True,
    "tool_version_probe_timeout_seconds_max": 10,
    "warm_start_identity_derivation_authorized": True,
}
_ALLOWED_TOP_LEVEL_KEYS = (
    frozenset(
        {
            "authority",
            "authorized_at",
            "consumption_registry_token",
            "contract_version",
            "discovery_public_sha256",
            "experiment_id",
            "grant_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_revision",
            "operation_scope",
            "owner_principal",
            "payload_sha256",
            "protocol_version",
            "repository",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "rights_attested_at",
            "schema",
            "schema_version",
            "state",
            "valid_until",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

if set(_OPERATION_SCOPE) & AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError("AV1 v4 r3 preparation scope collides with frozen authority")


class AV1V4R3PreparationGrantError(ValueError):
    """Raised when a revision-3 preparation grant is invalid."""


def build_av1_v4r3_preparation_grant(
    *,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    """Build one canonical single-use, non-media revision-3 grant."""

    _assert_safe_payload_shape(rights_attestation)
    rights = object_dict(rights_attestation)
    try:
        assert_av1_v4r3_rights_attestation(rights)
    except AV1V4R3RightsError as exc:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant requires an attested r3 rights record"
        ) from exc
    if rights.get("state") != AV1_V4R3_RIGHTS_STATE:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant requires an attested r3 rights record"
        )
    if owner_principal != rights.get("owner_principal"):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant owner must match the rights attestation"
        )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_PREPARATION_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_V4R3_PREPARATION_GRANT_STATE,
        "authority": AV1_V4R3_PREPARATION_GRANT_AUTHORITY,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "owner_principal": owner_principal,
        "repository": {
            "commit": repository_commit,
            "tree": repository_tree,
        },
        "consumption_registry_token": (AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN),
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "operation_scope": _OPERATION_SCOPE,
        **{field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_preparation_grant(bound)
    return bound


def assert_av1_v4r3_preparation_grant(payload: Mapping[str, Any]) -> None:
    """Validate one canonical revision-3 preparation grant mapping."""

    _assert_safe_payload_shape(payload)
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant payload is invalid"
        )
    unknown = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise AV1V4R3PreparationGrantError(
            f"AV1 v4 r3 preparation grant contains unknown fields: {sorted(unknown)}"
        )
    _assert_base_binding(materialized)
    _assert_false_authorities(materialized)
    _assert_rights_binding(materialized)
    _assert_repository(materialized)
    _assert_window(materialized)
    _assert_registry_token(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(materialized)


def assert_av1_v4r3_preparation_grant_active(
    payload: Mapping[str, Any], *, as_of: str
) -> None:
    """Validate a grant and require one timestamp inside its active window."""

    assert_av1_v4r3_preparation_grant(payload)
    observed_at = _parse_timestamp(as_of, "activity timestamp")
    authorized_at = _parse_timestamp(payload.get("authorized_at"), "authorization")
    valid_until = _parse_timestamp(payload.get("valid_until"), "expiration")
    if not authorized_at <= observed_at < valid_until:
        raise AV1V4R3PreparationGrantError("AV1 v4 r3 preparation grant is not active")


def serialize_av1_v4r3_preparation_grant(payload: Mapping[str, Any]) -> bytes:
    """Return canonical JSON bytes for one validated revision-3 grant."""

    assert_av1_v4r3_preparation_grant(payload)
    try:
        materialized = json.loads(canonical_json_bytes(payload))
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant JSON is invalid"
        ) from exc
    assert_av1_v4r3_preparation_grant(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_preparation_grant(data: bytes) -> dict[str, Any]:
    """Load canonical revision-3 grant bytes without filesystem access."""

    if not isinstance(data, bytes):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant input must be bytes"
        )
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant JSON is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant payload must be a JSON object"
        )
    assert_av1_v4r3_preparation_grant(payload)
    try:
        canonical = canonical_json_bytes(payload) + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant JSON is invalid"
        ) from exc
    if data != canonical:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant bytes are not canonical"
        )
    return payload


def av1_v4r3_preparation_grant_id(payload: Mapping[str, Any]) -> str:
    """Return the revision-scoped identity for one grant payload."""

    _assert_safe_payload_shape(payload)
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"grant_id", "payload_sha256"}
    }
    digest = stable_json_hash(
        {
            "domain": AV1_V4R3_PREPARATION_GRANT_IDENTITY_DOMAIN,
            "payload": semantic,
        }
    )
    return f"av1vprepgrant4r3_{digest[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["grant_id"] = av1_v4r3_preparation_grant_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _expected_registry_token() -> str:
    digest = stable_json_hash(
        {
            "domain": AV1_V4R3_PREPARATION_REGISTRY_TOKEN_DOMAIN,
            "manifest_id": AV1_V4R3_MANIFEST_ID,
            "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        }
    )
    return f"av1v4r3prepregistry_{digest[:32]}"


def _assert_base_binding(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema": AV1_V4R3_PREPARATION_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_V4R3_PREPARATION_GRANT_STATE,
        "authority": AV1_V4R3_PREPARATION_GRANT_AUTHORITY,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "operation_scope": _OPERATION_SCOPE,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant binding is invalid"
        )


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1V4R3PreparationGrantError(
                f"AV1 v4 r3 preparation grant cannot authorize {field}"
            )


def _assert_rights_binding(payload: Mapping[str, Any]) -> None:
    if not _RIGHTS_ID_RE.fullmatch(
        str(payload.get("rights_attestation_id") or "")
    ) or not _SHA256_RE.fullmatch(
        str(payload.get("rights_attestation_payload_sha256") or "")
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant rights binding is invalid"
        )
    owner_principal = payload.get("owner_principal")
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 128
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant owner principal is invalid"
        )
    if payload.get("rights_attested_at") != AV1_V4R3_RIGHTS_REAFFIRMED_AT:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant rights timestamp is invalid"
        )


def _assert_repository(payload: Mapping[str, Any]) -> None:
    repository = object_dict(payload.get("repository"))
    if set(repository) != {"commit", "tree"}:
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant repository binding is invalid"
        )
    if any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant repository identity is invalid"
        )


def _assert_window(payload: Mapping[str, Any]) -> None:
    manifest_approved_at = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest approval"
    )
    manifest_valid_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest expiration"
    )
    rights_attested_at = _parse_timestamp(
        payload.get("rights_attested_at"), "rights attestation"
    )
    authorized_at = _parse_timestamp(payload.get("authorized_at"), "authorization")
    valid_until = _parse_timestamp(payload.get("valid_until"), "expiration")
    if not (
        manifest_approved_at
        <= rights_attested_at
        <= authorized_at
        < valid_until
        <= manifest_valid_until
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant timestamps are out of order"
        )
    if valid_until - authorized_at > timedelta(
        seconds=AV1_V4R3_PREPARATION_GRANT_MAX_SECONDS
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant exceeds its maximum duration"
        )


def _assert_registry_token(payload: Mapping[str, Any]) -> None:
    token = str(payload.get("consumption_registry_token") or "")
    if (
        not _REGISTRY_TOKEN_RE.fullmatch(token)
        or token != AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
        or token != _expected_registry_token()
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant consumption registry token is invalid"
        )


def _assert_identity(payload: Mapping[str, Any]) -> None:
    grant_id = str(payload.get("grant_id") or "")
    if not _GRANT_ID_RE.fullmatch(grant_id):
        raise AV1V4R3PreparationGrantError("AV1 v4 r3 preparation grant ID is invalid")
    if grant_id != av1_v4r3_preparation_grant_id(payload):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant ID does not match its payload"
        )
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha256 = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get("payload_sha256") != expected_sha256 or not _SHA256_RE.fullmatch(
        str(payload.get("payload_sha256") or "")
    ):
        raise AV1V4R3PreparationGrantError(
            "AV1 v4 r3 preparation grant payload SHA-256 does not match"
        )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3PreparationGrantError(
            f"AV1 v4 r3 preparation grant {label} timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3PreparationGrantError(
            f"AV1 v4 r3 preparation grant {label} timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AV1V4R3PreparationGrantError(
            f"AV1 v4 r3 preparation grant {label} timestamp must use canonical UTC seconds"
        )
    normalized = parsed.astimezone(UTC)
    if normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3PreparationGrantError(
            f"AV1 v4 r3 preparation grant {label} timestamp must use canonical UTC seconds"
        )
    return normalized


def _assert_no_private_text(value: Any) -> None:
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if av1_validation_v4_contains_private_text(current):
                raise AV1V4R3PreparationGrantError(
                    "AV1 v4 r3 preparation grant contains private path-like text"
                )
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                stack.append(str(key))
                stack.append(nested)
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (bytes, bytearray)
        ):
            stack.extend(current)


def _assert_safe_payload_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > _MAX_PAYLOAD_DEPTH or nodes > _MAX_PAYLOAD_NODES:
            raise AV1V4R3PreparationGrantError(
                "AV1 v4 r3 preparation grant payload structure is too deep or large"
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise AV1V4R3PreparationGrantError(
                "AV1 v4 r3 preparation grant payload contains a non-finite number"
            )
        if isinstance(current, Mapping):
            for key, nested in current.items():
                stack.append((str(key), depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((nested, depth + 1) for nested in current)
