from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Optional

from mediaforce.config.logging import log_event
from mediaforce.domain.types import MediaInfo


def find_ffprobe() -> Optional[str]:
    for candidate in [
        "/opt/homebrew/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "ffprobe",
    ]:
        if shutil.which(candidate):
            return candidate
    return None


def probe_media(path: pathlib.Path) -> Optional[MediaInfo]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        log_event(40, "ffprobe_missing", path=str(path))
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
        log_event(40, "ffprobe_failed", path=str(path), error=str(e))
        return None

    info = MediaInfo(path=path)

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

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")

        if codec_type == "video":
            info.video_codec = stream.get("codec_name")
            info.video_width = stream.get("width")
            info.video_height = stream.get("height")

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

            if bitrate := stream.get("bit_rate"):
                try:
                    info.video_bitrate_kbps = int(bitrate) // 1000
                except ValueError:
                    pass
            if info.video_bitrate_kbps is None and info.container_bitrate_kbps is not None:
                info.video_bitrate_kbps = info.container_bitrate_kbps

            if fr := stream.get("avg_frame_rate"):
                try:
                    num, den = fr.split("/")
                    if float(den) != 0:
                        info.video_framerate = float(num) / float(den)
                except Exception:
                    pass

            info.video_field_order = stream.get("field_order")

            # HDR hints (best-effort)
            transfer = (stream.get("color_transfer") or "").lower()
            primaries = (stream.get("color_primaries") or "").lower()
            matrix = (stream.get("color_space") or "").lower()
            if "smpte2084" in transfer or "arib-std-b67" in transfer:
                info.is_hdr = True
                info.hdr_format = "hdr10" if "smpte2084" in transfer else "hlg"
            elif "bt2020" in primaries or "bt2020" in matrix:
                info.is_hdr = True
                info.hdr_format = "bt2020"

        elif codec_type == "audio":
            info.audio_tracks.append({
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "lang": (stream.get("tags", {}) or {}).get("language"),
                "title": (stream.get("tags", {}) or {}).get("title"),
            })

        elif codec_type == "subtitle":
            info.subtitle_tracks.append({
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "lang": (stream.get("tags", {}) or {}).get("language"),
                "title": (stream.get("tags", {}) or {}).get("title"),
            })

    return info

