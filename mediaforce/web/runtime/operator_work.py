import logging
import threading
from pathlib import Path


from mediaforce.core.config import load_config
from mediaforce.core.db import open_db
from mediaforce.library.evidence_queue import MAX_CONCURRENT_EVIDENCE_FILES, \
    recover_evidence_queue, refresh_controller_digest_waits, runnable_evidence_work_count
from mediaforce.library.evidence_hosts import collect_evidence_host_rows, controller_digest_window
from mediaforce.library.evidence_worker import EvidenceQueueProgress, EvidenceWorkerDeps, run_evidence_queue_until_blocked

LOGGER = logging.getLogger(__name__)

ACTIVE_SCAN_STATUSES = ("queued", "running")
TERMINAL_EVIDENCE_QUEUE_STATUSES = ("completed", "completed_with_errors", "cancelled")


class BoundedEvidenceRunner:
    def __init__(self, config_path: Path, *, deps: EvidenceWorkerDeps | None = None, stop_event: threading.Event | None = None) -> None:
        self._config_path = config_path
        self._deps = deps
        self._stop_event = stop_event
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress = EvidenceQueueProgress()

    def prepare_background_work(self) -> int:
        from mediaforce.library.source_intake import queue_original_intake

        config = self._deps.load_config(self._config_path) if self._deps else load_config(self._config_path)
        window = controller_digest_window(config, self._deps.evidence_host_rows if self._deps else collect_evidence_host_rows)
        with open_db(config.paths.db_path) as connection:
            discovered = queue_original_intake(connection, config)
            refresh_controller_digest_waits(connection, window)
            return discovered

    @property
    def active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, *, max_work_items: int | None) -> bool:
        if max_work_items is not None and max_work_items <= 0:
            raise ValueError("Evidence work item budget must be greater than zero.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._progress.wake()
                return False
            thread = threading.Thread(
                target=self._run,
                kwargs={"max_work_items": max_work_items},
                name="bounded-evidence-runner",
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise
        return True

    def wake(self) -> bool:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._progress.wake()
            return True

    def join(self) -> None:
        while True:
            with self._lock:
                thread = self._thread
            if thread is None:
                return
            thread.join()
            with self._lock:
                if self._thread is thread and not thread.is_alive():
                    self._thread = None

    def _run(self, *, max_work_items: int | None) -> None:
        current_thread = threading.current_thread()
        try:
            run_evidence_queue_until_blocked(
                config_path=self._config_path,
                deps=self._deps,
                max_work_items=max_work_items,
                stop_event=self._stop_event,
                max_running_files=MAX_CONCURRENT_EVIDENCE_FILES if max_work_items is None else 1,
                wake_signal=self._progress,
            )
        except Exception:
            LOGGER.exception("Bounded evidence work failed")
        finally:
            with self._lock:
                if self._thread is current_thread:
                    self._thread = None


def start_ready_evidence_work(db_path: Path, runner: BoundedEvidenceRunner) -> bool:
    """Run evidence work that is queued and unpaused but has no runner, such as checks production waits on."""
    if runner.wake():
        return False
    try:
        runner.prepare_background_work()
    except Exception:
        LOGGER.exception("Original discovery failed; queued analysis can still start")
    with open_db(db_path) as connection:
        recover_evidence_queue(connection)
        runnable = runnable_evidence_work_count(connection)
    if runnable <= 0:
        return False
    return runner.start(max_work_items=None)
