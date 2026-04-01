import platform as platform_module
import subprocess
from functools import lru_cache

from mediaforce.binaries import ffmpeg_binary

VIDEOTOOLBOX_REQUIRED_ISSUE = "ffmpeg is missing VideoToolbox hardware decode required for H.264/H.265 sources."
SVT_AV1_REQUIRED_ISSUE = "ffmpeg is missing libsvtav1 support required for queue encodes."
VIDEOTOOLBOX_SOURCE_CODECS = {"avc1", "h264", "h265", "hevc"}


def normalize_execution_platform(platform_name: str | None) -> str:
    normalized = str(platform_name or "").strip().lower()
    if normalized in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if normalized.startswith("linux"):
        return "linux"
    if normalized in {"win32", "windows"}:
        return "windows"
    return normalized or "unknown"


def local_execution_platform() -> str:
    return normalize_execution_platform(platform_module.system())


def should_use_videotoolbox_decode(source_codec: str | None) -> bool:
    return str(source_codec or "").strip().lower() in VIDEOTOOLBOX_SOURCE_CODECS


def has_videotoolbox_hwaccel(hwaccels_output: str) -> bool:
    return "videotoolbox" in hwaccels_output.lower()


@lru_cache(maxsize=1)
def local_videotoolbox_support() -> bool:
    ffmpeg_cmd = ffmpeg_binary()
    try:
        result = subprocess.run([ffmpeg_cmd, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=2)
    except Exception:
        return False

    if result.returncode != 0:
        return False

    return has_videotoolbox_hwaccel(f"{result.stdout}\n{result.stderr}")


def ffmpeg_hwaccel_input_args(
        source_codec: str | None,
        *,
        platform_name: str | None = None,
        videotoolbox_available: bool | None = None,
) -> list[str]:
    if not should_use_videotoolbox_decode(source_codec):
        return []
    platform_value = normalize_execution_platform(
        platform_name) if platform_name is not None else local_execution_platform()
    if platform_value != "macos":
        return []
    if videotoolbox_available is None:
        videotoolbox_available = local_videotoolbox_support()
    if not videotoolbox_available:
        return []
    return ["-hwaccel", "videotoolbox"]


def ab_av1_hwaccel_input_args(
        source_codec: str | None,
        *,
        platform_name: str | None = None,
        videotoolbox_available: bool | None = None,
) -> list[str]:
    if not should_use_videotoolbox_decode(source_codec):
        return []
    platform_value = normalize_execution_platform(
        platform_name) if platform_name is not None else local_execution_platform()
    if platform_value != "macos":
        return []
    if videotoolbox_available is None:
        videotoolbox_available = local_videotoolbox_support()
    if not videotoolbox_available:
        return []
    return ["--enc-input", "hwaccel=videotoolbox"]
