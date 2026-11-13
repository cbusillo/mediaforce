import json
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Any, Literal

from sqlalchemy import func, select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items, series_metadata, source_admissions, source_measurements, \
    source_transitions, source_versions, source_moves, staged_artifacts, target_production_outcomes, motion_keep_decisions
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.library.candidate_selection import season_identity
from mediaforce.library.calculation_reuse import reuse_library_calculation
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES, SourceEvidence, load_source_evidence_bulk, load_source_evidence_context

# Kinds of content group seasons by number ranges of their originals' measured traits, so that one
# approval can cover every season that measures the same (Director decision, #517, 2026-10-22).
# The ranges were chosen against the live library on 2026-10-25; see #1046 for the evidence.
# 4K is a picture height, not a kind: a 4K season takes its kind from its other traits, and each kind's approval
# says whether 4K stays 4K or comes down to the library's tallest picture (#1385).
ContentKind = Literal["darker_live_action", "bright_live_action", "cartoon"]
SeasonKind = Literal["darker_live_action", "bright_live_action", "cartoon", "mixed", "unmeasured"]
EpisodeSide = Literal["darker", "bright", "between", "repeated_frames"]

CONTENT_KINDS_VERSION = 1
# An episode is darker when at least this share of its measured frames is dark, and bright below the
# lower bound. Episodes between the two fit either side, so close measurements never split a show.
DARKER_MIN_DARK_FRAMES = 0.20
BRIGHT_MAX_DARK_FRAMES = 0.10
# Live action rarely repeats frames (90% of its episodes are under 0.16); above this an episode does
# not look like live action, whatever its darkness.
LIVE_ACTION_MAX_REPEATED_FRAMES = 0.40
# A season belongs to a kind when this share of its sided episodes agree; otherwise it is mixed.
SEASON_AGREEMENT = 0.80
ANIMATION_GENRE = "animation"


@dataclass(frozen=True, slots=True)
class EpisodeTraits:
    """An original's traits; darkness and repeated frames are None when its fingerprint is unmeasured."""

    dark_frame_fraction: float | None
    repeated_frame_fraction: float | None


@dataclass(frozen=True, slots=True)
class SeasonGrouping:
    kind: SeasonKind
    episodes: int
    measured: int
    sides: Mapping[EpisodeSide, int]
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "episodes": self.episodes, "measured": self.measured,
                "sides": dict(sorted(self.sides.items())), "reason": self.reason}


def episode_traits(fingerprint_summary: Mapping[str, Any] | None) -> EpisodeTraits:
    """Traits from an original's media fingerprint summary, if measured."""
    aggregate = object_dict(object_dict(object_dict(fingerprint_summary).get("analysis")).get("aggregate"))
    measured = "dark_frame_fraction" in aggregate and "duplicate_like_frame_fraction" in aggregate
    return EpisodeTraits(
        dark_frame_fraction=float_value(aggregate["dark_frame_fraction"]) if measured else None,
        repeated_frame_fraction=float_value(aggregate["duplicate_like_frame_fraction"]) if measured else None,
    )


def episode_side(traits: EpisodeTraits) -> EpisodeSide | None:
    """Where one episode falls; None when its fingerprint is unmeasured."""
    if traits.dark_frame_fraction is None or traits.repeated_frame_fraction is None:
        return None
    if traits.repeated_frame_fraction >= LIVE_ACTION_MAX_REPEATED_FRAMES:
        return "repeated_frames"
    if traits.dark_frame_fraction >= DARKER_MIN_DARK_FRAMES:
        return "darker"
    if traits.dark_frame_fraction < BRIGHT_MAX_DARK_FRAMES:
        return "bright"
    return "between"


def is_animated(genres: Sequence[str] | None) -> bool | None:
    """Whether a series is animated by its TMDB genres; None when the genres are unknown."""
    if not genres:
        return None
    return any(str(genre).strip().casefold() == ANIMATION_GENRE for genre in genres)


def season_kind(episodes: Sequence[EpisodeTraits | None], *, animated: bool | None) -> SeasonGrouping:
    """Group one season by its episodes' original traits; unmeasured episodes do not vote."""
    measured: list[EpisodeSide] = []
    for traits in episodes:
        side = episode_side(traits) if traits is not None else None
        if side is not None:
            measured.append(side)
    sides: Counter[EpisodeSide] = Counter()
    sides.update(measured)

    def grouping(kind: SeasonKind, reason: str | None = None) -> SeasonGrouping:
        return SeasonGrouping(kind, len(episodes), len(measured), sides.copy(), reason)

    if not measured:
        return grouping("unmeasured", "no_measured_original")
    # Cartoons stay one-time Director choices, so they never fall into a live-action kind.
    if animated:
        return grouping("cartoon")
    if animated is None:
        return grouping("unmeasured", "genres_unknown")
    sided = sides["darker"] + sides["bright"] + sides["repeated_frames"]
    if not sided:
        return grouping("mixed", "all_between")
    if sides["darker"] >= SEASON_AGREEMENT * sided:
        return grouping("darker_live_action")
    if sides["bright"] >= SEASON_AGREEMENT * sided:
        return grouping("bright_live_action")
    return grouping("mixed", "sides_disagree")


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


def _original_traits(
        episodes: Sequence[Mapping[str, Any]],
        admitted: set[int],
        evidence_by_item: Mapping[int, SourceEvidence],
) -> list[EpisodeTraits | None]:
    traits: list[EpisodeTraits | None] = []
    for item in episodes:
        if int(item["id"]) not in admitted:
            traits.append(None)
            continue
        evidence = evidence_by_item[int(item["id"])]
        if evidence.original_version_id is None:
            traits.append(None)
            continue
        fingerprint = evidence.traits["media_fingerprint"]
        traits.append(episode_traits(fingerprint.summary if fingerprint.tier in {"proven", "probable"} else None))
    return traits


def _in_folder(prefix: str) -> tuple[Any, Any]:
    """Every path under the folder, as a range the rel_path index answers ("0" sorts right after "/")."""
    return library_items.c.rel_path >= f"{prefix}/", library_items.c.rel_path < f"{prefix}0"


def _present_items(connection: DBClient, season_prefix: str | None = None) -> Sequence[Mapping[str, Any]]:
    query = select(
        library_items.c.id, library_items.c.rel_path, library_items.c.media_root, library_items.c.status, library_items.c.video_codec,
        library_items.c.size_bytes,
    ).where(library_items.c.missing_since.is_(None))
    if season_prefix is not None:
        query = query.where(*_in_folder(season_prefix))
    return connection.execute(query).mappings().all()


def _seasons(
        items: Sequence[Mapping[str, Any]], library_types: Mapping[str, str],
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, str]]:
    """Episodes by season, and each season's show; files sitting directly in a show folder are in no season."""
    seasons: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    series_of: dict[str, str] = {}
    for item in items:
        identity = season_identity(dict(item), library_types=library_types)
        if identity is None or len(str(item["rel_path"]).split("/")) < 4:
            continue
        seasons[identity.season_prefix].append(item)
        series_of[identity.season_prefix] = identity.series_prefix
    return seasons, series_of


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
        return season_kind([None] * len(episodes), animated=None)
    evidence_by_item = load_source_evidence_bulk(
        connection, (int(item["id"]) for item in episodes if int(item["id"]) in admitted),
        context=load_source_evidence_context(connection),
    )
    traits = _original_traits(episodes, admitted, evidence_by_item)
    series = series_of.get(season_prefix)
    animated = _series_animation(connection, series).get(series) if series else None
    return season_kind(traits, animated=animated)


def _series_animation(connection: DBClient, series_prefix: str | None = None) -> dict[str, bool | None]:
    animation: dict[str, bool | None] = {}
    query = select(series_metadata.c.series_prefix, series_metadata.c.tmdb_genres_json)
    if series_prefix is not None:
        query = query.where(series_metadata.c.series_prefix == series_prefix)
    for row in connection.execute(query):
        try:
            genres = [str(genre) for genre in object_list(json.loads(row.tmdb_genres_json or "[]"))]
        except json.JSONDecodeError:
            genres = []
        animation[str(row.series_prefix)] = is_animated(genres)
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

    def measure() -> tuple[dict[str, list[str]], dict[str, SeasonGrouping]]:
        admitted = _admitted_item_ids(connection)
        context = load_source_evidence_context(connection)
        animation = _series_animation(connection)
        evidence = load_source_evidence_bulk(
            connection, (int(item["id"]) for prefix in measured_seasons for item in seasons[prefix] if int(item["id"]) in admitted),
            context=context,
        )
        measured_groupings = {prefix: season_kind(_original_traits(seasons[prefix], admitted, evidence),
                                         animated=animation.get(series_of[prefix])) for prefix in measured_seasons}
        reasons = {prefix: _measurement_reasons(waiting_by_season[prefix], evidence, {}, measurement_roots)
                   for prefix, measured_grouping in measured_groupings.items() if measured_grouping.reason == "no_measured_original"}
        return reasons, measured_groupings

    reasons_by_season, groupings = reuse_library_calculation(
        connection, "season_measurements",
        (select(library_items.c.id, library_items.c.rel_path, library_items.c.media_root, library_items.c.video_codec,
                library_items.c.size_bytes, library_items.c.status, library_items.c.active_source_version_id,
                library_items.c.content_version_fingerprint, library_items.c.fingerprint, library_items.c.missing_since),
         *(select(table) for table in (source_versions, source_admissions, source_measurements, source_transitions, source_moves,
                                      staged_artifacts, target_production_outcomes, series_metadata, motion_keep_decisions))),
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
        "ranges": {
            "darker_min_dark_frames": DARKER_MIN_DARK_FRAMES,
            "bright_max_dark_frames": BRIGHT_MAX_DARK_FRAMES,
            "live_action_max_repeated_frames": LIVE_ACTION_MAX_REPEATED_FRAMES,
            "season_agreement": SEASON_AGREEMENT,
        },
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
