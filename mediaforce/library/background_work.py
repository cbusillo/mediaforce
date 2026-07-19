from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import background_work_state, library_item_evidence_state, library_items
from mediaforce.core.utils import timestamp
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED

BACKGROUND_WORK_AREA = "catalog_evidence"
EVIDENCE_BACKLOG_STATES = (
    EVIDENCE_STATE_ANALYSIS_REQUIRED,
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED,
)
EVIDENCE_BACKLOG_WORK_STATUSES = (
    "not_prepared",
    "queued",
    "running",
    "retry_wait",
    "waiting_source",
    "failed",
    "cancelled",
    "completed",
)
DEFAULT_EVIDENCE_BACKLOG_LIMIT = 25
MAX_EVIDENCE_BACKLOG_LIMIT = 100


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
) -> dict[str, Any]:
    now_iso = updated_at or timestamp()
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
        connection.rollback()
        raise
    return load_background_work_state(connection)


def summarize_evidence_inventory(connection: DBClient) -> dict[str, Any]:
    rows = connection.execute(
        select(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
            func.count().label("item_count"),
        )
        .select_from(
            library_item_evidence_state.join(
                library_items,
                library_items.c.id == library_item_evidence_state.c.library_item_id,
            )
        )
        .where(library_items.c.status != "missing")
        .group_by(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
        )
        .order_by(
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
        )
    ).mappings().fetchall()
    by_kind: dict[str, dict[str, Any]] = {
        kind: {"total": 0, "states": {}, "reasons": {}}
        for kind in EVIDENCE_KINDS
    }
    total = 0
    backlog_total = 0
    for row in rows:
        evidence_kind = str(row["evidence_kind"])
        state = str(row["state"])
        reason = str(row["reason"] or "")
        item_count = int(row["item_count"] or 0)
        total += item_count
        if state in EVIDENCE_BACKLOG_STATES:
            backlog_total += item_count
        kind_payload = by_kind.setdefault(evidence_kind, {"total": 0, "states": {}, "reasons": {}})
        kind_payload["total"] += item_count
        kind_payload["states"][state] = kind_payload["states"].get(state, 0) + item_count
        if reason:
            kind_payload["reasons"][reason] = kind_payload["reasons"].get(reason, 0) + item_count
    return {
        "total": total,
        "backlog_total": backlog_total,
        "by_kind": by_kind,
    }


def list_evidence_backlog(
        connection: DBClient,
        *,
        offset: int = 0,
        limit: int = DEFAULT_EVIDENCE_BACKLOG_LIMIT,
        evidence_kind: str | None = None,
        state: str | None = None,
        media_root: str | None = None,
        work_status: str | None = None,
) -> dict[str, Any]:
    normalized_offset = max(0, int(offset))
    normalized_limit = min(max(1, int(limit)), MAX_EVIDENCE_BACKLOG_LIMIT)
    normalized_kind = _optional_choice(evidence_kind, EVIDENCE_KINDS, "evidence kind")
    normalized_state = _optional_choice(state, EVIDENCE_BACKLOG_STATES, "evidence state")
    normalized_work_status = _optional_choice(
        work_status,
        EVIDENCE_BACKLOG_WORK_STATUSES,
        "work status",
    )
    normalized_root = str(media_root or "").strip() or None

    conditions = [
        library_item_evidence_state.c.state.in_(EVIDENCE_BACKLOG_STATES),
        library_items.c.status != "missing",
    ]
    if normalized_kind:
        conditions.append(library_item_evidence_state.c.evidence_kind == normalized_kind)
    if normalized_state:
        conditions.append(library_item_evidence_state.c.state == normalized_state)
    if normalized_root:
        conditions.append(library_items.c.media_root == normalized_root)
    if normalized_work_status == "not_prepared":
        conditions.append(library_item_evidence_state.c.work_status.is_(None))
    elif normalized_work_status:
        conditions.append(library_item_evidence_state.c.work_status == normalized_work_status)

    joined = library_item_evidence_state.join(
        library_items,
        library_items.c.id == library_item_evidence_state.c.library_item_id,
    )
    total = int(
        connection.execute(
            select(func.count()).select_from(joined).where(*conditions)
        ).scalar_one()
    )
    page_offset = normalized_offset
    if total and page_offset >= total:
        page_offset = ((total - 1) // normalized_limit) * normalized_limit
    rows = connection.execute(
        select(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
            library_item_evidence_state.c.work_status,
            library_item_evidence_state.c.attempt_count,
            library_item_evidence_state.c.retry_not_before,
            library_item_evidence_state.c.last_attempt_at,
            library_item_evidence_state.c.last_error,
            library_item_evidence_state.c.updated_at,
            library_items.c.rel_path,
            library_items.c.media_root,
            library_items.c.parent_dir,
            library_items.c.file_name,
        )
        .select_from(joined)
        .where(*conditions)
        .order_by(
            library_items.c.media_root,
            library_items.c.rel_path,
            library_item_evidence_state.c.evidence_kind,
        )
        .offset(page_offset)
        .limit(normalized_limit)
    ).mappings().fetchall()
    range_start = page_offset + 1 if total and rows else 0
    range_end = min(page_offset + len(rows), total)
    return {
        "offset": page_offset,
        "limit": normalized_limit,
        "total": total,
        "range_start": range_start,
        "range_end": range_end,
        "has_previous": page_offset > 0,
        "has_next": range_end < total,
        "filters": {
            "evidence_kind": normalized_kind,
            "state": normalized_state,
            "media_root": normalized_root,
            "work_status": normalized_work_status,
        },
        "rows": [dict(row) for row in rows],
    }


def _optional_choice(value: str | None, choices: tuple[str, ...], label: str) -> str | None:
    normalized = str(value or "").strip() or None
    if normalized is not None and normalized not in choices:
        raise ValueError(f"Unsupported {label}: {normalized}")
    return normalized
