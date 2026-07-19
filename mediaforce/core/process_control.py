import os
import signal
import subprocess
import threading
import time
from typing import Mapping


class ProcessCancelledError(RuntimeError):
    pass


class ManagedProcessController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
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

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            self._terminate_locked()

    def terminate(self) -> None:
        with self._lock:
            self._terminate_locked()

    def reset(self) -> None:
        with self._lock:
            self._cancel_requested = False
            self._process = None
            self._terminate_process_group = False

    def throw_if_cancelled(self) -> None:
        with self._lock:
            if self._cancel_requested:
                raise ProcessCancelledError("Operation was cancelled.")

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
        if self._process.poll() is not None:
            return
        process_group_id: int | None = None
        if self._terminate_process_group:
            try:
                process_group_id = os.getpgid(self._process.pid)
            except OSError:
                process_group_id = None
        try:
            if process_group_id is not None:
                os.killpg(process_group_id, signal.SIGTERM)
            else:
                self._process.terminate()
        except OSError:
            return
        if self._process.poll() is not None:
            return
        end_time = time.monotonic() + 1.5
        while time.monotonic() < end_time:
            if self._process.poll() is not None:
                return
            time.sleep(0.05)
        try:
            if process_group_id is not None:
                os.killpg(process_group_id, signal.SIGKILL)
            else:
                self._process.kill()
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
) -> subprocess.CompletedProcess[str]:
    if process_controller is None:
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=text,
            env=env,
            timeout=timeout,
            check=check,
        )

    process_controller.throw_if_cancelled()
    stdout_pipe = subprocess.PIPE if capture_output else None
    stderr_pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(
        cmd,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        text=text,
        env=env,
        start_new_session=True,
    )
    process_controller.attach(process, terminate_process_group=True)
    try:
        try:
            if timeout is None:
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process_controller.terminate()
            process.communicate()
            raise
    finally:
        process_controller.clear(process)
    if process_controller.cancelled:
        raise ProcessCancelledError("Operation was cancelled.")
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=stdout,
            stderr=stderr,
        )
    return completed
