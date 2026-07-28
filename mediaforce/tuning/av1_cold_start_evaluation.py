from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, cast, Literal, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    AV1_COLD_START_TRAITS,
    AV1ColdStartEvaluationSummaryV1,
    assert_av1_cold_start_public_payload_safe,
)
from mediaforce.tuning.compression_intent import COMPRESSION_INTENT_LEVELS, CompressionIntentLevel


AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA = "mediaforce.av1_cold_start_validation_manifest"
AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA_VERSION = 1
AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA = "mediaforce.av1_cold_start_validation_evidence"
AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA_VERSION = 1
AV1_COLD_START_VALIDATION_REPORT_SCHEMA = "mediaforce.av1_cold_start_validation_report"
AV1_COLD_START_VALIDATION_REPORT_SCHEMA_VERSION = 1
AV1_COLD_START_VALIDATION_CONTRACT_VERSION = "acsv1"
AV1_COLD_START_VALIDATION_PREDICTOR_CONTRACT_VERSION = "acsp1"
AV1_COLD_START_VALIDATION_REGISTERED_AT = "2026-07-27T00:00:00Z"
AV1_COLD_START_VALIDATION_VALID_UNTIL = "2027-01-27T00:00:00Z"
AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT = 12
AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT = 6
AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS = 180
AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN = 6.0
AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_GROUP_COUNT = 6
AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_SOURCE_GROUP_COUNT = (
    AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT // 3
)

AV1ColdStartValidationMode = Literal["publication_candidate", "fallback_conformance"]
AV1ColdStartValidationTraitMatch = Literal["exact", "contains_all"]
AV1ColdStartValidationStratum = Literal["animation", "live_action", "risk", "intent"]
AV1ColdStartValidationExecutionOrder = Literal[
    "baseline_only",
    "baseline_then_guided",
    "guided_then_baseline",
]
AV1ColdStartValidationStatus = Literal["complete", "failed", "not_run_after_safety_stop"]
AV1ColdStartValidationPredictionStatus = Literal["recommended", "no_recommendation"]
AV1ColdStartValidationRuntimeState = Literal["paused", "available"]
AV1ColdStartValidationEvidenceState = Literal["not_started", "incomplete", "complete"]
AV1ColdStartValidationDecision = Literal[
    "not_ready",
    "supports_publication",
    "rejected",
    "conformance_passed",
    "conformance_failed",
]

_SLUG_RE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(
    r"av1v(?:plan|case|lock|authorization|result|manifest|evidence|report)1_[0-9a-f]{32}\Z"
)
_MANIFEST_REFERENCE_RE = re.compile(r"av1vmanifest[12]_[0-9a-f]{32}\Z")
_PLAN_REFERENCE_RE = re.compile(r"av1vplan[12]_[0-9a-f]{32}\Z")
_FALLBACK_REASON_RE = re.compile(r"[a-z][a-z0-9_]{2,95}\Z")

_RESULT_FAILURE_REASONS = frozenset({
    "baseline_failed",
    "guided_failed",
    "compatibility_drift",
    "metrics_incomplete",
    "operator_review_unavailable",
    "selection_ineligible",
    "safety_stop",
    "timeout",
})


class AV1ColdStartValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ColdStartEvaluationCase:
    recommended_crf_lower: float
    recommended_crf_upper: float
    measured_selected_crf: float
    baseline_candidate_count: int
    guided_candidate_count: int
    baseline_quality_floor_met: bool
    guided_quality_floor_met: bool
    baseline_final_size_met: bool
    guided_final_size_met: bool
    baseline_visual_accepted: bool
    guided_visual_accepted: bool
    baseline_operator_attention_events: int
    guided_operator_attention_events: int

    def __post_init__(self) -> None:
        crf_values = (
            self.recommended_crf_lower,
            self.recommended_crf_upper,
            self.measured_selected_crf,
        )
        if not all(math.isfinite(value) for value in crf_values):
            raise ValueError("AV1 cold-start evaluation CRFs must be finite")
        if not 0 <= self.recommended_crf_lower <= self.recommended_crf_upper <= 63:
            raise ValueError("AV1 cold-start evaluation range is invalid")
        if not 0 <= self.measured_selected_crf <= 63:
            raise ValueError("AV1 cold-start measured CRF is invalid")
        counts = (
            self.baseline_candidate_count,
            self.guided_candidate_count,
            self.baseline_operator_attention_events,
            self.guided_operator_attention_events,
        )
        if any(value < 0 for value in counts):
            raise ValueError("AV1 cold-start evaluation counts must be non-negative")


def summarize_av1_cold_start_evaluation(
        cases: Sequence[AV1ColdStartEvaluationCase],
) -> AV1ColdStartEvaluationSummaryV1:
    return AV1ColdStartEvaluationSummaryV1(
        held_out_count=len(cases),
        range_hit_count=sum(
            case.recommended_crf_lower <= case.measured_selected_crf <= case.recommended_crf_upper
            for case in cases
        ),
        baseline_candidate_count=sum(case.baseline_candidate_count for case in cases),
        guided_candidate_count=sum(case.guided_candidate_count for case in cases),
        quality_floor_regression_count=sum(
            case.baseline_quality_floor_met and not case.guided_quality_floor_met
            for case in cases
        ),
        final_size_regression_count=sum(
            case.baseline_final_size_met and not case.guided_final_size_met
            for case in cases
        ),
        visual_rejection_regression_count=sum(
            case.baseline_visual_accepted and not case.guided_visual_accepted
            for case in cases
        ),
        operator_attention_regression_count=sum(
            case.guided_operator_attention_events > case.baseline_operator_attention_events
            for case in cases
        ),
    )


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationCriteriaV1:
    minimum_derivation_evidence_count: int
    minimum_derivation_source_count: int
    minimum_holdout_count: int
    minimum_holdout_source_count: int
    maximum_derivation_age_days: int
    maximum_candidate_crf_span: float
    range_hit_null_rate: float
    maximum_one_sided_p_value: float
    maximum_safety_regression_count: int
    duplicate_review_fraction: float
    minimum_duplicate_review_agreement: float
    confidence_level: str
    confidence_score: float

    def __post_init__(self) -> None:
        if (
            self.minimum_derivation_evidence_count
            != AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT
        ):
            raise AV1ColdStartValidationError(
                "AV1 validation requires exactly twelve derivation observations"
            )
        if not (
            AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT
            <= self.minimum_derivation_source_count
            <= self.minimum_derivation_evidence_count
        ):
            raise AV1ColdStartValidationError("AV1 validation derivation-source minimum is invalid")
        if self.minimum_holdout_count < 16:
            raise AV1ColdStartValidationError("AV1 validation requires at least sixteen held-out cases")
        if not 6 <= self.minimum_holdout_source_count <= self.minimum_holdout_count:
            raise AV1ColdStartValidationError("AV1 validation held-out source minimum is invalid")
        if not (
            1
            <= self.maximum_derivation_age_days
            <= AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS
        ):
            raise AV1ColdStartValidationError("AV1 validation derivation evidence age is too permissive")
        rates = (
            self.range_hit_null_rate,
            self.maximum_one_sided_p_value,
            self.duplicate_review_fraction,
            self.minimum_duplicate_review_agreement,
            self.confidence_score,
        )
        if any(not math.isfinite(value) or not 0 < value <= 1 for value in rates):
            raise AV1ColdStartValidationError("AV1 validation rates must be finite values above zero")
        if self.range_hit_null_rate != 0.5:
            raise AV1ColdStartValidationError("AV1 validation range-hit null rate must remain one half")
        if self.maximum_one_sided_p_value > 0.025:
            raise AV1ColdStartValidationError("AV1 validation significance threshold is too permissive")
        if (
            not math.isfinite(self.maximum_candidate_crf_span)
            or not 0
            < self.maximum_candidate_crf_span
            <= AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN
        ):
            raise AV1ColdStartValidationError("AV1 validation candidate range exceeds the safe span")
        if self.maximum_safety_regression_count != 0:
            raise AV1ColdStartValidationError("AV1 validation safety regressions must remain zero")
        if self.duplicate_review_fraction < 0.2:
            raise AV1ColdStartValidationError("AV1 validation duplicate-review fraction is too small")
        if self.minimum_duplicate_review_agreement < 0.8:
            raise AV1ColdStartValidationError("AV1 validation duplicate-review agreement is too low")
        if self.confidence_level not in {"moderate", "high"}:
            raise AV1ColdStartValidationError("AV1 validation confidence target is unsupported")
        if self.confidence_score < 0.7:
            raise AV1ColdStartValidationError("AV1 validation confidence score is below the predictor gate")

    def to_payload(self) -> dict[str, Any]:
        return {
            "minimum_derivation_evidence_count": self.minimum_derivation_evidence_count,
            "minimum_derivation_source_count": self.minimum_derivation_source_count,
            "minimum_holdout_count": self.minimum_holdout_count,
            "minimum_holdout_source_count": self.minimum_holdout_source_count,
            "maximum_derivation_age_days": self.maximum_derivation_age_days,
            "maximum_candidate_crf_span": self.maximum_candidate_crf_span,
            "range_hit_null_rate": self.range_hit_null_rate,
            "maximum_one_sided_p_value": self.maximum_one_sided_p_value,
            "maximum_safety_regression_count": self.maximum_safety_regression_count,
            "duplicate_review_fraction": self.duplicate_review_fraction,
            "minimum_duplicate_review_agreement": self.minimum_duplicate_review_agreement,
            "confidence_level": self.confidence_level,
            "confidence_score": self.confidence_score,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationTraitSelectorV1:
    match: AV1ColdStartValidationTraitMatch
    traits: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.match not in {"exact", "contains_all"}:
            raise AV1ColdStartValidationError("AV1 validation trait matching is unsupported")
        if not self.traits or self.traits != tuple(sorted(set(self.traits))):
            raise AV1ColdStartValidationError("AV1 validation traits must be non-empty, unique, and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.traits):
            raise AV1ColdStartValidationError("AV1 validation trait is unsupported")

    def matches(self, traits: Sequence[str]) -> bool:
        normalized = tuple(sorted(set(str(trait) for trait in traits)))
        if self.match == "exact":
            return normalized == self.traits
        return set(self.traits).issubset(normalized)

    def to_payload(self) -> dict[str, Any]:
        return {"match": self.match, "traits": list(self.traits)}


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationCellPlanV1:
    cell_plan_id: str
    name: str
    mode: AV1ColdStartValidationMode
    stratum: AV1ColdStartValidationStratum
    intent_level: CompressionIntentLevel
    optimization_objective: str
    trait_selector: AV1ColdStartValidationTraitSelectorV1
    registered_case_count: int
    expected_prediction: AV1ColdStartValidationPredictionStatus
    allowed_fallback_reasons: tuple[str, ...]
    target_confidence: str
    duplicate_review_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.cell_plan_id) or not self.cell_plan_id.startswith("av1vplan1_"):
            raise AV1ColdStartValidationError("AV1 validation cell-plan ID is invalid")
        if not _SLUG_RE.fullmatch(self.name):
            raise AV1ColdStartValidationError("AV1 validation cell-plan name is invalid")
        if self.mode not in {"publication_candidate", "fallback_conformance"}:
            raise AV1ColdStartValidationError("AV1 validation cell-plan mode is unsupported")
        if self.stratum not in {"animation", "live_action", "risk", "intent"}:
            raise AV1ColdStartValidationError("AV1 validation stratum is unsupported")
        if self.intent_level not in COMPRESSION_INTENT_LEVELS:
            raise AV1ColdStartValidationError("AV1 validation intent is unsupported")
        if self.optimization_objective != AV1_COLD_START_INTENT_OBJECTIVES[self.intent_level]:
            raise AV1ColdStartValidationError("AV1 validation optimization objective does not match intent")
        if self.registered_case_count <= 0:
            raise AV1ColdStartValidationError("AV1 validation requires at least one registered case")
        if self.expected_prediction not in {"recommended", "no_recommendation"}:
            raise AV1ColdStartValidationError("AV1 validation expected prediction is unsupported")
        if self.allowed_fallback_reasons != tuple(sorted(set(self.allowed_fallback_reasons))):
            raise AV1ColdStartValidationError("AV1 validation fallback reasons must be unique and sorted")
        if any(not _FALLBACK_REASON_RE.fullmatch(reason) for reason in self.allowed_fallback_reasons):
            raise AV1ColdStartValidationError("AV1 validation fallback reason is invalid")
        if self.mode == "publication_candidate":
            if self.intent_level != "balanced" or self.trait_selector.match != "exact":
                raise AV1ColdStartValidationError("AV1 publication candidates require balanced exact-trait plans")
            if self.expected_prediction != "recommended" or self.allowed_fallback_reasons:
                raise AV1ColdStartValidationError("AV1 publication candidates require a recommendation")
            if self.target_confidence not in {"moderate", "high"}:
                raise AV1ColdStartValidationError("AV1 publication candidate confidence is unsupported")
            if (
                    self.duplicate_review_ordinals
                    != tuple(sorted(set(self.duplicate_review_ordinals)))
                    or not self.duplicate_review_ordinals
                    or any(not 1 <= ordinal <= self.registered_case_count for ordinal in self.duplicate_review_ordinals)
            ):
                raise AV1ColdStartValidationError("AV1 publication duplicate-review ordinals are invalid")
        else:
            if self.expected_prediction != "no_recommendation" or not self.allowed_fallback_reasons:
                raise AV1ColdStartValidationError("AV1 fallback plans require bounded no-recommendation reasons")
            if self.target_confidence != "none":
                raise AV1ColdStartValidationError("AV1 fallback plans cannot claim publication confidence")
            if self.duplicate_review_ordinals:
                raise AV1ColdStartValidationError("AV1 fallback plans cannot register duplicate reviews")
        expected_id = _validation_id("plan", self.semantic_payload())
        if self.cell_plan_id != expected_id:
            raise AV1ColdStartValidationError("AV1 validation cell-plan ID does not match its payload")

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
        }

    def to_payload(self) -> dict[str, Any]:
        return {"cell_plan_id": self.cell_plan_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationCaseV1:
    case_id: str
    cell_plan_id: str
    ordinal: int
    execution_order: AV1ColdStartValidationExecutionOrder

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.case_id) or not self.case_id.startswith("av1vcase1_"):
            raise AV1ColdStartValidationError("AV1 validation case ID is invalid")
        if not _ID_RE.fullmatch(self.cell_plan_id) or not self.cell_plan_id.startswith("av1vplan1_"):
            raise AV1ColdStartValidationError("AV1 validation case references an invalid cell plan")
        if self.ordinal <= 0:
            raise AV1ColdStartValidationError("AV1 validation case ordinal must be positive")
        if self.execution_order not in {
            "baseline_only",
            "baseline_then_guided",
            "guided_then_baseline",
        }:
            raise AV1ColdStartValidationError("AV1 validation execution order is unsupported")
        expected_id = _validation_id(
            "case",
            {
                "cell_plan_id": self.cell_plan_id,
                "ordinal": self.ordinal,
                "execution_order": self.execution_order,
            },
        )
        if self.case_id != expected_id:
            raise AV1ColdStartValidationError("AV1 validation case ID does not match its payload")

    def to_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "cell_plan_id": self.cell_plan_id,
            "ordinal": self.ordinal,
            "execution_order": self.execution_order,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationManifestV1:
    manifest_id: str
    validation_contract_version: str
    predictor_contract_version: str
    governance_issue: str
    state: str
    registered_at: str
    valid_until: str
    execution_policy: str
    privacy_contract: str
    selection_lock_required: bool
    criteria: AV1ColdStartValidationCriteriaV1
    cell_plans: tuple[AV1ColdStartValidationCellPlanV1, ...]
    cases: tuple[AV1ColdStartValidationCaseV1, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.manifest_id) or not self.manifest_id.startswith("av1vmanifest1_"):
            raise AV1ColdStartValidationError("AV1 validation manifest ID is invalid")
        if self.validation_contract_version != AV1_COLD_START_VALIDATION_CONTRACT_VERSION:
            raise AV1ColdStartValidationError("AV1 validation contract version is unsupported")
        if self.predictor_contract_version != AV1_COLD_START_VALIDATION_PREDICTOR_CONTRACT_VERSION:
            raise AV1ColdStartValidationError("AV1 predictor contract version is unsupported")
        if self.governance_issue != "#277" or self.state != "preregistered":
            raise AV1ColdStartValidationError("AV1 validation governance state is invalid")
        if self.execution_policy != "runtime_paused_until_selection_and_candidate_locks_are_reviewed":
            raise AV1ColdStartValidationError("AV1 validation execution policy is invalid")
        if self.privacy_contract != "private_source_mapping_aggregate_public_report":
            raise AV1ColdStartValidationError("AV1 validation privacy contract is invalid")
        if self.selection_lock_required is not True:
            raise AV1ColdStartValidationError("AV1 validation requires a private selection lock")
        registered_at = _parse_timestamp(self.registered_at, "manifest registration")
        valid_until = _parse_timestamp(self.valid_until, "manifest expiration")
        if valid_until <= registered_at:
            raise AV1ColdStartValidationError("AV1 validation manifest expiration must follow registration")
        if not self.cell_plans or self.cell_plans != tuple(sorted(self.cell_plans, key=lambda plan: plan.name)):
            raise AV1ColdStartValidationError("AV1 validation cell plans must be non-empty and sorted by name")
        if len({plan.cell_plan_id for plan in self.cell_plans}) != len(self.cell_plans):
            raise AV1ColdStartValidationError("AV1 validation cell-plan IDs must be unique")
        if self.cases != tuple(sorted(self.cases, key=lambda case: case.case_id)):
            raise AV1ColdStartValidationError("AV1 validation cases must be sorted by case ID")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise AV1ColdStartValidationError("AV1 validation case IDs must be unique")
        plan_by_id = {plan.cell_plan_id: plan for plan in self.cell_plans}
        case_counts = Counter(case.cell_plan_id for case in self.cases)
        for case in self.cases:
            plan = plan_by_id.get(case.cell_plan_id)
            if plan is None:
                raise AV1ColdStartValidationError("AV1 validation case references an undeclared cell plan")
            expected_order = _case_execution_order(plan, case.ordinal)
            if case.execution_order != expected_order:
                raise AV1ColdStartValidationError("AV1 validation case execution order is not deterministic")
        for plan in self.cell_plans:
            if case_counts[plan.cell_plan_id] != plan.registered_case_count:
                raise AV1ColdStartValidationError("AV1 validation registered-case count does not match cases")
            if (
                    plan.mode == "publication_candidate"
                    and plan.registered_case_count < self.criteria.minimum_holdout_count
            ):
                raise AV1ColdStartValidationError("AV1 validation publication cohort is below the held-out minimum")
            required_duplicate_count = math.ceil(
                plan.registered_case_count * self.criteria.duplicate_review_fraction
            )
            if (
                    plan.mode == "publication_candidate"
                    and len(plan.duplicate_review_ordinals) < required_duplicate_count
            ):
                raise AV1ColdStartValidationError("AV1 validation publication duplicate-review coverage is too small")
            if plan.target_confidence not in {self.criteria.confidence_level, "none"}:
                raise AV1ColdStartValidationError("AV1 validation plan confidence does not match criteria")
        _validate_manifest_coverage(self.cell_plans)
        semantic_payload = self.semantic_payload()
        if self.manifest_id != _validation_id("manifest", semantic_payload):
            raise AV1ColdStartValidationError("AV1 validation manifest ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"manifest_id": self.manifest_id, **semantic_payload}):
            raise AV1ColdStartValidationError("AV1 validation manifest digest does not match its payload")
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA,
            "schema_version": AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA_VERSION,
            "validation_contract_version": self.validation_contract_version,
            "predictor_contract_version": self.predictor_contract_version,
            "governance_issue": self.governance_issue,
            "state": self.state,
            "registered_at": self.registered_at,
            "valid_until": self.valid_until,
            "execution_policy": self.execution_policy,
            "privacy_contract": self.privacy_contract,
            "selection_lock_required": self.selection_lock_required,
            "criteria": self.criteria.to_payload(),
            "cell_plans": [plan.to_payload() for plan in self.cell_plans],
            "cases": [case.to_payload() for case in self.cases],
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationCandidateLockV1:
    candidate_lock_id: str
    manifest_id: str
    cell_plan_id: str
    exact_traits: tuple[str, ...]
    crf_lower: float
    crf_center: float
    crf_upper: float
    compatibility_signature: str
    policy_signature: str
    target_video_bitrate_min_bps: int
    target_video_bitrate_max_bps: int
    minimum_quality_score: float
    confidence_level: str
    confidence_score: float
    derivation_evidence_count: int
    derivation_source_count: int
    derivation_source_tokens: tuple[str, ...]
    derivation_title_tokens: tuple[str, ...]
    derivation_series_tokens: tuple[str, ...]
    derivation_source_group_tokens: tuple[str, ...]
    derivation_source_group_observation_tokens: tuple[str, ...]
    derivation_oldest_recorded_at: str
    derivation_newest_recorded_at: str
    derivation_conflict_count: int
    derivation_snapshot_sha256: str
    selection_lock_sha256: str
    locked_at: str
    reviewed_at: str
    review_state: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.candidate_lock_id) or not self.candidate_lock_id.startswith("av1vlock1_"):
            raise AV1ColdStartValidationError("AV1 validation candidate-lock ID is invalid")
        if not _MANIFEST_REFERENCE_RE.fullmatch(self.manifest_id):
            raise AV1ColdStartValidationError("AV1 validation candidate lock references an invalid manifest")
        if not _PLAN_REFERENCE_RE.fullmatch(self.cell_plan_id):
            raise AV1ColdStartValidationError("AV1 validation candidate lock references an invalid plan")
        if self.exact_traits != tuple(sorted(set(self.exact_traits))) or not self.exact_traits:
            raise AV1ColdStartValidationError("AV1 validation candidate traits must be non-empty and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.exact_traits):
            raise AV1ColdStartValidationError("AV1 validation candidate trait is unsupported")
        if not all(math.isfinite(value) for value in (self.crf_lower, self.crf_center, self.crf_upper)):
            raise AV1ColdStartValidationError("AV1 validation candidate CRFs must be finite")
        if not 0 <= self.crf_lower <= self.crf_center <= self.crf_upper <= 63:
            raise AV1ColdStartValidationError("AV1 validation candidate CRF range is invalid")
        if (
            self.crf_upper - self.crf_lower
            > AV1_COLD_START_VALIDATION_MAXIMUM_CANDIDATE_CRF_SPAN
        ):
            raise AV1ColdStartValidationError("AV1 validation candidate CRF span is too wide")
        for signature in (self.compatibility_signature, self.policy_signature):
            if not _SAFE_TOKEN_RE.fullmatch(signature):
                raise AV1ColdStartValidationError("AV1 validation candidate signature is invalid")
        if not 0 < self.target_video_bitrate_min_bps <= self.target_video_bitrate_max_bps:
            raise AV1ColdStartValidationError("AV1 validation candidate bitrate range is invalid")
        if not math.isfinite(self.minimum_quality_score) or self.minimum_quality_score <= 0:
            raise AV1ColdStartValidationError("AV1 validation candidate quality floor is invalid")
        if self.confidence_level not in {"none", "limited", "moderate", "high"}:
            raise AV1ColdStartValidationError("AV1 validation candidate confidence is unsupported")
        if not math.isfinite(self.confidence_score) or not 0 <= self.confidence_score <= 1:
            raise AV1ColdStartValidationError("AV1 validation candidate confidence score is invalid")
        if self.derivation_evidence_count != AV1_COLD_START_VALIDATION_REQUIRED_DERIVATION_EVIDENCE_COUNT:
            raise AV1ColdStartValidationError("AV1 validation candidate lock requires exactly 12 derivation observations")
        if (
            self.derivation_source_count
            < AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_COUNT
        ):
            raise AV1ColdStartValidationError(
                "AV1 validation derivation source count is below the preregistered minimum"
            )
        if self.derivation_source_count > self.derivation_evidence_count:
            raise AV1ColdStartValidationError("AV1 validation derivation sources exceed evidence")
        if self.derivation_conflict_count < 0:
            raise AV1ColdStartValidationError("AV1 validation derivation conflict count is invalid")
        for tokens in (
            self.derivation_source_tokens,
            self.derivation_title_tokens,
            self.derivation_series_tokens,
            self.derivation_source_group_tokens,
        ):
            if tokens != tuple(sorted(set(tokens))) or not tokens:
                raise AV1ColdStartValidationError("AV1 validation derivation tokens are incomplete")
            if any(not _SAFE_TOKEN_RE.fullmatch(token) for token in tokens):
                raise AV1ColdStartValidationError("AV1 validation derivation token is invalid")
        if (
            self.derivation_source_group_observation_tokens
            != tuple(sorted(self.derivation_source_group_observation_tokens))
            or len(self.derivation_source_group_observation_tokens)
            != self.derivation_evidence_count
            or any(
                not _SAFE_TOKEN_RE.fullmatch(token)
                for token in self.derivation_source_group_observation_tokens
            )
        ):
            raise AV1ColdStartValidationError(
                "AV1 validation derivation source-group observations are invalid"
            )
        source_group_counts = Counter(self.derivation_source_group_observation_tokens)
        if tuple(sorted(source_group_counts)) != self.derivation_source_group_tokens:
            raise AV1ColdStartValidationError(
                "AV1 validation derivation source-group observations are incomplete"
            )
        if len(source_group_counts) < AV1_COLD_START_VALIDATION_MINIMUM_DERIVATION_SOURCE_GROUP_COUNT:
            raise AV1ColdStartValidationError(
                "AV1 validation derivation requires at least six source groups"
            )
        if any(
            count > AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_SOURCE_GROUP_COUNT
            for count in source_group_counts.values()
        ):
            raise AV1ColdStartValidationError(
                "AV1 validation derivation source-group concentration is too high"
            )
        if len(self.derivation_source_tokens) != self.derivation_source_count:
            raise AV1ColdStartValidationError("AV1 validation derivation source tokens are incomplete")
        if len(self.derivation_title_tokens) != self.derivation_evidence_count:
            raise AV1ColdStartValidationError("AV1 validation derivation title tokens are incomplete")
        if len(self.derivation_series_tokens) != self.derivation_evidence_count:
            raise AV1ColdStartValidationError("AV1 validation derivation series tokens are incomplete")
        if (
            len(self.derivation_source_group_tokens) > self.derivation_source_count
        ):
            raise AV1ColdStartValidationError("AV1 validation derivation grouping tokens exceed evidence")
        for digest in (self.derivation_snapshot_sha256, self.selection_lock_sha256, self.payload_sha256):
            if not _SHA256_RE.fullmatch(digest):
                raise AV1ColdStartValidationError("AV1 validation candidate digest is invalid")
        locked_at = _parse_timestamp(self.locked_at, "candidate lock")
        reviewed_at = _parse_timestamp(self.reviewed_at, "candidate review")
        oldest_recorded_at = _parse_timestamp(
            self.derivation_oldest_recorded_at,
            "oldest derivation observation",
        )
        newest_recorded_at = _parse_timestamp(
            self.derivation_newest_recorded_at,
            "newest derivation observation",
        )
        if not oldest_recorded_at <= newest_recorded_at <= locked_at:
            raise AV1ColdStartValidationError("AV1 validation derivation chronology is invalid")
        if oldest_recorded_at < locked_at - timedelta(
            days=AV1_COLD_START_VALIDATION_MAXIMUM_DERIVATION_AGE_DAYS
        ):
            raise AV1ColdStartValidationError("AV1 validation derivation evidence is stale")
        if reviewed_at < locked_at or self.review_state != "approved_for_holdout":
            raise AV1ColdStartValidationError("AV1 validation candidate review state is invalid")
        semantic_payload = self.semantic_payload()
        if self.candidate_lock_id != _validation_id("lock", semantic_payload):
            raise AV1ColdStartValidationError("AV1 validation candidate-lock ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({
            "candidate_lock_id": self.candidate_lock_id,
            **semantic_payload,
        }):
            raise AV1ColdStartValidationError("AV1 validation candidate-lock digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "cell_plan_id": self.cell_plan_id,
            "exact_traits": list(self.exact_traits),
            "crf_lower": self.crf_lower,
            "crf_center": self.crf_center,
            "crf_upper": self.crf_upper,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "target_video_bitrate_min_bps": self.target_video_bitrate_min_bps,
            "target_video_bitrate_max_bps": self.target_video_bitrate_max_bps,
            "minimum_quality_score": self.minimum_quality_score,
            "confidence_level": self.confidence_level,
            "confidence_score": self.confidence_score,
            "derivation_evidence_count": self.derivation_evidence_count,
            "derivation_source_count": self.derivation_source_count,
            "derivation_source_tokens": list(self.derivation_source_tokens),
            "derivation_title_tokens": list(self.derivation_title_tokens),
            "derivation_series_tokens": list(self.derivation_series_tokens),
            "derivation_source_group_tokens": list(self.derivation_source_group_tokens),
            "derivation_source_group_observation_tokens": list(
                self.derivation_source_group_observation_tokens
            ),
            "derivation_oldest_recorded_at": self.derivation_oldest_recorded_at,
            "derivation_newest_recorded_at": self.derivation_newest_recorded_at,
            "derivation_conflict_count": self.derivation_conflict_count,
            "derivation_snapshot_sha256": self.derivation_snapshot_sha256,
            "selection_lock_sha256": self.selection_lock_sha256,
            "locked_at": self.locked_at,
            "reviewed_at": self.reviewed_at,
            "review_state": self.review_state,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate_lock_id": self.candidate_lock_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationExecutionAuthorizationV1:
    authorization_id: str
    manifest_id: str
    selection_lock_sha256: str
    candidate_lock_ids: tuple[str, ...]
    review_environment_token: str
    authorized_at: str
    review_state: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.authorization_id) or not self.authorization_id.startswith(
                "av1vauthorization1_"
        ):
            raise AV1ColdStartValidationError("AV1 validation execution-authorization ID is invalid")
        if not _MANIFEST_REFERENCE_RE.fullmatch(self.manifest_id):
            raise AV1ColdStartValidationError("AV1 validation authorization references an invalid manifest")
        if not _SHA256_RE.fullmatch(self.selection_lock_sha256):
            raise AV1ColdStartValidationError("AV1 validation authorization selection lock is invalid")
        if (
                not self.candidate_lock_ids
                or self.candidate_lock_ids != tuple(sorted(set(self.candidate_lock_ids)))
                or any(
                    not _ID_RE.fullmatch(lock_id) or not lock_id.startswith("av1vlock1_")
                    for lock_id in self.candidate_lock_ids
                )
        ):
            raise AV1ColdStartValidationError("AV1 validation authorization candidate locks are invalid")
        if not _SAFE_TOKEN_RE.fullmatch(self.review_environment_token):
            raise AV1ColdStartValidationError("AV1 validation authorization review token is invalid")
        _parse_timestamp(self.authorized_at, "execution authorization")
        if self.review_state != "approved_for_execution":
            raise AV1ColdStartValidationError("AV1 validation execution authorization is not approved")
        semantic_payload = self.semantic_payload()
        if self.authorization_id != _validation_id("authorization", semantic_payload):
            raise AV1ColdStartValidationError("AV1 validation authorization ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({
            "authorization_id": self.authorization_id,
            **semantic_payload,
        }):
            raise AV1ColdStartValidationError("AV1 validation authorization digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "selection_lock_sha256": self.selection_lock_sha256,
            "candidate_lock_ids": list(self.candidate_lock_ids),
            "review_environment_token": self.review_environment_token,
            "authorized_at": self.authorized_at,
            "review_state": self.review_state,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationArmResultV1:
    selected_crf: float
    candidate_count: int
    search_seconds: float
    workflow_seconds: float
    measured_quality_score: float
    final_size_bytes: int
    quality_floor_met: bool
    final_size_met: bool
    visual_accepted: bool
    operator_attention_events: int

    def __post_init__(self) -> None:
        numeric_values = (
            self.selected_crf,
            self.search_seconds,
            self.workflow_seconds,
            self.measured_quality_score,
        )
        if any(not math.isfinite(value) for value in numeric_values):
            raise AV1ColdStartValidationError("AV1 validation arm metrics must be finite")
        if self.measured_quality_score < 0:
            raise AV1ColdStartValidationError("AV1 validation measured quality score must be non-negative")
        if not 0 <= self.selected_crf <= 63:
            raise AV1ColdStartValidationError("AV1 validation selected CRF is invalid")
        if self.candidate_count <= 0 or self.search_seconds < 0 or self.workflow_seconds < 0:
            raise AV1ColdStartValidationError("AV1 validation arm work metrics are invalid")
        if self.final_size_bytes <= 0 or self.operator_attention_events < 0:
            raise AV1ColdStartValidationError("AV1 validation arm outcome counts are invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "selected_crf": self.selected_crf,
            "candidate_count": self.candidate_count,
            "search_seconds": self.search_seconds,
            "workflow_seconds": self.workflow_seconds,
            "measured_quality_score": self.measured_quality_score,
            "final_size_bytes": self.final_size_bytes,
            "quality_floor_met": self.quality_floor_met,
            "final_size_met": self.final_size_met,
            "visual_accepted": self.visual_accepted,
            "operator_attention_events": self.operator_attention_events,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationResultV1:
    result_id: str
    manifest_id: str
    cell_plan_id: str
    case_id: str
    execution_order: AV1ColdStartValidationExecutionOrder
    completed_at: str
    status: AV1ColdStartValidationStatus
    source_token: str
    title_token: str
    series_token: str
    source_group_token: str
    content_traits: tuple[str, ...]
    intent_level: CompressionIntentLevel
    compatibility_signature: str
    policy_signature: str
    review_environment_token: str
    target_video_bitrate_bps: int
    prediction_status: AV1ColdStartValidationPredictionStatus | None
    fallback_reason: str | None
    local_evidence_present: bool
    candidate_lock_id: str | None
    blinded_visual_review: bool
    duplicate_guided_visual_accepted: bool | None
    duplicate_review_environment_token: str | None
    baseline: AV1ColdStartValidationArmResultV1 | None
    guided: AV1ColdStartValidationArmResultV1 | None
    failure_reason: str | None
    payload_sha256: str

    def __post_init__(self) -> None:
        for value, prefix, label in (
            (self.result_id, "av1vresult1_", "result"),
            (self.manifest_id, "av1vmanifest1_", "manifest"),
            (self.cell_plan_id, "av1vplan1_", "cell plan"),
            (self.case_id, "av1vcase1_", "case"),
        ):
            if not _ID_RE.fullmatch(value) or not value.startswith(prefix):
                raise AV1ColdStartValidationError(f"AV1 validation {label} ID is invalid")
        _parse_timestamp(self.completed_at, "validation result")
        if self.execution_order not in {
            "baseline_only",
            "baseline_then_guided",
            "guided_then_baseline",
        }:
            raise AV1ColdStartValidationError("AV1 validation result execution order is unsupported")
        if self.status not in {"complete", "failed", "not_run_after_safety_stop"}:
            raise AV1ColdStartValidationError("AV1 validation result status is unsupported")
        for token in (
            self.source_token,
            self.title_token,
            self.series_token,
            self.source_group_token,
            self.compatibility_signature,
            self.policy_signature,
            self.review_environment_token,
        ):
            if not _SAFE_TOKEN_RE.fullmatch(token):
                raise AV1ColdStartValidationError("AV1 validation private token is invalid")
        if self.content_traits != tuple(sorted(set(self.content_traits))) or not self.content_traits:
            raise AV1ColdStartValidationError("AV1 validation result traits must be non-empty and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.content_traits):
            raise AV1ColdStartValidationError("AV1 validation result trait is unsupported")
        if self.intent_level not in COMPRESSION_INTENT_LEVELS or self.target_video_bitrate_bps <= 0:
            raise AV1ColdStartValidationError("AV1 validation result intent or target bitrate is invalid")
        if self.candidate_lock_id is not None:
            if not _ID_RE.fullmatch(self.candidate_lock_id) or not self.candidate_lock_id.startswith("av1vlock1_"):
                raise AV1ColdStartValidationError("AV1 validation result candidate-lock ID is invalid")
        if self.duplicate_guided_visual_accepted is None:
            if self.duplicate_review_environment_token is not None:
                raise AV1ColdStartValidationError("AV1 validation duplicate-review token is unexpected")
        else:
            if (
                    self.duplicate_review_environment_token is None
                    or not _SAFE_TOKEN_RE.fullmatch(self.duplicate_review_environment_token)
                    or self.duplicate_review_environment_token == self.review_environment_token
            ):
                raise AV1ColdStartValidationError("AV1 validation duplicate review is not independent")
        if self.status == "complete":
            if self.baseline is None or self.prediction_status is None or self.failure_reason is not None:
                raise AV1ColdStartValidationError("Complete AV1 validation results require measured baseline data")
            if self.prediction_status == "recommended":
                if self.guided is None or self.candidate_lock_id is None or self.fallback_reason is not None:
                    raise AV1ColdStartValidationError("Recommended AV1 validation results require guided evidence")
            elif self.guided is not None or self.candidate_lock_id is not None or not self.fallback_reason:
                raise AV1ColdStartValidationError("No-recommendation AV1 validation results require one fallback")
            if self.fallback_reason is not None and not _FALLBACK_REASON_RE.fullmatch(self.fallback_reason):
                raise AV1ColdStartValidationError("AV1 validation result fallback reason is invalid")
        else:
            if self.baseline is not None or self.guided is not None or self.prediction_status is not None:
                raise AV1ColdStartValidationError("Incomplete AV1 validation results cannot carry measurements")
            if self.candidate_lock_id is not None or self.fallback_reason is not None:
                raise AV1ColdStartValidationError("Incomplete AV1 validation results cannot carry predictions")
            if self.failure_reason not in _RESULT_FAILURE_REASONS:
                raise AV1ColdStartValidationError("AV1 validation failure reason is unsupported")
        if self.payload_sha256 != _payload_sha256(self.semantic_payload_with_id()):
            raise AV1ColdStartValidationError("AV1 validation result digest does not match its payload")
        if self.result_id != _validation_id("result", self.semantic_payload()):
            raise AV1ColdStartValidationError("AV1 validation result ID does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "cell_plan_id": self.cell_plan_id,
            "case_id": self.case_id,
            "execution_order": self.execution_order,
            "completed_at": self.completed_at,
            "status": self.status,
            "source_token": self.source_token,
            "title_token": self.title_token,
            "series_token": self.series_token,
            "source_group_token": self.source_group_token,
            "content_traits": list(self.content_traits),
            "intent_level": self.intent_level,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "review_environment_token": self.review_environment_token,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
            "prediction_status": self.prediction_status,
            "fallback_reason": self.fallback_reason,
            "local_evidence_present": self.local_evidence_present,
            "candidate_lock_id": self.candidate_lock_id,
            "blinded_visual_review": self.blinded_visual_review,
            "duplicate_guided_visual_accepted": self.duplicate_guided_visual_accepted,
            "duplicate_review_environment_token": self.duplicate_review_environment_token,
            "baseline": self.baseline.to_payload() if self.baseline else None,
            "guided": self.guided.to_payload() if self.guided else None,
            "failure_reason": self.failure_reason,
        }

    def semantic_payload_with_id(self) -> dict[str, Any]:
        return {"result_id": self.result_id, **self.semantic_payload()}

    def to_payload(self) -> dict[str, Any]:
        return {**self.semantic_payload_with_id(), "payload_sha256": self.payload_sha256}


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationEvidenceSetV1:
    evidence_set_id: str
    manifest_id: str
    selection_lock_sha256: str
    finalized_at: str
    candidate_locks: tuple[AV1ColdStartValidationCandidateLockV1, ...]
    execution_authorization: AV1ColdStartValidationExecutionAuthorizationV1
    results: tuple[AV1ColdStartValidationResultV1, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.evidence_set_id) or not self.evidence_set_id.startswith("av1vevidence1_"):
            raise AV1ColdStartValidationError("AV1 validation evidence-set ID is invalid")
        if not _ID_RE.fullmatch(self.manifest_id) or not self.manifest_id.startswith("av1vmanifest1_"):
            raise AV1ColdStartValidationError("AV1 validation evidence set references an invalid manifest")
        if not _SHA256_RE.fullmatch(self.selection_lock_sha256):
            raise AV1ColdStartValidationError("AV1 validation selection-lock digest is invalid")
        _parse_timestamp(self.finalized_at, "evidence finalization")
        if self.candidate_locks != tuple(sorted(self.candidate_locks, key=lambda lock: lock.cell_plan_id)):
            raise AV1ColdStartValidationError("AV1 validation candidate locks must be sorted by cell plan")
        if self.results != tuple(sorted(self.results, key=lambda result: result.case_id)):
            raise AV1ColdStartValidationError("AV1 validation results must be sorted by case ID")
        if len({lock.cell_plan_id for lock in self.candidate_locks}) != len(self.candidate_locks):
            raise AV1ColdStartValidationError("AV1 validation candidate locks must be unique per cell plan")
        if len({result.case_id for result in self.results}) != len(self.results):
            raise AV1ColdStartValidationError("AV1 validation results must be unique per case")
        if any(lock.manifest_id != self.manifest_id for lock in self.candidate_locks):
            raise AV1ColdStartValidationError("AV1 validation candidate lock references another manifest")
        if any(lock.selection_lock_sha256 != self.selection_lock_sha256 for lock in self.candidate_locks):
            raise AV1ColdStartValidationError("AV1 validation candidate lock uses another selection lock")
        if self.execution_authorization.manifest_id != self.manifest_id:
            raise AV1ColdStartValidationError("AV1 validation authorization references another manifest")
        if self.execution_authorization.selection_lock_sha256 != self.selection_lock_sha256:
            raise AV1ColdStartValidationError("AV1 validation authorization uses another selection lock")
        if self.execution_authorization.candidate_lock_ids != tuple(
                sorted(lock.candidate_lock_id for lock in self.candidate_locks)
        ):
            raise AV1ColdStartValidationError("AV1 validation authorization does not bind candidate locks")
        if any(result.manifest_id != self.manifest_id for result in self.results):
            raise AV1ColdStartValidationError("AV1 validation result references another manifest")
        semantic_payload = self.semantic_payload()
        if self.evidence_set_id != _validation_id("evidence", semantic_payload):
            raise AV1ColdStartValidationError("AV1 validation evidence-set ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({
            "evidence_set_id": self.evidence_set_id,
            **semantic_payload,
        }):
            raise AV1ColdStartValidationError("AV1 validation evidence-set digest does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA,
            "schema_version": AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "selection_lock_sha256": self.selection_lock_sha256,
            "finalized_at": self.finalized_at,
            "candidate_locks": [lock.to_payload() for lock in self.candidate_locks],
            "execution_authorization": self.execution_authorization.to_payload(),
            "results": [result.to_payload() for result in self.results],
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidence_set_id": self.evidence_set_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationCellReportV1:
    cell_plan_id: str
    name: str
    mode: AV1ColdStartValidationMode
    intent_level: CompressionIntentLevel
    registered_case_count: int
    complete_case_count: int
    failed_case_count: int
    stopped_case_count: int
    missing_case_count: int
    derivation_evidence_count: int | None
    derivation_source_count: int | None
    evaluation: AV1ColdStartEvaluationSummaryV1 | None
    range_hit_p_value: float | None
    guided_candidate_win_count: int
    guided_candidate_loss_count: int
    guided_candidate_tie_count: int
    guided_candidate_p_value: float | None
    baseline_search_seconds: float | None
    guided_search_seconds: float | None
    baseline_workflow_seconds: float | None
    guided_workflow_seconds: float | None
    duplicate_review_count: int
    duplicate_review_agreement: float | None
    decision: AV1ColdStartValidationDecision
    blocking_reasons: tuple[str, ...]

    @property
    def supports_publication(self) -> bool:
        return self.decision == "supports_publication"

    def to_payload(self) -> dict[str, Any]:
        return {
            "cell_plan_id": self.cell_plan_id,
            "name": self.name,
            "mode": self.mode,
            "intent_level": self.intent_level,
            "registered_case_count": self.registered_case_count,
            "complete_case_count": self.complete_case_count,
            "failed_case_count": self.failed_case_count,
            "stopped_case_count": self.stopped_case_count,
            "missing_case_count": self.missing_case_count,
            "derivation_evidence_count": self.derivation_evidence_count,
            "derivation_source_count": self.derivation_source_count,
            "evaluation": self.evaluation.to_payload() if self.evaluation else None,
            "range_hit_p_value": self.range_hit_p_value,
            "guided_candidate_win_count": self.guided_candidate_win_count,
            "guided_candidate_loss_count": self.guided_candidate_loss_count,
            "guided_candidate_tie_count": self.guided_candidate_tie_count,
            "guided_candidate_p_value": self.guided_candidate_p_value,
            "baseline_search_seconds": self.baseline_search_seconds,
            "guided_search_seconds": self.guided_search_seconds,
            "baseline_workflow_seconds": self.baseline_workflow_seconds,
            "guided_workflow_seconds": self.guided_workflow_seconds,
            "duplicate_review_count": self.duplicate_review_count,
            "duplicate_review_agreement": self.duplicate_review_agreement,
            "decision": self.decision,
            "blocking_reasons": list(self.blocking_reasons),
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartValidationReportV1:
    report_id: str
    manifest_id: str
    as_of: str
    evidence_payload_sha256: str | None
    evidence_finalized_at: str | None
    runtime_state: AV1ColdStartValidationRuntimeState
    evidence_state: AV1ColdStartValidationEvidenceState
    registered_case_count: int
    observed_case_count: int
    publication_ready_cell_count: int
    publication_rejected_cell_count: int
    conformance_failed_cell_count: int
    supports_publication_review: bool
    blocking_reasons: tuple[str, ...]
    cell_reports: tuple[AV1ColdStartValidationCellReportV1, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.report_id) or not self.report_id.startswith("av1vreport1_"):
            raise AV1ColdStartValidationError("AV1 validation report ID is invalid")
        if not _ID_RE.fullmatch(self.manifest_id) or not self.manifest_id.startswith("av1vmanifest1_"):
            raise AV1ColdStartValidationError("AV1 validation report references an invalid manifest")
        _parse_timestamp(self.as_of, "report as-of")
        if self.evidence_payload_sha256 is None:
            if self.evidence_finalized_at is not None:
                raise AV1ColdStartValidationError("AV1 validation report evidence timestamp is unexpected")
        else:
            if not _SHA256_RE.fullmatch(self.evidence_payload_sha256):
                raise AV1ColdStartValidationError("AV1 validation report evidence digest is invalid")
            if self.evidence_finalized_at is None:
                raise AV1ColdStartValidationError("AV1 validation report evidence timestamp is missing")
            _parse_timestamp(self.evidence_finalized_at, "report evidence finalization")
        if self.runtime_state not in {"paused", "available"}:
            raise AV1ColdStartValidationError("AV1 validation runtime state is unsupported")
        if self.evidence_state not in {"not_started", "incomplete", "complete"}:
            raise AV1ColdStartValidationError("AV1 validation evidence state is unsupported")
        counts = (
            self.registered_case_count,
            self.observed_case_count,
            self.publication_ready_cell_count,
            self.publication_rejected_cell_count,
            self.conformance_failed_cell_count,
        )
        if any(value < 0 for value in counts):
            raise AV1ColdStartValidationError("AV1 validation report counts must be non-negative")
        if self.cell_reports != tuple(sorted(self.cell_reports, key=lambda report: report.name)):
            raise AV1ColdStartValidationError("AV1 validation cell reports must be sorted by name")
        if self.blocking_reasons != tuple(sorted(set(self.blocking_reasons))):
            raise AV1ColdStartValidationError("AV1 validation report blockers must be unique and sorted")
        semantic_payload = self.semantic_payload()
        if self.report_id != _validation_id("report", semantic_payload):
            raise AV1ColdStartValidationError("AV1 validation report ID does not match its payload")
        if self.payload_sha256 != _payload_sha256({"report_id": self.report_id, **semantic_payload}):
            raise AV1ColdStartValidationError("AV1 validation report digest does not match its payload")
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_VALIDATION_REPORT_SCHEMA,
            "schema_version": AV1_COLD_START_VALIDATION_REPORT_SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "as_of": self.as_of,
            "evidence_payload_sha256": self.evidence_payload_sha256,
            "evidence_finalized_at": self.evidence_finalized_at,
            "runtime_state": self.runtime_state,
            "evidence_state": self.evidence_state,
            "registered_case_count": self.registered_case_count,
            "observed_case_count": self.observed_case_count,
            "publication_ready_cell_count": self.publication_ready_cell_count,
            "publication_rejected_cell_count": self.publication_rejected_cell_count,
            "conformance_failed_cell_count": self.conformance_failed_cell_count,
            "supports_publication_review": self.supports_publication_review,
            "blocking_reasons": list(self.blocking_reasons),
            "cell_reports": [report.to_payload() for report in self.cell_reports],
        }

    def to_payload(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.semantic_payload(), "payload_sha256": self.payload_sha256}


def build_av1_cold_start_validation_cell_plan(
        *,
        name: str,
        mode: AV1ColdStartValidationMode,
        stratum: AV1ColdStartValidationStratum,
        intent_level: CompressionIntentLevel,
        trait_match: AV1ColdStartValidationTraitMatch,
        traits: Sequence[str],
        registered_case_count: int,
        expected_prediction: AV1ColdStartValidationPredictionStatus,
        allowed_fallback_reasons: Sequence[str] = (),
        target_confidence: str = "none",
        duplicate_review_ordinals: Sequence[int] = (),
) -> AV1ColdStartValidationCellPlanV1:
    selector = AV1ColdStartValidationTraitSelectorV1(
        match=trait_match,
        traits=tuple(sorted(set(str(trait) for trait in traits))),
    )
    semantic_payload = {
        "name": name,
        "mode": mode,
        "stratum": stratum,
        "intent_level": intent_level,
        "optimization_objective": AV1_COLD_START_INTENT_OBJECTIVES[intent_level],
        "trait_selector": selector.to_payload(),
        "registered_case_count": registered_case_count,
        "expected_prediction": expected_prediction,
        "allowed_fallback_reasons": sorted(set(str(reason) for reason in allowed_fallback_reasons)),
        "target_confidence": target_confidence,
        "duplicate_review_ordinals": sorted(set(duplicate_review_ordinals)),
    }
    return AV1ColdStartValidationCellPlanV1(
        cell_plan_id=_validation_id("plan", semantic_payload),
        name=name,
        mode=mode,
        stratum=stratum,
        intent_level=intent_level,
        optimization_objective=AV1_COLD_START_INTENT_OBJECTIVES[intent_level],
        trait_selector=selector,
        registered_case_count=registered_case_count,
        expected_prediction=expected_prediction,
        allowed_fallback_reasons=tuple(semantic_payload["allowed_fallback_reasons"]),
        target_confidence=target_confidence,
        duplicate_review_ordinals=tuple(semantic_payload["duplicate_review_ordinals"]),
    )


def build_av1_cold_start_validation_case(
        plan: AV1ColdStartValidationCellPlanV1,
        ordinal: int,
) -> AV1ColdStartValidationCaseV1:
    execution_order = _case_execution_order(plan, ordinal)
    semantic_payload = {
        "cell_plan_id": plan.cell_plan_id,
        "ordinal": ordinal,
        "execution_order": execution_order,
    }
    return AV1ColdStartValidationCaseV1(
        case_id=_validation_id("case", semantic_payload),
        cell_plan_id=plan.cell_plan_id,
        ordinal=ordinal,
        execution_order=execution_order,
    )


def build_av1_cold_start_validation_manifest(
        *,
        registered_at: str,
        valid_until: str,
        criteria: AV1ColdStartValidationCriteriaV1,
        cell_plans: Sequence[AV1ColdStartValidationCellPlanV1],
) -> AV1ColdStartValidationManifestV1:
    plans = tuple(sorted(cell_plans, key=lambda plan: plan.name))
    cases = tuple(sorted(
        (
            build_av1_cold_start_validation_case(plan, ordinal)
            for plan in plans
            for ordinal in range(1, plan.registered_case_count + 1)
        ),
        key=lambda case: case.case_id,
    ))
    semantic_payload = {
        "schema": AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA,
        "schema_version": AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA_VERSION,
        "validation_contract_version": AV1_COLD_START_VALIDATION_CONTRACT_VERSION,
        "predictor_contract_version": AV1_COLD_START_VALIDATION_PREDICTOR_CONTRACT_VERSION,
        "governance_issue": "#277",
        "state": "preregistered",
        "registered_at": registered_at,
        "valid_until": valid_until,
        "execution_policy": "runtime_paused_until_selection_and_candidate_locks_are_reviewed",
        "privacy_contract": "private_source_mapping_aggregate_public_report",
        "selection_lock_required": True,
        "criteria": criteria.to_payload(),
        "cell_plans": [plan.to_payload() for plan in plans],
        "cases": [case.to_payload() for case in cases],
    }
    manifest_id = _validation_id("manifest", semantic_payload)
    return AV1ColdStartValidationManifestV1(
        manifest_id=manifest_id,
        validation_contract_version=AV1_COLD_START_VALIDATION_CONTRACT_VERSION,
        predictor_contract_version=AV1_COLD_START_VALIDATION_PREDICTOR_CONTRACT_VERSION,
        governance_issue="#277",
        state="preregistered",
        registered_at=registered_at,
        valid_until=valid_until,
        execution_policy="runtime_paused_until_selection_and_candidate_locks_are_reviewed",
        privacy_contract="private_source_mapping_aggregate_public_report",
        selection_lock_required=True,
        criteria=criteria,
        cell_plans=plans,
        cases=cases,
        payload_sha256=_payload_sha256({"manifest_id": manifest_id, **semantic_payload}),
    )


def build_preregistered_av1_cold_start_validation_manifest() -> AV1ColdStartValidationManifestV1:
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
    plans = [
        build_av1_cold_start_validation_cell_plan(
            name="animation_balanced_candidate",
            mode="publication_candidate",
            stratum="animation",
            intent_level="balanced",
            trait_match="exact",
            traits=("animation",),
            registered_case_count=16,
            expected_prediction="recommended",
            target_confidence="moderate",
            duplicate_review_ordinals=(4, 8, 12, 16),
        ),
        build_av1_cold_start_validation_cell_plan(
            name="typical_live_action_balanced_candidate",
            mode="publication_candidate",
            stratum="live_action",
            intent_level="balanced",
            trait_match="exact",
            traits=("typical",),
            registered_case_count=16,
            expected_prediction="recommended",
            target_confidence="moderate",
            duplicate_review_ordinals=(4, 8, 12, 16),
        ),
    ]
    for trait in (
        "darkness",
        "grain_noise",
        "low_motion_dialogue",
        "mixed",
        "motion",
        "texture_detail",
        "unknown",
    ):
        plans.append(build_av1_cold_start_validation_cell_plan(
            name=f"{trait}_risk_fallback",
            mode="fallback_conformance",
            stratum="risk",
            intent_level="balanced",
            trait_match="contains_all",
            traits=(trait,),
            registered_case_count=3,
            expected_prediction="no_recommendation",
            allowed_fallback_reasons=("content_profile_uncovered",),
        ))
    for intent in ("reference", "transparent", "perceptual_floor"):
        plans.append(build_av1_cold_start_validation_cell_plan(
            name=f"{intent}_directional_fallback",
            mode="fallback_conformance",
            stratum="intent",
            intent_level=intent,
            trait_match="contains_all",
            traits=("typical",),
            registered_case_count=3,
            expected_prediction="no_recommendation",
            allowed_fallback_reasons=("compression_intent_requires_directional_search",),
        ))
    return build_av1_cold_start_validation_manifest(
        registered_at=AV1_COLD_START_VALIDATION_REGISTERED_AT,
        valid_until=AV1_COLD_START_VALIDATION_VALID_UNTIL,
        criteria=criteria,
        cell_plans=plans,
    )


def assert_preregistered_av1_cold_start_validation_manifest(
        manifest: AV1ColdStartValidationManifestV1,
) -> None:
    expected = build_preregistered_av1_cold_start_validation_manifest()
    if manifest != expected:
        raise AV1ColdStartValidationError(
            "AV1 validation manifest does not match the pinned issue #277 preregistration"
        )


def serialize_av1_cold_start_validation_manifest(
        manifest: AV1ColdStartValidationManifestV1,
) -> bytes:
    return canonical_json_bytes(manifest.to_payload()) + b"\n"


def load_av1_cold_start_validation_manifest(
        path: Path,
) -> AV1ColdStartValidationManifestV1:
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1ColdStartValidationError("AV1 validation manifest is not valid UTF-8 JSON") from exc
    manifest = av1_cold_start_validation_manifest_from_payload(_mapping(payload, "manifest"))
    if raw_bytes != serialize_av1_cold_start_validation_manifest(manifest):
        raise AV1ColdStartValidationError("AV1 validation manifest bytes are not canonical")
    return manifest


def av1_cold_start_validation_manifest_from_payload(
        payload: Mapping[str, Any],
) -> AV1ColdStartValidationManifestV1:
    expected_keys = {
        "manifest_id",
        "schema",
        "schema_version",
        "validation_contract_version",
        "predictor_contract_version",
        "governance_issue",
        "state",
        "registered_at",
        "valid_until",
        "execution_policy",
        "privacy_contract",
        "selection_lock_required",
        "criteria",
        "cell_plans",
        "cases",
        "payload_sha256",
    }
    _require_exact_keys(payload, expected_keys, "manifest")
    if payload["schema"] != AV1_COLD_START_VALIDATION_MANIFEST_SCHEMA:
        raise AV1ColdStartValidationError("AV1 validation manifest schema is unsupported")
    if _integer(payload["schema_version"], "manifest schema version") != 1:
        raise AV1ColdStartValidationError("AV1 validation manifest schema version is unsupported")
    criteria = _criteria_from_payload(_mapping(payload["criteria"], "criteria"))
    plans = tuple(
        _cell_plan_from_payload(_mapping(value, "cell plan"))
        for value in _sequence(payload["cell_plans"], "cell plans")
    )
    cases = tuple(
        _case_from_payload(_mapping(value, "case"))
        for value in _sequence(payload["cases"], "cases")
    )
    return AV1ColdStartValidationManifestV1(
        manifest_id=_text(payload["manifest_id"], "manifest ID"),
        validation_contract_version=_text(
            payload["validation_contract_version"],
            "validation contract version",
        ),
        predictor_contract_version=_text(
            payload["predictor_contract_version"],
            "predictor contract version",
        ),
        governance_issue=_text(payload["governance_issue"], "governance issue"),
        state=_text(payload["state"], "manifest state"),
        registered_at=_text(payload["registered_at"], "manifest registration"),
        valid_until=_text(payload["valid_until"], "manifest expiration"),
        execution_policy=_text(payload["execution_policy"], "execution policy"),
        privacy_contract=_text(payload["privacy_contract"], "privacy contract"),
        selection_lock_required=_boolean(payload["selection_lock_required"], "selection-lock requirement"),
        criteria=criteria,
        cell_plans=plans,
        cases=cases,
        payload_sha256=_text(payload["payload_sha256"], "manifest digest"),
    )


def build_av1_cold_start_validation_candidate_lock(
        *,
        manifest_id: str,
        cell_plan_id: str,
        exact_traits: Sequence[str],
        crf_lower: float,
        crf_center: float,
        crf_upper: float,
        compatibility_signature: str,
        policy_signature: str,
        target_video_bitrate_min_bps: int,
        target_video_bitrate_max_bps: int,
        minimum_quality_score: float,
        confidence_level: str,
        confidence_score: float,
        derivation_evidence_count: int,
        derivation_source_count: int,
        derivation_source_tokens: Sequence[str],
        derivation_title_tokens: Sequence[str],
        derivation_series_tokens: Sequence[str],
        derivation_source_group_tokens: Sequence[str],
        derivation_source_group_observation_tokens: Sequence[str],
        derivation_oldest_recorded_at: str,
        derivation_newest_recorded_at: str,
        derivation_conflict_count: int,
        derivation_snapshot_sha256: str,
        selection_lock_sha256: str,
        locked_at: str,
        reviewed_at: str,
) -> AV1ColdStartValidationCandidateLockV1:
    semantic_payload = {
        "manifest_id": manifest_id,
        "cell_plan_id": cell_plan_id,
        "exact_traits": sorted(set(str(trait) for trait in exact_traits)),
        "crf_lower": crf_lower,
        "crf_center": crf_center,
        "crf_upper": crf_upper,
        "compatibility_signature": compatibility_signature,
        "policy_signature": policy_signature,
        "target_video_bitrate_min_bps": target_video_bitrate_min_bps,
        "target_video_bitrate_max_bps": target_video_bitrate_max_bps,
        "minimum_quality_score": minimum_quality_score,
        "confidence_level": confidence_level,
        "confidence_score": confidence_score,
        "derivation_evidence_count": derivation_evidence_count,
        "derivation_source_count": derivation_source_count,
        "derivation_source_tokens": sorted(set(derivation_source_tokens)),
        "derivation_title_tokens": sorted(set(derivation_title_tokens)),
        "derivation_series_tokens": sorted(set(derivation_series_tokens)),
        "derivation_source_group_tokens": sorted(set(derivation_source_group_tokens)),
        "derivation_source_group_observation_tokens": sorted(
            derivation_source_group_observation_tokens
        ),
        "derivation_oldest_recorded_at": derivation_oldest_recorded_at,
        "derivation_newest_recorded_at": derivation_newest_recorded_at,
        "derivation_conflict_count": derivation_conflict_count,
        "derivation_snapshot_sha256": derivation_snapshot_sha256,
        "selection_lock_sha256": selection_lock_sha256,
        "locked_at": locked_at,
        "reviewed_at": reviewed_at,
        "review_state": "approved_for_holdout",
    }
    candidate_lock_id = _validation_id("lock", semantic_payload)
    return AV1ColdStartValidationCandidateLockV1(
        candidate_lock_id=candidate_lock_id,
        manifest_id=manifest_id,
        cell_plan_id=cell_plan_id,
        exact_traits=tuple(semantic_payload["exact_traits"]),
        crf_lower=crf_lower,
        crf_center=crf_center,
        crf_upper=crf_upper,
        compatibility_signature=compatibility_signature,
        policy_signature=policy_signature,
        target_video_bitrate_min_bps=target_video_bitrate_min_bps,
        target_video_bitrate_max_bps=target_video_bitrate_max_bps,
        minimum_quality_score=minimum_quality_score,
        confidence_level=confidence_level,
        confidence_score=confidence_score,
        derivation_evidence_count=derivation_evidence_count,
        derivation_source_count=derivation_source_count,
        derivation_source_tokens=tuple(semantic_payload["derivation_source_tokens"]),
        derivation_title_tokens=tuple(semantic_payload["derivation_title_tokens"]),
        derivation_series_tokens=tuple(semantic_payload["derivation_series_tokens"]),
        derivation_source_group_tokens=tuple(semantic_payload["derivation_source_group_tokens"]),
        derivation_source_group_observation_tokens=tuple(
            semantic_payload["derivation_source_group_observation_tokens"]
        ),
        derivation_oldest_recorded_at=derivation_oldest_recorded_at,
        derivation_newest_recorded_at=derivation_newest_recorded_at,
        derivation_conflict_count=derivation_conflict_count,
        derivation_snapshot_sha256=derivation_snapshot_sha256,
        selection_lock_sha256=selection_lock_sha256,
        locked_at=locked_at,
        reviewed_at=reviewed_at,
        review_state="approved_for_holdout",
        payload_sha256=_payload_sha256({"candidate_lock_id": candidate_lock_id, **semantic_payload}),
    )


def build_av1_cold_start_validation_execution_authorization(
        *,
        manifest_id: str,
        selection_lock_sha256: str,
        candidate_lock_ids: Sequence[str],
        review_environment_token: str,
        authorized_at: str,
) -> AV1ColdStartValidationExecutionAuthorizationV1:
    semantic_payload = {
        "manifest_id": manifest_id,
        "selection_lock_sha256": selection_lock_sha256,
        "candidate_lock_ids": sorted(set(candidate_lock_ids)),
        "review_environment_token": review_environment_token,
        "authorized_at": authorized_at,
        "review_state": "approved_for_execution",
    }
    authorization_id = _validation_id("authorization", semantic_payload)
    return AV1ColdStartValidationExecutionAuthorizationV1(
        authorization_id=authorization_id,
        manifest_id=manifest_id,
        selection_lock_sha256=selection_lock_sha256,
        candidate_lock_ids=tuple(semantic_payload["candidate_lock_ids"]),
        review_environment_token=review_environment_token,
        authorized_at=authorized_at,
        review_state="approved_for_execution",
        payload_sha256=_payload_sha256({
            "authorization_id": authorization_id,
            **semantic_payload,
        }),
    )


def build_av1_cold_start_validation_result(
        *,
        manifest_id: str,
        cell_plan_id: str,
        case_id: str,
        execution_order: AV1ColdStartValidationExecutionOrder,
        completed_at: str,
        status: AV1ColdStartValidationStatus,
        source_token: str,
        title_token: str,
        series_token: str,
        source_group_token: str,
        content_traits: Sequence[str],
        intent_level: CompressionIntentLevel,
        compatibility_signature: str,
        policy_signature: str,
        review_environment_token: str,
        target_video_bitrate_bps: int,
        prediction_status: AV1ColdStartValidationPredictionStatus | None = None,
        fallback_reason: str | None = None,
        local_evidence_present: bool = False,
        candidate_lock_id: str | None = None,
        blinded_visual_review: bool = False,
        duplicate_guided_visual_accepted: bool | None = None,
        duplicate_review_environment_token: str | None = None,
        baseline: AV1ColdStartValidationArmResultV1 | None = None,
        guided: AV1ColdStartValidationArmResultV1 | None = None,
        failure_reason: str | None = None,
) -> AV1ColdStartValidationResultV1:
    semantic_payload = {
        "manifest_id": manifest_id,
        "cell_plan_id": cell_plan_id,
        "case_id": case_id,
        "execution_order": execution_order,
        "completed_at": completed_at,
        "status": status,
        "source_token": source_token,
        "title_token": title_token,
        "series_token": series_token,
        "source_group_token": source_group_token,
        "content_traits": sorted(set(str(trait) for trait in content_traits)),
        "intent_level": intent_level,
        "compatibility_signature": compatibility_signature,
        "policy_signature": policy_signature,
        "review_environment_token": review_environment_token,
        "target_video_bitrate_bps": target_video_bitrate_bps,
        "prediction_status": prediction_status,
        "fallback_reason": fallback_reason,
        "local_evidence_present": local_evidence_present,
        "candidate_lock_id": candidate_lock_id,
        "blinded_visual_review": blinded_visual_review,
        "duplicate_guided_visual_accepted": duplicate_guided_visual_accepted,
        "duplicate_review_environment_token": duplicate_review_environment_token,
        "baseline": baseline.to_payload() if baseline else None,
        "guided": guided.to_payload() if guided else None,
        "failure_reason": failure_reason,
    }
    result_id = _validation_id("result", semantic_payload)
    return AV1ColdStartValidationResultV1(
        result_id=result_id,
        manifest_id=manifest_id,
        cell_plan_id=cell_plan_id,
        case_id=case_id,
        execution_order=execution_order,
        completed_at=completed_at,
        status=status,
        source_token=source_token,
        title_token=title_token,
        series_token=series_token,
        source_group_token=source_group_token,
        content_traits=tuple(semantic_payload["content_traits"]),
        intent_level=intent_level,
        compatibility_signature=compatibility_signature,
        policy_signature=policy_signature,
        review_environment_token=review_environment_token,
        target_video_bitrate_bps=target_video_bitrate_bps,
        prediction_status=prediction_status,
        fallback_reason=fallback_reason,
        local_evidence_present=local_evidence_present,
        candidate_lock_id=candidate_lock_id,
        blinded_visual_review=blinded_visual_review,
        duplicate_guided_visual_accepted=duplicate_guided_visual_accepted,
        duplicate_review_environment_token=duplicate_review_environment_token,
        baseline=baseline,
        guided=guided,
        failure_reason=failure_reason,
        payload_sha256=_payload_sha256({"result_id": result_id, **semantic_payload}),
    )


def build_av1_cold_start_validation_evidence_set(
        *,
        manifest_id: str,
        selection_lock_sha256: str,
        finalized_at: str,
        candidate_locks: Sequence[AV1ColdStartValidationCandidateLockV1],
        execution_authorization: AV1ColdStartValidationExecutionAuthorizationV1,
        results: Sequence[AV1ColdStartValidationResultV1],
) -> AV1ColdStartValidationEvidenceSetV1:
    locks = tuple(sorted(candidate_locks, key=lambda lock: lock.cell_plan_id))
    sorted_results = tuple(sorted(results, key=lambda result: result.case_id))
    semantic_payload = {
        "schema": AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA,
        "schema_version": AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "selection_lock_sha256": selection_lock_sha256,
        "finalized_at": finalized_at,
        "candidate_locks": [lock.to_payload() for lock in locks],
        "execution_authorization": execution_authorization.to_payload(),
        "results": [result.to_payload() for result in sorted_results],
    }
    evidence_set_id = _validation_id("evidence", semantic_payload)
    return AV1ColdStartValidationEvidenceSetV1(
        evidence_set_id=evidence_set_id,
        manifest_id=manifest_id,
        selection_lock_sha256=selection_lock_sha256,
        finalized_at=finalized_at,
        candidate_locks=locks,
        execution_authorization=execution_authorization,
        results=sorted_results,
        payload_sha256=_payload_sha256({"evidence_set_id": evidence_set_id, **semantic_payload}),
    )


def serialize_av1_cold_start_validation_evidence_set(
        evidence_set: AV1ColdStartValidationEvidenceSetV1,
) -> bytes:
    return canonical_json_bytes(evidence_set.to_payload()) + b"\n"


def load_av1_cold_start_validation_evidence_set(
        path: Path,
) -> AV1ColdStartValidationEvidenceSetV1:
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1ColdStartValidationError("AV1 validation evidence is not valid UTF-8 JSON") from exc
    evidence_set = av1_cold_start_validation_evidence_set_from_payload(_mapping(payload, "evidence set"))
    if raw_bytes != serialize_av1_cold_start_validation_evidence_set(evidence_set):
        raise AV1ColdStartValidationError("AV1 validation evidence bytes are not canonical")
    return evidence_set


def av1_cold_start_validation_evidence_set_from_payload(
        payload: Mapping[str, Any],
) -> AV1ColdStartValidationEvidenceSetV1:
    expected_keys = {
        "evidence_set_id",
        "schema",
        "schema_version",
        "manifest_id",
        "selection_lock_sha256",
        "finalized_at",
        "candidate_locks",
        "execution_authorization",
        "results",
        "payload_sha256",
    }
    _require_exact_keys(payload, expected_keys, "evidence set")
    if payload["schema"] != AV1_COLD_START_VALIDATION_EVIDENCE_SCHEMA:
        raise AV1ColdStartValidationError("AV1 validation evidence schema is unsupported")
    if _integer(payload["schema_version"], "evidence schema version") != 1:
        raise AV1ColdStartValidationError("AV1 validation evidence schema version is unsupported")
    return AV1ColdStartValidationEvidenceSetV1(
        evidence_set_id=_text(payload["evidence_set_id"], "evidence-set ID"),
        manifest_id=_text(payload["manifest_id"], "manifest ID"),
        selection_lock_sha256=_text(payload["selection_lock_sha256"], "selection-lock digest"),
        finalized_at=_text(payload["finalized_at"], "evidence finalization"),
        candidate_locks=tuple(
            _candidate_lock_from_payload(_mapping(value, "candidate lock"))
            for value in _sequence(payload["candidate_locks"], "candidate locks")
        ),
        execution_authorization=_execution_authorization_from_payload(
            _mapping(payload["execution_authorization"], "execution authorization")
        ),
        results=tuple(
            _result_from_payload(_mapping(value, "result"))
            for value in _sequence(payload["results"], "results")
        ),
        payload_sha256=_text(payload["payload_sha256"], "evidence-set digest"),
    )


def build_av1_cold_start_validation_report(
        manifest: AV1ColdStartValidationManifestV1,
        evidence_set: AV1ColdStartValidationEvidenceSetV1 | None,
        *,
        as_of: str,
        runtime_state: AV1ColdStartValidationRuntimeState,
) -> AV1ColdStartValidationReportV1:
    assert_preregistered_av1_cold_start_validation_manifest(manifest)
    if runtime_state not in {"paused", "available"}:
        raise AV1ColdStartValidationError("AV1 validation runtime state is unsupported")
    as_of_time = _parse_timestamp(as_of, "report as-of")
    registered_at = _parse_timestamp(manifest.registered_at, "manifest registration")
    valid_until = _parse_timestamp(manifest.valid_until, "manifest expiration")
    if as_of_time < registered_at:
        raise AV1ColdStartValidationError("AV1 validation report predates preregistration")
    if evidence_set is not None and evidence_set.manifest_id != manifest.manifest_id:
        raise AV1ColdStartValidationError("AV1 validation evidence belongs to another manifest")

    results = evidence_set.results if evidence_set else ()
    candidate_locks = evidence_set.candidate_locks if evidence_set else ()
    if evidence_set is not None:
        finalized_at = _parse_timestamp(evidence_set.finalized_at, "evidence finalization")
        authorization = evidence_set.execution_authorization
        authorized_at = _parse_timestamp(authorization.authorized_at, "execution authorization")
        if finalized_at > as_of_time:
            raise AV1ColdStartValidationError("AV1 validation evidence is future-dated")
        if not registered_at <= authorized_at <= finalized_at:
            raise AV1ColdStartValidationError("AV1 validation execution authorization chronology is invalid")
        if authorized_at >= valid_until:
            raise AV1ColdStartValidationError("AV1 validation execution authorization is stale")
        publication_plan_ids = {
            plan.cell_plan_id
            for plan in manifest.cell_plans
            if plan.mode == "publication_candidate"
        }
        if {lock.cell_plan_id for lock in candidate_locks} != publication_plan_ids:
            raise AV1ColdStartValidationError("AV1 validation authorization requires every candidate lock")
        for candidate_lock in candidate_locks:
            locked_at = _parse_timestamp(candidate_lock.locked_at, "candidate lock")
            reviewed_at = _parse_timestamp(candidate_lock.reviewed_at, "candidate review")
            if not registered_at <= locked_at <= reviewed_at <= authorized_at:
                raise AV1ColdStartValidationError("AV1 validation candidate lock chronology is invalid")
            if reviewed_at >= valid_until:
                raise AV1ColdStartValidationError("AV1 validation candidate review is stale")
        for result in results:
            completed_at = _parse_timestamp(result.completed_at, "validation result")
            if not authorized_at <= completed_at <= finalized_at:
                raise AV1ColdStartValidationError("AV1 validation result chronology is invalid")
    expected_case_ids = {case.case_id for case in manifest.cases}
    unexpected_case_ids = {result.case_id for result in results}.difference(expected_case_ids)
    if unexpected_case_ids:
        raise AV1ColdStartValidationError("AV1 validation evidence contains undeclared cases")
    plan_ids = {plan.cell_plan_id for plan in manifest.cell_plans}
    unexpected_lock_ids = {lock.cell_plan_id for lock in candidate_locks}.difference(plan_ids)
    if unexpected_lock_ids:
        raise AV1ColdStartValidationError("AV1 validation evidence contains an undeclared candidate lock")

    results_by_case = {result.case_id: result for result in results}
    locks_by_plan = {lock.cell_plan_id: lock for lock in candidate_locks}
    cases_by_plan: dict[str, list[AV1ColdStartValidationCaseV1]] = {
        plan.cell_plan_id: [] for plan in manifest.cell_plans
    }
    for case in manifest.cases:
        cases_by_plan[case.cell_plan_id].append(case)

    cell_reports = tuple(sorted(
        (
            _build_validation_cell_report(
                manifest,
                plan,
                cases_by_plan[plan.cell_plan_id],
                results_by_case,
                locks_by_plan.get(plan.cell_plan_id),
                evidence_set.selection_lock_sha256 if evidence_set else None,
            )
            for plan in manifest.cell_plans
        ),
        key=lambda report: report.name,
    ))
    observed_case_count = len(results)
    registered_case_count = len(manifest.cases)
    if observed_case_count == 0:
        evidence_state: AV1ColdStartValidationEvidenceState = "not_started"
    elif observed_case_count < registered_case_count:
        evidence_state = "incomplete"
    else:
        evidence_state = "complete"

    publication_ready = sum(report.decision == "supports_publication" for report in cell_reports)
    publication_rejected = sum(report.decision == "rejected" for report in cell_reports)
    conformance_failed = sum(report.decision == "conformance_failed" for report in cell_reports)
    blocking_reasons: list[str] = []
    if evidence_state != "complete":
        blocking_reasons.append("evidence_incomplete")
        if runtime_state == "paused":
            blocking_reasons.append("runtime_paused")
    if publication_ready == 0:
        blocking_reasons.append("no_publication_candidate_supported")
    if conformance_failed:
        blocking_reasons.append("conformance_failure")
    supports_publication_review = (
        evidence_state == "complete"
        and publication_ready > 0
        and conformance_failed == 0
    )
    semantic_payload = {
        "schema": AV1_COLD_START_VALIDATION_REPORT_SCHEMA,
        "schema_version": AV1_COLD_START_VALIDATION_REPORT_SCHEMA_VERSION,
        "manifest_id": manifest.manifest_id,
        "as_of": as_of,
        "evidence_payload_sha256": evidence_set.payload_sha256 if evidence_set else None,
        "evidence_finalized_at": evidence_set.finalized_at if evidence_set else None,
        "runtime_state": runtime_state,
        "evidence_state": evidence_state,
        "registered_case_count": registered_case_count,
        "observed_case_count": observed_case_count,
        "publication_ready_cell_count": publication_ready,
        "publication_rejected_cell_count": publication_rejected,
        "conformance_failed_cell_count": conformance_failed,
        "supports_publication_review": supports_publication_review,
        "blocking_reasons": sorted(set(blocking_reasons)),
        "cell_reports": [report.to_payload() for report in cell_reports],
    }
    report_id = _validation_id("report", semantic_payload)
    return AV1ColdStartValidationReportV1(
        report_id=report_id,
        manifest_id=manifest.manifest_id,
        as_of=as_of,
        evidence_payload_sha256=evidence_set.payload_sha256 if evidence_set else None,
        evidence_finalized_at=evidence_set.finalized_at if evidence_set else None,
        runtime_state=runtime_state,
        evidence_state=evidence_state,
        registered_case_count=registered_case_count,
        observed_case_count=observed_case_count,
        publication_ready_cell_count=publication_ready,
        publication_rejected_cell_count=publication_rejected,
        conformance_failed_cell_count=conformance_failed,
        supports_publication_review=supports_publication_review,
        blocking_reasons=tuple(semantic_payload["blocking_reasons"]),
        cell_reports=cell_reports,
        payload_sha256=_payload_sha256({"report_id": report_id, **semantic_payload}),
    )


def serialize_av1_cold_start_validation_report(
        report: AV1ColdStartValidationReportV1,
) -> bytes:
    return canonical_json_bytes(report.to_payload()) + b"\n"


def format_av1_cold_start_validation_report(
        report: AV1ColdStartValidationReportV1,
) -> str:
    lines = [
        f"AV1 cold-start validation: {report.evidence_state}",
        f"Runtime: {report.runtime_state}",
        f"Cases: {report.observed_case_count}/{report.registered_case_count}",
        f"Publication-ready cells: {report.publication_ready_cell_count}",
    ]
    if report.blocking_reasons:
        lines.append(f"Global blockers: {', '.join(report.blocking_reasons)}")
    for cell in report.cell_reports:
        line = f"- {cell.name}: {cell.decision} ({cell.complete_case_count}/{cell.registered_case_count})"
        if cell.blocking_reasons:
            line += f" [{', '.join(cell.blocking_reasons)}]"
        lines.append(line)
    return "\n".join(lines)


def _build_validation_cell_report(
        manifest: AV1ColdStartValidationManifestV1,
        plan: AV1ColdStartValidationCellPlanV1,
        registered_cases: Sequence[AV1ColdStartValidationCaseV1],
        results_by_case: Mapping[str, AV1ColdStartValidationResultV1],
        candidate_lock: AV1ColdStartValidationCandidateLockV1 | None,
        selection_lock_sha256: str | None,
) -> AV1ColdStartValidationCellReportV1:
    observed_results = [
        results_by_case[case.case_id]
        for case in registered_cases
        if case.case_id in results_by_case
    ]
    case_by_id = {case.case_id: case for case in registered_cases}
    blockers: list[str] = []
    for result in observed_results:
        case = case_by_id[result.case_id]
        if result.cell_plan_id != plan.cell_plan_id or result.execution_order != case.execution_order:
            raise AV1ColdStartValidationError("AV1 validation result does not match its registered case")
        if result.intent_level != plan.intent_level or not plan.trait_selector.matches(result.content_traits):
            raise AV1ColdStartValidationError("AV1 validation result does not match its registered cohort")
        completed_at = _parse_timestamp(result.completed_at, "validation result")
        if completed_at < _parse_timestamp(manifest.registered_at, "manifest registration"):
            raise AV1ColdStartValidationError("AV1 validation result predates preregistration")
        if completed_at >= _parse_timestamp(manifest.valid_until, "manifest expiration"):
            blockers.append("result_after_manifest_expiration")

    complete_results = [result for result in observed_results if result.status == "complete"]
    failed_results = [result for result in observed_results if result.status == "failed"]
    stopped_results = [
        result for result in observed_results if result.status == "not_run_after_safety_stop"
    ]
    missing_case_count = len(registered_cases) - len(observed_results)
    if missing_case_count:
        blockers.append("missing_case_results")
    if failed_results:
        blockers.append("failed_case_results")
    if stopped_results:
        blockers.append("safety_stop")

    if plan.mode == "publication_candidate":
        duplicate_case_ids = {
            case.case_id
            for case in registered_cases
            if case.ordinal in plan.duplicate_review_ordinals
        }
        return _build_publication_candidate_report(
            manifest,
            plan,
            complete_results,
            failed_results,
            stopped_results,
            missing_case_count,
            candidate_lock,
            selection_lock_sha256,
            duplicate_case_ids,
            blockers,
        )
    return _build_fallback_conformance_report(
        plan,
        complete_results,
        failed_results,
        stopped_results,
        missing_case_count,
        candidate_lock,
        blockers,
    )


def _build_publication_candidate_report(
        manifest: AV1ColdStartValidationManifestV1,
        plan: AV1ColdStartValidationCellPlanV1,
        complete_results: Sequence[AV1ColdStartValidationResultV1],
        failed_results: Sequence[AV1ColdStartValidationResultV1],
        stopped_results: Sequence[AV1ColdStartValidationResultV1],
        missing_case_count: int,
        candidate_lock: AV1ColdStartValidationCandidateLockV1 | None,
        selection_lock_sha256: str | None,
        duplicate_case_ids: set[str],
        blockers: list[str],
) -> AV1ColdStartValidationCellReportV1:
    criteria = manifest.criteria
    if candidate_lock is None:
        blockers.append("candidate_lock_missing")
    else:
        if candidate_lock.manifest_id != manifest.manifest_id or candidate_lock.cell_plan_id != plan.cell_plan_id:
            raise AV1ColdStartValidationError("AV1 validation candidate lock does not match its plan")
        if candidate_lock.selection_lock_sha256 != selection_lock_sha256:
            blockers.append("selection_lock_mismatch")
        if not plan.trait_selector.matches(candidate_lock.exact_traits):
            blockers.append("candidate_traits_mismatch")
        if candidate_lock.derivation_evidence_count < criteria.minimum_derivation_evidence_count:
            blockers.append("derivation_evidence_insufficient")
        if candidate_lock.derivation_source_count < criteria.minimum_derivation_source_count:
            blockers.append("derivation_sources_insufficient")
        allowed_confidence_levels = (
            {"moderate", "high"}
            if criteria.confidence_level == "moderate"
            else {"high"}
        )
        if (
                candidate_lock.confidence_level not in allowed_confidence_levels
                or candidate_lock.confidence_score < criteria.confidence_score
        ):
            blockers.append("candidate_confidence_insufficient")
        oldest_derivation = _parse_timestamp(
            candidate_lock.derivation_oldest_recorded_at,
            "oldest derivation observation",
        )
        locked_at = _parse_timestamp(candidate_lock.locked_at, "candidate lock")
        if oldest_derivation < locked_at - timedelta(days=criteria.maximum_derivation_age_days):
            blockers.append("derivation_evidence_stale")
        if candidate_lock.derivation_conflict_count:
            blockers.append("derivation_evidence_conflicting")
        if candidate_lock.crf_upper - candidate_lock.crf_lower > criteria.maximum_candidate_crf_span:
            blockers.append("candidate_range_too_wide")

    compatible_results: list[AV1ColdStartValidationResultV1] = []
    if candidate_lock is not None:
        lock_time = _parse_timestamp(candidate_lock.reviewed_at, "candidate review")
        for result in complete_results:
            if result.prediction_status != "recommended" or result.candidate_lock_id != candidate_lock.candidate_lock_id:
                blockers.append("candidate_lock_not_used")
                continue
            if result.compatibility_signature != candidate_lock.compatibility_signature:
                blockers.append("compatibility_mismatch")
                continue
            if result.policy_signature != candidate_lock.policy_signature:
                blockers.append("policy_signature_mismatch")
                continue
            if tuple(result.content_traits) != candidate_lock.exact_traits:
                blockers.append("candidate_traits_mismatch")
                continue
            if not (
                    candidate_lock.target_video_bitrate_min_bps
                    <= result.target_video_bitrate_bps
                    <= candidate_lock.target_video_bitrate_max_bps
            ):
                blockers.append("target_bitrate_outside_locked_range")
                continue
            if _parse_timestamp(result.completed_at, "validation result") < lock_time:
                blockers.append("result_predates_candidate_review")
                continue
            if result.local_evidence_present:
                blockers.append("local_evidence_contamination")
                continue
            if not result.blinded_visual_review:
                blockers.append("visual_review_unblinded")
                continue
            baseline = result.baseline
            guided = result.guided
            if baseline is None or guided is None:
                blockers.append("paired_measurements_missing")
                continue
            if (
                    baseline.quality_floor_met
                    != (baseline.measured_quality_score >= candidate_lock.minimum_quality_score)
                    or guided.quality_floor_met
                    != (guided.measured_quality_score >= candidate_lock.minimum_quality_score)
            ):
                blockers.append("quality_floor_flag_mismatch")
                continue
            if not (
                    baseline.quality_floor_met
                    and baseline.final_size_met
                    and baseline.visual_accepted
            ):
                blockers.append("baseline_outcome_not_accepted")
                continue
            if not guided.quality_floor_met or not guided.final_size_met or not guided.visual_accepted:
                if (
                        (baseline.quality_floor_met and not guided.quality_floor_met)
                        or (baseline.final_size_met and not guided.final_size_met)
                        or (baseline.visual_accepted and not guided.visual_accepted)
                ):
                    blockers.append("safety_regression")
                blockers.append("guided_outcome_not_accepted")
                continue
            compatible_results.append(result)

    source_tokens = {result.source_token for result in compatible_results}
    title_counts = Counter(result.title_token for result in compatible_results)
    series_counts = Counter(result.series_token for result in compatible_results)
    source_group_counts = Counter(result.source_group_token for result in compatible_results)
    if len(source_tokens) < criteria.minimum_holdout_count:
        blockers.append("held_out_source_tokens_not_unique")
    if any(count > 1 for count in title_counts.values()):
        blockers.append("held_out_titles_reused")
    if any(count > 1 for count in series_counts.values()):
        blockers.append("held_out_series_reused")
    maximum_source_group_count = math.floor(len(compatible_results) / 3)
    if source_group_counts and max(source_group_counts.values()) > maximum_source_group_count:
        blockers.append("held_out_source_group_concentrated")
    if len(source_group_counts) < criteria.minimum_holdout_source_count:
        blockers.append("held_out_source_groups_insufficient")
    if candidate_lock is not None:
        if source_tokens.intersection(candidate_lock.derivation_source_tokens):
            blockers.append("derivation_holdout_source_overlap")
        if set(title_counts).intersection(candidate_lock.derivation_title_tokens):
            blockers.append("derivation_holdout_title_overlap")
        if set(series_counts).intersection(candidate_lock.derivation_series_tokens):
            blockers.append("derivation_holdout_series_overlap")
        if set(source_group_counts).intersection(candidate_lock.derivation_source_group_tokens):
            blockers.append("derivation_holdout_source_group_overlap")

    evaluation_cases: list[AV1ColdStartEvaluationCase] = []
    if candidate_lock is not None:
        for result in compatible_results:
            if result.baseline is None or result.guided is None:
                continue
            evaluation_cases.append(AV1ColdStartEvaluationCase(
                recommended_crf_lower=candidate_lock.crf_lower,
                recommended_crf_upper=candidate_lock.crf_upper,
                measured_selected_crf=result.baseline.selected_crf,
                baseline_candidate_count=result.baseline.candidate_count,
                guided_candidate_count=result.guided.candidate_count,
                baseline_quality_floor_met=result.baseline.quality_floor_met,
                guided_quality_floor_met=result.guided.quality_floor_met,
                baseline_final_size_met=result.baseline.final_size_met,
                guided_final_size_met=result.guided.final_size_met,
                baseline_visual_accepted=result.baseline.visual_accepted,
                guided_visual_accepted=result.guided.visual_accepted,
                baseline_operator_attention_events=result.baseline.operator_attention_events,
                guided_operator_attention_events=result.guided.operator_attention_events,
            ))
    evaluation = summarize_av1_cold_start_evaluation(evaluation_cases) if evaluation_cases else None
    range_hit_p_value = None
    guided_candidate_p_value = None
    win_count = loss_count = tie_count = 0
    if evaluation is not None:
        range_hit_p_value = _one_sided_binomial_p_value(
            evaluation.range_hit_count,
            evaluation.held_out_count,
            criteria.range_hit_null_rate,
        )
        for result in compatible_results:
            if result.baseline is None or result.guided is None:
                continue
            if result.guided.candidate_count < result.baseline.candidate_count:
                win_count += 1
            elif result.guided.candidate_count > result.baseline.candidate_count:
                loss_count += 1
            else:
                tie_count += 1
        discordant_count = win_count + loss_count
        if discordant_count:
            guided_candidate_p_value = _one_sided_binomial_p_value(win_count, discordant_count, 0.5)
        if evaluation.held_out_count < criteria.minimum_holdout_count:
            blockers.append("held_out_cases_insufficient")
        if range_hit_p_value > criteria.maximum_one_sided_p_value:
            blockers.append("range_hit_evidence_insufficient")
        if guided_candidate_p_value is None or guided_candidate_p_value > criteria.maximum_one_sided_p_value:
            blockers.append("guided_candidate_evidence_insufficient")
        if not evaluation.supports_publication:
            if evaluation.guided_candidate_count >= evaluation.baseline_candidate_count:
                blockers.append("candidate_work_not_reduced")
            if evaluation.safety_regression_count:
                blockers.append("safety_regression")

    compatible_by_case = {result.case_id: result for result in compatible_results}
    duplicate_results = [
        compatible_by_case[case_id]
        for case_id in sorted(duplicate_case_ids)
        if case_id in compatible_by_case
        and compatible_by_case[case_id].duplicate_guided_visual_accepted is not None
        and compatible_by_case[case_id].guided is not None
    ]
    unexpected_duplicate_results = [
        result for result in compatible_results
        if result.case_id not in duplicate_case_ids
        and result.duplicate_guided_visual_accepted is not None
    ]
    required_duplicate_count = len(duplicate_case_ids)
    duplicate_review_agreement = None
    if duplicate_results:
        agreement_count = sum(
            result.duplicate_guided_visual_accepted == result.guided.visual_accepted
            for result in duplicate_results
            if result.guided is not None
        )
        duplicate_review_agreement = round(agreement_count / len(duplicate_results), 6)
    if len(duplicate_results) < required_duplicate_count:
        blockers.append("duplicate_review_insufficient")
    if unexpected_duplicate_results:
        blockers.append("duplicate_review_not_preregistered")
    elif (
            duplicate_review_agreement is None
            or duplicate_review_agreement < criteria.minimum_duplicate_review_agreement
    ):
        blockers.append("duplicate_review_agreement_low")
    if any(
            result.baseline is not None
            and result.baseline.visual_accepted
            and result.duplicate_guided_visual_accepted is False
            for result in duplicate_results
    ):
        blockers.append("duplicate_visual_rejection")

    blockers = sorted(set(blockers))
    if missing_case_count:
        decision: AV1ColdStartValidationDecision = "not_ready"
    elif failed_results or stopped_results or blockers:
        decision = "rejected"
    else:
        decision = "supports_publication"
    return AV1ColdStartValidationCellReportV1(
        cell_plan_id=plan.cell_plan_id,
        name=plan.name,
        mode=plan.mode,
        intent_level=plan.intent_level,
        registered_case_count=plan.registered_case_count,
        complete_case_count=len(complete_results),
        failed_case_count=len(failed_results),
        stopped_case_count=len(stopped_results),
        missing_case_count=missing_case_count,
        derivation_evidence_count=candidate_lock.derivation_evidence_count if candidate_lock else None,
        derivation_source_count=candidate_lock.derivation_source_count if candidate_lock else None,
        evaluation=evaluation,
        range_hit_p_value=_round_optional(range_hit_p_value),
        guided_candidate_win_count=win_count,
        guided_candidate_loss_count=loss_count,
        guided_candidate_tie_count=tie_count,
        guided_candidate_p_value=_round_optional(guided_candidate_p_value),
        baseline_search_seconds=_sum_arm_metric(compatible_results, "baseline", "search_seconds"),
        guided_search_seconds=_sum_arm_metric(compatible_results, "guided", "search_seconds"),
        baseline_workflow_seconds=_sum_arm_metric(compatible_results, "baseline", "workflow_seconds"),
        guided_workflow_seconds=_sum_arm_metric(compatible_results, "guided", "workflow_seconds"),
        duplicate_review_count=len(duplicate_results),
        duplicate_review_agreement=duplicate_review_agreement,
        decision=decision,
        blocking_reasons=tuple(blockers),
    )


def _build_fallback_conformance_report(
        plan: AV1ColdStartValidationCellPlanV1,
        complete_results: Sequence[AV1ColdStartValidationResultV1],
        failed_results: Sequence[AV1ColdStartValidationResultV1],
        stopped_results: Sequence[AV1ColdStartValidationResultV1],
        missing_case_count: int,
        candidate_lock: AV1ColdStartValidationCandidateLockV1 | None,
        blockers: list[str],
) -> AV1ColdStartValidationCellReportV1:
    if candidate_lock is not None:
        blockers.append("unexpected_candidate_lock")
    for result in complete_results:
        if result.prediction_status != "no_recommendation":
            blockers.append("unexpected_recommendation")
        if result.fallback_reason not in plan.allowed_fallback_reasons:
            blockers.append("unexpected_fallback_reason")
        if result.guided is not None or result.candidate_lock_id is not None:
            blockers.append("fallback_changed_search")
        if result.local_evidence_present:
            blockers.append("local_evidence_contamination")
    blockers = sorted(set(blockers))
    if missing_case_count:
        decision: AV1ColdStartValidationDecision = "not_ready"
    elif failed_results or stopped_results or blockers:
        decision = "conformance_failed"
    else:
        decision = "conformance_passed"
    return AV1ColdStartValidationCellReportV1(
        cell_plan_id=plan.cell_plan_id,
        name=plan.name,
        mode=plan.mode,
        intent_level=plan.intent_level,
        registered_case_count=plan.registered_case_count,
        complete_case_count=len(complete_results),
        failed_case_count=len(failed_results),
        stopped_case_count=len(stopped_results),
        missing_case_count=missing_case_count,
        derivation_evidence_count=None,
        derivation_source_count=None,
        evaluation=None,
        range_hit_p_value=None,
        guided_candidate_win_count=0,
        guided_candidate_loss_count=0,
        guided_candidate_tie_count=0,
        guided_candidate_p_value=None,
        baseline_search_seconds=_sum_arm_metric(complete_results, "baseline", "search_seconds"),
        guided_search_seconds=None,
        baseline_workflow_seconds=_sum_arm_metric(complete_results, "baseline", "workflow_seconds"),
        guided_workflow_seconds=None,
        duplicate_review_count=0,
        duplicate_review_agreement=None,
        decision=decision,
        blocking_reasons=tuple(blockers),
    )


def _criteria_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartValidationCriteriaV1:
    expected_keys = {
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
    }
    _require_exact_keys(payload, expected_keys, "criteria")
    return AV1ColdStartValidationCriteriaV1(
        minimum_derivation_evidence_count=_integer(
            payload["minimum_derivation_evidence_count"],
            "minimum derivation evidence",
        ),
        minimum_derivation_source_count=_integer(
            payload["minimum_derivation_source_count"],
            "minimum derivation sources",
        ),
        minimum_holdout_count=_integer(payload["minimum_holdout_count"], "minimum holdout count"),
        minimum_holdout_source_count=_integer(
            payload["minimum_holdout_source_count"],
            "minimum holdout sources",
        ),
        maximum_derivation_age_days=_integer(
            payload["maximum_derivation_age_days"],
            "maximum derivation age",
        ),
        maximum_candidate_crf_span=_number(
            payload["maximum_candidate_crf_span"],
            "maximum candidate CRF span",
        ),
        range_hit_null_rate=_number(payload["range_hit_null_rate"], "range-hit null rate"),
        maximum_one_sided_p_value=_number(
            payload["maximum_one_sided_p_value"],
            "maximum p-value",
        ),
        maximum_safety_regression_count=_integer(
            payload["maximum_safety_regression_count"],
            "maximum safety regressions",
        ),
        duplicate_review_fraction=_number(
            payload["duplicate_review_fraction"],
            "duplicate-review fraction",
        ),
        minimum_duplicate_review_agreement=_number(
            payload["minimum_duplicate_review_agreement"],
            "duplicate-review agreement",
        ),
        confidence_level=_text(payload["confidence_level"], "confidence level"),
        confidence_score=_number(payload["confidence_score"], "confidence score"),
    )


def _trait_selector_from_payload(
        payload: Mapping[str, Any],
) -> AV1ColdStartValidationTraitSelectorV1:
    _require_exact_keys(payload, {"match", "traits"}, "trait selector")
    return AV1ColdStartValidationTraitSelectorV1(
        match=cast(AV1ColdStartValidationTraitMatch, _text(payload["match"], "trait match")),
        traits=tuple(_text(value, "trait") for value in _sequence(payload["traits"], "traits")),
    )


def _cell_plan_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartValidationCellPlanV1:
    expected_keys = {
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
    }
    _require_exact_keys(payload, expected_keys, "cell plan")
    return AV1ColdStartValidationCellPlanV1(
        cell_plan_id=_text(payload["cell_plan_id"], "cell-plan ID"),
        name=_text(payload["name"], "cell-plan name"),
        mode=cast(AV1ColdStartValidationMode, _text(payload["mode"], "cell-plan mode")),
        stratum=cast(
            AV1ColdStartValidationStratum,
            _text(payload["stratum"], "cell-plan stratum"),
        ),
        intent_level=cast(
            CompressionIntentLevel,
            _text(payload["intent_level"], "intent level"),
        ),
        optimization_objective=_text(payload["optimization_objective"], "optimization objective"),
        trait_selector=_trait_selector_from_payload(_mapping(payload["trait_selector"], "trait selector")),
        registered_case_count=_integer(payload["registered_case_count"], "registered case count"),
        expected_prediction=cast(
            AV1ColdStartValidationPredictionStatus,
            _text(payload["expected_prediction"], "expected prediction"),
        ),
        allowed_fallback_reasons=tuple(
            _text(value, "fallback reason")
            for value in _sequence(payload["allowed_fallback_reasons"], "fallback reasons")
        ),
        target_confidence=_text(payload["target_confidence"], "target confidence"),
        duplicate_review_ordinals=tuple(
            _integer(value, "duplicate-review ordinal")
            for value in _sequence(payload["duplicate_review_ordinals"], "duplicate-review ordinals")
        ),
    )


def _case_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartValidationCaseV1:
    _require_exact_keys(payload, {"case_id", "cell_plan_id", "ordinal", "execution_order"}, "case")
    return AV1ColdStartValidationCaseV1(
        case_id=_text(payload["case_id"], "case ID"),
        cell_plan_id=_text(payload["cell_plan_id"], "cell-plan ID"),
        ordinal=_integer(payload["ordinal"], "case ordinal"),
        execution_order=cast(
            AV1ColdStartValidationExecutionOrder,
            _text(payload["execution_order"], "execution order"),
        ),
    )


def _candidate_lock_from_payload(
        payload: Mapping[str, Any],
) -> AV1ColdStartValidationCandidateLockV1:
    expected_keys = {
        "candidate_lock_id",
        "manifest_id",
        "cell_plan_id",
        "exact_traits",
        "crf_lower",
        "crf_center",
        "crf_upper",
        "compatibility_signature",
        "policy_signature",
        "target_video_bitrate_min_bps",
        "target_video_bitrate_max_bps",
        "minimum_quality_score",
        "confidence_level",
        "confidence_score",
        "derivation_evidence_count",
        "derivation_source_count",
        "derivation_source_tokens",
        "derivation_title_tokens",
        "derivation_series_tokens",
        "derivation_source_group_tokens",
        "derivation_source_group_observation_tokens",
        "derivation_oldest_recorded_at",
        "derivation_newest_recorded_at",
        "derivation_conflict_count",
        "derivation_snapshot_sha256",
        "selection_lock_sha256",
        "locked_at",
        "reviewed_at",
        "review_state",
        "payload_sha256",
    }
    _require_exact_keys(payload, expected_keys, "candidate lock")
    return AV1ColdStartValidationCandidateLockV1(
        candidate_lock_id=_text(payload["candidate_lock_id"], "candidate-lock ID"),
        manifest_id=_text(payload["manifest_id"], "manifest ID"),
        cell_plan_id=_text(payload["cell_plan_id"], "cell-plan ID"),
        exact_traits=tuple(
            _text(value, "trait") for value in _sequence(payload["exact_traits"], "exact traits")
        ),
        crf_lower=_number(payload["crf_lower"], "candidate lower CRF"),
        crf_center=_number(payload["crf_center"], "candidate center CRF"),
        crf_upper=_number(payload["crf_upper"], "candidate upper CRF"),
        compatibility_signature=_text(
            payload["compatibility_signature"],
            "compatibility signature",
        ),
        policy_signature=_text(payload["policy_signature"], "policy signature"),
        target_video_bitrate_min_bps=_integer(
            payload["target_video_bitrate_min_bps"],
            "minimum target bitrate",
        ),
        target_video_bitrate_max_bps=_integer(
            payload["target_video_bitrate_max_bps"],
            "maximum target bitrate",
        ),
        minimum_quality_score=_number(payload["minimum_quality_score"], "minimum quality score"),
        confidence_level=_text(payload["confidence_level"], "candidate confidence level"),
        confidence_score=_number(payload["confidence_score"], "candidate confidence score"),
        derivation_evidence_count=_integer(
            payload["derivation_evidence_count"],
            "derivation evidence count",
        ),
        derivation_source_count=_integer(
            payload["derivation_source_count"],
            "derivation source count",
        ),
        derivation_source_tokens=tuple(
            _text(value, "derivation source token")
            for value in _sequence(payload["derivation_source_tokens"], "derivation source tokens")
        ),
        derivation_title_tokens=tuple(
            _text(value, "derivation title token")
            for value in _sequence(
                payload["derivation_title_tokens"],
                "derivation title tokens",
            )
        ),
        derivation_series_tokens=tuple(
            _text(value, "derivation series token")
            for value in _sequence(payload["derivation_series_tokens"], "derivation series tokens")
        ),
        derivation_source_group_tokens=tuple(
            _text(value, "derivation source-group token")
            for value in _sequence(
                payload["derivation_source_group_tokens"],
                "derivation source-group tokens",
            )
        ),
        derivation_source_group_observation_tokens=tuple(
            _text(value, "derivation source-group observation token")
            for value in _sequence(
                payload["derivation_source_group_observation_tokens"],
                "derivation source-group observation tokens",
            )
        ),
        derivation_oldest_recorded_at=_text(
            payload["derivation_oldest_recorded_at"],
            "oldest derivation observation",
        ),
        derivation_newest_recorded_at=_text(
            payload["derivation_newest_recorded_at"],
            "newest derivation observation",
        ),
        derivation_conflict_count=_integer(
            payload["derivation_conflict_count"],
            "derivation conflict count",
        ),
        derivation_snapshot_sha256=_text(
            payload["derivation_snapshot_sha256"],
            "derivation snapshot digest",
        ),
        selection_lock_sha256=_text(payload["selection_lock_sha256"], "selection-lock digest"),
        locked_at=_text(payload["locked_at"], "candidate lock"),
        reviewed_at=_text(payload["reviewed_at"], "candidate review"),
        review_state=_text(payload["review_state"], "candidate review state"),
        payload_sha256=_text(payload["payload_sha256"], "candidate-lock digest"),
    )


def _execution_authorization_from_payload(
        payload: Mapping[str, Any],
) -> AV1ColdStartValidationExecutionAuthorizationV1:
    expected_keys = {
        "authorization_id",
        "manifest_id",
        "selection_lock_sha256",
        "candidate_lock_ids",
        "review_environment_token",
        "authorized_at",
        "review_state",
        "payload_sha256",
    }
    _require_exact_keys(payload, expected_keys, "execution authorization")
    return AV1ColdStartValidationExecutionAuthorizationV1(
        authorization_id=_text(payload["authorization_id"], "authorization ID"),
        manifest_id=_text(payload["manifest_id"], "manifest ID"),
        selection_lock_sha256=_text(payload["selection_lock_sha256"], "selection-lock digest"),
        candidate_lock_ids=tuple(
            _text(value, "candidate-lock ID")
            for value in _sequence(payload["candidate_lock_ids"], "candidate-lock IDs")
        ),
        review_environment_token=_text(
            payload["review_environment_token"],
            "authorization review token",
        ),
        authorized_at=_text(payload["authorized_at"], "execution authorization"),
        review_state=_text(payload["review_state"], "authorization review state"),
        payload_sha256=_text(payload["payload_sha256"], "authorization digest"),
    )


def _arm_result_from_payload(payload: object, label: str) -> AV1ColdStartValidationArmResultV1 | None:
    if payload is None:
        return None
    value = _mapping(payload, label)
    expected_keys = {
        "selected_crf",
        "candidate_count",
        "search_seconds",
        "workflow_seconds",
        "measured_quality_score",
        "final_size_bytes",
        "quality_floor_met",
        "final_size_met",
        "visual_accepted",
        "operator_attention_events",
    }
    _require_exact_keys(value, expected_keys, label)
    return AV1ColdStartValidationArmResultV1(
        selected_crf=_number(value["selected_crf"], f"{label} selected CRF"),
        candidate_count=_integer(value["candidate_count"], f"{label} candidate count"),
        search_seconds=_number(value["search_seconds"], f"{label} search seconds"),
        workflow_seconds=_number(value["workflow_seconds"], f"{label} workflow seconds"),
        measured_quality_score=_number(
            value["measured_quality_score"],
            f"{label} measured quality",
        ),
        final_size_bytes=_integer(value["final_size_bytes"], f"{label} final size"),
        quality_floor_met=_boolean(value["quality_floor_met"], f"{label} quality-floor status"),
        final_size_met=_boolean(value["final_size_met"], f"{label} final-size status"),
        visual_accepted=_boolean(value["visual_accepted"], f"{label} visual verdict"),
        operator_attention_events=_integer(
            value["operator_attention_events"],
            f"{label} attention events",
        ),
    )


def _result_from_payload(payload: Mapping[str, Any]) -> AV1ColdStartValidationResultV1:
    expected_keys = {
        "result_id",
        "manifest_id",
        "cell_plan_id",
        "case_id",
        "execution_order",
        "completed_at",
        "status",
        "source_token",
        "title_token",
        "series_token",
        "source_group_token",
        "content_traits",
        "intent_level",
        "compatibility_signature",
        "policy_signature",
        "review_environment_token",
        "target_video_bitrate_bps",
        "prediction_status",
        "fallback_reason",
        "local_evidence_present",
        "candidate_lock_id",
        "blinded_visual_review",
        "duplicate_guided_visual_accepted",
        "duplicate_review_environment_token",
        "baseline",
        "guided",
        "failure_reason",
        "payload_sha256",
    }
    _require_exact_keys(payload, expected_keys, "result")
    return AV1ColdStartValidationResultV1(
        result_id=_text(payload["result_id"], "result ID"),
        manifest_id=_text(payload["manifest_id"], "manifest ID"),
        cell_plan_id=_text(payload["cell_plan_id"], "cell-plan ID"),
        case_id=_text(payload["case_id"], "case ID"),
        execution_order=cast(
            AV1ColdStartValidationExecutionOrder,
            _text(payload["execution_order"], "execution order"),
        ),
        completed_at=_text(payload["completed_at"], "result completion"),
        status=cast(AV1ColdStartValidationStatus, _text(payload["status"], "result status")),
        source_token=_text(payload["source_token"], "source token"),
        title_token=_text(payload["title_token"], "title token"),
        series_token=_text(payload["series_token"], "series token"),
        source_group_token=_text(payload["source_group_token"], "source-group token"),
        content_traits=tuple(
            _text(value, "trait") for value in _sequence(payload["content_traits"], "content traits")
        ),
        intent_level=cast(
            CompressionIntentLevel,
            _text(payload["intent_level"], "intent level"),
        ),
        compatibility_signature=_text(
            payload["compatibility_signature"],
            "compatibility signature",
        ),
        policy_signature=_text(payload["policy_signature"], "policy signature"),
        review_environment_token=_text(
            payload["review_environment_token"],
            "review-environment token",
        ),
        target_video_bitrate_bps=_integer(
            payload["target_video_bitrate_bps"],
            "target video bitrate",
        ),
        prediction_status=cast(
            AV1ColdStartValidationPredictionStatus | None,
            _optional_text(payload["prediction_status"]),
        ),
        fallback_reason=_optional_text(payload["fallback_reason"]),
        local_evidence_present=_boolean(payload["local_evidence_present"], "local evidence status"),
        candidate_lock_id=_optional_text(payload["candidate_lock_id"]),
        blinded_visual_review=_boolean(payload["blinded_visual_review"], "blinded-review status"),
        duplicate_guided_visual_accepted=_optional_boolean(
            payload["duplicate_guided_visual_accepted"],
            "duplicate visual verdict",
        ),
        duplicate_review_environment_token=_optional_text(
            payload["duplicate_review_environment_token"]
        ),
        baseline=_arm_result_from_payload(payload["baseline"], "baseline"),
        guided=_arm_result_from_payload(payload["guided"], "guided"),
        failure_reason=_optional_text(payload["failure_reason"]),
        payload_sha256=_text(payload["payload_sha256"], "result digest"),
    )


def _validate_manifest_coverage(
        plans: Sequence[AV1ColdStartValidationCellPlanV1],
) -> None:
    candidate_traits = {
        trait
        for plan in plans
        if plan.mode == "publication_candidate"
        for trait in plan.trait_selector.traits
    }
    if not {"animation", "typical"}.issubset(candidate_traits):
        raise AV1ColdStartValidationError("AV1 validation requires animation and live-action candidates")
    fallback_traits = {
        trait
        for plan in plans
        if plan.mode == "fallback_conformance"
        for trait in plan.trait_selector.traits
    }
    required_fallback_traits = {
        "darkness",
        "motion",
        "grain_noise",
        "texture_detail",
        "low_motion_dialogue",
        "mixed",
        "unknown",
    }
    if not required_fallback_traits.issubset(fallback_traits):
        raise AV1ColdStartValidationError("AV1 validation risk coverage is incomplete")
    intents = {plan.intent_level for plan in plans}
    if not set(COMPRESSION_INTENT_LEVELS).issubset(intents):
        raise AV1ColdStartValidationError("AV1 validation intent coverage is incomplete")


def _case_execution_order(
        plan: AV1ColdStartValidationCellPlanV1,
        ordinal: int,
) -> AV1ColdStartValidationExecutionOrder:
    if ordinal <= 0:
        raise AV1ColdStartValidationError("AV1 validation case ordinal must be positive")
    if plan.mode != "publication_candidate":
        return "baseline_only"
    return "baseline_then_guided" if ordinal % 2 else "guided_then_baseline"


def _validation_id(kind: str, payload: object) -> str:
    return f"av1v{kind}1_{stable_json_hash(payload)[:32]}"


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _one_sided_binomial_p_value(successes: int, trials: int, null_rate: float) -> float:
    if trials <= 0 or not 0 <= successes <= trials:
        raise AV1ColdStartValidationError("AV1 validation binomial inputs are invalid")
    if not math.isfinite(null_rate) or not 0 < null_rate < 1:
        raise AV1ColdStartValidationError("AV1 validation binomial null rate is invalid")
    probability = sum(
        math.comb(trials, count)
        * (null_rate ** count)
        * ((1 - null_rate) ** (trials - count))
        for count in range(successes, trials + 1)
    )
    return round(probability, 12)


def _sum_arm_metric(
        results: Sequence[AV1ColdStartValidationResultV1],
        arm_name: Literal["baseline", "guided"],
        metric_name: Literal["search_seconds", "workflow_seconds"],
) -> float | None:
    values: list[float] = []
    for result in results:
        arm = result.baseline if arm_name == "baseline" else result.guided
        if arm is not None:
            values.append(float(getattr(arm, metric_name)))
    if not values:
        return None
    try:
        total = math.fsum(values)
    except OverflowError as exc:
        raise AV1ColdStartValidationError("AV1 validation aggregate work metric overflowed") from exc
    if not math.isfinite(total):
        raise AV1ColdStartValidationError("AV1 validation aggregate work metric must be finite")
    return round(total, 6)


def _round_optional(value: float | None) -> float | None:
    return round(value, 12) if value is not None else None


def _parse_timestamp(value: str, label: str) -> datetime:
    text = value.strip()
    if not text.endswith("Z"):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise AV1ColdStartValidationError(f"AV1 validation {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be UTC")
    return parsed


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        extra = sorted(actual.difference(expected))
        raise AV1ColdStartValidationError(
            f"AV1 validation {label} keys are invalid: missing={missing} extra={extra}"
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AV1ColdStartValidationError("AV1 validation optional text must be non-empty")
    return value.strip()


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be numeric")
    try:
        number = float(value)
    except OverflowError as exc:
        raise AV1ColdStartValidationError(f"AV1 validation {label} is too large") from exc
    if not math.isfinite(number):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be finite")
    return number


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AV1ColdStartValidationError(f"AV1 validation {label} must be boolean")
    return value


def _optional_boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, label)
