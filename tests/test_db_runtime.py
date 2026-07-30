import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# noinspection PyPackageRequirements
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from mediaforce.core import db as db_module
from mediaforce.core.db import _load_sql_asset
from mediaforce.core.db import connect
from mediaforce.core.db import open_db
from mediaforce.core.db import open_readonly_db
from mediaforce.core.db import reset_engine_cache
from mediaforce.core.db_tables import alembic_version
from mediaforce.core.db_tables import background_work_state
from mediaforce.core.db_tables import evidence_queue_state
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import encode_queue_state
from mediaforce.core.db_tables import library_item_evidence_state
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location, run_migrations
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.cadence import cadence_policy_snapshot
from mediaforce.encoding.fingerprint import media_fingerprint_policy_snapshot
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    exclusive_mediaforce_runtime_lock,
    reserve_mediaforce_database_identity,
)

CURRENT_DB_REVISION = "20260726_0019"


class DatabaseRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    def test_open_db_requires_missing_database_reservation_under_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "missing-db-open-probe"},
            ):
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "db_path identity is unavailable",
                ):
                    with open_db(db_path):
                        pass

            self.assertFalse(db_path.exists())

    def test_connect_checks_reserved_identity_before_sqlite_pragmas(self) -> None:
        connection = Mock()
        engine = Mock()
        engine.connect.return_value = connection
        identity = (7, 11, 22)
        identity_changed = MediaforceRuntimeBusyError(
            "database identity changed"
        )

        with (
            patch(
                "mediaforce.core.db._assert_writable_database_identity_reserved",
                side_effect=[identity, identity, identity_changed],
            ),
            patch(
                "mediaforce.core.db._engine_for_db_path",
                return_value=engine,
            ),
            patch(
                "mediaforce.core.db._configure_sqlite_connection",
            ) as configure_sqlite,
            self.assertRaisesRegex(
                MediaforceRuntimeBusyError,
                "database identity changed",
            ),
        ):
            connect(Path("library.sqlite3"))

        connection.close.assert_called_once_with()
        configure_sqlite.assert_not_called()

    def test_open_db_rejects_lease_loss_before_engine_connect(self) -> None:
        engine = Mock()
        lease_identity = (7, 11, 22)
        lease_ended = MediaforceRuntimeBusyError(
            "database lease ended"
        )

        with (
            patch(
                "mediaforce.core.db._assert_writable_database_identity_reserved",
                side_effect=[lease_identity, lease_ended],
            ),
            patch(
                "mediaforce.core.db._engine_for_db_path",
                return_value=engine,
            ),
            self.assertRaisesRegex(
                MediaforceRuntimeBusyError,
                "database lease ended",
            ),
        ):
            with open_db(Path("library.sqlite3")):
                pass

        engine.connect.assert_not_called()

    def test_run_migrations_checks_identity_before_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            identity_guard = Mock(side_effect=RuntimeError("identity changed"))

            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                run_migrations(
                    db_path,
                    identity_guard=identity_guard,
                )

            identity_guard.assert_called_once_with()
            self.assertFalse(db_path.exists())

    def test_open_db_uses_reserved_missing_database_under_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "reserved-db-open-probe"},
            ) as lease:
                reserve_mediaforce_database_identity(
                    config,
                    create_if_missing=True,
                )
                identity = db_path.stat()
                self.assertEqual(
                    lease._database_identity,
                    (identity.st_dev, identity.st_ino),
                )
                with open_db(db_path) as connection:
                    version = connection.execute(
                        select(alembic_version.c.version_num)
                    ).scalar_one()
                    journal_mode = connection.exec_driver_sql(
                        "PRAGMA journal_mode"
                    ).scalar_one()
                    foreign_keys = connection.exec_driver_sql(
                        "PRAGMA foreign_keys"
                    ).scalar_one()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertEqual(str(journal_mode).lower(), "wal")
            self.assertEqual(foreign_keys, 1)

    def test_open_db_rejects_database_replacement_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "pre-commit-replacement-probe"},
            ):
                reserve_mediaforce_database_identity(
                    config,
                    create_if_missing=True,
                )
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "not reserved by the active lease",
                ):
                    with open_db(db_path):
                        db_path.rename(root / "library-original.sqlite3")
                        db_path.write_bytes(b"replacement")

                self.assertEqual(db_path.read_bytes(), b"replacement")

    def test_runtime_lease_rejects_replaced_database_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )
            with open_db(db_path):
                pass
            reset_engine_cache()

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "replaced-db-open-probe"},
            ) as lease:
                reserve_mediaforce_database_identity(config)
                reserved_identity = lease._database_identity
                db_path.rename(root / "library-original.sqlite3")
                for suffix in ("-shm", "-wal"):
                    sidecar = Path(f"{db_path}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()
                with sqlite3.connect(db_path) as replacement:
                    replacement.execute("CREATE TABLE sentinel (value INTEGER NOT NULL)")
                    replacement.execute("INSERT INTO sentinel VALUES (1)")

                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "identity changed under the active lease",
                ):
                    reserve_mediaforce_database_identity(
                        config,
                        create_if_missing=True,
                    )
                self.assertEqual(lease._database_identity, reserved_identity)
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "not reserved by the active lease",
                ):
                    with open_db(db_path):
                        pass

                with sqlite3.connect(db_path) as replacement:
                    self.assertEqual(
                        replacement.execute(
                            "SELECT value FROM sentinel"
                        ).fetchone(),
                        (1,),
                    )
                    self.assertIsNone(replacement.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'alembic_version'"
                    ).fetchone())

    def test_reserved_engine_does_not_recreate_database_removed_during_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "connect-removal-probe"},
            ) as lease:
                reserve_mediaforce_database_identity(
                    config,
                    create_if_missing=True,
                )
                with open_db(db_path):
                    pass
                identity = lease._database_identity
                self.assertIsNotNone(identity)
                engine = db_module._engine_for_db_path(
                    str(db_path.resolve()),
                    (id(lease), identity[0], identity[1]),
                )
                original_path = root / "library-original.sqlite3"

                class _RemovingEngine:
                    def connect(self) -> object:
                        db_path.rename(original_path)
                        return engine.connect()

                with (
                    patch(
                        "mediaforce.core.db._engine_for_db_path",
                        return_value=_RemovingEngine(),
                    ),
                    self.assertRaises(OperationalError),
                ):
                    connect(db_path)

                self.assertTrue(original_path.exists())
                self.assertFalse(db_path.exists())

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
                evidence_index_columns = {
                    str(index_row["name"]): tuple(index_row.get("column_names") or ())
                    for index_row in inspector.get_indexes("library_item_evidence_state")
                }
                evidence_column_details = {
                    str(column["name"]): column
                    for column in inspector.get_columns("library_item_evidence_state")
                }
                library_columns = {str(column["name"]) for column in inspector.get_columns("library_items")}
                encode_columns = {str(column["name"]) for column in inspector.get_columns("encode_jobs")}
                quality_observation_columns = {
                    str(column["name"])
                    for column in inspector.get_columns("quality_search_observations")
                }
                boundary_observation_columns = {
                    str(column["name"])
                    for column in inspector.get_columns("content_intent_boundary_observations")
                }

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertGreaterEqual(len(table_names), 10)
            self.assertIn("idx_encode_jobs_status_retry_ready", indexes)
            self.assertIn("schedule_close_deadline_at", encode_columns)
            self.assertIn("shadow_json", quality_observation_columns)
            self.assertIn("compatibility_key", boundary_observation_columns)
            self.assertIn("content_id", boundary_observation_columns)
            self.assertIn("intent_snapshot_id", boundary_observation_columns)
            self.assertIn("cadence_summary_json", library_columns)
            self.assertIn("media_fingerprint_json", library_columns)
            self.assertIn("attachment_summary_json", library_columns)
            self.assertIn("content_version_changed_at", library_columns)
            self.assertIn("content_version_fingerprint", library_columns)
            self.assertIn("plex_item_metadata", table_names)
            self.assertIn("series_metadata", table_names)
            self.assertIn("metadata_sync_state", table_names)
            self.assertIn("library_item_evidence_state", table_names)
            self.assertIn("evidence_queue_state", table_names)
            self.assertIn("background_work_state", table_names)
            self.assertIn("idx_library_item_evidence_state_kind_state", evidence_indexes)
            self.assertIn("idx_library_item_evidence_state_work_ready", evidence_indexes)
            self.assertIn("idx_library_item_evidence_state_work_claim", evidence_indexes)
            self.assertIn("work_batch_id", evidence_column_details)
            self.assertIn("work_status", evidence_column_details)
            self.assertIn("work_priority", evidence_column_details)
            self.assertIn("work_reason", evidence_column_details)
            self.assertIn("lease_expires_at", evidence_column_details)
            self.assertEqual(
                str(evidence_column_details["work_priority"].get("default") or "").strip("()'\""),
                "100",
            )
            self.assertEqual(
                evidence_index_columns["idx_library_item_evidence_state_work_claim"],
                (
                    "work_batch_id",
                    "work_status",
                    "retry_not_before",
                    "work_priority",
                    "evidence_kind",
                    "library_item_id",
                ),
            )

    def test_open_readonly_db_never_creates_or_mutates_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                with open_readonly_db(missing_path):
                    pass
            self.assertFalse(missing_path.exists())

            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            with open_readonly_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                with self.assertRaisesRegex(OperationalError, "readonly|read-only"):
                    connection.exec_driver_sql("CREATE TABLE forbidden_write (id INTEGER)")

            self.assertEqual(version, CURRENT_DB_REVISION)
            with open_readonly_db(db_path) as connection:
                self.assertNotIn("forbidden_write", inspect(connection).get_table_names())

    def test_open_readonly_db_rejects_replacement_under_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            with open_db(db_path):
                pass
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "readonly-replacement-probe"},
            ):
                replacement_path = root / "replacement.sqlite3"
                replacement_path.write_bytes(db_path.read_bytes())
                replacement_path.chmod(0o600)
                replacement_path.replace(db_path)
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "identity is not reserved|identity changed",
                ):
                    with open_readonly_db(db_path):
                        pass

    def test_open_readonly_db_rechecks_identity_before_each_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            with open_db(db_path):
                pass
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "readonly-query-guard-probe"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "identity is not reserved|identity changed",
                ),
            ):
                with open_readonly_db(db_path) as connection:
                    replacement_path = root / "replacement.sqlite3"
                    replacement_path.write_bytes(db_path.read_bytes())
                    replacement_path.chmod(0o600)
                    replacement_path.replace(db_path)
                    connection.exec_driver_sql("SELECT 1")

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

    def test_open_db_adds_calibration_progress_columns_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE calibration_jobs DROP COLUMN heartbeat_at")
                raw_connection.execute("ALTER TABLE calibration_jobs DROP COLUMN progress_json")
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260719_0014",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                calibration_columns = {
                    str(column["name"])
                    for column in inspect(connection).get_columns("calibration_jobs")
                }

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("heartbeat_at", calibration_columns)
            self.assertIn("progress_json", calibration_columns)

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

    def test_evidence_queue_migration_round_trip_preserves_state_without_starting_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cadence_json = '{"schema_version":1,"decision":{"status":"measured"}}'
            with open_db(db_path) as connection:
                item_result = connection.execute(
                    library_items.insert().values(
                        **self._library_item_values(
                            cadence_summary_json=cadence_json,
                            media_fingerprint_json=None,
                        )
                    )
                )
                item_id = int(item_result.inserted_primary_key[0])
                connection.execute(
                    library_item_evidence_state.insert().values(
                        library_item_id=item_id,
                        evidence_kind="cadence_analysis",
                        state="analysis_required",
                        reason="retry_required",
                        summary_sha256="sha256:fixture",
                        source_fingerprint="content-1",
                        summary_schema_version=1,
                        analyzer_name="mediaforce.ffmpeg_idet",
                        analyzer_version="1",
                        analyzer_runtime_version="ffmpeg fixture",
                        policy_hash="sha256:policy",
                        decision_status="measured",
                        attempt_count=2,
                        retry_not_before="2026-07-20T00:00:00+00:00",
                        last_attempt_at="2026-07-19T12:30:00+00:00",
                        last_error="fixture failure",
                        work_batch_id="batch-1",
                        work_status="queued",
                        work_source_fingerprint="content-1",
                        updated_at="2026-07-19T12:30:00+00:00",
                    )
                )
                connection.execute(
                    evidence_queue_state.insert().values(
                        queue_name="evidence",
                        batch_id="batch-1",
                        status="paused",
                        scope_json='{"prefix":"tv/show"}',
                        evidence_kinds_json='["cadence_analysis"]',
                        is_paused=1,
                        cancel_requested=0,
                        item_count=1,
                        completed_count=0,
                        failed_count=0,
                        cancelled_count=0,
                        created_at="2026-07-19T12:30:00+00:00",
                        updated_at="2026-07-19T12:30:00+00:00",
                    )
                )
            reset_engine_cache()

            with _alembic_script_location() as script_location:
                command.downgrade(
                    _alembic_config(db_path, script_location),
                    "20260719_0011",
                )

            raw_connection = sqlite3.connect(db_path)
            try:
                table_names = {
                    str(row[0])
                    for row in raw_connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                evidence_columns = {
                    str(row[1])
                    for row in raw_connection.execute(
                        "PRAGMA table_info(library_item_evidence_state)"
                    ).fetchall()
                }
                downgraded_state = raw_connection.execute(
                    "SELECT attempt_count, retry_not_before, last_error "
                    "FROM library_item_evidence_state"
                ).fetchone()
                canonical = raw_connection.execute(
                    "SELECT cadence_summary_json FROM library_items"
                ).fetchone()
            finally:
                raw_connection.close()

            self.assertNotIn("evidence_queue_state", table_names)
            self.assertNotIn("background_work_state", table_names)
            self.assertNotIn("work_status", evidence_columns)
            self.assertEqual(
                downgraded_state,
                (2, "2026-07-20T00:00:00+00:00", "fixture failure"),
            )
            self.assertEqual(canonical, (cadence_json,))

            with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")), patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ), open_db(db_path) as connection:
                upgraded_state = connection.execute(
                    select(
                        library_item_evidence_state.c.attempt_count,
                        library_item_evidence_state.c.retry_not_before,
                        library_item_evidence_state.c.last_error,
                        library_item_evidence_state.c.work_batch_id,
                        library_item_evidence_state.c.work_status,
                    )
                ).one()
                queue_rows = connection.execute(select(evidence_queue_state.c.queue_name)).fetchall()
                background_rows = connection.execute(
                    select(background_work_state.c.work_area)
                ).fetchall()
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertEqual(
                tuple(upgraded_state),
                (2, "2026-07-20T00:00:00+00:00", "fixture failure", None, None),
            )
            self.assertEqual(queue_rows, [])
            self.assertEqual(background_rows, [])

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

        with patch(
            "mediaforce.core.db._connect_with_reserved_identity",
            return_value=fake,
        ):
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
