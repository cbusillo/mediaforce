from __future__ import annotations

import math
import os
import select
import signal
import sys
import time
from collections.abc import Callable


_STATUS_EXPIRED = b"E"
_STATUS_UNAVAILABLE = b"U"
_STATUS_COMPLETE = b"C"
_WATCHDOG_READY = b"R"
_WATCHDOG_FAILED = b"F"
_WATCHDOG_POLL_SECONDS = 0.25
_PROCESS_GROUP_POLL_SECONDS = 0.05


def _notify(status_descriptor: int, status: bytes) -> None:
    try:
        os.write(status_descriptor, status)
    except OSError:
        pass


def _close_standard_streams() -> None:
    null_descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null_descriptor, descriptor)
    finally:
        if null_descriptor > 2:
            os.close(null_descriptor)


def _darwin_exit_waiter(target_pid: int) -> tuple[Callable[[float], bool], Callable[[], None]]:
    queue = select.kqueue()
    process_event = select.kevent(
        target_pid,
        filter=select.KQ_FILTER_PROC,
        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
        fflags=select.KQ_NOTE_EXIT,
    )
    queue.control([process_event], 0, 0)

    def wait(timeout: float) -> bool:
        return bool(queue.control([], 1, timeout))

    return wait, queue.close


def _linux_exit_waiter(target_pid: int) -> tuple[Callable[[float], bool], Callable[[], None]]:
    if not hasattr(os, "pidfd_open"):
        raise RuntimeError("pidfd process monitoring is unavailable")
    process_descriptor = os.pidfd_open(target_pid)
    poller = select.poll()
    poller.register(
        process_descriptor,
        select.POLLIN | select.POLLHUP | select.POLLERR,
    )

    def wait(timeout: float) -> bool:
        timeout_ms = max(0, math.ceil(timeout * 1000))
        return bool(poller.poll(timeout_ms))

    def close() -> None:
        os.close(process_descriptor)

    return wait, close


def _exit_waiter(target_pid: int) -> tuple[Callable[[float], bool], Callable[[], None]]:
    if sys.platform == "darwin":
        return _darwin_exit_waiter(target_pid)
    if sys.platform.startswith("linux"):
        return _linux_exit_waiter(target_pid)
    raise RuntimeError("absolute process deadline monitoring is unavailable")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_process_group(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass


def _watch_target(
        *,
        target_pid: int,
        target_process_group: int,
        deadline_ns: int,
        status_descriptor: int,
        ready_descriptor: int,
) -> None:
    close_waiter: Callable[[], None] | None = None
    try:
        os.setsid()
        _close_standard_streams()
        wait_for_exit, close_waiter = _exit_waiter(target_pid)
        os.write(ready_descriptor, _WATCHDOG_READY)
    except BaseException:
        try:
            os.write(ready_descriptor, _WATCHDOG_FAILED)
        except OSError:
            pass
        os._exit(1)
    finally:
        try:
            os.close(ready_descriptor)
        except OSError:
            pass

    target_exited = False
    status = _STATUS_UNAVAILABLE
    try:
        while True:
            remaining_ns = deadline_ns - time.time_ns()
            if remaining_ns <= 0:
                status = _STATUS_EXPIRED
                break
            timeout = min(
                remaining_ns / 1_000_000_000,
                (
                    _PROCESS_GROUP_POLL_SECONDS
                    if target_exited
                    else _WATCHDOG_POLL_SECONDS
                ),
            )
            if target_exited:
                if not _process_group_exists(target_process_group):
                    status = _STATUS_COMPLETE
                    break
                time.sleep(timeout)
            elif wait_for_exit(timeout):
                target_exited = True
                if not _process_group_exists(target_process_group):
                    status = _STATUS_COMPLETE
                    break
    except BaseException:
        status = _STATUS_UNAVAILABLE
    finally:
        if close_waiter is not None:
            try:
                close_waiter()
            except BaseException:
                status = _STATUS_UNAVAILABLE
        _notify(status_descriptor, status)
        if status in {_STATUS_EXPIRED, _STATUS_UNAVAILABLE}:
            _kill_process_group(target_process_group)
        try:
            os.close(status_descriptor)
        except OSError:
            pass


def _stop_watchdog(watchdog_pid: int) -> None:
    try:
        os.kill(watchdog_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(watchdog_pid, 0)
    except ChildProcessError:
        pass


def _run(deadline_ns: int, status_descriptor: int, command: list[str]) -> int:
    if not command:
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    if os.getpid() != os.getpgrp():
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    if time.time_ns() >= deadline_ns:
        _notify(status_descriptor, _STATUS_EXPIRED)
        return 124

    ready_read_descriptor, ready_write_descriptor = os.pipe()
    watchdog_pid = os.fork()
    if watchdog_pid == 0:
        os.close(ready_read_descriptor)
        _watch_target(
            target_pid=os.getppid(),
            target_process_group=os.getpgrp(),
            deadline_ns=deadline_ns,
            status_descriptor=status_descriptor,
            ready_descriptor=ready_write_descriptor,
        )
        os._exit(0)

    os.close(ready_write_descriptor)
    try:
        watchdog_status = os.read(ready_read_descriptor, 1)
    finally:
        os.close(ready_read_descriptor)
    if watchdog_status != _WATCHDOG_READY:
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        _stop_watchdog(watchdog_pid)
        return 125

    os.set_inheritable(status_descriptor, False)
    remaining_ns = deadline_ns - time.time_ns()
    if remaining_ns <= 0:
        _notify(status_descriptor, _STATUS_EXPIRED)
        _stop_watchdog(watchdog_pid)
        return 124
    if time.time_ns() >= deadline_ns:
        _notify(status_descriptor, _STATUS_EXPIRED)
        _stop_watchdog(watchdog_pid)
        return 124
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        _stop_watchdog(watchdog_pid)
        _notify(status_descriptor, _STATUS_COMPLETE)
        os.write(2, f"Mediaforce process launch failed: {exc}\n".encode())
        return 127


def main() -> int:
    try:
        separator = sys.argv.index("--")
        deadline_ns = int(sys.argv[1])
        status_descriptor = int(sys.argv[2])
        command = sys.argv[separator + 1:]
    except (ValueError, IndexError):
        return 125
    return _run(deadline_ns, status_descriptor, command)


if __name__ == "__main__":
    raise SystemExit(main())
