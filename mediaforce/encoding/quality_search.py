from pathlib import Path
from typing import Any, Callable

from mediaforce.encoding.quality import QualitySearchError, QualitySearchResult
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.tuning.stream_budget import StreamBudgetLedger

MAX_CRF_SEARCH_CEILING = 63


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
) -> QualitySearchResult:
    quality_host = host
    if host_media_access_for_host(host) == "stream":
        quality_host = {**(host or {}), "mode": "local"}
    metric_name, default_target = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
    metric_target = float(video_policy.get(f"target_{metric_name.lower()}", default_target))
    min_target = float(video_policy.get(f"min_target_{metric_name.lower()}", metric_target))
    relax_step = float(
        video_policy.get(f"target_relax_step_{metric_name.lower()}", 1.0 if metric_name == "xpsnr" else 0.5)
    )
    svt_params = build_svt_params(video_policy)
    preset = effective_video_preset(video_policy, width=width, height=height)
    video_filter = build_video_filter(
        video_policy,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
    )
    attempted_target = metric_target
    last_error: Exception | None = None
    configured_max_crf = int(video_policy["max_crf"])
    max_encoded_percent = float(video_policy["max_encoded_percent"])
    if stream_budget_ledger is not None:
        stream_budget_ledger.require_positive_source_cap_video_budget()
        if stream_budget_ledger.source_cap_video_percent is not None:
            max_encoded_percent = stream_budget_ledger.source_cap_video_percent

    while attempted_target >= min_target:
        for max_crf in _max_crf_attempts(configured_max_crf):
            try:
                return run_crf_search(
                    source_path,
                    source_codec=source_codec,
                    preferred_metric=metric_name,
                    metric_target=attempted_target,
                    preset=preset,
                    pixel_format=str(video_policy["pixel_format"]),
                    sample_every=str(video_policy["sample_every"]),
                    sample_duration=str(video_policy["sample_duration"]),
                    min_crf=int(video_policy["min_crf"]),
                    max_crf=max_crf,
                    max_encoded_percent=max_encoded_percent,
                    svt_params=svt_params,
                    video_filter=video_filter,
                    thorough=bool(video_policy.get("thorough", False)),
                    process_controller=process_controller,
                    host=quality_host,
                    quality_temp_dir=quality_temp_dir,
                )
            except QualitySearchError as exc:
                last_error = exc
                if max_crf < MAX_CRF_SEARCH_CEILING and _failed_to_find_suitable_crf(exc):
                    continue
                break
        attempted_target = round(attempted_target - relax_step, 3)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Quality search did not run")


def _max_crf_attempts(configured_max_crf: int) -> list[int]:
    if configured_max_crf >= MAX_CRF_SEARCH_CEILING:
        return [MAX_CRF_SEARCH_CEILING]
    return [configured_max_crf, MAX_CRF_SEARCH_CEILING]


def _failed_to_find_suitable_crf(exc: QualitySearchError) -> bool:
    return "failed to find a suitable crf" in str(exc).lower()
