from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult
from mediaforce.tuning.stream_budget import (
    StreamBudgetLedger,
    StreamBudgetProjectionBlocker,
    stream_budget_projection_blocker,
)


TARGET_SIZE_SEARCH_SCHEMA_VERSION = 1
TARGET_SIZE_TRANSFORM_PLAN_SCHEMA_VERSION = 1
MAX_TARGET_SIZE_CANDIDATES = 6
MAX_FINAL_OUTPUT_RETRIES = 1

SearchStatus = Literal["selected", "infeasible", "quality_conflict", "needs_review"]
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

    @property
    def passed(self) -> bool:
        return self.status == "inside_target_band"

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
) -> QualitySearchResult:
    _validate_search_inputs(stream_budget_ledger)
    normalized_min_crf, normalized_max_crf = _normalized_crf_bounds(min_crf, max_crf)
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
            status="infeasible",
            reason=source_cap_blocker.code,
            ledger=stream_budget_ledger,
            candidates=[],
            selected=None,
            min_metric_score=min_metric_score,
            metric_name=metric_name,
            metric_target=metric_target,
            min_crf=normalized_min_crf,
            max_crf=normalized_max_crf,
            transform_plan=validated_transform_plan,
        )
        raise TargetSizeSearchError(
            source_cap_blocker.message,
            status="infeasible",
            trace=trace,
        )

    candidates: list[TargetSizeCandidate] = []
    measured_crfs: set[int] = set()
    seed_crf = _seed_crf(stream_budget_ledger, min_crf=normalized_min_crf, max_crf=normalized_max_crf)

    def measure(crf: int, role: str) -> None:
        normalized_crf = _clamp_int(crf, normalized_min_crf, normalized_max_crf)
        if normalized_crf in measured_crfs:
            return
        measured_crfs.add(normalized_crf)
        sample = run_sample_encode(
            source_path,
            source_codec=source_codec,
            preferred_metric=metric_name,
            crf=float(normalized_crf),
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
        selected = _select_candidate(candidates)
        if selected is not None:
            break
        next_crf = _next_crf(candidates, measured_crfs, normalized_min_crf, normalized_max_crf, stream_budget_ledger)
        if next_crf is None:
            break
        measure(next_crf, "refine")

    selected = _select_candidate(candidates)
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
            transform_plan=validated_transform_plan,
        )
        return QualitySearchResult(
            crf=selected.crf,
            metric=selected.metric,
            target=metric_target,
            score=selected.metric_score,
            stdout="target-size-search",
            target_size_trace=trace,
        )

    status, reason = _failure_status(candidates, stream_budget_ledger)
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
        transform_plan=validated_transform_plan,
    )
    raise TargetSizeSearchError(_failure_message(status, reason, trace), status=status, trace=trace)


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
    retry_allowed = status in {"over_target", "under_target"} and retry_count < MAX_FINAL_OUTPUT_RETRIES
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
            else None if status == "inside_target_band" else "The final-size retry budget is exhausted."
        ),
    )


def retry_quality_result_for_final_miss(
        quality: QualitySearchResult,
        verification: FinalSizeVerification,
) -> QualitySearchResult | None:
    trace = object_dict(quality.target_size_trace)
    if not trace or not verification.retry_allowed:
        return None
    selected = object_dict(trace.get("selected_candidate"))
    selected_crf = float_value(selected.get("crf"))
    if selected_crf <= 0:
        return None
    candidates = [object_dict(candidate) for candidate in trace.get("candidates", []) if isinstance(candidate, dict)]
    if verification.status == "over_target":
        eligible = [
            candidate for candidate in candidates
            if float_value(candidate.get("crf")) > selected_crf
            and bool(candidate.get("quality_floor_met"))
            and not bool(candidate.get("violates_source_cap"))
        ]
        eligible.sort(key=lambda candidate: (float_value(candidate.get("crf")), _distance(candidate)))
    elif verification.status == "under_target":
        eligible = [
            candidate for candidate in candidates
            if 0 < float_value(candidate.get("crf")) < selected_crf
            and bool(candidate.get("quality_floor_met"))
            and not bool(candidate.get("violates_source_cap"))
        ]
        eligible.sort(key=lambda candidate: (-float_value(candidate.get("crf")), _distance(candidate)))
    else:
        return None
    if not eligible:
        return None
    candidate = eligible[0]
    retry_trace = dict(trace)
    retry_trace["selected_candidate"] = candidate
    retry_trace["status"] = "selected"
    retry_trace["selection_reason"] = "bounded_final_size_retry_candidate"
    retry_trace["final_retry_from_candidate"] = selected
    return QualitySearchResult(
        crf=float_value(candidate.get("crf")),
        metric=str(candidate.get("metric") or quality.metric),
        target=float_value(candidate.get("metric_target")) or quality.target,
        score=float_value(candidate.get("metric_score")),
        stdout=quality.stdout,
        target_size_trace=retry_trace,
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
    payload["final_output"] = {
        **verification.to_payload(),
        "retry_count": retry_count,
        "max_retries": MAX_FINAL_OUTPUT_RETRIES,
    }
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


def _select_candidate(candidates: list[TargetSizeCandidate]) -> TargetSizeCandidate | None:
    eligible = [
        candidate for candidate in candidates
        if candidate.within_sample_band and candidate.quality_floor_met and not candidate.violates_source_cap
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            candidate.target_distance_bytes if candidate.target_distance_bytes is not None else 10 ** 30,
            -candidate.metric_score,
            candidate.crf,
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
            candidate = _nearest_unmeasured(midpoint, measured_crfs, min_crf, max_crf)
            if candidate is not None:
                return candidate
    if above:
        return _nearest_unmeasured(max_crf, measured_crfs, min_crf, max_crf)
    if below:
        return _nearest_unmeasured(min_crf, measured_crfs, min_crf, max_crf)
    closest = min(candidates, key=lambda candidate: _candidate_distance(candidate), default=None)
    if closest is None:
        return None
    return _nearest_unmeasured(int(round(closest.crf)), measured_crfs, min_crf, max_crf)


def _nearest_unmeasured(seed: int, measured: set[int], min_crf: int, max_crf: int) -> int | None:
    for offset in range(0, max(max_crf - min_crf, 0) + 1):
        for candidate in (seed + offset, seed - offset):
            if min_crf <= candidate <= max_crf and candidate not in measured:
                return candidate
    return None


def _failure_status(candidates: list[TargetSizeCandidate], ledger: StreamBudgetLedger) -> tuple[SearchStatus, str]:
    in_band = [candidate for candidate in candidates if candidate.within_sample_band and not candidate.violates_source_cap]
    if in_band and not any(candidate.quality_floor_met for candidate in in_band):
        return "quality_conflict", "target_band_violates_quality_floor"
    if not any(candidate.quality_floor_met for candidate in candidates):
        return "quality_conflict", "all_candidates_violate_quality_floor"
    shape = _curve_shape(candidates)
    if shape == "non_monotonic":
        return "needs_review", "non_monotonic_size_curve_exhausted"
    lower, upper = _sample_bounds(ledger)
    quality_safe = [candidate for candidate in candidates if candidate.quality_floor_met and not candidate.violates_source_cap]
    if quality_safe and all((candidate.predicted_whole_episode_bytes or 0) > upper for candidate in quality_safe):
        return "infeasible", "smallest_quality_safe_candidate_over_target_band"
    if quality_safe and all(
            candidate.predicted_whole_episode_bytes is not None and candidate.predicted_whole_episode_bytes < lower
            for candidate in quality_safe
    ):
        return "infeasible", "largest_quality_safe_candidate_under_target_band"
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
    if status == "infeasible":
        return (
            "The approved target size is not reachable within the approved CRF range and caps "
            f"({reason}); target={target.get('total_target_bytes')} bytes."
        )
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
        transform_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    best = _best_reachable(candidates)
    lower, upper = _sample_bounds(ledger)
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
        "crf_bounds": {"min_crf": min_crf, "max_crf": max_crf},
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


def _candidate_distance(candidate: TargetSizeCandidate) -> int:
    return candidate.target_distance_bytes if candidate.target_distance_bytes is not None else 10 ** 30


def _distance(candidate: dict[str, Any]) -> float:
    distance = float_value(candidate.get("target_distance_bytes"))
    return distance if distance > 0 else 10 ** 30


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
