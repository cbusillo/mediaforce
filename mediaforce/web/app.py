import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp
from typing import Any, NamedTuple

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete
from sqlalchemy import and_, func
from sqlalchemy import literal_column
from sqlalchemy import or_
from sqlalchemy import select

from mediaforce.advisor import TuningPolicyResponse
from mediaforce.advising.routing import advisor_routing_from_config
from mediaforce.tuning.calibration_jobs import load_active_job, load_job, \
    list_queue_summary
from mediaforce.core.config import DEFAULT_CONFIG_PATH, MediaforceConfig, load_config, update_runtime_settings, \
    update_runtime_folder_policy_values, upsert_runtime_folder_policy_override
from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs as calibration_jobs_table
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import learning_artifacts
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.encoding.encode_queue import DEFAULT_SCHEDULER_POLICY, DISPLAY_ENCODE_JOB_KINDS, \
    clear_terminal_encode_jobs_for_prefix, \
    ensure_queue_state, load_active_encode_job_for_prefix, load_latest_encode_job, \
    repair_persisted_encode_job_hosts, \
    queue_position as encode_queue_position, save_encode_job, summarize_encode_queue
from mediaforce.execution import (
    build_svt_params,
    describe_item_plan,
    detect_video_crop,
    encode_manifest_items,
    effective_video_preset,
    promote_manifest_items,
    resolve_stream_budget_ledger,
    search_quality_for_source,
    validate_manifest_items,
)
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.library.media_scopes import media_group_scope_for_rel_path, resolve_media_scope, resolve_media_scopes, \
    scope_rel_path_filter, series_context_for_prefix, tv_series_scope_for_rel_path
from mediaforce.library.movie_library import load_movie_library_payload, load_movie_scope_payload
from mediaforce.library.movie_workflow import classify_movie_path, movie_item_included
from mediaforce.library.planner import build_manifest_item
from mediaforce.library.representatives import RepresentativeSelection, load_representative_selection, \
    public_representative_item
from mediaforce.library.run_manifests import select_encode_candidates
from mediaforce.library.candidate_selection import CandidateDecision, encode_candidate_decisions, project_candidates, \
    scope_lifecycle_payload_from_decisions, workflow_eligibility
from mediaforce.hosts.types import HostSetupResult
from mediaforce.hosts.config import configured_remote_host_execution_mode
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.encoding.quality import run_sample_encode, select_quality_metric
from mediaforce.remote import (
    DEFAULT_HOST_CAPABILITIES,
    HostStatus,
    collect_host_statuses,
    prepare_remote_host_with_password,
    remote_mount_recovery_supported,
    reset_remote_host_trust,
    run_remote_command,
    run_host_lifecycle_command,
)
from mediaforce.review import (
    encode_preview_clips,
    generate_compare_clips,
    generate_compare_clips_from_previews,
    recommend_review_moments,
    recommend_review_timestamps,
    review_moment_payload,
    render_source_review_clips,
)
from mediaforce.reviewing.renderers import NATIVE_COMPARE_FILTER
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.tuning.tuning_memory import (
    record_tuning_session,
    record_visual_approval_artifact,
    sibling_approved_season_memory,
)
from mediaforce.tuning.quality_risk import build_quality_risk_contract, quality_risk_public_view
from mediaforce.tuning.size_goals import guided_size_goal_options, operator_intent_from_policy
from mediaforce.core.type_defs import JSONValue, float_value, mapping_dict, object_dict, object_list
from mediaforce.web.routes import register_completed_routes, register_dashboard_routes, register_folder_routes, \
    register_frontend_routes, register_host_routes, register_queue_routes, register_settings_routes
from mediaforce.web.runtime import FolderCard, cached_folder_cards, dashboard_folders_payload, \
    dashboard_library_payload, dashboard_summary_payload, default_sample_host_key, default_sample_host_key_from_statuses, \
    FolderAiTuneDeps, FolderStateDeps, FolderTuningRuntimeDeps, clear_pending_proposal, \
    archive_cleanup_summary, clear_archive_cleanup_action, \
    clear_completed_backups_action, completed_page_payload, confirm_originals_removed_action, \
    list_completed_folders, \
    ensure_encode_host_ready, ensure_sample_host_ready, \
    folder_ai_tune_action, folder_ai_tune_confirm_action, folder_ai_tune_preview_action, \
    folder_card_cache_key, folder_status_payload, host_config_for_key, host_lifecycle_start_command, \
    host_lifecycle_start_timeout_seconds, host_lifecycle_stop_command, host_runtime_rows, \
    list_library_structure_cards, load_calibration_state, load_json_object, load_pending_proposal, \
    multimodal_review_pack_public_view, pause_encode_queue_action, pending_proposal_public_view, \
    planned_audio_review_context, preview_folder_cards, proposal_alignment_issue, \
    proposal_context_snapshot, proposal_signal_copy, promote_folder_outputs_action, \
    approve_measured_encode_recovery_action, \
    queue_folder_encode_action, recent_tuning_sessions, \
    refresh_host_status_cache, reset_folder_card_cache, resume_encode_queue_action, \
    retry_failed_encode_prefix_action, retry_failed_encode_queue_action, review_media_context, \
    review_pack_dir, review_pair_key, review_pairs, safe_collect_host_statuses, save_advice_state, \
    save_calibration_state, save_pending_proposal, save_profile_action, \
    sample_calibration_host_statuses, sample_host_options, sample_host_options_from_statuses, \
    settings_page_payload, stop_calibration_queue_action, stop_encode_queue_action, \
    validate_folder_outputs_action, \
    build_multimodal_review_pack, build_tuning_runtime_toolbelt, load_latest_failed_sample_job_state, \
    load_latest_failed_target_size_job_state, load_retryable_sample_job_state
from mediaforce.web.runtime.folder_actions import ActionPayload, FolderItem, production_action_blocker
from mediaforce.web.runtime.folder_cards import list_folder_cards
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.calibration_runtime import CalibrationRunDeps, \
    restore_staged_artifact as runtime_restore_staged_artifact, \
    run_calibration_job as runtime_run_calibration_job, \
    run_full_calibration as runtime_run_full_calibration, \
    run_sampled_calibration as runtime_run_sampled_calibration, \
    remove_path as runtime_remove_path, snapshot_staged_artifact as runtime_snapshot_staged_artifact
from mediaforce.web.runtime.host_runtime import lifecycle_command_error_detail as runtime_lifecycle_command_error_detail
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
    seed_advice_payload as runtime_seed_advice_payload, \
    summarize_calibration_result as runtime_summarize_calibration_result, \
    tuning_advice_payload as runtime_tuning_advice_payload, \
    tuning_policy_focus as runtime_tuning_policy_focus, \
    tuning_policy_key_paths as runtime_tuning_policy_key_paths, \
    apply_policy_fragment as runtime_apply_policy_fragment
from mediaforce.web.runtime.folder_tuning_helpers import size_budget_sample_analysis
from mediaforce.web.runtime.catalog_signature import (
    catalog_signature_file as _catalog_signature_file,
    current_catalog_signature as _current_catalog_signature,
    load_catalog_signature as _load_catalog_signature,
    save_catalog_signature as _save_catalog_signature,
)
from mediaforce.web.runtime.job_runtime import JobRuntimeDeps, active_scan_from_db as runtime_active_scan_from_db, \
    CalibrationQueueRuntimeDeps, calibration_queue_worker_loop as runtime_calibration_queue_worker_loop, \
    calibration_job_belongs_to_current_process as runtime_calibration_job_belongs_to_current_process, \
    dispatch_calibration_job as runtime_dispatch_calibration_job, \
    expire_calibration_job as runtime_expire_calibration_job, \
    latest_scan_completed_at as runtime_latest_scan_completed_at, \
    load_job_state as runtime_load_job_state, \
    load_overlapping_job_state as runtime_load_overlapping_job_state, \
    load_scan_job_state as runtime_load_scan_job_state, \
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
    runtime_library_signature as _runtime_library_signature,
    bind_runtime_library_overrides as _bind_runtime_library_overrides,
)

LOGGER = logging.getLogger(__name__)


class VideoGeometry(NamedTuple):
    width: int
    height: int

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
    "attachment_summary_json",
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
_operator_requested_experiment = runtime_operator_requested_experiment
_apply_policy_fragment = runtime_apply_policy_fragment


def _read_advice_state(path: Path, *, strict: bool) -> ActionPayload | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        if strict:
            raise
        return None
    if isinstance(payload, dict):
        return payload
    if strict:
        raise ValueError(f"Advice state at {path} must be a JSON object")
    return None


def _load_advice_state(config: MediaforceConfig, prefix: str) -> ActionPayload | None:
    return _read_advice_state(_advice_file(config, prefix), strict=False)


def _load_advice_state_for_queue(config: MediaforceConfig, prefix: str) -> ActionPayload | None:
    try:
        return _read_advice_state(_advice_file(config, prefix), strict=True)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Mediaforce could not read the current review evidence safely. "
                "Repair or rerun the representative test before starting production."
            ),
        ) from exc


@contextmanager
def _locked_advice_state(config: MediaforceConfig, prefix: str) -> Iterator[Path]:
    path = _advice_file(config, prefix)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _merge_quality_risk_records(existing: Any, incoming: Any) -> list[ActionPayload]:
    records = [object_dict(record) for record in object_list(existing)]
    binding_keys = ("kind", "verdict", "sample_job_id", "policy_hash", "source_id", "prefix")
    for raw_record in object_list(incoming):
        record = object_dict(raw_record)
        matching = [
            current
            for current in records
            if all(current.get(key) == record.get(key) for key in binding_keys)
        ]
        records = [
            current
            for current in records
            if not all(current.get(key) == record.get(key) for key in binding_keys)
        ]
        records.append(
            max(
                [*matching, record],
                key=lambda current: _advice_record_timestamp(current.get("created_at")),
            )
        )
    return records[-100:]


def _advice_record_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _merge_advice_state(config: MediaforceConfig, prefix: str, patch: ActionPayload) -> ActionPayload:
    with _locked_advice_state(config, prefix) as path:
        existing = _read_advice_state(path, strict=True) or {}
        merged = {**existing, **patch}
        if "quality_risk_records" in patch:
            merged["quality_risk_records"] = _merge_quality_risk_records(
                existing.get("quality_risk_records"),
                patch.get("quality_risk_records"),
            )
        save_advice_state(path, merged)
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
    advisor_routing = advisor_routing_from_config(config)
    cleanup_lock = threading.Lock()

    @asynccontextmanager
    async def _app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        threading.Thread(
            target=purge_transient_artifacts,
            args=(config,),
            kwargs={"force": True},
            name="transient-cleanup",
            daemon=True,
        ).start()
        with open_db(config.paths.db_path) as connection:
            repaired_host_rows = repair_persisted_encode_job_hosts(connection)
            if repaired_host_rows:
                LOGGER.warning("Repaired %s persisted encode job host payloads.", repaired_host_rows)
            ensure_queue_state(connection, updated_at=_now_iso())
            _recover_calibration_jobs(connection, config)
            _recover_encode_queue(connection, config)
        _start_background_workers(config)
        _safe_collect_host_statuses(config)
        try:
            yield
        finally:
            for controller in _active_calibration_process_controllers():
                controller.cancel()
            _cancel_active_encode_processes()

    app = FastAPI(title="Mediaforce Calibration Bench", lifespan=_app_lifespan)
    review_dir = config.paths.review_dir
    project_frontend_build_dir = config.paths.project_root / "frontend" / "build"
    packaged_frontend_build_dir = Path(__file__).resolve().parent / "frontend_build"
    frontend_build_dir = (
        project_frontend_build_dir if project_frontend_build_dir.exists() else packaged_frontend_build_dir
    )
    review_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/review-media", StaticFiles(directory=str(review_dir)), name="review_media")

    @app.middleware("http")
    async def ensure_review_media_root(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/review-media/"):
            review_dir.mkdir(parents=True, exist_ok=True)
        return await call_next(request)

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
            video_defaults: dict[str, Any] | None = None,
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
            video_defaults=video_defaults,
            encode_queue_scheduler=encode_queue_scheduler,
            schedule_profiles=schedule_profiles,
        )

    def _dashboard_summary_payload(preview_limit: int | None = None) -> dict[str, Any]:
        return dashboard_summary_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            preview_folder_cards=_preview_folder_cards,
            maybe_schedule_scan=_maybe_schedule_scan,
            decorate_encode_queue_for_scheduler=_decorate_encode_queue_for_scheduler,
            library_color_map_for_config=_library_color_map_for_config,
            preview_limit=preview_limit,
        )

    def _dashboard_folders_payload(include_series_folders: bool = True) -> dict[str, Any]:
        return dashboard_folders_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            list_folder_cards=_list_folder_cards,
            list_series_folder_cards=_list_series_folder_cards,
            include_series_folders=include_series_folders,
        )

    def _dashboard_library_payload() -> dict[str, Any]:
        return dashboard_library_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            list_library_structure_cards=_list_library_structure_cards,
        )

    def _dashboard_library_details_payload() -> dict[str, Any]:
        return dashboard_folders_payload(
            config,
            folder_card_cache_key=_folder_card_cache_key,
            list_folder_cards=_list_library_detail_cards,
            include_series_folders=False,
        )

    def _dashboard_movie_library_payload() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            return load_movie_library_payload(
                connection,
                config,
                include_details=False,
            )

    def _dashboard_movie_library_details_payload() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            movie_roots = {
                root
                for root, library_type in config.library_type_map.items()
                if library_type == "movie"
            }
            if not movie_roots:
                return load_movie_library_payload(
                    connection,
                    config,
                    include_details=True,
                    candidate_decisions=[],
                )
            decisions = project_candidates(connection, config, prefixes=sorted(movie_roots))
            cards = _folder_cards_for_group(
                config,
                connection,
                folder_group=lambda rel_path: _movie_folder_group(
                    rel_path,
                    library_types=config.library_type_map,
                ),
                minimum_recommended_savings_bytes=None,
                media_roots=movie_roots,
                candidate_decisions=decisions,
                include_lifecycle=False,
                include_workflow_states=False,
            )
            return load_movie_library_payload(
                connection,
                config,
                include_details=True,
                metrics_by_prefix={card.prefix: asdict(card) for card in cards},
                candidate_decisions=decisions,
            )

    def _dashboard_api_payload(preview_limit: int | None = None) -> dict[str, Any]:
        metric_support = _metric_support()
        return {
            **_dashboard_summary_payload(preview_limit),
            "metric_support": dict(metric_support),
            "metric_status_copy": _metric_status_copy(metric_support),
        }

    def _folder_status_payload(normalized_prefix: str) -> dict[str, Any]:
        return folder_status_payload(
            config,
            normalized_prefix,
            load_job_state=_load_overlapping_job_state,
            load_retryable_sample_job_state=_load_retryable_sample_job_state,
            load_scan_job_state=_load_scan_job_state,
            load_active_encode_job_for_prefix=load_active_encode_job_for_prefix,
        )

    def _save_settings_action(
            *,
            libraries: list[dict[str, Any]],
            remote_hosts: list[dict[str, Any]],
            transcode_root: str,
            video_defaults: dict[str, Any],
            encode_queue_scheduler: dict[str, Any],
            schedule_profiles: list[dict[str, Any]],
            metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal config
        if not transcode_root:
            raise ValueError("A transcode folder is required.")
        payload = _build_runtime_settings_payload(
            libraries=libraries,
            remote_hosts=remote_hosts,
            transcode_root=transcode_root,
            video_defaults=video_defaults,
            encode_queue_scheduler=encode_queue_scheduler,
            schedule_profiles=schedule_profiles,
            metadata=metadata if metadata is not None else config.metadata,
            existing_library_types={
                key: str(definition.get("type") or "")
                for key, definition in config.library_definition_map.items()
            },
            existing_libraries={
                str(item.get("key") or ""): dict(item)
                for item in config.media.get("libraries", [])
                if isinstance(item, dict) and str(item.get("key") or "").strip()
            } if isinstance(config.media.get("libraries"), list) else {},
            existing_library_paths={
                str(key): str(path)
                for key, path in config.media.get("source_roots", {}).items()
            } if isinstance(config.media.get("source_roots"), dict) else {},
        )
        changed_type_roots = {
            str(definition.get("key") or ""): str(
                config.library_definition_map[str(definition.get("key") or "")].get("type") or ""
            )
            for definition in payload.get("media", {}).get("libraries", [])
            if isinstance(definition, dict)
            and str(definition.get("key") or "") in config.library_definition_map
            and str(config.library_definition_map[str(definition.get("key") or "")].get("type") or "")
            != str(definition.get("type") or "")
        }
        catalog_refresh_needed = False

        def _apply_runtime_settings(existing_runtime_settings: dict[str, Any]) -> dict[str, Any]:
            nonlocal catalog_refresh_needed
            merged_runtime_settings = _merge_runtime_settings_payload(existing_runtime_settings, payload)
            merged_runtime_settings = _bind_runtime_library_overrides(merged_runtime_settings, changed_type_roots)
            catalog_refresh_needed = (
                _runtime_source_roots(existing_runtime_settings) != _runtime_source_roots(merged_runtime_settings)
                or _runtime_library_signature(existing_runtime_settings)
                != _runtime_library_signature(merged_runtime_settings)
                or object_dict(existing_runtime_settings.get("metadata"))
                != object_dict(merged_runtime_settings.get("metadata"))
            )
            return merged_runtime_settings

        update_runtime_settings(config.paths.runtime_settings_path, _apply_runtime_settings)
        config = load_config(config.paths.config_path)
        app.state.config = config
        if catalog_refresh_needed:
            _reset_folder_card_cache()
            with open_db(config.paths.db_path) as connection:
                _maybe_schedule_scan(connection, config, prefix=None, force=True)
        _refresh_host_status_cache(config)
        return {"ok": True, "message": "Settings saved.", "settings": _settings_page_payload(saved=True)}

    def _library_type_preview_action(key: str, library_type: str) -> dict[str, Any]:
        definition = config.library_definition_map.get(key)
        if definition is None:
            raise ValueError("Unknown library root.")
        current_type = str(definition.get("type") or "other")
        supported_types = {"tv", "movie", "spatial", "other"}
        if library_type not in supported_types:
            raise ValueError("Unsupported library type.")
        with open_db(config.paths.db_path) as connection:
            item_count = int(
                connection.execute(
                    select(func.count()).select_from(library_items).where(library_items.c.media_root == key)
                ).scalar_one()
            )
        return {
            "ok": True,
            "preview": {
                "key": key,
                "from_type": current_type,
                "to_type": library_type,
                "item_count": item_count,
                "requires_rescan": current_type != library_type,
                "clears_saved_profiles": current_type != library_type,
                "acknowledgement": f"{key}:{current_type}->{library_type}",
            },
        }

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
            message = f"{label} is reachable now."
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
        dashboard_library_payload=_dashboard_library_payload,
        dashboard_library_details_payload=_dashboard_library_details_payload,
        dashboard_movie_library_payload=_dashboard_movie_library_payload,
        dashboard_movie_library_details_payload=_dashboard_movie_library_details_payload,
    )
    register_settings_routes(
        app,
        settings_payload=_settings_page_payload,
        save_settings_action=_save_settings_action,
        library_type_preview_action=_library_type_preview_action,
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
        confirm_originals_removed_action=lambda prefixes: _confirm_originals_removed_action(prefixes),
    )
    register_host_routes(
        app,
        hosts_payload=_hosts_payload,
        start_host_action=_start_host_action,
        prepare_host_action=_prepare_host_action,
        reset_host_trust_action=_reset_host_trust_action,
    )

    def _folder_content_payload(normalized_prefix: str) -> tuple[dict[str, Any], int]:
        latest_failed_sample_job_payload: dict[str, Any] | None = None
        with open_db(config.paths.db_path) as connection:
            media_scope = resolve_media_scope(
                connection,
                normalized_prefix,
                library_types=config.library_type_map,
            )
            movie_context = None
            if media_scope.domain == "movie":
                membership = classify_movie_path(normalized_prefix, root=media_scope.root)
                movie_title_prefix = membership.title_prefix if membership is not None else normalized_prefix
                movie_decisions = project_candidates(connection, config, prefixes=[movie_title_prefix])
                movie_cards = _folder_cards_for_group(
                    config,
                    connection,
                    folder_group=lambda rel_path: _movie_folder_group(
                        rel_path,
                        library_types=config.library_type_map,
                    ),
                    minimum_recommended_savings_bytes=None,
                    rel_path_root=movie_title_prefix,
                    media_roots={media_scope.root},
                    candidate_decisions=movie_decisions,
                    include_lifecycle=False,
                    include_workflow_states=False,
                )
                movie_context = load_movie_scope_payload(
                    connection,
                    config,
                    normalized_prefix,
                    metrics_by_prefix={card.prefix: asdict(card) for card in movie_cards},
                    candidate_decisions=movie_decisions,
                )
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
                "media_scope": media_scope.to_payload(),
                "movie_context": movie_context,
                "calibration_job": calibration_job,
                "folder_scan_job": folder_scan_job,
                "metric_support": metric_support,
                "metric_status_copy": _metric_status_copy(metric_support),
            }
            if summary.get("item_count", 0) == 0:
                status_code = 200 if folder_scan_job and folder_scan_job.get("status") in {"queued", "running"} else 404
                return {**base_context, "pending": True, "scan_job": folder_scan_job}, status_code
            representative_selection = _representative_selection(connection, config, normalized_prefix)
            if representative_selection is None:
                return (
                    {
                        **base_context,
                        "pending": True,
                        "scan_job": folder_scan_job,
                        "error_message": f"No sample item found for {normalized_prefix}",
                    },
                    404,
                )
            sample_item = representative_selection.primary_item()
            advice_state = _load_advice_state(config, normalized_prefix)
            pending_proposal_raw = _load_pending_proposal(config, normalized_prefix)
            pending_proposal = _pending_proposal_public_view(pending_proposal_raw)
            calibration = _load_calibration_state(config, normalized_prefix)
            recent_sessions = _recent_tuning_sessions(connection, normalized_prefix)
            approved_season_shortcut = (
                sibling_approved_season_memory(connection, prefix=normalized_prefix)
                if media_scope.domain == "tv"
                else None
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
            lifecycle_decisions = (
                project_candidates(connection, config, prefixes=[normalized_prefix])
                if media_scope.domain == "movie"
                else encode_candidate_decisions(connection, config, prefixes=[normalized_prefix])
            )
            lifecycle = (
                scope_lifecycle_payload_from_decisions(normalized_prefix, lifecycle_decisions)
                if media_scope.domain == "tv"
                else None
            )
            encode_candidate_count = sum(decision.eligible for decision in lifecycle_decisions)
            workflow_state = build_folder_workflow_state(
                connection,
                normalized_prefix,
                candidate_eligibility=workflow_eligibility(lifecycle_decisions),
                library_types=config.library_type_map,
            ).to_payload()
            series_context = (
                _folder_series_context(normalized_prefix, library_types=config.library_type_map)
                if media_scope.domain == "tv"
                else None
            )
            latest_failed_sample_job_payload = object_dict(
                _load_latest_failed_sample_job_state(connection, config, normalized_prefix)
            ) or None
        policy = _folder_display_policy(
            sample_item=sample_item,
            calibration=calibration,
            pending_proposal=pending_proposal_raw,
            summary=summary,
        )
        advice_state = _backfill_multimodal_review_pack(
            config,
            normalized_prefix,
            sample_item=sample_item,
            calibration=calibration,
            advice_state=advice_state,
        )
        size_target_analysis = size_budget_sample_analysis(
            operator_request=object_dict(object_dict(advice_state).get("operator_request")) or None,
            calibration_payload=object_dict(calibration),
        )
        video_policy = object_dict(policy.get("video"))
        item_runtime_seconds = float_value(sample_item.get("duration_seconds")) or None
        operator_intent = operator_intent_from_policy(
            video_policy,
            default_video_policy=config.video,
            audio_policy=object_dict(policy.get("audio")),
            subtitle_policy=object_dict(policy.get("subtitle")),
        )
        resolved_operator_intent = operator_intent.to_payload(item_runtime_seconds=item_runtime_seconds)
        budget_item = {
            **sample_item,
            "resolved_policy": policy,
            "output_container": config.output_container,
        }
        stream_budget = resolve_stream_budget_ledger(
            budget_item,
            default_video_policy=config.video,
            output_container=config.output_container,
            prefer_persisted=False,
        )
        budget_item["stream_budget_ledger"] = stream_budget.to_payload()
        size_goal_options = guided_size_goal_options(
            video_policy,
            item_runtime_seconds=item_runtime_seconds,
            default_video_policy=config.video,
            audio_policy=object_dict(policy.get("audio")),
            subtitle_policy=object_dict(policy.get("subtitle")),
        )
        current_quality_risk_policy = (
            object_dict(object_dict(calibration).get("policy"))
            or object_dict(sample_item.get("resolved_policy"))
            or policy
        )
        pending_preview_policy = object_dict(object_dict(pending_proposal_raw).get("preview_policy"))
        quality_risk_preview_policy = (
            current_quality_risk_policy
            if calibration
            else pending_preview_policy or current_quality_risk_policy
        )
        quality_risk_contract = build_quality_risk_contract(
            prefix=normalized_prefix,
            sample_item=budget_item,
            current_policy=current_quality_risk_policy,
            preview_policy=quality_risk_preview_policy,
            operator_request=object_dict(object_dict(advice_state).get("operator_request")) or None,
            calibration=calibration,
            advice_state=advice_state,
            latest_failed_sample_job=latest_failed_sample_job_payload,
            interpretation=object_dict(object_dict(advice_state).get("quality_risk_interpretation")) or None,
            proposed_policy=(
                object_dict(object_dict(pending_proposal_raw).get("applied_policy"))
                if not calibration
                else None
            ),
        )
        resolved_metric, _ = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
        sample_host_statuses = _sample_calibration_host_statuses(config)
        sample_host_choices = _sample_host_options_from_statuses(config, sample_host_statuses)
        sample_host_key = _sample_host_key_from_choices(
            _default_sample_host_key_from_statuses(sample_host_statuses),
            sample_host_choices,
        )
        return (
            {
                **base_context,
                "pending": False,
                "summary": summary,
                "sample_item": public_representative_item(sample_item),
                "representative_selection": representative_selection.public_payload(),
                "item_plan": describe_item_plan(budget_item),
                "policy": policy,
                "hot_spots": hot_spots,
                "calibration": calibration,
                "advice": advice_state,
                "size_target_analysis": size_target_analysis or None,
                "resolved_operator_intent": resolved_operator_intent,
                "stream_budget_ledger": stream_budget.to_payload(),
                "size_goal_options": size_goal_options,
                "quality_risk": quality_risk_public_view(quality_risk_contract),
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
                "encode_candidate_count": encode_candidate_count,
                "workflow_state": workflow_state,
                "lifecycle": lifecycle,
                "series_context": series_context,
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
                folder_group=lambda rel_path: _folder_group(
                    rel_path,
                    library_types=config.library_type_map,
                ),
            )

    def _clear_completed_backups_action(prefixes: list[str] | None) -> dict[str, Any]:
        try:
            archive_root = config.archive_root
        except KeyError:
            archive_root = None
        with open_db(config.paths.db_path) as connection:
            folders = list_completed_folders(
                connection,
                folder_group=lambda rel_path: _folder_group(
                    rel_path,
                    library_types=config.library_type_map,
                ),
                archive_root=archive_root,
            )
        valid_prefixes = {folder.prefix for folder in folders}
        result = clear_completed_backups_action(
            config,
            folder_group=lambda rel_path: _folder_group(
                rel_path,
                library_types=config.library_type_map,
            ),
            prefixes=prefixes,
            valid_prefixes=valid_prefixes,
        )
        result["completed"] = _completed_page_payload()
        return result

    def _confirm_originals_removed_action(prefixes: list[str]) -> dict[str, Any]:
        try:
            archive_root = config.archive_root
        except KeyError:
            archive_root = None
        with open_db(config.paths.db_path) as connection:
            folders = list_completed_folders(
                connection,
                folder_group=lambda rel_path: _folder_group(
                    rel_path,
                    library_types=config.library_type_map,
                ),
                archive_root=archive_root,
            )
            valid_prefixes = {folder.prefix for folder in folders}
            result = confirm_originals_removed_action(
                connection,
                folder_group=lambda rel_path: _folder_group(
                    rel_path,
                    library_types=config.library_type_map,
                ),
                archive_root=archive_root,
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
            allow_measured_size_quality_tradeoff: bool = False,
            allow_measured_size_quality_increase: bool = False,
    ) -> str | None:
        return proposal_alignment_issue(
            operator_request=operator_request,
            request_disposition=request_disposition,
            current_policy=current_policy,
            preview_policy=preview_policy,
            allow_measured_size_quality_tradeoff=allow_measured_size_quality_tradeoff,
            allow_measured_size_quality_increase=allow_measured_size_quality_increase,
        )

    def _operator_requested_experiment_for_config(
            note: str,
            sample_item: dict[str, Any] | None = None,
            *,
            parsed_note: dict[str, Any] | None = None,
            current_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return runtime_operator_requested_experiment(
            note,
            sample_item,
            parsed_note=parsed_note,
            current_policy=current_policy,
            advisor_routing=advisor_routing,
        )

    def _folder_ai_tune_deps() -> FolderAiTuneDeps:
        return FolderAiTuneDeps(
            resolve_sample_host=_resolve_sample_host,
            load_job_state=_load_job_state,
            load_retryable_sample_job_state=_load_retryable_sample_job_state,
            load_latest_failed_sample_job_state=_load_latest_failed_sample_job_state,
            sample_item=_sample_item,
            operator_requested_experiment=_operator_requested_experiment_for_config,
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
            load_advice_state=_load_advice_state,
            save_advice_state=_save_advice_state,
            advisor_routing=advisor_routing,
            save_job_state=_save_job_state,
            clear_pending_proposal=_clear_pending_proposal,
            record_tuning_session=record_tuning_session,
        )

    def _folder_ai_tune_preview_action(
            normalized_prefix: str,
            note: str,
            host_key: str,
            operator_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blocker = production_action_blocker(config, normalized_prefix)
        if blocker is not None:
            return blocker
        return folder_ai_tune_preview_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            note,
            host_key,
            operator_intent,
        )

    def _folder_ai_tune_action(
            normalized_prefix: str,
            note: str,
            host_key: str,
            operator_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blocker = production_action_blocker(config, normalized_prefix)
        if blocker is not None:
            return blocker
        return folder_ai_tune_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            note,
            host_key,
            operator_intent,
        )

    def _folder_ai_tune_confirm_action(normalized_prefix: str, proposal_id: str) -> dict[str, Any]:
        blocker = production_action_blocker(config, normalized_prefix)
        if blocker is not None:
            return blocker
        return folder_ai_tune_confirm_action(
            config,
            _folder_ai_tune_deps(),
            normalized_prefix,
            proposal_id,
        )

    def _queue_folder_encode_action(
            normalized_prefix: str,
            notes: str,
            bypass_schedule: bool,
            override_policy_holds: bool = False,
    ) -> ActionPayload:
        return queue_folder_encode_action(
            config,
            normalized_prefix,
            notes,
            bypass_schedule,
            override_policy_holds,
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
            load_advice_state=_load_advice_state_for_queue,
            load_latest_failed_target_size_job_state=_load_latest_failed_target_size_job_state,
        )

    def _approve_measured_encode_recovery_action(normalized_prefix: str) -> ActionPayload:
        blocker = production_action_blocker(config, normalized_prefix)
        if blocker is not None:
            return blocker
        return approve_measured_encode_recovery_action(
            config,
            normalized_prefix,
            now_iso=_now_iso,
            load_calibration_state=_load_calibration_state,
            calibration_draft_hash=_calibration_draft_hash,
            save_calibration_state=_save_calibration_state,
            review_gate=_review_gate,
            upsert_override=_upsert_override,
            queue_folder_encode_action=_queue_folder_encode_action,
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
            confirm_size_tradeoff: bool,
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
            confirm_high_impact=confirm_high_impact,
            confirm_size_tradeoff=confirm_size_tradeoff,
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

    def _retry_failed_encode_prefix_action(prefix: str) -> dict[str, Any]:
        return retry_failed_encode_prefix_action(
            connection_factory=lambda: open_db(config.paths.db_path),
            config=config,
            prefix=prefix,
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

    def _save_series_lifecycle_action(normalized_prefix: str, mode: str) -> dict[str, Any]:
        nonlocal config
        normalized_mode = mode.strip().lower()
        with open_db(config.paths.db_path) as connection:
            media_scope = resolve_media_scope(
                connection,
                normalized_prefix,
                library_types=config.library_type_map,
            )
        if media_scope.domain != "tv" or media_scope.kind != "tv_series":
            raise HTTPException(status_code=400, detail="Lifecycle mode can only be set for one TV series.")
        if normalized_mode not in {"auto", "on", "off"}:
            raise HTTPException(status_code=400, detail="Lifecycle mode must be auto, on, or off.")
        update_runtime_folder_policy_values(
            config.paths.runtime_settings_path,
            normalized_prefix,
            section="planning",
            values={"series_lifecycle_mode": normalized_mode},
        )
        config = load_config(config.paths.config_path)
        app.state.config = config
        with open_db(config.paths.db_path) as connection:
            decisions = encode_candidate_decisions(
                connection,
                config,
                prefixes=[normalized_prefix],
            )
            lifecycle = scope_lifecycle_payload_from_decisions(normalized_prefix, decisions)
        _reset_folder_card_cache()
        return {
            "ok": True,
            "message": f"Series lifecycle mode set to {normalized_mode}.",
            "lifecycle": lifecycle,
        }

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
        save_series_lifecycle_action=_save_series_lifecycle_action,
        approve_measured_encode_recovery_action=_approve_measured_encode_recovery_action,
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
        retry_failed_encode_prefix_action=_retry_failed_encode_prefix_action,
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
        summary: dict[str, Any] | None = None,
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
    live_policy = object_dict(object_dict(summary).get("resolved_policy"))
    if live_policy:
        return live_policy
    return object_dict(sample_item.get("resolved_policy"))


def _download_review_compare_action(config: MediaforceConfig, prefix: str) -> FileResponse:
    calibration = _load_calibration_state(config, prefix)
    if calibration is None:
        raise HTTPException(status_code=404, detail="No sampled review clips are available for this folder yet.")
    retained_pairs = _review_compare_pair_entries(config, calibration)
    compare_clips = [
        clip_path for clip_path in _review_compare_bundle_entries(config, calibration)
        if clip_path.exists()
    ]
    if retained_pairs and len(retained_pairs) >= max(len(compare_clips), 1):
        bundle_path, download_name, cleanup_path = _build_review_compare_video_from_pairs(
            config=config,
            prefix=prefix,
            retained_pairs=retained_pairs,
        )
        return FileResponse(
            bundle_path,
            filename=download_name,
            media_type="video/quicktime",
            background=BackgroundTask(_remove_path_if_exists, cleanup_path),
        )

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


def _build_review_compare_video_from_pairs(
        *,
        config: MediaforceConfig,
        prefix: str,
        retained_pairs: list[tuple[Path, Path]],
) -> tuple[Path, str, Path]:
    downloads_dir = config.paths.web_state_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = Path(mkdtemp(prefix="review-bundle-", dir=downloads_dir))
    compare_clips: list[Path] = []
    try:
        geometry = _retained_review_pairs_compare_geometry(retained_pairs)
        for index, (source_clip, preview_clip) in enumerate(retained_pairs, start=1):
            output_path = bundle_dir / f"native-compare-{index:02d}.mkv"
            _render_retained_review_pair_compare(
                source_clip=source_clip,
                preview_clip=preview_clip,
                output_path=output_path,
                geometry=geometry,
            )
            compare_clips.append(output_path)
        bundle_path = bundle_dir / f"{_download_name_slug(prefix)}-full-review-compare.mov"
        _concat_review_compare_clips(bundle_path=bundle_path, compare_clips=compare_clips)
    except Exception:
        _remove_path_if_exists(bundle_dir)
        raise
    download_name = f"{_download_name_slug(prefix)}-full-review-compare.mov"
    return bundle_path, download_name, bundle_dir


def _retained_review_pair_compare_geometry(retained_pair: tuple[Path, Path]) -> VideoGeometry:
    source_clip, preview_clip = retained_pair
    source_geometry = _video_geometry(source_clip)
    preview_geometry = _video_geometry(preview_clip)
    return VideoGeometry(
        width=source_geometry.width + preview_geometry.width,
        height=max(source_geometry.height, preview_geometry.height),
    )


def _retained_review_pairs_compare_geometry(retained_pairs: list[tuple[Path, Path]]) -> VideoGeometry:
    geometries = [_retained_review_pair_compare_geometry(pair) for pair in retained_pairs]
    return VideoGeometry(
        width=max(geometry.width for geometry in geometries),
        height=max(geometry.height for geometry in geometries),
    )


def _video_geometry(path: Path) -> VideoGeometry:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = stderr or f"ffprobe could not read video dimensions for {path.name}."
        raise HTTPException(status_code=500, detail=detail)
    try:
        payload = json.loads(result.stdout or "{}")
        stream = object_dict(object_list(payload.get("streams"))[0] if object_list(payload.get("streams")) else {})
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        width = 0
        height = 0
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=500, detail=f"Could not read video dimensions for {path.name}.")
    return VideoGeometry(width=width, height=height)


def _retained_review_pair_compare_filter(geometry: VideoGeometry | None = None) -> str:
    if geometry is None:
        return NATIVE_COMPARE_FILTER
    return (
        f"{NATIVE_COMPARE_FILTER};"
        f"[v]pad={geometry.width}:{geometry.height}:0:0:black,setsar=1[vout]"
    )


def _retained_review_pair_compare_map(geometry: VideoGeometry | None = None) -> str:
    return "[vout]" if geometry is not None else "[v]"


def _render_retained_review_pair_compare(
        *,
        source_clip: Path,
        preview_clip: Path,
        output_path: Path,
        geometry: VideoGeometry | None = None,
) -> None:
    command = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_clip),
        "-i",
        str(preview_clip),
        "-filter_complex",
        _retained_review_pair_compare_filter(geometry),
        "-map",
        _retained_review_pair_compare_map(geometry),
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "veryfast",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        _remove_path_if_exists(output_path)
        stderr = (result.stderr or "").strip()
        detail = stderr or "ffmpeg could not build a native-resolution review comparison clip."
        raise HTTPException(status_code=500, detail=detail)


def _review_compare_pair_entries(config: MediaforceConfig, calibration: dict[str, Any]) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    for raw_pair in object_list(calibration.get("review_pairs")):
        pair = object_dict(raw_pair)
        source_clip = _review_file_from_url(config, str(object_dict(pair.get("source_clip")).get("path") or ""))
        preview_clip = _review_file_from_url(config, str(object_dict(pair.get("preview_clip")).get("path") or ""))
        if source_clip is None or preview_clip is None:
            continue
        if source_clip.exists() and preview_clip.exists():
            entries.append((source_clip, preview_clip))
    if entries:
        return entries

    source_by_timestamp = {
        _review_pair_timestamp_key(float_value(clip.get("timestamp_seconds"))): _review_file_from_url(
            config,
            str(clip.get("path") or ""),
        )
        for clip in (object_dict(raw_clip) for raw_clip in object_list(calibration.get("source_clips")))
    }
    for raw_clip in object_list(calibration.get("preview_clips")):
        preview = object_dict(raw_clip)
        key = _review_pair_timestamp_key(float_value(preview.get("timestamp_seconds")))
        source_clip = source_by_timestamp.get(key)
        preview_clip = _review_file_from_url(config, str(preview.get("path") or ""))
        if source_clip is None or preview_clip is None:
            continue
        if source_clip.exists() and preview_clip.exists():
            entries.append((source_clip, preview_clip))
    return entries


def _review_pair_timestamp_key(timestamp: float | None) -> int:
    return int(round(float(timestamp or 0.0) * 1000))


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


@dataclass(frozen=True)
class WebStartupSettings:
    config_path: Path
    host: str
    port: int
    reload_enabled: bool


def main(argv: list[str] | None = None) -> None:
    args = _parse_web_startup_args(argv)
    _load_project_env_file()
    settings = _web_startup_settings(args)
    config = load_config(settings.config_path)
    with _exclusive_web_server_lock(config, settings):
        if settings.reload_enabled:
            os.environ["MEDIAFORCE_CONFIG_PATH"] = str(config.paths.config_path)
            uvicorn.run(
                "mediaforce.web.app:create_reloadable_app",
                host=settings.host,
                port=settings.port,
                reload=True,
                factory=True,
                log_level="info",
            )
            return
        uvicorn.run(create_app(config.paths.config_path), host=settings.host, port=settings.port, log_level="info")


def _parse_web_startup_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mediaforce-web", description="Run the Mediaforce web UI.")
    parser.add_argument("--config", help="Path to the TOML config file")
    parser.add_argument("--host", help="Bind host for the FastAPI app")
    parser.add_argument("--port", type=int, help="Bind port for the FastAPI app")
    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument("--reload", dest="reload", action="store_true", help="Enable uvicorn reload mode")
    reload_group.add_argument("--no-reload", dest="reload", action="store_false", help="Disable uvicorn reload mode")
    parser.set_defaults(reload=None)
    return parser.parse_args(argv)


def _web_startup_settings(args: argparse.Namespace) -> WebStartupSettings:
    config_value = _startup_arg_value(args.config) or _startup_env_value("MEDIAFORCE_CONFIG_PATH")
    config_path = Path(config_value or str(DEFAULT_CONFIG_PATH)).expanduser()
    host = _startup_arg_value(args.host) or _default_web_host()
    port = args.port if args.port is not None else _default_web_port()
    reload_enabled = bool(args.reload) if args.reload is not None else _default_web_reload_enabled()
    return WebStartupSettings(
        config_path=config_path,
        host=host,
        port=port,
        reload_enabled=reload_enabled,
    )


def _startup_arg_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _startup_env_value(name: str) -> str | None:
    value = _preferred_env(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _default_web_port() -> int:
    port_value = _startup_env_value("MEDIAFORCE_WEB_PORT")
    if port_value is None:
        return 8777
    return int(port_value)


@contextmanager
def _exclusive_web_server_lock(config: MediaforceConfig, settings: WebStartupSettings) -> Iterator[None]:
    lock_path = _web_server_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = _web_server_lock_owner(lock_path)
            owner_detail = f" ({owner})" if owner else ""
            raise SystemExit(f"mediaforce-web is already running{owner_detail}") from exc

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps(_web_server_lock_payload(config, settings), indent=2, sort_keys=True))
        lock_file.write("\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            _remove_web_server_lock(lock_path)


def _web_server_lock_path(config: MediaforceConfig) -> Path:
    return config.paths.web_state_dir.parent / "mediaforce-web.lock"


def _web_server_lock_payload(config: MediaforceConfig, settings: WebStartupSettings) -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "host": settings.host,
        "port": settings.port,
        "reload": settings.reload_enabled,
        "config_path": str(config.paths.config_path),
        "started_at": _now_iso(),
    }


def _web_server_lock_owner(lock_path: Path) -> str | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    host = payload.get("host")
    port = payload.get("port")
    if pid and host and port:
        return f"pid {pid} on {host}:{port}"
    if pid:
        return f"pid {pid}"
    return None


def _remove_web_server_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


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
    explicit_value = _startup_env_value("MEDIAFORCE_WEB_RELOAD")
    if explicit_value is not None:
        return explicit_value.lower() in {"1", "true", "yes", "on"}
    return False


def _default_web_host() -> str:
    explicit_host = _startup_env_value("MEDIAFORCE_WEB_HOST")
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


def _list_library_structure_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    needs_attention_badges = _folder_needs_attention_badges(connection)
    calibration_job_badges = _folder_calibration_job_badges(connection)
    cards = list_library_structure_cards(
        connection,
        config=config,
        folder_group=lambda rel_path: _folder_group(rel_path, library_types=config.library_type_map),
        review_badge_for_prefix=lambda prefix: _folder_review_badge(
            config,
            prefix,
            needs_attention_badges=needs_attention_badges,
            calibration_job_badges=calibration_job_badges,
        ),
    )
    return _attach_media_scopes(connection, config, cards)


def _list_library_detail_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    return _folder_cards_for_group(
        config,
        connection,
        folder_group=lambda rel_path: _tv_season_folder_group(
            rel_path,
            library_types=config.library_type_map,
        ),
        minimum_recommended_savings_bytes=None,
        media_roots={root for root, library_type in config.library_type_map.items() if library_type == "tv"},
    )


def _list_folder_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    needs_attention_badges: dict[str, dict[str, str | None]] | None = None
    calibration_job_badges: dict[str, dict[str, str | None]] | None = None

    def review_badge_for_prefix(prefix: str) -> dict[str, str | None]:
        nonlocal calibration_job_badges, needs_attention_badges
        if needs_attention_badges is None:
            needs_attention_badges = _folder_needs_attention_badges(connection)
        if calibration_job_badges is None:
            calibration_job_badges = _folder_calibration_job_badges(connection)
        return _folder_review_badge(
            config,
            prefix,
            needs_attention_badges=needs_attention_badges,
            calibration_job_badges=calibration_job_badges,
        )

    cards = cached_folder_cards(
        config,
        connection,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=lambda rel_path: _folder_group(rel_path, library_types=config.library_type_map),
        age_days=_age_days,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=review_badge_for_prefix,
    )
    return _attach_media_scopes(connection, config, cards)


def _list_series_folder_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    return _folder_cards_for_group(
        config,
        connection,
        folder_group=lambda rel_path: _tv_series_folder_group(
            rel_path,
            library_types=config.library_type_map,
        ),
        aggregate_badges=True,
        media_roots={root for root, library_type in config.library_type_map.items() if library_type == "tv"},
    )


def _folder_cards_for_group(
        config: MediaforceConfig,
        connection: DBClient,
        *,
        folder_group: Any,
        aggregate_badges: bool = False,
        minimum_recommended_savings_bytes: int | None = MIN_RECOMMENDED_SAVINGS_BYTES,
        rel_path_root: str | None = None,
        media_roots: set[str] | None = None,
        candidate_decisions: list[CandidateDecision] | None = None,
        include_lifecycle: bool = True,
        include_workflow_states: bool = True,
) -> list[FolderCard]:
    needs_attention_badges: dict[str, dict[str, str | None]] | None = None
    calibration_job_badges: dict[str, dict[str, str | None]] | None = None

    def review_badge_for_prefix(prefix: str) -> dict[str, str | None]:
        nonlocal calibration_job_badges, needs_attention_badges
        if needs_attention_badges is None:
            needs_attention_badges = _folder_needs_attention_badges(connection)
        if calibration_job_badges is None:
            calibration_job_badges = _folder_calibration_job_badges(connection)
        if aggregate_badges:
            aggregated_badge = _aggregated_folder_review_badge(
                prefix,
                needs_attention_badges=needs_attention_badges,
                calibration_job_badges=calibration_job_badges,
            )
            if aggregated_badge is not None:
                return aggregated_badge
        return _folder_review_badge(
            config,
            prefix,
            needs_attention_badges=needs_attention_badges,
            calibration_job_badges=calibration_job_badges,
        )

    cards = list_folder_cards(
        connection,
        config=config,
        minimum_recommended_savings_bytes=minimum_recommended_savings_bytes,
        folder_group=folder_group,
        age_days=_age_days,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=review_badge_for_prefix,
        rel_path_root=rel_path_root,
        media_roots=media_roots,
        candidate_decisions=candidate_decisions,
        include_lifecycle=include_lifecycle,
        include_workflow_states=include_workflow_states,
    )
    return _attach_media_scopes(connection, config, cards)


def _preview_folder_cards(config: MediaforceConfig, connection: DBClient) -> list[FolderCard]:
    needs_attention_badges = _folder_needs_attention_badges(connection)
    calibration_job_badges = _folder_calibration_job_badges(connection)
    cards = preview_folder_cards(
        connection,
        config=config,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=lambda rel_path: _folder_group(rel_path, library_types=config.library_type_map),
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=lambda prefix: _folder_review_badge(
            config,
            prefix,
            needs_attention_badges=needs_attention_badges,
            calibration_job_badges=calibration_job_badges,
        ),
    )
    return _attach_media_scopes(connection, config, cards)


def _attach_media_scopes(
        connection: DBClient,
        config: MediaforceConfig,
        cards: list[FolderCard],
) -> list[FolderCard]:
    scopes = resolve_media_scopes(
        connection,
        [card.prefix for card in cards],
        library_types=config.library_type_map,
    )
    for card, scope in zip(cards, scopes, strict=True):
        card.media_scope = scope.to_payload()
    return cards


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
    return ensure_encode_host_ready(
        config,
        host_payload,
        lifecycle_command_timeout_seconds=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS,
        lifecycle_poll_seconds=HOST_LIFECYCLE_POLL_SECONDS,
    )


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
    return sample_host_options(
        config,
        safe_collect_statuses=_safe_collect_host_statuses,
        schedule_fields_for_host=lambda status: _sample_host_schedule_fields(config, status),
    )


def _sample_host_options_from_statuses(
        config: MediaforceConfig,
        statuses: list[HostStatus],
) -> list[dict[str, Any]]:
    return sample_host_options_from_statuses(
        statuses,
        schedule_fields_for_host=lambda status: _sample_host_schedule_fields(config, status),
    )


def _sample_host_schedule_fields(config: MediaforceConfig, status: HostStatus) -> dict[str, Any]:
    host_config = _host_config_for_key(config, status.key)
    host_payload = {**host_config, **asdict(status)}
    policy = _schedule_profile_policy_for_host(config, host_payload)
    schedule_open = _scheduler_allows_encode_run(policy, host_payload=host_payload)
    summary = str(policy.get("summary") or "").strip()
    return {
        "schedule_open": schedule_open,
        "schedule_detail": summary,
        "schedule_profile_label": str(policy.get("label") or "Always"),
        "storage_recovery_available": remote_mount_recovery_supported(config, host_config, status),
    }


def _sample_host_help_text(sample_host_choices: list[dict[str, Any]], selected_key: str) -> str:
    for option in sample_host_choices:
        if str(option.get("key") or "") != selected_key:
            continue
        detail = str(option.get("detail") or "").strip()
        return detail or "Choose where sampled calibration should run."
    return "Choose where sampled calibration should run."


def _sample_host_key_from_choices(default_key: str, sample_host_choices: list[dict[str, Any]]) -> str:
    if default_key:
        return default_key
    for option in sample_host_choices:
        if bool(option.get("available")):
            return str(option.get("key") or "").strip()
    return ""


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
    if not host.available and not remote_mount_recovery_supported(config, _host_config_for_key(config, host.key), host):
        raise HTTPException(status_code=400, detail=host.message)
    return host


def _encode_queue_scheduler_from_form(form_data: dict[str, str]) -> dict[str, Any]:
    _ = form_data
    # The shared Queue Window is retired; hosts now use Always or an explicit
    # custom schedule profile.
    return _normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})


def _folder_badge_failure_detail(raw_error: Any, raw_waiting_reason: Any = None) -> str | None:
    error_text = str(raw_error or raw_waiting_reason or "").strip()
    if not error_text:
        return None
    if "Error:" in error_text:
        error_text = error_text[error_text.rfind("Error:"):]
    else:
        lines = [line.strip() for line in error_text.splitlines() if line.strip()]
        error_text = lines[-1] if lines else error_text
    compact = " ".join(error_text.split())
    return f"{compact[:157]}..." if len(compact) > 160 else compact


def _folder_badge_prefix(raw_prefix: Any) -> str:
    return raw_prefix.strip().strip("/") if isinstance(raw_prefix, str) else ""


def _folder_needs_attention_badges(connection: DBClient) -> dict[str, dict[str, str | None]]:
    rows = connection.execute(
        select(
            encode_jobs.c.prefix,
            encode_jobs.c.status,
            encode_jobs.c.error,
            encode_jobs.c.waiting_reason,
        )
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.prefix.asc(), encode_jobs.c.created_at.desc(), literal_column("rowid").desc())
    ).mappings().fetchall()
    badges: dict[str, dict[str, str | None]] = {}
    seen_prefixes: set[str] = set()
    for row in rows:
        prefix = _folder_badge_prefix(row["prefix"])
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        if str(row["status"] or "") != "needs_attention":
            continue
        badges[prefix] = {
            "label": "Needs attention",
            "tone": "warning",
            "detail": _folder_badge_failure_detail(row["error"], row["waiting_reason"]),
        }
    return badges


def _aggregated_folder_review_badge(
        prefix: str,
        *,
        needs_attention_badges: dict[str, dict[str, str | None]],
        calibration_job_badges: dict[str, dict[str, str | None]],
) -> dict[str, str | None] | None:
    for badges in (needs_attention_badges, calibration_job_badges):
        exact_badge = badges.get(prefix)
        if exact_badge is not None:
            return exact_badge
        descendant_badges = [
            badge
            for badge_prefix, badge in badges.items()
            if badge_prefix.startswith(f"{prefix}/")
        ]
        if descendant_badges:
            return descendant_badges[0]
    return None


def _folder_calibration_job_badges(connection: DBClient) -> dict[str, dict[str, str | None]]:
    rows = connection.execute(
        select(
            calibration_jobs_table.c.prefix,
            calibration_jobs_table.c.status,
            calibration_jobs_table.c.lane,
            calibration_jobs_table.c.error,
        )
        .order_by(
            calibration_jobs_table.c.prefix.asc(),
            calibration_jobs_table.c.created_at.desc(),
            literal_column("rowid").desc(),
        )
    ).mappings().fetchall()
    badges: dict[str, dict[str, str | None]] = {}
    seen_prefixes: set[str] = set()
    for row in rows:
        prefix = _folder_badge_prefix(row["prefix"])
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        badge = _calibration_job_badge(
            status=str(row["status"] or ""),
            lane=str(row["lane"] or "sample"),
            error=row["error"],
        )
        if badge is not None:
            badges[prefix] = badge
    return badges


def _calibration_job_badge(*, status: str, lane: str, error: Any) -> dict[str, str | None] | None:
    mode = "full" if lane == "full" else "sample"
    if status == "running":
        return {
            "label": "Proof running" if mode == "full" else "Sample running",
            "tone": "attention",
            "detail": "Representative-file proof encode is active." if mode == "full" else "Sample calibration is active.",
        }
    if status == "queued":
        return {
            "label": "Proof queued" if mode == "full" else "Sample queued",
            "tone": "attention",
            "detail": "Waiting for proof encode capacity." if mode == "full" else "Waiting for sample host capacity.",
        }
    if status == "pending_review":
        return {"label": "Review pending", "tone": "attention", "detail": None}
    if status == "failed":
        return {
            "label": "Proof failed" if mode == "full" else "Sample failed",
            "tone": "warning",
            "detail": _folder_badge_failure_detail(error),
        }
    if status == "stopped":
        return {
            "label": "Proof stopped" if mode == "full" else "Sample stopped",
            "tone": "warning",
            "detail": _folder_badge_failure_detail(error),
        }
    return None


def _folder_review_badge(
        config: MediaforceConfig,
        prefix: str,
        connection: DBClient | None = None,
        needs_attention_badges: dict[str, dict[str, str | None]] | None = None,
        calibration_job_badges: dict[str, dict[str, str | None]] | None = None,
) -> dict[str, str | None]:
    if needs_attention_badges is not None:
        badge = needs_attention_badges.get(prefix)
        if badge is not None:
            return badge
    if connection is not None:
        encode_job = load_latest_encode_job(connection, prefix)
        if encode_job and str(encode_job.get("status") or "") == "needs_attention":
            return {
                "label": "Needs attention",
                "tone": "warning",
                "detail": _folder_badge_failure_detail(
                    encode_job.get("error"),
                    encode_job.get("waiting_reason"),
                ),
            }

    if calibration_job_badges is not None:
        badge = calibration_job_badges.get(prefix)
        if badge is not None:
            return badge
    elif connection is not None:
        calibration_job = _load_job_state(connection, config, prefix)
        if calibration_job is not None:
            badge = _calibration_job_badge(
                status=str(calibration_job.get("status") or ""),
                lane=str(calibration_job.get("lane") or calibration_job.get("mode") or "sample"),
                error=calibration_job.get("error"),
            )
            if badge is not None:
                return badge

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
    selection = _representative_selection(connection, config, prefix)
    return selection.primary_item() if selection is not None else None


def _representative_selection(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> RepresentativeSelection | None:
    return load_representative_selection(connection, config, prefix)


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
    scope = resolve_media_scope(
        connection,
        normalized_prefix,
        library_types=config.library_type_map,
    )
    rows = connection.execute(
        select(library_items)
        .join(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .where(library_items.c.status.in_(tuple(sorted(statuses))))
        .where(staged_artifacts.c.staging_path.is_not(None))
        .where(staged_artifacts.c.promoted_at.is_(None))
        .order_by(library_items.c.rel_path.asc())
    ).mappings().fetchall()
    items: list[FolderItem] = []
    for row in rows:
        if scope.domain == "movie":
            membership = classify_movie_path(str(row["rel_path"]), root=scope.root)
            library = config.library_definition_map.get(scope.root, {})
            policy = object_dict(library.get("policy"))
            if membership is None or not movie_item_included(
                    membership,
                    policy,
                    explicit_exact=scope.match == "exact_item",
            )[0]:
                continue
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
        latest_failed_sample_job: dict[str, Any] | None = None,
        learning_context_payload: list[dict[str, Any]] | None = None,
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
        latest_failed_sample_job=latest_failed_sample_job,
        learning_context_payload=learning_context_payload,
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
        latest_failed_sample_job: dict[str, Any] | None = None,
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
        latest_failed_sample_job=latest_failed_sample_job,
    )


def _build_tuning_runtime_toolbelt(
        *,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
        calibration: dict[str, Any] | None,
        metric_support: dict[str, bool],
        operator_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_tuning_runtime_toolbelt(
        _folder_tuning_runtime_deps(),
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        metric_support=metric_support,
        operator_request=operator_request,
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


def _settings_library_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    return _settings_library_rows_for_config_runtime(config, min_rows=min_rows)


def _settings_remote_rows_for_config(config: MediaforceConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    return _settings_remote_rows_for_config_runtime(config, min_rows=min_rows)


def _settings_transcode_root_value(config: MediaforceConfig) -> str:
    return _settings_transcode_root_value_runtime(config)


def _settings_archive_root(transcode_root: str) -> str:
    return _settings_archive_root_runtime(transcode_root)


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


def _save_advice_state(config: MediaforceConfig, prefix: str, advice: ActionPayload) -> None:
    with _locked_advice_state(config, prefix) as path:
        try:
            existing = _read_advice_state(path, strict=True) or {}
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            existing = {}
        payload = dict(advice)
        if existing.get("quality_risk_records") or payload.get("quality_risk_records"):
            payload["quality_risk_records"] = _merge_quality_risk_records(
                existing.get("quality_risk_records"),
                payload.get("quality_risk_records"),
            )
        save_advice_state(path, payload)


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
        ensure_sample_host_ready=ensure_sample_host_ready,
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
        recommend_review_moments=recommend_review_moments,
        review_moment_payload=review_moment_payload,
        encode_preview_clips=encode_preview_clips,
        render_source_review_clips=render_source_review_clips,
        generate_compare_clips_from_previews=generate_compare_clips_from_previews,
        resolve_stream_budget_ledger=resolve_stream_budget_ledger,
        build_svt_params=build_svt_params,
        review_url=_review_url,
        encode_manifest_items=encode_manifest_items,
        validate_manifest_items=validate_manifest_items,
        generate_compare_clips=generate_compare_clips,
        staged_artifact_columns=CALIBRATION_STAGED_ARTIFACT_COLUMNS,
    )


def _load_job_state(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return runtime_load_job_state(connection, config, prefix, _job_runtime_deps())


def _load_overlapping_job_state(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return runtime_load_overlapping_job_state(connection, config, prefix, _job_runtime_deps())


def _load_retryable_sample_job_state(connection: DBClient, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return load_retryable_sample_job_state(connection, config, prefix, _job_runtime_deps())


def _load_latest_failed_sample_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> dict[str, Any] | None:
    return load_latest_failed_sample_job_state(connection, config, prefix, _job_runtime_deps())


def _load_latest_failed_target_size_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> dict[str, Any] | None:
    return load_latest_failed_target_size_job_state(connection, config, prefix, _job_runtime_deps())


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
        connection: DBClient, config: MediaforceConfig, prefix: str | None, *, force: bool = False
) -> dict[str, Any] | None:
    return runtime_maybe_schedule_scan(connection, config, prefix, _job_runtime_deps(), force=force)


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


def _folder_group(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str] | None:
    scope = media_group_scope_for_rel_path(rel_path, library_types=library_types)
    return scope.group_tuple() if scope is not None else None


def _movie_folder_group(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str] | None:
    scope = media_group_scope_for_rel_path(rel_path, library_types=library_types)
    if scope is None or scope.domain != "movie":
        return None
    return scope.group_tuple()


def _tv_series_folder_group(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str] | None:
    scope = tv_series_scope_for_rel_path(rel_path, library_types=library_types)
    return scope.group_tuple() if scope is not None else None


def _tv_season_folder_group(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str] | None:
    scope = media_group_scope_for_rel_path(rel_path, library_types=library_types)
    if scope is None or scope.kind != "tv_season":
        return None
    return scope.group_tuple()


def _folder_series_context(
        prefix: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    return series_context_for_prefix(prefix, library_types=library_types)


def _folder_encode_candidate_count(connection: DBClient, config: MediaforceConfig, prefix: str) -> int:
    return sum(
        decision.eligible
        for decision in encode_candidate_decisions(connection, config, prefixes=[prefix])
    )


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
    running_prefixes = [
        str(row["prefix"] or "").strip()
        for row in connection.execute(
            select(encode_jobs.c.prefix)
            .where(encode_jobs.c.status == "running")
            .where(encode_jobs.c.job_kind.in_(("single", "shard")))
        ).mappings().fetchall()
    ]
    runtime_recover_encode_queue(connection, config, _encode_queue_runtime_deps())
    cleaned_prefixes = sorted({prefix for prefix in running_prefixes if prefix})
    if cleaned_prefixes:
        _sweep_orphaned_encode_processes(config, prefixes=cleaned_prefixes)


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
        latest_failed_sample_job: dict[str, Any] | None = None,
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
        latest_failed_sample_job=latest_failed_sample_job,
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


def _sweep_orphaned_encode_processes(config: MediaforceConfig, *, prefixes: list[str] | None = None) -> None:
    normalized_prefixes = [prefix.strip().strip("/") for prefix in prefixes or [] if prefix.strip().strip("/")]

    def path_filter_for_host(target_host: dict[str, Any]) -> str:
        if not normalized_prefixes:
            return ""
        patterns: list[str] = []

        def path_prefix_pattern(path: Path) -> str:
            return f"{re.escape(str(path))}(/|[[:space:]]|$)"

        for prefix in normalized_prefixes:
            patterns.append(path_prefix_pattern(config.staging_root_for_host(target_host) / prefix))
            for root_key, source_root in config.source_root_map_for_host(target_host).items():
                root_prefix = str(root_key).strip().strip("/")
                if prefix == root_prefix:
                    patterns.append(path_prefix_pattern(source_root))
                    continue
                root_leader = f"{root_prefix}/"
                if prefix.startswith(root_leader):
                    patterns.append(path_prefix_pattern(source_root / prefix[len(root_leader):]))
        return "|".join(patterns)

    sweep_script = (
        "self_pid=$$; "
        "self_pgid=$(ps -o pgid= -p \"$self_pid\" 2>/dev/null | tr -d ' '); "
        "kill_tree() ( signal=$1; target=$2; children=$(ps -axo pid=,ppid= | awk -v target=\"$target\" '$2 == target { print $1 }'); for child in $children; do kill_tree \"$signal\" \"$child\"; done; kill -\"$signal\" \"$target\" 2>/dev/null || true; ); "
        "patterns='mediaforce_encoded_by=mediaforce|ab-av1 .*--temp-dir .*\\.mediaforce-ab-av1-'; "
        "extra_patterns=$1; "
        "pids=$(ps -axo pid=,command= | awk -v self=\"$self_pid\" -v pat=\"$patterns\" -v extra=\"$extra_patterns\" '$1 != self && $0 !~ /(^|[[:space:]/])awk([[:space:]]|$)/ { pid=$1; $1=\"\"; if ($0 ~ pat && (extra == \"\" || $0 ~ extra)) print pid }' || true); "
        "if [ -n \"$pids\" ]; then "
        "for pid in $pids; do pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d ' '); "
        "if [ -n \"$pgid\" ] && [ \"$pgid\" != \"$self_pgid\" ]; then kill -TERM -\"$pgid\" 2>/dev/null || true; else kill_tree TERM \"$pid\"; fi; done; "
        "sleep 2; "
        "pids=$(ps -axo pid=,command= | awk -v self=\"$self_pid\" -v pat=\"$patterns\" -v extra=\"$extra_patterns\" '$1 != self && $0 !~ /(^|[[:space:]/])awk([[:space:]]|$)/ { pid=$1; $1=\"\"; if ($0 ~ pat && (extra == \"\" || $0 ~ extra)) print pid }' || true); "
        "if [ -n \"$pids\" ]; then "
        "for pid in $pids; do pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d ' '); "
        "if [ -n \"$pgid\" ] && [ \"$pgid\" != \"$self_pgid\" ]; then kill -KILL -\"$pgid\" 2>/dev/null || true; else kill_tree KILL \"$pid\"; fi; done; "
        "fi; "
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
        if configured_remote_host_execution_mode(host) != "ssh":
            continue
        try:
            path_filter = path_filter_for_host(host)
            run_remote_command(host, ["sh", "-lc", sweep_script, "mediaforce-sweep", path_filter], timeout=10)
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
