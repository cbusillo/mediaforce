from datetime import UTC, datetime, timedelta
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from mediaforce.core import _process_deadline as process_deadline_module
from mediaforce.core import process_control as process_control_module
from mediaforce.core.process_control import (
    ManagedProcessController,
    ProcessCancelledError,
    ProcessDeadlineEnforcementError,
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

    def test_deadline_managed_command_retries_communicate_with_stdin_text(self) -> None:
        controller = ManagedProcessController()

        with controller.absolute_deadline(
            datetime.now(UTC) + timedelta(seconds=2)
        ):
            result = run_command(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; time.sleep(0.1); sys.stdout.write(sys.stdin.read())",
                ],
                process_controller=controller,
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

    def test_managed_command_preserves_signal_return_code(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ],
            process_controller=ManagedProcessController(),
        )

        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_managed_command_waits_for_detached_descendant_after_leader_exits(
            self,
    ) -> None:
        controller = ManagedProcessController()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leader_exited_path = root / "leader-exited"
            descendant_finished_path = root / "descendant-finished"
            target_script = "\n".join((
                "from pathlib import Path",
                "import subprocess",
                "import sys",
                "subprocess.Popen([",
                "    sys.executable, '-c',",
                "    \"from pathlib import Path; import sys, time; time.sleep(0.3); Path(sys.argv[1]).write_text('finished')\",",
                "    sys.argv[1],",
                "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)",
                "Path(sys.argv[2]).write_text('exited')",
            ))

            started_at = time.monotonic()
            result = run_command(
                [
                    sys.executable,
                    "-c",
                    target_script,
                    str(descendant_finished_path),
                    str(leader_exited_path),
                ],
                process_controller=controller,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(leader_exited_path.exists())
            self.assertTrue(descendant_finished_path.exists())
            self.assertGreaterEqual(time.monotonic() - started_at, 0.25)

    def test_deadline_kills_detached_descendant_after_leader_exits(self) -> None:
        controller = ManagedProcessController()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leader_exited_path = root / "leader-exited"
            descendant_started_path = root / "descendant-started"
            descendant_finished_path = root / "descendant-finished"
            descendant_script = "\n".join((
                "from pathlib import Path",
                "import sys",
                "import time",
                "Path(sys.argv[1]).write_text('started')",
                "time.sleep(1.2)",
                "Path(sys.argv[2]).write_text('finished')",
            ))
            target_script = "\n".join((
                "from pathlib import Path",
                "import subprocess",
                "import sys",
                "subprocess.Popen([",
                "    sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3],",
                "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)",
                "Path(sys.argv[4]).write_text('exited')",
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
                        descendant_script,
                        str(descendant_started_path),
                        str(descendant_finished_path),
                        str(leader_exited_path),
                    ],
                    process_controller=controller,
                )

            self.assertTrue(leader_exited_path.exists())
            self.assertTrue(descendant_started_path.exists())
            time.sleep(1.0)
            self.assertFalse(descendant_finished_path.exists())

    def test_cancellation_kills_detached_descendant_after_leader_exits(self) -> None:
        controller = ManagedProcessController()
        unrelated_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                leader_exited_path = root / "leader-exited"
                started_path = root / "descendant-started"
                finished_path = root / "descendant-finished"
                descendant_script = "\n".join((
                    "from pathlib import Path",
                    "import sys",
                    "import time",
                    "Path(sys.argv[1]).write_text('started')",
                    "time.sleep(1.2)",
                    "Path(sys.argv[2]).write_text('finished')",
                ))
                target_script = "\n".join((
                    "from pathlib import Path",
                    "import subprocess",
                    "import sys",
                    "subprocess.Popen([",
                    "    sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3],",
                    "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)",
                    "Path(sys.argv[4]).write_text('exited')",
                ))
                errors: list[BaseException] = []

                def run_target() -> None:
                    try:
                        run_command(
                            [
                                sys.executable,
                                "-c",
                                target_script,
                                descendant_script,
                                str(started_path),
                                str(finished_path),
                                str(leader_exited_path),
                            ],
                            process_controller=controller,
                        )
                    except BaseException as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run_target)
                worker.start()
                wait_deadline = time.monotonic() + 3
                while (
                    not started_path.exists() or not leader_exited_path.exists()
                ) and time.monotonic() < wait_deadline:
                    time.sleep(0.02)
                self.assertTrue(started_path.exists())
                self.assertTrue(leader_exited_path.exists())
                self.assertIsNotNone(controller.pid)
                self.assertTrue(worker.is_alive())

                controller.cancel()
                worker.join(timeout=5)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ProcessCancelledError)
                self.assertIsNone(unrelated_process.poll())
                time.sleep(1.3)
                self.assertFalse(finished_path.exists())
        finally:
            if unrelated_process.poll() is None:
                unrelated_process.terminate()
            unrelated_process.wait(timeout=5)

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_deadline_status_unexpected_eof_fails_closed(
            self,
            popen_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 987654
        process.returncode = 0
        process.communicate.return_value = ("", "")
        popen_mock.return_value = process
        controller = ManagedProcessController()

        with (
            controller.absolute_deadline(
                datetime.now(UTC) + timedelta(seconds=2)
            ),
            patch.object(
                controller,
                "terminate",
                wraps=controller.terminate,
            ) as terminate,
            self.assertRaisesRegex(
                ProcessDeadlineEnforcementError,
                "failed closed",
            ),
        ):
            run_command(["echo", "ok"], process_controller=controller)
        terminate.assert_called_once_with()

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_deadline_status_unavailable_retains_group_until_cleanup(
            self,
            popen_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 987654
        process.returncode = 0
        process.communicate.return_value = ("", "")
        popen_mock.return_value = process
        controller = Mock(spec=ManagedProcessController)
        controller.process_deadline_ns.return_value = time.time_ns() + 2_000_000_000
        events: list[str] = []
        controller.clear.side_effect = lambda *_args, **_kwargs: events.append("retain")
        controller.terminate.side_effect = lambda: events.append("terminate")
        controller.release_process_group.side_effect = (
            lambda _process_group: events.append("release")
        )

        with (
            patch.object(process_control_module.os, "pipe", return_value=(101, 102)),
            patch.object(
                process_control_module.os,
                "read",
                side_effect=(b"U", b""),
            ),
            patch.object(
                process_control_module.os,
                "close",
                side_effect=lambda descriptor: events.append(f"close:{descriptor}"),
            ),
            self.assertRaisesRegex(
                ProcessDeadlineEnforcementError,
                "failed closed",
            ),
        ):
            run_command(["echo", "ok"], process_controller=controller)

        self.assertEqual(events[0], "close:102")
        self.assertIn("retain", events)
        self.assertIn("terminate", events)
        self.assertIn("close:101", events)

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_deadline_status_read_base_exceptions_cleanup_before_reraise(
            self,
            popen_mock: Mock,
    ) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit(17)):
            with self.subTest(error_type=type(interruption).__name__):
                process = Mock()
                process.pid = 987654
                process.returncode = 0
                process.communicate.return_value = ("", "")
                popen_mock.return_value = process
                controller = Mock(spec=ManagedProcessController)
                controller.process_deadline_ns.return_value = (
                    time.time_ns() + 2_000_000_000
                )
                events: list[str] = []
                controller.clear.side_effect = (
                    lambda *_args, **_kwargs: events.append("retain")
                )
                controller.terminate.side_effect = lambda: events.append("terminate")
                controller.release_process_group.side_effect = (
                    lambda _process_group: events.append("release")
                )

                with (
                    patch.object(
                        process_control_module.os,
                        "pipe",
                        return_value=(101, 102),
                    ),
                    patch.object(
                        process_control_module.os,
                        "read",
                        side_effect=interruption,
                    ),
                    patch.object(
                        process_control_module.os,
                        "close",
                        side_effect=(
                            lambda descriptor: events.append(f"close:{descriptor}")
                        ),
                    ),
                    self.assertRaises(type(interruption)) as raised,
                ):
                    run_command(["echo", "ok"], process_controller=controller)

                self.assertIs(raised.exception, interruption)
                self.assertEqual(events[0], "close:102")
                self.assertIn("retain", events)
                self.assertIn("terminate", events)
                self.assertIn("close:101", events)

    @patch(
        "mediaforce.core.process_control._terminate_process",
        side_effect=RuntimeError("fallback cleanup failed"),
    )
    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_deadline_status_read_preserves_cleanup_failures_as_notes(
            self,
            popen_mock: Mock,
            _terminate_process_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 987654
        process.returncode = 0
        process.communicate.return_value = ("", "")
        popen_mock.return_value = process
        interruption = KeyboardInterrupt()
        controller = Mock(spec=ManagedProcessController)
        controller.process_deadline_ns.return_value = time.time_ns() + 2_000_000_000
        controller.terminate.side_effect = RuntimeError("controller cleanup failed")

        with (
            patch.object(process_control_module.os, "pipe", return_value=(101, 102)),
            patch.object(
                process_control_module.os,
                "read",
                side_effect=interruption,
            ),
            patch.object(process_control_module.os, "close"),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            run_command(["echo", "ok"], process_controller=controller)

        self.assertIs(raised.exception, interruption)
        self.assertIn(
            "RuntimeError: controller cleanup failed",
            "\n".join(raised.exception.__notes__),
        )
        self.assertIn(
            "RuntimeError: fallback cleanup failed",
            "\n".join(raised.exception.__notes__),
        )

    def test_terminate_process_propagates_non_esrch_signal_failures(self) -> None:
        process = Mock()
        process.pid = 1234

        with (
            patch.object(
                process_control_module.os,
                "killpg",
                side_effect=PermissionError(errno.EPERM, "SIGTERM denied"),
            ),
            self.assertRaisesRegex(PermissionError, "SIGTERM denied"),
        ):
            _terminate_process(
                process,
                terminate_process_group=True,
                process_group_id=process.pid,
            )

    def test_terminate_process_fails_if_group_remains_live_after_sigkill(self) -> None:
        process = Mock()
        process.pid = 1234

        with (
            patch.object(process_control_module.os, "killpg"),
            patch.object(
                process_control_module,
                "_wait_for_target_exit",
                side_effect=[False, False],
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "remained live after SIGKILL",
            ),
        ):
            _terminate_process(
                process,
                terminate_process_group=True,
                process_group_id=process.pid,
            )

        def fail_sigkill(_process_group: int, signal_number: int) -> None:
            if signal_number == signal.SIGKILL:
                raise PermissionError(errno.EPERM, "SIGKILL denied")

        with (
            patch.object(
                process_control_module.os,
                "killpg",
                side_effect=fail_sigkill,
            ),
            patch.object(
                process_control_module,
                "_wait_for_target_exit",
                return_value=False,
            ),
            self.assertRaisesRegex(PermissionError, "SIGKILL denied"),
        ):
            _terminate_process(
                process,
                terminate_process_group=True,
                process_group_id=process.pid,
            )

    def test_supervisor_fails_closed_before_launch_when_containment_unavailable(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "target-ran"
            status_read, status_write = os.pipe()
            helper_pid = os.fork()
            if helper_pid == 0:
                os.close(status_read)
                with patch.object(
                    process_deadline_module,
                    "_process_tree",
                    side_effect=RuntimeError("containment unavailable"),
                ):
                    exit_code = process_deadline_module._run(
                        -1,
                        status_write,
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')",
                            str(marker_path),
                        ],
                    )
                os.close(status_write)
                os._exit(exit_code)

            os.close(status_write)
            _, wait_status = os.waitpid(helper_pid, 0)
            status = os.read(status_read, 16)
            os.close(status_read)

            self.assertEqual(os.waitstatus_to_exitcode(wait_status), 125)
            self.assertEqual(status, b"U")
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

    def test_deadline_supervisor_kills_detached_descendant_while_parent_is_stopped(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started_path = root / "target-started"
            descendant_path = root / "descendant-ran"
            target_script = "\n".join((
                "from pathlib import Path",
                "import subprocess",
                "import sys",
                "Path(sys.argv[1]).write_text('started')",
                "subprocess.Popen([",
                "    sys.executable, '-c',",
                "    \"from pathlib import Path; import sys, time; time.sleep(2.2); Path(sys.argv[1]).write_text('ran')\",",
                "    sys.argv[2],",
                "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)",
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

        with patch.object(
            process_control_module,
            "_read_deadline_status",
            return_value=b"C",
        ):
            result = run_command(
                ["echo", "ok"],
                process_controller=ManagedProcessController(),
            )

        self.assertEqual(result.returncode, 0)
        popen_mock.assert_called_once()
        self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])
        self.assertEqual(
            popen_mock.call_args.args[0][-3:],
            ["--", "echo", "ok"],
        )
        self.assertEqual(len(popen_mock.call_args.kwargs["pass_fds"]), 1)

    @patch("mediaforce.core.process_control.subprocess.Popen")
    def test_run_command_terminates_and_reaps_on_base_exception(self, popen_mock: Mock) -> None:
        process = Mock()
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        popen_mock.return_value = process
        controller = Mock(spec=ManagedProcessController)
        controller.process_deadline_ns.return_value = None

        with (
            patch.object(
                process_control_module,
                "_read_deadline_status",
                return_value=b"C",
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_command(["echo", "ok"], process_controller=controller)

        controller.attach.assert_called_once_with(process)
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

        with (
            patch.object(
                process_control_module,
                "_read_deadline_status",
                return_value=b"C",
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_command(["echo", "ok"], process_controller=controller)

        terminate_process_mock.assert_called_once_with(
            process,
            terminate_process_group=False,
        )
        process.communicate.assert_called_once_with(timeout=2.0)
        controller.clear.assert_called_once_with(process)

    @patch(
        "mediaforce.core.process_control._wait_for_target_exit",
        side_effect=[False, True],
    )
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid")
    def test_cancel_terminates_process_group(
            self,
            getpgid_mock: Mock,
            killpg_mock: Mock,
            _wait_for_exit_mock: Mock,
    ) -> None:
        process = Mock()
        process.pid = 1234
        process.poll.side_effect = [None, None, None]

        controller = ManagedProcessController()
        controller.attach(process, terminate_process_group=True)
        controller.cancel()

        getpgid_mock.assert_not_called()
        self.assertEqual(
            killpg_mock.call_args_list,
            [
                ((1234, signal.SIGTERM),),
                ((1234, 0),),
                ((1234, signal.SIGKILL),),
            ],
        )

    @patch(
        "mediaforce.core.process_control._wait_for_target_exit",
        side_effect=[False, True],
    )
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid", side_effect=ProcessLookupError())
    def test_termination_kills_descendants_after_group_leader_exits(
            self,
            _getpgid_mock: Mock,
            killpg_mock: Mock,
            _wait_for_exit_mock: Mock,
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
                ((1234, signal.SIGKILL),),
            ],
        )

    @patch(
        "mediaforce.core.process_control._wait_for_target_exit",
        side_effect=[False, True],
    )
    @patch("mediaforce.core.process_control.os.killpg")
    @patch("mediaforce.core.process_control.os.getpgid")
    def test_cancel_only_terminates_process_without_group_opt_in(
            self,
            getpgid_mock: Mock,
            killpg_mock: Mock,
            _wait_for_exit_mock: Mock,
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
