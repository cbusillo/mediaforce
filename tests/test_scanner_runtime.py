import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import select, update

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, scan_runs
from mediaforce.core.models import ProbeSummary
from mediaforce.library.scanner import (
    _content_version_changed,
    _failed_probe_summary,
    _iter_media_files,
    scan_library,
)
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
from mediaforce.web.runtime.job_runtime import _stats_payload


class _FakeCursor:
    rowcount = -1

    @staticmethod
    def fetchone() -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.commit_count = 0
        self.statements: list[str] = []

    def execute(self, sql: object, _params: object | None = None) -> _FakeCursor:
        self.statements.append(str(sql))
        return _FakeCursor()

    def exec_driver_sql(self, sql: object, _params: object | None = None) -> _FakeCursor:
        self.statements.append(str(sql))
        return _FakeCursor()

    def commit(self) -> None:
        self.commit_count += 1


class _FakeConfig:
    source_root_map: dict[str, Path] = {}


class ScannerRuntimeTests(unittest.TestCase):
    @staticmethod
    def _config(project_root: Path, roots: dict[str, Path], labels: dict[str, str] | None = None) -> MediaforceConfig:
        paths = ConfigPaths(
            project_root=project_root,
            config_path=project_root / "config.toml",
            db_path=project_root / "library.sqlite3",
            run_manifest_dir=project_root / "runs",
            web_state_dir=project_root / "web",
            review_dir=project_root / "review",
            runtime_settings_path=project_root / "runtime-settings.json",
        )
        return MediaforceConfig(
            raw={
                "media": {
                    "libraries": [
                        {
                            "key": key,
                            "label": (labels or {}).get(key, key.replace("_", " ").title()),
                            "path": str(path),
                            "type": "other",
                            "availability": "browse_only",
                        }
                        for key, path in roots.items()
                    ]
                },
                "video": {},
                "audio": {},
                "subtitle": {},
                "planning": {},
                "validation": {},
                "overrides": [],
                "remote_hosts": [],
            },
            paths=paths,
        )

    def test_content_version_fingerprint_ignores_mtime_but_detects_same_size_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "episode.mkv"
            path.write_bytes(b"a" * 1024)
            first_stat = path.stat()
            first = content_version_fingerprint(path, first_stat)

            os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns + 1_000_000_000))
            touched = content_version_fingerprint(path, path.stat())
            path.write_bytes(b"b" * 1024)
            os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
            replaced = content_version_fingerprint(path, path.stat())

        self.assertEqual(first, touched)
        self.assertNotEqual(first, replaced)

    def test_first_post_migration_size_change_refreshes_content_age(self) -> None:
        row = {
            "status": "discovered",
            "size_bytes": 1_024,
            "content_version_fingerprint": None,
        }

        self.assertTrue(
            _content_version_changed(
                row,
                size_bytes=2_048,
                content_fingerprint=None,
            )
        )
        self.assertFalse(
            _content_version_changed(
                row,
                size_bytes=1_024,
                content_fingerprint="first-baseline",
            )
        )

    def test_scan_library_commits_progress_before_and_after_work(self) -> None:
        connection = _FakeConnection()

        stats = scan_library(cast(Any, connection), cast(Any, _FakeConfig()))

        self.assertEqual(stats.total_seen, 0)
        self.assertGreaterEqual(connection.commit_count, 2)
        self.assertGreaterEqual(len(connection.statements), 2)
        self.assertTrue(connection.statements[0])
        self.assertTrue(connection.statements[-1])

    def test_scan_releases_write_transaction_before_probing_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            for name in ("first.mkv", "second.mkv"):
                (media_root / name).write_bytes(name.encode())
            paths = ConfigPaths(
                project_root=project_root,
                config_path=project_root / "config.toml",
                db_path=project_root / "library.sqlite3",
                run_manifest_dir=project_root / "runs",
                web_state_dir=project_root / "web",
                review_dir=project_root / "review",
                runtime_settings_path=project_root / "runtime-settings.json",
            )
            config = MediaforceConfig(
                raw={
                    "media": {"source_roots": {"movies": str(media_root)}},
                    "video": {},
                    "audio": {},
                    "subtitle": {},
                    "planning": {},
                    "validation": {},
                    "overrides": [],
                    "remote_hosts": [],
                },
                paths=paths,
            )
            probe_count = 0
            writer_errors: list[Exception] = []

            def probe_with_concurrent_writer(_path: Path) -> ProbeSummary:
                nonlocal probe_count
                probe_count += 1
                if probe_count == 2:
                    try:
                        with open_db(paths.db_path) as writer:
                            writer.exec_driver_sql("PRAGMA busy_timeout=100")
                            writer.exec_driver_sql("BEGIN IMMEDIATE")
                            writer.exec_driver_sql("ROLLBACK")
                    except Exception as exc:
                        writer_errors.append(exc)
                return _failed_probe_summary(RuntimeError("fixture probe"))

            try:
                with patch("mediaforce.library.scanner.probe_media", side_effect=probe_with_concurrent_writer):
                    with open_db(paths.db_path) as connection:
                        stats = scan_library(connection, config)
            finally:
                reset_engine_cache()

        self.assertEqual(stats.discovered, 2)
        self.assertEqual(writer_errors, [])

    def test_unchanged_inventory_preserves_noncurrent_evidence_without_probing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root})
            retryable_summary = _failed_probe_summary(RuntimeError("fixture probe failure"))
            stale_cadence = json.loads(retryable_summary.cadence_summary_json)
            stale_cadence.pop("retry_required", None)
            stale_cadence["analysis"]["tool"]["version"] = "0"
            stale_fingerprint = json.loads(retryable_summary.media_fingerprint_json)
            stale_fingerprint.pop("retry_required", None)
            stale_fingerprint["analysis"]["tool"]["version"] = "0"

            try:
                with open_db(config.paths.db_path) as connection:
                    with patch("mediaforce.library.scanner.probe_media", return_value=retryable_summary):
                        scan_library(connection, config)

                    evidence_cases = (
                        (None, None),
                        ("not-json", "not-json"),
                        (json.dumps(stale_cadence), json.dumps(stale_fingerprint)),
                        (retryable_summary.cadence_summary_json, retryable_summary.media_fingerprint_json),
                    )
                    for cadence_summary, media_fingerprint in evidence_cases:
                        with self.subTest(cadence_summary=cadence_summary, media_fingerprint=media_fingerprint):
                            connection.execute(
                                update(library_items).values(
                                    cadence_summary_json=cadence_summary,
                                    media_fingerprint_json=media_fingerprint,
                                )
                            )
                            connection.commit()
                            with patch(
                                "mediaforce.library.scanner.probe_media",
                                return_value=retryable_summary,
                            ) as probe_mock:
                                stats = scan_library(connection, config)
                            row = connection.execute(select(library_items)).mappings().one()

                            probe_mock.assert_not_called()
                            self.assertEqual(stats.unchanged, 1)
                            self.assertEqual(stats.reprobed, 0)
                            self.assertEqual(row["cadence_summary_json"], cadence_summary)
                            self.assertEqual(row["media_fingerprint_json"], media_fingerprint)
            finally:
                reset_engine_cache()

    def test_mtime_only_change_preserves_evidence_without_probing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root})
            retryable_summary = _failed_probe_summary(RuntimeError("fixture probe failure"))

            try:
                with open_db(config.paths.db_path) as connection:
                    with patch("mediaforce.library.scanner.probe_media", return_value=retryable_summary):
                        scan_library(connection, config)
                    original_row = dict(connection.execute(select(library_items)).mappings().one())
                    original_stat = movie.stat()
                    os.utime(
                        movie,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
                    )

                    with patch(
                        "mediaforce.library.scanner.probe_media",
                        return_value=retryable_summary,
                    ) as probe_mock:
                        stats = scan_library(connection, config)
                    refreshed_row = connection.execute(select(library_items)).mappings().one()
                    expected_fingerprint = file_fingerprint(
                        movie,
                        movie.stat(),
                        refreshed_row["duration_seconds"],
                    )
            finally:
                reset_engine_cache()

        probe_mock.assert_not_called()
        self.assertEqual(stats.unchanged, 1)
        self.assertEqual(stats.reprobed, 0)
        self.assertNotEqual(refreshed_row["mtime_ns"], original_row["mtime_ns"])
        self.assertNotEqual(refreshed_row["fingerprint"], original_row["fingerprint"])
        self.assertEqual(
            refreshed_row["fingerprint"],
            expected_fingerprint,
        )
        self.assertEqual(refreshed_row["cadence_summary_json"], original_row["cadence_summary_json"])
        self.assertEqual(refreshed_row["media_fingerprint_json"], original_row["media_fingerprint_json"])

    def test_same_size_same_mtime_content_replacement_still_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root})
            retryable_summary = _failed_probe_summary(RuntimeError("fixture probe failure"))

            try:
                with open_db(config.paths.db_path) as connection:
                    with patch("mediaforce.library.scanner.probe_media", return_value=retryable_summary):
                        scan_library(connection, config)
                    original_stat = movie.stat()
                    movie.write_bytes(b"other")
                    os.utime(movie, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

                    with patch(
                        "mediaforce.library.scanner.probe_media",
                        return_value=retryable_summary,
                    ) as probe_mock:
                        stats = scan_library(connection, config)
            finally:
                reset_engine_cache()

        probe_mock.assert_called_once_with(movie)
        self.assertEqual(stats.unchanged, 0)
        self.assertEqual(stats.reprobed, 1)

    def test_media_file_prefixes_use_the_configured_root_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            physical_root = Path(temp_dir) / "Movies on Disk"
            feature = physical_root / "Example" / "Feature.mkv"
            feature.parent.mkdir(parents=True)
            feature.write_bytes(b"movie")

            matches = list(
                _iter_media_files(
                    "films",
                    physical_root,
                    prefixes=["films/Example"],
                    limit=None,
                    seen=0,
                )
            )

        self.assertEqual(matches, [feature])

    def test_rescan_rewrites_catalog_identity_to_the_configured_root_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            physical_root = project_root / "Movies on Disk"
            feature = physical_root / "Example" / "Feature.mkv"
            feature.parent.mkdir(parents=True)
            feature.write_bytes(b"not a real movie")
            paths = ConfigPaths(
                project_root=project_root,
                config_path=project_root / "config.toml",
                db_path=project_root / "library.sqlite3",
                run_manifest_dir=project_root / "runs",
                web_state_dir=project_root / "web",
                review_dir=project_root / "review",
                runtime_settings_path=project_root / "runtime-settings.json",
            )

            def config_for(key: str) -> MediaforceConfig:
                return MediaforceConfig(
                    raw={
                        "media": {"source_roots": {key: str(physical_root)}},
                        "video": {},
                        "audio": {},
                        "subtitle": {},
                        "planning": {},
                        "validation": {},
                        "overrides": [],
                        "remote_hosts": [],
                    },
                    paths=paths,
                )

            try:
                with open_db(paths.db_path) as connection:
                    scan_library(connection, config_for("legacy_movies"))
                    scan_library(connection, config_for("films"))
                    row = connection.execute(
                        select(library_items.c.media_root, library_items.c.rel_path, library_items.c.parent_dir)
                    ).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["media_root"], "films")
        self.assertEqual(row["rel_path"], "films/Example/Feature.mkv")
        self.assertEqual(row["parent_dir"], "films/Example")

    def test_unavailable_root_preserves_catalog_rows_and_reports_redacted_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "Mounted Movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root}, {"movies": "Movies"})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        connection.execute(update(library_items).values(status="promoted"))
                        connection.commit()
                        before = connection.execute(select(library_items)).mappings().one()
                        movie.unlink()
                        media_root.rmdir()

                        stats = scan_library(connection, config)
                        after = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(after["status"], "promoted")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["last_scan_id"], before["last_scan_id"])
        self.assertEqual(stats.missing, 0)
        self.assertEqual([warning.code for warning in stats.warnings], ["source_unavailable"])
        payload = _stats_payload(stats)
        self.assertEqual(payload["warnings"][0]["label"], "Movies")
        self.assertEqual(payload["warnings"][0]["preserved_item_count"], 1)
        self.assertNotIn(str(media_root), json.dumps(payload))

    def test_unexpectedly_empty_root_preserves_previous_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        movie.unlink()
                        stats = scan_library(connection, config)
                        row = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["status"], "discovered")
        self.assertEqual(stats.missing, 0)
        self.assertEqual([warning.code for warning in stats.warnings], ["source_unexpectedly_empty"])
        self.assertIn("disable or remove", stats.warnings[0].message)

    def test_healthy_root_reconciles_deletion_while_unavailable_root_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            healthy_root = project_root / "healthy"
            unavailable_root = project_root / "unavailable"
            healthy_root.mkdir()
            unavailable_root.mkdir()
            kept = healthy_root / "kept.mkv"
            removed = healthy_root / "removed.mkv"
            preserved = unavailable_root / "preserved.mkv"
            kept.write_bytes(b"kept")
            removed.write_bytes(b"removed")
            preserved.write_bytes(b"preserved")
            config = self._config(
                project_root,
                {"healthy": healthy_root, "archive": unavailable_root},
                {"healthy": "Healthy", "archive": "Archive"},
            )

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        removed.unlink()
                        preserved.unlink()
                        unavailable_root.rmdir()

                        stats = scan_library(connection, config)
                        rows = {
                            row["file_name"]: row
                            for row in connection.execute(select(library_items)).mappings().all()
                        }
            finally:
                reset_engine_cache()

        self.assertEqual(rows["kept.mkv"]["status"], "discovered")
        self.assertEqual(rows["removed.mkv"]["status"], "missing")
        self.assertEqual(rows["preserved.mkv"]["status"], "discovered")
        self.assertEqual(stats.missing, 1)
        self.assertEqual([warning.library_key for warning in stats.warnings], ["archive"])

    def test_mass_disappearance_preserves_rows_and_reports_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            files = [media_root / f"Feature-{index:02d}.mkv" for index in range(30)]
            for file_path in files:
                file_path.write_bytes(file_path.name.encode())
            config = self._config(project_root, {"movies": media_root}, {"movies": "Movies"})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        for file_path in files[5:]:
                            file_path.unlink()

                        stats = scan_library(connection, config)
                        statuses = connection.execute(select(library_items.c.status)).scalars().all()
            finally:
                reset_engine_cache()

        self.assertEqual(set(statuses), {"discovered"})
        self.assertEqual(stats.missing, 0)
        self.assertEqual([warning.code for warning in stats.warnings], ["source_mass_disappearance"])
        self.assertEqual(stats.warnings[0].preserved_item_count, 25)

    def test_prefix_scan_does_not_touch_or_warn_about_unrelated_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            tv_root = project_root / "tv"
            movie_root = project_root / "movies"
            tv_root.mkdir()
            movie_root.mkdir()
            (tv_root / "Episode.mkv").write_bytes(b"episode")
            movie = movie_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"tv": tv_root, "movies": movie_root})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        movie.unlink()
                        movie_root.rmdir()

                        stats = scan_library(connection, config, prefixes=["tv"])
                        rows = {
                            row["media_root"]: row
                            for row in connection.execute(select(library_items)).mappings().all()
                        }
            finally:
                reset_engine_cache()

        self.assertEqual(stats.total_seen, 1)
        self.assertEqual(stats.warnings, [])
        self.assertEqual(rows["movies"]["status"], "discovered")

    def test_prefix_scan_reports_target_root_unavailable_without_reconciling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root}, {"movies": "Movies"})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        movie.unlink()
                        media_root.rmdir()

                        stats = scan_library(connection, config, prefixes=["movies"])
                        row = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["status"], "discovered")
        self.assertEqual([warning.code for warning in stats.warnings], ["source_unavailable"])
        self.assertNotIn(str(media_root), json.dumps(_stats_payload(stats)))

    def test_new_empty_root_is_valid_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "empty"
            media_root.mkdir()
            config = self._config(project_root, {"empty": media_root})

            try:
                with open_db(config.paths.db_path) as connection:
                    stats = scan_library(connection, config)
                    rows = connection.execute(select(library_items)).mappings().all()
            finally:
                reset_engine_cache()

        self.assertEqual(rows, [])
        self.assertEqual(stats.missing, 0)
        self.assertEqual(stats.warnings, [])

    def test_removed_root_marks_previous_rows_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            (media_root / "Feature.mkv").write_bytes(b"movie")
            configured = self._config(project_root, {"movies": media_root})
            removed = self._config(project_root, {})

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(configured.paths.db_path) as connection:
                        scan_library(connection, configured)
                        stats = scan_library(connection, removed)
                        row = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["status"], "missing")
        self.assertEqual(stats.missing, 1)
        self.assertEqual(stats.warnings, [])

    def test_enumeration_error_preserves_rows_without_exposing_error_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            (media_root / "Feature.mkv").write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root}, {"movies": "Movies"})

            def unreadable_walk(_root: Path, *, onerror: Any) -> Any:
                onerror(PermissionError(str(media_root / "private")))
                return iter(())

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        with patch("mediaforce.library.scanner.os.walk", side_effect=unreadable_walk):
                            stats = scan_library(connection, config)
                        row = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["status"], "discovered")
        self.assertEqual([warning.code for warning in stats.warnings], ["source_scan_incomplete"])
        self.assertNotIn(str(media_root), json.dumps(_stats_payload(stats)))

    def test_file_stat_error_preserves_rows_without_exposing_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            movie = media_root / "Feature.mkv"
            movie.write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root}, {"movies": "Movies"})
            original_stat = Path.stat

            def flaky_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
                if path == movie:
                    raise FileNotFoundError(str(path))
                return original_stat(path, *args, **kwargs)

            try:
                with patch(
                    "mediaforce.library.scanner.probe_media",
                    return_value=_failed_probe_summary(RuntimeError("fixture probe")),
                ):
                    with open_db(config.paths.db_path) as connection:
                        scan_library(connection, config)
                        connection.execute(update(library_items).values(status="promoted"))
                        connection.commit()
                        with patch.object(Path, "stat", new=flaky_stat):
                            stats = scan_library(connection, config)
                        row = connection.execute(select(library_items)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(row["status"], "promoted")
        self.assertEqual([warning.code for warning in stats.warnings], ["source_scan_incomplete"])
        self.assertNotIn(str(movie), json.dumps(_stats_payload(stats)))

    def test_limited_scan_is_not_recorded_as_a_full_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            media_root = project_root / "movies"
            media_root.mkdir()
            (media_root / "Feature.mkv").write_bytes(b"movie")
            config = self._config(project_root, {"movies": media_root})

            try:
                with open_db(config.paths.db_path) as connection:
                    stats = scan_library(connection, config, limit=0)
                    scan_row = connection.execute(select(scan_runs)).mappings().one()
            finally:
                reset_engine_cache()

        self.assertEqual(stats.total_seen, 0)
        self.assertEqual(scan_row["scope"], "limited")
        self.assertEqual(scan_row["file_count"], 0)

    def test_failed_probe_becomes_blocked_unknown_evidence(self) -> None:
        summary = _failed_probe_summary(RuntimeError("corrupt media"))

        self.assertIn('"classification":"unknown"', summary.cadence_summary_json)
        self.assertIn("corrupt media", summary.cadence_summary_json)
        self.assertIn('"retry_required":true', summary.cadence_summary_json)
        self.assertIn('"status":"unknown"', summary.media_fingerprint_json)
        self.assertIn("corrupt media", summary.media_fingerprint_json)
        self.assertIn('"retry_required":true', summary.media_fingerprint_json)


if __name__ == "__main__":
    unittest.main()
