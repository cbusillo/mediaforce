import json
import sqlite3
from typing import Any

from mediaforce.core.utils import timestamp


def _select_streams(item: dict[str, Any], *, text_subtitle_codecs: set[str]) -> dict[str, Any]:
    audio_tracks = item["audio_summary"]
    subtitle_tracks = item["subtitle_summary"]

    selected_audio = _pick_audio(audio_tracks)
    selected_subtitles = _pick_subtitles(
        subtitle_tracks,
        bool(item["resolved_policy"]["subtitle"].get("prefer_text", True)),
        text_subtitle_codecs=text_subtitle_codecs,
    )

    return {
        "audio_tracks": [selected_audio],
        "subtitle_tracks": selected_subtitles,
    }


def estimate_output_overhead_bytes(item: dict[str, Any], *, text_subtitle_codecs: set[str]) -> dict[str, int]:
    selection = _select_streams(item, text_subtitle_codecs=text_subtitle_codecs)
    duration_seconds = float(item.get("duration_seconds") or 0.0)
    audio_bytes = 0
    for track in selection["audio_tracks"]:
        audio_bytes += _estimate_audio_track_bytes(track, item["resolved_policy"]["audio"], duration_seconds)

    subtitle_bytes = 0
    for track in selection["subtitle_tracks"]:
        subtitle_bytes += _estimate_subtitle_track_bytes(track, text_subtitle_codecs=text_subtitle_codecs)

    container_bytes = 256 * 1024
    return {
        "audio_bytes": audio_bytes,
        "subtitle_bytes": subtitle_bytes,
        "container_bytes": container_bytes,
        "total_bytes": audio_bytes + subtitle_bytes + container_bytes,
    }


def _pick_audio(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    english = [track for track in audio_tracks if track.get("language") == "eng"]
    candidates = english or [track for track in audio_tracks if track.get("language") in {None, "und"}] or audio_tracks
    if not candidates:
        raise ValueError("No audio tracks available")
    return sorted(candidates, key=lambda track: (-int(track.get("default") or 0), -(int(track.get("channels") or 0)),
                                                 int(track["index"])))[0]


def _pick_subtitles(
        subtitle_tracks: list[dict[str, Any]], prefer_text: bool, *, text_subtitle_codecs: set[str]
) -> list[dict[str, Any]]:
    english = [track for track in subtitle_tracks if track.get("language") == "eng"]
    if not english:
        fallback = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
        if fallback and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks):
            english = fallback
        else:
            return []

    def sort_key(track: dict[str, Any]) -> tuple[int, int, int, int]:
        codec = str(track.get("codec_name") or "")
        is_text = codec in text_subtitle_codecs
        return (
            0 if (prefer_text and is_text and not track.get("forced")) else 1,
            0 if (not track.get("forced")) else 1,
            0 if track.get("default") else 1,
            int(track["index"]),
        )

    ordered = sorted(english, key=sort_key)
    forced = [track for track in ordered if track.get("forced")]
    full = [track for track in ordered if not track.get("forced")]
    return full[:1] + forced + full[1:]


def _source_has_preservable_subtitles(subtitle_tracks: list[dict[str, Any]]) -> bool:
    if any(track.get("language") == "eng" for track in subtitle_tracks):
        return True
    untagged = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
    return bool(untagged) and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks)


def _audio_codec(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    codec = str(track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in audio_policy.get("copy_codecs", [])}:
        return "copy"
    if codec in {str(name).lower() for name in audio_policy.get("convert_to_opus_codecs", [])}:
        return "libopus"
    return "copy"


def _opus_bitrate(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return str(audio_policy["surround_7_1_opus_bitrate"])
    if channels >= 6:
        return str(audio_policy["surround_5_1_opus_bitrate"])
    return str(audio_policy["stereo_opus_bitrate"])


def _opus_layout_filter(track: dict[str, Any]) -> str | None:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return "channelmap=channel_layout=7.1"
    if channels >= 6:
        return "channelmap=channel_layout=5.1"
    return None


def _check(validation: dict[str, Any], passed: bool, message: str) -> None:
    validation["checks"].append({"passed": passed, "message": message})
    validation["passed"] = validation["passed"] and passed


def _estimate_audio_track_bytes(track: dict[str, Any], audio_policy: dict[str, Any], duration_seconds: float) -> int:
    codec = _audio_codec(track, audio_policy)
    if codec == "libopus":
        bitrate_text = _opus_bitrate(track, audio_policy)
        bitrate_bps = _parse_bitrate_text(bitrate_text)
    else:
        bitrate_bps = int(track.get("bit_rate") or 0)
        if bitrate_bps <= 0:
            channels = int(track.get("channels") or 2)
            bitrate_bps = 640_000 if channels >= 6 else 192_000
    return int((bitrate_bps / 8) * duration_seconds)


def _estimate_subtitle_track_bytes(track: dict[str, Any], *, text_subtitle_codecs: set[str]) -> int:
    bit_rate = int(track.get("bit_rate") or 0)
    if bit_rate > 0:
        duration_seconds = float(track.get("duration_seconds") or 0.0)
        if duration_seconds > 0:
            return int((bit_rate / 8) * duration_seconds)
    codec = str(track.get("codec_name") or "").lower()
    if codec in text_subtitle_codecs:
        return 128 * 1024
    return 4 * 1024 * 1024


def _parse_bitrate_text(value: str) -> int:
    stripped = value.strip().lower()
    if stripped.endswith("k"):
        return int(float(stripped[:-1]) * 1000)
    if stripped.endswith("m"):
        return int(float(stripped[:-1]) * 1_000_000)
    return int(float(stripped))


def _record_event(connection: sqlite3.Connection, library_item_id: int, event_type: str,
                  details: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO item_events(library_item_id, created_at, event_type, details_json) VALUES (?, ?, ?, ?)",
        (library_item_id, timestamp(), event_type, json.dumps(details, separators=(",", ":"))),
    )


def _format_crf(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"
