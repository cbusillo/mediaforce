from datetime import datetime, timedelta
from typing import TypedDict

from sqlalchemy import func
from sqlmodel import Session, select

from mediaforce.db.models import EncodeProgress, MediaItem


class ReconcileResult(TypedDict):
    reset_to_pending: int
    force_to_encoding: int


def reconcile_queue_state(
    session: Session,
    *,
    progress_stale_seconds: int = 10 * 60,
) -> ReconcileResult:
    now = datetime.now()
    cutoff = (now - timedelta(seconds=int(progress_stale_seconds))).isoformat()
    changed: ReconcileResult = {"reset_to_pending": 0, "force_to_encoding": 0}

    for progress in session.exec(select(EncodeProgress)).all():
        item = session.get(MediaItem, int(progress.source_id))
        if not item:
            continue
        if item.status != "encoding":
            item.status = "encoding"
            item.claimed_by = progress.machine
            item.claimed_at = str(progress.started_at)
            item.updated_at = now.isoformat()
            session.add(item)
            changed["force_to_encoding"] += 1

    encoding_items = session.exec(select(MediaItem).where(MediaItem.status == "encoding")).all()
    for item in encoding_items:
        if item.id is None:
            continue

        progress = session.exec(
            select(EncodeProgress)
            .where(EncodeProgress.source_id == int(item.id))
            .order_by(func.coalesce(EncodeProgress.updated_at, EncodeProgress.started_at).desc())
        ).first()

        if progress is None:
            claimed_at = str(item.claimed_at or "")
            if claimed_at and claimed_at < cutoff:
                item.status = "pending"
                item.claimed_by = None
                item.claimed_at = None
                item.updated_at = now.isoformat()
                session.add(item)
                changed["reset_to_pending"] += 1
            continue

        updated_at = str(progress.updated_at or progress.started_at or "")
        if updated_at and updated_at < cutoff:
            item.status = "pending"
            item.claimed_by = None
            item.claimed_at = None
            item.updated_at = now.isoformat()
            session.add(item)
            changed["reset_to_pending"] += 1

    if changed["reset_to_pending"] or changed["force_to_encoding"]:
        session.commit()
    return changed
