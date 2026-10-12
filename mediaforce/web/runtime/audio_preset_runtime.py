from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from mediaforce.core.audio_presets import AUDIO_PRESETS, KEEP_SURROUND
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_dict
from mediaforce.library.media_scopes import normalize_scope_prefix

AudioPresetSource = Literal["show", "library", "default"]


def tv_show_prefix(config: MediaforceConfig, prefix: str) -> str | None:
    parts = PurePosixPath(normalize_scope_prefix(prefix)).parts
    if len(parts) < 2 or config.library_type_map.get(parts[0]) != "tv":
        return None
    return "/".join(parts[:2])


def show_audio_preset_payload(config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    show_prefix = tv_show_prefix(config, prefix)
    if show_prefix is None:
        return None
    current = str(object_dict(config.resolve_policy(prefix).get("audio")).get("preset") or "")
    library_default = _library_default_preset(config, show_prefix)
    return {
        "show_prefix": show_prefix,
        "current": current if current in AUDIO_PRESETS else KEEP_SURROUND,
        "source": _preset_source(config, prefix, show_prefix, library_default),
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
    return "library" if library_default else "default"


def _library_default_preset(config: MediaforceConfig, show_prefix: str) -> str | None:
    library = object_dict(config.library_definition_map.get(show_prefix.split("/", 1)[0]))
    preset = str(object_dict(library.get("policy")).get("audio_preset") or "")
    return preset if preset in AUDIO_PRESETS else None
