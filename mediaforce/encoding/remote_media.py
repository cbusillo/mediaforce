from __future__ import annotations

import inspect
import json
import math
import subprocess
import tempfile
import time
import threading
from copy import deepcopy
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path

from mediaforce.core import utils
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, ProcessDeadlineEnforcementError, run_command
from mediaforce.hosts.config import ssh_target_for_host
from mediaforce.hosts.transport import ssh_connection_lost
from mediaforce.remote import _ensure_remote_awake_for_ssh, run_remote_command, ssh_client_options

REMOTE_FFMPEG_VERSION_TIMEOUT_SECONDS = 20
REMOTE_STAT_TIMEOUT_SECONDS = 20
# The SSH connection itself takes time on top of what the measurement is allowed.
REMOTE_CONNECT_ALLOWANCE_SECONDS = 10


def source_observation_script() -> str:
    from mediaforce.library.source_evidence import observe_file

    functions = (utils.descriptor_content_version_fingerprint, utils.descriptor_sha256, utils.file_stat_signature, observe_file)
    return ("from __future__ import annotations\nimport hashlib, json, os, sys\nfrom pathlib import Path\nfrom typing import Any, Callable\n"
            + f"CONTENT_VERSION_SAMPLE_BYTES = {utils.CONTENT_VERSION_SAMPLE_BYTES}\n"
            + f"FILE_DIGEST_CHUNK_BYTES = {utils.FILE_DIGEST_CHUNK_BYTES}\n"
            + "\n".join(inspect.getsource(function) for function in functions)
            + "\nprint(json.dumps(observe_file(Path(sys.argv[1]), full_digest=sys.argv[2] == '1')))\n")


class RemoteMediaHostUnavailableError(RuntimeError):
    pass


class RemoteMediaCommands:
    """Runs the same ffmpeg and ffprobe commands on an encode computer over SSH.

    Stopping the local SSH client does not stop ffmpeg on that computer. Every command sent here reads a
    bounded number of frames or seconds, so whatever is left there finishes on its own shortly after.
    """

    def __init__(self, host: dict[str, object], *, connection: _EvidenceSSHMaster | None = None) -> None:
        self.host = deepcopy(host)
        self.host_key = ssh_target_for_host(self.host)
        self.host_label = str(self.host.get("label") or "").strip() or self.host_key
        self._ffmpeg_version: str | None = None
        self.connection = connection

    def begin_claim(self) -> None:
        self._ffmpeg_version = None

    def run(
            self,
            command: list[str],
            *,
            timeout_seconds: float,
            process_controller: ManagedProcessController | None,
    ) -> subprocess.CompletedProcess[str]:
        if self.connection is not None:
            self.connection.ensure_started(process_controller)
        try:
            result = run_remote_command(
                self.host,
                command,
                math.ceil(timeout_seconds) + REMOTE_CONNECT_ALLOWANCE_SECONDS,
                process_controller=process_controller,
                **({"control_path": self.connection.control_path} if self.connection is not None else {}),
            )
        except subprocess.TimeoutExpired as exc:
            if self.connection is not None and not self.connection.alive(process_controller):
                raise RemoteMediaHostUnavailableError(f"The connection to {self.host_label} was lost.") from exc
            raise
        if (self.connection is not None and result.returncode == 255
                and not self.connection.alive(process_controller)) or ssh_connection_lost(result.returncode, result.stderr):
            # A lost connection says nothing about the file, so it must not be recorded as a failed measurement.
            raise RemoteMediaHostUnavailableError(f"The connection to {self.host_label} was lost.")
        return result

    def ffmpeg_version(self, process_controller: ManagedProcessController | None = None) -> str:
        """The first line of `ffmpeg -version` on that computer; raises when the computer cannot answer."""
        if self._ffmpeg_version is not None:
            return self._ffmpeg_version
        try:
            if self.connection is not None:
                self.connection.ensure_started(process_controller)
            result = run_remote_command(
                self.host,
                ["ffmpeg", "-version"],
                REMOTE_FFMPEG_VERSION_TIMEOUT_SECONDS,
                process_controller=process_controller,
                **({"control_path": self.connection.control_path} if self.connection is not None else {}),
            )
        except (ProcessCancelledError, ProcessDeadlineEnforcementError):
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise RemoteMediaHostUnavailableError(f"{self.host_label} could not be reached.") from exc
        first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
        if result.returncode != 0 or not first_line:
            raise RemoteMediaHostUnavailableError(f"{self.host_label} could not run ffmpeg.")
        self._ffmpeg_version = first_line
        return first_line

    def observe_source(
            self, path: Path, *, full_digest: bool = False,
            process_controller: ManagedProcessController | None = None,
    ) -> dict[str, object]:
        result = self.run(
            ["python3", "-c", source_observation_script(), str(path), "1" if full_digest else "0"],
            timeout_seconds=3600 if full_digest else REMOTE_STAT_TIMEOUT_SECONDS,
            process_controller=process_controller,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{self.host_label} could not complete a content check for this file; the check will retry")
        observation = json.loads(result.stdout)
        if (not isinstance(observation, dict) or observation.get("strength") != "observed"
                or not isinstance(observation.get("sample_fingerprint"), str)
                or len(observation["sample_fingerprint"]) != 40
                or not isinstance(observation.get("stat"), list)
                or (full_digest and not observation.get("sha256"))):
            raise RuntimeError("Encode computer did not return a complete source observation")
        return observation

    def tool_lineage(self) -> dict[str, str]:
        return {
            "ffmpeg_version": self.ffmpeg_version(),
            "host": self.host_key,
            "host_label": self.host_label,
        }


class _EvidenceSSHMaster:
    """A foreground control process owned by the worker, outside individual command containment."""

    def __init__(self, host: dict[str, object]) -> None:
        self.host = deepcopy(host)
        self.directory = Path(tempfile.mkdtemp(prefix="mf-ssh-", dir="/tmp"))
        self.control_path = self.directory / "control"
        self.closed = False
        self.controller = ManagedProcessController()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None

    def _run(self) -> None:
        try:
            result = run_command([
                "ssh", *ssh_client_options(), "-M", "-N", "-n", "-o", "ControlPersist=no", "-o", "ForkAfterAuthentication=no",
                "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1",
                "-o", f"ControlPath={self.control_path}", ssh_target_for_host(self.host),
            ], process_controller=self.controller)
            if result.returncode:
                self.error = RemoteMediaHostUnavailableError("The measurement connection ended.")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.error = exc

    def ensure_started(self, controller: ManagedProcessController | None) -> None:
        if self.closed:
            raise RemoteMediaHostUnavailableError("The measurement connection is closed.")
        if self.controller.cleanup_unproven:
            raise ProcessDeadlineEnforcementError("Measurement connection cleanup is unproven.")
        if controller is not None:
            controller.throw_if_cancelled()
        if self.thread is not None:
            if self.thread.is_alive() and self.control_path.exists():
                return
            raise RemoteMediaHostUnavailableError("The measurement connection ended; this file will retry.")
        _ensure_remote_awake_for_ssh(self.host)
        if controller is not None:
            controller.throw_if_cancelled()
        master_thread = threading.Thread(target=self._run, name="evidence-ssh-master", daemon=True)
        self.thread = master_thread
        master_thread.start()
        deadline = time.monotonic() + 10
        try:
            while master_thread.is_alive():
                if controller is not None:
                    controller.throw_if_cancelled()
                if self.control_path.exists():
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            raise RemoteMediaHostUnavailableError("The measurement connection could not be opened.") from self.error
        except BaseException:
            self.close()
            raise

    def alive(self, controller: ManagedProcessController | None) -> bool:
        try:
            result = run_command([
                "ssh", "-o", "BatchMode=yes", "-S", str(self.control_path), "-O", "check", ssh_target_for_host(self.host),
            ], process_controller=controller, timeout=5)
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    def close(self) -> None:
        if self.closed:
            return
        self.controller.cancel()
        if self.thread is not None:
            self.thread.join(timeout=10)
        if (self.thread is not None and self.thread.is_alive()) or self.controller.cleanup_unproven:
            raise ProcessDeadlineEnforcementError("Measurement connection cleanup is unproven.")
        self.control_path.unlink(missing_ok=True)
        self.directory.rmdir()
        self.closed = True


class RemoteMediaConnections(AbstractContextManager):
    """Own only this worker's SSH masters; never use the user's control sockets."""

    def __init__(self) -> None:
        self._connections: list[RemoteMediaCommands] = []
        self._resources = ExitStack()

    def runner(self, host: dict[str, object]) -> RemoteMediaCommands:
        for runner in self._connections:
            if runner.connection is None or runner.connection.closed:
                continue
            if runner.host_key == ssh_target_for_host(host) and runner.host != host:
                runner.connection.close()
                continue
            if runner.host == host:
                thread = runner.connection.thread
                if thread is None or thread.is_alive():
                    runner.begin_claim()
                    return runner
                runner.connection.close()
        connection = _EvidenceSSHMaster(host)
        self._resources.callback(connection.close)
        runner = RemoteMediaCommands(host, connection=connection)
        self._connections.append(runner)
        return runner

    def __exit__(self, *_exc: object) -> None:
        try:
            self._resources.close()
        finally:
            self._connections.clear()
