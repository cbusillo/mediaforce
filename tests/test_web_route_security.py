import asyncio
import json
import threading
import time
import unittest
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request
from starlette.routing import Route

from mediaforce.web.routes.completed import COMPLETED_CLEANUP_ERROR_MESSAGE, register_completed_routes
from mediaforce.web.routes.folders import register_folder_routes
from mediaforce.web.routes.queues import register_queue_routes
from mediaforce.web.routes.settings import SETTINGS_SAVE_ERROR_MESSAGE, register_settings_routes
from mediaforce.web.settings_runtime import SettingsValidationError


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


class WebRouteSecurityTests(unittest.TestCase):
    def test_folder_ai_tune_preview_does_not_block_event_loop(self) -> None:
        app = FastAPI()
        preview_started = threading.Event()
        release_preview = threading.Event()
        captured_intents: list[dict[str, Any] | None] = []
        captured_membership_tokens: list[str] = []

        def slow_preview(
                prefix: str,
                note: str,
                host_key: str,
                operator_intent: dict[str, Any] | None,
                _scope_membership_token: str,
        ) -> dict[str, Any]:
            preview_started.set()
            captured_intents.append(operator_intent)
            captured_membership_tokens.append(_scope_membership_token)
            release_preview.wait(timeout=1.0)
            return {"ok": True, "prefix": prefix, "note": note, "host_key": host_key}

        register_folder_routes(
            app,
            folder_status_payload=lambda _prefix: {},
            folder_content_payload=lambda _prefix: ({}, 200),
            download_review_compare_action=lambda _prefix: None,
            folder_ai_tune_action=slow_preview,
            folder_ai_tune_preview_action=slow_preview,
            folder_ai_tune_confirm_action=lambda _prefix, _proposal_id, _membership_token: {"ok": True},
            clear_folder_tuning_action=lambda _prefix: {},
            save_series_lifecycle_action=lambda _prefix, _mode: {"ok": True},
            approve_measured_encode_recovery_action=lambda _prefix, _membership_token: {},
            queue_folder_encode_action=lambda _prefix, _notes, _bypass_schedule, _override_policy_holds, _membership_token: {},
            queue_older_seasons_encode_action=lambda _prefix, _notes, _bypass, _confirmed, _token: {},
            validate_folder_outputs_action=lambda _prefix, _membership_token: {},
            promote_folder_outputs_action=lambda _prefix, _membership_token: {},
            save_profile_action=lambda _prefix, _profile, _approve, _notes, _membership_token: {},
        )
        endpoint = _route_endpoint(app, "/api/folders/{prefix:path}/ai-tune/preview", "POST")

        async def exercise() -> tuple[float, Any]:
            started_at = time.monotonic()
            task = asyncio.create_task(
                endpoint(
                    "tv/show",
                    _json_request(
                        {
                            "note": "target 225 MB",
                            "host_key": "host-1",
                            "operator_intent": {
                                "schema_version": 1,
                                "size_goal": {"mode": "absolute", "value_mb": 225},
                                "resolution": {"mode": "source"},
                            },
                            "scope_membership_token": "membership-v1",
                        }
                    ),
                )
            )
            await asyncio.sleep(0.05)
            elapsed = time.monotonic() - started_at
            release_preview.set()
            return elapsed, await task

        elapsed, response = asyncio.run(exercise())

        self.assertTrue(preview_started.is_set())
        self.assertLess(elapsed, 0.2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_intents[0]["size_goal"]["mode"], "absolute")
        self.assertEqual(captured_membership_tokens, ["membership-v1"])

    def test_folder_lifecycle_routes_forward_scope_membership_token(self) -> None:
        app = FastAPI()
        captured: list[tuple[str, str]] = []

        def capture(action: str, token: str) -> dict[str, Any]:
            captured.append((action, token))
            return {"ok": True}

        register_folder_routes(
            app,
            folder_status_payload=lambda _prefix: {},
            folder_content_payload=lambda _prefix: ({}, 200),
            download_review_compare_action=lambda _prefix: None,
            folder_ai_tune_action=lambda *_args: {"ok": True},
            folder_ai_tune_preview_action=lambda *_args: {"ok": True},
            folder_ai_tune_confirm_action=lambda *_args: {"ok": True},
            clear_folder_tuning_action=lambda _prefix: {},
            save_series_lifecycle_action=lambda _prefix, _mode: {},
            approve_measured_encode_recovery_action=lambda _prefix, token: capture("recovery", token),
            queue_folder_encode_action=lambda _prefix, _notes, _bypass, _override, token: capture("queue", token),
            queue_older_seasons_encode_action=lambda _prefix, _notes, _bypass, confirmed, token: capture(
                "older-confirmed" if confirmed else "older-unconfirmed",
                token,
            ),
            validate_folder_outputs_action=lambda _prefix, token: capture("validate", token),
            promote_folder_outputs_action=lambda _prefix, token: capture("promote", token),
            save_profile_action=lambda _prefix, _high, _size, _hash, token: capture("save", token),
        )
        request = lambda **extra: _json_request({"scope_membership_token": "membership-v2", **extra})

        async def exercise() -> None:
            await _route_endpoint(app, "/api/folders/{prefix:path}/queue-encode", "POST")(
                "other/Batch", request()
            )
            await _route_endpoint(app, "/api/folders/{prefix:path}/queue-older-seasons", "POST")(
                "tv/Show", request(confirmed=True)
            )
            await _route_endpoint(app, "/api/folders/{prefix:path}/approve-recovery", "POST")(
                "other/Batch", request()
            )
            await _route_endpoint(app, "/api/folders/{prefix:path}/validate-outputs", "POST")(
                "other/Batch", request()
            )
            await _route_endpoint(app, "/api/folders/{prefix:path}/promote-outputs", "POST")(
                "other/Batch", request()
            )
            await _route_endpoint(app, "/api/folders/{prefix:path}/save-profile", "POST")(
                "other/Batch", request()
            )

        asyncio.run(exercise())

        self.assertEqual(
            captured,
            [
                ("queue", "membership-v2"),
                ("older-confirmed", "membership-v2"),
                ("recovery", "membership-v2"),
                ("validate", "membership-v2"),
                ("promote", "membership-v2"),
                ("save", "membership-v2"),
            ],
        )

    def test_folder_save_profile_failure_returns_conflict_status(self) -> None:
        app = FastAPI()
        register_folder_routes(
            app,
            folder_status_payload=lambda _prefix: {},
            folder_content_payload=lambda _prefix: ({}, 200),
            download_review_compare_action=lambda _prefix: None,
            folder_ai_tune_action=lambda *_args: {},
            folder_ai_tune_preview_action=lambda *_args: {},
            folder_ai_tune_confirm_action=lambda *_args: {},
            clear_folder_tuning_action=lambda _prefix: {},
            save_series_lifecycle_action=lambda _prefix, _mode: {},
            approve_measured_encode_recovery_action=lambda _prefix, _membership_token: {},
            queue_folder_encode_action=lambda *_args: {},
            queue_older_seasons_encode_action=lambda *_args: {},
            validate_folder_outputs_action=lambda _prefix, _membership_token: {},
            promote_folder_outputs_action=lambda _prefix, _membership_token: {},
            save_profile_action=lambda *_args: {
                "ok": False,
                "code": "library_not_production",
                "message": "Movies is browse only.",
            },
        )
        endpoint = _route_endpoint(app, "/api/folders/{prefix:path}/save-profile", "POST")

        response = asyncio.run(endpoint("films/Example", _json_request({})))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "library_not_production")

    def test_retry_prefix_forwards_scope_membership_token(self) -> None:
        app = FastAPI()
        captured: list[tuple[str, str]] = []
        register_queue_routes(
            app,
            pause_encode_queue_action=lambda: {},
            resume_encode_queue_action=lambda: {},
            retry_failed_encode_queue_action=lambda: {},
            retry_failed_encode_prefix_action=lambda prefix, token: (
                captured.append((prefix, token)) or {"ok": True}
            ),
            stop_encode_queue_action=lambda: {},
            stop_calibration_queue_action=lambda: {},
        )

        response = asyncio.run(
            _route_endpoint(app, "/api/encode-queue/retry-prefix", "POST")(
                _json_request(
                    {
                        "prefix": "other/Batch",
                        "scope_membership_token": "membership-v3",
                    }
                )
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [("other/Batch", "membership-v3")])

    def test_settings_save_hides_internal_validation_detail(self) -> None:
        app = FastAPI()

        def save_settings_action(**_kwargs: Any) -> dict[str, Any]:
            raise ValueError("internal filesystem path /Volumes/media/private leaked")

        register_settings_routes(
            app,
            settings_payload=lambda _include_archive_cleanup: {},
            save_settings_action=save_settings_action,
            library_type_preview_action=lambda _key, _library_type: {},
            archive_cleanup_payload=lambda _transcode_root: {},
            clear_archive_cleanup_action=lambda _transcode_root: {},
        )

        response = asyncio.run(_route_endpoint(app, "/api/settings", "POST")(_json_request({})))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"ok": False, "message": SETTINGS_SAVE_ERROR_MESSAGE})
        self.assertNotIn("/Volumes/media/private", response.body.decode())

    def test_settings_save_returns_explicit_operator_validation_detail(self) -> None:
        app = FastAPI()

        def save_settings_action(**_kwargs: Any) -> dict[str, Any]:
            raise SettingsValidationError("Library local paths must be absolute.")

        register_settings_routes(
            app,
            settings_payload=lambda _include_archive_cleanup: {},
            save_settings_action=save_settings_action,
            library_type_preview_action=lambda _key, _library_type: {},
            archive_cleanup_payload=lambda _transcode_root: {},
            clear_archive_cleanup_action=lambda _transcode_root: {},
        )

        response = asyncio.run(_route_endpoint(app, "/api/settings", "POST")(_json_request({})))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.body),
            {"ok": False, "message": "Library local paths must be absolute."},
        )

    def test_completed_cleanup_hides_internal_validation_detail(self) -> None:
        app = FastAPI()

        def clear_completed_backups_action(_prefixes: list[str] | None) -> dict[str, Any]:
            raise ValueError("internal archive path /Volumes/media/transcode/.archive leaked")

        register_completed_routes(
            app,
            completed_payload=lambda: {},
            clear_completed_backups_action=clear_completed_backups_action,
            confirm_originals_removed_action=lambda _prefixes: {},
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
