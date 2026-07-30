from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.utils import filesystem_collision_key


class MediaforceRuntimeBusyError(RuntimeError):
    pass


class MediaforceRuntimeLockOwnershipError(RuntimeError):
    pass


@dataclass(slots=True)
class MediaforceRuntimeLease:
    namespace_keys: tuple[str, ...]
    owner_pid: int
    _active: bool = field(default=False, init=False, repr=False)
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock,
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


_RUNTIME_NAMESPACE_LOCK_DIRECTORY_NAME = "mediaforce-runtime-namespaces-v1"
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


def mediaforce_runtime_lock_path(config: MediaforceConfig) -> Path:
    return mediaforce_runtime_lock_path_for_web_state_dir(
        config.paths.web_state_dir
    )


def mediaforce_runtime_lock_path_for_web_state_dir(web_state_dir: Path) -> Path:
    return web_state_dir.expanduser().resolve().parent / "mediaforce-web.lock"


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


def _runtime_namespace_keys(
        config: MediaforceConfig,
        lock_path: Path,
) -> tuple[str, ...]:
    keys = {f"lock:{filesystem_collision_key(lock_path)}"}
    for name in ("config_path", "db_path"):
        value = getattr(config.paths, name, None)
        if value is not None:
            try:
                resolved = Path(value).expanduser().resolve()
            except OSError as exc:
                raise MediaforceRuntimeBusyError(
                    f"Mediaforce runtime {name} namespace is unavailable"
                ) from exc
            keys.add(f"{name}:{filesystem_collision_key(resolved)}")
            try:
                identity = resolved.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MediaforceRuntimeBusyError(
                    f"Mediaforce runtime {name} identity is unavailable"
                ) from exc
            keys.add(f"file:{identity.st_dev}:{identity.st_ino}")
    return tuple(sorted(keys))


def _runtime_namespace_lock_directory() -> Path:
    try:
        temporary_directory = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace directory is unavailable"
        ) from exc
    return temporary_directory / (
        f"{_RUNTIME_NAMESPACE_LOCK_DIRECTORY_NAME}-{os.getuid()}"
    )


def _runtime_namespace_lock_name(key: str) -> str:
    payload = f"{os.getuid()}:{key}".encode("utf-8")
    return f"{hashlib.sha256(payload).hexdigest()}.lock"


def _runtime_namespace_path_keys(keys: tuple[str, ...]) -> frozenset[str]:
    return frozenset(key for key in keys if not key.startswith("file:"))


@dataclass(frozen=True, slots=True)
class _RuntimeNamespaceLocks:
    directory_descriptor: int
    lock_descriptors: tuple[int, ...]

    def close(self) -> None:
        for descriptor in reversed(self.lock_descriptors):
            os.close(descriptor)
        os.close(self.directory_descriptor)


def _open_runtime_namespace_directory() -> int:
    directory_path = _runtime_namespace_lock_directory()
    try:
        directory_path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace directory is unavailable"
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
            "Mediaforce runtime namespace directory is invalid"
        ) from exc
    try:
        try:
            descriptor_info = os.fstat(descriptor)
            path_info = directory_path.lstat()
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace directory is invalid"
            ) from exc
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_info.st_mode) & 0o077
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace directory is invalid"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_namespace_lock_file(
        name: str,
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
            name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace lock is unavailable"
        ) from exc
    try:
        try:
            descriptor_info = os.fstat(descriptor)
            path_info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace lock identity is invalid"
            ) from exc
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
        try:
            os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace lock is unavailable"
            ) from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_runtime_namespace_locks(
        namespace_keys: tuple[str, ...],
        lock_path: Path,
) -> _RuntimeNamespaceLocks:
    directory_descriptor = _open_runtime_namespace_directory()
    lock_descriptors: list[int] = []
    try:
        lock_names = tuple(sorted(
            _runtime_namespace_lock_name(key)
            for key in namespace_keys
        ))
        if len(set(lock_names)) != len(lock_names):
            raise MediaforceRuntimeBusyError(
                "Mediaforce runtime namespace lock identity is invalid"
            )
        for name in lock_names:
            descriptor = _open_runtime_namespace_lock_file(
                name,
                directory_descriptor=directory_descriptor,
            )
            lock_descriptors.append(descriptor)
            _acquire_nonblocking_lock(
                descriptor,
                lock_path=lock_path,
            )
        return _RuntimeNamespaceLocks(
            directory_descriptor=directory_descriptor,
            lock_descriptors=tuple(lock_descriptors),
        )
    except BaseException:
        for descriptor in reversed(lock_descriptors):
            os.close(descriptor)
        os.close(directory_descriptor)
        raise


def _runtime_lock_parent_identity_matches(
        lock_path: Path,
        directory_descriptor: int,
) -> bool:
    try:
        descriptor_info = os.fstat(directory_descriptor)
        path_info = lock_path.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(descriptor_info.st_mode)
        and stat.S_ISDIR(path_info.st_mode)
        and (descriptor_info.st_dev, descriptor_info.st_ino)
        == (path_info.st_dev, path_info.st_ino)
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
    namespace_keys = _runtime_namespace_keys(config, lock_path)
    active_lease = _ACTIVE_RUNTIME_LEASE.get()
    if active_lease is not None and (
        _runtime_namespace_path_keys(active_lease.namespace_keys)
        == _runtime_namespace_path_keys(namespace_keys)
    ):
        active_lease.assert_active()
        yield active_lease
        return
    namespace_locks = _acquire_runtime_namespace_locks(
        namespace_keys,
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
                namespace_locks.close()
    if not parent_identity_valid:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime lock path changed while active"
        )
