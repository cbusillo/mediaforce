"""Per-file decisions for unpublished outputs that need to be kept or made again."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping
from functools import cache
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, motion_keep_decisions, \
    run_manifests, staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.encoding.staging import partial_output_path
from mediaforce.library.evidence_state import library_item_source_fingerprint
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.library.motion_keep_state import kept_original_clause
from mediaforce.library.remake_intents import (
    INTENT_KEY,
    finish_remake_intents,
    remake_intent,
    requested_copy_is_present,
    save_remake_intent,
    stored_validation as _stored_validation,
)
from mediaforce.library.staged_validation import missed_only_final_size, staged_validation_outcome
from mediaforce.tuning.calibration_jobs import EXECUTION_ACTIVE_JOB_STATUSES, load_latest_job
from mediaforce.web.runtime.encode_runtime import (
    remove_stale_staging_path, staged_output_cleanup_blocker, staged_output_cleanup_refusal,
    staged_output_cleanup_target,
)
from mediaforce.web.runtime.ambiguous_motion import _legacy_queue_mode
from mediaforce.web.runtime.folder_actions import _final_size_requeue_contract_blocker, staged_requeue_size_blocker, \
    staged_run_items
from mediaforce.web.runtime.manifest_reads import ManifestReader, read_manifest
from mediaforce.web.runtime.motion_keep import KEPT_ORIGINAL_EVENT

logger = logging.getLogger(__name__)

NOT_HELD_MESSAGE = "This file is no longer waiting for a decision about its size."
CHECK_FAILED_KEEP_REASON = "check_failed"
# The copy passed every check but its size: the same settings make the same file, so its original is kept (Q948).
OVER_SIZE_KEEP_REASON = "over_size"

ValidateItemsFn = Callable[[str, Collection[int]], dict[str, Any]]
CurrentApprovalFn = Callable[[str], dict[str, Any] | None]
QueueItemsFn = Callable[[str, str, Collection[int], dict[str, Any]], dict[str, Any]]


def decide_size_held_file(
        config: MediaforceConfig,
        prefix: str,
        library_item_id: int | Collection[int],
        *,
        keep: bool,
        now_iso: Callable[[], str],
        validate_items: ValidateItemsFn,
        queue_items: QueueItemsFn,
        current_approval: CurrentApprovalFn = lambda _prefix: None,
) -> dict[str, Any]:
    """Keep one held file or remake the named files, rechecking each before removal."""
    if not keep:
        return remake_staged_files(
            config, prefix, [library_item_id] if isinstance(library_item_id, int) else library_item_id,
            now_iso=now_iso, queue_items=queue_items, current_approval=current_approval,
        )
    if not isinstance(library_item_id, int):
        return {"ok": False, "message": "Choose one file to keep."}
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(
            select(staged_artifacts, library_items.c.rel_path,
                   library_items.c.source_path.label("original_source_path"),
                   kept_original_clause(library_items.c.id).label("kept_original"))
            .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
            .where(staged_artifacts.c.library_item_id == int(library_item_id))
        ).mappings().fetchone()
        validation = _stored_validation(row)
        size_prediction = object_dict(validation.get("size_prediction"))
        if (
                row is None
                or row["promoted_at"] is not None
                or row["kept_original"]
                or not path_matches_scope(str(row["rel_path"] or ""), prefix)
        ):
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        held = staged_validation_outcome(row["validation_json"]) == "size_held"
        final_size_miss = missed_only_final_size(validation)
        if not held and not final_size_miss:
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        intent = remake_intent(row)
        if intent and not requested_copy_is_present(row):
            return {"ok": False, "message": "This file is waiting to be made again. Retry Make again with its saved settings."}
        name = Path(str(row["rel_path"])).name
        now = now_iso()
        validation.pop(INTENT_KEY, None)
        if held:
            validation["size_prediction"] = {**size_prediction, "owner_kept_at": now, "held": False}
        if final_size_miss:
            # Bound to this copy's size, so a later remake must meet its goal again.
            validation["final_size_goal"] = {**object_dict(validation.get("final_size_goal")), "owner_kept_at": now,
                                             "owner_kept_bytes": int(row["staging_size_bytes"] or 0)}
        connection.execute(
            update(staged_artifacts)
            .where(staged_artifacts.c.library_item_id == int(library_item_id))
            .values(validation_json=json.dumps(validation, separators=(",", ":")), updated_at=now)
        )
        _record_decision(connection, int(library_item_id), "keep", size_prediction, now)
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


def remake_staged_files(
        config: MediaforceConfig,
        prefix: str,
        library_item_ids: Collection[int],
        *,
        now_iso: Callable[[], str],
        queue_items: QueueItemsFn,
        current_approval: CurrentApprovalFn,
        retry_failed_check: bool = False,
) -> dict[str, Any]:
    """Recheck requested files independently and queue compatible remakes together.

    ``retry_failed_check`` lets the automatic publisher remake a copy that failed a check other than its size goal.
    """
    groups: dict[tuple[str, str, str], list[int]] = {}
    left_out: list[dict[str, Any]] = []
    removed: list[int] = []
    names: dict[int, str] = {}
    manifest_reader = cache(read_manifest)
    for item_id in dict.fromkeys(library_item_ids):
        names[item_id] = "Unavailable file"
        request_saved = False
        try:
            with open_db(config.paths.db_path) as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                row = _load_remake_row(connection, item_id)
                names[item_id] = Path(str(row["rel_path"])).name if row is not None else "Unavailable file"
                recovery = None
                if row is not None and path_matches_scope(str(row["rel_path"] or ""), prefix):
                    recovery = staged_remake_details(connection, row, prefix, current_approval=current_approval,
                                                    manifest_reader=manifest_reader, retry_failed_check=retry_failed_check)
                reason = NOT_HELD_MESSAGE if recovery is None else str(recovery["blocked_reason"])
                if recovery is None or reason:
                    left_out.append({"library_item_id": item_id, "name": names[item_id], "reason": reason,
                                     "codes": _refusal_codes(recovery),
                                     "busy": bool(recovery and recovery["busy"])})
                    continue
                intent = remake_intent(row)
                code, reason = "", ""
                if row is not None and intent.get("state") != "removed":
                    code, reason = staged_output_cleanup_refusal(connection, config, row)
                if reason:
                    # Work that still owns the copy clears by itself; a missing path or computer setting needs someone.
                    left_out.append({"library_item_id": item_id, "name": names[item_id], "reason": reason,
                                     "codes": [code], "busy": code == "copy_in_use"})
                    continue
                if not intent or requested_copy_is_present(row):
                    run_prefix, mode, _available = _run_context(connection, row, prefix, manifest_reader=manifest_reader)
                    intent = {"prefix": run_prefix, "mode": mode, "approval": recovery["approval"],
                              "reason": recovery["reason"], "state": "requested"}
                    save_remake_intent(connection, row, intent, now=now_iso())
                    request_saved = True
            # The request commits before unlink; custody and eligibility are rechecked in the removal transaction.
            with open_db(config.paths.db_path) as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                row = _load_remake_row(connection, item_id)
                recovery = staged_remake_details(connection, row, prefix, current_approval=current_approval,
                                                manifest_reader=manifest_reader, retry_failed_check=retry_failed_check)
                reason = NOT_HELD_MESSAGE if recovery is None else str(recovery["blocked_reason"])
                codes = _refusal_codes(recovery)
                if not reason and remake_intent(row) != intent:
                    reason, codes = "This file’s saved request changed. Refresh it before retrying.", ["request_changed"]
                if not reason and recovery is not None and recovery["approval"] != intent["approval"]:
                    reason = "The approved settings changed before removal. Refresh before retrying. Nothing was removed."
                    codes = ["approval_changed"]
                busy = bool(recovery and recovery["busy"])
                if not reason and intent.get("state") != "removed" and not _remove_finished_output(config, row):
                    reason, busy = "Mediaforce could not remove the finished copy yet. Try again.", True
                    codes = ["copy_not_removed"]
                if recovery is None or reason:
                    if request_saved and remake_intent(row) == intent:
                        save_remake_intent(connection, row, {}, now=now_iso())
                    left_out.append({"library_item_id": item_id, "name": names[item_id], "reason": reason,
                                     "codes": codes, "busy": busy})
                    continue
                removed.append(item_id)
                run_prefix, mode = str(intent["prefix"]), str(intent["mode"])
                if intent.get("state") != "removed":
                    now = now_iso()
                    save_remake_intent(connection, row, {**intent, "state": "removed"}, now=now)
                    connection.execute(update(library_items).where(library_items.c.id == item_id)
                                       .values(status="planned", updated_at=now))
                    _record_decision(connection, item_id, "remake", {
                        **object_dict(_stored_validation(row).get("size_prediction")),
                        "recovery_reason": recovery["reason"],
                    }, now)
            groups.setdefault((run_prefix, mode, json.dumps(intent["approval"], sort_keys=True)), []).append(item_id)
        except Exception:
            logger.exception("Could not finish remake recovery for library item %s", item_id)
            left_out.append({"library_item_id": item_id, "name": names[item_id],
                             "reason": ("Finished copy removed, but recovery was not saved. "
                                        "Use Make again to retry with its saved settings." if item_id in removed else
                                        "Could not finish this file’s recovery. Use Make again to retry."),
                             "codes": ["recovery_not_saved" if item_id in removed else "recovery_failed"]})
    runs: list[dict[str, Any]] = []
    queued_ids: list[int] = []
    for (run_prefix, mode, saved_approval), item_ids in groups.items():
        approval = object_dict(json.loads(saved_approval))
        queue_failed = False
        try:
            result = queue_items(run_prefix, mode, item_ids, approval)
        except Exception:
            logger.exception("Could not queue remade files for %s in %s mode", run_prefix, mode)
            result = {"ok": False, "message": "Queueing failed. Use Make again to retry with its saved settings."}
            queue_failed = True
        runs.append(result)
        queue_left_out = object_list(result.get("left_out"))
        excluded_ids = {file.get("library_item_id") for file in queue_left_out}
        accepted_ids = set(result.get("queued_library_item_ids", item_ids if result.get("ok") else []))
        for item_id in item_ids:
            if result.get("ok") and item_id in accepted_ids and item_id not in excluded_ids:
                queued_ids.append(item_id)
            else:
                fallback = ("This file was not accepted by the queue. Check whether another run includes it, "
                            "or review its current settings." if result.get("ok")
                            else str(result.get("message") or "Try queueing again."))
                reason = next((str(file.get("reason")) for file in queue_left_out
                               if file.get("library_item_id") == item_id), fallback)
                code = ("queue_failed" if queue_failed
                        else "queue_excluded" if result.get("ok") or item_id in excluded_ids else "queue_refused")
                left_out.append({"library_item_id": item_id, "name": names[item_id],
                                 "reason": f"Finished copy removed, but not queued: {reason} "
                                           "Use Make again to retry only this file with its saved settings once this clears.",
                                 "codes": [code]})
        accepted = [item_id for item_id in item_ids if item_id in queued_ids]
        if accepted:
            try:
                with open_db(config.paths.db_path) as connection:
                    finish_remake_intents(connection, accepted, prefix=run_prefix, mode=mode, approval=approval)
            except Exception:
                logger.exception("Could not clear accepted remake records for %s", run_prefix)
    message = f"Queued {len(queued_ids)} {'file' if len(queued_ids) == 1 else 'files'} to make again."
    if left_out:
        message += " " + " ".join(f"{file['name']}: {file['reason']}" for file in left_out)
    return {
        "ok": bool(queued_ids), "action": "size_held_remade", "message": message,
        "queued_library_item_ids": queued_ids, "removed_library_item_ids": removed,
        "left_out": left_out, "runs": runs,
        **({"job": runs[0]["job"]} if len(runs) == 1 and runs[0].get("job") else {}),
    }


def _refusal_codes(recovery: Mapping[str, Any] | None) -> list[str]:
    """Why a remake was refused, as codes: the recovery's own blockers, or that the file no longer waits for one."""
    return list(recovery["blocked_codes"]) if recovery is not None else ["not_waiting"]


def keep_original_after_failed_checks(
        config: MediaforceConfig, library_item_id: int, *, failed_attempts: int, now_iso: Callable[[], str],
        reason_code: str = CHECK_FAILED_KEEP_REASON,
) -> str:
    """Keep the original of a file whose copies kept failing their check, then remove its last copy.

    The keep is bound to the current source like any other, so a changed source lapses it. Returns why the
    failed copy is still there when it cannot be removed yet; the caller tries again on a later pass.
    """
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = _load_remake_row(connection, library_item_id)
        source = connection.execute(
            select(library_items).where(library_items.c.id == library_item_id)
        ).mappings().fetchone()
        if row is None or source is None or row["promoted_at"] is not None:
            return ""
        fingerprint = library_item_source_fingerprint(cast(dict[str, Any], dict(source)))
        if not fingerprint:
            return "Mediaforce can't identify the source of this file, so it can't keep the original."
        stored = connection.execute(
            select(motion_keep_decisions.c.source_fingerprint)
            .where(motion_keep_decisions.c.library_item_id == library_item_id)
        ).scalar_one_or_none()
        if stored != fingerprint:
            now = now_iso()
            keep = {"source_fingerprint": fingerprint, "reason_code": reason_code,
                    "prefix": str(source["parent_dir"]), "kept_at": now}
            connection.execute(
                sqlite_insert(motion_keep_decisions)
                .values(library_item_id=library_item_id, **keep)
                .on_conflict_do_update(index_elements=[motion_keep_decisions.c.library_item_id], set_=keep)
            )
            connection.execute(item_events.insert().values(
                library_item_id=library_item_id, created_at=now, event_type=KEPT_ORIGINAL_EVENT,
                details_json=json.dumps({**keep, "rel_path": source["rel_path"], "failed_attempts": failed_attempts},
                                        sort_keys=True),
            ))
    if not _remove_finished_output(config, row):
        return "The original is kept, but Mediaforce could not remove the failed copy yet. It will try again."
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(delete(staged_artifacts).where(
            staged_artifacts.c.library_item_id == library_item_id, staged_artifacts.c.promoted_at.is_(None)))
        connection.execute(update(library_items).where(library_items.c.id == library_item_id)
                           .values(status="discovered", updated_at=now_iso()))
    return ""


def staged_remake_details(
        connection: DBClient,
        row: Any,
        prefix: str,
        *,
        current_approval: CurrentApprovalFn,
        settings_history: Collection[int] | None = None,
        manifest_reader: ManifestReader = read_manifest,
        retry_failed_check: bool = False,
) -> dict[str, Any] | None:
    """Offer recovery only for a size-only failure or missing settings history, or a failed check when retrying it.

    The action calls this again under its write lock before removing the output.
    """
    # A kept original settles the file; its leftover copy only waits to be removed.
    if row is None or row["promoted_at"] is not None or row.get("kept_original"):
        return None
    if not path_matches_scope(str(row.get("rel_path") or ""), prefix) and "rel_path" in row:
        return None
    intent = remake_intent(row)
    if requested_copy_is_present(row):
        intent = {}
    validation = _stored_validation(row)
    outcome = staged_validation_outcome(row["validation_json"])
    held = outcome == "size_held"
    final_size = missed_only_final_size(validation)
    failed_check = retry_failed_check and outcome in {"failed", "remaking"} and not final_size
    item_id = int(row["library_item_id"])
    missing_policy = validation.get("passed") is True and item_id not in (
        settings_history if settings_history is not None
        else staged_run_items(connection, {item_id}, manifest_reader=manifest_reader)
    )
    if not (held or final_size or missing_policy or intent or failed_check):
        return None
    run_prefix, _mode, context_available = _run_context(connection, row, prefix, manifest_reader=manifest_reader)
    blocked: list[tuple[str, str]] = []
    approval = current_approval(run_prefix)
    if intent and approval != intent.get("approval"):
        blocked.append((
            "approval_changed",
            "The approved settings changed after this remake was saved. Restore that approval before retrying.",
        ))
    if not context_available:
        blocked.append((
            "run_settings_missing", "Restore the saved run settings before making this file again. Nothing was removed.",
        ))
    else:
        if approval is None:
            blocked.append(("approval_missing", "Approve a fresh sample before making this file again."))
        elif not intent and final_size and not manifest_reader(Path(str(row["manifest_path"] or ""))):
            blocked.append((
                "run_details_missing",
                "Restore the run manifest from a run backup before making this file again; "
                "its size comparison cannot be verified. Nothing was removed.",
            ))
        elif not intent and final_size:
            blocker = _final_size_requeue_contract_blocker({
                "manifest_path": row["manifest_path"],
                "progress": {"failure_analysis": {
                    "kind": "final_size_target_miss",
                    "manifest_index": row["item_index"],
                    "target_size_verification": object_dict(validation.get("final_size_goal")),
                }},
            }, approval, manifest_reader=manifest_reader)
            if blocker:
                blocked.append((
                    "size_settings_unchanged",
                    "Approve a fresh sample with changed size or quality settings before making this file again. "
                    "Older runs without approval history need a changed size goal.",
                ))
    queue_blocker = staged_requeue_size_blocker(
        connection, run_prefix, int(row["library_item_id"]), approval, manifest_reader=manifest_reader,
    )
    if queue_blocker is not None:
        blocked.append((
            "size_checks_block",
            "This file’s saved size checks still block a new encode. Review its run settings before making it again.",
        ))
    original = Path(str(row["original_source_path"] or ""))
    staging = Path(str(row["staging_path"] or ""))
    if not original.is_file():
        blocked.append((
            "original_unreachable", "Restore access to the original file before making it again. Nothing was removed.",
        ))
    elif original.resolve() in {staging.resolve(), partial_output_path(staging).resolve()}:
        blocked.append((
            "copy_points_at_original",
            "The compressed copy points at the original. Check this file’s paths before making it again.",
        ))
    # Work still running here clears by itself; every other blocker needs someone to act.
    lasting_blockers = len(blocked)
    sample = load_latest_job(connection, run_prefix)
    if sample and sample.get("status") in EXECUTION_ACTIVE_JOB_STATUSES:
        blocked.append(("sampling", "Mediaforce is still sampling here. Make this file again once that sample finishes."))
    active_jobs = load_active_encode_jobs_for_prefix(connection, run_prefix)
    if any(job.get("status") != "queued" or job.get("started_at") is not None for job in active_jobs):
        blocked.append(("compressing", "Mediaforce is still compressing here. Make this file again once that run finishes."))
    elif active_jobs:
        busy_ids = _queued_run_item_ids(active_jobs, manifest_reader=manifest_reader)
        if busy_ids is None or int(row["library_item_id"]) in busy_ids:
            blocked.append((
                "queued_run", "A queued run may still make this file. Wait for it before making this file again.",
            ))
    reason = ("size_held" if held else "final_size" if final_size else "check_failed" if failed_check
              else "settings_history")
    return {"reason": intent.get("reason") or reason,
            "blocked_reason": " ".join(sentence for _code, sentence in blocked),
            "blocked_codes": [code for code, _sentence in blocked], "approval": approval,
            "busy": not lasting_blockers and len(blocked) > lasting_blockers}


def _queued_run_item_ids(jobs: list[dict[str, Any]], *, manifest_reader: ManifestReader) -> set[int] | None:
    """Missing file identities or invalid shard indexes cannot prove a copy is idle."""
    busy_ids: set[int] = set()
    for job in jobs:
        manifest = object_dict(manifest_reader(Path(str(job.get("manifest_path") or ""))))
        items = [object_dict(item) for item in object_list(manifest.get("items"))]
        if not items or any(isinstance(item.get("library_item_id"), bool)
                            or not isinstance(item.get("library_item_id"), int)
                            or item["library_item_id"] <= 0 for item in items):
            return None
        indexes = object_list(job.get("manifest_indexes")) or list(range(len(items)))
        if any(not isinstance(index, int) or not 0 <= index < len(items) for index in indexes):
            return None
        busy_ids.update(items[index]["library_item_id"] for index in indexes)
    return busy_ids


def _load_remake_row(connection: DBClient, item_id: int) -> DBRow | None:
    return connection.execute(
        select(staged_artifacts, library_items.c.rel_path,
               library_items.c.source_path.label("original_source_path"),
               kept_original_clause(library_items.c.id).label("kept_original"),
               func.coalesce(func.nullif(encode_jobs.c.last_host_json, "{}"),
                             encode_jobs.c.host_json).label("saved_host_json"))
        .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id)
                     .outerjoin(encode_jobs, encode_jobs.c.job_id == staged_artifacts.c.encode_job_id))
        .where(staged_artifacts.c.library_item_id == item_id)
    ).mappings().fetchone()


def _run_context(
        connection: DBClient, row: Any, prefix: str, *, manifest_reader: ManifestReader = read_manifest,
) -> tuple[str, str, bool]:
    """Use the existing saved selection when the manifest or terminal job is no longer available."""
    intent = remake_intent(row)
    if intent:
        return str(intent["prefix"]), str(intent["mode"]), True
    manifest = manifest_reader(Path(str(row["manifest_path"] or "")))
    if manifest is not None:
        selection = object_dict(manifest.get("selection"))
        available = True
    else:
        manifest = {}
        stored = connection.execute(select(run_manifests.c.selection_json).where(
            run_manifests.c.run_id == str(row["manifest_run_id"] or "")
        )).scalar_one_or_none()
        try:
            selection = object_dict(json.loads(str(stored))) if stored is not None else {}
            available = stored is not None and bool(
                selection.get("queue_mode") or object_dict(selection.get("lifecycle_override"))
            )
        except json.JSONDecodeError:
            selection, available = {}, False
    lifecycle_override = object_dict(selection.get("lifecycle_override"))
    recorded_prefix = object_dict(selection.get("media_scope")).get("prefix") or lifecycle_override.get("series_prefix")
    run_prefix = str(connection.execute(
        select(encode_jobs.c.prefix).where(encode_jobs.c.job_id == str(row["encode_job_id"] or ""))
    ).scalar_one_or_none() or recorded_prefix or prefix)
    return run_prefix, str(selection.get("queue_mode") or lifecycle_override.get("mode")
                           or _legacy_queue_mode(manifest, older_seasons=bool(lifecycle_override))), available


def _remove_finished_output(config: MediaforceConfig, row: Any) -> bool:
    target = staged_output_cleanup_target(config, row)
    if target is None:
        return False
    staging_path, host = target

    def check_ownership() -> None:
        with open_db(config.paths.db_path) as connection:
            if reason := staged_output_cleanup_blocker(connection, config, row):
                raise RuntimeError(reason)

    if not remove_stale_staging_path(partial_output_path(staging_path), host=host,
                                     ownership_check=check_ownership):
        return False
    return remove_stale_staging_path(staging_path, host=host, ownership_check=check_ownership)


def _record_decision(connection: Any, library_item_id: int, answer: str, size_prediction: dict[str, Any], now: str) -> None:
    connection.execute(
        item_events.insert().values(
            library_item_id=library_item_id,
            created_at=now,
            event_type="owner_size_held_decision",
            details_json=json.dumps({"answer": answer, **size_prediction}, sort_keys=True),
        )
    )
