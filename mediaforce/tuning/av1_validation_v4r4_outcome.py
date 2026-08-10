"""Pure outcome, terminal, and map contracts for AV1 protocol-v4 revision 4."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_ADVANCING_DISPOSITIONS,
    AV1_V4R4_CONFLICT_REASONS,
    AV1_V4R4_CONTRACT_VERSION,
    AV1_V4R4_DISPOSITIONS,
    AV1_V4R4_EXPERIMENT_ID,
    AV1_V4R4_GUIDED_PROBE_STATUSES,
    AV1_V4R4_HYPOTHESIS_RESULTS,
    AV1_V4R4_LEVER_CLASSES,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_MANIFEST_REVISION,
    AV1_V4R4_MAP_RESULTS,
    AV1_V4R4_ORDINAL_COUNT,
    AV1_V4R4_POSITIVE_CONTROLS,
    AV1_V4R4_PROTOCOL_VERSION,
    AV1_V4R4_QUALITY_GAP_BANDS,
    AV1_V4R4_SIZE_GAP_BANDS,
    AV1_V4R4_TERMINATION_KINDS,
    AV1_V4R4_VERDICTS,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_diagnostics import (
    AV1V4R4ConflictDiagnostic,
    AV1V4R4DiagnosticError,
    assert_av1_v4r4_conflict_diagnostic,
)


AV1_V4R4_OUTCOME_SCHEMA = "mediaforce.av1_cold_start_v4r4_ordinal_outcome"
AV1_V4R4_OUTCOME_SCHEMA_VERSION = 1
AV1_V4R4_OUTCOME_CONTRACT_VERSION = "av1v4r4out1"
AV1_V4R4_TERMINAL_SCHEMA = "mediaforce.av1_cold_start_v4r4_terminal"
AV1_V4R4_TERMINAL_SCHEMA_VERSION = 1
AV1_V4R4_TERMINAL_CONTRACT_VERSION = "av1v4r4term1"

_OUTCOME_ID_RE = re.compile(r"av1vordout4r4_[0-9a-f]{32}\Z")
_TERMINAL_ID_RE = re.compile(r"av1vordterm4r4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OUTCOME_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "protocol_version",
    "manifest_revision",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "outcome_id",
    "payload_sha256",
    "ordinal",
    "asset_id",
    "content_class",
    "role",
    "configuration",
    "completed",
    "disposition",
    "conflict_diagnostic",
    "positive_control_observation",
    "positive_control_matched",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_TERMINAL_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "protocol_version",
    "manifest_revision",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "terminal_id",
    "payload_sha256",
    "outcome_bindings",
    "conflict_cells",
    "outcome_count",
    "selected_count",
    "conflict_count",
    "traversed_count",
    "traversal_complete",
    "termination_kind",
    "verdict",
    "conflict_reason_counts",
    "quality_gap_band_counts",
    "size_gap_band_counts",
    "map_result",
    "hypothesis_h1",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS


class AV1V4R4OutcomeError(ValueError):
    """Raised when an r4 outcome, terminal, or map artifact is invalid."""


def build_av1_v4r4_outcome(
    *,
    ordinal: int,
    disposition: str,
    conflict_diagnostic: AV1V4R4ConflictDiagnostic | None = None,
    guided_probe_status: str | None = None,
    guided_fallback_selected: bool | None = None,
) -> dict[str, Any]:
    layout = _layout_for_ordinal(ordinal)
    if disposition not in AV1_V4R4_DISPOSITIONS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome disposition is invalid")
    if (disposition == "bounded_quality_conflict") != (conflict_diagnostic is not None):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 conflict diagnostic must exist exactly for a bounded conflict"
        )
    diagnostic_payload = (
        conflict_diagnostic.to_payload() if conflict_diagnostic is not None else None
    )
    if conflict_diagnostic is not None and (
        conflict_diagnostic.ordinal != ordinal
        or conflict_diagnostic.asset_id != layout["asset_id"]
    ):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 conflict diagnostic does not match the outcome ordinal"
        )
    positive_control_observation = _positive_control_observation(
        ordinal=ordinal,
        guided_probe_status=guided_probe_status,
        guided_fallback_selected=guided_fallback_selected,
    )
    positive_control_matched = _positive_control_match(
        ordinal=ordinal,
        disposition=disposition,
        diagnostic=conflict_diagnostic,
        observation=positive_control_observation,
    )
    if positive_control_matched is False and disposition != "fatal_failure":
        raise AV1V4R4OutcomeError("AV1 v4 r4 positive-control divergence must be fatal")
    payload: dict[str, Any] = {
        "schema": AV1_V4R4_OUTCOME_SCHEMA,
        "schema_version": AV1_V4R4_OUTCOME_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_OUTCOME_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        "ordinal": ordinal,
        "asset_id": layout["asset_id"],
        "content_class": layout["content_class"],
        "role": layout["role"],
        "configuration": layout["configuration"],
        "completed": disposition in AV1_V4R4_ADVANCING_DISPOSITIONS,
        "disposition": disposition,
        "conflict_diagnostic": diagnostic_payload,
        "positive_control_observation": positive_control_observation,
        "positive_control_matched": positive_control_matched,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(
        payload,
        id_field="outcome_id",
        id_prefix="av1vordout4r4_",
        domain=av1_v4r4_identity_domain("ordinal-outcome"),
    )
    assert_av1_v4r4_outcome(bound)
    return bound


def assert_av1_v4r4_outcome(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _OUTCOME_KEYS or "success" in materialized:
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome shape is invalid")
    _assert_common_binding(materialized)
    if (
        materialized.get("schema") != AV1_V4R4_OUTCOME_SCHEMA
        or materialized.get("schema_version") != AV1_V4R4_OUTCOME_SCHEMA_VERSION
        or materialized.get("contract_version") != AV1_V4R4_OUTCOME_CONTRACT_VERSION
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome schema binding is invalid")
    _assert_no_private_text(materialized)
    ordinal = materialized.get("ordinal")
    if type(ordinal) is not int:
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome ordinal is invalid")
    layout = _layout_for_ordinal(ordinal)
    for field in ("asset_id", "content_class", "role", "configuration"):
        if materialized.get(field) != layout[field]:
            raise AV1V4R4OutcomeError("AV1 v4 r4 outcome ordinal binding is invalid")
    disposition = materialized.get("disposition")
    if disposition not in AV1_V4R4_DISPOSITIONS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome disposition is invalid")
    if materialized.get("completed") is not (
        disposition in AV1_V4R4_ADVANCING_DISPOSITIONS
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome completion flag is invalid")
    diagnostic_payload = materialized.get("conflict_diagnostic")
    if disposition == "bounded_quality_conflict":
        if not isinstance(diagnostic_payload, Mapping):
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 bounded conflict diagnostic is missing"
            )
        try:
            assert_av1_v4r4_conflict_diagnostic(diagnostic_payload)
        except AV1V4R4DiagnosticError as exc:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 bounded conflict diagnostic is invalid"
            ) from exc
        if (
            diagnostic_payload.get("ordinal") != ordinal
            or diagnostic_payload.get("asset_id") != layout["asset_id"]
        ):
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 conflict diagnostic does not match the outcome ordinal"
            )
    elif diagnostic_payload is not None:
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 non-conflict outcome cannot carry a diagnostic"
        )
    positive_control = materialized.get("positive_control_matched")
    observation = materialized.get("positive_control_observation")
    if ordinal in AV1_V4R4_POSITIVE_CONTROLS:
        if type(positive_control) is not bool or not isinstance(observation, Mapping):
            raise AV1V4R4OutcomeError("AV1 v4 r4 positive-control status is invalid")
        try:
            diagnostic = (
                AV1V4R4ConflictDiagnostic(
                    ordinal=object_dict(diagnostic_payload).get("ordinal"),
                    asset_id=str(object_dict(diagnostic_payload).get("asset_id") or ""),
                    failure_search_reason=str(
                        object_dict(diagnostic_payload).get("failure_search_reason")
                        or ""
                    ),
                    conflict_quality_gap_band=str(
                        object_dict(diagnostic_payload).get("conflict_quality_gap_band")
                        or ""
                    ),
                    conflict_size_gap_band=str(
                        object_dict(diagnostic_payload).get("conflict_size_gap_band")
                        or ""
                    ),
                )
                if disposition == "bounded_quality_conflict"
                else None
            )
            expected_control = _positive_control_match(
                ordinal=ordinal,
                disposition=str(disposition),
                diagnostic=diagnostic,
                observation=object_dict(observation),
            )
        except AV1V4R4DiagnosticError as exc:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 positive-control observation is invalid"
            ) from exc
        if positive_control is not expected_control:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 positive-control status does not match observation"
            )
        if expected_control is False and disposition != "fatal_failure":
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 positive-control divergence is not fatal"
            )
    elif positive_control is not None or observation is not None:
        raise AV1V4R4OutcomeError("AV1 v4 r4 non-control ordinal has a control status")
    _assert_false_authorities(materialized)
    _assert_bound_identity(
        materialized,
        id_field="outcome_id",
        id_pattern=_OUTCOME_ID_RE,
        id_prefix="av1vordout4r4_",
        domain=av1_v4r4_identity_domain("ordinal-outcome"),
    )


def build_av1_v4r4_terminal(
    outcomes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    materialized_outcomes = [dict(outcome) for outcome in outcomes]
    for outcome in materialized_outcomes:
        assert_av1_v4r4_outcome(outcome)
    _assert_outcome_sequence(materialized_outcomes)
    summary = _terminal_summary(materialized_outcomes)
    payload: dict[str, Any] = {
        "schema": AV1_V4R4_TERMINAL_SCHEMA,
        "schema_version": AV1_V4R4_TERMINAL_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_TERMINAL_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        **summary,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(
        payload,
        id_field="terminal_id",
        id_prefix="av1vordterm4r4_",
        domain=av1_v4r4_identity_domain("terminal"),
    )
    assert_av1_v4r4_terminal(bound)
    return bound


def assert_av1_v4r4_terminal(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _TERMINAL_KEYS or "success" in materialized:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal shape is invalid")
    _assert_common_binding(materialized)
    if (
        materialized.get("schema") != AV1_V4R4_TERMINAL_SCHEMA
        or materialized.get("schema_version") != AV1_V4R4_TERMINAL_SCHEMA_VERSION
        or materialized.get("contract_version") != AV1_V4R4_TERMINAL_CONTRACT_VERSION
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal schema binding is invalid")
    _assert_no_private_text(materialized)
    bindings = object_list(materialized.get("outcome_bindings"))
    conflict_cells = object_list(materialized.get("conflict_cells"))
    _assert_terminal_bindings(bindings)
    _assert_conflict_cells(bindings, conflict_cells)
    expected_summary = _terminal_summary_from_bindings(bindings, conflict_cells)
    for key, value in expected_summary.items():
        if materialized.get(key) != value:
            raise AV1V4R4OutcomeError(
                f"AV1 v4 r4 terminal {key} does not match outcome bindings"
            )
    if materialized.get("map_result") not in AV1_V4R4_MAP_RESULTS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal map result is invalid")
    if materialized.get("hypothesis_h1") not in AV1_V4R4_HYPOTHESIS_RESULTS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal hypothesis result is invalid")
    _assert_false_authorities(materialized)
    _assert_bound_identity(
        materialized,
        id_field="terminal_id",
        id_pattern=_TERMINAL_ID_RE,
        id_prefix="av1vordterm4r4_",
        domain=av1_v4r4_identity_domain("terminal"),
    )


def assert_av1_v4r4_terminal_chain(
    *,
    outcomes: Sequence[Mapping[str, Any]],
    terminal: Mapping[str, Any],
) -> None:
    materialized_outcomes = [dict(outcome) for outcome in outcomes]
    for outcome in materialized_outcomes:
        assert_av1_v4r4_outcome(outcome)
    _assert_outcome_sequence(materialized_outcomes)
    assert_av1_v4r4_terminal(terminal)
    expected = build_av1_v4r4_terminal(materialized_outcomes)
    if dict(terminal) != expected:
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 terminal does not match the bound outcome sequence"
        )


def serialize_av1_v4r4_outcome(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r4_outcome(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r4_outcome(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_outcome, "outcome")


def serialize_av1_v4r4_terminal(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r4_terminal(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r4_terminal(data: bytes) -> dict[str, Any]:
    return _deserialize_artifact(data, assert_av1_v4r4_terminal, "terminal")


def av1_v4r4_cell_lever(
    *,
    quality_gap_band: str,
    size_gap_band: str,
) -> str:
    if quality_gap_band not in AV1_V4R4_QUALITY_GAP_BANDS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 quality-gap band is invalid")
    if size_gap_band not in AV1_V4R4_SIZE_GAP_BANDS:
        raise AV1V4R4OutcomeError("AV1 v4 r4 size-gap band is invalid")
    if size_gap_band == "no_quality_safe_candidate":
        return "measurement_or_quality_corridor_investigation"
    if quality_gap_band != "within_relax_step":
        if size_gap_band in {
            "within_final_tolerance_above_target",
            "within_projection_tolerance_above_target",
        }:
            return "quality_corridor_investigation"
        return "multi_axis_policy_required"
    if size_gap_band == "below_sample_lower_bound":
        return "search_or_intent_semantics_investigation"
    if size_gap_band == "over_source_cap":
        return "source_cap_or_resolution_intent"
    if size_gap_band == "beyond_projection_tolerance_within_source_cap":
        return "content_class_target_normalization"
    return "projection_or_tolerance_calibration"


def _positive_control_observation(
    *,
    ordinal: int,
    guided_probe_status: str | None,
    guided_fallback_selected: bool | None,
) -> dict[str, Any] | None:
    if ordinal not in AV1_V4R4_POSITIVE_CONTROLS:
        if guided_probe_status is not None or guided_fallback_selected is not None:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 non-control ordinal has control observations"
            )
        return None
    if guided_probe_status is not None and not isinstance(guided_probe_status, str):
        raise AV1V4R4OutcomeError("AV1 v4 r4 guided positive-control status is invalid")
    if (
        guided_probe_status is not None
        and guided_probe_status not in AV1_V4R4_GUIDED_PROBE_STATUSES
    ):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 guided positive-control status is not frozen"
        )
    if (
        guided_fallback_selected is not None
        and type(guided_fallback_selected) is not bool
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 guided fallback observation is invalid")
    return {
        "guided_probe_status": guided_probe_status,
        "guided_fallback_selected": guided_fallback_selected,
    }


def _positive_control_match(
    *,
    ordinal: int,
    disposition: str,
    diagnostic: AV1V4R4ConflictDiagnostic | None,
    observation: Mapping[str, Any] | None,
) -> bool | None:
    expectation = AV1_V4R4_POSITIVE_CONTROLS.get(ordinal)
    if expectation is None:
        return None
    if not isinstance(observation, Mapping):
        raise AV1V4R4OutcomeError("AV1 v4 r4 positive-control observation is missing")
    if set(observation) != {"guided_probe_status", "guided_fallback_selected"}:
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 positive-control observation shape is invalid"
        )
    guided_probe_status = observation.get("guided_probe_status")
    guided_fallback_selected = observation.get("guided_fallback_selected")
    if guided_probe_status is not None and not isinstance(guided_probe_status, str):
        raise AV1V4R4OutcomeError("AV1 v4 r4 guided positive-control status is invalid")
    if (
        guided_probe_status is not None
        and guided_probe_status not in AV1_V4R4_GUIDED_PROBE_STATUSES
    ):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 guided positive-control status is not frozen"
        )
    if (
        guided_fallback_selected is not None
        and type(guided_fallback_selected) is not bool
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 guided fallback observation is invalid")
    reason = diagnostic.failure_search_reason if diagnostic is not None else None
    return (
        disposition == expectation["disposition"]
        and reason == expectation["conflict_reason"]
        and guided_probe_status == expectation["guided_probe_status"]
        and guided_fallback_selected == expectation["guided_fallback_selected"]
    )


def _terminal_summary(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bindings = [
        {
            "ordinal": outcome["ordinal"],
            "outcome_id": outcome["outcome_id"],
            "payload_sha256": outcome["payload_sha256"],
            "disposition": outcome["disposition"],
            "content_class": outcome["content_class"],
            "conflict_reason": object_dict(outcome.get("conflict_diagnostic")).get(
                "failure_search_reason"
            ),
            "positive_control_observation": outcome["positive_control_observation"],
            "positive_control_matched": outcome["positive_control_matched"],
        }
        for outcome in outcomes
    ]
    conflict_cells = []
    for outcome in outcomes:
        diagnostic = object_dict(outcome.get("conflict_diagnostic"))
        if not diagnostic:
            continue
        quality_band = str(diagnostic["conflict_quality_gap_band"])
        size_band = str(diagnostic["conflict_size_gap_band"])
        conflict_cells.append(
            {
                "ordinal": outcome["ordinal"],
                "content_class": outcome["content_class"],
                "failure_search_reason": diagnostic["failure_search_reason"],
                "conflict_quality_gap_band": quality_band,
                "conflict_size_gap_band": size_band,
                "lever_class": av1_v4r4_cell_lever(
                    quality_gap_band=quality_band,
                    size_gap_band=size_band,
                ),
            }
        )
    return _terminal_summary_from_bindings(bindings, conflict_cells)


def _terminal_summary_from_bindings(
    bindings: Sequence[Mapping[str, Any]],
    conflict_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    dispositions = [str(binding["disposition"]) for binding in bindings]
    selected_count = dispositions.count("selected_success")
    conflict_count = dispositions.count("bounded_quality_conflict")
    traversed_count = selected_count + conflict_count
    fatal_count = dispositions.count("fatal_failure")
    traversal_complete = (
        len(bindings) == AV1_V4R4_ORDINAL_COUNT
        and traversed_count == AV1_V4R4_ORDINAL_COUNT
        and fatal_count == 0
    )
    if fatal_count:
        termination_kind = "fatal_failure"
    elif traversal_complete:
        termination_kind = "traversal_complete"
    else:
        termination_kind = "incomplete_seal"
    if traversal_complete and conflict_count == 0:
        verdict = "policy_satisfied"
    elif traversal_complete:
        verdict = "policy_conflicted"
    else:
        verdict = "indeterminate"
    reason_counts = Counter(
        str(cell["failure_search_reason"]) for cell in conflict_cells
    )
    quality_counts = Counter(
        str(cell["conflict_quality_gap_band"]) for cell in conflict_cells
    )
    size_counts = Counter(
        str(cell["conflict_size_gap_band"]) for cell in conflict_cells
    )
    map_result, hypothesis_h1 = _map_interpretation(
        bindings=bindings,
        conflict_cells=conflict_cells,
        traversal_complete=traversal_complete,
    )
    return {
        "outcome_bindings": [dict(binding) for binding in bindings],
        "conflict_cells": [dict(cell) for cell in conflict_cells],
        "outcome_count": len(bindings),
        "selected_count": selected_count,
        "conflict_count": conflict_count,
        "traversed_count": traversed_count,
        "traversal_complete": traversal_complete,
        "termination_kind": termination_kind,
        "verdict": verdict,
        "conflict_reason_counts": {
            reason: reason_counts[reason] for reason in AV1_V4R4_CONFLICT_REASONS
        },
        "quality_gap_band_counts": {
            band: quality_counts[band] for band in AV1_V4R4_QUALITY_GAP_BANDS
        },
        "size_gap_band_counts": {
            band: size_counts[band] for band in AV1_V4R4_SIZE_GAP_BANDS
        },
        "map_result": map_result,
        "hypothesis_h1": hypothesis_h1,
    }


def _map_interpretation(
    *,
    bindings: Sequence[Mapping[str, Any]],
    conflict_cells: Sequence[Mapping[str, Any]],
    traversal_complete: bool,
) -> tuple[str, str]:
    controls_match = all(
        binding.get("positive_control_matched") is True
        for binding in bindings
        if int(binding["ordinal"]) in AV1_V4R4_POSITIVE_CONTROLS
    ) and all(
        any(
            int(binding["ordinal"]) == ordinal
            and binding.get("positive_control_matched") is True
            for binding in bindings
        )
        for ordinal in AV1_V4R4_POSITIVE_CONTROLS
    )
    if not traversal_complete or not controls_match:
        return "invalid", "not_applicable"
    if not conflict_cells:
        return "policy_satisfied", "not_applicable"
    lever_classes = {str(cell["lever_class"]) for cell in conflict_cells}
    if "multi_axis_policy_required" in lever_classes or len(lever_classes) > 1:
        map_result = "multi_axis_policy_required"
    else:
        map_result = next(iter(lever_classes))
    hypothesis_h1 = (
        "supported"
        if map_result == "content_class_target_normalization"
        and all(
            cell["content_class"] == "live_action_content" for cell in conflict_cells
        )
        else "falsified"
    )
    return map_result, hypothesis_h1


def _assert_outcome_sequence(outcomes: Sequence[Mapping[str, Any]]) -> None:
    if len(outcomes) > AV1_V4R4_ORDINAL_COUNT:
        raise AV1V4R4OutcomeError("AV1 v4 r4 outcome sequence is too long")
    ordinals = [outcome["ordinal"] for outcome in outcomes]
    if ordinals != list(range(1, len(outcomes) + 1)):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 outcome sequence must be contiguous from ordinal one"
        )
    fatal_indexes = [
        index
        for index, outcome in enumerate(outcomes)
        if outcome["disposition"] == "fatal_failure"
    ]
    if fatal_indexes and fatal_indexes != [len(outcomes) - 1]:
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 fatal outcome must absorb the remaining sequence"
        )


def _assert_terminal_bindings(bindings: Sequence[Mapping[str, Any]]) -> None:
    if len(bindings) > AV1_V4R4_ORDINAL_COUNT:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal has too many bindings")
    expected_ordinals = list(range(1, len(bindings) + 1))
    if [binding.get("ordinal") for binding in bindings] != expected_ordinals:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal ordinals are invalid")
    allowed_keys = {
        "ordinal",
        "outcome_id",
        "payload_sha256",
        "disposition",
        "content_class",
        "conflict_reason",
        "positive_control_observation",
        "positive_control_matched",
    }
    for binding in bindings:
        if set(binding) != allowed_keys:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal outcome binding shape is invalid"
            )
        ordinal = binding["ordinal"]
        layout = _layout_for_ordinal(ordinal)
        if (
            binding["content_class"] != layout["content_class"]
            or binding["disposition"] not in AV1_V4R4_DISPOSITIONS
            or not _OUTCOME_ID_RE.fullmatch(str(binding["outcome_id"]))
            or not _SHA256_RE.fullmatch(str(binding["payload_sha256"]))
        ):
            raise AV1V4R4OutcomeError("AV1 v4 r4 terminal outcome binding is invalid")
        if (binding["disposition"] == "bounded_quality_conflict") != (
            binding["conflict_reason"] is not None
        ):
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal conflict-reason binding is invalid"
            )
        try:
            diagnostic = (
                AV1V4R4ConflictDiagnostic(
                    ordinal=ordinal,
                    asset_id=str(layout["asset_id"]),
                    failure_search_reason=str(binding["conflict_reason"] or ""),
                    conflict_quality_gap_band="within_relax_step",
                    conflict_size_gap_band="no_quality_safe_candidate",
                )
                if binding["disposition"] == "bounded_quality_conflict"
                else None
            )
        except AV1V4R4DiagnosticError as exc:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal conflict-reason binding is invalid"
            ) from exc
        observation = binding["positive_control_observation"]
        expected_control = _positive_control_match(
            ordinal=ordinal,
            disposition=str(binding["disposition"]),
            diagnostic=diagnostic,
            observation=(
                object_dict(observation) if isinstance(observation, Mapping) else None
            ),
        )
        if binding["positive_control_matched"] is not expected_control:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal positive-control binding is invalid"
            )
        if ordinal in AV1_V4R4_POSITIVE_CONTROLS:
            if expected_control is False and binding["disposition"] != "fatal_failure":
                raise AV1V4R4OutcomeError(
                    "AV1 v4 r4 terminal positive-control divergence is not fatal"
                )
        elif observation is not None or binding["positive_control_matched"] is not None:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal non-control binding has control data"
            )
    fatal_indexes = [
        index
        for index, binding in enumerate(bindings)
        if binding["disposition"] == "fatal_failure"
    ]
    if fatal_indexes and fatal_indexes != [len(bindings) - 1]:
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal does not absorb fatal failure")


def _assert_conflict_cells(
    bindings: Sequence[Mapping[str, Any]],
    conflict_cells: Sequence[Mapping[str, Any]],
) -> None:
    conflict_bindings = {
        int(binding["ordinal"]): binding
        for binding in bindings
        if binding["disposition"] == "bounded_quality_conflict"
    }
    if len(conflict_cells) != len(conflict_bindings):
        raise AV1V4R4OutcomeError("AV1 v4 r4 terminal conflict-cell count is invalid")
    cell_ordinals = [cell.get("ordinal") for cell in conflict_cells]
    if any(type(ordinal) is not int for ordinal in cell_ordinals):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 terminal conflict-cell ordinal is invalid"
        )
    if cell_ordinals != sorted(cell_ordinals):
        raise AV1V4R4OutcomeError(
            "AV1 v4 r4 terminal conflict cells are not in ordinal order"
        )
    allowed_keys = {
        "ordinal",
        "content_class",
        "failure_search_reason",
        "conflict_quality_gap_band",
        "conflict_size_gap_band",
        "lever_class",
    }
    seen: set[int] = set()
    for cell in conflict_cells:
        if set(cell) != allowed_keys or type(cell.get("ordinal")) is not int:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal conflict-cell shape is invalid"
            )
        ordinal = int(cell["ordinal"])
        if ordinal in seen or ordinal not in conflict_bindings:
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal conflict-cell ordinal is invalid"
            )
        seen.add(ordinal)
        expected_lever = av1_v4r4_cell_lever(
            quality_gap_band=str(cell["conflict_quality_gap_band"]),
            size_gap_band=str(cell["conflict_size_gap_band"]),
        )
        if (
            cell["content_class"] != conflict_bindings[ordinal]["content_class"]
            or cell["failure_search_reason"] not in AV1_V4R4_CONFLICT_REASONS
            or cell["failure_search_reason"]
            != conflict_bindings[ordinal]["conflict_reason"]
            or cell["lever_class"] != expected_lever
        ):
            raise AV1V4R4OutcomeError(
                "AV1 v4 r4 terminal conflict-cell binding is invalid"
            )


def _layout_for_ordinal(ordinal: int) -> dict[str, Any]:
    if type(ordinal) is not int or not 1 <= ordinal <= AV1_V4R4_ORDINAL_COUNT:
        raise AV1V4R4OutcomeError("AV1 v4 r4 ordinal is invalid")
    return av1_v4r4_ordinal_layout()[ordinal - 1]


def _assert_common_binding(payload: Mapping[str, Any]) -> None:
    expected = {
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R4OutcomeError("AV1 v4 r4 artifact binding is invalid")
    if payload.get("contract_version") not in {
        AV1_V4R4_OUTCOME_CONTRACT_VERSION,
        AV1_V4R4_TERMINAL_CONTRACT_VERSION,
    }:
        raise AV1V4R4OutcomeError("AV1 v4 r4 contract version is invalid")


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    if any(
        payload.get(field) is not False
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    ):
        raise AV1V4R4OutcomeError("AV1 v4 r4 artifact cannot confer authority")


def _bind_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_prefix: str,
    domain: str,
) -> dict[str, Any]:
    bound = dict(payload)
    bound[id_field] = (
        id_prefix + stable_json_hash({"domain": domain, "payload": bound})[:32]
    )
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_bound_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_pattern: re.Pattern[str],
    id_prefix: str,
    domain: str,
) -> None:
    artifact_id = str(payload.get(id_field) or "")
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {id_field, "payload_sha256"}
    }
    expected_id = (
        id_prefix + stable_json_hash({"domain": domain, "payload": semantic})[:32]
    )
    if not id_pattern.fullmatch(artifact_id) or artifact_id != expected_id:
        raise AV1V4R4OutcomeError("AV1 v4 r4 artifact ID is invalid")
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R4OutcomeError("AV1 v4 r4 artifact digest is invalid")


def _deserialize_artifact(
    data: bytes,
    assertion: Any,
    label: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R4OutcomeError(f"AV1 v4 r4 {label} bytes are unreadable") from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R4OutcomeError(f"AV1 v4 r4 {label} bytes are not canonical")
    assertion(payload)
    return payload


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R4OutcomeError("AV1 v4 r4 artifact contains machine-local text")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_text(child)


if AV1_V4R4_CONTRACT_VERSION == AV1_V4R4_OUTCOME_CONTRACT_VERSION:
    raise RuntimeError("AV1 v4 r4 contract and outcome namespaces must differ")
if set(AV1_V4R4_LEVER_CLASSES) & set(AV1_V4R4_VERDICTS):
    raise RuntimeError("AV1 v4 r4 lever and verdict vocabularies must differ")
if set(AV1_V4R4_TERMINATION_KINDS) & set(AV1_V4R4_HYPOTHESIS_RESULTS):
    raise RuntimeError("AV1 v4 r4 terminal vocabularies must differ")
