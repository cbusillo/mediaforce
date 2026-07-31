import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
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
from mediaforce.core import config as config_module
from mediaforce.core import db_migrations as db_migrations_module
from mediaforce.core.config import ConfigPaths
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.config import migrate_config_state
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
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location, run_migrations
from mediaforce.core.db_migrations import create_engine_for_path
from mediaforce.core.db_migrations import database_url
from mediaforce.core.db_migrations import database_identity_connection_factory
from mediaforce.core.db_migrations import readonly_database_url
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.cadence import cadence_policy_snapshot
from mediaforce.encoding.fingerprint import media_fingerprint_policy_snapshot
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    exclusive_mediaforce_runtime_lock,
    reserve_mediaforce_database_identity,
)

CURRENT_DB_REVISION = "20260731_0020"


class DatabaseRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    @staticmethod
    def _legacy_migration_config(
            root: Path,
            *,
            database_path: Path,
            name: str,
    ) -> MediaforceConfig:
        config_path = root / f"{name}.toml"
        config_path.write_text("[state]\n", encoding="utf-8")
        runtime_root = root / f"{name}-runtime"
        return MediaforceConfig(
            raw={},
            paths=ConfigPaths(
                project_root=root,
                config_path=config_path,
                db_path=database_path,
                run_manifest_dir=runtime_root / "runs",
                web_state_dir=runtime_root / "web",
                review_dir=runtime_root / "review",
                runtime_settings_path=runtime_root / "runtime-settings.json",
                runtime_reservation_dir=runtime_root / "reservations",
            ),
        )

    def test_migrate_config_state_preserves_committed_wal_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            source_connection = sqlite3.connect(source_path)
            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('committed-wal-row')"
                )
                source_connection.commit()
                self.assertTrue(Path(f"{source_path}-wal").is_file())

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-wal-migration"},
                ):
                    migrate_config_state(config)

                with sqlite3.connect(destination_path) as destination_connection:
                    self.assertEqual(
                        destination_connection.execute(
                            "SELECT value FROM migration_rows"
                        ).fetchall(),
                        [("committed-wal-row",)],
                    )
                self.assertFalse(source_path.exists())
                self.assertFalse(Path(f"{source_path}-wal").exists())
                self.assertFalse(Path(f"{source_path}-shm").exists())
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_active_legacy_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            source_path.parent.mkdir()
            sqlite3.connect(source_path).close()
            target_config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_config = self._legacy_migration_config(
                root,
                database_path=source_path,
                name="legacy-source",
            )

            with exclusive_mediaforce_runtime_lock(
                source_config,
                owner_payload={"purpose": "legacy-runtime-owner"},
            ):
                with exclusive_mediaforce_runtime_lock(
                    target_config,
                    owner_payload={"purpose": "legacy-migration-probe"},
                ):
                    with self.assertRaises(MediaforceRuntimeBusyError):
                        migrate_config_state(target_config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())

    def test_migrate_config_state_rejects_active_legacy_sqlite_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as source_connection:
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
            writer_connection = sqlite3.connect(
                source_path,
                timeout=0,
                isolation_level=None,
            )
            try:
                writer_connection.execute("BEGIN IMMEDIATE")
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-writer-contention"},
                ):
                    with self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "active or unavailable",
                    ):
                        migrate_config_state(config)
            finally:
                writer_connection.rollback()
                writer_connection.close()

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())

    def test_migrate_config_state_rejects_symlink_and_hardlink_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            state_root = root / "state"
            state_root.mkdir()
            backing_path = root / "backing.sqlite3"
            sqlite3.connect(backing_path).close()
            source_path = state_root / "library.sqlite3"
            source_path.symlink_to(backing_path)

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-symlink-probe"},
            ):
                with self.assertRaisesRegex(MediaforceRuntimeBusyError, "unsafe"):
                    migrate_config_state(config)

            source_path.unlink()
            sqlite3.connect(source_path).close()
            hardlink_path = root / "legacy-alias.sqlite3"
            os.link(source_path, hardlink_path)
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-hardlink-probe"},
            ):
                with self.assertRaisesRegex(MediaforceRuntimeBusyError, "unsafe"):
                    migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertTrue(hardlink_path.is_file())
            self.assertFalse(destination_path.exists())

    def test_migrate_config_state_rejects_source_path_replacement_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as source_connection:
                source_connection.execute("CREATE TABLE source_rows (value TEXT NOT NULL)")
                source_connection.execute("INSERT INTO source_rows VALUES ('original')")
            with sqlite3.connect(replacement_path) as replacement_connection:
                replacement_connection.execute(
                    "CREATE TABLE replacement_rows (value TEXT NOT NULL)"
                )
                replacement_connection.execute(
                    "INSERT INTO replacement_rows VALUES ('replacement')"
                )
            original_fsync_file = config_module._fsync_file

            def replace_source_then_fsync(path: Path) -> None:
                replacement_path.replace(source_path)
                original_fsync_file(path)

            with patch.object(
                config_module,
                "_fsync_file",
                side_effect=replace_source_then_fsync,
            ):
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-replacement-probe"},
                ):
                    with self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "identity changed",
                    ):
                        migrate_config_state(config)

            with sqlite3.connect(source_path) as replacement_connection:
                self.assertEqual(
                    replacement_connection.execute(
                        "SELECT value FROM replacement_rows"
                    ).fetchall(),
                    [("replacement",)],
                )
            self.assertFalse(destination_path.exists())
            self.assertEqual(
                list(destination_path.parent.glob(".library.sqlite3.migration-*.sqlite3")),
                [],
            )

    def test_migrate_config_state_cleans_staging_after_interrupted_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            sqlite3.connect(source_path).close()

            with patch.object(
                config_module,
                "_fsync_file",
                side_effect=OSError("interrupted staging sync"),
            ):
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-interruption-probe"},
                ):
                    with self.assertRaisesRegex(OSError, "interrupted staging sync"):
                        migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertEqual(
                list(destination_path.parent.glob(".library.sqlite3.migration-*.sqlite3")),
                [],
            )

    def test_migrate_config_state_defers_destination_creation_until_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            sqlite3.connect(source_path).close()
            original_copy = config_module._copied_legacy_sqlite_database
            copy_started = False

            @contextmanager
            def assert_destination_is_missing(locked_source: object, staging_path: Path):
                nonlocal copy_started
                self.assertFalse(destination_path.exists())
                copy_started = True
                with original_copy(locked_source, staging_path):
                    yield

            with patch.object(
                config_module,
                "_copied_legacy_sqlite_database",
                side_effect=assert_destination_is_missing,
            ):
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-ordering-probe"},
                ):
                    migrate_config_state(config)

            self.assertTrue(copy_started)
            self.assertTrue(destination_path.is_file())

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

    def test_connection_factory_rejects_replacement_despite_expected_concurrent_open(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            original_path = root / "library-original.sqlite3"
            sqlite3.connect(db_path).close()
            sqlite3.connect(replacement_path).close()
            expected_identity = db_path.stat()

            def identity_guard() -> None:
                current = db_path.stat()
                if (
                    current.st_dev,
                    current.st_ino,
                ) != (
                    expected_identity.st_dev,
                    expected_identity.st_ino,
                ):
                    raise RuntimeError("database identity changed")

            factory = database_identity_connection_factory(
                db_path,
                identity_guard,
            )
            assert factory is not None
            real_connection = db_migrations_module._DatabaseIdentityConnection
            legitimate_connections: list[sqlite3.Connection] = []

            def connect_replacement(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                db_path.replace(original_path)
                replacement_path.replace(db_path)
                try:
                    replacement_connection = real_connection(*args, **kwargs)
                finally:
                    db_path.replace(replacement_path)
                    original_path.replace(db_path)
                legitimate_connections.append(sqlite3.Connection(db_path))
                return replacement_connection

            try:
                with (
                    patch(
                        "mediaforce.core.db_migrations._DatabaseIdentityConnection",
                        side_effect=connect_replacement,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "identity changed during connection",
                    ),
                ):
                    factory(str(db_path), check_same_thread=False)
            finally:
                for connection in legitimate_connections:
                    connection.close()

    def test_connection_factory_pins_resolved_directory_during_ancestor_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            replacement_root = root / "replacement"
            original_root.mkdir()
            replacement_root.mkdir()
            original_path = original_root / "library.sqlite3"
            replacement_path = replacement_root / "library.sqlite3"
            with sqlite3.connect(original_path) as connection:
                connection.execute("CREATE TABLE expected_database (value INTEGER)")
            with sqlite3.connect(replacement_path) as connection:
                connection.execute("CREATE TABLE replacement_database (value INTEGER)")
            database_root = root / "database"
            database_root.symlink_to(original_root, target_is_directory=True)
            db_path = database_root / "library.sqlite3"
            resolved_path = db_path.resolve()
            expected_identity = resolved_path.stat()

            def identity_guard() -> None:
                current = resolved_path.stat()
                if (
                    current.st_dev,
                    current.st_ino,
                ) != (
                    expected_identity.st_dev,
                    expected_identity.st_ino,
                ):
                    raise RuntimeError("database identity changed")

            factory = database_identity_connection_factory(
                db_path,
                identity_guard,
            )
            assert factory is not None
            real_connection = db_migrations_module._DatabaseIdentityConnection

            def connect_replacement(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                database_root.unlink()
                database_root.symlink_to(
                    replacement_root,
                    target_is_directory=True,
                )
                try:
                    return real_connection(*args, **kwargs)
                finally:
                    database_root.unlink()
                    database_root.symlink_to(
                        original_root,
                        target_is_directory=True,
                    )

            with patch(
                "mediaforce.core.db_migrations._DatabaseIdentityConnection",
                side_effect=connect_replacement,
            ):
                connection = factory(str(db_path), check_same_thread=False)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertIn("expected_database", tables)
            self.assertNotIn("replacement_database", tables)

    def test_connection_factory_rejects_substituted_opened_parent_with_hardlinked_leaf(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_root = root / "database"
            replacement_root = root / "replacement"
            retired_root = root / "retired"
            database_root.mkdir()
            replacement_root.mkdir()
            db_path = database_root / "library.sqlite3"
            sqlite3.connect(db_path).close()
            replacement_path = replacement_root / db_path.name
            replacement_path.hardlink_to(db_path)
            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None
            real_snapshot = (
                db_migrations_module._database_connection_path_snapshot
            )
            parent_swapped = False

            def snapshot_before_parent_swap(
                    path: Path,
            ) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
                nonlocal parent_swapped
                snapshot = real_snapshot(path)
                database_root.rename(retired_root)
                replacement_root.rename(database_root)
                parent_swapped = True
                return snapshot

            try:
                with (
                    patch.object(
                        db_migrations_module,
                        "_database_connection_path_snapshot",
                        side_effect=snapshot_before_parent_swap,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "identity changed during connection",
                    ),
                ):
                    factory(str(db_path), check_same_thread=False)
            finally:
                if parent_swapped:
                    database_root.rename(replacement_root)
                    retired_root.rename(database_root)

    def test_connection_factory_rejects_post_connect_parent_hardlink_namespace_swap(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_root = root / "database"
            replacement_root = root / "replacement"
            retired_root = root / "retired"
            database_root.mkdir()
            replacement_root.mkdir()
            db_path = database_root / "library.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE expected_database (value INTEGER)")
            replacement_path = replacement_root / db_path.name
            replacement_path.hardlink_to(db_path)
            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None
            real_connection = db_migrations_module._DatabaseIdentityConnection
            parent_swapped = False

            def connect_then_swap_parent(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                nonlocal parent_swapped
                connection = real_connection(*args, **kwargs)
                database_root.rename(retired_root)
                replacement_root.rename(database_root)
                parent_swapped = True
                return connection

            try:
                with (
                    patch(
                        "mediaforce.core.db_migrations._DatabaseIdentityConnection",
                        side_effect=connect_then_swap_parent,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "identity changed during connection",
                    ),
                ):
                    factory(str(db_path), check_same_thread=False)
            finally:
                if parent_swapped:
                    database_root.rename(replacement_root)
                    retired_root.rename(database_root)

    def test_sqlalchemy_guards_parent_hardlink_namespace_for_connection_lifetime(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_root = root / "database"
            replacement_root = root / "replacement"
            retired_root = root / "retired"
            database_root.mkdir()
            replacement_root.mkdir()
            db_path = database_root / "library.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE expected_database (value INTEGER)")
            (replacement_root / db_path.name).hardlink_to(db_path)
            engine = create_engine_for_path(db_path, identity_guard=Mock())
            try:
                with engine.connect() as connection:
                    database_root.rename(retired_root)
                    replacement_root.rename(database_root)
                    try:
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "identity changed during connection",
                        ):
                            connection.exec_driver_sql("SELECT 1")
                    finally:
                        database_root.rename(replacement_root)
                        retired_root.rename(database_root)
            finally:
                engine.dispose()

    def test_connection_factory_retains_verified_descriptors_until_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            sqlite3.connect(db_path).close()
            factory = database_identity_connection_factory(db_path, Mock())
            assert factory is not None

            connection = factory(str(db_path), check_same_thread=False)
            descriptors = connection._database_identity_descriptors
            self.assertEqual(len(descriptors), 2)
            for descriptor in descriptors:
                os.fstat(descriptor)
            connection.close()
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_sqlite_urls_quote_legal_special_characters_without_changing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library space#?%:.sqlite3"
            writable_url = database_url(db_path)
            readonly_url = readonly_database_url(db_path)
            for encoded_character in ("%20", "%23", "%3F", "%25", "%3A"):
                self.assertIn(encoded_character, writable_url)
                self.assertIn(encoded_character, readonly_url)

            engine = create_engine_for_path(db_path)
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE expected_database (value INTEGER)"
                    )
            finally:
                engine.dispose()
            self.assertTrue(db_path.is_file())

            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")

            identity_guard = Mock()
            guarded_engine = create_engine_for_path(
                db_path,
                identity_guard=identity_guard,
            )
            try:
                with guarded_engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("PRAGMA journal_mode").scalar_one(),
                        "wal",
                    )
                    self.assertEqual(
                        connection.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE name = 'expected_database'"
                        ).scalar_one(),
                        "expected_database",
                    )
            finally:
                guarded_engine.dispose()

            readonly_engine = create_engine(readonly_url)
            try:
                with readonly_engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE name = 'expected_database'"
                        ).scalar_one(),
                        "expected_database",
                    )
                    with self.assertRaisesRegex(
                        OperationalError,
                        "readonly|read-only",
                    ):
                        connection.exec_driver_sql(
                            "CREATE TABLE forbidden_write (value INTEGER)"
                        )
            finally:
                readonly_engine.dispose()

            config = _alembic_config(db_path, "unused")
            self.assertEqual(config.get_main_option("sqlalchemy.url"), writable_url)

    def test_connection_factory_fails_closed_without_descriptor_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            sqlite3.connect(db_path).close()
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
            )
            assert factory is not None

            with (
                patch(
                    "mediaforce.core.db_migrations.sys.platform",
                    "unsupported",
                ),
                patch(
                    "mediaforce.core.db_migrations._DatabaseIdentityConnection",
                ) as connection,
                self.assertRaisesRegex(
                    RuntimeError,
                    "actual-opened identity inspection is unavailable",
                ),
            ):
                factory(str(db_path), check_same_thread=False)

            connection.assert_not_called()

    def test_connection_factory_preserves_readonly_uri_and_wal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE expected_database (value INTEGER)")
            resolved_path = db_path.resolve()
            identity_guard = Mock()
            factory = database_identity_connection_factory(
                resolved_path,
                identity_guard,
            )
            assert factory is not None

            connection = factory(
                f"file:{resolved_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone(),
                    ("wal",),
                )
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "readonly|read-only",
                ):
                    connection.execute("CREATE TABLE forbidden_write (value INTEGER)")
            finally:
                connection.close()
            self.assertEqual(identity_guard.call_count, 3)

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

    def test_open_db_rechecks_identity_after_commit(self) -> None:
        events: list[str] = []

        class _FakeConnection:
            @staticmethod
            def in_transaction() -> bool:
                return True

            @staticmethod
            def rollback() -> None:
                raise AssertionError("rollback should not be called")

            def commit(self) -> None:
                events.append("commit")

            def close(self) -> None:
                events.append("close")

        guard_calls = 0

        def identity_guard() -> None:
            nonlocal guard_calls
            guard_calls += 1
            events.append("guard")
            if guard_calls == 2:
                raise MediaforceRuntimeBusyError("database identity changed")

        with (
            patch(
                "mediaforce.core.db._assert_writable_database_identity_reserved",
                return_value=(7, 11, 22),
            ),
            patch(
                "mediaforce.core.db._database_identity_guard",
                return_value=identity_guard,
            ),
            patch(
                "mediaforce.core.db._connect_with_reserved_identity",
                return_value=_FakeConnection(),
            ),
            self.assertRaisesRegex(
                MediaforceRuntimeBusyError,
                "database identity changed",
            ),
        ):
            with open_db(Path("library.sqlite3")):
                pass

        self.assertEqual(events, ["guard", "commit", "guard", "close"])

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
                    self.assertRaises(MediaforceRuntimeBusyError),
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
                scan_columns = {str(column["name"]) for column in inspector.get_columns("scan_runs")}
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
            self.assertEqual(scan_runs.c.status.server_default.arg, "running")
            self.assertIn("status", scan_columns)
            self.assertIn("error", scan_columns)
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

    def test_open_readonly_db_rechecks_identity_after_each_query(self) -> None:
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
                    owner_payload={"purpose": "readonly-post-query-guard-probe"},
                ),
                open_readonly_db(db_path) as connection,
            ):
                replacement_path = root / "replacement.sqlite3"
                replacement_path.write_bytes(db_path.read_bytes())
                replacement_path.chmod(0o600)
                original_path = root / "library-original.sqlite3"

                def replace_database() -> int:
                    db_path.replace(original_path)
                    replacement_path.replace(db_path)
                    return 1

                raw_connection = connection.connection.driver_connection
                raw_connection.create_function("replace_database", 0, replace_database)
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "identity is not reserved|identity changed",
                ):
                    connection.exec_driver_sql("SELECT replace_database()")
                db_path.unlink()
                original_path.replace(db_path)

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

    def test_open_db_adds_scan_run_terminal_columns_to_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path):
                pass
            reset_engine_cache()

            raw_connection = sqlite3.connect(db_path)
            try:
                raw_connection.execute("ALTER TABLE scan_runs DROP COLUMN status")
                raw_connection.execute("ALTER TABLE scan_runs DROP COLUMN error")
                raw_connection.execute(
                    """
                    INSERT INTO scan_runs (
                        scan_id,
                        started_at,
                        completed_at,
                        owner_pid,
                        last_progress_at,
                        roots_json,
                        scope,
                        prefixes_json,
                        file_count,
                        reprobed_count,
                        unchanged_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "historical-completed-scan",
                        "2026-07-30T12:00:00+00:00",
                        "2026-07-30T12:05:00+00:00",
                        None,
                        "2026-07-30T12:05:00+00:00",
                        '["tv"]',
                        "full",
                        None,
                        10,
                        4,
                        6,
                    ),
                )
                raw_connection.execute(
                    "UPDATE alembic_version SET version_num = ?",
                    ("20260726_0019",),
                )
                raw_connection.commit()
            finally:
                raw_connection.close()

            with open_db(db_path) as connection:
                version = connection.execute(select(alembic_version.c.version_num)).scalar_one()
                columns = {str(column["name"]) for column in inspect(connection).get_columns("scan_runs")}
                historical_status = connection.execute(
                    select(scan_runs.c.status).where(
                        scan_runs.c.scan_id == "historical-completed-scan"
                    )
                ).scalar_one()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("status", columns)
            self.assertIn("error", columns)
            self.assertEqual(historical_status, "completed")

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
