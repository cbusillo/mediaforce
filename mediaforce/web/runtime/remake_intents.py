"""Saved, explicit remake requests retained until their files enter the queue."""

import json
from collections.abc import Collection
from typing import Any

from sqlalchemy import delete, select, update

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.type_defs import object_dict

INTENT_KEY = "remake_intent"


def stored_validation(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return object_dict(json.loads(str(row["validation_json"] or "{}")))
    except json.JSONDecodeError:
        return {}


def remake_intent(row: Any) -> dict[str, Any]:
    return object_dict(stored_validation(row).get(INTENT_KEY))


def save_remake_intent(connection: DBClient, row: Any, intent: dict[str, Any], *, now: str) -> None:
    connection.execute(update(staged_artifacts)
                       .where(staged_artifacts.c.library_item_id == row["library_item_id"])
                       .values(validation_json=json.dumps({**stored_validation(row), INTENT_KEY: intent}),
                               updated_at=now))


def finish_remake_intents(
        connection: DBClient, item_ids: Collection[int], *, prefix: str, mode: str, approval: dict[str, Any],
) -> None:
    """Clear only the old records matching the request that was actually accepted."""
    for row in connection.execute(select(staged_artifacts).where(
            staged_artifacts.c.library_item_id.in_(item_ids))).mappings():
        intent = remake_intent(row)
        if (intent.get("prefix"), intent.get("mode"), intent.get("approval")) == (prefix, mode, approval):
            connection.execute(delete(staged_artifacts).where(
                staged_artifacts.c.library_item_id == row["library_item_id"]))
