from __future__ import annotations

import functools
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.core.type_defs import float_value, object_dict
from mediaforce.encoding.duration_estimate import estimate_encode_duration, host_identity_tokens, load_encode_duration_samples
from mediaforce.hosts.config import host_targets_current_machine
from mediaforce.web.runtime.app_window import season_identity
from mediaforce.web.runtime.encode_scheduler import (
    encode_queue_schedule_profiles, evaluate_encode_schedule_transition, schedule_policy_deps, schedule_profile_policy_for_host,
)

RECENT_FILE_LIMIT = 12
WEEK = timedelta(days=7)
TYPICAL_EPISODE = {"duration_seconds": 3600.0, "height": 1080}
EPISODE_PATTERN = re.compile(r"[Ss]\d{1,2}[Ee](\d{1,3})")
FIX_FIELDS = ("available", "setup_supported", "setup_requires_password", "trust_reset_supported", "issues", "recovery")
STARTING_STATES = {"starting": "getting_ready", "quality_search": "finding_size"}
SYSTEM_PROFILER = "/usr/sbin/system_profiler"


@dataclass(frozen=True, slots=True)
class _Finished:
    host_key: str
    host_label: str
    finished_at: datetime
    bytes_saved: int
    replaced: bool
    rel_path: str


def computers_payload(
        connection: DBClient, config: MediaforceConfig, computers: Iterable[Mapping[str, Any]], *, observed_at: datetime,
) -> dict[str, Any]:
    finished = _finished_files(connection)
    samples = load_encode_duration_samples(connection)
    deps = schedule_policy_deps()
    profiles = encode_queue_schedule_profiles(config, deps)
    rows = []
    for computer in computers:
        tokens = host_identity_tokens(dict(computer))
        own = [file for file in finished if _made_by(file, tokens)]
        policy = schedule_profile_policy_for_host(config, dict(computer), deps, profiles=profiles)
        estimate = estimate_encode_duration(TYPICAL_EPISODE, dict(computer), samples)
        rows.append({
            "key": computer["key"],
            "name": computer.get("label"),
            "model": _model(computer),
            "this_mac": _is_this_mac(computer),
            "platform": computer.get("platform"),
            "last_answered_at": _last_answered_at(computer),
            "abilities": list(computer.get("capabilities") or []),
            "library_kinds": _library_kinds(config, computer.get("allowed_libraries") or []),
            "storage": _storage(computer),
            "window": {
                "name": str(policy.get("label") or ""),
                "open": bool(computer.get("schedule_open")),
                "closes_at": computer.get("schedule_closes_at"),
                "opens_at": computer.get("schedule_next_opens_at"),
                "week": _open_windows(computer, policy, *_this_week(observed_at)),
            },
            "running": [_running(job) for job in computer.get("running_jobs") or []],
            "done": _done(own, observed_at),
            "hour_takes_seconds": estimate.total_seconds if estimate.host_sample_size else None,
            "recent": [_recent(file) for file in own[:RECENT_FILE_LIMIT]],
            **{field: computer.get(field) for field in FIX_FIELDS},
        })
    return {"observed_at": observed_at.isoformat(timespec="seconds"), "computers": rows}


def _finished_files(connection: DBClient) -> list[_Finished]:
    rows = connection.execute(
        select(
            staged_artifacts.c.encode_host_key,
            staged_artifacts.c.encode_host_label,
            staged_artifacts.c.encode_completed_at,
            staged_artifacts.c.bytes_saved,
            staged_artifacts.c.promoted_at,
            library_items.c.rel_path,
        )
        .select_from(staged_artifacts.join(library_items, library_items.c.id == staged_artifacts.c.library_item_id))
        .where(staged_artifacts.c.encode_completed_at.is_not(None))
        .order_by(staged_artifacts.c.encode_completed_at.desc())
    ).mappings()
    return [
        _Finished(
            host_key=str(row["encode_host_key"] or "").strip().casefold(),
            host_label=str(row["encode_host_label"] or "").strip().casefold(),
            finished_at=finished_at,
            bytes_saved=int(row["bytes_saved"] or 0),
            replaced=row["promoted_at"] is not None,
            rel_path=str(row["rel_path"]),
        )
        for row in rows
        if (finished_at := _timestamp(row["encode_completed_at"])) is not None
    ]


def _made_by(file: _Finished, tokens: set[str]) -> bool:
    return file.host_key in tokens or file.host_label in tokens


def _done(files: list[_Finished], observed_at: datetime) -> dict[str, Any]:
    local_now = observed_at.astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": _tally(file for file in files if file.finished_at >= midnight),
        "week": _tally(file for file in files if file.finished_at >= local_now - WEEK),
        "all": _tally(files),
        "since": files[-1].finished_at.isoformat() if files else None,
    }


def _tally(files: Iterable[_Finished]) -> dict[str, int]:
    chosen = list(files)
    return {"files": len(chosen), "bytes_saved": sum(file.bytes_saved for file in chosen if file.replaced)}


def _recent(file: _Finished) -> dict[str, Any]:
    return {**_file_identity(file.rel_path), "finished_at": file.finished_at.isoformat(), "bytes_saved": file.bytes_saved,
            "replaced": file.replaced}


def _running(job: Mapping[str, Any]) -> dict[str, Any]:
    progress = object_dict(job.get("progress"))
    rel_path = str(progress.get("current_item_rel_path") or progress.get("rel_path") or "")
    identity = _file_identity(rel_path) if rel_path else {**season_identity(str(job["prefix"])), "episode": None}
    percent = progress.get("percent_complete")
    stage = STARTING_STATES.get(str(progress.get("progress_state") or "").lower(), "compressing")
    eta = float_value(progress.get("eta_seconds"))
    updated = _timestamp(progress.get("updated_at"))
    return {
        "id": job["job_id"], **identity, "stage": stage,
        "progress": min(1.0, max(0.0, float_value(percent) / 100)) if percent is not None and stage == "compressing" else None,
        "finishes_at": (updated + timedelta(seconds=eta)).isoformat() if eta > 0 and updated else None,
        "file_count": int(job.get("item_count") or 1),
    }


def _file_identity(rel_path: str) -> dict[str, Any]:
    path = PurePosixPath(rel_path)
    match = EPISODE_PATTERN.search(path.name)
    return {**season_identity(str(path.parent)), "episode": int(match.group(1)) if match else None}


def _this_week(observed_at: datetime) -> tuple[datetime, datetime]:
    start = observed_at.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + WEEK


def _open_windows(computer: Mapping[str, Any], policy: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, str]]:
    deps = schedule_policy_deps()
    windows = []
    cursor = start
    while cursor < end:
        transition = evaluate_encode_schedule_transition(policy, deps, now=cursor, host_payload=dict(computer))
        if transition.is_open:
            close = min(transition.current_closes_at or end, end)
            windows.append({"starts_at": cursor.isoformat(), "ends_at": close.isoformat()})
            cursor = close
        elif transition.next_opens_at is not None:
            cursor = transition.next_opens_at
        else:
            break
    return windows


def _last_answered_at(computer: Mapping[str, Any]) -> str | None:
    recovery = object_dict(computer.get("recovery"))
    if not recovery:
        return None
    if recovery.get("status") == "ready":
        return recovery.get("last_check_at")
    return recovery.get("first_failure_at")


def _storage(computer: Mapping[str, Any]) -> str:
    if computer.get("media_access") == "stream":
        return "sent"
    if computer.get("missing_mounts") or computer.get("controller_storage_issue"):
        return "missing"
    return "connected"


def _library_kinds(config: MediaforceConfig, allowed: Iterable[str]) -> list[str]:
    types = config.library_type_map
    return sorted({types.get(str(key), "other") for key in allowed})


def _is_this_mac(computer: Mapping[str, Any]) -> bool:
    return host_targets_current_machine({"host": computer.get("host") or computer.get("key")})


def _model(computer: Mapping[str, Any]) -> str | None:
    model = computer.get("computer_model")
    if model or computer.get("platform") != "macos" or not _is_this_mac(computer):
        return model
    return this_mac_model()


@functools.cache
def this_mac_model() -> str | None:
    try:
        result = subprocess.run([SYSTEM_PROFILER, "SPHardwareDataType"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        label, _, value = line.strip().partition(":")
        if label == "Model Name" and value.strip():
            return value.strip()
    return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed and parsed.tzinfo is None else parsed
