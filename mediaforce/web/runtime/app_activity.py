from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.type_defs import float_value, object_dict
from mediaforce.encoding.plain_failure import plain_failure_summary
from mediaforce.encoding.duration_estimate import estimate_encode_job_duration, estimate_fits_before_schedule_close, load_encode_duration_samples
from mediaforce.encoding.encode_queue import EncodeRunFile, RUNNABLE_ENCODE_JOB_KINDS, encode_run_manifest_files, list_encode_jobs, load_queue_state
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.encode_runtime import _encode_job_estimate_items, _host_allows_library
from mediaforce.web.runtime.encode_scheduler import (
    encode_queue_schedule_profiles, evaluate_encode_schedule_transition, schedule_policy_deps, schedule_profile_policy_for_host,
)


def activity_payload(
        connection: DBClient, config: MediaforceConfig, computers: list[dict[str, Any]], *, observed_at: datetime,
        next_season: str | None, storage_admits_work: bool,
) -> dict[str, Any]:
    until = observed_at + timedelta(hours=24)
    deps = schedule_policy_deps()
    profiles = encode_queue_schedule_profiles(config, deps)
    rows = []
    slots: list[dict[str, Any]] = []
    for computer in computers:
        policy = schedule_profile_policy_for_host(config, computer, deps, profiles=profiles)
        windows = _windows(computer, policy, observed_at, until)
        running = [_running(job, observed_at) for job in computer.get("running_jobs") or []]
        rows.append({"key": computer["key"], "windows": windows, "running": running,
                     "finish_current_file": policy.get("finish_current_file") is True})
        if not computer.get("available") or computer.get("controller_storage_issue") or computer.get("active_flag") == "cooldown":
            continue
        if "encode_queue" not in computer.get("capabilities", []):
            continue
        capacity = int(computer.get("max_parallel_encodes") or 1)
        for index in range(capacity):
            if len(running) <= index < int(computer.get("active_encode_count") or 0):
                continue
            active = running[index] if index < len(running) else None
            available = _date(active.get("finishes_at")) if active else observed_at
            if available is not None:
                slots.append({"computer": computer, "windows": windows, "available": available})
    count = connection.execute(select(func.count()).select_from(encode_jobs)).scalar_one()
    jobs = list_encode_jobs(connection, statuses=("queued", "retry_backoff"), limit=count, job_kinds=RUNNABLE_ENCODE_JOB_KINDS)
    samples = load_encode_duration_samples(connection)
    manifest_items_cache: dict[Path, list[dict[str, Any]] | None] = {}
    paused = bool(load_queue_state(connection).get("is_paused")) or background_work_is_paused(connection)
    lined_up = []
    for job in jobs:
        files = encode_run_manifest_files(job, strict_indexes=True)
        entry = {"id": job["job_id"], **season_identity(job["prefix"]),
                 "title": _job_title(job, files),
                 "starts_at": None, "finishes_at": None, "computer_key": None, "computer_name": None,
                 "waiting": "Waiting for a computer with an open work window"}
        lined_up.append(entry)
        if paused:
            entry["waiting"] = "Paused. Starts after you resume work"
            continue
        if not storage_admits_work:
            entry["waiting"] = "Waiting for shared storage"
            continue
        if job["status"] == "retry_backoff":
            entry["waiting"] = plain_failure_summary(waiting_reason=job.get("waiting_reason")) or "Waiting for its retry check"
            continue
        items = _encode_job_estimate_items(job, manifest_items_cache=manifest_items_cache, strict_indexes=True)
        choices = []
        for slot in slots:
            computer = slot["computer"]
            if slot["available"] is None or not _host_allows_library(computer, job["prefix"].split("/")[0].lower()):
                continue
            if not items or any(float_value(item.get("duration_seconds")) <= 0 for item in items):
                slot["available"] = None
                continue
            estimate = estimate_encode_job_duration(items, computer, samples)
            if not estimate.host_sample_size:
                slot["available"] = None
                continue
            for window in slot["windows"]:
                start = max(slot["available"], datetime.fromisoformat(window["starts_at"]))
                finish = start + timedelta(seconds=estimate.total_seconds)
                if start < until and estimate_fits_before_schedule_close(estimate, {"schedule_closes_at": window["closes_at"]}, now=start):
                    choices.append((start, finish, slot))
                    break
        if choices:
            start, finish, slot = min(choices, key=lambda choice: choice[0])
            slot["available"] = finish
            entry.update(starts_at=start.isoformat(), finishes_at=finish.isoformat(),
                         computer_key=slot["computer"]["key"], computer_name=slot["computer"].get("label"), waiting=None)
        else:
            entry["waiting"] = plain_failure_summary(waiting_reason=job.get("waiting_reason")) or "Start time and computer are not known yet"
    earlier_holds = []
    for job in list_encode_jobs(connection, statuses=("needs_attention",), limit=count, job_kinds=("single", "folder")):
        files = encode_run_manifest_files(job, strict_indexes=True)
        earlier_holds.append(
            {"id": job["job_id"], **season_identity(job["prefix"]), "title": _job_title(job, files),
             "starts_at": None, "finishes_at": None, "computer_key": None, "computer_name": None,
             "waiting": plain_failure_summary(job.get("error"), waiting_reason=job.get("waiting_reason"),
                                              failure_kind=job.get("last_failure_kind"))}
        )
    return {"starts_at": observed_at.isoformat(), "ends_at": until.isoformat(), "computers": rows,
            "lined_up": sorted(lined_up, key=lambda file: file["starts_at"] or "9999"), "earlier_holds": earlier_holds, "next_season": season_identity(next_season) if next_season else None}


def _windows(computer: dict[str, Any], policy: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, str]]:
    windows = []
    cursor = start
    deps = schedule_policy_deps()
    while cursor < end:
        transition = evaluate_encode_schedule_transition(policy, deps, now=cursor, host_payload=computer)
        if transition.is_open:
            close = transition.current_closes_at or end
            windows.append({"starts_at": cursor.isoformat(), "ends_at": close.isoformat(), "closes_at": transition.current_closes_at.isoformat() if transition.current_closes_at else ""})
            cursor = close
        elif transition.next_opens_at is not None:
            cursor = transition.next_opens_at
        else:
            break
    return windows


def _running(job: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    progress = object_dict(job.get("progress"))
    seconds = float_value(progress.get("eta_seconds"))
    prefix = str(job["prefix"])
    rel_path = str(progress.get("current_item_rel_path") or progress.get("rel_path") or "")
    percent = progress.get("percent_complete")
    return {"id": job["job_id"], **season_identity(prefix), "title": _title(rel_path) if rel_path else _season_title(prefix),
            "starts_at": now.isoformat(), "finishes_at": (now + timedelta(seconds=seconds)).isoformat() if seconds > 0 else None,
            "progress": min(1.0, max(0.0, float_value(percent) / 100)) if percent is not None else None}


def _job_title(job: Mapping[str, Any], files: list[EncodeRunFile]) -> str:
    if len(files) == 1:
        return _title(files[0].rel_path)
    count = len(files) or int(job.get("item_count") or 0)
    title = _season_title(str(job["prefix"]))
    return f"{title}, {count} files" if count > 1 else title


def _title(path: str) -> str:
    return PurePosixPath(path).stem.replace(".", " ").replace("_", " ")


def _season_title(prefix: str) -> str:
    identity = season_identity(prefix)
    return f"{identity['show']}, Season {identity['season']}" if identity["season"] is not None else identity["show"]


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
