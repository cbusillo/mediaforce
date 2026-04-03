import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.encoding.encode_queue import ensure_queue_state, load_active_encode_job, load_encode_job, load_queue_state, \
    save_encode_job, save_queue_state
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list


@dataclass(slots=True)
class EncodeQueueRuntimeDeps:
    load_config: Any
    now_iso: Any
    parse_iso: Any
    host_runtime_rows: Any
    schedule_profile_policy_for_host: Any
    scheduler_allows_encode_run: Any
    host_lifecycle_start_command: Any
    ensure_encode_host_ready: Any
    stop_encode_host_if_configured: Any
    encode_manifest_items: Any
    logger: Any
    encode_queue_process: ManagedProcessController
    encode_queue_poll_seconds: float
    encode_job_lease_seconds: int
    encode_job_heartbeat_seconds: float
    encode_job_progress_write_interval_seconds: float
    encode_job_retry_base_delay_seconds: int
    encode_job_retry_max_delay_seconds: int
    encode_job_max_attempts: int
    encode_host_cooldown_seconds: int


def recover_encode_queue(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    reconcile_encode_jobs(connection, config, deps, restart_recovery=True)


def reconcile_encode_jobs(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
        *,
        restart_recovery: bool = False,
) -> None:
    now = datetime.now(tz=UTC)
    running_rows = connection.execute(
        select(encode_jobs.c.job_id).where(encode_jobs.c.status == "running")
    ).mappings().fetchall()
    for row in running_rows:
        payload = load_encode_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        lease_expires_at = deps.parse_iso(payload.get("lease_expires_at"))
        if not restart_recovery and lease_expires_at is not None and lease_expires_at > now:
            continue
        failure_kind = "worker_restart" if restart_recovery else "stale_lease"
        failure_message = (
            "Encode queue job was interrupted by a web process restart."
            if restart_recovery
            else "Encode queue job stopped heartbeating and was reclaimed for retry."
        )
        transition_encode_job_failure(
            connection,
            config,
            payload,
            deps,
            failure_kind=failure_kind,
            error_message=failure_message,
        )

    retry_backoff_rows = connection.execute(
        select(encode_jobs.c.job_id)
        .where(encode_jobs.c.status == "retry_backoff")
        .order_by(encode_jobs.c.created_at, literal_column("rowid"))
    ).mappings().fetchall()
    for row in retry_backoff_rows:
        payload = load_encode_job(connection, str(row["job_id"]))
        if payload is None:
            continue
        retry_not_before = deps.parse_iso(payload.get("retry_not_before"))
        if retry_not_before is not None and retry_not_before > now:
            continue
        payload.update(
            {
                "status": "queued",
                "retry_not_before": None,
                "waiting_reason": None,
                "updated_at": deps.now_iso(),
            }
        )
        save_encode_job(connection, payload)

    state = load_queue_state(connection)
    running_count = int(
        connection.execute(
            select(func.count()).select_from(encode_jobs).where(encode_jobs.c.status == "running")
        ).scalar_one()
    )
    if running_count == 0 and (state.get("active_job_id") or state.get("stop_requested")):
        state.update({"active_job_id": None, "stop_requested": False, "updated_at": deps.now_iso()})
        save_queue_state(connection, state)


def encode_job_manifest_totals(job: dict[str, Any]) -> dict[str, Any]:
    manifest_path = Path(str(job.get("manifest_path") or "")).expanduser()
    fallback_item_count = int_value(job.get("item_count"))
    if not manifest_path.exists():
        return {
            "total_item_count": fallback_item_count,
            "total_duration_seconds": 0.0,
            "total_source_size_bytes": 0,
        }
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "total_item_count": fallback_item_count,
            "total_duration_seconds": 0.0,
            "total_source_size_bytes": 0,
        }
    items = [object_dict(item) for item in object_list(payload.get("items"))]
    return {
        "total_item_count": len(items) or fallback_item_count,
        "total_duration_seconds": sum(float_value(item.get("duration_seconds")) for item in items),
        "total_source_size_bytes": sum(int_value(item.get("source_size_bytes")) for item in items),
    }


def transition_encode_job_failure(
        connection: DBClient,
        config: MediaforceConfig,
        job: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
        *,
        failure_kind: str,
        error_message: str,
) -> None:
    _ = config
    now = datetime.now(tz=UTC)
    now_iso = now.isoformat(timespec="seconds")
    assigned_host = object_dict(job.get("host"))
    attempt_count = int_value(job.get("attempt_count"))
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
            "progress": _finalize_encode_job_progress(job, deps=deps, terminal_state="needs_attention"),
            "updated_at": now_iso,
        }
    )

    if retryable and attempt_count < deps.encode_job_max_attempts:
        _cleanup_encode_retry_artifacts(connection, manifest_path=Path(str(job["manifest_path"])), deps=deps)
        retry_delay = _encode_job_retry_delay_seconds(attempt_count, deps)
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
                    (now + timedelta(seconds=deps.encode_host_cooldown_seconds)).isoformat(timespec="seconds")
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


def select_encode_host(
        connection: DBClient,
        config: MediaforceConfig,
        job: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
) -> tuple[dict[str, Any] | None, str | None]:
    host_rows = sorted(
        deps.host_runtime_rows(connection, config),
        key=lambda status: (-int(status["priority"]), str(status["label"])),
    )
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
            or deps.scheduler_allows_encode_run(
                deps.schedule_profile_policy_for_host(config, host),
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
        and bool(deps.host_lifecycle_start_command(host))
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
        and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
        and (
            bypass_schedule
            or deps.scheduler_allows_encode_run(
                deps.schedule_profile_policy_for_host(config, host),
                now=now,
                host_payload=host,
            )
        )
    ]
    if not encode_capable_hosts and not startable_hosts:
        return None, "waiting for an available encode host"

    cooldown_until = deps.parse_iso(job.get("host_cooldown_until"))
    last_host = object_dict(job.get("last_host"))
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
            return object_dict(eligible_active_hosts[0]), None
        eligible_startable_hosts = [
            host
            for host in startable_hosts
            if str(host.get("key") or "") not in blocked_keys and str(host.get("label") or "") not in blocked_keys
        ]
        if eligible_startable_hosts:
            return object_dict(eligible_startable_hosts[0]), None
        host_name = str(last_host.get("label") or last_host.get("key") or "the last host")
        return None, f"waiting for host cooldown to expire on {host_name}"
    if active_hosts:
        return object_dict(active_hosts[0]), None
    if startable_hosts:
        return object_dict(startable_hosts[0]), None
    if any(
            int(host.get("active_encode_count") or 0) >= int(host.get("max_parallel_encodes") or 1)
            for host in encode_capable_hosts
    ):
        return None, "waiting for host capacity to free up"
    if any(
            not deps.scheduler_allows_encode_run(
                deps.schedule_profile_policy_for_host(config, host),
                now=now,
                host_payload=host,
            )
            for host in encode_capable_hosts
    ):
        return None, "waiting for a host schedule window"
    return None, "waiting for an available encode host"


def encode_job_heartbeat_loop(
        *,
        config_path: Path,
        job_id: str,
        worker_id: str,
        stop_event: threading.Event,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    while not stop_event.wait(deps.encode_job_heartbeat_seconds):
        with open_db(deps.load_config(config_path).paths.db_path) as connection:
            job = load_encode_job(connection, job_id)
            if job is None or str(job.get("status") or "") != "running":
                return
            if str(job.get("worker_id") or "") != worker_id:
                return
            job.update(
                {
                    "heartbeat_at": deps.now_iso(),
                    "lease_expires_at": _encode_job_lease_expires_at(deps),
                    "process_pid": deps.encode_queue_process.pid,
                    "updated_at": deps.now_iso(),
                }
            )
            save_encode_job(connection, job)


def encode_queue_worker_loop(*, config_path: Path, deps: EncodeQueueRuntimeDeps) -> None:
    while True:
        try:
            process_encode_queue_once(config_path=config_path, deps=deps)
        except Exception:
            pass
        threading.Event().wait(deps.encode_queue_poll_seconds)


def process_encode_queue_once(*, config_path: Path, deps: EncodeQueueRuntimeDeps) -> None:
    config = deps.load_config(config_path)
    with open_db(config.paths.db_path) as connection:
        ensure_queue_state(connection, updated_at=deps.now_iso())
        reconcile_encode_jobs(connection, config, deps)
        state = load_queue_state(connection)
        running_job = load_active_encode_job(connection)
        if running_job is not None:
            if state.get("stop_requested"):
                deps.encode_queue_process.cancel()
            return
        if state.get("stop_requested"):
            state.update({"stop_requested": False, "active_job_id": None, "updated_at": deps.now_iso()})
            save_queue_state(connection, state)
        if state.get("is_paused"):
            return
        next_job = load_next_runnable_encode_job(connection, config, deps)
        if next_job is None:
            return
        worker_id = _encode_job_worker_id()
        now_iso = deps.now_iso()
        state.update({"active_job_id": next_job["job_id"], "updated_at": deps.now_iso()})
        save_queue_state(connection, state)
        next_job.update(
            {
                "status": "running",
                "started_at": now_iso,
                "finished_at": None,
                "process_pid": None,
                "leased_at": now_iso,
                "heartbeat_at": now_iso,
                "lease_expires_at": _encode_job_lease_expires_at(deps),
                "worker_id": worker_id,
                "attempt_count": int_value(next_job.get("attempt_count")) + 1,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": None,
                "last_failure_kind": None,
                "progress": _initial_encode_job_progress(next_job, deps),
                "updated_at": now_iso,
            }
        )
        save_encode_job(connection, next_job)

    run_encode_job(config_path=config_path, job_id=str(next_job["job_id"]), deps=deps)


def load_next_runnable_encode_job(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> dict[str, Any] | None:
    rows = connection.execute(
        select(encode_jobs.c.job_id)
        .where(encode_jobs.c.status == "queued")
        .order_by(encode_jobs.c.created_at, literal_column("rowid"))
    ).mappings().fetchall()
    for row in rows:
        job = load_encode_job(connection, str(row["job_id"]))
        if job is None:
            continue
        host_payload, waiting_reason = select_encode_host(connection, config, job, deps)
        if host_payload is None:
            if str(job.get("waiting_reason") or "") != str(waiting_reason or ""):
                job.update({"waiting_reason": waiting_reason, "updated_at": deps.now_iso()})
                save_encode_job(connection, job)
            continue
        if job.get("waiting_reason") or job.get("host") != host_payload:
            job.update({"waiting_reason": None, "host": host_payload, "updated_at": deps.now_iso()})
            save_encode_job(connection, job)
        return job
    return None


def run_encode_job(*, config_path: Path, job_id: str, deps: EncodeQueueRuntimeDeps) -> None:
    config = deps.load_config(config_path)
    deps.encode_queue_process.reset()
    with open_db(config.paths.db_path) as connection:
        job = load_encode_job(connection, job_id)
        if job is None:
            return
        manifest_path = Path(job["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        indexes = list(range(len(manifest.get("items") or [])))
        job.update({"process_pid": deps.encode_queue_process.pid, "updated_at": deps.now_iso()})
        save_encode_job(connection, job)

    progress_write_lock = threading.Lock()
    last_progress_write = 0.0

    def report_progress(progress: dict[str, Any]) -> None:
        nonlocal last_progress_write
        now_monotonic = time.monotonic()
        progress_state = str(progress.get("progress_state") or "")
        if progress_state != "end" and (
                now_monotonic - last_progress_write) < deps.encode_job_progress_write_interval_seconds:
            return
        with progress_write_lock:
            if progress_state != "end" and (
                    now_monotonic - last_progress_write) < deps.encode_job_progress_write_interval_seconds:
                return
            last_progress_write = now_monotonic
        _persist_encode_job_progress(config_path, job_id, progress, deps)

    heartbeat_stop = threading.Event()
    worker_id = str(job.get("worker_id") or _encode_job_worker_id())
    heartbeat_thread = threading.Thread(
        target=encode_job_heartbeat_loop,
        kwargs={
            "config_path": config_path,
            "job_id": job_id,
            "worker_id": worker_id,
            "stop_event": heartbeat_stop,
            "deps": deps,
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
        deps.encode_queue_process.throw_if_cancelled()
        started_host_for_job = deps.ensure_encode_host_ready(config, job.get("host"))
        with open_db(config.paths.db_path) as connection:
            deps.encode_manifest_items(
                connection,
                config,
                manifest_path,
                manifest,
                indexes,
                overwrite=False,
                process_controller=deps.encode_queue_process,
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
                deps.stop_encode_host_if_configured(config, job.get("host"))
            except Exception as exc:
                deps.logger.warning("Encode host stop command failed for %s: %s", job_id, exc)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        with open_db(config.paths.db_path) as connection:
            job = load_encode_job(connection, job_id)
            if job is not None:
                if final_status is not None:
                    job.update(
                        {
                            "status": final_status,
                            "finished_at": deps.now_iso(),
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
                            "progress": _finalize_encode_job_progress(job, deps=deps, terminal_state=final_status),
                            "updated_at": deps.now_iso(),
                        }
                    )
                    save_encode_job(connection, job)
                elif error is not None:
                    if failure_kind is None:
                        effective_failure_kind = "deterministic"
                    else:
                        effective_failure_kind = failure_kind
                    transition_encode_job_failure(
                        connection,
                        config,
                        job,
                        deps,
                        failure_kind=effective_failure_kind,
                        error_message=error,
                    )
            state = load_queue_state(connection)
            state.update({"active_job_id": None, "stop_requested": False, "updated_at": deps.now_iso()})
            save_queue_state(connection, state)
        deps.encode_queue_process.reset()


def _encode_job_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


def _encode_job_lease_expires_at(deps: EncodeQueueRuntimeDeps) -> str:
    return (datetime.now(tz=UTC) + timedelta(seconds=deps.encode_job_lease_seconds)).isoformat(timespec="seconds")


def _encode_job_retry_delay_seconds(attempt_count: int, deps: EncodeQueueRuntimeDeps) -> int:
    exponent = max(attempt_count - 1, 0)
    delay = deps.encode_job_retry_base_delay_seconds * (2 ** exponent)
    return min(delay, deps.encode_job_retry_max_delay_seconds)


def _initial_encode_job_progress(job: dict[str, Any], deps: EncodeQueueRuntimeDeps) -> dict[str, Any]:
    manifest_totals = encode_job_manifest_totals(job)
    return {
        **manifest_totals,
        "completed_item_count": 0,
        "completed_duration_seconds": 0.0,
        "overall_completed_duration_seconds": 0.0,
        "remaining_duration_seconds": float_value(manifest_totals.get("total_duration_seconds")),
        "percent_complete": 0.0,
        "progress_state": "starting",
        "fps": None,
        "speed": None,
        "eta_seconds": None,
        "elapsed_seconds": 0.0,
        "out_time_seconds": 0.0,
        "updated_at": deps.now_iso(),
    }


def _persist_encode_job_progress(
        config_path: Path,
        job_id: str,
        progress: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
) -> None:
    with open_db(deps.load_config(config_path).paths.db_path) as connection:
        job = load_encode_job(connection, job_id)
        if job is None or str(job.get("status") or "") != "running":
            return
        job.update({"progress": {**progress, "updated_at": deps.now_iso()}, "updated_at": deps.now_iso()})
        save_encode_job(connection, job)


def _finalize_encode_job_progress(
        job: dict[str, Any],
        *,
        deps: EncodeQueueRuntimeDeps,
        terminal_state: str,
) -> dict[str, Any] | None:
    progress = object_dict(job.get("progress"))
    if not progress:
        return None
    total_duration_seconds = float_value(progress.get("total_duration_seconds"))
    total_item_count = int_value(progress.get("total_item_count") or job.get("item_count"))
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
                "updated_at": deps.now_iso(),
            }
        )
        return progress
    progress.update({"progress_state": terminal_state, "updated_at": deps.now_iso()})
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


def _cleanup_encode_retry_artifacts(
        connection: DBClient,
        *,
        manifest_path: Path,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    _ = deps
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    now_iso = deps.now_iso()
    for item in object_list(manifest.get("items")):
        staging_value = item.get("staging_path")
        if staging_value:
            staging_path = Path(str(staging_value))
            _remove_path(staging_path)
            _remove_path(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"))
        library_item_id = item.get("library_item_id")
        if library_item_id is None:
            continue
        stage_row = connection.execute(
            select(staged_artifacts.c.promoted_at).where(staged_artifacts.c.library_item_id == library_item_id)
        ).mappings().fetchone()
        if stage_row is not None and not stage_row["promoted_at"]:
            connection.execute(delete(staged_artifacts).where(staged_artifacts.c.library_item_id == library_item_id))
        connection.execute(
            update(library_items)
            .where(library_items.c.id == library_item_id, library_items.c.status != "promoted")
            .values(
                status=case((library_items.c.status == "promoted", library_items.c.status), else_="planned"),
                updated_at=now_iso,
            )
        )


def _classify_encode_failure(exc: Exception, job: dict[str, Any]) -> str:
    message = str(exc).lower()
    host_payload = object_dict(job.get("host"))
    if _encode_failure_is_host_related("ssh_transport", message, host_payload):
        return "ssh_transport"
    if "staging file already exists" in message:
        return "deterministic"
    return "deterministic"


def _remove_path(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    if path.is_dir():
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
