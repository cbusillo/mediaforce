"""A Mac encode computer stays awake exactly as long as Mediaforce's work runs on it.

The remote scripts run in a real ``/bin/sh``. A stand-in ``caffeinate`` on ``PATH`` records the
process it was asked to watch and, like the real one, exits once that process is gone, so the
tests need neither macOS nor SSH.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch

from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.schedule_deadline import SCHEDULE_CLOSE_DEADLINE_KEY, SCHEDULE_DEADLINE_EXIT_CODE
from mediaforce.encoding import runner
from mediaforce.encoding.staged_host import STAGED_JOB_KEY, StagedJob, staged_job
from mediaforce.hosts.transport import run_remote_command

_FAKE_CAFFEINATE = """#!/bin/sh
printf '%s %s\\n' "$$" "$*" >> "$KEEP_AWAKE_LOG"
echo caffeinate-noise
echo caffeinate-noise >&2
while [ "$#" -gt 0 ]; do
    [ "$1" = -w ] && watched=$2
    shift
done
while kill -0 "$watched" 2>/dev/null; do sleep 0.05; done
"""

_FAKE_FFMPEG = """#!/bin/sh
sleep "$FAKE_ENCODE_SECONDS"
"""


def _wait_until(condition: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _shell_script(ssh_argument: str) -> str:
    program, flag, script = shlex.split(ssh_argument)
    assert program == "sh" and flag in {"-c", "-lc"}, ssh_argument
    return script


class RemoteKeepAwakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        tools = self.root / "bin"
        tools.mkdir()
        for name, body in (("caffeinate", _FAKE_CAFFEINATE), ("ffmpeg", _FAKE_FFMPEG)):
            tool = tools / name
            tool.write_text(body)
            tool.chmod(0o755)
        self.log = self.root / "keep-awake.log"
        self.env = {
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            "KEEP_AWAKE_LOG": str(self.log),
            "FAKE_ENCODE_SECONDS": "0.2",
            "TMPDIR": str(self.root),
        }

    def _start(self, script: str, *, stdin: int | None = None) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            ["/bin/sh", "-c", script], env=self.env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self._end_session, process)
        return process

    @staticmethod
    def _end_session(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _keep_awake_starts(self) -> list[tuple[int, int]]:
        """Each stand-in caffeinate's own pid and the pid it watches."""
        if not self.log.exists():
            return []
        starts = []
        for line in self.log.read_text().splitlines():
            pid, *args = line.split()
            starts.append((int(pid), int(args[args.index("-w") + 1])))
        return starts

    def _assert_kept_awake_for(self, shell: subprocess.Popen[bytes]) -> int:
        self.assertTrue(_wait_until(lambda: bool(self._keep_awake_starts())), "the work must keep the computer awake")
        [(keep_awake_pid, watched_pid)] = self._keep_awake_starts()
        self.assertEqual(watched_pid, shell.pid, "the assertion must end with the shell running the work")
        return keep_awake_pid

    def _assert_released(self, keep_awake_pid: int) -> None:
        self.assertTrue(_wait_until(lambda: not _process_exists(keep_awake_pid)), "nothing may keep it awake afterwards")

    def _remote_command_script(self, command: list[str], **kwargs: Any) -> str:
        host: dict[str, object] = {"host": "mac.example", **kwargs.pop("host", {})}
        run_remote_ssh = Mock(return_value=subprocess.CompletedProcess(["ssh"], 0, "", ""))
        run_remote_command(
            host,
            command,
            30,
            ssh_target_for_host=lambda payload: str(payload["host"]),
            remote_shell_path_export_line=lambda: ":",
            run_remote_ssh=run_remote_ssh,
            **kwargs,
        )
        program, flag, script = run_remote_ssh.call_args.args[1:]
        self.assertEqual((program, flag), ("sh", "-lc"))
        return script

    def test_remote_work_keeps_the_computer_awake_until_it_finishes_with_its_output_untouched(self) -> None:
        shell = self._start(self._remote_command_script(["sh", "-c", "sleep 0.3; printf done; exit 7"]))
        keep_awake_pid = self._assert_kept_awake_for(shell)

        stdout, stderr = shell.communicate(timeout=10)

        self.assertEqual(shell.returncode, 7)
        self.assertEqual(stdout, b"done")
        self.assertNotIn(b"caffeinate", stderr)
        self._assert_released(keep_awake_pid)

    def test_a_probe_that_leaves_the_computer_asleep_does_not_keep_it_awake(self) -> None:
        shell = self._start(self._remote_command_script(["true"], wake_before_connect=False))

        shell.communicate(timeout=10)

        self.assertEqual(shell.returncode, 0)
        self.assertEqual(self._keep_awake_starts(), [])

    def test_killing_the_work_shell_releases_the_computer(self) -> None:
        shell = self._start(self._remote_command_script(["sleep", "60"]))
        keep_awake_pid = self._assert_kept_awake_for(shell)

        os.kill(shell.pid, signal.SIGKILL)
        shell.wait(timeout=5)

        self._assert_released(keep_awake_pid)

    def test_reaching_the_schedule_deadline_releases_the_computer(self) -> None:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=1)
        script = self._remote_command_script(
            ["sleep", "30"],
            host={SCHEDULE_CLOSE_DEADLINE_KEY: deadline.isoformat()},
            process_controller=ManagedProcessController(),
        )
        shell = self._start(script)
        keep_awake_pid = self._assert_kept_awake_for(shell)

        shell.communicate(timeout=10)

        self.assertEqual(shell.returncode, SCHEDULE_DEADLINE_EXIT_CODE)
        self._assert_released(keep_awake_pid)

    def _encode_ssh_argument(self, host: dict[str, Any], **kwargs: Any) -> str:
        commands: list[list[str]] = []

        def tracked(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(runner, "pull_output"):
            runner.run_encode_command(
                ffmpeg_cmd=["ffmpeg", "-i", str(self.root / "source.mkv"), str(self.root / "out" / "E01.partial.mkv")],
                temp_output=self.root / "out" / "E01.partial.mkv",
                staging_path=self.root / "out" / "E01.mkv",
                overwrite=False,
                process_controller=None,
                host=host,
                execution_mode_for_host=lambda _host: "ssh",
                host_media_access_for_host=lambda _host: str(host.get("media_access") or "mounted"),
                remote_shell_path_export_line=lambda: ":",
                ssh_client_options=lambda: [],
                ffmpeg_command_with_progress=lambda cmd: cmd,
                run_tracked_process_fn=tracked,
                run_streamed_remote_encode_command_fn=Mock(side_effect=AssertionError("not a streamed encode")),
                **kwargs,
            )
        [ssh_cmd] = commands
        return ssh_cmd[-1]

    def test_a_dropped_link_ends_a_mounted_encode_and_releases_the_computer(self) -> None:
        self.env["FAKE_ENCODE_SECONDS"] = "60"
        argument = self._encode_ssh_argument({"key": "mac", "mode": "ssh"}, progress_callback=lambda _snapshot: None)
        shell = self._start(_shell_script(argument), stdin=subprocess.PIPE)
        keep_awake_pid = self._assert_kept_awake_for(shell)

        assert shell.stdin is not None
        shell.stdin.close()
        shell.wait(timeout=15)

        self._assert_released(keep_awake_pid)

    def test_a_staged_encode_keeps_the_computer_awake_until_it_finishes(self) -> None:
        scratch = PurePosixPath(str(self.root / "scratch"))
        job = StagedJob(scratch_dir=scratch, source_path=scratch / "source.mkv", output_path=scratch / "output.mkv")
        argument = self._encode_ssh_argument(
            {"key": "mac", "mode": "ssh", "media_access": "stream", STAGED_JOB_KEY: job.to_payload()},
        )
        shell = self._start(_shell_script(argument))
        keep_awake_pid = self._assert_kept_awake_for(shell)

        shell.communicate(timeout=10)

        self.assertEqual(shell.returncode, 0)
        self._assert_released(keep_awake_pid)

    def test_a_streamed_encode_keeps_the_computer_awake_until_it_finishes(self) -> None:
        source = self.root / "source.mkv"
        source.write_bytes(b"source")
        shells: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def local_shell(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            # The streamed encode opens its connection in binary mode.
            shell = cast(subprocess.Popen[bytes], real_popen(["/bin/sh", "-c", _shell_script(argv[-1])], env=self.env, **kwargs))
            self.addCleanup(self._end_session, shell)
            shells.append(shell)
            return shell

        with patch.object(runner.subprocess, "Popen", side_effect=local_shell):
            result = runner.run_streamed_remote_encode_command(
                ffmpeg_cmd=["ffmpeg", "-i", str(source), str(self.root / "E01.partial.mkv")],
                temp_output=self.root / "E01.partial.mkv",
                source_path=source,
                process_controller=None,
                host={"key": "mac", "mode": "ssh"},
                progress_callback=None,
                ssh_client_options=lambda: [],
                build_streaming_remote_ffmpeg_command=lambda *_args, **_kwargs: ["ffmpeg"],
                update_ffmpeg_progress_state=lambda *_args, **_kwargs: None,
                process_cancelled_error=ProcessCancelledError,
            )

        self.assertEqual(result.returncode, 0)
        [shell] = shells
        self._assert_released(self._assert_kept_awake_for(shell))

    def test_staging_keeps_the_computer_awake_while_the_scratch_copy_exists(self) -> None:
        source = self.root / "Episode.mkv"
        source.write_bytes(b"source-bytes" * 1000)
        keepers: list[subprocess.Popen[bytes]] = []

        def run_remote(_host: Any, command: list[str], _timeout: int, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["/bin/sh", "-c", command[-1]], env=self.env, capture_output=True, text=True)

        def local_ssh(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            is_keeper = "ServerAliveInterval=30" in argv
            script = _shell_script(argv[-1]) if is_keeper else argv[-1]
            # Staging opens its connections in binary mode.
            process = cast(subprocess.Popen[bytes], subprocess.Popen(["/bin/sh", "-c", script], env=self.env, **kwargs))
            if is_keeper:
                keepers.append(process)
            return process

        with staged_job(
                {"key": "mac", "scratch_root": str(self.root / "scratch")}, source, output_suffix=".mkv",
                ssh_target="mac", ssh_options=[], run_remote_command=run_remote, popen=local_ssh,
        ):
            [keeper] = keepers
            keep_awake_pid = self._assert_kept_awake_for(keeper)
            self.assertTrue(_process_exists(keep_awake_pid))

        self._assert_released(keep_awake_pid)


if __name__ == "__main__":
    unittest.main()
