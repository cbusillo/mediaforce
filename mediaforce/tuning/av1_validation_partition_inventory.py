from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
import unicodedata
from typing import Any, Mapping

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.library.media_scopes import (
    media_group_scope_for_rel_path,
    normalize_scope_prefix,
    tv_series_scope_for_rel_path,
)
from mediaforce.tuning.av1_trait_projection import (
    AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
    AV1ColdStartTraitProjectionError,
    project_av1_cold_start_fingerprint_summary,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
    AV1ValidationPartitionExpectations,
    AV1ValidationPartitionSource,
)
from mediaforce.tuning.stream_budget import (
    resolve_stream_budget_ledger,
    stream_budget_projection_blocker,
)


@dataclass(frozen=True, slots=True)
class AV1ValidationPartitionInventory:
    expectations: AV1ValidationPartitionExpectations
    sources: tuple[AV1ValidationPartitionSource, ...]


@dataclass(frozen=True, slots=True)
class _EvidenceCompatibility:
    summary_schema_version: int
    analyzer_name: str
    analyzer_version: str
    analyzer_runtime_version: str
    analyzer_policy_digest: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "summary_schema_version": self.summary_schema_version,
            "analyzer_name": self.analyzer_name,
            "analyzer_version": self.analyzer_version,
            "analyzer_runtime_version": self.analyzer_runtime_version,
            "analyzer_policy_digest": self.analyzer_policy_digest,
        }


def load_av1_validation_partition_inventory(
    connection: DBClient,
    *,
    config: MediaforceConfig,
) -> AV1ValidationPartitionInventory:
    rows = list(
        connection.execute(
            select(
                library_items.c.id,
                library_items.c.rel_path,
                library_items.c.fingerprint,
                library_items.c.content_version_fingerprint,
                library_items.c.size_bytes,
                library_items.c.video_bitrate,
                library_items.c.duration_seconds,
                library_items.c.container,
                library_items.c.audio_summary_json,
                library_items.c.subtitle_summary_json,
                library_items.c.attachment_summary_json,
                library_items.c.media_fingerprint_json,
                library_item_evidence_state.c.summary_sha256,
                library_item_evidence_state.c.summary_schema_version,
                library_item_evidence_state.c.analyzer_name,
                library_item_evidence_state.c.analyzer_version,
                library_item_evidence_state.c.analyzer_runtime_version,
                library_item_evidence_state.c.policy_hash,
            )
            .join(
                library_item_evidence_state,
                library_item_evidence_state.c.library_item_id == library_items.c.id,
            )
            .where(
                library_items.c.status != "missing",
                library_item_evidence_state.c.evidence_kind
                == MEDIA_FINGERPRINT_EVIDENCE_KIND,
                library_item_evidence_state.c.state == "current",
                library_item_evidence_state.c.decision_status == "measured",
            )
        ).mappings()
    )
    compatible_evidence = [
        evidence
        for row in rows
        if (evidence := _evidence_compatibility(row)) is not None
    ]
    evidence_cohorts = Counter(compatible_evidence)
    if not evidence_cohorts:
        raise ValueError(
            "AV1 partition inventory has no compatible measured fingerprint evidence"
        )
    expected_evidence = min(
        evidence_cohorts,
        key=lambda cohort: (-evidence_cohorts[cohort], _compatibility_sort_key(cohort)),
    )
    default_policy = _partition_policy(
        {
            "video": config.video,
            "audio": config.audio,
            "subtitle": config.subtitle,
        }
    )
    expectations = _expectations(
        config=config,
        policy=default_policy,
        evidence=expected_evidence,
    )

    sources = []
    for row in rows:
        evidence = _evidence_compatibility(row)
        if evidence is None:
            continue
        source = _source_from_row(
            row,
            config=config,
            evidence=evidence,
        )
        if source is not None:
            sources.append(source)
    source_counts = Counter(source.source_identity for source in sources)
    unambiguous_sources = [
        source for source in sources if source_counts[source.source_identity] == 1
    ]
    return AV1ValidationPartitionInventory(
        expectations=expectations,
        sources=tuple(
            sorted(unambiguous_sources, key=lambda source: source.local_item_id)
        ),
    )


def _source_from_row(
    row: Mapping[str, Any],
    *,
    config: MediaforceConfig,
    evidence: _EvidenceCompatibility,
) -> AV1ValidationPartitionSource | None:
    rel_path = normalize_scope_prefix(str(row.get("rel_path") or ""))
    content_version = str(row.get("content_version_fingerprint") or "").strip()
    summary_digest = str(row.get("summary_sha256") or "").strip()
    if not rel_path or not content_version or not summary_digest:
        return None
    group_scope = media_group_scope_for_rel_path(
        rel_path,
        library_types=config.library_type_map,
    )
    if group_scope is None or not group_scope.prefix:
        return None
    series_scope = tv_series_scope_for_rel_path(
        rel_path,
        library_types=config.library_type_map,
    )
    series_prefix = (
        series_scope.prefix if series_scope is not None else group_scope.prefix
    )
    try:
        fingerprint_summary = object_dict(
            json.loads(str(row.get("media_fingerprint_json") or "{}"))
        )
        projection = project_av1_cold_start_fingerprint_summary(fingerprint_summary)
        audio_summary = json.loads(str(row.get("audio_summary_json") or "[]"))
        subtitle_summary = json.loads(str(row.get("subtitle_summary_json") or "[]"))
        attachment_raw = row.get("attachment_summary_json")
        attachment_summary = json.loads(str(attachment_raw)) if attachment_raw else None
    except (
        AV1ColdStartTraitProjectionError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return None

    resolved_policy = config.resolve_policy(rel_path)
    normalized_policy = _partition_policy(resolved_policy)
    quality_contract = _quality_contract(normalized_policy)
    if quality_contract is None:
        return None
    quality_metric, quality_target, minimum_quality_score = quality_contract
    item = {
        "library_item_id": int(row["id"]),
        "rel_path": rel_path,
        "source_fingerprint": str(row.get("fingerprint") or "") or None,
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
        return None
    target_video_bitrate_bps = ledger.remaining_video_bitrate_bps
    if (
        ledger.feasibility_status != "feasible"
        or target_video_bitrate_bps is None
        or target_video_bitrate_bps <= 0
        or stream_budget_projection_blocker(ledger) is not None
    ):
        return None
    return AV1ValidationPartitionSource(
        local_item_id=int(row["id"]),
        source_identity=content_version,
        title_identity=_normalized_identity(rel_path),
        series_identity=_normalized_identity(series_prefix),
        source_group_identity=_normalized_identity(group_scope.prefix),
        traits=projection.cell_traits,
        compatibility_signature=_compatibility_signature(
            config=config,
            evidence=evidence,
        ),
        base_policy_signature=_base_policy_signature(normalized_policy),
        target_video_bitrate_bps=target_video_bitrate_bps,
        quality_metric=quality_metric,
        quality_target=quality_target,
        minimum_quality_score=minimum_quality_score,
        evidence_summary_sha256=summary_digest,
    )


def _expectations(
    *,
    config: MediaforceConfig,
    policy: Mapping[str, Any],
    evidence: _EvidenceCompatibility,
) -> AV1ValidationPartitionExpectations:
    quality_contract = _quality_contract(policy)
    if quality_contract is None:
        raise ValueError("AV1 partition default quality contract is incomplete")
    quality_metric, quality_target, minimum_quality_score = quality_contract
    return AV1ValidationPartitionExpectations(
        compatibility_signature=_compatibility_signature(
            config=config,
            evidence=evidence,
        ),
        base_policy_signature=_base_policy_signature(policy),
        quality_metric=quality_metric,
        quality_target=quality_target,
        minimum_quality_score=minimum_quality_score,
    )


def _partition_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "video": dict(object_dict(policy.get("video"))),
        "audio": dict(object_dict(policy.get("audio"))),
        "subtitle": dict(object_dict(policy.get("subtitle"))),
    }
    for key in (
        "compression_intent",
        "compression_intent_source",
        "compression_intent_confirmed",
    ):
        normalized["video"].pop(key, None)
    return normalized


def _base_policy_signature(policy: Mapping[str, Any]) -> str:
    return f"av1vbasepolicy1_{
        stable_json_hash(
            {
                'contract_version': AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
                'policy': policy,
            }
        )[:32]
    }"


def _compatibility_signature(
    *,
    config: MediaforceConfig,
    evidence: _EvidenceCompatibility,
) -> str:
    video = config.video
    payload = {
        "contract_version": AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
        "trait_projection_contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "output_container": config.output_container,
        "video": {
            key: video.get(key)
            for key in (
                "encoder",
                "pixel_format",
                "preset",
                "quality_engine",
                "quality_metric",
                "max_height",
                "resolution_intent_mode",
                "downsample_algorithm",
                "black_bar_handling",
                "crop",
            )
        },
        "fingerprint_evidence": evidence.to_payload(),
    }
    return f"av1vcompat1_{stable_json_hash(payload)[:32]}"


def _quality_contract(policy: Mapping[str, Any]) -> tuple[str, float, float] | None:
    video = object_dict(policy.get("video"))
    metric = str(video.get("quality_metric") or "").strip().lower()
    if metric == "vmaf":
        target = _finite_number(video.get("target_vmaf"))
        minimum = _finite_number(video.get("min_target_vmaf"))
    elif metric == "xpsnr":
        target = _finite_number(video.get("target_xpsnr"))
        minimum = _finite_number(video.get("min_target_xpsnr"))
    else:
        return None
    if target is None or minimum is None or minimum > target:
        return None
    return metric, target, minimum


def _evidence_compatibility(row: Mapping[str, Any]) -> _EvidenceCompatibility | None:
    summary_schema_version = int(row.get("summary_schema_version") or 0)
    analyzer_name = str(row.get("analyzer_name") or "").strip()
    analyzer_version = str(row.get("analyzer_version") or "").strip()
    analyzer_runtime_version = str(row.get("analyzer_runtime_version") or "").strip()
    analyzer_policy_digest = str(row.get("policy_hash") or "").strip()
    if (
        summary_schema_version <= 0
        or not analyzer_name
        or not analyzer_version
        or not analyzer_runtime_version
        or not analyzer_policy_digest
    ):
        return None
    return _EvidenceCompatibility(
        summary_schema_version=summary_schema_version,
        analyzer_name=analyzer_name,
        analyzer_version=analyzer_version,
        analyzer_runtime_version=analyzer_runtime_version,
        analyzer_policy_digest=analyzer_policy_digest,
    )


def _compatibility_sort_key(cohort: _EvidenceCompatibility) -> tuple[object, ...]:
    return (
        cohort.summary_schema_version,
        cohort.analyzer_name,
        cohort.analyzer_version,
        cohort.analyzer_runtime_version,
        cohort.analyzer_policy_digest,
    )


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFC", normalize_scope_prefix(value)).casefold()


def _finite_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed
