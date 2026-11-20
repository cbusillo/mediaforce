"""Coordinate controller cleanup with encode reservation publication."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager

from mediaforce.core.db import DBClient, database_file_path

STANDALONE_CLI_ENCODE_ORIGINS = frozenset({"cli", "cli-production", "cli-review"})


@contextmanager
def locked_encode_dispatch(connection: DBClient, *, blocking: bool = False) -> Iterator[None]:
    path = database_file_path(connection)
    if path is None:
        raise RuntimeError("Encode coordination needs a persistent controller database.")
    lock_path = path.with_suffix(f"{path.suffix}.encode-dispatch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
