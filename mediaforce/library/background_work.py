from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import background_work_state
from mediaforce.core.utils import timestamp

BACKGROUND_WORK_AREA = "catalog_evidence"


def ensure_background_work_state(
        connection: DBClient,
        *,
        updated_at: str | None = None,
) -> None:
    now_iso = updated_at or timestamp()
    statement = sqlite_insert(background_work_state).values(
        work_area=BACKGROUND_WORK_AREA,
        is_paused=0,
        updated_at=now_iso,
    )
    connection.execute(
        statement.on_conflict_do_nothing(index_elements=[background_work_state.c.work_area])
    )


def load_background_work_state(connection: DBClient) -> dict[str, Any]:
    row = connection.execute(
        select(background_work_state).where(
            background_work_state.c.work_area == BACKGROUND_WORK_AREA
        )
    ).mappings().fetchone()
    if row is None:
        return {
            "work_area": BACKGROUND_WORK_AREA,
            "is_paused": False,
            "updated_at": None,
        }
    return {
        "work_area": str(row["work_area"]),
        "is_paused": bool(row["is_paused"]),
        "updated_at": row["updated_at"],
    }


def background_work_is_paused(connection: DBClient) -> bool:
    return bool(load_background_work_state(connection)["is_paused"])


def set_background_work_paused(
        connection: DBClient,
        *,
        is_paused: bool,
        updated_at: str | None = None,
        within_transaction: bool = False,
) -> dict[str, Any]:
    now_iso = updated_at or timestamp()
    if not within_transaction:
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        ensure_background_work_state(connection, updated_at=now_iso)
        connection.execute(
            update(background_work_state)
            .where(background_work_state.c.work_area == BACKGROUND_WORK_AREA)
            .values(is_paused=int(is_paused), updated_at=now_iso)
        )
    except BaseException:
        if not within_transaction:
            connection.rollback()
        raise
    return load_background_work_state(connection)
