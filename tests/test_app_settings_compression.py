import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from mediaforce.web.settings_runtime import settings_section_revision
from test_app_settings_computers import _endpoint
from test_app_settings_library import _json_request, library_app  # noqa: F401  (`library_app` is the fixture)

PATH = "/api/app/settings/compression"
UNTOUCHED_SECTIONS = ("media", "remote_hosts", "encode_queue", "metadata", "folder_policy_overrides")
OTHER_TABS = ("/api/app/settings/library", "/api/app/settings/computers", "/api/app/settings/metadata")


def _facts(app: FastAPI, path: str = PATH) -> dict[str, Any]:
    return json.loads(_endpoint(app, path, "GET")().body)


def _body(app: FastAPI) -> dict[str, Any]:
    facts = _facts(app)
    return {
        key: facts[key]
        for key in (
            "aim", "size_goal_mb", "size_goal_minutes", "size_limit_percent", "largest_percent", "tallest_picture",
            "grain", "measurement", "appearance_target", "appearance_minimum", "detail_target", "detail_minimum",
            "sample_method", "revision",
        )
    }


def _save(app: FastAPI, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(_endpoint(app, PATH, "POST")(_json_request(body)))
    return response.status_code, json.loads(response.body)


@pytest.fixture
def compression_app(library_app: tuple[FastAPI, Path]) -> tuple[FastAPI, Path]:  # noqa: F811
    """The library fixture with a sample size limit unlike the finished-file limit and a key no form shows."""
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["video"].update(
        sample_projection_tolerance_percent=12, final_output_tolerance_percent=5, kept_by_hand="yes",
    )
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    return app, runtime_settings_path


def test_compression_save_changes_only_the_video_section(compression_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = compression_app
    before = json.loads(runtime_settings_path.read_text())
    body = _body(app)
    body.update(
        aim="quality_first_sample", size_goal_mb=450, size_goal_minutes=30, largest_percent=60, tallest_picture=720,
        grain=4, measurement="xpsnr", appearance_target=90, appearance_minimum=85, detail_target=44,
        detail_minimum=40,
    )

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200 and result["ok"] is True
    assert {key: after.get(key) for key in UNTOUCHED_SECTIONS} == {key: before.get(key) for key in UNTOUCHED_SECTIONS}
    video = after["video"]
    assert (video["decision_model"], video["target_size_mb"], video["target_runtime_minutes"]) == (
        "quality_first_sample", 450, 30,
    )
    assert (video["max_encoded_percent"], video["max_height"], video["default_grain"]) == (60, 720, 4)
    assert (video["quality_metric"], video["target_vmaf"], video["min_target_vmaf"]) == ("xpsnr", 90, 85)
    assert (video["target_xpsnr"], video["min_target_xpsnr"]) == (44, 40)
    assert (video["sample_projection_tolerance_percent"], video["final_output_tolerance_percent"]) == (12, 5)
    assert video["kept_by_hand"] == "yes"
    assert result["settings"]["size_goal_mb"] == 450


def test_changing_the_size_limit_holds_samples_to_it_as_the_website_does(
        compression_app: tuple[FastAPI, Path],
) -> None:
    app, runtime_settings_path = compression_app
    body = _body(app)
    body["size_limit_percent"] = 8

    assert _save(app, body)[0] == 200

    video = json.loads(runtime_settings_path.read_text())["video"]
    assert (video["sample_projection_tolerance_percent"], video["final_output_tolerance_percent"]) == (8, 8)


def test_compression_save_leaves_the_other_tabs_current(compression_app: tuple[FastAPI, Path]) -> None:
    app, _runtime_settings_path = compression_app
    revisions = {path: _facts(app, path)["revision"] for path in OTHER_TABS}
    body = _body(app)
    body["grain"] = 12

    assert _save(app, body)[0] == 200

    assert {path: _facts(app, path)["revision"] for path in OTHER_TABS} == revisions


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"size_goal_mb": 0}, "size_goal_invalid"),
        ({"size_goal_minutes": 2000}, "size_goal_minutes_invalid"),
        ({"size_limit_percent": 0}, "size_limit_invalid"),
        ({"largest_percent": 101}, "largest_size_invalid"),
        ({"tallest_picture": 720.5}, "tallest_picture_invalid"),
        ({"tallest_picture": 5000}, "tallest_picture_invalid"),
        ({"grain": 51}, "grain_invalid"),
        ({"appearance_target": "high"}, "appearance_target_invalid"),
        ({"appearance_minimum": 0}, "appearance_minimum_invalid"),
        ({"detail_target": 101}, "detail_target_invalid"),
        ({"detail_minimum": -1}, "detail_minimum_invalid"),
        ({"appearance_target": 80, "appearance_minimum": 81}, "appearance_minimum_above_target"),
        ({"detail_target": 40, "detail_minimum": 41}, "detail_minimum_above_target"),
        ({"aim": "smallest"}, "unknown_aim"),
        ({"measurement": "VMAF"}, "unknown_measurement"),
        ({"sample_method": "slow"}, "unknown_sample_method"),
        ({"grain": True}, "invalid_settings"),
    ],
)
def test_refused_compression_save_writes_nothing_and_answers_with_a_code(
        compression_app: tuple[FastAPI, Path], change: dict[str, Any], code: str,
) -> None:
    app, runtime_settings_path = compression_app
    before = runtime_settings_path.read_bytes()
    body = _body(app)
    body.update(change)

    assert _save(app, body) == (400, {"ok": False, "code": code})
    assert runtime_settings_path.read_bytes() == before


def test_compression_save_missing_a_setting_writes_nothing(compression_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = compression_app
    before = runtime_settings_path.read_bytes()
    body = _body(app)
    del body["grain"]

    assert _save(app, body) == (400, {"ok": False, "code": "invalid_settings"})
    assert runtime_settings_path.read_bytes() == before


def test_second_compression_save_from_the_same_loaded_settings_is_refused(
        compression_app: tuple[FastAPI, Path],
) -> None:
    app, runtime_settings_path = compression_app
    first = _body(app)
    second = dict(first)
    first["grain"] = 2
    second["tallest_picture"] = 720

    assert _save(app, first)[0] == 200
    after_first = runtime_settings_path.read_bytes()

    assert _save(app, second) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == after_first


def test_compression_save_after_the_section_changed_elsewhere_is_refused(
        compression_app: tuple[FastAPI, Path],
) -> None:
    app, runtime_settings_path = compression_app
    body = _body(app)
    stored = json.loads(runtime_settings_path.read_text())
    stored["video"]["sample_projection_tolerance_percent"] = 20
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    changed_elsewhere = runtime_settings_path.read_bytes()
    body["grain"] = 2

    assert _save(app, body) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == changed_elsewhere


def test_compression_facts_read_the_file_as_it_is_now_with_its_revision(
        compression_app: tuple[FastAPI, Path],
) -> None:
    app, runtime_settings_path = compression_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["video"].update(target_size_mb=320, max_height=0)
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")

    facts = _facts(app)

    assert (facts["size_goal_mb"], facts["tallest_picture"]) == (320, 0)
    assert facts["revision"] == settings_section_revision(stored, "compression")
    body = _body(app)
    body["grain"] = 2
    assert _save(app, body)[0] == 200
    assert json.loads(runtime_settings_path.read_text())["video"]["target_size_mb"] == 320
