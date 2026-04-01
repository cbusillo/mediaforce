from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def register_dashboard_routes(
        app: FastAPI,
        *,
        dashboard_payload: Callable[[], dict[str, Any]],
        dashboard_folders_payload: Callable[[], dict[str, Any]],
) -> None:
    @app.get("/api/dashboard")
    def api_dashboard() -> JSONResponse:
        return JSONResponse(dashboard_payload())

    @app.get("/api/dashboard/folders")
    def api_dashboard_folders() -> JSONResponse:
        return JSONResponse(dashboard_folders_payload())
