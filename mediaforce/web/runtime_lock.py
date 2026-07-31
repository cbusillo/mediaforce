from __future__ import annotations

from collections.abc import Iterator, Mapping
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
import stat
import sys
import threading

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import register_database_identity_provider
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

    def assert_stable(self) -> None:
        try:
            descriptor_info = os.fstat(self._file_descriptor)
            directory_descriptor_info = os.fstat(self._directory_descriptor)
            relative_info = os.stat(
                self.path.name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
            path_info = self.path.lstat()
            parent_info = self.path.parent.lstat()
        except OSError as exc:
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

    def discard_after_publish(self) -> None:
        self.assert_stable()
        try:
            os.unlink(self.path.name, dir_fd=self._directory_descriptor)
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source cleanup failed after publication"
            ) from exc
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(
                    f"{self.path.name}{suffix}",
                    dir_fd=self._directory_descriptor,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MediaforceRuntimeBusyError(
                    "Legacy SQLite source cleanup failed after publication"
                ) from exc
        try:
            os.fsync(self._directory_descriptor)
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Legacy SQLite source cleanup could not be synchronized"
            ) from exc

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
        locked_source = LegacySQLiteMigrationSource(
            path=source,
            _file_descriptor=descriptor,
            _directory_descriptor=directory_descriptor,
            _file_snapshot=_legacy_sqlite_source_snapshot(source_info),
            _directory_identity=(
                source_parent_info.st_dev,
                source_parent_info.st_ino,
            ),
        )
        locked_source.assert_stable()
        yield locked_source
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


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
