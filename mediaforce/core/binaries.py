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
    override = os.environ.get(f"MEDIAFORCE_{name.upper()}")
    if override:
        return override

    preferred = _HOMEBREW_BINARIES.get(name)
    if preferred and preferred.exists():
        return str(preferred)

    # noinspection PyDeprecation
    discovered = shutil.which(name)
    if discovered:
        return discovered

    return name


def ffmpeg_binary() -> str:
    return media_binary("ffmpeg")


def ffprobe_binary() -> str:
    return media_binary("ffprobe")
