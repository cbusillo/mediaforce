import asyncio
import json
import unittest
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.routing import Route

from mediaforce.web.routes.folders import register_folder_routes


def _json_request(payload: dict[str, Any]) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")]}, receive)


def _route_endpoint(app: FastAPI, path: str, method: str) -> Any:
    for route in app.routes:
        if isinstance(route, Route) and route.path == path and method.upper() in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"Route not found: {method.upper()} {path}")


class WebRouteSecurityTests(unittest.TestCase):
    def test_remake_route_accepts_a_file_list_but_rejects_bulk_keep_and_invalid_ids(self) -> None:
        app = FastAPI()
        calls: list[tuple[str, object, bool]] = []
        register_folder_routes(
            app,
            save_series_lifecycle_action=lambda *_args: {},
            save_audio_preset_action=lambda *_args: {},
            dismiss_audio_preset_suggestion_action=lambda *_args: {},
            queue_folder_encode_action=lambda *_args: {},
            validate_folder_outputs_action=lambda *_args: {},
            promote_folder_outputs_action=lambda *_args: {},
            decide_size_held_action=lambda prefix, ids, keep: calls.append((prefix, ids, keep)) or {"ok": True},
        )
        endpoint = _route_endpoint(app, "/api/folders/{prefix:path}/size-held-decision", "POST")
        response = asyncio.run(endpoint("/tv/Show/", _json_request({"library_item_ids": [7, 8], "keep": False})))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("tv/Show", [7, 8], False)])
        for payload in (
            {"library_item_ids": [7], "keep": True},
            {"library_item_ids": [], "keep": False},
            {"library_item_ids": [True], "keep": False},
            {"library_item_ids": [0], "keep": False},
            {"library_item_ids": ["7"], "keep": False},
            {"library_item_ids": 7, "keep": False},
            {"library_item_ids": [7], "library_item_id": 8, "keep": False},
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as raised:
                asyncio.run(endpoint("tv/Show", _json_request(payload)))
            self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(len(calls), 1)

    def test_keep_motion_original_routes_take_a_file_list_and_reject_invalid_ids(self) -> None:
        app = FastAPI()
        calls: list[tuple[str, str, object]] = []
        register_folder_routes(
            app,
            save_series_lifecycle_action=lambda *_args: {},
            save_audio_preset_action=lambda *_args: {},
            dismiss_audio_preset_suggestion_action=lambda *_args: {},
            queue_folder_encode_action=lambda *_args: {},
            validate_folder_outputs_action=lambda *_args: {},
            promote_folder_outputs_action=lambda *_args: {},
            keep_motion_held_originals_action=lambda prefix, ids: calls.append(("keep", prefix, ids)) or {"ok": True},
            undo_motion_keep_action=lambda prefix, ids: calls.append(("undo", prefix, ids)) or {"ok": False},
        )
        keep = _route_endpoint(app, "/api/folders/{prefix:path}/keep-motion-original", "POST")
        undo = _route_endpoint(app, "/api/folders/{prefix:path}/undo-keep-motion-original", "POST")
        self.assertEqual(asyncio.run(keep("/tv/Show/", _json_request({"library_item_ids": [7, 8]}))).status_code, 200)
        self.assertEqual(asyncio.run(undo("tv/Show", _json_request({"library_item_ids": [7]}))).status_code, 409)
        self.assertEqual(calls, [("keep", "tv/Show", [7, 8]), ("undo", "tv/Show", [7])])
        for payload in (
            {},
            {"library_item_ids": []},
            {"library_item_ids": [True]},
            {"library_item_ids": [0]},
            {"library_item_ids": ["7"]},
            {"library_item_ids": 7},
        ):
            for endpoint in (keep, undo):
                with self.subTest(payload=payload), self.assertRaises(HTTPException) as raised:
                    asyncio.run(endpoint("tv/Show", _json_request(payload)))
                self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(len(calls), 2)

    def test_put_original_back_route_takes_exactly_one_file(self) -> None:
        app = FastAPI()
        calls: list[tuple[str, int]] = []
        register_folder_routes(
            app,
            save_series_lifecycle_action=lambda *_args: {},
            save_audio_preset_action=lambda *_args: {},
            dismiss_audio_preset_suggestion_action=lambda *_args: {},
            queue_folder_encode_action=lambda *_args: {},
            validate_folder_outputs_action=lambda *_args: {},
            promote_folder_outputs_action=lambda *_args: {},
            put_original_back_action=lambda prefix, item_id: calls.append((prefix, item_id)) or {"ok": item_id == 7},
        )
        put_back = _route_endpoint(app, "/api/folders/{prefix:path}/put-original-back", "POST")
        self.assertEqual(asyncio.run(put_back("/tv/Show/", _json_request({"library_item_ids": [7]}))).status_code, 200)
        self.assertEqual(asyncio.run(put_back("tv/Show", _json_request({"library_item_ids": [8]}))).status_code, 409)
        for payload in ({}, {"library_item_ids": [7, 8]}, {"library_item_ids": [0]}, {"library_item_ids": [True]}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as raised:
                asyncio.run(put_back("tv/Show", _json_request(payload)))
            self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(calls, [("tv/Show", 7), ("tv/Show", 8)])

    def test_retained_lifecycle_routes_forward_scope_membership(self) -> None:
        app = FastAPI()
        captured: list[tuple[str, str]] = []

        def capture(action: str, token: str) -> dict[str, Any]:
            captured.append((action, token))
            return {"ok": True}

        register_folder_routes(
            app,
            save_series_lifecycle_action=lambda *_args: {},
            save_audio_preset_action=lambda *_args: {},
            dismiss_audio_preset_suggestion_action=lambda *_args: {},
            queue_folder_encode_action=lambda _prefix, _notes, _bypass, _override, token: capture("queue", token),
            validate_folder_outputs_action=lambda _prefix, token: capture("validate", token),
            promote_folder_outputs_action=lambda _prefix, token: capture("promote", token),
        )
        for route_action in ("queue-encode", "validate-outputs", "promote-outputs"):
            endpoint = _route_endpoint(app, f"/api/folders/{{prefix:path}}/{route_action}", "POST")
            response = asyncio.run(endpoint("other/Batch", _json_request({"scope_membership_token": "reviewed-scope"})))
            self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [("queue", "reviewed-scope"), ("validate", "reviewed-scope"), ("promote", "reviewed-scope")])
