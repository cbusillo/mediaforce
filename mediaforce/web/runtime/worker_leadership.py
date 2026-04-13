import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO


class WorkerLeadershipLease:
    def __init__(self, lock_path: Path, *, worker_name: str) -> None:
        self.lock_path = lock_path
        self.worker_name = worker_name
        self.metadata_path = lock_path.with_suffix(f"{lock_path.suffix}.json")
        self._handle: TextIO | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._handle = handle
        self.metadata_path.write_text(json.dumps(self._metadata_payload(), indent=2, sort_keys=True) + "\n")
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        try:
            self.metadata_path.unlink()
        except FileNotFoundError:
            pass

    def owner_metadata(self) -> dict[str, Any] | None:
        if not self.metadata_path.exists():
            return None
        try:
            payload = json.loads(self.metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _metadata_payload(self) -> dict[str, Any]:
        return {
            "worker_name": self.worker_name,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "cwd": os.getcwd(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
