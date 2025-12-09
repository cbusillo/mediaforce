#!/usr/bin/env python3
# mypy: ignore-errors
"""Mediaforce: content-aware media encoder with unified scheduling.

Analyzes source quality and applies appropriate compression settings.
Maximum compression with watchable quality, not source fidelity.

Runs on:
  - Mac Studio (M2): /Volumes/media, /Volumes/extras
  - Mac (M4): /Volumes/media, /Volumes/extras
  - tdarr (Proxmox CT 103): /mnt/media, /mnt/extras
    - Has NVIDIA 3060 + 1660 available for future GPU encoding
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
import builtins
import inspect
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from sqlmodel import Session, select, delete
from sqlalchemy import text

from db import (
    AppSetting,
    EncodeProgress,
    EncodeResult,
    Library,
    MediaItem,
    ShowOverride,
    init_engine,
    now_iso,
)


# Cross-platform media roots
MEDIA_ROOTS_MAC = ["/Volumes/media", "/Volumes/extras"]
MEDIA_ROOTS_LINUX = ["/mnt/media", "/mnt/extras"]


# Application-level settings -------------------------------------------------


# Settings and data storage live under ~/.config/mediaforce
CONFIG_DIR = pathlib.Path.home() / ".config" / "mediaforce"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = CONFIG_DIR / "mediaforce.db"
SETTINGS_DB = DB_PATH
SETTINGS_PATH = DB_PATH
INVENTORY_DB = DB_PATH
REMOTE_SETTINGS_URL: str | None = None

ENGINE = init_engine(str(DB_PATH))
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("mediaforce")


def log_event(level: int, message: str, **fields: Any) -> None:
    payload = {"message": message, **fields}
    logger.log(level, json.dumps(payload, ensure_ascii=False))


def log_info(message: str, **fields: Any) -> None:
    log_event(logging.INFO, message, **fields)


def log_warn(message: str, **fields: Any) -> None:
    log_event(logging.WARNING, message, **fields)


def log_error(message: str, **fields: Any) -> None:
    log_event(logging.ERROR, message, **fields)


def _print_override(*args, **kwargs):
    stream = kwargs.get("file")
    level = logging.ERROR if stream is sys.stderr else logging.INFO
    text = " ".join(str(a) for a in args)
    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame and frame.f_back else None
    origin = caller.f_code.co_name if caller else "print"
    log_event(level, "cli_output", origin=origin, text=text)


builtins.print = _print_override


class ResultWrapper:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class SessionShim:
    def __init__(self, session: Session):
        self.session = session

    def execute(self, sql: str, params: tuple | dict | None = None):
        result = self.session.exec(text(sql), params or {})
        rows = result.all()
        return ResultWrapper(rows)

    def commit(self):
        self.session.commit()

    def close(self):
        self.session.close()


@dataclass
class LibrarySettings:
    """Configuration for a logical media library.

    We keep separate paths for macOS and Linux so that the same logical
    library (e.g. "TV" or "Movies") can be accessed from both /Volumes and
    /mnt style mounts. This configuration is shared by the CLI, the web UI,
    and any background watchers.
    """

    id: str
    name: str
    media_type: str  # e.g. "tv", "movies"
    mac_path: str
    linux_path: str
    watch: bool = True
    max_height: Optional[int] = None  # Downscale target height (e.g., 1080); never upscales
    weight: float = 1.0


@dataclass
class AppSettings:
    """Top-level application settings container."""

    libraries: list[LibrarySettings] = field(default_factory=list)
    global_max_height: Optional[int] = None
    max_concurrency: int = 1
    offpeak_enabled: bool = False
    offpeak_start: str = "00:00"
    offpeak_end: str = "05:00"


def _default_app_settings() -> AppSettings:
    """Return default settings used when no config file exists.

    By default we track TV and Movies libraries under /Volumes/media on macOS
    and /mnt/media on Linux, which matches the documented layout in README.
    """

    return AppSettings(
        global_max_height=1080,
        max_concurrency=1,
        offpeak_enabled=False,
        offpeak_start="00:00",
        offpeak_end="05:00",
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV Library",
                media_type="tv",
                mac_path="/Volumes/media/tv",
                linux_path="/mnt/media/tv",
                watch=True,
                max_height=1080,
                weight=1.0,
            ),
            LibrarySettings(
                id="movies",
                name="Movies Library",
                media_type="movies",
                mac_path="/Volumes/media/movies",
                linux_path="/mnt/media/movies",
                watch=True,
                max_height=2160,
                weight=1.0,
            ),
        ]
    )


# =============================================================================
# Autoupdate helper
# =============================================================================


def _download_file(url: str, dest: pathlib.Path, expected_sha256: str | None = None) -> bool:
    try:
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
    except Exception:
        return False

    if expected_sha256:
        h = hashlib.sha256(data).hexdigest()
        if h != expected_sha256:
            return False

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def maybe_autoupdate(base_url: str, files: list[str]) -> bool:
    """Pull latest allowed files from base_url (hosting /raw/manifest.json and /raw/<file>)."""
    if not base_url.endswith('/'):
        base_url += '/'

    manifest_url = base_url + 'manifest.json'
    try:
        with urllib.request.urlopen(manifest_url) as resp:
            manifest = json.loads(resp.read().decode())
    except Exception:
        return False

    changed = False
    file_info = manifest.get('files', {}) if isinstance(manifest, dict) else {}
    base_dir = pathlib.Path(__file__).parent

    for fname in files:
        info = file_info.get(fname)
        if not info:
            continue
        target = base_dir / fname
        expected = info.get('sha256')
        # Check local hash
        local_hash = None
        if target.exists():
            h = hashlib.sha256()
            with target.open('rb') as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            local_hash = h.hexdigest()
        if local_hash == expected:
            continue

        if _download_file(base_url + fname, target, expected_sha256=expected):
            changed = True

    return changed


def load_app_settings() -> AppSettings:
    """Load application settings from disk, falling back to defaults."""
    if SETTINGS_DB.exists():
        try:
            with Session(ENGINE) as session:
                setting = session.get(AppSetting, 1)
                gmh = setting.global_max_height if setting else None
                libs = session.exec(select(Library)).all()
                if libs:
                    return AppSettings(
                        libraries=[
                            LibrarySettings(
                                id=lib.id,
                                name=lib.name,
                                media_type=lib.media_type,
                                mac_path=lib.mac_path,
                                linux_path=lib.linux_path,
                                watch=lib.watch,
                                max_height=lib.max_height,
                                weight=lib.weight,
                            )
                            for lib in libs
                        ],
                        global_max_height=gmh,
                        max_concurrency=setting.max_concurrency if setting else 1,
                        offpeak_enabled=setting.offpeak_enabled if setting else False,
                        offpeak_start=setting.offpeak_start if setting else "00:00",
                        offpeak_end=setting.offpeak_end if setting else "05:00",
                    )
        except Exception:
            pass

    return _default_app_settings()


def load_remote_settings(url: str) -> Optional[AppSettings]:
    """Fetch settings JSON from master API and convert to AppSettings."""
    try:
        with urllib.request.urlopen(url) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    settings_payload = payload.get("settings") if "settings" in payload else payload
    if not isinstance(settings_payload, dict):
        return None

    libs_raw = settings_payload.get("libraries", [])
    libraries: list[LibrarySettings] = []
    for raw in libs_raw:
        try:
            libraries.append(
                LibrarySettings(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    media_type=str(raw.get("media_type") or ""),
                    mac_path=str(raw.get("mac_path") or ""),
                    linux_path=str(raw.get("linux_path") or ""),
                    watch=bool(raw.get("watch", True)),
                    max_height=(int(raw.get("max_height")) if raw.get("max_height") else None),
                    weight=float(raw.get("weight", 1.0)),
                )
            )
        except Exception:
            continue

    global_max_height = settings_payload.get("global_max_height")
    try:
        global_max_height = int(global_max_height) if global_max_height is not None else None
    except Exception:
        global_max_height = None

    if not libraries:
        return None

    return AppSettings(libraries=libraries, global_max_height=global_max_height)



def save_app_settings(settings: AppSettings) -> None:
    """Persist application settings to SQLite."""
    with Session(ENGINE) as session:
        setting = session.get(AppSetting, 1) or AppSetting(id=1)
        setting.global_max_height = settings.global_max_height
        setting.max_concurrency = settings.max_concurrency
        setting.offpeak_enabled = settings.offpeak_enabled
        setting.offpeak_start = settings.offpeak_start
        setting.offpeak_end = settings.offpeak_end
        session.add(setting)
        session.exec(delete(Library))
        for lib in settings.libraries:
            session.add(
                Library(
                    id=lib.id,
                    name=lib.name,
                    media_type=lib.media_type,
                    mac_path=lib.mac_path,
                    linux_path=lib.linux_path,
                    watch=lib.watch,
                    max_height=lib.max_height,
                    weight=lib.weight,
                )
            )
        session.commit()


def iter_libraries_for_current_host(settings: Optional[AppSettings] = None) -> list[tuple[LibrarySettings, pathlib.Path]]:
    """Return libraries and resolved paths for the current OS.

    This is the canonical way to get logical -> physical library mappings.
    """

    if settings is None:
        settings = load_app_settings()

    is_mac = platform.system() == "Darwin"
    result: list[tuple[LibrarySettings, pathlib.Path]] = []

    for lib in settings.libraries:
        root = lib.mac_path if is_mac else lib.linux_path
        if not root:
            continue
        result.append((lib, pathlib.Path(root)))

    return result


def get_media_roots() -> list[str]:
    """Return media root paths for current platform.

    This uses the coarse /Volumes vs /mnt roots so it stays compatible with
    existing paths and the inventory database. Higher-level code should
    prefer the library settings helpers when it needs specific tv/movies
    roots, but this remains the canonical list of mount prefixes.
    """

    if platform.system() == "Darwin":
        return MEDIA_ROOTS_MAC
    return MEDIA_ROOTS_LINUX


def normalize_path(path: pathlib.Path) -> pathlib.Path:
    """Normalize path between macOS and Linux mount points.

    If a path doesn't exist, try swapping /Volumes/X <-> /mnt/X.
    """
    if path.exists():
        return path

    path_str = str(path)

    # Try Mac -> Linux
    for mac_root in MEDIA_ROOTS_MAC:
        if path_str.startswith(mac_root):
            linux_root = mac_root.replace("/Volumes/", "/mnt/")
            candidate = pathlib.Path(path_str.replace(mac_root, linux_root, 1))
            if candidate.exists():
                return candidate

    # Try Linux -> Mac
    for linux_root in MEDIA_ROOTS_LINUX:
        if path_str.startswith(linux_root):
            mac_root = linux_root.replace("/mnt/", "/Volumes/")
            candidate = pathlib.Path(path_str.replace(linux_root, mac_root, 1))
            if candidate.exists():
                return candidate

    return path


def detect_platform() -> Dict[str, Any]:
    """Detect current platform and available hardware."""
    info: Dict[str, Any] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "has_nvidia": False,
        "media_roots": [],
    }

    # Check for NVIDIA GPU (Linux only, for future NVENC support)
    if info["system"] == "Linux":
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["has_nvidia"] = True
                info["nvidia_gpus"] = [g.strip() for g in result.stdout.strip().split("\n")]
        except FileNotFoundError:
            pass

    # Check which media roots are available
    for root in get_media_roots():
        if pathlib.Path(root).exists():
            info["media_roots"].append(root)

    return info


class SourceTier(Enum):
    """Source quality classification."""

    PRISTINE = "pristine"  # Modern streaming, Blu-ray
    GOOD = "good"  # Most HD TV
    MEDIOCRE = "mediocre"  # Older HD, moderate grain
    POOR = "poor"  # Upscaled SD, heavy noise


@dataclass
class TierSettings:
    """Encoding settings for a source tier."""

    crf: int
    preset: int
    film_grain: int  # 0 = disabled, 4-8 = typical range
    denoise: Optional[str]  # None, "light", "medium", "heavy"


# Encoding parameters per tier
TIER_SETTINGS: dict[SourceTier, TierSettings] = {
    SourceTier.PRISTINE: TierSettings(crf=26, preset=5, film_grain=0, denoise=None),
    SourceTier.GOOD: TierSettings(crf=28, preset=5, film_grain=8, denoise=None),
    SourceTier.MEDIOCRE: TierSettings(crf=30, preset=6, film_grain=4, denoise="light"),
    SourceTier.POOR: TierSettings(crf=32, preset=6, film_grain=0, denoise="heavy"),
}

# Denoise filter strings for ffmpeg
DENOISE_FILTERS: dict[str, str] = {
    "light": "hqdn3d=2:2:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
    "heavy": "nlmeans=s=3.0:p=7:r=9",
}

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov"}


# =============================================================================
# Library watching (auto-queue new files)
# =============================================================================


if TYPE_CHECKING:
    from watchfiles import Change, awatch  # type: ignore
else:
    try:  # Optional dependency – only needed for the watch command
        from watchfiles import Change, awatch
    except Exception:  # pragma: no cover - watch is an optional feature
        Change = None  # type: ignore
        awatch = None  # type: ignore


@dataclass
class MediaInfo:
    """Parsed media file information."""

    path: pathlib.Path
    duration_seconds: Optional[float] = None
    video_codec: Optional[str] = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    video_bitrate_kbps: Optional[int] = None
    video_bit_depth: Optional[int] = None
    video_framerate: Optional[float] = None
    video_field_order: Optional[str] = None  # progressive, tt, bb, tb, bt
    # Interlacing detection via idet filter (actual frame analysis)
    interlace_detected: Optional[bool] = None  # True if idet found interlaced content
    interlace_tff_ratio: Optional[float] = None  # Ratio of TFF frames (0-1)
    audio_tracks: list[dict] = field(default_factory=list)
    subtitle_tracks: list[dict] = field(default_factory=list)
    container_bitrate_kbps: Optional[int] = None
    is_hdr: Optional[bool] = None
    hdr_format: Optional[str] = None

    @property
    def resolution_label(self) -> str:
        if self.video_height is None:
            return "unknown"
        if self.video_height >= 2160:
            return "4K"
        if self.video_height >= 1080:
            return "1080p"
        if self.video_height >= 720:
            return "720p"
        if self.video_height >= 480:
            return "480p"
        return f"{self.video_height}p"

    @property
    def is_already_av1(self) -> bool:
        return self.video_codec and "av1" in self.video_codec.lower()

    @property
    def is_interlaced(self) -> bool:
        """Check if video is interlaced.

        Prioritizes actual frame analysis (interlace_detected) over metadata (field_order).
        Many older shows have incorrect metadata saying "progressive" when they're actually
        interlaced, so we use ffmpeg's idet filter for detection.
        """
        # If we've run idet detection, trust that over metadata
        if self.interlace_detected is not None:
            return self.interlace_detected
        # Fall back to metadata-based check
        if not self.video_field_order:
            return False
        # progressive = not interlaced
        # tt = top field first, bb = bottom field first
        # tb = top coded first, bottom displayed first
        # bt = bottom coded first, top displayed first
        return self.video_field_order.lower() not in ("progressive", "unknown", "")


@dataclass
class ClassificationResult:
    """Result of source quality classification."""

    tier: SourceTier
    confidence: str  # "high", "medium", "low"
    reasons: list[str]
    recommended_settings: TierSettings


@dataclass
class QualityMetrics:
    """Quality metrics from SSIM/PSNR/VMAF comparison."""

    ssim: Optional[float] = None  # 0-1 scale, 1 = identical
    psnr: Optional[float] = None  # dB, higher = better, typically 30-50
    vmaf: Optional[float] = None  # 0-100 scale, 100 = perfect
    sample_duration_sec: Optional[float] = None  # Duration of sampled clip
    sample_start_sec: Optional[float] = None  # Start position of sample

    @property
    def is_acceptable(self) -> bool:
        """Check if quality metrics meet minimum thresholds."""
        # Thresholds for "watchable quality"
        # SSIM >= 0.95 is generally visually transparent
        # VMAF >= 90 is excellent, >= 80 is good
        if self.vmaf is not None:
            return self.vmaf >= 85
        if self.ssim is not None:
            return self.ssim >= 0.92
        return True  # No metrics = assume OK

    @property
    def quality_grade(self) -> str:
        """Return a letter grade for quality."""
        if self.vmaf is not None:
            if self.vmaf >= 95:
                return "A+"
            if self.vmaf >= 90:
                return "A"
            if self.vmaf >= 85:
                return "B"
            if self.vmaf >= 80:
                return "C"
            return "D"
        if self.ssim is not None:
            if self.ssim >= 0.98:
                return "A+"
            if self.ssim >= 0.96:
                return "A"
            if self.ssim >= 0.94:
                return "B"
            if self.ssim >= 0.92:
                return "C"
            return "D"
        if self.psnr is not None:
            # PSNR grades (typical video quality ranges)
            if self.psnr >= 45:
                return "A+"
            if self.psnr >= 40:
                return "A"
            if self.psnr >= 35:
                return "B"
            if self.psnr >= 30:
                return "C"
            return "D"
        return "?"


@dataclass
class OutlierThresholds:
    """Thresholds for flagging encodes as outliers requiring review."""

    # Quality thresholds (below these = outlier)
    # Aligned with is_acceptable: VMAF >= 85 is acceptable
    min_vmaf: float = 85.0
    min_ssim: float = 0.92
    min_psnr: float = 32.0

    # Compression ratio thresholds (output/source)
    min_compression_ratio: float = 0.15  # Too aggressive (<15% of original)
    max_compression_ratio: float = 0.75  # Too weak (>75% of original)

    # Bitrate thresholds by resolution (kbps)
    # If output is below min or above max for resolution, flag it
    min_bitrate_1080p: int = 800
    max_bitrate_1080p: int = 6000
    min_bitrate_720p: int = 500
    max_bitrate_720p: int = 4000
    min_bitrate_480p: int = 300
    max_bitrate_480p: int = 2000


# Default thresholds
DEFAULT_OUTLIER_THRESHOLDS = OutlierThresholds()


@dataclass
class OutlierResult:
    """Result of outlier check with reasons."""

    is_outlier: bool
    reasons: list[str]
    metrics: Optional[QualityMetrics] = None
    compression_ratio: Optional[float] = None
    output_bitrate_kbps: Optional[int] = None


def check_for_outliers(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    metrics: Optional[QualityMetrics] = None,
    thresholds: OutlierThresholds = DEFAULT_OUTLIER_THRESHOLDS,
) -> OutlierResult:
    """Check if an encode is an outlier requiring review.

    Args:
        source_path: Original video file
        encoded_path: Encoded video file
        metrics: Pre-computed quality metrics (if available)
        thresholds: Thresholds for outlier detection

    Returns:
        OutlierResult with is_outlier flag and reasons
    """
    reasons: list[str] = []

    # Get file sizes
    source_size = source_path.stat().st_size
    encoded_size = encoded_path.stat().st_size
    compression_ratio = encoded_size / source_size

    # Check compression ratio
    if compression_ratio < thresholds.min_compression_ratio:
        reasons.append(f"Aggressive compression ({compression_ratio:.1%} of original)")
    elif compression_ratio > thresholds.max_compression_ratio:
        reasons.append(f"Weak compression ({compression_ratio:.1%} of original)")

    # Get encoded file info
    encoded_info = probe_media(encoded_path)
    output_bitrate = encoded_info.video_bitrate_kbps if encoded_info else None

    # Check bitrate for resolution
    if encoded_info and output_bitrate:
        height = encoded_info.video_height or 0
        if height >= 1080:
            if output_bitrate < thresholds.min_bitrate_1080p:
                reasons.append(f"Low bitrate for 1080p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_1080p:
                reasons.append(f"High bitrate for 1080p ({output_bitrate} kbps)")
        elif height >= 720:
            if output_bitrate < thresholds.min_bitrate_720p:
                reasons.append(f"Low bitrate for 720p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_720p:
                reasons.append(f"High bitrate for 720p ({output_bitrate} kbps)")
        elif height >= 480:
            if output_bitrate < thresholds.min_bitrate_480p:
                reasons.append(f"Low bitrate for 480p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_480p:
                reasons.append(f"High bitrate for 480p ({output_bitrate} kbps)")

    # Check quality metrics
    if metrics:
        if metrics.vmaf is not None and metrics.vmaf < thresholds.min_vmaf:
            reasons.append(f"Low VMAF ({metrics.vmaf:.1f} < {thresholds.min_vmaf})")
        if metrics.ssim is not None and metrics.ssim < thresholds.min_ssim:
            reasons.append(f"Low SSIM ({metrics.ssim:.3f} < {thresholds.min_ssim})")
        if metrics.psnr is not None and metrics.psnr < thresholds.min_psnr:
            reasons.append(f"Low PSNR ({metrics.psnr:.1f} < {thresholds.min_psnr})")

    return OutlierResult(
        is_outlier=len(reasons) > 0,
        reasons=reasons,
        metrics=metrics,
        compression_ratio=compression_ratio,
        output_bitrate_kbps=output_bitrate,
    )


def find_ffprobe() -> Optional[str]:
    """Find ffprobe executable."""
    for candidate in ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "ffprobe"]:
        if shutil.which(candidate):
            return candidate
    return None


def find_ffmpeg() -> Optional[str]:
    """Find ffmpeg executable."""
    for candidate in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]:
        if shutil.which(candidate):
            return candidate
    return None


def choose_output_format(info: MediaInfo) -> str:
    """Select pixel format; prefer 10-bit only when useful.

    - If source is >8-bit or HDR, keep 10-bit (yuv420p10le) to avoid banding.
    - Otherwise use 8-bit (yuv420p) to save bits and speed.
    """
    if info.video_bit_depth and info.video_bit_depth > 8:
        return "yuv420p10le"
    if info.is_hdr:
        return "yuv420p10le"
    return "yuv420p"


# =============================================================================
# VMAF Sampling Helpers
# =============================================================================


def window_bitrate(path: pathlib.Path, start: float, duration: float = 5.0) -> Optional[float]:
    """Approximate bitrate (bps) in a short window using ffprobe packets.

    Uses: ffprobe -show_packets -read_intervals "{start}%+{duration}" and sums packet sizes.
    """
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=size",
        "-of",
        "csv=p=0",
        "-read_intervals",
        f"{start}%+{duration}",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        total_bytes = 0
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                total_bytes += int(line.strip())
            except ValueError:
                continue
        if duration <= 0:
            return None
        return (total_bytes * 8) / duration
    except Exception:
        return None


def pick_sample_times(
    info: MediaInfo,
    count: int = 3,
    sample_len: float = 8.0,
    motion_aware: bool = True,
) -> list[float]:
    """Pick sample start times (seconds).

    Motion-aware: probe a handful of windows and choose those with highest bitrate.
    Fallback: evenly spaced 25/50/75%. Never exceed duration - sample_len.
    """
    duration = info.duration_seconds or 0
    if duration <= 0:
        return []

    def clamp_ts(ts: float) -> float:
        return max(0.0, min(ts, max(0.0, duration - sample_len)))

    if not motion_aware or duration < sample_len * 2:
        pct = [0.25, 0.5, 0.75][:count]
        return [clamp_ts(duration * p) for p in pct]

    # Probe 8 candidate windows across the file
    candidates = []
    steps = max(count * 3, 8)
    for i in range(1, steps + 1):
        p = i / (steps + 1)
        start = clamp_ts(duration * p)
        br = window_bitrate(info.path, start, duration=5.0)
        if br is not None:
            candidates.append((br, start))

    if not candidates:
        pct = [0.25, 0.5, 0.75][:count]
        return [clamp_ts(duration * p) for p in pct]

    candidates.sort(reverse=True, key=lambda x: x[0])
    chosen: List[float] = []
    for _, ts in candidates:
        if len(chosen) >= count:
            break
        # Keep simple spacing: avoid picks within sample_len of each other
        if all(abs(ts - c) > sample_len for c in chosen):
            chosen.append(ts)

    # If we didn't get enough distinct windows, pad with spaced positions
    if len(chosen) < count:
        pct = [0.25, 0.5, 0.75]
        for p in pct:
            if len(chosen) >= count:
                break
            ts = clamp_ts(duration * p)
            if all(abs(ts - c) > sample_len for c in chosen):
                chosen.append(ts)

    return chosen[:count]


def encode_sample_clip(
    path: pathlib.Path,
    settings: TierSettings,
    info: MediaInfo,
    start: float,
    duration: float,
    max_height: Optional[int],
) -> tuple[Optional[pathlib.Path], Optional[tuple[int, int]]]:
    """Encode a short sample with current settings; return path and (w,h)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, None

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="av1sample_"))
    out_path = tmp_dir / "sample.mkv"

    vf_parts: list[str] = []
    if info.is_interlaced:
        vf_parts.append("bwdif=mode=0:parity=-1:deint=0")
    if settings.denoise and settings.denoise in DENOISE_FILTERS:
        vf_parts.append(DENOISE_FILTERS[settings.denoise])
    vf_parts = apply_downscale_filter(vf_parts, info, max_height)
    pfmt = choose_output_format(info)
    vf_parts.append(f"format={pfmt}")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        ",".join(vf_parts),
        "-c:v",
        "libsvtav1",
        "-crf",
        str(settings.crf),
        "-preset",
        str(settings.preset),
        "-svtav1-params",
        f"film-grain={settings.film_grain}",
        "-y",
        str(out_path),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except Exception:
        return None, None

    # Probe encoded dimensions
    ffprobe = find_ffprobe()
    if not ffprobe:
        return out_path, None
    try:
        res = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        st = data.get("streams", [{}])[0]
        w = st.get("width")
        h = st.get("height")
        if w and h:
            return out_path, (int(w), int(h))
    except Exception:
        pass
    return out_path, None


def compute_vmaf_score(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start: float,
    duration: float,
    encoded_size: Optional[tuple[int, int]] = None,
) -> Optional[float]:
    """Compute VMAF for a short clip; returns mean VMAF."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_error("ffmpeg_missing", stage="vmaf")
        return None

    w, h = encoded_size if encoded_size else (None, None)
    scale_ref = f"scale={w}:{h}:flags=bicubic" if w and h else "format=yuv420p"

    tmp_json = pathlib.Path(tempfile.mkdtemp(prefix="vmaf_")) / "vmaf.json"

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(encoded_path),
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(source_path),
        "-lavfi",
        f"[1:v]{scale_ref}[ref];[0:v][ref]libvmaf=log_fmt=json:log_path={tmp_json}",
        "-f",
        "null",
        "-",
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=240)
        data = json.loads(tmp_json.read_text())
        frames = data.get("frames", [])
        if not frames:
            return None
        scores = [f.get("metrics", {}).get("vmaf") for f in frames if f.get("metrics", {}).get("vmaf") is not None]
        if not scores:
            return None
        return sum(scores) / len(scores)
    except Exception:
        return None


def sample_vmaf(
    info: MediaInfo,
    settings: TierSettings,
    max_height: Optional[int],
    sample_count: int = 3,
    sample_length: float = 8.0,
    motion_aware: bool = True,
) -> dict:
    """Compute median/min VMAF across several samples.

    Returns {"median": float, "min": float, "samples": [scores], "timestamps": [...]} or {} on failure.
    """
    times = pick_sample_times(info, count=sample_count, sample_len=sample_length, motion_aware=motion_aware)
    if not times:
        return {}

    scores = []
    for ts in times:
        enc_path, enc_size = encode_sample_clip(info.path, settings, info, ts, sample_length, max_height)
        if not enc_path:
            continue
        vmaf = compute_vmaf_score(info.path, enc_path, ts, sample_length, encoded_size=enc_size)
        try:
            enc_path.unlink(missing_ok=True)
            enc_path.parent.rmdir()
        except Exception:
            pass
        if vmaf is not None:
            scores.append(vmaf)

    if not scores:
        return {}

    scores.sort()
    median = scores[len(scores) // 2]
    return {
        "median": median,
        "min": min(scores),
        "samples": scores,
        "timestamps": times,
    }


def apply_downscale_filter(
    input_filters: list[str],
    info: MediaInfo,
    max_height: Optional[int],
) -> list[str]:
    """Append a scale filter if source is taller than max_height.

    Never upscales; if max_height is None or source height is missing/<=max, returns unchanged list.
    """
    if not max_height or not info.video_height:
        return input_filters

    if info.video_height <= max_height:
        return input_filters

    # Keep width mod-2, preserve aspect
    scale_expr = f"scale=-2:{max_height}"
    input_filters = input_filters.copy()
    input_filters.append(scale_expr)
    return input_filters


def probe_media(path: pathlib.Path) -> Optional[MediaInfo]:
    """Run ffprobe and parse results."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        log_error("ffprobe_missing", path=str(path))
        return None

    cmd = [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        log_error("ffprobe_failed", path=str(path), error=str(e))
        return None

    info = MediaInfo(path=path)

    # Parse format info
    fmt = data.get("format", {})
    if duration := fmt.get("duration"):
        try:
            info.duration_seconds = float(duration)
        except ValueError:
            pass

    if bitrate := fmt.get("bit_rate"):
        try:
            info.container_bitrate_kbps = int(bitrate) // 1000
        except ValueError:
            pass

    # Parse streams
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")

        if codec_type == "video":
            info.video_codec = stream.get("codec_name")
            info.video_width = stream.get("width")
            info.video_height = stream.get("height")

            # Bit depth
            if bits := stream.get("bits_per_raw_sample"):
                try:
                    info.video_bit_depth = int(bits)
                except ValueError:
                    pass
            if info.video_bit_depth is None:
                pix_fmt = (stream.get("pix_fmt") or "").lower()
                if "p10" in pix_fmt or "10le" in pix_fmt:
                    info.video_bit_depth = 10
                elif "p12" in pix_fmt:
                    info.video_bit_depth = 12
                else:
                    info.video_bit_depth = 8

            # Bitrate
            if br := stream.get("bit_rate"):
                try:
                    info.video_bitrate_kbps = int(br) // 1000
                except ValueError:
                    pass

            # Frame rate
            if fps_str := stream.get("avg_frame_rate"):
                try:
                    if "/" in fps_str:
                        num, den = fps_str.split("/")
                        info.video_framerate = float(num) / float(den)
                    else:
                        info.video_framerate = float(fps_str)
                except (ValueError, ZeroDivisionError):
                    pass

            # Field order (interlaced detection)
            info.video_field_order = stream.get("field_order")

        elif codec_type == "audio":
            track = {
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "language": (stream.get("tags") or {}).get("language"),
            }
            if br := stream.get("bit_rate"):
                try:
                    track["bitrate_kbps"] = int(br) // 1000
                except ValueError:
                    pass
            info.audio_tracks.append(track)

        elif codec_type == "subtitle":
            track = {
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "language": (stream.get("tags") or {}).get("language"),
            }
            info.subtitle_tracks.append(track)

    # If no video bitrate found, estimate from container
    if info.video_bitrate_kbps is None and info.container_bitrate_kbps:
        # Rough estimate: subtract ~200kbps per audio track
        audio_estimate = len(info.audio_tracks) * 200
        info.video_bitrate_kbps = max(0, info.container_bitrate_kbps - audio_estimate)

    return info


def detect_interlacing(path: pathlib.Path, num_frames: int = 500) -> tuple[bool, float]:
    """Detect interlacing by analyzing actual frame content using ffmpeg's idet filter.

    Many older shows have incorrect metadata (field_order=progressive) when the
    content is actually interlaced. This function analyzes actual frames to detect
    interlacing artifacts.

    Args:
        path: Path to video file
        num_frames: Number of frames to analyze (default 500, ~17 seconds at 30fps)

    Returns:
        Tuple of (is_interlaced, tff_ratio) where:
        - is_interlaced: True if >30% of frames are detected as interlaced
        - tff_ratio: Ratio of TFF (top-field-first) to total interlaced frames
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, 0.0

    cmd = [
        ffmpeg,
        "-i", str(path),
        "-vf", "idet",
        "-frames:v", str(num_frames),
        "-an",
        "-f", "null",
        "-"
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=60
        )
        # idet output is in stderr
        output = result.stderr
    except subprocess.TimeoutExpired:
        return False, 0.0
    except Exception:
        return False, 0.0

    # Parse idet output - look for "Multi frame detection" line which is more reliable
    # Example: [Parsed_idet_0 @ 0x...] Multi frame detection: TFF:   368 BFF:     0 Progressive:   133 Undetermined:     0
    # Note: There may be multiple lines (intermediate and final), we want the LAST one
    tff = 0
    bff = 0
    progressive = 0

    for line in output.split("\n"):
        if "Multi frame detection:" in line:
            # Parse the counts - keep overwriting to get the final (most accurate) line
            tff_match = re.search(r"TFF:\s*(\d+)", line)
            bff_match = re.search(r"BFF:\s*(\d+)", line)
            prog_match = re.search(r"Progressive:\s*(\d+)", line)

            if tff_match:
                tff = int(tff_match.group(1))
            if bff_match:
                bff = int(bff_match.group(1))
            if prog_match:
                progressive = int(prog_match.group(1))
            # Don't break - continue to get the last (final) line

    total_interlaced = tff + bff
    total_frames = total_interlaced + progressive

    if total_frames == 0:
        return False, 0.0

    interlaced_ratio = total_interlaced / total_frames
    tff_ratio = tff / total_interlaced if total_interlaced > 0 else 0.0

    # Consider interlaced if >30% of frames are detected as interlaced
    # This threshold catches partially-interlaced content (telecined, etc.)
    is_interlaced = interlaced_ratio > 0.30

    return is_interlaced, tff_ratio


def probe_media_with_interlace_detection(
    path: pathlib.Path, detect_interlace: bool = True
) -> Optional[MediaInfo]:
    """Probe media file and optionally detect interlacing via frame analysis.

    This is the recommended function for getting MediaInfo when you need
    accurate interlacing detection (e.g., before encoding).
    """
    info = probe_media(path)
    if info is None:
        return None

    if detect_interlace:
        is_interlaced, tff_ratio = detect_interlacing(path)
        info.interlace_detected = is_interlaced
        info.interlace_tff_ratio = tff_ratio

    return info


def classify_source(info: MediaInfo, show_config: Optional[dict] = None, vmaf_hint: Optional[float] = None) -> ClassificationResult:
    """Classify source quality and recommend encoding settings.

    The classification is based on heuristics about bitrate efficiency,
    codec age, and resolution vs. likely content era.
    """
    reasons: list[str] = []

    # Check for manual override
    if show_config and "tier" in show_config:
        tier_str = show_config["tier"].lower()
        for tier in SourceTier:
            if tier.value == tier_str:
                reasons.append(f"Manual override from config: {tier_str}")
                return ClassificationResult(
                    tier=tier,
                    confidence="high",
                    reasons=reasons,
                    recommended_settings=TIER_SETTINGS[tier],
                )

    # Start with a score-based approach
    # Higher score = worse quality source = more aggressive compression
    score = 0


    # Codec-based scoring
    codec = (info.video_codec or "").lower()
    if codec in ("mpeg2video", "mpeg2"):
        score += 3
        reasons.append("MPEG-2 codec suggests older/legacy source")
    elif codec in ("mpeg4", "msmpeg4", "divx", "xvid"):
        score += 3
        reasons.append("Legacy MPEG-4/DivX codec")
    elif codec == "vc1":
        score += 1
        reasons.append("VC-1 codec (older HD era)")
    elif codec in ("h264", "avc"):
        # H.264 is neutral - could be anything
        pass
    elif codec in ("hevc", "h265"):
        score -= 1
        reasons.append("HEVC suggests modern encode")
    elif "av1" in codec:
        score -= 2
        reasons.append("Already AV1 - likely high quality source")

    # Bitrate efficiency scoring
    # Calculate bits per pixel per frame as a quality indicator
    if info.video_bitrate_kbps and info.video_width and info.video_height:
        pixels = info.video_width * info.video_height
        fps = info.video_framerate or 24
        bpp = (info.video_bitrate_kbps * 1000) / (pixels * fps)

        # bpp ranges (very rough):
        # < 0.02: heavily compressed
        # 0.02-0.05: typical streaming
        # 0.05-0.10: high quality streaming/blu-ray encode
        # > 0.10: very high bitrate or inefficient

        if bpp > 0.15:
            score += 2
            reasons.append(f"High bpp ({bpp:.3f}) suggests noisy/inefficient source")
        elif bpp > 0.10:
            score += 1
            reasons.append(f"Elevated bpp ({bpp:.3f})")
        elif bpp < 0.02:
            score -= 1
            reasons.append(f"Low bpp ({bpp:.3f}) - already well compressed")

    # Resolution vs bitrate sanity check
    # 1080p should typically be 3-8 Mbps for good quality
    # If it's much higher, source is probably noisy
    if info.video_height and info.video_bitrate_kbps:
        if info.video_height >= 1080:
            if info.video_bitrate_kbps > 15000:
                score += 2
                reasons.append(f"Very high bitrate ({info.video_bitrate_kbps}kbps) for {info.resolution_label}")
            elif info.video_bitrate_kbps > 10000:
                score += 1
                reasons.append(f"High bitrate ({info.video_bitrate_kbps}kbps) for {info.resolution_label}")
            elif info.video_bitrate_kbps < 3000:
                score -= 1
                reasons.append(f"Efficient bitrate ({info.video_bitrate_kbps}kbps)")
        elif info.video_height >= 720:
            if info.video_bitrate_kbps > 8000:
                score += 2
                reasons.append(f"Very high bitrate for 720p ({info.video_bitrate_kbps}kbps)")
        elif info.video_height < 720:
            # SD content - likely upscaled if file is "HD"
            score += 2
            reasons.append(f"Sub-HD resolution ({info.resolution_label}) - possibly upscaled")

    # Optional VMAF hint (from quick sample) can sway aggressiveness
    if vmaf_hint is not None:
        if vmaf_hint >= 95:
            score -= 1
            reasons.append(f"VMAF hint high ({vmaf_hint:.1f}) -> more aggressive")
        elif vmaf_hint < 85:
            score += 1
            reasons.append(f"VMAF hint low ({vmaf_hint:.1f}) -> less aggressive")

    # Convert score to tier
    if score <= -1:
        tier = SourceTier.PRISTINE
    elif score <= 1:
        tier = SourceTier.GOOD
    elif score <= 3:
        tier = SourceTier.MEDIOCRE
    else:
        tier = SourceTier.POOR

    # Determine confidence
    if len(reasons) >= 3:
        confidence = "high"
    elif len(reasons) >= 2:
        confidence = "medium"
    else:
        confidence = "low"
        reasons.append("Limited metadata available for classification")

    if not reasons:
        reasons.append("Default classification - no strong indicators")

    return ClassificationResult(
        tier=tier,
        confidence=confidence,
        reasons=reasons,
        recommended_settings=TIER_SETTINGS[tier],
    )


def adjust_tier_with_vmaf(classification: ClassificationResult, vmaf_stats: dict) -> ClassificationResult:
    """Adjust tier based on VMAF statistics (median/min)."""

    median = vmaf_stats.get("median")
    vmin = vmaf_stats.get("min")
    tier = classification.tier

    def more_aggressive(t: SourceTier) -> SourceTier:
        order = [SourceTier.POOR, SourceTier.MEDIOCRE, SourceTier.GOOD, SourceTier.PRISTINE]
        idx = order.index(t)
        return order[max(0, idx - 1)]

    def less_aggressive(t: SourceTier) -> SourceTier:
        order = [SourceTier.POOR, SourceTier.MEDIOCRE, SourceTier.GOOD, SourceTier.PRISTINE]
        idx = order.index(t)
        return order[min(len(order) - 1, idx + 1)]

    adjusted = tier
    reasons = list(classification.reasons)

    if median is not None and median >= 94:
        adjusted = more_aggressive(adjusted)
        reasons.append(f"VMAF median {median:.1f} -> more aggressive")
    if (median is not None and median < 86) or (vmin is not None and vmin < 82):
        adjusted = less_aggressive(adjusted)
        reasons.append(
            f"VMAF low (median {median:.1f if median is not None else 'n/a'}, min {vmin:.1f if vmin is not None else 'n/a'}) -> less aggressive"
        )

    return ClassificationResult(
        tier=adjusted,
        confidence=classification.confidence,
        reasons=reasons,
        recommended_settings=TIER_SETTINGS[adjusted],
    )


def is_english_track(track: dict) -> bool:
    """Check if a track is English."""
    lang = (track.get("language") or "").lower()
    return lang in ("eng", "en", "english")


def is_undefined_track(track: dict) -> bool:
    """Check if a track has no/undefined language."""
    lang = track.get("language")
    return lang is None or lang.lower() in ("", "und", "unk", "unknown")


def select_audio_tracks(audio_tracks: list[dict]) -> list[dict]:
    """Select which audio tracks to keep.

    Priority:
    1. English tracks only (if any exist)
    2. Undefined tracks as fallback (if no English)
    3. First track as safety (if nothing else matches)
    """
    if not audio_tracks:
        return []

    english = [t for t in audio_tracks if is_english_track(t)]
    if english:
        return english

    undefined = [t for t in audio_tracks if is_undefined_track(t)]
    if undefined:
        return undefined

    # Safety: keep first track
    return [audio_tracks[0]]


def select_subtitle_tracks(subtitle_tracks: list[dict]) -> list[dict]:
    """Select which subtitle tracks to keep.

    Keep only English text-based subtitles (SRT, MOV_TEXT, SUBRIP).
    Drop image-based (PGS, VobSub) and styled (ASS) formats.
    """
    TEXT_SUBTITLE_CODECS = {"subrip", "srt", "mov_text", "text"}

    selected = []
    for track in subtitle_tracks:
        codec = (track.get("codec") or "").lower()

        # Only keep text-based formats compatible with MP4
        if codec not in TEXT_SUBTITLE_CODECS:
            continue

        # Only keep English or undefined
        if is_english_track(track) or is_undefined_track(track):
            selected.append(track)

    # If we have English, drop undefined
    english = [t for t in selected if is_english_track(t)]
    if english:
        return english

    return selected


def get_opus_target_bitrate(channels: int) -> int:
    """Get target Opus bitrate for channel count."""
    target_bitrates = {
        1: 64,    # mono
        2: 128,   # stereo
        6: 256,   # 5.1
        8: 384,   # 7.1
    }
    return target_bitrates.get(channels, min(384, 128 + (channels - 2) * 48))


def build_ffmpeg_command(
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    settings: TierSettings,
    media_info: MediaInfo,
    max_height: Optional[int] = None,
    hw_decode: bool = False,
    hw_encode: bool = False,
) -> list[str]:
    """Build ffmpeg command for AV1 encoding."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")

    cmd = [ffmpeg, "-hide_banner"]

    if hw_decode:
        system = platform.system().lower()
        if system == "darwin":
            cmd.extend(["-hwaccel", "videotoolbox"])
        elif system == "linux":
            cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

    cmd.extend(["-i", str(input_path)])

    # Select tracks to keep
    audio_to_keep = select_audio_tracks(media_info.audio_tracks)
    subs_to_keep = select_subtitle_tracks(media_info.subtitle_tracks)

    # Map video stream (first video)
    cmd.extend(["-map", "0:v:0"])

    # Map selected audio streams
    for track in audio_to_keep:
        cmd.extend(["-map", f"0:{track['index']}"])

    # Map selected subtitle streams
    for track in subs_to_keep:
        cmd.extend(["-map", f"0:{track['index']}"])

    # Video filter chain
    vf_parts: list[str] = []

    # Deinterlace filter (if needed) - must be first in chain
    if media_info.is_interlaced:
        # bwdif: bob weaver deinterlacing filter (high quality)
        # mode=1: output one frame for each field (doubles framerate)
        # mode=0: output one frame for each frame (preserves framerate)
        # Using mode=0 to preserve original framerate
        vf_parts.append("bwdif=mode=0:parity=-1:deint=0")

    # Denoise filter (if enabled)
    if settings.denoise and settings.denoise in DENOISE_FILTERS:
        vf_parts.append(DENOISE_FILTERS[settings.denoise])

    # Downscale if needed (never upscale)
    vf_parts = apply_downscale_filter(vf_parts, media_info, max_height)

    # Choose pixel format based on source
    vf_parts.append(f"format={choose_output_format(media_info)}")

    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])

    # Video encoding
    if hw_encode and platform.system().lower() == "darwin":
        cmd.extend(["-c:v", "av1_videotoolbox"])
        cmd.extend(["-b:v", "0"])
        cmd.extend(["-crf", str(settings.crf)])
    else:
        cmd.extend(["-c:v", "libsvtav1"])
        cmd.extend(["-crf", str(settings.crf)])
        cmd.extend(["-preset", str(settings.preset)])
        svt_params = [f"film-grain={settings.film_grain}"]
        cmd.extend(["-svtav1-params", ":".join(svt_params)])

    # Audio encoding - process each selected track
    for i, track in enumerate(audio_to_keep):
        channels = track.get("channels") or 2
        codec = (track.get("codec") or "").lower()
        source_bitrate = track.get("bitrate_kbps")
        target_bitrate = get_opus_target_bitrate(channels)

        # Decide: passthrough or encode
        passthrough = False

        if codec == "opus":
            # Already Opus - pass through if at or below our target
            if source_bitrate is None or source_bitrate <= target_bitrate * 1.2:
                passthrough = True

        elif codec == "aac":
            # AAC - pass through only if at or below our target
            if source_bitrate and source_bitrate <= target_bitrate:
                passthrough = True

        # AC3, EAC3, DTS, MP3, lossless - always convert to Opus

        if passthrough:
            cmd.extend([f"-c:a:{i}", "copy"])
        else:
            cmd.extend([f"-c:a:{i}", "libopus"])
            cmd.extend([f"-b:a:{i}", f"{target_bitrate}k"])
            # For surround sound (>2 channels):
            # - Use mapping_family 1 (required for multichannel Opus)
            # - Force standard 5.1 layout (fixes 5.1(side) incompatibility)
            if channels > 2:
                cmd.extend([f"-mapping_family", "1"])
                if channels == 6:
                    cmd.extend([f"-af:a:{i}", "channelmap=channel_layout=5.1"])
                elif channels == 8:
                    cmd.extend([f"-af:a:{i}", "channelmap=channel_layout=7.1"])
            cmd.extend([f"-ac:{i}", str(channels)])

    # If no audio tracks selected, this shouldn't happen but handle gracefully
    if not audio_to_keep:
        cmd.extend(["-an"])

    # Subtitle encoding - copy text-based subs (convert to mov_text for MP4)
    for i, track in enumerate(subs_to_keep):
        cmd.extend([f"-c:s:{i}", "mov_text"])

    # If no subtitles, explicitly disable
    if not subs_to_keep:
        cmd.extend(["-sn"])

    # Output
    cmd.extend(["-y", str(output_path)])

    return cmd


def format_duration(seconds: Optional[float]) -> str:
    """Format duration as HH:MM:SS."""
    if seconds is None:
        return "unknown"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bitrate(kbps: Optional[int]) -> str:
    """Format bitrate nicely."""
    if kbps is None:
        return "unknown"
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps} kbps"


def collect_video_files(path: pathlib.Path) -> list[pathlib.Path]:
    """Collect video files from path (file or directory)."""
    if path.is_file():
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            return [path]
        return []

    if path.is_dir():
        files = []
        for f in sorted(path.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                files.append(f)
        return files

    return []


def load_show_config(config_path: Optional[pathlib.Path] = None) -> dict[str, dict]:
    """Load show configuration overrides."""
    if config_path is None:
        # Look for config in standard locations
        candidates = [
            pathlib.Path("show_config.json"),
            pathlib.Path(__file__).parent / "show_config.json",
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break

    if config_path and config_path.exists():
        try:
            with config_path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log_warn("show_config_load_failed", config=str(config_path), error=str(e))

    return {}


def guess_show_name(path: pathlib.Path) -> Optional[str]:
    """Try to extract show name from path."""
    # Look for "Show Name/Season N" pattern
    parts = path.parts
    for i, part in enumerate(parts):
        if part.lower().startswith("season"):
            if i > 0:
                return parts[i - 1]
    return None


# =============================================================================
# Quality Verification (SSIM/PSNR/VMAF)
# =============================================================================


def measure_ssim_psnr(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start_sec: float = 0,
    duration_sec: float = 0,
) -> tuple[Optional[float], Optional[float]]:
    """Measure SSIM and PSNR between source and encoded video.

    Args:
        source_path: Original video file
        encoded_path: Encoded video file
        start_sec: Start position for sampling (0 = full file)
        duration_sec: Duration to sample (0 = full file)

    Returns:
        Tuple of (ssim, psnr) or (None, None) on failure
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, None

    # Build ffmpeg command for SSIM/PSNR measurement
    # The encoded file is the "main" input, source is the "reference"
    cmd = [ffmpeg, "-hide_banner"]

    # Add seeking if sampling
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])

    cmd.extend(["-i", str(encoded_path)])

    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])

    cmd.extend(["-i", str(source_path)])

    # Add duration limit if sampling
    if duration_sec > 0:
        cmd.extend(["-t", str(duration_sec)])

    # Use lavfi to compute both SSIM and PSNR
    # Scale encoded to match source resolution if different
    filter_complex = (
        "[0:v]scale=flags=bicubic[enc];"
        "[1:v]scale=flags=bicubic[ref];"
        "[enc][ref]ssim=stats_file=-;[enc][ref]psnr=stats_file=-"
    )

    # SSIM filter - scale both to same format for comparison
    # Convert both to yuv420p to ensure format compatibility
    ssim_filter = "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];[enc][ref]ssim=stats_file=-"
    cmd.extend([
        "-lavfi", ssim_filter,
        "-f", "null", "-"
    ])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = result.stderr  # ffmpeg outputs stats to stderr

        # Parse SSIM from output
        # Format options:
        # - "SSIM All:0.987654 (39.123456)" - from stats file
        # - "SSIM Y:0.940 ... All:0.954" - from filter summary
        ssim = None
        # Try stats file format first
        ssim_match = re.search(r"SSIM All:([0-9.]+)", output)
        if ssim_match:
            ssim = float(ssim_match.group(1))
        else:
            # Try filter summary format (All: followed by value in parens)
            ssim_match = re.search(r"All:([0-9.]+)\s*\([0-9.]+\)", output)
            if ssim_match:
                ssim = float(ssim_match.group(1))

    except Exception as e:
        log_warn("ssim_failed", source=str(source_path), encoded=str(encoded_path), error=str(e))
        return None, None

    # Run PSNR separately
    psnr = None
    cmd_psnr = [ffmpeg, "-hide_banner"]
    if start_sec > 0:
        cmd_psnr.extend(["-ss", str(start_sec)])
    cmd_psnr.extend(["-i", str(encoded_path)])
    if start_sec > 0:
        cmd_psnr.extend(["-ss", str(start_sec)])
    cmd_psnr.extend(["-i", str(source_path)])
    if duration_sec > 0:
        cmd_psnr.extend(["-t", str(duration_sec)])
    # PSNR filter - normalize formats for comparison
    psnr_filter = "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];[enc][ref]psnr=stats_file=-"
    cmd_psnr.extend([
        "-lavfi", psnr_filter,
        "-f", "null", "-"
    ])

    try:
        result = subprocess.run(cmd_psnr, capture_output=True, text=True, check=False)
        output = result.stderr
        # Format: "PSNR average:37.123456 min:35.123456 max:45.123456"
        psnr_match = re.search(r"PSNR.*average:([0-9.]+)", output)
        if psnr_match:
            psnr = float(psnr_match.group(1))
    except Exception:
        pass

    return ssim, psnr


def measure_vmaf(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    start_sec: float = 0,
    duration_sec: float = 0,
    model: str = "vmaf_v0.6.1",
) -> Optional[float]:
    """Measure VMAF between source and encoded video.

    VMAF (Video Multi-method Assessment Fusion) is a perceptual quality metric
    developed by Netflix. Scores range 0-100 where 100 is perfect.

    Args:
        source_path: Original video file (reference)
        encoded_path: Encoded video file (distorted)
        start_sec: Start position for sampling
        duration_sec: Duration to sample (0 = full file, expensive!)
        model: VMAF model to use

    Returns:
        VMAF score (0-100) or None on failure
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    # Build ffmpeg command
    cmd = [ffmpeg, "-hide_banner"]

    # Input 0: encoded (distorted)
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(encoded_path)])

    # Input 1: source (reference)
    if start_sec > 0:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend(["-i", str(source_path)])

    if duration_sec > 0:
        cmd.extend(["-t", str(duration_sec)])

    # VMAF filter - [0] is distorted, [1] is reference
    # Normalize pixel formats for comparison
    # Use log_fmt=json for easier parsing
    vmaf_filter = (
        f"[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];"
        f"[enc][ref]libvmaf=model=version={model}:log_fmt=json:log_path=/dev/stdout"
    )

    cmd.extend(["-lavfi", vmaf_filter, "-f", "null", "-"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)

        # Parse VMAF from JSON output (written to stdout via log_path)
        # The JSON is embedded in stdout
        output = result.stdout

        # Find the JSON block
        json_match = re.search(r'\{[\s\S]*"VMAF score"[\s\S]*\}', output)
        if json_match:
            try:
                vmaf_data = json.loads(json_match.group(0))
                if "pooled_metrics" in vmaf_data:
                    return vmaf_data["pooled_metrics"]["vmaf"]["mean"]
            except (json.JSONDecodeError, KeyError):
                pass

        # Try alternate format: line-based output
        vmaf_match = re.search(r"VMAF score[:\s]+([0-9.]+)", output + result.stderr)
        if vmaf_match:
            return float(vmaf_match.group(1))

    except subprocess.TimeoutExpired:
        log_warn("vmaf_timeout", source=str(source_path), encoded=str(encoded_path))
    except Exception as e:
        log_warn("vmaf_failed", source=str(source_path), encoded=str(encoded_path), error=str(e))

    return None


def verify_encode_quality(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    sample_duration_sec: float = 60,
    sample_positions: list[float] | None = None,
    use_vmaf: bool = True,
) -> QualityMetrics:
    """Verify encoding quality by sampling and measuring metrics.

    For efficiency, we sample short clips at specific positions rather than
    processing the entire video.

    Args:
        source_path: Original video file
        encoded_path: Encoded video file
        sample_duration_sec: Duration of each sample clip
        sample_positions: List of start positions (as fraction 0-1). If None,
                         defaults to [0.2, 0.5, 0.8] for beginning, middle, end.
        use_vmaf: Whether to compute VMAF (slower but more accurate)

    Returns:
        QualityMetrics with averaged results
    """
    if sample_positions is None:
        sample_positions = [0.25, 0.5, 0.75]

    # Get source duration
    source_info = probe_media(source_path)
    if source_info is None or source_info.duration_seconds is None:
        log_warn("duration_unknown", source=str(source_path))
        return QualityMetrics()

    duration = source_info.duration_seconds

    # Collect metrics from each sample position
    ssim_values: list[float] = []
    psnr_values: list[float] = []
    vmaf_values: list[float] = []

    for pos_frac in sample_positions:
        start_sec = max(0, (duration * pos_frac) - (sample_duration_sec / 2))
        # Don't exceed video length
        start_sec = min(start_sec, duration - sample_duration_sec)
        if start_sec < 0:
            start_sec = 0

        actual_duration = min(sample_duration_sec, duration - start_sec)

        log_info("sample_segment", source=str(source_path), start_sec=start_sec, duration=actual_duration)

        # Measure SSIM/PSNR
        ssim, psnr = measure_ssim_psnr(source_path, encoded_path, start_sec, actual_duration)
        if ssim is not None:
            ssim_values.append(ssim)
        if psnr is not None:
            psnr_values.append(psnr)

        # Measure VMAF (slower)
        if use_vmaf:
            vmaf = measure_vmaf(source_path, encoded_path, start_sec, actual_duration)
            if vmaf is not None:
                vmaf_values.append(vmaf)

        # Print intermediate results
        parts = []
        if ssim is not None:
            parts.append(f"SSIM={ssim:.4f}")
        if psnr is not None:
            parts.append(f"PSNR={psnr:.1f}")
        if vmaf_values and vmaf_values[-1] is not None:
            parts.append(f"VMAF={vmaf_values[-1]:.1f}")
        log_info("sample_metrics", detail=" ".join(parts) if parts else "no metrics")

    # Average the results
    return QualityMetrics(
        ssim=sum(ssim_values) / len(ssim_values) if ssim_values else None,
        psnr=sum(psnr_values) / len(psnr_values) if psnr_values else None,
        vmaf=sum(vmaf_values) / len(vmaf_values) if vmaf_values else None,
        sample_duration_sec=sample_duration_sec * len(sample_positions),
        sample_start_sec=duration * sample_positions[0] if sample_positions else None,
    )


def cmd_status(args: argparse.Namespace) -> int:
    """Status command - show platform info and available resources."""
    info = detect_platform()

    log_info("platform_status", system=info['system'], machine=info['machine'], hostname=info['hostname'])

    roots = info.get("media_roots", [])
    if roots:
        log_info("media_roots", roots=roots)
    else:
        log_warn("media_roots_missing", expected=list(get_media_roots()))

    if info["system"] == "Darwin":
        if info["machine"] == "arm64":
            log_info("hardware_gpu", type="apple_silicon")
        else:
            log_info("hardware_gpu", type="intel_mac")
    elif info.get("has_nvidia"):
        log_info("hardware_gpu", type="nvidia", gpus=info.get("nvidia_gpus", []))
    else:
        log_info("hardware_gpu", type="none")

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        result = subprocess.run(
            [ffmpeg, "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        svt = "libsvtav1" in result.stdout
        log_info("ffmpeg_status", path=ffmpeg, svt_av1=svt)
    else:
        log_error("ffmpeg_missing", stage="status")

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Analyze command - probe and classify media files."""
    path = normalize_path(pathlib.Path(args.path).resolve())
    files = collect_video_files(path)

    if not files:
        log_error("analyze_no_files", path=str(path))
        return 1

    show_config = load_show_config()
    show_name = guess_show_name(path)
    file_config = show_config.get(show_name, {}) if show_name else {}

    log_info("analyze_start", files=len(files), show=show_name)

    for f in files:
        info = probe_media(f)
        if info is None:
            log_warn("analyze_probe_failed", file=str(f))
            continue

        classification = classify_source(info, file_config)

        log_info(
            "analyze_file",
            file=str(f),
            duration=format_duration(info.duration_seconds),
            video=f"{info.video_codec} {info.video_width}x{info.video_height} {info.video_bit_depth}-bit",
            bitrate=format_bitrate(info.video_bitrate_kbps),
            interlaced=info.is_interlaced,
            audio_tracks=len(info.audio_tracks),
            tier=classification.tier.value,
            confidence=classification.confidence,
            reasons=classification.reasons,
            recommended_crf=classification.recommended_settings.crf,
            recommended_preset=classification.recommended_settings.preset,
            recommended_denoise=classification.recommended_settings.denoise or "none",
            already_av1=info.is_already_av1,
        )

        if info.is_already_av1:
            log_info("analyze_skip_av1", file=str(f))

    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    """Encode command - encode media files to AV1."""
    path = normalize_path(pathlib.Path(args.path).resolve())
    output_dir = normalize_path(pathlib.Path(args.output).resolve())
    files = collect_video_files(path)

    if not files:
        log_error("encode_no_files", path=str(path))
        return 1

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_error("encode_ffmpeg_missing")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    show_config = load_show_config()
    show_name = guess_show_name(path)
    file_config = show_config.get(show_name, {}) if show_name else {}

    # Manual tier override from CLI
    if args.tier:
        file_config["tier"] = args.tier

    log_info("encode_start_cli", files=len(files), output=str(output_dir), show=show_name)

    success_count = 0
    for i, f in enumerate(files, 1):
        log_info("encode_file_start", index=i, total=len(files), file=str(f))

        info = probe_media_with_interlace_detection(f)
        if info is None:
            log_error("encode_probe_failed", file=str(f))
            continue

        if info.is_already_av1 and not args.force:
            log_info("encode_skip_av1", file=str(f))
            continue

        classification = classify_source(info, file_config)
        settings = classification.recommended_settings

        log_info(
            "encode_classification",
            file=str(f),
            tier=classification.tier.value,
            crf=settings.crf,
            preset=settings.preset,
            denoise=settings.denoise or "none",
            interlaced=info.is_interlaced,
        )

        # Determine downscale target (never upscale)
        app_settings = load_app_settings()
        target_height = app_settings.global_max_height

        for lib, root in iter_libraries_for_current_host(app_settings):
            try:
                if f.is_relative_to(root):
                    if lib.max_height:
                        target_height = lib.max_height
                    break
            except Exception:
                if str(f).startswith(str(root)):
                    if lib.max_height:
                        target_height = lib.max_height
                    break

        # Optional VMAF sampling to adjust tier
        if args.sample_vmaf:
            vmaf_stats = sample_vmaf(
                info,
                settings,
                max_height=target_height,
                sample_count=args.sample_count,
                sample_length=args.sample_length,
                motion_aware=args.sample_motion_aware,
            )
            if vmaf_stats:
                classification = adjust_tier_with_vmaf(classification, vmaf_stats)
                settings = classification.recommended_settings
                log_info(
                    "encode_vmaf_adjust",
                    file=str(f),
                    median=vmaf_stats['median'],
                    minimum=vmaf_stats['min'],
                    tier=classification.tier.value,
                )

        cmd = build_ffmpeg_command(
            f,
            output_path,
            settings,
            info,
            max_height=target_height,
            hw_decode=args.hw_decode,
            hw_encode=args.hw_encode,
        )

        # Build output path (mirror library structure like run command)
        source_str = str(f)
        rel_path = None
        for root in get_media_roots():
            if source_str.startswith(root):
                rel_path = f.relative_to(root)
                break

        if rel_path:
            # Mirror the source structure: tv/Show/Season/file.AV1.mp4
            file_output_dir = output_dir / rel_path.parent
        else:
            # Fallback to flat output if not in a known media root
            file_output_dir = output_dir

        file_output_dir.mkdir(parents=True, exist_ok=True)

        # Build output filename
        stem = f.stem
        # Remove old codec markers if present
        for marker in [".x264", ".x265", ".h264", ".h265", ".HEVC", ".AVC"]:
            stem = stem.replace(marker, "")
        output_name = f"{stem}.AV1.mp4"
        output_path = file_output_dir / output_name

        if output_path.exists() and not args.force:
            log_info("encode_skip_output_exists", output=output_path.name)
            continue

        # Build and run ffmpeg command
        cmd = build_ffmpeg_command(
            f,
            output_path,
            settings,
            info,
            hw_decode=args.hw_decode,
            hw_encode=args.hw_encode,
        )

        if args.dry_run:
            log_info("encode_dry_run", output=str(output_path))
            continue

        log_info("encode_launch_cli", output=output_path.name)
        try:
            subprocess.run(cmd, check=True)
            success_count += 1
            orig_size = f.stat().st_size
            new_size = output_path.stat().st_size
            ratio = new_size / orig_size * 100
            log_info(
                "encode_done_cli",
                source_mb=orig_size // 1024 // 1024,
                output_mb=new_size // 1024 // 1024,
                ratio=ratio,
            )

        except subprocess.CalledProcessError as e:
            log_error("encode_failed_cli", error=str(e))
            continue
    log_info("encode_complete_cli", success=success_count, total=len(files))
    return 0


# =============================================================================
# Database / Inventory System
# =============================================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_inventory (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    library_id TEXT,

    -- File info
    size_bytes INTEGER,
    mtime INTEGER,
    duration_sec REAL,

    -- Video info
    video_codec TEXT,
    video_profile TEXT,
    resolution TEXT,
    width INTEGER,
    height INTEGER,
    bitrate_kbps INTEGER,
    bit_depth INTEGER,
    frame_rate TEXT,
    is_interlaced BOOLEAN DEFAULT FALSE,
    is_hdr BOOLEAN DEFAULT FALSE,
    hdr_format TEXT,

    -- Audio/subtitle (JSON arrays)
    audio_tracks TEXT,
    subtitle_tracks TEXT,

    -- Classification
    detected_tier TEXT,
    tier_reasoning TEXT,
    is_av1 BOOLEAN DEFAULT FALSE,
    is_opus BOOLEAN DEFAULT FALSE,

    -- Estimated encode info
    estimated_target_bitrate_kbps INTEGER,
    potential_savings_bytes INTEGER,
    priority_score REAL,
    manual_priority INTEGER DEFAULT 0,

    -- Status
    status TEXT DEFAULT 'pending',
    skip_reason TEXT,

    -- Claim info (for multi-machine coordination)
    claimed_by TEXT,
    claimed_at TEXT,

    scanned_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS encode_results (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES media_inventory(id),
    source_path TEXT NOT NULL,

    -- Encode settings
    tier TEXT,
    crf INTEGER,
    preset INTEGER,
    denoise TEXT,
    film_grain INTEGER,
    audio_codec TEXT,
    audio_bitrate_kbps INTEGER,

    -- Output info
    output_path TEXT,
    output_size_bytes INTEGER,
    output_bitrate_kbps INTEGER,
    compression_ratio REAL,

    -- Quality metrics
    psnr REAL,
    ssim REAL,
    vmaf REAL,
    vmaf_sample_sec REAL,

    -- Execution info
    machine TEXT,
    started_at TEXT,
    completed_at TEXT,
    encode_speed REAL,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,

    -- Promotion
    promoted BOOLEAN DEFAULT FALSE,
    promoted_at TEXT,

    -- Outlier detection
    is_outlier BOOLEAN DEFAULT FALSE,
    outlier_reasons TEXT,
    review_status TEXT DEFAULT 'pending',  -- pending, approved, rejected
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inventory_status ON media_inventory(status);
CREATE INDEX IF NOT EXISTS idx_inventory_priority ON media_inventory(priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_results_source ON encode_results(source_id);
CREATE INDEX IF NOT EXISTS idx_results_outlier ON encode_results(is_outlier, review_status);

-- Real-time encoding progress (updated by encoder, read by web UI)
CREATE TABLE IF NOT EXISTS encode_progress (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES media_inventory(id),
    source_path TEXT NOT NULL,
    output_path TEXT,

    -- Progress info
    machine TEXT,
    tier TEXT,
    started_at TEXT,

    -- FFmpeg progress stats
    frame INTEGER DEFAULT 0,
    total_frames INTEGER,
    fps REAL DEFAULT 0,
    speed REAL DEFAULT 0,
    bitrate_kbps REAL,
    size_bytes INTEGER DEFAULT 0,
    time_encoded_sec REAL DEFAULT 0,
    duration_sec REAL,

    -- Calculated fields
    percent_complete REAL DEFAULT 0,
    eta_seconds INTEGER,

    -- Phase tracking
    phase TEXT DEFAULT 'encoding',  -- probing, encoding, verifying, complete, error
    phase_detail TEXT,

    updated_at TEXT
);

-- Show-level overrides (tier, optional max height)
CREATE TABLE IF NOT EXISTS show_overrides (
    show_name TEXT PRIMARY KEY,
    default_tier TEXT,
    notes TEXT,
    max_height INTEGER,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_progress_machine ON encode_progress(machine);
"""

# Stale claim timeout (machine crashed) - 8 hours
# Long movies with denoise can take 4-6+ hours on slower presets
STALE_CLAIM_SECONDS = 8 * 60 * 60


def get_db_path(_: pathlib.Path | None = None) -> pathlib.Path:
    """Return the unified inventory database path for all libraries."""
    return INVENTORY_DB


def init_db(_: pathlib.Path) -> Session:
    return Session(ENGINE)


def init_db_shim(_: pathlib.Path) -> SessionShim:
    return SessionShim(Session(ENGINE))


def get_library_root(path: pathlib.Path) -> pathlib.Path:
    """Find the library root (tv, movies, etc.) for a given path."""
    path = path.resolve()
    for root in get_media_roots():
        root_path = pathlib.Path(root)
        if str(path).startswith(str(root_path)):
            # Return first level under media root (e.g., /Volumes/media/tv)
            rel = path.relative_to(root_path)
            parts = rel.parts
            if parts:
                return root_path / parts[0]
    # Fallback: use the path itself if it's a directory
    if path.is_dir():
        return path
    return path.parent


def find_library_for_path(
    path: pathlib.Path,
    settings: Optional[AppSettings] = None,
) -> tuple[Optional[LibrarySettings], Optional[pathlib.Path]]:
    resolved = path.resolve()
    if settings is None:
        settings = load_app_settings()

    for lib, root in iter_libraries_for_current_host(settings):
        try:
            resolved.relative_to(root)
            return lib, root
        except ValueError:
            continue
    return None, None


def detect_hdr(info: MediaInfo) -> tuple[bool, Optional[str]]:
    """Detect if content is HDR and what format."""
    # Check color transfer and primaries from ffprobe
    # Common HDR indicators: bt2020, smpte2084 (PQ), arib-std-b67 (HLG)
    # For now, just check bit depth as a simple heuristic
    if info.video_bit_depth and info.video_bit_depth > 8:
        # 10-bit+ could be HDR, but need more info
        # TODO: Parse color_transfer, color_primaries from ffprobe
        pass
    return False, None


def detect_interlaced(info: MediaInfo) -> bool:
    """Detect if content is interlaced."""
    return info.is_interlaced


def calculate_priority(
    potential_savings_bytes: Optional[int],
    mtime: int,
    max_savings: int,
    max_age: int,
) -> float:
    """Calculate priority score (0-1, higher = encode first).

    Priority favors:
    - Biggest space savings (encode files that will free up the most space)
    - Older files (process backlog first)
    """
    now = int(time.time())
    age = now - mtime

    # Normalize to 0-1
    # Age: older = higher priority
    age_score = min(1.0, age / max_age) if max_age > 0 else 0.5

    # Savings: larger savings = higher priority
    if potential_savings_bytes and potential_savings_bytes > 0 and max_savings > 0:
        savings_score = min(1.0, potential_savings_bytes / max_savings)
    else:
        savings_score = 0.0  # No estimated savings = low priority

    # Savings weighted more heavily (70%) since space recovery is the goal
    return (age_score * 0.3) + (savings_score * 0.7)


def scan_file_to_db(
    session: Session,
    file_path: pathlib.Path,
    max_savings: int,
    max_age: int,
    library_id: str | None = None,
) -> Optional[dict]:
    """Scan a single file and insert/update in database."""
    try:
        info = probe_media(file_path)
        if info is None:
            return None

        # Get file stats
        stat = file_path.stat()
        size_bytes = stat.st_size
        mtime = int(stat.st_mtime)

        # Detect special cases
        is_av1 = info.is_already_av1
        is_hdr, hdr_format = detect_hdr(info)
        is_interlaced = detect_interlaced(info)

        # Check if any audio track is Opus
        is_opus = any(
            (t.get("codec") or "").lower() == "opus"
            for t in info.audio_tracks
        )

        # Classify
        classification = classify_source(info)
        tier = classification.tier.value
        tier_reasoning = "; ".join(classification.reasons)

        # Estimate target bitrate and savings (calculate before priority)
        # AV1 typically achieves 30-50% file size compared to H.264 at similar quality
        # Use compression ratio based on tier (higher CRF = more compression)
        settings = TIER_SETTINGS.get(classification.tier)
        estimated_target = None
        potential_savings = None
        if settings and info.video_bitrate_kbps:
            # Estimate AV1 output as a fraction of source bitrate
            # Lower CRF = higher quality = less compression
            av1_ratio = {26: 0.40, 28: 0.35, 30: 0.30, 32: 0.25}.get(settings.crf, 0.35)
            estimated_target = int(info.video_bitrate_kbps * av1_ratio)

            if info.video_bitrate_kbps > estimated_target:
                # Calculate bytes saved: (bitrate_diff_kbps * duration_sec * 1000) / 8
                # bitrate is in kbps, duration in seconds, result in bytes
                bitrate_diff = info.video_bitrate_kbps - estimated_target
                potential_savings = int(
                    bitrate_diff * (info.duration_seconds or 0) * 1000 / 8
                )

        # Calculate priority based on potential savings and age
        priority = calculate_priority(potential_savings, mtime, max_savings, max_age)

        # Determine status
        status = "pending"
        skip_reason = None
        if is_av1:
            status = "skipped_native_av1"
            skip_reason = "Already AV1 encoded"
        elif is_hdr:
            status = "skipped_hdr"
            skip_reason = f"HDR content ({hdr_format or 'unknown format'})"

        now_str = now_iso()

        existing = session.exec(select(MediaItem).where(MediaItem.path == str(file_path))).one_or_none()
        item = existing or MediaItem(path=str(file_path))
        item.library_id = library_id or item.library_id
        item.size_bytes = size_bytes
        item.mtime = mtime
        item.duration_sec = info.duration_seconds
        item.video_codec = info.video_codec
        item.video_profile = None
        item.resolution = f"{info.video_width}x{info.video_height}" if info.video_width and info.video_height else None
        item.width = info.video_width
        item.height = info.video_height
        item.bitrate_kbps = info.video_bitrate_kbps
        item.bit_depth = info.video_bit_depth
        item.frame_rate = str(info.video_framerate) if info.video_framerate else None
        item.is_interlaced = is_interlaced
        item.is_hdr = is_hdr
        item.hdr_format = hdr_format
        item.audio_tracks = json.dumps(info.audio_tracks)
        item.subtitle_tracks = json.dumps(info.subtitle_tracks)
        item.detected_tier = tier
        item.tier_reasoning = tier_reasoning
        item.is_av1 = is_av1
        item.is_opus = is_opus
        item.estimated_target_bitrate_kbps = estimated_target
        item.potential_savings_bytes = potential_savings
        item.priority_score = priority
        item.status = item.status if item.status in ("encoded", "encoding", "completed") else status
        item.skip_reason = skip_reason
        item.scanned_at = now_str if not item.scanned_at else item.scanned_at
        item.updated_at = now_str

        session.add(item)
        session.commit()

        return {
            "path": str(file_path),
            "status": status,
            "tier": tier,
            "is_av1": is_av1,
            "is_hdr": is_hdr,
        }

    except Exception as e:
        print(f"  [error] Failed to scan {file_path}: {e}", file=sys.stderr)
        return None


def recalculate_priorities(session: Session, max_age: int) -> None:
    """Recalculate priority scores using actual max_savings from the database.

    This should be called after scanning to properly normalize priority scores
    based on the actual range of potential_savings_bytes values.
    """
    # Get max savings from all pending files
    max_savings = session.exec(
        select(MediaItem.potential_savings_bytes)
        .where(
            MediaItem.status == "pending",
            MediaItem.potential_savings_bytes.is_not(None),
            MediaItem.potential_savings_bytes > 0,
        )
        .order_by(MediaItem.potential_savings_bytes.desc())
        .limit(1)
    ).first() or 1

    # Recalculate priorities for all pending files
    pending = session.exec(select(MediaItem).where(MediaItem.status == "pending")).all()

    for item in pending:
        priority = calculate_priority(item.potential_savings_bytes, item.mtime, max_savings, max_age)
        item.priority_score = priority
        item.updated_at = now_iso()
        session.add(item)

    session.commit()
    print(f"  Updated priorities for {len(pending)} pending files (max_savings={max_savings:,} bytes)")


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan library and populate inventory database."""
    path = pathlib.Path(args.path).resolve()
    if not path.exists():
        log_error("path_missing", path=str(path))
        return 1

    settings = load_app_settings()
    lib, library_root = find_library_for_path(path, settings)
    if library_root is None:
        library_root = get_library_root(path)

    db_path = get_db_path(library_root)
    log_info("scan_start", library=str(library_root), database=str(db_path))

    session = init_db(db_path)

    # Find all video files
    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    if not files:
        log_info("scan_no_files")
        return 0

    log_info("scan_files", count=len(files))

    # Get max age for priority calculation (max_savings calculated after scan)
    stats = [int(f.stat().st_mtime) for f in files]
    max_age = max(int(time.time()) - s for s in stats) if stats else 1

    # Counters
    counts = {"pending": 0, "skipped_native_av1": 0, "skipped_hdr": 0, "error": 0}

    # First pass: scan files with placeholder max_savings (will recalculate after)
    for i, f in enumerate(files, 1):
        if i % 10 == 0 or i == len(files):
            log_info("scan_progress", index=i, total=len(files), file=f.name)

        result = scan_file_to_db(session, f, 1, max_age, library_id=lib.id if lib else None)
        if result:
            status = result["status"]
            if status in counts:
                counts[status] += 1
            else:
                counts["pending"] += 1
        else:
            counts["error"] += 1

        # Commit periodically
        if i % 50 == 0:
            session.commit()

    session.commit()

    # Recalculate priorities using actual max_savings from database
    log_info("recalculate_priorities")
    recalculate_priorities(session, max_age)

    # Check for completed encodes with missing output files
    log_info("check_missing_outputs")
    missing_reset = check_missing_outputs(session)

    log_info(
        "scan_complete",
        pending=counts["pending"],
        skipped_native_av1=counts["skipped_native_av1"],
        skipped_hdr=counts["skipped_hdr"],
        errors=counts["error"],
        reset_missing=missing_reset,
    )

    return 0


async def _watch_single_library(lib: LibrarySettings, root: pathlib.Path) -> None:
    """Watch a single library for new video files and auto-queue them.

    When a new file appears under the configured root, we run it through the
    existing scan pipeline so it lands in the inventory database and queue.
    """

    if awatch is None or Change is None:
        print("[error] watchfiles is not installed; 'watch' command is unavailable", file=sys.stderr)
        return

    if not root.exists():
        print(f"[watch] Root does not exist, skipping: {root}")
        return

    print(f"[watch] Watching {root} ({lib.name}) for new video files...")

    async for changes in awatch(root, recursive=True):
        for change, path_str in changes:
            if change not in (Change.added, Change.modified):
                continue

            path = normalize_path(pathlib.Path(path_str))
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue
            if not path.is_file():
                continue

            try:
                library_root = get_library_root(path)
                db_path = get_db_path(library_root)
                session = init_db(db_path)

                now = int(time.time())
                oldest_mtime = session.exec(
                    select(MediaItem.mtime)
                    .where(MediaItem.status == "pending", MediaItem.mtime.is_not(None), MediaItem.mtime > 0)
                    .order_by(MediaItem.mtime)
                    .limit(1)
                ).first() or int(path.stat().st_mtime)
                max_age = max(now - oldest_mtime, 1)

                print(f"[watch] Detected new media file: {path}")
                result = scan_file_to_db(session, path, max_savings=1, max_age=max_age, library_id=lib.id)
                session.commit()

                recalculate_priorities(session, max_age=max_age)
                session.close()

                if result:
                    print(f"[watch] Queued {path.name} (tier={result['tier']})")
                else:
                    print(f"[watch] Failed to scan {path}", file=sys.stderr)
            except Exception as exc:
                print(f"[watch] Error handling {path}: {exc}", file=sys.stderr)


async def _watch_libraries() -> None:
    """Watch all configured libraries that have watch enabled."""

    settings = load_app_settings()
    libraries = [
        (lib, root)
        for lib, root in iter_libraries_for_current_host(settings)
        if lib.watch
    ]

    if not libraries:
        print("[watch] No libraries configured for this host.")
        return

    tasks = [
        _watch_single_library(lib, root)
        for lib, root in libraries
    ]

    await asyncio.gather(*tasks)


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch configured libraries and auto-queue new video files."""

    if awatch is None or Change is None:
        print("[error] watchfiles is not installed. Install with 'pip install watchfiles' to use the watch command.", file=sys.stderr)
        return 1

    if getattr(args, "autoupdate_url", None):
        updated = maybe_autoupdate(args.autoupdate_url, ["mediaforce.py"])
        if updated:
            print("[autoupdate] Updated mediaforce.py. Restarting watch to load new code.")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # Load settings (remote or local)
    settings_url = getattr(args, "settings_url", None)
    settings = load_remote_settings(settings_url) if settings_url else load_app_settings()
    if settings is None:
        print("[error] Could not load settings.", file=sys.stderr)
        return 1

    # Optional periodic autoupdate during long runs
    if getattr(args, "autoupdate_interval", None):
        interval = max(300, int(args.autoupdate_interval))
    else:
        interval = None

    async def runner():
        if interval:
            async def updater():
                while True:
                    await asyncio.sleep(interval)
                    if maybe_autoupdate(args.autoupdate_url, ["mediaforce.py"]):
                        print("[autoupdate] New version fetched; restarting watch to apply.")
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            asyncio.create_task(updater())
        await _watch_libraries()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("\n[watch] Stopping.")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Show encoding queue (priority order)."""
    path = pathlib.Path(args.path).resolve()
    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_error("run_no_db", path=str(db_path))
        return 1

    session = init_db(db_path)
    limit = args.limit or 20

    rows = session.exec(
        select(MediaItem)
        .where(MediaItem.status == "pending")
        .order_by(MediaItem.priority_score.desc())
        .limit(limit)
    ).all()

    if not rows:
        print("Queue is empty (no pending files).")
        return 0

    print(f"Top {len(rows)} files in queue:\n")
    print(f"{'Priority':>8}  {'Size':>8}  {'Savings':>8}  {'Tier':>10}  {'Bitrate':>10}  Path")
    print("-" * 90)

    for row in rows:
        size_mb = (row.size_bytes or 0) // 1024 // 1024
        savings = row.potential_savings_bytes
        savings_str = f"{savings // 1024 // 1024}MB" if savings else "?"
        bitrate = f"{row.bitrate_kbps}k" if row.bitrate_kbps else "?"
        priority = f"{row.priority_score:.3f}" if row.priority_score is not None else "?"
        # Shorten path for display
        display_path = row.path
        if len(display_path) > 40:
            display_path = "..." + display_path[-37:]
        print(f"{priority:>8}  {size_mb:>6}MB  {savings_str:>8}  {row.detected_tier or '?':>10}  {bitrate:>10}  {display_path}")

    # Show summary
    log_info("inventory_summary")
    summary = session.exec(
        select(MediaItem.status, MediaItem.id, MediaItem.size_bytes)
    ).all()
    totals: dict[str, tuple[int, int]] = {}
    for status, mid, size_bytes in summary:
        cnt, total = totals.get(status, (0, 0))
        totals[status] = (cnt + 1, total + (size_bytes or 0))
    for status, (cnt, total_bytes) in totals.items():
        total_gb = total_bytes / 1024 / 1024 / 1024
        print(f"  {status}: {cnt} files ({total_gb:.1f} GB)")

    # Calculate space saved from completed encodes
    encode_rows = session.exec(
        select(EncodeResult.output_size_bytes, MediaItem.size_bytes)
        .join(MediaItem, EncodeResult.source_id == MediaItem.id)
        .where(EncodeResult.output_size_bytes.is_not(None), EncodeResult.output_size_bytes > 0)
    ).all()
    if encode_rows:
        source_bytes = sum(src or 0 for _, src in encode_rows)
        output_bytes = sum(out or 0 for out, _ in encode_rows)
        if source_bytes and output_bytes:
            source_gb = source_bytes / 1024 / 1024 / 1024
            output_gb = output_bytes / 1024 / 1024 / 1024
            saved_gb = source_gb - output_gb
            saved_pct = (1 - output_bytes / source_bytes) * 100
            print(f"\nSpace saved from {len(encode_rows)} encodes:")
            print(f"  Source: {source_gb:.1f} GB → Output: {output_gb:.1f} GB")
            print(f"  Saved: {saved_gb:.1f} GB ({saved_pct:.1f}%)")

    session.close()
    return 0


def check_missing_outputs(session: Session) -> int:
    """Check for completed encodes with missing output files and reset to pending.

    Returns the number of files reset.
    """
    now_str = datetime.now().isoformat()

    # Find completed encodes where the output file no longer exists
    joins = session.exec(
        select(MediaItem.id, MediaItem.path, EncodeResult.output_path)
        .join(EncodeResult, EncodeResult.source_id == MediaItem.id)
        .where(MediaItem.status == "encoded", EncodeResult.output_path.is_not(None))
    ).all()

    missing_count = 0
    for mid, src_path, out_path in joins:
        output_path = pathlib.Path(out_path)
        if not output_path.exists():
            item = session.get(MediaItem, mid)
            if item:
                item.status = "pending"
                item.updated_at = now_iso()
                session.add(item)
            missing_count += 1
            print(f"  [reset] Missing output for: {pathlib.Path(src_path).name}")

    if missing_count:
        session.commit()

    return missing_count


def claim_next_file(session: Session, machine: str) -> Optional[dict]:
    """Claim the next file with library-aware weighting and manual bumping."""
    from datetime import timedelta

    now = datetime.now()
    now_str = now_iso()
    stale_cutoff = (now - timedelta(seconds=STALE_CLAIM_SECONDS)).isoformat()

    stale_items = session.exec(
        select(MediaItem).where(
            MediaItem.status == "encoding",
            MediaItem.claimed_at < stale_cutoff,
        )
    ).all()
    for item in stale_items:
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
        item.updated_at = now_str
        session.add(item)
    if stale_items:
        session.commit()

    weighted = session.exec(
        select(MediaItem, Library.weight)
        .join(Library, MediaItem.library_id == Library.id, isouter=True)
        .where(MediaItem.status == "pending")
        .order_by(
            MediaItem.manual_priority,
            ((MediaItem.priority_score or 0) * (Library.weight or 1)).desc(),
        )
        .limit(1)
    ).first()

    if not weighted:
        return None

    item, _weight = weighted
    fresh = session.get(MediaItem, item.id)
    if fresh is None or fresh.status != "pending":
        return claim_next_file(session, machine)

    fresh.status = "encoding"
    fresh.claimed_by = machine
    fresh.claimed_at = now_str
    fresh.updated_at = now_str
    session.add(fresh)
    session.commit()

    return {
        "id": fresh.id,
        "path": fresh.path,
        "detected_tier": fresh.detected_tier,
        "bitrate_kbps": fresh.bitrate_kbps,
        "duration_sec": fresh.duration_sec,
        "library_id": fresh.library_id,
    }


def release_claim(session: Session, file_id: int, success: bool) -> None:
    item = session.get(MediaItem, file_id)
    if not item:
        return
    now_str = now_iso()
    if success:
        item.status = "encoded"
    else:
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
    item.updated_at = now_str
    session.add(item)
    session.commit()


# =============================================================================
# Progress Tracking
# =============================================================================


def start_progress_tracking(
    session: Session,
    source_id: int,
    source_path: str,
    output_path: str,
    machine: str,
    tier: str,
    duration_sec: float,
    total_frames: Optional[int] = None,
) -> int:
    now_str = now_iso()
    session.exec(delete(EncodeProgress).where(EncodeProgress.machine == machine))
    progress = EncodeProgress(
        source_id=source_id,
        source_path=source_path,
        output_path=output_path,
        machine=machine,
        tier=tier,
        started_at=now_str,
        duration_sec=duration_sec,
        total_frames=total_frames,
        phase="encoding",
        updated_at=now_str,
    )
    session.add(progress)
    session.commit()
    session.refresh(progress)
    return progress.id  # type: ignore[arg-type]


def update_progress(
    session: Session,
    progress_id: int,
    frame: int = 0,
    fps: float = 0,
    speed: float = 0,
    bitrate_kbps: Optional[float] = None,
    size_bytes: int = 0,
    time_encoded_sec: float = 0,
    duration_sec: Optional[float] = None,
    phase: Optional[str] = None,
    phase_detail: Optional[str] = None,
) -> None:
    now_str = now_iso()

    percent_complete = 0.0
    eta_seconds: Optional[int] = None

    if duration_sec and duration_sec > 0 and time_encoded_sec > 0:
        percent_complete = min(100.0, (time_encoded_sec / duration_sec) * 100)
        if speed and speed > 0:
            remaining_sec = duration_sec - time_encoded_sec
            eta_seconds = int(remaining_sec / speed)

    progress = session.get(EncodeProgress, progress_id)
    if not progress:
        return

    progress.frame = frame
    progress.fps = fps
    progress.speed = speed
    progress.bitrate_kbps = bitrate_kbps
    progress.size_bytes = size_bytes
    progress.time_encoded_sec = time_encoded_sec
    progress.percent_complete = percent_complete
    progress.eta_seconds = eta_seconds
    if phase:
        progress.phase = phase
    if phase_detail:
        progress.phase_detail = phase_detail
    progress.updated_at = now_str
    session.add(progress)
    session.commit()


def finish_progress_tracking(
    session: Session,
    progress_id: int,
    success: bool,
    error_msg: Optional[str] = None,
) -> None:
    progress = session.get(EncodeProgress, progress_id)
    if progress:
        session.delete(progress)
        session.commit()


def parse_ffmpeg_progress(line: str) -> dict:
    """Parse ffmpeg progress output line.

    FFmpeg with -progress outputs key=value pairs like:
        frame=1234
        fps=45.67
        bitrate=2345.6kbits/s
        total_size=12345678
        out_time_us=12345678
        speed=1.23x
    """
    result = {}

    # Parse key=value
    if "=" in line:
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if key == "frame":
            try:
                result["frame"] = int(value)
            except ValueError:
                pass
        elif key == "fps":
            try:
                result["fps"] = float(value)
            except ValueError:
                pass
        elif key == "bitrate":
            # e.g., "2345.6kbits/s" or "N/A"
            if value != "N/A":
                match = re.match(r"([\d.]+)kbits/s", value)
                if match:
                    result["bitrate_kbps"] = float(match.group(1))
        elif key == "total_size":
            try:
                result["size_bytes"] = int(value)
            except ValueError:
                pass
        elif key == "out_time_us":
            try:
                result["time_encoded_sec"] = int(value) / 1_000_000
            except ValueError:
                pass
        elif key == "out_time_ms":
            try:
                result["time_encoded_sec"] = int(value) / 1_000
            except ValueError:
                pass
        elif key == "speed":
            # e.g., "1.23x" or "N/A"
            if value != "N/A":
                match = re.match(r"([\d.]+)x", value)
                if match:
                    result["speed"] = float(match.group(1))

    return result


def run_ffmpeg_with_progress(
    cmd: list[str],
    session: Session,
    progress_id: int,
    duration_sec: float,
) -> subprocess.CompletedProcess:
    """Run ffmpeg command with progress tracking.

    Adds -progress pipe:1 to get machine-readable progress updates.
    """
    # Add progress output flag
    cmd_with_progress = cmd.copy()

    # Insert -progress after -hide_banner or at position 1
    try:
        idx = cmd_with_progress.index("-hide_banner") + 1
    except ValueError:
        idx = 1
    cmd_with_progress.insert(idx, "-progress")
    cmd_with_progress.insert(idx + 1, "pipe:1")

    # Also add -nostats to avoid duplicate output
    cmd_with_progress.insert(idx, "-nostats")

    process = subprocess.Popen(
        cmd_with_progress,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    accumulated = {}
    last_update = time.time()

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break

        parsed = parse_ffmpeg_progress(line)
        accumulated.update(parsed)

        # Update database every 2 seconds to avoid too many writes
        now = time.time()
        if now - last_update >= 2 and accumulated:
            try:
                update_progress(
                    session,
                    progress_id,
                    frame=accumulated.get("frame", 0),
                    fps=accumulated.get("fps", 0),
                    speed=accumulated.get("speed", 0),
                    bitrate_kbps=accumulated.get("bitrate_kbps"),
                    size_bytes=accumulated.get("size_bytes", 0),
                    time_encoded_sec=accumulated.get("time_encoded_sec", 0),
                    duration_sec=duration_sec,
                )
            except Exception:
                pass
            last_update = now

    # Get stderr for error messages
    _, stderr = process.communicate()

    return subprocess.CompletedProcess(
        args=cmd_with_progress,
        returncode=process.returncode,
        stdout="",
        stderr=stderr,
    )


def record_encode_result(
    session: Session,
    source_id: int,
    source_path: str,
    tier: str,
    settings: TierSettings,
    output_path: str,
    output_size: int,
    output_bitrate: Optional[int],
    source_size: int,
    machine: str,
    started_at: str,
    error_msg: Optional[str] = None,
    metrics: Optional[QualityMetrics] = None,
    outlier_result: Optional[OutlierResult] = None,
) -> int:
    """Record encode result in the database.

    Returns:
        The ID of the inserted encode_results row.
    """
    completed_at = datetime.now().isoformat()
    compression_ratio = output_size / source_size if source_size > 0 else None

    # Extract quality metrics
    psnr = metrics.psnr if metrics else None
    ssim = metrics.ssim if metrics else None
    vmaf = metrics.vmaf if metrics else None
    vmaf_sample_sec = metrics.sample_duration_sec if metrics else None

    # Extract outlier info
    is_outlier = outlier_result.is_outlier if outlier_result else False
    outlier_reasons = "; ".join(outlier_result.reasons) if outlier_result and outlier_result.reasons else None
    review_status = "pending" if is_outlier else "approved"  # Auto-approve non-outliers

    result = EncodeResult(
        source_id=source_id,
        source_path=source_path,
        tier=tier,
        crf=settings.crf,
        preset=settings.preset,
        denoise=settings.denoise,
        film_grain=settings.film_grain,
        audio_codec="opus",
        audio_bitrate_kbps=256,
        output_path=output_path,
        output_size_bytes=output_size,
        output_bitrate_kbps=output_bitrate,
        compression_ratio=compression_ratio,
        psnr=psnr,
        ssim=ssim,
        vmaf=vmaf,
        vmaf_sample_sec=vmaf_sample_sec,
        machine=machine,
        started_at=started_at,
        completed_at=completed_at,
        error_message=error_msg,
        is_outlier=is_outlier,
        outlier_reasons=outlier_reasons,
        review_status=review_status,
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result.id  # type: ignore[arg-type]


def parse_until_time(until_str: str) -> Optional[datetime]:
    """Parse --until time string (HH:MM format)."""
    try:
        hour, minute = map(int, until_str.split(":"))
        now = datetime.now()
        until = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # If the time has passed today, assume it's tomorrow
        if until <= now:
            until = until.replace(day=until.day + 1)
        return until
    except (ValueError, AttributeError):
        return None


def within_offpeak(settings: AppSettings) -> bool:
    if not settings.offpeak_enabled:
        return True
    try:
        start_h, start_m = map(int, settings.offpeak_start.split(":"))
        end_h, end_m = map(int, settings.offpeak_end.split(":"))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
            return True
    except Exception:
        return True
    now = datetime.now()
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def cmd_run(args: argparse.Namespace) -> int:
    """Run queue-based encoding from inventory database."""
    path = pathlib.Path(args.path).resolve()

    if not path.exists():
        log_error("run_path_missing", path=str(path))
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_error("run_db_missing", db=str(db_path))
        return 1

    # Determine transcode output root
    transcode_root = pathlib.Path(args.output)
    transcode_root.mkdir(parents=True, exist_ok=True)

    # Parse --until time
    until_time = None
    if args.until:
        until_time = parse_until_time(args.until)
        if until_time is None:
            log_error("run_until_invalid", value=args.until)
            return 1
        log_info("run_until_set", until=args.until)

    machine = socket.gethostname()
    log_info("run_start", machine=machine, library=str(library_root), output=str(transcode_root))

    # Autoupdate on startup
    if getattr(args, "autoupdate_url", None):
        if maybe_autoupdate(args.autoupdate_url, ["mediaforce.py"]):
            log_info("autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # Load settings (prefer remote settings-url if provided)
    settings_url = getattr(args, "settings_url", None)
    app_settings = None
    if settings_url:
        app_settings = load_remote_settings(settings_url)
        if app_settings is None:
            print(f"[warn] Could not load remote settings from {settings_url}; using local defaults.")
    if app_settings is None:
        app_settings = load_app_settings()

    if getattr(args, "max_concurrency", None):
        app_settings.max_concurrency = max(args.max_concurrency, 1)
    if getattr(args, "offpeak_enabled", None):
        app_settings.offpeak_enabled = True
    if getattr(args, "offpeak_start", None):
        app_settings.offpeak_start = args.offpeak_start or app_settings.offpeak_start
    if getattr(args, "offpeak_end", None):
        app_settings.offpeak_end = args.offpeak_end or app_settings.offpeak_end

    # Periodic autoupdate during run
    interval = int(getattr(args, "autoupdate_interval", 0) or 0)
    last_update_check = time.time()

    def check_autoupdate():
        nonlocal last_update_check
        if not args.autoupdate_url or interval <= 0:
            return
        now = time.time()
        if now - last_update_check < interval:
            return
        last_update_check = now
        if maybe_autoupdate(args.autoupdate_url, ["mediaforce.py"]):
            log_info("autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    session = init_db(db_path)
    show_config = load_show_config()

    encoded_count = 0
    error_count = 0
    outlier_count = 0

    max_concurrency = int(os.getenv("MEDIAFORCE_MAX_CONCURRENCY", app_settings.max_concurrency or 1))
    if getattr(args, "max_concurrency", None):
        max_concurrency = args.max_concurrency
    if max_concurrency < 1:
        max_concurrency = 1
    active_slots = 0

    while True:
        check_autoupdate()
        if until_time and datetime.now() >= until_time:
            log_info("run_until_reached", until=args.until)
            break

        if app_settings.offpeak_enabled and not within_offpeak(app_settings):
            log_info("offpeak_pause", window=f"{app_settings.offpeak_start}-{app_settings.offpeak_end}")
            time.sleep(300)
            continue

        if active_slots >= max_concurrency:
            time.sleep(1)
            continue

        # Claim next file
        claimed = claim_next_file(session, machine)
        if claimed is None:
            log_info("queue_empty")
            break

        source_path = pathlib.Path(claimed["path"])
        log_info("encode_start", index=encoded_count + 1, file=str(source_path))

        # Probe file with interlacing detection
        log_info("detect_interlace", file=str(source_path))
        info = probe_media_with_interlace_detection(source_path)
        if info is None:
            log_error("probe_failed", file=str(source_path))
            release_claim(session, claimed["id"], success=False)
            error_count += 1
            continue

        # Get show config if applicable
        show_name = guess_show_name(source_path)
        file_config = show_config.get(show_name, {}) if show_name else {}

        # Classify and get settings
        classification = classify_source(info, file_config)
        settings = classification.recommended_settings
        tier = classification.tier.value

        log_info(
            "classification",
            file=str(source_path),
            tier=tier,
            crf=settings.crf,
            preset=settings.preset,
            denoise=settings.denoise or "none",
        )
        if info.is_interlaced:
            log_info("interlaced_detected", file=str(source_path))

        # Build output path (mirror library structure)
        source_str = str(source_path)
        for root in get_media_roots():
            if source_str.startswith(root):
                rel_path = source_path.relative_to(root)
                break
        else:
            rel_path = pathlib.Path(source_path.name)

        output_dir = transcode_root / rel_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = source_path.stem
        output_name = f"{stem}.AV1.mp4"
        output_path = output_dir / output_name

        if output_path.exists() and not args.force:
            log_info("skip_output_exists", output=str(output_path))
            release_claim(session, claimed["id"], success=True)
            continue

        # Build and run ffmpeg command
        # Downscale target from settings (never upscale)
        app_settings = load_app_settings()
        target_height = app_settings.global_max_height
        for lib, root in iter_libraries_for_current_host(app_settings):
            try:
                if source_path.is_relative_to(root):
                    if lib.max_height:
                        target_height = lib.max_height
                    break
            except Exception:
                if str(source_path).startswith(str(root)):
                    if lib.max_height:
                        target_height = lib.max_height
                    break

        # Optional VMAF sampling to adjust tier before full encode
        if args.sample_vmaf:
            vmaf_stats = sample_vmaf(
                info,
                settings,
                max_height=target_height,
                sample_count=args.sample_count,
                sample_length=args.sample_length,
                motion_aware=args.sample_motion_aware,
            )
            if vmaf_stats:
                classification = adjust_tier_with_vmaf(classification, vmaf_stats)
                settings = classification.recommended_settings
                tier = classification.tier.value
                log_info(
                    "vmaf_sample",
                    file=str(source_path),
                    median=vmaf_stats['median'],
                    minimum=vmaf_stats['min'],
                    tier=tier,
                )

        cmd = build_ffmpeg_command(
            source_path,
            output_path,
            settings,
            info,
            max_height=target_height,
            hw_decode=args.hw_decode,
            hw_encode=args.hw_encode,
        )
        started_at = datetime.now().isoformat()

        if args.dry_run:
            log_info("dry_run", output=str(output_path))
            release_claim(session, claimed["id"], success=True)
            encoded_count += 1
            continue

        log_info("encode_launch", output=output_path.name)
        active_slots += 1

        # Start progress tracking
        duration_sec = info.duration_sec or 0
        progress_id = start_progress_tracking(
            session, claimed["id"], str(source_path), str(output_path),
            machine, tier, duration_sec,
        )

        try:
            # Run ffmpeg with progress tracking
            result = run_ffmpeg_with_progress(cmd, session, progress_id, duration_sec)

            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr
                )

            # Get output stats
            output_size = output_path.stat().st_size
            source_size = source_path.stat().st_size
            ratio = output_size / source_size * 100

            # Probe output for bitrate
            output_info = probe_media(output_path)
            output_bitrate = output_info.video_bitrate_kbps if output_info else None

            log_info(
                "encode_complete",
                source_mb=source_size // 1024 // 1024,
                output_mb=output_size // 1024 // 1024,
                ratio=ratio,
            )

            # Quality verification and outlier detection
            metrics: Optional[QualityMetrics] = None
            outlier_result: Optional[OutlierResult] = None

            if args.verify:
                # Update progress phase
                update_progress(session, progress_id, phase="verifying", phase_detail="Running quality checks")
                log_info("verify_start", file=str(source_path))
                try:
                    metrics = verify_encode_quality(
                        source_path,
                        output_path,
                        sample_duration_sec=args.verify_duration,
                    )
                    if metrics:
                        log_info(
                            "verify_metrics",
                            vmaf=metrics.vmaf,
                            ssim=metrics.ssim,
                            psnr=metrics.psnr,
                        )

                        # Check for outliers
                        outlier_result = check_for_outliers(
                            source_path,
                            output_path,
                            metrics=metrics,
                        )
                        if outlier_result.is_outlier:
                            log_warn("outlier_flagged", reasons=", ".join(outlier_result.reasons))
                        outlier_count += 1
                    else:
                        log_warn("verify_failed")
                except Exception as e:
                    log_error("verify_exception", error=str(e))

            # Clean up progress tracking
            finish_progress_tracking(session, progress_id, success=True)

            # Record result
            record_encode_result(
                session, claimed["id"], str(source_path), tier, settings,
                str(output_path), output_size, output_bitrate, source_size,
                machine, started_at,
                metrics=metrics,
                outlier_result=outlier_result,
            )
            release_claim(session, claimed["id"], success=True)
            encoded_count += 1

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else str(e))
            log_error("encode_failed", error=error_msg[:200])

            # Clean up progress tracking
            finish_progress_tracking(session, progress_id, success=False, error_msg=error_msg[:500])

            record_encode_result(
                session, claimed["id"], str(source_path), tier, settings,
                str(output_path), 0, None, source_path.stat().st_size,
                machine, started_at, error_msg
            )
            release_claim(session, claimed["id"], success=False)
            error_count += 1

            # Clean up partial output
            if output_path.exists():
                output_path.unlink()
        finally:
            active_slots = max(active_slots - 1, 0)

    session.close()

    log_info(
        "run_complete",
        encoded=encoded_count,
        errors=error_count,
        outliers=outlier_count,
    )

    return 0 if error_count == 0 else 1


# Sidecar extensions to rename when promoting
SIDECAR_EXTENSIONS = {
    ".nfo",      # metadata
    ".srt",      # subtitles
    ".sub",      # subtitles
    ".idx",      # subtitle index
    ".ass",      # styled subtitles
    ".ssa",      # styled subtitles
}

# Image sidecar patterns (partial match on stem)
IMAGE_SIDECAR_SUFFIXES = [
    "-poster",
    "-fanart",
    "-thumb",
    "-banner",
    "-landscape",
    "-clearlogo",
    "-clearart",
]


def find_sidecars(source_path: pathlib.Path) -> list[pathlib.Path]:
    """Find all sidecar files associated with a video file."""
    sidecars = []
    parent = source_path.parent
    stem = source_path.stem

    # Direct extension match (e.g., show.s01e01.mkv -> show.s01e01.nfo)
    for ext in SIDECAR_EXTENSIONS:
        sidecar = parent / f"{stem}{ext}"
        if sidecar.exists():
            sidecars.append(sidecar)

    # Image sidecars (e.g., show.s01e01-thumb.jpg)
    for suffix in IMAGE_SIDECAR_SUFFIXES:
        for img_ext in [".jpg", ".jpeg", ".png", ".webp"]:
            sidecar = parent / f"{stem}{suffix}{img_ext}"
            if sidecar.exists():
                sidecars.append(sidecar)

    return sidecars


def get_transcode_output_path(source_path: pathlib.Path, transcode_root: pathlib.Path) -> Optional[pathlib.Path]:
    """Find the corresponding encoded file in the transcode folder.

    Output files follow the pattern: {original_stem}.AV1.mp4
    The transcode folder mirrors the library structure.
    """
    # Build expected output path
    # Source: /Volumes/media/tv/Show/Season 1/episode.mkv
    # Output: /Volumes/media/transcode/tv/Show/Season 1/episode.AV1.mp4

    # Find relative path from media root
    source_str = str(source_path)
    rel_path = None
    for root in get_media_roots():
        if source_str.startswith(root):
            rel_path = source_path.relative_to(root)
            break

    # The encoder uses the original stem (with codec markers) and appends .AV1.mp4
    # So we need to match that pattern
    stem = source_path.stem

    # Try structured path first (mirrors library structure)
    if rel_path:
        output_dir = transcode_root / rel_path.parent
        output_path = output_dir / f"{stem}.AV1.mp4"
        if output_path.exists():
            return output_path

    # Try flat structure (all files directly in transcode root)
    flat_output = transcode_root / f"{stem}.AV1.mp4"
    if flat_output.exists():
        return flat_output

    # Also try with stripped codec markers (in case encode was done with older code)
    stem_stripped = stem
    for marker in [".x264", ".x265", ".h264", ".h265", ".HEVC", ".AVC", ".H.264", ".H.265"]:
        stem_stripped = stem_stripped.replace(marker, "")

    if stem_stripped != stem:
        if rel_path:
            output_dir = transcode_root / rel_path.parent
            output_path = output_dir / f"{stem_stripped}.AV1.mp4"
            if output_path.exists():
                return output_path

        flat_output = transcode_root / f"{stem_stripped}.AV1.mp4"
        if flat_output.exists():
            return flat_output

    return None


def cmd_promote(args: argparse.Namespace) -> int:
    """Promote completed encodes - replace originals with encoded files."""
    path = pathlib.Path(args.path).resolve()
    transcode_root = pathlib.Path(args.transcode_root).resolve()

    if not path.exists():
        print(f"[error] Path does not exist: {path}", file=sys.stderr)
        return 1

    if not transcode_root.exists():
        print(f"[error] Transcode root does not exist: {transcode_root}", file=sys.stderr)
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    # Collect video files to process
    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    if not files:
        print("No video files found.")
        return 0

    print(f"Checking {len(files)} files for promotion...")
    print(f"Transcode root: {transcode_root}")
    print()

    promoted = 0
    skipped = 0
    errors = 0

    # Open DB if it exists (for updating status)
    conn = None
    if db_path.exists():
        conn = init_db(db_path)

    for f in files:
        # Skip if already AV1
        if ".AV1." in f.name or f.suffix.lower() == ".av1":
            continue

        # Find corresponding encoded file
        encoded = get_transcode_output_path(f, transcode_root)

        if encoded is None:
            skipped += 1
            continue

        # Calculate new filename for destination
        new_stem = encoded.stem  # Already has .AV1 in it
        new_name = f"{new_stem}.mp4"
        dest_path = f.parent / new_name

        print(f"  {f.name}")
        print(f"    -> {new_name}")

        # Find sidecars
        sidecars = find_sidecars(f)

        if args.dry_run:
            print(f"    [dry-run] Would move encoded file")
            if sidecars:
                print(f"    [dry-run] Would rename {len(sidecars)} sidecar(s)")
            if args.delete_original:
                print(f"    [dry-run] Would delete original")
            continue

        try:
            # Move encoded file to destination
            shutil.move(str(encoded), str(dest_path))

            # Rename sidecars to match new filename
            for sidecar in sidecars:
                sidecar_ext = sidecar.suffix
                # For image sidecars, preserve the suffix
                sidecar_stem = sidecar.stem
                for suffix in IMAGE_SIDECAR_SUFFIXES:
                    if sidecar_stem.endswith(suffix):
                        new_sidecar_name = f"{new_stem.replace('.AV1', '')}{suffix}{sidecar_ext}"
                        break
                else:
                    new_sidecar_name = f"{new_stem.replace('.AV1', '')}{sidecar_ext}"

                new_sidecar_path = sidecar.parent / new_sidecar_name
                if sidecar != new_sidecar_path:
                    sidecar.rename(new_sidecar_path)
                    print(f"    Renamed: {sidecar.name} -> {new_sidecar_name}")

            # Delete original if requested
            if args.delete_original:
                f.unlink()
                print(f"    Deleted original")

            # Update database
            if conn:
                now_str = datetime.now().isoformat()
                conn.execute("""
                    UPDATE media_inventory
                    SET status = 'completed', updated_at = ?
                    WHERE path = ?
                """, (now_str, str(f)))
                conn.execute("""
                    UPDATE encode_results
                    SET promoted = TRUE, promoted_at = ?
                    WHERE source_path = ?
                """, (now_str, str(f)))
                conn.commit()

            promoted += 1
            print(f"    [ok]")

        except Exception as e:
            print(f"    [error] {e}")
            errors += 1

    if conn:
        conn.close()

    print()
    print(f"Promotion complete:")
    print(f"  Promoted: {promoted}")
    print(f"  Skipped (no encode found): {skipped}")
    print(f"  Errors: {errors}")

    return 0 if errors == 0 else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify quality of encoded files by comparing to source."""
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists():
        print(f"[error] Source file not found: {source_path}", file=sys.stderr)
        return 1

    if not encoded_path.exists():
        print(f"[error] Encoded file not found: {encoded_path}", file=sys.stderr)
        return 1

    print(f"Source:  {source_path.name}")
    print(f"Encoded: {encoded_path.name}")
    print()

    # Get file sizes
    source_size = source_path.stat().st_size
    encoded_size = encoded_path.stat().st_size
    ratio = encoded_size / source_size * 100
    print(f"Size: {source_size // 1024 // 1024}MB -> {encoded_size // 1024 // 1024}MB ({ratio:.1f}%)")
    print()

    print("Measuring quality (this may take a while)...")
    print()

    metrics = verify_encode_quality(
        source_path,
        encoded_path,
        sample_duration_sec=args.sample_duration,
        use_vmaf=not args.no_vmaf,
    )

    print()
    print("=" * 50)
    print("Results:")
    print(f"  Grade: {metrics.quality_grade}")

    if metrics.ssim is not None:
        print(f"  SSIM:  {metrics.ssim:.4f}")
    if metrics.psnr is not None:
        print(f"  PSNR:  {metrics.psnr:.2f} dB")
    if metrics.vmaf is not None:
        print(f"  VMAF:  {metrics.vmaf:.2f}")

    if metrics.is_acceptable:
        print()
        print("[ok] Quality is acceptable for promotion")
    else:
        print()
        print("[!] Quality may be too low - consider re-encoding with lower CRF")

    return 0


def cmd_verify_batch(args: argparse.Namespace) -> int:
    """Verify quality of all pending encodes in transcode folder."""
    path = pathlib.Path(args.path).resolve()
    transcode_root = pathlib.Path(args.transcode_root).resolve()

    if not path.exists():
        print(f"[error] Path does not exist: {path}", file=sys.stderr)
        return 1

    if not transcode_root.exists():
        print(f"[error] Transcode root does not exist: {transcode_root}", file=sys.stderr)
        return 1

    # Find all video files in source path
    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    if not files:
        print("No video files found.")
        return 0

    print(f"Checking {len(files)} files for verification...")
    print()

    verified = 0
    skipped = 0
    failed = 0
    results: list[tuple[str, QualityMetrics]] = []

    for f in files:
        # Skip if already AV1
        if ".AV1." in f.name:
            continue

        # Find corresponding encoded file
        encoded = get_transcode_output_path(f, transcode_root)
        if encoded is None:
            skipped += 1
            continue

        print(f"Verifying: {f.name}")

        metrics = verify_encode_quality(
            f,
            encoded,
            sample_duration_sec=args.sample_duration,
            sample_positions=[0.5],  # Single sample for batch
            use_vmaf=not args.no_vmaf,
        )

        results.append((f.name, metrics))

        if metrics.is_acceptable:
            print(f"  -> Grade: {metrics.quality_grade} [OK]")
            verified += 1
        else:
            print(f"  -> Grade: {metrics.quality_grade} [FAIL]")
            failed += 1
        print()

    print("=" * 60)
    print("Summary:")
    print(f"  Verified (acceptable): {verified}")
    print(f"  Failed (low quality):  {failed}")
    print(f"  Skipped (no encode):   {skipped}")
    print()

    if results:
        print("Detailed results:")
        print(f"{'File':<40} {'SSIM':>8} {'PSNR':>8} {'VMAF':>8} {'Grade':>6}")
        print("-" * 72)
        for name, m in results:
            short_name = name[:37] + "..." if len(name) > 40 else name
            ssim_str = f"{m.ssim:.4f}" if m.ssim else "N/A"
            psnr_str = f"{m.psnr:.1f}" if m.psnr else "N/A"
            vmaf_str = f"{m.vmaf:.1f}" if m.vmaf else "N/A"
            print(f"{short_name:<40} {ssim_str:>8} {psnr_str:>8} {vmaf_str:>8} {m.quality_grade:>6}")

    return 0 if failed == 0 else 1


def cmd_review_list(args: argparse.Namespace) -> int:
    """List encodes that need review (outliers and pending)."""
    path = pathlib.Path(args.path).resolve()

    if not path.exists():
        print(f"[error] Path does not exist: {path}", file=sys.stderr)
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        print(f"[error] No database found: {db_path}", file=sys.stderr)
        return 1

    conn = init_db(db_path)

    # Query for items needing review
    if args.all:
        # Show all encodes with quality metrics
        query = """
            SELECT r.id, r.source_path, r.output_path, r.tier,
                   r.output_size_bytes, m.size_bytes as source_size,
                   r.psnr, r.ssim, r.vmaf, r.is_outlier, r.outlier_reasons, r.review_status,
                   r.completed_at
            FROM encode_results r
            LEFT JOIN media_inventory m ON r.source_id = m.id
            WHERE r.psnr IS NOT NULL OR r.ssim IS NOT NULL OR r.vmaf IS NOT NULL
            ORDER BY r.is_outlier DESC, r.vmaf ASC NULLS LAST
        """
    else:
        # Show only pending reviews (outliers not yet reviewed)
        query = """
            SELECT r.id, r.source_path, r.output_path, r.tier,
                   r.output_size_bytes, m.size_bytes as source_size,
                   r.psnr, r.ssim, r.vmaf, r.is_outlier, r.outlier_reasons, r.review_status,
                   r.completed_at
            FROM encode_results r
            LEFT JOIN media_inventory m ON r.source_id = m.id
            WHERE r.is_outlier = 1 AND r.review_status = 'pending'
            ORDER BY r.vmaf ASC NULLS LAST
        """

    results = conn.execute(query).fetchall()
    conn.close()

    if not results:
        if args.all:
            print("No encodes with quality metrics found.")
        else:
            print("No pending reviews. All encodes are approved or no outliers detected.")
        return 0

    # Print header
    print(f"\n{'ID':>4} {'Status':>8} {'VMAF':>6} {'SSIM':>7} {'PSNR':>6} {'Ratio':>6} {'Tier':>8} File")
    print("-" * 100)

    for row in results:
        result_id = row[0]
        source_path = pathlib.Path(row[1])
        output_size = row[4] or 0
        source_size = row[5] or 1
        psnr = row[6]
        ssim = row[7]
        vmaf = row[8]
        is_outlier = row[9]
        outlier_reasons = row[10]
        review_status = row[11] or "pending"
        tier = row[3] or "?"

        ratio = output_size / source_size * 100 if source_size > 0 else 0

        # Format metrics
        vmaf_str = f"{vmaf:.1f}" if vmaf is not None else "-"
        ssim_str = f"{ssim:.4f}" if ssim is not None else "-"
        psnr_str = f"{psnr:.1f}" if psnr is not None else "-"

        # Status indicator
        if review_status == "approved":
            status = "OK"
        elif review_status == "rejected":
            status = "REJ"
        elif is_outlier:
            status = "OUTLIER"
        else:
            status = "pending"

        # Truncate filename
        name = source_path.name
        if len(name) > 45:
            name = name[:42] + "..."

        print(f"{result_id:>4} {status:>8} {vmaf_str:>6} {ssim_str:>7} {psnr_str:>6} {ratio:>5.1f}% {tier:>8} {name}")

        # Show outlier reasons if present
        if is_outlier and outlier_reasons and args.verbose:
            print(f"     Reasons: {outlier_reasons}")

    print()
    print(f"Total: {len(results)} encodes")
    if not args.all:
        print("Use --all to show all encodes with metrics (including approved)")

    return 0


def cmd_review_approve(args: argparse.Namespace) -> int:
    """Approve an encode (mark as reviewed and OK to promote)."""
    path = pathlib.Path(args.path).resolve()

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        print(f"[error] No database found: {db_path}", file=sys.stderr)
        return 1

    conn = init_db(db_path)

    # Update the review status
    result = conn.execute(
        """
        UPDATE encode_results
        SET review_status = 'approved', reviewed_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(), args.id),
    )
    conn.commit()

    if result.rowcount == 0:
        print(f"[error] No encode found with ID {args.id}", file=sys.stderr)
        conn.close()
        return 1

    # Get the encode info
    row = conn.execute(
        "SELECT source_path, output_path FROM encode_results WHERE id = ?",
        (args.id,),
    ).fetchone()
    conn.close()

    if row:
        print(f"Approved: {pathlib.Path(row[0]).name}")
        print(f"  Output: {row[1]}")
    else:
        print(f"Approved encode ID {args.id}")

    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    """Reject an encode (mark for re-encoding or deletion)."""
    path = pathlib.Path(args.path).resolve()

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        print(f"[error] No database found: {db_path}", file=sys.stderr)
        return 1

    conn = init_db(db_path)

    # Get the encode info first
    row = conn.execute(
        "SELECT source_path, output_path FROM encode_results WHERE id = ?",
        (args.id,),
    ).fetchone()

    if not row:
        print(f"[error] No encode found with ID {args.id}", file=sys.stderr)
        conn.close()
        return 1

    source_path = pathlib.Path(row[0])
    output_path = pathlib.Path(row[1]) if row[1] else None

    # Update the review status
    conn.execute(
        """
        UPDATE encode_results
        SET review_status = 'rejected', reviewed_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(), args.id),
    )
    conn.commit()
    conn.close()

    print(f"Rejected: {source_path.name}")

    # Optionally delete the output file
    if args.delete and output_path and output_path.exists():
        output_path.unlink()
        print(f"  Deleted: {output_path}")
    elif output_path and output_path.exists():
        print(f"  Output still exists: {output_path}")
        print("  Use --delete to remove the output file")

    return 0


def generate_compare_html(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    output_file: pathlib.Path,
    source_info: Optional[MediaInfo] = None,
    encoded_info: Optional[MediaInfo] = None,
    encode_id: Optional[int] = None,
    vmaf_score: Optional[float] = None,
) -> None:
    """Generate HTML file for side-by-side video comparison."""
    # Get file sizes
    source_size_mb = source_path.stat().st_size / 1024 / 1024
    encoded_size_mb = encoded_path.stat().st_size / 1024 / 1024
    ratio_pct = encoded_size_mb / source_size_mb * 100

    # Format duration
    duration_str = ""
    if source_info and source_info.duration_seconds:
        mins = int(source_info.duration_seconds // 60)
        secs = int(source_info.duration_seconds % 60)
        duration_str = f"{mins}:{secs:02d}"

    # VMAF display
    vmaf_html = ""
    if vmaf_score is not None:
        vmaf_color = "#4a4" if vmaf_score >= 90 else "#aa4" if vmaf_score >= 80 else "#a44"
        vmaf_html = f'<span style="color: {vmaf_color}">VMAF: {vmaf_score:.1f}</span>'

    # Encode ID for promotion actions
    encode_id_attr = f'data-encode-id="{encode_id}"' if encode_id else ""

    html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>Compare: {source_path.stem}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #1a1a1a;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            padding: 20px;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 10px;
            font-size: 1.2em;
            color: #ccc;
        }}
        .info {{
            text-align: center;
            margin-bottom: 20px;
            font-size: 0.9em;
            color: #888;
        }}
        .container {{
            display: flex;
            gap: 20px;
            justify-content: center;
            flex-wrap: wrap;
        }}
        .video-box {{
            flex: 1;
            max-width: 960px;
            min-width: 400px;
        }}
        .label {{
            text-align: center;
            padding: 10px;
            font-weight: bold;
            font-size: 1.1em;
        }}
        .source .label {{ background: #2d4a2d; }}
        .encoded .label {{ background: #4a2d2d; }}
        video {{
            width: 100%;
            background: #000;
            display: block;
        }}
        .controls {{
            text-align: center;
            margin-top: 20px;
            padding: 15px;
            background: #2a2a2a;
            border-radius: 8px;
        }}
        button {{
            background: #444;
            color: #fff;
            border: none;
            padding: 10px 20px;
            margin: 5px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1em;
        }}
        button:hover {{ background: #555; }}
        button.active {{ background: #0066cc; }}
        .time-display {{
            font-family: monospace;
            font-size: 1.2em;
            margin: 10px 0;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 40px;
            margin-top: 15px;
            font-size: 0.9em;
            color: #aaa;
        }}
        .keyboard-hints {{
            margin-top: 15px;
            font-size: 0.8em;
            color: #666;
        }}
        .seek-bar {{
            width: 80%;
            margin: 15px auto;
            display: block;
        }}
    </style>
</head>
<body {encode_id_attr}>
    <h1>{source_path.stem}</h1>
    <div class="info">
        {duration_str} {vmaf_html}
    </div>

    <div class="container">
        <div class="video-box source">
            <div class="label">SOURCE ({source_size_mb:.0f} MB)</div>
            <video id="source" muted playsinline>
                <source src="file://{source_path}" type="video/mp4">
                Your browser cannot play this file directly.
            </video>
        </div>
        <div class="video-box encoded">
            <div class="label">ENCODED ({encoded_size_mb:.0f} MB)</div>
            <video id="encoded" muted playsinline>
                <source src="file://{encoded_path}" type="video/mp4">
                Your browser cannot play this file directly.
            </video>
        </div>
    </div>

    <div class="controls">
        <input type="range" class="seek-bar" id="seekBar" min="0" max="100" value="0" step="0.1">
        <div class="time-display">
            <span id="currentTime">0:00</span> / <span id="duration">{duration_str or '0:00'}</span>
        </div>
        <div>
            <button onclick="skipTime(-10)">-10s</button>
            <button onclick="skipTime(-5)">-5s</button>
            <button onclick="togglePlay()" id="playBtn">Play</button>
            <button onclick="skipTime(5)">+5s</button>
            <button onclick="skipTime(10)">+10s</button>
        </div>
        <div style="margin-top: 10px;">
            <button onclick="setSpeed(0.25)">0.25x</button>
            <button onclick="setSpeed(0.5)">0.5x</button>
            <button onclick="setSpeed(1)" class="active" id="speed1">1x</button>
            <button onclick="setSpeed(2)">2x</button>
        </div>
        <div class="stats">
            <span>Source: {source_size_mb:.1f} MB</span>
            <span>Encoded: {encoded_size_mb:.1f} MB</span>
            <span>Ratio: {ratio_pct:.0f}%</span>
        </div>
        <div class="keyboard-hints">
            Space: Play/Pause | Left/Right: ±5s | Shift+Left/Right: ±10s | 1-4: Speed
        </div>
    </div>

    <script>
        const source = document.getElementById('source');
        const encoded = document.getElementById('encoded');
        const playBtn = document.getElementById('playBtn');
        const currentTimeEl = document.getElementById('currentTime');
        const durationEl = document.getElementById('duration');
        const seekBar = document.getElementById('seekBar');
        let isSyncing = false;

        function formatTime(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${{mins}}:${{secs.toString().padStart(2, '0')}}`;
        }}

        function syncVideos(primary, secondary) {{
            if (isSyncing) return;
            isSyncing = true;
            secondary.currentTime = primary.currentTime;
            setTimeout(() => isSyncing = false, 50);
        }}

        source.addEventListener('seeked', () => syncVideos(source, encoded));
        source.addEventListener('timeupdate', () => {{
            syncVideos(source, encoded);
            currentTimeEl.textContent = formatTime(source.currentTime);
            if (source.duration) {{
                seekBar.value = (source.currentTime / source.duration) * 100;
            }}
        }});

        source.addEventListener('loadedmetadata', () => {{
            durationEl.textContent = formatTime(source.duration);
        }});

        source.addEventListener('play', () => {{
            encoded.play();
            playBtn.textContent = 'Pause';
        }});

        source.addEventListener('pause', () => {{
            encoded.pause();
            playBtn.textContent = 'Play';
        }});

        seekBar.addEventListener('input', () => {{
            if (source.duration) {{
                source.currentTime = (seekBar.value / 100) * source.duration;
            }}
        }});

        function togglePlay() {{
            if (source.paused) {{
                source.play();
            }} else {{
                source.pause();
            }}
        }}

        function skipTime(delta) {{
            source.currentTime = Math.max(0, Math.min(source.duration || 0, source.currentTime + delta));
        }}

        function setSpeed(speed) {{
            source.playbackRate = speed;
            encoded.playbackRate = speed;
            document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
            if (speed === 1) document.getElementById('speed1').classList.add('active');
            event.target.classList.add('active');
        }}

        // Keyboard controls
        document.addEventListener('keydown', (e) => {{
            if (e.code === 'Space') {{
                e.preventDefault();
                togglePlay();
            }} else if (e.code === 'ArrowLeft') {{
                e.preventDefault();
                skipTime(e.shiftKey ? -10 : -5);
            }} else if (e.code === 'ArrowRight') {{
                e.preventDefault();
                skipTime(e.shiftKey ? 10 : 5);
            }} else if (e.key >= '1' && e.key <= '4') {{
                const speeds = [0.25, 0.5, 1, 2];
                setSpeed(speeds[parseInt(e.key) - 1]);
            }}
        }});

        // Click on either video to play/pause
        source.addEventListener('click', togglePlay);
        encoded.addEventListener('click', togglePlay);
    </script>
</body>
</html>
'''
    output_file.write_text(html_content)


def cmd_compare_clips(args: argparse.Namespace) -> int:
    """Extract clips and generate HTML for lossless side-by-side comparison."""
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists():
        print(f"[error] Source file not found: {source_path}", file=sys.stderr)
        return 1

    if not encoded_path.exists():
        print(f"[error] Encoded file not found: {encoded_path}", file=sys.stderr)
        return 1

    # Determine output location
    if args.output:
        compare_dir = pathlib.Path(args.output)
    else:
        compare_dir = pathlib.Path("/Volumes/media/transcode/_compare")

    # Create subdirectory for this comparison
    safe_name = re.sub(r"[^\w\-.]", "_", source_path.stem)[:50]
    clip_dir = compare_dir / f"compare_{safe_name}"
    clip_dir.mkdir(parents=True, exist_ok=True)

    # Get duration for seeking
    source_info = probe_media(source_path)
    if source_info is None:
        print(f"[error] Failed to probe source: {source_path}", file=sys.stderr)
        return 1

    duration = source_info.duration_seconds or 60
    seek_pos = args.seek if args.seek is not None else duration / 2
    clip_duration = args.duration

    # Ensure we don't seek past the end
    if seek_pos + clip_duration > duration:
        seek_pos = max(0, duration - clip_duration - 5)

    print(f"Extracting comparison clips (no re-encoding)...")
    print(f"  Source: {source_path.name}")
    print(f"  Encoded: {encoded_path.name}")
    print(f"  Position: {seek_pos:.1f}s, Duration: {clip_duration}s")

    # Determine output format - try to keep source codec for browser compatibility
    # For H.264 source, keep as .mp4; for AV1 encoded, keep as .mp4
    source_clip = clip_dir / "source.mp4"
    encoded_clip = clip_dir / "encoded.mp4"

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[error] ffmpeg not found", file=sys.stderr)
        return 1

    # Extract source clip - copy video stream (remux to MP4 for browser compatibility)
    # Most sources are H.264 which browsers handle natively
    cmd_source = [
        ffmpeg, "-y",
        "-ss", str(seek_pos),
        "-i", str(source_path),
        "-t", str(clip_duration),
        "-c:v", "copy",  # Copy video stream as-is
        "-an",  # No audio for comparison
        str(source_clip),
    ]

    # Extract encoded clip - copy if already AV1/MP4, otherwise re-encode
    # AV1 in MP4 should work in modern browsers
    cmd_encoded = [
        ffmpeg, "-y",
        "-ss", str(seek_pos),
        "-i", str(encoded_path),
        "-t", str(clip_duration),
        "-c:v", "copy",  # Copy AV1 stream as-is
        "-an",
        str(encoded_clip),
    ]

    try:
        print("  Extracting source clip...")
        subprocess.run(cmd_source, check=True, capture_output=True)
        print("  Extracting encoded clip...")
        subprocess.run(cmd_encoded, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"[error] Failed to extract clips: {e.stderr.decode()[:500]}", file=sys.stderr)
        return 1

    # Get file sizes for display
    source_clip_size = source_clip.stat().st_size / 1024 / 1024
    encoded_clip_size = encoded_clip.stat().st_size / 1024 / 1024

    # Generate HTML file for synced playback
    html_file = clip_dir / "compare.html"
    html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>Video Comparison: {source_path.stem}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #1a1a1a;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            padding: 20px;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 10px;
            font-size: 1.2em;
            color: #ccc;
        }}
        .info {{
            text-align: center;
            margin-bottom: 20px;
            font-size: 0.9em;
            color: #888;
        }}
        .container {{
            display: flex;
            gap: 20px;
            justify-content: center;
            flex-wrap: wrap;
        }}
        .video-box {{
            flex: 1;
            max-width: 960px;
            min-width: 400px;
        }}
        .label {{
            text-align: center;
            padding: 10px;
            font-weight: bold;
            font-size: 1.1em;
        }}
        .source .label {{ background: #2d4a2d; }}
        .encoded .label {{ background: #4a2d2d; }}
        video {{
            width: 100%;
            background: #000;
            display: block;
        }}
        .controls {{
            text-align: center;
            margin-top: 20px;
            padding: 15px;
            background: #2a2a2a;
            border-radius: 8px;
        }}
        button {{
            background: #444;
            color: #fff;
            border: none;
            padding: 10px 20px;
            margin: 5px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1em;
        }}
        button:hover {{ background: #555; }}
        button.active {{ background: #0066cc; }}
        .time-display {{
            font-family: monospace;
            font-size: 1.2em;
            margin: 10px 0;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 40px;
            margin-top: 15px;
            font-size: 0.9em;
            color: #aaa;
        }}
        .keyboard-hints {{
            margin-top: 15px;
            font-size: 0.8em;
            color: #666;
        }}
    </style>
</head>
<body>
    <h1>{source_path.stem}</h1>
    <div class="info">
        Clip from {seek_pos:.1f}s to {seek_pos + clip_duration:.1f}s ({clip_duration}s duration)
    </div>

    <div class="container">
        <div class="video-box source">
            <div class="label">SOURCE (Original)</div>
            <video id="source" muted playsinline>
                <source src="source.mp4" type="video/mp4">
            </video>
        </div>
        <div class="video-box encoded">
            <div class="label">ENCODED (AV1)</div>
            <video id="encoded" muted playsinline>
                <source src="encoded.mp4" type="video/mp4">
            </video>
        </div>
    </div>

    <div class="controls">
        <div class="time-display">
            <span id="currentTime">0:00.000</span> / <span id="duration">0:00</span>
        </div>
        <div>
            <button onclick="skipTime(-5)">-5s</button>
            <button onclick="skipTime(-1)">-1s</button>
            <button onclick="togglePlay()" id="playBtn">Play</button>
            <button onclick="skipTime(1)">+1s</button>
            <button onclick="skipTime(5)">+5s</button>
        </div>
        <div style="margin-top: 10px;">
            <button onclick="setSpeed(0.25)">0.25x</button>
            <button onclick="setSpeed(0.5)">0.5x</button>
            <button onclick="setSpeed(1)" class="active" id="speed1">1x</button>
            <button onclick="setSpeed(2)">2x</button>
        </div>
        <div class="stats">
            <span>Source clip: {source_clip_size:.1f} MB</span>
            <span>Encoded clip: {encoded_clip_size:.1f} MB</span>
            <span>Ratio: {encoded_clip_size/source_clip_size*100:.0f}%</span>
        </div>
        <div class="keyboard-hints">
            Space: Play/Pause | Left/Right: ±1s | Shift+Left/Right: ±5s | 1-4: Speed
        </div>
    </div>

    <script>
        const source = document.getElementById('source');
        const encoded = document.getElementById('encoded');
        const playBtn = document.getElementById('playBtn');
        const currentTimeEl = document.getElementById('currentTime');
        const durationEl = document.getElementById('duration');
        let isSyncing = false;

        function formatTime(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = (seconds % 60).toFixed(3);
            return `${{mins}}:${{secs.padStart(6, '0')}}`;
        }}

        function formatDuration(seconds) {{
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${{mins}}:${{secs.toString().padStart(2, '0')}}`;
        }}

        function syncVideos(primary, secondary) {{
            if (isSyncing) return;
            isSyncing = true;
            secondary.currentTime = primary.currentTime;
            setTimeout(() => isSyncing = false, 50);
        }}

        source.addEventListener('seeked', () => syncVideos(source, encoded));
        source.addEventListener('timeupdate', () => {{
            syncVideos(source, encoded);
            currentTimeEl.textContent = formatTime(source.currentTime);
        }});

        source.addEventListener('loadedmetadata', () => {{
            durationEl.textContent = formatDuration(source.duration);
        }});

        source.addEventListener('play', () => {{
            encoded.play();
            playBtn.textContent = 'Pause';
        }});

        source.addEventListener('pause', () => {{
            encoded.pause();
            playBtn.textContent = 'Play';
        }});

        function togglePlay() {{
            if (source.paused) {{
                source.play();
            }} else {{
                source.pause();
            }}
        }}

        function skipTime(delta) {{
            source.currentTime = Math.max(0, Math.min(source.duration, source.currentTime + delta));
        }}

        function setSpeed(speed) {{
            source.playbackRate = speed;
            encoded.playbackRate = speed;
            document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
            if (speed === 1) document.getElementById('speed1').classList.add('active');
            event.target.classList.add('active');
        }}

        // Keyboard controls
        document.addEventListener('keydown', (e) => {{
            if (e.code === 'Space') {{
                e.preventDefault();
                togglePlay();
            }} else if (e.code === 'ArrowLeft') {{
                e.preventDefault();
                skipTime(e.shiftKey ? -5 : -1);
            }} else if (e.code === 'ArrowRight') {{
                e.preventDefault();
                skipTime(e.shiftKey ? 5 : 1);
            }} else if (e.key >= '1' && e.key <= '4') {{
                const speeds = [0.25, 0.5, 1, 2];
                setSpeed(speeds[parseInt(e.key) - 1]);
            }}
        }});

        // Click on either video to play/pause
        source.addEventListener('click', togglePlay);
        encoded.addEventListener('click', togglePlay);
    </script>
</body>
</html>
'''

    html_file.write_text(html_content)

    print(f"\nComparison ready:")
    print(f"  Directory: {clip_dir}")
    print(f"  HTML viewer: {html_file}")
    print(f"\nOpen with: open \"{html_file}\"")

    # Try to open automatically on macOS
    if platform.system() == "Darwin":
        try:
            subprocess.run(["open", str(html_file)], check=False)
        except Exception:
            pass

    return 0


def cmd_compare_full(args: argparse.Namespace) -> int:
    """Generate HTML for side-by-side comparison of full video files (no clip extraction)."""
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists():
        print(f"[error] Source file not found: {source_path}", file=sys.stderr)
        return 1

    if not encoded_path.exists():
        print(f"[error] Encoded file not found: {encoded_path}", file=sys.stderr)
        return 1

    # Determine output location
    if args.output:
        compare_dir = pathlib.Path(args.output)
    else:
        compare_dir = pathlib.Path("/Volumes/media/transcode/_compare")
    compare_dir.mkdir(parents=True, exist_ok=True)

    # Generate HTML filename
    safe_name = re.sub(r"[^\w\-.]", "_", source_path.stem)[:50]
    html_file = compare_dir / f"compare_{safe_name}.html"

    # Get source info for duration display
    source_info = probe_media(source_path)

    print(f"Generating comparison HTML (full videos)...")
    print(f"  Source: {source_path}")
    print(f"  Encoded: {encoded_path}")

    generate_compare_html(
        source_path=source_path,
        encoded_path=encoded_path,
        output_file=html_file,
        source_info=source_info,
    )

    print(f"\nComparison ready: {html_file}")
    print(f"Open with: open \"{html_file}\"")

    # Auto-open on macOS
    if platform.system() == "Darwin":
        try:
            subprocess.run(["open", str(html_file)], check=False)
        except Exception:
            pass

    return 0


def cmd_review_compare(args: argparse.Namespace) -> int:
    """Generate a side-by-side comparison video of source vs encoded."""
    path = pathlib.Path(args.path).resolve()

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        print(f"[error] No database found: {db_path}", file=sys.stderr)
        return 1

    conn = init_db(db_path)

    # Get the encode info
    row = conn.execute(
        "SELECT source_path, output_path, vmaf FROM encode_results WHERE id = ?",
        (args.id,),
    ).fetchone()
    conn.close()

    if not row:
        print(f"[error] No encode found with ID {args.id}", file=sys.stderr)
        return 1

    source_path = pathlib.Path(row[0])
    output_path = pathlib.Path(row[1]) if row[1] else None

    if not source_path.exists():
        print(f"[error] Source file not found: {source_path}", file=sys.stderr)
        return 1

    if not output_path or not output_path.exists():
        print(f"[error] Encoded file not found: {output_path}", file=sys.stderr)
        return 1

    # Determine output location - put compare videos in _compare subfolder
    if args.output:
        compare_dir = pathlib.Path(args.output) / "_compare"
    else:
        compare_dir = pathlib.Path("/tmp/av1_compare")
    compare_dir.mkdir(parents=True, exist_ok=True)

    # Generate comparison filename
    compare_file = compare_dir / f"compare_{args.id}_{source_path.stem}.mp4"

    # Get duration for seeking
    source_info = probe_media(source_path)
    if source_info is None:
        print(f"[error] Failed to probe source: {source_path}", file=sys.stderr)
        return 1

    duration = source_info.duration_seconds or 60

    # Determine sample positions - use middle by default, or worst VMAF spots if available
    if args.seek:
        positions = [args.seek]
    else:
        # Use mid-point
        positions = [duration / 2]

    clip_duration = args.duration

    print(f"Generating comparison video...")
    print(f"  Source: {source_path.name}")
    print(f"  Encoded: {output_path.name}")
    print(f"  Position: {positions[0]:.1f}s, Duration: {clip_duration}s")

    # Build ffmpeg command for stacked comparison at source resolution
    # Stack vertically with labels (SOURCE on top, ENCODED on bottom)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(positions[0]),
        "-i", str(source_path),
        "-ss", str(positions[0]),
        "-i", str(output_path),
        "-t", str(clip_duration),
        "-filter_complex",
        "[0:v]drawtext=text='SOURCE':fontsize=36:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10[top];"
        "[1:v]drawtext=text='ENCODED':fontsize=36:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10[bottom];"
        "[top][bottom]vstack=inputs=2[v]",
        "-map", "[v]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        str(compare_file),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"\nComparison saved: {compare_file}")
        print(f"Open with: open \"{compare_file}\"")
    except subprocess.CalledProcessError as e:
        print(f"[error] Failed to generate comparison: {e.stderr.decode()[:500]}", file=sys.stderr)
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mediaforce content-aware encoder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser(
        "status",
        help="Show platform info, media roots, and available encoders",
    )
    p_status.set_defaults(func=cmd_status)

    # analyze
    p_analyze = subparsers.add_parser(
        "analyze",
        help="Analyze media files and show classification",
    )
    p_analyze.add_argument("path", help="Video file or directory to analyze")
    p_analyze.set_defaults(func=cmd_analyze)

    # scan
    p_scan = subparsers.add_parser(
        "scan",
        help="Scan library and populate inventory database",
    )
    p_scan.add_argument("path", help="Library path to scan (e.g., /Volumes/media/tv)")
    p_scan.set_defaults(func=cmd_scan)

    # queue
    p_queue = subparsers.add_parser(
        "queue",
        help="Show encoding queue (priority order)",
    )
    p_queue.add_argument("path", help="Library path (e.g., /Volumes/media/tv)")
    p_queue.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of files to show (default: 20)",
    )
    p_queue.set_defaults(func=cmd_queue)

    # run (queue-based encoding)
    p_run = subparsers.add_parser(
        "run",
        help="Run queue-based encoding from inventory database",
    )
    p_run.add_argument("path", help="Library path (e.g., /Volumes/media/tv)")
    p_run.add_argument(
        "-o", "--output",
        default="/Volumes/media/transcode",
        help="Output directory root (default: /Volumes/media/transcode)",
    )
    p_run.add_argument(
        "--until",
        help="Stop after this time (HH:MM format, e.g., 05:00)",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without encoding",
    )
    p_run.add_argument(
        "--force",
        action="store_true",
        help="Re-encode even if output exists",
    )
    p_run.add_argument(
        "--verify",
        action="store_true",
        help="Run quality verification (VMAF/SSIM/PSNR) after each encode and flag outliers",
    )
    p_run.add_argument(
        "--verify-duration",
        type=int,
        default=60,
        help="Duration in seconds for quality verification samples (default: 60)",
    )
    p_run.add_argument(
        "--sample-vmaf",
        dest="sample_vmaf",
        action="store_true",
        default=True,
        help="Enable VMAF sampling to adjust tier before encode (default: on)",
    )
    p_run.add_argument(
        "--no-sample-vmaf",
        dest="sample_vmaf",
        action="store_false",
        help="Disable VMAF sampling",
    )
    p_run.add_argument("--sample-count", type=int, default=3, help="Number of VMAF samples (default: 3)")
    p_run.add_argument("--sample-length", type=float, default=8.0, help="Sample length in seconds (default: 8)")
    p_run.add_argument(
        "--sample-motion-aware",
        dest="sample_motion_aware",
        action="store_true",
        default=True,
        help="Pick samples from highest-bitrate windows (default: on)",
    )
    p_run.add_argument(
        "--no-sample-motion-aware",
        dest="sample_motion_aware",
        action="store_false",
        help="Use evenly spaced samples instead of motion-aware",
    )
    p_run.add_argument(
        "--max-concurrency",
        type=int,
        help="Maximum concurrent encodes on this host (overrides settings/env)",
    )
    p_run.add_argument(
        "--hw-decode",
        dest="hw_decode",
        action="store_true",
        default=True,
        help="Enable hardware decoding when available (default: on)",
    )
    p_run.add_argument(
        "--no-hw-decode",
        dest="hw_decode",
        action="store_false",
        help="Disable hardware decoding",
    )
    p_run.add_argument(
        "--offpeak",
        dest="offpeak_enabled",
        action="store_true",
        help="Enable off-peak window enforcement",
    )
    p_run.add_argument(
        "--offpeak-start",
        type=str,
        default=None,
        help="Off-peak start time HH:MM (default 00:00)",
    )
    p_run.add_argument(
        "--offpeak-end",
        type=str,
        default=None,
        help="Off-peak end time HH:MM (default 05:00)",
    )
    p_run.add_argument(
        "--hw-encode",
        dest="hw_encode",
        action="store_true",
        default=False,
        help="Enable hardware encoding when available (default: off)",
    )
    p_run.add_argument(
        "--no-hw-encode",
        dest="hw_encode",
        action="store_false",
        help="Disable hardware encoding",
    )
    p_run.add_argument("--autoupdate-url", help="Base URL hosting manifest.json and raw files for updates")
    p_run.add_argument("--autoupdate-interval", type=int, default=0, help="Seconds between update checks (0 = only at startup)")
    p_run.add_argument("--settings-url", help="Remote settings endpoint (e.g., http://host:5555/api/settings/current)")
    p_run.set_defaults(func=cmd_run)

    # watch (auto-queue new files as they appear)
    p_watch = subparsers.add_parser(
        "watch",
        help="Watch configured libraries and auto-queue new files",
    )
    p_watch.add_argument("--autoupdate-url", help="Base URL hosting manifest.json and raw files for updates")
    p_watch.add_argument("--autoupdate-interval", type=int, default=3600, help="Seconds between update checks (default: 3600)")
    p_watch.add_argument("--settings-url", help="Remote settings endpoint (e.g., http://host:5555/api/settings/current)")
    p_watch.set_defaults(func=cmd_watch)

    # encode
    p_encode = subparsers.add_parser(
        "encode",
        help="Encode media files to AV1",
    )
    p_encode.add_argument("path", help="Video file or directory to encode")
    p_encode.add_argument("-o", "--output", required=True, help="Output directory")
    p_encode.add_argument(
        "--tier",
        choices=["pristine", "good", "mediocre", "poor"],
        help="Override automatic tier classification",
    )
    p_encode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without encoding",
    )
    p_encode.add_argument(
        "--force",
        action="store_true",
        help="Re-encode even if output exists or source is AV1",
    )
    p_encode.add_argument(
        "--sample-vmaf",
        dest="sample_vmaf",
        action="store_true",
        default=True,
        help="Enable VMAF sampling to adjust tier (default: on)",
    )
    p_encode.add_argument(
        "--no-sample-vmaf",
        dest="sample_vmaf",
        action="store_false",
        help="Disable VMAF sampling",
    )
    p_encode.add_argument("--sample-count", type=int, default=3, help="Number of VMAF samples (default: 3)")
    p_encode.add_argument("--sample-length", type=float, default=8.0, help="Sample length in seconds (default: 8)")
    p_encode.add_argument(
        "--sample-motion-aware",
        dest="sample_motion_aware",
        action="store_true",
        default=True,
        help="Pick samples from highest-bitrate windows (default: on)",
    )
    p_encode.add_argument(
        "--no-sample-motion-aware",
        dest="sample_motion_aware",
        action="store_false",
        help="Use evenly spaced samples instead of motion-aware",
    )
    p_encode.add_argument(
        "--hw-decode",
        dest="hw_decode",
        action="store_true",
        default=True,
        help="Enable hardware decoding when available (default: on)",
    )
    p_encode.add_argument(
        "--no-hw-decode",
        dest="hw_decode",
        action="store_false",
        help="Disable hardware decoding",
    )
    p_encode.add_argument(
        "--hw-encode",
        dest="hw_encode",
        action="store_true",
        default=False,
        help="Enable hardware encoding when available (default: off)",
    )
    p_encode.add_argument(
        "--no-hw-encode",
        dest="hw_encode",
        action="store_false",
        help="Disable hardware encoding",
    )
    p_encode.set_defaults(func=cmd_encode)

    # promote
    p_promote = subparsers.add_parser(
        "promote",
        help="Replace originals with encoded files from transcode folder",
    )
    p_promote.add_argument("path", help="Library path to promote (e.g., /Volumes/media/tv)")
    p_promote.add_argument(
        "--transcode-root",
        default="/Volumes/media/transcode",
        help="Root of transcode folder (default: /Volumes/media/transcode)",
    )
    p_promote.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without moving files",
    )
    p_promote.add_argument(
        "--delete-original",
        action="store_true",
        default=True,
        help="Delete original after promotion (default: True)",
    )
    p_promote.add_argument(
        "--no-delete",
        action="store_false",
        dest="delete_original",
        help="Keep original file after promotion",
    )
    p_promote.set_defaults(func=cmd_promote)

    # verify (single file pair)
    p_verify = subparsers.add_parser(
        "verify",
        help="Verify quality of a single encoded file vs source",
    )
    p_verify.add_argument("source", help="Source (original) video file")
    p_verify.add_argument("encoded", help="Encoded video file")
    p_verify.add_argument(
        "--sample-duration",
        type=int,
        default=30,
        help="Duration of each sample clip in seconds (default: 30)",
    )
    p_verify.add_argument(
        "--no-vmaf",
        action="store_true",
        help="Skip VMAF measurement (faster, SSIM/PSNR only)",
    )
    p_verify.set_defaults(func=cmd_verify)

    # verify-batch (batch verify from transcode folder)
    p_verify_batch = subparsers.add_parser(
        "verify-batch",
        help="Verify quality of all pending encodes in transcode folder",
    )
    p_verify_batch.add_argument("path", help="Library path (e.g., /Volumes/media/tv)")
    p_verify_batch.add_argument(
        "--transcode-root",
        default="/Volumes/media/transcode",
        help="Root of transcode folder (default: /Volumes/media/transcode)",
    )
    p_verify_batch.add_argument(
        "--sample-duration",
        type=int,
        default=30,
        help="Duration of each sample clip in seconds (default: 30)",
    )
    p_verify_batch.add_argument(
        "--no-vmaf",
        action="store_true",
        help="Skip VMAF measurement (faster, SSIM/PSNR only)",
    )
    p_verify_batch.set_defaults(func=cmd_verify_batch)

    # review list
    p_review_list = subparsers.add_parser(
        "review-list",
        help="List encodes needing review (outliers and pending)",
    )
    p_review_list.add_argument("path", help="Library path (e.g., /Volumes/media/tv)")
    p_review_list.add_argument(
        "--all",
        action="store_true",
        help="Show all encodes with metrics (not just pending reviews)",
    )
    p_review_list.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed outlier reasons",
    )
    p_review_list.set_defaults(func=cmd_review_list)

    # review approve
    p_review_approve = subparsers.add_parser(
        "review-approve",
        help="Approve an encode (mark as OK to promote)",
    )
    p_review_approve.add_argument("path", help="Library path")
    p_review_approve.add_argument("id", type=int, help="Encode result ID to approve")
    p_review_approve.set_defaults(func=cmd_review_approve)

    # review reject
    p_review_reject = subparsers.add_parser(
        "review-reject",
        help="Reject an encode (mark for re-encoding)",
    )
    p_review_reject.add_argument("path", help="Library path")
    p_review_reject.add_argument("id", type=int, help="Encode result ID to reject")
    p_review_reject.add_argument(
        "--delete",
        action="store_true",
        help="Delete the encoded output file",
    )
    p_review_reject.set_defaults(func=cmd_review_reject)

    # review compare
    p_review_compare = subparsers.add_parser(
        "review-compare",
        help="Generate side-by-side comparison video of source vs encoded",
    )
    p_review_compare.add_argument("path", help="Library path")
    p_review_compare.add_argument("id", type=int, help="Encode result ID to compare")
    p_review_compare.add_argument(
        "-o", "--output",
        help="Output directory for comparison video (default: /tmp/av1_compare)",
    )
    p_review_compare.add_argument(
        "--seek",
        type=float,
        help="Start position in seconds (default: middle of video)",
    )
    p_review_compare.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Duration of comparison clip in seconds (default: 10)",
    )
    p_review_compare.set_defaults(func=cmd_review_compare)

    # compare-clips - HTML-based lossless comparison
    p_compare_clips = subparsers.add_parser(
        "compare-clips",
        help="Extract clips and generate HTML for lossless side-by-side comparison",
    )
    p_compare_clips.add_argument("source", help="Source video file path")
    p_compare_clips.add_argument("encoded", help="Encoded video file path")
    p_compare_clips.add_argument(
        "-o", "--output",
        help="Output directory (default: /Volumes/media/transcode/_compare)",
    )
    p_compare_clips.add_argument(
        "--seek",
        type=float,
        help="Start position in seconds (default: middle of video)",
    )
    p_compare_clips.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Duration of comparison clip in seconds (default: 30)",
    )
    p_compare_clips.set_defaults(func=cmd_compare_clips)

    # compare-full - HTML comparison using full video files (no clip extraction)
    p_compare_full = subparsers.add_parser(
        "compare-full",
        help="Generate HTML for side-by-side comparison of full video files",
    )
    p_compare_full.add_argument("source", help="Source video file path")
    p_compare_full.add_argument("encoded", help="Encoded video file path")
    p_compare_full.add_argument(
        "-o", "--output",
        help="Output directory for HTML file (default: /Volumes/media/transcode/_compare)",
    )
    p_compare_full.set_defaults(func=cmd_compare_full)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
