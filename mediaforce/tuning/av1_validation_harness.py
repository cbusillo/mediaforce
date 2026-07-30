from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.quality import QualitySearchResult, QualitySearchWarmStart
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    assert_av1_cold_start_public_payload_safe,
)
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationCandidateLockV1,
    AV1ColdStartValidationCellPlanV1,
    AV1ColdStartValidationError,
    AV1ColdStartValidationExecutionAuthorizationV1,
    AV1ColdStartValidationManifestV1,
    resolve_av1_cold_start_validation_candidate_lock_set,
)
from mediaforce.tuning.target_size_search import search_target_size


AV1_VALIDATION_HARNESS_SCHEMA = "mediaforce.av1_validation_harness_trace"
AV1_VALIDATION_HARNESS_SCHEMA_VERSION = 1
AV1_VALIDATION_HARNESS_CONTRACT_VERSION = "av1vh1"
AV1_VALIDATION_HARNESS_SOURCE = "av1_validation_harness"
_SEARCH_STATUSES = frozenset({"selected", "infeasible", "bound_exhausted", "quality_conflict", "needs_review"})
_SEARCH_REASONS = frozenset({
    "all_candidates_violate_quality_floor",
    "arithmetically_infeasible_stream_budget",
    "bounded_search_exhausted",
    "bounded_search_exhausted_before_lower_bound",
    "bounded_search_exhausted_before_upper_bound",
    "candidate_inside_sample_projection_band",
    "largest_quality_safe_candidate_under_target_band",
    "missing_target_video_budget",
    "non_monotonic_size_curve_exhausted",
    "non_video_budget_requires_measurement",
    "quality_memory_candidate_inside_sample_projection_band",
    "smallest_quality_safe_candidate_over_target_band",
    "source_relative_cap_consumed_by_non_video_budget",
    "target_band_violates_quality_floor",
    "target_lower_bound_exceeds_source_relative_cap",
    "target_requires_crossing_quality_floor",
    "transform_plan_identity_invalid",
})
_DECISION_FALLBACK_REASONS = frozenset({
    "compression_intent_requires_directional_search",
    "content_profile_uncovered",
})
_TRACE_FALLBACK_REASONS = _DECISION_FALLBACK_REASONS | frozenset({
    "candidate_rejected",
    "invalid_hint_contract",
    "probe_error",
    "quality_floor_miss",
    "source_cap_miss",
    "target_band_miss",
})

AV1ValidationHarnessMode = Literal["baseline", "guided"]
AV1ValidationHarnessPredictionStatus = Literal["baseline", "recommended", "no_recommendation"]


class AV1ValidationHarnessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationHarnessMachineBindingV1:
    binding_id: str
    compatibility_signature: str
    policy_signature: str
    search_signature_id: str
    target_video_bitrate_bps: int
    target_video_bytes: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    transform_plan_id: str
    encoder: str
    encoder_version: str
    encoder_runtime_signature_id: str
    quality_tool: str
    quality_tool_version: str
    metric_runtime_signature_id: str

    def __post_init__(self) -> None:
        text_values = (
            self.compatibility_signature,
            self.policy_signature,
            self.search_signature_id,
            self.quality_metric,
            self.transform_plan_id,
            self.encoder,
            self.encoder_version,
            self.encoder_runtime_signature_id,
            self.quality_tool,
            self.quality_tool_version,
            self.metric_runtime_signature_id,
        )
        if any(not value.strip() for value in text_values):
            raise AV1ValidationHarnessError("AV1 validation machine binding requires complete identities")
        if self.target_video_bitrate_bps <= 0 or self.target_video_bytes <= 0:
            raise AV1ValidationHarnessError("AV1 validation machine target must be positive")
        if any(
                not math.isfinite(value) or value < 0
                for value in (self.quality_target, self.minimum_quality_score)
        ):
            raise AV1ValidationHarnessError("AV1 validation machine quality floor is invalid")
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationHarnessError("AV1 validation minimum quality exceeds its target")
        if self.binding_id != _harness_id("machine", self.semantic_payload()):
            raise AV1ValidationHarnessError("AV1 validation machine-binding ID does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "search_signature_id": self.search_signature_id,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
            "target_video_bytes": self.target_video_bytes,
            "quality_metric": self.quality_metric.upper(),
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "transform_plan_id": self.transform_plan_id,
            "encoder": self.encoder,
            "encoder_version": self.encoder_version,
            "encoder_runtime_signature_id": self.encoder_runtime_signature_id,
            "quality_tool": self.quality_tool,
            "quality_tool_version": self.quality_tool_version,
            "metric_runtime_signature_id": self.metric_runtime_signature_id,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"binding_id": self.binding_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationCandidateContextV1:
    context_id: str
    manifest_id: str
    cell_plan_id: str
    candidate_lock_id: str
    authorization_id: str
    machine_binding_id: str
    intent_level: str
    exact_traits: tuple[str, ...]
    requested_crf: float
    candidate_crf: int
    crf_lower: float
    crf_upper: float
    search_signature_id: str
    candidate_lock_payload_sha256: str
    authorization_payload_sha256: str
    local_evidence_present: bool = False
    production_prediction_allowed: bool = False

    def __post_init__(self) -> None:
        if self.exact_traits != tuple(sorted(set(self.exact_traits))) or not self.exact_traits:
            raise AV1ValidationHarnessError("AV1 validation context traits must be unique and sorted")
        if not 0 <= self.crf_lower <= self.requested_crf <= self.crf_upper <= 63:
            raise AV1ValidationHarnessError("AV1 validation context CRF range is invalid")
        if not self.crf_lower <= self.candidate_crf <= self.crf_upper:
            raise AV1ValidationHarnessError("AV1 validation candidate CRF is outside the locked range")
        if self.local_evidence_present or self.production_prediction_allowed:
            raise AV1ValidationHarnessError("AV1 validation context cannot use local evidence or production routing")
        if self.context_id != _harness_id("context", self.semantic_payload()):
            raise AV1ValidationHarnessError("AV1 validation context ID does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
            "scope": "isolated_validation_only",
            "manifest_id": self.manifest_id,
            "cell_plan_id": self.cell_plan_id,
            "candidate_lock_id": self.candidate_lock_id,
            "authorization_id": self.authorization_id,
            "machine_binding_id": self.machine_binding_id,
            "intent_level": self.intent_level,
            "exact_traits": list(self.exact_traits),
            "requested_crf": self.requested_crf,
            "candidate_crf": self.candidate_crf,
            "crf_lower": self.crf_lower,
            "crf_upper": self.crf_upper,
            "search_signature_id": self.search_signature_id,
            "candidate_lock_payload_sha256": self.candidate_lock_payload_sha256,
            "authorization_payload_sha256": self.authorization_payload_sha256,
            "local_evidence_present": self.local_evidence_present,
            "production_prediction_allowed": self.production_prediction_allowed,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"context_id": self.context_id, **self.semantic_payload()}


@dataclass(frozen=True, slots=True)
class AV1ValidationHarnessDecisionV1:
    manifest_id: str
    cell_plan_id: str
    mode: AV1ValidationHarnessMode
    prediction_status: AV1ValidationHarnessPredictionStatus
    intent_level: str
    optimization_objective: str
    content_traits: tuple[str, ...]
    machine_binding_id: str
    context: AV1ValidationCandidateContextV1 | None
    fallback_reason: str | None
    local_evidence_present: bool

    def __post_init__(self) -> None:
        if not self.manifest_id or not self.cell_plan_id:
            raise AV1ValidationHarnessError("AV1 validation decision requires manifest and plan bindings")
        if self.local_evidence_present:
            raise AV1ValidationHarnessError("AV1 validation decision rejects private local personalization")
        if self.mode == "baseline":
            if self.prediction_status != "baseline" or self.context is not None or self.fallback_reason is not None:
                raise AV1ValidationHarnessError("AV1 validation baseline cannot carry a candidate or fallback")
        elif self.prediction_status == "recommended":
            if self.context is None or self.fallback_reason is not None:
                raise AV1ValidationHarnessError("AV1 validation recommendation requires one candidate context")
            if (
                    self.context.manifest_id != self.manifest_id
                    or self.context.cell_plan_id != self.cell_plan_id
            ):
                raise AV1ValidationHarnessError("AV1 validation candidate context does not match its decision")
        elif self.prediction_status == "no_recommendation":
            if self.context is not None or not self.fallback_reason:
                raise AV1ValidationHarnessError("AV1 validation fallback requires a governed reason")
            if self.fallback_reason not in _DECISION_FALLBACK_REASONS:
                raise AV1ValidationHarnessError("AV1 validation fallback reason is unsupported")
        else:
            raise AV1ValidationHarnessError("AV1 validation guided decision is invalid")

    def search_hint(self) -> QualitySearchWarmStart | None:
        if self.prediction_status != "recommended" or self.context is None:
            return None
        return QualitySearchWarmStart(
            requested_crf=self.context.requested_crf,
            candidate_crf=self.context.candidate_crf,
            search_signature_id=self.context.search_signature_id,
            cohort_id=f"av1vhc1_{stable_json_hash(self.context.context_id)[:24]}",
            source=AV1_VALIDATION_HARNESS_SOURCE,
            confidence="moderate",
            provenance_id=self.context.context_id,
            review_risks=(),
        )


@dataclass(frozen=True, slots=True)
class AV1ValidationHarnessWorkV1:
    ordinal: int
    role: str
    crf: float
    status: str
    quality_floor_met: bool | None
    target_band_met: bool | None

    def __post_init__(self) -> None:
        if self.ordinal <= 0 or not 0 <= self.crf <= 63:
            raise AV1ValidationHarnessError("AV1 validation work item is invalid")
        if self.role not in {"locked_first_probe", "measured_full_search_probe"}:
            raise AV1ValidationHarnessError("AV1 validation work role is unsupported")
        if self.status not in {"measured", "probe_error", "guard_rejected"}:
            raise AV1ValidationHarnessError("AV1 validation work status is unsupported")
        if self.status == "measured":
            if not isinstance(self.quality_floor_met, bool) or not isinstance(self.target_band_met, bool):
                raise AV1ValidationHarnessError("Measured AV1 validation work requires measured flags")
        elif self.quality_floor_met is not None or self.target_band_met is not None:
            raise AV1ValidationHarnessError("Unmeasured AV1 validation work cannot claim measured flags")

    def to_payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "crf": self.crf,
            "status": self.status,
            "quality_floor_met": self.quality_floor_met,
            "target_band_met": self.target_band_met,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationHarnessTraceV1:
    trace_id: str
    manifest_id: str
    cell_plan_id: str
    mode: AV1ValidationHarnessMode
    prediction_status: AV1ValidationHarnessPredictionStatus
    fallback_reason: str | None
    machine_binding_id: str
    context_id: str | None
    candidate_lock_id: str | None
    authorization_id: str | None
    local_evidence_present: bool
    fallback_invoked: bool
    search_status: str
    search_selection_reason: str
    observed_search_digest: str
    ordered_work: tuple[AV1ValidationHarnessWorkV1, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.mode not in {"baseline", "guided"}:
            raise AV1ValidationHarnessError("AV1 validation trace mode is unsupported")
        if self.prediction_status not in {"baseline", "recommended", "no_recommendation"}:
            raise AV1ValidationHarnessError("AV1 validation trace prediction status is unsupported")
        if self.local_evidence_present:
            raise AV1ValidationHarnessError("AV1 validation trace contains private local personalization")
        if not self.ordered_work:
            raise AV1ValidationHarnessError("AV1 validation trace requires observed search work")
        if self.ordered_work != tuple(sorted(self.ordered_work, key=lambda work: work.ordinal)):
            raise AV1ValidationHarnessError("AV1 validation work must be ordered")
        if tuple(work.ordinal for work in self.ordered_work) != tuple(range(1, len(self.ordered_work) + 1)):
            raise AV1ValidationHarnessError("AV1 validation work ordinals must be contiguous")
        if self.prediction_status == "recommended":
            if self.context_id is None or self.candidate_lock_id is None or self.authorization_id is None:
                raise AV1ValidationHarnessError("AV1 validation recommendation trace is not bound")
            if self.ordered_work[0].role != "locked_first_probe":
                raise AV1ValidationHarnessError("AV1 validation guided trace must start with the locked probe")
            if sum(work.role == "locked_first_probe" for work in self.ordered_work) != 1:
                raise AV1ValidationHarnessError("AV1 validation guided trace must contain exactly one locked probe")
        else:
            if any(work.role == "locked_first_probe" for work in self.ordered_work):
                raise AV1ValidationHarnessError("AV1 validation baseline/fallback trace cannot carry a locked probe")
            if self.context_id is not None or self.candidate_lock_id is not None or self.authorization_id is not None:
                raise AV1ValidationHarnessError("AV1 validation baseline/fallback trace cannot carry candidate bindings")
        if self.prediction_status == "baseline" and self.fallback_invoked:
            raise AV1ValidationHarnessError("AV1 validation baseline cannot claim fallback execution")
        if self.prediction_status == "no_recommendation" and not self.fallback_invoked:
            raise AV1ValidationHarnessError("AV1 validation no-recommendation trace must execute fallback")
        if self.prediction_status == "recommended" and self.fallback_invoked and not self.fallback_reason:
            raise AV1ValidationHarnessError("AV1 validation guided fallback requires the observed reason")
        if self.fallback_reason is not None and self.fallback_reason not in _TRACE_FALLBACK_REASONS:
            raise AV1ValidationHarnessError("AV1 validation trace fallback reason is unsupported")
        if not self.manifest_id.startswith(("av1vmanifest1_", "av1vmanifest2_")) or not self.cell_plan_id.startswith(
                ("av1vplan1_", "av1vplan2_")
        ):
            raise AV1ValidationHarnessError("AV1 validation trace manifest or plan ID is invalid")
        if not self.machine_binding_id.startswith("av1vhm1_"):
            raise AV1ValidationHarnessError("AV1 validation trace machine binding is invalid")
        if self.context_id is not None and not self.context_id.startswith("av1vhc1_"):
            raise AV1ValidationHarnessError("AV1 validation trace context ID is invalid")
        if self.candidate_lock_id is not None and not self.candidate_lock_id.startswith("av1vlock1_"):
            raise AV1ValidationHarnessError("AV1 validation trace candidate-lock ID is invalid")
        if self.authorization_id is not None and not self.authorization_id.startswith("av1vauthorization1_"):
            raise AV1ValidationHarnessError("AV1 validation trace authorization ID is invalid")
        if self.search_status not in _SEARCH_STATUSES or self.search_selection_reason not in _SEARCH_REASONS:
            raise AV1ValidationHarnessError("AV1 validation search outcome is unsupported")
        semantic_payload = self.semantic_payload()
        if self.trace_id != _harness_id("trace", semantic_payload):
            raise AV1ValidationHarnessError("AV1 validation trace ID does not match its payload")
        if self.payload_sha256 != f"sha256:{stable_json_hash({'trace_id': self.trace_id, **semantic_payload})}":
            raise AV1ValidationHarnessError("AV1 validation trace digest does not match its payload")
        _assert_public_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_HARNESS_SCHEMA,
            "schema_version": AV1_VALIDATION_HARNESS_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
            "trace_origin": "validated_target_size_search_v1",
            "manifest_id": self.manifest_id,
            "cell_plan_id": self.cell_plan_id,
            "mode": self.mode,
            "prediction_status": self.prediction_status,
            "fallback_reason": self.fallback_reason,
            "machine_binding_id": self.machine_binding_id,
            "context_id": self.context_id,
            "candidate_lock_id": self.candidate_lock_id,
            "authorization_id": self.authorization_id,
            "local_evidence_present": self.local_evidence_present,
            "fallback_invoked": self.fallback_invoked,
            "search_status": self.search_status,
            "search_selection_reason": self.search_selection_reason,
            "observed_search_digest": self.observed_search_digest,
            "ordered_work": [work.to_payload() for work in self.ordered_work],
        }

    def to_payload(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, **self.semantic_payload(), "payload_sha256": self.payload_sha256}


def build_av1_validation_harness_machine_binding(
        *,
        compatibility_signature: str,
        policy_signature: str,
        search_signature_id: str,
        target_video_bitrate_bps: int,
        target_video_bytes: int,
        quality_metric: str,
        quality_target: float,
        minimum_quality_score: float,
        transform_plan_id: str,
        encoder: str,
        encoder_version: str,
        encoder_runtime_signature_id: str,
        quality_tool: str,
        quality_tool_version: str,
        metric_runtime_signature_id: str,
) -> AV1ValidationHarnessMachineBindingV1:
    semantic_payload = {
        "compatibility_signature": compatibility_signature,
        "policy_signature": policy_signature,
        "search_signature_id": search_signature_id,
        "target_video_bitrate_bps": target_video_bitrate_bps,
        "target_video_bytes": target_video_bytes,
        "quality_metric": quality_metric.upper(),
        "quality_target": quality_target,
        "minimum_quality_score": minimum_quality_score,
        "transform_plan_id": transform_plan_id,
        "encoder": encoder,
        "encoder_version": encoder_version,
        "encoder_runtime_signature_id": encoder_runtime_signature_id,
        "quality_tool": quality_tool,
        "quality_tool_version": quality_tool_version,
        "metric_runtime_signature_id": metric_runtime_signature_id,
    }
    return AV1ValidationHarnessMachineBindingV1(
        binding_id=_harness_id("machine", semantic_payload),
        **semantic_payload,
    )


def build_av1_validation_candidate_context(
        *,
        manifest: AV1ColdStartValidationManifestV1,
        plan: AV1ColdStartValidationCellPlanV1,
        candidate_locks: Sequence[AV1ColdStartValidationCandidateLockV1],
        authorization: AV1ColdStartValidationExecutionAuthorizationV1,
        machine_binding: AV1ValidationHarnessMachineBindingV1,
        configured_min_crf: int,
        configured_max_crf: int,
        local_evidence_present: bool = False,
) -> AV1ValidationCandidateContextV1:
    if plan not in manifest.cell_plans or plan.mode != "publication_candidate":
        raise AV1ValidationHarnessError("AV1 validation candidate requires a manifest publication plan")
    if authorization.manifest_id != manifest.manifest_id:
        raise AV1ValidationHarnessError("AV1 validation authorization references another manifest")
    locks_by_plan = _resolve_authorized_candidate_locks(manifest, candidate_locks, authorization)
    for publication_plan in manifest.cell_plans:
        if publication_plan.mode != "publication_candidate":
            continue
        _assert_candidate_lock_is_executable(
            manifest=manifest,
            plan=publication_plan,
            candidate_lock=locks_by_plan[publication_plan.cell_plan_id],
            authorization=authorization,
            configured_min_crf=configured_min_crf,
            configured_max_crf=configured_max_crf,
        )
    candidate_lock = locks_by_plan[plan.cell_plan_id]
    if candidate_lock.compatibility_signature != machine_binding.compatibility_signature:
        raise AV1ValidationHarnessError("AV1 validation machine compatibility differs from the candidate lock")
    if candidate_lock.policy_signature != machine_binding.policy_signature:
        raise AV1ValidationHarnessError("AV1 validation machine policy differs from the candidate lock")
    if not (
            candidate_lock.target_video_bitrate_min_bps
            <= machine_binding.target_video_bitrate_bps
            <= candidate_lock.target_video_bitrate_max_bps
    ):
        raise AV1ValidationHarnessError("AV1 validation target bitrate is outside the candidate lock")
    if machine_binding.minimum_quality_score != candidate_lock.minimum_quality_score:
        raise AV1ValidationHarnessError("AV1 validation quality floor differs from the candidate lock")
    if local_evidence_present:
        raise AV1ValidationHarnessError("AV1 validation publication candidates reject private local evidence")
    minimum_candidate = math.ceil(candidate_lock.crf_lower)
    maximum_candidate = math.floor(candidate_lock.crf_upper)
    if minimum_candidate > maximum_candidate:
        raise AV1ValidationHarnessError("AV1 validation candidate range has no integer probe")
    candidate_crf = min(
        max(int(math.floor(candidate_lock.crf_center + 0.5)), minimum_candidate),
        maximum_candidate,
    )
    semantic_payload = {
        "contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "scope": "isolated_validation_only",
        "manifest_id": manifest.manifest_id,
        "cell_plan_id": plan.cell_plan_id,
        "candidate_lock_id": candidate_lock.candidate_lock_id,
        "authorization_id": authorization.authorization_id,
        "machine_binding_id": machine_binding.binding_id,
        "intent_level": plan.intent_level,
        "exact_traits": list(candidate_lock.exact_traits),
        "requested_crf": round(candidate_lock.crf_center, 3),
        "candidate_crf": candidate_crf,
        "crf_lower": candidate_lock.crf_lower,
        "crf_upper": candidate_lock.crf_upper,
        "search_signature_id": machine_binding.search_signature_id,
        "candidate_lock_payload_sha256": candidate_lock.payload_sha256,
        "authorization_payload_sha256": authorization.payload_sha256,
        "local_evidence_present": False,
        "production_prediction_allowed": False,
    }
    return AV1ValidationCandidateContextV1(
        context_id=_harness_id("context", semantic_payload),
        manifest_id=manifest.manifest_id,
        cell_plan_id=plan.cell_plan_id,
        candidate_lock_id=candidate_lock.candidate_lock_id,
        authorization_id=authorization.authorization_id,
        machine_binding_id=machine_binding.binding_id,
        intent_level=plan.intent_level,
        exact_traits=candidate_lock.exact_traits,
        requested_crf=round(candidate_lock.crf_center, 3),
        candidate_crf=candidate_crf,
        crf_lower=candidate_lock.crf_lower,
        crf_upper=candidate_lock.crf_upper,
        search_signature_id=machine_binding.search_signature_id,
        candidate_lock_payload_sha256=candidate_lock.payload_sha256,
        authorization_payload_sha256=authorization.payload_sha256,
    )


def _resolve_authorized_candidate_locks(
        manifest: AV1ColdStartValidationManifestV1,
        candidate_locks: Sequence[AV1ColdStartValidationCandidateLockV1],
        authorization: AV1ColdStartValidationExecutionAuthorizationV1,
) -> dict[str, AV1ColdStartValidationCandidateLockV1]:
    try:
        locks_by_plan = resolve_av1_cold_start_validation_candidate_lock_set(
            manifest=manifest,
            candidate_locks=candidate_locks,
            selection_lock_sha256=authorization.selection_lock_sha256,
        )
    except AV1ColdStartValidationError as exc:
        raise AV1ValidationHarnessError(
            "AV1 validation holdout requires one approved candidate lock per publication candidate cell"
        ) from exc
    authorized_lock_ids = tuple(sorted(lock.candidate_lock_id for lock in locks_by_plan.values()))
    if authorization.candidate_lock_ids != authorized_lock_ids:
        raise AV1ValidationHarnessError("AV1 validation authorization does not bind the approved candidate locks")
    return locks_by_plan


def _assert_candidate_lock_is_executable(
        *,
        manifest: AV1ColdStartValidationManifestV1,
        plan: AV1ColdStartValidationCellPlanV1,
        candidate_lock: AV1ColdStartValidationCandidateLockV1,
        authorization: AV1ColdStartValidationExecutionAuthorizationV1,
        configured_min_crf: int,
        configured_max_crf: int,
) -> None:
    if candidate_lock.manifest_id != manifest.manifest_id or candidate_lock.cell_plan_id != plan.cell_plan_id:
        raise AV1ValidationHarnessError("AV1 validation candidate lock does not match its plan")
    if not plan.trait_selector.matches(candidate_lock.exact_traits):
        raise AV1ValidationHarnessError("AV1 validation candidate traits do not match the plan")
    if candidate_lock.review_state != "approved_for_holdout":
        raise AV1ValidationHarnessError("AV1 validation candidate lock is not reviewed")
    if (
            candidate_lock.derivation_evidence_count < manifest.criteria.minimum_derivation_evidence_count
            or candidate_lock.derivation_source_count < manifest.criteria.minimum_derivation_source_count
            or candidate_lock.confidence_level not in {manifest.criteria.confidence_level, "high"}
            or candidate_lock.confidence_score < manifest.criteria.confidence_score
            or candidate_lock.derivation_conflict_count != 0
    ):
        raise AV1ValidationHarnessError("AV1 validation candidate lock does not satisfy derivation gates")
    if candidate_lock.selection_lock_sha256 != authorization.selection_lock_sha256:
        raise AV1ValidationHarnessError("AV1 validation selection-lock binding is inconsistent")
    oldest_derivation = _parse_timestamp(candidate_lock.derivation_oldest_recorded_at)
    newest_derivation = _parse_timestamp(candidate_lock.derivation_newest_recorded_at)
    locked_at = _parse_timestamp(candidate_lock.locked_at)
    reviewed_at = _parse_timestamp(candidate_lock.reviewed_at)
    authorized_at = _parse_timestamp(authorization.authorized_at)
    if not oldest_derivation <= newest_derivation <= locked_at <= reviewed_at <= authorized_at:
        raise AV1ValidationHarnessError("AV1 validation candidate chronology is invalid")
    if (locked_at - oldest_derivation).days > manifest.criteria.maximum_derivation_age_days:
        raise AV1ValidationHarnessError("AV1 validation derivation evidence is stale")
    if (
            candidate_lock.crf_lower < configured_min_crf
            or candidate_lock.crf_upper > configured_max_crf
            or candidate_lock.crf_upper - candidate_lock.crf_lower > manifest.criteria.maximum_candidate_crf_span
    ):
        raise AV1ValidationHarnessError("AV1 validation candidate range is not executable")
    if math.ceil(candidate_lock.crf_lower) > math.floor(candidate_lock.crf_upper):
        raise AV1ValidationHarnessError("AV1 validation candidate range has no integer probe")


def plan_av1_validation_harness_case(
        *,
        manifest: AV1ColdStartValidationManifestV1,
        plan: AV1ColdStartValidationCellPlanV1,
        machine_binding: AV1ValidationHarnessMachineBindingV1,
        mode: AV1ValidationHarnessMode,
        candidate_context: AV1ValidationCandidateContextV1 | None = None,
        local_evidence_present: bool = False,
) -> AV1ValidationHarnessDecisionV1:
    if plan not in manifest.cell_plans:
        raise AV1ValidationHarnessError("AV1 validation plan does not belong to the manifest")
    if local_evidence_present:
        raise AV1ValidationHarnessError("AV1 validation fixtures reject private local personalization")
    if mode == "baseline":
        if candidate_context is not None:
            raise AV1ValidationHarnessError("AV1 validation baseline cannot receive a validation candidate")
        status: AV1ValidationHarnessPredictionStatus = "baseline"
        fallback_reason = None
    elif plan.mode == "publication_candidate":
        if candidate_context is None or candidate_context.cell_plan_id != plan.cell_plan_id:
            raise AV1ValidationHarnessError("AV1 validation publication fixture requires its reviewed candidate")
        if candidate_context.machine_binding_id != machine_binding.binding_id:
            raise AV1ValidationHarnessError("AV1 validation candidate uses another machine binding")
        status = "recommended"
        fallback_reason = None
    else:
        if candidate_context is not None:
            raise AV1ValidationHarnessError("AV1 validation fallback fixture cannot receive a candidate")
        status = "no_recommendation"
        fallback_reason = (
            "compression_intent_requires_directional_search"
            if plan.intent_level != "balanced"
            else "content_profile_uncovered"
        )
        if fallback_reason not in plan.allowed_fallback_reasons:
            raise AV1ValidationHarnessError("AV1 validation fallback reason is not preregistered")
    return AV1ValidationHarnessDecisionV1(
        manifest_id=manifest.manifest_id,
        cell_plan_id=plan.cell_plan_id,
        mode=mode,
        prediction_status=status,
        intent_level=plan.intent_level,
        optimization_objective=AV1_COLD_START_INTENT_OBJECTIVES[plan.intent_level],
        content_traits=plan.trait_selector.traits,
        machine_binding_id=machine_binding.binding_id,
        context=candidate_context,
        fallback_reason=fallback_reason,
        local_evidence_present=False,
    )


def build_av1_validation_harness_trace(
        *,
        decision: AV1ValidationHarnessDecisionV1,
        machine_binding: AV1ValidationHarnessMachineBindingV1,
        target_size_trace: Mapping[str, Any],
) -> AV1ValidationHarnessTraceV1:
    if decision.machine_binding_id != machine_binding.binding_id:
        raise AV1ValidationHarnessError("AV1 validation decision uses another machine binding")
    trace = object_dict(target_size_trace)
    if _required_int(trace.get("schema_version")) != 1:
        raise AV1ValidationHarnessError("AV1 validation requires a target-size-search v1 trace")
    quality_floor = object_dict(trace.get("quality_floor"))
    if (
            str(quality_floor.get("metric") or "").upper() != machine_binding.quality_metric.upper()
            or float_value(quality_floor.get("target")) != machine_binding.quality_target
            or float_value(quality_floor.get("minimum")) != machine_binding.minimum_quality_score
    ):
        raise AV1ValidationHarnessError("AV1 validation search quality floor does not match the machine binding")
    target = object_dict(trace.get("target"))
    if int_value(target.get("target_video_bytes")) != machine_binding.target_video_bytes:
        raise AV1ValidationHarnessError("AV1 validation search target does not match the machine binding")
    transform_plan = object_dict(trace.get("transform_plan"))
    if str(transform_plan.get("transform_plan_id") or "") != machine_binding.transform_plan_id:
        raise AV1ValidationHarnessError("AV1 validation transform plan does not match the machine binding")

    raw_warm_start = trace.get("warm_start")
    if raw_warm_start is not None and not isinstance(raw_warm_start, Mapping):
        raise AV1ValidationHarnessError("AV1 validation warm-start trace must be an object")
    warm_start = object_dict(raw_warm_start)
    ordered_work: list[AV1ValidationHarnessWorkV1] = []
    fallback_invoked = decision.prediction_status == "no_recommendation"
    observed_fallback_reason = decision.fallback_reason
    context = decision.context
    if decision.prediction_status == "recommended":
        if context is None:
            raise AV1ValidationHarnessError("AV1 validation recommendation lacks its context")
        _validate_warm_start_trace(warm_start, context)
        warm_status = str(warm_start.get("status") or "")
        warm_candidate = object_dict(warm_start.get("candidate"))
        ordered_work.append(_locked_probe_work(context, warm_status, warm_candidate))
        fallback_invoked = warm_status != "accepted"
        if fallback_invoked:
            observed_fallback_reason = str(warm_start.get("fallback_reason") or "") or None
        candidates = object_list(trace.get("candidates"))
        if fallback_invoked:
            ordered_work.extend(_full_search_work(candidates, start_ordinal=2))
        else:
            if len(candidates) != 1 or str(object_dict(candidates[0]).get("role") or "") != "quality_memory_hint":
                raise AV1ValidationHarnessError("AV1 validation accepted trace does not contain one locked probe")
    else:
        if warm_start:
            raise AV1ValidationHarnessError("AV1 validation baseline/fallback cannot carry warm-start work")
        ordered_work.extend(_full_search_work(object_list(trace.get("candidates")), start_ordinal=1))

    if fallback_invoked and not any(work.role == "measured_full_search_probe" for work in ordered_work):
        raise AV1ValidationHarnessError("AV1 validation fallback was not observed")
    sanitized_search = {
        "status": str(trace.get("status") or ""),
        "selection_reason": str(trace.get("selection_reason") or ""),
        "quality_floor": {
            "metric": machine_binding.quality_metric.upper(),
            "target": machine_binding.quality_target,
            "minimum": machine_binding.minimum_quality_score,
        },
        "target_video_bytes": machine_binding.target_video_bytes,
        "transform_plan_id": machine_binding.transform_plan_id,
        "ordered_work": [work.to_payload() for work in ordered_work],
    }
    semantic_payload = {
        "schema": AV1_VALIDATION_HARNESS_SCHEMA,
        "schema_version": AV1_VALIDATION_HARNESS_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_HARNESS_CONTRACT_VERSION,
        "trace_origin": "validated_target_size_search_v1",
        "manifest_id": decision.manifest_id,
        "cell_plan_id": decision.cell_plan_id,
        "mode": decision.mode,
        "prediction_status": decision.prediction_status,
        "fallback_reason": observed_fallback_reason,
        "machine_binding_id": machine_binding.binding_id,
        "context_id": context.context_id if context else None,
        "candidate_lock_id": context.candidate_lock_id if context else None,
        "authorization_id": context.authorization_id if context else None,
        "local_evidence_present": False,
        "fallback_invoked": fallback_invoked,
        "search_status": sanitized_search["status"],
        "search_selection_reason": sanitized_search["selection_reason"],
        "observed_search_digest": f"sha256:{stable_json_hash(sanitized_search)}",
        "ordered_work": sanitized_search["ordered_work"],
    }
    trace_id = _harness_id("trace", semantic_payload)
    return AV1ValidationHarnessTraceV1(
        trace_id=trace_id,
        manifest_id=decision.manifest_id,
        cell_plan_id=decision.cell_plan_id,
        mode=decision.mode,
        prediction_status=decision.prediction_status,
        fallback_reason=observed_fallback_reason,
        machine_binding_id=machine_binding.binding_id,
        context_id=context.context_id if context else None,
        candidate_lock_id=context.candidate_lock_id if context else None,
        authorization_id=context.authorization_id if context else None,
        local_evidence_present=False,
        fallback_invoked=fallback_invoked,
        search_status=cast(str, sanitized_search["status"]),
        search_selection_reason=cast(str, sanitized_search["selection_reason"]),
        observed_search_digest=cast(str, semantic_payload["observed_search_digest"]),
        ordered_work=tuple(ordered_work),
        payload_sha256=f"sha256:{stable_json_hash({'trace_id': trace_id, **semantic_payload})}",
    )


def serialize_av1_validation_harness_trace(trace: AV1ValidationHarnessTraceV1) -> bytes:
    return canonical_json_bytes(trace.to_payload())


def run_av1_validation_target_size_search(
        *args: Any,
        decision: AV1ValidationHarnessDecisionV1,
        **kwargs: Any,
) -> QualitySearchResult:
    if "warm_start" in kwargs or "expected_search_signature_id" in kwargs:
        raise AV1ValidationHarnessError("AV1 validation runner owns warm-start inputs")
    hint = decision.search_hint()
    return search_target_size(
        *args,
        warm_start=hint,
        expected_search_signature_id=hint.search_signature_id if hint else None,
        _allow_validation_warm_start=True,
        **kwargs,
    )


def load_av1_validation_harness_trace(path: Path) -> AV1ValidationHarnessTraceV1:
    raw_bytes = path.read_bytes()
    try:
        payload = object_dict(json.loads(raw_bytes.decode("utf-8")))
        trace = av1_validation_harness_trace_from_payload(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationHarnessError("AV1 validation harness trace is invalid") from exc
    if raw_bytes != serialize_av1_validation_harness_trace(trace):
        raise AV1ValidationHarnessError("AV1 validation harness trace bytes are not canonical")
    return trace


def av1_validation_harness_trace_from_payload(
        payload: Mapping[str, Any],
) -> AV1ValidationHarnessTraceV1:
    value = object_dict(payload)
    expected_keys = {
        "trace_id",
        "schema",
        "schema_version",
        "contract_version",
        "trace_origin",
        "manifest_id",
        "cell_plan_id",
        "mode",
        "prediction_status",
        "fallback_reason",
        "machine_binding_id",
        "context_id",
        "candidate_lock_id",
        "authorization_id",
        "local_evidence_present",
        "fallback_invoked",
        "search_status",
        "search_selection_reason",
        "observed_search_digest",
        "ordered_work",
        "payload_sha256",
    }
    if set(value) != expected_keys:
        raise AV1ValidationHarnessError("AV1 validation harness trace fields are unsupported")
    if (
            value.get("schema") != AV1_VALIDATION_HARNESS_SCHEMA
            or _required_int(value.get("schema_version")) != AV1_VALIDATION_HARNESS_SCHEMA_VERSION
            or value.get("contract_version") != AV1_VALIDATION_HARNESS_CONTRACT_VERSION
            or value.get("trace_origin") != "validated_target_size_search_v1"
    ):
        raise AV1ValidationHarnessError("AV1 validation harness trace contract is unsupported")
    work = tuple(
        AV1ValidationHarnessWorkV1(
            ordinal=_required_int(item.get("ordinal")),
            role=str(item.get("role") or ""),
            crf=_required_number(item.get("crf")),
            status=str(item.get("status") or ""),
            quality_floor_met=_optional_bool(item.get("quality_floor_met")),
            target_band_met=_optional_bool(item.get("target_band_met")),
        )
        for item in (object_dict(entry) for entry in object_list(value.get("ordered_work")))
    )
    return AV1ValidationHarnessTraceV1(
        trace_id=str(value.get("trace_id") or ""),
        manifest_id=str(value.get("manifest_id") or ""),
        cell_plan_id=str(value.get("cell_plan_id") or ""),
        mode=cast(AV1ValidationHarnessMode, str(value.get("mode") or "")),
        prediction_status=cast(
            AV1ValidationHarnessPredictionStatus,
            str(value.get("prediction_status") or ""),
        ),
        fallback_reason=_optional_text(value.get("fallback_reason")),
        machine_binding_id=str(value.get("machine_binding_id") or ""),
        context_id=_optional_text(value.get("context_id")),
        candidate_lock_id=_optional_text(value.get("candidate_lock_id")),
        authorization_id=_optional_text(value.get("authorization_id")),
        local_evidence_present=_required_bool(value.get("local_evidence_present")),
        fallback_invoked=_required_bool(value.get("fallback_invoked")),
        search_status=str(value.get("search_status") or ""),
        search_selection_reason=str(value.get("search_selection_reason") or ""),
        observed_search_digest=str(value.get("observed_search_digest") or ""),
        ordered_work=work,
        payload_sha256=str(value.get("payload_sha256") or ""),
    )


def _validate_warm_start_trace(
        warm_start: Mapping[str, Any],
        context: AV1ValidationCandidateContextV1,
) -> None:
    if not warm_start:
        raise AV1ValidationHarnessError("AV1 validation guided trace is missing its locked probe")
    requested_crf = _required_number(warm_start.get("requested_crf"))
    candidate_crf = _required_int(warm_start.get("candidate_crf"))
    candidate_count = _required_int(warm_start.get("candidate_count"))
    if (
            _required_int(warm_start.get("schema_version")) != 1
            or str(warm_start.get("source") or "") != AV1_VALIDATION_HARNESS_SOURCE
            or str(warm_start.get("provenance_id") or "") != context.context_id
            or str(warm_start.get("search_signature_id") or "") != context.search_signature_id
            or requested_crf != context.requested_crf
            or candidate_crf != context.candidate_crf
            or not isinstance(warm_start.get("attempted"), bool)
            or candidate_count not in {0, 1}
            or str(warm_start.get("status") or "") not in {
                "accepted",
                "rejected_fallback",
                "probe_error_fallback",
                "guard_rejected",
            }
    ):
        raise AV1ValidationHarnessError("AV1 validation locked-probe trace is inconsistent")
    warm_status = str(warm_start.get("status") or "")
    attempted = _required_bool(warm_start.get("attempted"))
    if attempted != (warm_status != "guard_rejected"):
        raise AV1ValidationHarnessError("AV1 validation locked-probe attempt flag is inconsistent")
    if str(warm_start.get("cohort_id") or "") != f"av1vhc1_{stable_json_hash(context.context_id)[:24]}":
        raise AV1ValidationHarnessError("AV1 validation locked-probe cohort is inconsistent")
    if object_list(warm_start.get("review_risks")):
        raise AV1ValidationHarnessError("AV1 validation locked probe cannot carry private review risks")
    fallback_used = _required_bool(warm_start.get("fallback_used"))
    if (warm_status == "accepted" and fallback_used) or (warm_status != "accepted" and not fallback_used):
        raise AV1ValidationHarnessError("AV1 validation locked-probe fallback flag is inconsistent")
    if _required_int(warm_start.get("total_candidate_count")) != candidate_count + _required_int(
            warm_start.get("baseline_candidate_count")
    ):
        raise AV1ValidationHarnessError("AV1 validation candidate counts are inconsistent")


def _locked_probe_work(
        context: AV1ValidationCandidateContextV1,
        warm_status: str,
        candidate: Mapping[str, Any],
) -> AV1ValidationHarnessWorkV1:
    if candidate:
        if (
                float_value(candidate.get("crf")) != context.candidate_crf
                or str(candidate.get("role") or "") != "quality_memory_hint"
        ):
            raise AV1ValidationHarnessError("AV1 validation locked candidate payload is inconsistent")
        return AV1ValidationHarnessWorkV1(
            ordinal=1,
            role="locked_first_probe",
            crf=float_value(candidate.get("crf")),
            status="measured",
            quality_floor_met=_required_bool(candidate.get("quality_floor_met")),
            target_band_met=_required_bool(candidate.get("within_sample_band")),
        )
    if warm_status not in {"probe_error_fallback", "guard_rejected"}:
        raise AV1ValidationHarnessError("AV1 validation locked probe lacks measured candidate data")
    return AV1ValidationHarnessWorkV1(
        ordinal=1,
        role="locked_first_probe",
        crf=float(context.candidate_crf),
        status="guard_rejected" if warm_status == "guard_rejected" else "probe_error",
        quality_floor_met=None,
        target_band_met=None,
    )


def _full_search_work(
        candidates: Sequence[Any],
        *,
        start_ordinal: int,
) -> list[AV1ValidationHarnessWorkV1]:
    candidate_payloads = [object_dict(candidate) for candidate in candidates]
    if not candidate_payloads:
        return []
    if any(str(candidate.get("role") or "") == "quality_memory_hint" for candidate in candidate_payloads):
        raise AV1ValidationHarnessError("AV1 validation fallback contains another guided probe")
    return [
        AV1ValidationHarnessWorkV1(
            ordinal=start_ordinal + index,
            role="measured_full_search_probe",
            crf=float_value(candidate.get("crf")),
            status="measured",
            quality_floor_met=_required_bool(candidate.get("quality_floor_met")),
            target_band_met=_required_bool(candidate.get("within_sample_band")),
        )
        for index, candidate in enumerate(sorted(candidate_payloads, key=lambda item: int_value(item.get("attempt"))))
    ]


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AV1ValidationHarnessError("AV1 validation timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationHarnessError("AV1 validation timestamp must include UTC")
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise AV1ValidationHarnessError("AV1 validation optional flag must be boolean")
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AV1ValidationHarnessError("AV1 validation flag must be boolean")
    return value


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AV1ValidationHarnessError("AV1 validation integer field is invalid")
    return value


def _required_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AV1ValidationHarnessError("AV1 validation numeric field is invalid")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise AV1ValidationHarnessError("AV1 validation numeric field must be finite")
    return normalized


def _assert_public_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationHarnessError("AV1 validation public payload is unsafe") from exc


def _harness_id(kind: str, payload: Mapping[str, Any]) -> str:
    prefixes = {"machine": "av1vhm1", "context": "av1vhc1", "trace": "av1vht1"}
    return f"{prefixes[kind]}_{stable_json_hash(payload)[:32]}"
