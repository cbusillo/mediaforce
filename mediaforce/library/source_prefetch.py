import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import TracebackType

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_item_evidence_state
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.utils import timestamp
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.evidence_queue import (
    EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
    EvidenceWorkClaim,
    load_evidence_queue_state,
    next_evidence_work_query,
)
from mediaforce.library.source_intake import assess_source_input


class SourceAdmissionPrefetcher:
    def __init__(self, *, stop_event: threading.Event | None, logger: logging.Logger) -> None:
        self._stop_event = stop_event
        self._logger = logger
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="original-admission")
        self._future: Future[None] | None = None

    def __enter__(self) -> "SourceAdmissionPrefetcher":
        return self

    def __exit__(self, _kind: type[BaseException] | None, _error: BaseException | None, _trace: TracebackType | None) -> None:
        self.wait()
        self._executor.shutdown(wait=True)

    def start(self, config: MediaforceConfig, claim: EvidenceWorkClaim) -> None:
        if self._future is not None:
            return
        with open_db(config.paths.db_path) as connection:
            candidate = connection.execute(
                next_evidence_work_query(claim.batch_id, timestamp()).where(
                    library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                    library_item_evidence_state.c.library_item_id != claim.library_item_id,
                )
            ).mappings().first()
        if candidate is not None:
            self._future = self._executor.submit(self._prepare, config, claim, int(candidate["library_item_id"]))

    def wait(self) -> None:
        if self._future is None:
            return
        try:
            self._future.result()
        except ProcessCancelledError:
            self._logger.debug("Original admission preparation was interrupted")
        except Exception:
            self._logger.exception("Original admission preparation failed; its queued claim will retry normally")
        finally:
            self._future = None

    def _prepare(self, config: MediaforceConfig, claim: EvidenceWorkClaim, item_id: int) -> None:
        controller = ManagedProcessController()
        last_check = 0.0

        def check_cancelled() -> None:
            nonlocal last_check
            if self._stop_event is not None and self._stop_event.is_set():
                raise ProcessCancelledError("Original admission will resume after restart")
            now = time.monotonic()
            if now - last_check < 1:
                return
            last_check = now
            with open_db(config.paths.db_path) as connection:
                state = load_evidence_queue_state(connection)
                candidate = connection.execute(
                    next_evidence_work_query(claim.batch_id, timestamp()).where(
                        library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                        library_item_evidence_state.c.library_item_id != claim.library_item_id,
                    )
                ).mappings().first()
                priority = connection.execute(select(library_item_evidence_state.c.library_item_id).where(
                    library_item_evidence_state.c.work_batch_id == claim.batch_id,
                    library_item_evidence_state.c.work_status == "queued",
                    library_item_evidence_state.c.work_priority < claim.work_priority,
                ).limit(1)).first()
                if (background_work_is_paused(connection) or state["is_paused"] or state["cancel_requested"]
                        or state["batch_id"] != claim.batch_id or candidate is None
                        or candidate["library_item_id"] != item_id or priority is not None):
                    raise ProcessCancelledError("Original admission preparation is no longer runnable")

        try:
            check_cancelled()
            with controller.activity_guard(check_cancelled):
                assess_source_input(config, item_id, controller, check_cancelled)
        finally:
            controller.reset()
