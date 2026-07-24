from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mediaforce.encoding.quality import (
    QualitySearchError,
    QualitySearchResult,
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
class _QualityContext:
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
) -> QualitySearchResult:
    context = _quality_context(
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
        )

    while attempted_target >= context.min_metric_score:
        for max_crf in _max_crf_attempts(configured_max_crf):
            try:
                result = run_crf_search(
                    source_path,
                    source_codec=source_codec,
                    preferred_metric=context.metric_name,
                    metric_target=attempted_target,
                    preset=context.preset,
                    pixel_format=str(video_policy["pixel_format"]),
                    sample_every=str(video_policy["sample_every"]),
                    sample_duration=str(video_policy["sample_duration"]),
                    min_crf=int(video_policy["min_crf"]),
                    max_crf=max_crf,
                    max_encoded_percent=max_encoded_percent,
                    svt_params=context.svt_params,
                    video_filter=context.video_filter,
                    thorough=bool(video_policy.get("thorough", False)),
                    process_controller=process_controller,
                    host=context.host,
                    quality_temp_dir=quality_temp_dir,
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
    context = _quality_context(
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
        context: _QualityContext,
        video_policy: dict[str, Any],
        configured_max_crf: int,
        attempts: list[dict[str, Any]],
) -> dict[str, object]:
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
            (int(attempt["max_crf"]) for attempt in attempts),
            default=configured_max_crf,
        ),
        "max_encoded_percent": float(video_policy["max_encoded_percent"]),
        "attempt_count": len(attempts),
        "candidate_count": sum(int(attempt["candidate_count"]) for attempt in attempts),
        "attempts": attempts,
    }


def _quality_context(
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
) -> _QualityContext:
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
    return _QualityContext(
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
