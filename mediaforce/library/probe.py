import json
import subprocess
from pathlib import Path

from mediaforce.core.binaries import ffprobe_binary
from mediaforce.core.models import ProbeSummary
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.cadence import analyze_cadence
from mediaforce.encoding.fingerprint import analyze_media_fingerprint

TRACK_FIELDS = ("index", "codec_name", "channels", "bit_rate", "language", "default", "forced")
PROBE_TIMEOUT_SECONDS = 60


def _normalize_language(stream: dict[str, object]) -> str | None:
    tags = object_dict(stream.get("tags"))
    language = tags.get("language") or tags.get("LANGUAGE")
    if language is None:
        return None
    return str(language).lower()


def _stream_entry(stream: dict[str, object]) -> dict[str, object]:
    disposition = object_dict(stream.get("disposition"))
    entry = {
        "index": stream.get("index"),
        "codec_name": stream.get("codec_name"),
        "channels": stream.get("channels"),
        "bit_rate": stream.get("bit_rate"),
        "language": _normalize_language(stream),
        "default": int_value(disposition.get("default", 0)),
        "forced": int_value(disposition.get("forced", 0)),
    }
    return {key: entry[key] for key in TRACK_FIELDS}


def probe_media(path: Path) -> ProbeSummary:
    cmd = [
        ffprobe_binary(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    payload = object_dict(json.loads(result.stdout))

    streams = [object_dict(stream) for stream in object_list(payload.get("streams"))]
    format_info = object_dict(payload.get("format"))
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]

    audio_summary = [_stream_entry(stream) for stream in audio_streams]
    subtitle_summary = [_stream_entry(stream) for stream in subtitle_streams]

    default_audio_language = next(
        (_normalize_language(stream) for stream in audio_streams if object_dict(stream.get("disposition")).get("default")),
        _normalize_language(audio_streams[0]) if audio_streams else None,
    )
    default_subtitle_language = next(
        (_normalize_language(stream) for stream in subtitle_streams if
         object_dict(stream.get("disposition")).get("default")),
        _normalize_language(subtitle_streams[0]) if subtitle_streams else None,
    )

    duration_seconds = _as_float(format_info.get("duration"))
    cadence_summary = analyze_cadence(
        path,
        video_stream=video_stream,
        duration_seconds=duration_seconds,
    )
    media_fingerprint = analyze_media_fingerprint(
        path,
        video_stream=video_stream,
        audio_streams=audio_streams,
        duration_seconds=duration_seconds,
    )

    return ProbeSummary(
        duration_seconds=duration_seconds,
        video_codec=video_stream.get("codec_name") if video_stream else None,
        video_bitrate=_as_int(video_stream.get("bit_rate")) if video_stream else None,
        width=_as_int(video_stream.get("width")) if video_stream else None,
        height=_as_int(video_stream.get("height")) if video_stream else None,
        pix_fmt=video_stream.get("pix_fmt") if video_stream else None,
        audio_track_count=len(audio_streams),
        subtitle_track_count=len(subtitle_streams),
        english_audio_count=sum(1 for stream in audio_streams if _normalize_language(stream) == "eng"),
        english_subtitle_count=sum(1 for stream in subtitle_streams if _normalize_language(stream) == "eng"),
        default_audio_language=default_audio_language,
        default_subtitle_language=default_subtitle_language,
        audio_summary_json=json.dumps(audio_summary, separators=(",", ":"), sort_keys=True),
        subtitle_summary_json=json.dumps(subtitle_summary, separators=(",", ":"), sort_keys=True),
        cadence_summary_json=json.dumps(cadence_summary, separators=(",", ":"), sort_keys=True),
        media_fingerprint_json=json.dumps(media_fingerprint, separators=(",", ":"), sort_keys=True),
    )


def _as_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int, str)):
        return float(value)
    raise TypeError(f"Unsupported float value: {value!r}")


def _as_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"Unsupported int value: {value!r}")
