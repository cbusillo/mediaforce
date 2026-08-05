from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1ValidationV3Tier1CommandResult,
    AV1ValidationV3Tier1ExecutionContext,
    AV1ValidationV3Tier1ExecutorError,
    AV1ValidationV3Tier1HashResult,
    build_av1_validation_v3_tier1_fixture_plans,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    AV1ValidationV3Tier1CompatAuthorizationError,
    assert_av1_validation_v3_tier1_compat_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_probe import (
    AV1ValidationV3Tier1CompatExecutionContext,
    build_av1_validation_v3_tier1_compat_probe_plans,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_authorization import (
    AV1ValidationV3Tier1ResidualAuthorizationError,
    assert_av1_validation_v3_tier1_residual_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_probe import (
    AV1_VALIDATION_V3_TIER1_RESIDUAL_EXPECTED_DECODED_BYTE_COUNT,
    AV1ValidationV3Tier1ResidualExecutionContext,
    build_av1_validation_v3_tier1_residual_probe_plans,
)

_DEFAULT_MAX_STREAM_BYTES = 800_000_000
if AV1_VALIDATION_V3_TIER1_RESIDUAL_EXPECTED_DECODED_BYTE_COUNT > _DEFAULT_MAX_STREAM_BYTES:
    raise RuntimeError("AV1 v3 Tier 1 residual output exceeds the default stream limit")
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1GrantError,
    assert_av1_validation_v3_tier1_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    AV1ValidationV3Tier1ConfigSnapshotError,
    av1_validation_v3_tier1_config_snapshot_sha256,
    assert_av1_validation_v3_tier1_config_snapshot_matches,
)
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeLease,
    MediaforceRuntimeLockOwnershipError,
    assert_mediaforce_runtime_lock_held,
    exclusive_mediaforce_runtime_lock,
)


AV1_VALIDATION_V3_TIER1_RUNTIME_SCHEMA = "mediaforce.av1_cold_start_v3_tier1_toolchain"
AV1_VALIDATION_V3_TIER1_RUNTIME_SCHEMA_VERSION = 1

_PROGRAM_NAMES = ("ffmpeg", "ffprobe")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMEOUT_RETURN_CODE = -124
_OUTPUT_LIMIT_RETURN_CODE = -125
_SPAWN_FAILURE_RETURN_CODE = -126
_MAX_CHUNK_BYTES = 8 << 20


class AV1ValidationV3Tier1RuntimeError(AV1ValidationV3Tier1ExecutorError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ProgramBinding:
    name: str
    path: Path
    file_sha256: str
    device: int
    inode: int
    size: int
    modified_ns: int

    def semantic_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "file_sha256": self.file_sha256,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ToolchainBinding:
    programs: tuple[AV1ValidationV3Tier1ProgramBinding, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if tuple(program.name for program in self.programs) != _PROGRAM_NAMES:
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 toolchain programs are invalid")
        for program in self.programs:
            if (
                not program.path.is_absolute()
                or not _SHA256_RE.fullmatch(program.file_sha256)
                or program.device < 0
                or program.inode < 0
                or program.size <= 0
                or program.modified_ns < 0
            ):
                raise AV1ValidationV3Tier1RuntimeError(
                    "AV1 v3 Tier 1 toolchain program binding is invalid"
                )
        if self.payload_sha256 != _toolchain_payload_sha256(self.programs):
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 toolchain digest is invalid")

    def program(self, name: str) -> AV1ValidationV3Tier1ProgramBinding:
        for program in self.programs:
            if program.name == name:
                return program
        raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 command program is not allowed")


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1RuntimeLimits:
    timeout_seconds: float = 300.0
    max_stdout_bytes: int = 1 << 20
    max_stderr_bytes: int = 1 << 16
    max_stream_bytes: int = _DEFAULT_MAX_STREAM_BYTES
    chunk_bytes: int = 1 << 20

    def __post_init__(self) -> None:
        if (
            self.timeout_seconds <= 0
            or self.max_stdout_bytes <= 0
            or self.max_stderr_bytes <= 0
            or self.max_stream_bytes <= 0
            or self.chunk_bytes <= 0
            or self.chunk_bytes > self.max_stream_bytes
            or self.chunk_bytes > _MAX_CHUNK_BYTES
        ):
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 runtime limits are invalid")


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CommandDiagnostic:
    program: str
    argv_sha256: str
    outcome: str
    returncode: int
    stdout_bytes: int
    stderr_bytes: int
    stderr_truncated: bool


class AV1ValidationV3Tier1PausedRuntimeExecutor:
    def __init__(
        self,
        *,
        lease: MediaforceRuntimeLease,
        output_root: Path,
        toolchain: AV1ValidationV3Tier1ToolchainBinding,
        limits: AV1ValidationV3Tier1RuntimeLimits,
        buffered_commands: frozenset[tuple[str, ...]],
        streaming_commands: frozenset[tuple[str, ...]],
    ) -> None:
        self._lease = lease
        self._output_root = output_root
        self._toolchain = toolchain
        self._limits = limits
        self._buffered_commands = buffered_commands
        self._streaming_commands = streaming_commands
        self._diagnostics: list[AV1ValidationV3Tier1CommandDiagnostic] = []
        self._owned_outputs: dict[Path, tuple[int, int, int, int, int] | None] = {}

    @property
    def diagnostics(self) -> tuple[AV1ValidationV3Tier1CommandDiagnostic, ...]:
        return tuple(self._diagnostics)

    def run(self, args: Sequence[str]) -> AV1ValidationV3Tier1CommandResult:
        result = self._run_process(args, streaming=False)
        return AV1ValidationV3Tier1CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_streaming_sha256(self, args: Sequence[str]) -> AV1ValidationV3Tier1HashResult:
        result = self._run_process(args, streaming=True)
        return AV1ValidationV3Tier1HashResult(
            returncode=result.returncode,
            content_sha256=result.content_sha256,
            byte_count=result.stdout_bytes,
            stderr=result.stderr,
        )

    def cleanup(self) -> tuple[str, ...]:
        failures: list[str] = []
        for path, expected_identity in sorted(self._owned_outputs.items(), key=lambda item: str(item[0])):
            try:
                path_info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                failures.append(path.name)
                continue
            if (
                expected_identity is None
                or not stat.S_ISREG(path_info.st_mode)
                or _file_identity(path_info) != expected_identity
            ):
                failures.append(path.name)
                continue
            try:
                path.unlink()
            except OSError:
                failures.append(path.name)
        return tuple(failures)

    def _run_process(
        self,
        args: Sequence[str],
        *,
        streaming: bool,
    ) -> _ProcessResult:
        self._lease.assert_active()
        command, program = self._validated_command(args, streaming=streaming)
        candidate_output: Path | None = None
        if program.name == "ffmpeg" and command[-1] != "-":
            candidate_output = Path(command[-1])
            if candidate_output.is_absolute():
                self._owned_outputs.setdefault(candidate_output, None)
        try:
            process = subprocess.Popen(
                command,
                executable=str(program.path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._output_root,
                env=_sanitized_environment(),
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            stderr_raw = str(exc).encode("utf-8", errors="replace")
            stderr_truncated = len(stderr_raw) > self._limits.max_stderr_bytes
            stderr = _bounded_error_text(str(exc), self._limits.max_stderr_bytes)
            result = _ProcessResult(
                returncode=_SPAWN_FAILURE_RETURN_CODE,
                stdout=b"",
                content_sha256="",
                stdout_bytes=0,
                stderr=stderr,
                stderr_bytes=len(stderr.encode("utf-8")),
                stderr_truncated=stderr_truncated,
                outcome="spawn_failed",
            )
            self._record_diagnostic(args, result)
            return result
        result = self._collect_process(process, streaming=streaming)
        if candidate_output is not None:
            self._capture_output_identity(candidate_output)
        self._record_diagnostic(args, result)
        return result

    def _capture_output_identity(self, path: Path) -> None:
        try:
            path_info = path.lstat()
        except OSError:
            return
        if stat.S_ISREG(path_info.st_mode):
            self._owned_outputs[path] = _file_identity(path_info)

    def _validated_command(
        self,
        args: Sequence[str],
        *,
        streaming: bool,
    ) -> tuple[tuple[str, ...], AV1ValidationV3Tier1ProgramBinding]:
        if isinstance(args, (str, bytes)) or not args:
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 command is invalid")
        values = tuple(args)
        if not all(isinstance(part, str) and part and "\0" not in part for part in values):
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 command arguments are invalid")
        allowed_commands = self._streaming_commands if streaming else self._buffered_commands
        if values not in allowed_commands:
            raise AV1ValidationV3Tier1RuntimeError(
                "AV1 v3 Tier 1 command is not derived from the frozen fixture matrix"
            )
        program = self._toolchain.program(values[0])
        _assert_program_current(program)
        for part in values[1:]:
            path = Path(part)
            if path.is_absolute():
                resolved = path.resolve(strict=False)
                if resolved != self._output_root and not resolved.is_relative_to(self._output_root):
                    raise AV1ValidationV3Tier1RuntimeError(
                        "AV1 v3 Tier 1 command path is outside the output root"
                    )
        return (str(program.path), *values[1:]), program

    def _collect_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        streaming: bool,
    ) -> _ProcessResult:
        if process.stdout is None or process.stderr is None:
            _terminate_process_group(process)
            raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 process pipes are unavailable")
        selector = selectors.DefaultSelector()
        stdout_descriptor = process.stdout.fileno()
        stderr_descriptor = process.stderr.fileno()
        os.set_blocking(stdout_descriptor, False)
        os.set_blocking(stderr_descriptor, False)
        selector.register(stdout_descriptor, selectors.EVENT_READ, "stdout")
        selector.register(stderr_descriptor, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        stderr_bytes = 0
        stderr_truncated = False
        stdout_bytes = 0
        hasher = hashlib.sha256()
        outcome = "completed"
        forced_returncode: int | None = None
        deadline = time.monotonic() + self._limits.timeout_seconds
        try:
            while selector.get_map():
                self._lease.assert_active()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = "timed_out"
                    forced_returncode = _TIMEOUT_RETURN_CODE
                    _terminate_process_group(process)
                    break
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    try:
                        data = os.read(key.fd, self._limits.chunk_bytes)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fd)
                        continue
                    if key.data == "stderr":
                        stderr_bytes += len(data)
                        stderr.extend(data)
                        if len(stderr) > self._limits.max_stderr_bytes:
                            del stderr[:-self._limits.max_stderr_bytes]
                            stderr_truncated = True
                        continue
                    stdout_bytes += len(data)
                    if streaming:
                        hasher.update(data)
                        if stdout_bytes > self._limits.max_stream_bytes:
                            outcome = "stream_limit"
                            forced_returncode = _OUTPUT_LIMIT_RETURN_CODE
                            _terminate_process_group(process)
                            break
                    else:
                        stdout.extend(data)
                        if len(stdout) > self._limits.max_stdout_bytes:
                            del stdout[self._limits.max_stdout_bytes:]
                            outcome = "stdout_limit"
                            forced_returncode = _OUTPUT_LIMIT_RETURN_CODE
                            _terminate_process_group(process)
                            break
                if forced_returncode is not None:
                    break
            if forced_returncode is None:
                try:
                    returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    outcome = "timed_out"
                    returncode = _TIMEOUT_RETURN_CODE
                    _terminate_process_group(process)
            else:
                returncode = forced_returncode
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                _terminate_process_group(process)
        stderr_text = bytes(stderr).decode("utf-8", errors="replace")
        if stderr_truncated:
            stderr_text = "[stderr truncated; tail follows]\n" + stderr_text
        return _ProcessResult(
            returncode=returncode,
            stdout=bytes(stdout),
            content_sha256=f"sha256:{hasher.hexdigest()}" if streaming else "",
            stdout_bytes=stdout_bytes,
            stderr=stderr_text,
            stderr_bytes=stderr_bytes,
            stderr_truncated=stderr_truncated,
            outcome=outcome,
        )

    def _record_diagnostic(self, args: Sequence[str], result: _ProcessResult) -> None:
        self._diagnostics.append(AV1ValidationV3Tier1CommandDiagnostic(
            program=str(args[0]) if args else "",
            argv_sha256=f"sha256:{stable_json_hash(list(args))}",
            outcome=result.outcome,
            returncode=result.returncode,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            stderr_truncated=result.stderr_truncated,
        ))


class AV1ValidationV3Tier1RuntimeSession:
    def __init__(self, executor: AV1ValidationV3Tier1PausedRuntimeExecutor) -> None:
        self.executor = executor
        self.cleanup_passed = False
        self.cleanup_failures: tuple[str, ...] = ()

    @property
    def diagnostics(self) -> tuple[AV1ValidationV3Tier1CommandDiagnostic, ...]:
        return self.executor.diagnostics

    def _cleanup(self) -> None:
        self.cleanup_failures = self.executor.cleanup()
        self.cleanup_passed = not self.cleanup_failures


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    content_sha256: str
    stdout_bytes: int
    stderr: str
    stderr_bytes: int
    stderr_truncated: bool
    outcome: str


def build_av1_validation_v3_tier1_toolchain_binding(
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> AV1ValidationV3Tier1ToolchainBinding:
    programs = tuple(
        _build_program_binding(name, path)
        for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path))
    )
    return AV1ValidationV3Tier1ToolchainBinding(
        programs=programs,
        payload_sha256=_toolchain_payload_sha256(programs),
    )


@contextmanager
def paused_av1_validation_v3_tier1_runtime(
    config: MediaforceConfig,
    *,
    config_snapshot_bytes: bytes,
    context: AV1ValidationV3Tier1ExecutionContext,
    matrix: Mapping[str, object],
    output_directory: Path,
    repository_root: Path,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    limits: AV1ValidationV3Tier1RuntimeLimits | None = None,
) -> Iterator[AV1ValidationV3Tier1RuntimeSession]:
    try:
        assert_mediaforce_runtime_lock_held()
    except MediaforceRuntimeLockOwnershipError:
        pass
    else:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime requires a newly acquired exclusive pause lease"
        )
    output_root = _validated_output_root(
        output_directory,
        repository_root=repository_root,
    )
    try:
        assert_av1_validation_v3_tier1_config_snapshot_matches(
            config,
            config_snapshot_bytes,
        )
    except AV1ValidationV3Tier1ConfigSnapshotError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime effective config changed after the snapshot"
        ) from exc
    if (
        context.qualification_plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(config_snapshot_bytes)
    ):
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime config snapshot is not bound to the qualification plan"
        )
    _assert_toolchain_current(toolchain)
    if context.qualification_plan.toolchain_sha256 != toolchain.payload_sha256:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime toolchain is not bound to the qualification plan"
        )
    try:
        assert_av1_validation_v3_tier1_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1GrantError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime grant is not active"
        ) from exc
    plans = build_av1_validation_v3_tier1_fixture_plans(
        matrix,
        output_directory=output_root,
        repository_root=repository_root,
    )
    buffered_commands = frozenset(
        command
        for plan in plans
        for command in (plan.generate_args, plan.probe_args)
    )
    streaming_commands = frozenset(plan.content_hash_args for plan in plans)
    with exclusive_mediaforce_runtime_lock(
        config,
        owner_payload={
            "purpose": "av1-v3-tier1-synthetic-qualification",
            "plan_id": context.qualification_plan.plan_id,
            "request_id": context.request.request_id,
            "grant_id": context.grant.grant_id,
        },
    ) as lease:
        lease.assert_active()
        executor = AV1ValidationV3Tier1PausedRuntimeExecutor(
            lease=lease,
            output_root=output_root,
            toolchain=toolchain,
            limits=limits or AV1ValidationV3Tier1RuntimeLimits(),
            buffered_commands=buffered_commands,
            streaming_commands=streaming_commands,
        )
        session = AV1ValidationV3Tier1RuntimeSession(executor)
        try:
            yield session
        finally:
            session._cleanup()


@contextmanager
def paused_av1_validation_v3_tier1_compat_runtime(
    config: MediaforceConfig,
    *,
    config_snapshot_bytes: bytes,
    context: AV1ValidationV3Tier1CompatExecutionContext,
    matrix: Mapping[str, object],
    output_directory: Path,
    repository_root: Path,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    limits: AV1ValidationV3Tier1RuntimeLimits | None = None,
) -> Iterator[AV1ValidationV3Tier1RuntimeSession]:
    try:
        assert_mediaforce_runtime_lock_held()
    except MediaforceRuntimeLockOwnershipError:
        pass
    else:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 compatibility probe requires a newly acquired exclusive pause lease"
        )
    output_root = _validated_output_root(
        output_directory,
        repository_root=repository_root,
    )
    try:
        assert_av1_validation_v3_tier1_config_snapshot_matches(
            config,
            config_snapshot_bytes,
        )
    except AV1ValidationV3Tier1ConfigSnapshotError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 compatibility-probe config changed after the snapshot"
        ) from exc
    if (
        context.qualification_plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(config_snapshot_bytes)
    ):
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 compatibility-probe config is not bound to the plan"
        )
    _assert_toolchain_current(toolchain)
    if context.qualification_plan.toolchain_sha256 != toolchain.payload_sha256:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 compatibility-probe toolchain is not bound to the plan"
        )
    try:
        assert_av1_validation_v3_tier1_compat_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1CompatAuthorizationError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 compatibility-probe grant is not active"
        ) from exc
    plans = build_av1_validation_v3_tier1_compat_probe_plans(
        matrix,
        output_directory=output_root,
        repository_root=repository_root,
    )
    buffered_commands = frozenset(
        command
        for plan in plans
        for command in (
            plan.generate_args,
            *(surface.probe_args for surface in plan.surfaces),
        )
    )
    with exclusive_mediaforce_runtime_lock(
        config,
        owner_payload={
            "purpose": "av1-v3-tier1-compatibility-probe",
            "plan_id": context.qualification_plan.plan_id,
            "request_id": context.request.request_id,
            "grant_id": context.grant.grant_id,
        },
    ) as lease:
        lease.assert_active()
        executor = AV1ValidationV3Tier1PausedRuntimeExecutor(
            lease=lease,
            output_root=output_root,
            toolchain=toolchain,
            limits=limits or AV1ValidationV3Tier1RuntimeLimits(),
            buffered_commands=buffered_commands,
            streaming_commands=frozenset(),
        )
        session = AV1ValidationV3Tier1RuntimeSession(executor)
        try:
            yield session
        finally:
            session._cleanup()


@contextmanager
def paused_av1_validation_v3_tier1_residual_runtime(
    config: MediaforceConfig,
    *,
    config_snapshot_bytes: bytes,
    context: AV1ValidationV3Tier1ResidualExecutionContext,
    matrix: Mapping[str, object],
    output_directory: Path,
    repository_root: Path,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    limits: AV1ValidationV3Tier1RuntimeLimits | None = None,
) -> Iterator[AV1ValidationV3Tier1RuntimeSession]:
    try:
        assert_mediaforce_runtime_lock_held()
    except MediaforceRuntimeLockOwnershipError:
        pass
    else:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 residual probe requires a newly acquired exclusive pause lease"
        )
    output_root = _validated_output_root(
        output_directory,
        repository_root=repository_root,
    )
    try:
        assert_av1_validation_v3_tier1_config_snapshot_matches(
            config,
            config_snapshot_bytes,
        )
    except AV1ValidationV3Tier1ConfigSnapshotError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 residual-probe config changed after the snapshot"
        ) from exc
    if (
        context.qualification_plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(config_snapshot_bytes)
    ):
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 residual-probe config is not bound to the plan"
        )
    _assert_toolchain_current(toolchain)
    if context.qualification_plan.toolchain_sha256 != toolchain.payload_sha256:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 residual-probe toolchain is not bound to the plan"
        )
    try:
        assert_av1_validation_v3_tier1_residual_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1ResidualAuthorizationError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 residual-probe grant is not active"
        ) from exc
    plans = build_av1_validation_v3_tier1_residual_probe_plans(
        matrix,
        output_directory=output_root,
        repository_root=repository_root,
    )
    buffered_commands = frozenset(
        command
        for plan in plans
        for command in (
            plan.generate_args,
            *(surface.command_args for surface in plan.surfaces if not surface.streaming),
        )
    )
    streaming_commands = frozenset(
        surface.command_args
        for plan in plans
        for surface in plan.surfaces
        if surface.streaming
    )
    with exclusive_mediaforce_runtime_lock(
        config,
        owner_payload={
            "purpose": "av1-v3-tier1-residual-probe",
            "plan_id": context.qualification_plan.plan_id,
            "request_id": context.request.request_id,
            "grant_id": context.grant.grant_id,
        },
    ) as lease:
        lease.assert_active()
        executor = AV1ValidationV3Tier1PausedRuntimeExecutor(
            lease=lease,
            output_root=output_root,
            toolchain=toolchain,
            limits=limits or AV1ValidationV3Tier1RuntimeLimits(),
            buffered_commands=buffered_commands,
            streaming_commands=streaming_commands,
        )
        session = AV1ValidationV3Tier1RuntimeSession(executor)
        try:
            yield session
        finally:
            session._cleanup()


def _build_program_binding(name: str, path: Path) -> AV1ValidationV3Tier1ProgramBinding:
    try:
        resolved = path.expanduser().resolve(strict=True)
        path_info = resolved.stat()
    except OSError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            f"AV1 v3 Tier 1 {name} binary is unavailable"
        ) from exc
    if not stat.S_ISREG(path_info.st_mode) or not os.access(resolved, os.X_OK):
        raise AV1ValidationV3Tier1RuntimeError(
            f"AV1 v3 Tier 1 {name} binary is invalid"
        )
    return AV1ValidationV3Tier1ProgramBinding(
        name=name,
        path=resolved,
        file_sha256=_file_sha256(resolved),
        device=path_info.st_dev,
        inode=path_info.st_ino,
        size=path_info.st_size,
        modified_ns=path_info.st_mtime_ns,
    )


def _assert_toolchain_current(toolchain: AV1ValidationV3Tier1ToolchainBinding) -> None:
    for program in toolchain.programs:
        _assert_program_current(program, verify_hash=True)


def _assert_program_current(
    program: AV1ValidationV3Tier1ProgramBinding,
    *,
    verify_hash: bool = False,
) -> None:
    try:
        path_info = program.path.stat()
    except OSError as exc:
        raise AV1ValidationV3Tier1RuntimeError(
            f"AV1 v3 Tier 1 {program.name} binary changed"
        ) from exc
    if (
        not stat.S_ISREG(path_info.st_mode)
        or path_info.st_dev != program.device
        or path_info.st_ino != program.inode
        or path_info.st_size != program.size
        or path_info.st_mtime_ns != program.modified_ns
        or (verify_hash and _file_sha256(program.path) != program.file_sha256)
    ):
        raise AV1ValidationV3Tier1RuntimeError(
            f"AV1 v3 Tier 1 {program.name} binary changed"
        )


def _toolchain_payload_sha256(
    programs: Sequence[AV1ValidationV3Tier1ProgramBinding],
) -> str:
    payload = {
        "schema": AV1_VALIDATION_V3_TIER1_RUNTIME_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_RUNTIME_SCHEMA_VERSION,
        "programs": [program.semantic_payload() for program in programs],
    }
    return f"sha256:{stable_json_hash(payload)}"


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1 << 20):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _file_identity(path_info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        path_info.st_dev,
        path_info.st_ino,
        path_info.st_size,
        path_info.st_mtime_ns,
        path_info.st_ctime_ns,
    )


def _validated_output_root(output_directory: Path, *, repository_root: Path) -> Path:
    if not output_directory.is_absolute() or not repository_root.is_absolute():
        raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 runtime paths must be absolute")
    if output_directory.is_symlink():
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime output root cannot be a symlink"
        )
    try:
        output_root = output_directory.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV3Tier1RuntimeError("AV1 v3 Tier 1 runtime paths are invalid") from exc
    if not output_root.is_dir():
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime output root must be a directory"
        )
    if (
        output_root == repository
        or output_root.is_relative_to(repository)
        or repository.is_relative_to(output_root)
    ):
        raise AV1ValidationV3Tier1RuntimeError(
            "AV1 v3 Tier 1 runtime outputs must remain outside the repository"
        )
    return output_root


def _sanitized_environment() -> Mapping[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AV1ValidationV3Tier1RuntimeError(
                "AV1 v3 Tier 1 child process could not be reaped"
            ) from exc


def _bounded_error_text(value: str, limit: int) -> str:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value
    return "[stderr truncated; tail follows]\n" + raw[-limit:].decode("utf-8", errors="replace")
