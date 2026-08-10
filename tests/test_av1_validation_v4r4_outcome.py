from copy import deepcopy

import pytest

from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    assert_av1_v4r3_ordinal_window_outcome,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_LEVER_CLASSES,
    AV1_V4R4_QUALITY_GAP_BANDS,
    AV1_V4R4_SIZE_GAP_BANDS,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_diagnostics import (
    AV1V4R4ConflictDiagnostic,
)
from mediaforce.tuning.av1_validation_v4r4_outcome import (
    AV1V4R4OutcomeError,
    assert_av1_v4r4_outcome,
    assert_av1_v4r4_terminal,
    assert_av1_v4r4_terminal_chain,
    av1_v4r4_cell_lever,
    build_av1_v4r4_outcome,
    build_av1_v4r4_terminal,
    deserialize_av1_v4r4_outcome,
    deserialize_av1_v4r4_terminal,
    serialize_av1_v4r4_outcome,
    serialize_av1_v4r4_terminal,
)


def _diagnostic(
    *,
    ordinal: int = 3,
    reason: str = "target_band_violates_quality_floor",
    quality: str = "within_relax_step",
    size: str = "beyond_projection_tolerance_within_source_cap",
) -> AV1V4R4ConflictDiagnostic:
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    return AV1V4R4ConflictDiagnostic(
        ordinal=ordinal,
        asset_id=str(layout["asset_id"]),
        failure_search_reason=reason,
        conflict_quality_gap_band=quality,
        conflict_size_gap_band=size,
    )


def _selected(ordinal: int) -> dict[str, object]:
    if ordinal == 2:
        return build_av1_v4r4_outcome(
            ordinal=ordinal,
            disposition="selected_success",
            guided_probe_status="size_band_miss",
            guided_fallback_selected=True,
        )
    return build_av1_v4r4_outcome(
        ordinal=ordinal,
        disposition="selected_success",
    )


def _valid_complete_outcomes(
    *,
    additional_conflicts: dict[int, AV1V4R4ConflictDiagnostic] | None = None,
) -> list[dict[str, object]]:
    conflicts = {
        3: _diagnostic(ordinal=3),
        **(additional_conflicts or {}),
    }
    outcomes: list[dict[str, object]] = []
    for ordinal in range(1, 9):
        diagnostic = conflicts.get(ordinal)
        if diagnostic is not None:
            outcomes.append(
                build_av1_v4r4_outcome(
                    ordinal=ordinal,
                    disposition="bounded_quality_conflict",
                    conflict_diagnostic=diagnostic,
                )
            )
        else:
            outcomes.append(_selected(ordinal))
    return outcomes


def test_outcome_has_no_success_key_and_round_trips() -> None:
    outcome = _selected(1)

    assert outcome["completed"] is True
    assert outcome["disposition"] == "selected_success"
    assert "success" not in outcome
    assert deserialize_av1_v4r4_outcome(serialize_av1_v4r4_outcome(outcome)) == outcome


def test_bounded_conflict_requires_exact_diagnostic() -> None:
    outcome = build_av1_v4r4_outcome(
        ordinal=3,
        disposition="bounded_quality_conflict",
        conflict_diagnostic=_diagnostic(),
    )

    assert outcome["completed"] is True
    assert outcome["positive_control_matched"] is True

    with pytest.raises(AV1V4R4OutcomeError, match="must exist exactly"):
        build_av1_v4r4_outcome(
            ordinal=4,
            disposition="bounded_quality_conflict",
        )
    with pytest.raises(AV1V4R4OutcomeError, match="must exist exactly"):
        build_av1_v4r4_outcome(
            ordinal=4,
            disposition="selected_success",
            conflict_diagnostic=_diagnostic(),
        )


def test_positive_control_divergence_must_be_fatal() -> None:
    with pytest.raises(AV1V4R4OutcomeError, match="divergence must be fatal"):
        build_av1_v4r4_outcome(
            ordinal=3,
            disposition="selected_success",
        )

    fatal = build_av1_v4r4_outcome(
        ordinal=3,
        disposition="fatal_failure",
    )
    assert fatal["completed"] is False
    assert fatal["positive_control_matched"] is False


def test_revision_three_reader_rejects_revision_four_outcome() -> None:
    with pytest.raises(AV1V4R3OrdinalWindowError):
        assert_av1_v4r3_ordinal_window_outcome(_selected(1))


def test_outcome_rejects_tamper_unknown_field_and_authority() -> None:
    outcome = _selected(1)

    extra = deepcopy(outcome)
    extra["success"] = True
    with pytest.raises(AV1V4R4OutcomeError, match="shape"):
        assert_av1_v4r4_outcome(extra)

    authorized = deepcopy(outcome)
    authorized["dogfood_authorized"] = True
    with pytest.raises(AV1V4R4OutcomeError, match="cannot confer authority"):
        assert_av1_v4r4_outcome(authorized)

    false_control = deepcopy(outcome)
    false_control["positive_control_matched"] = False
    with pytest.raises(AV1V4R4OutcomeError, match="does not match observation"):
        assert_av1_v4r4_outcome(false_control)

    malformed_reason = build_av1_v4r4_outcome(
        ordinal=3,
        disposition="bounded_quality_conflict",
        conflict_diagnostic=_diagnostic(),
    )
    malformed_reason["conflict_diagnostic"]["failure_search_reason"] = "future_reason"
    with pytest.raises(AV1V4R4OutcomeError, match="diagnostic is invalid"):
        assert_av1_v4r4_outcome(malformed_reason)


def test_complete_mixed_terminal_separates_traversal_from_policy_verdict() -> None:
    terminal = build_av1_v4r4_terminal(_valid_complete_outcomes())

    assert terminal["traversal_complete"] is True
    assert terminal["termination_kind"] == "traversal_complete"
    assert terminal["verdict"] == "policy_conflicted"
    assert terminal["selected_count"] == 7
    assert terminal["conflict_count"] == 1
    assert terminal["traversed_count"] == 8
    assert terminal["map_result"] == "content_class_target_normalization"
    assert terminal["hypothesis_h1"] == "supported"
    assert "success" not in terminal
    assert (
        deserialize_av1_v4r4_terminal(serialize_av1_v4r4_terminal(terminal)) == terminal
    )
    assert_av1_v4r4_terminal_chain(
        outcomes=_valid_complete_outcomes(),
        terminal=terminal,
    )


def test_fatal_and_incomplete_terminals_are_invalid_for_interpretation() -> None:
    first_three = _valid_complete_outcomes()[:3]
    incomplete = build_av1_v4r4_terminal(first_three)
    fatal = build_av1_v4r4_terminal(
        [
            *first_three,
            build_av1_v4r4_outcome(
                ordinal=4,
                disposition="fatal_failure",
            ),
        ]
    )

    assert incomplete["termination_kind"] == "incomplete_seal"
    assert incomplete["verdict"] == "indeterminate"
    assert incomplete["map_result"] == "invalid"
    assert fatal["termination_kind"] == "fatal_failure"
    assert fatal["traversed_count"] == 3
    assert fatal["map_result"] == "invalid"


def test_fatal_outcome_absorbs_later_ordinals() -> None:
    outcomes = [
        _selected(1),
        _selected(2),
        build_av1_v4r4_outcome(ordinal=3, disposition="fatal_failure"),
        _selected(4),
    ]

    with pytest.raises(AV1V4R4OutcomeError, match="absorb"):
        build_av1_v4r4_terminal(outcomes)


def test_map_aggregation_prefers_multi_axis_and_falsifies_h1() -> None:
    terminal = build_av1_v4r4_terminal(
        _valid_complete_outcomes(
            additional_conflicts={
                5: _diagnostic(
                    ordinal=5,
                    reason="target_requires_crossing_quality_floor",
                    size="within_final_tolerance_above_target",
                )
            }
        )
    )

    assert terminal["map_result"] == "multi_axis_policy_required"
    assert terminal["hypothesis_h1"] == "falsified"


@pytest.mark.parametrize("quality", AV1_V4R4_QUALITY_GAP_BANDS)
@pytest.mark.parametrize("size", AV1_V4R4_SIZE_GAP_BANDS)
def test_map_rule_is_total_over_every_quality_size_cell(
    quality: str,
    size: str,
) -> None:
    assert (
        av1_v4r4_cell_lever(
            quality_gap_band=quality,
            size_gap_band=size,
        )
        in AV1_V4R4_LEVER_CLASSES
    )


@pytest.mark.parametrize(
    ("quality", "size", "expected"),
    [
        (
            "within_relax_step",
            "no_quality_safe_candidate",
            "measurement_or_quality_corridor_investigation",
        ),
        (
            "within_target_floor_span",
            "within_final_tolerance_above_target",
            "quality_corridor_investigation",
        ),
        (
            "beyond_two_target_floor_spans",
            "over_source_cap",
            "multi_axis_policy_required",
        ),
        (
            "within_relax_step",
            "below_sample_lower_bound",
            "search_or_intent_semantics_investigation",
        ),
        (
            "within_relax_step",
            "over_source_cap",
            "source_cap_or_resolution_intent",
        ),
        (
            "within_relax_step",
            "beyond_projection_tolerance_within_source_cap",
            "content_class_target_normalization",
        ),
        (
            "within_relax_step",
            "within_projection_tolerance_above_target",
            "projection_or_tolerance_calibration",
        ),
    ],
)
def test_each_normative_map_branch_has_an_exact_fixture(
    quality: str,
    size: str,
    expected: str,
) -> None:
    assert (
        av1_v4r4_cell_lever(
            quality_gap_band=quality,
            size_gap_band=size,
        )
        == expected
    )


def test_terminal_count_and_map_tamper_is_rejected() -> None:
    terminal = build_av1_v4r4_terminal(_valid_complete_outcomes())

    wrong_count = deepcopy(terminal)
    wrong_count["conflict_count"] = 2
    with pytest.raises(AV1V4R4OutcomeError, match="conflict_count"):
        assert_av1_v4r4_terminal(wrong_count)

    wrong_lever = deepcopy(terminal)
    wrong_lever["conflict_cells"][0]["lever_class"] = (
        "projection_or_tolerance_calibration"
    )
    with pytest.raises(AV1V4R4OutcomeError, match="conflict-cell binding"):
        assert_av1_v4r4_terminal(wrong_lever)

    wrong_reason_join = deepcopy(terminal)
    wrong_reason_join["conflict_cells"][0]["failure_search_reason"] = (
        "all_candidates_violate_quality_floor"
    )
    with pytest.raises(AV1V4R4OutcomeError, match="conflict-cell binding"):
        assert_av1_v4r4_terminal(wrong_reason_join)

    malformed_binding_reason = deepcopy(terminal)
    malformed_binding_reason["outcome_bindings"][2]["conflict_reason"] = (
        "future_reason"
    )
    with pytest.raises(AV1V4R4OutcomeError, match="conflict-reason binding"):
        assert_av1_v4r4_terminal(malformed_binding_reason)

    malformed_cell_reason = deepcopy(terminal)
    malformed_cell_reason["conflict_cells"][0]["failure_search_reason"] = (
        "future_reason"
    )
    with pytest.raises(AV1V4R4OutcomeError, match="conflict-cell binding"):
        assert_av1_v4r4_terminal(malformed_cell_reason)

    false_control = deepcopy(terminal)
    false_control["outcome_bindings"][2]["disposition"] = "selected_success"
    false_control["outcome_bindings"][2]["conflict_reason"] = None
    with pytest.raises(AV1V4R4OutcomeError, match="positive-control"):
        assert_av1_v4r4_terminal(false_control)

    wrong_chain = build_av1_v4r4_terminal(
        _valid_complete_outcomes(
            additional_conflicts={
                5: _diagnostic(
                    ordinal=5,
                    reason="target_requires_crossing_quality_floor",
                    size="within_final_tolerance_above_target",
                )
            }
        )
    )
    with pytest.raises(AV1V4R4OutcomeError, match="does not match"):
        assert_av1_v4r4_terminal_chain(
            outcomes=_valid_complete_outcomes(),
            terminal=wrong_chain,
        )
