"""Kind approvals: one approval covering every TV season of a kind of content (#1047).

The Director decided on 2026-10-22 (#517) that one approval may cover every show that measures the same,
starting with darker and bright live action. Cartoons and 4K stay one-time choices made show by show.

A kind approval is a copy of a current folder approval the Director chose to extend, kept in the web state
directory's `kinds` folder, one file per kind. Production approval precedence lives in production_approval;
the season's kind comes from its originals' traits
(library/content_kinds). Every file it covers still meets the per-file quality floor and size hold.
"""

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_readonly_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, RUNNABLE_ENCODE_JOB_KINDS, load_queue_state
from mediaforce.core.type_defs import object_dict
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.workflow_state import ENCODE_CANDIDATE_STATUSES, derive_item_workflow_state
from mediaforce.library.content_kinds import evidence_stamp, season_evidence, season_grouping, season_kind_report, \
    season_stamp
from mediaforce.library.source_evidence import SourceEvidenceContext
from mediaforce.tuning.calibration_jobs import load_latest_job, load_latest_jobs_for_prefixes
from mediaforce.web.runtime.app_samples import sample_moments
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash
from mediaforce.web.runtime.production_approval import EffectiveApproval, LoadCalibrationState, \
    SampleWait, approval_prefixes, cached_calibration_loader, effective_production_approval, holds_approval, production_approval_contract, \
    production_calibration, sample_allows_kind, season_prefix_of
from mediaforce.web.runtime.completed_runtime import confirmed_originals_removed_item_ids
from mediaforce.web.runtime.page_state import SAMPLE_FAILED_STATUSES, SAMPLE_RUNNING_STATUSES, SAMPLE_WAITING_STATUSES
from mediaforce.web.runtime.production_holds import approval_identity
from mediaforce.web.runtime.file_encode_outcomes import file_encode_outcomes

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
    refusal = _extension_refusal(EffectiveApproval(from_prefix, payload, production_approval_contract(payload)))
    if refusal is not None:
        return {"ok": False, "code": refusal}
    saved = {**payload, "kind": kind, "from_prefix": from_prefix, "kind_approved_at": approved_at}
    path = kind_approval_file(config, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(saved, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)
    return {"ok": True, "kind": kind, "from_prefix": from_prefix}


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
                settings = _lent_settings(kind, contents.approvals, contents.episodes,
                                          object_dict(contents.report.get("kinds_by_season")))
                if settings:
                    boundaries[kind] = object_dict(load(config, settings[0]["from_prefix"])).get("accepted_at")
                else:
                    representative = contents.samples.get(kind)
                    boundaries[kind] = object_dict(representative).get("created_at")
            jobs = load_latest_jobs_for_prefixes(connection, [row["season"] for row in rows])
        waits.update({row["season"]: SampleWait(kind=row["kind"]) for row in rows
                      if row["kind"] in boundaries
                      and sample_allows_kind(config, row["season"], load, jobs.get(row["season"]), boundaries[row["kind"]])
                      and effective_production_approval(config, row["season"], load) is None})
        return waits

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
        approval = EffectiveApproval(season, calibration, production_approval_contract(calibration), kind=kind)
        if any(self._load(config, prefix) is not None for prefix in approval_prefixes(rel_path)):
            if not approval.current or not config.paths.db_path.exists():
                return None
            with open_readonly_db(config.paths.db_path) as connection:
                job = load_latest_job(connection, season)
            if not sample_allows_kind(config, rel_path, self._load, job, calibration.get("kind_approved_at")):
                return None
        return approval


KIND_QUEUE_WAITS_CODE = "kind_queue_waits"
# A refused season is tried again after this long, in case what refused it (such as free space) has cleared.
KIND_RETRY_SECONDS = 6 * 3600.0


def background_queue_blocker(
        connection: DBClient, *, library_item_ids: Collection[int] | None = None, config: MediaforceConfig | None = None,
) -> dict[str, Any] | None:
    """Why the background queue waits: the owner paused work, or a production run is queued or running."""
    active = connection.execute(select(encode_jobs.c.job_id).where(
        encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES), encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS),
    ).limit(1)).first()
    if load_queue_state(connection).get("is_paused") or active is not None:
        return {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    if library_item_ids is not None:
        files = _waiting_files(connection, library_item_ids=library_item_ids)
        if set(library_item_ids) != {int(item["id"]) for item in files} or _files_waiting_for_owner(connection, files, config=config):
            return {"ok": False, "code": KIND_QUEUE_WAITS_CODE}
    return None


def _waiting_seasons(connection: DBClient, config: MediaforceConfig) -> list[tuple[str, int, tuple[int, ...]]]:
    """Seasons with files still to compress, largest waiting space first, with those files' ids.

    Files the owner chose to keep original are finished, so they never bring a season back.
    """
    rows = _waiting_files(connection)
    blocked = _files_waiting_for_owner(connection, rows, config=config)
    waiting: dict[str, tuple[int, list[int]]] = {}
    for item in rows:
        season = season_prefix_of(config, str(item["rel_path"]))
        if season is None or int(item["id"]) in blocked or not str(item["rel_path"]).startswith(f"{season}/"):
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
    if background_queue_blocker(connection) is not None:
        return None
    attempts = _load_attempts(config)
    for season, waiting_bytes, files in _waiting_seasons(connection, config):
        approval = effective_production_approval(config, season, load_calibration_state)
        if approval is None or not approval.current:
            continue
        sample_prefix, _sample = production_calibration(config, season, load_calibration_state)
        if sample_prefix != approval.prefix:
            continue
        chosen = KindSeason(season, approval.kind, waiting_bytes, approval_identity(approval.calibration, approval.contract), files)
        if _attempted(attempts.get(season), chosen, now):
            continue
        return chosen
    return None


KINDS_IN_ORDER = ("darker_live_action", "bright_live_action", "cartoon", "4k")
NO_FOLDER_APPROVAL_CODE = "no_folder_approval"
SOURCE_KIND_DIFFERS_CODE = "source_kind_differs"


def _approval_facts(config: MediaforceConfig, calibration: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not holds_approval(calibration):
        return None
    payload = object_dict(calibration)
    contract = object_dict(production_approval_contract(payload))
    size_goal = object_dict(object_dict(contract.get("operator_intent")).get("size_goal"))
    return {"from_prefix": payload.get("from_prefix"), "approved_at": payload.get("kind_approved_at"),
            "size_goal_mode": size_goal.get("mode"), "size_goal_mb": size_goal.get("value_mb"),
            "sample_moments": sample_moments(payload, review_dir=config.paths.review_dir)}


LEFT_OUT_KINDS = ("mixed", "unmeasured")


def _size_goal_facts(approval: EffectiveApproval) -> dict[str, Any]:
    size_goal = object_dict(object_dict(object_dict(approval.contract).get("operator_intent")).get("size_goal"))
    return {"size_goal_mode": size_goal.get("mode"), "size_goal_mb": size_goal.get("value_mb")}


def _tv_episodes(connection: DBClient, config: MediaforceConfig) -> dict[str, list[tuple[str, int]]]:
    """Every TV show's files by path, each with its id when it is published and its original backup is still kept to
    compare, else 0."""
    removed = confirmed_originals_removed_item_ids(connection)
    shows: dict[str, list[tuple[str, int]]] = {}
    rows = connection.execute(select(
        library_items.c.id, library_items.c.rel_path, staged_artifacts.c.promoted_at, staged_artifacts.c.archived_source_path,
    ).select_from(library_items.outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id))
        .where(LIBRARY_ITEM_PRESENT).order_by(library_items.c.rel_path)).mappings()
    for row in rows:
        rel_path = str(row["rel_path"])
        if season_prefix_of(config, rel_path) is None:
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
        show: str,
        episodes: list[tuple[str, int]],
        load_calibration_state: LoadCalibrationState,
) -> list[_ShowApproval]:
    """The current folder approvals on a show or its seasons, each with its sample's season and the seasons it governs."""
    seasons: set[str] = set()
    for rel_path, _ in episodes:
        if season := season_prefix_of(config, rel_path):
            seasons.add(season)
    found: dict[str, tuple[EffectiveApproval, str]] = {}
    governed: dict[str, list[str]] = {}
    for folder in [show, *sorted(seasons)]:
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
        kind: str,
        approvals: Mapping[str, list[_ShowApproval]],
        episodes: Mapping[str, list[tuple[str, int]]],
        kinds_by_season: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Each show approved on a season of this kind, once, with an episode made under its approval to watch when one
    is kept; shows with an episode come first. A kind one approval covers lists only approvals that can extend to it."""
    settings = []
    for show, found in approvals.items():
        candidates = [
            {"show": show, "from_prefix": entry.approval.prefix, **_size_goal_facts(entry.approval),
             "library_item_id": _episode_made_under(episodes[show], entry.seasons)}
            for entry in found
            if kinds_by_season.get(entry.sample_season) == kind
            and (kind not in APPROVABLE_KINDS or _extension_refusal(entry.approval) is None)
        ]
        if candidates:
            settings.append(max(candidates, key=lambda setting: setting["library_item_id"] is not None))
    return sorted(settings, key=lambda setting: (setting["library_item_id"] is None, setting["show"]))


SAMPLE_IN_HAND_STATUSES = SAMPLE_RUNNING_STATUSES | SAMPLE_WAITING_STATUSES | SAMPLE_FAILED_STATUSES | {"pending_review", "completed"}


def _samples_in_hand(connection: DBClient, seasons: Collection[str]) -> list[dict[str, Any]]:
    return [job for job in load_latest_jobs_for_prefixes(connection, list(seasons)).values()
            if job.get("lane") == "sample" and job.get("status") in SAMPLE_IN_HAND_STATUSES]


def _kind_report(
        connection: DBClient, config: MediaforceConfig, load: LoadCalibrationState, *, sample_seasons: Collection[str] = (),
) -> dict[str, Any]:
    def folder_approved(prefix: str) -> bool:
        approval = effective_production_approval(config, prefix, load)
        return approval is not None and approval.current

    return season_kind_report(connection, library_types=config.library_type_map, folder_approved=folder_approved,
                              measurement_roots=config.source_root_map,
                              sample_seasons=sample_seasons)


@dataclass(frozen=True, slots=True)
class _KindContents:
    episodes: dict[str, list[tuple[str, int]]]
    approvals: dict[str, list[_ShowApproval]]
    report: dict[str, Any]
    samples: dict[str, dict[str, Any]]


_request_kind_contents: ContextVar[tuple[MediaforceConfig, _KindContents] | None] = ContextVar("kind_contents", default=None)


@contextmanager
def kind_snapshot(connection: DBClient, config: MediaforceConfig, load: LoadCalibrationState) -> Iterator[None]:
    token = _request_kind_contents.set((config, _kind_contents(connection, config, load)))
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

    episodes = _tv_episodes(connection, config)
    approvals_by_show = {show: found for show, files in episodes.items()
                         if (found := _show_approvals(config, show, files, folders_only))}
    samples = _samples_in_hand(connection, {str(season) for files in episodes.values() for path, _ in files
                                                if (season := season_prefix_of(config, path))})
    report = _kind_report(
        connection, config, folders_only,
        sample_seasons={entry.sample_season for found in approvals_by_show.values() for entry in found}
                       | {str(job["prefix"]) for job in samples},
    )
    kinds_by_season = object_dict(report.get("kinds_by_season"))
    newest: dict[str, dict[str, Any]] = {}
    for job in sorted(samples, key=lambda sample: str(sample.get("created_at") or "")):
        kind = kinds_by_season.get(job["prefix"])
        if kind is None:
            continue
        if kind in APPROVABLE_KINDS and job.get("status") == "completed" and not _scalable_sample(
                load_calibration_state(config, str(job["prefix"]))):
            continue
        newest[str(kind)] = job
    return _KindContents(episodes, approvals_by_show, report, newest)


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
    seasons: dict[str, dict[str, list[tuple[int, str]]]] = {kind: {} for kind in KINDS_IN_ORDER}
    for row in report["seasons"]:
        if row["kind"] in seasons:
            season = str(row["season"])
            seasons[row["kind"]].setdefault("/".join(season.split("/")[:2]), []).append((int(row["waiting_bytes"]), season))
    # A show's sample is made on its biggest waiting season of the kind, the one that saves the most.
    shows = {kind: {show: {"seasons": len(found), "waiting_bytes": sum(size for size, _ in found),
                           "sample_season": min(found, key=lambda entry: (-entry[0], entry[1]))[1]}
                    for show, found in by_show.items()}
             for kind, by_show in seasons.items()}
    totals = report["totals"]
    approvals = {kind: _approval_facts(config, load_kind_approval(config, kind)) if kind in APPROVABLE_KINDS else None
                 for kind in KINDS_IN_ORDER}
    for approval in approvals.values():
        if approval:
            show = "/".join(str(approval["from_prefix"]).split("/")[:2])
            lent = next((entry for entry in approvals_by_show.get(show, []) if entry.approval.prefix == approval["from_prefix"]), None)
            approval["library_item_id"] = _episode_made_under(episodes.get(show, []), lent.seasons) if lent else None
    payload: dict[str, Any] = {
        "kinds": [{
            "kind": kind,
            "one_time": kind not in APPROVABLE_KINDS,
            "seasons": int(object_dict(totals.get(kind)).get("seasons") or 0),
            "waiting_bytes": int(object_dict(totals.get(kind)).get("waiting_bytes") or 0),
            "shows": [{"prefix": prefix, **values} for prefix, values in
                      sorted(shows[kind].items(), key=lambda entry: (-entry[1]["waiting_bytes"], entry[0]))],
            "settings": _lent_settings(kind, approvals_by_show, episodes, kinds_by_season),
            "approval": approvals[kind],
            "sample_season": _approved_sample_season(config, kind) or object_dict(contents.samples.get(kind)).get("prefix"),
            "needs_you": kind in APPROVABLE_KINDS and approvals[kind] is None
                         and object_dict(contents.samples.get(kind)).get("status") in {"pending_review", "completed"},
        } for kind in KINDS_IN_ORDER],
        "left_out": [{"season": row["season"], "kind": row["kind"], "waiting_bytes": int(row["waiting_bytes"])}
                     for row in report["seasons"] if row["kind"] in LEFT_OUT_KINDS],
        "mixed_seasons": int(object_dict(totals.get("mixed")).get("seasons") or 0),
        "unmeasured_seasons": int(object_dict(totals.get("unmeasured")).get("seasons") or 0),
        "folder_approved_seasons": int(object_dict(totals.get("folder_approved")).get("seasons") or 0),
    }
    for entry in payload["kinds"]:
        if entry["needs_you"]:
            season = str(entry["sample_season"])
            entry["needs_you"] = sample_approval_kind(
                config, payload, season, load_calibration_state(config, season),
                kind_of=lambda _config, prefix: kinds_by_season.get(prefix),
            ) == entry["kind"]
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
    payload = object_dict(calibration)
    if kind is None or not _scalable_sample(payload):
        return None
    return kind if _sample_kind_refusal(config, kind, payload, kind_of) is None else None


def _scalable_sample(calibration: Mapping[str, Any] | None) -> bool:
    payload = object_dict(calibration)
    if not payload.get("job_id") or str(payload.get("mode") or "sample") != "sample":
        return False
    contract = production_approval_contract({
        **payload, "accepted_sample_job_id": payload["job_id"], "accepted_policy_hash": calibration_policy_hash(payload),
    })
    return contract is not None and _size_extension_refusal(contract) is None


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
    if refusal := _sample_kind_refusal(config, kind, approval.calibration, kind_of):
        return {"ok": False, "code": refusal}
    return approve_kind(config, kind, approval.prefix, approval.calibration, approved_at=approved_at)
