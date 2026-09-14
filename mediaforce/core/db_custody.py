from __future__ import annotations

from collections.abc import Callable
import ctypes
import errno
import os
from pathlib import Path
import select as select_module
import stat
import struct
import sys
import threading
from typing import Any


class DatabaseCustodyError(RuntimeError):
    pass


class DatabaseCustodyBorrow:
    """Explicit borrow released only after the SQLite handle is proven closed."""

    def __init__(self, custody: DatabaseFileCustody) -> None:
        self._custody: DatabaseFileCustody | None = custody

    @property
    def file_descriptor(self) -> int:
        custody = self._custody
        if custody is None:
            raise DatabaseCustodyError("database custody borrow is released")
        return custody.file_descriptor

    def assert_quiet(self) -> None:
        custody = self._custody
        if custody is None:
            raise DatabaseCustodyError("database custody borrow is released")
        custody.assert_quiet()

    def release(self) -> None:
        custody = self._custody
        if custody is None:
            return
        self._custody = None
        custody._release_borrow()


class DatabaseFileCustody:
    def __init__(
            self,
            path: Path,
            *,
            file_descriptor: int,
            close_file_descriptor: Callable[[], None],
    ) -> None:
        self._path = path.expanduser().resolve()
        self._file_descriptor = file_descriptor
        self._close_file_descriptor = close_file_descriptor
        self._state_lock = threading.Lock()
        self._borrow_count = 0
        self._closing = False
        self._closed = False
        self._file_descriptor_closed = False
        self._file_descriptor_close_attempted = False
        self._file_descriptor_close_error: BaseException | None = None
        self._witness: _NamespaceEventWitness | None = None
        self._expected_file: tuple[int, int, int] | None = None
        self._expected_parent: tuple[int, int] | None = None
        self._arm()

    @property
    def file_descriptor(self) -> int:
        return self._file_descriptor

    def matches(self, path: Path, identity: tuple[int, int]) -> bool:
        with self._state_lock:
            return (
                not self._closing
                and not self._closed
                and path.expanduser().resolve() == self._path
                and self._expected_file is not None
                and self._expected_file[:2] == identity
            )

    def borrow(self) -> DatabaseCustodyBorrow:
        with self._state_lock:
            if self._closing or self._closed:
                raise DatabaseCustodyError("database custody lease is closing")
            self._assert_quiet_locked()
            self._borrow_count += 1
        return DatabaseCustodyBorrow(self)

    def assert_quiet(self) -> None:
        with self._state_lock:
            self._assert_quiet_locked()

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            if self._borrow_count == 0:
                self._close_locked()

    def _release_borrow(self) -> None:
        with self._state_lock:
            if self._borrow_count <= 0:
                raise DatabaseCustodyError("database custody borrow count is invalid")
            self._borrow_count -= 1
            if self._closing and self._borrow_count == 0:
                self._close_locked()

    def _arm(self) -> None:
        last_error: BaseException | None = None
        for _attempt in range(3):
            witness: _NamespaceEventWitness | None = None
            try:
                file_info = os.fstat(self._file_descriptor)
                path_info = self._path.stat(follow_symlinks=False)
                parent_info = self._path.parent.stat(follow_symlinks=False)
                expected_file = _file_identity(file_info)
                expected_parent = _directory_identity(parent_info)
                if (
                    not stat.S_ISREG(file_info.st_mode)
                    or not stat.S_ISREG(path_info.st_mode)
                    or _file_identity(path_info) != expected_file
                    or not stat.S_ISDIR(parent_info.st_mode)
                ):
                    raise DatabaseCustodyError(
                        "database custody identity is invalid"
                    )
                before_ctime = file_info.st_ctime_ns
                witness = _NamespaceEventWitness(
                    file_descriptor=self._file_descriptor,
                    parent_path=self._path.parent,
                    expected_parent=expected_parent,
                )
                after_info = os.fstat(self._file_descriptor)
                if (
                    after_info.st_ctime_ns != before_ctime
                    or _file_identity(after_info) != expected_file
                ):
                    witness.close()
                    last_error = DatabaseCustodyError(
                        "database custody metadata changed while arming"
                    )
                    continue
                self._witness = witness
                self._expected_file = expected_file
                self._expected_parent = expected_parent
                self._assert_quiet_locked()
                return
            except BaseException as exc:
                if witness is not None:
                    witness.close()
                last_error = exc
                if not isinstance(exc, _VolatileCustodyMetadata):
                    break
        raise DatabaseCustodyError(
            "database custody monitoring is unavailable"
        ) from last_error

    def _assert_quiet_locked(self) -> None:
        witness = self._witness
        expected_file = self._expected_file
        expected_parent = self._expected_parent
        if (
            witness is None
            or expected_file is None
            or expected_parent is None
            or self._closed
        ):
            raise DatabaseCustodyError("database custody monitoring is unavailable")
        witness.assert_quiet()
        try:
            descriptor_info = os.fstat(self._file_descriptor)
            path_info = self._path.stat(follow_symlinks=False)
            parent_info = self._path.parent.stat(follow_symlinks=False)
            relative_info = os.stat(
                self._path.name,
                dir_fd=witness.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DatabaseCustodyError("database custody identity changed") from exc
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or _directory_identity(parent_info) != expected_parent
            or any(
                not stat.S_ISREG(info.st_mode)
                or _file_identity(info) != expected_file
                for info in (descriptor_info, path_info, relative_info)
            )
        ):
            raise DatabaseCustodyError("database custody identity changed")

    def _close_locked(self) -> None:
        if self._closed:
            return
        cleanup_error: BaseException | None = None
        witness = self._witness
        if witness is not None:
            try:
                witness.close()
            except BaseException as exc:
                cleanup_error = exc
            else:
                self._witness = None
        if not self._file_descriptor_close_attempted:
            self._file_descriptor_close_attempted = True
            try:
                self._close_file_descriptor()
            except BaseException as exc:
                self._file_descriptor_close_error = exc
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(
                        "Database custody descriptor cleanup also failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            else:
                self._file_descriptor_closed = True
        elif self._file_descriptor_close_error is not None:
            if cleanup_error is None:
                cleanup_error = self._file_descriptor_close_error
            else:
                cleanup_error.add_note(
                    "Database custody descriptor cleanup remains unresolved: "
                    f"{type(self._file_descriptor_close_error).__name__}: "
                    f"{self._file_descriptor_close_error}"
                )
        self._closed = self._witness is None and self._file_descriptor_closed
        if cleanup_error is not None:
            raise cleanup_error


class _VolatileCustodyMetadata(RuntimeError):
    pass


class _NamespaceEventWitness:
    _IN_ATTRIB = 0x00000004
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800
    _IN_UNMOUNT = 0x00002000
    _IN_Q_OVERFLOW = 0x00004000
    _IN_IGNORED = 0x00008000
    _IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
    _IN_NONBLOCK = os.O_NONBLOCK
    _EVENT_HEADER = struct.Struct("iIII")

    def __init__(
            self,
            *,
            file_descriptor: int,
            parent_path: Path,
            expected_parent: tuple[int, int],
    ) -> None:
        self._file_descriptor = file_descriptor
        self._parent_descriptor = -1
        self._watcher: Any | None = None
        self._inotify_descriptor = -1
        self._inotify_watches: set[int] = set()
        self._violated = False
        try:
            self._parent_descriptor = _open_parent_descriptor(
                parent_path,
                expected_parent=expected_parent,
            )
            if sys.platform == "darwin":
                self._arm_kqueue()
            elif sys.platform.startswith("linux"):
                self._arm_inotify()
            else:
                raise DatabaseCustodyError(
                    "database custody monitoring is unsupported"
                )
        except BaseException:
            self.close()
            raise

    @property
    def parent_descriptor(self) -> int:
        return self._parent_descriptor

    def assert_quiet(self) -> None:
        if self._violated:
            raise DatabaseCustodyError("database namespace custody was violated")
        try:
            if sys.platform == "darwin":
                watcher = self._watcher
                if watcher is None or watcher.control(None, 16, 0):
                    self._violated = True
            elif sys.platform.startswith("linux"):
                self._poll_inotify()
            else:
                self._violated = True
        except (OSError, ValueError):
            self._violated = True
        if self._violated:
            raise DatabaseCustodyError("database namespace custody was violated")

    def close(self) -> None:
        cleanup_error: BaseException | None = None
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            try:
                watcher.close()
            except BaseException as exc:
                cleanup_error = exc
        if self._inotify_descriptor >= 0:
            inotify_descriptor = self._inotify_descriptor
            self._inotify_descriptor = -1
            try:
                os.close(inotify_descriptor)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(
                        "Inotify descriptor cleanup also failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if self._parent_descriptor >= 0:
            parent_descriptor = self._parent_descriptor
            self._parent_descriptor = -1
            try:
                os.close(parent_descriptor)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(
                        "Database custody parent descriptor cleanup also failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if cleanup_error is not None:
            raise cleanup_error

    def _arm_kqueue(self) -> None:
        required = (
            "kqueue",
            "kevent",
            "KQ_FILTER_VNODE",
            "KQ_EV_ADD",
            "KQ_EV_CLEAR",
            "KQ_NOTE_RENAME",
            "KQ_NOTE_DELETE",
            "KQ_NOTE_LINK",
            "KQ_NOTE_REVOKE",
        )
        if any(not hasattr(select_module, name) for name in required):
            raise DatabaseCustodyError("database custody kqueue is unavailable")
        watcher = select_module.kqueue()
        self._watcher = watcher
        file_flags = (
            select_module.KQ_NOTE_RENAME
            | select_module.KQ_NOTE_DELETE
            | select_module.KQ_NOTE_LINK
            | select_module.KQ_NOTE_REVOKE
        )
        parent_flags = (
            select_module.KQ_NOTE_RENAME
            | select_module.KQ_NOTE_DELETE
            | select_module.KQ_NOTE_REVOKE
        )
        watcher.control(
            [
                select_module.kevent(
                    self._file_descriptor,
                    filter=select_module.KQ_FILTER_VNODE,
                    flags=select_module.KQ_EV_ADD | select_module.KQ_EV_CLEAR,
                    fflags=file_flags,
                ),
                select_module.kevent(
                    self._parent_descriptor,
                    filter=select_module.KQ_FILTER_VNODE,
                    flags=select_module.KQ_EV_ADD | select_module.KQ_EV_CLEAR,
                    fflags=parent_flags,
                ),
            ],
            0,
            0,
        )

    def _arm_inotify(self) -> None:
        if not Path("/proc/self/fd").is_dir():
            raise DatabaseCustodyError("database custody inotify path is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        inotify_init1 = getattr(libc, "inotify_init1", None)
        inotify_add_watch = getattr(libc, "inotify_add_watch", None)
        if inotify_init1 is None or inotify_add_watch is None:
            raise DatabaseCustodyError("database custody inotify is unavailable")
        inotify_init1.argtypes = (ctypes.c_int,)
        inotify_init1.restype = ctypes.c_int
        inotify_add_watch.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        inotify_add_watch.restype = ctypes.c_int
        descriptor = inotify_init1(self._IN_CLOEXEC | self._IN_NONBLOCK)
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        self._inotify_descriptor = descriptor
        leaf_mask = (
            self._IN_MOVE_SELF
            | self._IN_DELETE_SELF
            | self._IN_ATTRIB
            | self._IN_UNMOUNT
        )
        parent_mask = self._IN_MOVE_SELF | self._IN_DELETE_SELF | self._IN_UNMOUNT
        for watched_descriptor, mask in (
            (self._file_descriptor, leaf_mask),
            (self._parent_descriptor, parent_mask),
        ):
            watch = inotify_add_watch(
                descriptor,
                os.fsencode(f"/proc/self/fd/{watched_descriptor}"),
                mask,
            )
            if watch < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            self._inotify_watches.add(watch)

    def _poll_inotify(self) -> None:
        if self._inotify_descriptor < 0:
            self._violated = True
            return
        for _read_attempt in range(8):
            try:
                payload = os.read(self._inotify_descriptor, 65_536)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    return
                raise
            if not payload:
                self._violated = True
                return
            offset = 0
            while offset < len(payload):
                remaining = len(payload) - offset
                if remaining < self._EVENT_HEADER.size:
                    self._violated = True
                    return
                watch, mask, _cookie, name_length = self._EVENT_HEADER.unpack_from(
                    payload,
                    offset,
                )
                event_size = self._EVENT_HEADER.size + name_length
                if event_size > remaining:
                    self._violated = True
                    return
                if (
                    mask & (self._IN_Q_OVERFLOW | self._IN_UNMOUNT | self._IN_IGNORED)
                    or watch in self._inotify_watches
                ):
                    self._violated = True
                offset += event_size
            if len(payload) < 65_536:
                return
        self._violated = True


def _open_parent_descriptor(
        path: Path,
        *,
        expected_parent: tuple[int, int],
) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise DatabaseCustodyError("database custody directory pinning is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or _directory_identity(info) != expected_parent:
            raise _VolatileCustodyMetadata
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_nlink


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino
