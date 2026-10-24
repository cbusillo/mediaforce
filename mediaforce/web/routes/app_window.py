from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def register_app_window_routes(app: FastAPI, *, main_window_payload: Callable[[], dict[str, Any]]) -> None:
    @app.get("/api/app/main-window")
    def api_app_main_window() -> JSONResponse:
        return JSONResponse(main_window_payload())
