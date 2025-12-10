from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from mediaforce.core import recalculate_priorities, check_missing_outputs
from mediaforce.db import MediaItem, EncodeResult


def make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_recalculate_priorities_updates_scores():
    session = make_session()
    # Two pending items with different potential savings
    items = [
        MediaItem(path="/a", status="pending", potential_savings_bytes=100, mtime=1000),
        MediaItem(path="/b", status="pending", potential_savings_bytes=200, mtime=1000),
    ]
    session.add_all(items)
    session.commit()

    recalculate_priorities(session, max_age=1000)

    rows = session.exec(select(MediaItem)).all()
    scores = {row.path: row.priority_score for row in rows}
    assert scores["/b"] is not None and scores["/a"] is not None
    assert scores["/b"] > scores["/a"]  # higher savings -> higher priority


def test_check_missing_outputs_resets_status(tmp_path):
    session = make_session()
    item = MediaItem(path="/source", status="encoded")
    session.add(item)
    session.commit()

    enc = EncodeResult(source_id=item.id, source_path="/source", output_path=str(tmp_path / "missing.mp4"))
    session.add(enc)
    session.commit()

    reset = check_missing_outputs(session)
    session.refresh(item)

    assert reset == 1
    assert item.status == "pending"
