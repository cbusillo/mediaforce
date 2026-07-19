import asyncio
import json
import unittest
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.routing import Route

from mediaforce.library.evidence_queue import EvidenceQueueConflict
from mediaforce.web.routes.operator_work import register_operator_work_routes


class OperatorWorkRouteTests(unittest.TestCase):
    def test_get_forwards_pagination_and_not_prepared_filter(self) -> None:
        captured: dict[str, Any] = {}
        app = self._app(
            operator_work_payload=lambda **kwargs: captured.update(kwargs) or {"ok": True}
        )

        endpoint = _route_endpoint(app, "/api/operator-work", "GET")

        response = endpoint(
            offset=25,
            limit=10,
            evidence_kind=None,
            state=None,
            media_root=None,
            work_status="not_prepared",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["backlog_offset"], 25)
        self.assertEqual(captured["backlog_limit"], 10)
        self.assertEqual(captured["work_status"], "not_prepared")

    def test_prepare_validates_and_forwards_explicit_scope(self) -> None:
        captured: list[tuple[str, list[str] | None, int]] = []
        app = self._app(
            prepare_evidence_action=lambda prefix, kinds, limit: (
                captured.append((prefix, list(kinds) if kinds is not None else None, limit))
                or {"ok": True, "message": "Prepared."}
            )
        )

        endpoint = _route_endpoint(app, "/api/evidence-work/prepare", "POST")
        response = asyncio.run(
            endpoint(
                _json_request(
                    {
                        "prefix": "tv/Show/Season 1",
                        "evidence_kinds": ["cadence_analysis"],
                        "limit": 5,
                    }
                )
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [("tv/Show/Season 1", ["cadence_analysis"], 5)])

    def test_conflicts_return_operator_visible_409(self) -> None:
        def conflict() -> dict[str, Any]:
            raise EvidenceQueueConflict("Prepare a batch first.")

        app = self._app(resume_evidence_action=conflict)

        endpoint = _route_endpoint(app, "/api/evidence-work/resume", "POST")
        with self.assertRaises(HTTPException) as context:
            asyncio.run(endpoint())

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail, "Prepare a batch first.")

    @staticmethod
    def _app(**overrides: Any) -> FastAPI:
        callbacks: dict[str, Any] = {
            "operator_work_payload": lambda **_kwargs: {"ok": True},
            "pause_background_work_action": lambda: {"ok": True, "message": "Paused."},
            "resume_background_work_action": lambda: {"ok": True, "message": "Resumed."},
            "refresh_catalog_action": lambda: {"ok": True, "message": "Queued."},
            "prepare_evidence_action": lambda _prefix, _kinds, _limit: {
                "ok": True,
                "message": "Prepared.",
            },
            "pause_evidence_action": lambda: {"ok": True, "message": "Paused."},
            "resume_evidence_action": lambda: {"ok": True, "message": "Started."},
            "cancel_evidence_action": lambda: {"ok": True, "message": "Cancelled."},
        }
        callbacks.update(overrides)
        app = FastAPI()
        register_operator_work_routes(app, **callbacks)
        return app


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
        if isinstance(route, Route) and route.path == path and method.upper() in route.methods:
            return route.endpoint
    raise AssertionError(f"Route not found: {method.upper()} {path}")


if __name__ == "__main__":
    unittest.main()
