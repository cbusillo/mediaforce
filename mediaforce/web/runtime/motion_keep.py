"""The owner's choice to keep files held for their motion pattern original, and undoing it.

Some held files have no conversion Mediaforce can trust, so the owner can settle them instead: a kept
file stops counting as work, keeps its reason on the page, and lets its season finish. The choice is
stored per file and bound to the file's source. A changed source lapses it, so the new source is
checked like any other file. Undoing it returns the file to held, where an earlier production hold
lets it join production again once its motion pattern clears.
"""

import json
from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_items, motion_keep_decisions
from mediaforce.encoding.cadence import cadence_hold_reason
from mediaforce.library.evidence_state import library_item_source_fingerprint
from mediaforce.library.item_presence import library_item_is_missing
from mediaforce.library.media_scopes import path_matches_scope, resolve_media_scope
from mediaforce.library.motion_holds import cadence_safety_partition, cadence_summary_payload

KEPT_ORIGINAL_EVENT = "motion_kept_original"
KEEP_UNDONE_EVENT = "motion_keep_original_undone"


class _Refused(Exception):
    """A request this action declines as a whole, before anything is written."""

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "code": "motion_keep_refused", "message": str(self)}


def keep_motion_held_originals(
        config: MediaforceConfig,
        prefix: str,
        library_item_ids: Collection[int],
        *,
        now_iso: str,
) -> dict[str, Any]:
    """Keep each named file original; every one must be in this scope and held for its motion pattern now."""
    try:
        with open_db(config.paths.db_path) as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            noun, to_keep = _files_to_keep(connection, config, prefix, library_item_ids)
            for row in to_keep:
                values = {
                    "source_fingerprint": library_item_source_fingerprint(row),
                    "reason_code": cadence_hold_reason(cadence_summary_payload(row.get("cadence_summary_json"))),
                    "prefix": prefix,
                    "kept_at": now_iso,
                }
                connection.execute(
                    sqlite_insert(motion_keep_decisions)
                    .values(library_item_id=int(row["id"]), **values)
                    .on_conflict_do_update(index_elements=[motion_keep_decisions.c.library_item_id], set_=values)
                )
                _record_event(connection, row, KEPT_ORIGINAL_EVENT, now_iso, {**values, "rel_path": row["rel_path"]})
    except _Refused as refused:
        return refused.payload()
    count = len(to_keep)
    if not count:
        return {"ok": True, "kept_count": 0, "message": f"These {noun}s were already kept original."}
    return {
        "ok": True,
        "kept_count": count,
        "message": f"Kept {_count(count, noun)} original. You can undo this here.",
    }


def undo_motion_keep(
        config: MediaforceConfig,
        prefix: str,
        library_item_ids: Collection[int],
        *,
        now_iso: str,
) -> dict[str, Any]:
    """Undo the keep-original choice for each named file in this scope, returning it to held."""
    try:
        with open_db(config.paths.db_path) as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            noun, rows = _scoped_rows(connection, config, prefix, library_item_ids)
            kept_ids = set(
                connection.execute(
                    select(motion_keep_decisions.c.library_item_id).where(
                        motion_keep_decisions.c.library_item_id.in_(sorted(rows)),
                    )
                ).scalars()
            )
            if not kept_ids:
                raise _Refused(f"These {noun}s are not kept original anymore. Refresh the page to see where they are.")
            connection.execute(
                delete(motion_keep_decisions).where(motion_keep_decisions.c.library_item_id.in_(sorted(kept_ids)))
            )
            for item_id in sorted(kept_ids):
                row = rows[item_id]
                _record_event(
                    connection, row, KEEP_UNDONE_EVENT, now_iso, {"prefix": prefix, "rel_path": row["rel_path"]},
                )
    except _Refused as refused:
        return refused.payload()
    count = len(kept_ids)
    held = "is held for its" if count == 1 else "are held for their"
    return {
        "ok": True,
        "undone_count": count,
        "message": f"{_count(count, noun).capitalize()} {held} motion pattern again.",
    }


def _files_to_keep(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        library_item_ids: Collection[int],
) -> tuple[str, list[Mapping[str, Any]]]:
    """The named files not kept yet, after checking every one is held for its motion pattern now."""
    noun, rows = _scoped_rows(connection, config, prefix, library_item_ids)
    # The same read-only split the page shows, so only a file the page lists as held can be kept.
    partition = cadence_safety_partition(connection, library_item_ids=list(rows), synchronize=False)
    not_held = [
        row for item_id, row in rows.items()
        if item_id not in partition.blocked_item_ids | partition.kept_item_ids
    ]
    if not_held:
        raise _Refused(
            f"{_file_name(not_held[0])} is no longer held for its motion pattern. "
            "Refresh the page to see where it is now."
        )
    to_keep = [row for item_id, row in rows.items() if item_id not in partition.kept_item_ids]
    unidentified = next((row for row in to_keep if not library_item_source_fingerprint(row)), None)
    if unidentified is not None:
        raise _Refused(f"Mediaforce can't identify the source of {_file_name(unidentified)}, so it can't be kept.")
    return noun, to_keep


def _scoped_rows(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        library_item_ids: Collection[int],
) -> tuple[str, dict[int, Mapping[str, Any]]]:
    """The named files keyed by id; refused when one is unknown, gone, or outside this scope."""
    scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
    noun = "episode" if scope.domain == "tv" else "file"
    ids = sorted({int(item_id) for item_id in library_item_ids})
    if not ids:
        raise _Refused(f"Choose the {noun}s to keep original.")
    rows = {
        int(row["id"]): row
        for row in connection.execute(select(library_items).where(library_items.c.id.in_(ids))).mappings().fetchall()
    }
    outside = [
        item_id for item_id in ids
        if item_id not in rows
        or library_item_is_missing(rows[item_id])
        or not path_matches_scope(str(rows[item_id]["rel_path"] or ""), scope.prefix)
    ]
    if outside:
        raise _Refused(f"Some of these {noun}s are not in this folder anymore. Refresh the page.")
    return noun, rows


def _record_event(
        connection: DBClient,
        row: Mapping[str, Any],
        event_type: str,
        now_iso: str,
        details: Mapping[str, Any],
) -> None:
    connection.execute(
        item_events.insert().values(
            library_item_id=int(row["id"]),
            created_at=now_iso,
            event_type=event_type,
            details_json=json.dumps(dict(details), sort_keys=True),
        )
    )


def _file_name(row: Mapping[str, Any]) -> str:
    return str(row.get("rel_path") or "").rsplit("/", 1)[-1] or "This file"


def _count(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"
