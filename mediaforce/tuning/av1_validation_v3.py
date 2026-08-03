from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    AV1_COLD_START_TRAITS,
    assert_av1_cold_start_public_payload_safe,
)
from mediaforce.tuning.av1_cold_start_evaluation import AV1ColdStartValidationCriteriaV1
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_REASON_CODES,
    AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES,
)


AV1_VALIDATION_V3_PROTOCOL_SCHEMA = "mediaforce.av1_cold_start_validation_protocol"
AV1_VALIDATION_V3_PROTOCOL_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_PROTOCOL_VERSION = 3
AV1_VALIDATION_V3_CONTRACT_VERSION = "acsv3"
AV1_VALIDATION_V3_EXPERIMENT_ID = "av1_cold_start_v3"
AV1_VALIDATION_V3_REGISTERED_AT = "2026-08-02T16:52:28Z"
AV1_VALIDATION_V3_VALID_UNTIL = "2027-01-29T16:52:28Z"
AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID = (
    "av1vmanifest2_f772824de3f113f8c8aca8dd2dd50749"
)
AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256 = (
    "sha256:5a24bbfdfe699aa2c6f037b9473ce06607aee20af743f57266f38bf7eb08d268"
)
AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY = "av1_v3_qualification"
AV1_VALIDATION_V3_EMPIRICAL_AUTHORITIES = frozenset({
    "av1_v3_derivation",
    "av1_v3_holdout",
})
AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE = "av1_v3_qualification"
AV1_VALIDATION_V3_EMPIRICAL_NAMESPACE = "av1_v3_empirical"
AV1_VALIDATION_V3_REQUIRED_DERIVATION_OBSERVATIONS = 12
AV1_VALIDATION_V3_DERIVATION_RESERVE_COUNT = 2
AV1_VALIDATION_V3_VOID_CAP_PER_CELL = 2
AV1_VALIDATION_V3_GLOBAL_STOP_THRESHOLD = 3
AV1_VALIDATION_V3_HOLDOUT_COUNT = 16
AV1_VALIDATION_V3_RANGE_HIT_MINIMUM = 13

AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES = frozenset({
    "runtime_crop_detection_failure",
    "runtime_preflight_failure",
    "runtime_source_snapshot_failure",
    "runtime_toolchain_failure",
})
AV1_VALIDATION_V3_AT_OR_POST_MEASUREMENT_TERMINALS = frozenset({
    "compatibility_drift",
    "metrics_incomplete",
    "quality_floor_miss",
    "runtime_cleanup_failure",
    "runtime_quality_search_failure",
    "runtime_result_validation_failure",
    "runtime_review_generation_failure",
    "runtime_sample_encode_failure",
    "storage_stop",
    "timeout",
    "toolchain_drift",
})
AV1_VALIDATION_V3_STAGE_AMBIGUOUS_TERMINALS = frozenset({
    "authorization_expired",
    "interrupted_claim",
})
AV1_VALIDATION_V3_ALWAYS_NONVOIDABLE_TERMINALS = frozenset({
    "content_intent_observation_excluded",
    "media_unavailable",
    "operator_stop",
    "safety_stop",
})
AV1_VALIDATION_V3_EXECUTOR_STAGES = (
    "preflight",
    "source_snapshot",
    "toolchain",
    "crop_detection",
    "quality_search",
    "sample_encode",
    "result_validation",
    "review_generation",
    "cleanup",
)
AV1_VALIDATION_V3_RECOVERY_PATHS = (
    "late_observed_intent",
    "orphan_assignment_claim",
    "recovery_required_exit_2",
    "review_claim_without_envelope",
    "rollback",
    "terminal_intent_without_record",
    "unaccepted_attempt",
    "verdict_claim_without_intent",
)
AV1_VALIDATION_V3_GATE_SEQUENCE = ("A0", "A", "B", "C", "D", "E", "F")
AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS: tuple[int | None, ...] = (
    None,
    None,
    None,
    None,
    None,
    None,
    6,
    7,
    8,
    8,
    9,
    10,
    10,
    11,
    12,
    12,
    13,
)
AV1_VALIDATION_V3_POWER_RATES = (
    0.50,
    0.60,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
)

if (
    AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS[AV1_VALIDATION_V3_HOLDOUT_COUNT]
    != AV1_VALIDATION_V3_RANGE_HIT_MINIMUM
):
    raise RuntimeError("AV1 v3 holdout thresholds are inconsistent")

AV1ValidationV3TerminalDisposition = Literal[
    "assignment_void",
    "cell_terminal",
    "protocol_nonconformance",
]

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SAFE_KIND_RE = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}\Z")


class AV1ValidationV3Error(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3CandidateCell:
    cell_id: str
    name: str
    intent_level: str
    exact_traits: tuple[str, ...]
    holdout_count: int
    range_hit_minimum: int
    duplicate_review_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.intent_level != "balanced":
            raise AV1ValidationV3Error("AV1 v3 candidate intent is unsupported")
        _validate_traits(self.exact_traits, "candidate")
        if self.holdout_count != AV1_VALIDATION_V3_HOLDOUT_COUNT:
            raise AV1ValidationV3Error("AV1 v3 holdout count does not match the frozen protocol")
        if self.range_hit_minimum != AV1_VALIDATION_V3_RANGE_HIT_MINIMUM:
            raise AV1ValidationV3Error("AV1 v3 range-hit minimum does not match the frozen protocol")
        if self.duplicate_review_ordinals != (4, 8, 12, 16):
            raise AV1ValidationV3Error("AV1 v3 duplicate-review ordinals do not match the frozen protocol")
        if self.cell_id != av1_validation_v3_id("cell", self.semantic_payload()):
            raise AV1ValidationV3Error("AV1 v3 candidate cell ID does not match its payload")

    def matches(self, *, intent_level: str, traits: Sequence[str]) -> bool:
        return intent_level == self.intent_level and tuple(sorted(set(traits))) == self.exact_traits

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intent_level": self.intent_level,
            "optimization_objective": AV1_COLD_START_INTENT_OBJECTIVES[self.intent_level],
            "trait_selector": {"match": "exact", "traits": list(self.exact_traits)},
            "holdout_count": self.holdout_count,
            "range_hit_minimum": self.range_hit_minimum,
            "duplicate_review_ordinals": list(self.duplicate_review_ordinals),
        }

    def to_payload(self) -> dict[str, Any]:
        return {"cell_id": self.cell_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2Stratum:
    stratum_id: str
    name: str
    intent_level: str
    exact_traits: tuple[str, ...]
    source_slots: int
    candidate_configurations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.intent_level != "balanced":
            raise AV1ValidationV3Error("AV1 v3 Tier 2 intent is unsupported")
        _validate_traits(self.exact_traits, "Tier 2")
        if self.source_slots != 1:
            raise AV1ValidationV3Error("AV1 v3 Tier 2 source slots do not match the frozen protocol")
        if self.candidate_configurations != (
            "darkness_balanced_candidate",
            "motion_balanced_candidate",
        ):
            raise AV1ValidationV3Error("AV1 v3 Tier 2 candidate coverage is incomplete")
        if self.stratum_id != av1_validation_v3_id("qstratum", self.semantic_payload()):
            raise AV1ValidationV3Error("AV1 v3 Tier 2 stratum ID does not match its payload")

    def matches(self, source: AV1ValidationV3QualificationSource) -> bool:
        return source.intent_level == self.intent_level and source.exact_traits == self.exact_traits

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intent_level": self.intent_level,
            "trait_selector": {"match": "exact", "traits": list(self.exact_traits)},
            "source_slots": self.source_slots,
            "candidate_configurations": list(self.candidate_configurations),
        }

    def to_payload(self) -> dict[str, Any]:
        return {"stratum_id": self.stratum_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationV3PowerPoint:
    true_rate: float
    component_power: float

    def __post_init__(self) -> None:
        if self.true_rate not in AV1_VALIDATION_V3_POWER_RATES:
            raise AV1ValidationV3Error("AV1 v3 power-table rate is not frozen")
        expected = round(_binomial_tail_probability(
            trials=AV1_VALIDATION_V3_HOLDOUT_COUNT,
            minimum_successes=AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
            success_probability=self.true_rate,
        ), 9)
        if self.component_power != expected:
            raise AV1ValidationV3Error("AV1 v3 component power does not match its exact binomial value")

    def to_payload(self) -> dict[str, Any]:
        return {
            "true_rate": self.true_rate,
            "component_power": self.component_power,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationProtocolV3:
    protocol_id: str
    experiment_id: str
    supersedes_manifest_id: str
    supersedes_payload_sha256: str
    registered_at: str
    valid_until: str
    criteria: AV1ColdStartValidationCriteriaV1
    candidate_cells: tuple[AV1ValidationV3CandidateCell, ...]
    tier2_strata: tuple[AV1ValidationV3Tier2Stratum, ...]
    power_table: tuple[AV1ValidationV3PowerPoint, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.experiment_id != AV1_VALIDATION_V3_EXPERIMENT_ID:
            raise AV1ValidationV3Error("AV1 v3 experiment identity is invalid")
        if self.supersedes_manifest_id != AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID:
            raise AV1ValidationV3Error("AV1 v3 superseded manifest is invalid")
        if self.supersedes_payload_sha256 != AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256:
            raise AV1ValidationV3Error("AV1 v3 superseded manifest digest is invalid")
        registered = _parse_timestamp(self.registered_at, "protocol registration")
        valid_until = _parse_timestamp(self.valid_until, "protocol expiration")
        if valid_until <= registered:
            raise AV1ValidationV3Error("AV1 v3 protocol expiration must follow registration")
        if self.candidate_cells != tuple(sorted(self.candidate_cells, key=lambda cell: cell.name)):
            raise AV1ValidationV3Error("AV1 v3 candidate cells are not canonical")
        if self.tier2_strata != tuple(sorted(self.tier2_strata, key=lambda stratum: stratum.name)):
            raise AV1ValidationV3Error("AV1 v3 Tier 2 strata are not canonical")
        if tuple(point.true_rate for point in self.power_table) != AV1_VALIDATION_V3_POWER_RATES:
            raise AV1ValidationV3Error("AV1 v3 power table is incomplete")
        _validate_protocol_contract(self)
        semantic_payload = self.semantic_payload()
        if self.protocol_id != av1_validation_v3_id("protocol", semantic_payload):
            raise AV1ValidationV3Error("AV1 v3 protocol ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({
            "protocol_id": self.protocol_id,
            **semantic_payload,
        }):
            raise AV1ValidationV3Error("AV1 v3 protocol digest does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_PROTOCOL_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_PROTOCOL_SCHEMA_VERSION,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": self.experiment_id,
            "contract_version": AV1_VALIDATION_V3_CONTRACT_VERSION,
            "governance_issue": "#302",
            "implementation_issue": "#303",
            "supersedes_manifest_id": self.supersedes_manifest_id,
            "supersedes_payload_sha256": self.supersedes_payload_sha256,
            "state": "owner_approved_non_executing",
            "authority": "av1_v3_protocol",
            "artifact_namespace": "av1_v3_protocol",
            "evidence_eligible": False,
            "runtime_execution_authorized": False,
            "qualification_execution_authorized": False,
            "private_inventory_read_authorized": False,
            "key_creation_authorized": False,
            "partition_construction_authorized": False,
            "derivation_execution_authorized": False,
            "holdout_execution_authorized": False,
            "public_bundle_activation_allowed": False,
            "registered_at": self.registered_at,
            "valid_until": self.valid_until,
            "criteria": self.criteria.to_payload(),
            "candidate_cells": [cell.to_payload() for cell in self.candidate_cells],
            "tier2_qualification": {
                "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
                "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
                "evidence_eligible": False,
                "ranking_algorithm": "hmac_sha256_lowest_v1",
                "ranking_domain": av1_validation_v3_hmac_domain("tier2:qualification-rank"),
                "qualification_key_distinct_from_empirical_key": True,
                "selection_after_a0": True,
                "redraw_or_substitution_allowed": False,
                "private_fault_injection_allowed": False,
                "strata": [stratum.to_payload() for stratum in self.tier2_strata],
            },
            "qualification_matrix": {
                "success_configurations": [cell.name for cell in self.candidate_cells],
                "executor_stages": list(AV1_VALIDATION_V3_EXECUTOR_STAGES),
                "runtime_failure_reason_codes": sorted(
                    AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES
                ),
                "terminal_reason_codes": sorted(AV1_VALIDATION_DERIVATION_REASON_CODES),
                "recovery_paths": list(AV1_VALIDATION_V3_RECOVERY_PATHS),
                "success_streak_proxy_allowed": False,
            },
            "partition_policy": {
                "holdout_first_single_event": True,
                "required_derivation_observations_per_cell": (
                    AV1_VALIDATION_V3_REQUIRED_DERIVATION_OBSERVATIONS
                ),
                "derivation_reserve_count_per_cell": (
                    AV1_VALIDATION_V3_DERIVATION_RESERVE_COUNT
                ),
                "derivation_assignment_count_per_cell": (
                    AV1_VALIDATION_V3_REQUIRED_DERIVATION_OBSERVATIONS
                    + AV1_VALIDATION_V3_DERIVATION_RESERVE_COUNT
                ),
                "holdout_count_per_cell": AV1_VALIDATION_V3_HOLDOUT_COUNT,
                "void_cap_per_cell": AV1_VALIDATION_V3_VOID_CAP_PER_CELL,
                "global_same_stage_stop_threshold": (
                    AV1_VALIDATION_V3_GLOBAL_STOP_THRESHOLD
                ),
                "inventory_dependent_resize_allowed": False,
                "retry_substitution_backfill_allowed": False,
            },
            "terminal_policy": {
                "pre_measurement_runtime_failures": sorted(
                    AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES
                ),
                "at_or_post_measurement_terminals": sorted(
                    AV1_VALIDATION_V3_AT_OR_POST_MEASUREMENT_TERMINALS
                ),
                "stage_ambiguous_terminals": sorted(
                    AV1_VALIDATION_V3_STAGE_AMBIGUOUS_TERMINALS
                ),
                "always_nonvoidable_terminals": sorted(
                    AV1_VALIDATION_V3_ALWAYS_NONVOIDABLE_TERMINALS
                ),
                "unknown_reason_default": "cell_terminal",
                "runtime_failure_disposition": "protocol_nonconformance",
                "technically_infeasible_is_evidence": False,
            },
            "holdout_policy": {
                "count_per_cell": AV1_VALIDATION_V3_HOLDOUT_COUNT,
                "range_hit_minimum": AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
                "range_hit_null_rate": 0.5,
                "maximum_one_sided_p_value": 0.025,
                "sign_test_minimum_wins_by_non_tied_count": [
                    {
                        "non_tied_count": non_tied_count,
                        "minimum_wins": minimum_wins,
                    }
                    for non_tied_count, minimum_wins in enumerate(
                        AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS
                    )
                ],
                "tests_form_intersection_union": True,
                "component_power_only": True,
                "joint_power_claim_allowed": False,
                "power_table": [point.to_payload() for point in self.power_table],
                "fallback_conformance_disposition": "activation_only_inherited_guard",
            },
            "evidence_policy": {
                "qualification_authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
                "qualification_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
                "empirical_authorities": sorted(AV1_VALIDATION_V3_EMPIRICAL_AUTHORITIES),
                "empirical_namespace": AV1_VALIDATION_V3_EMPIRICAL_NAMESPACE,
                "evidence_eligible_field_required": True,
                "missing_evidence_eligible_is_false": True,
                "namespace_and_marker_independently_enforced": True,
                "public_summary_policy": "totals_and_pass_fail_only",
            },
            "chronology_policy": {
                "predicate_freeze_before_qualification_key_commitment": True,
                "qualification_key_commitment_before_tier2_selection": True,
                "qualification_acceptance_before_empirical_key_commitment": True,
                "empirical_key_commitment_before_eligibility_cutoff": True,
                "eligibility_cutoff_not_after_partition_selection": True,
                "partition_selection_before_expiration": True,
            },
            "gate_sequence": list(AV1_VALIDATION_V3_GATE_SEQUENCE),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationSource:
    source_fingerprint: str
    intent_level: str
    exact_traits: tuple[str, ...]
    pipeline_ready: bool

    def __post_init__(self) -> None:
        _require_sha256(self.source_fingerprint, "qualification source fingerprint")
        if self.intent_level not in AV1_COLD_START_INTENT_OBJECTIVES:
            raise AV1ValidationV3Error("AV1 v3 qualification source intent is unsupported")
        _validate_traits(self.exact_traits, "qualification source")


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationSelection:
    stratum_id: str
    stratum_name: str
    source_fingerprint: str
    rank_sha256: str

    def __post_init__(self) -> None:
        if not self.stratum_id.startswith("av1vqstratum3_"):
            raise AV1ValidationV3Error("AV1 v3 qualification selection stratum is invalid")
        _require_sha256(self.source_fingerprint, "qualification source fingerprint")
        _require_sha256(self.rank_sha256, "qualification rank")

    def to_owner_payload(self) -> dict[str, Any]:
        return {
            "stratum_id": self.stratum_id,
            "stratum_name": self.stratum_name,
            "source_fingerprint": self.source_fingerprint,
            "rank_sha256": self.rank_sha256,
        }

    def to_public_payload(self) -> dict[str, Any]:
        return {
            "stratum_name": self.stratum_name,
            "selected_source_count": 1,
            "runtime_execution_authorized": False,
            "evidence_eligible": False,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Chronology:
    predicate_frozen_at: str
    qualification_key_committed_at: str
    tier2_selected_at: str
    qualification_accepted_at: str
    empirical_key_committed_at: str
    eligibility_attestation_cutoff: str
    partition_selected_at: str
    valid_until: str

    def __post_init__(self) -> None:
        timestamps = tuple(
            _parse_timestamp(value, label)
            for value, label in (
                (self.predicate_frozen_at, "predicate freeze"),
                (self.qualification_key_committed_at, "qualification key commitment"),
                (self.tier2_selected_at, "Tier 2 selection"),
                (self.qualification_accepted_at, "qualification acceptance"),
                (self.empirical_key_committed_at, "empirical key commitment"),
                (self.eligibility_attestation_cutoff, "eligibility cutoff"),
                (self.partition_selected_at, "partition selection"),
                (self.valid_until, "protocol expiration"),
            )
        )
        if not (
            timestamps[0]
            <= timestamps[1]
            < timestamps[2]
            < timestamps[3]
            < timestamps[4]
            < timestamps[5]
            <= timestamps[6]
            < timestamps[7]
        ):
            raise AV1ValidationV3Error("AV1 v3 chronology is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "predicate_frozen_at": self.predicate_frozen_at,
            "qualification_key_committed_at": self.qualification_key_committed_at,
            "tier2_selected_at": self.tier2_selected_at,
            "qualification_accepted_at": self.qualification_accepted_at,
            "empirical_key_committed_at": self.empirical_key_committed_at,
            "eligibility_attestation_cutoff": self.eligibility_attestation_cutoff,
            "partition_selected_at": self.partition_selected_at,
            "valid_until": self.valid_until,
        }


def build_preregistered_av1_validation_protocol_v3() -> AV1ValidationProtocolV3:
    candidate_cells = tuple(sorted((
        _candidate_cell("darkness_balanced_candidate", ("darkness",)),
        _candidate_cell("motion_balanced_candidate", ("motion",)),
    ), key=lambda cell: cell.name))
    candidate_names = tuple(cell.name for cell in candidate_cells)
    tier2_strata = tuple(sorted((
        _tier2_stratum(
            "animation_balanced_qualification",
            ("animation",),
            candidate_names,
        ),
        _tier2_stratum(
            "typical_balanced_qualification",
            ("typical",),
            candidate_names,
        ),
    ), key=lambda stratum: stratum.name))
    power_table = tuple(
        AV1ValidationV3PowerPoint(
            true_rate=true_rate,
            component_power=round(_binomial_tail_probability(
                trials=AV1_VALIDATION_V3_HOLDOUT_COUNT,
                minimum_successes=AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
                success_probability=true_rate,
            ), 9),
        )
        for true_rate in AV1_VALIDATION_V3_POWER_RATES
    )
    criteria = AV1ColdStartValidationCriteriaV1(
        minimum_derivation_evidence_count=12,
        minimum_derivation_source_count=6,
        minimum_holdout_count=16,
        minimum_holdout_source_count=6,
        maximum_derivation_age_days=180,
        maximum_candidate_crf_span=6.0,
        range_hit_null_rate=0.5,
        maximum_one_sided_p_value=0.025,
        maximum_safety_regression_count=0,
        duplicate_review_fraction=0.2,
        minimum_duplicate_review_agreement=0.8,
        confidence_level="moderate",
        confidence_score=0.7,
    )
    placeholder = AV1ValidationProtocolV3.__new__(AV1ValidationProtocolV3)
    object.__setattr__(placeholder, "protocol_id", "")
    object.__setattr__(placeholder, "experiment_id", AV1_VALIDATION_V3_EXPERIMENT_ID)
    object.__setattr__(placeholder, "supersedes_manifest_id", AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID)
    object.__setattr__(
        placeholder,
        "supersedes_payload_sha256",
        AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256,
    )
    object.__setattr__(placeholder, "registered_at", AV1_VALIDATION_V3_REGISTERED_AT)
    object.__setattr__(placeholder, "valid_until", AV1_VALIDATION_V3_VALID_UNTIL)
    object.__setattr__(placeholder, "criteria", criteria)
    object.__setattr__(placeholder, "candidate_cells", candidate_cells)
    object.__setattr__(placeholder, "tier2_strata", tier2_strata)
    object.__setattr__(placeholder, "power_table", power_table)
    object.__setattr__(placeholder, "payload_sha256", "")
    semantic_payload = placeholder.semantic_payload()
    protocol_id = av1_validation_v3_id("protocol", semantic_payload)
    return AV1ValidationProtocolV3(
        protocol_id=protocol_id,
        experiment_id=AV1_VALIDATION_V3_EXPERIMENT_ID,
        supersedes_manifest_id=AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID,
        supersedes_payload_sha256=AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256,
        registered_at=AV1_VALIDATION_V3_REGISTERED_AT,
        valid_until=AV1_VALIDATION_V3_VALID_UNTIL,
        criteria=criteria,
        candidate_cells=candidate_cells,
        tier2_strata=tier2_strata,
        power_table=power_table,
        payload_sha256=_payload_sha256({"protocol_id": protocol_id, **semantic_payload}),
    )


def assert_preregistered_av1_validation_protocol_v3(
    protocol: AV1ValidationProtocolV3,
) -> None:
    if protocol != build_preregistered_av1_validation_protocol_v3():
        raise AV1ValidationV3Error(
            "AV1 v3 protocol does not match the owner-approved issue #302 contract"
        )


def assert_av1_validation_v3_protocol_active(
    protocol: AV1ValidationProtocolV3,
    *,
    as_of: str,
) -> None:
    assert_preregistered_av1_validation_protocol_v3(protocol)
    checked_at = _parse_timestamp(as_of, "protocol active-check timestamp")
    registered_at = _parse_timestamp(protocol.registered_at, "protocol registration")
    valid_until = _parse_timestamp(protocol.valid_until, "protocol expiration")
    if checked_at < registered_at or checked_at >= valid_until:
        raise AV1ValidationV3Error("AV1 v3 protocol is not active at the requested time")


def serialize_av1_validation_protocol_v3(protocol: AV1ValidationProtocolV3) -> bytes:
    return canonical_json_bytes(protocol.to_payload()) + b"\n"


def load_av1_validation_protocol_v3(path: Path) -> AV1ValidationProtocolV3:
    raw = path.read_bytes()
    try:
        protocol = av1_validation_protocol_v3_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Error("AV1 v3 protocol is invalid") from exc
    if raw != serialize_av1_validation_protocol_v3(protocol):
        raise AV1ValidationV3Error("AV1 v3 protocol bytes are not canonical")
    return protocol


def av1_validation_protocol_v3_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationProtocolV3:
    value = object_dict(payload)
    expected = {
        "protocol_id",
        "schema",
        "schema_version",
        "protocol_version",
        "experiment_id",
        "contract_version",
        "governance_issue",
        "implementation_issue",
        "supersedes_manifest_id",
        "supersedes_payload_sha256",
        "state",
        "authority",
        "artifact_namespace",
        "evidence_eligible",
        "runtime_execution_authorized",
        "qualification_execution_authorized",
        "private_inventory_read_authorized",
        "key_creation_authorized",
        "partition_construction_authorized",
        "derivation_execution_authorized",
        "holdout_execution_authorized",
        "public_bundle_activation_allowed",
        "registered_at",
        "valid_until",
        "criteria",
        "candidate_cells",
        "tier2_qualification",
        "qualification_matrix",
        "partition_policy",
        "terminal_policy",
        "holdout_policy",
        "evidence_policy",
        "chronology_policy",
        "gate_sequence",
        "payload_sha256",
    }
    _require_exact_keys(value, expected, "protocol")
    if (
        value.get("schema") != AV1_VALIDATION_V3_PROTOCOL_SCHEMA
        or int_value(value.get("schema_version"))
        != AV1_VALIDATION_V3_PROTOCOL_SCHEMA_VERSION
        or int_value(value.get("protocol_version")) != AV1_VALIDATION_V3_PROTOCOL_VERSION
        or value.get("contract_version") != AV1_VALIDATION_V3_CONTRACT_VERSION
    ):
        raise AV1ValidationV3Error("AV1 v3 protocol contract is unsupported")
    candidate_cells = tuple(
        _candidate_cell_from_payload(object_dict(item))
        for item in object_list(value.get("candidate_cells"))
    )
    tier2_payload = object_dict(value.get("tier2_qualification"))
    tier2_strata = tuple(
        _tier2_stratum_from_payload(object_dict(item))
        for item in object_list(tier2_payload.get("strata"))
    )
    holdout_policy = object_dict(value.get("holdout_policy"))
    power_table = tuple(
        AV1ValidationV3PowerPoint(
            true_rate=float_value(object_dict(item).get("true_rate")),
            component_power=float_value(object_dict(item).get("component_power")),
        )
        for item in object_list(holdout_policy.get("power_table"))
    )
    criteria_value = object_dict(value.get("criteria"))
    criteria = AV1ColdStartValidationCriteriaV1(
        minimum_derivation_evidence_count=int_value(
            criteria_value.get("minimum_derivation_evidence_count")
        ),
        minimum_derivation_source_count=int_value(
            criteria_value.get("minimum_derivation_source_count")
        ),
        minimum_holdout_count=int_value(criteria_value.get("minimum_holdout_count")),
        minimum_holdout_source_count=int_value(
            criteria_value.get("minimum_holdout_source_count")
        ),
        maximum_derivation_age_days=int_value(
            criteria_value.get("maximum_derivation_age_days")
        ),
        maximum_candidate_crf_span=float_value(
            criteria_value.get("maximum_candidate_crf_span")
        ),
        range_hit_null_rate=float_value(criteria_value.get("range_hit_null_rate")),
        maximum_one_sided_p_value=float_value(
            criteria_value.get("maximum_one_sided_p_value")
        ),
        maximum_safety_regression_count=int_value(
            criteria_value.get("maximum_safety_regression_count")
        ),
        duplicate_review_fraction=float_value(
            criteria_value.get("duplicate_review_fraction")
        ),
        minimum_duplicate_review_agreement=float_value(
            criteria_value.get("minimum_duplicate_review_agreement")
        ),
        confidence_level=str(criteria_value.get("confidence_level") or ""),
        confidence_score=float_value(criteria_value.get("confidence_score")),
    )
    protocol = AV1ValidationProtocolV3(
        protocol_id=str(value.get("protocol_id") or ""),
        experiment_id=str(value.get("experiment_id") or ""),
        supersedes_manifest_id=str(value.get("supersedes_manifest_id") or ""),
        supersedes_payload_sha256=str(value.get("supersedes_payload_sha256") or ""),
        registered_at=str(value.get("registered_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        criteria=criteria,
        candidate_cells=candidate_cells,
        tier2_strata=tier2_strata,
        power_table=power_table,
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != protocol.to_payload():
        raise AV1ValidationV3Error("AV1 v3 protocol payload is not exact")
    return protocol


def av1_validation_v3_id(kind: str, payload: object) -> str:
    if not _SAFE_KIND_RE.fullmatch(kind):
        raise AV1ValidationV3Error("AV1 v3 ID kind is invalid")
    digest = stable_json_hash({
        "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
        "kind": kind,
        "payload": payload,
    })
    return f"av1v{kind}3_{digest[:32]}"


def av1_validation_v3_hmac_domain(purpose: str) -> str:
    if not _SAFE_TOKEN_RE.fullmatch(purpose):
        raise AV1ValidationV3Error("AV1 v3 HMAC purpose is invalid")
    return (
        f"mediaforce:av1:protocol:{AV1_VALIDATION_V3_PROTOCOL_VERSION}:"
        f"{AV1_VALIDATION_V3_EXPERIMENT_ID}:{purpose}"
    )


def av1_validation_v3_qualification_key_id(qualification_key: bytes) -> str:
    _validate_key(qualification_key)
    digest = hmac.new(
        _derived_key(qualification_key, "tier2:qualification-key-id"),
        b"mediaforce-av1-v3-tier2-qualification-key",
        hashlib.sha256,
    ).hexdigest()
    return f"av1vqkey3_{digest[:32]}"


def select_av1_validation_v3_tier2_sources(
    *,
    protocol: AV1ValidationProtocolV3,
    sources: Sequence[AV1ValidationV3QualificationSource],
    qualification_key: bytes,
    expected_key_id: str,
) -> tuple[AV1ValidationV3QualificationSelection, ...]:
    assert_preregistered_av1_validation_protocol_v3(protocol)
    _validate_key(qualification_key)
    if av1_validation_v3_qualification_key_id(qualification_key) != expected_key_id:
        raise AV1ValidationV3Error("AV1 v3 qualification key does not match its commitment")
    fingerprints = [source.source_fingerprint for source in sources]
    if len(fingerprints) != len(set(fingerprints)):
        raise AV1ValidationV3Error("AV1 v3 qualification inventory repeats a source fingerprint")
    for source in sources:
        matching_strata = tuple(
            stratum
            for stratum in protocol.tier2_strata
            if stratum.matches(source)
        )
        if (
            len(matching_strata) != 1
            or not source.pipeline_ready
            or not _source_is_structurally_ineligible(protocol, source)
        ):
            raise AV1ValidationV3Error(
                "AV1 v3 qualification inventory contains an out-of-scope source"
            )
    selections: list[AV1ValidationV3QualificationSelection] = []
    for stratum in protocol.tier2_strata:
        candidates = tuple(
            source
            for source in sources
            if stratum.matches(source)
        )
        if len(candidates) < stratum.source_slots:
            raise AV1ValidationV3Error(
                f"AV1 v3 Tier 2 stratum {stratum.name!r} cannot fill its frozen slot"
            )
        ranked = sorted(
            (
                _qualification_rank(
                    qualification_key=qualification_key,
                    stratum=stratum,
                    source=source,
                ),
                source.source_fingerprint,
            )
            for source in candidates
        )
        rank_hex, source_fingerprint = ranked[0]
        selections.append(AV1ValidationV3QualificationSelection(
            stratum_id=stratum.stratum_id,
            stratum_name=stratum.name,
            source_fingerprint=source_fingerprint,
            rank_sha256=f"sha256:{rank_hex}",
        ))
    return tuple(sorted(selections, key=lambda selection: selection.stratum_name))


def assert_av1_validation_v3_artifact_namespace(
    payload: Mapping[str, Any],
    *,
    evidence_required: bool,
) -> None:
    value = object_dict(payload)
    _require_protocol_identity(value)
    authority = str(value.get("authority") or "")
    namespace = str(value.get("artifact_namespace") or "")
    if evidence_required:
        if (
            authority not in AV1_VALIDATION_V3_EMPIRICAL_AUTHORITIES
            or namespace != AV1_VALIDATION_V3_EMPIRICAL_NAMESPACE
        ):
            raise AV1ValidationV3Error("AV1 v3 evidence artifact namespace is ineligible")
        return
    if (
        authority != AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY
        or namespace != AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE
    ):
        raise AV1ValidationV3Error("AV1 v3 qualification artifact namespace is invalid")


def assert_av1_validation_v3_evidence_marker(
    payload: Mapping[str, Any],
    *,
    evidence_required: bool,
) -> None:
    value = object_dict(payload)
    marker = value.get("evidence_eligible")
    if marker is not evidence_required:
        raise AV1ValidationV3Error("AV1 v3 evidence eligibility marker is invalid")


def assert_av1_validation_v3_evidence_artifact(payload: Mapping[str, Any]) -> None:
    assert_av1_validation_v3_artifact_namespace(payload, evidence_required=True)
    assert_av1_validation_v3_evidence_marker(payload, evidence_required=True)


def av1_validation_v3_terminal_disposition(
    reason_code: str,
    *,
    machine_emitted: bool,
    content_measurement_started: bool,
    review_media_exists: bool,
) -> AV1ValidationV3TerminalDisposition:
    if reason_code == "runtime_failure":
        return "protocol_nonconformance"
    if (
        reason_code
        in (
            AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES
            | AV1_VALIDATION_V3_STAGE_AMBIGUOUS_TERMINALS
        )
        and machine_emitted
        and not content_measurement_started
        and not review_media_exists
    ):
        return "assignment_void"
    return "cell_terminal"


def av1_validation_v3_global_stop_required(
    *,
    reason_code: str,
    global_void_count: int,
) -> bool:
    if reason_code not in AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES:
        raise AV1ValidationV3Error("AV1 v3 global stop requires a pre-measurement stage code")
    if global_void_count < 0:
        raise AV1ValidationV3Error("AV1 v3 global void count cannot be negative")
    return global_void_count >= AV1_VALIDATION_V3_GLOBAL_STOP_THRESHOLD


def _candidate_cell(name: str, exact_traits: tuple[str, ...]) -> AV1ValidationV3CandidateCell:
    semantic_payload = {
        "name": name,
        "intent_level": "balanced",
        "optimization_objective": AV1_COLD_START_INTENT_OBJECTIVES["balanced"],
        "trait_selector": {"match": "exact", "traits": list(exact_traits)},
        "holdout_count": AV1_VALIDATION_V3_HOLDOUT_COUNT,
        "range_hit_minimum": AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
        "duplicate_review_ordinals": [4, 8, 12, 16],
    }
    return AV1ValidationV3CandidateCell(
        cell_id=av1_validation_v3_id("cell", semantic_payload),
        name=name,
        intent_level="balanced",
        exact_traits=exact_traits,
        holdout_count=AV1_VALIDATION_V3_HOLDOUT_COUNT,
        range_hit_minimum=AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
        duplicate_review_ordinals=(4, 8, 12, 16),
    )


def _tier2_stratum(
    name: str,
    exact_traits: tuple[str, ...],
    candidate_configurations: tuple[str, ...],
) -> AV1ValidationV3Tier2Stratum:
    semantic_payload = {
        "name": name,
        "intent_level": "balanced",
        "trait_selector": {"match": "exact", "traits": list(exact_traits)},
        "source_slots": 1,
        "candidate_configurations": list(candidate_configurations),
    }
    return AV1ValidationV3Tier2Stratum(
        stratum_id=av1_validation_v3_id("qstratum", semantic_payload),
        name=name,
        intent_level="balanced",
        exact_traits=exact_traits,
        source_slots=1,
        candidate_configurations=candidate_configurations,
    )


def _candidate_cell_from_payload(payload: Mapping[str, Any]) -> AV1ValidationV3CandidateCell:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "cell_id",
        "name",
        "intent_level",
        "optimization_objective",
        "trait_selector",
        "holdout_count",
        "range_hit_minimum",
        "duplicate_review_ordinals",
    }, "candidate cell")
    selector = object_dict(value.get("trait_selector"))
    if selector.get("match") != "exact":
        raise AV1ValidationV3Error("AV1 v3 candidate selector is not exact")
    intent_level = str(value.get("intent_level") or "")
    if value.get("optimization_objective") != AV1_COLD_START_INTENT_OBJECTIVES.get(intent_level):
        raise AV1ValidationV3Error("AV1 v3 candidate objective does not match intent")
    return AV1ValidationV3CandidateCell(
        cell_id=str(value.get("cell_id") or ""),
        name=str(value.get("name") or ""),
        intent_level=intent_level,
        exact_traits=tuple(str(item) for item in object_list(selector.get("traits"))),
        holdout_count=int_value(value.get("holdout_count")),
        range_hit_minimum=int_value(value.get("range_hit_minimum")),
        duplicate_review_ordinals=tuple(
            int_value(item) for item in object_list(value.get("duplicate_review_ordinals"))
        ),
    )


def _tier2_stratum_from_payload(payload: Mapping[str, Any]) -> AV1ValidationV3Tier2Stratum:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "stratum_id",
        "name",
        "intent_level",
        "trait_selector",
        "source_slots",
        "candidate_configurations",
    }, "Tier 2 stratum")
    selector = object_dict(value.get("trait_selector"))
    if selector.get("match") != "exact":
        raise AV1ValidationV3Error("AV1 v3 Tier 2 selector is not exact")
    return AV1ValidationV3Tier2Stratum(
        stratum_id=str(value.get("stratum_id") or ""),
        name=str(value.get("name") or ""),
        intent_level=str(value.get("intent_level") or ""),
        exact_traits=tuple(str(item) for item in object_list(selector.get("traits"))),
        source_slots=int_value(value.get("source_slots")),
        candidate_configurations=tuple(
            str(item) for item in object_list(value.get("candidate_configurations"))
        ),
    )


def _validate_protocol_contract(protocol: AV1ValidationProtocolV3) -> None:
    if {cell.name: cell.exact_traits for cell in protocol.candidate_cells} != {
        "darkness_balanced_candidate": ("darkness",),
        "motion_balanced_candidate": ("motion",),
    }:
        raise AV1ValidationV3Error("AV1 v3 candidate cells do not match the frozen protocol")
    if {stratum.name: stratum.exact_traits for stratum in protocol.tier2_strata} != {
        "animation_balanced_qualification": ("animation",),
        "typical_balanced_qualification": ("typical",),
    }:
        raise AV1ValidationV3Error("AV1 v3 Tier 2 strata do not match the frozen protocol")
    for stratum in protocol.tier2_strata:
        synthetic_source = AV1ValidationV3QualificationSource(
            source_fingerprint=f"sha256:{'0' * 64}",
            intent_level=stratum.intent_level,
            exact_traits=stratum.exact_traits,
            pipeline_ready=True,
        )
        if not _source_is_structurally_ineligible(protocol, synthetic_source):
            raise AV1ValidationV3Error("AV1 v3 Tier 2 stratum overlaps a powered candidate")
    covered_reasons = (
        AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES
        | AV1_VALIDATION_V3_AT_OR_POST_MEASUREMENT_TERMINALS
        | AV1_VALIDATION_V3_STAGE_AMBIGUOUS_TERMINALS
        | AV1_VALIDATION_V3_ALWAYS_NONVOIDABLE_TERMINALS
        | {"runtime_failure"}
    )
    if covered_reasons != AV1_VALIDATION_DERIVATION_REASON_CODES:
        raise AV1ValidationV3Error("AV1 v3 terminal policy is not exhaustive")
    if not AV1_VALIDATION_V3_PRE_MEASUREMENT_RUNTIME_FAILURES.issubset(
        AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES
    ):
        raise AV1ValidationV3Error("AV1 v3 pre-measurement runtime policy is invalid")


def _source_is_structurally_ineligible(
    protocol: AV1ValidationProtocolV3,
    source: AV1ValidationV3QualificationSource,
) -> bool:
    return not any(
        cell.matches(intent_level=source.intent_level, traits=source.exact_traits)
        for cell in protocol.candidate_cells
    )


def _qualification_rank(
    *,
    qualification_key: bytes,
    stratum: AV1ValidationV3Tier2Stratum,
    source: AV1ValidationV3QualificationSource,
) -> str:
    return hmac.new(
        _derived_key(qualification_key, f"tier2:qualification-rank:{stratum.stratum_id}"),
        canonical_json_bytes({
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "stratum_id": stratum.stratum_id,
            "source_fingerprint": source.source_fingerprint,
        }),
        hashlib.sha256,
    ).hexdigest()


def _derived_key(key: bytes, purpose: str) -> bytes:
    return hmac.new(
        key,
        av1_validation_v3_hmac_domain(purpose).encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _validate_key(key: bytes) -> None:
    if len(key) < 32:
        raise AV1ValidationV3Error("AV1 v3 HMAC key must contain at least 32 bytes")


def _require_protocol_identity(value: Mapping[str, Any]) -> None:
    if (
        int_value(value.get("protocol_version")) != AV1_VALIDATION_V3_PROTOCOL_VERSION
        or value.get("experiment_id") != AV1_VALIDATION_V3_EXPERIMENT_ID
    ):
        raise AV1ValidationV3Error("AV1 v3 artifact protocol identity is invalid")


def _validate_traits(traits: tuple[str, ...], label: str) -> None:
    if not traits or traits != tuple(sorted(set(traits))):
        raise AV1ValidationV3Error(f"AV1 v3 {label} traits are invalid")
    if any(trait not in AV1_COLD_START_TRAITS for trait in traits):
        raise AV1ValidationV3Error(f"AV1 v3 {label} trait is unsupported")


def _binomial_tail_probability(
    *,
    trials: int,
    minimum_successes: int,
    success_probability: float,
) -> float:
    return sum(
        math.comb(trials, successes)
        * success_probability**successes
        * (1 - success_probability) ** (trials - successes)
        for successes in range(minimum_successes, trials + 1)
    )


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationV3Error(f"AV1 v3 {label} is invalid")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Error(f"AV1 v3 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Error(f"AV1 v3 {label} must include a timezone")
    return parsed.astimezone(UTC)


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise AV1ValidationV3Error(f"AV1 v3 {label} keys are invalid")


def _assert_public_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationV3Error("AV1 v3 protocol contains private data") from exc
