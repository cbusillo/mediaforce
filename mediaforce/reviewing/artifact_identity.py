from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import float_value


def reviewed_artifact_fingerprint(
        clips: Sequence[tuple[str, Path, float, float]],
) -> str | None:
    clip_payloads: list[dict[str, object]] = []
    for role, path, timestamp_seconds, duration_seconds in clips:
        try:
            initial_stat = path.stat()
            content_sha256 = _file_sha256(path)
            final_stat = path.stat()
        except OSError:
            return None
        if (
                initial_stat.st_size != final_stat.st_size
                or initial_stat.st_mtime_ns != final_stat.st_mtime_ns
        ):
            return None
        clip_payloads.append(
            {
                "role": role,
                "timestamp_seconds": round(float(timestamp_seconds), 3),
                "duration_seconds": round(float(duration_seconds), 3),
                "size_bytes": int(final_stat.st_size),
                "content_sha256": content_sha256,
            }
        )
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
    return f"cira1_{stable_json_hash({'schema_version': 1, 'clips': clip_payloads})[:32]}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
