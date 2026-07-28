from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Literal, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    AV1ValidationV2CellPlan,
    assert_preregistered_av1_validation_manifest_v2,
)


AV1_VALIDATION_PARTITION_SCHEMA = "mediaforce.av1_cold_start_private_source_partition"
AV1_VALIDATION_PARTITION_SCHEMA_VERSION = 1
AV1_VALIDATION_SELECTION_LOCK_SCHEMA = "mediaforce.av1_cold_start_source_selection_lock"
AV1_VALIDATION_SELECTION_LOCK_SCHEMA_VERSION = 1
AV1_VALIDATION_PARTITION_SUMMARY_SCHEMA = (
    "mediaforce.av1_cold_start_source_partition_summary"
)
AV1_VALIDATION_PARTITION_CONTRACT_VERSION = "av1vsp1"
AV1_VALIDATION_PARTITION_TOKEN_VERSION = "av1vtok1"
AV1_VALIDATION_PARTITION_ALGORITHM = "hmac_rank_backtracking_v1"
AV1_VALIDATION_DERIVATION_RESERVATION_COUNT = 12
AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT = 6
AV1_VALIDATION_MAXIMUM_SOURCE_GROUP_FRACTION = 1 / 3

AV1ValidationPartitionRole = Literal["holdout", "derivation"]

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}\Z")
_QUALITY_METRIC_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{1,31}\Z")
_MAXIMUM_SEARCH_NODES = 1_000_000


class AV1ValidationPartitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationPartitionExpectations:
    compatibility_signature: str
    base_policy_signature: str
    quality_metric: str
    quality_target: float
    minimum_quality_score: float

    def __post_init__(self) -> None:
        for value, label in (
            (self.compatibility_signature, "compatibility signature"),
            (self.base_policy_signature, "base policy signature"),
        ):
            if not _TOKEN_RE.fullmatch(value):
                raise AV1ValidationPartitionError(f"AV1 partition {label} is invalid")
        if not _QUALITY_METRIC_RE.fullmatch(self.quality_metric):
            raise AV1ValidationPartitionError("AV1 partition quality metric is invalid")
        if (
            not math.isfinite(self.quality_target)
            or self.quality_target < 0
            or not math.isfinite(self.minimum_quality_score)
            or self.minimum_quality_score <= 0
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition quality expectations are invalid"
            )
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationPartitionError(
                "AV1 partition quality floor exceeds its target"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "compatibility_signature": self.compatibility_signature,
            "base_policy_signature": self.base_policy_signature,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationPartitionSource:
    local_item_id: int
    source_identity: str
    title_identity: str
    series_identity: str
    source_group_identity: str
    traits: tuple[str, ...]
    compatibility_signature: str
    base_policy_signature: str
    target_video_bitrate_bps: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    evidence_summary_sha256: str

    def __post_init__(self) -> None:
        if self.local_item_id <= 0:
            raise AV1ValidationPartitionError("AV1 partition source item ID is invalid")
        if any(
            not value
            for value in (
                self.source_identity,
                self.title_identity,
                self.series_identity,
                self.source_group_identity,
            )
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition source identity is incomplete"
            )
        if self.traits != tuple(sorted(set(self.traits))) or not self.traits:
            raise AV1ValidationPartitionError("AV1 partition source traits are invalid")
        for value, label in (
            (self.compatibility_signature, "compatibility signature"),
            (self.base_policy_signature, "base policy signature"),
        ):
            if not _TOKEN_RE.fullmatch(value):
                raise AV1ValidationPartitionError(
                    f"AV1 partition source {label} is invalid"
                )
        if not _QUALITY_METRIC_RE.fullmatch(self.quality_metric):
            raise AV1ValidationPartitionError(
                "AV1 partition source quality metric is invalid"
            )
        if self.target_video_bitrate_bps <= 0:
            raise AV1ValidationPartitionError(
                "AV1 partition source target bitrate is invalid"
            )
        if not all(
            math.isfinite(value) and value >= 0
            for value in (
                self.quality_target,
                self.minimum_quality_score,
            )
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition source quality contract is invalid"
            )
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationPartitionError(
                "AV1 partition source quality floor exceeds its target"
            )
        _require_sha256(self.evidence_summary_sha256, "source evidence digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "local_item_id": self.local_item_id,
            "source_identity": self.source_identity,
            "title_identity": self.title_identity,
            "series_identity": self.series_identity,
            "source_group_identity": self.source_group_identity,
            "traits": list(self.traits),
            "compatibility_signature": self.compatibility_signature,
            "base_policy_signature": self.base_policy_signature,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "evidence_summary_sha256": self.evidence_summary_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationPartitionAssignment:
    assignment_id: str
    role: AV1ValidationPartitionRole
    cell_plan_id: str
    ordinal: int
    local_item_id: int
    traits: tuple[str, ...]
    intent_level: str
    source_token: str
    title_token: str
    series_token: str
    source_group_token: str
    compatibility_signature: str
    policy_signature: str
    target_video_bitrate_bps: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    evidence_summary_sha256: str

    def __post_init__(self) -> None:
        if self.role not in {"holdout", "derivation"}:
            raise AV1ValidationPartitionError(
                "AV1 partition assignment role is invalid"
            )
        if self.ordinal <= 0 or self.local_item_id <= 0:
            raise AV1ValidationPartitionError(
                "AV1 partition assignment ordinal or item ID is invalid"
            )
        if self.traits != tuple(sorted(set(self.traits))) or not self.traits:
            raise AV1ValidationPartitionError(
                "AV1 partition assignment traits are invalid"
            )
        for value, label in (
            (self.assignment_id, "assignment ID"),
            (self.cell_plan_id, "cell-plan ID"),
            (self.intent_level, "intent"),
            (self.source_token, "source token"),
            (self.title_token, "title token"),
            (self.series_token, "series token"),
            (self.source_group_token, "source-group token"),
            (self.compatibility_signature, "compatibility signature"),
            (self.policy_signature, "policy signature"),
        ):
            if not _TOKEN_RE.fullmatch(value):
                raise AV1ValidationPartitionError(
                    f"AV1 partition assignment {label} is invalid"
                )
        if not _QUALITY_METRIC_RE.fullmatch(self.quality_metric):
            raise AV1ValidationPartitionError(
                "AV1 partition assignment quality metric is invalid"
            )
        if self.target_video_bitrate_bps <= 0:
            raise AV1ValidationPartitionError(
                "AV1 partition assignment target bitrate is invalid"
            )
        if not all(
            math.isfinite(value) and value >= 0
            for value in (
                self.quality_target,
                self.minimum_quality_score,
            )
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition assignment quality contract is invalid"
            )
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationPartitionError(
                "AV1 partition assignment quality floor exceeds target"
            )
        _require_sha256(self.evidence_summary_sha256, "assignment evidence digest")

    def token_payload(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "role": self.role,
            "cell_plan_id": self.cell_plan_id,
            "ordinal": self.ordinal,
            "traits": list(self.traits),
            "intent_level": self.intent_level,
            "source_token": self.source_token,
            "title_token": self.title_token,
            "series_token": self.series_token,
            "source_group_token": self.source_group_token,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "target_video_bitrate_bps": self.target_video_bitrate_bps,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.token_payload(),
            "local_item_id": self.local_item_id,
            "evidence_summary_sha256": self.evidence_summary_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationPartitionPlanBinding:
    cell_plan_id: str
    intent_level: str
    compatibility_signature: str
    policy_signature: str
    target_video_bitrate_min_bps: int
    target_video_bitrate_max_bps: int
    quality_metric: str
    quality_target: float
    minimum_quality_score: float
    holdout_count: int
    derivation_reservation_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.cell_plan_id, "cell-plan ID"),
            (self.intent_level, "intent"),
            (self.compatibility_signature, "compatibility signature"),
            (self.policy_signature, "policy signature"),
        ):
            if not _TOKEN_RE.fullmatch(value):
                raise AV1ValidationPartitionError(
                    f"AV1 partition plan {label} is invalid"
                )
        if not _QUALITY_METRIC_RE.fullmatch(self.quality_metric):
            raise AV1ValidationPartitionError(
                "AV1 partition plan quality metric is invalid"
            )
        if (
            not 0
            < self.target_video_bitrate_min_bps
            <= self.target_video_bitrate_max_bps
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition plan bitrate range is invalid"
            )
        if self.holdout_count <= 0 or self.derivation_reservation_count < 0:
            raise AV1ValidationPartitionError("AV1 partition plan counts are invalid")
        if not all(
            math.isfinite(value) and value >= 0
            for value in (
                self.quality_target,
                self.minimum_quality_score,
            )
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition plan quality contract is invalid"
            )
        if self.minimum_quality_score > self.quality_target:
            raise AV1ValidationPartitionError(
                "AV1 partition plan quality floor exceeds target"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "cell_plan_id": self.cell_plan_id,
            "intent_level": self.intent_level,
            "compatibility_signature": self.compatibility_signature,
            "policy_signature": self.policy_signature,
            "target_video_bitrate_min_bps": self.target_video_bitrate_min_bps,
            "target_video_bitrate_max_bps": self.target_video_bitrate_max_bps,
            "quality_metric": self.quality_metric,
            "quality_target": self.quality_target,
            "minimum_quality_score": self.minimum_quality_score,
            "holdout_count": self.holdout_count,
            "derivation_reservation_count": self.derivation_reservation_count,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationPrivatePartition:
    partition_id: str
    manifest_id: str
    manifest_payload_sha256: str
    eligibility_attestation_id: str
    eligibility_payload_sha256: str
    selected_at: str
    token_key_id: str
    inventory_snapshot_sha256: str
    expectations: AV1ValidationPartitionExpectations
    inventory_sources: tuple[AV1ValidationPartitionSource, ...]
    plan_bindings: tuple[AV1ValidationPartitionPlanBinding, ...]
    assignments: tuple[AV1ValidationPartitionAssignment, ...]
    derivation_partition_sha256: str
    selection_lock_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.partition_id.startswith("av1vpartition1_"):
            raise AV1ValidationPartitionError("AV1 partition ID is invalid")
        if not self.manifest_id.startswith("av1vmanifest2_"):
            raise AV1ValidationPartitionError(
                "AV1 partition manifest reference is invalid"
            )
        if not self.eligibility_attestation_id.startswith("av1vfeasibility2_"):
            raise AV1ValidationPartitionError(
                "AV1 partition eligibility reference is invalid"
            )
        if not self.token_key_id.startswith("av1vkey1_"):
            raise AV1ValidationPartitionError("AV1 partition token-key ID is invalid")
        for value, label in (
            (self.manifest_payload_sha256, "manifest digest"),
            (self.eligibility_payload_sha256, "eligibility digest"),
            (self.inventory_snapshot_sha256, "inventory snapshot digest"),
            (self.derivation_partition_sha256, "derivation partition digest"),
            (self.selection_lock_sha256, "selection lock digest"),
            (self.payload_sha256, "payload digest"),
        ):
            _require_sha256(value, label)
        _parse_timestamp(self.selected_at, "selection timestamp")
        if self.plan_bindings != tuple(
            sorted(self.plan_bindings, key=lambda item: item.cell_plan_id)
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition plan bindings are not canonical"
            )
        if self.inventory_sources != tuple(
            sorted(self.inventory_sources, key=lambda source: source.local_item_id)
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition inventory snapshot is not canonical"
            )
        _validate_source_set(self.inventory_sources)
        if self.assignments != tuple(
            sorted(self.assignments, key=_assignment_sort_key)
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition assignments are not canonical"
            )
        if self.derivation_partition_sha256 != _derivation_partition_sha256(
            manifest_id=self.manifest_id,
            assignments=self.assignments,
        ):
            raise AV1ValidationPartitionError(
                "AV1 derivation partition digest does not match assignments"
            )
        if self.selection_lock_sha256 != _payload_sha256(self.selection_lock_payload()):
            raise AV1ValidationPartitionError(
                "AV1 selection lock digest does not match its payload"
            )
        semantic_payload = self.semantic_payload()
        if self.partition_id != _partition_id("partition", semantic_payload):
            raise AV1ValidationPartitionError(
                "AV1 partition ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {
                "partition_id": self.partition_id,
                **semantic_payload,
            }
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition payload digest does not match its payload"
            )

    @property
    def holdout_count(self) -> int:
        return sum(assignment.role == "holdout" for assignment in self.assignments)

    @property
    def derivation_reservation_count(self) -> int:
        return sum(assignment.role == "derivation" for assignment in self.assignments)

    def selection_lock_payload(self) -> dict[str, Any]:
        return _selection_lock_payload(
            manifest_id=self.manifest_id,
            manifest_payload_sha256=self.manifest_payload_sha256,
            eligibility_attestation_id=self.eligibility_attestation_id,
            eligibility_payload_sha256=self.eligibility_payload_sha256,
            selected_at=self.selected_at,
            token_key_id=self.token_key_id,
            inventory_snapshot_sha256=self.inventory_snapshot_sha256,
            expectations=self.expectations,
            plan_bindings=self.plan_bindings,
            assignments=self.assignments,
            derivation_partition_sha256=self.derivation_partition_sha256,
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_PARTITION_SCHEMA,
            "schema_version": AV1_VALIDATION_PARTITION_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
            "selection_lock": self.selection_lock_payload(),
            "inventory_sources": [
                source.to_payload() for source in self.inventory_sources
            ],
            "assignments": [assignment.to_payload() for assignment in self.assignments],
            "selection_lock_sha256": self.selection_lock_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class _PartitionSlot:
    assignment_id: str
    role: AV1ValidationPartitionRole
    plan: AV1ValidationV2CellPlan
    ordinal: int


def build_av1_validation_private_partition(
    *,
    manifest: AV1ValidationManifestV2,
    eligibility_attestation_id: str,
    eligibility_payload_sha256: str,
    sources: Sequence[AV1ValidationPartitionSource],
    expectations: AV1ValidationPartitionExpectations,
    token_key: bytes,
    expected_token_key_id: str,
    selected_at: str,
) -> AV1ValidationPrivatePartition:
    assert_preregistered_av1_validation_manifest_v2(manifest)
    _assert_partition_criteria(manifest)
    selection_timestamp = _parse_timestamp(selected_at, "selection timestamp")
    manifest_registered_at = _parse_timestamp(
        manifest.registered_at,
        "manifest registration",
    )
    manifest_valid_until = _parse_timestamp(
        manifest.valid_until,
        "manifest expiration",
    )
    if selection_timestamp < manifest_registered_at:
        raise AV1ValidationPartitionError(
            "AV1 partition selection timestamp predates manifest registration"
        )
    if selection_timestamp >= manifest_valid_until:
        raise AV1ValidationPartitionError(
            "AV1 partition selection timestamp must precede manifest expiration"
        )
    _validate_key(token_key)
    token_key_id = _token_key_id(token_key)
    if expected_token_key_id != token_key_id:
        raise AV1ValidationPartitionError(
            "AV1 partition key does not match its precommitted token-key ID"
        )
    if eligibility_attestation_id != manifest.eligibility_attestation_id:
        raise AV1ValidationPartitionError(
            "AV1 partition eligibility ID does not match manifest"
        )
    if eligibility_payload_sha256 != manifest.eligibility_payload_sha256:
        raise AV1ValidationPartitionError(
            "AV1 partition eligibility digest does not match manifest"
        )
    _validate_source_set(sources)

    eligible_sources = tuple(
        source
        for source in sources
        if _source_matches_expectations(
            source,
            expectations=expectations,
        )
    )
    if not eligible_sources:
        raise AV1ValidationPartitionError(
            "AV1 partition has no compatible eligible sources"
        )
    slots = _partition_slots(manifest)
    candidates_by_plan = {
        plan.cell_plan_id: tuple(
            source
            for source in eligible_sources
            if plan.trait_selector.matches(source.traits)
        )
        for plan in manifest.cell_plans
    }
    for plan in manifest.cell_plans:
        required = plan.registered_case_count + (
            AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
            if plan.mode == "publication_candidate"
            else 0
        )
        candidates = candidates_by_plan[plan.cell_plan_id]
        if len({source.series_identity for source in candidates}) < required:
            raise AV1ValidationPartitionError(
                f"AV1 partition cell {plan.cell_plan_id} lacks distinct eligible series"
            )
        if plan.mode == "publication_candidate":
            _validate_candidate_group_capacity(plan, candidates)
    mapped_sources = tuple(
        source
        for source in eligible_sources
        if any(
            plan.trait_selector.matches(source.traits) for plan in manifest.cell_plans
        )
    )
    for attribute in ("source_identity", "title_identity", "series_identity"):
        if len({getattr(source, attribute) for source in mapped_sources}) < len(slots):
            raise AV1ValidationPartitionError(
                f"AV1 partition lacks globally distinct {attribute.replace('_', ' ')} values"
            )

    ordered_holdout_slots = _ordered_partition_slots(
        slots,
        candidates_by_plan=candidates_by_plan,
        role="holdout",
    )
    holdout_selected = _select_sources(
        slots=ordered_holdout_slots,
        candidates_by_plan=candidates_by_plan,
        manifest_id=manifest.manifest_id,
        token_key=token_key,
    )
    ordered_derivation_slots = _ordered_partition_slots(
        slots,
        candidates_by_plan=candidates_by_plan,
        role="derivation",
    )
    fixed_holdouts = tuple(
        (slot, holdout_selected[slot.assignment_id]) for slot in ordered_holdout_slots
    )
    selected = _select_sources(
        slots=ordered_derivation_slots,
        candidates_by_plan=candidates_by_plan,
        manifest_id=manifest.manifest_id,
        token_key=token_key,
        fixed=fixed_holdouts,
    )
    assignments = tuple(
        sorted(
            (
                _assignment_for_source(
                    slot, selected[slot.assignment_id], token_key=token_key
                )
                for slot in slots
            ),
            key=_assignment_sort_key,
        )
    )
    _validate_assignment_contract(manifest, assignments)

    plan_bindings = _plan_bindings(manifest, assignments)
    inventory_sources = tuple(
        sorted(eligible_sources, key=lambda source: source.local_item_id)
    )
    inventory_snapshot_sha256 = _inventory_snapshot_sha256(
        manifest=manifest,
        sources=inventory_sources,
        token_key=token_key,
    )
    derivation_partition_sha256 = _derivation_partition_sha256(
        manifest_id=manifest.manifest_id,
        assignments=assignments,
    )
    selection_lock_payload = _selection_lock_payload(
        manifest_id=manifest.manifest_id,
        manifest_payload_sha256=manifest.payload_sha256,
        eligibility_attestation_id=eligibility_attestation_id,
        eligibility_payload_sha256=eligibility_payload_sha256,
        selected_at=selected_at,
        token_key_id=token_key_id,
        inventory_snapshot_sha256=inventory_snapshot_sha256,
        expectations=expectations,
        plan_bindings=plan_bindings,
        assignments=assignments,
        derivation_partition_sha256=derivation_partition_sha256,
    )
    selection_lock_sha256 = _payload_sha256(selection_lock_payload)
    semantic_payload = {
        "schema": AV1_VALIDATION_PARTITION_SCHEMA,
        "schema_version": AV1_VALIDATION_PARTITION_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
        "selection_lock": selection_lock_payload,
        "inventory_sources": [source.to_payload() for source in inventory_sources],
        "assignments": [assignment.to_payload() for assignment in assignments],
        "selection_lock_sha256": selection_lock_sha256,
    }
    partition_id = _partition_id("partition", semantic_payload)
    return AV1ValidationPrivatePartition(
        partition_id=partition_id,
        manifest_id=manifest.manifest_id,
        manifest_payload_sha256=manifest.payload_sha256,
        eligibility_attestation_id=eligibility_attestation_id,
        eligibility_payload_sha256=eligibility_payload_sha256,
        selected_at=selected_at,
        token_key_id=token_key_id,
        inventory_snapshot_sha256=inventory_snapshot_sha256,
        expectations=expectations,
        inventory_sources=inventory_sources,
        plan_bindings=plan_bindings,
        assignments=assignments,
        derivation_partition_sha256=derivation_partition_sha256,
        selection_lock_sha256=selection_lock_sha256,
        payload_sha256=_payload_sha256(
            {"partition_id": partition_id, **semantic_payload}
        ),
    )


def validate_av1_validation_private_partition(
    partition: AV1ValidationPrivatePartition,
    *,
    manifest: AV1ValidationManifestV2,
    token_key: bytes,
) -> None:
    rebuilt = build_av1_validation_private_partition(
        manifest=manifest,
        eligibility_attestation_id=partition.eligibility_attestation_id,
        eligibility_payload_sha256=partition.eligibility_payload_sha256,
        sources=partition.inventory_sources,
        expectations=partition.expectations,
        token_key=token_key,
        expected_token_key_id=partition.token_key_id,
        selected_at=partition.selected_at,
    )
    if rebuilt != partition:
        raise AV1ValidationPartitionError(
            "AV1 private partition does not match current locked inputs"
        )


def validate_av1_validation_partition_current_inputs(
    partition: AV1ValidationPrivatePartition,
    *,
    manifest: AV1ValidationManifestV2,
    sources: Sequence[AV1ValidationPartitionSource],
    expectations: AV1ValidationPartitionExpectations,
    token_key: bytes,
) -> None:
    rebuilt = build_av1_validation_private_partition(
        manifest=manifest,
        eligibility_attestation_id=partition.eligibility_attestation_id,
        eligibility_payload_sha256=partition.eligibility_payload_sha256,
        sources=sources,
        expectations=expectations,
        token_key=token_key,
        expected_token_key_id=partition.token_key_id,
        selected_at=partition.selected_at,
    )
    if rebuilt != partition:
        raise AV1ValidationPartitionError(
            "AV1 private partition does not match current inventory or policy inputs"
        )


def serialize_av1_validation_private_partition(
    partition: AV1ValidationPrivatePartition,
) -> bytes:
    return canonical_json_bytes(partition.to_payload())


def load_av1_validation_private_partition(path: Path) -> AV1ValidationPrivatePartition:
    raw = _read_owner_only_regular_file(path, label="private partition")
    try:
        payload = object_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AV1ValidationPartitionError(
            "AV1 private partition is not valid JSON"
        ) from exc
    partition = av1_validation_private_partition_from_payload(payload)
    if raw != serialize_av1_validation_private_partition(partition):
        raise AV1ValidationPartitionError("AV1 private partition JSON is not canonical")
    return partition


def av1_validation_private_partition_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationPrivatePartition:
    data = object_dict(payload)
    _require_exact_keys(
        data,
        {
            "partition_id",
            "schema",
            "schema_version",
            "contract_version",
            "selection_lock",
            "inventory_sources",
            "assignments",
            "selection_lock_sha256",
            "payload_sha256",
        },
        "private partition",
    )
    if data.get("schema") != AV1_VALIDATION_PARTITION_SCHEMA:
        raise AV1ValidationPartitionError("AV1 private partition schema is invalid")
    if int_value(data.get("schema_version")) != AV1_VALIDATION_PARTITION_SCHEMA_VERSION:
        raise AV1ValidationPartitionError(
            "AV1 private partition schema version is invalid"
        )
    if data.get("contract_version") != AV1_VALIDATION_PARTITION_CONTRACT_VERSION:
        raise AV1ValidationPartitionError(
            "AV1 private partition contract version is invalid"
        )
    selection_lock = object_dict(data.get("selection_lock"))
    _require_exact_keys(
        selection_lock,
        {
            "schema",
            "schema_version",
            "contract_version",
            "authority",
            "runtime_execution_authorized",
            "derivation_execution_authorized",
            "holdout_execution_authorized",
            "manifest_id",
            "manifest_payload_sha256",
            "eligibility_attestation_id",
            "eligibility_payload_sha256",
            "selected_at",
            "assignment_algorithm",
            "token_version",
            "token_key_id",
            "identity_policy",
            "inventory_snapshot_sha256",
            "expectations",
            "plan_bindings",
            "assignments",
            "derivation_partition_sha256",
        },
        "selection lock",
    )
    if selection_lock.get("schema") != AV1_VALIDATION_SELECTION_LOCK_SCHEMA:
        raise AV1ValidationPartitionError("AV1 selection lock schema is invalid")
    if (
        int_value(selection_lock.get("schema_version"))
        != AV1_VALIDATION_SELECTION_LOCK_SCHEMA_VERSION
    ):
        raise AV1ValidationPartitionError(
            "AV1 selection lock schema version is invalid"
        )
    if (
        selection_lock.get("contract_version")
        != AV1_VALIDATION_PARTITION_CONTRACT_VERSION
    ):
        raise AV1ValidationPartitionError(
            "AV1 selection lock contract version is invalid"
        )
    if (
        selection_lock.get("assignment_algorithm") != AV1_VALIDATION_PARTITION_ALGORITHM
        or selection_lock.get("token_version") != AV1_VALIDATION_PARTITION_TOKEN_VERSION
    ):
        raise AV1ValidationPartitionError(
            "AV1 selection lock algorithm or token version is invalid"
        )
    if (
        selection_lock.get("authority") != "selection_lock_only"
        or selection_lock.get("runtime_execution_authorized") is not False
        or selection_lock.get("derivation_execution_authorized") is not False
        or selection_lock.get("holdout_execution_authorized") is not False
    ):
        raise AV1ValidationPartitionError(
            "AV1 selection lock carries unauthorized execution authority"
        )
    identity_policy = object_dict(selection_lock.get("identity_policy"))
    if identity_policy != {
        "global_source_unique": True,
        "global_title_unique": True,
        "global_series_unique": True,
        "derivation_holdout_disjoint": ["source", "series", "source_group"],
        "candidate_source_group_minimum": AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT,
        "candidate_source_group_maximum_numerator": 1,
        "candidate_source_group_maximum_denominator": 3,
    }:
        raise AV1ValidationPartitionError(
            "AV1 selection lock identity policy is invalid"
        )
    expectations = _expectations_from_payload(
        object_dict(selection_lock.get("expectations"))
    )
    plan_bindings = tuple(
        _plan_binding_from_payload(object_dict(item))
        for item in object_list(selection_lock.get("plan_bindings"))
    )
    private_assignments = tuple(
        _assignment_from_payload(object_dict(item))
        for item in object_list(data.get("assignments"))
    )
    inventory_sources = tuple(
        _source_from_payload(object_dict(item))
        for item in object_list(data.get("inventory_sources"))
    )
    if [
        assignment.token_payload() for assignment in private_assignments
    ] != object_list(selection_lock.get("assignments")):
        raise AV1ValidationPartitionError(
            "AV1 private assignments do not match the selection lock"
        )
    return AV1ValidationPrivatePartition(
        partition_id=_required_text(data.get("partition_id"), "partition ID"),
        manifest_id=_required_text(selection_lock.get("manifest_id"), "manifest ID"),
        manifest_payload_sha256=_required_text(
            selection_lock.get("manifest_payload_sha256"),
            "manifest digest",
        ),
        eligibility_attestation_id=_required_text(
            selection_lock.get("eligibility_attestation_id"),
            "eligibility ID",
        ),
        eligibility_payload_sha256=_required_text(
            selection_lock.get("eligibility_payload_sha256"),
            "eligibility digest",
        ),
        selected_at=_required_text(
            selection_lock.get("selected_at"), "selection timestamp"
        ),
        token_key_id=_required_text(selection_lock.get("token_key_id"), "token-key ID"),
        inventory_snapshot_sha256=_required_text(
            selection_lock.get("inventory_snapshot_sha256"),
            "inventory snapshot digest",
        ),
        expectations=expectations,
        inventory_sources=inventory_sources,
        plan_bindings=plan_bindings,
        assignments=private_assignments,
        derivation_partition_sha256=_required_text(
            selection_lock.get("derivation_partition_sha256"),
            "derivation partition digest",
        ),
        selection_lock_sha256=_required_text(
            data.get("selection_lock_sha256"), "selection lock digest"
        ),
        payload_sha256=_required_text(data.get("payload_sha256"), "payload digest"),
    )


def create_av1_validation_partition_key(path: Path) -> str:
    if path.exists():
        raise AV1ValidationPartitionError("AV1 partition key already exists")
    key = secrets.token_bytes(32)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_owner_only_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return _token_key_id(key)


def load_av1_validation_partition_key(path: Path) -> bytes:
    key = _read_owner_only_regular_file(path, label="partition key")
    _validate_key(key)
    return key


def av1_validation_partition_key_id(token_key: bytes) -> str:
    _validate_key(token_key)
    return _token_key_id(token_key)


def write_av1_validation_private_partition(
    path: Path,
    partition: AV1ValidationPrivatePartition,
) -> None:
    if path.exists():
        raise AV1ValidationPartitionError("AV1 private partition already exists")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_owner_only_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialize_av1_validation_private_partition(partition))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def av1_validation_partition_public_summary(
    partition: AV1ValidationPrivatePartition,
) -> dict[str, Any]:
    payload = {
        "schema": AV1_VALIDATION_PARTITION_SUMMARY_SCHEMA,
        "schema_version": 1,
        "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
        "manifest_id": partition.manifest_id,
        "selection_lock_sha256": partition.selection_lock_sha256,
        "derivation_partition_sha256": partition.derivation_partition_sha256,
        "holdout_count": partition.holdout_count,
        "derivation_reservation_count": partition.derivation_reservation_count,
        "runtime_execution_authorized": False,
        "derivation_execution_authorized": False,
        "holdout_execution_authorized": False,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    return payload


def assert_private_artifact_path(path: Path, *, repository_root: Path) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_root = repository_root.expanduser().resolve()
    if resolved_path == resolved_root or resolved_root in resolved_path.parents:
        raise AV1ValidationPartitionError(
            "AV1 private artifacts must remain outside the repository"
        )


def _partition_slots(manifest: AV1ValidationManifestV2) -> tuple[_PartitionSlot, ...]:
    plans = {plan.cell_plan_id: plan for plan in manifest.cell_plans}
    slots = [
        _PartitionSlot(
            assignment_id=case.case_id,
            role="holdout",
            plan=plans[case.cell_plan_id],
            ordinal=case.ordinal,
        )
        for case in manifest.cases
    ]
    for plan in manifest.cell_plans:
        if plan.mode != "publication_candidate":
            continue
        slots.extend(
            _PartitionSlot(
                assignment_id=_partition_id(
                    "derivation_slot",
                    {
                        "manifest_id": manifest.manifest_id,
                        "cell_plan_id": plan.cell_plan_id,
                        "ordinal": ordinal,
                    },
                ),
                role="derivation",
                plan=plan,
                ordinal=ordinal,
            )
            for ordinal in range(1, AV1_VALIDATION_DERIVATION_RESERVATION_COUNT + 1)
        )
    return tuple(slots)


def _ordered_partition_slots(
    slots: Sequence[_PartitionSlot],
    *,
    candidates_by_plan: Mapping[str, Sequence[AV1ValidationPartitionSource]],
    role: AV1ValidationPartitionRole,
) -> tuple[_PartitionSlot, ...]:
    return tuple(
        sorted(
            (slot for slot in slots if slot.role == role),
            key=lambda slot: (
                len(
                    {
                        source.series_identity
                        for source in candidates_by_plan[slot.plan.cell_plan_id]
                    }
                ),
                slot.plan.name,
                slot.ordinal,
                slot.assignment_id,
            ),
        )
    )


def _select_sources(
    *,
    slots: Sequence[_PartitionSlot],
    candidates_by_plan: Mapping[str, Sequence[AV1ValidationPartitionSource]],
    manifest_id: str,
    token_key: bytes,
    fixed: Sequence[tuple[_PartitionSlot, AV1ValidationPartitionSource]] = (),
) -> dict[str, AV1ValidationPartitionSource]:
    ranked_candidates = {
        slot.assignment_id: tuple(
            sorted(
                candidates_by_plan[slot.plan.cell_plan_id],
                key=lambda source: (
                    _rank_source(
                        source,
                        manifest_id=manifest_id,
                        assignment_id=slot.assignment_id,
                        token_key=token_key,
                    ),
                    source.local_item_id,
                ),
            )
        )
        for slot in slots
    }
    selected = {slot.assignment_id: source for slot, source in fixed}
    used: dict[str, set[str]] = {
        "source": set(),
        "title": set(),
        "series": set(),
    }
    groups_by_role: dict[AV1ValidationPartitionRole, Counter[str]] = {
        "holdout": Counter(),
        "derivation": Counter(),
    }
    groups_by_plan_role: dict[tuple[str, AV1ValidationPartitionRole], Counter[str]] = {}
    for slot, source in fixed:
        identities = {
            "source": source.source_identity,
            "title": source.title_identity,
            "series": source.series_identity,
        }
        if any(value in used[kind] for kind, value in identities.items()):
            raise AV1ValidationPartitionError(
                "AV1 partition fixed selection repeats an identity"
            )
        for kind, value in identities.items():
            used[kind].add(value)
        group = source.source_group_identity
        groups_by_role[slot.role][group] += 1
        groups_by_plan_role.setdefault((slot.plan.cell_plan_id, slot.role), Counter())[
            group
        ] += 1
    search_nodes = 0
    validation_slots = tuple(slot for slot, _ in fixed) + tuple(slots)

    def _search(index: int) -> bool:
        nonlocal search_nodes
        if index == len(slots):
            return _source_group_selection_valid(validation_slots, selected)
        slot = slots[index]
        for source in ranked_candidates[slot.assignment_id]:
            search_nodes += 1
            if search_nodes > _MAXIMUM_SEARCH_NODES:
                raise AV1ValidationPartitionError(
                    "AV1 partition assignment search exceeded its bound"
                )
            identities = {
                "source": source.source_identity,
                "title": source.title_identity,
                "series": source.series_identity,
            }
            if any(value in used[kind] for kind, value in identities.items()):
                continue
            opposite_role: AV1ValidationPartitionRole = (
                "derivation" if slot.role == "holdout" else "holdout"
            )
            group = source.source_group_identity
            if groups_by_role[opposite_role][group] > 0:
                continue
            plan_group_key = (slot.plan.cell_plan_id, slot.role)
            plan_groups = groups_by_plan_role.setdefault(plan_group_key, Counter())
            if slot.plan.mode == "publication_candidate":
                cohort_size = (
                    slot.plan.registered_case_count
                    if slot.role == "holdout"
                    else AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
                )
                maximum = math.floor(
                    cohort_size * AV1_VALIDATION_MAXIMUM_SOURCE_GROUP_FRACTION
                )
                if plan_groups[group] >= maximum:
                    continue
            for kind, value in identities.items():
                used[kind].add(value)
            groups_by_role[slot.role][group] += 1
            plan_groups[group] += 1
            selected[slot.assignment_id] = source
            if _search(index + 1):
                return True
            selected.pop(slot.assignment_id)
            groups_by_role[slot.role][group] -= 1
            if groups_by_role[slot.role][group] == 0:
                del groups_by_role[slot.role][group]
            plan_groups[group] -= 1
            if plan_groups[group] == 0:
                del plan_groups[group]
            for kind, value in identities.items():
                used[kind].remove(value)
        return False

    if not _search(0):
        raise AV1ValidationPartitionError(
            "AV1 partition cannot satisfy the preregistered identity constraints"
        )
    return selected


def _assignment_for_source(
    slot: _PartitionSlot,
    source: AV1ValidationPartitionSource,
    *,
    token_key: bytes,
) -> AV1ValidationPartitionAssignment:
    return AV1ValidationPartitionAssignment(
        assignment_id=slot.assignment_id,
        role=slot.role,
        cell_plan_id=slot.plan.cell_plan_id,
        ordinal=slot.ordinal,
        local_item_id=source.local_item_id,
        traits=source.traits,
        intent_level=slot.plan.intent_level,
        source_token=_identity_token(
            "source", source.source_identity, token_key=token_key
        ),
        title_token=_identity_token(
            "title", source.title_identity, token_key=token_key
        ),
        series_token=_identity_token(
            "series", source.series_identity, token_key=token_key
        ),
        source_group_token=_identity_token(
            "group", source.source_group_identity, token_key=token_key
        ),
        compatibility_signature=source.compatibility_signature,
        policy_signature=_plan_policy_signature(
            source.base_policy_signature, slot.plan
        ),
        target_video_bitrate_bps=source.target_video_bitrate_bps,
        quality_metric=source.quality_metric,
        quality_target=source.quality_target,
        minimum_quality_score=source.minimum_quality_score,
        evidence_summary_sha256=source.evidence_summary_sha256,
    )


def _validate_assignment_contract(
    manifest: AV1ValidationManifestV2,
    assignments: Sequence[AV1ValidationPartitionAssignment],
) -> None:
    expected_slots = {slot.assignment_id: slot for slot in _partition_slots(manifest)}
    actual_ids = [assignment.assignment_id for assignment in assignments]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(
        expected_slots
    ):
        raise AV1ValidationPartitionError(
            "AV1 partition does not cover the exact registered slots"
        )
    for attribute in (
        "local_item_id",
        "source_token",
        "title_token",
        "series_token",
    ):
        values = [getattr(assignment, attribute) for assignment in assignments]
        if len(values) != len(set(values)):
            raise AV1ValidationPartitionError(
                f"AV1 partition repeats {attribute.replace('_', ' ')}"
            )
    for assignment in assignments:
        slot = expected_slots[assignment.assignment_id]
        if (
            assignment.role != slot.role
            or assignment.cell_plan_id != slot.plan.cell_plan_id
            or assignment.ordinal != slot.ordinal
            or assignment.intent_level != slot.plan.intent_level
            or not slot.plan.trait_selector.matches(assignment.traits)
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition assignment violates its registered slot"
            )
    by_plan = {
        plan.cell_plan_id: [
            assignment
            for assignment in assignments
            if assignment.cell_plan_id == plan.cell_plan_id
        ]
        for plan in manifest.cell_plans
    }
    for plan in manifest.cell_plans:
        plan_assignments = by_plan[plan.cell_plan_id]
        holdouts = [
            assignment
            for assignment in plan_assignments
            if assignment.role == "holdout"
        ]
        derivation = [
            assignment
            for assignment in plan_assignments
            if assignment.role == "derivation"
        ]
        if len(holdouts) != plan.registered_case_count:
            raise AV1ValidationPartitionError(
                "AV1 partition holdout count does not match manifest"
            )
        expected_derivation = (
            AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
            if plan.mode == "publication_candidate"
            else 0
        )
        if len(derivation) != expected_derivation:
            raise AV1ValidationPartitionError(
                "AV1 partition derivation count does not match protocol"
            )
        if plan.mode != "publication_candidate":
            continue
        _validate_source_group_distribution(holdouts)
        _validate_source_group_distribution(derivation)
        for attribute in ("source_token", "series_token"):
            if {getattr(assignment, attribute) for assignment in holdouts} & {
                getattr(assignment, attribute) for assignment in derivation
            }:
                raise AV1ValidationPartitionError(
                    "AV1 partition derivation overlaps its holdout"
                )
    holdout_groups = {
        assignment.source_group_token
        for assignment in assignments
        if assignment.role == "holdout"
    }
    derivation_groups = {
        assignment.source_group_token
        for assignment in assignments
        if assignment.role == "derivation"
    }
    if holdout_groups & derivation_groups:
        raise AV1ValidationPartitionError(
            "AV1 partition derivation source groups overlap holdouts"
        )


def _validate_source_group_distribution(
    assignments: Sequence[AV1ValidationPartitionAssignment],
) -> None:
    groups = Counter(assignment.source_group_token for assignment in assignments)
    if len(groups) < AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT:
        raise AV1ValidationPartitionError(
            "AV1 partition lacks required source-group diversity"
        )
    maximum = math.floor(
        len(assignments) * AV1_VALIDATION_MAXIMUM_SOURCE_GROUP_FRACTION
    )
    if max(groups.values(), default=0) > maximum:
        raise AV1ValidationPartitionError(
            "AV1 partition exceeds source-group concentration limit"
        )


def _validate_candidate_group_capacity(
    plan: AV1ValidationV2CellPlan,
    candidates: Sequence[AV1ValidationPartitionSource],
) -> None:
    groups = Counter(source.source_group_identity for source in candidates)
    minimum_distinct_groups = 2 * AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT
    if len(groups) < minimum_distinct_groups:
        raise AV1ValidationPartitionError(
            f"AV1 partition cell {plan.cell_plan_id} lacks disjoint candidate source-group diversity"
        )
    if not _candidate_group_capacity_available(plan, groups):
        raise AV1ValidationPartitionError(
            f"AV1 partition cell {plan.cell_plan_id} lacks source-group concentration capacity"
        )


def _candidate_group_capacity_available(
    plan: AV1ValidationV2CellPlan,
    groups: Mapping[str, int],
) -> bool:
    holdout_target = plan.registered_case_count
    derivation_target = AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
    holdout_maximum = math.floor(
        holdout_target * AV1_VALIDATION_MAXIMUM_SOURCE_GROUP_FRACTION
    )
    derivation_maximum = math.floor(
        derivation_target * AV1_VALIDATION_MAXIMUM_SOURCE_GROUP_FRACTION
    )
    minimum_groups = AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT
    states = {(0, 0, 0, 0)}
    for count in groups.values():
        holdout_capacity = min(count, holdout_maximum)
        derivation_capacity = min(count, derivation_maximum)
        updated = set(states)
        for (
            holdout_count,
            derivation_count,
            holdout_groups,
            derivation_groups,
        ) in states:
            updated.add(
                (
                    min(holdout_target, holdout_count + holdout_capacity),
                    derivation_count,
                    min(minimum_groups, holdout_groups + 1),
                    derivation_groups,
                )
            )
            updated.add(
                (
                    holdout_count,
                    min(derivation_target, derivation_count + derivation_capacity),
                    holdout_groups,
                    min(minimum_groups, derivation_groups + 1),
                )
            )
        states = updated
    return (
        holdout_target,
        derivation_target,
        minimum_groups,
        minimum_groups,
    ) in states


def _source_group_selection_valid(
    slots: Sequence[_PartitionSlot],
    selected: Mapping[str, AV1ValidationPartitionSource],
) -> bool:
    for role in ("holdout", "derivation"):
        for plan_id in {
            slot.plan.cell_plan_id
            for slot in slots
            if slot.plan.mode == "publication_candidate" and slot.role == role
        }:
            groups = Counter(
                selected[slot.assignment_id].source_group_identity
                for slot in slots
                if slot.plan.cell_plan_id == plan_id and slot.role == role
            )
            if len(groups) < AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT:
                return False
    return True


def _plan_bindings(
    manifest: AV1ValidationManifestV2,
    assignments: Sequence[AV1ValidationPartitionAssignment],
) -> tuple[AV1ValidationPartitionPlanBinding, ...]:
    bindings = []
    for plan in manifest.cell_plans:
        selected = [
            assignment
            for assignment in assignments
            if assignment.cell_plan_id == plan.cell_plan_id
        ]
        compatibility = {assignment.compatibility_signature for assignment in selected}
        policies = {assignment.policy_signature for assignment in selected}
        metrics = {assignment.quality_metric for assignment in selected}
        targets = {assignment.quality_target for assignment in selected}
        floors = {assignment.minimum_quality_score for assignment in selected}
        if any(
            len(values) != 1
            for values in (compatibility, policies, metrics, targets, floors)
        ):
            raise AV1ValidationPartitionError(
                "AV1 partition plan execution contract is incoherent"
            )
        bitrates = [assignment.target_video_bitrate_bps for assignment in selected]
        bindings.append(
            AV1ValidationPartitionPlanBinding(
                cell_plan_id=plan.cell_plan_id,
                intent_level=plan.intent_level,
                compatibility_signature=next(iter(compatibility)),
                policy_signature=next(iter(policies)),
                target_video_bitrate_min_bps=min(bitrates),
                target_video_bitrate_max_bps=max(bitrates),
                quality_metric=next(iter(metrics)),
                quality_target=next(iter(targets)),
                minimum_quality_score=next(iter(floors)),
                holdout_count=sum(
                    assignment.role == "holdout" for assignment in selected
                ),
                derivation_reservation_count=sum(
                    assignment.role == "derivation" for assignment in selected
                ),
            )
        )
    return tuple(sorted(bindings, key=lambda binding: binding.cell_plan_id))


def _inventory_snapshot_sha256(
    *,
    manifest: AV1ValidationManifestV2,
    sources: Sequence[AV1ValidationPartitionSource],
    token_key: bytes,
) -> str:
    rows = []
    for source in sources:
        eligible_plan_ids = sorted(
            plan.cell_plan_id
            for plan in manifest.cell_plans
            if plan.trait_selector.matches(source.traits)
        )
        if not eligible_plan_ids:
            continue
        rows.append(
            {
                "source_token": _identity_token(
                    "source", source.source_identity, token_key=token_key
                ),
                "title_token": _identity_token(
                    "title", source.title_identity, token_key=token_key
                ),
                "series_token": _identity_token(
                    "series", source.series_identity, token_key=token_key
                ),
                "source_group_token": _identity_token(
                    "group", source.source_group_identity, token_key=token_key
                ),
                "traits": list(source.traits),
                "eligible_cell_plan_ids": eligible_plan_ids,
                "compatibility_signature": source.compatibility_signature,
                "base_policy_signature": source.base_policy_signature,
                "target_video_bitrate_bps": source.target_video_bitrate_bps,
                "quality_metric": source.quality_metric,
                "quality_target": source.quality_target,
                "minimum_quality_score": source.minimum_quality_score,
                "evidence_summary_sha256": source.evidence_summary_sha256,
            }
        )
    return _payload_sha256(
        sorted(
            rows,
            key=lambda row: (
                str(row["source_token"]),
                str(row["series_token"]),
            ),
        )
    )


def _derivation_partition_sha256(
    *,
    manifest_id: str,
    assignments: Sequence[AV1ValidationPartitionAssignment],
) -> str:
    return _payload_sha256(
        {
            "schema": "mediaforce.av1_cold_start_derivation_partition",
            "schema_version": 1,
            "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
            "manifest_id": manifest_id,
            "assignments": [
                assignment.token_payload()
                for assignment in assignments
                if assignment.role == "derivation"
            ],
        }
    )


def _selection_lock_payload(
    *,
    manifest_id: str,
    manifest_payload_sha256: str,
    eligibility_attestation_id: str,
    eligibility_payload_sha256: str,
    selected_at: str,
    token_key_id: str,
    inventory_snapshot_sha256: str,
    expectations: AV1ValidationPartitionExpectations,
    plan_bindings: Sequence[AV1ValidationPartitionPlanBinding],
    assignments: Sequence[AV1ValidationPartitionAssignment],
    derivation_partition_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_SELECTION_LOCK_SCHEMA,
        "schema_version": AV1_VALIDATION_SELECTION_LOCK_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
        "authority": "selection_lock_only",
        "runtime_execution_authorized": False,
        "derivation_execution_authorized": False,
        "holdout_execution_authorized": False,
        "manifest_id": manifest_id,
        "manifest_payload_sha256": manifest_payload_sha256,
        "eligibility_attestation_id": eligibility_attestation_id,
        "eligibility_payload_sha256": eligibility_payload_sha256,
        "selected_at": selected_at,
        "assignment_algorithm": AV1_VALIDATION_PARTITION_ALGORITHM,
        "token_version": AV1_VALIDATION_PARTITION_TOKEN_VERSION,
        "token_key_id": token_key_id,
        "identity_policy": {
            "global_source_unique": True,
            "global_title_unique": True,
            "global_series_unique": True,
            "derivation_holdout_disjoint": ["source", "series", "source_group"],
            "candidate_source_group_minimum": AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT,
            "candidate_source_group_maximum_numerator": 1,
            "candidate_source_group_maximum_denominator": 3,
        },
        "inventory_snapshot_sha256": inventory_snapshot_sha256,
        "expectations": expectations.to_payload(),
        "plan_bindings": [binding.to_payload() for binding in plan_bindings],
        "assignments": [assignment.token_payload() for assignment in assignments],
        "derivation_partition_sha256": derivation_partition_sha256,
    }


def _source_matches_expectations(
    source: AV1ValidationPartitionSource,
    *,
    expectations: AV1ValidationPartitionExpectations,
) -> bool:
    return (
        source.compatibility_signature == expectations.compatibility_signature
        and source.base_policy_signature == expectations.base_policy_signature
        and source.quality_metric == expectations.quality_metric
        and source.quality_target == expectations.quality_target
        and source.minimum_quality_score == expectations.minimum_quality_score
    )


def _validate_source_set(sources: Sequence[AV1ValidationPartitionSource]) -> None:
    for attribute, label in (
        ("local_item_id", "local item IDs"),
        ("source_identity", "source identities"),
    ):
        values = [getattr(source, attribute) for source in sources]
        if len(values) != len(set(values)):
            raise AV1ValidationPartitionError(
                f"AV1 partition inventory repeats {label}"
            )


def _assert_partition_criteria(manifest: AV1ValidationManifestV2) -> None:
    criteria = manifest.criteria
    if (
        criteria.minimum_derivation_evidence_count
        != AV1_VALIDATION_DERIVATION_RESERVATION_COUNT
        or criteria.minimum_derivation_source_count
        != AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT
        or criteria.minimum_holdout_source_count
        != AV1_VALIDATION_MINIMUM_SOURCE_GROUP_COUNT
    ):
        raise AV1ValidationPartitionError(
            "AV1 partition policy does not match the preregistered criteria"
        )


def _rank_source(
    source: AV1ValidationPartitionSource,
    *,
    manifest_id: str,
    assignment_id: str,
    token_key: bytes,
) -> str:
    return hmac.new(
        _derived_key(token_key, "selection-rank"),
        canonical_json_bytes(
            {
                "manifest_id": manifest_id,
                "assignment_id": assignment_id,
                "source_identity": source.source_identity,
                "title_identity": source.title_identity,
                "series_identity": source.series_identity,
                "source_group_identity": source.source_group_identity,
            }
        ),
        hashlib.sha256,
    ).hexdigest()


def _identity_token(kind: str, identity: str, *, token_key: bytes) -> str:
    prefixes = {
        "source": "av1src1",
        "title": "av1title1",
        "series": "av1series1",
        "group": "av1group1",
    }
    prefix = prefixes.get(kind)
    if prefix is None:
        raise AV1ValidationPartitionError("AV1 partition token kind is invalid")
    digest = hmac.new(
        _derived_key(token_key, f"identity:{kind}"),
        canonical_json_bytes({"identity": identity}),
        hashlib.sha256,
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _plan_policy_signature(
    base_policy_signature: str, plan: AV1ValidationV2CellPlan
) -> str:
    return f"av1vpolicy1_{
        stable_json_hash(
            {
                'base_policy_signature': base_policy_signature,
                'intent_level': plan.intent_level,
                'optimization_objective': plan.optimization_objective,
            }
        )[:32]
    }"


def _derived_key(token_key: bytes, domain: str) -> bytes:
    return hmac.new(
        token_key,
        f"{AV1_VALIDATION_PARTITION_TOKEN_VERSION}:{domain}".encode(),
        hashlib.sha256,
    ).digest()


def _token_key_id(token_key: bytes) -> str:
    return f"av1vkey1_{
        hmac.new(
            _derived_key(token_key, 'key-id'),
            b'mediaforce-av1-validation-partition',
            hashlib.sha256,
        ).hexdigest()[:32]
    }"


def _validate_key(token_key: bytes) -> None:
    if len(token_key) < 32:
        raise AV1ValidationPartitionError(
            "AV1 partition token key must contain at least 32 bytes"
        )


def _assignment_sort_key(
    assignment: AV1ValidationPartitionAssignment,
) -> tuple[str, str, int, str]:
    return (
        assignment.role,
        assignment.cell_plan_id,
        assignment.ordinal,
        assignment.assignment_id,
    )


def _partition_id(kind: str, payload: object) -> str:
    prefixes = {
        "partition": "av1vpartition1",
        "derivation_slot": "av1vderive1",
    }
    prefix = prefixes.get(kind)
    if prefix is None:
        raise AV1ValidationPartitionError("AV1 partition ID kind is invalid")
    return f"{prefix}_{stable_json_hash(payload)[:32]}"


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationPartitionError(f"AV1 partition {label} is invalid")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationPartitionError(f"AV1 partition {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AV1ValidationPartitionError(
            f"AV1 partition {label} must be timezone-aware"
        )
    normalized = (
        parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    if value != normalized:
        raise AV1ValidationPartitionError(
            f"AV1 partition {label} must be canonical UTC"
        )
    return parsed


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AV1ValidationPartitionError(f"AV1 partition {label} is required")
    return text


def _assert_owner_only_directory(path: Path) -> None:
    if os.name != "posix":
        return
    directory_stat = path.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise AV1ValidationPartitionError(
            "AV1 private artifact parent is not a directory"
        )
    if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
        raise AV1ValidationPartitionError(
            "AV1 private artifact directory must be owner-only"
        )


def _assert_owner_only_regular_file(path: Path, *, label: str) -> None:
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise AV1ValidationPartitionError(f"AV1 {label} is not a regular file")
    if os.name == "posix" and (
        file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077
    ):
        raise AV1ValidationPartitionError(f"AV1 {label} permissions must be owner-only")


def _read_owner_only_regular_file(path: Path, *, label: str) -> bytes:
    _assert_owner_only_regular_file(path, label=label)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AV1ValidationPartitionError(f"AV1 {label} is not a regular file")
        if os.name == "posix" and (
            file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077
        ):
            raise AV1ValidationPartitionError(f"AV1 {label} permissions must be owner-only")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    except OSError as exc:
        raise AV1ValidationPartitionError(f"AV1 {label} could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _expectations_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationPartitionExpectations:
    data = object_dict(payload)
    _require_exact_keys(
        data,
        {
            "compatibility_signature",
            "base_policy_signature",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
        },
        "expectations",
    )
    return AV1ValidationPartitionExpectations(
        compatibility_signature=_required_text(
            data.get("compatibility_signature"), "compatibility signature"
        ),
        base_policy_signature=_required_text(
            data.get("base_policy_signature"), "base policy signature"
        ),
        quality_metric=_required_text(data.get("quality_metric"), "quality metric"),
        quality_target=float_value(data.get("quality_target")),
        minimum_quality_score=float_value(data.get("minimum_quality_score")),
    )


def _source_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationPartitionSource:
    data = object_dict(payload)
    _require_exact_keys(
        data,
        {
            "local_item_id",
            "source_identity",
            "title_identity",
            "series_identity",
            "source_group_identity",
            "traits",
            "compatibility_signature",
            "base_policy_signature",
            "target_video_bitrate_bps",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
            "evidence_summary_sha256",
        },
        "inventory source",
    )
    return AV1ValidationPartitionSource(
        local_item_id=int_value(data.get("local_item_id")),
        source_identity=_required_text(data.get("source_identity"), "source identity"),
        title_identity=_required_text(data.get("title_identity"), "title identity"),
        series_identity=_required_text(data.get("series_identity"), "series identity"),
        source_group_identity=_required_text(
            data.get("source_group_identity"), "source-group identity"
        ),
        traits=tuple(str(item) for item in object_list(data.get("traits"))),
        compatibility_signature=_required_text(
            data.get("compatibility_signature"), "compatibility signature"
        ),
        base_policy_signature=_required_text(
            data.get("base_policy_signature"), "base policy signature"
        ),
        target_video_bitrate_bps=int_value(data.get("target_video_bitrate_bps")),
        quality_metric=_required_text(data.get("quality_metric"), "quality metric"),
        quality_target=float_value(data.get("quality_target")),
        minimum_quality_score=float_value(data.get("minimum_quality_score")),
        evidence_summary_sha256=_required_text(
            data.get("evidence_summary_sha256"), "evidence digest"
        ),
    )


def _plan_binding_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationPartitionPlanBinding:
    data = object_dict(payload)
    _require_exact_keys(
        data,
        {
            "cell_plan_id",
            "intent_level",
            "compatibility_signature",
            "policy_signature",
            "target_video_bitrate_min_bps",
            "target_video_bitrate_max_bps",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
            "holdout_count",
            "derivation_reservation_count",
        },
        "plan binding",
    )
    return AV1ValidationPartitionPlanBinding(
        cell_plan_id=_required_text(data.get("cell_plan_id"), "cell-plan ID"),
        intent_level=_required_text(data.get("intent_level"), "intent"),
        compatibility_signature=_required_text(
            data.get("compatibility_signature"),
            "compatibility signature",
        ),
        policy_signature=_required_text(
            data.get("policy_signature"), "policy signature"
        ),
        target_video_bitrate_min_bps=int_value(
            data.get("target_video_bitrate_min_bps")
        ),
        target_video_bitrate_max_bps=int_value(
            data.get("target_video_bitrate_max_bps")
        ),
        quality_metric=_required_text(data.get("quality_metric"), "quality metric"),
        quality_target=float_value(data.get("quality_target")),
        minimum_quality_score=float_value(data.get("minimum_quality_score")),
        holdout_count=int_value(data.get("holdout_count")),
        derivation_reservation_count=int_value(
            data.get("derivation_reservation_count")
        ),
    )


def _assignment_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationPartitionAssignment:
    data = object_dict(payload)
    _require_exact_keys(
        data,
        {
            "assignment_id",
            "role",
            "cell_plan_id",
            "ordinal",
            "traits",
            "intent_level",
            "source_token",
            "title_token",
            "series_token",
            "source_group_token",
            "compatibility_signature",
            "policy_signature",
            "target_video_bitrate_bps",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
            "local_item_id",
            "evidence_summary_sha256",
        },
        "assignment",
    )
    role = _required_text(data.get("role"), "assignment role")
    if role not in {"holdout", "derivation"}:
        raise AV1ValidationPartitionError("AV1 partition assignment role is invalid")
    return AV1ValidationPartitionAssignment(
        assignment_id=_required_text(data.get("assignment_id"), "assignment ID"),
        role=role,
        cell_plan_id=_required_text(data.get("cell_plan_id"), "cell-plan ID"),
        ordinal=int_value(data.get("ordinal")),
        local_item_id=int_value(data.get("local_item_id")),
        traits=tuple(str(item) for item in object_list(data.get("traits"))),
        intent_level=_required_text(data.get("intent_level"), "intent"),
        source_token=_required_text(data.get("source_token"), "source token"),
        title_token=_required_text(data.get("title_token"), "title token"),
        series_token=_required_text(data.get("series_token"), "series token"),
        source_group_token=_required_text(
            data.get("source_group_token"), "source-group token"
        ),
        compatibility_signature=_required_text(
            data.get("compatibility_signature"),
            "compatibility signature",
        ),
        policy_signature=_required_text(
            data.get("policy_signature"), "policy signature"
        ),
        target_video_bitrate_bps=int_value(data.get("target_video_bitrate_bps")),
        quality_metric=_required_text(data.get("quality_metric"), "quality metric"),
        quality_target=float_value(data.get("quality_target")),
        minimum_quality_score=float_value(data.get("minimum_quality_score")),
        evidence_summary_sha256=_required_text(
            data.get("evidence_summary_sha256"),
            "evidence digest",
        ),
    )


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(payload) != expected:
        raise AV1ValidationPartitionError(f"AV1 partition {label} keys are invalid")
