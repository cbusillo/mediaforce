import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from mediaforce import execution
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, \
    ProcessDeadlineEnforcementError
from mediaforce.encoding.runner import _ProgressCallbackFailure, _join_process_threads, run_encode_command, \
    run_streamed_remote_encode_command, run_tracked_process
from mediaforce.web.runtime import encode_runtime


_PIPE_FILL_BYTES = 2 * 1024 * 1024


def _progress_state(_state: dict[str, str], line: str, *, elapsed_seconds: float) -> dict[str, object] | None:
    _ = elapsed_seconds
    return {"line": line} if line.startswith("progress=") else None


class RunnerProgressFailureTests(unittest.TestCase):
    def test_bounded_reader_join_marks_cleanup_unproven_and_preserves_progress_error(self) -> None:
        callback_failure = _ProgressCallbackFailure()
        callback_failure.report(RuntimeError("progress persistence failed"))
        thread = Mock()
        thread.is_alive.return_value = True

        with self.assertRaises(ProcessDeadlineEnforcementError) as raised:
            _join_process_threads((thread,), callback_failure)

        thread.join.assert_called_once_with(timeout=1.0)
        self.assertTrue(callback_failure.cleanup_unproven)
        self.assertIn("progress persistence failed", " ".join(raised.exception.__notes__))

    def test_execution_wrapper_forwards_progress_failure_termination_policy(self) -> None:
        with patch.object(execution, "_run_tracked_process_impl") as implementation:
            execution._run_tracked_process(
                ["ssh", "fixture.invalid"],
                process_controller=None,
                progress_callback=None,
                terminate_on_progress_failure=False,
            )

        self.assertFalse(implementation.call_args.kwargs["terminate_on_progress_failure"])

    def test_remote_encode_command_disables_local_ssh_termination_for_progress_failure(self) -> None:
        tracked = Mock(return_value=subprocess.CompletedProcess(["ssh"], 0, "", ""))

        run_encode_command(
            ffmpeg_cmd=["ffmpeg", "-i", "/Volumes/media/source.mkv", "/tmp/output.mkv"],
            temp_output=Path("/Volumes/media/output.partial.mkv"),
            staging_path=Path("/Volumes/media/output.mkv"),
            overwrite=False,
            process_controller=None,
            host={"key": "fixture.invalid", "mode": "ssh", "media_access": "mounted"},
            progress_callback=Mock(),
            execution_mode_for_host=lambda _host: "ssh",
            host_media_access_for_host=lambda _host: "mounted",
            remote_shell_path_export_line=lambda: "export PATH=/usr/bin",
            ssh_client_options=lambda: [],
            ffmpeg_command_with_progress=lambda command: command,
            run_tracked_process_fn=tracked,
            run_streamed_remote_encode_command_fn=Mock(),
        )

        self.assertFalse(tracked.call_args.kwargs["terminate_on_progress_failure"])

    def test_local_tracked_process_terminates_after_callback_failure_without_hanging(self) -> None:
        child = (
            "import sys, time; "
            "sys.stderr.write('progress=continue\\n'); sys.stderr.flush(); "
            f"sys.stderr.write('x' * {_PIPE_FILL_BYTES}); sys.stderr.flush(); time.sleep(30)"
        )
        controller = ManagedProcessController()
        started = time.monotonic()

        with patch.object(controller, "terminate", wraps=controller.terminate) as terminate, \
                self.assertRaisesRegex(RuntimeError, "progress persistence failed"):
            run_tracked_process(
                [sys.executable, "-c", child],
                process_controller=controller,
                progress_callback=lambda _snapshot: (_ for _ in ()).throw(
                    RuntimeError("progress persistence failed")
                ),
                run_command=lambda _cmd: subprocess.CompletedProcess(_cmd, 0, "", ""),
                update_ffmpeg_progress_state=_progress_state,
                process_cancelled_error=ProcessCancelledError,
            )

        self.assertLess(time.monotonic() - started, 8)
        terminate.assert_called()
        self.assertIsNone(controller.pid)

    def test_local_progress_failure_without_controller_kills_sigterm_ignoring_child(self) -> None:
        child = (
            "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "sys.stderr.write('progress=continue\\n'); sys.stderr.flush(); "
            f"sys.stderr.write('x' * {_PIPE_FILL_BYTES}); sys.stderr.flush(); time.sleep(30)"
        )
        started = time.monotonic()

        with self.assertRaisesRegex(RuntimeError, "progress persistence failed"):
            run_tracked_process(
                [sys.executable, "-c", child],
                process_controller=None,
                progress_callback=lambda _snapshot: (_ for _ in ()).throw(
                    RuntimeError("progress persistence failed")
                ),
                run_command=lambda _cmd: subprocess.CompletedProcess(_cmd, 0, "", ""),
                update_ffmpeg_progress_state=_progress_state,
                process_cancelled_error=ProcessCancelledError,
            )

        self.assertLess(time.monotonic() - started, 8)

    def test_remote_tracked_process_drains_to_natural_exit_before_raising_callback_failure(self) -> None:
        child = (
            "import sys; sys.stderr.write('progress=continue\\n'); sys.stderr.flush(); "
            f"sys.stderr.write('x' * {_PIPE_FILL_BYTES}); sys.stderr.flush(); "
            "sys.stdout.write('complete'); sys.stdout.flush()"
        )
        controller = ManagedProcessController()

        with patch.object(controller, "terminate", wraps=controller.terminate) as terminate, \
                self.assertRaisesRegex(RuntimeError, "progress persistence failed"):
            run_tracked_process(
                [sys.executable, "-c", child],
                process_controller=controller,
                progress_callback=lambda _snapshot: (_ for _ in ()).throw(
                    RuntimeError("progress persistence failed")
                ),
                run_command=lambda _cmd: subprocess.CompletedProcess(_cmd, 0, "", ""),
                update_ffmpeg_progress_state=_progress_state,
                process_cancelled_error=ProcessCancelledError,
                terminate_on_progress_failure=False,
            )

        terminate.assert_not_called()
        self.assertFalse(controller.cancelled)
        self.assertIsNone(controller.pid)

    def test_remote_stream_drains_to_natural_exit_before_raising_callback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source.bin"
            output = root / "output.bin"
            source.write_bytes(b"source")
            child = (
                "import sys, time; sys.stdin.buffer.read(); "
                "sys.stderr.write('progress=continue\\n'); sys.stderr.flush(); "
                f"sys.stderr.write('x' * {_PIPE_FILL_BYTES}); sys.stderr.flush(); "
                "sys.stdout.buffer.write(b'complete'); sys.stdout.flush()"
            )
            controller = ManagedProcessController()
            started = time.monotonic()
            real_popen = subprocess.Popen

            with patch.object(controller, "terminate", wraps=controller.terminate) as terminate, patch(
                    "mediaforce.encoding.runner.subprocess.Popen",
                    side_effect=lambda *_args, **kwargs: real_popen(
                        [sys.executable, "-c", child], **kwargs
                    ),
            ), self.assertRaisesRegex(RuntimeError, "progress persistence failed"):
                run_streamed_remote_encode_command(
                    ffmpeg_cmd=[sys.executable, "-i", str(source), str(output)],
                    temp_output=output,
                    source_path=source,
                    process_controller=controller,
                    host={"key": "fixture.invalid", "mode": "ssh"},
                    progress_callback=lambda _snapshot: (_ for _ in ()).throw(
                        RuntimeError("progress persistence failed")
                    ),
                    ssh_client_options=lambda: [],
                    build_streaming_remote_ffmpeg_command=lambda *_args, **_kwargs: [sys.executable, "-c", child],
                    update_ffmpeg_progress_state=_progress_state,
                    process_cancelled_error=ProcessCancelledError,
                )

            self.assertLess(time.monotonic() - started, 8)
            terminate.assert_not_called()
            self.assertEqual(output.read_bytes(), b"complete")
            self.assertIsNone(controller.pid)

    def test_heartbeat_retries_transient_database_open_failure_then_updates_owner(self) -> None:
        stop_event = Mock()
        stop_event.wait.side_effect = [False, False, True]
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        deps = SimpleNamespace(
            encode_job_heartbeat_seconds=0.01,
            encode_job_lease_seconds=60,
            load_config=Mock(return_value=SimpleNamespace(paths=SimpleNamespace(db_path=Path("fixture.db")))),
            logger=Mock(),
            now_iso=Mock(side_effect=["heartbeat", "updated"]),
        )
        job = {"status": "running", "worker_id": "worker-1"}

        with patch.object(
                encode_runtime,
                "open_db",
                side_effect=[RuntimeError("identity changed"), context],
        ), patch.object(
            encode_runtime, "load_encode_job", return_value=job,
        ), patch.object(encode_runtime, "save_encode_job") as save:
            encode_runtime.encode_job_heartbeat_loop(
                config_path=Path("fixture.toml"),
                job_id="job-1",
                worker_id="worker-1",
                stop_event=stop_event,
                process_controller=ManagedProcessController(),
                deps=deps,
            )

        deps.logger.exception.assert_called_once()
        save.assert_called_once_with(connection, job)
        self.assertEqual(job["heartbeat_at"], "heartbeat")

    def test_heartbeat_wrong_worker_exits_without_writing(self) -> None:
        stop_event = Mock()
        stop_event.wait.return_value = False
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        deps = SimpleNamespace(
            encode_job_heartbeat_seconds=0.01,
            load_config=Mock(return_value=SimpleNamespace(paths=SimpleNamespace(db_path=Path("fixture.db")))),
            logger=Mock(),
        )

        with patch.object(encode_runtime, "open_db", return_value=context), patch.object(
                encode_runtime, "load_encode_job", return_value={"status": "running", "worker_id": "other"},
        ), patch.object(encode_runtime, "save_encode_job") as save:
            encode_runtime.encode_job_heartbeat_loop(
                config_path=Path("fixture.toml"),
                job_id="job-1",
                worker_id="worker-1",
                stop_event=stop_event,
                process_controller=ManagedProcessController(),
                deps=deps,
            )

        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
