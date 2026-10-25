import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from starlette.routing import Route

from mediaforce.web import app as web_app

OTHER_SECTIONS = ("remote_hosts", "encode_queue", "video", "metadata", "folder_policy_overrides")


def _json_request(payload: dict[str, Any]) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")]},
        receive,
    )


def _endpoint(app: FastAPI, method: str) -> Any:
    for route in app.routes:
        if isinstance(route, Route) and route.path == "/api/app/settings/library" and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} /api/app/settings/library is not registered")


def _seed_runtime_settings(root: Path) -> dict[str, Any]:
    return {
        "media": {
            "source_roots": {"tv": str(root / "media" / "tv"), "movies": str(root / "media" / "movies")},
            "libraries": [
                {"key": "tv", "label": "TV", "path": str(root / "media" / "tv"), "type": "tv",
                 "availability": "production", "color": "#a16207"},
                {"key": "movies", "label": "Movies", "path": str(root / "media" / "movies"), "type": "movie",
                 "availability": "production", "default_profile": "movie_quality_first",
                 "plex_path": "/plex/movies"},
            ],
            "staging_root": str(root / "transcode"),
            "archive_root": str(root / "transcode" / "_replaced"),
            "catalog_refresh_hours": 6,
        },
        "remote_hosts": [
            {"host": "studio.local", "label": "Studio", "priority": "2", "max_parallel_encodes": 2,
             "schedule_profile": "nights", "capabilities": ["encode_queue"]},
        ],
        "encode_queue": {
            "scheduler": {"mode": "night", "start_hour": 22, "end_hour": 7, "timezone": "host_local"},
            "schedule_profiles": [
                {"key": "nights", "label": "Nights", "mode": "night", "timezone": "host_local",
                 "days_of_week": ["mon", "tue"], "all_day_days_of_week": ["sat"], "start_hour": 21, "end_hour": 6},
            ],
        },
        "video": {"target_size_mb": 700, "target_runtime_minutes": 45, "max_height": 1080},
        "metadata": {
            "plex": {"enabled": True, "base_url": "http://plex.local:32400",
                     "library_roots": {"movies": "/plex/movies"}},
            "tmdb": {"enabled": False},
        },
        "folder_policy_overrides": [{"path_prefix": "tv/Show", "target_size_mb": 500}],
    }


@pytest.fixture
def library_app(tmp_path: Path) -> Iterator[tuple[FastAPI, Path]]:
    (tmp_path / "config").mkdir()
    runtime_settings_path = tmp_path / "state" / "runtime-settings.json"
    runtime_settings_path.parent.mkdir()
    runtime_settings_path.write_text(json.dumps(_seed_runtime_settings(tmp_path)), encoding="utf-8")
    config_path = tmp_path / "config" / "config.toml"
    config_path.write_text(
        "\n".join([
            "[state]",
            'db_path = "state/library.sqlite3"',
            'run_manifest_dir = "state/runs"',
            'web_state_dir = "state/web"',
            'review_dir = "state/review"',
            f'runtime_settings_path = "{runtime_settings_path}"',
            'runtime_reservation_dir = "state/reservations"',
            "[media]",
            "[media.source_roots]",
            "[video]",
            "[metadata]",
        ]),
        encoding="utf-8",
    )
    with (
        patch("mediaforce.web.app.purge_transient_artifacts"),
        patch("mediaforce.web.app._start_calibration_queue_worker"),
        patch("mediaforce.web.app._start_encode_queue_worker"),
        patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]),
        patch("mediaforce.web.app._maybe_schedule_scan"),
    ):
        yield web_app.create_app(config_path), runtime_settings_path


def _save(app: FastAPI, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(_endpoint(app, "POST")(_json_request(body)))
    return response.status_code, json.loads(response.body)


def _facts_body(app: FastAPI) -> dict[str, Any]:
    facts = json.loads(_endpoint(app, "GET")().body)
    return {
        "libraries": [{key: library[key] for key in ("key", "name", "folder", "type")} for library in facts["libraries"]],
        "working_folder": facts["working_folder"],
        "catalog_refresh_hours": facts["catalog_refresh_hours"],
        "revision": facts["revision"],
    }


def test_library_save_changes_only_the_library_section(library_app: tuple[FastAPI, Path], tmp_path: Path) -> None:
    app, runtime_settings_path = library_app
    before = json.loads(runtime_settings_path.read_text())
    body = _facts_body(app)
    body["libraries"][0]["name"] = "Television"
    body["libraries"].append({"key": "", "name": "Home Videos", "folder": str(tmp_path / "media" / "home"), "type": "other"})
    body["working_folder"] = str(tmp_path / "work")
    body["catalog_refresh_hours"] = 12

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200 and result["ok"] is True
    assert {key: after.get(key) for key in OTHER_SECTIONS} == {key: before.get(key) for key in OTHER_SECTIONS}
    assert after["media"]["staging_root"] == str(tmp_path / "work")
    assert after["media"]["catalog_refresh_hours"] == 12
    libraries = {library["key"]: library for library in after["media"]["libraries"]}
    assert libraries["tv"]["label"] == "Television"
    assert libraries["home_videos"]["path"] == str(tmp_path / "media" / "home")
    assert libraries["movies"]["default_profile"] == "movie_quality_first"
    assert libraries["movies"]["plex_path"] == "/plex/movies"


def test_library_type_change_needs_the_preview_acknowledgement(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    before = runtime_settings_path.read_bytes()
    body = _facts_body(app)
    body["libraries"][1]["type"] = "other"

    assert _save(app, body) == (400, {"ok": False, "code": "library_type_unconfirmed"})
    assert runtime_settings_path.read_bytes() == before

    body["libraries"][1]["type_change_confirmation"] = "movies:movie->other"
    body["revision"] = _facts_body(app)["revision"]
    status, _result = _save(app, body)

    movies = next(item for item in json.loads(runtime_settings_path.read_text())["media"]["libraries"] if item["key"] == "movies")
    assert status == 200
    assert (movies["type"], movies["availability"], movies["default_profile"]) == ("other", "browse_only", "other_conservative")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda body: body.update(working_folder=""), "working_folder_missing"),
        (lambda body: body["libraries"][0].update(folder="relative/tv"), "library_folder_not_absolute"),
        (lambda body: body["libraries"][0].update(folder=""), "library_incomplete"),
        (lambda body: body.update(libraries=[]), "no_libraries"),
    ],
)
def test_refused_library_save_writes_nothing_and_answers_with_a_code(
        library_app: tuple[FastAPI, Path], change: Any, code: str,
) -> None:
    app, runtime_settings_path = library_app
    before = runtime_settings_path.read_bytes()
    body = _facts_body(app)
    change(body)

    assert _save(app, body) == (400, {"ok": False, "code": code})
    assert runtime_settings_path.read_bytes() == before


def test_second_save_from_the_same_loaded_settings_is_refused(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    first = _facts_body(app)
    second = json.loads(json.dumps(first))
    first["libraries"][0]["name"] = "Television"
    second["libraries"][1]["name"] = "Films"

    assert _save(app, first)[0] == 200
    after_first = runtime_settings_path.read_bytes()

    assert _save(app, second) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == after_first


def test_save_after_the_library_section_changed_elsewhere_is_refused(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    body = _facts_body(app)
    stored = json.loads(runtime_settings_path.read_text())
    stored["media"]["catalog_refresh_hours"] = 24
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    changed_elsewhere = runtime_settings_path.read_bytes()
    body["libraries"][0]["name"] = "Television"

    assert _save(app, body) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == changed_elsewhere


def test_new_library_never_takes_the_key_of_one_removed_in_the_same_save(
        library_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    app, runtime_settings_path = library_app
    body = _facts_body(app)
    body["libraries"] = [library for library in body["libraries"] if library["key"] != "movies"]
    body["libraries"].append({"key": "", "name": "Movies", "folder": str(tmp_path / "media" / "films"), "type": "movie"})

    assert _save(app, body)[0] == 200

    libraries = json.loads(runtime_settings_path.read_text())["media"]["libraries"]
    added = next(library for library in libraries if library["label"] == "Movies")
    assert added["key"] != "movies"
    assert "plex_path" not in added or not added["plex_path"]
