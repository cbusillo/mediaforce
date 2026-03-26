from __future__ import annotations

import subprocess
import threading
from typing import Any


class ProcessCancelledError(RuntimeError):
    pass


class ManagedProcessController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def attach(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            if self._cancel_requested:
                self._terminate_locked()

    def clear(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            self._terminate_locked()

    def reset(self) -> None:
        with self._lock:
            self._cancel_requested = False
            self._process = None

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
        try:
            self._process.terminate()
        except OSError:
            return


def run_command(
    cmd: list[str],
    *,
    process_controller: ManagedProcessController | None = None,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    if process_controller is None:
        return subprocess.run(cmd, check=False, capture_output=capture_output, text=text)

    process_controller.throw_if_cancelled()
    stdout_pipe = subprocess.PIPE if capture_output else None
    stderr_pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(cmd, stdout=stdout_pipe, stderr=stderr_pipe, text=text)
    process_controller.attach(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        process_controller.clear(process)
    if process_controller.cancelled:
        raise ProcessCancelledError("Operation was cancelled.")
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
