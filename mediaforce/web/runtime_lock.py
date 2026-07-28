from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

from mediaforce.core.config import MediaforceConfig


class MediaforceRuntimeBusyError(RuntimeError):
    pass


def mediaforce_runtime_lock_path(config: MediaforceConfig) -> Path:
    return config.paths.web_state_dir.parent / "mediaforce-web.lock"


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


def _open_runtime_lock_file(lock_path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        descriptor_info = os.fstat(descriptor)
        path_info = lock_path.lstat()
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
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    directory_descriptor = os.open(lock_path.parent, directory_flags)
    lock_descriptor = -1
    directory_locked = False
    try:
        _acquire_nonblocking_lock(
            directory_descriptor,
            lock_path=lock_path,
        )
        directory_locked = True
        lock_descriptor = _open_runtime_lock_file(lock_path)
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
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.flush()
                os.fsync(lock_file.fileno())
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        try:
            if directory_locked:
                fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(directory_descriptor)
