from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
import re
from typing import Any, Literal, Mapping, Sequence, cast

from mediaforce.core.db import DBClient
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict
from mediaforce.encoding.quality import QualitySearchWarmStart
from mediaforce.tuning.compression_intent import (
    COMPRESSION_INTENT_LEVELS,
    CompressionIntentLevel,
    compression_intent_from_item,
)
from mediaforce.tuning.content_intent_observations import (
    BOUNDARY_ASSESSMENT_CONTRACT,
    ContentIntentBoundaryCohort,
    ContentIntentBoundaryCompatibilityV1,
    ContentIntentPersonalizationState,
    ContentIntentReplayContext,
    content_intent_boundary_compatibility_from_payload,
    content_intent_replay_context,
    content_intent_replay_scope_rows,
    load_current_content_intent_boundary_observations,
    replay_content_intent_personalization,
)


AV1_COLD_START_PREDICTOR_VERSION = "acsp1"
AV1_COLD_START_LOCAL_MAX_AGE_DAYS = 180
AV1_COLD_START_MAX_LOCAL_CRF_MAD = 2.0
AV1_COLD_START_MAX_LOCAL_CRF_RANGE = 6.0
AV1_COLD_START_PROBE_SUPPORTED_INTENTS = frozenset({"balanced"})
AV1_COLD_START_REQUEST_FALLBACK_CODES = frozenset({
    "compression_intent_unconfirmed",
    "content_evidence_missing",
    "target_video_bitrate_missing",
})
AV1ColdStartConfidence = Literal["none", "limited", "moderate", "high"]
AV1ColdStartPredictionStatus = Literal["recommended", "no_recommendation"]
AV1ColdStartPredictionSource = Literal["none", "local"]
AV1ColdStartTrait = Literal[
    "animation",
    "darkness",
    "motion",
    "grain_noise",
    "texture_detail",
    "low_motion_dialogue",
    "mixed",
    "typical",
    "unknown",
]

AV1_COLD_START_INTENT_OBJECTIVES: dict[str, str] = {
    "reference": "maximize_measured_fidelity_within_authoritative_size_limit",
    "transparent": "minimize_size_subject_to_source_indistinguishability",
    "balanced": "minimize_target_distance_subject_to_measured_quality_floor",
    "perceptual_floor": "minimize_size_subject_to_explicit_visual_acceptability",
}
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")


class AV1ColdStartContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ColdStartRuntimeCompatibilityV1:
    encoder: str
    encoder_version: tuple[int, int, int]
    encoder_runtime_major: int
    quality_tool: str
    quality_tool_major: int
    measurement_basis: str
    assessment_contract: str
    preset: int
    pixel_format: str
    bit_depth: int
    output_width: int
    output_height: int
    frame_rate: str
    cadence_transform: str
    video_filter: str | None
    encoder_parameters: tuple[str, ...]
    grain_strength: int
    grain_denoise: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    output_container: str
    target_video_bitrate_bps: int

    def __post_init__(self) -> None:
        if self.quality_tool != "ab-av1":
            raise AV1ColdStartContractError("AV1 cold-start quality tool is unsupported")
        if self.measurement_basis != "sample_projection":
            raise AV1ColdStartContractError("AV1 cold-start measurement basis is unsupported")
        if self.assessment_contract != BOUNDARY_ASSESSMENT_CONTRACT:
            raise AV1ColdStartContractError("AV1 cold-start assessment contract is unsupported")

    @property
    def compatibility_id(self) -> str:
        return f"acsrc1_{stable_json_hash(self.to_payload())[:32]}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "encoder": self.encoder,
            "encoder_version": _format_version(self.encoder_version),
            "encoder_runtime_major": self.encoder_runtime_major,
            "quality_tool": self.quality_tool,
            "quality_tool_major": self.quality_tool_major,
            "measurement_basis": self.measurement_basis,
            "assessment_contract": self.assessment_contract,
            "preset": self.preset,
            "pixel_format": self.pixel_format,
            "bit_depth": self.bit_depth,
            "output_width": self.output_width,
            "output_height": self.output_height,
            "frame_rate": self.frame_rate,
            "cadence_transform": self.cadence_transform,
            "video_filter": self.video_filter,
            "encoder_parameters": list(self.encoder_parameters),
            "grain_strength": self.grain_strength,
            "grain_denoise": self.grain_denoise,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "output_container": self.output_container,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartRequestV1:
    intent_level: CompressionIntentLevel
    intent_semantic_id: str
    content_traits: tuple[AV1ColdStartTrait, ...]
    runtime_compatibility: AV1ColdStartRuntimeCompatibilityV1
    configured_min_crf: int
    configured_max_crf: int
    as_of: str

    def __post_init__(self) -> None:
        if self.intent_level not in COMPRESSION_INTENT_LEVELS:
            raise AV1ColdStartContractError("AV1 cold-start requests require confirmed intent")
        if not self.intent_semantic_id.strip():
            raise AV1ColdStartContractError("AV1 cold-start requests require intent identity")
        if not self.content_traits or tuple(sorted(set(self.content_traits))) != self.content_traits:
            raise AV1ColdStartContractError("AV1 cold-start request traits must be unique and sorted")
        if not 0 <= self.configured_min_crf <= self.configured_max_crf <= 63:
            raise AV1ColdStartContractError("AV1 cold-start request CRF bounds are invalid")
        _parse_timestamp(self.as_of, "AV1 cold-start request timestamp")

    @property
    def search_signature_id(self) -> str:
        payload = {
            "predictor_version": AV1_COLD_START_PREDICTOR_VERSION,
            "intent_semantic_id": self.intent_semantic_id,
            "content_traits": list(self.content_traits),
            "runtime_compatibility_id": self.runtime_compatibility.compatibility_id,
            "configured_min_crf": self.configured_min_crf,
            "configured_max_crf": self.configured_max_crf,
        }
        return f"acss1_{stable_json_hash(payload)[:32]}"


@dataclass(frozen=True, slots=True)
class AV1ColdStartPrediction:
    status: AV1ColdStartPredictionStatus
    source: AV1ColdStartPredictionSource
    fallback_reason: str | None
    intent_level: str | None
    optimization_objective: str | None
    content_traits: tuple[str, ...]
    requested_crf: float | None
    candidate_crf: int | None
    crf_lower: float | None
    crf_upper: float | None
    confidence: AV1ColdStartConfidence
    confidence_score: float
    provenance_ids: tuple[str, ...]
    review_risks: tuple[str, ...]
    search_signature_id: str | None
    local_scope: str | None
    local_evidence_present: bool = False

    @property
    def recommended(self) -> bool:
        return self.status == "recommended" and self.candidate_crf is not None

    def search_hint(self) -> QualitySearchWarmStart | None:
        if not self.recommended or self.requested_crf is None or self.search_signature_id is None:
            return None
        provenance_id = self.provenance_ids[0] if self.provenance_ids else None
        cohort_id = f"acsh1_{stable_json_hash(self.provenance_ids or (self.source,))[:24]}"
        return QualitySearchWarmStart(
            requested_crf=self.requested_crf,
            candidate_crf=cast(int, self.candidate_crf),
            search_signature_id=self.search_signature_id,
            cohort_id=cohort_id,
            source=f"av1_cold_start_{self.source}",
            confidence=self.confidence,
            provenance_id=provenance_id,
            review_risks=self.review_risks,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "predictor_version": AV1_COLD_START_PREDICTOR_VERSION,
            "status": self.status,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
            "intent_level": self.intent_level,
            "optimization_objective": self.optimization_objective,
            "content_traits": list(self.content_traits),
            "requested_crf": self.requested_crf,
            "candidate_crf": self.candidate_crf,
            "crf_range": (
                {"lower": self.crf_lower, "upper": self.crf_upper}
                if self.crf_lower is not None and self.crf_upper is not None
                else None
            ),
            "confidence": {
                "level": self.confidence,
                "score": self.confidence_score,
            },
            "provenance_ids": list(self.provenance_ids),
            "review_risks": list(self.review_risks),
            "local_scope": self.local_scope,
            "fallback": {
                "full_measured_calibration": not self.recommended,
                "quality_floor_unchanged": True,
                "target_size_authority_unchanged": True,
            },
        }


def build_av1_cold_start_runtime_compatibility(
        compatibility: ContentIntentBoundaryCompatibilityV1,
        *,
        target_video_bitrate_bps: int,
) -> AV1ColdStartRuntimeCompatibilityV1:
    parameters = tuple(sorted(compatibility.encoder_parameters))
    grain_strength, grain_denoise = _grain_settings(parameters)
    encoder_version = _runtime_version(
        compatibility.encoder_version,
        "encoder version",
    )
    encoder_runtime_version = _runtime_version(
        compatibility.encoder_runtime_version,
        "encoder runtime version",
    )
    quality_tool_version = _runtime_version(
        compatibility.quality_tool_version,
        "quality tool version",
    )
    return AV1ColdStartRuntimeCompatibilityV1(
        encoder=_normalized_encoder(compatibility.encoder),
        encoder_version=encoder_version,
        encoder_runtime_major=encoder_runtime_version[0],
        quality_tool=compatibility.quality_tool.casefold(),
        quality_tool_major=quality_tool_version[0],
        measurement_basis=compatibility.measurement_basis,
        assessment_contract=compatibility.assessment_contract,
        preset=compatibility.preset,
        pixel_format=compatibility.pixel_format.casefold(),
        bit_depth=_pixel_format_bit_depth(compatibility.pixel_format),
        output_width=compatibility.output_width,
        output_height=compatibility.output_height,
        frame_rate=compatibility.frame_rate,
        cadence_transform=compatibility.cadence_transform.casefold(),
        video_filter=compatibility.video_filter,
        encoder_parameters=parameters,
        grain_strength=grain_strength,
        grain_denoise=grain_denoise,
        quality_metric=compatibility.quality_metric.upper(),
        quality_target=round(compatibility.quality_target, 3),
        minimum_quality_score=round(compatibility.minimum_quality_score, 3),
        output_container=compatibility.output_container.casefold(),
        target_video_bitrate_bps=_positive_int(target_video_bitrate_bps, "target video bitrate"),
    )


def build_av1_cold_start_request(
        *,
        sample_item: Mapping[str, Any],
        compatibility_payload: Mapping[str, Any],
        configured_min_crf: int,
        configured_max_crf: int,
        as_of: str,
) -> AV1ColdStartRequestV1:
    item = object_dict(sample_item)
    intent = compression_intent_from_item(item)
    if intent.requires_confirmation:
        raise AV1ColdStartContractError("compression_intent_unconfirmed")
    decision = object_dict(item.get("media_fingerprint_decision"))
    if str(decision.get("status") or "") != "measured":
        raise AV1ColdStartContractError("content_evidence_missing")
    replay_context = content_intent_replay_context(item)
    compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
    stream_budget = object_dict(item.get("stream_budget_ledger"))
    target_video_bitrate_bps = int_value(
        object_dict(stream_budget.get("totals")).get("remaining_video_bitrate_bps")
    )
    if target_video_bitrate_bps <= 0:
        raise AV1ColdStartContractError("target_video_bitrate_missing")
    return AV1ColdStartRequestV1(
        intent_level=intent.level,
        intent_semantic_id=intent.semantic_id,
        content_traits=av1_cold_start_traits(replay_context.content_traits),
        runtime_compatibility=build_av1_cold_start_runtime_compatibility(
            compatibility,
            target_video_bitrate_bps=target_video_bitrate_bps,
        ),
        configured_min_crf=configured_min_crf,
        configured_max_crf=configured_max_crf,
        as_of=as_of,
    )


def av1_cold_start_traits(measured_traits: Sequence[str]) -> tuple[AV1ColdStartTrait, ...]:
    normalized = {str(value).strip().casefold() for value in measured_traits if str(value).strip()}
    traits: set[AV1ColdStartTrait] = set()
    if "animation_cues" in normalized:
        traits.add("animation")
    if normalized.intersection({"dark_luma", "dark_gradient_banding_risk"}):
        traits.add("darkness")
    if "high_motion" in normalized:
        traits.add("motion")
    if normalized.intersection({
        "likely_film_grain",
        "likely_analog_noise",
        "compression_noise_advisory",
        "uncertain_noise_mix",
    }):
        traits.add("grain_noise")
    if "high_texture" in normalized:
        traits.add("texture_detail")
    if "low_motion_dialogue" in normalized:
        traits.add("low_motion_dialogue")
    if "typical" in normalized:
        traits.add("typical")
    core_traits = traits.difference({"typical"})
    if len(core_traits) > 1:
        traits.add("mixed")
    if not traits or "unknown" in normalized:
        traits.add("unknown")
    return tuple(sorted(traits))


def predict_local_av1_cold_start(
        all_state: ContentIntentPersonalizationState | None,
        fresh_state: ContentIntentPersonalizationState | None,
        request: AV1ColdStartRequestV1,
        *,
        freshness_fallback_reason: str | None = None,
) -> AV1ColdStartPrediction:
    if all_state is None:
        return _no_recommendation(request, "no_local_evidence")
    all_cohort = _first_evidence_cohort(all_state)
    if all_cohort is None:
        return _no_recommendation(request, "no_local_evidence")
    local_provenance = _local_provenance_id(all_cohort)
    if freshness_fallback_reason is not None:
        return _no_recommendation(
            request,
            freshness_fallback_reason,
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if all_cohort.boundary_status == "conflicting":
        return _no_recommendation(
            request,
            "local_evidence_conflicting",
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    fresh_cohort = _cohort_for_scope(fresh_state, all_cohort.scope)
    if fresh_cohort is None or fresh_cohort.observation_count == 0:
        return _no_recommendation(
            request,
            "local_evidence_stale",
            local_scope=all_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    local_provenance = _local_provenance_id(fresh_cohort)
    if fresh_cohort.boundary_status == "conflicting":
        return _no_recommendation(
            request,
            "local_evidence_conflicting",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    effective_confidence: AV1ColdStartConfidence = fresh_cohort.confidence
    if fresh_cohort.scope == "item" and fresh_cohort.actionable:
        effective_confidence = "moderate"
    elif fresh_cohort.confidence not in {"moderate", "high"}:
        return _no_recommendation(
            request,
            "local_evidence_low_confidence",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.median_acceptable_crf is None
            or fresh_cohort.minimum_acceptable_crf is None
            or fresh_cohort.maximum_acceptable_crf is None
    ):
        return _no_recommendation(
            request,
            "local_evidence_missing_crf_distribution",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.minimum_acceptable_bitrate_bps is None
            or fresh_cohort.maximum_acceptable_bitrate_bps is None
            or not fresh_cohort.minimum_acceptable_bitrate_bps
            <= request.runtime_compatibility.target_video_bitrate_bps
            <= fresh_cohort.maximum_acceptable_bitrate_bps
    ):
        return _no_recommendation(
            request,
            "local_evidence_target_incompatible",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    if (
            fresh_cohort.acceptable_crf_mad is None
            or fresh_cohort.acceptable_crf_mad > AV1_COLD_START_MAX_LOCAL_CRF_MAD
            or fresh_cohort.maximum_acceptable_crf - fresh_cohort.minimum_acceptable_crf
            > AV1_COLD_START_MAX_LOCAL_CRF_RANGE
    ):
        return _no_recommendation(
            request,
            "local_evidence_crf_dispersion",
            local_scope=fresh_cohort.scope,
            local_evidence_present=True,
            provenance_ids=(local_provenance,),
        )
    return _recommended_prediction(
        request,
        crf_lower=fresh_cohort.minimum_acceptable_crf,
        crf_center=fresh_cohort.median_acceptable_crf,
        crf_upper=fresh_cohort.maximum_acceptable_crf,
        confidence=effective_confidence,
        confidence_score=_local_confidence_score(fresh_cohort),
        provenance_ids=(local_provenance,),
        review_risks=_trait_review_risks(request.content_traits),
        local_scope=fresh_cohort.scope,
        local_evidence_present=True,
    )


def plan_av1_cold_start(
        connection: DBClient,
        *,
        sample_item: Mapping[str, Any],
        prefix: str,
        compatibility_payload: Mapping[str, Any],
        configured_min_crf: int,
        configured_max_crf: int,
        as_of: str,
) -> AV1ColdStartPrediction:
    try:
        request = build_av1_cold_start_request(
            sample_item=sample_item,
            compatibility_payload=compatibility_payload,
            configured_min_crf=configured_min_crf,
            configured_max_crf=configured_max_crf,
            as_of=as_of,
        )
    except ValueError as exc:
        return _no_recommendation(None, _request_fallback_reason(exc))
    try:
        local_compatibility = content_intent_boundary_compatibility_from_payload(compatibility_payload)
        replay_context = content_intent_replay_context(sample_item)
        loaded_rows = load_current_content_intent_boundary_observations(
            connection,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
        rows = _rows_for_replay_context(loaded_rows, replay_context)
        all_state = _replay_local_state(
            rows,
            replay_context=replay_context,
            prefix=prefix,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
    except ValueError:
        return _no_recommendation(request, "cold_start_request_invalid")
    all_cohort = _first_evidence_cohort(all_state) if all_state is not None else None
    request_time = _parse_timestamp(as_of, "request timestamp")
    cutoff = request_time - timedelta(days=AV1_COLD_START_LOCAL_MAX_AGE_DAYS)
    fresh_rows = [row for row in rows if _row_at_or_after(row, cutoff, request_time)]
    try:
        selected_scope_rows = (
            content_intent_replay_scope_rows(
                rows,
                source_id=replay_context.source_id,
                content_id=replay_context.content_id,
                prefix=prefix,
                content_profile_id=replay_context.content_profile_id,
                intent_semantic_id=request.intent_semantic_id,
                compatibility_key=local_compatibility.compatibility_key,
                scope=all_cohort.scope,
            )
            if all_cohort is not None
            else ()
        )
    except ValueError:
        return _no_recommendation(request, "cold_start_request_invalid")
    selected_timestamp_statuses = {
        _row_timestamp_status(row, cutoff, request_time)
        for row in selected_scope_rows
    }
    if "unversioned" in selected_timestamp_statuses:
        freshness_fallback_reason = "local_evidence_unversioned"
    elif "future" in selected_timestamp_statuses:
        freshness_fallback_reason = "local_evidence_future_dated"
    else:
        freshness_fallback_reason = None
    try:
        fresh_state = _replay_local_state(
            fresh_rows,
            replay_context=replay_context,
            prefix=prefix,
            intent_semantic_id=request.intent_semantic_id,
            compatibility_key=local_compatibility.compatibility_key,
        )
    except ValueError:
        return _no_recommendation(request, "cold_start_request_invalid")
    return predict_local_av1_cold_start(
        all_state,
        fresh_state,
        request,
        freshness_fallback_reason=freshness_fallback_reason,
    )


def unavailable_av1_cold_start_prediction(fallback_reason: str) -> AV1ColdStartPrediction:
    return _no_recommendation(None, fallback_reason)


def _replay_local_state(
        rows: Sequence[Mapping[str, Any]],
        *,
        replay_context: Any,
        prefix: str,
        intent_semantic_id: str,
        compatibility_key: str,
) -> ContentIntentPersonalizationState | None:
    if not rows:
        return None
    return replay_content_intent_personalization(
        rows,
        source_id=replay_context.source_id,
        content_id=replay_context.content_id,
        prefix=prefix,
        content_profile_id=replay_context.content_profile_id,
        intent_semantic_id=intent_semantic_id,
        compatibility_key=compatibility_key,
    )


def _rows_for_replay_context(
        rows: Sequence[Mapping[str, Any]],
        replay_context: ContentIntentReplayContext,
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if (
            str(row.get("source_id") or "") == replay_context.source_id
            and str(row.get("content_id") or "") == replay_context.content_id
        )
        or str(row.get("content_profile_id") or "") == replay_context.content_profile_id
    ]


def _recommended_prediction(
        request: AV1ColdStartRequestV1,
        *,
        crf_lower: float,
        crf_center: float,
        crf_upper: float,
        confidence: AV1ColdStartConfidence,
        confidence_score: float,
        provenance_ids: tuple[str, ...],
        review_risks: tuple[str, ...],
        local_scope: str | None = None,
        local_evidence_present: bool = False,
) -> AV1ColdStartPrediction:
    if request.intent_level not in AV1_COLD_START_PROBE_SUPPORTED_INTENTS:
        return _no_recommendation(
            request,
            "compression_intent_requires_directional_search",
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            provenance_ids=provenance_ids,
        )
    if (
            crf_lower < request.configured_min_crf
            or crf_upper > request.configured_max_crf
            or crf_lower > crf_center
            or crf_center > crf_upper
    ):
        return _no_recommendation(
            request,
            "recommended_range_outside_configured_bounds",
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            provenance_ids=provenance_ids,
        )
    minimum_candidate_crf = math.ceil(crf_lower)
    maximum_candidate_crf = math.floor(crf_upper)
    if minimum_candidate_crf > maximum_candidate_crf:
        return _no_recommendation(
            request,
            "recommended_range_has_no_integer_probe",
            local_scope=local_scope,
            local_evidence_present=local_evidence_present,
            provenance_ids=provenance_ids,
        )
    candidate_crf = min(
        max(_round_crf(crf_center), minimum_candidate_crf),
        maximum_candidate_crf,
    )
    return AV1ColdStartPrediction(
        status="recommended",
        source="local",
        fallback_reason=None,
        intent_level=request.intent_level,
        optimization_objective=AV1_COLD_START_INTENT_OBJECTIVES[request.intent_level],
        content_traits=tuple(request.content_traits),
        requested_crf=round(crf_center, 3),
        candidate_crf=candidate_crf,
        crf_lower=round(crf_lower, 3),
        crf_upper=round(crf_upper, 3),
        confidence=confidence,
        confidence_score=round(confidence_score, 3),
        provenance_ids=tuple(sorted(set(provenance_ids))),
        review_risks=tuple(sorted(set(review_risks))),
        search_signature_id=request.search_signature_id,
        local_scope=local_scope,
        local_evidence_present=local_evidence_present,
    )


def _no_recommendation(
        request: AV1ColdStartRequestV1 | None,
        fallback_reason: str,
        *,
        local_scope: str | None = None,
        local_evidence_present: bool = False,
        provenance_ids: tuple[str, ...] = (),
) -> AV1ColdStartPrediction:
    intent_level = request.intent_level if request is not None else None
    return AV1ColdStartPrediction(
        status="no_recommendation",
        source="none",
        fallback_reason=fallback_reason,
        intent_level=intent_level,
        optimization_objective=(
            AV1_COLD_START_INTENT_OBJECTIVES[intent_level]
            if intent_level in AV1_COLD_START_INTENT_OBJECTIVES
            else None
        ),
        content_traits=tuple(request.content_traits) if request is not None else (),
        requested_crf=None,
        candidate_crf=None,
        crf_lower=None,
        crf_upper=None,
        confidence="none",
        confidence_score=0.0,
        provenance_ids=tuple(sorted(set(provenance_ids))),
        review_risks=_trait_review_risks(request.content_traits) if request is not None else (),
        search_signature_id=request.search_signature_id if request is not None else None,
        local_scope=local_scope,
        local_evidence_present=local_evidence_present,
    )


def _first_evidence_cohort(state: ContentIntentPersonalizationState) -> ContentIntentBoundaryCohort | None:
    return next((cohort for cohort in state.cohorts if cohort.observation_count > 0), None)


def _cohort_for_scope(
        state: ContentIntentPersonalizationState | None,
        scope: str,
) -> ContentIntentBoundaryCohort | None:
    if state is None:
        return None
    return next((cohort for cohort in state.cohorts if cohort.scope == scope), None)


def _local_confidence_score(cohort: ContentIntentBoundaryCohort) -> float:
    if cohort.scope == "item" and cohort.actionable:
        return 0.8
    score = {"none": 0.0, "limited": 0.45, "moderate": 0.75, "high": 0.9}[cohort.confidence]
    return min(0.99, score + min(cohort.source_count, 10) * 0.005)


def _local_provenance_id(cohort: ContentIntentBoundaryCohort) -> str:
    return f"acsl1_{stable_json_hash((cohort.scope, cohort.cohort_id, cohort.evidence_snapshot_id))[:24]}"


def _trait_review_risks(traits: Sequence[str]) -> tuple[str, ...]:
    mapping = {
        "animation": "flat_regions_and_edges",
        "darkness": "banding_dark_scene_damage",
        "motion": "motion_breakup",
        "grain_noise": "grain_noise_treatment",
        "texture_detail": "softness_detail_loss",
        "low_motion_dialogue": "dialogue_facial_detail",
        "mixed": "mixed_or_unknown_content",
        "unknown": "mixed_or_unknown_content",
    }
    return tuple(sorted({mapping[trait] for trait in traits if trait in mapping}))


def _row_at_or_after(
        row: Mapping[str, Any],
        cutoff: datetime,
        as_of: datetime | None = None,
) -> bool:
    return _row_timestamp_status(row, cutoff, as_of) == "fresh"


def _row_timestamp_status(
        row: Mapping[str, Any],
        cutoff: datetime,
        as_of: datetime | None = None,
) -> str:
    try:
        recorded_at = str(row.get("recorded_at") or "").strip()
        provenance = object_dict(json.loads(str(row.get("provenance_json") or "{}")))
        if str(provenance.get("recorded_at") or "").strip() != recorded_at:
            return "unversioned"
        timestamp = _parse_timestamp(recorded_at, "local observation timestamp")
        if as_of is not None and timestamp > as_of:
            return "future"
        return "fresh" if timestamp >= cutoff else "stale"
    except (AV1ColdStartContractError, TypeError, ValueError, json.JSONDecodeError):
        return "unversioned"


def _grain_settings(parameters: Sequence[str]) -> tuple[int, int]:
    values: dict[str, int] = {}
    for parameter in parameters:
        key, separator, raw_value = str(parameter).partition("=")
        if separator and key in {"film-grain", "film-grain-denoise"}:
            try:
                values[key] = int(raw_value)
            except ValueError as exc:
                raise AV1ColdStartContractError("AV1 cold-start grain settings are not numeric") from exc
    grain_strength = values.get("film-grain", 0)
    grain_denoise = values.get("film-grain-denoise", 0)
    if not 0 <= grain_strength <= 50 or grain_denoise not in {0, 1}:
        raise AV1ColdStartContractError("AV1 cold-start grain settings are invalid")
    return grain_strength, grain_denoise


def _pixel_format_bit_depth(pixel_format: str) -> int:
    normalized = pixel_format.strip().casefold()
    match = re.search(r"p(10|12)(?:le|be)?$", normalized)
    if match is not None:
        return int(match.group(1))
    if normalized.endswith("p") or normalized.endswith("p8"):
        return 8
    raise AV1ColdStartContractError("AV1 cold-start pixel format has unsupported bit depth")


def _normalized_encoder(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized in {"libsvtav1", "svt-av1", "svt_av1"}:
        return "svt-av1"
    raise AV1ColdStartContractError("AV1 cold-start supports only SVT-AV1")


def _parse_version(value: object, label: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search(str(value or ""))
    if match is None:
        raise AV1ColdStartContractError(f"AV1 cold-start {label} is not a semantic version")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def _runtime_version(value: object, label: str) -> tuple[int, int, int]:
    try:
        return _parse_version(value, label)
    except AV1ColdStartContractError:
        return (0, 0, 0)


def _format_version(value: tuple[int, int, int]) -> str:
    return ".".join(str(component) for component in value)


def _parse_timestamp(value: object, label: str) -> datetime:
    normalized = str(value or "").strip()
    if not normalized:
        raise AV1ColdStartContractError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ColdStartContractError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _request_fallback_reason(exc: ValueError) -> str:
    message = str(exc).strip()
    if message in AV1_COLD_START_REQUEST_FALLBACK_CODES:
        return message
    return "cold_start_request_invalid"


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AV1ColdStartContractError(f"AV1 cold-start {label} must be a positive integer")
    return value


def _round_crf(value: float) -> int:
    return int(math.floor(value + 0.5))
