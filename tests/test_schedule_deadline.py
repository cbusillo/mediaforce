import os
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from mediaforce.core.process_control import ManagedProcessController, ScheduleWindowClosedError
from mediaforce.core.schedule_deadline import SCHEDULE_CLOSE_DEADLINE_KEY, SCHEDULE_DEADLINE_EXIT_CODE, \
    SCHEDULE_DEADLINE_MARKER, guard_command_for_schedule_deadline, guard_shell_script_for_schedule_deadline, \
    process_result_reached_schedule_deadline
from mediaforce.hosts.transport import run_remote_command


class ScheduleDeadlineTests(TestCase):
    def test_guard_rejects_command_when_deadline_is_already_due(self) -> None:
        script = guard_shell_script_for_schedule_deadline(
            "printf 'should-not-run\\n'",
            datetime.now(tz=UTC) - timedelta(seconds=1),
        )

        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
            start_new_session=True,
        )

        self.assertEqual(result.returncode, SCHEDULE_DEADLINE_EXIT_CODE)
        self.assertNotIn("should-not-run", result.stdout)
        self.assertIn(SCHEDULE_DEADLINE_MARKER, result.stderr)

    def test_watchdog_stops_process_group_at_absolute_deadline(self) -> None:
        script = guard_shell_script_for_schedule_deadline(
            "sleep 30",
            datetime.now(tz=UTC) + timedelta(seconds=1),
        )
        started = time.monotonic()

        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=6,
            start_new_session=True,
        )

        self.assertEqual(result.returncode, SCHEDULE_DEADLINE_EXIT_CODE)
        self.assertIn(SCHEDULE_DEADLINE_MARKER, result.stderr)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_fast_command_cleans_up_watchdog(self) -> None:
        script = guard_shell_script_for_schedule_deadline(
            "printf 'done\\n'",
            datetime.now(tz=UTC) + timedelta(seconds=30),
        )
        started = time.monotonic()

        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
            start_new_session=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "done\n")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_killing_the_guard_shell_ends_its_command_and_watchdog_without_a_deadline_marker(self) -> None:
        self._assert_guard_shell_loss_ends_command(wrapped=False)

    def test_guard_inside_a_wrapper_shell_watches_its_own_shell(self) -> None:
        # A connection watcher runs the guard in a subshell, where $$ still names the wrapper.
        self._assert_guard_shell_loss_ends_command(wrapped=True)

    def _assert_guard_shell_loss_ends_command(self, *, wrapped: bool) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pids = Path(temp_dir)
            script = guard_shell_script_for_schedule_deadline(
                f'printf \'%s\\n\' "$mediaforce_watchdog_pid" > {pids / "watchdog.pid"}; '
                f'sleep 60 & printf \'%s\\n\' "$!" > {pids / "child.pid"}; wait',
                datetime.now(tz=UTC) + timedelta(seconds=60),
            )
            if wrapped:
                script = f'(\n{script}\n) &\nprintf \'%s\\n\' "$!" > {pids / "guard.pid"}\nwait\nsleep 60'
            outer_shell = subprocess.Popen(
                ["/bin/sh", "-c", script],
                env={**os.environ, "TMPDIR": temp_dir},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                watchdog_pid = _wait_for_pid_file(pids / "watchdog.pid")
                child_pid = _wait_for_pid_file(pids / "child.pid")
                guard_pid = _wait_for_pid_file(pids / "guard.pid") if wrapped else outer_shell.pid

                os.kill(guard_pid, signal.SIGKILL)
                if not wrapped:
                    outer_shell.wait(timeout=5)

                self.assertTrue(_wait_until_process_exits(child_pid, timeout_seconds=5.0))
                self.assertTrue(_wait_until_process_exits(watchdog_pid, timeout_seconds=5.0))
                self.assertEqual(list(Path(temp_dir).glob(".mediaforce-deadline-*")), [])
            finally:
                try:
                    os.killpg(outer_shell.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Gone already, or only the unreaped group leader is left.
                    pass
                outer_shell.wait(timeout=5)

    def test_success_after_deadline_is_rejected_even_if_watchdog_disappears(self) -> None:
        script = guard_shell_script_for_schedule_deadline(
            'kill -KILL "$mediaforce_watchdog_pid"; sleep 1.2',
            datetime.now(tz=UTC) + timedelta(seconds=1),
        )

        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
            start_new_session=True,
        )

        self.assertEqual(result.returncode, SCHEDULE_DEADLINE_EXIT_CODE)
        self.assertIn(SCHEDULE_DEADLINE_MARKER, result.stderr)

    def test_failed_process_at_or_after_deadline_is_schedule_interruption(self) -> None:
        deadline = datetime.now(tz=UTC) - timedelta(seconds=1)
        host = {SCHEDULE_CLOSE_DEADLINE_KEY: deadline.isoformat(timespec="seconds")}
        result = subprocess.CompletedProcess(["ffmpeg"], 255, "", "connection closed")

        self.assertTrue(process_result_reached_schedule_deadline(result, host))

    def test_deterministic_failure_at_deadline_is_not_masked(self) -> None:
        deadline = datetime.now(tz=UTC) - timedelta(seconds=1)
        host = {SCHEDULE_CLOSE_DEADLINE_KEY: deadline.isoformat(timespec="seconds")}
        result = subprocess.CompletedProcess(["ffmpeg"], 1, "", "invalid argument")

        self.assertFalse(process_result_reached_schedule_deadline(result, host))
        timeout_result = subprocess.CompletedProcess(["ffmpeg"], SCHEDULE_DEADLINE_EXIT_CODE, "", "timed out")
        self.assertFalse(process_result_reached_schedule_deadline(timeout_result, host))

    def test_local_deadline_guard_requires_managed_process_group(self) -> None:
        deadline = datetime.now(tz=UTC) + timedelta(minutes=5)
        host = {SCHEDULE_CLOSE_DEADLINE_KEY: deadline.isoformat(timespec="seconds")}

        with self.assertRaisesRegex(RuntimeError, "managed process controller"):
            guard_command_for_schedule_deadline(["ffmpeg", "-version"], host, process_controller=None)

    def test_remote_transport_classifies_disconnect_at_deadline_as_schedule_close(self) -> None:
        deadline = datetime.now(tz=UTC) - timedelta(seconds=1)
        host = {
            "host": "worker.example",
            SCHEDULE_CLOSE_DEADLINE_KEY: deadline.isoformat(timespec="seconds"),
        }
        run_remote_ssh = Mock(
            return_value=subprocess.CompletedProcess(["ssh"], 255, "", "Connection closed")
        )

        with self.assertRaises(ScheduleWindowClosedError):
            run_remote_command(
                host,
                ["ffmpeg", "-version"],
                30,
                process_controller=ManagedProcessController(),
                ssh_target_for_host=lambda payload: str(payload["host"]),
                remote_shell_path_export_line=lambda: "export PATH=/usr/bin:/bin",
                run_remote_ssh=run_remote_ssh,
            )

        remote_script = run_remote_ssh.call_args.args[-1]
        self.assertIn(SCHEDULE_DEADLINE_MARKER, remote_script)


def _wait_for_pid_file(path: Path, timeout_seconds: float = 5.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = path.read_text().strip() if path.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"No process id was written to {path}")


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_process_exits(pid: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_is_alive(pid)
