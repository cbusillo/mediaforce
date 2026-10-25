"""Facts for the Mac app's page for one show season, movie or folder, and its episode player.

The page shows every file with where it has got, its size before and after, and whether its original
backup is still there to compare against. The server sends states and codes; the app words them. The
player reads one file at a time as MP4, remuxed once and cached, since the Mac's player cannot open MKV.
"""

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import library_items, motion_keep_decisions, staged_artifacts
from mediaforce.encoding.encode_queue import list_encode_runs_for_prefix
from mediaforce.library.candidate_selection import encode_candidate_decisions, scope_lifecycle_payload_from_decisions, \
    workflow_eligibility
from mediaforce.library.media_scopes import resolve_media_scope
from mediaforce.library.motion_holds import queue_motion_holds
from mediaforce.library.motion_keep_state import library_item_source_sql
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.web.runtime.episode_progress import load_season_episode_progress

PLAYER_SIDES = ("compressed", "original")
# Each remuxed file is about the size of the one it plays, so only the last few stay.
PLAYER_CACHE_KEEP = 6
_EPISODE_NUMBER = re.compile(r"[sS]\d+[eE](\d+)")


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
            for episode in load_season_episode_progress(connection, items, list_encode_runs_for_prefix(connection, scope.prefix))
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
            "episode": int(number.group(1)) if number else None,
            "stage": episode.get("stage") or "not_started",
            "validation": episode.get("validation"),
            "kept_reason": kept.get(item.item_id),
            "held_reason": "motion_pattern" if item.motion_held else None,
            "percent_complete": episode.get("percent_complete"),
            "size_question": episode.get("size_question"),
            "bytes": sizes.get(item.item_id, 0),
            "original_bytes": int(stage.get("source_size_bytes") or 0) if replaced else None,
            "original_backup": replaced and bool(stage.get("archived_source_path")) and item.item_id not in removed,
        })
    files.sort(key=lambda row: (row["episode"] is None, row["episode"] or 0, row["name"]))
    return {
        "prefix": scope.prefix,
        "domain": scope.domain,
        "kind": scope.kind,
        **(season_identity(scope.prefix) if scope.kind == "tv_season" else {"show": None, "season": None}),
        "title": scope.title,
        "parent_prefix": scope.parent_prefix,
        "files": files,
        "sound": _sound(sound_facts),
        "still_airing": _still_airing(lifecycle) if scope.domain == "tv" else None,
    }


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


def player_file(config: MediaforceConfig, library_item_id: int, side: str) -> Path | None:
    """The episode as MP4 for the Mac player, remuxed on first request; None when that side is not available."""
    if side not in PLAYER_SIDES:
        return None
    with open_readonly_db(config.paths.db_path) as connection:
        item = connection.execute(
            select(library_items.c.source_path, library_items.c.content_version_fingerprint, library_items.c.size_bytes)
            .where(library_items.c.id == library_item_id)
        ).mappings().one_or_none()
        stage = connection.execute(
            select(staged_artifacts.c.promoted_at, staged_artifacts.c.archived_source_path)
            .where(staged_artifacts.c.library_item_id == library_item_id)
        ).mappings().one_or_none()
    if item is None:
        return None
    replaced = stage is not None and stage["promoted_at"] is not None
    if side == "compressed":
        source = Path(str(item["source_path"])) if replaced else None
    elif replaced:
        source = Path(str(stage["archived_source_path"])) if stage["archived_source_path"] else None
    else:
        source = Path(str(item["source_path"]))
    if source is None or not source.is_file():
        return None
    stat = source.stat()
    cache = config.paths.web_state_dir / "player"
    target = cache / f"{library_item_id}-{side}-{stat.st_size}-{stat.st_mtime_ns}.mp4"
    if not target.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        _remux(source, target)
        _trim_cache(cache, keep=target)
    return target


def _remux(source: Path, target: Path) -> None:
    """Copy the picture as it is and make the sound stereo AAC, which every Mac plays."""
    partial = target.with_suffix(".partial.mp4")
    # The Mac player takes HEVC only under its hvc1 tag.
    tag = ["-tag:v", "hvc1"] if _video_codec(source) == "hevc" else []
    command = [
        ffmpeg_binary(), "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *tag, "-c:a", "aac", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart", str(partial),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Could not prepare {source.name} for playing: {result.stderr.strip()[:300]}")
    partial.replace(target)


def _video_codec(source: Path) -> str:
    result = subprocess.run(
        [ffprobe_binary(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _trim_cache(cache: Path, *, keep: Path) -> None:
    files = sorted((path for path in cache.glob("*.mp4") if path != keep and not path.name.endswith(".partial.mp4")),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[PLAYER_CACHE_KEEP - 1:]:
        path.unlink(missing_ok=True)
