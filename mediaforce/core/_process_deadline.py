from __future__ import annotations

import ctypes
import errno
import os
import select
import signal
import sys
import time
from collections.abc import Callable
from enum import Enum


_STATUS_EXPIRED = b"E"
_STATUS_UNAVAILABLE = b"U"
_STATUS_COMPLETE = b"C"
_TARGET_READY = b"R"
_TREE_POLL_SECONDS = 0.02
_TREE_SETTLE_SECONDS = 0.08
_TERM_GRACE_SECONDS = 0.45
_KILL_GRACE_SECONDS = 0.75
_PR_SET_CHILD_SUBREAPER = 36
_TASK_AUDIT_TOKEN = 15
_PROC_PID_UNIQUE_IDENTIFIER_INFO = 17


class _ContainmentUnavailableError(RuntimeError):
    pass


class _ParentLivenessMonitor:
    def __init__(self, descriptor: int) -> None:
        self._poller = select.poll()
        self._poller.register(
            descriptor,
            select.POLLIN | select.POLLHUP | select.POLLERR,
        )

    def lost(self) -> bool:
        return bool(self._poller.poll(0))


def _notify(status_descriptor: int, status: bytes) -> None:
    try:
        os.write(status_descriptor, status)
    except OSError:
        pass


def _pidfd_exited(process_descriptor: int) -> bool:
    poller = select.poll()
    poller.register(
        process_descriptor,
        select.POLLIN | select.POLLHUP | select.POLLERR,
    )
    return bool(poller.poll(0))


def _linux_parent_pid(pid: int) -> int:
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
        stat = stat_file.read()
    separator = stat.rfind(")")
    if separator < 0:
        raise _ContainmentUnavailableError("invalid procfs process status")
    fields = stat[separator + 2:].split()
    if len(fields) < 2:
        raise _ContainmentUnavailableError("incomplete procfs process status")
    return int(fields[1])


def _linux_child_pids(
        pid: int,
        process_descriptor: int | None = None,
) -> set[int]:
    child_pids: set[int] = set()
    task_path = f"/proc/{pid}/task"
    try:
        tasks = list(os.scandir(task_path))
    except FileNotFoundError as exc:
        if (
            process_descriptor is not None
            and _pidfd_exited(process_descriptor)
        ):
            return child_pids
        raise _ContainmentUnavailableError(
            f"cannot inspect descendants of live process {pid}"
        ) from exc
    except OSError as exc:
        raise _ContainmentUnavailableError(
            f"cannot inspect descendants of process {pid}"
        ) from exc
    for task in tasks:
        try:
            with open(
                f"{task_path}/{task.name}/children",
                encoding="utf-8",
            ) as children_file:
                children = children_file.read()
        except FileNotFoundError as exc:
            if (
                process_descriptor is not None
                and _pidfd_exited(process_descriptor)
            ):
                return child_pids
            try:
                os.stat(task_path)
            except FileNotFoundError as task_root_error:
                if (
                    process_descriptor is not None
                    and _pidfd_exited(process_descriptor)
                ):
                    return child_pids
                raise _ContainmentUnavailableError(
                    f"cannot inspect descendants of live process {pid}"
                ) from task_root_error
            except OSError as task_root_error:
                raise _ContainmentUnavailableError(
                    f"cannot inspect descendants of process {pid}"
                ) from task_root_error
            try:
                os.stat(task.path)
            except FileNotFoundError:
                continue
            except OSError as task_error:
                raise _ContainmentUnavailableError(
                    f"cannot inspect descendants of process {pid}"
                ) from task_error
            raise _ContainmentUnavailableError(
                f"cannot inspect descendants of live process {pid}"
            ) from exc
        except OSError as exc:
            raise _ContainmentUnavailableError(
                f"cannot inspect descendants of process {pid}"
            ) from exc
        child_pids.update(int(child_pid) for child_pid in children.split())
    return child_pids


class _LinuxProcessIdentity:
    def __init__(self, pid: int, parent_pid: int, process_descriptor: int) -> None:
        self.pid = pid
        self.parent_pid = parent_pid
        self.process_descriptor = process_descriptor


class _LinuxProcessTree:
    def __init__(self) -> None:
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            raise _ContainmentUnavailableError("pidfd signaling is unavailable")
        self._pidfd_open = pidfd_open
        self._pidfd_send_signal = pidfd_send_signal
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise _ContainmentUnavailableError(
                "cannot establish a scoped child subreaper"
            ) from OSError(error_number, os.strerror(error_number))
        _linux_child_pids(os.getpid())
        self._processes: dict[int, _LinuxProcessIdentity] = {}

    @property
    def compromised(self) -> bool:
        return False

    def add_root(self, pid: int) -> None:
        if not self._register(pid, os.getpid()):
            raise _ContainmentUnavailableError(
                "managed command exited before containment was established"
            )

    def refresh(self, timeout: float = 0.0) -> None:
        if timeout > 0:
            time.sleep(timeout)
        self._discard_exited()
        changed = True
        while changed:
            changed = False
            parent_pids = [os.getpid(), *self._processes]
            for parent_pid in parent_pids:
                identity = self._processes.get(parent_pid)
                if identity is not None and _pidfd_exited(identity.process_descriptor):
                    continue
                for child_pid in _linux_child_pids(
                    parent_pid,
                    identity.process_descriptor if identity is not None else None,
                ):
                    if child_pid == os.getpid() or child_pid in self._processes:
                        continue
                    if self._register(child_pid, parent_pid):
                        changed = True
            self._discard_exited()

    def live(self) -> bool:
        self._discard_exited()
        return bool(self._processes)

    def signal_all(self, signal_number: int) -> bool:
        self.refresh()
        succeeded = True
        identities = sorted(
            self._processes.values(),
            key=lambda identity: self._depth(identity),
            reverse=True,
        )
        for identity in identities:
            try:
                self._pidfd_send_signal(
                    identity.process_descriptor,
                    signal_number,
                )
            except ProcessLookupError:
                continue
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    succeeded = False
        return succeeded

    def close(self) -> None:
        for identity in self._processes.values():
            try:
                os.close(identity.process_descriptor)
            except OSError:
                pass
        self._processes.clear()

    def _register(self, pid: int, parent_pid: int) -> bool:
        try:
            process_descriptor = self._pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise _ContainmentUnavailableError(
                f"cannot pin process identity for {pid}"
            ) from exc
        if _pidfd_exited(process_descriptor):
            os.close(process_descriptor)
            return False
        try:
            observed_parent_pid = _linux_parent_pid(pid)
        except FileNotFoundError as exc:
            exited = _pidfd_exited(process_descriptor)
            os.close(process_descriptor)
            if exited:
                return False
            raise _ContainmentUnavailableError(
                f"cannot inspect live process {pid}"
            ) from exc
        except BaseException:
            os.close(process_descriptor)
            raise
        if observed_parent_pid not in {parent_pid, os.getpid()}:
            os.close(process_descriptor)
            return False
        self._processes[pid] = _LinuxProcessIdentity(
            pid,
            observed_parent_pid,
            process_descriptor,
        )
        return True

    def _discard_exited(self) -> None:
        for pid, identity in list(self._processes.items()):
            if not _pidfd_exited(identity.process_descriptor):
                continue
            try:
                os.close(identity.process_descriptor)
            except OSError:
                pass
            del self._processes[pid]

    def _depth(self, identity: _LinuxProcessIdentity) -> int:
        depth = 0
        parent_pid = identity.parent_pid
        seen: set[int] = set()
        while parent_pid in self._processes and parent_pid not in seen:
            seen.add(parent_pid)
            depth += 1
            parent_pid = self._processes[parent_pid].parent_pid
        return depth


class _AuditToken(ctypes.Structure):
    _fields_ = [("values", ctypes.c_uint32 * 8)]


class _UniqueProcessInfo(ctypes.Structure):
    _fields_ = [
        ("executable_uuid", ctypes.c_uint8 * 16),
        ("unique_id", ctypes.c_uint64),
        ("parent_unique_id", ctypes.c_uint64),
        ("id_version", ctypes.c_int32),
        ("parent_id_version", ctypes.c_uint32),
        ("reserved", ctypes.c_uint64 * 2),
    ]


class _DarwinSignalState(Enum):
    EXITED = "exited"
    SIGNALABLE = "signalable"
    UNSIGNALABLE = "unsignalable"


class _DarwinProcessIdentity:
    def __init__(
            self,
            *,
            serial: int,
            pid: int,
            parent_serial: int | None,
            token: _AuditToken,
            unique_info: _UniqueProcessInfo,
    ) -> None:
        self.serial = serial
        self.pid = pid
        self.parent_serial = parent_serial
        self.token = token
        self.pid_version = int(token.values[7])
        self.unique_id = int(unique_info.unique_id)
        self.parent_unique_id = int(unique_info.parent_unique_id)
        self.exited = False


class _DarwinProcessTree:
    def __init__(self) -> None:
        if not hasattr(select, "kqueue"):
            raise _ContainmentUnavailableError("kqueue process monitoring is unavailable")
        self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self._configure_functions()
        self._queue = select.kqueue()
        self._next_serial = 1
        self._processes: dict[int, _DarwinProcessIdentity] = {}
        self._current_by_pid: dict[int, int] = {}
        self._serial_by_unique_id: dict[int, int] = {}
        self._compromised = False
        if self._unique_process_info(os.getpid()) is None:
            raise _ContainmentUnavailableError(
                "unique Darwin process identity is unavailable"
            )

    @property
    def compromised(self) -> bool:
        return self._compromised

    def add_root(self, pid: int) -> None:
        if self._register(pid, None, required=True) is None:
            raise _ContainmentUnavailableError(
                "managed command exited before containment was established"
            )

    def refresh(self, timeout: float = 0.0) -> None:
        self._discover_until_stable()
        events = self._read_events(timeout)
        self._discover_until_stable()
        for event in events:
            if not event.fflags & select.KQ_NOTE_FORK:
                continue
            self._compromised = True
            self._discover_reparented_descendants()
        for event in events:
            if event.fflags & select.KQ_NOTE_EXIT:
                identity = self._processes.get(int(event.udata))
                if identity is not None:
                    self._mark_exited(identity)
        self._refresh_liveness()

    def live(self) -> bool:
        self._refresh_liveness()
        return any(not identity.exited for identity in self._processes.values())

    def signal_all(self, signal_number: int) -> bool:
        self.refresh()
        succeeded = True
        identities = sorted(
            (
                identity
                for identity in self._processes.values()
                if not identity.exited
            ),
            key=self._depth,
            reverse=True,
        )
        for identity in identities:
            token_state = self._refresh_signal_token(identity)
            if token_state is _DarwinSignalState.EXITED:
                self._mark_exited(identity)
                continue
            if token_state is _DarwinSignalState.UNSIGNALABLE:
                succeeded = False
                continue
            result = self._libc.proc_signal_with_audittoken(
                ctypes.byref(identity.token),
                signal_number,
            )
            if result == errno.ESRCH:
                token_state = self._refresh_signal_token(identity)
                if token_state is _DarwinSignalState.EXITED:
                    self._mark_exited(identity)
                    continue
                if token_state is _DarwinSignalState.UNSIGNALABLE:
                    succeeded = False
                    continue
                result = self._libc.proc_signal_with_audittoken(
                    ctypes.byref(identity.token),
                    signal_number,
                )
            if result == errno.ESRCH:
                if self._same_identity_alive(identity):
                    succeeded = False
                else:
                    self._mark_exited(identity)
            elif result != 0:
                succeeded = False
        return succeeded

    def close(self) -> None:
        self._queue.close()
        self._processes.clear()
        self._current_by_pid.clear()
        self._serial_by_unique_id.clear()

    def _configure_functions(self) -> None:
        self._libc.mach_task_self.argtypes = []
        self._libc.mach_task_self.restype = ctypes.c_uint
        self._libc.task_name_for_pid.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._libc.task_name_for_pid.restype = ctypes.c_int
        self._libc.task_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._libc.task_info.restype = ctypes.c_int
        self._libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._libc.mach_port_deallocate.restype = ctypes.c_int
        self._libc.proc_listchildpids.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._libc.proc_listchildpids.restype = ctypes.c_int
        self._libc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._libc.proc_listallpids.restype = ctypes.c_int
        self._libc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._libc.proc_pidinfo.restype = ctypes.c_int
        self._libc.proc_signal_with_audittoken.argtypes = [
            ctypes.POINTER(_AuditToken),
            ctypes.c_int,
        ]
        self._libc.proc_signal_with_audittoken.restype = ctypes.c_int

    def _task_identity(self, pid: int) -> tuple[int, _AuditToken] | None:
        task_port = ctypes.c_uint()
        self_task = self._libc.mach_task_self()
        if self._libc.task_name_for_pid(
            self_task,
            pid,
            ctypes.byref(task_port),
        ) != 0:
            return None
        token = self._token_for_task_port(task_port.value)
        if token is None or int(token.values[5]) != pid:
            self._libc.mach_port_deallocate(self_task, task_port)
            raise _ContainmentUnavailableError(
                f"cannot pin process identity for {pid}"
            )
        return task_port.value, token

    def _token_for_task_port(self, task_port: int) -> _AuditToken | None:
        token = _AuditToken()
        count = ctypes.c_uint(8)
        result = self._libc.task_info(
            task_port,
            _TASK_AUDIT_TOKEN,
            ctypes.cast(ctypes.byref(token), ctypes.POINTER(ctypes.c_int)),
            ctypes.byref(count),
        )
        if result != 0 or count.value != 8:
            return None
        return token

    def _child_pids(self, pid: int) -> set[int]:
        capacity = 64
        while capacity <= 65_536:
            buffer = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self._libc.proc_listchildpids(
                pid,
                buffer,
                ctypes.sizeof(buffer),
            )
            if count < 0:
                error_number = ctypes.get_errno()
                if error_number == errno.ESRCH:
                    return set()
                raise _ContainmentUnavailableError(
                    f"cannot inspect descendants of process {pid}"
                ) from OSError(error_number, os.strerror(error_number))
            if count < capacity:
                return {int(buffer[index]) for index in range(count)}
            capacity *= 2
        raise _ContainmentUnavailableError(
            f"descendant list for process {pid} exceeds the supported bound"
        )

    def _all_pids(self) -> set[int]:
        capacity = max(256, self._libc.proc_listallpids(None, 0))
        while capacity <= 131_072:
            buffer = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self._libc.proc_listallpids(buffer, ctypes.sizeof(buffer))
            if count < 0:
                error_number = ctypes.get_errno()
                raise _ContainmentUnavailableError(
                    "cannot inspect the Darwin process table"
                ) from OSError(error_number, os.strerror(error_number))
            if count < capacity:
                return {
                    int(buffer[index])
                    for index in range(count)
                    if buffer[index] > 0
                }
            capacity *= 2
        raise _ContainmentUnavailableError(
            "Darwin process table exceeds the supported bound"
        )

    def _unique_process_info(self, pid: int) -> _UniqueProcessInfo | None:
        info = _UniqueProcessInfo()
        ctypes.set_errno(0)
        result = self._libc.proc_pidinfo(
            pid,
            _PROC_PID_UNIQUE_IDENTIFIER_INFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if result == 0 and ctypes.get_errno() in {0, errno.ESRCH}:
            return None
        if result != ctypes.sizeof(info):
            error_number = ctypes.get_errno()
            raise _ContainmentUnavailableError(
                f"cannot read unique process identity for {pid}"
            ) from OSError(error_number, os.strerror(error_number))
        if info.unique_id == 0:
            raise _ContainmentUnavailableError(
                f"invalid unique process identity for {pid}"
            )
        return info

    def _register(
            self,
            pid: int,
            parent_serial: int | None,
            *,
            required: bool = False,
            observed_unique_info: _UniqueProcessInfo | None = None,
    ) -> int | None:
        current_serial = self._current_by_pid.get(pid)
        if current_serial is not None:
            current = self._processes[current_serial]
            if self._same_identity_alive(current):
                return current_serial
            self._mark_exited(current)
        unique_info = observed_unique_info or self._unique_process_info(pid)
        if unique_info is None:
            if required:
                raise _ContainmentUnavailableError(
                    f"cannot pin process identity for {pid}"
                )
            return None
        known_serial = self._serial_by_unique_id.get(int(unique_info.unique_id))
        if known_serial is not None:
            return known_serial
        if parent_serial is not None:
            parent = self._processes[parent_serial]
            if int(unique_info.parent_unique_id) != parent.unique_id:
                return None
        task_identity = self._task_identity(pid)
        if task_identity is None:
            if required:
                raise _ContainmentUnavailableError(
                    f"cannot pin process identity for {pid}"
                )
            return None
        task_port, token = task_identity
        try:
            confirmed_unique_info = self._unique_process_info(pid)
        finally:
            self._libc.mach_port_deallocate(
                self._libc.mach_task_self(),
                task_port,
            )
        if (
            confirmed_unique_info is None
            or confirmed_unique_info.unique_id != unique_info.unique_id
        ):
            return None
        serial = self._next_serial
        self._next_serial += 1
        identity = _DarwinProcessIdentity(
            serial=serial,
            pid=pid,
            parent_serial=parent_serial,
            token=token,
            unique_info=confirmed_unique_info,
        )
        process_event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_FORK,
            udata=serial,
        )
        try:
            self._queue.control([process_event], 0, 0)
        except ProcessLookupError:
            if required:
                raise _ContainmentUnavailableError(
                    f"cannot monitor process identity for {pid}"
                )
            return None
        except OSError as exc:
            raise _ContainmentUnavailableError(
                f"cannot monitor process identity for {pid}"
            ) from exc
        self._processes[serial] = identity
        self._current_by_pid[pid] = serial
        self._serial_by_unique_id[identity.unique_id] = serial
        return serial

    def _discover_until_stable(self) -> None:
        changed = True
        while changed:
            changed = False
            for identity in list(self._processes.values()):
                if identity.exited or not self._same_identity_alive(identity):
                    continue
                for child_pid in self._child_pids(identity.pid):
                    before = self._current_by_pid.get(child_pid)
                    after = self._register(child_pid, identity.serial)
                    if after is not None and after != before:
                        changed = True

    def _discover_reparented_descendants(self) -> None:
        candidates: list[tuple[int, _UniqueProcessInfo]] = []
        for pid in self._all_pids():
            try:
                info = self._unique_process_info(pid)
            except _ContainmentUnavailableError:
                continue
            if info is not None:
                candidates.append((pid, info))
        changed = True
        while changed:
            changed = False
            for pid, info in candidates:
                if int(info.unique_id) in self._serial_by_unique_id:
                    continue
                parent_serial = self._serial_by_unique_id.get(
                    int(info.parent_unique_id)
                )
                if parent_serial is None:
                    continue
                if self._register(
                    pid,
                    parent_serial,
                    observed_unique_info=info,
                ) is not None:
                    changed = True

    def _read_events(self, timeout: float) -> list[select.kevent]:
        events: list[select.kevent] = []
        event_limit = max(64, len(self._processes) * 2)
        wait_timeout = timeout
        while True:
            batch = self._queue.control([], event_limit, wait_timeout)
            events.extend(batch)
            if len(batch) < event_limit:
                return events
            wait_timeout = 0

    def _same_identity_alive(self, identity: _DarwinProcessIdentity) -> bool:
        info = self._unique_process_info(identity.pid)
        if info is None:
            return False
        return int(info.unique_id) == identity.unique_id

    def _refresh_signal_token(
            self,
            identity: _DarwinProcessIdentity,
    ) -> _DarwinSignalState:
        info = self._unique_process_info(identity.pid)
        if info is None or int(info.unique_id) != identity.unique_id:
            return _DarwinSignalState.EXITED
        task_identity = self._task_identity(identity.pid)
        if task_identity is None:
            return _DarwinSignalState.UNSIGNALABLE
        task_port, token = task_identity
        try:
            confirmed_info = self._unique_process_info(identity.pid)
        finally:
            self._libc.mach_port_deallocate(
                self._libc.mach_task_self(),
                task_port,
            )
        if confirmed_info is None or int(confirmed_info.unique_id) != identity.unique_id:
            return _DarwinSignalState.EXITED
        identity.token = token
        identity.pid_version = int(token.values[7])
        return _DarwinSignalState.SIGNALABLE

    def _refresh_liveness(self) -> None:
        for identity in self._processes.values():
            if not identity.exited and not self._same_identity_alive(identity):
                self._mark_exited(identity)

    def _mark_exited(self, identity: _DarwinProcessIdentity) -> None:
        identity.exited = True
        if self._current_by_pid.get(identity.pid) == identity.serial:
            del self._current_by_pid[identity.pid]

    def _depth(self, identity: _DarwinProcessIdentity) -> int:
        depth = 0
        parent_serial = identity.parent_serial
        seen: set[int] = set()
        while parent_serial is not None and parent_serial not in seen:
            seen.add(parent_serial)
            parent = self._processes.get(parent_serial)
            if parent is None:
                break
            depth += 1
            parent_serial = parent.parent_serial
        return depth


def _process_tree() -> _LinuxProcessTree | _DarwinProcessTree:
    if sys.platform.startswith("linux"):
        return _LinuxProcessTree()
    if sys.platform == "darwin":
        return _DarwinProcessTree()
    raise _ContainmentUnavailableError(
        "managed process containment is unavailable on this host"
    )


def _reap_children(
        target_pid: int,
        target_status: int | None,
) -> int | None:
    while True:
        try:
            reaped_pid, wait_status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return target_status
        if reaped_pid == 0:
            return target_status
        if reaped_pid == target_pid:
            target_status = wait_status


def _terminate_tree(
        tree: _LinuxProcessTree | _DarwinProcessTree,
        reap: Callable[[], None],
) -> bool:
    succeeded = tree.signal_all(signal.SIGTERM)
    term_deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < term_deadline:
        reap()
        tree.refresh(_TREE_POLL_SECONDS)
        if not tree.live():
            reap()
            return succeeded and not tree.compromised
        succeeded = tree.signal_all(signal.SIGTERM) and succeeded
    succeeded = tree.signal_all(signal.SIGKILL) and succeeded
    kill_deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while time.monotonic() < kill_deadline:
        reap()
        tree.refresh(_TREE_POLL_SECONDS)
        if not tree.live():
            reap()
            return succeeded and not tree.compromised
        succeeded = tree.signal_all(signal.SIGKILL) and succeeded
    return False


def _terminate_tree_after_parent_loss(
        tree: _LinuxProcessTree | _DarwinProcessTree,
        reap: Callable[[], None],
) -> None:
    while True:
        try:
            _terminate_tree(tree, reap)
            reap()
            tree.refresh(_TREE_POLL_SECONDS)
            reap()
            if not tree.live():
                return
        except BaseException:
            pass
        time.sleep(_TREE_POLL_SECONDS)


def _target_child(
        gate_descriptor: int,
        status_descriptor: int,
        parent_liveness_descriptor: int,
        command: list[str],
) -> None:
    os.close(parent_liveness_descriptor)
    try:
        gate_status = os.read(gate_descriptor, 1)
    finally:
        os.close(gate_descriptor)
    if gate_status != _TARGET_READY:
        os._exit(125)
    os.close(status_descriptor)
    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signal_number, signal.SIG_DFL)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        os.write(2, f"Mediaforce process launch failed: {exc}\n".encode())
        os._exit(127)


def _exit_like_target(wait_status: int) -> int:
    exit_code = os.waitstatus_to_exitcode(wait_status)
    if exit_code >= 0:
        return exit_code
    signal_number = -exit_code
    if signal_number not in {signal.SIGKILL, signal.SIGSTOP}:
        signal.signal(signal_number, signal.SIG_DFL)
    os.kill(os.getpid(), signal_number)
    return 128 + signal_number


def _run(
        deadline_ns: int,
        status_descriptor: int,
        parent_liveness_descriptor: int,
        command: list[str],
) -> int:
    if not command:
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    if deadline_ns >= 0 and time.time_ns() >= deadline_ns:
        _notify(status_descriptor, _STATUS_EXPIRED)
        return 124

    termination_signal = 0

    def request_termination(signal_number: int, _frame: object) -> None:
        nonlocal termination_signal
        if termination_signal == 0:
            termination_signal = signal_number

    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signal_number, request_termination)

    try:
        parent_liveness = _ParentLivenessMonitor(parent_liveness_descriptor)
    except BaseException:
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    if parent_liveness.lost():
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125

    try:
        tree = _process_tree()
    except BaseException:
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    if parent_liveness.lost():
        tree.close()
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125

    gate_read_descriptor, gate_write_descriptor = os.pipe()
    target_pid = os.fork()
    if target_pid == 0:
        os.close(gate_write_descriptor)
        _target_child(
            gate_read_descriptor,
            status_descriptor,
            parent_liveness_descriptor,
            command,
        )
        os._exit(125)

    os.close(gate_read_descriptor)
    target_status: int | None = None

    def reap() -> None:
        nonlocal target_status
        target_status = _reap_children(target_pid, target_status)

    try:
        try:
            tree.add_root(target_pid)
        except BaseException:
            os.close(gate_write_descriptor)
            _terminate_tree(tree, reap)
            _notify(status_descriptor, _STATUS_UNAVAILABLE)
            return 125
        if parent_liveness.lost():
            os.close(gate_write_descriptor)
            _terminate_tree_after_parent_loss(tree, reap)
            _notify(status_descriptor, _STATUS_UNAVAILABLE)
            return 125
        if termination_signal != 0:
            os.close(gate_write_descriptor)
            cleanup_succeeded = _terminate_tree(tree, reap)
            _notify(
                status_descriptor,
                _STATUS_COMPLETE if cleanup_succeeded else _STATUS_UNAVAILABLE,
            )
            return 128 + termination_signal
        if deadline_ns >= 0 and time.time_ns() >= deadline_ns:
            os.close(gate_write_descriptor)
            cleanup_succeeded = _terminate_tree(tree, reap)
            _notify(
                status_descriptor,
                _STATUS_EXPIRED if cleanup_succeeded else _STATUS_UNAVAILABLE,
            )
            return 124
        os.write(gate_write_descriptor, _TARGET_READY)
        os.close(gate_write_descriptor)

        empty_since: float | None = None
        while True:
            reap()
            tree.refresh(_TREE_POLL_SECONDS)
            reap()
            if parent_liveness.lost():
                _terminate_tree_after_parent_loss(tree, reap)
                _notify(status_descriptor, _STATUS_UNAVAILABLE)
                return 125
            if termination_signal != 0:
                cleanup_succeeded = _terminate_tree(tree, reap)
                _notify(
                    status_descriptor,
                    _STATUS_COMPLETE if cleanup_succeeded else _STATUS_UNAVAILABLE,
                )
                return 128 + termination_signal
            if deadline_ns >= 0 and time.time_ns() >= deadline_ns:
                cleanup_succeeded = _terminate_tree(tree, reap)
                _notify(
                    status_descriptor,
                    _STATUS_EXPIRED if cleanup_succeeded else _STATUS_UNAVAILABLE,
                )
                return 124
            if target_status is not None and not tree.live():
                if empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since >= _TREE_SETTLE_SECONDS:
                    if tree.compromised:
                        _notify(status_descriptor, _STATUS_UNAVAILABLE)
                        return 125
                    _notify(status_descriptor, _STATUS_COMPLETE)
                    return _exit_like_target(target_status)
            else:
                empty_since = None
    except BaseException:
        if parent_liveness.lost():
            _terminate_tree_after_parent_loss(tree, reap)
        else:
            try:
                _terminate_tree(tree, reap)
            except BaseException:
                pass
        _notify(status_descriptor, _STATUS_UNAVAILABLE)
        return 125
    finally:
        tree.close()


def main() -> int:
    try:
        separator = sys.argv.index("--")
        deadline_ns = int(sys.argv[1])
        status_descriptor = int(sys.argv[2])
        parent_liveness_descriptor = int(sys.argv[3])
        command = sys.argv[separator + 1:]
    except (ValueError, IndexError):
        return 125
    try:
        return _run(
            deadline_ns,
            status_descriptor,
            parent_liveness_descriptor,
            command,
        )
    finally:
        try:
            os.close(status_descriptor)
        except OSError:
            pass
        try:
            os.close(parent_liveness_descriptor)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
