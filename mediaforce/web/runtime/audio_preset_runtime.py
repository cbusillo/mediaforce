from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import select

from mediaforce.core.audio_presets import AUDIO_PRESETS, KEEP_SURROUND, SPEECH_STEREO
from mediaforce.core.config import MediaforceConfig, load_runtime_settings, update_runtime_settings
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import series_metadata
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.library.media_scopes import normalize_scope_prefix

AudioPresetSource = Literal["show", "library", "default"]
# TMDB genre names whose shows are mostly talking; they only ever suggest a choice.
SPEECH_GENRES = ("Reality", "Talk")
SUGGESTION_DISMISSALS_KEY = "audio_preset_suggestions_dismissed"


def tv_show_prefix(config: MediaforceConfig, prefix: str) -> str | None:
    parts = PurePosixPath(normalize_scope_prefix(prefix)).parts
    if len(parts) < 2 or config.library_type_map.get(parts[0]) != "tv":
        return None
    return "/".join(parts[:2])


def show_audio_preset_payload(
        config: MediaforceConfig,
        prefix: str,
        *,
        genres: Sequence[str] = (),
        sample_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The show's sound choice; with the approved sample's policy, also whether it was made with another one."""
    show_prefix = tv_show_prefix(config, prefix)
    if show_prefix is None:
        return None
    current = _preset_key(config.resolve_policy(prefix))
    library_default = _library_default_preset(config, show_prefix)
    source = _preset_source(config, prefix, show_prefix, library_default)
    return {
        "show_prefix": show_prefix,
        "current": current,
        # Production uses the current choice; the approval stays valid, but its clips used the older sound.
        "sound_changed_since_sample": sample_policy is not None and _preset_key(sample_policy) != current,
        "source": source,
        "library_default": library_default,
        "options": [preset.to_payload() for preset in AUDIO_PRESETS.values()],
        "suggestion": (
            _genre_suggestion(genres)
            if source == "default" and show_prefix not in dismissed_suggestions(config.paths.runtime_settings_path)
            else None
        ),
    }


def series_genres(connection: DBClient, show_prefix: str) -> tuple[str, ...]:
    genres_json = connection.execute(
        select(series_metadata.c.tmdb_genres_json).where(series_metadata.c.series_prefix == show_prefix)
    ).scalar_one_or_none()
    try:
        return tuple(str(genre) for genre in object_list(json.loads(genres_json or "[]")))
    except json.JSONDecodeError:
        return ()


def dismissed_suggestions(runtime_settings_path: Path) -> set[str]:
    return {str(prefix) for prefix in object_list(load_runtime_settings(runtime_settings_path).get(SUGGESTION_DISMISSALS_KEY))}


def dismiss_suggestion(runtime_settings_path: Path, show_prefix: str) -> None:
    def _apply(runtime_settings: dict[str, Any]) -> dict[str, Any]:
        dismissed = {str(prefix) for prefix in object_list(runtime_settings.get(SUGGESTION_DISMISSALS_KEY))}
        runtime_settings[SUGGESTION_DISMISSALS_KEY] = sorted(dismissed | {show_prefix})
        return runtime_settings

    update_runtime_settings(runtime_settings_path, _apply)


def _genre_suggestion(genres: Sequence[str]) -> dict[str, str] | None:
    genre = next((name for name in SPEECH_GENRES if name in genres), None)
    if genre is None:
        return None
    preset = AUDIO_PRESETS[SPEECH_STEREO]
    return {"preset": preset.key, "label": preset.label, "reason": f"TMDB lists this show as {genre}."}


def _preset_source(
        config: MediaforceConfig,
        prefix: str,
        show_prefix: str,
        library_default: str | None,
) -> AudioPresetSource:
    normalized = normalize_scope_prefix(prefix)
    for override in config.overrides:
        override_prefix = str(override.get("path_prefix", "")).strip("/")
        within_show = override_prefix == show_prefix or override_prefix.startswith(f"{show_prefix}/")
        applies = normalized == override_prefix or normalized.startswith(f"{override_prefix}/")
        if within_show and applies and object_dict(override.get("audio")).get("preset"):
            return "show"
    return "library" if library_default else "default"


def _preset_key(policy: Mapping[str, Any]) -> str:
    preset = str(object_dict(policy.get("audio")).get("preset") or "")
    return preset if preset in AUDIO_PRESETS else KEEP_SURROUND


def _library_default_preset(config: MediaforceConfig, show_prefix: str) -> str | None:
    library = object_dict(config.library_definition_map.get(show_prefix.split("/", 1)[0]))
    preset = str(object_dict(library.get("policy")).get("audio_preset") or "")
    return preset if preset in AUDIO_PRESETS else None
