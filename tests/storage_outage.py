import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch


@contextmanager
def storage_outage(*paths: Path, error: int = errno.ETIMEDOUT) -> Iterator[None]:
    stat = os.stat

    def unavailable(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, (str, Path)) and Path(path) in paths:
            raise OSError(error, os.strerror(error), str(path))
        return stat(path, *args, **kwargs)

    with patch.object(os, "stat", unavailable):
        yield
