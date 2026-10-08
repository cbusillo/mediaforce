"""Saved, explicit remake requests retained until their files enter the queue."""

import json
from collections.abc import Collection
from pathlib import Path
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


def requested_copy_is_present(row: Any) -> bool:
    return (remake_intent(row).get("state") == "requested"
            and Path(str(row["staging_path"] or "")).is_file())


def save_remake_intent(connection: DBClient, row: Any, intent: dict[str, Any], *, now: str) -> None:
    validation = {**stored_validation(row), INTENT_KEY: intent}
    if not intent:
        validation.pop(INTENT_KEY, None)
    connection.execute(update(staged_artifacts)
                       .where(staged_artifacts.c.library_item_id == row["library_item_id"])
                       .values(validation_json=json.dumps(validation),
                               updated_at=now))


def finish_remake_intents(
        connection: DBClient, item_ids: Collection[int], *, prefix: str, mode: str, approval: dict[str, Any] | None,
) -> None:
    """An explicit new queue replaces removed requests; a saved retry clears only its matching record."""
    for row in connection.execute(select(staged_artifacts).where(
            staged_artifacts.c.library_item_id.in_(item_ids))).mappings():
        intent = remake_intent(row)
        if (approval is None and intent.get("state") == "removed") or (
                approval is not None and
                (intent.get("prefix"), intent.get("mode"), intent.get("approval")) == (prefix, mode, approval)):
            connection.execute(delete(staged_artifacts).where(
                staged_artifacts.c.library_item_id == row["library_item_id"]).where(staged_artifacts.c.promoted_at.is_(None)))
