from typing import Any, Optional, cast

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
        encode_cols = cast(Any, EncodeResult.__table__.c)
        return self.list(order_by=desc(encode_cols.completed_at), pagination=pagination)

    def list_pending_review(self, pagination: Optional[Pagination] = None) -> Page[EncodeResult]:
        encode_cols = cast(Any, EncodeResult.__table__.c)
        where = encode_cols.review_status == "pending"
        return self.list(where=where, order_by=desc(encode_cols.completed_at), pagination=pagination)

    def recent_completions(self, *, limit: int) -> list[tuple]:
        encode_cols = cast(Any, EncodeResult.__table__.c)
        media_cols = cast(Any, MediaItem.__table__.c)
        output_size: Any = encode_cols.output_size_bytes
        completed_at: Any = encode_cols.completed_at
        return (
            self.session.exec(
                select(
                    media_cols.path,
                    media_cols.size_bytes,
                    output_size,
                    completed_at,
                    media_cols.detected_tier,
                    encode_cols.id,
                )
                .select_from(EncodeResult.__table__)
                .join(MediaItem.__table__, encode_cols.source_id == media_cols.id)
                .where(
                    output_size.is_not(None),
                    output_size > 0,
                )
                .order_by(desc(completed_at))
                .limit(limit)
            ).all()
        )

    def space_saved_bytes(self) -> int:
        encode_cols = cast(Any, EncodeResult.__table__.c)
        media_cols = cast(Any, MediaItem.__table__.c)
        output_size: Any = encode_cols.output_size_bytes
        size_bytes: Any = media_cols.size_bytes
        row = self.session.exec(
            select(
                func.coalesce(func.sum(size_bytes), 0),
                func.coalesce(func.sum(output_size), 0),
            )
            .select_from(EncodeResult.__table__)
            .join(MediaItem.__table__, encode_cols.source_id == media_cols.id)
            .where(
                output_size.is_not(None),
                output_size > 0,
            )
        ).first()
        if not row:
            return 0
        source_bytes, output_bytes = row
        return int((source_bytes or 0) - (output_bytes or 0))
