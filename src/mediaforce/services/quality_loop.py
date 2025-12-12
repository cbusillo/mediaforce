from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from statistics import median
from typing import Callable, Optional

from sqlmodel import Session

from mediaforce.db import ProfileEvaluation, ProfileSettingsSource, VmafSample, now_iso


@dataclass(frozen=True)
class VmafThresholds:
    min_vmaf: float
    median_vmaf: float
    max_vmaf: Optional[float] = None


@dataclass(frozen=True)
class VmafPlanItem:
    kind: str
    start_sec: float
    duration_sec: float
    weight: float


@dataclass(frozen=True)
class VmafSampleResult:
    kind: str
    start_sec: float
    duration_sec: float
    weight: float
    vmaf: float


@dataclass(frozen=True)
class VmafSummary:
    weighted: Optional[float]
    median: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]


@dataclass(frozen=True)
class QualityLoopResult:
    evaluation_id: int
    selected_profile: str
    initial_profile: str
    decision: str
    status: str
    note: Optional[str]
    thresholds: VmafThresholds
    summary: VmafSummary


def extract_thresholds(settings_source: Optional[ProfileSettingsSource]) -> VmafThresholds:
    default_min = 82.0
    default_median = 92.0
    default_max: Optional[float] = None
    if not settings_source:
        return VmafThresholds(min_vmaf=default_min, median_vmaf=default_median, max_vmaf=default_max)
    try:
        data = json.loads(settings_source.payload)
        thresholds = data.get("thresholds", {})
        max_raw = thresholds.get("max")
        max_val = float(max_raw) if max_raw is not None else None
        return VmafThresholds(
            min_vmaf=float(thresholds.get("min", default_min)),
            median_vmaf=float(thresholds.get("median", default_median)),
            max_vmaf=max_val,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return VmafThresholds(min_vmaf=default_min, median_vmaf=default_median, max_vmaf=default_max)


def build_motion_weighted_plan(
    *,
    source_path: pathlib.Path,
    duration_seconds: float,
    sample_length: float,
    motion_aware: bool,
    window_bitrate: Optional[Callable[[pathlib.Path, float, float], Optional[float]]] = None,
) -> list[VmafPlanItem]:
    """Return a 3-sample (short/mid/motion) plan with weights.

    motion_aware requires window_bitrate callable (ffprobe-based) to choose a
    high-motion window; otherwise it falls back to 75%.
    """

    if duration_seconds <= 0 or sample_length <= 0:
        return []

    def clamp_ts(ts: float) -> float:
        return max(0.0, min(ts, max(0.0, duration_seconds - sample_length)))

    short_ts = clamp_ts(duration_seconds * 0.15)
    mid_ts = clamp_ts(duration_seconds * 0.50)
    motion_ts = clamp_ts(duration_seconds * 0.75)
    motion_weight = 1.5

    if motion_aware and window_bitrate is not None:
        candidates: list[tuple[float, float]] = []
        steps = max(9, 8)
        for i in range(1, steps + 1):
            frac = i / (steps + 1)
            ts = clamp_ts(duration_seconds * frac)
            br = window_bitrate(source_path, ts, 5.0)
            if br is not None:
                candidates.append((float(br), ts))

        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            best_br, best_ts = candidates[0]
            avg_br = sum(b for b, _ in candidates) / len(candidates)
            motion_ts = best_ts
            if avg_br > 0:
                motion_weight = max(1.25, min(5.0, best_br / avg_br))

    return [
        VmafPlanItem(kind="short", start_sec=short_ts, duration_sec=sample_length, weight=1.0),
        VmafPlanItem(kind="mid", start_sec=mid_ts, duration_sec=sample_length, weight=1.0),
        VmafPlanItem(kind="motion", start_sec=motion_ts, duration_sec=sample_length, weight=motion_weight),
    ]


def summarize_vmaf_samples(samples: list[VmafSampleResult]) -> VmafSummary:
    if not samples:
        return VmafSummary(weighted=None, median=None, minimum=None, maximum=None)

    scores = [s.vmaf for s in samples]
    weights = [s.weight for s in samples]
    weight_total = sum(weights)
    weighted = None
    if weight_total > 0:
        weighted = sum(s.vmaf * s.weight for s in samples) / weight_total
    return VmafSummary(
        weighted=weighted,
        median=float(median(scores)) if scores else None,
        minimum=min(scores) if scores else None,
        maximum=max(scores) if scores else None,
    )


def _tier_rank(tier: str) -> Optional[int]:
    order = ["poor", "mediocre", "good", "pristine"]
    try:
        return order.index(tier)
    except ValueError:
        return None


def _tier_more_aggressive(tier: str) -> str:
    rank = _tier_rank(tier)
    if rank is None:
        return tier
    order = ["poor", "mediocre", "good", "pristine"]
    return order[max(0, rank - 1)]


def _tier_less_aggressive(tier: str) -> str:
    rank = _tier_rank(tier)
    if rank is None:
        return tier
    order = ["poor", "mediocre", "good", "pristine"]
    return order[min(len(order) - 1, rank + 1)]


def choose_profile(
    *,
    initial_profile: str,
    summary: VmafSummary,
    thresholds: VmafThresholds,
) -> tuple[str, str, str, Optional[str]]:
    """Return (selected_profile, decision, status, note)."""

    if summary.minimum is not None and summary.minimum < thresholds.min_vmaf:
        selected = _tier_less_aggressive(initial_profile)
        note = f"min_vmaf_below_threshold ({summary.minimum:.1f} < {thresholds.min_vmaf:.1f})"
        decision = "bump" if selected != initial_profile else "keep"
        return selected, decision, "done", note

    if summary.weighted is not None and summary.weighted < thresholds.median_vmaf:
        selected = _tier_less_aggressive(initial_profile)
        note = f"weighted_vmaf_below_threshold ({summary.weighted:.1f} < {thresholds.median_vmaf:.1f})"
        decision = "bump" if selected != initial_profile else "keep"
        return selected, decision, "done", note

    # If very high quality, try a more aggressive profile.
    high_threshold = thresholds.max_vmaf if thresholds.max_vmaf is not None else 95.0
    if summary.weighted is not None and summary.weighted >= high_threshold:
        selected = _tier_more_aggressive(initial_profile)
        note = f"high_weighted_vmaf ({summary.weighted:.1f} >= {high_threshold:.1f})"
        decision = "bump" if selected != initial_profile else "keep"
        return selected, decision, "done", note

    return initial_profile, "keep", "done", None


def start_profile_evaluation(
    session: Session,
    *,
    media_id: int,
    initial_profile: str,
    thresholds: VmafThresholds,
    settings_source: Optional[ProfileSettingsSource],
    sample_length: float,
    sample_strategy: str = "3x8s_motion",
    sample_count: int = 3,
) -> ProfileEvaluation:
    now_str = now_iso()
    ev = ProfileEvaluation(
        media_id=media_id,
        encode_result_id=None,
        settings_source_id=settings_source.id if settings_source else None,
        selected_profile=initial_profile,
        sample_strategy=sample_strategy,
        sample_count=sample_count,
        sample_length=sample_length,
        threshold_min=thresholds.min_vmaf,
        threshold_median=thresholds.median_vmaf,
        threshold_max=thresholds.max_vmaf,
        decision="keep",
        status="running",
        created_at=now_str,
        updated_at=now_str,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev


def finalize_profile_evaluation(
    session: Session,
    *,
    evaluation_id: int,
    initial_profile: str,
    thresholds: VmafThresholds,
    settings_source: Optional[ProfileSettingsSource],
    sample_results: list[VmafSampleResult],
    target_height: Optional[int] = None,
    target_height_reason: Optional[str] = None,
) -> QualityLoopResult:
    ev = session.get(ProfileEvaluation, evaluation_id)
    if not ev:
        raise ValueError("evaluation_not_found")

    now_str = now_iso()
    if not sample_results:
        ev.status = "failed"
        ev.decision = "fail"
        ev.note = "VMAF sampling failed"
        ev.reason_json = json.dumps(
            {
                "version": 1,
                "status": ev.status,
                "decision": ev.decision,
                "note": ev.note,
                "initial_profile": initial_profile,
                "selected_profile": initial_profile,
                "thresholds": {
                    "min": thresholds.min_vmaf,
                    "median": thresholds.median_vmaf,
                    "max": thresholds.max_vmaf,
                },
                "target_height": target_height,
                "target_height_reason": target_height_reason,
                "settings_source_id": settings_source.id if settings_source else None,
                "created_at": now_str,
            },
            ensure_ascii=False,
        )
        ev.updated_at = now_str
        session.add(ev)
        session.commit()
        return QualityLoopResult(
            evaluation_id=ev.id or 0,
            selected_profile=initial_profile,
            initial_profile=initial_profile,
            decision=ev.decision,
            status=ev.status,
            note=ev.note,
            thresholds=thresholds,
            summary=VmafSummary(weighted=None, median=None, minimum=None, maximum=None),
        )

    summary = summarize_vmaf_samples(sample_results)
    selected_profile, decision, status, note = choose_profile(
        initial_profile=initial_profile,
        summary=summary,
        thresholds=thresholds,
    )

    for sample in sample_results:
        session.add(
            VmafSample(
                evaluation_id=ev.id,  # type: ignore[arg-type]
                sample_kind=sample.kind,
                start_sec=sample.start_sec,
                duration_sec=sample.duration_sec,
                vmaf=sample.vmaf,
                weight=sample.weight,
                created_at=now_str,
            )
        )
    session.commit()

    reason_payload = {
        "version": 1,
        "initial_profile": initial_profile,
        "selected_profile": selected_profile,
        "decision": decision,
        "status": status,
        "thresholds": {
            "min": thresholds.min_vmaf,
            "median": thresholds.median_vmaf,
            "max": thresholds.max_vmaf,
        },
        "summary": {
            "weighted": summary.weighted,
            "median": summary.median,
            "min": summary.minimum,
            "max": summary.maximum,
        },
        "samples": [
            {
                "kind": s.kind,
                "start_sec": s.start_sec,
                "duration_sec": s.duration_sec,
                "weight": s.weight,
                "vmaf": s.vmaf,
            }
            for s in sample_results
        ],
        "target_height": target_height,
        "target_height_reason": target_height_reason,
        "settings_source_id": settings_source.id if settings_source else None,
        "settings_source_checksum": settings_source.checksum if settings_source else None,
        "created_at": now_str,
    }

    ev.selected_profile = selected_profile
    ev.weighted_vmaf = summary.weighted
    ev.median_vmaf = summary.median
    ev.min_vmaf = summary.minimum
    ev.max_vmaf = summary.maximum
    ev.decision = decision
    ev.status = status
    ev.note = note
    ev.reason_json = json.dumps(reason_payload, ensure_ascii=False)
    ev.updated_at = now_str
    session.add(ev)
    session.commit()

    return QualityLoopResult(
        evaluation_id=ev.id or 0,
        selected_profile=selected_profile,
        initial_profile=initial_profile,
        decision=decision,
        status=status,
        note=note,
        thresholds=thresholds,
        summary=summary,
    )


def run_profile_quality_loop(
    session: Session,
    *,
    media_id: int,
    source_path: pathlib.Path,
    duration_seconds: float,
    initial_profile: str,
    settings_source: Optional[ProfileSettingsSource],
    sample_length: float,
    motion_aware: bool,
    measure_vmaf: Callable[[VmafPlanItem], Optional[float]],
    window_bitrate: Optional[Callable[[pathlib.Path, float, float], Optional[float]]] = None,
    target_height: Optional[int] = None,
    target_height_reason: Optional[str] = None,
) -> QualityLoopResult:
    thresholds = extract_thresholds(settings_source)
    now_str = now_iso()
    ev = start_profile_evaluation(
        session,
        media_id=media_id,
        initial_profile=initial_profile,
        thresholds=thresholds,
        settings_source=settings_source,
        sample_length=sample_length,
        sample_strategy="3x8s_motion",
        sample_count=3,
    )

    plan = build_motion_weighted_plan(
        source_path=source_path,
        duration_seconds=duration_seconds,
        sample_length=sample_length,
        motion_aware=motion_aware,
        window_bitrate=window_bitrate,
    )

    if not plan:
        ev.status = "failed"
        ev.decision = "fail"
        ev.note = "No duration available for sampling"
        ev.reason_json = json.dumps(
            {
                "version": 1,
                "status": ev.status,
                "decision": ev.decision,
                "note": ev.note,
                "initial_profile": initial_profile,
                "selected_profile": initial_profile,
                "thresholds": {
                    "min": thresholds.min_vmaf,
                    "median": thresholds.median_vmaf,
                    "max": thresholds.max_vmaf,
                },
                "target_height": target_height,
                "target_height_reason": target_height_reason,
                "settings_source_id": settings_source.id if settings_source else None,
                "created_at": now_str,
            },
            ensure_ascii=False,
        )
        ev.updated_at = now_iso()
        session.add(ev)
        session.commit()
        return QualityLoopResult(
            evaluation_id=ev.id or 0,
            selected_profile=initial_profile,
            initial_profile=initial_profile,
            decision=ev.decision,
            status=ev.status,
            note=ev.note,
            thresholds=thresholds,
            summary=VmafSummary(weighted=None, median=None, minimum=None, maximum=None),
        )

    sample_results: list[VmafSampleResult] = []
    for item in plan:
        score = measure_vmaf(item)
        if score is None:
            continue
        sample_results.append(
            VmafSampleResult(
                kind=item.kind,
                start_sec=item.start_sec,
                duration_sec=item.duration_sec,
                weight=item.weight,
                vmaf=float(score),
            )
        )

    return finalize_profile_evaluation(
        session,
        evaluation_id=ev.id or 0,
        initial_profile=initial_profile,
        thresholds=thresholds,
        settings_source=settings_source,
        sample_results=sample_results,
        target_height=target_height,
        target_height_reason=target_height_reason,
    )
