from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

def register_completed_routes(
        app: FastAPI,
        *,
        completed_payload: Callable[[], dict[str, Any]],
) -> None:
    @app.get("/api/completed")
    def api_completed() -> JSONResponse:
        return JSONResponse(completed_payload())
