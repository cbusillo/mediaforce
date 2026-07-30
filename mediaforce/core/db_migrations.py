from collections.abc import Callable, Iterator
from contextlib import contextmanager
import importlib
from importlib.resources import as_file
from importlib.resources import files
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
from typing import Any, cast

# noinspection PyPackageRequirements
from alembic import command
# noinspection PyPackageRequirements
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

SQLITE_BUSY_TIMEOUT_MS = 30_000
INITIAL_REVISION = "20260401_0001"
SQLiteConnectionFactory = Callable[..., sqlite3.Connection]
DatabaseConnectionPathSnapshot = tuple[int, int, int, int]
DatabaseConnectionDescriptorSnapshot = tuple[int, int, int, int, str]
_DATABASE_CONNECTION_IDENTITY_LOCK = threading.RLock()


class _DatabaseIdentityConnection(sqlite3.Connection):
    def close(self) -> None:
        with _DATABASE_CONNECTION_IDENTITY_LOCK:
            super().close()


def database_url(
        db_path: Path,
        *,
        require_existing: bool = False,
) -> str:
    if require_existing:
        return f"sqlite+pysqlite:///file:{db_path}?mode=rw&uri=true"
    return str(URL.create("sqlite+pysqlite", database=str(db_path)))


@contextmanager
def _alembic_script_location() -> Iterator[str]:
    resource = files("mediaforce.core").joinpath("db_migration_scripts")
    with as_file(resource) as path:
        yield str(path)


def create_engine_for_path(
        db_path: Path,
        *,
    identity_guard: Callable[[], None] | None = None,
) -> Engine:
    connect_args: dict[str, Any] = {
        "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
    }
    connection_factory = database_identity_connection_factory(
        db_path,
        identity_guard,
    )
    if connection_factory is not None:
        connect_args["factory"] = connection_factory
    engine = create_engine(
        database_url(
            db_path,
            require_existing=identity_guard is not None,
        ),
        connect_args=connect_args,
        poolclass=NullPool,
        future=True,
    )
    register_database_identity_guards(engine, identity_guard)
    return engine


def database_identity_connection_factory(
        db_path: Path,
        identity_guard: Callable[[], None] | None,
) -> SQLiteConnectionFactory | None:
    if identity_guard is None:
        return None
    resolved_path = db_path.expanduser().resolve()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        with _DATABASE_CONNECTION_IDENTITY_LOCK:
            identity_guard()
            before = _database_connection_path_snapshot(resolved_path)
            identity_guard()
            before_descriptors = _database_connection_descriptor_snapshot()
            kwargs.pop("factory", None)
            connection = _DatabaseIdentityConnection(*args, **kwargs)
            try:
                after_descriptors = _database_connection_descriptor_snapshot()
                identity_guard()
                after = _database_connection_path_snapshot(resolved_path)
                if after != before or not _database_connection_opened_expected_file(
                    resolved_path,
                    expected=before,
                    before=before_descriptors,
                    after=after_descriptors,
                ):
                    raise RuntimeError(
                        "Mediaforce database identity changed during connection"
                    )
            except BaseException:
                connection.close()
                raise
            return connection

    return connect


def _database_connection_path_snapshot(
        db_path: Path,
) -> DatabaseConnectionPathSnapshot:
    info = db_path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _database_connection_descriptor_snapshot(
) -> dict[int, DatabaseConnectionDescriptorSnapshot]:
    if sys.platform == "darwin":
        try:
            fcntl = importlib.import_module("fcntl")
        except ImportError as exc:
            raise RuntimeError(
                "Mediaforce database actual-opened identity inspection is unavailable"
            ) from exc
        if not hasattr(fcntl, "F_GETPATH"):
            raise RuntimeError(
                "Mediaforce database actual-opened identity inspection is unavailable"
            )
        descriptor_directory = Path("/dev/fd")

        def descriptor_path(descriptor: int) -> str:
            raw_path = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                b"\0" * 1024,
            )
            return os.fsdecode(raw_path.split(b"\0", 1)[0])
    elif sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        descriptor_directory = Path("/proc/self/fd")

        def descriptor_path(descriptor: int) -> str:
            return os.readlink(descriptor_directory / str(descriptor))
    else:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )

    try:
        descriptor_names = os.listdir(descriptor_directory)
    except OSError as exc:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        ) from exc
    snapshot: dict[int, DatabaseConnectionDescriptorSnapshot] = {}
    for name in descriptor_names:
        if not name.isdecimal():
            continue
        descriptor = int(name)
        try:
            info = os.fstat(descriptor)
            path = descriptor_path(descriptor)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            snapshot[descriptor] = (
                info.st_dev,
                info.st_ino,
                info.st_ctime_ns,
                info.st_nlink,
                path,
            )
    return snapshot


def _database_connection_opened_expected_file(
        db_path: Path,
        *,
        expected: DatabaseConnectionPathSnapshot,
        before: dict[int, DatabaseConnectionDescriptorSnapshot],
        after: dict[int, DatabaseConnectionDescriptorSnapshot],
) -> bool:
    expected_descriptor = (*expected, str(db_path))
    opened_expected_descriptors = [
        identity
        for descriptor, identity in after.items()
        if before.get(descriptor) != identity
        and identity == expected_descriptor
    ]
    return opened_expected_descriptors == [expected_descriptor]


def register_database_identity_guards(
        engine: Engine,
        identity_guard: Callable[[], None] | None,
) -> None:
    if identity_guard is None:
        return
    event.listen(
        engine,
        "connect",
        lambda _connection, _record: identity_guard(),
    )
    event.listen(
        engine,
        "before_cursor_execute",
        lambda *_args, **_kwargs: identity_guard(),
    )
    event.listen(
        engine,
        "after_cursor_execute",
        lambda *_args, **_kwargs: identity_guard(),
    )


def run_migrations(
        db_path: Path,
        *,
        identity_guard: Callable[[], None] | None = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _run_identity_guard(identity_guard)
    engine = create_engine_for_path(
        db_path,
        identity_guard=identity_guard,
    )
    try:
        with engine.connect() as connection:
            _run_identity_guard(identity_guard)
            if _has_alembic_version_table(connection):
                _upgrade_with_alembic(
                    db_path,
                    identity_guard=identity_guard,
                )
                _run_identity_guard(identity_guard)
                return
            if _has_user_tables(connection):
                _bootstrap_legacy_schema(
                    connection,
                    identity_guard=identity_guard,
                )
                _run_identity_guard(identity_guard)
                _stamp_revision(
                    db_path,
                    INITIAL_REVISION,
                    identity_guard=identity_guard,
                )
                _upgrade_with_alembic(
                    db_path,
                    identity_guard=identity_guard,
                )
                _run_identity_guard(identity_guard)
                return
        _run_identity_guard(identity_guard)
        _upgrade_with_alembic(
            db_path,
            identity_guard=identity_guard,
        )
        _run_identity_guard(identity_guard)
    finally:
        engine.dispose()


def _has_alembic_version_table(connection: Connection) -> bool:
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
    ).fetchone()
    return row is not None


def _has_user_tables(connection: Connection) -> bool:
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _bootstrap_legacy_schema(
        connection: Connection,
        *,
        identity_guard: Callable[[], None] | None = None,
) -> None:
    # Keep this bridge aligned with the initial Alembic revision so future
    # revisions still run normally after the one-time legacy stamp.
    raw_connection = connection.connection.driver_connection
    if raw_connection is None:
        raise RuntimeError("SQLite driver connection is unavailable for legacy schema bootstrap.")
    sqlite_connection = cast(Any, raw_connection)
    schema_sql = files("mediaforce.core").joinpath("sql", "schema.sql").read_text(encoding="utf-8")
    _run_identity_guard(identity_guard)
    sqlite_connection.executescript(schema_sql)
    _run_identity_guard(identity_guard)
    _ensure_column(connection, "scan_runs", "scope", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(connection, "scan_runs", "prefixes_json", "TEXT")
    _ensure_column(connection, "scan_runs", "owner_pid", "INTEGER")
    _ensure_column(connection, "scan_runs", "last_progress_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "bypass_schedule", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "encode_jobs", "last_host_json", "TEXT NOT NULL DEFAULT '{}' ".strip())
    _ensure_column(connection, "encode_jobs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "encode_jobs", "job_kind", "TEXT NOT NULL DEFAULT 'single'")
    _ensure_column(connection, "encode_jobs", "parent_job_id", "TEXT")
    _ensure_column(connection, "encode_jobs", "leased_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "lease_expires_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "heartbeat_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "manifest_indexes_json", "TEXT")
    _ensure_column(connection, "encode_jobs", "worker_id", "TEXT")
    _ensure_column(connection, "encode_jobs", "schedule_close_deadline_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "retry_not_before", "TEXT")
    _ensure_column(connection, "encode_jobs", "waiting_reason", "TEXT")
    _ensure_column(connection, "encode_jobs", "terminal_reason", "TEXT")
    _ensure_column(connection, "encode_jobs", "last_failure_kind", "TEXT")
    _ensure_column(connection, "encode_jobs", "last_failure_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "host_cooldown_until", "TEXT")
    _ensure_column(connection, "encode_jobs", "progress_json", "TEXT")
    _ensure_column(connection, "library_items", "cadence_summary_json", "TEXT")
    _ensure_column(connection, "library_items", "media_fingerprint_json", "TEXT")
    _run_identity_guard(identity_guard)
    connection.commit()
    _run_identity_guard(identity_guard)


def _ensure_column(connection: Connection, table_name: str, column_name: str, column_definition: str) -> None:
    existing = {
        str(row[1])
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in existing:
        return
    connection.exec_driver_sql(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
    )


def _upgrade_with_alembic(
        db_path: Path,
        *,
        identity_guard: Callable[[], None] | None = None,
) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(
            db_path,
            script_location,
            identity_guard=identity_guard,
        )
        command.upgrade(config, "head")


def _stamp_revision(
        db_path: Path,
        revision: str,
        *,
        identity_guard: Callable[[], None] | None = None,
) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(
            db_path,
            script_location,
            identity_guard=identity_guard,
        )
        command.stamp(config, revision)


def _alembic_config(
        db_path: Path,
        script_location: str,
        *,
        identity_guard: Callable[[], None] | None = None,
) -> Config:
    config = Config()
    config.set_main_option("script_location", script_location)
    config.set_main_option(
        "sqlalchemy.url",
        database_url(
            db_path,
            require_existing=identity_guard is not None,
        ),
    )
    if identity_guard is not None:
        config.attributes["database_identity_guard"] = identity_guard
        config.attributes["database_identity_connection_factory"] = (
            database_identity_connection_factory(db_path, identity_guard)
        )
    return config


def _run_identity_guard(
        identity_guard: Callable[[], None] | None,
) -> None:
    if identity_guard is not None:
        identity_guard()
