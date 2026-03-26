from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path


_HOMEBREW_BINARIES = {
    "ffmpeg": Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
    "ffprobe": Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"),
}


@lru_cache(maxsize=None)
def media_binary(name: str) -> str:
    env_name = f"MEDIA_HARNESS_{name.upper()}"
    override = os.environ.get(env_name)
    if override:
        return override

    preferred = _HOMEBREW_BINARIES.get(name)
    if preferred and preferred.exists():
        return str(preferred)

    discovered = shutil.which(name)
    if discovered:
        return discovered

    return name


def ffmpeg_binary() -> str:
    return media_binary("ffmpeg")


def ffprobe_binary() -> str:
    return media_binary("ffprobe")
