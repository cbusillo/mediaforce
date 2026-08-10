"""Pure bounded conflict diagnostics for AV1 protocol-v4 revision 4."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1V4R4ContractError,
    AV1_V4R4_CONFLICT_REASONS,
    AV1_V4R4_POLICY_VALUES,
    AV1_V4R4_QUALITY_GAP_BANDS,
    AV1_V4R4_SIZE_GAP_BANDS,
    av1_v4r4_ordinal_layout,
    assert_av1_v4r4_runtime_policy,
)


AV1_V4R4_CONFLICT_PHASE = "production_search"
AV1_V4R4_CONFLICT_CLASS = "target_size_search_error"
AV1_V4R4_CONFLICT_STATUS = "quality_conflict"
AV1_V4R4_DIAGNOSTIC_SCHEMA = "mediaforce.av1_cold_start_v4r4_conflict_diagnostic"
AV1_V4R4_DIAGNOSTIC_SCHEMA_VERSION = 1
AV1_V4R4_DIAGNOSTIC_CONTRACT_VERSION = "av1v4r4diag1"

_DIAGNOSTIC_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "failure_phase",
    "failure_class",
    "failure_search_status",
    "failure_search_reason",
    "ordinal",
    "asset_id",
    "conflict_quality_gap_band",
    "conflict_size_gap_band",
}


class AV1V4R4DiagnosticError(ValueError):
    """Raised when conflict data cannot produce a bounded r4 diagnostic."""


@dataclass(frozen=True, slots=True)
class AV1V4R4CandidateObservation:
    attempt: int
    metric_name: str
    metric_target: float
    metric_score: float
    minimum_metric_score: float
    projected_whole_output_bytes: int

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or self.attempt < 1:
            raise AV1V4R4DiagnosticError("AV1 v4 r4 candidate attempt is invalid")
        if self.metric_name not in {"vmaf", "xpsnr"}:
            raise AV1V4R4DiagnosticError("AV1 v4 r4 candidate metric is invalid")
        for label, value in (
            ("metric target", self.metric_target),
            ("metric score", self.metric_score),
            ("minimum metric score", self.minimum_metric_score),
        ):
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise AV1V4R4DiagnosticError(f"AV1 v4 r4 candidate {label} is invalid")
        if (
            type(self.projected_whole_output_bytes) is not int
            or self.projected_whole_output_bytes <= 0
        ):
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 candidate whole-output projection is invalid"
            )


@dataclass(frozen=True, slots=True)
class AV1V4R4RuntimePolicy:
    ordinal: int
    asset_id: str
    metric_name: str
    metric_target: float
    minimum_metric_score: float
    relax_step: float
    sample_projection_tolerance_percent: int
    final_output_tolerance_percent: int
    source_cap_percent: int
    total_target_bytes: int
    source_cap_total_bytes: int

    def __post_init__(self) -> None:
        try:
            assert_av1_v4r4_runtime_policy(
                metric_name=self.metric_name,
                metric_target=self.metric_target,
                minimum_metric_score=self.minimum_metric_score,
                relax_step=self.relax_step,
                sample_projection_tolerance_percent=(
                    self.sample_projection_tolerance_percent
                ),
                final_output_tolerance_percent=self.final_output_tolerance_percent,
                source_cap_percent=self.source_cap_percent,
            )
        except AV1V4R4ContractError as exc:
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 runtime policy does not match the frozen policy"
            ) from exc
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= len(
            av1_v4r4_ordinal_layout()
        ):
            raise AV1V4R4DiagnosticError("AV1 v4 r4 runtime ordinal is invalid")
        layout = av1_v4r4_ordinal_layout()[self.ordinal - 1]
        if self.asset_id != layout["asset_id"]:
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 runtime asset does not match the frozen ordinal"
            )
        if (
            type(self.total_target_bytes) is not int
            or self.total_target_bytes != layout["target_size_bytes"]
        ):
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 total target bytes do not match the frozen ordinal"
            )
        if (
            type(self.source_cap_total_bytes) is not int
            or self.source_cap_total_bytes != layout["source_cap_total_bytes"]
        ):
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 source cap bytes do not match the frozen ordinal"
            )


@dataclass(frozen=True, slots=True)
class AV1V4R4ConflictDiagnostic:
    ordinal: int
    asset_id: str
    failure_search_reason: str
    conflict_quality_gap_band: str
    conflict_size_gap_band: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= len(
            av1_v4r4_ordinal_layout()
        ):
            raise AV1V4R4DiagnosticError("AV1 v4 r4 diagnostic ordinal is invalid")
        if self.asset_id != av1_v4r4_ordinal_layout()[self.ordinal - 1]["asset_id"]:
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 diagnostic asset does not match the frozen ordinal"
            )
        if self.failure_search_reason not in AV1_V4R4_CONFLICT_REASONS:
            raise AV1V4R4DiagnosticError("AV1 v4 r4 conflict reason is not frozen")
        if self.conflict_quality_gap_band not in AV1_V4R4_QUALITY_GAP_BANDS:
            raise AV1V4R4DiagnosticError("AV1 v4 r4 quality-gap band is not frozen")
        if self.conflict_size_gap_band not in AV1_V4R4_SIZE_GAP_BANDS:
            raise AV1V4R4DiagnosticError("AV1 v4 r4 size-gap band is not frozen")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_V4R4_DIAGNOSTIC_SCHEMA,
            "schema_version": AV1_V4R4_DIAGNOSTIC_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_DIAGNOSTIC_CONTRACT_VERSION,
            "failure_phase": AV1_V4R4_CONFLICT_PHASE,
            "failure_class": AV1_V4R4_CONFLICT_CLASS,
            "failure_search_status": AV1_V4R4_CONFLICT_STATUS,
            "failure_search_reason": self.failure_search_reason,
            "ordinal": self.ordinal,
            "asset_id": self.asset_id,
            "conflict_quality_gap_band": self.conflict_quality_gap_band,
            "conflict_size_gap_band": self.conflict_size_gap_band,
        }


def classify_av1_v4r4_conflict(
    *,
    failure_phase: str,
    failure_class: str,
    failure_search_status: str,
    failure_search_reason: str,
    runtime_policy: AV1V4R4RuntimePolicy,
    candidates: Sequence[AV1V4R4CandidateObservation],
) -> AV1V4R4ConflictDiagnostic:
    if (
        failure_phase != AV1_V4R4_CONFLICT_PHASE
        or failure_class != AV1_V4R4_CONFLICT_CLASS
        or failure_search_status != AV1_V4R4_CONFLICT_STATUS
    ):
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 conflict boundary does not match the frozen failure tuple"
        )
    if failure_search_reason not in AV1_V4R4_CONFLICT_REASONS:
        raise AV1V4R4DiagnosticError("AV1 v4 r4 conflict reason is not frozen")
    observations = tuple(candidates)
    if not observations:
        raise AV1V4R4DiagnosticError("AV1 v4 r4 conflict candidates are missing")
    attempts = [candidate.attempt for candidate in observations]
    if len(set(attempts)) != len(attempts):
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 conflict candidate attempts must be distinct"
        )
    _assert_candidate_policy_bindings(observations, runtime_policy)
    quality_reference = _quality_reference_candidate(
        reason=failure_search_reason,
        candidates=observations,
        runtime_policy=runtime_policy,
    )
    quality_gap = runtime_policy.minimum_metric_score - quality_reference.metric_score
    if not math.isfinite(quality_gap) or quality_gap <= 0:
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 conflict quality gap must be finite and positive"
        )
    quality_band = classify_av1_v4r4_quality_gap_band(
        metric_name=runtime_policy.metric_name,
        quality_gap=quality_gap,
    )
    size_band = classify_av1_v4r4_size_gap_band(
        candidates=observations,
        runtime_policy=runtime_policy,
    )
    return AV1V4R4ConflictDiagnostic(
        ordinal=runtime_policy.ordinal,
        asset_id=runtime_policy.asset_id,
        failure_search_reason=failure_search_reason,
        conflict_quality_gap_band=quality_band,
        conflict_size_gap_band=size_band,
    )


def classify_av1_v4r4_quality_gap_band(
    *,
    metric_name: str,
    quality_gap: float,
) -> str:
    if type(quality_gap) not in {int, float} or not math.isfinite(float(quality_gap)):
        raise AV1V4R4DiagnosticError("AV1 v4 r4 quality gap is invalid")
    gap = float(quality_gap)
    if gap <= 0:
        raise AV1V4R4DiagnosticError("AV1 v4 r4 quality gap must be strictly positive")
    if metric_name == "vmaf":
        relax_step = float(AV1_V4R4_POLICY_VALUES["target_relax_step_vmaf"])
        target_floor_span = float(AV1_V4R4_POLICY_VALUES["target_vmaf"]) - float(
            AV1_V4R4_POLICY_VALUES["min_target_vmaf"]
        )
    elif metric_name == "xpsnr":
        relax_step = float(AV1_V4R4_POLICY_VALUES["target_relax_step_xpsnr"])
        target_floor_span = float(AV1_V4R4_POLICY_VALUES["target_xpsnr"]) - float(
            AV1_V4R4_POLICY_VALUES["min_target_xpsnr"]
        )
    else:
        raise AV1V4R4DiagnosticError("AV1 v4 r4 quality metric is invalid")
    if gap <= relax_step:
        return "within_relax_step"
    if gap <= target_floor_span:
        return "within_target_floor_span"
    if gap <= target_floor_span * 2:
        return "within_two_target_floor_spans"
    return "beyond_two_target_floor_spans"


def assert_av1_v4r4_conflict_diagnostic(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _DIAGNOSTIC_KEYS:
        raise AV1V4R4DiagnosticError("AV1 v4 r4 conflict diagnostic shape is invalid")
    expected = {
        "schema": AV1_V4R4_DIAGNOSTIC_SCHEMA,
        "schema_version": AV1_V4R4_DIAGNOSTIC_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_DIAGNOSTIC_CONTRACT_VERSION,
        "failure_phase": AV1_V4R4_CONFLICT_PHASE,
        "failure_class": AV1_V4R4_CONFLICT_CLASS,
        "failure_search_status": AV1_V4R4_CONFLICT_STATUS,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R4DiagnosticError("AV1 v4 r4 conflict diagnostic binding is invalid")
    AV1V4R4ConflictDiagnostic(
        ordinal=materialized.get("ordinal"),
        asset_id=str(materialized.get("asset_id") or ""),
        failure_search_reason=str(materialized.get("failure_search_reason") or ""),
        conflict_quality_gap_band=str(
            materialized.get("conflict_quality_gap_band") or ""
        ),
        conflict_size_gap_band=str(materialized.get("conflict_size_gap_band") or ""),
    )


def _assert_candidate_policy_bindings(
    candidates: Sequence[AV1V4R4CandidateObservation],
    runtime_policy: AV1V4R4RuntimePolicy,
) -> None:
    for candidate in candidates:
        if (
            candidate.metric_name != runtime_policy.metric_name
            or candidate.metric_target != runtime_policy.metric_target
            or candidate.minimum_metric_score != runtime_policy.minimum_metric_score
        ):
            raise AV1V4R4DiagnosticError(
                "AV1 v4 r4 candidate policy binding is invalid"
            )


def _quality_reference_candidate(
    *,
    reason: str,
    candidates: Sequence[AV1V4R4CandidateObservation],
    runtime_policy: AV1V4R4RuntimePolicy,
) -> AV1V4R4CandidateObservation:
    if reason == "target_band_violates_quality_floor":
        pool = [
            candidate
            for candidate in candidates
            if _inside_sample_band(candidate, runtime_policy)
            and not _violates_source_cap(candidate, runtime_policy)
        ]
    elif reason == "target_requires_crossing_quality_floor":
        pool = [
            candidate
            for candidate in candidates
            if _at_or_below_sample_upper(candidate, runtime_policy)
            and not _violates_source_cap(candidate, runtime_policy)
        ]
    else:
        pool = [
            candidate
            for candidate in candidates
            if not _violates_source_cap(candidate, runtime_policy)
        ]
    if not pool:
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 conflict reason has no consistent quality reference"
        )
    return min(
        pool,
        key=lambda candidate: (
            -candidate.metric_score,
            abs(
                candidate.projected_whole_output_bytes
                - runtime_policy.total_target_bytes
            ),
            candidate.attempt,
        ),
    )


def classify_av1_v4r4_size_gap_band(
    *,
    candidates: Sequence[AV1V4R4CandidateObservation],
    runtime_policy: AV1V4R4RuntimePolicy,
) -> str:
    quality_safe = [
        candidate
        for candidate in candidates
        if candidate.metric_score >= runtime_policy.minimum_metric_score
    ]
    if not quality_safe:
        return "no_quality_safe_candidate"
    non_cap = [
        candidate
        for candidate in quality_safe
        if not _violates_source_cap(candidate, runtime_policy)
    ]
    if not non_cap:
        return "over_source_cap"
    sample_lower_percent = 100 - runtime_policy.sample_projection_tolerance_percent
    if any(
        candidate.projected_whole_output_bytes * 100
        >= runtime_policy.total_target_bytes * sample_lower_percent
        and candidate.projected_whole_output_bytes <= runtime_policy.total_target_bytes
        for candidate in non_cap
    ):
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 quality-safe candidate contradicts the conflict outcome"
        )
    reference = min(
        non_cap,
        key=lambda candidate: (
            abs(
                candidate.projected_whole_output_bytes
                - runtime_policy.total_target_bytes
            ),
            -candidate.metric_score,
            candidate.attempt,
        ),
    )
    projection = reference.projected_whole_output_bytes
    target = runtime_policy.total_target_bytes
    sample_tolerance = runtime_policy.sample_projection_tolerance_percent
    final_tolerance = runtime_policy.final_output_tolerance_percent
    if projection * 100 < target * (100 - sample_tolerance):
        return "below_sample_lower_bound"
    if projection <= target:
        raise AV1V4R4DiagnosticError(
            "AV1 v4 r4 quality-safe candidate contradicts the conflict outcome"
        )
    if projection * 100 <= target * (100 + final_tolerance):
        return "within_final_tolerance_above_target"
    if projection * 100 <= target * (100 + sample_tolerance):
        return "within_projection_tolerance_above_target"
    return "beyond_projection_tolerance_within_source_cap"


def _inside_sample_band(
    candidate: AV1V4R4CandidateObservation,
    runtime_policy: AV1V4R4RuntimePolicy,
) -> bool:
    projection = candidate.projected_whole_output_bytes * 100
    target = runtime_policy.total_target_bytes
    tolerance = runtime_policy.sample_projection_tolerance_percent
    return target * (100 - tolerance) <= projection <= target * (100 + tolerance)


def _at_or_below_sample_upper(
    candidate: AV1V4R4CandidateObservation,
    runtime_policy: AV1V4R4RuntimePolicy,
) -> bool:
    return (
        candidate.projected_whole_output_bytes * 100
        <= runtime_policy.total_target_bytes
        * (100 + runtime_policy.sample_projection_tolerance_percent)
    )


def _violates_source_cap(
    candidate: AV1V4R4CandidateObservation,
    runtime_policy: AV1V4R4RuntimePolicy,
) -> bool:
    return (
        candidate.projected_whole_output_bytes > runtime_policy.source_cap_total_bytes
    )
