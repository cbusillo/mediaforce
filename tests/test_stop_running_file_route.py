import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from mediaforce.web.routes.queues import STOP_RUNNING_FILE_PATH, register_queue_routes
from test_app_settings_library import APP_HEADERS, _endpoint, _json_request


def _stop_route(action: Mock) -> Callable[..., Any]:
    app = FastAPI()
    register_queue_routes(app, pause_encode_queue_action=Mock(), resume_encode_queue_action=Mock(),
                          stop_running_file_action=action)
    return _endpoint(app, "POST", STOP_RUNNING_FILE_PATH)


@pytest.mark.parametrize("answer, status", [({"ok": True}, 200), ({"ok": False, "code": "not_running"}, 409)])
def test_the_mac_app_stops_the_file_it_names(answer: dict[str, object], status: int) -> None:
    action = Mock(return_value=answer)

    response = asyncio.run(_stop_route(action)("job-7", _json_request({"run": "2026-11-18T19:00:00+00:00"})))

    action.assert_called_once_with("job-7", "2026-11-18T19:00:00+00:00")
    assert (response.status_code, json.loads(response.body)) == (status, answer)


@pytest.mark.parametrize("headers", [
    {"host": "mediaforce.example:8777"},
    {**APP_HEADERS, "origin": "http://mediaforce.example:8777"},
])
def test_a_web_page_cannot_stop_a_file(headers: dict[str, str]) -> None:
    action = Mock(return_value={"ok": True})

    response = asyncio.run(_stop_route(action)("job-7", _json_request({"run": "x"}, headers)))

    action.assert_not_called()
    assert response.status_code == 403


def test_a_stop_that_does_not_say_which_run_it_saw_is_refused() -> None:
    action = Mock(return_value={"ok": True})

    response = asyncio.run(_stop_route(action)("job-7", _json_request({})))

    action.assert_not_called()
    assert response.status_code == 400
