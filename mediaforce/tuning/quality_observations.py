from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import PurePosixPath
from typing import Any, Mapping

from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import quality_search_observations
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.quality import QualitySearchResult, parse_quality_search_measurements
from mediaforce.library.media_scopes import MediaScope, scope_rel_path_filter
from mediaforce.tuning.quality_memory import (
    QualitySearchContext,
    accepted_quality_outcomes,
)

QUALITY_OBSERVATION_SCHEMA_VERSION = 1
QUALITY_OBSERVATION_TRACE_VERSION = 1
MAX_QUALITY_OBSERVATION_CANDIDATES = 32
MAX_QUALITY_OBSERVATION_FINAL_ATTEMPTS = 4

AUTHORITY_STAGED_BACKFILL = "staged_backfill"
AUTHORITY_RUNTIME_NATIVE = "runtime_native"
AUTHORITY_CORRECTION = "correction"

OUTCOME_SELECTED = "selected"
OUTCOME_DETERMINISTIC_FAILURE = "deterministic_search_failure"
OUTCOME_FINAL_SIZE_FAILURE = "final_size_failure"


class QualityObservationConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QualitySearchObservation:
    observation_id: str
    search_run_id: str
    revision: int
    supersedes_observation_id: str | None
    supersession_reason: str | None
    schema_version: int
    library_item_id: int
    prefix: str
    source_rel_path: str
    source_fingerprint: str | None
    search_signature_id: str | None
    policy_hash: str | None
    search_objective: str
    quality_metric: str
    outcome_kind: str
    authority: str
    learning_eligible: bool
    exclusion_reason: str | None
    selected_crf: float | None
    selected_target: float | None
    selected_score: float | None
    actual_output_bytes: int | None
    candidate_count: int
    search_duration_seconds: float | None
    context_json: str
    bounds_json: str
    candidate_trace_json: str
    outcome_json: str
    timing_json: str
    provenance_json: str
    shadow_json: str | None
    payload_sha256: str
    recorded_at: str

    def values(self) -> dict[str, Any]:
        return {**asdict(self), "learning_eligible": int(self.learning_eligible)}


@dataclass(frozen=True, slots=True)
class QualityObservationBackfillResult:
    inserted: int
    existing: int
    excluded: tuple[tuple[str, int], ...]


def build_selected_quality_observation(
        *,
        search_run_id: str,
        library_item_id: int,
        source_rel_path: str,
        source_fingerprint: str | None,
        context: QualitySearchContext | None,
        partial_context: Mapping[str, Any] | None = None,
        policy_hash: str | None,
        quality_result: QualitySearchResult,
        configured_min_crf: float | None,
        configured_max_crf: float | None,
        max_encoded_percent: float | None,
        search_started_at: str | None,
        search_completed_at: str | None,
        search_duration_seconds: float | None,
        retry_measurement_seconds: float | None = None,
        encode_completed_at: str | None,
        encode_duration_seconds: float | None,
        workflow_duration_seconds: float | None = None,
        quality_memory_arm: str | None = None,
        quality_memory_experiment_version: str | None = None,
        quality_memory_scope: str | None = None,
        quality_memory_scope_prefix: str | None = None,
        quality_memory_search_signature_id: str | None = None,
        actual_output_bytes: int | None,
        size_ratio: float | None,
        provenance: Mapping[str, Any],
        recorded_at: str,
        shadow_payload: Mapping[str, Any] | None = None,
        authority: str = AUTHORITY_RUNTIME_NATIVE,
        outcome_kind: str = OUTCOME_SELECTED,
        learning_eligible: bool | None = None,
        exclusion_reason: str | None = None,
        revision: int = 0,
        supersedes_observation_id: str | None = None,
        supersession_reason: str | None = None,
) -> QualitySearchObservation:
    warm_start_trace = _quality_result_warm_start_trace(quality_result)
    warm_start_status = str(warm_start_trace.get("status") or "") or None
    warm_start_selected = warm_start_status == "accepted"
    resolved_learning_eligible = (
        False
        if warm_start_selected
        else context is not None if learning_eligible is None else learning_eligible
    )
    resolved_exclusion_reason = (
        ("warm_start_selected" if warm_start_selected else exclusion_reason)
        if not resolved_learning_eligible
        else None
    )
    if not resolved_learning_eligible and resolved_exclusion_reason is None:
        resolved_exclusion_reason = "search_context_incomplete"
    trace, candidate_count = _selected_candidate_trace(quality_result)
    bounds = _search_bounds(
        context=context,
        target_size_trace=quality_result.target_size_trace,
        quality_search_trace=quality_result.quality_search_trace,
        configured_min_crf=configured_min_crf,
        configured_max_crf=configured_max_crf,
        max_encoded_percent=max_encoded_percent,
        candidates=object_list(trace.get("candidates")),
    )
    selected_candidate = object_dict(trace.get("selected_candidate"))
    predicted_output_bytes = int_value(
        selected_candidate.get("predicted_whole_episode_bytes")
        or selected_candidate.get("predicted_encode_size_bytes")
    ) or None
    final_output = object_dict(object_dict(quality_result.target_size_trace).get("final_output"))
    outcome = {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "search_selected": True,
        "terminal_success": outcome_kind == OUTCOME_SELECTED,
        "quality_margin": (
            round(quality_result.score - context.minimum_quality_score, 3)
            if context is not None
            else None
        ),
        "predicted_output_bytes": predicted_output_bytes,
        "actual_output_bytes": actual_output_bytes,
        "size_ratio": size_ratio,
        "final_size_status": str(final_output.get("status") or "") or None,
        "final_size_error_percent": _size_error_percent(
            actual_output_bytes,
            context.size_target_bytes if context is not None else None,
        ),
        "quality_memory_arm": quality_memory_arm,
        "quality_memory_experiment_version": quality_memory_experiment_version,
        "quality_memory_scope": quality_memory_scope,
        "quality_memory_scope_prefix": quality_memory_scope_prefix,
        "quality_memory_search_signature_id": quality_memory_search_signature_id,
        "quality_memory_attempted": warm_start_trace.get("attempted") is True,
        "exploration_kind": (
            "warm_start_accepted"
            if warm_start_selected
            else "warm_start_fallback" if warm_start_status
            else "quality_memory_holdout" if quality_memory_arm == "baseline_holdout"
            else "full_search"
        ),
    }
    timing = _timing_payload(
        search_started_at=search_started_at,
        search_completed_at=search_completed_at,
        search_duration_seconds=search_duration_seconds,
        retry_measurement_seconds=retry_measurement_seconds,
        encode_completed_at=encode_completed_at,
        encode_duration_seconds=encode_duration_seconds,
        workflow_duration_seconds=workflow_duration_seconds,
    )
    return _build_observation(
        search_run_id=search_run_id,
        revision=revision,
        supersedes_observation_id=supersedes_observation_id,
        supersession_reason=supersession_reason,
        library_item_id=library_item_id,
        source_rel_path=source_rel_path,
        source_fingerprint=source_fingerprint,
        context=context,
        partial_context=partial_context,
        policy_hash=policy_hash,
        outcome_kind=outcome_kind,
        authority=authority,
        learning_eligible=resolved_learning_eligible,
        exclusion_reason=resolved_exclusion_reason,
        selected_crf=quality_result.crf,
        selected_target=quality_result.target,
        selected_score=quality_result.score,
        actual_output_bytes=actual_output_bytes,
        candidate_count=candidate_count,
        search_duration_seconds=search_duration_seconds,
        bounds=bounds,
        candidate_trace=trace,
        outcome=outcome,
        timing=timing,
        provenance=provenance,
        shadow_payload=shadow_payload,
        recorded_at=recorded_at,
    )


def build_failed_quality_observation(
        *,
        search_run_id: str,
        library_item_id: int,
        source_rel_path: str,
        source_fingerprint: str | None,
        context: QualitySearchContext | None,
        partial_context: Mapping[str, Any],
        policy_hash: str | None,
        quality_metric: str,
        search_objective: str,
        exclusion_reason: str,
        target_size_trace: Mapping[str, Any] | None,
        quality_search_trace: Mapping[str, Any] | None,
        quality_error_text: str | None,
        configured_min_crf: float | None,
        configured_max_crf: float | None,
        max_encoded_percent: float | None,
        search_started_at: str | None,
        search_completed_at: str | None,
        search_duration_seconds: float | None,
        workflow_duration_seconds: float | None = None,
        quality_memory_arm: str | None = None,
        quality_memory_experiment_version: str | None = None,
        quality_memory_scope: str | None = None,
        quality_memory_scope_prefix: str | None = None,
        quality_memory_search_signature_id: str | None = None,
        quality_memory_attempted: bool = False,
        provenance: Mapping[str, Any],
        recorded_at: str,
        shadow_payload: Mapping[str, Any] | None = None,
        outcome_kind: str = OUTCOME_DETERMINISTIC_FAILURE,
) -> QualitySearchObservation:
    trace, candidate_count = _failed_candidate_trace(
        target_size_trace=target_size_trace,
        quality_search_trace=quality_search_trace,
        quality_error_text=quality_error_text,
    )
    bounds = _search_bounds(
        context=context,
        target_size_trace=target_size_trace,
        quality_search_trace=quality_search_trace,
        configured_min_crf=configured_min_crf,
        configured_max_crf=configured_max_crf,
        max_encoded_percent=max_encoded_percent,
        candidates=object_list(trace.get("candidates")),
    )
    best_candidate = object_dict(trace.get("best_reachable_candidate"))
    if not best_candidate:
        candidates = object_list(trace.get("candidates"))
        best_candidate = object_dict(candidates[-1]) if candidates else {}
    quality_floor = object_dict(object_dict(target_size_trace).get("quality_floor"))
    selected_target = context.target if context is not None else float_value(quality_floor.get("target")) or None
    outcome = {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "search_selected": False,
        "terminal_success": False,
        "status": str(object_dict(target_size_trace).get("status") or "") or None,
        "reason": str(object_dict(target_size_trace).get("selection_reason") or "") or exclusion_reason,
        "best_candidate": best_candidate or None,
        "quality_memory_arm": quality_memory_arm,
        "quality_memory_experiment_version": quality_memory_experiment_version,
        "quality_memory_scope": quality_memory_scope,
        "quality_memory_scope_prefix": quality_memory_scope_prefix,
        "quality_memory_search_signature_id": quality_memory_search_signature_id,
        "quality_memory_attempted": quality_memory_attempted,
    }
    timing = _timing_payload(
        search_started_at=search_started_at,
        search_completed_at=search_completed_at,
        search_duration_seconds=search_duration_seconds,
        retry_measurement_seconds=None,
        encode_completed_at=None,
        encode_duration_seconds=None,
        workflow_duration_seconds=workflow_duration_seconds,
    )
    return _build_observation(
        search_run_id=search_run_id,
        revision=0,
        supersedes_observation_id=None,
        supersession_reason=None,
        library_item_id=library_item_id,
        source_rel_path=source_rel_path,
        source_fingerprint=source_fingerprint,
        context=context,
        partial_context={**partial_context, "search_objective": search_objective, "metric": quality_metric},
        policy_hash=policy_hash,
        outcome_kind=outcome_kind,
        authority=AUTHORITY_RUNTIME_NATIVE,
        learning_eligible=False,
        exclusion_reason=exclusion_reason,
        selected_crf=None,
        selected_target=selected_target,
        selected_score=None,
        actual_output_bytes=None,
        candidate_count=candidate_count,
        search_duration_seconds=search_duration_seconds,
        bounds=bounds,
        candidate_trace=trace,
        outcome=outcome,
        timing=timing,
        provenance=provenance,
        shadow_payload=shadow_payload,
        recorded_at=recorded_at,
    )


def append_quality_search_observation(
        connection: DBClient,
        observation: QualitySearchObservation,
) -> bool:
    result = connection.execute(
        sqlite_insert(quality_search_observations)
        .values(**observation.values())
        .on_conflict_do_nothing()
    )
    if result.rowcount:
        return True
    existing_hash = connection.execute(
        select(quality_search_observations.c.payload_sha256)
        .where(quality_search_observations.c.observation_id == observation.observation_id)
    ).scalar_one_or_none()
    if existing_hash == observation.payload_sha256:
        return False
    conflicting_hash = connection.execute(
        select(quality_search_observations.c.payload_sha256)
        .where(quality_search_observations.c.search_run_id == observation.search_run_id)
        .where(quality_search_observations.c.authority == observation.authority)
        .where(quality_search_observations.c.revision == observation.revision)
    ).scalar_one_or_none()
    if conflicting_hash == observation.payload_sha256:
        return False
    raise QualityObservationConflictError(
        f"Conflicting quality-search observation for {observation.search_run_id} "
        f"authority={observation.authority} revision={observation.revision}"
    )


def load_current_quality_search_observations(
        connection: DBClient,
        *,
        recorded_before: str | None = None,
        scope: MediaScope | None = None,
) -> list[Mapping[str, Any]]:
    authority_rank = case(
        (quality_search_observations.c.authority == AUTHORITY_CORRECTION, 30),
        (quality_search_observations.c.authority == AUTHORITY_RUNTIME_NATIVE, 20),
        else_=10,
    )
    ranked_query = select(
        *quality_search_observations.c,
        func.row_number().over(
            partition_by=quality_search_observations.c.search_run_id,
            order_by=(authority_rank.desc(), quality_search_observations.c.revision.desc()),
        ).label("observation_rank"),
    )
    if recorded_before is not None:
        ranked_query = ranked_query.where(quality_search_observations.c.recorded_at < recorded_before)
    ranked = ranked_query.subquery()
    current_query = select(ranked).where(ranked.c.observation_rank == 1)
    if scope is not None:
        current_query = current_query.where(scope_rel_path_filter(ranked.c.source_rel_path, scope))
    return list(
        connection.execute(current_query).mappings()
    )


def backfill_quality_search_observations(
        connection: DBClient,
        *,
        as_of: datetime,
) -> QualityObservationBackfillResult:
    outcomes, exclusions = accepted_quality_outcomes(connection, metric=None, as_of=as_of)
    inserted = 0
    existing = 0
    recorded_at = _timestamp(as_of)
    for outcome in outcomes:
        search_run_id = outcome.quality_search_run_id or _historical_search_run_id(outcome)
        quality_result = QualitySearchResult(
            crf=outcome.chosen_crf,
            metric=outcome.quality_metric,
            target=outcome.quality_target,
            score=outcome.quality_score,
            stdout="",
            target_size_trace=outcome.target_size_trace,
        )
        observation = build_selected_quality_observation(
            search_run_id=search_run_id,
            library_item_id=outcome.library_item_id,
            source_rel_path=outcome.rel_path,
            source_fingerprint=outcome.source_fingerprint,
            context=outcome.context,
            policy_hash=None,
            quality_result=quality_result,
            configured_min_crf=None,
            configured_max_crf=None,
            max_encoded_percent=None,
            search_started_at=None,
            search_completed_at=None,
            search_duration_seconds=None,
            encode_completed_at=_timestamp(outcome.encode_completed_at),
            encode_duration_seconds=outcome.encode_duration_seconds,
            actual_output_bytes=outcome.staging_size_bytes,
            size_ratio=outcome.size_ratio,
            provenance={
                "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
                "source": "accepted_staged_artifact",
                "policy_hash_source": "unavailable",
                "manifest_run_id": outcome.manifest_run_id,
                "item_index": outcome.item_index,
                "encode_origin": outcome.encode_origin,
                "encode_job_id": outcome.encode_job_id,
                "encode_worker_id": outcome.encode_worker_id,
                "encode_host_key": outcome.encode_host_key,
                "promoted_at": _timestamp(outcome.promoted_at),
            },
            recorded_at=recorded_at,
            authority=AUTHORITY_STAGED_BACKFILL,
        )
        if append_quality_search_observation(connection, observation):
            inserted += 1
        else:
            existing += 1
    return QualityObservationBackfillResult(
        inserted=inserted,
        existing=existing,
        excluded=tuple(sorted(exclusions.items())),
    )


def _build_observation(
        *,
        search_run_id: str,
        revision: int,
        supersedes_observation_id: str | None,
        supersession_reason: str | None,
        library_item_id: int,
        source_rel_path: str,
        source_fingerprint: str | None,
        context: QualitySearchContext | None,
        partial_context: Mapping[str, Any] | None,
        policy_hash: str | None,
        outcome_kind: str,
        authority: str,
        learning_eligible: bool,
        exclusion_reason: str | None,
        selected_crf: float | None,
        selected_target: float | None,
        selected_score: float | None,
        actual_output_bytes: int | None,
        candidate_count: int,
        search_duration_seconds: float | None,
        bounds: Mapping[str, Any],
        candidate_trace: Mapping[str, Any],
        outcome: Mapping[str, Any],
        timing: Mapping[str, Any],
        provenance: Mapping[str, Any],
        shadow_payload: Mapping[str, Any] | None,
        recorded_at: str,
) -> QualitySearchObservation:
    normalized_path = str(PurePosixPath(source_rel_path.strip().strip("/")))
    if not normalized_path or normalized_path == ".":
        raise ValueError("Quality-search observation requires a source relative path")
    prefix = str(PurePosixPath(normalized_path).parent)
    if prefix == ".":
        prefix = normalized_path
    context_payload = _context_payload(context, partial_context)
    semantic_payload = {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "search_run_id": search_run_id,
        "revision": revision,
        "supersedes_observation_id": supersedes_observation_id,
        "supersession_reason": supersession_reason,
        "library_item_id": library_item_id,
        "prefix": prefix,
        "source_rel_path": normalized_path,
        "source_fingerprint": source_fingerprint,
        "search_signature_id": context.signature_id if context is not None else None,
        "policy_hash": policy_hash,
        "search_objective": (
            context.search_objective
            if context is not None
            else str((partial_context or {}).get("search_objective") or "quality")
        ),
        "quality_metric": (
            context.metric
            if context is not None
            else str((partial_context or {}).get("metric") or "UNKNOWN").upper()
        ),
        "outcome_kind": outcome_kind,
        "authority": authority,
        "learning_eligible": learning_eligible,
        "exclusion_reason": exclusion_reason,
        "selected_crf": selected_crf,
        "selected_target": selected_target,
        "selected_score": selected_score,
        "actual_output_bytes": actual_output_bytes,
        "candidate_count": candidate_count,
        "search_duration_seconds": search_duration_seconds,
        "context": context_payload,
        "bounds": dict(bounds),
        "candidate_trace": dict(candidate_trace),
        "outcome": dict(outcome),
        "timing": dict(timing),
        "provenance": dict(provenance),
    }
    if shadow_payload is not None:
        semantic_payload["shadow"] = dict(shadow_payload)
    payload_sha256 = stable_json_hash(semantic_payload)
    return QualitySearchObservation(
        observation_id=f"qso1_{payload_sha256[:32]}",
        search_run_id=search_run_id,
        revision=revision,
        supersedes_observation_id=supersedes_observation_id,
        supersession_reason=supersession_reason,
        schema_version=QUALITY_OBSERVATION_SCHEMA_VERSION,
        library_item_id=library_item_id,
        prefix=prefix,
        source_rel_path=normalized_path,
        source_fingerprint=source_fingerprint,
        search_signature_id=context.signature_id if context is not None else None,
        policy_hash=policy_hash,
        search_objective=str(semantic_payload["search_objective"]),
        quality_metric=str(semantic_payload["quality_metric"]),
        outcome_kind=outcome_kind,
        authority=authority,
        learning_eligible=learning_eligible,
        exclusion_reason=exclusion_reason,
        selected_crf=selected_crf,
        selected_target=selected_target,
        selected_score=selected_score,
        actual_output_bytes=actual_output_bytes,
        candidate_count=candidate_count,
        search_duration_seconds=search_duration_seconds,
        context_json=_json(context_payload),
        bounds_json=_json(bounds),
        candidate_trace_json=_json(candidate_trace),
        outcome_json=_json(outcome),
        timing_json=_json(timing),
        provenance_json=_json(provenance),
        shadow_json=_json(shadow_payload) if shadow_payload is not None else None,
        payload_sha256=payload_sha256,
        recorded_at=recorded_at,
    )


def _selected_candidate_trace(quality_result: QualitySearchResult) -> tuple[dict[str, Any], int]:
    target_size_trace = object_dict(quality_result.target_size_trace)
    if target_size_trace:
        return _project_target_size_trace(target_size_trace)
    quality_search_trace = object_dict(quality_result.quality_search_trace)
    if quality_search_trace:
        return _project_quality_search_trace(
            quality_search_trace,
            selected_crf=quality_result.crf,
            selected_metric=quality_result.metric,
            selected_score=quality_result.score,
        )
    candidates = [_quality_candidate_payload(measurement) for measurement in parse_quality_search_measurements(quality_result.stdout)]
    selected = next(
        (
            candidate
            for candidate in reversed(candidates)
            if math.isclose(float_value(candidate.get("crf")), quality_result.crf, abs_tol=1e-6)
        ),
        None,
    )
    if selected is None:
        selected = {
            "crf": quality_result.crf,
            "metric": quality_result.metric.upper(),
            "metric_score": quality_result.score,
            "predicted_encode_percent": None,
            "predicted_encode_size_bytes": None,
        }
        candidates.append(selected)
    candidate_count = len(candidates)
    stored = candidates[:MAX_QUALITY_OBSERVATION_CANDIDATES]
    return {
        "schema_version": QUALITY_OBSERVATION_TRACE_VERSION,
        "objective": "quality",
        "status": "selected",
        "candidate_count": candidate_count,
        "truncated": candidate_count > len(stored),
        "candidates": stored,
        "selected_candidate": selected,
        "best_reachable_candidate": selected,
    }, candidate_count


def _quality_result_warm_start_trace(quality_result: QualitySearchResult) -> dict[str, Any]:
    target_trace = object_dict(quality_result.target_size_trace)
    quality_trace = object_dict(quality_result.quality_search_trace)
    return object_dict(target_trace.get("warm_start") or quality_trace.get("warm_start"))


def _failed_candidate_trace(
        *,
        target_size_trace: Mapping[str, Any] | None,
        quality_search_trace: Mapping[str, Any] | None,
        quality_error_text: str | None,
) -> tuple[dict[str, Any], int]:
    if target_size_trace:
        return _project_target_size_trace(target_size_trace)
    if quality_search_trace:
        return _project_quality_search_trace(quality_search_trace)
    candidates = [
        _quality_candidate_payload(measurement)
        for measurement in parse_quality_search_measurements(quality_error_text or "")
    ]
    candidate_count = len(candidates)
    stored = candidates[:MAX_QUALITY_OBSERVATION_CANDIDATES]
    return {
        "schema_version": QUALITY_OBSERVATION_TRACE_VERSION,
        "objective": "quality",
        "status": "deterministic_search_failure",
        "candidate_count": candidate_count,
        "truncated": candidate_count > len(stored),
        "candidates": stored,
        "selected_candidate": None,
        "best_reachable_candidate": stored[-1] if stored else None,
    }, candidate_count


def _project_quality_search_trace(
        trace: Mapping[str, Any],
        *,
        selected_crf: float | None = None,
        selected_metric: str | None = None,
        selected_score: float | None = None,
) -> tuple[dict[str, Any], int]:
    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for attempt_payload in object_list(trace.get("attempts")):
        attempt = object_dict(attempt_payload)
        attempt_number = int_value(attempt.get("attempt"))
        attempt_candidates = [
            {
                **_quality_candidate_mapping(object_dict(candidate)),
                "search_attempt": attempt_number,
                "attempt_metric_target": attempt.get("metric_target"),
                "attempt_max_crf": attempt.get("max_crf"),
                "attempt_status": attempt.get("status"),
            }
            for candidate in object_list(attempt.get("candidates"))
        ]
        attempts.append(
            {
                "attempt": attempt_number,
                "metric_target": attempt.get("metric_target"),
                "max_crf": attempt.get("max_crf"),
                "status": attempt.get("status"),
                "candidate_count": len(attempt_candidates),
            }
        )
        candidates.extend(attempt_candidates)
    selected = None
    if selected_crf is not None:
        selected = next(
            (
                candidate
                for candidate in reversed(candidates)
                if math.isclose(float_value(candidate.get("crf")), selected_crf, abs_tol=1e-6)
            ),
            None,
        )
        if selected is None:
            selected = {
                "crf": selected_crf,
                "metric": str(selected_metric or "").upper() or None,
                "metric_score": selected_score,
                "predicted_encode_percent": None,
                "predicted_encode_size_bytes": None,
            }
            candidates.append(selected)
    candidate_count = max(int_value(trace.get("candidate_count")), len(candidates))
    stored = candidates[:MAX_QUALITY_OBSERVATION_CANDIDATES]
    return {
        "schema_version": QUALITY_OBSERVATION_TRACE_VERSION,
        "objective": "quality",
        "status": str(trace.get("status") or "deterministic_search_failure"),
        "attempt_count": len(attempts),
        "attempts": attempts[:MAX_QUALITY_OBSERVATION_CANDIDATES],
        "candidate_count": candidate_count,
        "truncated": candidate_count > len(stored),
        "candidates": stored,
        "selected_candidate": selected,
        "best_reachable_candidate": selected or (stored[-1] if stored else None),
        "warm_start": _project_warm_start_trace(trace.get("warm_start")),
    }, candidate_count


def _project_target_size_trace(trace: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    candidates = [
        _target_candidate_payload(object_dict(candidate))
        for candidate in object_list(trace.get("candidates"))
    ]
    candidate_count = max(int_value(trace.get("candidate_count")), len(candidates))
    stored = candidates[:MAX_QUALITY_OBSERVATION_CANDIDATES]
    return {
        "schema_version": QUALITY_OBSERVATION_TRACE_VERSION,
        "objective": "target_size",
        "status": str(trace.get("status") or "needs_review"),
        "selection_reason": str(trace.get("selection_reason") or "") or None,
        "candidate_count": candidate_count,
        "truncated": candidate_count > len(stored),
        "candidates": stored,
        "selected_candidate": _target_candidate_payload(object_dict(trace.get("selected_candidate"))) or None,
        "best_reachable_candidate": _target_candidate_payload(
            object_dict(trace.get("best_reachable_candidate"))
        ) or None,
        "curve": _curve_payload(object_dict(trace.get("curve"))) or None,
        "final_output": _final_output_payload(object_dict(trace.get("final_output"))) or None,
        "final_output_attempts": [
            _final_output_payload(object_dict(attempt))
            for attempt in object_list(trace.get("final_output_attempts"))
        ][:MAX_QUALITY_OBSERVATION_FINAL_ATTEMPTS],
        "warm_start": _project_warm_start_trace(trace.get("warm_start")),
    }, candidate_count


def _project_warm_start_trace(value: object) -> dict[str, Any] | None:
    trace = object_dict(value)
    if not trace:
        return None
    candidate = object_dict(trace.get("candidate"))
    return {
        "schema_version": int_value(trace.get("schema_version")),
        "status": str(trace.get("status") or "") or None,
        "attempted": trace.get("attempted") is True,
        "requested_crf": trace.get("requested_crf"),
        "candidate_crf": trace.get("candidate_crf"),
        "search_signature_id": str(trace.get("search_signature_id") or "") or None,
        "signature_match": (
            trace.get("signature_match")
            if isinstance(trace.get("signature_match"), bool)
            else None
        ),
        "final_search_signature_id": str(trace.get("final_search_signature_id") or "") or None,
        "cohort_id": str(trace.get("cohort_id") or "") or None,
        "candidate_count": max(int_value(trace.get("candidate_count")), 0),
        "baseline_candidate_count": max(int_value(trace.get("baseline_candidate_count")), 0),
        "total_candidate_count": max(int_value(trace.get("total_candidate_count")), 0),
        "duration_seconds": trace.get("duration_seconds"),
        "fallback_used": trace.get("fallback_used") is True,
        "fallback_reason": str(trace.get("fallback_reason") or "") or None,
        "error_type": str(trace.get("error_type") or "") or None,
        "candidate": (
            _target_candidate_payload(candidate)
            if "predicted_whole_episode_bytes" in candidate
            else _quality_candidate_mapping(candidate)
        ) if candidate else None,
    }


def _quality_candidate_payload(measurement: Any) -> dict[str, Any]:
    return {
        "crf": measurement.crf,
        "metric": measurement.metric,
        "metric_score": measurement.score,
        "predicted_encode_percent": measurement.predicted_encode_percent,
        "predicted_encode_size_bytes": measurement.predicted_encode_size_bytes,
    }


def _quality_candidate_mapping(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "crf",
            "metric",
            "metric_score",
            "predicted_encode_percent",
            "predicted_encode_size_bytes",
        )
    }


def _target_candidate_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not candidate:
        return {}
    keys = (
        "attempt",
        "role",
        "crf",
        "metric",
        "metric_target",
        "metric_score",
        "min_metric_score",
        "quality_floor_met",
        "sampled_clip_bytes",
        "predicted_video_bytes",
        "predicted_whole_episode_bytes",
        "predicted_encode_percent",
        "predicted_encode_seconds",
        "target_distance_bytes",
        "within_sample_band",
        "violates_source_cap",
    )
    return {key: candidate.get(key) for key in keys}


def _curve_payload(curve: Mapping[str, Any]) -> dict[str, Any]:
    if not curve:
        return {}
    return {
        key: curve.get(key)
        for key in ("shape", "candidate_count", "max_candidates")
    }


def _final_output_payload(output: Mapping[str, Any]) -> dict[str, Any]:
    if not output:
        return {}
    keys = (
        "schema_version",
        "status",
        "passed",
        "target_size_bytes",
        "lower_bound_bytes",
        "upper_bound_bytes",
        "actual_output_bytes",
        "tolerance_percent",
        "retry_allowed",
        "retry_reason",
        "retry_count",
        "max_retries",
    )
    return {key: output.get(key) for key in keys}


def _search_bounds(
        *,
        context: QualitySearchContext | None,
        target_size_trace: Mapping[str, Any] | None,
        quality_search_trace: Mapping[str, Any] | None,
        configured_min_crf: float | None,
        configured_max_crf: float | None,
        max_encoded_percent: float | None,
        candidates: list[Any],
) -> dict[str, Any]:
    target_trace = object_dict(target_size_trace)
    quality_trace = object_dict(quality_search_trace)
    target_bounds = object_dict(target_trace.get("crf_bounds"))
    observed_crfs = [
        float_value(object_dict(candidate).get("crf"))
        for candidate in candidates
        if object_dict(candidate).get("crf") is not None
    ]
    return {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "configured_min_crf": target_bounds.get(
            "min_crf",
            quality_trace.get("configured_min_crf", configured_min_crf),
        ),
        "configured_max_crf": target_bounds.get(
            "configured_max_crf",
            quality_trace.get("configured_max_crf", configured_max_crf),
        ),
        "search_max_crf": target_bounds.get(
            "search_max_crf",
            quality_trace.get(
                "search_max_crf",
                max([value for value in observed_crfs if value > 0], default=configured_max_crf),
            ),
        ),
        "max_encoded_percent": quality_trace.get("max_encoded_percent", max_encoded_percent),
        "quality_target": context.target if context is not None else None,
        "minimum_quality_score": context.minimum_quality_score if context is not None else None,
        "size_target_bytes": context.size_target_bytes if context is not None else None,
        "target_video_bitrate": context.target_video_bitrate if context is not None else None,
        "target": object_dict(target_trace.get("target")) or None,
        "quality_floor": object_dict(target_trace.get("quality_floor")) or None,
        "source_cap": object_dict(target_trace.get("source_cap")) or None,
    }


def _context_payload(
        context: QualitySearchContext | None,
        partial_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if context is None:
        return {
            "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
            "complete": False,
            "search_signature_id": None,
            "partial": dict(partial_context or {}),
        }
    payload = context.to_payload()
    return {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "complete": True,
        "search_signature_id": context.signature_id,
        "search": payload,
        "source_signature": {
            "source_codec": context.source_codec,
            "output_width": context.output_width,
            "output_height": context.output_height,
        },
        "encoder_signature": {
            "encoder": context.encoder,
            "pixel_format": context.pixel_format,
            "preset": context.preset,
            "encoder_parameters": context.encoder_parameters,
            "video_filter": context.video_filter,
            "output_container": context.output_container,
        },
    }


def _timing_payload(
        *,
        search_started_at: str | None,
        search_completed_at: str | None,
        search_duration_seconds: float | None,
        retry_measurement_seconds: float | None,
        encode_completed_at: str | None,
        encode_duration_seconds: float | None,
        workflow_duration_seconds: float | None,
) -> dict[str, Any]:
    return {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "search_started_at": search_started_at,
        "search_completed_at": search_completed_at,
        "search_duration_seconds": search_duration_seconds,
        "retry_measurement_seconds": retry_measurement_seconds,
        "encode_completed_at": encode_completed_at,
        "encode_duration_seconds": encode_duration_seconds,
        "workflow_duration_seconds": workflow_duration_seconds,
    }


def _size_error_percent(actual_output_bytes: int | None, target_size_bytes: int | None) -> float | None:
    if actual_output_bytes is None or target_size_bytes is None or target_size_bytes <= 0:
        return None
    return round((actual_output_bytes - target_size_bytes) / target_size_bytes * 100.0, 3)


def _historical_search_run_id(outcome: Any) -> str:
    payload = {
        "schema_version": QUALITY_OBSERVATION_SCHEMA_VERSION,
        "library_item_id": outcome.library_item_id,
        "manifest_run_id": outcome.manifest_run_id,
        "item_index": outcome.item_index,
        "encode_origin": outcome.encode_origin,
        "encode_job_id": outcome.encode_job_id,
        "encode_completed_at": _timestamp(outcome.encode_completed_at),
        "source_fingerprint": outcome.source_fingerprint,
        "search_signature_id": outcome.context.signature_id,
    }
    return f"qsr1_{stable_json_hash(payload)[:32]}"


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="seconds")
