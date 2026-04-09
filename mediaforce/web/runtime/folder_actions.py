import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, list_child_encode_jobs, \
    load_latest_terminal_encode_job_for_prefix
from mediaforce.encoding.staging import safe_unlink
from mediaforce.library.run_manifests import create_folder_manifest


def queue_folder_encode_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        notes: str,
        bypass_schedule: bool,
        *,
        now_iso: Any,
        load_job_state: Any,
        load_calibration_state: Any,
        review_gate: Any,
        upsert_override: Any,
        load_active_encode_job_for_prefix_fn: Any,
        load_latest_encode_job_for_prefix_fn: Any,
        clear_terminal_encode_jobs_for_prefix_fn: Any,
        prepare_terminal_encode_job_for_requeue_fn: Any,
        save_encode_job: Any,
) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        existing_job = load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        calibration = load_calibration_state(config, normalized_prefix)
        gate = review_gate(calibration)
        if not gate["can_confirm_full"]:
            raise HTTPException(status_code=400, detail=str(gate["message"]))
        if calibration is None:
            raise HTTPException(status_code=400, detail="Run a sampled calibration first.")
        saved_profile_path = config.paths.config_path.parent / "folder-defaults.toml"
        calibration_payload = object_dict(calibration)
        upsert_override(saved_profile_path, normalized_prefix, calibration_payload["policy"])
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        if active_encode_job is not None:
            recovered = _recover_active_folder_encode_job(
                connection,
                active_encode_job,
                notes=notes,
                now_iso=now_iso,
                prepare_terminal_encode_job_for_requeue_fn=prepare_terminal_encode_job_for_requeue_fn,
                save_encode_job=save_encode_job,
            )
            if recovered is not None:
                return recovered
            active_status = str(active_encode_job.get("status") or "queued").replace("_", " ")
            return {
                "ok": False,
                "message": f"A folder encode is already {active_status} for this folder.",
            }
        latest_encode_job = load_latest_terminal_encode_job_for_prefix(connection, normalized_prefix)
        if latest_encode_job is not None and str(latest_encode_job.get("status") or "") in {
            "needs_attention",
            "failed",
            "stopped",
        }:
            prepare_terminal_encode_job_for_requeue_fn(connection, latest_encode_job)
            _reset_stale_prefix_encoding_items_for_requeue(connection, config, normalized_prefix, now_iso=now_iso)
        refreshed_config = load_config(config.paths.config_path)
        manifest, manifest_path = create_folder_manifest(connection, refreshed_config, prefix=normalized_prefix)
        if not manifest["items"]:
            raise HTTPException(status_code=400, detail="No pending items were found to enqueue for this folder.")
        clear_terminal_encode_jobs_for_prefix_fn(connection, normalized_prefix)
        created_at = now_iso()
        parent_job_id = uuid.uuid4().hex[:12]
        queue_job = {
            "job_id": parent_job_id,
            "prefix": normalized_prefix,
            "job_kind": "folder",
            "parent_job_id": None,
            "status": "queued",
            "manifest_path": str(manifest_path),
            "item_count": len(manifest["items"]),
            "saved_profile_path": str(saved_profile_path),
            "manifest_indexes": None,
            "host": {},
            "last_host": {},
            "notes": notes.strip(),
            "bypass_schedule": bypass_schedule,
            "process_pid": None,
            "error": None,
            "attempt_count": 0,
            "leased_at": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "worker_id": None,
            "retry_not_before": None,
            "waiting_reason": None,
            "terminal_reason": None,
            "last_failure_kind": None,
            "last_failure_at": None,
            "host_cooldown_until": None,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "updated_at": created_at,
        }
        save_encode_job(connection, queue_job)
        for shard_indexes in _build_manifest_shards(refreshed_config, manifest):
            save_encode_job(
                connection,
                {
                    **queue_job,
                    "job_id": uuid.uuid4().hex[:12],
                    "job_kind": "shard",
                    "parent_job_id": parent_job_id,
                    "manifest_indexes": shard_indexes,
                    "item_count": len(shard_indexes),
                },
            )
    return {"ok": True, "message": "Queued the full folder encode.", "job": queue_job}


def validate_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: Any | None = None,
        load_folder_staged_items_fn: Any,
        validate_manifest_items_fn: Any,
) -> dict[str, Any]:
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = lambda *_args, **_kwargs: None
    with open_db(config.paths.db_path) as connection:
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        encode_blocked = _validate_delivery_blocked_by_active_encode(active_encode_job)
        if encode_blocked is not None:
            return encode_blocked
        items = load_folder_staged_items_fn(
            connection,
            config,
            normalized_prefix,
            statuses={"encoded", "validated"},
        )
        if not items:
            return {
                "ok": False,
                "message": "No staged encoded files are ready to validate for this folder.",
            }
        inaccessible_response = _inaccessible_staged_item_response(
            items=items,
            action="validate",
            zero_count_key="validated_count",
        )
        if inaccessible_response is not None:
            return inaccessible_response
        manifest = {"items": items}
        results: list[dict[str, Any]] = []
        for index in range(len(items)):
            try:
                result = validate_manifest_items_fn(connection, config, manifest, [index])[0]
            except Exception as exc:
                result = {
                    "passed": False,
                    "error": str(exc),
                }
            results.append(object_dict(result))
    passed_count = sum(1 for result in results if object_dict(result).get("passed"))
    failed_count = len(results) - passed_count
    if failed_count:
        message = f"Validated {len(results)} files: {passed_count} passed, {failed_count} failed."
    else:
        message = f"Validated {passed_count} files. All staged outputs passed."
    return {
        "ok": True,
        "message": message,
        "validated_count": passed_count,
        "failed_count": failed_count,
        "item_count": len(results),
    }


def promote_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: Any | None = None,
        load_folder_staged_items_fn: Any,
        promote_manifest_items_fn: Any,
) -> dict[str, Any]:
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = lambda *_args, **_kwargs: None
    with open_db(config.paths.db_path) as connection:
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        encode_blocked = _validate_delivery_blocked_by_active_encode(active_encode_job)
        if encode_blocked is not None:
            return encode_blocked
        items = load_folder_staged_items_fn(
            connection,
            config,
            normalized_prefix,
            statuses={"validated"},
        )
        if not items:
            return {
                "ok": False,
                "message": "No validated staged files are ready to promote for this folder.",
            }
        inaccessible_response = _inaccessible_staged_item_response(
            items=items,
            action="promote",
            zero_count_key="promoted_count",
        )
        if inaccessible_response is not None:
            return inaccessible_response
        manifest = {"items": items}
        promoted_paths = promote_manifest_items_fn(connection, config, manifest, list(range(len(items))), force=False)
    promoted_count = len(promoted_paths)
    file_label = "file" if promoted_count == 1 else "files"
    return {
        "ok": True,
        "message": f"Promoted {promoted_count} validated {file_label} into the library.",
        "promoted_count": promoted_count,
    }


def _inaccessible_staged_item_response(
        *,
        items: list[dict[str, Any]],
        action: str,
        zero_count_key: str,
) -> dict[str, Any] | None:
    inaccessible_items = [item for item in items if not Path(str(item.get("staging_path") or "")).exists()]
    if not inaccessible_items:
        return None
    inaccessible_hosts = sorted(
        {
            str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
            for item in inaccessible_items
            if str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
        }
    )
    host_copy = f" Encoded hosts: {', '.join(inaccessible_hosts)}." if inaccessible_hosts else ""
    response = {
        "ok": False,
        "message": (
            f"Cannot {action} this folder from the current web host because {len(inaccessible_items)} staged "
            f"file{'s are' if len(inaccessible_items) != 1 else ' is'} not accessible locally.{host_copy}"
        ),
        zero_count_key: 0,
        "failed_count": len(inaccessible_items),
        "item_count": len(items),
    }
    if zero_count_key == "promoted_count":
        response.pop("failed_count")
        response.pop("item_count")
    return response


def _build_manifest_shards(_config: MediaforceConfig, manifest: dict[str, Any]) -> list[list[int]]:
    items = [object_dict(item) for item in object_list(manifest.get("items"))]
    if not items:
        return []
    return [[manifest_index] for manifest_index in range(len(items))]


def _recover_active_folder_encode_job(
        connection: Any,
        active_encode_job: dict[str, Any],
        *,
        notes: str,
        now_iso: Any,
        prepare_terminal_encode_job_for_requeue_fn: Any,
        save_encode_job: Any,
) -> dict[str, Any] | None:
    if str(active_encode_job.get("job_kind") or "single") != "folder":
        return None
    recoverable_children: list[dict[str, Any]] = []
    recoverable_indexes: list[int] = []
    for child in _folder_recoverable_children(connection, str(active_encode_job.get("job_id") or "")):
        child_indexes = [index for index in object_list(child.get("manifest_indexes")) if isinstance(index, int)]
        if not child_indexes:
            continue
        recoverable_children.append(child)
        recoverable_indexes.extend(child_indexes)
    recoverable_indexes = sorted(set(recoverable_indexes))
    if not recoverable_children or not recoverable_indexes:
        return None

    created_at = now_iso()
    for child in recoverable_children:
        prepare_terminal_encode_job_for_requeue_fn(connection, child)
        connection.execute(delete(encode_jobs).where(encode_jobs.c.job_id == str(child.get("job_id") or "")))

    recovery_notes = notes.strip() or str(active_encode_job.get("notes") or "").strip()
    for manifest_index in recoverable_indexes:
        save_encode_job(
            connection,
            {
                "job_id": uuid.uuid4().hex[:12],
                "prefix": str(active_encode_job.get("prefix") or ""),
                "job_kind": "shard",
                "parent_job_id": str(active_encode_job.get("job_id") or ""),
                "status": "queued",
                "manifest_path": str(active_encode_job.get("manifest_path") or ""),
                "manifest_indexes": [manifest_index],
                "item_count": 1,
                "saved_profile_path": active_encode_job.get("saved_profile_path"),
                "host": {},
                "last_host": {},
                "notes": recovery_notes,
                "bypass_schedule": bool(active_encode_job.get("bypass_schedule")),
                "process_pid": None,
                "error": None,
                "attempt_count": 0,
                "leased_at": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "worker_id": None,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": None,
                "last_failure_kind": None,
                "last_failure_at": None,
                "host_cooldown_until": None,
                "created_at": created_at,
                "started_at": None,
                "finished_at": None,
                "updated_at": created_at,
            },
        )

    file_label = "file" if len(recoverable_indexes) == 1 else "files"
    return {
        "ok": True,
        "action": "recovered",
        "message": f"Recovered {len(recoverable_indexes)} failed {file_label} back into the active folder encode.",
        "job": active_encode_job,
        "recovered_item_count": len(recoverable_indexes),
    }


def _folder_recoverable_children(connection: Any, parent_job_id: str) -> list[dict[str, Any]]:
    if not parent_job_id:
        return []
    return [
        child
        for child in list_child_encode_jobs(connection, parent_job_id)
        if str(child.get("status") or "") in {"needs_attention", "failed", "stopped"}
    ]


def _validate_delivery_blocked_by_active_encode(active_encode_job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not active_encode_job:
        return None
    status = str(active_encode_job.get("status") or "queued")
    if status not in ACTIVE_ENCODE_JOB_STATUSES:
        return None
    return {
        "ok": False,
        "message": f"Cannot deliver outputs while folder encode is {status.replace('_', ' ')}.",
    }


def _reset_stale_prefix_encoding_items_for_requeue(
        connection: Any,
        config: MediaforceConfig,
        prefix: str,
        *,
        now_iso: Any,
) -> None:
    normalized_prefix = str(prefix).strip().strip("/")
    if not normalized_prefix:
        return
    descendant_pattern = _prefix_descendant_path_pattern(normalized_prefix)
    protected_prefixes = _active_descendant_encode_prefixes(connection, normalized_prefix)
    rows = connection.execute(
        select(library_items.c.id, library_items.c.rel_path)
        .where(
            or_(
                library_items.c.parent_dir == normalized_prefix,
                library_items.c.parent_dir.like(descendant_pattern, escape="\\"),
            )
        )
        .where(library_items.c.status == "encoding")
    ).mappings().fetchall()
    if not rows:
        return
    updated_at = now_iso()
    for row in rows:
        rel_path = str(row["rel_path"] or "").strip()
        if _rel_path_is_within_any_prefix(rel_path, protected_prefixes):
            continue
        if rel_path:
            output_suffix = str(object_dict(config.media).get("output_container") or "").strip()
            if output_suffix:
                output_suffix = f".{output_suffix.lstrip('.')}"
            else:
                output_suffix = Path(rel_path).suffix or ".mkv"
            staging_path = config.staging_root / Path(rel_path).with_suffix(output_suffix)
            safe_unlink(staging_path)
            safe_unlink(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"))
        connection.execute(
            delete(staged_artifacts)
            .where(staged_artifacts.c.library_item_id == row["id"])
            .where(staged_artifacts.c.promoted_at.is_(None))
        )
        connection.execute(
            update(library_items)
            .where(library_items.c.id == row["id"])
            .where(library_items.c.status == "encoding")
            .values(status="planned", updated_at=updated_at)
        )


def _prefix_descendant_path_pattern(prefix: str) -> str:
    if not prefix:
        return "%"
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}/%"


def _active_descendant_encode_prefixes(connection: Any, prefix: str) -> set[str]:
    if not prefix:
        return set()
    descendant_pattern = _prefix_descendant_path_pattern(prefix)
    rows = connection.execute(
        select(encode_jobs.c.prefix)
        .where(encode_jobs.c.status.in_(("queued", "retry_backoff", "running")))
        .where(
            or_(
                encode_jobs.c.prefix == prefix,
                encode_jobs.c.prefix.like(descendant_pattern, escape="\\"),
            )
        )
    ).fetchall()
    return {
        str(row[0]).strip().strip("/")
        for row in rows
        if str(row[0] or "").strip().strip("/") and str(row[0]).strip().strip("/") != prefix
    }


def _rel_path_is_within_any_prefix(rel_path: str, prefixes: set[str]) -> bool:
    normalized_rel_path = str(rel_path).strip().strip("/")
    for prefix in prefixes:
        if normalized_rel_path == prefix or normalized_rel_path.startswith(f"{prefix}/"):
            return True
    return False


def save_profile_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        now_iso: Any,
        load_calibration_state: Any,
        calibration_draft_hash: Any,
        save_calibration_state: Any,
        load_advice_state: Any,
        record_visual_approval_artifact: Any,
        merge_advice_state: Any,
        upsert_override: Any,
) -> dict[str, Any]:
    calibration = load_calibration_state(config, normalized_prefix)
    if not calibration:
        raise HTTPException(status_code=400, detail="No draft calibration found for this folder")
    calibration_payload = object_dict(calibration)
    if str(calibration_payload.get("mode") or "sample") == "sample":
        if not calibration_payload.get("review_media_ready"):
            raise HTTPException(
                status_code=400,
                detail="Run a fresh sample before approving because the review clips are unavailable.",
            )
        calibration_payload["accepted_at"] = now_iso()
        calibration_payload["accepted_draft_hash"] = str(
            calibration_payload.get("draft_hash") or calibration_draft_hash(calibration_payload)
        )
        calibration_payload["accepted_sample_job_id"] = str(calibration_payload.get("job_id") or "")
        save_calibration_state(config, normalized_prefix, calibration_payload)
        advice_state = object_dict(load_advice_state(config, normalized_prefix))
        existing_approval = object_dict(advice_state.get("approval_artifact"))
        if str(existing_approval.get("sample_job_id") or "") != str(calibration_payload.get("job_id") or ""):
            with open_db(config.paths.db_path) as connection:
                approval_artifact = record_visual_approval_artifact(
                    connection,
                    config,
                    prefix=normalized_prefix,
                    note=str(advice_state.get("operator_note") or ""),
                    sample_item=object_dict(calibration_payload.get("sample_item")),
                    calibration=calibration_payload,
                    run_verdict=object_dict(advice_state.get("run_verdict")),
                    created_at=str(calibration_payload["accepted_at"]),
                )
            if approval_artifact is not None:
                approval_artifact["sample_job_id"] = str(calibration_payload.get("job_id") or "")
                merge_advice_state(
                    config,
                    normalized_prefix,
                    {
                        "approval_artifact": approval_artifact,
                        "operator_approved_at": calibration_payload["accepted_at"],
                    },
                )
    upsert_override(
        config.paths.config_path.parent / "folder-defaults.toml",
        normalized_prefix,
        calibration_payload["policy"],
    )
    return {"ok": True, "message": "Approved the current draft and saved it as the folder profile."}
