from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list

SCHEDULE_DAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    config_raw = object_dict(config.raw)
    encode_queue = object_dict(config_raw.get("encode_queue"))
    raw = encode_queue.get("scheduler")
    if not isinstance(raw, dict):
        legacy_queue = object_dict(config_raw.get("heavy_queue"))
        raw = legacy_queue.get("scheduler")
    return deps.normalize_encode_queue_scheduler(raw if isinstance(raw, dict) else None)


def encode_queue_schedule_profiles(config: MediaforceConfig, deps: EncodeSchedulerDeps) -> dict[str, dict[str, Any]]:
    always = deps.normalize_encode_queue_scheduler({"mode": "anytime", "timezone": "host_local"})
    always["key"] = deps.always_schedule_profile
    always["label"] = "Always"
    never = deps.normalize_encode_queue_scheduler({"mode": "never", "timezone": "host_local"})
    never["key"] = "never"
    never["label"] = "Never"
    profiles = {deps.always_schedule_profile: always, "never": never}
    raw_profiles = object_list(object_dict(object_dict(config.raw).get("encode_queue")).get("schedule_profiles"))
    if not raw_profiles:
        return profiles
    for raw_profile in raw_profiles:
        profile = object_dict(raw_profile)
        if not profile:
            continue
        key = deps.canonical_schedule_profile_key(str(profile.get("key") or profile.get("name") or ""))
        if not key or key in {deps.always_schedule_profile, "never"}:
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
    host_data = object_dict(host_payload)
    profile_key = deps.canonical_schedule_profile_key(
        host_data.get("schedule_profile") or deps.default_host_schedule_profile
    )
    if profile_key in profiles:
        selected_profile = profiles[profile_key]
    else:
        selected_profile = profiles[deps.default_host_schedule_profile]
    return dict(selected_profile)


def encode_queue_scheduler_summary(policy: dict[str, Any]) -> str:
    mode = str(policy.get("mode") or "anytime")
    if mode == "night":
        return f"window {int(policy['start_hour']):02d}:00-{int(policy['end_hour']):02d}:00 in host local time"
    if mode == "never":
        return "disabled"
    return "runs anytime"


def host_schedule_now(current: datetime, host_payload: dict[str, Any] | None) -> datetime:
    host_data = object_dict(host_payload)
    offset_minutes = host_data.get("utc_offset_minutes")
    try:
        if isinstance(offset_minutes, str | int | float):
            return current.astimezone(timezone(timedelta(minutes=int(offset_minutes))))
    except (TypeError, ValueError):
        pass
    timezone_name = str(host_data.get("schedule_timezone") or host_data.get("timezone") or "").strip()
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
    mode = str(policy.get("mode") or "anytime")
    if mode == "never":
        return False
    if bypass_schedule or mode == "anytime":
        return True
    current = datetime.now(UTC) if now is None else now
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
    current_day = SCHEDULE_DAY_ORDER[local_now.weekday()]
    full_day_days = [
        day
        for day in object_list(policy.get("all_day_days_of_week"))
        if str(day).strip().lower() in SCHEDULE_DAY_ORDER
    ]
    if current_day in full_day_days:
        return True
    allowed_days = [
        day for day in object_list(policy.get("days_of_week")) if str(day).strip().lower() in SCHEDULE_DAY_ORDER
    ]
    if not allowed_days and full_day_days:
        return False
    start_hour_value = policy.get("start_hour")
    if start_hour_value is None:
        start_hour_value = deps.default_scheduler_policy["start_hour"]
    end_hour_value = policy.get("end_hour")
    if end_hour_value is None:
        end_hour_value = deps.default_scheduler_policy["end_hour"]
    start_hour = int(str(start_hour_value))
    end_hour = int(str(end_hour_value))
    if start_hour == end_hour:
        if allowed_days and current_day not in allowed_days:
            return False
        return True
    current_hour = local_now.hour
    if allowed_days:
        if start_hour < end_hour and current_day not in allowed_days:
            return False
        if start_hour > end_hour:
            if current_hour >= start_hour and current_day not in allowed_days:
                return False
            if current_hour < end_hour:
                previous_day = SCHEDULE_DAY_ORDER[(local_now.weekday() - 1) % len(SCHEDULE_DAY_ORDER)]
                if previous_day not in allowed_days and current_day not in allowed_days:
                    return False
            if end_hour <= current_hour < start_hour:
                return False
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


def _float_or_none(value: object | None) -> float | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _telemetry_speed_for_eta(job: dict[str, Any]) -> float:
    progress = object_dict(job.get("progress"))
    eta_speed = _float_or_none(progress.get("eta_speed"))
    if eta_speed is not None and eta_speed > 0:
        return eta_speed
    speed = _float_or_none(progress.get("speed"))
    return speed if speed is not None and speed > 0 else 0.0


def decorate_encode_job_telemetry(
        job: dict[str, Any],
        *,
        encode_job_manifest_totals: Any,
) -> dict[str, Any]:
    decorated = dict(job)
    progress = object_dict(decorated.get("progress"))
    manifest_totals = object_dict(encode_job_manifest_totals(decorated))
    total_duration_seconds = float_value(
        progress.get("total_duration_seconds") or manifest_totals.get("total_duration_seconds")
    )
    total_item_count = int_value(
        progress.get("total_item_count") or manifest_totals.get("total_item_count") or decorated.get("item_count")
    )
    overall_completed_duration_seconds = float_value(progress.get("overall_completed_duration_seconds"))
    remaining_duration_seconds = float_value(
        progress.get("remaining_duration_seconds")
        or max(total_duration_seconds - overall_completed_duration_seconds, 0.0)
    )
    percent_complete = _float_or_none(progress.get("percent_complete"))
    if percent_complete is None and total_duration_seconds > 0:
        percent_complete = min(overall_completed_duration_seconds / total_duration_seconds, 1.0) * 100.0
    fps = _float_or_none(progress.get("fps"))
    speed = _float_or_none(progress.get("speed"))
    eta_seconds = _float_or_none(progress.get("eta_seconds"))
    progress_state = str(progress.get("progress_state") or "").strip().lower()
    phase_label = str(progress.get("phase_label") or "").strip()
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
    include_numeric_summary = progress_state not in {"quality_search", "starting"}
    if percent_complete is not None and include_numeric_summary:
        summary_parts.append(f"{percent_complete:.0f}%")
    if speed_value is not None and include_numeric_summary:
        summary_parts.append(f"{speed:.2f}x")
    if fps not in {None, 0, 0.0} and include_numeric_summary:
        summary_parts.append(f"{fps:.1f} fps")
    eta_copy = object_dict(decorated["progress"]).get("eta_copy")
    if eta_copy and include_numeric_summary:
        summary_parts.append(f"Est. ETA {eta_copy}")
    if summary_parts:
        decorated["telemetry_summary"] = " · ".join(summary_parts)
    elif progress_state == "quality_search":
        decorated["telemetry_summary"] = phase_label or "Searching quality"
    elif progress_state == "starting":
        decorated["telemetry_summary"] = phase_label or "Starting encode"
    else:
        decorated["telemetry_summary"] = phase_label
    return decorated


def encode_queue_telemetry(encode_queue: dict[str, Any]) -> dict[str, Any]:
    running_jobs = [object_dict(job) for job in object_list(encode_queue.get("running"))]
    queued_jobs = [object_dict(job) for job in object_list(encode_queue.get("queued"))]
    aggregate_speed = sum(_telemetry_speed_for_eta(job) for job in running_jobs)
    total_remaining_duration_seconds = sum(
        float_value(object_dict(job.get("progress")).get("remaining_duration_seconds"))
        for job in running_jobs
    )
    total_remaining_duration_seconds += sum(
        float_value(object_dict(job.get("progress")).get("total_duration_seconds")) for job in queued_jobs
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
    host_payload = object_dict(decorated.get("host"))
    policy = schedule_profile_policy_for_host(config, host_payload, deps)
    status = str(decorated.get("status") or "")
    bypass_schedule = bool(decorated.get("bypass_schedule"))
    attempt_count = int_value(decorated.get("attempt_count"))
    waiting_reason = str(decorated.get("waiting_reason") or "").strip()
    progress = object_dict(decorated.get("progress"))
    has_started = bool(str(decorated.get("started_at") or "").strip())
    if not has_started:
        has_started = bool(str(progress.get("progress_state") or "").strip()) or (
            float_value(progress.get("overall_completed_duration_seconds")) > 0.0
        )
    effective_attempt_count = attempt_count if attempt_count > 0 else (1 if has_started else 0)
    display_attempt_count = min(effective_attempt_count, deps.encode_job_max_attempts)
    schedule_waiting = (
        status == "queued"
        and not scheduler_allows_encode_run(
            policy,
            deps,
            bypass_schedule=bypass_schedule,
            host_payload=host_payload,
        )
    )
    decorated["schedule_waiting"] = schedule_waiting
    decorated["scheduler_summary"] = str(policy["summary"])
    decorated["attempt_summary"] = (
        f"attempt {display_attempt_count} of {deps.encode_job_max_attempts}"
        if display_attempt_count
        else "not started yet"
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
    queue_state = object_dict(encode_queue.get("state"))
    queue_state["scheduler"] = policy
    queue_state["scheduler_summary"] = str(policy["summary"])
    queue_state["schedule_profiles"] = list(encode_queue_schedule_profiles(config, deps).values())
    decorated = dict(encode_queue)
    decorated["state"] = queue_state
    decorated["running"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in object_list(encode_queue.get("running"))
    ]
    decorated["queued"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in object_list(encode_queue.get("queued"))
    ]
    decorated["recent"] = [
        decorate_encode_job_for_scheduler(config, job, deps) or job
        for job in object_list(encode_queue.get("recent"))
    ]
    decorated["queued_waiting_count"] = sum(
        1
        for job in decorated["queued"]
        if (
            bool(job.get("schedule_waiting"))
            or str(job.get("status") or "") == "retry_backoff"
            or bool(str(job.get("waiting_reason") or "").strip())
        )
    )
    decorated["telemetry"] = encode_queue_telemetry(decorated)
    return decorated


def encode_queue_summary_copy(
        encode_queue: dict[str, Any],
        encode_queue_state: dict[str, Any],
        encode_job: dict[str, Any] | None,
) -> str:
    parts = [
        f"{int_value(encode_queue.get('running_count'))} running",
        f"{int_value(encode_queue.get('queued_count'))} queued",
    ]

    status = str(encode_job.get("status") or "") if encode_job else ""
    if status == "queued" and encode_job and encode_job.get("queue_position"):
        queue_position = int_value(encode_job.get("queue_position"))
        queue_depth = int_value(encode_job.get("queue_depth") or queue_position)
        parts.append(f"this folder is {queue_position} of {queue_depth}")
    elif status == "running":
        parts.append("this folder is active now")
    elif status == "needs_attention":
        parts.append("latest folder job needs attention")
    elif status in {"completed", "failed", "stopped"}:
        parts.append(f"latest folder job {status}")
    else:
        parts.append("no folder job queued yet")

    waiting_count = int_value(encode_queue.get("queued_waiting_count"))
    if waiting_count:
        parts.append(f"{waiting_count} waiting")

    queue_eta_copy = str(object_dict(encode_queue.get("telemetry")).get("eta_copy") or "").strip()
    if queue_eta_copy:
        parts.append(f"estimated queue finish in {queue_eta_copy}")

    if encode_job and encode_job.get("scheduler_status_copy") and status in {"queued", "retry_backoff", "running", "needs_attention"}:
        parts.append(str(encode_job["scheduler_status_copy"]))

    if encode_queue_state.get("is_paused"):
        parts.append("queue paused")

    return " · ".join(parts)
