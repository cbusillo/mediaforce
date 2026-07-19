import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# noinspection PyPackageRequirements
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select

from mediaforce.core.db import _load_sql_asset
from mediaforce.core.db import open_db
from mediaforce.core.db import reset_engine_cache
from mediaforce.core.db_tables import alembic_version
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import encode_queue_state
from mediaforce.core.db_tables import library_item_evidence_state
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.cadence import cadence_policy_snapshot
from mediaforce.encoding.fingerprint import media_fingerprint_policy_snapshot

CURRENT_DB_REVISION = "20260719_0011"


class DatabaseRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    def test_open_db_applies_alembic_schema_to_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"

            with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")), patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ), open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                inspector = inspect(connection)
                table_names = inspector.get_table_names()
                indexes = {str(index_row["name"]) for index_row in inspector.get_indexes("encode_jobs")}
                evidence_indexes = {
                    str(index_row["name"])
                    for index_row in inspector.get_indexes("library_item_evidence_state")
                }
                library_columns = {str(column["name"]) for column in inspector.get_columns("library_items")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertGreaterEqual(len(table_names), 10)
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)
            self.assertIn("cadence_summary_json", library_columns)
            self.assertIn("media_fingerprint_json", library_columns)
            self.assertIn("attachment_summary_json", library_columns)
            self.assertIn("content_version_changed_at", library_columns)
            self.assertIn("content_version_fingerprint", library_columns)
            self.assertIn("plex_item_metadata", table_names)
            self.assertIn("series_metadata", table_names)
            self.assertIn("metadata_sync_state", table_names)
            self.assertIn("library_item_evidence_state", table_names)
            self.assertIn("idx_library_item_evidence_state_kind_state", evidence_indexes)
            self.assertIn("idx_library_item_evidence_state_work_ready", evidence_indexes)

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
            self.assertEqual(version, CURRENT_DB_REVISION)
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

    def test_open_db_adds_cadence_evidence_column_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE library_items DROP COLUMN cadence_summary_json")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260403_0004",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                columns = {str(column["name"]) for column in inspect(connection).get_columns("library_items")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("cadence_summary_json", columns)

    def test_open_db_adds_attachment_summary_columns_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE library_items DROP COLUMN attachment_summary_json")
                raw_connection.execute("ALTER TABLE staged_artifacts DROP COLUMN attachment_summary_json")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260711_0005",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                inspector = inspect(connection)
                library_columns = {str(column["name"]) for column in inspector.get_columns("library_items")}
                staged_columns = {str(column["name"]) for column in inspector.get_columns("staged_artifacts")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("attachment_summary_json", library_columns)
            self.assertIn("attachment_summary_json", staged_columns)

    def test_open_db_adds_media_fingerprint_column_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE library_items DROP COLUMN media_fingerprint_json")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260711_0006",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                columns = {str(column["name"]) for column in inspect(connection).get_columns("library_items")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("media_fingerprint_json", columns)

    def test_open_db_adds_library_lifecycle_metadata_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("DROP TABLE series_metadata")
                raw_connection.execute("DROP TABLE plex_item_metadata")
                raw_connection.execute("ALTER TABLE library_items DROP COLUMN content_version_changed_at")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260711_0007",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                inspector = inspect(connection)
                table_names = inspector.get_table_names()
                library_columns = {str(column["name"]) for column in inspector.get_columns("library_items")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("content_version_changed_at", library_columns)
            self.assertIn("plex_item_metadata", table_names)
            self.assertIn("series_metadata", table_names)

    def test_open_db_adds_content_version_fingerprint_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE library_items DROP COLUMN content_version_fingerprint")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260712_0008",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                columns = {str(column["name"]) for column in inspect(connection).get_columns("library_items")}

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("content_version_fingerprint", columns)

    def test_open_db_adds_metadata_sync_state_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("DROP TABLE metadata_sync_state")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260712_0009",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                table_names = inspect(connection).get_table_names()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("metadata_sync_state", table_names)

    def test_open_db_projects_existing_evidence_without_media_subprocesses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cadence_json = json.dumps(
                {
                    "schema_version": 1,
                    "probe": {
                        "field_order": "progressive",
                        "idet_required": False,
                    },
                    "analysis": {
                        "sampled_frames": 0,
                        "tool": {
                            "name": "mediaforce.ffmpeg_idet",
                            "version": "1",
                            "ffmpeg_version": "ffmpeg fixture",
                        }
                    },
                    "decision": {"status": "resolved", "classification": "progressive"},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            fingerprint_json = '{"retry_required":true}'
            with open_db(db_path) as connection:
                connection.execute(
                    library_items.insert().values(
                        **self._library_item_values(
                            cadence_summary_json=cadence_json,
                            media_fingerprint_json=fingerprint_json,
                        )
                    )
                )
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("DROP TABLE library_item_evidence_state")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260712_0010",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")), patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ), open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                state_rows = connection.execute(
                    select(
                        library_item_evidence_state.c.evidence_kind,
                        library_item_evidence_state.c.state,
                        library_item_evidence_state.c.reason,
                        library_item_evidence_state.c.policy_hash,
                    ).order_by(library_item_evidence_state.c.evidence_kind)
                ).mappings().fetchall()
                canonical = connection.execute(
                    select(
                        library_items.c.cadence_summary_json,
                        library_items.c.media_fingerprint_json,
                    )
                ).mappings().one()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertEqual(
                [(row["evidence_kind"], row["state"], row["reason"]) for row in state_rows],
                [
                    ("cadence_analysis", "current", None),
                    ("media_fingerprint", "analysis_required", "retry_required"),
                ],
            )
            self.assertEqual(canonical["cadence_summary_json"], cadence_json)
            self.assertEqual(canonical["media_fingerprint_json"], fingerprint_json)
            self.assertEqual(
                {row["evidence_kind"]: row["policy_hash"] for row in state_rows},
                {
                    "cadence_analysis": stable_policy_hash(cadence_policy_snapshot()),
                    "media_fingerprint": stable_policy_hash(media_fingerprint_policy_snapshot()),
                },
            )

    def test_evidence_state_downgrade_preserves_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cadence_json = '{"schema_version":1}'
            fingerprint_json = '{"schema_version":1}'
            with open_db(db_path) as connection:
                connection.execute(
                    library_items.insert().values(
                        **self._library_item_values(
                            cadence_summary_json=cadence_json,
                            media_fingerprint_json=fingerprint_json,
                        )
                    )
                )
            reset_engine_cache()

            with _alembic_script_location() as script_location:
                command.downgrade(
                    _alembic_config(db_path, script_location),
                    "20260712_0010",
                )

            raw_connection = sqlite3.connect(db_path)
            try:
                table_names = {
                    str(row[0])
                    for row in raw_connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                canonical = raw_connection.execute(
                    "SELECT cadence_summary_json, media_fingerprint_json FROM library_items"
                ).fetchone()
                version = raw_connection.execute("SELECT version_num FROM alembic_version").fetchone()
            finally:
                raw_connection.close()

            self.assertNotIn("library_item_evidence_state", table_names)
            self.assertEqual(canonical, (cadence_json, fingerprint_json))
            self.assertEqual(version, ("20260712_0010",))

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

    @staticmethod
    def _library_item_values(
            *,
            cadence_summary_json: str | None,
            media_fingerprint_json: str | None,
    ) -> dict[str, object]:
        now = "2026-07-19T12:00:00+00:00"
        return {
            "source_path": "/media/item.mkv",
            "rel_path": "tv/show/item.mkv",
            "media_root": "tv",
            "parent_dir": "tv/show",
            "file_name": "item.mkv",
            "container": ".mkv",
            "size_bytes": 1000,
            "mtime_ns": 1,
            "fingerprint": "file-1",
            "duration_seconds": 60.0,
            "video_codec": "h264",
            "audio_track_count": 1,
            "subtitle_track_count": 0,
            "english_audio_count": 1,
            "english_subtitle_count": 0,
            "audio_summary_json": "[]",
            "subtitle_summary_json": "[]",
            "cadence_summary_json": cadence_summary_json,
            "media_fingerprint_json": media_fingerprint_json,
            "content_version_changed_at": now,
            "content_version_fingerprint": "content-1",
            "status": "discovered",
            "priority_score": 0,
            "last_scan_id": "fixture",
            "discovered_at": now,
            "last_seen_at": now,
            "updated_at": now,
        }


if __name__ == "__main__":
    unittest.main()
