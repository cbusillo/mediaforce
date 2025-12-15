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
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from sqlmodel import Session, select
from sqlalchemy import text, func, desc

from mediaforce.config.logging import (
    configure_logging,
    env_log_config,
    log_event as _structured_log_event,
)

from mediaforce.services.promote import promote_encoded_file_atomic, rollback_promote
from mediaforce.services.classification import TIER_SETTINGS, adjust_tier_with_vmaf, classify_source
from mediaforce.services.show_overrides import get_default_tier_for_show, import_show_config_json
from mediaforce.services.remote_settings import load_remote_settings
from mediaforce.services.worker_api import WorkerApiClient, WorkerApiError

from mediaforce.domain.types import ClassificationResult, MediaInfo, SourceTier, TierSettings

from mediaforce.db import (
    EncodeResult,
    MediaItem,
    ProfileSettingsSource,
    ProfileEvaluation,
    VmafSample,
    now_iso,
)

from mediaforce.config.paths import (
    default_transcode_root,
    find_library_for_path,
    get_library_root,
    get_media_roots,
    iter_libraries_for_current_host,
    normalize_path,
)
from mediaforce.config.settings import AppSettings, CONFIG_DIR, ENGINE, INVENTORY_DB, load_app_settings
from mediaforce.services.encoder import (
    parse_ffmpeg_progress as svc_parse_ffmpeg_progress,
    record_encode_result as svc_record_encode_result,
    run_ffmpeg_with_progress as svc_run_ffmpeg_with_progress,
)
from mediaforce.services.media_probe import probe_media
from mediaforce.services.progress import (
    finish_progress_tracking as svc_finish_progress_tracking,
    start_progress_tracking as svc_start_progress_tracking,
    update_progress as svc_update_progress,
)
from mediaforce.services.queue import (
    check_missing_outputs as svc_check_missing_outputs,
    claim_next_file as svc_claim_next_file,
    recalculate_priorities as svc_recalculate_priorities,
    release_claim as svc_release_claim,
)
from mediaforce.services.scanner import (
    calculate_priority as svc_calculate_priority,
    scan_file_to_db as svc_scan_file_to_db,
    VIDEO_EXTENSIONS,
)
from mediaforce.services.watch import watch_libraries as svc_watch_libraries
from mediaforce.services.notifications import send_notifications


REMOTE_SETTINGS_URL: str | None = None

# Configure the base logger once so services that log against the default
# component ("mediaforce") inherit handlers/formatters.
configure_logging(env_log_config(component="mediaforce"))

# CLI events use a dedicated logger so we can optionally emit compact human
# output to stderr while keeping structured JSON on stdout.
CLI_LOGGER = configure_logging(env_log_config(component="mediaforce.cli"))


def log_event(level: int, message: str, **fields: Any) -> None:
    _structured_log_event(level, message, logger=CLI_LOGGER, **fields)


def log_info(message: str, **fields: Any) -> None:
    log_event(logging.INFO, message, **fields)


def log_warn(message: str, **fields: Any) -> None:
    log_event(logging.WARNING, message, **fields)


def log_error(message: str, **fields: Any) -> None:
    log_event(logging.ERROR, message, **fields)


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


# Package-relative files that worker autoupdate should pull
AUTOUPDATE_FILES = [
    "__init__.py",
    "core.py",
    "db/__init__.py",
    "db/models.py",
]


def _fetch_remote_profile_settings(url: str, existing_etag: str | None = None) -> tuple[Optional[str], Optional[str]]:
    """Fetch remote profile settings payload and return (payload, etag)."""

    headers = {"User-Agent": "mediaforce/0.2"}
    if existing_etag:
        headers["If-None-Match"] = existing_etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # type: ignore[arg-type]
            if resp.status == 304:
                return None, existing_etag
            payload = resp.read().decode("utf-8")
            etag = resp.headers.get("ETag")
            return payload, etag
    except Exception:
        return None, None


def ensure_active_profile_settings(session: Session) -> Optional[ProfileSettingsSource]:
    """Return active profile settings, refreshing from REMOTE_SETTINGS_URL when configured."""

    src = session.exec(
        select(ProfileSettingsSource)
        .where(ProfileSettingsSource.is_active)
        .order_by(text("id DESC"))
    ).first()

    if REMOTE_SETTINGS_URL is None:
        return src

    needs_fetch = src is None
    if src and src.fetched_at:
        try:
            last = datetime.fromisoformat(src.fetched_at)
            needs_fetch = (datetime.now() - last).total_seconds() > 24 * 3600
        except Exception:
            needs_fetch = True

    if not needs_fetch:
        return src

    payload, etag = _fetch_remote_profile_settings(REMOTE_SETTINGS_URL, existing_etag=src.etag if src else None)
    if payload is None and etag == (src.etag if src else None):
        return src
    if payload:
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        new_src = ProfileSettingsSource(
            name="remote-default",
            source_type="remote",
            url=REMOTE_SETTINGS_URL,
            etag=etag,
            checksum=checksum,
            payload=payload,
            fetched_at=datetime.now().isoformat(),
            applied_at=datetime.now().isoformat(),
            is_active=True,
        )
        session.add(new_src)
        session.commit()
        session.refresh(new_src)
        return new_src
    return src


def _extract_thresholds(source: Optional[ProfileSettingsSource]) -> tuple[float, float]:
    """Return (threshold_min, threshold_median) defaults if missing."""

    default_min = 82.0
    default_median = 92.0
    if not source:
        return default_min, default_median
    try:
        data = json.loads(source.payload)
        thresholds = data.get("thresholds", {})
        return float(thresholds.get("min", default_min)), float(thresholds.get("median", default_median))
    except Exception:
        return default_min, default_median


def create_profile_evaluation(
    session: Session,
    media_id: int,
    selected_profile: str,
    settings_source: Optional[ProfileSettingsSource],
    sample_count: int,
    sample_length: float,
) -> ProfileEvaluation:
    thresholds = _extract_thresholds(settings_source)
    eval_obj = ProfileEvaluation(
        media_id=media_id,
        selected_profile=selected_profile,
        sample_strategy="3x8s_motion",
        sample_count=sample_count,
        sample_length=sample_length,
        threshold_min=thresholds[0],
        threshold_median=thresholds[1],
        status="running",
        settings_source_id=settings_source.id if settings_source else None,
    )
    session.add(eval_obj)
    session.commit()
    session.refresh(eval_obj)
    return eval_obj


def save_vmaf_samples(
    session: Session,
    evaluation_id: int,
    timestamps: list[float],
    scores: list[float],
    sample_length: float,
):
    kinds = ["short", "mid", "motion"]
    for idx, (ts, score) in enumerate(zip(timestamps, scores)):
        sample = VmafSample(
            evaluation_id=evaluation_id,
            sample_kind=kinds[idx] if idx < len(kinds) else "auto",
            start_sec=ts,
            duration_sec=sample_length,
            vmaf=score,
        )
        session.add(sample)
    session.commit()


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


# Denoise filter strings for ffmpeg
DENOISE_FILTERS: dict[str, str] = {
    "light": "hqdn3d=2:2:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
    "heavy": "nlmeans=s=3.0:p=7:r=9",
}

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


def build_sample_plan(
    info: MediaInfo,
    count: int = 3,
    sample_len: float = 8.0,
    motion_aware: bool = True,
) -> list[tuple[float, float, str]]:
    """Return a weighted sampling plan.

    Each entry is (start_sec, weight, label). The first two samples are
    representative "short" and "mid" positions, and the final one is a
    "motion" sample (optionally chosen via window bitrate probing).
    """

    duration = info.duration_seconds or 0.0
    if duration <= 0:
        return []

    def clamp_ts(ts: float) -> float:
        return max(0.0, min(ts, max(0.0, duration - sample_len)))

    plan: list[tuple[float, float, str]] = []
    if count >= 1:
        plan.append((clamp_ts(duration * 0.15), 1.0, "short"))
    if count >= 2:
        plan.append((clamp_ts(duration * 0.50), 1.0, "mid"))

    if count >= 3:
        motion_ts = clamp_ts(duration * 0.75)
        motion_weight = 1.0

        if motion_aware:
            candidates: list[tuple[float, float]] = []
            steps = max(count * 3, 8)
            for i in range(1, steps + 1):
                p = i / (steps + 1)
                start = clamp_ts(duration * p)
                br = window_bitrate(info.path, start, duration=5.0)
                if br is not None:
                    candidates.append((float(br), start))

            if candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                best_br, best_ts = candidates[0]
                avg_br = sum(b for b, _ in candidates) / len(candidates)
                motion_ts = best_ts
                if avg_br > 0:
                    motion_weight = max(1.25, min(5.0, best_br / avg_br))
                else:
                    motion_weight = 1.5
            else:
                motion_weight = 1.5

        plan.append((motion_ts, motion_weight, "motion"))

    for idx in range(len(plan), count):
        frac = 0.2 + (idx * 0.15)
        plan.append((clamp_ts(duration * frac), 1.0, "auto"))

    return plan[:count]


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
                cmd.extend(["-mapping_family", "1"])
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
        "[0:v]format=yuv420p[enc];[1:v]format=yuv420p[ref];"
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

    show_name = guess_show_name(path)
    override_tier: Optional[str] = None
    if show_name:
        with Session(ENGINE) as session:
            override_tier = get_default_tier_for_show(session, show_name=show_name)

    log_info("analyze_start", files=len(files), show=show_name)

    for f in files:
        info = probe_media(f)
        if info is None:
            log_warn("analyze_probe_failed", file=str(f))
            continue

        classification = classify_source(info, override_tier)

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

    show_name = guess_show_name(path)
    override_tier: Optional[str] = None
    if show_name:
        with Session(ENGINE) as session:
            override_tier = get_default_tier_for_show(session, show_name=show_name)

    # Manual tier override from CLI
    if args.tier:
        override_tier = args.tier

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

        classification = classify_source(info, override_tier)
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


def resolve_target_height_for_path(path: pathlib.Path, settings: AppSettings) -> tuple[Optional[int], str]:
    """Resolve the downscale target height for a given path.

    Prefers per-library max_height when the path matches a configured library,
    falling back to global_max_height. Never upscales (caller enforces).
    """

    # Prefer per-library max_height when the path matches any configured root.
    # We intentionally check both mac and linux roots so paths that originated
    # from another host (e.g. DB entries) still resolve correctly.
    path_str = str(path)
    for lib in settings.libraries:
        roots = [lib.mac_path, lib.linux_path]
        for root in roots:
            if not root:
                continue
            try:
                if path.is_relative_to(pathlib.Path(root)):
                    if lib.max_height is not None:
                        return lib.max_height, f"library:{lib.id}"
                    return settings.global_max_height, "global"
            except Exception:
                if path_str.startswith(root.rstrip("/") + "/") or path_str == root:
                    if lib.max_height is not None:
                        return lib.max_height, f"library:{lib.id}"
                    return settings.global_max_height, "global"

    if settings.global_max_height is not None:
        return settings.global_max_height, "global"

    return None, "none"


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
    return svc_calculate_priority(potential_savings_bytes, mtime, max_savings, max_age)


def scan_file_to_db(
    session: Session,
    file_path: pathlib.Path,
    max_savings: int,
    max_age: int,
    library_id: str | None = None,
) -> Optional[dict]:
    try:
        return svc_scan_file_to_db(
            session,
            file_path,
            max_savings=max_savings,
            max_age=max_age,
            library_id=library_id,
            classify_source=classify_source,
            probe_media=probe_media,
            now_iso=now_iso,
        )
    except Exception as e:
        log_error("scan_file_failed", file=str(file_path), error=str(e))
        return None


def recalculate_priorities(session: Session, max_age: int) -> None:
    svc_recalculate_priorities(session, max_age=max_age, calculate_priority=calculate_priority)


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


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch configured libraries and auto-queue new video files."""

    if awatch is None or Change is None:
        log_error("watch_unavailable", error="watchfiles_not_installed")
        return 1

    if getattr(args, "autoupdate_url", None):
        updated = maybe_autoupdate(args.autoupdate_url, AUTOUPDATE_FILES)
        if updated:
            log_info("autoupdate_restart", component="watch")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # Load settings (remote or local)
    settings_url = getattr(args, "settings_url", None)
    env_settings_url = os.getenv("MEDIAFORCE_REMOTE_SETTINGS_URL")
    effective_settings_url = settings_url or env_settings_url
    settings = load_remote_settings(effective_settings_url) if effective_settings_url else load_app_settings()
    if settings is None:
        log_error("settings_load_failed", url=effective_settings_url)
        return 1

    # Optional periodic autoupdate during long runs
    if getattr(args, "autoupdate_interval", None):
        interval: Optional[int] = max(300, int(args.autoupdate_interval))
    else:
        interval = None

    async def runner():
        if interval is not None:
            async def updater():
                while True:
                    await asyncio.sleep(float(interval))  # type: ignore[arg-type]
                    if maybe_autoupdate(args.autoupdate_url, AUTOUPDATE_FILES):
                        log_info("autoupdate_restart", component="watch")
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            asyncio.create_task(updater())
        await svc_watch_libraries(settings)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log_info("watch_stop")
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
        .order_by(desc(MediaItem.priority_score))
        .limit(limit)
    ).all()

    if not rows:
        log_info("queue_empty", library=str(library_root), limit=limit)
        return 0

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "path": row.path,
                "priority_score": row.priority_score,
                "size_bytes": row.size_bytes,
                "potential_savings_bytes": row.potential_savings_bytes,
                "tier": row.detected_tier,
                "bitrate_kbps": row.bitrate_kbps,
                "library_id": row.library_id,
            }
        )

    # Show summary
    log_info("inventory_summary")
    summary = session.exec(
        select(MediaItem.status, MediaItem.id, MediaItem.size_bytes)
    ).all()
    totals: dict[str, tuple[int, int]] = {}
    for status, mid, size_bytes in summary:
        cnt, total = totals.get(status, (0, 0))
        totals[status] = (cnt + 1, total + (size_bytes or 0))

    totals_payload: dict[str, dict[str, Any]] = {}
    for status, (cnt, total_bytes) in totals.items():
        totals_payload[status] = {
            "count": cnt,
            "total_bytes": total_bytes,
        }

    # Calculate space saved from completed encodes
    encode_rows = session.exec(
        select(EncodeResult.output_size_bytes, MediaItem.size_bytes)
        .join(MediaItem, EncodeResult.source_id == MediaItem.id)
        .where(func.coalesce(EncodeResult.output_size_bytes, 0) > 0)
    ).all()

    space_saved_payload: Optional[dict[str, Any]] = None
    if encode_rows:
        source_bytes = sum(src or 0 for _, src in encode_rows)
        output_bytes = sum(out or 0 for out, _ in encode_rows)
        if source_bytes and output_bytes:
            saved_bytes = source_bytes - output_bytes
            saved_pct = (1 - output_bytes / source_bytes) * 100
            space_saved_payload = {
                "encodes": len(encode_rows),
                "source_bytes": source_bytes,
                "output_bytes": output_bytes,
                "saved_bytes": saved_bytes,
                "saved_pct": saved_pct,
            }

    log_info(
        "queue_listing",
        library=str(library_root),
        limit=limit,
        count=len(items),
        items=items,
        totals=totals_payload,
        space_saved=space_saved_payload,
    )

    session.close()
    return 0


def check_missing_outputs(session: Session) -> int:
    missing_count, missing_files = svc_check_missing_outputs(session, now_iso=now_iso)
    for entry in missing_files:
        log_warn("missing_output_reset", source=entry.get("source"), output=entry.get("output"))
    return missing_count


def claim_next_file(session: Session, machine: str) -> Optional[dict]:
    return svc_claim_next_file(session, machine, stale_seconds=STALE_CLAIM_SECONDS, now_iso=now_iso)


def release_claim(session: Session, file_id: int, success: bool) -> None:
    svc_release_claim(session, file_id, success, now_iso=now_iso)


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
    return svc_start_progress_tracking(
        session,
        source_id=source_id,
        source_path=source_path,
        output_path=output_path,
        machine=machine,
        tier=tier,
        duration_sec=duration_sec,
        total_frames=total_frames,
    )


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
    svc_update_progress(
        session,
        progress_id,
        frame=frame,
        fps=fps,
        speed=speed,
        bitrate_kbps=bitrate_kbps,
        size_bytes=size_bytes,
        time_encoded_sec=time_encoded_sec,
        duration_sec=duration_sec,
        phase=phase,
        phase_detail=phase_detail,
    )


def finish_progress_tracking(
    session: Session,
    progress_id: int,
    success: bool,
    error_msg: Optional[str] = None,
) -> None:
    svc_finish_progress_tracking(
        session,
        progress_id,
        success=success,
        error_msg=error_msg,
    )


def parse_ffmpeg_progress(line: str) -> dict:
    return svc_parse_ffmpeg_progress(line)


def run_ffmpeg_with_progress(
    cmd: list[str],
    session: Session,
    progress_id: int,
    duration_sec: float,
) -> subprocess.CompletedProcess:
    return svc_run_ffmpeg_with_progress(
        cmd,
        session,
        progress_id,
        duration_sec,
        update_progress=update_progress,
    )


def run_ffmpeg_with_progress_api(
    cmd: list[str],
    api_client: WorkerApiClient,
    machine: str,
    progress_id: int,
    duration_sec: float,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with progress updates sent to the Mediaforce API."""

    cmd_with_progress = cmd.copy()

    try:
        idx = cmd_with_progress.index("-hide_banner") + 1
    except ValueError:
        idx = 1
    cmd_with_progress.insert(idx, "-progress")
    cmd_with_progress.insert(idx + 1, "pipe:1")
    cmd_with_progress.insert(idx, "-nostats")

    process = subprocess.Popen(
        cmd_with_progress,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    accumulated: dict[str, Any] = {}
    last_update = time.time()
    last_control_check = time.time()
    assert process.stdout is not None

    while True:
        line = ""
        try:
            import select

            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if ready:
                line = process.stdout.readline()
        except Exception:
            line = process.stdout.readline()

        if not line and process.poll() is not None:
            break

        if line:
            parsed = parse_ffmpeg_progress(line)
            accumulated.update(parsed)

        now = time.time()

        if now - last_control_check >= 5:
            try:
                control = api_client.control(machine=machine)
                if bool(control.stop_now):
                    try:
                        api_client.ack(machine=machine, action="stop_now")
                    except WorkerApiError:
                        pass

                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass

                    return subprocess.CompletedProcess(
                        args=cmd_with_progress,
                        returncode=255,
                        stdout="",
                        stderr="stopped_now",
                    )
            except WorkerApiError:
                pass
            last_control_check = now

        if now - last_update >= 2 and accumulated:
            try:
                api_client.progress_update(
                    progress_id=progress_id,
                    frame=int(accumulated.get("frame", 0) or 0),
                    fps=float(accumulated.get("fps", 0) or 0.0),
                    speed=float(accumulated.get("speed", 0) or 0.0),
                    bitrate_kbps=accumulated.get("bitrate_kbps"),
                    size_bytes=int(accumulated.get("size_bytes", 0) or 0),
                    time_encoded_sec=float(accumulated.get("time_encoded_sec", 0) or 0.0),
                    duration_sec=duration_sec,
                )
            except WorkerApiError:
                pass
            last_update = now

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
    profile_eval_id: Optional[int] = None,
) -> int:
    return svc_record_encode_result(
        session,
        source_id=source_id,
        source_path=source_path,
        tier=tier,
        settings=settings,
        output_path=output_path,
        output_size=output_size,
        output_bitrate=output_bitrate,
        source_size=source_size,
        machine=machine,
        started_at=started_at,
        error_msg=error_msg,
        metrics=metrics,
        outlier_result=outlier_result,
        profile_eval_id=profile_eval_id,
    )


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


def _resolve_machine_name() -> str:
    override = (os.getenv("MEDIAFORCE_MACHINE_NAME") or "").strip()
    if override:
        return override

    hostname = socket.gethostname().strip()
    if hostname.lower().endswith(".local"):
        return hostname.rsplit(".", 1)[0]
    return hostname


def cmd_run(args: argparse.Namespace) -> int:
    """Run queue-based encoding from inventory database."""
    raw_path = pathlib.Path(args.path)
    try:
        path = raw_path.resolve()
    except Exception:
        path = raw_path

    library_root = get_library_root(path)

    api_url = getattr(args, "api_url", None) or os.getenv("MEDIAFORCE_API_URL")
    use_api = bool(api_url)
    api_client: Optional[WorkerApiClient] = None
    db_path: Optional[pathlib.Path] = None

    if use_api:
        assert api_url is not None
        api_client = WorkerApiClient(api_url)
        log_info("worker_api_enabled", url=api_url)
    else:
        try:
            path.stat()
        except PermissionError:
            pass
        except FileNotFoundError:
            log_error("run_path_missing", path=str(path))
            return 1

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

    machine = _resolve_machine_name()
    log_info("run_start", machine=machine, library=str(library_root), output=str(transcode_root))

    # Autoupdate on startup
    if getattr(args, "autoupdate_url", None):
        if maybe_autoupdate(args.autoupdate_url, AUTOUPDATE_FILES):
            log_info("autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # Load settings (prefer remote settings-url if provided)
    settings_url = getattr(args, "settings_url", None)
    if use_api and not settings_url and api_url:
        settings_url = f"{api_url.rstrip('/')}/api/settings/current"
    app_settings = None
    if settings_url:
        app_settings = load_remote_settings(settings_url)
        if app_settings is None:
            log_warn("remote_settings_load_failed", url=settings_url)
    if app_settings is None:
        app_settings = load_app_settings()

    profile_settings_url = (
        getattr(args, "profile_settings_url", None)
        or os.getenv("MEDIAFORCE_PROFILE_SETTINGS_URL")
    )
    if profile_settings_url:
        global REMOTE_SETTINGS_URL
        REMOTE_SETTINGS_URL = profile_settings_url
        log_info("profile_settings_url_set", url=profile_settings_url)

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
        if maybe_autoupdate(args.autoupdate_url, AUTOUPDATE_FILES):
            log_info("autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    session: Optional[Session] = None
    if not use_api:
        assert db_path is not None
        session = init_db(db_path)

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
        library_available = True
        if use_api:
            try:
                path.stat()
            except PermissionError:
                library_available = True
            except FileNotFoundError:
                library_available = False

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
        claimed: Optional[dict] = None
        override_tier: Optional[str] = None
        if use_api and api_client is not None:
            try:
                claim_result = api_client.claim(
                    machine=machine,
                    available=library_available,
                    sample_path=(str(path) if not library_available else None),
                )
            except WorkerApiError as e:
                log_error("worker_api_claim_failed", error=str(e))
                break

            if claim_result is None:
                log_info("queue_empty")
                if args.dry_run:
                    break
                time.sleep(10)
                continue

            claim_obj = claim_result.claim
            if claim_obj is None:
                if not library_available:
                    log_warn("run_library_unavailable", path=str(path))
                    time.sleep(30)
                    continue

                if claim_result.control_mode == "stop":
                    log_info("worker_stopped", machine=machine)
                else:
                    event = "worker_paused" if claim_result.control_mode == "drain" else "queue_empty"
                    log_info(event)
                if args.dry_run:
                    break
                time.sleep(10)
                continue

            claimed = {"id": claim_obj.id, "path": claim_obj.path}
            override_tier = claim_obj.override_tier
        else:
            assert session is not None
            claimed = claim_next_file(session, machine)
            if claimed is None:
                log_info("queue_empty")
                break
            show_name = guess_show_name(normalize_path(pathlib.Path(claimed["path"])))
            override_tier = get_default_tier_for_show(session, show_name=show_name) if show_name else None

        source_path = normalize_path(pathlib.Path(claimed["path"]))
        log_info("encode_start", index=encoded_count + 1, file=str(source_path))

        # Probe file with interlacing detection
        log_info("detect_interlace", file=str(source_path))
        info = probe_media_with_interlace_detection(source_path)
        if info is None:
            log_error("probe_failed", file=str(source_path))
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                except WorkerApiError as e:
                    log_warn("worker_api_release_failed", error=str(e))
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=False)
            error_count += 1
            continue

        # override_tier already resolved (DB or API claim)

        # Classify and get settings
        classification = classify_source(info, override_tier)
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
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=True)
                except WorkerApiError as e:
                    log_warn("worker_api_release_failed", error=str(e))
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=True)
            continue

        # Build and run ffmpeg command
        # Downscale target from settings (never upscale)
        target_height, target_height_reason = resolve_target_height_for_path(source_path, app_settings)

        eval_obj_id: Optional[int] = None
        active_settings_source = ensure_active_profile_settings(session) if session is not None else None

        # Optional VMAF sampling to adjust tier before full encode
        if args.sample_vmaf:
            from mediaforce.services.quality_loop import build_motion_weighted_plan, run_profile_quality_loop

            if session is not None:
                info_for_samples = info
                assert info_for_samples is not None

                def measure(item) -> Optional[float]:
                    enc_path, enc_size = encode_sample_clip(
                        source_path,
                        settings,
                        info_for_samples,
                        item.start_sec,
                        item.duration_sec,
                        target_height,
                    )
                    if not enc_path:
                        return None
                    vmaf = compute_vmaf_score(
                        source_path,
                        enc_path,
                        item.start_sec,
                        item.duration_sec,
                        encoded_size=enc_size,
                    )
                    try:
                        enc_path.unlink(missing_ok=True)
                        enc_path.parent.rmdir()
                    except OSError:
                        pass
                    return vmaf

                loop_result = run_profile_quality_loop(
                    session,
                    media_id=claimed["id"],
                    source_path=source_path,
                    duration_seconds=float(info.duration_seconds or 0.0),
                    initial_profile=tier,
                    settings_source=active_settings_source,
                    sample_length=args.sample_length,
                    motion_aware=args.sample_motion_aware,
                    measure_vmaf=measure,
                    window_bitrate=window_bitrate,
                    target_height=target_height,
                    target_height_reason=target_height_reason,
                )
                eval_obj_id = loop_result.evaluation_id
            elif use_api and api_client is not None:
                duration_seconds = float(info.duration_seconds or 0.0)
                if duration_seconds > 0.0 and args.sample_length > 0:
                    try:
                        eval_id, _thresholds = api_client.evaluation_start(
                            media_id=int(claimed["id"]),
                            initial_profile=tier,
                            sample_length=float(args.sample_length),
                        )
                        eval_obj_id = eval_id

                        plan = build_motion_weighted_plan(
                            source_path=source_path,
                            duration_seconds=duration_seconds,
                            sample_length=float(args.sample_length),
                            motion_aware=bool(args.sample_motion_aware),
                            window_bitrate=window_bitrate,
                        )

                        samples_payload: list[dict[str, Any]] = []
                        for item in plan:
                            enc_path, enc_size = encode_sample_clip(
                                source_path,
                                settings,
                                info,
                                item.start_sec,
                                item.duration_sec,
                                target_height,
                            )
                            if not enc_path:
                                continue
                            vmaf = compute_vmaf_score(
                                source_path,
                                enc_path,
                                item.start_sec,
                                item.duration_sec,
                                encoded_size=enc_size,
                            )
                            try:
                                enc_path.unlink(missing_ok=True)
                                enc_path.parent.rmdir()
                            except OSError:
                                pass
                            if vmaf is None:
                                continue
                            samples_payload.append(
                                {
                                    "kind": item.kind,
                                    "start_sec": item.start_sec,
                                    "duration_sec": item.duration_sec,
                                    "weight": item.weight,
                                    "vmaf": float(vmaf),
                                }
                            )

                        resp = api_client.evaluation_submit_samples(
                            evaluation_id=eval_id,
                            samples=samples_payload,
                            target_height=target_height,
                            target_height_reason=target_height_reason,
                        )
                        loop_result = SimpleNamespace(
                            evaluation_id=eval_id,
                            initial_profile=resp.get("initial_profile") or tier,
                            selected_profile=resp.get("selected_profile") or tier,
                            decision=resp.get("decision") or "keep",
                            summary=SimpleNamespace(
                                weighted=(resp.get("summary") or {}).get("weighted"),
                                minimum=(resp.get("summary") or {}).get("min"),
                                median=(resp.get("summary") or {}).get("median"),
                            ),
                            thresholds=SimpleNamespace(
                                min_vmaf=(_thresholds or {}).get("min"),
                                median_vmaf=(_thresholds or {}).get("median"),
                            ),
                        )
                    except WorkerApiError as e:
                        log_warn("quality_loop_api_failed", error=str(e))
                        loop_result = None
                else:
                    loop_result = None
            else:
                loop_result = None

            if loop_result is None:
                pass

            if loop_result is not None and loop_result.selected_profile != tier:
                try:
                    new_tier = SourceTier(loop_result.selected_profile)
                    classification = ClassificationResult(
                        tier=new_tier,
                        confidence=classification.confidence,
                        reasons=classification.reasons
                        + [
                            f"quality_loop:{tier}->{loop_result.selected_profile}"
                            f" (weighted={loop_result.summary.weighted})"
                        ],
                        recommended_settings=TIER_SETTINGS[new_tier],
                    )
                    settings = classification.recommended_settings
                    tier = classification.tier.value
                except ValueError:
                    pass

            if loop_result is not None:
                log_info(
                    "quality_loop_result",
                    file=str(source_path),
                    eval_id=loop_result.evaluation_id,
                    initial=loop_result.initial_profile,
                    selected=loop_result.selected_profile,
                    decision=loop_result.decision,
                    weighted=loop_result.summary.weighted,
                    minimum=loop_result.summary.minimum,
                    median=loop_result.summary.median,
                    threshold_min=loop_result.thresholds.min_vmaf,
                    threshold_median=loop_result.thresholds.median_vmaf,
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
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                except WorkerApiError as e:
                    log_warn("worker_api_release_failed", error=str(e))
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=False)
            encoded_count += 1
            continue

        log_info("encode_launch", output=output_path.name)
        active_slots += 1

        total_frames: Optional[int] = None
        if info.video_framerate and info.duration_seconds and info.video_framerate > 0 and info.duration_seconds > 0:
            try:
                total_frames = int(info.video_framerate * info.duration_seconds)
            except Exception:
                total_frames = None

        # Start progress tracking
        duration_sec = float(info.duration_seconds or 0.0)
        if use_api and api_client is not None:
            progress_id = api_client.progress_start(
                source_id=int(claimed["id"]),
                source_path=str(source_path),
                output_path=str(output_path),
                machine=machine,
                tier=tier,
                duration_sec=duration_sec,
                total_frames=total_frames,
            )
        else:
            assert session is not None
            progress_id = start_progress_tracking(
                session,
                claimed["id"],
                str(source_path),
                str(output_path),
                machine,
                tier,
                duration_sec,
                total_frames=total_frames,
            )

        try:
            # Run ffmpeg with progress tracking
            if use_api and api_client is not None:
                result = run_ffmpeg_with_progress_api(cmd, api_client, machine, progress_id, duration_sec)
            else:
                assert session is not None
                result = run_ffmpeg_with_progress(cmd, session, progress_id, duration_sec)

            if result.returncode != 0 and args.hw_decode:
                stderr = result.stderr or ""
                hwaccel_failed = any(
                    token in stderr
                    for token in (
                        "cuInit(0) failed",
                        "CUDA_ERROR",
                        "No device available for decoder",
                        "Device setup failed",
                    )
                )
                used_cuda = any(part == "cuda" for part in (result.args or []))
                if hwaccel_failed and used_cuda:
                    log_warn("hw_decode_failed_fallback", machine=machine)
                    cmd = build_ffmpeg_command(
                        source_path,
                        output_path,
                        settings,
                        info,
                        max_height=target_height,
                        hw_decode=False,
                        hw_encode=args.hw_encode,
                    )
                    if use_api and api_client is not None:
                        result = run_ffmpeg_with_progress_api(cmd, api_client, machine, progress_id, duration_sec)
                    else:
                        assert session is not None
                        result = run_ffmpeg_with_progress(cmd, session, progress_id, duration_sec)

            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr
                )

            # Get output stats
            try:
                output_size = output_path.stat().st_size
            except FileNotFoundError:
                output_size = 0
            try:
                source_size = source_path.stat().st_size
            except FileNotFoundError:
                source_size = 0
            ratio = (output_size / source_size * 100) if source_size > 0 else 0

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
                if use_api and api_client is not None:
                    try:
                        api_client.progress_update(
                            progress_id=progress_id,
                            duration_sec=duration_sec,
                            phase="verifying",
                            phase_detail="Running quality checks",
                        )
                    except WorkerApiError as e:
                        log_warn("worker_api_progress_failed", error=str(e))
                else:
                    assert session is not None
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

            if use_api and api_client is not None:
                payload: dict[str, Any] = {
                    "source_id": int(claimed["id"]),
                    "source_path": str(source_path),
                    "tier": tier,
                    "crf": settings.crf,
                    "preset": settings.preset,
                    "film_grain": settings.film_grain,
                    "denoise": settings.denoise,
                    "output_path": str(output_path),
                    "output_size_bytes": int(output_size),
                    "output_bitrate_kbps": output_bitrate,
                    "source_size_bytes": int(source_size),
                    "machine": machine,
                    "started_at": started_at,
                    "success": True,
                    "profile_eval_id": eval_obj_id,
                    "progress_id": progress_id,
                }
                if metrics is not None:
                    payload["metrics"] = {
                        "ssim": metrics.ssim,
                        "psnr": metrics.psnr,
                        "vmaf": metrics.vmaf,
                        "sample_duration_sec": metrics.sample_duration_sec,
                        "sample_start_sec": metrics.sample_start_sec,
                    }
                if outlier_result is not None:
                    payload["outlier"] = {
                        "is_outlier": outlier_result.is_outlier,
                        "reasons": outlier_result.reasons,
                    }
                try:
                    api_client.report_encode_result(payload=payload)
                except WorkerApiError as e:
                    log_error("worker_api_report_failed", error=str(e))
                    try:
                        api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                    except WorkerApiError:
                        pass
            else:
                assert session is not None
                # Clean up progress tracking
                finish_progress_tracking(session, progress_id, success=True)

                # Record result
                result_id = record_encode_result(
                    session, claimed["id"], str(source_path), tier, settings,
                    str(output_path), output_size, output_bitrate, source_size,
                    machine, started_at,
                    metrics=metrics,
                    outlier_result=outlier_result,
                    profile_eval_id=eval_obj_id,
                )
                if eval_obj_id:
                    eval_obj = session.get(ProfileEvaluation, eval_obj_id)
                    if eval_obj:
                        eval_obj.encode_result_id = result_id
                        eval_obj.updated_at = datetime.now().isoformat()
                        session.add(eval_obj)
                        session.commit()
                release_claim(session, claimed["id"], success=True)

                size_increase = output_size > source_size if source_size > 0 else False
                saved_bytes = max(0, source_size - output_size) if source_size > 0 else 0
                reduction_pct = (1 - (output_size / source_size)) * 100 if source_size > 0 else None
                event = "encode_size_increase" if size_increase else "encode_completed"
                summary = (
                    f"{event}: {source_path}"
                    + (f" ({saved_bytes} bytes saved)" if saved_bytes else "")
                    + (" (size increased)" if size_increase else "")
                )
                try:
                    send_notifications(
                        event=event,
                        summary=summary,
                        data={
                            "encode_result_id": result_id,
                            "success": True,
                            "source_id": int(claimed["id"]),
                            "source_path": str(source_path),
                            "output_path": str(output_path),
                            "tier": tier,
                            "machine": machine,
                            "source_size_bytes": int(source_size),
                            "output_size_bytes": int(output_size),
                            "saved_bytes": int(saved_bytes),
                            "reduction_pct": reduction_pct,
                            "vmaf": metrics.vmaf if metrics else None,
                            "outlier": bool(outlier_result.is_outlier) if outlier_result else None,
                            "outlier_reasons": list(outlier_result.reasons) if outlier_result else [],
                        },
                        logger=CLI_LOGGER,
                    )
                except Exception:
                    pass
            encoded_count += 1

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else str(e))
            log_error("encode_failed", error=error_msg[:200])

            if use_api and api_client is not None:
                payload = {
                    "source_id": int(claimed["id"]),
                    "source_path": str(source_path),
                    "tier": tier,
                    "crf": settings.crf,
                    "preset": settings.preset,
                    "film_grain": settings.film_grain,
                    "denoise": settings.denoise,
                    "output_path": str(output_path),
                    "output_size_bytes": 0,
                    "output_bitrate_kbps": None,
                    "source_size_bytes": int(source_path.stat().st_size),
                    "machine": machine,
                    "started_at": started_at,
                    "success": False,
                    "error_message": error_msg[:500],
                    "profile_eval_id": eval_obj_id,
                    "progress_id": progress_id,
                }
                try:
                    api_client.report_encode_result(payload=payload)
                except WorkerApiError as ex:
                    log_error("worker_api_report_failed", error=str(ex))
                    try:
                        api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                    except WorkerApiError:
                        pass
            else:
                assert session is not None

                # Clean up progress tracking
                finish_progress_tracking(session, progress_id, success=False, error_msg=error_msg[:500])

                result_id = record_encode_result(
                    session, claimed["id"], str(source_path), tier, settings,
                    str(output_path), 0, None, source_path.stat().st_size,
                    machine, started_at, error_msg,
                    profile_eval_id=eval_obj_id,
                )
                if eval_obj_id:
                    eval_obj = session.get(ProfileEvaluation, eval_obj_id)
                    if eval_obj:
                        eval_obj.encode_result_id = result_id
                        eval_obj.status = "failed"
                        eval_obj.updated_at = datetime.now().isoformat()
                        session.add(eval_obj)
                        session.commit()
                release_claim(session, claimed["id"], success=False)

                try:
                    send_notifications(
                        event="encode_failed",
                        summary=f"encode_failed: {source_path}",
                        data={
                            "encode_result_id": result_id,
                            "success": False,
                            "source_id": int(claimed["id"]),
                            "source_path": str(source_path),
                            "output_path": str(output_path),
                            "tier": tier,
                            "machine": machine,
                            "error_message": error_msg[:500],
                        },
                        logger=CLI_LOGGER,
                    )
                except Exception:
                    pass
            error_count += 1

            # Clean up partial output
            if output_path.exists():
                output_path.unlink()

            # Avoid hammering the same failing job in a tight loop.
            time.sleep(5)
        finally:
            active_slots = max(active_slots - 1, 0)

    if session is not None:
        session.close()

    log_info(
        "run_complete",
        encoded=encoded_count,
        errors=error_count,
        outliers=outlier_count,
    )

    return 0 if error_count == 0 else 1




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
    path = normalize_path(pathlib.Path(args.path).resolve())
    transcode_root = normalize_path(pathlib.Path(args.transcode_root).resolve())

    if not path.exists():
        log_error("promote_path_missing", path=str(path))
        return 1

    if not transcode_root.exists():
        log_error("promote_transcode_root_missing", transcode_root=str(transcode_root))
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if path.is_file():
        files = [path]
    else:
        files = []
        for ext in VIDEO_EXTENSIONS:
            files.extend(path.rglob(f"*{ext}"))
        files = sorted(files)

    files = [f for f in files if f.is_file() and not f.name.startswith(".")]
    if not files:
        log_info("promote_no_files", path=str(path))
        return 0

    log_info(
        "promote_scan",
        files=len(files),
        transcode_root=str(transcode_root),
        dry_run=args.dry_run,
    )

    promoted = 0
    skipped = 0
    errors = 0

    session = None
    if db_path.exists():
        session = init_db(db_path)

    for f in files:
        if ".AV1." in f.name or f.suffix.lower() == ".av1":
            continue

        encoded = get_transcode_output_path(f, transcode_root)
        if encoded is None:
            skipped += 1
            continue

        dest_path = f.parent / encoded.name
        log_info(
            "promote_candidate",
            source=str(f),
            encoded=str(encoded),
            dest=str(dest_path),
        )

        rollback_state = None
        try:
            result, rollback_state = promote_encoded_file_atomic(
                source_path=f,
                encoded_path=encoded,
                dest_path=dest_path,
                dry_run=args.dry_run,
                move_original_to_backup=args.delete_original,
                rename_sidecars=True,
                verify=True,
                logger=CLI_LOGGER,
            )

            if args.dry_run:
                promoted += 1
                continue

            if session:
                try:
                    now_str = now_iso()
                    item = session.exec(select(MediaItem).where(MediaItem.path == str(f))).first()
                    if item:
                        item.status = "completed"
                        item.path = str(result.dest_path)
                        item.updated_at = now_str
                        session.add(item)

                    enc = session.exec(select(EncodeResult).where(EncodeResult.source_path == str(f))).first()
                    if enc:
                        enc.promoted = True
                        enc.promoted_at = now_str
                        enc.promoted_path = str(result.dest_path)
                        enc.source_backup_path = (
                            str(result.backup_source_path) if result.backup_source_path else None
                        )
                        enc.promote_manifest_json = result.manifest.to_json()
                        enc.output_path = str(result.dest_path)
                        session.add(enc)

                    session.commit()
                except Exception:
                    session.rollback()
                    if rollback_state:
                        rollback_promote(rollback_state)
                    raise

            promoted += 1
        except Exception as e:
            log_error("promote_item_failed", source=str(f), error=str(e))
            errors += 1

    if session:
        session.close()

    log_info("promote_summary", promoted=promoted, skipped=skipped, errors=errors)
    return 0 if errors == 0 else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify quality of encoded files by comparing to source."""
    source_path = pathlib.Path(args.source).resolve()
    encoded_path = pathlib.Path(args.encoded).resolve()

    if not source_path.exists():
        log_error("verify_source_missing", source=str(source_path))
        return 1

    if not encoded_path.exists():
        log_error("verify_encoded_missing", encoded=str(encoded_path))
        return 1

    # Get file sizes
    source_size = source_path.stat().st_size
    encoded_size = encoded_path.stat().st_size
    ratio = encoded_size / source_size * 100

    log_info(
        "verify_start",
        source=str(source_path),
        encoded=str(encoded_path),
        source_size_bytes=source_size,
        encoded_size_bytes=encoded_size,
        ratio_pct=ratio,
        sample_duration_sec=float(args.sample_duration),
        use_vmaf=not bool(args.no_vmaf),
    )

    metrics = verify_encode_quality(
        source_path,
        encoded_path,
        sample_duration_sec=args.sample_duration,
        use_vmaf=not args.no_vmaf,
    )

    log_info(
        "verify_result",
        source=str(source_path),
        encoded=str(encoded_path),
        grade=metrics.quality_grade,
        acceptable=bool(metrics.is_acceptable),
        ssim=metrics.ssim,
        psnr=metrics.psnr,
        vmaf=metrics.vmaf,
        sample_start_sec=metrics.sample_start_sec,
        sample_duration_sec=metrics.sample_duration_sec,
    )

    return 0


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cmd_purge_backups(args: argparse.Namespace) -> int:
    """Purge promotion backup files for older, successfully-promoted items."""

    older_than_days = int(args.older_than_days)
    if older_than_days < 0:
        log_error("purge_backups_invalid_threshold", older_than_days=older_than_days)
        return 1

    limit = int(args.limit)
    if limit < 0:
        log_error("purge_backups_invalid_limit", limit=limit)
        return 1

    apply = bool(args.apply)
    dry_run = not apply

    cutoff_dt = datetime.now() - timedelta(days=older_than_days)
    cutoff_iso = cutoff_dt.isoformat()

    db_path = get_db_path()
    if not db_path.exists():
        log_info("purge_backups_no_db", db=str(db_path))
        return 0

    session = init_db(db_path)
    try:
        stmt = (
            select(EncodeResult)
            .where(
                EncodeResult.promoted == True,  # noqa: E712
                EncodeResult.promoted_at.is_not(None),  # type: ignore[union-attr]
                EncodeResult.promoted_at < cutoff_iso,  # type: ignore[operator]
                EncodeResult.source_backup_path.is_not(None),  # type: ignore[union-attr]
                EncodeResult.promoted_path.is_not(None),  # type: ignore[union-attr]
            )
            .order_by(EncodeResult.promoted_at)
        )
        candidates = session.exec(stmt).all()
    finally:
        session.close()

    considered = 0
    eligible = 0
    deleted = 0
    skipped = 0
    errors = 0
    freed_bytes = 0

    for enc in candidates:
        considered += 1
        if limit and deleted >= limit:
            break

        promoted_at_raw = enc.promoted_at
        promoted_at = _parse_iso_datetime(promoted_at_raw) if promoted_at_raw else None
        if promoted_at is None:
            skipped += 1
            continue

        cutoff_for_compare = (
            cutoff_dt
            if promoted_at.tzinfo is None
            else datetime.now(promoted_at.tzinfo) - timedelta(days=older_than_days)
        )
        if promoted_at > cutoff_for_compare:
            skipped += 1
            continue

        if not enc.promoted_path or not enc.source_backup_path:
            skipped += 1
            continue

        promoted_path = normalize_path(pathlib.Path(enc.promoted_path))
        backup_path = normalize_path(pathlib.Path(enc.source_backup_path))

        if not promoted_path.exists():
            log_warn(
                "purge_backups_skip_promoted_missing",
                encode_id=enc.id,
                promoted=str(promoted_path),
                backup=str(backup_path),
            )
            skipped += 1
            continue

        if not backup_path.exists():
            skipped += 1
            continue

        # Extra safety: backup file must match our known naming scheme.
        source_name = pathlib.Path(enc.source_path).name
        expected_prefix = f".{source_name}.mediaforce-orig-"
        if not backup_path.name.startswith(expected_prefix):
            log_warn(
                "purge_backups_skip_unexpected_name",
                encode_id=enc.id,
                backup=str(backup_path),
                expected_prefix=expected_prefix,
            )
            skipped += 1
            continue

        if backup_path.parent != promoted_path.parent:
            log_warn(
                "purge_backups_skip_suspicious_paths",
                encode_id=enc.id,
                promoted=str(promoted_path),
                backup=str(backup_path),
            )
            skipped += 1
            continue

        eligible += 1

        try:
            size = backup_path.stat().st_size
        except OSError:
            size = 0

        if dry_run:
            log_info(
                "purge_backups_dry_run",
                encode_id=enc.id,
                backup=str(backup_path),
                promoted=str(promoted_path),
                bytes=size,
                promoted_at=promoted_at_raw,
            )
            continue

        try:
            backup_path.unlink()
            deleted += 1
            freed_bytes += size
            log_info(
                "purge_backups_deleted",
                encode_id=enc.id,
                backup=str(backup_path),
                bytes=size,
                promoted_at=promoted_at_raw,
            )
        except OSError as e:
            errors += 1
            log_error(
                "purge_backups_delete_failed",
                encode_id=enc.id,
                backup=str(backup_path),
                error=str(e),
            )

    log_info(
        "purge_backups_summary",
        older_than_days=older_than_days,
        dry_run=dry_run,
        limit=limit if limit else None,
        candidates=len(candidates),
        considered=considered,
        eligible=eligible,
        deleted=deleted,
        skipped=skipped,
        errors=errors,
        freed_bytes=freed_bytes,
    )
    return 0 if errors == 0 else 1


def cmd_import_show_config(args: argparse.Namespace) -> int:
    """Import legacy show overrides from `show_config.json` into the DB."""

    apply = bool(getattr(args, "apply", False))
    dry_run = not apply
    overwrite_existing = bool(getattr(args, "overwrite_existing", False))

    config_path: Optional[pathlib.Path] = None
    if getattr(args, "path", None):
        config_path = pathlib.Path(args.path).expanduser()
    else:
        candidates = [
            pathlib.Path("show_config.json"),
            CONFIG_DIR / "show_config.json",
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break

    if not config_path or not config_path.exists():
        log_error("show_config_missing", searched=str(config_path) if config_path else None)
        return 1

    try:
        with Session(ENGINE) as session:
            result = import_show_config_json(
                session,
                config_path=config_path,
                dry_run=dry_run,
                overwrite_existing=overwrite_existing,
            )
    except Exception as e:
        log_error("show_config_import_failed", config=str(config_path), error=str(e))
        return 1

    log_info(
        "show_config_import_summary",
        config=str(config_path),
        dry_run=dry_run,
        overwrite_existing=overwrite_existing,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        total_actions=len(result.actions),
    )
    return 0


def cmd_verify_batch(args: argparse.Namespace) -> int:
    """Verify quality of all pending encodes in transcode folder."""
    path = pathlib.Path(args.path).resolve()
    transcode_root = pathlib.Path(args.transcode_root).resolve()

    if not path.exists():
        log_error("verify_batch_path_missing", path=str(path))
        return 1

    if not transcode_root.exists():
        log_error("verify_batch_transcode_root_missing", transcode_root=str(transcode_root))
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
        log_info("verify_batch_no_files", path=str(path))
        return 0

    log_info(
        "verify_batch_start",
        path=str(path),
        transcode_root=str(transcode_root),
        total_files=len(files),
        sample_duration_sec=float(args.sample_duration),
        use_vmaf=not bool(args.no_vmaf),
    )

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

        metrics = verify_encode_quality(
            f,
            encoded,
            sample_duration_sec=args.sample_duration,
            sample_positions=[0.5],  # Single sample for batch
            use_vmaf=not args.no_vmaf,
        )

        results.append((f.name, metrics))

        log_info(
            "verify_batch_result",
            source=str(f),
            encoded=str(encoded),
            grade=metrics.quality_grade,
            acceptable=bool(metrics.is_acceptable),
            ssim=metrics.ssim,
            psnr=metrics.psnr,
            vmaf=metrics.vmaf,
        )

        if metrics.is_acceptable:
            verified += 1
        else:
            failed += 1

    log_info(
        "verify_batch_summary",
        path=str(path),
        transcode_root=str(transcode_root),
        verified=verified,
        failed=failed,
        skipped=skipped,
        total_results=len(results),
    )

    return 0 if failed == 0 else 1


def cmd_review_list(args: argparse.Namespace) -> int:
    """List encodes that need review (outliers and pending)."""
    path = pathlib.Path(args.path).resolve()

    if not path.exists():
        log_error("review_list_path_missing", path=str(path))
        return 1

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_error("review_list_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)

    base_query = (
        select(
            EncodeResult.id,
            EncodeResult.source_path,
            EncodeResult.output_path,
            EncodeResult.tier,
            EncodeResult.output_size_bytes,
            MediaItem.size_bytes,
            EncodeResult.psnr,
            EncodeResult.ssim,
            EncodeResult.vmaf,
            EncodeResult.is_outlier,
            EncodeResult.outlier_reasons,
            EncodeResult.review_status,
            EncodeResult.completed_at,
        )
        .select_from(EncodeResult)
        .join(MediaItem, EncodeResult.source_id == MediaItem.id, isouter=True)
    )

    if args.all:
        stmt = base_query.where(
            (EncodeResult.psnr.is_not(None))  # type: ignore[attr-defined]
            | (EncodeResult.ssim.is_not(None))  # type: ignore[attr-defined]
            | (EncodeResult.vmaf.is_not(None))  # type: ignore[attr-defined]
        ).order_by(desc(EncodeResult.is_outlier), func.coalesce(EncodeResult.vmaf, 0).asc())
    else:
        stmt = base_query.where(
            EncodeResult.is_outlier == True,  # noqa: E712
            EncodeResult.review_status == "pending",
        ).order_by(func.coalesce(EncodeResult.vmaf, 0).asc())

    results = session.exec(stmt).all()

    if not results:
        log_info(
            "review_list_empty",
            library=str(library_root),
            all=bool(args.all),
        )
        return 0

    items: list[dict[str, Any]] = []
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

        # Status indicator
        if review_status == "approved":
            status = "OK"
        elif review_status == "rejected":
            status = "REJ"
        elif is_outlier:
            status = "OUTLIER"
        else:
            status = "pending"

        payload: dict[str, Any] = {
            "id": int(result_id),
            "source_path": str(source_path),
            "output_path": row[2],
            "tier": tier,
            "review_status": review_status,
            "is_outlier": bool(is_outlier),
            "ratio_pct": ratio,
            "vmaf": vmaf,
            "ssim": ssim,
            "psnr": psnr,
            "grade": status,
        }
        if is_outlier and outlier_reasons and args.verbose:
            payload["outlier_reasons"] = outlier_reasons
        items.append(payload)

    log_info(
        "review_list",
        library=str(library_root),
        all=bool(args.all),
        count=len(items),
        items=items,
    )

    return 0


def cmd_review_approve(args: argparse.Namespace) -> int:
    """Approve an encode (mark as reviewed and OK to promote)."""
    path = pathlib.Path(args.path).resolve()

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_error("review_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)

    enc = session.get(EncodeResult, args.id)
    if not enc:
        log_error("review_encode_not_found", id=int(args.id))
        session.close()
        return 1

    enc.review_status = "approved"
    enc.reviewed_at = now_iso()
    session.add(enc)
    session.commit()

    log_info(
        "review_approved",
        id=int(args.id),
        source=str(enc.source_path),
        output=str(enc.output_path) if enc.output_path else None,
    )

    session.close()

    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    """Reject an encode (mark for re-encoding or deletion)."""
    path = pathlib.Path(args.path).resolve()

    library_root = get_library_root(path)
    db_path = get_db_path(library_root)

    if not db_path.exists():
        log_error("review_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)

    enc = session.get(EncodeResult, args.id)
    if not enc:
        log_error("review_encode_not_found", id=int(args.id))
        session.close()
        return 1

    source_path = pathlib.Path(enc.source_path)
    output_path = pathlib.Path(enc.output_path) if enc.output_path else None

    enc.review_status = "rejected"
    enc.reviewed_at = now_iso()
    session.add(enc)
    session.commit()
    session.close()

    # Optionally delete the output file
    deleted = False
    if args.delete and output_path and output_path.exists():
        output_path.unlink()
        deleted = True

    log_info(
        "review_rejected",
        id=int(args.id),
        source=str(source_path),
        output=str(output_path) if output_path else None,
        deleted=deleted,
    )

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
        log_error("compare_source_missing", source=str(source_path))
        return 1

    if not encoded_path.exists():
        log_error("compare_encoded_missing", encoded=str(encoded_path))
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
        log_error("compare_probe_failed", source=str(source_path))
        return 1

    duration = source_info.duration_seconds or 60
    seek_pos = args.seek if args.seek is not None else duration / 2
    clip_duration = args.duration

    # Ensure we don't seek past the end
    if seek_pos + clip_duration > duration:
        seek_pos = max(0, duration - clip_duration - 5)

    log_info(
        "compare_clips_start",
        source=str(source_path),
        encoded=str(encoded_path),
        clip_dir=str(clip_dir),
        seek_pos_sec=float(seek_pos),
        clip_duration_sec=float(clip_duration),
    )

    # Determine output format - try to keep source codec for browser compatibility
    # For H.264 source, keep as .mp4; for AV1 encoded, keep as .mp4
    source_clip = clip_dir / "source.mp4"
    encoded_clip = clip_dir / "encoded.mp4"

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_error("compare_ffmpeg_missing")
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
        log_info("compare_extract_clip", kind="source", path=str(source_clip))
        subprocess.run(cmd_source, check=True, capture_output=True)
        log_info("compare_extract_clip", kind="encoded", path=str(encoded_clip))
        subprocess.run(cmd_encoded, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log_error("compare_extract_failed", error=e.stderr.decode()[:500])
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

    log_info(
        "compare_ready",
        clip_dir=str(clip_dir),
        html=str(html_file),
        open_cmd=f"open \"{html_file}\"" if platform.system() == "Darwin" else None,
    )

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
        log_error("compare_source_missing", source=str(source_path))
        return 1

    if not encoded_path.exists():
        log_error("compare_encoded_missing", encoded=str(encoded_path))
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

    log_info(
        "compare_full_start",
        source=str(source_path),
        encoded=str(encoded_path),
        html=str(html_file),
    )

    generate_compare_html(
        source_path=source_path,
        encoded_path=encoded_path,
        output_file=html_file,
        source_info=source_info,
    )

    log_info(
        "compare_ready",
        html=str(html_file),
        open_cmd=f"open \"{html_file}\"" if platform.system() == "Darwin" else None,
    )

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
        log_error("review_db_missing", db=str(db_path))
        return 1

    session = init_db(db_path)
    enc = session.get(EncodeResult, args.id)
    if not enc:
        session.close()
        log_error("review_encode_not_found", id=int(args.id))
        return 1

    source_path = pathlib.Path(enc.source_path)
    output_path = pathlib.Path(enc.output_path) if enc.output_path else None
    session.close()

    if not source_path.exists():
        log_error("compare_source_missing", source=str(source_path))
        return 1

    if not output_path or not output_path.exists():
        log_error("compare_encoded_missing", encoded=str(output_path) if output_path else None)
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
        log_error("compare_probe_failed", source=str(source_path))
        return 1

    duration = source_info.duration_seconds or 60

    # Determine sample positions - use middle by default, or worst VMAF spots if available
    if args.seek:
        positions = [args.seek]
    else:
        # Use mid-point
        positions = [duration / 2]

    clip_duration = args.duration

    log_info(
        "compare_video_start",
        id=int(args.id),
        source=str(source_path),
        encoded=str(output_path),
        position_sec=float(positions[0]),
        clip_duration_sec=float(clip_duration),
        output=str(compare_file),
    )

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
        log_info(
            "compare_video_ready",
            output=str(compare_file),
            open_cmd=f"open \"{compare_file}\"" if platform.system() == "Darwin" else None,
        )
    except subprocess.CalledProcessError as e:
        log_error("compare_video_failed", error=e.stderr.decode()[:500])
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
        default=default_transcode_root(),
        help=f"Output directory root (default: {default_transcode_root()})",
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
    p_run.add_argument(
        "--api-url",
        dest="api_url",
        help="Mediaforce API base URL for worker coordination (or set MEDIAFORCE_API_URL)",
    )
    p_run.add_argument("--settings-url", help="Remote settings endpoint (e.g., http://host:5555/api/settings/current)")
    p_run.add_argument(
        "--profile-settings-url",
        dest="profile_settings_url",
        help="Remote profile thresholds/settings JSON for quality loop (or set MEDIAFORCE_PROFILE_SETTINGS_URL)",
    )
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
        default=default_transcode_root(),
        help=f"Root of transcode folder (default: {default_transcode_root()})",
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
        help="Move original to hidden backup after promotion (default: True)",
    )
    p_promote.add_argument(
        "--no-delete",
        action="store_false",
        dest="delete_original",
        help="Keep original file after promotion (no backup move)",
    )
    p_promote.set_defaults(func=cmd_promote)

    # purge-backups
    p_purge = subparsers.add_parser(
        "purge-backups",
        help="Purge promotion backup files for older promoted items (dry-run by default)",
    )
    p_purge.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        help="Only purge backups for items promoted more than N days ago (default: 30)",
    )
    p_purge.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max backups to delete (0 = no limit)",
    )
    p_purge.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete backup files (default: dry-run)",
    )
    p_purge.set_defaults(func=cmd_purge_backups)

    # import-show-config
    p_import_show_config = subparsers.add_parser(
        "import-show-config",
        help="Import legacy show_config.json into DB show overrides (dry-run by default)",
    )
    p_import_show_config.add_argument(
        "--path",
        help="Path to show_config.json (default: ./show_config.json or ~/.config/mediaforce/show_config.json)",
    )
    p_import_show_config.add_argument(
        "--apply",
        action="store_true",
        help="Actually write overrides to the database (default: dry-run)",
    )
    p_import_show_config.add_argument(
        "--overwrite-existing",
        dest="overwrite_existing",
        action="store_true",
        help="Overwrite existing DB overrides when importing (default: keep DB values)",
    )
    p_import_show_config.set_defaults(func=cmd_import_show_config)

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
        default=default_transcode_root(),
        help=f"Root of transcode folder (default: {default_transcode_root()})",
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
    from mediaforce.config.dotenv import load_dotenv_if_present

    load_dotenv_if_present()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
