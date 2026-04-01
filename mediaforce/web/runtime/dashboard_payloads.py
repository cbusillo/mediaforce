from dataclasses import asdict
import sqlite3
from typing import Any

from mediaforce.tuning.calibration_jobs import list_queue_summary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.encoding.encode_queue import summarize_encode_queue

from mediaforce.web.runtime.folder_cards import FolderCard


def dashboard_summary_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        preview_folder_cards: Any,
        maybe_schedule_scan: Any,
        decorate_encode_queue_for_scheduler: Any,
        library_color_map_for_config: Any,
) -> dict[str, Any]:
    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        scan_job = maybe_schedule_scan(connection, config, prefix=None)
        preview_folders = preview_folder_cards(config, connection)
        calibration_queue = list_queue_summary(connection)
        encode_queue = decorate_encode_queue_for_scheduler(config, summarize_encode_queue(connection))
    return {
        "library_colors": library_color_map_for_config(config),
        "scan_job": scan_job,
        "calibration_queue": calibration_queue,
        "encode_queue": encode_queue,
        "folders_preview": [asdict(folder) for folder in preview_folders],
        "catalog_empty": not preview_folders,
        "folder_cache_key": _serialize_cache_key(cache_key),
    }


def dashboard_folders_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        list_folder_cards: Any,
) -> dict[str, Any]:
    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_folder_cards(config, connection)
    return {
        "folders": [asdict(folder) for folder in folders],
        "catalog_empty": not folders,
        "folder_cache_key": _serialize_cache_key(cache_key),
    }


def folder_status_payload(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_job_state: Any,
        load_scan_job_state: Any,
) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        calibration_job = load_job_state(connection, config, normalized_prefix)
        folder_scan_job = load_scan_job_state(config, normalized_prefix)
    polling_active = bool(
        (calibration_job and calibration_job.get("status") in {"queued", "running"})
        or (folder_scan_job and folder_scan_job.get("status") in {"queued", "running"})
    )
    return {
        "prefix": normalized_prefix,
        "polling_active": polling_active,
        "calibration_status": calibration_job.get("status") if calibration_job else "idle",
        "folder_scan_status": folder_scan_job.get("status") if folder_scan_job else "idle",
        "calibration_job": calibration_job,
        "folder_scan_job": folder_scan_job,
    }


def _serialize_cache_key(cache_key: tuple[str, int, int]) -> str:
    return ":".join(str(part) for part in cache_key[1:])
