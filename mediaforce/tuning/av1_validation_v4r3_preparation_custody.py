"""Pure revision-3 preparation registry, claim, and key-custody contracts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    AV1V4R3PreparationGrantError,
    AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN,
    assert_av1_v4r3_preparation_grant_active,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    AV1_V4R3_RIGHTS_STATE,
    assert_av1_v4r3_rights_attestation,
)


AV1_V4R3_PREPARATION_REGISTRY_BINDING_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_preparation_registry_binding"
)
AV1_V4R3_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION = "av1v4r3pregbind1"
AV1_V4R3_PREPARATION_CLAIM_SCHEMA = "mediaforce.av1_cold_start_v4r3_preparation_claim"
AV1_V4R3_PREPARATION_CLAIM_CONTRACT_VERSION = "av1v4r3prepclaim1"
AV1_V4R3_PREPARATION_CLAIM_STATE = "grant_consumed"
AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_path_privacy_key_custody"
)
AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION = "av1v4r3keycustody1"
AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_STATE = "key_created"
AV1_V4R3_PREPARATION_REGISTRY_BINDING_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:preparation:registry-binding:identity:v1"
)
AV1_V4R3_PREPARATION_CLAIM_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:preparation:claim:identity:v1"
)
AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:preparation:key-custody:identity:v1"
)

_REGISTRY_BINDING_ID_RE = re.compile(r"av1v4r3pregbind_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1v4r3prepclaim_[0-9a-f]{32}\Z")
_CUSTODY_ID_RE = re.compile(r"av1v4r3keycustody_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vprepgrant4r3_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_RIGHTS_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_PAYLOAD_DEPTH = 32
_MAX_PAYLOAD_NODES = 10_000
_BASE_KEYS = (
    frozenset(
        {
            "contract_version",
            "discovery_public_sha256",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_revision",
            "payload_sha256",
            "protocol_version",
            "schema",
            "schema_version",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)
_REGISTRY_BINDING_KEYS = _BASE_KEYS | {
    "consumption_registry_token",
    "registry_binding_id",
}
_CLAIM_KEYS = _BASE_KEYS | {
    "claim_id",
    "claimed_at",
    "consumption_registry_token",
    "owner_principal",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "state",
}
_CUSTODY_KEYS = _BASE_KEYS | {
    "claim_id",
    "claim_payload_sha256",
    "consumption_registry_token",
    "created_at",
    "custody_id",
    "key_bytes_length",
    "key_file_create_exclusive",
    "key_file_mode",
    "key_id",
    "key_material_serialized",
    "owner_principal",
    "claimed_at",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "repository",
    "state",
}


class AV1V4R3PreparationCustodyError(ValueError):
    """Raised when a revision-3 preparation custody artifact is invalid."""


def build_av1_v4r3_preparation_registry_binding() -> dict[str, Any]:
    """Build the path-free marker for one revision-3 private registry."""

    payload = {
        **_base_payload(
            schema=AV1_V4R3_PREPARATION_REGISTRY_BINDING_SCHEMA,
            contract_version=AV1_V4R3_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION,
        ),
        "consumption_registry_token": (AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN),
    }
    bound = _bind_identity(
        payload,
        id_field="registry_binding_id",
        id_prefix="av1v4r3pregbind_",
        domain=AV1_V4R3_PREPARATION_REGISTRY_BINDING_IDENTITY_DOMAIN,
    )
    assert_av1_v4r3_preparation_registry_binding(bound)
    return bound


def assert_av1_v4r3_preparation_registry_binding(
    payload: Mapping[str, Any],
) -> None:
    """Validate one canonical path-free private-registry marker."""

    materialized = _materialize(payload, _REGISTRY_BINDING_KEYS, "registry binding")
    _assert_base(
        materialized,
        schema=AV1_V4R3_PREPARATION_REGISTRY_BINDING_SCHEMA,
        contract_version=AV1_V4R3_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION,
    )
    if (
        materialized.get("consumption_registry_token")
        != AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation registry token is invalid"
        )
    _assert_identity(
        materialized,
        id_field="registry_binding_id",
        id_pattern=_REGISTRY_BINDING_ID_RE,
        id_prefix="av1v4r3pregbind_",
        domain=AV1_V4R3_PREPARATION_REGISTRY_BINDING_IDENTITY_DOMAIN,
        label="registry binding",
    )


def build_av1_v4r3_preparation_claim(
    *,
    preparation_grant: Mapping[str, Any],
    rights_attestation: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    """Build one canonical claim that permanently consumes an active grant."""

    grant = object_dict(preparation_grant)
    rights = object_dict(rights_attestation)
    try:
        assert_av1_v4r3_preparation_grant_active(grant, as_of=claimed_at)
        assert_av1_v4r3_rights_attestation(rights)
    except (AV1V4R3PreparationGrantError, AV1V4R3RightsError) as exc:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation claim inputs are invalid"
        ) from exc
    if rights.get("state") != AV1_V4R3_RIGHTS_STATE:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation claim requires attested r3 rights"
        )
    if (
        grant.get("rights_attestation_id") != rights.get("attestation_id")
        or grant.get("rights_attestation_payload_sha256")
        != rights.get("payload_sha256")
        or grant.get("owner_principal") != rights.get("owner_principal")
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation claim rights binding does not match"
        )
    payload = {
        **_base_payload(
            schema=AV1_V4R3_PREPARATION_CLAIM_SCHEMA,
            contract_version=AV1_V4R3_PREPARATION_CLAIM_CONTRACT_VERSION,
        ),
        "state": AV1_V4R3_PREPARATION_CLAIM_STATE,
        "consumption_registry_token": grant["consumption_registry_token"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "owner_principal": rights["owner_principal"],
        "repository": grant["repository"],
        "claimed_at": claimed_at,
    }
    bound = _bind_identity(
        payload,
        id_field="claim_id",
        id_prefix="av1v4r3prepclaim_",
        domain=AV1_V4R3_PREPARATION_CLAIM_IDENTITY_DOMAIN,
    )
    assert_av1_v4r3_preparation_claim(bound)
    return bound


def assert_av1_v4r3_preparation_claim(payload: Mapping[str, Any]) -> None:
    """Validate one canonical revision-3 preparation claim."""

    materialized = _materialize(payload, _CLAIM_KEYS, "claim")
    _assert_base(
        materialized,
        schema=AV1_V4R3_PREPARATION_CLAIM_SCHEMA,
        contract_version=AV1_V4R3_PREPARATION_CLAIM_CONTRACT_VERSION,
    )
    if materialized.get("state") != AV1_V4R3_PREPARATION_CLAIM_STATE:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation claim state is invalid"
        )
    if (
        materialized.get("consumption_registry_token")
        != AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
        or not _GRANT_ID_RE.fullmatch(
            str(materialized.get("preparation_grant_id") or "")
        )
        or not _RIGHTS_ID_RE.fullmatch(
            str(materialized.get("rights_attestation_id") or "")
        )
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation claim binding is invalid"
        )
    _assert_sha_fields(
        materialized,
        "manifest_payload_sha256",
        "preparation_grant_payload_sha256",
        "rights_attestation_payload_sha256",
        "payload_sha256",
    )
    _assert_owner(materialized)
    _assert_repository(materialized)
    _parse_timestamp(materialized.get("claimed_at"), "claim")
    _assert_identity(
        materialized,
        id_field="claim_id",
        id_pattern=_CLAIM_ID_RE,
        id_prefix="av1v4r3prepclaim_",
        domain=AV1_V4R3_PREPARATION_CLAIM_IDENTITY_DOMAIN,
        label="claim",
    )


def build_av1_v4r3_path_privacy_key_custody(
    *,
    preparation_claim: Mapping[str, Any],
    key_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Build path-free custody evidence for one exclusively created key."""

    claim = object_dict(preparation_claim)
    assert_av1_v4r3_preparation_claim(claim)
    if not _KEY_ID_RE.fullmatch(key_id):
        raise AV1V4R3PreparationCustodyError("AV1 v4 r3 path-privacy key ID is invalid")
    if _parse_timestamp(created_at, "key custody") < _parse_timestamp(
        claim.get("claimed_at"), "claim"
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 key custody cannot precede the claim"
        )
    payload = {
        **_base_payload(
            schema=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_SCHEMA,
            contract_version=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION,
        ),
        "state": AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_STATE,
        "consumption_registry_token": claim["consumption_registry_token"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "preparation_grant_id": claim["preparation_grant_id"],
        "preparation_grant_payload_sha256": claim["preparation_grant_payload_sha256"],
        "owner_principal": claim["owner_principal"],
        "repository": claim["repository"],
        "claimed_at": claim["claimed_at"],
        "key_id": key_id,
        "created_at": created_at,
        "key_bytes_length": 32,
        "key_file_mode": "0600",
        "key_file_create_exclusive": True,
        "key_material_serialized": False,
    }
    bound = _bind_identity(
        payload,
        id_field="custody_id",
        id_prefix="av1v4r3keycustody_",
        domain=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_IDENTITY_DOMAIN,
    )
    assert_av1_v4r3_path_privacy_key_custody(bound)
    return bound


def assert_av1_v4r3_path_privacy_key_custody(
    payload: Mapping[str, Any],
) -> None:
    """Validate one canonical revision-3 path-privacy key custody record."""

    materialized = _materialize(payload, _CUSTODY_KEYS, "key custody")
    _assert_base(
        materialized,
        schema=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_SCHEMA,
        contract_version=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION,
    )
    expected = {
        "state": AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_STATE,
        "consumption_registry_token": (AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN),
        "key_bytes_length": 32,
        "key_file_mode": "0600",
        "key_file_create_exclusive": True,
        "key_material_serialized": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 path-privacy key custody binding is invalid"
        )
    if (
        not _CLAIM_ID_RE.fullmatch(str(materialized.get("claim_id") or ""))
        or not _GRANT_ID_RE.fullmatch(
            str(materialized.get("preparation_grant_id") or "")
        )
        or not _KEY_ID_RE.fullmatch(str(materialized.get("key_id") or ""))
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 path-privacy key custody identity binding is invalid"
        )
    _assert_sha_fields(
        materialized,
        "manifest_payload_sha256",
        "claim_payload_sha256",
        "preparation_grant_payload_sha256",
        "payload_sha256",
    )
    _assert_owner(materialized)
    _assert_repository(materialized)
    claimed_at = _parse_timestamp(materialized.get("claimed_at"), "claim")
    created_at = _parse_timestamp(materialized.get("created_at"), "key custody")
    if created_at < claimed_at:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 key custody cannot precede the claim"
        )
    _assert_identity(
        materialized,
        id_field="custody_id",
        id_pattern=_CUSTODY_ID_RE,
        id_prefix="av1v4r3keycustody_",
        domain=AV1_V4R3_PATH_PRIVACY_KEY_CUSTODY_IDENTITY_DOMAIN,
        label="key custody",
    )


def serialize_av1_v4r3_preparation_registry_binding(
    payload: Mapping[str, Any],
) -> bytes:
    return _serialize(payload, assert_av1_v4r3_preparation_registry_binding)


def deserialize_av1_v4r3_preparation_registry_binding(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r3_preparation_registry_binding)


def serialize_av1_v4r3_preparation_claim(payload: Mapping[str, Any]) -> bytes:
    return _serialize(payload, assert_av1_v4r3_preparation_claim)


def deserialize_av1_v4r3_preparation_claim(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r3_preparation_claim)


def serialize_av1_v4r3_path_privacy_key_custody(
    payload: Mapping[str, Any],
) -> bytes:
    return _serialize(payload, assert_av1_v4r3_path_privacy_key_custody)


def deserialize_av1_v4r3_path_privacy_key_custody(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r3_path_privacy_key_custody)


def _base_payload(*, schema: str, contract_version: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "schema_version": 1,
        "contract_version": contract_version,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        **{field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)},
    }


def _materialize(
    payload: Mapping[str, Any], allowed: frozenset[str] | set[str], label: str
) -> dict[str, Any]:
    _assert_safe_shape(payload)
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} payload is invalid"
        )
    unknown = set(materialized) - set(allowed)
    if unknown:
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    return materialized


def _assert_base(
    payload: Mapping[str, Any], *, schema: str, contract_version: str
) -> None:
    expected = _base_payload(schema=schema, contract_version=contract_version)
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody base binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1V4R3PreparationCustodyError(
                f"AV1 v4 r3 preparation custody cannot authorize {field}"
            )


def _assert_owner(payload: Mapping[str, Any]) -> None:
    owner = payload.get("owner_principal")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody owner principal is invalid"
        )


def _assert_repository(payload: Mapping[str, Any]) -> None:
    repository = object_dict(payload.get("repository"))
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody repository binding is invalid"
        )


def _assert_sha_fields(payload: Mapping[str, Any], *fields: str) -> None:
    for field in fields:
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3PreparationCustodyError(
                f"AV1 v4 r3 preparation custody {field} is invalid"
            )


def _bind_identity(
    payload: Mapping[str, Any], *, id_field: str, id_prefix: str, domain: str
) -> dict[str, Any]:
    bound = dict(payload)
    bound[id_field] = _artifact_id(
        bound, id_field=id_field, id_prefix=id_prefix, domain=domain
    )
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _artifact_id(
    payload: Mapping[str, Any], *, id_field: str, id_prefix: str, domain: str
) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {id_field, "payload_sha256"}
    }
    digest = stable_json_hash({"domain": domain, "payload": semantic})
    return f"{id_prefix}{digest[:32]}"


def _assert_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_pattern: re.Pattern[str],
    id_prefix: str,
    domain: str,
    label: str,
) -> None:
    artifact_id = str(payload.get(id_field) or "")
    if not id_pattern.fullmatch(artifact_id) or artifact_id != _artifact_id(
        payload, id_field=id_field, id_prefix=id_prefix, domain=domain
    ):
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} ID is invalid"
        )
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} payload SHA-256 does not match"
        )


def _serialize(
    payload: Mapping[str, Any],
    assertion: Callable[[Mapping[str, Any]], None],
) -> bytes:
    assertion(payload)
    materialized = json.loads(canonical_json_bytes(payload))
    assertion(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def _deserialize(
    data: bytes,
    assertion: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody input must be bytes"
        )
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody JSON is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody payload must be a JSON object"
        )
    assertion(payload)
    try:
        canonical = canonical_json_bytes(payload) + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody JSON is invalid"
        ) from exc
    if data != canonical:
        raise AV1V4R3PreparationCustodyError(
            "AV1 v4 r3 preparation custody bytes are not canonical"
        )
    return payload


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} timestamp must use canonical UTC seconds"
        )
    normalized = parsed.astimezone(UTC)
    if normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3PreparationCustodyError(
            f"AV1 v4 r3 preparation {label} timestamp must use canonical UTC seconds"
        )
    return normalized


def _assert_no_private_text(value: Any) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if av1_validation_v4_contains_private_text(current):
                raise AV1V4R3PreparationCustodyError(
                    "AV1 v4 r3 preparation custody contains private path-like text"
                )
        elif isinstance(current, Mapping):
            for key, nested in current.items():
                stack.append(str(key))
                stack.append(nested)
        elif isinstance(current, Sequence) and not isinstance(
            current, (bytes, bytearray)
        ):
            stack.extend(current)


def _assert_safe_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > _MAX_PAYLOAD_DEPTH or nodes > _MAX_PAYLOAD_NODES:
            raise AV1V4R3PreparationCustodyError(
                "AV1 v4 r3 preparation custody payload is too deep or large"
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise AV1V4R3PreparationCustodyError(
                "AV1 v4 r3 preparation custody payload contains a non-finite number"
            )
        if isinstance(current, Mapping):
            for key, nested in current.items():
                stack.append((str(key), depth + 1))
                stack.append((nested, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((nested, depth + 1) for nested in current)
