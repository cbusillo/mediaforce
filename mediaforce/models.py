from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ProbeSummary:
    duration_seconds: float | None
    video_codec: str | None
    video_bitrate: int | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    audio_track_count: int
    subtitle_track_count: int
    english_audio_count: int
    english_subtitle_count: int
    default_audio_language: str | None
    default_subtitle_language: str | None
    audio_summary_json: str
    subtitle_summary_json: str
