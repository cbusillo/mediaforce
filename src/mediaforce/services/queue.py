from __future__ import annotations

import pathlib
from datetime import datetime, timedelta
from typing import Optional, Callable

from sqlalchemy import func, text
from sqlmodel import Session, select

from mediaforce.db.models import EncodeProgress, EncodeResult, Library, MediaItem
from mediaforce.db import now_iso as default_now_iso


def recalculate_priorities(session: Session, max_age: int, calculate_priority: Callable) -> None:
    """Recalculate priority scores using actual max_savings from the database."""

    row = session.exec(  # type: ignore[call-overload]
        text(
            """
            SELECT MAX(potential_savings_bytes)
            FROM media_inventory
            WHERE status = 'pending'
              AND potential_savings_bytes IS NOT NULL
              AND potential_savings_bytes > 0
            """
        )
    ).first()
    max_savings = row[0] if row and row[0] else 1

    pending = session.exec(select(MediaItem).where(MediaItem.status == "pending")).all()
    now = datetime.now().isoformat()
    for item in pending:
        priority = calculate_priority(item.potential_savings_bytes, item.mtime or 0, max_savings, max_age)
        item.priority_score = priority
        item.updated_at = now
        session.add(item)
    session.commit()


def check_missing_outputs(
    session: Session,
    now_iso: Callable[[], str] = default_now_iso,
) -> tuple[int, list[dict[str, str]]]:
    """Check for completed encodes with missing output files and reset to pending.

    Returns (count, list of {"source": ..., "output": ...} entries reset).
    """

    joins = session.exec(
        select(MediaItem.id, MediaItem.path, EncodeResult.output_path)
        .join(EncodeResult, EncodeResult.source_id == MediaItem.id)
        .where(MediaItem.status == "encoded", EncodeResult.output_path != None)  # noqa: E711
    ).all()

    missing_count = 0
    missing_files: list[dict[str, str]] = []
    now_str = now_iso()
    for mid, src_path, out_path in joins:
        output_path = pathlib.Path(out_path)
        if not output_path.exists():
            item = session.get(MediaItem, mid)
            if item:
                item.status = "pending"
                item.updated_at = now_str
                session.add(item)
            missing_count += 1
            missing_files.append({
                "source": str(src_path),
                "output": str(out_path),
            })

    if missing_count:
        session.commit()

    return missing_count, missing_files


def release_claim(
    session: Session,
    file_id: int,
    success: bool,
    now_iso: Callable[[], str] = default_now_iso,
) -> None:
    item = session.get(MediaItem, file_id)
    if not item:
        return

    now_str = now_iso()
    if success:
        item.status = "encoded"
    else:
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
    item.updated_at = now_str
    session.add(item)
    session.commit()


def claim_next_file(
    session: Session,
    machine: str,
    stale_seconds: int = 8 * 60 * 60,
    progress_stale_seconds: int = 30 * 60,
    now_iso: Callable[[], str] = default_now_iso,
) -> Optional[dict]:
    """Claim the next file with library-aware weighting and manual bumping."""

    now = datetime.now()
    now_str = now_iso()
    stale_cutoff = (now - timedelta(seconds=stale_seconds)).isoformat()

    stale_items = session.exec(
        select(MediaItem).where(
            MediaItem.status == "encoding",
            MediaItem.claimed_at != None,  # noqa: E711
            MediaItem.claimed_at < stale_cutoff,  # type: ignore[operator]
        )
    ).all()
    for item in stale_items:
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
        item.updated_at = now_str
        session.add(item)
    if stale_items:
        session.commit()

    # If a worker crashes after claiming but before starting progress updates,
    # the job can get stuck in "encoding" forever (and other workers won't pick
    # it up). Treat any "encoding" row with no recent progress as stale.
    progress_cutoff = (now - timedelta(seconds=int(progress_stale_seconds))).isoformat()

    active_progress_source_ids = {
        int(row[0])
        for row in session.exec(
            select(EncodeProgress.source_id)
            .where(
                EncodeProgress.updated_at.is_not(None),
                EncodeProgress.updated_at >= progress_cutoff,
            )
        ).all()
        if row and row[0] is not None
    }

    stalled_items = session.exec(
        select(MediaItem).where(
            MediaItem.status == "encoding",
            MediaItem.claimed_at != None,  # noqa: E711
            MediaItem.claimed_at < progress_cutoff,  # type: ignore[operator]
        )
    ).all()
    dirty = False
    for item in stalled_items:
        if item.id is None:
            continue
        if int(item.id) in active_progress_source_ids:
            continue
        item.status = "pending"
        item.claimed_by = None
        item.claimed_at = None
        item.updated_at = now_str
        session.add(item)
        dirty = True
    if dirty:
        session.commit()

    # Keep at most one active claim per machine unless progress is actively
    # updating. This prevents a worker from accumulating multiple "encoding"
    # rows if it restarts or hangs between claim and release.

    active_progress_source_ids_for_machine = {
        int(row[0])
        for row in session.exec(
            select(EncodeProgress.source_id)
            .where(
                EncodeProgress.machine == machine,
                EncodeProgress.updated_at.is_not(None),
                EncodeProgress.updated_at >= progress_cutoff,
            )
        ).all()
        if row and row[0] is not None
    }

    claimed_for_machine = session.exec(
        select(MediaItem).where(
            MediaItem.status == "encoding",
            MediaItem.claimed_by == machine,
        )
    ).all()

    dirty = False
    for item in claimed_for_machine:
        if item.id is None:
            continue
        if int(item.id) in active_progress_source_ids_for_machine:
            continue
        if item.claimed_at is None:
            continue
        if item.claimed_at < progress_cutoff:  # type: ignore[operator]
            item.status = "pending"
            item.claimed_by = None
            item.claimed_at = None
            item.updated_at = now_str
            session.add(item)
            dirty = True
    if dirty:
        session.commit()

    active_claim_count = session.exec(
        select(func.count()).select_from(MediaItem).where(
            MediaItem.status == "encoding",
            MediaItem.claimed_by == machine,
        )
    ).one()
    if active_claim_count and int(active_claim_count or 0) > 0:
        return None

    weight_expr = func.coalesce(MediaItem.priority_score, 0) * func.coalesce(Library.weight, 1)
    weighted = session.exec(
        select(MediaItem, Library.weight)
        .join(Library, MediaItem.library_id == Library.id, isouter=True)
        .where(
            MediaItem.status == "pending",
            ((MediaItem.claimed_by == None) | (MediaItem.claimed_by == machine)),  # noqa: E711
        )
        .order_by(MediaItem.manual_priority, weight_expr.desc())
        .limit(1)
    ).first()

    if not weighted:
        return None

    item, _weight = weighted
    fresh = session.get(MediaItem, item.id)
    if fresh is None or fresh.status != "pending":
        return claim_next_file(session, machine, stale_seconds, now_iso)

    fresh.status = "encoding"
    fresh.claimed_by = machine
    fresh.claimed_at = now_str
    fresh.updated_at = now_str
    session.add(fresh)
    session.commit()
    session.refresh(fresh)

    return {
        "id": fresh.id,
        "path": fresh.path,
        "detected_tier": fresh.detected_tier,
        "potential_savings_bytes": fresh.potential_savings_bytes,
        "priority_score": fresh.priority_score,
        "bitrate_kbps": fresh.bitrate_kbps,
        "duration_sec": fresh.duration_sec,
        "library_id": fresh.library_id,
    }


def queue_listing(session: Session, limit: int):
    return (
        session.exec(
            select(MediaItem)
            .where(MediaItem.status == "pending")
            .order_by(func.coalesce(MediaItem.priority_score, 0).desc())
            .limit(limit)
        ).all()
    )


def encode_rows_with_sizes(session: Session):
    return session.exec(
        select(EncodeResult.output_size_bytes, MediaItem.size_bytes)
        .join(MediaItem, EncodeResult.source_id == MediaItem.id)
        .where(
            EncodeResult.output_size_bytes != None,  # noqa: E711
            EncodeResult.output_size_bytes > 0,  # type: ignore[operator]
        )
    ).all()
