import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Iterator

SQLITE_BUSY_TIMEOUT_MS = 30_000


@lru_cache(maxsize=None)
def _load_sql_asset(name: str) -> str:
    resource = files("mediaforce.core").joinpath("sql")
    for part in name.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def _execute_sql_asset(connection: sqlite3.Connection, name: str) -> None:
    connection.executescript(_load_sql_asset(name))


SCHEMA = _load_sql_asset("schema.sql")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")
    connection.executescript(SCHEMA)
    _ensure_scan_run_columns(connection)
    _ensure_encode_queue_columns(connection)
    return connection


def _ensure_scan_run_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
    }
    migrations = {
        "scope": "migrations/scan_runs_add_scope.sql",
        "prefixes_json": "migrations/scan_runs_add_prefixes_json.sql",
        "owner_pid": "migrations/scan_runs_add_owner_pid.sql",
        "last_progress_at": "migrations/scan_runs_add_last_progress_at.sql",
    }
    for column_name, migration_name in migrations.items():
        if column_name in existing:
            continue
        _execute_sql_asset(connection, migration_name)


def _ensure_encode_queue_columns(connection: sqlite3.Connection) -> None:
    encode_job_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(encode_jobs)").fetchall()
    }
    column_defaults = {
        "bypass_schedule": "INTEGER NOT NULL DEFAULT 0",
        "last_host_json": "TEXT NOT NULL DEFAULT '{}'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "leased_at": "TEXT",
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "worker_id": "TEXT",
        "retry_not_before": "TEXT",
        "waiting_reason": "TEXT",
        "terminal_reason": "TEXT",
        "last_failure_kind": "TEXT",
        "last_failure_at": "TEXT",
        "host_cooldown_until": "TEXT",
        "progress_json": "TEXT",
    }
    for column_name, column_definition in column_defaults.items():
        if column_name in encode_job_columns:
            continue
        connection.execute(
            f"ALTER TABLE encode_jobs ADD COLUMN {column_name} {column_definition}"
        )


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()
