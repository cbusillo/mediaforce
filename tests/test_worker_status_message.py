import importlib

from fastapi.testclient import TestClient

webapp_module = importlib.import_module("mediaforce.web.app")


def test_worker_claim_persists_status_message(monkeypatch):
    captured = {}

    monkeypatch.delenv("MEDIAFORCE_API_TOKEN", raising=False)

    def _fake_upsert_heartbeat(session, *, machine, sample_path=None, status_message=None, now_iso=None):
        captured["machine"] = machine
        captured["status_message"] = status_message

    monkeypatch.setattr(webapp_module, "upsert_heartbeat", _fake_upsert_heartbeat, raising=False)

    payload = {
        "machine": "test-worker",
        "available": False,
        "sample_path": "/Volumes/media/tv",
        "status_message": "Library not mounted: /Volumes/media/tv",
    }

    client = TestClient(webapp_module.app)
    resp = client.post("/api/worker/claim", json=payload)
    data = resp.json()
    assert data["success"] is True
    assert captured["machine"] == "test-worker"
    assert captured["status_message"] == "Library not mounted: /Volumes/media/tv"
