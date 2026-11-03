from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mediaforce.web.routes.folders import request_body
from mediaforce.web.settings_runtime import SettingsValidationError


def register_settings_routes(
    app: FastAPI,
    *,
    library_type_preview_action: Callable[[str, str], dict[str, Any]],
    archive_cleanup_payload: Callable[[str | None], dict[str, Any]],
    clear_archive_cleanup_action: Callable[[str | None], dict[str, Any]],
) -> None:

    @app.get("/api/archive-cleanup")
    def api_archive_cleanup(transcode_root: str | None = None) -> JSONResponse:
        return JSONResponse(archive_cleanup_payload((transcode_root or "").strip() or None))

    @app.post("/api/settings/library-type-preview")
    async def api_library_type_preview(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            result = library_type_preview_action(
                str(body.get("key", "")).strip(),
                str(body.get("library_type", "")).strip(),
            )
        except ValueError:
            return JSONResponse({"ok": False, "code": "invalid_settings"}, status_code=400)
        return JSONResponse(result)

    @app.post("/api/archive-cleanup/clear")
    async def api_archive_cleanup_clear(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(clear_archive_cleanup_action(str(body.get("transcode_root", "")).strip() or None))


def register_library_settings_routes(
    app: FastAPI,
    *,
    library_settings_facts: Callable[[], dict[str, Any]],
    save_library_settings_action: Callable[..., dict[str, Any]],
) -> None:
    """The Mac app's Libraries settings tab. Refusals carry a code the app words; never a sentence."""

    @app.get("/api/app/settings/library")
    def api_app_library_settings() -> JSONResponse:
        try:
            return JSONResponse(library_settings_facts())
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=409)

    @app.post("/api/app/settings/library")
    async def api_app_library_settings_save(request: Request) -> JSONResponse:
        body = await request_body(request)
        libraries = body.get("libraries")
        try:
            result = await run_in_threadpool(
                save_library_settings_action,
                libraries=[dict(item) for item in libraries if isinstance(item, dict)] if isinstance(libraries, list) else [],
                working_folder=str(body.get("working_folder") or "").strip(),
                catalog_refresh_hours=body.get("catalog_refresh_hours"),
                revision=str(body.get("revision") or ""),
            )
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=400)
        except ValueError:
            return JSONResponse({"ok": False, "code": "invalid_settings"}, status_code=400)
        return JSONResponse(result)


def register_computer_settings_routes(
    app: FastAPI,
    *,
    computer_settings_facts: Callable[[], dict[str, Any]],
    save_computer_settings_action: Callable[..., dict[str, Any]],
) -> None:
    """The Mac app's Computers settings tab. Refusals carry a code the app words; never a sentence."""

    @app.get("/api/app/settings/computers")
    def api_app_computer_settings() -> JSONResponse:
        try:
            return JSONResponse(computer_settings_facts())
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=409)

    @app.post("/api/app/settings/computers")
    async def api_app_computer_settings_save(request: Request) -> JSONResponse:
        body = await request_body(request)

        def _rows(name: str) -> list[dict[str, Any]]:
            value = body.get(name)
            return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

        try:
            result = await run_in_threadpool(
                save_computer_settings_action,
                computers=_rows("computers"),
                schedules=_rows("schedules"),
                revision=str(body.get("revision") or ""),
            )
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=400)
        except ValueError:
            return JSONResponse({"ok": False, "code": "invalid_settings"}, status_code=400)
        return JSONResponse(result)


def register_metadata_settings_routes(
    app: FastAPI,
    *,
    metadata_settings_facts: Callable[[], dict[str, Any]],
    save_metadata_settings_action: Callable[..., dict[str, Any]],
) -> None:
    """The Mac app's Plex and TMDB settings tab. Refusals carry a code the app words; never a sentence."""

    @app.get("/api/app/settings/metadata")
    def api_app_metadata_settings() -> JSONResponse:
        try:
            return JSONResponse(metadata_settings_facts())
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=409)

    @app.post("/api/app/settings/metadata")
    async def api_app_metadata_settings_save(request: Request) -> JSONResponse:
        body = await request_body(request)

        def _connection(name: str) -> dict[str, Any]:
            value = body.get(name)
            return dict(value) if isinstance(value, dict) else {}

        try:
            result = await run_in_threadpool(
                save_metadata_settings_action,
                plex=_connection("plex"),
                tmdb=_connection("tmdb"),
                revision=str(body.get("revision") or ""),
            )
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=400)
        except ValueError:
            return JSONResponse({"ok": False, "code": "invalid_settings"}, status_code=400)
        return JSONResponse(result)


def register_compression_settings_routes(
    app: FastAPI,
    *,
    compression_settings_facts: Callable[[], dict[str, Any]],
    save_compression_settings_action: Callable[..., dict[str, Any]],
) -> None:
    """The Mac app's Compression settings tab. Refusals carry a code the app words; never a sentence."""

    @app.get("/api/app/settings/compression")
    def api_app_compression_settings() -> JSONResponse:
        try:
            return JSONResponse(compression_settings_facts())
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=409)

    @app.post("/api/app/settings/compression")
    async def api_app_compression_settings_save(request: Request) -> JSONResponse:
        body = await request_body(request)
        try:
            result = await run_in_threadpool(
                save_compression_settings_action,
                edits={key: value for key, value in body.items() if key != "revision"},
                revision=str(body.get("revision") or ""),
            )
        except SettingsValidationError as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=400)
        except ValueError:
            return JSONResponse({"ok": False, "code": "invalid_settings"}, status_code=400)
        return JSONResponse(result)
