import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.core.models import ProbeSummary
from mediaforce.library.scanner import (
    _cadence_summary_present,
    _content_version_changed,
    _failed_probe_summary,
    _iter_media_files,
    _media_fingerprint_present,
    scan_library,
)
from mediaforce.core.utils import content_version_fingerprint


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

    def test_failed_probe_becomes_blocked_unknown_evidence(self) -> None:
        summary = _failed_probe_summary(RuntimeError("corrupt media"))

        self.assertFalse(_cadence_summary_present(summary.cadence_summary_json))
        self.assertFalse(_media_fingerprint_present(summary.media_fingerprint_json))
        self.assertIn('"classification":"unknown"', summary.cadence_summary_json)
        self.assertIn("corrupt media", summary.cadence_summary_json)
        self.assertIn('"retry_required":true', summary.cadence_summary_json)
        self.assertIn('"status":"unknown"', summary.media_fingerprint_json)
        self.assertIn("corrupt media", summary.media_fingerprint_json)
        self.assertIn('"retry_required":true', summary.media_fingerprint_json)

    def test_empty_or_malformed_cadence_summary_requires_reprobe(self) -> None:
        self.assertFalse(_cadence_summary_present(None))
        self.assertFalse(_cadence_summary_present("{}"))
        self.assertFalse(_cadence_summary_present("not-json"))
        old_summary = json.loads(_failed_probe_summary(RuntimeError("old")).cadence_summary_json)
        old_summary["analysis"]["tool"]["version"] = "0"
        self.assertFalse(_cadence_summary_present(json.dumps(old_summary)))

    def test_empty_or_malformed_media_fingerprint_requires_reprobe(self) -> None:
        self.assertFalse(_media_fingerprint_present(None))
        self.assertFalse(_media_fingerprint_present("{}"))
        self.assertFalse(_media_fingerprint_present("not-json"))
        old_summary = json.loads(_failed_probe_summary(RuntimeError("old")).media_fingerprint_json)
        old_summary["analysis"]["tool"]["version"] = "0"
        self.assertFalse(_media_fingerprint_present(json.dumps(old_summary)))


if __name__ == "__main__":
    unittest.main()
