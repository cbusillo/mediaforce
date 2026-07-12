import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from mediaforce.library.scanner import (
    _cadence_summary_present,
    _content_version_changed,
    _failed_probe_summary,
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
