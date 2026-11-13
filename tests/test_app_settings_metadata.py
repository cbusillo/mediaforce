import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from mediaforce.library.service_keys import KeyStore
from test_app_settings_computers import _endpoint
from test_app_settings_library import APP_HEADERS, _json_request, library_app  # noqa: F401  (`library_app` is the fixture)

PATH = "/api/app/settings/metadata"
UNTOUCHED_SECTIONS = ("media", "remote_hosts", "encode_queue", "video", "folder_policy_overrides")


def _facts(app: FastAPI, path: str = PATH) -> dict[str, Any]:
    return json.loads(_endpoint(app, path, "GET")().body)


def _body(app: FastAPI) -> dict[str, Any]:
    facts = _facts(app)
    return {
        "plex": {"enabled": facts["plex"]["enabled"], "address": facts["plex"]["address"]},
        "tmdb": {"enabled": facts["tmdb"]["enabled"]},
        "revision": facts["revision"],
    }


def _save(app: FastAPI, body: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(_endpoint(app, PATH, "POST")(_json_request(body, headers)))
    return response.status_code, json.loads(response.body)


@pytest.fixture
def metadata_app(library_app: tuple[FastAPI, Path]) -> tuple[FastAPI, Path]:  # noqa: F811
    """The library fixture plus a Plex path kept for a library that is no longer configured."""
    app, runtime_settings_path = library_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["library_roots"]["removed_home"] = "/plex/home"
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    return app, runtime_settings_path


def test_metadata_save_changes_only_the_metadata_section(metadata_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = metadata_app
    before = json.loads(runtime_settings_path.read_text())
    body = _body(app)
    body["plex"] = {"enabled": False, "address": "https://plex.example.com:32400/"}
    body["tmdb"] = {"enabled": True}

    status, result = _save(app, body)

    after = json.loads(runtime_settings_path.read_text())
    assert status == 200 and result["ok"] is True
    assert {key: after.get(key) for key in UNTOUCHED_SECTIONS} == {key: before.get(key) for key in UNTOUCHED_SECTIONS}
    plex, tmdb = after["metadata"]["plex"], after["metadata"]["tmdb"]
    assert (plex["enabled"], plex["base_url"], tmdb["enabled"]) == (False, "https://plex.example.com:32400", True)
    assert plex["library_roots"] == {"movies": "/plex/movies", "removed_home": "/plex/home"}
    assert result["settings"]["plex"]["address"] == "https://plex.example.com:32400"


def test_metadata_save_leaves_the_other_tabs_current(metadata_app: tuple[FastAPI, Path]) -> None:
    app, _runtime_settings_path = metadata_app
    library_revision = _facts(app, "/api/app/settings/library")["revision"]
    computers_revision = _facts(app, "/api/app/settings/computers")["revision"]
    body = _body(app)
    body["tmdb"]["enabled"] = True

    assert _save(app, body)[0] == 200

    assert _facts(app, "/api/app/settings/library")["revision"] == library_revision
    assert _facts(app, "/api/app/settings/computers")["revision"] == computers_revision


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda body: body["plex"].update(address="plex.local:32400"), "plex_address_invalid"),
        (lambda body: body["plex"].update(address="ftp://plex.local"), "plex_address_invalid"),
        (lambda body: body["plex"].update(address="http://user:pass@plex.local"), "plex_address_invalid"),
        (lambda body: body["plex"].update(address="http://plex.local/?x=1"), "plex_address_invalid"),
        (lambda body: body["plex"].update(enabled="false"), "invalid_settings"),
        (lambda body: body.update(tmdb={}), "invalid_settings"),
    ],
)
def test_refused_metadata_save_writes_nothing_and_answers_with_a_code(
        metadata_app: tuple[FastAPI, Path], change: Any, code: str,
) -> None:
    app, runtime_settings_path = metadata_app
    before = runtime_settings_path.read_bytes()
    body = _body(app)
    change(body)

    assert _save(app, body) == (400, {"ok": False, "code": code})
    assert runtime_settings_path.read_bytes() == before


def test_key_fields_sent_by_the_app_are_never_stored(
        metadata_app: tuple[FastAPI, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime_settings_path = metadata_app
    monkeypatch.delenv("MEDIAFORCE_PLEX_TOKEN", raising=False)
    body = _body(app)
    body["plex"].update(key_found=True, token_configured=True, token_env="MEDIAFORCE_OTHER", token="sent-by-client")
    body["tmdb"].update(key_found=True, token_configured=True, token="sent-by-client")

    assert _save(app, body)[0] == 200

    stored = json.loads(runtime_settings_path.read_text())["metadata"]
    assert set(stored["plex"]) == {"enabled", "base_url", "token_env", "refresh_interval_hours", "library_roots"}
    assert set(stored["tmdb"]) == {"enabled", "base_url", "token_env", "refresh_interval_hours"}
    assert stored["plex"]["token_env"] == "MEDIAFORCE_PLEX_TOKEN"
    assert _facts(app)["plex"]["key_set"] is False


def test_metadata_facts_say_whether_a_key_is_set_and_never_carry_one(
        metadata_app: tuple[FastAPI, Path], key_store: KeyStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _runtime_settings_path = metadata_app
    monkeypatch.setenv("MEDIAFORCE_PLEX_TOKEN", "plex-key-from-env")
    monkeypatch.delenv("MEDIAFORCE_TMDB_TOKEN", raising=False)
    key_store.write("plex", "plex-key-from-keychain")

    body = _endpoint(app, PATH, "GET")().body.decode()
    facts = json.loads(body)

    assert (facts["plex"]["key_set"], facts["tmdb"]["key_set"]) == (True, False)
    assert "plex-key" not in body


def _save_key(app: FastAPI, body: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, str]:
    response = asyncio.run(_endpoint(app, f"{PATH}/key", "POST")(_json_request(body, headers)))
    return response.status_code, response.body.decode()


def _connections(app: FastAPI, headers: dict[str, str] | None = None) -> Response:
    return _endpoint(app, f"{PATH}/connections", "GET")(_json_request({}, headers, method="GET"))


def test_a_key_entered_in_settings_is_kept_in_the_key_store_and_nowhere_else(
        metadata_app: tuple[FastAPI, Path], key_store: KeyStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, _runtime_settings_path = metadata_app
    monkeypatch.delenv("MEDIAFORCE_TMDB_TOKEN", raising=False)

    status, answer = _save_key(app, {"service": "tmdb", "key": " eyJ.tmdb-secret_key "})

    assert status == 200
    assert key_store.read("tmdb") == "eyJ.tmdb-secret_key"
    assert json.loads(answer)["settings"]["tmdb"]["key_set"] is True
    assert "tmdb-secret" not in answer
    stored = [path for path in tmp_path.rglob("*") if path.is_file() and b"tmdb-secret" in path.read_bytes()]
    assert stored == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"service": "plex", "key": ""}, "key_invalid"),
        ({"service": "plex", "key": 'a" -A "b'}, "key_invalid"),
        ({"service": "plex", "key": "two words"}, "key_invalid"),
        ({"service": "sonarr", "key": "abc"}, "service_unknown"),
    ],
)
def test_refused_key_is_not_kept(
        metadata_app: tuple[FastAPI, Path], key_store: KeyStore, body: dict[str, Any], code: str,
) -> None:
    app, _runtime_settings_path = metadata_app

    assert _save_key(app, body) == (400, json.dumps({"ok": False, "code": code}, separators=(",", ":")))
    assert key_store.read("plex") is None


def test_second_metadata_save_from_the_same_loaded_settings_is_refused(metadata_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = metadata_app
    first = _body(app)
    second = json.loads(json.dumps(first))
    first["plex"]["enabled"] = False
    second["tmdb"]["enabled"] = True

    assert _save(app, first)[0] == 200
    after_first = runtime_settings_path.read_bytes()

    assert _save(app, second) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == after_first


def test_metadata_facts_read_the_file_as_it_is_now_and_a_save_keeps_it(metadata_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = metadata_app
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["plex"]["base_url"] = "http://plex.elsewhere:32400"
    stored["metadata"]["tmdb"]["refresh_interval_hours"] = 48
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")

    body = _body(app)

    assert body["plex"]["address"] == "http://plex.elsewhere:32400"
    body["tmdb"]["enabled"] = True
    assert _save(app, body)[0] == 200
    saved = json.loads(runtime_settings_path.read_text())["metadata"]
    assert (saved["plex"]["base_url"], saved["tmdb"]["refresh_interval_hours"]) == ("http://plex.elsewhere:32400", 48)



def test_metadata_save_after_the_section_changed_elsewhere_is_refused(metadata_app: tuple[FastAPI, Path]) -> None:
    app, runtime_settings_path = metadata_app
    body = _body(app)
    stored = json.loads(runtime_settings_path.read_text())
    stored["metadata"]["tmdb"]["refresh_interval_hours"] = 48
    runtime_settings_path.write_text(json.dumps(stored), encoding="utf-8")
    changed_elsewhere = runtime_settings_path.read_bytes()
    body["plex"]["enabled"] = False

    assert _save(app, body) == (400, {"ok": False, "code": "settings_changed"})
    assert runtime_settings_path.read_bytes() == changed_elsewhere


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "plex-update.example:8777", "content-type": "application/json"},
        {"host": "127.0.0.1:9999", "content-type": "application/json"},
        {**APP_HEADERS, "origin": "http://plex-update.example:8777"},
        {**APP_HEADERS, "origin": "null"},
    ],
)
def test_a_web_page_cannot_set_a_key_move_where_it_goes_or_make_one_be_sent(
        metadata_app: tuple[FastAPI, Path], key_store: KeyStore, headers: dict[str, str],
) -> None:
    app, runtime_settings_path = metadata_app
    before = runtime_settings_path.read_bytes()
    key_store.write("plex", "plex-key")
    body = _body(app)
    body["plex"]["address"] = "http://plex-update.example:32400"

    with patch("mediaforce.web.app.metadata_connection_states") as check:
        assert _connections(app, headers).status_code == 403
    assert _save(app, body, headers)[0] == 403
    assert _save_key(app, {"service": "plex", "key": "page-key"}, headers)[0] == 403

    check.assert_not_called()
    assert runtime_settings_path.read_bytes() == before
    assert key_store.read("plex") == "plex-key"


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8777", "http://localhost:8777"])
def test_the_mac_app_and_this_servers_own_pages_reach_the_key_routes(
        metadata_app: tuple[FastAPI, Path], origin: str | None,
) -> None:
    app, _runtime_settings_path = metadata_app
    headers = APP_HEADERS if origin is None else {**APP_HEADERS, "origin": origin}

    with patch("mediaforce.web.app.metadata_connection_states", return_value={"plex": "missing", "tmdb": "missing"}):
        response = _connections(app, headers)

    assert (response.status_code, json.loads(response.body)) == (200, {"plex": "missing", "tmdb": "missing"})
    assert _save_key(app, {"service": "tmdb", "key": "abc"}, headers)[0] == 200
