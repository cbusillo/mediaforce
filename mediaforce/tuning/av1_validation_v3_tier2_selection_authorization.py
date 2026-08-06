from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Mapping

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    av1_validation_v3_hmac_domain,
    av1_validation_v3_id,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
    assert_av1_validation_v3_qualification_plan_active,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadClaim,
    AV1ValidationV3Tier2InventoryReadContext,
    AV1ValidationV3Tier2InventoryReadRequest,
    assert_av1_validation_v3_tier2_inventory_read_claim_active,
    av1_validation_v3_tier2_inventory_projection_contract_digest,
    av1_validation_v3_tier2_inventory_scope_digest,
)


AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_selection_request"
)
AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_selection_grant"
)
AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_selection_claim"
)
AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_CONTRACT_VERSION = "av1vt2sr1"
AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_CONTRACT_VERSION = "av1vt2sg1"
AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_CONTRACT_VERSION = "av1vt2sc1"
AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY = "av1_v3_tier2_selection"
AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_DOMAIN = (
    "mediaforce:av1:v3:tier2-selection-authorization-contract:v1"
)

_OWNER_PRINCIPAL_RE = re.compile(r"owner-[a-z0-9]{8,32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_KEY_ID_RE = re.compile(r"av1vqkey3_[0-9a-f]{32}\Z")

AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS = tuple(sorted([
    "activation_authorized",
    "derivation_authorized",
    "empirical_authority_conferred",
    "evidence_creation_authorized",
    "evidence_eligible",
    "holdout_authorized",
    "key_creation_authorized",
    "media_library_read_authorized",
    "media_read_authorized",
    "path_matrix_coverage_claimed",
    "private_inventory_read_authorized",
    "private_inventory_serialization_authorized",
    "public_bundle_activation_allowed",
    "publication_authorized",
    "qualification_complete",
    "qualification_execution_authorized",
    "retry_authorized",
    "runtime_execution_authorized",
    "tier1_coverage_eligible",
    "tier1_execution_authorized",
    "tier2_execution_authorized",
]))


class AV1ValidationV3Tier2SelectionAuthorizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionRequest:
    request_id: str
    protocol_id: str
    protocol_payload_sha256: str
    qualification_plan_id: str
    qualification_plan_payload_sha256: str
    qualification_key_id: str
    eligibility_predicate_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    tier2_scope_digest: str
    inventory_projection_contract_digest: str
    selection_contract_digest: str
    inner_inventory_request_id: str
    inner_inventory_request_payload_sha256: str
    requested_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.request_id.startswith("av1vtier2selrequest3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request ID is invalid"
            )
        if not self.protocol_id.startswith("av1vprotocol3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request protocol ID is invalid"
            )
        if not self.qualification_plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request plan ID is invalid"
            )
        if not _KEY_ID_RE.fullmatch(self.qualification_key_id):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request key ID is invalid"
            )
        if not self.inner_inventory_request_id.startswith("av1vtier2invreadrequest3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request inner inventory request ID is invalid"
            )
        for value, label in (
            (self.protocol_payload_sha256, "protocol digest"),
            (self.qualification_plan_payload_sha256, "plan digest"),
            (self.eligibility_predicate_sha256, "eligibility predicate digest"),
            (self.config_sha256, "config digest"),
            (self.tier2_scope_digest, "Tier 2 scope digest"),
            (self.inventory_projection_contract_digest, "inventory projection contract digest"),
            (self.selection_contract_digest, "selection contract digest"),
            (self.inner_inventory_request_payload_sha256, "inner inventory request digest"),
            (self.payload_sha256, "payload digest"),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier2SelectionAuthorizationError(
                    f"AV1 v3 Tier 2 selection request {label} is invalid"
                )
        for value, label in (
            (self.repository_commit, "repository commit"),
            (self.repository_tree, "repository tree"),
        ):
            if not _GIT_OBJECT_ID_RE.fullmatch(value):
                raise AV1ValidationV3Tier2SelectionAuthorizationError(
                    f"AV1 v3 Tier 2 selection request {label} is invalid"
                )
        requested_at = _parse_timestamp(
            self.requested_at, "request timestamp", canonical=True
        )
        valid_until = _parse_timestamp(
            self.valid_until, "request expiration", canonical=True
        )
        if valid_until <= requested_at:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.request_id != av1_validation_v3_id("tier2selrequest", semantic):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"request_id": self.request_id, **semantic}
        ):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection request digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "request_state": "owner_action_required",
            "single_selection_requested": True,
            "tier2_selection_execution_authorized": False,
            "qualification_key_read_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "qualification_plan_id": self.qualification_plan_id,
            "qualification_plan_payload_sha256": self.qualification_plan_payload_sha256,
            "qualification_key_id": self.qualification_key_id,
            "eligibility_predicate_sha256": self.eligibility_predicate_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "tier2_scope_digest": self.tier2_scope_digest,
            "inventory_projection_contract_digest": self.inventory_projection_contract_digest,
            "selection_contract_digest": self.selection_contract_digest,
            "inner_inventory_request_id": self.inner_inventory_request_id,
            "inner_inventory_request_payload_sha256": self.inner_inventory_request_payload_sha256,
            "requested_at": self.requested_at,
            "valid_until": self.valid_until,
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
        inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_selection_request_active(
            protocol, plan, inner_inventory_request, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_selection_request",
            "gate": "A0",
            "tier": "tier2",
            "request_state": "owner_action_required",
            "single_selection_requested": True,
            "tier2_selection_execution_authorized": False,
            "qualification_key_read_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionGrant:
    grant_id: str
    request_id: str
    request_payload_sha256: str
    owner_principal: str
    authorized_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.grant_id.startswith("av1vtier2selgrant3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant ID is invalid"
            )
        if not self.request_id.startswith("av1vtier2selrequest3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant request ID is invalid"
            )
        if not _SHA256_RE.fullmatch(self.request_payload_sha256):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant request digest is invalid"
            )
        if not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant owner principal is invalid"
            )
        authorized_at = _parse_timestamp(
            self.authorized_at, "grant timestamp", canonical=True
        )
        valid_until = _parse_timestamp(
            self.valid_until, "grant expiration", canonical=True
        )
        if valid_until <= authorized_at:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.grant_id != av1_validation_v3_id("tier2selgrant", semantic):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"grant_id": self.grant_id, **semantic}
        ):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection grant digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "single_selection_authorized": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
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

    def to_owner_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        plan: AV1ValidationV3QualificationPlan,
        inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
        request: AV1ValidationV3Tier2SelectionRequest,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_selection_grant_active(
            protocol, plan, inner_inventory_request, request, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_selection_grant",
            "gate": "A0",
            "tier": "tier2",
            "single_selection_authorized": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
            "owner_principal": self.owner_principal,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionClaim:
    claim_id: str
    plan_id: str
    plan_payload_sha256: str
    request_id: str
    request_payload_sha256: str
    grant_id: str
    grant_payload_sha256: str
    owner_principal: str
    inner_inventory_claim_id: str
    inner_inventory_claim_payload_sha256: str
    claimed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.claim_id.startswith("av1vtier2selclaim3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim ID is invalid"
            )
        if not self.plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim plan ID is invalid"
            )
        if not self.request_id.startswith("av1vtier2selrequest3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim request ID is invalid"
            )
        if not self.grant_id.startswith("av1vtier2selgrant3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim grant ID is invalid"
            )
        if not self.inner_inventory_claim_id.startswith("av1vtier2invreadclaim3_"):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim inner inventory claim ID is invalid"
            )
        if not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim owner principal is invalid"
            )
        for value, label in (
            (self.plan_payload_sha256, "plan digest"),
            (self.request_payload_sha256, "request digest"),
            (self.grant_payload_sha256, "grant digest"),
            (self.inner_inventory_claim_payload_sha256, "inner inventory claim digest"),
            (self.payload_sha256, "payload digest"),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier2SelectionAuthorizationError(
                    f"AV1 v3 Tier 2 selection claim {label} is invalid"
                )
        _parse_timestamp(self.claimed_at, "claim timestamp", canonical=True)
        semantic = self.semantic_payload()
        if self.claim_id != av1_validation_v3_id("tier2selclaim", semantic):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"claim_id": self.claim_id, **semantic}
        ):
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection claim digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "single_selection_claimed": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
            **_false_authority_fields(),
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "grant_id": self.grant_id,
            "grant_payload_sha256": self.grant_payload_sha256,
            "owner_principal": self.owner_principal,
            "inner_inventory_claim_id": self.inner_inventory_claim_id,
            "inner_inventory_claim_payload_sha256": self.inner_inventory_claim_payload_sha256,
            "claimed_at": self.claimed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_owner_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        plan: AV1ValidationV3QualificationPlan,
        inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
        request: AV1ValidationV3Tier2SelectionRequest,
        grant: AV1ValidationV3Tier2SelectionGrant,
        inner_inventory_claim: AV1ValidationV3Tier2InventoryReadClaim,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_selection_claim_active(
            protocol, plan, inner_inventory_request, request, grant,
            inner_inventory_claim, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_selection_claim",
            "gate": "A0",
            "tier": "tier2",
            "single_selection_claimed": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
            "owner_principal": self.owner_principal,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionContext:
    plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier2SelectionRequest
    grant: AV1ValidationV3Tier2SelectionGrant
    claim: AV1ValidationV3Tier2SelectionClaim

    def __post_init__(self) -> None:
        if type(self.plan) is not AV1ValidationV3QualificationPlan:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection context plan is invalid"
            )
        if type(self.request) is not AV1ValidationV3Tier2SelectionRequest:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection context request is invalid"
            )
        if type(self.grant) is not AV1ValidationV3Tier2SelectionGrant:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection context grant is invalid"
            )
        if type(self.claim) is not AV1ValidationV3Tier2SelectionClaim:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                "AV1 v3 Tier 2 selection context claim is invalid"
            )


def build_av1_validation_v3_tier2_selection_request(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    requested_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier2SelectionRequest:
    assert_av1_validation_v3_qualification_plan_active(
        protocol, plan, as_of=requested_at
    )
    if _parse_timestamp(valid_until, "request expiration") > _parse_timestamp(
        plan.valid_until, "plan expiration"
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request cannot outlive its qualification plan"
        )
    if _parse_timestamp(valid_until, "request expiration") > _parse_timestamp(
        inner_inventory_request.valid_until, "inner inventory request expiration"
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request cannot outlive its inner inventory request"
        )
    tier2_scope = av1_validation_v3_tier2_inventory_scope_digest(protocol)
    projection_contract = av1_validation_v3_tier2_inventory_projection_contract_digest()
    selection_contract = _compute_selection_contract_digest(protocol)
    semantic = _request_semantic(
        protocol=protocol,
        plan=plan,
        tier2_scope_digest=tier2_scope,
        inventory_projection_contract_digest=projection_contract,
        selection_contract_digest=selection_contract,
        inner_inventory_request_id=inner_inventory_request.request_id,
        inner_inventory_request_payload_sha256=inner_inventory_request.payload_sha256,
        requested_at=requested_at,
        valid_until=valid_until,
    )
    request_id = av1_validation_v3_id("tier2selrequest", semantic)
    return AV1ValidationV3Tier2SelectionRequest(
        request_id=request_id,
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        qualification_plan_id=plan.plan_id,
        qualification_plan_payload_sha256=plan.payload_sha256,
        qualification_key_id=plan.qualification_key_id,
        eligibility_predicate_sha256=plan.eligibility_predicate_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        tier2_scope_digest=tier2_scope,
        inventory_projection_contract_digest=projection_contract,
        selection_contract_digest=selection_contract,
        inner_inventory_request_id=inner_inventory_request.request_id,
        inner_inventory_request_payload_sha256=inner_inventory_request.payload_sha256,
        requested_at=requested_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"request_id": request_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_selection_request_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    request: AV1ValidationV3Tier2SelectionRequest,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_qualification_plan_active(protocol, plan, as_of=as_of)
    expected_request = build_av1_validation_v3_tier2_selection_request(
        protocol=protocol,
        plan=plan,
        inner_inventory_request=inner_inventory_request,
        requested_at=request.requested_at,
        valid_until=request.valid_until,
    )
    if request != expected_request:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request is not bound to its plan"
        )
    checked_at = _parse_timestamp(
        as_of,
        "request active-check timestamp",
        canonical=True,
    )
    if checked_at < _parse_timestamp(
        request.requested_at, "request timestamp"
    ) or checked_at >= _parse_timestamp(request.valid_until, "request expiration"):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request is not active"
        )


def build_av1_validation_v3_tier2_selection_grant(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    request: AV1ValidationV3Tier2SelectionRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier2SelectionGrant:
    assert_av1_validation_v3_tier2_selection_request_active(
        protocol, plan, inner_inventory_request, request, as_of=authorized_at
    )
    if _parse_timestamp(valid_until, "grant expiration") > _parse_timestamp(
        request.valid_until, "request expiration"
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant cannot outlive its request"
        )
    semantic = _grant_semantic(
        request=request,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
    )
    grant_id = av1_validation_v3_id("tier2selgrant", semantic)
    return AV1ValidationV3Tier2SelectionGrant(
        grant_id=grant_id,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"grant_id": grant_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_selection_grant_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    request: AV1ValidationV3Tier2SelectionRequest,
    grant: AV1ValidationV3Tier2SelectionGrant,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_selection_request_active(
        protocol, plan, inner_inventory_request, request, as_of=as_of
    )
    if (
        grant.request_id != request.request_id
        or grant.request_payload_sha256 != request.payload_sha256
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant is not bound to its request"
        )
    checked_at = _parse_timestamp(
        as_of,
        "grant active-check timestamp",
        canonical=True,
    )
    if checked_at < _parse_timestamp(
        grant.authorized_at, "grant timestamp"
    ) or checked_at >= _parse_timestamp(grant.valid_until, "grant expiration"):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant is not active"
        )


def build_av1_validation_v3_tier2_selection_claim(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    request: AV1ValidationV3Tier2SelectionRequest,
    grant: AV1ValidationV3Tier2SelectionGrant,
    inner_inventory_claim: AV1ValidationV3Tier2InventoryReadClaim,
    claimed_at: str,
) -> AV1ValidationV3Tier2SelectionClaim:
    assert_av1_validation_v3_tier2_selection_grant_active(
        protocol, plan, inner_inventory_request, request, grant, as_of=claimed_at
    )
    semantic = _claim_semantic(
        plan=plan,
        request=request,
        grant=grant,
        inner_inventory_claim=inner_inventory_claim,
        claimed_at=claimed_at,
    )
    claim_id = av1_validation_v3_id("tier2selclaim", semantic)
    return AV1ValidationV3Tier2SelectionClaim(
        claim_id=claim_id,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        grant_id=grant.grant_id,
        grant_payload_sha256=grant.payload_sha256,
        owner_principal=grant.owner_principal,
        inner_inventory_claim_id=inner_inventory_claim.claim_id,
        inner_inventory_claim_payload_sha256=inner_inventory_claim.payload_sha256,
        claimed_at=claimed_at,
        payload_sha256=_payload_sha256({"claim_id": claim_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_selection_claim_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    inner_inventory_request: AV1ValidationV3Tier2InventoryReadRequest,
    request: AV1ValidationV3Tier2SelectionRequest,
    grant: AV1ValidationV3Tier2SelectionGrant,
    inner_inventory_claim: AV1ValidationV3Tier2InventoryReadClaim,
    claim: AV1ValidationV3Tier2SelectionClaim,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_selection_grant_active(
        protocol, plan, inner_inventory_request, request, grant, as_of=as_of
    )
    if (
        claim.plan_id != plan.plan_id
        or claim.plan_payload_sha256 != plan.payload_sha256
        or claim.request_id != request.request_id
        or claim.request_payload_sha256 != request.payload_sha256
        or claim.grant_id != grant.grant_id
        or claim.grant_payload_sha256 != grant.payload_sha256
        or claim.owner_principal != grant.owner_principal
        or claim.inner_inventory_claim_id != inner_inventory_claim.claim_id
        or claim.inner_inventory_claim_payload_sha256 != inner_inventory_claim.payload_sha256
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim is not bound to its grant chain"
        )
    claimed_at = _parse_timestamp(claim.claimed_at, "claim timestamp")
    checked_at = _parse_timestamp(
        as_of,
        "claim active-check timestamp",
        canonical=True,
    )
    if claimed_at < _parse_timestamp(
        grant.authorized_at, "grant timestamp"
    ) or claimed_at >= _parse_timestamp(grant.valid_until, "grant expiration"):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim was not made within its grant window"
        )
    if checked_at < claimed_at:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim is not active"
        )


def assert_av1_validation_v3_tier2_selection_context(
    protocol: AV1ValidationProtocolV3,
    selection_context: AV1ValidationV3Tier2SelectionContext,
    inventory_context: AV1ValidationV3Tier2InventoryReadContext,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_selection_claim_active(
        protocol,
        selection_context.plan,
        inventory_context.request,
        selection_context.request,
        selection_context.grant,
        inventory_context.claim,
        selection_context.claim,
        as_of=as_of,
    )
    assert_av1_validation_v3_tier2_inventory_read_claim_active(
        protocol,
        inventory_context.plan,
        inventory_context.request,
        inventory_context.grant,
        inventory_context.claim,
        as_of=as_of,
    )
    if (
        selection_context.grant.owner_principal
        != inventory_context.claim.owner_principal
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection context owner does not match inventory context"
        )
    if (
        selection_context.request.qualification_plan_id != inventory_context.claim.plan_id
        or selection_context.request.qualification_plan_payload_sha256
        != inventory_context.claim.plan_payload_sha256
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection context plan does not match inventory context"
        )
    if (
        selection_context.request.inner_inventory_request_id
        != inventory_context.request.request_id
        or selection_context.request.inner_inventory_request_payload_sha256
        != inventory_context.request.payload_sha256
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request is not bound to its inner inventory request"
        )
    if (
        selection_context.claim.inner_inventory_claim_id
        != inventory_context.claim.claim_id
        or selection_context.claim.inner_inventory_claim_payload_sha256
        != inventory_context.claim.payload_sha256
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim is not bound to its inner inventory claim"
        )


def serialize_av1_validation_v3_tier2_selection_request(
    request: AV1ValidationV3Tier2SelectionRequest,
) -> bytes:
    return canonical_json_bytes(request.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier2_selection_grant(
    grant: AV1ValidationV3Tier2SelectionGrant,
) -> bytes:
    return canonical_json_bytes(grant.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier2_selection_claim(
    claim: AV1ValidationV3Tier2SelectionClaim,
) -> bytes:
    return canonical_json_bytes(claim.to_payload()) + b"\n"


def deserialize_av1_validation_v3_tier2_selection_request(
    raw: bytes,
) -> AV1ValidationV3Tier2SelectionRequest:
    try:
        request = av1_validation_v3_tier2_selection_request_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_request(request):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request bytes are not canonical"
        )
    return request


def deserialize_av1_validation_v3_tier2_selection_grant(
    raw: bytes,
) -> AV1ValidationV3Tier2SelectionGrant:
    try:
        grant = av1_validation_v3_tier2_selection_grant_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_grant(grant):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant bytes are not canonical"
        )
    return grant


def deserialize_av1_validation_v3_tier2_selection_claim(
    raw: bytes,
) -> AV1ValidationV3Tier2SelectionClaim:
    try:
        claim = av1_validation_v3_tier2_selection_claim_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_claim(claim):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim bytes are not canonical"
        )
    return claim


def av1_validation_v3_tier2_selection_request_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2SelectionRequest:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "request_id",
            "schema",
            "schema_version",
            "contract_version",
            "authority",
            "gate",
            "tier",
            "request_state",
            "single_selection_requested",
            "tier2_selection_execution_authorized",
            "qualification_key_read_authorized",
            "execution_requires_separate_owner_authorization",
            *AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
            "protocol_id",
            "protocol_payload_sha256",
            "qualification_plan_id",
            "qualification_plan_payload_sha256",
            "qualification_key_id",
            "eligibility_predicate_sha256",
            "repository_commit",
            "repository_tree",
            "config_sha256",
            "tier2_scope_digest",
            "inventory_projection_contract_digest",
            "selection_contract_digest",
            "inner_inventory_request_id",
            "inner_inventory_request_payload_sha256",
            "requested_at",
            "valid_until",
            "payload_sha256",
        },
        "selection request",
    )
    _require_request_constants(value)
    request = AV1ValidationV3Tier2SelectionRequest(
        request_id=_string_field(value, "request_id"),
        protocol_id=_string_field(value, "protocol_id"),
        protocol_payload_sha256=_string_field(value, "protocol_payload_sha256"),
        qualification_plan_id=_string_field(value, "qualification_plan_id"),
        qualification_plan_payload_sha256=_string_field(
            value, "qualification_plan_payload_sha256"
        ),
        qualification_key_id=_string_field(value, "qualification_key_id"),
        eligibility_predicate_sha256=_string_field(
            value, "eligibility_predicate_sha256"
        ),
        repository_commit=_string_field(value, "repository_commit"),
        repository_tree=_string_field(value, "repository_tree"),
        config_sha256=_string_field(value, "config_sha256"),
        tier2_scope_digest=_string_field(value, "tier2_scope_digest"),
        inventory_projection_contract_digest=_string_field(
            value, "inventory_projection_contract_digest"
        ),
        selection_contract_digest=_string_field(value, "selection_contract_digest"),
        inner_inventory_request_id=_string_field(
            value, "inner_inventory_request_id"
        ),
        inner_inventory_request_payload_sha256=_string_field(
            value, "inner_inventory_request_payload_sha256"
        ),
        requested_at=_string_field(value, "requested_at"),
        valid_until=_string_field(value, "valid_until"),
        payload_sha256=_string_field(value, "payload_sha256"),
    )
    if value != request.to_payload():
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection request payload is not exact"
        )
    return request


def av1_validation_v3_tier2_selection_grant_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2SelectionGrant:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "grant_id",
            "schema",
            "schema_version",
            "contract_version",
            "authority",
            "gate",
            "tier",
            "single_selection_authorized",
            "tier2_selection_execution_authorized",
            "qualification_key_read_authorized",
            *AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
            "request_id",
            "request_payload_sha256",
            "owner_principal",
            "authorized_at",
            "valid_until",
            "payload_sha256",
        },
        "selection grant",
    )
    _require_grant_constants(value)
    grant = AV1ValidationV3Tier2SelectionGrant(
        grant_id=str(value.get("grant_id") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        authorized_at=str(value.get("authorized_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != grant.to_payload():
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection grant payload is not exact"
        )
    return grant


def av1_validation_v3_tier2_selection_claim_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2SelectionClaim:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "claim_id",
            "schema",
            "schema_version",
            "contract_version",
            "authority",
            "gate",
            "tier",
            "single_selection_claimed",
            "tier2_selection_execution_authorized",
            "qualification_key_read_authorized",
            *AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
            "plan_id",
            "plan_payload_sha256",
            "request_id",
            "request_payload_sha256",
            "grant_id",
            "grant_payload_sha256",
            "owner_principal",
            "inner_inventory_claim_id",
            "inner_inventory_claim_payload_sha256",
            "claimed_at",
            "payload_sha256",
        },
        "selection claim",
    )
    _require_claim_constants(value)
    claim = AV1ValidationV3Tier2SelectionClaim(
        claim_id=str(value.get("claim_id") or ""),
        plan_id=str(value.get("plan_id") or ""),
        plan_payload_sha256=str(value.get("plan_payload_sha256") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        grant_id=str(value.get("grant_id") or ""),
        grant_payload_sha256=str(value.get("grant_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        inner_inventory_claim_id=str(value.get("inner_inventory_claim_id") or ""),
        inner_inventory_claim_payload_sha256=str(
            value.get("inner_inventory_claim_payload_sha256") or ""
        ),
        claimed_at=str(value.get("claimed_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != claim.to_payload():
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection claim payload is not exact"
        )
    return claim


def _request_semantic(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    tier2_scope_digest: str,
    inventory_projection_contract_digest: str,
    selection_contract_digest: str,
    inner_inventory_request_id: str,
    inner_inventory_request_payload_sha256: str,
    requested_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "request_state": "owner_action_required",
        "single_selection_requested": True,
        "tier2_selection_execution_authorized": False,
        "qualification_key_read_authorized": False,
        "execution_requires_separate_owner_authorization": True,
        **_false_authority_fields(),
        "protocol_id": protocol.protocol_id,
        "protocol_payload_sha256": protocol.payload_sha256,
        "qualification_plan_id": plan.plan_id,
        "qualification_plan_payload_sha256": plan.payload_sha256,
        "qualification_key_id": plan.qualification_key_id,
        "eligibility_predicate_sha256": plan.eligibility_predicate_sha256,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "tier2_scope_digest": tier2_scope_digest,
        "inventory_projection_contract_digest": inventory_projection_contract_digest,
        "selection_contract_digest": selection_contract_digest,
        "inner_inventory_request_id": inner_inventory_request_id,
        "inner_inventory_request_payload_sha256": inner_inventory_request_payload_sha256,
        "requested_at": requested_at,
        "valid_until": valid_until,
    }


def _grant_semantic(
    *,
    request: AV1ValidationV3Tier2SelectionRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_selection_authorized": True,
        "tier2_selection_execution_authorized": True,
        "qualification_key_read_authorized": True,
        **_false_authority_fields(),
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "owner_principal": owner_principal,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
    }


def _claim_semantic(
    *,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2SelectionRequest,
    grant: AV1ValidationV3Tier2SelectionGrant,
    inner_inventory_claim: AV1ValidationV3Tier2InventoryReadClaim,
    claimed_at: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_selection_claimed": True,
        "tier2_selection_execution_authorized": True,
        "qualification_key_read_authorized": True,
        **_false_authority_fields(),
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "grant_id": grant.grant_id,
        "grant_payload_sha256": grant.payload_sha256,
        "owner_principal": grant.owner_principal,
        "inner_inventory_claim_id": inner_inventory_claim.claim_id,
        "inner_inventory_claim_payload_sha256": inner_inventory_claim.payload_sha256,
        "claimed_at": claimed_at,
    }


def _compute_selection_contract_digest(protocol: AV1ValidationProtocolV3) -> str:
    contract = {
        "selection_algorithm": "hmac_sha256_lowest_v1",
        "ranking_domain": av1_validation_v3_hmac_domain("tier2:qualification-rank"),
        "single_key_read": True,
        "single_selection_per_stratum": True,
        "strata_names": [stratum.name for stratum in protocol.tier2_strata],
        "strata_slot_expectations": {
            stratum.name: stratum.source_slots for stratum in protocol.tier2_strata
        },
    }
    return _domain_payload_sha256(
        AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_DOMAIN, contract
    )


def _false_authority_fields() -> dict[str, bool]:
    return dict.fromkeys(
        AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
        False,
    )


def _require_request_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "request_state": "owner_action_required",
        "single_selection_requested": True,
        "tier2_selection_execution_authorized": False,
        "qualification_key_read_authorized": False,
        "execution_requires_separate_owner_authorization": True,
        **_false_authority_fields(),
    }
    _check_constants(value, constants, "request")


def _require_grant_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_selection_authorized": True,
        "tier2_selection_execution_authorized": True,
        "qualification_key_read_authorized": True,
        **_false_authority_fields(),
    }
    _check_constants(value, constants, "grant")


def _require_claim_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_selection_claimed": True,
        "tier2_selection_execution_authorized": True,
        "qualification_key_read_authorized": True,
        **_false_authority_fields(),
    }
    _check_constants(value, constants, "claim")


def _check_constants(
    value: Mapping[str, Any],
    constants: dict[str, object],
    label: str,
) -> None:
    for key, expected in constants.items():
        actual = value.get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise AV1ValidationV3Tier2SelectionAuthorizationError(
                f"AV1 v3 Tier 2 selection {label} constants are invalid"
            )


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            f"AV1 v3 Tier 2 selection {label} keys are invalid"
        )


def _string_field(value: Mapping[str, Any], field_name: str) -> str:
    field = value.get(field_name)
    if not isinstance(field, str):
        return ""
    return field


def _domain_payload_sha256(domain: str, payload: object) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({"domain": domain, "payload": payload})
    ).hexdigest()
    return f"sha256:{digest}"


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _parse_timestamp(value: str, label: str, *, canonical: bool = False) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            f"AV1 v3 Tier 2 selection {label} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            f"AV1 v3 Tier 2 selection {label} must include a timezone"
        )
    normalized = parsed.astimezone(UTC)
    if canonical and (
        normalized.microsecond != 0
        or not value.endswith("Z")
        or normalized.isoformat(timespec="seconds").replace("+00:00", "Z") != value
    ):
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            f"AV1 v3 Tier 2 selection {label} must use canonical UTC"
        )
    return normalized


def _assert_public_summary_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationV3Tier2SelectionAuthorizationError(
            "AV1 v3 Tier 2 selection authorization public summary contains private data"
        ) from exc
