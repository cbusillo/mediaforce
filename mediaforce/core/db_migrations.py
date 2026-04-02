from contextlib import contextmanager
from collections.abc import Iterator
from importlib.resources import as_file
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

SQLITE_BUSY_TIMEOUT_MS = 30_000
INITIAL_REVISION = "20260401_0001"


def database_url(db_path: Path) -> str:
    return str(URL.create("sqlite+pysqlite", database=str(db_path)))


@contextmanager
def _alembic_script_location() -> Iterator[str]:
    resource = files("mediaforce.core").joinpath("alembic")
    with as_file(resource) as path:
        yield str(path)


def create_engine_for_path(db_path: Path) -> Engine:
    return create_engine(
        database_url(db_path),
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
        poolclass=NullPool,
        future=True,
    )


def run_migrations(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for_path(db_path)
    try:
        with engine.connect() as connection:
            if _has_alembic_version_table(connection):
                _upgrade_with_alembic(db_path)
                return
            if _has_user_tables(connection):
                _bootstrap_legacy_schema(connection)
                _stamp_revision(db_path, INITIAL_REVISION)
                _upgrade_with_alembic(db_path)
                return
        _upgrade_with_alembic(db_path)
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


def _bootstrap_legacy_schema(connection: Connection) -> None:
    # Keep this bridge aligned with the initial Alembic revision so future
    # revisions still run normally after the one-time legacy stamp.
    raw_connection = connection.connection.driver_connection
    if raw_connection is None:
        raise RuntimeError("SQLite driver connection is unavailable for legacy schema bootstrap.")
    sqlite_connection = cast(Any, raw_connection)
    schema_sql = files("mediaforce.core").joinpath("sql", "schema.sql").read_text(encoding="utf-8")
    sqlite_connection.executescript(schema_sql)
    _ensure_column(connection, "scan_runs", "scope", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(connection, "scan_runs", "prefixes_json", "TEXT")
    _ensure_column(connection, "scan_runs", "owner_pid", "INTEGER")
    _ensure_column(connection, "scan_runs", "last_progress_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "bypass_schedule", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "encode_jobs", "last_host_json", "TEXT NOT NULL DEFAULT '{}' ".strip())
    _ensure_column(connection, "encode_jobs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "encode_jobs", "leased_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "lease_expires_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "heartbeat_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "worker_id", "TEXT")
    _ensure_column(connection, "encode_jobs", "retry_not_before", "TEXT")
    _ensure_column(connection, "encode_jobs", "waiting_reason", "TEXT")
    _ensure_column(connection, "encode_jobs", "terminal_reason", "TEXT")
    _ensure_column(connection, "encode_jobs", "last_failure_kind", "TEXT")
    _ensure_column(connection, "encode_jobs", "last_failure_at", "TEXT")
    _ensure_column(connection, "encode_jobs", "host_cooldown_until", "TEXT")
    _ensure_column(connection, "encode_jobs", "progress_json", "TEXT")
    connection.commit()


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


def _upgrade_with_alembic(db_path: Path) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(db_path, script_location)
        command.upgrade(config, "head")


def _stamp_revision(db_path: Path, revision: str) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(db_path, script_location)
        command.stamp(config, revision)


def _alembic_config(db_path: Path, script_location: str) -> Config:
    config = Config()
    config.set_main_option("script_location", script_location)
    config.set_main_option("sqlalchemy.url", database_url(db_path))
    return config
