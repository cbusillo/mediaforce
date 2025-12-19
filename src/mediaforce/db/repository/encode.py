from typing import Any, Optional

from sqlalchemy import func
from sqlmodel import select as _select  # type: ignore[reportMissingImports]

from sqlmodel import Session, desc  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeResult
from mediaforce.db.models import MediaItem
from mediaforce.db.repository.base import BaseRepository, Page, Pagination

select: Any = _select


class EncodeRepository(BaseRepository[EncodeResult]):
    def __init__(self, session: Session):
        super().__init__(session, EncodeResult)

    def list_recent(self, pagination: Optional[Pagination] = None) -> Page[EncodeResult]:
        return self.list(order_by=desc(EncodeResult.completed_at), pagination=pagination)

    def list_pending_review(self, pagination: Optional[Pagination] = None) -> Page[EncodeResult]:
        where = EncodeResult.review_status == "pending"
        return self.list(where=where, order_by=desc(EncodeResult.completed_at), pagination=pagination)

    def recent_completions(self, *, limit: int) -> list[tuple]:
        output_size: Any = EncodeResult.output_size_bytes
        completed_at: Any = EncodeResult.completed_at
        return (
            self.session.exec(
                select(
                    MediaItem.path,
                    MediaItem.size_bytes,
                    output_size,
                    completed_at,
                    MediaItem.detected_tier,
                    EncodeResult.id,
                )
                .join(MediaItem, EncodeResult.source_id == MediaItem.id)
                .where(
                    output_size.is_not(None),
                    output_size > 0,
                )
                .order_by(completed_at.desc())
                .limit(limit)
            ).all()
        )

    def space_saved_bytes(self) -> int:
        output_size: Any = EncodeResult.output_size_bytes
        size_bytes: Any = MediaItem.size_bytes
        row = self.session.exec(
            select(
                func.coalesce(func.sum(size_bytes), 0),
                func.coalesce(func.sum(output_size), 0),
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(
                output_size.is_not(None),
                output_size > 0,
            )
        ).first()
        if not row:
            return 0
        source_bytes, output_bytes = row
        return int((source_bytes or 0) - (output_bytes or 0))
