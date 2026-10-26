import inspect
import json
import math
import subprocess
from pathlib import Path

from mediaforce.core import utils
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.hosts.config import ssh_target_for_host
from mediaforce.hosts.transport import ssh_connection_lost
from mediaforce.remote import run_remote_command

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

    def __init__(self, host: dict[str, object]) -> None:
        self.host = dict(host)
        self.host_key = ssh_target_for_host(self.host)
        self.host_label = str(self.host.get("label") or "").strip() or self.host_key
        self._ffmpeg_version: str | None = None

    def run(
            self,
            command: list[str],
            *,
            timeout_seconds: float,
            process_controller: ManagedProcessController | None,
    ) -> subprocess.CompletedProcess[str]:
        result = run_remote_command(
            self.host,
            command,
            math.ceil(timeout_seconds) + REMOTE_CONNECT_ALLOWANCE_SECONDS,
            process_controller=process_controller,
        )
        if ssh_connection_lost(result.returncode, result.stderr):
            # A lost connection says nothing about the file, so it must not be recorded as a failed measurement.
            raise RemoteMediaHostUnavailableError(f"The connection to {self.host_label} was lost.")
        return result

    def ffmpeg_version(self, process_controller: ManagedProcessController | None = None) -> str:
        """The first line of `ffmpeg -version` on that computer; raises when the computer cannot answer."""
        if self._ffmpeg_version is not None:
            return self._ffmpeg_version
        try:
            result = run_remote_command(
                self.host,
                ["ffmpeg", "-version"],
                REMOTE_FFMPEG_VERSION_TIMEOUT_SECONDS,
                process_controller=process_controller,
            )
        except ProcessCancelledError:
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

