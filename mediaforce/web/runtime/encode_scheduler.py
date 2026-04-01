from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import JSONValue


@dataclass(slots=True)
class EncodeSchedulerDeps:
    normalize_encode_queue_scheduler: Any
    canonical_schedule_profile_key: Any
    default_host_schedule_profile: str
    always_schedule_profile: str
    default_scheduler_policy: dict[str, Any]
    encode_job_manifest_totals: Any
    encode_job_max_attempts: int


def encode_queue_scheduler_policy(config: MediaforceConfig, deps: EncodeSchedulerDeps) -> dict[str, Any]:
    encode_queue = config.raw.get("encode_queue")
    raw = encode_queue.get("scheduler") if isinstance(encode_queue, dict) else None
    if not isinstance(raw, dict):
        legacy_queue = config.raw.get("heavy_queue")
        raw = legacy_queue.get("scheduler") if isinstance(legacy_queue, dict) else None
    return deps.normalize_encode_queue_scheduler(raw if isinstance(raw, dict) else None)


def encode_queue_schedule_profiles(config: MediaforceConfig, deps: EncodeSchedulerDeps) -> dict[str, dict[str, Any]]:
    always = deps.normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})
    always["key"] = deps.always_schedule_profile
    always["label"] = "Always"
    profiles = {deps.always_schedule_profile: always}
    encode_queue = config.raw.get("encode_queue")
    raw_profiles = encode_queue.get("schedule_profiles") if isinstance(encode_queue, dict) else None
    if not isinstance(raw_profiles, list):
        return profiles
    for profile in raw_profiles:
        if not isinstance(profile, dict):
            continue
        key = deps.canonical_schedule_profile_key(str(profile.get("key") or profile.get("name") or ""))
        if not key or key == deps.always_schedule_profile:
            continue
        normalized = deps.normalize_encode_queue_scheduler(profile)
        normalized["key"] = key
        normalized["label"] = str(profile.get("label") or key.replace("_", " ").title())
        profiles[key] = normalized
    return profiles


def schedule_profile_policy_for_host(
        config: MediaforceConfig,
        host_payload: dict[str, Any] | None,
        deps: EncodeSchedulerDeps,
) -> dict[str, Any]:
    profiles = encode_queue_schedule_profiles(config, deps)
    profile_key = deps.canonical_schedule_profile_key(
        (host_payload or {}).get("schedule_profile") or deps.default_host_schedule_profile
    )
    return dict(profiles.get(profile_key) or profiles[deps.default_host_schedule_profile])


def encode_queue_scheduler_summary(policy: dict[str, Any]) -> str:
    if str(policy.get("mode") or "anytime") == "night":
        return f"window {int(policy['start_hour']):02d}:00-{int(policy['end_hour']):02d}:00 in host local time"
    return "runs anytime"


def host_schedule_now(current: datetime, host_payload: dict[str, Any] | None) -> datetime:
    offset_minutes = (host_payload or {}).get("utc_offset_minutes")
    try:
        if offset_minutes is not None:
            return current.astimezone(timezone(timedelta(minutes=int(offset_minutes))))
    except (TypeError, ValueError):
        pass
    timezone_name = str(
        (host_payload or {}).get("schedule_timezone") or (host_payload or {}).get("timezone") or ""
    ).strip()
    if timezone_name:
        try:
            return current.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return current.astimezone()


def scheduler_allows_encode_run(
        policy: dict[str, Any],
        deps: EncodeSchedulerDeps,
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
        local_now = host_schedule_now(current, host_payload)
    elif timezone_name == "local":
        local_now = current.astimezone()
    else:
        try:
            local_now = current.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            local_now = current.astimezone()
    start_hour_value = policy.get("start_hour")
    if start_hour_value is None:
        start_hour_value = deps.default_scheduler_policy["start_hour"]
    end_hour_value = policy.get("end_hour")
    if end_hour_value is None:
        end_hour_value = deps.default_scheduler_policy["end_hour"]
    start_hour = int(str(start_hour_value))
    end_hour = int(str(end_hour_value))
    if start_hour == end_hour:
        return True
    current_hour = local_now.hour
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


def format_eta_seconds(seconds: float | None) -> str | None:
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
    if not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decorate_encode_job_telemetry(
        job: dict[str, Any],
        *,
        encode_job_manifest_totals: Any,
) -> dict[str, Any]:
    decorated = dict(job)
    progress = dict(decorated.get("progress") or {})
    manifest_totals = encode_job_manifest_totals(decorated)
    total_duration_seconds = float(
        progress.get("total_duration_seconds") or manifest_totals["total_duration_seconds"] or 0.0
    )
    total_item_count = int(
        progress.get("total_item_count") or manifest_totals["total_item_count"] or decorated.get("item_count") or 0
    )
    overall_completed_duration_seconds = float(progress.get("overall_completed_duration_seconds") or 0.0)
    remaining_duration_seconds = float(
        progress.get("remaining_duration_seconds")
        or max(total_duration_seconds - overall_completed_duration_seconds, 0.0)
    )
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
        "eta_copy": format_eta_seconds(eta_seconds),
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


def encode_queue_telemetry(encode_queue: dict[str, Any]) -> dict[str, Any]:
    running_jobs = [dict(job) for job in encode_queue.get("running") or []]
    queued_jobs = [dict(job) for job in encode_queue.get("queued") or []]
    aggregate_speed = sum(float(((job.get("progress") or {}).get("speed") or 0.0)) for job in running_jobs)
    total_remaining_duration_seconds = sum(
        float(((job.get("progress") or {}).get("remaining_duration_seconds") or 0.0))
        for job in running_jobs
    )
    total_remaining_duration_seconds += sum(
        float(((job.get("progress") or {}).get("total_duration_seconds") or 0.0)) for job in queued_jobs
    )
    eta_seconds = (total_remaining_duration_seconds / aggregate_speed) if aggregate_speed > 0 else None
    return {
        "aggregate_speed": aggregate_speed or None,
        "eta_seconds": eta_seconds,
        "eta_copy": format_eta_seconds(eta_seconds),
        "running_jobs": len(running_jobs),
        "queued_jobs": len(queued_jobs),
    }


def decorate_encode_job_for_scheduler(
        config: MediaforceConfig,
        job: dict[str, Any] | None,
        deps: EncodeSchedulerDeps,
) -> dict[str, Any] | None:
    if job is None:
        return None
    decorated = dict(job)
    policy = schedule_profile_policy_for_host(config, dict(decorated.get("host") or {}), deps)
    status = str(decorated.get("status") or "")
    bypass_schedule = bool(decorated.get("bypass_schedule"))
    attempt_count = int(decorated.get("attempt_count") or 0)
    waiting_reason = str(decorated.get("waiting_reason") or "").strip()
    schedule_waiting = (
        status == "queued"
        and not scheduler_allows_encode_run(
            policy,
            deps,
            bypass_schedule=bypass_schedule,
            host_payload=dict(decorated.get("host") or {}),
        )
    )
    decorated["schedule_waiting"] = schedule_waiting
    decorated["scheduler_summary"] = str(policy["summary"])
    decorated["attempt_summary"] = (
        f"attempt {attempt_count} of {deps.encode_job_max_attempts}" if attempt_count else "not started yet"
    )
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
    return decorate_encode_job_telemetry(decorated, encode_job_manifest_totals=deps.encode_job_manifest_totals)


def decorate_encode_queue_for_scheduler(
        config: MediaforceConfig,
        encode_queue: dict[str, Any],
        deps: EncodeSchedulerDeps,
) -> dict[str, Any]:
    policy = encode_queue_scheduler_policy(config, deps)
    queue_state = dict(encode_queue.get("state") or {})
    queue_state["scheduler"] = policy
    queue_state["scheduler_summary"] = str(policy["summary"])
    queue_state["schedule_profiles"] = list(encode_queue_schedule_profiles(config, deps).values())
    decorated = dict(encode_queue)
    decorated["state"] = queue_state
    decorated["running"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in encode_queue.get("running") or []
    ]
    decorated["queued"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in encode_queue.get("queued") or []
    ]
    decorated["recent"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in encode_queue.get("recent") or []
    ]
    decorated["queued_waiting_count"] = sum(
        1
        for job in decorated["queued"]
        if bool(job.get("schedule_waiting")) or str(job.get("status") or "") == "retry_backoff"
    )
    decorated["telemetry"] = encode_queue_telemetry(decorated)
    return decorated


def encode_queue_summary_copy(
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

    if encode_job and encode_job.get("scheduler_status_copy") and status in {"queued", "retry_backoff", "running", "needs_attention"}:
        parts.append(str(encode_job["scheduler_status_copy"]))

    if encode_queue_state.get("is_paused"):
        parts.append("queue paused")

    return " · ".join(parts)
