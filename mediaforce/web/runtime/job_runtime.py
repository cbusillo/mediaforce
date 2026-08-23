import errno
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.tuning.calibration_jobs import RUNNING_JOB_STATUSES, claim_next_queued_calibration_job, \
    load_active_overlapping_job, \
    load_latest_failed_sample_job, \
    load_latest_failed_target_size_sample_job, load_latest_job, load_latest_overlapping_job, \
    load_latest_retryable_sample_job, load_latest_sample_job, load_recent_completed_sample_jobs, queue_position, save_job
from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.library.media_scopes import path_matches_scope, resolve_media_scope, scope_rel_path_filter
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.metadata_sync import sync_external_metadata
from mediaforce.library.scanner import scan_library
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.core.type_defs import JSONValue, float_value, int_value, object_dict
from mediaforce.web.runtime.worker_supervision import run_supervised_worker_loop

_MISSING = object()
ACTIVE_SCAN_JOB_STATUSES = {"queued", "running"}
TERMINAL_SCAN_JOB_STATUSES = {"completed", "failed", "stopped", "paused"}
SUCCESSFUL_SCAN_RUN_STATUS = "completed"
DEFAULT_SCAN_INTERRUPTED_ERROR = "Background scan was interrupted by a web process restart."
_SCAN_SCHEDULE_LOCK = threading.Lock()
_SCAN_JOB_STATE_LOCK = threading.Lock()


@dataclass(slots=True)
class JobRuntimeDeps:
    parse_iso: Any
    now_iso: Any
    run_scan_job: Any
    scan_process_is_alive: Any
    current_catalog_signature: Any
    load_catalog_signature: Any
    load_scan_job_state: Any
    save_scan_job_state: Any
    calibration_job_notice_after: timedelta
    full_scan_stale_after: timedelta
    prefix_scan_stale_after: timedelta
    scan_retry_cooldown: timedelta
    scan_interrupted_error: str
    save_catalog_signature: Any
    reset_folder_card_cache: Any
    start_scan_job_thread: Callable[..., None] | None = None
    list_scan_job_files: Callable[[MediaforceConfig], Iterable[Any]] | None = None


@dataclass(slots=True)
class CalibrationQueueRuntimeDeps:
    now_iso: Any
    run_calibration_job: Any
    job_seed_metadata: Any
    save_job_state: Any
    mark_calibration_submission_complete: Any
    register_calibration_process_controller: Any
    unregister_calibration_process_controller: Any
    submission_cleanup_callback: Callable[[MediaforceConfig, dict[str, Any]], Callable[[Future[object]], None]]
    calibration_submissions: set[str]
    calibration_submissions_lock: Any
    calibration_executors: dict[str, ThreadPoolExecutor]
    sample_calibration_concurrency: int
    full_calibration_concurrency: int
    calibration_queue_poll_seconds: float


def calibration_job_public_payload(
        connection: DBClient,
        config: MediaforceConfig,
        payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    owner_prefix = str(payload.get("prefix") or "").strip()
    if not owner_prefix:
        return dict(payload)
    scope = resolve_media_scope(
        connection,
        owner_prefix,
        library_types=config.library_type_map,
    )
    return {
        **payload,
        "activity_scope": scope.to_payload(),
        "target_contract": calibration_job_target_contract(payload),
        "progress": calibration_job_progress_payload(connection, payload),
    }


def calibration_job_target_contract(payload: dict[str, Any]) -> dict[str, Any] | None:
    video = object_dict(object_dict(payload.get("policy")).get("video"))
    target_size_bytes = int_value(video.get("target_size_bytes"))
    target_size_mb = float_value(video.get("target_size_mb"))
    if target_size_bytes <= 0 and target_size_mb > 0:
        target_size_bytes = round(target_size_mb * 1_000_000)
    if target_size_bytes <= 0:
        return None
    max_height = int_value(video.get("max_height"))
    resolution_mode = str(video.get("resolution_intent_mode") or "").strip()
    if not resolution_mode:
        resolution_mode = "source" if max_height <= 0 else "max_height"
    return {
        "schema_version": 1,
        "target_size_bytes": target_size_bytes,
        "mode": str(video.get("size_goal_mode") or "legacy"),
        "source": str(video.get("size_goal_source") or "legacy"),
        "target_runtime_minutes": float_value(video.get("target_runtime_minutes")) or None,
        "sample_tolerance_percent": float_value(video.get("sample_projection_tolerance_percent")) or None,
        "final_tolerance_percent": float_value(video.get("final_output_tolerance_percent")) or None,
        "resolution_mode": resolution_mode,
        "max_height": max_height if max_height > 0 else None,
    }


def calibration_scope_relation(route_prefix: str, owner_prefix: str) -> str | None:
    normalized_route = route_prefix.strip("/")
    normalized_owner = owner_prefix.strip("/")
    if not normalized_route or not normalized_owner:
        return None
    if normalized_route == normalized_owner:
        return "exact"
    if normalized_route.startswith(f"{normalized_owner}/"):
        return "ancestor"
    if normalized_owner.startswith(f"{normalized_route}/"):
        return "descendant"
    return None


def calibration_job_compatibility_key(payload: dict[str, Any]) -> str:
    host = object_dict(payload.get("host"))
    video = object_dict(object_dict(payload.get("policy")).get("video"))
    sample_item = object_dict(payload.get("sample_item"))
    height = int_value(sample_item.get("height"))
    duration_seconds = float_value(sample_item.get("duration_seconds"))
    width = int_value(sample_item.get("width"))
    target_size_bytes = int_value(video.get("target_size_bytes"))
    if target_size_bytes <= 0:
        target_size_bytes = round(float_value(video.get("target_size_mb")) * 1_000_000)
    signature = {
        "schema_version": 2,
        "pipeline": "sample-calibration-v1",
        "host": str(host.get("key") or host.get("host") or host.get("label") or "local"),
        "encoder": str(video.get("encoder") or ""),
        "preset": str(video.get("preset") or video.get("speed") or ""),
        "pixel_format": str(video.get("pixel_format") or ""),
        "quality_engine": str(video.get("quality_engine") or ""),
        "quality_metric": str(video.get("quality_metric") or ""),
        "target_vmaf": float_value(video.get("target_vmaf")),
        "target_xpsnr": float_value(video.get("target_xpsnr")),
        "min_target_vmaf": float_value(video.get("min_target_vmaf")),
        "min_target_xpsnr": float_value(video.get("min_target_xpsnr")),
        "min_crf": int_value(video.get("min_crf")),
        "max_crf": int_value(video.get("max_crf")),
        "target_search_max_crf": int_value(video.get("target_search_max_crf")),
        "max_encoded_percent": float_value(video.get("max_encoded_percent")),
        "thorough": bool(video.get("thorough", False)),
        "sample_every": str(video.get("sample_every") or ""),
        "sample_duration": str(video.get("sample_duration") or ""),
        "target_size_search": target_size_bytes > 0,
        "target_size_bucket_mb": round(target_size_bytes / 25_000_000) * 25 if target_size_bytes > 0 else 0,
        "source_codec": str(sample_item.get("video_codec") or ""),
        "source_width_class": _resolution_class(width),
        "resolution_class": _resolution_class(height),
        "cadence_class": str(sample_item.get("cadence_class") or ""),
        "runtime_bucket_minutes": round(duration_seconds / 900) * 15 if duration_seconds > 0 else 0,
    }
    encoded = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def calibration_job_progress_payload(
        connection: DBClient,
        payload: dict[str, Any],
) -> dict[str, Any] | None:
    stored = object_dict(payload.get("progress"))
    status = str(payload.get("status") or "").strip()
    if not stored and status not in {"queued", "starting", "running"}:
        return None
    now = datetime.now(tz=UTC)
    compatibility_key = str(stored.get("compatibility_key") or calibration_job_compatibility_key(payload))
    heartbeat_at = _parse_job_timestamp(payload.get("heartbeat_at"))
    started_at = _parse_job_timestamp(payload.get("started_at") or payload.get("created_at"))
    stage_started_at = _parse_job_timestamp(stored.get("stage_started_at"))
    finished_at = _parse_job_timestamp(payload.get("finished_at"))
    active_status = status in {"starting", "running"}
    terminal_elapsed_seconds = int_value(stored.get("total_elapsed_seconds"))
    if terminal_elapsed_seconds <= 0 and started_at and finished_at and finished_at >= started_at:
        terminal_elapsed_seconds = round((finished_at - started_at).total_seconds())
    elapsed_seconds = (
        max(0, round((now - started_at).total_seconds()))
        if active_status and started_at
        else terminal_elapsed_seconds or None
    )
    heartbeat_age_seconds = (
        max(0, round((now - heartbeat_at).total_seconds()))
        if active_status and heartbeat_at
        else None
    )
    stage_elapsed_seconds = (
        max(0, round((now - stage_started_at).total_seconds()))
        if active_status and stage_started_at
        else None
    )
    estimate = (
        _calibration_history_estimate(
            connection,
            payload,
            compatibility_key=compatibility_key,
            elapsed_seconds=elapsed_seconds,
        )
        if status in {"queued", "starting", "running"}
        else None
    )
    public_progress = {
        **stored,
        "schema_version": 1,
        "compatibility_key": compatibility_key,
        "elapsed_seconds": elapsed_seconds,
        "stage_elapsed_seconds": stage_elapsed_seconds,
        "estimate": estimate,
    }
    if status == "queued":
        public_progress["liveness"] = "queued"
    elif active_status:
        if heartbeat_age_seconds is None:
            public_progress["liveness"] = (
                "starting" if elapsed_seconds is not None and elapsed_seconds <= 45 else "not_reporting"
            )
        elif heartbeat_age_seconds <= 15:
            public_progress["liveness"] = "reporting"
        elif heartbeat_age_seconds <= 45:
            public_progress["liveness"] = "delayed"
        else:
            public_progress["liveness"] = "not_reporting"
        public_progress["heartbeat_at"] = payload.get("heartbeat_at")
        public_progress["heartbeat_age_seconds"] = heartbeat_age_seconds
    else:
        public_progress.pop("heartbeat_at", None)
        public_progress.pop("heartbeat_age_seconds", None)
        public_progress.pop("liveness", None)
    return public_progress


def _calibration_history_estimate(
        connection: DBClient,
        payload: dict[str, Any],
        *,
        compatibility_key: str,
        elapsed_seconds: int | None,
) -> dict[str, Any] | None:
    if str(payload.get("status") or "") not in {"running", "starting"} or elapsed_seconds is None:
        return None
    durations = []
    current_job_id = str(payload.get("job_id") or "")
    for completed in load_recent_completed_sample_jobs(connection):
        if str(completed.get("job_id") or "") == current_job_id:
            continue
        if calibration_job_compatibility_key(completed) != compatibility_key:
            continue
        started_at = _parse_job_timestamp(completed.get("started_at"))
        finished_at = _parse_job_timestamp(completed.get("finished_at"))
        if started_at is None or finished_at is None or finished_at <= started_at:
            continue
        durations.append((finished_at - started_at).total_seconds())
    if len(durations) < 3:
        return None
    ordered = sorted(durations[:30])
    total_low = _quantize_seconds(_percentile(ordered, 0.10), round_up=False)
    total_high = _quantize_seconds(_percentile(ordered, 0.90), round_up=True)
    total_high = max(total_high, total_low + 300)
    return {
        "kind": "historical_range",
        "sample_size": len(ordered),
        "remaining_seconds_low": max(0, total_low - elapsed_seconds),
        "remaining_seconds_high": max(0, total_high - elapsed_seconds),
        "total_seconds_low": total_low,
        "total_seconds_high": total_high,
        "longer_than_recent_runs": elapsed_seconds > total_high,
        "confidence": "moderate" if len(ordered) >= 8 else "limited",
        "basis": "Comparable completed tests on the same computer and encode profile",
    }


def _parse_job_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _resolution_class(height: int) -> str:
    if height <= 0:
        return "unknown"
    if height <= 576:
        return "sd"
    if height <= 900:
        return "720p"
    if height <= 1440:
        return "1080p"
    return "uhd"


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(values) - 1)
    weight = position - lower_index
    return values[lower_index] * (1 - weight) + values[upper_index] * weight


def _quantize_seconds(value: float, *, round_up: bool) -> int:
    step = 300 if value >= 900 else 60
    units = value / step
    whole_units = int(units)
    if round_up and units > whole_units:
        whole_units += 1
    return max(step, whole_units * step)


def load_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    payload = load_latest_job(connection, prefix)
    return _job_state_from_payload(connection, config, prefix, payload, deps)


def load_overlapping_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    payload = load_active_overlapping_job(connection, prefix) or load_latest_overlapping_job(connection, prefix)
    save_prefix = str(payload.get("prefix") or prefix) if payload is not None else prefix
    return _job_state_from_payload(connection, config, save_prefix, payload, deps)


def _job_state_from_payload(
        connection: DBClient,
        config: MediaforceConfig,
        save_prefix: str,
        payload: dict[str, Any] | None,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    status = str(payload.get("status") or "")
    if status == "running" and not calibration_job_belongs_to_current_process(payload):
        payload = expire_calibration_job(connection, config, save_prefix, payload, deps)
        status = str(payload.get("status") or "")
    if status == "queued":
        position = queue_position(connection, str(payload["job_id"]))
        if position is not None:
            payload["queue_position"] = position[0]
            payload["queue_depth"] = position[1]
    if status in {"failed", "completed", "stopped"}:
        finished_at = deps.parse_iso(payload.get("finished_at") or payload.get("started_at") or payload.get("created_at"))
        if (
                finished_at and
                datetime.now(tz=UTC) - finished_at > deps.calibration_job_notice_after and
                not retryable_saved_sample_job(payload)
        ):
            return None
    return payload


def load_retryable_sample_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    payload = load_latest_retryable_sample_job(connection, prefix)
    if payload is None:
        return None
    latest_sample_payload = load_latest_sample_job(connection, prefix)
    if latest_sample_payload is not None and str(latest_sample_payload.get("job_id") or "") != str(payload.get("job_id") or ""):
        return None
    status = str(payload.get("status") or "")
    if status == "running" and not calibration_job_belongs_to_current_process(payload):
        payload = expire_calibration_job(connection, config, prefix, payload, deps)
        status = str(payload.get("status") or "")
    if status == "queued":
        position = queue_position(connection, str(payload["job_id"]))
        if position is not None:
            payload["queue_position"] = position[0]
            payload["queue_depth"] = position[1]
    if status in {"failed", "stopped"} and not retryable_saved_sample_job(payload):
        return None
    if status in {"failed", "completed", "stopped"}:
        finished_at = deps.parse_iso(payload.get("finished_at") or payload.get("started_at") or payload.get("created_at"))
        if (
                finished_at and
                datetime.now(tz=UTC) - finished_at > deps.calibration_job_notice_after and
                not retryable_saved_sample_job(payload)
        ):
            return None
    return payload


def load_latest_failed_sample_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    _ = config, deps
    return load_latest_failed_sample_job(connection, prefix)


def load_latest_failed_target_size_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    _ = config, deps
    payload = load_latest_failed_target_size_sample_job(connection, prefix)
    if payload is None:
        return None
    latest_sample_payload = load_latest_sample_job(connection, prefix)
    if (
            latest_sample_payload is not None
            and str(latest_sample_payload.get("job_id") or "") != str(payload.get("job_id") or "")
    ):
        return None
    return payload


def save_job_state(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        payload: dict[str, Any],
        deps: JobRuntimeDeps,
) -> None:
    _ = config, deps
    save_job(connection, {**payload, "prefix": prefix, "updated_at": deps.now_iso()})


def calibration_job_belongs_to_current_process(job: dict[str, Any]) -> bool:
    numeric_pid = _coerce_pid(job.get("owner_pid"))
    return numeric_pid is not None and numeric_pid == os.getpid()


def retryable_saved_sample_job(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").strip()
    if status not in {"failed", "stopped"}:
        return False
    mode = str(job.get("mode") or job.get("lane") or "sample").strip()
    if mode != "sample":
        return False
    result = object_dict(job.get("result"))
    trace = object_dict(result.get("target_size_trace"))
    target_status = str(result.get("target_size_status") or trace.get("status") or "").strip().lower()
    if target_status in {"infeasible", "bound_exhausted", "quality_conflict"}:
        return False
    action = str(job.get("action") or "").strip()
    return action in {"baseline", "ai_tune"}


def expire_calibration_job(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        job: dict[str, Any],
        deps: JobRuntimeDeps,
) -> dict[str, Any]:
    expired = {
        **job,
        "status": "failed",
        "finished_at": deps.now_iso(),
        "error": "Calibration was interrupted by a web process restart.",
    }
    save_job_state(connection, config, prefix, expired, deps)
    return expired


def load_scan_job_state(
        config: MediaforceConfig,
        prefix: str | None,
        scan_job_file: Any,
) -> dict[str, Any] | None:
    path = Path(scan_job_file(config, prefix))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _malformed_scan_job(path)
    if not isinstance(payload, dict):
        return _malformed_scan_job(path)
    return payload


def save_scan_job_state(
        config: MediaforceConfig,
        prefix: str | None,
        payload: dict[str, Any],
        scan_job_file: Any,
) -> None:
    _atomic_write_json(Path(scan_job_file(config, prefix)), payload)


def maybe_schedule_scan(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
        *,
        force: bool = False,
) -> dict[str, Any] | None:
    with _SCAN_SCHEDULE_LOCK:
        active_scan = active_scan_from_db(connection, config, prefix, deps)
        if active_scan is not None:
            return active_scan
        if prefix is not None:
            full_job = deps.load_scan_job_state(config, None)
            if full_job and full_job.get("status") in ACTIVE_SCAN_JOB_STATUSES and not scan_job_belongs_to_current_process(
                    full_job,
                    deps.scan_process_is_alive,
            ):
                full_job = _expire_scan_job(config, None, full_job, deps)
            if full_job and full_job.get("status") in ACTIVE_SCAN_JOB_STATUSES:
                return full_job
        job = deps.load_scan_job_state(config, prefix)
        if job and job.get("status") in ACTIVE_SCAN_JOB_STATUSES and not scan_job_belongs_to_current_process(
                job,
                deps.scan_process_is_alive,
        ):
            job = _expire_scan_job(config, prefix, job, deps)
        if job and job.get("status") in ACTIVE_SCAN_JOB_STATUSES:
            return job
        if background_work_is_paused(connection):
            return job
        if not force and not scan_is_stale(connection, config, prefix, deps):
            return job
        if not force and job and job.get("status") == "failed":
            finished_at = deps.parse_iso(job.get("finished_at") or job.get("started_at"))
            interrupted_restart = str(job.get("error") or "") == deps.scan_interrupted_error
            if not interrupted_restart and finished_at and datetime.now(tz=UTC) - finished_at < deps.scan_retry_cooldown:
                return job

        scan_id = uuid.uuid4().hex[:12]
        job_payload = {
            "job_id": scan_id,
            "scan_id": scan_id,
            "status": "queued",
            "scope": "full" if prefix is None else "prefix",
            "prefix": prefix,
            "owner_pid": os.getpid(),
            "created_at": deps.now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        _save_scan_job_state(config, prefix, job_payload, deps)
        thread_kwargs = {
            "config_path": config.paths.config_path,
            "prefix": prefix,
            "job_id": str(job_payload["job_id"]),
            "process_controller": ManagedProcessController(),
        }
        try:
            if deps.start_scan_job_thread is not None:
                deps.start_scan_job_thread(**thread_kwargs)
            else:
                thread = threading.Thread(
                    target=deps.run_scan_job,
                    kwargs=thread_kwargs,
                    name=f"scan-job-{job_payload['job_id']}",
                )
                thread.start()
        except BaseException as exc:
            _save_worker_scan_job_state(
                config,
                prefix,
                str(job_payload["scan_id"]),
                {
                    **job_payload,
                    "status": "failed",
                    "finished_at": deps.now_iso(),
                    "error": str(exc) or type(exc).__name__,
                },
                deps,
            )
            raise
        return job_payload


def load_scan_status(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    active_scan = active_scan_from_db(connection, config, prefix, deps, repair_stale=False)
    if active_scan is not None:
        return active_scan
    if prefix is not None:
        full_job = _current_scan_job_snapshot(connection, config, None, deps)
        if full_job and full_job.get("status") in ACTIVE_SCAN_JOB_STATUSES:
            return full_job
    return _current_scan_job_snapshot(connection, config, prefix, deps)


def run_scan_job(
        *,
        config_path: Any,
        prefix: str | None,
        job_id: str,
        process_controller: ManagedProcessController,
        deps: JobRuntimeDeps,
) -> None:
    config = load_config(config_path)
    job: dict[str, Any] = {}
    with open_db(config.paths.db_path) as connection:
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            with _SCAN_JOB_STATE_LOCK:
                job = deps.load_scan_job_state(config, prefix) or {}
                if not _scan_job_owned_by_worker(job, scan_id=job_id):
                    connection.rollback()
                    return
                if background_work_is_paused(connection):
                    job.update(
                        {
                            "job_id": job_id,
                            "scan_id": job_id,
                            "status": "paused",
                            "started_at": None,
                            "finished_at": deps.now_iso(),
                            "error": None,
                            "stats": None,
                        }
                    )
                    deps.save_scan_job_state(config, prefix, job)
                    return
                job.update(
                    {
                        "job_id": job_id,
                        "scan_id": job_id,
                        "status": "running",
                        "started_at": deps.now_iso(),
                        "finished_at": None,
                        "error": None,
                    }
                )
                deps.save_scan_job_state(config, prefix, job)
        except BaseException:
            connection.rollback()
            raise
    cleanup_required = False
    try:
        process_controller.throw_if_cancelled()
        cleanup_required = True
        purge_transient_artifacts(config, force=True)
        process_controller.throw_if_cancelled()
        metadata_stats: dict[str, Any] | None = None
        with open_db(config.paths.db_path) as connection:
            stats = scan_library(
                connection,
                config,
                prefixes=[prefix] if prefix else None,
                process_controller=process_controller,
                scan_id=job_id,
            )
            process_controller.throw_if_cancelled()
            if prefix is None:
                try:
                    process_controller.throw_if_cancelled()
                    metadata_stats = sync_external_metadata(connection, config).to_payload()
                    process_controller.throw_if_cancelled()
                except ProcessCancelledError:
                    raise
                except Exception as exc:
                    metadata_stats = {
                        "status": "completed_with_warnings",
                        "message": f"External metadata refresh failed: {type(exc).__name__}",
                    }
        process_controller.throw_if_cancelled()
        if prefix is None:
            deps.save_catalog_signature(config)
        process_controller.throw_if_cancelled()
        deps.reset_folder_card_cache()
        process_controller.throw_if_cancelled()
        _save_worker_scan_job_state(
            config,
            prefix,
            job_id,
            {
                **job,
                "job_id": job_id,
                "scan_id": job_id,
                "status": "completed",
                "finished_at": deps.now_iso(),
                "error": None,
                "stats": _stats_payload(stats),
                "metadata": metadata_stats,
            },
            deps,
        )
    except ProcessCancelledError:
        _save_worker_scan_job_state(
            config,
            prefix,
            job_id,
            {
                **job,
                "job_id": job_id,
                "scan_id": job_id,
                "status": "failed",
                "finished_at": deps.now_iso(),
                "error": deps.scan_interrupted_error,
                "stats": None,
            },
            deps,
        )
    except Exception as exc:
        _save_worker_scan_job_state(
            config,
            prefix,
            job_id,
            {
                **job,
                "job_id": job_id,
                "scan_id": job_id,
                "status": "failed",
                "finished_at": deps.now_iso(),
                "error": str(exc),
                "stats": None,
            },
            deps,
        )
    finally:
        if cleanup_required:
            purge_transient_artifacts(config, force=True)


def dispatch_calibration_job(
        config: MediaforceConfig,
        job_payload: dict[str, Any],
        deps: CalibrationQueueRuntimeDeps,
) -> None:
    job_id = str(job_payload["job_id"])
    with deps.calibration_submissions_lock:
        if job_id in deps.calibration_submissions:
            return
        deps.calibration_submissions.add(job_id)
    process_controller = ManagedProcessController()
    deps.register_calibration_process_controller(job_id, process_controller)
    lane = str(job_payload.get("lane") or job_payload.get("mode") or "sample")
    task = partial(
        deps.run_calibration_job,
        config_path=config.paths.config_path,
        prefix=str(job_payload["prefix"]),
        action=str(job_payload["action"]),
        host_data=object_dict(job_payload.get("host")),
        notes=str(job_payload.get("notes") or ""),
        policy=object_dict(job_payload.get("policy")),
        job_id=job_id,
        seed_metadata=deps.job_seed_metadata(job_payload),
        process_controller=process_controller,
    )
    try:
        future = deps.calibration_executors[lane].submit(task)
    except Exception:
        deps.mark_calibration_submission_complete(job_id)
        deps.unregister_calibration_process_controller(job_id)
        raise
    future.add_done_callback(deps.submission_cleanup_callback(config, job_payload))


def calibration_queue_worker_loop(
        *,
        config_path: Any,
        deps: CalibrationQueueRuntimeDeps,
        logger: Any,
        stop_event: threading.Event,
) -> None:
    run_supervised_worker_loop(
        process_once_fn=lambda: process_calibration_queue_once(config_path=config_path, deps=deps),
        poll_seconds=deps.calibration_queue_poll_seconds,
        stop_event=stop_event,
        logger=logger,
        failure_message="Calibration queue worker pass failed",
    )


def process_calibration_queue_once(*, config_path: Any, deps: CalibrationQueueRuntimeDeps) -> None:
    config = load_config(config_path)
    capacities = {
        "sample": deps.sample_calibration_concurrency,
        "full": deps.full_calibration_concurrency,
    }
    with open_db(config.paths.db_path) as connection:
        active_rows = connection.execute(
            select(
                calibration_jobs.c.job_id,
                calibration_jobs.c.lane,
                calibration_jobs.c.prefix,
                calibration_jobs.c.status,
            )
            .where(calibration_jobs.c.status.in_(tuple(RUNNING_JOB_STATUSES)))
            .order_by(calibration_jobs.c.created_at, literal_column("rowid"))
        ).mappings().fetchall()
        running_by_lane = {lane: 0 for lane in capacities}
        active_prefixes: set[str] = set()
        for row in active_rows:
            lane = str(row["lane"])
            status = str(row["status"])
            prefix = str(row["prefix"])
            active_prefixes.add(prefix)
            if status in {"starting", "running"} and lane in running_by_lane:
                running_by_lane[lane] += 1

        for lane, capacity in capacities.items():
            while running_by_lane[lane] < capacity:
                started_at = deps.now_iso()
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
                    dispatch_calibration_job(config, payload, deps)
                except Exception as exc:
                    deps.mark_calibration_submission_complete(str(payload["job_id"]))
                    deps.save_job_state(
                        connection,
                        config,
                        str(payload["prefix"]),
                        {
                            **payload,
                            "status": "failed",
                            "owner_pid": os.getpid(),
                            "started_at": payload.get("started_at") or started_at,
                            "finished_at": deps.now_iso(),
                            "error": str(exc),
                        },
                    )
                    connection.commit()
                    continue
                active_prefixes.add(str(payload["prefix"]))
                running_by_lane[lane] += 1


def scan_is_stale(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
) -> bool:
    if prefix is None:
        if deps.load_catalog_signature(config) != deps.current_catalog_signature(config):
            return True
        item_count = int(connection.execute(select(func.count()).select_from(library_items)).scalar_one())
        if item_count == 0:
            return True
        latest = latest_scan_completed_at(connection, prefix=None)
        if latest is None:
            return True
        return datetime.now(tz=UTC) - latest > deps.full_scan_stale_after

    scope = resolve_media_scope(connection, prefix)
    item_count = int(
        connection.execute(
            select(func.count())
            .select_from(library_items)
            .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        ).scalar_one()
    )
    if item_count == 0:
        return True
    latest = latest_scan_completed_at(connection, prefix=prefix)
    if latest is None:
        return True
    return datetime.now(tz=UTC) - latest > deps.prefix_scan_stale_after

def latest_scan_completed_at(connection: DBClient, prefix: str | None) -> datetime | None:
    rows = connection.execute(
        select(
            scan_runs.c.completed_at,
            scan_runs.c.started_at,
            scan_runs.c.scope,
            scan_runs.c.prefixes_json,
            scan_runs.c.status,
        )
        .order_by(scan_runs.c.started_at.desc())
        .limit(250)
    ).mappings().fetchall()
    for row in rows:
        if not scan_run_completed_successfully(row):
            continue
        scope = str(row["scope"] or "unknown")
        completed = _parse_iso(row["completed_at"])
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
            if normalized and path_matches_scope(prefix, normalized):
                return completed
    return None


def active_scan_from_db(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
        *,
        repair_stale: bool = True,
) -> dict[str, Any] | None:
    rows = connection.execute(
        select(
            scan_runs.c.scan_id,
            scan_runs.c.started_at,
            scan_runs.c.scope,
            scan_runs.c.prefixes_json,
            scan_runs.c.owner_pid,
            scan_runs.c.last_progress_at,
            scan_runs.c.file_count,
            scan_runs.c.reprobed_count,
            scan_runs.c.unchanged_count,
            scan_runs.c.status,
            scan_runs.c.error,
        )
        .where(scan_runs.c.completed_at.is_(None))
        .where(~scan_runs.c.status.in_(tuple(TERMINAL_SCAN_JOB_STATUSES)))
        .order_by(scan_runs.c.started_at.desc())
        .limit(25)
    ).mappings().fetchall()
    for row in rows:
        matched_prefix = _scan_run_matches_prefix(row, prefix)
        if matched_prefix is _MISSING:
            continue

        scope = str(row["scope"] or "unknown")
        row_status = str(row["status"] or "running")
        job_prefix = None if scope in {"full", "unknown"} else str(matched_prefix)
        job = deps.load_scan_job_state(config, job_prefix)
        if job is not None and not _scan_job_matches_database_row(
                job,
                scan_id=str(row["scan_id"]),
        ):
            job = None
        job_status = str(job.get("status") or "") if job else ""
        if job and job_status in TERMINAL_SCAN_JOB_STATUSES:
            if repair_stale and job_status != SUCCESSFUL_SCAN_RUN_STATUS:
                _expire_scan_run(
                    connection,
                    str(row["scan_id"]),
                    str(job.get("error") or deps.scan_interrupted_error),
                )
            return _scan_job_payload_for_prefix(job, prefix=prefix)
        if row_status in TERMINAL_SCAN_JOB_STATUSES:
            if row_status == "failed" and job and job_status in ACTIVE_SCAN_JOB_STATUSES and repair_stale:
                deps.save_scan_job_state(
                    config,
                    job_prefix,
                    _interrupted_scan_job(
                        job,
                        deps,
                        finished_at=row["completed_at"] or row["last_progress_at"] or row["started_at"],
                    ),
                )
            return _scan_run_terminal_job(row, prefix=prefix, job_prefix=job_prefix)
        if job and job_status in ACTIVE_SCAN_JOB_STATUSES and not deps.scan_process_is_alive(job.get("owner_pid")):
            job = (
                _expire_scan_job(config, job_prefix, job, deps, connection=connection, scan_id=str(row["scan_id"]))
                if repair_stale
                else _interrupted_scan_job(
                    job,
                    deps,
                    finished_at=job.get("finished_at") or job.get("started_at") or job.get("created_at"),
                )
            )

        if deps.scan_process_is_alive(row["owner_pid"]):
            return {
                "job_id": str(row["scan_id"]),
                "scan_id": str(row["scan_id"]),
                "status": "running",
                "scope": scope,
                "prefix": None if prefix is None else (job_prefix or prefix),
                "created_at": row["started_at"],
                "started_at": row["started_at"],
                "last_progress_at": row["last_progress_at"] or row["started_at"],
                "finished_at": None,
                "error": None,
                "stats": _scan_job_progress(row),
            }

        if job and job.get("status") in ACTIVE_SCAN_JOB_STATUSES:
            return {
                "job_id": str(job.get("job_id") or row["scan_id"]),
                "scan_id": str(job.get("scan_id") or row["scan_id"]),
                "status": str(job.get("status") or "running"),
                "scope": str(job.get("scope") or scope),
                "prefix": job.get("prefix") if prefix is not None else None,
                "created_at": job.get("created_at") or row["started_at"],
                "started_at": job.get("started_at") or row["started_at"],
                "finished_at": job.get("finished_at"),
                "error": job.get("error"),
                "stats": job.get("stats"),
            }

        if repair_stale:
            _expire_scan_run(connection, str(row["scan_id"]), deps.scan_interrupted_error)
    return None


def repair_stale_scan_runs(
        connection: DBClient,
        config: MediaforceConfig,
        deps: JobRuntimeDeps,
) -> int:
    rows = connection.execute(
        select(
            scan_runs.c.scan_id,
            scan_runs.c.started_at,
            scan_runs.c.scope,
            scan_runs.c.prefixes_json,
            scan_runs.c.owner_pid,
            scan_runs.c.last_progress_at,
            scan_runs.c.status,
            scan_runs.c.error,
        )
        .where(scan_runs.c.completed_at.is_(None))
        .where(~scan_runs.c.status.in_(tuple(TERMINAL_SCAN_JOB_STATUSES)))
        .order_by(scan_runs.c.started_at.desc())
    ).mappings().fetchall()
    repaired = 0
    for row in rows:
        status = str(row["status"] or "running")
        if status in TERMINAL_SCAN_JOB_STATUSES:
            continue
        scope = str(row["scope"] or "unknown")
        job_prefixes = [None]
        if scope == "prefix":
            job_prefixes = _prefixes_for_scan_run(row)
        _expire_scan_run(
            connection,
            str(row["scan_id"]),
            deps.scan_interrupted_error,
        )
        for job_prefix in job_prefixes:
            job = deps.load_scan_job_state(config, job_prefix)
            if (
                job
                and _scan_job_matches_database_row(
                    job,
                    scan_id=str(row["scan_id"]),
                )
                and str(job.get("status") or "") in ACTIVE_SCAN_JOB_STATUSES
            ):
                deps.save_scan_job_state(
                    config,
                    job_prefix,
                    _interrupted_scan_job(
                        job,
                        deps,
                        finished_at=(
                            row["last_progress_at"]
                            or row["started_at"]
                        ),
                    ),
                )
        repaired += 1
    if deps.list_scan_job_files is None:
        return repaired
    for raw_path in deps.list_scan_job_files(config):
        path = Path(raw_path)
        job, malformed = _load_scan_job_file(path)
        if job is None:
            continue
        if malformed:
            _save_scan_job_file(
                path,
                _interrupted_scan_job(job, deps, finished_at=deps.now_iso()),
            )
            repaired += 1
            continue
        if str(job.get("status") or "") in ACTIVE_SCAN_JOB_STATUSES:
            job_prefix = (
                None
                if str(job.get("scope") or "") in {"full", "unknown"}
                else str(job.get("prefix") or "").strip("/") or None
            )
            terminal_run = _latest_scan_terminal_job(
                connection,
                job_prefix,
                scan_id=_scan_job_database_id(job),
            )
            replacement = (
                terminal_run
                if terminal_run is not None
                and _scan_run_supersedes_job(terminal_run, job, deps)
                else _interrupted_scan_job(
                    job,
                    deps,
                    finished_at=(
                        job.get("finished_at")
                        or job.get("started_at")
                        or job.get("created_at")
                        or deps.now_iso()
                    ),
                )
            )
            _save_scan_job_file(path, replacement)
            repaired += 1
    return repaired


def scan_process_is_alive(pid: JSONValue) -> bool:
    numeric_pid = _coerce_pid(pid)
    if numeric_pid is None:
        return False
    if numeric_pid <= 0:
        return False
    try:
        os.kill(numeric_pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def scan_job_belongs_to_current_process(job: dict[str, Any], scan_process_is_alive_fn: Any) -> bool:
    owner_pid = job.get("owner_pid")
    numeric_pid = _coerce_pid(owner_pid)
    if numeric_pid is None:
        return False
    return scan_process_is_alive_fn(owner_pid) and numeric_pid == os.getpid()


def _coerce_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


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


def _scan_run_matches_prefix(row: DBRow, prefix: str | None) -> str | None | object:
    scope = str(row["scope"] or "unknown")
    if prefix is None:
        if scope in {"full", "unknown"}:
            return None
        return _MISSING

    if scope in {"full", "unknown"}:
        return prefix

    try:
        prefixes = json.loads(row["prefixes_json"] or "[]")
    except json.JSONDecodeError:
        prefixes = []
    for candidate in prefixes:
        normalized = str(candidate).strip("/")
        if normalized and path_matches_scope(prefix, normalized):
            return normalized
    return _MISSING


def _expire_scan_run(connection: DBClient, scan_id: str, error: str) -> None:
    expired_at = _now_iso()
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(
            completed_at=func.coalesce(scan_runs.c.completed_at, expired_at),
            last_progress_at=func.coalesce(scan_runs.c.last_progress_at, expired_at),
            status="failed",
            error=error,
        )
    )
    connection.commit()


def scan_run_completed_successfully(row: DBRow) -> bool:
    return str(row["status"] or "") == SUCCESSFUL_SCAN_RUN_STATUS and bool(row["completed_at"])


def _scan_run_terminal_job(
        row: DBRow,
        *,
        prefix: str | None,
        job_prefix: str | None,
) -> dict[str, Any]:
    status = str(row["status"] or "failed")
    finished_at = row["completed_at"] or row["last_progress_at"] or row["started_at"]
    return {
        "job_id": str(row["scan_id"]),
        "scan_id": str(row["scan_id"]),
        "status": status,
        "scope": str(row["scope"] or "unknown"),
        "prefix": None if prefix is None else (job_prefix or prefix),
        "created_at": row["started_at"],
        "started_at": row["started_at"],
        "last_progress_at": row["last_progress_at"] or row["started_at"],
        "finished_at": finished_at,
        "error": row["error"],
        "stats": _scan_job_progress(row),
    }


def _scan_job_payload_for_prefix(job: dict[str, Any], *, prefix: str | None) -> dict[str, Any]:
    if prefix is None:
        return {**job, "prefix": None}
    return job


def _prefixes_for_scan_run(row: DBRow) -> list[str | None]:
    try:
        values = json.loads(row["prefixes_json"] or "[]")
    except json.JSONDecodeError:
        values = []
    prefixes = [str(value).strip("/") for value in values if str(value).strip("/")]
    return prefixes or [None]


def _scan_job_progress(row: DBRow) -> dict[str, int]:
    return {
        "items_seen": int(row["file_count"] or 0),
        "updated_paths": int(row["reprobed_count"] or 0),
        "unchanged": int(row["unchanged_count"] or 0),
    }


def _expire_scan_job(
        config: MediaforceConfig,
        prefix: str | None,
        job: dict[str, Any],
        deps: JobRuntimeDeps,
        *,
        connection: DBClient | None = None,
        scan_id: str | None = None,
) -> dict[str, Any]:
    expired = _interrupted_scan_job(job, deps, finished_at=deps.now_iso())
    if connection is not None and scan_id:
        _expire_scan_run(connection, scan_id, str(expired.get("error") or deps.scan_interrupted_error))
    deps.save_scan_job_state(config, prefix, expired)
    return expired


def _scan_job_snapshot(
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    job = deps.load_scan_job_state(config, prefix)
    if not job:
        return None
    if job.get("status") in ACTIVE_SCAN_JOB_STATUSES and not deps.scan_process_is_alive(job.get("owner_pid")):
        return _interrupted_scan_job(
            job,
            deps,
            finished_at=job.get("finished_at") or job.get("started_at") or job.get("created_at"),
        )
    return job


def _current_scan_job_snapshot(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    job = _scan_job_snapshot(config, prefix, deps)
    if not job:
        return job
    if (
        job.get("status") in ACTIVE_SCAN_JOB_STATUSES
        and scan_job_belongs_to_current_process(
            job,
            deps.scan_process_is_alive,
        )
    ):
        return job
    terminal_run = _latest_scan_terminal_job(
        connection,
        prefix,
        scan_id=_scan_job_database_id(job),
    )
    if (
        terminal_run is not None
        and _scan_run_supersedes_job(terminal_run, job, deps)
    ):
        if (
            terminal_run.get("status") != SUCCESSFUL_SCAN_RUN_STATUS
            or job.get("status") in ACTIVE_SCAN_JOB_STATUSES
            or str(job.get("error") or "") == deps.scan_interrupted_error
        ):
            return terminal_run
    if job.get("status") in ACTIVE_SCAN_JOB_STATUSES:
        return job
    latest_completed = latest_scan_completed_at(connection, prefix)
    if latest_completed is None:
        return job
    job_timestamp = deps.parse_iso(
        job.get("finished_at")
        or job.get("started_at")
        or job.get("created_at")
    )
    if job_timestamp is None or job_timestamp < latest_completed:
        return None
    return job


def _latest_scan_terminal_job(
        connection: DBClient,
        prefix: str | None,
        *,
        scan_id: str | None = None,
) -> dict[str, Any] | None:
    terminal_selection = select(
        scan_runs.c.scan_id,
        scan_runs.c.started_at,
        scan_runs.c.completed_at,
        scan_runs.c.scope,
        scan_runs.c.prefixes_json,
        scan_runs.c.last_progress_at,
        scan_runs.c.file_count,
        scan_runs.c.reprobed_count,
        scan_runs.c.unchanged_count,
        scan_runs.c.status,
        scan_runs.c.error,
    ).where(scan_runs.c.status.in_(tuple(TERMINAL_SCAN_JOB_STATUSES)))
    if scan_id is not None:
        exact_row = connection.execute(
            terminal_selection
            .where(scan_runs.c.scan_id == scan_id)
            .limit(1)
        ).mappings().first()
        if exact_row is not None:
            exact_job = _scan_run_terminal_job_for_prefix(
                exact_row,
                prefix=prefix,
            )
            if exact_job is not None:
                return exact_job
        return None
    rows = connection.execute(
        terminal_selection
        .where(scan_runs.c.status != SUCCESSFUL_SCAN_RUN_STATUS)
        .order_by(
            func.coalesce(
                scan_runs.c.completed_at,
                scan_runs.c.last_progress_at,
                scan_runs.c.started_at,
            ).desc()
        )
        .limit(250)
    ).mappings().fetchall()
    for row in rows:
        terminal_job = _scan_run_terminal_job_for_prefix(
            row,
            prefix=prefix,
        )
        if terminal_job is not None:
            return terminal_job
    return None


def _scan_run_terminal_job_for_prefix(
        row: DBRow,
        *,
        prefix: str | None,
) -> dict[str, Any] | None:
    matched_prefix = _scan_run_matches_prefix(row, prefix)
    if matched_prefix is _MISSING:
        return None
    scope = str(row["scope"] or "unknown")
    job_prefix = None if scope in {"full", "unknown"} else str(matched_prefix)
    return _scan_run_terminal_job(
        row,
        prefix=prefix,
        job_prefix=job_prefix,
    )


def _scan_run_supersedes_job(
        scan_run: dict[str, Any],
        job: dict[str, Any],
        deps: JobRuntimeDeps,
) -> bool:
    job_scan_id = _scan_job_database_id(job)
    scan_run_id = str(
        scan_run.get("scan_id")
        or scan_run.get("job_id")
        or ""
    )
    if job_scan_id is not None:
        return scan_run_id == job_scan_id
    if scan_run_id == str(job.get("job_id") or ""):
        return True
    scan_started_at = deps.parse_iso(scan_run.get("started_at"))
    job_created_at = deps.parse_iso(
        job.get("created_at") or job.get("started_at")
    )
    return (
        scan_started_at is not None
        and job_created_at is not None
        and scan_started_at >= job_created_at
    )


def _scan_job_database_id(job: dict[str, Any]) -> str | None:
    value = str(job.get("scan_id") or "")
    return value or None


def _scan_job_owned_by_worker(
        job: dict[str, Any],
        *,
        scan_id: str,
) -> bool:
    explicit_scan_id = _scan_job_database_id(job)
    if explicit_scan_id is not None:
        return explicit_scan_id == scan_id
    return str(job.get("job_id") or "") == scan_id


def _scan_job_matches_database_row(
        job: dict[str, Any],
        *,
        scan_id: str,
) -> bool:
    return _scan_job_database_id(job) == scan_id


def _save_scan_job_state(
        config: MediaforceConfig,
        prefix: str | None,
        payload: dict[str, Any],
        deps: JobRuntimeDeps,
) -> None:
    with _SCAN_JOB_STATE_LOCK:
        deps.save_scan_job_state(config, prefix, payload)


def _save_worker_scan_job_state(
        config: MediaforceConfig,
        prefix: str | None,
        scan_id: str,
        payload: dict[str, Any],
        deps: JobRuntimeDeps,
) -> bool:
    with _SCAN_JOB_STATE_LOCK:
        current = deps.load_scan_job_state(config, prefix)
        if current is None or not _scan_job_owned_by_worker(
                current,
                scan_id=scan_id,
        ) or str(current.get("status") or "") not in ACTIVE_SCAN_JOB_STATUSES:
            return False
        deps.save_scan_job_state(config, prefix, payload)
        return True


def _interrupted_scan_job(
        job: dict[str, Any],
        deps: JobRuntimeDeps,
        *,
        finished_at: Any,
) -> dict[str, Any]:
    return {
        **job,
        "status": "failed",
        "finished_at": finished_at,
        "error": deps.scan_interrupted_error,
    }


def _malformed_scan_job(path: Path) -> dict[str, Any]:
    return {
        "job_id": path.stem,
        "scan_id": None,
        "status": "failed",
        "scope": "unknown",
        "prefix": None,
        "owner_pid": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "error": DEFAULT_SCAN_INTERRUPTED_ERROR,
        "stats": None,
    }


def _load_scan_job_file(path: Path) -> tuple[dict[str, Any] | None, bool]:
    if not path.exists():
        return None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _malformed_scan_job(path), True
    if not isinstance(payload, dict):
        return _malformed_scan_job(path), True
    return payload, False


def _save_scan_job_file(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_json(path, payload)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if directory_flag:
        flags |= directory_flag
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _stats_payload(stats: Any) -> dict[str, Any]:
    if isinstance(stats, dict):
        return dict(stats)
    if is_dataclass(stats) and not isinstance(stats, type):
        return dict(asdict(stats))
    return object_dict(getattr(stats, "__dict__", None))
