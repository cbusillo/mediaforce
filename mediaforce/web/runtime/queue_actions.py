from typing import Any


from mediaforce.core.config import MediaforceConfig
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state
from mediaforce.library.background_work import background_work_is_paused, set_background_work_paused
from mediaforce.ops.runtime_install import read_update_intent, update_hold_reason
from mediaforce.library.evidence_queue import resume_evidence_queue


def pause_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    _ = config
    with connection_factory() as connection:
        state = load_queue_state(connection)
        state.update({"is_paused": True, "updated_at": now_iso()})
        save_queue_state(connection, state)
    return {"ok": True, "message": "Paused the encode queue."}


def resume_encode_queue_action(*, connection_factory: Any, config: MediaforceConfig, now_iso: Any) -> dict[str, Any]:
    hold = update_hold_reason(config.paths.db_path.parent)
    if hold:
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


def fence_for_update_action(*, connection_factory: Any, now_iso: Any) -> dict[str, Any]:
    """Pause new encodes and background work for an app update; report what was already paused so it can be restored."""
    with connection_factory() as connection:
        updated_at = now_iso()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            state = load_queue_state(connection)
            paused = {"encode": bool(state["is_paused"]), "background": background_work_is_paused(connection)}
            state.update({"is_paused": True, "updated_at": updated_at})
            save_queue_state(connection, state)
            set_background_work_paused(connection, is_paused=True, updated_at=updated_at, within_transaction=True)
        except BaseException:
            connection.rollback()
            raise
    return {"ok": True, "paused": paused}


def release_update_fence_action(
        paused: dict[str, Any], *, connection_factory: Any, config: MediaforceConfig, now_iso: Any,
) -> dict[str, Any]:
    """Put each pause back as it was before the update, so only a previously running queue resumes. Only an update
    that is ending may do this; while any other step is unfinished, new work stays held."""
    intent = read_update_intent(config.paths.db_path.parent)
    if intent is not None and intent.get("phase") != "releasing":
        return {"ok": False, "message": update_hold_reason(config.paths.db_path.parent)}
    with connection_factory() as connection:
        updated_at = now_iso()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            state = load_queue_state(connection)
            state.update({"is_paused": paused.get("encode") is not False, "updated_at": updated_at})
            save_queue_state(connection, state)
            set_background_work_paused(
                connection, is_paused=paused.get("background") is not False, updated_at=updated_at, within_transaction=True,
            )
        except BaseException:
            connection.rollback()
            raise
    return {"ok": True}
