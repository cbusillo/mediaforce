"""Kind approvals: one approval covering every TV season of a kind of content (#1047), a "look" on screen.

The Director decided on 2026-10-22 (#517) that one approval may cover every show that measures the same, and on
#1385 that live action is one look. Cartoons also share a look.

A kind approval is a copy of a current folder approval the Director chose to extend, kept in the web state
directory's `kinds` folder, one file per kind. Production approval precedence lives in production_approval;
the season's kind comes from its originals' traits
(library/content_kinds). Every file it covers still meets the per-file quality floor and size hold.
"""

import json
import os
import tempfile
from threading import Lock
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts, item_events
from mediaforce.library.calculation_reuse import LibraryCalculations, reuse_library_calculation
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, RUNNABLE_ENCODE_JOB_KINDS, load_active_encode_jobs_for_prefix, load_queue_state
from mediaforce.core.type_defs import object_dict
from mediaforce.library.animation_probe import CARTOON_TARGET_MB
from mediaforce.ops.runtime_install import update_hold_reason
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.workflow_state import ENCODE_CANDIDATE_STATUSES, derive_item_workflow_state
from mediaforce.library.content_kinds import look_group, season_grouping, season_kind_report
from mediaforce.tuning.calibration_jobs import load_latest_job, load_latest_sample_job, load_latest_jobs_for_prefixes
from mediaforce.tuning.size_goals import tallest_picture
from mediaforce.web.runtime.app_samples import sample_moments
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash
from mediaforce.web.runtime.production_approval import KEEP_FULL_HEIGHT_FIELD, EffectiveApproval, LoadCalibrationState, \
    SampleWait, approval_prefixes, cached_calibration_loader, effective_production_approval, holds_approval, production_approval_contract, \
    offered_library_types, production_calibration, sample_allows_kind, sample_kept_full_height, season_prefix_of
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.web.runtime.page_state import SAMPLE_FAILED_STATUSES, SAMPLE_RUNNING_STATUSES, SAMPLE_WAITING_STATUSES
from mediaforce.web.runtime.production_holds import approval_identity
from mediaforce.web.runtime.encode_runtime import _host_allows_library, available_encode_slots
from mediaforce.web.runtime.file_encode_outcomes import file_encode_outcomes

LIVE_ACTION = "live_action"
CARTOON = "cartoon"
APPROVABLE_KINDS = (LIVE_ACTION, CARTOON)
_group_choice_lock = Lock()
CARTOON_SIZES_MB = (CARTOON_TARGET_MB * 0.75, CARTOON_TARGET_MB, CARTOON_TARGET_MB * 1.5)
# Darker and bright live action merged into live action (#1385); their saved approvals are carried over once.
MERGED_INTO_LIVE_ACTION = ("darker_live_action", "bright_live_action")
KIND_IS_ONE_TIME_CODE = "kind_is_one_time"
APPROVAL_NOT_CURRENT_CODE = "approval_not_current"
SIZE_GOAL_NOT_SCALABLE_CODE = "size_goal_not_scalable"

KindOf = Callable[[MediaforceConfig, str], str | None]


def kind_approval_file(config: MediaforceConfig, kind: str) -> Path:
    return config.paths.web_state_dir / "kinds" / f"{kind}.json"


def load_kind_approval(config: MediaforceConfig, kind: str) -> dict[str, Any] | None:
    path = kind_approval_file(config, kind)
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("kind") == kind else None


def migrate_kind_approvals(config: MediaforceConfig) -> None:
    """Carry a darker or bright live action approval over to live action, unchanged; safe to run any number of times.

    The darker approval wins when both exist; the show the bright one came from keeps its own approval, which still
    governs that show. An old file that holds no approval is removed without becoming one.
    """
    old_files = [kind_approval_file(config, kind) for kind in MERGED_INTO_LIVE_ACTION]
    if not any(path.exists() for path in old_files):
        return
    if load_kind_approval(config, LIVE_ACTION) is None:
        carried = next((approval for kind in MERGED_INTO_LIVE_ACTION
                        if holds_approval(approval := load_kind_approval(config, kind))), None)
        if carried is not None:
            _save_kind_approval(config, LIVE_ACTION, {**carried, "kind": LIVE_ACTION})
    for path in old_files:
        path.unlink(missing_ok=True)


def approved_kinds(config: MediaforceConfig) -> list[str]:
    return [kind for kind in APPROVABLE_KINDS if holds_approval(load_kind_approval(config, kind))]


def approve_kind(
        config: MediaforceConfig,
        kind: str,
        from_prefix: str,
        calibration: Mapping[str, Any] | None,
        *,
        approved_at: str,
) -> dict[str, Any]:
    """Extend a folder's current approval to every season of this kind; refusals say why with a code."""
    if kind not in APPROVABLE_KINDS:
        return {"ok": False, "code": KIND_IS_ONE_TIME_CODE}
    payload = object_dict(calibration)
    refusal = _extension_refusal(EffectiveApproval(from_prefix, payload, production_approval_contract(payload)))
    if refusal is not None:
        return {"ok": False, "code": refusal}
    saved = {key: value for key, value in payload.items() if key != KEEP_FULL_HEIGHT_FIELD}
    # A sample the Director watched at 4K keeps 4K for the whole kind; any other comes down to the tallest picture.
    if sample_kept_full_height(payload, config):
        saved[KEEP_FULL_HEIGHT_FIELD] = True
    with _group_choice_lock:
        if not _matches_group_size(config, kind, production_approval_contract(payload)):
            return {"ok": False, "code": "sample_changed"}
        _save_kind_approval(config, kind, {**saved, "kind": kind, "from_prefix": from_prefix, "kind_approved_at": approved_at})
    return {"ok": True, "kind": kind, "from_prefix": from_prefix}


def _save_kind_approval(config: MediaforceConfig, kind: str, payload: Mapping[str, Any]) -> None:
    _write_kind_file(kind_approval_file(config, kind), payload)


def _write_kind_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)


def selected_cartoon_size(config: MediaforceConfig) -> float:
    path = kind_approval_file(config, CARTOON).with_suffix(".size.json")
    try:
        size = object_dict(json.loads(path.read_text())).get("size_mb")
    except (FileNotFoundError, json.JSONDecodeError):
        return CARTOON_TARGET_MB
    return size if isinstance(size, (float, int)) and size in CARTOON_SIZES_MB else CARTOON_TARGET_MB


def choose_kind_size(config: MediaforceConfig, kind: str, size_mb: float) -> bool:
    with _group_choice_lock:
        if kind != CARTOON or size_mb not in CARTOON_SIZES_MB or holds_approval(load_kind_approval(config, kind)):
            return False
        _write_kind_file(kind_approval_file(config, kind).with_suffix(".size.json"), {"size_mb": size_mb})
    return True


def _matches_group_size(config: MediaforceConfig, kind: str, contract: Mapping[str, Any] | None) -> bool:
    if kind != CARTOON:
        return True
    return matches_cartoon_size(contract, selected_cartoon_size(config))


def matches_cartoon_size(contract: Mapping[str, Any] | None, size_mb: float) -> bool:
    goal = object_dict(object_dict(object_dict(contract).get("operator_intent")).get("size_goal"))
    return (goal.get("mode") == "normalized" and goal.get("value_mb") == size_mb
            and goal.get("reference_runtime_minutes") == 45)


def choose_kind_picture_height(config: MediaforceConfig, kind: str, *, keep_full_height: bool) -> bool:
    """Save whether 4K stays 4K under this kind's approval; False when the kind holds no approval to save it on."""
    approval = load_kind_approval(config, kind)
    if kind not in APPROVABLE_KINDS or approval is None or not holds_approval(approval):
        return False
    saved = {key: value for key, value in approval.items() if key != KEEP_FULL_HEIGHT_FIELD}
    _save_kind_approval(config, kind, {**saved, KEEP_FULL_HEIGHT_FIELD: True} if keep_full_height else saved)
    return True


def _extension_refusal(approval: EffectiveApproval) -> str | None:
    """Why this folder approval cannot cover a kind, or None when it can."""
    if not holds_approval(approval.calibration) or not approval.current or approval.contract is None:
        return APPROVAL_NOT_CURRENT_CODE
    return _size_extension_refusal(approval.contract)


def _size_extension_refusal(contract: Mapping[str, Any]) -> str | None:
    size_goal = object_dict(object_dict(contract.get("operator_intent")).get("size_goal"))
    # A size per episode fits only that show's runtime; a size per 45 minutes carries to other shows.
    if size_goal.get("mode") != "normalized":
        return SIZE_GOAL_NOT_SCALABLE_CODE
    return None


def stop_kind_approval(config: MediaforceConfig, kind: str) -> bool:
    if kind not in APPROVABLE_KINDS:
        return False
    try:
        kind_approval_file(config, kind).unlink()
    except FileNotFoundError:
        return False
    return True


class SeasonKinds:
    def __init__(self) -> None:
        self._calculations = LibraryCalculations()

    def __call__(self, config: MediaforceConfig, season_prefix: str) -> str | None:
        snapshot = _request_kind_contents.get()
        if snapshot is not None and snapshot[0] is config:
            kind = snapshot[1].kinds_by_season.get(season_prefix)
            if kind is not None:
                return kind
            return season_grouping(snapshot[2], season_prefix, library_types=config.library_type_map).kind
        if not config.paths.db_path.exists():
            return None
        with open_readonly_db(config.paths.db_path) as connection, self._calculations.request(connection):
            return season_grouping(connection, season_prefix, library_types=config.library_type_map).kind



class KindAwareLoader:
    """A calibration loader that reads folders exactly as the one it wraps, and also finds kind approvals.

    Only the approval resolver asks for kind approvals, so nothing that saves a folder's own sample ever
    reads a kind's copy as that folder's.
    """

    def __init__(self, load_calibration_state: LoadCalibrationState, kind_of: KindOf) -> None:
        self._load = load_calibration_state
        self._kind_of = kind_of

    def __call__(self, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        return self._load(config, prefix)

    def sample_waits(self, config: MediaforceConfig) -> dict[str, SampleWait]:
        if not config.paths.db_path.exists():
            return {}
        pending = [kind for kind in APPROVABLE_KINDS if not holds_approval(load_kind_approval(config, kind))]
        load = cached_calibration_loader(self._load)
        with open_readonly_db(config.paths.db_path) as connection:
            contents = _kind_contents(connection, config, load)
            rows = contents.report["seasons"]
            waits = {row["season"]: SampleWait(measurement_reasons=tuple(row.get("measurement_reasons", ())))
                     for row in rows if row.get("reason") == "no_measured_original"}
            boundaries = {}
            for kind in pending:
                settings = _lent_settings(config, kind, contents.approvals, contents.episodes,
                                          object_dict(contents.report.get("kinds_by_season")), contents.failed_seasons)
                if settings:
                    boundaries[kind] = object_dict(load(config, settings[0]["from_prefix"])).get("accepted_at")
                else:
                    representative = contents.samples.get(kind)
                    prefix = str(object_dict(representative).get("prefix") or "")
                    finished = object_dict(representative).get("status") in {"pending_review", "completed"}
                    boundaries[kind] = (object_dict(representative).get("created_at")
                                        if not finished or _sample_covers_kind(config, kind, load(config, prefix), self._kind_of) else None)
            jobs = load_latest_jobs_for_prefixes(connection, [row["season"] for row in rows])
        waits.update({row["season"]: SampleWait(kind=row["kind"]) for row in rows
                      if row["kind"] in boundaries and (row["kind"] != CARTOON or row["season"] not in contents.failed_seasons)
                      and sample_allows_kind(config, row["season"], load, jobs.get(row["season"]), boundaries[row["kind"]])
                      and effective_production_approval(config, row["season"], load) is None})
        return waits

    def kind_approval(self, config: MediaforceConfig, rel_path: str) -> EffectiveApproval | None:
        # No season's kind is read until the Director has approved a kind.
        snapshot = _request_kind_contents.get()
        contents = snapshot[1] if snapshot is not None and snapshot[0] is config else None
        saved = contents.kind_approvals if contents else {kind: load_kind_approval(config, kind) for kind in APPROVABLE_KINDS}
        load = contents.load_calibration if contents else self._load
        approved = [kind for kind, approval in saved.items() if holds_approval(approval)]
        season = season_prefix_of(config, rel_path) if approved else None
        if season is None:
            return None
        kind = self._kind_of(config, season)
        if kind not in approved:
            return None
        calibration = saved.get(str(kind))
        if calibration is None:
            return None
        approval = EffectiveApproval(season, calibration, production_approval_contract(calibration), kind=kind)
        has_folder = any(load(config, prefix) is not None for prefix in approval_prefixes(rel_path))
        if kind == CARTOON and contents and season in contents.failed_seasons:
            return None
        if has_folder or (kind == CARTOON and contents is None and config.paths.db_path.exists()):
            if has_folder and (not approval.current or not config.paths.db_path.exists()):
                return None
            with open_readonly_db(config.paths.db_path) as connection:
                job = load_latest_sample_job(connection, season) if kind == CARTOON else load_latest_job(connection, season)
            if (kind == CARTOON and job and job.get("status") in SAMPLE_FAILED_STATUSES
                    or not sample_allows_kind(config, rel_path, load, job, calibration.get("kind_approved_at"))):
                return None
        return approval


KIND_QUEUE_WAITS_CODE = "kind_queue_waits"
# A refused season is tried again after this long, in case what refused it (such as free space) has cleared.
KIND_RETRY_SECONDS = 6 * 3600.0


def background_queue_blocker(
        connection: DBClient, config: MediaforceConfig, prefix: str, *, library_item_ids: Collection[int],
        computers: Collection[dict[str, Any]],
) -> dict[str, Any] | None:
    if (_queue_held(connection, config) or prefix.partition("/")[0] not in _libraries_to_top_up(connection, config, computers)
            or load_active_encode_jobs_for_prefix(connection, prefix)):
        return {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    files = _waiting_files(connection, library_item_ids=library_item_ids)
    if set(library_item_ids) != {int(item["id"]) for item in files} or _files_waiting_for_owner(connection, files, config=config):
        return {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    return None


def _queue_held(connection: DBClient, config: MediaforceConfig) -> bool:
    return bool(update_hold_reason(config.paths.db_path.parent) or load_queue_state(connection).get("is_paused"))


def _libraries_to_top_up(
        connection: DBClient, config: MediaforceConfig, computers: Collection[dict[str, Any]],
) -> set[str]:
    libraries = set(config.source_root_map)
    jobs = connection.execute(select(encode_jobs.c.status, encode_jobs.c.prefix).where(
        encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES), encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS),
    ).order_by(encode_jobs.c.created_at, encode_jobs.c.job_id)).all()
    if not jobs:
        return libraries
    slots: Counter[frozenset[str]] = Counter()
    for computer in computers:
        if computer["schedule_open"] and not computer.get("cooldown_until") and not computer.get("controller_storage_issue"):
            allowed = frozenset(key for key in libraries if _host_allows_library(computer, key.lower()))
            slots[allowed] += available_encode_slots(computer)
    restricted_first = sorted(slots, key=len)
    for status, prefix in jobs:
        if status != "queued":
            continue
        library = prefix.partition("/")[0]
        for allowed in restricted_first:
            if slots[allowed] and library in allowed:
                slots[allowed] -= 1
                break
    return {key for allowed, remaining in slots.items() if remaining for key in allowed}


def _waiting_seasons(
        connection: DBClient, config: MediaforceConfig, *, library_item_ids: Collection[int] | None = None,
) -> list[tuple[str, int, tuple[int, ...]]]:
    """Seasons and movie titles with files still to compress, largest waiting space first, with those files' ids.

    Files the owner chose to keep original are finished, so they never bring a season back.
    """
    rows = _waiting_files(connection, library_item_ids=library_item_ids)
    blocked = _files_waiting_for_owner(connection, rows, config=config)
    library_types = offered_library_types(config)
    waiting: dict[str, tuple[int, list[int]]] = {}
    for item in rows:
        rel_path = str(item["rel_path"])
        season = look_group(library_types, rel_path)
        if (season is None or int(item["id"]) in blocked
                or not (rel_path.startswith(f"{season}/") or (rel_path == season and PurePosixPath(rel_path).suffix))):
            continue
        size, ids = waiting.get(season, (0, []))
        waiting[season] = (size + int(item["size_bytes"] or 0), [*ids, int(item["id"])])
    return sorted(((season, size, tuple(sorted(ids))) for season, (size, ids) in waiting.items()),
                  key=lambda entry: (-entry[1], entry[0]))


def _waiting_files(connection: DBClient, *, library_item_ids: Collection[int] | None = None) -> list[dict[str, Any]]:
    query = select(
        library_items.c.id, library_items.c.id.label("item_id"), library_items.c.rel_path, library_items.c.video_codec, library_items.c.size_bytes,
        library_items.c.status, staged_artifacts.c.library_item_id.label("staged_library_item_id"),
        staged_artifacts.c.staging_path, staged_artifacts.c.promoted_at, staged_artifacts.c.validation_json,
    ).select_from(library_items.outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)).where(
        LIBRARY_ITEM_PRESENT, library_items.c.status.in_(ENCODE_CANDIDATE_STATUSES),
    )
    if library_item_ids is not None:
        query = query.where(library_items.c.id.in_(library_item_ids))
    rows: list[dict[str, Any]] = []
    for row in connection.execute(query).mappings():
        item = {str(key): value for key, value in row.items()}
        if str(item["video_codec"] or "").casefold() != "av1" and derive_item_workflow_state(item).lane == "encode":
            rows.append(item)
    kept = library_item_ids_kept_original(connection, [int(item["id"]) for item in rows])
    return [item for item in rows if int(item["id"]) not in kept]


def _files_waiting_for_owner(
        connection: DBClient, files: list[dict[str, Any]], *, config: MediaforceConfig | None = None,
) -> frozenset[int]:
    outcomes = file_encode_outcomes(connection, {int(item["id"]): str(item["rel_path"]) for item in files}, config=config)
    return frozenset(item_id for item_id, outcome in outcomes.items() if outcome.holds_file)


def _attempts_file(config: MediaforceConfig) -> Path:
    return config.paths.web_state_dir / "kinds" / "attempts.json"


def _load_attempts(config: MediaforceConfig) -> dict[str, Any]:
    try:
        payload = json.loads(_attempts_file(config).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class KindSeason:
    season: str
    kind: str | None
    waiting_bytes: int
    approval: str
    files: tuple[int, ...]


def record_kind_attempt(config: MediaforceConfig, chosen: KindSeason, result: Mapping[str, Any], *, at: float) -> None:
    """Remember how queueing a season went, across restarts; a wait for the queue to settle is no attempt."""
    if result.get("code") == KIND_QUEUE_WAITS_CODE:
        return
    attempts = _load_attempts(config)
    attempts[chosen.season] = {"approval": chosen.approval, "files": list(chosen.files), "ok": bool(result.get("ok")), "at": at}
    if not result.get("ok"):
        attempts[chosen.season]["refusal"] = {key: result[key] for key in ("code", "message", "measurable") if key in result}
    path = _attempts_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(attempts, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)


def _retry_pending(record: Mapping[str, Any], now: float) -> bool:
    return not record.get("ok") and now < float(record.get("at") or 0) + KIND_RETRY_SECONDS


def _attempted(attempt: Any, chosen: KindSeason, now: float) -> bool:
    """True when this season was already tried under this approval with the same waiting files."""
    record = object_dict(attempt)
    if record.get("approval") != chosen.approval or tuple(record.get("files") or ()) != chosen.files:
        return False
    return bool(record.get("ok")) or _retry_pending(record, now)


def next_kind_season(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        load_calibration_state: LoadCalibrationState,
        now: float,
        computers: Collection[dict[str, Any]],
) -> KindSeason | None:
    if _queue_held(connection, config):
        return None
    libraries = _libraries_to_top_up(connection, config, computers)
    if not libraries:
        return None
    return next_approved_season(connection, config, load_calibration_state=load_calibration_state, now=now, libraries=libraries)


def next_approved_season(
        connection: DBClient, config: MediaforceConfig, *, load_calibration_state: LoadCalibrationState, now: float,
        libraries: set[str] | None = None,
) -> KindSeason | None:
    attempts = _load_attempts(config)
    active_prefixes = connection.execute(select(encode_jobs.c.prefix).where(
        encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES), encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS),
    )).scalars().all()
    for season, waiting_bytes, files in _waiting_seasons(connection, config):
        if libraries is not None and season.partition("/")[0] not in libraries:
            continue
        if any(season == prefix or season.startswith(f"{prefix}/") or prefix.startswith(f"{season}/") for prefix in active_prefixes):
            continue
        chosen = _approved_candidate(config, season, waiting_bytes, files, load_calibration_state)
        if chosen is not None and not _attempted(attempts.get(season), chosen, now):
            return chosen
    return None


def _approved_candidate(
        config: MediaforceConfig, season: str, waiting_bytes: int, files: tuple[int, ...], load: LoadCalibrationState,
) -> KindSeason | None:
    approval = effective_production_approval(config, season, load)
    if approval is None or not approval.current:
        return None
    sample_prefix, _sample = production_calibration(config, season, load)
    if sample_prefix != approval.prefix:
        return None
    return KindSeason(season, approval.kind, waiting_bytes, approval_identity(approval.calibration, approval.contract), files)


def movie_queue_facts(
        connection: DBClient, config: MediaforceConfig, prefix: str, file_ids: Collection[int], load: LoadCalibrationState,
        *, now: float, computers: Collection[dict[str, Any]] = (),
) -> dict[str, Any]:
    waits = ["queue_paused"] if load_queue_state(connection).get("is_paused") else []
    if prefix.partition("/")[0] not in _libraries_to_top_up(connection, config, computers):
        waits.append("computer_capacity")
    facts: dict[str, Any] = {"waits": waits, "refusal": None}
    attempt = object_dict(_load_attempts(config).get(prefix))
    if not attempt or not _retry_pending(attempt, now):
        return facts
    for season, size, files in _waiting_seasons(connection, config, library_item_ids=file_ids):
        if season == prefix:
            chosen = _approved_candidate(config, season, size, files, load)
            if chosen is not None and _attempted(attempt, chosen, now):
                facts["refusal"] = object_dict(attempt.get("refusal"))
            break
    return facts


NO_FOLDER_APPROVAL_CODE = "no_folder_approval"
SOURCE_KIND_DIFFERS_CODE = "source_kind_differs"


def _approval_facts(config: MediaforceConfig, calibration: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not holds_approval(calibration):
        return None
    payload = object_dict(calibration)
    contract = object_dict(production_approval_contract(payload))
    size_goal = object_dict(object_dict(contract.get("operator_intent")).get("size_goal"))
    return {"from_prefix": payload.get("from_prefix"), "approved_at": payload.get("kind_approved_at"),
            "keep_full_height": bool(payload.get(KEEP_FULL_HEIGHT_FIELD)),
            "size_goal_mode": size_goal.get("mode"), "size_goal_mb": size_goal.get("value_mb"),
            "sample_moments": sample_moments(payload, review_dir=config.paths.review_dir)}


LEFT_OUT_KINDS = ("unmeasured",)


def _size_goal_facts(approval: EffectiveApproval) -> dict[str, Any]:
    size_goal = object_dict(object_dict(object_dict(approval.contract).get("operator_intent")).get("size_goal"))
    return {"size_goal_mode": size_goal.get("mode"), "size_goal_mb": size_goal.get("value_mb")}


def _tv_episodes(connection: DBClient, config: MediaforceConfig) -> dict[str, list[tuple[str, int]]]:
    return reuse_library_calculation(
        connection, "tv_episodes", (select(library_items.c.id, library_items.c.rel_path, library_items.c.missing_since),
                                    select(staged_artifacts.c.library_item_id, staged_artifacts.c.promoted_at, staged_artifacts.c.archived_source_path),
                                    select(item_events.c.library_item_id, item_events.c.event_type)),
        offered_library_types(config), lambda: _read_tv_episodes(connection, config),
    )


def _read_tv_episodes(connection: DBClient, config: MediaforceConfig) -> dict[str, list[tuple[str, int]]]:
    """Every TV show's and movie title's files by path, each with its id when it is published and its original backup is
    still kept to compare, else 0. A movie title stands as its own show and season."""
    removed = confirmed_originals_removed_item_ids(connection)
    library_types = offered_library_types(config)
    shows: dict[str, list[tuple[str, int]]] = {}
    rows = connection.execute(select(
        library_items.c.id, library_items.c.rel_path, staged_artifacts.c.promoted_at, staged_artifacts.c.archived_source_path,
    ).select_from(library_items.outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id))
        .where(LIBRARY_ITEM_PRESENT).order_by(library_items.c.rel_path)).mappings()
    for row in rows:
        rel_path = str(row["rel_path"])
        if look_group(library_types, rel_path) is None:
            continue
        watchable = row["promoted_at"] is not None and bool(row["archived_source_path"]) and int(row["id"]) not in removed
        shows.setdefault("/".join(rel_path.split("/")[:2]), []).append((rel_path, int(row["id"]) if watchable else 0))
    return shows


def _episode_under(episodes: list[tuple[str, int]], prefix: str) -> int | None:
    return next((item_id for rel_path, item_id in episodes if item_id and rel_path.startswith(f"{prefix}/")), None)


@dataclass(frozen=True, slots=True)
class _ShowApproval:
    approval: EffectiveApproval
    sample_season: str
    # The show's seasons this approval governs, the sample's own first: where an episode made under it is.
    seasons: tuple[str, ...]


def _show_approvals(
        config: MediaforceConfig,
        library_types: dict[str, str],
        show: str,
        episodes: list[tuple[str, int]],
        load_calibration_state: LoadCalibrationState,
) -> list[_ShowApproval]:
    """The current folder approvals on a show or its seasons, each with its sample's season and the seasons it governs."""
    seasons = {season for rel_path, _ in episodes if (season := look_group(library_types, rel_path)) is not None}
    found: dict[str, tuple[EffectiveApproval, str]] = {}
    governed: dict[str, list[str]] = {}
    for folder in dict.fromkeys([show, *sorted(seasons)]):
        approval = effective_production_approval(config, folder, load_calibration_state)
        if approval is None or approval.kind is not None or not approval.current:
            continue
        if folder != show:
            governed.setdefault(approval.prefix, []).append(folder)
        if approval.prefix in found:
            continue
        sample_path = str(object_dict(approval.calibration.get("sample_item")).get("rel_path") or "")
        if sample_season := season_prefix_of(config, sample_path) if sample_path else None:
            found[approval.prefix] = (approval, sample_season)
    return [_ShowApproval(approval, sample_season, tuple(sorted(governed.get(prefix, []), key=lambda governed_season: governed_season != sample_season)))
            for prefix, (approval, sample_season) in found.items()]


def _episode_made_under(episodes: list[tuple[str, int]], seasons: tuple[str, ...]) -> int | None:
    return next((item_id for season in seasons if (item_id := _episode_under(episodes, season)) is not None), None)


def _lent_settings(
        config: MediaforceConfig, kind: str,
        approvals: Mapping[str, list[_ShowApproval]],
        episodes: Mapping[str, list[tuple[str, int]]],
        kinds_by_season: Mapping[str, str], failed_seasons: set[str],
) -> list[dict[str, Any]]:
    """Each show approved on a season of this kind, once, with an episode made under its approval to watch when one
    is kept; shows with an episode come first. A kind one approval covers lists only approvals that can extend to it."""
    settings = []
    for show, found in approvals.items():
        candidates = [
            {"show": show, "from_prefix": entry.approval.prefix, **_size_goal_facts(entry.approval),
             "library_item_id": _episode_made_under(episodes[show], entry.seasons)}
            for entry in found
            if kinds_by_season.get(entry.sample_season) == kind and entry.sample_season not in failed_seasons
            and _extension_refusal(entry.approval) is None and _matches_group_size(config, kind, entry.approval.contract)
        ]
        if candidates:
            settings.append(max(candidates, key=lambda setting: setting["library_item_id"] is not None))
    return sorted(settings, key=lambda setting: (setting["library_item_id"] is None, setting["show"]))


SAMPLE_IN_HAND_STATUSES = SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES | SAMPLE_FAILED_STATUSES | {"pending_review", "completed"}


def _samples_in_hand(connection: DBClient, seasons: Collection[str]) -> list[dict[str, Any]]:
    return [job for job in load_latest_jobs_for_prefixes(connection, list(seasons), lane="sample").values()
            if job.get("status") in SAMPLE_IN_HAND_STATUSES]


def _kind_report(
        connection: DBClient, config: MediaforceConfig, load: LoadCalibrationState, *, sample_seasons: Collection[str] = (),
) -> dict[str, Any]:
    def folder_approved(prefix: str) -> bool:
        approval = effective_production_approval(config, prefix, load)
        return approval is not None and approval.current

    return season_kind_report(connection, library_types=offered_library_types(config), folder_approved=folder_approved,
                              measurement_roots=config.source_root_map,
                              sample_seasons=sample_seasons)


@dataclass(frozen=True, slots=True)
class _KindContents:
    episodes: dict[str, list[tuple[str, int]]]
    approvals: dict[str, list[_ShowApproval]]
    report: dict[str, Any]
    samples: dict[str, dict[str, Any]]
    kinds_by_season: dict[str, str]
    failed_seasons: set[str]
    kind_approvals: dict[str, dict[str, Any] | None]
    load_calibration: LoadCalibrationState


_request_kind_contents: ContextVar[tuple[MediaforceConfig, _KindContents, DBClient] | None] = ContextVar("kind_contents", default=None)


@contextmanager
def kind_snapshot(connection: DBClient, config: MediaforceConfig, load: LoadCalibrationState) -> Iterator[None]:
    token = _request_kind_contents.set((config, _kind_contents(connection, config, load), connection))
    try:
        yield
    finally:
        _request_kind_contents.reset(token)


def _kind_contents(connection: DBClient, config: MediaforceConfig, load_calibration_state: LoadCalibrationState) -> _KindContents:
    snapshot = _request_kind_contents.get()
    if snapshot is not None and snapshot[0] is config:
        return snapshot[1]

    def folders_only(loader_config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        return load_calibration_state(loader_config, prefix)

    load_calibration_state = cached_calibration_loader(load_calibration_state)
    library_types = offered_library_types(config)
    episodes = _tv_episodes(connection, config)
    samples = _samples_in_hand(connection, {season for files in episodes.values() for path, _ in files
                                          if (season := look_group(library_types, path)) is not None})
    failed = {job["prefix"] for job in samples if job.get("status") in SAMPLE_FAILED_STATUSES}
    approvals_by_show = {show: found for show, files in episodes.items()
                         if (found := _show_approvals(config, library_types, show, files, folders_only))}
    report = _kind_report(
        connection, config, folders_only,
        sample_seasons={entry.sample_season for found in approvals_by_show.values() for entry in found}
                       | {str(job["prefix"]) for job in samples},
    )
    kinds_by_season = object_dict(report.get("kinds_by_season"))
    newest: dict[str, dict[str, Any]] = {}
    fallback: dict[str, dict[str, Any]] = {}

    def rank(job: Mapping[str, Any]) -> tuple[bool, bool, str]:
        prefix = str(job["prefix"])
        return (job.get("status") in {"pending_review", "completed"},
                kinds_by_season.get(prefix) == CARTOON and prefix.split("/")[1].casefold() == "futurama",
                str(job.get("created_at") or ""))

    for job in sorted(samples, key=rank):
        kind = kinds_by_season.get(job["prefix"])
        if kind not in APPROVABLE_KINDS or job["prefix"] in failed:
            continue
        if kind != CARTOON or job.get("status") not in {"pending_review", "completed"}:
            fallback[str(kind)] = job
        if job.get("status") not in {"pending_review", "completed"} or _sample_covers_kind(
                config, str(kind), load_calibration_state(config, str(job["prefix"])),
                lambda _config, prefix: kinds_by_season.get(prefix)):
            newest[str(kind)] = job
    measured_kinds = {row["season"]: row["kind"] for row in report["seasons"]}
    measured_kinds.update(kinds_by_season)
    return _KindContents(episodes, approvals_by_show, report, {**fallback, **newest}, measured_kinds, failed,
                         {kind: load_kind_approval(config, kind) for kind in APPROVABLE_KINDS}, load_calibration_state)


def kinds_payload(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        load_calibration_state: LoadCalibrationState,
) -> dict[str, Any]:
    """Facts for the page where each kind of content's approval is decided; the app words every one.

    A show already approved on a season of the kind lends its setting: the kind lists it with its size setting and,
    when one is kept, a published episode with its original backup, so the page can play what the setting looks like.
    Such a show's own seasons follow its approval, so it is never among the shows the kind covers.
    """

    contents = _kind_contents(connection, config, load_calibration_state)
    episodes, approvals_by_show, report = contents.episodes, contents.approvals, contents.report
    kinds_by_season = object_dict(report.get("kinds_by_season"))
    seasons: dict[str, dict[str, list[tuple[int, str]]]] = {kind: {} for kind in APPROVABLE_KINDS}
    for row in report["seasons"]:
        if row["kind"] in seasons:
            season = str(row["season"])
            seasons[row["kind"]].setdefault("/".join(season.split("/")[:2]), []).append((int(row["waiting_bytes"]), season))
    # A show's sample is made on its biggest waiting season of the kind, the one that saves the most.
    library_types = config.library_type_map
    shows = {kind: {show: {"media_type": library_types.get(show.partition("/")[0]), "seasons": len(found), "waiting_bytes": sum(size for size, _ in found),
                           "sample_season": next((prefix for _, prefix in sorted(found, key=lambda entry: (-entry[0], entry[1]))
                                                  if prefix not in contents.failed_seasons), None)}
                    for show, found in by_show.items()}
             for kind, by_show in seasons.items()}
    totals = report["totals"]
    approvals = {kind: _approval_facts(config, contents.kind_approvals.get(kind))
                 for kind in APPROVABLE_KINDS}
    for approval in approvals.values():
        if approval:
            show = "/".join(str(approval["from_prefix"]).split("/")[:2])
            lent = next((entry for entry in approvals_by_show.get(show, []) if entry.approval.prefix == approval["from_prefix"]), None)
            approval["library_item_id"] = _episode_made_under(episodes.get(show, []), lent.seasons) if lent else None
    payload: dict[str, Any] = {
        "kinds": [{
            "kind": kind,
            "one_time": False,
            **({"selected_size_mb": selected_cartoon_size(config), "size_options_mb": CARTOON_SIZES_MB} if kind == CARTOON else {}),
            "seasons": int(object_dict(totals.get(kind)).get("seasons") or 0),
            "waiting_bytes": int(object_dict(totals.get(kind)).get("waiting_bytes") or 0),
            "shows": [{"prefix": prefix, **values} for prefix, values in
                      sorted(shows[kind].items(), key=lambda entry: (kind == CARTOON and entry[0].split("/")[-1].casefold() != "futurama", -entry[1]["waiting_bytes"], entry[0]))],
            "settings": _lent_settings(config, kind, approvals_by_show, episodes, kinds_by_season, contents.failed_seasons),
            "approval": approvals[kind],
            "sample_season": _approved_sample_season(config, kind) or object_dict(contents.samples.get(kind)).get("prefix"),
            "sample_status": object_dict(contents.samples.get(kind)).get("status") or "not_started",
            "needs_you": approvals[kind] is None and any(show["sample_season"] for show in shows[kind].values()),
        } for kind in APPROVABLE_KINDS],
        "left_out": [{"season": row["season"], "kind": row["kind"], "waiting_bytes": int(row["waiting_bytes"])}
                     for row in report["seasons"] if row["kind"] in LEFT_OUT_KINDS],
        "unmeasured_seasons": int(object_dict(totals.get("unmeasured")).get("seasons") or 0),
        "folder_approved_seasons": int(object_dict(totals.get("folder_approved")).get("seasons") or 0),
        "tallest_picture": tallest_picture(config.video),
    }
    for entry in payload["kinds"]:
        season = entry["sample_season"]
        if entry["kind"] == CARTOON:
            entry["sample_in_progress"] = entry["sample_status"] in SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES
            offered = {setting["show"] for setting in entry["settings"]}
            entry["animation_titles"] = report.get("animation_titles", [])
            entry["season_approved_shows"] = sorted(show for show, found in approvals_by_show.items() if show not in offered
                                                   and any(kinds_by_season.get(item.sample_season) == CARTOON for item in found))
        entry["sample_ready"] = False
        if season and entry["sample_status"] in {"pending_review", "completed"}:
            calibration = contents.load_calibration(config, season)
            if sample_moments(object_dict(calibration)):
                entry["sample_ready"] = sample_approval_kind(
                    config, payload, season, calibration, kind_of=lambda _config, prefix: kinds_by_season.get(prefix),
                ) == entry["kind"]
            else:
                approval = effective_production_approval(config, season, contents.load_calibration)
                entry["sample_status"] = "choose_another_show" if approval is not None and approval.current else "missing_clips"
        entry["needs_you"] = entry["needs_you"] or (approvals[entry["kind"]] is None and entry["sample_ready"])
    return payload


def _approved_sample_season(config: MediaforceConfig, kind: str) -> str | None:
    calibration = load_kind_approval(config, kind)
    if calibration is None or not EffectiveApproval("", calibration, production_approval_contract(calibration)).current:
        return None
    return season_prefix_of(config, str(object_dict(calibration.get("sample_item")).get("rel_path") or ""))


def sample_approval_kind(
        config: MediaforceConfig, kinds: Mapping[str, Any], prefix: str, calibration: Mapping[str, Any] | None,
        *, kind_of: KindOf,
) -> str | None:
    kind = next((str(entry["kind"]) for entry in kinds.get("kinds", [])
                 if entry["kind"] in APPROVABLE_KINDS and entry.get("sample_season") == prefix
                 and _approved_sample_season(config, str(entry["kind"])) is None), None)
    return kind if kind is not None and _sample_covers_kind(config, kind, calibration, kind_of) else None


def _sample_covers_kind(config: MediaforceConfig, kind: str, calibration: Mapping[str, Any] | None, kind_of: KindOf) -> bool:
    return _scalable_sample(config, kind, calibration) and _sample_kind_refusal(config, kind, object_dict(calibration), kind_of) is None


def _scalable_sample(config: MediaforceConfig, kind: str, calibration: Mapping[str, Any] | None) -> bool:
    payload = object_dict(calibration)
    if not payload.get("job_id") or str(payload.get("mode") or "sample") != "sample":
        return False
    contract = production_approval_contract({
        **payload, "accepted_sample_job_id": payload["job_id"], "accepted_policy_hash": calibration_policy_hash(payload),
    })
    return contract is not None and _size_extension_refusal(contract) is None and _matches_group_size(config, kind, contract)


def _sample_kind_refusal(config: MediaforceConfig, kind: str, calibration: Mapping[str, Any], kind_of: KindOf) -> str | None:
    sample_path = str(object_dict(calibration.get("sample_item")).get("rel_path") or "")
    sample_season = season_prefix_of(config, sample_path) if sample_path else None
    return None if sample_season is not None and kind_of(config, sample_season) == kind else SOURCE_KIND_DIFFERS_CODE


def approve_kind_action(
        config: MediaforceConfig,
        kind: str,
        from_prefix: str,
        *,
        load_calibration_state: LoadCalibrationState,
        kind_of: KindOf,
        approved_at: str,
        reviewed_draft_hash: str | None = None,
) -> dict[str, Any]:
    """Use the approval governing a folder for every season of this kind.

    Only a folder's own approval extends, and only when the episode its sample was made from belongs to a season
    that measures as this kind, so the settings chosen for one kind of picture never cover another. A show's
    approval counts by its sample's season, not by the season named here.
    """
    if kind not in APPROVABLE_KINDS:
        return {"ok": False, "code": KIND_IS_ONE_TIME_CODE}
    approval = effective_production_approval(config, from_prefix, load_calibration_state)
    if approval is None or approval.kind is not None:
        return {"ok": False, "code": NO_FOLDER_APPROVAL_CODE}
    if reviewed_draft_hash is not None and approval.calibration.get("accepted_draft_hash") != reviewed_draft_hash:
        return {"ok": False, "code": "sample_changed"}
    if kind == CARTOON and config.paths.db_path.exists():
        source_file = str(object_dict(approval.calibration.get("sample_item")).get("rel_path") or "")
        source_season = season_prefix_of(config, source_file)
        if source_season:
            with open_readonly_db(config.paths.db_path) as connection:
                job = load_latest_sample_job(connection, source_season)
            if job and job.get("status") in SAMPLE_FAILED_STATUSES:
                return {"ok": False, "code": APPROVAL_NOT_CURRENT_CODE}
    if refusal := _sample_kind_refusal(config, kind, approval.calibration, kind_of):
        return {"ok": False, "code": refusal}
    return approve_kind(config, kind, approval.prefix, approval.calibration, approved_at=approved_at)
