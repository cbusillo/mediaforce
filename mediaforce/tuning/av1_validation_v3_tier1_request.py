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
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_PROTOCOL_VERSION,
    AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
    AV1ValidationProtocolV3,
    av1_validation_v3_id,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    AV1ValidationV3QualificationPlan,
    assert_av1_validation_v3_qualification_plan_active,
)


AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_authorization_request"
)
AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER1_REQUEST_CONTRACT_VERSION = "av1vt1r1"
AV1_VALIDATION_V3_TIER1_REQUEST_AUTHORITY = "av1_v3_tier1_owner_request"
AV1_VALIDATION_V3_TIER1_FIXTURE_SCOPE = "deterministic_synthetic_public_only"

_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier1RequestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1AuthorizationRequest:
    request_id: str
    protocol_id: str
    protocol_payload_sha256: str
    qualification_plan_id: str
    qualification_plan_payload_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    fixture_matrix_sha256: str
    expected_path_matrix_sha256: str
    requested_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.protocol_id.startswith("av1vprotocol3_"):
            raise AV1ValidationV3Tier1RequestError("AV1 v3 Tier 1 request protocol ID is invalid")
        if not self.qualification_plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier1RequestError(
                "AV1 v3 Tier 1 request qualification plan ID is invalid"
            )
        for value, label in (
            (self.protocol_payload_sha256, "protocol digest"),
            (self.qualification_plan_payload_sha256, "qualification plan digest"),
            (self.config_sha256, "config digest"),
            (self.toolchain_sha256, "toolchain digest"),
            (self.fixture_matrix_sha256, "fixture-matrix digest"),
            (self.expected_path_matrix_sha256, "expected path-matrix digest"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.repository_commit, "repository commit"),
            (self.repository_tree, "repository tree"),
        ):
            if not _GIT_OBJECT_ID_RE.fullmatch(value):
                raise AV1ValidationV3Tier1RequestError(f"AV1 v3 Tier 1 request {label} is invalid")
        requested_at = _parse_timestamp(self.requested_at, "request timestamp")
        valid_until = _parse_timestamp(self.valid_until, "request expiration")
        if valid_until <= requested_at:
            raise AV1ValidationV3Tier1RequestError(
                "AV1 v3 Tier 1 request expiration is invalid"
            )
        semantic_payload = self.semantic_payload()
        if self.request_id != av1_validation_v3_id("tier1request", semantic_payload):
            raise AV1ValidationV3Tier1RequestError(
                "AV1 v3 Tier 1 request ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({"request_id": self.request_id, **semantic_payload}):
            raise AV1ValidationV3Tier1RequestError(
                "AV1 v3 Tier 1 request digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA_VERSION,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "contract_version": AV1_VALIDATION_V3_TIER1_REQUEST_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER1_REQUEST_AUTHORITY,
            "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
            "tier": "tier1",
            "fixture_scope": AV1_VALIDATION_V3_TIER1_FIXTURE_SCOPE,
            "request_state": "owner_action_required",
            "evidence_eligible": False,
            "private_inventory_read_authorized": False,
            "key_creation_authorized": False,
            "media_read_authorized": False,
            "qualification_execution_authorized": False,
            "evidence_creation_authorized": False,
            "empirical_authority_conferred": False,
            "public_bundle_activation_allowed": False,
            "execution_requires_separate_owner_authorization": True,
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "qualification_plan_id": self.qualification_plan_id,
            "qualification_plan_payload_sha256": self.qualification_plan_payload_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "fixture_matrix_sha256": self.fixture_matrix_sha256,
            "expected_path_matrix_sha256": self.expected_path_matrix_sha256,
            "requested_at": self.requested_at,
            "valid_until": self.valid_until,
            "gate": "A0",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_owner_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        plan: AV1ValidationV3QualificationPlan,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier1_request_active(
            protocol,
            plan,
            self,
            as_of=as_of,
        )
        summary = {
            "tier": "tier1",
            "fixture_scope": AV1_VALIDATION_V3_TIER1_FIXTURE_SCOPE,
            "request_state": "owner_action_required",
            "execution_authorized": False,
            "evidence_eligible": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def build_av1_validation_v3_tier1_authorization_request(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    requested_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier1AuthorizationRequest:
    _assert_plan_active(protocol, plan, as_of=requested_at)
    if _parse_timestamp(valid_until, "request expiration") > _parse_timestamp(
        plan.valid_until,
        "qualification plan expiration",
    ):
        raise AV1ValidationV3Tier1RequestError(
            "AV1 v3 Tier 1 request cannot outlive its qualification plan"
        )
    semantic_payload = _request_semantic_payload(
        protocol=protocol,
        plan=plan,
        requested_at=requested_at,
        valid_until=valid_until,
    )
    request_id = av1_validation_v3_id("tier1request", semantic_payload)
    return AV1ValidationV3Tier1AuthorizationRequest(
        request_id=request_id,
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        qualification_plan_id=plan.plan_id,
        qualification_plan_payload_sha256=plan.payload_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        fixture_matrix_sha256=plan.fixture_matrix_sha256,
        expected_path_matrix_sha256=plan.expected_path_matrix_sha256,
        requested_at=requested_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"request_id": request_id, **semantic_payload}),
    )


def assert_av1_validation_v3_tier1_request_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    *,
    as_of: str,
) -> None:
    _assert_plan_active(protocol, plan, as_of=as_of)
    if (
        request.protocol_id != protocol.protocol_id
        or request.protocol_payload_sha256 != protocol.payload_sha256
        or request.qualification_plan_id != plan.plan_id
        or request.qualification_plan_payload_sha256 != plan.payload_sha256
        or request.repository_commit != plan.repository_commit
        or request.repository_tree != plan.repository_tree
        or request.config_sha256 != plan.config_sha256
        or request.toolchain_sha256 != plan.toolchain_sha256
        or request.fixture_matrix_sha256 != plan.fixture_matrix_sha256
        or request.expected_path_matrix_sha256 != plan.expected_path_matrix_sha256
    ):
        raise AV1ValidationV3Tier1RequestError(
            "AV1 v3 Tier 1 request is not bound to its frozen qualification plan"
        )
    checked_at = _parse_timestamp(as_of, "request active-check timestamp")
    requested_at = _parse_timestamp(request.requested_at, "request timestamp")
    valid_until = _parse_timestamp(request.valid_until, "request expiration")
    if checked_at < requested_at or checked_at >= valid_until:
        raise AV1ValidationV3Tier1RequestError(
            "AV1 v3 Tier 1 request is not active at the requested time"
        )


def serialize_av1_validation_v3_tier1_authorization_request(
    request: AV1ValidationV3Tier1AuthorizationRequest,
) -> bytes:
    return canonical_json_bytes(request.to_payload()) + b"\n"


def load_av1_validation_v3_tier1_authorization_request(
    path: Path,
) -> AV1ValidationV3Tier1AuthorizationRequest:
    raw = path.read_bytes()
    try:
        request = av1_validation_v3_tier1_request_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1RequestError("AV1 v3 Tier 1 request is invalid") from exc
    if raw != serialize_av1_validation_v3_tier1_authorization_request(request):
        raise AV1ValidationV3Tier1RequestError(
            "AV1 v3 Tier 1 request bytes are not canonical"
        )
    return request


def av1_validation_v3_tier1_request_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1AuthorizationRequest:
    value = object_dict(payload)
    expected_keys = {
        "request_id",
        "schema",
        "schema_version",
        "protocol_version",
        "experiment_id",
        "contract_version",
        "authority",
        "artifact_namespace",
        "tier",
        "fixture_scope",
        "request_state",
        "evidence_eligible",
        "private_inventory_read_authorized",
        "key_creation_authorized",
        "media_read_authorized",
        "qualification_execution_authorized",
        "evidence_creation_authorized",
        "empirical_authority_conferred",
        "public_bundle_activation_allowed",
        "execution_requires_separate_owner_authorization",
        "protocol_id",
        "protocol_payload_sha256",
        "qualification_plan_id",
        "qualification_plan_payload_sha256",
        "repository_commit",
        "repository_tree",
        "config_sha256",
        "toolchain_sha256",
        "fixture_matrix_sha256",
        "expected_path_matrix_sha256",
        "requested_at",
        "valid_until",
        "gate",
        "payload_sha256",
    }
    if set(value) != expected_keys:
        raise AV1ValidationV3Tier1RequestError("AV1 v3 Tier 1 request keys are invalid")
    request = AV1ValidationV3Tier1AuthorizationRequest(
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
        fixture_matrix_sha256=str(value.get("fixture_matrix_sha256") or ""),
        expected_path_matrix_sha256=str(value.get("expected_path_matrix_sha256") or ""),
        requested_at=str(value.get("requested_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != request.to_payload():
        raise AV1ValidationV3Tier1RequestError("AV1 v3 Tier 1 request payload is invalid")
    return request


def _request_semantic_payload(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    requested_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_REQUEST_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
        "contract_version": AV1_VALIDATION_V3_TIER1_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER1_REQUEST_AUTHORITY,
        "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
        "tier": "tier1",
        "fixture_scope": AV1_VALIDATION_V3_TIER1_FIXTURE_SCOPE,
        "request_state": "owner_action_required",
        "evidence_eligible": False,
        "private_inventory_read_authorized": False,
        "key_creation_authorized": False,
        "media_read_authorized": False,
        "qualification_execution_authorized": False,
        "evidence_creation_authorized": False,
        "empirical_authority_conferred": False,
        "public_bundle_activation_allowed": False,
        "execution_requires_separate_owner_authorization": True,
        "protocol_id": protocol.protocol_id,
        "protocol_payload_sha256": protocol.payload_sha256,
        "qualification_plan_id": plan.plan_id,
        "qualification_plan_payload_sha256": plan.payload_sha256,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "toolchain_sha256": plan.toolchain_sha256,
        "fixture_matrix_sha256": plan.fixture_matrix_sha256,
        "expected_path_matrix_sha256": plan.expected_path_matrix_sha256,
        "requested_at": requested_at,
        "valid_until": valid_until,
        "gate": "A0",
    }


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _assert_plan_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    *,
    as_of: str,
) -> None:
    try:
        assert_av1_validation_v3_qualification_plan_active(protocol, plan, as_of=as_of)
    except AV1ValidationV3QualificationError as exc:
        raise AV1ValidationV3Tier1RequestError(
            "AV1 v3 Tier 1 request qualification plan is not active at the requested time"
        ) from exc


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1RequestError(f"AV1 v3 Tier 1 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Tier1RequestError(
            f"AV1 v3 Tier 1 {label} must include a timezone"
        )
    return parsed.astimezone(UTC)


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationV3Tier1RequestError(f"AV1 v3 Tier 1 {label} is invalid")
