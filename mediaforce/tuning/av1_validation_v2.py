from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    AV1_COLD_START_TRAITS,
    assert_av1_cold_start_public_payload_safe,
)
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN,
    AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS,
    AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT,
    AV1ColdStartValidationCriteriaV1,
)
from mediaforce.tuning.av1_trait_feasibility import AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION
from mediaforce.tuning.av1_trait_projection import AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION
from mediaforce.tuning.av1_validation_harness import AV1_VALIDATION_HARNESS_CONTRACT_VERSION


AV1_VALIDATION_V2_MANIFEST_SCHEMA = "mediaforce.av1_cold_start_validation_manifest"
AV1_VALIDATION_V2_MANIFEST_SCHEMA_VERSION = 2
AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA = "mediaforce.av1_cold_start_validation_eligibility"
AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA_VERSION = 1
AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA = (
    "mediaforce.av1_cold_start_derivation_authorization"
)
AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA_VERSION = 1
AV1_VALIDATION_V2_CONTRACT_VERSION = "acsv2"
AV1_VALIDATION_V2_PRODUCTION_PREDICTOR_CONTRACT_VERSION = "acsp1"
AV1_VALIDATION_V2_CANDIDATE_CONTRACT_VERSION = "acsvp1"
AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION = "acsvda1"
AV1_VALIDATION_V2_REGISTERED_AT = "2026-07-27T21:10:30Z"
AV1_VALIDATION_V2_VALID_UNTIL = "2027-01-27T21:10:30Z"
AV1_VALIDATION_V2_ELIGIBILITY_ATTESTATION_ID = "av1vfeasibility2_ebfb94fbc24d6cb9a0230a3214e04874"
AV1_VALIDATION_V2_ELIGIBILITY_PAYLOAD_SHA256 = (
    "sha256:bc29be3e2d1eb70e130223ce2a4aef8185f85d188fa580177a2e6acce254ddc9"
)
AV1_VALIDATION_V1_MANIFEST_ID = "av1vmanifest1_df59c782c1514402da6a26c06bfb8c40"
AV1_VALIDATION_V1_MANIFEST_PAYLOAD_SHA256 = (
    "sha256:ecf3de7047a1aaa769f31bb570fbb6096d7910c300ad659fe1aa225fb2223686"
)

AV1ValidationV2PlanMode = Literal["publication_candidate", "fallback_conformance"]
AV1ValidationV2Match = Literal["exact", "contains_all"]
AV1ValidationV2ExecutionOrder = Literal["baseline_only", "baseline_then_guided", "guided_then_baseline"]
AV1ValidationV2EligibilityState = Literal["eligible", "excluded"]

_SHA256_PREFIX = "sha256:"
_EXPECTED_CANDIDATES = {
    "darkness_balanced_candidate": ("darkness",),
    "motion_balanced_candidate": ("motion",),
}
_EXPECTED_FALLBACKS = {
    "grain_noise_risk_fallback": ("balanced", "contains_all", ("grain_noise",)),
    "mixed_risk_fallback": ("balanced", "contains_all", ("mixed",)),
    "perceptual_floor_directional_fallback": ("perceptual_floor", "contains_all", ("typical",)),
    "reference_directional_fallback": ("reference", "contains_all", ("typical",)),
    "texture_detail_risk_fallback": ("balanced", "contains_all", ("texture_detail",)),
    "transparent_directional_fallback": ("transparent", "contains_all", ("typical",)),
}
_EXPECTED_EXCLUSIONS = {
    "animation_balanced_candidate": "insufficient_inventory",
    "low_motion_dialogue_risk_fallback": "trait_projection_unavailable",
    "typical_live_action_balanced_candidate": "insufficient_inventory",
    "unknown_risk_fallback": "request_contract_unavailable",
}
_ELIGIBILITY_PRODUCER_BLOCKERS = frozenset({
    "low_motion_dialogue_analyzer_unavailable",
    "unknown_evidence_rejected_by_request_contract",
})
_ELIGIBILITY_EXCLUSION_REASONS = frozenset(_EXPECTED_EXCLUSIONS.values())


class AV1ValidationV2Error(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV2EligibilityCell:
    name: str
    state: AV1ValidationV2EligibilityState
    match: AV1ValidationV2Match
    traits: tuple[str, ...]
    required_case_count: int
    matching_item_count: int
    distinct_parent_scope_count: int
    producer_blockers: tuple[str, ...]
    exclusion_reason: str | None

    def __post_init__(self) -> None:
        if self.state not in {"eligible", "excluded"}:
            raise AV1ValidationV2Error("AV1 v2 eligibility state is unsupported")
        if not self.name or self.traits != tuple(sorted(set(self.traits))) or not self.traits:
            raise AV1ValidationV2Error("AV1 v2 eligibility cell identity is invalid")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.traits):
            raise AV1ValidationV2Error("AV1 v2 eligibility cell trait is unsupported")
        if self.match not in {"exact", "contains_all"}:
            raise AV1ValidationV2Error("AV1 v2 eligibility match is unsupported")
        if self.required_case_count <= 0:
            raise AV1ValidationV2Error("AV1 v2 eligibility cell requires cases")
        if min(self.matching_item_count, self.distinct_parent_scope_count) < 0:
            raise AV1ValidationV2Error("AV1 v2 eligibility counts cannot be negative")
        if self.producer_blockers != tuple(sorted(set(self.producer_blockers))):
            raise AV1ValidationV2Error("AV1 v2 producer blockers must be unique and sorted")
        if any(blocker not in _ELIGIBILITY_PRODUCER_BLOCKERS for blocker in self.producer_blockers):
            raise AV1ValidationV2Error("AV1 v2 producer blocker is unsupported")
        if self.exclusion_reason is not None and self.exclusion_reason not in _ELIGIBILITY_EXCLUSION_REASONS:
            raise AV1ValidationV2Error("AV1 v2 exclusion reason is unsupported")
        eligible = (
            not self.producer_blockers
            and self.matching_item_count >= self.required_case_count
            and self.distinct_parent_scope_count >= self.required_case_count
        )
        if self.state == "eligible":
            if not eligible or self.exclusion_reason is not None:
                raise AV1ValidationV2Error("AV1 v2 eligible cell does not satisfy its aggregate gate")
        elif eligible or not self.exclusion_reason:
            raise AV1ValidationV2Error("AV1 v2 excluded cell does not carry a valid blocker")

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "trait_selector": {"match": self.match, "traits": list(self.traits)},
            "required_case_count": self.required_case_count,
            "matching_item_count": self.matching_item_count,
            "distinct_parent_scope_count": self.distinct_parent_scope_count,
            "producer_blockers": list(self.producer_blockers),
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV2EligibilityAttestation:
    attestation_id: str
    as_of: str
    observation_store_available: bool
    cells: tuple[AV1ValidationV2EligibilityCell, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        _parse_timestamp(self.as_of, "eligibility cutoff")
        if not self.cells or self.cells != tuple(sorted(self.cells, key=lambda cell: cell.name)):
            raise AV1ValidationV2Error("AV1 v2 eligibility cells must be non-empty and sorted")
        if len({cell.name for cell in self.cells}) != len(self.cells):
            raise AV1ValidationV2Error("AV1 v2 eligibility cell names must be unique")
        semantic_payload = self.semantic_payload()
        if self.attestation_id != _v2_id("feasibility", semantic_payload):
            raise AV1ValidationV2Error("AV1 v2 eligibility ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"attestation_id": self.attestation_id, **semantic_payload}):
            raise AV1ValidationV2Error("AV1 v2 eligibility digest does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA,
            "schema_version": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA_VERSION,
            "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
            "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
            "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
            "authority": "derivation_only",
            "runtime_execution_authorized": False,
            "holdout_execution_authorized": False,
            "derivation_evidence_in_scope": False,
            "candidate_locks_ready": False,
            "observation_store_available": self.observation_store_available,
            "as_of": self.as_of,
            "cells": [cell.to_payload() for cell in self.cells],
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV2TraitSelector:
    match: AV1ValidationV2Match
    traits: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.match not in {"exact", "contains_all"}:
            raise AV1ValidationV2Error("AV1 v2 trait-selector match is unsupported")
        if self.traits != tuple(sorted(set(self.traits))) or not self.traits:
            raise AV1ValidationV2Error("AV1 v2 trait selector must be non-empty and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.traits):
            raise AV1ValidationV2Error("AV1 v2 trait selector contains an unsupported trait")

    def to_payload(self) -> dict[str, Any]:
        return {"match": self.match, "traits": list(self.traits)}

    def matches(self, traits: Sequence[str]) -> bool:
        normalized = tuple(sorted(set(str(trait) for trait in traits)))
        if self.match == "exact":
            return normalized == self.traits
        return set(self.traits).issubset(normalized)


@dataclass(frozen=True, slots=True)
class AV1ValidationV2CellPlan:
    cell_plan_id: str
    name: str
    mode: AV1ValidationV2PlanMode
    stratum: str
    intent_level: str
    optimization_objective: str
    trait_selector: AV1ValidationV2TraitSelector
    registered_case_count: int
    expected_prediction: str
    allowed_fallback_reasons: tuple[str, ...]
    target_confidence: str
    duplicate_review_ordinals: tuple[int, ...]
    eligibility_binding_id: str

    def __post_init__(self) -> None:
        if self.mode not in {"publication_candidate", "fallback_conformance"}:
            raise AV1ValidationV2Error("AV1 v2 plan mode is unsupported")
        if self.intent_level not in AV1_COLD_START_INTENT_OBJECTIVES:
            raise AV1ValidationV2Error("AV1 v2 intent is unsupported")
        if self.optimization_objective != AV1_COLD_START_INTENT_OBJECTIVES[self.intent_level]:
            raise AV1ValidationV2Error("AV1 v2 optimization objective does not match intent")
        if self.registered_case_count <= 0:
            raise AV1ValidationV2Error("AV1 v2 plan requires registered cases")
        if self.allowed_fallback_reasons != tuple(sorted(set(self.allowed_fallback_reasons))):
            raise AV1ValidationV2Error("AV1 v2 fallback reasons must be unique and sorted")
        if self.mode == "publication_candidate":
            if (
                    self.intent_level != "balanced"
                    or self.trait_selector.match != "exact"
                    or self.expected_prediction != "recommended"
                    or self.allowed_fallback_reasons
                    or self.target_confidence != "moderate"
                    or self.duplicate_review_ordinals
                    != tuple(sorted(set(self.duplicate_review_ordinals)))
                    or not self.duplicate_review_ordinals
                    or any(
                        not 1 <= ordinal <= self.registered_case_count
                        for ordinal in self.duplicate_review_ordinals
                    )
            ):
                raise AV1ValidationV2Error("AV1 v2 publication plan is invalid")
        elif (
                self.expected_prediction != "no_recommendation"
                or not self.allowed_fallback_reasons
                or self.target_confidence != "none"
                or self.duplicate_review_ordinals
        ):
            raise AV1ValidationV2Error("AV1 v2 fallback plan is invalid")
        if self.cell_plan_id != _v2_id("plan", self.semantic_payload()):
            raise AV1ValidationV2Error("AV1 v2 plan ID does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "stratum": self.stratum,
            "intent_level": self.intent_level,
            "optimization_objective": self.optimization_objective,
            "trait_selector": self.trait_selector.to_payload(),
            "registered_case_count": self.registered_case_count,
            "expected_prediction": self.expected_prediction,
            "allowed_fallback_reasons": list(self.allowed_fallback_reasons),
            "target_confidence": self.target_confidence,
            "duplicate_review_ordinals": list(self.duplicate_review_ordinals),
            "eligibility_binding_id": self.eligibility_binding_id,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"cell_plan_id": self.cell_plan_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationV2ExcludedCell:
    excluded_cell_id: str
    name: str
    intent_level: str
    trait_selector: AV1ValidationV2TraitSelector
    reason: str
    disposition: str

    def __post_init__(self) -> None:
        if self.disposition != "no_recommendation_not_registered" or not self.reason:
            raise AV1ValidationV2Error("AV1 v2 excluded-cell disposition is invalid")
        if self.excluded_cell_id != _v2_id("excluded", self.semantic_payload()):
            raise AV1ValidationV2Error("AV1 v2 excluded-cell ID does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intent_level": self.intent_level,
            "trait_selector": self.trait_selector.to_payload(),
            "reason": self.reason,
            "disposition": self.disposition,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"excluded_cell_id": self.excluded_cell_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationV2Case:
    case_id: str
    cell_plan_id: str
    ordinal: int
    execution_order: AV1ValidationV2ExecutionOrder

    def __post_init__(self) -> None:
        if self.ordinal <= 0 or self.execution_order not in {
            "baseline_only",
            "baseline_then_guided",
            "guided_then_baseline",
        }:
            raise AV1ValidationV2Error("AV1 v2 case execution contract is invalid")
        if self.case_id != _v2_id("case", self.semantic_payload()):
            raise AV1ValidationV2Error("AV1 v2 case ID does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "cell_plan_id": self.cell_plan_id,
            "ordinal": self.ordinal,
            "execution_order": self.execution_order,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"case_id": self.case_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationV2AuthorityPolicy:
    derivation_authorization_required: bool
    derivation_scope: str
    guided_probe_allowed_during_derivation: bool
    holdout_case_execution_allowed_during_derivation: bool
    holdout_execution_requires_selection_lock: bool
    holdout_execution_requires_candidate_locks: bool
    holdout_execution_requires_separate_authorization: bool
    public_bundle_activation_allowed: bool

    def __post_init__(self) -> None:
        if (
                self.derivation_authorization_required is not True
                or self.derivation_scope != "runtime_native_full_search_only"
                or self.guided_probe_allowed_during_derivation is not False
                or self.holdout_case_execution_allowed_during_derivation is not False
                or self.holdout_execution_requires_selection_lock is not True
                or self.holdout_execution_requires_candidate_locks is not True
                or self.holdout_execution_requires_separate_authorization is not True
                or self.public_bundle_activation_allowed is not False
        ):
            raise AV1ValidationV2Error("AV1 v2 authority policy is too permissive")

    def to_payload(self) -> dict[str, Any]:
        return {
            "derivation_authorization_required": self.derivation_authorization_required,
            "derivation_scope": self.derivation_scope,
            "guided_probe_allowed_during_derivation": self.guided_probe_allowed_during_derivation,
            "holdout_case_execution_allowed_during_derivation": self.holdout_case_execution_allowed_during_derivation,
            "holdout_execution_requires_selection_lock": self.holdout_execution_requires_selection_lock,
            "holdout_execution_requires_candidate_locks": self.holdout_execution_requires_candidate_locks,
            "holdout_execution_requires_separate_authorization": self.holdout_execution_requires_separate_authorization,
            "public_bundle_activation_allowed": self.public_bundle_activation_allowed,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV2DerivationAuthorization:
    authorization_id: str
    manifest_id: str
    manifest_payload_sha256: str
    selection_lock_sha256: str
    derivation_partition_sha256: str
    authorized_at: str
    valid_until: str
    review_state: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.manifest_id.startswith("av1vmanifest2_"):
            raise AV1ValidationV2Error("AV1 v2 derivation authorization references an invalid manifest")
        for value, label in (
            (self.manifest_payload_sha256, "manifest digest"),
            (self.selection_lock_sha256, "selection-lock digest"),
            (self.derivation_partition_sha256, "derivation-partition digest"),
        ):
            _require_sha256(value, label)
        authorized = _parse_timestamp(self.authorized_at, "derivation authorization")
        valid_until = _parse_timestamp(self.valid_until, "derivation authorization expiration")
        if valid_until <= authorized:
            raise AV1ValidationV2Error("AV1 v2 derivation authorization expiration is invalid")
        if self.review_state != "approved_for_derivation":
            raise AV1ValidationV2Error("AV1 v2 derivation authorization is not approved")
        semantic_payload = self.semantic_payload()
        if self.authorization_id != _v2_id("derivation_authorization", semantic_payload):
            raise AV1ValidationV2Error("AV1 v2 derivation authorization ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({
            "authorization_id": self.authorization_id,
            **semantic_payload,
        }):
            raise AV1ValidationV2Error("AV1 v2 derivation authorization digest does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return _derivation_authorization_semantic_payload(
            manifest_id=self.manifest_id,
            manifest_payload_sha256=self.manifest_payload_sha256,
            selection_lock_sha256=self.selection_lock_sha256,
            derivation_partition_sha256=self.derivation_partition_sha256,
            authorized_at=self.authorized_at,
            valid_until=self.valid_until,
            review_state=self.review_state,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationManifestV2:
    manifest_id: str
    supersedes_manifest_id: str
    supersedes_payload_sha256: str
    eligibility_attestation_id: str
    eligibility_payload_sha256: str
    registered_at: str
    valid_until: str
    criteria: AV1ColdStartValidationCriteriaV1
    authority_policy: AV1ValidationV2AuthorityPolicy
    cell_plans: tuple[AV1ValidationV2CellPlan, ...]
    excluded_cells: tuple[AV1ValidationV2ExcludedCell, ...]
    cases: tuple[AV1ValidationV2Case, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.supersedes_manifest_id.startswith("av1vmanifest1_"):
            raise AV1ValidationV2Error("AV1 v2 superseded manifest ID is invalid")
        _require_sha256(self.supersedes_payload_sha256, "superseded manifest digest")
        if not self.eligibility_attestation_id.startswith("av1vfeasibility2_"):
            raise AV1ValidationV2Error("AV1 v2 eligibility attestation ID is invalid")
        _require_sha256(self.eligibility_payload_sha256, "eligibility digest")
        registered = _parse_timestamp(self.registered_at, "manifest registration")
        valid_until = _parse_timestamp(self.valid_until, "manifest expiration")
        if valid_until <= registered:
            raise AV1ValidationV2Error("AV1 v2 manifest expiration must follow registration")
        if self.cell_plans != tuple(sorted(self.cell_plans, key=lambda plan: plan.name)) or not self.cell_plans:
            raise AV1ValidationV2Error("AV1 v2 plans must be non-empty and sorted")
        if self.excluded_cells != tuple(sorted(self.excluded_cells, key=lambda cell: cell.name)):
            raise AV1ValidationV2Error("AV1 v2 exclusions must be sorted")
        if self.cases != tuple(sorted(self.cases, key=lambda case: case.case_id)):
            raise AV1ValidationV2Error("AV1 v2 cases must be sorted")
        _validate_v2_coverage(self)
        semantic_payload = self.semantic_payload()
        if self.manifest_id != _v2_id("manifest", semantic_payload):
            raise AV1ValidationV2Error("AV1 v2 manifest ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"manifest_id": self.manifest_id, **semantic_payload}):
            raise AV1ValidationV2Error("AV1 v2 manifest digest does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V2_MANIFEST_SCHEMA,
            "schema_version": AV1_VALIDATION_V2_MANIFEST_SCHEMA_VERSION,
            "validation_contract_version": AV1_VALIDATION_V2_CONTRACT_VERSION,
            "production_predictor_contract_version": AV1_VALIDATION_V2_PRODUCTION_PREDICTOR_CONTRACT_VERSION,
            "validation_candidate_contract_version": AV1_VALIDATION_V2_CANDIDATE_CONTRACT_VERSION,
            "derivation_authorization_contract_version": (
                AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION
            ),
            "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
            "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
            "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
            "governance_issue": "#277",
            "preregistration_issue": "#285",
            "supersedes_manifest_id": self.supersedes_manifest_id,
            "supersedes_payload_sha256": self.supersedes_payload_sha256,
            "state": "preregistered_derivation_only",
            "authority": "derivation_only",
            "runtime_execution_authorized": False,
            "holdout_execution_authorized": False,
            "selection_lock_required_for_holdout": True,
            "execution_policy": "separate_derivation_and_holdout_authorizations",
            "privacy_contract": "digest_bound_eligibility_private_mapping_aggregate_public_report",
            "eligibility_attestation_id": self.eligibility_attestation_id,
            "eligibility_payload_sha256": self.eligibility_payload_sha256,
            "registered_at": self.registered_at,
            "valid_until": self.valid_until,
            "criteria": self.criteria.to_payload(),
            "authority_policy": self.authority_policy.to_payload(),
            "execution_semantics": _v2_execution_semantics(),
            "cell_plans": [plan.to_payload() for plan in self.cell_plans],
            "excluded_cells": [cell.to_payload() for cell in self.excluded_cells],
            "cases": [case.to_payload() for case in self.cases],
        }

    def to_payload(self) -> dict[str, Any]:
        return {"manifest_id": self.manifest_id, **self.semantic_payload(), "payload_sha256": self.payload_sha256}


def build_av1_validation_v2_eligibility_attestation(
        *,
        as_of: str,
        observation_store_available: bool,
        cells: Sequence[AV1ValidationV2EligibilityCell],
) -> AV1ValidationV2EligibilityAttestation:
    sorted_cells = tuple(sorted(cells, key=lambda cell: cell.name))
    semantic_payload = {
        "schema": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA_VERSION,
        "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
        "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "authority": "derivation_only",
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
        "derivation_evidence_in_scope": False,
        "candidate_locks_ready": False,
        "observation_store_available": observation_store_available,
        "as_of": as_of,
        "cells": [cell.to_payload() for cell in sorted_cells],
    }
    attestation_id = _v2_id("feasibility", semantic_payload)
    return AV1ValidationV2EligibilityAttestation(
        attestation_id=attestation_id,
        as_of=as_of,
        observation_store_available=observation_store_available,
        cells=sorted_cells,
        payload_sha256=_payload_sha256({"attestation_id": attestation_id, **semantic_payload}),
    )


def build_av1_validation_manifest_v2(
        *,
        eligibility_attestation_id: str,
        eligibility_payload_sha256: str,
        supersedes_manifest_id: str,
        supersedes_payload_sha256: str,
) -> AV1ValidationManifestV2:
    criteria = _v2_criteria()
    plans = _v2_plans(eligibility_attestation_id)
    excluded = _v2_exclusions()
    cases = tuple(sorted((
        _build_v2_case(plan, ordinal)
        for plan in plans
        for ordinal in range(1, plan.registered_case_count + 1)
    ), key=lambda case: case.case_id))
    authority = AV1ValidationV2AuthorityPolicy(
        derivation_authorization_required=True,
        derivation_scope="runtime_native_full_search_only",
        guided_probe_allowed_during_derivation=False,
        holdout_case_execution_allowed_during_derivation=False,
        holdout_execution_requires_selection_lock=True,
        holdout_execution_requires_candidate_locks=True,
        holdout_execution_requires_separate_authorization=True,
        public_bundle_activation_allowed=False,
    )
    semantic_payload = _manifest_semantic_payload(
        supersedes_manifest_id=supersedes_manifest_id,
        supersedes_payload_sha256=supersedes_payload_sha256,
        eligibility_attestation_id=eligibility_attestation_id,
        eligibility_payload_sha256=eligibility_payload_sha256,
        criteria=criteria,
        authority=authority,
        plans=plans,
        excluded=excluded,
        cases=cases,
    )
    manifest_id = _v2_id("manifest", semantic_payload)
    return AV1ValidationManifestV2(
        manifest_id=manifest_id,
        supersedes_manifest_id=supersedes_manifest_id,
        supersedes_payload_sha256=supersedes_payload_sha256,
        eligibility_attestation_id=eligibility_attestation_id,
        eligibility_payload_sha256=eligibility_payload_sha256,
        registered_at=AV1_VALIDATION_V2_REGISTERED_AT,
        valid_until=AV1_VALIDATION_V2_VALID_UNTIL,
        criteria=criteria,
        authority_policy=authority,
        cell_plans=plans,
        excluded_cells=excluded,
        cases=cases,
        payload_sha256=_payload_sha256({"manifest_id": manifest_id, **semantic_payload}),
    )


def build_preregistered_av1_validation_manifest_v2() -> AV1ValidationManifestV2:
    return build_av1_validation_manifest_v2(
        eligibility_attestation_id=AV1_VALIDATION_V2_ELIGIBILITY_ATTESTATION_ID,
        eligibility_payload_sha256=AV1_VALIDATION_V2_ELIGIBILITY_PAYLOAD_SHA256,
        supersedes_manifest_id=AV1_VALIDATION_V1_MANIFEST_ID,
        supersedes_payload_sha256=AV1_VALIDATION_V1_MANIFEST_PAYLOAD_SHA256,
    )


def assert_preregistered_av1_validation_manifest_v2(manifest: AV1ValidationManifestV2) -> None:
    if manifest != build_preregistered_av1_validation_manifest_v2():
        raise AV1ValidationV2Error("AV1 v2 manifest does not match the pinned issue #285 preregistration")


def assert_preregistered_av1_validation_v2_eligibility(
        eligibility: AV1ValidationV2EligibilityAttestation,
) -> None:
    _validate_v2_eligibility(eligibility)
    if _parse_timestamp(eligibility.as_of, "eligibility cutoff") >= _parse_timestamp(
            AV1_VALIDATION_V2_REGISTERED_AT,
            "manifest registration",
    ):
        raise AV1ValidationV2Error("AV1 v2 eligibility must predate manifest registration")
    if (
            eligibility.attestation_id != AV1_VALIDATION_V2_ELIGIBILITY_ATTESTATION_ID
            or eligibility.payload_sha256 != AV1_VALIDATION_V2_ELIGIBILITY_PAYLOAD_SHA256
    ):
        raise AV1ValidationV2Error("AV1 v2 eligibility does not match the pinned aggregate attestation")


def build_av1_validation_v2_derivation_authorization(
        *,
        manifest: AV1ValidationManifestV2,
        selection_lock_sha256: str,
        derivation_partition_sha256: str,
        authorized_at: str,
        valid_until: str,
) -> AV1ValidationV2DerivationAuthorization:
    assert_preregistered_av1_validation_manifest_v2(manifest)
    semantic_payload = _derivation_authorization_semantic_payload(
        manifest_id=manifest.manifest_id,
        manifest_payload_sha256=manifest.payload_sha256,
        selection_lock_sha256=selection_lock_sha256,
        derivation_partition_sha256=derivation_partition_sha256,
        authorized_at=authorized_at,
        valid_until=valid_until,
        review_state="approved_for_derivation",
    )
    authorization_id = _v2_id("derivation_authorization", semantic_payload)
    return AV1ValidationV2DerivationAuthorization(
        authorization_id=authorization_id,
        manifest_id=manifest.manifest_id,
        manifest_payload_sha256=manifest.payload_sha256,
        selection_lock_sha256=selection_lock_sha256,
        derivation_partition_sha256=derivation_partition_sha256,
        authorized_at=authorized_at,
        valid_until=valid_until,
        review_state="approved_for_derivation",
        payload_sha256=_payload_sha256({
            "authorization_id": authorization_id,
            **semantic_payload,
        }),
    )


def serialize_av1_validation_manifest_v2(manifest: AV1ValidationManifestV2) -> bytes:
    return canonical_json_bytes(manifest.to_payload()) + b"\n"


def serialize_av1_validation_v2_eligibility(
        eligibility: AV1ValidationV2EligibilityAttestation,
) -> bytes:
    return canonical_json_bytes(eligibility.to_payload()) + b"\n"


def serialize_av1_validation_v2_derivation_authorization(
        authorization: AV1ValidationV2DerivationAuthorization,
) -> bytes:
    return canonical_json_bytes(authorization.to_payload()) + b"\n"


def load_av1_validation_manifest_v2(path: Path) -> AV1ValidationManifestV2:
    raw = path.read_bytes()
    try:
        manifest = av1_validation_manifest_v2_from_payload(object_dict(json.loads(raw.decode("utf-8"))))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV2Error("AV1 v2 manifest is invalid") from exc
    if raw != serialize_av1_validation_manifest_v2(manifest):
        raise AV1ValidationV2Error("AV1 v2 manifest bytes are not canonical")
    return manifest


def load_av1_validation_v2_eligibility(path: Path) -> AV1ValidationV2EligibilityAttestation:
    raw = path.read_bytes()
    try:
        eligibility = av1_validation_v2_eligibility_from_payload(object_dict(json.loads(raw.decode("utf-8"))))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV2Error("AV1 v2 eligibility attestation is invalid") from exc
    if raw != serialize_av1_validation_v2_eligibility(eligibility):
        raise AV1ValidationV2Error("AV1 v2 eligibility bytes are not canonical")
    return eligibility


def load_av1_validation_v2_derivation_authorization(
        path: Path,
) -> AV1ValidationV2DerivationAuthorization:
    raw = path.read_bytes()
    try:
        authorization = av1_validation_v2_derivation_authorization_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV2Error("AV1 v2 derivation authorization is invalid") from exc
    if raw != serialize_av1_validation_v2_derivation_authorization(authorization):
        raise AV1ValidationV2Error("AV1 v2 derivation authorization bytes are not canonical")
    return authorization


def av1_validation_v2_eligibility_from_payload(
        payload: Mapping[str, Any],
) -> AV1ValidationV2EligibilityAttestation:
    value = object_dict(payload)
    _require_v2_eligibility_contract(value)
    cells = tuple(
        _eligibility_cell_from_payload(object_dict(cell))
        for cell in object_list(value.get("cells"))
    )
    return AV1ValidationV2EligibilityAttestation(
        attestation_id=str(value.get("attestation_id") or ""),
        as_of=str(value.get("as_of") or ""),
        observation_store_available=_required_bool(value.get("observation_store_available")),
        cells=cells,
        payload_sha256=str(value.get("payload_sha256") or ""),
    )


def av1_validation_v2_derivation_authorization_from_payload(
        payload: Mapping[str, Any],
) -> AV1ValidationV2DerivationAuthorization:
    value = object_dict(payload)
    _require_derivation_authorization_contract(value)
    return AV1ValidationV2DerivationAuthorization(
        authorization_id=str(value.get("authorization_id") or ""),
        manifest_id=str(value.get("manifest_id") or ""),
        manifest_payload_sha256=str(value.get("manifest_payload_sha256") or ""),
        selection_lock_sha256=str(value.get("selection_lock_sha256") or ""),
        derivation_partition_sha256=str(value.get("derivation_partition_sha256") or ""),
        authorized_at=str(value.get("authorized_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        review_state=str(value.get("review_state") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )


def av1_validation_manifest_v2_from_payload(payload: Mapping[str, Any]) -> AV1ValidationManifestV2:
    value = object_dict(payload)
    _require_v2_manifest_contract(value)
    return AV1ValidationManifestV2(
        manifest_id=str(value.get("manifest_id") or ""),
        supersedes_manifest_id=str(value.get("supersedes_manifest_id") or ""),
        supersedes_payload_sha256=str(value.get("supersedes_payload_sha256") or ""),
        eligibility_attestation_id=str(value.get("eligibility_attestation_id") or ""),
        eligibility_payload_sha256=str(value.get("eligibility_payload_sha256") or ""),
        registered_at=str(value.get("registered_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        criteria=_criteria_from_payload(object_dict(value.get("criteria"))),
        authority_policy=_authority_from_payload(object_dict(value.get("authority_policy"))),
        cell_plans=tuple(_plan_from_payload(object_dict(plan)) for plan in object_list(value.get("cell_plans"))),
        excluded_cells=tuple(
            _excluded_from_payload(object_dict(cell)) for cell in object_list(value.get("excluded_cells"))
        ),
        cases=tuple(_case_from_payload(object_dict(case)) for case in object_list(value.get("cases"))),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )


def _v2_execution_semantics() -> dict[str, Any]:
    return {
        "baseline_arm": "unchanged_measured_full_search",
        "guided_arm": "one_locked_candidate_probe_then_unchanged_measured_fallback",
        "fallback_conformance_arm": "unchanged_measured_full_search_only",
        "case_storage_order": "canonical_case_id_sort",
        "case_execution_sequence": "manifest_cases_array_order",
        "arm_execution_order_source": "case_execution_order_field",
        "publication_candidate_local_personalization_allowed": False,
        "result_statuses": ["complete", "failed", "not_run_after_safety_stop"],
        "visible_blockers": [
            "compatibility_drift",
            "failed",
            "incomplete",
            "missing",
            "safety_stopped",
            "timed_out",
        ],
        "unfavorable_or_incomplete_cases_droppable": False,
        "safety_stop_scope": "affected_cell",
        "safety_stop_threshold": 0,
        "human_visual_verdict_required": True,
    }


def _v2_criteria() -> AV1ColdStartValidationCriteriaV1:
    return AV1ColdStartValidationCriteriaV1(
        minimum_derivation_evidence_count=12,
        minimum_derivation_source_count=(
            AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT
        ),
        minimum_holdout_count=16,
        minimum_holdout_source_count=6,
        maximum_derivation_age_days=(
            AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS
        ),
        maximum_candidate_crf_span=(
            AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN
        ),
        range_hit_null_rate=0.5,
        maximum_one_sided_p_value=0.025,
        maximum_safety_regression_count=0,
        duplicate_review_fraction=0.2,
        minimum_duplicate_review_agreement=0.8,
        confidence_level="moderate",
        confidence_score=0.7,
    )


def _v2_plans(
        eligibility_attestation_id: str,
) -> tuple[AV1ValidationV2CellPlan, ...]:
    definitions = (
        ("darkness_balanced_candidate", "publication_candidate", "risk", "balanced", "exact", ("darkness",), 16, "recommended", (), "moderate"),
        ("grain_noise_risk_fallback", "fallback_conformance", "risk", "balanced", "contains_all", ("grain_noise",), 3, "no_recommendation", ("content_profile_uncovered",), "none"),
        ("mixed_risk_fallback", "fallback_conformance", "risk", "balanced", "contains_all", ("mixed",), 3, "no_recommendation", ("content_profile_uncovered",), "none"),
        ("motion_balanced_candidate", "publication_candidate", "risk", "balanced", "exact", ("motion",), 16, "recommended", (), "moderate"),
        ("perceptual_floor_directional_fallback", "fallback_conformance", "intent", "perceptual_floor", "contains_all", ("typical",), 3, "no_recommendation", ("compression_intent_requires_directional_search",), "none"),
        ("reference_directional_fallback", "fallback_conformance", "intent", "reference", "contains_all", ("typical",), 3, "no_recommendation", ("compression_intent_requires_directional_search",), "none"),
        ("texture_detail_risk_fallback", "fallback_conformance", "risk", "balanced", "contains_all", ("texture_detail",), 3, "no_recommendation", ("content_profile_uncovered",), "none"),
        ("transparent_directional_fallback", "fallback_conformance", "intent", "transparent", "contains_all", ("typical",), 3, "no_recommendation", ("compression_intent_requires_directional_search",), "none"),
    )
    plans: list[AV1ValidationV2CellPlan] = []
    for definition in definitions:
        name, mode, stratum, intent, match, traits, count, prediction, reasons, confidence = definition
        selector = AV1ValidationV2TraitSelector(cast(AV1ValidationV2Match, match), traits)
        ordinals = tuple(range(4, count + 1, 4)) if mode == "publication_candidate" else ()
        binding_id = _eligibility_binding_id(eligibility_attestation_id, name, selector, count)
        semantic_payload = {
            "name": name,
            "mode": mode,
            "stratum": stratum,
            "intent_level": intent,
            "optimization_objective": AV1_COLD_START_INTENT_OBJECTIVES[intent],
            "trait_selector": selector.to_payload(),
            "registered_case_count": count,
            "expected_prediction": prediction,
            "allowed_fallback_reasons": list(reasons),
            "target_confidence": confidence,
            "duplicate_review_ordinals": list(ordinals),
            "eligibility_binding_id": binding_id,
        }
        plans.append(AV1ValidationV2CellPlan(
            cell_plan_id=_v2_id("plan", semantic_payload),
            name=name,
            mode=cast(AV1ValidationV2PlanMode, mode),
            stratum=stratum,
            intent_level=intent,
            optimization_objective=AV1_COLD_START_INTENT_OBJECTIVES[intent],
            trait_selector=selector,
            registered_case_count=count,
            expected_prediction=prediction,
            allowed_fallback_reasons=reasons,
            target_confidence=confidence,
            duplicate_review_ordinals=ordinals,
            eligibility_binding_id=binding_id,
        ))
    return tuple(sorted(plans, key=lambda plan: plan.name))


def _v2_exclusions() -> tuple[AV1ValidationV2ExcludedCell, ...]:
    results: list[AV1ValidationV2ExcludedCell] = []
    traits_by_name = {
        "animation_balanced_candidate": ("animation",),
        "low_motion_dialogue_risk_fallback": ("low_motion_dialogue",),
        "typical_live_action_balanced_candidate": ("typical",),
        "unknown_risk_fallback": ("unknown",),
    }
    matches_by_name: dict[str, AV1ValidationV2Match] = {
        "animation_balanced_candidate": "exact",
        "low_motion_dialogue_risk_fallback": "contains_all",
        "typical_live_action_balanced_candidate": "exact",
        "unknown_risk_fallback": "contains_all",
    }
    for name, expected_reason in _EXPECTED_EXCLUSIONS.items():
        traits = traits_by_name[name]
        match = matches_by_name[name]
        selector = AV1ValidationV2TraitSelector(match, traits)
        semantic_payload = {
            "name": name,
            "intent_level": "balanced",
            "trait_selector": selector.to_payload(),
            "reason": expected_reason,
            "disposition": "no_recommendation_not_registered",
        }
        results.append(AV1ValidationV2ExcludedCell(
            excluded_cell_id=_v2_id("excluded", semantic_payload),
            name=name,
            intent_level="balanced",
            trait_selector=selector,
            reason=expected_reason,
            disposition="no_recommendation_not_registered",
        ))
    return tuple(sorted(results, key=lambda cell: cell.name))


def _build_v2_case(plan: AV1ValidationV2CellPlan, ordinal: int) -> AV1ValidationV2Case:
    execution_order: AV1ValidationV2ExecutionOrder
    if plan.mode == "fallback_conformance":
        execution_order = "baseline_only"
    elif ordinal % 2:
        execution_order = "baseline_then_guided"
    else:
        execution_order = "guided_then_baseline"
    semantic_payload = {
        "cell_plan_id": plan.cell_plan_id,
        "ordinal": ordinal,
        "execution_order": execution_order,
    }
    return AV1ValidationV2Case(
        case_id=_v2_id("case", semantic_payload),
        cell_plan_id=plan.cell_plan_id,
        ordinal=ordinal,
        execution_order=execution_order,
    )


def _manifest_semantic_payload(
        *,
        supersedes_manifest_id: str,
        supersedes_payload_sha256: str,
        eligibility_attestation_id: str,
        eligibility_payload_sha256: str,
        criteria: AV1ColdStartValidationCriteriaV1,
        authority: AV1ValidationV2AuthorityPolicy,
        plans: Sequence[AV1ValidationV2CellPlan],
        excluded: Sequence[AV1ValidationV2ExcludedCell],
        cases: Sequence[AV1ValidationV2Case],
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V2_MANIFEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_MANIFEST_SCHEMA_VERSION,
        "validation_contract_version": AV1_VALIDATION_V2_CONTRACT_VERSION,
        "production_predictor_contract_version": AV1_VALIDATION_V2_PRODUCTION_PREDICTOR_CONTRACT_VERSION,
        "validation_candidate_contract_version": AV1_VALIDATION_V2_CANDIDATE_CONTRACT_VERSION,
        "derivation_authorization_contract_version": (
            AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION
        ),
        "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
        "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "governance_issue": "#277",
        "preregistration_issue": "#285",
        "supersedes_manifest_id": supersedes_manifest_id,
        "supersedes_payload_sha256": supersedes_payload_sha256,
        "state": "preregistered_derivation_only",
        "authority": "derivation_only",
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
        "selection_lock_required_for_holdout": True,
        "execution_policy": "separate_derivation_and_holdout_authorizations",
        "privacy_contract": "digest_bound_eligibility_private_mapping_aggregate_public_report",
        "eligibility_attestation_id": eligibility_attestation_id,
        "eligibility_payload_sha256": eligibility_payload_sha256,
        "registered_at": AV1_VALIDATION_V2_REGISTERED_AT,
        "valid_until": AV1_VALIDATION_V2_VALID_UNTIL,
        "criteria": criteria.to_payload(),
        "authority_policy": authority.to_payload(),
        "execution_semantics": _v2_execution_semantics(),
        "cell_plans": [plan.to_payload() for plan in plans],
        "excluded_cells": [cell.to_payload() for cell in excluded],
        "cases": [case.to_payload() for case in cases],
    }


def _derivation_authorization_semantic_payload(
        *,
        manifest_id: str,
        manifest_payload_sha256: str,
        selection_lock_sha256: str,
        derivation_partition_sha256: str,
        authorized_at: str,
        valid_until: str,
        review_state: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION,
        "authority": "derivation_only",
        "runtime_execution_authorized": True,
        "execution_scope": "reserved_derivation_sources_only",
        "search_mode": "unchanged_measured_full_search",
        "validation_harness_allowed": False,
        "validation_candidate_allowed": False,
        "guided_probe_allowed": False,
        "holdout_case_execution_allowed": False,
        "public_bundle_activation_allowed": False,
        "manifest_id": manifest_id,
        "manifest_payload_sha256": manifest_payload_sha256,
        "selection_lock_sha256": selection_lock_sha256,
        "derivation_partition_sha256": derivation_partition_sha256,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "review_state": review_state,
    }


def _validate_v2_coverage(manifest: AV1ValidationManifestV2) -> None:
    candidate_plans = {plan.name: plan for plan in manifest.cell_plans if plan.mode == "publication_candidate"}
    if {name: plan.trait_selector.traits for name, plan in candidate_plans.items()} != _EXPECTED_CANDIDATES:
        raise AV1ValidationV2Error("AV1 v2 publication candidates are not the preregistered reachable cells")
    if any(plan.registered_case_count < manifest.criteria.minimum_holdout_count for plan in candidate_plans.values()):
        raise AV1ValidationV2Error("AV1 v2 publication candidate is below the holdout minimum")
    if any(
            len(plan.duplicate_review_ordinals)
            < math.ceil(plan.registered_case_count * manifest.criteria.duplicate_review_fraction)
            for plan in candidate_plans.values()
    ):
        raise AV1ValidationV2Error("AV1 v2 publication duplicate-review coverage is too small")
    exclusion_reasons = {cell.name: cell.reason for cell in manifest.excluded_cells}
    if exclusion_reasons != _EXPECTED_EXCLUSIONS:
        raise AV1ValidationV2Error("AV1 v2 excluded cells do not match the preregistered no-go set")
    plan_ids = {plan.cell_plan_id for plan in manifest.cell_plans}
    counts = Counter(case.cell_plan_id for case in manifest.cases)
    if set(counts) != plan_ids:
        raise AV1ValidationV2Error("AV1 v2 cases do not cover every plan")
    if any(counts[plan.cell_plan_id] != plan.registered_case_count for plan in manifest.cell_plans):
        raise AV1ValidationV2Error("AV1 v2 case counts do not match their plans")
    expected_intent_fallbacks = {"reference", "transparent", "perceptual_floor"}
    actual_intent_fallbacks = {
        plan.intent_level
        for plan in manifest.cell_plans
        if plan.mode == "fallback_conformance"
        and "compression_intent_requires_directional_search" in plan.allowed_fallback_reasons
    }
    if actual_intent_fallbacks != expected_intent_fallbacks:
        raise AV1ValidationV2Error("AV1 v2 directional-intent fallback coverage is incomplete")
    fallback_plans = {plan.name: plan for plan in manifest.cell_plans if plan.mode == "fallback_conformance"}
    if {
            name: (plan.intent_level, plan.trait_selector.match, plan.trait_selector.traits)
            for name, plan in fallback_plans.items()
    } != _EXPECTED_FALLBACKS:
        raise AV1ValidationV2Error("AV1 v2 fallback coverage does not match the preregistered reachable cells")


def _validate_v2_eligibility(eligibility: AV1ValidationV2EligibilityAttestation) -> None:
    cells_by_name = {cell.name: cell for cell in eligibility.cells}
    expected_names = set(_EXPECTED_CANDIDATES) | set(_EXPECTED_FALLBACKS) | set(_EXPECTED_EXCLUSIONS)
    if set(cells_by_name) != expected_names:
        raise AV1ValidationV2Error("AV1 v2 eligibility cells do not match the preregistered cohort")

    for name, traits in _EXPECTED_CANDIDATES.items():
        cell = cells_by_name[name]
        if (
                cell.state != "eligible"
                or cell.match != "exact"
                or cell.traits != traits
                or cell.required_case_count != 16
                or cell.producer_blockers
                or cell.exclusion_reason is not None
        ):
            raise AV1ValidationV2Error(f"AV1 v2 candidate eligibility is invalid for {name}")

    for name, (intent, match, traits) in _EXPECTED_FALLBACKS.items():
        del intent
        cell = cells_by_name[name]
        if (
                cell.state != "eligible"
                or cell.match != match
                or cell.traits != traits
                or cell.required_case_count != 3
                or cell.producer_blockers
                or cell.exclusion_reason is not None
        ):
            raise AV1ValidationV2Error(f"AV1 v2 fallback eligibility is invalid for {name}")

    exclusion_specs = {
        "animation_balanced_candidate": ("exact", ("animation",), 16, (), "insufficient_inventory"),
        "low_motion_dialogue_risk_fallback": (
            "contains_all",
            ("low_motion_dialogue",),
            3,
            ("low_motion_dialogue_analyzer_unavailable",),
            "trait_projection_unavailable",
        ),
        "typical_live_action_balanced_candidate": (
            "exact",
            ("typical",),
            16,
            (),
            "insufficient_inventory",
        ),
        "unknown_risk_fallback": (
            "contains_all",
            ("unknown",),
            3,
            ("unknown_evidence_rejected_by_request_contract",),
            "request_contract_unavailable",
        ),
    }
    for name, (match, traits, count, blockers, reason) in exclusion_specs.items():
        cell = cells_by_name[name]
        if (
                cell.state != "excluded"
                or cell.match != match
                or cell.traits != traits
                or cell.required_case_count != count
                or cell.producer_blockers != blockers
                or cell.exclusion_reason != reason
        ):
            raise AV1ValidationV2Error(f"AV1 v2 excluded-cell eligibility is invalid for {name}")


def _require_v2_manifest_contract(value: Mapping[str, Any]) -> None:
    expected = {
        "manifest_id", "schema", "schema_version", "validation_contract_version",
        "production_predictor_contract_version", "validation_candidate_contract_version",
        "derivation_authorization_contract_version",
        "trait_projection_contract_version", "trait_feasibility_contract_version",
        "validation_harness_contract_version", "governance_issue", "preregistration_issue",
        "supersedes_manifest_id", "supersedes_payload_sha256", "state", "authority",
        "runtime_execution_authorized", "holdout_execution_authorized",
        "selection_lock_required_for_holdout", "execution_policy", "privacy_contract",
        "eligibility_attestation_id", "eligibility_payload_sha256", "registered_at",
        "valid_until", "criteria", "authority_policy", "execution_semantics", "cell_plans", "excluded_cells",
        "cases", "payload_sha256",
    }
    if set(value) != expected:
        raise AV1ValidationV2Error("AV1 v2 manifest fields are unsupported")
    required_values = {
        "schema": AV1_VALIDATION_V2_MANIFEST_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_MANIFEST_SCHEMA_VERSION,
        "validation_contract_version": AV1_VALIDATION_V2_CONTRACT_VERSION,
        "production_predictor_contract_version": AV1_VALIDATION_V2_PRODUCTION_PREDICTOR_CONTRACT_VERSION,
        "validation_candidate_contract_version": AV1_VALIDATION_V2_CANDIDATE_CONTRACT_VERSION,
        "derivation_authorization_contract_version": (
            AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION
        ),
        "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
        "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "governance_issue": "#277",
        "preregistration_issue": "#285",
        "state": "preregistered_derivation_only",
        "authority": "derivation_only",
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
        "selection_lock_required_for_holdout": True,
        "execution_policy": "separate_derivation_and_holdout_authorizations",
        "privacy_contract": "digest_bound_eligibility_private_mapping_aggregate_public_report",
    }
    if any(value.get(key) != expected_value for key, expected_value in required_values.items()):
        raise AV1ValidationV2Error("AV1 v2 manifest contract is unsupported")
    if object_dict(value.get("execution_semantics")) != _v2_execution_semantics():
        raise AV1ValidationV2Error("AV1 v2 execution semantics are unsupported")


def _require_v2_eligibility_contract(value: Mapping[str, Any]) -> None:
    expected = {
        "attestation_id",
        "schema",
        "schema_version",
        "trait_projection_contract_version",
        "trait_feasibility_contract_version",
        "validation_harness_contract_version",
        "authority",
        "runtime_execution_authorized",
        "holdout_execution_authorized",
        "derivation_evidence_in_scope",
        "candidate_locks_ready",
        "observation_store_available",
        "as_of",
        "cells",
        "payload_sha256",
    }
    if set(value) != expected:
        raise AV1ValidationV2Error("AV1 v2 eligibility fields are unsupported")
    required_values = {
        "schema": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_ELIGIBILITY_SCHEMA_VERSION,
        "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "trait_feasibility_contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
        "validation_harness_contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "authority": "derivation_only",
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
        "derivation_evidence_in_scope": False,
        "candidate_locks_ready": False,
    }
    if any(value.get(key) != expected_value for key, expected_value in required_values.items()):
        raise AV1ValidationV2Error("AV1 v2 eligibility contract is unsupported")


def _require_derivation_authorization_contract(value: Mapping[str, Any]) -> None:
    expected = {
        "authorization_id",
        "schema",
        "schema_version",
        "contract_version",
        "authority",
        "runtime_execution_authorized",
        "execution_scope",
        "search_mode",
        "validation_harness_allowed",
        "validation_candidate_allowed",
        "guided_probe_allowed",
        "holdout_case_execution_allowed",
        "public_bundle_activation_allowed",
        "manifest_id",
        "manifest_payload_sha256",
        "selection_lock_sha256",
        "derivation_partition_sha256",
        "authorized_at",
        "valid_until",
        "review_state",
        "payload_sha256",
    }
    if set(value) != expected:
        raise AV1ValidationV2Error("AV1 v2 derivation authorization fields are unsupported")
    required_values = {
        "schema": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA,
        "schema_version": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V2_DERIVATION_AUTHORIZATION_CONTRACT_VERSION,
        "authority": "derivation_only",
        "runtime_execution_authorized": True,
        "execution_scope": "reserved_derivation_sources_only",
        "search_mode": "unchanged_measured_full_search",
        "validation_harness_allowed": False,
        "validation_candidate_allowed": False,
        "guided_probe_allowed": False,
        "holdout_case_execution_allowed": False,
        "public_bundle_activation_allowed": False,
    }
    if any(value.get(key) != expected_value for key, expected_value in required_values.items()):
        raise AV1ValidationV2Error("AV1 v2 derivation authorization is too permissive")


def _eligibility_cell_from_payload(value: Mapping[str, Any]) -> AV1ValidationV2EligibilityCell:
    _require_exact_keys(value, {
        "name",
        "state",
        "trait_selector",
        "required_case_count",
        "matching_item_count",
        "distinct_parent_scope_count",
        "producer_blockers",
        "exclusion_reason",
    }, "eligibility cell")
    selector = object_dict(value.get("trait_selector"))
    _require_exact_keys(selector, {"match", "traits"}, "eligibility trait selector")
    return AV1ValidationV2EligibilityCell(
        name=str(value.get("name") or ""),
        state=cast(AV1ValidationV2EligibilityState, str(value.get("state") or "")),
        match=cast(AV1ValidationV2Match, str(selector.get("match") or "")),
        traits=tuple(sorted(str(trait) for trait in object_list(selector.get("traits")))),
        required_case_count=int_value(value.get("required_case_count")),
        matching_item_count=int_value(value.get("matching_item_count")),
        distinct_parent_scope_count=int_value(value.get("distinct_parent_scope_count")),
        producer_blockers=tuple(sorted(str(reason) for reason in object_list(value.get("producer_blockers")))),
        exclusion_reason=_optional_text(value.get("exclusion_reason")),
    )


def _criteria_from_payload(value: Mapping[str, Any]) -> AV1ColdStartValidationCriteriaV1:
    _require_exact_keys(value, {
        "minimum_derivation_evidence_count",
        "minimum_derivation_source_count",
        "minimum_holdout_count",
        "minimum_holdout_source_count",
        "maximum_derivation_age_days",
        "maximum_candidate_crf_span",
        "range_hit_null_rate",
        "maximum_one_sided_p_value",
        "maximum_safety_regression_count",
        "duplicate_review_fraction",
        "minimum_duplicate_review_agreement",
        "confidence_level",
        "confidence_score",
    }, "criteria")
    return AV1ColdStartValidationCriteriaV1(
        minimum_derivation_evidence_count=int_value(value.get("minimum_derivation_evidence_count")),
        minimum_derivation_source_count=int_value(value.get("minimum_derivation_source_count")),
        minimum_holdout_count=int_value(value.get("minimum_holdout_count")),
        minimum_holdout_source_count=int_value(value.get("minimum_holdout_source_count")),
        maximum_derivation_age_days=int_value(value.get("maximum_derivation_age_days")),
        maximum_candidate_crf_span=float_value(value.get("maximum_candidate_crf_span")),
        range_hit_null_rate=float_value(value.get("range_hit_null_rate")),
        maximum_one_sided_p_value=float_value(value.get("maximum_one_sided_p_value")),
        maximum_safety_regression_count=int_value(value.get("maximum_safety_regression_count")),
        duplicate_review_fraction=float_value(value.get("duplicate_review_fraction")),
        minimum_duplicate_review_agreement=float_value(value.get("minimum_duplicate_review_agreement")),
        confidence_level=str(value.get("confidence_level") or ""),
        confidence_score=float_value(value.get("confidence_score")),
    )


def _authority_from_payload(value: Mapping[str, Any]) -> AV1ValidationV2AuthorityPolicy:
    _require_exact_keys(value, {
        "derivation_authorization_required",
        "derivation_scope",
        "guided_probe_allowed_during_derivation",
        "holdout_case_execution_allowed_during_derivation",
        "holdout_execution_requires_selection_lock",
        "holdout_execution_requires_candidate_locks",
        "holdout_execution_requires_separate_authorization",
        "public_bundle_activation_allowed",
    }, "authority policy")
    return AV1ValidationV2AuthorityPolicy(
        derivation_authorization_required=_required_bool(value.get("derivation_authorization_required")),
        derivation_scope=str(value.get("derivation_scope") or ""),
        guided_probe_allowed_during_derivation=_required_bool(value.get("guided_probe_allowed_during_derivation")),
        holdout_case_execution_allowed_during_derivation=_required_bool(
            value.get("holdout_case_execution_allowed_during_derivation")
        ),
        holdout_execution_requires_selection_lock=_required_bool(
            value.get("holdout_execution_requires_selection_lock")
        ),
        holdout_execution_requires_candidate_locks=_required_bool(
            value.get("holdout_execution_requires_candidate_locks")
        ),
        holdout_execution_requires_separate_authorization=_required_bool(
            value.get("holdout_execution_requires_separate_authorization")
        ),
        public_bundle_activation_allowed=_required_bool(value.get("public_bundle_activation_allowed")),
    )


def _plan_from_payload(value: Mapping[str, Any]) -> AV1ValidationV2CellPlan:
    _require_exact_keys(value, {
        "cell_plan_id",
        "name",
        "mode",
        "stratum",
        "intent_level",
        "optimization_objective",
        "trait_selector",
        "registered_case_count",
        "expected_prediction",
        "allowed_fallback_reasons",
        "target_confidence",
        "duplicate_review_ordinals",
        "eligibility_binding_id",
    }, "cell plan")
    selector_value = object_dict(value.get("trait_selector"))
    _require_exact_keys(selector_value, {"match", "traits"}, "plan trait selector")
    selector = AV1ValidationV2TraitSelector(
        cast(AV1ValidationV2Match, str(selector_value.get("match") or "")),
        tuple(sorted(str(trait) for trait in object_list(selector_value.get("traits")))),
    )
    return AV1ValidationV2CellPlan(
        cell_plan_id=str(value.get("cell_plan_id") or ""),
        name=str(value.get("name") or ""),
        mode=cast(AV1ValidationV2PlanMode, str(value.get("mode") or "")),
        stratum=str(value.get("stratum") or ""),
        intent_level=str(value.get("intent_level") or ""),
        optimization_objective=str(value.get("optimization_objective") or ""),
        trait_selector=selector,
        registered_case_count=int_value(value.get("registered_case_count")),
        expected_prediction=str(value.get("expected_prediction") or ""),
        allowed_fallback_reasons=tuple(
            sorted(str(reason) for reason in object_list(value.get("allowed_fallback_reasons")))
        ),
        target_confidence=str(value.get("target_confidence") or ""),
        duplicate_review_ordinals=tuple(
            sorted(int_value(ordinal) for ordinal in object_list(value.get("duplicate_review_ordinals")))
        ),
        eligibility_binding_id=str(value.get("eligibility_binding_id") or ""),
    )


def _excluded_from_payload(value: Mapping[str, Any]) -> AV1ValidationV2ExcludedCell:
    _require_exact_keys(value, {
        "excluded_cell_id",
        "name",
        "intent_level",
        "trait_selector",
        "reason",
        "disposition",
    }, "excluded cell")
    selector_value = object_dict(value.get("trait_selector"))
    _require_exact_keys(selector_value, {"match", "traits"}, "excluded trait selector")
    selector = AV1ValidationV2TraitSelector(
        cast(AV1ValidationV2Match, str(selector_value.get("match") or "")),
        tuple(sorted(str(trait) for trait in object_list(selector_value.get("traits")))),
    )
    return AV1ValidationV2ExcludedCell(
        excluded_cell_id=str(value.get("excluded_cell_id") or ""),
        name=str(value.get("name") or ""),
        intent_level=str(value.get("intent_level") or ""),
        trait_selector=selector,
        reason=str(value.get("reason") or ""),
        disposition=str(value.get("disposition") or ""),
    )


def _case_from_payload(value: Mapping[str, Any]) -> AV1ValidationV2Case:
    _require_exact_keys(
        value,
        {"case_id", "cell_plan_id", "ordinal", "execution_order"},
        "case",
    )
    return AV1ValidationV2Case(
        case_id=str(value.get("case_id") or ""),
        cell_plan_id=str(value.get("cell_plan_id") or ""),
        ordinal=int_value(value.get("ordinal")),
        execution_order=cast(AV1ValidationV2ExecutionOrder, str(value.get("execution_order") or "")),
    )


def _eligibility_binding_id(
        attestation_id: str,
        name: str,
        selector: AV1ValidationV2TraitSelector,
        registered_case_count: int,
) -> str:
    return f"av1vebinding2_{stable_json_hash({
        'attestation_id': attestation_id,
        'name': name,
        'trait_selector': selector.to_payload(),
        'registered_case_count': registered_case_count,
    })[:32]}"


def _v2_id(kind: str, payload: Mapping[str, Any]) -> str:
    prefixes = {
        "manifest": "av1vmanifest2",
        "feasibility": "av1vfeasibility2",
        "plan": "av1vplan2",
        "excluded": "av1vexcluded2",
        "case": "av1vcase2",
        "derivation_authorization": "av1vderivation1",
    }
    return f"{prefixes[kind]}_{stable_json_hash(payload)[:32]}"


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _require_sha256(value: str, label: str) -> None:
    if not value.startswith(_SHA256_PREFIX) or len(value) != len(_SHA256_PREFIX) + 64:
        raise AV1ValidationV2Error(f"AV1 v2 {label} is invalid")
    try:
        int(value[len(_SHA256_PREFIX):], 16)
    except ValueError as exc:
        raise AV1ValidationV2Error(f"AV1 v2 {label} is invalid") from exc


def _parse_timestamp(value: str, label: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AV1ValidationV2Error(f"AV1 v2 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV2Error(f"AV1 v2 {label} must include UTC")
    return parsed.astimezone(UTC)


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AV1ValidationV2Error("AV1 v2 boolean field is invalid")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise AV1ValidationV2Error(f"AV1 v2 {label} fields are unsupported")


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _assert_public_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationV2Error("AV1 v2 public payload is unsafe") from exc
