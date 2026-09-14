from __future__ import annotations

from contextlib import ExitStack, closing
import os
from pathlib import Path
import select
import sqlite3
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mediaforce.core import db_migrations
from mediaforce.core import db_custody as db_custody_module
from mediaforce.core.db import open_db
from mediaforce.core.db_custody import DatabaseFileCustody
from mediaforce.core.db_migrations import database_identity_connection_factory
from mediaforce.core.file_integrity import rename_exchange
from mediaforce.web import runtime_lock as runtime_lock_module
from mediaforce.web.runtime_lock import MediaforceRuntimeBusyError
from mediaforce.web.runtime_lock import MediaforceRuntimeLease
from mediaforce.web.runtime_lock import exclusive_mediaforce_runtime_lock
from mediaforce.web.runtime_lock import reserve_mediaforce_database_identity


_CHILD_WAL_WRITE = """
from contextlib import closing
import sqlite3
import sys

with closing(sqlite3.connect(sys.argv[1])) as connection:
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    connection.execute("INSERT INTO events VALUES ('checkpointed')")
    connection.commit()
    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    connection.execute("INSERT INTO events VALUES ('fresh-wal-cycle')")
    connection.commit()
"""

_CHILD_TRY_WRITE = """
from contextlib import closing
import sqlite3
import sys

try:
    with closing(sqlite3.connect(sys.argv[1], timeout=0.05)) as connection:
        connection.execute("INSERT INTO events VALUES ('unexpected-write')")
        connection.commit()
except sqlite3.OperationalError as exc:
    if "locked" not in str(exc).lower():
        raise
    raise SystemExit(23)
raise SystemExit(0)
"""


def _fixture_database_custody(db_path: Path) -> DatabaseFileCustody:
    descriptor = os.open(
        db_path,
        os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        return DatabaseFileCustody(
            db_path,
            file_descriptor=descriptor,
            close_file_descriptor=lambda: os.close(descriptor),
        )
    except BaseException:
        os.close(descriptor)
        raise


class DatabaseConnectionCustodyTests(unittest.TestCase):
    def test_second_custody_close_failure_does_not_skip_namespace_cleanup(
            self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=root / "absent.sqlite3",
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )
            cleanup_order: list[str] = []
            custody_close_calls = 0
            real_namespace_close = runtime_lock_module._RuntimeNamespaceLocks.close

            def close_custody(_lease: MediaforceRuntimeLease) -> None:
                nonlocal custody_close_calls
                custody_close_calls += 1
                if custody_close_calls == 2:
                    cleanup_order.append("custody-error")
                    raise RuntimeError("injected second custody close failure")

            def close_namespace(
                    locks: object,
                    *,
                    excluded_descriptors: frozenset[int] = frozenset(),
            ) -> None:
                cleanup_order.append("namespace")
                assert isinstance(
                    locks,
                    runtime_lock_module._RuntimeNamespaceLocks,
                )
                real_namespace_close(
                    locks,
                    excluded_descriptors=excluded_descriptors,
                )

            with (
                patch.object(
                    MediaforceRuntimeLease,
                    "_close_database_custody",
                    autospec=True,
                    side_effect=close_custody,
                ),
                patch.object(
                    runtime_lock_module._RuntimeNamespaceLocks,
                    "close",
                    autospec=True,
                    side_effect=close_namespace,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "injected second custody close failure",
                ),
            ):
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "cleanup-order-probe"},
                ):
                    pass
            self.assertEqual(cleanup_order, ["namespace", "custody-error"])

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires native atomic rename exchange",
    )
    def test_native_atomic_exchange_and_restore_is_sticky(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            db_path = root / "library.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            with closing(sqlite3.connect(db_path)) as original:
                original.execute("CREATE TABLE expected (value INTEGER)")
                original.commit()
            with closing(sqlite3.connect(replacement_path)) as replacement:
                replacement.execute("CREATE TABLE replacement (value INTEGER)")
                replacement.commit()
            custody = _fixture_database_custody(db_path)
            self.addCleanup(custody.close)
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            real_connection = db_migrations._DatabaseIdentityConnection

            with ExitStack() as resources:
                directory_descriptor = os.open(
                    root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                resources.callback(os.close, directory_descriptor)

                def connect_during_atomic_exchange(
                        *args: object,
                        **kwargs: object,
                ) -> sqlite3.Connection:
                    rename_exchange(
                        first_directory_descriptor=directory_descriptor,
                        first_name=db_path.name,
                        second_directory_descriptor=directory_descriptor,
                        second_name=replacement_path.name,
                    )
                    try:
                        return real_connection(*args, **kwargs)
                    finally:
                        rename_exchange(
                            first_directory_descriptor=directory_descriptor,
                            first_name=db_path.name,
                            second_directory_descriptor=directory_descriptor,
                            second_name=replacement_path.name,
                        )

                with (
                    patch.object(
                        db_migrations,
                        "_DatabaseIdentityConnection",
                        side_effect=connect_during_atomic_exchange,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "identity changed during connection",
                    ),
                ):
                    factory(str(db_path), check_same_thread=False)

    def test_recycled_lease_id_rebuilds_engine_with_fresh_custody(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
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

            with patch.object(
                runtime_lock_module,
                "id",
                return_value=41_337,
                create=True,
            ):
                for generation in range(2):
                    with exclusive_mediaforce_runtime_lock(
                        config,
                        owner_payload={"purpose": f"recycled-{generation}"},
                    ):
                        reserve_mediaforce_database_identity(
                            config,
                            create_if_missing=True,
                        )
                        with open_db(db_path) as connection:
                            connection.exec_driver_sql(
                                "CREATE TABLE IF NOT EXISTS events (value INTEGER)"
                            )
                            connection.exec_driver_sql(
                                "INSERT INTO events VALUES (?)",
                                (generation,),
                            )
                with closing(sqlite3.connect(db_path)) as observer:
                    self.assertEqual(
                        observer.execute("SELECT value FROM events").fetchall(),
                        [(0,), (1,)],
                    )

    def test_other_active_lease_survives_engine_cache_reset(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)

            def config_for(name: str) -> SimpleNamespace:
                case_root = root / name
                case_root.mkdir()
                config_path = case_root / "config.toml"
                config_path.write_text("config", encoding="utf-8")
                return SimpleNamespace(
                    paths=SimpleNamespace(
                        config_path=config_path,
                        db_path=case_root / "library.sqlite3",
                        web_state_dir=case_root / "state",
                        runtime_reservation_dir=case_root / "reservations",
                    ),
                )

            first_config = config_for("first")
            second_config = config_for("second")
            with exclusive_mediaforce_runtime_lock(
                first_config,
                owner_payload={"purpose": "first-active-lease"},
            ):
                reserve_mediaforce_database_identity(
                    first_config,
                    create_if_missing=True,
                )
                with open_db(first_config.paths.db_path) as first_connection:
                    first_connection.exec_driver_sql(
                        "CREATE TABLE events (value INTEGER)"
                    )
                    first_connection.exec_driver_sql("INSERT INTO events VALUES (1)")
                    with exclusive_mediaforce_runtime_lock(
                        second_config,
                        owner_payload={"purpose": "second-active-lease"},
                    ):
                        reserve_mediaforce_database_identity(
                            second_config,
                            create_if_missing=True,
                        )
                        with open_db(second_config.paths.db_path) as second_connection:
                            second_connection.exec_driver_sql(
                                "CREATE TABLE events (value INTEGER)"
                            )
                    self.assertEqual(
                        first_connection.exec_driver_sql(
                            "SELECT value FROM events"
                        ).fetchall(),
                        [(1,)],
                    )
                with open_db(first_config.paths.db_path) as fresh_connection:
                    self.assertEqual(
                        fresh_connection.exec_driver_sql(
                            "SELECT value FROM events"
                        ).fetchall(),
                        [(1,)],
                    )

    def test_runtime_custody_arm_failures_use_runtime_busy_error(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for existing in (True, False):
                with self.subTest(existing=existing):
                    case_root = root / str(existing)
                    case_root.mkdir()
                    config_path = case_root / "config.toml"
                    config_path.write_text("config", encoding="utf-8")
                    db_path = case_root / "library.sqlite3"
                    if existing:
                        with closing(sqlite3.connect(db_path)):
                            pass
                    config = SimpleNamespace(
                        paths=SimpleNamespace(
                            config_path=config_path,
                            db_path=db_path,
                            web_state_dir=case_root / "state",
                            runtime_reservation_dir=case_root / "reservations",
                        ),
                    )
                    failure = db_custody_module.DatabaseCustodyError(
                        "injected custody arm failure"
                    )
                    if existing:
                        with (
                            patch.object(
                                runtime_lock_module,
                                "DatabaseFileCustody",
                                side_effect=failure,
                            ),
                            self.assertRaisesRegex(
                                MediaforceRuntimeBusyError,
                                "database custody is unavailable",
                            ),
                        ):
                            with exclusive_mediaforce_runtime_lock(
                                config,
                                owner_payload={"purpose": "custody-arm-failure"},
                            ):
                                pass
                    else:
                        with exclusive_mediaforce_runtime_lock(
                            config,
                            owner_payload={"purpose": "custody-arm-failure"},
                        ):
                            with (
                                patch.object(
                                    runtime_lock_module,
                                    "DatabaseFileCustody",
                                    side_effect=failure,
                                ),
                                self.assertRaisesRegex(
                                    MediaforceRuntimeBusyError,
                                    "database custody is unavailable",
                                ),
                            ):
                                reserve_mediaforce_database_identity(
                                    config,
                                    create_if_missing=True,
                                )

    def test_connection_retries_checkpoint_between_fd_and_pinned_path_stats(
            self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with closing(sqlite3.connect(db_path)) as setup_connection:
                setup_connection.execute("PRAGMA journal_mode=WAL")
                setup_connection.execute("CREATE TABLE events (value TEXT)")
                setup_connection.commit()
            custody = _fixture_database_custody(db_path)
            self.addCleanup(custody.close)
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            real_pinned_path = (
                db_migrations._database_connection_path_for_directory_descriptor
            )
            real_snapshot = db_migrations._database_connection_path_snapshot
            checkpointed = False

            def pinned_path_then_checkpoint(
                    directory_descriptor: int,
                    *,
                    directory_info: os.stat_result,
                    filename: str,
            ) -> Path:
                nonlocal checkpointed
                pinned_path = real_pinned_path(
                    directory_descriptor,
                    directory_info=directory_info,
                    filename=filename,
                )
                if checkpointed:
                    return pinned_path
                checkpointed = True
                before = db_path.stat()
                with closing(sqlite3.connect(db_path)) as writer:
                    writer.execute("INSERT INTO events VALUES ('second-seam')")
                    writer.commit()
                    self.assertEqual(
                        writer.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone(),
                        (0, 0, 0),
                    )
                after = db_path.stat()
                self.assertEqual(
                    (after.st_dev, after.st_ino, after.st_nlink),
                    (before.st_dev, before.st_ino, before.st_nlink),
                )
                self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
                return pinned_path

            with (
                patch.object(
                    db_migrations,
                    "_database_connection_path_for_directory_descriptor",
                    side_effect=pinned_path_then_checkpoint,
                ),
                patch.object(
                    db_migrations,
                    "_database_connection_path_snapshot",
                    wraps=real_snapshot,
                ) as snapshot_mock,
                closing(factory(str(db_path), check_same_thread=False)) as connection,
            ):
                self.assertEqual(
                    connection.execute("SELECT value FROM events").fetchall(),
                    [("second-seam",)],
                )
            self.assertEqual(snapshot_mock.call_count, 2)

    def test_late_create_failure_closes_transferred_database_fd_once(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
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
                owner_payload={"purpose": "late-custody-failure-probe"},
            ) as lease:
                with (
                    patch.object(
                        MediaforceRuntimeLease,
                        "assert_database_identity_reserved",
                        side_effect=RuntimeError("injected post-transfer failure"),
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "injected post-transfer failure",
                    ),
                ):
                    reserve_mediaforce_database_identity(
                        config,
                        create_if_missing=True,
                    )
                custody = lease._database_custody
                assert custody is not None
                with self.assertRaises(OSError):
                    os.fstat(custody.file_descriptor)
                with self.assertRaisesRegex(
                    MediaforceRuntimeBusyError,
                    "database custody is unavailable",
                ):
                    lease._assert_database_custody_available(db_path)

    def test_custody_close_reports_raw_fd_failure_without_double_close(self) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with closing(sqlite3.connect(db_path)):
                pass
            descriptor = os.open(
                db_path,
                os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
            close_attempts = 0

            def close_descriptor() -> None:
                nonlocal close_attempts
                close_attempts += 1
                raise OSError("injected close failure")

            custody = DatabaseFileCustody(
                db_path,
                file_descriptor=descriptor,
                close_file_descriptor=close_descriptor,
            )
            with self.assertRaisesRegex(OSError, "injected close failure"):
                custody.close()
            os.fstat(descriptor)
            with self.assertRaisesRegex(OSError, "injected close failure"):
                custody.close()
            self.assertEqual(close_attempts, 1)
            os.close(descriptor)

    def test_sqlite_close_failure_keeps_custody_borrow_and_raw_fd_live(
            self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with closing(sqlite3.connect(db_path)):
                pass
            custody = _fixture_database_custody(db_path)
            database_descriptor = custody.file_descriptor
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            connection = factory(str(db_path), check_same_thread=False)
            directory_descriptor = connection._database_identity_descriptors[0]
            try:
                with (
                    patch.object(
                        db_migrations._DatabaseIdentityConnection,
                        "_close_sqlite_connection",
                        side_effect=RuntimeError("SQLite close failed"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "SQLite close failed"),
                ):
                    connection.close()
                self.assertIsNotNone(connection._database_custody_borrow)
                os.fstat(directory_descriptor)
                custody.close()
                os.fstat(database_descriptor)
            finally:
                connection.close()
                custody.close()

            with self.assertRaises(OSError):
                os.fstat(directory_descriptor)
            with self.assertRaises(OSError):
                os.fstat(database_descriptor)

    def test_runtime_lease_defers_database_fd_close_until_connection_closes(
            self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            config_path = root / "config.toml"
            config_path.write_text("config", encoding="utf-8")
            db_path = root / "library.sqlite3"
            with closing(sqlite3.connect(db_path)) as setup_connection:
                setup_connection.execute("CREATE TABLE events (value TEXT)")
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=config_path,
                    db_path=db_path,
                    web_state_dir=root / "state",
                    runtime_reservation_dir=root / "reservations",
                ),
            )

            connection: sqlite3.Connection | None = None
            database_descriptor = -1
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "custody-teardown-probe"},
            ) as lease:
                reserve_mediaforce_database_identity(config)
                custody = lease._database_custody
                assert custody is not None
                database_descriptor = custody.file_descriptor
                factory = database_identity_connection_factory(
                    db_path,
                    Mock(),
                    database_custody=custody,
                )
                assert factory is not None
                connection = factory(str(db_path), check_same_thread=False)

            assert connection is not None
            os.fstat(database_descriptor)
            connection.close()
            with self.assertRaises(OSError):
                os.fstat(database_descriptor)

    def test_transient_parent_rename_is_sticky_during_connection_open(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            database_root = root / "database"
            retired_root = root / "retired"
            database_root.mkdir()
            db_path = database_root / "library.sqlite3"
            with closing(sqlite3.connect(db_path)):
                pass
            custody = _fixture_database_custody(db_path)
            self.addCleanup(custody.close)
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            real_connection = db_migrations._DatabaseIdentityConnection

            def connect_then_restore_parent(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                candidate = real_connection(*args, **kwargs)
                database_root.rename(retired_root)
                retired_root.rename(database_root)
                return candidate

            with (
                patch.object(
                    db_migrations,
                    "_DatabaseIdentityConnection",
                    side_effect=connect_then_restore_parent,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "identity changed during connection",
                ),
            ):
                factory(str(db_path), check_same_thread=False)

    def test_linux_witness_fails_closed_for_ignored_and_malformed_events(
            self,
    ) -> None:
        witness_type = db_custody_module._NamespaceEventWitness
        for payload in (
            struct.pack("iIII", 7, witness_type._IN_IGNORED, 0, 0),
            struct.pack("iIII", -1, witness_type._IN_Q_OVERFLOW, 0, 0),
            b"short",
        ):
            witness = object.__new__(witness_type)
            witness._inotify_descriptor = 41
            witness._inotify_watches = {7}
            witness._violated = False
            with patch.object(
                db_custody_module.os,
                "read",
                return_value=payload,
            ):
                witness._poll_inotify()
            self.assertTrue(witness._violated)

    def test_witness_close_never_retries_an_ambiguously_closed_reused_fd(
            self,
    ) -> None:
        witness_type = db_custody_module._NamespaceEventWitness
        real_close = os.close
        for descriptor_field in ("_inotify_descriptor", "_parent_descriptor"):
            with self.subTest(descriptor_field=descriptor_field):
                original_descriptor = os.open("/dev/null", os.O_RDONLY)
                witness = object.__new__(witness_type)
                witness._watcher = None
                witness._inotify_descriptor = -1
                witness._parent_descriptor = -1
                setattr(witness, descriptor_field, original_descriptor)
                reused_descriptor = -1

                def close_then_reuse(descriptor: int) -> None:
                    nonlocal reused_descriptor
                    real_close(descriptor)
                    reused_descriptor = os.open("/dev/null", os.O_RDONLY)
                    self.assertEqual(reused_descriptor, descriptor)
                    raise OSError("ambiguous close failure")

                with (
                    patch.object(
                        db_custody_module.os,
                        "close",
                        side_effect=close_then_reuse,
                    ),
                    self.assertRaisesRegex(OSError, "ambiguous close failure"),
                ):
                    witness.close()
                witness.close()
                os.fstat(reused_descriptor)
                real_close(reused_descriptor)

    def test_connection_close_and_failed_open_preserve_other_sqlite_locks(
            self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with closing(sqlite3.connect(db_path)) as setup_connection:
                setup_connection.execute("CREATE TABLE events (value TEXT)")
                setup_connection.commit()

            custody = _fixture_database_custody(db_path)
            database_descriptor = custody.file_descriptor
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            connection_b = factory(str(db_path), check_same_thread=False)
            try:
                connection_b.execute("BEGIN EXCLUSIVE")
                connection_b.execute("INSERT INTO events VALUES ('held-lock')")

                with closing(
                    factory(str(db_path), check_same_thread=False)
                ):
                    pass
                os.fstat(database_descriptor)
                self.assertEqual(self._child_write_returncode(db_path), 23)

                with (
                    patch.object(
                        db_migrations,
                        "_DatabaseIdentityConnection",
                        side_effect=sqlite3.OperationalError("failed open"),
                    ),
                    self.assertRaisesRegex(sqlite3.OperationalError, "failed open"),
                ):
                    factory(str(db_path), check_same_thread=False)
                os.fstat(database_descriptor)
                self.assertEqual(self._child_write_returncode(db_path), 23)

                custody.close()
                os.fstat(database_descriptor)
            finally:
                connection_b.rollback()
                connection_b.close()

            with self.assertRaises(OSError):
                os.fstat(database_descriptor)
            self.assertEqual(self._child_write_returncode(db_path), 0)

    def _child_write_returncode(self, db_path: Path) -> int:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_TRY_WRITE, str(db_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode not in (0, 23):
            self.fail(
                f"child SQLite writer failed: {result.returncode}: {result.stderr}"
            )
        return result.returncode

    def test_connection_accepts_wal_checkpoint_after_descriptor_pinning(self) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            with closing(sqlite3.connect(db_path)) as setup_connection:
                self.assertEqual(
                    setup_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                setup_connection.execute("CREATE TABLE events (value TEXT)")
                setup_connection.commit()

            custody = _fixture_database_custody(db_path)
            self.addCleanup(custody.close)
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            real_connection = db_migrations._DatabaseIdentityConnection
            before = db_path.stat()

            def connect_then_checkpoint(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                candidate = real_connection(*args, **kwargs)
                try:
                    with closing(sqlite3.connect(db_path)) as writer:
                        writer.execute("INSERT INTO events VALUES ('checkpointed')")
                        writer.commit()
                        self.assertEqual(
                            writer.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            ).fetchone(),
                            (0, 0, 0),
                        )

                    after = db_path.stat()
                    self.assertEqual(
                        (after.st_dev, after.st_ino, after.st_nlink),
                        (before.st_dev, before.st_ino, before.st_nlink),
                    )
                    self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
                    with closing(sqlite3.connect(db_path)) as observer:
                        self.assertEqual(
                            observer.execute("SELECT value FROM events").fetchall(),
                            [("checkpointed",)],
                        )
                except BaseException:
                    with closing(candidate):
                        raise
                return candidate

            with patch.object(
                db_migrations,
                "_DatabaseIdentityConnection",
                side_effect=connect_then_checkpoint,
            ):
                with closing(
                    factory(str(db_path), check_same_thread=False)
                ) as connection:
                    self.assertEqual(
                        connection.execute("SELECT value FROM events").fetchall(),
                        [("checkpointed",)],
                    )
                    connection.assert_database_identity()

    @unittest.skipUnless(
        sys.platform == "darwin" and hasattr(select, "kqueue"),
        "requires macOS kqueue",
    )
    def test_namespace_kqueue_ignores_wal_writes_and_detects_leaf_swap(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            db_path = root / "library.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            original_path = root / "library-original.sqlite3"
            with closing(sqlite3.connect(db_path)) as setup_connection:
                self.assertEqual(
                    setup_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                setup_connection.execute("CREATE TABLE events (value TEXT)")
                setup_connection.commit()
            with closing(sqlite3.connect(replacement_path)) as replacement:
                replacement.execute("CREATE TABLE replacement (value TEXT)")
                replacement.commit()

            with ExitStack() as resources:
                file_descriptor = os.open(
                    db_path,
                    os.O_RDONLY | os.O_NOFOLLOW,
                )
                resources.callback(os.close, file_descriptor)
                directory_descriptor = os.open(
                    root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                resources.callback(os.close, directory_descriptor)
                watcher = select.kqueue()
                resources.callback(watcher.close)
                file_flags = (
                    select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_LINK
                    | select.KQ_NOTE_REVOKE
                )
                directory_flags = (
                    select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_REVOKE
                )
                watcher.control(
                    [
                        select.kevent(
                            file_descriptor,
                            filter=select.KQ_FILTER_VNODE,
                            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                            fflags=file_flags,
                        ),
                        select.kevent(
                            directory_descriptor,
                            filter=select.KQ_FILTER_VNODE,
                            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                            fflags=directory_flags,
                        ),
                    ],
                    0,
                    0,
                )

                subprocess.run(
                    [sys.executable, "-c", _CHILD_WAL_WRITE, str(db_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(watcher.control([], 8, 0), [])
                with closing(sqlite3.connect(db_path)) as observer:
                    self.assertEqual(
                        observer.execute("SELECT value FROM events").fetchall(),
                        [("checkpointed",), ("fresh-wal-cycle",)],
                    )

                db_path.replace(original_path)
                replacement_path.replace(db_path)
                db_path.replace(replacement_path)
                original_path.replace(db_path)

                events = watcher.control([], 8, 0)
                self.assertTrue(
                    any(
                        event.ident == file_descriptor
                        and event.fflags & file_flags
                        for event in events
                    ),
                    events,
                )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS /.vol path")
    def test_guarded_first_open_recreates_wal_sidecars_through_volume_path(self) -> None:
        with TemporaryDirectory() as raw_root:
            db_path = Path(raw_root) / "library.sqlite3"
            wal_path = Path(f"{db_path}-wal")
            shm_path = Path(f"{db_path}-shm")
            with closing(sqlite3.connect(db_path)) as setup_connection:
                self.assertEqual(
                    setup_connection.execute("PRAGMA journal_mode=WAL").fetchone(),
                    ("wal",),
                )
                setup_connection.execute("CREATE TABLE events (value TEXT)")
                setup_connection.commit()

            self.assertFalse(wal_path.exists())
            self.assertFalse(shm_path.exists())
            custody = _fixture_database_custody(db_path)
            self.addCleanup(custody.close)
            factory = database_identity_connection_factory(
                db_path,
                Mock(),
                database_custody=custody,
            )
            assert factory is not None
            real_connection = db_migrations._DatabaseIdentityConnection

            def connect_through_volume_path(
                    *args: object,
                    **kwargs: object,
            ) -> sqlite3.Connection:
                database = args[0] if args else kwargs["database"]
                self.assertIsInstance(database, str)
                assert isinstance(database, str)
                self.assertTrue(database.startswith("/.vol/"), database)
                return real_connection(*args, **kwargs)

            with patch.object(
                db_migrations,
                "_DatabaseIdentityConnection",
                side_effect=connect_through_volume_path,
            ):
                with closing(
                    factory(str(db_path), check_same_thread=False)
                ) as connection:
                    connection.execute("INSERT INTO events VALUES ('guarded-open')")
                    connection.commit()
                    self.assertTrue(wal_path.is_file())
                    self.assertTrue(shm_path.is_file())
                    self.assertEqual(
                        connection.execute("SELECT value FROM events").fetchall(),
                        [("guarded-open",)],
                    )


if __name__ == "__main__":
    unittest.main()
