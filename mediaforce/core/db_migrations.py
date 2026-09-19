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

from mediaforce.core.db_custody import DatabaseCustodyBorrow
from mediaforce.core.db_custody import DatabaseCustodyError
from mediaforce.core.db_custody import DatabaseFileCustody

SQLITE_BUSY_TIMEOUT_MS = 30_000
INITIAL_REVISION = "20260401_0001"
SQLiteConnectionFactory = Callable[..., sqlite3.Connection]
DatabaseDirectoryIdentity = tuple[int, int]
DatabaseConnectionPathSnapshot = tuple[int, int, int, int]
DatabaseConnectionLifetimeIdentity = tuple[int, int, int]


class _DatabaseIdentityConnection(sqlite3.Connection):
    _database_identity_descriptors: tuple[int, ...] = ()
    _database_identity_validator: Callable[[], str | None] | None = None
    _database_custody_borrow: DatabaseCustodyBorrow | None = None

    def retain_database_identity_descriptors(
            self,
            descriptors: tuple[int, ...],
            validator: Callable[[], str | None],
            custody_borrow: DatabaseCustodyBorrow,
    ) -> None:
        if (
            self._database_identity_descriptors
            or self._database_custody_borrow is not None
        ):
            raise RuntimeError(
                "Mediaforce database identity descriptors are already retained"
            )
        self._database_identity_descriptors = descriptors
        self._database_identity_validator = validator
        self._database_custody_borrow = custody_borrow

    def assert_database_identity(self) -> None:
        validator = self._database_identity_validator
        failure_detail = validator() if validator is not None else "validator missing"
        if failure_detail is not None:
            error = RuntimeError(
                "Mediaforce database identity changed during connection"
            )
            error.add_note(f"Database custody detail: {failure_detail}")
            raise error

    def close(self) -> None:
        self._close_sqlite_connection()
        descriptors = self._database_identity_descriptors
        custody_borrow = self._database_custody_borrow
        self._database_identity_descriptors = ()
        self._database_identity_validator = None
        self._database_custody_borrow = None
        close_error: BaseException | None = None
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
        if custody_borrow is not None:
            try:
                custody_borrow.release()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
                else:
                    close_error.add_note(
                        "Database custody borrow cleanup also failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if close_error is not None:
            raise close_error

    def _close_sqlite_connection(self) -> None:
        super().close()


class _DatabaseConnectionVolatileMetadataChanged(RuntimeError):
    pass


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
        database_custody: DatabaseFileCustody | None = None,
) -> Engine:
    connect_args: dict[str, Any] = {
        "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
    }
    connection_factory = database_identity_connection_factory(
        db_path,
        identity_guard,
        database_custody=database_custody,
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
        *,
        database_custody: DatabaseFileCustody | None = None,
) -> SQLiteConnectionFactory | None:
    if identity_guard is None:
        return None
    if database_custody is None:
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    resolved_path = db_path.expanduser().resolve()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        identity_guard()
        try:
            custody_borrow = database_custody.borrow()
        except DatabaseCustodyError as exc:
            raise _database_identity_changed(
                "lease custody was unavailable before SQLite open",
                cause=exc,
            ) from exc
        connection: _DatabaseIdentityConnection | None = None
        directory_descriptor = -1
        resources_retained = False
        try:
            for attempt in range(3):
                expected_parent, expected = _database_connection_path_snapshot(
                    resolved_path
                )
                identity_guard()
                custody_borrow.assert_quiet()
                try:
                    # Concurrent SQLite writes and checkpoints move ctime on the same inode, so
                    # sustained churn is not replacement: the last attempt pins on the stable
                    # device/inode/link-count identity, which every attempt still enforces.
                    pinned_path, directory_descriptor = (
                        _pin_database_connection_path(
                            resolved_path,
                            file_descriptor=custody_borrow.file_descriptor,
                            expected_parent=expected_parent,
                            expected=expected,
                            tolerate_volatile_change=attempt == 2,
                        )
                    )
                    break
                except _DatabaseConnectionVolatileMetadataChanged:
                    continue
            else:
                raise AssertionError(
                    "database identity pin retry loop did not terminate"
                )
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
                (directory_descriptor,),
                lambda: _database_connection_validation_detail(
                    resolved_path,
                    pinned_path=pinned_path,
                    expected_parent=expected_parent,
                    expected=(
                        expected[0],
                        expected[1],
                        expected[3],
                    ),
                    descriptors=(
                        custody_borrow.file_descriptor,
                        directory_descriptor,
                    ),
                    custody_borrow=custody_borrow,
                ),
                custody_borrow,
            )
            resources_retained = True
            identity_guard()
            connection.assert_database_identity()
        except BaseException as exc:
            failure = (
                _database_identity_changed(
                    "lease custody reported a namespace event or binding change",
                    cause=exc,
                )
                if isinstance(exc, DatabaseCustodyError)
                else exc
            )
            if connection is not None:
                try:
                    connection.close()
                except BaseException as cleanup_error:
                    failure.add_note(
                        "Database connection cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if not resources_retained:
                try:
                    _release_database_connection_resources(
                        directory_descriptor,
                        custody_borrow,
                    )
                except BaseException as cleanup_error:
                    failure.add_note(
                        "Database custody resource cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if failure is not exc:
                raise failure from exc
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
        file_descriptor: int,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionPathSnapshot,
        tolerate_volatile_change: bool = False,
) -> tuple[Path, int]:
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "Mediaforce database actual-opened identity inspection is unavailable"
        )
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
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
        file_info = os.fstat(file_descriptor)
        observed = _database_connection_info_snapshot(file_info)
        if not stat.S_ISREG(file_info.st_mode) or (observed[0], observed[1], observed[3]) != (
            expected[0], expected[1], expected[3]
        ):
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )
        if observed[2] != expected[2] and not tolerate_volatile_change:
            raise _DatabaseConnectionVolatileMetadataChanged
        pinned_path = _database_connection_path_for_directory_descriptor(
            directory_descriptor,
            directory_info=directory_info,
            filename=db_path.name,
        )
        pinned_info = pinned_path.stat()
        pinned_observed = _database_connection_info_snapshot(pinned_info)
        if not stat.S_ISREG(pinned_info.st_mode) or (
            pinned_observed[0],
            pinned_observed[1],
            pinned_observed[3],
        ) != (
            expected[0],
            expected[1],
            expected[3],
        ):
            raise RuntimeError(
                "Mediaforce database identity changed during connection"
            )
        if pinned_observed[2] != expected[2] and not tolerate_volatile_change:
            raise _DatabaseConnectionVolatileMetadataChanged
    except BaseException:
        os.close(directory_descriptor)
        raise
    return pinned_path, directory_descriptor


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


def _database_connection_validation_detail(
        db_path: Path,
        *,
        pinned_path: Path,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionLifetimeIdentity,
        descriptors: tuple[int, int],
        custody_borrow: DatabaseCustodyBorrow,
) -> str | None:
    try:
        custody_borrow.assert_quiet()
    except DatabaseCustodyError as exc:
        return f"namespace witness failed: {type(exc).__name__}: {exc}"
    if _database_connection_path_remains_bound(
        db_path,
        pinned_path=pinned_path,
        expected_parent=expected_parent,
        expected=expected,
        descriptors=descriptors,
    ):
        return None
    return _database_connection_path_diagnostics(
        db_path,
        pinned_path=pinned_path,
        expected_parent=expected_parent,
        expected=expected,
        descriptors=descriptors,
    )


def _release_database_connection_resources(
        directory_descriptor: int,
        custody_borrow: DatabaseCustodyBorrow,
) -> None:
    cleanup_error: BaseException | None = None
    if directory_descriptor >= 0:
        try:
            os.close(directory_descriptor)
        except BaseException as exc:
            cleanup_error = exc
    try:
        custody_borrow.release()
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
        else:
            cleanup_error.add_note(
                "Database custody borrow cleanup also failed: "
                f"{type(exc).__name__}: {exc}"
            )
    if cleanup_error is not None:
        raise cleanup_error


def _database_identity_changed(
        detail: str,
        *,
        cause: BaseException | None = None,
) -> RuntimeError:
    error = RuntimeError(
        "Mediaforce database identity changed during connection"
    )
    cause_detail = ""
    if cause is not None:
        cause_detail = f"; cause={type(cause).__name__}: {cause}"
    error.add_note(f"Database custody detail: {detail}{cause_detail}")
    return error


def _database_connection_path_diagnostics(
        db_path: Path,
        *,
        pinned_path: Path,
        expected_parent: DatabaseDirectoryIdentity,
        expected: DatabaseConnectionLifetimeIdentity,
        descriptors: tuple[int, int],
) -> str:
    file_descriptor, directory_descriptor = descriptors
    observations: list[str] = []

    def observe(label: str, loader: Callable[[], os.stat_result]) -> None:
        try:
            info = loader()
        except OSError as exc:
            observations.append(f"{label}=unavailable({exc.errno})")
            return
        identity = (
            _database_directory_identity(info)
            if stat.S_ISDIR(info.st_mode)
            else _database_connection_lifetime_info_snapshot(info)
        )
        observations.append(f"{label}={identity}")

    observe("database_fd", lambda: os.fstat(file_descriptor))
    observe("directory_fd", lambda: os.fstat(directory_descriptor))
    observe(
        "relative_leaf",
        lambda: os.stat(
            db_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        ),
    )
    observe("path", lambda: db_path.stat(follow_symlinks=False))
    observe("parent", lambda: db_path.parent.stat(follow_symlinks=False))
    observe("pinned", lambda: pinned_path.stat(follow_symlinks=False))
    observe("pinned_parent", lambda: pinned_path.parent.stat())
    return (
        f"expected_file={expected}; expected_parent={expected_parent}; "
        + "; ".join(observations)
    )


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
        database_custody: DatabaseFileCustody | None = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _run_identity_guard(identity_guard)
    engine = create_engine_for_path(
        db_path,
        identity_guard=identity_guard,
        database_custody=database_custody,
    )
    try:
        with engine.connect() as connection:
            _run_identity_guard(identity_guard)
            if _has_alembic_version_table(connection):
                _upgrade_with_alembic(
                    db_path,
                    identity_guard=identity_guard,
                    database_custody=database_custody,
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
                    database_custody=database_custody,
                )
                _upgrade_with_alembic(
                    db_path,
                    identity_guard=identity_guard,
                    database_custody=database_custody,
                )
                _run_identity_guard(identity_guard)
                return
        _run_identity_guard(identity_guard)
        _upgrade_with_alembic(
            db_path,
            identity_guard=identity_guard,
            database_custody=database_custody,
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
        database_custody: DatabaseFileCustody | None = None,
) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(
            db_path,
            script_location,
            identity_guard=identity_guard,
            database_custody=database_custody,
        )
        command.upgrade(config, "head")


def _stamp_revision(
        db_path: Path,
        revision: str,
        *,
        identity_guard: Callable[[], None] | None = None,
        database_custody: DatabaseFileCustody | None = None,
) -> None:
    with _alembic_script_location() as script_location:
        config = _alembic_config(
            db_path,
            script_location,
            identity_guard=identity_guard,
            database_custody=database_custody,
        )
        command.stamp(config, revision)


def _alembic_config(
        db_path: Path,
        script_location: str,
        *,
        identity_guard: Callable[[], None] | None = None,
        database_custody: DatabaseFileCustody | None = None,
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
            database_identity_connection_factory(
                db_path,
                identity_guard,
                database_custody=database_custody,
            )
        )
    return config


def _run_identity_guard(
        identity_guard: Callable[[], None] | None,
) -> None:
    if identity_guard is not None:
        identity_guard()
