from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.timezones import system_timezone, valid_timezone_name
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


@dataclass(frozen=True, slots=True)
class EncodeScheduleTransition:
    is_open: bool
    current_closes_at: datetime | None = None
    next_opens_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schedule_open": self.is_open,
            "schedule_closes_at": _transition_timestamp(self.current_closes_at),
            "schedule_next_opens_at": _transition_timestamp(self.next_opens_at),
        }


@dataclass(frozen=True, slots=True)
class _ScheduleInterval:
    starts_at: datetime
    ends_at: datetime


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
    return _current_utc(current).astimezone(_host_schedule_timezone(host_payload))


def _host_schedule_timezone(host_payload: dict[str, Any] | None) -> tzinfo:
    host_data = object_dict(host_payload)
    for key in ("schedule_timezone", "timezone"):
        timezone_name = valid_timezone_name(host_data.get(key))
        if timezone_name:
            return ZoneInfo(timezone_name)
    offset_minutes = host_data.get("utc_offset_minutes")
    try:
        if isinstance(offset_minutes, str | int | float):
            return timezone(timedelta(minutes=int(offset_minutes)))
    except (TypeError, ValueError):
        pass
    return system_timezone()


def evaluate_encode_schedule_transition(
        policy: dict[str, Any],
        deps: EncodeSchedulerDeps,
        *,
        bypass_schedule: bool = False,
        now: datetime | None = None,
        host_payload: dict[str, Any] | None = None,
) -> EncodeScheduleTransition:
    mode = str(policy.get("mode") or "anytime")
    if mode == "never":
        return EncodeScheduleTransition(is_open=False)
    if bypass_schedule or mode == "anytime":
        return EncodeScheduleTransition(is_open=True)

    current_utc = _current_utc(now or datetime.now(UTC))
    schedule_timezone = _schedule_timezone(policy, host_payload)
    current_local_date = current_utc.astimezone(schedule_timezone).date()
    window_days = _schedule_days(policy, "days_of_week")
    all_day_days = _schedule_days(policy, "all_day_days_of_week")
    if not window_days and not all_day_days:
        window_days = frozenset(SCHEDULE_DAY_ORDER)
    start_hour = _schedule_hour(policy, "start_hour", deps)
    end_hour = _schedule_hour(policy, "end_hour", deps)
    if start_hour == end_hour and (window_days | all_day_days) == set(SCHEDULE_DAY_ORDER):
        return EncodeScheduleTransition(is_open=True)
    if all_day_days == set(SCHEDULE_DAY_ORDER):
        return EncodeScheduleTransition(is_open=True)

    intervals = _schedule_intervals(
        anchor_date=current_local_date,
        schedule_timezone=schedule_timezone,
        window_days=window_days,
        all_day_days=all_day_days,
        start_hour=start_hour,
        end_hour=end_hour,
    )
    cycle_start = _local_boundary(current_local_date, 0, schedule_timezone)
    cycle_end = _local_boundary(current_local_date + timedelta(days=7), 0, schedule_timezone)
    if any(interval.starts_at <= cycle_start and interval.ends_at >= cycle_end for interval in intervals):
        return EncodeScheduleTransition(is_open=True)
    active_interval = next(
        (interval for interval in intervals if interval.starts_at <= current_utc < interval.ends_at),
        None,
    )
    if active_interval is not None:
        next_open = next(
            (interval.starts_at for interval in intervals if interval.starts_at > active_interval.ends_at),
            None,
        )
        return EncodeScheduleTransition(
            is_open=True,
            current_closes_at=active_interval.ends_at,
            next_opens_at=next_open,
        )
    next_open = next((interval.starts_at for interval in intervals if interval.starts_at > current_utc), None)
    return EncodeScheduleTransition(is_open=False, next_opens_at=next_open)


def scheduler_allows_encode_run(
        policy: dict[str, Any],
        deps: EncodeSchedulerDeps,
        *,
        bypass_schedule: bool = False,
        now: datetime | None = None,
        host_payload: dict[str, Any] | None = None,
) -> bool:
    return evaluate_encode_schedule_transition(
        policy,
        deps,
        bypass_schedule=bypass_schedule,
        now=now,
        host_payload=host_payload,
    ).is_open


def max_encode_schedule_window_seconds(
        policy: dict[str, Any],
        deps: EncodeSchedulerDeps,
        *,
        now: datetime | None = None,
        host_payload: dict[str, Any] | None = None,
) -> float | None:
    mode = str(policy.get("mode") or "anytime")
    if mode == "anytime":
        return None
    if mode == "never":
        return 0.0
    current_utc = _current_utc(now or datetime.now(UTC))
    schedule_timezone = _schedule_timezone(policy, host_payload)
    current_local_date = current_utc.astimezone(schedule_timezone).date()
    window_days = _schedule_days(policy, "days_of_week")
    all_day_days = _schedule_days(policy, "all_day_days_of_week")
    if not window_days and not all_day_days:
        window_days = frozenset(SCHEDULE_DAY_ORDER)
    intervals = _schedule_intervals(
        anchor_date=current_local_date,
        schedule_timezone=schedule_timezone,
        window_days=window_days,
        all_day_days=all_day_days,
        start_hour=_schedule_hour(policy, "start_hour", deps),
        end_hour=_schedule_hour(policy, "end_hour", deps),
    )
    durations = [
        (interval.ends_at - interval.starts_at).total_seconds()
        for interval in intervals
        if interval.ends_at > interval.starts_at
    ]
    if not durations:
        return 0.0
    longest = max(durations)
    if longest >= timedelta(days=7).total_seconds():
        return None
    return longest


def _current_utc(current: datetime) -> datetime:
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _schedule_timezone(policy: dict[str, Any], host_payload: dict[str, Any] | None) -> tzinfo:
    timezone_scope = str(policy.get("timezone") or "controller_local").strip()
    if timezone_scope == "host_local":
        return _host_schedule_timezone(host_payload)
    if timezone_scope in {"controller_local", "local"}:
        return system_timezone()
    if timezone_scope == "utc":
        return UTC
    timezone_name = valid_timezone_name(timezone_scope)
    return ZoneInfo(timezone_name) if timezone_name else system_timezone()


def _schedule_days(policy: dict[str, Any], key: str) -> frozenset[str]:
    return frozenset({
        str(day).strip().lower()
        for day in object_list(policy.get(key))
        if str(day).strip().lower() in SCHEDULE_DAY_ORDER
    })


def _schedule_hour(policy: dict[str, Any], key: str, deps: EncodeSchedulerDeps) -> int:
    value = policy.get(key)
    if value is None:
        value = deps.default_scheduler_policy[key]
    return max(0, min(23, int(str(value))))


@lru_cache(maxsize=512)
def _schedule_intervals(
        *,
        anchor_date: date,
        schedule_timezone: tzinfo,
        window_days: frozenset[str],
        all_day_days: frozenset[str],
        start_hour: int,
        end_hour: int,
) -> tuple[_ScheduleInterval, ...]:
    intervals: list[_ScheduleInterval] = []
    for day_offset in range(-1, 16):
        local_date = anchor_date + timedelta(days=day_offset)
        day_name = SCHEDULE_DAY_ORDER[local_date.weekday()]
        if day_name in all_day_days or (day_name in window_days and start_hour == end_hour):
            intervals.append(_local_interval(local_date, 0, local_date + timedelta(days=1), 0, schedule_timezone))
            continue
        if start_hour == end_hour:
            continue
        if start_hour < end_hour:
            if day_name in window_days:
                intervals.append(_local_interval(local_date, start_hour, local_date, end_hour, schedule_timezone))
            continue
        previous_day_name = SCHEDULE_DAY_ORDER[(local_date.weekday() - 1) % len(SCHEDULE_DAY_ORDER)]
        if end_hour > 0 and (day_name in window_days or previous_day_name in window_days):
            intervals.append(_local_interval(local_date, 0, local_date, end_hour, schedule_timezone))
        if day_name in window_days:
            intervals.append(_local_interval(local_date, start_hour, local_date + timedelta(days=1), 0, schedule_timezone))
    return tuple(_merge_schedule_intervals(intervals))


def _local_interval(
        start_date: date,
        start_hour: int,
        end_date: date,
        end_hour: int,
        schedule_timezone: tzinfo,
) -> _ScheduleInterval:
    return _ScheduleInterval(
        starts_at=_local_boundary(start_date, start_hour, schedule_timezone),
        ends_at=_local_boundary(end_date, end_hour, schedule_timezone),
    )


def _local_boundary(local_date: date, hour: int, schedule_timezone: tzinfo) -> datetime:
    local_wall_time = datetime.combine(local_date, time(hour=hour))
    candidates = _valid_boundary_candidates(local_wall_time, schedule_timezone)
    if candidates:
        return min(candidates)
    for minute_offset in range(1, 181):
        candidates = _valid_boundary_candidates(local_wall_time + timedelta(minutes=minute_offset), schedule_timezone)
        if candidates:
            return min(candidates)
    raise ValueError(f"Could not resolve schedule boundary for {local_wall_time.isoformat()}")


def _valid_boundary_candidates(local_wall_time: datetime, schedule_timezone: tzinfo) -> list[datetime]:
    candidates: set[datetime] = set()
    for fold in (0, 1):
        localized = local_wall_time.replace(tzinfo=schedule_timezone, fold=fold)
        candidate_utc = localized.astimezone(UTC)
        round_trip = candidate_utc.astimezone(schedule_timezone).replace(tzinfo=None)
        if round_trip == local_wall_time:
            candidates.add(candidate_utc)
    return sorted(candidates)


def _merge_schedule_intervals(intervals: list[_ScheduleInterval]) -> list[_ScheduleInterval]:
    merged: list[_ScheduleInterval] = []
    for interval in sorted(intervals, key=lambda item: item.starts_at):
        if interval.ends_at <= interval.starts_at:
            continue
        if not merged or interval.starts_at > merged[-1].ends_at:
            merged.append(interval)
            continue
        previous = merged[-1]
        merged[-1] = _ScheduleInterval(previous.starts_at, max(previous.ends_at, interval.ends_at))
    return merged


def _transition_timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value is not None else None


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
