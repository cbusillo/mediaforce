"""Per-file decisions for unpublished outputs that need to be kept or made again."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, run_manifests, staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.encoding.staging import FINAL_SIZE_GOAL_CHECK, partial_output_path
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.library.staged_integrity import staged_validation_outcome
from mediaforce.tuning.calibration_jobs import EXECUTION_ACTIVE_JOB_STATUSES, load_latest_job
from mediaforce.web.runtime.encode_runtime import remove_stale_staging_path
from mediaforce.web.runtime.ambiguous_motion import _legacy_queue_mode
from mediaforce.web.runtime.folder_actions import _final_size_requeue_contract_blocker, _staged_policy_states, \
    staged_requeue_size_blocker
from mediaforce.web.runtime.host_runtime import host_config_for_key
from mediaforce.web.runtime.manifest_reads import ManifestReader, read_manifest
from mediaforce.library.remake_intents import INTENT_KEY, finish_remake_intents, remake_intent, save_remake_intent
from mediaforce.library.remake_intents import requested_copy_is_present
from mediaforce.library.remake_intents import stored_validation as _stored_validation

logger = logging.getLogger(__name__)

NOT_HELD_MESSAGE = "This file is no longer waiting for a decision about its size."

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
        held = staged_validation_outcome(row["validation_json"]) == "size_held"
        if not held:
            return {"ok": False, "message": NOT_HELD_MESSAGE}
        intent = remake_intent(row)
        if intent and not requested_copy_is_present(row):
            return {"ok": False, "message": "This file is waiting to be made again. Retry Make again with its saved settings."}
        name = Path(str(row["rel_path"])).name
        now = now_iso()
        validation.pop(INTENT_KEY, None)
        validation["size_prediction"] = {**size_prediction, "owner_kept_at": now, "held": False}
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
) -> dict[str, Any]:
    """Recheck requested files independently and queue compatible remakes together."""
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
                                                    manifest_reader=manifest_reader)
                reason = NOT_HELD_MESSAGE if recovery is None else str(recovery["blocked_reason"])
                if recovery is None or reason:
                    left_out.append({"library_item_id": item_id, "name": names[item_id], "reason": reason})
                    continue
                intent = remake_intent(row)
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
                                                manifest_reader=manifest_reader)
                reason = NOT_HELD_MESSAGE if recovery is None else str(recovery["blocked_reason"])
                if not reason and remake_intent(row) != intent:
                    reason = "This file’s saved request changed. Refresh it before retrying."
                if not reason and recovery is not None and recovery["approval"] != intent["approval"]:
                    reason = "The approved settings changed before removal. Refresh before retrying. Nothing was removed."
                if not reason and not _remove_finished_output(config, row):
                    reason = "Mediaforce could not remove the finished copy yet. Try again."
                if recovery is None or reason:
                    if request_saved and remake_intent(row) == intent:
                        save_remake_intent(connection, row, {}, now=now_iso())
                    left_out.append({"library_item_id": item_id, "name": names[item_id], "reason": reason})
                    continue
                removed.append(item_id)
                run_prefix, mode = str(intent["prefix"]), str(intent["mode"])
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
                                        "Could not finish this file’s recovery. Use Make again to retry.")})
    runs: list[dict[str, Any]] = []
    queued_ids: list[int] = []
    for (run_prefix, mode, saved_approval), item_ids in groups.items():
        approval = object_dict(json.loads(saved_approval))
        try:
            result = queue_items(run_prefix, mode, item_ids, approval)
        except Exception:
            logger.exception("Could not queue remade files for %s in %s mode", run_prefix, mode)
            result = {"ok": False, "message": "Queueing failed. Use Make again to retry with its saved settings."}
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
                left_out.append({"library_item_id": item_id, "name": names[item_id],
                                 "reason": f"Finished copy removed, but not queued: {reason} "
                                           "Use Make again to retry only this file with its saved settings once this clears."})
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


def staged_remake_details(
        connection: DBClient,
        row: Any,
        prefix: str,
        *,
        current_approval: CurrentApprovalFn,
        policy_states: Mapping[int, str | None] | None = None,
        manifest_reader: ManifestReader = read_manifest,
) -> dict[str, Any] | None:
    """Offer recovery only for a size-only failure or missing settings history.

    The action calls this again under its write lock before removing the output.
    """
    if row is None or row["promoted_at"] is not None:
        return None
    if not path_matches_scope(str(row.get("rel_path") or ""), prefix) and "rel_path" in row:
        return None
    intent = remake_intent(row)
    if requested_copy_is_present(row):
        intent = {}
    validation = _stored_validation(row)
    failed = [str(object_dict(check).get("message") or "") for check in object_list(validation.get("checks"))
              if object_dict(check).get("passed") is False]
    held = staged_validation_outcome(row["validation_json"]) == "size_held"
    final_size = validation.get("passed") is False and failed == [FINAL_SIZE_GOAL_CHECK]
    missing_policy = validation.get("passed") is True and (
        policy_states if policy_states is not None else _staged_policy_states(
            connection, {int(row["library_item_id"])}, accepted_policy_hash="", manifest_reader=manifest_reader,
        )
    ).get(int(row["library_item_id"])) == "season_policy_provenance_missing"
    if not (held or final_size or missing_policy or intent):
        return None
    run_prefix, _mode, context_available = _run_context(connection, row, prefix, manifest_reader=manifest_reader)
    blocked_reasons: list[str] = []
    approval = current_approval(run_prefix)
    if intent and approval != intent.get("approval"):
        blocked_reasons.append("The approved settings changed after this remake was saved. Restore that approval before retrying.")
    if not context_available:
        blocked_reasons.append("Restore the saved run settings before making this file again. Nothing was removed.")
    else:
        if approval is None:
            blocked_reasons.append("Approve a fresh sample before making this file again.")
        elif not intent and final_size and not manifest_reader(Path(str(row["manifest_path"] or ""))):
            blocked_reasons.append(
                "Restore the run manifest from a run backup before making this file again; "
                "its size comparison cannot be verified. Nothing was removed."
            )
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
                blocked_reasons.append(
                    "Approve a fresh sample with changed size or quality settings before making this file again. "
                    "Older runs without approval history need a changed size goal."
                )
    queue_blocker = staged_requeue_size_blocker(
        connection, run_prefix, int(row["library_item_id"]), approval, manifest_reader=manifest_reader,
    )
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
    active_jobs = load_active_encode_jobs_for_prefix(connection, run_prefix)
    if any(job.get("status") != "queued" or job.get("started_at") is not None for job in active_jobs):
        blocked_reasons.append("Mediaforce is still compressing here. Make this file again once that run finishes.")
    elif active_jobs:
        busy_ids = _queued_run_item_ids(active_jobs, manifest_reader=manifest_reader)
        if busy_ids is None or int(row["library_item_id"]) in busy_ids:
            blocked_reasons.append("A queued run may still make this file. Wait for it before making this file again.")
    return {"reason": intent.get("reason") or ("size_held" if held else "final_size" if final_size else "settings_history"),
            "blocked_reason": " ".join(blocked_reasons), "approval": approval}


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


def staged_remake_records(
        connection: DBClient,
        records: list[dict[str, Any]],
        prefix: str,
        *,
        current_approval: CurrentApprovalFn,
) -> list[dict[str, Any]]:
    """Attach per-file action availability to the already paginated integrity rows."""
    manifest_reader = cache(read_manifest)
    ids = [int(record["item_id"]) for record in records if record.get("item_id") is not None]
    rows = {int(row["library_item_id"]): row for row in connection.execute(
        select(staged_artifacts, library_items.c.source_path.label("original_source_path"))
        .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
        .where(staged_artifacts.c.library_item_id.in_(ids))
    ).mappings()}
    policy_states = _staged_policy_states(connection, {
        item_id for item_id, row in rows.items() if _stored_validation(row).get("passed") is True
    }, accepted_policy_hash="", manifest_reader=manifest_reader)
    approvals: dict[str, dict[str, Any] | None] = {}

    def approval_for_scope(scope: str) -> dict[str, Any] | None:
        if scope not in approvals:
            approvals[scope] = current_approval(scope)
        return approvals[scope]

    for record in records:
        record_id = record.get("item_id")
        row = rows.get(record_id) if isinstance(record_id, int) else None
        recovery = staged_remake_details(connection, row, prefix,
                                        current_approval=approval_for_scope, policy_states=policy_states,
                                        manifest_reader=manifest_reader)
        if recovery is not None:
            record["remake"] = {key: recovery[key] for key in ("reason", "blocked_reason")}
            intent = remake_intent(row)
            if intent:
                record["remake"]["pending"] = True
                record["detail"] = ("Saved request to make this file again. Use Make again to retry only this file "
                                    "with its saved settings. " + str(recovery["blocked_reason"]))
                if intent.get("state") == "removed":
                    record["detail"] = "Finished copy removed; not queued yet. " + record["detail"]
                elif requested_copy_is_present(row):
                    record["detail"] = ("Finished copy remains. Use Make again to retry with the current approved settings. "
                                        + str(recovery["blocked_reason"]))
    return records


def _load_remake_row(connection: DBClient, item_id: int) -> DBRow | None:
    return connection.execute(
        select(staged_artifacts, library_items.c.rel_path,
               library_items.c.source_path.label("original_source_path"))
        .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
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
