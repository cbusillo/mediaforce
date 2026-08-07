from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    AV1ValidationV4PreparationGrantError,
    assert_av1_validation_v4_preparation_grant_active,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_claim"
)
AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PREPARATION_CLAIM_CONTRACT_VERSION = "av1v4pclaim1"
AV1_VALIDATION_V4_PREPARATION_CLAIM_STATE = "grant_consumed"

_CLAIM_ID_RE = re.compile(r"av1vprepclaim4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "claim_id",
    "claimed_at",
    "contract_version",
    "discovery_public_sha256",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "owner_principal",
    "payload_sha256",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "protocol_version",
    "repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "schema",
    "schema_version",
    "state",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS


class AV1ValidationV4PreparationClaimError(ValueError):
    pass


def build_av1_validation_v4_preparation_claim(
    *,
    preparation_grant: Mapping[str, Any],
    rights_attestation: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    rights = object_dict(rights_attestation)
    try:
        assert_av1_validation_v4_preparation_grant_active(grant, as_of=claimed_at)
        assert_av1_validation_v4_rights_attestation(rights)
    except (AV1ValidationV4PreparationGrantError, AV1ValidationV4RightsError) as exc:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim inputs are invalid"
        ) from exc
    if rights.get("state") != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim requires owner-attested rights"
        )
    if (
        grant.get("rights_attestation_id") != rights.get("attestation_id")
        or grant.get("rights_attestation_payload_sha256")
        != rights.get("payload_sha256")
        or grant.get("owner_principal") != rights.get("owner_principal")
    ):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim rights binding does not match"
        )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PREPARATION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_PREPARATION_CLAIM_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "owner_principal": rights["owner_principal"],
        "repository": grant["repository"],
        "claimed_at": claimed_at,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_preparation_claim(bound)
    return bound


def assert_av1_validation_v4_preparation_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim payload is invalid"
        )
    unknown = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise AV1ValidationV4PreparationClaimError(
            f"AV1 v4 preparation claim contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PREPARATION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_PREPARATION_CLAIM_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4PreparationClaimError(
                f"AV1 v4 preparation claim cannot authorize {field}"
            )
    for field in (
        "manifest_payload_sha256",
        "preparation_grant_payload_sha256",
        "rights_attestation_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4PreparationClaimError(
                f"AV1 v4 preparation claim {field} is invalid"
            )
    repository = object_dict(materialized.get("repository"))
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim repository binding is invalid"
        )
    _parse_canonical_timestamp(materialized.get("claimed_at"))
    claim_id = str(materialized.get("claim_id") or "")
    if not _CLAIM_ID_RE.fullmatch(claim_id):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim ID is invalid"
        )
    if claim_id != av1_validation_v4_preparation_claim_id(materialized):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim ID does not match its payload"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim payload SHA-256 does not match"
        )


def serialize_av1_validation_v4_preparation_claim(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_claim(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_preparation_claim(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim bytes are not canonical"
        )
    assert_av1_validation_v4_preparation_claim(payload)
    return payload


def av1_validation_v4_preparation_claim_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"claim_id", "payload_sha256"}
    }
    return f"av1vprepclaim4_{stable_json_hash(semantic)[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["claim_id"] = av1_validation_v4_preparation_claim_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_no_private_text(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4PreparationClaimError(
                "AV1 v4 preparation claim exposes machine-local text"
            )

    visit(payload)


def _parse_canonical_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim timestamp is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4PreparationClaimError(
            "AV1 v4 preparation claim timestamp must be canonical UTC seconds"
        )
    return normalized
