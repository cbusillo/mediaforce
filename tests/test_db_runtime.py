import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
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

    def test_legacy_cleanup_collects_linux_quarantine_events(self) -> None:
        guard_type = runtime_lock_module._LegacySQLiteMutationGuard
        guard = object.__new__(guard_type)
        guard._darwin_guard = None
        guard._inotify_descriptor = 41
        guard._file_watch = 7
        guard._sidecar_watches = set()
        guard._directory_watch = 11
        guard._path = Path("/tmp/library.sqlite3")
        guard._sidecar_names = (
            "library.sqlite3-wal",
            "library.sqlite3-shm",
            "library.sqlite3-journal",
        )
        guard._watched_directory_names = {"library.sqlite3"}
        quarantine_name = (
            ".library.sqlite3.mediaforce-retired-0123456789abcdef01234567"
        )
        encoded_name = quarantine_name.encode("utf-8") + b"\0"
        event = struct.pack(
            "iIII",
            guard._directory_watch,
            guard_type._IN_CREATE,
            0,
            len(encoded_name),
        ) + encoded_name
        reads = iter((event, BlockingIOError()))

        def read_event(_descriptor: int, _size: int) -> bytes:
            result = next(reads)
            if isinstance(result, BaseException):
                raise result
            return result

        with patch("mediaforce.web.runtime_lock.os.read", side_effect=read_event):
            file_changed, directory_changed, directory_names = (
                guard._drain_mutation_events(
                    collect_all_directory_names=True,
                )
            )

        self.assertFalse(file_changed)
        self.assertTrue(directory_changed)
        self.assertEqual(directory_names, {quarantine_name})
        self.assertTrue(guard._cleanup_events_are_relevant(
            file_changed=file_changed,
            directory_changed=directory_changed,
            directory_names=directory_names,
        ))

    def test_legacy_cleanup_rejects_bare_darwin_events_after_seal(self) -> None:
        guard_type = runtime_lock_module._LegacySQLiteMutationGuard
        guard = object.__new__(guard_type)
        guard._cleanup_sealed = True
        guard._path = Path("/tmp/library.sqlite3")
        guard._sidecar_names = (
            "library.sqlite3-wal",
            "library.sqlite3-shm",
            "library.sqlite3-journal",
        )

        with patch.object(runtime_lock_module.sys, "platform", "darwin"):
            self.assertTrue(guard._cleanup_events_are_relevant(
                file_changed=False,
                directory_changed=True,
                directory_names=set(),
            ))

    def test_legacy_cleanup_ignores_named_unrelated_linux_churn_after_seal(
            self,
    ) -> None:
        guard_type = runtime_lock_module._LegacySQLiteMutationGuard
        guard = object.__new__(guard_type)
        guard._cleanup_sealed = True
        guard._path = Path("/tmp/library.sqlite3")
        guard._sidecar_names = (
            "library.sqlite3-wal",
            "library.sqlite3-shm",
            "library.sqlite3-journal",
        )

        with patch.object(runtime_lock_module.sys, "platform", "linux"):
            self.assertFalse(guard._cleanup_events_are_relevant(
                file_changed=False,
                directory_changed=True,
                directory_names={".DS_Store"},
            ))

    def test_legacy_intent_preparation_uses_bound_destination_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination_path = root / "configured-state" / "library.sqlite3"
            destination_path.parent.mkdir()
            parent_info = destination_path.parent.stat(follow_symlinks=False)
            displaced_parent = root / "configured-state.displaced"
            intent_name = ".library.sqlite3.legacy-migration-intent.json"
            payload = {"phase": "cleaning", "schema": "test"}
            swapped = False

            def write_after_parent_swap(
                    *,
                    directory_descriptor: int,
            ) -> None:
                nonlocal swapped
                destination_path.parent.rename(displaced_parent)
                destination_path.parent.mkdir()
                try:
                    config_module._write_legacy_sqlite_migration_intent_at(
                        directory_descriptor=directory_descriptor,
                        intent_name=intent_name,
                        payload=payload,
                    )
                finally:
                    destination_path.parent.rmdir()
                    displaced_parent.rename(destination_path.parent)
                swapped = True

            with config_module._opened_legacy_sqlite_migration_destination(
                    destination_path,
                    expected_parent_identity=[
                        parent_info.st_dev,
                        parent_info.st_ino,
                    ],
            ) as destination_binding:
                write_after_parent_swap(
                    directory_descriptor=(
                        destination_binding.directory_descriptor
                    ),
                )

            self.assertTrue(swapped)
            self.assertFalse(displaced_parent.exists())
            self.assertEqual(
                json.loads(
                    (destination_path.parent / intent_name).read_text(
                        encoding="utf-8"
                    )
                ),
                payload,
            )

    @staticmethod
    def _legacy_migration_config(
            root: Path,
            *,
            database_path: Path,
            name: str,
            runtime_reservation_dir: Path | None = None,
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
                runtime_reservation_dir=(
                    runtime_reservation_dir
                    if runtime_reservation_dir is not None
                    else runtime_root / "reservations"
                ),
            ),
        )

    @staticmethod
    def _legacy_v4_migration_intent(
            payload: dict[str, object],
    ) -> dict[str, object]:
        legacy_payload = json.loads(json.dumps(payload))
        legacy_payload["schema_version"] = 4
        for key in (
            "cleanup_policy",
            "publication_policy",
            "legacy_cleanup_absent_prefix",
            "retained_cleanup_artifacts",
        ):
            legacy_payload.pop(key)
        return legacy_payload

    @staticmethod
    def _legacy_v3_migration_intent(
            payload: dict[str, object],
    ) -> dict[str, object]:
        legacy_payload = DatabaseRuntimeTests._legacy_v4_migration_intent(
            payload
        )
        legacy_payload["schema_version"] = 3
        legacy_payload.pop("source_main_sha256")
        snapshots = legacy_payload.get("source_sidecar_snapshots")
        if isinstance(snapshots, dict):
            for snapshot in snapshots.values():
                if isinstance(snapshot, dict):
                    snapshot.pop("sha256")
        return legacy_payload

    @staticmethod
    def _legacy_v2_migration_intent(
            payload: dict[str, object],
            *,
            phase: str = "ready",
    ) -> dict[str, object]:
        legacy_payload = DatabaseRuntimeTests._legacy_v4_migration_intent(
            payload
        )
        legacy_payload["schema_version"] = 2
        legacy_payload["phase"] = phase
        legacy_payload.pop("source_main_sha256")
        legacy_payload.pop("source_sidecar_snapshots")
        return legacy_payload

    def _assert_completed_v5_intent(
            self,
            intent_path: Path,
    ) -> dict[str, object]:
        self.assertTrue(intent_path.is_file())
        payload = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["phase"], "complete")
        self.assertIn(
            payload["cleanup_policy"],
            {
                "retain_verified_quarantines",
                "preserve_recorded_legacy_artifacts",
            },
        )
        self.assertIn(
            payload["publication_policy"],
            {
                "exclusive_staging_rename",
                "legacy_hardlink_retained_alias",
                "legacy_hardlink_without_alias",
            },
        )
        self.assertIsInstance(payload["retained_cleanup_artifacts"], list)
        return payload

    def _prepare_interrupted_cleaning_migration(
            self,
            root: Path,
            *,
            name: str,
            value: str,
    ) -> tuple[
        MediaforceConfig,
        Path,
        Path,
        Path,
        dict[str, object],
    ]:
        source_path = root / "state" / "library.sqlite3"
        destination_path = root / "configured-state" / "library.sqlite3"
        intent_path = config_module._legacy_sqlite_migration_intent_path(
            destination_path
        )
        config = self._legacy_migration_config(
            root,
            database_path=destination_path,
            name=name,
        )
        source_path.parent.mkdir()
        with sqlite3.connect(source_path) as connection:
            connection.execute("CREATE TABLE migration_rows (value TEXT)")
            connection.execute(
                "INSERT INTO migration_rows VALUES (?)",
                (value,),
            )
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar_path = Path(f"{source_path}{suffix}")
            sidecar_path.write_bytes(b"")
            sidecar_path.chmod(0o600)
        with (
            patch.object(
                LegacySQLiteMigrationSource,
                "discard_after_publish",
                side_effect=OSError("simulated cleaning interruption"),
            ),
            exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-cleaning-fixture"},
            ),
            self.assertRaisesRegex(OSError, "simulated cleaning interruption"),
        ):
            migrate_config_state(config)
        payload = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["phase"], "cleaning")
        return config, source_path, destination_path, intent_path, payload

    @staticmethod
    def _cleanup_quarantine_path(
            source_path: Path,
            payload: dict[str, object],
            suffix: str,
    ) -> Path:
        if suffix:
            snapshots = payload["source_sidecar_snapshots"]
            assert isinstance(snapshots, dict)
            snapshot = snapshots[suffix]
            assert isinstance(snapshot, dict)
            device = int(snapshot["device"])
            inode = int(snapshot["inode"])
            size = int(snapshot["size"])
            mtime_ns = int(snapshot["mtime_ns"])
        else:
            snapshot = payload["source_main_snapshot"]
            assert isinstance(snapshot, list)
            device, inode, size, mtime_ns = (
                int(snapshot[0]),
                int(snapshot[1]),
                int(snapshot[2]),
                int(snapshot[3]),
            )
        name = f"{source_path.name}{suffix}"
        return source_path.parent / config_module.legacy_sqlite_migration_quarantine_name(
            name,
            device=device,
            inode=inode,
            size=size,
            mtime_ns=mtime_ns,
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
                intent_path = config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                )
                completed = self._assert_completed_v5_intent(intent_path)
                self.assertEqual(
                    completed["publication_policy"],
                    "exclusive_staging_rename",
                )
                retained_names = {
                    artifact["name"]
                    for artifact in completed["retained_cleanup_artifacts"]
                }
                self.assertEqual(len(retained_names), 4)
                self.assertEqual(
                    {
                        path.name
                        for path in source_path.parent.iterdir()
                        if ".mediaforce-retired-" in path.name
                    },
                    retained_names,
                )
                self.assertEqual(
                    list(destination_path.parent.glob(
                        ".library.sqlite3.migration-*.sqlite3"
                    )),
                    [],
                )
            finally:
                source_connection.close()

    def test_migrate_config_state_rejects_destination_appearing_after_source_lock(
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
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('legacy-source')"
                )

            real_source_lock = exclusive_legacy_sqlite_migration_source

            @contextmanager
            def create_destination_after_source_lock(
                    locked_config: MediaforceConfig,
                    locked_source_path: Path,
            ) -> Iterator[LegacySQLiteMigrationSource]:
                with real_source_lock(
                    locked_config,
                    locked_source_path,
                ) as locked_source:
                    destination_path.parent.mkdir(exist_ok=True)
                    with sqlite3.connect(destination_path) as connection:
                        connection.execute(
                            "CREATE TABLE replacement_rows (value TEXT)"
                        )
                        connection.execute(
                            "INSERT INTO replacement_rows VALUES ('replacement')"
                        )
                    yield locked_source

            with (
                patch.object(
                    runtime_lock_module,
                    "exclusive_legacy_sqlite_migration_source",
                    new=create_destination_after_source_lock,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-post-lock-destination-race"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "destination appeared after the legacy source was locked",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(
                config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                ).exists()
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM replacement_rows"
                    ).fetchall(),
                    [("replacement",)],
                )

    def test_migrate_config_state_completed_intent_allows_destination_writes(self) -> None:
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
                connection.execute("CREATE TABLE migration_rows (value TEXT NOT NULL)")
                connection.execute("INSERT INTO migration_rows VALUES ('migrated-row')")

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-complete-mutable-setup"},
            ):
                migrate_config_state(config)

            destination_inode = destination_path.stat().st_ino
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                connection.execute("INSERT INTO migration_rows VALUES ('runtime-row')")
                connection.commit()
                self.assertTrue(Path(f"{destination_path}-wal").is_file())
                self.assertTrue(Path(f"{destination_path}-shm").is_file())
                self.assertEqual(destination_path.stat().st_ino, destination_inode)
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-complete-mutable-restart"},
                ):
                    migrate_config_state(config)

            self.assertEqual(destination_path.stat().st_ino, destination_inode)
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows ORDER BY rowid").fetchall(),
                    [("migrated-row",), ("runtime-row",)],
                )

    def test_migrate_config_state_completed_intent_rejects_destination_replacement(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_path = destination_path.with_suffix(".displaced.sqlite3")
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT NOT NULL)")

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-complete-replacement-setup"},
            ):
                migrate_config_state(config)

            destination_path.rename(displaced_path)
            with sqlite3.connect(destination_path) as connection:
                connection.execute("CREATE TABLE replacement_rows (value TEXT)")

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-complete-replacement-restart"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(displaced_path.is_file())
            self.assertTrue(destination_path.is_file())

    def test_migrate_config_state_completed_intent_rejects_unsafe_sidecars(self) -> None:
        for sidecar_kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=sidecar_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                sidecar_path = Path(f"{destination_path}-wal")
                target_path = root / f"{sidecar_kind}-target"
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name=f"target-{sidecar_kind}",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-unsafe-sidecar-setup"},
                ):
                    migrate_config_state(config)

                if sidecar_kind == "symlink":
                    target_path.write_bytes(b"symlink-target")
                    sidecar_path.symlink_to(target_path)
                elif sidecar_kind == "hardlink":
                    target_path.write_bytes(b"hardlink-target")
                    os.link(target_path, sidecar_path)
                elif sidecar_kind == "fifo":
                    os.mkfifo(sidecar_path, 0o600)
                else:
                    sidecar_path.mkdir()

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-unsafe-sidecar-restart"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(os.path.lexists(sidecar_path))
                if target_path.exists():
                    self.assertEqual(
                        target_path.read_bytes(),
                        f"{sidecar_kind}-target".encode("utf-8"),
                    )

    def test_migrate_config_state_sealing_intent_rejects_destination_writes(self) -> None:
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
                connection.execute("INSERT INTO migration_rows VALUES ('migrated-row')")

            with (
                patch.object(
                    config_module,
                    "_legacy_sqlite_migration_sealed_payload",
                    side_effect=OSError("simulated sealing interruption"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sealing-interruption"},
                ),
                self.assertRaisesRegex(OSError, "simulated sealing interruption"),
            ):
                migrate_config_state(config)

            payload = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "sealing")
            self.assertFalse(source_path.exists())
            destination_inode = destination_path.stat().st_ino
            with sqlite3.connect(destination_path) as connection:
                connection.execute("INSERT INTO migration_rows VALUES ('pre-seal-write')")
            self.assertEqual(destination_path.stat().st_ino, destination_inode)

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-sealing-retry"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            payload = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "sealing")

    def test_migrate_config_state_rechecks_prepared_completion_transition(
            self,
    ) -> None:
        for mutate_destination in (False, True):
            with (
                self.subTest(mutate_destination=mutate_destination),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                intent_path = config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                )
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name=f"target-{mutate_destination}",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                    connection.execute(
                        "INSERT INTO migration_rows VALUES ('migrated-row')"
                    )

                real_exchange = config_module.rename_exchange
                exchange_count = 0

                def interrupt_prepared_completion(**kwargs: object) -> None:
                    nonlocal exchange_count
                    exchange_count += 1
                    if exchange_count == 4:
                        raise OSError("simulated prepared completion interruption")
                    real_exchange(**kwargs)

                with (
                    patch.object(
                        config_module,
                        "rename_exchange",
                        side_effect=interrupt_prepared_completion,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-prepared-completion"},
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "simulated prepared completion interruption",
                    ),
                ):
                    migrate_config_state(config)

                self.assertEqual(exchange_count, 4)
                payload = json.loads(intent_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["phase"], "sealing")
                prepared_completions = [
                    path
                    for path in intent_path.parent.glob(
                        f"{intent_path.name}.transition-*.json"
                    )
                    if json.loads(path.read_text(encoding="utf-8")).get("phase")
                    == "complete"
                ]
                self.assertEqual(len(prepared_completions), 1)
                if mutate_destination:
                    with sqlite3.connect(destination_path) as connection:
                        connection.execute(
                            "INSERT INTO migration_rows VALUES ('pre-handoff-write')"
                        )

                retry = exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-prepared-completion-retry"},
                )
                if mutate_destination:
                    with (
                        retry,
                        self.assertRaisesRegex(
                            MediaforceRuntimeBusyError,
                            "could not be resumed safely",
                        ),
                    ):
                        migrate_config_state(config)
                    payload = json.loads(intent_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["phase"], "sealing")
                else:
                    with retry:
                        migrate_config_state(config)
                    self._assert_completed_v5_intent(intent_path)

    def test_migrate_config_state_rechecks_legacy_prepared_completion_transition(
            self,
    ) -> None:
        for mutate_destination in (False, True):
            with (
                self.subTest(mutate_destination=mutate_destination),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                intent_path = config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                )
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name=f"legacy-target-{mutate_destination}",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                    connection.execute(
                        "INSERT INTO migration_rows VALUES ('legacy-migrated-row')"
                    )

                with (
                    patch.object(
                        config_module,
                        "_legacy_sqlite_migration_completion_payload",
                        side_effect=OSError(
                            "simulated pre-seal completion interruption"
                        ),
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-prepared-completion"},
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "simulated pre-seal completion interruption",
                    ),
                ):
                    migrate_config_state(config)

                cleaning_payload = json.loads(
                    intent_path.read_text(encoding="utf-8")
                )
                self.assertEqual(cleaning_payload["phase"], "cleaning")
                legacy_completion = (
                    config_module._legacy_sqlite_migration_legacy_completion_payload(
                        cleaning_payload
                    )
                )
                transition_path = (
                    config_module._legacy_sqlite_migration_intent_transition_path(
                        intent_path,
                        expected=cleaning_payload,
                        payload=legacy_completion,
                    )
                )
                config_module._write_legacy_sqlite_migration_intent(
                    transition_path,
                    legacy_completion,
                )
                if mutate_destination:
                    with sqlite3.connect(destination_path) as connection:
                        connection.execute(
                            "INSERT INTO migration_rows VALUES ('legacy-pre-handoff-write')"
                        )

                retry = exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-prepared-completion-retry"},
                )
                if mutate_destination:
                    with (
                        retry,
                        self.assertRaisesRegex(
                            MediaforceRuntimeBusyError,
                            "could not be resumed safely",
                        ),
                    ):
                        migrate_config_state(config)
                    payload = json.loads(intent_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["phase"], "cleaning")
                    self.assertEqual(
                        json.loads(transition_path.read_text(encoding="utf-8")),
                        legacy_completion,
                    )
                else:
                    with retry:
                        migrate_config_state(config)
                    self._assert_completed_v5_intent(intent_path)
                    self.assertEqual(
                        json.loads(transition_path.read_text(encoding="utf-8")),
                        cleaning_payload,
                    )

    def test_migrate_config_state_rejects_staging_path_swap_before_backup(self) -> None:
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
                connection.execute("CREATE TABLE migration_rows (value TEXT NOT NULL)")
                connection.execute("INSERT INTO migration_rows VALUES ('trusted-row')")

            victim_path = root / "victim.sqlite3"
            victim_bytes = b"must-not-be-overwritten"
            victim_path.write_bytes(victim_bytes)
            real_write = config_module._LegacySQLiteMigrationStaging.write_from
            replaced = False
            trusted_staging_path: Path | None = None

            def replace_staging_before_write(
                    staging: object,
                    backup_path: Path,
            ) -> None:
                nonlocal replaced, trusted_staging_path
                assert isinstance(
                    staging,
                    config_module._LegacySQLiteMigrationStaging,
                )
                if not replaced:
                    trusted_staging_path = staging.path.with_name(
                        f"{staging.path.name}.trusted"
                    )
                    staging.path.rename(trusted_staging_path)
                    staging.path.symlink_to(victim_path)
                    replaced = True
                real_write(staging, backup_path)

            with (
                patch.object(
                    config_module._LegacySQLiteMigrationStaging,
                    "write_from",
                    autospec=True,
                    side_effect=replace_staging_before_write,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-staging-replacement"},
                ),
                self.assertRaisesRegex(OSError, "staging identity changed"),
            ):
                migrate_config_state(config)

            self.assertTrue(replaced)
            self.assertIsNotNone(trusted_staging_path)
            assert trusted_staging_path is not None
            self.assertTrue(trusted_staging_path.is_file())
            self.assertEqual(victim_path.read_bytes(), victim_bytes)
            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())

    def test_migrate_config_state_rejects_destination_parent_rebind_during_copy(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            displaced_parent = root / "configured-state.displaced"
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
                connection.execute("INSERT INTO migration_rows VALUES ('trusted-row')")

            real_intent_payload = (
                config_module._legacy_sqlite_migration_intent_payload
            )
            payload_count = 0
            parent_rebound = False

            def rebind_destination_parent_before_ready(
                    **kwargs: object,
            ) -> dict[str, object]:
                nonlocal parent_rebound, payload_count
                payload_count += 1
                if payload_count == 2:
                    destination_path.parent.rename(displaced_parent)
                    destination_path.parent.mkdir()
                    for path in tuple(displaced_parent.iterdir()):
                        path.rename(destination_path.parent / path.name)
                    parent_rebound = True
                return real_intent_payload(**kwargs)

            with (
                patch.object(
                    config_module,
                    "_legacy_sqlite_migration_intent_payload",
                    side_effect=rebind_destination_parent_before_ready,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-copy-parent-rebind"},
                ),
                self.assertRaisesRegex(OSError, "destination parent changed"),
            ):
                migrate_config_state(config)

            self.assertTrue(parent_rebound)
            self.assertEqual(payload_count, 2)
            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_rejects_preexisting_destination_sidecars(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                sidecar_path = Path(f"{destination_path}{suffix}")
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name="target",
                )
                source_path.parent.mkdir()
                destination_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                sidecar_path.write_bytes(f"foreign{suffix}".encode("utf-8"))

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-destination-sidecar"},
                    ),
                    self.assertRaisesRegex(OSError, "destination sidecar already exists"),
                ):
                    migrate_config_state(config)

                self.assertEqual(
                    sidecar_path.read_bytes(),
                    f"foreign{suffix}".encode("utf-8"),
                )
                self.assertTrue(source_path.is_file())
                self.assertFalse(destination_path.exists())

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

    def test_migrate_config_state_rejects_source_path_replacement_and_preserves_staging(self) -> None:
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
                len(list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                ))),
                1,
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
                len(list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                ))),
                1,
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
            self.assertEqual(intent_payload["schema_version"], 5)
            self.assertEqual(intent_payload["phase"], "cleaning")
            self.assertEqual(
                intent_payload["cleanup_policy"],
                "retain_verified_quarantines",
            )
            self.assertEqual(
                intent_payload["publication_policy"],
                "exclusive_staging_rename",
            )
            self.assertRegex(
                intent_payload["source_main_sha256"],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertEqual(
                set(intent_payload["source_sidecar_snapshots"]),
                {"-wal", "-shm", "-journal"},
            )
            for snapshot in intent_payload["source_sidecar_snapshots"].values():
                self.assertRegex(snapshot["sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                len(list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                ))),
                0,
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
            self._assert_completed_v5_intent(intent_path)
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

    def test_migrate_config_state_upgrades_legacy_v3_cleaning_intent(self) -> None:
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
                    "INSERT INTO migration_rows VALUES ('legacy-v3-row')"
                )

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "discard_after_publish",
                    side_effect=OSError("simulated legacy v3 interruption"),
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-v3-setup"},
                ),
                self.assertRaisesRegex(
                    OSError,
                    "simulated legacy v3 interruption",
                ),
            ):
                migrate_config_state(config)

            current_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            legacy_payload = self._legacy_v3_migration_intent(current_payload)
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=current_payload,
                payload=legacy_payload,
            )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-v3-resume"},
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self._assert_completed_v5_intent(intent_path)
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("legacy-v3-row",)],
                )

    def test_migrate_config_state_recovers_parent_v4_all_absent_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                config,
                source_path,
                destination_path,
                intent_path,
                current_payload,
            ) = self._prepare_interrupted_cleaning_migration(
                root,
                name="target",
                value="v4-all-absent-row",
            )
            v4_payload = self._legacy_v4_migration_intent(current_payload)
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=current_payload,
                payload=v4_payload,
            )
            receipt_path = config_module._legacy_sqlite_migration_receipt_path(
                config,
                source=source_path,
            )
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{source_path}{suffix}").unlink()

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "parent-v4-all-absent-resume"},
            ):
                migrate_config_state(config)

            completed = self._assert_completed_v5_intent(intent_path)
            self.assertEqual(
                completed["legacy_cleanup_absent_prefix"],
                ["", "-wal", "-shm", "-journal"],
            )
            self.assertEqual(completed["retained_cleanup_artifacts"], [])
            self.assertEqual(
                completed["publication_policy"],
                "legacy_hardlink_without_alias",
            )
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(source_path.parent.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("v4-all-absent-row",)],
                )

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "parent-v4-all-absent-recheck"},
            ):
                migrate_config_state(config)

            displaced_parent = root / "configured-state.displaced"
            destination_path.parent.rename(displaced_parent)
            destination_path.parent.mkdir()
            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "parent-v4-all-absent-rebind"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "migration authority is missing",
                ),
            ):
                migrate_config_state(config)
            self.assertFalse(destination_path.exists())

    def test_migrate_config_state_rejects_receipt_inside_destination_parent(
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
                runtime_reservation_dir=(
                    destination_path.parent / "reservations"
                ),
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('co-located-receipt-row')"
                )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-co-located-receipt"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "receipt storage must be outside",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertFalse(
                config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                ).exists()
            )

    def test_migrate_config_state_rejects_unsafe_receipt_layout_for_existing_destination(
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
                runtime_reservation_dir=(
                    destination_path.parent / "reservations"
                ),
            )
            destination_path.parent.mkdir()
            with sqlite3.connect(destination_path) as connection:
                connection.execute(
                    "CREATE TABLE replacement_rows (value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO replacement_rows VALUES ('replacement')"
                )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={
                        "purpose": "legacy-unsafe-receipt-without-evidence"
                    },
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "receipt storage must be outside",
                ),
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(
                config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                ).exists()
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM replacement_rows"
                    ).fetchall(),
                    [("replacement",)],
                )

    def test_migrate_config_state_rejects_unsafe_receipt_layout_before_database_creation(
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
                runtime_reservation_dir=(
                    destination_path.parent / "reservations"
                ),
            )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-unsafe-fresh-layout"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "receipt storage must be outside",
                ),
            ):
                migrate_config_state(config)

            self.assertFalse(source_path.exists())
            self.assertFalse(destination_path.exists())
            self.assertFalse(
                config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                ).exists()
            )

    def test_migrate_config_state_rejects_unsafe_receipt_layout_when_source_is_destination(
            self,
    ) -> None:
        for database_exists in (False, True):
            with self.subTest(database_exists=database_exists):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    database_path = root / "state" / "library.sqlite3"
                    config = self._legacy_migration_config(
                        root,
                        database_path=database_path,
                        name="target",
                        runtime_reservation_dir=(
                            database_path.parent / "reservations"
                        ),
                    )
                    if database_exists:
                        database_path.parent.mkdir()
                        with sqlite3.connect(database_path) as connection:
                            connection.execute(
                                "CREATE TABLE retained_rows (value TEXT NOT NULL)"
                            )
                            connection.execute(
                                "INSERT INTO retained_rows VALUES ('retained')"
                            )

                    with (
                        exclusive_mediaforce_runtime_lock(
                            config,
                            owner_payload={
                                "purpose": "legacy-unsafe-same-path-layout"
                            },
                        ),
                        self.assertRaisesRegex(
                            MediaforceRuntimeBusyError,
                            "receipt storage must be outside",
                        ),
                    ):
                        migrate_config_state(config)

                    self.assertEqual(database_path.exists(), database_exists)
                    self.assertFalse(
                        config_module._legacy_sqlite_migration_intent_path(
                            database_path
                        ).exists()
                    )
                    if database_exists:
                        with sqlite3.connect(database_path) as connection:
                            self.assertEqual(
                                connection.execute(
                                    "SELECT value FROM retained_rows"
                                ).fetchall(),
                                [("retained",)],
                            )

    def test_migrate_config_state_rejects_same_path_rollback_after_completed_migration(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            runtime_reservation_dir = root / "runtime-reservations"
            migrated_config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="migrated",
                runtime_reservation_dir=runtime_reservation_dir,
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute(
                    "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('authoritative')"
                )

            with exclusive_mediaforce_runtime_lock(
                migrated_config,
                owner_payload={"purpose": "legacy-completed-migration"},
            ):
                migrate_config_state(migrated_config)

            receipt_path = config_module._legacy_sqlite_migration_receipt_path(
                migrated_config,
                source=source_path,
            )
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(source_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM migration_rows"
                    ).fetchall(),
                    [("authoritative",)],
                )

            rollback_config = self._legacy_migration_config(
                root,
                database_path=source_path,
                name="rollback",
                runtime_reservation_dir=runtime_reservation_dir,
            )
            with (
                exclusive_mediaforce_runtime_lock(
                    rollback_config,
                    owner_payload={"purpose": "legacy-same-path-rollback"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "migration authority is missing",
                ),
            ):
                migrate_config_state(rollback_config)

            self.assertFalse(source_path.exists())
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM migration_rows"
                    ).fetchall(),
                    [("authoritative",)],
                )

    def test_migrate_config_state_rejects_symlinked_receipt_discovery_through_destination_parent(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            outside_reservations = root / "outside-reservations"
            destination_path.parent.mkdir()
            outside_reservations.mkdir()
            outside_reservations.chmod(0o700)
            destination_link = destination_path.parent / "reservations-link"
            destination_link.symlink_to(
                outside_reservations,
                target_is_directory=True,
            )
            reservation_link = root / "reservation-discovery-link"
            reservation_link.symlink_to(destination_link, target_is_directory=True)
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
                runtime_reservation_dir=reservation_link,
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('aliased-receipt-row')"
                )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-aliased-receipt"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "path alias through that parent",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertFalse(destination_path.exists())
            self.assertTrue(reservation_link.is_symlink())
            self.assertTrue(destination_link.is_symlink())
            self.assertFalse(
                config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                ).exists()
            )

    def test_migrate_config_state_rejects_live_sidecar_without_legacy_main(
            self,
    ) -> None:
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
                sidecar_path = Path(f"{source_path}{suffix}")
                sidecar_path.write_bytes(b"orphaned-legacy-sidecar")

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-orphaned-sidecar"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "sidecars exist without the main database",
                    ),
                ):
                    migrate_config_state(config)

                self.assertEqual(
                    sidecar_path.read_bytes(),
                    b"orphaned-legacy-sidecar",
                )
                self.assertFalse(destination_path.exists())
                self.assertFalse(
                    config_module._legacy_sqlite_migration_intent_path(
                        destination_path
                    ).exists()
                )

    def test_migrate_config_state_publishes_receipt_before_v4_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                config,
                source_path,
                destination_path,
                intent_path,
                current_payload,
            ) = self._prepare_interrupted_cleaning_migration(
                root,
                name="target",
                value="v4-receipt-row",
            )
            v4_payload = self._legacy_v4_migration_intent(current_payload)
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=current_payload,
                payload=v4_payload,
            )
            receipt_path = config_module._legacy_sqlite_migration_receipt_path(
                config,
                source=source_path,
            )
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{source_path}{suffix}").unlink()
            real_replace = config_module._replace_legacy_sqlite_migration_intent

            def interrupt_after_receipt(
                    path: Path,
                    *,
                    expected: dict[str, object],
                    payload: dict[str, object],
                    before_exchange: Callable[[], None] | None = None,
            ) -> None:
                if (
                    expected.get("schema_version") == 4
                    and payload.get("schema_version") == 5
                    and payload.get("phase") == "cleaning"
                ):
                    self.assertTrue(receipt_path.is_file())
                    raise OSError("simulated post-receipt interruption")
                real_replace(
                    path,
                    expected=expected,
                    payload=payload,
                    before_exchange=before_exchange,
                )

            with (
                patch.object(
                    config_module,
                    "_replace_legacy_sqlite_migration_intent",
                    side_effect=interrupt_after_receipt,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "parent-v4-receipt-interruption"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(receipt_path.is_file())
            displaced_parent = root / "configured-state.displaced"
            destination_path.parent.rename(displaced_parent)
            destination_path.parent.mkdir()
            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "parent-v4-receipt-rebind"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "migration authority is missing",
                ),
            ):
                migrate_config_state(config)

    def test_migrate_config_state_recovers_parent_v4_partial_prefix_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                config,
                source_path,
                destination_path,
                intent_path,
                current_payload,
            ) = self._prepare_interrupted_cleaning_migration(
                root,
                name="target",
                value="v4-prefix-row",
            )
            v4_payload = self._legacy_v4_migration_intent(current_payload)
            config_module._replace_legacy_sqlite_migration_intent(
                intent_path,
                expected=current_payload,
                payload=v4_payload,
            )
            staging_path = destination_path.parent / str(
                v4_payload["staging_name"]
            )
            os.link(destination_path, staging_path)
            source_path.unlink()
            Path(f"{source_path}-wal").unlink()
            shm_path = Path(f"{source_path}-shm")
            shm_quarantine = self._cleanup_quarantine_path(
                source_path,
                v4_payload,
                "-shm",
            )
            shm_path.rename(shm_quarantine)

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "parent-v4-prefix-resume"},
            ):
                migrate_config_state(config)

            completed = self._assert_completed_v5_intent(intent_path)
            self.assertEqual(
                completed["legacy_cleanup_absent_prefix"],
                ["", "-wal"],
            )
            self.assertEqual(
                completed["publication_policy"],
                "legacy_hardlink_retained_alias",
            )
            retained_names = {
                artifact["name"]
                for artifact in completed["retained_cleanup_artifacts"]
            }
            self.assertEqual(
                retained_names,
                {
                    shm_quarantine.name,
                    self._cleanup_quarantine_path(
                        source_path,
                        v4_payload,
                        "-journal",
                    ).name,
                },
            )
            self.assertEqual(
                {
                    path.name
                    for path in source_path.parent.iterdir()
                    if ".mediaforce-retired-" in path.name
                },
                retained_names,
            )
            self.assertEqual(
                staging_path.stat(follow_symlinks=False).st_ino,
                destination_path.stat(follow_symlinks=False).st_ino,
            )
            self.assertEqual(destination_path.stat().st_nlink, 2)
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("v4-prefix-row",)],
                )

    def test_migrate_config_state_rejects_impossible_parent_v4_cleanup_states(
            self,
    ) -> None:
        for mode in ("replacement", "conflict", "out-of-order", "unidentified"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (
                    config,
                    source_path,
                    destination_path,
                    intent_path,
                    current_payload,
                ) = self._prepare_interrupted_cleaning_migration(
                    root,
                    name=f"target-{mode}",
                    value=f"v4-invalid-{mode}",
                )
                v4_payload = self._legacy_v4_migration_intent(current_payload)
                config_module._replace_legacy_sqlite_migration_intent(
                    intent_path,
                    expected=current_payload,
                    payload=v4_payload,
                )
                staging_path = destination_path.parent / str(
                    v4_payload["staging_name"]
                )
                os.link(destination_path, staging_path)
                preserved_paths: list[Path] = []
                if mode == "replacement":
                    trusted_main = source_path.parent / "trusted-main.sqlite3"
                    source_path.rename(trusted_main)
                    source_path.write_bytes(b"replacement-main")
                    preserved_paths.extend((trusted_main, source_path))
                elif mode == "conflict":
                    main_quarantine = self._cleanup_quarantine_path(
                        source_path,
                        v4_payload,
                        "",
                    )
                    os.link(source_path, main_quarantine)
                    preserved_paths.extend((source_path, main_quarantine))
                elif mode == "out-of-order":
                    wal_path = Path(f"{source_path}-wal")
                    wal_quarantine = self._cleanup_quarantine_path(
                        source_path,
                        v4_payload,
                        "-wal",
                    )
                    wal_path.rename(wal_quarantine)
                    preserved_paths.extend((source_path, wal_quarantine))
                else:
                    unidentified = source_path.parent / (
                        f".{source_path.name}-wal.mediaforce-retired-unidentified"
                    )
                    unidentified.write_bytes(b"unidentified")
                    preserved_paths.extend((source_path, unidentified))

                with (
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": f"parent-v4-invalid-{mode}"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "could not be resumed safely",
                    ),
                ):
                    migrate_config_state(config)

                self.assertEqual(
                    json.loads(intent_path.read_text(encoding="utf-8")),
                    v4_payload,
                )
                self.assertTrue(destination_path.is_file())
                self.assertTrue(staging_path.is_file())
                for path in preserved_paths:
                    self.assertTrue(path.exists())

    def test_migrate_config_state_preserves_digestless_main_quarantine(self) -> None:
        for schema_version, phase in ((2, "ready"), (3, "cleaning")):
            with self.subTest(schema_version=schema_version):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    source_path = root / "state" / "library.sqlite3"
                    destination_path = (
                        root / "configured-state" / "library.sqlite3"
                    )
                    intent_path = (
                        config_module._legacy_sqlite_migration_intent_path(
                            destination_path
                        )
                    )
                    config = self._legacy_migration_config(
                        root,
                        database_path=destination_path,
                        name=f"target-{schema_version}",
                    )
                    source_path.parent.mkdir()
                    with sqlite3.connect(source_path) as connection:
                        connection.execute(
                            "CREATE TABLE migration_rows (value TEXT)"
                        )
                        connection.execute(
                            "INSERT INTO migration_rows VALUES (?)",
                            (f"legacy-v{schema_version}-quarantine-row",),
                        )
                    real_rename_exclusive = runtime_lock_module.rename_exclusive
                    interrupted = False

                    def interrupt_after_main_claim(**kwargs: object) -> None:
                        nonlocal interrupted
                        real_rename_exclusive(**kwargs)
                        if (
                            kwargs.get("source_name") == source_path.name
                            and not interrupted
                        ):
                            interrupted = True
                            raise OSError(
                                "simulated digestless main quarantine interruption"
                            )

                    with (
                        patch(
                            "mediaforce.web.runtime_lock.rename_exclusive",
                            new=interrupt_after_main_claim,
                        ),
                        exclusive_mediaforce_runtime_lock(
                            config,
                            owner_payload={
                                "purpose": "legacy-digestless-quarantine-setup"
                            },
                        ),
                        self.assertRaisesRegex(
                            MediaforceRuntimeBusyError,
                            "source cleanup failed after publication",
                        ),
                    ):
                        migrate_config_state(config)

                    current_payload = json.loads(
                        intent_path.read_text(encoding="utf-8")
                    )
                    if schema_version == 2:
                        legacy_payload = self._legacy_v2_migration_intent(
                            current_payload,
                            phase=phase,
                        )
                    else:
                        legacy_payload = self._legacy_v3_migration_intent(
                            current_payload
                        )
                    config_module._replace_legacy_sqlite_migration_intent(
                        intent_path,
                        expected=current_payload,
                        payload=legacy_payload,
                    )
                    quarantines = list(source_path.parent.glob(
                        f".{source_path.name}.mediaforce-retired-*"
                    ))
                    self.assertTrue(interrupted)
                    self.assertEqual(len(quarantines), 1)

                    with (
                        self.assertLogs(
                            config_module.LOGGER,
                            level="WARNING",
                        ) as logs,
                        exclusive_mediaforce_runtime_lock(
                            config,
                            owner_payload={
                                "purpose": "legacy-digestless-quarantine-resume"
                            },
                        ),
                    ):
                        migrate_config_state(config)

                    self.assertTrue(any(
                        "Retained legacy SQLite cleanup artifacts"
                        in message
                        for message in logs.output
                    ))
                    self.assertFalse(source_path.exists())
                    self._assert_completed_v5_intent(intent_path)
                    self.assertTrue(quarantines[0].is_file())
                    with sqlite3.connect(destination_path) as connection:
                        self.assertEqual(
                            connection.execute(
                                "SELECT value FROM migration_rows"
                            ).fetchall(),
                            [(f"legacy-v{schema_version}-quarantine-row",)],
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
            v2_payload = self._legacy_v2_migration_intent(v3_payload)
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
            self._assert_completed_v5_intent(intent_path)
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
            v2_payload = self._legacy_v2_migration_intent(v3_payload)
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
                "Retained legacy SQLite cleanup artifacts" in message
                for message in logs.output
            ))
            self.assertFalse(source_path.exists())
            self._assert_completed_v5_intent(intent_path)
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

    def test_migrate_config_state_rechecks_source_parent_after_intent_completion(
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
            real_exchange = config_module.rename_exchange
            replacement_marker = source_path.parent / "replacement-marker"
            parent_swapped = False
            exchange_count = 0

            def swap_source_parent_during_completion(
                    **kwargs: object,
            ) -> None:
                nonlocal exchange_count, parent_swapped
                exchange_count += 1
                if exchange_count == 3:
                    source_path.parent.rename(displaced_parent)
                    source_path.parent.mkdir()
                    replacement_marker.write_bytes(b"replacement-parent")
                    parent_swapped = True
                real_exchange(**kwargs)

            with (
                patch.object(
                    config_module,
                    "rename_exchange",
                    side_effect=swap_source_parent_during_completion,
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
            self.assertEqual(exchange_count, 3)
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

            self._assert_completed_v5_intent(intent_path)
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

    def test_migrate_config_state_preserves_exclusive_publication_race(self) -> None:
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
                    "INSERT INTO migration_rows VALUES ('publication-race-row')"
                )
            real_rename_exclusive = config_module.rename_exclusive
            raced = False

            def publish_after_replacement(**kwargs: object) -> None:
                nonlocal raced
                if (
                    kwargs.get("destination_name") == destination_path.name
                    and str(kwargs.get("source_name", "")).startswith(
                        f".{destination_path.name}.migration-"
                    )
                    and not raced
                ):
                    destination_path.write_bytes(b"replacement-destination")
                    raced = True
                real_rename_exclusive(**kwargs)

            with (
                patch.object(
                    config_module,
                    "rename_exclusive",
                    side_effect=publish_after_replacement,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-publication-race"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "destination appeared",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(raced)
            self.assertEqual(
                destination_path.read_bytes(),
                b"replacement-destination",
            )
            staging_paths = list(destination_path.parent.glob(
                ".library.sqlite3.migration-*.sqlite3"
            ))
            self.assertEqual(len(staging_paths), 1)
            with sqlite3.connect(staging_paths[0]) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("publication-race-row",)],
                )
            intent_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(intent_payload["schema_version"], 5)
            self.assertEqual(intent_payload["phase"], "ready")
            self.assertTrue(source_path.is_file())

    def test_migrate_config_state_recovers_prepared_and_published_intent_crashes(
            self,
    ) -> None:
        for crash_state in ("prepared", "published"):
            with (
                self.subTest(crash_state=crash_state),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                source_path = root / "state" / "library.sqlite3"
                destination_path = root / "configured-state" / "library.sqlite3"
                intent_path = config_module._legacy_sqlite_migration_intent_path(
                    destination_path
                )
                config = self._legacy_migration_config(
                    root,
                    database_path=destination_path,
                    name=f"target-{crash_state}",
                )
                source_path.parent.mkdir()
                with sqlite3.connect(source_path) as connection:
                    connection.execute("CREATE TABLE migration_rows (value TEXT)")
                    connection.execute(
                        "INSERT INTO migration_rows VALUES (?)",
                        (f"intent-{crash_state}-row",),
                    )
                real_exchange = config_module.rename_exchange
                interrupted = False

                def interrupt_first_exchange(**kwargs: object) -> None:
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        if crash_state == "published":
                            real_exchange(**kwargs)
                        raise OSError(f"simulated {crash_state} intent crash")
                    real_exchange(**kwargs)

                with (
                    patch.object(
                        config_module,
                        "rename_exchange",
                        side_effect=interrupt_first_exchange,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": f"intent-{crash_state}-setup"},
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        f"simulated {crash_state} intent crash",
                    ),
                ):
                    migrate_config_state(config)

                self.assertTrue(interrupted)
                current = json.loads(intent_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    current["phase"],
                    "copying" if crash_state == "prepared" else "ready",
                )
                transition_paths = list(intent_path.parent.glob(
                    f"{intent_path.name}.transition-*.json"
                ))
                self.assertEqual(len(transition_paths), 1)
                transition_payload = json.loads(
                    transition_paths[0].read_text(encoding="utf-8")
                )
                self.assertEqual(
                    transition_payload["phase"],
                    "ready" if crash_state == "prepared" else "copying",
                )

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": f"intent-{crash_state}-resume"},
                ):
                    migrate_config_state(config)

                self._assert_completed_v5_intent(intent_path)
                self.assertFalse(source_path.exists())
                with sqlite3.connect(destination_path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM migration_rows"
                        ).fetchall(),
                        [(f"intent-{crash_state}-row",)],
                    )

    def test_migrate_config_state_preserves_intent_transition_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            trusted_path = intent_path.parent / f"{intent_path.name}.trusted"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
            real_exchange = config_module.rename_exchange
            replacement_payload = {"replacement": "transition"}
            replacement_bytes = config_module.canonical_json_bytes(
                replacement_payload
            )
            transition_path: Path | None = None
            swapped = False

            def swap_transition_source(**kwargs: object) -> None:
                nonlocal swapped, transition_path
                if not swapped:
                    trusted_bytes = intent_path.read_bytes()
                    intent_path.rename(trusted_path)
                    intent_path.write_bytes(replacement_bytes)
                    intent_path.chmod(0o400)
                    transition_path = intent_path.parent / str(
                        kwargs["second_name"]
                    )
                    real_exchange(**kwargs)
                    self.assertEqual(trusted_path.read_bytes(), trusted_bytes)
                    swapped = True
                    return
                real_exchange(**kwargs)

            with (
                patch.object(
                    config_module,
                    "rename_exchange",
                    side_effect=swap_transition_source,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "intent-transition-swap"},
                ),
                self.assertRaisesRegex(OSError, "intent identity changed"),
            ):
                migrate_config_state(config)

            self.assertTrue(swapped)
            self.assertIsNotNone(transition_path)
            assert transition_path is not None
            self.assertEqual(transition_path.read_bytes(), replacement_bytes)
            self.assertTrue(trusted_path.is_file())
            self.assertTrue(intent_path.is_file())
            self.assertTrue(source_path.is_file())
            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "intent-transition-swap-retry"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)
            self.assertEqual(transition_path.read_bytes(), replacement_bytes)
            self.assertTrue(trusted_path.is_file())

    def test_migrate_config_state_preserves_intent_completion_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            trusted_path = intent_path.parent / f"{intent_path.name}.trusted-cleaning"
            config = self._legacy_migration_config(
                root,
                database_path=destination_path,
                name="target",
            )
            source_path.parent.mkdir()
            with sqlite3.connect(source_path) as connection:
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
            real_exchange = config_module.rename_exchange
            replacement_bytes = config_module.canonical_json_bytes(
                {"replacement": "completion"}
            )
            exchange_count = 0
            transition_path: Path | None = None

            def swap_completion_source(**kwargs: object) -> None:
                nonlocal exchange_count, transition_path
                exchange_count += 1
                if exchange_count == 3:
                    intent_path.rename(trusted_path)
                    intent_path.write_bytes(replacement_bytes)
                    intent_path.chmod(0o400)
                    transition_path = intent_path.parent / str(
                        kwargs["second_name"]
                    )
                real_exchange(**kwargs)

            with (
                patch.object(
                    config_module,
                    "rename_exchange",
                    side_effect=swap_completion_source,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "intent-completion-swap"},
                ),
                self.assertRaisesRegex(OSError, "intent identity changed"),
            ):
                migrate_config_state(config)

            self.assertEqual(exchange_count, 3)
            self.assertIsNotNone(transition_path)
            assert transition_path is not None
            self.assertEqual(transition_path.read_bytes(), replacement_bytes)
            trusted_payload = json.loads(trusted_path.read_text(encoding="utf-8"))
            self.assertEqual(trusted_payload["phase"], "cleaning")
            sealing_payload = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(sealing_payload["phase"], "sealing")
            self.assertFalse(source_path.exists())
            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "intent-completion-swap-retry"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)
            self.assertEqual(transition_path.read_bytes(), replacement_bytes)
            self.assertTrue(trusted_path.is_file())

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
            real_retire_sidecar = runtime_lock_module._LegacySQLiteMutationGuard.retire_sidecar
            interrupted = False

            def interrupt_before_sidecar_cleanup(
                    guard: object,
                    suffix: str,
                    **kwargs: object,
            ) -> None:
                nonlocal interrupted
                if suffix == "-wal" and not interrupted:
                    interrupted = True
                    raise OSError("simulated interruption before sidecar cleanup")
                real_retire_sidecar(guard, suffix, **kwargs)

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
                    patch.object(
                        runtime_lock_module._LegacySQLiteMutationGuard,
                        "retire_sidecar",
                        new=interrupt_before_sidecar_cleanup,
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
                self.assertTrue(Path(f"{source_path}-wal").exists())
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())

                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-main-retirement-resume"},
                ):
                    migrate_config_state(config)

                self._assert_completed_v5_intent(intent_path)
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
                    expected_snapshot: object,
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

    def test_migrate_config_state_rejects_source_recreation_during_intent_completion(
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
            real_exchange = config_module.rename_exchange
            recreated = False
            exchange_count = 0

            def recreate_during_completion(
                    **kwargs: object,
            ) -> None:
                nonlocal exchange_count, recreated
                exchange_count += 1
                if exchange_count == 3:
                    source_path.write_bytes(b"replacement-source")
                    recreated = True
                real_exchange(**kwargs)

            with (
                patch.object(
                    config_module,
                    "rename_exchange",
                    side_effect=recreate_during_completion,
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
            self.assertEqual(exchange_count, 3)
            self.assertEqual(source_path.read_bytes(), b"replacement-source")
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_preserves_post_claim_byte_mutation(self) -> None:
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
                    "INSERT INTO migration_rows VALUES ('post-claim-row')"
                )
            real_rename_exclusive = runtime_lock_module.rename_exclusive
            mutated = False

            def mutate_after_claim(**kwargs: object) -> None:
                nonlocal mutated
                real_rename_exclusive(**kwargs)
                if kwargs.get("source_name") != source_path.name or mutated:
                    return
                directory_descriptor = int(
                    kwargs["destination_directory_descriptor"]
                )
                quarantine_name = str(kwargs["destination_name"])
                descriptor = os.open(
                    quarantine_name,
                    os.O_RDWR,
                    dir_fd=directory_descriptor,
                )
                try:
                    info = os.fstat(descriptor)
                    original = os.pread(descriptor, 1, 0)
                    self.assertTrue(original)
                    os.pwrite(
                        descriptor,
                        bytes([original[0] ^ 0xFF]),
                        0,
                    )
                    os.fsync(descriptor)
                    os.utime(
                        descriptor,
                        ns=(info.st_atime_ns, info.st_mtime_ns),
                    )
                finally:
                    os.close(descriptor)
                mutated = True

            with (
                patch(
                    "mediaforce.web.runtime_lock.rename_exclusive",
                    new=mutate_after_claim,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-post-claim-mutation"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source cleanup failed after publication",
                ),
            ):
                migrate_config_state(config)

            quarantines = list(source_path.parent.glob(
                f".{source_path.name}.mediaforce-retired-*"
            ))
            self.assertTrue(mutated)
            self.assertFalse(source_path.exists())
            self.assertEqual(len(quarantines), 1)
            self.assertTrue(quarantines[0].is_file())
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_preserves_last_boundary_main_quarantine_swap(
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
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('last-boundary-live-row')"
                )
            source_info = source_path.stat(follow_symlinks=False)
            quarantine_path = source_path.parent / (
                config_module.legacy_sqlite_migration_quarantine_name(
                    source_path.name,
                    device=source_info.st_dev,
                    inode=source_info.st_ino,
                    size=source_info.st_size,
                    mtime_ns=source_info.st_mtime_ns,
                )
            )
            claimed_path = source_path.parent / "claimed-original.sqlite3"
            replacement = b"last-boundary-live-replacement"
            real_descriptor_sha256 = (
                runtime_lock_module._legacy_sqlite_descriptor_sha256
            )
            quarantine_validations = 0
            swapped = False

            def swap_after_last_quarantine_validation(descriptor: int) -> str:
                nonlocal quarantine_validations, swapped
                digest = real_descriptor_sha256(descriptor)
                if quarantine_path.is_file():
                    quarantine_validations += 1
                    if quarantine_validations == 2 and not swapped:
                        quarantine_path.rename(claimed_path)
                        quarantine_path.write_bytes(replacement)
                        swapped = True
                return digest

            with (
                patch(
                    "mediaforce.web.runtime_lock._legacy_sqlite_descriptor_sha256",
                    side_effect=swap_after_last_quarantine_validation,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-last-boundary-live-swap"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source cleanup is incomplete",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(swapped)
            self.assertFalse(source_path.exists())
            self.assertEqual(quarantine_path.read_bytes(), replacement)
            self.assertEqual(
                claimed_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertNotEqual(
                quarantine_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-last-boundary-live-resume"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertEqual(quarantine_path.read_bytes(), replacement)
            self.assertEqual(
                claimed_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertTrue(intent_path.is_file())

    def test_migrate_config_state_rejects_source_and_destination_without_intent(
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
            destination_path.parent.mkdir()
            for path, value in (
                (source_path, "legacy"),
                (destination_path, "configured"),
            ):
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "CREATE TABLE migration_rows (value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO migration_rows VALUES (?)",
                        (value,),
                    )

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-conflicting-databases"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "both exist without a resumable migration intent",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(source_path.is_file())
            self.assertTrue(destination_path.is_file())

    def test_migrate_config_state_filters_named_unrelated_source_churn(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "state" / "library.sqlite3"
            destination_path = root / "configured-state" / "library.sqlite3"
            intent_path = config_module._legacy_sqlite_migration_intent_path(
                destination_path
            )
            sibling_path = source_path.parent / ".DS_Store"
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
                    "INSERT INTO migration_rows VALUES ('sibling-churn-row')"
                )
            real_assert_cleanup = LegacySQLiteMigrationSource.assert_cleanup_complete
            checks = 0

            def assert_cleanup_then_create_sibling(
                    locked_source: LegacySQLiteMigrationSource,
            ) -> None:
                nonlocal checks
                real_assert_cleanup(locked_source)
                checks += 1
                if checks == 1:
                    sibling_path.write_bytes(b"unrelated")

            if sys.platform == "darwin":
                with (
                    patch.object(
                        LegacySQLiteMigrationSource,
                        "assert_cleanup_complete",
                        new=assert_cleanup_then_create_sibling,
                    ),
                    exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": "legacy-unrelated-sibling-churn"},
                    ),
                    self.assertRaisesRegex(
                        MediaforceRuntimeBusyError,
                        "source cleanup is incomplete",
                    ),
                ):
                    migrate_config_state(config)

                self.assertEqual(checks, 1)
                self.assertFalse(source_path.exists())
                self.assertTrue(destination_path.is_file())
                self.assertTrue(intent_path.is_file())
                return

            with (
                patch.object(
                    LegacySQLiteMigrationSource,
                    "assert_cleanup_complete",
                    new=assert_cleanup_then_create_sibling,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-unrelated-sibling-churn"},
                ),
            ):
                migrate_config_state(config)

            self.assertGreaterEqual(checks, 3)
            self.assertFalse(source_path.exists())
            self.assertTrue(destination_path.is_file())

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
            real_rename_exclusive = runtime_lock_module.rename_exclusive
            interrupted = False

            def interrupt_after_main_quarantine_claim(**kwargs: object) -> None:
                nonlocal interrupted
                if (
                    kwargs.get("source_name") == source_path.name
                    and not interrupted
                ):
                    real_rename_exclusive(**kwargs)
                    interrupted = True
                    raise OSError("simulated interruption after main claim")
                real_rename_exclusive(**kwargs)

            with (
                patch(
                    "mediaforce.web.runtime_lock.rename_exclusive",
                    new=interrupt_after_main_quarantine_claim,
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

            self._assert_completed_v5_intent(intent_path)
            self.assertEqual(
                len(list(source_path.parent.glob("*.mediaforce-retired-*"))),
                4,
            )
            with sqlite3.connect(destination_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM migration_rows").fetchall(),
                    [("main-quarantine-row",)],
                )

    def test_migrate_config_state_preserves_last_boundary_resumed_quarantine_swap(
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
                connection.execute("CREATE TABLE migration_rows (value TEXT)")
                connection.execute(
                    "INSERT INTO migration_rows VALUES ('last-boundary-resume-row')"
                )
            source_info = source_path.stat(follow_symlinks=False)
            quarantine_path = source_path.parent / (
                config_module.legacy_sqlite_migration_quarantine_name(
                    source_path.name,
                    device=source_info.st_dev,
                    inode=source_info.st_ino,
                    size=source_info.st_size,
                    mtime_ns=source_info.st_mtime_ns,
                )
            )
            real_retire_sidecar = runtime_lock_module._LegacySQLiteMutationGuard.retire_sidecar
            interrupted = False

            def interrupt_before_sidecar_retirement(
                    guard: object,
                    suffix: str,
                    **kwargs: object,
            ) -> None:
                nonlocal interrupted
                if suffix == "-wal" and not interrupted:
                    interrupted = True
                    raise OSError("simulated interruption before sidecar retirement")
                real_retire_sidecar(guard, suffix, **kwargs)

            with (
                patch.object(
                    runtime_lock_module._LegacySQLiteMutationGuard,
                    "retire_sidecar",
                    new=interrupt_before_sidecar_retirement,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-last-boundary-resume-setup"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "source cleanup failed after publication",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(interrupted)
            self.assertFalse(source_path.exists())
            self.assertTrue(quarantine_path.is_file())
            self.assertTrue(intent_path.is_file())

            claimed_path = source_path.parent / "claimed-resume-original.sqlite3"
            replacement = b"last-boundary-resume-replacement"
            real_snapshot = config_module._legacy_sqlite_migration_file_snapshot_at
            quarantine_validations = 0
            swapped = False

            def swap_after_last_resumed_validation(
                    directory_descriptor: int,
                    name: str,
                    *,
                    include_sha256: bool,
                    require_single_link: bool,
                    include_timestamps: bool = True,
            ) -> dict[str, object]:
                nonlocal quarantine_validations, swapped
                snapshot = real_snapshot(
                    directory_descriptor,
                    name,
                    include_sha256=include_sha256,
                    require_single_link=require_single_link,
                    include_timestamps=include_timestamps,
                )
                if name == quarantine_path.name and include_sha256:
                    quarantine_validations += 1
                    if quarantine_validations == 2 and not swapped:
                        quarantine_path.rename(claimed_path)
                        quarantine_path.write_bytes(replacement)
                        swapped = True
                return snapshot

            with (
                patch.object(
                    config_module,
                    "_legacy_sqlite_migration_file_snapshot_at",
                    side_effect=swap_after_last_resumed_validation,
                ),
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-last-boundary-resume-swap"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertTrue(swapped)
            self.assertEqual(quarantine_path.read_bytes(), replacement)
            self.assertEqual(
                claimed_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertNotEqual(
                quarantine_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertTrue(destination_path.is_file())
            self.assertTrue(intent_path.is_file())

            with (
                exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "legacy-last-boundary-resume-retry"},
                ),
                self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "could not be resumed safely",
                ),
            ):
                migrate_config_state(config)

            self.assertEqual(quarantine_path.read_bytes(), replacement)
            self.assertEqual(
                claimed_path.stat(follow_symlinks=False).st_ino,
                source_info.st_ino,
            )
            self.assertTrue(intent_path.is_file())

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
            with (
                patch.object(
                    config_module,
                    "_legacy_sqlite_migration_completion_payload",
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
                real_retire_sidecar = runtime_lock_module._LegacySQLiteMutationGuard.retire_sidecar
                interrupted = False

                def interrupt_sidecar_cleanup(
                        guard: object,
                        suffix: str,
                        **kwargs: object,
                ) -> None:
                    nonlocal interrupted
                    if suffix == interrupted_suffix and not interrupted:
                        interrupted = True
                        raise OSError("simulated sidecar cleanup interruption")
                    real_retire_sidecar(guard, suffix, **kwargs)

                with (
                    patch.object(
                        runtime_lock_module._LegacySQLiteMutationGuard,
                        "retire_sidecar",
                        new=interrupt_sidecar_cleanup,
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
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{source_path}{suffix}").exists())
            self.assertEqual(
                len(list(source_path.parent.glob(
                    f".{source_path.name}-*.mediaforce-reservation-*"
                ))),
                3,
            )
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
            self._assert_completed_v5_intent(intent_path)
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
                0,
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
            self._assert_completed_v5_intent(intent_path)
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                [staging_path],
            )
            self.assertEqual(staging_path.read_bytes(), b"partial backup")

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

    def test_migrate_config_state_preserves_staging_after_interrupted_copy(self) -> None:
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
            retained_staging = list(destination_path.parent.glob(
                ".library.sqlite3.migration-*.sqlite3"
            ))
            self.assertEqual(len(retained_staging), 1)
            retained_bytes = retained_staging[0].read_bytes()
            self.assertTrue(intent_path.is_file())

            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "legacy-interruption-resume"},
            ):
                migrate_config_state(config)

            self.assertEqual(retained_staging[0].read_bytes(), retained_bytes)
            self.assertTrue(destination_path.is_file())
            self._assert_completed_v5_intent(intent_path)
            self.assertEqual(
                list(destination_path.parent.glob(
                    ".library.sqlite3.migration-*.sqlite3"
                )),
                retained_staging,
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
                    staging: object,
            ) -> Iterator[None]:
                nonlocal copy_started
                self.assertFalse(destination_path.exists())
                copy_started = True
                with original_copy(locked_source, staging):
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
