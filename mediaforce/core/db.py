from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any
from typing import TypeAlias

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.engine import RowMapping

from mediaforce.core.db_migrations import SQLITE_BUSY_TIMEOUT_MS
from mediaforce.core.db_migrations import create_engine_for_path
from mediaforce.core.db_migrations import run_migrations

DBClient: TypeAlias = Connection
DBRow: TypeAlias = RowMapping


@lru_cache(maxsize=None)
def _load_sql_asset(name: str) -> str:
    resource = files("mediaforce.core").joinpath("sql")
    for part in name.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _engine_for_db_path(db_path_str: str) -> Engine:
    db_path = Path(db_path_str)
    run_migrations(db_path)
    engine = create_engine_for_path(db_path)
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    finally:
        cursor.close()


def connect(db_path: Path) -> Connection:
    engine = _engine_for_db_path(str(db_path.resolve()))
    return engine.connect()


@contextmanager
def open_db(db_path: Path) -> Iterator[Connection]:
    connection = connect(db_path)
    try:
        yield connection
    except BaseException:
        try:
            if connection.in_transaction():
                connection.rollback()
        finally:
            connection.close()
        raise
    else:
        try:
            if connection.in_transaction():
                connection.commit()
        finally:
            connection.close()


def reset_engine_cache() -> None:
    _engine_for_db_path.cache_clear()
