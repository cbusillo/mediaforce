"""Coordinate controller cleanup with encode reservation publication."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mediaforce.core.db import DBClient

STANDALONE_CLI_ENCODE_ORIGINS = frozenset({"cli", "cli-production", "cli-review"})


@contextmanager
def locked_encode_dispatch(connection: DBClient, *, blocking: bool = False) -> Iterator[None]:
    database = next((row[2] for row in connection.exec_driver_sql("PRAGMA database_list").all() if row[1] == "main"), None)
    if not database or database == ":memory:":
        raise RuntimeError("Encode coordination needs a persistent controller database.")
    path = Path(database)
    lock_path = path.with_suffix(f"{path.suffix}.encode-dispatch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
