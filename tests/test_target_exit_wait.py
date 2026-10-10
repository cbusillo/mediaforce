from contextlib import contextmanager
from collections.abc import Iterator
import select
import signal
import subprocess
import sys
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from mediaforce.core import process_control


@contextmanager
def _owned_target() -> Iterator[subprocess.Popen[bytes]]:
    source = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "os.write(1, b'R')\n"
        "sys.stdin.buffer.read(1)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as target:
        assert target.stdin is not None
        assert target.stdout is not None
        try:
            assert select.select([target.stdout], [], [], 5)[0], "target did not become ready"
            assert target.stdout.read(1) == b"R"
            yield target
        finally:
            target.stdin.close()
            target.wait(timeout=5)


class TargetExitWaitTests(TestCase):
    def test_wait_observes_native_exit_during_controller_pause(self) -> None:
        with _owned_target() as target:
            assert target.stdin is not None
            assert target.stdout is not None
            clock = Mock(wraps=time)

            def pause_controller(_delay: float) -> None:
                target.stdin.close()
                self.assertTrue(select.select([target.stdout], [], [], 5)[0])
                self.assertEqual(target.stdout.read(), b"")
                self.assertEqual(target.wait(timeout=5), 0)
                time.sleep(0.1)

            clock.sleep.side_effect = pause_controller
            with patch.object(process_control, "time", clock):
                exited = process_control._wait_for_target_exit(
                    lambda: target.poll() is None,
                    timeout=0.05,
                )
            self.assertTrue(exited)

    def test_wait_keeps_a_native_survivor_failing_after_pause(self) -> None:
        with _owned_target() as target:
            clock = Mock(wraps=time)
            clock.sleep.side_effect = lambda _delay: time.sleep(0.1)
            with patch.object(process_control, "time", clock):
                exited = process_control._wait_for_target_exit(
                    lambda: target.poll() is None,
                    timeout=0.05,
                )
            self.assertFalse(exited)
            self.assertIsNone(target.poll())
            assert target.stdout is not None
            self.assertFalse(select.select([target.stdout], [], [], 0)[0])

    def test_termination_observes_native_exit_after_each_phase_pause(self) -> None:
        for exit_phase in ("term", "kill"):
            with self.subTest(exit_phase=exit_phase), _owned_target() as target:
                assert target.stdin is not None
                assert target.stdout is not None
                clock = Mock(wraps=time)
                phase = 0
                clock_reads = 0

                def paused_monotonic() -> float:
                    nonlocal phase, clock_reads
                    clock_reads += 1
                    if clock_reads % 2 == 0:
                        phase += 1
                        if exit_phase == "term" or phase == 2:
                            if exit_phase == "term":
                                target.stdin.close()
                            self.assertTrue(select.select([target.stdout], [], [], 5)[0])
                            self.assertEqual(target.stdout.read(), b"")
                            expected_status = 0 if exit_phase == "term" else -signal.SIGKILL
                            self.assertEqual(target.wait(timeout=5), expected_status)
                        else:
                            self.assertIsNone(target.poll())
                        time.sleep(1.6)
                    return time.monotonic()

                clock.monotonic.side_effect = paused_monotonic
                with (
                    patch.object(process_control, "time", clock),
                    patch.object(target, "kill", wraps=target.kill) as kill,
                ):
                    process_control._terminate_process(target, terminate_process_group=False)
                self.assertEqual(kill.call_count, int(exit_phase == "kill"))

    def test_termination_keeps_surviving_target_failure_bounded(self) -> None:
        target = Mock()
        target.poll.return_value = None
        clock = Mock()
        clock.monotonic.side_effect = [0.0, 2.0, 3.0, 5.0]
        with (
            patch.object(process_control, "time", clock),
            self.assertRaisesRegex(RuntimeError, "remained live after SIGKILL"),
        ):
            process_control._terminate_process(target, terminate_process_group=False)
        target.terminate.assert_called_once_with()
        target.kill.assert_called_once_with()
        clock.sleep.assert_not_called()
