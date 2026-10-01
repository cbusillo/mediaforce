import math
import subprocess
from pathlib import Path

from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.hosts.config import ssh_target_for_host
from mediaforce.remote import run_remote_command

REMOTE_FFMPEG_VERSION_TIMEOUT_SECONDS = 20
REMOTE_STAT_TIMEOUT_SECONDS = 20
# macOS stat first; GNU stat when the BSD form is not understood. Prints "<size> <modified epoch seconds>".
_REMOTE_STAT_SCRIPT = 'stat -f "%z %m" "$1" 2>/dev/null || stat -c "%s %Y" "$1"'
# The SSH connection itself takes time on top of what the measurement is allowed.
REMOTE_CONNECT_ALLOWANCE_SECONDS = 10
_SSH_FAILURE_PREFIXES = (
    "ssh:",
    "client_loop:",
    "kex_exchange_identification:",
    "connection closed by",
    "connection to ",
    "timeout, server",
)


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
        if _ssh_connection_failed(result):
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

    def source_size_and_mtime(
            self,
            path: Path,
            *,
            process_controller: ManagedProcessController | None,
    ) -> tuple[int, int] | None:
        """Size in bytes and whole-second modification time of ``path`` as that computer sees it.

        None when that computer cannot see the file.
        """
        result = self.run(
            ["sh", "-c", _REMOTE_STAT_SCRIPT, "sh", str(path)],
            timeout_seconds=REMOTE_STAT_TIMEOUT_SECONDS,
            process_controller=process_controller,
        )
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) != 2 or not all(field.isdigit() for field in fields):
            return None
        return int(fields[0]), int(fields[1])

    def tool_lineage(self) -> dict[str, str]:
        return {
            "ffmpeg_version": self.ffmpeg_version(),
            "host": self.host_key,
            "host_label": self.host_label,
        }


def _ssh_connection_failed(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 255:
        return False
    return any(
        line.strip().lower().startswith(_SSH_FAILURE_PREFIXES)
        for line in (result.stderr or "").splitlines()
    )
