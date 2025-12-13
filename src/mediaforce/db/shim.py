from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from mediaforce.config.settings import ENGINE


class ResultWrapper:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class SessionShim:
    """sqlite3-like shim over SQLModel Session.exec()."""

    def __init__(self, session: Session):
        self.session = session

    def execute(self, sql: str, params: tuple | dict | None = None):
        if isinstance(params, (list, tuple)):
            placeholders = sql.count("?")
            mapping = {f"p{i}": params[i] for i in range(min(placeholders, len(params)))}
            for i in range(placeholders):
                sql = sql.replace("?", f":p{i}", 1)
            stmt = text(sql)
            result = self.session.exec(stmt, params=mapping)  # type: ignore[call-overload]
        elif isinstance(params, dict):
            stmt = text(sql)
            result = self.session.exec(stmt, params=params)  # type: ignore[call-overload]
        else:
            stmt = text(sql)
            result = self.session.exec(stmt)  # type: ignore[call-overload]

        return ResultWrapper(result.all())

    def commit(self) -> None:
        self.session.commit()

    def close(self) -> None:
        self.session.close()


def init_db_shim() -> SessionShim:
    return SessionShim(Session(ENGINE))
