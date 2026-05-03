import asyncio
import json
import unittest
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request

from mediaforce.web.routes.completed import COMPLETED_CLEANUP_ERROR_MESSAGE, register_completed_routes
from mediaforce.web.routes.settings import SETTINGS_SAVE_ERROR_MESSAGE, register_settings_routes


def _json_request(payload: dict[str, Any]) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


def _route_endpoint(app: FastAPI, path: str, method: str) -> Any:
    for route in app.routes:
        methods = getattr(route, "methods", set())
        if getattr(route, "path", None) == path and method.upper() in methods:
            return route.endpoint
    raise AssertionError(f"Route not found: {method.upper()} {path}")


class WebRouteSecurityTests(unittest.TestCase):
    def test_settings_save_hides_internal_validation_detail(self) -> None:
        app = FastAPI()

        def save_settings_action(**_kwargs: Any) -> dict[str, Any]:
            raise ValueError("internal filesystem path /Volumes/media/private leaked")

        register_settings_routes(
            app,
            settings_payload=lambda _include_archive_cleanup: {},
            save_settings_action=save_settings_action,
            archive_cleanup_payload=lambda _transcode_root: {},
            clear_archive_cleanup_action=lambda _transcode_root: {},
        )

        response = asyncio.run(_route_endpoint(app, "/api/settings", "POST")(_json_request({})))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"ok": False, "message": SETTINGS_SAVE_ERROR_MESSAGE})
        self.assertNotIn("/Volumes/media/private", response.body.decode())

    def test_completed_cleanup_hides_internal_validation_detail(self) -> None:
        app = FastAPI()

        def clear_completed_backups_action(_prefixes: list[str] | None) -> dict[str, Any]:
            raise ValueError("internal archive path /Volumes/media/transcode/.archive leaked")

        register_completed_routes(
            app,
            completed_payload=lambda: {},
            clear_completed_backups_action=clear_completed_backups_action,
        )

        response = asyncio.run(
            _route_endpoint(app, "/api/completed/backups/clear", "POST")(
                _json_request({"prefixes": ["tv/show"]})
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"ok": False, "message": COMPLETED_CLEANUP_ERROR_MESSAGE})
        self.assertNotIn("/Volumes/media/transcode/.archive", response.body.decode())


if __name__ == "__main__":
    unittest.main()
