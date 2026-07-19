from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mediaforce.core.evidence import stable_source_id
from mediaforce.encoding.fingerprint import (
    DEFAULT_FINGERPRINT_MAX_FRAMES,
    DEFAULT_FINGERPRINT_RANGE_COUNT,
    DEFAULT_FINGERPRINT_SAMPLE_FPS,
)
from mediaforce.library.representatives import FINGERPRINT_DIMENSIONS, RepresentativeSelection, select_representatives


DEFAULT_UNCERTAINTY_FRONTIER_LIMIT = 3

FingerprintAcquisitionPhase = Literal["technical_representatives", "uncertainty_frontier", "complete"]
AudioComplexityRecommendation = Literal["retain", "defer", "not_applicable", "evaluate_with_replay"]


@dataclass(frozen=True, slots=True)
class SharedVisualPassCost:
    candidate_count: int
    estimated_range_count: int
    estimated_frame_count: int


@dataclass(frozen=True, slots=True)
class AudioComplexityCost:
    candidate_count: int
    estimated_range_count: int
    estimated_sample_seconds: float
    recommendation: AudioComplexityRecommendation


@dataclass(frozen=True, slots=True)
class FingerprintAnalysisCost:
    shared_visual_pass: SharedVisualPassCost
    audio_complexity: AudioComplexityCost


@dataclass(frozen=True, slots=True)
class FingerprintAcquisitionCoverage:
    total_item_count: int
    measured_item_count: int
    unmeasured_item_count: int
    technical_representative_count: int
    measured_technical_representative_count: int
    all_dimension_representative_count: int
    measured_all_dimension_representative_count: int
    global_fingerprint_coverage: float
    technical_representative_coverage: float
    all_dimension_representative_coverage: float


@dataclass(frozen=True, slots=True)
class FingerprintAcquisitionPlan:
    phase: FingerprintAcquisitionPhase
    candidate_ids: tuple[str, ...]
    technical_representative_ids: tuple[str, ...]
    all_dimension_representative_ids: tuple[str, ...]
    coverage: FingerprintAcquisitionCoverage
    stop_reason: str
    intentionally_unqueued_count: int
    estimated_analysis_cost: FingerprintAnalysisCost


@dataclass(frozen=True, slots=True)
class RepresentativeChanges:
    baseline_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added_ids or self.removed_ids)


@dataclass(frozen=True, slots=True)
class HardCaseRecall:
    total_hard_case_count: int
    recalled_hard_case_count: int
    fraction: float
    recalled_cases: tuple[tuple[str, str], ...]
    missed_cases: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SampleSetGrowth:
    baseline_count: int
    selected_count: int
    change_count: int


@dataclass(frozen=True, slots=True)
class RepresentativeDimensionReplay:
    name: str
    omitted_fingerprint_dimension: str | None
    fingerprint_dimensions: tuple[str, ...]
    representative_ids: tuple[str, ...]
    representative_changes: RepresentativeChanges
    hard_case_recall: HardCaseRecall
    sample_set_growth: SampleSetGrowth
    estimated_analysis_cost: FingerprintAnalysisCost


@dataclass(frozen=True, slots=True)
class RepresentativeDimensionReplayReport:
    technical_only: RepresentativeDimensionReplay
    all_dimensions: RepresentativeDimensionReplay
    leave_one_dimension_out: tuple[RepresentativeDimensionReplay, ...]
    shared_visual_pass_cost: SharedVisualPassCost
    audio_complexity_cost: AudioComplexityCost


def plan_fingerprint_acquisition(
        items: Sequence[Mapping[str, Any]],
        *,
        prefix: str = "",
        policy: Mapping[str, Any] | None = None,
        max_uncertainty_frontier: int = DEFAULT_UNCERTAINTY_FRONTIER_LIMIT,
        measured_source_ids: Collection[str] | None = None,
) -> FingerprintAcquisitionPlan:
    if max_uncertainty_frontier <= 0:
        raise ValueError("max_uncertainty_frontier must be greater than zero")

    item_by_source_id = _items_by_source_id(items)
    current_source_ids = _current_source_ids(items, measured_source_ids)
    technical_selection = select_representatives(
        items,
        prefix=prefix,
        policy=policy,
        fingerprint_dimensions=(),
    )
    all_dimension_selection = select_representatives(
        items,
        prefix=prefix,
        policy=policy,
        fingerprint_dimensions=FINGERPRINT_DIMENSIONS,
    )
    technical_representative_ids = _selected_source_ids(technical_selection)
    all_dimension_representative_ids = _selected_source_ids(all_dimension_selection)
    technical_candidates = tuple(
        source_id
        for source_id in technical_representative_ids
        if source_id not in current_source_ids
    )
    uncertainty_frontier = tuple(
        source_id
        for source_id in all_dimension_representative_ids
        if source_id not in current_source_ids and source_id not in technical_representative_ids
    )

    if technical_candidates:
        phase: FingerprintAcquisitionPhase = "technical_representatives"
        candidate_ids = technical_candidates
        stop_reason = "technical_representatives_pending"
    elif uncertainty_frontier:
        phase = "uncertainty_frontier"
        candidate_ids = uncertainty_frontier[:max_uncertainty_frontier]
        stop_reason = (
            "uncertainty_frontier_capped"
            if len(uncertainty_frontier) > len(candidate_ids)
            else "uncertainty_frontier_pending"
        )
    else:
        phase = "complete"
        candidate_ids = ()
        stop_reason = "representative_coverage_satisfied"

    coverage = _acquisition_coverage(
        item_by_source_id,
        current_source_ids,
        technical_representative_ids,
        all_dimension_representative_ids,
    )
    unmeasured_source_ids = set(item_by_source_id) - current_source_ids
    intentionally_unqueued_count = len(unmeasured_source_ids - set(candidate_ids))
    return FingerprintAcquisitionPlan(
        phase=phase,
        candidate_ids=candidate_ids,
        technical_representative_ids=technical_representative_ids,
        all_dimension_representative_ids=all_dimension_representative_ids,
        coverage=coverage,
        stop_reason=stop_reason,
        intentionally_unqueued_count=intentionally_unqueued_count,
        estimated_analysis_cost=_estimate_analysis_cost(
            candidate_ids,
            item_by_source_id,
            audio_recommendation="evaluate_with_replay",
        ),
    )


def replay_representative_dimensions(
        items: Sequence[Mapping[str, Any]],
        *,
        prefix: str = "",
        policy: Mapping[str, Any] | None = None,
        measured_source_ids: Collection[str] | None = None,
) -> RepresentativeDimensionReplayReport:
    item_by_source_id = _items_by_source_id(items)
    current_source_ids = _current_source_ids(items, measured_source_ids)
    technical_selection = select_representatives(
        items,
        prefix=prefix,
        policy=policy,
        fingerprint_dimensions=(),
    )
    all_dimension_selection = select_representatives(
        items,
        prefix=prefix,
        policy=policy,
        fingerprint_dimensions=FINGERPRINT_DIMENSIONS,
    )
    technical_representative_ids = _selected_source_ids(technical_selection)
    all_dimension_representative_ids = _selected_source_ids(all_dimension_selection)
    hard_cases = _hard_case_clusters(all_dimension_selection)
    technical_only = _replay_entry(
        name="technical_only",
        omitted_fingerprint_dimension=None,
        fingerprint_dimensions=(),
        selection=technical_selection,
        baseline_ids=technical_representative_ids,
        hard_cases=hard_cases,
        current_source_ids=current_source_ids,
        item_by_source_id=item_by_source_id,
    )
    all_dimensions = _replay_entry(
        name="all_dimensions",
        omitted_fingerprint_dimension=None,
        fingerprint_dimensions=FINGERPRINT_DIMENSIONS,
        selection=all_dimension_selection,
        baseline_ids=technical_representative_ids,
        hard_cases=hard_cases,
        current_source_ids=current_source_ids,
        item_by_source_id=item_by_source_id,
    )
    leave_one_dimension_out_entries: list[RepresentativeDimensionReplay] = []
    for omitted_dimension in FINGERPRINT_DIMENSIONS:
        replay_dimensions = tuple(
            dimension
            for dimension in FINGERPRINT_DIMENSIONS
            if dimension != omitted_dimension
        )
        leave_one_dimension_out_entries.append(
            _replay_entry(
                name="leave_one_dimension_out",
                omitted_fingerprint_dimension=omitted_dimension,
                fingerprint_dimensions=replay_dimensions,
                selection=select_representatives(
                    items,
                    prefix=prefix,
                    policy=policy,
                    fingerprint_dimensions=replay_dimensions,
                ),
                baseline_ids=all_dimension_representative_ids,
                hard_cases=hard_cases,
                current_source_ids=current_source_ids,
                item_by_source_id=item_by_source_id,
            )
        )
    leave_one_dimension_out = tuple(leave_one_dimension_out_entries)
    audio_omission = next(
        replay
        for replay in leave_one_dimension_out
        if replay.omitted_fingerprint_dimension == "audio_complexity"
    )
    audio_recommendation = _audio_complexity_recommendation(all_dimensions, audio_omission)
    return RepresentativeDimensionReplayReport(
        technical_only=technical_only,
        all_dimensions=all_dimensions,
        leave_one_dimension_out=leave_one_dimension_out,
        shared_visual_pass_cost=all_dimensions.estimated_analysis_cost.shared_visual_pass,
        audio_complexity_cost=_audio_cost(
            _unmeasured_selected_items(
                all_dimension_representative_ids,
                current_source_ids,
                item_by_source_id,
            ),
            recommendation=audio_recommendation,
        ),
    )


def _items_by_source_id(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    item_by_source_id = {stable_source_id(item): item for item in items}
    if len(item_by_source_id) != len(items):
        raise ValueError("Fingerprint acquisition candidates must have unique source IDs")
    return item_by_source_id


def _current_source_ids(
        items: Sequence[Mapping[str, Any]],
        measured_source_ids: Collection[str] | None,
) -> frozenset[str]:
    if measured_source_ids is not None:
        return frozenset(str(source_id).strip() for source_id in measured_source_ids if str(source_id).strip())
    return frozenset(
        stable_source_id(item)
        for item in items
        if _fingerprint_is_measured(item)
    )


def _fingerprint_is_measured(item: Mapping[str, Any]) -> bool:
    decision = item.get("media_fingerprint_decision")
    return (
        isinstance(decision, Mapping)
        and str(decision.get("status") or "").strip().lower() == "measured"
    )


def _selected_source_ids(selection: RepresentativeSelection) -> tuple[str, ...]:
    return tuple(str(item["source_id"]) for item in selection.payload["selected_items"])


def _acquisition_coverage(
        item_by_source_id: Mapping[str, Mapping[str, Any]],
        current_source_ids: Collection[str],
        technical_representative_ids: Sequence[str],
        all_dimension_representative_ids: Sequence[str],
) -> FingerprintAcquisitionCoverage:
    relevant_current_ids = set(item_by_source_id) & set(current_source_ids)
    technical_current_count = sum(
        source_id in relevant_current_ids
        for source_id in technical_representative_ids
    )
    all_dimension_current_count = sum(
        source_id in relevant_current_ids
        for source_id in all_dimension_representative_ids
    )
    total_item_count = len(item_by_source_id)
    return FingerprintAcquisitionCoverage(
        total_item_count=total_item_count,
        measured_item_count=len(relevant_current_ids),
        unmeasured_item_count=total_item_count - len(relevant_current_ids),
        technical_representative_count=len(technical_representative_ids),
        measured_technical_representative_count=technical_current_count,
        all_dimension_representative_count=len(all_dimension_representative_ids),
        measured_all_dimension_representative_count=all_dimension_current_count,
        global_fingerprint_coverage=_fraction(len(relevant_current_ids), total_item_count),
        technical_representative_coverage=_fraction(
            technical_current_count,
            len(technical_representative_ids),
        ),
        all_dimension_representative_coverage=_fraction(
            all_dimension_current_count,
            len(all_dimension_representative_ids),
        ),
    )


def _replay_entry(
        *,
        name: str,
        omitted_fingerprint_dimension: str | None,
        fingerprint_dimensions: tuple[str, ...],
        selection: RepresentativeSelection,
        baseline_ids: tuple[str, ...],
        hard_cases: tuple[tuple[str, str], ...],
        current_source_ids: Collection[str],
        item_by_source_id: Mapping[str, Mapping[str, Any]],
) -> RepresentativeDimensionReplay:
    representative_ids = _selected_source_ids(selection)
    return RepresentativeDimensionReplay(
        name=name,
        omitted_fingerprint_dimension=omitted_fingerprint_dimension,
        fingerprint_dimensions=fingerprint_dimensions,
        representative_ids=representative_ids,
        representative_changes=_representative_changes(baseline_ids, representative_ids),
        hard_case_recall=_hard_case_recall(hard_cases, selection),
        sample_set_growth=SampleSetGrowth(
            baseline_count=len(baseline_ids),
            selected_count=len(representative_ids),
            change_count=len(representative_ids) - len(baseline_ids),
        ),
        estimated_analysis_cost=_estimate_analysis_cost(
            representative_ids,
            item_by_source_id,
            current_source_ids=current_source_ids,
            audio_recommendation="evaluate_with_replay",
        ),
    )


def _representative_changes(
        baseline_ids: Sequence[str],
        representative_ids: Sequence[str],
) -> RepresentativeChanges:
    baseline_id_set = set(baseline_ids)
    representative_id_set = set(representative_ids)
    return RepresentativeChanges(
        baseline_ids=tuple(baseline_ids),
        added_ids=tuple(source_id for source_id in representative_ids if source_id not in baseline_id_set),
        removed_ids=tuple(source_id for source_id in baseline_ids if source_id not in representative_id_set),
    )


def _hard_case_clusters(selection: RepresentativeSelection) -> tuple[tuple[str, str], ...]:
    coverage_dimensions = selection.payload["coverage"]["dimensions"]
    hard_cases: set[tuple[str, str]] = set()
    for dimension in FINGERPRINT_DIMENSIONS:
        dimension_coverage = coverage_dimensions.get(dimension)
        if not isinstance(dimension_coverage, Mapping):
            continue
        values = {
            str(value)
            for value in dimension_coverage.get("covered_values", [])
        }
        values.update(
            str(value.get("value"))
            for value in dimension_coverage.get("uncovered_values", [])
            if isinstance(value, Mapping)
        )
        hard_cases.update(
            (dimension, value)
            for value in values
            if value not in {"typical", "unknown"}
        )
    return tuple(sorted(hard_cases))


def _hard_case_recall(
        hard_cases: tuple[tuple[str, str], ...],
        selection: RepresentativeSelection,
) -> HardCaseRecall:
    selected_profiles = [
        profile
        for item in selection.payload["selected_items"]
        if isinstance(profile := item.get("technical_profile"), Mapping)
    ]
    recalled_cases = tuple(
        hard_case
        for hard_case in hard_cases
        if any(profile.get(hard_case[0]) == hard_case[1] for profile in selected_profiles)
    )
    recalled_case_set = set(recalled_cases)
    return HardCaseRecall(
        total_hard_case_count=len(hard_cases),
        recalled_hard_case_count=len(recalled_cases),
        fraction=_fraction(len(recalled_cases), len(hard_cases)),
        recalled_cases=recalled_cases,
        missed_cases=tuple(hard_case for hard_case in hard_cases if hard_case not in recalled_case_set),
    )


def _estimate_analysis_cost(
        candidate_ids: Sequence[str],
        item_by_source_id: Mapping[str, Mapping[str, Any]],
        *,
        current_source_ids: Collection[str] = (),
        audio_recommendation: AudioComplexityRecommendation,
) -> FingerprintAnalysisCost:
    candidate_items = _unmeasured_selected_items(candidate_ids, current_source_ids, item_by_source_id)
    return FingerprintAnalysisCost(
        shared_visual_pass=_visual_cost(candidate_items),
        audio_complexity=_audio_cost(candidate_items, recommendation=audio_recommendation),
    )


def _unmeasured_selected_items(
        candidate_ids: Sequence[str],
        current_source_ids: Collection[str],
        item_by_source_id: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    current_id_set = set(current_source_ids)
    return tuple(
        item_by_source_id[source_id]
        for source_id in candidate_ids
        if source_id not in current_id_set
    )


def _visual_cost(candidate_items: Sequence[Mapping[str, Any]]) -> SharedVisualPassCost:
    range_counts = [_fingerprint_range_count(item) for item in candidate_items]
    return SharedVisualPassCost(
        candidate_count=len(candidate_items),
        estimated_range_count=sum(range_counts),
        estimated_frame_count=len(candidate_items) * DEFAULT_FINGERPRINT_MAX_FRAMES,
    )


def _audio_cost(
        candidate_items: Sequence[Mapping[str, Any]],
        *,
        recommendation: AudioComplexityRecommendation,
) -> AudioComplexityCost:
    audio_items = tuple(item for item in candidate_items if _has_audio(item))
    range_counts = [_fingerprint_range_count(item) for item in audio_items]
    estimated_sample_seconds = sum(
        _audio_sample_seconds(range_count)
        for range_count in range_counts
    )
    return AudioComplexityCost(
        candidate_count=len(audio_items),
        estimated_range_count=sum(range_counts),
        estimated_sample_seconds=round(estimated_sample_seconds, 3),
        recommendation=recommendation if audio_items else "not_applicable",
    )


def _fingerprint_range_count(item: Mapping[str, Any]) -> int:
    duration_seconds = _positive_duration(item.get("duration_seconds"))
    if duration_seconds is None or duration_seconds < 30:
        return 1
    return DEFAULT_FINGERPRINT_RANGE_COUNT


def _audio_sample_seconds(range_count: int) -> float:
    frames_per_range = max(1, DEFAULT_FINGERPRINT_MAX_FRAMES // max(1, range_count))
    return float(
        range_count
        * min(max(frames_per_range / max(DEFAULT_FINGERPRINT_SAMPLE_FPS, 0.1), 4.0), 12.0)
    )


def _has_audio(item: Mapping[str, Any]) -> bool:
    audio_summary = item.get("audio_summary")
    if isinstance(audio_summary, str):
        try:
            audio_summary = json.loads(audio_summary)
        except json.JSONDecodeError:
            return False
    return isinstance(audio_summary, list) and any(isinstance(track, Mapping) for track in audio_summary)


def _positive_duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        duration_seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return None
    return duration_seconds


def _fraction(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)


def _audio_complexity_recommendation(
        all_dimensions: RepresentativeDimensionReplay,
        audio_omission: RepresentativeDimensionReplay,
) -> AudioComplexityRecommendation:
    if all_dimensions.estimated_analysis_cost.audio_complexity.candidate_count == 0:
        return "not_applicable"
    if (
            audio_omission.representative_changes.changed
            or audio_omission.hard_case_recall.recalled_hard_case_count
            < all_dimensions.hard_case_recall.recalled_hard_case_count
    ):
        return "retain"
    return "defer"
