"""Owner-only preparation registry for AV1 protocol-v4 revision 4."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4r4_preparation import (
    AV1V4R4PreparationError,
    assert_av1_v4r4_path_privacy_key_custody,
    assert_av1_v4r4_preparation_claim,
    assert_av1_v4r4_preparation_grant,
    assert_av1_v4r4_preparation_grant_active,
    assert_av1_v4r4_preparation_registry_binding,
    av1_v4r4_path_privacy_key_id,
    build_av1_v4r4_path_privacy_key_custody,
    build_av1_v4r4_preparation_claim,
    build_av1_v4r4_preparation_grant,
    build_av1_v4r4_preparation_registry_binding,
    deserialize_av1_v4r4_path_privacy_key_custody,
    deserialize_av1_v4r4_preparation_claim,
    deserialize_av1_v4r4_preparation_grant,
    deserialize_av1_v4r4_preparation_registry_binding,
    serialize_av1_v4r4_path_privacy_key_custody,
    serialize_av1_v4r4_preparation_claim,
    serialize_av1_v4r4_preparation_grant,
    serialize_av1_v4r4_preparation_registry_binding,
)


Clock = Callable[[], datetime]

_PROCESS_LOCK = threading.RLock()
_LOCK_NAME = "v4r4-registry.lock"
_BINDING_NAME = "v4r4-registry-binding.json"
_GRANT_NAME = "v4r4-preparation-grant.json"
_CLAIM_NAME = "v4r4-preparation-claim.json"
_KEY_NAME = "v4r4-path-privacy.key"
_CUSTODY_NAME = "v4r4-path-privacy-key-custody.json"
_CUSTODY_ATTEMPT_NAME = "v4r4-preparation-custody-attempt.json"
_ATTEMPT_NAME = "v4r4-preparation-attempt-started.json"
_CONFIG_NAME = "v4r4-effective-config.json"
_BUNDLE_NAME = "v4r4-preparation-bundle.json"
_MEASUREMENT_NAME = "v4r4-preparation-terminal-measurement.json"
_FREEZE_NAME = "v4r4-owner-freeze.json"
_REQUEST_NAME = "v4r4-qualification-request.json"
_PREFLIGHT_NAME = "v4r4-execution-preflight.json"
_MAX_JSON_BYTES = 256 * 1024
_TEMP_SUFFIX = ".tmp"
_ARTIFACT_NAMES = frozenset(
    {
        _BINDING_NAME,
        _GRANT_NAME,
        _CLAIM_NAME,
        _KEY_NAME,
        _CUSTODY_NAME,
        _CUSTODY_ATTEMPT_NAME,
        _ATTEMPT_NAME,
        _CONFIG_NAME,
        _BUNDLE_NAME,
        _MEASUREMENT_NAME,
        _FREEZE_NAME,
        _REQUEST_NAME,
        _PREFLIGHT_NAME,
    }
)
_V4R3_ARTIFACT_NAMES = frozenset(
    {
        ".registry.lock",
        "registry-binding.json",
        "preparation-grant.json",
        "preparation-claim.json",
        "path-privacy.key",
        "path-privacy-key-custody.json",
        "preparation-attempt-started.json",
        "effective-config.json",
        "preparation-bundle.json",
        "preparation-terminal-measurement.json",
        "owner-freeze.json",
        "qualification-request.json",
        "execution-preflight.json",
    }
)


class AV1V4R4PreparationRegistryError(RuntimeError):
    """Raised when revision-4 preparation registry custody fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R4PreparationRegistryBinding:
    registry: Path
    repository_root: Path


@dataclass(frozen=True, slots=True)
class AV1V4R4PreparationGrantPublication:
    grant: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R4PreparationCustodyResult:
    grant: Mapping[str, Any]
    claim: Mapping[str, Any]
    key_custody: Mapping[str, Any]
    artifact_paths: Mapping[str, Path]


def initialize_av1_v4r4_preparation_registry(
    binding: AV1V4R4PreparationRegistryBinding,
) -> None:
    if not isinstance(binding, AV1V4R4PreparationRegistryBinding):
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry binding is invalid"
        )
    registry_path = Path(binding.registry)
    if not registry_path.is_absolute():
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry must be an absolute path"
        )
    repository_root = _canonical_directory(
        binding.repository_root, "repository root", create_missing=False
    )
    created = False
    try:
        registry_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry initialization failed"
        ) from exc
    registry = _canonical_directory(registry_path, "registry", create_missing=False)
    if (
        registry == repository_root
        or _is_descendant(registry, repository_root)
        or _is_descendant(repository_root, registry)
    ):
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry must be outside and not contain the repository"
        )
    try:
        _assert_registry_directory_discriminant(registry)
        metadata = registry.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry custody is invalid"
            )
    except OSError as exc:
        if created:
            try:
                registry.rmdir()
            except OSError:
                pass
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry initialization failed"
        ) from exc
    assert_av1_v4r4_preparation_registry(binding)


def assert_av1_v4r4_preparation_registry(
    binding: AV1V4R4PreparationRegistryBinding,
) -> None:
    registry, _repository_root = _normalized_binding_paths(binding)
    dir_fd = _open_registry(registry)
    try:
        _assert_registry_directory_discriminant_fd(dir_fd)
        _assert_registry_fd(dir_fd)
    finally:
        os.close(dir_fd)


def assert_av1_v4r4_preparation_registry_file_custody(
    binding: AV1V4R4PreparationRegistryBinding,
    filename: str,
) -> None:
    with _locked_registry(binding) as context:
        context.assert_file_custody(filename)


def publish_av1_v4r4_preparation_grant(
    *,
    binding: AV1V4R4PreparationRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    authorized_at: str,
    valid_until: str,
    clock: Clock,
) -> AV1V4R4PreparationGrantPublication:
    with _locked_registry(binding) as context:
        now = _clock_timestamp(clock)
        try:
            grant = build_av1_v4r4_preparation_grant(
                rights_attestation=rights_attestation,
                owner_principal=owner_principal,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
                authorized_at=authorized_at,
                valid_until=valid_until,
            )
            assert_av1_v4r4_preparation_grant_active(grant, as_of=now)
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation grant publication input is invalid"
            ) from exc
        binding_marker = build_av1_v4r4_preparation_registry_binding()
        context.publish_singleton(
            _BINDING_NAME,
            serialize_av1_v4r4_preparation_registry_binding(binding_marker),
            deserialize_av1_v4r4_preparation_registry_binding,
            "registry binding",
        )
        if (
            context.exists(_CLAIM_NAME)
            or context.exists(_KEY_NAME)
            or context.exists(_CUSTODY_NAME)
        ):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry already consumed its grant"
            )
        created = not context.exists(_GRANT_NAME)
        canonical = context.publish_singleton(
            _GRANT_NAME,
            serialize_av1_v4r4_preparation_grant(grant),
            deserialize_av1_v4r4_preparation_grant,
            "preparation grant",
        )
    return AV1V4R4PreparationGrantPublication(grant=canonical, created=created)


def consume_av1_v4r4_preparation_grant(
    *,
    binding: AV1V4R4PreparationRegistryBinding,
    rights_attestation: Mapping[str, Any],
    clock: Clock,
) -> AV1V4R4PreparationCustodyResult:
    with _locked_registry(binding) as context:
        context.reconcile_custody_attempt()
        claimed_at = _clock_timestamp(clock)
        context.load_binding()
        grant = context.load_grant()
        try:
            assert_av1_v4r4_preparation_grant_active(grant, as_of=claimed_at)
            claim = build_av1_v4r4_preparation_claim(
                preparation_grant=grant,
                rights_attestation=rights_attestation,
                claimed_at=claimed_at,
            )
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation grant cannot be consumed"
            ) from exc
        if context.exists(_CLAIM_NAME):
            context.reconcile_consumed_claim()
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation grant has already been consumed"
            )
        if context.exists(_KEY_NAME) or context.exists(_CUSTODY_NAME):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry state is inconsistent"
            )
        context.write_exclusive(_CUSTODY_ATTEMPT_NAME, _custody_attempt_bytes(claim))
        key_created = False
        custody_created = False
        claim_created = False
        try:
            key = secrets.token_bytes(32)
            if len(key) != 32:
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 path-privacy key generation failed"
                )
            context.write_key(key)
            key_created = True
            custody = build_av1_v4r4_path_privacy_key_custody(
                preparation_claim=claim,
                key_id=av1_v4r4_path_privacy_key_id(key),
                created_at=claimed_at,
            )
            context.write_exclusive(
                _CUSTODY_NAME,
                serialize_av1_v4r4_path_privacy_key_custody(custody),
            )
            custody_created = True
            context.assert_file_custody(_KEY_NAME, expected_size=32)
            context.assert_file_custody(_CUSTODY_NAME)
            context.write_exclusive(
                _CLAIM_NAME,
                serialize_av1_v4r4_preparation_claim(claim),
            )
            claim_created = True
            cleanup_errors: list[OSError] = []
            context.unlink_owned(_CUSTODY_ATTEMPT_NAME, cleanup_errors)
            if cleanup_errors:
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation custody committed with pending recovery"
                )
        except BaseException as exc:
            if context.exists(_CLAIM_NAME):
                try:
                    visible_claim = deserialize_av1_v4r4_preparation_claim(
                        context.read(_CLAIM_NAME)
                    )
                    if visible_claim != claim:
                        raise AV1V4R4PreparationRegistryError(
                            "AV1 v4 r4 preparation visible custody claim conflicts"
                        )
                    context.reconcile_consumed_claim()
                except AV1V4R4PreparationError as claim_exc:
                    raise AV1V4R4PreparationRegistryError(
                        "AV1 v4 r4 preparation custody claim publication is ambiguous"
                    ) from claim_exc
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation custody committed with pending recovery"
                ) from exc
            if claim_created:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation custody committed with pending recovery"
                ) from exc
            rollback_errors: list[OSError] = []
            if custody_created:
                context.unlink_owned(_CUSTODY_NAME, rollback_errors)
            if key_created:
                context.unlink_owned(_KEY_NAME, rollback_errors)
            context.unlink_owned(_CUSTODY_ATTEMPT_NAME, rollback_errors)
            if rollback_errors:
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation custody failed after consuming the grant "
                    "and rollback was incomplete"
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation custody failed after consuming the grant"
            ) from exc
        return AV1V4R4PreparationCustodyResult(
            grant=grant,
            claim=claim,
            key_custody=custody,
            artifact_paths={
                "preparation_grant": context.registry / _GRANT_NAME,
                "preparation_claim": context.registry / _CLAIM_NAME,
                "path_privacy_key": context.registry / _KEY_NAME,
                "key_custody": context.registry / _CUSTODY_NAME,
            },
        )


def load_av1_v4r4_preparation_claim(
    binding: AV1V4R4PreparationRegistryBinding,
) -> dict[str, Any] | None:
    with _locked_registry(binding) as context:
        if not context.exists(_CLAIM_NAME):
            return None
        try:
            return deserialize_av1_v4r4_preparation_claim(context.read(_CLAIM_NAME))
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation claim registry artifact is invalid"
            ) from exc


def load_av1_v4r4_path_privacy_key_custody(
    binding: AV1V4R4PreparationRegistryBinding,
) -> dict[str, Any] | None:
    with _locked_registry(binding) as context:
        if not context.exists(_CUSTODY_NAME):
            return None
        try:
            return deserialize_av1_v4r4_path_privacy_key_custody(
                context.read(_CUSTODY_NAME)
            )
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 key custody registry artifact is invalid"
            ) from exc


@dataclass(slots=True)
class _RegistryContext:
    binding: AV1V4R4PreparationRegistryBinding
    dir_fd: int
    registry: Path

    def exists(self, filename: str) -> bool:
        _assert_supported_filename(filename)
        try:
            os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} is unavailable"
            ) from exc
        return True

    def read(self, filename: str) -> bytes:
        _assert_supported_filename(filename)
        descriptor = -1
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self.dir_fd,
            )
            metadata = os.fstat(descriptor)
            _assert_owned_regular_file(metadata, filename)
            if metadata.st_size < 0 or metadata.st_size > _MAX_JSON_BYTES:
                raise AV1V4R4PreparationRegistryError(
                    f"AV1 v4 r4 preparation {filename} is oversized"
                )
            data = b""
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                data += chunk
                if len(data) > _MAX_JSON_BYTES:
                    raise AV1V4R4PreparationRegistryError(
                        f"AV1 v4 r4 preparation {filename} is oversized"
                    )
            after = os.fstat(descriptor)
            if not _same_file_snapshot(metadata, after):
                raise AV1V4R4PreparationRegistryError(
                    f"AV1 v4 r4 preparation {filename} changed during read"
                )
            return data
        except FileNotFoundError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} is unavailable"
            ) from exc
        except OSError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} is unavailable"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def assert_file_custody(self, filename: str, expected_size: int | None = None) -> None:
        _assert_supported_filename(filename)
        try:
            metadata = os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} custody is invalid"
            ) from exc
        except OSError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} custody is invalid"
            ) from exc
        _assert_owned_regular_file(metadata, filename)
        if expected_size is not None and metadata.st_size != expected_size:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} size is invalid"
            )

    def write(self, filename: str, data: bytes) -> None:
        self._publish_candidate(filename, data, replace=True)

    def write_exclusive(self, filename: str, data: bytes) -> None:
        self._publish_candidate(filename, data, replace=False)

    def write_key(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation key material is invalid"
            )
        self._publish_candidate(_KEY_NAME, key, replace=False, max_bytes=32)
        self.assert_file_custody(_KEY_NAME, expected_size=32)

    def publish_singleton(
        self,
        filename: str,
        data: bytes,
        loader: Callable[[bytes], dict[str, Any]],
        label: str,
    ) -> dict[str, Any]:
        if self.exists(filename):
            try:
                existing = loader(self.read(filename))
                candidate = loader(data)
            except AV1V4R4PreparationError as exc:
                raise AV1V4R4PreparationRegistryError(
                    f"AV1 v4 r4 preparation singleton {label} is invalid"
                ) from exc
            if existing != candidate:
                raise AV1V4R4PreparationRegistryError(
                    f"AV1 v4 r4 preparation singleton {label} conflicts with the registry"
                )
            return existing
        self.write_exclusive(filename, data)
        self.assert_file_custody(filename)
        try:
            return loader(self.read(filename))
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation singleton {label} custody is invalid"
            ) from exc

    def load_binding(self) -> dict[str, Any]:
        try:
            marker = deserialize_av1_v4r4_preparation_registry_binding(
                self.read(_BINDING_NAME)
            )
            assert_av1_v4r4_preparation_registry_binding(marker)
            return marker
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry binding is invalid"
            ) from exc

    def load_grant(self) -> dict[str, Any]:
        try:
            grant = deserialize_av1_v4r4_preparation_grant(self.read(_GRANT_NAME))
            assert_av1_v4r4_preparation_grant(grant)
            return grant
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation grant is unavailable"
            ) from exc

    def reconcile_consumed_claim(self) -> None:
        claim = deserialize_av1_v4r4_preparation_claim(self.read(_CLAIM_NAME))
        assert_av1_v4r4_preparation_claim(claim)
        if not self.exists(_KEY_NAME) or not self.exists(_CUSTODY_NAME):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation consumed registry is missing its key custody"
            )
        custody = deserialize_av1_v4r4_path_privacy_key_custody(self.read(_CUSTODY_NAME))
        assert_av1_v4r4_path_privacy_key_custody(custody)
        self.assert_file_custody(_KEY_NAME, expected_size=32)
        if custody.get("claim_id") != claim.get("claim_id"):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation consumed registry is inconsistent"
            )

    def reconcile_custody_attempt(self) -> None:
        if not self.exists(_CUSTODY_ATTEMPT_NAME):
            return
        try:
            attempt = json.loads(self.read(_CUSTODY_ATTEMPT_NAME))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation custody attempt is invalid"
            ) from exc
        if (
            not isinstance(attempt, dict)
            or set(attempt) != {"schema", "claim_id", "claim_payload_sha256"}
            or attempt.get("schema")
            != "mediaforce.av1_cold_start_v4r4_preparation_custody_attempt"
        ):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation custody attempt is invalid"
            )
        cleanup_errors: list[OSError] = []
        if self.exists(_CLAIM_NAME):
            self.reconcile_consumed_claim()
            claim = deserialize_av1_v4r4_preparation_claim(self.read(_CLAIM_NAME))
            if (
                attempt.get("claim_id") != claim.get("claim_id")
                or attempt.get("claim_payload_sha256") != claim.get("payload_sha256")
            ):
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation custody attempt conflicts with the claim"
                )
        else:
            for filename in (_CUSTODY_NAME, _KEY_NAME):
                if self.exists(filename):
                    self.unlink_owned(filename, cleanup_errors)
        self.unlink_owned(_CUSTODY_ATTEMPT_NAME, cleanup_errors)
        if cleanup_errors:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation custody recovery was incomplete"
            )

    def unlink_owned(self, filename: str, errors: list[OSError]) -> None:
        try:
            self.assert_file_custody(filename)
            os.unlink(filename, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            errors.append(exc)

    def _publish_candidate(
        self,
        filename: str,
        data: bytes,
        *,
        replace: bool,
        max_bytes: int | None = None,
    ) -> None:
        _assert_supported_filename(filename)
        if not isinstance(data, bytes):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry writes require bytes"
            )
        limit = max_bytes if max_bytes is not None else _MAX_JSON_BYTES
        if len(data) > limit:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} publication is oversized"
            )
        temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
        descriptor = -1
        try:
            descriptor = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.dir_fd,
            )
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if replace:
                os.replace(temp_name, filename, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
            else:
                os.link(temp_name, filename, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd, follow_symlinks=False)
                os.unlink(temp_name, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except FileExistsError as exc:
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} already exists"
            ) from exc
        except OSError as exc:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
            except OSError:
                pass
            cleanup_errors: list[OSError] = []
            try:
                os.unlink(temp_name, dir_fd=self.dir_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
            if cleanup_errors:
                raise AV1V4R4PreparationRegistryError(
                    f"AV1 v4 r4 preparation {filename} publication failed and cleanup was incomplete"
                ) from exc
            raise AV1V4R4PreparationRegistryError(
                f"AV1 v4 r4 preparation {filename} publication failed"
            ) from exc


@contextmanager
def _locked_registry(
    binding: AV1V4R4PreparationRegistryBinding,
) -> Iterator[_RegistryContext]:
    registry, repository_root = _normalized_binding_paths(binding)
    with _PROCESS_LOCK:
        dir_fd = _open_registry(registry)
        lock_fd = -1
        try:
            _assert_registry_directory_discriminant_fd(dir_fd)
            _assert_registry_fd(dir_fd)
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _assert_registry_fd(dir_fd)
            _assert_registry_directory_discriminant_fd(dir_fd)
            _reconcile_stale_temp_files(dir_fd)
            yield _RegistryContext(binding=binding, dir_fd=dir_fd, registry=registry)
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            os.close(dir_fd)


def _clock_timestamp(clock: Clock) -> str:
    if not callable(clock):
        raise AV1V4R4PreparationRegistryError("AV1 v4 r4 preparation clock is invalid")
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation clock must return an aware datetime"
        )
    if value.microsecond != 0:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation clock must use whole seconds"
        )
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalized_binding_paths(
    binding: AV1V4R4PreparationRegistryBinding,
) -> tuple[Path, Path]:
    if not isinstance(binding, AV1V4R4PreparationRegistryBinding):
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry binding is invalid"
        )
    registry = _canonical_directory(binding.registry, "registry", create_missing=False)
    repository_root = _canonical_directory(
        binding.repository_root, "repository root", create_missing=False
    )
    if registry == repository_root or _is_descendant(registry, repository_root) or _is_descendant(repository_root, registry):
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry must be outside and not contain the repository"
        )
    return registry, repository_root


def _canonical_directory(path: Path, label: str, *, create_missing: bool) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise AV1V4R4PreparationRegistryError(
            f"AV1 v4 r4 preparation {label} must be an absolute path"
        )
    try:
        if create_missing:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=not create_missing)
    except FileNotFoundError as exc:
        raise AV1V4R4PreparationRegistryError(
            f"AV1 v4 r4 preparation {label} is unavailable"
        ) from exc
    except OSError as exc:
        raise AV1V4R4PreparationRegistryError(
            f"AV1 v4 r4 preparation {label} is unavailable"
        ) from exc
    if not resolved.exists() or not resolved.is_dir():
        raise AV1V4R4PreparationRegistryError(
            f"AV1 v4 r4 preparation {label} must be a canonical real directory"
        )
    return resolved


def _open_registry(registry: Path) -> int:
    try:
        descriptor = os.open(registry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry is unavailable"
        ) from exc
    return descriptor


def _assert_registry_fd(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o777 != 0o700:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry custody is invalid"
        )


def _assert_owned_regular_file(metadata: os.stat_result, filename: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o777 != 0o600
    ):
        raise AV1V4R4PreparationRegistryError(
            f"AV1 v4 r4 preparation {filename} custody is invalid"
        )


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mode == after.st_mode
        and before.st_uid == after.st_uid
        and before.st_gid == after.st_gid
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
        and before.st_nlink == after.st_nlink
    )


def _assert_supported_filename(filename: str) -> None:
    if (
        filename not in _ARTIFACT_NAMES
        and filename != _LOCK_NAME
        and _temporary_target(filename) is None
    ):
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry filename is invalid"
        )


def _assert_registry_directory_discriminant(registry: Path) -> None:
    try:
        names = os.listdir(registry)
    except OSError as exc:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry is unavailable"
        ) from exc
    _assert_supported_registry_names(names)


def _assert_registry_directory_discriminant_fd(dir_fd: int) -> None:
    try:
        names = os.listdir(dir_fd)
    except OSError as exc:
        raise AV1V4R4PreparationRegistryError(
            "AV1 v4 r4 preparation registry is unavailable"
        ) from exc
    _assert_supported_registry_names(names)


def _assert_supported_registry_names(names: list[str]) -> None:
    for name in names:
        if name in _V4R3_ARTIFACT_NAMES:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry contains revision-3 artifacts"
            )
        if (
            name not in _ARTIFACT_NAMES
            and name != _LOCK_NAME
            and _temporary_target(name) is None
        ):
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation registry contains an unsupported artifact"
            )


def _temporary_target(filename: str) -> str | None:
    if not filename.endswith(_TEMP_SUFFIX):
        return None
    for target in _ARTIFACT_NAMES:
        prefix = f".{target}."
        if not filename.startswith(prefix):
            continue
        suffix = filename[len(prefix) : -len(_TEMP_SUFFIX)]
        parts = suffix.split(".")
        if (
            len(parts) == 2
            and parts[0].isdigit()
            and parts[0] != "0"
            and len(parts[1]) == 16
            and all(character in "0123456789abcdef" for character in parts[1])
        ):
            return target
    return None


def _reconcile_stale_temp_files(dir_fd: int) -> None:
    removed = False
    for name in os.listdir(dir_fd):
        target = _temporary_target(name)
        if target is None:
            continue
        try:
            metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink not in {1, 2}
            ):
                raise AV1V4R4PreparationRegistryError(
                    "AV1 v4 r4 preparation stale publication custody is invalid"
                )
            if metadata.st_nlink == 2:
                target_metadata = os.stat(
                    target,
                    dir_fd=dir_fd,
                    follow_symlinks=False,
                )
                if not _same_file_snapshot(metadata, target_metadata):
                    raise AV1V4R4PreparationRegistryError(
                        "AV1 v4 r4 preparation stale publication custody is invalid"
                    )
            os.unlink(name, dir_fd=dir_fd)
            removed = True
        except OSError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation stale publication recovery failed"
            ) from exc
    if removed:
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            raise AV1V4R4PreparationRegistryError(
                "AV1 v4 r4 preparation stale publication recovery failed"
            ) from exc


def _custody_attempt_bytes(claim: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(
        {
            "schema": "mediaforce.av1_cold_start_v4r4_preparation_custody_attempt",
            "claim_id": claim["claim_id"],
            "claim_payload_sha256": claim["payload_sha256"],
        }
    ) + b"\n"


def _is_descendant(path: Path, candidate_ancestor: Path) -> bool:
    try:
        path.relative_to(candidate_ancestor)
        return True
    except ValueError:
        return False
