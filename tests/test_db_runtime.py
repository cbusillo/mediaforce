import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select

from mediaforce.core.db import _load_sql_asset
from mediaforce.core.db import open_db
from mediaforce.core.db import reset_engine_cache
from mediaforce.core.db_tables import alembic_version
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import encode_queue_state
from mediaforce.core.type_defs import object_dict


class DatabaseRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    def test_open_db_applies_alembic_schema_to_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                inspector = inspect(connection)
                table_names = inspector.get_table_names()
                indexes = {str(index_row["name"]) for index_row in inspector.get_indexes("encode_jobs")}

            self.assertEqual(version, "20260401_0002")
            self.assertGreaterEqual(len(table_names), 10)
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)

    def test_open_db_stamps_existing_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{db_path}")
            try:
                raw_connection = sqlite3.connect(db_path)
                try:
                    raw_connection.executescript(_load_sql_asset("schema.sql"))
                    raw_connection.commit()
                finally:
                    raw_connection.close()
                with engine.begin() as legacy_connection:
                    legacy_connection.execute(
                        encode_jobs.insert().values(
                            job_id="job-1",
                            prefix="tv/show",
                            status="queued",
                            manifest_path="/tmp/run.json",
                            item_count=1,
                            host_json="{}",
                            created_at="2026-04-01T00:00:00",
                            updated_at="2026-04-01T00:00:00",
                        )
                    )
            finally:
                engine.dispose()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                stored = connection.execute(
                    select(encode_jobs.c.prefix).where(encode_jobs.c.job_id == "job-1")
                ).mappings().fetchone()
                indexes = {str(index_row["name"]) for index_row in inspect(connection).get_indexes("encode_jobs")}

            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(version, "20260401_0002")
            self.assertEqual(stored["prefix"], "tv/show")
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)

    def test_open_db_supports_sqlalchemy_mapping_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with open_db(db_path) as connection:
                connection.execute(
                    encode_queue_state.insert().values(
                        queue_name="heavy",
                        is_paused=0,
                        stop_requested=0,
                        active_job_id=None,
                        updated_at="2026-04-01T00:00:00",
                    )
                )
                row = connection.execute(
                    select(encode_queue_state.c.queue_name, encode_queue_state.c.is_paused)
                    .order_by(encode_queue_state.c.queue_name)
                    .limit(1)
                ).mappings().fetchone()

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["queue_name"], "heavy")
            self.assertIn("queue_name", object_dict(row))

    def test_open_db_rolls_back_on_base_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with self.assertRaises(KeyboardInterrupt):
                with open_db(db_path) as connection:
                    connection.execute(
                        encode_queue_state.insert().values(
                            queue_name="heavy",
                            is_paused=0,
                            stop_requested=0,
                            active_job_id=None,
                            updated_at="2026-04-01T00:00:00",
                        )
                    )
                    raise KeyboardInterrupt()

            with open_db(db_path) as connection:
                row = connection.execute(
                    select(encode_queue_state.c.queue_name).where(encode_queue_state.c.queue_name == "heavy")
                ).fetchall()

            self.assertEqual(len(row), 0)

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
