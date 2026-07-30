from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any
from typing import TypeAlias

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.engine import RowMapping

from mediaforce.core.db_migrations import SQLITE_BUSY_TIMEOUT_MS
from mediaforce.core.db_migrations import create_engine_for_path
from mediaforce.core.db_migrations import run_migrations

DBClient: TypeAlias = Connection
DBRow: TypeAlias = RowMapping
DatabaseLeaseIdentity = tuple[int, int, int]
DatabaseIdentityProvider = Callable[
    [Path, DatabaseLeaseIdentity | None],
    DatabaseLeaseIdentity | None,
]

_DATABASE_IDENTITY_PROVIDER: DatabaseIdentityProvider | None = None


@lru_cache(maxsize=None)
def _load_sql_asset(name: str) -> str:
    resource = files("mediaforce.core").joinpath("sql")
    for part in name.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _engine_for_db_path(
        db_path_str: str,
        reserved_identity: DatabaseLeaseIdentity | None,
) -> Engine:
    db_path = Path(db_path_str)
    run_migrations(
        db_path,
        identity_guard=_database_identity_guard(
            db_path,
            reserved_identity,
        ),
    )
    return create_engine_for_path(
        db_path,
        identity_guard=_database_identity_guard(
            db_path,
            reserved_identity,
        ),
    )


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    finally:
        cursor.close()


def connect(db_path: Path) -> Connection:
    resolved_path = db_path.expanduser().resolve()
    reserved_lease_identity = _assert_writable_database_identity_reserved(
        resolved_path
    )
    return _connect_with_reserved_identity(
        resolved_path,
        reserved_lease_identity,
    )


def _connect_with_reserved_identity(
        resolved_path: Path,
        reserved_lease_identity: DatabaseLeaseIdentity | None,
) -> Connection:
    identity_guard = _database_identity_guard(
        resolved_path,
        reserved_lease_identity,
    )
    engine = _engine_for_db_path(
        str(resolved_path),
        reserved_lease_identity,
    )
    if identity_guard is not None:
        identity_guard()
    connection = engine.connect()
    try:
        if identity_guard is not None:
            identity_guard()
        _configure_sqlite_connection(
            connection.connection.driver_connection,
            None,
        )
        if identity_guard is not None:
            identity_guard()
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def open_db(db_path: Path) -> Iterator[Connection]:
    resolved_path = db_path.expanduser().resolve()
    reserved_lease_identity = _assert_writable_database_identity_reserved(
        resolved_path
    )
    identity_guard = _database_identity_guard(
        resolved_path,
        reserved_lease_identity,
    )
    connection = _connect_with_reserved_identity(
        resolved_path,
        reserved_lease_identity,
    )
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
            if identity_guard is not None:
                identity_guard()
            if connection.in_transaction():
                connection.commit()
        finally:
            connection.close()


@contextmanager
def open_readonly_db(db_path: Path) -> Iterator[Connection]:
    resolved_path = db_path.expanduser().resolve()
    reserved_lease_identity = _assert_writable_database_identity_reserved(
        resolved_path
    )
    identity_guard = _database_identity_guard(
        resolved_path,
        reserved_lease_identity,
    )
    if identity_guard is not None:
        identity_guard()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Mediaforce database does not exist: {resolved_path}")
    engine = create_engine(f"sqlite+pysqlite:///file:{resolved_path}?mode=ro&uri=true")
    if identity_guard is not None:
        event.listen(
            engine,
            "connect",
            lambda _connection, _record: identity_guard(),
        )
    connection = engine.connect()
    try:
        if identity_guard is not None:
            identity_guard()
        connection.exec_driver_sql("PRAGMA query_only=ON")
        if identity_guard is not None:
            identity_guard()
        yield connection
        if identity_guard is not None:
            identity_guard()
    finally:
        connection.close()
        engine.dispose()


def reset_engine_cache() -> None:
    _engine_for_db_path.cache_clear()


def register_database_identity_provider(
        provider: DatabaseIdentityProvider,
) -> None:
    global _DATABASE_IDENTITY_PROVIDER
    if (
        _DATABASE_IDENTITY_PROVIDER is not None
        and _DATABASE_IDENTITY_PROVIDER is not provider
    ):
        raise RuntimeError("Mediaforce database identity provider is already registered")
    _DATABASE_IDENTITY_PROVIDER = provider


def _assert_writable_database_identity_reserved(
        db_path: Path,
        expected_identity: DatabaseLeaseIdentity | None = None,
) -> DatabaseLeaseIdentity | None:
    provider = _DATABASE_IDENTITY_PROVIDER
    if provider is None:
        return None
    return provider(db_path, expected_identity)


def _database_identity_guard(
        db_path: Path,
        expected_identity: DatabaseLeaseIdentity | None,
) -> Callable[[], None] | None:
    if expected_identity is None:
        return None

    def assert_identity() -> None:
        _assert_writable_database_identity_reserved(
            db_path,
            expected_identity,
        )

    return assert_identity
