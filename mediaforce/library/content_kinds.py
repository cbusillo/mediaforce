import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, series_metadata, source_admissions, source_versions
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.library.candidate_selection import season_identity
from mediaforce.library.source_evidence import load_source_evidence, load_source_evidence_context

# Kinds of content group seasons by number ranges of their originals' measured traits, so that one
# approval can cover every season that measures the same (Director decision, #517, 2026-10-22).
# The ranges were chosen against the live library on 2026-10-25; see #1046 for the evidence.
ContentKind = Literal["darker_live_action", "bright_live_action", "cartoon", "4k"]
SeasonKind = Literal["darker_live_action", "bright_live_action", "cartoon", "4k", "mixed", "unmeasured"]
EpisodeSide = Literal["darker", "bright", "between", "repeated_frames", "4k"]

CONTENT_KINDS_VERSION = 1
# An episode is darker when at least this share of its measured frames is dark, and bright below the
# lower bound. Episodes between the two fit either side, so close measurements never split a show.
DARKER_MIN_DARK_FRAMES = 0.20
BRIGHT_MAX_DARK_FRAMES = 0.10
# Live action rarely repeats frames (90% of its episodes are under 0.16); above this an episode does
# not look like live action, whatever its darkness.
LIVE_ACTION_MAX_REPEATED_FRAMES = 0.40
UHD_MIN_HEIGHT = 1600
UHD_MIN_WIDTH = 3000
# A season belongs to a kind when this share of its sided episodes agree; otherwise it is mixed.
SEASON_AGREEMENT = 0.80
ANIMATION_GENRE = "animation"


@dataclass(frozen=True, slots=True)
class EpisodeTraits:
    dark_frame_fraction: float
    repeated_frame_fraction: float
    height: int | None
    width: int | None


@dataclass(frozen=True, slots=True)
class SeasonGrouping:
    kind: SeasonKind
    episodes: int
    measured: int
    sides: dict[EpisodeSide, int]
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "episodes": self.episodes, "measured": self.measured,
                "sides": dict(sorted(self.sides.items())), "reason": self.reason}


def episode_traits(fingerprint_summary: Mapping[str, Any] | None, profile: Mapping[str, Any]) -> EpisodeTraits | None:
    """Traits from an original's media fingerprint summary and its stream profile, or None if unmeasured."""
    analysis = object_dict(object_dict(fingerprint_summary).get("analysis"))
    aggregate = object_dict(analysis.get("aggregate"))
    if "dark_frame_fraction" not in aggregate or "duplicate_like_frame_fraction" not in aggregate:
        return None
    return EpisodeTraits(
        dark_frame_fraction=float_value(aggregate["dark_frame_fraction"]),
        repeated_frame_fraction=float_value(aggregate["duplicate_like_frame_fraction"]),
        height=int_value(profile.get("height")) or None,
        width=int_value(profile.get("width")) or None,
    )


def episode_side(traits: EpisodeTraits) -> EpisodeSide:
    if (traits.height or 0) >= UHD_MIN_HEIGHT or (traits.width or 0) >= UHD_MIN_WIDTH:
        return "4k"
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
    measured = [traits for traits in episodes if traits is not None]
    sides = Counter(episode_side(traits) for traits in measured)

    def grouping(kind: SeasonKind, reason: str | None = None) -> SeasonGrouping:
        return SeasonGrouping(kind, len(episodes), len(measured), dict(sides), reason)

    if not measured:
        return grouping("unmeasured", "no_measured_original")
    # Cartoons and 4K stay one-time Director choices, so they never fall into a live-action kind.
    if sides["4k"]:
        return grouping("4k") if sides["4k"] == len(measured) else grouping("mixed", "partly_4k")
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


def _admitted_item_ids(connection: DBClient) -> set[int]:
    """Items whose active source has an admitted original; the reader gives every other item no traits."""
    rows = connection.execute(
        select(library_items.c.id)
        .join(source_versions, source_versions.c.version_id == library_items.c.active_source_version_id)
        .join(source_admissions, source_admissions.c.generation_id == source_versions.c.generation_id)
    )
    return {int(row.id) for row in rows}


def _series_animation(connection: DBClient) -> dict[str, bool | None]:
    animation: dict[str, bool | None] = {}
    for row in connection.execute(select(series_metadata.c.series_prefix, series_metadata.c.tmdb_genres_json)):
        try:
            genres = [str(genre) for genre in object_list(json.loads(row.tmdb_genres_json or "[]"))]
        except json.JSONDecodeError:
            genres = []
        animation[str(row.series_prefix)] = is_animated(genres)
    return animation


def season_kind_report(
        connection: DBClient,
        *,
        library_types: Mapping[str, str],
        folder_approved: Callable[[str], bool],
) -> dict[str, Any]:
    """Read-only: each season still waiting on a folder approval, grouped by its originals' traits.

    Only original traits from the source evidence reader count, proven or probable, and published
    episodes vote with the original they were made from. Output measurements never do.
    """
    items = connection.execute(select(
        library_items.c.id, library_items.c.rel_path, library_items.c.status, library_items.c.video_codec,
        library_items.c.size_bytes,
    ).where(library_items.c.missing_since.is_(None))).mappings().all()
    admitted = _admitted_item_ids(connection)
    context = load_source_evidence_context(connection)
    animation = _series_animation(connection)
    seasons: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    series_of: dict[str, str] = {}
    for item in items:
        identity = season_identity(dict(item), library_types=library_types)
        if identity is None or len(str(item["rel_path"]).split("/")) < 4:
            continue
        seasons[identity.season_prefix].append(item)
        series_of[identity.season_prefix] = identity.series_prefix
    rows = []
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    for prefix in sorted(seasons):
        episodes = seasons[prefix]
        waiting = [item for item in episodes if _waiting(item)]
        if not waiting:
            continue
        if folder_approved(prefix):
            totals["folder_approved"]["seasons"] += 1
            continue
        traits = []
        for item in episodes:
            if int(item["id"]) not in admitted:
                traits.append(None)
                continue
            evidence = load_source_evidence(connection, int(item["id"]), context=context)
            fingerprint = evidence.traits["media_fingerprint"]
            traits.append(episode_traits(fingerprint.summary, evidence.profile) if fingerprint.tier in {"proven", "probable"} else None)
        grouping = season_kind(traits, animated=animation.get(series_of[prefix]))
        waiting_bytes = sum(int_value(item["size_bytes"]) for item in waiting)
        totals[grouping.kind]["seasons"] += 1
        totals[grouping.kind]["waiting_bytes"] += waiting_bytes
        rows.append({"season": prefix, **grouping.to_payload(), "waiting_episodes": len(waiting),
                     "waiting_bytes": waiting_bytes})
    return {
        "content_kinds_version": CONTENT_KINDS_VERSION,
        "ranges": {
            "darker_min_dark_frames": DARKER_MIN_DARK_FRAMES,
            "bright_max_dark_frames": BRIGHT_MAX_DARK_FRAMES,
            "live_action_max_repeated_frames": LIVE_ACTION_MAX_REPEATED_FRAMES,
            "uhd_min_height": UHD_MIN_HEIGHT,
            "uhd_min_width": UHD_MIN_WIDTH,
            "season_agreement": SEASON_AGREEMENT,
        },
        "totals": {kind: dict(counts) for kind, counts in sorted(totals.items())},
        "unmeasured_reasons": dict(Counter(row["reason"] for row in rows if row["kind"] == "unmeasured")),
        "seasons": rows,
    }
