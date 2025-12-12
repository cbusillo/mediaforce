from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select

from mediaforce.db import EncodeProgress, EncodeResult, MediaItem, ShowOverride
from mediaforce.core import cmd_run
from mediaforce.web.app import app


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
    monkeypatch.setattr(webapp_module, "ensure_schema", lambda _engine: None, raising=False)

    return TestClient(app)


def test_worker_claim_includes_show_override(client):
    import sys
    webapp_module = sys.modules["mediaforce.web.app"]
    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        item = MediaItem(path="/tmp/ShowA/Season 1/E01.mkv", status="pending", detected_tier="good")
        session.add(item)
        session.add(ShowOverride(show_name="ShowA", default_tier="poor"))
        session.commit()
        session.refresh(item)
        assert item.id is not None
        item_id = int(item.id)

    resp = client.post("/api/worker/claim", json={"machine": "worker-1"})
    data = resp.json()
    assert data["success"] is True
    assert data["claimed"]["id"] == item_id
    assert data["override_tier"] == "poor"

    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        updated = session.get(MediaItem, item_id)
        assert updated is not None
        assert updated.status == "encoding"
        assert updated.claimed_by == "worker-1"
        assert updated.claimed_at


def test_worker_report_success_creates_encode_result_and_clears_progress(client):
    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        item = MediaItem(path="/tmp/foo.mkv", status="pending")
        session.add(item)
        session.commit()
        session.refresh(item)
        assert item.id is not None
        source_id = int(item.id)

    claim = client.post("/api/worker/claim", json={"machine": "worker-1"}).json()
    assert claim["success"] and claim["claimed"]["id"] == source_id

    pid = client.post(
        "/api/worker/progress/start",
        json={
            "source_id": source_id,
            "source_path": "/tmp/foo.mkv",
            "output_path": "/tmp/foo.AV1.mp4",
            "machine": "worker-1",
            "tier": "good",
            "duration_sec": 120.0,
        },
    ).json()["progress_id"]

    resp = client.post(
        "/api/worker/report",
        json={
            "source_id": source_id,
            "source_path": "/tmp/foo.mkv",
            "tier": "good",
            "crf": 28,
            "preset": 5,
            "film_grain": 8,
            "denoise": None,
            "output_path": "/tmp/foo.AV1.mp4",
            "output_size_bytes": 123,
            "output_bitrate_kbps": 1000,
            "source_size_bytes": 456,
            "machine": "worker-1",
            "started_at": "2025-01-01T00:00:00",
            "success": True,
            "progress_id": pid,
            "metrics": {"vmaf": 95.0},
            "outlier": {"is_outlier": False, "reasons": []},
        },
    )
    assert resp.json()["success"] is True

    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        item = session.get(MediaItem, source_id)
        assert item is not None
        assert item.status == "encoded"

        enc = session.exec(select(EncodeResult).where(EncodeResult.source_id == source_id)).one()
        assert enc.output_size_bytes == 123
        assert enc.vmaf == 95.0

        progress_rows = session.exec(select(EncodeProgress)).all()
        assert progress_rows == []


def test_worker_report_failure_resets_claim(client):
    import sys

    webapp_module = sys.modules["mediaforce.web.app"]
    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        item = MediaItem(path="/tmp/bar.mkv", status="pending")
        session.add(item)
        session.commit()
        session.refresh(item)
        assert item.id is not None
        source_id = int(item.id)

    claim = client.post("/api/worker/claim", json={"machine": "worker-2"}).json()
    assert claim["success"] and claim["claimed"]["id"] == source_id

    pid = client.post(
        "/api/worker/progress/start",
        json={
            "source_id": source_id,
            "source_path": "/tmp/bar.mkv",
            "output_path": "/tmp/bar.AV1.mp4",
            "machine": "worker-2",
            "tier": "good",
            "duration_sec": 120.0,
        },
    ).json()["progress_id"]

    resp = client.post(
        "/api/worker/report",
        json={
            "source_id": source_id,
            "source_path": "/tmp/bar.mkv",
            "tier": "good",
            "crf": 28,
            "preset": 5,
            "film_grain": 8,
            "denoise": None,
            "output_path": "/tmp/bar.AV1.mp4",
            "output_size_bytes": 0,
            "output_bitrate_kbps": None,
            "source_size_bytes": 456,
            "machine": "worker-2",
            "started_at": "2025-01-01T00:00:00",
            "success": False,
            "error_message": "ffmpeg failed",
            "progress_id": pid,
        },
    )
    assert resp.json()["success"] is True

    with webapp_module.session_scope() as session:  # type: ignore[attr-defined]
        item = session.get(MediaItem, source_id)
        assert item is not None
        assert item.status == "pending"
        assert item.claimed_by is None
        assert item.claimed_at is None


def test_cmd_run_api_mode_does_not_require_db(tmp_path, monkeypatch):
    class _StubClient:
        def __init__(self, _url: str):
            pass

        def claim(self, *, machine: str):
            return None

    monkeypatch.setattr("mediaforce.core.WorkerApiClient", _StubClient)

    args = SimpleNamespace(
        path=str(tmp_path),
        output=str(tmp_path / "out"),
        until=None,
        dry_run=True,
        force=False,
        verify=False,
        verify_duration=60,
        sample_vmaf=False,
        sample_length=8.0,
        sample_motion_aware=True,
        hw_decode=True,
        hw_encode=False,
        autoupdate_url=None,
        autoupdate_interval=0,
        settings_url=None,
        profile_settings_url=None,
        max_concurrency=None,
        offpeak_enabled=None,
        offpeak_start=None,
        offpeak_end=None,
        api_url="http://localhost:5555",
    )

    assert cmd_run(args) == 0
