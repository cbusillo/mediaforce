import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from starlette.routing import Route

from mediaforce.library.library_settings import normalize_library_definition
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


def _endpoint(app: FastAPI, method: str, path: str = "/api/app/settings/library") -> Any:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} is not registered")


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
        "libraries": [
            {key: library[key] for key in ("key", "name", "folder", "type", "plex_path")}
            for library in facts["libraries"]
        ],
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
        (lambda body: body["libraries"][0].update(plex_path="plex/tv"), "plex_path_relative"),
        (lambda body: body["libraries"][0].update(plex_path="/plex/movies/"), "plex_path_duplicate"),
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


@pytest.mark.parametrize("hand_edit", ["base", "included"])
def test_save_after_a_hand_edit_of_the_base_config_is_refused(
        library_app: tuple[FastAPI, Path], tmp_path: Path, hand_edit: str,
) -> None:
    app, runtime_settings_path = library_app
    config_path = tmp_path / "config" / "config.toml"
    config_path.write_text(
        '[config]\ninclude_files = ["local.toml"]\n' + config_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    stale = _facts_body(app)
    if hand_edit == "base":
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("[media]", "[media]\ncatalog_refresh_hours = 12"),
            encoding="utf-8",
        )
    else:
        (tmp_path / "config" / "local.toml").write_text("[media]\ncatalog_refresh_hours = 12\n", encoding="utf-8")
    before = runtime_settings_path.read_bytes()
    stale["libraries"][0]["name"] = "Television"

    assert _save(app, stale) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == before

    fresh = _facts_body(app)
    fresh["libraries"][0]["name"] = "Television"
    status, result = _save(app, fresh)
    assert status == 200 and result["ok"] is True
    assert json.loads(runtime_settings_path.read_text())["media"]["libraries"][0]["label"] == "Television"


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


def test_new_library_never_takes_the_key_of_one_removed_in_an_earlier_save(
        library_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    app, runtime_settings_path = library_app
    body = _facts_body(app)
    body["libraries"] = [library for library in body["libraries"] if library["key"] != "movies"]
    assert _save(app, body)[0] == 200

    body = _facts_body(app)
    body["libraries"].append({"key": "", "name": "Movies", "folder": str(tmp_path / "media" / "films"), "type": "movie"})
    assert _save(app, body)[0] == 200

    stored = json.loads(runtime_settings_path.read_text())
    added = next(library for library in stored["media"]["libraries"] if library["label"] == "Movies")
    assert added["key"] != "movies"
    assert added["key"] not in stored["metadata"]["plex"]["library_roots"]
    assert not added.get("plex_path")


def test_settings_read_after_the_file_changed_shows_the_file_and_a_save_keeps_it(
        library_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["media"]["catalog_refresh_hours"] = 24
    stored["media"]["staging_root"] = str(tmp_path / "elsewhere")
    stored["media"]["source_roots"]["home"] = str(tmp_path / "media" / "home")
    stored["media"]["libraries"].append(
        {"key": "home", "label": "Home", "path": str(tmp_path / "media" / "home"), "type": "other"}
    )
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")

    body = _facts_body(app)

    assert body["catalog_refresh_hours"] == 24
    assert body["working_folder"] == str(tmp_path / "elsewhere")
    assert [library["key"] for library in body["libraries"]] == ["tv", "movies", "home"]

    body["libraries"][0]["name"] = "Television"
    assert _save(app, body)[0] == 200

    media = json.loads(runtime_settings_path.read_text())["media"]
    assert (media["catalog_refresh_hours"], media["staging_root"]) == (24, str(tmp_path / "elsewhere"))
    assert [library["key"] for library in media["libraries"]] == ["tv", "movies", "home"]


def test_plex_path_edit_changes_only_that_library(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["library_roots"]["removed_home"] = "/plex/home"
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    assert _save(app, _facts_body(app))[0] == 200  # a save without edits fills in each library's stored defaults
    before = json.loads(runtime_settings_path.read_text())
    other_tabs = ("computers", "metadata", "compression")
    revisions = {tab: json.loads(_endpoint(app, "GET", f"/api/app/settings/{tab}")().body)["revision"] for tab in other_tabs}
    body = _facts_body(app)
    body["libraries"][0]["plex_path"] = "/plex/tv"

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200
    assert {key: after.get(key) for key in OTHER_SECTIONS} == {key: before.get(key) for key in OTHER_SECTIONS}
    changed = [
        (old["key"], new.get("plex_path"))
        for old, new in zip(before["media"]["libraries"], after["media"]["libraries"], strict=True)
        if old != new
    ]
    assert changed == [("tv", "/plex/tv")]
    assert [library["plex_path"] for library in result["settings"]["libraries"]] == ["/plex/tv", "/plex/movies"]
    assert {tab: json.loads(_endpoint(app, "GET", f"/api/app/settings/{tab}")().body)["revision"] for tab in other_tabs} == revisions


def test_plex_path_facts_fall_back_to_the_plex_settings(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["media"]["libraries"][1].pop("plex_path")
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")

    libraries = {library["key"]: library["plex_path"] for library in _facts_body(app)["libraries"]}

    assert libraries == {"tv": "", "movies": "/plex/movies"}


def test_clearing_a_plex_path_removes_only_that_librarys_kept_root(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["library_roots"].update(tv="/plex/tv", removed_home="/plex/home")
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    before = json.loads(runtime_settings_path.read_text())
    body = _facts_body(app)
    body["libraries"][1]["plex_path"] = ""

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200
    untouched = [key for key in OTHER_SECTIONS if key != "metadata"]
    assert {key: after.get(key) for key in untouched} == {key: before.get(key) for key in untouched}
    expected_metadata = json.loads(json.dumps(before["metadata"]))
    del expected_metadata["plex"]["library_roots"]["movies"]
    assert after["metadata"] == expected_metadata
    movies = next(library for library in after["media"]["libraries"] if library["key"] == "movies")
    assert not movies.get("plex_path")
    assert [library["plex_path"] for library in result["settings"]["libraries"]] == ["/plex/tv", ""]


def test_save_after_a_kept_plex_root_changed_elsewhere_is_refused(library_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = library_app
    body = _facts_body(app)
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["library_roots"]["tv"] = "/plex/tv"
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    changed_elsewhere = runtime_settings_path.read_bytes()
    body["libraries"][1]["plex_path"] = ""

    assert _save(app, body) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == changed_elsewhere


def _set_movies_definition_only_path(runtime_settings_path: Path) -> None:
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["library_roots"] = {}
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")


@pytest.mark.parametrize(
    ("prepare", "change", "rescans"),
    [
        (None, lambda libraries: libraries[1].update(plex_path="/plex/films"), True),
        (None, lambda libraries: libraries[0].update(plex_path="/plex/tv"), True),
        (_set_movies_definition_only_path, lambda libraries: libraries[1].update(plex_path=""), True),
        (None, lambda libraries: libraries[0].update(name="Television"), False),
    ],
    ids=["edited", "added", "cleared_definition_only", "renamed_only"],
)
def test_library_save_rescans_when_a_plex_path_in_effect_changes(
        library_app: tuple[FastAPI, Path], prepare: Any, change: Any, rescans: bool,
) -> None:
    app, runtime_settings_path = library_app
    if prepare is not None:
        prepare(runtime_settings_path)
    assert _save(app, _facts_body(app))[0] == 200  # a save without edits fills in each library's stored defaults
    body = _facts_body(app)
    change(body["libraries"])

    with patch("mediaforce.web.app._maybe_schedule_scan") as schedule_scan:
        assert _save(app, body)[0] == 200

    assert schedule_scan.called is rescans


def test_a_library_without_its_own_name_is_named_for_its_type() -> None:
    assert normalize_library_definition({"path": "/media/tv"}, key="tv")["label"] == "TV"
    assert normalize_library_definition({"path": "/media/kids"}, key="kids_shows")["label"] == "Kids Shows"
    assert normalize_library_definition({"path": "/media/tv", "label": "Shows"}, key="tv")["label"] == "Shows"


def test_saved_roots_defer_durably_instead_of_failing_when_the_controller_window_is_closed(
        library_app: tuple[FastAPI, Path], tmp_path: Path,
) -> None:
    from functools import partial

    from mediaforce.web.runtime.job_runtime import maybe_schedule_scan

    app, settings_path = library_app
    settings = json.loads(settings_path.read_text())
    settings["remote_hosts"][0]["schedule_profile"] = "never"
    settings["media"]["catalog_refresh_hours"] = 0
    settings_path.write_text(json.dumps(settings))
    body = _facts_body(app)
    changed_root = str(tmp_path / "changed-tv")
    body["libraries"][0]["folder"] = changed_root
    with patch("mediaforce.library.evidence_hosts._controller_target_matches", return_value=True), patch(
        "mediaforce.web.app._start_scan_job_thread"
    ) as start, patch("mediaforce.web.app._refresh_host_status_cache") as refresh, patch(
        "mediaforce.web.app._maybe_schedule_scan",
        side_effect=partial(maybe_schedule_scan, deps=web_app._job_runtime_deps()),
    ):
        status, result = _save(app, body)
    assert status == 200 and result["ok"]
    assert app.state.config.catalog_refresh_interval is None
    assert app.state.config.scan_source_root_map["tv"] == Path(changed_root)
    refresh.assert_called_once()
    start.assert_called_once()
    pending = json.loads((app.state.config.paths.web_state_dir / "scan-full-catalog.job.json").read_text())
    assert pending["status"] == "queued"
    assert pending["resume_after_interruption"]
    assert pending["waiting_for_window"]
