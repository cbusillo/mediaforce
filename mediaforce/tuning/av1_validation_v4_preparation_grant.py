from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_REVISED_AT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_grant"
)
AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PREPARATION_GRANT_CONTRACT_VERSION = "av1v4pgg1"
AV1_VALIDATION_V4_PREPARATION_GRANT_STATE = "owner_authorized"
AV1_VALIDATION_V4_PREPARATION_GRANT_AUTHORITY = (
    "av1_v4_path_privacy_key_and_non_media_preparation"
)
AV1_VALIDATION_V4_PREPARATION_GRANT_MAX_SECONDS = 86_400

_GRANT_ID_RE = re.compile(r"av1vprepgrant4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OPERATION_SCOPE = {
    "ab_av1_version_probe_argv": ["--version"],
    "binary_hashing_authorized": True,
    "builder_subprocess_authorized": False,
    "config_hashing_authorized": True,
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
    "ffmpeg_version_probe_argv": ["-version"],
    "ffprobe_version_probe_argv": ["-version"],
}
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "authority",
    "authorized_at",
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
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

if set(_OPERATION_SCOPE) & AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
    raise RuntimeError("AV1 v4 preparation grant scope collides with frozen authority")


class AV1ValidationV4PreparationGrantError(ValueError):
    pass


def build_av1_validation_v4_preparation_grant(
    *,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    _assert_completed_rights_attestation(rights)
    if owner_principal != rights.get("owner_principal"):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant owner must match the rights attestation"
        )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PREPARATION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_PREPARATION_GRANT_STATE,
        "authority": AV1_VALIDATION_V4_PREPARATION_GRANT_AUTHORITY,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "owner_principal": owner_principal,
        "repository": {
            "commit": repository_commit,
            "tree": repository_tree,
        },
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "operation_scope": _OPERATION_SCOPE,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_preparation_grant(bound)
    return bound


def assert_av1_validation_v4_preparation_grant(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant payload is invalid"
        )
    unknown_fields = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_fields:
        raise AV1ValidationV4PreparationGrantError(
            f"AV1 v4 preparation grant contains unknown fields: {sorted(unknown_fields)}"
        )
    _assert_no_private_paths(materialized)
    _assert_base_binding(materialized)
    _assert_false_authorities(materialized)
    _assert_rights_binding(materialized)
    _assert_repository(materialized)
    _assert_window(materialized)
    _assert_identity(materialized)


def assert_av1_validation_v4_preparation_grant_active(
    payload: Mapping[str, Any],
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v4_preparation_grant(payload)
    checked_at = _parse_canonical_timestamp(as_of, "activity timestamp")
    authorized_at = _parse_canonical_timestamp(
        payload.get("authorized_at"),
        "authorization timestamp",
    )
    valid_until = _parse_canonical_timestamp(
        payload.get("valid_until"),
        "expiration timestamp",
    )
    if checked_at < authorized_at or checked_at >= valid_until:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant is not active"
        )


def load_av1_validation_v4_preparation_grant(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant must contain a JSON object"
        )
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant bytes are not canonical"
        )
    assert_av1_validation_v4_preparation_grant(payload)
    return payload


def serialize_av1_validation_v4_preparation_grant(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_grant(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def av1_validation_v4_preparation_grant_id(
    payload: Mapping[str, Any],
) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"grant_id", "payload_sha256"}
    }
    return f"av1vprepgrant4_{stable_json_hash(semantic)[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["grant_id"] = av1_validation_v4_preparation_grant_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_base_binding(payload: Mapping[str, Any]) -> None:
    if (
        payload.get("schema") != AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA
        or payload.get("schema_version")
        != AV1_VALIDATION_V4_PREPARATION_GRANT_SCHEMA_VERSION
        or payload.get("contract_version")
        != AV1_VALIDATION_V4_PREPARATION_GRANT_CONTRACT_VERSION
        or payload.get("protocol_version") != AV1_VALIDATION_V4_PROTOCOL_VERSION
        or payload.get("manifest_revision") != AV1_VALIDATION_V4_MANIFEST_REVISION
        or payload.get("experiment_id") != AV1_VALIDATION_V4_EXPERIMENT_ID
        or payload.get("state") != AV1_VALIDATION_V4_PREPARATION_GRANT_STATE
        or payload.get("authority") != AV1_VALIDATION_V4_PREPARATION_GRANT_AUTHORITY
        or payload.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or payload.get("manifest_payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or payload.get("discovery_public_sha256")
        != AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256
        or payload.get("operation_scope") != _OPERATION_SCOPE
    ):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant binding is invalid"
        )


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1ValidationV4PreparationGrantError(
                f"AV1 v4 preparation grant cannot authorize {field}"
            )


def _assert_completed_rights_attestation(rights: Mapping[str, Any]) -> None:
    try:
        assert_av1_validation_v4_rights_attestation(rights)
    except AV1ValidationV4RightsError as exc:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant rights attestation is invalid"
        ) from exc
    if rights.get("state") != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant requires an owner-attested rights record"
        )


def _assert_rights_binding(payload: Mapping[str, Any]) -> None:
    if (
        not re.fullmatch(
            r"av1vrights4_[0-9a-f]{32}",
            str(payload.get("rights_attestation_id") or ""),
        )
        or not _SHA256_RE.fullmatch(
            str(payload.get("rights_attestation_payload_sha256") or "")
        )
    ):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant rights binding is invalid"
        )
    owner_principal = payload.get("owner_principal")
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 128
    ):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant owner principal is invalid"
        )


def _assert_repository(payload: Mapping[str, Any]) -> None:
    repository = object_dict(payload.get("repository"))
    if set(repository) != {"commit", "tree"}:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant repository binding is invalid"
        )
    if any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant repository identity is invalid"
        )


def _assert_window(payload: Mapping[str, Any]) -> None:
    active_at = _parse_canonical_timestamp(
        AV1_VALIDATION_V4_REVISED_AT,
        "manifest revision timestamp",
    )
    manifest_valid_until = _parse_canonical_timestamp(
        AV1_VALIDATION_V4_VALID_UNTIL,
        "manifest expiration timestamp",
    )
    rights_attested_at = _parse_canonical_timestamp(
        payload.get("rights_attested_at"),
        "rights attestation timestamp",
    )
    authorized_at = _parse_canonical_timestamp(
        payload.get("authorized_at"),
        "authorization timestamp",
    )
    valid_until = _parse_canonical_timestamp(
        payload.get("valid_until"),
        "expiration timestamp",
    )
    if not active_at <= rights_attested_at <= authorized_at < valid_until:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant timestamps are out of order"
        )
    if valid_until > manifest_valid_until:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant outlives the manifest"
        )
    if valid_until - authorized_at > timedelta(
        seconds=AV1_VALIDATION_V4_PREPARATION_GRANT_MAX_SECONDS
    ):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant exceeds its maximum duration"
        )


def _assert_identity(payload: Mapping[str, Any]) -> None:
    grant_id = str(payload.get("grant_id") or "")
    if not _GRANT_ID_RE.fullmatch(grant_id):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant ID is invalid"
        )
    if grant_id != av1_validation_v4_preparation_grant_id(payload):
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant ID does not match its payload"
        )
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get("payload_sha256") != expected_sha:
        raise AV1ValidationV4PreparationGrantError(
            "AV1 v4 preparation grant payload SHA-256 does not match"
        )


def _assert_no_private_paths(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child_value in value.values():
                visit(child_value)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4PreparationGrantError(
                "AV1 v4 preparation grant exposes a machine-local path"
            )

    visit(payload)


def _parse_canonical_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4PreparationGrantError(
            f"AV1 v4 preparation grant {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4PreparationGrantError(
            f"AV1 v4 preparation grant {label} is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AV1ValidationV4PreparationGrantError(
            f"AV1 v4 preparation grant {label} must be canonical UTC seconds"
        )
    normalized = parsed.astimezone(UTC)
    if normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4PreparationGrantError(
            f"AV1 v4 preparation grant {label} must be canonical UTC seconds"
        )
    return normalized
