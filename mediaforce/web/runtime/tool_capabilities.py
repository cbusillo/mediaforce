import subprocess
import threading
from pathlib import Path

from mediaforce.core.binaries import ffmpeg_binary

_METRIC_SUPPORT_DEFAULTS = {"vmaf": False, "xpsnr": False, "ssim": False, "psnr": False}
_METRIC_SUPPORT_LOCK = threading.Lock()
_METRIC_SUPPORT_BY_BINARY: dict[tuple[str, int, int], dict[str, bool]] = {}
_ACTIVE_METRIC_SUPPORT_IDENTITY: tuple[str, int, int] | None = None


def metric_support() -> dict[str, bool]:
    with _METRIC_SUPPORT_LOCK:
        if _ACTIVE_METRIC_SUPPORT_IDENTITY is None:
            return dict(_METRIC_SUPPORT_DEFAULTS)
        return dict(_METRIC_SUPPORT_BY_BINARY.get(_ACTIVE_METRIC_SUPPORT_IDENTITY, _METRIC_SUPPORT_DEFAULTS))


def refresh_metric_support() -> dict[str, bool]:
    global _ACTIVE_METRIC_SUPPORT_IDENTITY

    binary = ffmpeg_binary()
    identity = _binary_identity(binary)
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        support = dict(_METRIC_SUPPORT_DEFAULTS)
    else:
        output = result.stdout.lower()
        support = {
            "vmaf": "libvmaf" in output,
            "xpsnr": "xpsnr" in output,
            "ssim": "ssim" in output,
            "psnr": " psnr " in output or "\n ts psnr" in output,
        }

    with _METRIC_SUPPORT_LOCK:
        _METRIC_SUPPORT_BY_BINARY[identity] = support
        _ACTIVE_METRIC_SUPPORT_IDENTITY = identity
    return dict(support)


def reset_metric_support_cache() -> None:
    global _ACTIVE_METRIC_SUPPORT_IDENTITY

    with _METRIC_SUPPORT_LOCK:
        _METRIC_SUPPORT_BY_BINARY.clear()
        _ACTIVE_METRIC_SUPPORT_IDENTITY = None


def _binary_identity(binary: str) -> tuple[str, int, int]:
    path = Path(binary).expanduser()
    try:
        stat_result = path.stat()
    except OSError:
        return binary, -1, -1
    return str(path.resolve()), stat_result.st_size, stat_result.st_mtime_ns
