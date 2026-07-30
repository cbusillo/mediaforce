from __future__ import annotations

from contextlib import contextmanager, ExitStack
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import errno
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
from typing import Any, Callable, Iterator, Mapping, cast
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.config import MediaforceConfig, load_config, migrate_config_state
from mediaforce.core.db import DBClient, open_db, open_readonly_db
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.file_integrity import (
    FileIntegrityError,
    MACOS_FILE_INTEGRITY_CONTRACT_VERSION,
    MacOSFileIntegrityGuard,
    assert_macos_file_integrity_capability,
    ensure_owner_only_directory,
    open_stable_directory,
    probe_macos_file_integrity,
    stable_absolute_path,
)
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, mapping_dict, object_dict, object_list
from mediaforce.core.utils import (
    descriptor_content_version_fingerprint,
    descriptor_sha256,
    file_stat_signature,
)
from mediaforce.encoding.helpers import resolve_item_source_path
from mediaforce.encoding.quality import quality_toolchain_identity, run_sample_encode, select_quality_metric
from mediaforce.execution import (
    build_svt_params,
    detect_video_crop,
    effective_video_preset,
    resolve_stream_budget_ledger,
    search_quality_for_source,
)
from mediaforce.library.media_scopes import media_group_scope_for_rel_path
from mediaforce.library.planner import build_manifest_item
from mediaforce.hosts.config import host_media_access_for_host
from mediaforce.review import (
    default_review_timestamps,
    encode_preview_clips,
    generate_compare_clips_from_review_pairs,
    recommend_review_moments,
    recommend_review_timestamps,
    render_source_review_clips,
    review_moment_payload,
)
from mediaforce.reviewing.artifact_identity import (
    reviewed_artifact_clip_payload_from_descriptor,
    reviewed_artifact_fingerprint_from_payloads,
)
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
    AV1ValidationDerivationAttempt,
    AV1ValidationDerivationAttemptStatus,
    AV1ValidationDerivationCandidateLockEnvelope,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationTerminalRecord,
    assert_av1_validation_derivation_authorization_active,
    assert_av1_validation_derivation_source_commitments,
    av1_validation_derivation_plan_source_commitment,
    av1_validation_derivation_statistics_contract_sha256,
    build_av1_validation_derivation_attempt,
    build_av1_validation_derivation_terminal_record,
    evaluate_av1_validation_derivation_candidate,
    ensure_av1_validation_derivation_terminal_intent,
    ensure_av1_validation_derivation_terminal_record,
    ensure_av1_validation_derivation_verdict_claim,
    load_av1_validation_derivation_assignment_claims,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelopes,
    load_av1_validation_derivation_terminal_intents,
    load_av1_validation_derivation_terminal_records,
    load_av1_validation_derivation_verdict_claims,
    load_av1_validation_derivation_verdict_intent,
    _finalize_and_write_av1_validation_derivation_candidate_lock,
    _load_av1_validation_derivation_candidate_lock_envelope,
    _load_verified_av1_validation_derivation_candidate_lock,
    resolve_av1_validation_derivation_verdict_intent,
    validate_av1_validation_derivation_artifact_root_binding,
    validate_av1_validation_derivation_attempt_binding,
    validate_av1_validation_derivation_plan_binding,
    write_av1_validation_derivation_attempt,
    write_av1_validation_derivation_assignment_claim,
    write_av1_validation_derivation_terminal_record,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionAssignment,
    AV1ValidationPartitionError,
    AV1ValidationPrivatePartition,
    validate_av1_validation_partition_current_inputs,
    validate_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    AV1ValidationPartitionSourceSHA256Session,
    av1_validation_partition_source_sha256_resolver,
    load_av1_validation_partition_inventory,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    assert_preregistered_av1_validation_manifest_v2,
)
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.tuning.stream_budget import stream_budget_projection_blocker
from mediaforce.tuning.target_size_search import TargetSizeSearchError
from mediaforce.tuning.content_intent_observations import (
    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
    ContentIntentObservationConflictError,
    ContentIntentBoundaryObservation,
    append_content_intent_boundary_observation,
    build_visual_content_intent_observation,
    content_intent_boundary_observation_from_values,
    load_current_content_intent_boundary_observations,
)
from mediaforce.web.runtime.calibration_runtime import (
    CalibrationRunDeps,
    restore_staged_artifact,
    run_sampled_calibration,
    snapshot_staged_artifact,
)
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    exclusive_mediaforce_runtime_lock,
    mediaforce_runtime_lock_path,
    mediaforce_runtime_lock_path_for_web_state_dir,
)


AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES = 5 * 1024 ** 3
_AV1_VALIDATION_DERIVATION_SOURCE_SNAPSHOT_DIRECTORY = "source-snapshots"
_AV1_VALIDATION_DERIVATION_COPY_CHUNK_BYTES = 4 * 1024 * 1024
_AV1_VALIDATION_DERIVATION_HOST_DATA = {
    "key": "av1-derivation-local",
    "label": "AV1 derivation local",
    "mode": "local",
    "media_access": "direct",
}
_AV1_VALIDATION_DERIVATION_IMPLEMENTATION_FIXED_FILES = (
    "pyproject.toml",
    "scripts/verify_av1_cold_start_preregistration.py",
    "uv.lock",
)


@dataclass(frozen=True, slots=True)
class _PinnedDerivationSource:
    path: Path
    content_version_fingerprint: str
    content_sha256: str
    size_bytes: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _AV1ValidationDerivationAssignmentPreflight:
    assignment: AV1ValidationPartitionAssignment
    artifact_root: Path
    clock: Callable[[], str]


@dataclass(frozen=True, slots=True)
class _DerivationReviewClip:
    role: str
    path: Path
    timestamp_seconds: float
    duration_seconds: float


@dataclass(slots=True)
class _HeldDerivationReviewClip:
    clip: _DerivationReviewClip
    canonical_path: Path
    descriptor: int
    guard: MacOSFileIntegrityGuard | None = None


class _AV1ValidationDerivationVerdictSafetyStop(AV1ValidationDerivationError):
    pass


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("AV1 derivation source snapshot write did not progress")
        written += count


def _available_bytes_for_descriptor(descriptor: int) -> int:
    filesystem_info = os.fstatvfs(descriptor)
    block_size = filesystem_info.f_frsize or filesystem_info.f_bsize
    return int(filesystem_info.f_bavail) * int(block_size)


@contextmanager
def _owner_only_umask() -> Iterator[None]:
    previous_umask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous_umask)


def _prepare_owner_only_directory(path: Path, *, label: str) -> Path:
    try:
        normalized, descriptor = ensure_owner_only_directory(path)
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 derivation {label} directory could not be safely prepared"
        ) from exc
    os.close(descriptor)
    return normalized


def _open_owner_only_directory_descriptor(path: Path, *, label: str) -> int:
    try:
        _, descriptor = ensure_owner_only_directory(path)
        return descriptor
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 derivation {label} directory could not be safely prepared"
        ) from exc


def _bind_owner_only_directory_descriptor(path: Path, *, label: str) -> int:
    try:
        _, descriptor = open_stable_directory(
            path,
            require_owner_only=True,
        )
        return descriptor
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 derivation {label} directory could not be safely bound"
        ) from exc


def _retained_source_snapshot_name(assignment_id: str) -> str:
    if (
        not assignment_id
        or assignment_id in {".", ".."}
        or Path(assignment_id).name != assignment_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation assignment ID is invalid for retained snapshot storage"
        )
    return f"{assignment_id}.source-media"


def _assert_pinned_source_guard_quiet(
        guard: MacOSFileIntegrityGuard,
) -> None:
    try:
        guard.assert_quiet()
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation pinned source changed during media execution"
        ) from exc


def _validate_pinned_derivation_source(
        pinned: _PinnedDerivationSource,
        *,
        descriptor: int,
        process_controller: ManagedProcessController,
) -> None:
    descriptor_info = os.fstat(descriptor)
    try:
        path_info = pinned.path.lstat()
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation pinned source path is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(descriptor_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or path_info.st_uid != os.getuid()
        or path_info.st_mode & 0o377
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (pinned.device, pinned.inode)
        or (path_info.st_dev, path_info.st_ino)
        != (pinned.device, pinned.inode)
        or descriptor_info.st_nlink != 1
        or path_info.st_nlink != 1
        or descriptor_info.st_size != pinned.size_bytes
        or descriptor_content_version_fingerprint(
            descriptor,
            size_bytes=pinned.size_bytes,
        ) != pinned.content_version_fingerprint
        or descriptor_sha256(
            descriptor,
            check_cancelled=process_controller.throw_if_cancelled,
        ) != pinned.content_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation pinned source changed during media execution"
        )


@contextmanager
def _pinned_derivation_source(
        *,
        artifact_root: Path,
        assignment_id: str,
        source_path: Path,
        expected_content_version_fingerprint: str,
        expected_source_sha256: str,
        expected_size_bytes: int,
        process_controller: ManagedProcessController,
) -> Iterator[_PinnedDerivationSource]:
    snapshot_root = (
        artifact_root / _AV1_VALIDATION_DERIVATION_SOURCE_SNAPSHOT_DIRECTORY
    )
    snapshot_root_descriptor = -1
    source_descriptor = -1
    snapshot_write_descriptor = -1
    snapshot_descriptor = -1
    snapshot_guard: MacOSFileIntegrityGuard | None = None
    try:
        snapshot_name = _retained_source_snapshot_name(assignment_id)
        snapshot_path = snapshot_root / snapshot_name
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_descriptor = os.open(source_path, source_flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation source could not be opened without following links"
            ) from exc
        source_initial = os.fstat(source_descriptor)
        try:
            source_path_info = source_path.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation source path could not be verified"
            ) from exc
        if (
            not stat.S_ISREG(source_initial.st_mode)
            or not stat.S_ISREG(source_path_info.st_mode)
            or (source_initial.st_dev, source_initial.st_ino)
            != (source_path_info.st_dev, source_path_info.st_ino)
            or source_initial.st_size != expected_size_bytes
            or expected_size_bytes <= 0
            or descriptor_content_version_fingerprint(
                source_descriptor,
                size_bytes=source_initial.st_size,
            ) != expected_content_version_fingerprint
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation source bytes drifted from the frozen reservation"
            )
        snapshot_root_descriptor = _open_owner_only_directory_descriptor(
            snapshot_root,
            label="source-snapshot",
        )
        if (
            _available_bytes_for_descriptor(snapshot_root_descriptor)
            < source_initial.st_size + AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
        ):
            raise OSError("AV1 derivation source snapshot storage floor is not available")
        snapshot_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        snapshot_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            snapshot_write_descriptor = os.open(
                snapshot_name,
                snapshot_flags,
                0o400,
                dir_fd=snapshot_root_descriptor,
            )
        except FileExistsError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation retained source snapshot already exists for this assignment"
            ) from exc
        created_snapshot_info = os.fstat(snapshot_write_descriptor)
        current_snapshot_info = os.stat(
            snapshot_name,
            dir_fd=snapshot_root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(created_snapshot_info.st_mode)
            or created_snapshot_info.st_uid != os.getuid()
            or created_snapshot_info.st_nlink != 1
            or created_snapshot_info.st_mode & 0o077
            or (created_snapshot_info.st_dev, created_snapshot_info.st_ino)
            != (current_snapshot_info.st_dev, current_snapshot_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation retained source snapshot was not created safely"
            )
        digest = hashlib.sha256()
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            process_controller.throw_if_cancelled()
            chunk = os.read(
                source_descriptor,
                _AV1_VALIDATION_DERIVATION_COPY_CHUNK_BYTES,
            )
            if not chunk:
                break
            digest.update(chunk)
            _write_all(snapshot_write_descriptor, chunk)
        os.fsync(snapshot_write_descriptor)
        source_final = os.fstat(source_descriptor)
        try:
            source_path_final = source_path.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation source path changed during snapshot creation"
            ) from exc
        snapshot_info = os.fstat(snapshot_write_descriptor)
        copied_sha256 = f"sha256:{digest.hexdigest()}"
        if (
            file_stat_signature(source_initial)
            != file_stat_signature(source_final)
            or (source_final.st_dev, source_final.st_ino)
            != (source_path_final.st_dev, source_path_final.st_ino)
            or not stat.S_ISREG(source_path_final.st_mode)
            or snapshot_info.st_size != source_initial.st_size
            or descriptor_content_version_fingerprint(
                snapshot_write_descriptor,
                size_bytes=snapshot_info.st_size,
            ) != expected_content_version_fingerprint
            or descriptor_sha256(
                snapshot_write_descriptor,
                check_cancelled=process_controller.throw_if_cancelled,
            ) != copied_sha256
            or copied_sha256 != expected_source_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation source changed while its snapshot was created"
            )
        os.fchmod(snapshot_write_descriptor, 0o400)
        os.fsync(snapshot_write_descriptor)
        snapshot_descriptor = os.open(
            snapshot_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=snapshot_root_descriptor,
        )
        reopened_snapshot_info = os.fstat(snapshot_descriptor)
        current_snapshot_info = os.stat(
            snapshot_name,
            dir_fd=snapshot_root_descriptor,
            follow_symlinks=False,
        )
        if (
            (reopened_snapshot_info.st_dev, reopened_snapshot_info.st_ino)
            != (snapshot_info.st_dev, snapshot_info.st_ino)
            or (current_snapshot_info.st_dev, current_snapshot_info.st_ino)
            != (snapshot_info.st_dev, snapshot_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation source snapshot changed before monitoring"
            )
        try:
            snapshot_guard = MacOSFileIntegrityGuard(
                path=snapshot_path,
                descriptor=snapshot_descriptor,
                require_single_link=True,
            )
        except FileIntegrityError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation source snapshot monitoring is unavailable"
            ) from exc
        os.close(snapshot_write_descriptor)
        snapshot_write_descriptor = -1
        pinned = _PinnedDerivationSource(
            path=snapshot_guard.path,
            content_version_fingerprint=expected_content_version_fingerprint,
            content_sha256=copied_sha256,
            size_bytes=int(snapshot_info.st_size),
            device=int(snapshot_info.st_dev),
            inode=int(snapshot_info.st_ino),
        )
        _validate_pinned_derivation_source(
            pinned,
            descriptor=snapshot_descriptor,
            process_controller=process_controller,
        )
        _assert_pinned_source_guard_quiet(snapshot_guard)
        try:
            yield pinned
        finally:
            _assert_pinned_source_guard_quiet(snapshot_guard)
            _validate_pinned_derivation_source(
                pinned,
                descriptor=snapshot_descriptor,
                process_controller=process_controller,
            )
            _assert_pinned_source_guard_quiet(snapshot_guard)
    finally:
        if snapshot_guard is not None:
            snapshot_guard.close()
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if snapshot_write_descriptor >= 0:
            os.close(snapshot_write_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if snapshot_root_descriptor >= 0:
            os.close(snapshot_root_descriptor)


def av1_validation_derivation_runtime_context_sha256(
        config: MediaforceConfig,
) -> str:
    payload = {
        "db_path": str(config.paths.db_path.expanduser().resolve()),
        "review_dir": str(config.paths.review_dir.expanduser().resolve()),
        "web_state_dir": str(config.paths.web_state_dir.expanduser().resolve()),
    }
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _av1_validation_derivation_implementation_sha256() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        file_digests = {
            relative_path: f"sha256:{hashlib.sha256(
                path.read_bytes()
            ).hexdigest()}"
            for relative_path, path in (
                _av1_validation_derivation_implementation_files(repository_root)
            )
        }
        python_sha256 = f"sha256:{hashlib.sha256(
            Path(sys.executable).resolve(strict=True).read_bytes()
        ).hexdigest()}"
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation implementation identity is unavailable"
        ) from exc
    return f"sha256:{hashlib.sha256(canonical_json_bytes({
        'files': file_digests,
        'python_sha256': python_sha256,
        'python_version': platform.python_version(),
    })).hexdigest()}"


def _av1_validation_derivation_implementation_files(
        repository_root: Path,
) -> tuple[tuple[str, Path], ...]:
    package_root = repository_root / "mediaforce"
    paths = list(package_root.rglob("*"))
    paths.extend(
        repository_root / relative_path
        for relative_path in _AV1_VALIDATION_DERIVATION_IMPLEMENTATION_FIXED_FILES
    )
    files: list[tuple[str, Path]] = []
    try:
        for path in paths:
            if (
                "__pycache__" in path.parts
                or path.name == ".DS_Store"
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            path_info = path.lstat()
            if stat.S_ISLNK(path_info.st_mode):
                raise AV1ValidationDerivationError(
                    "AV1 derivation implementation contains a symbolic link"
                )
            if not stat.S_ISREG(path_info.st_mode):
                continue
            files.append((
                path.relative_to(repository_root).as_posix(),
                path,
            ))
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation implementation identity is unavailable"
        ) from exc
    return tuple(sorted(files))


def av1_validation_derivation_execution_environment_sha256(
        *,
        quality_metric: str,
) -> str:
    try:
        assert_macos_file_integrity_capability()
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation source-integrity monitoring is unavailable"
        ) from exc
    toolchain = quality_toolchain_identity(quality_metric=quality_metric)
    if toolchain.get("status") != "available":
        raise AV1ValidationDerivationError(
            "AV1 derivation execution toolchain is unavailable"
        )
    payload = {
        "machine": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "node": platform.node(),
        },
        "implementation_sha256": _av1_validation_derivation_implementation_sha256(),
        "quality_metric": quality_metric.strip().casefold(),
        "source_integrity_contract": MACOS_FILE_INTEGRITY_CONTRACT_VERSION,
        "toolchain": toolchain,
    }
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def assert_av1_validation_derivation_runtime_context(
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> None:
    if (
        av1_validation_derivation_runtime_context_sha256(config)
        != plan.runtime_context_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation config does not match the immutable runtime context"
        )


def assert_av1_validation_derivation_execution_environment(
        plan: AV1ValidationDerivationPlan,
) -> None:
    quality_metrics = {assignment.quality_metric for assignment in plan.assignments}
    if len(quality_metrics) != 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation plan quality metric is not uniform"
        )
    if (
        av1_validation_derivation_execution_environment_sha256(
            quality_metric=next(iter(quality_metrics)),
        )
        != plan.execution_environment_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation execution environment drifted after authorization"
        )


def assert_av1_validation_derivation_execution_contract(
        manifest: AV1ValidationManifestV2,
        plan: AV1ValidationDerivationPlan,
) -> None:
    assert_av1_validation_derivation_execution_environment(plan)
    if (
        av1_validation_derivation_statistics_contract_sha256(manifest)
        != plan.statistics_contract_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation statistics contract drifted after authorization"
        )


def av1_validation_derivation_artifact_root(
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    assert_av1_validation_derivation_runtime_context(config, plan)
    return _av1_validation_derivation_state_root(config, plan)


def _av1_validation_derivation_state_root(
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    return (
        config.paths.web_state_dir
        / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
        / plan.partition_id
    )


def load_av1_validation_derivation_sample_item(
        connection: DBClient,
        *,
        config: MediaforceConfig,
        local_item_id: int,
) -> dict[str, Any]:
    row = connection.execute(
        select(library_items).where(library_items.c.id == local_item_id)
    ).mappings().one_or_none()
    if row is None:
        raise AV1ValidationDerivationError("AV1 derivation source is no longer in the library")
    return build_manifest_item(mapping_dict(row), config)


def _load_canonical_av1_validation_derivation_plan(
        *,
        config: MediaforceConfig,
        plan_path: Path,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    plan = load_av1_validation_derivation_plan(plan_path)
    artifact_root = av1_validation_derivation_artifact_root(config, plan)
    if plan_path.expanduser().resolve() != (artifact_root / "plan.json").resolve():
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the config-derived canonical root"
        )
    artifact_root = _validated_av1_validation_derivation_artifact_root(
        config=config,
        plan=plan,
        artifact_root=artifact_root,
    )
    return plan, artifact_root


def _load_current_derivation_observations_from_connection(
        *,
        connection: DBClient,
        records: tuple[AV1ValidationDerivationTerminalRecord, ...],
) -> dict[str, ContentIntentBoundaryObservation]:
    current_rows = load_current_content_intent_boundary_observations(
        connection,
        include_withdrawn=True,
    )
    current_by_id = {
        str(row.get("observation_id") or ""):
        content_intent_boundary_observation_from_values(row)
        for row in current_rows
    }
    current_observations: dict[str, ContentIntentBoundaryObservation] = {}
    for record in records:
        if record.observation is None:
            continue
        observation = current_by_id.get(record.observation.observation_id)
        if observation is not None:
            current_observations[record.assignment_id] = observation
    return current_observations


def _assert_derivation_terminal_observations_current(
        *,
        connection: DBClient,
        records: tuple[AV1ValidationDerivationTerminalRecord, ...],
) -> None:
    if not records:
        return
    current_observations = _load_current_derivation_observations_from_connection(
        connection=connection,
        records=records,
    )
    for record in records:
        projection = record.observation
        if projection is None:
            continue
        current = current_observations.get(record.assignment_id)
        if (
            current is None
            or current.observation_id != projection.observation_id
            or f"sha256:{current.payload_sha256}"
            != projection.observation_payload_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation observed terminal requires an idempotent verdict retry"
            )


def load_current_av1_validation_derivation_observations(
        *,
        config_path: Path,
        config: MediaforceConfig | None = None,
        records: tuple[AV1ValidationDerivationTerminalRecord, ...],
) -> dict[str, ContentIntentBoundaryObservation]:
    current_config = config or load_config(config_path)
    with open_readonly_db(current_config.paths.db_path) as connection:
        return _load_current_derivation_observations_from_connection(
            connection=connection,
            records=records,
        )


def build_av1_validation_derivation_calibration_deps() -> CalibrationRunDeps:
    staged_columns = tuple(column.name for column in staged_artifacts.columns)
    return CalibrationRunDeps(
        now_iso=_now_iso,
        ensure_sample_host_ready=_unused,
        load_job_state=_unused,
        sample_item=_unused,
        save_job_state=_unused,
        update_job_telemetry=_unused,
        calibration_job_compatibility_key=_unused,
        calibration_heartbeat_seconds=5.0,
        save_calibration_state=_unused,
        record_run_verdict=_unused,
        summarize_calibration_result=_unused,
        calibration_mode_for_action=_unused,
        effective_video_preset=effective_video_preset,
        search_quality_for_source=search_quality_for_source,
        run_sample_encode=run_sample_encode,
        detect_video_crop=detect_video_crop,
        recommend_review_timestamps=recommend_review_timestamps,
        recommend_review_moments=recommend_review_moments,
        review_moment_payload=review_moment_payload,
        default_review_timestamps=default_review_timestamps,
        encode_preview_clips=encode_preview_clips,
        render_source_review_clips=render_source_review_clips,
        generate_compare_clips_from_review_pairs=generate_compare_clips_from_review_pairs,
        resolve_stream_budget_ledger=resolve_stream_budget_ledger,
        build_svt_params=build_svt_params,
        review_url=_review_url,
        encode_manifest_items=_unused,
        validate_manifest_items=_unused,
        generate_compare_clips=_unused,
        staged_artifact_columns=staged_columns,
        quality_toolchain_identity=quality_toolchain_identity,
        select_quality_metric=select_quality_metric,
        plan_av1_cold_start=None,
    )


@contextmanager
def _live_av1_validation_derivation_source_commitments(
        *,
        config: MediaforceConfig,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        plan: AV1ValidationDerivationPlan,
        terminal_records: tuple[AV1ValidationDerivationTerminalRecord, ...],
) -> Iterator[AV1ValidationPartitionSourceSHA256Session]:
    with ExitStack() as source_stack:
        with open_readonly_db(config.paths.db_path) as connection:
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
            _assert_derivation_terminal_observations_current(
                connection=connection,
                records=terminal_records,
            )
            source_sha256_resolver = source_stack.enter_context(
                av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=config,
                )
            )
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            assert_av1_validation_derivation_source_commitments(
                plan,
                resolver=source_sha256_resolver,
            )
            source_sha256_resolver.verify()
        yield source_sha256_resolver


def run_av1_validation_derivation_assignment(
        *,
        config_path: Path,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        plan: AV1ValidationDerivationPlan,
        assignment_id: str,
        attempts_directory: Path,
        terminal_records_directory: Path,
        process_controller: ManagedProcessController | None = None,
        deps: CalibrationRunDeps | None = None,
        now_iso: Callable[[], str] | None = None,
) -> AV1ValidationDerivationAttempt:
    config = load_config(config_path)
    try:
        with exclusive_mediaforce_runtime_lock(
            config,
            owner_payload={
                "purpose": "av1-derivation",
                "plan_id": plan.plan_id,
                "assignment_id": assignment_id,
            },
        ):
            migrate_config_state(config)
            return _run_av1_validation_derivation_assignment_locked(
                config=config,
                manifest=manifest,
                partition=partition,
                token_key=token_key,
                plan=plan,
                assignment_id=assignment_id,
                attempts_directory=attempts_directory,
                terminal_records_directory=terminal_records_directory,
                process_controller=process_controller,
                deps=deps,
                now_iso=now_iso,
            )
    except MediaforceRuntimeBusyError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation requires the Mediaforce runtime to be paused"
        ) from exc


def _run_av1_validation_derivation_assignment_locked(
        *,
        config: MediaforceConfig,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        plan: AV1ValidationDerivationPlan,
        assignment_id: str,
        attempts_directory: Path,
        terminal_records_directory: Path,
        process_controller: ManagedProcessController | None = None,
        deps: CalibrationRunDeps | None = None,
        now_iso: Callable[[], str] | None = None,
        _preflight: _AV1ValidationDerivationAssignmentPreflight | None = None,
        _source_commitment_guard: Callable[[], None] | None = None,
) -> AV1ValidationDerivationAttempt:
    if _preflight is None:
        assert_preregistered_av1_validation_manifest_v2(manifest)
        assignment = next(
            (item for item in plan.assignments if item.assignment_id == assignment_id),
            None,
        )
        if assignment is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment is not authorized"
            )
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        validate_av1_validation_derivation_plan_binding(
            plan=plan,
            partition=partition,
        )
        artifact_root = attempts_directory.expanduser().resolve().parent
        if (
            attempts_directory.resolve()
            != (artifact_root / "attempts").resolve()
            or terminal_records_directory.resolve()
            != (artifact_root / "terminal-records").resolve()
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation runtime artifacts must use the partition-global canonical directory"
            )
        artifact_root = _validated_av1_validation_derivation_artifact_root(
            config=config,
            plan=plan,
            artifact_root=artifact_root,
        )
        attempts_directory = artifact_root / "attempts"
        terminal_records_directory = artifact_root / "terminal-records"
        if load_av1_validation_derivation_plan(artifact_root / "plan.json") != plan:
            raise AV1ValidationDerivationError(
                "AV1 derivation runtime plan does not match the frozen artifact root"
            )
        clock = now_iso or _now_iso
        recovery_completed_at = clock()
        interrupted_state_recovered = _recover_interrupted_derivation_state(
            plan=plan,
            partition=partition,
            artifact_root=artifact_root,
            attempts_directory=attempts_directory,
            terminal_records_directory=terminal_records_directory,
            completed_at=recovery_completed_at,
        )
        if interrupted_state_recovered:
            raise AV1ValidationDerivationError(
                "AV1 derivation interrupted state was terminalized; retry the next canonical assignment"
            )
        assert_av1_validation_derivation_execution_contract(manifest, plan)
        if stable_absolute_path(
            av1_validation_derivation_artifact_root(config, plan)
        ) != artifact_root:
            raise AV1ValidationDerivationError(
                "AV1 derivation runtime artifacts drifted from the immutable runtime context"
            )
        terminal_records = (
            load_av1_validation_derivation_terminal_records(
                terminal_records_directory
            )
            if terminal_records_directory.exists()
            else ()
        )
        preflight = _AV1ValidationDerivationAssignmentPreflight(
            assignment=assignment,
            artifact_root=artifact_root,
            clock=clock,
        )
        with _live_av1_validation_derivation_source_commitments(
            config=config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan=plan,
            terminal_records=terminal_records,
        ) as source_sha256_session:
            return _run_av1_validation_derivation_assignment_locked(
                config=config,
                manifest=manifest,
                partition=partition,
                token_key=token_key,
                plan=plan,
                assignment_id=assignment_id,
                attempts_directory=attempts_directory,
                terminal_records_directory=terminal_records_directory,
                process_controller=process_controller,
                deps=deps,
                now_iso=now_iso,
                _preflight=preflight,
                _source_commitment_guard=source_sha256_session.assert_quiet,
            )
    assignment = _preflight.assignment
    artifact_root = _preflight.artifact_root
    clock = _preflight.clock
    if _source_commitment_guard is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation source commitments are not live"
        )
    source = next(
        (item for item in partition.inventory_sources if item.local_item_id == assignment.local_item_id),
        None,
    )
    if source is None:
        raise AV1ValidationDerivationError("AV1 derivation source is absent from the partition")
    source_commitment = av1_validation_derivation_plan_source_commitment(
        plan,
        assignment.assignment_id,
    )
    try:
        probe_macos_file_integrity(artifact_root)
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation source-integrity probe failed before assignment claim"
        ) from exc
    _assert_next_assignment(
        plan=plan,
        assignment_id=assignment.assignment_id,
        attempts_directory=attempts_directory,
        terminal_records_directory=terminal_records_directory,
    )
    started_at = clock()
    assert_av1_validation_derivation_authorization_active(plan, at=started_at)
    _source_commitment_guard()
    write_av1_validation_derivation_assignment_claim(
        attempts_directory,
        assignment_id=assignment.assignment_id,
        plan_id=plan.plan_id,
        authorization_id=plan.authorization.authorization_id,
        claimed_at=started_at,
    )
    controller = process_controller or ManagedProcessController()
    run_deps = deps or build_av1_validation_derivation_calibration_deps()
    attempt: AV1ValidationDerivationAttempt | None = None
    staged_snapshot: dict[str, Any] | None = None
    staged_snapshot_taken = False
    sample_item: dict[str, Any] | None = None
    try:
        if (
            shutil.disk_usage(config.paths.review_dir.parent).free
            < AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
        ):
            raise OSError("AV1 derivation storage safety floor is not available")
        purge_transient_artifacts(config, force=True)
        controller.throw_if_cancelled()
        with open_db(config.paths.db_path) as connection:
            sample_item = load_av1_validation_derivation_sample_item(
                connection,
                config=config,
                local_item_id=assignment.local_item_id,
            )
            _bind_derivation_intent(
                config=config,
                sample_item=sample_item,
                intent_level=assignment.intent_level,
            )
            _validate_bound_sample_item(
                assignment=assignment,
                source_identity=source.source_identity,
                sample_item=sample_item,
            )
            staged_snapshot = snapshot_staged_artifact(
                connection,
                assignment.local_item_id,
                run_deps.staged_artifact_columns,
            )
            staged_snapshot_taken = True
        host_data = dict(_AV1_VALIDATION_DERIVATION_HOST_DATA)
        source_path = resolve_item_source_path(
            config,
            sample_item,
            host=host_data,
            host_media_access_for_host=host_media_access_for_host,
        )
        prefix = _derivation_prefix(config, sample_item)
        calibration_run_id = _run_id(plan.plan_id, assignment.assignment_id, started_at)
        review_root = _prepare_derivation_review_root(artifact_root)
        calibration_config = _config_with_review_directory(config, review_root)
        run_deps = replace(
            run_deps,
            secure_review_artifacts=(
                lambda preview_clips, source_clips, compare_clips: (
                    _secure_and_fingerprint_derivation_review_clips(
                        review_root=review_root,
                        clips=_derivation_review_clips_from_generated_artifacts(
                            preview_clips=preview_clips,
                            source_clips=source_clips,
                            compare_clips=compare_clips,
                        ),
                    )
                )
            ),
        )
        with _pinned_derivation_source(
            artifact_root=artifact_root,
            assignment_id=assignment.assignment_id,
            source_path=source_path,
            expected_content_version_fingerprint=source.source_identity,
            expected_source_sha256=source_commitment.source_sha256,
            expected_size_bytes=source_commitment.source_size_bytes,
            process_controller=controller,
        ) as pinned_source:
            sample_item["source_snapshot_sha256"] = pinned_source.content_sha256
            sample_item["source_snapshot_size_bytes"] = pinned_source.size_bytes
            sample_item["source_snapshot_content_version_fingerprint"] = (
                pinned_source.content_version_fingerprint
            )
            with _owner_only_umask():
                payload, _ = run_sampled_calibration(
                    config=calibration_config,
                    prefix=prefix,
                    action="av1_derivation",
                    host_data=host_data,
                    notes="Bounded AV1 v2 derivation observation",
                    policy=object_dict(sample_item.get("resolved_policy")),
                    seed_metadata=None,
                    sample_item=sample_item,
                    calibration_run_id=calibration_run_id,
                    process_controller=controller,
                    deps=run_deps,
                    source_path_override=pinned_source.path,
                )
        assert_av1_validation_derivation_execution_contract(manifest, plan)
        current_review_fingerprint = _current_derivation_review_artifact_fingerprint(
            review_root=review_root,
            calibration=payload,
        )
        if (
            current_review_fingerprint is None
            or current_review_fingerprint
            != str(payload.get("review_artifact_fingerprint") or "")
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media could not be secured without identity drift"
            )
        payload["job_id"] = f"av1vdjob1_{calibration_run_id}"
        payload["review_media_ready"] = bool(payload.get("preview_clips") and payload.get("source_clips"))
        payload["boundary_review_media_ready"] = payload["review_media_ready"]
        payload["current_review_artifact_fingerprint"] = payload.get("review_artifact_fingerprint")
        completed_at = clock()
        if _authorization_expired(plan, completed_at):
            attempt = _failed_attempt(
                plan=plan,
                partition=partition,
                assignment_id=assignment.assignment_id,
                started_at=started_at,
                completed_at=completed_at,
                status="failed",
                reason_code="authorization_expired",
            )
        else:
            attempt = build_av1_validation_derivation_attempt(
                plan=plan,
                partition=partition,
                assignment_id=assignment.assignment_id,
                started_at=started_at,
                completed_at=completed_at,
                status="review_pending",
                calibration_payload=payload,
            )
    except ProcessCancelledError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="stopped",
            reason_code="operator_stop",
        )
    except TargetSizeSearchError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="failed",
            reason_code="metrics_incomplete",
        )
    except FileNotFoundError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="failed",
            reason_code="media_unavailable",
        )
    except TimeoutError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="failed",
            reason_code="timeout",
        )
    except AV1ValidationDerivationError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="stopped",
            reason_code="safety_stop",
        )
    except OSError:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="failed",
            reason_code="storage_stop",
        )
    except Exception:
        attempt = _failed_attempt(
            plan=plan,
            partition=partition,
            assignment_id=assignment.assignment_id,
            started_at=started_at,
            completed_at=clock(),
            status="failed",
            reason_code="runtime_failure",
        )
    finally:
        cleanup_failed = False
        if staged_snapshot_taken:
            try:
                with open_db(config.paths.db_path) as connection:
                    restore_staged_artifact(
                        connection,
                        assignment.local_item_id,
                        staged_snapshot,
                        run_deps.staged_artifact_columns,
                    )
            except Exception:
                cleanup_failed = True
        try:
            purge_transient_artifacts(config, force=True)
        except Exception:
            cleanup_failed = True
        if cleanup_failed:
            attempt = _failed_attempt(
                plan=plan,
                partition=partition,
                assignment_id=assignment.assignment_id,
                started_at=started_at,
                completed_at=clock(),
                status="failed",
                reason_code="runtime_failure",
            )
    if attempt is None:
        raise AV1ValidationDerivationError("AV1 derivation attempt did not reach a terminal state")
    _source_commitment_guard()
    write_av1_validation_derivation_attempt(attempts_directory, attempt)
    if attempt.status != "review_pending":
        terminal = build_av1_validation_derivation_terminal_record(
            plan=plan,
            partition=partition,
            attempt=attempt,
        )
        _source_commitment_guard()
        write_av1_validation_derivation_terminal_record(
            terminal_records_directory,
            terminal,
        )
    return attempt


def _validated_av1_validation_derivation_artifact_root(
        *,
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
        artifact_root: Path,
) -> Path:
    canonical_artifact_root = stable_absolute_path(artifact_root)
    expected_artifact_root = stable_absolute_path(
        _av1_validation_derivation_state_root(config, plan)
    )
    if canonical_artifact_root != expected_artifact_root:
        raise AV1ValidationDerivationError(
            "AV1 derivation recovery requires the canonical runtime state root"
        )
    canonical_web_state_dir = canonical_artifact_root.parent.parent
    bound_lock_path = mediaforce_runtime_lock_path_for_web_state_dir(
        canonical_web_state_dir
    )
    if bound_lock_path != mediaforce_runtime_lock_path(config):
        raise AV1ValidationDerivationError(
            "AV1 derivation runtime-lock domain drifted"
        )
    validate_av1_validation_derivation_artifact_root_binding(
        canonical_artifact_root,
        plan,
    )
    return canonical_artifact_root


def finalize_av1_validation_derivation_candidate_lock(
        *,
        config_path: Path,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        plan_path: Path,
        cell_plan_id: str,
        now_iso: Callable[[], str] | None = None,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    config = load_config(config_path)
    plan, artifact_root = _load_canonical_av1_validation_derivation_plan(
        config=config,
        plan_path=plan_path,
    )
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    validate_av1_validation_derivation_plan_binding(
        plan=plan,
        partition=partition,
    )
    assert_av1_validation_derivation_execution_contract(manifest, plan)
    clock = now_iso or _now_iso
    try:
        with exclusive_mediaforce_runtime_lock(
            config,
            owner_payload={
                "purpose": "av1-derivation-finalization",
                "plan_id": plan.plan_id,
                "cell_plan_id": cell_plan_id,
            },
        ):
            migrate_config_state(config)
            artifact_root = _validated_av1_validation_derivation_artifact_root(
                config=config,
                plan=plan,
                artifact_root=artifact_root,
            )
            proposal = load_av1_validation_derivation_candidate_proposal(
                artifact_root,
                plan=plan,
                cell_plan_id=cell_plan_id,
            )
            attempts = load_av1_validation_derivation_attempts(
                artifact_root / "attempts"
            )
            records = load_av1_validation_derivation_terminal_records(
                artifact_root / "terminal-records"
            )
            review_claims = load_av1_validation_derivation_review_claims(
                artifact_root,
                plan=plan,
                proposal=proposal,
            )
            review_envelopes = load_av1_validation_derivation_review_envelopes(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claims=review_claims,
            )
            locked_at = _av1_validation_derivation_candidate_locked_at(
                artifact_root=artifact_root,
                plan=plan,
                cell_plan_id=cell_plan_id,
                clock=clock,
            )
            with open_db(config.paths.db_path) as connection, ExitStack() as source_stack:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                inventory = load_av1_validation_partition_inventory(
                    connection,
                    config=config,
                )
                source_sha256_resolver = source_stack.enter_context(
                    av1_validation_partition_source_sha256_resolver(
                        connection,
                        config=config,
                    )
                )
                validate_av1_validation_partition_current_inputs(
                    partition,
                    manifest=manifest,
                    sources=inventory.sources,
                    expectations=inventory.expectations,
                    token_key=token_key,
                )
                assert_av1_validation_derivation_source_commitments(
                    plan,
                    resolver=source_sha256_resolver,
                )
                current_evaluation = evaluate_av1_validation_derivation_candidate(
                    manifest=manifest,
                    plan=plan,
                    partition=partition,
                    cell_plan_id=cell_plan_id,
                    attempts=attempts,
                    records=records,
                    current_observations=(
                        _load_current_derivation_observations_from_connection(
                            connection=connection,
                            records=records,
                        )
                    ),
                    proposed_at=locked_at,
                )
                assert_av1_validation_derivation_execution_contract(
                    manifest,
                    plan,
                )
                source_sha256_resolver.verify()
                return _finalize_and_write_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=plan,
                    proposal=proposal,
                    review_claims=review_claims,
                    review_envelopes=review_envelopes,
                    current_evaluation=current_evaluation,
                    locked_at=locked_at,
                    before_publish=source_sha256_resolver.assert_quiet,
                )
    except MediaforceRuntimeBusyError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation finalization requires the Mediaforce runtime to be paused"
        ) from exc


def _av1_validation_derivation_candidate_locked_at(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        cell_plan_id: str,
        clock: Callable[[], str],
) -> str:
    candidate_lock_path = (
        artifact_root / "candidate-locks" / f"{cell_plan_id}.json"
    )
    if not candidate_lock_path.exists() and not candidate_lock_path.is_symlink():
        return clock()
    return _load_av1_validation_derivation_candidate_lock_envelope(
        artifact_root,
        plan=plan,
        cell_plan_id=cell_plan_id,
    ).candidate_lock.locked_at


def load_verified_av1_validation_derivation_candidate_lock(
        *,
        config_path: Path,
        manifest: AV1ValidationManifestV2,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        plan_path: Path,
        cell_plan_id: str,
) -> AV1ValidationDerivationCandidateLockEnvelope:
    config = load_config(config_path)
    plan, artifact_root = _load_canonical_av1_validation_derivation_plan(
        config=config,
        plan_path=plan_path,
    )
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    validate_av1_validation_derivation_plan_binding(
        plan=plan,
        partition=partition,
    )
    assert_av1_validation_derivation_execution_contract(manifest, plan)
    try:
        with exclusive_mediaforce_runtime_lock(
            config,
            owner_payload={
                "purpose": "av1-candidate-lock-verification",
                "plan_id": plan.plan_id,
                "cell_plan_id": cell_plan_id,
            },
        ):
            migrate_config_state(config)
            artifact_root = _validated_av1_validation_derivation_artifact_root(
                config=config,
                plan=plan,
                artifact_root=artifact_root,
            )
            proposal = load_av1_validation_derivation_candidate_proposal(
                artifact_root,
                plan=plan,
                cell_plan_id=cell_plan_id,
            )
            attempts = load_av1_validation_derivation_attempts(
                artifact_root / "attempts"
            )
            records = load_av1_validation_derivation_terminal_records(
                artifact_root / "terminal-records"
            )
            review_claims = load_av1_validation_derivation_review_claims(
                artifact_root,
                plan=plan,
                proposal=proposal,
            )
            review_envelopes = load_av1_validation_derivation_review_envelopes(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claims=review_claims,
            )
            persisted = _load_av1_validation_derivation_candidate_lock_envelope(
                artifact_root,
                plan=plan,
                cell_plan_id=cell_plan_id,
            )
            with open_db(config.paths.db_path) as connection, ExitStack() as source_stack:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                inventory = load_av1_validation_partition_inventory(
                    connection,
                    config=config,
                )
                source_sha256_resolver = source_stack.enter_context(
                    av1_validation_partition_source_sha256_resolver(
                        connection,
                        config=config,
                    )
                )
                validate_av1_validation_partition_current_inputs(
                    partition,
                    manifest=manifest,
                    sources=inventory.sources,
                    expectations=inventory.expectations,
                    token_key=token_key,
                )
                assert_av1_validation_derivation_source_commitments(
                    plan,
                    resolver=source_sha256_resolver,
                )
                current_evaluation = evaluate_av1_validation_derivation_candidate(
                    manifest=manifest,
                    plan=plan,
                    partition=partition,
                    cell_plan_id=cell_plan_id,
                    attempts=attempts,
                    records=records,
                    current_observations=(
                        _load_current_derivation_observations_from_connection(
                            connection=connection,
                            records=records,
                        )
                    ),
                    proposed_at=persisted.candidate_lock.locked_at,
                )
                assert_av1_validation_derivation_execution_contract(
                    manifest,
                    plan,
                )
                source_sha256_resolver.verify()
                return _load_verified_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=plan,
                    proposal=proposal,
                    review_claims=review_claims,
                    review_envelopes=review_envelopes,
                    current_evaluation=current_evaluation,
                    cell_plan_id=cell_plan_id,
                )
    except MediaforceRuntimeBusyError as exc:
        raise AV1ValidationDerivationError(
            "AV1 candidate-lock verification requires the Mediaforce runtime to be paused"
        ) from exc


def record_av1_validation_derivation_visual_verdict(
        *,
        config_path: Path,
        manifest: AV1ValidationManifestV2,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        attempt: AV1ValidationDerivationAttempt,
        terminal_records_directory: Path,
        verdict: str,
        concern_tags: list[str],
        evidence_ids: list[str],
        moment_indexes: list[int],
        recorded_at: str,
) -> AV1ValidationDerivationTerminalRecord:
    config = load_config(config_path)
    try:
        with exclusive_mediaforce_runtime_lock(
            config,
            owner_payload={
                "purpose": "av1-derivation-verdict",
                "plan_id": plan.plan_id,
                "assignment_id": attempt.assignment_id,
            },
        ):
            migrate_config_state(config)
            try:
                return _record_av1_validation_derivation_visual_verdict_locked(
                    config=config,
                    manifest=manifest,
                    plan=plan,
                    partition=partition,
                    token_key=token_key,
                    attempt=attempt,
                    terminal_records_directory=terminal_records_directory,
                    verdict=verdict,
                    concern_tags=concern_tags,
                    evidence_ids=evidence_ids,
                    moment_indexes=moment_indexes,
                    recorded_at=recorded_at,
                )
            except _AV1ValidationDerivationVerdictSafetyStop:
                artifact_root = _validated_av1_validation_derivation_artifact_root(
                    config=config,
                    plan=plan,
                    artifact_root=av1_validation_derivation_artifact_root(config, plan),
                )
                canonical_terminal_records_directory = (
                    artifact_root / "terminal-records"
                )
                if terminal_records_directory.resolve() != (
                    canonical_terminal_records_directory.resolve()
                ):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation terminal artifacts must use the partition-global canonical directory"
                    )
                terminal = build_av1_validation_derivation_terminal_record(
                    plan=plan,
                    partition=partition,
                    attempt=attempt,
                    review_failure_reason_code="safety_stop",
                )
                ensure_av1_validation_derivation_terminal_intent(
                    artifact_root / "terminal-intents",
                    terminal,
                )
                ensure_av1_validation_derivation_terminal_record(
                    canonical_terminal_records_directory,
                    terminal,
                )
                return terminal
    except MediaforceRuntimeBusyError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict requires the Mediaforce runtime to be paused"
        ) from exc


def _record_av1_validation_derivation_visual_verdict_locked(
        *,
        config: MediaforceConfig,
        manifest: AV1ValidationManifestV2,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        token_key: bytes,
        attempt: AV1ValidationDerivationAttempt,
        terminal_records_directory: Path,
        verdict: str,
        concern_tags: list[str],
        evidence_ids: list[str],
        moment_indexes: list[int],
        recorded_at: str,
) -> AV1ValidationDerivationTerminalRecord:
    if attempt.status != "review_pending":
        raise AV1ValidationDerivationError("AV1 derivation attempt is not awaiting visual review")
    if verdict not in {"approved", "rejected"}:
        raise AV1ValidationDerivationError("AV1 derivation visual verdict is invalid")
    assert_preregistered_av1_validation_manifest_v2(manifest)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    validate_av1_validation_derivation_plan_binding(
        plan=plan,
        partition=partition,
    )
    validate_av1_validation_derivation_attempt_binding(
        plan=plan,
        partition=partition,
        attempt=attempt,
    )
    artifact_root = _validated_av1_validation_derivation_artifact_root(
        config=config,
        plan=plan,
        artifact_root=av1_validation_derivation_artifact_root(config, plan),
    )
    persisted_plan = load_av1_validation_derivation_plan(
        artifact_root / "plan.json"
    )
    if persisted_plan != plan:
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict plan is not the canonical partition authority"
        )
    persisted_attempt = next(
        (
            item
            for item in load_av1_validation_derivation_attempts(
                artifact_root / "attempts"
            )
            if item.assignment_id == attempt.assignment_id
        ),
        None,
    )
    if persisted_attempt != attempt:
        raise AV1ValidationDerivationError(
            "AV1 derivation verdict attempt is not the immutable current attempt"
        )
    if terminal_records_directory.resolve() != (
        artifact_root / "terminal-records"
    ).resolve():
        raise AV1ValidationDerivationError(
            "AV1 derivation terminal artifacts must use the partition-global canonical directory"
        )
    terminal_records_directory = artifact_root / "terminal-records"
    calibration = attempt.calibration_payload()
    sample_item = object_dict(calibration.get("sample_item"))
    claim_created = ensure_av1_validation_derivation_verdict_claim(
        artifact_root / "verdict-claims",
        plan=plan,
        attempt=attempt,
        claimed_at=recorded_at,
    )
    existing_verdict_intent = (
        None
        if claim_created
        else load_av1_validation_derivation_verdict_intent(
            artifact_root / "verdict-intents",
            plan=plan,
            attempt=attempt,
        )
    )
    if not claim_created and existing_verdict_intent is None:
        raise _AV1ValidationDerivationVerdictSafetyStop(
            "AV1 derivation interrupted verdict claim stopped the affected cell"
        )
    if claim_created:
        try:
            assert_av1_validation_derivation_authorization_active(
                plan,
                at=recorded_at,
            )
        except AV1ValidationDerivationError as exc:
            raise _AV1ValidationDerivationVerdictSafetyStop(
                "AV1 derivation verdict arrived outside its authorization window"
            ) from exc
    terminal_publication_started = False
    try:
        with open_db(config.paths.db_path) as connection, ExitStack() as source_stack:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
            source_sha256_resolver = source_stack.enter_context(
                av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=config,
                )
            )
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            assert_av1_validation_derivation_source_commitments(
                plan,
                resolver=source_sha256_resolver,
            )
            assert_av1_validation_derivation_execution_contract(manifest, plan)
            review_root = _prepare_derivation_review_root(artifact_root)
            current_review_fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            if (
                current_review_fingerprint is None
                or current_review_fingerprint
                != str(calibration.get("review_artifact_fingerprint") or "")
            ):
                raise _AV1ValidationDerivationVerdictSafetyStop(
                    "AV1 derivation review media is unavailable or changed"
                )
            assert_av1_validation_derivation_execution_contract(manifest, plan)
            verdict_intent = resolve_av1_validation_derivation_verdict_intent(
                artifact_root / "verdict-intents",
                plan=plan,
                attempt=attempt,
                verdict=verdict,
                concern_tags=concern_tags,
                evidence_ids=evidence_ids,
                moment_indexes=moment_indexes,
                recorded_at=recorded_at,
            )
            frozen_verdict = str(verdict_intent["verdict"])
            frozen_concern_tags = [
                str(value) for value in object_list(verdict_intent["concern_tags"])
            ]
            frozen_evidence_ids = [
                str(value) for value in object_list(verdict_intent["evidence_ids"])
            ]
            frozen_moment_indexes = [
                int(value) for value in object_list(verdict_intent["moment_indexes"])
            ]
            frozen_recorded_at = str(verdict_intent["recorded_at"])
            calibration["current_review_artifact_fingerprint"] = (
                current_review_fingerprint
            )
            result = build_visual_content_intent_observation(
                prefix=_derivation_prefix(config, sample_item),
                sample_item=sample_item,
                calibration=calibration,
                verdict=frozen_verdict,
                concern_tags=frozen_concern_tags,
                evidence_ids=frozen_evidence_ids,
                moment_indexes=frozen_moment_indexes,
                recorded_at=frozen_recorded_at,
                personalization_eligible=False,
                personalization_exclusion_reason=(
                    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON
                ),
            )
            if result.observation is None:
                terminal = build_av1_validation_derivation_terminal_record(
                    plan=plan,
                    partition=partition,
                    attempt=attempt,
                    observation_exclusion_reason="content_intent_observation_excluded",
                )
            else:
                terminal = build_av1_validation_derivation_terminal_record(
                    plan=plan,
                    partition=partition,
                    attempt=attempt,
                    observation=result.observation,
                )
            if result.observation is not None:
                try:
                    append_content_intent_boundary_observation(
                        connection,
                        result.observation,
                    )
                except ContentIntentObservationConflictError as exc:
                    raise _AV1ValidationDerivationVerdictSafetyStop(
                        "AV1 derivation visual observation conflicts with existing evidence"
                    ) from exc
            assert_av1_validation_derivation_execution_contract(manifest, plan)
            source_sha256_resolver.verify()
            terminal_publication_started = True
            ensure_av1_validation_derivation_terminal_intent(
                artifact_root / "terminal-intents",
                terminal,
                before_publish=source_sha256_resolver.assert_quiet,
            )
            ensure_av1_validation_derivation_terminal_record(
                terminal_records_directory,
                terminal,
                before_publish=source_sha256_resolver.assert_quiet,
            )
    except _AV1ValidationDerivationVerdictSafetyStop:
        raise
    except (
        AV1ValidationDerivationError,
        AV1ValidationPartitionError,
        OSError,
        SQLAlchemyError,
    ) as exc:
        if terminal_publication_started:
            raise
        raise _AV1ValidationDerivationVerdictSafetyStop(
            "AV1 derivation pre-publication failure stopped the affected cell"
        ) from exc
    return terminal


def _failed_attempt(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        assignment_id: str,
        started_at: str,
        completed_at: str,
        status: AV1ValidationDerivationAttemptStatus,
        reason_code: str,
) -> AV1ValidationDerivationAttempt:
    if _authorization_expired(plan, completed_at):
        status = "failed"
        reason_code = "authorization_expired"
    return build_av1_validation_derivation_attempt(
        plan=plan,
        partition=partition,
        assignment_id=assignment_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        reason_code=reason_code,
    )


def _authorization_expired(
        plan: AV1ValidationDerivationPlan,
        completed_at: str,
) -> bool:
    return _timestamp(completed_at) >= _timestamp(plan.authorization.valid_until)


def _bind_derivation_intent(
        *,
        config: MediaforceConfig,
        sample_item: dict[str, Any],
        intent_level: str,
) -> None:
    policy = deepcopy(config.resolve_policy(str(sample_item.get("rel_path") or "")))
    video = dict(object_dict(policy.get("video")))
    video.update({
        "compression_intent_schema_version": 1,
        "compression_intent": intent_level,
        "compression_intent_source": "operator",
        "compression_intent_confirmed": True,
    })
    policy["video"] = video
    intent = operator_intent_from_policy(
        video,
        default_video_policy=config.video,
        audio_policy=object_dict(policy.get("audio")),
        subtitle_policy=object_dict(policy.get("subtitle")),
    )
    duration_seconds = float_value(sample_item.get("duration_seconds")) or None
    sample_item["resolved_policy"] = policy
    sample_item["resolved_operator_intent"] = intent.to_payload(
        item_runtime_seconds=duration_seconds
    )
    sample_item["compression_intent"] = intent.compression_intent.to_payload()
    ledger = resolve_stream_budget_ledger(
        sample_item,
        default_video_policy=config.video,
        output_container=config.output_container,
        resolved_size_goal=intent.size_goal.resolve(duration_seconds),
        prefer_persisted=False,
    )
    sample_item["stream_budget_ledger"] = ledger.to_payload()


def _validate_bound_sample_item(
        *,
        assignment: Any,
        source_identity: str,
        sample_item: Mapping[str, Any],
) -> None:
    ledger = resolve_stream_budget_ledger(
        sample_item,
        default_video_policy=object_dict(object_dict(sample_item.get("resolved_policy")).get("video")),
        output_container=str(sample_item.get("output_container") or "mkv"),
    )
    if (
        int(sample_item.get("library_item_id") or 0) != assignment.local_item_id
        or str(sample_item.get("content_version_fingerprint") or "") != source_identity
        or object_dict(sample_item.get("compression_intent")).get("level") != assignment.intent_level
        or ledger.remaining_video_bitrate_bps != assignment.target_video_bitrate_bps
        or stream_budget_projection_blocker(ledger) is not None
    ):
        raise AV1ValidationDerivationError("AV1 derivation sample item drifted from its reservation")


def _derivation_prefix(config: MediaforceConfig, sample_item: Mapping[str, Any]) -> str:
    rel_path = str(sample_item.get("rel_path") or "")
    scope = media_group_scope_for_rel_path(rel_path, library_types=config.library_type_map)
    if scope is not None and scope.prefix:
        return scope.prefix
    return str(Path(rel_path).parent).strip(".")


def _assert_next_assignment(
        *,
        plan: AV1ValidationDerivationPlan,
        assignment_id: str,
        attempts_directory: Path,
        terminal_records_directory: Path,
) -> None:
    assignments_by_id = {
        assignment.assignment_id: assignment
        for assignment in plan.assignments
    }
    attempts = (
        load_av1_validation_derivation_attempts(attempts_directory)
        if attempts_directory.exists()
        else ()
    )
    attempts_by_id: dict[str, AV1ValidationDerivationAttempt] = {}
    for attempt in attempts:
        assignment = assignments_by_id.get(attempt.assignment_id)
        if (
            assignment is None
            or attempt.plan_id != plan.plan_id
            or attempt.authorization_id != plan.authorization.authorization_id
            or attempt.cell_plan_id != assignment.cell_plan_id
            or attempt.ordinal != assignment.ordinal
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation attempt is bound to another work item"
            )
        attempts_by_id[attempt.assignment_id] = attempt
    records = (
        load_av1_validation_derivation_terminal_records(terminal_records_directory)
        if terminal_records_directory.exists()
        else ()
    )
    records_by_id: dict[str, AV1ValidationDerivationTerminalRecord] = {}
    for record in records:
        assignment = assignments_by_id.get(record.assignment_id)
        attempt = attempts_by_id.get(record.assignment_id)
        if (
            assignment is None
            or record.plan_id != plan.plan_id
            or record.authorization_id != plan.authorization.authorization_id
            or record.cell_plan_id != assignment.cell_plan_id
            or record.ordinal != assignment.ordinal
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation terminal record is bound to another work item"
            )
        if (
            attempt is None
            or record.attempt_id != attempt.attempt_id
            or record.attempt_payload_sha256 != attempt.payload_sha256
            or record.started_at != attempt.started_at
            or record.completed_at != attempt.completed_at
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation terminal record does not match its attempt"
            )
        records_by_id[record.assignment_id] = record
    attempted_ids = set(attempts_by_id)
    record_ids = set(records_by_id)
    if attempted_ids != record_ids:
        raise AV1ValidationDerivationError(
            "AV1 derivation must record the prior human visual terminal before continuing"
        )
    claim_ids = {
        path.stem
        for path in attempts_directory.glob("*.claim")
    } if attempts_directory.exists() else set()
    if claim_ids - attempted_ids:
        raise AV1ValidationDerivationError(
            "AV1 derivation has an interrupted claimed assignment and cannot continue"
        )
    stopped_cells: set[str] = set()
    next_assignment: str | None = None
    for assignment in plan.assignments:
        attempt = attempts_by_id.get(assignment.assignment_id)
        if assignment.cell_plan_id in stopped_cells:
            if attempt is not None:
                raise AV1ValidationDerivationError(
                    "AV1 derivation attempted a later assignment in an affected stopped cell"
                )
            continue
        if attempt is None:
            if next_assignment is None:
                next_assignment = assignment.assignment_id
            continue
        if next_assignment is not None:
            raise AV1ValidationDerivationError(
                "AV1 derivation attempts do not preserve the immutable canonical order"
            )
        record = records_by_id[assignment.assignment_id]
        if (
            attempt.status != "review_pending"
            or record.status != "observed"
            or record.observation is None
            or record.observation.verdict != "acceptable"
            or not record.observation.quality_floor_met
        ):
            stopped_cells.add(assignment.cell_plan_id)
    if next_assignment is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation worklist has no remaining assignment"
        )
    requested_assignment = assignments_by_id.get(assignment_id)
    if requested_assignment is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation assignment is not authorized"
        )
    if requested_assignment.cell_plan_id in stopped_cells:
        raise AV1ValidationDerivationError(
            "AV1 derivation assignment belongs to an affected stopped cell"
        )
    if assignment_id != next_assignment:
        raise AV1ValidationDerivationError(
            "AV1 derivation assignment is not next in the immutable worklist"
        )


def _recover_interrupted_derivation_state(
        *,
        plan: AV1ValidationDerivationPlan,
        partition: AV1ValidationPrivatePartition,
        artifact_root: Path,
        attempts_directory: Path,
        terminal_records_directory: Path,
        completed_at: str,
) -> bool:
    attempts = (
        load_av1_validation_derivation_attempts(attempts_directory)
        if attempts_directory.exists()
        else ()
    )
    records = (
        load_av1_validation_derivation_terminal_records(
            terminal_records_directory
        )
        if terminal_records_directory.exists()
        else ()
    )
    attempts_by_assignment = {
        attempt.assignment_id: attempt
        for attempt in attempts
    }
    records_by_assignment = {
        record.assignment_id: record
        for record in records
    }
    terminal_intents_directory = artifact_root / "terminal-intents"
    terminal_intents = (
        load_av1_validation_derivation_terminal_intents(
            terminal_intents_directory
        )
        if terminal_intents_directory.exists()
        else ()
    )
    interrupted_terminal_publications: list[
        AV1ValidationDerivationTerminalRecord
    ] = []
    for terminal_intent in terminal_intents:
        attempt = attempts_by_assignment.get(terminal_intent.assignment_id)
        if (
            attempt is None
            or terminal_intent.plan_id != plan.plan_id
            or terminal_intent.authorization_id
            != plan.authorization.authorization_id
            or terminal_intent.attempt_id != attempt.attempt_id
            or terminal_intent.attempt_payload_sha256 != attempt.payload_sha256
            or terminal_intent.cell_plan_id != attempt.cell_plan_id
            or terminal_intent.ordinal != attempt.ordinal
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation terminal intent does not match its attempt"
            )
        terminal_record = records_by_assignment.get(
            terminal_intent.assignment_id
        )
        if terminal_record is not None:
            if terminal_record != terminal_intent:
                raise AV1ValidationDerivationError(
                    "AV1 derivation terminal intent conflicts with its record"
                )
            continue
        interrupted_terminal_publications.append(terminal_intent)
    if len(interrupted_terminal_publications) > 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation has multiple interrupted terminal publications"
        )
    if interrupted_terminal_publications:
        ensure_av1_validation_derivation_terminal_record(
            terminal_records_directory,
            interrupted_terminal_publications[0],
        )
        return True
    terminal_attempt_ids = {record.attempt_id for record in records}
    unterminated = [
        attempt
        for attempt in attempts
        if attempt.attempt_id not in terminal_attempt_ids
        and attempt.status != "review_pending"
    ]
    if len(unterminated) > 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation has multiple unterminated attempts"
        )
    if unterminated:
        terminal = build_av1_validation_derivation_terminal_record(
            plan=plan,
            partition=partition,
            attempt=unterminated[0],
        )
        write_av1_validation_derivation_terminal_record(
            terminal_records_directory,
            terminal,
        )
        return True
    interrupted_verdicts: list[AV1ValidationDerivationAttempt] = []
    for claim in load_av1_validation_derivation_verdict_claims(
        artifact_root / "verdict-claims",
        plan=plan,
        attempts=attempts,
    ):
        attempt = attempts_by_assignment[str(claim["assignment_id"])]
        if attempt.attempt_id in terminal_attempt_ids:
            continue
        if attempt.status != "review_pending":
            raise AV1ValidationDerivationError(
                "AV1 derivation verdict claim is not bound to a pending review"
            )
        verdict_intent = load_av1_validation_derivation_verdict_intent(
            artifact_root / "verdict-intents",
            plan=plan,
            attempt=attempt,
        )
        if verdict_intent is None:
            interrupted_verdicts.append(attempt)
    if len(interrupted_verdicts) > 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation has multiple interrupted verdict claims"
        )
    if interrupted_verdicts:
        terminal = build_av1_validation_derivation_terminal_record(
            plan=plan,
            partition=partition,
            attempt=interrupted_verdicts[0],
            review_failure_reason_code="safety_stop",
        )
        ensure_av1_validation_derivation_terminal_intent(
            artifact_root / "terminal-intents",
            terminal,
        )
        ensure_av1_validation_derivation_terminal_record(
            terminal_records_directory,
            terminal,
        )
        return True
    attempted_ids = {attempt.assignment_id for attempt in attempts}
    claims = load_av1_validation_derivation_assignment_claims(attempts_directory)
    orphaned = [
        claim for claim in claims if claim["assignment_id"] not in attempted_ids
    ]
    if len(orphaned) > 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation has multiple interrupted claimed assignments"
        )
    if not orphaned:
        return False
    claim = orphaned[0]
    if (
        claim["plan_id"] != plan.plan_id
        or claim["authorization_id"] != plan.authorization.authorization_id
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation interrupted claim is bound to another plan"
        )
    attempt = _failed_attempt(
        plan=plan,
        partition=partition,
        assignment_id=str(claim["assignment_id"]),
        started_at=str(claim["claimed_at"]),
        completed_at=completed_at,
        status="stopped",
        reason_code="interrupted_claim",
    )
    write_av1_validation_derivation_attempt(attempts_directory, attempt)
    terminal = build_av1_validation_derivation_terminal_record(
        plan=plan,
        partition=partition,
        attempt=attempt,
    )
    write_av1_validation_derivation_terminal_record(
        terminal_records_directory,
        terminal,
    )
    return True


def _current_derivation_review_artifact_fingerprint(
        *,
        review_root: Path,
        calibration: dict[str, Any],
) -> str | None:
    clips: list[_DerivationReviewClip] = []
    for role, key in (
        ("preview", "preview_clips"),
        ("source", "source_clips"),
        ("compare", "compare_clips"),
    ):
        for value in calibration.get(key, []):
            clip = object_dict(value)
            parsed = urlparse(str(clip.get("path") or ""))
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                return None
            candidate_path = Path(unquote(parsed.path))
            if not candidate_path.is_absolute():
                return None
            clips.append(_DerivationReviewClip(
                role=role,
                path=candidate_path,
                timestamp_seconds=float_value(clip.get("timestamp_seconds")),
                duration_seconds=float_value(clip.get("duration_seconds")),
            ))
    return _secure_and_fingerprint_derivation_review_clips(
        review_root=review_root,
        clips=tuple(clips),
    )


def _derivation_review_clips_from_generated_artifacts(
        *,
        preview_clips: list[Any],
        source_clips: list[Any],
        compare_clips: list[Any],
) -> tuple[_DerivationReviewClip, ...]:
    return tuple(
        _DerivationReviewClip(
            role=role,
            path=Path(getattr(clip, "output_path", "")),
            timestamp_seconds=float_value(getattr(clip, "timestamp_seconds", None)),
            duration_seconds=float_value(getattr(clip, "duration_seconds", None)),
        )
        for role, role_clips in (
            ("preview", preview_clips),
            ("source", source_clips),
            ("compare", compare_clips),
        )
        for clip in role_clips
    )


def _secure_and_fingerprint_derivation_review_clips(
        *,
        review_root: Path,
        clips: tuple[_DerivationReviewClip, ...],
) -> str | None:
    root_descriptor = -1
    held_clips: list[_HeldDerivationReviewClip] = []
    clip_payloads: list[dict[str, object]] = []
    completed = False
    try:
        root_descriptor = _bind_owner_only_directory_descriptor(
            review_root,
            label="review-media",
        )
        resolved_review_root = stable_absolute_path(review_root)
        invalid_clip_path = False
        for clip in clips:
            canonical_path, relative_path = _resolve_derivation_review_clip_path(
                review_root=resolved_review_root,
                path=clip.path,
            )
            if canonical_path is None or relative_path is None:
                invalid_clip_path = True
                break
            held_clips.append(_HeldDerivationReviewClip(
                clip=clip,
                canonical_path=canonical_path,
                descriptor=_open_owner_only_review_media_relative_file(
                    root_descriptor,
                    relative_path,
                ),
            ))
        _secure_derivation_review_media(
            review_root,
            root_descriptor=root_descriptor,
        )
        if invalid_clip_path:
            completed = True
            return None
        for held_clip in held_clips:
            _assert_held_derivation_review_clip_is_immutable(held_clip)
            held_clip.guard = MacOSFileIntegrityGuard(
                path=held_clip.canonical_path,
                descriptor=held_clip.descriptor,
                require_single_link=True,
            )
            held_clip.guard.assert_quiet()
        for held_clip in held_clips:
            clip_payload = reviewed_artifact_clip_payload_from_descriptor(
                role=held_clip.clip.role,
                path=held_clip.canonical_path,
                descriptor=held_clip.descriptor,
                timestamp_seconds=held_clip.clip.timestamp_seconds,
                duration_seconds=held_clip.clip.duration_seconds,
            )
            if clip_payload is None:
                completed = True
                return None
            clip_payloads.append(clip_payload)
        for held_clip in held_clips:
            if held_clip.guard is None:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review-media guard is unavailable"
                )
            held_clip.guard.assert_quiet()
        result = reviewed_artifact_fingerprint_from_payloads(
            clip_payloads,
            schema_version=3,
        )
        completed = True
        return result
    except FileIntegrityError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review-media integrity verification failed"
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM}:
            raise AV1ValidationDerivationError(
                "AV1 derivation review-media verification resources are unavailable"
            ) from exc
        completed = True
        return None
    finally:
        cleanup_error: OSError | None = None
        for held_clip in reversed(held_clips):
            if held_clip.guard is not None:
                try:
                    held_clip.guard.close()
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            try:
                os.close(held_clip.descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if root_descriptor >= 0:
            try:
                os.close(root_descriptor)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and completed:
            raise AV1ValidationDerivationError(
                "AV1 derivation review-media verification cleanup failed"
            ) from cleanup_error


def _resolve_derivation_review_clip_path(
        *,
        review_root: Path,
        path: Path,
) -> tuple[Path | None, Path | None]:
    if not path.is_absolute():
        return None, None
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return None, None
        canonical_path = stable_absolute_path(path)
        relative_path = canonical_path.relative_to(review_root)
    except (OSError, ValueError):
        return None, None
    if (
        not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        return None, None
    return canonical_path, relative_path


def _assert_held_derivation_review_clip_is_immutable(
        held_clip: _HeldDerivationReviewClip,
) -> None:
    try:
        descriptor_info = os.fstat(held_clip.descriptor)
        path_info = held_clip.canonical_path.lstat()
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media is unavailable after sealing"
        ) from exc
    if (
        not stat.S_ISREG(descriptor_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) != 0o400
        or descriptor_info.st_nlink != 1
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review media changed before fingerprinting"
        )


def _prepare_derivation_review_root(artifact_root: Path) -> Path:
    return _prepare_owner_only_directory(
        artifact_root / "review-media",
        label="review-media",
    )


def _secure_derivation_review_media(
        review_root: Path,
        *,
        root_descriptor: int | None = None,
) -> None:
    descriptor = (
        root_descriptor
        if root_descriptor is not None
        else _open_owner_only_directory_descriptor(
            review_root,
            label="review-media",
        )
    )
    try:
        _validate_owner_only_review_media_directory(descriptor)
    finally:
        if root_descriptor is None:
            os.close(descriptor)


def _validate_owner_only_review_media_directory(
        directory_descriptor: int,
) -> None:
    try:
        with os.scandir(directory_descriptor) as entries:
            entry_names = tuple(sorted(entry.name for entry in entries))
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media could not be inspected safely"
        ) from exc
    for entry_name in entry_names:
        try:
            initial_info = os.stat(
                entry_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review media could not be inspected safely"
            ) from exc
        is_directory = stat.S_ISDIR(initial_info.st_mode)
        is_regular_file = stat.S_ISREG(initial_info.st_mode)
        if not is_directory and not is_regular_file:
            raise AV1ValidationDerivationError(
                "AV1 derivation review media must not contain links or special files"
            )
        if is_directory:
            if initial_info.st_uid != os.getuid() or initial_info.st_mode & 0o077:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review media must be owner-only"
                )
            _validate_owner_only_review_media_subdirectory(
                directory_descriptor,
                entry_name,
                initial_info=initial_info,
            )
        else:
            if initial_info.st_nlink != 1:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review media must not contain hard links"
                )
            if initial_info.st_uid != os.getuid() or initial_info.st_mode & 0o077:
                raise AV1ValidationDerivationError(
                    "AV1 derivation review media must be owner-only"
                )
            _validate_owner_only_review_media_file(
                directory_descriptor,
                entry_name,
                initial_info=initial_info,
            )
    try:
        with os.scandir(directory_descriptor) as entries:
            final_names = tuple(sorted(entry.name for entry in entries))
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media could not be inspected safely"
        ) from exc
    if final_names != entry_names:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media changed during validation"
        )


def _validate_owner_only_review_media_subdirectory(
        parent_descriptor: int,
        entry_name: str,
        *,
        initial_info: os.stat_result,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            entry_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        descriptor_info = os.fstat(descriptor)
        current_info = os.stat(
            entry_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or descriptor_info.st_mode & 0o077
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (initial_info.st_dev, initial_info.st_ino)
            or (current_info.st_dev, current_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media changed during validation"
            )
        _validate_owner_only_review_media_directory(descriptor)
    except AV1ValidationDerivationError:
        raise
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media could not be inspected safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_owner_only_review_media_file(
        parent_descriptor: int,
        entry_name: str,
        *,
        initial_info: os.stat_result,
) -> None:
    if initial_info.st_nlink != 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media must not contain hard links"
        )
    descriptor = -1
    try:
        descriptor = os.open(
            entry_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        descriptor_info = os.fstat(descriptor)
        current_info = os.stat(
            entry_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or descriptor_info.st_mode & 0o177
            or descriptor_info.st_nlink != 1
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (initial_info.st_dev, initial_info.st_ino)
            or (current_info.st_dev, current_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media changed during validation"
            )
        if stat.S_IMODE(descriptor_info.st_mode) != 0o400:
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        sealed_info = os.fstat(descriptor)
        sealed_path_info = os.stat(
            entry_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_IMODE(sealed_info.st_mode) != 0o400
            or sealed_info.st_nlink != 1
            or (sealed_info.st_dev, sealed_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
            or (sealed_path_info.st_dev, sealed_path_info.st_ino)
            != (sealed_info.st_dev, sealed_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media could not be sealed read-only"
            )
    except AV1ValidationDerivationError:
        raise
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review media could not be inspected safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_owner_only_review_media_relative_file(
        root_descriptor: int,
        relative_path: Path,
) -> int:
    current_descriptor = os.dup(root_descriptor)
    file_descriptor = -1
    try:
        for component in relative_path.parts[:-1]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_descriptor,
            )
            try:
                descriptor_info = os.fstat(next_descriptor)
                current_info = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(descriptor_info.st_mode)
                    or descriptor_info.st_uid != os.getuid()
                    or descriptor_info.st_mode & 0o077
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (current_info.st_dev, current_info.st_ino)
                ):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation review media path changed during fingerprinting"
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        entry_name = relative_path.parts[-1]
        initial_info = os.stat(
            entry_name,
            dir_fd=current_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(initial_info.st_mode)
            or initial_info.st_uid != os.getuid()
            or initial_info.st_mode & 0o077
            or initial_info.st_nlink != 1
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media file is not owner-only and single-link"
            )
        file_descriptor = os.open(
            entry_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=current_descriptor,
        )
        descriptor_info = os.fstat(file_descriptor)
        current_info = os.stat(
            entry_name,
            dir_fd=current_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or descriptor_info.st_mode & 0o077
            or descriptor_info.st_nlink != 1
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (initial_info.st_dev, initial_info.st_ino)
            or (current_info.st_dev, current_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review media file changed during fingerprinting"
            )
        return file_descriptor
    except AV1ValidationDerivationError:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise
    except OSError as exc:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise AV1ValidationDerivationError(
            "AV1 derivation review media file could not be opened safely"
        ) from exc
    finally:
        os.close(current_descriptor)


def _config_with_review_directory(
        config: MediaforceConfig,
        review_root: Path,
) -> MediaforceConfig:
    runtime_config = copy(config)
    runtime_paths = copy(config.paths)
    object.__setattr__(runtime_paths, "review_dir", review_root)
    object.__setattr__(runtime_config, "paths", runtime_paths)
    return cast(MediaforceConfig, runtime_config)


def _run_id(plan_id: str, assignment_id: str, started_at: str) -> str:
    digest = hashlib.sha256(canonical_json_bytes({
        "plan_id": plan_id,
        "assignment_id": assignment_id,
        "started_at": started_at,
    })).hexdigest()
    return digest[:12]


def _review_url(_config: MediaforceConfig, path: Path) -> str:
    return path.resolve().as_uri()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AV1ValidationDerivationError("AV1 derivation timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _unused(*_args: Any, **_kwargs: Any) -> Any:
    raise AV1ValidationDerivationError("AV1 derivation invoked an unsupported runtime dependency")
