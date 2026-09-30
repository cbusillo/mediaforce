"""The owner's answer for one file that came out far smaller than its sample predicted.

Validation holds such a file instead of letting it be published (#633). Keeping it records the owner's
decision and checks the file again, so it can be replaced like any other checked file. Making it again
removes the finished file and queues only that file under the mode its run was queued with.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, staged_artifacts
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.encoding.staging import partial_output_path
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.web.runtime.encode_runtime import remove_stale_staging_path
from mediaforce.web.runtime.host_runtime import host_config_for_key
from mediaforce.web.runtime.production_holds import MODE_FOLDER

NOT_HELD_MESSAGE = "This file is no longer waiting for a decision about its size."

ValidateItemsFn = Callable[[str, Collection[int]], dict[str, Any]]
QueueItemsFn = Callable[[str, str, Collection[int]], dict[str, Any]]


def decide_size_held_file(
        config: MediaforceConfig,
        prefix: str,
        library_item_id: int,
        *,
        keep: bool,
        now_iso: Callable[[], str],
        validate_items: ValidateItemsFn,
        queue_items: QueueItemsFn,
) -> dict[str, Any]:
    """Keep a held file (then check it again) or make it again; only this file is touched."""
    queue_mode = MODE_FOLDER
    run_prefix = prefix
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(
            select(staged_artifacts, library_items.c.rel_path, library_items.c.status.label("library_status"))
            .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
            .where(staged_artifacts.c.library_item_id == int(library_item_id))
        ).mappings().fetchone()
        validation = _stored_validation(row)
        size_prediction = object_dict(validation.get("size_prediction"))
        if (
                row is None
                or row["promoted_at"] is not None
                or not path_matches_scope(str(row["rel_path"] or ""), prefix)
                or not size_prediction.get("held")
        ):
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        name = Path(str(row["rel_path"])).name
        now = now_iso()
        if keep:
            validation["size_prediction"] = {**size_prediction, "owner_kept_at": now, "held": False}
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == int(library_item_id))
                .values(validation_json=json.dumps(validation, separators=(",", ":")), updated_at=now)
            )
            _record_decision(connection, int(library_item_id), "keep", size_prediction, now)
        else:
            queue_mode = _run_queue_mode(row["manifest_path"])
            # A show-level run (older seasons, say) must be queued at its own scope, not the page's.
            run_prefix = str(
                connection.execute(
                    select(encode_jobs.c.prefix).where(encode_jobs.c.job_id == str(row["encode_job_id"] or ""))
                ).scalar_one_or_none()
                or prefix
            )
            # A run still working on this scope would take over the new request (or refuse it) after the
            # finished file was already gone, so nothing is removed until that run is done.
            if load_active_encode_jobs_for_prefix(connection, run_prefix):
                return {
                    "ok": False,
                    "message": f"Mediaforce is still compressing here. Make {name} again once that run finishes.",
                }
            if not _remove_finished_output(config, row):
                return {
                    "ok": False,
                    "message": f"Mediaforce could not remove the finished {name} yet. Nothing changed; try again.",
                }
            connection.execute(
                delete(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == int(library_item_id))
                .where(staged_artifacts.c.promoted_at.is_(None))
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == int(library_item_id))
                .values(status="planned", updated_at=now)
            )
            _record_decision(connection, int(library_item_id), "remake", size_prediction, now)
    if keep:
        checked = validate_items(prefix, [int(library_item_id)])
        passed = bool(checked.get("ok")) and int(checked.get("validated_count") or 0) > 0
        return {
            "ok": True,
            "action": "size_held_kept",
            "message": (
                f"Kept {name}. It passed its check and can be replaced now."
                if passed
                else f"Kept {name}. {str(checked.get('message') or 'Check it again before replacing it.')}"
            ),
        }
    queued = queue_items(run_prefix, queue_mode, [int(library_item_id)])
    return {
        **queued,
        "ok": bool(queued.get("ok")),
        "action": "size_held_remade",
        "message": (
            f"Making {name} again."
            if queued.get("ok")
            else f"Removed the finished {name}, but it could not be queued yet: {queued.get('message') or ''}".strip()
        ),
    }


def _stored_validation(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return object_dict(json.loads(str(row["validation_json"] or "{}")))
    except json.JSONDecodeError:
        return {}


def _run_queue_mode(manifest_path: Any) -> str:
    """The mode the file's run was queued with, so the new encode joins the same way."""
    try:
        manifest = object_dict(json.loads(Path(str(manifest_path or "")).read_text()))
    except (OSError, json.JSONDecodeError):
        return MODE_FOLDER
    return str(object_dict(manifest.get("selection")).get("queue_mode") or MODE_FOLDER)


def _remove_finished_output(config: MediaforceConfig, row: Any) -> bool:
    staging_value = str(row["staging_path"] or "").strip()
    if not staging_value:
        return True
    host_key = str(row["encode_host_key"] or "").strip()
    host = (
        host_config_for_key(config, host_key)
        if host_key
        else {"mode": str(row["encode_host_mode"] or ""), "media_access": str(row["encode_media_access"] or "")}
    )
    staging_path = Path(staging_value)
    removed = remove_stale_staging_path(staging_path, host=host)
    return remove_stale_staging_path(partial_output_path(staging_path), host=host) and removed


def _record_decision(connection: Any, library_item_id: int, answer: str, size_prediction: dict[str, Any], now: str) -> None:
    connection.execute(
        item_events.insert().values(
            library_item_id=library_item_id,
            created_at=now,
            event_type="owner_size_held_decision",
            details_json=json.dumps({"answer": answer, **size_prediction}, sort_keys=True),
        )
    )
