from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_settings_routes(
        app: FastAPI,
        *,
        settings_payload: Callable[[], dict[str, Any]],
        save_settings_action: Callable[..., dict[str, Any]],
        clear_archive_cleanup_action: Callable[[str | None], dict[str, Any]],
) -> None:
    @app.get("/api/settings")
    def api_settings() -> JSONResponse:
        return JSONResponse(settings_payload())

    @app.post("/api/settings")
    async def api_settings_save(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            result = save_settings_action(
                libraries=[dict(item) for item in body.get("libraries", [])],
                remote_hosts=[dict(item) for item in body.get("remote_hosts", [])],
                transcode_root=str(body.get("transcode_root", "")).strip(),
                encode_queue_scheduler=dict(body.get("encode_queue_scheduler", {})),
                schedule_profiles=[dict(item) for item in body.get("schedule_profiles", [])],
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse(result)

    @app.post("/api/archive-cleanup/clear")
    async def api_archive_cleanup_clear(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(clear_archive_cleanup_action(str(body.get("transcode_root", "")).strip() or None))
