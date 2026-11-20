from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import read_catalog_value
from mediaforce.core.db_tables import series_metadata

from mediaforce.core.type_defs import object_list

KEEP_SURROUND = "keep_surround"
SPEECH_STEREO = "speech_stereo"
# Keys a preset writes. Choosing another preset rewrites or removes all of them.
PRESET_AUDIO_KEYS = ("preset", "downmix_to_stereo", "stereo_opus_bitrate", "copy_codecs", "convert_to_opus_codecs")
SPEECH_STEREO_BITRATE = "96k"


@dataclass(frozen=True, slots=True)
class AudioPreset:
    key: str
    label: str
    summary: str

    def to_payload(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "summary": self.summary}


AUDIO_PRESETS: dict[str, AudioPreset] = {
    KEEP_SURROUND: AudioPreset(
        key=KEEP_SURROUND,
        label="Film",
        summary="Keeps surround sound at the standard quality.",
    ),
    SPEECH_STEREO: AudioPreset(
        key=SPEECH_STEREO,
        label="Speech-heavy",
        summary="Stereo at 96 kbps. Also re-encodes AAC sound to save space.",
    ),
}


SPEECH_GENRE_SETTING = "speech_heavy_for_reality_and_talk"


def is_reality_or_talk(db_path: Path, show_prefix: str) -> bool:
    genres = read_catalog_value(db_path, select(series_metadata.c.tmdb_genres_json).where(series_metadata.c.series_prefix == show_prefix))
    try:
        return any(genre in ("Reality", "Talk") for genre in object_list(json.loads(genres or "[]")))
    except json.JSONDecodeError:
        return False


def audio_preset_values(key: str, base_audio: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return the audio values a preset sets and the preset keys it removes.

    ``base_audio`` is the default audio policy the preset starts from, so
    "Film" restores the defaults wherever a show previously used another preset.
    """
    if key not in AUDIO_PRESETS:
        raise ValueError(f"Unknown sound choice: {key}")
    copy_codecs = [str(codec) for codec in object_list(base_audio.get("copy_codecs"))]
    convert_codecs = [str(codec) for codec in object_list(base_audio.get("convert_to_opus_codecs"))]
    if key == SPEECH_STEREO:
        return (
            {
                "preset": key,
                "downmix_to_stereo": True,
                "stereo_opus_bitrate": SPEECH_STEREO_BITRATE,
                "copy_codecs": [codec for codec in copy_codecs if codec != "aac"],
                "convert_to_opus_codecs": [*convert_codecs, *([] if "aac" in convert_codecs else ["aac"])],
            },
            (),
        )
    return (
        {
            "preset": key,
            "stereo_opus_bitrate": base_audio.get("stereo_opus_bitrate"),
            "copy_codecs": copy_codecs,
            "convert_to_opus_codecs": convert_codecs,
        },
        ("downmix_to_stereo",),
    )


def owner_sound_keys(audio_policy: Mapping[str, Any]) -> frozenset[str]:
    """Audio keys only the owner's sound choice may change.

    Once a show or library has a sound choice, every key that choice writes
    belongs to it; tuning proposals may still change the rest.
    """
    if str(audio_policy.get("preset") or "") in AUDIO_PRESETS:
        return frozenset(PRESET_AUDIO_KEYS)
    return frozenset({"preset", "downmix_to_stereo"})


def apply_audio_preset(audio_policy: dict[str, Any], key: str, base_audio: Mapping[str, Any]) -> None:
    values, removed = audio_preset_values(key, base_audio)
    audio_policy.update(values)
    for removed_key in removed:
        audio_policy.pop(removed_key, None)
