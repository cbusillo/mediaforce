import logging
import threading
import time
from collections.abc import Collection
from concurrent.futures import Future, ThreadPoolExecutor
from types import TracebackType

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
    waiting_evidence_host_roots,
)
from mediaforce.library.source_intake import assess_source_input
from mediaforce.library.evidence_state import EVIDENCE_STATE_CURRENT
from mediaforce.library.evidence_hosts import remote_evidence_hosts


class SourceAdmissionPrefetcher:
    def __init__(self, *, stop_event: threading.Event | None, logger: logging.Logger, deadline: float | None = None) -> None:
        self._stop_event = stop_event
        self._closed = threading.Event()
        self._deadline = deadline
        self._logger = logger
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
        with open_db(config.paths.db_path) as connection:
            item_id = self._next_item(config, connection, claim, roots)
        if item_id is not None:
            self._prepare(config, claim, item_id, roots)

    @staticmethod
    def _next_item(config: MediaforceConfig, connection: DBClient, claim: EvidenceWorkClaim, roots: Collection[str]) -> int | None:
        if remote_evidence_hosts(config):
            roots = set(roots) | waiting_evidence_host_roots(connection, batch_id=claim.batch_id)
        candidate = connection.execute(next_evidence_work_query(claim.batch_id, timestamp(), excluded_media_roots=roots).where(
            library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
            library_item_evidence_state.c.state != EVIDENCE_STATE_CURRENT,
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
            if (original_intake_hold(connection, batch_id=claim.batch_id, priority=claim.work_priority) is not None
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
                assess_source_input(config, item_id, controller, check_cancelled, before_commit=require_runnable)
        finally:
            controller.reset()
