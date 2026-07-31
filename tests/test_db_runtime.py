import json
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
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
    LegacySQLiteMigrationSource,
    MediaforceRuntimeBusyError,
    exclusive_legacy_sqlite_migration_source,
    exclusive_mediaforce_runtime_lock,
    reserve_mediaforce_database_identity,
)
from mediaforce.web import runtime_lock as runtime_lock_module

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

    def test_migrate_config_state_gates_wal_writes_before_snapshot(self) -> None:
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
            real_connect = sqlite3.connect
            writer_connection = real_connect(source_path)
            writer_committed = False

            class CommitBeforeWriteGate:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                @property
                def in_transaction(self) -> bool:
                    return self.connection.in_transaction

                def execute(self, statement: str) -> sqlite3.Cursor:
                    nonlocal writer_committed
                    if statement == "BEGIN IMMEDIATE" and not writer_committed:
                        writer_connection.execute(
                            "INSERT INTO migration_rows VALUES ('gate-boundary-row')"
                        )
                        writer_connection.commit()
                        writer_committed = True
                    return self.connection.execute(statement)

                def backup(self, target: sqlite3.Connection) -> None:
                    self.connection.backup(target)

                def rollback(self) -> None:
                    self.connection.rollback()

                def close(self) -> None:
                    self.connection.close()

            try:
                self.assertEqual(
                    writer_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                writer_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                writer_connection.execute(
                    "INSERT INTO migration_rows VALUES ('initial-row')"
                )
                writer_connection.commit()
                def connect_with_gate_commit(
                        database: str | Path,
                        *args: object,
                        **kwargs: object,
                ) -> sqlite3.Connection | CommitBeforeWriteGate:
                    connection = real_connect(database, *args, **kwargs)
                    database_text = os.fspath(database)
                    if (
                        isinstance(database_text, str)
                        and database_text.startswith("file:")
                        and "mode=rw" in database_text
                    ):
                        return CommitBeforeWriteGate(connection)
                    return connection

                with (
                    patch.object(
                        config_module.sqlite3,
                        "connect",
                        side_effect=connect_with_gate_commit,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-wal-gate-order"},
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(writer_committed)
                with real_connect(destination_path) as destination_connection:
                    self.assertEqual(
                        destination_connection.execute(
                            "SELECT value FROM migration_rows ORDER BY rowid"
                        ).fetchall(),
                        [("initial-row",), ("gate-boundary-row",)],
                    )
                self.assertFalse(source_path.exists())
            finally:
                writer_connection.close()

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

    def test_migrate_config_state_removes_untouched_sidecar_reservations_on_failure(
            self,
    ) -> None:
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

            with (
                patch.object(
                    config_module,
                    "_create_legacy_sqlite_staging_path",
                    side_effect=OSError("simulated pre-publication failure"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sidecar-reservation-rollback"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated pre-publication failure",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{source_path}{suffix}").exists())

    def test_legacy_sidecar_reservation_rejects_post_create_path_swap(self) -> None:
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
            wal_path = Path(f"{source_path}-wal")
            displaced_wal_path = Path(f"{source_path}-wal.created")
            real_open = os.open
            swapped = False

            def open_then_swap_created_wal(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if dir_fd is None:
                    descriptor = real_open(path, flags, mode)
                else:
                    descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    not swapped
                    and path == f"{source_path.name}-wal"
                    and flags & os.O_CREAT
                    and flags & os.O_EXCL
                    and dir_fd is not None
                ):
                    os.rename(
                        path,
                        displaced_wal_path.name,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    replacement_descriptor = real_open(
                        path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(replacement_descriptor, b"replacement-wal")
                    finally:
                        os.close(replacement_descriptor)
                    swapped = True
                return descriptor

            with (
                patch(
                    "mediaforce.web.runtime_lock.os.open",
                    side_effect=open_then_swap_created_wal,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sidecar-create-swap"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "identity monitoring is unavailable",
                ),
            ):
                with exclusive_legacy_sqlite_migration_source(
                    config,
                    source_path,
                ):
                    pass

            self.assertTrue(swapped)
            self.assertEqual(wal_path.read_bytes(), b"replacement-wal")
            self.assertTrue(displaced_wal_path.is_file())

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

    def test_migrate_config_state_rejects_transient_replacement_during_sqlite_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            replacement_path = source_path.parent / "replacement.sqlite3"
            original_path = source_path.parent / ".original.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
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
            real_connect = sqlite3.connect
            source_connection_count = 0

            def connect_with_transient_replacement(
                    database: str | Path,
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                nonlocal source_connection_count
                database_text = os.fspath(database)
                if isinstance(database_text, str) and database_text.startswith("file:"):
                    source_connection_count += 1
                    if source_connection_count == 2:
                        source_path.replace(original_path)
                        replacement_path.replace(source_path)
                        try:
                            return real_connect(database, *args, **kwargs)
                        finally:
                            source_path.replace(replacement_path)
                            original_path.replace(source_path)
                return real_connect(database, *args, **kwargs)

            with patch.object(
                config_module.sqlite3,
                "connect",
                side_effect=connect_with_transient_replacement,
            ):
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-open-replacement-probe"},
                ):
                    with self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "identity changed",
                    ):
                        migrate_config_state(config)

            with sqlite3.connect(source_path) as source_connection:
                self.assertEqual(
                    source_connection.execute(
                        "SELECT value FROM source_rows"
                    ).fetchall(),
                    [("original",)],
                )
            with sqlite3.connect(replacement_path) as replacement_connection:
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

    def test_legacy_migration_source_detects_transient_sidecar_replacement(self) -> None:
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp_dir:
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
                    sidecar_path = Path(f"{source_path}{suffix}")
                    self.assertTrue(sidecar_path.is_file())
                    replacement_path = sidecar_path.with_name(
                        f"{sidecar_path.name}.replacement"
                    )
                    original_path = sidecar_path.with_name(
                        f"{sidecar_path.name}.original"
                    )
                    replacement_path.write_bytes(sidecar_path.read_bytes())

                    with exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-sidecar-replacement-probe"},
                    ):
                        with exclusive_legacy_sqlite_migration_source(
                            config,
                            source_path,
                        ) as locked_source:
                            locked_source.prepare_sqlite_sidecars_for_write_gate()
                            gate_connection = sqlite3.connect(
                                locked_source.sqlite_uri(),
                                uri=True,
                                timeout=0,
                                isolation_level=None,
                            )
                            try:
                                gate_connection.execute("BEGIN IMMEDIATE")
                                locked_source.bind_sqlite_sidecars()
                                sidecar_path.replace(original_path)
                                replacement_path.replace(sidecar_path)
                                sidecar_path.replace(replacement_path)
                                original_path.replace(sidecar_path)
                                with self.assertRaisesRegex(
                                    MediaforceRuntimeBusyError,
                                    "identity changed",
                                ):
                                    locked_source.assert_stable()
                            finally:
                                if gate_connection.in_transaction:
                                    gate_connection.rollback()
                                gate_connection.close()
                finally:
                    source_connection.close()

    def test_legacy_migration_source_rejects_sidecar_swap_before_binding(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp_dir:
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
                sidecar_path = Path(f"{source_path}{suffix}")
                displaced_path = Path(f"{sidecar_path}.displaced")

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sidecar-prebind-swap"},
                ):
                    with exclusive_legacy_sqlite_migration_source(
                        config,
                        source_path,
                    ) as locked_source:
                        locked_source.prepare_sqlite_sidecars_for_write_gate()
                        sidecar_path.rename(displaced_path)
                        sidecar_path.write_bytes(b"")
                        with self.assertRaisesRegex(
                            MediaforceRuntimeBusyError,
                            "identity changed",
                        ):
                            locked_source.sqlite_uri()

    def test_legacy_migration_source_rejects_connection_to_other_database(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            other_path = root / "other.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            sqlite3.connect(source_path).close()
            sqlite3.connect(other_path).close()

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-connection-identity"},
                ),
                exclusive_legacy_sqlite_migration_source(
                    config,
                    source_path,
                ) as locked_source,
                sqlite3.connect(other_path) as other_connection,
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "unexpected database",
                ),
            ):
                locked_source.assert_connection_bound(other_connection)

    def test_migrate_config_state_rejects_wal_replacement_before_write_gate_open(self) -> None:
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
            original_wal_path = Path(f"{source_path}-wal.original")
            replacement_wal_path = Path(f"{source_path}-wal.replacement")
            wal_path = Path(f"{source_path}-wal")
            real_connect = sqlite3.connect
            replaced = False

            def connect_after_wal_replacement(
                    database: str | Path,
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                nonlocal replaced
                database_text = os.fspath(database)
                if (
                    not replaced
                    and isinstance(database_text, str)
                    and database_text.startswith("file:")
                    and "mode=rw" in database_text
                ):
                    replacement_wal_path.write_bytes(wal_path.read_bytes())
                    wal_path.rename(original_wal_path)
                    replacement_wal_path.rename(wal_path)
                    replaced = True
                return real_connect(database, *args, **kwargs)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute("CREATE TABLE migration_rows (value TEXT)")
                source_connection.execute("INSERT INTO migration_rows VALUES ('row')")
                source_connection.commit()

                with (
                    patch.object(
                        config_module.sqlite3,
                        "connect",
                        side_effect=connect_after_wal_replacement,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-pre-gate-wal-replacement"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "sidecar identity changed",
                    ),
                ):
                    migrate_config_state(config)
            finally:
                source_connection.close()
                if original_wal_path.exists():
                    try:
                        wal_path.unlink()
                    except FileNotFoundError:
                        pass
                    original_wal_path.rename(wal_path)

    def test_migrate_config_state_resumes_after_destination_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_parent = root / "configured-state.resume-displaced"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute("INSERT INTO migration_rows VALUES ('resumable-row')")

            with (
                patch(
                    "mediaforce.web.runtime_lock.LegacySQLiteMigrationSource.discard_after_publish",
                    side_effect=OSError("simulated interruption after publication"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-publication-interruption"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after publication",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())
            intent_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(intent_payload["schema_version"], 3)
            self.assertEqual(intent_payload["phase"], "cleaning")
            self.assertEqual(
                set(intent_payload["source_sidecar_snapshots"]),
                {"-wal", "-shm", "-journal"},
            )
            self.assertEqual(
                len(list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                ))),
                1,
            )

            real_discard = LegacySQLiteMigrationSource.discard_after_publish

            def swap_parent_then_discard(
                    locked_source: LegacySQLiteMigrationSource,
                    **kwargs: object,
            ) -> None:
                destination_path.parent.rename(displaced_parent)
                destination_path.parent.mkdir()
                real_discard(locked_source, **kwargs)

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    new=swap_parent_then_discard,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-publication-resume-swap"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            destination_path.parent.rmdir()
            displaced_parent.rename(destination_path.parent)

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-publication-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(intent_path.exists())
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                [],
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("resumable-row",)],
                )

    def test_migrate_config_state_resumes_completed_v2_source_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('v2-cleanup-row')"
                )
            real_discard = LegacySQLiteMigrationSource.discard_after_publish

            def discard_then_interrupt(
                    locked_source: LegacySQLiteMigrationSource,
                    **kwargs: object,
            ) -> None:
                real_discard(locked_source, **kwargs)
                raise OSError("simulated interruption after source cleanup")

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    new=discard_then_interrupt,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-v2-cleanup-setup"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after source cleanup",
                ),
            ):
                migrate_config_state(config)

            v3_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            v2_payload = dict(v3_payload)
            v2_payload["schema_version"] = 2
            v2_payload["phase"] = "ready"
            v2_payload.pop("source_sidecar_snapshots")
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=v3_payload,
                payload=v2_payload,
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-v2-cleanup-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(intent_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("v2-cleanup-row",)],
                )

    def test_migrate_config_state_preserves_v2_residual_sidecar_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('v2-residual-row')"
                )
            real_discard = LegacySQLiteMigrationSource.discard_after_publish

            def discard_then_interrupt(
                    locked_source: LegacySQLiteMigrationSource,
                    **kwargs: object,
            ) -> None:
                real_discard(locked_source, **kwargs)
                raise OSError("simulated interruption after source cleanup")

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    new=discard_then_interrupt,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-v2-residual-setup"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after source cleanup",
                ),
            ):
                migrate_config_state(config)

            v3_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            v2_payload = dict(v3_payload)
            v2_payload["schema_version"] = 2
            v2_payload["phase"] = "ready"
            v2_payload.pop("source_sidecar_snapshots")
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=v3_payload,
                payload=v2_payload,
            )
            residual_wal = Path(f"{source_path}-wal")
            residual_quarantine = source_path.parent / (
                f".{source_path.name}-shm.mediaforce-retired-legacy"
            )
            residual_wal.write_bytes(b"unmanifested-v2-wal")
            residual_quarantine.write_bytes(b"unmanifested-v2-quarantine")

            with (
                self.assertLogs(config_module.LOGGER, level="WARNING") as logs,
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-v2-residual-resume"},
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(any(
                "Retained legacy SQLite v2 sidecar artifacts" in message
                for message in logs.output
            ))
            self.assertFalse(source_path.exists())
            self.assertFalse(intent_path.exists())
            self.assertEqual(residual_wal.read_bytes(), b"unmanifested-v2-wal")
            self.assertEqual(
                residual_quarantine.read_bytes(),
                b"unmanifested-v2-quarantine",
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("v2-residual-row",)],
                )

    def test_migrate_config_state_rejects_destination_parent_swap_before_cleanup(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_parent = root / "configured-state.displaced"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('parent-bound-row')"
                )
            real_discard = LegacySQLiteMigrationSource.discard_after_publish
            parent_swapped = False

            def swap_parent_then_discard(
                    locked_source: LegacySQLiteMigrationSource,
                    **kwargs: object,
            ) -> None:
                nonlocal parent_swapped
                destination_path.parent.rename(displaced_parent)
                destination_path.parent.mkdir()
                parent_swapped = True
                real_discard(locked_source, **kwargs)

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    new=swap_parent_then_discard,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-destination-parent-swap"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "destination parent changed",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(parent_swapped)
            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertTrue((displaced_parent / destination_path.name).is_file())

            destination_path.parent.rmdir()
            displaced_parent.rename(destination_path.parent)
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-destination-parent-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("parent-bound-row",)],
                )

    def test_migrate_config_state_rechecks_source_parent_before_intent_removal(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_parent = root / "state.displaced"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('source-parent-row')"
                )
            destination_type = config_module._LegacySQLiteMigrationDestination
            real_discard_staging = destination_type.discard_staging
            replacement_marker = source_path.parent / "replacement-marker"
            parent_swapped = False

            def swap_source_parent_then_discard_staging(
                    binding: object,
                    staging_name: str,
                    expected: dict[str, object],
            ) -> None:
                nonlocal parent_swapped
                source_path.parent.rename(displaced_parent)
                source_path.parent.mkdir()
                replacement_marker.write_bytes(b"replacement-parent")
                parent_swapped = True
                real_discard_staging(binding, staging_name, expected)

            with (
                patch.object(
                    destination_type,
                    "discard_staging",
                    new=swap_source_parent_then_discard_staging,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-source-parent-finalization-swap"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source parent changed during migration",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(parent_swapped)
            self.assertEqual(replacement_marker.read_bytes(), b"replacement-parent")
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                [],
            )

            replacement_marker.unlink()
            source_path.parent.rmdir()
            displaced_parent.rename(source_path.parent)
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-source-parent-finalization-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(intent_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("source-parent-row",)],
                )

    def test_migrate_config_state_publish_rollback_preserves_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_destination = (
                destination_path.parent / "library.sqlite3.published"
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('rollback-row')"
                )
            destination_type = config_module._LegacySQLiteMigrationDestination
            real_assert_file_matches = destination_type.assert_file_matches
            replaced = False

            def replace_destination_then_assert(
                    binding: object,
                    name: str,
                    expected: dict[str, object],
                    *,
                    allowed_link_counts: set[int],
            ) -> None:
                nonlocal replaced
                if name == destination_path.name and not replaced:
                    destination_path.rename(displaced_destination)
                    destination_path.write_bytes(b"replacement-destination")
                    replaced = True
                real_assert_file_matches(
                    binding,
                    name,
                    expected,
                    allowed_link_counts=allowed_link_counts,
                )

            with (
                patch.object(
                    destination_type,
                    "assert_file_matches",
                    new=replace_destination_then_assert,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-publication-rollback-replacement"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "destination identity changed",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(replaced)
            self.assertTrue(source_path.is_file())
            self.assertEqual(
                destination_path.read_bytes(),
                b"replacement-destination",
            )
            self.assertTrue(displaced_destination.is_file())

    def test_migrate_config_state_resumes_after_main_retirement_before_sidecars(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            source_connection = sqlite3.connect(source_path)
            real_unlink = os.unlink
            interrupted = False

            def interrupt_before_sidecar_cleanup(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    *,
                    dir_fd: int | None = None,
            ) -> None:
                nonlocal interrupted
                if (
                    str(path).startswith(
                        f".{source_path.name}-wal.mediaforce-retired-"
                    )
                    and not interrupted
                ):
                    interrupted = True
                    raise OSError("simulated interruption before sidecar cleanup")
                if dir_fd is None:
                    real_unlink(path)
                else:
                    real_unlink(path, dir_fd=dir_fd)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('retired-main-row')"
                )
                source_connection.commit()
                self.assertTrue(Path(f"{source_path}-wal").is_file())

                with (
                    patch(
                        "mediaforce.web.runtime_lock.os.unlink",
                        side_effect=interrupt_before_sidecar_cleanup,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-main-retirement-interruption"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(interrupted)
                self.assertFalse(source_path.exists())
                self.assertFalse(Path(f"{source_path}-wal").exists())
                self.assertEqual(
                    len(list(source_path.parent.glob(
                        f".{source_path.name}-wal.mediaforce-retired-*"
                    ))),
                    1,
                )
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-main-retirement-resume"},
                ):
                    migrate_config_state(config)

                self.assertFalse(intent_path.exists())
                for suffix in ("-wal", "-shm", "-journal"):
                    self.assertFalse(Path(f"{source_path}{suffix}").exists())
                with sqlite3.connect(destination_path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM migration_rows"
                        ).fetchall(),
                        [("retired-main-row",)],
                    )
            finally:
                source_connection.close()

    def test_migrate_config_state_preserves_replaced_sidecar_after_main_retirement(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            source_connection = sqlite3.connect(source_path)
            real_rename_exclusive = runtime_lock_module.rename_exclusive
            interrupted = False

            def interrupt_before_sidecar_cleanup(
                    **kwargs: object,
            ) -> None:
                nonlocal interrupted
                if (
                    kwargs.get("source_name") == f"{source_path.name}-wal"
                    and not interrupted
                ):
                    interrupted = True
                    raise OSError("simulated interruption before sidecar cleanup")
                real_rename_exclusive(**kwargs)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('preserved-sidecar-row')"
                )
                source_connection.commit()

                with (
                    patch(
                        "mediaforce.web.runtime_lock.rename_exclusive",
                        new=interrupt_before_sidecar_cleanup,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-sidecar-replacement-setup"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                wal_path = Path(f"{source_path}-wal")
                original_wal_path = Path(f"{source_path}-wal.original")
                wal_path.rename(original_wal_path)
                wal_path.write_bytes(b"replacement-sidecar")

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-sidecar-replacement-resume"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertFalse(source_path.exists())
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())
                self.assertEqual(wal_path.read_bytes(), b"replacement-sidecar")
                self.assertTrue(original_wal_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_replaced_sidecar_before_main_retirement(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            source_connection = sqlite3.connect(source_path)

            def interrupt_before_source_retirement(
                    _locked_source: LegacySQLiteMigrationSource,
                    **_kwargs: object,
            ) -> None:
                raise OSError("simulated interruption before source retirement")

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('cleaning-lineage-row')"
                )
                source_connection.commit()

                with (
                    patch.object(
                        LegacySQLiteMigrationSource,
                        "discard_after_publish",
                        new=interrupt_before_source_retirement,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-cleaning-lineage-setup"},
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "simulated interruption before source retirement",
                    ),
                ):
                    migrate_config_state(config)

                payload = json.loads(intent_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["phase"], "cleaning")
                wal_path = Path(f"{source_path}-wal")
                original_wal_path = Path(f"{source_path}-wal.original")
                wal_bytes = wal_path.read_bytes()
                wal_path.rename(original_wal_path)
                wal_path.write_bytes(wal_bytes)

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-cleaning-lineage-resume"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(source_path.is_file())
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())
                self.assertEqual(wal_path.read_bytes(), wal_bytes)
                self.assertTrue(original_wal_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_sidecar_swap_during_initial_cleanup(
            self,
    ) -> None:
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
            wal_path = Path(f"{source_path}-wal")
            original_wal_path = Path(f"{source_path}-wal.original")
            real_assert_sidecar_bound = (
                runtime_lock_module._LegacySQLiteMutationGuard.assert_sidecar_bound
            )
            replaced = False

            def replace_wal_then_assert(
                    guard: object,
                    suffix: str,
            ) -> None:
                nonlocal replaced
                if suffix == "-wal" and not replaced:
                    wal_path.rename(original_wal_path)
                    wal_path.write_bytes(b"initial-cleanup-replacement")
                    replaced = True
                real_assert_sidecar_bound(guard, suffix)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('initial-cleanup-row')"
                )
                source_connection.commit()

                with (
                    patch.object(
                        runtime_lock_module._LegacySQLiteMutationGuard,
                        "assert_sidecar_bound",
                        new=replace_wal_then_assert,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-initial-sidecar-swap"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(replaced)
                self.assertFalse(source_path.exists())
                self.assertTrue(destination_path.is_file())
                self.assertEqual(
                    wal_path.read_bytes(),
                    b"initial-cleanup-replacement",
                )
                self.assertTrue(original_wal_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_same_inode_sidecar_mutation_after_manifest(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            source_connection = sqlite3.connect(source_path)
            wal_path = Path(f"{source_path}-wal")
            real_retire_sidecar = (
                runtime_lock_module._LegacySQLiteMutationGuard.retire_sidecar
            )
            mutated = False

            def mutate_wal_then_retire(
                    guard: object,
                    suffix: str,
                    *,
                    expected_snapshot: tuple[int, int, int, int, int, int, int, int],
            ) -> None:
                nonlocal mutated
                if suffix == "-wal" and not mutated:
                    original_inode = wal_path.stat().st_ino
                    with wal_path.open("ab") as wal_file:
                        wal_file.write(b"same-inode-mutation")
                    self.assertEqual(wal_path.stat().st_ino, original_inode)
                    mutated = True
                real_retire_sidecar(
                    guard,
                    suffix,
                    expected_snapshot=expected_snapshot,
                )

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('same-inode-row')"
                )
                source_connection.commit()

                with (
                    patch.object(
                        runtime_lock_module._LegacySQLiteMutationGuard,
                        "retire_sidecar",
                        new=mutate_wal_then_retire,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-sidecar-same-inode-mutation"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(mutated)
                self.assertFalse(source_path.exists())
                self.assertTrue(wal_path.is_file())
                self.assertTrue(intent_path.is_file())
                self.assertTrue(destination_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_source_recreation_before_intent_removal(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('recreated-source-row')"
                )
            real_discard = LegacySQLiteMigrationSource.discard_after_publish
            recreated = False

            def discard_then_recreate(
                    locked_source: LegacySQLiteMigrationSource,
                    **kwargs: object,
            ) -> None:
                nonlocal recreated
                real_discard(locked_source, **kwargs)
                source_path.write_bytes(b"replacement-source")
                recreated = True

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    new=discard_then_recreate,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-source-recreation"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source cleanup is incomplete",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(recreated)
            self.assertEqual(source_path.read_bytes(), b"replacement-source")
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_preserves_sidecar_swapped_during_quarantine_claim(
            self,
    ) -> None:
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
            wal_path = Path(f"{source_path}-wal")
            original_wal_path = Path(f"{source_path}-wal.original")
            real_rename_exclusive = runtime_lock_module.rename_exclusive
            replaced = False

            def replace_wal_during_claim(**kwargs: object) -> None:
                nonlocal replaced
                if (
                    kwargs.get("source_name") == f"{source_path.name}-wal"
                    and not replaced
                ):
                    wal_path.rename(original_wal_path)
                    wal_path.write_bytes(b"quarantine-claim-replacement")
                    replaced = True
                real_rename_exclusive(**kwargs)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('quarantine-claim-row')"
                )
                source_connection.commit()

                with (
                    patch(
                        "mediaforce.web.runtime_lock.rename_exclusive",
                        new=replace_wal_during_claim,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-quarantine-claim-swap"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(replaced)
                self.assertFalse(source_path.exists())
                self.assertTrue(destination_path.is_file())
                self.assertFalse(wal_path.exists())
                quarantined_wal_paths = list(source_path.parent.glob(
                    f".{wal_path.name}.mediaforce-retired-*"
                ))
                self.assertEqual(len(quarantined_wal_paths), 1)
                self.assertEqual(
                    quarantined_wal_paths[0].read_bytes(),
                    b"quarantine-claim-replacement",
                )
                self.assertTrue(original_wal_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_preserves_resumed_sidecar_swap_during_claim(
            self,
    ) -> None:
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
            wal_path = Path(f"{source_path}-wal")
            original_wal_path = Path(f"{source_path}-wal.original")
            runtime_rename_exclusive = runtime_lock_module.rename_exclusive
            config_rename_exclusive = config_module.rename_exclusive
            interrupted = False
            replaced = False

            def interrupt_before_wal_claim(**kwargs: object) -> None:
                nonlocal interrupted
                if (
                    kwargs.get("source_name") == f"{source_path.name}-wal"
                    and not interrupted
                ):
                    interrupted = True
                    raise OSError("simulated interruption before WAL claim")
                runtime_rename_exclusive(**kwargs)

            def replace_wal_during_resumed_claim(**kwargs: object) -> None:
                nonlocal replaced
                if (
                    kwargs.get("source_name") == f"{source_path.name}-wal"
                    and not replaced
                ):
                    wal_path.rename(original_wal_path)
                    wal_path.write_bytes(b"resumed-claim-replacement")
                    replaced = True
                config_rename_exclusive(**kwargs)

            try:
                self.assertEqual(
                    source_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                source_connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                source_connection.execute(
                    "INSERT INTO migration_rows VALUES ('resumed-claim-row')"
                )
                source_connection.commit()

                with (
                    patch(
                        "mediaforce.web.runtime_lock.rename_exclusive",
                        new=interrupt_before_wal_claim,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-resumed-claim-setup"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(interrupted)
                self.assertFalse(source_path.exists())
                self.assertTrue(wal_path.is_file())

                with (
                    patch.object(
                        config_module,
                        "rename_exclusive",
                        new=replace_wal_during_resumed_claim,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-resumed-claim-swap"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(replaced)
                self.assertFalse(wal_path.exists())
                quarantined_wal_paths = list(source_path.parent.glob(
                    f".{wal_path.name}.mediaforce-retired-*"
                ))
                self.assertEqual(len(quarantined_wal_paths), 1)
                self.assertEqual(
                    quarantined_wal_paths[0].read_bytes(),
                    b"resumed-claim-replacement",
                )
                self.assertTrue(original_wal_path.is_file())
            finally:
                source_connection.close()

    def test_migrate_config_state_resumes_after_main_quarantine_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('main-quarantine-row')"
                )
            real_unlink = os.unlink
            interrupted = False

            def interrupt_main_quarantine_cleanup(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    *,
                    dir_fd: int | None = None,
            ) -> None:
                nonlocal interrupted
                if (
                    str(path).startswith(
                        f".{source_path.name}.mediaforce-retired-"
                    )
                    and not interrupted
                ):
                    interrupted = True
                    raise OSError("simulated interruption after main claim")
                if dir_fd is None:
                    real_unlink(path)
                else:
                    real_unlink(path, dir_fd=dir_fd)

            with (
                patch(
                    "mediaforce.web.runtime_lock.os.unlink",
                    side_effect=interrupt_main_quarantine_cleanup,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-main-quarantine-setup"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source cleanup failed after publication",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(interrupted)
            self.assertFalse(source_path.exists())
            self.assertTrue(intent_path.is_file())
            self.assertEqual(
                len(list(source_path.parent.glob(
                    f".{source_path.name}.mediaforce-retired-*"
                ))),
                1,
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-main-quarantine-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(intent_path.exists())
            self.assertEqual(
                list(source_path.parent.glob("*.mediaforce-retired-*")),
                [],
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("main-quarantine-row",)],
                )

    def test_migrate_config_state_rejects_unowned_cleanup_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('quarantine-owner-row')"
                )
            destination_type = config_module._LegacySQLiteMigrationDestination

            with (
                patch.object(
                    destination_type,
                    "discard_staging",
                    side_effect=OSError("simulated interruption after source cleanup"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-unowned-quarantine-setup"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after source cleanup",
                ),
            ):
                migrate_config_state(config)

            unowned_quarantine = source_path.parent / (
                f".{source_path.name}-journal.mediaforce-retired-unowned"
            )
            unowned_quarantine.write_bytes(b"unowned-quarantine")

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-unowned-quarantine-resume"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertEqual(
                unowned_quarantine.read_bytes(),
                b"unowned-quarantine",
            )
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_resumes_after_each_sidecar_cleanup_boundary(
            self,
    ) -> None:
        for interrupted_suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=interrupted_suffix), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name=f"target-{interrupted_suffix[1:]}",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                    connection.execute(
                        "INSERT INTO migration_rows VALUES (?)",
                        (interrupted_suffix,),
                    )
                real_unlink = os.unlink
                interrupted = False

                def interrupt_sidecar_cleanup(
                        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                        *,
                        dir_fd: int | None = None,
                ) -> None:
                    nonlocal interrupted
                    if (
                        str(path).startswith(
                            f".{source_path.name}{interrupted_suffix}.mediaforce-retired-"
                        )
                        and not interrupted
                    ):
                        interrupted = True
                        raise OSError("simulated sidecar cleanup interruption")
                    if dir_fd is None:
                        real_unlink(path)
                    else:
                        real_unlink(path, dir_fd=dir_fd)

                with (
                    patch(
                        "mediaforce.web.runtime_lock.os.unlink",
                        side_effect=interrupt_sidecar_cleanup,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-sidecar-cleanup-boundary"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup failed after publication",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(interrupted)
                self.assertFalse(source_path.exists())
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sidecar-cleanup-resume"},
                ):
                    migrate_config_state(config)

                for suffix in ("-wal", "-shm", "-journal"):
                    self.assertFalse(Path(f"{source_path}{suffix}").exists())
                with sqlite3.connect(destination_path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM migration_rows"
                        ).fetchall(),
                        [(interrupted_suffix,)],
                    )

    def test_migrate_config_state_retries_visible_unsynced_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute("INSERT INTO migration_rows VALUES ('intent-retry-row')")
            real_fsync_directory = config_module._fsync_directory
            failed = False

            def fail_after_intent_publication(path: Path) -> None:
                nonlocal failed
                if not failed and intent_path.exists() and not destination_path.exists():
                    failed = True
                    raise OSError("simulated intent directory sync failure")
                real_fsync_directory(path)

            with (
                patch.object(
                    config_module,
                    "_fsync_directory",
                    side_effect=fail_after_intent_publication,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-intent-sync-failure"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated intent directory sync failure",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertTrue(intent_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                [],
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-intent-sync-retry"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(intent_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("intent-retry-row",)],
                )

    def test_migrate_config_state_rejects_diverged_source_during_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute("INSERT INTO migration_rows VALUES ('published-row')")

            with (
                patch(
                    "mediaforce.web.runtime_lock.LegacySQLiteMigrationSource.discard_after_publish",
                    side_effect=OSError("simulated interruption after publication"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-divergence-interruption"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after publication",
                ),
            ):
                migrate_config_state(config)

            with sqlite3.connect(source_path) as connection:
                connection.execute("INSERT INTO migration_rows VALUES ('later-row')")

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-divergence-resume"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())
            self.assertEqual(
                len(list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                ))),
                1,
            )

    def test_migrate_config_state_rejects_metadata_divergence_during_resume(self) -> None:
        for pragma, value in (("user_version", 17), ("application_id", 42)):
            with self.subTest(pragma=pragma), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                intent_path = config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                )
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name="target",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                    connection.execute("INSERT INTO migration_rows VALUES ('row')")

                with (
                    patch(
                        "mediaforce.web.runtime_lock.LegacySQLiteMigrationSource.discard_after_publish",
                        side_effect=OSError("simulated interruption after publication"),
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-metadata-interruption"},
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "simulated interruption after publication",
                    ),
                ):
                    migrate_config_state(config)

                with sqlite3.connect(source_path) as connection:
                    connection.execute(f"PRAGMA {pragma}={value}")

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-metadata-resume"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(source_path.is_file())
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_rejects_replaced_source_parent_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            sqlite3.connect(source_path).close()

            with (
                patch(
                    "mediaforce.web.runtime_lock.LegacySQLiteMigrationSource.discard_after_publish",
                    side_effect=OSError("simulated interruption after publication"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-parent-interruption"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated interruption after publication",
                ),
            ):
                migrate_config_state(config)

            for candidate in source_path.parent.glob("library.sqlite3*"):
                candidate.unlink()
            displaced_parent = root / "displaced-state"
            source_path.parent.rename(displaced_parent)
            source_path.parent.mkdir()
            decoy = source_path.parent / "library.sqlite3-journal"
            decoy.write_bytes(b"unrelated")

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-parent-resume"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertEqual(decoy.read_bytes(), b"unrelated")
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_resumes_pre_copy_intent_without_orphan_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            destination_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute("INSERT INTO migration_rows VALUES ('row')")

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-pre-copy-fixture"},
            ):
                with exclusive_legacy_sqlite_migration_source(
                    config,
                    source_path,
                ) as locked_source:
                    staging_path = config_module._reserved_legacy_sqlite_staging_path(
                        destination_path
                    )
                    payload = config_module._legacy_sqlite_migration_intent_payload(
                        locked_source=locked_source,
                        source=source_path,
                        destination=destination_path,
                        staging_path=staging_path,
                        staging_snapshot=None,
                    )
                    config_module._write_legacy_sqlite_migration_intent(
                        intent_path,
                        payload,
                    )
                    staging_path.write_bytes(b"partial backup")

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-pre-copy-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertTrue(destination_path.is_file())
            self.assertFalse(intent_path.exists())
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                [],
            )

    def test_migrate_config_state_removes_persisted_rollback_journal(self) -> None:
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
            with sqlite3.connect(source_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode=PERSIST").fetchone(),
                    ("persist",),
                )
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute("INSERT INTO migration_rows VALUES ('row')")
            journal_path = Path(f"{source_path}-journal")
            self.assertTrue(journal_path.is_file())

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-persist-journal"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertTrue(destination_path.is_file())

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
            def assert_destination_is_missing(
                    locked_source: object,
                    staging_path: Path,
            ) -> Iterator[None]:
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

    def test_open_db_backfills_ambiguous_legacy_scan_runs_as_failed(self) -> None:
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
                        "historical-interrupted-scan",
                        "2026-07-30T13:00:00+00:00",
                        None,
                        999999,
                        "2026-07-30T13:01:00+00:00",
                        '["tv"]',
                        "full",
                        None,
                        3,
                        1,
                        2,
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
                historical_error = connection.execute(
                    select(scan_runs.c.error).where(
                        scan_runs.c.scan_id == "historical-completed-scan"
                    )
                ).scalar_one()
                interrupted_row = connection.execute(
                    select(scan_runs.c.status, scan_runs.c.error, scan_runs.c.completed_at).where(
                        scan_runs.c.scan_id == "historical-interrupted-scan"
                    )
                ).mappings().one()

            self.assertEqual(version, CURRENT_DB_REVISION)
            self.assertIn("status", columns)
            self.assertIn("error", columns)
            self.assertEqual(historical_status, "failed")
            self.assertEqual(
                historical_error,
                "Background scan was interrupted by a web process restart.",
            )
            self.assertEqual(interrupted_row["status"], "failed")
            self.assertEqual(
                interrupted_row["error"],
                "Background scan was interrupted by a web process restart.",
            )
            self.assertEqual(interrupted_row["completed_at"], "2026-07-30T13:01:00+00:00")

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
