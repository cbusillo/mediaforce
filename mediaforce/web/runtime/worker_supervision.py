import threading
from collections.abc import Callable
from typing import Any


def run_supervised_worker_loop(
        *,
        process_once_fn: Callable[[], None],
        poll_seconds: float,
        logger: Any,
        failure_message: str,
        wait_for_next_poll_fn: Callable[[float], Any] | None = None,
) -> None:
    # Last-resort supervision boundary: one failed pass must not kill the worker.
    effective_wait_for_next_poll_fn = wait_for_next_poll_fn or threading.Event().wait
    while True:
        # noinspection PyBroadException
        try:
            process_once_fn()
        except Exception:
            logger.exception(failure_message)
        effective_wait_for_next_poll_fn(poll_seconds)
