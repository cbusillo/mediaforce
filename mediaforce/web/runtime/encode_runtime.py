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
from mediaforce.encoding.encode_queue import RUNNABLE_ENCODE_JOB_KINDS, ensure_queue_state, list_child_encode_jobs, \
    load_encode_job, load_queue_state, persisted_encode_host_payload, save_encode_job, save_queue_state
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.quality import QualityTempCleanupError, QualityTempSetupError, quality_error_message
from mediaforce.encoding.staging import safe_unlink
from mediaforce.web.runtime.host_runtime import host_config_for_key
from mediaforce.web.runtime.worker_supervision import run_supervised_worker_loop


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
    dispatch_encode_job: Any
    active_encode_process_controllers: Any
    logger: Any
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
        select(encode_jobs.c.job_id)
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
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
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
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
        sync_encode_job_parent(connection, payload, deps)

    state = load_queue_state(connection)
    running_count = running_encode_job_count(connection)
    if running_count == 0 and (state.get("active_job_id") or state.get("stop_requested")):
        state.update({"active_job_id": None, "stop_requested": False, "updated_at": deps.now_iso()})
        save_queue_state(connection, state)
    clear_stale_encoding_items_when_idle(connection, config, deps)


def clear_stale_encoding_items_when_idle(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> int:
    if running_encode_job_count(connection) > 0:
        return 0
    stale_rows = connection.execute(
        select(
            library_items.c.id,
            library_items.c.rel_path,
            staged_artifacts.c.staging_path,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.encode_host_key,
            staged_artifacts.c.encode_host_label,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(library_items.c.status == "encoding")
        .order_by(library_items.c.updated_at.asc(), library_items.c.id.asc())
    ).mappings().fetchall()
    if not stale_rows:
        return 0

    for row in stale_rows:
        if row["promoted_at"] is not None:
            continue
        staging_path = _resolve_stale_staging_path(config, row)
        if staging_path is None:
            continue
        _remove_stale_staging_path(staging_path)
        _remove_stale_staging_path(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"))

    stale_ids = [int(row["id"]) for row in stale_rows]
    updated_at = deps.now_iso()
    connection.execute(
        delete(staged_artifacts)
        .where(staged_artifacts.c.library_item_id.in_(stale_ids))
        .where(staged_artifacts.c.promoted_at.is_(None))
    )
    connection.execute(
        update(library_items)
        .where(library_items.c.id.in_(stale_ids))
        .where(library_items.c.status == "encoding")
        .values(status="planned", updated_at=updated_at)
    )
    return len(stale_ids)


def _resolve_stale_staging_path(config: MediaforceConfig, row: dict[str, Any]) -> Path | None:
    staging_value = str(row.get("staging_path") or "").strip()
    if staging_value:
        return Path(staging_value)

    rel_path = str(row.get("rel_path") or "").strip()
    if not rel_path:
        return None

    host_key = str(row.get("encode_host_key") or row.get("encode_host_label") or "").strip()
    host_config = host_config_for_key(config, host_key) if host_key else {}
    output_suffix = str(object_dict(config.media).get("output_container") or "").strip()
    if output_suffix:
        output_suffix = f".{output_suffix.lstrip('.')}"
    else:
        output_suffix = Path(rel_path).suffix or ".mkv"
    return config.staging_root_for_host(host_config) / Path(rel_path).with_suffix(output_suffix)


def _remove_stale_staging_path(path: Path) -> None:
    if not path.exists() or path.is_dir():
        return
    try:
        safe_unlink(path)
    except OSError:
        return


def running_encode_job_count(connection: DBClient) -> int:
    return int(connection.execute(
        select(func.count())
        .select_from(encode_jobs)
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
    ).scalar_one())


def resolve_encode_job_for_display(
        connection: DBClient,
        job: dict[str, Any] | None,
        deps: EncodeQueueRuntimeDeps,
) -> dict[str, Any] | None:
    if job is None:
        return None
    if str(job.get("job_kind") or "single") != "folder":
        return job
    return aggregate_encode_parent_job(connection, job, deps)


def sync_encode_job_parent(connection: DBClient, job: dict[str, Any], deps: EncodeQueueRuntimeDeps) -> dict[str, Any] | None:
    if str(job.get("job_kind") or "single") != "shard":
        return None
    parent_job_id = str(job.get("parent_job_id") or "").strip()
    if not parent_job_id:
        return None
    parent = load_encode_job(connection, parent_job_id)
    if parent is None:
        return None
    aggregated = aggregate_encode_parent_job(connection, parent, deps)
    save_encode_job(connection, aggregated)
    return aggregated


def aggregate_encode_parent_job(
        connection: DBClient,
        job: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
) -> dict[str, Any]:
    children = list_child_encode_jobs(connection, str(job["job_id"]))
    if not children:
        return dict(job)

    running_children = [child for child in children if str(child.get("status") or "") == "running"]
    queued_children = [
        child for child in children if str(child.get("status") or "") in {"queued", "retry_backoff"}
    ]
    completed_children = [child for child in children if str(child.get("status") or "") == "completed"]
    attention_children = [child for child in children if str(child.get("status") or "") == "needs_attention"]
    stopped_children = [child for child in children if str(child.get("status") or "") == "stopped"]
    failed_children = [child for child in children if str(child.get("status") or "") == "failed"]

    if running_children:
        status = "running"
    elif attention_children:
        status = "needs_attention"
    elif any(str(child.get("status") or "") == "retry_backoff" for child in children):
        status = "retry_backoff"
    elif queued_children:
        status = "queued"
    elif len(completed_children) == len(children):
        status = "completed"
    elif stopped_children:
        status = "stopped"
    elif failed_children:
        status = "failed"
    else:
        status = str(job.get("status") or "queued")

    total_duration_seconds = 0.0
    total_item_count = 0
    completed_duration_seconds = 0.0
    completed_item_count = 0
    remaining_duration_seconds = 0.0
    aggregate_speed = 0.0
    aggregate_fps = 0.0
    recoverable_item_count = 0
    speed_reporting_children = 0
    running_progress_states: list[str] = []
    active_hosts: list[dict[str, Any]] = []
    seen_host_keys: set[str] = set()
    current_items: list[str] = []
    for child in children:
        progress = object_dict(child.get("progress"))
        child_totals = encode_job_manifest_totals(child)
        child_total_duration_seconds = float_value(
            progress.get("total_duration_seconds") or child_totals.get("total_duration_seconds")
        )
        child_total_item_count = int_value(progress.get("total_item_count") or child_totals.get("total_item_count") or child.get("item_count"))
        child_completed_duration_seconds = float_value(progress.get("overall_completed_duration_seconds"))
        if str(child.get("status") or "") == "completed":
            child_completed_duration_seconds = child_total_duration_seconds
            child_completed_item_count = child_total_item_count
        else:
            child_completed_item_count = int_value(progress.get("completed_item_count"))
        total_duration_seconds += child_total_duration_seconds
        total_item_count += child_total_item_count
        completed_duration_seconds += min(child_completed_duration_seconds, child_total_duration_seconds)
        completed_item_count += min(child_completed_item_count, child_total_item_count)
        remaining_duration_seconds += max(child_total_duration_seconds - child_completed_duration_seconds, 0.0)
        if str(child.get("status") or "") in {"needs_attention", "failed", "stopped"}:
            recoverable_item_count += child_total_item_count
        speed = float_value(progress.get("speed"))
        child_progress_state = str(progress.get("progress_state") or "").strip().lower()
        if str(child.get("status") or "") == "running" and child_progress_state:
            running_progress_states.append(child_progress_state)
        if str(child.get("status") or "") == "running" and speed > 0:
            aggregate_speed += speed
            speed_reporting_children += 1
        fps = float_value(progress.get("fps"))
        if str(child.get("status") or "") == "running" and fps > 0:
            aggregate_fps += fps
        host_payload = object_dict(child.get("host"))
        host_key = str(host_payload.get("key") or host_payload.get("host") or host_payload.get("label") or "").strip()
        if str(child.get("status") or "") == "running" and host_key and host_key not in seen_host_keys:
            seen_host_keys.add(host_key)
            active_hosts.append(host_payload)
        current_item = str(progress.get("current_item_rel_path") or "").strip()
        if str(child.get("status") or "") == "running" and current_item:
            current_items.append(current_item)

    percent_complete = (
        min(completed_duration_seconds / total_duration_seconds, 1.0) * 100.0
        if total_duration_seconds > 0
        else 0.0
    )
    eta_speed = aggregate_speed
    if 0 < speed_reporting_children < len(running_children):
        eta_speed *= len(running_children) / speed_reporting_children
    eta_seconds = (remaining_duration_seconds / eta_speed) if eta_speed > 0 else None
    progress_state = {
        "running": "running",
        "completed": "completed",
        "queued": "queued",
        "retry_backoff": "queued",
        "needs_attention": "needs_attention",
        "stopped": "stopped",
        "failed": "failed",
    }.get(status, "queued")
    phase_label = None
    if (
        status == "running"
        and running_progress_states
        and len(running_progress_states) == len(running_children)
        and all(state in {"quality_search", "starting"} for state in running_progress_states)
    ):
        if "quality_search" in running_progress_states:
            progress_state = "quality_search"
            phase_label = "Searching quality"
        else:
            progress_state = "starting"
    progress = {
        "total_item_count": total_item_count,
        "completed_item_count": min(completed_item_count, total_item_count),
        "total_duration_seconds": total_duration_seconds,
        "completed_duration_seconds": completed_duration_seconds,
        "overall_completed_duration_seconds": completed_duration_seconds,
        "remaining_duration_seconds": max(remaining_duration_seconds, 0.0),
        "percent_complete": percent_complete,
        "speed": aggregate_speed or None,
        "eta_speed": eta_speed or None,
        "fps": aggregate_fps or None,
        "eta_seconds": eta_seconds,
        "progress_state": progress_state,
        "phase_label": phase_label,
        "current_item_rel_path": current_items[0] if len(current_items) == 1 else None,
        "active_host_labels": [
            str(host.get("label") or host.get("key") or host.get("host") or "").strip()
            for host in active_hosts
            if str(host.get("label") or host.get("key") or host.get("host") or "").strip()
        ],
        "updated_at": deps.now_iso(),
    }
    waiting_reason = None
    if not running_children:
        for child in children:
            waiting_reason = str(child.get("waiting_reason") or "").strip() or None
            if waiting_reason:
                break
    terminal_reason = None
    for child in attention_children + failed_children + stopped_children:
        terminal_reason = str(child.get("terminal_reason") or child.get("last_failure_kind") or "").strip() or None
        if terminal_reason:
            break
    error = None
    for child in attention_children + failed_children + stopped_children:
        error = str(child.get("error") or "").strip() or None
        if error:
            break
    started_candidates = [
        str(child.get("started_at") or "").strip() for child in children if str(child.get("started_at") or "").strip()
    ]
    finished_candidates = [
        str(child.get("finished_at") or "").strip() for child in children if str(child.get("finished_at") or "").strip()
    ]
    aggregated = {
        **job,
        "status": status,
        # Folder aggregate rows are display-only and must not count as runnable host work.
        "host": {},
        "progress": progress,
        "waiting_reason": waiting_reason,
        "terminal_reason": terminal_reason,
        "error": error,
        "started_at": min(started_candidates) if started_candidates else job.get("started_at"),
        "finished_at": max(finished_candidates) if len(finished_candidates) == len(children) else None,
        "updated_at": deps.now_iso(),
        "active_hosts": active_hosts,
        "running_shard_count": len(running_children),
        "queued_shard_count": len(queued_children),
        "completed_shard_count": len(completed_children),
        "shard_count": len(children),
        "recoverable_item_count": recoverable_item_count,
    }
    return aggregated


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
    manifest_items = [object_dict(item) for item in object_list(payload.get("items"))]
    indexes = _manifest_indexes_for_job(job, manifest_items)
    items = [manifest_items[index] for index in indexes if 0 <= index < len(manifest_items)]
    return {
        "total_item_count": len(items) or fallback_item_count,
        "total_duration_seconds": sum(float_value(item.get("duration_seconds")) for item in items),
        "total_source_size_bytes": sum(int_value(item.get("source_size_bytes")) for item in items),
    }


def _manifest_indexes_for_job(job: dict[str, Any], manifest_items: list[dict[str, Any]]) -> list[int]:
    manifest_indexes = job.get("manifest_indexes")
    if not isinstance(manifest_indexes, list):
        return list(range(len(manifest_items)))
    indexes: list[int] = []
    for value in manifest_indexes:
        if not isinstance(value, int):
            continue
        if 0 <= value < len(manifest_items):
            indexes.append(value)
    return indexes or list(range(len(manifest_items)))


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
        _cleanup_encode_retry_artifacts(
            connection,
            manifest_path=Path(str(job["manifest_path"])),
            indexes=job.get("manifest_indexes"),
            deps=deps,
        )
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
        sync_encode_job_parent(connection, job, deps)
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
    sync_encode_job_parent(connection, job, deps)


def select_encode_host(
        connection: DBClient,
        config: MediaforceConfig,
        job: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
) -> tuple[dict[str, Any] | None, str | None]:
    library_key = _encode_job_library_key(job)
    host_rows = sorted(
        deps.host_runtime_rows(connection, config),
        key=lambda status: (-int(status["priority"]), str(status["label"])),
    )
    if library_key:
        host_rows = [host for host in host_rows if _host_allows_library(host, library_key)]
    now = datetime.now(tz=UTC)
    bypass_schedule = bool(job.get("bypass_schedule"))

    def _schedule_open(host: dict[str, Any]) -> bool:
        return deps.scheduler_allows_encode_run(
            deps.schedule_profile_policy_for_host(config, host),
            bypass_schedule=bypass_schedule,
            now=now,
            host_payload=host,
        )

    active_hosts = [
        host
        for host in host_rows
        if bool(host.get("available"))
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
        and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
        and _schedule_open(host)
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
        and not object_list(host.get("issues"))
        and bool(deps.host_lifecycle_start_command(host))
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
        and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
        and _schedule_open(host)
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
    if library_key and encode_capable_hosts:
        return None, f"waiting for a host allowed to encode {library_key}"
    return None, "waiting for an available encode host"


def _encode_job_library_key(job: dict[str, Any]) -> str:
    prefix = str(job.get("prefix") or "").strip()
    if not prefix:
        return ""
    return prefix.split("/", 1)[0].strip().lower()


def _host_allows_library(host: dict[str, Any], library_key: str) -> bool:
    allowed_libraries = host.get("allowed_libraries")
    if not isinstance(allowed_libraries, list) or not allowed_libraries:
        return True
    normalized_allowed = {str(value or "").strip().lower() for value in allowed_libraries if str(value or "").strip()}
    return library_key in normalized_allowed


def encode_job_heartbeat_loop(
        *,
        config_path: Path,
        job_id: str,
        worker_id: str,
        stop_event: threading.Event,
        process_controller: ManagedProcessController,
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
                    "process_pid": process_controller.pid,
                    "updated_at": deps.now_iso(),
                }
            )
            save_encode_job(connection, job)


def encode_queue_worker_loop(*, config_path: Path, deps: EncodeQueueRuntimeDeps) -> None:
    run_supervised_worker_loop(
        process_once_fn=lambda: process_encode_queue_once(config_path=config_path, deps=deps),
        poll_seconds=deps.encode_queue_poll_seconds,
        logger=deps.logger,
        failure_message="Encode queue worker pass failed",
    )


def process_encode_queue_once(*, config_path: Path, deps: EncodeQueueRuntimeDeps) -> None:
    config = deps.load_config(config_path)
    claimed_jobs: list[dict[str, Any]] = []
    with open_db(config.paths.db_path) as connection:
        ensure_queue_state(connection, updated_at=deps.now_iso())
        reconcile_encode_jobs(connection, config, deps)
        state = load_queue_state(connection)
        if state.get("stop_requested"):
            for controller in deps.active_encode_process_controllers():
                controller.cancel()
            if running_encode_job_count(connection) > 0:
                return
            state.update({"stop_requested": False, "active_job_id": None, "updated_at": deps.now_iso()})
            save_queue_state(connection, state)
        if state.get("is_paused"):
            return
        while True:
            next_job = claim_next_runnable_encode_job(connection, config, deps)
            if next_job is None:
                break
            claimed_jobs.append(next_job)
        if state.get("active_job_id"):
            state.update({"active_job_id": None, "updated_at": deps.now_iso()})
            save_queue_state(connection, state)

    for job in claimed_jobs:
        deps.dispatch_encode_job(config_path=config_path, job_id=str(job["job_id"]))


def claim_next_runnable_encode_job(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> dict[str, Any] | None:
    next_job = load_next_runnable_encode_job(connection, config, deps)
    if next_job is None:
        return None
    worker_id = _encode_job_worker_id()
    now_iso = deps.now_iso()
    update_result = connection.execute(
        update(encode_jobs)
        .where(encode_jobs.c.job_id == next_job["job_id"])
        .where(encode_jobs.c.status == "queued")
        .values(
            status="running",
            started_at=now_iso,
            finished_at=None,
            process_pid=None,
            leased_at=now_iso,
            heartbeat_at=now_iso,
            lease_expires_at=_encode_job_lease_expires_at(deps),
            worker_id=worker_id,
            attempt_count=int_value(next_job.get("attempt_count")) + 1,
            retry_not_before=None,
            waiting_reason=None,
            terminal_reason=None,
            last_failure_kind=None,
            progress_json=json.dumps(_initial_encode_job_progress(next_job, deps), sort_keys=True),
            updated_at=now_iso,
        )
    )
    rowcount = update_result.rowcount
    if callable(rowcount):
        claimed_rowcount = rowcount()
    elif isinstance(rowcount, int):
        claimed_rowcount = rowcount
    else:
        claimed_rowcount = 0
    if claimed_rowcount != 1:
        return None
    claimed_job = load_encode_job(connection, str(next_job["job_id"]))
    if claimed_job is None:
        return None
    sync_encode_job_parent(connection, claimed_job, deps)
    return claimed_job


def load_next_runnable_encode_job(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> dict[str, Any] | None:
    rows = connection.execute(
        select(encode_jobs.c.job_id)
        .where(encode_jobs.c.status == "queued")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
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
                sync_encode_job_parent(connection, job, deps)
            continue
        persisted_host_payload = persisted_encode_host_payload(host_payload)
        if job.get("waiting_reason") or job.get("host") != persisted_host_payload:
            job.update({"waiting_reason": None, "host": persisted_host_payload, "updated_at": deps.now_iso()})
            save_encode_job(connection, job)
            sync_encode_job_parent(connection, job, deps)
        return job
    return None


def run_encode_job(
        *,
        config_path: Path,
        job_id: str,
        process_controller: ManagedProcessController,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    config = deps.load_config(config_path)
    process_controller.reset()
    with open_db(config.paths.db_path) as connection:
        job = load_encode_job(connection, job_id)
        if job is None:
            return
        manifest_path = Path(job["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        manifest_items = [object_dict(item) for item in object_list(manifest.get("items"))]
        indexes = _manifest_indexes_for_job(job, manifest_items)
        job.update({"process_pid": process_controller.pid, "updated_at": deps.now_iso()})
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
            "process_controller": process_controller,
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
        process_controller.throw_if_cancelled()
        started_host_for_job = deps.ensure_encode_host_ready(config, job.get("host"))
        with open_db(config.paths.db_path) as connection:
            deps.encode_manifest_items(
                connection,
                config,
                manifest_path,
                manifest,
                indexes,
                overwrite=False,
                process_controller=process_controller,
                host=job.get("host"),
                progress_callback=report_progress,
                encode_context={
                    "origin": "queue",
                    "encode_job_id": job_id,
                    "encode_worker_id": worker_id,
                },
            )
        final_status = "completed"
    except ProcessCancelledError:
        final_status = "stopped"
        error = "Encode queue job was stopped and cleaned up."
    except Exception as exc:
        failure_kind = _classify_encode_failure(exc, job)
        error = quality_error_message(exc)
    finally:
        if started_host_for_job and not _host_has_other_running_jobs(config, job_id, job.get("host")):
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
                    sync_encode_job_parent(connection, job, deps)
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
            if running_encode_job_count(connection) == 0:
                state.update({"active_job_id": None, "stop_requested": False, "updated_at": deps.now_iso()})
                save_queue_state(connection, state)
        process_controller.reset()


def _encode_job_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


def _encode_job_lease_expires_at(deps: EncodeQueueRuntimeDeps) -> str:
    return (datetime.now(tz=UTC) + timedelta(seconds=deps.encode_job_lease_seconds)).isoformat(timespec="seconds")


def _encode_job_retry_delay_seconds(attempt_count: int, deps: EncodeQueueRuntimeDeps) -> int:
    exponent = max(attempt_count - 1, 0)
    delay = deps.encode_job_retry_base_delay_seconds * (2 ** exponent)
    return min(delay, deps.encode_job_retry_max_delay_seconds)


def _host_has_other_running_jobs(config: MediaforceConfig, job_id: str, host_payload: dict[str, Any] | None) -> bool:
    target_host = object_dict(host_payload)
    target_key = str(target_host.get("key") or target_host.get("host") or target_host.get("label") or "").strip()
    if not target_key:
        return False
    with open_db(config.paths.db_path) as connection:
        rows = connection.execute(
            select(encode_jobs.c.job_id, encode_jobs.c.host_json)
            .where(encode_jobs.c.status == "running")
            .where(encode_jobs.c.job_id != job_id)
            .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        ).mappings().fetchall()
    for row in rows:
        host = object_dict(json.loads(str(row["host_json"] or "{}")))
        host_key = str(host.get("key") or host.get("host") or host.get("label") or "").strip()
        if host_key == target_key:
            return True
    return False


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
        sync_encode_job_parent(connection, job, deps)


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


def prepare_terminal_encode_job_for_requeue(
        connection: DBClient,
        job: dict[str, Any],
        *,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    if str(job.get("status") or "") not in {"needs_attention", "failed", "stopped"}:
        return
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not str(manifest_path):
        return
    selected_indexes = job.get("manifest_indexes")
    if str(job.get("job_kind") or "") == "folder":
        child_indexes: list[int] = []
        for child in list_child_encode_jobs(connection, str(job.get("job_id") or "")):
            if str(child.get("status") or "") == "completed":
                continue
            for index in object_list(child.get("manifest_indexes")):
                if isinstance(index, int):
                    child_indexes.append(index)
        if child_indexes:
            selected_indexes = sorted(set(child_indexes))

    # Legacy and malformed folder jobs may have no child rows or no non-completed child
    # manifest indexes; in those cases, fallback to the job-level indexes to avoid
    # skipping retry cleanup and item resets.
    _cleanup_encode_retry_artifacts(
        connection,
        manifest_path=manifest_path,
        indexes=selected_indexes,
        deps=deps,
    )


def _cleanup_encode_retry_artifacts(
        connection: DBClient,
        *,
        manifest_path: Path,
        indexes: list[int] | None = None,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    now_iso = deps.now_iso()
    manifest_items = [object_dict(item) for item in object_list(manifest.get("items"))]
    selected_indexes = indexes if isinstance(indexes, list) else list(range(len(manifest_items)))
    for index in selected_indexes:
        if not isinstance(index, int) or index < 0 or index >= len(manifest_items):
            continue
        item = manifest_items[index]
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
    if isinstance(exc, (QualityTempCleanupError, QualityTempSetupError)):
        return "deterministic"
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
        safe_unlink(path)
    except OSError:
        return
