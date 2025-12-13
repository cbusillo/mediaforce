from contextlib import contextmanager
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from mediaforce.db import EncodeResult, MediaItem
from mediaforce.web.app import app


def test_stats_summary_returns_windowed_series(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def _scope():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    monkeypatch.setattr(webapp_module, "session_scope", _scope, raising=False)
    monkeypatch.setattr(webapp_module, "ensure_schema", lambda _engine: None, raising=False)
    monkeypatch.setattr(webapp_module, "resolve_existing_library_root", lambda: "/tmp", raising=False)

    today = date.today()
    yesterday = date.fromordinal(today.toordinal() - 1)

    with _scope() as session:
        item1 = MediaItem(path="/tmp/ShowA/Season 1/E01.mkv", status="encoded", size_bytes=1000)
        item2 = MediaItem(path="/tmp/ShowB/Season 1/E01.mkv", status="encoded", size_bytes=2000)
        session.add(item1)
        session.add(item2)
        session.commit()
        session.refresh(item1)
        session.refresh(item2)
        assert item1.id is not None
        assert item2.id is not None

        session.add(
            EncodeResult(
                source_id=int(item1.id),
                source_path=item1.path,
                tier="good",
                output_path="/tmp/E01.AV1.mp4",
                output_size_bytes=500,
                encode_speed=1.5,
                completed_at=f"{today.isoformat()}T12:00:00",
            )
        )
        session.add(
            EncodeResult(
                source_id=int(item2.id),
                source_path=item2.path,
                tier="bad",
                output_path="/tmp/E01b.AV1.mp4",
                output_size_bytes=1000,
                encode_speed=2.0,
                completed_at=f"{yesterday.isoformat()}T12:00:00",
            )
        )
        session.commit()

    client = TestClient(app)
    data = client.get("/api/stats/summary?days=7").json()
    assert data["window_days"] == 7
    assert data["totals"]["encodes"] == 2
    assert data["totals"]["saved_bytes"] == 1500
    assert len(data["daily"]) == 7

    day_map = {row["day"]: row for row in data["daily"]}
    assert day_map[today.isoformat()]["encodes"] == 1
    assert day_map[yesterday.isoformat()]["encodes"] == 1
    assert sorted(t["tier"] for t in data["tiers"]) == ["bad", "good"]


def test_stats_page_renders(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def _scope():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    monkeypatch.setattr(webapp_module, "session_scope", _scope, raising=False)
    monkeypatch.setattr(webapp_module, "ensure_schema", lambda _engine: None, raising=False)
    monkeypatch.setattr(webapp_module, "resolve_existing_library_root", lambda: "/tmp", raising=False)

    client = TestClient(app)
    resp = client.get("/stats?days=7")
    assert resp.status_code == 200
    assert "Stats" in resp.text

