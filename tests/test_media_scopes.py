import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, select

from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.library.media_scopes import MediaScopeConflict, media_group_scope_for_rel_path, \
    media_scope_from_prefix, path_matches_scope, resolve_media_scope, resolve_media_scopes, \
    scope_prefix_overlap_filter, scope_rel_path_filter, scopes_overlap, tv_series_scope_for_rel_path
from mediaforce.web import app as web_app


class MediaScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "library.sqlite3"

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_classifies_root_files_nested_folders_and_tv_scopes(self) -> None:
        movie_file = media_group_scope_for_rel_path("movies/Foo.mkv")
        other_file = media_group_scope_for_rel_path("other/Concert.mkv")
        movie_title = media_group_scope_for_rel_path("movies/Foo/Foo.mkv")
        other_folder = media_group_scope_for_rel_path("other/Concert/Night 1.mkv")
        tv_season = media_group_scope_for_rel_path("tv/Show/Season 2/Episode 1.mkv")
        tv_series = tv_series_scope_for_rel_path("tv/Show/Season 2/Episode 1.mkv")

        self.assertIsNotNone(movie_file)
        self.assertEqual((movie_file.kind, movie_file.match, movie_file.prefix), ("movie_file", "exact_item", "movies/Foo.mkv"))
        self.assertEqual(movie_file.to_payload()["parent"], {"prefix": "movies", "title": "Movies"})
        self.assertIsNotNone(other_file)
        self.assertEqual((other_file.kind, other_file.match), ("media_file", "exact_item"))
        self.assertIsNotNone(movie_title)
        self.assertEqual((movie_title.kind, movie_title.prefix), ("movie_title", "movies/Foo"))
        self.assertIsNotNone(other_folder)
        self.assertEqual((other_folder.kind, other_folder.prefix), ("media_folder", "other/Concert"))
        self.assertIsNotNone(tv_season)
        self.assertEqual((tv_season.kind, tv_season.prefix), ("tv_season", "tv/Show/Season 2"))
        self.assertIsNotNone(tv_series)
        self.assertEqual((tv_series.kind, tv_series.prefix), ("tv_series", "tv/Show"))

    def test_web_grouping_preserves_tv_and_exposes_exact_root_files(self) -> None:
        self.assertEqual(
            web_app._folder_group("tv/Show/Season 2/Episode 1.mkv"),
            ("tv/Show/Season 2", "Show · Season 2", "Show", "Season"),
        )
        self.assertEqual(
            web_app._tv_series_folder_group("tv/Show/Season 2/Episode 1.mkv"),
            ("tv/Show", "Show", "TV series", "Series"),
        )
        self.assertEqual(
            web_app._folder_group("movies/Foo.mkv"),
            ("movies/Foo.mkv", "Foo", "Movies", "Title"),
        )
        self.assertEqual(
            web_app._folder_group("other/Concert.mkv"),
            ("other/Concert.mkv", "Concert", "Other", "File"),
        )
        self.assertEqual(
            web_app._folder_group("tv/Show/Episode 1.mkv"),
            ("tv/Show/Episode 1.mkv", "Episode 1", "TV", "File"),
        )
        self.assertEqual(
            web_app._folder_group("tv/Show/Season 2/Extras/Clip.mkv"),
            ("tv/Show/Season 2", "Show · Season 2", "Show", "Season"),
        )

    def test_nested_tv_folder_scope_preserves_requested_boundary(self) -> None:
        with open_db(self.db_path) as connection:
            child_id = self._insert_item(connection, "tv/Show/Season 2/Extras/Clip.mkv")
            season_id = self._insert_item(connection, "tv/Show/Season 2/Episode 1.mkv")

            scope = resolve_media_scope(connection, "tv/Show/Season 2/Extras")
            rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, scope))
            ).scalars().all()

        self.assertEqual((scope.kind, scope.prefix), ("media_folder", "tv/Show/Season 2/Extras"))
        self.assertEqual(rows, [child_id])
        self.assertNotIn(season_id, rows)

    def test_exact_and_descendant_filters_exclude_similarly_named_siblings(self) -> None:
        with open_db(self.db_path) as connection:
            exact_id = self._insert_item(connection, "movies/Foo.mkv")
            sibling_id = self._insert_item(connection, "movies/Foo.mkv.backup.mkv")
            folder_id = self._insert_item(connection, "movies/Foo/Foo.mkv")
            folder_sibling_id = self._insert_item(connection, "movies/Foo 2/Foo.mkv")

            exact_scope = resolve_media_scope(connection, "movies/Foo.mkv")
            folder_scope = resolve_media_scope(connection, "movies/Foo")
            exact_rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, exact_scope))
            ).scalars().all()
            folder_rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, folder_scope))
            ).scalars().all()

        self.assertEqual(exact_scope.match, "exact_item")
        self.assertEqual(exact_rows, [exact_id])
        self.assertNotIn(sibling_id, exact_rows)
        self.assertEqual(folder_scope.match, "descendants")
        self.assertEqual(folder_rows, [folder_id])
        self.assertNotIn(folder_sibling_id, folder_rows)

    def test_missing_exact_item_keeps_file_scope_identity(self) -> None:
        with open_db(self.db_path) as connection:
            missing_id = self._insert_item(connection, "other/Offline.mkv", status="missing")

            scope = resolve_media_scope(connection, "other/Offline.mkv")
            rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, scope))
            ).scalars().all()

        self.assertEqual((scope.kind, scope.match), ("media_file", "exact_item"))
        self.assertEqual(rows, [missing_id])

    def test_active_folder_topology_supersedes_missing_exact_tombstone(self) -> None:
        with open_db(self.db_path) as connection:
            missing_id = self._insert_item(connection, "other/Foo", status="missing")
            child_id = self._insert_item(connection, "other/Foo/Bar.mkv")

            scope = resolve_media_scope(connection, "other/Foo")
            rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, scope))
            ).scalars().all()

        self.assertEqual((scope.kind, scope.match), ("media_folder", "descendants"))
        self.assertEqual(rows, [child_id])
        self.assertNotIn(missing_id, rows)

    def test_missing_descendant_does_not_override_active_exact_item(self) -> None:
        with open_db(self.db_path) as connection:
            exact_id = self._insert_item(connection, "other/Foo")
            self._insert_item(connection, "other/Foo/Bar.mkv", status="missing")

            scope = resolve_media_scope(connection, "other/Foo")
            rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, scope))
            ).scalars().all()

        self.assertEqual(scope.match, "exact_item")
        self.assertEqual(rows, [exact_id])

    def test_multiple_active_exact_items_fail_closed(self) -> None:
        with open_db(self.db_path) as connection:
            self._insert_item(connection, "other/Foo.mkv", source_path="/media-a/other/Foo.mkv")
            self._insert_item(connection, "other/Foo.mkv", source_path="/media-b/other/Foo.mkv")

            with self.assertRaisesRegex(MediaScopeConflict, "multiple active items"):
                resolve_media_scope(connection, "other/Foo.mkv")

    def test_descendant_matching_is_case_sensitive(self) -> None:
        with open_db(self.db_path) as connection:
            upper_id = self._insert_item(connection, "movies/Foo/A.mkv")
            lower_id = self._insert_item(connection, "movies/foo/B.mkv")

            scope = resolve_media_scope(connection, "movies/Foo")
            rows = connection.execute(
                select(library_items.c.id).where(scope_rel_path_filter(library_items.c.rel_path, scope))
            ).scalars().all()
            overlap_rows = connection.execute(
                select(library_items.c.id).where(
                    scope_prefix_overlap_filter(
                        library_items.c.rel_path,
                        media_scope_from_prefix("movies/Foo", match="descendants"),
                    )
                )
            ).scalars().all()

        self.assertEqual(rows, [upper_id])
        self.assertNotIn(lower_id, rows)
        self.assertEqual(overlap_rows, [upper_id])

    def test_bulk_exact_resolution_uses_bounded_query_count(self) -> None:
        with open_db(self.db_path) as connection:
            prefixes = [f"movies/Movie {index}.mkv" for index in range(401)]
            for prefix in prefixes:
                self._insert_item(connection, prefix)
            query_count = 0

            def count_query(*_args: object) -> None:
                nonlocal query_count
                query_count += 1

            event.listen(connection.engine, "before_cursor_execute", count_query)
            try:
                bulk_scopes = resolve_media_scopes(connection, prefixes)
            finally:
                event.remove(connection.engine, "before_cursor_execute", count_query)

        self.assertTrue(all(scope.match == "exact_item" for scope in bulk_scopes))
        self.assertLessEqual(query_count, 6)

    def test_ambiguous_active_item_and_descendants_fail_closed(self) -> None:
        with open_db(self.db_path) as connection:
            self._insert_item(connection, "other/Foo")
            self._insert_item(connection, "other/Foo/Bar.mkv")

            with self.assertRaisesRegex(MediaScopeConflict, "both an item and a folder prefix"):
                resolve_media_scope(connection, "other/Foo")

    def test_exact_scope_does_not_overlap_descendant_jobs(self) -> None:
        with open_db(self.db_path) as connection:
            self._insert_item(connection, "movies/Foo.mkv")
            exact_scope = resolve_media_scope(connection, "movies/Foo.mkv")

        self.assertTrue(path_matches_scope("movies/Foo.mkv", exact_scope))
        self.assertFalse(path_matches_scope("movies/Foo.mkv/descendant.mkv", exact_scope))
        self.assertTrue(scopes_overlap(exact_scope, "movies"))
        self.assertFalse(scopes_overlap(exact_scope, "movies/Foo.mkv/descendant"))

    @staticmethod
    def _insert_item(
            connection: DBClient,
            rel_path: str,
            *,
            status: str = "discovered",
            source_path: str | None = None,
    ) -> int:
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        path = Path(rel_path)
        result = connection.execute(
            library_items.insert().values(
                source_path=source_path or f"/media/{rel_path}",
                rel_path=rel_path,
                media_root=path.parts[0],
                parent_dir=str(path.parent),
                file_name=path.name,
                container=path.suffix or ".mkv",
                size_bytes=1024,
                mtime_ns=1,
                fingerprint=f"fingerprint-{rel_path}",
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                status=status,
                last_scan_id="scan-1",
                discovered_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        return int(result.inserted_primary_key[0])
