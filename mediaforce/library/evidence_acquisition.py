from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state
from mediaforce.core.evidence import stable_source_id
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.fingerprint import (
    DEFAULT_FINGERPRINT_MAX_FRAMES,
    DEFAULT_FINGERPRINT_RANGE_COUNT,
    DEFAULT_FINGERPRINT_SAMPLE_FPS,
)
from mediaforce.library.evidence_state import EVIDENCE_KINDS, parse_evidence_summary
from mediaforce.library.media_scopes import MediaScope
from mediaforce.library.representatives import FINGERPRINT_DIMENSIONS, MEANINGFUL_CLUSTER_FRACTION, \
    TECHNICAL_PROFILE_DIMENSIONS, RepresentativeSelection, load_representative_candidate_rows, \
    representative_profiles, select_representatives


DEFAULT_UNCERTAINTY_FRONTIER_LIMIT = 3
MIN_FINGERPRINT_ANALYSIS_BUDGET = 3
FINGERPRINT_ANALYSIS_BUDGET_MULTIPLIER = 2

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
    analysis_budget: int
    remaining_analysis_budget: int
    coverage: FingerprintAcquisitionCoverage
    stop_reason: str
    intentionally_unqueued_count: int
    estimated_analysis_cost: FingerprintAnalysisCost


@dataclass(frozen=True, slots=True)
class RepresentativeChanges:
    baseline_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    changed: bool


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
    remaining_analysis_cost: FingerprintAnalysisCost


@dataclass(frozen=True, slots=True)
class RepresentativeDimensionReplayReport:
    technical_only: RepresentativeDimensionReplay
    all_dimensions: RepresentativeDimensionReplay
    leave_one_dimension_out: tuple[RepresentativeDimensionReplay, ...]
    shared_visual_pass_cost: SharedVisualPassCost
    audio_complexity_cost: AudioComplexityCost


def load_fingerprint_acquisition_items(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> tuple[MediaScope, list[dict[str, Any]], frozenset[str]]:
    scope, rows = load_representative_candidate_rows(connection, config, prefix)
    if not rows:
        return scope, [], frozenset()
    item_ids = {int(row["id"]) for row in rows}
    current_rows = connection.execute(
        select(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
        )
        .where(
            library_item_evidence_state.c.library_item_id.in_(item_ids),
            library_item_evidence_state.c.evidence_kind.in_(EVIDENCE_KINDS),
            library_item_evidence_state.c.state == "current",
        )
    ).mappings().fetchall()
    current_item_ids_by_kind = {
        evidence_kind: {
            int(row["library_item_id"])
            for row in current_rows
            if str(row["evidence_kind"]) == evidence_kind
        }
        for evidence_kind in EVIDENCE_KINDS
    }
    items = [
        _acquisition_item(
            row,
            cadence_current=int(row["id"]) in current_item_ids_by_kind[EVIDENCE_KINDS[0]],
            fingerprint_current=int(row["id"]) in current_item_ids_by_kind[EVIDENCE_KINDS[1]],
        )
        for row in rows
    ]
    measured_source_ids = frozenset(
        stable_source_id(item)
        for item in items
        if int(item["library_item_id"]) in current_item_ids_by_kind[EVIDENCE_KINDS[1]]
    )
    return scope, items, measured_source_ids


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
    selection_items = _selection_items(item_by_source_id, current_source_ids)
    technical_selection = select_representatives(
        selection_items,
        prefix=prefix,
        policy=policy,
        fingerprint_dimensions=(),
    )
    all_dimension_selection = select_representatives(
        selection_items,
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
    analysis_budget = min(
        len(item_by_source_id),
        max(
            MIN_FINGERPRINT_ANALYSIS_BUDGET,
            len(technical_representative_ids) * FINGERPRINT_ANALYSIS_BUDGET_MULTIPLIER,
        ),
    )
    measured_item_count = len(set(item_by_source_id) & set(current_source_ids))
    remaining_analysis_budget = max(0, analysis_budget - measured_item_count)
    uncertainty_frontier = _uncertainty_frontier(
        selection_items,
        current_source_ids=current_source_ids,
        technical_representative_ids=technical_representative_ids,
        all_dimension_representative_ids=all_dimension_representative_ids,
    )

    if technical_candidates:
        phase: FingerprintAcquisitionPhase = "technical_representatives"
        candidate_ids = technical_candidates
        stop_reason = "technical_representatives_pending"
    elif uncertainty_frontier and remaining_analysis_budget:
        phase = "uncertainty_frontier"
        candidate_ids = uncertainty_frontier[:min(max_uncertainty_frontier, remaining_analysis_budget)]
        stop_reason = (
            "uncertainty_frontier_capped"
            if len(uncertainty_frontier) > len(candidate_ids)
            else "uncertainty_frontier_pending"
        )
    elif uncertainty_frontier:
        phase = "complete"
        candidate_ids = ()
        stop_reason = "bounded_analysis_budget_reached"
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
        analysis_budget=analysis_budget,
        remaining_analysis_budget=max(0, remaining_analysis_budget - len(candidate_ids)),
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
            _selected_items(
                all_dimension_representative_ids,
                item_by_source_id,
            ),
            recommendation=audio_recommendation,
        ),
    )


def _acquisition_item(
        row: Mapping[str, Any],
        *,
        cadence_current: bool,
        fingerprint_current: bool,
) -> dict[str, Any]:
    cadence_summary = parse_evidence_summary(row.get("cadence_summary_json")) if cadence_current else None
    fingerprint_summary = (
        parse_evidence_summary(row.get("media_fingerprint_json"))
        if fingerprint_current
        else None
    )
    cadence_decision = object_dict(cadence_summary.get("decision")) if cadence_summary else {}
    fingerprint_decision = object_dict(fingerprint_summary.get("decision")) if fingerprint_summary else {}
    source_fingerprint = str(
        row.get("content_version_fingerprint")
        or row.get("fingerprint")
        or ""
    ).strip() or None
    item = {
        "library_item_id": int(row["id"]),
        "rel_path": str(row["rel_path"]),
        "source_fingerprint": source_fingerprint,
        "source_size_bytes": int_value(row.get("size_bytes")),
        "video_codec": row.get("video_codec"),
        "width": row.get("width"),
        "height": row.get("height"),
        "cadence_class": str(cadence_decision.get("classification") or "unknown"),
        "duration_seconds": row.get("duration_seconds"),
        "audio_summary": _json_list(row.get("audio_summary_json")),
    }
    if fingerprint_decision:
        item["media_fingerprint_decision"] = fingerprint_decision
    return item


def _json_list(value: object) -> list[Any]:
    if isinstance(value, str):
        try:
            return object_list(json.loads(value))
        except json.JSONDecodeError:
            return []
    return object_list(value)


def _items_by_source_id(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    item_by_source_id = {stable_source_id(item): item for item in items}
    if len(item_by_source_id) != len(items):
        raise ValueError("Fingerprint acquisition candidates must have unique source IDs")
    return item_by_source_id


def _selection_items(
        item_by_source_id: Mapping[str, Mapping[str, Any]],
        current_source_ids: Collection[str],
) -> tuple[dict[str, Any], ...]:
    current_id_set = set(current_source_ids)
    selection_items: list[dict[str, Any]] = []
    for source_id, item in item_by_source_id.items():
        payload = dict(item)
        if source_id not in current_id_set:
            payload.pop("media_fingerprint_decision", None)
        selection_items.append(payload)
    return tuple(selection_items)


def _uncertainty_frontier(
        items: Sequence[Mapping[str, Any]],
        *,
        current_source_ids: Collection[str],
        technical_representative_ids: Sequence[str],
        all_dimension_representative_ids: Sequence[str],
) -> tuple[str, ...]:
    current_id_set = set(current_source_ids)
    if not current_id_set:
        return ()
    fingerprint_profiles = representative_profiles(
        items,
        fingerprint_dimensions=FINGERPRINT_DIMENSIONS,
    )
    hard_value_counts: Counter[tuple[str, str]] = Counter(
        (dimension, value)
        for source_id in current_id_set
        for dimension, value in fingerprint_profiles.get(source_id, {}).items()
        if dimension in FINGERPRINT_DIMENSIONS and _fingerprint_value_is_hard(value)
    )
    meaningful_count = max(1, math.ceil(len(items) * MEANINGFUL_CLUSTER_FRACTION))
    uncertain_values = {
        fingerprint_value
        for fingerprint_value, count in hard_value_counts.items()
        if count < meaningful_count
    }
    if not uncertain_values:
        return ()
    seed_ids = tuple(dict.fromkeys((
        *technical_representative_ids,
        *all_dimension_representative_ids,
    )))
    uncertain_seed_ids = tuple(
        source_id
        for source_id in seed_ids
        if source_id in current_id_set
        and any(
            (dimension, value) in uncertain_values
            for dimension, value in fingerprint_profiles.get(source_id, {}).items()
        )
    )
    if not uncertain_seed_ids:
        return ()
    technical_profiles = representative_profiles(items, fingerprint_dimensions=())
    item_by_source_id = _items_by_source_id(items)
    all_dimension_id_set = set(all_dimension_representative_ids)
    unmeasured_ids = set(item_by_source_id) - current_id_set
    return tuple(sorted(
        unmeasured_ids,
        key=lambda source_id: (
            source_id not in all_dimension_id_set,
            min(
                _technical_profile_distance(
                    technical_profiles[source_id],
                    technical_profiles[seed_id],
                )
                for seed_id in uncertain_seed_ids
            ),
            str(item_by_source_id[source_id].get("rel_path") or "").casefold(),
            source_id,
        ),
    ))


def _fingerprint_value_is_hard(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized not in {"", "unknown", "typical"}


def _technical_profile_distance(
        first: Mapping[str, str],
        second: Mapping[str, str],
) -> int:
    return sum(
        first.get(dimension) != second.get(dimension)
        for dimension in TECHNICAL_PROFILE_DIMENSIONS
    )


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
            audio_recommendation="evaluate_with_replay",
        ),
        remaining_analysis_cost=_estimate_analysis_cost(
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
        changed=baseline_id_set != representative_id_set,
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


def _selected_items(
        candidate_ids: Sequence[str],
        item_by_source_id: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(item_by_source_id[source_id] for source_id in candidate_ids)


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
