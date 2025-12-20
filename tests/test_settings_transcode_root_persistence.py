import platform
import sys

from fastapi.testclient import TestClient

from mediaforce.web.app import app


def test_api_update_settings_does_not_rewrite_transcode_root(monkeypatch):
    webapp_module = sys.modules["mediaforce.web.app"]

    captured = {}

    def _fake_save_app_settings(settings):
        captured["transcode_root"] = settings.transcode_root

    monkeypatch.setattr(webapp_module, "save_app_settings", _fake_save_app_settings, raising=False)

    is_mac = platform.system() == "Darwin"
    transcode_root = "/mnt/media/tv/transcode" if is_mac else "/Volumes/media/tv/transcode"

    payload = {
        "libraries": [
            {
                "id": "tv",
                "name": "TV",
                "media_type": "tv",
                "mac_path": "/Volumes/media/tv",
                "linux_path": "/mnt/media/tv",
                "watch": True,
                "max_height": 1080,
                "weight": 1.0,
            }
        ],
        "global_max_height": 1080,
        "max_concurrency": 1,
        "offpeak_enabled": False,
        "offpeak_start": "00:00",
        "offpeak_end": "05:00",
        "transcode_root": transcode_root,
    }

    client = TestClient(app)
    resp = client.post("/api/settings", json=payload)
    data = resp.json()
    assert data["success"] is True
    assert captured["transcode_root"] == transcode_root
