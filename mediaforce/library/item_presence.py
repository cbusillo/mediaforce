"""Whether a catalog file is on disk, kept apart from its workflow stage."""

from typing import Any, Mapping

from mediaforce.core.db_tables import library_items

LIBRARY_ITEM_PRESENT = library_items.c.missing_since.is_(None)
LIBRARY_ITEM_MISSING = library_items.c.missing_since.is_not(None)


def library_item_is_missing(row: Mapping[str, Any]) -> bool:
    return row.get("missing_since") is not None
