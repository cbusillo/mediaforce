from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_queue_routes(
        app: FastAPI,
        *,
        pause_encode_queue_action: Callable[[], dict[str, Any]],
        resume_encode_queue_action: Callable[[], dict[str, Any]],
        retry_failed_encode_queue_action: Callable[[], dict[str, Any]],
        retry_failed_encode_prefix_action: Callable[[str], dict[str, Any]],
        stop_encode_queue_action: Callable[[], dict[str, Any]],
        stop_calibration_queue_action: Callable[[], dict[str, Any]],
) -> None:
    @app.post("/api/encode-queue/pause")
    def api_pause_encode_queue() -> JSONResponse:
        return JSONResponse(pause_encode_queue_action())

    @app.post("/api/encode-queue/resume")
    def api_resume_encode_queue() -> JSONResponse:
        return JSONResponse(resume_encode_queue_action())

    @app.post("/api/encode-queue/retry-failed")
    def api_retry_failed_encode_queue() -> JSONResponse:
        return JSONResponse(retry_failed_encode_queue_action())

    @app.post("/api/encode-queue/retry-prefix")
    async def api_retry_failed_encode_prefix(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(retry_failed_encode_prefix_action(str(body.get("prefix", "")).strip()))

    @app.post("/api/encode-queue/stop")
    def api_stop_encode_queue() -> JSONResponse:
        return JSONResponse(stop_encode_queue_action())

    @app.post("/api/calibration-queue/stop")
    def api_stop_calibration_queue() -> JSONResponse:
        return JSONResponse(stop_calibration_queue_action())
