import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import or_
from sqlalchemy import select

from mediaforce.advisor import (
    AdvisorResponse,
    TuningPolicyResponse,
)
from mediaforce.tuning.calibration_jobs import load_active_job, load_job, \
    list_queue_summary
from mediaforce.core.config import DEFAULT_CONFIG_PATH, MediaforceConfig, load_config, update_runtime_settings, \
    upsert_runtime_folder_policy_override
from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs as calibration_jobs_table
from mediaforce.core.db_tables import learning_artifacts
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.encoding.encode_queue import DEFAULT_SCHEDULER_POLICY, clear_terminal_encode_jobs_for_prefix, \
    ensure_queue_state, load_active_encode_job_for_prefix, load_latest_encode_job, \
    repair_persisted_encode_job_hosts, \
    queue_position as encode_queue_position, save_encode_job, summarize_encode_queue
from mediaforce.execution import (
    build_svt_params,
    describe_item_plan,
    detect_video_crop,
    encode_manifest_items,
    effective_video_preset,
    estimate_output_overhead_bytes,
    promote_manifest_items,
    search_quality_for_source,
    validate_manifest_items,
)
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.library.planner import build_manifest_item
from mediaforce.hosts.types import HostSetupResult
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.encoding.quality import run_sample_encode, select_quality_metric
from mediaforce.remote import (
    DEFAULT_HOST_CAPABILITIES,
    HostStatus,
    collect_host_statuses,
    prepare_remote_host_with_password,
    reset_remote_host_trust,
    run_remote_command,
    run_host_lifecycle_command,
)
from mediaforce.review import (
    encode_preview_clips,
    generate_compare_clips,
    generate_compare_clips_from_previews,
    recommend_review_timestamps,
    render_source_review_clips,
)
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.tuning.tuning_memory import (
    record_tuning_session,
    record_visual_approval_artifact,
    sibling_approved_season_memory,
)
from mediaforce.core.type_defs import JSONValue, float_value, mapping_dict, object_dict, object_list
from mediaforce.web.routes import register_completed_routes, register_dashboard_routes, register_folder_routes, \
    register_frontend_routes, register_host_routes, register_queue_routes, register_settings_routes
from mediaforce.web.runtime import FolderCard, cached_folder_cards, dashboard_folders_payload, \
    dashboard_summary_payload, default_sample_host_key, default_sample_host_key_from_statuses, \
    FolderAiTuneDeps, FolderStateDeps, FolderTuningRuntimeDeps, clear_pending_proposal, \
    archive_cleanup_summary, clear_archive_cleanup_action, \
    clear_completed_backups_action, completed_page_payload, list_completed_folders, \
    ensure_encode_host_ready, \
    folder_ai_tune_action, folder_ai_tune_confirm_action, folder_ai_tune_preview_action, \
    folder_card_cache_key, folder_status_payload, host_config_for_key, host_lifecycle_start_command, \
    host_lifecycle_start_timeout_seconds, host_lifecycle_stop_command, host_runtime_rows, \
    load_calibration_state, load_json_object, load_pending_proposal, \
    multimodal_review_pack_public_view, pause_encode_queue_action, pending_proposal_public_view, \
    planned_audio_review_context, preview_folder_cards, proposal_alignment_issue, \
    proposal_context_snapshot, proposal_signal_copy, promote_folder_outputs_action, \
    queue_folder_encode_action, recent_tuning_sessions, \
    refresh_host_status_cache, reset_folder_card_cache, resume_encode_queue_action, \
    retry_failed_encode_queue_action, review_media_context, \
    review_pack_dir, review_pair_key, review_pairs, safe_collect_host_statuses, save_advice_state, \
    save_calibration_state, save_pending_proposal, save_profile_action, \
    sample_calibration_host_statuses, sample_host_options, sample_host_options_from_statuses, \
    settings_page_payload, stop_calibration_queue_action, stop_encode_queue_action, \
    validate_folder_outputs_action, \
    build_multimodal_review_pack, build_tuning_runtime_toolbelt, load_retryable_sample_job_state
from mediaforce.web.runtime.folder_actions import ActionPayload, FolderItem
from mediaforce.web.runtime.calibration_runtime import CalibrationRunDeps, \
    restore_staged_artifact as runtime_restore_staged_artifact, \
    run_calibration_job as runtime_run_calibration_job, \
    run_full_calibration as runtime_run_full_calibration, \
    run_sampled_calibration as runtime_run_sampled_calibration, \
    remove_path as runtime_remove_path, snapshot_staged_artifact as runtime_snapshot_staged_artifact
from mediaforce.web.runtime.host_runtime import lifecycle_command_error_detail as runtime_lifecycle_command_error_detail, \
    unavailable_host_error_message as runtime_unavailable_host_error_message
from mediaforce.web.runtime.worker_leadership import WorkerLeadershipLease
from mediaforce.web.runtime.encode_runtime import EncodeQueueRuntimeDeps, \
    clear_stale_encoding_items_when_idle as runtime_clear_stale_encoding_items_when_idle, \
    encode_job_heartbeat_loop as runtime_encode_job_heartbeat_loop, \
    encode_job_manifest_totals as runtime_encode_job_manifest_totals, \
    encode_queue_worker_loop as runtime_encode_queue_worker_loop, \
    load_next_runnable_encode_job as runtime_load_next_runnable_encode_job, \
    prepare_terminal_encode_job_for_requeue as runtime_prepare_terminal_encode_job_for_requeue, \
    process_encode_queue_once as runtime_process_encode_queue_once, \
    reconcile_encode_jobs as runtime_reconcile_encode_jobs, \
    resolve_encode_job_for_display as runtime_resolve_encode_job_for_display, \
    recover_encode_queue as runtime_recover_encode_queue, \
    run_encode_job as runtime_run_encode_job, \
    select_encode_host as runtime_select_encode_host, \
    transition_encode_job_failure as runtime_transition_encode_job_failure
from mediaforce.web.runtime.encode_scheduler import EncodeSchedulerDeps, \
    decorate_encode_job_for_scheduler as runtime_decorate_encode_job_for_scheduler, \
    decorate_encode_queue_for_scheduler as runtime_decorate_encode_queue_for_scheduler, \
    encode_queue_schedule_profiles as runtime_encode_queue_schedule_profiles, \
    encode_queue_scheduler_policy as runtime_encode_queue_scheduler_policy, \
    encode_queue_summary_copy as runtime_encode_queue_summary_copy, \
    format_eta_seconds as runtime_format_eta_seconds, \
    host_schedule_now as runtime_host_schedule_now, \
    schedule_profile_policy_for_host as runtime_schedule_profile_policy_for_host, \
    scheduler_allows_encode_run as runtime_scheduler_allows_encode_run
from mediaforce.web.runtime.folder_tuning_advice import build_run_verdict_payload as runtime_build_run_verdict_payload, \
    build_seed_policy_payload as runtime_build_seed_policy_payload, \
    calibration_draft_hash as runtime_calibration_draft_hash, \
    job_seed_metadata as runtime_job_seed_metadata, metric_status_copy as runtime_metric_status_copy, \
    matching_request_history as runtime_matching_request_history, \
    metric_support as runtime_metric_support, \
    maybe_seed_baseline_policy as runtime_maybe_seed_baseline_policy, \
    operator_requested_experiment as runtime_operator_requested_experiment, \
    parse_audio_bitrate_kbps as runtime_parse_audio_bitrate_kbps, \
    record_run_verdict as runtime_record_run_verdict, review_gate as runtime_review_gate, \
    sample_audio_target_kbps as runtime_sample_audio_target_kbps, \
    seed_advice_payload as runtime_seed_advice_payload, \
    summarize_calibration_result as runtime_summarize_calibration_result, \
    tuning_advice_payload as runtime_tuning_advice_payload, \
    tuning_policy_focus as runtime_tuning_policy_focus, \
    tuning_policy_key_paths as runtime_tuning_policy_key_paths, \
    apply_policy_fragment as runtime_apply_policy_fragment
from mediaforce.web.runtime.job_runtime import JobRuntimeDeps, active_scan_from_db as runtime_active_scan_from_db, \
    CalibrationQueueRuntimeDeps, calibration_queue_worker_loop as runtime_calibration_queue_worker_loop, \
    calibration_job_belongs_to_current_process as runtime_calibration_job_belongs_to_current_process, \
    dispatch_calibration_job as runtime_dispatch_calibration_job, \
    expire_calibration_job as runtime_expire_calibration_job, \
    latest_scan_completed_at as runtime_latest_scan_completed_at, \
    load_job_state as runtime_load_job_state, load_scan_job_state as runtime_load_scan_job_state, \
    maybe_schedule_scan as runtime_maybe_schedule_scan, \
    process_calibration_queue_once as runtime_process_calibration_queue_once, \
    run_scan_job as runtime_run_scan_job, save_job_state as runtime_save_job_state, \
    save_scan_job_state as runtime_save_scan_job_state, scan_is_stale as runtime_scan_is_stale, \
    scan_job_belongs_to_current_process as runtime_scan_job_belongs_to_current_process, \
    scan_process_is_alive as runtime_scan_process_is_alive
from mediaforce.web.settings_runtime import (
    ALWAYS_SCHEDULE_PROFILE,
    DEFAULT_HOST_SCHEDULE_PROFILE,
    build_runtime_settings_payload as _build_runtime_settings_payload,
    canonical_schedule_profile_key as _canonical_schedule_profile_key,
    host_max_parallel_encodes as _host_max_parallel_encodes,
    host_schedule_profile_key as _host_schedule_profile_key,
    library_color_map_for_config as _library_color_map_for_config,
    merge_runtime_settings_payload as _merge_runtime_settings_payload,
    normalize_encode_queue_scheduler as _normalize_encode_queue_scheduler,
    settings_archive_root as _settings_archive_root_runtime,
    settings_library_rows_for_config as _settings_library_rows_for_config_runtime,
    settings_remote_rows_for_config as _settings_remote_rows_for_config_runtime,
    settings_transcode_root_value as _settings_transcode_root_value_runtime,
    runtime_source_roots as _runtime_source_roots,
)

LOGGER = logging.getLogger(__name__)

MIN_RECOMMENDED_SAVINGS_BYTES = 100 * 1024 * 1024
FULL_SCAN_STALE_AFTER = timedelta(minutes=15)
PREFIX_SCAN_STALE_AFTER = timedelta(minutes=15)
SCAN_RETRY_COOLDOWN = timedelta(minutes=5)
SCAN_INTERRUPTED_ERROR = "Background scan was interrupted by a web process restart."
CALIBRATION_JOB_NOTICE_AFTER = timedelta(hours=1)
SAMPLE_CALIBRATION_CONCURRENCY = 2
FULL_CALIBRATION_CONCURRENCY = 1
CALIBRATION_QUEUE_POLL_SECONDS = 2.0
ENCODE_QUEUE_POLL_SECONDS = 2.0
ENCODE_JOB_LEASE_SECONDS = 45
ENCODE_JOB_HEARTBEAT_SECONDS = 10.0
ENCODE_JOB_PROGRESS_WRITE_INTERVAL_SECONDS = 1.0
ENCODE_JOB_RETRY_BASE_DELAY_SECONDS = 60
ENCODE_JOB_RETRY_MAX_DELAY_SECONDS = 15 * 60
ENCODE_JOB_MAX_ATTEMPTS = 3
HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS = 30
HOST_LIFECYCLE_POLL_SECONDS = 2.0
ENCODE_HOST_COOLDOWN_SECONDS = 10 * 60
CALIBRATION_STAGED_ARTIFACT_COLUMNS = (
    "library_item_id",
    "manifest_run_id",
    "manifest_path",
    "item_index",
    "source_fingerprint",
    "staging_path",
    "staging_size_bytes",
    "staging_mtime_ns",
    "staging_fingerprint",
    "chosen_crf",
    "quality_metric",
    "quality_target",
    "quality_score",
    "encode_command_json",
    "audio_summary_json",
    "subtitle_summary_json",
    "validation_json",
    "staged_at",
    "validated_at",
    "promoted_at",
    "promoted_path",
    "archived_source_path",
    "updated_at",
)
CALIBRATION_EXECUTORS = {
    "sample": ThreadPoolExecutor(max_workers=SAMPLE_CALIBRATION_CONCURRENCY, thread_name_prefix="cal-sample"),
    "full": ThreadPoolExecutor(max_workers=FULL_CALIBRATION_CONCURRENCY, thread_name_prefix="cal-full"),
}
CALIBRATION_SUBMISSIONS: set[str] = set()
CALIBRATION_SUBMISSIONS_LOCK = threading.Lock()
CALIBRATION_QUEUE_PROCESSES: dict[str, ManagedProcessController] = {}
CALIBRATION_QUEUE_PROCESSES_LOCK = threading.Lock()
CALIBRATION_QUEUE_WORKER_LOCK = threading.Lock()
CALIBRATION_QUEUE_WORKER_STARTED = False
ENCODE_QUEUE_PROCESS = ManagedProcessController()
ENCODE_QUEUE_PROCESSES: dict[str, ManagedProcessController] = {}
ENCODE_QUEUE_PROCESSES_LOCK = threading.Lock()
ENCODE_QUEUE_WORKER_LOCK = threading.Lock()
ENCODE_QUEUE_WORKER_STARTED = False
BACKGROUND_WORKER_LEASES: dict[Path, WorkerLeadershipLease] = {}
BACKGROUND_WORKER_LEASES_LOCK = threading.Lock()


_parse_audio_bitrate_kbps = runtime_parse_audio_bitrate_kbps
_sample_audio_target_kbps = runtime_sample_audio_target_kbps
_operator_requested_experiment = runtime_operator_requested_experiment
_apply_policy_fragment = runtime_apply_policy_fragment


def _load_advice_state(config: MediaforceConfig, prefix: str) -> ActionPayload | None:
    path = _advice_file(config, prefix)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_advice_state(config: MediaforceConfig, prefix: str, patch: ActionPayload) -> ActionPayload:
    existing = _load_advice_state(config, prefix) or {}
    merged = {**existing, **patch}
    _save_advice_state(config, prefix, merged)
    return merged


_build_run_verdict_payload = runtime_build_run_verdict_payload


def _record_run_verdict(config: MediaforceConfig, prefix: str, calibration_payload: dict[str, Any]) -> None:
    runtime_record_run_verdict(
        project_root=config.paths.project_root,
        prefix=prefix,
        calibration_payload=calibration_payload,
        advice_state=_load_advice_state(config, prefix) or {},
        merge_advice_state=lambda proposal_prefix, patch: _merge_advice_state(config, proposal_prefix, patch),
        now_iso=_now_iso,
    )


def create_app(config_path: Path | None = None) -> FastAPI:
    config = load_config(config_path or DEFAULT_CONFIG_PATH)
    cleanup_lock = threading.Lock()

    @asynccontextmanager
    async def _app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        purge_transient_artifacts(config, force=True)
        with open_db(config.paths.db_path) as connection:
            repaired_host_rows = repair_persisted_encode_job_hosts(connection)
            if repaired_host_rows:
                LOGGER.warning("Repaired %s persisted encode job host payloads.", repaired_host_rows)
            ensure_queue_state(connection, updated_at=_now_iso())
            _recover_calibration_jobs(connection, config)
            _recover_encode_queue(connection, config)
        _start_background_workers(config)
        _refresh_host_status_cache(config)
        try:
            yield
        finally:
            for controller in _active_calibration_process_controllers():
                controller.cancel()
            _cancel_active_encode_processes()

    app = FastAPI(title="Mediaforce Calibration Bench", lifespan=_app_lifespan)
    review_dir = config.paths.review_dir
    packaged_frontend_build_dir = Path(__file__).resolve().parent / "frontend_build"
    frontend_build_dir = packaged_frontend_build_dir if packaged_frontend_build_dir.exists() else config.paths.project_root / "frontend" / "build"
    review_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/review-media", StaticFiles(directory=str(review_dir)), name="review_media")
    frontend_app_dir = frontend_build_dir / "_app"
    if frontend_app_dir.exists():
        app.mount("/_app", StaticFiles(directory=str(frontend_app_dir)), name="frontend_app")
    app.state.config = config

    @app.middleware("http")
    async def periodic_cleanup(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        _run_periodic_cleanup(config, cleanup_lock)
        return await call_next(request)

    def _settings_page_payload(
            include_archive_cleanup: bool = True,
            *,
            error: str | None = None,
            saved: bool = False,
            host_notice: str | None = None,
            host_notice_kind: str | None = None,
            libraries: list[dict[str, Any]] | None = None,
            remote_hosts: list[dict[str, str]] | None = None,
            transcode_root: str | None = None,
            encode_queue_scheduler: dict[str, Any] | None = None,
            schedule_profiles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return settings_page_payload(
            config,
            encode_queue_scheduler_policy=_encode_queue_scheduler_policy,
            normalize_encode_queue_scheduler=_normalize_encode_queue_scheduler,
            archive_cleanup_summary_fn=archive_cleanup_summary,
            include_archive_cleanup=include_archive_cleanup,
            error=error,
            saved=saved,
            host_notice=host_notice,
            host_notice_kind=host_notice_kind,
            libraries=libraries,
            remote_hosts=remote_hosts,
            transcode_root=transcode_root,
            encode_queue_scheduler=encode_queue_scheduler,
            schedule_profiles=schedule_profiles,
        )

    def _dashboard_summary_payload() -> dict[str, Any]:
        return dashboard_summary_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            preview_folder_cards=_preview_folder_cards,
            maybe_schedule_scan=_maybe_schedule_scan,
            decorate_encode_queue_for_scheduler=_decorate_encode_queue_for_scheduler,
            library_color_map_for_config=_library_color_map_for_config,
            archive_cleanup_summary_fn=archive_cleanup_summary,
        )

    def _dashboard_folders_payload() -> dict[str, Any]:
        return dashboard_folders_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            list_folder_cards=_list_folder_cards,
        )

    def _dashboard_api_payload() -> dict[str, Any]:
        metric_support = _metric_support()
        return {
            **_dashboard_summary_payload(),
            "metric_support": dict(metric_support),
            "metric_status_copy": _metric_status_copy(metric_support),
        }

    def _folder_status_payload(normalized_prefix: str) -> dict[str, Any]:
        return folder_status_payload(
            config,
            normalized_prefix,
            load_job_state=_load_job_state,
            load_retryable_sample_job_state=_load_retryable_sample_job_state,
            load_scan_job_state=_load_scan_job_state,
            load_active_encode_job_for_prefix=load_active_encode_job_for_prefix,
        )

    def _save_settings_action(
            *,
            libraries: list[dict[str, str]],
            remote_hosts: list[dict[str, Any]],
            transcode_root: str,
            encode_queue_scheduler: dict[str, Any],
            schedule_profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nonlocal config
        if not transcode_root:
            raise ValueError("A transcode folder is required.")
        payload = _build_runtime_settings_payload(
            libraries=libraries,
            remote_hosts=remote_hosts,
            transcode_root=transcode_root,
            encode_queue_scheduler=encode_queue_scheduler,
            schedule_profiles=schedule_profiles,
        )
        libraries_changed = False

        def _apply_runtime_settings(existing_runtime_settings: dict[str, Any]) -> dict[str, Any]:
            nonlocal libraries_changed
            merged_runtime_settings = _merge_runtime_settings_payload(existing_runtime_settings, payload)
            libraries_changed = _runtime_source_roots(existing_runtime_settings) != _runtime_source_roots(
                merged_runtime_settings)
            return merged_runtime_settings

        update_runtime_settings(config.paths.runtime_settings_path, _apply_runtime_settings)
        config = load_config(config.paths.config_path)
        app.state.config = config
        if libraries_changed:
            _reset_folder_card_cache()
            with open_db(config.paths.db_path) as connection:
                _maybe_schedule_scan(connection, config, prefix=None)
        _refresh_host_status_cache(config)
        return {"ok": True, "message": "Settings saved.", "settings": _settings_page_payload(saved=True)}

    def _prepare_host_action(host_key: str, remote_password: str | None = None) -> dict[str, Any]:
        nonlocal config
        result = prepare_remote_host_with_password(config, host_key, password=remote_password or None)
        return _host_action_result(result)

    def _start_host_action(host_key: str) -> dict[str, Any]:
        nonlocal config
        host = host_config_for_key(config, host_key)
        if not host:
            return _host_action_result(HostSetupResult(ok=False, message="Remote host is no longer configured"))
        try:
            started = ensure_encode_host_ready(
                config,
                host,
                lifecycle_command_timeout_seconds=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS,
                lifecycle_poll_seconds=HOST_LIFECYCLE_POLL_SECONDS,
            )
        except RuntimeError as exc:
            return _host_action_result(HostSetupResult(ok=False, message=str(exc)))
        label = str(host.get("label") or host.get("host") or host_key).strip() or host_key
        if started:
            message = f"{label} accepted the start command and is reachable now."
        else:
            message = f"{label} was already reachable."
        return _host_action_result(HostSetupResult(ok=True, message=message))

    def _reset_host_trust_action(host_key: str) -> dict[str, Any]:
        nonlocal config
        result = reset_remote_host_trust(config, host_key)
        return _host_action_result(result)

    def _host_action_result(result: HostSetupResult) -> dict[str, Any]:
        nonlocal config
        config = load_config(config.paths.config_path)
        app.state.config = config
        _refresh_host_status_cache(config)
        notice = result.message if not result.detail else f"{result.message} Details: {result.detail}"
        return {"ok": result.ok, "message": notice, "kind": "success" if result.ok else "error"}

    def _hosts_payload(compact: int = 0) -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            hosts = _host_runtime_rows(connection, config)
        return {"compact": bool(compact), "hosts": hosts}

    register_dashboard_routes(
        app,
        dashboard_payload=_dashboard_api_payload,
        dashboard_folders_payload=_dashboard_folders_payload,
    )
    register_settings_routes(
        app,
        settings_payload=_settings_page_payload,
        save_settings_action=_save_settings_action,
        archive_cleanup_payload=lambda transcode_root=None: archive_cleanup_summary(
            config,
            transcode_root=transcode_root,
        ),
        clear_archive_cleanup_action=lambda transcode_root=None: clear_archive_cleanup_action(
            config,
            transcode_root=transcode_root,
        ),
    )
    register_completed_routes(
        app,
        completed_payload=lambda: _completed_page_payload(),
        clear_completed_backups_action=lambda prefixes=None: _clear_completed_backups_action(prefixes),
    )
    register_host_routes(
        app,
        hosts_payload=_hosts_payload,
        start_host_action=_start_host_action,
        prepare_host_action=_prepare_host_action,
        reset_host_trust_action=_reset_host_trust_action,
    )

    def _folder_content_payload(normalized_prefix: str) -> tuple[dict[str, Any], int]:
        with open_db(config.paths.db_path) as connection:
            calibration_job = _load_job_state(connection, config, normalized_prefix)
            if calibration_job and calibration_job.get("status") in {"queued", "running"}:
                existing_scan_job = _load_scan_job_state(config, normalized_prefix)
                folder_scan_job = (
                    existing_scan_job if existing_scan_job and existing_scan_job.get("status") in {"queued",
                                                                                                   "running"} else None
                )
            else:
                folder_scan_job = _maybe_schedule_scan(connection, config, prefix=normalized_prefix)
            summary = inspect_prefix(connection, config, normalized_prefix)
            metric_support = _metric_support()
            base_context: dict[str, Any] = {
                "prefix": normalized_prefix,
                "calibration_job": calibration_job,
                "folder_scan_job": folder_scan_job,
                "metric_support": metric_support,
                "metric_status_copy": _metric_status_copy(metric_support),
            }
            if summary.get("item_count", 0) == 0:
                status_code = 200 if folder_scan_job and folder_scan_job.get("status") in {"queued", "running"} else 404
                return {**base_context, "pending": True, "scan_job": folder_scan_job}, status_code
            sample_item = _sample_item(connection, config, normalized_prefix)
            if sample_item is None:
                return (
                    {
                        **base_context,
                        "pending": True,
                        "scan_job": folder_scan_job,
                        "error_message": f"No sample item found for {normalized_prefix}",
                    },
                    404,
                )
            advice_state = _load_advice_state(config, normalized_prefix)
            pending_proposal_raw = _load_pending_proposal(config, normalized_prefix)
            pending_proposal = _pending_proposal_public_view(pending_proposal_raw)
            calibration = _load_calibration_state(config, normalized_prefix)
            recent_sessions = _recent_tuning_sessions(connection, normalized_prefix)
            approved_season_shortcut = sibling_approved_season_memory(
                connection,
                prefix=normalized_prefix,
            )
            review_gate = _review_gate(calibration)
            hot_spots = _preview_hotspots(sample_item, calibration)
            calibration_queue = list_queue_summary(connection, limit_per_lane=3)
            encode_job = load_latest_encode_job(connection, normalized_prefix)
            encode_job = runtime_resolve_encode_job_for_display(connection, encode_job, _encode_queue_runtime_deps())
            encode_queue = _decorate_encode_queue_for_scheduler(config, summarize_encode_queue(connection))
            if encode_job and encode_job.get("status") == "queued":
                position = encode_queue_position(connection, str(encode_job["job_id"]))
                if position is not None:
                    encode_job["queue_position"] = position[0]
                    encode_job["queue_depth"] = position[1]
            encode_queue_state = object_dict(encode_queue.get("state"))
            scheduler_policy = object_dict(encode_queue_state.get("scheduler"))
            encode_job = _decorate_encode_job_for_scheduler(config, encode_job)
            encode_queue_summary = _encode_queue_summary_copy(encode_queue, encode_queue_state, encode_job)
        policy = _folder_display_policy(
            sample_item=sample_item,
            calibration=calibration,
            pending_proposal=pending_proposal_raw,
        )
        advice_state = _backfill_multimodal_review_pack(
            config,
            normalized_prefix,
            sample_item=sample_item,
            calibration=calibration,
            advice_state=advice_state,
        )
        video_policy = object_dict(policy.get("video"))
        resolved_metric, _ = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
        sample_host_statuses = _sample_calibration_host_statuses(config)
        sample_host_key = _default_sample_host_key_from_statuses(sample_host_statuses)
        sample_host_choices = _sample_host_options_from_statuses(sample_host_statuses)
        return (
            {
                **base_context,
                "pending": False,
                "summary": summary,
                "sample_item": sample_item,
                "item_plan": describe_item_plan({**sample_item, "resolved_policy": policy}),
                "policy": policy,
                "hot_spots": hot_spots,
                "calibration": calibration,
                "advice": advice_state,
                "pending_proposal": pending_proposal,
                "recent_tuning_sessions": recent_sessions,
                "approved_season_shortcut": approved_season_shortcut,
                "review_gate": review_gate,
                "calibration_queue": calibration_queue,
                "encode_job": encode_job,
                "encode_queue": encode_queue,
                "encode_queue_state": encode_queue_state,
                "encode_queue_summary": encode_queue_summary,
                "encode_queue_scheduler": scheduler_policy,
                "resolved_metric": resolved_metric.upper(),
                "sample_host_key": sample_host_key,
                "sample_host_options": sample_host_choices,
                "sample_host_help_text": _sample_host_help_text(sample_host_choices, sample_host_key),
            },
            200,
        )

    def _completed_page_payload() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            return completed_page_payload(
                config,
                connection,
                folder_group=_folder_group,
            )

    def _clear_completed_backups_action(prefixes: list[str] | None) -> dict[str, Any]:
        archive_root = None
        try:
            archive_root = config.archive_root
        except KeyError:
            archive_root = None
        with open_db(config.paths.db_path) as connection:
            folders = list_completed_folders(
                connection,
                folder_group=_folder_group,
                archive_root=archive_root,
            )
        valid_prefixes = {folder.prefix for folder in folders}
        result = clear_completed_backups_action(
            config,
            folder_group=_folder_group,
            prefixes=prefixes,
            valid_prefixes=valid_prefixes,
        )
        result["completed"] = _completed_page_payload()
        return result

    def _recent_tuning_sessions(connection: DBClient, prefix: str, limit: int = 8) -> list[dict[str, Any]]:
        return recent_tuning_sessions(connection, prefix, load_json_object_fn=_load_json_object, limit=limit)

    def _proposal_signal_copy(
            note: str,
            operator_request: dict[str, Any] | None,
            has_calibration: bool,
            request_disposition: str | None = None,
    ) -> str:
        return proposal_signal_copy(note, operator_request, has_calibration, request_disposition)

    def _load_json_object(raw: str) -> dict[str, Any]:
        return load_json_object(raw)

    def _proposal_alignment_issue(
            *,
            operator_request: dict[str, Any] | None,
            request_disposition: str | None,
            current_policy: dict[str, Any],
            preview_policy: dict[str, Any],
    ) -> str | None:
        return proposal_alignment_issue(
            operator_request=operator_request,
            request_disposition=request_disposition,
            current_policy=current_policy,
            preview_policy=preview_policy,
        )

    def _folder_ai_tune_deps() -> FolderAiTuneDeps:
        return FolderAiTuneDeps(
            resolve_sample_host=_resolve_sample_host,
            load_job_state=_load_job_state,
            load_retryable_sample_job_state=_load_retryable_sample_job_state,
            sample_item=_sample_item,
            operator_requested_experiment=_operator_requested_experiment,
            load_calibration_state=_load_calibration_state,
            recent_tuning_sessions=_recent_tuning_sessions,
            matching_request_history=runtime_matching_request_history,
            metric_support=_metric_support,
            maybe_seed_baseline_policy=_maybe_seed_baseline_policy,
            seed_advice_payload=_seed_advice_payload,
            proposal_alignment_issue=_proposal_alignment_issue,
            now_iso=_now_iso,
            proposal_signal_copy=_proposal_signal_copy,
            proposal_context_snapshot=_proposal_context_snapshot,
            save_pending_proposal=_save_pending_proposal,
            pending_proposal_public_view=_pending_proposal_public_view,
            build_tuning_runtime_toolbelt=_build_tuning_runtime_toolbelt,
            review_pack_dir=_review_pack_dir,
            remove_path_if_exists=_remove_path_if_exists,
            build_multimodal_review_pack=_build_multimodal_review_pack,
            multimodal_review_pack_public_view=_multimodal_review_pack_public_view,
            tuning_advice_payload=_tuning_advice_payload,
            load_pending_proposal=_load_pending_proposal,
            apply_policy_fragment=_apply_policy_fragment,
            save_advice_state=_save_advice_state,
            save_job_state=_save_job_state,
            clear_pending_proposal=_clear_pending_proposal,
            record_tuning_session=record_tuning_session,
        )

    def _folder_ai_tune_preview_action(normalized_prefix: str, note: str, host_key: str) -> dict[str, Any]:
        return folder_ai_tune_preview_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            note,
            host_key,
        )

    def _folder_ai_tune_confirm_action(normalized_prefix: str, proposal_id: str) -> dict[str, Any]:
        return folder_ai_tune_confirm_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            proposal_id,
        )

    def _folder_ai_tune_action(normalized_prefix: str, note: str, host_key: str) -> dict[str, Any]:
        return folder_ai_tune_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            note,
            host_key,
        )

    def _queue_folder_encode_action(normalized_prefix: str, notes: str, bypass_schedule: bool) -> ActionPayload:
        return queue_folder_encode_action(
            config,
            normalized_prefix,
            notes,
            bypass_schedule,
            now_iso=_now_iso,
            load_job_state=_load_job_state,
            load_calibration_state=_load_calibration_state,
            review_gate=_review_gate,
            upsert_override=_upsert_override,
            load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
            clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
            prepare_terminal_encode_job_for_requeue_fn=lambda connection, job: runtime_prepare_terminal_encode_job_for_requeue(
                connection,
                job,
                deps=_encode_queue_runtime_deps(),
            ),
            save_encode_job=save_encode_job,
        )

    def _validate_folder_outputs_action(normalized_prefix: str) -> ActionPayload:
        return validate_folder_outputs_action(
            config,
            normalized_prefix,
            load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
            load_folder_staged_items_fn=_load_folder_staged_items,
            validate_manifest_items_fn=validate_manifest_items,
        )

    def _promote_folder_outputs_action(normalized_prefix: str) -> ActionPayload:
        return promote_folder_outputs_action(
            config,
            normalized_prefix,
            load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
            load_folder_staged_items_fn=_load_folder_staged_items,
            promote_manifest_items_fn=promote_manifest_items,
        )

    def _save_profile_action(
            normalized_prefix: str,
            confirm_high_impact: bool,
            reviewed_draft_hash: str,
    ) -> ActionPayload:
        return save_profile_action(
            config,
            normalized_prefix,
            now_iso=_now_iso,
            load_sample_item=_sample_item,
            load_calibration_state=_load_calibration_state,
            calibration_draft_hash=_calibration_draft_hash,
            save_calibration_state=_save_calibration_state,
            load_advice_state=_load_advice_state,
            record_visual_approval_artifact=record_visual_approval_artifact,
            merge_advice_state=_merge_advice_state,
            upsert_override=_upsert_override,
            auto_queue_folder_encode=_queue_folder_encode_action,
            confirm_high_impact=confirm_high_impact,
            reviewed_draft_hash=reviewed_draft_hash,
        )

    def _pause_encode_queue_action() -> dict[str, Any]:
        return pause_encode_queue_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            now_iso=_now_iso,
        )

    def _resume_encode_queue_action() -> dict[str, Any]:
        return resume_encode_queue_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            now_iso=_now_iso,
        )

    def _stop_encode_queue_action() -> dict[str, Any]:
        return stop_encode_queue_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            now_iso=_now_iso,
            cancel_queue_process=_cancel_active_encode_processes,
            sweep_orphaned_encode_processes=lambda: _sweep_orphaned_encode_processes(config),
            clear_stale_encoding_items=lambda: _clear_stale_encoding_items_when_idle(config),
        )

    def _retry_failed_encode_queue_action() -> dict[str, Any]:
        return retry_failed_encode_queue_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            load_calibration_state=_load_calibration_state,
            review_gate=_review_gate,
            queue_folder_encode_action=_queue_folder_encode_action,
        )

    def _stop_calibration_queue_action() -> dict[str, Any]:
        return stop_calibration_queue_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            now_iso=_now_iso,
            active_calibration_process_controllers=_active_calibration_process_controllers,
            load_job_state=_load_job_state,
            save_job_state=_save_job_state,
        )

    def _clear_folder_tuning_action(normalized_prefix: str) -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            return _clear_folder_tuning_state(
                connection,
                config=config,
                prefix=normalized_prefix,
            )

    register_folder_routes(
        app,
        folder_status_payload=_folder_status_payload,
        folder_content_payload=_folder_content_payload,
        download_review_compare_action=lambda prefix: _download_review_compare_action(
            config,
            prefix,
        ),
        folder_ai_tune_action=_folder_ai_tune_action,
        folder_ai_tune_preview_action=_folder_ai_tune_preview_action,
        folder_ai_tune_confirm_action=_folder_ai_tune_confirm_action,
        clear_folder_tuning_action=_clear_folder_tuning_action,
        queue_folder_encode_action=_queue_folder_encode_action,
        validate_folder_outputs_action=_validate_folder_outputs_action,
        promote_folder_outputs_action=_promote_folder_outputs_action,
        save_profile_action=_save_profile_action,
    )
    register_queue_routes(
        app,
        pause_encode_queue_action=_pause_encode_queue_action,
        resume_encode_queue_action=_resume_encode_queue_action,
        retry_failed_encode_queue_action=_retry_failed_encode_queue_action,
        stop_encode_queue_action=_stop_encode_queue_action,
        stop_calibration_queue_action=_stop_calibration_queue_action,
    )

    def _frontend_index_path() -> Path:
        index_path = frontend_build_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Frontend build is missing. Run `npm run build` in "
                    f"{config.paths.project_root / 'frontend'} before starting the web UI."
                ),
            )
        return index_path

    register_frontend_routes(
        app,
        frontend_build_dir=frontend_build_dir,
        frontend_index_path=_frontend_index_path,
    )

    return app


def _folder_display_policy(
        *,
        sample_item: dict[str, Any],
        calibration: dict[str, Any] | None,
        pending_proposal: dict[str, Any] | None,
) -> dict[str, Any]:
    if calibration:
        calibration_policy = object_dict(calibration.get("policy"))
        if calibration_policy:
            return calibration_policy
    proposal = object_dict(pending_proposal)
    preview_policy = object_dict(proposal.get("preview_policy"))
    if preview_policy:
        return preview_policy
    current_policy = object_dict(proposal.get("current_policy"))
    if current_policy:
        return current_policy
    return object_dict(sample_item.get("resolved_policy"))


def _download_review_compare_action(config: MediaforceConfig, prefix: str) -> FileResponse:
    calibration = _load_calibration_state(config, prefix)
    if calibration is None:
        raise HTTPException(status_code=404, detail="No sampled review clips are available for this folder yet.")
    compare_clips = [
        clip_path for clip_path in _review_compare_bundle_entries(config, calibration)
        if clip_path.exists()
    ]
    if not compare_clips:
        raise HTTPException(status_code=404, detail="No side-by-side compare clips are available for this folder yet.")
    bundle_path, download_name = _build_review_compare_video(
        config=config,
        prefix=prefix,
        compare_clips=compare_clips,
    )
    return FileResponse(
        bundle_path,
        filename=download_name,
        media_type="video/quicktime",
        background=BackgroundTask(_remove_path_if_exists, bundle_path),
    )


def _build_review_compare_video(
        *,
        config: MediaforceConfig,
        prefix: str,
        compare_clips: list[Path],
) -> tuple[Path, str]:
    bundle_dir = config.paths.web_state_dir / "downloads"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix="review-bundle-", suffix=".mov", dir=bundle_dir, delete=False) as temp_file:
        bundle_path = Path(temp_file.name)
    _concat_review_compare_clips(bundle_path=bundle_path, compare_clips=compare_clips)
    download_name = f"{_download_name_slug(prefix)}-full-review-compare.mov"
    return bundle_path, download_name


def _review_compare_bundle_entries(config: MediaforceConfig, calibration: dict[str, Any]) -> list[Path]:
    entries: list[Path] = []
    for raw_clip in object_list(calibration.get("compare_clips")):
        clip_path = _review_file_from_url(config, str(object_dict(raw_clip).get("path") or ""))
        if clip_path is None:
            continue
        entries.append(clip_path)
    if entries:
        return entries
    for raw_pair in object_list(calibration.get("review_pairs")):
        pair = object_dict(raw_pair)
        compare_clip = object_dict(pair.get("compare_clip"))
        clip_path = _review_file_from_url(config, str(compare_clip.get("path") or ""))
        if clip_path is None:
            continue
        entries.append(clip_path)
    return entries


def _concat_review_compare_clips(*, bundle_path: Path, compare_clips: list[Path]) -> None:
    list_file = bundle_path.with_suffix(".txt")
    list_file.write_text("".join(_ffmpeg_concat_file_line(path) for path in compare_clips))
    command = [
        ffmpeg_binary(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(bundle_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    _remove_path_if_exists(list_file)
    if result.returncode != 0 or not bundle_path.exists() or bundle_path.stat().st_size <= 0:
        _remove_path_if_exists(bundle_path)
        stderr = (result.stderr or "").strip()
        detail = stderr or "ffmpeg could not build the combined review video."
        raise HTTPException(status_code=500, detail=detail)


def _ffmpeg_concat_file_line(path: Path) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _download_name_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "review-bundle"


def main() -> None:
    _load_project_env_file()
    config = load_config(DEFAULT_CONFIG_PATH)
    host = _default_web_host()
    configured_port = _preferred_env("MEDIAFORCE_WEB_PORT")
    port_value = configured_port.strip() if configured_port is not None else ""
    port = int(port_value) if port_value else 8777
    reload_enabled = _default_web_reload_enabled()
    if reload_enabled:
        config_path = str(config.paths.config_path)
        os.environ.setdefault("MEDIAFORCE_CONFIG_PATH", config_path)
        uvicorn.run(
            "mediaforce.web.app:create_reloadable_app",
            host=str(host),
            port=port,
            reload=True,
            factory=True,
            log_level="info",
        )
        return
    uvicorn.run(create_app(config.paths.config_path), host=str(host), port=port, log_level="info")


def create_reloadable_app() -> FastAPI:
    configured_path = _preferred_env("MEDIAFORCE_CONFIG_PATH")
    config_value = configured_path.strip() if configured_path is not None else ""
    config_path = Path(config_value or str(DEFAULT_CONFIG_PATH)).expanduser()
    return create_app(config_path)


def _load_project_env_file() -> None:
    env_path = DEFAULT_CONFIG_PATH.parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        os.environ.setdefault(env_key, _parse_project_env_value(raw_value.strip()))


def _parse_project_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _default_web_reload_enabled() -> bool:
    explicit_value = _preferred_env("MEDIAFORCE_WEB_RELOAD")
    if explicit_value is not None:
        return explicit_value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _default_web_host() -> str:
    explicit_host = _preferred_env("MEDIAFORCE_WEB_HOST")
    if explicit_host is not None:
        return explicit_host
    return "127.0.0.1"


def _preferred_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _refresh_host_status_cache(config: MediaforceConfig) -> list[HostStatus]:
    return refresh_host_status_cache(config)


def _safe_collect_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    return safe_collect_host_statuses(config)


def _folder_card_cache_key(config: MediaforceConfig) -> tuple[str, int, int]:
    return folder_card_cache_key(config)


def _reset_folder_card_cache() -> None:
    reset_folder_card_cache()


def _list_folder_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    return cached_folder_cards(
        config,
        connection,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=_folder_group,
        age_days=_age_days,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=lambda prefix: _folder_review_badge(config, prefix),
    )


def _preview_folder_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    return preview_folder_cards(
        connection,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=_folder_group,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=lambda prefix: _folder_review_badge(config, prefix),
    )


def _host_runtime_rows(
        connection: DBClient, config: MediaforceConfig, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    return host_runtime_rows(
        connection,
        config,
        safe_collect_statuses=_safe_collect_host_statuses,
        encode_queue_schedule_profiles=_encode_queue_schedule_profiles,
        host_max_parallel_encodes=_host_max_parallel_encodes,
        host_schedule_profile_key=_host_schedule_profile_key,
        scheduler_allows_encode_run=_scheduler_allows_encode_run,
        format_eta_seconds=_format_eta_seconds,
        always_schedule_profile=ALWAYS_SCHEDULE_PROFILE,
        default_host_schedule_profile=DEFAULT_HOST_SCHEDULE_PROFILE,
        now=now,
    )


def _host_config_for_key(config: MediaforceConfig, host_key: str) -> dict[str, Any]:
    return host_config_for_key(config, host_key)


def _host_lifecycle_start_command(host: dict[str, Any] | None) -> str:
    return host_lifecycle_start_command(host)


def _host_lifecycle_stop_command(host: dict[str, Any] | None) -> str:
    return host_lifecycle_stop_command(host)


def _host_lifecycle_start_timeout_seconds(host: dict[str, Any] | None) -> int:
    return host_lifecycle_start_timeout_seconds(host)


def _fresh_host_status_for_key(config: MediaforceConfig, host_key: str) -> HostStatus | None:
    for status in collect_host_statuses(config):
        if status.key == host_key or status.label == host_key:
            return status
    return None


def _ensure_encode_host_ready(config: MediaforceConfig, host_payload: dict[str, Any] | None) -> bool:
    host = object_dict(host_payload)
    host_key = str(host.get("key") or host.get("host") or host.get("label") or "").strip()
    if not host_key:
        return False
    status = _fresh_host_status_for_key(config, host_key)
    if status is not None and status.available:
        return False
    start_command = _host_lifecycle_start_command(host)
    if not start_command:
        raise RuntimeError(runtime_unavailable_host_error_message(status))
    result = run_host_lifecycle_command(host, start_command, timeout=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(runtime_lifecycle_command_error_detail(result, "host start command failed"))
    deadline = time.monotonic() + _host_lifecycle_start_timeout_seconds(host)
    while time.monotonic() < deadline:
        refreshed = _fresh_host_status_for_key(config, host_key)
        if refreshed is not None and refreshed.available:
            return True
        time.sleep(HOST_LIFECYCLE_POLL_SECONDS)
    refreshed = _fresh_host_status_for_key(config, host_key)
    if refreshed is not None and refreshed.available:
        return True
    detail = refreshed.detail if refreshed is not None else None
    message = refreshed.message if refreshed is not None else f"Timed out waiting for {host_key}"
    raise RuntimeError(detail or message or f"Timed out waiting for {host_key}")


def _stop_encode_host_if_configured(config: MediaforceConfig, host_payload: dict[str, Any] | None) -> None:
    _ = config
    host = object_dict(host_payload)
    stop_command = _host_lifecycle_stop_command(host)
    if not stop_command:
        return
    result = run_host_lifecycle_command(host, stop_command, timeout=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(runtime_lifecycle_command_error_detail(result, "host stop command failed"))


def _default_sample_host_key(config: MediaforceConfig) -> str:
    return default_sample_host_key(config, safe_collect_statuses=_safe_collect_host_statuses)


def _default_sample_host_key_from_statuses(statuses: list[HostStatus]) -> str:
    return default_sample_host_key_from_statuses(statuses)


def _sample_calibration_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    return sample_calibration_host_statuses(config, safe_collect_statuses=_safe_collect_host_statuses)


def _sample_host_options(config: MediaforceConfig) -> list[dict[str, Any]]:
    return sample_host_options(config, safe_collect_statuses=_safe_collect_host_statuses)


def _sample_host_options_from_statuses(statuses: list[HostStatus]) -> list[dict[str, Any]]:
    return sample_host_options_from_statuses(statuses)


def _sample_host_help_text(sample_host_choices: list[dict[str, Any]], selected_key: str) -> str:
    for option in sample_host_choices:
        if str(option.get("key") or "") != selected_key:
            continue
        detail = str(option.get("detail") or "").strip()
        return detail or "Choose where sampled calibration should run."
    return "Choose where sampled calibration should run."


def _resolve_sample_host(config: MediaforceConfig, host_key: str) -> HostStatus:
    statuses = {host.key: host for host in _sample_calibration_host_statuses(config)}
    requested_key = host_key.strip()
    if requested_key in {"", "local"}:
        requested_key = _default_sample_host_key(config)
    if not requested_key:
        raise HTTPException(
            status_code=400,
            detail="Configure at least one sample-calibration host in Settings before running sampled calibration.",
        )
    host = statuses.get(requested_key)
    if host is None:
        raise HTTPException(status_code=400, detail="Unknown sampled calibration host")
    if not host.available:
        raise HTTPException(status_code=400, detail=host.message)
    return host


def _encode_queue_scheduler_from_form(form_data: dict[str, str]) -> dict[str, Any]:
    _ = form_data
    # The shared Queue Window is retired; hosts now use Always or an explicit
    # custom schedule profile.
    return _normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})


def _folder_review_badge(config: MediaforceConfig, prefix: str) -> dict[str, str | None]:
    calibration = _load_calibration_state(config, prefix)
    if calibration is None:
        return {"label": None, "tone": None}

    review_gate = _review_gate(calibration)
    if review_gate.get("status") == "accepted":
        return {"label": "Approved draft", "tone": "ok"}

    if bool(calibration.get("browser_review_ready")):
        return {"label": "Ready to review", "tone": "attention"}

    if bool(calibration.get("review_media_ready")):
        return {"label": "Refresh review", "tone": "warning"}

    if str(calibration.get("mode") or "sample") == "sample":
        return {"label": "Sample needs rerun", "tone": "warning"}

    return {"label": None, "tone": None}


def _sample_item(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    base_query = (
        select(library_items)
        .where(library_items.c.rel_path.like(f"{prefix}%"))
        .order_by(library_items.c.priority_score.desc(), library_items.c.size_bytes.desc())
        .limit(1)
    )
    row = connection.execute(
        base_query.where(library_items.c.status.in_(("discovered", "planned", "validated", "encoded")))
    ).mappings().fetchone()
    if row is None:
        row = connection.execute(base_query).mappings().fetchone()
    if row is None:
        return None
    return build_manifest_item(mapping_dict(row), config)


def _prefix_descendant_like_pattern(prefix: str) -> str:
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}/%"


def _load_folder_staged_items(
        connection: DBClient,
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        statuses: set[str],
) -> list[FolderItem]:
    normalized_prefix = normalized_prefix.strip().strip("/")
    if not normalized_prefix or not statuses:
        return []
    descendant_pattern = _prefix_descendant_like_pattern(normalized_prefix)
    rows = connection.execute(
        select(library_items)
        .join(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)
        .where(
            or_(
                library_items.c.parent_dir == normalized_prefix,
                library_items.c.parent_dir.like(descendant_pattern, escape="\\"),
            )
        )
        .where(library_items.c.status.in_(tuple(sorted(statuses))))
        .where(staged_artifacts.c.staging_path.is_not(None))
        .where(staged_artifacts.c.promoted_at.is_(None))
        .order_by(library_items.c.rel_path.asc())
    ).mappings().fetchall()
    items: list[FolderItem] = []
    for row in rows:
        item = build_manifest_item(mapping_dict(row), config)
        item["staging_host_label"] = str(row.get("encode_host_label") or "").strip() or None
        item["staging_host_key"] = str(row.get("encode_host_key") or "").strip() or None
        item["staging_media_access"] = str(row.get("encode_media_access") or "").strip() or None
        items.append(item)
    return items


_metric_support = runtime_metric_support
_metric_status_copy = runtime_metric_status_copy
_tuning_policy_focus = runtime_tuning_policy_focus
_tuning_policy_key_paths = runtime_tuning_policy_key_paths


def _build_seed_policy_payload(
        *,
        prefix: str,
        user_note: str,
        base_policy: dict[str, Any],
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        metric_support: dict[str, bool],
        recent_sessions_payload: list[dict[str, Any]] | None = None,
        requested_experiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return runtime_build_seed_policy_payload(
        prefix=prefix,
        user_note=user_note,
        base_policy=base_policy,
        sample_item=sample_item,
        summary=summary,
        metric_support_payload=metric_support,
        recent_sessions_payload=recent_sessions_payload,
        requested_experiment=requested_experiment,
    )


def _folder_tuning_runtime_deps() -> FolderTuningRuntimeDeps:
    return FolderTuningRuntimeDeps(
        tuning_policy_focus=_tuning_policy_focus,
        summarize_calibration_result=_summarize_calibration_result,
        tuning_policy_key_paths=_tuning_policy_key_paths,
        review_media_context=_review_media_context,
        review_file_from_url=_review_file_from_url,
        review_url=_review_url,
        slug=_slug,
    )


def _proposal_context_snapshot(
        *,
        goal: str,
        current_policy: dict[str, Any],
        sample_item: dict[str, Any],
        runtime_toolbelt: dict[str, Any] | None = None,
        learning_context: list[dict[str, Any]] | None = None,
        recent_calibration: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        metric_support: dict[str, Any] | None = None,
        requested_experiment: dict[str, Any] | None = None,
        multimodal_review_pack: dict[str, Any] | None = None,
        review_artifact_critique: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return proposal_context_snapshot(
        _folder_tuning_runtime_deps(),
        goal=goal,
        current_policy=current_policy,
        sample_item=sample_item,
        runtime_toolbelt=runtime_toolbelt,
        learning_context=learning_context,
        recent_calibration=recent_calibration,
        summary=summary,
        metric_support=metric_support,
        requested_experiment=requested_experiment,
        multimodal_review_pack=multimodal_review_pack,
        review_artifact_critique=review_artifact_critique,
    )


def _build_tuning_runtime_toolbelt(
        *,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
        calibration: dict[str, Any] | None,
        metric_support: dict[str, bool],
) -> dict[str, Any]:
    return build_tuning_runtime_toolbelt(
        _folder_tuning_runtime_deps(),
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        metric_support=metric_support,
    )


def _build_multimodal_review_pack(
        *,
        config: MediaforceConfig,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
        calibration: dict[str, Any] | None,
        output_dir: Path,
) -> dict[str, Any] | None:
    return build_multimodal_review_pack(
        _folder_tuning_runtime_deps(),
        config=config,
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        output_dir=output_dir,
    )


def _review_pack_dir(config: MediaforceConfig, prefix: str, request_id: str | None = None) -> Path:
    return review_pack_dir(_folder_tuning_runtime_deps(), config, prefix, request_id)


def _multimodal_review_pack_public_view(
        config: MediaforceConfig,
        review_pack: dict[str, Any] | None,
) -> dict[str, Any] | None:
    return multimodal_review_pack_public_view(_folder_tuning_runtime_deps(), config, review_pack)


def _backfill_multimodal_review_pack(
        config: MediaforceConfig,
        prefix: str,
        *,
        sample_item: dict[str, Any],
        calibration: dict[str, Any] | None,
        advice_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    calibration_payload = object_dict(calibration)
    if not calibration_payload:
        return advice_state
    existing_pack = object_dict(object_dict(advice_state).get("multimodal_review_pack"))
    if existing_pack:
        if isinstance(calibration, dict):
            calibration["advice"] = {**object_dict(calibration.get("advice")), "multimodal_review_pack": existing_pack}
        return object_dict(advice_state) or advice_state
    if not bool(calibration_payload.get("review_media_ready")):
        return advice_state
    stored_sample_item = object_dict(calibration_payload.get("sample_item")) or object_dict(sample_item)
    if not stored_sample_item:
        return advice_state
    current_policy = object_dict(calibration_payload.get("policy")) or object_dict(stored_sample_item.get("resolved_policy"))
    if not current_policy:
        return advice_state
    draft_hash = str(calibration_payload.get("draft_hash") or _calibration_draft_hash(calibration_payload)).strip()
    request_id = f"legacy-{draft_hash[:12]}" if draft_hash else "legacy-review-pack"
    review_pack = _build_multimodal_review_pack(
        config=config,
        sample_item=stored_sample_item,
        current_policy=current_policy,
        calibration=calibration_payload,
        output_dir=_review_pack_dir(config, prefix, request_id),
    )
    public_review_pack = _multimodal_review_pack_public_view(config, review_pack)
    if public_review_pack is None:
        return advice_state
    merged_advice_state = _merge_advice_state(config, prefix, {"multimodal_review_pack": public_review_pack})
    if isinstance(calibration, dict):
        calibration["advice"] = {**object_dict(calibration.get("advice")), "multimodal_review_pack": public_review_pack}
    return merged_advice_state


def _planned_audio_review_context(*, sample_item: dict[str, Any], current_policy: dict[str, Any]) -> dict[str, Any]:
    return planned_audio_review_context(sample_item=sample_item, current_policy=current_policy)


def _encode_scheduler_deps() -> EncodeSchedulerDeps:
    return EncodeSchedulerDeps(
        normalize_encode_queue_scheduler=_normalize_encode_queue_scheduler,
        canonical_schedule_profile_key=_canonical_schedule_profile_key,
        default_host_schedule_profile=DEFAULT_HOST_SCHEDULE_PROFILE,
        always_schedule_profile=ALWAYS_SCHEDULE_PROFILE,
        default_scheduler_policy=DEFAULT_SCHEDULER_POLICY,
        encode_job_manifest_totals=runtime_encode_job_manifest_totals,
        encode_job_max_attempts=ENCODE_JOB_MAX_ATTEMPTS,
    )


def _encode_queue_scheduler_policy(config: MediaforceConfig) -> dict[str, Any]:
    return runtime_encode_queue_scheduler_policy(config, _encode_scheduler_deps())


def _encode_queue_schedule_profiles(config: MediaforceConfig) -> dict[str, dict[str, Any]]:
    return runtime_encode_queue_schedule_profiles(config, _encode_scheduler_deps())


def _schedule_profile_policy_for_host(config: MediaforceConfig, host_payload: dict[str, Any] | None) -> dict[str, Any]:
    return runtime_schedule_profile_policy_for_host(config, host_payload, _encode_scheduler_deps())


def _host_schedule_now(current: datetime, host_payload: dict[str, Any] | None) -> datetime:
    return runtime_host_schedule_now(current, host_payload)


def _scheduler_allows_encode_run(
        policy: dict[str, Any],
        *,
        bypass_schedule: bool = False,
        now: datetime | None = None,
        host_payload: dict[str, Any] | None = None,
) -> bool:
    return runtime_scheduler_allows_encode_run(
        policy,
        _encode_scheduler_deps(),
        bypass_schedule=bypass_schedule,
        now=now,
        host_payload=host_payload,
    )


def _format_eta_seconds(seconds: float | None) -> str | None:
    return runtime_format_eta_seconds(seconds)


def _job_host_key(job: dict[str, Any]) -> str:
    host = object_dict(job.get("host"))
    return str(host.get("key") or host.get("host") or host.get("label") or "").strip()


def _decorate_encode_job_for_scheduler(config: MediaforceConfig, job: dict[str, Any] | None) -> dict[str, Any] | None:
    return runtime_decorate_encode_job_for_scheduler(config, job, _encode_scheduler_deps())


def _decorate_encode_queue_for_scheduler(config: MediaforceConfig, encode_queue: dict[str, Any]) -> dict[str, Any]:
    return runtime_decorate_encode_queue_for_scheduler(config, encode_queue, _encode_scheduler_deps())


def _state_web_dir(config: MediaforceConfig) -> Path:
    state_dir = config.paths.web_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _calibration_file(config: MediaforceConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.json"


def _advice_file(config: MediaforceConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.advice.json"


def _proposal_file(config: MediaforceConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.proposal.json"


def _job_file(config: MediaforceConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.job.json"


def _scan_job_file(config: MediaforceConfig, prefix: str | None) -> Path:
    name = "full-catalog" if prefix is None else f"prefix-{prefix}"
    return _state_web_dir(config) / f"scan-{_slug(name)}.job.json"


def _catalog_signature_file(config: MediaforceConfig) -> Path:
    return _state_web_dir(config) / "full-catalog.signature.json"


def _current_catalog_signature(config: MediaforceConfig) -> dict[str, Any]:
    return {"source_roots": _runtime_source_roots(config.raw)}


def _settings_library_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, str]]:
    return _settings_library_rows_for_config_runtime(config, min_rows=min_rows)


def _settings_remote_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    return _settings_remote_rows_for_config_runtime(config, min_rows=min_rows)


def _settings_transcode_root_value(config: MediaforceConfig) -> str:
    return _settings_transcode_root_value_runtime(config)


def _settings_archive_root(transcode_root: str) -> str:
    return _settings_archive_root_runtime(transcode_root)


def _load_catalog_signature(config: MediaforceConfig) -> dict[str, Any] | None:
    path = _catalog_signature_file(config)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_roots = payload.get("source_roots")
    if not isinstance(source_roots, dict):
        return None
    return {"source_roots": _runtime_source_roots({"media": {"source_roots": source_roots}})}


def _save_catalog_signature(config: MediaforceConfig) -> None:
    _catalog_signature_file(config).write_text(json.dumps(_current_catalog_signature(config), indent=2) + "\n")


_calibration_draft_hash = runtime_calibration_draft_hash
_review_gate = runtime_review_gate


def _encode_queue_summary_copy(
        encode_queue: dict[str, Any],
        encode_queue_state: dict[str, Any],
        encode_job: dict[str, Any] | None,
) -> str:
    return runtime_encode_queue_summary_copy(encode_queue, encode_queue_state, encode_job)


def _folder_state_deps() -> FolderStateDeps:
    return FolderStateDeps(
        review_file_from_url=_review_file_from_url,
        load_advice_state=_load_advice_state,
        calibration_draft_hash=_calibration_draft_hash,
        tuning_policy_focus=_tuning_policy_focus,
        pending_proposal_trace_public_view=_pending_proposal_trace_public_view,
    )


def _load_calibration_state(config: MediaforceConfig, prefix: str) -> ActionPayload | None:
    return load_calibration_state(_folder_state_deps(), config, prefix, _calibration_file(config, prefix))


def _save_calibration_state(config: MediaforceConfig, prefix: str, payload: ActionPayload) -> None:
    save_calibration_state(_calibration_file(config, prefix), payload, calibration_draft_hash=_calibration_draft_hash)


def _review_pairs(
        source_clips: list[dict[str, Any]],
        preview_clips: list[dict[str, Any]],
        compare_clips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return review_pairs(source_clips, preview_clips, compare_clips)


def _review_media_context(calibration: dict[str, Any] | None) -> dict[str, Any]:
    return review_media_context(calibration)


def _review_pair_key(timestamp_seconds: float) -> int:
    return review_pair_key(timestamp_seconds)


def _save_advice_state(config: MediaforceConfig, prefix: str, advice: AdvisorResponse | ActionPayload) -> None:
    save_advice_state(_advice_file(config, prefix), advice)


def _load_pending_proposal(config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return load_pending_proposal(_proposal_file(config, prefix))


def _save_pending_proposal(config: MediaforceConfig, prefix: str, payload: dict[str, Any]) -> None:
    save_pending_proposal(_proposal_file(config, prefix), payload)


def _clear_pending_proposal(config: MediaforceConfig, prefix: str) -> None:
    clear_pending_proposal(_proposal_file(config, prefix))


def _pending_proposal_public_view(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    return pending_proposal_public_view(_folder_state_deps(), payload)


def _pending_proposal_trace_public_view(trace: dict[str, Any]) -> dict[str, Any] | None:
    if not trace:
        return None
    proposed_policy = trace.get("proposed_policy")
    context = object_dict(trace.get("context"))
    current_policy = object_dict(context.get("current_policy"))
    if current_policy:
        context["current_policy"] = _tuning_policy_focus(current_policy)
    proposed_policy_payload = object_dict(proposed_policy)
    return {
        "prompt_version": trace.get("prompt_version"),
        "raw_response": trace.get("raw_response"),
        "proposed_policy": _tuning_policy_focus(proposed_policy_payload) if proposed_policy_payload else {},
        "context": context,
    }


def _job_runtime_deps() -> JobRuntimeDeps:
    return JobRuntimeDeps(
        parse_iso=_parse_iso,
        now_iso=_now_iso,
        run_scan_job=_run_scan_job,
        scan_process_is_alive=_scan_process_is_alive,
        current_catalog_signature=_current_catalog_signature,
        load_catalog_signature=_load_catalog_signature,
        load_scan_job_state=_load_scan_job_state,
        save_scan_job_state=_save_scan_job_state,
        calibration_job_notice_after=CALIBRATION_JOB_NOTICE_AFTER,
        full_scan_stale_after=FULL_SCAN_STALE_AFTER,
        prefix_scan_stale_after=PREFIX_SCAN_STALE_AFTER,
        scan_retry_cooldown=SCAN_RETRY_COOLDOWN,
        scan_interrupted_error=SCAN_INTERRUPTED_ERROR,
        save_catalog_signature=_save_catalog_signature,
        reset_folder_card_cache=_reset_folder_card_cache,
    )


def _calibration_queue_runtime_deps() -> CalibrationQueueRuntimeDeps:
    return CalibrationQueueRuntimeDeps(
        now_iso=_now_iso,
        run_calibration_job=_run_calibration_job,
        job_seed_metadata=_job_seed_metadata,
        save_job_state=_save_job_state,
        mark_calibration_submission_complete=_mark_calibration_submission_complete,
        register_calibration_process_controller=_register_calibration_process_controller,
        unregister_calibration_process_controller=_unregister_calibration_process_controller,
        submission_cleanup_callback=_submission_cleanup_callback,
        calibration_submissions=CALIBRATION_SUBMISSIONS,
        calibration_submissions_lock=CALIBRATION_SUBMISSIONS_LOCK,
        calibration_executors=CALIBRATION_EXECUTORS,
        sample_calibration_concurrency=SAMPLE_CALIBRATION_CONCURRENCY,
        full_calibration_concurrency=FULL_CALIBRATION_CONCURRENCY,
        calibration_queue_poll_seconds=CALIBRATION_QUEUE_POLL_SECONDS,
    )


def _encode_queue_runtime_deps() -> EncodeQueueRuntimeDeps:
    return EncodeQueueRuntimeDeps(
        load_config=load_config,
        now_iso=_now_iso,
        parse_iso=_parse_iso,
        host_runtime_rows=_host_runtime_rows,
        schedule_profile_policy_for_host=_schedule_profile_policy_for_host,
        scheduler_allows_encode_run=_scheduler_allows_encode_run,
        host_lifecycle_start_command=_host_lifecycle_start_command,
        ensure_encode_host_ready=_ensure_encode_host_ready,
        stop_encode_host_if_configured=_stop_encode_host_if_configured,
        encode_manifest_items=encode_manifest_items,
        dispatch_encode_job=_dispatch_encode_job,
        active_encode_process_controllers=_active_encode_process_controllers,
        logger=LOGGER,
        encode_queue_poll_seconds=ENCODE_QUEUE_POLL_SECONDS,
        encode_job_lease_seconds=ENCODE_JOB_LEASE_SECONDS,
        encode_job_heartbeat_seconds=ENCODE_JOB_HEARTBEAT_SECONDS,
        encode_job_progress_write_interval_seconds=ENCODE_JOB_PROGRESS_WRITE_INTERVAL_SECONDS,
        encode_job_retry_base_delay_seconds=ENCODE_JOB_RETRY_BASE_DELAY_SECONDS,
        encode_job_retry_max_delay_seconds=ENCODE_JOB_RETRY_MAX_DELAY_SECONDS,
        encode_job_max_attempts=ENCODE_JOB_MAX_ATTEMPTS,
        encode_host_cooldown_seconds=ENCODE_HOST_COOLDOWN_SECONDS,
    )


def _calibration_run_deps() -> CalibrationRunDeps:
    return CalibrationRunDeps(
        now_iso=_now_iso,
        load_job_state=_load_job_state,
        sample_item=_sample_item,
        save_job_state=_save_job_state,
        save_calibration_state=_save_calibration_state,
        record_run_verdict=_record_run_verdict,
        summarize_calibration_result=_summarize_calibration_result,
        calibration_mode_for_action=_calibration_mode_for_action,
        effective_video_preset=effective_video_preset,
        search_quality_for_source=search_quality_for_source,
        run_sample_encode=run_sample_encode,
        detect_video_crop=detect_video_crop,
        recommend_review_timestamps=recommend_review_timestamps,
        encode_preview_clips=encode_preview_clips,
        render_source_review_clips=render_source_review_clips,
        generate_compare_clips_from_previews=generate_compare_clips_from_previews,
        estimate_output_overhead_bytes=estimate_output_overhead_bytes,
        build_svt_params=build_svt_params,
        review_url=_review_url,
        encode_manifest_items=encode_manifest_items,
        validate_manifest_items=validate_manifest_items,
        generate_compare_clips=generate_compare_clips,
        staged_artifact_columns=CALIBRATION_STAGED_ARTIFACT_COLUMNS,
    )


def _load_job_state(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return runtime_load_job_state(connection, config, prefix, _job_runtime_deps())


def _load_retryable_sample_job_state(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return load_retryable_sample_job_state(connection, config, prefix, _job_runtime_deps())


def _save_job_state(
        connection: DBClient, config: MediaforceConfig, prefix: str, payload: dict[str, Any]
) -> None:
    runtime_save_job_state(connection, config, prefix, payload, _job_runtime_deps())


def _calibration_job_belongs_to_current_process(job: dict[str, Any]) -> bool:
    return runtime_calibration_job_belongs_to_current_process(job)


def _expire_calibration_job(
        connection: DBClient, config: MediaforceConfig, prefix: str, job: dict[str, Any]
) -> dict[str, Any]:
    return runtime_expire_calibration_job(connection, config, prefix, job, _job_runtime_deps())


def _load_scan_job_state(config: MediaforceConfig, prefix: str | None) -> dict[str, Any] | None:
    return runtime_load_scan_job_state(config, prefix, _scan_job_file)


def _save_scan_job_state(config: MediaforceConfig, prefix: str | None, payload: dict[str, Any]) -> None:
    runtime_save_scan_job_state(config, prefix, payload, _scan_job_file)


def _maybe_schedule_scan(
        connection: DBClient, config: MediaforceConfig, prefix: str | None
) -> dict[str, Any] | None:
    return runtime_maybe_schedule_scan(connection, config, prefix, _job_runtime_deps())


def _scan_is_stale(connection: DBClient, config: MediaforceConfig, prefix: str | None) -> bool:
    return runtime_scan_is_stale(connection, config, prefix, _job_runtime_deps())


def _latest_scan_completed_at(connection: DBClient, prefix: str | None) -> datetime | None:
    return runtime_latest_scan_completed_at(connection, prefix)


def _scan_process_is_alive(pid: JSONValue) -> bool:
    return runtime_scan_process_is_alive(pid)


def _active_scan_from_db(
        connection: DBClient, config: MediaforceConfig, prefix: str | None
) -> dict[str, Any] | None:
    return runtime_active_scan_from_db(connection, config, prefix, _job_runtime_deps())


def _parse_iso(value: JSONValue) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _scan_job_belongs_to_current_process(job: dict[str, Any]) -> bool:
    return runtime_scan_job_belongs_to_current_process(job, _scan_process_is_alive)


def _calibration_mode_for_action(action: str) -> str:
    return "full" if action == "confirm_full" else "sample"


def _adjust_bitrate(value: str, delta_kbps: int, *, minimum: int) -> str:
    stripped = value.rstrip("kK")
    bitrate = int(stripped)
    return f"{max(bitrate + delta_kbps, minimum)}k"


def _preview_hotspots(sample_item: dict[str, Any], calibration: dict[str, Any] | None) -> list[float]:
    if calibration and calibration.get("compare_clips"):
        return [
            float_value(object_dict(clip).get("timestamp_seconds"))
            for clip in object_list(calibration.get("compare_clips"))
        ]
    total_duration = float_value(sample_item.get("duration_seconds"))
    if total_duration <= 0:
        return [0.0]
    usable = max(total_duration - 8.0, 0.0)
    return [round(usable * ratio, 3) for ratio in (0.2, 0.5, 0.8)]


def _folder_group(rel_path: str) -> tuple[str, str, str, str] | None:
    parts = Path(rel_path).parts
    if len(parts) < 2:
        return None
    if parts[0] == "tv" and len(parts) >= 3:
        return "/".join(parts[:3]), f"{parts[1]} · {parts[2]}", parts[1], "Season"
    return "/".join(parts[:2]), parts[1], parts[0].title(), "Folder"


def _estimate_savings_bytes(*, size_bytes: int, video_codec: str, audio_summary_json: str) -> int:
    video_ratio = {
        "h264": 0.42,
        "mpeg4": 0.4,
        "vc1": 0.36,
        "hevc": 0.16,
        "h265": 0.16,
        "av1": 0.03,
    }.get(video_codec.lower(), 0.2)
    audio_ratio = 0.0
    try:
        audio_tracks = json.loads(audio_summary_json)
    except json.JSONDecodeError:
        audio_tracks = []
    for track in audio_tracks:
        codec_name = str(track.get("codec_name") or "").lower()
        channels = int(track.get("channels") or 0)
        if codec_name in {"dts", "dtshd", "truehd", "flac", "pcm_s16le", "pcm_s24le"}:
            audio_ratio += 0.05 if channels >= 6 else 0.025
        elif codec_name in {"ac3", "eac3"}:
            audio_ratio += 0.025 if channels >= 6 else 0.012
    total_ratio = min(video_ratio + audio_ratio, 0.7)
    return int(size_bytes * total_ratio)


@lru_cache(maxsize=4096)
def _age_days(source_path: str) -> float:
    try:
        stat_result = Path(source_path).stat()
    except OSError:
        return 0.0
    created_at = getattr(stat_result, "st_birthtime", None)
    if created_at is None:
        created_at = stat_result.st_mtime
    age_seconds = max(datetime.now(tz=UTC).timestamp() - float(created_at), 0.0)
    return age_seconds / 86400.0


def _age_multiplier(age_days: float) -> float:
    if age_days >= 730:
        return 1.25
    if age_days >= 365:
        return 1.16
    if age_days >= 180:
        return 1.08
    if age_days <= 30:
        return 0.88
    if age_days <= 90:
        return 0.95
    return 1.0


def _review_url(config: MediaforceConfig, output_path: Path) -> str:
    relative = output_path.relative_to(config.paths.review_dir)
    return f"/review-media/{relative.as_posix()}"


def _review_file_from_url(config: MediaforceConfig, value: str) -> Path | None:
    prefix = "/review-media/"
    if not value.startswith(prefix):
        return None
    candidate = (config.paths.review_dir / value.removeprefix(prefix)).resolve()
    review_root = config.paths.review_dir.resolve()
    if not candidate.is_relative_to(review_root):
        return None
    return candidate


def _review_cleanup_targets(config: MediaforceConfig, calibration_payload: dict[str, Any] | None) -> list[Path]:
    if not isinstance(calibration_payload, dict):
        return []
    targets: list[Path] = []
    seen: set[Path] = set()
    for key in ("compare_clips", "preview_clips", "source_clips"):
        for clip in object_list(calibration_payload.get(key)):
            review_file = _review_file_from_url(config, str(object_dict(clip).get("path") or ""))
            if review_file is None:
                continue
            if review_file not in seen:
                seen.add(review_file)
                targets.append(review_file)
            try:
                relative = review_file.relative_to(config.paths.review_dir)
            except ValueError:
                continue
            if not relative.parts:
                continue
            root = config.paths.review_dir / relative.parts[0]
            if root in seen:
                continue
            seen.add(root)
            targets.append(root)
    return sorted(targets, key=lambda path: len(path.parts), reverse=True)


def _remove_path_if_exists(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _prune_empty_parents(path: Path, *, stop_at: Path) -> None:
    current = path
    stop = stop_at.resolve()
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == stop:
            return
        try:
            current.rmdir()
        except FileNotFoundError:
            current = current.parent
            continue
        except OSError:
            return
        current = current.parent


def _clear_folder_tuning_state(
        connection: DBClient,
        *,
        config: MediaforceConfig,
        prefix: str,
) -> dict[str, Any]:
    active_job = load_active_job(connection, prefix)
    if active_job is not None:
        return {"ok": False,
                "message": "A calibration job is still active for this folder. Wait for it to finish before clearing it."}

    calibration_payload = None
    calibration_path = _calibration_file(config, prefix)
    if calibration_path.exists():
        try:
            loaded = json.loads(calibration_path.read_text())
            calibration_payload = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            calibration_payload = None

    review_targets = _review_cleanup_targets(config, calibration_payload)
    review_pack_path = _review_pack_dir(config, prefix)
    review_pack_present = review_pack_path.exists()
    artifact_rows = connection.execute(
        select(learning_artifacts.c.artifact_path).where(learning_artifacts.c.prefix == prefix)
    ).mappings().fetchall()
    artifact_paths = [Path(str(row["artifact_path"])) for row in artifact_rows if
                      str(row["artifact_path"] or "").strip()]
    session_count = int(connection.execute(
        select(func.count()).select_from(tuning_sessions).where(tuning_sessions.c.prefix == prefix)
    ).scalar_one())
    job_count = int(connection.execute(
        select(func.count()).select_from(calibration_jobs_table).where(calibration_jobs_table.c.prefix == prefix)
    ).scalar_one())

    connection.execute(delete(tuning_sessions).where(tuning_sessions.c.prefix == prefix))
    connection.execute(delete(calibration_jobs_table).where(calibration_jobs_table.c.prefix == prefix))

    _remove_path_if_exists(calibration_path)
    _remove_path_if_exists(_advice_file(config, prefix))
    _remove_path_if_exists(_proposal_file(config, prefix))
    for artifact_path in artifact_paths:
        _remove_path_if_exists(artifact_path)
    for review_target in review_targets:
        _remove_path_if_exists(review_target)
        if review_target.parent != config.paths.review_dir:
            _prune_empty_parents(review_target.parent, stop_at=config.paths.review_dir)
    _remove_path_if_exists(review_pack_path)
    if review_pack_path.parent != config.paths.review_dir:
        _prune_empty_parents(review_pack_path.parent, stop_at=config.paths.review_dir)

    return {
        "ok": True,
        "message": "Cleared the tuning thread and sampled calibration artifacts for this folder.",
        "cleared": {
            "tuning_sessions": session_count,
            "calibration_jobs": job_count,
            "review_targets": len(review_targets),
            "review_pack_artifacts": int(review_pack_present),
            "learning_artifacts": len(artifact_paths),
        },
    }


def _upsert_override(file_path: Path, prefix: str, policy: ActionPayload) -> None:
    # Bench-saved profiles are local operator state, so persist them beside the
    # runtime settings rather than mutating repo-tracked TOML defaults.
    upsert_runtime_folder_policy_override(file_path, prefix, policy)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _recover_calibration_jobs(connection: DBClient, config: MediaforceConfig) -> None:
    running_rows = connection.execute(
        select(calibration_jobs_table.c.job_id)
        .where(
            calibration_jobs_table.c.status == "running",
            (calibration_jobs_table.c.owner_pid.is_(None)) | (calibration_jobs_table.c.owner_pid != os.getpid()),
        )
        .order_by(calibration_jobs_table.c.created_at, literal_column("rowid"))
    ).mappings().fetchall()
    for row in running_rows:
        payload = load_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        _expire_calibration_job(connection, config, str(payload["prefix"]), payload)


def _recover_encode_queue(connection: DBClient, config: MediaforceConfig) -> None:
    runtime_recover_encode_queue(connection, config, _encode_queue_runtime_deps())


def _reconcile_encode_jobs(
        connection: DBClient, config: MediaforceConfig, *, restart_recovery: bool = False
) -> None:
    runtime_reconcile_encode_jobs(connection, config, _encode_queue_runtime_deps(), restart_recovery=restart_recovery)


def _clear_stale_encoding_items_when_idle(config: MediaforceConfig) -> int:
    with open_db(config.paths.db_path) as connection:
        return runtime_clear_stale_encoding_items_when_idle(connection, config, _encode_queue_runtime_deps())


def _encode_job_manifest_totals(job: dict[str, Any]) -> dict[str, Any]:
    return runtime_encode_job_manifest_totals(job)


def _transition_encode_job_failure(
        connection: DBClient,
        config: MediaforceConfig,
        job: dict[str, Any],
        *,
        failure_kind: str,
        error_message: str,
) -> None:
    runtime_transition_encode_job_failure(
        connection,
        config,
        job,
        _encode_queue_runtime_deps(),
        failure_kind=failure_kind,
        error_message=error_message,
    )


def _select_encode_host(connection: DBClient, config: MediaforceConfig, job: dict[str, Any]) -> tuple[
    dict[str, Any] | None, str | None]:
    return runtime_select_encode_host(connection, config, job, _encode_queue_runtime_deps())


def _encode_job_heartbeat_loop(
        *,
        config_path: Path,
        job_id: str,
        worker_id: str,
        stop_event: threading.Event,
        process_controller: ManagedProcessController | None = None,
) -> None:
    controller = process_controller if process_controller is not None else ManagedProcessController()
    runtime_encode_job_heartbeat_loop(
        config_path=config_path,
        job_id=job_id,
        worker_id=worker_id,
        stop_event=stop_event,
        process_controller=controller,
        deps=_encode_queue_runtime_deps(),
    )


def _dispatch_calibration_job(config: MediaforceConfig, job_payload: dict[str, Any]) -> None:
    runtime_dispatch_calibration_job(config, job_payload, _calibration_queue_runtime_deps())


def _start_calibration_queue_worker(config: MediaforceConfig) -> None:
    global CALIBRATION_QUEUE_WORKER_STARTED
    with CALIBRATION_QUEUE_WORKER_LOCK:
        if CALIBRATION_QUEUE_WORKER_STARTED:
            return
        thread = threading.Thread(
            target=_calibration_queue_worker_loop,
            kwargs={"config_path": config.paths.config_path},
            daemon=True,
            name="calibration-queue-worker",
        )
        thread.start()
        CALIBRATION_QUEUE_WORKER_STARTED = True


def _calibration_queue_worker_loop(*, config_path: Path) -> None:
    runtime_calibration_queue_worker_loop(
        config_path=config_path,
        deps=_calibration_queue_runtime_deps(),
        logger=LOGGER,
    )


def _process_calibration_queue_once(*, config_path: Path) -> None:
    runtime_process_calibration_queue_once(config_path=config_path, deps=_calibration_queue_runtime_deps())


def _mark_calibration_submission_complete(job_id: str) -> None:
    with CALIBRATION_SUBMISSIONS_LOCK:
        CALIBRATION_SUBMISSIONS.discard(job_id)


def _register_calibration_process_controller(job_id: str, controller: ManagedProcessController) -> None:
    with CALIBRATION_QUEUE_PROCESSES_LOCK:
        CALIBRATION_QUEUE_PROCESSES[job_id] = controller


def _unregister_calibration_process_controller(job_id: str) -> None:
    with CALIBRATION_QUEUE_PROCESSES_LOCK:
        CALIBRATION_QUEUE_PROCESSES.pop(job_id, None)


def _active_calibration_process_controllers() -> list[ManagedProcessController]:
    with CALIBRATION_QUEUE_PROCESSES_LOCK:
        return list(CALIBRATION_QUEUE_PROCESSES.values())


def _submission_cleanup_callback(job_id: str) -> Callable[[Future[object]], None]:
    def _callback(_future: Future[object]) -> None:
        _mark_calibration_submission_complete(job_id)
        _unregister_calibration_process_controller(job_id)

    return _callback


def _maybe_seed_baseline_policy(
        *,
        config: MediaforceConfig,
        prefix: str,
        action: str,
        user_note: str,
        base_policy: dict[str, Any],
        sample_item: dict[str, Any],
        existing_calibration: dict[str, Any] | None,
        connection: DBClient,
        requested_experiment: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return runtime_maybe_seed_baseline_policy(
        config=config,
        project_root=config.paths.project_root,
        prefix=prefix,
        action=action,
        user_note=user_note,
        base_policy=base_policy,
        sample_item=sample_item,
        existing_calibration=existing_calibration,
        connection=connection,
        requested_experiment=requested_experiment,
    )


def _tuning_advice_payload(
        *,
        tuning: TuningPolicyResponse,
        note: str,
        applied_fragment: dict[str, Any],
) -> dict[str, Any]:
    return runtime_tuning_advice_payload(tuning=tuning, note=note, applied_fragment=applied_fragment)


def _seed_advice_payload(note: str, seed_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    return runtime_seed_advice_payload(note, seed_metadata)


def _job_seed_metadata(job_payload: dict[str, Any]) -> dict[str, Any] | None:
    return runtime_job_seed_metadata(job_payload)


def _summarize_calibration_result(calibration_payload: dict[str, Any]) -> dict[str, Any]:
    return runtime_summarize_calibration_result(calibration_payload)


def _start_encode_queue_worker(config: MediaforceConfig) -> None:
    global ENCODE_QUEUE_WORKER_STARTED
    with ENCODE_QUEUE_WORKER_LOCK:
        if ENCODE_QUEUE_WORKER_STARTED:
            return
        thread = threading.Thread(target=_encode_queue_worker_loop, kwargs={"config_path": config.paths.config_path},
                                  daemon=True)
        thread.start()
        ENCODE_QUEUE_WORKER_STARTED = True


def _background_worker_lease(config: MediaforceConfig) -> WorkerLeadershipLease:
    lock_path = config.paths.web_state_dir / "background-workers.lock"
    with BACKGROUND_WORKER_LEASES_LOCK:
        lease = BACKGROUND_WORKER_LEASES.get(lock_path)
        if lease is None:
            lease = WorkerLeadershipLease(lock_path, worker_name="background-workers")
            BACKGROUND_WORKER_LEASES[lock_path] = lease
        return lease


def _acquire_background_worker_leadership(config: MediaforceConfig) -> bool:
    lease = _background_worker_lease(config)
    if lease.acquire():
        return True
    owner = lease.owner_metadata() or {}
    owner_copy = ", ".join(
        f"{key}={owner[key]}"
        for key in ("pid", "hostname", "cwd", "started_at")
        if owner.get(key) is not None
    )
    if owner_copy:
        LOGGER.warning("Background workers disabled; lock %s is already held by %s.", lease.lock_path, owner_copy)
    else:
        LOGGER.warning("Background workers disabled; lock %s is already held by another process.", lease.lock_path)
    return False


def _start_background_workers(config: MediaforceConfig) -> bool:
    if not _acquire_background_worker_leadership(config):
        return False
    _start_calibration_queue_worker(config)
    _start_encode_queue_worker(config)
    return True


def _reset_background_worker_leadership_for_tests() -> None:
    with BACKGROUND_WORKER_LEASES_LOCK:
        leases = list(BACKGROUND_WORKER_LEASES.values())
        BACKGROUND_WORKER_LEASES.clear()
    for lease in leases:
        lease.release()


def _sweep_orphaned_encode_processes(config: MediaforceConfig) -> None:
    sweep_script = (
        "pids=$(pgrep -f 'mediaforce_encoded_by=mediaforce' || true); "
        "if [ -n \"$pids\" ]; then "
        "kill $pids 2>/dev/null || true; "
        "sleep 2; "
        "pids=$(pgrep -f 'mediaforce_encoded_by=mediaforce' || true); "
        "if [ -n \"$pids\" ]; then kill -9 $pids 2>/dev/null || true; fi; "
        "fi"
    )
    for host in config.remote_hosts:
        capabilities = {
            str(capability).strip().lower()
            for capability in object_list(host.get("capabilities") or list(DEFAULT_HOST_CAPABILITIES))
            if str(capability).strip()
        }
        if "encode_queue" not in capabilities:
            continue
        if str(host.get("mode") or "ssh").strip().lower() != "ssh":
            continue
        try:
            run_remote_command(host, ["sh", "-lc", sweep_script], timeout=10)
        except Exception as exc:
            host_label = str(host.get("label") or host.get("host") or host.get("key") or "remote host")
            LOGGER.warning("Orphan encode sweep failed for %s: %s", host_label, exc)


def _encode_queue_worker_loop(*, config_path: Path) -> None:
    runtime_encode_queue_worker_loop(config_path=config_path, deps=_encode_queue_runtime_deps())


def _process_encode_queue_once(*, config_path: Path) -> None:
    runtime_process_encode_queue_once(config_path=config_path, deps=_encode_queue_runtime_deps())


def _load_next_runnable_encode_job(
        connection: DBClient, config: MediaforceConfig
) -> dict[str, Any] | None:
    return runtime_load_next_runnable_encode_job(connection, config, _encode_queue_runtime_deps())


def _register_encode_process_controller(job_id: str, controller: ManagedProcessController) -> None:
    with ENCODE_QUEUE_PROCESSES_LOCK:
        ENCODE_QUEUE_PROCESSES[job_id] = controller


def _unregister_encode_process_controller(job_id: str) -> None:
    with ENCODE_QUEUE_PROCESSES_LOCK:
        ENCODE_QUEUE_PROCESSES.pop(job_id, None)


def _active_encode_process_controllers() -> list[ManagedProcessController]:
    with ENCODE_QUEUE_PROCESSES_LOCK:
        return list(ENCODE_QUEUE_PROCESSES.values())


def _cancel_active_encode_processes() -> None:
    for controller in _active_encode_process_controllers():
        controller.cancel()
    ENCODE_QUEUE_PROCESS.cancel()


def _dispatch_encode_job(*, config_path: Path, job_id: str) -> None:
    controller = ManagedProcessController()
    _register_encode_process_controller(job_id, controller)
    thread = threading.Thread(
        target=_run_encode_job,
        kwargs={"config_path": config_path, "job_id": job_id, "process_controller": controller},
        daemon=True,
        name=f"encode-job-{job_id}",
    )
    try:
        thread.start()
    except Exception:
        _unregister_encode_process_controller(job_id)
        raise


def _run_encode_job(
        *,
        config_path: Path,
        job_id: str,
        process_controller: ManagedProcessController | None = None,
) -> None:
    if process_controller is None:
        controller = ManagedProcessController()
    else:
        controller = process_controller
    try:
        runtime_run_encode_job(
            config_path=config_path,
            job_id=job_id,
            process_controller=controller,
            deps=_encode_queue_runtime_deps(),
        )
    finally:
        if process_controller is not None:
            _unregister_encode_process_controller(job_id)


def _run_periodic_cleanup(config: MediaforceConfig, cleanup_lock: threading.Lock) -> None:
    if not cleanup_lock.acquire(blocking=False):
        return
    thread = threading.Thread(
        target=_run_periodic_cleanup_task,
        args=(config, cleanup_lock),
        name="mediaforce-periodic-cleanup",
        daemon=True,
    )
    thread.start()


def _run_periodic_cleanup_task(config: MediaforceConfig, cleanup_lock: threading.Lock) -> None:
    try:
        purge_transient_artifacts(config)
    finally:
        cleanup_lock.release()


def _snapshot_staged_artifact(connection: DBClient, library_item_id: int) -> dict[str, Any] | None:
    return runtime_snapshot_staged_artifact(connection, library_item_id, CALIBRATION_STAGED_ARTIFACT_COLUMNS)


def _restore_staged_artifact(
        connection: DBClient, library_item_id: int, snapshot: dict[str, Any] | None
) -> None:
    runtime_restore_staged_artifact(connection, library_item_id, snapshot, CALIBRATION_STAGED_ARTIFACT_COLUMNS)


def _remove_path(path: Path | None) -> None:
    runtime_remove_path(path)


def _run_scan_job(*, config_path: Path, prefix: str | None, job_id: str) -> None:
    runtime_run_scan_job(config_path=config_path, prefix=prefix, job_id=job_id, deps=_job_runtime_deps())


def _run_calibration_job(
        *,
        config_path: Path,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        job_id: str,
        seed_metadata: dict[str, Any] | None,
        process_controller: ManagedProcessController,
) -> None:
    runtime_run_calibration_job(
        config_path=config_path,
        prefix=prefix,
        action=action,
        host_data=host_data,
        notes=notes,
        policy=policy,
        job_id=job_id,
        seed_metadata=seed_metadata,
        process_controller=process_controller,
        deps=_calibration_run_deps(),
    )


def _run_sampled_calibration(
        *,
        config: MediaforceConfig,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        seed_metadata: dict[str, Any] | None,
        sample_item: dict[str, Any],
        calibration_run_id: str,
        process_controller: ManagedProcessController,
) -> tuple[dict[str, Any], Path | None]:
    return runtime_run_sampled_calibration(
        config=config,
        prefix=prefix,
        action=action,
        host_data=host_data,
        notes=notes,
        policy=policy,
        seed_metadata=seed_metadata,
        sample_item=sample_item,
        calibration_run_id=calibration_run_id,
        process_controller=process_controller,
        deps=_calibration_run_deps(),
    )


def _run_full_calibration(
        *,
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        seed_metadata: dict[str, Any] | None,
        sample_item: dict[str, Any],
        calibration_run_id: str,
        process_controller: ManagedProcessController,
) -> tuple[dict[str, Any], Path, Path]:
    return runtime_run_full_calibration(
        connection=connection,
        config=config,
        prefix=prefix,
        action=action,
        host_data=host_data,
        notes=notes,
        policy=policy,
        seed_metadata=seed_metadata,
        sample_item=sample_item,
        calibration_run_id=calibration_run_id,
        process_controller=process_controller,
        deps=_calibration_run_deps(),
    )
