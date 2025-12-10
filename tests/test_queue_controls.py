import pathlib
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, Session

from mediaforce.web.app import app
from mediaforce.db.models import MediaItem


@pytest.fixture()
def client(tmp_path, monkeypatch):
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

    # seed one pending item
    with Session(engine) as s:
        item = MediaItem(path="/tmp/foo.mkv", status="pending", manual_priority=0)
        s.add(item)
        s.commit()
        s.refresh(item)
        seed_id = item.id

    client = TestClient(app)
    client.seed_id = seed_id  # type: ignore[attr-defined]
    return client


def test_bump_pause_resume(client):
    mid = client.seed_id
    resp = client.post(f"/api/queue/{mid}/bump", json={"delta": 2})
    assert resp.json()["success"]

    resp = client.post(f"/api/queue/{mid}/pause")
    assert resp.json()["success"]

    resp = client.post(f"/api/queue/{mid}/resume")
    assert resp.json()["success"]


def test_force_rescan_reset(client):
    mid = client.seed_id
    resp = client.post(f"/api/queue/{mid}/force-rescan")
    assert resp.json()["success"]

    resp = client.post(f"/api/queue/{mid}/reset-skip")
    assert resp.json()["success"]


def test_add_and_not_found(client):
    resp = client.post("/api/queue/add", json={"path": "/tmp/newfile.mkv"})
    data = resp.json()
    assert data["success"] and data["id"]

    resp = client.post("/api/queue/999999/pause")
    assert resp.json()["success"] is False
# mypy: ignore-errors
# pyright: reportMissingImports=false
