import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typing import Any

from test_app_window import _encode_job, connection
from test_kind_approvals import _config

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs
from mediaforce.encoding.duration_estimate import EncodeDurationSample
from mediaforce.encoding.encode_queue import save_queue_state
from mediaforce.web.runtime.app_activity import _running, _windows, activity_payload
from mediaforce.web.runtime.encode_scheduler import encode_queue_schedule_profiles, schedule_policy_deps

NOW = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)


def _queued_file(
        connection: DBClient, tmp_path: Path, prefix: str, duration: float | None,
        *, status: str = "queued", video: dict[str, Any] | None = None,
) -> None:
    _encode_job(connection, prefix, status=status)
    manifest = tmp_path / f"{prefix.replace('/', '-')}.json"
    manifest.write_text(json.dumps({"items": [{
        "rel_path": f"{prefix}/episode.mkv", "duration_seconds": duration, "resolved_policy": {"video": video or {}},
    }]}))
    connection.execute(encode_jobs.update().where(encode_jobs.c.prefix == prefix).values(manifest_path=str(manifest)))
    connection.commit()


def _computer(schedule_profile: str = "always") -> dict[str, Any]:
    return {"key": "mini", "label": "Mini", "available": True, "capabilities": ["encode_queue"],
            "allowed_libraries": ["TV"], "max_parallel_encodes": 1, "running_jobs": [],
            "schedule_profile": schedule_profile, "timezone": "UTC"}


def _daytime_window(config: MediaforceConfig) -> None:
    config.raw["encode_queue"] = {"schedule_profiles": [{
        "key": "daytime", "mode": "night", "start_hour": 14, "end_hour": 19, "timezone": "host_local",
    }]}


def test_night_window_uses_real_hours_across_clock_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.raw["encode_queue"] = {"schedule_profiles": [{
        "key": "night", "label": "Night", "mode": "night", "start_hour": 0, "end_hour": 5,
        "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "timezone": "host_local",
    }]}
    policy = encode_queue_schedule_profiles(config, schedule_policy_deps())["night"]
    windows = _windows({"timezone": "America/New_York"}, policy, NOW, NOW.replace(day=1, month=11))
    assert len(windows) == 1
    start, end = (datetime.fromisoformat(windows[0][key]) for key in ("starts_at", "ends_at"))
    assert (end - start).total_seconds() == 6 * 3600
    assert start.hour == 4
    assert end.hour == 10


def test_running_progress_keeps_unknown_finish_and_names_the_file() -> None:
    job: dict[str, Any] = {"job_id": "a", "prefix": "tv/Show/Season 2", "progress": {
        "eta_seconds": None, "current_item_rel_path": "tv/Show/Season 2/Show.S02E03.mkv", "percent_complete": 4,
    }}
    running = _running(job, NOW)
    assert running["finishes_at"] is None
    assert "S02E03" in running["title"]
    assert running["progress"] == 0.04
    job["progress"]["eta_seconds"] = 600
    assert datetime.fromisoformat(_running(job, NOW)["finishes_at"]) > NOW


def test_forecast_preserves_jobs_without_changing_queue_and_honors_pause(connection: DBClient, tmp_path: Path) -> None:
    config = _config(tmp_path)
    prefix = "tv/Show/Season 1"
    _queued_file(connection, tmp_path, prefix, 120)
    computer = _computer()
    sample = EncodeDurationSample(host_key="mini", host_label="Mini", encode_realtime_factor=1.0, quality_overhead_seconds=10)
    with patch("mediaforce.web.runtime.app_activity.load_encode_duration_samples", return_value=(sample,)):
        facts = activity_payload(connection, config, [computer], observed_at=NOW, next_season="tv/Next/Season 1", storage_admits_work=True)
        assert facts["lined_up"][0]["computer_key"] == "mini"
        assert datetime.fromisoformat(facts["lined_up"][0]["starts_at"]) == NOW
        assert facts["next_season"]["show"] == "Next"
        save_queue_state(connection, {"queue_name": "heavy", "is_paused": True, "stop_requested": False, "updated_at": NOW.isoformat()})
        paused = activity_payload(connection, config, [computer], observed_at=NOW, next_season=None, storage_admits_work=True)
        assert paused["lined_up"][0]["starts_at"] is None
        assert paused["lined_up"][0]["waiting"]
        assert connection.execute(encode_jobs.select()).mappings().one()["status"] == "queued"


def test_away_computer_and_missing_history_never_get_an_assignment(connection: DBClient, tmp_path: Path) -> None:
    _encode_job(connection, "tv/Show/Season 1", status="queued")
    computer = {"key": "away", "available": False, "capabilities": ["encode_queue"], "schedule_profile": "always"}
    result = activity_payload(connection, _config(tmp_path), [computer], observed_at=NOW, next_season=None, storage_admits_work=True)
    assert result["computers"][0]["windows"]
    assert result["lined_up"][0]["computer_key"] is None
    assert result["lined_up"][0]["starts_at"] is None


def test_stale_held_attempts_remain_visible_without_becoming_queued_work(connection: DBClient, tmp_path: Path) -> None:
    prefix = "tv/Finished/Season 1"
    _encode_job(connection, prefix, status="needs_attention")
    result = activity_payload(connection, _config(tmp_path), [], observed_at=NOW, next_season=None, storage_admits_work=True)
    assert result["earlier_holds"][0]["prefix"] == prefix
    assert not result["lined_up"]
    assert connection.execute(encode_jobs.select()).mappings().one()["status"] == "needs_attention"


@pytest.mark.parametrize("first_duration", [None, 100_000])
def test_unknown_or_long_front_file_cannot_give_later_files_an_early_start(
        connection: DBClient, tmp_path: Path, first_duration: float | None,
) -> None:
    for index, duration in enumerate((first_duration, 120)):
        _queued_file(connection, tmp_path, f"tv/Show/Season {index + 1}", duration)
    computer = _computer()
    sample = EncodeDurationSample(host_key="mini", host_label="Mini", encode_realtime_factor=1, quality_overhead_seconds=10)
    with patch("mediaforce.web.runtime.app_activity.load_encode_duration_samples", return_value=(sample,)):
        result = activity_payload(connection, _config(tmp_path), [computer], observed_at=NOW, next_season=None, storage_admits_work=True)
    first, second = result["lined_up"]
    assert second["starts_at"] is None
    if first_duration is None:
        assert first["starts_at"] is None
    else:
        assert datetime.fromisoformat(first["starts_at"]) == NOW
        assert datetime.fromisoformat(first["finishes_at"]) > datetime.fromisoformat(result["ends_at"])


@pytest.mark.parametrize(("duration", "video", "quality_overhead"), [
    (100_000, {}, 10),
    (900, {"thorough": True, "sample_every": "60s", "sample_duration": "300s"}, None),
], ids=["long_file", "thorough_manifest_without_library_id"])
def test_known_manifest_estimate_that_does_not_fit_leaves_room_for_later_work(
        connection: DBClient, tmp_path: Path, duration: float, video: dict[str, Any], quality_overhead: float | None,
) -> None:
    config = _config(tmp_path)
    _daytime_window(config)
    long_prefix, short_prefix = "tv/Long/Season 1", "tv/Short/Season 1"
    _queued_file(connection, tmp_path, long_prefix, duration, video=video)
    _queued_file(connection, tmp_path, short_prefix, 120)
    sample = EncodeDurationSample(host_key="mini", host_label="Mini", encode_realtime_factor=1, quality_overhead_seconds=quality_overhead)
    with patch("mediaforce.web.runtime.app_activity.load_encode_duration_samples", return_value=(sample,)):
        result = activity_payload(connection, config, [_computer("daytime")], observed_at=NOW, next_season=None, storage_admits_work=True)
    by_prefix = {file["prefix"]: file for file in result["lined_up"]}
    assert by_prefix[long_prefix]["starts_at"] is None
    assert datetime.fromisoformat(by_prefix[short_prefix]["starts_at"]) == NOW


@pytest.mark.parametrize("retry_delay", [600, 90_000])
def test_retry_check_does_not_claim_or_block_a_computer(
        connection: DBClient, tmp_path: Path, retry_delay: int,
) -> None:
    retry_prefix, queued_prefix = "tv/Retry/Season 1", "tv/Queued/Season 1"
    _queued_file(connection, tmp_path, retry_prefix, 120, status="retry_backoff")
    _queued_file(connection, tmp_path, queued_prefix, 120)
    connection.execute(encode_jobs.update().where(encode_jobs.c.prefix == retry_prefix).values(
        retry_not_before=(NOW + timedelta(seconds=retry_delay)).isoformat(), waiting_reason="Waiting for its computer to return",
    ))
    connection.commit()
    sample = EncodeDurationSample(host_key="mini", host_label="Mini", encode_realtime_factor=1, quality_overhead_seconds=10)
    with patch("mediaforce.web.runtime.app_activity.load_encode_duration_samples", return_value=(sample,)):
        result = activity_payload(connection, _config(tmp_path), [_computer()], observed_at=NOW, next_season=None, storage_admits_work=True)
    by_prefix = {file["prefix"]: file for file in result["lined_up"]}
    assert by_prefix[retry_prefix]["starts_at"] is None
    assert by_prefix[retry_prefix]["computer_key"] is None
    assert by_prefix[retry_prefix]["waiting"]
    assert datetime.fromisoformat(by_prefix[queued_prefix]["starts_at"]) == NOW
    assert connection.execute(encode_jobs.select().where(encode_jobs.c.prefix == retry_prefix)).mappings().one()["status"] == "retry_backoff"
