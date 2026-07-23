import json
import os
import shlex
import socket
import threading
import time
import fcntl
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
from mediaforce.core.db_tables import item_events
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.encoding.encode_queue import RUNNABLE_ENCODE_JOB_KINDS, ensure_queue_state, list_child_encode_jobs, \
    load_encode_job, load_queue_state, persisted_encode_host_payload, save_encode_job, save_queue_state
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.quality import QualitySearchError, QualityTempCleanupError, QualityTempSetupError, \
    analyze_quality_policy_failure, quality_error_message
from mediaforce.encoding.staging import safe_unlink
from mediaforce.remote import HostReadinessError, execution_mode_for_host, host_media_access_for_host, run_remote_command
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


ENCODE_HOST_BACKUP_FAILURE_THRESHOLD = 2


def recover_encode_queue(
        connection: DBClient,
        config: MediaforceConfig,
        deps: EncodeQueueRuntimeDeps,
) -> None:
    connection.commit()
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
        payload = _claim_stale_encode_job(
            connection,
            str(row["job_id"]),
            deps,
            restart_recovery=restart_recovery,
        )
        if payload is None:
            continue
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
        manifest_path = str(payload.get("manifest_path") or "").strip()
        if manifest_path:
            connection.commit()
            _cleanup_encode_retry_artifacts(
                connection,
                manifest_path=Path(manifest_path),
                indexes=payload.get("manifest_indexes"),
                deps=deps,
            )
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
    connection.commit()
    clear_stale_encoding_items_when_idle(connection, config, deps)
    connection.commit()


def _claim_stale_encode_job(
        connection: DBClient,
        job_id: str,
        deps: EncodeQueueRuntimeDeps,
        *,
        restart_recovery: bool,
) -> dict[str, Any] | None:
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    payload = load_encode_job(connection, job_id)
    if payload is None or str(payload.get("status") or "") != "running":
        connection.rollback()
        return None
    lease_expires_at = deps.parse_iso(payload.get("lease_expires_at"))
    if not restart_recovery and lease_expires_at is not None and lease_expires_at > datetime.now(tz=UTC):
        connection.rollback()
        return None
    return payload


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

    stale_ids: list[int] = []
    for row in stale_rows:
        if row["promoted_at"] is not None:
            continue
        item_id = int(row["id"])
        if _active_standalone_cli_encode(connection, item_id):
            continue
        for staging_path, host in _candidate_stale_staging_targets(config, row):
            _remove_stale_staging_path(staging_path, host=host)
            _remove_stale_staging_path(
                staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"),
                host=host,
            )
        stale_ids.append(item_id)

    if not stale_ids:
        return 0
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


def _active_standalone_cli_encode(connection: DBClient, library_item_id: int) -> bool:
    details_rows = connection.execute(
        select(item_events.c.details_json)
        .where(item_events.c.library_item_id == library_item_id)
        .where(item_events.c.event_type == "encoding_started")
        .order_by(item_events.c.id.desc())
    ).scalars().all()
    for details_json in details_rows:
        try:
            details = json.loads(str(details_json))
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict) or str(details.get("encode_origin") or "") != "cli":
            continue
        if _process_is_running(int_value(details.get("encode_owner_pid"))):
            return True
    return False


def _process_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _candidate_stale_staging_targets(
        config: MediaforceConfig,
        row: Mapping[str, Any],
) -> list[tuple[Path, dict[str, Any] | None]]:
    targets: list[tuple[Path, dict[str, Any] | None]] = []

    def _add_target(path: Path | None, host: dict[str, Any] | None = None) -> None:
        if path is None:
            return
        normalized_host = object_dict(host)
        host_key = str(
            normalized_host.get("host")
            or normalized_host.get("key")
            or normalized_host.get("label")
            or ""
        ).strip()
        if any(existing == path and existing_host_key == host_key for existing, existing_host_key in (
            (candidate_path, str(object_dict(candidate_host).get("host") or object_dict(candidate_host).get("key") or object_dict(candidate_host).get("label") or "").strip())
            for candidate_path, candidate_host in targets
        )):
            return
        targets.append((path, normalized_host or None))

    staging_value = str(row.get("staging_path") or "").strip()
    if staging_value:
        host_key = str(row.get("encode_host_key") or row.get("encode_host_label") or "").strip()
        host_config = host_config_for_key(config, host_key) if host_key else {}
        _add_target(Path(staging_value), host_config or None)

    rel_path = str(row.get("rel_path") or "").strip()
    if not rel_path:
        return targets

    output_suffix = str(object_dict(config.media).get("output_container") or "").strip()
    if output_suffix:
        output_suffix = f".{output_suffix.lstrip('.')}"
    else:
        output_suffix = Path(rel_path).suffix or ".mkv"

    rel_output_path = Path(rel_path).with_suffix(output_suffix)
    host_key = str(row.get("encode_host_key") or row.get("encode_host_label") or "").strip()
    if host_key:
        host_config = host_config_for_key(config, host_key)
        _add_target(config.staging_root_for_host(host_config) / rel_output_path, host_config)

    _add_target(config.staging_root / rel_output_path)
    for host in config.remote_hosts:
        _add_target(config.staging_root_for_host(host) / rel_output_path, host if isinstance(host, dict) else None)
    return targets


def _remove_stale_staging_path(path: Path, *, host: dict[str, Any] | None = None) -> None:
    host_payload = object_dict(host)
    if (
            host_payload
            and execution_mode_for_host(host_payload) == "ssh"
            and host_media_access_for_host(host_payload) != "stream"
    ):
        _remove_remote_stale_staging_path(path, host_payload)
        return
    if path.is_dir():
        return
    if path.exists():
        try:
            safe_unlink(path)
        except OSError:
            pass
    _prune_empty_quality_temp_dir(path.parent)


def _prune_empty_quality_temp_dir(path: Path) -> None:
    if path.name.startswith(".mediaforce-ab-av1-") or path.name.startswith(".ab-av1-"):
        try:
            path.rmdir()
        except OSError:
            return


def _remove_remote_stale_staging_path(path: Path, host: dict[str, Any]) -> None:
    quoted_path = shlex.quote(str(path))
    script = f"rm -f {quoted_path}"
    if path.parent.name.startswith(".mediaforce-ab-av1-") or path.parent.name.startswith(".ab-av1-"):
        quoted_parent = shlex.quote(str(path.parent))
        script = (
            f"{script}; "
            f"if [ -d {quoted_parent} ]; then rmdir {quoted_parent} >/dev/null 2>&1 || true; fi"
        )
    try:
        run_remote_command(host, ["sh", "-lc", script], timeout=10)
    except Exception:
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
    manifest_items_cache: dict[Path, list[dict[str, Any]] | None] = {}
    for child in children:
        progress = object_dict(child.get("progress"))
        child_totals = encode_job_manifest_totals(child, manifest_items_cache=manifest_items_cache)
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
    failure_analyses: list[dict[str, Any]] = []
    failure_indexes: list[int] = []
    for child in attention_children + failed_children + stopped_children:
        failure_analysis = object_dict(object_dict(child.get("progress")).get("failure_analysis"))
        if not failure_analysis:
            continue
        child_item_analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
        if child_item_analyses:
            failure_analyses.extend(child_item_analyses)
        else:
            failure_analyses.append(failure_analysis)
        for index in object_list(failure_analysis.get("manifest_indexes")):
            if isinstance(index, int) and index >= 0:
                failure_indexes.append(index)
    if failure_analyses:
        if not failure_indexes:
            failure_indexes = [
                int_value(analysis.get("manifest_index"))
                for analysis in failure_analyses
                if int_value(analysis.get("manifest_index")) >= 0
            ]
        progress["failure_analysis"] = _aggregate_quality_failure_analysis(
            sorted(set(failure_indexes)),
            failure_analyses,
        )
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


def encode_job_manifest_totals(
        job: dict[str, Any],
        *,
        manifest_items_cache: dict[Path, list[dict[str, Any]] | None] | None = None,
) -> dict[str, Any]:
    manifest_path = Path(str(job.get("manifest_path") or "")).expanduser()
    fallback_item_count = int_value(job.get("item_count"))
    if manifest_items_cache is not None and manifest_path in manifest_items_cache:
        manifest_items = manifest_items_cache[manifest_path]
    else:
        manifest_items = None
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
            else:
                manifest_items = [object_dict(item) for item in object_list(payload.get("items"))]
        if manifest_items_cache is not None:
            manifest_items_cache[manifest_path] = manifest_items
    if manifest_items is None:
        return {
            "total_item_count": fallback_item_count,
            "total_duration_seconds": 0.0,
            "total_source_size_bytes": 0,
        }
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
    previous_last_host = object_dict(job.get("last_host"))
    attempt_count = int_value(job.get("attempt_count"))
    failure_analysis = _encode_failure_analysis(job, failure_kind=failure_kind, error_message=error_message)
    auto_policy_retry = _apply_auto_quality_policy_retry(job, failure_analysis)
    retryable = _encode_failure_is_retryable(failure_kind, error_message, assigned_host)
    if auto_policy_retry:
        retryable = True
    host_related = _encode_failure_is_host_related(failure_kind, error_message, assigned_host)
    retry_beyond_attempt_cap = _encode_failure_retries_after_attempt_cap(
        failure_kind,
        error_message,
        assigned_host,
    )
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
            "last_host": _encode_failure_last_host_payload(
                assigned_host,
                previous_last_host=previous_last_host,
                host_related=host_related,
            ),
            "updated_at": now_iso,
        }
    )

    if retryable and (attempt_count < deps.encode_job_max_attempts or retry_beyond_attempt_cap):
        retry_delay = _encode_job_retry_delay_seconds(min(attempt_count, deps.encode_job_max_attempts), deps)
        retry_not_before = (now + timedelta(seconds=retry_delay)).isoformat(timespec="seconds")
        retry_reason = _encode_retry_waiting_reason(
            failure_kind=failure_kind,
            retry_not_before=retry_not_before,
        )
        if auto_policy_retry and failure_analysis:
            retry_reason = f"retrying after measured policy adjustment at {retry_not_before}"
        job.update(
            {
                "status": "retry_backoff",
                "finished_at": None,
                "retry_not_before": retry_not_before,
                "waiting_reason": retry_reason,
                "terminal_reason": None,
                "host_cooldown_until": (
                    (now + timedelta(seconds=deps.encode_host_cooldown_seconds)).isoformat(timespec="seconds")
                    if host_related and assigned_host
                    else None
                ),
                "progress": _finalize_encode_job_progress(job, deps=deps, terminal_state="retry_backoff"),
            }
        )
        _attach_failure_analysis_to_progress(job, failure_analysis)
        save_encode_job(connection, job)
        sync_encode_job_parent(connection, job, deps)
        connection.commit()
        _cleanup_encode_retry_artifacts(
            connection,
            manifest_path=Path(str(job["manifest_path"])),
            indexes=job.get("manifest_indexes"),
            deps=deps,
        )
        connection.commit()
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
            "progress": _finalize_encode_job_progress(job, deps=deps, terminal_state="needs_attention"),
        }
    )
    _attach_failure_analysis_to_progress(job, failure_analysis)
    save_encode_job(connection, job)
    sync_encode_job_parent(connection, job, deps)
    connection.commit()


def _encode_failure_analysis(
        job: dict[str, Any],
        *,
        failure_kind: str,
        error_message: str,
) -> dict[str, Any] | None:
    if failure_kind != "deterministic":
        return None
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    manifest_items = [object_dict(item) for item in object_list(manifest.get("items"))]
    indexes = _manifest_indexes_for_job(job, manifest_items)
    if not indexes:
        return None
    item_analyses: list[dict[str, Any]] = []
    for index in indexes:
        if index < 0 or index >= len(manifest_items):
            continue
        item = manifest_items[index]
        video_policy = object_dict(object_dict(item.get("resolved_policy")).get("video"))
        source_cap_video_percent = float_value(
            object_dict(object_dict(item.get("stream_budget_ledger")).get("source_relative_cap")).get(
                "video_cap_percent"
            )
        ) or None
        item_analysis = analyze_quality_policy_failure(
            error_message,
            video_policy,
            max_encoded_percent_override=source_cap_video_percent,
        )
        if not item_analysis:
            continue
        item_analysis["manifest_index"] = index
        item_analysis["manifest_indexes"] = [index]
        item_analysis["item_rel_path"] = str(item.get("rel_path") or item.get("source_path") or "")
        item_analyses.append(item_analysis)
    if not item_analyses:
        return None
    return _aggregate_quality_failure_analysis(indexes, item_analyses)


def _aggregate_quality_failure_analysis(
        indexes: list[int],
        item_analyses: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_indexes = {index for index in indexes if index >= 0}
    analyzed_indexes = {
        int_value(item.get("manifest_index"))
        for item in item_analyses
        if int_value(item.get("manifest_index")) >= 0
    }
    all_selected_analyzed = selected_indexes == analyzed_indexes
    if len(item_analyses) == 1:
        analysis = dict(item_analyses[0])
        analysis["manifest_indexes"] = indexes
        analysis["item_analyses"] = item_analyses
        if not all_selected_analyzed:
            analysis["auto_retry_allowed"] = False
            analysis["retry_strategy"] = "needs_operator_approval"
            analysis["summary"] = (
                f"Measured policy analysis covers {len(item_analyses)} of {len(selected_indexes)} selected items; "
                "operator approval is required."
            )
        return analysis
    all_auto_retry = all(
        bool(item.get("auto_retry_allowed"))
        and str(item.get("retry_strategy") or "") == "auto_adjust_cap"
        and int_value(item.get("proposed_max_encoded_percent")) > 0
        for item in item_analyses
    ) and all_selected_analyzed
    proposed_caps = [
        int_value(item.get("proposed_max_encoded_percent"))
        for item in item_analyses
        if int_value(item.get("proposed_max_encoded_percent")) > 0
    ]
    first = dict(item_analyses[0])
    first.update(
        {
            "manifest_indexes": indexes,
            "item_analyses": item_analyses,
            "auto_retry_allowed": all_auto_retry,
            "retry_strategy": "auto_adjust_cap" if all_auto_retry else "needs_operator_approval",
            "proposed_max_encoded_percent": max(proposed_caps) if proposed_caps else None,
            "summary": (
                f"Measured policy analysis covers {len(item_analyses)} selected items; "
                "all are safe for bounded cap adjustment."
                if all_auto_retry
                else (
                    f"Measured policy analysis covers {len(item_analyses)} of {len(selected_indexes)} selected items; "
                    "operator approval is required."
                )
            ),
        }
    )
    return first


def _apply_auto_quality_policy_retry(job: dict[str, Any], analysis: dict[str, Any] | None) -> bool:
    if not analysis or not bool(analysis.get("auto_retry_allowed")):
        return False
    if str(analysis.get("retry_strategy") or "") != "auto_adjust_cap":
        return False
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not manifest_path.exists():
        return False
    with _locked_manifest_file(manifest_path):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        manifest_items = [object_dict(item) for item in object_list(manifest.get("items"))]
        cap_by_index = _quality_policy_retry_caps_by_index(job, analysis, manifest_items)
        if not cap_by_index:
            return False
        changed = False
        for index, proposed_cap in cap_by_index.items():
            if index < 0 or index >= len(manifest_items):
                continue
            item = manifest_items[index]
            policy = object_dict(item.get("resolved_policy"))
            video_policy = dict(object_dict(policy.get("video")))
            current_cap = int_value(video_policy.get("max_encoded_percent"))
            if proposed_cap <= current_cap:
                continue
            video_policy["max_encoded_percent"] = proposed_cap
            policy["video"] = video_policy
            item["resolved_policy"] = policy
            manifest["items"][index] = item
            changed = True
        if not changed:
            return False
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        except OSError:
            return False
    return True


def _quality_policy_retry_caps_by_index(
        job: dict[str, Any],
        analysis: dict[str, Any],
        manifest_items: list[dict[str, Any]],
) -> dict[int, int]:
    item_analyses = [object_dict(item) for item in object_list(analysis.get("item_analyses"))]
    if item_analyses:
        caps: dict[int, int] = {}
        expected_indexes = {index for index in object_list(analysis.get("manifest_indexes")) if isinstance(index, int)}
        for item_analysis in item_analyses:
            if not bool(item_analysis.get("auto_retry_allowed")):
                return {}
            if str(item_analysis.get("retry_strategy") or "") != "auto_adjust_cap":
                return {}
            index = int_value(item_analysis.get("manifest_index"))
            proposed_cap = int_value(item_analysis.get("proposed_max_encoded_percent"))
            if index < 0 or proposed_cap <= 0:
                return {}
            caps[index] = proposed_cap
        if expected_indexes and set(caps) != expected_indexes:
            return {}
        return caps

    indexes = [index for index in object_list(analysis.get("manifest_indexes")) if isinstance(index, int)]
    if not indexes:
        indexes = _manifest_indexes_for_job(job, manifest_items)
    if len(indexes) != 1:
        return {}
    proposed_cap = int_value(analysis.get("proposed_max_encoded_percent"))
    return {indexes[0]: proposed_cap} if proposed_cap > 0 else {}


@contextmanager
def _locked_manifest_file(manifest_path: Path) -> Iterator[None]:
    lock_path = manifest_path.with_suffix(f"{manifest_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _attach_failure_analysis_to_progress(job: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    if not analysis:
        return
    progress = object_dict(job.get("progress"))
    progress["failure_analysis"] = analysis
    job["progress"] = progress


def _controller_staging_access_issue(
        config: MediaforceConfig,
        host: dict[str, Any],
) -> str | None:
    staging_root = (
        config.staging_root
        if host_media_access_for_host(host) == "stream"
        else config.staging_root_for_host(host)
    ).expanduser()
    nearest_existing = staging_root
    try:
        while not nearest_existing.exists():
            parent = nearest_existing.parent
            if parent == nearest_existing:
                break
            nearest_existing = parent
        accessible = (
            nearest_existing.is_dir()
            and os.access(nearest_existing, os.W_OK | os.X_OK)
        )
    except OSError:
        accessible = False
    if accessible:
        return None
    return (
        f"Mediaforce cannot access {staging_root} on this computer. "
        "Mount the storage to continue."
    )


def select_encode_host(
        connection: DBClient,
        config: MediaforceConfig,
        job: dict[str, Any],
        deps: EncodeQueueRuntimeDeps,
        *,
        host_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    library_key = _encode_job_library_key(job)
    host_rows = sorted(
        host_rows if host_rows is not None else deps.host_runtime_rows(connection, config),
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

    def _probe_available(host: dict[str, Any]) -> bool:
        return bool(host.get("probe_available", host.get("available")))

    active_host_candidates = [
        host
        for host in host_rows
        if bool(host.get("available"))
        and _probe_available(host)
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
        and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
        and _schedule_open(host)
    ]
    encode_capable_hosts = [
        host
        for host in host_rows
        if "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
    ]
    startable_host_candidates = [
        host
        for host in host_rows
        if not bool(host.get("available"))
        and not _probe_available(host)
        and not object_list(host.get("issues"))
        and (
            bool(deps.host_lifecycle_start_command(host))
            or bool(host.get("storage_recovery_available"))
        )
        and "encode_queue" in {str(capability).lower() for capability in host.get("capabilities") or []}
        and int(host.get("active_encode_count") or 0) < int(host.get("max_parallel_encodes") or 1)
        and _schedule_open(host)
    ]
    if not encode_capable_hosts and not startable_host_candidates:
        return None, "waiting for an available encode host"

    storage_issues: list[str] = []

    def _storage_ready(host: dict[str, Any]) -> bool:
        issue = _controller_staging_access_issue(config, host)
        if issue:
            storage_issues.append(issue)
            return False
        return True

    active_hosts = [host for host in active_host_candidates if _storage_ready(host)]
    startable_hosts = [host for host in startable_host_candidates if _storage_ready(host)]
    if not active_hosts and not startable_hosts and storage_issues:
        return None, storage_issues[0]

    globally_blocked_hosts = _globally_backed_off_encode_hosts(connection, deps, now=now)
    blocked_host_tokens = {
        token
        for blocked_host in globally_blocked_hosts.values()
        for token in _host_identity_tokens(blocked_host)
    }
    if blocked_host_tokens:
        had_blocked_candidate = any(
            bool(_host_identity_tokens(host) & blocked_host_tokens)
            for host in active_hosts + startable_hosts
        )
        eligible_active_hosts = [
            host
            for host in active_hosts
            if not (_host_identity_tokens(host) & blocked_host_tokens)
        ]
        eligible_startable_hosts = [
            host
            for host in startable_hosts
            if not (_host_identity_tokens(host) & blocked_host_tokens)
        ]
        active_hosts = eligible_active_hosts
        startable_hosts = eligible_startable_hosts
        if active_hosts or startable_hosts:
            pass
        elif had_blocked_candidate:
            blocked_host_name = _blocked_host_wait_name(globally_blocked_hosts)
            return None, f"waiting for host cooldown to expire on {blocked_host_name}"

    cooldown_until = deps.parse_iso(job.get("host_cooldown_until"))
    last_host = object_dict(job.get("last_host"))
    blocked_tokens = _host_identity_tokens(last_host)
    if cooldown_until is not None and cooldown_until > now and blocked_tokens:
        eligible_active_hosts = [
            host
            for host in active_hosts
            if not (_host_identity_tokens(host) & blocked_tokens)
        ]
        if eligible_active_hosts:
            return object_dict(eligible_active_hosts[0]), None
        eligible_startable_hosts = [
            host
            for host in startable_hosts
            if not (_host_identity_tokens(host) & blocked_tokens)
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


def _globally_backed_off_encode_hosts(
        connection: DBClient,
        deps: EncodeQueueRuntimeDeps,
        *,
        now: datetime,
) -> dict[str, dict[str, Any]]:
    if connection is None:
        return {}
    rows = connection.execute(
        select(
            encode_jobs.c.last_host_json,
            encode_jobs.c.host_cooldown_until,
            encode_jobs.c.last_failure_kind,
            encode_jobs.c.error,
            encode_jobs.c.attempt_count,
        )
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .where(encode_jobs.c.host_cooldown_until.is_not(None))
    ).mappings().fetchall()
    host_failures: list[dict[str, Any]] = []
    for row in rows:
        cooldown_until = deps.parse_iso(row["host_cooldown_until"])
        if cooldown_until is None or cooldown_until <= now:
            continue
        host_payload = _load_quarantine_host_payload(row["last_host_json"])
        identity_tokens = _host_identity_tokens(host_payload)
        if not identity_tokens:
            continue
        failure_kind = str(row["last_failure_kind"] or "").strip()
        error_message = str(row["error"] or "")
        if not _encode_failure_retries_after_attempt_cap(failure_kind, error_message, host_payload):
            continue
        failure_state = _matching_host_failure_state(host_failures, identity_tokens)
        if failure_state is None:
            failure_state = {
                "count": 0,
                "cooldown_until": cooldown_until,
                "identity_tokens": sorted(identity_tokens),
                **host_payload,
                "label": str(host_payload.get("label") or host_payload.get("key") or host_payload.get("host") or "blocked host"),
            }
            host_failures.append(failure_state)
        else:
            merged_tokens = set(object_list(failure_state.get("identity_tokens"))) | identity_tokens
            failure_state["identity_tokens"] = sorted(merged_tokens)
            for key in ("key", "label", "host"):
                value = str(host_payload.get(key) or "").strip()
                if value and not str(failure_state.get(key) or "").strip():
                    failure_state[key] = value
        failure_state["count"] = int(failure_state["count"]) + 1
        failure_state["failure_streak"] = max(
            int(failure_state.get("failure_streak") or 0),
            int_value(host_payload.get("failure_streak")),
        )
        if cooldown_until > failure_state["cooldown_until"]:
            failure_state["cooldown_until"] = cooldown_until
    blocked_hosts = [
        payload
        for payload in host_failures
        if int(payload["count"]) >= ENCODE_HOST_BACKUP_FAILURE_THRESHOLD
        or int(payload.get("failure_streak") or 0) >= ENCODE_HOST_BACKUP_FAILURE_THRESHOLD
    ]
    blocked_hosts.sort(key=_blocked_host_sort_key)
    return {
        f"blocked-{index}": payload
        for index, payload in enumerate(blocked_hosts)
    }


def _encode_failure_last_host_payload(
        assigned_host: dict[str, Any],
        *,
        previous_last_host: dict[str, Any],
        host_related: bool,
) -> dict[str, Any]:
    if not assigned_host:
        return {}
    payload = dict(assigned_host)
    if not host_related:
        payload.pop("failure_streak", None)
        return payload
    previous_streak = int_value(previous_last_host.get("failure_streak"))
    if _host_identity_matches(assigned_host, previous_last_host):
        payload = {**previous_last_host, **payload}
        payload["failure_streak"] = previous_streak + 1
    else:
        payload["failure_streak"] = 1
    return payload


def _host_identity_tokens(host: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in object_list(host.get("identity_tokens")):
        token = str(value or "").strip()
        if token:
            tokens.add(token)
    for key in ("key", "host"):
        value = str(host.get(key) or "").strip()
        if value:
            tokens.add(value)
    return tokens


def _host_identity_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_tokens = _host_identity_tokens(left)
    right_tokens = _host_identity_tokens(right)
    return bool(left_tokens and right_tokens and left_tokens & right_tokens)


def _matching_host_failure_state(
        host_failures: list[dict[str, Any]],
        identity_tokens: set[str],
) -> dict[str, Any] | None:
    for payload in host_failures:
        if _host_identity_matches({"identity_tokens": sorted(identity_tokens)}, payload):
            return payload
    return None


def _load_quarantine_host_payload(raw_payload: Any) -> dict[str, Any]:
    try:
        return object_dict(json.loads(str(raw_payload or "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _reset_host_failure_streak(job: dict[str, Any]) -> dict[str, Any]:
    payload = object_dict(job.get("host")) or object_dict(job.get("last_host"))
    if not payload:
        return {}
    reset_payload = dict(payload)
    reset_payload.pop("failure_streak", None)
    return reset_payload


def _blocked_host_sort_key(host_payload: dict[str, Any]) -> tuple[str, str, str]:
    cooldown_until = str(host_payload.get("cooldown_until") or "")
    identity_key = str(host_payload.get("key") or host_payload.get("host") or "")
    display_name = _blocked_host_display_name(host_payload)
    return cooldown_until, identity_key.lower(), display_name.lower()


def _blocked_host_display_name(host_payload: dict[str, Any]) -> str:
    return (
        str(host_payload.get("label") or "").strip()
        or str(host_payload.get("key") or "").strip()
        or str(host_payload.get("host") or "").strip()
        or "the blocked host"
    )


def _blocked_host_wait_name(blocked_hosts: dict[str, dict[str, Any]]) -> str:
    if not blocked_hosts:
        return "the blocked host"
    first_blocked = sorted(blocked_hosts.values(), key=_blocked_host_sort_key)[0]
    return _blocked_host_display_name(first_blocked)


def _encode_job_library_key(job: dict[str, Any]) -> str:
    prefix = str(job.get("prefix") or "").strip()
    if not prefix:
        return ""
    return prefix.split("/", 1)[0].strip().lower()


def _encode_host_selection_key(job: dict[str, Any]) -> tuple[str, bool, str, str]:
    return (
        _encode_job_library_key(job),
        bool(job.get("bypass_schedule")),
        str(job.get("host_cooldown_until") or ""),
        json.dumps(object_dict(job.get("last_host")), sort_keys=True, separators=(",", ":")),
    )


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
        connection.commit()
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
    parent_sync_jobs: dict[str, dict[str, Any]] = {}

    def defer_parent_sync(job: dict[str, Any]) -> None:
        parent_job_id = str(job.get("parent_job_id") or "").strip()
        if parent_job_id:
            parent_sync_jobs[parent_job_id] = job

    def sync_deferred_parents() -> None:
        for pending_job in parent_sync_jobs.values():
            sync_encode_job_parent(connection, pending_job, deps)

    rows = connection.execute(
        select(encode_jobs.c.job_id)
        .where(encode_jobs.c.status == "queued")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.created_at, literal_column("rowid"))
    ).mappings().fetchall()
    host_rows = deps.host_runtime_rows(connection, config) if rows else []
    host_selection_cache: dict[
        tuple[str, bool, str, str],
        tuple[dict[str, Any] | None, str | None],
    ] = {}
    for row in rows:
        job = load_encode_job(connection, str(row["job_id"]))
        if job is None:
            continue
        library_key = _encode_job_library_key(job)
        if library_key and library_key not in config.source_root_map:
            waiting_reason = "Library is Browse only or Disabled in Settings."
            if str(job.get("waiting_reason") or "") != waiting_reason:
                job.update({"waiting_reason": waiting_reason, "updated_at": deps.now_iso()})
                save_encode_job(connection, job)
                defer_parent_sync(job)
            continue
        selection_key = _encode_host_selection_key(job)
        selection = host_selection_cache.get(selection_key)
        if selection is None:
            selection = select_encode_host(
                connection,
                config,
                job,
                deps,
                host_rows=host_rows,
            )
            host_selection_cache[selection_key] = selection
        host_payload, waiting_reason = selection
        if host_payload is None:
            if str(job.get("waiting_reason") or "") != str(waiting_reason or ""):
                job.update({"waiting_reason": waiting_reason, "updated_at": deps.now_iso()})
                save_encode_job(connection, job)
                defer_parent_sync(job)
            continue
        persisted_host_payload = persisted_encode_host_payload(host_payload)
        if job.get("waiting_reason") or job.get("host") != persisted_host_payload:
            job.update({"waiting_reason": None, "host": persisted_host_payload, "updated_at": deps.now_iso()})
            save_encode_job(connection, job)
            defer_parent_sync(job)
        sync_deferred_parents()
        return job
    sync_deferred_parents()
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
        library_key = _encode_job_library_key(job)
        if library_key and library_key not in config.source_root_map:
            job.update(
                {
                    "status": "queued",
                    "worker_id": None,
                    "owner_pid": None,
                    "process_pid": None,
                    "waiting_reason": "Library is Browse only or Disabled in Settings.",
                    "updated_at": deps.now_iso(),
                }
            )
            save_encode_job(connection, job)
            sync_encode_job_parent(connection, job, deps)
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
                    last_host = object_dict(job.get("last_host"))
                    if final_status == "completed":
                        last_host = _reset_host_failure_streak(job)
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
                            "last_host": last_host,
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
    if failure_kind in {"controller_storage_unavailable", "host_unavailable", "ssh_transport"}:
        return True
    return _encode_failure_is_ssh_transport(error_message, host_payload)


def _encode_failure_is_ssh_transport(error_message: str, host_payload: dict[str, Any]) -> bool:
    if str(host_payload.get("mode") or "") != "ssh":
        return False
    lowered = error_message.lower()
    direct_markers = (
        "host key verification failed",
        "could not resolve hostname",
        "temporary failure in name resolution",
        "name or service not known",
        "nodename nor servname provided",
        "no route to host",
        "kex_exchange_identification",
        "ssh_exchange_identification",
        "permission denied (publickey",
        "ssh remote command exited with status 255",
        "ssh:",
    )
    if any(marker in lowered for marker in direct_markers):
        return True
    transport_markers = (
        "connection refused",
        "connection reset",
        "connection timed out",
        "operation timed out",
        "broken pipe",
        "connection closed by",
    )
    ssh_context_markers = ("remote host", "port 22", "ssh connection")
    return any(marker in lowered for marker in transport_markers) and any(
        marker in lowered for marker in ssh_context_markers
    )


def _quality_temp_setup_is_host_related(message: str) -> bool:
    lowered = message.lower()
    mount_markers = (
        "/volumes/",
        "mount",
        "staging",
        "transcode",
    )
    transport_markers = (
        "operation timed out",
        "connection timed out",
        "broken pipe",
        "connection reset",
        "ssh:",
    )
    return any(marker in lowered for marker in mount_markers) and any(
        marker in lowered for marker in transport_markers
    )


def _encode_failure_is_retryable(failure_kind: str, error_message: str, host_payload: dict[str, Any]) -> bool:
    if failure_kind in {
        "controller_storage_unavailable",
        "worker_restart",
        "stale_lease",
        "host_unavailable",
        "ssh_transport",
    }:
        return True
    if failure_kind in {"stopped", "deterministic"}:
        return False
    return _encode_failure_is_host_related(failure_kind, error_message, host_payload)


def _encode_failure_retries_after_attempt_cap(
        failure_kind: str,
        error_message: str,
        host_payload: dict[str, Any],
) -> bool:
    if failure_kind == "controller_storage_unavailable":
        return False
    if not _encode_failure_is_host_related(failure_kind, error_message, host_payload):
        return False
    lowered = error_message.lower()
    non_transient_markers = ("permission denied", "host key verification failed")
    return not any(marker in lowered for marker in non_transient_markers)


def _encode_retry_waiting_reason(*, failure_kind: str, retry_not_before: str) -> str:
    reason = {
        "worker_restart": "worker restart",
        "stale_lease": "stale worker lease",
        "host_unavailable": "host availability issue",
        "controller_storage_unavailable": "controller storage issue",
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
        connection.commit()


def _classify_encode_failure(exc: Exception, job: dict[str, Any]) -> str:
    message = str(exc).lower()
    host_payload = object_dict(job.get("host"))
    if isinstance(exc, HostReadinessError):
        return exc.failure_kind
    if isinstance(exc, PermissionError):
        return "controller_media_access"
    if isinstance(exc, QualityTempSetupError) and _quality_temp_setup_is_host_related(message):
        return "ssh_transport"
    if isinstance(exc, (QualitySearchError, QualityTempCleanupError, QualityTempSetupError)):
        return "deterministic"
    if _encode_failure_is_quality_policy_failure(message):
        return "deterministic"
    if _encode_failure_is_ssh_transport(message, host_payload):
        return "ssh_transport"
    if "staging file already exists" in message:
        return "deterministic"
    return "deterministic"


def _encode_failure_is_quality_policy_failure(message: str) -> bool:
    return any(
        marker in message
        for marker in (
            "failed to find a suitable crf",
            "quality search did not run",
        )
    )


def _remove_path(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    if path.is_dir():
        return
    try:
        safe_unlink(path)
    except OSError:
        return
