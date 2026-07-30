from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from mediaforce.core import process_control as process_control_module
from mediaforce.core.process_control import (
    ManagedProcessController,
    ProcessDeadlineExpiredError,
    ScheduleWindowClosedError,
    _terminate_process,
    run_command,
)


class ProcessControlTests(TestCase):
    def test_managed_command_accepts_stdin_text(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            process_controller=ManagedProcessController(),
            input_text="hello",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello")

    def test_schedule_cancellation_preserves_specific_error(self) -> None:
        controller = ManagedProcessController()

        controller.cancel(ScheduleWindowClosedError("Schedule closed."))

        with self.assertRaisesRegex(ScheduleWindowClosedError, "Schedule closed"):
            controller.throw_if_cancelled()

    def test_first_cancellation_reason_wins(self) -> None:
        controller = ManagedProcessController()

        controller.cancel()
        controller.cancel(ScheduleWindowClosedError("Schedule closed."))

        with self.assertRaisesRegex(RuntimeError, "Operation was cancelled") as raised:
            controller.throw_if_cancelled()
        self.assertNotIsInstance(raised.exception, ScheduleWindowClosedError)

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_activity_guard_prevents_managed_process_start(
            self,
            popen_mock: Mock,
    ) -> None:
        controller = ManagedProcessController()
        guard = Mock(
            side_effect=ScheduleWindowClosedError("Authorization expired.")
        )

        with (
            controller.activity_guard(guard),
            self.assertRaisesRegex(
                ScheduleWindowClosedError,
                "Authorization expired",
            ),
        ):
            run_command(["echo", "ok"], process_controller=controller)

        guard.assert_called_once_with()
        popen_mock.assert_not_called()

    def test_deadline_runner_rejects_target_after_parent_launch_pause(self) -> None:
        real_popen = process_control_module.subprocess.Popen
        controller = ManagedProcessController()

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "target-ran"

            def delayed_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
                time.sleep(0.2)
                return real_popen(*args, **kwargs)

            with (
                controller.absolute_deadline(
                    datetime.now(UTC) + timedelta(seconds=0.1)
                ),
                patch.object(
                    process_control_module.subprocess,
                    "Popen",
                    side_effect=delayed_popen,
                ),
                self.assertRaises(ProcessDeadlineExpiredError),
            ):
                run_command(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')",
                        str(marker_path),
                    ],
                    process_controller=controller,
                )

            self.assertFalse(marker_path.exists())

    def test_target_exit_124_is_not_misclassified_as_deadline(self) -> None:
        controller = ManagedProcessController()

        with controller.absolute_deadline(
            datetime.now(UTC) + timedelta(seconds=2)
        ):
            result = run_command(
                [sys.executable, "-c", "raise SystemExit(124)"],
                process_controller=controller,
            )

        self.assertEqual(result.returncode, 124)

    def test_deadline_watchdog_kills_descendant_after_target_leader_exits(self) -> None:
        controller = ManagedProcessController()

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "descendant-ran"
            target_script = "\n".join((
                "import subprocess",
                "import sys",
                "subprocess.Popen([",
                "    sys.executable, '-c',",
                "    \"from pathlib import Path; import sys, time; time.sleep(1.0); Path(sys.argv[1]).write_text('ran')\",",
                "    sys.argv[1],",
                "])",
            ))

            with (
                controller.absolute_deadline(
                    datetime.now(UTC) + timedelta(seconds=0.4)
                ),
                self.assertRaises(ProcessDeadlineExpiredError),
            ):
                run_command(
                    [
                        sys.executable,
                        "-c",
                        target_script,
                        str(marker_path),
                    ],
                    process_controller=controller,
                )

            time.sleep(0.8)
            self.assertFalse(marker_path.exists())

    def test_nested_absolute_deadlines_use_earliest_and_restore_previous(self) -> None:
        controller = ManagedProcessController()
        outer = datetime.now(UTC) + timedelta(seconds=10)
        later = outer + timedelta(seconds=10)
        earlier = outer - timedelta(seconds=5)

        with controller.absolute_deadline(outer):
            outer_deadline_ns = controller.process_deadline_ns()
            with controller.absolute_deadline(later):
                self.assertEqual(controller.process_deadline_ns(), outer_deadline_ns)
            self.assertEqual(controller.process_deadline_ns(), outer_deadline_ns)
            with controller.absolute_deadline(earlier):
                self.assertLess(
                    controller.process_deadline_ns() or 0,
                    outer_deadline_ns or 0,
                )
            self.assertEqual(controller.process_deadline_ns(), outer_deadline_ns)

        self.assertIsNone(controller.process_deadline_ns())

    def test_deadline_watchdog_kills_group_while_parent_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started_path = root / "target-started"
            descendant_path = root / "descendant-ran"
            target_script = "\n".join((
                "from pathlib import Path",
                "import subprocess",
                "import sys",
                "import time",
                "Path(sys.argv[1]).write_text('started')",
                "subprocess.Popen([",
                "    sys.executable, '-c',",
                "    \"from pathlib import Path; import sys, time; time.sleep(2.2); Path(sys.argv[1]).write_text('ran')\",",
                "    sys.argv[2],",
                "])",
                "time.sleep(10)",
            ))
            harness_script = "\n".join((
                "from datetime import UTC, datetime, timedelta",
                "import sys",
                "from mediaforce.core.process_control import (",
                "    ManagedProcessController,",
                "    ProcessDeadlineExpiredError,",
                "    run_command,",
                ")",
                "controller = ManagedProcessController()",
                "try:",
                "    with controller.absolute_deadline(datetime.now(UTC) + timedelta(seconds=1.5)):",
                "        run_command([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], process_controller=controller)",
                "except ProcessDeadlineExpiredError:",
                "    raise SystemExit(0)",
                "raise SystemExit(3)",
            ))
            harness = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    harness_script,
                    target_script,
                    str(started_path),
                    str(descendant_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                wait_deadline = time.monotonic() + 2
                while not started_path.exists() and time.monotonic() < wait_deadline:
                    time.sleep(0.02)
                self.assertTrue(started_path.exists())
                os.kill(harness.pid, signal.SIGSTOP)
                time.sleep(2.5)
                os.kill(harness.pid, signal.SIGCONT)
                stdout, stderr = harness.communicate(timeout=5)
            finally:
                if harness.poll() is None:
                    try:
                        os.kill(harness.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    harness.kill()
                    harness.communicate()

            self.assertEqual(harness.returncode, 0, f"stdout={stdout!r} stderr={stderr!r}")
            self.assertFalse(descendant_path.exists())

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_run_command_starts_new_session_for_managed_processes(self, popen_mock: Mock) -> None:
        process = Mock()
        process.communicate.return_value = ("stdout", "stderr")
        process.returncode = 0
        popen_mock.return_value = process

        result = run_command(["echo", "ok"], process_controller=ManagedProcessController())

        self.assertEqual(result.returncode, 0)
        popen_mock.assert_called_once()
        self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_run_command_terminates_and_reaps_on_base_exception(self, popen_mock: Mock) -> None:
        process = Mock()
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        popen_mock.return_value = process
        controller = Mock(spec=ManagedProcessController)
        controller.process_deadline_ns.return_value = None

        with self.assertRaises(KeyboardInterrupt):
            run_command(["echo", "ok"], process_controller=controller)

        controller.attach.assert_called_once_with(
            process,
            terminate_process_group=True,
        )
        controller.terminate.assert_called_once_with()
        self.assertEqual(process.communicate.call_count, 2)
        controller.clear.assert_called_once_with(process)

    @patch("mediaforce.core.process_control._terminate_process")
    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_run_command_terminates_when_controller_attach_is_interrupted(
            self,
            popen_mock: Mock,
            terminate_process_mock: Mock,
    ) -> None:
        process = Mock()
        process.communicate.return_value = ("", "")
        popen_mock.return_value = process
        controller = Mock(spec=ManagedProcessController)
        controller.attach.side_effect = KeyboardInterrupt()
        controller.process_deadline_ns.return_value = None

        with self.assertRaises(KeyboardInterrupt):
            run_command(["echo", "ok"], process_controller=controller)

        terminate_process_mock.assert_called_once_with(
            process,
            terminate_process_group=True,
        )
        process.communicate.assert_called_once_with()
        controller.clear.assert_called_once_with(process)

    @patch("mediaforce.core.process_control.time.monotonic", side_effect=[0.0, 0.0, 2.0])
    @patch("mediaforce.core.process_control.time.sleep", return_value=None)
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid", return_value=4321)
    def test_cancel_terminates_process_group(
            self,
            getpgid_mock: Mock,
            killpg_mock: Mock,
            _sleep_mock: Mock,
            _monotonic_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 1234
        process.poll.side_effect = [None, None, None]

        controller = ManagedProcessController()
        controller.attach(process, terminate_process_group=True)
        controller.cancel()

        getpgid_mock.assert_called_once_with(1234)
        self.assertEqual(
            killpg_mock.call_args_list,
            [
                ((4321, signal.SIGTERM),),
                ((4321, 0),),
                ((4321, 0),),
                ((4321, signal.SIGKILL),),
            ],
        )

    @patch("mediaforce.core.process_control.time.monotonic", side_effect=[0.0, 0.0, 2.0])
    @patch("mediaforce.core.process_control.time.sleep", return_value=None)
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid", side_effect=ProcessLookupError())
    def test_termination_kills_descendants_after_group_leader_exits(
            self,
            _getpgid_mock: Mock,
            killpg_mock: Mock,
            _sleep_mock: Mock,
            _monotonic_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 1234
        process.poll.return_value = 0

        _terminate_process(
            process,
            terminate_process_group=True,
        )

        self.assertEqual(
            killpg_mock.call_args_list,
            [
                ((1234, signal.SIGTERM),),
                ((1234, 0),),
                ((1234, 0),),
                ((1234, signal.SIGKILL),),
            ],
        )

    @patch("mediaforce.core.process_control.time.monotonic", side_effect=[0.0, 0.0, 2.0])
    @patch("mediaforce.core.process_control.time.sleep", return_value=None)
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid")
    def test_cancel_only_terminates_process_without_group_opt_in(
            self,
            getpgid_mock: Mock,
            killpg_mock: Mock,
            _sleep_mock: Mock,
            _monotonic_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 1234
        process.poll.side_effect = [None, None, None]

        controller = ManagedProcessController()
        controller.attach(process)
        controller.cancel()

        getpgid_mock.assert_not_called()
        killpg_mock.assert_not_called()
        self.assertEqual(process.terminate.call_count, 1)
        self.assertEqual(process.kill.call_count, 1)
