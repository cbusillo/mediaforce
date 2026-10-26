"""Kind approvals: one approval covering every TV season of a kind of content (#1047).

The Director decided on 2026-10-22 (#517) that one approval may cover every show that measures the same,
starting with darker and bright live action. Cartoons and 4K stay one-time choices made show by show.

A kind approval is a copy of a current folder approval the Director chose to extend, kept in the web state
directory's `kinds` folder, one file per kind. It governs a season only when no folder on the season's path
holds any saved sample (see production_approval), and the season's kind comes from its originals' traits
(library/content_kinds). Every file it covers still meets the per-file quality floor and size hold.
"""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import encode_jobs, library_items
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, RUNNABLE_ENCODE_JOB_KINDS, load_queue_state
from mediaforce.core.type_defs import object_dict
from mediaforce.library.candidate_selection import season_identity
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.content_kinds import evidence_stamp, season_evidence, season_grouping, season_kind_report, \
    season_stamp
from mediaforce.library.source_evidence import SourceEvidenceContext
from mediaforce.web.runtime.production_approval import EffectiveApproval, LoadCalibrationState, \
    effective_production_approval, holds_approval, production_approval_contract
from mediaforce.web.runtime.production_holds import approval_identity

APPROVABLE_KINDS = ("darker_live_action", "bright_live_action")
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
    approval = EffectiveApproval(from_prefix, payload, production_approval_contract(payload))
    if not holds_approval(payload) or not approval.current or approval.contract is None:
        return {"ok": False, "code": APPROVAL_NOT_CURRENT_CODE}
    size_goal = object_dict(object_dict(approval.contract.get("operator_intent")).get("size_goal"))
    # A size per episode fits only that show's runtime; a size per 45 minutes carries to other shows.
    if size_goal.get("mode") != "normalized":
        return {"ok": False, "code": SIZE_GOAL_NOT_SCALABLE_CODE}
    saved = {**payload, "kind": kind, "from_prefix": from_prefix, "kind_approved_at": approved_at}
    path = kind_approval_file(config, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(saved, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)
    return {"ok": True, "kind": kind, "from_prefix": from_prefix}


def stop_kind_approval(config: MediaforceConfig, kind: str) -> bool:
    if kind not in APPROVABLE_KINDS:
        return False
    try:
        kind_approval_file(config, kind).unlink()
    except FileNotFoundError:
        return False
    return True


def season_prefix_of(config: MediaforceConfig, rel_path: str) -> str | None:
    """The TV season a file or season folder belongs to; None for a show, a movie or a file in a show folder."""
    path = PurePosixPath(str(rel_path or "").strip().strip("/"))
    if len(path.parts) < 3 or (len(path.parts) == 3 and path.suffix):
        return None
    identity = season_identity({"rel_path": path.as_posix()}, library_types=config.library_type_map)
    return identity.season_prefix if identity is not None else None


class SeasonKinds:
    """Each season's kind from the live database, read again only when its evidence changes.

    Pages ask for many seasons at once, so one library-wide evidence snapshot is shared until anything it
    reads changes, and one season is worked out at a time so concurrent requests never repeat the work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kinds: dict[str, tuple[tuple[Any, ...], str]] = {}
        self._evidence: tuple[tuple[Any, ...], tuple[set[int], SourceEvidenceContext]] | None = None

    def __call__(self, config: MediaforceConfig, season_prefix: str) -> str | None:
        if not config.paths.db_path.exists():
            return None
        with self._lock, open_readonly_db(config.paths.db_path) as connection:
            shared = evidence_stamp(connection)
            stamp = (shared, season_stamp(connection, season_prefix))
            cached = self._kinds.get(season_prefix)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            if self._evidence is None or self._evidence[0] != shared:
                self._evidence = (shared, season_evidence(connection))
            kind = season_grouping(connection, season_prefix, library_types=config.library_type_map,
                                   evidence=self._evidence[1]).kind
            self._kinds[season_prefix] = (stamp, kind)
            return kind


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

    def kind_approval(self, config: MediaforceConfig, rel_path: str) -> EffectiveApproval | None:
        # No season's kind is read until the Director has approved a kind.
        approved = approved_kinds(config)
        season = season_prefix_of(config, rel_path) if approved else None
        if season is None:
            return None
        kind = self._kind_of(config, season)
        if kind not in approved:
            return None
        calibration = load_kind_approval(config, str(kind))
        if calibration is None:
            return None
        return EffectiveApproval(season, calibration, production_approval_contract(calibration), kind=kind)


# A run that ended badly leaves its season for the Director; the background queue never retries it.
_UNSETTLED_RUN_STATUSES = ("failed", "stopped", "needs_attention")
KIND_QUEUE_WAITS_CODE = "kind_queue_waits"
# A refused season is tried again after this long, in case what refused it (such as free space) has cleared.
KIND_RETRY_SECONDS = 6 * 3600.0


def background_queue_blocker(connection: DBClient) -> dict[str, Any] | None:
    """Why the background queue waits: the owner paused work, or a production run is queued or running."""
    active = connection.execute(select(encode_jobs.c.job_id).where(
        encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES), encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS),
    ).limit(1)).first()
    if load_queue_state(connection).get("is_paused") or active is not None:
        return {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    return None


def _waiting_seasons(connection: DBClient, config: MediaforceConfig) -> list[tuple[str, int, tuple[int, ...]]]:
    """Seasons with files still to compress, largest waiting space first, with those files' ids.

    Files the owner chose to keep original are finished, so they never bring a season back.
    """
    rows = [item for item in connection.execute(select(
        library_items.c.id, library_items.c.rel_path, library_items.c.video_codec, library_items.c.size_bytes,
    ).where(LIBRARY_ITEM_PRESENT, library_items.c.status != "promoted")).mappings()
        if str(item["video_codec"] or "").casefold() != "av1"]
    kept = library_item_ids_kept_original(connection, [int(item["id"]) for item in rows])
    waiting: dict[str, tuple[int, list[int]]] = {}
    for item in rows:
        season = season_prefix_of(config, str(item["rel_path"]))
        if season is None or int(item["id"]) in kept or not str(item["rel_path"]).startswith(f"{season}/"):
            continue
        size, ids = waiting.get(season, (0, []))
        waiting[season] = (size + int(item["size_bytes"] or 0), [*ids, int(item["id"])])
    return sorted(((season, size, tuple(sorted(ids))) for season, (size, ids) in waiting.items()),
                  key=lambda entry: (-entry[1], entry[0]))


def _latest_run_unsettled(connection: DBClient, season: str) -> bool:
    status = connection.execute(
        select(encode_jobs.c.status).where(encode_jobs.c.prefix == season).order_by(encode_jobs.c.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return status in _UNSETTLED_RUN_STATUSES


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
    kind: str
    waiting_bytes: int
    approval: str
    files: tuple[int, ...]


def record_kind_attempt(config: MediaforceConfig, chosen: KindSeason, result: Mapping[str, Any], *, at: float) -> None:
    """Remember how queueing a season went, across restarts; a wait for the queue to settle is no attempt."""
    if result.get("code") == KIND_QUEUE_WAITS_CODE:
        return
    attempts = _load_attempts(config)
    attempts[chosen.season] = {"approval": chosen.approval, "files": list(chosen.files), "ok": bool(result.get("ok")), "at": at}
    path = _attempts_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(attempts, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)


def _attempted(attempt: Any, chosen: KindSeason, now: float) -> bool:
    """True when this season was already tried under this approval with the same waiting files."""
    record = object_dict(attempt)
    if record.get("approval") != chosen.approval or tuple(record.get("files") or ()) != chosen.files:
        return False
    return bool(record.get("ok")) or now - float(record.get("at") or 0) < KIND_RETRY_SECONDS


def next_kind_season(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        load_calibration_state: LoadCalibrationState,
        now: float,
) -> KindSeason | None:
    """The next waiting season a current kind approval covers, to queue on its own, largest first.

    None while the background queue waits (see background_queue_blocker), so seasons go one at a time. A season
    already tried under the same approval with the same waiting files is skipped, a refused one until it is retried
    after KIND_RETRY_SECONDS, and a season whose last run ended badly waits for the Director.
    """
    if not approved_kinds(config) or background_queue_blocker(connection) is not None:
        return None
    attempts = _load_attempts(config)
    for season, waiting_bytes, files in _waiting_seasons(connection, config):
        approval = effective_production_approval(config, season, load_calibration_state)
        if approval is None or approval.kind is None or not approval.current:
            continue
        chosen = KindSeason(season, approval.kind, waiting_bytes, approval_identity(approval.calibration, approval.contract), files)
        if _attempted(attempts.get(season), chosen, now) or _latest_run_unsettled(connection, season):
            continue
        return chosen
    return None


KINDS_IN_ORDER = ("darker_live_action", "bright_live_action", "cartoon", "4k")
NO_FOLDER_APPROVAL_CODE = "no_folder_approval"
SOURCE_KIND_DIFFERS_CODE = "source_kind_differs"


def _approval_facts(calibration: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not holds_approval(calibration):
        return None
    payload = object_dict(calibration)
    contract = object_dict(production_approval_contract(payload))
    size_goal = object_dict(object_dict(contract.get("operator_intent")).get("size_goal"))
    return {"from_prefix": payload.get("from_prefix"), "approved_at": payload.get("kind_approved_at"),
            "size_goal_mode": size_goal.get("mode"), "size_goal_mb": size_goal.get("value_mb")}


def kinds_payload(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        load_calibration_state: LoadCalibrationState,
) -> dict[str, Any]:
    """Facts for the page where each kind of content's approval is decided; the app words every one."""

    def folders_only(loader_config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        return load_calibration_state(loader_config, prefix)

    def folder_approved(prefix: str) -> bool:
        # Folders alone: a kind's approval is what this page decides, so it never counts here.
        approval = effective_production_approval(config, prefix, folders_only)
        return approval is not None and approval.current

    report = season_kind_report(connection, library_types=config.library_type_map, folder_approved=folder_approved)
    shows: dict[str, dict[str, dict[str, int]]] = {kind: {} for kind in KINDS_IN_ORDER}
    for row in report["seasons"]:
        if row["kind"] not in shows:
            continue
        show = shows[row["kind"]].setdefault("/".join(str(row["season"]).split("/")[:2]), {"seasons": 0, "waiting_bytes": 0})
        show["seasons"] += 1
        show["waiting_bytes"] += int(row["waiting_bytes"])
    totals = report["totals"]
    return {
        "kinds": [{
            "kind": kind,
            "one_time": kind not in APPROVABLE_KINDS,
            "seasons": int(object_dict(totals.get(kind)).get("seasons") or 0),
            "waiting_bytes": int(object_dict(totals.get(kind)).get("waiting_bytes") or 0),
            "shows": [{"prefix": prefix, **values} for prefix, values in
                      sorted(shows[kind].items(), key=lambda entry: (-entry[1]["waiting_bytes"], entry[0]))],
            "approval": _approval_facts(load_kind_approval(config, kind)) if kind in APPROVABLE_KINDS else None,
        } for kind in KINDS_IN_ORDER],
        "mixed_seasons": int(object_dict(totals.get("mixed")).get("seasons") or 0),
        "unmeasured_seasons": int(object_dict(totals.get("unmeasured")).get("seasons") or 0),
        "folder_approved_seasons": int(object_dict(totals.get("folder_approved")).get("seasons") or 0),
    }


def approve_kind_action(
        config: MediaforceConfig,
        kind: str,
        from_prefix: str,
        *,
        load_calibration_state: LoadCalibrationState,
        kind_of: KindOf,
        approved_at: str,
) -> dict[str, Any]:
    """Use the approval governing a season for every season of this kind.

    Only a folder's own approval extends, and only from a season that measures as this kind, so the settings
    chosen for one kind of picture never cover another.
    """
    if kind not in APPROVABLE_KINDS:
        return {"ok": False, "code": KIND_IS_ONE_TIME_CODE}
    approval = effective_production_approval(config, from_prefix, load_calibration_state)
    if approval is None or approval.kind is not None:
        return {"ok": False, "code": NO_FOLDER_APPROVAL_CODE}
    season = season_prefix_of(config, from_prefix)
    if season is None or kind_of(config, season) != kind:
        return {"ok": False, "code": SOURCE_KIND_DIFFERS_CODE}
    return approve_kind(config, kind, approval.prefix, approval.calibration, approved_at=approved_at)
