from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Mapping

from mediaforce.core.file_integrity import (
    fsync_durable_file,
    rename_exclusive,
)
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    AV1ValidationV3Error,
    av1_validation_v3_qualification_key_id,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
    serialize_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_preparation import (
    av1_validation_v3_tier1_config_sha256,
    build_av1_validation_v3_tier1_prepared_plan,
    build_av1_validation_v3_tier1_prepared_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    validate_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
    serialize_av1_validation_v3_tier1_authorization_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1RuntimeError,
    AV1ValidationV3Tier1ToolchainBinding,
    build_av1_validation_v3_tier1_toolchain_binding,
)


AV1_VALIDATION_V3_TIER1_PLAN_FILENAME = "qualification-plan.json"
AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME = (
    "tier1-authorization-request.json"
)
AV1_VALIDATION_V3_TIER1_PUBLICATION_DIRECTORY_PREFIX = (
    "av1-v3-tier1-preparation-"
)
AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME = (
    "tier1-effective-config-snapshot.json"
)
AV1_VALIDATION_V3_TIER1_CONFIG_PUBLICATION_DIRECTORY_PREFIX = (
    "av1-v3-tier1-config-"
)
AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY = (
    "av1-v3-qualification-key"
)
AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME = "qualification-key.bin"

AV1_VALIDATION_V3_TIER1_PUBLICATION_REASON_CODES = frozenset({
    "artifact_conflict",
    "artifact_incomplete",
    "artifact_malformed",
    "artifact_unsafe",
    "execution_already_claimed",
    "filesystem_capability_missing",
    "inventory_read_already_claimed",
    "publication_cleanup_failed",
    "publication_failed",
    "publication_root_unsafe",
    "tier2_selection_already_claimed",
    "toolchain_drifted",
    "toolchain_invalid",
})

_ARTIFACT_FILENAMES = frozenset({
    AV1_VALIDATION_V3_TIER1_PLAN_FILENAME,
    AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME,
})
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class AV1ValidationV3Tier1PublicationError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in AV1_VALIDATION_V3_TIER1_PUBLICATION_REASON_CODES:
            raise ValueError("AV1 v3 Tier 1 publication reason code is invalid")
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1PublicationResult:
    directory: Path
    plan_path: Path
    request_path: Path
    plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier1AuthorizationRequest
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "gate": "A0",
            "tier": "tier1",
            "plan_id": self.plan.plan_id,
            "plan_payload_sha256": self.plan.payload_sha256,
            "request_id": self.request.request_id,
            "request_payload_sha256": self.request.payload_sha256,
            "config_sha256": self.plan.config_sha256,
            "toolchain_sha256": self.plan.toolchain_sha256,
            "fixture_matrix_sha256": self.plan.fixture_matrix_sha256,
            "private_inventory_read_authorized": False,
            "media_read_authorized": False,
            "tier2_execution_authorized": False,
            "runtime_execution_authorized": False,
            "qualification_execution_authorized": False,
            "key_creation_authorized": False,
            "evidence_creation_authorized": False,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "public_bundle_activation_allowed": False,
            "activation_authorized": False,
            "execution_requires_separate_owner_authorization": True,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ConfigPublicationResult:
    directory: Path
    snapshot_path: Path
    config_sha256: str
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier1_effective_config_snapshot",
            "gate": "A0",
            "tier": "tier1",
            "config_sha256": self.config_sha256,
            "private_inventory_read_authorized": False,
            "media_read_authorized": False,
            "tier2_execution_authorized": False,
            "runtime_execution_authorized": False,
            "qualification_execution_authorized": False,
            "key_creation_authorized": False,
            "evidence_creation_authorized": False,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "public_bundle_activation_allowed": False,
            "activation_authorized": False,
            "execution_requires_separate_owner_authorization": True,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationKeyPublicationResult:
    directory: Path
    key_path: Path
    qualification_key_id: str
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "v3_qualification_key",
            "gate": "A0",
            "qualification_key_id": self.qualification_key_id,
            "private_inventory_read_authorized": False,
            "media_read_authorized": False,
            "tier2_execution_authorized": False,
            "runtime_execution_authorized": False,
            "qualification_execution_authorized": False,
            "key_creation_authorized": False,
            "evidence_creation_authorized": False,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "public_bundle_activation_allowed": False,
            "activation_authorized": False,
            "execution_requires_separate_owner_authorization": True,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def ensure_av1_validation_v3_qualification_key(
    *,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3QualificationKeyPublicationResult:
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    output_root_descriptor = -1
    try:
        output_root_descriptor = _open_or_create_owner_only_directory(
            normalized_root
        )
        existing_key = _read_existing_qualification_key(
            output_root_descriptor=output_root_descriptor,
        )
        if existing_key is not None:
            return _qualification_key_publication_result(
                normalized_root=normalized_root,
                qualification_key=existing_key,
                created=False,
            )
        qualification_key = secrets.token_bytes(32)
        expected_members = {
            AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME: qualification_key,
        }
        try:
            created = _publish_single_member_artifact_set(
                output_root_descriptor=output_root_descriptor,
                final_name=AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
                expected_members=expected_members,
            )
        except AV1ValidationV3Tier1PublicationError as exc:
            if exc.reason_code != "artifact_conflict":
                raise
            existing_key = _read_existing_qualification_key(
                output_root_descriptor=output_root_descriptor,
            )
            if existing_key is None:
                raise
            qualification_key = existing_key
            created = False
        return _qualification_key_publication_result(
            normalized_root=normalized_root,
            qualification_key=qualification_key,
            created=created,
        )
    except AV1ValidationV3Tier1PublicationError:
        raise
    except OSError as exc:
        reason_code = (
            "filesystem_capability_missing"
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
            else "publication_failed"
        )
        raise AV1ValidationV3Tier1PublicationError(
            reason_code,
            "AV1 v3 qualification key could not be published safely",
        ) from exc
    finally:
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def load_av1_validation_v3_qualification_key(
    *,
    output_root: Path,
    repository_root: Path,
) -> bytes:
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    output_root_descriptor = -1
    try:
        output_root_descriptor = _open_existing_owner_only_directory(
            normalized_root
        )
        qualification_key = _read_existing_qualification_key(
            output_root_descriptor=output_root_descriptor,
        )
        if qualification_key is None:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_incomplete",
                "AV1 v3 qualification key is unavailable",
            )
        return qualification_key
    finally:
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def publish_av1_validation_v3_tier1_config_snapshot(
    *,
    snapshot_bytes: bytes,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ConfigPublicationResult:
    validate_av1_validation_v3_tier1_config_snapshot(snapshot_bytes)
    config_sha256 = av1_validation_v3_tier1_config_sha256(snapshot_bytes)
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    final_name = (
        f"{AV1_VALIDATION_V3_TIER1_CONFIG_PUBLICATION_DIRECTORY_PREFIX}"
        f"{config_sha256.removeprefix('sha256:')}"
    )
    expected_members = {
        AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME: snapshot_bytes,
    }
    output_root_descriptor = -1
    try:
        output_root_descriptor = _open_or_create_owner_only_directory(
            normalized_root
        )
        existing = _existing_config_publication_result(
            output_root_descriptor=output_root_descriptor,
            normalized_root=normalized_root,
            final_name=final_name,
            expected_members=expected_members,
            config_sha256=config_sha256,
        )
        if existing is not None:
            return existing
        created = _publish_single_member_artifact_set(
            output_root_descriptor=output_root_descriptor,
            final_name=final_name,
            expected_members=expected_members,
        )
        return AV1ValidationV3Tier1ConfigPublicationResult(
            directory=normalized_root / final_name,
            snapshot_path=(
                normalized_root
                / final_name
                / AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME
            ),
            config_sha256=config_sha256,
            created=created,
        )
    except AV1ValidationV3Tier1PublicationError:
        raise
    except OSError as exc:
        reason_code = (
            "filesystem_capability_missing"
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
            else "publication_failed"
        )
        raise AV1ValidationV3Tier1PublicationError(
            reason_code,
            "AV1 v3 Tier 1 config snapshot could not be published safely",
        ) from exc
    finally:
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def publish_av1_validation_v3_tier1_preparation(
    *,
    protocol: AV1ValidationProtocolV3,
    qualification_key_id: str,
    repository_commit: str,
    repository_tree: str,
    config_bytes: bytes,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    output_root: Path,
    repository_root: Path,
    frozen_at: str,
    plan_valid_until: str,
    requested_at: str,
    request_valid_until: str,
) -> AV1ValidationV3Tier1PublicationResult:
    validate_av1_validation_v3_tier1_config_snapshot(config_bytes)
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    toolchain = _build_toolchain(ffmpeg_path, ffprobe_path)
    plan = build_av1_validation_v3_tier1_prepared_plan(
        protocol=protocol,
        qualification_key_id=qualification_key_id,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        config_bytes=config_bytes,
        toolchain=toolchain,
        frozen_at=frozen_at,
        valid_until=plan_valid_until,
    )
    request = build_av1_validation_v3_tier1_prepared_request(
        protocol=protocol,
        plan=plan,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        config_bytes=config_bytes,
        toolchain=toolchain,
        requested_at=requested_at,
        valid_until=request_valid_until,
    )
    expected_members = {
        AV1_VALIDATION_V3_TIER1_PLAN_FILENAME: (
            serialize_av1_validation_v3_qualification_plan(plan)
        ),
        AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME: (
            serialize_av1_validation_v3_tier1_authorization_request(request)
        ),
    }
    final_name = (
        f"{AV1_VALIDATION_V3_TIER1_PUBLICATION_DIRECTORY_PREFIX}"
        f"{request.request_id}"
    )
    output_root_descriptor = -1
    try:
        output_root_descriptor = _open_or_create_owner_only_directory(
            normalized_root
        )
        existing = _existing_publication_result(
            output_root_descriptor=output_root_descriptor,
            normalized_root=normalized_root,
            final_name=final_name,
            expected_members=expected_members,
            plan=plan,
            request=request,
        )
        if existing is not None:
            _assert_toolchain_unchanged(
                toolchain,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
            )
            return existing
        return _publish_new_artifact_set(
            output_root_descriptor=output_root_descriptor,
            normalized_root=normalized_root,
            final_name=final_name,
            expected_members=expected_members,
            plan=plan,
            request=request,
            toolchain=toolchain,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    except AV1ValidationV3Tier1PublicationError:
        raise
    except OSError as exc:
        reason_code = (
            "filesystem_capability_missing"
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
            else "publication_failed"
        )
        raise AV1ValidationV3Tier1PublicationError(
            reason_code,
            "AV1 v3 Tier 1 preparation could not be published safely",
        ) from exc
    finally:
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def publish_av1_validation_v3_owner_artifact(
    *,
    output_root: Path,
    repository_root: Path,
    final_name: str,
    filename: str,
    content: bytes,
    description: str,
) -> tuple[Path, bool]:
    return publish_av1_validation_v3_owner_artifacts(
        output_root=output_root,
        repository_root=repository_root,
        final_name=final_name,
        members={filename: content},
        description=description,
    )


def publish_av1_validation_v3_owner_artifacts(
    *,
    output_root: Path,
    repository_root: Path,
    final_name: str,
    members: Mapping[str, bytes],
    description: str,
) -> tuple[Path, bool]:
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    expected_members = dict(members)
    if not expected_members:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 publication artifact set is empty",
        )
    output_root_descriptor = -1
    try:
        output_root_descriptor = _open_or_create_owner_only_directory(
            normalized_root
        )
        created = _publish_single_member_artifact_set(
            output_root_descriptor=output_root_descriptor,
            final_name=final_name,
            expected_members=expected_members,
        )
        return normalized_root / final_name, created
    except AV1ValidationV3Tier1PublicationError:
        raise
    except OSError as exc:
        reason_code = (
            "filesystem_capability_missing"
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
            else "publication_failed"
        )
        raise AV1ValidationV3Tier1PublicationError(
            reason_code,
            f"AV1 v3 Tier 1 {description} could not be published safely",
        ) from exc
    finally:
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def load_av1_validation_v3_owner_artifact(
    *,
    output_root: Path,
    repository_root: Path,
    final_name: str,
    filename: str,
    maximum_size: int,
) -> bytes:
    normalized_root = _validated_publication_root(
        output_root,
        repository_root=repository_root,
    )
    output_root_descriptor = -1
    directory_descriptor = -1
    try:
        output_root_descriptor = _open_existing_owner_only_directory(
            normalized_root
        )
        directory_descriptor = os.open(
            final_name,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
        _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=final_name,
            descriptor=directory_descriptor,
        )
        if frozenset(os.listdir(directory_descriptor)) != frozenset({filename}):
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_malformed",
                "AV1 v3 Tier 1 publication artifact set is invalid",
            )
        member_info = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if member_info.st_size <= 0 or member_info.st_size > maximum_size:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_malformed",
                "AV1 v3 Tier 1 publication artifact size is invalid",
            )
        return _read_member(
            directory_descriptor,
            filename,
            expected_size=member_info.st_size,
        )
    except FileNotFoundError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_incomplete",
            "AV1 v3 Tier 1 publication artifact is unavailable",
        ) from exc
    except AV1ValidationV3Tier1PublicationError:
        raise
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication artifact could not be opened safely",
        ) from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if output_root_descriptor >= 0:
            os.close(output_root_descriptor)


def _validated_publication_root(
    output_root: Path,
    *,
    repository_root: Path,
) -> Path:
    expanded = output_root.expanduser()
    if not expanded.is_absolute():
        raise AV1ValidationV3Tier1PublicationError(
            "publication_root_unsafe",
            "AV1 v3 Tier 1 publication root must be absolute",
        )
    normalized = Path(os.path.abspath(os.fspath(expanded)))
    resolved_for_containment = Path(os.path.realpath(os.fspath(expanded)))
    try:
        repository = repository_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "publication_root_unsafe",
            "AV1 v3 Tier 1 repository root is unavailable",
        ) from exc
    if (
        resolved_for_containment == repository
        or resolved_for_containment.is_relative_to(repository)
        or repository.is_relative_to(resolved_for_containment)
    ):
        raise AV1ValidationV3Tier1PublicationError(
            "publication_root_unsafe",
            "AV1 v3 Tier 1 publication root overlaps the repository",
        )
    return normalized


def _open_or_create_owner_only_directory(path: Path) -> int:
    return _open_owner_only_directory(path, create_missing=True)


def _open_existing_owner_only_directory(path: Path) -> int:
    return _open_owner_only_directory(path, create_missing=False)


def _open_owner_only_directory(
    path: Path,
    *,
    create_missing: bool,
) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise AV1ValidationV3Tier1PublicationError(
            "filesystem_capability_missing",
            "AV1 v3 Tier 1 stable directory binding is unavailable",
        )
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for index, component in enumerate(path.parts[1:], start=1):
            created = False
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise AV1ValidationV3Tier1PublicationError(
                        "artifact_incomplete",
                        "AV1 v3 owner-only artifact root is unavailable",
                    )
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                created = True
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            try:
                descriptor_info = os.fstat(next_descriptor)
                path_info = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(descriptor_info.st_mode)
                    or not stat.S_ISDIR(path_info.st_mode)
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                ):
                    raise AV1ValidationV3Tier1PublicationError(
                        "publication_root_unsafe",
                        "AV1 v3 Tier 1 publication root changed during binding",
                    )
                if (
                    (created or index == len(path.parts) - 1)
                    and (
                        descriptor_info.st_uid != os.getuid()
                        or stat.S_IMODE(descriptor_info.st_mode) != 0o700
                    )
                ):
                    raise AV1ValidationV3Tier1PublicationError(
                        "publication_root_unsafe",
                        "AV1 v3 Tier 1 publication root must be owner-only",
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except AV1ValidationV3Tier1PublicationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise AV1ValidationV3Tier1PublicationError(
            "publication_root_unsafe",
            "AV1 v3 Tier 1 publication root could not be bound safely",
        ) from exc


def _build_toolchain(
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> AV1ValidationV3Tier1ToolchainBinding:
    try:
        return build_av1_validation_v3_tier1_toolchain_binding(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    except AV1ValidationV3Tier1RuntimeError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "toolchain_invalid",
            "AV1 v3 Tier 1 publication toolchain is invalid",
        ) from exc


def _assert_toolchain_unchanged(
    expected: AV1ValidationV3Tier1ToolchainBinding,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    current = _build_toolchain(ffmpeg_path, ffprobe_path)
    if current.payload_sha256 != expected.payload_sha256:
        raise AV1ValidationV3Tier1PublicationError(
            "toolchain_drifted",
            "AV1 v3 Tier 1 publication toolchain changed before publication",
        )


def _publish_new_artifact_set(
    *,
    output_root_descriptor: int,
    normalized_root: Path,
    final_name: str,
    expected_members: dict[str, bytes],
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> AV1ValidationV3Tier1PublicationResult:
    staging_name = f".{final_name}.{secrets.token_hex(12)}.tmp"
    staging_descriptor = -1
    staging_created = False
    renamed = False
    try:
        os.mkdir(staging_name, 0o700, dir_fd=output_root_descriptor)
        staging_created = True
        os.fsync(output_root_descriptor)
        staging_descriptor = os.open(
            staging_name,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
        staging_identity = _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=staging_name,
            descriptor=staging_descriptor,
        )
        for filename in sorted(expected_members):
            _write_member(
                staging_descriptor,
                filename,
                expected_members[filename],
            )
        os.fsync(staging_descriptor)
        _assert_member_set(staging_descriptor, expected_members)
        _assert_toolchain_unchanged(
            toolchain,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
        try:
            rename_exclusive(
                source_directory_descriptor=output_root_descriptor,
                source_name=staging_name,
                destination_directory_descriptor=output_root_descriptor,
                destination_name=final_name,
            )
        except FileExistsError:
            _remove_known_directory(
                parent_descriptor=output_root_descriptor,
                directory_descriptor=staging_descriptor,
                name=staging_name,
            )
            staging_created = False
            existing = _existing_publication_result(
                output_root_descriptor=output_root_descriptor,
                normalized_root=normalized_root,
                final_name=final_name,
                expected_members=expected_members,
                plan=plan,
                request=request,
            )
            if existing is None:
                raise AV1ValidationV3Tier1PublicationError(
                    "publication_failed",
                    "AV1 v3 Tier 1 publication race could not be reconciled",
                )
            return existing
        renamed = True
        staging_created = False
        os.fsync(output_root_descriptor)
        final_identity = _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=final_name,
            descriptor=staging_descriptor,
        )
        if final_identity != staging_identity:
            raise AV1ValidationV3Tier1PublicationError(
                "publication_failed",
                "AV1 v3 Tier 1 publication directory changed during publication",
            )
        _assert_member_set(staging_descriptor, expected_members)
        return AV1ValidationV3Tier1PublicationResult(
            directory=normalized_root / final_name,
            plan_path=(
                normalized_root
                / final_name
                / AV1_VALIDATION_V3_TIER1_PLAN_FILENAME
            ),
            request_path=(
                normalized_root
                / final_name
                / AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME
            ),
            plan=plan,
            request=request,
            created=True,
        )
    except BaseException:
        cleanup_error: OSError | AV1ValidationV3Tier1PublicationError | None = None
        if staging_descriptor >= 0 and (staging_created or renamed):
            cleanup_name = final_name if renamed else staging_name
            try:
                _remove_known_directory(
                    parent_descriptor=output_root_descriptor,
                    directory_descriptor=staging_descriptor,
                    name=cleanup_name,
                )
            except (OSError, AV1ValidationV3Tier1PublicationError) as error:
                cleanup_error = error
        elif staging_created:
            try:
                os.rmdir(staging_name, dir_fd=output_root_descriptor)
                os.fsync(output_root_descriptor)
            except OSError as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise AV1ValidationV3Tier1PublicationError(
                "publication_cleanup_failed",
                "AV1 v3 Tier 1 publication cleanup failed",
            ) from cleanup_error
        raise
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)


def _existing_config_publication_result(
    *,
    output_root_descriptor: int,
    normalized_root: Path,
    final_name: str,
    expected_members: dict[str, bytes],
    config_sha256: str,
) -> AV1ValidationV3Tier1ConfigPublicationResult | None:
    if not _assert_existing_member_set(
        output_root_descriptor=output_root_descriptor,
        final_name=final_name,
        expected_members=expected_members,
    ):
        return None
    return AV1ValidationV3Tier1ConfigPublicationResult(
        directory=normalized_root / final_name,
        snapshot_path=(
            normalized_root
            / final_name
            / AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME
        ),
        config_sha256=config_sha256,
        created=False,
    )


def _publish_single_member_artifact_set(
    *,
    output_root_descriptor: int,
    final_name: str,
    expected_members: dict[str, bytes],
) -> bool:
    staging_name = f".{final_name}.{secrets.token_hex(12)}.tmp"
    staging_descriptor = -1
    staging_created = False
    renamed = False
    try:
        os.mkdir(staging_name, 0o700, dir_fd=output_root_descriptor)
        staging_created = True
        os.fsync(output_root_descriptor)
        staging_descriptor = os.open(
            staging_name,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
        staging_identity = _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=staging_name,
            descriptor=staging_descriptor,
        )
        for filename in sorted(expected_members):
            _write_member(
                staging_descriptor,
                filename,
                expected_members[filename],
            )
        os.fsync(staging_descriptor)
        _assert_member_set(staging_descriptor, expected_members)
        try:
            rename_exclusive(
                source_directory_descriptor=output_root_descriptor,
                source_name=staging_name,
                destination_directory_descriptor=output_root_descriptor,
                destination_name=final_name,
            )
        except FileExistsError:
            _remove_known_directory(
                parent_descriptor=output_root_descriptor,
                directory_descriptor=staging_descriptor,
                name=staging_name,
                expected_filenames=frozenset(expected_members),
            )
            staging_created = False
            if not _assert_existing_member_set(
                output_root_descriptor=output_root_descriptor,
                final_name=final_name,
                expected_members=expected_members,
            ):
                raise AV1ValidationV3Tier1PublicationError(
                    "publication_failed",
                    "AV1 v3 Tier 1 config publication race could not be reconciled",
                )
            return False
        renamed = True
        staging_created = False
        os.fsync(output_root_descriptor)
        final_identity = _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=final_name,
            descriptor=staging_descriptor,
        )
        if final_identity != staging_identity:
            raise AV1ValidationV3Tier1PublicationError(
                "publication_failed",
                "AV1 v3 Tier 1 config directory changed during publication",
            )
        _assert_member_set(staging_descriptor, expected_members)
        return True
    except BaseException:
        cleanup_error: OSError | AV1ValidationV3Tier1PublicationError | None = None
        if staging_descriptor >= 0 and (staging_created or renamed):
            cleanup_name = final_name if renamed else staging_name
            try:
                _remove_known_directory(
                    parent_descriptor=output_root_descriptor,
                    directory_descriptor=staging_descriptor,
                    name=cleanup_name,
                    expected_filenames=frozenset(expected_members),
                )
            except (OSError, AV1ValidationV3Tier1PublicationError) as error:
                cleanup_error = error
        elif staging_created:
            try:
                os.rmdir(staging_name, dir_fd=output_root_descriptor)
                os.fsync(output_root_descriptor)
            except OSError as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise AV1ValidationV3Tier1PublicationError(
                "publication_cleanup_failed",
                "AV1 v3 Tier 1 config publication cleanup failed",
            ) from cleanup_error
        raise
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)


def _assert_existing_member_set(
    *,
    output_root_descriptor: int,
    final_name: str,
    expected_members: dict[str, bytes],
) -> bool:
    try:
        path_info = os.stat(
            final_name,
            dir_fd=output_root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(path_info.st_mode):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 config publication target is not a safe directory",
        )
    try:
        descriptor = os.open(
            final_name,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 config publication target could not be opened safely",
        ) from exc
    try:
        _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=final_name,
            descriptor=descriptor,
        )
        _assert_member_set(descriptor, expected_members)
        return True
    finally:
        os.close(descriptor)


def _read_existing_qualification_key(
    *,
    output_root_descriptor: int,
) -> bytes | None:
    try:
        path_info = os.stat(
            AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
            dir_fd=output_root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(path_info.st_mode):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 qualification key target is not a safe directory",
        )
    try:
        descriptor = os.open(
            AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 qualification key directory could not be opened safely",
        ) from exc
    try:
        _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
            descriptor=descriptor,
        )
        names = frozenset(os.listdir(descriptor))
        expected_names = frozenset({
            AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME,
        })
        if names != expected_names:
            reason_code = (
                "artifact_incomplete"
                if names < expected_names
                else "artifact_malformed"
            )
            raise AV1ValidationV3Tier1PublicationError(
                reason_code,
                "AV1 v3 qualification key artifact set is invalid",
            )
        qualification_key = _read_member(
            descriptor,
            AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME,
            expected_size=32,
        )
        try:
            av1_validation_v3_qualification_key_id(qualification_key)
        except AV1ValidationV3Error as exc:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_malformed",
                "AV1 v3 qualification key is invalid",
            ) from exc
        return qualification_key
    finally:
        os.close(descriptor)


def _qualification_key_publication_result(
    *,
    normalized_root: Path,
    qualification_key: bytes,
    created: bool,
) -> AV1ValidationV3QualificationKeyPublicationResult:
    try:
        qualification_key_id = av1_validation_v3_qualification_key_id(
            qualification_key
        )
    except AV1ValidationV3Error as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 qualification key is invalid",
        ) from exc
    directory = normalized_root / AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY
    return AV1ValidationV3QualificationKeyPublicationResult(
        directory=directory,
        key_path=directory / AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME,
        qualification_key_id=qualification_key_id,
        created=created,
    )


def _write_member(
    directory_descriptor: int,
    filename: str,
    content: bytes,
) -> None:
    descriptor = os.open(
        filename,
        _WRITE_FLAGS,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        initial = _assert_file_binding(
            parent_descriptor=directory_descriptor,
            name=filename,
            descriptor=descriptor,
            expected_size=0,
        )
        offset = 0
        while offset < len(content):
            count = os.write(descriptor, content[offset:])
            if count <= 0:
                raise OSError("AV1 v3 Tier 1 artifact write did not progress")
            offset += count
        fsync_durable_file(descriptor)
        completed = _assert_file_binding(
            parent_descriptor=directory_descriptor,
            name=filename,
            descriptor=descriptor,
            expected_size=len(content),
        )
        if completed != initial:
            raise AV1ValidationV3Tier1PublicationError(
                "publication_failed",
                "AV1 v3 Tier 1 artifact identity changed during write",
            )
    finally:
        os.close(descriptor)


def _existing_publication_result(
    *,
    output_root_descriptor: int,
    normalized_root: Path,
    final_name: str,
    expected_members: dict[str, bytes],
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
) -> AV1ValidationV3Tier1PublicationResult | None:
    try:
        path_info = os.stat(
            final_name,
            dir_fd=output_root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(path_info.st_mode):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication target is not a safe directory",
        )
    try:
        descriptor = os.open(
            final_name,
            _DIRECTORY_FLAGS,
            dir_fd=output_root_descriptor,
        )
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication target could not be opened safely",
        ) from exc
    try:
        _assert_directory_binding(
            parent_descriptor=output_root_descriptor,
            name=final_name,
            descriptor=descriptor,
        )
        _assert_member_set(descriptor, expected_members)
        return AV1ValidationV3Tier1PublicationResult(
            directory=normalized_root / final_name,
            plan_path=(
                normalized_root
                / final_name
                / AV1_VALIDATION_V3_TIER1_PLAN_FILENAME
            ),
            request_path=(
                normalized_root
                / final_name
                / AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME
            ),
            plan=plan,
            request=request,
            created=False,
        )
    finally:
        os.close(descriptor)


def _assert_member_set(
    directory_descriptor: int,
    expected_members: dict[str, bytes],
) -> None:
    names = frozenset(os.listdir(directory_descriptor))
    expected_names = frozenset(expected_members)
    if names != expected_names:
        reason_code = (
            "artifact_incomplete"
            if names < expected_names
            else "artifact_malformed"
        )
        raise AV1ValidationV3Tier1PublicationError(
            reason_code,
            "AV1 v3 Tier 1 publication artifact set is invalid",
        )
    for filename in sorted(expected_members):
        actual = _read_member(
            directory_descriptor,
            filename,
            expected_size=len(expected_members[filename]),
        )
        if actual != expected_members[filename]:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_conflict",
                "AV1 v3 Tier 1 publication artifact conflicts with preparation",
            )


def _read_member(
    directory_descriptor: int,
    filename: str,
    *,
    expected_size: int,
) -> bytes:
    try:
        descriptor = os.open(
            filename,
            _READ_FLAGS,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication artifact could not be opened safely",
        ) from exc
    try:
        identity = _assert_file_binding(
            parent_descriptor=directory_descriptor,
            name=filename,
            descriptor=descriptor,
            expected_size=expected_size,
        )
        content = bytearray()
        while len(content) < expected_size:
            chunk = os.read(descriptor, expected_size - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if os.read(descriptor, 1) or len(content) != expected_size:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_malformed",
                "AV1 v3 Tier 1 publication artifact size is invalid",
            )
        if _assert_file_binding(
            parent_descriptor=directory_descriptor,
            name=filename,
            descriptor=descriptor,
            expected_size=expected_size,
        ) != identity:
            raise AV1ValidationV3Tier1PublicationError(
                "artifact_unsafe",
                "AV1 v3 Tier 1 publication artifact changed while reading",
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _assert_directory_binding(
    *,
    parent_descriptor: int,
    name: str,
    descriptor: int,
) -> tuple[int, int]:
    descriptor_info = os.fstat(descriptor)
    path_info = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(descriptor_info.st_mode)
        or not stat.S_ISDIR(path_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) != 0o700
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
    ):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication directory is unsafe",
        )
    return descriptor_info.st_dev, descriptor_info.st_ino


def _assert_file_binding(
    *,
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected_size: int,
) -> tuple[int, int]:
    descriptor_info = os.fstat(descriptor)
    path_info = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(descriptor_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or descriptor_info.st_uid != os.getuid()
        or stat.S_IMODE(descriptor_info.st_mode) != 0o600
        or descriptor_info.st_nlink != 1
        or descriptor_info.st_size != expected_size
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
    ):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_unsafe",
            "AV1 v3 Tier 1 publication artifact is unsafe",
        )
    return descriptor_info.st_dev, descriptor_info.st_ino


def _remove_known_directory(
    *,
    parent_descriptor: int,
    directory_descriptor: int,
    name: str,
    expected_filenames: frozenset[str] = _ARTIFACT_FILENAMES,
) -> None:
    names = frozenset(os.listdir(directory_descriptor))
    if not names <= expected_filenames:
        raise AV1ValidationV3Tier1PublicationError(
            "publication_cleanup_failed",
            "AV1 v3 Tier 1 publication directory contains unexpected entries",
        )
    for filename in sorted(expected_filenames):
        try:
            os.unlink(filename, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
    if os.listdir(directory_descriptor):
        raise AV1ValidationV3Tier1PublicationError(
            "publication_cleanup_failed",
            "AV1 v3 Tier 1 publication directory contains unexpected entries",
        )
    os.fsync(directory_descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
