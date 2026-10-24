from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, motion_keep_decisions, staged_artifacts
from mediaforce.encoding.encode_queue import count_pending_encode_work, load_queue_state
from mediaforce.library.candidate_selection import SEASON_PATTERN
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.motion_keep_state import library_item_source_sql
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.web.runtime.host_runtime import running_encode_counts_by_host
from mediaforce.web.runtime.host_status import is_awaiting_first_check

SCHEMA_VERSION = 1
RECENT_SEASON_LIMIT = 12
BIGGEST_SHOW_LIMIT = 10
EVENT_LIMIT = 12
PROMOTED = "promoted"
EVENT_KINDS = (
    "encoding_completed",
    "encoding_failed",
    "encoding_stopped",
    "validation_completed",
    "promotion_completed",
)
WORKING_COMPUTER_STATES = frozenset({"ready", "busy"})


@dataclass(slots=True)
class _Item:
    item_id: int
    rel_path: str
    kind: str
    size_bytes: int
    promoted: bool

    @property
    def parts(self) -> tuple[str, ...]:
        return PurePosixPath(self.rel_path).parts

    @property
    def show_prefix(self) -> str | None:
        return "/".join(self.parts[:2]) if self.kind == "tv" and len(self.parts) >= 3 else None

    @property
    def season_prefix(self) -> str | None:
        return "/".join(self.parts[:3]) if self.kind == "tv" and len(self.parts) >= 4 else None


@dataclass(slots=True)
class _Promotion:
    bytes_saved: int
    promoted_at: str | None
    source_size_bytes: int
    has_backup: bool


@dataclass(slots=True)
class _Season:
    prefix: str
    item_ids: list[int] = field(default_factory=list)


def app_window_payload(
        connection: DBClient,
        *,
        library_types: Mapping[str, str],
        season_cards: Iterable[Mapping[str, Any]],
        computers: Iterable[Mapping[str, Any]],
        controller_storage: Mapping[str, Any],
        storage_admits_work: bool,
        observed_at: datetime,
) -> dict[str, Any]:
    items = _present_items(connection, library_types)
    promotions = _promotions(connection)
    kept = _kept_original_rows(connection)
    seasons = _seasons(items)
    cards = [card for card in season_cards if card.get("prefix")]
    samples = _samples(cards)
    finished = _finished_seasons(items, promotions, seasons, kept)
    events = _events(connection)
    computer_states = [_computer(row) for row in computers]
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "work": _work(connection, controller_storage, storage_admits_work, computer_states),
        "library": _library_totals(items),
        "space_saved": _space_saved(items, promotions, finished),
        "recently_finished_seasons": finished[:RECENT_SEASON_LIMIT],
        "biggest_not_started": _biggest_not_started(connection, items, promotions, samples),
        "samples": samples,
        "computers": computer_states,
        "events": events,
        "last_change": events[0] if events else None,
        "original_backups": _original_backups(connection, promotions),
        "kept_original": _kept_original(kept, cards),
    }


def _present_items(connection: DBClient, library_types: Mapping[str, str]) -> list[_Item]:
    rows = connection.execute(
        select(
            library_items.c.id,
            library_items.c.rel_path,
            library_items.c.media_root,
            library_items.c.size_bytes,
            library_items.c.status,
        ).where(LIBRARY_ITEM_PRESENT)
    ).mappings()
    return [
        _Item(
            item_id=int(row["id"]),
            rel_path=str(row["rel_path"]),
            kind=library_types.get(str(row["media_root"]), "other"),
            size_bytes=int(row["size_bytes"] or 0),
            promoted=row["status"] == PROMOTED,
        )
        for row in rows
    ]


def _promotions(connection: DBClient) -> dict[int, _Promotion]:
    rows = connection.execute(
        select(
            staged_artifacts.c.library_item_id,
            staged_artifacts.c.bytes_saved,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.source_size_bytes,
            staged_artifacts.c.archived_source_path,
        ).where(staged_artifacts.c.promoted_at.is_not(None))
    ).mappings()
    return {
        int(row["library_item_id"]): _Promotion(
            bytes_saved=int(row["bytes_saved"] or 0),
            promoted_at=row["promoted_at"],
            source_size_bytes=int(row["source_size_bytes"] or 0),
            has_backup=bool(row["archived_source_path"]),
        )
        for row in rows
    }


def _kept_original_rows(connection: DBClient) -> dict[int, Mapping[str, Any]]:
    rows = connection.execute(
        select(motion_keep_decisions.c.library_item_id, motion_keep_decisions.c.prefix, motion_keep_decisions.c.reason_code)
        .select_from(motion_keep_decisions.join(library_items, motion_keep_decisions.c.library_item_id == library_items.c.id))
        .where(LIBRARY_ITEM_PRESENT, motion_keep_decisions.c.source_fingerprint == library_item_source_sql())
    ).mappings()
    return {int(row["library_item_id"]): row for row in rows}


def _seasons(items: list[_Item]) -> dict[str, _Season]:
    seasons: dict[str, _Season] = {}
    for item in items:
        prefix = item.season_prefix
        if prefix is not None:
            seasons.setdefault(prefix, _Season(prefix)).item_ids.append(item.item_id)
    return seasons


def _season_identity(prefix: str) -> dict[str, Any]:
    parts = PurePosixPath(prefix).parts
    match = SEASON_PATTERN.match(parts[2]) if len(parts) >= 3 else None
    return {
        "prefix": prefix,
        "show_prefix": "/".join(parts[:2]),
        "show": parts[1] if len(parts) >= 2 else prefix,
        "season_folder": parts[2] if len(parts) >= 3 else None,
        "season": int(match.group(1)) if match else None,
    }


def _library_totals(items: list[_Item]) -> dict[str, Any]:
    by_kind: dict[str, list[_Item]] = defaultdict(list)
    for item in items:
        by_kind[item.kind].append(item)
    tv = by_kind.get("tv", [])
    movies = by_kind.get("movie", [])
    other = [item for kind, kind_items in by_kind.items() if kind not in {"tv", "movie"} for item in kind_items]
    return {
        "tv": {
            "show_count": len({item.show_prefix for item in tv if item.show_prefix}),
            "season_count": len({item.season_prefix for item in tv if item.season_prefix}),
            "episode_count": len(tv),
            **_size_totals(tv),
        },
        "movies": {
            "title_count": len({"/".join(item.parts[:2]) for item in movies}),
            **_size_totals(movies),
        },
        "other": {"item_count": len(other), **_size_totals(other)},
    }


def _size_totals(items: list[_Item]) -> dict[str, int]:
    return {
        "total_bytes": sum(item.size_bytes for item in items),
        "original_bytes": sum(item.size_bytes for item in items if not item.promoted),
    }


def _is_settled(item_id: int, items_by_id: Mapping[int, _Item], kept: Mapping[int, Any]) -> bool:
    return items_by_id[item_id].promoted or item_id in kept


def _finished_seasons(
        items: list[_Item],
        promotions: Mapping[int, _Promotion],
        seasons: Mapping[str, _Season],
        kept: Mapping[int, Any],
) -> list[dict[str, Any]]:
    items_by_id = {item.item_id: item for item in items}
    finished = []
    for season in seasons.values():
        replaced = [promotions[item_id] for item_id in season.item_ids if item_id in promotions and items_by_id[item_id].promoted]
        if not replaced or not all(_is_settled(item_id, items_by_id, kept) for item_id in season.item_ids):
            continue
        finished.append({
            **_season_identity(season.prefix),
            "episode_count": len(replaced),
            "kept_original_count": sum(1 for item_id in season.item_ids if item_id in kept),
            "bytes_saved": sum(promotion.bytes_saved for promotion in replaced),
            "finished_at": max((promotion.promoted_at or "" for promotion in replaced), default="") or None,
        })
    return sorted(finished, key=lambda finished_season: finished_season["finished_at"] or "", reverse=True)


def _space_saved(items: list[_Item], promotions: Mapping[int, _Promotion], finished: list[dict[str, Any]]) -> dict[str, int]:
    replaced = [item for item in items if item.promoted and item.item_id in promotions]
    return {
        "bytes": sum(promotions[item.item_id].bytes_saved for item in replaced),
        "item_count": len(replaced),
        "episode_count": sum(1 for item in replaced if item.kind == "tv"),
        "season_count": len(finished),
    }


def _shows_with_encode_jobs(connection: DBClient) -> set[str]:
    prefixes = connection.execute(select(encode_jobs.c.prefix).distinct()).scalars()
    return {"/".join(PurePosixPath(str(prefix)).parts[:2]) for prefix in prefixes if prefix}


def _biggest_not_started(
        connection: DBClient,
        items: list[_Item],
        promotions: Mapping[int, _Promotion],
        samples: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    started = _shows_with_encode_jobs(connection) | {
        item.show_prefix for item in items if item.show_prefix and (item.promoted or item.item_id in promotions)
    }
    started |= {sample["show_prefix"] for sample in samples["approved_not_started"]}
    waiting_by_show = {sample["show_prefix"]: sample["prefix"] for sample in reversed(samples["waiting_on_you"])}
    shows: dict[str, list[_Item]] = defaultdict(list)
    for item in items:
        if item.show_prefix and item.show_prefix not in started:
            shows[item.show_prefix].append(item)
    ranked = sorted(shows.items(), key=lambda show: sum(item.size_bytes for item in show[1]), reverse=True)
    return [
        {
            "show_prefix": show_prefix,
            "show": PurePosixPath(show_prefix).parts[1],
            "bytes": sum(item.size_bytes for item in show_items),
            "episode_count": len(show_items),
            "season_count": len({item.season_prefix for item in show_items if item.season_prefix}),
            "waiting_sample_prefix": waiting_by_show.get(show_prefix),
        }
        for show_prefix, show_items in ranked[:BIGGEST_SHOW_LIMIT]
    ]


def _samples(cards: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    waiting, approved = [], []
    for card in cards:
        state = card.get("page_state") or {}
        key = state.get("key")
        entry = {
            **_season_identity(str(card["prefix"])),
            "state": key,
            "action": state.get("primary_action"),
            "failure": state.get("failure"),
        }
        if key == "ready_to_compare" or (key == "needs_help" and state.get("recovery_kind") == "test"):
            waiting.append(entry)
        elif key == "ready_to_make":
            approved.append(entry)
    waiting.sort(key=lambda sample: sample["state"] != "ready_to_compare")
    return {"waiting_on_you": waiting, "approved_not_started": approved}


def _work(
        connection: DBClient,
        controller_storage: Mapping[str, Any],
        storage_admits_work: bool,
        computers: list[dict[str, Any]],
) -> dict[str, Any]:
    paused = bool(load_queue_state(connection).get("is_paused"))
    waiting_count = count_pending_encode_work(connection)
    running_count = sum(running_encode_counts_by_host(connection).values())
    stop_reasons = []
    if not storage_admits_work:
        stop_reasons.append("storage_unavailable")
    if waiting_count and not any(computer["state"] in WORKING_COMPUTER_STATES for computer in computers):
        stop_reasons.append("no_computer_ready")
    if paused:
        state = "paused"
    elif stop_reasons:
        state = "stopped"
    elif running_count:
        state = "running"
    else:
        state = "idle"
    return {
        "state": state,
        "paused_by_owner": paused,
        "stop_reasons": stop_reasons,
        "running_count": running_count,
        "waiting_count": waiting_count,
        "storage": [
            {"mount_point": mount.get("mount_point"), "status": mount.get("status"), "failure": mount.get("last_failure_kind")}
            for mount in controller_storage.get("mounts") or []
        ],
    }


def _computer(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": row.get("key"),
        "name": row.get("label"),
        "platform": row.get("platform"),
        "state": _computer_state(row),
        "running_count": int(row.get("active_encode_count") or 0),
        "schedule_opens_at": row.get("schedule_next_opens_at"),
        "cooldown_until": row.get("cooldown_until"),
    }


def _computer_state(row: Mapping[str, Any]) -> str:
    if is_awaiting_first_check(str(row.get("message") or "")):
        return "checking"
    if not row.get("available"):
        return _unavailable_computer_state(row)
    if int(row.get("active_encode_count") or 0):
        return "busy"
    if "encode_queue" not in {str(capability).lower() for capability in row.get("capabilities") or []}:
        return "not_encoding"
    if row.get("controller_storage_issue"):
        return "storage_unreachable"
    if row.get("active_flag") == "cooldown":
        return "cooling_down"
    if not row.get("schedule_open"):
        return "outside_schedule"
    return "ready" if row.get("queue_active") else "not_ready"


def _unavailable_computer_state(row: Mapping[str, Any]) -> str:
    if row.get("check_failure"):
        return str(row["check_failure"])
    if row.get("missing_mounts") or row.get("controller_storage_issue"):
        return "storage_unreachable"
    return "not_ready"


def _events(connection: DBClient) -> list[dict[str, Any]]:
    rows = connection.execute(
        select(
            item_events.c.id,
            item_events.c.event_type,
            item_events.c.created_at,
            library_items.c.rel_path,
            staged_artifacts.c.bytes_saved,
        )
        .select_from(
            item_events.join(library_items, item_events.c.library_item_id == library_items.c.id).outerjoin(
                staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id
            )
        )
        .where(item_events.c.event_type.in_(EVENT_KINDS))
        .order_by(item_events.c.created_at.desc(), item_events.c.id.desc())
        .limit(EVENT_LIMIT)
    ).mappings()
    return [_event(row) for row in rows]


def _event(row: Mapping[str, Any]) -> dict[str, Any]:
    parts = PurePosixPath(str(row["rel_path"])).parts
    season_prefix = "/".join(parts[:3]) if len(parts) >= 4 else None
    return {
        "id": int(row["id"]),
        "kind": row["event_type"],
        "at": row["created_at"],
        "rel_path": row["rel_path"],
        "season": _season_identity(season_prefix) if season_prefix else None,
        "bytes_saved": int(row["bytes_saved"]) if row["event_type"] == "promotion_completed" and row["bytes_saved"] else None,
    }


def _original_backups(connection: DBClient, promotions: Mapping[int, _Promotion]) -> dict[str, int]:
    removed = confirmed_originals_removed_item_ids(connection)
    waiting = [promotion for item_id, promotion in promotions.items() if promotion.has_backup and item_id not in removed]
    return {"count": len(waiting), "bytes": sum(promotion.source_size_bytes for promotion in waiting)}


def _kept_original(kept: Mapping[int, Mapping[str, Any]], cards: list[Mapping[str, Any]]) -> dict[str, Any]:
    kept_by_season: dict[tuple[str, str], int] = defaultdict(int)
    for row in kept.values():
        kept_by_season[(str(row["prefix"]), str(row["reason_code"]))] += 1
    held = []
    for card in cards:
        state = card.get("page_state") or {}
        reasons = _hold_reason_codes(card.get("lifecycle"))
        if state.get("key") == "motion_held":
            reasons.append("motion_pattern")
        if reasons:
            held.append({**_season_identity(str(card["prefix"])), "reasons": reasons})
    return {
        "kept": [
            {**_season_identity(prefix), "reason": f"motion_{reason}", "item_count": count}
            for (prefix, reason), count in sorted(kept_by_season.items())
        ],
        "kept_item_count": len(kept),
        "held": held,
    }


def _hold_reason_codes(lifecycle: Mapping[str, Any] | None) -> list[str]:
    codes: list[str] = []
    for season in (lifecycle or {}).get("seasons") or []:
        for reason in season.get("hold_reasons") or []:
            code = reason.get("code")
            if code and code not in codes:
                codes.append(code)
    return codes
