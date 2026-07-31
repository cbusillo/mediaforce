import errno
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class ProcessCancelledError(RuntimeError):
    pass


class ScheduleWindowClosedError(ProcessCancelledError):
    pass


class ProcessDeadlineExpiredError(ScheduleWindowClosedError):
    pass


class ProcessDeadlineEnforcementError(RuntimeError):
    pass


class _ContainmentReportedUnavailableError(ProcessDeadlineEnforcementError):
    pass


_PROCESS_COMMUNICATION_POLL_SECONDS = 0.05
_PROCESS_REAP_TIMEOUT_SECONDS = 2.0
_PROCESS_STATUS_CLEANUP_TIMEOUT_SECONDS = 4.0


class ManagedProcessController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._cancel_error_type: type[ProcessCancelledError] = ProcessCancelledError
        self._cancel_message = "Operation was cancelled."
        self._terminate_process_group = False
        self._process_group_id: int | None = None
        self._cleanup_unproven = False
        self._activity_guard: Callable[[], None] | None = None
        self._process_deadline_ns: int | None = None

    def attach(self, process: subprocess.Popen[str], *, terminate_process_group: bool = False) -> None:
        with self._lock:
            if self._cleanup_unproven:
                raise ProcessDeadlineEnforcementError(
                    "Managed process controller cannot be reused because prior "
                    "containment cleanup is unproven."
                )
            self._process = process
            self._terminate_process_group = terminate_process_group
            self._process_group_id = process.pid if terminate_process_group else None
            if self._cancel_requested:
                self._terminate_locked()

    def clear(
            self,
            process: subprocess.Popen[str],
            *,
            retain_process_group: bool = False,
    ) -> None:
        with self._lock:
            if self._process is not None and self._process is not process:
                return
            if self._process is process:
                self._process = None
            if retain_process_group:
                self._terminate_process_group = True
                self._process_group_id = process.pid
                self._cleanup_unproven = True
            elif not self._cleanup_unproven:
                self._terminate_process_group = False
                self._process_group_id = None

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
            if self._cleanup_unproven:
                raise ProcessDeadlineEnforcementError(
                    "Managed process controller cannot be reset because prior "
                    "containment cleanup is unproven."
                )
            self._cancel_requested = False
            self._cancel_error_type = ProcessCancelledError
            self._cancel_message = "Operation was cancelled."
            self._process = None
            self._terminate_process_group = False
            self._process_group_id = None
            self._activity_guard = None
            self._process_deadline_ns = None

    @contextmanager
    def absolute_deadline(self, deadline: datetime) -> Iterator[None]:
        if deadline.tzinfo is None:
            raise ValueError("Process deadline must include a UTC offset")
        requested_deadline_ns = int(deadline.timestamp() * 1_000_000_000)
        with self._lock:
            previous_deadline_ns = self._process_deadline_ns
            active_deadline_ns = (
                requested_deadline_ns
                if previous_deadline_ns is None
                else min(previous_deadline_ns, requested_deadline_ns)
            )
            self._process_deadline_ns = active_deadline_ns
        try:
            yield
        finally:
            with self._lock:
                if self._process_deadline_ns == active_deadline_ns:
                    self._process_deadline_ns = previous_deadline_ns

    @contextmanager
    def activity_guard(
            self,
            guard: Callable[[], None],
    ) -> Iterator[None]:
        with self._lock:
            previous_guard = self._activity_guard

            if previous_guard is None:
                active_guard = guard
            else:
                def active_guard() -> None:
                    previous_guard()
                    guard()

            self._activity_guard = active_guard
        try:
            yield
        finally:
            with self._lock:
                if self._activity_guard is active_guard:
                    self._activity_guard = previous_guard

    def throw_if_cancelled(self) -> None:
        with self._lock:
            cleanup_unproven = self._cleanup_unproven
            activity_guard = self._activity_guard
            cancelled = self._cancel_requested
            error_type = self._cancel_error_type
            message = self._cancel_message
            process_deadline_ns = self._process_deadline_ns
        if cleanup_unproven:
            raise ProcessDeadlineEnforcementError(
                "Managed process controller cannot be reused because prior "
                "containment cleanup is unproven."
            )
        if cancelled:
            raise error_type(message)
        if activity_guard is not None:
            activity_guard()
        if process_deadline_ns is not None and time.time_ns() >= process_deadline_ns:
            raise ProcessDeadlineExpiredError("Process authorization deadline expired.")

    def process_deadline_ns(self) -> int | None:
        with self._lock:
            return self._process_deadline_ns

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def cleanup_unproven(self) -> bool:
        with self._lock:
            return self._cleanup_unproven

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    def _terminate_locked(self) -> None:
        if self._process is None and self._process_group_id is None:
            return
        _terminate_process(
            self._process,
            terminate_process_group=self._terminate_process_group,
            process_group_id=self._process_group_id,
        )


def _terminate_process(
        process: subprocess.Popen[str] | None,
        *,
        terminate_process_group: bool,
        process_group_id: int | None = None,
) -> None:
    if terminate_process_group:
        if process_group_id is None:
            if process is None:
                return
            try:
                process_group_id = os.getpgid(process.pid)
            except ProcessLookupError:
                process_group_id = process.pid
    elif process is None or process.poll() is not None:
        return

    def target_running() -> bool:
        if process_group_id is None:
            if process is None:
                return False
            return process.poll() is None
        if process is not None:
            process.poll()
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            if exc.errno == errno.EPERM:
                return True
            raise
        return True

    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGTERM)
        elif process is not None:
            process.terminate()
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise
    if not target_running():
        return
    if _wait_for_target_exit(target_running, timeout=1.5):
        return
    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGKILL)
        elif process is not None:
            process.kill()
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise
    if _wait_for_target_exit(target_running, timeout=1.5):
        return
    target_label = (
        f"process group {process_group_id}"
        if process_group_id is not None
        else "managed process"
    )
    raise RuntimeError(f"{target_label} remained live after SIGKILL")


def _wait_for_target_exit(
        target_running: Callable[[], bool],
        *,
        timeout: float,
) -> bool:
    end_time = time.monotonic() + timeout
    while time.monotonic() < end_time:
        if not target_running():
            return True
        time.sleep(0.05)
    return False


def _add_managed_process_cleanup_note(
        error: BaseException,
        cleanup_error: BaseException,
) -> None:
    error.add_note(
        "Managed process cleanup also failed: "
        f"{type(cleanup_error).__name__}: {cleanup_error}"
    )


def _read_deadline_status(status_descriptor: int) -> bytes:
    try:
        chunks: list[bytes] = []
        while chunk := os.read(status_descriptor, 16):
            chunks.append(chunk)
    except OSError as exc:
        raise ProcessDeadlineEnforcementError(
            "Managed process containment status is unavailable."
        ) from exc
    status = b"".join(chunks)
    if status not in {b"C", b"E", b"U"}:
        raise ProcessDeadlineEnforcementError(
            "Managed process containment failed closed."
        )
    return status


class _ContainmentStatusMonitor:
    def __init__(
            self,
            *,
            status_descriptor: int,
            process_controller: ManagedProcessController,
            process: subprocess.Popen[str],
    ) -> None:
        self._status_descriptor = status_descriptor
        self._process_controller = process_controller
        self._process = process
        self._failure_detected = threading.Event()
        self._completed = threading.Event()
        self._status: bytes | None = None
        self._error: BaseException | None = None
        self._cleanup_errors: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._run,
            name="mediaforce-process-deadline-status",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def failure_detected(self) -> bool:
        return self._failure_detected.is_set()

    def error(self) -> BaseException | None:
        return self._error

    def wait(self, timeout: float | None = None) -> bool:
        return self._completed.wait(timeout)

    def status(self) -> bytes:
        if not self._completed.is_set() or self._status is None:
            raise ProcessDeadlineEnforcementError(
                "Managed process containment status is unavailable."
            )
        return self._status

    def add_cleanup_notes(self, error: BaseException) -> None:
        for cleanup_error in self._cleanup_errors:
            _add_managed_process_cleanup_note(error, cleanup_error)

    def _run(self) -> None:
        try:
            self._status = _read_deadline_status(self._status_descriptor)
            if self._status == b"U":
                self._error = _ContainmentReportedUnavailableError(
                    "Managed process containment cleanup is unproven."
                )
                self._failure_detected.set()
                self._terminate_target()
        except BaseException as exc:
            self._error = exc
            self._failure_detected.set()
            self._terminate_target()
        finally:
            try:
                os.close(self._status_descriptor)
            except BaseException as cleanup_error:
                if self._error is None:
                    self._error = ProcessDeadlineEnforcementError(
                        "Managed process containment status is unavailable."
                    )
                    self._cleanup_errors.append(cleanup_error)
                    self._status = None
                    self._failure_detected.set()
                    self._terminate_target()
                else:
                    self._cleanup_errors.append(cleanup_error)
            self._completed.set()

    def _terminate_target(self) -> None:
        try:
            self._process_controller.terminate()
        except BaseException as cleanup_error:
            self._cleanup_errors.append(cleanup_error)
        try:
            _terminate_process(
                self._process,
                terminate_process_group=True,
                process_group_id=self._process.pid,
            )
        except BaseException as fallback_error:
            self._cleanup_errors.append(fallback_error)


def _communicate_with_containment_monitor(
        process: subprocess.Popen[str],
        *,
        cmd: list[str],
        input_text: str | None,
        timeout: float | None,
        monitor: _ContainmentStatusMonitor,
) -> tuple[str, str]:
    started_at = time.monotonic()
    pending_input = input_text
    while True:
        monitor_error = monitor.error() if monitor.failure_detected() else None
        if monitor_error is not None:
            raise monitor_error
        poll_timeout = _PROCESS_COMMUNICATION_POLL_SECONDS
        if timeout is not None:
            remaining = timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            poll_timeout = min(poll_timeout, remaining)
        try:
            return process.communicate(
                pending_input,
                timeout=poll_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            pending_input = None
            monitor_error = monitor.error() if monitor.failure_detected() else None
            if monitor_error is not None:
                raise monitor_error
            if timeout is not None and time.monotonic() - started_at >= timeout:
                raise subprocess.TimeoutExpired(
                    cmd,
                    timeout,
                    output=exc.output,
                    stderr=exc.stderr,
                ) from exc


def _reap_terminated_process(
        process: subprocess.Popen[str],
        error: BaseException,
) -> bool:
    try:
        process.communicate(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except BaseException as cleanup_error:
        _add_managed_process_cleanup_note(error, cleanup_error)
        return process.poll() is not None
    return True


def _terminate_and_reap_managed_process(
        *,
        process_controller: ManagedProcessController,
        process: subprocess.Popen[str],
        monitor: _ContainmentStatusMonitor,
        attached: bool,
        error: BaseException,
) -> bool:
    termination_succeeded = False
    try:
        if attached:
            process_controller.terminate()
        else:
            _terminate_process(
                process,
                terminate_process_group=True,
                process_group_id=process.pid,
            )
        termination_succeeded = True
    except BaseException as cleanup_error:
        _add_managed_process_cleanup_note(error, cleanup_error)
        if attached:
            try:
                _terminate_process(
                    process,
                    terminate_process_group=True,
                    process_group_id=process.pid,
                )
                termination_succeeded = True
            except BaseException as fallback_error:
                _add_managed_process_cleanup_note(error, fallback_error)
    if not termination_succeeded:
        return False
    if not _reap_terminated_process(process, error):
        return False
    return _containment_status_proves_cleanup(
        monitor=monitor,
        error=error,
    )


def _containment_status_proves_cleanup(
        *,
        monitor: _ContainmentStatusMonitor,
        error: BaseException,
) -> bool:
    if not monitor.wait(_PROCESS_STATUS_CLEANUP_TIMEOUT_SECONDS):
        _add_managed_process_cleanup_note(
            error,
            TimeoutError("containment status cleanup did not finish"),
        )
        return False
    monitor.add_cleanup_notes(error)
    status_error = monitor.error()
    if status_error is not None:
        if (
            status_error is not error
            and not isinstance(status_error, _ContainmentReportedUnavailableError)
        ):
            _add_managed_process_cleanup_note(error, status_error)
        return False
    try:
        return monitor.status() in {b"C", b"E"}
    except BaseException as status_exception:
        _add_managed_process_cleanup_note(error, status_exception)
        return False


def _finish_containment_status_failure(
        *,
        monitor: _ContainmentStatusMonitor,
        process: subprocess.Popen[str],
        error: BaseException,
) -> bool:
    if not monitor.wait(_PROCESS_STATUS_CLEANUP_TIMEOUT_SECONDS):
        _add_managed_process_cleanup_note(
            error,
            TimeoutError("containment status cleanup did not finish"),
        )
    else:
        monitor.add_cleanup_notes(error)
    _reap_terminated_process(process, error)
    return False


def run_command(
        cmd: list[str],
        *,
        process_controller: ManagedProcessController | None = None,
        capture_output: bool = True,
        text: bool = True,
        cwd: str | os.PathLike[str] | None = None,
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
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            input=input_text,
        )

    process_controller.throw_if_cancelled()
    process_deadline_ns = process_controller.process_deadline_ns()
    stdout_pipe = subprocess.PIPE if capture_output else None
    stderr_pipe = subprocess.PIPE if capture_output else None
    if not hasattr(os, "fork"):
        raise ProcessDeadlineEnforcementError(
            "Managed process containment is unavailable."
        )
    deadline_status_descriptor, deadline_status_write_descriptor = os.pipe()
    try:
        parent_liveness_descriptor, parent_liveness_write_descriptor = os.pipe()
    except BaseException:
        os.close(deadline_status_descriptor)
        os.close(deadline_status_write_descriptor)
        raise
    helper_path = Path(__file__).with_name("_process_deadline.py")
    process_cmd = [
        sys.executable,
        "-I",
        str(helper_path),
        str(process_deadline_ns if process_deadline_ns is not None else -1),
        str(deadline_status_write_descriptor),
        str(parent_liveness_descriptor),
        "--",
        *cmd,
    ]
    try:
        try:
            process = subprocess.Popen(
                process_cmd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=stdout_pipe,
                stderr=stderr_pipe,
                text=text,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
                pass_fds=(
                    deadline_status_write_descriptor,
                    parent_liveness_descriptor,
                ),
            )
        except BaseException:
            os.close(deadline_status_descriptor)
            os.close(deadline_status_write_descriptor)
            os.close(parent_liveness_descriptor)
            raise
        os.close(deadline_status_write_descriptor)
        os.close(parent_liveness_descriptor)
        attached = False
        containment_finalized = False
        status_monitor = _ContainmentStatusMonitor(
            status_descriptor=deadline_status_descriptor,
            process_controller=process_controller,
            process=process,
        )
        status_monitor.start()
        try:
            process_controller.attach(process, terminate_process_group=True)
            attached = True
            stdout, stderr = _communicate_with_containment_monitor(
                process,
                cmd=cmd,
                input_text=input_text,
                timeout=timeout,
                monitor=status_monitor,
            )
            if not status_monitor.wait(_PROCESS_STATUS_CLEANUP_TIMEOUT_SECONDS):
                raise ProcessDeadlineEnforcementError(
                    "Managed process containment status is unavailable."
                )
            status_error = status_monitor.error()
            if status_error is not None:
                raise status_error
            deadline_status = status_monitor.status()
            containment_finalized = True
            if deadline_status == b"E":
                raise ProcessDeadlineExpiredError(
                    "Process authorization deadline expired."
                )
            process_controller.throw_if_cancelled()
            completed = subprocess.CompletedProcess(
                cmd,
                process.returncode,
                stdout,
                stderr,
            )
            if check and completed.returncode:
                raise subprocess.CalledProcessError(
                    completed.returncode,
                    cmd,
                    output=stdout,
                    stderr=stderr,
                )
        except BaseException as exc:
            cleanup_succeeded = containment_finalized
            if not containment_finalized:
                if status_monitor.error() is exc:
                    cleanup_succeeded = _finish_containment_status_failure(
                        monitor=status_monitor,
                        process=process,
                        error=exc,
                    )
                else:
                    cleanup_succeeded = _terminate_and_reap_managed_process(
                        process_controller=process_controller,
                        process=process,
                        monitor=status_monitor,
                        attached=attached,
                        error=exc,
                    )
                if not cleanup_succeeded:
                    _add_managed_process_cleanup_note(
                        exc,
                        RuntimeError(
                            "managed process containment cleanup did not complete"
                        ),
                    )
            try:
                process_controller.clear(
                    process,
                    retain_process_group=not cleanup_succeeded,
                )
            except BaseException as cleanup_error:
                _add_managed_process_cleanup_note(exc, cleanup_error)
            raise
        process_controller.clear(process, retain_process_group=False)
        return completed
    finally:
        os.close(parent_liveness_write_descriptor)
