from typing import Any

from mediaforce.tuning.calibration_jobs import load_job
from mediaforce.core.config import MediaforceConfig
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state


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


def stop_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any, cancel_queue_process: Any) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"stop_requested": True, "is_paused": True, "updated_at": now_iso()})
        save_queue_state(connection, state)
    cancel_queue_process()
    return {"ok": True, "message": "Stopped and cleaned the encode queue."}


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
        active_rows = connection.exec_driver_sql(
            """
            SELECT prefix
            FROM calibration_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY created_at, rowid
            """
        ).mappings().fetchall()
        active_prefixes = [str(row["prefix"]) for row in active_rows]
        for prefix in active_prefixes:
            payload = load_job_state(connection, config, prefix)
            if payload is None or str(payload.get("status") or "") not in {"queued", "running"}:
                continue
            save_job_state(
                connection,
                config,
                prefix,
                {
                    **payload,
                    "status": "failed",
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
