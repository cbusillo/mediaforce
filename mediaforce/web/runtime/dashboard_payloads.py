from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select

from mediaforce.tuning.calibration_jobs import list_queue_summary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items
from mediaforce.encoding.encode_queue import summarize_encode_queue
from mediaforce.library.media_scopes import resolve_media_scope
from mediaforce.library.movie_library import adapt_movie_workflow_payload, load_movie_scope_payload
from mediaforce.library.staged_integrity import staged_integrity_report_for_scope
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.library.candidate_selection import encode_candidate_decisions, project_candidates, workflow_eligibility
from mediaforce.web.runtime.job_runtime import calibration_job_public_payload, calibration_scope_relation


def dashboard_summary_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        preview_folder_cards: Any,
        load_scan_status: Any,
        decorate_encode_queue_for_scheduler: Any,
        library_color_map_for_config: Any,
        preview_limit: int | None = None,
) -> dict[str, Any]:
    if preview_limit is not None and preview_limit < 0:
        raise ValueError("preview_limit must be non-negative")

    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        scan_job = load_scan_status(connection, config, prefix=None)
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


def dashboard_library_payload(
        config: MediaforceConfig,
        *,
        folder_card_cache_key: Any,
        list_library_structure_cards: Any,
) -> dict[str, Any]:
    cache_key = folder_card_cache_key(config)
    with open_db(config.paths.db_path) as connection:
        folders = list_library_structure_cards(config, connection)
    return {
        "folders": [asdict(folder) for folder in folders],
        "series_folders": [],
        "catalog_empty": not folders,
        "folder_cache_key": _serialize_cache_key(cache_key),
    }


def folder_status_payload(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_exact_job_state: Any,
        load_overlapping_job_state: Any,
        load_retryable_sample_job_state: Any,
        load_scan_status: Any,
        load_active_encode_job_for_prefix: Any,
) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        media_scope = resolve_media_scope(
            connection,
            normalized_prefix,
            library_types=config.library_type_map,
        )
        exact_calibration_job = calibration_job_public_payload(
            connection,
            config,
            load_exact_job_state(connection, config, normalized_prefix),
        )
        overlapping_calibration_job = calibration_job_public_payload(
            connection,
            config,
            load_overlapping_job_state(connection, config, normalized_prefix),
        )
        scope_activity = _scope_activity_payload(
            normalized_prefix,
            exact_calibration_job,
            overlapping_calibration_job,
        )
        legacy_calibration_job = overlapping_calibration_job or exact_calibration_job
        retryable_sample_job = load_retryable_sample_job_state(connection, config, normalized_prefix)
        active_encode_job = load_active_encode_job_for_prefix(connection, normalized_prefix)
        folder_scan_job = load_scan_status(connection, config, normalized_prefix)
        lifecycle_decisions = (
            project_candidates(connection, config, prefixes=[normalized_prefix])
            if media_scope.domain == "movie"
            else encode_candidate_decisions(connection, config, prefixes=[normalized_prefix])
        )
        workflow_state = build_folder_workflow_state(
            connection,
            normalized_prefix,
            candidate_eligibility=workflow_eligibility(lifecycle_decisions),
            library_types=config.library_type_map,
        ).to_payload()
        staged_integrity = staged_integrity_report_for_scope(
            connection,
            config,
            media_scope,
            discover=False,
        ).summary_payload()
        if media_scope.domain == "movie":
            movie_context = load_movie_scope_payload(connection, config, normalized_prefix)
            if movie_context is not None:
                library = {
                    "availability": movie_context.get("availability"),
                }
                workflow_state = adapt_movie_workflow_payload(
                    workflow_state,
                    library=library,
                    members=list(movie_context.get("members") or []),
                ) or workflow_state
    polling_active = bool(
        (exact_calibration_job and exact_calibration_job.get("status") in {"queued", "starting", "running"})
        or (
            scope_activity
            and scope_activity["job"].get("status") in {"queued", "starting", "running"}
        )
        or (active_encode_job and active_encode_job.get("status") in {"queued", "retry_backoff", "running"})
        or (folder_scan_job and folder_scan_job.get("status") in {"queued", "running"})
    )
    return {
        "prefix": normalized_prefix,
        "media_scope": media_scope.to_payload(),
        "polling_active": polling_active,
        "calibration_status": legacy_calibration_job.get("status") if legacy_calibration_job else "idle",
        "exact_calibration_status": exact_calibration_job.get("status") if exact_calibration_job else "idle",
        "scope_activity_status": scope_activity["job"].get("status") if scope_activity else "idle",
        "folder_scan_status": folder_scan_job.get("status") if folder_scan_job else "idle",
        "calibration_job": legacy_calibration_job,
        "exact_calibration_job": exact_calibration_job,
        "scope_activity": scope_activity,
        "retryable_sample_job": retryable_sample_job,
        "folder_scan_job": folder_scan_job,
        "workflow_state": workflow_state,
        "staged_integrity": staged_integrity,
    }


def _scope_activity_payload(
        route_prefix: str,
        exact_job: dict[str, Any] | None,
        overlapping_job: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if overlapping_job is None:
        return None
    if exact_job and overlapping_job.get("job_id") == exact_job.get("job_id"):
        return None
    owner_prefix = str(overlapping_job.get("prefix") or "")
    relation = calibration_scope_relation(route_prefix, owner_prefix)
    if relation not in {"ancestor", "descendant"}:
        return None
    return {
        "schema_version": 1,
        "relation": relation,
        "job": overlapping_job,
    }


def _serialize_cache_key(cache_key: tuple[str, int, int]) -> str:
    return ":".join(str(part) for part in cache_key[1:])
