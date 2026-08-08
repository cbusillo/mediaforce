"""Owner-only terminal preparation-bundle operation for AV1 protocol-v4 revision 3."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    av1_v4r3_path_privacy_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_paths import (
    AV1V4R3CanonicalPathError,
    canonical_av1_v4r3_absolute_posix_path,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    build_av1_v4r3_preparation_bundle,
    serialize_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    build_av1_v4r3_effective_config_snapshot,
    serialize_av1_v4r3_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    AV1V4R3PreparationCustodyError,
    assert_av1_v4r3_path_privacy_key_custody,
    assert_av1_v4r3_preparation_claim,
    deserialize_av1_v4r3_path_privacy_key_custody,
    deserialize_av1_v4r3_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
    AV1V4R3PreparationCustodyRegistryError,
    _ATTEMPT_NAME,
    _BUNDLE_NAME,
    _CLAIM_NAME,
    _CONFIG_NAME,
    _CUSTODY_NAME,
    _KEY_NAME,
    _MEASUREMENT_NAME,
    _RegistryContext,
    _clock_timestamp,
    _locked_registry,
    _no_follow_flag,
    _same_file_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    AV1V4R3PreparationGrantError,
    assert_av1_v4r3_preparation_grant_active,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_measurement import (
    AV1_V4R3_PREPARATION_METHOD,
    AV1_V4R3_PREPARATION_STAGES,
    build_av1_v4r3_preparation_failure_measurement,
    build_av1_v4r3_preparation_success_measurement,
    serialize_av1_v4r3_preparation_measurement,
)


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class AV1V4R3PreparationBundleResult:
    """Terminal result of the sole owner-only non-media bundle operation."""

    effective_config: Mapping[str, Any]
    preparation_bundle: Mapping[str, Any]
    measurement: Mapping[str, Any]
    artifact_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class _ToolBinary:
    descriptor: int
    metadata: os.stat_result
    binary_sha256: str


def run_av1_v4r3_preparation_bundle(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
    tool_paths: Mapping[str, Path],
    clock: Clock,
) -> AV1V4R3PreparationBundleResult:
    """Run the terminal, non-media preparation bundle operation exactly once."""

    with _locked_registry(binding) as context:
        completed: list[str] = []
        probes: list[dict[str, Any]] = []
        started_at = _clock_timestamp(clock)
        context.load_binding()
        _cleanup_tool_snapshots(context)
        grant = context.load_grant()
        claim, custody = _load_claim_and_custody(context)
        try:
            assert_av1_v4r3_preparation_grant_active(grant, as_of=started_at)
        except AV1V4R3PreparationGrantError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation bundle grant is inactive"
            ) from exc
        expected_repository = {"commit": repository_commit, "tree": repository_tree}
        if (
            grant.get("repository") != expected_repository
            or claim.get("repository") != expected_repository
            or custody.get("repository") != expected_repository
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation bundle repository binding is invalid"
            )
        _assert_non_media_boundaries(
            binding=binding,
            source_paths=source_paths,
            dedicated_instance_paths=dedicated_instance_paths,
            quality_temp_paths=quality_temp_paths,
            tool_paths=tool_paths,
        )
        completed.append("validate_custody_chain")
        if context.exists(_MEASUREMENT_NAME):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation bundle attempt is already terminal"
            )
        if context.exists(_ATTEMPT_NAME):
            _recover_interrupted_bundle_attempt(
                context=context,
                grant=grant,
                claim=claim,
                custody=custody,
                completed_at=started_at,
            )
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation bundle attempt was already started"
            )
        context.write_exclusive(
            _ATTEMPT_NAME,
            _attempt_bytes(
                grant=grant,
                claim=claim,
                custody=custody,
                started_at=started_at,
            ),
        )
        completed.append("start_terminal_attempt")
        config_created = False
        bundle_created = False
        try:
            key = context.read(_KEY_NAME)
            context.assert_file_custody(_KEY_NAME, expected_size=32)
            if custody.get("key_id") != av1_v4r3_path_privacy_key_id(key):
                raise AV1V4R3PreparationCustodyRegistryError(
                    "AV1 v4 r3 preparation path-privacy key does not match custody"
                )
            completed.append("read_path_privacy_key")
            config = build_av1_v4r3_effective_config_snapshot(
                repository_commit=repository_commit,
                repository_tree=repository_tree,
                source_paths=source_paths,
                dedicated_instance_paths=dedicated_instance_paths,
                quality_temp_paths=quality_temp_paths,
            )
            completed.append("build_effective_config")
            live_commit, live_tree = _measure_clean_repository(binding.repository_root)
            if (live_commit, live_tree) != (repository_commit, repository_tree):
                raise AV1V4R3PreparationCustodyRegistryError(
                    "AV1 v4 r3 preparation live repository identity does not match"
                )
            completed.append("measure_repository")
            tool_handles = _open_tool_binaries(tool_paths)
            completed.append("hash_toolchain")
            try:
                _probe_tool_versions(grant, tool_handles, context, probes)
                hashed_tools = {
                    name: handle.binary_sha256 for name, handle in tool_handles.items()
                }
            finally:
                _close_tool_binaries(tool_handles)
            completed.append("probe_toolchain")
            runtime = {
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "platform_system": platform.system(),
                "platform_machine": platform.machine(),
            }
            bundle = build_av1_v4r3_preparation_bundle(
                preparation_grant=grant,
                preparation_claim=claim,
                key_custody=custody,
                effective_config=config,
                path_privacy_key=key,
                toolchain={
                    name: {
                        "binary_sha256": hashed_tools[name],
                        "version": probes[index]["version"],
                    }
                    for index, name in enumerate(("ab_av1", "ffmpeg", "ffprobe"))
                },
                runtime=runtime,
            )
            completed.append("derive_private_identities")
            completed.append("build_prepared_bundle")
            context.write_exclusive(
                _CONFIG_NAME, serialize_av1_v4r3_effective_config_snapshot(config)
            )
            config_created = True
            context.write_exclusive(
                _BUNDLE_NAME, serialize_av1_v4r3_preparation_bundle(bundle)
            )
            bundle_created = True
            completed_at = _clock_timestamp(clock)
            measurement = build_av1_v4r3_preparation_success_measurement(
                preparation_grant=grant,
                preparation_claim=claim,
                key_custody=custody,
                effective_config=config,
                preparation_bundle=bundle,
                path_privacy_key=key,
                started_at=started_at,
                completed_at=completed_at,
                probes=probes,
            )
            context.write_exclusive(
                _MEASUREMENT_NAME,
                serialize_av1_v4r3_preparation_measurement(measurement),
            )
            return AV1V4R3PreparationBundleResult(
                effective_config=config,
                preparation_bundle=bundle,
                measurement=measurement,
                artifact_paths={
                    "effective_config": context.registry / _CONFIG_NAME,
                    "preparation_bundle": context.registry / _BUNDLE_NAME,
                    "terminal_measurement": context.registry / _MEASUREMENT_NAME,
                },
            )
        except BaseException as exc:
            rollback_errors: list[OSError] = []
            if bundle_created:
                context.unlink_owned(_BUNDLE_NAME, rollback_errors)
            if config_created:
                context.unlink_owned(_CONFIG_NAME, rollback_errors)
            failure_stage = AV1_V4R3_PREPARATION_STAGES[len(completed)]
            failure = build_av1_v4r3_preparation_failure_measurement(
                preparation_grant=grant,
                preparation_claim=claim,
                key_custody=custody,
                started_at=started_at,
                completed_at=_clock_timestamp(clock),
                stages_completed=completed,
                probes=probes,
                failure_stage=failure_stage,
                error_class=type(exc).__name__,
                error_code=(
                    "rollback_incomplete" if rollback_errors else "stage_failed"
                ),
                message_sha256="sha256:"
                + hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest(),
            )
            context.write_exclusive(
                _MEASUREMENT_NAME,
                serialize_av1_v4r3_preparation_measurement(failure),
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation bundle attempt terminated"
            ) from exc


def _load_claim_and_custody(
    context: _RegistryContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        claim = deserialize_av1_v4r3_preparation_claim(context.read(_CLAIM_NAME))
        custody = deserialize_av1_v4r3_path_privacy_key_custody(
            context.read(_CUSTODY_NAME)
        )
        assert_av1_v4r3_preparation_claim(claim)
        assert_av1_v4r3_path_privacy_key_custody(custody)
    except (AV1V4R3PreparationCustodyError, FileNotFoundError) as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation bundle custody chain is unavailable"
        ) from exc
    if custody.get("claim_id") != claim.get("claim_id") or custody.get(
        "claim_payload_sha256"
    ) != claim.get("payload_sha256"):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation bundle custody chain is inconsistent"
        )
    return claim, custody


def _attempt_bytes(
    *,
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    started_at: str,
) -> bytes:
    payload: dict[str, Any] = {
        "schema": "mediaforce.av1_cold_start_v4r3_preparation_attempt_started",
        "schema_version": 1,
        "state": "attempt_started",
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "started_at": started_at,
    }
    payload["payload_sha256"] = f"sha256:{stable_json_hash(payload)}"
    return canonical_json_bytes(payload) + b"\n"


def _load_attempt(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt marker is invalid"
        )
    expected_keys = {
        "schema",
        "schema_version",
        "state",
        "preparation_grant_id",
        "preparation_grant_payload_sha256",
        "claim_id",
        "claim_payload_sha256",
        "custody_id",
        "custody_payload_sha256",
        "started_at",
        "payload_sha256",
    }
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if (
        data != canonical_json_bytes(payload) + b"\n"
        or set(payload) != expected_keys
        or payload.get("schema")
        != "mediaforce.av1_cold_start_v4r3_preparation_attempt_started"
        or payload.get("schema_version") != 1
        or payload.get("state") != "attempt_started"
        or payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}"
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt marker is invalid"
        )
    try:
        started_at = datetime.fromisoformat(
            str(payload.get("started_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt timestamp is invalid"
        ) from exc
    if started_at.microsecond or started_at.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ) != payload.get("started_at"):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt timestamp is invalid"
        )
    return payload


def _recover_interrupted_bundle_attempt(
    *,
    context: _RegistryContext,
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    completed_at: str,
) -> None:
    attempt = _load_attempt(context.read(_ATTEMPT_NAME))
    expected = {
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
    }
    if any(attempt.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation attempt marker does not match custody"
        )
    rollback_errors: list[OSError] = []
    context.unlink_owned(_BUNDLE_NAME, rollback_errors)
    context.unlink_owned(_CONFIG_NAME, rollback_errors)
    failure = build_av1_v4r3_preparation_failure_measurement(
        preparation_grant=grant,
        preparation_claim=claim,
        key_custody=custody,
        started_at=str(attempt["started_at"]),
        completed_at=completed_at,
        stages_completed=AV1_V4R3_PREPARATION_STAGES[:2],
        probes=(),
        failure_stage="read_path_privacy_key",
        error_class="builtins.RuntimeError",
        error_code="interrupted_prior_attempt",
        message_sha256="sha256:"
        + hashlib.sha256(b"interrupted prior preparation attempt").hexdigest(),
    )
    context.write_exclusive(
        _MEASUREMENT_NAME,
        serialize_av1_v4r3_preparation_measurement(failure),
    )
    if rollback_errors:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 interrupted preparation cleanup was incomplete"
        )


def _assert_non_media_boundaries(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
    tool_paths: Mapping[str, Path],
) -> None:
    try:
        source_root = PurePosixPath(
            canonical_av1_v4r3_absolute_posix_path(
                str(dedicated_instance_paths["source_root"])
            )
        )
        temp_root = PurePosixPath(
            canonical_av1_v4r3_absolute_posix_path(
                str(dedicated_instance_paths["temp_root"])
            )
        )
        private_paths = {
            PurePosixPath(canonical_av1_v4r3_absolute_posix_path(str(path)))
            for path in (*source_paths.values(), *quality_temp_paths.values())
        }
        tool_values = {
            PurePosixPath(canonical_av1_v4r3_absolute_posix_path(str(path)))
            for path in tool_paths.values()
        }
    except (KeyError, AV1V4R3CanonicalPathError) as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation private path boundary is invalid"
        ) from exc
    repository = PurePosixPath(str(binding.repository_root))
    registry = PurePosixPath(str(binding.registry))
    if any(
        _paths_overlap(private_root, protected_root)
        for private_root in (source_root, temp_root)
        for protected_root in (repository, registry)
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation custody paths overlap a private media root"
        )
    for tool in tool_values:
        if (
            _paths_overlap(tool, source_root)
            or _paths_overlap(tool, temp_root)
            or tool in private_paths
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation tool path overlaps a private media path"
            )


def _paths_overlap(first: PurePosixPath, second: PurePosixPath) -> bool:
    return first == second or first in second.parents or second in first.parents


def _measure_clean_repository(repository_root: Path) -> tuple[str, str]:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        identity = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--show-toplevel", "HEAD", "HEAD^{tree}"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        status_result = subprocess.run(
            ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation repository measurement failed"
        ) from exc
    values = identity.stdout.splitlines()
    if (
        identity.returncode != 0
        or len(values) != 3
        or Path(values[0]).resolve() != repository_root.resolve()
        or status_result.returncode != 0
        or status_result.stdout
    ):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation repository must be clean and canonical"
        )
    return values[1].strip(), values[2].strip()


def _open_tool_binaries(tool_paths: Mapping[str, Path]) -> dict[str, _ToolBinary]:
    expected = ("ab_av1", "ffmpeg", "ffprobe")
    if set(tool_paths) != set(expected):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool path set is invalid"
        )
    handles: dict[str, _ToolBinary] = {}
    try:
        for name in expected:
            path = Path(tool_paths[name])
            descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or before.st_mode & 0o111 == 0
            ):
                raise AV1V4R3PreparationCustodyRegistryError(
                    "AV1 v4 r3 preparation tool binary custody is invalid"
                )
            digest = _hash_open_tool(descriptor, before)
            handles[name] = _ToolBinary(
                descriptor=descriptor,
                metadata=before,
                binary_sha256=digest,
            )
    except BaseException as exc:
        for handle in handles.values():
            os.close(handle.descriptor)
        if "descriptor" in locals() and all(
            handle.descriptor != descriptor for handle in handles.values()
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, AV1V4R3PreparationCustodyRegistryError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool binary is unavailable"
        ) from exc
    return handles


def _close_tool_binaries(handles: Mapping[str, _ToolBinary]) -> None:
    errors: list[OSError] = []
    for handle in handles.values():
        try:
            os.close(handle.descriptor)
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool binary custody close failed"
        )


def _hash_open_tool(descriptor: int, expected: os.stat_result) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 64 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    after = os.fstat(descriptor)
    if not _same_file_snapshot(expected, after):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool binary changed during measurement"
        )
    return f"sha256:{digest.hexdigest()}"


def _probe_tool_versions(
    grant: Mapping[str, Any],
    handles: Mapping[str, _ToolBinary],
    context: _RegistryContext,
    probes: list[dict[str, Any]],
) -> None:
    scope = grant.get("operation_scope")
    if not isinstance(scope, Mapping):
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation probe scope is invalid"
        )
    if probes:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation probe result list must start empty"
        )
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        if (
            scope.get(f"{name}_version_probe_argv")
            != AV1_V4R3_PREPARATION_METHOD["tool_version_probe_argv"][name]
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation probe scope is invalid"
            )
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        argv_value = scope.get(f"{name}_version_probe_argv")
        argv = list(argv_value)
        handle = handles[name]
        snapshot_name = _snapshot_tool_binary(context, handle)
        snapshot_digest: str | None = None
        try:
            result = subprocess.run(
                [str(context.registry / snapshot_name), *argv],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={"LANG": "C", "LC_ALL": "C"},
            )
            snapshot_digest = _hash_path(context.registry / snapshot_name)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation tool version probe failed"
            ) from exc
        finally:
            _remove_tool_snapshot(context, snapshot_name)
        output = result.stdout or result.stderr
        lines = output.splitlines()
        if (
            result.returncode != 0
            or not lines
            or not lines[0].strip()
            or snapshot_digest != handle.binary_sha256
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation tool version probe failed"
            )
        probes.append(
            {
                "tool": name,
                "argv": argv,
                "version": lines[0].strip(),
                "binary_sha256": handle.binary_sha256,
            }
        )
        if _hash_open_tool(handle.descriptor, handle.metadata) != handle.binary_sha256:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation tool binary changed during probing"
            )


def _snapshot_tool_binary(
    context: _RegistryContext,
    handle: _ToolBinary,
) -> str:
    snapshot_name = f".tool-probe-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            snapshot_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o500,
            dir_fd=context.dir_fd,
        )
        os.fchmod(descriptor, 0o500)
        os.lseek(handle.descriptor, 0, os.SEEK_SET)
        while chunk := os.read(handle.descriptor, 64 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short tool snapshot write")
                view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o500
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation tool snapshot custody is invalid"
            )
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            os.unlink(snapshot_name, dir_fd=context.dir_fd)
        except FileNotFoundError:
            pass
        if isinstance(exc, AV1V4R3PreparationCustodyRegistryError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool snapshot creation failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    snapshot_path = context.registry / snapshot_name
    if _hash_path(snapshot_path) != handle.binary_sha256:
        _remove_tool_snapshot(context, snapshot_name)
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool snapshot digest is invalid"
        )
    return snapshot_name


def _remove_tool_snapshot(context: _RegistryContext, snapshot_name: str) -> None:
    try:
        os.unlink(snapshot_name, dir_fd=context.dir_fd)
        os.fsync(context.dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool snapshot cleanup failed"
        ) from exc


def _cleanup_tool_snapshots(context: _RegistryContext) -> None:
    for name in os.listdir(context.dir_fd):
        if not name.startswith(".tool-probe-"):
            continue
        try:
            metadata = os.stat(name, dir_fd=context.dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation stale tool snapshot is invalid"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o500
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise AV1V4R3PreparationCustodyRegistryError(
                "AV1 v4 r3 preparation stale tool snapshot is invalid"
            )
        _remove_tool_snapshot(context, name)


def _hash_path(path: Path) -> str:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
        metadata = os.fstat(descriptor)
        return _hash_open_tool(descriptor, metadata)
    except OSError as exc:
        raise AV1V4R3PreparationCustodyRegistryError(
            "AV1 v4 r3 preparation tool snapshot is unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
