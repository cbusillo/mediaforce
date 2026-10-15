"""Let a season follow its show's approved settings instead of its own."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from mediaforce.core.config import MediaforceConfig, remove_runtime_folder_policy_override
from mediaforce.library.media_scopes import normalize_scope_prefix
from mediaforce.web.runtime.audio_preset_runtime import tv_show_prefix


def use_show_settings(
        config: MediaforceConfig,
        prefix: str,
        *,
        show_has_approval: Callable[[str], bool],
        season_has_tuning: Callable[[str], bool],
        clear_tuning: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Remove a season's own saved settings and sample approval so the show's approval governs it.

    Files already published keep what they were made with; only later work and publishing checks change.
    """
    season_prefix = normalize_scope_prefix(prefix)
    show_prefix = tv_show_prefix(config, season_prefix)
    if show_prefix is None or len(PurePosixPath(season_prefix).parts) != 3:
        return {"ok": False, "message": "Only a season can switch to its show's settings."}
    if not show_has_approval(show_prefix):
        return {"ok": False, "message": "The show has no approved settings yet. Approve a show sample first."}
    had_tuning = season_has_tuning(season_prefix)
    if had_tuning:
        cleared = clear_tuning(season_prefix)
        if not cleared.get("ok"):
            return {"ok": False, "message": str(cleared.get("message") or "The season's sample could not be cleared.")}
    removed = remove_runtime_folder_policy_override(config.paths.runtime_settings_path, season_prefix)
    if not (had_tuning or removed):
        return {"ok": True, "changed": False, "message": "This season already uses the show's settings."}
    return {"ok": True, "changed": True, "message": "This season now uses the show's approved settings."}
