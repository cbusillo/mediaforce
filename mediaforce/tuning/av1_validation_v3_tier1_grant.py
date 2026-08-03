from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3, av1_validation_v3_id
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
    AV1ValidationV3Tier1RequestError,
    assert_av1_validation_v3_tier1_request_active,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
)


AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA = "mediaforce.av1_cold_start_v3_tier1_execution_grant"
AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER1_GRANT_CONTRACT_VERSION = "av1vt1g1"
AV1_VALIDATION_V3_TIER1_GRANT_AUTHORITY = "av1_v3_tier1_execution"

_OWNER_PRINCIPAL_RE = re.compile(r"owner-[a-z0-9]{8,32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier1GrantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ExecutionGrant:
    grant_id: str
    request_id: str
    request_payload_sha256: str
    owner_principal: str
    authorized_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.request_id.startswith("av1vtier1request3_"):
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant request ID is invalid")
        if not _SHA256_RE.fullmatch(self.request_payload_sha256):
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant request digest is invalid")
        if not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal):
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant owner principal is invalid")
        authorized_at = _parse_timestamp(self.authorized_at, "grant timestamp")
        valid_until = _parse_timestamp(self.valid_until, "grant expiration")
        if valid_until <= authorized_at:
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant expiration is invalid")
        semantic_payload = self.semantic_payload()
        if self.grant_id != av1_validation_v3_id("tier1grant", semantic_payload):
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"grant_id": self.grant_id, **semantic_payload}):
            raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant digest does not match its payload")
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER1_GRANT_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER1_GRANT_AUTHORITY,
            "tier": "tier1",
            "fixture_scope": "deterministic_synthetic_public_only",
            "evidence_eligible": False,
            "tier1_execution_authorized": True,
            "private_inventory_read_authorized": False,
            "key_creation_authorized": False,
            "media_library_read_authorized": False,
            "tier2_execution_authorized": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "activation_authorized": False,
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "owner_principal": self.owner_principal,
            "authorized_at": self.authorized_at,
            "valid_until": self.valid_until,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"grant_id": self.grant_id, **self.semantic_payload(), "payload_sha256": self.payload_sha256}


def build_av1_validation_v3_tier1_execution_grant(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier1ExecutionGrant:
    _assert_request_active(protocol, plan, request, as_of=authorized_at)
    if _parse_timestamp(valid_until, "grant expiration") > _parse_timestamp(
        request.valid_until,
        "request expiration",
    ):
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant cannot outlive its request")
    semantic_payload = _grant_semantic_payload(
        request=request,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
    )
    grant_id = av1_validation_v3_id("tier1grant", semantic_payload)
    return AV1ValidationV3Tier1ExecutionGrant(
        grant_id=grant_id,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"grant_id": grant_id, **semantic_payload}),
    )


def assert_av1_validation_v3_tier1_grant_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    *,
    as_of: str,
) -> None:
    _assert_request_active(protocol, plan, request, as_of=as_of)
    if grant.request_id != request.request_id or grant.request_payload_sha256 != request.payload_sha256:
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant is not bound to its request")
    checked_at = _parse_timestamp(as_of, "grant active-check timestamp")
    authorized_at = _parse_timestamp(grant.authorized_at, "grant timestamp")
    valid_until = _parse_timestamp(grant.valid_until, "grant expiration")
    if checked_at < authorized_at or checked_at >= valid_until:
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant is not active at the requested time")


def serialize_av1_validation_v3_tier1_execution_grant(
    grant: AV1ValidationV3Tier1ExecutionGrant,
) -> bytes:
    return canonical_json_bytes(grant.to_payload()) + b"\n"


def load_av1_validation_v3_tier1_execution_grant(path: Path) -> AV1ValidationV3Tier1ExecutionGrant:
    raw = path.read_bytes()
    try:
        grant = av1_validation_v3_tier1_grant_from_payload(object_dict(json.loads(raw.decode("utf-8"))))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant is invalid") from exc
    if raw != serialize_av1_validation_v3_tier1_execution_grant(grant):
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant bytes are not canonical")
    return grant


def av1_validation_v3_tier1_grant_from_payload(payload: Mapping[str, Any]) -> AV1ValidationV3Tier1ExecutionGrant:
    value = object_dict(payload)
    expected_keys = {
        "grant_id", "schema", "schema_version", "contract_version", "authority", "tier",
        "fixture_scope", "evidence_eligible", "tier1_execution_authorized",
        "private_inventory_read_authorized", "key_creation_authorized", "media_library_read_authorized",
        "tier2_execution_authorized", "empirical_authority_conferred", "derivation_authorized",
        "holdout_authorized", "publication_authorized", "activation_authorized", "request_id",
        "request_payload_sha256", "owner_principal", "authorized_at", "valid_until", "payload_sha256",
    }
    if set(value) != expected_keys:
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant keys are invalid")
    grant = AV1ValidationV3Tier1ExecutionGrant(
        grant_id=str(value.get("grant_id") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        authorized_at=str(value.get("authorized_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != grant.to_payload():
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant payload is invalid")
    return grant


def _grant_semantic_payload(*, request: AV1ValidationV3Tier1AuthorizationRequest, owner_principal: str, authorized_at: str, valid_until: str) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER1_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER1_GRANT_AUTHORITY,
        "tier": "tier1",
        "fixture_scope": "deterministic_synthetic_public_only",
        "evidence_eligible": False,
        "tier1_execution_authorized": True,
        "private_inventory_read_authorized": False,
        "key_creation_authorized": False,
        "media_library_read_authorized": False,
        "tier2_execution_authorized": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "owner_principal": owner_principal,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
    }


def _assert_request_active(protocol: AV1ValidationProtocolV3, plan: AV1ValidationV3QualificationPlan, request: AV1ValidationV3Tier1AuthorizationRequest, *, as_of: str) -> None:
    try:
        assert_av1_validation_v3_tier1_request_active(protocol, plan, request, as_of=as_of)
    except AV1ValidationV3Tier1RequestError as exc:
        raise AV1ValidationV3Tier1GrantError("AV1 v3 Tier 1 grant request is not active") from exc


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1GrantError(f"AV1 v3 Tier 1 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Tier1GrantError(f"AV1 v3 Tier 1 {label} must include a timezone")
    return parsed.astimezone(UTC)
