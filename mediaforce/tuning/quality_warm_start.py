from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from statistics import median
from typing import Any, Literal, Mapping

from mediaforce.core.db import DBClient
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.quality import QualitySearchWarmStart
from mediaforce.tuning.quality_memory import QualitySearchContext
from mediaforce.tuning.quality_observations import load_current_quality_search_observations
from mediaforce.tuning.quality_shadow import (
    MAX_EVIDENCE_OBSERVATION_IDS,
    MIN_SHADOW_RECOMMENDATIONS,
    QUALITY_SHADOW_ALGORITHM_VERSION,
    QualityShadowMetrics,
    QualityShadowRecommendation,
    deduplicate_quality_terminal_rows,
    quality_memory_scope_chain,
    quality_shadow_metrics,
    recommend_quality_search_from_rows,
)

QUALITY_WARM_START_SCHEMA_VERSION = 1
QUALITY_WARM_START_EXPERIMENT_VERSION = "qwa1"
QUALITY_WARM_START_HOLDOUT_PERCENT = 20

QualityWarmStartExperimentArm = Literal["ineligible", "passive"]
QualityWarmStartFutureArm = Literal["ineligible", "baseline_holdout", "warm_start"]

QualityWarmStartBlockReason = Literal[
    "recommendation_unavailable",
    "shadow_thresholds_not_met",
    "hint_adjustment_exceeds_one_crf",
    "baseline_benchmark_unavailable",
]


@dataclass(frozen=True, slots=True)
class QualityWarmStartPlan:
    recommendation: QualityShadowRecommendation
    readiness: QualityShadowMetrics
    candidate_crf: int | None
    adjusted: bool
    block_reason: QualityWarmStartBlockReason | None
    baseline_median_candidate_count: float | None
    baseline_median_search_seconds: float | None
    experiment_arm: QualityWarmStartExperimentArm = "passive"
    future_experiment_arm: QualityWarmStartFutureArm = "ineligible"
    scope_prefix: str | None = None

    @property
    def eligible(self) -> bool:
        return self.candidate_crf is not None and self.block_reason is None

    @property
    def active(self) -> bool:
        return False

    def search_hint(self) -> QualitySearchWarmStart | None:
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_WARM_START_SCHEMA_VERSION,
            "eligible": self.eligible,
            "block_reason": self.block_reason,
            "experiment_version": QUALITY_WARM_START_EXPERIMENT_VERSION,
            "experiment_arm": self.experiment_arm,
            "future_experiment_arm": self.future_experiment_arm,
            "execution_mode": "passive",
            "holdout_percent": QUALITY_WARM_START_HOLDOUT_PERCENT,
            "scope": self.recommendation.scope,
            "scope_prefix": self.scope_prefix,
            "search_signature_id": self.recommendation.search_signature_id,
            "requested_crf": self.recommendation.first_crf,
            "candidate_crf": self.candidate_crf,
            "adjusted": self.adjusted,
            "readiness": self.readiness.to_payload(),
            "baseline_median_candidate_count": self.baseline_median_candidate_count,
            "baseline_median_search_seconds": self.baseline_median_search_seconds,
        }


def plan_quality_warm_start(
        connection: DBClient,
        *,
        source_rel_path: str,
        source_fingerprint: str | None,
        expected_context: QualitySearchContext,
        policy_hash: str,
        configured_min_crf: int,
        configured_max_crf: int,
        search_run_id: str,
        as_of: datetime | str,
        library_types: Mapping[str, str] | None = None,
) -> QualityWarmStartPlan:
    scope_chain = quality_memory_scope_chain(source_rel_path, library_types=library_types)
    broadest_scope = scope_chain[-1][1]
    cutoff = as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)
    rows = load_current_quality_search_observations(
        connection,
        recorded_before=cutoff,
        scope=broadest_scope,
    )
    recommendation = recommend_quality_search_from_rows(
        rows,
        source_rel_path=source_rel_path,
        source_fingerprint=source_fingerprint,
        expected_context=expected_context,
        policy_hash=policy_hash,
        as_of=as_of,
        library_types=library_types,
        allow_unhashed_backfill=False,
    )
    cohort_scope = next(
        (scope for scope_name, scope in scope_chain if scope_name == recommendation.scope),
        broadest_scope,
    )
    context_rows = [
        row
        for row in rows
        if cohort_scope.includes(str(row.get("source_rel_path") or ""))
        and str(row.get("policy_hash") or "") == policy_hash
        and _row_matches_context(row, expected_context)
    ]
    verified_observation_ids = {
        str(row.get("observation_id") or "")
        for row in context_rows
        if str(row.get("observation_id") or "")
    }
    cohort_rows = deduplicate_quality_terminal_rows([
        row for row in context_rows
        if _shadow_evidence_is_verified(row, verified_observation_ids)
        and _row_matches_recommendation_scope(
            row,
            scope_kind=recommendation.scope,
            scope_prefix=cohort_scope.prefix,
        )
    ])
    readiness = quality_shadow_metrics(cohort_rows)
    baseline_candidate_count, baseline_search_seconds = _baseline_benchmarks(cohort_rows)
    if not recommendation.available:
        return QualityWarmStartPlan(
            recommendation=recommendation,
            readiness=readiness,
            candidate_crf=None,
            adjusted=False,
            block_reason="recommendation_unavailable",
            baseline_median_candidate_count=baseline_candidate_count,
            baseline_median_search_seconds=baseline_search_seconds,
            experiment_arm="ineligible",
            future_experiment_arm="ineligible",
            scope_prefix=cohort_scope.prefix if recommendation.scope is not None else None,
        )
    assert recommendation.first_crf is not None
    candidate_crf = _normalized_candidate_crf(
        recommendation.first_crf,
        minimum=configured_min_crf,
        maximum=configured_max_crf,
    )
    adjusted = not math.isclose(candidate_crf, recommendation.first_crf, abs_tol=1e-6)
    if not readiness.active_eligible:
        block_reason = "shadow_thresholds_not_met"
    elif abs(candidate_crf - recommendation.first_crf) > 1.0:
        block_reason = "hint_adjustment_exceeds_one_crf"
    elif baseline_candidate_count is None or baseline_search_seconds is None:
        block_reason = "baseline_benchmark_unavailable"
    else:
        block_reason = None
    future_experiment_arm: QualityWarmStartFutureArm = (
        _quality_warm_start_experiment_arm(source_rel_path, source_fingerprint)
        if block_reason is None
        else "ineligible"
    )
    return QualityWarmStartPlan(
        recommendation=recommendation,
        readiness=readiness,
        candidate_crf=candidate_crf,
        adjusted=adjusted,
        block_reason=block_reason,
        baseline_median_candidate_count=baseline_candidate_count,
        baseline_median_search_seconds=baseline_search_seconds,
        experiment_arm="passive" if block_reason is None else "ineligible",
        future_experiment_arm=future_experiment_arm,
        scope_prefix=cohort_scope.prefix,
    )


def _row_matches_context(row: Mapping[str, Any], context: QualitySearchContext) -> bool:
    return str(row.get("search_signature_id") or "") == context.signature_id


def _row_matches_recommendation_scope(
        row: Mapping[str, Any],
        *,
        scope_kind: str | None,
        scope_prefix: str,
) -> bool:
    if scope_kind is None:
        return True
    payload = _json_object(row.get("shadow_json"))
    recommendation = object_dict(payload.get("recommendation"))
    if not recommendation:
        return True
    if str(recommendation.get("scope") or "") != scope_kind:
        return False
    source_rel_path = "/".join(
        part for part in str(row.get("source_rel_path") or "").strip().split("/") if part
    )
    if scope_kind == "item":
        return source_rel_path == scope_prefix
    return source_rel_path == scope_prefix or source_rel_path.startswith(f"{scope_prefix}/")


def _quality_warm_start_experiment_arm(
        source_rel_path: str,
        source_fingerprint: str | None,
) -> QualityWarmStartFutureArm:
    normalized_source_path = "/".join(part for part in source_rel_path.strip().split("/") if part)
    content_version = str(source_fingerprint or "").strip()
    if not normalized_source_path or not content_version:
        return "ineligible"
    digest = hashlib.sha256(
        f"{QUALITY_WARM_START_EXPERIMENT_VERSION}:{normalized_source_path}:{content_version}".encode("utf-8")
    ).digest()
    bucket = int.from_bytes(digest[:8], byteorder="big") % 100
    return "baseline_holdout" if bucket < QUALITY_WARM_START_HOLDOUT_PERCENT else "warm_start"


def _baseline_benchmarks(rows: list[Mapping[str, Any]]) -> tuple[float | None, float | None]:
    benchmarks: list[tuple[float, float]] = []
    for row in rows:
        payload = _json_object(row.get("shadow_json"))
        if (
                payload.get("algorithm_version") != QUALITY_SHADOW_ALGORITHM_VERSION
                or payload.get("production_search_changed") is True
                or not object_dict(payload.get("recommendation"))
        ):
            continue
        comparison = object_dict(payload.get("comparison"))
        if comparison.get("evaluation_exclusion_reason"):
            continue
        candidate_count = int_value(comparison.get("candidate_count"))
        duration = _number(comparison.get("search_duration_seconds"))
        if candidate_count > 0 and duration is not None and duration >= 0:
            benchmarks.append((float(candidate_count), duration))
    if len(benchmarks) < MIN_SHADOW_RECOMMENDATIONS:
        return None, None
    return (
        float(median(candidate_count for candidate_count, _ in benchmarks)),
        float(median(duration for _, duration in benchmarks)),
    )


def _shadow_evidence_is_verified(row: Mapping[str, Any], verified_observation_ids: set[str]) -> bool:
    payload = _json_object(row.get("shadow_json"))
    recommendation = object_dict(payload.get("recommendation"))
    if not recommendation:
        return True
    evidence_ids = [
        str(value)
        for value in object_list(recommendation.get("evidence_observation_ids"))
        if str(value).strip()
    ]
    sample_count = int_value(recommendation.get("sample_count"))
    return (
        len(evidence_ids) == min(sample_count, MAX_EVIDENCE_OBSERVATION_IDS)
        and bool(evidence_ids)
        and all(observation_id in verified_observation_ids for observation_id in evidence_ids)
    )


def _normalized_candidate_crf(value: float, *, minimum: int, maximum: int) -> int:
    lower = min(minimum, maximum)
    upper = max(minimum, maximum)
    rounded = math.floor(value + 0.5)
    return min(max(rounded, lower), upper)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
