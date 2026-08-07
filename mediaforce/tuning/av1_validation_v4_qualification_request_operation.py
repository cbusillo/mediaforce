from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_VALID_UNTIL,
    load_av1_validation_manifest_v4,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    AV1ValidationV4FreezeError,
    assert_av1_validation_v4_manifest_freeze_binds_bundle,
    load_av1_validation_v4_manifest_freeze,
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
from mediaforce.tuning.av1_validation_v4_qualification_authority import (
    AV1ValidationV4QualificationAuthorityError,
    assert_av1_validation_v4_qualification_request,
    build_av1_validation_v4_qualification_request,
    serialize_av1_validation_v4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    load_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_QUALIFICATION_REQUEST_FILENAME = (
    f"{AV1_VALIDATION_V4_MANIFEST_ID}-revision-"
    f"{AV1_VALIDATION_V4_MANIFEST_REVISION}-qualification-request.json"
)


class AV1ValidationV4QualificationRequestOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4QualificationRequestOperationInputs:
    repository_root: Path
    registry: Path
    manifest_path: Path
    freeze_path: Path
    rights_attestation_path: Path
    preparation_grant_path: Path
    effective_config_path: Path
    preparation_path: Path
    preparation_measurement_path: Path


@dataclass(frozen=True, slots=True)
class AV1ValidationV4QualificationRequestOperationResult:
    request: Mapping[str, Any]
    path: Path
    created: bool


Clock = Callable[[], datetime]


def materialize_av1_validation_v4_qualification_request(
    inputs: AV1ValidationV4QualificationRequestOperationInputs,
    *,
    now: Clock = lambda: datetime.now(UTC),
) -> AV1ValidationV4QualificationRequestOperationResult:
    _assert_registry(inputs.registry, repository_root=inputs.repository_root)
    with _registry_lock(inputs.registry):
        return _materialize_locked(inputs, now=now)


def _materialize_locked(
    inputs: AV1ValidationV4QualificationRequestOperationInputs,
    *,
    now: Clock,
) -> AV1ValidationV4QualificationRequestOperationResult:
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
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request manifest binding is invalid"
        )
    rights = load_av1_validation_v4_rights_attestation(
        inputs.rights_attestation_path
    )
    grant = load_av1_validation_v4_preparation_grant(
        inputs.preparation_grant_path
    )
    claim = load_av1_validation_v4_preparation_claim(
        _claim_registry_path(grant, repository_root=inputs.repository_root)
        / f"{grant['grant_id']}.json"
    )
    config = load_av1_validation_v4_effective_config_snapshot(
        inputs.effective_config_path
    )
    preparation = _load_canonical_object(
        inputs.preparation_path,
        "AV1 v4 preparation record",
    )
    measurement = load_av1_validation_v4_preparation_measurement(
        inputs.preparation_measurement_path
    )
    assert_av1_validation_v4_preparation_bundle(
        preparation,
        rights,
        grant,
        claim,
    )
    freeze = _load_freeze(
        inputs.freeze_path,
        rights=rights,
        grant=grant,
        claim=claim,
        config=config,
        preparation=preparation,
        measurement=measurement,
    )
    execution_commit, execution_tree = _measure_repository_identity(
        inputs.repository_root
    )
    requested_at = _canonical_timestamp(now())
    path = inputs.registry / AV1_VALIDATION_V4_QUALIFICATION_REQUEST_FILENAME
    _cleanup_stale_temp_files(path)
    if path.exists() or path.is_symlink():
        existing = _reconcile_existing_request(
            path,
            freeze=freeze,
            preparation=preparation,
            execution_commit=execution_commit,
            execution_tree=execution_tree,
            registry=str(inputs.registry.resolve(strict=True)),
            as_of=requested_at,
        )
        return AV1ValidationV4QualificationRequestOperationResult(
            request=existing,
            path=path,
            created=False,
        )
    request = _build_request(
        freeze=freeze,
        preparation=preparation,
        execution_commit=execution_commit,
        execution_tree=execution_tree,
        registry=str(inputs.registry.resolve(strict=True)),
        requested_at=requested_at,
    )
    serialized = serialize_av1_validation_v4_qualification_request(request)
    if not _atomic_publish(path, serialized):
        existing = _reconcile_existing_request(
            path,
            freeze=freeze,
            preparation=preparation,
            execution_commit=execution_commit,
            execution_tree=execution_tree,
            registry=str(inputs.registry.resolve(strict=True)),
            as_of=requested_at,
        )
        return AV1ValidationV4QualificationRequestOperationResult(
            request=existing,
            path=path,
            created=False,
        )
    _assert_file_custody(path, expected_size=len(serialized))
    return AV1ValidationV4QualificationRequestOperationResult(
        request=request,
        path=path,
        created=True,
    )


def _build_request(
    *,
    freeze: Mapping[str, Any],
    preparation: Mapping[str, Any],
    execution_commit: str,
    execution_tree: str,
    registry: str,
    requested_at: str,
) -> dict[str, Any]:
    return build_av1_validation_v4_qualification_request(
        freeze=freeze,
        preparation=preparation,
        owner_principal=str(freeze["owner_principal"]),
        execution_repository_commit=execution_commit,
        execution_repository_tree=execution_tree,
        consumption_registry=registry,
        requested_at=requested_at,
        valid_until=_request_valid_until(requested_at),
    )


def _load_freeze(
    path: Path,
    *,
    rights: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    measurement: Mapping[str, Any],
) -> dict[str, Any]:
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
        assert_av1_validation_v4_manifest_freeze_binds_bundle(
            freeze,
            rights_attestation=rights,
            preparation_grant=grant,
            preparation_claim=claim,
            effective_config=config,
            preparation=preparation,
            preparation_measurement=measurement,
        )
    except AV1ValidationV4FreezeError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request freeze does not bind the full bundle"
        ) from exc
    return freeze


def _reconcile_existing_request(
    path: Path,
    *,
    freeze: Mapping[str, Any],
    preparation: Mapping[str, Any],
    execution_commit: str,
    execution_tree: str,
    registry: str,
    as_of: str,
) -> dict[str, Any]:
    try:
        existing, existing_size = _load_request_with_custody(path)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request singleton is different or invalid"
        ) from exc
    if _parse_timestamp(existing["valid_until"]) <= _parse_timestamp(as_of):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request singleton is expired"
        )
    expected = _build_request(
        freeze=freeze,
        preparation=preparation,
        execution_commit=execution_commit,
        execution_tree=execution_tree,
        registry=registry,
        requested_at=str(existing["requested_at"]),
    )
    expected_size = len(serialize_av1_validation_v4_qualification_request(expected))
    if existing != dict(expected):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request singleton is different"
        )
    if existing_size != expected_size:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request singleton custody is invalid"
        )
    _assert_file_custody(path, expected_size=expected_size)
    return existing


def _request_valid_until(requested_at: str) -> str:
    requested = _parse_timestamp(requested_at)
    manifest_valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL)
    valid_until = min(requested + timedelta(hours=24), manifest_valid_until)
    if requested >= valid_until:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request window has expired"
        )
    return valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_registry(path: Path, *, repository_root: Path) -> None:
    if not path.is_absolute() or not repository_root.is_absolute():
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request paths must be absolute"
        )
    try:
        metadata = path.lstat()
        resolved_registry = path.resolve(strict=True)
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request registry is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or path != resolved_registry
        or repository_root != resolved_repository
        or resolved_registry.is_relative_to(resolved_repository)
        or resolved_repository.is_relative_to(resolved_registry)
    ):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request registry must be an owner-only directory outside the repository"
        )


@contextmanager
def _registry_lock(registry: Path) -> Iterator[None]:
    directory_descriptor = os.open(
        registry,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        lock_descriptor = os.open(
            ".av1-v4-qualification-request.lock",
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
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request registry lock is invalid"
                )
            os.fsync(directory_descriptor)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(directory_descriptor)


def _claim_registry_path(
    grant: Mapping[str, Any],
    *,
    repository_root: Path,
) -> Path:
    value = grant.get("consumption_registry")
    if not isinstance(value, str):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 preparation claim registry is invalid"
        )
    pure = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "//" in value
        or value.endswith("/")
        or ".." in pure.parts
        or pure.as_posix() != value
    ):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 preparation claim registry is invalid"
        )
    path = Path(value)
    _assert_registry(path, repository_root=repository_root)
    return path.resolve(strict=True)


def _measure_repository_identity(repository_root: Path) -> tuple[str, str]:
    git_path = _trusted_git_path()
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            [git_path, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "rev-parse", "--show-toplevel", "HEAD", "HEAD^{tree}"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request repository measurement failed"
        ) from exc
    values = result.stdout.splitlines()
    if (
        result.returncode != 0
        or len(values) != 3
        or Path(values[0]).resolve() != repository_root.resolve()
    ):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request repository measurement failed"
        )
    try:
        status = subprocess.run(
            [git_path, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request repository measurement failed"
        ) from exc
    if status.returncode != 0 or status.stdout:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request repository must be clean"
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
        if stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0:
            return str(resolved)
    raise AV1ValidationV4QualificationRequestOperationError(
        "AV1 v4 qualification request requires a trusted Git executable"
    )


def _assert_manifest_location(path: Path, *, repository_root: Path) -> None:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request manifest path is unavailable"
        ) from exc
    expected = (
        resolved_root
        / "docs/validation/av1-cold-start-preregistration-v4.json"
    )
    if resolved_path != expected:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request manifest is outside the materializer repository"
        )


def _load_canonical_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4QualificationRequestOperationError(
            f"{label} bytes are not canonical"
        )
    return payload


def _atomic_publish(path: Path, data: bytes) -> bool:
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
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request temporary write failed and cleanup was incomplete"
                ) from ExceptionGroup(
                    "AV1 v4 qualification request temporary write and cleanup failures",
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
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request link failed and temporary cleanup was incomplete"
                ) from ExceptionGroup(
                    "AV1 v4 qualification request link and cleanup failures",
                    [link_exc, cleanup_exc],
                )
            raise
        os.fsync(directory_descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        except OSError as exc:
            raise AV1ValidationV4QualificationRequestOperationError(
                "AV1 v4 qualification request materialized but temporary-link cleanup requires reconciliation"
            ) from exc
        return True
    finally:
        os.close(directory_descriptor)


def _load_request_with_custody(path: Path) -> tuple[dict[str, Any], int]:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size <= 0
            ):
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request candidate custody is invalid"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read()
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request candidate is unavailable"
        ) from exc
    finally:
        os.close(directory_descriptor)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request bytes are not canonical"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4QualificationAuthorityError(
            "AV1 v4 qualification request bytes are not canonical"
        )
    assert_av1_validation_v4_qualification_request(payload)
    return payload, len(raw)


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
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request registry contains an unexpected temporary artifact"
                )
            os.unlink(name, dir_fd=directory_descriptor)
            removed = True
        if removed:
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _assert_file_custody(path: Path, *, expected_size: int) -> None:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size != expected_size
            ):
                raise AV1ValidationV4QualificationRequestOperationError(
                    "AV1 v4 qualification request custody verification failed"
                )
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request custody verification failed"
        ) from exc
    finally:
        os.close(directory_descriptor)


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request clock must return a timezone-aware timestamp"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request timestamp is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4QualificationRequestOperationError(
            "AV1 v4 qualification request timestamp must be canonical UTC seconds"
        )
    return normalized
