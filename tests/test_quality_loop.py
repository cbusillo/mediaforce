import pathlib

import pytest

from mediaforce.config.settings import AppSettings, LibrarySettings
from mediaforce.core import build_sample_plan, resolve_target_height_for_path


class _DummyInfo:
    def __init__(self, path: pathlib.Path, duration: float):
        self.path = path
        self.duration_seconds = duration


def test_build_sample_plan_motion_weight(monkeypatch, tmp_path):
    info = _DummyInfo(tmp_path / "clip.mkv", 120.0)

    # Make one window obviously high motion
    def fake_window_bitrate(path, start, duration=5.0):
        return 8000 if start >= 50 else 800

    monkeypatch.setattr("mediaforce.core.window_bitrate", fake_window_bitrate)

    plan = build_sample_plan(info, count=3, sample_len=8.0, motion_aware=True)

    labels = [p[2] for p in plan]
    weights = [p[1] for p in plan]

    assert len(plan) == 3
    assert "motion" in labels
    assert max(weights) > 1.0  # motion window weighted higher


def test_resolve_target_height_prefers_library_over_global():
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path="/Volumes/media/tv",
                linux_path="/mnt/media/tv",
                max_height=720,
            )
        ],
        global_max_height=1080,
    )

    path = pathlib.Path("/Volumes/media/tv/Show/Episode.mkv")

    target, reason = resolve_target_height_for_path(path, settings)

    assert target == 720
    assert reason.startswith("library")
