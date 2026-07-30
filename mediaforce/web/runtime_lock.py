from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.utils import filesystem_collision_key


class MediaforceRuntimeBusyError(RuntimeError):
    pass


_RUNTIME_NAMESPACE_PORT_MIN = 20_000
_RUNTIME_NAMESPACE_PORT_SPAN = 29_000


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
            resolved = Path(value).expanduser().resolve()
            keys.add(f"{name}:{filesystem_collision_key(resolved)}")
    return tuple(sorted(keys))


def _runtime_namespace_port(key: str) -> int:
    payload = f"{os.getuid()}:{key}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return (
        _RUNTIME_NAMESPACE_PORT_MIN
        + int.from_bytes(digest[:4], "big") % _RUNTIME_NAMESPACE_PORT_SPAN
    )


def _acquire_runtime_namespace_socket(
        config: MediaforceConfig,
        lock_path: Path,
) -> tuple[socket.socket, ...]:
    namespace_sockets: list[socket.socket] = []
    try:
        for port in sorted({
            _runtime_namespace_port(key)
            for key in _runtime_namespace_keys(config, lock_path)
        }):
            namespace_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            namespace_sockets.append(namespace_socket)
            namespace_socket.bind(("127.0.0.1", port))
        return tuple(namespace_sockets)
    except OSError as exc:
        for namespace_socket in reversed(namespace_sockets):
            namespace_socket.close()
        owner = mediaforce_runtime_lock_owner(lock_path)
        owner_detail = f" ({owner})" if owner else ""
        if exc.errno == errno.EADDRINUSE:
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime is already active{owner_detail}"
            ) from exc
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime namespace lock is unavailable"
        ) from exc


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
) -> Iterator[None]:
    lock_path = mediaforce_runtime_lock_path(config)
    namespace_sockets = _acquire_runtime_namespace_socket(config, lock_path)
    directory_descriptor = -1
    lock_descriptor = -1
    directory_locked = False
    parent_identity_valid = True
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
            try:
                yield
            finally:
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
                for namespace_socket in reversed(namespace_sockets):
                    namespace_socket.close()
    if not parent_identity_valid:
        raise MediaforceRuntimeBusyError(
            "Mediaforce runtime lock path changed while active"
        )
