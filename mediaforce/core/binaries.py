import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

_HOMEBREW_BINARIES = {
    "ffmpeg": Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
    "ffprobe": Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"),
}

_LOW_CPU_PRIORITY = ("-n", "19")
_IDLE_DISK_PRIORITY = ("-c", "3")
_MEDIA_TOOL_NAMES = {"ffmpeg", "ffprobe", "ab-av1"}
_MEDIA_TOOL_OVERRIDE_KEYS = ("MEDIAFORCE_FFMPEG", "MEDIAFORCE_FFPROBE")


def _normalized_executable_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def is_media_tool_command(cmd: list[str], env: Mapping[str, str] | None = None) -> bool:
    if not cmd:
        return False
    executable = cmd[0]
    if os.path.basename(executable) in _MEDIA_TOOL_NAMES:
        return True
    normalized_executable = _normalized_executable_path(executable)
    for environment in (os.environ, env):
        if environment is None:
            continue
        for key in _MEDIA_TOOL_OVERRIDE_KEYS:
            override = environment.get(key)
            if override and _normalized_executable_path(override) == normalized_executable:
                return True
    return False


def low_priority_command(command: list[str]) -> list[str]:
    prefix = ["/usr/bin/nice", *_LOW_CPU_PRIORITY]
    if sys.platform.startswith("linux"):
        prefix.extend(["/usr/bin/ionice", *_IDLE_DISK_PRIORITY])
    return [*prefix, *command]


def remote_low_priority_shell(command: list[str]) -> str:
    return (
        f'(case "$(uname -s)" in Linux) set -- ionice {shlex.join(_IDLE_DISK_PRIORITY)} ;; *) set -- ;; esac; '
        f'exec nice {shlex.join(_LOW_CPU_PRIORITY)} "$@" {shlex.join(command)})'
    )


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
