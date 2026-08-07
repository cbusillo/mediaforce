from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import stat
import subprocess
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    load_av1_validation_manifest_v4,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    AV1ValidationV4FreezeError,
    build_av1_validation_v4_manifest_freeze,
    load_av1_validation_v4_manifest_freeze,
    serialize_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    assert_av1_validation_v4_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    load_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    load_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_measurement import (
    load_av1_validation_v4_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    load_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_FREEZE_FILENAME = (
    f"{AV1_VALIDATION_V4_MANIFEST_ID}-revision-"
    f"{AV1_VALIDATION_V4_MANIFEST_REVISION}-freeze.json"
)


class AV1ValidationV4FreezeOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4FreezeOperationInputs:
    repository_root: Path
    registry: Path
    manifest_path: Path
    rights_attestation_path: Path
    preparation_grant_path: Path
    effective_config_path: Path
    preparation_path: Path
    preparation_measurement_path: Path


@dataclass(frozen=True, slots=True)
class AV1ValidationV4FreezeOperationResult:
    freeze: Mapping[str, Any]
    path: Path


Clock = Callable[[], datetime]


def materialize_av1_validation_v4_manifest_freeze(
    inputs: AV1ValidationV4FreezeOperationInputs,
    *,
    now: Clock = lambda: datetime.now(UTC),
) -> AV1ValidationV4FreezeOperationResult:
    _assert_registry(inputs.registry, repository_root=inputs.repository_root)
    with _registry_lock(inputs.registry):
        return _materialize_locked(inputs, now=now)


def _materialize_locked(
    inputs: AV1ValidationV4FreezeOperationInputs,
    *,
    now: Clock,
) -> AV1ValidationV4FreezeOperationResult:
    _assert_manifest_location(
        inputs.manifest_path,
        repository_root=inputs.repository_root,
    )
    manifest = load_av1_validation_manifest_v4(inputs.manifest_path)
    if (
        manifest.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or manifest.get("payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or manifest.get("state") != "draft_unapproved"
    ):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze manifest binding is invalid"
        )
    rights = load_av1_validation_v4_rights_attestation(
        inputs.rights_attestation_path
    )
    grant = load_av1_validation_v4_preparation_grant(
        inputs.preparation_grant_path
    )
    claim_registry = _claim_registry_path(
        grant,
        repository_root=inputs.repository_root,
    )
    claim = load_av1_validation_v4_preparation_claim(
        claim_registry / f"{grant['grant_id']}.json"
    )
    config = load_av1_validation_v4_effective_config_snapshot(
        inputs.effective_config_path
    )
    measurement = load_av1_validation_v4_preparation_measurement(
        inputs.preparation_measurement_path
    )
    preparation = _load_canonical_object(
        inputs.preparation_path,
        "AV1 v4 preparation record",
    )
    assert_av1_validation_v4_preparation_bundle(
        preparation,
        rights,
        grant,
        claim,
    )
    materializer_commit, materializer_tree = _measure_repository_identity(
        inputs.repository_root
    )
    path = inputs.registry / AV1_VALIDATION_V4_FREEZE_FILENAME
    _cleanup_stale_temp_files(path)
    if path.exists() or path.is_symlink():
        freeze = _reconcile_existing_freeze(
            path,
            rights=rights,
            grant=grant,
            claim=claim,
            config=config,
            preparation=preparation,
            measurement=measurement,
            materializer_commit=materializer_commit,
            materializer_tree=materializer_tree,
        )
        return AV1ValidationV4FreezeOperationResult(freeze=freeze, path=path)
    freeze = build_av1_validation_v4_manifest_freeze(
        rights_attestation=rights,
        preparation_grant=grant,
        preparation_claim=claim,
        effective_config=config,
        preparation=preparation,
        preparation_measurement=measurement,
        owner_principal=str(rights["owner_principal"]),
        decided_at=_canonical_timestamp(now()),
        materializer_repository_commit=materializer_commit,
        materializer_repository_tree=materializer_tree,
    )
    serialized = serialize_av1_validation_v4_manifest_freeze(
        freeze,
        rights_attestation=rights,
        preparation_grant=grant,
        preparation_claim=claim,
        effective_config=config,
        preparation=preparation,
        preparation_measurement=measurement,
    )
    if not _atomic_publish(path, serialized):
        freeze = _reconcile_existing_freeze(
            path,
            rights=rights,
            grant=grant,
            claim=claim,
            config=config,
            preparation=preparation,
            measurement=measurement,
            materializer_commit=materializer_commit,
            materializer_tree=materializer_tree,
        )
        return AV1ValidationV4FreezeOperationResult(freeze=freeze, path=path)
    _assert_file_custody(path, expected_size=len(serialized))
    return AV1ValidationV4FreezeOperationResult(freeze=freeze, path=path)


def _assert_registry(path: Path, *, repository_root: Path) -> None:
    if not path.is_absolute() or not repository_root.is_absolute():
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze paths must be absolute"
        )
    try:
        metadata = path.lstat()
        resolved_registry = path.resolve(strict=True)
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze registry is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or resolved_registry.is_relative_to(resolved_repository)
        or resolved_repository.is_relative_to(resolved_registry)
    ):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze registry must be an owner-only directory outside the repository"
        )


@contextmanager
def _registry_lock(registry: Path) -> Iterator[None]:
    directory_descriptor = os.open(
        registry,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        lock_descriptor = os.open(
            ".av1-v4-freeze.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise AV1ValidationV4FreezeOperationError(
                    "AV1 v4 freeze registry lock is invalid"
                )
            os.fsync(directory_descriptor)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(directory_descriptor)


def _measure_repository_identity(repository_root: Path) -> tuple[str, str]:
    git_path = _trusted_git_path()
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            [git_path, "rev-parse", "--show-toplevel", "HEAD", "HEAD^{tree}"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze materializer repository measurement failed"
        ) from exc
    values = result.stdout.splitlines()
    if (
        result.returncode != 0
        or len(values) != 3
        or Path(values[0]).resolve() != repository_root.resolve()
    ):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze materializer repository measurement failed"
        )
    try:
        status = subprocess.run(
            [git_path, "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze materializer repository measurement failed"
        ) from exc
    if status.returncode != 0 or status.stdout:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze materializer repository must be clean"
        )
    return values[1].strip(), values[2].strip()


def _trusted_git_path() -> str:
    for candidate in (
        Path("/usr/bin/git"),
        Path("/bin/git"),
        Path("/opt/homebrew/bin/git"),
        Path("/usr/local/bin/git"),
    ):
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            return str(resolved)
    raise AV1ValidationV4FreezeOperationError(
        "AV1 v4 freeze requires a trusted Git executable"
    )


def _assert_manifest_location(path: Path, *, repository_root: Path) -> None:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze manifest path is unavailable"
        ) from exc
    expected = (
        resolved_root
        / "docs/validation/av1-cold-start-preregistration-v4.json"
    )
    if resolved_path != expected:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze manifest is outside the materializer repository"
        )


def _claim_registry_path(
    grant: Mapping[str, Any],
    *,
    repository_root: Path,
) -> Path:
    value = grant.get("consumption_registry")
    if not isinstance(value, str):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 preparation claim registry is invalid"
        )
    pure = PurePosixPath(value)
    if not value.startswith("/") or ".." in pure.parts or pure.as_posix() != value:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 preparation claim registry is invalid"
        )
    path = Path(value)
    _assert_registry(path, repository_root=repository_root)
    return path


def _load_canonical_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4FreezeOperationError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4FreezeOperationError(
            f"{label} bytes are not canonical"
        )
    return payload


def _atomic_publish(path: Path, data: bytes) -> bool:
    if not hasattr(os, "O_NOFOLLOW"):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze materialization requires O_NOFOLLOW"
        )
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    temp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_descriptor)
        except Exception as write_exc:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError as cleanup_exc:
                raise AV1ValidationV4FreezeOperationError(
                    "AV1 v4 freeze temporary write failed and cleanup was incomplete"
                ) from ExceptionGroup(
                    "AV1 v4 freeze temporary write and cleanup failures",
                    [write_exc, cleanup_exc],
                )
            raise
        try:
            os.link(
                temp_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            os.unlink(temp_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            return False
        except OSError as link_exc:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError as cleanup_exc:
                raise AV1ValidationV4FreezeOperationError(
                    "AV1 v4 freeze link failed and temporary cleanup was incomplete"
                ) from ExceptionGroup(
                    "AV1 v4 freeze link and cleanup failures",
                    [link_exc, cleanup_exc],
                )
            raise
        os.fsync(directory_descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        except OSError as exc:
            raise AV1ValidationV4FreezeOperationError(
                "AV1 v4 freeze materialized but temporary-link cleanup requires reconciliation"
            ) from exc
        return True
    finally:
        os.close(directory_descriptor)


def _reconcile_existing_freeze(
    path: Path,
    *,
    rights: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    measurement: Mapping[str, Any],
    materializer_commit: str,
    materializer_tree: str,
) -> dict[str, Any]:
    _assert_materialized_candidate(path)
    try:
        freeze = load_av1_validation_v4_manifest_freeze(
            path,
            rights_attestation=rights,
            preparation_grant=grant,
            preparation_claim=claim,
            effective_config=config,
            preparation=preparation,
            preparation_measurement=measurement,
        )
    except AV1ValidationV4FreezeError as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 manifest revision 2 already has a different or invalid freeze"
        ) from exc
    if freeze.get("materializer_repository") != {
        "commit": materializer_commit,
        "tree": materializer_tree,
    }:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 manifest revision 2 already has a different freeze"
        )
    _assert_file_custody(path, expected_size=path.stat().st_size)
    return freeze


def _assert_materialized_candidate(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 manifest freeze candidate is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
    ):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 manifest freeze candidate custody is invalid"
        )


def _cleanup_stale_temp_files(path: Path) -> None:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        prefix = f".{path.name}."
        removed = False
        for name in os.listdir(directory_descriptor):
            if not name.startswith(prefix) or not name.endswith(".tmp"):
                continue
            candidate = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(candidate.st_mode)
                or stat.S_IMODE(candidate.st_mode) != 0o600
                or candidate.st_uid != os.geteuid()
            ):
                raise AV1ValidationV4FreezeOperationError(
                    "AV1 v4 freeze registry contains an unexpected temporary artifact"
                )
            os.unlink(name, dir_fd=directory_descriptor)
            removed = True
        if removed:
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _assert_file_custody(path: Path, *, expected_size: int) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size != expected_size
    ):
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 manifest freeze custody verification failed"
        )


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise AV1ValidationV4FreezeOperationError(
            "AV1 v4 freeze clock must return a timezone-aware timestamp"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
