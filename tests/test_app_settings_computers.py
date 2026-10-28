import asyncio
import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.routing import Route

from mediaforce.core.config import load_config
from mediaforce.core.db import open_db
from mediaforce.hosts.types import HostStatus
from mediaforce.web import app as web_app
from mediaforce.web.runtime.encode_runtime import _selected_encode_schedule_close_deadline

from test_app_settings_library import _json_request, library_app  # noqa: F401  (`library_app` is the fixture)

UNTOUCHED_SECTIONS = ("media", "video", "metadata", "folder_policy_overrides")


def _endpoint(app: FastAPI, path: str, method: str) -> Any:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} is not registered")


def _studio(root: Path) -> dict[str, Any]:
    """A stored computer as the settings save writes it, plus a field no Settings form shows."""
    return {
        "host": "studio.local",
        "label": "Studio",
        "repo_path": "/Users/operator/mediaforce",
        "wake_mac": "00:11:22:33:44:55",
        "start_command": "wake-studio",
        "stop_command": "sleep-studio",
        "start_timeout_seconds": 300,
        "media_access": "stream",
        "priority": "2",
        "max_parallel_encodes": 2,
        "schedule_profile": "nights",
        "capabilities": ["encode_queue", "sample_calibration"],
        "allowed_libraries": ["tv"],
        "source_roots": {"tv": "/Volumes/remote/tv"},
        "staging_root": str(root / "studio-work"),
        "scratch_root": "/var/tmp/mediaforce",
        "ffmpeg_path": "/opt/ffmpeg/bin/ffmpeg",
    }


def _store_computers(runtime_settings_path: Path, root: Path) -> dict[str, Any]:
    stored = json.loads(runtime_settings_path.read_text())
    stored["remote_hosts"] = [
        _studio(root),
        {"host": "mini.local", "label": "Mini", "start_timeout_seconds": 180, "media_access": "mounted",
         "priority": "0", "max_parallel_encodes": 1, "schedule_profile": "always", "capabilities": ["encode_queue"],
         "platform": "darwin"},
    ]
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    return stored


def _facts(app: FastAPI, path: str = "/api/app/settings/computers") -> dict[str, Any]:
    return json.loads(_endpoint(app, path, "GET")().body)


def _body(app: FastAPI) -> dict[str, Any]:
    facts = _facts(app)
    return {"computers": facts["computers"], "schedules": facts["schedules"], "revision": facts["revision"]}


def _save(app: FastAPI, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(_endpoint(app, "/api/app/settings/computers", "POST")(_json_request(body)))
    return response.status_code, json.loads(response.body)


@pytest.fixture
def computers_app(library_app: tuple[FastAPI, Path], tmp_path: Path) -> tuple[FastAPI, Path]:  # noqa: F811
    app, runtime_settings_path = library_app
    _store_computers(runtime_settings_path, tmp_path)
    return app, runtime_settings_path


def test_computer_save_changes_only_computers_and_schedules(
        computers_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    app, runtime_settings_path = computers_app
    before = json.loads(runtime_settings_path.read_text())
    body = _body(app)
    body["computers"][0]["name"] = "Studio Mac"
    body["schedules"].append(
        {"key": "new-1", "name": "Weekends", "days": [], "all_day_days": ["sat", "sun"], "start_hour": 0, "end_hour": 0}
    )
    body["computers"].append({
        "address": "laptop.local", "name": "Laptop", "abilities": ["sample_calibration"], "schedule": "new-1",
        "libraries": [], "at_once": 1, "priority": 5, "media_access": "mounted", "library_folders": [],
    })

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200 and result["ok"] is True
    assert {key: after.get(key) for key in UNTOUCHED_SECTIONS} == {key: before.get(key) for key in UNTOUCHED_SECTIONS}
    studio, mini, laptop = after["remote_hosts"]
    assert studio == {**_studio(tmp_path), "label": "Studio Mac"}
    assert mini == before["remote_hosts"][1]
    weekends = after["encode_queue"]["schedule_profiles"][-1]
    assert (weekends["key"], weekends["all_day_days_of_week"]) == ("weekends", ["sat", "sun"])
    assert laptop["schedule_profile"] == "weekends"
    assert "ffmpeg_path" not in laptop and "platform" not in laptop
    assert after["encode_queue"]["scheduler"]["start_hour"] == before["encode_queue"]["scheduler"]["start_hour"]


def test_computer_save_leaves_the_libraries_tab_current(computers_app: tuple[FastAPI, Path]) -> None:
    app, _runtime_settings_path = computers_app
    library_revision = _facts(app, "/api/app/settings/library")["revision"]
    body = _body(app)
    body["computers"][1]["at_once"] = 3

    assert _save(app, body)[0] == 200

    assert _facts(app, "/api/app/settings/library")["revision"] == library_revision


def test_removing_and_reordering_keeps_each_computers_hidden_fields_with_it(
        computers_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    app, runtime_settings_path = computers_app
    body = _body(app)
    studio, mini = body["computers"]
    body["computers"] = [mini, studio]
    assert _save(app, body)[0] == 200
    stored = json.loads(runtime_settings_path.read_text())["remote_hosts"]
    assert [host["host"] for host in stored] == ["mini.local", "studio.local"]
    assert stored[0]["platform"] == "darwin" and stored[1] == _studio(tmp_path)

    body = _body(app)
    body["computers"] = [computer for computer in body["computers"] if computer["address"] != "studio.local"]
    body["computers"].append({**body["computers"][0], "id": "", "address": "studio.local", "name": "Studio"})
    assert _save(app, body)[0] == 200
    readded = json.loads(runtime_settings_path.read_text())["remote_hosts"][1]
    assert "platform" not in readded and "ffmpeg_path" not in readded


def test_new_schedule_never_takes_the_key_of_one_removed_in_the_same_save(computers_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = computers_app
    body = _body(app)
    for computer in body["computers"]:
        computer["schedule"] = "always"
    body["schedules"] = [
        {"key": "", "name": "Nights", "days": ["fri"], "all_day_days": [], "start_hour": 20, "end_hour": 2}
    ]

    assert _save(app, body)[0] == 200

    profiles = json.loads(runtime_settings_path.read_text())["encode_queue"]["schedule_profiles"]
    assert [(profile["key"], profile["days_of_week"]) for profile in profiles] == [("nights_2", ["fri"])]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda body: body["schedules"][0].update(start_hour=24), "schedule_hours_invalid"),
        (lambda body: body["schedules"][0].update(end_hour="7"), "schedule_hours_invalid"),
        (lambda body: body["schedules"][0].update(days=[], all_day_days=[]), "schedule_days_missing"),
        (lambda body: body.update(schedules=[]), "schedule_unknown"),
        (lambda body: body["computers"][1].update(schedule="weekdays"), "schedule_unknown"),
        (lambda body: body["computers"][1].update(address=""), "computer_address_missing"),
        (lambda body: body["computers"][1].update(address="studio.local"), "computer_address_duplicate"),
        (lambda body: body["computers"][1].update(abilities=[]), "computer_abilities_missing"),
        (lambda body: body["computers"][1].update(priority="first"), "computer_priority_invalid"),
        (lambda body: body["computers"][1].update(scratch_folder="tmp"), "computer_scratch_folder_not_absolute"),
        (
            lambda body: body["computers"][1].update(library_folders=[{"library": "music", "folder": "/m"}]),
            "computer_library_folder_unknown",
        ),
    ],
)
def test_refused_computer_save_writes_nothing_and_answers_with_a_code(
        computers_app: tuple[FastAPI, Path], change: Any, code: str,
) -> None:
    app, runtime_settings_path = computers_app
    before = runtime_settings_path.read_bytes()
    body = _body(app)
    change(body)

    assert _save(app, body) == (400, {"ok": False, "code": code})
    assert runtime_settings_path.read_bytes() == before


def test_second_computer_save_from_the_same_loaded_settings_is_refused(computers_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = computers_app
    first = _body(app)
    second = json.loads(json.dumps(first))
    first["computers"][0]["name"] = "Studio Mac"
    second["computers"][1]["name"] = "Mac mini"

    assert _save(app, first)[0] == 200
    after_first = runtime_settings_path.read_bytes()

    assert _save(app, second) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == after_first


def test_computer_save_after_schedules_changed_elsewhere_is_refused(computers_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = computers_app
    body = _body(app)
    stored = json.loads(runtime_settings_path.read_text())
    stored["encode_queue"]["schedule_profiles"][0]["end_hour"] = 8
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    changed_elsewhere = runtime_settings_path.read_bytes()
    body["computers"][0]["name"] = "Studio Mac"

    assert _save(app, body) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == changed_elsewhere


def test_computer_facts_read_the_file_as_it_is_now(computers_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = computers_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["remote_hosts"][1]["label"] = "Renamed elsewhere"
    stored["encode_queue"]["schedule_profiles"][0]["label"] = "Late nights"
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")

    body = _body(app)

    assert [computer["name"] for computer in body["computers"]] == ["Studio", "Renamed elsewhere"]
    assert [schedule["name"] for schedule in body["schedules"]] == ["Late nights"]
    body["computers"][0]["at_once"] = 4
    assert _save(app, body)[0] == 200
    saved = json.loads(runtime_settings_path.read_text())
    assert saved["remote_hosts"][1]["label"] == "Renamed elsewhere"
    assert saved["encode_queue"]["schedule_profiles"][0]["label"] == "Late nights"


@pytest.mark.parametrize("day", ["2026-10-28", "2026-11-01", "2027-03-14"])
@pytest.mark.parametrize("finish_current_file", [False, True])
def test_saved_night_window_reaches_dispatch_for_only_the_selected_computer(
    computers_app: tuple[FastAPI, Path], finish_current_file: bool, day: str,
) -> None:
    app, runtime_settings_path = computers_app
    before = json.loads(runtime_settings_path.read_text())
    body = _body(app)
    body["schedules"].append({
        "key": "new-night", "name": "Night window", "days": body["schedules"][0]["days"] + ["wed", "thu", "fri", "sat", "sun"],
        "all_day_days": [], "start_hour": 0, "end_hour": 5, "finish_current_file": finish_current_file,
    })
    body["computers"][0]["schedule"] = "new-night"
    assert _save(app, body)[0] == 200
    assert _facts(app)["schedules"][-1]["finish_current_file"] is finish_current_file
    stored = json.loads(runtime_settings_path.read_text())
    assert stored["remote_hosts"][1] == before["remote_hosts"][1]
    config = load_config(runtime_settings_path.parent.parent / "config" / "config.toml")
    status = HostStatus(
        key="studio.local", label="Studio", mode="ssh", priority=2, capabilities=["encode_queue"],
        available=True, message="Ready", missing_paths=[], schedule_timezone="America/New_York",
    )
    for hour, minute, second, allowed in [(23, 59, 59, False), (0, 0, 0, True), (4, 59, 59, True), (5, 0, 0, False)]:
        now = datetime.fromisoformat(day).replace(hour=hour, minute=minute, second=second, tzinfo=ZoneInfo("America/New_York"))
        with open_db(config.paths.db_path) as connection, patch("mediaforce.web.app._safe_collect_host_statuses", return_value=[status]):
            rows = web_app._host_runtime_rows(connection, config, now=now)
        assert rows[0]["queue_active"] is allowed
        assert rows[0]["finish_current_file"] is finish_current_file
        if allowed:
            closes_at = datetime.fromisoformat(rows[0]["schedule_closes_at"])
            assert closes_at == datetime.fromisoformat(day).replace(hour=5, tzinfo=ZoneInfo("America/New_York"))
            assert (_selected_encode_schedule_close_deadline({"item_count": 1}, rows[0]) is None) is finish_current_file


@pytest.mark.parametrize("submitted", [None, False])
def test_older_computers_save_preserves_the_close_option_unless_explicitly_changed(
    computers_app: tuple[FastAPI, Path], submitted: bool | None,
) -> None:
    app, runtime_settings_path = computers_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["encode_queue"]["schedule_profiles"][0]["finish_current_file"] = True
    runtime_settings_path.write_text(json.dumps(stored))
    body = _body(app)
    if submitted is None:
        body["schedules"][0].pop("finish_current_file")
    else:
        body["schedules"][0]["finish_current_file"] = submitted

    assert _save(app, body)[0] == 200
    assert _facts(app)["schedules"][0]["finish_current_file"] is (submitted is None)
