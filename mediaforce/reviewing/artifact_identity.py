from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Sequence

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import float_value


def reviewed_artifact_fingerprint(
        clips: Sequence[tuple[str, Path, float, float]],
) -> str | None:
    clip_payloads: list[dict[str, object]] = []
    for role, path, timestamp_seconds, duration_seconds in clips:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            clip_payload = reviewed_artifact_clip_payload_from_descriptor(
                role=role,
                path=path,
                descriptor=descriptor,
                timestamp_seconds=timestamp_seconds,
                duration_seconds=duration_seconds,
            )
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if clip_payload is None:
            return None
        clip_payloads.append(clip_payload)
    return reviewed_artifact_fingerprint_from_payloads(clip_payloads)


def reviewed_artifact_clip_payload_from_descriptor(
        *,
        role: str,
        path: Path,
        descriptor: int,
        timestamp_seconds: float,
        duration_seconds: float,
) -> dict[str, object] | None:
    try:
        initial_stat = os.fstat(descriptor)
        initial_path_stat = path.lstat()
        if (
                not stat.S_ISREG(initial_stat.st_mode)
                or not stat.S_ISREG(initial_path_stat.st_mode)
                or (initial_stat.st_dev, initial_stat.st_ino)
                != (initial_path_stat.st_dev, initial_path_stat.st_ino)
        ):
            return None
        content_sha256 = _file_sha256(descriptor)
        final_stat = os.fstat(descriptor)
        final_path_stat = path.lstat()
    except OSError:
        return None
    if (
            initial_stat.st_dev != final_stat.st_dev
            or initial_stat.st_ino != final_stat.st_ino
            or initial_stat.st_size != final_stat.st_size
            or initial_stat.st_mtime_ns != final_stat.st_mtime_ns
            or initial_stat.st_ctime_ns != final_stat.st_ctime_ns
            or stat.S_IMODE(initial_stat.st_mode) != stat.S_IMODE(final_stat.st_mode)
            or initial_stat.st_nlink != final_stat.st_nlink
            or (final_stat.st_dev, final_stat.st_ino)
            != (final_path_stat.st_dev, final_path_stat.st_ino)
    ):
        return None
    normalized_path = Path(os.path.abspath(os.fspath(path.expanduser())))
    return {
        "role": role,
        "timestamp_seconds": round(float(timestamp_seconds), 3),
        "duration_seconds": round(float(duration_seconds), 3),
        "size_bytes": int(final_stat.st_size),
        "content_sha256": content_sha256,
        "path_sha256": hashlib.sha256(os.fsencode(normalized_path)).hexdigest(),
        "device": int(final_stat.st_dev),
        "inode": int(final_stat.st_ino),
        "mtime_ns": int(final_stat.st_mtime_ns),
        "ctime_ns": int(final_stat.st_ctime_ns),
        "mode": stat.S_IMODE(final_stat.st_mode),
        "link_count": int(final_stat.st_nlink),
    }


def reviewed_artifact_fingerprint_from_payloads(
        clip_payloads: list[dict[str, object]],
) -> str | None:
    if not clip_payloads:
        return None
    clip_payloads.sort(
        key=lambda clip: (
            str(clip["role"]),
            float_value(clip["timestamp_seconds"]),
            float_value(clip["duration_seconds"]),
            str(clip["content_sha256"]),
        )
    )
    return f"cira2_{stable_json_hash({'schema_version': 2, 'clips': clip_payloads})[:32]}"


def _file_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
