from dataclasses import dataclass
import math
from typing import Sequence

from mediaforce.tuning.av1_cold_start import AV1ColdStartEvaluationSummaryV1


@dataclass(frozen=True, slots=True)
class AV1ColdStartEvaluationCase:
    recommended_crf_lower: float
    recommended_crf_upper: float
    measured_selected_crf: float
    baseline_candidate_count: int
    guided_candidate_count: int
    baseline_quality_floor_met: bool
    guided_quality_floor_met: bool
    baseline_final_size_met: bool
    guided_final_size_met: bool
    baseline_visual_accepted: bool
    guided_visual_accepted: bool
    baseline_operator_attention_events: int
    guided_operator_attention_events: int

    def __post_init__(self) -> None:
        crf_values = (
            self.recommended_crf_lower,
            self.recommended_crf_upper,
            self.measured_selected_crf,
        )
        if not all(math.isfinite(value) for value in crf_values):
            raise ValueError("AV1 cold-start evaluation CRFs must be finite")
        if not 0 <= self.recommended_crf_lower <= self.recommended_crf_upper <= 63:
            raise ValueError("AV1 cold-start evaluation range is invalid")
        if not 0 <= self.measured_selected_crf <= 63:
            raise ValueError("AV1 cold-start measured CRF is invalid")
        counts = (
            self.baseline_candidate_count,
            self.guided_candidate_count,
            self.baseline_operator_attention_events,
            self.guided_operator_attention_events,
        )
        if any(value < 0 for value in counts):
            raise ValueError("AV1 cold-start evaluation counts must be non-negative")


def summarize_av1_cold_start_evaluation(
        cases: Sequence[AV1ColdStartEvaluationCase],
) -> AV1ColdStartEvaluationSummaryV1:
    return AV1ColdStartEvaluationSummaryV1(
        held_out_count=len(cases),
        range_hit_count=sum(
            case.recommended_crf_lower <= case.measured_selected_crf <= case.recommended_crf_upper
            for case in cases
        ),
        baseline_candidate_count=sum(case.baseline_candidate_count for case in cases),
        guided_candidate_count=sum(case.guided_candidate_count for case in cases),
        quality_floor_regression_count=sum(
            case.baseline_quality_floor_met and not case.guided_quality_floor_met
            for case in cases
        ),
        final_size_regression_count=sum(
            case.baseline_final_size_met and not case.guided_final_size_met
            for case in cases
        ),
        visual_rejection_regression_count=sum(
            case.baseline_visual_accepted and not case.guided_visual_accepted
            for case in cases
        ),
        operator_attention_regression_count=sum(
            case.guided_operator_attention_events > case.baseline_operator_attention_events
            for case in cases
        ),
    )
