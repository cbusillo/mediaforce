import os
import signal
import subprocess
import threading
import time
from typing import Mapping


class ProcessCancelledError(RuntimeError):
    pass


class ScheduleWindowClosedError(ProcessCancelledError):
    pass


class ManagedProcessController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._cancel_error_type: type[ProcessCancelledError] = ProcessCancelledError
        self._cancel_message = "Operation was cancelled."
        self._terminate_process_group = False

    def attach(self, process: subprocess.Popen[str], *, terminate_process_group: bool = False) -> None:
        with self._lock:
            self._process = process
            self._terminate_process_group = terminate_process_group
            if self._cancel_requested:
                self._terminate_locked()

    def clear(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None
                self._terminate_process_group = False

    def cancel(self, error: ProcessCancelledError | None = None) -> None:
        with self._lock:
            if not self._cancel_requested and error is not None:
                self._cancel_error_type = type(error)
                self._cancel_message = str(error)
            self._cancel_requested = True
            self._terminate_locked()

    def terminate(self) -> None:
        with self._lock:
            self._terminate_locked()

    def reset(self) -> None:
        with self._lock:
            self._cancel_requested = False
            self._cancel_error_type = ProcessCancelledError
            self._cancel_message = "Operation was cancelled."
            self._process = None
            self._terminate_process_group = False

    def throw_if_cancelled(self) -> None:
        with self._lock:
            if not self._cancel_requested:
                return
            error_type = self._cancel_error_type
            message = self._cancel_message
        raise error_type(message)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    def _terminate_locked(self) -> None:
        if self._process is None:
            return
        _terminate_process(
            self._process,
            terminate_process_group=self._terminate_process_group,
        )


def _terminate_process(
        process: subprocess.Popen[str],
        *,
        terminate_process_group: bool,
) -> None:
    process_group_id: int | None = None
    if terminate_process_group:
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = process.pid
    elif process.poll() is not None:
        return

    def target_running() -> bool:
        if process_group_id is None:
            return process.poll() is None
        process.poll()
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
        return
    if not target_running():
        return
    end_time = time.monotonic() + 1.5
    while time.monotonic() < end_time:
        if not target_running():
            return
        time.sleep(0.05)
    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        return


def run_command(
        cmd: list[str],
        *,
        process_controller: ManagedProcessController | None = None,
        capture_output: bool = True,
        text: bool = True,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = False,
        input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if process_controller is None:
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=text,
            env=env,
            timeout=timeout,
            check=check,
            input=input_text,
        )

    process_controller.throw_if_cancelled()
    stdout_pipe = subprocess.PIPE if capture_output else None
    stderr_pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        text=text,
        env=env,
        start_new_session=True,
    )
    attached = False
    try:
        process_controller.attach(process, terminate_process_group=True)
        attached = True
        if timeout is None:
            stdout, stderr = process.communicate(input_text)
        else:
            stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException as exc:
        cleanup_errors: list[BaseException] = []
        try:
            if attached:
                process_controller.terminate()
            else:
                _terminate_process(
                    process,
                    terminate_process_group=True,
                )
        except BaseException as terminate_error:
            cleanup_errors.append(terminate_error)
        try:
            process.communicate()
        except BaseException as reap_error:
            cleanup_errors.append(reap_error)
        for error in cleanup_errors:
            exc.add_note(
                "Managed process cleanup also failed: "
                f"{type(error).__name__}: {error}"
            )
        raise
    finally:
        process_controller.clear(process)
    process_controller.throw_if_cancelled()
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=stdout,
            stderr=stderr,
        )
    return completed
