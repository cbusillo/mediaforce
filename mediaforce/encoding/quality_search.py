from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Any, Callable

from mediaforce.encoding.quality import (
    QualitySearchError,
    QualitySearchResult,
    QualitySearchWarmStart,
    QualityTempCleanupError,
    QualityTempSetupError,
    SampleEncodeError,
    SampleEncodeResult,
    parse_quality_search_measurements,
)
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.target_size_search import build_target_size_transform_plan, search_target_size
from mediaforce.tuning.stream_budget import StreamBudgetLedger

MAX_CRF_SEARCH_CEILING = 63
QUALITY_SEARCH_TRACE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class QualitySearchPlan:
    host: dict[str, Any] | None
    metric_name: str
    metric_target: float
    min_metric_score: float
    relax_step: float
    svt_params: list[str]
    preset: int
    video_filter: str | None


def search_quality(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
        process_controller: Any = None,
        host: dict[str, Any] | None = None,
        quality_temp_dir: Path | None = None,
        stream_budget_ledger: StreamBudgetLedger | None = None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
        select_quality_metric: Callable[[str], tuple[str, float]],
        build_svt_params: Callable[[dict[str, Any]], list[str]],
        effective_video_preset: Callable[..., int],
        run_crf_search: Callable[..., QualitySearchResult],
        run_sample_encode: Callable[..., SampleEncodeResult] | None = None,
        resolved_plan: QualitySearchPlan | None = None,
        warm_start: QualitySearchWarmStart | None = None,
        expected_search_signature_id: str | None = None,
        candidate_progress_callback: Callable[[int, int], None] | None = None,
) -> QualitySearchResult:
    context = resolved_plan or resolve_quality_search_plan(
        video_policy,
        host=host,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        host_media_access_for_host=host_media_access_for_host,
        select_quality_metric=select_quality_metric,
        build_svt_params=build_svt_params,
        effective_video_preset=effective_video_preset,
    )
    attempted_target = context.metric_target
    last_error: Exception | None = None
    attempts: list[dict[str, Any]] = []
    configured_max_crf = int(video_policy["max_crf"])
    target_search_max_crf = int(video_policy.get("target_search_max_crf", configured_max_crf))
    max_encoded_percent = float(video_policy["max_encoded_percent"])
    if stream_budget_ledger is not None:
        if run_sample_encode is None:
            raise RuntimeError("Target-size search requires sample-encode measurement support")
        return search_target_size(
            source_path,
            video_policy,
            source_codec=source_codec,
            metric_name=context.metric_name,
            metric_target=context.metric_target,
            min_metric_score=context.min_metric_score,
            preset=context.preset,
            pixel_format=str(video_policy["pixel_format"]),
            sample_every=str(video_policy["sample_every"]),
            sample_duration=str(video_policy["sample_duration"]),
            min_crf=int(video_policy["min_crf"]),
            max_crf=configured_max_crf,
            svt_params=context.svt_params,
            video_filter=context.video_filter,
            stream_budget_ledger=stream_budget_ledger,
            transform_plan=_transform_plan_payload(cadence_decision, context.video_filter),
            process_controller=process_controller,
            host=context.host,
            quality_temp_dir=quality_temp_dir,
            run_sample_encode=run_sample_encode,
            search_max_crf=target_search_max_crf,
            warm_start=warm_start,
            expected_search_signature_id=expected_search_signature_id,
            candidate_progress_callback=candidate_progress_callback,
        )

    def run_search(*, metric_target: float, min_crf: int, max_crf: int) -> QualitySearchResult:
        return run_crf_search(
            source_path,
            source_codec=source_codec,
            preferred_metric=context.metric_name,
            metric_target=metric_target,
            preset=context.preset,
            pixel_format=str(video_policy["pixel_format"]),
            sample_every=str(video_policy["sample_every"]),
            sample_duration=str(video_policy["sample_duration"]),
            min_crf=min_crf,
            max_crf=max_crf,
            max_encoded_percent=max_encoded_percent,
            svt_params=context.svt_params,
            video_filter=context.video_filter,
            thorough=bool(video_policy.get("thorough", False)),
            process_controller=process_controller,
            host=context.host,
            quality_temp_dir=quality_temp_dir,
        )

    def run_sample(crf: int) -> SampleEncodeResult:
        if run_sample_encode is None:
            raise RuntimeError("Quality warm start requires sample-encode measurement support")
        return run_sample_encode(
            source_path,
            source_codec=source_codec,
            preferred_metric=context.metric_name,
            crf=float(crf),
            preset=context.preset,
            pixel_format=str(video_policy["pixel_format"]),
            sample_every=str(video_policy["sample_every"]),
            sample_duration=str(video_policy["sample_duration"]),
            svt_params=context.svt_params,
            video_filter=context.video_filter,
            process_controller=process_controller,
            host=context.host,
            quality_temp_dir=quality_temp_dir,
        )

    warm_result, warm_trace = _try_quality_warm_start(
        warm_start,
        context=context,
        video_policy=video_policy,
        configured_max_crf=configured_max_crf,
        max_encoded_percent=max_encoded_percent,
        run_sample=run_sample if run_sample_encode is not None else None,
        expected_search_signature_id=expected_search_signature_id,
    )
    if warm_result is not None:
        return warm_result

    while attempted_target >= context.min_metric_score:
        for max_crf in _max_crf_attempts(configured_max_crf):
            try:
                result = run_search(
                    metric_target=attempted_target,
                    min_crf=int(video_policy["min_crf"]),
                    max_crf=max_crf,
                )
                attempts.append(
                    _quality_search_attempt(
                        attempt=len(attempts) + 1,
                        metric_target=attempted_target,
                        max_crf=max_crf,
                        status="selected",
                        output=result.stdout,
                    )
                )
                result.quality_search_trace = _quality_search_trace(
                    status="selected",
                    context=context,
                    video_policy=video_policy,
                    configured_max_crf=configured_max_crf,
                    attempts=attempts,
                    warm_start=warm_trace,
                )
                return result
            except QualitySearchError as exc:
                last_error = exc
                attempts.append(
                    _quality_search_attempt(
                        attempt=len(attempts) + 1,
                        metric_target=attempted_target,
                        max_crf=max_crf,
                        status="no_selection",
                        output=str(exc),
                    )
                )
                if max_crf < MAX_CRF_SEARCH_CEILING and _failed_to_find_suitable_crf(exc):
                    continue
                break
        attempted_target = round(attempted_target - context.relax_step, 3)

    if last_error is not None:
        if isinstance(last_error, QualitySearchError):
            last_error.quality_search_trace = _quality_search_trace(
                status="deterministic_search_failure",
                context=context,
                video_policy=video_policy,
                configured_max_crf=configured_max_crf,
                attempts=attempts,
                warm_start=warm_trace,
            )
        raise last_error
    raise RuntimeError("Quality search did not run")


def measure_quality_candidate(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        crf: float,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
        process_controller: Any = None,
        host: dict[str, Any] | None = None,
        quality_temp_dir: Path | None = None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
        select_quality_metric: Callable[[str], tuple[str, float]],
        build_svt_params: Callable[[dict[str, Any]], list[str]],
        effective_video_preset: Callable[..., int],
        run_sample_encode: Callable[..., SampleEncodeResult],
) -> SampleEncodeResult:
    context = resolve_quality_search_plan(
        video_policy,
        host=host,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        host_media_access_for_host=host_media_access_for_host,
        select_quality_metric=select_quality_metric,
        build_svt_params=build_svt_params,
        effective_video_preset=effective_video_preset,
    )
    return run_sample_encode(
        source_path,
        source_codec=source_codec,
        preferred_metric=context.metric_name,
        crf=crf,
        preset=context.preset,
        pixel_format=str(video_policy["pixel_format"]),
        sample_every=str(video_policy["sample_every"]),
        sample_duration=str(video_policy["sample_duration"]),
        svt_params=context.svt_params,
        video_filter=context.video_filter,
        process_controller=process_controller,
        host=context.host,
        quality_temp_dir=quality_temp_dir,
    )


def _try_quality_warm_start(
        warm_start: QualitySearchWarmStart | None,
        *,
        context: QualitySearchPlan,
        video_policy: dict[str, Any],
        configured_max_crf: int,
        max_encoded_percent: float,
        run_sample: Callable[[int], SampleEncodeResult] | None,
        expected_search_signature_id: str | None,
) -> tuple[QualitySearchResult | None, dict[str, Any] | None]:
    if warm_start is None:
        return None, None
    configured_min_crf = int(video_policy["min_crf"])
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
        return None, _warm_start_trace(
            warm_start,
            status="guard_rejected",
            attempted=False,
            duration_seconds=0.0,
            candidate_count=0,
            fallback_reason="invalid_hint_contract",
        )
    if run_sample is None:
        return None, _warm_start_trace(
            warm_start,
            status="guard_rejected",
            attempted=False,
            duration_seconds=0.0,
            candidate_count=0,
            fallback_reason="measurement_unavailable",
        )
    started = time.monotonic()
    try:
        sample = run_sample(candidate_crf)
    except QualityTempSetupError as exc:
        return None, _warm_start_trace(
            warm_start,
            status="probe_error_fallback",
            attempted=True,
            duration_seconds=max(time.monotonic() - started, 0.0),
            candidate_count=0,
            fallback_reason="probe_error",
            error_type=type(exc).__name__,
        )
    except (QualityTempCleanupError, SampleEncodeError, ValueError) as exc:
        return None, _warm_start_trace(
            warm_start,
            status="probe_error_fallback",
            attempted=True,
            duration_seconds=max(time.monotonic() - started, 0.0),
            candidate_count=1,
            fallback_reason="probe_error",
            error_type=type(exc).__name__,
        )
    candidate = {
        "crf": float(candidate_crf),
        "metric": sample.metric,
        "metric_score": sample.score,
        "predicted_encode_percent": sample.predicted_encode_percent,
        "predicted_encode_size_bytes": sample.predicted_encode_size_bytes,
    }
    rejection_reason = _quality_warm_start_rejection_reason(
        sample,
        metric_name=context.metric_name,
        metric_target=context.metric_target,
        max_encoded_percent=max_encoded_percent,
    )
    if rejection_reason is not None:
        return None, _warm_start_trace(
            warm_start,
            status="rejected_fallback",
            attempted=True,
            duration_seconds=max(time.monotonic() - started, 0.0),
            candidate_count=1,
            fallback_reason=rejection_reason,
            candidate=candidate,
        )
    attempt = {
        "attempt": 1,
        "metric_target": context.metric_target,
        "max_crf": candidate_crf,
        "status": "selected",
        "candidate_count": 1,
        "candidates": [candidate],
    }
    warm_trace = _warm_start_trace(
        warm_start,
        status="accepted",
        attempted=True,
        duration_seconds=max(time.monotonic() - started, 0.0),
        candidate_count=1,
        candidate=candidate,
    )
    result = QualitySearchResult(
        crf=float(candidate_crf),
        metric=sample.metric,
        target=context.metric_target,
        score=sample.score,
        stdout=sample.stdout,
    )
    result.quality_search_trace = _quality_search_trace(
        status="selected",
        context=context,
        video_policy=video_policy,
        configured_max_crf=configured_max_crf,
        attempts=[attempt],
        warm_start=warm_trace,
    )
    return result, warm_trace


def _quality_warm_start_rejection_reason(
        sample: SampleEncodeResult,
        *,
        metric_name: str,
        metric_target: float,
        max_encoded_percent: float,
) -> str | None:
    if sample.metric.upper() != metric_name.upper():
        return "metric_mismatch"
    if not math.isfinite(sample.score) or not math.isfinite(sample.predicted_encode_percent):
        return "invalid_measurement"
    if sample.score < metric_target:
        return "quality_target_miss"
    if sample.predicted_encode_percent > max_encoded_percent:
        return "size_cap_miss"
    return None


def _warm_start_trace(
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


def _max_crf_attempts(configured_max_crf: int) -> list[int]:
    if configured_max_crf >= MAX_CRF_SEARCH_CEILING:
        return [MAX_CRF_SEARCH_CEILING]
    return [configured_max_crf, MAX_CRF_SEARCH_CEILING]


def _failed_to_find_suitable_crf(exc: QualitySearchError) -> bool:
    return "failed to find a suitable crf" in str(exc).lower()


def _quality_search_attempt(
        *,
        attempt: int,
        metric_target: float,
        max_crf: int,
        status: str,
        output: str,
) -> dict[str, Any]:
    candidates = [
        {
            "crf": measurement.crf,
            "metric": measurement.metric,
            "metric_score": measurement.score,
            "predicted_encode_percent": measurement.predicted_encode_percent,
            "predicted_encode_size_bytes": measurement.predicted_encode_size_bytes,
        }
        for measurement in parse_quality_search_measurements(output)
    ]
    return {
        "attempt": attempt,
        "metric_target": metric_target,
        "max_crf": max_crf,
        "status": status,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _quality_search_trace(
        *,
        status: str,
        context: QualitySearchPlan,
        video_policy: dict[str, Any],
        configured_max_crf: int,
        attempts: list[dict[str, Any]],
        warm_start: dict[str, Any] | None = None,
) -> dict[str, object]:
    baseline_candidate_count = sum(int(attempt["candidate_count"]) for attempt in attempts)
    warm_payload = dict(warm_start) if warm_start is not None else None
    candidate_count = baseline_candidate_count
    if warm_payload is not None:
        if warm_payload.get("status") == "accepted":
            warm_payload["baseline_candidate_count"] = 0
        else:
            warm_payload["baseline_candidate_count"] = baseline_candidate_count
            candidate_count += int(warm_payload.get("candidate_count") or 0)
        warm_payload["total_candidate_count"] = candidate_count
    return {
        "schema_version": QUALITY_SEARCH_TRACE_SCHEMA_VERSION,
        "objective": "quality",
        "status": status,
        "metric": context.metric_name.upper(),
        "initial_target": context.metric_target,
        "minimum_quality_score": context.min_metric_score,
        "target_relax_step": context.relax_step,
        "configured_min_crf": int(video_policy["min_crf"]),
        "configured_max_crf": configured_max_crf,
        "search_max_crf": max(
            configured_max_crf,
            *(int(attempt["max_crf"]) for attempt in attempts),
        ),
        "max_encoded_percent": float(video_policy["max_encoded_percent"]),
        "attempt_count": len(attempts),
        "candidate_count": candidate_count,
        "attempts": attempts,
        "warm_start": warm_payload,
    }


def resolve_quality_search_plan(
        video_policy: dict[str, Any],
        *,
        host: dict[str, Any] | None,
        width: int | None,
        height: int | None,
        detected_crop: str | None,
        cadence_decision: dict[str, Any] | None,
        cadence_evidence: dict[str, Any] | None,
        cadence_source_fingerprint: str | None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
        select_quality_metric: Callable[[str], tuple[str, float]],
        build_svt_params: Callable[[dict[str, Any]], list[str]],
        effective_video_preset: Callable[..., int],
) -> QualitySearchPlan:
    quality_host = host
    if host_media_access_for_host(host) == "stream":
        quality_host = {**(host or {}), "mode": "local"}
    metric_name, default_target = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
    metric_target = float(video_policy.get(f"target_{metric_name.lower()}", default_target))
    min_metric_score = float(video_policy.get(f"min_target_{metric_name.lower()}", metric_target))
    relax_step = float(
        video_policy.get(
            f"target_relax_step_{metric_name.lower()}",
            1.0 if metric_name == "xpsnr" else 0.5,
        )
    )
    return QualitySearchPlan(
        host=quality_host,
        metric_name=metric_name,
        metric_target=metric_target,
        min_metric_score=min_metric_score,
        relax_step=relax_step,
        svt_params=build_svt_params(video_policy),
        preset=effective_video_preset(video_policy, width=width, height=height),
        video_filter=build_video_filter(
            video_policy,
            width=width,
            height=height,
            detected_crop=detected_crop,
            cadence_decision=cadence_decision,
            cadence_evidence=cadence_evidence,
            cadence_source_fingerprint=cadence_source_fingerprint,
        ),
    )


def _transform_plan_payload(cadence_decision: dict[str, Any] | None, video_filter: str | None) -> dict[str, Any]:
    cadence_payload = object_dict(cadence_decision)
    return build_target_size_transform_plan(
        cadence_evidence_id=cadence_payload.get("evidence_id"),
        cadence_class=cadence_payload.get("classification"),
        cadence_transform=cadence_payload.get("transform"),
        video_filter=video_filter,
    )
