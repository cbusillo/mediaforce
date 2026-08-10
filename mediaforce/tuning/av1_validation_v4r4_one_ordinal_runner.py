"""One-ordinal AV1 v4r4 runner boundary.

The runner consumes existing owner authority and private runtime inputs.  It
publishes only public r4 registry artifacts and returns a bounded public result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import math
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.encoding.streams import resolve_stream_plan
from mediaforce.tuning.av1_validation_v4_qualification_search import run_v4_qualification_search
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_POLICY_VALUES,
    AV1_V4R4_SOURCE_LAYOUT,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_diagnostics import (
    AV1V4R4CandidateObservation,
    AV1V4R4RuntimePolicy,
    classify_av1_v4r4_conflict,
)
from mediaforce.tuning.av1_validation_v4r4_invocation import (
    av1_v4r4_mode_for_ordinal,
    av1_v4r4_runner_invocation_sha256,
    av1_v4r4_search_kwargs_for_inputs,
    av1_v4r4_video_policy_for_ordinal,
    av1_v4r4_warm_start_for_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_outcome import build_av1_v4r4_outcome
from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
    av1_v4r4_runner_production_stream_plan_identity,
    av1_v4r4_runner_stream_budget_ledger_identity,
    build_av1_v4r4_runner_admission,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryBinding,
    AV1V4R4OrdinalRegistryError,
    publish_av1_v4r4_ordinal_registry_outcome,
    publish_av1_v4r4_ordinal_registry_runner_admission_started,
)
from mediaforce.tuning.size_goals import SizeGoalIntent
from mediaforce.tuning.stream_budget import build_stream_budget_ledger
from mediaforce.tuning.target_size_search import TargetSizeSearchError


SearchQualityForSource = Callable[..., QualitySearchResult]

_PUBLIC_RESULT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "completed",
        "disposition",
        "ordinal",
        "outcome_id",
        "outcome_publication_id",
        "terminal_publication_id",
        "failure_search_reason",
        "conflict_quality_gap_band",
        "conflict_size_gap_band",
    }
)


class AV1V4R4OneOrdinalRunnerError(ValueError):
    """Raised for pre-run failures before a started artifact can be published."""


@dataclass(frozen=True, slots=True)
class AV1V4R4OneOrdinalRunResult:
    public_result: Mapping[str, Any]
    outcome: Mapping[str, Any]
    outcome_publication: Mapping[str, Any]
    terminal_publication: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class AV1V4R4OneOrdinalRuntimeInputs:
    source_path: Path
    quality_temp_path: Path
    width: int
    height: int
    source_codec: str


def run_av1_v4r4_one_ordinal(
    *,
    binding: AV1V4R4OrdinalRegistryBinding,
    qualification_request: Mapping[str, Any],
    execution_preflight: Mapping[str, Any],
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    runtime_inputs: AV1V4R4OneOrdinalRuntimeInputs,
    search_quality_for_source: SearchQualityForSource,
) -> AV1V4R4OneOrdinalRunResult:
    if not callable(search_quality_for_source):
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner inputs are invalid")
    clock = _utc_now
    ordinal = _ordinal_from_chain(sequencing_grant, sequencing_claim, execution_grant, execution_claim)
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    runtime_policy = _runtime_policy(ordinal)
    video_policy = av1_v4r4_video_policy_for_ordinal(ordinal)
    mode = av1_v4r4_mode_for_ordinal(ordinal)
    warm_start = av1_v4r4_warm_start_for_ordinal(ordinal)
    search_kwargs = _search_kwargs(runtime_inputs)
    runtime_item = _runtime_item(ordinal, video_policy)
    stream_plan = resolve_stream_plan(runtime_item)
    stream_budget_ledger = build_stream_budget_ledger(
        runtime_item,
        resolved_size_goal=SizeGoalIntent(
            mode="absolute",
            value_bytes=int(layout["target_size_bytes"]),
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=float(
                AV1_V4R4_POLICY_VALUES["sample_projection_tolerance_percent"]
            ),
            final_output_tolerance_percent=float(
                AV1_V4R4_POLICY_VALUES["final_output_tolerance_percent"]
            ),
            source="av1_v4r4_frozen_ordinal_layout",
        ).resolve(float(_source_for_ordinal(ordinal)["duration_seconds"])),
        stream_plan=stream_plan,
    )
    invocation_sha256 = av1_v4r4_runner_invocation_sha256(
        ordinal=ordinal,
        source_path=runtime_inputs.source_path,
        quality_temp_path=runtime_inputs.quality_temp_path,
        source_codec=runtime_inputs.source_codec,
        width=runtime_inputs.width,
        height=runtime_inputs.height,
    )
    admission = build_av1_v4r4_runner_admission(
        qualification_request=qualification_request,
        execution_preflight=execution_preflight,
        plan=plan,
        sequencing_grant=sequencing_grant,
        sequencing_claim=sequencing_claim,
        execution_grant=execution_grant,
        execution_claim=execution_claim,
        invocation_sha256=invocation_sha256,
        stream_budget_ledger=stream_budget_ledger.to_payload(),
        production_stream_plan=stream_plan.to_payload(),
        metric_name=runtime_policy.metric_name,
        metric_target=runtime_policy.metric_target,
        minimum_metric_score=runtime_policy.minimum_metric_score,
        relax_step=runtime_policy.relax_step,
        sample_projection_tolerance_percent=runtime_policy.sample_projection_tolerance_percent,
        final_output_tolerance_percent=runtime_policy.final_output_tolerance_percent,
        source_cap_percent=runtime_policy.source_cap_percent,
        total_target_bytes=runtime_policy.total_target_bytes,
        source_cap_total_bytes=runtime_policy.source_cap_total_bytes,
    )
    started = publish_av1_v4r4_ordinal_registry_runner_admission_started(
        binding=binding,
        plan=plan,
        sequencing_grant=sequencing_grant,
        sequencing_claim=sequencing_claim,
        execution_grant=execution_grant,
        execution_claim=execution_claim,
        admission=admission,
        clock=clock,
    ).started

    pending_interrupt: BaseException | None = None
    try:
        operation = run_v4_qualification_search(
            search_quality_for_source,
            runtime_inputs.source_path,
            video_policy,
            mode=mode,
            stream_budget_ledger=stream_budget_ledger,
            warm_start=warm_start,
            extra_search_kwargs=search_kwargs,
        )
        if operation.invocation_sha256 != admission["invocation_sha256"]:
            raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner invocation binding drifted")
        _assert_returned_trace_bindings(
            operation.quality_result.target_size_trace,
            admission=admission,
            stream_budget_ledger=stream_budget_ledger,
        )
        outcome = _outcome_from_callback_result(
            ordinal=ordinal,
            result=operation.quality_result,
            runtime_policy=runtime_policy,
        )
    except TargetSizeSearchError as exc:
        try:
            _assert_returned_trace_bindings(
                exc.trace,
                admission=admission,
                stream_budget_ledger=stream_budget_ledger,
            )
        except Exception:
            outcome = build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")
        else:
            outcome = classify_av1_v4r4_runtime_exception(ordinal=ordinal, exc=exc)
    except (KeyboardInterrupt, SystemExit) as exc:
        outcome = build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")
        pending_interrupt = exc
    except BaseException:
        outcome = build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")

    publication = publish_av1_v4r4_ordinal_registry_outcome(
        binding=binding,
        plan=plan,
        started=started,
        outcome=outcome,
        clock=clock,
    )
    result = AV1V4R4OneOrdinalRunResult(
        public_result=_public_result(outcome, publication.outcome_publication, publication.terminal_publication),
        outcome=outcome,
        outcome_publication=publication.outcome_publication,
        terminal_publication=publication.terminal_publication,
    )
    if pending_interrupt is not None:
        raise pending_interrupt
    return result


def _outcome_from_callback_result(
    *,
    ordinal: int,
    result: Any,
    runtime_policy: AV1V4R4RuntimePolicy,
) -> Mapping[str, Any]:
    if isinstance(result, QualitySearchResult):
        return build_av1_v4r4_outcome(
            ordinal=ordinal,
            disposition="selected_success",
            guided_probe_status=_guided_probe_status(result.target_size_trace),
            guided_fallback_selected=_guided_fallback_selected(result.target_size_trace),
        )
    raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner callback result is unsupported")


def _conflict_outcome_from_trace(
    *,
    ordinal: int,
    trace: Mapping[str, Any],
    runtime_policy: AV1V4R4RuntimePolicy,
) -> Mapping[str, Any]:
    diagnostic = classify_av1_v4r4_conflict(
        failure_phase="production_search",
        failure_class="target_size_search_error",
        failure_search_status=str(trace.get("status") or ""),
        failure_search_reason=str(trace.get("selection_reason") or ""),
        runtime_policy=runtime_policy,
        candidates=_candidate_observations(trace),
    )
    return build_av1_v4r4_outcome(
        ordinal=ordinal,
        disposition="bounded_quality_conflict",
        conflict_diagnostic=diagnostic,
        guided_probe_status=_guided_probe_status(trace),
        guided_fallback_selected=_guided_fallback_selected(trace),
    )


def classify_av1_v4r4_runtime_exception(
    *,
    ordinal: int,
    exc: TargetSizeSearchError,
) -> Mapping[str, Any]:
    """Return an outcome from a private TargetSizeSearchError without trusting best candidate."""

    runtime_policy = _runtime_policy(ordinal)
    if exc.status != "quality_conflict":
        return build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")
    try:
        return _conflict_outcome_from_trace(
            ordinal=ordinal,
            trace=object_dict(exc.trace),
            runtime_policy=runtime_policy,
        )
    except Exception:
        return build_av1_v4r4_outcome(ordinal=ordinal, disposition="fatal_failure")


def _candidate_observations(trace: Mapping[str, Any]) -> tuple[AV1V4R4CandidateObservation, ...]:
    candidates = object_list(trace.get("candidates"))
    observations: list[AV1V4R4CandidateObservation] = []
    for raw in candidates:
        candidate = object_dict(raw)
        projection = candidate.get("predicted_whole_episode_bytes")
        if type(projection) is not int:
            raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner candidate projection is invalid")
        metric = str(candidate.get("metric") or "").lower()
        observations.append(
            AV1V4R4CandidateObservation(
                attempt=_strict_int(candidate.get("attempt")),
                metric_name=metric,
                metric_target=_strict_finite_float(candidate.get("metric_target")),
                metric_score=_strict_finite_float(candidate.get("metric_score")),
                minimum_metric_score=_strict_finite_float(candidate.get("min_metric_score")),
                projected_whole_output_bytes=projection,
            )
        )
    return tuple(observations)


def _runtime_policy(ordinal: int) -> AV1V4R4RuntimePolicy:
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    return AV1V4R4RuntimePolicy(
        ordinal=ordinal,
        asset_id=str(layout["asset_id"]),
        metric_name=str(AV1_V4R4_POLICY_VALUES["quality_metric"]),
        metric_target=float(AV1_V4R4_POLICY_VALUES["target_vmaf"]),
        minimum_metric_score=float(AV1_V4R4_POLICY_VALUES["min_target_vmaf"]),
        relax_step=float(AV1_V4R4_POLICY_VALUES["target_relax_step_vmaf"]),
        sample_projection_tolerance_percent=int(AV1_V4R4_POLICY_VALUES["sample_projection_tolerance_percent"]),
        final_output_tolerance_percent=int(AV1_V4R4_POLICY_VALUES["final_output_tolerance_percent"]),
        source_cap_percent=int(AV1_V4R4_POLICY_VALUES["max_encoded_percent"]),
        total_target_bytes=int(layout["target_size_bytes"]),
        source_cap_total_bytes=int(layout["source_cap_total_bytes"]),
    )


def _ordinal_from_chain(*records: Mapping[str, Any]) -> int:
    ordinals = [record.get("ordinal") for record in records]
    if len(set(ordinals)) != 1 or type(ordinals[0]) is not int:
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner ordinal chain is invalid")
    ordinal = int(ordinals[0])
    if not 1 <= ordinal <= len(av1_v4r4_ordinal_layout()):
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner ordinal is invalid")
    return ordinal


def _public_result(
    outcome: Mapping[str, Any],
    publication: Mapping[str, Any],
    terminal: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = {
        "schema": "mediaforce.av1_cold_start_v4r4_one_ordinal_cli_result",
        "schema_version": 1,
        "completed": outcome["completed"],
        "disposition": outcome["disposition"],
        "ordinal": outcome["ordinal"],
        "outcome_id": outcome["outcome_id"],
        "outcome_publication_id": publication["outcome_publication_id"],
        "terminal_publication_id": None if terminal is None else terminal["terminal_publication_id"],
        "failure_search_reason": None,
        "conflict_quality_gap_band": None,
        "conflict_size_gap_band": None,
    }
    diagnostic = object_dict(outcome.get("conflict_diagnostic"))
    if diagnostic:
        result["failure_search_reason"] = diagnostic["failure_search_reason"]
        result["conflict_quality_gap_band"] = diagnostic["conflict_quality_gap_band"]
        result["conflict_size_gap_band"] = diagnostic["conflict_size_gap_band"]
    if set(result) != _PUBLIC_RESULT_KEYS:
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 public result shape is invalid")
    return result


def _guided_probe_status(trace: Any) -> str | None:
    warm = object_dict(object_dict(trace).get("warm_start"))
    status = str(warm.get("status") or "")
    reason = str(warm.get("fallback_reason") or "")
    normalized_reason = "size_band_miss" if reason in {"size_band_miss", "target_band_miss"} else reason
    if status == "rejected_fallback" and warm.get("fallback_used") is True and normalized_reason == "size_band_miss":
        return "size_band_miss"
    return None


def _guided_fallback_selected(trace: Any) -> bool | None:
    if _guided_probe_status(trace) == "size_band_miss":
        return True
    return None


def _runtime_item(ordinal: int, video_policy: Mapping[str, Any]) -> dict[str, Any]:
    source = _source_for_ordinal(ordinal)
    return {
        "library_item_id": source["asset_id"],
        "rel_path": f"av1-v4r4/{source['asset_id']}.mkv",
        "source_fingerprint": source["media_sha256"],
        "source_size_bytes": source["media_bytes"],
        "video_bitrate": int(round(float(source["media_bytes"]) * 8 / float(source["duration_seconds"]))),
        "duration_seconds": source["duration_seconds"],
        "output_container": "mkv",
        "audio_summary": [],
        "subtitle_summary": [],
        "attachment_summary": [],
        "resolved_policy": {"video": dict(video_policy), "audio": {}, "subtitle": {}},
    }


def _source_for_ordinal(ordinal: int) -> Mapping[str, Any]:
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    for source in AV1_V4R4_SOURCE_LAYOUT:
        if source["asset_id"] == layout["asset_id"]:
            return source
    raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 source layout binding is invalid")


def _video_policy_for_ordinal(ordinal: int) -> dict[str, Any]:
    return av1_v4r4_video_policy_for_ordinal(ordinal)


def _warm_start_for_ordinal(ordinal: int) -> Any:
    return av1_v4r4_warm_start_for_ordinal(ordinal)


def _search_kwargs(runtime_inputs: AV1V4R4OneOrdinalRuntimeInputs) -> dict[str, Any]:
    if not isinstance(runtime_inputs, AV1V4R4OneOrdinalRuntimeInputs):
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 private runtime inputs are invalid")
    return av1_v4r4_search_kwargs_for_inputs(
        source_codec=runtime_inputs.source_codec,
        width=runtime_inputs.width,
        height=runtime_inputs.height,
        quality_temp_path=runtime_inputs.quality_temp_path,
    )


def _assert_returned_trace_bindings(
    target_size_trace: Any,
    *,
    admission: Mapping[str, Any],
    stream_budget_ledger: Any,
) -> None:
    trace = object_dict(target_size_trace)
    ledger_trace = object_dict(trace.get("ledger"))
    if not ledger_trace:
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner target trace binding drifted")
    expected_ledger = av1_v4r4_runner_stream_budget_ledger_identity(
        stream_budget_ledger.to_payload(),
    )
    expected_plan = av1_v4r4_runner_production_stream_plan_identity(
        stream_budget_ledger.stream_plan.to_payload(),
    )
    if (
        ledger_trace.get("ledger_id") != expected_ledger["ledger_id"]
        or ledger_trace.get("source_id") != stream_budget_ledger.source_id
        or ledger_trace.get("stream_plan_id") != expected_plan["stream_plan_id"]
        or admission.get("stream_budget_ledger_identity") != expected_ledger
        or admission.get("production_stream_plan_identity") != expected_plan
    ):
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner target trace binding drifted")


def _strict_int(value: Any) -> int:
    if type(value) is not int:
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner candidate integer is invalid")
    return value


def _strict_finite_float(value: Any) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise AV1V4R4OneOrdinalRunnerError("AV1 v4 r4 runner candidate number is invalid")
    return float(value)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
