import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
import json
import time
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.type_defs import float_value, object_dict
from mediaforce.encoding.encode_queue import RUNNABLE_ENCODE_JOB_KINDS
from mediaforce.encoding.encode_queue import list_encode_jobs
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
        decorate_encode_job_for_scheduler: Any,
        encode_queue_schedule_profiles: Any,
        host_max_parallel_encodes: Any,
        host_schedule_profile_key: Any,
        scheduler_allows_encode_run: Any,
        format_eta_seconds: Any,
        job_host_key: Any,
        always_schedule_profile: str,
        default_host_schedule_profile: str,
        now: datetime | None = None,
) -> list[dict[str, Any]]:
    statuses = safe_collect_statuses(config)
    running_counts = running_encode_counts_by_host(connection)
    running_jobs = [
        job
        for job in (
            decorate_encode_job_for_scheduler(config, job)
            for job in list_encode_jobs(connection, statuses=("running",), limit=32, job_kinds=RUNNABLE_ENCODE_JOB_KINDS)
        )
        if job is not None
    ]
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
        merged_source_roots = {
            key: str(path)
            for key, path in config.source_root_map_for_host(host_config).items()
        }
        runtime_host_payload = {
            "host": str(host_config.get("host") or status.key),
            "start_command": str(host_config.get("start_command") or ""),
            "stop_command": str(host_config.get("stop_command") or ""),
            "start_timeout_seconds": host_lifecycle_start_timeout_seconds(host_config),
            "media_access": normalize_host_media_access(host_config.get("media_access")),
            "allowed_libraries": allowed_libraries,
            "source_roots": merged_source_roots,
            "staging_root": str(host_config.get("staging_root") or "").strip() or None,
        }
        max_parallel_encodes = host_max_parallel_encodes(host_config)
        schedule_profile = host_schedule_profile_key(host_config)
        policy = object_dict(profiles.get(schedule_profile) or profiles[default_host_schedule_profile])
        active_encode_count = running_counts.get(status.key, 0)
        schedule_open = scheduler_allows_encode_run(policy, now=current_time, host_payload=asdict(status))
        encode_capable = "encode_queue" in capabilities
        queue_active = status.available and encode_capable and schedule_open and active_encode_count < max_parallel_encodes
        if not status.available:
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
        host_running_jobs = [job for job in running_jobs if job_host_key(job) == status.key]
        host_speed = sum(_progress_float(job, "speed") for job in host_running_jobs)
        host_remaining_duration_seconds = sum(
            _progress_float(job, "remaining_duration_seconds") for job in host_running_jobs
        )
        host_eta_seconds = (host_remaining_duration_seconds / host_speed) if host_speed > 0 else None
        rows.append(
            {
                **asdict(status),
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
    counts: dict[str, int] = {}
    rows = connection.execute(
        select(encode_jobs.c.host_json)
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
    ).mappings().fetchall()
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


def stop_encode_host_if_configured(host_payload: dict[str, Any] | None, *, lifecycle_command_timeout_seconds: int) -> None:
    host = object_dict(host_payload)
    stop_command = host_lifecycle_stop_command(host)
    if not stop_command:
        return
    result = run_host_lifecycle_command(host, stop_command, timeout=lifecycle_command_timeout_seconds)
    if result.returncode != 0:
        raise RuntimeError(lifecycle_command_error_detail(result, "host stop command failed"))


def default_sample_host_key(config: MediaforceConfig, *, safe_collect_statuses: Any) -> str:
    return default_sample_host_key_from_statuses(sample_calibration_host_statuses(config, safe_collect_statuses=safe_collect_statuses))


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
    return sample_host_options_from_statuses(sample_calibration_host_statuses(config, safe_collect_statuses=safe_collect_statuses))


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
