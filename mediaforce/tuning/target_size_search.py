from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Literal

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.quality import (
    QualitySearchResult,
    QualitySearchWarmStart,
    QualityTempCleanupError,
    QualityTempSetupError,
    SampleEncodeError,
    SampleEncodeResult,
)
from mediaforce.tuning.compression_intent import (
    CompressionAuthorizationDecision,
    CompressionEvidenceRef,
    CompressionIntentV1,
    compression_candidate_rank,
    compression_intent_from_policy,
    legacy_compression_intent,
)
from mediaforce.tuning.stream_budget import (
    StreamBudgetLedger,
    StreamBudgetProjectionBlocker,
    stream_budget_projection_blocker,
)


TARGET_SIZE_SEARCH_SCHEMA_VERSION = 1
TARGET_SIZE_TRANSFORM_PLAN_SCHEMA_VERSION = 1
MAX_TARGET_SIZE_CANDIDATES = 6
TARGET_SIZE_BOUND_EXPANSION_STEP = 8
MAX_FINAL_OUTPUT_RETRIES = 1
MIN_FINAL_RETRY_CALIBRATION_FACTOR = 0.5
MAX_FINAL_RETRY_CALIBRATION_FACTOR = 2.0
CRF_PER_BITRATE_HALVING = 6.0
MAX_DIRECTIONAL_PROBE_CRF_STEP = 6.0
FINAL_RETRY_SKIP_REASONS = frozenset({
    "final_retry_skipped_all_candidates_over_source_cap",
    "final_retry_skipped_calibration_factor_out_of_bounds",
    "final_retry_skipped_interpolation_failure",
    "final_retry_skipped_invalid_trace_inputs",
    "final_retry_skipped_measurement_unavailable",
    "final_retry_skipped_no_calibrated_bracket",
    "final_retry_skipped_no_eligible_directional_candidate",
    "final_retry_skipped_non_monotonic_curve",
})

SearchStatus = Literal["selected", "infeasible", "bound_exhausted", "quality_conflict", "needs_review"]
CurveShape = Literal["single_point", "monotonic", "non_monotonic"]
FinalSizeStatus = Literal["inside_target_band", "over_target", "under_target", "missing_target"]


class TargetSizeSearchError(RuntimeError):
    def __init__(self, message: str, *, status: SearchStatus, trace: dict[str, Any]) -> None:
        super().__init__(message)
        self.status = status
        self.trace = trace


class FinalSizeMissError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TargetSizeCandidate:
    attempt: int
    role: str
    crf: float
    metric: str
    metric_target: float
    metric_score: float
    min_metric_score: float
    quality_floor_met: bool
    sampled_clip_bytes: int | None
    predicted_video_bytes: int
    predicted_whole_episode_bytes: int | None
    predicted_encode_percent: float
    predicted_encode_seconds: float
    target_distance_bytes: int | None
    within_sample_band: bool
    violates_source_cap: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "role": self.role,
            "crf": self.crf,
            "metric": self.metric,
            "metric_target": self.metric_target,
            "metric_score": self.metric_score,
            "min_metric_score": self.min_metric_score,
            "quality_floor_met": self.quality_floor_met,
            "sampled_clip_bytes": self.sampled_clip_bytes,
            "predicted_video_bytes": self.predicted_video_bytes,
            "predicted_whole_episode_bytes": self.predicted_whole_episode_bytes,
            "predicted_encode_percent": self.predicted_encode_percent,
            "predicted_encode_seconds": self.predicted_encode_seconds,
            "target_distance_bytes": self.target_distance_bytes,
            "within_sample_band": self.within_sample_band,
            "violates_source_cap": self.violates_source_cap,
        }


@dataclass(frozen=True, slots=True)
class FinalSizeVerification:
    status: FinalSizeStatus
    target_size_bytes: int | None
    lower_bound_bytes: int | None
    upper_bound_bytes: int | None
    actual_output_bytes: int | None
    tolerance_percent: float | None
    retry_allowed: bool
    retry_reason: str | None
    accepted: bool = False
    compression_evidence: dict[str, Any] | None = None
    compression_authorization: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status == "inside_target_band" or self.accepted

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TARGET_SIZE_SEARCH_SCHEMA_VERSION,
            "status": self.status,
            "passed": self.passed,
            "target_size_bytes": self.target_size_bytes,
            "lower_bound_bytes": self.lower_bound_bytes,
            "upper_bound_bytes": self.upper_bound_bytes,
            "actual_output_bytes": self.actual_output_bytes,
            "tolerance_percent": self.tolerance_percent,
            "retry_allowed": self.retry_allowed,
            "retry_reason": self.retry_reason,
            "accepted": self.accepted,
            "compression_evidence": self.compression_evidence,
            "compression_authorization": self.compression_authorization,
        }


def search_target_size(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None,
        metric_name: str,
        metric_target: float,
        min_metric_score: float,
        preset: int,
        pixel_format: str,
        sample_every: str,
        sample_duration: str,
        min_crf: int,
        max_crf: int,
        svt_params: list[str],
        video_filter: str | None,
        stream_budget_ledger: StreamBudgetLedger,
        transform_plan: dict[str, Any] | None,
        process_controller: Any,
        host: dict[str, Any] | None,
        quality_temp_dir: Path | None,
        run_sample_encode: Callable[..., SampleEncodeResult],
        search_max_crf: int | None = None,
        warm_start: QualitySearchWarmStart | None = None,
        expected_search_signature_id: str | None = None,
) -> QualitySearchResult:
    _validate_search_inputs(stream_budget_ledger)
    compression_intent = compression_intent_from_policy(video_policy)
    normalized_min_crf, configured_max_crf = _normalized_crf_bounds(min_crf, max_crf)
    normalized_max_crf = _normalized_search_max_crf(configured_max_crf, search_max_crf)
    if not target_size_transform_plan_valid(transform_plan):
        trace = _trace_payload(
            status="needs_review",
            reason="transform_plan_identity_invalid",
            ledger=stream_budget_ledger,
            candidates=[],
            selected=None,
            min_metric_score=min_metric_score,
            metric_name=metric_name,
            metric_target=metric_target,
            min_crf=normalized_min_crf,
            max_crf=normalized_max_crf,
            configured_max_crf=configured_max_crf,
            transform_plan=object_dict(transform_plan) or None,
        )
        raise TargetSizeSearchError(
            "Target-size search requires an identity-bound resolved transform plan.",
            status="needs_review",
            trace=trace,
        )
    validated_transform_plan = dict(object_dict(transform_plan))
    source_cap_blocker = _source_cap_blocker(stream_budget_ledger)
    if source_cap_blocker is not None:
        trace = _trace_payload(
            status="bound_exhausted",
            reason=source_cap_blocker.code,
            ledger=stream_budget_ledger,
            candidates=[],
            selected=None,
            min_metric_score=min_metric_score,
            metric_name=metric_name,
            metric_target=metric_target,
            min_crf=normalized_min_crf,
            max_crf=normalized_max_crf,
            configured_max_crf=configured_max_crf,
            transform_plan=validated_transform_plan,
        )
        raise TargetSizeSearchError(
            source_cap_blocker.message,
            status="bound_exhausted",
            trace=trace,
        )

    def run_sample(crf: int) -> SampleEncodeResult:
        return run_sample_encode(
            source_path,
            source_codec=source_codec,
            preferred_metric=metric_name,
            crf=float(crf),
            preset=preset,
            pixel_format=pixel_format,
            sample_every=sample_every,
            sample_duration=sample_duration,
            svt_params=svt_params,
            video_filter=video_filter,
            process_controller=process_controller,
            host=host,
            quality_temp_dir=quality_temp_dir,
        )

    warm_result, warm_trace = _try_target_size_warm_start(
        warm_start,
        configured_min_crf=normalized_min_crf,
        configured_max_crf=configured_max_crf,
        metric_target=metric_target,
        min_metric_score=min_metric_score,
        ledger=stream_budget_ledger,
        transform_plan=validated_transform_plan,
        run_sample=run_sample,
        expected_search_signature_id=expected_search_signature_id,
        compression_intent=compression_intent,
    )
    if warm_result is not None:
        return warm_result

    candidates: list[TargetSizeCandidate] = []
    measured_crfs: set[int] = set()
    seed_crf = _seed_crf(stream_budget_ledger, min_crf=normalized_min_crf, max_crf=configured_max_crf)

    def measure(crf: int, role: str) -> None:
        normalized_crf = _clamp_int(crf, normalized_min_crf, normalized_max_crf)
        if normalized_crf in measured_crfs:
            return
        measured_crfs.add(normalized_crf)
        sample = run_sample(normalized_crf)
        candidates.append(
            _candidate_from_sample(
                sample,
                attempt=len(candidates) + 1,
                role=role,
                crf=float(normalized_crf),
                metric_target=metric_target,
                min_metric_score=min_metric_score,
                ledger=stream_budget_ledger,
            )
        )

    measure(seed_crf, "target_seed")
    while len(candidates) < MAX_TARGET_SIZE_CANDIDATES:
        selected = _select_candidate(candidates, compression_intent)
        if selected is not None:
            next_floor_crf = _next_intent_probe_crf(
                candidates,
                selected=selected,
                measured_crfs=measured_crfs,
                min_crf=normalized_min_crf,
                max_crf=normalized_max_crf,
                compression_intent=compression_intent,
            )
            if next_floor_crf is None:
                break
            try:
                measure(next_floor_crf, "compression_floor")
            except SampleEncodeError:
                break
            continue
        next_crf = _next_crf(candidates, measured_crfs, normalized_min_crf, normalized_max_crf, stream_budget_ledger)
        if next_crf is None:
            break
        measure(next_crf, "expanded_bound" if next_crf > configured_max_crf else "refine")

    selected = _select_candidate(candidates, compression_intent)
    if selected is not None:
        trace = _trace_payload(
            status="selected",
            reason="candidate_inside_sample_projection_band",
            ledger=stream_budget_ledger,
            candidates=candidates,
            selected=selected,
            min_metric_score=min_metric_score,
            metric_name=metric_name,
            metric_target=metric_target,
            min_crf=normalized_min_crf,
            max_crf=normalized_max_crf,
            configured_max_crf=configured_max_crf,
            transform_plan=validated_transform_plan,
        )
        trace = _trace_with_warm_start(trace, warm_trace)
        return QualitySearchResult(
            crf=selected.crf,
            metric=selected.metric,
            target=metric_target,
            score=selected.metric_score,
            stdout="target-size-search",
            target_size_trace=trace,
        )

    status, reason = _failure_status(
        candidates,
        stream_budget_ledger,
        min_crf=normalized_min_crf,
        max_crf=normalized_max_crf,
    )
    trace = _trace_payload(
        status=status,
        reason=reason,
        ledger=stream_budget_ledger,
        candidates=candidates,
        selected=None,
        min_metric_score=min_metric_score,
        metric_name=metric_name,
        metric_target=metric_target,
        min_crf=normalized_min_crf,
        max_crf=normalized_max_crf,
        configured_max_crf=configured_max_crf,
        transform_plan=validated_transform_plan,
    )
    trace = _trace_with_warm_start(trace, warm_trace)
    raise TargetSizeSearchError(_failure_message(status, reason, trace), status=status, trace=trace)


def _try_target_size_warm_start(
        warm_start: QualitySearchWarmStart | None,
        *,
        configured_min_crf: int,
        configured_max_crf: int,
        metric_target: float,
        min_metric_score: float,
        ledger: StreamBudgetLedger,
        transform_plan: dict[str, Any],
        run_sample: Callable[[int], SampleEncodeResult],
        expected_search_signature_id: str | None,
        compression_intent: CompressionIntentV1,
) -> tuple[QualitySearchResult | None, dict[str, Any] | None]:
    if warm_start is None:
        return None, None
    candidate_crf = warm_start.candidate_crf
    if (
            isinstance(candidate_crf, bool)
            or not isinstance(candidate_crf, int)
            or not configured_min_crf <= candidate_crf <= configured_max_crf
            or not math.isfinite(warm_start.requested_crf)
            or not warm_start.search_signature_id
            or warm_start.search_signature_id != expected_search_signature_id
            or not warm_start.cohort_id
    ):
        return None, _target_warm_start_trace(
            warm_start,
            status="guard_rejected",
            attempted=False,
            duration_seconds=0.0,
            candidate_count=0,
            fallback_reason="invalid_hint_contract",
        )
    if (
            not compression_intent.requires_confirmation
            and compression_intent.level in {"reference", "transparent", "perceptual_floor"}
    ):
        return None, _target_warm_start_trace(
            warm_start,
            status="guard_rejected",
            attempted=False,
            duration_seconds=0.0,
            candidate_count=0,
            fallback_reason="compression_intent_requires_directional_search",
        )
    started = time.monotonic()
    try:
        sample = run_sample(candidate_crf)
    except QualityTempSetupError as exc:
        return None, _target_warm_start_trace(
            warm_start,
            status="probe_error_fallback",
            attempted=True,
            duration_seconds=max(time.monotonic() - started, 0.0),
            candidate_count=0,
            fallback_reason="probe_error",
            error_type=type(exc).__name__,
        )
    except (QualityTempCleanupError, SampleEncodeError, ValueError) as exc:
        return None, _target_warm_start_trace(
            warm_start,
            status="probe_error_fallback",
            attempted=True,
            duration_seconds=max(time.monotonic() - started, 0.0),
            candidate_count=1,
            fallback_reason="probe_error",
            error_type=type(exc).__name__,
        )
    candidate = _candidate_from_sample(
        sample,
        attempt=1,
        role="quality_memory_hint",
        crf=float(candidate_crf),
        metric_target=metric_target,
        min_metric_score=min_metric_score,
        ledger=ledger,
    )
    selected = _select_candidate([candidate], compression_intent)
    warm_trace = _target_warm_start_trace(
        warm_start,
        status="accepted" if selected is not None else "rejected_fallback",
        attempted=True,
        duration_seconds=max(time.monotonic() - started, 0.0),
        candidate_count=1,
        fallback_reason=None if selected is not None else _warm_start_rejection_reason(candidate),
        candidate=candidate.to_payload(),
    )
    if selected is None:
        return None, warm_trace
    trace = _trace_payload(
        status="selected",
        reason="quality_memory_candidate_inside_sample_projection_band",
        ledger=ledger,
        candidates=[candidate],
        selected=candidate,
        min_metric_score=min_metric_score,
        metric_name=candidate.metric,
        metric_target=metric_target,
        min_crf=configured_min_crf,
        max_crf=configured_max_crf,
        configured_max_crf=configured_max_crf,
        transform_plan=transform_plan,
    )
    trace = _trace_with_warm_start(trace, warm_trace)
    return QualitySearchResult(
        crf=candidate.crf,
        metric=candidate.metric,
        target=metric_target,
        score=candidate.metric_score,
        stdout=sample.stdout,
        target_size_trace=trace,
    ), warm_trace


def _warm_start_rejection_reason(candidate: TargetSizeCandidate) -> str:
    if not candidate.quality_floor_met:
        return "quality_floor_miss"
    if candidate.violates_source_cap:
        return "source_cap_miss"
    if not candidate.within_sample_band:
        return "target_band_miss"
    return "candidate_rejected"


def _target_warm_start_trace(
        warm_start: QualitySearchWarmStart,
        *,
        status: str,
        attempted: bool,
        duration_seconds: float,
        candidate_count: int,
        fallback_reason: str | None = None,
        candidate: dict[str, Any] | None = None,
        error_type: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "attempted": attempted,
        "requested_crf": warm_start.requested_crf,
        "candidate_crf": warm_start.candidate_crf,
        "search_signature_id": warm_start.search_signature_id,
        "cohort_id": warm_start.cohort_id,
        "source": warm_start.source,
        "confidence": warm_start.confidence,
        "provenance_id": warm_start.provenance_id,
        "review_risks": list(warm_start.review_risks),
        "candidate_count": max(candidate_count, 0),
        "duration_seconds": round(max(duration_seconds, 0.0), 3),
        "fallback_used": status != "accepted",
        "fallback_reason": fallback_reason,
        "error_type": error_type,
        "candidate": candidate,
    }


def _trace_with_warm_start(
        trace: dict[str, Any],
        warm_start: dict[str, Any] | None,
) -> dict[str, Any]:
    if warm_start is None:
        return trace
    payload = dict(trace)
    warm_payload = dict(warm_start)
    baseline_candidate_count = len(payload.get("candidates", []))
    candidate_count = baseline_candidate_count
    if warm_payload.get("status") == "accepted":
        warm_payload["baseline_candidate_count"] = 0
    else:
        warm_payload["baseline_candidate_count"] = baseline_candidate_count
        candidate_count += int(warm_payload.get("candidate_count") or 0)
    warm_payload["total_candidate_count"] = candidate_count
    payload["candidate_count"] = candidate_count
    payload["warm_start"] = warm_payload
    return payload


def build_target_size_transform_plan(
        *,
        cadence_evidence_id: object,
        cadence_class: object,
        cadence_transform: object,
        video_filter: str | None,
) -> dict[str, Any]:
    payload = {
        "schema_version": TARGET_SIZE_TRANSFORM_PLAN_SCHEMA_VERSION,
        "cadence_evidence_id": str(cadence_evidence_id or "") or None,
        "cadence_class": str(cadence_class or "") or None,
        "cadence_transform": str(cadence_transform or "") or None,
        "video_filter": video_filter,
    }
    return {
        **payload,
        "transform_plan_id": f"tp1_{stable_json_hash(payload)[:32]}",
    }


def target_size_transform_plan_valid(transform_plan: dict[str, Any] | None) -> bool:
    payload = dict(object_dict(transform_plan))
    transform_plan_id = str(payload.pop("transform_plan_id", ""))
    return (
        payload.get("schema_version") == TARGET_SIZE_TRANSFORM_PLAN_SCHEMA_VERSION
        and transform_plan_id == f"tp1_{stable_json_hash(payload)[:32]}"
    )


def verify_final_output_size(
        ledger: StreamBudgetLedger | None,
        actual_output_bytes: int | None,
        *,
        retry_count: int = 0,
        compression_intent: CompressionIntentV1 | None = None,
        compression_evidence: CompressionEvidenceRef | None = None,
        compression_authorization: CompressionAuthorizationDecision | None = None,
) -> FinalSizeVerification:
    if ledger is None or ledger.total_target_bytes is None:
        return FinalSizeVerification(
            status="missing_target",
            target_size_bytes=None,
            lower_bound_bytes=None,
            upper_bound_bytes=None,
            actual_output_bytes=actual_output_bytes,
            tolerance_percent=None,
            retry_allowed=False,
            retry_reason="No resolved final size target is available.",
        )
    tolerance_percent = _final_tolerance_percent(ledger)
    lower, upper = _bounds(ledger.total_target_bytes, tolerance_percent)
    if actual_output_bytes is None:
        status: FinalSizeStatus = "missing_target"
    elif actual_output_bytes < lower:
        status = "under_target"
    elif actual_output_bytes > upper:
        status = "over_target"
    else:
        status = "inside_target_band"
    intent = compression_intent or legacy_compression_intent()
    under_target_accepted = status == "under_target" and intent.accepts_under_target_result
    under_target_growth_authorized = bool(
        status == "under_target"
        and not intent.requires_confirmation
        and intent.level in {"balanced", "reference"}
        and compression_authorization is not None
        and compression_authorization.intent_id == intent.semantic_id
        and compression_authorization.direction == "larger"
        and compression_authorization.outcome == "authorized"
    )
    retry_allowed = (
        (status == "over_target" or under_target_growth_authorized)
        and retry_count < MAX_FINAL_OUTPUT_RETRIES
    )
    return FinalSizeVerification(
        status=status,
        target_size_bytes=ledger.total_target_bytes,
        lower_bound_bytes=lower,
        upper_bound_bytes=upper,
        actual_output_bytes=actual_output_bytes,
        tolerance_percent=tolerance_percent,
        retry_allowed=retry_allowed,
        retry_reason=(
            "A bounded measured retry is allowed for this final-size miss."
            if retry_allowed
            else (
                (
                    "The active compression goal accepts this smaller result without spending unused bytes."
                    if under_target_accepted
                    else "Choose a compression goal before Mediaforce spends unused bytes."
                    if intent.requires_confirmation
                    else "A larger retry requires typed current-run evidence bound to this item."
                )
                if status == "under_target"
                else None if status == "inside_target_band" else "The final-size retry budget is exhausted."
            )
        ),
        accepted=under_target_accepted,
        compression_evidence=compression_evidence.to_payload() if compression_evidence is not None else None,
        compression_authorization=(
            compression_authorization.to_payload() if compression_authorization is not None else None
        ),
    )


def retry_quality_result_for_final_miss(
        quality: QualitySearchResult,
        verification: FinalSizeVerification,
        *,
        measure_candidate: Callable[[float], SampleEncodeResult] | None = None,
        compression_intent: CompressionIntentV1 | None = None,
) -> QualitySearchResult | None:
    trace = object_dict(quality.target_size_trace)
    if not trace or not verification.retry_allowed:
        return None
    selected = object_dict(trace.get("selected_candidate"))

    def skip_retry(reason: str, **details: Any) -> QualitySearchResult | None:
        quality.target_size_trace = _final_retry_skip_trace(
            trace,
            selected,
            verification,
            reason=reason,
            details=details,
        )
        return None

    selected_crf = float_value(selected.get("crf"))
    selected_metric = str(selected.get("metric") or quality.metric).strip()
    selected_metric_score = float_value(selected.get("metric_score"))
    target = object_dict(trace.get("target"))
    non_video_bytes = int_value(target.get("non_video_bytes"))
    selected_predicted_video_bytes = int_value(selected.get("predicted_video_bytes"))
    actual_output_bytes = int_value(verification.actual_output_bytes)
    actual_video_bytes = actual_output_bytes - non_video_bytes
    invalid_inputs = []
    if "crf" not in selected or not math.isfinite(selected_crf) or selected_crf < 0:
        invalid_inputs.append("selected_candidate.crf")
    if non_video_bytes < 0:
        invalid_inputs.append("target.non_video_bytes")
    if selected_predicted_video_bytes <= 0:
        invalid_inputs.append("selected_candidate.predicted_video_bytes")
    if actual_video_bytes <= 0:
        invalid_inputs.append("final_output.actual_video_bytes")
    if verification.status == "under_target" and not math.isfinite(selected_metric_score):
        invalid_inputs.append("selected_candidate.metric_score")
    if invalid_inputs:
        return skip_retry(
            "final_retry_skipped_invalid_trace_inputs",
            invalid_inputs=invalid_inputs,
        )
    calibration_factor = actual_video_bytes / selected_predicted_video_bytes
    if not MIN_FINAL_RETRY_CALIBRATION_FACTOR <= calibration_factor <= MAX_FINAL_RETRY_CALIBRATION_FACTOR:
        return skip_retry(
            "final_retry_skipped_calibration_factor_out_of_bounds",
            video_projection_factor=round(calibration_factor, 9),
        )
    target_size_bytes = int_value(verification.target_size_bytes)
    lower_bound_bytes = int_value(verification.lower_bound_bytes)
    upper_bound_bytes = int_value(verification.upper_bound_bytes)
    invalid_inputs = []
    if target_size_bytes <= 0:
        invalid_inputs.append("final_output.target_size_bytes")
    if lower_bound_bytes <= 0:
        invalid_inputs.append("final_output.lower_bound_bytes")
    if upper_bound_bytes <= 0:
        invalid_inputs.append("final_output.upper_bound_bytes")
    source_cap = object_dict(trace.get("source_cap"))
    source_cap_video_bytes = int_value(source_cap.get("video_cap_bytes")) or None
    minimum_quality_score = float_value(object_dict(trace.get("quality_floor")).get("minimum"))
    if minimum_quality_score <= 0:
        invalid_inputs.append("quality_floor.minimum")
    if invalid_inputs:
        return skip_retry(
            "final_retry_skipped_invalid_trace_inputs",
            invalid_inputs=invalid_inputs,
            video_projection_factor=round(calibration_factor, 9),
        )
    candidates = [object_dict(candidate) for candidate in trace.get("candidates", []) if isinstance(candidate, dict)]
    cap_violating_crfs = [
        float_value(candidate.get("crf"))
        for candidate in candidates
        if candidate.get("crf") is not None
        and math.isfinite(float_value(candidate.get("crf")))
        and source_cap_video_bytes is not None
        and (
            bool(candidate.get("violates_source_cap"))
            or _calibrated_video_bytes(candidate, calibration_factor) > source_cap_video_bytes
        )
    ]
    if verification.status == "over_target":
        directional_candidates = [
            candidate for candidate in candidates
            if float_value(candidate.get("crf")) > selected_crf
            and int_value(candidate.get("predicted_video_bytes")) > 0
        ]
        eligible = [
            candidate for candidate in directional_candidates
            if bool(candidate.get("quality_floor_met"))
            and math.isfinite(float_value(candidate.get("metric_score")))
            and float_value(candidate.get("metric_score")) >= minimum_quality_score
            and str(candidate.get("metric") or "").strip().casefold() == selected_metric.casefold()
        ]
        eligible.sort(key=lambda candidate: float_value(candidate.get("crf")))
    elif verification.status == "under_target":
        intent = compression_intent or legacy_compression_intent()
        if intent.requires_confirmation or intent.level not in {"balanced", "reference"}:
            return None
        directional_candidates = [
            candidate for candidate in candidates
            if candidate.get("crf") is not None
            and 0 <= float_value(candidate.get("crf")) < selected_crf
            and int_value(candidate.get("predicted_video_bytes")) > 0
        ]
        eligible = [
            candidate for candidate in directional_candidates
            if bool(candidate.get("quality_floor_met"))
            and math.isfinite(float_value(candidate.get("metric_score")))
            and str(candidate.get("metric") or "").strip().casefold() == selected_metric.casefold()
            and float_value(candidate.get("metric_score")) > selected_metric_score
        ]
        eligible.sort(key=lambda candidate: -float_value(candidate.get("crf")))
    else:
        return None
    if directional_candidates and not eligible:
        return skip_retry(
            "final_retry_skipped_no_eligible_directional_candidate",
            directional_candidate_count=len(directional_candidates),
            eligible_candidate_count=0,
            video_projection_factor=round(calibration_factor, 9),
        )
    calibrated = [
        (
            candidate,
            _calibrated_video_bytes(candidate, calibration_factor),
            _calibrated_total_bytes(candidate, calibration_factor, non_video_bytes),
        )
        for candidate in eligible
    ]
    calibrated_safe = [
        entry for entry in calibrated
        if source_cap_video_bytes is None or entry[1] <= source_cap_video_bytes
    ]
    if eligible and not calibrated_safe:
        return skip_retry(
            "final_retry_skipped_all_candidates_over_source_cap",
            calibrated_candidate_count=len(calibrated),
            calibrated_safe_candidate_count=0,
            eligible_candidate_count=len(eligible),
            source_cap_video_bytes=source_cap_video_bytes,
            video_projection_factor=round(calibration_factor, 9),
        )
    in_band = [
        entry for entry in calibrated_safe
        if lower_bound_bytes <= entry[2] <= upper_bound_bytes
    ]
    if in_band:
        candidate, calibrated_video_bytes, calibrated_total_bytes = min(
            in_band,
            key=lambda entry: (
                abs(entry[2] - target_size_bytes),
                abs(float_value(entry[0].get("crf")) - selected_crf),
                -float_value(entry[0].get("metric_score")),
            ),
        )
        return _final_retry_quality_result(
            quality,
            trace,
            selected,
            _candidate_with_calibration(
                candidate,
                calibration_factor=calibration_factor,
                calibrated_video_bytes=calibrated_video_bytes,
                calibrated_total_bytes=calibrated_total_bytes,
                source_cap_video_bytes=source_cap_video_bytes,
                lower_bound_bytes=lower_bound_bytes,
                upper_bound_bytes=upper_bound_bytes,
            ),
            verification,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            bracket_candidate=candidate,
        )

    if verification.status == "over_target":
        bracket = next((entry for entry in calibrated_safe if entry[2] < target_size_bytes), None)
    else:
        bracket = next((entry for entry in calibrated_safe if entry[2] > target_size_bytes), None)
    if bracket is None:
        missing_bracket_reason = (
            "final_retry_skipped_no_eligible_directional_candidate"
            if not eligible
            else "final_retry_skipped_no_calibrated_bracket"
        )
        if object_dict(trace.get("curve")).get("shape") == "non_monotonic":
            return skip_retry(
                "final_retry_skipped_non_monotonic_curve",
                probe_reason=missing_bracket_reason,
            )
        probe = _bounded_directional_probe(
            verification_status=verification.status,
            selected=selected,
            selected_actual_video_bytes=actual_video_bytes,
            calibrated_candidates=calibrated,
            target_video_bytes=target_size_bytes - non_video_bytes,
            source_cap_video_bytes=source_cap_video_bytes,
            cap_violating_crfs=cap_violating_crfs,
            crf_bounds=object_dict(trace.get("crf_bounds")),
            measured_crfs=_measured_integer_crfs(candidates),
        )
        if probe is None:
            return skip_retry(
                missing_bracket_reason,
                calibrated_candidate_count=len(calibrated),
                calibrated_safe_candidate_count=len(calibrated_safe),
                eligible_candidate_count=len(eligible),
                video_projection_factor=round(calibration_factor, 9),
            )
        probe_crf, probe_from_candidate = probe
        if measure_candidate is None:
            return skip_retry(
                "final_retry_skipped_measurement_unavailable",
                calibrated_candidate_count=len(calibrated),
                calibrated_safe_candidate_count=len(calibrated_safe),
                eligible_candidate_count=len(eligible),
                probe_crf=probe_crf,
                probe_reason=missing_bracket_reason,
                video_projection_factor=round(calibration_factor, 9),
            )
        sample, candidate = _measure_calibrated_retry_candidate(
            measure_candidate,
            trace=trace,
            crf=probe_crf,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            target_size_bytes=target_size_bytes,
            source_cap_video_bytes=source_cap_video_bytes,
            lower_bound_bytes=lower_bound_bytes,
            upper_bound_bytes=upper_bound_bytes,
        )
        rejection_reason = _final_retry_candidate_rejection_reason(
            sample,
            candidate,
            verification_status=verification.status,
            selected_metric=selected_metric,
            selected_metric_score=selected_metric_score,
            minimum_quality_score=minimum_quality_score,
        )
        if rejection_reason is not None:
            quality.target_size_trace = _final_retry_rejection_trace(
                trace,
                selected,
                candidate,
                verification,
                calibration_factor=calibration_factor,
                non_video_bytes=non_video_bytes,
                bracket_candidate=probe_from_candidate,
                rejection_reason=rejection_reason,
                strategy="bounded_directional_probe",
            )
            return None
        retry_trace = dict(trace)
        retry_trace["candidates"] = [dict(existing) for existing in candidates]
        retry_trace["candidates"].append(candidate)
        _increment_retry_candidate_count(trace, retry_trace)
        _refresh_retry_curve(retry_trace)
        return _final_retry_quality_result(
            quality,
            retry_trace,
            selected,
            candidate,
            verification,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            bracket_candidate=probe_from_candidate,
            strategy="bounded_directional_probe",
            stdout=sample.stdout,
        )
    if measure_candidate is None:
        return skip_retry(
            "final_retry_skipped_measurement_unavailable",
            calibrated_candidate_count=len(calibrated),
            calibrated_safe_candidate_count=len(calibrated_safe),
            eligible_candidate_count=len(eligible),
            video_projection_factor=round(calibration_factor, 9),
        )
    bracket_candidate, bracket_video_bytes, _bracket_total_bytes = bracket
    retry_crf = _interpolated_retry_crf(
        selected_crf=selected_crf,
        selected_actual_video_bytes=actual_video_bytes,
        bracket_crf=float_value(bracket_candidate.get("crf")),
        bracket_calibrated_video_bytes=bracket_video_bytes,
        non_video_bytes=non_video_bytes,
        target_size_bytes=target_size_bytes,
        lower_bound_bytes=lower_bound_bytes,
        upper_bound_bytes=upper_bound_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
        measured_crfs=_measured_integer_crfs(candidates),
    )
    if retry_crf is None:
        return skip_retry(
            "final_retry_skipped_interpolation_failure",
            bracket_crf=float_value(bracket_candidate.get("crf")),
            calibrated_candidate_count=len(calibrated),
            calibrated_safe_candidate_count=len(calibrated_safe),
            eligible_candidate_count=len(eligible),
            video_projection_factor=round(calibration_factor, 9),
        )
    sample, candidate = _measure_calibrated_retry_candidate(
        measure_candidate,
        trace=trace,
        crf=retry_crf,
        calibration_factor=calibration_factor,
        non_video_bytes=non_video_bytes,
        target_size_bytes=target_size_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
        lower_bound_bytes=lower_bound_bytes,
        upper_bound_bytes=upper_bound_bytes,
    )
    rejection_reason = _final_retry_candidate_rejection_reason(
        sample,
        candidate,
        verification_status=verification.status,
        selected_metric=selected_metric,
        selected_metric_score=selected_metric_score,
        minimum_quality_score=minimum_quality_score,
    )
    if rejection_reason is not None:
        quality.target_size_trace = _final_retry_rejection_trace(
            trace,
            selected,
            candidate,
            verification,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            bracket_candidate=bracket_candidate,
            rejection_reason=rejection_reason,
        )
        return None
    retry_trace_candidates = [dict(existing) for existing in candidates]
    retry_trace_candidates.append(candidate)
    retry_trace = dict(trace)
    retry_trace["candidates"] = retry_trace_candidates
    _increment_retry_candidate_count(trace, retry_trace)
    _refresh_retry_curve(retry_trace)
    return _final_retry_quality_result(
        quality,
        retry_trace,
        selected,
        candidate,
        verification,
        calibration_factor=calibration_factor,
        non_video_bytes=non_video_bytes,
        bracket_candidate=bracket_candidate,
        stdout=sample.stdout,
    )


def _final_retry_skip_trace(
        trace: dict[str, Any],
        selected: dict[str, Any],
        verification: FinalSizeVerification,
        *,
        reason: str,
        details: dict[str, Any],
) -> dict[str, Any]:
    if reason not in FINAL_RETRY_SKIP_REASONS:
        raise ValueError(f"Unsupported final retry skip reason: {reason}")
    retry_trace = dict(trace)
    previous_status = str(trace.get("status") or "") or None
    previous_selection_reason = str(trace.get("selection_reason") or "") or None
    retry_trace["status"] = "needs_review"
    retry_trace["selection_reason"] = reason
    if selected:
        retry_trace["final_retry_from_candidate"] = selected
    retry_trace["final_retry_skip"] = {
        "schema_version": TARGET_SIZE_SEARCH_SCHEMA_VERSION,
        "status": "skipped",
        "reason": reason,
        "verification_status": verification.status,
        "retry_allowed": verification.retry_allowed,
        "previous_status": previous_status,
        "previous_selection_reason": previous_selection_reason,
        **details,
    }
    return retry_trace


def _final_retry_quality_result(
        quality: QualitySearchResult,
        trace: dict[str, Any],
        selected: dict[str, Any],
        candidate: dict[str, Any],
        verification: FinalSizeVerification,
        *,
        calibration_factor: float,
        non_video_bytes: int,
        bracket_candidate: dict[str, Any],
        strategy: str = "calibrated_bracket",
        stdout: str | None = None,
) -> QualitySearchResult:
    retry_trace = dict(trace)
    retry_trace["selected_candidate"] = candidate
    retry_trace["status"] = "selected"
    retry_trace["selection_reason"] = "calibrated_final_size_retry_candidate"
    retry_trace["final_retry_from_candidate"] = selected
    retry_trace["final_retry_calibration"] = {
        **_final_retry_calibration_payload(
            selected,
            candidate,
            verification,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            bracket_candidate=bracket_candidate,
            strategy=strategy,
        ),
        "status": "selected",
    }
    return QualitySearchResult(
        crf=float_value(candidate.get("crf")),
        metric=str(candidate.get("metric") or quality.metric),
        target=float_value(candidate.get("metric_target")) or quality.target,
        score=float_value(candidate.get("metric_score")),
        stdout=stdout if stdout is not None else quality.stdout,
        target_size_trace=retry_trace,
    )


def _candidate_with_calibration(
        candidate: dict[str, Any],
        *,
        calibration_factor: float,
        calibrated_video_bytes: int,
        calibrated_total_bytes: int,
        source_cap_video_bytes: int | None,
        lower_bound_bytes: int,
        upper_bound_bytes: int,
) -> dict[str, Any]:
    return {
        **candidate,
        "sample_projection_violates_source_cap": bool(candidate.get("violates_source_cap")),
        "violates_source_cap": (
            source_cap_video_bytes is not None
            and calibrated_video_bytes > source_cap_video_bytes
        ),
        "projection_calibration_factor": round(calibration_factor, 9),
        "calibrated_predicted_video_bytes": calibrated_video_bytes,
        "calibrated_predicted_whole_episode_bytes": calibrated_total_bytes,
        "within_calibrated_final_band": lower_bound_bytes <= calibrated_total_bytes <= upper_bound_bytes,
    }


def _final_retry_rejection_trace(
        trace: dict[str, Any],
        selected: dict[str, Any],
        candidate: dict[str, Any],
        verification: FinalSizeVerification,
        *,
        calibration_factor: float,
        non_video_bytes: int,
        bracket_candidate: dict[str, Any],
        rejection_reason: str,
        strategy: str = "calibrated_bracket",
) -> dict[str, Any]:
    retry_trace = dict(trace)
    retry_trace["candidates"] = [
        *[dict(existing) for existing in trace.get("candidates", []) if isinstance(existing, dict)],
        candidate,
    ]
    _increment_retry_candidate_count(trace, retry_trace)
    _refresh_retry_curve(retry_trace)
    retry_trace["status"] = "needs_review"
    retry_trace["selection_reason"] = rejection_reason
    retry_trace["final_retry_from_candidate"] = selected
    retry_trace["final_retry_calibration"] = {
        **_final_retry_calibration_payload(
            selected,
            candidate,
            verification,
            calibration_factor=calibration_factor,
            non_video_bytes=non_video_bytes,
            bracket_candidate=bracket_candidate,
            strategy=strategy,
        ),
        "status": "rejected",
        "rejection_reason": rejection_reason,
    }
    return retry_trace


def _increment_retry_candidate_count(previous_trace: dict[str, Any], retry_trace: dict[str, Any]) -> None:
    previous_candidates = [
        candidate for candidate in previous_trace.get("candidates", []) if isinstance(candidate, dict)
    ]
    retry_trace["candidate_count"] = max(
        int_value(previous_trace.get("candidate_count")),
        len(previous_candidates),
    ) + 1
    warm_start = object_dict(previous_trace.get("warm_start"))
    if not warm_start:
        return
    warm_payload = dict(warm_start)
    warm_payload["baseline_candidate_count"] = int_value(warm_payload.get("baseline_candidate_count")) + 1
    warm_payload["total_candidate_count"] = int_value(warm_payload.get("total_candidate_count")) + 1
    retry_trace["warm_start"] = warm_payload


def _refresh_retry_curve(trace: dict[str, Any]) -> None:
    curve = object_dict(trace.get("curve"))
    if not curve:
        return
    candidates = [candidate for candidate in trace.get("candidates", []) if isinstance(candidate, dict)]
    retry_measurement_count = sum(candidate.get("role") == "final_retry_measurement" for candidate in candidates)
    trace["curve"] = {
        **curve,
        "shape": _payload_curve_shape(candidates),
        "search_candidate_count": int_value(curve.get("candidate_count")),
        "candidate_count": len(candidates),
        "final_retry_measurement_count": retry_measurement_count,
    }


def _final_retry_calibration_payload(
        selected: dict[str, Any],
        candidate: dict[str, Any],
        verification: FinalSizeVerification,
        *,
        calibration_factor: float,
        non_video_bytes: int,
        bracket_candidate: dict[str, Any],
        strategy: str = "calibrated_bracket",
) -> dict[str, Any]:
    payload = {
        "schema_version": TARGET_SIZE_SEARCH_SCHEMA_VERSION,
        "strategy": strategy,
        "anchor_crf": float_value(selected.get("crf")),
        "anchor_predicted_video_bytes": int_value(selected.get("predicted_video_bytes")),
        "anchor_actual_output_bytes": verification.actual_output_bytes,
        "non_video_bytes": non_video_bytes,
        "video_projection_factor": round(calibration_factor, 9),
        "selected_retry_crf": float_value(candidate.get("crf")),
        "calibrated_predicted_video_bytes": int_value(candidate.get("calibrated_predicted_video_bytes")),
        "calibrated_predicted_whole_episode_bytes": int_value(
            candidate.get("calibrated_predicted_whole_episode_bytes")
        ),
    }
    payload["bracket_crf"] = None
    payload["probe_from_crf"] = None
    reference_key = "probe_from_crf" if strategy == "bounded_directional_probe" else "bracket_crf"
    payload[reference_key] = float_value(bracket_candidate.get("crf"))
    return payload


def _calibrated_video_bytes(candidate: dict[str, Any], calibration_factor: float) -> int:
    return round(int_value(candidate.get("predicted_video_bytes")) * calibration_factor)


def _calibrated_total_bytes(candidate: dict[str, Any], calibration_factor: float, non_video_bytes: int) -> int:
    return _calibrated_video_bytes(candidate, calibration_factor) + non_video_bytes


def _bounded_directional_probe(
        *,
        verification_status: FinalSizeStatus,
        selected: dict[str, Any],
        selected_actual_video_bytes: int,
        calibrated_candidates: list[tuple[dict[str, Any], int, int]],
        target_video_bytes: int,
        source_cap_video_bytes: int | None,
        cap_violating_crfs: list[float],
        crf_bounds: dict[str, Any],
        measured_crfs: set[int],
) -> tuple[float, dict[str, Any]] | None:
    if verification_status not in {"over_target", "under_target"}:
        return None
    if "min_crf" not in crf_bounds or "max_crf" not in crf_bounds:
        return None
    min_crf = int_value(crf_bounds.get("min_crf"))
    max_crf = int_value(crf_bounds.get("max_crf"))
    selected_crf = float_value(selected.get("crf"))
    if (
            min_crf < 0
            or max_crf < min_crf
            or not math.isfinite(selected_crf)
            or selected_actual_video_bytes <= 0
            or target_video_bytes <= 0
    ):
        return None

    calibrated_safe_candidates = [
        entry for entry in calibrated_candidates
        if source_cap_video_bytes is None or entry[1] <= source_cap_video_bytes
    ]
    if calibrated_safe_candidates:
        if verification_status == "over_target":
            probe_from, probe_from_video_bytes, _ = max(
                calibrated_safe_candidates,
                key=lambda entry: float_value(entry[0].get("crf")),
            )
        else:
            probe_from, probe_from_video_bytes, _ = min(
                calibrated_safe_candidates,
                key=lambda entry: float_value(entry[0].get("crf")),
            )
    else:
        probe_from = selected
        probe_from_video_bytes = selected_actual_video_bytes

    probe_from_crf = float_value(probe_from.get("crf"))
    if not math.isfinite(probe_from_crf) or probe_from_video_bytes <= 0:
        return None
    if not math.isclose(probe_from_crf, selected_crf):
        slope = (
            math.log(probe_from_video_bytes) - math.log(selected_actual_video_bytes)
        ) / (probe_from_crf - selected_crf)
        if not math.isfinite(slope) or slope >= 0:
            return None
        estimated_crf = probe_from_crf + (
            math.log(target_video_bytes) - math.log(probe_from_video_bytes)
        ) / slope
    else:
        estimated_crf = selected_crf + CRF_PER_BITRATE_HALVING * math.log2(
            selected_actual_video_bytes / target_video_bytes
        )
    if not math.isfinite(estimated_crf):
        return None
    if verification_status == "over_target":
        directional_min = math.floor(probe_from_crf) + 1
        directional_max = max_crf
    else:
        directional_min = max(
            min_crf,
            math.floor(max(cap_violating_crfs)) + 1 if cap_violating_crfs else min_crf,
        )
        directional_max = math.ceil(probe_from_crf) - 1
    directional_min = max(
        directional_min,
        math.ceil(probe_from_crf - MAX_DIRECTIONAL_PROBE_CRF_STEP),
    )
    directional_max = min(
        directional_max,
        math.floor(probe_from_crf + MAX_DIRECTIONAL_PROBE_CRF_STEP),
    )
    if directional_min > directional_max:
        return None
    seed = _clamp_int(round(estimated_crf), directional_min, directional_max)
    probe_crf = _nearest_unmeasured(seed, measured_crfs, directional_min, directional_max)
    if probe_crf is None:
        return None
    return float(probe_crf), dict(probe_from)


def _final_retry_candidate_rejection_reason(
        sample: SampleEncodeResult,
        candidate: dict[str, Any],
        *,
        verification_status: FinalSizeStatus,
        selected_metric: str,
        selected_metric_score: float,
        minimum_quality_score: float,
) -> str | None:
    if not math.isfinite(sample.score):
        return "final_retry_measurement_invalid_quality_score"
    if sample.predicted_encode_size_bytes <= 0:
        return "final_retry_measurement_invalid_projection"
    if sample.metric.casefold() != selected_metric.casefold():
        return "final_retry_measurement_metric_mismatch"
    if sample.score < minimum_quality_score:
        return "final_retry_measurement_below_quality_floor"
    if verification_status == "under_target" and sample.score <= selected_metric_score:
        return "final_retry_measurement_no_quality_benefit"
    if bool(candidate.get("violates_source_cap")):
        return "final_retry_measurement_exceeds_source_cap"
    if not bool(candidate.get("within_calibrated_final_band")):
        return "final_retry_measurement_outside_final_band"
    return None


def _interpolated_retry_crf(
        *,
        selected_crf: float,
        selected_actual_video_bytes: int,
        bracket_crf: float,
        bracket_calibrated_video_bytes: int,
        non_video_bytes: int,
        target_size_bytes: int,
        lower_bound_bytes: int,
        upper_bound_bytes: int,
        source_cap_video_bytes: int | None,
        measured_crfs: set[int],
) -> float | None:
    if (
            selected_actual_video_bytes <= 0
            or bracket_calibrated_video_bytes <= 0
            or bracket_crf == selected_crf
    ):
        return None
    start = math.floor(min(selected_crf, bracket_crf)) + 1
    stop = math.ceil(max(selected_crf, bracket_crf))
    candidates: list[tuple[float, int]] = []
    for integer_crf in range(start, stop):
        if integer_crf in measured_crfs:
            continue
        retry_crf = float(integer_crf)
        fraction = (retry_crf - selected_crf) / (bracket_crf - selected_crf)
        if not 0 < fraction < 1:
            continue
        predicted_video_bytes = round(
            math.exp(
                math.log(selected_actual_video_bytes)
                + (math.log(bracket_calibrated_video_bytes) - math.log(selected_actual_video_bytes)) * fraction
            )
        )
        predicted_total_bytes = predicted_video_bytes + non_video_bytes
        if (
                lower_bound_bytes <= predicted_total_bytes <= upper_bound_bytes
                and (source_cap_video_bytes is None or predicted_video_bytes <= source_cap_video_bytes)
        ):
            candidates.append((retry_crf, predicted_total_bytes))
    if not candidates:
        return None
    return min(candidates, key=lambda entry: (abs(entry[1] - target_size_bytes), entry[0]))[0]


def _measured_integer_crfs(candidates: list[dict[str, Any]]) -> set[int]:
    measured: set[int] = set()
    for candidate in candidates:
        if candidate.get("crf") is None:
            continue
        crf = float_value(candidate.get("crf"))
        rounded_crf = round(crf) if math.isfinite(crf) else 0
        if math.isfinite(crf) and math.isclose(crf, rounded_crf):
            measured.add(rounded_crf)
    return measured


def _measured_retry_candidate(
        sample: SampleEncodeResult,
        *,
        trace: dict[str, Any],
        crf: float,
        non_video_bytes: int,
        target_size_bytes: int,
        source_cap_video_bytes: int | None,
) -> dict[str, Any]:
    target = object_dict(trace.get("target"))
    tolerance = float_value(target.get("sample_projection_tolerance_percent"))
    lower_bound_bytes, upper_bound_bytes = _bounds(target_size_bytes, tolerance)
    predicted_total_bytes = sample.predicted_encode_size_bytes + non_video_bytes
    attempts = [int_value(object_dict(candidate).get("attempt")) for candidate in trace.get("candidates", [])]
    minimum_quality_score = float_value(object_dict(trace.get("quality_floor")).get("minimum"))
    return {
        "attempt": max(attempts, default=0) + 1,
        "role": "final_retry_measurement",
        "crf": crf,
        "metric": sample.metric,
        "metric_target": float_value(object_dict(trace.get("quality_floor")).get("target")),
        "metric_score": sample.score,
        "min_metric_score": minimum_quality_score,
        "quality_floor_met": sample.score >= minimum_quality_score,
        "sampled_clip_bytes": sample.sampled_clip_size_bytes,
        "predicted_video_bytes": sample.predicted_encode_size_bytes,
        "predicted_whole_episode_bytes": predicted_total_bytes,
        "predicted_encode_percent": sample.predicted_encode_percent,
        "predicted_encode_seconds": sample.predicted_encode_seconds,
        "target_distance_bytes": abs(predicted_total_bytes - target_size_bytes),
        "within_sample_band": lower_bound_bytes <= predicted_total_bytes <= upper_bound_bytes,
        "violates_source_cap": (
            source_cap_video_bytes is not None
            and sample.predicted_encode_size_bytes > source_cap_video_bytes
        ),
    }


def _measure_calibrated_retry_candidate(
        measure_candidate: Callable[[float], SampleEncodeResult],
        *,
        trace: dict[str, Any],
        crf: float,
        calibration_factor: float,
        non_video_bytes: int,
        target_size_bytes: int,
        source_cap_video_bytes: int | None,
        lower_bound_bytes: int,
        upper_bound_bytes: int,
) -> tuple[SampleEncodeResult, dict[str, Any]]:
    sample = measure_candidate(crf)
    measured_candidate = _measured_retry_candidate(
        sample,
        trace=trace,
        crf=crf,
        non_video_bytes=non_video_bytes,
        target_size_bytes=target_size_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
    )
    calibrated_video_bytes = round(sample.predicted_encode_size_bytes * calibration_factor)
    return sample, _candidate_with_calibration(
        measured_candidate,
        calibration_factor=calibration_factor,
        calibrated_video_bytes=calibrated_video_bytes,
        calibrated_total_bytes=calibrated_video_bytes + non_video_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
        lower_bound_bytes=lower_bound_bytes,
        upper_bound_bytes=upper_bound_bytes,
    )


def target_trace_with_actual_output(
        trace: dict[str, Any] | None,
        verification: FinalSizeVerification,
        *,
        retry_count: int,
) -> dict[str, Any] | None:
    payload = object_dict(trace)
    if not payload:
        return None
    payload = dict(payload)
    final_output = {
        **verification.to_payload(),
        "retry_count": retry_count,
        "max_retries": MAX_FINAL_OUTPUT_RETRIES,
    }
    attempts = [dict(attempt) for attempt in payload.get("final_output_attempts", []) if isinstance(attempt, dict)]
    attempts.append(final_output)
    payload["final_output_attempts"] = attempts
    payload["final_output"] = final_output
    return payload


def _validate_search_inputs(ledger: StreamBudgetLedger) -> None:
    if ledger.arithmetic_infeasible:
        trace = _trace_payload(
            status="infeasible",
            reason="arithmetically_infeasible_stream_budget",
            ledger=ledger,
            candidates=[],
            selected=None,
            min_metric_score=0.0,
            metric_name="unknown",
            metric_target=0.0,
            min_crf=0,
            max_crf=0,
            transform_plan=None,
        )
        raise TargetSizeSearchError(
            "The approved size target leaves no positive production video budget.",
            status="infeasible",
            trace=trace,
        )
    if ledger.total_target_bytes is None or ledger.remaining_video_bytes is None:
        trace = _trace_payload(
            status="infeasible",
            reason="missing_target_video_budget",
            ledger=ledger,
            candidates=[],
            selected=None,
            min_metric_score=0.0,
            metric_name="unknown",
            metric_target=0.0,
            min_crf=0,
            max_crf=0,
            transform_plan=None,
        )
        raise TargetSizeSearchError(
            "The stream budget ledger does not contain a resolved target video budget.",
            status="infeasible",
            trace=trace,
        )
    if ledger.non_video_bytes is None:
        trace = _trace_payload(
            status="needs_review",
            reason="non_video_budget_requires_measurement",
            ledger=ledger,
            candidates=[],
            selected=None,
            min_metric_score=0.0,
            metric_name="unknown",
            metric_target=0.0,
            min_crf=0,
            max_crf=0,
            transform_plan=None,
        )
        raise TargetSizeSearchError(
            "The stream budget ledger still requires non-video measurement before target-size search.",
            status="needs_review",
            trace=trace,
        )


def _normalized_crf_bounds(min_crf: int, max_crf: int) -> tuple[int, int]:
    lower = max(0, min(int(min_crf), int(max_crf)))
    upper = min(63, max(int(min_crf), int(max_crf)))
    return lower, upper


def _normalized_search_max_crf(configured_max_crf: int, search_max_crf: int | None) -> int:
    if search_max_crf is None:
        return configured_max_crf
    return min(63, max(configured_max_crf, int(search_max_crf)))


def _seed_crf(ledger: StreamBudgetLedger, *, min_crf: int, max_crf: int) -> int:
    if min_crf >= max_crf:
        return min_crf
    target_percent = None
    if ledger.source_size_bytes and ledger.remaining_video_bytes is not None:
        target_percent = max(0.0, ledger.remaining_video_bytes / ledger.source_size_bytes * 100.0)
    cap_percent = ledger.source_cap_video_percent or ledger.source_cap_percent or 100.0
    if target_percent is None or cap_percent <= 0:
        return round((min_crf + max_crf) / 2)
    ratio = max(0.0, min(target_percent / cap_percent, 1.0))
    return _clamp_int(round(min_crf + (1.0 - ratio) * (max_crf - min_crf)), min_crf, max_crf)


def _candidate_from_sample(
        sample: SampleEncodeResult,
        *,
        attempt: int,
        role: str,
        crf: float,
        metric_target: float,
        min_metric_score: float,
        ledger: StreamBudgetLedger,
) -> TargetSizeCandidate:
    predicted_total = (
        sample.predicted_encode_size_bytes + ledger.non_video_bytes
        if ledger.non_video_bytes is not None
        else None
    )
    lower, upper = _sample_bounds(ledger)
    distance = abs(predicted_total - ledger.total_target_bytes) if predicted_total is not None and ledger.total_target_bytes else None
    cap_bytes = ledger.source_cap_video_bytes
    return TargetSizeCandidate(
        attempt=attempt,
        role=role,
        crf=crf,
        metric=sample.metric,
        metric_target=metric_target,
        metric_score=sample.score,
        min_metric_score=min_metric_score,
        quality_floor_met=sample.score >= min_metric_score,
        sampled_clip_bytes=sample.sampled_clip_size_bytes,
        predicted_video_bytes=sample.predicted_encode_size_bytes,
        predicted_whole_episode_bytes=predicted_total,
        predicted_encode_percent=sample.predicted_encode_percent,
        predicted_encode_seconds=sample.predicted_encode_seconds,
        target_distance_bytes=distance,
        within_sample_band=predicted_total is not None and lower <= predicted_total <= upper,
        violates_source_cap=cap_bytes is not None and sample.predicted_encode_size_bytes > cap_bytes,
    )


def _select_candidate(
        candidates: list[TargetSizeCandidate],
        compression_intent: CompressionIntentV1,
) -> TargetSizeCandidate | None:
    eligible = [
        candidate for candidate in candidates
        if candidate.within_sample_band and candidate.quality_floor_met and not candidate.violates_source_cap
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: compression_candidate_rank(
            compression_intent,
            target_distance_bytes=(
                candidate.target_distance_bytes if candidate.target_distance_bytes is not None else 10 ** 30
            ),
            predicted_bytes=(
                candidate.predicted_whole_episode_bytes
                if candidate.predicted_whole_episode_bytes is not None
                else 10 ** 30
            ),
            metric_score=candidate.metric_score,
            crf=candidate.crf,
        ),
    )


def _next_crf(
        candidates: list[TargetSizeCandidate],
        measured_crfs: set[int],
        min_crf: int,
        max_crf: int,
        ledger: StreamBudgetLedger,
) -> int | None:
    lower, upper = _sample_bounds(ledger)
    above = [candidate for candidate in candidates if (candidate.predicted_whole_episode_bytes or 0) > upper]
    below = [
        candidate for candidate in candidates
        if candidate.predicted_whole_episode_bytes is not None and candidate.predicted_whole_episode_bytes < lower
    ]
    if above and below:
        sorted_candidates = sorted(candidates, key=lambda candidate: candidate.crf)
        pairs: list[tuple[int, int]] = []
        for left, right in zip(sorted_candidates, sorted_candidates[1:]):
            left_size = left.predicted_whole_episode_bytes
            right_size = right.predicted_whole_episode_bytes
            if left_size is None or right_size is None:
                continue
            if (left_size - lower) * (right_size - lower) <= 0 or (left_size - upper) * (right_size - upper) <= 0:
                pairs.append((int(round(left.crf)), int(round(right.crf))))
        for left, right in sorted(pairs, key=lambda pair: (abs(pair[1] - pair[0]), pair[0])):
            midpoint = round((left + right) / 2)
            candidate = _nearest_unmeasured(midpoint, measured_crfs, left + 1, right - 1)
            if candidate is not None:
                return candidate
    if above:
        highest_measured = max(measured_crfs)
        if highest_measured >= max_crf:
            return None
        expansion_probe = min(max_crf, highest_measured + TARGET_SIZE_BOUND_EXPANSION_STEP)
        return _nearest_unmeasured(expansion_probe, measured_crfs, highest_measured + 1, max_crf)
    if below:
        if min(measured_crfs) <= min_crf:
            return None
        return _nearest_unmeasured(min_crf, measured_crfs, min_crf, max_crf)
    closest = min(candidates, key=lambda candidate: _candidate_distance(candidate), default=None)
    if closest is None:
        return None
    return _nearest_unmeasured(int(round(closest.crf)), measured_crfs, min_crf, max_crf)


def _next_intent_probe_crf(
        candidates: list[TargetSizeCandidate],
        *,
        selected: TargetSizeCandidate,
        measured_crfs: set[int],
        min_crf: int,
        max_crf: int,
        compression_intent: CompressionIntentV1,
) -> int | None:
    selected_crf = int(round(selected.crf))
    if not compression_intent.requires_confirmation and compression_intent.level == "reference":
        lower_crf_candidates = [candidate for candidate in candidates if candidate.crf < selected.crf]
        if any(
                not candidate.within_sample_band
                or not candidate.quality_floor_met
                or candidate.violates_source_cap
                for candidate in lower_crf_candidates
        ):
            return None
        return _nearest_unmeasured(selected_crf - 1, measured_crfs, min_crf, selected_crf - 1)
    if not compression_intent.accepts_under_target_result:
        return None
    higher_crf_candidates = [candidate for candidate in candidates if candidate.crf > selected.crf]
    if any(
            not candidate.within_sample_band
            or not candidate.quality_floor_met
            or candidate.violates_source_cap
            for candidate in higher_crf_candidates
    ):
        return None
    return _nearest_unmeasured(selected_crf + 1, measured_crfs, selected_crf + 1, max_crf)


def _nearest_unmeasured(seed: int, measured: set[int], min_crf: int, max_crf: int) -> int | None:
    for offset in range(0, max(max_crf - min_crf, 0) + 1):
        for candidate in (seed + offset, seed - offset):
            if min_crf <= candidate <= max_crf and candidate not in measured:
                return candidate
    return None


def _failure_status(
        candidates: list[TargetSizeCandidate],
        ledger: StreamBudgetLedger,
        *,
        min_crf: int,
        max_crf: int,
) -> tuple[SearchStatus, str]:
    in_band = [candidate for candidate in candidates if candidate.within_sample_band and not candidate.violates_source_cap]
    if in_band and not any(candidate.quality_floor_met for candidate in in_band):
        return "quality_conflict", "target_band_violates_quality_floor"
    _, upper = _sample_bounds(ledger)
    size_reachable = [
        candidate for candidate in candidates
        if candidate.predicted_whole_episode_bytes is not None
        and candidate.predicted_whole_episode_bytes <= upper
        and not candidate.violates_source_cap
    ]
    if size_reachable and not any(candidate.quality_floor_met for candidate in size_reachable):
        return "quality_conflict", "target_requires_crossing_quality_floor"
    if not any(candidate.quality_floor_met for candidate in candidates):
        return "quality_conflict", "all_candidates_violate_quality_floor"
    shape = _curve_shape(candidates)
    if shape == "non_monotonic":
        return "needs_review", "non_monotonic_size_curve_exhausted"
    lower, upper = _sample_bounds(ledger)
    quality_safe = [candidate for candidate in candidates if candidate.quality_floor_met and not candidate.violates_source_cap]
    if quality_safe and all((candidate.predicted_whole_episode_bytes or 0) > upper for candidate in quality_safe):
        if any(math.isclose(candidate.crf, max_crf) for candidate in candidates):
            return "bound_exhausted", "smallest_quality_safe_candidate_over_target_band"
        return "needs_review", "bounded_search_exhausted_before_upper_bound"
    if quality_safe and all(
            candidate.predicted_whole_episode_bytes is not None and candidate.predicted_whole_episode_bytes < lower
            for candidate in quality_safe
    ):
        if any(math.isclose(candidate.crf, min_crf) for candidate in candidates):
            return "bound_exhausted", "largest_quality_safe_candidate_under_target_band"
        return "needs_review", "bounded_search_exhausted_before_lower_bound"
    return "needs_review", "bounded_search_exhausted"


def _failure_message(status: SearchStatus, reason: str, trace: dict[str, Any]) -> str:
    target = object_dict(trace.get("target"))
    best = object_dict(trace.get("best_reachable_candidate"))
    if status == "quality_conflict":
        return (
            "The approved target size conflicts with the configured quality floor "
            f"({reason}); target={target.get('total_target_bytes')} bytes, "
            f"best_reachable={best.get('predicted_whole_episode_bytes')} bytes."
        )
    if status == "bound_exhausted":
        return (
            "The approved target size was not reached before the configured search bound "
            f"({reason}); target={target.get('total_target_bytes')} bytes."
        )
    if status == "infeasible":
        return "The approved target size leaves no positive production video budget."
    return f"Target-size search needs review after bounded measurements ({reason})."


def _trace_payload(
        *,
        status: SearchStatus,
        reason: str,
        ledger: StreamBudgetLedger,
        candidates: list[TargetSizeCandidate],
        selected: TargetSizeCandidate | None,
        min_metric_score: float,
        metric_name: str,
        metric_target: float,
        min_crf: int,
        max_crf: int,
        configured_max_crf: int | None = None,
        transform_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    best = _best_reachable(candidates)
    lower, upper = _sample_bounds(ledger)
    resolved_configured_max_crf = max_crf if configured_max_crf is None else configured_max_crf
    measured_beyond_configured = any(candidate.crf > resolved_configured_max_crf for candidate in candidates)
    return {
        "schema_version": TARGET_SIZE_SEARCH_SCHEMA_VERSION,
        "status": status,
        "selection_reason": reason,
        "ledger": {
            "ledger_id": ledger.ledger_id,
            "source_id": ledger.source_id,
            "stream_plan_id": ledger.stream_plan.plan_id,
            "feasibility_status": ledger.feasibility_status,
        },
        "target": {
            "total_target_bytes": ledger.total_target_bytes,
            "target_video_bytes": ledger.remaining_video_bytes,
            "non_video_bytes": ledger.non_video_bytes,
            "sample_projection_tolerance_percent": _sample_tolerance_percent(ledger),
            "sample_lower_bound_bytes": lower,
            "sample_upper_bound_bytes": upper,
            "final_output_tolerance_percent": _final_tolerance_percent(ledger),
        },
        "source_cap": {
            "video_cap_bytes": ledger.source_cap_video_bytes,
            "video_cap_percent": ledger.source_cap_video_percent,
            "status": ledger.source_cap_status,
        },
        "quality_floor": {
            "metric": metric_name.upper(),
            "target": metric_target,
            "minimum": min_metric_score,
        },
        "crf_bounds": {
            "min_crf": min_crf,
            "configured_max_crf": resolved_configured_max_crf,
            "search_max_crf": max_crf,
            "max_crf": max_crf,
            "range_expanded": max_crf > resolved_configured_max_crf,
            "measured_beyond_configured": measured_beyond_configured,
            "selected_beyond_configured": selected is not None and selected.crf > resolved_configured_max_crf,
        },
        "transform_plan": transform_plan,
        "curve": {
            "shape": _curve_shape(candidates),
            "candidate_count": len(candidates),
            "max_candidates": MAX_TARGET_SIZE_CANDIDATES,
        },
        "retry_policy": {"max_final_output_retries": MAX_FINAL_OUTPUT_RETRIES},
        "candidates": [candidate.to_payload() for candidate in sorted(candidates, key=lambda item: item.attempt)],
        "selected_candidate": selected.to_payload() if selected is not None else None,
        "best_reachable_candidate": best.to_payload() if best is not None else None,
    }


def _best_reachable(candidates: list[TargetSizeCandidate]) -> TargetSizeCandidate | None:
    quality_safe = [candidate for candidate in candidates if candidate.quality_floor_met and not candidate.violates_source_cap]
    pool = quality_safe or candidates
    return min(pool, key=lambda candidate: (_candidate_distance(candidate), -candidate.metric_score), default=None)


def _curve_shape(candidates: list[TargetSizeCandidate]) -> CurveShape:
    measured = [candidate for candidate in sorted(candidates, key=lambda item: item.crf) if candidate.predicted_whole_episode_bytes is not None]
    if len(measured) <= 1:
        return "single_point"
    previous = measured[0].predicted_whole_episode_bytes or 0
    for candidate in measured[1:]:
        current = candidate.predicted_whole_episode_bytes or 0
        if current > previous:
            return "non_monotonic"
        previous = current
    return "monotonic"


def _payload_curve_shape(candidates: list[dict[str, Any]]) -> CurveShape:
    measured = [
        candidate
        for candidate in sorted(candidates, key=lambda item: float_value(item.get("crf")))
        if candidate.get("predicted_whole_episode_bytes") is not None
    ]
    if len(measured) <= 1:
        return "single_point"
    previous = int_value(measured[0].get("predicted_whole_episode_bytes"))
    for candidate in measured[1:]:
        current = int_value(candidate.get("predicted_whole_episode_bytes"))
        if current > previous:
            return "non_monotonic"
        previous = current
    return "monotonic"


def _candidate_distance(candidate: TargetSizeCandidate) -> int:
    return candidate.target_distance_bytes if candidate.target_distance_bytes is not None else 10 ** 30


def _source_cap_blocker(ledger: StreamBudgetLedger) -> StreamBudgetProjectionBlocker | None:
    blocker = stream_budget_projection_blocker(ledger)
    if blocker is None or blocker.code not in {
        "source_relative_cap_consumed_by_non_video_budget",
        "target_lower_bound_exceeds_source_relative_cap",
    }:
        return None
    return blocker


def _sample_bounds(ledger: StreamBudgetLedger) -> tuple[int, int]:
    target = ledger.total_target_bytes or 0
    return _bounds(target, _sample_tolerance_percent(ledger))


def _bounds(target: int, tolerance_percent: float) -> tuple[int, int]:
    tolerance = max(tolerance_percent, 0.0) / 100.0
    return int(round(target * (1.0 - tolerance))), int(round(target * (1.0 + tolerance)))


def _sample_tolerance_percent(ledger: StreamBudgetLedger) -> float:
    return float_value(object_dict(ledger.size_goal).get("sample_projection_tolerance_percent")) or 10.0


def _final_tolerance_percent(ledger: StreamBudgetLedger) -> float:
    return float_value(object_dict(ledger.size_goal).get("final_output_tolerance_percent")) or 5.0


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))
