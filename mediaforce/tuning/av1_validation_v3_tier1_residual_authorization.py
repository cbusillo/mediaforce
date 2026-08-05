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
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
    assert_av1_validation_v3_qualification_plan_active,
)


AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_residual_request"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_residual_grant"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_CONTRACT_VERSION = "av1vt1rpr1"
AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_CONTRACT_VERSION = "av1vt1rpg1"
AV1_VALIDATION_V3_TIER1_RESIDUAL_AUTHORITY = "av1_v3_tier1_residual_probe_execution"
_OWNER_PRINCIPAL_RE = re.compile(r"owner-[a-z0-9]{8,32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier1ResidualAuthorizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualRequest:
    request_id: str
    protocol_id: str
    protocol_payload_sha256: str
    qualification_plan_id: str
    qualification_plan_payload_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    probe_matrix_sha256: str
    requested_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.request_id.startswith("av1vtier1residualrequest3_"):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual request ID is invalid"
            )
        for value in (
            self.protocol_payload_sha256,
            self.qualification_plan_payload_sha256,
            self.config_sha256,
            self.toolchain_sha256,
            self.probe_matrix_sha256,
            self.payload_sha256,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier1ResidualAuthorizationError(
                    "AV1 v3 Tier 1 residual request digest is invalid"
                )
        requested_at = _parse_timestamp(self.requested_at)
        valid_until = _parse_timestamp(self.valid_until)
        if valid_until <= requested_at:
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual request expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.request_id != av1_validation_v3_id("tier1residualrequest", semantic):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual request ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"request_id": self.request_id, **semantic}
        ):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual request digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER1_RESIDUAL_AUTHORITY,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "fixture_scope": "deterministic_synthetic_public_only",
            "request_state": "owner_action_required",
            "residual_probe_execution_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "qualification_plan_id": self.qualification_plan_id,
            "qualification_plan_payload_sha256": self.qualification_plan_payload_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "probe_matrix_sha256": self.probe_matrix_sha256,
            "requested_at": self.requested_at,
            "valid_until": self.valid_until,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualGrant:
    grant_id: str
    request_id: str
    request_payload_sha256: str
    owner_principal: str
    authorized_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.grant_id.startswith("av1vtier1residualgrant3_"):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual grant ID is invalid"
            )
        if (
            not self.request_id.startswith("av1vtier1residualrequest3_")
            or not _SHA256_RE.fullmatch(self.request_payload_sha256)
            or not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal)
        ):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual grant binding is invalid"
            )
        authorized_at = _parse_timestamp(self.authorized_at)
        valid_until = _parse_timestamp(self.valid_until)
        if valid_until <= authorized_at:
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual grant expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.grant_id != av1_validation_v3_id("tier1residualgrant", semantic):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual grant ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"grant_id": self.grant_id, **semantic}
        ):
            raise AV1ValidationV3Tier1ResidualAuthorizationError(
                "AV1 v3 Tier 1 residual grant digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER1_RESIDUAL_AUTHORITY,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "fixture_scope": "deterministic_synthetic_public_only",
            "residual_probe_execution_authorized": True,
            **_false_authority_fields(),
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "owner_principal": self.owner_principal,
            "authorized_at": self.authorized_at,
            "valid_until": self.valid_until,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


def build_av1_validation_v3_tier1_residual_request(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    probe_matrix_sha256: str,
    requested_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier1ResidualRequest:
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=requested_at,
    )
    if _parse_timestamp(valid_until) > _parse_timestamp(plan.valid_until):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual request cannot outlive its plan"
        )
    semantic = _request_semantic(
        protocol=protocol,
        plan=plan,
        probe_matrix_sha256=probe_matrix_sha256,
        requested_at=requested_at,
        valid_until=valid_until,
    )
    request_id = av1_validation_v3_id("tier1residualrequest", semantic)
    return AV1ValidationV3Tier1ResidualRequest(
        request_id=request_id,
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        qualification_plan_id=plan.plan_id,
        qualification_plan_payload_sha256=plan.payload_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        probe_matrix_sha256=probe_matrix_sha256,
        requested_at=requested_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"request_id": request_id, **semantic}),
    )


def assert_av1_validation_v3_tier1_residual_request_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1ResidualRequest,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_qualification_plan_active(protocol, plan, as_of=as_of)
    if request != build_av1_validation_v3_tier1_residual_request(
        protocol=protocol,
        plan=plan,
        probe_matrix_sha256=request.probe_matrix_sha256,
        requested_at=request.requested_at,
        valid_until=request.valid_until,
    ):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual request is not bound to its plan"
        )
    checked_at = _parse_timestamp(as_of)
    if checked_at < _parse_timestamp(request.requested_at) or checked_at >= _parse_timestamp(
        request.valid_until
    ):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual request is not active"
        )


def build_av1_validation_v3_tier1_residual_grant(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1ResidualRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier1ResidualGrant:
    assert_av1_validation_v3_tier1_residual_request_active(
        protocol,
        plan,
        request,
        as_of=authorized_at,
    )
    if _parse_timestamp(valid_until) > _parse_timestamp(request.valid_until):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual grant cannot outlive its request"
        )
    semantic = _grant_semantic(
        request=request,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
    )
    grant_id = av1_validation_v3_id("tier1residualgrant", semantic)
    return AV1ValidationV3Tier1ResidualGrant(
        grant_id=grant_id,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"grant_id": grant_id, **semantic}),
    )


def assert_av1_validation_v3_tier1_residual_grant_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1ResidualRequest,
    grant: AV1ValidationV3Tier1ResidualGrant,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier1_residual_request_active(
        protocol,
        plan,
        request,
        as_of=as_of,
    )
    if grant.request_id != request.request_id or grant.request_payload_sha256 != request.payload_sha256:
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual grant is not bound to its request"
        )
    checked_at = _parse_timestamp(as_of)
    if checked_at < _parse_timestamp(grant.authorized_at) or checked_at >= _parse_timestamp(
        grant.valid_until
    ):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual grant is not active"
        )


def serialize_av1_validation_v3_tier1_residual_request(
    request: AV1ValidationV3Tier1ResidualRequest,
) -> bytes:
    return canonical_json_bytes(request.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier1_residual_grant(
    grant: AV1ValidationV3Tier1ResidualGrant,
) -> bytes:
    return canonical_json_bytes(grant.to_payload()) + b"\n"


def load_av1_validation_v3_tier1_residual_request(
    path: Path,
) -> AV1ValidationV3Tier1ResidualRequest:
    return _load(
        path,
        av1_validation_v3_tier1_residual_request_from_payload,
        serialize_av1_validation_v3_tier1_residual_request,
    )


def load_av1_validation_v3_tier1_residual_grant(
    path: Path,
) -> AV1ValidationV3Tier1ResidualGrant:
    return _load(
        path,
        av1_validation_v3_tier1_residual_grant_from_payload,
        serialize_av1_validation_v3_tier1_residual_grant,
    )


def av1_validation_v3_tier1_residual_request_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1ResidualRequest:
    value = object_dict(payload)
    request = AV1ValidationV3Tier1ResidualRequest(
        request_id=str(value.get("request_id") or ""),
        protocol_id=str(value.get("protocol_id") or ""),
        protocol_payload_sha256=str(value.get("protocol_payload_sha256") or ""),
        qualification_plan_id=str(value.get("qualification_plan_id") or ""),
        qualification_plan_payload_sha256=str(
            value.get("qualification_plan_payload_sha256") or ""
        ),
        repository_commit=str(value.get("repository_commit") or ""),
        repository_tree=str(value.get("repository_tree") or ""),
        config_sha256=str(value.get("config_sha256") or ""),
        toolchain_sha256=str(value.get("toolchain_sha256") or ""),
        probe_matrix_sha256=str(value.get("probe_matrix_sha256") or ""),
        requested_at=str(value.get("requested_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != request.to_payload():
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual request fields are invalid"
        )
    return request


def av1_validation_v3_tier1_residual_grant_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1ResidualGrant:
    value = object_dict(payload)
    grant = AV1ValidationV3Tier1ResidualGrant(
        grant_id=str(value.get("grant_id") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        authorized_at=str(value.get("authorized_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != grant.to_payload():
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual grant fields are invalid"
        )
    return grant


def _request_semantic(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    probe_matrix_sha256: str,
    requested_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER1_RESIDUAL_AUTHORITY,
        "gate": "A0",
        "tier": "none",
        "diagnostic_only": True,
        "fixture_scope": "deterministic_synthetic_public_only",
        "request_state": "owner_action_required",
        "residual_probe_execution_authorized": False,
        "execution_requires_separate_owner_authorization": True,
        **_false_authority_fields(),
        "protocol_id": protocol.protocol_id,
        "protocol_payload_sha256": protocol.payload_sha256,
        "qualification_plan_id": plan.plan_id,
        "qualification_plan_payload_sha256": plan.payload_sha256,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "toolchain_sha256": plan.toolchain_sha256,
        "probe_matrix_sha256": probe_matrix_sha256,
        "requested_at": requested_at,
        "valid_until": valid_until,
    }


def _grant_semantic(
    *,
    request: AV1ValidationV3Tier1ResidualRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER1_RESIDUAL_AUTHORITY,
        "gate": "A0",
        "tier": "none",
        "diagnostic_only": True,
        "fixture_scope": "deterministic_synthetic_public_only",
        "residual_probe_execution_authorized": True,
        **_false_authority_fields(),
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "owner_principal": owner_principal,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
    }


def _false_authority_fields() -> dict[str, bool]:
    return {
        "tier1_execution_authorized": False,
        "tier1_coverage_eligible": False,
        "tier2_execution_authorized": False,
        "qualification_execution_authorized": False,
        "qualification_complete": False,
        "path_matrix_coverage_claimed": False,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "public_bundle_activation_allowed": False,
        "private_inventory_read_authorized": False,
        "media_library_read_authorized": False,
        "key_creation_authorized": False,
        "retry_authorized": False,
    }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual timestamp is invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.microsecond
        or not value.endswith("Z")
        or parsed.isoformat().replace("+00:00", "Z") != value
    ):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual timestamp is invalid"
        )
    return parsed


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _load(path: Path, parser: Any, serializer: Any) -> Any:
    raw = path.read_bytes()
    try:
        item = parser(object_dict(json.loads(raw.decode("utf-8"))))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual authorization artifact is invalid"
        ) from exc
    if raw != serializer(item):
        raise AV1ValidationV3Tier1ResidualAuthorizationError(
            "AV1 v3 Tier 1 residual authorization bytes are not canonical"
        )
    return item
