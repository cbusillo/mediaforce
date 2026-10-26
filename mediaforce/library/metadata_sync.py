import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, metadata_sync_state, plex_item_metadata, series_metadata, \
    title_posters
from mediaforce.library.external_metadata import FetchImage, FetchJson, PlexClient, PlexPathMapping, PlexShow, \
    ProviderHttpError, TmdbClient, fetch_image, fetch_json, map_plex_part_path, plex_thumb_path, tmdb_id_from_guids
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.movie_workflow import classify_movie_path


@dataclass(slots=True)
class ProviderSyncStats:
    status: str = "not_configured"
    matched: int = 0
    unmatched: int = 0
    conflicts: int = 0
    refreshed: int = 0
    cached: int = 0
    failed: int = 0
    message: str | None = None


@dataclass(slots=True)
class MetadataSyncStats:
    status: str = "not_configured"
    plex: ProviderSyncStats = field(default_factory=ProviderSyncStats)
    tmdb: ProviderSyncStats = field(default_factory=ProviderSyncStats)
    posters: ProviderSyncStats = field(default_factory=ProviderSyncStats)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "providers": {
                "plex": asdict(self.plex),
                "tmdb": asdict(self.tmdb),
                "posters": asdict(self.posters),
            },
        }


def sync_external_metadata(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        now: datetime | None = None,
        fetch_json_fn: FetchJson = fetch_json,
        fetch_image_fn: FetchImage = fetch_image,
) -> MetadataSyncStats:
    observed_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
    settings = config.metadata
    plex_settings = _mapping(settings.get("plex"))
    tmdb_settings = _mapping(settings.get("tmdb"))
    stats = MetadataSyncStats()

    plex_base_url = str(plex_settings.get("base_url") or "").strip()
    plex_token = _environment_secret(plex_settings, "MEDIAFORCE_PLEX_TOKEN")
    if not bool(plex_settings.get("enabled", True)) or not plex_base_url or not plex_token:
        stats.plex.message = "Set the Plex server URL and token environment variable to enable metadata sync."
        stats.tmdb.message = "Plex metadata is required before TMDB series status can be resolved."
        return stats

    client_identifier = uuid.uuid5(uuid.NAMESPACE_URL, str(config.paths.db_path)).hex
    plex_client = PlexClient(
        base_url=plex_base_url,
        token=plex_token,
        client_identifier=f"mediaforce-{client_identifier}",
        fetch_json_fn=fetch_json_fn,
        fetch_image_fn=fetch_image_fn,
    )
    plex_refresh_interval = timedelta(hours=_positive_float(plex_settings.get("refresh_interval_hours"), 1.0))
    previous_plex_refresh = _provider_last_success(connection, "plex")
    if previous_plex_refresh is not None and observed_at - previous_plex_refresh < plex_refresh_interval:
        series_prefixes = _cached_plex_series_prefixes(connection)
        stats.plex.status = "completed"
        stats.plex.cached = _cached_plex_item_count(connection)
        stats.plex.message = f"Using Plex metadata cached at {previous_plex_refresh.isoformat(timespec='seconds')}."
    else:
        try:
            series_prefixes = _sync_plex(
                connection,
                config,
                plex_client,
                observed_at=observed_at,
                stats=stats.plex,
            )
        except ProviderHttpError as exc:
            stats.plex.status = "failed"
            stats.plex.failed += 1
            stats.plex.message = _provider_error_message("Plex", exc)
            stats.tmdb.message = "TMDB was not refreshed because Plex identity mapping failed."
            stats.status = "completed_with_warnings"
            return stats
        _record_provider_success(connection, "plex", observed_at)
        connection.commit()
    # Posters come from Plex alone, so they are kept even when TMDB isn't set up.
    _cache_posters(connection, config.paths.poster_dir, plex_client, stats=stats.posters)

    tmdb_token = _environment_secret(tmdb_settings, "MEDIAFORCE_TMDB_TOKEN")
    if not bool(tmdb_settings.get("enabled", True)) or not tmdb_token:
        stats.tmdb.status = "not_configured"
        stats.tmdb.message = "Set the TMDB token environment variable to resolve current-series status."
        stats.status = "completed_with_warnings"
        return stats

    tmdb_client = TmdbClient(
        base_url=str(tmdb_settings.get("base_url") or "https://api.themoviedb.org/3"),
        token=tmdb_token,
        fetch_json_fn=fetch_json_fn,
    )
    _sync_tmdb(
        connection,
        tmdb_client,
        series_prefixes=series_prefixes,
        observed_at=observed_at,
        refresh_interval=timedelta(hours=_positive_float(tmdb_settings.get("refresh_interval_hours"), 24.0)),
        stats=stats.tmdb,
    )
    connection.commit()
    stats.status = (
        "completed"
        if stats.plex.status == "completed" and stats.tmdb.status == "completed" and not stats.posters.failed
        else "completed_with_warnings"
    )
    return stats


def metadata_configuration_status(config: MediaforceConfig) -> dict[str, Any]:
    settings = config.metadata
    plex = _mapping(settings.get("plex"))
    tmdb = _mapping(settings.get("tmdb"))
    return {
        "plex": {
            "enabled": bool(plex.get("enabled", True)),
            "base_url": str(plex.get("base_url") or ""),
            "library_roots": dict(_mapping(plex.get("library_roots"))),
            "token_env": str(plex.get("token_env") or "MEDIAFORCE_PLEX_TOKEN"),
            "token_configured": bool(_environment_secret(plex, "MEDIAFORCE_PLEX_TOKEN")),
            "refresh_interval_hours": _positive_float(plex.get("refresh_interval_hours"), 1.0),
        },
        "tmdb": {
            "enabled": bool(tmdb.get("enabled", True)),
            "base_url": str(tmdb.get("base_url") or "https://api.themoviedb.org/3"),
            "token_env": str(tmdb.get("token_env") or "MEDIAFORCE_TMDB_TOKEN"),
            "token_configured": bool(_environment_secret(tmdb, "MEDIAFORCE_TMDB_TOKEN")),
            "refresh_interval_hours": _positive_float(tmdb.get("refresh_interval_hours"), 24.0),
        },
    }


def _sync_plex(
        connection: DBClient,
        config: MediaforceConfig,
        client: PlexClient,
        *,
        observed_at: datetime,
        stats: ProviderSyncStats,
) -> set[str]:
    server_id = client.server_id()
    shows = {show.rating_key: show for show in client.shows()}
    rows = connection.execute(
        select(library_items.c.id, library_items.c.source_path, library_items.c.rel_path)
        .where(LIBRARY_ITEM_PRESENT)
    ).mappings().fetchall()
    rows_by_path = {_normalized_local_path(str(row["source_path"])): row for row in rows}
    rows_by_id = {int(row["id"]): row for row in rows}
    mappings = _plex_path_mappings(config)
    claims: dict[int, tuple[Any, Any]] = {}
    conflicted_item_ids: set[int] = set()

    for item in chain(client.items(1), client.items(4)):
        mapped_path = map_plex_part_path(item.part_path, mappings)
        row = rows_by_path.get(_normalized_local_path(mapped_path)) if mapped_path else None
        if row is None:
            stats.unmatched += 1
            continue
        item_id = int(row["id"])
        if item_id in conflicted_item_ids:
            continue
        if item_id in claims:
            stats.conflicts += 1
            claims.pop(item_id, None)
            conflicted_item_ids.add(item_id)
            continue
        claims[item_id] = (row, item)

    observed_at_text = observed_at.isoformat(timespec="seconds")
    series_claims: dict[str, set[str]] = {}
    for row, item in claims.values():
        series_prefix = _series_prefix(str(row["rel_path"] or ""), library_types=config.library_type_map)
        if series_prefix and item.show_rating_key:
            series_claims.setdefault(series_prefix, set()).add(item.show_rating_key)
    referenced_show_keys = {rating_key for rating_keys in series_claims.values() for rating_key in rating_keys}
    missing_show_keys = referenced_show_keys - set(shows)
    if missing_show_keys:
        raise ProviderHttpError("Plex item inventory referenced a show missing from the show inventory")

    for item_id, (row, item) in claims.items():
        statement = sqlite_insert(plex_item_metadata).values(
            library_item_id=item_id,
            plex_server_id=server_id,
            plex_item_rating_key=item.rating_key,
            plex_part_id=item.part_id,
            plex_show_rating_key=item.show_rating_key,
            plex_season_index=item.season_index,
            plex_added_at=item.added_at,
            plex_part_path=item.part_path,
            observed_at=observed_at_text,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[plex_item_metadata.c.library_item_id],
                set_={
                    "plex_server_id": statement.excluded.plex_server_id,
                    "plex_item_rating_key": statement.excluded.plex_item_rating_key,
                    "plex_part_id": statement.excluded.plex_part_id,
                    "plex_show_rating_key": statement.excluded.plex_show_rating_key,
                    "plex_season_index": statement.excluded.plex_season_index,
                    "plex_added_at": statement.excluded.plex_added_at,
                    "plex_part_path": statement.excluded.plex_part_path,
                    "observed_at": statement.excluded.observed_at,
                },
            )
        )
        stats.matched += 1

    local_item_ids = {int(row["id"]) for row in rows}
    stale_item_ids = local_item_ids - set(claims) - conflicted_item_ids
    for item_id_batch in _batches(sorted(stale_item_ids)):
        connection.execute(
            plex_item_metadata.delete().where(plex_item_metadata.c.library_item_id.in_(item_id_batch))
        )

    refreshed_series: set[str] = set()
    conflicted_series_prefixes = {
        prefix
        for item_id in conflicted_item_ids
        for prefix in [
            _series_prefix(
                str(rows_by_id[item_id]["rel_path"] or ""),
                library_types=config.library_type_map,
            )
        ]
        if prefix is not None
    }
    for series_prefix, rating_keys in series_claims.items():
        if len(rating_keys) != 1:
            stats.conflicts += 1
            conflicted_series_prefixes.add(series_prefix)
            continue
        rating_key = next(iter(rating_keys))
        show = shows.get(rating_key)
        guids = show.guids if show is not None else ()
        tmdb_id = tmdb_id_from_guids(guids)
        statement = sqlite_insert(series_metadata).values(
            series_prefix=series_prefix,
            plex_server_id=server_id,
            plex_show_rating_key=rating_key,
            plex_guids_json=json.dumps(list(guids), separators=(",", ":")),
            plex_observed_at=observed_at_text,
            tmdb_series_id=tmdb_id,
            tmdb_status=None,
            tmdb_in_production=None,
            tmdb_genres_json=None,
            tmdb_observed_at=None,
            updated_at=observed_at_text,
        )
        update_values: dict[str, Any] = {
            "plex_server_id": statement.excluded.plex_server_id,
            "plex_show_rating_key": statement.excluded.plex_show_rating_key,
            "plex_guids_json": statement.excluded.plex_guids_json,
            "plex_observed_at": statement.excluded.plex_observed_at,
            "tmdb_series_id": statement.excluded.tmdb_series_id,
            "updated_at": statement.excluded.updated_at,
        }
        existing = connection.execute(
            select(series_metadata.c.tmdb_series_id).where(series_metadata.c.series_prefix == series_prefix)
        ).scalar_one_or_none()
        if existing != tmdb_id:
            update_values.update({
                "tmdb_status": None,
                "tmdb_in_production": None,
                "tmdb_genres_json": None,
                "tmdb_observed_at": None,
            })
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[series_metadata.c.series_prefix],
                set_=update_values,
            )
        )
        refreshed_series.add(series_prefix)

    local_series_prefixes = {
        prefix
        for row in rows
        for prefix in [_series_prefix(str(row["rel_path"] or ""), library_types=config.library_type_map)]
        if prefix is not None
    }
    stale_series_prefixes = local_series_prefixes - refreshed_series - conflicted_series_prefixes
    for series_prefix_batch in _batches(sorted(stale_series_prefixes)):
        connection.execute(
            series_metadata.delete().where(series_metadata.c.series_prefix.in_(series_prefix_batch))
        )

    _record_posters(
        connection, config, shows, series_claims, claims.values(),
        library_rel_paths=[str(row["rel_path"] or "") for row in rows], observed_at_text=observed_at_text,
    )
    stats.status = "completed" if not stats.conflicts else "completed_with_warnings"
    return refreshed_series


def _record_posters(
        connection: DBClient,
        config: MediaforceConfig,
        shows: Mapping[str, PlexShow],
        series_claims: Mapping[str, set[str]],
        claims: Any,
        *,
        library_rel_paths: list[str],
        observed_at_text: str,
) -> None:
    """Each show's and movie's Plex poster path, replacing what the last full Plex sync saw.

    A show takes its own poster; a movie takes its feature's. A movie whose library copy has no feature at all, only
    extras, takes their poster instead; a feature Plex didn't match leaves its title without one rather than an
    extra's. A title that Plex matched to more than one show or movie gets none, as its poster would be a guess.
    """
    thumbs: dict[str, set[str]] = {}
    for series_prefix, rating_keys in series_claims.items():
        show = shows.get(next(iter(rating_keys))) if len(rating_keys) == 1 else None
        if show is not None and show.thumb:
            thumbs.setdefault(series_prefix, set()).add(show.thumb)
    feature_thumbs: dict[str, set[str]] = {}
    extra_thumbs: dict[str, set[str]] = {}
    for row, item in claims:
        movie = None if item.show_rating_key else _movie_poster_key(
            str(row["rel_path"] or ""), library_types=config.library_type_map,
        )
        if movie is not None and item.thumb:
            prefix, is_feature = movie
            (feature_thumbs if is_feature else extra_thumbs).setdefault(prefix, set()).add(item.thumb)
    titles_with_a_feature: set[str] = set()
    for rel_path in library_rel_paths:
        movie = _movie_poster_key(rel_path, library_types=config.library_type_map)
        if movie is not None and movie[1]:
            titles_with_a_feature.add(movie[0])
    for prefix, found in feature_thumbs.items():
        thumbs[prefix] = found
    for prefix, found in extra_thumbs.items():
        if prefix not in titles_with_a_feature:
            thumbs[prefix] = found
    connection.execute(title_posters.delete())
    rows = [
        {"title_prefix": prefix, "plex_thumb": next(iter(found)), "observed_at": observed_at_text}
        for prefix, found in sorted(thumbs.items())
        if len(found) == 1
    ]
    if rows:
        connection.execute(title_posters.insert(), rows)


def _sync_tmdb(
        connection: DBClient,
        client: TmdbClient,
        *,
        series_prefixes: set[str],
        observed_at: datetime,
        refresh_interval: timedelta,
        stats: ProviderSyncStats,
) -> None:
    rows = connection.execute(
        select(
            series_metadata.c.series_prefix,
            series_metadata.c.tmdb_series_id,
            series_metadata.c.tmdb_genres_json,
            series_metadata.c.tmdb_observed_at,
        ).where(series_metadata.c.series_prefix.in_(series_prefixes))
    ).mappings().fetchall()
    observed_at_text = observed_at.isoformat(timespec="seconds")
    refreshed_rows: list[tuple[str, Any]] = []
    for row in rows:
        series_id = _positive_int(row["tmdb_series_id"])
        if series_id is None:
            continue
        previous = _parse_timestamp(row["tmdb_observed_at"])
        # Rows saved before genres were kept refresh once, so they don't wait a full interval.
        if (
                previous is not None
                and observed_at - previous < refresh_interval
                and row["tmdb_genres_json"] is not None
        ):
            stats.cached += 1
            continue
        try:
            series = client.series(series_id)
        except ProviderHttpError as exc:
            stats.failed += 1
            stats.status = "completed_with_warnings"
            stats.message = _provider_error_message("TMDB", exc)
            if exc.status_code in {401, 403, 429} or (exc.status_code is not None and exc.status_code >= 500):
                break
            continue
        refreshed_rows.append((str(row["series_prefix"]), series))
        stats.refreshed += 1
    for series_prefix, series in refreshed_rows:
        connection.execute(
            series_metadata.update()
            .where(series_metadata.c.series_prefix == series_prefix)
            .values(
                tmdb_status=series.status,
                tmdb_in_production=(1 if series.in_production else 0) if series.in_production is not None else None,
                tmdb_genres_json=json.dumps(list(series.genres), separators=(",", ":")),
                tmdb_observed_at=observed_at_text,
                updated_at=observed_at_text,
            )
        )
    if stats.status == "not_configured":
        stats.status = "completed"


def cached_poster(connection: DBClient, poster_dir: Path | None, title_prefix: str) -> Path | None:
    """The cached Plex poster for a show ("tv/Show") or movie ("movies/Title (Year)"), when there is one."""
    thumb = connection.execute(
        select(title_posters.c.plex_thumb).where(title_posters.c.title_prefix == title_prefix.strip("/"))
    ).scalar_one_or_none()
    target = poster_file(poster_dir, thumb)
    try:
        return target if target is not None and target.is_file() else None
    except OSError:
        return None


def poster_file(poster_dir: Path | None, thumb: str | None) -> Path | None:
    """Where a Plex poster is cached: named for its thumb path, so choosing another poster in Plex fetches it anew."""
    path = plex_thumb_path(thumb)
    if poster_dir is None or path is None:
        return None
    return poster_dir / "plex" / f"{hashlib.sha256(path.encode()).hexdigest()[:32]}.jpg"


def _cache_posters(
        connection: DBClient,
        poster_dir: Path | None,
        client: PlexClient,
        *,
        stats: ProviderSyncStats,
) -> None:
    if poster_dir is None:
        stats.message = "Set state.poster_dir to keep posters."
        return
    thumbs = connection.execute(select(title_posters.c.plex_thumb).distinct()).scalars()
    for thumb in thumbs:
        target = poster_file(poster_dir, thumb)
        if target is None:
            continue
        try:
            already_cached = target.is_file()
        except OSError:
            stats.failed += 1
            stats.message = "The poster cache could not be read; posters are retried on the next sync."
            continue
        if already_cached:
            stats.cached += 1
            continue
        try:
            image = client.poster(thumb)
        except ProviderHttpError as exc:
            stats.failed += 1
            stats.message = _provider_error_message("Plex posters", exc)
            continue
        try:
            _write_atomically(target, image)
        except OSError:
            stats.failed += 1
            stats.message = "The poster cache could not be written; posters are retried on the next sync."
            continue
        stats.refreshed += 1
    stats.status = "completed" if not stats.failed else "completed_with_warnings"


def _write_atomically(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    try:
        partial.write_bytes(content)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _plex_path_mappings(config: MediaforceConfig) -> tuple[PlexPathMapping, ...]:
    plex_settings = _mapping(config.metadata.get("plex"))
    library_roots = _mapping(plex_settings.get("library_roots"))
    mappings: list[PlexPathMapping] = []
    scan_source_roots = getattr(config, "scan_source_root_map", config.source_root_map)
    library_definitions = getattr(config, "library_definition_map", {})
    for key, local_root in scan_source_roots.items():
        definition = library_definitions.get(key, {}) if isinstance(library_definitions, dict) else {}
        configured_root = str(definition.get("plex_path") or library_roots.get(key) or "").strip()
        mappings.append(
            PlexPathMapping(
                plex_root=configured_root or str(local_root),
                mediaforce_root=str(local_root),
            )
        )
    return tuple(mappings)


def _environment_secret(settings: dict[str, Any], default_name: str) -> str:
    name = str(settings.get("token_env") or default_name).strip()
    return str(os.environ.get(name) or "").strip()


def _provider_last_success(connection: DBClient, provider: str) -> datetime | None:
    value = connection.execute(
        select(metadata_sync_state.c.last_success_at)
        .where(metadata_sync_state.c.provider == provider)
    ).scalar_one_or_none()
    return _parse_timestamp(value)


def _record_provider_success(connection: DBClient, provider: str, observed_at: datetime) -> None:
    observed_at_text = observed_at.isoformat(timespec="seconds")
    statement = sqlite_insert(metadata_sync_state).values(
        provider=provider,
        last_success_at=observed_at_text,
        updated_at=observed_at_text,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[metadata_sync_state.c.provider],
            set_={
                "last_success_at": statement.excluded.last_success_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


def _cached_plex_series_prefixes(connection: DBClient) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            select(series_metadata.c.series_prefix)
            .where(series_metadata.c.plex_observed_at.is_not(None))
        ).scalars()
    }


def _cached_plex_item_count(connection: DBClient) -> int:
    return len(connection.execute(select(plex_item_metadata.c.library_item_id)).fetchall())


def _provider_error_message(provider: str, error: ProviderHttpError) -> str:
    if error.status_code in {401, 403, 498}:
        return f"{provider} authentication failed; cached metadata was preserved."
    if error.status_code == 429:
        return f"{provider} rate limited metadata refresh; cached metadata was preserved."
    return f"{provider} metadata refresh failed; cached metadata was preserved."


def _series_prefix(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> str | None:
    parts = Path(rel_path).parts
    root = parts[0] if parts else ""
    configured_type = str((library_types or {}).get(root) or "").strip().lower()
    if len(parts) < 3 or (configured_type or root.lower()) != "tv":
        return None
    return "/".join(parts[:2])


def _movie_poster_key(
        rel_path: str,
        *,
        library_types: Mapping[str, str] | None = None,
) -> tuple[str, bool] | None:
    """A movie file's title prefix as the Mac app's movie list names it, its title folder ("movies/Title (Year)") or
    the file itself for a movie stored straight under the library root, and whether the file is the feature."""
    parts = Path(rel_path).parts
    root = parts[0] if parts else ""
    configured_type = str((library_types or {}).get(root) or "").strip().lower()
    if (configured_type or root.lower()) not in {"movie", "movies"}:
        return None
    membership = classify_movie_path(rel_path, root=root)
    return (membership.title_prefix, membership.role == "feature") if membership is not None else None


def _normalized_local_path(value: str | None) -> str:
    if not value:
        return ""
    return os.path.normpath(str(Path(value).expanduser()))


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _batches(values: list[Any], size: int = 500) -> list[list[Any]]:
    return [values[index:index + size] for index in range(0, len(values), size)]
