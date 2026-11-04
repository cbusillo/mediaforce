import threading
from hashlib import sha256
from pickle import dumps
from sqlite3 import Connection as SQLiteConnection
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, cast

from sqlalchemy import Select

from mediaforce.core.db import DBClient


class LibraryCalculations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._saved: dict[str, tuple[object, Any]] = {}
        self._read_revision: object = None
        self._reads: dict[str, object] = {}

    @contextmanager
    def request(self) -> Iterator[None]:
        token = _request.set(self)
        try:
            yield
        finally:
            _request.reset(token)

    def saved_inputs(self, connection: DBClient, queries: Sequence[Select]) -> object:
        driver = cast(SQLiteConnection, connection.connection.driver_connection)
        revision = _database_revision(connection)
        if driver.in_transaction:
            return tuple(_saved_input_digest(connection, query) for query in queries)
        with self._lock:
            if self._read_revision != revision:
                self._reads.clear()
                self._read_revision = revision
            keys = []
            for query in queries:
                compiled = query.compile()
                key = repr((str(compiled), compiled.params))
                keys.append(key)
                if key not in self._reads:
                    self._reads[key] = _saved_input_digest(connection, query)
            return tuple(self._reads[key] for key in keys)

    def reuse[T](
        self, name: str, inputs: object, calculate: Callable[[], T], copy_result: Callable[[T], T], still_current: Callable[[], bool]
    ) -> T:
        with self._lock:
            saved = self._saved.get(name)
            if saved is None or saved[0] != inputs:
                saved = (inputs, calculate())
                if still_current():
                    self._saved[name] = saved
            return copy_result(saved[1])


_request: ContextVar[LibraryCalculations | None] = ContextVar("library_calculations", default=None)


def reuse_library_calculation[T](
    connection: DBClient,
    name: str,
    queries: Sequence[Select],
    inputs: object,
    calculate: Callable[[], T],
    *,
    copy_result: Callable[[T], T] = deepcopy,
    inputs_current: Callable[[], bool] | None = None,
) -> T:
    calculations = _request.get()
    if calculations is None:
        return calculate()
    revision = _database_revision(connection)
    saved_inputs = calculations.saved_inputs(connection, queries)
    return calculations.reuse(
        name,
        (saved_inputs, deepcopy(inputs)),
        calculate,
        copy_result,
        lambda: _database_revision(connection) == revision and (inputs_current is None or inputs_current()),
    )


def _database_revision(connection: DBClient) -> tuple[object, int, int, int]:
    return (
        connection.info.setdefault("library_calculations_connection", object()),
        connection.exec_driver_sql("PRAGMA data_version").scalar_one(),
        connection.exec_driver_sql("PRAGMA schema_version").scalar_one(),
        cast(SQLiteConnection, connection.connection.driver_connection).total_changes,
    )


def _saved_input_digest(connection: DBClient, query: Select) -> bytes:
    digest = sha256()
    for row in connection.execute(query):
        digest.update(dumps(tuple(row), protocol=5))
    return digest.digest()
