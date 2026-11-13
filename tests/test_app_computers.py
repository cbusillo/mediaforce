from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_app_window import _item, connection
from test_kind_approvals import _config

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.encoding.duration_estimate import _query_encode_duration_samples
from mediaforce.web.runtime import app_computers
from mediaforce.web.runtime.app_computers import computers_payload

NOW = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)


def _finished(
        connection: DBClient, rel_path: str, *, host_key: str, host_label: str, at: datetime, saved: int, replaced: bool = True,
) -> None:
    item_id = _item(connection, rel_path)
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=f"/staging/{rel_path}", encode_host_key=host_key, encode_host_label=host_label,
        encode_completed_at=at.isoformat(), bytes_saved=saved, promoted_at=at.isoformat() if replaced else None,
        source_duration_seconds=3000.0, encode_duration_seconds=6000.0, updated_at=at.isoformat(),
    ))
    connection.commit()


def _computer(**changes: Any) -> dict[str, Any]:
    return {"key": "me@mini.local", "host": "me@mini.local", "label": "Mini", "platform": "macos", "computer_model": "Mac mini",
            "available": True, "capabilities": ["encode_queue"], "allowed_libraries": [], "media_access": "mounted",
            "missing_mounts": [], "running_jobs": [], "schedule_profile": "always", "schedule_open": True,
            "setup_supported": True, "setup_requires_password": False, "trust_reset_supported": False, "issues": [],
            "recovery": {"status": "ready", "last_check_at": (NOW - timedelta(minutes=2)).isoformat(), "first_failure_at": None},
            **changes}


def _payload(connection: DBClient, tmp_path: Path, *computers: dict[str, Any]) -> dict[str, Any]:
    with patch.object(app_computers, "host_targets_current_machine", return_value=False):
        return computers_payload(connection, _config(tmp_path), computers or [_computer()], observed_at=NOW)


def test_work_done_counts_files_by_period_and_space_only_from_replaced_files(connection: DBClient, tmp_path: Path) -> None:
    local_midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    _finished(connection, "tv/Show/Season 1/Show.S01E01.mkv", host_key="me@mini.local", host_label="Mini",
              at=local_midnight + timedelta(minutes=1), saved=100)
    _finished(connection, "tv/Show/Season 1/Show.S01E02.mkv", host_key="me@mini.local", host_label="Mini",
              at=local_midnight + timedelta(minutes=2), saved=50, replaced=False)
    _finished(connection, "tv/Show/Season 1/Show.S01E03.mkv", host_key="me@old-name", host_label="Mini",
              at=NOW - timedelta(days=3), saved=200)
    _finished(connection, "movies/Film (1999)/Film.mkv", host_key="me@mini.local", host_label="Mini",
              at=NOW - timedelta(days=30), saved=400)
    _finished(connection, "tv/Show/Season 1/Show.S01E04.mkv", host_key="me@studio", host_label="Studio",
              at=NOW - timedelta(minutes=5), saved=800)

    done = _payload(connection, tmp_path)["computers"][0]["done"]

    assert done["today"] == {"files": 2, "bytes_saved": 100}
    assert done["week"] == {"files": 3, "bytes_saved": 300}
    assert done["all"] == {"files": 4, "bytes_saved": 700}


def test_files_are_named_by_show_season_and_episode(connection: DBClient, tmp_path: Path) -> None:
    _finished(connection, "tv/The Show/Season 2/the.show.s02e07.1080p.bluray-group.mkv", host_key="me@mini.local",
              host_label="Mini", at=NOW - timedelta(hours=1), saved=10)
    running_job = {"job_id": "j", "prefix": "tv/The Show/Season 2", "item_count": 1, "progress": {
        "current_item_rel_path": "tv/The Show/Season 2/the.show.s02e08.mkv", "percent_complete": 40.0,
        "progress_state": "encoding", "eta_seconds": 600, "updated_at": NOW.isoformat(),
    }}

    computer = _payload(connection, tmp_path, _computer(running_jobs=[running_job]))["computers"][0]

    assert {key: computer["recent"][0][key] for key in ("show", "season", "episode")} == {"show": "The Show", "season": 2, "episode": 7}
    running = computer["running"][0]
    assert (running["show"], running["season"], running["episode"], running["progress"]) == ("The Show", 2, 8, 0.4)
    assert datetime.fromisoformat(running["finishes_at"]) == NOW + timedelta(seconds=600)


def test_last_answered_is_the_last_good_check_or_when_answers_stopped(connection: DBClient, tmp_path: Path) -> None:
    stopped = (NOW - timedelta(hours=3)).isoformat()
    away = _computer(key="me@laptop", available=False,
                     recovery={"status": "retrying", "last_check_at": NOW.isoformat(), "first_failure_at": stopped})

    answered = [row["last_answered_at"] for row in _payload(connection, tmp_path, _computer(), away)["computers"]]

    assert answered == [_computer()["recovery"]["last_check_at"], stopped]


def test_speed_is_unknown_until_the_computer_finishes_a_file(connection: DBClient, tmp_path: Path) -> None:
    with patch.object(app_computers, "load_encode_duration_samples", _query_encode_duration_samples):
        unknown = _payload(connection, tmp_path)["computers"][0]["hour_takes_seconds"]
        _finished(connection, "tv/Show/Season 1/Show.S01E01.mkv", host_key="me@mini.local", host_label="Mini",
                  at=NOW - timedelta(hours=1), saved=10)
        hour_takes = _payload(connection, tmp_path)["computers"][0]["hour_takes_seconds"]
    assert unknown is None
    assert hour_takes is not None and hour_takes >= 2 * 3600
