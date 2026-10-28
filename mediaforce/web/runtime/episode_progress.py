"""Where each episode of a season has got, in the owner's words.

Each episode gets one stage. Its run in the season's latest encode run says what is happening now;
its library and staged state say what already happened or why nothing will.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.encode_queue import encode_run_rel_paths, list_child_encode_jobs_for_parents
from mediaforce.library.staged_validation import not_remade_blockers, staged_validation_outcome
from mediaforce.library.workflow_state import ItemWorkflowState
from mediaforce.web.runtime.encode_runtime import KEPT_ORIGINAL_TERMINAL_REASON, size_exception_question, unfinished_child_reason

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
_OWNER_VALIDATIONS = frozenset({"size_held", "failed"})
_VALIDATION_DETAILS = {
    "size_held": "much smaller than expected; keep it or make it again",
    "failed": "didn't pass its check",
    "remaking": "didn't pass its check; being made again",
}


def load_season_episode_progress(
        connection: DBClient,
        item_states: Iterable[ItemWorkflowState],
        runs_newest_first: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Each episode takes its own newest run, so re-running one file never hides another's question."""
    items = [item for item in item_states if item.state != "missing"]
    if not items:
        return []
    # A published episode reads as published whatever its runs say.
    wanted = {item.rel_path for item in items if item.state != "complete"}
    run_by_rel_path: dict[str, dict[str, Any]] = {}
    parents = list(runs_newest_first)
    children = list_child_encode_jobs_for_parents(
        connection,
        [str(parent["job_id"]) for parent in parents if str(parent.get("job_kind") or "") == "folder"],
    )
    for parent in parents:
        if len(run_by_rel_path) == len(wanted):
            break
        parent_runs = children.get(str(parent["job_id"])) or [dict(parent)]
        # Within one run, a file's later part (a retry) replaces its earlier one.
        for run in reversed(parent_runs):
            for rel_path in encode_run_rel_paths(run):
                if rel_path in wanted and rel_path not in run_by_rel_path:
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
    not_remade = {
        item_id: blockers
        for item_id, row in staged.items()
        if (blockers := not_remade_blockers(row["validation_json"])) is not None
    }
    publish_stopped = {
        item_id: state
        for item_id, row in staged.items()
        if (state := _stopped_publish_state(row["validation_json"])) is not None
    }
    return season_episode_progress(items, run_by_rel_path, bytes_saved, validation, not_remade, publish_stopped)


def season_episode_progress(
        items: Iterable[ItemWorkflowState],
        run_by_rel_path: Mapping[str, Mapping[str, Any]],
        bytes_saved: Mapping[int, int],
        validation: Mapping[int, str] | None = None,
        not_remade: Mapping[int, list[str]] | None = None,
        publish_stopped: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """`validation` names each staged file's check outcome: "size_held" or "failed" wait on the owner, "remaking" does not.

    `not_remade` gives, for a failed copy Mediaforce could not make again, the codes for why.
    """
    validation = validation or {}
    not_remade = not_remade or {}
    publish_stopped = publish_stopped or {}
    episodes = [
        _episode(
            item,
            run_by_rel_path.get(item.rel_path),
            bytes_saved.get(item.item_id),
            validation.get(item.item_id),
            not_remade.get(item.item_id),
            publish_stopped.get(item.item_id),
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
        not_remade: list[str] | None,
        publish_stopped: str | None,
) -> dict[str, Any]:
    """One episode's stage; one that waits on the owner says why in ``wait_reason``, a code for the app to word."""
    episode: dict[str, Any] = {
        "rel_path": item.rel_path,
        "stage": "not_started",
        "detail": None,
        "percent_complete": None,
        "bytes_saved": None,
        "size_question": None,
        "owner_action": None,
        "validation": None,
        "wait_reason": None,
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
    if item.state == "kept_original":
        # The owner settled it; a run stopped for that choice is not a question for them.
        episode["stage"] = "kept_original"
        episode["detail"] = item.blocker
        return episode
    if publish_stopped is not None:
        episode["stage"] = "needs_you"
        episode["wait_reason"] = {"code": f"publish_{publish_stopped}"}
        return episode
    if (run is not None and run_status in _OWNER_RUN_STATUSES | {"queued", "retry_backoff"}
            and run.get("terminal_reason") != KEPT_ORIGINAL_TERMINAL_REASON):
        reason, label, needs_owner = unfinished_child_reason(run)
        owner_run = reason != "size_exception_declined" and (needs_owner or run_status in _OWNER_RUN_STATUSES)
        # A run that waits on the owner says less than the copy it left, once that copy has been checked.
        if not (owner_run and validation in _OWNER_VALIDATIONS):
            if reason == "size_exception_declined":
                episode["stage"] = "kept_original"
            elif owner_run:
                episode["stage"] = "needs_you"
                episode["size_question"] = size_exception_question(run)
                episode["wait_reason"] = _run_wait_reason(reason, run)
            else:
                episode["stage"] = "waiting"
            episode["detail"] = label
            return episode
    if validation == "remaking":
        episode["stage"] = "waiting"
        episode["detail"] = _VALIDATION_DETAILS[validation]
        episode["validation"] = validation
        return episode
    if validation is not None:
        episode["stage"] = "needs_you"
        episode["detail"] = _VALIDATION_DETAILS[validation]
        episode["validation"] = validation
        episode["owner_action"] = "keep_or_remake" if validation == "size_held" else None
        episode["wait_reason"] = (
            {"code": "size_held"} if validation == "size_held"
            else {"code": "check_failed"} if not_remade is None
            else {"code": "not_made_again", "blockers": not_remade}
        )
        return episode
    # A finished run says nothing about now; the file's own state does.
    if item.state in {"ready_to_validate", "ready_to_promote"}:
        episode["stage"] = "checking"
    elif item.state == "encoding":
        episode["stage"] = "compressing"
    elif item.state == "held":
        episode["stage"] = "held"
        episode["detail"] = item.blocker
    elif item.state == "blocked":
        episode["stage"] = "needs_you"
        episode["detail"] = item.blocker
        # Its copy finished but is gone, or its library's settings leave it out of the work.
        episode["wait_reason"] = {"code": "copy_missing" if item.status == "encoded" else "settings_block"}
    return episode


def _run_wait_reason(reason: str, run: Mapping[str, Any]) -> dict[str, Any]:
    """The run's reason code, with the computer a restart stopped it on as its own value when the run names one."""
    code = reason.partition(":")[0]
    if code != "restart_stopped":
        return {"code": code}
    return {"code": code, "computer": str(object_dict(run.get("host")).get("label") or "").strip() or None}


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


def _stopped_publish_state(raw_validation: Any) -> str | None:
    try:
        payload = object_dict(json.loads(str(raw_validation or "{}")))
    except json.JSONDecodeError:
        return None
    state = object_dict(payload.get("automatic_publish")).get("state")
    return str(state) if payload.get("passed") is not False and state in {"failed", "unsafe"} else None
