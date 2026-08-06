from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_trait_projection import (
    AV1ColdStartTraitProjectionError,
    project_av1_cold_start_fingerprint_summary,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    _av1_validation_fingerprint_summaries,
    av1_validation_compatibility_sort_key,
    av1_validation_evidence_compatibility,
    av1_validation_measured_fingerprint_rows,
    av1_validation_normalized_policy,
    av1_validation_quality_contract,
)
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_PROTOCOL_VERSION,
    AV1ValidationProtocolV3,
    AV1ValidationV3QualificationSource,
    assert_preregistered_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    assert_av1_validation_v3_tier1_config_snapshot_matches,
    av1_validation_v3_tier1_config_snapshot_sha256,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadContext,
    assert_av1_validation_v3_tier2_inventory_read_context,
)
from mediaforce.tuning.compression_intent import compression_intent_from_policy
from mediaforce.tuning.stream_budget import (
    resolve_stream_budget_ledger,
    stream_budget_projection_blocker,
)


AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN = (
    "mediaforce:av1:v3:tier2-qualification-source:v1"
)
_CONTENT_VERSION_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier2InventoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _AV1ValidationV3Tier2InventoryEntry:
    local_item_id: int
    source_identity: str
    evidence_summary_sha256: str
    qualification_source: AV1ValidationV3QualificationSource

    def __post_init__(self) -> None:
        if self.local_item_id <= 0:
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 inventory local item ID is invalid"
            )
        if not _CONTENT_VERSION_RE.fullmatch(self.source_identity):
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 inventory source identity is invalid"
            )
        if not _SHA256_RE.fullmatch(self.evidence_summary_sha256):
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 inventory evidence digest is invalid"
            )

    @property
    def source_fingerprint(self) -> str:
        return self.qualification_source.source_fingerprint


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryStratumCount:
    stratum_id: str
    stratum_name: str
    intent_level: str
    exact_traits: tuple[str, ...]
    private_candidate_count: int


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2Inventory:
    entries: tuple[_AV1ValidationV3Tier2InventoryEntry, ...]
    frozen_stratum_private_counts: tuple[
        AV1ValidationV3Tier2InventoryStratumCount,
        ...,
    ]
    measured_row_count: int
    incompatible_evidence_count: int
    malformed_identity_count: int
    duplicate_source_identity_row_count: int
    missing_evidence_summary_count: int
    projection_error_count: int
    ambiguous_trait_count: int
    non_tier2_trait_count: int
    powered_candidate_cell_overlap_count: int
    non_balanced_intent_count: int
    unconfirmed_intent_count: int
    incomplete_quality_contract_count: int
    infeasible_stream_budget_count: int

    def __post_init__(self) -> None:
        if self.entries != tuple(
            sorted(self.entries, key=lambda entry: entry.source_fingerprint)
        ):
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 inventory entries are not canonical"
            )
        fingerprints = [entry.source_fingerprint for entry in self.entries]
        if len(fingerprints) != len(set(fingerprints)):
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 inventory repeats a source fingerprint"
            )
        if self.frozen_stratum_private_counts != tuple(
            sorted(
                self.frozen_stratum_private_counts,
                key=lambda count: count.stratum_name,
            )
        ):
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 stratum counts are not canonical"
            )
        for value in (
            self.measured_row_count,
            self.incompatible_evidence_count,
            self.malformed_identity_count,
            self.duplicate_source_identity_row_count,
            self.missing_evidence_summary_count,
            self.projection_error_count,
            self.ambiguous_trait_count,
            self.non_tier2_trait_count,
            self.powered_candidate_cell_overlap_count,
            self.non_balanced_intent_count,
            self.unconfirmed_intent_count,
            self.incomplete_quality_contract_count,
            self.infeasible_stream_budget_count,
        ):
            if value < 0:
                raise AV1ValidationV3Tier2InventoryError(
                    "AV1 v3 Tier 2 inventory count cannot be negative"
                )

    @property
    def candidate_sources(self) -> tuple[AV1ValidationV3QualificationSource, ...]:
        return tuple(entry.qualification_source for entry in self.entries)


def load_av1_validation_v3_tier2_inventory(
    connection: DBClient,
    *,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    read_context: AV1ValidationV3Tier2InventoryReadContext,
    config_snapshot_bytes: bytes,
    clock: Callable[[], str],
) -> AV1ValidationV3Tier2Inventory:
    _assert_authorized_inventory_read(
        config=config,
        protocol=protocol,
        read_context=read_context,
        config_snapshot_bytes=config_snapshot_bytes,
        as_of=clock(),
    )
    rows = tuple(av1_validation_measured_fingerprint_rows(connection))
    compatible = [
        evidence
        for row in rows
        if (evidence := av1_validation_evidence_compatibility(row)) is not None
    ]
    evidence_cohorts = Counter(compatible)
    expected_evidence = (
        min(
            evidence_cohorts,
            key=lambda cohort: (
                -evidence_cohorts[cohort],
                av1_validation_compatibility_sort_key(cohort),
            ),
        )
        if evidence_cohorts
        else None
    )
    counts: Counter[str] = Counter()
    counts["measured_row_count"] = len(rows)
    rows_by_identity: dict[str, list[Mapping[str, Any]]] = {}
    unique_identity_rows: list[tuple[str, Mapping[str, Any]]] = []

    for row in rows:
        source_identity = _source_identity(row)
        if source_identity is None:
            counts["malformed_identity_count"] += 1
            continue
        rows_by_identity.setdefault(source_identity, []).append(row)

    for source_identity, identity_rows in rows_by_identity.items():
        if len(identity_rows) > 1:
            counts["duplicate_source_identity_row_count"] += len(identity_rows)
        else:
            row = identity_rows[0]
            evidence = av1_validation_evidence_compatibility(row)
            if expected_evidence is None or evidence != expected_evidence:
                counts["incompatible_evidence_count"] += 1
                continue
            unique_identity_rows.append((source_identity, row))

    entries: list[_AV1ValidationV3Tier2InventoryEntry] = []
    fingerprint_identities: dict[str, str] = {}
    for source_identity, row in unique_identity_rows:
        entry = _entry_from_row(
            row,
            source_identity=source_identity,
            config=config,
            protocol=protocol,
            counts=counts,
        )
        if entry is None:
            continue
        previous_identity = fingerprint_identities.setdefault(
            entry.source_fingerprint,
            entry.source_identity,
        )
        if previous_identity != entry.source_identity:
            raise AV1ValidationV3Tier2InventoryError(
                "AV1 v3 Tier 2 source fingerprint collision detected"
            )
        entries.append(entry)

    sorted_entries = tuple(
        sorted(entries, key=lambda candidate: candidate.source_fingerprint)
    )
    stratum_counts = _stratum_counts(protocol, sorted_entries)
    return AV1ValidationV3Tier2Inventory(
        entries=sorted_entries,
        frozen_stratum_private_counts=stratum_counts,
        measured_row_count=counts["measured_row_count"],
        incompatible_evidence_count=counts["incompatible_evidence_count"],
        malformed_identity_count=counts["malformed_identity_count"],
        duplicate_source_identity_row_count=counts[
            "duplicate_source_identity_row_count"
        ],
        missing_evidence_summary_count=counts["missing_evidence_summary_count"],
        projection_error_count=counts["projection_error_count"],
        ambiguous_trait_count=counts["ambiguous_trait_count"],
        non_tier2_trait_count=counts["non_tier2_trait_count"],
        powered_candidate_cell_overlap_count=counts[
            "powered_candidate_cell_overlap_count"
        ],
        non_balanced_intent_count=counts["non_balanced_intent_count"],
        unconfirmed_intent_count=counts["unconfirmed_intent_count"],
        incomplete_quality_contract_count=counts[
            "incomplete_quality_contract_count"
        ],
        infeasible_stream_budget_count=counts["infeasible_stream_budget_count"],
    )


def _assert_authorized_inventory_read(
    *,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    read_context: AV1ValidationV3Tier2InventoryReadContext,
    config_snapshot_bytes: bytes,
    as_of: str,
) -> None:
    assert_preregistered_av1_validation_protocol_v3(protocol)
    assert_av1_validation_v3_tier2_inventory_read_context(
        protocol,
        read_context,
        as_of=as_of,
    )
    assert_av1_validation_v3_tier1_config_snapshot_matches(
        config,
        config_snapshot_bytes,
    )
    if (
        read_context.plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(config_snapshot_bytes)
    ):
        raise AV1ValidationV3Tier2InventoryError(
            "AV1 v3 Tier 2 inventory read config is not bound to the plan"
        )


def _entry_from_row(
    row: Mapping[str, Any],
    *,
    source_identity: str,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    counts: Counter[str],
) -> _AV1ValidationV3Tier2InventoryEntry | None:
    evidence_summary_sha256 = str(row.get("summary_sha256") or "").strip()
    if not _SHA256_RE.fullmatch(evidence_summary_sha256):
        counts["missing_evidence_summary_count"] += 1
        return None
    rel_path = str(row.get("rel_path") or "").strip().strip("/")
    summaries = _av1_validation_fingerprint_summaries(row)
    if summaries is None:
        counts["projection_error_count"] += 1
        return None
    try:
        projection = project_av1_cold_start_fingerprint_summary(
            summaries.fingerprint_summary
        )
    except AV1ColdStartTraitProjectionError:
        counts["projection_error_count"] += 1
        return None

    traits = tuple(projection.cell_traits)
    if "mixed" in traits or "unknown" in traits:
        counts["ambiguous_trait_count"] += 1
        return None
    if any(
        cell.matches(intent_level="balanced", traits=traits)
        for cell in protocol.candidate_cells
    ):
        counts["powered_candidate_cell_overlap_count"] += 1
        return None
    matching_strata = tuple(
        stratum
        for stratum in protocol.tier2_strata
        if stratum.intent_level == "balanced" and stratum.exact_traits == traits
    )
    if len(matching_strata) != 1:
        counts["non_tier2_trait_count"] += 1
        return None

    resolved_policy = config.resolve_policy(rel_path)
    intent = compression_intent_from_policy(
        object_dict(resolved_policy.get("video")),
        default_video_policy=config.video,
    )
    if intent.requires_confirmation:
        counts["unconfirmed_intent_count"] += 1
        return None
    if intent.level != "balanced":
        counts["non_balanced_intent_count"] += 1
        return None

    normalized_policy = av1_validation_normalized_policy(resolved_policy)
    if av1_validation_quality_contract(normalized_policy) is None:
        counts["incomplete_quality_contract_count"] += 1
        return None
    if not _stream_budget_feasible(
        row,
        config=config,
        rel_path=rel_path,
        resolved_policy=resolved_policy,
        audio_summary=summaries.audio_summary,
        subtitle_summary=summaries.subtitle_summary,
        attachment_summary=summaries.attachment_summary,
    ):
        counts["infeasible_stream_budget_count"] += 1
        return None

    source = AV1ValidationV3QualificationSource(
        source_fingerprint=_source_fingerprint(source_identity),
        intent_level=intent.level,
        exact_traits=traits,
        pipeline_ready=True,
    )
    return _AV1ValidationV3Tier2InventoryEntry(
        local_item_id=int(row["id"]),
        source_identity=source_identity,
        evidence_summary_sha256=evidence_summary_sha256,
        qualification_source=source,
    )


def _stream_budget_feasible(
    row: Mapping[str, Any],
    *,
    config: MediaforceConfig,
    rel_path: str,
    resolved_policy: Mapping[str, Any],
    audio_summary: object,
    subtitle_summary: object,
    attachment_summary: object,
) -> bool:
    item = {
        "library_item_id": int(row["id"]),
        "rel_path": rel_path,
        "source_size_bytes": int(row.get("size_bytes") or 0),
        "video_bitrate": row.get("video_bitrate"),
        "duration_seconds": row.get("duration_seconds"),
        "container": str(row.get("container") or ""),
        "output_container": config.output_container,
        "resolved_policy": resolved_policy,
        "audio_summary": audio_summary,
        "subtitle_summary": subtitle_summary,
        "attachment_summary": attachment_summary,
    }
    try:
        ledger = resolve_stream_budget_ledger(
            item,
            default_video_policy=config.video,
            output_container=config.output_container,
            prefer_persisted=False,
        )
    except (TypeError, ValueError):
        return False
    return (
        ledger.feasibility_status == "feasible"
        and ledger.remaining_video_bitrate_bps is not None
        and ledger.remaining_video_bitrate_bps > 0
        and stream_budget_projection_blocker(ledger) is None
    )


def _stratum_counts(
    protocol: AV1ValidationProtocolV3,
    entries: tuple[_AV1ValidationV3Tier2InventoryEntry, ...],
) -> tuple[AV1ValidationV3Tier2InventoryStratumCount, ...]:
    return tuple(
        AV1ValidationV3Tier2InventoryStratumCount(
            stratum_id=stratum.stratum_id,
            stratum_name=stratum.name,
            intent_level=stratum.intent_level,
            exact_traits=stratum.exact_traits,
            private_candidate_count=sum(
                1
                for entry in entries
                if stratum.matches(entry.qualification_source)
            ),
        )
        for stratum in protocol.tier2_strata
    )


def _source_identity(row: Mapping[str, Any]) -> str | None:
    value = str(row.get("content_version_fingerprint") or "").strip()
    if not _CONTENT_VERSION_RE.fullmatch(value):
        return None
    return value


def _source_fingerprint(content_version_fingerprint: str) -> str:
    if not _CONTENT_VERSION_RE.fullmatch(content_version_fingerprint):
        raise AV1ValidationV3Tier2InventoryError(
            "AV1 v3 Tier 2 source identity is invalid"
        )
    digest = hashlib.sha256(
        canonical_json_bytes({
            "domain": AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "content_version_fingerprint": content_version_fingerprint,
        })
    ).hexdigest()
    return f"sha256:{digest}"
