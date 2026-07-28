from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

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


@contextmanager
def exclusive_mediaforce_runtime_lock(
        config: MediaforceConfig,
        *,
        owner_payload: Mapping[str, object],
) -> Iterator[None]:
    lock_path = mediaforce_runtime_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = mediaforce_runtime_lock_owner(lock_path)
            owner_detail = f" ({owner})" if owner else ""
            raise MediaforceRuntimeBusyError(
                f"Mediaforce runtime is already active{owner_detail}"
            ) from exc

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
