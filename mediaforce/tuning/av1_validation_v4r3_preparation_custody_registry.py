"""Owner-only registry and key-custody operation for AV1 protocol-v4 revision 3."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    av1_v4r3_path_privacy_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    AV1V4R3PreparationCustodyError,
    assert_av1_v4r3_path_privacy_key_custody,
    assert_av1_v4r3_preparation_claim,
    assert_av1_v4r3_preparation_registry_binding,
    build_av1_v4r3_path_privacy_key_custody,
    build_av1_v4r3_preparation_claim,
    build_av1_v4r3_preparation_registry_binding,
    deserialize_av1_v4r3_path_privacy_key_custody,
    deserialize_av1_v4r3_preparation_claim,
    deserialize_av1_v4r3_preparation_registry_binding,
    serialize_av1_v4r3_path_privacy_key_custody,
    serialize_av1_v4r3_preparation_claim,
    serialize_av1_v4r3_preparation_registry_binding,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    AV1V4R3PreparationGrantError,
    assert_av1_v4r3_preparation_grant_active,
    build_av1_v4r3_preparation_grant,
    deserialize_av1_v4r3_preparation_grant,
    serialize_av1_v4r3_preparation_grant,
)


Clock = Callable[[], datetime]

_PROCESS_LOCK = threading.RLock()
_LOCK_NAME = ".registry.lock"
_BINDING_NAME = "registry-binding.json"
_GRANT_NAME = "preparation-grant.json"
_CLAIM_NAME = "preparation-claim.json"
_KEY_NAME = "path-privacy.key"
_CUSTODY_NAME = "path-privacy-key-custody.json"
_ATTEMPT_NAME = "preparation-attempt-started.json"
_CONFIG_NAME = "effective-config.json"
_BUNDLE_NAME = "preparation-bundle.json"
_MEASUREMENT_NAME = "preparation-terminal-measurement.json"
_FREEZE_NAME = "owner-freeze.json"
_REQUEST_NAME = "qualification-request.json"
_MAX_JSON_BYTES = 256 * 1024
_TEMP_SUFFIX = ".tmp"
_ARTIFACT_NAMES = frozenset(
    {
        _BINDING_NAME,
        _GRANT_NAME,
        _CLAIM_NAME,
        _KEY_NAME,
        _CUSTODY_NAME,
        _ATTEMPT_NAME,
        _CONFIG_NAME,
        _BUNDLE_NAME,
        _MEASUREMENT_NAME,
        _FREEZE_NAME,
        _REQUEST_NAME,
    }
)


class AV1V4R3PreparationCustodyRegistryError(RuntimeError):
    """Raised when owner-only revision-3 preparation custody fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R3PreparationCustodyRegistryBinding:
    """Private paths that never enter a canonical custody artifact."""

    registry: Path
    repository_root: Path


@dataclass(frozen=True, slots=True)
class AV1V4R3PreparationGrantPublication:
    grant: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R3PreparationCustodyResult:
    grant: Mapping[str, Any]
    claim: Mapping[str, Any]
    key_custody: Mapping[str, Any]
    artifact_paths: Mapping[str, Path]


def assert_av1_v4r3_preparation_custody_registry(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> None:
    """Verify one canonical private registry outside its repository."""

    registry, _repository_root = _normalized_binding_paths(binding)
    dir_fd = _open_registry(registry)
    try:
        _assert_registry_fd(dir_fd)
    finally:
        os.close(dir_fd)


def assert_av1_v4r3_preparation_custody_file(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    filename: str,
) -> None:
    """Verify one owner-only registry file by descriptor-relative custody."""

    with _locked_registry(binding) as context:
        context.assert_file_custody(filename)


def publish_av1_v4r3_preparation_grant(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    authorized_at: str,
    valid_until: str,
    clock: Clock,
) -> AV1V4R3PreparationGrantPublication:
    """Publish or return the one canonical grant singleton for this registry."""

    with _locked_registry(binding) as context:
        now = _clock_timestamp(clock)
        try:
            grant = build_av1_v4r3_preparation_grant(
                rights_attestation=rights_attestation,
                owner_principal=owner_principal,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
                authorized_at=authorized_at,
                valid_until=valid_until,
            )
            assert_av1_v4r3_preparation_grant_active(grant, as_of=now)
        except AV1V4R3PreparationGrantError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation grant publication input is invalid"
            ) from exc
        grant_bytes = serialize_av1_v4r3_preparation_grant(grant)
        marker = build_av1_v4r3_preparation_registry_binding()
        marker_bytes = serialize_av1_v4r3_preparation_registry_binding(marker)
        context.publish_singleton(
            _BINDING_NAME,
            marker_bytes,
            deserialize_av1_v4r3_preparation_registry_binding,
            "registry binding",
        )
        if (
            context.exists(_CLAIM_NAME)
            or context.exists(_KEY_NAME)
            or context.exists(_CUSTODY_NAME)
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation registry already consumed its grant"
            )
        created = not context.exists(_GRANT_NAME)
        canonical = context.publish_singleton(
            _GRANT_NAME,
            grant_bytes,
            deserialize_av1_v4r3_preparation_grant,
            "preparation grant",
        )
    return AV1V4R3PreparationGrantPublication(grant=canonical, created=created)


def consume_av1_v4r3_preparation_grant(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    clock: Clock,
) -> AV1V4R3PreparationCustodyResult:
    """Consume the registry grant once and create one private path-privacy key."""

    with _locked_registry(binding) as context:
        claimed_at = _clock_timestamp(clock)
        context.load_binding()
        grant = context.load_grant()
        try:
            assert_av1_v4r3_preparation_grant_active(grant, as_of=claimed_at)
            claim = build_av1_v4r3_preparation_claim(
                preparation_grant=grant,
                rights_attestation=rights_attestation,
                claimed_at=claimed_at,
            )
        except (AV1V4R3PreparationGrantError, AV1V4R3PreparationCustodyError) as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation grant cannot be consumed"
            ) from exc
        if context.exists(_CLAIM_NAME):
            context.reconcile_consumed_claim()
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation grant has already been consumed"
            )
        if context.exists(_KEY_NAME) or context.exists(_CUSTODY_NAME):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation registry state is inconsistent"
            )
        context.write_exclusive(
            _CLAIM_NAME,
            serialize_av1_v4r3_preparation_claim(claim),
        )
        key_created = False
        custody_created = False
        try:
            key = secrets.token_bytes(32)
            if len(key) != 32:
                raise AV1V4R3PreparationCustodyRegistryError(
                    "AV1 v4 r3 path-privacy key generation failed"
                )
            key_id = av1_v4r3_path_privacy_key_id(key)
            context.write_key(key)
            key_created = True
            custody = build_av1_v4r3_path_privacy_key_custody(
                preparation_claim=claim,
                key_id=key_id,
                created_at=claimed_at,
            )
            context.write_exclusive(
                _CUSTODY_NAME,
                serialize_av1_v4r3_path_privacy_key_custody(custody),
            )
            custody_created = True
            context.assert_file_custody(_KEY_NAME, expected_size=32)
            context.assert_file_custody(_CUSTODY_NAME)
        except BaseException as exc:
            rollback_errors: list[OSError] = []
            if custody_created:
                context.unlink_owned(_CUSTODY_NAME, rollback_errors)
            if key_created:
                context.unlink_owned(_KEY_NAME, rollback_errors)
            if rollback_errors:
                raise AV1V4R3PreparationCustodyRegistryError(
                    "AV1 v4 r3 preparation custody failed after consuming the grant "
                    "and rollback was incomplete"
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation custody failed after consuming the grant"
            ) from exc
        return AV1V4R3PreparationCustodyResult(
            grant=grant,
            claim=claim,
            key_custody=custody,
            artifact_paths={
                "registry_binding": context.registry / _BINDING_NAME,
                "preparation_grant": context.registry / _GRANT_NAME,
                "preparation_claim": context.registry / _CLAIM_NAME,
                "path_privacy_key": context.registry / _KEY_NAME,
                "path_privacy_key_custody": context.registry / _CUSTODY_NAME,
            },
        )


def load_av1_v4r3_preparation_claim(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> dict[str, Any] | None:
    with _locked_registry(binding) as context:
        if not context.exists(_CLAIM_NAME):
            return None
        try:
            return deserialize_av1_v4r3_preparation_claim(context.read(_CLAIM_NAME))
        except AV1V4R3PreparationCustodyError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation claim registry artifact is invalid"
            ) from exc


def load_av1_v4r3_path_privacy_key_custody(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> dict[str, Any] | None:
    with _locked_registry(binding) as context:
        if not context.exists(_CUSTODY_NAME):
            return None
        try:
            return deserialize_av1_v4r3_path_privacy_key_custody(
                context.read(_CUSTODY_NAME)
            )
        except AV1V4R3PreparationCustodyError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 key custody registry artifact is invalid"
            ) from exc


@dataclass(slots=True)
class _RegistryContext:
    registry: Path
    dir_fd: int

    def exists(self, filename: str) -> bool:
        _validate_filename(filename)
        try:
            os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def assert_file_custody(
        self,
        filename: str,
        *,
        expected_size: int | None = None,
        allowed_link_counts: tuple[int, ...] = (1,),
    ) -> os.stat_result:
        descriptor = self._open_read(filename)
        try:
            metadata = os.fstat(descriptor)
            _assert_regular_file_custody(
                metadata,
                filename,
                allowed_link_counts=allowed_link_counts,
            )
            if expected_size is not None and metadata.st_size != expected_size:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 preparation {filename} size is invalid"
                )
            if metadata.st_size > _MAX_JSON_BYTES:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 preparation {filename} is oversized"
                )
            return metadata
        finally:
            os.close(descriptor)

    def read(self, filename: str) -> bytes:
        descriptor = self._open_read(filename)
        try:
            metadata = os.fstat(descriptor)
            _assert_regular_file_custody(metadata, filename)
            if metadata.st_size > _MAX_JSON_BYTES:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 preparation {filename} is oversized"
                )
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise AV1V4R3PreparationCustodyRegistryError(
                        f"AV1 v4 r3 preparation {filename} is truncated"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if not _same_file_snapshot(metadata, after):
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 preparation {filename} changed during read"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def publish_singleton(
        self,
        filename: str,
        data: bytes,
        loader: Callable[[bytes], dict[str, Any]],
        label: str,
    ) -> dict[str, Any]:
        if self.exists(filename):
            existing_bytes = self.read(filename)
            try:
                existing = loader(existing_bytes)
            except (
                AV1V4R3PreparationCustodyError,
                AV1V4R3PreparationGrantError,
            ) as exc:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 {label} singleton is invalid"
                ) from exc
            if existing_bytes != data:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 {label} conflicts with the registry singleton"
                )
            return existing
        self.write_exclusive(filename, data)
        return loader(data)

    def write_exclusive(self, filename: str, data: bytes) -> None:
        _validate_filename(filename)
        if len(data) > _MAX_JSON_BYTES:
            raise AV1V4R3PreparationCustodyRegistryError(
                f"AV1 v4 r3 preparation {filename} publication is oversized"
            )
        temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag()
        descriptor: int | None = None
        try:
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=self.dir_fd)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, data)
            os.fsync(descriptor)
            _assert_regular_file_custody(os.fstat(descriptor), temp_name)
        except BaseException as exc:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            try:
                os.unlink(temp_name, dir_fd=self.dir_fd)
                os.fsync(self.dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AV1V4R3PreparationCustodyRegistryError(
                f"AV1 v4 r3 preparation {filename} temporary publication failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        linked = False
        try:
            os.link(
                temp_name,
                filename,
                src_dir_fd=self.dir_fd,
                dst_dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(self.dir_fd)
            os.unlink(temp_name, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
            self.assert_file_custody(filename)
        except BaseException as exc:
            cleanup_failed = False
            if linked:
                try:
                    os.unlink(filename, dir_fd=self.dir_fd)
                except OSError:
                    cleanup_failed = True
            try:
                os.unlink(temp_name, dir_fd=self.dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_failed = True
            try:
                os.fsync(self.dir_fd)
            except OSError:
                cleanup_failed = True
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if cleanup_failed:
                raise AV1V4R3PreparationCustodyRegistryError(
                    f"AV1 v4 r3 preparation {filename} publication failed and cleanup was incomplete"
                ) from exc
            raise AV1V4R3PreparationCustodyRegistryError(
                f"AV1 v4 r3 preparation {filename} publication failed"
            ) from exc

    def cleanup_stale_temps(self) -> None:
        removed = False
        for name in os.listdir(self.dir_fd):
            final_name = _temp_final_name(name)
            if final_name is None:
                continue
            metadata = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            _assert_regular_file_custody(
                metadata,
                name,
                allowed_link_counts=(1, 2),
            )
            if metadata.st_nlink == 2:
                final_metadata = self.assert_file_custody(
                    final_name,
                    allowed_link_counts=(2,),
                )
                if not _same_inode(metadata, final_metadata):
                    raise AV1V4R3PreparationCustodyRegistryError(
                        "AV1 v4 r3 preparation stale temporary file conflicts with its final artifact"
                    )
            os.unlink(name, dir_fd=self.dir_fd)
            removed = True
        if removed:
            os.fsync(self.dir_fd)

    def write_key(self, key: bytes) -> None:
        self.write_exclusive(_KEY_NAME, key)
        self.assert_file_custody(_KEY_NAME, expected_size=32)

    def reconcile_consumed_claim(self) -> None:
        key_exists = self.exists(_KEY_NAME)
        custody_exists = self.exists(_CUSTODY_NAME)
        if custody_exists and not key_exists:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation consumed registry is missing its key"
            )
        if not key_exists or custody_exists:
            return
        rollback_errors: list[OSError] = []
        self.unlink_owned(_KEY_NAME, rollback_errors)
        if rollback_errors:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation orphan key cleanup failed"
            )

    def unlink_owned(self, filename: str, errors: list[OSError]) -> None:
        if not self.exists(filename):
            return
        try:
            self.assert_file_custody(filename)
            os.unlink(filename, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except OSError as exc:
            errors.append(exc)
        except AV1V4R3PreparationCustodyRegistryError as exc:
            errors.append(OSError(str(exc)))

    def load_binding(self) -> dict[str, Any]:
        try:
            marker = deserialize_av1_v4r3_preparation_registry_binding(
                self.read(_BINDING_NAME)
            )
            assert_av1_v4r3_preparation_registry_binding(marker)
            return marker
        except (AV1V4R3PreparationCustodyError, FileNotFoundError) as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation registry binding is invalid"
            ) from exc

    def load_grant(self) -> dict[str, Any]:
        try:
            return deserialize_av1_v4r3_preparation_grant(self.read(_GRANT_NAME))
        except (AV1V4R3PreparationGrantError, FileNotFoundError) as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation grant is unavailable"
            ) from exc

    def _open_read(self, filename: str) -> int:
        _validate_filename(filename)
        flags = os.O_RDONLY | _no_follow_flag()
        try:
            return os.open(filename, flags, dir_fd=self.dir_fd)
        except OSError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                f"AV1 v4 r3 preparation {filename} is unavailable"
            ) from exc


@contextmanager
def _locked_registry(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> Iterator[_RegistryContext]:
    registry, _repository_root = _normalized_binding_paths(binding)
    with _PROCESS_LOCK:
        dir_fd = _open_registry(registry)
        lock_fd: int | None = None
        try:
            _assert_registry_fd(dir_fd)
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | _no_follow_flag(),
                0o600,
                dir_fd=dir_fd,
            )
            os.fchmod(lock_fd, 0o600)
            _assert_regular_file_custody(os.fstat(lock_fd), _LOCK_NAME)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            context = _RegistryContext(registry=registry, dir_fd=dir_fd)
            context.cleanup_stale_temps()
            yield context
        except AV1V4R3PreparationCustodyRegistryError:
            raise
        except OSError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation registry operation failed"
            ) from exc
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(dir_fd)


def _normalized_binding_paths(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> tuple[Path, Path]:
    if not isinstance(binding, AV1V4R3PreparationCustodyRegistryBinding):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry binding is invalid"
        )
    registry = _canonical_existing_directory(binding.registry, "registry")
    repository_root = _canonical_existing_directory(
        binding.repository_root, "repository root"
    )
    if (
        registry == repository_root
        or repository_root in registry.parents
        or registry in repository_root.parents
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry must be outside and not contain the repository"
        )
    return registry, repository_root


def _canonical_existing_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise AV1V4R3PreparationCustodyRegistryError(
            f"AV1 v4 r3 preparation {label} must be an absolute path"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            f"AV1 v4 r3 preparation {label} is unavailable"
        ) from exc
    if resolved != path or not resolved.is_dir():
        raise AV1V4R3PreparationCustodyRegistryError(
            f"AV1 v4 r3 preparation {label} must be a canonical real directory"
        )
    return resolved


def _open_registry(registry: Path) -> int:
    try:
        return os.open(registry, os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag())
    except OSError as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry is unavailable"
        ) from exc


def _assert_registry_fd(dir_fd: int) -> None:
    metadata = os.fstat(dir_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry custody is invalid"
        )


def _assert_regular_file_custody(
    metadata: os.stat_result,
    filename: str,
    *,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink not in allowed_link_counts
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            f"AV1 v4 r3 preparation {filename} custody is invalid"
        )


def _validate_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\x00" in filename
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry filename is invalid"
        )


def _temp_final_name(filename: str) -> str | None:
    if not filename.startswith(".") or not filename.endswith(_TEMP_SUFFIX):
        return None
    body = filename[1 : -len(_TEMP_SUFFIX)]
    parts = body.rsplit(".", 2)
    if (
        len(parts) != 3
        or parts[0] not in _ARTIFACT_NAMES
        or not parts[1].isdigit()
        or len(parts[2]) != 16
    ):
        return None
    try:
        int(parts[2], 16)
    except ValueError:
        return None
    return parts[0]


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_inode(first, second)
        and first.st_uid == second.st_uid
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _write_all(descriptor: int, data: bytes) -> None:
    if not isinstance(data, bytes):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation registry writes require bytes"
        )
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _clock_timestamp(clock: Clock) -> str:
    if not callable(clock):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation clock is invalid"
        )
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation clock must return an aware datetime"
        )
    normalized = value.astimezone(UTC)
    if normalized.microsecond != 0:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation clock must use whole seconds"
        )
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)
