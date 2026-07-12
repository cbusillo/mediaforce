import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

CONTENT_VERSION_SAMPLE_BYTES = 64 * 1024


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


def content_version_fingerprint(file_path: Path, stat_result: os.stat_result) -> str:
    size = max(0, int(stat_result.st_size))
    chunk_size = min(CONTENT_VERSION_SAMPLE_BYTES, size)
    positions = {0}
    if size > chunk_size:
        positions.add(max(0, (size - chunk_size) // 2))
        positions.add(max(0, size - chunk_size))
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(str(size).encode())
    with file_path.open("rb") as handle:
        for position in sorted(positions):
            handle.seek(position)
            digest.update(position.to_bytes(8, "big", signed=False))
            digest.update(handle.read(chunk_size))
    return digest.hexdigest()
