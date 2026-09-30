"""The owner's answer to one file whose quality floor needs more than its size goal allows.

A yes records the same item-local size exception the automatic retry would, with the owner's
approval as added evidence, and queues only that file again. A no keeps the original and leaves
the file listed. Nothing else in its folder waits on the answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.encode_queue import load_encode_job, save_encode_job
from mediaforce.web.runtime.encode_runtime import apply_quality_floor_size_exception, size_exception_question
from mediaforce.web.runtime.folder_actions import active_encode_library_item_ids

SyncParentFn = Callable[[DBClient, dict[str, Any]], Any]

NOT_WAITING_MESSAGE = "This file is no longer waiting for a size decision."


def decide_size_exception(
        connection: DBClient,
        job_id: str,
        *,
        allow: bool,
        now_iso: Callable[[], str],
        sync_parent: SyncParentFn,
) -> dict[str, Any]:
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        result = _decide(connection, job_id, allow=allow, now_iso=now_iso, sync_parent=sync_parent)
    except Exception:
        connection.rollback()
        raise
    if result["ok"]:
        connection.commit()
    else:
        connection.rollback()
    return result


def _decide(
        connection: DBClient,
        job_id: str,
        *,
        allow: bool,
        now_iso: Callable[[], str],
        sync_parent: SyncParentFn,
) -> dict[str, Any]:
    child = load_encode_job(connection, str(job_id or "").strip())
    question = size_exception_question(child) if child is not None else None
    if child is None or question is None:
        return {"ok": False, "message": NOT_WAITING_MESSAGE}
    progress = dict(object_dict(child.get("progress")))
    analysis = dict(object_dict(progress.get("failure_analysis")))
    library_item_id = _library_item_id(child, int_value(analysis.get("manifest_index")))
    name = Path(question["rel_path"]).name
    if allow:
        active_item_ids = active_encode_library_item_ids(connection, str(child.get("prefix") or ""))
        if active_item_ids is None or library_item_id in active_item_ids:
            return {
                "ok": False,
                "message": f"{name} is already queued again, so this answer would make it twice.",
            }
        if not apply_quality_floor_size_exception(child, analysis, owner_approved=True):
            return {
                "ok": False,
                "message": (
                    f"Mediaforce could not record the larger size for {name}. "
                    "Confirm this show's size choice, then answer again."
                ),
            }
    now = now_iso()
    decision = {
        "answer": "allow" if allow else "keep_original",
        "recorded_at": now,
        "goal_bytes": question["goal_bytes"],
        "smallest_quality_safe_bytes": question["smallest_quality_safe_bytes"],
    }
    analysis["owner_size_decision"] = decision
    progress["failure_analysis"] = analysis
    child["progress"] = progress
    child["updated_at"] = now
    if allow:
        child.update(
            {
                "status": "queued",
                "host": {},
                "process_pid": None,
                "leased_at": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "worker_id": None,
                "schedule_close_deadline_at": None,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": None,
                "finished_at": None,
            }
        )
    save_encode_job(connection, child)
    if library_item_id > 0:
        connection.execute(
            item_events.insert().values(
                library_item_id=library_item_id,
                created_at=now,
                event_type="owner_size_decision",
                details_json=json.dumps({**decision, "job_id": child["job_id"]}, sort_keys=True),
            )
        )
    sync_parent(connection, child)
    return {
        "ok": True,
        "action": "size_exception_allowed" if allow else "size_exception_declined",
        "job_id": child["job_id"],
        "rel_path": question["rel_path"],
        "message": (
            f"{name} will be made again at the larger size."
            if allow
            else f"{name} keeps its original file."
        ),
    }


def _library_item_id(job: dict[str, Any], index: int) -> int:
    try:
        manifest = json.loads(Path(str(job.get("manifest_path") or "")).read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    items = object_list(object_dict(manifest).get("items"))
    if not 0 <= index < len(items):
        return 0
    return int_value(object_dict(items[index]).get("library_item_id"))
