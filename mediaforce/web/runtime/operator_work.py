import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.library.background_work import list_evidence_backlog, load_background_work_state, \
    summarize_evidence_inventory
from mediaforce.library.evidence_queue import EVIDENCE_QUEUE_ACTIVE_STATUSES, evidence_queue_summary
from mediaforce.library.evidence_worker import run_evidence_queue_until_blocked

LOGGER = logging.getLogger(__name__)

ACTIVE_SCAN_STATUSES = ("queued", "running")
TERMINAL_EVIDENCE_QUEUE_STATUSES = ("completed", "completed_with_errors", "cancelled")


class BoundedEvidenceRunner:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, *, max_work_items: int) -> bool:
        if max_work_items <= 0:
            raise ValueError("Evidence work item budget must be greater than zero.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(
                target=self._run,
                kwargs={"max_work_items": max_work_items},
                name="bounded-evidence-runner",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return True

    def _run(self, *, max_work_items: int) -> None:
        current_thread = threading.current_thread()
        try:
            run_evidence_queue_until_blocked(
                config_path=self._config_path,
                max_work_items=max_work_items,
            )
        except Exception:
            LOGGER.exception("Bounded evidence work failed")
        finally:
            with self._lock:
                if self._thread is current_thread:
                    self._thread = None


def build_operator_work_payload(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        scan_job: dict[str, Any] | None,
        catalog_is_stale: bool,
        latest_scan_completed_at: datetime | None,
        evidence_runner_active: bool,
        backlog_offset: int = 0,
        backlog_limit: int = 25,
        evidence_kind: str | None = None,
        evidence_state: str | None = None,
        media_root: str | None = None,
        work_status: str | None = None,
) -> dict[str, Any]:
    background = load_background_work_state(connection)
    queue = evidence_queue_summary(connection)
    inventory = summarize_evidence_inventory(connection)
    backlog = list_evidence_backlog(
        connection,
        offset=backlog_offset,
        limit=backlog_limit,
        evidence_kind=evidence_kind,
        state=evidence_state,
        media_root=media_root,
        work_status=work_status,
    )
    root_rows = _catalog_root_rows(connection, config)
    warnings = _scan_warnings(scan_job)
    catalog_item_count = sum(int(row["item_count"]) for row in root_rows)
    scan_status = str((scan_job or {}).get("status") or "idle")
    queue_status = str(queue.get("status") or "idle")
    running_evidence = object_dict(queue.get("running")) or None
    background_paused = bool(background["is_paused"])
    queue_active = queue_status in EVIDENCE_QUEUE_ACTIVE_STATUSES
    terminal_status = queue_status if queue_status in TERMINAL_EVIDENCE_QUEUE_STATUSES else None
    live_status = "idle" if terminal_status else queue_status
    progress = _evidence_progress(queue)
    refresh_active = (
        scan_status in ACTIVE_SCAN_STATUSES
        or evidence_runner_active
        or running_evidence is not None
    )

    return {
        "background": {
            **background,
            "status": "paused" if background_paused else "active",
        },
        "catalog": {
            "status": scan_status,
            "freshness": _catalog_freshness(
                item_count=catalog_item_count,
                catalog_is_stale=catalog_is_stale,
                warning_count=len(warnings),
            ),
            "item_count": catalog_item_count,
            "last_completed_at": (
                latest_scan_completed_at.isoformat(timespec="seconds")
                if latest_scan_completed_at is not None
                else None
            ),
            "job": scan_job,
            "source_roots": root_rows,
            "warnings": warnings,
            "can_refresh": not background_paused and scan_status not in ACTIVE_SCAN_STATUSES,
        },
        "evidence": {
            "live_status": live_status,
            "terminal_status": terminal_status,
            "runner_active": evidence_runner_active,
            "queue": queue,
            "progress": progress,
            "inventory": inventory,
            "backlog": backlog,
            "can_prepare": not background_paused and not queue_active,
            "can_pause": (
                not background_paused
                and not bool(queue.get("is_paused"))
                and (evidence_runner_active or running_evidence is not None or queue_status == "running")
            ),
            "can_resume": (
                not background_paused
                and queue_status in {"paused", "queued"}
                and not bool(queue.get("cancel_requested"))
                and running_evidence is None
                and not evidence_runner_active
            ),
            "can_cancel": queue_active,
        },
        "refresh": {
            "mode": "active" if refresh_active else "manual",
            "interval_ms": 2_500 if refresh_active else None,
        },
    }


def _catalog_root_rows(connection: DBClient, config: MediaforceConfig) -> list[dict[str, Any]]:
    counts = {
        str(row["media_root"]): int(row["item_count"] or 0)
        for row in connection.execute(
            select(
                library_items.c.media_root,
                func.count().label("item_count"),
            )
            .where(library_items.c.status != "missing")
            .group_by(library_items.c.media_root)
        ).mappings()
    }
    definitions = config.library_definition_map
    return [
        {
            "key": root_key,
            "label": str(
                object_dict(definitions.get(root_key)).get("label")
                or root_key.replace("_", " ").replace("-", " ").title()
            ),
            "item_count": counts.get(root_key, 0),
        }
        for root_key in config.scan_source_root_map
    ]


def _scan_warnings(scan_job: dict[str, Any] | None) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for raw_warning in object_list(object_dict((scan_job or {}).get("stats")).get("warnings")):
        warning = object_dict(raw_warning)
        message = str(warning.get("message") or "").strip()
        if not message:
            continue
        warnings.append(
            {
                "code": str(warning.get("code") or "source_warning"),
                "library_key": str(warning.get("library_key") or ""),
                "label": str(warning.get("label") or "Library"),
                "message": message,
                "preserved_item_count": int(warning.get("preserved_item_count") or 0),
            }
        )
    return warnings


def _catalog_freshness(*, item_count: int, catalog_is_stale: bool, warning_count: int) -> str:
    if item_count <= 0 or warning_count:
        return "unknown"
    return "stale" if catalog_is_stale else "current"


def _evidence_progress(queue: dict[str, Any]) -> dict[str, Any]:
    total_count = int(queue.get("item_count") or 0)
    counts = {
        str(status): int(count or 0)
        for status, count in object_dict(queue.get("counts")).items()
    }
    done_count = sum(counts.get(status, 0) for status in ("completed", "failed", "cancelled"))
    percent = round(done_count * 100 / total_count) if total_count else None
    return {
        "total_count": total_count,
        "remaining_count": int(queue.get("remaining_count") or 0),
        "done_count": done_count,
        "percent": percent,
        "counts": counts,
        "running": object_dict(queue.get("running")) or None,
    }
