from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select as select_module
import stat
import struct
import sys
import threading

from mediaforce.core.config import (
    MediaforceConfig,
    legacy_sqlite_migration_quarantine_name,
)
from mediaforce.core.db import register_database_identity_provider
from mediaforce.core.file_integrity import (
    FileIntegrityError,
    MacOSFileIntegrityGuard,
    rename_exclusive,
)
from mediaforce.core.utils import filesystem_collision_key


class MediaforceRuntimeBusyError(RuntimeError):
    pass


class MediaforceRuntimeLockOwnershipError(RuntimeError):
    pass


@dataclass(slots=True)
class MediaforceRuntimeLease:
    namespace_keys: tuple[str, ...]
    owner_pid: int
    _database_path_key: str | None = field(default=None, repr=False)
    _database_identity: tuple[int, int] | None = field(default=None, repr=False)
    _active: bool = field(default=False, init=False, repr=False)
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _extension_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _extra_descriptors: list[int] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def assert_active(self) -> None:
        with self._state_lock:
            active = self._active
        with _RUNTIME_LEASE_REGISTRY_LOCK:
            registered = _REGISTERED_RUNTIME_LEASES.get(id(self)) is self
        if not active or not registered or self.owner_pid != os.getpid():
            raise MediaforceRuntimeLockOwnershipError(
                "Mediaforce runtime lock ownership is not active"
            )

    @contextmanager
    def bind(self) -> Iterator[MediaforceRuntimeLease]:
        self.assert_active()
        binding = _ACTIVE_RUNTIME_LEASE.set(self)
        try:
            self.assert_active()
            yield self
        finally:
            _ACTIVE_RUNTIME_LEASE.reset(binding)

    def reserve_database_identity(
            self,
            config: MediaforceConfig,
            *,
            create_if_missing: bool,
    ) -> None:
        self.assert_active()
        with self._extension_lock:
            self.assert_active()
            db_path = config.paths.db_path.expanduser().resolve()
            path_key = f"db_path:{filesystem_collision_key(db_path)}"
            if self._database_path_key is None:
                self._database_path_key = path_key
            if (
                path_key != self._database_path_key
                or path_key not in self.namespace_keys
            ):
                raise MediaforceRuntimeBusyError(
                    "Mediaforce runtime db_path namespace changed under the active lease"
                )
            try:
                identity = db_path.stat()
            except FileNotFoundError:
                identity = None
            except OSError as exc:
                raise MediaforceRuntimeBusyError(
                    "Mediaforce runtime db_path identity is unavailable"
                ) from exc
            if self._database_identity is not None:
                if identity is None or (
                    identity.st_dev,
                    identity.st_ino,
                ) != self._database_identity:
                    raise MediaforceRuntimeBusyError(
                        "Mediaforce runtime db_path identity changed under the active lease"
                    )
                self.assert_database_identity_reserved(db_path)
                return
            if identity is None and not create_if_missing:
                return
            descriptor = _open_runtime_direct_file_lock(
                _RuntimeDirectFileLock(
                    label="db_path",
                    path=db_path,
                    device=identity.st_dev if identity is not None else None,
                    inode=identity.st_ino if identity is not None else None,
                    create_if_missing=True,
                    database_identity_lock=True,
                ),
                lock_path=mediaforce_runtime_lock_path(config),
            )
            try:
                descriptor_identity = os.fstat(descriptor)
                try:
                    current_identity = db_path.stat()
                except OSError as exc:
                    raise MediaforceRuntimeBusyError(
                        "Mediaforce runtime db_path identity changed during reservation"
                    ) from exc
                if (
                    descriptor_identity.st_dev,
                    descriptor_identity.st_ino,
                ) != (
                    current_identity.st_dev,
                    current_identity.st_ino,
                ):
                    raise MediaforceRuntimeBusyError(
                        "Mediaforce runtime db_path identity changed during reservation"
                    )
                inode_key = (
                    f"file:{descriptor_identity.st_dev}:{descriptor_identity.st_ino}"
                )
                self._extra_descriptors.append(descriptor)
                self.namespace_keys = tuple(
                    sorted({*self.namespace_keys, inode_key})
                )
                self._database_identity = (
                    descriptor_identity.st_dev,
                    descriptor_identity.st_ino,
                )
                self.assert_database_identity_reserved(db_path)
            except BaseException:
                if descriptor not in self._extra_descriptors:
                    os.close(descriptor)
                raise

    def assert_database_identity_reserved(self, db_path: Path) -> tuple[int, int]:
        self.assert_active()
        try:
            resolved_path = db_path.expanduser().resolve()
            identity = resolved_path.stat()
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime db_path identity is unavailable"
            ) from exc
        path_key = f"db_path:{filesystem_collision_key(resolved_path)}"
        inode_key = f"file:{identity.st_dev}:{identity.st_ino}"
        if (
            not stat.S_ISREG(identity.st_mode)
            or path_key != self._database_path_key
            or self._database_identity is None
            or (identity.st_dev, identity.st_ino) != self._database_identity
            or path_key not in self.namespace_keys
            or inode_key not in self.namespace_keys
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime db_path identity is not reserved by the active lease"
            )
        return self._database_identity

    def _activate(self, authority: object) -> None:
        if authority is not _RUNTIME_LEASE_AUTHORITY:
            raise MediaforceRuntimeLockOwnershipError(
                "Mediaforce runtime lease activation is not authorized"
            )
        with _RUNTIME_LEASE_REGISTRY_LOCK:
            _REGISTERED_RUNTIME_LEASES[id(self)] = self
        with self._state_lock:
            self._active = True

    def _deactivate(self, authority: object) -> None:
        if authority is not _RUNTIME_LEASE_AUTHORITY:
            raise MediaforceRuntimeLockOwnershipError(
                "Mediaforce runtime lease deactivation is not authorized"
            )
        with self._state_lock:
            self._active = False
        with _RUNTIME_LEASE_REGISTRY_LOCK:
            if _REGISTERED_RUNTIME_LEASES.get(id(self)) is self:
                _REGISTERED_RUNTIME_LEASES.pop(id(self), None)


_RUNTIME_NAMESPACE_LOCAL_DIRECTORY_NAME = ".mediaforce-runtime-reservations"
_RUNTIME_NAMESPACE_LOCK_FILE_NAME = "lock"
_RUNTIME_DATABASE_LOCK_OFFSET = 1 << 62
_RUNTIME_LEASE_AUTHORITY = object()
_RUNTIME_LEASE_REGISTRY_LOCK = threading.Lock()
_REGISTERED_RUNTIME_LEASES: dict[int, MediaforceRuntimeLease] = {}
_ACTIVE_RUNTIME_LEASE: ContextVar[MediaforceRuntimeLease | None] = ContextVar(
    "mediaforce_active_runtime_lease",
    default=None,
)


def assert_mediaforce_runtime_lock_held() -> MediaforceRuntimeLease:
    lease = _ACTIVE_RUNTIME_LEASE.get()
    if lease is None:
        raise MediaforceRuntimeLockOwnershipError(
            "Mediaforce runtime lock ownership is required"
        )
    lease.assert_active()
    return lease


def reserve_mediaforce_database_identity(
        config: MediaforceConfig,
        *,
        create_if_missing: bool = False,
) -> None:
    assert_mediaforce_runtime_lock_held().reserve_database_identity(
        config,
        create_if_missing=create_if_missing,
    )


def assert_mediaforce_database_identity_reserved(
        db_path: Path,
        expected_identity: tuple[int, int, int] | None = None,
) -> tuple[int, int, int] | None:
    with _RUNTIME_LEASE_REGISTRY_LOCK:
        leases = tuple(_REGISTERED_RUNTIME_LEASES.values())
    if not leases:
        if expected_identity is not None:
            raise MediaforceRuntimeBusyError(
                "Mediaforce writable database lease ended during database access"
            )
        return None
    resolved_path = db_path.expanduser().resolve()
    path_key = f"db_path:{filesystem_collision_key(resolved_path)}"
    matching_leases = tuple(
        lease for lease in leases if path_key == lease._database_path_key
    )
    if not matching_leases:
        raise MediaforceRuntimeBusyError(
            "Mediaforce writable database path is outside the active runtime lease"
        )
    last_error: MediaforceRuntimeBusyError | None = None
    for lease in matching_leases:
        try:
            file_identity = lease.assert_database_identity_reserved(resolved_path)
            lease_identity = (
                id(lease),
                file_identity[0],
                file_identity[1],
            )
            if (
                expected_identity is not None
                and lease_identity != expected_identity
            ):
                raise MediaforceRuntimeBusyError(
                    "Mediaforce writable database identity changed under the active lease"
                )
            return lease_identity
        except MediaforceRuntimeBusyError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise MediaforceRuntimeBusyError(
        "Mediaforce writable database identity is not reserved"
    )


register_database_identity_provider(
    assert_mediaforce_database_identity_reserved,
)


def mediaforce_runtime_lock_path(config: MediaforceConfig) -> Path:
    return mediaforce_runtime_lock_path_for_web_state_dir(
        config.paths.web_state_dir
    )


def mediaforce_runtime_lock_path_for_web_state_dir(web_state_dir: Path) -> Path:
    return web_state_dir.expanduser().resolve().parent / "mediaforce-web.lock"


def mediaforce_runtime_reservation_dir(config: MediaforceConfig) -> Path:
    configured_path = getattr(config.paths, "runtime_reservation_dir", None)
    if configured_path is not None:
        return Path(configured_path).expanduser().resolve()
    return (
        config.paths.web_state_dir.expanduser().resolve().parent
        / _RUNTIME_NAMESPACE_LOCAL_DIRECTORY_NAME
    )


def mediaforce_runtime_lock_owner(lock_path: Path) -> str | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    host = payload.get("host")
    port = payload.get("port")
    purpose = str(payload.get("purpose") or "").strip()
    if pid and host and port:
        return f"pid {pid} on {host}:{port}"
    if pid and purpose:
        return f"pid {pid} ({purpose})"
    if pid:
        return f"pid {pid}"
    return purpose or None


def _acquire_nonblocking_lock(
        descriptor: int,
        *,
        lock_path: Path,
) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        owner = mediaforce_runtime_lock_owner(lock_path)
        owner_detail = f" ({owner})" if owner else ""
        raise MediaforceRuntimeBusyError(
            f"Mediaforce runtime is already active{owner_detail}"
        ) from exc


class _DarwinFileLock(ctypes.Structure):
    _fields_ = [
        ("l_start", ctypes.c_longlong),
        ("l_len", ctypes.c_longlong),
        ("l_pid", ctypes.c_int),
        ("l_type", ctypes.c_short),
        ("l_whence", ctypes.c_short),
    ]


class _LinuxFileLock(ctypes.Structure):
    _fields_ = [
        ("l_type", ctypes.c_short),
        ("l_whence", ctypes.c_short),
        ("l_start", ctypes.c_longlong),
        ("l_len", ctypes.c_longlong),
        ("l_pid", ctypes.c_int),
    ]


def _database_identity_lock_payload() -> bytes:
    if sys.platform == "darwin":
        if ctypes.sizeof(_DarwinFileLock) != 24:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime database identity lock layout is invalid"
            )
        lock = _DarwinFileLock(
            l_start=_RUNTIME_DATABASE_LOCK_OFFSET,
            l_len=1,
            l_pid=0,
            l_type=fcntl.F_WRLCK,
            l_whence=os.SEEK_SET,
        )
    elif sys.platform.startswith("linux"):
        expected_size = 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24
        if ctypes.sizeof(_LinuxFileLock) != expected_size:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime database identity lock layout is invalid"
            )
        lock = _LinuxFileLock(
            l_type=fcntl.F_WRLCK,
            l_whence=os.SEEK_SET,
            l_start=_RUNTIME_DATABASE_LOCK_OFFSET,
            l_len=1,
            l_pid=0,
        )
    else:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime database identity locking is unavailable"
        )
    return bytes(lock)


def _acquire_database_identity_lock(
        descriptor: int,
        *,
        lock_path: Path,
) -> None:
    command = getattr(fcntl, "F_OFD_SETLK", None)
    if command is None:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime database identity locking is unavailable"
        )
    try:
        fcntl.fcntl(
            descriptor,
            command,
            _database_identity_lock_payload(),
        )
    except OSError as exc:
        if exc.errno == errno.ENOLCK:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime database identity lock is unavailable "
                "(kernel lock table full)"
            ) from exc
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime database identity lock is unavailable"
            ) from exc
        owner = mediaforce_runtime_lock_owner(lock_path)
        owner_detail = f" ({owner})" if owner else ""
        raise MediaforceRuntimeBusyError(
            f"Mediaforce runtime is already active{owner_detail}"
        ) from exc


@dataclass(frozen=True, slots=True)
class _RuntimeDirectFileLock:
    label: str
    path: Path
    device: int | None
    inode: int | None
    create_if_missing: bool
    database_identity_lock: bool


@dataclass(frozen=True, slots=True)
class LegacySQLiteMigrationSource:
    path: Path
    _file_descriptor: int
    _directory_descriptor: int
    _file_snapshot: tuple[int, int, int, int, int, int]
    _directory_identity: tuple[int, int]
    _mutation_guard: _LegacySQLiteMutationGuard

    def assert_parent_stable(self) -> None:
        try:
            directory_descriptor_info = os.fstat(self._directory_descriptor)
            parent_info = self.path.parent.lstat()
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source parent changed during migration"
            ) from exc
        if (
            not stat.S_ISDIR(directory_descriptor_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or (
                directory_descriptor_info.st_dev,
                directory_descriptor_info.st_ino,
            )
            != self._directory_identity
            or (parent_info.st_dev, parent_info.st_ino)
            != self._directory_identity
        ):
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source parent changed during migration"
            )

    def assert_stable(self) -> None:
        try:
            self._mutation_guard.assert_quiet()
            descriptor_info = os.fstat(self._file_descriptor)
            directory_descriptor_info = os.fstat(self._directory_descriptor)
            relative_info = os.stat(
                self.path.name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
            path_info = self.path.lstat()
            parent_info = self.path.parent.lstat()
        except (FileIntegrityError, OSError) as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity changed during migration"
            ) from exc
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(relative_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or not stat.S_ISDIR(directory_descriptor_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(relative_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or _legacy_sqlite_source_snapshot(descriptor_info) != self._file_snapshot
            or _legacy_sqlite_source_snapshot(relative_info) != self._file_snapshot
            or _legacy_sqlite_source_snapshot(path_info) != self._file_snapshot
            or (directory_descriptor_info.st_dev, directory_descriptor_info.st_ino)
            != self._directory_identity
            or (parent_info.st_dev, parent_info.st_ino) != self._directory_identity
        ):
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity changed during migration"
            )
        try:
            self._mutation_guard.assert_quiet()
        except FileIntegrityError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity changed during migration"
            ) from exc

    def sqlite_uri(self) -> str:
        self.assert_stable()
        try:
            directory_info = os.fstat(self._directory_descriptor)
            pinned_path = _legacy_sqlite_connection_path_for_directory_descriptor(
                self._directory_descriptor,
                directory_info=directory_info,
                filename=self.path.name,
            )
            pinned_info = pinned_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity changed during migration"
            ) from exc
        if _legacy_sqlite_source_snapshot(pinned_info) != self._file_snapshot:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity changed during migration"
            )
        self.assert_stable()
        return f"{pinned_path.as_uri()}?mode=rw"

    def source_snapshot(self) -> tuple[int, int, int, int, int, int]:
        self.assert_stable()
        return self._file_snapshot

    def bind_sqlite_sidecars(self) -> None:
        try:
            self._mutation_guard.bind_sqlite_sidecars()
        except FileIntegrityError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite sidecar identity changed during migration"
            ) from exc
        self.assert_stable()

    def prepare_sqlite_sidecars_for_write_gate(self) -> None:
        try:
            self._mutation_guard.prepare_sqlite_sidecars_for_write_gate()
        except FileIntegrityError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite sidecar identity changed before migration"
            ) from exc
        self.assert_stable()

    def assert_connection_bound(self, connection: object) -> None:
        self.assert_stable()
        try:
            rows = connection.execute("PRAGMA database_list").fetchall()
            main_paths = [
                str(row[2])
                for row in rows
                if len(row) >= 3 and str(row[1]) == "main" and str(row[2])
            ]
            if len(main_paths) != 1:
                raise OSError("SQLite main database path is unavailable")
            connection_info = Path(main_paths[0]).stat(follow_symlinks=False)
        except (AttributeError, IndexError, OSError, TypeError) as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite connection identity is unavailable"
            ) from exc
        if _legacy_sqlite_source_snapshot(connection_info) != self._file_snapshot:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite connection opened an unexpected database"
            )
        self.assert_stable()

    def cleanup_snapshot(self) -> dict[str, dict[str, object]]:
        self.assert_stable()
        try:
            snapshot = self._mutation_guard.cleanup_snapshot()
        except FileIntegrityError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite sidecar identity changed before cleanup"
            ) from exc
        self.assert_stable()
        return snapshot

    def cleanup_sha256(self) -> str:
        self.assert_stable()
        digest = _legacy_sqlite_descriptor_sha256(self._file_descriptor)
        self.assert_stable()
        return digest

    def discard_after_publish(
            self,
            *,
            before_remove: Callable[[], None],
            expected_main_sha256: str,
            expected_sidecar_snapshots: dict[str, dict[str, object]],
    ) -> None:
        sidecar_snapshots = _legacy_sqlite_sidecar_cleanup_snapshots(
            expected_sidecar_snapshots
        )
        self.assert_stable()
        before_remove()
        self.assert_stable()
        try:
            self._mutation_guard.retire_main(
                expected_snapshot=self._file_snapshot,
                expected_sha256=expected_main_sha256,
            )
        except (FileIntegrityError, OSError) as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source cleanup failed after publication"
            ) from exc
        for suffix in ("-wal", "-shm", "-journal"):
            before_remove()
            self.assert_parent_stable()
            try:
                self._mutation_guard.retire_sidecar(
                    suffix,
                    expected_snapshot=sidecar_snapshots[suffix],
                )
            except (FileIntegrityError, OSError) as exc:
                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite source cleanup failed after publication"
                ) from exc
        self.assert_cleanup_complete()

    def assert_cleanup_complete(self) -> None:
        self.assert_parent_stable()
        try:
            self._mutation_guard.assert_cleanup_complete()
        except (FileIntegrityError, OSError) as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source cleanup is incomplete"
            ) from exc
        self.assert_parent_stable()


@dataclass(frozen=True, slots=True)
class _LegacySQLiteSidecarBinding:
    descriptor: int
    identity: tuple[int, int, int]
    created_snapshot: tuple[int, int, int, int, int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class _LegacySQLiteCleanupSnapshot:
    stat: tuple[int, int, int, int, int, int, int, int]
    sha256: str


@dataclass(frozen=True, slots=True)
class _LegacySQLiteRetiredEntry:
    quarantine_name: str
    expected_snapshot: tuple[int, ...]
    expected_sha256: str
    descriptor: int


class _LegacySQLiteMutationGuard:
    _INOTIFY_EVENT = struct.Struct("iIII")
    _IN_MODIFY = 0x00000002
    _IN_ATTRIB = 0x00000004
    _IN_MOVED_FROM = 0x00000040
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800
    _IN_Q_OVERFLOW = 0x00004000

    def __init__(
            self,
            *,
            path: Path,
            file_descriptor: int,
            directory_descriptor: int,
    ) -> None:
        self._path = path
        self._file_descriptor = file_descriptor
        self._directory_descriptor = directory_descriptor
        self._darwin_guard: MacOSFileIntegrityGuard | None = None
        self._darwin_directory_watcher = None
        self._inotify_descriptor = -1
        self._inotify_add_watch = None
        self._file_watch = -1
        self._directory_watch = -1
        self._sidecar_watches: set[int] = set()
        self._sidecar_names = tuple(
            f"{path.name}{suffix}"
            for suffix in ("-wal", "-shm", "-journal")
        )
        self._watched_directory_names = {
            path.name,
            *self._sidecar_names,
        }
        self._sidecar_bindings: dict[
            str,
            _LegacySQLiteSidecarBinding | None,
        ] = {}
        self._retired_entries: dict[str, _LegacySQLiteRetiredEntry] = {}
        self._sidecars_prepared = False
        self._sidecars_bound = False
        self._cleanup_started = False
        self._cleanup_sealed = False
        self._violated = False
        try:
            self._prepare_sidecar_bindings_before_monitoring()
            self._initialize_monitoring(file_descriptor)
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup_error:
                exc.add_note(
                    "Legacy SQLite sidecar reservation rollback also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def _initialize_monitoring(self, file_descriptor: int) -> None:
        if sys.platform == "darwin":
            self._darwin_guard = MacOSFileIntegrityGuard(
                path=self._path,
                descriptor=file_descriptor,
                require_single_link=True,
            )
            self._darwin_directory_watcher = select_module.kqueue()
            directory_flags = (
                select_module.KQ_NOTE_WRITE
                | select_module.KQ_NOTE_DELETE
                | select_module.KQ_NOTE_RENAME
                | select_module.KQ_NOTE_LINK
                | select_module.KQ_NOTE_REVOKE
                | select_module.KQ_NOTE_ATTRIB
            )
            self._darwin_directory_watcher.control(
                [
                    select_module.kevent(
                        self._directory_descriptor,
                        filter=select_module.KQ_FILTER_VNODE,
                        flags=(
                            select_module.KQ_EV_ADD
                            | select_module.KQ_EV_CLEAR
                        ),
                        fflags=directory_flags,
                    )
                ],
                0,
                0,
            )
            for name, binding in self._sidecar_bindings.items():
                if binding is None:
                    raise FileIntegrityError(
                        "legacy SQLite sidecar binding is unavailable"
                    )
                self._register_sidecar_watch(
                    name,
                    descriptor=binding.descriptor,
                    watch_writes=False,
                )
            self.assert_quiet()
            return
        if not sys.platform.startswith("linux") or not Path("/proc/self/fd").is_dir():
            raise FileIntegrityError(
                "legacy SQLite source mutation monitoring is unavailable"
            )
        libc = ctypes.CDLL(None, use_errno=True)
        inotify_init1 = getattr(libc, "inotify_init1", None)
        inotify_add_watch = getattr(libc, "inotify_add_watch", None)
        if inotify_init1 is None or inotify_add_watch is None:
            raise FileIntegrityError(
                "legacy SQLite source mutation monitoring is unavailable"
            )
        inotify_init1.argtypes = (ctypes.c_int,)
        inotify_init1.restype = ctypes.c_int
        inotify_add_watch.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        inotify_add_watch.restype = ctypes.c_int
        self._inotify_add_watch = inotify_add_watch
        descriptor = inotify_init1(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise FileIntegrityError(
                "legacy SQLite source mutation monitoring is unavailable"
            ) from OSError(error_number, os.strerror(error_number))
        self._inotify_descriptor = descriptor
        file_mask = (
            self._IN_MODIFY
            | self._IN_ATTRIB
            | self._IN_DELETE_SELF
            | self._IN_MOVE_SELF
        )
        directory_mask = (
            self._IN_MOVED_FROM
            | self._IN_MOVED_TO
            | self._IN_CREATE
            | self._IN_DELETE
            | self._IN_DELETE_SELF
            | self._IN_MOVE_SELF
        )
        self._file_watch = inotify_add_watch(
            descriptor,
            os.fsencode(f"/proc/self/fd/{file_descriptor}"),
            file_mask,
        )
        self._directory_watch = inotify_add_watch(
            descriptor,
            os.fsencode(f"/proc/self/fd/{self._directory_descriptor}"),
            directory_mask,
        )
        if self._file_watch < 0 or self._directory_watch < 0:
            error_number = ctypes.get_errno()
            self.close()
            raise FileIntegrityError(
                "legacy SQLite source mutation monitoring is unavailable"
            ) from OSError(error_number, os.strerror(error_number))
        for name, binding in self._sidecar_bindings.items():
            if binding is None:
                raise FileIntegrityError(
                    "legacy SQLite sidecar binding is unavailable"
                )
            self._register_sidecar_watch(
                name,
                descriptor=binding.descriptor,
                watch_writes=False,
            )
        self.assert_quiet()

    def prepare_sqlite_sidecars_for_write_gate(self) -> None:
        if not self._sidecars_prepared:
            raise FileIntegrityError(
                "legacy SQLite sidecars were not bound before monitoring"
            )
        self.assert_quiet()

    def bind_sqlite_sidecars(self) -> None:
        if not self._sidecars_prepared:
            raise FileIntegrityError(
                "legacy SQLite sidecars were not prepared before write-gate open"
            )
        if self._sidecars_bound:
            self.assert_quiet()
            return
        file_changed, directory_changed, directory_names = (
            self._drain_mutation_events()
        )
        if file_changed:
            self._violated = True
        for name, binding in self._sidecar_bindings.items():
            if binding is None or not self._sidecar_binding_matches(name, binding):
                self._violated = True
                continue
            self._register_sidecar_watch(
                name,
                descriptor=binding.descriptor,
                watch_writes=True,
            )
        later_file_changed, later_directory_changed, later_directory_names = (
            self._drain_mutation_events()
        )
        if later_file_changed:
            self._violated = True
        directory_changed = directory_changed or later_directory_changed
        directory_names.update(later_directory_names)
        if directory_changed or directory_names:
            self._violated = True
        self._sidecars_bound = True
        if not self._sidecar_bindings_match():
            self._violated = True
        if self._violated:
            raise FileIntegrityError("legacy SQLite source changed")

    def assert_quiet(self) -> None:
        if self._darwin_guard is not None:
            self._darwin_guard.assert_quiet()
        file_changed, directory_changed, directory_names = (
            self._drain_mutation_events()
        )
        if file_changed or directory_changed or directory_names:
            self._violated = True
        if not self._sidecar_bindings_match():
            self._violated = True
        if self._violated:
            raise FileIntegrityError("legacy SQLite source changed")

    def cleanup_snapshot(self) -> dict[str, dict[str, object]]:
        if not self._sidecars_bound:
            raise FileIntegrityError(
                "legacy SQLite sidecars were not bound for cleanup"
            )
        self.assert_quiet()
        snapshot: dict[str, dict[str, object]] = {}
        for suffix, name in zip(
                ("-wal", "-shm", "-journal"),
                self._sidecar_names,
                strict=True,
        ):
            binding = self._sidecar_bindings.get(name)
            if binding is None or not self._sidecar_binding_matches(name, binding):
                raise FileIntegrityError(
                    "legacy SQLite sidecar identity changed before cleanup"
                )
            info = os.fstat(binding.descriptor)
            snapshot[suffix] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "link_count": info.st_nlink,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "uid": info.st_uid,
                "mode": stat.S_IMODE(info.st_mode),
                "guard_created": binding.created_snapshot is not None,
                "sha256": _legacy_sqlite_descriptor_sha256(
                    binding.descriptor
                ),
            }
        self.assert_quiet()
        return snapshot

    def assert_sidecar_bound(self, suffix: str) -> None:
        if suffix not in {"-wal", "-shm", "-journal"}:
            raise FileIntegrityError("legacy SQLite sidecar suffix is invalid")
        name = f"{self._path.name}{suffix}"
        binding = self._sidecar_bindings.get(name)
        if binding is None or not self._sidecar_binding_matches(name, binding):
            raise FileIntegrityError(
                "legacy SQLite sidecar identity changed during cleanup"
            )

    def retire_sidecar(
            self,
            suffix: str,
            *,
            expected_snapshot: _LegacySQLiteCleanupSnapshot,
    ) -> None:
        self.assert_sidecar_bound(suffix)
        name = f"{self._path.name}{suffix}"
        binding = self._sidecar_bindings.get(name)
        if binding is None:
            raise FileIntegrityError(
                "legacy SQLite sidecar binding is unavailable"
            )
        self._retire_entry(
            name=name,
            expected_snapshot=expected_snapshot.stat,
            expected_sha256=expected_snapshot.sha256,
            descriptor=binding.descriptor,
        )

    def retire_main(
            self,
            *,
            expected_snapshot: tuple[int, ...],
            expected_sha256: str,
    ) -> None:
        self._cleanup_started = True
        self._retire_entry(
            name=self._path.name,
            expected_snapshot=expected_snapshot,
            expected_sha256=expected_sha256,
            descriptor=self._file_descriptor,
        )

    def _retire_entry(
            self,
            *,
            name: str,
            expected_snapshot: tuple[int, ...],
            expected_sha256: str,
            descriptor: int,
    ) -> None:
        if name in self._retired_entries:
            raise FileIntegrityError(
                "legacy SQLite cleanup entry was retired more than once"
            )
        quarantine_name = _retire_bound_legacy_sqlite_entry(
            directory_descriptor=self._directory_descriptor,
            name=name,
            expected_snapshot=expected_snapshot,
            expected_sha256=expected_sha256,
            descriptor=descriptor,
        )
        self._retired_entries[name] = _LegacySQLiteRetiredEntry(
            quarantine_name=quarantine_name,
            expected_snapshot=expected_snapshot,
            expected_sha256=expected_sha256,
            descriptor=descriptor,
        )

    def assert_cleanup_complete(self) -> None:
        if self._cleanup_sealed:
            file_changed, directory_changed, directory_names = (
                self._drain_mutation_events(
                    collect_all_directory_names=True,
                )
            )
            if self._cleanup_events_are_relevant(
                    file_changed=file_changed,
                    directory_changed=directory_changed,
                    directory_names=directory_names,
            ):
                raise FileIntegrityError(
                    "legacy SQLite source cleanup namespace changed"
                )
        else:
            self._drain_mutation_events()
        expected_artifacts = self._retired_quarantine_names()
        if self._cleanup_namespace_entries() != expected_artifacts:
            raise FileIntegrityError(
                "legacy SQLite source cleanup namespace is not exact"
            )
        if not self._retired_entries_match():
            raise FileIntegrityError(
                "legacy SQLite source cleanup quarantine identity changed"
            )
        file_changed, directory_changed, directory_names = (
            self._drain_mutation_events(
                collect_all_directory_names=True,
            )
        )
        if self._cleanup_events_are_relevant(
                file_changed=file_changed,
                directory_changed=directory_changed,
                directory_names=directory_names,
        ):
            raise FileIntegrityError(
                "legacy SQLite source cleanup namespace changed"
            )
        if self._cleanup_namespace_entries() != expected_artifacts:
            raise FileIntegrityError(
                "legacy SQLite source cleanup namespace is not exact"
            )
        if not self._retired_entries_match():
            raise FileIntegrityError(
                "legacy SQLite source cleanup quarantine identity changed"
            )
        self._cleanup_sealed = True

    def _retired_quarantine_names(self) -> set[str]:
        if set(self._retired_entries) != self._watched_directory_names:
            raise FileIntegrityError(
                "legacy SQLite source cleanup did not retire every entry"
            )
        names = {
            entry.quarantine_name
            for entry in self._retired_entries.values()
        }
        if len(names) != len(self._retired_entries):
            raise FileIntegrityError(
                "legacy SQLite cleanup quarantines are not unique"
            )
        return names

    def _retired_entries_match(self) -> bool:
        for entry in self._retired_entries.values():
            try:
                descriptor_info = os.fstat(entry.descriptor)
                quarantine_info = os.stat(
                    entry.quarantine_name,
                    dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                return False
            if (
                not _legacy_sqlite_retired_snapshot_matches(
                    descriptor_info,
                    entry.expected_snapshot,
                )
                or not _legacy_sqlite_retired_snapshot_matches(
                    quarantine_info,
                    entry.expected_snapshot,
                )
                or (descriptor_info.st_dev, descriptor_info.st_ino)
                != (quarantine_info.st_dev, quarantine_info.st_ino)
                or _legacy_sqlite_descriptor_sha256(entry.descriptor)
                != entry.expected_sha256
            ):
                return False
        return True

    def _cleanup_namespace_entries(self) -> set[str]:
        names = set(os.listdir(self._directory_descriptor))
        live_names = {
            self._path.name,
            *self._sidecar_names,
        }
        quarantine_prefixes = tuple(
            f".{name}.mediaforce-retired-"
            for name in live_names
        )
        return {
            name
            for name in names
            if (
            name in live_names or name.startswith(quarantine_prefixes)
            )
        }

    def _cleanup_events_are_relevant(
            self,
            *,
            file_changed: bool,
            directory_changed: bool,
            directory_names: set[str],
    ) -> bool:
        if file_changed:
            return True
        live_names = {
            self._path.name,
            *self._sidecar_names,
        }
        quarantine_prefixes = tuple(
            f".{name}.mediaforce-retired-"
            for name in live_names
        )
        if any(
            name in live_names or name.startswith(quarantine_prefixes)
            for name in directory_names
        ):
            return True
        return (
            directory_changed
            and not directory_names
            and (
                sys.platform.startswith("linux")
                or (sys.platform == "darwin" and self._cleanup_sealed)
            )
        )

    def _drain_mutation_events(
            self,
            *,
            collect_all_directory_names: bool = False,
    ) -> tuple[bool, bool, set[str]]:
        file_changed = False
        directory_changed = False
        directory_names: set[str] = set()
        if self._darwin_guard is not None:
            watcher = self._darwin_directory_watcher
            if watcher is None:
                return True, True, directory_names
            try:
                for event in watcher.control(None, 32, 0):
                    if int(event.ident) == self._directory_descriptor:
                        directory_changed = True
                    else:
                        file_changed = True
            except OSError:
                return True, True, directory_names
            return file_changed, directory_changed, directory_names
        descriptor = self._inotify_descriptor
        if descriptor < 0:
            return True, True, directory_names
        while True:
            try:
                data = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                break
            except OSError:
                return True, True, directory_names
            if not data:
                return True, True, directory_names
            offset = 0
            while offset + self._INOTIFY_EVENT.size <= len(data):
                watch, mask, _cookie, name_length = (
                    self._INOTIFY_EVENT.unpack_from(data, offset)
                )
                offset += self._INOTIFY_EVENT.size
                name_bytes = data[offset:offset + name_length]
                offset += name_length
                name = os.fsdecode(name_bytes.split(b"\0", 1)[0])
                if mask & self._IN_Q_OVERFLOW:
                    file_changed = True
                elif watch == self._file_watch or watch in self._sidecar_watches:
                    file_changed = True
                elif watch == self._directory_watch and (
                    collect_all_directory_names
                    or not name
                    or name in self._watched_directory_names
                ):
                    directory_changed = True
                    directory_names.add(name)
        return file_changed, directory_changed, directory_names

    def _open_sidecar_binding(
            self,
            name: str,
            *,
            created: bool = False,
    ) -> _LegacySQLiteSidecarBinding | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                name,
                flags,
                dir_fd=self._directory_descriptor,
            )
        except FileNotFoundError:
            try:
                os.stat(
                    name,
                    dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise FileIntegrityError(
                    "legacy SQLite sidecar identity is unavailable"
                ) from exc
            raise FileIntegrityError("legacy SQLite sidecar identity changed")
        except OSError as exc:
            raise FileIntegrityError(
                "legacy SQLite sidecar identity is unavailable"
            ) from exc
        try:
            return self._sidecar_binding_from_descriptor(
                name,
                descriptor=descriptor,
                created=created,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _sidecar_binding_from_descriptor(
            self,
            name: str,
            *,
            descriptor: int,
            created: bool,
    ) -> _LegacySQLiteSidecarBinding:
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            name,
            dir_fd=self._directory_descriptor,
            follow_symlinks=False,
        )
        snapshot = (
            descriptor_info.st_dev,
            descriptor_info.st_ino,
            descriptor_info.st_nlink,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or path_info.st_uid != os.getuid()
            or descriptor_info.st_nlink != 1
            or path_info.st_nlink != 1
            or (path_info.st_dev, path_info.st_ino, path_info.st_nlink)
            != snapshot
        ):
            raise FileIntegrityError(
                "legacy SQLite sidecar identity is unsafe"
            )
        return _LegacySQLiteSidecarBinding(
            descriptor=descriptor,
            identity=snapshot,
            created_snapshot=(
                _legacy_sqlite_sidecar_reservation_snapshot(descriptor_info)
                if created
                else None
            ),
        )

    def _prepare_sidecar_bindings_before_monitoring(self) -> None:
        created = False
        for name in self._sidecar_names:
            binding = self._open_sidecar_binding(name)
            if binding is None:
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                )
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(
                        name,
                        flags,
                        0o600,
                        dir_fd=self._directory_descriptor,
                    )
                except FileExistsError:
                    binding = self._open_sidecar_binding(name)
                    if binding is None:
                        raise FileIntegrityError(
                            "legacy SQLite sidecar identity changed"
                        )
                except OSError as exc:
                    raise FileIntegrityError(
                        "legacy SQLite sidecar could not be reserved"
                    ) from exc
                else:
                    try:
                        os.fsync(descriptor)
                        binding = self._sidecar_binding_from_descriptor(
                            name,
                            descriptor=descriptor,
                            created=True,
                        )
                    except BaseException:
                        os.close(descriptor)
                        raise
                    created = True
            self._sidecar_bindings[name] = binding
        if created:
            try:
                os.fsync(self._directory_descriptor)
            except OSError as exc:
                raise FileIntegrityError(
                    "legacy SQLite sidecar reservation could not be synchronized"
                ) from exc
        self._sidecars_prepared = True

    def _register_sidecar_watch(
            self,
            name: str,
            *,
            descriptor: int,
            watch_writes: bool,
    ) -> None:
        if self._darwin_directory_watcher is not None:
            flags = (
                select_module.KQ_NOTE_DELETE
                | select_module.KQ_NOTE_RENAME
                | select_module.KQ_NOTE_LINK
                | select_module.KQ_NOTE_REVOKE
            )
            if watch_writes:
                flags |= select_module.KQ_NOTE_ATTRIB
                if name.endswith("-wal"):
                    flags |= (
                        select_module.KQ_NOTE_WRITE
                        | select_module.KQ_NOTE_EXTEND
                    )
            self._darwin_directory_watcher.control(
                [
                    select_module.kevent(
                        descriptor,
                        filter=select_module.KQ_FILTER_VNODE,
                        flags=(
                            select_module.KQ_EV_ADD
                            | select_module.KQ_EV_CLEAR
                        ),
                        fflags=flags,
                    )
                ],
                0,
                0,
            )
            return
        inotify_add_watch = self._inotify_add_watch
        if inotify_add_watch is None or self._inotify_descriptor < 0:
            raise FileIntegrityError(
                "legacy SQLite sidecar mutation monitoring is unavailable"
            )
        mask = self._IN_ATTRIB | self._IN_DELETE_SELF | self._IN_MOVE_SELF
        if name.endswith("-wal") and watch_writes:
            mask |= self._IN_MODIFY
        watch = inotify_add_watch(
            self._inotify_descriptor,
            os.fsencode(f"/proc/self/fd/{descriptor}"),
            mask,
        )
        if watch < 0:
            error_number = ctypes.get_errno()
            raise FileIntegrityError(
                "legacy SQLite sidecar mutation monitoring is unavailable"
            ) from OSError(error_number, os.strerror(error_number))
        self._sidecar_watches.add(watch)

    def _sidecar_bindings_match(self) -> bool:
        if not self._sidecars_prepared:
            return True
        for name, binding in self._sidecar_bindings.items():
            if binding is None:
                if not self._sidecars_bound:
                    continue
                try:
                    os.stat(
                        name,
                        dir_fd=self._directory_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                return False
            if not self._sidecar_binding_matches(name, binding):
                return False
        return True

    def _sidecar_binding_matches(
            self,
            name: str,
            binding: _LegacySQLiteSidecarBinding,
    ) -> bool:
        try:
            descriptor_info = os.fstat(binding.descriptor)
            path_info = os.stat(
                name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            return False
        return (
            stat.S_ISREG(descriptor_info.st_mode)
            and stat.S_ISREG(path_info.st_mode)
            and (
                descriptor_info.st_dev,
                descriptor_info.st_ino,
                descriptor_info.st_nlink,
            )
            == binding.identity
            and (
                path_info.st_dev,
                path_info.st_ino,
                path_info.st_nlink,
            )
            == binding.identity
        )

    def _discard_created_sidecar_reservations(self) -> None:
        for name, binding in self._sidecar_bindings.items():
            if binding is None or binding.created_snapshot is None:
                continue
            try:
                descriptor_info = os.fstat(binding.descriptor)
                path_info = os.stat(
                    name,
                    dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise FileIntegrityError(
                    "legacy SQLite sidecar reservation cleanup is unavailable"
                ) from exc
            if (
                _legacy_sqlite_sidecar_reservation_snapshot(descriptor_info)
                != binding.created_snapshot
                or _legacy_sqlite_sidecar_reservation_snapshot(path_info)
                != binding.created_snapshot
            ):
                continue
            quarantine_name = (
                None
                if self._cleanup_started
                else _legacy_sqlite_sidecar_reservation_quarantine_name(
                    name,
                    expected_snapshot=(
                        descriptor_info.st_dev,
                        descriptor_info.st_ino,
                        descriptor_info.st_size,
                        descriptor_info.st_mtime_ns,
                    ),
                )
            )
            try:
                _retire_bound_legacy_sqlite_entry(
                    directory_descriptor=self._directory_descriptor,
                    name=name,
                    expected_snapshot=(
                        descriptor_info.st_dev,
                        descriptor_info.st_ino,
                        descriptor_info.st_size,
                        descriptor_info.st_mtime_ns,
                        descriptor_info.st_ctime_ns,
                        descriptor_info.st_nlink,
                        descriptor_info.st_uid,
                        stat.S_IMODE(descriptor_info.st_mode),
                    ),
                    expected_sha256=_legacy_sqlite_descriptor_sha256(
                        binding.descriptor
                    ),
                    descriptor=binding.descriptor,
                    quarantine_name=quarantine_name,
                )
            except (FileIntegrityError, OSError) as exc:
                raise FileIntegrityError(
                    "legacy SQLite sidecar reservation cleanup failed"
                ) from exc

    def close(self) -> None:
        if self._darwin_directory_watcher is not None:
            self._darwin_directory_watcher.close()
            self._darwin_directory_watcher = None
        if self._darwin_guard is not None:
            self._darwin_guard.close()
            self._darwin_guard = None
        if self._inotify_descriptor >= 0:
            os.close(self._inotify_descriptor)
            self._inotify_descriptor = -1
        cleanup_error: BaseException | None = None
        try:
            self._discard_created_sidecar_reservations()
        except BaseException as exc:
            cleanup_error = exc
        finally:
            for binding in self._sidecar_bindings.values():
                if binding is not None:
                    os.close(binding.descriptor)
            self._sidecar_bindings.clear()
        if cleanup_error is not None:
            raise cleanup_error


@dataclass(frozen=True, slots=True)
class _RuntimePathNamespace:
    key: str
    path: Path
    local_reservation_required: bool


@dataclass(frozen=True, slots=True)
class _RuntimeNamespaceState:
    keys: tuple[str, ...]
    paths: tuple[_RuntimePathNamespace, ...]
    direct_file_locks: tuple[_RuntimeDirectFileLock, ...]
    database_path_key: str | None = None
    database_identity: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeReservationLocation:
    directory: Path
    key: str
    name: str


def _runtime_namespace_state(
        config: MediaforceConfig,
        lock_path: Path,
) -> _RuntimeNamespaceState:
    lock_key = f"lock:{filesystem_collision_key(lock_path)}"
    keys = {lock_key}
    paths = [
        _RuntimePathNamespace(
            key=lock_key,
            path=lock_path,
            local_reservation_required=True,
        )
    ]
    direct_file_locks: list[_RuntimeDirectFileLock] = []
    database_path_key: str | None = None
    database_identity: tuple[int, int] | None = None
    for name in ("config_path", "db_path"):
        value = getattr(config.paths, name, None)
        if value is None:
            continue
        try:
            resolved = Path(value).expanduser().resolve()
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime {name} namespace is unavailable"
            ) from exc
        key = f"{name}:{filesystem_collision_key(resolved)}"
        if name == "db_path":
            database_path_key = key
        keys.add(key)
        try:
            identity = resolved.stat()
        except FileNotFoundError:
            identity = None
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime {name} identity is unavailable"
            ) from exc
        if identity is not None:
            keys.add(f"file:{identity.st_dev}:{identity.st_ino}")
            if name == "db_path":
                database_identity = (identity.st_dev, identity.st_ino)
        if name == "config_path" and identity is not None:
            direct_file_locks.append(
                _RuntimeDirectFileLock(
                    label=name,
                    path=resolved,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                    create_if_missing=False,
                    database_identity_lock=False,
                )
            )
        elif name == "db_path" and identity is not None:
            direct_file_locks.append(
                _RuntimeDirectFileLock(
                    label=name,
                    path=resolved,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                    create_if_missing=False,
                    database_identity_lock=True,
                )
            )
        paths.append(
            _RuntimePathNamespace(
                key=key,
                path=resolved,
                local_reservation_required=name == "db_path",
            )
        )
    return _RuntimeNamespaceState(
        keys=tuple(sorted(keys)),
        paths=tuple(sorted(paths, key=lambda item: item.key)),
        direct_file_locks=tuple(
            sorted(
                direct_file_locks,
                key=lambda item: (
                    item.path.as_posix(),
                    item.label,
                    item.device if item.device is not None else -1,
                    item.inode if item.inode is not None else -1,
                ),
            )
        ),
        database_path_key=database_path_key,
        database_identity=database_identity,
    )


def _runtime_namespace_keys(
        config: MediaforceConfig,
        lock_path: Path,
) -> tuple[str, ...]:
    return _runtime_namespace_state(config, lock_path).keys


def _runtime_namespace_lock_name(key: str) -> str:
    payload = f"{os.getuid()}:{key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_namespace_lock_path(directory: Path, key: str) -> Path:
    return (
        directory
        / _runtime_namespace_lock_name(key)
        / _RUNTIME_NAMESPACE_LOCK_FILE_NAME
    )


def _runtime_namespace_path_keys(keys: tuple[str, ...]) -> frozenset[str]:
    return frozenset(key for key in keys if not key.startswith("file:"))


def _runtime_namespace_reservation_locations(
        config: MediaforceConfig,
        namespace_state: _RuntimeNamespaceState,
) -> tuple[_RuntimeReservationLocation, ...]:
    try:
        shared_directory = mediaforce_runtime_reservation_dir(config)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime reservation directory is unavailable"
        ) from exc
    locations: dict[tuple[str, str], _RuntimeReservationLocation] = {}

    def add_location(directory: Path, key: str) -> None:
        try:
            resolved_directory = directory.expanduser().resolve()
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime reservation directory is unavailable"
            ) from exc
        name = _runtime_namespace_lock_name(key)
        location_key = (filesystem_collision_key(resolved_directory), name)
        existing = locations.get(location_key)
        if existing is not None and existing.key != key:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace reservation identity is invalid"
            )
        locations.setdefault(
            location_key,
            _RuntimeReservationLocation(
                directory=resolved_directory,
                key=key,
                name=name,
            ),
        )

    for key in namespace_state.keys:
        add_location(shared_directory, key)
    for path_namespace in namespace_state.paths:
        if path_namespace.local_reservation_required:
            add_location(
                path_namespace.path.parent / _RUNTIME_NAMESPACE_LOCAL_DIRECTORY_NAME,
                path_namespace.key,
            )
    return tuple(
        locations[key]
        for key in sorted(locations)
    )


@dataclass(frozen=True, slots=True)
class _RuntimeNamespaceLocks:
    descriptors: tuple[int, ...]

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)


def _directory_descriptor_matches_path(
        directory_path: Path,
        descriptor: int,
) -> bool:
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = directory_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(descriptor_info.st_mode)
        and stat.S_ISDIR(path_info.st_mode)
        and not stat.S_ISLNK(path_info.st_mode)
        and (descriptor_info.st_dev, descriptor_info.st_ino)
        == (path_info.st_dev, path_info.st_ino)
    )


def _open_owner_only_runtime_directory(directory_path: Path) -> int:
    try:
        directory_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime reservation directory is unavailable"
        ) from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(directory_path, flags)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime reservation directory is invalid"
        ) from exc
    try:
        descriptor_info = os.fstat(descriptor)
        if (
            not _directory_descriptor_matches_path(directory_path, descriptor)
            or descriptor_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) & 0o077
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime reservation directory is invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_namespace_key_directory(
        name: str,
        *,
        directory_descriptor: int,
) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=directory_descriptor)
    except FileExistsError:
        pass
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace reservation is unavailable"
        ) from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace reservation is invalid"
        ) from exc
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) & 0o077
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace reservation is invalid"
            )
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace reservation is invalid"
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_namespace_lock_file(
        *,
        directory_descriptor: int,
) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(
            _RUNTIME_NAMESPACE_LOCK_FILE_NAME,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace lock is unavailable"
        ) from exc
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            _RUNTIME_NAMESPACE_LOCK_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or descriptor_info.st_nlink != 1
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace lock identity is invalid"
            )
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace lock identity is invalid"
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_direct_file_lock(
        file_lock: _RuntimeDirectFileLock,
        *,
        lock_path: Path,
) -> int:
    descriptor, directory_descriptor = _open_runtime_direct_file_lock_with_parent(
        file_lock,
        lock_path=lock_path,
    )
    try:
        os.close(directory_descriptor)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _open_runtime_direct_file_lock_with_parent(
        file_lock: _RuntimeDirectFileLock,
        *,
        lock_path: Path,
) -> tuple[int, int]:
    if file_lock.create_if_missing:
        try:
            file_lock.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime {file_lock.label} identity is unavailable"
            ) from exc
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        directory_descriptor = os.open(file_lock.path.parent, directory_flags)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            f"Mediaforce runtime {file_lock.label} identity is unavailable"
        ) from exc
    descriptor = -1
    try:
        if not _directory_descriptor_matches_path(
            file_lock.path.parent,
            directory_descriptor,
        ):
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime {file_lock.label} identity is invalid"
            )
        if file_lock.database_identity_lock:
            file_flags = os.O_RDWR
            if file_lock.create_if_missing:
                file_flags |= os.O_CREAT
        else:
            file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            file_flags |= os.O_CLOEXEC
        descriptor = os.open(
            file_lock.path.name,
            file_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        if file_lock.create_if_missing:
            os.fchmod(descriptor, 0o600)
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            file_lock.path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime {file_lock.label} identity is invalid"
            )
        if file_lock.device is not None and file_lock.inode is not None:
            expected_identity = (file_lock.device, file_lock.inode)
            if (descriptor_info.st_dev, descriptor_info.st_ino) != expected_identity:
                raise MediaforceRuntimeBusyError(
                    f"Mediaforce runtime {file_lock.label} identity is invalid"
                )
        if file_lock.database_identity_lock:
            _acquire_database_identity_lock(
                descriptor,
                lock_path=lock_path,
            )
        return descriptor, directory_descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)
        raise MediaforceRuntimeBusyError(
            f"Mediaforce runtime {file_lock.label} identity is unavailable"
        ) from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)
        raise


def _legacy_sqlite_source_snapshot(
        source_info: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        source_info.st_dev,
        source_info.st_ino,
        source_info.st_size,
        source_info.st_mtime_ns,
        source_info.st_ctime_ns,
        source_info.st_nlink,
    )


def _legacy_sqlite_descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return f"sha256:{digest.hexdigest()}"


def _legacy_sqlite_sha256_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == len("sha256:") + 64
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _legacy_sqlite_sidecar_cleanup_snapshots(
        value: object,
) -> dict[str, _LegacySQLiteCleanupSnapshot]:
    if not isinstance(value, dict) or set(value) != {
        "-wal",
        "-shm",
        "-journal",
    }:
        raise FileIntegrityError("legacy SQLite cleanup manifest is invalid")
    snapshots: dict[str, _LegacySQLiteCleanupSnapshot] = {}
    required = {
        "device",
        "inode",
        "size",
        "link_count",
        "mtime_ns",
        "ctime_ns",
        "uid",
        "mode",
        "guard_created",
        "sha256",
    }
    for suffix in ("-wal", "-shm", "-journal"):
        snapshot = value.get(suffix)
        if not isinstance(snapshot, dict) or set(snapshot) != required:
            raise FileIntegrityError(
                "legacy SQLite cleanup manifest is invalid"
            )
        values = tuple(
            snapshot.get(key)
            for key in (
                "device",
                "inode",
                "size",
                "mtime_ns",
                "ctime_ns",
                "link_count",
                "uid",
                "mode",
            )
        )
        if (
            any(type(item) is not int for item in values)
            or values[0] < 0
            or values[1] <= 0
            or values[2] < 0
            or values[5] != 1
            or values[6] != os.getuid()
            or not 0 <= values[7] <= 0o777
            or type(snapshot.get("guard_created")) is not bool
            or not _legacy_sqlite_sha256_is_valid(snapshot.get("sha256"))
        ):
            raise FileIntegrityError(
                "legacy SQLite cleanup manifest is invalid"
            )
        snapshots[suffix] = _LegacySQLiteCleanupSnapshot(
            stat=values,
            sha256=str(snapshot["sha256"]),
        )
    return snapshots


def _retire_bound_legacy_sqlite_entry(
        *,
        directory_descriptor: int,
        name: str,
        expected_snapshot: tuple[int, ...],
        expected_sha256: str,
        descriptor: int,
        quarantine_name: str | None = None,
) -> str:
    if len(expected_snapshot) not in {6, 8}:
        raise FileIntegrityError("legacy SQLite cleanup snapshot is invalid")
    if not _legacy_sqlite_sha256_is_valid(expected_sha256):
        raise FileIntegrityError("legacy SQLite cleanup digest is invalid")
    descriptor_info = os.fstat(descriptor)
    path_info = os.stat(
        name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not _legacy_sqlite_live_snapshot_matches(
            descriptor_info,
            expected_snapshot,
        )
        or not _legacy_sqlite_live_snapshot_matches(
            path_info,
            expected_snapshot,
        )
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (path_info.st_dev, path_info.st_ino)
        or _legacy_sqlite_descriptor_sha256(descriptor) != expected_sha256
    ):
        raise FileIntegrityError(
            "legacy SQLite cleanup source changed before claim"
        )
    if quarantine_name is None:
        quarantine_name = legacy_sqlite_migration_quarantine_name(
            name,
            device=expected_snapshot[0],
            inode=expected_snapshot[1],
            size=expected_snapshot[2],
            mtime_ns=expected_snapshot[3],
        )
    try:
        os.stat(
            quarantine_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise FileIntegrityError(
            "legacy SQLite cleanup quarantine already exists"
        )
    rename_exclusive(
        source_directory_descriptor=directory_descriptor,
        source_name=name,
        destination_directory_descriptor=directory_descriptor,
        destination_name=quarantine_name,
    )
    descriptor_info = os.fstat(descriptor)
    quarantine_info = os.stat(
        quarantine_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if (
        not _legacy_sqlite_retired_snapshot_matches(
            descriptor_info,
            expected_snapshot,
        )
        or not _legacy_sqlite_retired_snapshot_matches(
            quarantine_info,
            expected_snapshot,
        )
        or (descriptor_info.st_dev, descriptor_info.st_ino)
        != (quarantine_info.st_dev, quarantine_info.st_ino)
        or _legacy_sqlite_descriptor_sha256(descriptor) != expected_sha256
    ):
        raise FileIntegrityError(
            "legacy SQLite cleanup claimed an unexpected entry"
        )
    os.fsync(directory_descriptor)
    quarantine_info = os.stat(
        quarantine_name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if not _legacy_sqlite_retired_snapshot_matches(
            quarantine_info,
            expected_snapshot,
    ) or _legacy_sqlite_descriptor_sha256(descriptor) != expected_sha256:
        raise FileIntegrityError(
            "legacy SQLite cleanup quarantine identity changed"
        )
    try:
        os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return quarantine_name
    raise FileIntegrityError(
        "legacy SQLite cleanup entry reappeared"
    )


def _legacy_sqlite_sidecar_reservation_quarantine_name(
        name: str,
        *,
        expected_snapshot: tuple[int, int, int, int],
) -> str:
    device, inode, size, mtime_ns = expected_snapshot
    identity = f"{name}\0{device}\0{inode}\0{size}\0{mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    return f".{name}.mediaforce-reservation-{digest}"


def _legacy_sqlite_live_snapshot_matches(
        info: os.stat_result,
        expected_snapshot: tuple[int, ...],
) -> bool:
    if not stat.S_ISREG(info.st_mode):
        return False
    if (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    ) != expected_snapshot[:6]:
        return False
    return len(expected_snapshot) == 6 or (
        info.st_uid,
        stat.S_IMODE(info.st_mode),
    ) == expected_snapshot[6:8]


def _legacy_sqlite_retired_snapshot_matches(
        info: os.stat_result,
        expected_snapshot: tuple[int, ...],
) -> bool:
    if not stat.S_ISREG(info.st_mode):
        return False
    if (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
    ) != (
        expected_snapshot[0],
        expected_snapshot[1],
        expected_snapshot[2],
        expected_snapshot[3],
        expected_snapshot[5],
    ):
        return False
    return len(expected_snapshot) == 6 or (
        info.st_uid,
        stat.S_IMODE(info.st_mode),
    ) == (
        expected_snapshot[6],
        expected_snapshot[7],
    )


def _legacy_sqlite_sidecar_reservation_snapshot(
        sidecar_info: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        sidecar_info.st_dev,
        sidecar_info.st_ino,
        sidecar_info.st_nlink,
        sidecar_info.st_size,
        sidecar_info.st_mtime_ns,
        sidecar_info.st_ctime_ns,
        sidecar_info.st_uid,
        stat.S_IMODE(sidecar_info.st_mode),
    )


def _legacy_sqlite_connection_path_for_directory_descriptor(
        directory_descriptor: int,
        *,
        directory_info: os.stat_result,
        filename: str,
) -> Path:
    if sys.platform == "darwin":
        pinned_directory = (
            Path("/.vol")
            / str(directory_info.st_dev)
            / str(directory_info.st_ino)
        )
    elif sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        pinned_directory = Path("/proc/self/fd") / str(directory_descriptor)
    else:
        raise OSError(
            errno.ENOTSUP,
            "legacy SQLite pinned connection paths are unavailable",
        )
    pinned_parent_info = pinned_directory.stat()
    if (
        not stat.S_ISDIR(pinned_parent_info.st_mode)
        or (pinned_parent_info.st_dev, pinned_parent_info.st_ino)
        != (directory_info.st_dev, directory_info.st_ino)
    ):
        raise OSError(
            errno.ESTALE,
            "legacy SQLite pinned directory identity changed",
        )
    return pinned_directory / filename


@contextmanager
def exclusive_legacy_sqlite_migration_source(
        config: MediaforceConfig,
        source_path: Path,
) -> Iterator[LegacySQLiteMigrationSource]:
    assert_mediaforce_runtime_lock_held()
    source = Path(os.path.abspath(os.fspath(source_path.expanduser())))
    try:
        source_info = source.lstat()
        source_parent_info = source.parent.lstat()
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Legacy SQLite source identity is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(source_info.st_mode)
        or source_info.st_nlink != 1
        or not stat.S_ISDIR(source_parent_info.st_mode)
        or stat.S_ISLNK(source_parent_info.st_mode)
    ):
        raise MediaforceRuntimeBusyError(
            "Legacy SQLite source identity is unsafe"
        )
    descriptor = -1
    directory_descriptor = -1
    mutation_guard: _LegacySQLiteMutationGuard | None = None
    try:
        descriptor, directory_descriptor = _open_runtime_direct_file_lock_with_parent(
            _RuntimeDirectFileLock(
                label="legacy SQLite source",
                path=source,
                device=source_info.st_dev,
                inode=source_info.st_ino,
                create_if_missing=False,
                database_identity_lock=True,
            ),
            lock_path=mediaforce_runtime_lock_path(config),
        )
        try:
            mutation_guard = _LegacySQLiteMutationGuard(
                path=source,
                file_descriptor=descriptor,
                directory_descriptor=directory_descriptor,
            )
        except FileIntegrityError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source identity monitoring is unavailable"
            ) from exc
        locked_source = LegacySQLiteMigrationSource(
            path=source,
            _file_descriptor=descriptor,
            _directory_descriptor=directory_descriptor,
            _file_snapshot=_legacy_sqlite_source_snapshot(source_info),
            _directory_identity=(
                source_parent_info.st_dev,
                source_parent_info.st_ino,
            ),
            _mutation_guard=mutation_guard,
        )
        locked_source.assert_stable()
        yield locked_source
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        if mutation_guard is not None:
            try:
                mutation_guard.close()
            except BaseException as exc:
                cleanup_error = exc
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if cleanup_error is not None:
            if active_error is not None:
                active_error.add_note(
                    "Legacy SQLite sidecar reservation rollback also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            else:
                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite sidecar reservation rollback failed"
                ) from cleanup_error


def _acquire_runtime_namespace_locks(
        config: MediaforceConfig,
        namespace_state: _RuntimeNamespaceState,
        lock_path: Path,
) -> _RuntimeNamespaceLocks:
    locations = _runtime_namespace_reservation_locations(
        config,
        namespace_state,
    )
    locations_by_directory: dict[str, tuple[Path, set[str]]] = {}
    for location in locations:
        directory_key = filesystem_collision_key(location.directory)
        if directory_key not in locations_by_directory:
            locations_by_directory[directory_key] = (location.directory, set())
        locations_by_directory[directory_key][1].add(location.name)
    descriptors: list[int] = []
    try:
        for directory_key in sorted(locations_by_directory):
            directory, names = locations_by_directory[directory_key]
            directory_descriptor = _open_owner_only_runtime_directory(directory)
            descriptors.append(directory_descriptor)
            for name in sorted(names):
                key_directory_descriptor = _open_runtime_namespace_key_directory(
                    name,
                    directory_descriptor=directory_descriptor,
                )
                descriptors.append(key_directory_descriptor)
                _acquire_nonblocking_lock(
                    key_directory_descriptor,
                    lock_path=lock_path,
                )
                lock_descriptor = _open_runtime_namespace_lock_file(
                    directory_descriptor=key_directory_descriptor,
                )
                descriptors.append(lock_descriptor)
                _acquire_nonblocking_lock(
                    lock_descriptor,
                    lock_path=lock_path,
                )
        for file_lock in namespace_state.direct_file_locks:
            descriptor = _open_runtime_direct_file_lock(
                file_lock,
                lock_path=lock_path,
            )
            descriptors.append(descriptor)
            if not file_lock.database_identity_lock:
                _acquire_nonblocking_lock(
                    descriptor,
                    lock_path=lock_path,
                )
        return _RuntimeNamespaceLocks(descriptors=tuple(descriptors))
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _runtime_lock_parent_identity_matches(
        lock_path: Path,
        directory_descriptor: int,
) -> bool:
    return _directory_descriptor_matches_path(
        lock_path.parent,
        directory_descriptor,
    )


def _open_runtime_lock_file(
        lock_path: Path,
        *,
        directory_descriptor: int,
) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        lock_path.name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        os.fchmod(descriptor, 0o600)
        descriptor_info = os.fstat(descriptor)
        path_info = os.stat(
            lock_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime lock identity is invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def exclusive_mediaforce_runtime_lock(
        config: MediaforceConfig,
        *,
        owner_payload: Mapping[str, object],
) -> Iterator[MediaforceRuntimeLease]:
    lock_path = mediaforce_runtime_lock_path(config)
    namespace_state = _runtime_namespace_state(config, lock_path)
    namespace_keys = namespace_state.keys
    active_lease = _ACTIVE_RUNTIME_LEASE.get()
    if active_lease is not None and (
        _runtime_namespace_path_keys(active_lease.namespace_keys)
        == _runtime_namespace_path_keys(namespace_keys)
    ):
        active_lease.assert_active()
        yield active_lease
        return
    namespace_locks = _acquire_runtime_namespace_locks(
        config,
        namespace_state,
        lock_path,
    )
    directory_descriptor = -1
    lock_descriptor = -1
    directory_locked = False
    parent_identity_valid = True
    lease: MediaforceRuntimeLease | None = None
    lease_binding = None
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_descriptor = os.open(lock_path.parent, directory_flags)
        if not _runtime_lock_parent_identity_matches(
            lock_path,
            directory_descriptor,
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime lock path identity is invalid"
            )
        _acquire_nonblocking_lock(
            directory_descriptor,
            lock_path=lock_path,
        )
        directory_locked = True
        lock_descriptor = _open_runtime_lock_file(
            lock_path,
            directory_descriptor=directory_descriptor,
        )
        _acquire_nonblocking_lock(
            lock_descriptor,
            lock_path=lock_path,
        )
        with os.fdopen(lock_descriptor, "r+", encoding="utf-8") as lock_file:
            lock_descriptor = -1
            payload = {"pid": os.getpid(), **dict(owner_payload)}
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(json.dumps(payload, indent=2, sort_keys=True))
            lock_file.write("\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
            lease = MediaforceRuntimeLease(
                namespace_keys=namespace_keys,
                owner_pid=os.getpid(),
                _database_path_key=namespace_state.database_path_key,
                _database_identity=namespace_state.database_identity,
            )
            lease._activate(_RUNTIME_LEASE_AUTHORITY)
            lease_binding = _ACTIVE_RUNTIME_LEASE.set(lease)
            try:
                yield lease
            finally:
                lease._deactivate(_RUNTIME_LEASE_AUTHORITY)
                if lease_binding is not None:
                    _ACTIVE_RUNTIME_LEASE.reset(lease_binding)
                parent_identity_valid = _runtime_lock_parent_identity_matches(
                    lock_path,
                    directory_descriptor,
                )
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.flush()
                os.fsync(lock_file.fileno())
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        try:
            if directory_locked and directory_descriptor >= 0:
                fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
        finally:
            try:
                if directory_descriptor >= 0:
                    os.close(directory_descriptor)
            finally:
                try:
                    if lease is not None:
                        for descriptor in reversed(lease._extra_descriptors):
                            try:
                                os.close(descriptor)
                            except OSError:
                                pass
                finally:
                    namespace_locks.close()
    if not parent_identity_valid:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime lock path changed while active"
        )
