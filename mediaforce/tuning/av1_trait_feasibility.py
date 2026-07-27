from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import func, inspect, select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import (
    content_intent_boundary_observations,
    library_item_evidence_state,
    library_items,
)
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationCellPlanV1,
    AV1ColdStartValidationManifestV1,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_trait_projection import (
    AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
    AV1ColdStartTraitProjectionError,
    av1_trait_selector_matches,
    av1_trait_selector_producer_blockers,
    project_av1_cold_start_fingerprint_summary,
)


AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA = "mediaforce.av1_cold_start_trait_feasibility"
AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA_VERSION = 1
AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION = "acstf1"
AV1_COLD_START_TRAIT_FEASIBILITY_MAX_AGE_DAYS = 180


class AV1ColdStartTraitFeasibilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ColdStartTraitFeasibilityCellV1:
    cell_plan_id: str
    name: str
    mode: str
    intent_level: str
    selector_match: str
    required_traits: tuple[str, ...]
    registered_case_count: int
    producer_blockers: tuple[str, ...]
    matching_item_count: int
    distinct_parent_scope_count: int
    eligible_observation_count: int
    independent_source_count: int
    independent_series_count: int
    independent_source_group_count: int
    coherent_cohort_count: int
    coherent_bitrate_min_bps: int | None
    coherent_bitrate_max_bps: int | None
    content_version_mismatch_count: int
    aged_out_observation_count: int
    ineligible_observation_count: int
    withdrawn_observation_count: int
    status: str

    def __post_init__(self) -> None:
        if self.required_traits != tuple(sorted(set(self.required_traits))):
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility traits must be unique and sorted")
        if self.producer_blockers != tuple(sorted(set(self.producer_blockers))):
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility blockers must be unique and sorted")
        counts = (
            self.registered_case_count,
            self.matching_item_count,
            self.distinct_parent_scope_count,
            self.eligible_observation_count,
            self.independent_source_count,
            self.independent_series_count,
            self.independent_source_group_count,
            self.coherent_cohort_count,
            self.content_version_mismatch_count,
            self.aged_out_observation_count,
            self.ineligible_observation_count,
            self.withdrawn_observation_count,
        )
        if any(value < 0 for value in counts):
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility counts cannot be negative")
        if self.status not in {
            "unreachable",
            "insufficient_inventory",
            "insufficient_scope_diversity",
            "insufficient_derivation_evidence",
            "ready_for_private_mapping",
        }:
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility status is unsupported")

    def to_payload(self) -> dict[str, Any]:
        return {
            "cell_plan_id": self.cell_plan_id,
            "name": self.name,
            "mode": self.mode,
            "intent_level": self.intent_level,
            "trait_selector": {
                "match": self.selector_match,
                "traits": list(self.required_traits),
            },
            "registered_case_count": self.registered_case_count,
            "producer_reachable": not self.producer_blockers,
            "producer_blockers": list(self.producer_blockers),
            "inventory": {
                "matching_item_count": self.matching_item_count,
                "distinct_parent_scope_count": self.distinct_parent_scope_count,
            },
            "derivation": {
                "required": self.mode == "publication_candidate",
                "eligible_observation_count": self.eligible_observation_count,
                "independent_source_count": self.independent_source_count,
                "independent_series_count": self.independent_series_count,
                "independent_source_group_count": self.independent_source_group_count,
                "coherent_cohort_count": self.coherent_cohort_count,
                "coherent_bitrate_min_bps": self.coherent_bitrate_min_bps,
                "coherent_bitrate_max_bps": self.coherent_bitrate_max_bps,
                "content_version_mismatch_count": self.content_version_mismatch_count,
                "aged_out_observation_count": self.aged_out_observation_count,
                "ineligible_observation_count": self.ineligible_observation_count,
                "withdrawn_observation_count": self.withdrawn_observation_count,
            },
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class AV1ColdStartTraitFeasibilityReportV1:
    report_id: str
    as_of: str
    manifest_id: str
    measured_analysis_count: int
    incompatible_analysis_count: int
    observation_store_available: bool
    cells: tuple[AV1ColdStartTraitFeasibilityCellV1, ...]

    def __post_init__(self) -> None:
        _parse_as_of(self.as_of)
        if self.measured_analysis_count < 0 or self.incompatible_analysis_count < 0:
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility analysis counts cannot be negative")
        if not self.cells or self.cells != tuple(sorted(self.cells, key=lambda cell: cell.name)):
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility cells must be non-empty and sorted")
        if self.report_id != _report_id(self.semantic_payload()):
            raise AV1ColdStartTraitFeasibilityError("AV1 feasibility report ID does not match its payload")
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    @property
    def ready(self) -> bool:
        return all(cell.status == "ready_for_private_mapping" for cell in self.cells)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA,
            "schema_version": AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA_VERSION,
            "contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
            "projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
            "as_of": self.as_of,
            "manifest_id": self.manifest_id,
            "execution_authorized": False,
            "private_mapping_required": True,
            "runtime_integration_required": True,
            "measured_analysis_count": self.measured_analysis_count,
            "incompatible_analysis_count": self.incompatible_analysis_count,
            "observation_store_available": self.observation_store_available,
            "cells": [cell.to_payload() for cell in self.cells],
        }

    def to_payload(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.semantic_payload(), "ready": self.ready}


def load_av1_trait_feasibility_report(
        connection: DBClient,
        *,
        manifest: AV1ColdStartValidationManifestV1,
        as_of: str,
) -> AV1ColdStartTraitFeasibilityReportV1:
    as_of_time = _parse_as_of(as_of)
    item_rows = list(connection.execute(
        select(
            library_items.c.id,
            library_items.c.media_root,
            library_items.c.parent_dir,
            library_items.c.content_version_fingerprint,
            library_items.c.media_fingerprint_json,
        )
        .join(
            library_item_evidence_state,
            library_item_evidence_state.c.library_item_id == library_items.c.id,
        )
        .where(
            library_item_evidence_state.c.evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND,
            library_item_evidence_state.c.state == "current",
            library_item_evidence_state.c.decision_status == "measured",
        )
    ).mappings())

    projected_items: dict[int, dict[str, Any]] = {}
    incompatible_analysis_count = 0
    for row in item_rows:
        try:
            summary = object_dict(json.loads(str(row.get("media_fingerprint_json") or "{}")))
            projection = project_av1_cold_start_fingerprint_summary(summary)
        except (json.JSONDecodeError, TypeError, AV1ColdStartTraitProjectionError):
            incompatible_analysis_count += 1
            continue
        projected_items[int_value(row.get("id"))] = {
            "traits": projection.cell_traits,
            "parent_scope": stable_json_hash({
                "media_root": str(row.get("media_root") or ""),
                "parent_dir": str(row.get("parent_dir") or ""),
            }),
            "content_version": str(row.get("content_version_fingerprint") or ""),
        }

    observations, observation_store_available = _current_observations(connection, as_of=as_of)
    cells = tuple(sorted((
        _cell_feasibility(
            plan,
            projected_items=projected_items,
            observations=observations,
            as_of=as_of_time,
            minimum_derivation_count=manifest.criteria.minimum_derivation_evidence_count,
            minimum_derivation_sources=manifest.criteria.minimum_derivation_source_count,
        )
        for plan in manifest.cell_plans
    ), key=lambda cell: cell.name))
    semantic_payload = {
        "schema": AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA,
        "schema_version": AV1_COLD_START_TRAIT_FEASIBILITY_SCHEMA_VERSION,
        "contract_version": AV1_COLD_START_TRAIT_FEASIBILITY_CONTRACT_VERSION,
        "projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "as_of": as_of,
        "manifest_id": manifest.manifest_id,
        "execution_authorized": False,
        "private_mapping_required": True,
        "runtime_integration_required": True,
        "measured_analysis_count": len(projected_items),
        "incompatible_analysis_count": incompatible_analysis_count,
        "observation_store_available": observation_store_available,
        "cells": [cell.to_payload() for cell in cells],
    }
    return AV1ColdStartTraitFeasibilityReportV1(
        report_id=_report_id(semantic_payload),
        as_of=as_of,
        manifest_id=manifest.manifest_id,
        measured_analysis_count=len(projected_items),
        incompatible_analysis_count=incompatible_analysis_count,
        observation_store_available=observation_store_available,
        cells=cells,
    )


def load_av1_trait_feasibility_report_from_path(
        connection: DBClient,
        *,
        manifest_path: Path,
        as_of: str,
) -> AV1ColdStartTraitFeasibilityReportV1:
    return load_av1_trait_feasibility_report(
        connection,
        manifest=load_av1_cold_start_validation_manifest(manifest_path),
        as_of=as_of,
    )


def serialize_av1_trait_feasibility_report(
        report: AV1ColdStartTraitFeasibilityReportV1,
) -> bytes:
    return canonical_json_bytes(report.to_payload())


def format_av1_trait_feasibility_report(report: AV1ColdStartTraitFeasibilityReportV1) -> str:
    lines = [
        f"AV1 trait feasibility {report.report_id}",
        f"as_of={report.as_of} ready={str(report.ready).lower()} execution_authorized=false",
        (
            f"measured_analysis={report.measured_analysis_count} "
            f"incompatible_analysis={report.incompatible_analysis_count} "
            f"observation_store_available={str(report.observation_store_available).lower()}"
        ),
    ]
    for cell in report.cells:
        blockers = ",".join(cell.producer_blockers) if cell.producer_blockers else "none"
        lines.append(
            f"{cell.name}: status={cell.status} items={cell.matching_item_count}/"
            f"{cell.registered_case_count} derivation={cell.eligible_observation_count} "
            f"sources={cell.independent_source_count} producer_blockers={blockers}"
        )
    return "\n".join(lines)


def _cell_feasibility(
        plan: AV1ColdStartValidationCellPlanV1,
        *,
        projected_items: Mapping[int, Mapping[str, Any]],
        observations: Sequence[Mapping[str, Any]],
        as_of: datetime,
        minimum_derivation_count: int,
        minimum_derivation_sources: int,
) -> AV1ColdStartTraitFeasibilityCellV1:
    required_traits = tuple(plan.trait_selector.traits)
    producer_blockers = av1_trait_selector_producer_blockers(
        intent_level=plan.intent_level,
        required_traits=required_traits,
        match=plan.trait_selector.match,
        expected_prediction=plan.expected_prediction,
    )
    matching_items = {
        item_id: item
        for item_id, item in projected_items.items()
        if av1_trait_selector_matches(item.get("traits", ()), required_traits, plan.trait_selector.match)
    }
    distinct_parent_scopes = {str(item.get("parent_scope") or "") for item in matching_items.values()}

    fresh_cutoff = as_of - timedelta(days=AV1_COLD_START_TRAIT_FEASIBILITY_MAX_AGE_DAYS)
    coherent_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    content_version_mismatch_count = 0
    aged_out_observation_count = 0
    ineligible_observation_count = 0
    withdrawn_observation_count = 0
    for observation in observations:
        if str(observation.get("intent_level") or "") != plan.intent_level:
            continue
        item_id = int_value(observation.get("library_item_id"))
        item = matching_items.get(item_id)
        if item is None:
            continue
        if str(observation.get("disposition") or "") == "withdrawn":
            withdrawn_observation_count += 1
            continue
        if str(observation.get("content_fingerprint") or "") != str(item.get("content_version") or ""):
            content_version_mismatch_count += 1
            continue
        recorded_at = _parse_timestamp(
            str(observation.get("recorded_at") or ""),
            "observation timestamp",
            canonical=False,
        )
        if recorded_at < fresh_cutoff:
            aged_out_observation_count += 1
            continue
        if not bool(observation.get("personalization_eligible")):
            ineligible_observation_count += 1
            continue
        cohort_key = (
            str(observation.get("intent_semantic_id") or ""),
            str(observation.get("compatibility_key") or ""),
            str(observation.get("policy_hash") or ""),
        )
        coherent_groups[cohort_key].append(observation)

    best_group = max(
        coherent_groups.values(),
        key=_cohort_rank,
        default=[],
    )
    source_count = len({str(row.get("source_id") or "") for row in best_group})
    series_count = len({str(row.get("series_id") or "") for row in best_group})
    source_group_count = len({str(row.get("boundary_group_id") or "") for row in best_group})
    bitrates = [int_value(row.get("boundary_bitrate_bps")) for row in best_group if int_value(row.get("boundary_bitrate_bps")) > 0]

    if producer_blockers:
        status = "unreachable"
    elif len(matching_items) < plan.registered_case_count:
        status = "insufficient_inventory"
    elif len(distinct_parent_scopes) < plan.registered_case_count:
        status = "insufficient_scope_diversity"
    elif (
            plan.mode == "publication_candidate"
            and (len(best_group) < minimum_derivation_count or source_count < minimum_derivation_sources)
    ):
        status = "insufficient_derivation_evidence"
    else:
        status = "ready_for_private_mapping"

    return AV1ColdStartTraitFeasibilityCellV1(
        cell_plan_id=plan.cell_plan_id,
        name=plan.name,
        mode=plan.mode,
        intent_level=plan.intent_level,
        selector_match=plan.trait_selector.match,
        required_traits=required_traits,
        registered_case_count=plan.registered_case_count,
        producer_blockers=producer_blockers,
        matching_item_count=len(matching_items),
        distinct_parent_scope_count=len(distinct_parent_scopes),
        eligible_observation_count=len(best_group),
        independent_source_count=source_count,
        independent_series_count=series_count,
        independent_source_group_count=source_group_count,
        coherent_cohort_count=len(coherent_groups),
        coherent_bitrate_min_bps=min(bitrates) if bitrates and source_count >= minimum_derivation_sources else None,
        coherent_bitrate_max_bps=max(bitrates) if bitrates and source_count >= minimum_derivation_sources else None,
        content_version_mismatch_count=content_version_mismatch_count,
        aged_out_observation_count=aged_out_observation_count,
        ineligible_observation_count=ineligible_observation_count,
        withdrawn_observation_count=withdrawn_observation_count,
        status=status,
    )


def _current_observations(
        connection: DBClient,
        *,
        as_of: str,
) -> tuple[list[Mapping[str, Any]], bool]:
    if not inspect(connection).has_table(content_intent_boundary_observations.name):
        return [], False
    stored_cutoff = _parse_as_of(as_of).isoformat()
    ranked_query = select(
        *content_intent_boundary_observations.c,
        func.row_number().over(
            partition_by=content_intent_boundary_observations.c.series_id,
            order_by=content_intent_boundary_observations.c.revision.desc(),
        ).label("observation_rank"),
    ).where(content_intent_boundary_observations.c.recorded_at <= stored_cutoff)
    ranked = ranked_query.subquery()
    return list(connection.execute(
        select(ranked).where(ranked.c.observation_rank == 1)
    ).mappings()), True


def _cohort_rank(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, int, int, int, int]:
    bitrates = [int_value(row.get("boundary_bitrate_bps")) for row in rows if int_value(row.get("boundary_bitrate_bps")) > 0]
    return (
        len(rows),
        len({str(row.get("source_id") or "") for row in rows}),
        len({str(row.get("series_id") or "") for row in rows}),
        len({str(row.get("boundary_group_id") or "") for row in rows}),
        -(max(bitrates) - min(bitrates)) if bitrates else 0,
        -min(bitrates) if bitrates else 0,
    )


def _parse_as_of(value: str) -> datetime:
    return _parse_timestamp(value, "report timestamp", canonical=True)


def _parse_timestamp(value: str, label: str, *, canonical: bool = False) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AV1ColdStartTraitFeasibilityError(f"Invalid AV1 feasibility {label}") from exc
    if parsed.tzinfo is None:
        raise AV1ColdStartTraitFeasibilityError(f"AV1 feasibility {label} must include UTC")
    parsed = parsed.astimezone(UTC)
    if canonical and parsed.isoformat().replace("+00:00", "Z") != value:
        raise AV1ColdStartTraitFeasibilityError(f"AV1 feasibility {label} must use canonical UTC")
    return parsed


def _report_id(payload: Mapping[str, Any]) -> str:
    return f"av1tf1_{stable_json_hash(payload)[:32]}"
