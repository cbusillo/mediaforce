from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlmodel import Session  # type: ignore[reportMissingImports]

from mediaforce.config.settings import ENGINE


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = Session(ENGINE)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
