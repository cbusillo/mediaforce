from __future__ import annotations

from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mediaforce.core import db_migrations
from mediaforce.core.db_migrations import database_identity_connection_factory


class DatabaseConnectionCheckpointTests(unittest.TestCase):
    def test_connection_accepts_legitimate_sqlite_change_between_snapshot_and_open(self) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE events (value INTEGER)")

            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None
            real_snapshot = db_migrations._database_connection_path_snapshot
            before = db_path.stat()
            checkpointed = False

            def snapshot_then_checkpoint(
                    path: Path,
            ) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
                nonlocal checkpointed
                snapshot = real_snapshot(path)
                if not checkpointed:
                    checkpointed = True
                    with sqlite3.connect(db_path) as writer:
                        writer.execute("INSERT INTO events VALUES (1)")
                        writer.commit()
                        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return snapshot

            with patch.object(
                db_migrations,
                "_database_connection_path_snapshot",
                side_effect=snapshot_then_checkpoint,
            ) as snapshot_mock:
                connection = factory(str(db_path), check_same_thread=False)
            try:
                self.assertEqual(connection.execute("SELECT value FROM events").fetchone(), (1,))
                connection.assert_database_identity()
            finally:
                connection.close()

            after = db_path.stat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
            self.assertEqual(snapshot_mock.call_count, 2)

    def test_connection_still_rejects_leaf_replacement(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            db_path = root / "library.sqlite3"
            replacement = root / "replacement.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE expected (value INTEGER)")
            with sqlite3.connect(replacement) as connection:
                connection.execute("CREATE TABLE replacement (value INTEGER)")

            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None
            real_snapshot = db_migrations._database_connection_path_snapshot

            def snapshot_then_replace(
                    path: Path,
            ) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
                snapshot = real_snapshot(path)
                replacement.replace(db_path)
                return snapshot

            with patch.object(
                db_migrations,
                "_database_connection_path_snapshot",
                side_effect=snapshot_then_replace,
            ) as snapshot_mock, self.assertRaisesRegex(RuntimeError, "identity changed during connection"):
                factory(str(db_path), check_same_thread=False)
            snapshot_mock.assert_called_once()

    def test_connection_bounds_repeated_pre_open_metadata_churn(self) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            sqlite3.connect(db_path).close()
            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None
            real_snapshot = db_migrations._database_connection_path_snapshot

            def snapshot_then_change(
                    path: Path,
            ) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
                snapshot = real_snapshot(path)
                os.utime(db_path, None)
                return snapshot

            with patch.object(
                db_migrations,
                "_database_connection_path_snapshot",
                side_effect=snapshot_then_change,
            ) as snapshot_mock, self.assertRaisesRegex(
                RuntimeError,
                "identity changed during connection",
            ):
                factory(str(db_path), check_same_thread=False)
            self.assertEqual(snapshot_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
