"""Facts for the Mac app's page for one show season, movie or folder, and its episode player.

The page shows every file with where it has got, its size before and after, and whether its original
backup is still there to compare against. The server sends states and codes; the app words them. The
player streams each file a few seconds at a time; see player_stream.
"""

import json
import re
from time import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.media_storage import probe_media_path
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import library_items, motion_keep_decisions, staged_artifacts
from mediaforce.core.type_defs import int_value
from mediaforce.library.candidate_selection import encode_candidate_decisions, scope_lifecycle_payload_from_decisions, \
    workflow_eligibility
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope
from mediaforce.library.motion_holds import compressible_motion_files, queue_motion_holds
from mediaforce.library.motion_keep_state import library_item_source_sql
from mediaforce.library.staged_validation import staged_validation_outcome
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.core.db_tables import season_redos
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.web.runtime.season_redo import cartoon_redo_approval
from mediaforce.web.runtime.kind_approvals import movie_queue_facts
from mediaforce.web.runtime.production_approval import LoadCalibrationState
from mediaforce.web.runtime.episode_progress import load_season_episode_progress

PLAYER_SIDES = ("compressed", "original")
_EPISODE_NUMBER = re.compile(r"[sS](\d+)[eE](\d+)")


def item_payload(
        config: MediaforceConfig,
        prefix: str,
        *,
        sound: Callable[[str], dict[str, Any] | None],
        load_calibration_state: LoadCalibrationState | None = None,
        computers: Callable[[DBClient, MediaforceConfig], list[dict[str, Any]]] | None = None,
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
        queue = (movie_queue_facts(connection, config, scope.prefix, ids, load_calibration_state, now=time(),
                                  computers=computers(connection, config) if computers else ())
                 if scope.kind in {"movie_title", "movie_file"} and load_calibration_state is not None else None)
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
                staged_artifacts.c.staging_path,
                staged_artifacts.c.staging_size_bytes,
                staged_artifacts.c.validation_json,
            ).where(staged_artifacts.c.library_item_id.in_(ids))).mappings()
        }
        redo_row = connection.execute(select(season_redos).where(season_redos.c.prefix == scope.prefix)).mappings().first()
        redo = redo_row["state"] if redo_row else None
        remaining = json.loads(redo_row["remaining_json"]) if redo_row and redo_row["remaining_json"] else {}
        redo_active = bool(redo == "running" and load_active_encode_jobs_for_prefix(connection, scope.prefix))
        kept = {
            int(item_id): str(reason)
            for item_id, reason in connection.execute(
                select(motion_keep_decisions.c.library_item_id, motion_keep_decisions.c.reason_code)
                .join(library_items, motion_keep_decisions.c.library_item_id == library_items.c.id)
                .where(library_items.c.id.in_(ids), motion_keep_decisions.c.source_fingerprint == library_item_source_sql())
            )
        }
        motion_choices = motion_holds.as_is | compressible_motion_files(connection, list(kept)).keys()
        removed = confirmed_originals_removed_item_ids(connection)
        lifecycle = scope_lifecycle_payload_from_decisions(scope.prefix, decisions, motion_holds)
        sound_facts = sound(scope.prefix) if scope.domain == "tv" else None
    movie_decisions = {decision.item_id: decision for decision in decisions} if scope.domain == "movie" else {}
    can_compress = scope.root in config.source_root_map
    files = []
    for item in items:
        episode = episodes.get(item.rel_path) or {}
        stage = stages.get(item.item_id) or {}
        replaced = stage.get("promoted_at") is not None
        name = PurePosixPath(item.rel_path).name
        number = _EPISODE_NUMBER.search(name)
        wait_reason = episode.get("wait_reason")
        held_reason = "motion_pattern" if item.motion_held else None
        movie = movie_decisions.get(item.item_id)
        if movie is not None:
            if wait_reason == {"code": "settings_block"} and movie.target_size_blocker is not None:
                wait_reason = {"code": movie.target_size_blocker.code}
            if episode.get("stage") == "held" and not movie.production_included:
                held_reason = f"movie_{movie.media_role}" if can_compress else "browse_only"
        files.append({
            "library_item_id": item.item_id,
            "rel_path": item.rel_path,
            "name": name,
            "season": int(number.group(1)) if number else None,
            "episode": int(number.group(2)) if number else None,
            "stage": episode.get("stage") or "not_started",
            "validation": episode.get("validation"),
            "wait_reason": wait_reason,
            "additional_wait_reasons": episode.get("additional_wait_reasons", []),
            "kept_reason": kept.get(item.item_id),
            "can_compress_as_is": item.item_id in motion_choices,
            "held_reason": held_reason,
            "redo_wait": (remaining.get(str(item.item_id)) or {}).get("reason"),
            "percent_complete": episode.get("percent_complete"),
            "size_question": episode.get("size_question"),
            "bytes": sizes.get(item.item_id, 0),
            "original_bytes": int(stage.get("source_size_bytes") or 0) if replaced else None,
            "original_backup": replaced and bool(stage.get("archived_source_path")) and item.item_id not in removed,
            "held_copy": _held_copy_facts(config, stage),
        })
    files.sort(key=lambda row: (row["episode"] is None, row["season"] or 0, row["episode"] or 0, row["name"]))
    return {
        "season_redo": ({"state": redo, "active": redo_active, "approved": cartoon_redo_approval(config, scope.prefix) is not None} if redo else None),
        "prefix": scope.prefix,
        "domain": scope.domain,
        "kind": scope.kind,
        **_identity(scope),
        "title": scope.title,
        "parent_prefix": scope.parent_prefix,
        "files": files,
        "sound": _sound(sound_facts),
        "movie_queue": queue,
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


def player_source(config: MediaforceConfig, library_item_id: int, side: str) -> Path | None:
    if side not in PLAYER_SIDES:
        return None
    with open_readonly_db(config.paths.db_path) as connection:
        item = connection.execute(
            select(library_items.c.source_path).where(library_items.c.id == library_item_id)
        ).scalar_one_or_none()
        stage = connection.execute(
            select(
                staged_artifacts.c.promoted_at,
                staged_artifacts.c.archived_source_path,
                staged_artifacts.c.staging_path,
                staged_artifacts.c.validation_json,
            ).where(staged_artifacts.c.library_item_id == library_item_id)
        ).mappings().one_or_none() or {}
    if item is None:
        return None
    return _player_sources(config, Path(str(item)), stage).get(side)


def _player_sources(config: MediaforceConfig, library_file: Path, stage: Mapping[Any, Any]) -> dict[str, Path]:
    """Each side that can play now: a held copy against the file as it is, or a replaced file's copy and backup."""
    if _waits_on_held_copy(stage):
        sides: dict[str, Path | None] = {"compressed": _scoped_held_copy(config, stage), "original": library_file}
    elif stage.get("promoted_at") is not None:
        backup = stage.get("archived_source_path")
        sides = {"compressed": library_file, "original": Path(str(backup)) if backup else None}
    else:
        sides = {"original": library_file}
    playable: dict[str, Path] = {}
    for side, path in sides.items():
        if path is not None and probe_media_path(path).is_file:
            playable[side] = path
    return playable


def _waits_on_held_copy(stage: Mapping[Any, Any]) -> bool:
    """An unpublished copy held only because it came out far smaller than predicted."""
    return stage.get("promoted_at") is None and staged_validation_outcome(stage.get("validation_json")) == "size_held"


def _scoped_held_copy(config: MediaforceConfig, stage: Mapping[Any, Any]) -> Path | None:
    staging_path = str(stage.get("staging_path") or "")
    if not staging_path:
        return None
    path = Path(staging_path)
    return path if _inside_staging_root(config, path) else None


def _inside_staging_root(config: MediaforceConfig, path: Path) -> bool:
    resolved = path.resolve()
    roots = {config.staging_root_for_host(host).expanduser().resolve() for host in [None, *config.remote_hosts]}
    return any(resolved.is_relative_to(root) for root in roots)


def _held_copy_facts(config: MediaforceConfig, stage: Mapping[Any, Any]) -> dict[str, Any] | None:
    """For a held file, its unpublished copy's size and whether the player can still show it."""
    if not _waits_on_held_copy(stage):
        return None
    size = stage.get("staging_size_bytes")
    path = _scoped_held_copy(config, stage)
    probe = probe_media_path(path) if path is not None else None
    problem = probe.problem if probe is not None else None
    return {"bytes": None if size is None else int_value(size), "present": probe is not None and probe.is_file,
            **({"waiting_reason": problem.reason} if problem else {})}
