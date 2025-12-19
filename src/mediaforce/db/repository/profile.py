from typing import Optional

from sqlmodel import Session, select, desc  # type: ignore[reportMissingImports]

from mediaforce.db.models import ProfileEvaluation, RetrainingCandidate, ProfileChoiceFeedback
from mediaforce.db.repository.base import BaseRepository, Page, Pagination


class ProfileEvaluationRepository(BaseRepository[ProfileEvaluation]):
    def __init__(self, session: Session):
        super().__init__(session, ProfileEvaluation)

    def list_recent(self, pagination: Optional[Pagination] = None) -> Page[ProfileEvaluation]:
        return self.list(order_by=desc(ProfileEvaluation.created_at), pagination=pagination)


class RetrainingRepository(BaseRepository[RetrainingCandidate]):
    def __init__(self, session: Session):
        super().__init__(session, RetrainingCandidate)

    def upsert_pending(self, evaluation_id: int, media_id: int, encode_result_id: Optional[int], reason_text: str, feedback_id: Optional[int]) -> RetrainingCandidate:
        existing = self.session.exec(select(RetrainingCandidate).where(RetrainingCandidate.evaluation_id == evaluation_id)).first()
        if existing:
            existing.reason_text = reason_text
            existing.status = existing.status or "pending"
            self.session.add(existing)
            self.session.commit()
            self.session.refresh(existing)
            return existing
        candidate = RetrainingCandidate(
            evaluation_id=evaluation_id,
            media_id=media_id,
            encode_result_id=encode_result_id,
            feedback_id=feedback_id,
            reason_text=reason_text,
            status="pending",
        )
        self.session.add(candidate)
        self.session.commit()
        self.session.refresh(candidate)
        return candidate


class FeedbackRepository(BaseRepository[ProfileChoiceFeedback]):
    def __init__(self, session: Session):
        super().__init__(session, ProfileChoiceFeedback)
