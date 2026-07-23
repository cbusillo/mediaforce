import subprocess
import time
from datetime import UTC, datetime, timedelta
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
