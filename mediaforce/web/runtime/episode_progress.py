"""Where each episode of a season has got, in the owner's words.

Each episode gets one stage. Its run in the season's latest encode run says what is happening now;
its library and staged state say what already happened or why nothing will.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.encode_queue import list_child_encode_jobs, load_latest_encode_job
from mediaforce.library.candidate_selection import encode_candidate_decisions, workflow_eligibility
from mediaforce.library.media_scopes import resolve_media_scope
from mediaforce.library.staged_integrity import staged_validation_outcome
from mediaforce.library.workflow_state import ItemWorkflowState, build_folder_workflow_state
from mediaforce.web.runtime.encode_runtime import (
    encode_job_rel_paths,
    size_exception_question,
    unfinished_child_reason,
)

# Stages in the order the owner reads them: their part first, then work under way, then what is done.
EPISODE_STAGES = (
    "needs_you",
    "compressing",
    "measuring",
    "getting_ready",
    "checking",
    "waiting",
    "published",
    "kept_original",
    "held",
    "not_started",
)
_RUNNING_PREPARATION_STATES = frozenset({"starting", "staging_source"})
_OWNER_RUN_STATUSES = frozenset({"needs_attention", "failed", "stopped"})
_VALIDATION_DETAILS = {
    "size_held": "much smaller than expected; keep it or make it again",
    "failed": "didn't pass its check",
}


def folder_episodes_payload(config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    """Every episode of one TV season with its stage; other scopes have no episode list."""
    with open_readonly_db(config.paths.db_path) as connection:
        scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
        if scope.kind != "tv_season":
            return {"prefix": scope.prefix, "available": False, "episodes": []}
        decisions = encode_candidate_decisions(connection, config, prefixes=[scope.prefix])
        workflow = build_folder_workflow_state(
            connection,
            scope.prefix,
            candidate_eligibility=workflow_eligibility(decisions),
            library_types=config.library_type_map,
        )
        episodes = load_season_episode_progress(
            connection,
            workflow.items,
            load_latest_encode_job(connection, scope.prefix),
        )
    return {"prefix": scope.prefix, "available": True, "episodes": episodes}


def load_season_episode_progress(
        connection: DBClient,
        item_states: Iterable[ItemWorkflowState],
        latest_job: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    items = [item for item in item_states if item.state != "missing"]
    if not items:
        return []
    runs = _runs_for(connection, latest_job)
    run_by_rel_path: dict[str, dict[str, Any]] = {}
    manifest_items_cache: dict[Path, list[dict[str, Any]] | None] = {}
    for run in runs:
        for rel_path in encode_job_rel_paths(run, manifest_items_cache=manifest_items_cache) or _saved_rel_paths(run):
            # A later run for the same file replaces an earlier one.
            run_by_rel_path[rel_path] = run
    staged = _staged_rows(
        connection,
        [item.item_id for item in items if item.has_staged_output or item.state == "complete"],
    )
    bytes_saved = {item_id: int_value(row["bytes_saved"]) for item_id, row in staged.items()}
    validation = {
        item_id: outcome
        for item_id, row in staged.items()
        if (outcome := staged_validation_outcome(row["validation_json"])) is not None
    }
    return season_episode_progress(items, run_by_rel_path, bytes_saved, validation)


def season_episode_progress(
        items: Iterable[ItemWorkflowState],
        run_by_rel_path: Mapping[str, Mapping[str, Any]],
        bytes_saved: Mapping[int, int],
        validation: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """`validation` names the staged files whose check waits on the owner: "size_held" or "failed"."""
    validation = validation or {}
    episodes = [
        _episode(
            item,
            run_by_rel_path.get(item.rel_path),
            bytes_saved.get(item.item_id),
            validation.get(item.item_id),
        )
        for item in items
        if item.state != "missing"
    ]
    order = {stage: index for index, stage in enumerate(EPISODE_STAGES)}
    return sorted(episodes, key=lambda episode: (order[episode["stage"]], episode["rel_path"]))


def _episode(
        item: ItemWorkflowState,
        run: Mapping[str, Any] | None,
        bytes_saved: int | None,
        validation: str | None,
) -> dict[str, Any]:
    episode: dict[str, Any] = {
        "rel_path": item.rel_path,
        "stage": "not_started",
        "detail": None,
        "percent_complete": None,
        "bytes_saved": None,
        "size_question": None,
        "owner_action": None,
    }
    if item.state == "complete":
        episode["stage"] = "published"
        episode["bytes_saved"] = bytes_saved if bytes_saved and bytes_saved > 0 else None
        return episode
    run_status = str(run.get("status") or "") if run is not None else ""
    if run is not None and run_status == "running":
        progress = object_dict(run.get("progress"))
        state = str(progress.get("progress_state") or "").strip().lower()
        if state == "quality_search":
            episode["stage"] = "measuring"
        elif state in _RUNNING_PREPARATION_STATES:
            episode["stage"] = "getting_ready"
        else:
            episode["stage"] = "compressing"
            percent = float_value(progress.get("percent_complete"))
            episode["percent_complete"] = max(0, min(100, round(percent))) if percent > 0 else None
        return episode
    if run is not None and run_status in _OWNER_RUN_STATUSES | {"queued", "retry_backoff"}:
        reason, label, needs_owner = unfinished_child_reason(run)
        if reason == "size_exception_declined":
            episode["stage"] = "kept_original"
        elif needs_owner or run_status in _OWNER_RUN_STATUSES:
            episode["stage"] = "needs_you"
            episode["size_question"] = size_exception_question(run)
        else:
            episode["stage"] = "waiting"
        episode["detail"] = label
        return episode
    if validation is not None:
        episode["stage"] = "needs_you"
        episode["detail"] = _VALIDATION_DETAILS[validation]
        episode["owner_action"] = "keep_or_remake" if validation == "size_held" else None
        return episode
    if item.state in {"ready_to_validate", "ready_to_promote"} or (run is not None and run_status == "completed"):
        episode["stage"] = "checking"
    elif item.state == "encoding":
        episode["stage"] = "compressing"
    elif item.state == "held":
        episode["stage"] = "held"
        episode["detail"] = item.blocker
    elif item.state == "blocked":
        episode["stage"] = "needs_you"
        episode["detail"] = item.blocker
    return episode


def _runs_for(connection: DBClient, latest_job: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if latest_job is None:
        return []
    if str(latest_job.get("job_kind") or "single") == "folder":
        return list_child_encode_jobs(connection, str(latest_job["job_id"]))
    return [dict(latest_job)]


def _saved_rel_paths(run: Mapping[str, Any]) -> list[str]:
    """The file a one-file run recorded in its own progress, for when its manifest is gone."""
    progress = object_dict(run.get("progress"))
    rel_path = str(
        object_dict(progress.get("failure_analysis")).get("item_rel_path")
        or progress.get("current_item_rel_path")
        or ""
    ).strip()
    return [rel_path] if rel_path else []


def _staged_rows(connection: DBClient, item_ids: list[int]) -> dict[int, Mapping[str, Any]]:
    if not item_ids:
        return {}
    rows = connection.execute(
        select(
            staged_artifacts.c.library_item_id,
            staged_artifacts.c.bytes_saved,
            staged_artifacts.c.validation_json,
        )
        .where(staged_artifacts.c.library_item_id.in_(item_ids))
    ).mappings().fetchall()
    return {int(row["library_item_id"]): row for row in rows}
