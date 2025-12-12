from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select

from mediaforce.db import MediaItem, ProfileEvaluation, ProfileSettingsSource, VmafSample
from mediaforce.web.app import app


def test_quality_loop_api_persists_evaluation_and_samples(tmp_path, monkeypatch):
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

    with _scope() as session:
        item = MediaItem(path=str(tmp_path / "Episode.mkv"), status="pending")
        session.add(item)
        src = ProfileSettingsSource(
            name="remote-default",
            source_type="remote",
            payload=json.dumps({"thresholds": {"min": 82.0, "median": 92.0}}, ensure_ascii=False),
            is_active=True,
        )
        session.add(src)
        session.commit()
        session.refresh(item)
        session.refresh(src)
        assert item.id is not None
        assert src.id is not None
        media_id = int(item.id)
        src_id = int(src.id)

    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    monkeypatch.setattr(webapp_module, "session_scope", _scope, raising=False)
    monkeypatch.setattr(webapp_module, "ensure_schema", lambda _engine: None, raising=False)

    def _ensure_active(session: Session):
        return session.get(ProfileSettingsSource, src_id)

    monkeypatch.setattr(webapp_module, "ensure_active_profile_settings", _ensure_active, raising=False)

    client = TestClient(app)
    start = client.post(
        "/api/evaluations/start",
        json={"media_id": media_id, "initial_profile": "good", "sample_length": 8.0},
    ).json()
    assert start["success"] is True
    eval_id = int(start["evaluation_id"])

    submit = client.post(
        f"/api/evaluations/{eval_id}/samples",
        json={
            "samples": [
                {"kind": "short", "start_sec": 10.0, "duration_sec": 8.0, "weight": 1.0, "vmaf": 83.0},
                {"kind": "mid", "start_sec": 60.0, "duration_sec": 8.0, "weight": 1.0, "vmaf": 83.0},
                {"kind": "motion", "start_sec": 90.0, "duration_sec": 8.0, "weight": 2.0, "vmaf": 80.0},
            ],
            "target_height": 1080,
            "target_height_reason": "global",
        },
    ).json()
    assert submit["success"] is True
    assert submit["selected_profile"] == "pristine"
    assert submit["decision"] == "bump"

    with _scope() as session:
        ev = session.get(ProfileEvaluation, eval_id)
        assert ev is not None
        assert ev.selected_profile == "pristine"
        assert ev.reason_json
        samples = session.exec(select(VmafSample).where(VmafSample.evaluation_id == eval_id)).all()
        assert len(samples) == 3


def test_quality_loop_api_respects_max_threshold(tmp_path, monkeypatch):
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

    with _scope() as session:
        item = MediaItem(path=str(tmp_path / "Episode2.mkv"), status="pending")
        session.add(item)
        src = ProfileSettingsSource(
            name="remote-default",
            source_type="remote",
            payload=json.dumps({"thresholds": {"min": 82.0, "median": 92.0, "max": 94.0}}, ensure_ascii=False),
            is_active=True,
        )
        session.add(src)
        session.commit()
        session.refresh(item)
        session.refresh(src)
        assert item.id is not None
        assert src.id is not None
        media_id = int(item.id)
        src_id = int(src.id)

    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    monkeypatch.setattr(webapp_module, "session_scope", _scope, raising=False)
    monkeypatch.setattr(webapp_module, "ensure_schema", lambda _engine: None, raising=False)

    def _ensure_active(session: Session):
        return session.get(ProfileSettingsSource, src_id)

    monkeypatch.setattr(webapp_module, "ensure_active_profile_settings", _ensure_active, raising=False)

    client = TestClient(app)
    start = client.post(
        "/api/evaluations/start",
        json={"media_id": media_id, "initial_profile": "good", "sample_length": 8.0},
    ).json()
    assert start["success"] is True
    eval_id = int(start["evaluation_id"])

    submit = client.post(
        f"/api/evaluations/{eval_id}/samples",
        json={
            "samples": [
                {"kind": "short", "start_sec": 10.0, "duration_sec": 8.0, "weight": 1.0, "vmaf": 94.0},
                {"kind": "mid", "start_sec": 60.0, "duration_sec": 8.0, "weight": 1.0, "vmaf": 94.0},
                {"kind": "motion", "start_sec": 90.0, "duration_sec": 8.0, "weight": 2.0, "vmaf": 94.0},
            ]
        },
    ).json()
    assert submit["success"] is True
    assert submit["selected_profile"] == "mediocre"
