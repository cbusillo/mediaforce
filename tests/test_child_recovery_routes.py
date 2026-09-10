import asyncio
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mediaforce.web.routes.queues import register_child_recovery_routes


def request(body: object) -> Request:
    async def receive() -> dict:
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")]}, receive)


@pytest.mark.parametrize("phase", ["preview", "apply"])
def test_recovery_route_forwards_exact_selection(phase: str) -> None:
    app = FastAPI()
    action = Mock(return_value={"ok": True})
    register_child_recovery_routes(app, recover_children_action=action)
    endpoint = next(route.endpoint for route in app.routes if route.path.endswith(f"recover-children/{phase}"))
    result = asyncio.run(endpoint(request({"parent_job_id": "parent", "child_ids": ["b", "a"], "token": "snapshot"})))
    assert result.status_code == 200
    action.assert_called_once_with("parent", ["b", "a"], "snapshot" if phase == "apply" else None)


@pytest.mark.parametrize("body", [None, [], {}, {"parent_job_id": "p", "child_ids": []},
                                      {"parent_job_id": "p", "child_ids": ["a", "a"]},
                                      {"parent_job_id": "p", "child_ids": [1]},
                                      {"parent_job_id": "p", "child_ids": ["a"]},
                                      {"parent_job_id": "p", "child_ids": ["a"], "token": True}])
def test_apply_route_rejects_invalid_requests_without_action(body: object) -> None:
    app = FastAPI()
    action = Mock()
    register_child_recovery_routes(app, recover_children_action=action)
    endpoint = next(route.endpoint for route in app.routes if route.path.endswith("recover-children/apply"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request(body)))
    assert exc.value.status_code == 400
    action.assert_not_called()


@pytest.mark.parametrize("headers,status", [
    ([(b"content-type", b"text/plain")], 415),
    ([(b"content-type", b"application/json"), (b"origin", b"https://unrelated.example")], 403),
    ([(b"content-type", b"application/json"), (b"sec-fetch-site", b"cross-site")], 403),
])
def test_recovery_rejects_cross_origin_and_simple_form_requests(headers: list[tuple[bytes, bytes]], status: int) -> None:
    app = FastAPI()
    action = Mock()
    register_child_recovery_routes(app, recover_children_action=action)
    endpoint = next(route.endpoint for route in app.routes if route.path.endswith("recover-children/apply"))
    req = request({"parent_job_id": "p", "child_ids": ["a"], "token": "t"})
    req.scope["headers"] = headers
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(req))
    assert exc.value.status_code == status
    action.assert_not_called()


def test_recovery_does_not_trigger_unrelated_artifact_cleanup() -> None:
    from mediaforce.web.app import _request_triggers_periodic_cleanup

    for phase in ("preview", "apply"):
        assert not _request_triggers_periodic_cleanup("POST", f"/api/encode-queue/recover-children/{phase}")
    assert _request_triggers_periodic_cleanup("POST", "/api/settings")
