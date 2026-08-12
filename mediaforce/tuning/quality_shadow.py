from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
from statistics import median, quantiles
from typing import Any, Iterable, Literal, Mapping

from mediaforce.core.db import DBClient
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.library.media_scopes import (
    MediaScope,
    media_group_scope_for_rel_path,
    media_scope_from_prefix,
    tv_series_scope_for_rel_path,
)
from mediaforce.tuning.quality_memory import (
    MAX_HINT_IQR,
    MAX_HINT_MAD,
    QUALITY_MEMORY_MAX_AGE_DAYS,
    QualityMemoryConfidence,
    QualityMemoryScope,
    QualitySearchContext,
)
from mediaforce.tuning.quality_observations import (
    AUTHORITY_STAGED_BACKFILL,
    OUTCOME_FINAL_SIZE_FAILURE,
    OUTCOME_SELECTED,
    load_current_quality_search_observations,
)

QUALITY_SHADOW_SCHEMA_VERSION = 1
QUALITY_SHADOW_ALGORITHM_VERSION = "qsh2"
MAX_EVIDENCE_OBSERVATION_IDS = 20
QUALITY_SCORE_MONOTONIC_TOLERANCE = 0.5
QUALITY_SCORE_CONFLICT_MARGIN = 0.25
MIN_SHADOW_RECOMMENDATIONS = 10
MIN_WITHIN_ONE_RATE = 0.70
MIN_MEDIAN_SAVINGS_RATE = 0.20

QualityShadowFallbackReason = Literal[
    "no_history",
    "stale_evidence",
    "stale_signature",
    "source_fingerprint_unavailable",
    "source_fingerprint_changed",
    "sparse_cohort",
    "high_dispersion",
    "search_target_changed",
    "non_monotonic_trace",
    "conflicting_quality_evidence",
    "final_retry_terminal",
    "shadow_evaluation_error",
]

_MIN_HINT_SAMPLES: dict[QualityMemoryScope, int] = {
    "item": 4,
    "season": 4,
    "series": 6,
}
_AUTHORITY_RANK = {
    "staged_backfill": 10,
    "runtime_native": 20,
    "correction": 30,
}
_FALLBACK_PRIORITY: tuple[QualityShadowFallbackReason, ...] = (
    "conflicting_quality_evidence",
    "non_monotonic_trace",
    "search_target_changed",
    "final_retry_terminal",
    "high_dispersion",
    "sparse_cohort",
    "source_fingerprint_changed",
    "source_fingerprint_unavailable",
    "stale_signature",
    "stale_evidence",
    "no_history",
)


@dataclass(frozen=True, slots=True)
class QualityShadowExclusion:
    reason: str
    count: int

    def to_payload(self) -> dict[str, Any]:
        return {"reason": self.reason, "count": self.count}


@dataclass(frozen=True, slots=True)
class QualityShadowRecommendation:
    search_signature_id: str
    evidence_cutoff_at: str
    first_crf: float | None
    scope: QualityMemoryScope | None
    confidence: QualityMemoryConfidence
    sample_count: int
    cohort_id: str | None
    minimum_crf: float | None
    maximum_crf: float | None
    iqr: float | None
    median_absolute_deviation: float | None
    evidence_observation_ids: tuple[str, ...]
    fallback_reason: QualityShadowFallbackReason | None
    reason: str
    exclusions: tuple[QualityShadowExclusion, ...]

    @property
    def available(self) -> bool:
        return self.first_crf is not None and self.scope is not None

    def to_payload(self) -> dict[str, Any]:
        recommendation = None
        if self.available:
            recommendation = {
                "first_crf": self.first_crf,
                "scope": self.scope,
                "confidence": self.confidence,
                "sample_count": self.sample_count,
                "cohort_id": self.cohort_id,
                "minimum_crf": self.minimum_crf,
                "maximum_crf": self.maximum_crf,
                "iqr": self.iqr,
                "median_absolute_deviation": self.median_absolute_deviation,
                "evidence_observation_ids": list(self.evidence_observation_ids),
            }
        return {
            "schema_version": QUALITY_SHADOW_SCHEMA_VERSION,
            "algorithm_version": QUALITY_SHADOW_ALGORITHM_VERSION,
            "search_signature_id": self.search_signature_id,
            "evidence_cutoff_at": self.evidence_cutoff_at,
            "recommendation": recommendation,
            "fallback_reason": self.fallback_reason,
            "reason": self.reason,
            "exclusions": [exclusion.to_payload() for exclusion in self.exclusions],
        }


@dataclass(frozen=True, slots=True)
class QualityShadowMetrics:
    evaluated_runs: int
    recommendation_runs: int
    coverage_rate: float
    within_one_rate: float
    false_narrow_rate: float
    fallback_need_rate: float
    median_candidate_savings_rate: float
    median_search_time_savings_rate: float
    passive_quality_floor_violations: int
    active_quality_floor_violations: int
    baseline_final_size_misses: int
    active_final_size_misses: int
    fallback_counts: tuple[tuple[str, int], ...]
    performance_thresholds_met: bool
    active_eligible: bool
    blocking_reasons: tuple[str, ...]
    raw_evaluated_runs: int = 0

    @property
    def production_quality_floor_violations(self) -> int:
        return self.passive_quality_floor_violations + self.active_quality_floor_violations

    @property
    def production_final_size_misses(self) -> int:
        return self.active_final_size_misses

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_SHADOW_SCHEMA_VERSION,
            "evaluated_runs": self.evaluated_runs,
            "recommendation_runs": self.recommendation_runs,
            "coverage_rate": self.coverage_rate,
            "within_one_rate": self.within_one_rate,
            "false_narrow_rate": self.false_narrow_rate,
            "fallback_need_rate": self.fallback_need_rate,
            "median_candidate_savings_rate": self.median_candidate_savings_rate,
            "median_search_time_savings_rate": self.median_search_time_savings_rate,
            "passive_quality_floor_violations": self.passive_quality_floor_violations,
            "active_quality_floor_violations": self.active_quality_floor_violations,
            "baseline_final_size_misses": self.baseline_final_size_misses,
            "active_final_size_misses": self.active_final_size_misses,
            "production_quality_floor_violations": (
                self.production_quality_floor_violations
            ),
            "production_final_size_misses": self.production_final_size_misses,
            "fallback_counts": dict(self.fallback_counts),
            "performance_thresholds_met": self.performance_thresholds_met,
            "active_eligible": self.active_eligible,
            "blocking_reasons": list(self.blocking_reasons),
            "raw_evaluated_runs": self.raw_evaluated_runs,
        }


@dataclass(frozen=True, slots=True)
class _ShadowEvidence:
    observation_id: str
    rel_path: str
    source_fingerprint: str | None
    selected_crf: float


def recommend_quality_search(
        connection: DBClient,
        *,
        source_rel_path: str,
        source_fingerprint: str | None,
        expected_context: QualitySearchContext,
        policy_hash: str | None,
        as_of: datetime | str,
        library_types: Mapping[str, str] | None = None,
        allow_unhashed_backfill: bool = True,
) -> QualityShadowRecommendation:
    cutoff = _parse_timestamp(as_of)
    if cutoff is None:
        raise ValueError("Quality-shadow evidence cutoff must be a valid timestamp")
    cutoff = _as_utc(cutoff)
    return recommend_quality_search_from_rows(
        load_current_quality_search_observations(
            connection,
            recorded_before=_timestamp(cutoff),
        ),
        source_rel_path=source_rel_path,
        source_fingerprint=source_fingerprint,
        expected_context=expected_context,
        policy_hash=policy_hash,
        as_of=cutoff,
        library_types=library_types,
        allow_unhashed_backfill=allow_unhashed_backfill,
    )


def recommend_quality_search_from_rows(
        rows: Iterable[Mapping[str, Any]],
        *,
        source_rel_path: str,
        source_fingerprint: str | None,
        expected_context: QualitySearchContext,
        policy_hash: str | None,
        as_of: datetime | str,
        library_types: Mapping[str, str] | None = None,
        allow_unhashed_backfill: bool = True,
) -> QualityShadowRecommendation:
    cutoff = _parse_timestamp(as_of)
    if cutoff is None:
        raise ValueError("Quality-shadow evidence cutoff must be a valid timestamp")
    cutoff = _as_utc(cutoff)
    scope_chain = quality_memory_scope_chain(source_rel_path, library_types=library_types)
    exclusion_counts: Counter[str] = Counter()
    if _optional_text(policy_hash) is None:
        return unavailable_quality_shadow_recommendation(
            expected_context=expected_context,
            as_of=cutoff,
            fallback_reason="stale_signature",
            reason="The current policy hash is unavailable, so compatible history cannot be verified.",
        )
    rows_before_cutoff: list[Mapping[str, Any]] = []
    for row in rows:
        rel_path = str(row.get("source_rel_path") or "").strip().strip("/")
        if not rel_path or not any(scope.includes(rel_path) for _, scope in scope_chain):
            continue
        recorded_at = _parse_timestamp(row.get("recorded_at"))
        if recorded_at is None:
            exclusion_counts["recorded_time_missing"] += 1
            continue
        if _as_utc(recorded_at) >= cutoff:
            exclusion_counts["recorded_after_cutoff"] += 1
            continue
        rows_before_cutoff.append(row)
    evidence: list[_ShadowEvidence] = []
    fallback_reasons: list[QualityShadowFallbackReason] = []
    terminal_rows = deduplicate_quality_terminal_rows(
        _resolve_current_rows(rows_before_cutoff),
        allow_unhashed_backfill=allow_unhashed_backfill,
        retain_missing_policy=True,
    )
    for row in terminal_rows:
        rel_path = str(row.get("source_rel_path") or "").strip().strip("/")
        if str(row.get("outcome_kind") or "") != OUTCOME_SELECTED:
            continue
        if int_value(row.get("learning_eligible")) != 1:
            continue
        completion_at = _observation_completion_at(row)
        if completion_at is None:
            exclusion_counts["completion_time_missing"] += 1
            continue
        if completion_at >= cutoff:
            exclusion_counts["completed_after_cutoff"] += 1
            continue
        if cutoff - completion_at > timedelta(days=QUALITY_MEMORY_MAX_AGE_DAYS):
            exclusion_counts["stale_evidence"] += 1
            continue
        if str(row.get("search_signature_id") or "") != expected_context.signature_id:
            exclusion_counts["stale_signature"] += 1
            continue
        historical_policy_hash = str(row.get("policy_hash") or "").strip() or None
        authority = str(row.get("authority") or "").strip()
        if historical_policy_hash is None and (
                authority != AUTHORITY_STAGED_BACKFILL or not allow_unhashed_backfill
        ):
            exclusion_counts["stale_signature"] += 1
            continue
        if historical_policy_hash is not None and historical_policy_hash != policy_hash:
            exclusion_counts["stale_signature"] += 1
            continue
        selected_crf = _strict_number(row.get("selected_crf"))
        selected_target = _strict_number(row.get("selected_target"))
        selected_score = _strict_number(row.get("selected_score"))
        observation_id = str(row.get("observation_id") or "").strip()
        if selected_crf is None or selected_target is None or selected_score is None or not observation_id:
            exclusion_counts["malformed_observation"] += 1
            continue
        candidate_trace = _json_object(row.get("candidate_trace_json"))
        if _selected_candidate_is_final_retry(candidate_trace):
            exclusion_counts["final_retry_terminal"] += 1
            fallback_reasons.append("final_retry_terminal")
            continue
        trace_rejection = _trace_rejection(
            candidate_trace,
            expected_context.minimum_quality_score,
            selected_target=selected_target,
        )
        if trace_rejection is not None:
            exclusion_counts[trace_rejection] += 1
            continue
        evidence.append(
            _ShadowEvidence(
                observation_id=observation_id,
                rel_path=rel_path,
                source_fingerprint=_optional_text(row.get("source_fingerprint")),
                selected_crf=selected_crf,
            )
        )

    normalized_fingerprint = _optional_text(source_fingerprint)
    for scope_name, scope in scope_chain:
        scoped = [observation for observation in evidence if scope.includes(observation.rel_path)]
        if scope_name == "item":
            if normalized_fingerprint is None:
                if scoped:
                    fallback_reasons.append("source_fingerprint_unavailable")
                exclusion_counts["source_fingerprint_unavailable"] += len(scoped)
                scoped = []
            else:
                matching: list[_ShadowEvidence] = []
                changed_fingerprints = 0
                for observation in scoped:
                    if observation.source_fingerprint is None:
                        exclusion_counts["source_fingerprint_unavailable"] += 1
                    elif observation.source_fingerprint != normalized_fingerprint:
                        changed_fingerprints += 1
                        exclusion_counts["source_fingerprint_changed"] += 1
                    else:
                        matching.append(observation)
                if scoped and not matching:
                    fallback_reasons.append(
                        "source_fingerprint_changed"
                        if changed_fingerprints > 0
                        else "source_fingerprint_unavailable"
                    )
                scoped = matching
        minimum_samples = _MIN_HINT_SAMPLES[scope_name]
        if len(scoped) < minimum_samples:
            if scoped:
                fallback_reasons.append("sparse_cohort")
            continue
        crfs = [observation.selected_crf for observation in scoped]
        median_crf = float(median(crfs))
        median_absolute_deviation = float(median(abs(value - median_crf) for value in crfs))
        first_quartile, _, third_quartile = quantiles(crfs, n=4, method="inclusive")
        iqr = float(third_quartile - first_quartile)
        if iqr > MAX_HINT_IQR or median_absolute_deviation > MAX_HINT_MAD:
            fallback_reasons.append("high_dispersion")
            continue
        observation_ids = tuple(sorted(observation.observation_id for observation in scoped))
        cohort_id = f"qsc1_{stable_json_hash({
            'scope': scope_name,
            'prefix': scope.prefix,
            'search_signature_id': expected_context.signature_id,
            'observation_ids': observation_ids,
        })[:32]}"
        confidence = _confidence(scope_name, len(scoped))
        return QualityShadowRecommendation(
            search_signature_id=expected_context.signature_id,
            evidence_cutoff_at=_timestamp(cutoff),
            first_crf=round(median_crf, 3),
            scope=scope_name,
            confidence=confidence,
            sample_count=len(scoped),
            cohort_id=cohort_id,
            minimum_crf=min(crfs),
            maximum_crf=max(crfs),
            iqr=round(iqr, 3),
            median_absolute_deviation=round(median_absolute_deviation, 3),
            evidence_observation_ids=observation_ids[:MAX_EVIDENCE_OBSERVATION_IDS],
            fallback_reason=None,
            reason=(
                f"{scope_name.title()} shadow hint uses median CRF {median_crf:.2f} from "
                f"{len(scoped)} compatible observations ({confidence} confidence, "
                f"IQR {iqr:.2f}, MAD {median_absolute_deviation:.2f})."
            ),
            exclusions=_exclusions(exclusion_counts),
        )

    fallback_reason = _fallback_reason(
        fallback_reasons,
        exclusion_counts=exclusion_counts,
    )
    return QualityShadowRecommendation(
        search_signature_id=expected_context.signature_id,
        evidence_cutoff_at=_timestamp(cutoff),
        first_crf=None,
        scope=None,
        confidence="none",
        sample_count=0,
        cohort_id=None,
        minimum_crf=None,
        maximum_crf=None,
        iqr=None,
        median_absolute_deviation=None,
        evidence_observation_ids=(),
        fallback_reason=fallback_reason,
        reason=_fallback_detail(fallback_reason, exclusion_counts),
        exclusions=_exclusions(exclusion_counts),
    )


def failed_quality_shadow_recommendation(
        *,
        expected_context: QualitySearchContext,
        as_of: datetime | str,
) -> QualityShadowRecommendation:
    return unavailable_quality_shadow_recommendation(
        expected_context=expected_context,
        as_of=as_of,
        fallback_reason="shadow_evaluation_error",
        reason="Shadow recommendation evaluation failed; production search continued unchanged.",
    )


def unavailable_quality_shadow_recommendation(
        *,
        expected_context: QualitySearchContext,
        as_of: datetime | str,
        fallback_reason: QualityShadowFallbackReason,
        reason: str,
) -> QualityShadowRecommendation:
    cutoff = _parse_timestamp(as_of)
    if cutoff is None:
        cutoff = datetime.now(UTC)
    return QualityShadowRecommendation(
        search_signature_id=expected_context.signature_id,
        evidence_cutoff_at=_timestamp(_as_utc(cutoff)),
        first_crf=None,
        scope=None,
        confidence="none",
        sample_count=0,
        cohort_id=None,
        minimum_crf=None,
        maximum_crf=None,
        iqr=None,
        median_absolute_deviation=None,
        evidence_observation_ids=(),
        fallback_reason=fallback_reason,
        reason=reason,
        exclusions=(),
    )


def build_quality_shadow_payload(
        recommendation: QualityShadowRecommendation,
        *,
        selected_crf: float,
        selected_score: float,
        minimum_quality_score: float,
        candidate_count: int,
        search_duration_seconds: float | None,
        actual_output_bytes: int | None,
        size_target_bytes: int | None,
        warm_start_plan: Mapping[str, Any] | None = None,
        warm_start_trace: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        final_size_miss: bool = False,
        selected_candidate_role: str | None = None,
) -> dict[str, Any]:
    normalized_candidate_count = max(int(candidate_count), 0)
    quality_margin = round(selected_score - minimum_quality_score, 3)
    size_error_percent = _size_error_percent(actual_output_bytes, size_target_bytes)
    first_crf = recommendation.first_crf
    evaluation_exclusion_reason = (
        "final_retry_terminal"
        if selected_candidate_role == "final_retry_measurement"
        else None
    )
    delta = (
        round(selected_crf - first_crf, 3)
        if first_crf is not None and evaluation_exclusion_reason is None
        else None
    )
    within_one = abs(delta) <= 1.0 if delta is not None else None
    projected_candidate_count: int | None = None
    candidate_savings_count: int | None = None
    candidate_savings_rate: float | None = None
    projected_search_seconds: float | None = None
    search_time_savings_seconds: float | None = None
    search_time_savings_rate: float | None = None
    warm_trace = object_dict(warm_start_trace)
    production_search_changed = warm_trace.get("attempted") is True
    if normalized_candidate_count > 0 and not production_search_changed:
        if first_crf is None:
            projected_candidate_count = normalized_candidate_count
        elif within_one:
            projected_candidate_count = 1
        else:
            projected_candidate_count = normalized_candidate_count + 1
        candidate_savings_count = normalized_candidate_count - projected_candidate_count
        candidate_savings_rate = round(candidate_savings_count / normalized_candidate_count, 6)
        if search_duration_seconds is not None and search_duration_seconds >= 0:
            projected_search_seconds = round(
                search_duration_seconds * projected_candidate_count / normalized_candidate_count,
                3,
            )
            search_time_savings_seconds = round(search_duration_seconds - projected_search_seconds, 3)
            search_time_savings_rate = round(
                search_time_savings_seconds / search_duration_seconds,
                6,
            ) if search_duration_seconds > 0 else 0.0
    warm_payload = _quality_warm_start_payload(
        warm_start_plan,
        warm_trace,
        candidate_count=normalized_candidate_count,
        search_duration_seconds=search_duration_seconds,
    )
    payload = recommendation.to_payload()
    payload.update(
        {
            "analysis_family_id": analysis_family_id_from_context(context),
            "production_search_changed": production_search_changed,
            "warm_start": warm_payload,
            "comparison": {
                "selected_crf": selected_crf,
                "selected_candidate_role": selected_candidate_role,
                "evaluation_exclusion_reason": evaluation_exclusion_reason,
                "crf_delta": delta,
                "within_one_crf": within_one,
                "candidate_count": normalized_candidate_count,
                "projected_candidate_count": projected_candidate_count,
                "candidate_savings_count": candidate_savings_count,
                "candidate_savings_rate": candidate_savings_rate,
                "search_duration_seconds": search_duration_seconds,
                "projected_search_seconds": projected_search_seconds,
                "search_time_savings_seconds": search_time_savings_seconds,
                "search_time_savings_rate": search_time_savings_rate,
                "quality_margin": quality_margin,
                "production_quality_floor_violation": quality_margin < 0,
                "size_error_percent": size_error_percent,
                "final_size_miss": final_size_miss,
                "fallback_needed": (
                    None
                    if evaluation_exclusion_reason is not None
                    else (
                        warm_trace.get("fallback_used") is True
                        if production_search_changed
                        else first_crf is None or not bool(within_one)
                    )
                ),
                "false_narrow": (
                    False
                    if production_search_changed or evaluation_exclusion_reason is not None
                    else first_crf is not None and not bool(within_one)
                ),
            },
        }
    )
    return payload


def _quality_warm_start_payload(
        plan: Mapping[str, Any] | None,
        trace: Mapping[str, Any],
        *,
        candidate_count: int,
        search_duration_seconds: float | None,
) -> dict[str, Any] | None:
    plan_payload = object_dict(plan)
    trace_payload = object_dict(trace)
    if not plan_payload and not trace_payload:
        return None
    baseline_candidate_count = _strict_number(plan_payload.get("baseline_median_candidate_count"))
    baseline_search_seconds = _strict_number(plan_payload.get("baseline_median_search_seconds"))
    estimated_candidate_savings_count = (
        round(baseline_candidate_count - candidate_count, 3)
        if baseline_candidate_count is not None
        else None
    )
    estimated_candidate_savings_rate = (
        round(estimated_candidate_savings_count / baseline_candidate_count, 6)
        if estimated_candidate_savings_count is not None and baseline_candidate_count > 0
        else None
    )
    estimated_search_time_savings_seconds = (
        round(baseline_search_seconds - search_duration_seconds, 3)
        if baseline_search_seconds is not None and search_duration_seconds is not None
        else None
    )
    estimated_search_time_savings_rate = (
        round(estimated_search_time_savings_seconds / baseline_search_seconds, 6)
        if estimated_search_time_savings_seconds is not None and baseline_search_seconds > 0
        else None
    )
    return {
        "schema_version": 1,
        "eligible": plan_payload.get("eligible") is True,
        "block_reason": _optional_text(plan_payload.get("block_reason")),
        "experiment_version": _optional_text(plan_payload.get("experiment_version")),
        "experiment_arm": _optional_text(plan_payload.get("experiment_arm")),
        "future_experiment_arm": _optional_text(plan_payload.get("future_experiment_arm")),
        "execution_mode": _optional_text(plan_payload.get("execution_mode")),
        "holdout_percent": int_value(plan_payload.get("holdout_percent")),
        "scope": _optional_text(plan_payload.get("scope")),
        "scope_prefix": _optional_text(plan_payload.get("scope_prefix")),
        "search_signature_id": _optional_text(plan_payload.get("search_signature_id")),
        "requested_crf": _strict_number(plan_payload.get("requested_crf")),
        "candidate_crf": _strict_number(plan_payload.get("candidate_crf")),
        "adjusted": plan_payload.get("adjusted") is True,
        "readiness": object_dict(plan_payload.get("readiness")) or None,
        "execution": dict(trace_payload) if trace_payload else None,
        "baseline_median_candidate_count": baseline_candidate_count,
        "baseline_median_search_seconds": baseline_search_seconds,
        "estimated_candidate_savings_count": estimated_candidate_savings_count,
        "estimated_candidate_savings_rate": estimated_candidate_savings_rate,
        "estimated_search_time_savings_seconds": estimated_search_time_savings_seconds,
        "estimated_search_time_savings_rate": estimated_search_time_savings_rate,
    }


def select_latest_quality_shadow_observation(
        rows: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    selected: tuple[datetime, str, Mapping[str, Any]] | None = None
    for row in _resolve_current_rows(rows):
        if str(row.get("outcome_kind") or "") != OUTCOME_SELECTED:
            continue
        shadow = _json_object(row.get("shadow_json"))
        if (
            int_value(shadow.get("schema_version")) != QUALITY_SHADOW_SCHEMA_VERSION
            or str(shadow.get("algorithm_version") or "") != QUALITY_SHADOW_ALGORITHM_VERSION
        ):
            continue
        if (
                int_value(row.get("learning_eligible")) != 1
                and not (
                    str(row.get("exclusion_reason") or "") == "warm_start_selected"
                    and shadow.get("production_search_changed") is True
                )
        ):
            continue
        completed_at = _observation_completion_at(row)
        if completed_at is None:
            continue
        observation_id = str(row.get("observation_id") or "")
        candidate = (completed_at, observation_id, row)
        if selected is None or candidate[:2] > selected[:2]:
            selected = candidate
    return selected[2] if selected is not None else None


def quality_shadow_public_view(observation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    row = object_dict(observation)
    shadow = _json_object(row.get("shadow_json"))
    if (
        not row
        or int_value(shadow.get("schema_version")) != QUALITY_SHADOW_SCHEMA_VERSION
        or str(shadow.get("algorithm_version") or "") != QUALITY_SHADOW_ALGORITHM_VERSION
    ):
        return None
    comparison = object_dict(shadow.get("comparison"))
    recommendation_payload = object_dict(shadow.get("recommendation"))
    recommendation = None
    recommendation_scope = str(recommendation_payload.get("scope") or "")
    recommendation_confidence = str(recommendation_payload.get("confidence") or "none")
    recommendation_crf = _strict_number(recommendation_payload.get("first_crf"))
    if (
        recommendation_crf is not None
        and recommendation_scope in {"item", "season", "series"}
        and recommendation_confidence in {"none", "limited", "moderate", "high"}
    ):
        evidence_ids = [
            str(value)
            for value in object_list(recommendation_payload.get("evidence_observation_ids"))
            if str(value).strip()
        ]
        recommendation = {
            "first_crf": recommendation_crf,
            "scope": recommendation_scope,
            "confidence": recommendation_confidence,
            "sample_count": max(int_value(recommendation_payload.get("sample_count")), 0),
            "evidence_count": len(evidence_ids),
            "minimum_crf": _strict_number(recommendation_payload.get("minimum_crf")),
            "maximum_crf": _strict_number(recommendation_payload.get("maximum_crf")),
            "iqr": _strict_number(recommendation_payload.get("iqr")),
            "median_absolute_deviation": _strict_number(
                recommendation_payload.get("median_absolute_deviation")
            ),
        }
    selected_score = _strict_number(row.get("selected_score"))
    context = _json_object(row.get("context_json"))
    quality_floor = _strict_number(object_dict(context.get("search")).get("minimum_quality_score"))
    quality_margin = (
        round(selected_score - quality_floor, 3)
        if selected_score is not None and quality_floor is not None
        else None
    )
    selected_crf = _strict_number(comparison.get("selected_crf"))
    if selected_crf is None:
        selected_crf = _strict_number(row.get("selected_crf"))
    candidate_count = int_value(comparison.get("candidate_count"))
    if "candidate_count" not in comparison:
        candidate_count = int_value(row.get("candidate_count"))
    search_duration_seconds = _strict_number(comparison.get("search_duration_seconds"))
    if search_duration_seconds is None:
        search_duration_seconds = _strict_number(row.get("search_duration_seconds"))
    fallback_reason = _optional_text(shadow.get("fallback_reason"))
    return {
        "schema_version": QUALITY_SHADOW_SCHEMA_VERSION,
        "algorithm_version": QUALITY_SHADOW_ALGORITHM_VERSION,
        "observation_id": _optional_text(row.get("observation_id")),
        "source_rel_path": _optional_text(row.get("source_rel_path")),
        "recorded_at": _optional_text(row.get("recorded_at")),
        "evidence_cutoff_at": _optional_text(shadow.get("evidence_cutoff_at")),
        "measured": {
            "selected_crf": selected_crf,
            "quality_metric": _optional_text(row.get("quality_metric")),
            "quality_score": selected_score,
            "quality_target": _strict_number(row.get("selected_target")),
            "quality_floor": quality_floor,
            "quality_margin": quality_margin,
            "output_bytes": int_value(row.get("actual_output_bytes")) or None,
            "size_error_percent": _strict_number(comparison.get("size_error_percent")),
            "candidate_count": max(candidate_count, 0),
            "search_duration_seconds": search_duration_seconds,
        },
        "recommendation": recommendation,
        "comparison": {
            "crf_delta": _strict_number(comparison.get("crf_delta")),
            "within_one_crf": (
                comparison.get("within_one_crf")
                if isinstance(comparison.get("within_one_crf"), bool)
                else None
            ),
        },
        "fallback_reason": fallback_reason,
        "reason": str(shadow.get("reason") or "").strip(),
        "production_search_changed": shadow.get("production_search_changed") is True,
        "warm_start": _quality_warm_start_public_view(shadow.get("warm_start")),
    }


def _quality_warm_start_public_view(value: Any) -> dict[str, Any] | None:
    payload = object_dict(value)
    if not payload:
        return None
    execution = object_dict(payload.get("execution"))
    return {
        "eligible": payload.get("eligible") is True,
        "block_reason": _optional_text(payload.get("block_reason")),
        "requested_crf": _strict_number(payload.get("requested_crf")),
        "candidate_crf": _strict_number(payload.get("candidate_crf")),
        "adjusted": payload.get("adjusted") is True,
        "status": _optional_text(execution.get("status")),
        "attempted": execution.get("attempted") is True,
        "fallback_used": execution.get("fallback_used") is True,
        "fallback_reason": _optional_text(execution.get("fallback_reason")),
        "candidate_count": max(int_value(execution.get("candidate_count")), 0),
        "baseline_candidate_count": max(int_value(execution.get("baseline_candidate_count")), 0),
        "total_candidate_count": max(int_value(execution.get("total_candidate_count")), 0),
        "duration_seconds": _strict_number(execution.get("duration_seconds")),
        "baseline_median_candidate_count": _strict_number(payload.get("baseline_median_candidate_count")),
        "baseline_median_search_seconds": _strict_number(payload.get("baseline_median_search_seconds")),
        "estimated_candidate_savings_count": _strict_number(payload.get("estimated_candidate_savings_count")),
        "estimated_candidate_savings_rate": _strict_number(payload.get("estimated_candidate_savings_rate")),
        "estimated_search_time_savings_seconds": _strict_number(
            payload.get("estimated_search_time_savings_seconds")
        ),
        "estimated_search_time_savings_rate": _strict_number(payload.get("estimated_search_time_savings_rate")),
    }


def load_quality_shadow_metrics(connection: DBClient) -> QualityShadowMetrics:
    return quality_shadow_metrics(load_current_quality_search_observations(connection))


def quality_shadow_metrics(rows: Iterable[Mapping[str, Any]]) -> QualityShadowMetrics:
    raw_row_list = _resolve_current_rows(rows)
    raw_evaluated_runs = sum(
        payload.get("production_search_changed") is not True
        for row in raw_row_list
        if (payload := _json_object(row.get("shadow_json")))
        and payload.get("algorithm_version") == QUALITY_SHADOW_ALGORITHM_VERSION
    )
    row_list = deduplicate_quality_terminal_rows(raw_row_list)
    row_payloads = [
        (row, payload)
        for row in row_list
        if (payload := _json_object(row.get("shadow_json")))
        and payload.get("algorithm_version") == QUALITY_SHADOW_ALGORITHM_VERSION
    ]
    raw_row_payloads = [
        (row, payload)
        for row in raw_row_list
        if (payload := _json_object(row.get("shadow_json")))
        and payload.get("algorithm_version") == QUALITY_SHADOW_ALGORITHM_VERSION
    ]
    payloads = [payload for _, payload in row_payloads]
    passive_payloads = [payload for payload in payloads if payload.get("production_search_changed") is not True]
    evaluated_runs = len(passive_payloads)
    recommendation_payloads = [
        payload
        for payload in passive_payloads
        if object_dict(payload.get("recommendation"))
        and not object_dict(payload.get("comparison")).get("evaluation_exclusion_reason")
    ]
    recommendation_runs = len(recommendation_payloads)
    comparisons = [object_dict(payload.get("comparison")) for payload in passive_payloads]
    recommendation_comparisons = [object_dict(payload.get("comparison")) for payload in recommendation_payloads]
    within_one_count = sum(comparison.get("within_one_crf") is True for comparison in recommendation_comparisons)
    false_narrow_count = sum(comparison.get("false_narrow") is True for comparison in recommendation_comparisons)
    fallback_need_count = sum(comparison.get("fallback_needed") is True for comparison in comparisons)
    candidate_savings = [
        float(value)
        for comparison in recommendation_comparisons
        if (value := _strict_number(comparison.get("candidate_savings_rate"))) is not None
    ]
    search_time_savings = [
        float(value)
        for comparison in recommendation_comparisons
        if (value := _strict_number(comparison.get("search_time_savings_rate"))) is not None
    ]
    fallback_counts: Counter[str] = Counter()
    for payload in passive_payloads:
        reasons = {
            str(reason)
            for reason in (
                payload.get("fallback_reason"),
                object_dict(payload.get("comparison")).get("evaluation_exclusion_reason"),
            )
            if reason
        }
        fallback_counts.update(reasons)
    passive_quality_floor_violations = sum(
        comparison.get("production_quality_floor_violation") is True
        for _, payload in raw_row_payloads
        if payload.get("production_search_changed") is not True
        for comparison in (object_dict(payload.get("comparison")),)
    )
    active_quality_floor_violations = sum(
        object_dict(payload.get("comparison")).get("production_quality_floor_violation") is True
        for _, payload in raw_row_payloads
        if payload.get("production_search_changed") is True
    )
    active_final_size_misses = sum(
        (
            object_dict(payload.get("comparison")).get("final_size_miss") is True
            or str(row.get("outcome_kind") or "") == OUTCOME_FINAL_SIZE_FAILURE
        )
        and _quality_memory_arm(row, payload) == "warm_start"
        and _quality_memory_attempted(row, payload)
        for row, payload in raw_row_payloads
    ) + sum(
        str(row.get("outcome_kind") or "") == OUTCOME_FINAL_SIZE_FAILURE
        and _quality_memory_arm(row, _json_object(row.get("shadow_json"))) == "warm_start"
        and _quality_memory_attempted(row, _json_object(row.get("shadow_json")))
        and not _json_object(row.get("shadow_json"))
        for row in raw_row_list
    )
    baseline_final_size_misses = sum(
        str(row.get("outcome_kind") or "") == OUTCOME_FINAL_SIZE_FAILURE
        and not (
            _quality_memory_arm(row, _json_object(row.get("shadow_json"))) == "warm_start"
            and _quality_memory_attempted(row, _json_object(row.get("shadow_json")))
        )
        for row in raw_row_list
    )
    coverage_rate = _ratio(recommendation_runs, evaluated_runs)
    within_one_rate = _ratio(within_one_count, recommendation_runs)
    false_narrow_rate = _ratio(false_narrow_count, recommendation_runs)
    fallback_need_rate = _ratio(fallback_need_count, evaluated_runs)
    median_candidate_savings_rate = round(float(median(candidate_savings)), 6) if candidate_savings else 0.0
    median_search_time_savings_rate = round(float(median(search_time_savings)), 6) if search_time_savings else 0.0
    performance_blocking_reasons: list[str] = []
    if recommendation_runs < MIN_SHADOW_RECOMMENDATIONS:
        performance_blocking_reasons.append("insufficient_shadow_recommendations")
    if within_one_rate < MIN_WITHIN_ONE_RATE:
        performance_blocking_reasons.append("within_one_rate_below_threshold")
    if median_candidate_savings_rate < MIN_MEDIAN_SAVINGS_RATE:
        performance_blocking_reasons.append("candidate_savings_below_threshold")
    if median_search_time_savings_rate < MIN_MEDIAN_SAVINGS_RATE:
        performance_blocking_reasons.append("search_time_savings_below_threshold")
    if passive_quality_floor_violations or active_quality_floor_violations:
        performance_blocking_reasons.append("production_quality_floor_violation")
    if active_final_size_misses:
        performance_blocking_reasons.append("active_final_size_miss")
    performance_thresholds_met = not performance_blocking_reasons
    return QualityShadowMetrics(
        evaluated_runs=evaluated_runs,
        raw_evaluated_runs=raw_evaluated_runs,
        recommendation_runs=recommendation_runs,
        coverage_rate=coverage_rate,
        within_one_rate=within_one_rate,
        false_narrow_rate=false_narrow_rate,
        fallback_need_rate=fallback_need_rate,
        median_candidate_savings_rate=median_candidate_savings_rate,
        median_search_time_savings_rate=median_search_time_savings_rate,
        passive_quality_floor_violations=passive_quality_floor_violations,
        active_quality_floor_violations=active_quality_floor_violations,
        baseline_final_size_misses=baseline_final_size_misses,
        active_final_size_misses=active_final_size_misses,
        fallback_counts=tuple(sorted(fallback_counts.items())),
        performance_thresholds_met=performance_thresholds_met,
        active_eligible=performance_thresholds_met,
        blocking_reasons=tuple(performance_blocking_reasons),
    )


def quality_result_candidate_count(quality_result: Any) -> int:
    target_size_trace = object_dict(getattr(quality_result, "target_size_trace", None))
    if target_size_trace:
        count = int_value(target_size_trace.get("candidate_count"))
        return max(count, len(object_list(target_size_trace.get("candidates"))))
    quality_search_trace = object_dict(getattr(quality_result, "quality_search_trace", None))
    if quality_search_trace:
        count = int_value(quality_search_trace.get("candidate_count"))
        if count > 0:
            return count
        return max(
            1,
            sum(
                int_value(object_dict(attempt).get("candidate_count"))
                for attempt in object_list(quality_search_trace.get("attempts"))
            ),
        )
    return 1


def quality_result_target_changed(quality_result: Any) -> bool:
    trace = object_dict(getattr(quality_result, "quality_search_trace", None))
    attempts = object_list(trace.get("attempts"))
    if not attempts:
        return False
    initial_target = _strict_number(
        trace.get("initial_target")
        or object_dict(attempts[0]).get("metric_target")
    )
    selected_target = next(
        (
            _strict_number(object_dict(attempt).get("metric_target"))
            for attempt in reversed(attempts)
            if str(object_dict(attempt).get("status") or "") == "selected"
        ),
        _strict_number(getattr(quality_result, "target", None)),
    )
    return (
        initial_target is not None
        and selected_target is not None
        and not math.isclose(initial_target, selected_target, abs_tol=1e-6)
    )


def _quality_memory_arm(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> str | None:
    warm_start = object_dict(shadow.get("warm_start"))
    if warm_start.get("execution_mode") == "passive":
        return "passive"
    experiment_arm = str(warm_start.get("experiment_arm") or "")
    if experiment_arm:
        return experiment_arm
    outcome = _json_object(row.get("outcome_json"))
    outcome_arm = str(outcome.get("quality_memory_arm") or "")
    if outcome_arm:
        return outcome_arm
    if shadow.get("production_search_changed") is True:
        return "warm_start"
    return None


def _quality_memory_attempted(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> bool:
    if object_dict(shadow.get("warm_start")).get("execution_mode") == "passive":
        return False
    execution = object_dict(object_dict(shadow.get("warm_start")).get("execution"))
    if execution.get("attempted") is True or shadow.get("production_search_changed") is True:
        return True
    return _json_object(row.get("outcome_json")).get("quality_memory_attempted") is True


def quality_memory_scope_chain(
        source_rel_path: str,
        *,
        library_types: Mapping[str, str] | None,
) -> list[tuple[QualityMemoryScope, MediaScope]]:
    exact_scope = media_scope_from_prefix(
        source_rel_path,
        match="exact_item",
        library_types=library_types,
    )
    scopes: list[tuple[QualityMemoryScope, MediaScope]] = [("item", exact_scope)]
    group_scope = media_group_scope_for_rel_path(source_rel_path, library_types=library_types)
    if group_scope is not None and group_scope.kind == "tv_season":
        scopes.append(("season", group_scope))
    series_scope = tv_series_scope_for_rel_path(source_rel_path, library_types=library_types)
    if series_scope is not None:
        scopes.append(("series", series_scope))
    return scopes


def _resolve_current_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    current: dict[str, tuple[tuple[int, int, str], Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        observation_id = str(row.get("observation_id") or "").strip()
        search_run_id = str(row.get("search_run_id") or "").strip() or observation_id or f"row-{index}"
        authority = str(row.get("authority") or "").strip()
        rank = (
            _AUTHORITY_RANK.get(authority, 0),
            int_value(row.get("revision")),
            observation_id,
        )
        existing = current.get(search_run_id)
        if existing is None or rank > existing[0]:
            current[search_run_id] = (rank, row)
    return [current[search_run_id][1] for search_run_id in sorted(current)]


def deduplicate_quality_terminal_rows(
        rows: Iterable[Mapping[str, Any]],
        *,
        allow_unhashed_backfill: bool = False,
        retain_missing_policy: bool = False,
) -> list[Mapping[str, Any]]:
    selected: dict[tuple[str, str, str, str], tuple[tuple[int, int, str], Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        outcome_kind = str(row.get("outcome_kind") or OUTCOME_SELECTED)
        if outcome_kind not in {OUTCOME_SELECTED, OUTCOME_FINAL_SIZE_FAILURE}:
            continue
        policy_hash = str(row.get("policy_hash") or "").strip()
        if not policy_hash:
            if allow_unhashed_backfill and str(row.get("authority") or "") == AUTHORITY_STAGED_BACKFILL:
                policy_hash = "staged_backfill:unhashed"
            elif retain_missing_policy:
                policy_hash = f"missing:{str(row.get('authority') or 'unknown')}"
            else:
                continue
        key = (
            "/".join(part for part in str(row.get("source_rel_path") or "").strip().split("/") if part),
            str(row.get("source_fingerprint") or "").strip(),
            str(row.get("search_signature_id") or "").strip(),
            policy_hash,
        )
        if not all(key):
            continue
        rank = (
            _timestamp_rank(row.get("recorded_at")),
            _AUTHORITY_RANK.get(str(row.get("authority") or ""), 0),
            str(row.get("observation_id") or f"row-{index}"),
        )
        existing = selected.get(key)
        if existing is None or rank > existing[0]:
            selected[key] = (rank, row)
    return [row for _, row in selected.values()]


def _timestamp_rank(value: Any) -> int:
    parsed = _parse_timestamp(value)
    return int(_as_utc(parsed).timestamp()) if parsed is not None else -1


def analysis_family_id_from_context(context: Mapping[str, Any] | None) -> str | None:
    payload = object_dict(context)
    if not payload:
        return None
    encoder_parameters = str(payload.get("encoder_parameters") or "").casefold()
    pixel_format = str(payload.get("pixel_format") or "").casefold()
    width = int_value(payload.get("output_width"))
    height = int_value(payload.get("output_height"))
    family = {
        "intent": _optional_text(payload.get("compression_intent_id")),
        "encoder_family": _optional_text(payload.get("encoder")),
        "preset": _optional_text(payload.get("preset")),
        "pixel_class": _pixel_class(pixel_format),
        "resolution_bucket": _resolution_bucket(width, height),
        "cadence_bucket": _cadence_bucket(str(payload.get("video_filter") or "")),
        "metric": _optional_text(payload.get("metric")),
        "search_objective": _optional_text(payload.get("search_objective")),
        "source_codec": _optional_text(payload.get("source_codec")),
        "floor_policy": _strict_number(payload.get("minimum_quality_score")),
        "grain_category": _grain_category(encoder_parameters),
        "filter_category": _filter_category(str(payload.get("video_filter") or "")),
        "output_container": _optional_text(payload.get("output_container")),
    }
    return f"qaf1_{stable_json_hash(family)[:24]}"


def _pixel_class(pixel_format: str) -> str:
    chroma = "444" if "444" in pixel_format else "422" if "422" in pixel_format else "420"
    bit_depth = "12" if "12" in pixel_format else "10" if "10" in pixel_format else "8"
    return f"{chroma}p{bit_depth}"


def _resolution_bucket(width: int, height: int) -> str:
    pixels = width * height
    if pixels <= 0:
        return "unknown"
    if pixels <= 1_000_000:
        return "sd_or_720p"
    if pixels <= 2_500_000:
        return "1080p"
    if pixels <= 5_000_000:
        return "1440p"
    return "uhd_or_larger"


def _cadence_bucket(video_filter: str) -> str:
    return "cadence_transform" if any(
        token in video_filter.casefold() for token in ("yadif", "bwdif", "pullup", "fps=")
    ) else "native"


def _grain_category(encoder_parameters: str) -> str:
    if "film-grain-denoise=" in encoder_parameters and "film-grain-denoise=0" not in encoder_parameters:
        return "grain_denoise"
    if "film-grain=" in encoder_parameters and "film-grain=0" not in encoder_parameters:
        return "grain_preserved"
    return "no_grain_transform"


def _filter_category(video_filter: str) -> str:
    normalized = video_filter.casefold()
    if not normalized:
        return "none"
    if "scale=" in normalized:
        return "scale"
    if "crop=" in normalized:
        return "crop"
    return "other"


def _trace_rejection(
        trace: Mapping[str, Any],
        minimum_quality_score: float,
        *,
        selected_target: float,
) -> QualityShadowFallbackReason | None:
    curve = object_dict(trace.get("curve"))
    if str(curve.get("shape") or "") == "non_monotonic":
        return "non_monotonic_trace"
    attempts = object_list(trace.get("attempts"))
    initial_target = (
        _strict_number(object_dict(attempts[0]).get("metric_target"))
        if attempts
        else None
    )
    if initial_target is not None and not math.isclose(initial_target, selected_target, abs_tol=1e-6):
        return "search_target_changed"
    candidates = object_list(trace.get("candidates"))
    scores_by_crf: dict[float, list[float]] = {}
    floor_results_by_crf: dict[float, set[bool]] = {}
    for candidate_payload in candidates:
        candidate = object_dict(candidate_payload)
        crf = _strict_number(candidate.get("crf"))
        score = _strict_number(candidate.get("metric_score"))
        if crf is None or score is None:
            continue
        normalized_crf = round(crf, 3)
        scores_by_crf.setdefault(normalized_crf, []).append(score)
        quality_floor_met = candidate.get("quality_floor_met")
        if isinstance(quality_floor_met, bool):
            floor_results_by_crf.setdefault(normalized_crf, set()).add(quality_floor_met)
        elif score >= minimum_quality_score + QUALITY_SCORE_CONFLICT_MARGIN:
            floor_results_by_crf.setdefault(normalized_crf, set()).add(True)
        elif score < minimum_quality_score - QUALITY_SCORE_CONFLICT_MARGIN:
            floor_results_by_crf.setdefault(normalized_crf, set()).add(False)
    if any(len(results) > 1 for results in floor_results_by_crf.values()):
        return "conflicting_quality_evidence"
    points = sorted((crf, float(median(scores))) for crf, scores in scores_by_crf.items())
    for (_, previous_score), (_, next_score) in zip(points, points[1:]):
        if next_score > previous_score + QUALITY_SCORE_MONOTONIC_TOLERANCE:
            return "non_monotonic_trace"
    return None


def _selected_candidate_is_final_retry(trace: Mapping[str, Any]) -> bool:
    selected_candidate = object_dict(trace.get("selected_candidate"))
    return str(selected_candidate.get("role") or "") == "final_retry_measurement"


def _fallback_reason(
        fallback_reasons: Iterable[QualityShadowFallbackReason],
        *,
        exclusion_counts: Counter[str],
) -> QualityShadowFallbackReason:
    available = set(fallback_reasons)
    for reason in _FALLBACK_PRIORITY:
        if reason in available:
            return reason
    for reason in _FALLBACK_PRIORITY:
        if exclusion_counts[reason] > 0:
            return reason
    return "no_history"


def _fallback_detail(reason: QualityShadowFallbackReason, exclusions: Counter[str]) -> str:
    count = exclusions[reason]
    if reason == "sparse_cohort":
        return "Compatible observations exist, but no item, season, or series cohort has enough samples."
    if reason == "high_dispersion":
        return "Compatible observations are too dispersed for a stable first-CRF hint."
    if reason == "search_target_changed":
        return "Searches that changed their quality target are excluded from first-CRF projections."
    if reason == "non_monotonic_trace":
        return f"{count or 'Historical'} observation trace(s) were non-monotonic and were excluded."
    if reason == "conflicting_quality_evidence":
        return f"{count or 'Historical'} observation trace(s) contained conflicting quality-floor evidence."
    if reason == "final_retry_terminal":
        return (
            f"{count or 'Historical'} terminal result(s) came from bounded final-size retries and were "
            "excluded from first-CRF guidance."
        )
    if reason == "stale_signature":
        return "Historical observations do not match the current search signature or policy."
    if reason == "stale_evidence":
        return "Matching observations are older than the quality-memory age limit."
    if reason == "source_fingerprint_changed":
        return "Exact-item observations belong to a different source fingerprint."
    if reason == "source_fingerprint_unavailable":
        return "Exact-item guidance requires a current and historical source fingerprint."
    return "No compatible quality-search observations are available for a shadow recommendation."


def _confidence(scope: QualityMemoryScope, evidence_count: int) -> QualityMemoryConfidence:
    if evidence_count <= 0:
        return "none"
    if evidence_count < 4:
        return "limited"
    if evidence_count < 10 or scope == "series":
        return "moderate"
    return "high"


def _observation_completion_at(row: Mapping[str, Any]) -> datetime | None:
    timing = _json_object(row.get("timing_json"))
    parsed = _parse_timestamp(timing.get("encode_completed_at") or row.get("recorded_at"))
    return _as_utc(parsed) if parsed is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _exclusions(counts: Counter[str]) -> tuple[QualityShadowExclusion, ...]:
    return tuple(
        QualityShadowExclusion(reason=reason, count=count)
        for reason, count in sorted(counts.items())
        if count > 0
    )


def _size_error_percent(actual_output_bytes: int | None, target_size_bytes: int | None) -> float | None:
    if actual_output_bytes is None or target_size_bytes is None or target_size_bytes <= 0:
        return None
    return round((actual_output_bytes - target_size_bytes) / target_size_bytes * 100.0, 3)


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parse_timestamp(value: datetime | str | Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator > 0 else 0.0
