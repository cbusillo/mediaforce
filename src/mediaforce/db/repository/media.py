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
        media_cols: Any = MediaItem.__table__.c
        where = media_cols.status == "pending"
        return self.list(where=where, order_by=desc(media_cols.priority_score), pagination=pagination)

    def list_skipped(self, pagination: Optional[Pagination] = None) -> Page[MediaItem]:
        media_cols: Any = MediaItem.__table__.c
        where = media_cols.skip_reason.is_not(None)
        return self.list(where=where, order_by=desc(media_cols.updated_at), pagination=pagination)

    def bump_priority(self, media_id: int, delta: int = 1) -> None:
        item = self.get(media_id)
        if not item:
            return
        item.manual_priority = (item.manual_priority or 0) + delta
        self.session.add(item)
        self.session.commit()

    def count_by_status(self) -> dict[str, int]:
        media_cols: Any = MediaItem.__table__.c
        rows = self.session.exec(
            select(media_cols.status, func.count().label("cnt"))
            .select_from(MediaItem.__table__)
            .group_by(media_cols.status)
        ).all()
        return {str(status): int(cnt or 0) for status, cnt in rows}

    def pending_tier_counts(self) -> dict[Optional[str], int]:
        media_cols: Any = MediaItem.__table__.c
        rows = self.session.exec(
            select(media_cols.detected_tier, func.count().label("cnt"))
            .select_from(MediaItem.__table__)
            .where(media_cols.status == "pending")
            .group_by(media_cols.detected_tier)
        ).all()
        return {tier: int(cnt or 0) for tier, cnt in rows}

    def last_scan_ts(self, *, library_id: Optional[str]) -> Optional[str]:
        media_cols: Any = MediaItem.__table__.c
        scanned_at: Any = media_cols.scanned_at
        stmt: Any = select(func.max(scanned_at))
        if library_id is not None:
            stmt = stmt.where(media_cols.library_id == library_id)
        return self.session.exec(stmt).first()
