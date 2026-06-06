import errno
import json
import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import true
from sqlalchemy import update

from mediaforce.tuning.calibration_jobs import claim_next_queued_calibration_job, load_latest_failed_sample_job, \
    load_latest_job, load_latest_overlapping_job, load_latest_retryable_sample_job, load_latest_sample_job, \
    queue_position, save_job
from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.library.scanner import scan_library
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.core.type_defs import JSONValue, object_dict
from mediaforce.web.runtime.worker_supervision import run_supervised_worker_loop

_MISSING = object()


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


@dataclass(slots=True)
class CalibrationQueueRuntimeDeps:
    now_iso: Any
    run_calibration_job: Any
    job_seed_metadata: Any
    save_job_state: Any
    mark_calibration_submission_complete: Any
    register_calibration_process_controller: Any
    unregister_calibration_process_controller: Any
    submission_cleanup_callback: Callable[[str], Callable[[Future[object]], None]]
    calibration_submissions: set[str]
    calibration_submissions_lock: Any
    calibration_executors: dict[str, ThreadPoolExecutor]
    sample_calibration_concurrency: int
    full_calibration_concurrency: int
    calibration_queue_poll_seconds: float


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
    payload = load_latest_overlapping_job(connection, prefix)
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
    path = scan_job_file(config, prefix)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_scan_job_state(
        config: MediaforceConfig,
        prefix: str | None,
        payload: dict[str, Any],
        scan_job_file: Any,
) -> None:
    scan_job_file(config, prefix).write_text(json.dumps(payload, indent=2) + "\n")


def maybe_schedule_scan(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
) -> dict[str, Any] | None:
    active_scan = active_scan_from_db(connection, config, prefix, deps)
    if active_scan is not None:
        return active_scan
    if prefix is not None:
        full_job = deps.load_scan_job_state(config, None)
        if full_job and full_job.get("status") in {"queued", "running"} and not scan_job_belongs_to_current_process(
                full_job,
                deps.scan_process_is_alive,
        ):
            full_job = _expire_scan_job(config, None, full_job, deps)
        if full_job and full_job.get("status") in {"queued", "running"}:
            return full_job
    job = deps.load_scan_job_state(config, prefix)
    if job and job.get("status") in {"queued", "running"} and not scan_job_belongs_to_current_process(
            job,
            deps.scan_process_is_alive,
    ):
        job = _expire_scan_job(config, prefix, job, deps)
    if job and job.get("status") in {"queued", "running"}:
        return job
    if not scan_is_stale(connection, config, prefix, deps):
        return job
    if job and job.get("status") == "failed":
        finished_at = deps.parse_iso(job.get("finished_at") or job.get("started_at"))
        interrupted_restart = str(job.get("error") or "") == deps.scan_interrupted_error
        if not interrupted_restart and finished_at and datetime.now(tz=UTC) - finished_at < deps.scan_retry_cooldown:
            return job

    job_payload = {
        "job_id": uuid.uuid4().hex[:12],
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
    deps.save_scan_job_state(config, prefix, job_payload)
    thread = threading.Thread(
        target=deps.run_scan_job,
        kwargs={
            "config_path": config.paths.config_path,
            "prefix": prefix,
            "job_id": str(job_payload["job_id"]),
        },
        daemon=True,
    )
    thread.start()
    return job_payload


def run_scan_job(
        *,
        config_path: Any,
        prefix: str | None,
        job_id: str,
        deps: JobRuntimeDeps,
) -> None:
    config = load_config(config_path)
    purge_transient_artifacts(config, force=True)
    job = deps.load_scan_job_state(config, prefix) or {}
    job.update({"status": "running", "started_at": deps.now_iso(), "finished_at": None, "error": None})
    deps.save_scan_job_state(config, prefix, job)

    try:
        with open_db(config.paths.db_path) as connection:
            stats = scan_library(connection, config, prefixes=[prefix] if prefix else None)
        if prefix is None:
            deps.save_catalog_signature(config)
            deps.reset_folder_card_cache()
        deps.save_scan_job_state(
            config,
            prefix,
            {
                **job,
                "job_id": job_id,
                "status": "completed",
                "finished_at": deps.now_iso(),
                "error": None,
                "stats": _stats_payload(stats),
            },
        )
    except Exception as exc:
        deps.save_scan_job_state(
            config,
            prefix,
            {
                **job,
                "job_id": job_id,
                "status": "failed",
                "finished_at": deps.now_iso(),
                "error": str(exc),
                "stats": None,
            },
        )
    finally:
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
    future.add_done_callback(deps.submission_cleanup_callback(job_id))


def calibration_queue_worker_loop(
        *,
        config_path: Any,
        deps: CalibrationQueueRuntimeDeps,
        logger: Any,
) -> None:
    run_supervised_worker_loop(
        process_once_fn=lambda: process_calibration_queue_once(config_path=config_path, deps=deps),
        poll_seconds=deps.calibration_queue_poll_seconds,
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
            .where(calibration_jobs.c.status.in_(("running", "pending_review")))
            .order_by(calibration_jobs.c.created_at, literal_column("rowid"))
        ).mappings().fetchall()
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

    item_count = int(
        connection.execute(
            select(func.count())
            .select_from(library_items)
            .where(_prefix_filter(prefix))
        ).scalar_one()
    )
    if item_count == 0:
        return True
    latest = latest_scan_completed_at(connection, prefix=prefix)
    if latest is None:
        return True
    return datetime.now(tz=UTC) - latest > deps.prefix_scan_stale_after


def _prefix_filter(prefix: str) -> Any:
    normalized_prefix = prefix.strip().strip("/")
    if not normalized_prefix:
        return true()
    return or_(
        library_items.c.rel_path == normalized_prefix,
        library_items.c.rel_path.like(f"{_sql_like_escape(normalized_prefix)}/%", escape="\\"),
    )


def _sql_like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def latest_scan_completed_at(connection: DBClient, prefix: str | None) -> datetime | None:
    rows = connection.execute(
        select(
            scan_runs.c.completed_at,
            scan_runs.c.started_at,
            scan_runs.c.scope,
            scan_runs.c.prefixes_json,
        )
        .order_by(scan_runs.c.started_at.desc())
        .limit(250)
    ).mappings().fetchall()
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


def active_scan_from_db(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str | None,
        deps: JobRuntimeDeps,
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
        )
        .where(scan_runs.c.completed_at.is_(None))
        .order_by(scan_runs.c.started_at.desc())
        .limit(25)
    ).mappings().fetchall()
    for row in rows:
        matched_prefix = _scan_run_matches_prefix(row, prefix)
        if matched_prefix is _MISSING:
            continue

        scope = str(row["scope"] or "unknown")
        job_prefix = None if scope in {"full", "unknown"} else str(matched_prefix)
        job = deps.load_scan_job_state(config, job_prefix)
        if job and job.get("status") in {"queued", "running"} and not deps.scan_process_is_alive(job.get("owner_pid")):
            job = _expire_scan_job(config, job_prefix, job, deps)

        if deps.scan_process_is_alive(row["owner_pid"]):
            return {
                "job_id": str(row["scan_id"]),
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

        if job and job.get("status") in {"queued", "running"}:
            return {
                "job_id": str(job.get("job_id") or row["scan_id"]),
                "status": str(job.get("status") or "running"),
                "scope": str(job.get("scope") or scope),
                "prefix": job.get("prefix") if prefix is not None else None,
                "created_at": job.get("created_at") or row["started_at"],
                "started_at": job.get("started_at") or row["started_at"],
                "finished_at": job.get("finished_at"),
                "error": job.get("error"),
                "stats": job.get("stats"),
            }

        if job is not None:
            _expire_scan_run(connection, str(row["scan_id"]))
    return None


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
        if normalized and prefix.startswith(normalized):
            return normalized
    return _MISSING


def _expire_scan_run(connection: DBClient, scan_id: str) -> None:
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(completed_at=func.coalesce(scan_runs.c.completed_at, _now_iso()))
    )
    connection.commit()


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
) -> dict[str, Any]:
    expired = {
        **job,
        "status": "failed",
        "finished_at": deps.now_iso(),
        "error": deps.scan_interrupted_error,
    }
    deps.save_scan_job_state(config, prefix, expired)
    return expired


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _stats_payload(stats: Any) -> dict[str, Any]:
    if isinstance(stats, dict):
        return dict(stats)
    if is_dataclass(stats) and not isinstance(stats, type):
        return dict(asdict(stats))
    return object_dict(getattr(stats, "__dict__", None))
