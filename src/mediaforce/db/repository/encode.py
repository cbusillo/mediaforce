from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select, desc  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeResult
from mediaforce.db.repository.base import BaseRepository, Page, Pagination


class EncodeRepository(BaseRepository[EncodeResult]):
    def __init__(self, session: Session):
        super().__init__(session, EncodeResult)

    def list_recent(self, pagination: Optional[Pagination] = None) -> Page[EncodeResult]:
        return self.list(order_by=desc(EncodeResult.completed_at), pagination=pagination)

    def list_pending_review(self, pagination: Optional[Pagination] = None) -> Page[EncodeResult]:
        where = EncodeResult.review_status == "pending"
        return self.list(where=where, order_by=desc(EncodeResult.completed_at), pagination=pagination)
