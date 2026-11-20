from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Literal

from mediaforce.core.audio_presets import AUDIO_PRESETS, KEEP_SURROUND
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_dict
from mediaforce.library.media_scopes import normalize_scope_prefix

AudioPresetSource = Literal["show", "genre", "library", "default"]


def tv_show_prefix(config: MediaforceConfig, prefix: str) -> str | None:
    parts = PurePosixPath(normalize_scope_prefix(prefix)).parts
    if len(parts) < 2 or config.library_type_map.get(parts[0]) != "tv":
        return None
    return "/".join(parts[:2])


def show_audio_preset_payload(
        config: MediaforceConfig,
        prefix: str,
        *,
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
    }


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
    if config.uses_genre_sound(prefix):
        return "genre"
    return "library" if library_default else "default"


def _preset_key(policy: Mapping[str, Any]) -> str:
    preset = str(object_dict(policy.get("audio")).get("preset") or "")
    return preset if preset in AUDIO_PRESETS else KEEP_SURROUND


def _library_default_preset(config: MediaforceConfig, show_prefix: str) -> str | None:
    library = object_dict(config.library_definition_map.get(show_prefix.split("/", 1)[0]))
    preset = str(object_dict(library.get("policy")).get("audio_preset") or "")
    return preset if preset in AUDIO_PRESETS else None
