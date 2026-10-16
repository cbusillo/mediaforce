"""Let a season follow its show's approved settings instead of its own."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig, remove_runtime_folder_policy_overrides_within
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.library.media_scopes import normalize_scope_prefix, path_matches_scope, scope_descendant_filter
from mediaforce.web.runtime.audio_preset_runtime import tv_show_prefix


def use_show_settings(
        config: MediaforceConfig,
        prefix: str,
        *,
        show_has_approval: Callable[[str], bool],
        tuned_prefixes: Callable[[str], Iterable[str]],
        clear_tuning: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Remove a season's own saved settings and sample approvals so the show's approval governs it.

    That includes any episode inside the season with its own sample or settings, since the nearest
    approval governs a file. Files already published keep what they were made with; only later work
    and publishing checks change.
    """
    season_prefix = normalize_scope_prefix(prefix)
    show_prefix = tv_show_prefix(config, season_prefix)
    if show_prefix is None or len(PurePosixPath(season_prefix).parts) != 3:
        return {"ok": False, "message": "Only a season can switch to its show's settings."}
    if not show_has_approval(show_prefix):
        return {"ok": False, "message": "The show has no approved settings yet. Approve a show sample first."}
    # Clear the nearest samples first, so a refusal leaves the season's own approval governing its files.
    tuned = sorted(
        {tuned_prefix for tuned_prefix in tuned_prefixes(season_prefix) if path_matches_scope(tuned_prefix, season_prefix)},
        key=lambda tuned_prefix: (-len(tuned_prefix), tuned_prefix),
    )
    for tuned_prefix in tuned:
        cleared = clear_tuning(tuned_prefix)
        if not cleared.get("ok"):
            return {"ok": False, "message": str(cleared.get("message") or "The season's sample could not be cleared.")}
    removed = remove_runtime_folder_policy_overrides_within(config.paths.runtime_settings_path, season_prefix)
    if not (tuned or removed):
        return {"ok": True, "changed": False, "message": "This season already uses the show's settings."}
    return {"ok": True, "changed": True, "message": "This season now uses the show's approved settings."}


def prefixes_with_samples(
        connection: DBClient,
        season_prefix: str,
        *,
        has_sample: Callable[[str], bool],
) -> list[str]:
    """The season and every folder or file inside it that holds a saved sample."""
    season_prefix = normalize_scope_prefix(season_prefix)
    candidates = {season_prefix}
    for rel_path in connection.execute(
        select(library_items.c.rel_path).where(scope_descendant_filter(library_items.c.rel_path, season_prefix))
    ).scalars():
        path = PurePosixPath(str(rel_path))
        candidates.update(
            folder.as_posix() for folder in (path, *path.parents) if path_matches_scope(folder.as_posix(), season_prefix)
        )
    return [candidate for candidate in sorted(candidates) if has_sample(candidate)]
