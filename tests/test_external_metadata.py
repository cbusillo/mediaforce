import copy
import json
from collections.abc import Mapping
import os
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.engine import Connection

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, plex_item_metadata, series_metadata
from mediaforce.library.external_metadata import PlexClient, PlexPathMapping, ProviderHttpError, TmdbClient, \
    FetchImage, FetchJson, JSONPayload, map_plex_part_path, plex_thumb_path, tmdb_id_from_guids
from mediaforce.library.metadata_sync import MetadataSyncStats, cached_poster, sync_external_metadata
from mediaforce.web.routes.posters import register_poster_routes
from mediaforce.web.settings_runtime import normalize_metadata_settings


OBSERVED_AT = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


class ExternalMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_path_mapping_uses_longest_exact_root_boundary(self) -> None:
        mappings = (
            PlexPathMapping("/plex/media", "/Volumes/media"),
            PlexPathMapping("/plex/media/tv", "/Volumes/tv"),
        )

        self.assertEqual(
            map_plex_part_path("/plex/media/tv/Show/Episode.mkv", mappings),
            "/Volumes/tv/Show/Episode.mkv",
        )
        self.assertEqual(
            map_plex_part_path("/plex/media2/tv/Show/Episode.mkv", mappings),
            "/plex/media2/tv/Show/Episode.mkv",
        )

    def test_tmdb_guid_resolution_requires_one_unambiguous_positive_id(self) -> None:
        self.assertEqual(tmdb_id_from_guids(("imdb://tt123", "tmdb://456")), 456)
        self.assertIsNone(tmdb_id_from_guids(("tmdb://456", "tmdb://789")))
        self.assertIsNone(tmdb_id_from_guids(("tmdb://0", "imdb://tt123")))

    def test_clients_parse_plex_and_tmdb_payloads_without_leaking_tokens(self) -> None:
        requests: list[tuple[str, dict[str, str]]] = []

        def fake_fetch(url: str, headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
            requests.append((url, dict(headers)))
            if "type=2" in url:
                query = parse_qs(urlsplit(url).query)
                return {
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [{
                            "ratingKey": "show-1",
                            "Guid": ([{"id": "tmdb://42"}] if query.get("includeGuids") == ["1"] else []),
                        }],
                    }
                }, {}
            if "type=1" in url:
                return {
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [{
                            "ratingKey": "movie-1",
                            "addedAt": 1_700_000_000,
                            "Media": [{"Part": [{"id": "part-1", "file": "/plex/movies/Movie.mkv"}]}],
                        }],
                    }
                }, {}
            if "type=4" in url:
                return {
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [{
                            "ratingKey": "episode-1",
                            "grandparentRatingKey": "show-1",
                            "parentIndex": 0,
                            "addedAt": 1_700_000_000,
                            "Media": [{"Part": [{"id": "part-1", "file": "/plex/tv/Show/Episode.mkv"}]}],
                        }],
                    }
                }, {}
            if url.endswith("/tv/42"):
                return {"id": 42, "status": "Returning Series", "in_production": True, "genres": [{"id": 10764, "name": "Reality"}]}, {}
            raise AssertionError(url)

        plex = PlexClient(
            base_url="http://plex.local:32400",
            token="plex-secret",
            client_identifier="mediaforce-test",
            fetch_json_fn=fake_fetch,
        )
        tmdb = TmdbClient(
            base_url="https://api.themoviedb.org/3",
            token="tmdb-secret",
            fetch_json_fn=fake_fetch,
        )

        show = list(plex.shows())[0]
        movie = list(plex.items(1))[0]
        item = list(plex.items(4))[0]
        series = tmdb.series(42)

        self.assertEqual(show.guids, ("tmdb://42",))
        self.assertEqual(movie.part_path, "/plex/movies/Movie.mkv")
        self.assertEqual(item.season_index, 0)
        self.assertEqual(series.status, "Returning Series")
        self.assertEqual(series.genres, ("Reality",))
        plex_queries = [parse_qs(urlsplit(url).query) for url, _headers in requests if url.startswith("http://plex")]
        self.assertEqual([query.get("type") for query in plex_queries], [["2"], ["1"], ["4"]])
        self.assertEqual(plex_queries[0].get("includeGuids"), ["1"])
        self.assertNotIn("includeGuids", plex_queries[1])
        self.assertNotIn("includeGuids", plex_queries[2])
        self.assertEqual(requests[0][1]["X-Plex-Token"], "plex-secret")
        self.assertEqual(requests[-1][1]["Authorization"], "Bearer tmdb-secret")
        self.assertTrue(all("secret" not in url for url, _ in requests))

    def test_plex_client_rejects_incomplete_pagination(self) -> None:
        def fake_fetch(_url: str, headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
            if dict(headers).get("X-Plex-Container-Start") == "0":
                return {
                    "MediaContainer": {
                        "totalSize": 2,
                        "Metadata": [{
                            "ratingKey": "1",
                            "grandparentRatingKey": "show-1",
                            "parentIndex": 1,
                            "Media": [{"Part": [{"file": "/plex/tv/Show/Episode.mkv"}]}],
                        }],
                    }
                }, {}
            return {"MediaContainer": {"totalSize": 2, "Metadata": []}}, {}

        client = PlexClient(
            base_url="http://plex.local:32400",
            token="plex-secret",
            client_identifier="mediaforce-test",
            fetch_json_fn=fake_fetch,
        )

        with self.assertRaisesRegex(ProviderHttpError, "ended before the reported total"):
            list(client.items(4))

    def test_plex_client_rejects_structurally_incomplete_items(self) -> None:
        def fake_fetch(_url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
            return {
                "MediaContainer": {
                    "totalSize": 1,
                    "Metadata": [{
                        "ratingKey": "episode-1",
                        "grandparentRatingKey": "show-1",
                        "parentIndex": 1,
                    }],
                }
            }, {}

        client = PlexClient(
            base_url="http://plex.local:32400",
            token="plex-secret",
            client_identifier="mediaforce-test",
            fetch_json_fn=fake_fetch,
        )

        with self.assertRaisesRegex(ProviderHttpError, "Media was missing or empty"):
            list(client.items(4))

    def test_conflicting_plex_claim_preserves_last_successful_cache(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        source_path = self.root / "source" / rel_path
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path, rel_path)
            connection.execute(
                plex_item_metadata.insert().values(
                    library_item_id=item_id,
                    plex_server_id="plex-server-1",
                    plex_item_rating_key="cached-episode",
                    plex_show_rating_key="show-1",
                    plex_season_index=1,
                    plex_added_at="2020-01-01T00:00:00+00:00",
                    plex_part_path="/plex/tv/Show/Season 1/Episode 01.mkv",
                    observed_at="2025-01-01T00:00:00+00:00",
                )
            )
            connection.execute(
                series_metadata.insert().values(
                    series_prefix="tv/Show",
                    plex_server_id="plex-server-1",
                    plex_show_rating_key="show-1",
                    plex_guids_json='["tmdb://42"]',
                    plex_observed_at="2025-01-01T00:00:00+00:00",
                    tmdb_series_id=42,
                    tmdb_status="Returning Series",
                    tmdb_in_production=1,
                    tmdb_observed_at="2025-01-01T00:00:00+00:00",
                    updated_at="2025-01-01T00:00:00+00:00",
                )
            )

            def conflicting_fetch(url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
                parsed = urlsplit(url)
                query = parse_qs(parsed.query)
                if parsed.path.endswith("/identity"):
                    return {"MediaContainer": {"machineIdentifier": "plex-server-1"}}, {}
                metadata_type = query.get("type", [""])[0]
                if metadata_type == "2":
                    return {
                        "MediaContainer": {
                            "totalSize": 1,
                            "Metadata": [{"ratingKey": "show-1", "Guid": [{"id": "tmdb://42"}]}],
                        }
                    }, {}
                if metadata_type == "4":
                    duplicate = {
                        "grandparentRatingKey": "show-1",
                        "parentIndex": 1,
                        "addedAt": 1_700_000_000,
                        "Media": [{
                            "Part": [{"file": "/plex/tv/Show/Season 1/Episode 01.mkv"}],
                        }],
                    }
                    return {
                        "MediaContainer": {
                            "totalSize": 3,
                            "Metadata": [
                                {**duplicate, "ratingKey": "duplicate-1"},
                                {**duplicate, "ratingKey": "duplicate-2"},
                                {**duplicate, "ratingKey": "duplicate-3"},
                            ],
                        }
                    }, {}
                return {"MediaContainer": {"totalSize": 0, "Metadata": []}}, {}

            with patch.dict(
                    os.environ,
                    {"MEDIAFORCE_PLEX_TOKEN": "plex-secret", "MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"},
                    clear=False,
            ):
                stats = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT,
                    fetch_json_fn=conflicting_fetch,
                )
            cached = connection.execute(
                select(plex_item_metadata).where(plex_item_metadata.c.library_item_id == item_id)
            ).mappings().one()
            series = connection.execute(
                select(series_metadata).where(series_metadata.c.series_prefix == "tv/Show")
            ).mappings().one()

        self.assertEqual(stats.status, "completed_with_warnings")
        self.assertEqual(stats.plex.conflicts, 1)
        self.assertEqual(cached["plex_item_rating_key"], "cached-episode")
        self.assertEqual(series["tmdb_status"], "Returning Series")

    def test_sync_maps_exact_paths_and_preserves_cache_on_provider_failure(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        source_path = self.root / "source" / rel_path
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path, rel_path)
            stale_item_id = self._insert_item(
                connection,
                self.root / "source" / "tv/Stale Show/Season 1/Episode 01.mkv",
                "tv/Stale Show/Season 1/Episode 01.mkv",
            )
            connection.execute(
                plex_item_metadata.insert().values(
                    library_item_id=stale_item_id,
                    plex_server_id="plex-server-1",
                    plex_item_rating_key="stale-episode",
                    plex_show_rating_key="stale-show",
                    plex_season_index=1,
                    plex_added_at="2020-01-01T00:00:00+00:00",
                    plex_part_path="/plex/tv/Stale Show/Season 1/Episode 01.mkv",
                    observed_at="2025-01-01T00:00:00+00:00",
                )
            )
            connection.execute(
                series_metadata.insert().values(
                    series_prefix="tv/Stale Show",
                    plex_server_id="plex-server-1",
                    plex_show_rating_key="stale-show",
                    plex_guids_json='["tmdb://99"]',
                    plex_observed_at="2025-01-01T00:00:00+00:00",
                    tmdb_series_id=99,
                    tmdb_status="Ended",
                    tmdb_in_production=0,
                    tmdb_observed_at="2025-01-01T00:00:00+00:00",
                    updated_at="2025-01-01T00:00:00+00:00",
                )
            )
            with patch.dict(
                    os.environ,
                    {"MEDIAFORCE_PLEX_TOKEN": "plex-secret", "MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"},
                    clear=False,
            ):
                stats = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT,
                    fetch_json_fn=self._successful_fetch,
                )
                cached_before = connection.execute(
                    select(plex_item_metadata).where(plex_item_metadata.c.library_item_id == item_id)
                ).mappings().one()
                series_before = connection.execute(
                    select(series_metadata).where(series_metadata.c.series_prefix == "tv/Show")
                ).mappings().one()
                stale_item_count = connection.execute(
                    select(plex_item_metadata.c.library_item_id)
                    .where(plex_item_metadata.c.library_item_id == stale_item_id)
                ).fetchall()
                stale_series_count = connection.execute(
                    select(series_metadata.c.series_prefix)
                    .where(series_metadata.c.series_prefix == "tv/Stale Show")
                ).fetchall()

                def failed_fetch(_url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
                    raise ProviderHttpError("offline", status_code=503)

                failed = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT + timedelta(hours=2),
                    fetch_json_fn=failed_fetch,
                )
                cached_after = connection.execute(
                    select(plex_item_metadata).where(plex_item_metadata.c.library_item_id == item_id)
                ).mappings().one()
                series_after = connection.execute(
                    select(series_metadata).where(series_metadata.c.series_prefix == "tv/Show")
                ).mappings().one()

        self.assertEqual(stats.status, "completed")
        self.assertEqual(stats.plex.matched, 1)
        self.assertEqual(stats.tmdb.refreshed, 1)
        self.assertEqual(cached_before["plex_item_rating_key"], "episode-1")
        self.assertEqual(series_before["tmdb_status"], "Returning Series")
        self.assertEqual(series_before["tmdb_genres_json"], '["Reality"]')
        self.assertEqual(stale_item_count, [])
        self.assertEqual(stale_series_count, [])
        self.assertEqual(failed.status, "completed_with_warnings")
        self.assertEqual(dict(cached_before), dict(cached_after))
        self.assertEqual(dict(series_before), dict(series_after))

    def test_plex_refresh_interval_uses_cached_inventory(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        source_path = self.root / "source" / rel_path
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, source_path, rel_path)
            with patch.dict(
                    os.environ,
                    {"MEDIAFORCE_PLEX_TOKEN": "plex-secret", "MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"},
                    clear=False,
            ):
                sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT,
                    fetch_json_fn=self._successful_fetch,
                )

                def unexpected_fetch(_url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
                    raise AssertionError("fresh provider caches should avoid network calls")

                cached = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT + timedelta(minutes=30),
                    fetch_json_fn=unexpected_fetch,
                )

        self.assertEqual(cached.status, "completed")
        self.assertEqual(cached.plex.status, "completed")
        self.assertEqual(cached.plex.cached, 1)
        self.assertIn("Using Plex metadata cached", cached.plex.message or "")

    def test_series_saved_before_genres_refresh_without_waiting_for_the_interval(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, self.root / "source" / rel_path, rel_path)
            with patch.dict(
                    os.environ,
                    {"MEDIAFORCE_PLEX_TOKEN": "plex-secret", "MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"},
                    clear=False,
            ):
                sync_external_metadata(connection, config, now=OBSERVED_AT, fetch_json_fn=self._successful_fetch)
                connection.execute(series_metadata.update().values(tmdb_genres_json=None))
                tmdb_urls: list[str] = []

                def tmdb_only_fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
                    if not urlsplit(url).path.endswith("/tv/42"):
                        raise AssertionError("fresh Plex caches should avoid network calls")
                    tmdb_urls.append(url)
                    return self._successful_fetch(url, headers, timeout)

                refreshed = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT + timedelta(minutes=30),
                    fetch_json_fn=tmdb_only_fetch,
                )
                cached = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT + timedelta(minutes=40),
                    fetch_json_fn=tmdb_only_fetch,
                )
                genres_json = connection.execute(select(series_metadata.c.tmdb_genres_json)).scalar_one()

        self.assertEqual(refreshed.tmdb.refreshed, 1)
        self.assertEqual(cached.tmdb.cached, 1)
        self.assertEqual(len(tmdb_urls), 1)
        self.assertEqual(genres_json, '["Reality"]')

    def test_missing_referenced_show_preserves_last_good_cache(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        source_path = self.root / "source" / rel_path
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path, rel_path)
            with patch.dict(
                    os.environ,
                    {"MEDIAFORCE_PLEX_TOKEN": "plex-secret", "MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"},
                    clear=False,
            ):
                sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT,
                    fetch_json_fn=self._successful_fetch,
                )
                cached_before = connection.execute(
                    select(plex_item_metadata).where(plex_item_metadata.c.library_item_id == item_id)
                ).mappings().one()
                series_before = connection.execute(
                    select(series_metadata).where(series_metadata.c.series_prefix == "tv/Show")
                ).mappings().one()

                def missing_show_fetch(url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
                    parsed = urlsplit(url)
                    query = parse_qs(parsed.query)
                    if parsed.path.endswith("/identity"):
                        return {"MediaContainer": {"machineIdentifier": "plex-server-1"}}, {}
                    metadata_type = query.get("type", [""])[0]
                    if metadata_type == "2":
                        return {"MediaContainer": {"totalSize": 0, "Metadata": []}}, {}
                    if metadata_type == "4":
                        return {
                            "MediaContainer": {
                                "totalSize": 1,
                                "Metadata": [{
                                    "ratingKey": "episode-1",
                                    "grandparentRatingKey": "show-1",
                                    "parentIndex": 1,
                                    "Media": [{"Part": [{
                                        "file": "/plex/tv/Show/Season 1/Episode 01.mkv",
                                    }]}],
                                }],
                            }
                        }, {}
                    return {"MediaContainer": {"totalSize": 0, "Metadata": []}}, {}

                failed = sync_external_metadata(
                    connection,
                    config,
                    now=OBSERVED_AT + timedelta(hours=2),
                    fetch_json_fn=missing_show_fetch,
                )
                cached_after = connection.execute(
                    select(plex_item_metadata).where(plex_item_metadata.c.library_item_id == item_id)
                ).mappings().one()
                series_after = connection.execute(
                    select(series_metadata).where(series_metadata.c.series_prefix == "tv/Show")
                ).mappings().one()

        self.assertEqual(failed.status, "completed_with_warnings")
        self.assertEqual(dict(cached_before), dict(cached_after))
        self.assertEqual(dict(series_before), dict(series_after))

    def test_metadata_settings_preserve_custom_env_names_and_refresh_intervals(self) -> None:
        payload = normalize_metadata_settings(
            {
                "plex": {
                    "base_url": "http://plex.local:32400",
                    "token_env": "MEDIAFORCE_PLEX_HOME_TOKEN",
                    "refresh_interval_hours": 2,
                    "library_roots": {"tv": "/plex/tv"},
                },
                "tmdb": {
                    "base_url": "https://api.themoviedb.org/3",
                    "token_env": "MEDIAFORCE_TMDB_HOME_TOKEN",
                    "refresh_interval_hours": 48,
                },
            },
            known_library_keys={"tv"},
        )

        self.assertEqual(payload["plex"]["token_env"], "MEDIAFORCE_PLEX_HOME_TOKEN")
        self.assertEqual(payload["plex"]["refresh_interval_hours"], 2.0)
        self.assertEqual(payload["tmdb"]["token_env"], "MEDIAFORCE_TMDB_HOME_TOKEN")
        self.assertEqual(payload["tmdb"]["refresh_interval_hours"], 48.0)

    def test_plex_poster_path_accepts_only_a_metadata_thumb(self) -> None:
        self.assertEqual(plex_thumb_path("/library/metadata/7/thumb/1700000001"), "/library/metadata/7/thumb/1700000001")
        self.assertEqual(plex_thumb_path("/library/metadata/7/thumb"), "/library/metadata/7/thumb")
        for value in (None, "", "/library/metadata/7/art/1", "/../thumb", "https://x.test/library/metadata/7/thumb", 7):
            self.assertIsNone(plex_thumb_path(value), value)

    def test_sync_caches_each_show_and_movie_poster_from_plex_once_and_serves_it(self) -> None:
        config = self._config(keep_posters=True)
        requests: list[tuple[str, Mapping[str, str]]] = []
        with open_db(config.paths.db_path) as connection:
            first = self._sync_show(connection, config, minutes=0, fetch_image_fn=self._poster_fetch(requests))
            second = self._sync_show(connection, config, minutes=30, fetch_image_fn=self._poster_fetch(requests))
            show = cached_poster(connection, config.paths.poster_dir, "tv/Show")
            movie = cached_poster(connection, config.paths.poster_dir, "movies/Heat (1995)")
            single_file_movie = cached_poster(connection, config.paths.poster_dir, "movies/Up (2009).mkv")
            extras_only_movie = cached_poster(connection, config.paths.poster_dir, "movies/Rebirth (2025)")
            missing = cached_poster(connection, config.paths.poster_dir, "tv/Unknown Show")

        self.assertEqual(sorted(url for url, _ in requests), [
            "http://plex.local:32400/library/metadata/11/thumb/1700000000",
            "http://plex.local:32400/library/metadata/7/thumb/1700000001",
            "http://plex.local:32400/library/metadata/8/thumb/1700000002",
            "http://plex.local:32400/library/metadata/9/thumb/1700000003",
        ])
        # The token goes in a header, never in the poster's URL.
        self.assertTrue(all(headers.get("X-Plex-Token") == "plex-secret" and "plex-secret" not in url for url, headers in requests))
        self.assertEqual((first.posters.refreshed, second.posters.cached), (4, 4))
        assert show is not None and movie is not None and config.paths.poster_dir is not None
        self.assertIsNotNone(single_file_movie)
        self.assertIsNotNone(extras_only_movie)
        self.assertEqual(show.read_bytes(), b"poster-bytes")
        self.assertTrue(show.is_relative_to(config.paths.poster_dir))
        self.assertNotEqual(show, movie)
        self.assertIsNone(missing)

    def test_a_poster_chosen_anew_in_plex_is_fetched_again(self) -> None:
        config = self._config(keep_posters=True)
        requests: list[tuple[str, Mapping[str, str]]] = []
        with open_db(config.paths.db_path) as connection:
            self._sync_show(connection, config, minutes=0, fetch_image_fn=self._poster_fetch(requests))
            before = cached_poster(connection, config.paths.poster_dir, "tv/Show")
            self._sync_show(
                connection, config, minutes=90, fetch_image_fn=self._poster_fetch(requests),
                fetch_json_fn=self._fetch_with_show_thumb("/library/metadata/11/thumb/1800000000"),
            )
            after = cached_poster(connection, config.paths.poster_dir, "tv/Show")

        self.assertIn("http://plex.local:32400/library/metadata/11/thumb/1800000000", [url for url, _ in requests])
        self.assertNotEqual(before, after)

    def test_a_title_without_a_plex_poster_reads_none(self) -> None:
        config = self._config(keep_posters=True)
        requests: list[tuple[str, Mapping[str, str]]] = []
        with open_db(config.paths.db_path) as connection:
            self._sync_show(
                connection, config, minutes=0, fetch_image_fn=self._poster_fetch(requests),
                fetch_json_fn=self._fetch_with_show_thumb(None),
            )
            poster = cached_poster(connection, config.paths.poster_dir, "tv/Show")

        self.assertIsNone(poster)
        self.assertNotIn("11", "".join(url for url, _ in requests))

    def test_posters_are_kept_without_tmdb(self) -> None:
        config = self._config(keep_posters=True)
        requests: list[tuple[str, Mapping[str, str]]] = []
        with open_db(config.paths.db_path) as connection:
            stats = self._sync_show(connection, config, minutes=0, fetch_image_fn=self._poster_fetch(requests), tmdb=False)
            poster = cached_poster(connection, config.paths.poster_dir, "tv/Show")

        self.assertEqual(stats.tmdb.status, "not_configured")
        self.assertEqual(stats.posters.refreshed, 4)
        self.assertIsNotNone(poster)

    def test_failed_poster_download_keeps_metadata_and_retries_next_sync(self) -> None:
        config = self._config(keep_posters=True)
        requests: list[tuple[str, Mapping[str, str]]] = []

        def failing_fetch(_url: str, _headers: Mapping[str, str], _timeout: float) -> bytes:
            raise ProviderHttpError("Provider request failed with HTTP 503", status_code=503)

        with open_db(config.paths.db_path) as connection:
            failed = self._sync_show(connection, config, minutes=0, fetch_image_fn=failing_fetch)
            status = connection.execute(select(series_metadata.c.tmdb_status)).scalar_one()
            retried = self._sync_show(connection, config, minutes=30, fetch_image_fn=self._poster_fetch(requests))

        self.assertEqual((failed.posters.status, failed.posters.failed), ("completed_with_warnings", 4))
        self.assertEqual((failed.status, retried.status), ("completed_with_warnings", "completed"))
        self.assertEqual(status, "Returning Series")
        self.assertEqual(retried.posters.refreshed, 4)
        self.assertEqual(len(requests), 4)

    def test_unwritable_poster_cache_is_a_warning_not_a_failed_sync(self) -> None:
        config = self._config(keep_posters=True)
        assert config.paths.poster_dir is not None
        config.paths.poster_dir.parent.mkdir(parents=True, exist_ok=True)
        config.paths.poster_dir.write_text("a file where the cache folder should be")
        with open_db(config.paths.db_path) as connection:
            stats = self._sync_show(connection, config, minutes=0, fetch_image_fn=self._poster_fetch([]))
            status = connection.execute(select(series_metadata.c.tmdb_status)).scalar_one()

        self.assertEqual((stats.status, stats.posters.failed), ("completed_with_warnings", 4))
        self.assertEqual(status, "Returning Series")

    @unittest.skipIf(os.geteuid() == 0, "root can search any folder")
    def test_unreadable_poster_cache_reads_as_no_poster(self) -> None:
        config = self._config(keep_posters=True)
        poster_dir = config.paths.poster_dir
        assert poster_dir is not None
        poster_dir.mkdir(parents=True)
        with open_db(config.paths.db_path) as connection:
            poster_dir.chmod(0)
            try:
                stats = self._sync_show(connection, config, minutes=0, fetch_image_fn=self._poster_fetch([]))
                poster = cached_poster(connection, poster_dir, "tv/Show")
            finally:
                poster_dir.chmod(0o755)

        self.assertEqual((stats.status, stats.posters.failed), ("completed_with_warnings", 4))
        self.assertIsNone(poster)

    def _sync_show(
            self,
            connection: Connection,
            config: MediaforceConfig,
            *,
            minutes: int,
            fetch_image_fn: FetchImage,
            fetch_json_fn: FetchJson | None = None,
            tmdb: bool = True,
    ) -> MetadataSyncStats:
        if minutes == 0:
            for rel_path in (
                "tv/Show/Season 1/Episode 01.mkv", "movies/Heat (1995)/Heat.mkv", "movies/Up (2009).mkv",
                "movies/Heat (1995)/Featurettes/Making Of.mkv", "movies/Rebirth (2025)/Rebirth 2025 Extras.mkv",
            ):
                self._insert_item(connection, self.root / "source" / rel_path, rel_path)
        secrets = {"MEDIAFORCE_PLEX_TOKEN": "plex-secret"} | ({"MEDIAFORCE_TMDB_TOKEN": "tmdb-secret"} if tmdb else {})
        with patch.dict(os.environ, secrets, clear=False):
            if not tmdb:
                os.environ.pop("MEDIAFORCE_TMDB_TOKEN", None)
            return sync_external_metadata(
                connection,
                config,
                now=OBSERVED_AT + timedelta(minutes=minutes),
                fetch_json_fn=fetch_json_fn or self._successful_fetch,
                fetch_image_fn=fetch_image_fn,
            )

    def test_poster_route_serves_the_file_or_says_none(self) -> None:
        poster = self.root / "poster.jpg"
        poster.write_bytes(b"poster-bytes")
        app = FastAPI()
        register_poster_routes(app, title_poster=lambda title_prefix: poster if title_prefix == "tv/Show" else None)
        endpoint = next(
            route.endpoint for route in app.router.routes
            if isinstance(route, APIRoute) and route.path == "/api/app/posters/{title_prefix:path}"
        )

        served = endpoint("tv/Show")
        none = endpoint("tv/Other")

        self.assertEqual(Path(served.path), poster)
        self.assertEqual((none.status_code, json.loads(none.body)), (404, {"poster": "none"}))

    def _config(self, *, keep_posters: bool = False) -> MediaforceConfig:
        with (Path(__file__).parent / "fixtures/defaults.toml").open("rb") as handle:
            raw = copy.deepcopy(tomllib.load(handle))
        raw["media"]["source_roots"] = {"tv": str(self.root / "source" / "tv"), "movies": str(self.root / "source" / "movies")}
        raw["media"]["staging_root"] = str(self.root / "staging")
        raw["media"]["archive_root"] = str(self.root / "archive")
        raw["metadata"]["plex"]["base_url"] = "http://plex.local:32400"
        raw["metadata"]["plex"]["library_roots"] = {"tv": "/plex/tv", "movies": "/plex/movies"}
        paths = ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
            poster_dir=self.root / "posters" if keep_posters else None,
        )
        return MediaforceConfig(raw=raw, paths=paths)

    @staticmethod
    def _insert_item(connection: Connection, source_path: Path, rel_path: str) -> int:
        timestamp = "2025-01-01T00:00:00+00:00"
        result = connection.execute(
            library_items.insert().values(
                source_path=str(source_path),
                rel_path=rel_path,
                media_root="tv",
                parent_dir=str(Path(rel_path).parent),
                file_name=Path(rel_path).name,
                container="mkv",
                size_bytes=1,
                mtime_ns=1,
                fingerprint="fingerprint",
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                content_version_changed_at=timestamp,
                status="discovered",
                last_scan_id="scan-1",
                discovered_at=timestamp,
                last_seen_at=timestamp,
                updated_at=timestamp,
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _successful_fetch(url: str, headers: Mapping[str, str], _timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/identity"):
            return {"MediaContainer": {"machineIdentifier": "plex-server-1"}}, {}
        if parsed.path.endswith("/library/all"):
            metadata_type = query.get("type", [""])[0]
            if metadata_type == "2":
                return {
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [{
                            "ratingKey": "show-1",
                            "Guid": [{"id": "tmdb://42"}, {"id": "imdb://tt42"}],
                            "thumb": "/library/metadata/11/thumb/1700000000",
                        }],
                    }
                }, {}
            if metadata_type == "4":
                return {
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [{
                            "ratingKey": "episode-1",
                            "grandparentRatingKey": "show-1",
                            "parentIndex": 1,
                            "addedAt": 1_700_000_000,
                            "Media": [{
                                "Part": [{
                                    "id": "part-1",
                                    "file": "/plex/tv/Show/Season 1/Episode 01.mkv",
                                }],
                            }],
                        }],
                    }
                }, {}
            if metadata_type == "1":
                return {
                    "MediaContainer": {
                        "totalSize": 4,
                        "Metadata": [{
                            "ratingKey": "7",
                            "thumb": "/library/metadata/7/thumb/1700000001",
                            "Media": [{"Part": [{"id": "part-7", "file": "/plex/movies/Heat (1995)/Heat.mkv"}]}],
                        }, {
                            "ratingKey": "8",
                            "thumb": "/library/metadata/8/thumb/1700000002",
                            "Media": [{"Part": [{"id": "part-8", "file": "/plex/movies/Up (2009).mkv"}]}],
                        }, {
                            # An extra Plex matched on its own, beside Heat's feature: Heat keeps its feature's poster.
                            "ratingKey": "10",
                            "thumb": "/library/metadata/10/thumb/1700000004",
                            "Media": [{"Part": [{"id": "part-10", "file": "/plex/movies/Heat (1995)/Featurettes/Making Of.mkv"}]}],
                        }, {
                            # A title whose library copy is only an extra takes the extra's poster.
                            "ratingKey": "9",
                            "thumb": "/library/metadata/9/thumb/1700000003",
                            "Media": [{"Part": [{"id": "part-9", "file": "/plex/movies/Rebirth (2025)/Rebirth 2025 Extras.mkv"}]}],
                        }],
                    }
                }, {}
            return {"MediaContainer": {"totalSize": 0, "Metadata": []}}, {}
        if parsed.path.endswith("/tv/42"):
            self_authorization = dict(headers).get("Authorization")
            if self_authorization != "Bearer tmdb-secret":
                raise AssertionError("TMDB bearer token was not sent")
            return {
                "id": 42, "status": "Returning Series", "in_production": True,
                "genres": [{"id": 10764, "name": "Reality"}],
            }, {}
        raise AssertionError(url)

    @staticmethod
    def _poster_fetch(requested: list[tuple[str, Mapping[str, str]]]) -> FetchImage:
        def fetch(url: str, headers: Mapping[str, str], _timeout: float) -> bytes:
            requested.append((url, dict(headers)))
            return b"poster-bytes"

        return fetch

    def _fetch_with_show_thumb(self, thumb: str | None) -> FetchJson:
        """The fake Plex and TMDB, with the show's poster path replaced, or removed when None."""
        def fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[JSONPayload, Mapping[str, str]]:
            payload, response_headers = self._successful_fetch(url, headers, timeout)
            if urlsplit(url).path.endswith("/library/all") and parse_qs(urlsplit(url).query).get("type") == ["2"]:
                show = {key: value for key, value in payload["MediaContainer"]["Metadata"][0].items() if key != "thumb"}
                if thumb is not None:
                    show["thumb"] = thumb
                payload = {"MediaContainer": {**payload["MediaContainer"], "Metadata": [show]}}
            return payload, response_headers

        return fetch


if __name__ == "__main__":
    unittest.main()
