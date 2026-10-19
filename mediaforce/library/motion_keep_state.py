"""Which files the owner kept original for their motion pattern, read the same way everywhere.

A choice in `motion_keep_decisions` holds only while the file's source fingerprint is the one it was
made for. Every reader treats a different fingerprint as lapsed, and the scanner removes the choice in
the same transaction that records a changed source, so an old source coming back cannot revive it.
Queueing, dispatch, publishing and motion-pattern checks all leave a kept file alone.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, func, select, update

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events, library_items, motion_keep_decisions
from mediaforce.core.utils import timestamp
from mediaforce.library.evidence_state import library_item_source_fingerprint

MOTION_KEEP_LAPSED_EVENT = "motion_keep_original_lapsed"


def library_item_source_sql() -> ColumnElement[Any]:
    """The SQL form of `library_item_source_fingerprint`: the content fingerprint, else the file one."""
    return func.coalesce(
        func.nullif(func.trim(library_items.c.content_version_fingerprint), ""),
        func.nullif(func.trim(library_items.c.fingerprint), ""),
    )


def kept_original_clause(library_item_id: ColumnElement[Any]) -> ColumnElement[bool]:
    """True for a row whose file is kept original for its current source; the query must join library_items."""
    return exists().where(
        motion_keep_decisions.c.library_item_id == library_item_id,
        motion_keep_decisions.c.source_fingerprint == library_item_source_sql(),
    )


def kept_original_item_ids(
        connection: DBClient,
        items: Sequence[Mapping[str, Any]],
        *,
        forget_lapsed: bool,
) -> frozenset[int]:
    """The files among ``items`` whose keep-original choice was made for their current source.

    A choice made for another source has lapsed: the file is checked again like any other. With
    ``forget_lapsed`` the lapsed choices are removed and the lapse is recorded.
    """
    fingerprints = {int(item["id"]): library_item_source_fingerprint(item) for item in items}
    if not fingerprints:
        return frozenset()
    rows = connection.execute(
        select(motion_keep_decisions.c.library_item_id, motion_keep_decisions.c.source_fingerprint).where(
            motion_keep_decisions.c.library_item_id.in_(sorted(fingerprints)),
        )
    ).fetchall()
    kept: set[int] = set()
    lapsed: set[int] = set()
    for item_id, source_fingerprint in rows:
        current = fingerprints.get(int(item_id))
        if current and str(source_fingerprint) == current:
            kept.add(int(item_id))
        else:
            lapsed.add(int(item_id))
    if forget_lapsed and lapsed:
        _forget(connection, {item_id: fingerprints.get(item_id) for item_id in lapsed}, now_iso=timestamp())
    return frozenset(kept)


def any_motion_keep_decision(connection: DBClient) -> bool:
    """Whether any file has a keep-original choice stored, current or not; a cheap guard before reading jobs."""
    return connection.execute(select(motion_keep_decisions.c.library_item_id).limit(1)).first() is not None


def library_item_ids_kept_original(connection: DBClient, library_item_ids: Collection[int]) -> frozenset[int]:
    """The named files kept original for their current source, read straight from the database."""
    ids = sorted({int(item_id) for item_id in library_item_ids if int(item_id) > 0})
    if not ids:
        return frozenset()
    return frozenset(
        int(item_id)
        for item_id in connection.execute(
            select(library_items.c.id).where(library_items.c.id.in_(ids), kept_original_clause(library_items.c.id))
        ).scalars()
    )


def reconcile_motion_keep_with_source(
        connection: DBClient,
        item: Mapping[str, Any],
        *,
        same_content: bool,
        previous_source_fingerprint: str | None,
        now_iso: str,
) -> None:
    """Keep a file's choice in step with the source the scanner just recorded for it.

    When the scanner saw the same content and only the fingerprint's form changed (a content
    fingerprint recorded for the first time), the choice follows it. Any other changed fingerprint is
    a new source, so the choice lapses in the scan's own transaction.
    """
    item_id = int(item["id"])
    stored = connection.execute(
        select(motion_keep_decisions.c.source_fingerprint).where(motion_keep_decisions.c.library_item_id == item_id)
    ).scalar_one_or_none()
    current = library_item_source_fingerprint(item)
    if stored is None or str(stored) == current:
        return
    if same_content and current and str(stored) == previous_source_fingerprint:
        connection.execute(
            update(motion_keep_decisions)
            .where(motion_keep_decisions.c.library_item_id == item_id)
            .values(source_fingerprint=current)
        )
        return
    _forget(connection, {item_id: current}, now_iso=now_iso)


def _forget(connection: DBClient, lapsed: Mapping[int, str | None], *, now_iso: str) -> None:
    connection.execute(
        delete(motion_keep_decisions).where(motion_keep_decisions.c.library_item_id.in_(sorted(lapsed)))
    )
    connection.execute(item_events.insert(), [
        {
            "library_item_id": item_id,
            "created_at": now_iso,
            "event_type": MOTION_KEEP_LAPSED_EVENT,
            "details_json": json.dumps({"source_fingerprint": source_fingerprint}),
        }
        for item_id, source_fingerprint in sorted(lapsed.items())
    ])
