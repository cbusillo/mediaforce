"""Per-file decisions for unpublished outputs that need to be kept or made again."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, run_manifests, staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.encoding.staging import FINAL_SIZE_GOAL_CHECK, partial_output_path
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.library.staged_integrity import staged_validation_outcome
from mediaforce.tuning.calibration_jobs import EXECUTION_ACTIVE_JOB_STATUSES, load_latest_job
from mediaforce.web.runtime.encode_runtime import remove_stale_staging_path
from mediaforce.web.runtime.folder_actions import _final_size_requeue_contract_blocker, _staged_policy_states, \
    staged_requeue_size_blocker
from mediaforce.web.runtime.host_runtime import host_config_for_key
from mediaforce.web.runtime.production_holds import MODE_FOLDER

NOT_HELD_MESSAGE = "This file is no longer waiting for a decision about its size."

ValidateItemsFn = Callable[[str, Collection[int]], dict[str, Any]]
CurrentApprovalFn = Callable[[str], dict[str, Any] | None]
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
        current_approval: CurrentApprovalFn = lambda _prefix: None,
) -> dict[str, Any]:
    """Keep a held file (then check it again) or make it again; only this file is touched."""
    queue_mode = MODE_FOLDER
    run_prefix = prefix
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(
            select(staged_artifacts, library_items.c.rel_path,
                   library_items.c.source_path.label("original_source_path"))
            .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
            .where(staged_artifacts.c.library_item_id == int(library_item_id))
        ).mappings().fetchone()
        validation = _stored_validation(row)
        size_prediction = object_dict(validation.get("size_prediction"))
        if (
                row is None
                or row["promoted_at"] is not None
                or not path_matches_scope(str(row["rel_path"] or ""), prefix)
        ):
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        recovery = staged_remake_details(connection, row, prefix, current_approval=current_approval)
        held = staged_validation_outcome(row["validation_json"]) == "size_held"
        if (keep and not held) or (not keep and recovery is None):
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        if not keep and recovery and recovery["blocked_reason"]:
            return {"ok": False, "message": recovery["blocked_reason"]}
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
            run_prefix, queue_mode, _context_available = _run_context(connection, row, prefix)
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
            _record_decision(connection, int(library_item_id), "remake",
                             {**size_prediction, "recovery_reason": recovery["reason"]}, now)
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


def staged_remake_details(
        connection: DBClient,
        row: Any,
        prefix: str,
        *,
        current_approval: CurrentApprovalFn,
        policy_states: Mapping[int, str | None] | None = None,
) -> dict[str, Any] | None:
    """Offer recovery only for a size-only failure or missing settings history.

    The action calls this again under its write lock before removing the output.
    """
    if row is None or row["promoted_at"] is not None:
        return None
    validation = _stored_validation(row)
    failed = [str(object_dict(check).get("message") or "") for check in object_list(validation.get("checks"))
              if object_dict(check).get("passed") is False]
    held = staged_validation_outcome(row["validation_json"]) == "size_held"
    final_size = validation.get("passed") is False and failed == [FINAL_SIZE_GOAL_CHECK]
    missing_policy = validation.get("passed") is True and (
        policy_states if policy_states is not None else _staged_policy_states(
            connection, {int(row["library_item_id"])}, accepted_policy_hash="",
        )
    ).get(int(row["library_item_id"])) == "season_policy_provenance_missing"
    if not (held or final_size or missing_policy):
        return None
    run_prefix, _mode, context_available = _run_context(connection, row, prefix)
    blocked_reasons: list[str] = []
    approval = current_approval(run_prefix)
    if not held and not context_available:
        blocked_reasons.append("Restore the saved run settings before making this file again. Nothing was removed.")
    else:
        if approval is None:
            blocked_reasons.append("Approve a fresh sample before making this file again.")
        elif final_size and not _readable_manifest(row):
            blocked_reasons.append(
                "Restore the run manifest from a run backup before making this file again; "
                "its size comparison cannot be verified. Nothing was removed."
            )
        elif final_size:
            blocker = _final_size_requeue_contract_blocker({
                "manifest_path": row["manifest_path"],
                "progress": {"failure_analysis": {
                    "kind": "final_size_target_miss",
                    "manifest_index": row["item_index"],
                    "target_size_verification": object_dict(validation.get("final_size_goal")),
                }},
            }, approval)
            if blocker:
                blocked_reasons.append(
                    "Approve a fresh sample with a changed size or quality goal before making this file again."
                )
    queue_blocker = staged_requeue_size_blocker(connection, run_prefix, int(row["library_item_id"]), approval)
    if queue_blocker is not None:
        blocked_reasons.append(
            "This file’s saved size checks still block a new encode. Review its run settings before making it again."
        )
    original = Path(str(row["original_source_path"] or ""))
    staging = Path(str(row["staging_path"] or ""))
    if not original.is_file():
        blocked_reasons.append("Restore access to the original file before making it again. Nothing was removed.")
    elif original.resolve() in {staging.resolve(), partial_output_path(staging).resolve()}:
        blocked_reasons.append(
            "The compressed copy points at the original. Check this file’s paths before making it again."
        )
    sample = load_latest_job(connection, run_prefix)
    if sample and sample.get("status") in EXECUTION_ACTIVE_JOB_STATUSES:
        blocked_reasons.append("Mediaforce is still sampling here. Make this file again once that sample finishes.")
    if load_active_encode_jobs_for_prefix(connection, run_prefix):
        blocked_reasons.append("Mediaforce is still compressing here. Make this file again once that run finishes.")
    return {"reason": "size_held" if held else "final_size" if final_size else "settings_history",
            "blocked_reason": " ".join(blocked_reasons)}


def staged_remake_records(
        connection: DBClient,
        records: list[dict[str, Any]],
        prefix: str,
        *,
        current_approval: CurrentApprovalFn,
) -> list[dict[str, Any]]:
    """Attach per-file action availability to the already paginated integrity rows."""
    ids = [int(record["item_id"]) for record in records if record.get("item_id") is not None]
    rows = {int(row["library_item_id"]): row for row in connection.execute(
        select(staged_artifacts, library_items.c.source_path.label("original_source_path"))
        .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
        .where(staged_artifacts.c.library_item_id.in_(ids))
    ).mappings()}
    policy_states = _staged_policy_states(connection, {
        item_id for item_id, row in rows.items() if _stored_validation(row).get("passed") is True
    }, accepted_policy_hash="")
    approvals: dict[str, dict[str, Any] | None] = {}

    def approval_for_scope(scope: str) -> dict[str, Any] | None:
        if scope not in approvals:
            approvals[scope] = current_approval(scope)
        return approvals[scope]

    for record in records:
        recovery = staged_remake_details(connection, rows.get(record.get("item_id")), prefix,
                                        current_approval=approval_for_scope, policy_states=policy_states)
        if recovery is not None:
            record["remake"] = recovery
    return records


def _stored_validation(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return object_dict(json.loads(str(row["validation_json"] or "{}")))
    except json.JSONDecodeError:
        return {}


def _readable_manifest(row: Any) -> bool:
    try:
        return bool(object_dict(json.loads(Path(str(row["manifest_path"] or "")).read_text())))
    except (OSError, json.JSONDecodeError):
        return False


def _run_context(connection: DBClient, row: Any, prefix: str) -> tuple[str, str, bool]:
    """Use the existing saved selection when the manifest or terminal job is no longer available."""
    try:
        manifest = object_dict(json.loads(Path(str(row["manifest_path"] or "")).read_text()))
        selection = object_dict(manifest.get("selection"))
        available = True
    except (OSError, json.JSONDecodeError):
        stored = connection.execute(select(run_manifests.c.selection_json).where(
            run_manifests.c.run_id == str(row["manifest_run_id"] or "")
        )).scalar_one_or_none()
        try:
            selection = object_dict(json.loads(str(stored))) if stored is not None else {}
            available = stored is not None and bool(selection)
        except json.JSONDecodeError:
            selection, available = {}, False
    lifecycle_override = object_dict(selection.get("lifecycle_override"))
    recorded_prefix = object_dict(selection.get("media_scope")).get("prefix") or lifecycle_override.get("series_prefix")
    run_prefix = str(connection.execute(
        select(encode_jobs.c.prefix).where(encode_jobs.c.job_id == str(row["encode_job_id"] or ""))
    ).scalar_one_or_none() or recorded_prefix or prefix)
    return run_prefix, str(selection.get("queue_mode") or lifecycle_override.get("mode") or MODE_FOLDER), available


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
    if not remove_stale_staging_path(partial_output_path(staging_path), host=host):
        return False
    return remove_stale_staging_path(staging_path, host=host)


def _record_decision(connection: Any, library_item_id: int, answer: str, size_prediction: dict[str, Any], now: str) -> None:
    connection.execute(
        item_events.insert().values(
            library_item_id=library_item_id,
            created_at=now,
            event_type="owner_size_held_decision",
            details_json=json.dumps({"answer": answer, **size_prediction}, sort_keys=True),
        )
    )
