import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaforce.core.db import _load_sql_asset
from mediaforce.core.db import open_db
from mediaforce.core.db import reset_engine_cache


class DatabaseRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    def test_open_db_applies_alembic_schema_to_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with open_db(db_path) as connection:
                version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
                row = connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
                indexes = {
                    str(index_row["name"])
                    for index_row in connection.exec_driver_sql("PRAGMA index_list('encode_jobs')").mappings().fetchall()
                }

            self.assertEqual(version[0], "20260401_0002")
            self.assertGreaterEqual(int(row[0]), 10)
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)

    def test_open_db_stamps_existing_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_connection = sqlite3.connect(db_path)
            try:
                legacy_connection.executescript(_load_sql_asset("schema.sql"))
                legacy_connection.execute(
                    "INSERT INTO encode_jobs(job_id, prefix, status, manifest_path, item_count, host_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("job-1", "tv/show", "queued", "/tmp/run.json", 1, "{}", "2026-04-01T00:00:00", "2026-04-01T00:00:00"),
                )
                legacy_connection.commit()
            finally:
                legacy_connection.close()

            with open_db(db_path) as connection:
                version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
                stored = connection.exec_driver_sql(
                    "SELECT prefix FROM encode_jobs WHERE job_id = ?", ("job-1",)
                ).mappings().fetchone()
                indexes = {
                    str(index_row["name"])
                    for index_row in connection.exec_driver_sql("PRAGMA index_list('encode_jobs')").mappings().fetchall()
                }

            self.assertEqual(version[0], "20260401_0002")
            self.assertEqual(stored["prefix"], "tv/show")
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)

    def test_open_db_supports_sqlalchemy_mapping_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with open_db(db_path) as connection:
                connection.exec_driver_sql(
                    "INSERT INTO encode_queue_state(queue_name, is_paused, stop_requested, active_job_id, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("heavy", 0, 0, None, "2026-04-01T00:00:00"),
                )
                row = connection.exec_driver_sql(
                    "SELECT queue_name, is_paused FROM encode_queue_state ORDER BY queue_name LIMIT 1"
                ).mappings().fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["queue_name"], "heavy")
            self.assertIn("queue_name", dict(row))

    def test_open_db_rolls_back_on_base_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with self.assertRaises(KeyboardInterrupt):
                with open_db(db_path) as connection:
                    connection.exec_driver_sql(
                        "INSERT INTO encode_queue_state(queue_name, is_paused, stop_requested, active_job_id, updated_at) VALUES (?, ?, ?, ?, ?)",
                        ("heavy", 0, 0, None, "2026-04-01T00:00:00"),
                    )
                    raise KeyboardInterrupt()

            with open_db(db_path) as connection:
                row = connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM encode_queue_state WHERE queue_name = ?",
                    ("heavy",),
                ).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(int(row[0]), 0)

    def test_finalize_closes_connection_when_commit_raises(self) -> None:
        class _FakeConnection:
            def __init__(self) -> None:
                self.closed = False
                self.close_called = False
                self.commit_called = False

            @staticmethod
            def in_transaction() -> bool:
                return True

            def commit(self) -> None:
                self.commit_called = True
                raise RuntimeError("commit failed")

            def rollback(self) -> None:
                raise AssertionError("rollback should not be called")

            def close(self) -> None:
                self.close_called = True
                self.closed = True

        fake = _FakeConnection()

        with patch("mediaforce.core.db.connect", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                with open_db(Path("/tmp/fake.sqlite3")):
                    pass

        self.assertTrue(fake.commit_called)
        self.assertTrue(fake.close_called)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
