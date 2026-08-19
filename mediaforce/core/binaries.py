import os
import shutil
import subprocess
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
    if preferred and preferred.exists() and _binary_runs(preferred):
        return str(preferred)

    # noinspection PyDeprecation
    discovered = shutil.which(name)
    if discovered and _binary_runs(Path(discovered)):
        return discovered

    return name


def _binary_runs(path: Path) -> bool:
    environment = dict(os.environ)
    environment.pop("DYLD_LIBRARY_PATH", None)
    try:
        result = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ffmpeg_binary() -> str:
    return media_binary("ffmpeg")


def ffprobe_binary() -> str:
    return media_binary("ffprobe")
