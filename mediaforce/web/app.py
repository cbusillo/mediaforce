from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mediaforce.advisor import AdvisorResponse, TuningPolicyResponse, apply_seed_policy, request_note_tuning, request_seed_policy
from mediaforce.binaries import ffmpeg_binary
from mediaforce.calibration_jobs import claim_next_queued_calibration_job, load_job, load_latest_job, list_queue_summary, queue_position, save_job
from mediaforce.config import DEFAULT_CONFIG_PATH, HarnessConfig, load_config, load_runtime_settings, save_runtime_settings
from mediaforce.db import open_db
from mediaforce.encode_queue import DEFAULT_SCHEDULER_POLICY, ensure_queue_state, load_active_encode_job, load_encode_job, load_latest_encode_job, load_queue_state, queue_position as encode_queue_position, save_encode_job, save_queue_state, summarize_encode_queue
from mediaforce.execution import (
    build_svt_params,
    describe_item_plan,
    encode_manifest_items,
    estimate_output_overhead_bytes,
    search_quality_for_source,
    validate_manifest_items,
)
from mediaforce.folder_profiles import inspect_prefix
from mediaforce.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.planner import build_manifest_item
from mediaforce.remote import (
    DEFAULT_HOST_CAPABILITIES,
    HostStatus,
    collect_host_statuses,
    host_status_targets_current_machine,
    prepare_remote_host_with_password,
    reset_remote_host_trust,
)
from mediaforce.run_manifests import create_folder_manifest
from mediaforce.review import (
    encode_preview_clips,
    generate_compare_clips,
    generate_compare_clips_from_previews,
    recommend_review_timestamps,
)
from mediaforce.quality import run_sample_encode, select_quality_metric
from mediaforce.scanner import scan_library
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.tuning_memory import promote_learning_artifact, record_tuning_session, retrieve_learning_context

MIN_RECOMMENDED_SAVINGS_BYTES = 100 * 1024 * 1024
FULL_SCAN_STALE_AFTER = timedelta(hours=24)
PREFIX_SCAN_STALE_AFTER = timedelta(minutes=15)
SCAN_RETRY_COOLDOWN = timedelta(minutes=5)
CALIBRATION_JOB_NOTICE_AFTER = timedelta(hours=1)
SAMPLE_CALIBRATION_CONCURRENCY = 2
FULL_CALIBRATION_CONCURRENCY = 1
CALIBRATION_QUEUE_POLL_SECONDS = 2.0
ENCODE_QUEUE_POLL_SECONDS = 2.0
ENCODE_JOB_LEASE_SECONDS = 45
ENCODE_JOB_HEARTBEAT_SECONDS = 10.0
ENCODE_JOB_RETRY_BASE_DELAY_SECONDS = 60
ENCODE_JOB_RETRY_MAX_DELAY_SECONDS = 15 * 60
ENCODE_JOB_MAX_ATTEMPTS = 3
ENCODE_HOST_COOLDOWN_SECONDS = 10 * 60
ALWAYS_SCHEDULE_PROFILE = "always"
LEGACY_QUEUE_WINDOW_SCHEDULE_PROFILE = "queue_window"
LEGACY_DEFAULT_SCHEDULE_PROFILE = "default"
DEFAULT_HOST_SCHEDULE_PROFILE = ALWAYS_SCHEDULE_PROFILE
DEFAULT_HOST_MAX_PARALLEL_ENCODES = 1
HOST_CAPABILITY_OPTIONS = (
    {"key": "encode_queue", "label": "Queue encodes", "help": "Allow this host to run queued folder encodes."},
    {
        "key": "sample_calibration",
        "label": "Sample calibration",
        "help": "Allow this host to handle sampled calibration work and AI-guided sample retries.",
    },
)
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
CALIBRATION_QUEUE_WORKER_LOCK = threading.Lock()
CALIBRATION_QUEUE_WORKER_STARTED = False
ENCODE_QUEUE_PROCESS = ManagedProcessController()
ENCODE_QUEUE_WORKER_LOCK = threading.Lock()
ENCODE_QUEUE_WORKER_STARTED = False
FOLDER_CARD_CACHE_LOCK = threading.Lock()
FOLDER_CARD_CACHE_KEY: tuple[str, int] | None = None
FOLDER_CARD_CACHE_VALUE: list[FolderCard] = []
CALIBRATION_REVIEW_FIELDS = {
    "accepted_at",
    "accepted_draft_hash",
    "accepted_sample_job_id",
    "draft_hash",
}


@dataclass(slots=True)
class FolderCard:
    prefix: str
    title: str
    subtitle: str
    scope_label: str
    item_count: int
    pending_count: int
    total_size_bytes: int
    estimated_savings_bytes: int
    average_age_days: float
    sort_score: float
    statuses: dict[str, int]
    video_codecs: dict[str, int]


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

    @app.middleware("http")
    async def periodic_cleanup(request: Request, call_next: Any) -> Any:
        _run_periodic_cleanup(config, cleanup_lock)
        return await call_next(request)

    def _settings_page_payload(
        *,
        error: str | None = None,
        saved: bool = False,
        host_notice: str | None = None,
        host_notice_kind: str | None = None,
        libraries: list[dict[str, str]] | None = None,
        remote_hosts: list[dict[str, str]] | None = None,
        transcode_root: str | None = None,
        encode_queue_scheduler: dict[str, Any] | None = None,
        schedule_profiles: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        resolved_transcode_root = _settings_transcode_root_value(config) if transcode_root is None else transcode_root
        resolved_encode_queue_scheduler = (
            _normalize_encode_queue_scheduler(encode_queue_scheduler)
            if encode_queue_scheduler is not None
            else _encode_queue_scheduler_policy(config)
        )
        resolved_schedule_profiles = (
            _index_schedule_profile_rows(schedule_profiles)
            if schedule_profiles is not None
            else _settings_schedule_profile_rows_for_config(config)
        )
        return {
            "error": error,
            "saved": saved,
            "host_notice": host_notice,
            "host_notice_kind": host_notice_kind,
            "libraries": _index_settings_library_rows(libraries) if libraries is not None else _settings_library_rows_for_config(config),
            "remote_hosts": _index_settings_remote_rows(remote_hosts) if remote_hosts is not None else _settings_remote_rows_for_config(config),
            "transcode_root": resolved_transcode_root,
            "encode_queue_scheduler": resolved_encode_queue_scheduler,
            "schedule_profiles": resolved_schedule_profiles,
            "schedule_profile_options": _schedule_profile_options(
                schedule_profiles=resolved_schedule_profiles,
            ),
            "host_capability_options": list(HOST_CAPABILITY_OPTIONS),
            "archive_root": _settings_archive_root(resolved_transcode_root),
            "runtime_settings_path": str(config.paths.runtime_settings_path),
            "repo_config_path": str(config.paths.config_path),
        }

    def _dashboard_content_payload() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            scan_job = _maybe_schedule_scan(connection, config, prefix=None)
            folders = _cached_folder_cards(config, connection)
            calibration_queue = list_queue_summary(connection)
            encode_queue = _decorate_encode_queue_for_scheduler(config, summarize_encode_queue(connection))
        return {
			"folders": [asdict(folder) for folder in folders],
            "scan_job": scan_job,
            "calibration_queue": calibration_queue,
            "encode_queue": encode_queue,
            "catalog_empty": not folders,
        }

    def _folder_status_payload(normalized_prefix: str) -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            calibration_job = _load_job_state(connection, config, normalized_prefix)
            folder_scan_job = _load_scan_job_state(config, normalized_prefix)
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

    @app.get("/api/dashboard")
    def api_dashboard() -> JSONResponse:
        metric_support = _metric_support()
        return JSONResponse(
            {
                **_dashboard_content_payload(),
				"metric_support": dict(metric_support),
                "metric_status_copy": _metric_status_copy(metric_support),
            }
        )

    @app.get("/api/settings")
    def api_settings() -> JSONResponse:
        return JSONResponse(_settings_page_payload())

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
        save_runtime_settings(config.paths.runtime_settings_path, merged_runtime_settings)
        config = load_config(config.paths.config_path)
        app.state.config = config
        return {"ok": True, "message": "Settings saved.", "settings": _settings_page_payload(saved=True)}

    @app.post("/api/settings")
    async def api_settings_save(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            result = _save_settings_action(
                libraries=[dict(item) for item in body.get("libraries", [])],
                remote_hosts=[dict(item) for item in body.get("remote_hosts", [])],
                transcode_root=str(body.get("transcode_root", "")).strip(),
                encode_queue_scheduler=dict(body.get("encode_queue_scheduler", {})),
                schedule_profiles=[dict(item) for item in body.get("schedule_profiles", [])],
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse(result)

    def _prepare_host_action(host_key: str, remote_password: str | None = None) -> dict[str, Any]:
        nonlocal config
        result = prepare_remote_host_with_password(config, host_key, password=remote_password or None)
        config = load_config(config.paths.config_path)
        app.state.config = config
        notice = result.message if not result.detail else f"{result.message} Details: {result.detail}"
        return {"ok": result.ok, "message": notice, "kind": "success" if result.ok else "error"}

    @app.post("/api/hosts/prepare")
    async def api_host_prepare(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(
            _prepare_host_action(
                str(body.get("host_key", "")).strip(),
                str(body.get("remote_password", "")).strip() or None,
            )
        )

    def _reset_host_trust_action(host_key: str) -> dict[str, Any]:
        nonlocal config
        result = reset_remote_host_trust(config, host_key)
        config = load_config(config.paths.config_path)
        app.state.config = config
        notice = result.message if not result.detail else f"{result.message} Details: {result.detail}"
        return {"ok": result.ok, "message": notice, "kind": "success" if result.ok else "error"}

    @app.post("/api/hosts/reset-trust")
    async def api_host_reset_trust(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(_reset_host_trust_action(str(body.get("host_key", "")).strip()))

    @app.get("/api/hosts")
    def api_hosts(compact: int = 0) -> JSONResponse:
        with open_db(config.paths.db_path) as connection:
            hosts = _host_runtime_rows(connection, config)
        return JSONResponse({"compact": bool(compact), "hosts": hosts})

    @app.get("/api/folders/{prefix:path}")
    def api_folder(prefix: str) -> JSONResponse:
        context, status_code = _folder_content_payload(prefix.strip("/"))
        return JSONResponse(context, status_code=status_code)

    @app.get("/api/folders/{prefix:path}/status")
    def api_folder_status(prefix: str) -> JSONResponse:
        return JSONResponse(_folder_status_payload(prefix.strip("/")))

    def _folder_content_payload(normalized_prefix: str) -> tuple[dict[str, Any], int]:
        with open_db(config.paths.db_path) as connection:
            calibration_job = _load_job_state(connection, config, normalized_prefix)
            if calibration_job and calibration_job.get("status") in {"queued", "running"}:
                existing_scan_job = _load_scan_job_state(config, normalized_prefix)
                folder_scan_job = (
                    existing_scan_job if existing_scan_job and existing_scan_job.get("status") in {"queued", "running"} else None
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
            calibration = _load_calibration_state(config, normalized_prefix)
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

    def _folder_ai_tune_action(normalized_prefix: str, note: str, host_key: str) -> dict[str, Any]:
        trimmed_note = note.strip()
        host = _resolve_sample_host(config, host_key)

        with open_db(config.paths.db_path) as connection:
            existing_job = _load_job_state(connection, config, normalized_prefix)
            if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
                return {"ok": False, "message": "A calibration job is already active for this folder."}
            summary = inspect_prefix(connection, config, normalized_prefix)
            sample_item = _sample_item(connection, config, normalized_prefix)
            if sample_item is None:
                raise HTTPException(status_code=404, detail=f"No sample item found for {normalized_prefix}")
            calibration = _load_calibration_state(config, normalized_prefix)
            if calibration is None:
                base_policy = dict(sample_item["resolved_policy"] or {})
                seed_metadata = _maybe_seed_baseline_policy(
                    config=config,
                    prefix=normalized_prefix,
                    action="baseline",
                    user_note=trimmed_note,
                    base_policy=base_policy,
                    sample_item=sample_item,
                    existing_calibration=None,
                    connection=connection,
                )
                seeded_policy = seed_metadata["policy"] if seed_metadata else base_policy
                advice_payload = _seed_advice_payload(trimmed_note, seed_metadata)
                if advice_payload is not None:
                    _save_advice_state(config, normalized_prefix, advice_payload)
                job_payload = {
                    "job_id": uuid.uuid4().hex[:12],
                    "status": "queued",
                    "lane": "sample",
                    "mode": "sample",
                    "owner_pid": None,
                    "prefix": normalized_prefix,
                    "host": asdict(host),
                    "action": "baseline",
                    "notes": trimmed_note,
                    "policy": seeded_policy,
                    "sample_item": {
                        "rel_path": sample_item["rel_path"],
                        "source_path": sample_item["source_path"],
                        "source_size_bytes": sample_item["source_size_bytes"],
                    },
                    "created_at": _now_iso(),
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "updated_at": _now_iso(),
                }
                if seed_metadata:
                    job_payload.update(seed_metadata["job_fields"])
                _save_job_state(connection, config, normalized_prefix, job_payload)
                return {"ok": True, "message": "Queued the initial AI-guided sample.", "job": job_payload}
            if not trimmed_note:
                raise HTTPException(status_code=400, detail="Add a note so the tuner knows what to change before running another sample.")
            current_policy = dict((calibration.get("policy") if calibration else sample_item["resolved_policy"]) or {})
            metric_support = _metric_support()
            learning_context = retrieve_learning_context(
                connection,
                prefix=normalized_prefix,
                sample_item=sample_item,
                note=trimmed_note,
            )
            runtime_toolbelt = _build_tuning_runtime_toolbelt(
                sample_item=sample_item,
                current_policy=current_policy,
                calibration=calibration,
                metric_support=metric_support,
            )
            tuning_payload = {
                "folder": normalized_prefix,
                "operator_note": trimmed_note,
                "summary": summary,
                "sample_item": {
                    "rel_path": sample_item["rel_path"],
                    "source_path": sample_item["source_path"],
                    "source_size_bytes": sample_item["source_size_bytes"],
                    "video_codec": sample_item["video_codec"],
                    "duration_seconds": sample_item["duration_seconds"],
                    "audio_summary": sample_item["audio_summary"],
                    "subtitle_summary": sample_item["subtitle_summary"],
                },
                "policy": current_policy,
                "recent_calibration": calibration,
                "metric_support": metric_support,
                "runtime_toolbelt": runtime_toolbelt,
                "retrieved_memory": learning_context,
            }
        tuning = request_note_tuning(project_root=config.paths.project_root, payload=tuning_payload)
        tuned_policy, applied_fragment = apply_seed_policy(current_policy, tuning.proposed_policy or {})
        advice_payload = _tuning_advice_payload(tuning=tuning, note=trimmed_note, applied_fragment=applied_fragment)
        advice_payload["retrieved_memory"] = learning_context

        with open_db(config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix=normalized_prefix,
                note=trimmed_note,
                response={
                    **advice_payload,
                    "prompt_version": tuning.prompt_version,
                    "proposed_policy": tuning.proposed_policy,
                },
                applied_policy=applied_fragment,
                toolbelt=runtime_toolbelt,
                created_at=_now_iso(),
            )
            advice_payload["session_id"] = session_id
            learning_artifact = promote_learning_artifact(
                connection,
                config,
                session_id=session_id,
                prefix=normalized_prefix,
                note=trimmed_note,
                sample_item=sample_item,
                response=advice_payload,
                applied_policy=applied_fragment,
                created_at=_now_iso(),
            )
            if learning_artifact is not None:
                advice_payload["learning_artifact"] = learning_artifact
        _save_advice_state(config, normalized_prefix, advice_payload)

        if not tuning.ok or not applied_fragment:
            return {
                "ok": False,
                "message": "Saved the tuning advice, but no new sample was queued.",
                "advice": advice_payload,
            }

        job_payload = {
            "job_id": uuid.uuid4().hex[:12],
            "status": "queued",
            "lane": "sample",
            "mode": "sample",
            "owner_pid": None,
            "prefix": normalized_prefix,
            "host": asdict(host),
            "action": "ai_tune",
            "notes": trimmed_note,
            "policy": tuned_policy,
            "sample_item": {
                "rel_path": sample_item["rel_path"],
                "source_path": sample_item["source_path"],
                "source_size_bytes": sample_item["source_size_bytes"],
            },
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "updated_at": _now_iso(),
        }
        with open_db(config.paths.db_path) as connection:
            _save_job_state(connection, config, normalized_prefix, job_payload)
        return {"ok": True, "message": "Queued an AI-guided sample retry.", "job": job_payload, "advice": advice_payload}

    def _queue_folder_encode_action(normalized_prefix: str, notes: str, bypass_schedule: bool) -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            existing_job = _load_job_state(connection, config, normalized_prefix)
            if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
                return {"ok": False, "message": "A calibration job is already active for this folder."}
            calibration = _load_calibration_state(config, normalized_prefix)
            review_gate = _review_gate(calibration)
            if not review_gate["can_confirm_full"]:
                raise HTTPException(status_code=400, detail=str(review_gate["message"]))
            if calibration is None:
                raise HTTPException(status_code=400, detail="Run a sampled calibration first.")
            _upsert_override(config.paths.config_path.parent / "folder-defaults.toml", normalized_prefix, calibration["policy"])
            refreshed_config = load_config(config.paths.config_path)
            manifest, manifest_path = create_folder_manifest(
                connection,
                refreshed_config,
                prefix=normalized_prefix,
                limit=None,
                scan_first=False,
            )
            if not manifest["items"]:
                raise HTTPException(status_code=400, detail="No pending items were found to enqueue for this folder.")
            queue_job = {
                "job_id": uuid.uuid4().hex[:12],
                "prefix": normalized_prefix,
                "status": "queued",
                "manifest_path": str(manifest_path),
                "item_count": len(manifest["items"]),
                "saved_profile_path": str(config.paths.config_path.parent / "folder-defaults.toml"),
                "host": {},
                "notes": notes.strip(),
                "bypass_schedule": bypass_schedule,
                "process_pid": None,
                "error": None,
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "updated_at": _now_iso(),
            }
            save_encode_job(connection, queue_job)
        return {"ok": True, "message": "Queued the full folder encode.", "job": queue_job}

    def _save_profile_action(normalized_prefix: str) -> dict[str, Any]:
        calibration = _load_calibration_state(config, normalized_prefix)
        if not calibration:
            raise HTTPException(status_code=400, detail="No draft calibration found for this folder")
        if str(calibration.get("mode") or "sample") == "sample":
            calibration["accepted_at"] = _now_iso()
            calibration["accepted_draft_hash"] = str(calibration.get("draft_hash") or _calibration_draft_hash(calibration))
            calibration["accepted_sample_job_id"] = str(calibration.get("job_id") or "")
            _save_calibration_state(config, normalized_prefix, calibration)
        _upsert_override(config.paths.config_path.parent / "folder-defaults.toml", normalized_prefix, calibration["policy"])
        return {"ok": True, "message": "Saved the current draft as the folder profile."}

    @app.post("/api/folders/{prefix:path}/ai-tune")
    async def api_folder_ai_tune(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = _folder_ai_tune_action(prefix.strip("/"), str(body.get("note", "")), str(body.get("host_key", "")))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/queue-encode")
    async def api_folder_queue_encode(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = _queue_folder_encode_action(
            prefix.strip("/"),
            str(body.get("notes", "")),
            bool(body.get("bypass_schedule", False)),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/save-profile")
    def api_folder_save_profile(prefix: str) -> JSONResponse:
        return JSONResponse(_save_profile_action(prefix.strip("/")))

    def _pause_encode_queue_action() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            state = load_queue_state(connection)
            state.update({"is_paused": True, "updated_at": _now_iso()})
            save_queue_state(connection, state)
        return {"ok": True, "message": "Paused the encode queue."}

    def _resume_encode_queue_action() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            state = load_queue_state(connection)
            state.update({"is_paused": False, "stop_requested": False, "updated_at": _now_iso()})
            save_queue_state(connection, state)
        return {"ok": True, "message": "Resumed the encode queue."}

    def _stop_encode_queue_action() -> dict[str, Any]:
        with open_db(config.paths.db_path) as connection:
            state = load_queue_state(connection)
            state.update({"stop_requested": True, "is_paused": True, "updated_at": _now_iso()})
            save_queue_state(connection, state)
        ENCODE_QUEUE_PROCESS.cancel()
        return {"ok": True, "message": "Stopped and cleaned the encode queue."}

    @app.post("/api/encode-queue/pause")
    def api_pause_encode_queue() -> JSONResponse:
        return JSONResponse(_pause_encode_queue_action())

    @app.post("/api/encode-queue/resume")
    def api_resume_encode_queue() -> JSONResponse:
        return JSONResponse(_resume_encode_queue_action())

    @app.post("/api/encode-queue/stop")
    def api_stop_encode_queue() -> JSONResponse:
        return JSONResponse(_stop_encode_queue_action())

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

    @app.get("/", include_in_schema=False)
    def frontend_root() -> FileResponse:
        return FileResponse(_frontend_index_path())

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_catchall(path: str) -> FileResponse:
        requested = (frontend_build_dir / path).resolve()
        if requested.is_file() and requested.is_relative_to(frontend_build_dir.resolve()):
            return FileResponse(requested)
        return FileResponse(_frontend_index_path())

    return app


def main() -> None:
    _load_project_env_file()
    config = load_config(DEFAULT_CONFIG_PATH)
    host = _default_web_host()
    port = int(_preferred_env("MEDIAFORCE_WEB_PORT", "MEDIA_HARNESS_WEB_PORT") or "8777")
    reload_enabled = _default_web_reload_enabled()
    if reload_enabled:
        config_path = str(config.paths.config_path)
        os.environ.setdefault("MEDIAFORCE_CONFIG_PATH", config_path)
        os.environ.setdefault("MEDIA_HARNESS_CONFIG_PATH", config_path)
        uvicorn.run(
            "mediaforce.web.app:create_reloadable_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
            log_level="info",
        )
        return
    uvicorn.run(
        create_app(config.paths.config_path),
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


def create_reloadable_app() -> FastAPI:
    config_path = Path(
        _preferred_env("MEDIAFORCE_CONFIG_PATH", "MEDIA_HARNESS_CONFIG_PATH")
        or str(DEFAULT_CONFIG_PATH)
    ).expanduser()
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
    explicit_value = _preferred_env("MEDIAFORCE_WEB_RELOAD", "MEDIA_HARNESS_WEB_RELOAD")
    if explicit_value is not None:
        return explicit_value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _default_web_host() -> str:
    explicit_host = _preferred_env("MEDIAFORCE_WEB_HOST", "MEDIA_HARNESS_WEB_HOST")
    if explicit_host is not None:
        return explicit_host
    return "127.0.0.1"


def _preferred_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _settings_library_rows(source_root_map: dict[str, Path], *, min_rows: int = 3) -> list[dict[str, str]]:
    rows = [{"key": key, "path": str(path)} for key, path in source_root_map.items()]
    return _index_settings_library_rows(rows, min_rows=1)


def _settings_library_rows_for_config(config: HarnessConfig, *, min_rows: int = 3) -> list[dict[str, str]]:
    media = config.raw.get("media")
    source_roots = media.get("source_roots") if isinstance(media, dict) else None
    rows: list[dict[str, str]] = []
    if isinstance(source_roots, dict):
        for key, value in source_roots.items():
            key_text = str(key).strip()
            path_text = _stringify_pathlike(value)
            if not key_text and not path_text:
                continue
            rows.append({"key": key_text, "path": path_text})
    return _index_settings_library_rows(rows, min_rows=min_rows)


def _index_settings_library_rows(rows: list[dict[str, str]], *, min_rows: int = 1) -> list[dict[str, str]]:
    indexed = [{"index": str(index), "key": row.get("key", ""), "path": row.get("path", "")} for index, row in enumerate(rows)]
    while len(indexed) < min_rows:
        indexed.append({"index": str(len(indexed)), "key": "", "path": ""})
    return indexed


def _settings_remote_rows(remote_hosts: list[dict[str, Any]], *, min_rows: int = 3) -> list[dict[str, Any]]:
    rows = [
        {
            "label": str(host.get("label") or ""),
            "host": str(host.get("host") or ""),
            "repo_path": str(host.get("repo_path") or ""),
            "wake_mac": str(host.get("wake_mac") or host.get("wol_mac") or ""),
            "priority": str(host.get("priority") or "0"),
            "max_parallel_encodes": str(_host_max_parallel_encodes(host)),
            "schedule_profile": _host_schedule_profile_key(host),
            "capabilities": _normalize_host_capabilities(host.get("capabilities")),
        }
        for host in remote_hosts
    ]
    return _index_settings_remote_rows(rows, min_rows=1)


def _settings_remote_rows_for_config(config: HarnessConfig, *, min_rows: int = 3) -> list[dict[str, Any]]:
    remote_hosts = config.raw.get("remote_hosts")
    rows: list[dict[str, Any]] = []
    if isinstance(remote_hosts, list):
        for host in remote_hosts:
            if not isinstance(host, dict):
                continue
            rows.append(
                {
                    "label": str(host.get("label") or ""),
                    "host": str(host.get("host") or ""),
                    "repo_path": str(host.get("repo_path") or ""),
                    "wake_mac": str(host.get("wake_mac") or host.get("wol_mac") or ""),
                    "priority": str(host.get("priority") or "0"),
                    "max_parallel_encodes": str(_host_max_parallel_encodes(host)),
                    "schedule_profile": _host_schedule_profile_key(host),
                    "capabilities": _normalize_host_capabilities(host.get("capabilities")),
                }
            )
    return _index_settings_remote_rows(rows, min_rows=min_rows)


def _index_settings_remote_rows(rows: list[dict[str, Any]], *, min_rows: int = 1) -> list[dict[str, Any]]:
    indexed = [
        {
            "index": str(index),
            "label": row.get("label", ""),
            "host": row.get("host", ""),
            "repo_path": row.get("repo_path", ""),
            "wake_mac": row.get("wake_mac", ""),
            "priority": row.get("priority", "0"),
            "max_parallel_encodes": row.get("max_parallel_encodes", str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)),
            "schedule_profile": _canonical_schedule_profile_key(row.get("schedule_profile", DEFAULT_HOST_SCHEDULE_PROFILE)),
            "capabilities": _normalize_host_capabilities(row.get("capabilities", list(DEFAULT_HOST_CAPABILITIES))),
        }
        for index, row in enumerate(rows)
    ]
    while len(indexed) < min_rows:
        indexed.append(
            {
                "index": str(len(indexed)),
                "label": "",
                "host": "",
                "repo_path": "",
                "wake_mac": "",
                "priority": "0",
                "max_parallel_encodes": str(DEFAULT_HOST_MAX_PARALLEL_ENCODES),
                "schedule_profile": DEFAULT_HOST_SCHEDULE_PROFILE,
                "capabilities": list(DEFAULT_HOST_CAPABILITIES),
            }
        )
    return indexed


def _settings_schedule_profile_rows_for_config(config: HarnessConfig, *, min_rows: int = 1) -> list[dict[str, str]]:
    encode_queue = config.raw.get("encode_queue")
    raw_profiles = encode_queue.get("schedule_profiles") if isinstance(encode_queue, dict) else None
    rows: list[dict[str, str]] = []
    if isinstance(raw_profiles, list):
        for profile in raw_profiles:
            if not isinstance(profile, dict):
                continue
            key = _canonical_schedule_profile_key(str(profile.get("key") or profile.get("name") or ""))
            if not key or key == ALWAYS_SCHEDULE_PROFILE:
                continue
            normalized = _normalize_encode_queue_scheduler(profile)
            rows.append(
                {
                    "key": key,
                    "label": str(profile.get("label") or key.replace("_", " ").title()),
                    "start_hour": str(normalized["start_hour"]),
                    "end_hour": str(normalized["end_hour"]),
                }
            )
    return _index_schedule_profile_rows(rows, min_rows=min_rows)


def _index_schedule_profile_rows(rows: list[dict[str, str]], *, min_rows: int = 1) -> list[dict[str, str]]:
    indexed = [
        {
            "index": str(index),
            "key": row.get("key", ""),
            "label": row.get("label", ""),
            "start_hour": row.get("start_hour", str(DEFAULT_SCHEDULER_POLICY["start_hour"])),
            "end_hour": row.get("end_hour", str(DEFAULT_SCHEDULER_POLICY["end_hour"])),
        }
        for index, row in enumerate(rows)
    ]
    while len(indexed) < min_rows:
        indexed.append(
            {
                "index": str(len(indexed)),
                "key": "",
                "label": "",
                "start_hour": str(DEFAULT_SCHEDULER_POLICY["start_hour"]),
                "end_hour": str(DEFAULT_SCHEDULER_POLICY["end_hour"]),
            }
        )
    return indexed


def _normalize_host_capabilities(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [value.strip() for value in raw.split(",") if value.strip()]
    elif isinstance(raw, list):
        values = [str(value).strip() for value in raw if str(value).strip()]
    else:
        values = []
    normalized = sorted({value.lower().replace(" ", "_") for value in values})
    return normalized or list(DEFAULT_HOST_CAPABILITIES)


def _host_capability_enabled(row: dict[str, Any], capability_key: str) -> bool:
    return capability_key in _normalize_host_capabilities(row.get("capabilities"))


def _host_max_parallel_encodes(host: dict[str, Any]) -> int:
    try:
        return max(1, int(str(host.get("max_parallel_encodes") or DEFAULT_HOST_MAX_PARALLEL_ENCODES)))
    except (TypeError, ValueError):
        return DEFAULT_HOST_MAX_PARALLEL_ENCODES


def _normalize_schedule_profile_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")


def _canonical_schedule_profile_key(value: Any) -> str:
    key = _normalize_schedule_profile_key(str(value or ""))
    if key in {"", "always", "no_schedule", "none"}:
        return ALWAYS_SCHEDULE_PROFILE
    if key in {LEGACY_DEFAULT_SCHEDULE_PROFILE, "queue_default", LEGACY_QUEUE_WINDOW_SCHEDULE_PROFILE}:
        return ALWAYS_SCHEDULE_PROFILE
    return key


def _host_schedule_profile_key(host: dict[str, Any]) -> str:
    return _canonical_schedule_profile_key(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)


def _schedule_profile_options(
    *,
    schedule_profiles: list[dict[str, Any]],
) -> list[dict[str, str]]:
    options = [
        {
            "key": ALWAYS_SCHEDULE_PROFILE,
            "label": "Always",
            "summary": "Runs anytime.",
        },
    ]
    for row in schedule_profiles:
        key = _canonical_schedule_profile_key(row.get("key"))
        if key == ALWAYS_SCHEDULE_PROFILE:
            continue
        policy = _normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "timezone": "host_local",
                "start_hour": row.get("start_hour"),
                "end_hour": row.get("end_hour"),
            }
        )
        options.append(
            {
                "key": key,
                "label": str(row.get("label") or key.replace("_", " ").title()),
                "summary": str(policy["summary"]),
            }
        )
    return options


def _settings_transcode_root_value(config: HarnessConfig) -> str:
    media = config.raw.get("media")
    return _stringify_pathlike(media.get("staging_root") if isinstance(media, dict) else None)


def _settings_archive_root(transcode_root: str) -> str:
    cleaned_root = transcode_root.strip()
    if not cleaned_root:
        return ""
    return str(Path(cleaned_root).expanduser() / "_replaced")


def _stringify_pathlike(value: Any) -> str:
    if isinstance(value, Path):
        return str(value.expanduser())
    if isinstance(value, str):
        return str(Path(value).expanduser()) if value.strip() else ""
    return ""


def _safe_collect_host_statuses(config: HarnessConfig) -> list[HostStatus]:
    try:
        return collect_host_statuses(config)
    except Exception as exc:
        return [
            HostStatus(
                key="host-status-error",
                label="Host status unavailable",
                mode="ssh",
                priority=0,
                capabilities=[],
                available=False,
                message="Host checks could not load with the current runtime settings.",
                missing_paths=[],
                issues=["Review the runtime settings values and save the page again."],
                detail=str(exc),
            )
        ]


def _host_runtime_rows(connection: Any, config: HarnessConfig, *, now: datetime | None = None) -> list[dict[str, Any]]:
    statuses = _safe_collect_host_statuses(config)
    running_counts = _running_encode_counts_by_host(connection)
    profiles = _encode_queue_schedule_profiles(config)
    rows: list[dict[str, Any]] = []
    current_time = now or datetime.now(UTC)
    for status in statuses:
        host_config = _host_config_for_key(config, status.key)
        capabilities = {capability.lower() for capability in status.capabilities}
        max_parallel_encodes = _host_max_parallel_encodes(host_config)
        schedule_profile = _host_schedule_profile_key(host_config)
        policy = dict(profiles.get(schedule_profile) or profiles[DEFAULT_HOST_SCHEDULE_PROFILE])
        active_encode_count = running_counts.get(status.key, 0)
        schedule_open = _scheduler_allows_encode_run(policy, now=current_time, host_payload=asdict(status))
        encode_capable = "encode_queue" in capabilities
        queue_active = status.available and encode_capable and schedule_open and active_encode_count < max_parallel_encodes
        if not status.available:
            active_reason = status.message
        elif not encode_capable:
            active_reason = "encode queue capability disabled"
        elif not schedule_open:
            active_reason = ""
        elif active_encode_count >= max_parallel_encodes:
            active_reason = "parallel encode slots are full"
        else:
            active_reason = ""
        schedule_detail = str(policy["summary"])
        if schedule_profile == ALWAYS_SCHEDULE_PROFILE and schedule_detail == "runs anytime":
            schedule_detail = ""
        rows.append(
            {
                **asdict(status),
                "schedule_profile": schedule_profile,
                "schedule_profile_label": str(policy.get("label") or "Always"),
                "scheduler_summary": str(policy["summary"]),
                "schedule_detail": schedule_detail,
                "schedule_open": schedule_open,
                "max_parallel_encodes": max_parallel_encodes,
                "active_encode_count": active_encode_count,
                "queue_active": queue_active,
                "active_flag": "active" if queue_active else "idle",
                "active_reason": active_reason,
            }
        )
    return rows


def _running_encode_counts_by_host(connection: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    rows = connection.execute("SELECT host_json FROM encode_jobs WHERE status = 'running'").fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["host_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        host_key = str(payload.get("key") or payload.get("host") or "").strip()
        if not host_key:
            continue
        counts[host_key] = counts.get(host_key, 0) + 1
    return counts


def _host_config_for_key(config: HarnessConfig, host_key: str) -> dict[str, Any]:
    for host in config.remote_hosts:
        ssh_host = str(host.get("host") or "")
        label = str(host.get("label") or ssh_host or "remote")
        if host_key in {ssh_host, label}:
            return host
    return {}


def _default_sample_host_key(config: HarnessConfig) -> str:
    return _default_sample_host_key_from_statuses(_sample_calibration_host_statuses(config))


def _default_sample_host_key_from_statuses(statuses: list[HostStatus]) -> str:
    for status in statuses:
        if status.available and host_status_targets_current_machine(status):
            return status.key
    for status in statuses:
        if status.available:
            return status.key
    return ""


def _sample_calibration_host_statuses(config: HarnessConfig) -> list[HostStatus]:
    hosts: list[HostStatus] = []
    for status in collect_host_statuses(config):
        capabilities = {capability.lower() for capability in status.capabilities}
        if "sample_calibration" in capabilities:
            hosts.append(status)
    return hosts


def _sample_host_options(config: HarnessConfig) -> list[dict[str, Any]]:
    return _sample_host_options_from_statuses(_sample_calibration_host_statuses(config))


def _sample_host_options_from_statuses(statuses: list[HostStatus]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for status in statuses:
        options.append(
            {
                "key": status.key,
                "label": status.label,
                "detail": status.message if not status.available else ("This machine" if host_status_targets_current_machine(status) else "Remote host"),
                "available": status.available,
            }
        )
    return options


def _sample_host_help_text(sample_host_options: list[dict[str, Any]], selected_key: str) -> str:
    for option in sample_host_options:
        if str(option.get("key") or "") != selected_key:
            continue
        detail = str(option.get("detail") or "").strip()
        return detail or "Choose where sampled calibration should run."
    return "Choose where sampled calibration should run."


def _resolve_sample_host(config: HarnessConfig, host_key: str) -> HostStatus:
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
    # The shared Queue Window is retired; hosts now use Always or an explicit
    # custom schedule profile.
    return _normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})


def _settings_form_indexes(form_data: dict[str, str], prefix: str) -> list[int]:
    indexes: set[int] = set()
    for key in form_data:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix.isdigit():
            indexes.add(int(suffix))
    return sorted(indexes)


def _build_runtime_settings_payload(
    *,
    libraries: list[dict[str, str]],
    remote_hosts: list[dict[str, Any]],
    transcode_root: str,
    encode_queue_scheduler: dict[str, Any],
    schedule_profiles: list[dict[str, str]],
) -> dict[str, Any]:
    source_roots: dict[str, str] = {}
    for row in libraries:
        key_text = row.get("key", "").strip()
        path_text = row.get("path", "").strip()
        if not key_text and not path_text:
            continue
        normalized_key = _normalize_library_key(key_text)
        if not normalized_key or not path_text:
            raise ValueError("Each library row needs both a library name and a mounted path.")
        if normalized_key in source_roots:
            raise ValueError(f"Duplicate library name: {normalized_key}")
        source_roots[normalized_key] = str(Path(path_text).expanduser())
    if not source_roots:
        raise ValueError("Add at least one library before saving settings.")

    normalized_remotes: list[dict[str, Any]] = []
    for row in remote_hosts:
        label = row.get("label", "").strip()
        host = row.get("host", "").strip()
        repo_path = row.get("repo_path", "").strip()
        wake_mac = row.get("wake_mac", "").strip()
        if not label and not host and not repo_path and not wake_mac:
            continue
        if not host:
            raise ValueError("Each remote host row needs an SSH host value.")
        priority_text = row.get("priority", "0").strip() or "0"
        try:
            priority = int(priority_text)
        except ValueError as exc:
            raise ValueError(f"Host priority must be a whole number for {label or host}.") from exc
        max_parallel_text = row.get("max_parallel_encodes", str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)).strip()
        try:
            max_parallel_encodes = max(1, int(max_parallel_text or str(DEFAULT_HOST_MAX_PARALLEL_ENCODES)))
        except ValueError as exc:
            raise ValueError(f"Parallel encodes must be a whole number for {label or host}.") from exc
        schedule_profile = _canonical_schedule_profile_key(row.get("schedule_profile", DEFAULT_HOST_SCHEDULE_PROFILE))
        payload: dict[str, Any] = {"host": host}
        if label:
            payload["label"] = label
        if repo_path:
            payload["repo_path"] = repo_path
        if wake_mac:
            payload["wake_mac"] = wake_mac
        payload["priority"] = str(priority)
        payload["max_parallel_encodes"] = max_parallel_encodes
        payload["schedule_profile"] = schedule_profile
        payload["capabilities"] = _normalize_host_capabilities(row.get("capabilities"))
        normalized_remotes.append(payload)

    normalized_profiles: list[dict[str, Any]] = []
    seen_profile_keys: set[str] = {ALWAYS_SCHEDULE_PROFILE}
    for row in schedule_profiles:
        key_text = _canonical_schedule_profile_key(row.get("key", ""))
        label_text = row.get("label", "").strip()
        start_hour_text = row.get("start_hour", str(DEFAULT_SCHEDULER_POLICY["start_hour"])).strip()
        end_hour_text = row.get("end_hour", str(DEFAULT_SCHEDULER_POLICY["end_hour"])).strip()
        if not any((key_text, label_text, start_hour_text, end_hour_text)):
            continue
        if not key_text:
            raise ValueError("Each schedule profile needs a key.")
        if key_text in seen_profile_keys:
            raise ValueError(f"Duplicate schedule profile key: {key_text}")
        normalized = _normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "timezone": "host_local",
                "start_hour": start_hour_text,
                "end_hour": end_hour_text,
            }
        )
        normalized_profiles.append(
            {
                "key": key_text,
                "label": label_text or key_text.replace("_", " ").title(),
                "mode": normalized["mode"],
                "timezone": normalized["timezone"],
                "start_hour": normalized["start_hour"],
                "end_hour": normalized["end_hour"],
            }
        )
        seen_profile_keys.add(key_text)

    invalid_host_profiles = sorted(
        {
            str(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)
            for host in normalized_remotes
            if str(host.get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE) not in seen_profile_keys
        }
    )
    if invalid_host_profiles:
        raise ValueError(
            "Unknown schedule profile for host assignment: " + ", ".join(invalid_host_profiles)
        )

    staging_root = Path(transcode_root).expanduser()
    return {
        "media": {
            "source_roots": source_roots,
            "staging_root": str(staging_root),
            "archive_root": str(staging_root / "_replaced"),
        },
        "remote_hosts": normalized_remotes,
        "encode_queue": {
            "scheduler": _normalize_encode_queue_scheduler(encode_queue_scheduler),
            "schedule_profiles": normalized_profiles,
        },
    }


def _merge_runtime_settings_payload(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    if "encode_queue" in updates:
        merged.pop("heavy_queue", None)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
            continue
        merged[key] = value
    return merged


def _normalize_library_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")


def _list_folder_cards(connection: Any) -> list[FolderCard]:
    rows = connection.execute(
        "SELECT rel_path, source_path, size_bytes, status, video_codec, audio_summary_json FROM library_items WHERE status != 'missing' ORDER BY rel_path"
    ).fetchall()
    grouped: dict[str, FolderCard] = {}
    for row in rows:
        rel_path = str(row["rel_path"])
        group = _folder_group(rel_path)
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        card = grouped.get(prefix)
        if card is None:
            card = FolderCard(
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                item_count=0,
                pending_count=0,
                total_size_bytes=0,
                estimated_savings_bytes=0,
                average_age_days=0.0,
                sort_score=0.0,
                statuses={},
                video_codecs={},
            )
            grouped[prefix] = card
        card.item_count += 1
        size_bytes = int(row["size_bytes"])
        card.total_size_bytes += size_bytes
        age_days = _age_days(str(row["source_path"]))
        card.average_age_days += age_days
        status = str(row["status"] or "unknown")
        codec = str(row["video_codec"] or "unknown")
        if status in {"discovered", "planned"}:
            estimated_savings = _estimate_savings_bytes(
                size_bytes=size_bytes,
                video_codec=codec,
                audio_summary_json=str(row["audio_summary_json"] or "[]"),
            )
            age_multiplier = _age_multiplier(age_days)
            card.pending_count += 1
            card.estimated_savings_bytes += estimated_savings
            card.sort_score += (estimated_savings / (1024 ** 3)) * age_multiplier
        card.statuses[status] = card.statuses.get(status, 0) + 1
        card.video_codecs[codec] = card.video_codecs.get(codec, 0) + 1
    cards = list(grouped.values())
    for card in cards:
        card.average_age_days = round(card.average_age_days / max(card.item_count, 1), 1)
    cards = [
        card
        for card in cards
        if card.pending_count > 0 and card.estimated_savings_bytes >= MIN_RECOMMENDED_SAVINGS_BYTES
    ]
    return sorted(cards, key=lambda item: (item.sort_score, item.estimated_savings_bytes, item.total_size_bytes), reverse=True)


def _cached_folder_cards(config: HarnessConfig, connection: Any) -> list[FolderCard]:
    global FOLDER_CARD_CACHE_KEY, FOLDER_CARD_CACHE_VALUE
    try:
        db_mtime_ns = config.paths.db_path.stat().st_mtime_ns
    except OSError:
        db_mtime_ns = 0
    cache_key = (str(config.paths.db_path), db_mtime_ns)
    with FOLDER_CARD_CACHE_LOCK:
        if FOLDER_CARD_CACHE_KEY == cache_key:
            return list(FOLDER_CARD_CACHE_VALUE)
    cards = _list_folder_cards(connection)
    with FOLDER_CARD_CACHE_LOCK:
        FOLDER_CARD_CACHE_KEY = cache_key
        FOLDER_CARD_CACHE_VALUE = list(cards)
    return cards


def _sample_item(connection: Any, config: HarnessConfig, prefix: str) -> dict[str, Any] | None:
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
        result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-filters"], check=True, capture_output=True, text=True)
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


def _tuning_policy_focus(policy: dict[str, Any]) -> dict[str, Any]:
    video = dict((policy.get("video") or {}))
    audio = dict((policy.get("audio") or {}))
    return {
        "video": {
            key: video[key]
            for key in (
                "quality_metric",
                "target_vmaf",
                "min_target_vmaf",
                "target_xpsnr",
                "min_target_xpsnr",
                "max_encoded_percent",
                "default_grain",
            )
            if key in video
        },
        "audio": {
            key: audio[key]
            for key in ("surround_5_1_opus_bitrate",)
            if key in audio
        },
    }


def _build_tuning_runtime_toolbelt(
    *,
    sample_item: dict[str, Any],
    current_policy: dict[str, Any],
    calibration: dict[str, Any] | None,
    metric_support: dict[str, bool],
) -> dict[str, Any]:
    sample_plan_item = dict(sample_item)
    sample_plan_item["resolved_policy"] = current_policy
    try:
        item_plan = describe_item_plan(sample_plan_item)
        overhead = estimate_output_overhead_bytes(sample_plan_item)
    except Exception:
        item_plan = {}
        overhead = {}
    sample_result = dict((calibration or {}).get("sample_result") or {})
    return {
        "allowed_policy_keys": [
            "video.target_vmaf",
            "video.min_target_vmaf",
            "video.target_xpsnr",
            "video.min_target_xpsnr",
            "video.max_encoded_percent",
            "video.default_grain",
            "audio.surround_5_1_opus_bitrate",
        ],
        "metric_support": metric_support,
        "current_policy_focus": _tuning_policy_focus(current_policy),
        "item_plan": item_plan,
        "estimated_overhead_bytes": overhead,
        "recent_sample_result": {
            key: sample_result.get(key)
            for key in (
                "chosen_crf",
                "quality_metric",
                "quality_target",
                "quality_score",
                "predicted_total_size_bytes",
                "predicted_encode_percent",
            )
            if key in sample_result
        },
    }


def _normalize_encode_queue_scheduler(raw: dict[str, Any] | None) -> dict[str, Any]:
    default_mode = str(DEFAULT_SCHEDULER_POLICY["mode"])
    default_start_hour = int(str(DEFAULT_SCHEDULER_POLICY["start_hour"]))
    default_end_hour = int(str(DEFAULT_SCHEDULER_POLICY["end_hour"]))
    default_timezone = str(DEFAULT_SCHEDULER_POLICY["timezone"])
    source = dict(DEFAULT_SCHEDULER_POLICY)
    if raw:
        source.update(raw)
    mode = str(source.get("mode") or default_mode).strip().lower()
    if mode not in {"anytime", "night"}:
        mode = default_mode
    timezone_name = str(source.get("timezone") or default_timezone).strip() or default_timezone
    if timezone_name == "local":
        timezone_name = "host_local"
    scheduler = {
        "mode": mode,
        "start_hour": _clamp_hour(source.get("start_hour"), default_start_hour),
        "end_hour": _clamp_hour(source.get("end_hour"), default_end_hour),
        "timezone": timezone_name,
    }
    scheduler["summary"] = _encode_queue_scheduler_summary(scheduler)
    return scheduler


def _encode_queue_scheduler_policy(config: HarnessConfig) -> dict[str, Any]:
    encode_queue = config.raw.get("encode_queue")
    raw = encode_queue.get("scheduler") if isinstance(encode_queue, dict) else None
    if not isinstance(raw, dict):
        legacy_queue = config.raw.get("heavy_queue")
        raw = legacy_queue.get("scheduler") if isinstance(legacy_queue, dict) else None
    return _normalize_encode_queue_scheduler(raw if isinstance(raw, dict) else None)


def _encode_queue_schedule_profiles(config: HarnessConfig) -> dict[str, dict[str, Any]]:
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


def _schedule_profile_policy_for_host(config: HarnessConfig, host_payload: dict[str, Any] | None) -> dict[str, Any]:
    profiles = _encode_queue_schedule_profiles(config)
    profile_key = _canonical_schedule_profile_key((host_payload or {}).get("schedule_profile") or DEFAULT_HOST_SCHEDULE_PROFILE)
    return dict(profiles.get(profile_key) or profiles[DEFAULT_HOST_SCHEDULE_PROFILE])


def _clamp_hour(value: Any, default: int) -> int:
    try:
        return max(0, min(23, int(str(value))))
    except (TypeError, ValueError):
        return default


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
    timezone_name = str((host_payload or {}).get("schedule_timezone") or (host_payload or {}).get("timezone") or "").strip()
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
    start_hour = int(policy.get("start_hour") or int(str(DEFAULT_SCHEDULER_POLICY["start_hour"])))
    end_hour = int(policy.get("end_hour") or int(str(DEFAULT_SCHEDULER_POLICY["end_hour"])))
    if start_hour == end_hour:
        return True
    current_hour = local_now.hour
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


def _decorate_encode_job_for_scheduler(config: HarnessConfig, job: dict[str, Any] | None) -> dict[str, Any] | None:
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
        and not _scheduler_allows_encode_run(policy, bypass_schedule=bypass_schedule, host_payload=dict(decorated.get("host") or {}))
    )
    decorated["schedule_waiting"] = schedule_waiting
    decorated["scheduler_summary"] = str(policy["summary"])
    decorated["attempt_summary"] = f"attempt {attempt_count} of {ENCODE_JOB_MAX_ATTEMPTS}" if attempt_count else "not started yet"
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
    return decorated


def _decorate_encode_queue_for_scheduler(config: HarnessConfig, encode_queue: dict[str, Any]) -> dict[str, Any]:
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
    return decorated


def _state_web_dir(config: HarnessConfig) -> Path:
    state_dir = config.paths.web_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _calibration_file(config: HarnessConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.json"


def _advice_file(config: HarnessConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.advice.json"


def _job_file(config: HarnessConfig, prefix: str) -> Path:
    return _state_web_dir(config) / f"{_slug(prefix)}.job.json"


def _scan_job_file(config: HarnessConfig, prefix: str | None) -> Path:
    name = "full-catalog" if prefix is None else f"prefix-{prefix}"
    return _state_web_dir(config) / f"scan-{_slug(name)}.job.json"


def _calibration_draft_hash(payload: dict[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in CALIBRATION_REVIEW_FIELDS
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
    can_confirm_full = bool(accepted_at and accepted_hash == current_hash and accepted_job_id == current_job_id)
    if can_confirm_full:
        return {
            "can_confirm_full": True,
            "message": f"Reviewed sample draft saved at {accepted_at}. Full confirmation is unlocked.",
            "status": "accepted",
            "accepted_at": accepted_at,
        }

    return {
        "can_confirm_full": False,
        "message": "Save this sampled draft to the folder profile before queueing the folder encode.",
        "status": "needs_save",
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

    attention_count = int(encode_queue.get("needs_attention_count") or 0)
    if attention_count:
        parts.append(f"{attention_count} need attention")

    if encode_job and encode_job.get("scheduler_status_copy") and status in {"queued", "retry_backoff", "running", "needs_attention"}:
        parts.append(str(encode_job["scheduler_status_copy"]))

    if encode_queue_state.get("is_paused"):
        parts.append("queue paused")

    return " · ".join(parts)


def _load_calibration_state(config: HarnessConfig, prefix: str) -> dict[str, Any] | None:
    path = _calibration_file(config, prefix)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    compare_clips = []
    compare_clips_purged = False
    for clip in payload.get("compare_clips") or []:
        review_file = _review_file_from_url(config, str(clip.get("path") or ""))
        if review_file is None or review_file.exists():
            compare_clips.append(clip)
            continue
        compare_clips_purged = True
    payload["compare_clips"] = compare_clips
    payload["compare_clips_purged"] = compare_clips_purged
    payload.setdefault("mode", "full" if payload.get("encode_result") else "sample")
    advice_path = _advice_file(config, prefix)
    if advice_path.exists():
        payload["advice"] = json.loads(advice_path.read_text())
    payload["draft_hash"] = _calibration_draft_hash(payload)
    return payload


def _save_calibration_state(config: HarnessConfig, prefix: str, payload: dict[str, Any]) -> None:
    stored_payload = dict(payload)
    stored_payload["draft_hash"] = _calibration_draft_hash(stored_payload)
    _calibration_file(config, prefix).write_text(json.dumps(stored_payload, indent=2) + "\n")


def _save_advice_state(config: HarnessConfig, prefix: str, advice: AdvisorResponse | dict[str, Any]) -> None:
    if isinstance(advice, AdvisorResponse):
        payload = {"ok": advice.ok, "summary": advice.summary, "raw": advice.raw}
    else:
        payload = dict(advice)
    _advice_file(config, prefix).write_text(json.dumps(payload, indent=2) + "\n")


def _load_job_state(connection: Any, config: HarnessConfig, prefix: str) -> dict[str, Any] | None:
    payload = load_latest_job(connection, prefix)
    if payload is None:
        return None
    status = str(payload.get("status") or "")
    if status == "running" and not _calibration_job_belongs_to_current_process(payload):
        payload = _expire_calibration_job(connection, config, prefix, payload)
        status = str(payload.get("status") or "")
    if status == "queued":
        position = queue_position(connection, str(payload["job_id"]))
        if position is not None:
            payload["queue_position"] = position[0]
            payload["queue_depth"] = position[1]
    if status in {"failed", "completed"}:
        finished_at = _parse_iso(payload.get("finished_at") or payload.get("started_at") or payload.get("created_at"))
        if finished_at and datetime.now(tz=UTC) - finished_at > CALIBRATION_JOB_NOTICE_AFTER:
            return None
    return payload


def _save_job_state(connection: Any, config: HarnessConfig, prefix: str, payload: dict[str, Any]) -> None:
    save_job(connection, {**payload, "prefix": prefix, "updated_at": _now_iso()})


def _calibration_job_belongs_to_current_process(job: dict[str, Any]) -> bool:
    return int(job.get("owner_pid") or -1) == os.getpid()


def _expire_calibration_job(connection: Any, config: HarnessConfig, prefix: str, job: dict[str, Any]) -> dict[str, Any]:
    expired = {
        **job,
        "status": "failed",
        "finished_at": _now_iso(),
        "error": "Calibration was interrupted by a web process restart.",
    }
    _save_job_state(connection, config, prefix, expired)
    return expired


def _load_scan_job_state(config: HarnessConfig, prefix: str | None) -> dict[str, Any] | None:
    path = _scan_job_file(config, prefix)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_scan_job_state(config: HarnessConfig, prefix: str | None, payload: dict[str, Any]) -> None:
    _scan_job_file(config, prefix).write_text(json.dumps(payload, indent=2) + "\n")


def _maybe_schedule_scan(connection: Any, config: HarnessConfig, prefix: str | None) -> dict[str, Any] | None:
    active_scan = _active_scan_from_db(connection, prefix)
    if active_scan is not None:
        return active_scan
    if prefix is not None:
        full_job = _load_scan_job_state(config, None)
        if full_job and full_job.get("status") in {"queued", "running"} and not _scan_job_belongs_to_current_process(full_job):
            full_job = _expire_scan_job(config, None, full_job)
        if full_job and full_job.get("status") in {"queued", "running"}:
            return full_job
    job = _load_scan_job_state(config, prefix)
    if job and job.get("status") in {"queued", "running"} and not _scan_job_belongs_to_current_process(job):
        job = _expire_scan_job(config, prefix, job)
    if job and job.get("status") in {"queued", "running"}:
        return job
    if not _scan_is_stale(connection, prefix):
        return job
    if job and job.get("status") == "failed":
        finished_at = _parse_iso(job.get("finished_at") or job.get("started_at"))
        if finished_at and datetime.now(tz=UTC) - finished_at < SCAN_RETRY_COOLDOWN:
            return job

    job_payload = {
        "job_id": uuid.uuid4().hex[:12],
        "status": "queued",
        "scope": "full" if prefix is None else "prefix",
        "prefix": prefix,
        "owner_pid": os.getpid(),
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "stats": None,
    }
    _save_scan_job_state(config, prefix, job_payload)
    thread = threading.Thread(
        target=_run_scan_job,
        kwargs={
            "config_path": config.paths.config_path,
            "prefix": prefix,
            "job_id": str(job_payload["job_id"]),
        },
        daemon=True,
    )
    thread.start()
    return job_payload


def _scan_is_stale(connection: Any, prefix: str | None) -> bool:
    if prefix is None:
        item_count = int(connection.execute("SELECT COUNT(*) FROM library_items").fetchone()[0])
        if item_count == 0:
            return True
        latest = _latest_scan_completed_at(connection, prefix=None)
        if latest is None:
            return True
        return datetime.now(tz=UTC) - latest > FULL_SCAN_STALE_AFTER

    item_count = int(
        connection.execute("SELECT COUNT(*) FROM library_items WHERE rel_path LIKE ?", (f"{prefix}%",)).fetchone()[0]
    )
    if item_count == 0:
        return True
    latest = _latest_scan_completed_at(connection, prefix=prefix)
    if latest is None:
        return True
    return datetime.now(tz=UTC) - latest > PREFIX_SCAN_STALE_AFTER


def _latest_scan_completed_at(connection: Any, prefix: str | None) -> datetime | None:
    rows = connection.execute(
        "SELECT completed_at, started_at, scope, prefixes_json FROM scan_runs ORDER BY started_at DESC LIMIT 250"
    ).fetchall()
    for row in rows:
        scope = str(row["scope"] or "unknown")
        completed = _parse_iso(row["completed_at"] or row["started_at"])
        if completed is None:
            continue
        if prefix is None:
            if scope == "full":
                return completed
            continue

        if scope == "full":
            return completed
        if scope != "prefix":
            continue
        try:
            prefixes = json.loads(row["prefixes_json"] or "[]")
        except json.JSONDecodeError:
            prefixes = []
        for candidate in prefixes:
            normalized = str(candidate).strip("/")
            if normalized and prefix.startswith(normalized):
                return completed
    return None


def _active_scan_from_db(connection: Any, prefix: str | None) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT scan_id, started_at, scope, prefixes_json FROM scan_runs WHERE completed_at IS NULL ORDER BY started_at DESC LIMIT 25"
    ).fetchall()
    for row in rows:
        scope = str(row["scope"] or "unknown")
        if prefix is None:
            if scope in {"full", "unknown"}:
                return {
                    "job_id": str(row["scan_id"]),
                    "status": "running",
                    "scope": scope,
                    "prefix": None,
                    "created_at": row["started_at"],
                    "started_at": row["started_at"],
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
            continue

        if scope in {"full", "unknown"}:
            return {
                "job_id": str(row["scan_id"]),
                "status": "running",
                "scope": scope,
                "prefix": prefix,
                "created_at": row["started_at"],
                "started_at": row["started_at"],
                "finished_at": None,
                "error": None,
                "stats": None,
            }
        try:
            prefixes = json.loads(row["prefixes_json"] or "[]")
        except json.JSONDecodeError:
            prefixes = []
        for candidate in prefixes:
            normalized = str(candidate).strip("/")
            if normalized and prefix.startswith(normalized):
                return {
                    "job_id": str(row["scan_id"]),
                    "status": "running",
                    "scope": scope,
                    "prefix": normalized,
                    "created_at": row["started_at"],
                    "started_at": row["started_at"],
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
    return None


def _parse_iso(value: Any) -> datetime | None:
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
    return int(job.get("owner_pid") or -1) == os.getpid()


def _expire_scan_job(config: HarnessConfig, prefix: str | None, job: dict[str, Any]) -> dict[str, Any]:
    expired = {
        **job,
        "status": "failed",
        "finished_at": _now_iso(),
        "error": "Background scan was interrupted by a web process restart.",
    }
    _save_scan_job_state(config, prefix, expired)
    return expired


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


def _review_url(config: HarnessConfig, output_path: Path) -> str:
    relative = output_path.relative_to(config.paths.review_dir)
    return f"/review-media/{relative.as_posix()}"


def _review_file_from_url(config: HarnessConfig, value: str) -> Path | None:
    prefix = "/review-media/"
    if not value.startswith(prefix):
        return None
    candidate = (config.paths.review_dir / value.removeprefix(prefix)).resolve()
    review_root = config.paths.review_dir.resolve()
    if not candidate.is_relative_to(review_root):
        return None
    return candidate


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
    for section in ("video", "audio", "planning"):
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


def _recover_calibration_jobs(connection: Any, config: HarnessConfig) -> None:
    running_rows = connection.execute(
        "SELECT job_id FROM calibration_jobs WHERE status = 'running' AND (owner_pid IS NULL OR owner_pid != ?)",
        (os.getpid(),),
    ).fetchall()
    for row in running_rows:
        payload = load_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        _expire_calibration_job(connection, config, str(payload["prefix"]), payload)


def _recover_encode_queue(connection: Any, config: HarnessConfig) -> None:
    _reconcile_encode_jobs(connection, config, restart_recovery=True)


def _reconcile_encode_jobs(connection: Any, config: HarnessConfig, *, restart_recovery: bool = False) -> None:
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
        "SELECT job_id FROM encode_jobs WHERE status = 'retry_backoff' ORDER BY created_at ASC, rowid ASC"
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
    connection: Any,
    config: HarnessConfig,
    job: dict[str, Any],
    *,
    failure_kind: str,
    error_message: str,
) -> None:
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


def _cleanup_encode_retry_artifacts(connection: Any, *, manifest_path: Path) -> None:
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
            SET status = CASE WHEN status = 'promoted' THEN status ELSE 'planned' END,
                updated_at = ?
            WHERE id = ? AND status != 'promoted'
            """,
            (now_iso, library_item_id),
        )


def _select_encode_host(connection: Any, config: HarnessConfig, job: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    host_rows = sorted(_host_runtime_rows(connection, config), key=lambda status: (-int(status["priority"]), str(status["label"])))
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
        if bool(host.get("available"))
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
    ]
    if not encode_capable_hosts:
        return None, "waiting for an available encode host"

    cooldown_until = _parse_iso(job.get("host_cooldown_until"))
    last_host = dict(job.get("last_host") or {})
    blocked_keys = {
        str(last_host.get("key") or ""),
        str(last_host.get("label") or ""),
        str(last_host.get("host") or ""),
    }
    if cooldown_until is not None and cooldown_until > now and any(blocked_keys):
        eligible = [
            host
            for host in active_hosts
            if str(host.get("key") or "") not in blocked_keys and str(host.get("label") or "") not in blocked_keys
        ]
        if eligible:
            return dict(eligible[0]), None
        host_name = str(last_host.get("label") or last_host.get("key") or "the last host")
        return None, f"waiting for host cooldown to expire on {host_name}"
    if active_hosts:
        return dict(active_hosts[0]), None
    if any(int(host.get("active_encode_count") or 0) >= int(host.get("max_parallel_encodes") or 1) for host in encode_capable_hosts):
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


def _dispatch_calibration_job(config: HarnessConfig, job_payload: dict[str, Any]) -> None:
    job_id = str(job_payload["job_id"])
    with CALIBRATION_SUBMISSIONS_LOCK:
        if job_id in CALIBRATION_SUBMISSIONS:
            return
        CALIBRATION_SUBMISSIONS.add(job_id)
    lane = str(job_payload.get("lane") or job_payload.get("mode") or "sample")
    task = partial(
        _run_calibration_job,
        config_path=config.paths.config_path,
        prefix=str(job_payload["prefix"]),
        action=str(job_payload["action"]),
        host_data=dict(job_payload.get("host") or {}),
        notes=str(job_payload.get("notes") or ""),
        policy=dict(job_payload.get("policy") or {}),
        job_id=job_id,
        seed_metadata=_job_seed_metadata(job_payload),
    )
    future = CALIBRATION_EXECUTORS[lane].submit(task)
    future.add_done_callback(_submission_cleanup_callback(job_id))


def _start_calibration_queue_worker(config: HarnessConfig) -> None:
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
    while True:
        try:
            _process_calibration_queue_once(config_path=config_path)
        except Exception:
            pass
        threading.Event().wait(CALIBRATION_QUEUE_POLL_SECONDS)


def _process_calibration_queue_once(*, config_path: Path) -> None:
    config = load_config(config_path)
    capacities = {
        "sample": SAMPLE_CALIBRATION_CONCURRENCY,
        "full": FULL_CALIBRATION_CONCURRENCY,
    }
    with open_db(config.paths.db_path) as connection:
        active_rows = connection.execute(
            """
            SELECT job_id, lane, prefix, status
            FROM calibration_jobs
            WHERE status IN ('running', 'pending_review')
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
        running_by_lane = {lane: 0 for lane in capacities}
        active_prefixes: set[str] = set()
        for row in active_rows:
            lane = str(row["lane"])
            status = str(row["status"])
            prefix = str(row["prefix"])
            active_prefixes.add(prefix)
            if status == "running" and lane in running_by_lane:
                running_by_lane[lane] += 1

        for lane, capacity in capacities.items():
            while running_by_lane[lane] < capacity:
                started_at = _now_iso()
                payload = claim_next_queued_calibration_job(
                    connection,
                    lane=lane,
                    owner_pid=os.getpid(),
                    started_at=started_at,
                    excluded_prefixes=tuple(sorted(active_prefixes)),
                )
                if payload is None:
                    break
                connection.commit()
                try:
                    _dispatch_calibration_job(config, payload)
                except Exception as exc:
                    _mark_calibration_submission_complete(str(payload["job_id"]))
                    _save_job_state(
                        connection,
                        config,
                        str(payload["prefix"]),
                        {
                            **payload,
                            "status": "failed",
                            "owner_pid": os.getpid(),
                            "started_at": payload.get("started_at") or started_at,
                            "finished_at": _now_iso(),
                            "error": str(exc),
                        },
                    )
                    connection.commit()
                    continue
                active_prefixes.add(str(payload["prefix"]))
                running_by_lane[lane] += 1


def _mark_calibration_submission_complete(job_id: str) -> None:
    with CALIBRATION_SUBMISSIONS_LOCK:
        CALIBRATION_SUBMISSIONS.discard(job_id)


def _submission_cleanup_callback(job_id: str) -> Any:
    def _callback(_future: Future[Any]) -> None:
        _mark_calibration_submission_complete(job_id)

    return _callback


def _maybe_seed_baseline_policy(
    *,
    config: HarnessConfig,
    prefix: str,
    action: str,
    user_note: str,
    base_policy: dict[str, Any],
    sample_item: dict[str, Any],
    existing_calibration: dict[str, Any] | None,
    connection: Any,
) -> dict[str, Any] | None:
    if action != "baseline" or existing_calibration is not None:
        return None
    summary = inspect_prefix(connection, config, prefix)
    payload = {
        "folder": prefix,
        "goal": "Prefer slightly smaller files when the visual difference is hard to spot.",
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
            "video_codec": sample_item["video_codec"],
            "duration_seconds": sample_item["duration_seconds"],
            "audio_summary": sample_item["audio_summary"],
            "subtitle_summary": sample_item["subtitle_summary"],
        },
        "summary": {
            "item_count": summary.get("item_count"),
            "total_size_bytes": summary.get("total_size_bytes"),
            "video_codecs": summary.get("video_codecs"),
            "audio_codecs": summary.get("audio_codecs"),
            "seasons": summary.get("seasons"),
        },
        "base_policy": base_policy,
        "operator_note": user_note or None,
        "metric_support": _metric_support(),
    }
    seed_response = request_seed_policy(project_root=config.paths.project_root, payload=payload)
    if not seed_response.ok or not seed_response.proposed_policy:
        return {
            "policy": base_policy,
            "job_fields": {
                "seed_source": "default",
                "seed_summary": seed_response.summary,
                "seed_prompt_version": seed_response.prompt_version,
                "seed_raw_response": seed_response.raw,
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
            },
        }
    seeded_policy, applied_fragment = apply_seed_policy(base_policy, seed_response.proposed_policy)
    seed_source = "ai" if applied_fragment else "default"
    return {
        "policy": seeded_policy,
        "job_fields": {
            "seed_source": seed_source,
            "seed_summary": seed_response.summary,
            "seed_prompt_version": seed_response.prompt_version,
            "seed_raw_response": seed_response.raw,
            "seed_proposed_policy": seed_response.proposed_policy,
            "seed_applied_policy": applied_fragment or None,
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
        "applied_policy": job_fields.get("seed_applied_policy"),
    }


def _job_seed_metadata(job_payload: dict[str, Any]) -> dict[str, Any] | None:
    if not any(job_payload.get(key) is not None for key in ("seed_source", "seed_prompt_version", "seed_raw_response", "seed_proposed_policy", "seed_applied_policy")):
        return None
    return {
        "source": job_payload.get("seed_source"),
        "summary": job_payload.get("seed_summary"),
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


def _start_encode_queue_worker(config: HarnessConfig) -> None:
    global ENCODE_QUEUE_WORKER_STARTED
    with ENCODE_QUEUE_WORKER_LOCK:
        if ENCODE_QUEUE_WORKER_STARTED:
            return
        thread = threading.Thread(target=_encode_queue_worker_loop, kwargs={"config_path": config.paths.config_path}, daemon=True)
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
                "updated_at": now_iso,
            }
        )
        save_encode_job(connection, next_job)

    _run_encode_job(config_path=config_path, job_id=str(next_job["job_id"]))


def _load_next_runnable_encode_job(connection: Any, config: HarnessConfig) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT job_id FROM encode_jobs WHERE status = 'queued' ORDER BY created_at ASC, rowid ASC"
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
    try:
        ENCODE_QUEUE_PROCESS.throw_if_cancelled()
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
            )
        final_status = "completed"
    except ProcessCancelledError:
        final_status = "stopped"
        error = "Encode queue job was stopped and cleaned up."
    except Exception as exc:
        failure_kind = _classify_encode_failure(exc, job)
        error = str(exc)
    finally:
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


def _run_periodic_cleanup(config: HarnessConfig, cleanup_lock: threading.Lock) -> None:
    if not cleanup_lock.acquire(blocking=False):
        return
    try:
        purge_transient_artifacts(config)
    finally:
        cleanup_lock.release()


def _snapshot_staged_artifact(connection: Any, library_item_id: int) -> dict[str, Any] | None:
    columns = ", ".join(CALIBRATION_STAGED_ARTIFACT_COLUMNS)
    row = connection.execute(
        f"SELECT {columns} FROM staged_artifacts WHERE library_item_id = ?",
        (library_item_id,),
    ).fetchone()
    if row is None:
        return None
    return {column: row[column] for column in CALIBRATION_STAGED_ARTIFACT_COLUMNS}


def _restore_staged_artifact(connection: Any, library_item_id: int, snapshot: dict[str, Any] | None) -> None:
    if snapshot is None:
        connection.execute("DELETE FROM staged_artifacts WHERE library_item_id = ?", (library_item_id,))
        return

    columns = ", ".join(CALIBRATION_STAGED_ARTIFACT_COLUMNS)
    placeholders = ", ".join("?" for _ in CALIBRATION_STAGED_ARTIFACT_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in CALIBRATION_STAGED_ARTIFACT_COLUMNS
        if column != "library_item_id"
    )
    values = tuple(snapshot[column] for column in CALIBRATION_STAGED_ARTIFACT_COLUMNS)
    connection.execute(
        f"""
        INSERT INTO staged_artifacts ({columns})
        VALUES ({placeholders})
        ON CONFLICT(library_item_id) DO UPDATE SET {updates}
        """,
        values,
    )


def _remove_path(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _run_scan_job(*, config_path: Path, prefix: str | None, job_id: str) -> None:
    config = load_config(config_path)
    purge_transient_artifacts(config, force=True)
    job = _load_scan_job_state(config, prefix) or {}
    job.update({"status": "running", "started_at": _now_iso(), "finished_at": None, "error": None})
    _save_scan_job_state(config, prefix, job)

    try:
        with open_db(config.paths.db_path) as connection:
            stats = scan_library(connection, config, prefixes=[prefix] if prefix else None, limit=None)
        _save_scan_job_state(
            config,
            prefix,
            {
                **job,
                "job_id": job_id,
                "status": "completed",
                "finished_at": _now_iso(),
                "error": None,
                "stats": asdict(stats),
            },
        )
    except Exception as exc:
        _save_scan_job_state(
            config,
            prefix,
            {
                **job,
                "job_id": job_id,
                "status": "failed",
                "finished_at": _now_iso(),
                "error": str(exc),
                "stats": None,
            },
        )
    finally:
        purge_transient_artifacts(config, force=True)


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
) -> None:
    config = load_config(config_path)
    purge_transient_artifacts(config, force=True)
    with open_db(config.paths.db_path) as connection:
        job = _load_job_state(connection, config, prefix) or {}
        started_at = str(job.get("started_at") or _now_iso())
        job.update({"status": "running", "started_at": started_at, "finished_at": None, "error": None, "updated_at": _now_iso()})
        _save_job_state(connection, config, prefix, job)

    calibration_dir: Path | None = None
    manifest_path: Path | None = None
    library_item_id: int | None = None
    staged_artifact_snapshot: dict[str, Any] | None = None

    try:
        with open_db(config.paths.db_path) as connection:
            sample_item = _sample_item(connection, config, prefix)
            if sample_item is None:
                raise RuntimeError(f"No sample item found for {prefix}")
            sample_item = dict(sample_item)
            sample_item["resolved_policy"] = policy
            library_item_id = int(sample_item["library_item_id"])
            staged_artifact_snapshot = _snapshot_staged_artifact(connection, library_item_id)

            calibration_run_id = uuid.uuid4().hex[:12]
            if _calibration_mode_for_action(action) == "full":
                calibration_payload, manifest_path, calibration_dir = _run_full_calibration(
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
                )
            else:
                calibration_payload, calibration_dir = _run_sampled_calibration(
                    config=config,
                    prefix=prefix,
                    action=action,
                    host_data=host_data,
                    notes=notes,
                    policy=policy,
                    seed_metadata=seed_metadata,
                    sample_item=sample_item,
                    calibration_run_id=calibration_run_id,
                )

        calibration_payload["job_id"] = job_id
        _save_calibration_state(config, prefix, calibration_payload)
        with open_db(config.paths.db_path) as connection:
            _save_job_state(
                connection,
                config,
                prefix,
                {
                    **job,
                    "job_id": job_id,
                    "status": "completed",
                    "finished_at": _now_iso(),
                    "error": None,
                    "result": _summarize_calibration_result(calibration_payload),
                },
            )
    except Exception as exc:
        with open_db(config.paths.db_path) as connection:
            _save_job_state(
                connection,
                config,
                prefix,
                {
                    **job,
                    "job_id": job_id,
                    "status": "failed",
                    "finished_at": _now_iso(),
                    "error": str(exc),
                },
            )
    finally:
        if library_item_id is not None:
            with open_db(config.paths.db_path) as connection:
                _restore_staged_artifact(connection, library_item_id, staged_artifact_snapshot)
        _remove_path(manifest_path)
        _remove_path(calibration_dir)
        purge_transient_artifacts(config, force=True)


def _run_sampled_calibration(
    *,
    config: HarnessConfig,
    prefix: str,
    action: str,
    host_data: dict[str, Any],
    notes: str,
    policy: dict[str, Any],
    seed_metadata: dict[str, Any] | None,
    sample_item: dict[str, Any],
    calibration_run_id: str,
) -> tuple[dict[str, Any], Path]:
    source_path = Path(sample_item["source_path"])
    video_policy = dict(policy["video"])
    quality_result = search_quality_for_source(source_path, video_policy, host=host_data)
    sample_result = run_sample_encode(
        source_path,
        preferred_metric=str(video_policy.get("quality_metric", "auto")),
        crf=quality_result.crf,
        preset=int(video_policy["preset"]),
        pixel_format=str(video_policy["pixel_format"]),
        sample_every=str(video_policy["sample_every"]),
        sample_duration=str(video_policy["sample_duration"]),
        svt_params=build_svt_params(video_policy),
        host=host_data,
    )

    timestamps = recommend_review_timestamps(
        source_path,
        float(sample_item.get("duration_seconds") or 0.0),
        8.0,
    )
    output_dir = config.paths.review_dir / calibration_run_id / "item-00"
    preview_clips = encode_preview_clips(
        source_path=source_path,
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=8.0,
        encoder=str(video_policy["encoder"]),
        pixel_format=str(video_policy["pixel_format"]),
        preset=int(video_policy["preset"]),
        crf=quality_result.crf,
        svt_params=build_svt_params(video_policy),
        host=host_data,
    )
    compare_clips = generate_compare_clips_from_previews(
        source_path=source_path,
        previews=preview_clips,
        output_dir=output_dir,
    )
    overhead = estimate_output_overhead_bytes(sample_item)
    estimated_total_size_bytes = sample_result.predicted_encode_size_bytes + overhead["total_bytes"]

    payload = {
        "mode": "sample",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_path": sample_item["source_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
        },
        "sample_result": {
            "chosen_crf": quality_result.crf,
            "quality_metric": sample_result.metric,
            "quality_target": quality_result.target,
            "quality_score": sample_result.score,
            "predicted_video_size_bytes": sample_result.predicted_encode_size_bytes,
            "predicted_total_size_bytes": estimated_total_size_bytes,
            "predicted_encode_percent": (estimated_total_size_bytes / int(sample_item["source_size_bytes"])) * 100,
            "predicted_encode_seconds": sample_result.predicted_encode_seconds,
            "estimated_audio_bytes": overhead["audio_bytes"],
            "estimated_subtitle_bytes": overhead["subtitle_bytes"],
            "estimated_container_bytes": overhead["container_bytes"],
            "sample_stdout": sample_result.stdout,
        },
        "compare_clips": [
            {
                "path": _review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
            }
            for clip in compare_clips
        ],
        "preview_clips": [
            {
                "path": _review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
            }
            for clip in preview_clips
        ],
    }
    return payload, output_dir.parent


def _run_full_calibration(
    *,
    connection: Any,
    config: HarnessConfig,
    prefix: str,
    action: str,
    host_data: dict[str, Any],
    notes: str,
    policy: dict[str, Any],
    seed_metadata: dict[str, Any] | None,
    sample_item: dict[str, Any],
    calibration_run_id: str,
) -> tuple[dict[str, Any], Path, Path]:
    manifest_item = dict(sample_item)
    rel_output = Path(prefix) / Path(sample_item["source_path"]).name
    calibration_staging = config.staging_root / "_calibration" / calibration_run_id / rel_output.name
    calibration_dir = calibration_staging.parent
    manifest_item["staging_path"] = str(calibration_staging)
    manifest = {
        "run_id": calibration_run_id,
        "created_at": _now_iso(),
        "config_path": str(config.paths.config_path),
        "db_path": str(config.paths.db_path),
        "staging_root": str(config.staging_root),
        "output_container": config.output_container,
        "items": [manifest_item],
    }
    manifest_path = config.paths.web_state_dir / f"calibration-{calibration_run_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    encode_result = encode_manifest_items(connection, config, manifest_path, manifest, [0], overwrite=True)[0]
    validation_result = validate_manifest_items(connection, config, manifest, [0])[0]
    output_dir = config.paths.review_dir / calibration_run_id
    clips = generate_compare_clips(
        connection,
        manifest,
        [0],
        output_dir=output_dir,
        duration_seconds=8.0,
        timestamps=None,
        play=False,
    )
    payload = {
        "mode": "full",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "manifest_path": str(manifest_path),
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_path": sample_item["source_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
        },
        "encode_result": {
            "staging_path": str(encode_result.staging_path),
            "source_size_bytes": encode_result.source_size_bytes,
            "staging_size_bytes": encode_result.staging_size_bytes,
            "chosen_crf": encode_result.chosen_crf,
            "quality_metric": encode_result.quality_metric,
            "quality_target": encode_result.quality_target,
            "quality_score": encode_result.quality_score,
        },
        "validation_result": validation_result,
        "compare_clips": [
            {
                "path": _review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
            }
            for clip in clips
        ],
    }
    return payload, manifest_path, calibration_dir
