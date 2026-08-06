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
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_inventory_read_request"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_inventory_read_grant"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_inventory_read_claim"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_CONTRACT_VERSION = "av1vt2irr1"
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_CONTRACT_VERSION = "av1vt2irg1"
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_CONTRACT_VERSION = "av1vt2irc1"
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY = "av1_v3_tier2_private_inventory_read"
AV1_VALIDATION_V3_TIER2_INVENTORY_SCOPE_DIGEST_DOMAIN = (
    "mediaforce:av1:v3:tier2-inventory-read-scope:v1"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_PROJECTION_CONTRACT_DOMAIN = (
    "mediaforce:av1:v3:tier2-inventory-projection-contract:v1"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_SOURCE_FINGERPRINT_DOMAIN = (
    "mediaforce:av1:v3:tier2-qualification-source:v1"
)

_OWNER_PRINCIPAL_RE = re.compile(r"owner-[a-z0-9]{8,32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_KEY_ID_RE = re.compile(r"av1vqkey3_[0-9a-f]{32}\Z")

AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS = (
    "tier1_execution_authorized",
    "tier1_coverage_eligible",
    "tier2_execution_authorized",
    "tier2_selection_execution_authorized",
    "runtime_execution_authorized",
    "qualification_execution_authorized",
    "qualification_complete",
    "path_matrix_coverage_claimed",
    "evidence_creation_authorized",
    "evidence_eligible",
    "empirical_authority_conferred",
    "derivation_authorized",
    "holdout_authorized",
    "publication_authorized",
    "activation_authorized",
    "public_bundle_activation_allowed",
    "media_library_read_authorized",
    "media_read_authorized",
    "key_creation_authorized",
    "qualification_key_read_authorized",
    "private_inventory_serialization_authorized",
    "retry_authorized",
)

AV1_VALIDATION_V3_TIER2_INVENTORY_EXCLUSION_COUNTER_FIELDS = tuple(
    sorted([
        "ambiguous_trait_count",
        "duplicate_source_identity_row_count",
        "incomplete_quality_contract_count",
        "incompatible_evidence_count",
        "infeasible_stream_budget_count",
        "malformed_identity_count",
        "missing_evidence_summary_count",
        "non_balanced_intent_count",
        "non_tier2_trait_count",
        "powered_candidate_cell_overlap_count",
        "projection_error_count",
        "unconfirmed_intent_count",
    ])
)


class AV1ValidationV3Tier2InventoryAuthorizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadRequest:
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
    requested_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.request_id.startswith("av1vtier2invreadrequest3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request ID is invalid"
            )
        if not self.protocol_id.startswith("av1vprotocol3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request protocol ID is invalid"
            )
        if not self.qualification_plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request plan ID is invalid"
            )
        if not _KEY_ID_RE.fullmatch(self.qualification_key_id):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request key ID is invalid"
            )
        for value, label in (
            (self.protocol_payload_sha256, "protocol digest"),
            (self.qualification_plan_payload_sha256, "plan digest"),
            (self.eligibility_predicate_sha256, "eligibility predicate digest"),
            (self.config_sha256, "config digest"),
            (self.tier2_scope_digest, "Tier 2 scope digest"),
            (self.inventory_projection_contract_digest, "inventory projection contract digest"),
            (self.payload_sha256, "payload digest"),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier2InventoryAuthorizationError(
                    f"AV1 v3 Tier 2 inventory read request {label} is invalid"
                )
        for value, label in (
            (self.repository_commit, "repository commit"),
            (self.repository_tree, "repository tree"),
        ):
            if not _GIT_OBJECT_ID_RE.fullmatch(value):
                raise AV1ValidationV3Tier2InventoryAuthorizationError(
                    f"AV1 v3 Tier 2 inventory read request {label} is invalid"
                )
        requested_at = _parse_timestamp(
            self.requested_at, "request timestamp", canonical=True
        )
        valid_until = _parse_timestamp(
            self.valid_until, "request expiration", canonical=True
        )
        if valid_until <= requested_at:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.request_id != av1_validation_v3_id("tier2invreadrequest", semantic):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"request_id": self.request_id, **semantic}
        ):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read request digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "request_state": "owner_action_required",
            "single_read_requested": True,
            "private_inventory_read_authorized": False,
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
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_inventory_read_request_active(
            protocol, plan, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_inventory_read_request",
            "gate": "A0",
            "tier": "tier2",
            "request_state": "owner_action_required",
            "single_read_requested": True,
            "private_inventory_read_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadGrant:
    grant_id: str
    request_id: str
    request_payload_sha256: str
    owner_principal: str
    authorized_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.grant_id.startswith("av1vtier2invreadgrant3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant ID is invalid"
            )
        if not self.request_id.startswith("av1vtier2invreadrequest3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant request ID is invalid"
            )
        if not _SHA256_RE.fullmatch(self.request_payload_sha256):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant request digest is invalid"
            )
        if not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant owner principal is invalid"
            )
        authorized_at = _parse_timestamp(
            self.authorized_at, "grant timestamp", canonical=True
        )
        valid_until = _parse_timestamp(
            self.valid_until, "grant expiration", canonical=True
        )
        if valid_until <= authorized_at:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant expiration is invalid"
            )
        semantic = self.semantic_payload()
        if self.grant_id != av1_validation_v3_id("tier2invreadgrant", semantic):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"grant_id": self.grant_id, **semantic}
        ):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read grant digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "single_read_authorized": True,
            "private_inventory_read_authorized": True,
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
        request: AV1ValidationV3Tier2InventoryReadRequest,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_inventory_read_grant_active(
            protocol, plan, request, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_inventory_read_grant",
            "gate": "A0",
            "tier": "tier2",
            "single_read_authorized": True,
            "private_inventory_read_authorized": True,
            "owner_principal": self.owner_principal,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadClaim:
    claim_id: str
    plan_id: str
    plan_payload_sha256: str
    request_id: str
    request_payload_sha256: str
    grant_id: str
    grant_payload_sha256: str
    owner_principal: str
    claimed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.claim_id.startswith("av1vtier2invreadclaim3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim ID is invalid"
            )
        if not self.plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim plan ID is invalid"
            )
        if not self.request_id.startswith("av1vtier2invreadrequest3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim request ID is invalid"
            )
        if not self.grant_id.startswith("av1vtier2invreadgrant3_"):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim grant ID is invalid"
            )
        if not _OWNER_PRINCIPAL_RE.fullmatch(self.owner_principal):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim owner principal is invalid"
            )
        for value, label in (
            (self.plan_payload_sha256, "plan digest"),
            (self.request_payload_sha256, "request digest"),
            (self.grant_payload_sha256, "grant digest"),
            (self.payload_sha256, "payload digest"),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier2InventoryAuthorizationError(
                    f"AV1 v3 Tier 2 inventory read claim {label} is invalid"
                )
        _parse_timestamp(self.claimed_at, "claim timestamp", canonical=True)
        semantic = self.semantic_payload()
        if self.claim_id != av1_validation_v3_id("tier2invreadclaim", semantic):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"claim_id": self.claim_id, **semantic}
        ):
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read claim digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
            "gate": "A0",
            "tier": "tier2",
            "single_read_claimed": True,
            "private_inventory_read_authorized": True,
            **_false_authority_fields(),
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "grant_id": self.grant_id,
            "grant_payload_sha256": self.grant_payload_sha256,
            "owner_principal": self.owner_principal,
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
        request: AV1ValidationV3Tier2InventoryReadRequest,
        grant: AV1ValidationV3Tier2InventoryReadGrant,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_inventory_read_claim_active(
            protocol, plan, request, grant, self, as_of=as_of
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_inventory_read_claim",
            "gate": "A0",
            "tier": "tier2",
            "single_read_claimed": True,
            "private_inventory_read_authorized": True,
            "owner_principal": self.owner_principal,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadContext:
    plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier2InventoryReadRequest
    grant: AV1ValidationV3Tier2InventoryReadGrant
    claim: AV1ValidationV3Tier2InventoryReadClaim

    def __post_init__(self) -> None:
        if type(self.plan) is not AV1ValidationV3QualificationPlan:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read context plan is invalid"
            )
        if type(self.request) is not AV1ValidationV3Tier2InventoryReadRequest:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read context request is invalid"
            )
        if type(self.grant) is not AV1ValidationV3Tier2InventoryReadGrant:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read context grant is invalid"
            )
        if type(self.claim) is not AV1ValidationV3Tier2InventoryReadClaim:
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                "AV1 v3 Tier 2 inventory read context claim is invalid"
            )

    def to_owner_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        as_of: str,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_inventory_read_context(
            protocol,
            self,
            as_of=as_of,
        )
        summary: dict[str, Any] = {
            "artifact_kind": "tier2_inventory_read_context",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.request.request_id,
            "grant_id": self.grant.grant_id,
            "claim_id": self.claim.claim_id,
            "single_read_claimed": True,
            "private_inventory_read_authorized": True,
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(summary)
        return summary


def build_av1_validation_v3_tier2_inventory_read_request(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    requested_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier2InventoryReadRequest:
    assert_av1_validation_v3_qualification_plan_active(
        protocol, plan, as_of=requested_at
    )
    if _parse_timestamp(valid_until, "request expiration") > _parse_timestamp(
        plan.valid_until, "plan expiration"
    ):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request cannot outlive its qualification plan"
        )
    tier2_scope_digest = _compute_tier2_scope_digest(protocol)
    projection_contract_digest = _compute_inventory_projection_contract_digest()
    semantic = _request_semantic(
        protocol=protocol,
        plan=plan,
        tier2_scope_digest=tier2_scope_digest,
        inventory_projection_contract_digest=projection_contract_digest,
        requested_at=requested_at,
        valid_until=valid_until,
    )
    request_id = av1_validation_v3_id("tier2invreadrequest", semantic)
    return AV1ValidationV3Tier2InventoryReadRequest(
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
        tier2_scope_digest=tier2_scope_digest,
        inventory_projection_contract_digest=projection_contract_digest,
        requested_at=requested_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"request_id": request_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_inventory_read_request_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_qualification_plan_active(protocol, plan, as_of=as_of)
    expected_request = build_av1_validation_v3_tier2_inventory_read_request(
        protocol=protocol,
        plan=plan,
        requested_at=request.requested_at,
        valid_until=request.valid_until,
    )
    if request != expected_request:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request is not bound to its plan"
        )
    checked_at = _parse_timestamp(
        as_of,
        "request active-check timestamp",
        canonical=True,
    )
    if checked_at < _parse_timestamp(
        request.requested_at, "request timestamp"
    ) or checked_at >= _parse_timestamp(request.valid_until, "request expiration"):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request is not active"
        )


def build_av1_validation_v3_tier2_inventory_read_grant(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier2InventoryReadGrant:
    assert_av1_validation_v3_tier2_inventory_read_request_active(
        protocol, plan, request, as_of=authorized_at
    )
    if _parse_timestamp(valid_until, "grant expiration") > _parse_timestamp(
        request.valid_until, "request expiration"
    ):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant cannot outlive its request"
        )
    semantic = _grant_semantic(
        request=request,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
    )
    grant_id = av1_validation_v3_id("tier2invreadgrant", semantic)
    return AV1ValidationV3Tier2InventoryReadGrant(
        grant_id=grant_id,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"grant_id": grant_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_inventory_read_grant_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    grant: AV1ValidationV3Tier2InventoryReadGrant,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_inventory_read_request_active(
        protocol, plan, request, as_of=as_of
    )
    if (
        grant.request_id != request.request_id
        or grant.request_payload_sha256 != request.payload_sha256
    ):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant is not bound to its request"
        )
    checked_at = _parse_timestamp(
        as_of,
        "grant active-check timestamp",
        canonical=True,
    )
    if checked_at < _parse_timestamp(
        grant.authorized_at, "grant timestamp"
    ) or checked_at >= _parse_timestamp(grant.valid_until, "grant expiration"):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant is not active"
        )


def build_av1_validation_v3_tier2_inventory_read_claim(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    grant: AV1ValidationV3Tier2InventoryReadGrant,
    claimed_at: str,
) -> AV1ValidationV3Tier2InventoryReadClaim:
    assert_av1_validation_v3_tier2_inventory_read_grant_active(
        protocol, plan, request, grant, as_of=claimed_at
    )
    semantic = _claim_semantic(
        plan=plan,
        request=request,
        grant=grant,
        claimed_at=claimed_at,
    )
    claim_id = av1_validation_v3_id("tier2invreadclaim", semantic)
    return AV1ValidationV3Tier2InventoryReadClaim(
        claim_id=claim_id,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        grant_id=grant.grant_id,
        grant_payload_sha256=grant.payload_sha256,
        owner_principal=grant.owner_principal,
        claimed_at=claimed_at,
        payload_sha256=_payload_sha256({"claim_id": claim_id, **semantic}),
    )


def assert_av1_validation_v3_tier2_inventory_read_claim_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    grant: AV1ValidationV3Tier2InventoryReadGrant,
    claim: AV1ValidationV3Tier2InventoryReadClaim,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_inventory_read_grant_active(
        protocol, plan, request, grant, as_of=as_of
    )
    if (
        claim.plan_id != plan.plan_id
        or claim.plan_payload_sha256 != plan.payload_sha256
        or claim.request_id != request.request_id
        or claim.request_payload_sha256 != request.payload_sha256
        or claim.grant_id != grant.grant_id
        or claim.grant_payload_sha256 != grant.payload_sha256
        or claim.owner_principal != grant.owner_principal
    ):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim is not bound to its grant chain"
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
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim was not made within its grant window"
        )
    if checked_at < claimed_at:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim is not active"
        )


def assert_av1_validation_v3_tier2_inventory_read_context(
    protocol: AV1ValidationProtocolV3,
    context: AV1ValidationV3Tier2InventoryReadContext,
    *,
    as_of: str,
) -> None:
    assert_av1_validation_v3_tier2_inventory_read_claim_active(
        protocol,
        context.plan,
        context.request,
        context.grant,
        context.claim,
        as_of=as_of,
    )


def serialize_av1_validation_v3_tier2_inventory_read_request(
    request: AV1ValidationV3Tier2InventoryReadRequest,
) -> bytes:
    return canonical_json_bytes(request.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier2_inventory_read_grant(
    grant: AV1ValidationV3Tier2InventoryReadGrant,
) -> bytes:
    return canonical_json_bytes(grant.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier2_inventory_read_claim(
    claim: AV1ValidationV3Tier2InventoryReadClaim,
) -> bytes:
    return canonical_json_bytes(claim.to_payload()) + b"\n"


def deserialize_av1_validation_v3_tier2_inventory_read_request(
    raw: bytes,
) -> AV1ValidationV3Tier2InventoryReadRequest:
    try:
        request = av1_validation_v3_tier2_inventory_read_request_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_request(request):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request bytes are not canonical"
        )
    return request


def deserialize_av1_validation_v3_tier2_inventory_read_grant(
    raw: bytes,
) -> AV1ValidationV3Tier2InventoryReadGrant:
    try:
        grant = av1_validation_v3_tier2_inventory_read_grant_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_grant(grant):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant bytes are not canonical"
        )
    return grant


def deserialize_av1_validation_v3_tier2_inventory_read_claim(
    raw: bytes,
) -> AV1ValidationV3Tier2InventoryReadClaim:
    try:
        claim = av1_validation_v3_tier2_inventory_read_claim_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_claim(claim):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim bytes are not canonical"
        )
    return claim


def av1_validation_v3_tier2_inventory_read_request_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2InventoryReadRequest:
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
            "single_read_requested",
            "private_inventory_read_authorized",
            "execution_requires_separate_owner_authorization",
            *AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
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
            "requested_at",
            "valid_until",
            "payload_sha256",
        },
        "inventory read request",
    )
    _require_request_constants(value)
    request = AV1ValidationV3Tier2InventoryReadRequest(
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
        requested_at=_string_field(value, "requested_at"),
        valid_until=_string_field(value, "valid_until"),
        payload_sha256=_string_field(value, "payload_sha256"),
    )
    if value != request.to_payload():
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read request payload is not exact"
        )
    return request


def av1_validation_v3_tier2_inventory_read_grant_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2InventoryReadGrant:
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
            "single_read_authorized",
            "private_inventory_read_authorized",
            *AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
            "request_id",
            "request_payload_sha256",
            "owner_principal",
            "authorized_at",
            "valid_until",
            "payload_sha256",
        },
        "inventory read grant",
    )
    _require_grant_constants(value)
    grant = AV1ValidationV3Tier2InventoryReadGrant(
        grant_id=str(value.get("grant_id") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        authorized_at=str(value.get("authorized_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != grant.to_payload():
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read grant payload is not exact"
        )
    return grant


def av1_validation_v3_tier2_inventory_read_claim_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2InventoryReadClaim:
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
            "single_read_claimed",
            "private_inventory_read_authorized",
            *AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
            "plan_id",
            "plan_payload_sha256",
            "request_id",
            "request_payload_sha256",
            "grant_id",
            "grant_payload_sha256",
            "owner_principal",
            "claimed_at",
            "payload_sha256",
        },
        "inventory read claim",
    )
    _require_claim_constants(value)
    claim = AV1ValidationV3Tier2InventoryReadClaim(
        claim_id=str(value.get("claim_id") or ""),
        plan_id=str(value.get("plan_id") or ""),
        plan_payload_sha256=str(value.get("plan_payload_sha256") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        grant_id=str(value.get("grant_id") or ""),
        grant_payload_sha256=str(value.get("grant_payload_sha256") or ""),
        owner_principal=str(value.get("owner_principal") or ""),
        claimed_at=str(value.get("claimed_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != claim.to_payload():
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory read claim payload is not exact"
        )
    return claim


def _request_semantic(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    tier2_scope_digest: str,
    inventory_projection_contract_digest: str,
    requested_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "request_state": "owner_action_required",
        "single_read_requested": True,
        "private_inventory_read_authorized": False,
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
        "requested_at": requested_at,
        "valid_until": valid_until,
    }


def _grant_semantic(
    *,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_read_authorized": True,
        "private_inventory_read_authorized": True,
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
    request: AV1ValidationV3Tier2InventoryReadRequest,
    grant: AV1ValidationV3Tier2InventoryReadGrant,
    claimed_at: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_read_claimed": True,
        "private_inventory_read_authorized": True,
        **_false_authority_fields(),
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "grant_id": grant.grant_id,
        "grant_payload_sha256": grant.payload_sha256,
        "owner_principal": grant.owner_principal,
        "claimed_at": claimed_at,
    }


def _compute_tier2_scope_digest(protocol: AV1ValidationProtocolV3) -> str:
    scope = {
        "strata": [stratum.to_payload() for stratum in protocol.tier2_strata],
        "adapter_excluded_powered_cells": [
            cell.to_payload() for cell in protocol.candidate_cells
        ],
        "total_tier2_slots": sum(
            stratum.source_slots for stratum in protocol.tier2_strata
        ),
        "strata_slot_expectations": {
            stratum.name: stratum.source_slots for stratum in protocol.tier2_strata
        },
        "ranking_algorithm": "hmac_sha256_lowest_v1",
        "ranking_domain": av1_validation_v3_hmac_domain("tier2:qualification-rank"),
    }
    return _domain_payload_sha256(
        AV1_VALIDATION_V3_TIER2_INVENTORY_SCOPE_DIGEST_DOMAIN, scope
    )


def _compute_inventory_projection_contract_digest() -> str:
    contract = {
        "source_fingerprint_domain": (
            AV1_VALIDATION_V3_TIER2_INVENTORY_SOURCE_FINGERPRINT_DOMAIN
        ),
        "source_identity_contract": "40_hex_content_version_fingerprint_strict",
        "dominant_evidence_cohort_selection": (
            "most_frequent_cohort_tie_break_compatibility_sort_key"
        ),
        "confirmed_balanced_intent_required": True,
        "exact_tier2_stratum_match_required": True,
        "powered_candidate_cell_overlap_excluded": True,
        "complete_quality_contract_required": True,
        "feasible_stream_budget_required": True,
        "no_candidate_cap": True,
        "duplicate_source_identity_rows_all_dropped": True,
        "fingerprint_collision_is_fatal": True,
        "pipeline_ready_semantics": "all_emitted_sources_always_true",
        "exclusion_counter_vocabulary": list(
            AV1_VALIDATION_V3_TIER2_INVENTORY_EXCLUSION_COUNTER_FIELDS
        ),
    }
    return _domain_payload_sha256(
        AV1_VALIDATION_V3_TIER2_INVENTORY_PROJECTION_CONTRACT_DOMAIN, contract
    )


def _false_authority_fields() -> dict[str, bool]:
    return dict.fromkeys(
        AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
        False,
    )


def _require_request_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "request_state": "owner_action_required",
        "single_read_requested": True,
        "private_inventory_read_authorized": False,
        "execution_requires_separate_owner_authorization": True,
        **_false_authority_fields(),
    }
    _check_constants(value, constants, "request")


def _require_grant_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_read_authorized": True,
        "private_inventory_read_authorized": True,
        **_false_authority_fields(),
    }
    _check_constants(value, constants, "grant")


def _require_claim_constants(value: Mapping[str, Any]) -> None:
    constants: dict[str, object] = {
        "schema": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_INVENTORY_READ_AUTHORITY,
        "gate": "A0",
        "tier": "tier2",
        "single_read_claimed": True,
        "private_inventory_read_authorized": True,
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
            raise AV1ValidationV3Tier2InventoryAuthorizationError(
                f"AV1 v3 Tier 2 inventory read {label} constants are invalid"
            )


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            f"AV1 v3 Tier 2 inventory read {label} keys are invalid"
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
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            f"AV1 v3 Tier 2 inventory read {label} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            f"AV1 v3 Tier 2 inventory read {label} must include a timezone"
        )
    normalized = parsed.astimezone(UTC)
    if canonical and (
        normalized.microsecond != 0
        or not value.endswith("Z")
        or normalized.isoformat(timespec="seconds").replace("+00:00", "Z") != value
    ):
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            f"AV1 v3 Tier 2 inventory read {label} must use canonical UTC"
        )
    return normalized


def _assert_public_summary_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationV3Tier2InventoryAuthorizationError(
            "AV1 v3 Tier 2 inventory authorization public summary contains private data"
        ) from exc


def av1_validation_v3_tier2_inventory_scope_digest(
    protocol: AV1ValidationProtocolV3,
) -> str:
    return _compute_tier2_scope_digest(protocol)


def av1_validation_v3_tier2_inventory_projection_contract_digest() -> str:
    return _compute_inventory_projection_contract_digest()
