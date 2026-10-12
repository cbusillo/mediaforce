"""Named sound choices an owner picks per show, or as a TV library default.

A preset only ever changes the audio policy of the shows it is chosen for.
Genre or names never apply one; they can at most suggest one for the owner
to confirm.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


def apply_audio_preset(audio_policy: dict[str, Any], key: str, base_audio: Mapping[str, Any]) -> None:
    values, removed = audio_preset_values(key, base_audio)
    audio_policy.update(values)
    for removed_key in removed:
        audio_policy.pop(removed_key, None)
