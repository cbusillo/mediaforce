from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select

from mediaforce.tuning.calibration_jobs import list_queue_summary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items
from mediaforce.encoding.encode_queue import summarize_encode_queue
from mediaforce.library.workflow_state import build_folder_workflow_state


def dashboard_summary_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        preview_folder_cards: Any,
        maybe_schedule_scan: Any,
        decorate_encode_queue_for_scheduler: Any,
        library_color_map_for_config: Any,
        preview_limit: int | None = None,
) -> dict[str, Any]:
    if preview_limit is not None and preview_limit < 0:
        raise ValueError("preview_limit must be non-negative")

    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        scan_job = maybe_schedule_scan(connection, config, prefix=None)
        preview_folders = [] if preview_limit == 0 else preview_folder_cards(config, connection)
        if preview_limit is not None and preview_limit > 0:
            preview_folders = preview_folders[:preview_limit]
        catalog_empty = not preview_folders
        if preview_limit == 0:
            catalog_empty = int(
                connection.execute(
                    select(func.count())
                    .select_from(library_items)
                    .where(library_items.c.status != "missing")
                ).scalar_one()
            ) == 0
        calibration_queue = list_queue_summary(connection)
        encode_queue = decorate_encode_queue_for_scheduler(config, summarize_encode_queue(connection))
    return {
        "library_colors": library_color_map_for_config(config),
        "scan_job": scan_job,
        "calibration_queue": calibration_queue,
        "encode_queue": encode_queue,
        "folders_preview": [asdict(folder) for folder in preview_folders],
        "catalog_empty": catalog_empty,
        "folder_cache_key": _serialize_cache_key(cache_key),
    }


def dashboard_folders_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        list_folder_cards: Any,
        list_series_folder_cards: Any | None = None,
        include_series_folders: bool = True,
) -> dict[str, Any]:
    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_folder_cards(config, connection)
        series_folders = (
            list_series_folder_cards(config, connection)
            if include_series_folders and list_series_folder_cards is not None
            else []
        )
    return {
        "folders": [asdict(folder) for folder in folders],
        "series_folders": [asdict(folder) for folder in series_folders],
        "catalog_empty": not folders,
        "folder_cache_key": _serialize_cache_key(cache_key),
    }


def folder_status_payload(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_job_state: Any,
        load_retryable_sample_job_state: Any,
        load_scan_job_state: Any,
        load_active_encode_job_for_prefix: Any,
) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        calibration_job = load_job_state(connection, config, normalized_prefix)
        retryable_sample_job = load_retryable_sample_job_state(connection, config, normalized_prefix)
        active_encode_job = load_active_encode_job_for_prefix(connection, normalized_prefix)
        folder_scan_job = load_scan_job_state(config, normalized_prefix)
        workflow_state = build_folder_workflow_state(connection, normalized_prefix).to_payload()
    polling_active = bool(
        (calibration_job and calibration_job.get("status") in {"queued", "running"})
        or (active_encode_job and active_encode_job.get("status") in {"queued", "retry_backoff", "running"})
        or (folder_scan_job and folder_scan_job.get("status") in {"queued", "running"})
    )
    return {
        "prefix": normalized_prefix,
        "polling_active": polling_active,
        "calibration_status": calibration_job.get("status") if calibration_job else "idle",
        "folder_scan_status": folder_scan_job.get("status") if folder_scan_job else "idle",
        "calibration_job": calibration_job,
        "retryable_sample_job": retryable_sample_job,
        "folder_scan_job": folder_scan_job,
        "workflow_state": workflow_state,
    }


def _serialize_cache_key(cache_key: tuple[str, int, int]) -> str:
    return ":".join(str(part) for part in cache_key[1:])
