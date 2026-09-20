"""Stage a source on an encode host that has scratch space but no media mount.

A stream host normally receives its source through a pipe, which cannot seek and keeps the
quality search on the controller. When the host declares a ``scratch_root`` the controller
instead copies the source into a per-job scratch directory, runs the search and the encode on
the host against that local file, pulls the output back, and removes the directory.

Scratch space is recovered three ways: the controller removes the directory when the job
ends, a keeper connection removes it when the controller goes away, and a sweep before each
job removes directories whose keeper is no longer running.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mediaforce.core.type_defs import object_dict

STAGED_JOB_KEY = "staged_job"
SCRATCH_DIR_PREFIX = ".mediaforce-staged-"
KEEPER_PID_FILE = "keeper.pid"
STAGED_SOURCE_STEM = "source"
STAGED_OUTPUT_STEM = "output"
# Source copy, the encoded output (bounded by the source size) and quality-search samples.
SCRATCH_SOURCE_MULTIPLIER = 2.25
SCRATCH_MARGIN_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
REMOTE_CONTROL_TIMEOUT_SECONDS = 60
# A directory without a keeper pid is only an orphan once stage-in could not still be running.
ORPHAN_GRACE_MINUTES = 10

RunRemoteCommand = Callable[..., subprocess.CompletedProcess[str]]


class StagedScratchError(RuntimeError):
    """The host could not stage, verify or return a file through its scratch directory."""


@dataclass(frozen=True, slots=True)
class StagedJob:
    scratch_dir: PurePosixPath
    source_path: PurePosixPath
    output_path: PurePosixPath

    def to_payload(self) -> dict[str, str]:
        return {
            "scratch_dir": str(self.scratch_dir),
            "source_path": str(self.source_path),
            "output_path": str(self.output_path),
        }


def host_scratch_root(host: dict[str, Any] | None) -> PurePosixPath | None:
    value = str(object_dict(host).get("scratch_root") or "").strip()
    if not value:
        return None
    root = PurePosixPath(value)
    # A relative or root-level scratch path would make the sweep's rm -rf unsafe.
    if not root.is_absolute() or len(root.parts) < 2:
        return None
    return root


def staged_job_for_host(host: dict[str, Any] | None) -> StagedJob | None:
    payload = object_dict(object_dict(host).get(STAGED_JOB_KEY))
    if not payload:
        return None
    return StagedJob(
        scratch_dir=PurePosixPath(str(payload["scratch_dir"])),
        source_path=PurePosixPath(str(payload["source_path"])),
        output_path=PurePosixPath(str(payload["output_path"])),
    )


def required_scratch_bytes(source_size_bytes: int) -> int:
    return int(source_size_bytes * SCRATCH_SOURCE_MULTIPLIER) + SCRATCH_MARGIN_BYTES


def sweep_script(scratch_root: PurePosixPath) -> str:
    """Remove scratch directories whose keeper process is gone."""
    root = shlex.quote(str(scratch_root))
    prefix = shlex.quote(SCRATCH_DIR_PREFIX)
    return "\n".join(
        [
            f"root={root}",
            '[ -d "$root" ] || exit 0',
            f'for dir in "$root"/{prefix}*; do',
            '  [ -d "$dir" ] || continue',
            f'  pid=$(cat "$dir/{KEEPER_PID_FILE}" 2>/dev/null || true)',
            '  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then continue; fi',
            '  if [ -z "$pid" ] && [ -z "$(find "$dir" -maxdepth 0 -mmin +'
            f'{ORPHAN_GRACE_MINUTES} 2>/dev/null)" ]; then continue; fi',
            '  rm -rf "$dir"',
            "done",
        ]
    )


def keeper_script(scratch_dir: PurePosixPath) -> str:
    """Hold the scratch directory for as long as this connection lives.

    ``cat`` blocks on stdin, so the shell exits when the controller closes the connection or
    disappears. The exit trap stops anything still using the directory and removes it. ``cat``
    runs in the background because a shell defers its traps while a foreground command blocks;
    ``wait`` returns as soon as a signal arrives.
    """
    directory = shlex.quote(str(scratch_dir))
    return "\n".join(
        [
            "set -u",
            f"dir={directory}",
            "reader=",
            'cleanup() { [ -n "$reader" ] && kill "$reader" 2>/dev/null; '
            'pkill -f -- "$dir/" 2>/dev/null || true; rm -rf "$dir"; }',
            "trap cleanup EXIT",
            "trap 'exit 1' HUP INT TERM",
            'mkdir -p "$dir"',
            f'echo $$ > "$dir/{KEEPER_PID_FILE}"',
            "echo ready",
            # A background command's stdin is /dev/null in some shells, so pass the connection
            # to it on its own descriptor.
            "exec 3<&0",
            "cat <&3 >/dev/null &",
            "reader=$!",
            'wait "$reader"',
        ]
    )


def remote_ffmpeg_command(ffmpeg_cmd: list[str], job: StagedJob, *, executable: str | None = None) -> list[str]:
    """Point an encode command at the staged source and a scratch output file."""
    command = list(ffmpeg_cmd[:-1]) + [str(job.output_path)]
    command[command.index("-i") + 1] = str(job.source_path)
    command[0] = executable or Path(command[0]).name
    return command


def _free_bytes_script(scratch_root: PurePosixPath) -> str:
    root = shlex.quote(str(scratch_root))
    return f'mkdir -p {root} && df -Pk {root} | tail -1 | awk \'{{print $4}}\''


def _digest_script(path: PurePosixPath) -> str:
    target = shlex.quote(str(path))
    return f"(sha256sum {target} 2>/dev/null || shasum -a 256 {target}) | awk '{{print $1}}'"


@contextmanager
def staged_job(
        host: dict[str, Any],
        source_path: Path,
        *,
        output_suffix: str,
        ssh_target: str,
        ssh_options: list[str],
        run_remote_command: RunRemoteCommand,
        process_controller: Any | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> Iterator[StagedJob]:
    """Stage ``source_path`` on the host for the duration of the block."""
    scratch_root = host_scratch_root(host)
    if scratch_root is None:
        raise StagedScratchError("The encode host has no usable scratch folder configured.")

    def remote(script: str, *, timeout: int = REMOTE_CONTROL_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        return run_remote_command(host, ["sh", "-c", script], timeout, process_controller=process_controller)

    remote(sweep_script(scratch_root))
    source_size = source_path.stat().st_size
    free_result = remote(_free_bytes_script(scratch_root))
    free_kib = (free_result.stdout or "").strip().splitlines()[-1:] or [""]
    if free_result.returncode != 0 or not free_kib[0].isdigit():
        raise StagedScratchError(f"Could not measure scratch space on the encode host: {free_result.stderr.strip()}")
    if int(free_kib[0]) * 1024 < required_scratch_bytes(source_size):
        raise StagedScratchError(
            f"The encode host scratch folder has {int(free_kib[0]) // (1024 * 1024)} GiB free; "
            f"this file needs about {required_scratch_bytes(source_size) // (1024 ** 3) + 1} GiB."
        )

    scratch_dir = scratch_root / f"{SCRATCH_DIR_PREFIX}{uuid.uuid4().hex}"
    job = StagedJob(
        scratch_dir=scratch_dir,
        source_path=scratch_dir / f"{STAGED_SOURCE_STEM}{source_path.suffix.lower()}",
        output_path=scratch_dir / f"{STAGED_OUTPUT_STEM}{output_suffix}",
    )
    keeper = popen(
        ["ssh", *ssh_options, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", ssh_target,
         f"sh -c {shlex.quote(keeper_script(scratch_dir))}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = keeper.stdout.readline() if keeper.stdout is not None else b""
        if ready.strip() != b"ready":
            raise StagedScratchError("The encode host could not create its scratch folder.")
        _copy_source(job, source_path, source_size, ssh_target=ssh_target, ssh_options=ssh_options,
                     remote=remote, process_controller=process_controller, popen=popen)
        yield job
    finally:
        _release_keeper(keeper)
        try:
            remote(f"rm -rf {shlex.quote(str(scratch_dir))}")
        except Exception:  # noqa: BLE001 - the keeper trap and the next sweep also remove it.
            pass


def _copy_source(
        job: StagedJob,
        source_path: Path,
        source_size: int,
        *,
        ssh_target: str,
        ssh_options: list[str],
        remote: Callable[..., subprocess.CompletedProcess[str]],
        process_controller: Any | None,
        popen: Callable[..., subprocess.Popen[bytes]],
) -> None:
    writer = popen(
        ["ssh", *ssh_options, ssh_target, f"cat > {shlex.quote(str(job.source_path))}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process_controller is not None:
        process_controller.attach(writer, terminate_process_group=True)
    digest = hashlib.sha256()
    try:
        assert writer.stdin is not None
        with source_path.open("rb") as source_file:
            while chunk := source_file.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
                writer.stdin.write(chunk)
        writer.stdin.close()
        return_code = writer.wait()
    except (BrokenPipeError, OSError) as exc:
        writer.kill()
        writer.wait()
        detail = (writer.stderr.read().decode(errors="replace") if writer.stderr else "").strip()
        raise StagedScratchError(f"Copying the source to the encode host failed: {detail or exc}") from exc
    finally:
        if process_controller is not None:
            process_controller.clear(writer)
    if process_controller is not None and process_controller.cancelled:
        process_controller.throw_if_cancelled()
    if return_code != 0:
        detail = (writer.stderr.read().decode(errors="replace") if writer.stderr else "").strip()
        raise StagedScratchError(f"Copying the source to the encode host failed: {detail or return_code}")
    # The copy is long enough for a link to drop mid-way, so prove the host holds the same bytes.
    verified = remote(_digest_script(job.source_path), timeout=max(REMOTE_CONTROL_TIMEOUT_SECONDS, source_size // 20_000_000))
    if (verified.stdout or "").strip() != digest.hexdigest():
        raise StagedScratchError("The source copied to the encode host does not match the original.")


def pull_output(
        job: StagedJob,
        local_path: Path,
        *,
        ssh_target: str,
        ssh_options: list[str],
        process_controller: Any | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> None:
    """Copy the encoded output from the scratch directory to the controller's staging path."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    script = f"stat -c %s {shlex.quote(str(job.output_path))} 2>/dev/null || stat -f %z {shlex.quote(str(job.output_path))}"
    size_result = subprocess.run(["ssh", *ssh_options, ssh_target, script], capture_output=True, text=True,
                                 timeout=REMOTE_CONTROL_TIMEOUT_SECONDS)
    expected_size = (size_result.stdout or "").strip()
    if size_result.returncode != 0 or not expected_size.isdigit():
        raise StagedScratchError("The encode host did not produce an output file.")
    with local_path.open("wb") as output_file:
        reader = popen(
            ["ssh", *ssh_options, ssh_target, f"cat {shlex.quote(str(job.output_path))}"],
            stdin=subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if process_controller is not None:
            process_controller.attach(reader, terminate_process_group=True)
        try:
            return_code = reader.wait()
        finally:
            if process_controller is not None:
                process_controller.clear(reader)
    if process_controller is not None and process_controller.cancelled:
        process_controller.throw_if_cancelled()
    if return_code != 0 or local_path.stat().st_size != int(expected_size):
        raise StagedScratchError("Copying the encoded output back from the encode host failed.")


def _release_keeper(keeper: subprocess.Popen[bytes]) -> None:
    """Close the keeper's stdin so its exit trap removes the scratch directory."""

    def close() -> None:
        try:
            if keeper.stdin is not None:
                keeper.stdin.close()
            keeper.wait(timeout=REMOTE_CONTROL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - fall through to a hard stop.
            keeper.kill()

    closer = threading.Thread(target=close, daemon=True)
    closer.start()
    closer.join(timeout=REMOTE_CONTROL_TIMEOUT_SECONDS + 5)
