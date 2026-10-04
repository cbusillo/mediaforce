import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_MUTEX = threading.Lock()
_LOCKS: dict[Path, Any] = {}
_HELD = threading.local()


@contextmanager
def delivery_lock(db_path: Path, item_id: int, *, blocking: bool = True) -> Iterator[bool]:
    """Serialize automatic and manual delivery, including another CLI process."""
    path = db_path.resolve().parent / "delivery-locks" / f"{item_id}.lock"
    with _MUTEX:
        lock = _LOCKS.setdefault(path, threading.RLock())
    if not lock.acquire(blocking=blocking):
        yield False
        return
    held = getattr(_HELD, "paths", set())
    _HELD.paths = held
    try:
        if path in held:
            yield True
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            held.add(path)
            try:
                yield True
            finally:
                held.remove(path)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()
