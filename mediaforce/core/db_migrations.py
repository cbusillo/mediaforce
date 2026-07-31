from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import as_file
from importlib.resources import files
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Any, cast
from urllib.parse import quote

# noinspection PyPackageRequirements
from alembic import command
# noinspection PyPackageRequirements
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

SQLITE_BUSY_TIMEOUT_MS = 30_000
INITIAL_REVISION = "20260401_0001"
SQLiteConnectionFactory = Callable[..., sqlite3.Connection]
DatabaseDirectoryIdentity = tuple[int, int]
DatabaseConnectionPathSnapshot = tuple[int, int, int, int]
DatabaseConnectionLifetimeIdentity = tuple[int, int, int]


class _DatabaseIdentityConnection(sqlite3.Connection):
    _database_identity_descriptors: tuple[int, ...] = ()
    _database_identity_validator: Callable[[], bool] | None = None

    def retain_database_identity_descriptors(
            self,
            descriptors: tuple[int, ...],
            validator: Callable[[], bool],
    ) -> None:
        if self._database_identity_descriptors:
            raise RuntimeError(
                "Mediaforce database identity descriptors are already retained"
            )
        self._database_identity_descriptors = descriptors
        self._database_identity_validator = validator

    def assert_database_identity(self) -> None:
        validator = self._database_identity_validator
        if validator is None or not validator():
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )

    def close(self) -> None:
        descriptors = self._database_identity_descriptors
        self._database_identity_descriptors = ()
        self._database_identity_validator = None
        close_error: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            close_error = exc
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError as exc:
                if close_error is None:
                    close_error = exc
                else:
                    close_error.add_note(
                        "Database identity descriptor cleanup also failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if close_error is not None:
            raise close_error


def database_url(
        db_path: Path,
        *,
        require_existing: bool = False,
) -> str:
    mode = "rw" if require_existing else "rwc"
    return _database_uri_url(db_path, mode=mode)


def readonly_database_url(db_path: Path) -> str:
    return _database_uri_url(db_path, mode="ro")


def _database_uri_url(db_path: Path, *, mode: str) -> str:
    quoted_path = quote(os.fspath(db_path), safe="/")
    return f"sqlite+pysqlite:///file:{quoted_path}?mode={mode}&uri=true"


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
        identity_guard()
        expected_parent, expected = _database_connection_path_snapshot(
            resolved_path
        )
        identity_guard()
        pinned_path, descriptors = _pin_database_connection_path(
            resolved_path,
            expected_parent=expected_parent,
            expected=expected,
        )
        connection: _DatabaseIdentityConnection | None = None
        descriptors_retained = False
        try:
            connection_args, connection_kwargs = (
                _database_connection_arguments_for_pinned_path(
                    args,
                    kwargs,
                    pinned_path=pinned_path,
                )
            )
            connection_kwargs.pop("factory", None)
            connection = _DatabaseIdentityConnection(
                *connection_args,
                **connection_kwargs,
            )
            connection.retain_database_identity_descriptors(
                descriptors,
                lambda: _database_connection_path_remains_bound(
                    resolved_path,
                    pinned_path=pinned_path,
                    expected_parent=expected_parent,
                    expected=(
                        expected[0],
                        expected[1],
                        expected[3],
                    ),
                    descriptors=descriptors,
                ),
            )
            descriptors_retained = True
            identity_guard()
            if not _database_connection_path_remains_pinned(
                resolved_path,
                pinned_path=pinned_path,
                expected_parent=expected_parent,
                expected=expected,
                descriptors=descriptors,
            ):
                raise RuntimeError(
                    "Mediaforce database identity changed during connection"
                )
            connection.assert_database_identity()
        except BaseException as exc:
            if connection is not None:
                try:
                    connection.close()
                except BaseException as cleanup_error:
                    exc.add_note(
                        "Database connection cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if not descriptors_retained:
                try:
                    _close_database_identity_descriptors(descriptors)
                except BaseException as cleanup_error:
                    exc.add_note(
                        "Database identity descriptor cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise
        return connection

    return connect


def _database_connection_path_snapshot(
        db_path: Path,
) -> tuple[DatabaseDirectoryIdentity, DatabaseConnectionPathSnapshot]:
    parent_info = db_path.parent.stat(follow_symlinks=False)
    file_info = db_path.stat(follow_symlinks=False)
    return (
        _database_directory_identity(parent_info),
        _database_connection_info_snapshot(file_info),
    )


def _database_directory_identity(
        info: os.stat_result,
) -> DatabaseDirectoryIdentity:
    return info.st_dev, info.st_ino


def _database_connection_info_snapshot(
        info: os.stat_result,
) -> DatabaseConnectionPathSnapshot:
    return (
        info.st_dev,
        info.st_ino,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _database_connection_lifetime_info_snapshot(
        info: os.stat_result,
) -> DatabaseConnectionLifetimeIdentity:
    return info.st_dev, info.st_ino, info.st_nlink


def _pin_database_connection_path(
        db_path: Path,
        *,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionPathSnapshot,
) -> tuple[Path, tuple[int, int]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    file_flags |= os.O_NOFOLLOW
    try:
        directory_descriptor = os.open(db_path.parent, directory_flags)
    except OSError as exc:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        ) from exc
    try:
        directory_info = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or _database_directory_identity(directory_info) != expected_parent
        ):
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )
    except BaseException:
        os.close(directory_descriptor)
        raise
    try:
        file_descriptor = os.open(
            db_path.name,
            file_flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        os.close(directory_descriptor)
        raise RuntimeError(
            "Mediaforce database identity changed during connection"
        ) from exc
    descriptors = (file_descriptor, directory_descriptor)
    try:
        file_info = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or _database_connection_info_snapshot(file_info) != expected
        ):
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )
        pinned_path = _database_connection_path_for_directory_descriptor(
            directory_descriptor,
            directory_info=directory_info,
            filename=db_path.name,
        )
        pinned_info = pinned_path.stat()
        if (
            not stat.S_ISREG(pinned_info.st_mode)
            or _database_connection_info_snapshot(pinned_info) != expected
        ):
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )
    except BaseException:
        _close_database_identity_descriptors(descriptors)
        raise
    return pinned_path, descriptors


def _database_connection_path_for_directory_descriptor(
        directory_descriptor: int,
        *,
        directory_info: os.stat_result,
        filename: str,
) -> Path:
    if sys.platform == "darwin":
        pinned_directory = (
            Path("/.vol")
            / str(directory_info.st_dev)
            / str(directory_info.st_ino)
        )
    elif sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        pinned_directory = Path("/proc/self/fd") / str(directory_descriptor)
    else:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    pinned_path = pinned_directory / filename
    try:
        pinned_parent_info = pinned_directory.stat()
    except OSError as exc:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        ) from exc
    if (
        pinned_parent_info.st_dev,
        pinned_parent_info.st_ino,
    ) != (
        directory_info.st_dev,
        directory_info.st_ino,
    ):
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    return pinned_path


def _database_connection_arguments_for_pinned_path(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        pinned_path: Path,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    connection_args = list(args)
    connection_kwargs = dict(kwargs)
    if connection_args:
        database = connection_args[0]
    elif "database" in connection_kwargs:
        database = connection_kwargs["database"]
    else:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    database_text = os.fspath(database)
    if not isinstance(database_text, str):
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    pinned_text = os.fspath(pinned_path)
    if database_text.startswith("file:"):
        if connection_kwargs.get("uri") is not True:
            raise RuntimeError(
                "Mediaforce database actual-opened identity inspection is unavailable"
            )
        _, separator, query = database_text.partition("?")
        pinned_database = f"file:{quote(pinned_text, safe='/')}"
        if separator:
            pinned_database = f"{pinned_database}?{query}"
    else:
        pinned_database = pinned_text
    if connection_args:
        connection_args[0] = pinned_database
    else:
        connection_kwargs["database"] = pinned_database
    return tuple(connection_args), connection_kwargs


def _database_connection_path_remains_pinned(
        db_path: Path,
        *,
        pinned_path: Path,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionPathSnapshot,
        descriptors: tuple[int, int],
) -> bool:
    return _database_connection_path_matches(
        db_path,
        pinned_path=pinned_path,
        expected_parent=expected_parent,
        expected=expected,
        descriptors=descriptors,
        snapshot=_database_connection_info_snapshot,
    )


def _database_connection_path_remains_bound(
        db_path: Path,
        *,
        pinned_path: Path,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionLifetimeIdentity,
        descriptors: tuple[int, int],
) -> bool:
    return _database_connection_path_matches(
        db_path,
        pinned_path=pinned_path,
        expected_parent=expected_parent,
        expected=expected,
        descriptors=descriptors,
        snapshot=_database_connection_lifetime_info_snapshot,
    )


def _database_connection_path_matches(
        db_path: Path,
        *,
        pinned_path: Path,
        expected_parent: DatabaseDirectoryIdentity,
        expected: tuple[int, ...],
        descriptors: tuple[int, int],
        snapshot: Callable[[os.stat_result], tuple[int, ...]],
) -> bool:
    file_descriptor, directory_descriptor = descriptors
    try:
        descriptor_info = os.fstat(file_descriptor)
        directory_descriptor_info = os.fstat(directory_descriptor)
        relative_info = os.stat(
            db_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        path_info = db_path.stat(follow_symlinks=False)
        parent_info = db_path.parent.stat(follow_symlinks=False)
        pinned_info = pinned_path.stat(follow_symlinks=False)
        pinned_parent_info = pinned_path.parent.stat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(directory_descriptor_info.st_mode)
        and stat.S_ISDIR(parent_info.st_mode)
        and stat.S_ISDIR(pinned_parent_info.st_mode)
        and all(
            _database_directory_identity(info) == expected_parent
            for info in (
                directory_descriptor_info,
                parent_info,
                pinned_parent_info,
            )
        )
        and all(
            stat.S_ISREG(info.st_mode)
            and snapshot(info) == expected
            for info in (
                descriptor_info,
                relative_info,
                path_info,
                pinned_info,
            )
        )
    )


def _close_database_identity_descriptors(
        descriptors: tuple[int, ...],
) -> None:
    close_error: OSError | None = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as exc:
            if close_error is None:
                close_error = exc
    if close_error is not None:
        raise close_error


def register_database_identity_guards(
        engine: Engine,
        identity_guard: Callable[[], None] | None,
) -> None:
    if identity_guard is None:
        return

    def assert_driver_connection_identity(
            driver_connection: Any,
    ) -> None:
        assert_identity = getattr(
            driver_connection,
            "assert_database_identity",
            None,
        )
        if callable(assert_identity):
            assert_identity()

    def assert_connected_identity(
            dbapi_connection: Any,
            _record: Any,
    ) -> None:
        identity_guard()
        assert_driver_connection_identity(dbapi_connection)

    def assert_cursor_identity(
            connection: Connection,
            *_args: Any,
            **_kwargs: Any,
    ) -> None:
        identity_guard()
        assert_driver_connection_identity(
            connection.connection.driver_connection
        )

    event.listen(
        engine,
        "connect",
        assert_connected_identity,
    )
    event.listen(
        engine,
        "before_cursor_execute",
        assert_cursor_identity,
    )
    event.listen(
        engine,
        "after_cursor_execute",
        assert_cursor_identity,
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
    _ensure_column(connection, "scan_runs", "status", "TEXT NOT NULL DEFAULT 'running'")
    _ensure_column(connection, "scan_runs", "error", "TEXT")
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
    sqlalchemy_url = database_url(
        db_path,
        require_existing=identity_guard is not None,
    ).replace("%", "%%")
    config.set_main_option(
        "sqlalchemy.url",
        sqlalchemy_url,
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
