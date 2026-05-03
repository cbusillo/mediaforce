import json
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.type_defs import float_value, object_dict
from mediaforce.encoding.encode_queue import RUNNABLE_ENCODE_JOB_KINDS
from mediaforce.remote import HostStatus, collect_host_statuses, host_status_targets_current_machine, \
    normalize_host_media_access, run_host_lifecycle_command


def _progress_float(job: dict[str, Any], key: str) -> float:
    return float_value(object_dict(job.get("progress")).get(key))


def unavailable_host_error_message(status: HostStatus | None) -> str:
    detail = status.detail if status is not None else None
    if detail:
        return detail
    if status is not None and status.message:
        return status.message
    return "Encode host is not available."


def lifecycle_command_error_detail(result: subprocess.CompletedProcess[str], default_message: str) -> str:
    return result.stderr.strip() or result.stdout.strip() or default_message


def host_runtime_rows(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        safe_collect_statuses: Any,
        encode_queue_schedule_profiles: Any,
        host_max_parallel_encodes: Any,
        host_schedule_profile_key: Any,
        scheduler_allows_encode_run: Any,
        format_eta_seconds: Any,
        always_schedule_profile: str,
        default_host_schedule_profile: str,
        now: datetime | None = None,
) -> list[dict[str, Any]]:
    statuses = safe_collect_statuses(config)
    running_jobs = _running_encode_jobs_for_hosts(connection, format_eta_seconds=format_eta_seconds)
    running_counts = running_encode_counts_by_host(connection)
    profiles = encode_queue_schedule_profiles(config)
    rows: list[dict[str, Any]] = []
    current_time = now or datetime.now(UTC)
    for status in statuses:
        host_config = host_config_for_key(config, status.key)
        capabilities = {capability.lower() for capability in status.capabilities}
        raw_allowed_libraries = host_config.get("allowed_libraries")
        allowed_libraries = [
            str(value)
            for value in raw_allowed_libraries
            if isinstance(value, str)
        ] if isinstance(raw_allowed_libraries, list) else []
        media_access = normalize_host_media_access(host_config.get("media_access"))
        host_source_roots = (
            config.explicit_source_root_map_for_host(host_config)
            if media_access == "stream"
            else config.source_root_map_for_host(host_config)
        )
        merged_source_roots = {key: str(path) for key, path in host_source_roots.items()}
        runtime_host_payload = {
            "host": str(host_config.get("host") or status.key),
            "start_command": str(host_config.get("start_command") or ""),
            "stop_command": str(host_config.get("stop_command") or ""),
            "start_timeout_seconds": host_lifecycle_start_timeout_seconds(host_config),
            "media_access": media_access,
            "allowed_libraries": allowed_libraries,
            "source_roots": merged_source_roots,
            "staging_root": str(host_config.get("staging_root") or "").strip() or None,
        }
        max_parallel_encodes = host_max_parallel_encodes(host_config)
        schedule_profile = host_schedule_profile_key(host_config)
        policy = object_dict(profiles.get(schedule_profile) or profiles[default_host_schedule_profile])
        active_encode_count = running_counts.get(status.key, 0)
        status_payload = asdict(status)
        schedule_open = scheduler_allows_encode_run(policy, now=current_time, host_payload=status_payload)
        encode_capable = "encode_queue" in capabilities
        queue_active = status.available and encode_capable and schedule_open and active_encode_count < max_parallel_encodes
        host_running_jobs = [job for job in running_jobs if str(job.get("host_key") or "") == status.key]
        active_probe_degraded = bool(active_encode_count > 0 and not status.available and host_running_jobs)
        if active_probe_degraded:
            active_reason = "status check deferred while encode is running"
        elif not status.available:
            active_reason = status.message
        elif not encode_capable:
            active_reason = "encode queue capability disabled"
        elif str(policy.get("mode") or "anytime") == "never":
            active_reason = "encode queue disabled by schedule"
        elif not schedule_open:
            active_reason = ""
        elif active_encode_count >= max_parallel_encodes:
            active_reason = "parallel encode slots are full"
        else:
            active_reason = ""
        schedule_detail = str(policy["summary"])
        if schedule_profile == always_schedule_profile and schedule_detail == "runs anytime":
            schedule_detail = ""
        host_speed = sum(_progress_float(job, "speed") for job in host_running_jobs)
        host_remaining_duration_seconds = sum(
            _progress_float(job, "remaining_duration_seconds") for job in host_running_jobs
        )
        host_eta_seconds = (host_remaining_duration_seconds / host_speed) if host_speed > 0 else None
        if active_probe_degraded:
            status_payload = {
                **status_payload,
                "probe_available": False,
                "probe_message": status.message,
                "probe_issues": list(status.issues),
                "available": True,
                "message": "Encoding in progress; latest host check was deferred",
                "issues": [],
            }
        else:
            status_payload = {
                **status_payload,
                "probe_available": status.available,
                "probe_message": status.message,
                "probe_issues": list(status.issues),
            }
        rows.append(
            {
                **status_payload,
                **runtime_host_payload,
                "schedule_profile": schedule_profile,
                "schedule_profile_label": str(policy.get("label") or "Always"),
                "scheduler_summary": str(policy["summary"]),
                "schedule_detail": schedule_detail,
                "schedule_open": schedule_open,
                "max_parallel_encodes": max_parallel_encodes,
                "active_encode_count": active_encode_count,
                "running_jobs": host_running_jobs,
                "telemetry": {
                    "aggregate_speed": host_speed or None,
                    "eta_seconds": host_eta_seconds,
                    "eta_copy": format_eta_seconds(host_eta_seconds),
                },
                "queue_active": queue_active,
                "active_flag": "active" if queue_active else "idle",
                "active_reason": active_reason,
            }
        )
    return rows


def running_encode_counts_by_host(connection: DBClient) -> dict[str, int]:
    return _running_encode_counts_by_host(connection, running_jobs=None)


def _running_encode_counts_by_host(
        connection: DBClient,
        *,
        running_jobs: list[dict[str, Any]] | None,
) -> dict[str, int]:
    if running_jobs is not None:
        counts: dict[str, int] = {}
        for job in running_jobs:
            host_key = str(job.get("host_key") or "").strip()
            if not host_key:
                continue
            counts[host_key] = counts.get(host_key, 0) + 1
        return counts

    counts: dict[str, int] = {}
    rows = connection.execute(
        select(_running_job_host_key_sql().label("host_key"), func.count().label("job_count"))
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .group_by(_running_job_host_key_sql())
    ).mappings().fetchall()
    for row in rows:
        host_key = str(row["host_key"] or "").strip()
        if not host_key:
            continue
        counts[host_key] = int(row["job_count"] or 0)
    return counts


def _running_encode_jobs_for_hosts(connection: DBClient, *, format_eta_seconds: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        select(
            encode_jobs.c.job_id,
            encode_jobs.c.prefix,
            encode_jobs.c.item_count,
            encode_jobs.c.progress_json,
            _running_job_host_key_sql().label("host_key"),
        )
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.created_at.asc())
        .limit(32)
    ).mappings().fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        progress = _loads_progress_payload(row["progress_json"])
        decorated_progress = _decorate_running_job_progress(progress, format_eta_seconds=format_eta_seconds)
        jobs.append(
            {
                "job_id": str(row["job_id"]),
                "prefix": str(row["prefix"]),
                "item_count": int(row["item_count"] or 0),
                "host_key": str(row["host_key"] or "").strip(),
                "progress": decorated_progress,
                "scheduler_status_copy": "running now",
                "telemetry_summary": _running_job_telemetry_summary(decorated_progress),
            }
        )
    return jobs


def _running_job_host_key_sql() -> Any:
    current_key = func.nullif(func.trim(func.json_extract(encode_jobs.c.host_json, "$.key")), "")
    current_host = func.nullif(func.trim(func.json_extract(encode_jobs.c.host_json, "$.host")), "")
    current_label = func.nullif(func.trim(func.json_extract(encode_jobs.c.host_json, "$.label")), "")
    last_key = func.nullif(func.trim(func.json_extract(encode_jobs.c.last_host_json, "$.key")), "")
    last_host = func.nullif(func.trim(func.json_extract(encode_jobs.c.last_host_json, "$.host")), "")
    last_label = func.nullif(func.trim(func.json_extract(encode_jobs.c.last_host_json, "$.label")), "")
    return func.coalesce(
        current_key,
        current_host,
        current_label,
        last_key,
        last_host,
        last_label,
    )


def _loads_progress_payload(raw_progress: Any) -> dict[str, Any]:
    if not raw_progress:
        return {}
    try:
        loaded = json.loads(str(raw_progress))
    except json.JSONDecodeError:
        return {}
    return object_dict(loaded)


def _decorate_running_job_progress(progress: dict[str, Any], *, format_eta_seconds: Any) -> dict[str, Any]:
    total_duration_seconds = float_value(progress.get("total_duration_seconds"))
    overall_completed_duration_seconds = float_value(progress.get("overall_completed_duration_seconds"))
    remaining_duration_seconds = _float_or_none(progress.get("remaining_duration_seconds"), allow_zero=True)
    if remaining_duration_seconds is None:
        remaining_duration_seconds = max(total_duration_seconds - overall_completed_duration_seconds, 0.0)
    percent_complete = _float_or_none(progress.get("percent_complete"), allow_zero=True)
    if percent_complete is None and total_duration_seconds > 0:
        percent_complete = min(overall_completed_duration_seconds / total_duration_seconds, 1.0) * 100.0
    eta_seconds = _float_or_none(progress.get("eta_seconds"), allow_zero=True)
    speed = _float_or_none(progress.get("speed"))
    if eta_seconds is None and speed not in {None, 0.0}:
        eta_seconds = remaining_duration_seconds / speed
    return {
        **progress,
        "total_duration_seconds": total_duration_seconds,
        "overall_completed_duration_seconds": overall_completed_duration_seconds,
        "remaining_duration_seconds": remaining_duration_seconds,
        "percent_complete": percent_complete,
        "eta_seconds": eta_seconds,
        "eta_copy": format_eta_seconds(eta_seconds),
    }


def _running_job_telemetry_summary(progress: dict[str, Any]) -> str:
    progress_state = str(progress.get("progress_state") or "").strip().lower()
    phase_label = str(progress.get("phase_label") or "").strip()
    include_numeric_summary = progress_state not in {"quality_search", "starting"}
    summary_parts: list[str] = []
    percent_complete = _float_or_none(progress.get("percent_complete"), allow_zero=True)
    speed = _float_or_none(progress.get("speed"))
    fps = _float_or_none(progress.get("fps"))
    eta_copy = str(progress.get("eta_copy") or "").strip()
    if percent_complete is not None and include_numeric_summary:
        summary_parts.append(f"{percent_complete:.0f}%")
    if speed not in {None, 0.0} and include_numeric_summary:
        summary_parts.append(f"{speed:.2f}x")
    if fps not in {None, 0.0} and include_numeric_summary:
        summary_parts.append(f"{fps:.1f} fps")
    if eta_copy and include_numeric_summary:
        summary_parts.append(f"Est. ETA {eta_copy}")
    if summary_parts:
        return " · ".join(summary_parts)
    if progress_state == "quality_search":
        return phase_label or "Searching quality"
    if progress_state == "starting":
        return phase_label or "Starting encode"
    return phase_label


def _float_or_none(value: Any, *, allow_zero: bool = False) -> float | None:
    number = float_value(value)
    if allow_zero:
        if number < 0:
            return None
        return number
    if number <= 0:
        return None
    return number


def host_config_for_key(config: MediaforceConfig, host_key: str) -> dict[str, Any]:
    for host in config.remote_hosts:
        ssh_host = str(host.get("host") or "")
        label = str(host.get("label") or ssh_host or "remote")
        if host_key in {ssh_host, label}:
            return host
    return {}


def host_lifecycle_start_command(host: dict[str, Any] | None) -> str:
    return str(object_dict(host).get("start_command") or "").strip()


def host_lifecycle_stop_command(host: dict[str, Any] | None) -> str:
    return str(object_dict(host).get("stop_command") or "").strip()


def host_lifecycle_start_timeout_seconds(host: dict[str, Any] | None) -> int:
    try:
        return max(1, int(str(object_dict(host).get("start_timeout_seconds") or "180")))
    except (TypeError, ValueError):
        return 180


def fresh_host_status_for_key(config: MediaforceConfig, host_key: str) -> HostStatus | None:
    for status in collect_host_statuses(config):
        if status.key == host_key or status.label == host_key:
            return status
    return None


def ensure_encode_host_ready(
        config: MediaforceConfig,
        host_payload: dict[str, Any] | None,
        *,
        lifecycle_command_timeout_seconds: int,
        lifecycle_poll_seconds: float,
) -> bool:
    host = object_dict(host_payload)
    host_key = str(host.get("key") or host.get("host") or host.get("label") or "").strip()
    if not host_key:
        return False
    status = fresh_host_status_for_key(config, host_key)
    if status is not None and status.available:
        return False
    start_command = host_lifecycle_start_command(host)
    if not start_command:
        raise RuntimeError(unavailable_host_error_message(status))
    result = run_host_lifecycle_command(host, start_command, timeout=lifecycle_command_timeout_seconds)
    if result.returncode != 0:
        raise RuntimeError(lifecycle_command_error_detail(result, "host start command failed"))
    deadline = time.monotonic() + host_lifecycle_start_timeout_seconds(host)
    while time.monotonic() < deadline:
        refreshed = fresh_host_status_for_key(config, host_key)
        if refreshed is not None and refreshed.available:
            return True
        time.sleep(lifecycle_poll_seconds)
    refreshed = fresh_host_status_for_key(config, host_key)
    if refreshed is not None and refreshed.available:
        return True
    detail = refreshed.detail if refreshed is not None else None
    message = refreshed.message if refreshed is not None else f"Timed out waiting for {host_key}"
    raise RuntimeError(detail or message or f"Timed out waiting for {host_key}")


def stop_encode_host_if_configured(host_payload: dict[str, Any] | None, *,
                                   lifecycle_command_timeout_seconds: int) -> None:
    host = object_dict(host_payload)
    stop_command = host_lifecycle_stop_command(host)
    if not stop_command:
        return
    result = run_host_lifecycle_command(host, stop_command, timeout=lifecycle_command_timeout_seconds)
    if result.returncode != 0:
        raise RuntimeError(lifecycle_command_error_detail(result, "host stop command failed"))


def default_sample_host_key(config: MediaforceConfig, *, safe_collect_statuses: Any) -> str:
    return default_sample_host_key_from_statuses(
        sample_calibration_host_statuses(config, safe_collect_statuses=safe_collect_statuses))


def default_sample_host_key_from_statuses(statuses: list[HostStatus]) -> str:
    for status in statuses:
        if status.available and host_status_targets_current_machine(status):
            return status.key
    for status in statuses:
        if status.available:
            return status.key
    return ""


def sample_calibration_host_statuses(config: MediaforceConfig, *, safe_collect_statuses: Any) -> list[HostStatus]:
    hosts: list[HostStatus] = []
    for status in safe_collect_statuses(config):
        capabilities = {capability.lower() for capability in status.capabilities}
        if "sample_calibration" in capabilities:
            hosts.append(status)
    return hosts


def sample_host_options(config: MediaforceConfig, *, safe_collect_statuses: Any) -> list[dict[str, Any]]:
    return sample_host_options_from_statuses(
        sample_calibration_host_statuses(config, safe_collect_statuses=safe_collect_statuses))


def sample_host_options_from_statuses(statuses: list[HostStatus]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for status in statuses:
        options.append(
            {
                "key": status.key,
                "label": status.label,
                "detail": status.message if not status.available else (
                    "This machine" if host_status_targets_current_machine(status) else "Remote host"),
                "available": status.available,
            }
        )
    return options
