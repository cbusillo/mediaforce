from collections.abc import Callable
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_completed_routes(
        app: FastAPI,
        *,
        completed_payload: Callable[[], dict[str, Any]],
        clear_completed_backups_action: Callable[[list[str] | None], dict[str, Any]],
) -> None:
    @app.get("/api/completed")
    def api_completed() -> JSONResponse:
        return JSONResponse(completed_payload())

    @app.post("/api/completed/backups/clear")
    async def api_completed_backups_clear(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "message": "Completed cleanup requests must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "message": "Completed cleanup requests must be JSON objects."}, status_code=400)
        raw_prefixes = body.get("prefixes")
        if isinstance(body, dict) and "prefixes" in body and raw_prefixes is not None and not isinstance(raw_prefixes, list):
            return JSONResponse({"ok": False, "message": "Completed-folder prefixes must be provided as a list."}, status_code=400)
        prefixes = raw_prefixes if isinstance(raw_prefixes, list) else None
        try:
            return JSONResponse(clear_completed_backups_action(prefixes))
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
