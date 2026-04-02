import unittest
from pathlib import Path
from typing import Any, cast

from mediaforce.library.scanner import scan_library


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
    def test_scan_library_commits_progress_before_and_after_work(self) -> None:
        connection = _FakeConnection()

        stats = scan_library(cast(Any, connection), cast(Any, _FakeConfig()))

        self.assertEqual(stats.total_seen, 0)
        self.assertGreaterEqual(connection.commit_count, 2)
        self.assertGreaterEqual(len(connection.statements), 2)
        self.assertTrue(connection.statements[0])
        self.assertTrue(connection.statements[-1])


if __name__ == "__main__":
    unittest.main()
