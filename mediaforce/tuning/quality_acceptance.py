from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from statistics import median
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events
from mediaforce.core.type_defs import object_dict
from mediaforce.library.media_scopes import (
    MediaScope,
    media_group_scope_for_rel_path,
    media_scope_from_prefix,
    normalize_scope_prefix,
)
from mediaforce.tuning.content_intent_observations import (
    load_current_content_intent_boundary_observations,
)
from mediaforce.tuning.quality_observations import (
    OUTCOME_FINAL_SIZE_FAILURE,
    OUTCOME_SELECTED,
    load_current_quality_search_observations,
)
from mediaforce.tuning.quality_shadow import (
    MIN_SHADOW_RECOMMENDATIONS,
    QUALITY_SHADOW_ALGORITHM_VERSION,
    QualityShadowMetrics,
    deduplicate_quality_terminal_rows,
    quality_memory_scope_chain,
    quality_shadow_metrics,
)
from mediaforce.tuning.quality_warm_start import QUALITY_WARM_START_EXPERIMENT_VERSION

QUALITY_ACCEPTANCE_SCHEMA_VERSION = 1
MIN_ACTIVE_ATTEMPTS = 20
MIN_HOLDOUT_RUNS = 10
MIN_DISTINCT_ITEMS = 20
MIN_HOLDOUT_DISTINCT_ITEMS = 10
MIN_OBSERVED_SAVINGS_RATE = 0.20


@dataclass(frozen=True, slots=True)
class QualityAcceptanceGroupKey:
    scope_kind: str
    scope_prefix: str
    search_signature_id: str
    policy_hash: str

    def to_payload(self) -> dict[str, str]:
        return {
            "scope_kind": self.scope_kind,
            "scope_prefix": self.scope_prefix,
            "search_signature_id": self.search_signature_id,
            "policy_hash": self.policy_hash,
        }


@dataclass(frozen=True, slots=True)
class QualityAcceptanceActiveObserved:
    assigned_warm_runs: int
    attempted_warm_runs: int
    accepted_warm_runs: int
    fallback_runs: int
    unclassified_warm_runs: int
    failed_warm_runs: int
    legacy_active_runs: int
    warm_signature_mismatch_runs: int
    holdout_signature_mismatch_runs: int
    terminal_event_failures: int
    holdout_runs: int
    failed_holdout_runs: int
    complete_warm_metric_runs: int
    complete_holdout_metric_runs: int
    complete_warm_distinct_items: int
    complete_holdout_distinct_items: int
    median_warm_candidate_count: float | None
    median_holdout_candidate_count: float | None
    observed_candidate_savings_rate: float | None
    median_warm_search_seconds: float | None
    median_holdout_search_seconds: float | None
    observed_search_time_savings_rate: float | None
    median_warm_workflow_seconds: float | None
    median_holdout_workflow_seconds: float | None
    observed_workflow_time_savings_rate: float | None
    fallback_counts: tuple[tuple[str, int], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "assigned_warm_runs": self.assigned_warm_runs,
            "attempted_warm_runs": self.attempted_warm_runs,
            "accepted_warm_runs": self.accepted_warm_runs,
            "fallback_runs": self.fallback_runs,
            "unclassified_warm_runs": self.unclassified_warm_runs,
            "failed_warm_runs": self.failed_warm_runs,
            "legacy_active_runs": self.legacy_active_runs,
            "warm_signature_mismatch_runs": self.warm_signature_mismatch_runs,
            "holdout_signature_mismatch_runs": self.holdout_signature_mismatch_runs,
            "terminal_event_failures": self.terminal_event_failures,
            "holdout_runs": self.holdout_runs,
            "failed_holdout_runs": self.failed_holdout_runs,
            "complete_warm_metric_runs": self.complete_warm_metric_runs,
            "complete_holdout_metric_runs": self.complete_holdout_metric_runs,
            "complete_warm_distinct_items": self.complete_warm_distinct_items,
            "complete_holdout_distinct_items": self.complete_holdout_distinct_items,
            "median_warm_candidate_count": self.median_warm_candidate_count,
            "median_holdout_candidate_count": self.median_holdout_candidate_count,
            "observed_candidate_savings_rate": self.observed_candidate_savings_rate,
            "median_warm_search_seconds": self.median_warm_search_seconds,
            "median_holdout_search_seconds": self.median_holdout_search_seconds,
            "observed_search_time_savings_rate": self.observed_search_time_savings_rate,
            "median_warm_workflow_seconds": self.median_warm_workflow_seconds,
            "median_holdout_workflow_seconds": self.median_holdout_workflow_seconds,
            "observed_workflow_time_savings_rate": self.observed_workflow_time_savings_rate,
            "fallback_counts": dict(self.fallback_counts),
        }


@dataclass(frozen=True, slots=True)
class QualityAcceptanceSafety:
    passive_quality_floor_violations: int
    active_quality_floor_violations: int
    baseline_final_size_misses: int
    active_final_size_misses: int

    def to_payload(self) -> dict[str, int]:
        return {
            "passive_quality_floor_violations": self.passive_quality_floor_violations,
            "active_quality_floor_violations": self.active_quality_floor_violations,
            "baseline_final_size_misses": self.baseline_final_size_misses,
            "active_final_size_misses": self.active_final_size_misses,
        }


@dataclass(frozen=True, slots=True)
class QualityAcceptanceOperatorOutcomes:
    current_observations: int
    rejection_count: int
    rejection_rate: float
    additional_attention_count: int
    additional_attention_rate: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "current_observations": self.current_observations,
            "rejection_count": self.rejection_count,
            "rejection_rate": self.rejection_rate,
            "additional_attention_count": self.additional_attention_count,
            "additional_attention_rate": self.additional_attention_rate,
        }


@dataclass(frozen=True, slots=True)
class QualityAcceptanceGroup:
    key: QualityAcceptanceGroupKey
    distinct_items: int
    passive_distinct_items: int
    natural_cluster_count: int
    largest_cluster_concentration: float
    passive_benchmark_runs: int
    passive_readiness: QualityShadowMetrics
    active_observed: QualityAcceptanceActiveObserved
    safety_and_data_quality: QualityAcceptanceSafety
    closure_eligible: bool
    closure_blocking_reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key.to_payload(),
            "distinct_items": self.distinct_items,
            "passive_distinct_items": self.passive_distinct_items,
            "natural_cluster_count": self.natural_cluster_count,
            "largest_cluster_concentration": self.largest_cluster_concentration,
            "passive_benchmark_runs": self.passive_benchmark_runs,
            "passive_readiness": self.passive_readiness.to_payload(),
            "active_observed": self.active_observed.to_payload(),
            "safety_and_data_quality": self.safety_and_data_quality.to_payload(),
            "closure_eligible": self.closure_eligible,
            "closure_blocking_reasons": list(self.closure_blocking_reasons),
        }


@dataclass(frozen=True, slots=True)
class QualityAcceptanceReport:
    groups: tuple[QualityAcceptanceGroup, ...]
    unattributed_final_size_failures: int
    requested_prefix: str | None
    rows_considered: int
    missing_policy_rows: int
    operator_outcomes: QualityAcceptanceOperatorOutcomes

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_ACCEPTANCE_SCHEMA_VERSION,
            "groups": [group.to_payload() for group in self.groups],
            "unattributed_final_size_failures": self.unattributed_final_size_failures,
            "requested_prefix": self.requested_prefix,
            "rows_considered": self.rows_considered,
            "missing_policy_rows": self.missing_policy_rows,
            "operator_outcomes": self.operator_outcomes.to_payload(),
        }


def load_quality_acceptance_report(
        connection: DBClient,
        *,
        library_types: Mapping[str, str] | None = None,
        prefix: str | None = None,
) -> QualityAcceptanceReport:
    terminal_events = list(
        connection.execute(
            select(item_events.c.event_type, item_events.c.details_json)
            .where(item_events.c.event_type == "encoding_failed")
        ).mappings()
    )
    return quality_acceptance_report(
        load_current_quality_search_observations(connection),
        terminal_events=terminal_events,
        boundary_rows=load_current_content_intent_boundary_observations(
            connection,
            include_withdrawn=True,
        ),
        library_types=library_types,
        prefix=prefix,
    )


def quality_acceptance_report(
        rows: Iterable[Mapping[str, Any]],
        *,
        terminal_events: Iterable[Mapping[str, Any]] = (),
        boundary_rows: Iterable[Mapping[str, Any]] = (),
        library_types: Mapping[str, str] | None = None,
        prefix: str | None = None,
) -> QualityAcceptanceReport:
    row_list = _resolve_current_rows(rows)
    terminal_event_list = list(terminal_events)
    group_scopes = _report_group_scopes(row_list, library_types=library_types)
    requested_prefix = normalize_scope_prefix(prefix or "") or None
    groups = tuple(
        _acceptance_group(
            row_list,
            key=key,
            scope=scope,
            terminal_events=terminal_event_list,
            library_types=library_types,
        )
        for key, scope in sorted(
            group_scopes.items(),
            key=lambda item: (
                item[0].scope_prefix,
                item[0].search_signature_id,
                item[0].policy_hash,
            ),
        )
        if _scope_matches_prefix(scope, requested_prefix)
    )
    relevant_rows = [
        row for row in row_list
        if _prefix_includes(requested_prefix, str(row.get("source_rel_path") or ""))
    ]
    unattributed_final_size_failures = sum(
        str(row.get("outcome_kind") or "") == OUTCOME_FINAL_SIZE_FAILURE
        and not str(row.get("search_signature_id") or "")
        for row in relevant_rows
    )
    return QualityAcceptanceReport(
        groups=groups,
        unattributed_final_size_failures=unattributed_final_size_failures,
        requested_prefix=requested_prefix,
        rows_considered=len(relevant_rows),
        missing_policy_rows=sum(not str(row.get("policy_hash") or "") for row in relevant_rows),
        operator_outcomes=_operator_outcomes(boundary_rows),
    )


def format_quality_acceptance_report(report: QualityAcceptanceReport) -> str:
    lines = [
        "Quality-memory acceptance",
        (
            f"groups={len(report.groups)} rows={report.rows_considered} "
            f"unattributed_final_size_failures={report.unattributed_final_size_failures} "
            f"missing_policy_rows={report.missing_policy_rows} "
            f"operator_rejections={report.operator_outcomes.rejection_count} "
            f"operator_attention={report.operator_outcomes.additional_attention_count}"
        ),
    ]
    if report.requested_prefix is not None:
        lines.append(f"prefix={report.requested_prefix}")
    if not report.groups:
        lines.append("No recommendation-bearing quality-memory groups are available.")
        return "\n".join(lines)
    for group in report.groups:
        key = group.key
        passive = group.passive_readiness
        active = group.active_observed
        safety = group.safety_and_data_quality
        lines.extend(
            [
                "",
                (
                    f"{key.scope_kind} {key.scope_prefix} "
                    f"signature={key.search_signature_id[:16]} policy={key.policy_hash[:16]}"
                ),
                (
                    "  passive "
                    f"recommendations={passive.recommendation_runs} "
                    f"benchmarks={group.passive_benchmark_runs} "
                    f"distinct_units={group.passive_distinct_items} "
                    f"clusters={group.natural_cluster_count} "
                    f"largest_cluster={_percent(group.largest_cluster_concentration)} "
                    f"within_one={_percent(passive.within_one_rate)} "
                    f"projected_candidates={_percent(passive.median_candidate_savings_rate)} "
                    f"eligible={'yes' if passive.active_eligible else 'no'}"
                ),
                (
                    "  active "
                    f"assigned={active.assigned_warm_runs} attempted={active.attempted_warm_runs} "
                    f"accepted={active.accepted_warm_runs} fallback={active.fallback_runs} "
                    f"unclassified={active.unclassified_warm_runs} failed={active.failed_warm_runs} "
                    f"holdout={active.holdout_runs} holdout_failed={active.failed_holdout_runs} "
                    f"legacy={active.legacy_active_runs} "
                    f"warm_mismatch={active.warm_signature_mismatch_runs} "
                    f"holdout_mismatch={active.holdout_signature_mismatch_runs} "
                    f"terminal_event_failures={active.terminal_event_failures}"
                ),
                (
                    "  observed "
                    f"candidates={_percent(active.observed_candidate_savings_rate)} "
                    f"search={_percent(active.observed_search_time_savings_rate)} "
                    f"workflow={_percent(active.observed_workflow_time_savings_rate)} "
                    f"complete={active.complete_warm_metric_runs}/{active.complete_holdout_metric_runs} "
                    f"distinct={active.complete_warm_distinct_items}/{active.complete_holdout_distinct_items}"
                ),
                (
                    "  safety "
                    f"passive_floor={safety.passive_quality_floor_violations} "
                    f"active_floor={safety.active_quality_floor_violations} "
                    f"baseline_size_miss={safety.baseline_final_size_misses} "
                    f"active_size_miss={safety.active_final_size_misses}"
                ),
                (
                    f"  closure={'ready' if group.closure_eligible else 'blocked'} "
                    f"reasons={','.join(group.closure_blocking_reasons) or 'none'}"
                ),
            ]
        )
    return "\n".join(lines)


def _report_group_scopes(
        rows: Iterable[Mapping[str, Any]],
        *,
        library_types: Mapping[str, str] | None,
) -> dict[QualityAcceptanceGroupKey, MediaScope]:
    groups: dict[QualityAcceptanceGroupKey, MediaScope] = {}
    for row in rows:
        group = _row_group(row, library_types=library_types)
        if group is not None:
            key, scope = group
            groups[key] = scope
    return groups


def _row_group(
        row: Mapping[str, Any],
        *,
        library_types: Mapping[str, str] | None,
) -> tuple[QualityAcceptanceGroupKey, MediaScope] | None:
    shadow = _json_object(row.get("shadow_json"))
    recommendation = object_dict(shadow.get("recommendation"))
    warm_start = object_dict(shadow.get("warm_start"))
    outcome = _json_object(row.get("outcome_json"))
    experiment_arm = _assigned_experiment_arm(row, shadow)
    scope_name = str(
        (
            warm_start.get("scope")
            or outcome.get("quality_memory_scope")
        )
        if experiment_arm is not None
        else recommendation.get("scope")
        or ""
    )
    planned_signature_id = str(
        warm_start.get("search_signature_id")
        or outcome.get("quality_memory_search_signature_id")
        or ""
    )
    signature_id = (
        planned_signature_id
        if experiment_arm is not None and planned_signature_id
        else str(row.get("search_signature_id") or "")
    )
    policy_hash = str(row.get("policy_hash") or "")
    if scope_name not in {"item", "season", "series"} or not signature_id or not policy_hash:
        return None
    scope_prefix = str(
        (
            warm_start.get("scope_prefix")
            or outcome.get("quality_memory_scope_prefix")
        )
        if experiment_arm is not None
        else ""
    )
    scope: MediaScope | None = None
    if scope_prefix:
        scope = media_scope_from_prefix(
            scope_prefix,
            match="exact_item" if scope_name == "item" else "descendants",
            library_types=library_types,
        )
    if scope is None:
        scope = next(
            (
                candidate_scope
                for candidate_name, candidate_scope in quality_memory_scope_chain(
                    str(row.get("source_rel_path") or ""),
                    library_types=library_types,
                )
                if candidate_name == scope_name
            ),
            None,
        )
    if scope is None:
        return None
    return (
        QualityAcceptanceGroupKey(
            scope_kind=scope_name,
            scope_prefix=scope.prefix,
            search_signature_id=signature_id,
            policy_hash=policy_hash,
        ),
        scope,
    )


def _terminal_event_group_key(event: Mapping[str, Any]) -> QualityAcceptanceGroupKey | None:
    if str(event.get("event_type") or "") != "encoding_failed":
        return None
    details = _json_object(event.get("details_json"))
    plan = object_dict(details.get("quality_memory_plan"))
    if (
            str(plan.get("experiment_version") or "") != QUALITY_WARM_START_EXPERIMENT_VERSION
            or str(plan.get("experiment_arm") or "") != "warm_start"
    ):
        return None
    scope_kind = str(plan.get("scope") or "")
    scope_prefix = str(plan.get("scope_prefix") or "")
    signature_id = str(plan.get("search_signature_id") or "")
    policy_hash = str(details.get("quality_memory_policy_hash") or "")
    if (
            scope_kind not in {"item", "season", "series"}
            or not scope_prefix
            or not signature_id
            or not policy_hash
    ):
        return None
    return QualityAcceptanceGroupKey(
        scope_kind=scope_kind,
        scope_prefix=scope_prefix,
        search_signature_id=signature_id,
        policy_hash=policy_hash,
    )


def _acceptance_group(
        rows: Iterable[Mapping[str, Any]],
        *,
        key: QualityAcceptanceGroupKey,
        scope: MediaScope,
        terminal_events: Iterable[Mapping[str, Any]],
        library_types: Mapping[str, str] | None,
) -> QualityAcceptanceGroup:
    safety_scope_rows = [
        row
        for row in rows
        if scope.includes(str(row.get("source_rel_path") or ""))
        and _row_matches_group_signature(row, key.search_signature_id)
        and str(row.get("policy_hash") or "") == key.policy_hash
    ]
    group_rows = [
        row
        for row in safety_scope_rows
        if (row_group := _row_group(row, library_types=library_types)) is None
        or row_group[0] == key
    ]
    passive_readiness = quality_shadow_metrics(group_rows)
    passive_benchmark_runs = _passive_benchmark_runs(group_rows)
    active_observed = _active_observed(
        group_rows,
        terminal_events=[
            event for event in terminal_events
            if _terminal_event_group_key(event) == key
        ],
    )
    safety = _safety(safety_scope_rows)
    distinct_items = len({str(row.get("source_rel_path") or "") for row in group_rows})
    passive_rows = deduplicate_quality_terminal_rows([
        row for row in group_rows
        if _assigned_experiment_arm(row, _json_object(row.get("shadow_json"))) != "warm_start"
        and _is_current_shadow_row(row)
    ])
    passive_distinct_items = len({str(row.get("source_rel_path") or "") for row in passive_rows})
    cluster_items = {
        (
            _natural_cluster_id(source_rel_path, library_types=library_types),
            source_rel_path,
        )
        for row in passive_rows
        if (source_rel_path := str(row.get("source_rel_path") or ""))
    }
    clusters = Counter(cluster_id for cluster_id, _ in cluster_items)
    largest_cluster_concentration = (
        round(max(clusters.values()) / passive_distinct_items, 6)
        if clusters and passive_distinct_items > 0
        else 0.0
    )
    closure_blocking_reasons = _closure_blocking_reasons(
        passive_readiness=passive_readiness,
        passive_benchmark_runs=passive_benchmark_runs,
        active=active_observed,
        safety=safety,
    )
    return QualityAcceptanceGroup(
        key=key,
        distinct_items=distinct_items,
        passive_distinct_items=passive_distinct_items,
        natural_cluster_count=len(clusters),
        largest_cluster_concentration=largest_cluster_concentration,
        passive_benchmark_runs=passive_benchmark_runs,
        passive_readiness=passive_readiness,
        active_observed=active_observed,
        safety_and_data_quality=safety,
        closure_eligible=not closure_blocking_reasons,
        closure_blocking_reasons=tuple(closure_blocking_reasons),
    )


def _active_observed(
        rows: Iterable[Mapping[str, Any]],
        *,
        terminal_events: Iterable[Mapping[str, Any]],
) -> QualityAcceptanceActiveObserved:
    warm_metric_rows: list[Mapping[str, Any]] = []
    holdout_metric_rows: list[Mapping[str, Any]] = []
    assigned_warm_runs = 0
    holdout_runs = 0
    attempted_runs = 0
    accepted_runs = 0
    fallback_runs = 0
    unclassified_warm_runs = 0
    failed_warm_runs = 0
    failed_holdout_runs = 0
    legacy_active_runs = 0
    warm_signature_mismatch_runs = 0
    holdout_signature_mismatch_runs = 0
    fallback_counts: Counter[str] = Counter()
    for row in rows:
        shadow = _json_object(row.get("shadow_json"))
        arm = _assigned_experiment_arm(row, shadow)
        if arm is None and shadow.get("production_search_changed") is True:
            legacy_active_runs += 1
            continue
        if arm is not None and _quality_memory_signature_mismatch(row, shadow):
            if arm == "warm_start":
                warm_signature_mismatch_runs += 1
            else:
                holdout_signature_mismatch_runs += 1
            continue
        if arm == "warm_start":
            assigned_warm_runs += 1
            execution = _warm_start_execution(row, shadow)
            attempted = _quality_memory_attempted(row, shadow)
            if attempted:
                attempted_runs += 1
            if str(row.get("outcome_kind") or "") != OUTCOME_SELECTED:
                failed_warm_runs += 1
                continue
            status = str(execution.get("status") or "")
            classified = False
            if attempted and status == "accepted":
                accepted_runs += 1
                classified = True
            elif attempted and execution.get("fallback_used") is True:
                fallback_runs += 1
                fallback_counts[str(execution.get("fallback_reason") or "unknown")] += 1
                classified = True
            elif attempted:
                unclassified_warm_runs += 1
            if classified and _complete_metrics(row) is not None:
                warm_metric_rows.append(row)
        elif arm == "baseline_holdout":
            holdout_runs += 1
            if str(row.get("outcome_kind") or "") != OUTCOME_SELECTED:
                failed_holdout_runs += 1
                continue
            if _complete_metrics(row) is not None:
                holdout_metric_rows.append(row)
    warm_candidate_counts = [metrics[0] for row in warm_metric_rows if (metrics := _complete_metrics(row))]
    holdout_candidate_counts = [metrics[0] for row in holdout_metric_rows if (metrics := _complete_metrics(row))]
    warm_search_seconds = [metrics[1] for row in warm_metric_rows if (metrics := _complete_metrics(row))]
    holdout_search_seconds = [metrics[1] for row in holdout_metric_rows if (metrics := _complete_metrics(row))]
    warm_workflow_seconds = [metrics[2] for row in warm_metric_rows if (metrics := _complete_metrics(row))]
    holdout_workflow_seconds = [metrics[2] for row in holdout_metric_rows if (metrics := _complete_metrics(row))]
    median_warm_candidates = _median_or_none(warm_candidate_counts)
    median_holdout_candidates = _median_or_none(holdout_candidate_counts)
    median_warm_search = _median_or_none(warm_search_seconds)
    median_holdout_search = _median_or_none(holdout_search_seconds)
    median_warm_workflow = _median_or_none(warm_workflow_seconds)
    median_holdout_workflow = _median_or_none(holdout_workflow_seconds)
    return QualityAcceptanceActiveObserved(
        assigned_warm_runs=assigned_warm_runs,
        attempted_warm_runs=attempted_runs,
        accepted_warm_runs=accepted_runs,
        fallback_runs=fallback_runs,
        unclassified_warm_runs=unclassified_warm_runs,
        failed_warm_runs=failed_warm_runs,
        legacy_active_runs=legacy_active_runs,
        warm_signature_mismatch_runs=warm_signature_mismatch_runs,
        holdout_signature_mismatch_runs=holdout_signature_mismatch_runs,
        terminal_event_failures=sum(1 for _ in terminal_events),
        holdout_runs=holdout_runs,
        failed_holdout_runs=failed_holdout_runs,
        complete_warm_metric_runs=len(warm_metric_rows),
        complete_holdout_metric_runs=len(holdout_metric_rows),
        complete_warm_distinct_items=len(
            {str(row.get("source_rel_path") or "") for row in warm_metric_rows}
        ),
        complete_holdout_distinct_items=len(
            {str(row.get("source_rel_path") or "") for row in holdout_metric_rows}
        ),
        median_warm_candidate_count=median_warm_candidates,
        median_holdout_candidate_count=median_holdout_candidates,
        observed_candidate_savings_rate=_savings_rate(median_warm_candidates, median_holdout_candidates),
        median_warm_search_seconds=median_warm_search,
        median_holdout_search_seconds=median_holdout_search,
        observed_search_time_savings_rate=_savings_rate(median_warm_search, median_holdout_search),
        median_warm_workflow_seconds=median_warm_workflow,
        median_holdout_workflow_seconds=median_holdout_workflow,
        observed_workflow_time_savings_rate=_savings_rate(median_warm_workflow, median_holdout_workflow),
        fallback_counts=tuple(sorted(fallback_counts.items())),
    )


def _safety(rows: Iterable[Mapping[str, Any]]) -> QualityAcceptanceSafety:
    passive_quality_floor_violations = 0
    active_quality_floor_violations = 0
    baseline_final_size_misses = 0
    active_final_size_misses = 0
    for row in rows:
        shadow = _json_object(row.get("shadow_json"))
        comparison = object_dict(shadow.get("comparison"))
        arm = _assigned_experiment_arm(row, shadow)
        warm_start_attempted = (
            arm == "warm_start" or shadow.get("production_search_changed") is True
        ) and _quality_memory_attempted(row, shadow)
        if comparison.get("production_quality_floor_violation") is True:
            if warm_start_attempted:
                active_quality_floor_violations += 1
            else:
                passive_quality_floor_violations += 1
        final_size_miss = (
            comparison.get("final_size_miss") is True
            or str(row.get("outcome_kind") or "") == OUTCOME_FINAL_SIZE_FAILURE
        )
        if final_size_miss:
            if warm_start_attempted:
                active_final_size_misses += 1
            else:
                baseline_final_size_misses += 1
    return QualityAcceptanceSafety(
        passive_quality_floor_violations=passive_quality_floor_violations,
        active_quality_floor_violations=active_quality_floor_violations,
        baseline_final_size_misses=baseline_final_size_misses,
        active_final_size_misses=active_final_size_misses,
    )


def _closure_blocking_reasons(
        *,
        passive_readiness: QualityShadowMetrics,
        passive_benchmark_runs: int,
        active: QualityAcceptanceActiveObserved,
        safety: QualityAcceptanceSafety,
) -> list[str]:
    reasons: list[str] = []
    if not passive_readiness.active_eligible:
        reasons.append("passive_readiness_not_met")
    if passive_benchmark_runs < MIN_SHADOW_RECOMMENDATIONS:
        reasons.append("insufficient_passive_benchmarks")
    if active.attempted_warm_runs < MIN_ACTIVE_ATTEMPTS:
        reasons.append("insufficient_active_attempts")
    if active.complete_warm_metric_runs < MIN_ACTIVE_ATTEMPTS:
        reasons.append("insufficient_complete_warm_metrics")
    if active.complete_holdout_metric_runs < MIN_HOLDOUT_RUNS:
        reasons.append("insufficient_holdout_runs")
    if active.complete_warm_distinct_items < MIN_DISTINCT_ITEMS:
        reasons.append("insufficient_warm_distinct_items")
    if active.complete_holdout_distinct_items < MIN_HOLDOUT_DISTINCT_ITEMS:
        reasons.append("insufficient_holdout_distinct_items")
    if active.unclassified_warm_runs:
        reasons.append("unclassified_warm_run")
    if active.failed_warm_runs:
        reasons.append("warm_terminal_failure")
    if active.legacy_active_runs:
        reasons.append("legacy_active_evidence")
    if active.warm_signature_mismatch_runs:
        reasons.append("warm_signature_mismatch")
    if active.terminal_event_failures:
        reasons.append("warm_terminal_event_failure")
    for value, reason in (
        (active.observed_candidate_savings_rate, "candidate_savings_below_threshold"),
        (active.observed_search_time_savings_rate, "search_time_savings_below_threshold"),
        (active.observed_workflow_time_savings_rate, "workflow_time_savings_below_threshold"),
    ):
        if value is None:
            reasons.append(reason.replace("below_threshold", "unmeasurable"))
        elif value < MIN_OBSERVED_SAVINGS_RATE:
            reasons.append(reason)
    if safety.passive_quality_floor_violations or safety.active_quality_floor_violations:
        reasons.append("quality_floor_violation")
    if safety.active_final_size_misses:
        reasons.append("active_final_size_miss")
    return reasons


def _assigned_experiment_arm(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> str | None:
    warm_start = object_dict(shadow.get("warm_start"))
    plan_version = str(warm_start.get("experiment_version") or "")
    plan_arm = str(warm_start.get("experiment_arm") or "")
    if plan_version == QUALITY_WARM_START_EXPERIMENT_VERSION and plan_arm in {"warm_start", "baseline_holdout"}:
        return plan_arm
    outcome = _json_object(row.get("outcome_json"))
    outcome_version = str(outcome.get("quality_memory_experiment_version") or "")
    outcome_arm = str(outcome.get("quality_memory_arm") or "")
    if (
            outcome_version == QUALITY_WARM_START_EXPERIMENT_VERSION
            and outcome_arm in {"warm_start", "baseline_holdout"}
    ):
        return outcome_arm
    return None


def _row_matches_group_signature(row: Mapping[str, Any], signature_id: str) -> bool:
    if str(row.get("search_signature_id") or "") == signature_id:
        return True
    shadow = _json_object(row.get("shadow_json"))
    if _assigned_experiment_arm(row, shadow) is None:
        return False
    warm_start = object_dict(shadow.get("warm_start"))
    outcome = _json_object(row.get("outcome_json"))
    planned_signature_id = str(
        warm_start.get("search_signature_id")
        or outcome.get("quality_memory_search_signature_id")
        or ""
    )
    return planned_signature_id == signature_id


def _quality_memory_signature_mismatch(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> bool:
    execution = _warm_start_execution(row, shadow)
    if execution.get("signature_match") is False:
        return True
    warm_start = object_dict(shadow.get("warm_start"))
    outcome = _json_object(row.get("outcome_json"))
    planned_signature_id = str(
        warm_start.get("search_signature_id")
        or outcome.get("quality_memory_search_signature_id")
        or ""
    )
    final_signature_id = str(row.get("search_signature_id") or "")
    return bool(planned_signature_id and final_signature_id and planned_signature_id != final_signature_id)


def _quality_memory_attempted(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> bool:
    execution = _warm_start_execution(row, shadow)
    if execution.get("attempted") is True or shadow.get("production_search_changed") is True:
        return True
    return _json_object(row.get("outcome_json")).get("quality_memory_attempted") is True


def _warm_start_execution(row: Mapping[str, Any], shadow: Mapping[str, Any]) -> dict[str, Any]:
    execution = object_dict(object_dict(shadow.get("warm_start")).get("execution"))
    if execution:
        return execution
    return object_dict(_json_object(row.get("candidate_trace_json")).get("warm_start"))


def _complete_metrics(row: Mapping[str, Any]) -> tuple[float, float, float] | None:
    candidate_count = _number(row.get("candidate_count"))
    search_seconds = _number(row.get("search_duration_seconds"))
    workflow_seconds = _number(_json_object(row.get("timing_json")).get("workflow_duration_seconds"))
    if (
            candidate_count is None
            or candidate_count <= 0
            or search_seconds is None
            or search_seconds < 0
            or workflow_seconds is None
            or workflow_seconds < 0
    ):
        return None
    return candidate_count, search_seconds, workflow_seconds


def _passive_benchmark_runs(rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for row in deduplicate_quality_terminal_rows(rows):
        shadow = _json_object(row.get("shadow_json"))
        if (
                shadow.get("algorithm_version") != QUALITY_SHADOW_ALGORITHM_VERSION
                or shadow.get("production_search_changed") is True
                or not object_dict(shadow.get("recommendation"))
        ):
            continue
        comparison = object_dict(shadow.get("comparison"))
        if comparison.get("evaluation_exclusion_reason"):
            continue
        candidate_count = _number(comparison.get("candidate_count"))
        search_seconds = _number(comparison.get("search_duration_seconds"))
        if candidate_count is not None and candidate_count > 0 and search_seconds is not None and search_seconds >= 0:
            count += 1
    return count


def _is_current_shadow_row(row: Mapping[str, Any]) -> bool:
    return _json_object(row.get("shadow_json")).get("algorithm_version") == QUALITY_SHADOW_ALGORITHM_VERSION


def _median_or_none(values: list[float]) -> float | None:
    return round(float(median(values)), 6) if values else None


def _savings_rate(observed: float | None, baseline: float | None) -> float | None:
    if observed is None or baseline is None or baseline <= 0:
        return None
    return round((baseline - observed) / baseline, 6)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator > 0 else 0.0


def _natural_cluster_id(
        source_rel_path: str,
        *,
        library_types: Mapping[str, str] | None,
) -> str:
    scope = media_group_scope_for_rel_path(source_rel_path, library_types=library_types)
    if scope is None:
        return source_rel_path
    return f"{scope.kind}:{scope.prefix}"


def _operator_outcomes(
        rows: Iterable[Mapping[str, Any]],
) -> QualityAcceptanceOperatorOutcomes:
    current_rows = _current_boundary_rows(rows)
    rejection_count = sum(
        str(row.get("verdict") or "").casefold() in {"unacceptable", "rejected"}
        for row in current_rows
    )
    additional_attention_count = sum(_requires_additional_attention(row) for row in current_rows)
    denominator = len(current_rows)
    return QualityAcceptanceOperatorOutcomes(
        current_observations=denominator,
        rejection_count=rejection_count,
        rejection_rate=_ratio(rejection_count, denominator),
        additional_attention_count=additional_attention_count,
        additional_attention_rate=_ratio(additional_attention_count, denominator),
    )


def _current_boundary_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    current: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        identity = str(row.get("series_id") or row.get("observation_id") or f"row-{index}")
        previous = current.get(identity)
        if previous is None or int(row.get("revision") or 0) >= int(previous.get("revision") or 0):
            current[identity] = row
    return list(current.values())


def _requires_additional_attention(row: Mapping[str, Any]) -> bool:
    if str(row.get("verdict") or "").casefold() in {"unacceptable", "rejected"}:
        return True
    if str(row.get("disposition") or "").casefold() == "withdrawn":
        return True
    if int(row.get("revision") or 0) > 0:
        return True
    assessment = _json_object(row.get("assessment_json"))
    concern_tags = assessment.get("concern_tags")
    return isinstance(concern_tags, list) and bool(concern_tags)


def _prefix_includes(prefix: str | None, rel_path: str) -> bool:
    normalized = normalize_scope_prefix(prefix or "")
    if not normalized:
        return True
    return rel_path == normalized or rel_path.startswith(f"{normalized}/")


def _scope_matches_prefix(scope: MediaScope, prefix: str | None) -> bool:
    if prefix is None:
        return True
    return (
        scope.includes(prefix)
        or scope.prefix == prefix
        or scope.prefix.startswith(f"{prefix}/")
    )


def _resolve_current_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    authority_rank = {"staged_backfill": 10, "runtime_native": 20, "correction": 30}
    current: dict[str, tuple[tuple[int, int], Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        identity = str(row.get("search_run_id") or row.get("observation_id") or f"row-{index}")
        rank = (
            authority_rank.get(str(row.get("authority") or ""), 0),
            int(row.get("revision") or 0),
        )
        existing = current.get(identity)
        if existing is None or rank > existing[0]:
            current[identity] = (rank, row)
    return [row for _, row in current.values()]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"
