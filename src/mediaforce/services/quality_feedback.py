from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from mediaforce.db import ProfileChoiceFeedback, ProfileEvaluation, RetrainingCandidate, now_iso


def flag_profile_choice(
    session: Session,
    *,
    evaluation_id: int,
    decision: str,
    reason: str,
) -> tuple[ProfileChoiceFeedback, Optional[RetrainingCandidate]]:
    ev = session.get(ProfileEvaluation, evaluation_id)
    if not ev:
        raise ValueError("evaluation_not_found")

    feedback = ProfileChoiceFeedback(
        evaluation_id=evaluation_id,
        decision=decision,
        reason_text=reason,
        status="queued",
    )
    session.add(feedback)

    ev.status = "flagged"
    ev.decision = decision
    ev.updated_at = now_iso()
    session.add(ev)

    session.commit()
    session.refresh(feedback)

    existing = session.exec(
        select(RetrainingCandidate).where(RetrainingCandidate.evaluation_id == evaluation_id)
    ).first()
    if existing:
        return feedback, None

    candidate = RetrainingCandidate(
        evaluation_id=evaluation_id,
        media_id=ev.media_id,
        encode_result_id=ev.encode_result_id,
        feedback_id=feedback.id,
        reason_text=reason,
        status="pending",
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return feedback, candidate

