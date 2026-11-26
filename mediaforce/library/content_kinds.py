import json
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import batched
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.utils import timestamp
from mediaforce.library.animation_probe import ANIMATION_PROBE, animation_look

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import animation_sorts, library_item_evidence_state, library_items, plex_item_metadata, series_metadata, source_admissions, source_measurements, \
    source_transitions, source_versions, source_moves, staged_artifacts, target_production_outcomes, motion_keep_decisions
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.library.candidate_selection import season_identity
from mediaforce.library.library_settings import normalize_library_type
from mediaforce.library.movie_workflow import classify_movie_path
from mediaforce.library.calculation_reuse import reuse_library_calculation
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES, SourceEvidence, load_source_evidence_bulk, load_source_evidence_context

# Kinds of content ("looks" on screen) group seasons so that one approval can cover every season of the same look
# (Director decision, #517, 2026-10-22). Live action is one look: darkness runs on a smooth scale with no gap, so
# splitting it left a third of the waiting seasons in no look (Director decision, #1385). 4K is a picture height, not
# a look: a 4K season takes its look from its other traits, and a look's approval says whether 4K stays 4K.
ContentKind = Literal["live_action", "cartoon"]
SeasonKind = Literal["live_action", "cartoon", "unmeasured"]

CONTENT_KINDS_VERSION = 4
AUTOMATIC_ANIMATION_SORTING = False
ANIMATION_GENRE = "animation"


@dataclass(frozen=True, slots=True)
class SeasonGrouping:
    kind: SeasonKind
    episodes: int
    measured: int
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "episodes": self.episodes, "measured": self.measured, "reason": self.reason}


def fingerprint_measured(fingerprint_summary: Mapping[str, Any] | None) -> bool:
    """Whether an original's media fingerprint summary holds its measured picture."""
    aggregate = object_dict(object_dict(object_dict(fingerprint_summary).get("analysis")).get("aggregate"))
    return "dark_frame_fraction" in aggregate and "duplicate_like_frame_fraction" in aggregate


def is_animated(genres: Sequence[str] | None) -> bool | None:
    """Whether a series is animated by its TMDB genres; None when the genres are unknown."""
    if not genres:
        return None
    return any(str(genre).strip().casefold() == ANIMATION_GENRE for genre in genres)


def season_kind(measured: Sequence[bool], *, animated: bool | None, sort: "AnimationTitle | None" = None) -> SeasonGrouping:
    count = sum(measured)

    def grouping(kind: SeasonKind, reason: str | None = None) -> SeasonGrouping:
        return SeasonGrouping(kind, len(measured), count, reason)

    if not count:
        return grouping("unmeasured", "no_measured_original")
    if sort or animated:
        if not AUTOMATIC_ANIMATION_SORTING:
            return grouping("unmeasured", "animation_sort_on_hold")
        if sort is None or not sort.measured:
            return grouping("unmeasured", "animation_probe_pending")
        return grouping(sort.look)
    if animated is None:
        return grouping("unmeasured", "genres_unknown")
    return grouping("live_action")


def _waiting(item: Mapping[str, Any]) -> bool:
    return str(item.get("status") or "") != "promoted" and str(item.get("video_codec") or "").casefold() != "av1"


def _admitted_item_ids(connection: DBClient, *, item_ids: Collection[int] | None = None) -> set[int]:
    """Items whose active source has an admitted original; the reader gives every other item no traits."""
    query = (select(library_items.c.id, source_versions.c.generation_id)
             .join(source_versions, source_versions.c.version_id == library_items.c.active_source_version_id))
    queries = (query,) if item_ids is None else (
        query.where(library_items.c.id.in_(batch)) for batch in batched(item_ids, 500)
    )
    active_sources = [row for scoped_query in queries for row in connection.execute(scoped_query)]
    generations = {row.generation_id for row in active_sources}
    if not generations:
        return set()
    admissions = select(source_admissions.c.generation_id)
    admission_queries = (admissions,) if item_ids is None else (
        admissions.where(source_admissions.c.generation_id.in_(batch)) for batch in batched(generations, 500)
    )
    admitted = {generation for admission_query in admission_queries for generation in connection.execute(admission_query).scalars()}
    return {int(row.id) for row in active_sources if row.generation_id in admitted}


def _measured_originals(
        episodes: Sequence[Mapping[str, Any]],
        admitted: set[int],
        evidence_by_item: Mapping[int, SourceEvidence],
) -> list[bool]:
    """For each episode, whether its admitted original's picture is measured with a trusted fingerprint."""
    measured = []
    for item in episodes:
        evidence = evidence_by_item.get(int(item["id"])) if int(item["id"]) in admitted else None
        fingerprint = evidence.traits["media_fingerprint"] if evidence is not None and evidence.original_version_id is not None else None
        measured.append(fingerprint is not None and fingerprint.tier in {"proven", "probable"} and fingerprint_measured(fingerprint.summary))
    return measured


def _in_folder(prefix: str) -> tuple[Any, Any]:
    """Every path under the folder, as a range the rel_path index answers ("0" sorts right after "/")."""
    return library_items.c.rel_path >= f"{prefix}/", library_items.c.rel_path < f"{prefix}0"


def _present_items(connection: DBClient, season_prefix: str | None = None) -> Sequence[Mapping[str, Any]]:
    query = select(
        library_items.c.id, library_items.c.rel_path, library_items.c.media_root, library_items.c.status, library_items.c.video_codec,
        library_items.c.source_path, source_versions.c.role, library_items.c.size_bytes, func.coalesce(library_items.c.content_version_fingerprint, library_items.c.fingerprint).label("source"),
        plex_item_metadata.c.plex_genres_json,
    ).select_from(library_items).outerjoin(source_versions, source_versions.c.version_id == library_items.c.active_source_version_id).outerjoin(plex_item_metadata, plex_item_metadata.c.library_item_id == library_items.c.id).where(library_items.c.missing_since.is_(None))
    if season_prefix is not None:
        # A movie that sits directly in its library is its own title.
        query = query.where(or_(library_items.c.rel_path == season_prefix, and_(*_in_folder(season_prefix))))
    return connection.execute(query).mappings().all()


def look_group(library_types: Mapping[str, str], rel_path: str) -> str | None:
    """The unit a look covers that holds this path: a TV season, or a movie title for a movie's feature file.

    Files sitting directly in a show folder are in no season; a movie's extras and unrecognised nested files are in
    no title, so a look never covers them.
    """
    return _look_group(tuple(library_types.items()), str(rel_path or ""))


@lru_cache(maxsize=1 << 17)
def _look_group(library_type_items: tuple[tuple[str, str], ...], rel_path: str) -> str | None:
    library_types = dict(library_type_items)
    path = PurePosixPath(rel_path.strip().strip("/"))
    if not path.parts:
        return None
    root = path.parts[0]
    library_type = normalize_library_type(library_types.get(root), key=root)
    if library_type == "movie":
        membership = classify_movie_path(path.as_posix(), root=root)
        return membership.title_prefix if membership is not None and membership.role == "feature" else None
    if len(path.parts) < 3 or (len(path.parts) == 3 and path.suffix):
        return None
    identity = season_identity({"rel_path": path.as_posix()}, library_types=library_types)
    return identity.season_prefix if identity is not None else None


def _seasons(
        items: Sequence[Mapping[str, Any]], library_types: Mapping[str, str],
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, str]]:
    """Files by the season or movie title a look covers, and each TV season's show; a movie title has no show."""
    seasons: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    series_of: dict[str, str] = {}
    for item in items:
        group = look_group(library_types, str(item["rel_path"]))
        if group is None:
            continue
        seasons[group].append(item)
        if (identity := season_identity(dict(item), library_types=library_types)) is not None:
            series_of[group] = identity.series_prefix
    return seasons, series_of


def _animated(episodes: Sequence[Mapping[str, Any]], series: str | None, animation: Mapping[str, bool | None]) -> bool | None:
    if series is not None:
        return animation.get(series)
    return any(_animated_genres(item["plex_genres_json"]) for item in episodes)


def _animated_genres(encoded: str | None) -> bool | None:
    try:
        return is_animated(object_list(json.loads(encoded or "[]")))
    except json.JSONDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class AnimationTitle:
    prefix: str
    item_id: int
    source: str
    kbps: float | None
    flat_fraction: float | None
    hdr: bool | None
    catalog_source: str
    original_path: str | None
    error: str | None = None

    @property
    def measured(self) -> bool:
        return bool(self.hdr) or (self.kbps is not None and self.flat_fraction is not None)

    @property
    def look(self) -> SeasonKind:
        if AUTOMATIC_ANIMATION_SORTING:
            if self.hdr:
                return "live_action"
            if self.kbps is not None and self.flat_fraction is not None:
                return animation_look(self.kbps, self.flat_fraction)
        return "unmeasured"

    def payload(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "source": self.source, "kbps": self.kbps, "flat_fraction": self.flat_fraction,
                "look": self.look, "hdr": self.hdr, "on_hold": not AUTOMATIC_ANIMATION_SORTING,
                "error": self.error, "original_missing": self.original_path is None and not self.measured}


def _animation_titles(connection: DBClient, seasons: Mapping[str, list[Mapping[str, Any]]], series_of: Mapping[str, str],
                      genres: Mapping[str, bool | None], *, prefix: str | None = None,
                      known_sources: Mapping[int, SourceEvidence] | None = None) -> dict[str, AnimationTitle]:
    query = select(animation_sorts)
    saved = {row.title: row for row in connection.execute(query if prefix is None else query.where(animation_sorts.c.title == prefix))}
    animated = {title: episodes for title, episodes in seasons.items() if title in saved or _animated(episodes, series_of.get(title), genres)}
    published = [int(item["id"]) for episodes in animated.values() for item in episodes if item["status"] == "promoted" or item["role"] == "output"]
    originals = dict(known_sources or {})
    if missing := set(published).difference(originals):
        originals.update(load_source_evidence_bulk(connection, missing))
    failures = {row.library_item_id: row for row in connection.execute(select(library_item_evidence_state.c.library_item_id,
        library_item_evidence_state.c.work_source_fingerprint, library_item_evidence_state.c.last_error).where(
        library_item_evidence_state.c.evidence_kind == ANIMATION_PROBE, library_item_evidence_state.c.work_status == "failed"))}
    titles = {}
    for title, episodes in animated.items():
        record = saved.get(title)
        candidates = []
        for item in episodes:
            original = originals.get(int(item["id"]))
            catalog_source = str(item["source"] or "")
            if item["status"] == "promoted" or item["role"] == "output":
                source = str(original.profile.get("content_version_fingerprint") or "") if original else ""
                path = original.source_path if original and original.availability == "available" else None
            else:
                source, path = catalog_source, item["source_path"]
            current = record if record is not None and record.item_id == item["id"] and record.source == source else None
            failure = failures.get(int(item["id"]))
            error = failure.last_error if failure and failure.work_source_fingerprint == catalog_source and current else None
            candidate = AnimationTitle(title, int(item["id"]), source, current.kbps if current is not None else None, current.flat_fraction if current is not None else None,
                                       current.hdr if current is not None else None,
                                       catalog_source, path, error)
            candidates.append((candidate, str(item["rel_path"])))
        titles[title] = min(candidates, key=lambda pair: (not pair[0].measured, pair[0].original_path is None,
                                                          record is None or pair[0].item_id != record.item_id, pair[1]))[0]
    return titles


def queue_animation_probes(connection: DBClient, config: MediaforceConfig, *, limit: int = 25) -> int:
    from mediaforce.library.evidence_queue import EVIDENCE_WORK_ACTIVE_STATUSES, queue_decision_evidence_work
    from mediaforce.library.background_work import background_work_is_paused

    if not AUTOMATIC_ANIMATION_SORTING or background_work_is_paused(connection):
        return 0
    seasons, shows = _seasons(_present_items(connection), config.library_type_map)
    queued = {row.library_item_id: row.work_source_fingerprint for row in connection.execute(
        select(library_item_evidence_state).where(library_item_evidence_state.c.evidence_kind == ANIMATION_PROBE,
            library_item_evidence_state.c.work_status.in_((*EVIDENCE_WORK_ACTIVE_STATUSES, "failed"))))}
    count = 0
    for title in _animation_titles(connection, seasons, shows, _series_animation(connection)).values():
        root = title.prefix.partition("/")[0]
        if title.measured or not title.original_path or not title.source or root not in config.source_root_map or queued.get(title.item_id) == title.catalog_source:
            continue
        with connection.begin_nested():
            values = {"item_id": title.item_id, "source": title.source, "kbps": None, "hdr": None, "flat_fraction": None}
            statement = sqlite_insert(animation_sorts).values(title=title.prefix, **values)
            connection.execute(statement.on_conflict_do_update(index_elements=[animation_sorts.c.title], set_=values,
                where=or_(animation_sorts.c.source != title.source, animation_sorts.c.item_id != title.item_id)))
            if connection.execute(select(animation_sorts.c.hdr).where(animation_sorts.c.title == title.prefix)).scalar_one() is not None:
                continue
            connection.execute(sqlite_insert(library_item_evidence_state).values(
                library_item_id=title.item_id, evidence_kind=ANIMATION_PROBE, state="analysis_required", policy_hash=ANIMATION_PROBE,
                updated_at=timestamp()).on_conflict_do_update(index_elements=[library_item_evidence_state.c.library_item_id, library_item_evidence_state.c.evidence_kind],
                                                      set_={"state": "analysis_required"}))
            result = queue_decision_evidence_work(connection, config, root, library_item_ids=[title.item_id], evidence_kind=ANIMATION_PROBE,
                                                  work_reason=ANIMATION_PROBE, priority=100, manage_transaction=False)
            count += result["prepared_count"]
        if count >= limit:
            break
    return count


def current_animation_title(connection: DBClient, prefix: str, library_types: Mapping[str, str]) -> AnimationTitle | None:
    seasons, shows = _seasons(_present_items(connection, prefix), library_types)
    series = shows.get(prefix)
    return _animation_titles(connection, seasons, shows, _series_animation(connection, series) if series else {}, prefix=prefix).get(prefix)


def retry_animation_probe(connection: DBClient, config: MediaforceConfig, prefix: str, source: str) -> bool:
    from mediaforce.library.evidence_queue import queue_decision_evidence_work

    title = current_animation_title(connection, prefix, config.library_type_map)
    if not AUTOMATIC_ANIMATION_SORTING or not title or not title.error or title.source != source or not title.original_path:
        return False
    return bool(queue_decision_evidence_work(connection, config, prefix, library_item_ids=[title.item_id], evidence_kind=ANIMATION_PROBE,
                                          work_reason=ANIMATION_PROBE, priority=100, restart_terminal=True)["prepared_count"])


def season_grouping(
        connection: DBClient,
        season_prefix: str,
        *,
        library_types: Mapping[str, str],
) -> SeasonGrouping:
    seasons, series_of = _seasons(_present_items(connection, season_prefix), library_types)
    episodes = seasons.get(season_prefix, [])
    admitted = _admitted_item_ids(connection, item_ids=[int(item["id"]) for item in episodes])
    if not admitted:
        return season_kind([False] * len(episodes), animated=None)
    evidence_by_item = load_source_evidence_bulk(
        connection, (int(item["id"]) for item in episodes if int(item["id"]) in admitted),
        context=load_source_evidence_context(connection),
    )
    measured = _measured_originals(episodes, admitted, evidence_by_item)
    series = series_of.get(season_prefix)
    genres = _series_animation(connection, series) if series else {}
    sorts = _animation_titles(connection, seasons, series_of, genres, prefix=season_prefix, known_sources=evidence_by_item)
    return season_kind(measured, animated=_animated(episodes, series, genres), sort=sorts.get(season_prefix))


def _series_animation(connection: DBClient, series_prefix: str | None = None) -> dict[str, bool | None]:
    animation: dict[str, bool | None] = {}
    query = select(series_metadata.c.series_prefix, series_metadata.c.tmdb_genres_json)
    if series_prefix is not None:
        query = query.where(series_metadata.c.series_prefix == series_prefix)
    for row in connection.execute(query):
        animation[str(row.series_prefix)] = _animated_genres(row.tmdb_genres_json)
    return animation


def _failed_measurements(connection: DBClient) -> dict[int, str]:
    rows = connection.execute(select(library_item_evidence_state.c.library_item_id, library_item_evidence_state.c.last_error)
                              .join(library_items, library_items.c.id == library_item_evidence_state.c.library_item_id)
                              .where(library_items.c.missing_since.is_(None),
                                     library_item_evidence_state.c.evidence_kind == "media_fingerprint",
                                     library_item_evidence_state.c.work_status == "failed",
                                     library_item_evidence_state.c.work_source_fingerprint == func.coalesce(
                                         library_items.c.content_version_fingerprint, library_items.c.fingerprint))).mappings()
    return {int(row["library_item_id"]): str(row["last_error"] or "measurement_failed") for row in rows}


def _measurement_reasons(
        episodes: Sequence[Mapping[str, Any]], evidence: Mapping[int, SourceEvidence], failed: Mapping[int, str],
        measurement_roots: Collection[str] | None,
) -> list[str]:
    reasons = {trait.reason for item in episodes if int(item["id"]) in evidence
               for trait in [evidence[int(item["id"])].traits["media_fingerprint"]]
               if trait.reason not in {None, "missing_source_measurement", "source_attachment_unresolved", "original_rule_changed"}}
    for item in episodes:
        original = evidence.get(int(item["id"]))
        if original is not None:
            if original.availability == "unavailable":
                reasons.add("original_unavailable")
        elif item["video_codec"] and item["video_codec"] not in HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]:
            reasons.add("original_format_unidentified")
        if measurement_roots is not None and item["media_root"] not in measurement_roots:
            reasons.add("measurement_library_disabled")
    reasons.update(failed[int(item["id"])] for item in episodes if int(item["id"]) in failed)
    return sorted(reason for reason in reasons if reason)


def season_kind_report(
        connection: DBClient,
        *,
        library_types: Mapping[str, str],
        folder_approved: Callable[[str], bool],
        sample_seasons: Collection[str] = (),
    measurement_roots: Collection[str] | None = None,
) -> dict[str, Any]:
    items = _present_items(connection)
    seasons, series_of = _seasons(items, library_types)
    # A file kept original is finished; it still measures as an original but no longer waits.
    kept = library_item_ids_kept_original(connection, [int(item["id"]) for item in items])
    waiting_by_season = {prefix: [item for item in episodes if _waiting(item) and int(item["id"]) not in kept]
                         for prefix, episodes in seasons.items()}
    approved = {prefix for prefix, waiting in waiting_by_season.items() if waiting and folder_approved(prefix)}
    measured_seasons = {prefix for prefix, waiting in waiting_by_season.items()
                        if (waiting and prefix not in approved) or prefix in sample_seasons}
    from mediaforce.library.source_intake import encoder_rule_hash

    confirmations = _original_confirmation_inputs(connection)

    animation = _series_animation(connection)
    def measure() -> tuple[dict[str, list[str]], dict[str, SeasonGrouping], dict[str, AnimationTitle]]:
        admitted = _admitted_item_ids(connection)
        context = load_source_evidence_context(connection)
        evidence = load_source_evidence_bulk(
            connection, (int(item["id"]) for prefix in measured_seasons for item in seasons[prefix] if int(item["id"]) in admitted),
            context=context,
        )
        animation_titles = _animation_titles(connection, seasons, series_of, animation, known_sources=evidence)
        measured_groupings = {prefix: season_kind(_measured_originals(seasons[prefix], admitted, evidence),
                                         animated=_animated(seasons[prefix], series_of.get(prefix), animation), sort=animation_titles.get(prefix)) for prefix in measured_seasons}
        reasons = {prefix: _measurement_reasons(waiting_by_season[prefix], evidence, {}, measurement_roots)
                   for prefix, measured_grouping in measured_groupings.items() if measured_grouping.reason == "no_measured_original"}
        return reasons, measured_groupings, animation_titles

    reasons_by_season, groupings, sorts = reuse_library_calculation(
        connection, "season_measurements",
        (select(library_item_evidence_state.c.library_item_id, library_item_evidence_state.c.work_source_fingerprint,
                library_item_evidence_state.c.last_error).where(library_item_evidence_state.c.evidence_kind == ANIMATION_PROBE,
                                                               library_item_evidence_state.c.work_status == "failed"),
         select(library_items.c.id, library_items.c.rel_path, library_items.c.media_root, library_items.c.video_codec,
                library_items.c.size_bytes, library_items.c.status, library_items.c.active_source_version_id,
                library_items.c.content_version_fingerprint, library_items.c.fingerprint, library_items.c.missing_since),
         *(select(table) for table in (source_versions, source_admissions, source_measurements, source_transitions, source_moves,
                                      staged_artifacts, target_production_outcomes, series_metadata, plex_item_metadata, motion_keep_decisions, animation_sorts))),
        (frozenset(measured_seasons), tuple(sorted(library_types.items())), confirmations,
         None if measurement_roots is None else frozenset(measurement_roots),
         json.dumps(HISTORICAL_OUTPUT_PROFILES, sort_keys=True), encoder_rule_hash()), measure,
        inputs_current=lambda: confirmations == _original_confirmation_inputs(connection),
    )
    failed = _failed_measurements(connection)
    rows = []
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    for prefix in sorted(seasons):
        waiting = waiting_by_season[prefix]
        if not waiting:
            continue
        if prefix in approved:
            totals["folder_approved"]["seasons"] += 1
            continue
        grouping = groupings[prefix]
        waiting_bytes = sum(int_value(item["size_bytes"]) for item in waiting)
        totals[grouping.kind]["seasons"] += 1
        totals[grouping.kind]["waiting_bytes"] += waiting_bytes
        rows.append({"season": prefix, **grouping.to_payload(),
                     "measurement_reasons": sorted(set(reasons_by_season.get(prefix, ()))
                                                   | {failed[int(item["id"])] for item in waiting if int(item["id"]) in failed})
                     if grouping.reason == "no_measured_original" else [],
                     "waiting_episodes": len(waiting),
                     "waiting_bytes": waiting_bytes})
    report: dict[str, Any] = {
        "content_kinds_version": CONTENT_KINDS_VERSION,
        "animation_titles": [sorts[prefix].payload() for prefix in sorted(sorts)],
        "totals": {kind: dict(counts) for kind, counts in sorted(totals.items())},
        "unmeasured_reasons": dict(Counter(row["reason"] for row in rows if row["kind"] == "unmeasured")),
        "seasons": rows,
    }
    if sample_seasons:
        report["kinds_by_season"] = {prefix: groupings[prefix].kind for prefix in sample_seasons if prefix in groupings}
    return report


def _original_confirmation_inputs(connection: DBClient) -> list[tuple[str, bytes | None]]:
    from mediaforce.library.original_registration import confirmation_files

    references = connection.execute(select(source_admissions.c.confirmation_ref).where(
        source_admissions.c.tier == "proven", source_admissions.c.basis == "owner",
    ).distinct()).scalars()
    inputs = []
    for reference in sorted(str(value or "") for value in references):
        try:
            paths = confirmation_files(reference.removeprefix("mac-app:"))
        except ValueError:
            continue
        for path in paths:
            try:
                inputs.append((str(path), path.read_bytes()))
            except OSError:
                inputs.append((str(path), None))
    return inputs
