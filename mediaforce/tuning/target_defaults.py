"""Read-only target proposals from visual boundaries, independent of CRF priors."""

from dataclasses import asdict, dataclass
from fractions import Fraction
from math import ceil, isfinite
from pathlib import PurePosixPath
from statistics import median
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import content_intent_boundary_observations
from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.content_intent_observations import (
    BoundaryCohortScope,
    content_intent_boundary_observation_from_values,
    content_intent_replay_scope_rows,
    load_current_content_intent_boundary_observations,
)


REFERENCE_RUNTIME_SECONDS = 2700
TARGET_DEFAULT_RULE_VERSION = 2
TARGET_DEFAULT_SCOPES: tuple[BoundaryCohortScope, ...] = ("item", "folder", "content_class")
MINIMUM_RUNTIME_RATIO = Fraction(4, 5)
MAXIMUM_RUNTIME_RATIO = Fraction(5, 4)


@dataclass(frozen=True, slots=True)
class TargetDefaultRule:
    minimum_approved_sources: int
    minimum_approved_artifacts: int
    minimum_rejected_sources: int
    minimum_approved_folders: int
    maximum_relative_spread: float


TARGET_DEFAULT_RULES: dict[BoundaryCohortScope, TargetDefaultRule] = {
    "item": TargetDefaultRule(1, 2, 0, 1, 0.25),
    "folder": TargetDefaultRule(3, 3, 0, 1, 0.25),
    "content_class": TargetDefaultRule(8, 8, 3, 3, 0.10),
}


@dataclass(frozen=True, slots=True)
class TargetDefaultScopeReport:
    scope: BoundaryCohortScope
    observation_ids: tuple[str, ...]
    approved_source_count: int
    approved_artifact_count: int
    rejected_source_count: int
    approved_folder_count: int
    excluded_measurement_count: int
    excluded_runtime_count: int
    relative_spread: float | None
    proposed_bytes_per_45_minutes: int | None
    confidence: Literal["none", "limited", "moderate", "high"]
    reason: str
    rule: TargetDefaultRule


@dataclass(frozen=True, slots=True)
class TargetDefaultReport:
    reference_observation_id: str
    reference_target_bytes: int
    reference_duration_seconds: float
    folder_prefix: str
    content_profile_id: str
    intent_semantic_id: str
    compatibility_key: str
    evidence_snapshot_id: str
    scopes: tuple[TargetDefaultScopeReport, ...]
    proposed_scope: BoundaryCohortScope | None
    proposed_bytes_per_45_minutes: int | None
    fallback_reason: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "rule_version": TARGET_DEFAULT_RULE_VERSION,
            "source": "operator_visual_boundaries",
            "mode": "review_only",
            "production_authority": "unverified",
            "reference_runtime_seconds": REFERENCE_RUNTIME_SECONDS,
            "application_requires": ["current_source_and_policy_check", "operator_target_confirmation", "representative_sample"],
            **asdict(self),
        }


def load_target_default_report(
        connection: DBClient,
        *,
        observation_id: str,
) -> TargetDefaultReport:
    reference = connection.execute(
        select(content_intent_boundary_observations).where(
            content_intent_boundary_observations.c.observation_id == observation_id,
        ),
    ).mappings().one_or_none()
    if reference is None:
        raise ValueError("Choose a current eligible visual boundary observation ID")
    observations = load_current_content_intent_boundary_observations(
        connection,
        intent_semantic_id=str(reference["intent_semantic_id"]),
        compatibility_key=str(reference["compatibility_key"]),
    )
    return target_default_report(observations, observation_id=observation_id)


def target_default_report(
        observations: Sequence[Mapping[str, Any]],
        *,
        observation_id: str,
) -> TargetDefaultReport:
    reference_values = next(
        (row for row in observations if row.get("observation_id") == observation_id), None,
    )
    if reference_values is None:
        raise ValueError("Choose a current eligible visual boundary observation ID")
    reference = content_intent_boundary_observation_from_values(reference_values)
    if not _valid_measurement(reference_values):
        raise ValueError("The reference boundary has an invalid size or duration")
    replay_scopes: tuple[BoundaryCohortScope, ...] = ("item", "operator")
    compatible_rows = {
        scope: content_intent_replay_scope_rows(
            observations,
            source_id=reference.source_id,
            content_id=reference.content_id,
            prefix=reference.prefix,
            content_profile_id=reference.content_profile_id,
            intent_semantic_id=reference.intent_semantic_id,
            compatibility_key=reference.compatibility_key,
            scope=scope,
        )
        for scope in replay_scopes
    }
    folder_prefix = str(PurePosixPath(reference.source_rel_path).parent)
    scoped_rows: dict[BoundaryCohortScope, tuple[Mapping[str, Any], ...]] = {
        "item": compatible_rows["item"],
        "folder": tuple(row for row in compatible_rows["operator"] if _source_folder(row) == folder_prefix),
        "content_class": compatible_rows["operator"],
    }
    if not any(row["observation_id"] == observation_id for row in scoped_rows["item"]):
        raise ValueError("Choose a current eligible visual boundary observation ID")

    scope_reports = {
        scope: _scope_report(scope, scoped_rows[scope], reference_duration=reference.duration_seconds)
        for scope in TARGET_DEFAULT_SCOPES
    }
    scopes = tuple(scope_reports[scope] for scope in TARGET_DEFAULT_SCOPES)
    item_conflicts = scope_reports["item"].reason == "conflicting_boundaries"
    item_rejections = [
        _normalized_bytes(row) for row in scoped_rows["item"]
        if row["verdict"] == "unacceptable" and _valid_measurement(row)
    ]
    selected = next((
        report for report in scopes
        if not item_conflicts
        and report.proposed_bytes_per_45_minutes is not None
        and all(report.proposed_bytes_per_45_minutes > rejected for rejected in item_rejections)
    ), None)
    snapshot = {
        "rule_version": TARGET_DEFAULT_RULE_VERSION,
        "rules": {scope: asdict(TARGET_DEFAULT_RULES[scope]) for scope in TARGET_DEFAULT_SCOPES},
        "reference_runtime_seconds": REFERENCE_RUNTIME_SECONDS,
        "runtime_ratio_bounds": [str(MINIMUM_RUNTIME_RATIO), str(MAXIMUM_RUNTIME_RATIO)],
        "production_authority": "unverified",
        "reference_observation_id": observation_id,
        "scopes": {
            scope: sorted(str(row["payload_sha256"]) for row in scoped_rows[scope])
            for scope in TARGET_DEFAULT_SCOPES
        },
    }
    return TargetDefaultReport(
        reference_observation_id=observation_id,
        reference_target_bytes=reference.authoritative_anchor_bytes,
        reference_duration_seconds=reference.duration_seconds,
        folder_prefix=folder_prefix,
        content_profile_id=reference.content_profile_id,
        intent_semantic_id=reference.intent_semantic_id,
        compatibility_key=reference.compatibility_key,
        evidence_snapshot_id=f"tds1_{stable_json_hash(snapshot)[:32]}",
        scopes=scopes,
        proposed_scope=selected.scope if selected else None,
        proposed_bytes_per_45_minutes=selected.proposed_bytes_per_45_minutes if selected else None,
        fallback_reason=None if selected else (
            "item_boundary_conflict" if item_conflicts else "no_supported_default_keep_reference_target"
        ),
    )


def _normalized_bytes(row: Mapping[str, Any]) -> Fraction:
    return Fraction(int(row["boundary_size_bytes"]) * REFERENCE_RUNTIME_SECONDS) / Fraction(str(row["duration_seconds"]))


def _valid_measurement(row: Mapping[str, Any]) -> bool:
    size = row.get("boundary_size_bytes")
    duration = row.get("duration_seconds")
    return (
        isinstance(size, int) and not isinstance(size, bool) and size > 0
        and isinstance(duration, (int, float)) and not isinstance(duration, bool)
        and isfinite(duration) and duration > 0
    )


def _source_folder(row: Mapping[str, Any]) -> str:
    return str(PurePosixPath(str(row["source_rel_path"])).parent)


def _scope_report(
        scope: BoundaryCohortScope,
        rows: Sequence[Mapping[str, Any]],
        *,
        reference_duration: float,
) -> TargetDefaultScopeReport:
    rule = TARGET_DEFAULT_RULES[scope]
    measured = [row for row in rows if _valid_measurement(row)]
    runtime_compatible = [
        row for row in measured
        if scope == "item" or MINIMUM_RUNTIME_RATIO <= (
            Fraction(str(row["duration_seconds"])) / Fraction(str(reference_duration))
        ) <= MAXIMUM_RUNTIME_RATIO
    ]
    excluded_measurement_count = len(rows) - len(measured)
    excluded_runtime_count = len(measured) - len(runtime_compatible)
    rows = runtime_compatible
    approved = [row for row in rows if row["verdict"] == "acceptable" and row["quality_floor_met"]]
    rejected = [row for row in rows if row["verdict"] == "unacceptable"]
    source_bounds: dict[str, Fraction] = {}
    for row in approved:
        source = str(row["source_id"])
        value = _normalized_bytes(row)
        source_bounds[source] = min(source_bounds.get(source, value), value)
    approved_artifacts = {str(row["artifact_fingerprint"]) for row in approved}
    rejected_sources = {str(row["source_id"]) for row in rejected}
    approved_folders = {_source_folder(row) for row in approved}
    bounds = list(source_bounds.values())
    dispersion_bounds = [_normalized_bytes(row) for row in approved] if scope == "item" else bounds
    spread = (
        float((max(dispersion_bounds) - min(dispersion_bounds)) / median(dispersion_bounds))
        if dispersion_bounds else None
    )
    # Total bytes include retained audio and attachments; the video-only CRF
    # posterior is deliberately not an input to a target-intent proposal.
    conflicting = bool(bounds and rejected and max(map(_normalized_bytes, rejected)) >= min(bounds))
    if not rows:
        reason = (
            "invalid_boundary_measurements" if excluded_measurement_count
            else "no_runtime_compatible_boundaries" if excluded_runtime_count
            else "no_compatible_boundaries"
        )
    elif conflicting:
        reason = "conflicting_boundaries"
    elif not bounds:
        reason = "no_quality_safe_visual_approval"
    elif len(source_bounds) < rule.minimum_approved_sources:
        reason = "insufficient_independent_approved_sources"
    elif len(approved_artifacts) < rule.minimum_approved_artifacts:
        reason = "insufficient_independent_approved_artifacts"
    elif len(rejected_sources) < rule.minimum_rejected_sources:
        reason = "insufficient_rejected_sources"
    elif len(approved_folders) < rule.minimum_approved_folders:
        reason = "insufficient_cross_folder_evidence"
    elif spread is None or spread > rule.maximum_relative_spread:
        reason = "unstable_boundary_dispersion"
    else:
        reason = "compatible_repeated_visual_decisions"
    supported = reason == "compatible_repeated_visual_decisions"
    return TargetDefaultScopeReport(
        scope=scope,
        observation_ids=tuple(sorted(str(row["observation_id"]) for row in rows)),
        approved_source_count=len(source_bounds),
        approved_artifact_count=len(approved_artifacts),
        rejected_source_count=len(rejected_sources),
        approved_folder_count=len(approved_folders),
        excluded_measurement_count=excluded_measurement_count,
        excluded_runtime_count=excluded_runtime_count,
        relative_spread=spread,
        proposed_bytes_per_45_minutes=ceil(max(bounds)) if supported else None,
        confidence=("high" if scope == "content_class" else "moderate") if supported else ("limited" if rows else "none"),
        reason=reason,
        rule=rule,
    )
