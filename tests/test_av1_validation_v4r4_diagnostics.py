import math

import pytest

from mediaforce.tuning.av1_validation_v4r4_contract import av1_v4r4_ordinal_layout
from mediaforce.tuning.av1_validation_v4r4_diagnostics import (
    AV1V4R4CandidateObservation,
    AV1V4R4ConflictDiagnostic,
    AV1V4R4DiagnosticError,
    AV1V4R4RuntimePolicy,
    AV1_V4R4_CONFLICT_CLASS,
    AV1_V4R4_CONFLICT_PHASE,
    AV1_V4R4_CONFLICT_STATUS,
    assert_av1_v4r4_conflict_diagnostic,
    classify_av1_v4r4_conflict,
    classify_av1_v4r4_quality_gap_band,
    classify_av1_v4r4_size_gap_band,
)


def _policy(*, ordinal: int = 1) -> AV1V4R4RuntimePolicy:
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    return AV1V4R4RuntimePolicy(
        ordinal=ordinal,
        asset_id=str(layout["asset_id"]),
        metric_name="vmaf",
        metric_target=85.0,
        minimum_metric_score=80.0,
        relax_step=0.5,
        sample_projection_tolerance_percent=10,
        final_output_tolerance_percent=5,
        source_cap_percent=80,
        total_target_bytes=int(layout["target_size_bytes"]),
        source_cap_total_bytes=int(layout["source_cap_total_bytes"]),
    )


def _candidate(
    attempt: int,
    *,
    score: float,
    projected: int,
    metric: str = "vmaf",
    target: float = 85.0,
    minimum: float = 80.0,
) -> AV1V4R4CandidateObservation:
    return AV1V4R4CandidateObservation(
        attempt=attempt,
        metric_name=metric,
        metric_target=target,
        metric_score=score,
        minimum_metric_score=minimum,
        projected_whole_output_bytes=projected,
    )


def _classify(
    reason: str,
    candidates: list[AV1V4R4CandidateObservation],
    *,
    policy: AV1V4R4RuntimePolicy | None = None,
) -> AV1V4R4ConflictDiagnostic:
    return classify_av1_v4r4_conflict(
        failure_phase=AV1_V4R4_CONFLICT_PHASE,
        failure_class=AV1_V4R4_CONFLICT_CLASS,
        failure_search_status=AV1_V4R4_CONFLICT_STATUS,
        failure_search_reason=reason,
        runtime_policy=policy or _policy(),
        candidates=candidates,
    )


@pytest.mark.parametrize(
    ("metric", "gap", "expected"),
    [
        ("vmaf", 0.5, "within_relax_step"),
        ("vmaf", 0.500001, "within_target_floor_span"),
        ("vmaf", 5.0, "within_target_floor_span"),
        ("vmaf", 5.000001, "within_two_target_floor_spans"),
        ("vmaf", 10.0, "within_two_target_floor_spans"),
        ("vmaf", 10.000001, "beyond_two_target_floor_spans"),
        ("xpsnr", 1.0, "within_relax_step"),
        ("xpsnr", 6.0, "within_target_floor_span"),
        ("xpsnr", 12.0, "within_two_target_floor_spans"),
        ("xpsnr", 12.000001, "beyond_two_target_floor_spans"),
    ],
)
def test_quality_gap_boundaries(metric: str, gap: float, expected: str) -> None:
    assert (
        classify_av1_v4r4_quality_gap_band(metric_name=metric, quality_gap=gap)
        == expected
    )


@pytest.mark.parametrize("gap", [0.0, -1.0, math.inf, math.nan])
def test_quality_gap_rejects_nonpositive_or_nonfinite(gap: float) -> None:
    with pytest.raises(AV1V4R4DiagnosticError):
        classify_av1_v4r4_quality_gap_band(metric_name="vmaf", quality_gap=gap)


def test_reason_specific_quality_reference_filters_are_literal() -> None:
    policy = _policy()
    target = policy.total_target_bytes
    candidates = [
        _candidate(1, score=79.0, projected=target),
        _candidate(2, score=79.8, projected=target * 8 // 10),
        _candidate(3, score=79.9, projected=policy.source_cap_total_bytes + 1),
    ]

    inside_band = _classify(
        "target_band_violates_quality_floor", candidates, policy=policy
    )
    crossing = _classify(
        "target_requires_crossing_quality_floor", candidates, policy=policy
    )
    all_candidates = _classify(
        "all_candidates_violate_quality_floor", candidates, policy=policy
    )

    assert inside_band.conflict_quality_gap_band == "within_target_floor_span"
    assert crossing.conflict_quality_gap_band == "within_relax_step"
    assert all_candidates.conflict_quality_gap_band == "within_relax_step"
    assert all_candidates.conflict_size_gap_band == "no_quality_safe_candidate"


def test_reason_pool_quality_safe_reference_makes_conflict_fatal() -> None:
    policy = _policy()
    target = policy.total_target_bytes
    with pytest.raises(AV1V4R4DiagnosticError, match="quality gap"):
        _classify(
            "target_band_violates_quality_floor",
            [
                _candidate(1, score=79.0, projected=target),
                _candidate(2, score=80.1, projected=target + 1),
            ],
            policy=policy,
        )


@pytest.mark.parametrize(
    ("projection_kind", "expected"),
    [
        ("below_lower", "below_sample_lower_bound"),
        ("above_target", "within_final_tolerance_above_target"),
        ("at_final_upper", "within_final_tolerance_above_target"),
        ("above_final_upper", "within_projection_tolerance_above_target"),
        ("at_sample_upper", "within_projection_tolerance_above_target"),
        ("above_sample_upper", "beyond_projection_tolerance_within_source_cap"),
    ],
)
def test_signed_size_gap_boundaries(projection_kind: str, expected: str) -> None:
    policy = _policy()
    target = policy.total_target_bytes
    projections = {
        "below_lower": (target * 90 - 1) // 100,
        "above_target": target + 1,
        "at_final_upper": target * 105 // 100,
        "above_final_upper": target * 105 // 100 + 1,
        "at_sample_upper": target * 110 // 100,
        "above_sample_upper": target * 110 // 100 + 1,
    }

    assert (
        classify_av1_v4r4_size_gap_band(
            candidates=[
                _candidate(1, score=80.1, projected=projections[projection_kind])
            ],
            runtime_policy=policy,
        )
        == expected
    )


def test_selectable_lower_half_contradiction_is_existential() -> None:
    policy = _policy()
    target = policy.total_target_bytes
    selectable_below = (target * 95 + 99) // 100
    closer_above = target * 102 // 100

    with pytest.raises(AV1V4R4DiagnosticError, match="contradicts"):
        classify_av1_v4r4_size_gap_band(
            candidates=[
                _candidate(1, score=80.5, projected=selectable_below),
                _candidate(2, score=81.0, projected=closer_above),
            ],
            runtime_policy=policy,
        )


def test_size_reference_reports_no_quality_safe_and_over_source_cap() -> None:
    policy = _policy()
    no_safe = classify_av1_v4r4_size_gap_band(
        candidates=[_candidate(1, score=79.0, projected=policy.total_target_bytes)],
        runtime_policy=policy,
    )
    over_cap = classify_av1_v4r4_size_gap_band(
        candidates=[
            _candidate(
                1,
                score=80.1,
                projected=policy.source_cap_total_bytes + 1,
            )
        ],
        runtime_policy=policy,
    )

    assert no_safe == "no_quality_safe_candidate"
    assert over_cap == "over_source_cap"


def test_size_reference_tie_breaks_distance_score_then_attempt() -> None:
    policy = _policy()
    target = policy.total_target_bytes
    distance = target // 5
    lower = target - distance
    upper = target + distance
    higher_score = classify_av1_v4r4_size_gap_band(
        candidates=[
            _candidate(1, score=80.1, projected=lower),
            _candidate(2, score=81.0, projected=upper),
        ],
        runtime_policy=policy,
    )
    lower_attempt = classify_av1_v4r4_size_gap_band(
        candidates=[
            _candidate(1, score=81.0, projected=lower),
            _candidate(2, score=81.0, projected=upper),
        ],
        runtime_policy=policy,
    )

    assert higher_score == "beyond_projection_tolerance_within_source_cap"
    assert lower_attempt == "below_sample_lower_bound"


def test_diagnostic_payload_is_bounded_and_ordinal_bound() -> None:
    policy = _policy()
    diagnostic = _classify(
        "target_band_violates_quality_floor",
        [_candidate(1, score=79.8, projected=policy.total_target_bytes)],
        policy=policy,
    )
    payload = diagnostic.to_payload()

    assert_av1_v4r4_conflict_diagnostic(payload)
    assert payload["ordinal"] == 1
    assert payload["asset_id"] == policy.asset_id
    prohibited = {
        "crf",
        "metric_score",
        "target_size_bytes",
        "source_cap_total_bytes",
        "candidate_count",
        "path",
        "argv",
        "stderr",
        "timing",
    }
    assert prohibited.isdisjoint(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_phase", "runtime_identity"),
        ("failure_class", "quality_search_error"),
        ("failure_search_status", "needs_review"),
        ("failure_search_reason", "future_reason"),
    ],
)
def test_conflict_boundary_rejects_unknown_or_noncanonical_values(
    field: str,
    value: str,
) -> None:
    policy = _policy()
    arguments = {
        "failure_phase": AV1_V4R4_CONFLICT_PHASE,
        "failure_class": AV1_V4R4_CONFLICT_CLASS,
        "failure_search_status": AV1_V4R4_CONFLICT_STATUS,
        "failure_search_reason": "target_band_violates_quality_floor",
        "runtime_policy": policy,
        "candidates": [_candidate(1, score=79.8, projected=policy.total_target_bytes)],
    }
    arguments[field] = value

    with pytest.raises(AV1V4R4DiagnosticError):
        classify_av1_v4r4_conflict(**arguments)


def test_candidates_reject_duplicate_attempts_and_policy_drift() -> None:
    policy = _policy()
    duplicate = _candidate(1, score=79.0, projected=policy.total_target_bytes)
    with pytest.raises(AV1V4R4DiagnosticError, match="distinct"):
        _classify(
            "target_band_violates_quality_floor",
            [duplicate, duplicate],
            policy=policy,
        )

    with pytest.raises(AV1V4R4DiagnosticError, match="policy binding"):
        _classify(
            "target_band_violates_quality_floor",
            [
                _candidate(
                    1,
                    score=79.0,
                    projected=policy.total_target_bytes,
                    target=84.0,
                )
            ],
            policy=policy,
        )


@pytest.mark.parametrize("field", ["total_target_bytes", "source_cap_total_bytes"])
def test_runtime_target_and_cap_bytes_are_frozen_by_ordinal(field: str) -> None:
    policy = _policy()
    arguments = {
        "ordinal": policy.ordinal,
        "asset_id": policy.asset_id,
        "metric_name": policy.metric_name,
        "metric_target": policy.metric_target,
        "minimum_metric_score": policy.minimum_metric_score,
        "relax_step": policy.relax_step,
        "sample_projection_tolerance_percent": (
            policy.sample_projection_tolerance_percent
        ),
        "final_output_tolerance_percent": policy.final_output_tolerance_percent,
        "source_cap_percent": policy.source_cap_percent,
        "total_target_bytes": policy.total_target_bytes,
        "source_cap_total_bytes": policy.source_cap_total_bytes,
    }
    arguments[field] = int(arguments[field]) + 1

    with pytest.raises(AV1V4R4DiagnosticError, match="frozen ordinal"):
        AV1V4R4RuntimePolicy(**arguments)


def test_reason_reference_rejects_an_empty_non_cap_pool() -> None:
    policy = _policy()
    with pytest.raises(AV1V4R4DiagnosticError, match="no consistent"):
        _classify(
            "target_band_violates_quality_floor",
            [
                _candidate(
                    1,
                    score=79.0,
                    projected=policy.source_cap_total_bytes + 1,
                )
            ],
            policy=policy,
        )


@pytest.mark.parametrize("projected", [0, -1, math.nan, math.inf, None])
def test_candidate_projection_must_be_positive(projected: object) -> None:
    with pytest.raises(AV1V4R4DiagnosticError, match="projection"):
        _candidate(1, score=79.0, projected=projected)  # type: ignore[arg-type]
