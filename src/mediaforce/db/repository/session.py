from contextlib import AbstractContextManager, contextmanager
from typing import Iterator

from sqlmodel import Session  # type: ignore[reportMissingImports]

from mediaforce.config.settings import ENGINE


@contextmanager
def _session_scope() -> Iterator[Session]:
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


def session_scope() -> AbstractContextManager[Session]:
    return _session_scope()
