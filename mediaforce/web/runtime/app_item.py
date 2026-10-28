"""Facts for the Mac app's page for one show season, movie or folder, and its episode player.

The page shows every file with where it has got, its size before and after, and whether its original
backup is still there to compare against. The server sends states and codes; the app words them. The
player reads one file at a time as MP4, remuxed once and cached, since the app's player cannot open MKV.
"""

import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import library_items, motion_keep_decisions, staged_artifacts
from mediaforce.library.candidate_selection import encode_candidate_decisions, scope_lifecycle_payload_from_decisions, \
    workflow_eligibility
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope
from mediaforce.library.motion_holds import queue_motion_holds
from mediaforce.library.motion_keep_state import library_item_source_sql
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.web.runtime.episode_progress import load_season_episode_progress

PLAYER_SIDES = ("compressed", "original")
# Each remuxed file is about the size of the one it plays, so only the last few stay, within a byte budget.
PLAYER_CACHE_KEEP = 6
PLAYER_CACHE_BYTES = 40 * 1000 ** 3
PLAYER_FREE_MARGIN_BYTES = 20 * 1000 ** 3
PLAYER_REMUX_TIMEOUT_SECONDS = 30 * 60
PARTIAL_SUFFIX = ".partial"
# One preparation at a time across every file, so the space check, eviction and remux never overlap.
_PLAYER_PREPARATION = threading.Lock()
_EPISODE_NUMBER = re.compile(r"[sS](\d+)[eE](\d+)")


def item_payload(
        config: MediaforceConfig,
        prefix: str,
        *,
        sound: Callable[[DBClient, str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Everything the item page shows for this prefix, or None when it names nothing in the library."""
    with open_readonly_db(config.paths.db_path) as connection:
        scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
        decisions = encode_candidate_decisions(connection, config, prefixes=[scope.prefix])
        motion_holds = queue_motion_holds(connection, decisions)
        workflow = build_folder_workflow_state(
            connection,
            scope.prefix,
            candidate_eligibility=workflow_eligibility(decisions, motion_holds),
            library_types=config.library_type_map,
        )
        items = [item for item in workflow.items if item.state != "missing"]
        if not items:
            return None
        episodes = {
            episode["rel_path"]: episode
            for episode in load_season_episode_progress(connection, items, config=config)
        }
        ids = [item.item_id for item in items]
        sizes = {
            int(item_id): int(size or 0)
            for item_id, size in connection.execute(
                select(library_items.c.id, library_items.c.size_bytes).where(library_items.c.id.in_(ids))
            )
        }
        stages = {
            int(row["library_item_id"]): row
            for row in connection.execute(select(
                staged_artifacts.c.library_item_id,
                staged_artifacts.c.promoted_at,
                staged_artifacts.c.source_size_bytes,
                staged_artifacts.c.archived_source_path,
            ).where(staged_artifacts.c.library_item_id.in_(ids))).mappings()
        }
        kept = {
            int(item_id): str(reason)
            for item_id, reason in connection.execute(
                select(motion_keep_decisions.c.library_item_id, motion_keep_decisions.c.reason_code)
                .join(library_items, motion_keep_decisions.c.library_item_id == library_items.c.id)
                .where(library_items.c.id.in_(ids), motion_keep_decisions.c.source_fingerprint == library_item_source_sql())
            )
        }
        removed = confirmed_originals_removed_item_ids(connection)
        lifecycle = scope_lifecycle_payload_from_decisions(scope.prefix, decisions, motion_holds)
        sound_facts = sound(connection, scope.prefix) if scope.domain == "tv" else None
    files = []
    for item in items:
        episode = episodes.get(item.rel_path) or {}
        stage = stages.get(item.item_id) or {}
        replaced = stage.get("promoted_at") is not None
        name = PurePosixPath(item.rel_path).name
        number = _EPISODE_NUMBER.search(name)
        files.append({
            "library_item_id": item.item_id,
            "rel_path": item.rel_path,
            "name": name,
            "season": int(number.group(1)) if number else None,
            "episode": int(number.group(2)) if number else None,
            "stage": episode.get("stage") or "not_started",
            "validation": episode.get("validation"),
            "wait_reason": episode.get("wait_reason"),
            "kept_reason": kept.get(item.item_id),
            "held_reason": "motion_pattern" if item.motion_held else None,
            "percent_complete": episode.get("percent_complete"),
            "size_question": episode.get("size_question"),
            "bytes": sizes.get(item.item_id, 0),
            "original_bytes": int(stage.get("source_size_bytes") or 0) if replaced else None,
            "original_backup": replaced and bool(stage.get("archived_source_path")) and item.item_id not in removed,
        })
    files.sort(key=lambda row: (row["episode"] is None, row["season"] or 0, row["episode"] or 0, row["name"]))
    return {
        "prefix": scope.prefix,
        "domain": scope.domain,
        "kind": scope.kind,
        **_identity(scope),
        "title": scope.title,
        "parent_prefix": scope.parent_prefix,
        "files": files,
        "sound": _sound(sound_facts),
        "still_airing": _still_airing(lifecycle) if scope.domain == "tv" else None,
    }


def _identity(scope: MediaScope) -> dict[str, Any]:
    """The show and season a page is about: a season names both, a whole show names only itself."""
    if scope.kind == "tv_season":
        return season_identity(scope.prefix)
    if scope.kind == "tv_series":
        return {"show": scope.title, "show_prefix": scope.prefix, "season": None}
    return {"show": None, "season": None}


def _sound(facts: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not facts:
        return None
    return {
        "show_prefix": facts.get("show_prefix"),
        "current": facts.get("current"),
        "options": [option.get("key") for option in facts.get("options") or []],
    }


def _still_airing(lifecycle: Mapping[str, Any]) -> dict[str, Any] | None:
    if not lifecycle.get("series_prefix"):
        return None
    return {
        "series_prefix": lifecycle["series_prefix"],
        "mode": lifecycle.get("policy_mode") or "auto",
        "airing": {"active": True, "ended": False}.get(str(lifecycle.get("provider_state") or "")),
    }


class PlayerUnavailable(RuntimeError):
    """The file cannot be prepared for playing now; ``code`` says why for the app to word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def player_file(config: MediaforceConfig, library_item_id: int, side: str) -> Path | None:
    """The episode as MP4 for the app's player, remuxed on first request; None when that side is not available."""
    if side not in PLAYER_SIDES:
        return None
    with open_readonly_db(config.paths.db_path) as connection:
        item = connection.execute(
            select(library_items.c.source_path).where(library_items.c.id == library_item_id)
        ).scalar_one_or_none()
        stage = connection.execute(
            select(staged_artifacts.c.promoted_at, staged_artifacts.c.archived_source_path)
            .where(staged_artifacts.c.library_item_id == library_item_id)
        ).one_or_none()
    if item is None:
        return None
    promoted_at, backup = stage if stage is not None else (None, None)
    sources = _player_sources(Path(str(item)), Path(str(backup)) if backup else None, replaced=promoted_at is not None)
    source = sources.get(side)
    if source is None:
        return None
    stat = source.stat()
    cache = config.paths.web_state_dir / "player"
    target = _cached_name(cache, library_item_id, side, source)
    if target.is_file():
        return target
    # A second request for the same file waits here and then reads the cache.
    with _PLAYER_PREPARATION:
        if not target.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            # The other side of the same file stays, so a comparison never evicts its own half.
            pair = [
                path for other, other_source in sources.items() if other != side
                if (path := _cached_name(cache, library_item_id, other, other_source)).is_file()
            ]
            if stat.st_size + sum(path.stat().st_size for path in pair) > PLAYER_CACHE_BYTES:
                raise PlayerUnavailable("too_large", f"{source.name} is too large to prepare for playing.")
            _trim_cache(cache, keep={target, *pair}, incoming_bytes=stat.st_size)
            if shutil.disk_usage(cache).free < stat.st_size + PLAYER_FREE_MARGIN_BYTES:
                raise PlayerUnavailable("no_room", f"Not enough free space to prepare {source.name} for playing.")
            _remux(source, target)
    return target


def _player_sources(library_file: Path, backup: Path | None, *, replaced: bool) -> dict[str, Path]:
    """Each side that can play now: a replaced file has its compressed copy and maybe its backup."""
    sides: list[tuple[str, Path | None]] = [("compressed", library_file), ("original", backup)] if replaced \
        else [("original", library_file)]
    playable: dict[str, Path] = {}
    for side, path in sides:
        if path is not None and path.is_file():
            playable[side] = path
    return playable


def _cached_name(cache: Path, library_item_id: int, side: str, source: Path) -> Path:
    """Named for the source's size and change time, so a changed file never plays an old copy."""
    stat = source.stat()
    return cache / f"{library_item_id}-{side}-{stat.st_size}-{stat.st_mtime_ns}.mp4"


def _remux(source: Path, target: Path) -> None:
    """Copy the picture as it is and make the sound stereo AAC, which every Mac plays."""
    partial = target.with_name(f".{target.stem}.{uuid4().hex}{PARTIAL_SUFFIX}")
    # The app's player takes HEVC only under its hvc1 tag.
    tag = ["-tag:v", "hvc1"] if _video_codec(source) == "hevc" else []
    command = [
        ffmpeg_binary(), "-nostdin", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *tag, "-c:a", "aac", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart", "-f", "mp4", str(partial),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=PLAYER_REMUX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        partial.unlink(missing_ok=True)
        raise PlayerUnavailable("too_slow", f"Preparing {source.name} for playing took too long.") from error
    if result.returncode != 0 or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise PlayerUnavailable("unreadable", f"Could not prepare {source.name} for playing: {result.stderr.strip()[:300]}")
    partial.replace(target)


def _video_codec(source: Path) -> str:
    try:
        result = subprocess.run(
            [ffprobe_binary(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise PlayerUnavailable("too_slow", f"Reading {source.name} took too long.") from error
    return result.stdout.strip()


def _trim_cache(cache: Path, *, keep: set[Path], incoming_bytes: int) -> None:
    """Keep the newest few files within the byte budget, leaving room for the one about to be made."""
    now = time.time()
    for partial in cache.glob(f"*{PARTIAL_SUFFIX}"):
        # A preparation still running is younger than its timeout; anything older was interrupted.
        if now - partial.stat().st_mtime > PLAYER_REMUX_TIMEOUT_SECONDS:
            partial.unlink(missing_ok=True)
    files = sorted((path for path in cache.glob("*.mp4") if path not in keep),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    total = incoming_bytes + sum(path.stat().st_size for path in keep if path.is_file())
    for index, path in enumerate(files, start=len(keep)):
        total += path.stat().st_size
        if index >= PLAYER_CACHE_KEEP or total > PLAYER_CACHE_BYTES:
            path.unlink(missing_ok=True)
