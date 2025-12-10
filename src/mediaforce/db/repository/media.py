from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select, desc  # type: ignore[reportMissingImports]

from mediaforce.db.models import MediaItem
from mediaforce.db.repository.base import BaseRepository, Page, Pagination


class MediaRepository(BaseRepository[MediaItem]):
    def __init__(self, session: Session):
        super().__init__(session, MediaItem)

    def list_pending(self, pagination: Optional[Pagination] = None) -> Page[MediaItem]:
        where = MediaItem.status == "pending"
        return self.list(where=where, order_by=desc(MediaItem.priority_score), pagination=pagination)

    def list_skipped(self, pagination: Optional[Pagination] = None) -> Page[MediaItem]:
        where = MediaItem.skip_reason.is_not(None)  # type: ignore[union-attr]
        order_expr = desc(MediaItem.updated_at) if MediaItem.updated_at is not None else None
        return self.list(where=where, order_by=order_expr, pagination=pagination)

    def bump_priority(self, media_id: int, delta: int = 1) -> None:
        item = self.get(media_id)
        if not item:
            return
        item.manual_priority = (item.manual_priority or 0) + delta
        self.session.add(item)
        self.session.commit()
