import logging
import threading
import time
from collections.abc import Callable, Collection
from concurrent.futures import Future, ThreadPoolExecutor
from types import TracebackType
from typing import Any, Protocol

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import library_item_evidence_state
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.utils import timestamp
from mediaforce.library.evidence_queue import (
    EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
    EvidenceWorkClaim,
    next_evidence_work_query,
    original_intake_hold,
    refresh_controller_digest_waits,
    waiting_evidence_host_roots,
)
from mediaforce.library.source_intake import assess_source_input
from mediaforce.library.evidence_hosts import collect_evidence_host_rows, controller_digest_window, remote_evidence_hosts


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class SourceAdmissionPrefetcher:
    def __init__(self, *, stop_event: StopSignal | None, logger: logging.Logger, deadline: float | None = None,
                 evidence_host_rows: Callable[[MediaforceConfig], list[dict[str, Any]]] = collect_evidence_host_rows) -> None:
        self._stop_event = stop_event
        self._closed = threading.Event()
        self._deadline = deadline
        self._logger = logger
        self._evidence_host_rows = evidence_host_rows
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="original-admission")
        self._future: Future[None] | None = None

    def __enter__(self) -> "SourceAdmissionPrefetcher":
        return self

    def __exit__(self, _kind: type[BaseException] | None, _error: BaseException | None, _trace: TracebackType | None) -> None:
        self._closed.set()
        self.wait()
        self._executor.shutdown(wait=True)

    def start(self, config: MediaforceConfig, claim: EvidenceWorkClaim, *, excluded_media_roots: Collection[str] = ()) -> None:
        if self._future is not None:
            return
        try:
            self._future = self._executor.submit(self._prepare_next, config, claim, frozenset(excluded_media_roots))
        except (OSError, RuntimeError):
            self._logger.exception("Original admission preparation could not start; continuing the current file")

    def _prepare_next(self, config: MediaforceConfig, claim: EvidenceWorkClaim, roots: frozenset[str]) -> None:
        window = controller_digest_window(config, self._evidence_host_rows)
        with open_db(config.paths.db_path) as connection:
            refresh_controller_digest_waits(connection, window)
            item_id = self._next_item(config, connection, claim, roots)
        if item_id is not None:
            self._prepare(config, claim, item_id, roots)

    @staticmethod
    def _next_item(config: MediaforceConfig, connection: DBClient, claim: EvidenceWorkClaim, roots: Collection[str]) -> int | None:
        if remote_evidence_hosts(config):
            roots = set(roots) | waiting_evidence_host_roots(connection, batch_id=claim.batch_id)
        candidate = connection.execute(next_evidence_work_query(claim.batch_id, timestamp(), excluded_media_roots=roots).where(
            library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
            library_item_evidence_state.c.library_item_id != claim.library_item_id,
        )).mappings().first()
        return int(candidate["library_item_id"]) if candidate is not None else None

    def _check_stopped(self) -> None:
        if (self._closed.is_set() or (self._stop_event is not None and self._stop_event.is_set())
                or (self._deadline is not None and time.monotonic() >= self._deadline)):
            raise ProcessCancelledError("Original admission preparation has stopped")

    def wait(self) -> None:
        if self._future is None:
            return
        error = self._future.exception()
        self._future = None
        if isinstance(error, ProcessCancelledError):
            self._logger.debug("Original admission preparation was interrupted")
        elif error is not None:
            self._logger.error("Original admission preparation failed; its queued claim will retry normally",
                               exc_info=(type(error), error, error.__traceback__))

    def _prepare(self, config: MediaforceConfig, claim: EvidenceWorkClaim, item_id: int, roots: frozenset[str]) -> None:
        controller = ManagedProcessController()
        last_check = 0.0

        def require_runnable(connection: DBClient) -> None:
            self._check_stopped()
            if (original_intake_hold(connection, batch_id=claim.batch_id, priority=claim.work_priority,
                    excluded_media_roots=roots, allowed_running_item=claim.library_item_id) is not None
                    or self._next_item(config, connection, claim, roots) != item_id):
                raise ProcessCancelledError("Original admission preparation is no longer runnable")

        def check_cancelled() -> None:
            nonlocal last_check
            self._check_stopped()
            now = time.monotonic()
            if now - last_check < 1:
                return
            last_check = now
            with open_db(config.paths.db_path) as connection:
                require_runnable(connection)

        try:
            check_cancelled()
            with controller.activity_guard(check_cancelled):
                assess_source_input(config, item_id, controller, controller.throw_if_cancelled, before_commit=require_runnable,
                    digest_guard=lambda: controller_digest_window(config, self._evidence_host_rows).guard(controller))
        finally:
            controller.reset()
