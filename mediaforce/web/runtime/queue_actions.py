from collections.abc import Callable
from typing import Any


from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.process_control import ManagedProcessController, StartOverCancelledError
from mediaforce.encoding.encode_queue import RUNNABLE_ENCODE_JOB_KINDS, load_encode_job, load_queue_state, save_queue_state
from mediaforce.library.background_work import set_background_work_paused
from mediaforce.library.evidence_queue import resume_evidence_queue
from mediaforce.ops.runtime_install import update_hold_reason
from mediaforce.web.runtime.encode_runtime import encode_job_outputs_completed


def pause_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"is_paused": True, "updated_at": now_iso()})
        save_queue_state(connection, state)
    return {"ok": True, "message": "Paused the encode queue."}


def resume_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    if hold := update_hold_reason(config.paths.db_path.parent):
        return {"ok": False, "message": hold}
    with connection_factory() as connection:
        updated_at = now_iso()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            state = load_queue_state(connection)
            state.update({"is_paused": False, "stop_requested": False, "updated_at": updated_at})
            save_queue_state(connection, state)
            set_background_work_paused(
                connection, is_paused=False, updated_at=updated_at, within_transaction=True,
            )
            resume_evidence_queue(connection, updated_at=updated_at, within_transaction=True)
        except BaseException:
            connection.rollback()
            raise
    return {"ok": True, "message": "Resumed the encode queue."}


def stop_running_file_action(
        job_id: str, run: str, *, connection_factory: Any,
        live_encode_job_controller: Callable[[str], ManagedProcessController | None],
) -> dict[str, Any]:
    with connection_factory() as connection:
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            controller = live_encode_job_controller(job_id)
            refusal = "not_running" if controller is None else _stop_refusal(connection, job_id, run)
            if controller is None or refusal is not None:
                return {"ok": False, "code": refusal}
            controller.cancel(StartOverCancelledError("You stopped this file; it starts over later."))
        finally:
            connection.rollback()
    return {"ok": True}


def _stop_refusal(connection: DBClient, job_id: str, run: str) -> str | None:
    job = load_encode_job(connection, job_id)
    if job is None or job.get("status") != "running" or job.get("job_kind") not in RUNNABLE_ENCODE_JOB_KINDS:
        return "not_running"
    if str(job.get("started_at") or "") != run:
        return "started_again"
    if encode_job_outputs_completed(connection, job):
        return "finishing"
    return None
