from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlmodel import Session, desc, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import MediaItem
from mediaforce.db.repository.base import BaseRepository, Page, Pagination

select: Any = _select


class MediaRepository(BaseRepository[MediaItem]):
    def __init__(self, session: Session):
        super().__init__(session, MediaItem)

    def list_pending(self, pagination: Optional[Pagination] = None) -> Page[MediaItem]:
        where = MediaItem.status == "pending"
        return self.list(where=where, order_by=desc(MediaItem.priority_score), pagination=pagination)

    def list_skipped(self, pagination: Optional[Pagination] = None) -> Page[MediaItem]:
        where = MediaItem.skip_reason.is_not(None)  # type: ignore[union-attr]
        order_expr: object | None = desc(MediaItem.updated_at) if MediaItem.updated_at is not None else None
        return self.list(where=where, order_by=order_expr, pagination=pagination)

    def bump_priority(self, media_id: int, delta: int = 1) -> None:
        item = self.get(media_id)
        if not item:
            return
        item.manual_priority = (item.manual_priority or 0) + delta
        self.session.add(item)
        self.session.commit()

    def count_by_status(self) -> dict[str, int]:
        rows = self.session.exec(
            select(MediaItem.status, func.count().label("cnt")).group_by(MediaItem.status)
        ).all()
        return {row.status: int(row.cnt or 0) for row in rows}

    def pending_tier_counts(self) -> dict[Optional[str], int]:
        rows = self.session.exec(
            select(MediaItem.detected_tier, func.count().label("cnt"))
            .where(MediaItem.status == "pending")
            .group_by(MediaItem.detected_tier)
        ).all()
        return {row.detected_tier: int(row.cnt or 0) for row in rows}

    def last_scan_ts(self, *, library_id: Optional[str]) -> Optional[str]:
        scanned_at: Any = MediaItem.scanned_at
        stmt: Any = select(func.max(scanned_at))
        if library_id is not None:
            stmt = stmt.where(MediaItem.library_id == library_id)
        return self.session.exec(stmt).first()
