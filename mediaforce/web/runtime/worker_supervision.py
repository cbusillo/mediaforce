import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SupervisedWorkerHandle:
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self) -> None:
        self.stop_event.set()

    def join(self) -> None:
        self.thread.join()


def run_supervised_worker_loop(
        *,
        process_once_fn: Callable[[], None],
        poll_seconds: float,
        stop_event: threading.Event,
        logger: Any,
        failure_message: str,
        wait_for_next_poll_fn: Callable[[float], Any] | None = None,
) -> None:
    # Last-resort supervision boundary: one failed pass must not kill the worker.
    effective_wait_for_next_poll_fn = wait_for_next_poll_fn or stop_event.wait
    while not stop_event.is_set():
        # noinspection PyBroadException
        try:
            process_once_fn()
        except Exception:
            logger.exception(failure_message)
        effective_wait_for_next_poll_fn(poll_seconds)
