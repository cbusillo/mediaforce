from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SETTINGS_SAVE_ERROR_MESSAGE = (
    "Settings could not be saved. Review the submitted values and try again."
)


def register_settings_routes(
        app: FastAPI,
        *,
        settings_payload: Callable[[bool], dict[str, Any]],
        save_settings_action: Callable[..., dict[str, Any]],
        archive_cleanup_payload: Callable[[str | None], dict[str, Any]],
        clear_archive_cleanup_action: Callable[[str | None], dict[str, Any]],
) -> None:
    @app.get("/api/settings")
    def api_settings(include_archive_cleanup: int = 1) -> JSONResponse:
        return JSONResponse(settings_payload(bool(include_archive_cleanup)))

    @app.get("/api/archive-cleanup")
    def api_archive_cleanup(transcode_root: str | None = None) -> JSONResponse:
        return JSONResponse(archive_cleanup_payload((transcode_root or "").strip() or None))

    @app.post("/api/settings")
    async def api_settings_save(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            result = save_settings_action(
                libraries=[dict(item) for item in body.get("libraries", [])],
                remote_hosts=[dict(item) for item in body.get("remote_hosts", [])],
                transcode_root=str(body.get("transcode_root", "")).strip(),
                video_defaults=dict(body.get("video_defaults", {})),
                encode_queue_scheduler=dict(body.get("encode_queue_scheduler", {})),
                schedule_profiles=[dict(item) for item in body.get("schedule_profiles", [])],
                metadata=(dict(body["metadata"]) if isinstance(body.get("metadata"), dict) else None),
            )
        except ValueError:
            return JSONResponse({"ok": False, "message": SETTINGS_SAVE_ERROR_MESSAGE}, status_code=400)
        return JSONResponse(result)

    @app.post("/api/archive-cleanup/clear")
    async def api_archive_cleanup_clear(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(clear_archive_cleanup_action(str(body.get("transcode_root", "")).strip() or None))
