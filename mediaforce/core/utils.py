import hashlib
import os
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

CONTENT_VERSION_SAMPLE_BYTES = 64 * 1024
FILE_DIGEST_CHUNK_BYTES = 4 * 1024 * 1024


def filesystem_collision_key(path: Path) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(str(path))).casefold()


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
    with file_path.open("rb") as handle:
        return descriptor_content_version_fingerprint(
            handle.fileno(),
            size_bytes=stat_result.st_size,
        )


def descriptor_content_version_fingerprint(
        descriptor: int,
        *,
        size_bytes: int,
) -> str:
    size = max(0, int(size_bytes))
    chunk_size = min(CONTENT_VERSION_SAMPLE_BYTES, size)
    positions = {0}
    if size > chunk_size:
        positions.add(max(0, (size - chunk_size) // 2))
        positions.add(max(0, size - chunk_size))
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(str(size).encode())
    for position in sorted(positions):
        digest.update(position.to_bytes(8, "big", signed=False))
        digest.update(os.pread(descriptor, chunk_size, position))
    return digest.hexdigest()


def descriptor_sha256(
        descriptor: int,
        *,
        check_cancelled: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        if check_cancelled is not None:
            check_cancelled()
        chunk = os.read(descriptor, FILE_DIGEST_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def file_stat_signature(
        file_stat: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )
