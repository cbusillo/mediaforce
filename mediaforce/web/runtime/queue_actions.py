import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import literal_column
from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db_tables import calibration_jobs, encode_jobs
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.encode_queue import DISPLAY_ENCODE_JOB_KINDS, load_queue_state, save_queue_state


RETRYABLE_TERMINAL_ENCODE_JOB_STATUSES = ("needs_attention", "failed", "stopped")


def pause_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"is_paused": True, "updated_at": now_iso()})
        save_queue_state(connection, state)
    return {"ok": True, "message": "Paused the encode queue."}


def resume_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"is_paused": False, "stop_requested": False, "updated_at": now_iso()})
        save_queue_state(connection, state)
    return {"ok": True, "message": "Resumed the encode queue."}


def stop_encode_queue_action(
        *,
        connection_factory: Any,
        config: MediaforceConfig,
        now_iso: Any,
        cancel_queue_process: Any,
        sweep_orphaned_encode_processes: Any | None = None,
        clear_stale_encoding_items: Any | None = None,
) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"stop_requested": True, "is_paused": True, "updated_at": now_iso()})
        save_queue_state(connection, state)
    cancel_queue_process()
    if sweep_orphaned_encode_processes is not None:
        sweep_orphaned_encode_processes()
    cleared_stale_item_count = 0
    if clear_stale_encoding_items is not None:
        cleared_stale_item_count = int(clear_stale_encoding_items() or 0)
    return {
        "ok": True,
        "message": "Stopped and cleaned the encode queue.",
        "cleared_stale_item_count": cleared_stale_item_count,
    }


def retry_failed_encode_queue_action(
        *,
        connection_factory: Any,
        config: MediaforceConfig,
        load_calibration_state: Any,
        review_gate: Any,
        queue_folder_encode_action: Any,
) -> dict[str, Any]:
    terminal_prefixes = _retryable_terminal_encode_prefixes(connection_factory=connection_factory)
    if not terminal_prefixes:
        return {"ok": True, "message": "No failed folder encodes were ready to retry.", "queued_count": 0}

    return _retry_failed_encode_prefixes(
        terminal_prefixes,
        config=config,
        load_calibration_state=load_calibration_state,
        review_gate=review_gate,
        queue_folder_encode_action=queue_folder_encode_action,
    )


def retry_failed_encode_prefix_action(
        *,
        connection_factory: Any,
        config: MediaforceConfig,
        prefix: str,
        load_calibration_state: Any,
        review_gate: Any,
        queue_folder_encode_action: Any,
) -> dict[str, Any]:
    normalized_prefix = prefix.strip("/").strip()
    if not normalized_prefix:
        raise HTTPException(status_code=400, detail="A folder prefix is required.")

    retryable_prefixes = set(_retryable_terminal_encode_prefixes(connection_factory=connection_factory))
    if normalized_prefix not in retryable_prefixes:
        changed_inputs_required = _terminal_encode_prefix_requires_changed_inputs(
            connection_factory=connection_factory,
            prefix=normalized_prefix,
        )
        return {
            "ok": True,
            "message": (
                f"Choose a fresh size or compression goal for {normalized_prefix} before retrying."
                if changed_inputs_required
                else f"No failed folder encode was ready to retry for {normalized_prefix}."
            ),
            "queued_count": 0,
            "queued_prefixes": [],
            "review_blocked_count": 0,
            "review_blocked_prefixes": [],
            "blocked_count": 0,
            "blocked": [],
        }

    return _retry_failed_encode_prefixes(
        [normalized_prefix],
        config=config,
        load_calibration_state=load_calibration_state,
        review_gate=review_gate,
        queue_folder_encode_action=queue_folder_encode_action,
    )


def _retryable_terminal_encode_prefixes(*, connection_factory: Any) -> list[str]:
    with connection_factory() as connection:
        rows = connection.execute(
            select(encode_jobs.c.prefix, encode_jobs.c.status, encode_jobs.c.progress_json)
            .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
            .order_by(encode_jobs.c.updated_at.desc(), literal_column("rowid").desc())
        ).mappings().fetchall()

    latest_job_by_prefix: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        prefix = str(row["prefix"] or "").strip()
        if not prefix or prefix in latest_job_by_prefix:
            continue
        latest_job_by_prefix[prefix] = (
            str(row["status"] or "").strip(),
            str(row["progress_json"] or "").strip() or None,
        )

    return [
        prefix
        for prefix, (status, progress_json) in latest_job_by_prefix.items()
        if status in RETRYABLE_TERMINAL_ENCODE_JOB_STATUSES
        and not _terminal_encode_requires_changed_inputs(progress_json)
    ]


def _terminal_encode_requires_changed_inputs(progress_json: str | None) -> bool:
    if not progress_json:
        return False
    try:
        progress = json.loads(progress_json)
    except (TypeError, json.JSONDecodeError):
        return False
    analysis = object_dict(object_dict(progress).get("failure_analysis"))
    return str(analysis.get("kind") or "") == "final_size_target_miss"


def _terminal_encode_prefix_requires_changed_inputs(*, connection_factory: Any, prefix: str) -> bool:
    with connection_factory() as connection:
        row = connection.execute(
            select(encode_jobs.c.status, encode_jobs.c.progress_json)
            .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
            .where(encode_jobs.c.prefix == prefix)
            .order_by(encode_jobs.c.updated_at.desc(), literal_column("rowid").desc())
            .limit(1)
        ).mappings().fetchone()
    if row is None or str(row["status"] or "").strip() not in RETRYABLE_TERMINAL_ENCODE_JOB_STATUSES:
        return False
    return _terminal_encode_requires_changed_inputs(str(row["progress_json"] or "").strip() or None)


def _retry_failed_encode_prefixes(
        terminal_prefixes: list[str],
        *,
        config: MediaforceConfig,
        load_calibration_state: Any,
        review_gate: Any,
        queue_folder_encode_action: Any,
) -> dict[str, Any]:

    queued_prefixes: list[str] = []
    review_blocked_prefixes: list[str] = []
    blocked: list[dict[str, str]] = []
    for prefix in terminal_prefixes:
        calibration = load_calibration_state(config, prefix)
        gate = review_gate(calibration)
        if not bool(gate.get("can_confirm_full")):
            review_blocked_prefixes.append(prefix)
            continue
        try:
            result = queue_folder_encode_action(prefix, "", False)
        except HTTPException as exc:
            blocked.append({"prefix": prefix, "message": str(exc.detail)})
            continue
        if bool(result.get("ok")):
            queued_prefixes.append(prefix)
            continue
        blocked.append({"prefix": prefix, "message": str(result.get("message") or "Retry blocked.")})

    message_parts: list[str] = []
    if queued_prefixes:
        folder_label = "folder" if len(queued_prefixes) == 1 else "folders"
        message_parts.append(f"Retried {len(queued_prefixes)} failed {folder_label}.")
    else:
        message_parts.append("No failed folder encodes were retried.")
    if review_blocked_prefixes:
        message_parts.append(
            f"{len(review_blocked_prefixes)} still need approval before Mediaforce can queue them again."
        )
    if blocked:
        message_parts.append(f"{len(blocked)} could not be retried due to queue conflicts or other blockers.")

    return {
        "ok": True,
        "message": " ".join(message_parts),
        "queued_count": len(queued_prefixes),
        "queued_prefixes": queued_prefixes,
        "review_blocked_count": len(review_blocked_prefixes),
        "review_blocked_prefixes": review_blocked_prefixes,
        "blocked_count": len(blocked),
        "blocked": blocked,
    }


def stop_calibration_queue_action(
        *,
        connection_factory: Any,
        config: MediaforceConfig,
        now_iso: Any,
        active_calibration_process_controllers: Any,
        load_job_state: Any,
        save_job_state: Any,
) -> dict[str, Any]:
    stopped_message = "Calibration queue job was stopped and cleaned up."
    with connection_factory() as connection:
        active_rows = connection.execute(
            select(calibration_jobs.c.prefix)
            .where(calibration_jobs.c.status.in_(("queued", "starting", "running")))
            .order_by(calibration_jobs.c.created_at, literal_column("rowid"))
        ).mappings().fetchall()
        active_prefixes = [str(row["prefix"]) for row in active_rows]
        for prefix in active_prefixes:
            payload = load_job_state(connection, config, prefix)
            if payload is None or str(payload.get("status") or "") not in {"queued", "starting", "running"}:
                continue
            save_job_state(
                connection,
                config,
                prefix,
                {
                    **payload,
                    "status": "stopped",
                    "finished_at": now_iso(),
                    "error": stopped_message,
                },
            )
        connection.commit()
    for controller in active_calibration_process_controllers():
        controller.cancel()
    if not active_prefixes:
        return {"ok": True, "message": "Calibration queue was already idle."}
    return {"ok": True, "message": "Stopped and cleaned the calibration queue."}
