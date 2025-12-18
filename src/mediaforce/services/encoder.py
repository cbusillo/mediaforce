from __future__ import annotations

import pathlib
import platform as platform_mod
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any, Callable, Optional

from sqlmodel import Session

from mediaforce.db.models import EncodeResult
from mediaforce.domain.types import QualityMetrics, MediaInfo, TierSettings

# Denoise filter strings for ffmpeg
DENOISE_FILTERS: dict[str, str] = {
    "light": "hqdn3d=2:2:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
    "heavy": "nlmeans=s=3.0:p=7:r=9",
}


def parse_ffmpeg_progress(line: str) -> dict:
    data: dict[str, float | int] = {}
    line = line.strip()
    if not line or "=" not in line:
        return data

    key, _, raw_value = line.partition("=")
    key = key.strip()
    value = raw_value.strip()

    if key == "frame":
        try:
            data["frame"] = int(value)
        except ValueError:
            pass
    elif key == "fps":
        try:
            data["fps"] = float(value)
        except ValueError:
            pass
    elif key == "bitrate":
        if value != "N/A":
            match = re.match(r"([\d.]+)kbits/s", value)
            if match:
                try:
                    data["bitrate_kbps"] = float(match.group(1))
                except ValueError:
                    pass
    elif key == "total_size":
        try:
            data["size_bytes"] = int(value)
        except ValueError:
            pass
    elif key == "out_time_us":
        try:
            data["time_encoded_sec"] = int(value) / 1_000_000
        except ValueError:
            pass
    elif key == "out_time_ms":
        # Despite the name, ffmpeg reports out_time_ms in microseconds.
        # (It mirrors out_time_us; see `ffmpeg -progress` output.)
        try:
            data["time_encoded_sec"] = int(value) / 1_000_000
        except ValueError:
            pass
    elif key == "speed":
        if value != "N/A":
            match = re.match(r"([\d.]+)x", value)
            if match:
                try:
                    data["speed"] = float(match.group(1))
                except ValueError:
                    pass

    return data


def run_ffmpeg_with_progress(
    cmd: list[str],
    session: Session,
    progress_id: int,
    duration_sec: float,
    update_progress: Callable[..., None],
) -> subprocess.CompletedProcess:
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

    while True:
        line = process.stdout.readline() if process.stdout else ""
        if not line and process.poll() is not None:
            break

        parsed = parse_ffmpeg_progress(line)
        accumulated.update(parsed)

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
    outlier_result: Optional[Any] = None,
    profile_eval_id: Optional[int] = None,
) -> int:
    completed_at = datetime.now().isoformat()
    compression_ratio = output_size / source_size if source_size > 0 else None

    psnr = metrics.psnr if metrics else None
    ssim = metrics.ssim if metrics else None
    vmaf = metrics.vmaf if metrics else None
    vmaf_sample_sec = metrics.sample_duration_sec if metrics else None

    is_outlier = outlier_result.is_outlier if outlier_result else False
    outlier_reasons = "; ".join(outlier_result.reasons) if outlier_result and outlier_result.reasons else None
    review_status = "pending" if is_outlier else "approved"

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
        profile_eval_id=profile_eval_id,
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return int(result.id)  # type: ignore[arg-type]


def find_ffmpeg() -> Optional[str]:
    """Find ffmpeg executable."""
    for candidate in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]:
        if shutil.which(candidate):
            return candidate
    return None


def choose_output_format(info: MediaInfo) -> str:
    """Select pixel format; prefer 10-bit only when useful."""
    if info.video_bit_depth and info.video_bit_depth > 8:
        return "yuv420p10le"
    if info.is_hdr:
        return "yuv420p10le"
    return "yuv420p"


def apply_downscale_filter(
    input_filters: list[str],
    info: MediaInfo,
    max_height: Optional[int],
) -> list[str]:
    """Append a scale filter if source is taller than max_height."""
    if not max_height or not info.video_height:
        return input_filters

    if info.video_height <= max_height:
        return input_filters

    # Keep width mod-2, preserve aspect
    scale_expr = f"scale=-2:{max_height}"
    new_filters = input_filters.copy()
    new_filters.append(scale_expr)
    return new_filters


def is_english_track(track: dict) -> bool:
    """Check if a track is English."""
    lang = (track.get("language") or "").lower()
    return lang in ("eng", "en", "english")


def is_undefined_track(track: dict) -> bool:
    """Check if a track has no/undefined language."""
    lang = track.get("language")
    return lang is None or lang.lower() in ("", "und", "unk", "unknown")


def select_audio_tracks(audio_tracks: list[dict]) -> list[dict]:
    """Select which audio tracks to keep."""
    if not audio_tracks:
        return []

    english = [t for t in audio_tracks if is_english_track(t)]
    if english:
        return english

    undefined = [t for t in audio_tracks if is_undefined_track(t)]
    if undefined:
        return undefined

    return [audio_tracks[0]]


def select_subtitle_tracks(subtitle_tracks: list[dict]) -> list[dict]:
    """Select which subtitle tracks to keep."""
    TEXT_SUBTITLE_CODECS = {"subrip", "srt", "mov_text", "text"}

    selected = []
    for track in subtitle_tracks:
        codec = (track.get("codec") or "").lower()
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        if is_english_track(track) or is_undefined_track(track):
            selected.append(track)

    english = [t for t in selected if is_english_track(t)]
    if english:
        return english

    return selected


def get_opus_target_bitrate(channels: int) -> int:
    """Get target Opus bitrate for channel count."""
    target_bitrates = {
        1: 64,
        2: 128,
        6: 256,
        8: 384,
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
        system = platform_mod.system().lower()
        if system == "darwin":
            cmd.extend(["-hwaccel", "videotoolbox"])
        elif system == "linux":
            # Only use CUDA when an NVIDIA device is present and we have nvidia-smi
            # This logic was originally in core.py, reproducing it here.
            # Assuming if the user asks for hw_decode on linux, they likely have it,
            # but checking paths is safer.
            if pathlib.Path("/dev/nvidia0").exists() and shutil.which("nvidia-smi"):
                cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

    cmd.extend(["-i", str(input_path)])

    audio_to_keep = select_audio_tracks(media_info.audio_tracks)
    subs_to_keep = select_subtitle_tracks(media_info.subtitle_tracks)

    cmd.extend(["-map", "0:v:0"])
    for track in audio_to_keep:
        cmd.extend(["-map", f"0:{track['index']}"])
    for track in subs_to_keep:
        cmd.extend(["-map", f"0:{track['index']}"])

    vf_parts: list[str] = []
    if media_info.is_interlaced:
        vf_parts.append("bwdif=mode=0:parity=-1:deint=0")
    if settings.denoise and settings.denoise in DENOISE_FILTERS:
        vf_parts.append(DENOISE_FILTERS[settings.denoise])
    vf_parts = apply_downscale_filter(vf_parts, media_info, max_height)
    vf_parts.append(f"format={choose_output_format(media_info)}")

    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])

    if hw_encode and platform_mod.system().lower() == "darwin":
        cmd.extend(["-c:v", "av1_videotoolbox"])
        cmd.extend(["-b:v", "0"])
        cmd.extend(["-crf", str(settings.crf)])
    else:
        cmd.extend(["-c:v", "libsvtav1"])
        cmd.extend(["-crf", str(settings.crf)])
        cmd.extend(["-preset", str(settings.preset)])
        svt_params = [f"film-grain={settings.film_grain}"]
        cmd.extend(["-svtav1-params", ":".join(svt_params)])

    for i, track in enumerate(audio_to_keep):
        channels = track.get("channels") or 2
        codec = (track.get("codec") or "").lower()
        source_bitrate = track.get("bitrate_kbps")
        target_bitrate = get_opus_target_bitrate(channels)
        passthrough = False

        if codec == "opus":
            if source_bitrate is None or source_bitrate <= target_bitrate * 1.2:
                passthrough = True
        elif codec == "aac":
            if source_bitrate and source_bitrate <= target_bitrate:
                passthrough = True

        if passthrough:
            cmd.extend([f"-c:a:{i}", "copy"])
        else:
            cmd.extend([f"-c:a:{i}", "libopus"])
            cmd.extend([f"-b:a:{i}", f"{target_bitrate}k"])
            if channels > 2:
                cmd.extend(["-mapping_family", "1"])
                if channels == 6:
                    cmd.extend([f"-af:a:{i}", "channelmap=channel_layout=5.1"])
                elif channels == 8:
                    cmd.extend([f"-af:a:{i}", "channelmap=channel_layout=7.1"])
            cmd.extend([f"-ac:{i}", str(channels)])

    if not audio_to_keep:
        cmd.extend(["-an"])

    for i, track in enumerate(subs_to_keep):
        cmd.extend([f"-c:s:{i}", "mov_text"])

    if not subs_to_keep:
        cmd.extend(["-sn"])

    cmd.extend(["-y", str(output_path)])
    return cmd