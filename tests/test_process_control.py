import signal
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

from mediaforce.core.process_control import (
    ManagedProcessController,
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
