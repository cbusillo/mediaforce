from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import unicodedata
from typing import Any, Callable, Iterator, Mapping

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.file_integrity import (
    FileIntegrityError,
    MacOSFileIntegrityGuard,
)
from mediaforce.core.type_defs import object_dict
from mediaforce.core.utils import (
    descriptor_content_version_fingerprint,
    descriptor_sha256,
    file_stat_signature,
)
from mediaforce.encoding.helpers import resolve_item_source_path
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.hosts.config import host_media_access_for_host
from mediaforce.library.media_scopes import (
    media_group_scope_for_rel_path,
    normalize_scope_prefix,
    tv_series_scope_for_rel_path,
)
from mediaforce.library.probe import probe_evidence
from mediaforce.tuning.av1_trait_projection import (
    AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
    AV1ColdStartTraitProjectionError,
    project_av1_cold_start_fingerprint_summary,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1_VALIDATION_PARTITION_CONTRACT_VERSION,
    AV1ValidationPartitionExpectations,
    AV1ValidationPartitionError,
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


@dataclass(frozen=True, slots=True)
class _GuardedPartitionSource:
    source: AV1ValidationPartitionSource
    path: Path
    descriptor: int
    guard: MacOSFileIntegrityGuard
    source_sha256: str
    initial_stat_signature: tuple[int, int, int, int, int]


class AV1ValidationPartitionSourceSHA256Session:
    def __init__(
            self,
            *,
            resolve: Callable[[AV1ValidationPartitionSource], str],
            verify: Callable[[], None],
            assert_quiet: Callable[[], None],
    ) -> None:
        self._resolve = resolve
        self._verify = verify
        self._assert_quiet = assert_quiet
        self._verified = False

    def __call__(self, source: AV1ValidationPartitionSource) -> str:
        if self._verified:
            raise AV1ValidationPartitionError(
                "AV1 partition source cohort is already verified"
            )
        return self._resolve(source)

    def verify(self) -> None:
        if self._verified:
            raise AV1ValidationPartitionError(
                "AV1 partition source cohort is already verified"
            )
        self._verify()
        self._verified = True

    def assert_verified(self) -> None:
        if not self._verified:
            raise AV1ValidationPartitionError(
                "AV1 partition source cohort was not verified before publication"
            )

    @property
    def verified(self) -> bool:
        return self._verified

    def assert_quiet(self) -> None:
        self.assert_verified()
        self._assert_quiet()


@contextmanager
def av1_validation_partition_source_sha256_resolver(
    connection: DBClient,
    *,
    config: MediaforceConfig,
    verify_evidence: bool = False,
) -> Iterator[AV1ValidationPartitionSourceSHA256Session]:
    digest_by_item_id: dict[int, str] = {}
    guarded_sources: list[_GuardedPartitionSource] = []

    def resolve(source: AV1ValidationPartitionSource) -> str:
        cached = digest_by_item_id.get(source.local_item_id)
        if cached is not None:
            return cached
        row = connection.execute(
            select(
                library_items.c.id,
                library_items.c.source_path,
                library_items.c.rel_path,
                library_items.c.media_root,
                library_items.c.content_version_fingerprint,
                library_items.c.size_bytes,
            ).where(library_items.c.id == source.local_item_id)
        ).mappings().one_or_none()
        if row is None:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source is no longer in the inventory"
            )
        source_path = resolve_item_source_path(
            config,
            dict(row),
            host=None,
            host_media_access_for_host=host_media_access_for_host,
        )
        if not hasattr(os, "O_NOFOLLOW"):
            raise AV1ValidationPartitionError(
                "AV1 partition secure source hashing is unavailable"
            )
        source_descriptor = -1
        source_guard: MacOSFileIntegrityGuard | None = None
        try:
            source_descriptor = os.open(
                source_path,
                os.O_RDONLY | os.O_NOFOLLOW,
            )
            source_guard = MacOSFileIntegrityGuard(
                path=source_path,
                descriptor=source_descriptor,
                require_single_link=False,
            )
            source_path = source_guard.path
            source_initial = os.fstat(source_descriptor)
            source_path_initial = source_path.lstat()
            expected_size_bytes = int(row.get("size_bytes") or 0)
            expected_content_version = str(
                row.get("content_version_fingerprint") or ""
            )
            if (
                expected_content_version != source.source_identity
                or expected_size_bytes <= 0
                or not stat.S_ISREG(source_initial.st_mode)
                or not stat.S_ISREG(source_path_initial.st_mode)
                or (source_initial.st_dev, source_initial.st_ino)
                != (source_path_initial.st_dev, source_path_initial.st_ino)
                or source_initial.st_size != expected_size_bytes
                or descriptor_content_version_fingerprint(
                    source_descriptor,
                    size_bytes=source_initial.st_size,
                ) != source.source_identity
            ):
                raise AV1ValidationPartitionError(
                    "AV1 partition selected source drifted before full hashing"
                )
            source_sha256 = descriptor_sha256(source_descriptor)
            source_guard.assert_quiet()
            if verify_evidence:
                summary = probe_evidence(
                    source_path,
                    MEDIA_FINGERPRINT_EVIDENCE_KIND,
                )
                summary_json = json.dumps(
                    summary,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                summary_sha256 = (
                    f"sha256:{hashlib.sha256(
                        summary_json.encode(),
                        usedforsecurity=False,
                    ).hexdigest()}"
                )
                if summary_sha256 != source.evidence_summary_sha256:
                    raise AV1ValidationPartitionError(
                        "AV1 partition selected source evidence does not replay from its frozen bytes"
                    )
                source_guard.assert_quiet()
            source_final = os.fstat(source_descriptor)
            source_path_final = source_path.lstat()
            if (
                file_stat_signature(source_initial)
                != file_stat_signature(source_final)
                or not stat.S_ISREG(source_path_final.st_mode)
                or (source_final.st_dev, source_final.st_ino)
                != (source_path_final.st_dev, source_path_final.st_ino)
                or descriptor_content_version_fingerprint(
                    source_descriptor,
                    size_bytes=source_final.st_size,
                ) != source.source_identity
            ):
                raise AV1ValidationPartitionError(
                    "AV1 partition selected source changed during full hashing"
                )
            guarded_sources.append(_GuardedPartitionSource(
                source=source,
                path=source_path,
                descriptor=source_descriptor,
                guard=source_guard,
                source_sha256=source_sha256,
                initial_stat_signature=file_stat_signature(source_initial),
            ))
            source_descriptor = -1
            source_guard = None
        except FileIntegrityError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source integrity monitoring failed"
            ) from exc
        except AV1ValidationPartitionError:
            raise
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
        ) as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source could not be securely hashed"
            ) from exc
        finally:
            if source_guard is not None:
                source_guard.close()
            if source_descriptor >= 0:
                os.close(source_descriptor)
        digest_by_item_id[source.local_item_id] = source_sha256
        return source_sha256

    def assert_quiet() -> None:
        try:
            for guarded in guarded_sources:
                guarded.guard.assert_quiet()
                source_final = os.fstat(guarded.descriptor)
                source_path_final = guarded.path.lstat()
                if (
                    guarded.initial_stat_signature
                    != file_stat_signature(source_final)
                    or not stat.S_ISREG(source_path_final.st_mode)
                    or (source_final.st_dev, source_final.st_ino)
                    != (source_path_final.st_dev, source_path_final.st_ino)
                    or descriptor_content_version_fingerprint(
                        guarded.descriptor,
                        size_bytes=source_final.st_size,
                    ) != guarded.source.source_identity
                ):
                    raise AV1ValidationPartitionError(
                        "AV1 partition selected source changed during cohort validation"
                    )
        except FileIntegrityError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source integrity monitoring failed"
            ) from exc
        except OSError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source could not be securely hashed"
            ) from exc

    def verify() -> None:
        try:
            for guarded in guarded_sources:
                guarded.guard.assert_quiet()
                if descriptor_sha256(guarded.descriptor) != guarded.source_sha256:
                    raise AV1ValidationPartitionError(
                        "AV1 partition selected source changed during cohort validation"
                    )
            assert_quiet()
        except FileIntegrityError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source integrity monitoring failed"
            ) from exc
        except OSError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition selected source could not be securely hashed"
            ) from exc

    session = AV1ValidationPartitionSourceSHA256Session(
        resolve=resolve,
        verify=verify,
        assert_quiet=assert_quiet,
    )
    try:
        try:
            yield session
        except BaseException as exc:
            if session.verified:
                try:
                    session.assert_quiet()
                except BaseException as integrity_exc:
                    raise integrity_exc from exc
            raise
        else:
            session.assert_quiet()
    finally:
        for guarded in reversed(guarded_sources):
            guarded.guard.close()
            os.close(guarded.descriptor)


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
        raise AV1ValidationPartitionError(
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
        raise AV1ValidationPartitionError(
            "AV1 partition default quality contract is incomplete"
        )
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
