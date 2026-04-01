import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path


def timestamp() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def file_fingerprint(file_path: Path, stat_result: os.stat_result, duration_seconds: float | None) -> str:
    material = ":".join(
        [
            str(file_path),
            str(stat_result.st_size),
            str(stat_result.st_mtime_ns),
            f"{duration_seconds or 0:.3f}",
        ]
    )
    return hashlib.sha1(material.encode(), usedforsecurity=False).hexdigest()
