"""Read-only work evidence for a system-owned release controller."""

from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from sqlalchemy import func, select

from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import calibration_jobs, encode_jobs, library_item_evidence_state, scan_runs
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, count_pending_encode_work
from mediaforce.library.evidence_queue import EVIDENCE_WORK_ACTIVE_STATUSES
from mediaforce.tuning.calibration_jobs import EXECUTION_ACTIVE_JOB_STATUSES
from mediaforce.web.runtime.job_runtime import TERMINAL_SCAN_JOB_STATUSES


class ReleaseWorkCounts(TypedDict):
    pending_encode: int
    active_encode: int
    active_calibration: int
    unfinished_scan_rows: int
    active_evidence: int


class ReleaseWorkSnapshot(TypedDict):
    observed_at: str
    database_idle: bool
    counts: ReleaseWorkCounts


def release_work_payload(db_path: Path) -> ReleaseWorkSnapshot:
    with open_readonly_db(db_path) as connection:
        return release_work_snapshot(connection)


def release_work_snapshot(connection: DBClient) -> ReleaseWorkSnapshot:
    """Observe database work, including children hidden by a folder's attention state.

    This is evidence only. It neither reserves an idle window nor authorizes a
    release. Scan job files, publishing locks and remote custody are separate
    evidence; the controller must quiesce all producers and recheck before stopping.
    """
    counts: ReleaseWorkCounts = {
        "pending_encode": count_pending_encode_work(connection),
        "active_encode": int(connection.execute(
            select(func.count()).select_from(encode_jobs)
            .where(encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES))
        ).scalar_one()),
        "active_calibration": int(connection.execute(
            select(func.count()).select_from(calibration_jobs)
            .where(calibration_jobs.c.status.in_(tuple(EXECUTION_ACTIVE_JOB_STATUSES)))
        ).scalar_one()),
        "unfinished_scan_rows": int(connection.execute(
            select(func.count()).select_from(scan_runs)
            .where(~scan_runs.c.status.in_(tuple(TERMINAL_SCAN_JOB_STATUSES)))
        ).scalar_one()),
        "active_evidence": int(connection.execute(
            select(func.count()).select_from(library_item_evidence_state)
            .where(library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_ACTIVE_STATUSES))
        ).scalar_one()),
    }
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "database_idle": not any(counts.values()),
        "counts": counts,
    }
