import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from mediaforce.advisor import (
    AdvisorResponse,
    TuningPolicyResponse,
    apply_seed_policy,
    request_run_verdict,
    request_seed_policy,
)
from mediaforce.binaries import ffmpeg_binary
from mediaforce.calibration_jobs import load_active_job, load_job, \
    list_queue_summary
from mediaforce.config import DEFAULT_CONFIG_PATH, MediaforceConfig, load_config, load_runtime_settings, \
    save_runtime_settings
from mediaforce.db import open_db
from mediaforce.encode_queue import DEFAULT_SCHEDULER_POLICY, ensure_queue_state, list_encode_jobs, \
    load_active_encode_job, load_encode_job, load_latest_encode_job, load_queue_state, \
    queue_position as encode_queue_position, save_encode_job, save_queue_state, summarize_encode_queue
from mediaforce.execution import (
    build_svt_params,
    describe_item_plan,
    encode_manifest_items,
    effective_video_preset,
    estimate_output_overhead_bytes,
    search_quality_for_source,
    validate_manifest_items,
)
from mediaforce.folder_profiles import inspect_prefix
from mediaforce.planner import build_manifest_item
from mediaforce.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.quality import run_sample_encode, select_quality_metric
from mediaforce.remote import (
    DEFAULT_HOST_MEDIA_ACCESS,
    HostStatus,
    collect_host_statuses,
    host_status_targets_current_machine,
    prepare_remote_host_with_password,
    reset_remote_host_trust,
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
from mediaforce.tuning_memory import (
    record_visual_approval_artifact,
)
from mediaforce.type_defs import JSONValue
from mediaforce.web.runtime import FolderCard, cached_folder_cards, dashboard_folders_payload, \
    dashboard_summary_payload, default_sample_host_key, default_sample_host_key_from_statuses, \
    FolderAiTuneDeps, FolderStateDeps, FolderTuningRuntimeDeps, clear_pending_proposal, \
    folder_ai_tune_action, folder_ai_tune_confirm_action, folder_ai_tune_preview_action, \
    folder_card_cache_key, folder_status_payload, host_config_for_key, host_lifecycle_start_command, \
    host_lifecycle_start_timeout_seconds, host_lifecycle_stop_command, host_runtime_rows, \
    load_calibration_state, load_json_object, load_pending_proposal, \
    multimodal_review_pack_public_view, pause_encode_queue_action, pending_proposal_public_view, \
    planned_audio_review_context, preview_folder_cards, proposal_alignment_issue, \
    proposal_context_snapshot, proposal_signal_copy, queue_folder_encode_action, recent_tuning_sessions, \
    refresh_host_status_cache, reset_folder_card_cache, resume_encode_queue_action, review_media_context, \
    review_pack_dir, review_pair_key, review_pairs, safe_collect_host_statuses, save_advice_state, \
    save_calibration_state, save_pending_proposal, save_profile_action, \
    sample_calibration_host_statuses, sample_host_options, sample_host_options_from_statuses, \
    settings_page_payload, stop_calibration_queue_action, stop_encode_queue_action, \
    build_multimodal_review_pack, build_tuning_runtime_toolbelt
from mediaforce.web.runtime.job_runtime import JobRuntimeDeps, active_scan_from_db as runtime_active_scan_from_db, \
    CalibrationQueueRuntimeDeps, calibration_queue_worker_loop as runtime_calibration_queue_worker_loop, \
    calibration_job_belongs_to_current_process as runtime_calibration_job_belongs_to_current_process, \
    dispatch_calibration_job as runtime_dispatch_calibration_job, \
    expire_calibration_job as runtime_expire_calibration_job, \
    latest_scan_completed_at as runtime_latest_scan_completed_at, \
    load_job_state as runtime_load_job_state, load_scan_job_state as runtime_load_scan_job_state, \
    maybe_schedule_scan as runtime_maybe_schedule_scan, process_calibration_queue_once as runtime_process_calibration_queue_once, \
    run_scan_job as runtime_run_scan_job, save_job_state as runtime_save_job_state, \
    save_scan_job_state as runtime_save_scan_job_state, scan_is_stale as runtime_scan_is_stale, \
    scan_job_belongs_to_current_process as runtime_scan_job_belongs_to_current_process, \
    scan_process_is_alive as runtime_scan_process_is_alive
from mediaforce.web.runtime.calibration_runtime import CalibrationRunDeps, \
    restore_staged_artifact as runtime_restore_staged_artifact, \
    run_calibration_job as runtime_run_calibration_job, \
    run_full_calibration as runtime_run_full_calibration, \
    run_sampled_calibration as runtime_run_sampled_calibration, \
    remove_path as runtime_remove_path, snapshot_staged_artifact as runtime_snapshot_staged_artifact
from mediaforce.web.routes import register_dashboard_routes, register_folder_routes, register_frontend_routes, \
    register_host_routes, register_queue_routes, register_settings_routes
from mediaforce.web.settings_runtime import (
    ALWAYS_SCHEDULE_PROFILE,
    DEFAULT_HOST_MAX_PARALLEL_ENCODES,
    DEFAULT_HOST_SCHEDULE_PROFILE,
    HOST_CAPABILITY_OPTIONS,
    build_runtime_settings_payload as _build_runtime_settings_payload,
    canonical_schedule_profile_key as _canonical_schedule_profile_key,
    host_max_parallel_encodes as _host_max_parallel_encodes,
    host_schedule_profile_key as _host_schedule_profile_key,
    index_schedule_profile_rows as _index_schedule_profile_rows,
    index_settings_library_rows as _index_settings_library_rows,
    index_settings_remote_rows as _index_settings_remote_rows,
    library_color_map_for_config as _library_color_map_for_config,
    merge_runtime_settings_payload as _merge_runtime_settings_payload,
    normalize_encode_queue_scheduler as _normalize_encode_queue_scheduler,
    normalize_host_source_root_overrides as _normalize_host_source_root_overrides,
    normalize_library_color as _normalize_library_color,
    normalize_library_key as _normalize_library_key,
    runtime_library_colors as _runtime_library_colors,
    runtime_source_roots as _runtime_source_roots,
    schedule_profile_options as _schedule_profile_options,
    settings_archive_root as _settings_archive_root,
    settings_library_rows_for_config as _settings_library_rows_for_config,
    settings_remote_rows_for_config as _settings_remote_rows_for_config,
    settings_schedule_profile_rows_for_config as _settings_schedule_profile_rows_for_config,
    settings_transcode_root_value as _settings_transcode_root_value,
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
ENCODE_QUEUE_WORKER_LOCK = threading.Lock()
ENCODE_QUEUE_WORKER_STARTED = False
CALIBRATION_REVIEW_FIELDS = {
    "accepted_at",
    "accepted_draft_hash",
    "accepted_sample_job_id",
    "draft_hash",
}

_NOTE_VMAF_PATTERNS = (
    re.compile(r"\b(?P<target>\d{2}(?:\.\d+)?)\s*vmaf\b", re.IGNORECASE),
    re.compile(r"\bvmaf\s*(?:target|around|at|to|of|=)?\s*(?P<target>\d{2}(?:\.\d+)?)\b", re.IGNORECASE),
)
_NOTE_XPSNR_PATTERNS = (
    re.compile(r"\b(?P<target>\d{2}(?:\.\d+)?)\s*xpsnr\b", re.IGNORECASE),
    re.compile(r"\bxpsnr\s*(?:target|around|at|to|of|=)?\s*(?P<target>\d{2}(?:\.\d+)?)\b", re.IGNORECASE),
)
_NOTE_SIZE_BUDGET_PATTERNS = (
    re.compile(
        r"\b(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\s*(?:per|/)\s*(?:episode|ep|file)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\s*(?:each|an?)\s*(?:episode|ep|file)\b",
        re.IGNORECASE,
    ),
)
_SIZE_BUDGET_UNIT_BYTES = {
    "kb": 1024,
    "mb": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "tb": 1024 * 1024 * 1024 * 1024,
}


def _parse_audio_bitrate_kbps(value: JSONValue, fallback: float) -> float:
    stripped = str(value or "").strip().lower().removesuffix("kbps").removesuffix("k")
    try:
        parsed = float(stripped)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _sample_audio_target_kbps(sample_item: dict[str, Any] | None) -> float:
    if not isinstance(sample_item, dict):
        return 160.0
    audio_policy = dict((sample_item.get("resolved_policy") or {}).get("audio") or {})
    audio_tracks = list(sample_item.get("audio_summary") or [])
    channels = 0
    for track in audio_tracks:
        try:
            channels = max(channels, int(track.get("channels") or 0))
        except (TypeError, ValueError):
            continue
    if channels >= 8:
        return _parse_audio_bitrate_kbps(audio_policy.get("surround_7_1_opus_bitrate"), 320.0)
    if channels >= 6:
        return _parse_audio_bitrate_kbps(audio_policy.get("surround_5_1_opus_bitrate"), 224.0)
    return _parse_audio_bitrate_kbps(audio_policy.get("stereo_opus_bitrate"), 128.0)


def _size_budget_feasibility(*, source_percent: float | None, video_bitrate_kbps: float | None) -> tuple[str, bool]:
    if source_percent is None or video_bitrate_kbps is None:
        return "unknown", False
    if source_percent <= 10.0 or video_bitrate_kbps <= 500.0:
        return "unreasonable", True
    if source_percent <= 20.0 or video_bitrate_kbps <= 900.0:
        return "aggressive", False
    return "reasonable", False


def _size_budget_request(trimmed: str, sample_item: dict[str, Any] | None) -> dict[str, Any] | None:
    for pattern in _NOTE_SIZE_BUDGET_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        unit = str(match.group("unit") or "").strip().lower()
        multiplier = _SIZE_BUDGET_UNIT_BYTES.get(unit)
        if multiplier is None:
            continue
        try:
            amount = float(match.group("amount"))
        except (TypeError, ValueError):
            continue
        budget_bytes = int(round(amount * multiplier))
        source_size_bytes = None
        duration_seconds = None
        if isinstance(sample_item, dict):
            try:
                source_size_bytes = float(sample_item.get("source_size_bytes") or 0)
            except (TypeError, ValueError):
                source_size_bytes = None
            try:
                duration_seconds = float(sample_item.get("duration_seconds") or 0)
            except (TypeError, ValueError):
                duration_seconds = None
        audio_kbps = _sample_audio_target_kbps(sample_item)
        estimated_audio_bytes = None
        estimated_video_bitrate_kbps = None
        estimated_source_percent = None
        if duration_seconds and duration_seconds > 0:
            estimated_audio_bytes = int(round((audio_kbps * 1000.0 / 8.0) * duration_seconds))
            remaining_video_bytes = max(budget_bytes - estimated_audio_bytes, 0)
            estimated_video_bitrate_kbps = round((remaining_video_bytes * 8.0 / duration_seconds) / 1000.0, 1)
        if source_size_bytes and source_size_bytes > 0:
            estimated_source_percent = round((budget_bytes / source_size_bytes) * 100.0, 2)
        feasibility, requires_confirmation = _size_budget_feasibility(
            source_percent=estimated_source_percent,
            video_bitrate_kbps=estimated_video_bitrate_kbps,
        )
        return {
            "source": "operator_note",
            "honor_mode": "size_budget_experiment",
            "request_type": "size_budget",
            "budget_bytes": budget_bytes,
            "budget_label": f"{amount:g} {unit.upper()} per episode",
            "request_text": trimmed,
            "estimated_source_percent": estimated_source_percent,
            "estimated_audio_bytes": estimated_audio_bytes,
            "estimated_video_bitrate_kbps": estimated_video_bitrate_kbps,
            "feasibility": feasibility,
            "requires_confirmation": requires_confirmation,
        }
    return None


def _operator_requested_experiment(note: str, sample_item: dict[str, Any] | None = None) -> dict[str, Any] | None:
    trimmed = note.strip()
    if not trimmed:
        return None

    size_budget_request = _size_budget_request(trimmed, sample_item)
    if size_budget_request:
        return size_budget_request

    for pattern in _NOTE_VMAF_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        try:
            target = float(match.group("target"))
        except (TypeError, ValueError):
            continue
        target = round(max(80.0, min(target, 98.0)), 2)
        min_target = round(min(target, max(75.0, target - 2.0)), 2)
        return {
            "source": "operator_note",
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "vmaf",
            "target": target,
            "applied_policy": {
                "video": {
                    "target_vmaf": target,
                    "min_target_vmaf": min_target,
                }
            },
            "request_text": trimmed,
        }

    for pattern in _NOTE_XPSNR_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        try:
            target = float(match.group("target"))
        except (TypeError, ValueError):
            continue
        target = round(max(30.0, min(target, 41.0)), 2)
        min_target = round(min(target, max(29.0, target - 1.0)), 2)
        return {
            "source": "operator_note",
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "xpsnr",
            "target": target,
            "applied_policy": {
                "video": {
                    "target_xpsnr": target,
                    "min_target_xpsnr": min_target,
                }
            },
            "request_text": trimmed,
        }
    return None


def _apply_policy_fragment(policy: dict[str, Any], fragment: dict[str, Any] | None) -> dict[str, Any]:
    updated_policy = json.loads(json.dumps(policy))
    for section, values in dict(fragment or {}).items():
        if not isinstance(values, dict):
            continue
        updated_policy.setdefault(section, {}).update(values)
    return updated_policy


def _load_advice_state(config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    path = _advice_file(config, prefix)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_advice_state(config: MediaforceConfig, prefix: str, patch: dict[str, Any]) -> dict[str, Any]:
    existing = _load_advice_state(config, prefix) or {}
    merged = {**existing, **patch}
    _save_advice_state(config, prefix, merged)
    return merged


def _build_run_verdict_payload(
        *,
        prefix: str,
        calibration_payload: dict[str, Any],
        advice_state: dict[str, Any] | None,
) -> dict[str, Any]:
    sample_result = dict(calibration_payload.get("sample_result") or {})
    sample_item = dict(calibration_payload.get("sample_item") or {})
    policy = dict(calibration_payload.get("policy") or {})
    return {
        "folder": prefix,
        "action": calibration_payload.get("action"),
        "mode": calibration_payload.get("mode"),
        "operator_note": (advice_state or {}).get("operator_note") or calibration_payload.get("notes") or None,
        "operator_request": (advice_state or {}).get("operator_request"),
        "sample_item": {
            "rel_path": sample_item.get("rel_path"),
            "source_size_bytes": sample_item.get("source_size_bytes"),
        },
        "policy": policy,
        "sample_result": {
            "quality_metric": sample_result.get("quality_metric"),
            "quality_target": sample_result.get("quality_target"),
            "quality_score": sample_result.get("quality_score"),
            "chosen_crf": sample_result.get("chosen_crf"),
            "predicted_total_size_bytes": sample_result.get("predicted_total_size_bytes"),
            "predicted_encode_percent": sample_result.get("predicted_encode_percent"),
            "predicted_encode_seconds": sample_result.get("predicted_encode_seconds"),
        },
    }


def _record_run_verdict(config: MediaforceConfig, prefix: str, calibration_payload: dict[str, Any]) -> None:
    if str(calibration_payload.get("mode") or "sample") != "sample":
        return
    if not calibration_payload.get("sample_result"):
        return
    advice_state = _load_advice_state(config, prefix) or {}
    verdict = request_run_verdict(
        project_root=config.paths.project_root,
        payload=_build_run_verdict_payload(
            prefix=prefix,
            calibration_payload=calibration_payload,
            advice_state=advice_state,
        ),
    )
    verdict_payload = {
        "summary": verdict.summary,
        "outcome": verdict.outcome,
        "confidence": verdict.confidence,
        "next_step": verdict.next_step,
        "prompt_version": verdict.prompt_version,
        "evidence_checked": verdict.evidence_checked,
        "evaluated_at": _now_iso(),
    }
    if verdict.raw:
        verdict_payload["raw"] = verdict.raw
    _merge_advice_state(config, prefix, {"run_verdict": verdict_payload})


def create_app(config_path: Path | None = None) -> FastAPI:
    config = load_config(config_path or DEFAULT_CONFIG_PATH)
    purge_transient_artifacts(config, force=True)
    with open_db(config.paths.db_path) as connection:
        ensure_queue_state(connection, updated_at=_now_iso())
        _recover_calibration_jobs(connection, config)
        _recover_encode_queue(connection, config)
    _start_calibration_queue_worker(config)
    _start_encode_queue_worker(config)
    app = FastAPI(title="Mediaforce Calibration Bench")
    cleanup_lock = threading.Lock()
    review_dir = config.paths.review_dir
    packaged_frontend_build_dir = Path(__file__).resolve().parent / "frontend_build"
    frontend_build_dir = packaged_frontend_build_dir if packaged_frontend_build_dir.exists() else config.paths.project_root / "frontend" / "build"
    review_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/review-media", StaticFiles(directory=str(review_dir)), name="review_media")
    frontend_app_dir = frontend_build_dir / "_app"
    if frontend_app_dir.exists():
        app.mount("/_app", StaticFiles(directory=str(frontend_app_dir)), name="frontend_app")
    app.state.config = config
    _refresh_host_status_cache(config)

    @app.middleware("http")
    async def periodic_cleanup(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        _run_periodic_cleanup(config, cleanup_lock)
        return await call_next(request)

    @app.on_event("shutdown")
    async def stop_managed_processes() -> None:
        for controller in _active_calibration_process_controllers():
            controller.cancel()
        ENCODE_QUEUE_PROCESS.cancel()

    def _settings_page_payload(
            *,
            error: str | None = None,
            saved: bool = False,
            host_notice: str | None = None,
            host_notice_kind: str | None = None,
            libraries: list[dict[str, Any]] | None = None,
            remote_hosts: list[dict[str, str]] | None = None,
            transcode_root: str | None = None,
            encode_queue_scheduler: dict[str, Any] | None = None,
            schedule_profiles: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return settings_page_payload(
            config,
            encode_queue_scheduler_policy=_encode_queue_scheduler_policy,
            normalize_encode_queue_scheduler=_normalize_encode_queue_scheduler,
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
            load_scan_job_state=_load_scan_job_state,
        )

    def _save_settings_action(
            *,
            libraries: list[dict[str, str]],
            remote_hosts: list[dict[str, Any]],
            transcode_root: str,
            encode_queue_scheduler: dict[str, Any],
            schedule_profiles: list[dict[str, str]],
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
        existing_runtime_settings = load_runtime_settings(config.paths.runtime_settings_path)
        merged_runtime_settings = _merge_runtime_settings_payload(existing_runtime_settings, payload)
        libraries_changed = _runtime_source_roots(existing_runtime_settings) != _runtime_source_roots(
            merged_runtime_settings)
        save_runtime_settings(config.paths.runtime_settings_path, merged_runtime_settings)
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
        config = load_config(config.paths.config_path)
        app.state.config = config
        _refresh_host_status_cache(config)
        notice = result.message if not result.detail else f"{result.message} Details: {result.detail}"
        return {"ok": result.ok, "message": notice, "kind": "success" if result.ok else "error"}

    def _reset_host_trust_action(host_key: str) -> dict[str, Any]:
        nonlocal config
        result = reset_remote_host_trust(config, host_key)
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
    )
    register_host_routes(
        app,
        hosts_payload=_hosts_payload,
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
                return ({**base_context, "pending": True, "scan_job": folder_scan_job}, status_code)
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
            pending_proposal = _pending_proposal_public_view(_load_pending_proposal(config, normalized_prefix))
            calibration = _load_calibration_state(config, normalized_prefix)
            recent_tuning_sessions = _recent_tuning_sessions(connection, normalized_prefix)
            review_gate = _review_gate(calibration)
            hot_spots = _preview_hotspots(sample_item, calibration)
            calibration_queue = list_queue_summary(connection, limit_per_lane=3)
            encode_job = load_latest_encode_job(connection, normalized_prefix)
            encode_queue = _decorate_encode_queue_for_scheduler(config, summarize_encode_queue(connection))
            if encode_job and encode_job.get("status") == "queued":
                position = encode_queue_position(connection, str(encode_job["job_id"]))
                if position is not None:
                    encode_job["queue_position"] = position[0]
                    encode_job["queue_depth"] = position[1]
            scheduler_policy = dict(encode_queue["state"]["scheduler"])
            encode_job = _decorate_encode_job_for_scheduler(config, encode_job)
            encode_queue_state = encode_queue["state"]
            encode_queue_summary = _encode_queue_summary_copy(encode_queue, encode_queue_state, encode_job)
        policy_source = calibration.get("policy") if calibration else sample_item["resolved_policy"]
        policy = dict(policy_source or {})
        video_policy = dict(policy.get("video") or {})
        resolved_metric, _ = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
        sample_host_statuses = _sample_calibration_host_statuses(config)
        sample_host_key = _default_sample_host_key_from_statuses(sample_host_statuses)
        sample_host_options = _sample_host_options_from_statuses(sample_host_statuses)
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
                "recent_tuning_sessions": recent_tuning_sessions,
                "review_gate": review_gate,
                "calibration_queue": calibration_queue,
                "encode_job": encode_job,
                "encode_queue": encode_queue,
                "encode_queue_state": encode_queue_state,
                "encode_queue_summary": encode_queue_summary,
                "encode_queue_scheduler": scheduler_policy,
                "resolved_metric": resolved_metric.upper(),
                "sample_host_key": sample_host_key,
                "sample_host_options": sample_host_options,
                "sample_host_help_text": _sample_host_help_text(sample_host_options, sample_host_key),
            },
            200,
        )

    def _recent_tuning_sessions(connection: sqlite3.Connection, prefix: str, limit: int = 8) -> list[dict[str, Any]]:
        return recent_tuning_sessions(connection, prefix, load_json_object=_load_json_object, limit=limit)

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
            sample_item=_sample_item,
            operator_requested_experiment=_operator_requested_experiment,
            load_calibration_state=_load_calibration_state,
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

    def _queue_folder_encode_action(normalized_prefix: str, notes: str, bypass_schedule: bool) -> dict[str, Any]:
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
            save_encode_job=save_encode_job,
        )

    def _save_profile_action(normalized_prefix: str) -> dict[str, Any]:
        return save_profile_action(
            config,
            normalized_prefix,
            now_iso=_now_iso,
            load_calibration_state=_load_calibration_state,
            calibration_draft_hash=_calibration_draft_hash,
            save_calibration_state=_save_calibration_state,
            load_advice_state=_load_advice_state,
            record_visual_approval_artifact=record_visual_approval_artifact,
            merge_advice_state=_merge_advice_state,
            upsert_override=_upsert_override,
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
            cancel_queue_process=ENCODE_QUEUE_PROCESS.cancel,
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
        folder_ai_tune_action=_folder_ai_tune_action,
        folder_ai_tune_preview_action=_folder_ai_tune_preview_action,
        folder_ai_tune_confirm_action=_folder_ai_tune_confirm_action,
        clear_folder_tuning_action=_clear_folder_tuning_action,
        queue_folder_encode_action=_queue_folder_encode_action,
        save_profile_action=_save_profile_action,
    )
    register_queue_routes(
        app,
        pause_encode_queue_action=_pause_encode_queue_action,
        resume_encode_queue_action=_resume_encode_queue_action,
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


def main() -> None:
    _load_project_env_file()
    config = load_config(DEFAULT_CONFIG_PATH)
    host = _default_web_host()
    port = int(_preferred_env("MEDIAFORCE_WEB_PORT") or "8777")
    reload_enabled = _default_web_reload_enabled()
    if reload_enabled:
        config_path = str(config.paths.config_path)
        os.environ.setdefault("MEDIAFORCE_CONFIG_PATH", config_path)
        uvicorn.run(
            "mediaforce.web.app:create_reloadable_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
            log_level="info",
        )
        return
    uvicorn.run(create_app(config.paths.config_path), host=host, port=port, log_level="info")


def create_reloadable_app() -> FastAPI:
    config_path = Path(_preferred_env("MEDIAFORCE_CONFIG_PATH") or str(DEFAULT_CONFIG_PATH)).expanduser()
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


def _list_folder_cards(config: MediaforceConfig, connection: sqlite3.Connection) -> list[FolderCard]:
    return cached_folder_cards(
        config,
        connection,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=_folder_group,
        age_days=_age_days,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=lambda prefix: _folder_review_badge(config, prefix),
    )


def _preview_folder_cards(config: MediaforceConfig, connection: sqlite3.Connection) -> list[FolderCard]:
    return preview_folder_cards(
        config,
        connection,
        minimum_recommended_savings_bytes=MIN_RECOMMENDED_SAVINGS_BYTES,
        folder_group=_folder_group,
        estimate_savings_bytes=_estimate_savings_bytes,
        review_badge_for_prefix=lambda prefix: _folder_review_badge(config, prefix),
    )


def _host_runtime_rows(
        connection: sqlite3.Connection, config: MediaforceConfig, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    return host_runtime_rows(
        connection,
        config,
        safe_collect_statuses=_safe_collect_host_statuses,
        decorate_encode_job_for_scheduler=_decorate_encode_job_for_scheduler,
        encode_queue_schedule_profiles=_encode_queue_schedule_profiles,
        host_max_parallel_encodes=_host_max_parallel_encodes,
        host_schedule_profile_key=_host_schedule_profile_key,
        scheduler_allows_encode_run=_scheduler_allows_encode_run,
        format_eta_seconds=_format_eta_seconds,
        job_host_key=_job_host_key,
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
    host = dict(host_payload or {})
    host_key = str(host.get("key") or host.get("host") or host.get("label") or "").strip()
    if not host_key:
        return False
    status = _fresh_host_status_for_key(config, host_key)
    if status is not None and status.available:
        return False
    start_command = _host_lifecycle_start_command(host)
    if not start_command:
        detail = status.detail if status is not None else None
        message = status.message if status is not None else "host unavailable"
        raise RuntimeError(detail or message or "Encode host is not available.")
    result = run_host_lifecycle_command(host, start_command, timeout=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "host start command failed"
        raise RuntimeError(detail)
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
    host = dict(host_payload or {})
    stop_command = _host_lifecycle_stop_command(host)
    if not stop_command:
        return
    result = run_host_lifecycle_command(host, stop_command, timeout=HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "host stop command failed"
        raise RuntimeError(detail)


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


def _sample_host_help_text(sample_host_options: list[dict[str, Any]], selected_key: str) -> str:
    for option in sample_host_options:
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


def _sample_item(connection: sqlite3.Connection, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    rows = connection.execute(
        """
        SELECT *
        FROM library_items
        WHERE rel_path LIKE ?
          AND status IN ('discovered', 'planned', 'validated', 'encoded')
        ORDER BY priority_score DESC, size_bytes DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchall()
    if not rows:
        return None
    row = dict(rows[0])
    return build_manifest_item(row, config)


def _metric_support() -> dict[str, bool]:
    try:
        result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-filters"], check=True, capture_output=True,
                                text=True)
    except Exception:
        return {"vmaf": False, "xpsnr": False, "ssim": False, "psnr": False}
    output = result.stdout.lower()
    return {
        "vmaf": "libvmaf" in output,
        "xpsnr": "xpsnr" in output,
        "ssim": "ssim" in output,
        "psnr": " psnr " in output or "\n ts psnr" in output,
    }


def _metric_status_copy(metric_support: dict[str, bool]) -> str:
    if metric_support.get("vmaf"):
        return "VMAF is available on this machine, so calibrations can use the preferred perceptual metric."
    if metric_support.get("xpsnr"):
        return "This ffmpeg build does not include libvmaf yet, so the app is falling back to XPSNR for calibration right now."
    return "Neither VMAF nor XPSNR is available from the current ffmpeg tooling, so calibration quality checks will fail until one is installed."


def _dominant_summary_key(values: JSONValue) -> str | None:
    if not isinstance(values, dict) or not values:
        return None
    best_key: str | None = None
    best_count = -1
    for key, value in values.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > best_count or (count == best_count and best_key is not None and str(key) < best_key):
            best_key = str(key)
            best_count = count
    return best_key


def _resolution_tier(width: JSONValue, height: JSONValue) -> str | None:
    try:
        width_value = int(width or 0)
        height_value = int(height or 0)
    except (TypeError, ValueError):
        return None
    largest_dimension = max(width_value, height_value)
    if largest_dimension >= 3800:
        return "2160p"
    if largest_dimension >= 1900:
        return "1080p"
    if largest_dimension >= 1200:
        return "720p"
    if width_value > 0 and height_value > 0:
        return f"{width_value}x{height_value}"
    return None


def _seed_collection_shape(prefix: str) -> str:
    parts = Path(prefix).parts
    if len(parts) >= 3 and parts[0].lower() == "tv" and parts[2].lower().startswith("season"):
        return "tv_season"
    if len(parts) >= 2 and parts[0].lower() == "tv":
        return "tv_series"
    if parts and parts[0].lower() in {"movie", "movies", "films"}:
        return "movie_folder"
    return "library_prefix"


def _seed_policy_fragment(raw: JSONValue) -> dict[str, Any]:
    return _tuning_policy_focus(
        {
            "video": dict((raw or {}).get("video") or {}),
            "audio": dict((raw or {}).get("audio") or {}),
            "subtitle": dict((raw or {}).get("subtitle") or {}),
        }
    )


def _seed_class_signals(prefix: str, sample_item: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    suggested_override_raw = summary.get("suggested_override")
    suggested_override: dict[str, Any] = dict(suggested_override_raw) if isinstance(suggested_override_raw,
                                                                                    dict) else {}
    collection_shape = _seed_collection_shape(prefix)
    resolution_tier = _resolution_tier(sample_item.get("width"), sample_item.get("height"))
    dominant_video_codec = _dominant_summary_key(summary.get("video_codecs"))
    video_codecs = summary.get("video_codecs")
    positive_signals: list[str] = []
    caution_flags: list[str] = []

    if collection_shape == "tv_season":
        positive_signals.append("Folder is a single TV season rather than a broad mixed prefix.")
    elif collection_shape == "movie_folder":
        positive_signals.append("Folder is movie-shaped rather than episodic TV.")
    else:
        caution_flags.append("Folder shape is broad enough that the seed should stay close to the base policy.")

    if resolution_tier is not None:
        positive_signals.append(f"Sample item resolves to {resolution_tier}.")
    else:
        caution_flags.append("Sample resolution is unknown, so avoid overfitting the first-pass guess.")

    sample_codec = str(sample_item.get("video_codec") or "").strip().lower()
    if dominant_video_codec and sample_codec and dominant_video_codec == sample_codec:
        positive_signals.append(f"Sample codec matches the folder majority codec ({dominant_video_codec}).")
    if isinstance(video_codecs, dict) and len(video_codecs) > 1:
        caution_flags.append(
            "Folder mixes multiple video codecs, so one sample item may not represent every episode equally.")

    item_count = int(summary.get("item_count") or 0)
    if item_count and item_count < 6:
        caution_flags.append("Small folder sample size means the seed should remain conservative.")

    for reason in list(suggested_override.get("reason") or [])[:2]:
        if reason:
            positive_signals.append(str(reason))

    caution_flags.append(
        "This first-pass seed is only a bounded starting point; measured calibration should confirm any lean move.")
    return {
        "collection_shape": collection_shape,
        "sample_resolution_tier": resolution_tier,
        "dominant_video_codec": dominant_video_codec,
        "positive_signals": positive_signals,
        "caution_flags": caution_flags,
    }


def _build_seed_policy_payload(
        *,
        prefix: str,
        user_note: str,
        base_policy: dict[str, Any],
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        metric_support: dict[str, bool],
) -> dict[str, Any]:
    suggested_override_raw = summary.get("suggested_override")
    suggested_override: dict[str, Any] = dict(suggested_override_raw) if isinstance(suggested_override_raw,
                                                                                    dict) else {}
    requested_experiment = _operator_requested_experiment(user_note, sample_item)
    return {
        "folder": prefix,
        "goal": "Prefer slightly smaller files when the visual difference is hard to spot, but keep first-pass drafts conservative when the class is uncertain.",
        "seed_principles": [
            "Teach media-class taste instead of optimizing one easy title in isolation.",
            "Prefer small, reversible moves away from the base policy.",
            "Clean, forgiving TV can lean a little smaller than default.",
            "Dark, grainy, fast-motion, or uncertain material should stay near the base policy until measured calibration says otherwise.",
        ],
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
            "video_codec": sample_item["video_codec"],
            "video_bitrate": sample_item.get("video_bitrate"),
            "width": sample_item.get("width"),
            "height": sample_item.get("height"),
            "resolution_tier": _resolution_tier(sample_item.get("width"), sample_item.get("height")),
            "duration_seconds": sample_item["duration_seconds"],
            "audio_summary": sample_item["audio_summary"],
            "subtitle_summary": sample_item["subtitle_summary"],
            "recommendation": sample_item.get("recommendation"),
            "recommendation_reason": sample_item.get("recommendation_reason"),
        },
        "summary": {
            "item_count": summary.get("item_count"),
            "total_size_bytes": summary.get("total_size_bytes"),
            "statuses": summary.get("statuses"),
            "video_codecs": summary.get("video_codecs"),
            "audio_codecs": summary.get("audio_codecs"),
            "seasons": summary.get("seasons"),
            "dominant_video_codec": _dominant_summary_key(summary.get("video_codecs")),
            "dominant_audio_codec": _dominant_summary_key(summary.get("audio_codecs")),
            "suggested_override": {
                "reason": list(suggested_override.get("reason") or []),
                "policy_focus": _seed_policy_fragment(suggested_override),
            },
        },
        "class_signals": _seed_class_signals(prefix, sample_item, summary),
        "base_policy": _tuning_policy_focus(base_policy),
        "operator_note": user_note or None,
        "requested_experiment": requested_experiment,
        "metric_support": metric_support,
        "preferred_metric": "vmaf" if metric_support.get("vmaf") else (
            "xpsnr" if metric_support.get("xpsnr") else None),
    }


def _tuning_policy_focus(policy: dict[str, Any]) -> dict[str, Any]:
    focused: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key, value in raw_section.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                cleaned[key] = value
                continue
            if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
                cleaned[key] = list(value)
        if cleaned:
            focused[section] = cleaned
    return focused


def _tuning_policy_key_paths(policy: dict[str, Any]) -> list[str]:
    focused = _tuning_policy_focus(policy)
    paths: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = focused.get(section)
        if not isinstance(raw_section, dict):
            continue
        for key in raw_section:
            paths.append(f"{section}.{key}")
    return paths


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


def _planned_audio_review_context(*, sample_item: dict[str, Any], current_policy: dict[str, Any]) -> dict[str, Any]:
    return planned_audio_review_context(sample_item=sample_item, current_policy=current_policy)


def _encode_queue_scheduler_policy(config: MediaforceConfig) -> dict[str, Any]:
    encode_queue = config.raw.get("encode_queue")
    raw = encode_queue.get("scheduler") if isinstance(encode_queue, dict) else None
    if not isinstance(raw, dict):
        legacy_queue = config.raw.get("heavy_queue")
        raw = legacy_queue.get("scheduler") if isinstance(legacy_queue, dict) else None
    return _normalize_encode_queue_scheduler(raw if isinstance(raw, dict) else None)


def _encode_queue_schedule_profiles(config: MediaforceConfig) -> dict[str, dict[str, Any]]:
    always = _normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})
    always["key"] = ALWAYS_SCHEDULE_PROFILE
    always["label"] = "Always"
    profiles = {ALWAYS_SCHEDULE_PROFILE: always}
    encode_queue = config.raw.get("encode_queue")
    raw_profiles = encode_queue.get("schedule_profiles") if isinstance(encode_queue, dict) else None
    if not isinstance(raw_profiles, list):
        return profiles
    for profile in raw_profiles:
        if not isinstance(profile, dict):
            continue
        key = _canonical_schedule_profile_key(str(profile.get("key") or profile.get("name") or ""))
        if not key or key == ALWAYS_SCHEDULE_PROFILE:
            continue
        normalized = _normalize_encode_queue_scheduler(profile)
        normalized["key"] = key
        normalized["label"] = str(profile.get("label") or key.replace("_", " ").title())
        profiles[key] = normalized
    return profiles


def _schedule_profile_policy_for_host(config: MediaforceConfig, host_payload: dict[str, Any] | None) -> dict[str, Any]:
    profiles = _encode_queue_schedule_profiles(config)
    profile_key = _canonical_schedule_profile_key(
        (host_payload or {}).get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)
    return dict(profiles.get(profile_key) or profiles[DEFAULT_HOST_SCHEDULE_PROFILE])


def _encode_queue_scheduler_summary(policy: dict[str, Any]) -> str:
    if str(policy.get("mode") or "anytime") == "night":
        return f"window {int(policy['start_hour']):02d}:00-{int(policy['end_hour']):02d}:00 in host local time"
    return "runs anytime"


def _host_schedule_now(current: datetime, host_payload: dict[str, Any] | None) -> datetime:
    offset_minutes = (host_payload or {}).get("utc_offset_minutes")
    try:
        if offset_minutes is not None:
            return current.astimezone(timezone(timedelta(minutes=int(offset_minutes))))
    except (TypeError, ValueError):
        pass
    timezone_name = str(
        (host_payload or {}).get("schedule_timezone") or (host_payload or {}).get("timezone") or "").strip()
    if timezone_name:
        try:
            return current.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return current.astimezone()


def _scheduler_allows_encode_run(
        policy: dict[str, Any],
        *,
        bypass_schedule: bool = False,
        now: datetime | None = None,
        host_payload: dict[str, Any] | None = None,
) -> bool:
    if bypass_schedule or str(policy.get("mode") or "anytime") == "anytime":
        return True
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    timezone_name = str(policy.get("timezone") or "local")
    if timezone_name == "host_local":
        local_now = _host_schedule_now(current, host_payload)
    elif timezone_name == "local":
        local_now = current.astimezone()
    else:
        try:
            local_now = current.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            local_now = current.astimezone()
    start_hour_value = policy.get("start_hour")
    if start_hour_value is None:
        start_hour_value = DEFAULT_SCHEDULER_POLICY["start_hour"]
    end_hour_value = policy.get("end_hour")
    if end_hour_value is None:
        end_hour_value = DEFAULT_SCHEDULER_POLICY["end_hour"]
    start_hour = int(str(start_hour_value))
    end_hour = int(str(end_hour_value))
    if start_hour == end_hour:
        return True
    current_hour = local_now.hour
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


def _format_eta_seconds(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _float_or_none(value: JSONValue) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _job_host_key(job: dict[str, Any]) -> str:
    host = dict(job.get("host") or {})
    return str(host.get("key") or host.get("host") or host.get("label") or "").strip()


def _decorate_encode_job_telemetry(job: dict[str, Any]) -> dict[str, Any]:
    decorated = dict(job)
    progress = dict(decorated.get("progress") or {})
    manifest_totals = _encode_job_manifest_totals(decorated)
    total_duration_seconds = float(
        progress.get("total_duration_seconds") or manifest_totals["total_duration_seconds"] or 0.0)
    total_item_count = int(
        progress.get("total_item_count") or manifest_totals["total_item_count"] or decorated.get("item_count") or 0)
    overall_completed_duration_seconds = float(progress.get("overall_completed_duration_seconds") or 0.0)
    remaining_duration_seconds = float(
        progress.get("remaining_duration_seconds") or max(total_duration_seconds - overall_completed_duration_seconds,
                                                          0.0))
    percent_complete = _float_or_none(progress.get("percent_complete"))
    if percent_complete is None and total_duration_seconds > 0:
        percent_complete = min(overall_completed_duration_seconds / total_duration_seconds, 1.0) * 100.0
    fps = _float_or_none(progress.get("fps"))
    speed = _float_or_none(progress.get("speed"))
    eta_seconds = _float_or_none(progress.get("eta_seconds"))
    speed_value = speed if speed not in {None, 0, 0.0} else None
    if eta_seconds is None and speed_value is not None:
        eta_seconds = remaining_duration_seconds / speed_value
    decorated["progress"] = {
        **progress,
        "total_duration_seconds": total_duration_seconds,
        "total_item_count": total_item_count,
        "overall_completed_duration_seconds": overall_completed_duration_seconds,
        "remaining_duration_seconds": remaining_duration_seconds,
        "percent_complete": percent_complete,
        "fps": fps,
        "speed": speed,
        "eta_seconds": eta_seconds,
        "eta_copy": _format_eta_seconds(eta_seconds),
    }
    summary_parts: list[str] = []
    if percent_complete is not None:
        summary_parts.append(f"{percent_complete:.0f}%")
    if speed_value is not None:
        summary_parts.append(f"{speed:.2f}x")
    if fps not in {None, 0, 0.0}:
        summary_parts.append(f"{fps:.1f} fps")
    eta_copy = decorated["progress"].get("eta_copy")
    if eta_copy:
        summary_parts.append(f"Est. ETA {eta_copy}")
    decorated["telemetry_summary"] = " · ".join(summary_parts)
    return decorated


def _encode_queue_telemetry(encode_queue: dict[str, Any]) -> dict[str, Any]:
    running_jobs = [dict(job) for job in encode_queue.get("running") or []]
    queued_jobs = [dict(job) for job in encode_queue.get("queued") or []]
    aggregate_speed = sum(float(((job.get("progress") or {}).get("speed") or 0.0)) for job in running_jobs)
    total_remaining_duration_seconds = sum(
        float(((job.get("progress") or {}).get("remaining_duration_seconds") or 0.0)) for job in running_jobs
    )
    total_remaining_duration_seconds += sum(
        float(((job.get("progress") or {}).get("total_duration_seconds") or 0.0)) for job in queued_jobs
    )
    eta_seconds = (total_remaining_duration_seconds / aggregate_speed) if aggregate_speed > 0 else None
    return {
        "aggregate_speed": aggregate_speed or None,
        "eta_seconds": eta_seconds,
        "eta_copy": _format_eta_seconds(eta_seconds),
        "running_jobs": len(running_jobs),
        "queued_jobs": len(queued_jobs),
    }


def _decorate_encode_job_for_scheduler(config: MediaforceConfig, job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    decorated = dict(job)
    policy = _schedule_profile_policy_for_host(config, dict(decorated.get("host") or {}))
    status = str(decorated.get("status") or "")
    bypass_schedule = bool(decorated.get("bypass_schedule"))
    attempt_count = int(decorated.get("attempt_count") or 0)
    waiting_reason = str(decorated.get("waiting_reason") or "").strip()
    schedule_waiting = (
            status == "queued"
            and not _scheduler_allows_encode_run(policy, bypass_schedule=bypass_schedule,
                                                 host_payload=dict(decorated.get("host") or {}))
    )
    decorated["schedule_waiting"] = schedule_waiting
    decorated["scheduler_summary"] = str(policy["summary"])
    decorated[
        "attempt_summary"] = f"attempt {attempt_count} of {ENCODE_JOB_MAX_ATTEMPTS}" if attempt_count else "not started yet"
    if status == "running":
        decorated["scheduler_status_copy"] = "running now"
    elif status == "needs_attention":
        decorated["scheduler_status_copy"] = waiting_reason or "needs attention before retrying"
    elif status == "retry_backoff":
        decorated["scheduler_status_copy"] = waiting_reason or "waiting for retry backoff"
    elif waiting_reason:
        decorated["scheduler_status_copy"] = waiting_reason
    elif bypass_schedule:
        decorated["scheduler_status_copy"] = "bypassing scheduler"
    elif schedule_waiting:
        decorated["scheduler_status_copy"] = f"waiting for {policy['summary']}"
    else:
        decorated["scheduler_status_copy"] = "ready when a worker is free"
    return _decorate_encode_job_telemetry(decorated)


def _decorate_encode_queue_for_scheduler(config: MediaforceConfig, encode_queue: dict[str, Any]) -> dict[str, Any]:
    policy = _encode_queue_scheduler_policy(config)
    queue_state = dict(encode_queue.get("state") or {})
    queue_state["scheduler"] = policy
    queue_state["scheduler_summary"] = str(policy["summary"])
    queue_state["schedule_profiles"] = list(_encode_queue_schedule_profiles(config).values())
    decorated = dict(encode_queue)
    decorated["state"] = queue_state
    decorated["running"] = [
        _decorate_encode_job_for_scheduler(config, job) or job
        for job in encode_queue.get("running") or []
    ]
    decorated["queued"] = [
        _decorate_encode_job_for_scheduler(config, job) or job
        for job in encode_queue.get("queued") or []
    ]
    decorated["recent"] = [
        _decorate_encode_job_for_scheduler(config, job) or job
        for job in encode_queue.get("recent") or []
    ]
    decorated["queued_waiting_count"] = sum(
        1
        for job in decorated["queued"]
        if bool(job.get("schedule_waiting")) or str(job.get("status") or "") == "retry_backoff"
    )
    decorated["telemetry"] = _encode_queue_telemetry(decorated)
    return decorated


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


def _calibration_draft_hash(payload: dict[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in CALIBRATION_REVIEW_FIELDS
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _review_gate(calibration: dict[str, Any] | None) -> dict[str, Any]:
    if calibration is None:
        return {
            "can_confirm_full": False,
            "message": "Run a sampled calibration first.",
            "status": "missing_sample",
        }

    if str(calibration.get("mode") or "sample") != "sample":
        return {
            "can_confirm_full": False,
            "message": "Run and save a fresh sampled draft before queueing the folder encode.",
            "status": "needs_fresh_sample",
        }

    current_hash = str(calibration.get("draft_hash") or _calibration_draft_hash(calibration))
    accepted_hash = str(calibration.get("accepted_draft_hash") or "")
    accepted_job_id = str(calibration.get("accepted_sample_job_id") or "")
    current_job_id = str(calibration.get("job_id") or "")
    accepted_at = calibration.get("accepted_at")
    review_media_ready = bool(calibration.get("review_media_ready"))
    can_confirm_full = bool(accepted_at and accepted_hash == current_hash and accepted_job_id == current_job_id)
    if can_confirm_full:
        return {
            "can_confirm_full": True,
            "message": f"Approved sample draft saved at {accepted_at}. Folder encode is unlocked.",
            "status": "accepted",
            "accepted_at": accepted_at,
        }

    if not review_media_ready:
        return {
            "can_confirm_full": False,
            "message": "Review clips are unavailable for this draft. Run a fresh sample before approving it.",
            "status": "missing_review_media",
        }

    return {
        "can_confirm_full": False,
        "message": "Review the sample clips, then approve this draft to save the folder policy and unlock folder encode.",
        "status": "needs_approval",
    }


def _encode_queue_summary_copy(
        encode_queue: dict[str, Any],
        encode_queue_state: dict[str, Any],
        encode_job: dict[str, Any] | None,
) -> str:
    parts = [
        f"{int(encode_queue.get('running_count') or 0)} running",
        f"{int(encode_queue.get('queued_count') or 0)} queued",
    ]

    status = str(encode_job.get("status") or "") if encode_job else ""
    if status == "queued" and encode_job and encode_job.get("queue_position"):
        queue_position = int(encode_job["queue_position"])
        queue_depth = int(encode_job.get("queue_depth") or queue_position)
        parts.append(f"this folder is {queue_position} of {queue_depth}")
    elif status == "running":
        parts.append("this folder is active now")
    elif status in {"completed", "failed", "stopped", "needs_attention"}:
        parts.append(f"latest folder job {status}")
    else:
        parts.append("no folder job queued yet")

    waiting_count = int(encode_queue.get("queued_waiting_count") or 0)
    if waiting_count:
        parts.append(f"{waiting_count} waiting")

    queue_eta_copy = str(((encode_queue.get("telemetry") or {}).get("eta_copy") or "")).strip()
    if queue_eta_copy:
        parts.append(f"estimated queue finish in {queue_eta_copy}")

    attention_count = int(encode_queue.get("needs_attention_count") or 0)
    if attention_count:
        parts.append(f"{attention_count} need attention")

    if encode_job and encode_job.get("scheduler_status_copy") and status in {"queued", "retry_backoff", "running",
                                                                             "needs_attention"}:
        parts.append(str(encode_job["scheduler_status_copy"]))

    if encode_queue_state.get("is_paused"):
        parts.append("queue paused")

    return " · ".join(parts)


def _folder_state_deps() -> FolderStateDeps:
    return FolderStateDeps(
        review_file_from_url=_review_file_from_url,
        load_advice_state=_load_advice_state,
        calibration_draft_hash=_calibration_draft_hash,
        tuning_policy_focus=_tuning_policy_focus,
        pending_proposal_trace_public_view=_pending_proposal_trace_public_view,
    )


def _load_calibration_state(config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return load_calibration_state(_folder_state_deps(), config, prefix, _calibration_file(config, prefix))


def _save_calibration_state(config: MediaforceConfig, prefix: str, payload: dict[str, Any]) -> None:
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


def _save_advice_state(config: MediaforceConfig, prefix: str, advice: AdvisorResponse | dict[str, Any]) -> None:
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
    context = dict(trace.get("context") or {})
    if isinstance(context.get("current_policy"), dict):
        context["current_policy"] = _tuning_policy_focus(dict(context["current_policy"]))
    return {
        "prompt_version": trace.get("prompt_version"),
        "raw_response": trace.get("raw_response"),
        "proposed_policy": _tuning_policy_focus(dict(proposed_policy or {})) if isinstance(proposed_policy,
                                                                                           dict) else {},
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


def _load_job_state(connection: sqlite3.Connection, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
    return runtime_load_job_state(connection, config, prefix, _job_runtime_deps())


def _save_job_state(
        connection: sqlite3.Connection, config: MediaforceConfig, prefix: str, payload: dict[str, Any]
) -> None:
    runtime_save_job_state(connection, config, prefix, payload, _job_runtime_deps())


def _calibration_job_belongs_to_current_process(job: dict[str, Any]) -> bool:
    return runtime_calibration_job_belongs_to_current_process(job)


def _expire_calibration_job(
        connection: sqlite3.Connection, config: MediaforceConfig, prefix: str, job: dict[str, Any]
) -> dict[str, Any]:
    return runtime_expire_calibration_job(connection, config, prefix, job, _job_runtime_deps())


def _load_scan_job_state(config: MediaforceConfig, prefix: str | None) -> dict[str, Any] | None:
    return runtime_load_scan_job_state(config, prefix, _scan_job_file)


def _save_scan_job_state(config: MediaforceConfig, prefix: str | None, payload: dict[str, Any]) -> None:
    runtime_save_scan_job_state(config, prefix, payload, _scan_job_file)


def _maybe_schedule_scan(
        connection: sqlite3.Connection, config: MediaforceConfig, prefix: str | None
) -> dict[str, Any] | None:
    return runtime_maybe_schedule_scan(connection, config, prefix, _job_runtime_deps())


def _scan_is_stale(connection: sqlite3.Connection, config: MediaforceConfig, prefix: str | None) -> bool:
    return runtime_scan_is_stale(connection, config, prefix, _job_runtime_deps())


def _latest_scan_completed_at(connection: sqlite3.Connection, prefix: str | None) -> datetime | None:
    return runtime_latest_scan_completed_at(connection, prefix)


def _scan_process_is_alive(pid: JSONValue) -> bool:
    return runtime_scan_process_is_alive(pid)


def _active_scan_from_db(
        connection: sqlite3.Connection, config: MediaforceConfig, prefix: str | None
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
        return [float(clip["timestamp_seconds"]) for clip in calibration["compare_clips"]]
    total_duration = float(sample_item.get("duration_seconds") or 0.0)
    if total_duration <= 0:
        return [0.0]
    usable = max(total_duration - 8.0, 0.0)
    return [round(usable * ratio, 3) for ratio in (0.2, 0.5, 0.8)]


def _folder_group(rel_path: str) -> tuple[str, str, str, str] | None:
    parts = Path(rel_path).parts
    if len(parts) < 2:
        return None
    if parts[0] == "tv" and len(parts) >= 3:
        return ("/".join(parts[:3]), f"{parts[1]} · {parts[2]}", parts[1], "Season")
    return ("/".join(parts[:2]), parts[1], parts[0].title(), "Folder")


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
        for clip in calibration_payload.get(key) or []:
            review_file = _review_file_from_url(config, str((clip or {}).get("path") or ""))
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
        connection: sqlite3.Connection,
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
    review_pack_dir = _review_pack_dir(config, prefix)
    review_pack_present = review_pack_dir.exists()
    artifact_rows = connection.execute(
        "SELECT artifact_path FROM learning_artifacts WHERE prefix = ?",
        (prefix,),
    ).fetchall()
    artifact_paths = [Path(str(row["artifact_path"])) for row in artifact_rows if
                      str(row["artifact_path"] or "").strip()]
    session_count = int(
        connection.execute("SELECT COUNT(*) FROM tuning_sessions WHERE prefix = ?", (prefix,)).fetchone()[0]
    )
    job_count = int(
        connection.execute("SELECT COUNT(*) FROM calibration_jobs WHERE prefix = ?", (prefix,)).fetchone()[0]
    )

    connection.execute("DELETE FROM tuning_sessions WHERE prefix = ?", (prefix,))
    connection.execute("DELETE FROM calibration_jobs WHERE prefix = ?", (prefix,))

    _remove_path_if_exists(calibration_path)
    _remove_path_if_exists(_advice_file(config, prefix))
    _remove_path_if_exists(_proposal_file(config, prefix))
    for artifact_path in artifact_paths:
        _remove_path_if_exists(artifact_path)
    for review_target in review_targets:
        _remove_path_if_exists(review_target)
        if review_target.parent != config.paths.review_dir:
            _prune_empty_parents(review_target.parent, stop_at=config.paths.review_dir)
    _remove_path_if_exists(review_pack_dir)
    if review_pack_dir.parent != config.paths.review_dir:
        _prune_empty_parents(review_pack_dir.parent, stop_at=config.paths.review_dir)

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


def _upsert_override(file_path: Path, prefix: str, policy: dict[str, Any]) -> None:
    content = file_path.read_text() if file_path.exists() else ""
    block = _render_override_block(prefix, policy)
    pattern = re.compile(
        rf'\[\[overrides\]\]\npath_prefix = "{re.escape(prefix)}"\n(?:.*?)(?=\n\[\[overrides\]\]|\Z)',
        re.DOTALL,
    )
    if pattern.search(content):
        updated = pattern.sub(block.rstrip("\n"), content).rstrip() + "\n"
    else:
        updated = content.rstrip() + "\n\n" + block if content.strip() else block
    file_path.write_text(updated)


def _render_override_block(prefix: str, policy: dict[str, Any]) -> str:
    lines = ["[[overrides]]", f'path_prefix = "{prefix}"', 'note = "Saved from the calibration bench."', ""]
    for section in ("video", "audio", "subtitle", "planning"):
        values = policy.get(section) or {}
        if not values:
            continue
        lines.append(f"[overrides.{section}]")
        for key, value in values.items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            elif isinstance(value, list):
                lines.append(f"{key} = {json.dumps(value)}")
            else:
                lines.append(f"{key} = {json.dumps(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _recover_calibration_jobs(connection: sqlite3.Connection, config: MediaforceConfig) -> None:
    running_rows = connection.execute(
        "SELECT job_id FROM calibration_jobs WHERE status = 'running' AND (owner_pid IS NULL OR owner_pid != ?)",
        (os.getpid(),),
    ).fetchall()
    for row in running_rows:
        payload = load_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        _expire_calibration_job(connection, config, str(payload["prefix"]), payload)


def _recover_encode_queue(connection: sqlite3.Connection, config: MediaforceConfig) -> None:
    _reconcile_encode_jobs(connection, config, restart_recovery=True)


def _reconcile_encode_jobs(
        connection: sqlite3.Connection, config: MediaforceConfig, *, restart_recovery: bool = False
) -> None:
    now = datetime.now(tz=UTC)
    running_rows = connection.execute("SELECT job_id FROM encode_jobs WHERE status = 'running'").fetchall()
    for row in running_rows:
        payload = load_encode_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        lease_expires_at = _parse_iso(payload.get("lease_expires_at"))
        if not restart_recovery and lease_expires_at is not None and lease_expires_at > now:
            continue
        failure_kind = "worker_restart" if restart_recovery else "stale_lease"
        failure_message = (
            "Encode queue job was interrupted by a web process restart."
            if restart_recovery
            else "Encode queue job stopped heartbeating and was reclaimed for retry."
        )
        _transition_encode_job_failure(
            connection,
            config,
            payload,
            failure_kind=failure_kind,
            error_message=failure_message,
        )

    retry_backoff_rows = connection.execute(
        "SELECT job_id FROM encode_jobs WHERE status = 'retry_backoff' ORDER BY created_at , rowid "
    ).fetchall()
    for row in retry_backoff_rows:
        payload = load_encode_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        retry_not_before = _parse_iso(payload.get("retry_not_before"))
        if retry_not_before is not None and retry_not_before > now:
            continue
        payload.update(
            {
                "status": "queued",
                "retry_not_before": None,
                "waiting_reason": None,
                "updated_at": _now_iso(),
            }
        )
        save_encode_job(connection, payload)

    state = load_queue_state(connection)
    running_count = int(
        connection.execute("SELECT COUNT(*) FROM encode_jobs WHERE status = 'running'").fetchone()[0]
    )
    if running_count == 0 and (state.get("active_job_id") or state.get("stop_requested")):
        state.update({"active_job_id": None, "stop_requested": False, "updated_at": _now_iso()})
        save_queue_state(connection, state)


def _encode_job_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


def _encode_job_lease_expires_at() -> str:
    return (datetime.now(tz=UTC) + timedelta(seconds=ENCODE_JOB_LEASE_SECONDS)).isoformat(timespec="seconds")


def _encode_job_retry_delay_seconds(attempt_count: int) -> int:
    exponent = max(attempt_count - 1, 0)
    delay = ENCODE_JOB_RETRY_BASE_DELAY_SECONDS * (2 ** exponent)
    return min(delay, ENCODE_JOB_RETRY_MAX_DELAY_SECONDS)


def _encode_job_manifest_totals(job: dict[str, Any]) -> dict[str, Any]:
    manifest_path = Path(str(job.get("manifest_path") or "")).expanduser()
    if not manifest_path.exists():
        return {
            "total_item_count": int(job.get("item_count") or 0),
            "total_duration_seconds": 0.0,
            "total_source_size_bytes": 0,
        }
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "total_item_count": int(job.get("item_count") or 0),
            "total_duration_seconds": 0.0,
            "total_source_size_bytes": 0,
        }
    items = list(payload.get("items") or [])
    return {
        "total_item_count": len(items) or int(job.get("item_count") or 0),
        "total_duration_seconds": sum(float(item.get("duration_seconds") or 0.0) for item in items),
        "total_source_size_bytes": sum(int(item.get("source_size_bytes") or 0) for item in items),
    }


def _initial_encode_job_progress(job: dict[str, Any]) -> dict[str, Any]:
    manifest_totals = _encode_job_manifest_totals(job)
    return {
        **manifest_totals,
        "completed_item_count": 0,
        "completed_duration_seconds": 0.0,
        "overall_completed_duration_seconds": 0.0,
        "remaining_duration_seconds": float(manifest_totals["total_duration_seconds"]),
        "percent_complete": 0.0,
        "progress_state": "starting",
        "fps": None,
        "speed": None,
        "eta_seconds": None,
        "elapsed_seconds": 0.0,
        "out_time_seconds": 0.0,
        "updated_at": _now_iso(),
    }


def _persist_encode_job_progress(config_path: Path, job_id: str, progress: dict[str, Any]) -> None:
    with open_db(load_config(config_path).paths.db_path) as connection:
        job = load_encode_job(connection, job_id)
        if job is None or str(job.get("status") or "") != "running":
            return
        job.update({"progress": {**progress, "updated_at": _now_iso()}, "updated_at": _now_iso()})
        save_encode_job(connection, job)


def _finalize_encode_job_progress(job: dict[str, Any], *, terminal_state: str) -> dict[str, Any] | None:
    progress = dict(job.get("progress") or {})
    if not progress:
        return None
    total_duration_seconds = float(progress.get("total_duration_seconds") or 0.0)
    total_item_count = int(progress.get("total_item_count") or int(job.get("item_count") or 0))
    if terminal_state == "completed":
        progress.update(
            {
                "completed_item_count": total_item_count,
                "completed_duration_seconds": total_duration_seconds,
                "overall_completed_duration_seconds": total_duration_seconds,
                "remaining_duration_seconds": 0.0,
                "percent_complete": 100.0,
                "eta_seconds": 0.0,
                "progress_state": "completed",
                "updated_at": _now_iso(),
            }
        )
        return progress
    progress.update({"progress_state": terminal_state, "updated_at": _now_iso()})
    return progress


def _encode_failure_is_host_related(failure_kind: str, error_message: str, host_payload: dict[str, Any]) -> bool:
    if failure_kind in {"host_unavailable", "ssh_transport"}:
        return True
    lowered = error_message.lower()
    markers = (
        "host key verification failed",
        "could not resolve hostname",
        "temporary failure in name resolution",
        "name or service not known",
        "nodename nor servname provided",
        "no route to host",
        "connection refused",
        "connection reset",
        "operation timed out",
        "broken pipe",
        "ssh:",
    )
    if any(marker in lowered for marker in markers):
        return True
    return str(host_payload.get("mode") or "") == "ssh" and "permission denied" in lowered


def _encode_failure_is_retryable(failure_kind: str, error_message: str, host_payload: dict[str, Any]) -> bool:
    if failure_kind in {"worker_restart", "stale_lease", "host_unavailable", "ssh_transport"}:
        return True
    if failure_kind in {"stopped", "deterministic"}:
        return False
    return _encode_failure_is_host_related(failure_kind, error_message, host_payload)


def _encode_retry_waiting_reason(*, failure_kind: str, retry_not_before: str) -> str:
    reason = {
        "worker_restart": "worker restart",
        "stale_lease": "stale worker lease",
        "host_unavailable": "host availability issue",
        "ssh_transport": "SSH transport failure",
    }.get(failure_kind, "retryable failure")
    return f"retrying after {reason} at {retry_not_before}"


def _transition_encode_job_failure(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        job: dict[str, Any],
        *,
        failure_kind: str,
        error_message: str,
) -> None:
    _ = config
    now = datetime.now(tz=UTC)
    now_iso = now.isoformat(timespec="seconds")
    assigned_host = dict(job.get("host") or {})
    attempt_count = int(job.get("attempt_count") or 0)
    retryable = _encode_failure_is_retryable(failure_kind, error_message, assigned_host)
    host_related = _encode_failure_is_host_related(failure_kind, error_message, assigned_host)
    job.update(
        {
            "process_pid": None,
            "leased_at": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "worker_id": None,
            "last_failure_kind": failure_kind,
            "last_failure_at": now_iso,
            "error": error_message,
            "last_host": assigned_host,
            "progress": _finalize_encode_job_progress(job, terminal_state="needs_attention"),
            "updated_at": now_iso,
        }
    )

    if retryable and attempt_count < ENCODE_JOB_MAX_ATTEMPTS:
        _cleanup_encode_retry_artifacts(connection, manifest_path=Path(str(job["manifest_path"])))
        retry_delay = _encode_job_retry_delay_seconds(attempt_count)
        retry_not_before = (now + timedelta(seconds=retry_delay)).isoformat(timespec="seconds")
        job.update(
            {
                "status": "retry_backoff",
                "finished_at": None,
                "retry_not_before": retry_not_before,
                "waiting_reason": _encode_retry_waiting_reason(
                    failure_kind=failure_kind,
                    retry_not_before=retry_not_before,
                ),
                "terminal_reason": None,
                "host_cooldown_until": (
                    (now + timedelta(seconds=ENCODE_HOST_COOLDOWN_SECONDS)).isoformat(timespec="seconds")
                    if host_related and assigned_host
                    else None
                ),
            }
        )
        save_encode_job(connection, job)
        return

    terminal_reason = "max_attempts_exhausted" if retryable else failure_kind
    job.update(
        {
            "status": "needs_attention",
            "finished_at": now_iso,
            "retry_not_before": None,
            "waiting_reason": None,
            "terminal_reason": terminal_reason,
            "host_cooldown_until": None,
        }
    )
    save_encode_job(connection, job)


def _cleanup_encode_retry_artifacts(connection: sqlite3.Connection, *, manifest_path: Path) -> None:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    now_iso = _now_iso()
    for item in manifest.get("items") or []:
        staging_value = item.get("staging_path")
        if staging_value:
            staging_path = Path(str(staging_value))
            _remove_path(staging_path)
            _remove_path(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"))
        library_item_id = item.get("library_item_id")
        if library_item_id is None:
            continue
        stage_row = connection.execute(
            "SELECT promoted_at FROM staged_artifacts WHERE library_item_id = ?",
            (library_item_id,),
        ).fetchone()
        if stage_row is not None and not stage_row["promoted_at"]:
            connection.execute(
                "DELETE FROM staged_artifacts WHERE library_item_id = ?",
                (library_item_id,),
            )
        connection.execute(
            """
            UPDATE library_items
            SET status     = CASE WHEN status = 'promoted' THEN status ELSE 'planned' END,
                updated_at = ?
            WHERE id = ?
              AND status != 'promoted'
            """,
            (now_iso, library_item_id),
        )


def _select_encode_host(connection: sqlite3.Connection, config: MediaforceConfig, job: dict[str, Any]) -> tuple[
    dict[str, Any] | None, str | None]:
    host_rows = sorted(_host_runtime_rows(connection, config),
                       key=lambda status: (-int(status["priority"]), str(status["label"])))
    now = datetime.now(tz=UTC)
    bypass_schedule = bool(job.get("bypass_schedule"))
    active_hosts = [
        host
        for host in host_rows
        if bool(host.get("available"))
           and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
           and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
           and (
                   bypass_schedule
                   or _scheduler_allows_encode_run(
               _schedule_profile_policy_for_host(config, host),
               now=now,
               host_payload=host,
           )
           )
    ]
    encode_capable_hosts = [
        host
        for host in host_rows
        if "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
    ]
    startable_hosts = [
        host
        for host in host_rows
        if not bool(host.get("available"))
           and bool(_host_lifecycle_start_command(host))
           and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
           and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
           and (
                   bypass_schedule
                   or _scheduler_allows_encode_run(
               _schedule_profile_policy_for_host(config, host),
               now=now,
               host_payload=host,
           )
           )
    ]
    if not encode_capable_hosts and not startable_hosts:
        return None, "waiting for an available encode host"

    cooldown_until = _parse_iso(job.get("host_cooldown_until"))
    last_host = dict(job.get("last_host") or {})
    blocked_keys = {
        str(last_host.get("key") or ""),
        str(last_host.get("label") or ""),
        str(last_host.get("host") or ""),
    }
    if cooldown_until is not None and cooldown_until > now and any(blocked_keys):
        eligible_active_hosts = [
            host
            for host in active_hosts
            if str(host.get("key") or "") not in blocked_keys and str(host.get("label") or "") not in blocked_keys
        ]
        if eligible_active_hosts:
            return dict(eligible_active_hosts[0]), None
        eligible_startable_hosts = [
            host
            for host in startable_hosts
            if str(host.get("key") or "") not in blocked_keys and str(host.get("label") or "") not in blocked_keys
        ]
        if eligible_startable_hosts:
            return dict(eligible_startable_hosts[0]), None
        host_name = str(last_host.get("label") or last_host.get("key") or "the last host")
        return None, f"waiting for host cooldown to expire on {host_name}"
    if active_hosts:
        return dict(active_hosts[0]), None
    if startable_hosts:
        return dict(startable_hosts[0]), None
    if any(int(host.get("active_encode_count") or 0) >= int(host.get("max_parallel_encodes") or 1) for host in
           encode_capable_hosts):
        return None, "waiting for host capacity to free up"
    if any(
            not _scheduler_allows_encode_run(
                _schedule_profile_policy_for_host(config, host),
                now=now,
                host_payload=host,
            )
            for host in encode_capable_hosts
    ):
        return None, "waiting for a host schedule window"
    return None, "waiting for an available encode host"


def _classify_encode_failure(exc: Exception, job: dict[str, Any]) -> str:
    message = str(exc).lower()
    host_payload = dict(job.get("host") or {})
    if _encode_failure_is_host_related("ssh_transport", message, host_payload):
        return "ssh_transport"
    if "staging file already exists" in message:
        return "deterministic"
    return "deterministic"


def _encode_job_heartbeat_loop(*, config_path: Path, job_id: str, worker_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(ENCODE_JOB_HEARTBEAT_SECONDS):
        with open_db(load_config(config_path).paths.db_path) as connection:
            job = load_encode_job(connection, job_id)
            if job is None or str(job.get("status") or "") != "running":
                return
            if str(job.get("worker_id") or "") != worker_id:
                return
            job.update(
                {
                    "heartbeat_at": _now_iso(),
                    "lease_expires_at": _encode_job_lease_expires_at(),
                    "process_pid": ENCODE_QUEUE_PROCESS.pid,
                    "updated_at": _now_iso(),
                }
            )
            save_encode_job(connection, job)


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
        connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    if action != "baseline" or existing_calibration is not None:
        return None
    summary = inspect_prefix(connection, config, prefix)
    metric_support = _metric_support()
    payload = _build_seed_policy_payload(
        prefix=prefix,
        user_note=user_note,
        base_policy=base_policy,
        sample_item=sample_item,
        summary=summary,
        metric_support=metric_support,
    )
    seed_response = request_seed_policy(project_root=config.paths.project_root, payload=payload)
    if not seed_response.ok or not seed_response.proposed_policy:
        return {
            "policy": base_policy,
            "job_fields": {
                "seed_source": "default",
                "seed_summary": seed_response.summary,
                "seed_diagnosis": seed_response.diagnosis,
                "seed_confidence": seed_response.confidence,
                "seed_evidence_checked": seed_response.evidence_checked,
                "seed_suggested_follow_up": seed_response.suggested_follow_up,
                "seed_request_disposition": seed_response.request_disposition,
                "seed_request_response": seed_response.request_response,
                "seed_feasibility_note": seed_response.feasibility_note,
                "seed_prompt_version": seed_response.prompt_version,
                "seed_raw_response": seed_response.raw,
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
                "seed_context_payload": payload,
            },
        }
    seeded_policy, applied_fragment = apply_seed_policy(base_policy, seed_response.proposed_policy)
    seed_source = "ai" if applied_fragment else "default"
    return {
        "policy": seeded_policy,
        "job_fields": {
            "seed_source": seed_source,
            "seed_summary": seed_response.summary,
            "seed_diagnosis": seed_response.diagnosis,
            "seed_confidence": seed_response.confidence,
            "seed_evidence_checked": seed_response.evidence_checked,
            "seed_suggested_follow_up": seed_response.suggested_follow_up,
            "seed_request_disposition": seed_response.request_disposition,
            "seed_request_response": seed_response.request_response,
            "seed_feasibility_note": seed_response.feasibility_note,
            "seed_prompt_version": seed_response.prompt_version,
            "seed_raw_response": seed_response.raw,
            "seed_proposed_policy": seed_response.proposed_policy,
            "seed_applied_policy": applied_fragment or None,
            "seed_context_payload": payload,
        },
    }


def _tuning_advice_payload(
        *,
        tuning: TuningPolicyResponse,
        note: str,
        applied_fragment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": tuning.ok,
        "summary": tuning.summary,
        "raw": tuning.raw,
        "kind": "ai_tune",
        "operator_note": note,
        "prompt_version": tuning.prompt_version,
        "request_disposition": tuning.request_disposition,
        "request_response": tuning.request_response,
        "feasibility_note": tuning.feasibility_note,
        "diagnosis": tuning.diagnosis,
        "confidence": tuning.confidence,
        "evidence_checked": tuning.evidence_checked,
        "suggested_follow_up": tuning.suggested_follow_up,
        "applied_policy": applied_fragment,
        "toolbelt_used": tuning.toolbelt_used,
        "self_check": tuning.self_check,
    }


def _seed_advice_payload(note: str, seed_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if seed_metadata is None and not note:
        return None
    job_fields = dict(seed_metadata.get("job_fields") or {}) if seed_metadata else {}
    return {
        "ok": True,
        "summary": job_fields.get("seed_summary") or "Queued an AI-guided first sample baseline.",
        "raw": job_fields.get("seed_raw_response") or "",
        "kind": "seed_baseline",
        "operator_note": note or None,
        "prompt_version": job_fields.get("seed_prompt_version"),
        "request_disposition": job_fields.get("seed_request_disposition"),
        "request_response": job_fields.get("seed_request_response"),
        "feasibility_note": job_fields.get("seed_feasibility_note"),
        "diagnosis": job_fields.get("seed_diagnosis"),
        "confidence": job_fields.get("seed_confidence"),
        "evidence_checked": list(job_fields.get("seed_evidence_checked") or []),
        "suggested_follow_up": job_fields.get("seed_suggested_follow_up"),
        "applied_policy": job_fields.get("seed_applied_policy"),
    }


def _job_seed_metadata(job_payload: dict[str, Any]) -> dict[str, Any] | None:
    if not any(job_payload.get(key) is not None for key in
               ("seed_source", "seed_prompt_version", "seed_raw_response", "seed_proposed_policy",
                "seed_applied_policy")):
        return None
    return {
        "source": job_payload.get("seed_source"),
        "summary": job_payload.get("seed_summary"),
        "diagnosis": job_payload.get("seed_diagnosis"),
        "confidence": job_payload.get("seed_confidence"),
        "request_disposition": job_payload.get("seed_request_disposition"),
        "request_response": job_payload.get("seed_request_response"),
        "feasibility_note": job_payload.get("seed_feasibility_note"),
        "prompt_version": job_payload.get("seed_prompt_version"),
        "raw_response": job_payload.get("seed_raw_response"),
        "proposed_policy": job_payload.get("seed_proposed_policy"),
        "applied_policy": job_payload.get("seed_applied_policy"),
    }


def _summarize_calibration_result(calibration_payload: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "mode": calibration_payload.get("mode"),
        "action": calibration_payload.get("action"),
        "policy_seed": calibration_payload.get("policy_seed"),
    }
    if calibration_payload.get("mode") == "sample":
        sample_result = calibration_payload.get("sample_result") or {}
        summary["sample_result"] = {
            "chosen_crf": sample_result.get("chosen_crf"),
            "quality_metric": sample_result.get("quality_metric"),
            "quality_score": sample_result.get("quality_score"),
            "predicted_total_size_bytes": sample_result.get("predicted_total_size_bytes"),
            "predicted_encode_percent": sample_result.get("predicted_encode_percent"),
        }
    else:
        encode_result = calibration_payload.get("encode_result") or {}
        summary["encode_result"] = {
            "chosen_crf": encode_result.get("chosen_crf"),
            "quality_metric": encode_result.get("quality_metric"),
            "quality_score": encode_result.get("quality_score"),
            "staging_size_bytes": encode_result.get("staging_size_bytes"),
        }
    return summary


def _start_encode_queue_worker(config: MediaforceConfig) -> None:
    global ENCODE_QUEUE_WORKER_STARTED
    with ENCODE_QUEUE_WORKER_LOCK:
        if ENCODE_QUEUE_WORKER_STARTED:
            return
        thread = threading.Thread(target=_encode_queue_worker_loop, kwargs={"config_path": config.paths.config_path},
                                  daemon=True)
        thread.start()
        ENCODE_QUEUE_WORKER_STARTED = True


def _encode_queue_worker_loop(*, config_path: Path) -> None:
    while True:
        try:
            _process_encode_queue_once(config_path=config_path)
        except Exception:
            pass
        threading.Event().wait(ENCODE_QUEUE_POLL_SECONDS)


def _process_encode_queue_once(*, config_path: Path) -> None:
    config = load_config(config_path)
    with open_db(config.paths.db_path) as connection:
        ensure_queue_state(connection, updated_at=_now_iso())
        _reconcile_encode_jobs(connection, config)
        state = load_queue_state(connection)
        running_job = load_active_encode_job(connection)
        if running_job is not None:
            if state.get("stop_requested"):
                ENCODE_QUEUE_PROCESS.cancel()
            return
        if state.get("stop_requested"):
            state.update({"stop_requested": False, "active_job_id": None, "updated_at": _now_iso()})
            save_queue_state(connection, state)
        if state.get("is_paused"):
            return
        next_job = _load_next_runnable_encode_job(connection, config)
        if next_job is None:
            return
        worker_id = _encode_job_worker_id()
        now_iso = _now_iso()
        state.update({"active_job_id": next_job["job_id"], "updated_at": _now_iso()})
        save_queue_state(connection, state)
        next_job.update(
            {
                "status": "running",
                "started_at": now_iso,
                "finished_at": None,
                "process_pid": None,
                "leased_at": now_iso,
                "heartbeat_at": now_iso,
                "lease_expires_at": _encode_job_lease_expires_at(),
                "worker_id": worker_id,
                "attempt_count": int(next_job.get("attempt_count") or 0) + 1,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": None,
                "last_failure_kind": None,
                "progress": _initial_encode_job_progress(next_job),
                "updated_at": now_iso,
            }
        )
        save_encode_job(connection, next_job)

    _run_encode_job(config_path=config_path, job_id=str(next_job["job_id"]))


def _load_next_runnable_encode_job(
        connection: sqlite3.Connection, config: MediaforceConfig
) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT job_id FROM encode_jobs WHERE status = 'queued' ORDER BY created_at , rowid "
    ).fetchall()
    for row in rows:
        job = load_encode_job(connection, str(row["job_id"]))
        if job is None:
            continue
        host_payload, waiting_reason = _select_encode_host(connection, config, job)
        if host_payload is None:
            if str(job.get("waiting_reason") or "") != str(waiting_reason or ""):
                job.update({"waiting_reason": waiting_reason, "updated_at": _now_iso()})
                save_encode_job(connection, job)
            continue
        if job.get("waiting_reason") or job.get("host") != host_payload:
            job.update({"waiting_reason": None, "host": host_payload, "updated_at": _now_iso()})
            save_encode_job(connection, job)
        return job
    return None


def _run_encode_job(*, config_path: Path, job_id: str) -> None:
    config = load_config(config_path)
    ENCODE_QUEUE_PROCESS.reset()
    with open_db(config.paths.db_path) as connection:
        job = load_encode_job(connection, job_id)
        if job is None:
            return
        manifest_path = Path(job["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        indexes = list(range(len(manifest.get("items") or [])))
        job.update({"process_pid": ENCODE_QUEUE_PROCESS.pid, "updated_at": _now_iso()})
        save_encode_job(connection, job)

    progress_write_lock = threading.Lock()
    last_progress_write = 0.0

    def report_progress(progress: dict[str, Any]) -> None:
        nonlocal last_progress_write
        now_monotonic = time.monotonic()
        progress_state = str(progress.get("progress_state") or "")
        if progress_state != "end" and (
                now_monotonic - last_progress_write) < ENCODE_JOB_PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        with progress_write_lock:
            if progress_state != "end" and (
                    now_monotonic - last_progress_write) < ENCODE_JOB_PROGRESS_WRITE_INTERVAL_SECONDS:
                return
            last_progress_write = now_monotonic
        _persist_encode_job_progress(config_path, job_id, progress)

    heartbeat_stop = threading.Event()
    worker_id = str(job.get("worker_id") or _encode_job_worker_id())
    heartbeat_thread = threading.Thread(
        target=_encode_job_heartbeat_loop,
        kwargs={
            "config_path": config_path,
            "job_id": job_id,
            "worker_id": worker_id,
            "stop_event": heartbeat_stop,
        },
        daemon=True,
        name=f"encode-heartbeat-{job_id}",
    )
    heartbeat_thread.start()
    final_status: str | None = None
    failure_kind: str | None = None
    error: str | None = None
    started_host_for_job = False
    try:
        ENCODE_QUEUE_PROCESS.throw_if_cancelled()
        started_host_for_job = _ensure_encode_host_ready(config, job.get("host"))
        with open_db(config.paths.db_path) as connection:
            encode_manifest_items(
                connection,
                config,
                manifest_path,
                manifest,
                indexes,
                overwrite=False,
                process_controller=ENCODE_QUEUE_PROCESS,
                host=job.get("host"),
                progress_callback=report_progress,
            )
        final_status = "completed"
    except ProcessCancelledError:
        final_status = "stopped"
        error = "Encode queue job was stopped and cleaned up."
    except Exception as exc:
        failure_kind = _classify_encode_failure(exc, job)
        error = str(exc)
    finally:
        if started_host_for_job:
            try:
                _stop_encode_host_if_configured(config, job.get("host"))
            except Exception as exc:
                LOGGER.warning("Encode host stop command failed for %s: %s", job_id, exc)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        with open_db(config.paths.db_path) as connection:
            job = load_encode_job(connection, job_id)
            if job is not None:
                if final_status is not None:
                    job.update(
                        {
                            "status": final_status,
                            "finished_at": _now_iso(),
                            "error": error,
                            "process_pid": None,
                            "leased_at": None,
                            "lease_expires_at": None,
                            "heartbeat_at": None,
                            "worker_id": None,
                            "retry_not_before": None,
                            "waiting_reason": None,
                            "terminal_reason": None,
                            "last_failure_kind": None,
                            "host_cooldown_until": None,
                            "progress": _finalize_encode_job_progress(job, terminal_state=final_status),
                            "updated_at": _now_iso(),
                        }
                    )
                    save_encode_job(connection, job)
                elif error is not None:
                    _transition_encode_job_failure(
                        connection,
                        config,
                        job,
                        failure_kind=failure_kind or "deterministic",
                        error_message=error,
                    )
            state = load_queue_state(connection)
            state.update({"active_job_id": None, "stop_requested": False, "updated_at": _now_iso()})
            save_queue_state(connection, state)
        ENCODE_QUEUE_PROCESS.reset()


def _run_periodic_cleanup(config: MediaforceConfig, cleanup_lock: threading.Lock) -> None:
    if not cleanup_lock.acquire(blocking=False):
        return
    try:
        purge_transient_artifacts(config)
    finally:
        cleanup_lock.release()


def _snapshot_staged_artifact(connection: sqlite3.Connection, library_item_id: int) -> dict[str, Any] | None:
    return runtime_snapshot_staged_artifact(connection, library_item_id, CALIBRATION_STAGED_ARTIFACT_COLUMNS)


def _restore_staged_artifact(
        connection: sqlite3.Connection, library_item_id: int, snapshot: dict[str, Any] | None
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
        connection: sqlite3.Connection,
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
