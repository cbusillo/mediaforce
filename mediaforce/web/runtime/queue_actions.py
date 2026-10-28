from typing import Any


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
