from __future__ import annotations

import json
import pathlib
import time
from typing import Optional, Callable

from sqlmodel import Session, select

from mediaforce.domain.types import MediaInfo, TierSettings, ClassificationResult
from mediaforce.db.models import MediaItem

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov"}


def detect_hdr(info: MediaInfo) -> tuple[bool, Optional[str]]:
    """Detect if content is HDR and what format (lightweight heuristic)."""

    if info.is_hdr:
        return True, info.hdr_format or "unknown"

    if info.video_bit_depth and info.video_bit_depth > 8:
        # Placeholder: future improvement to read color_transfer/primaries
        return True, info.hdr_format or "unknown"
    return False, None


def detect_interlaced(info: MediaInfo) -> bool:
    """Detect if content is interlaced (uses MediaInfo flags)."""

    return info.is_interlaced


def collect_video_files(path: pathlib.Path) -> list[pathlib.Path]:
    """Collect video files from a path (file or directory)."""

    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTENSIONS else []

    files: list[pathlib.Path] = []
    for p in path.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith("."):
            files.append(p)
    return files


def calculate_priority(
    potential_savings_bytes: Optional[int],
    mtime: int,
    max_savings: int,
    max_age: int,
) -> float:
    """Calculate priority score (0-1, higher = encode first)."""

    now = int(time.time())
    age = now - mtime

    age_score = min(1.0, age / max_age) if max_age > 0 else 0.5

    if potential_savings_bytes and potential_savings_bytes > 0 and max_savings > 0:
        savings_score = min(1.0, potential_savings_bytes / max_savings)
    else:
        savings_score = 0.0

    return (age_score * 0.3) + (savings_score * 0.7)


def scan_file_to_db(
    session: Session,
    file_path: pathlib.Path,
    max_savings: int,
    max_age: int,
    library_id: str | None,
    *,
    classify_source: Callable[[MediaInfo], ClassificationResult],
    probe_media: Callable[[pathlib.Path], Optional[MediaInfo]],
    now_iso: Callable[[], str],
) -> Optional[dict]:
    """Scan a single file and insert/update DB entry.

    External dependencies are injected to avoid circular imports.
    """

    info = probe_media(file_path)
    if info is None:
        return None

    stat = file_path.stat()
    size_bytes = stat.st_size
    mtime = int(stat.st_mtime)

    is_av1 = info.is_already_av1
    is_hdr, hdr_format = detect_hdr(info)
    is_interlaced = detect_interlaced(info)

    is_opus = any((t.get("codec") or "").lower() == "opus" for t in info.audio_tracks)

    classification = classify_source(info)
    tier = classification.tier.value
    tier_reasoning = "; ".join(classification.reasons)

    settings: Optional[TierSettings] = classification.recommended_settings
    estimated_target = None
    potential_savings = None
    if settings and info.video_bitrate_kbps:
        av1_ratio = {26: 0.40, 28: 0.35, 30: 0.30, 32: 0.25}.get(settings.crf, 0.35)
        estimated_target = int(info.video_bitrate_kbps * av1_ratio)
        if info.video_bitrate_kbps > estimated_target:
            bitrate_diff = info.video_bitrate_kbps - estimated_target
            potential_savings = int(bitrate_diff * (info.duration_seconds or 0) * 1000 / 8)

    priority = calculate_priority(potential_savings, mtime, max_savings, max_age)

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
    item.is_hdr = is_hdr or bool(info.is_hdr)
    item.hdr_format = hdr_format or info.hdr_format
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
    session.refresh(item)

    return {
        "tier": tier,
        "priority": priority,
        "status": status,
        "skip_reason": skip_reason,
        "is_interlaced": is_interlaced,
        "is_hdr": is_hdr,
        "hdr_format": hdr_format,
        "is_av1": is_av1,
        "is_opus": is_opus,
    }


def recalculate_priorities(
    session: Session,
    max_age: int,
    calculate_priority: Callable[[Optional[int], int, int, int], float],
) -> None:
    """Update all pending items with fresh priority scores."""

    # Find max potential savings for normalization
    from sqlalchemy import func
    max_savings_row = session.exec(select(func.max(MediaItem.potential_savings_bytes))).first()
    max_savings = int(max_savings_row or 1)

    items = session.exec(select(MediaItem).where(MediaItem.status == "pending")).all()
    for item in items:
        item.priority_score = calculate_priority(
            item.potential_savings_bytes,
            item.mtime,
            max_savings,
            max_age,
        )
        session.add(item)
    session.commit()
