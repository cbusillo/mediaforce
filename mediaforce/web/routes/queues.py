from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


CHILD_RECOVERY_PREVIEW_PATH = "/api/encode-queue/recover-children/preview"
CHILD_RECOVERY_APPLY_PATH = "/api/encode-queue/recover-children/apply"


def register_queue_routes(
        app: FastAPI,
        *,
        pause_encode_queue_action: Callable[[], dict[str, Any]],
        resume_encode_queue_action: Callable[[], dict[str, Any]],
        retry_failed_encode_queue_action: Callable[[], dict[str, Any]],
        retry_failed_encode_prefix_action: Callable[[str, str], dict[str, Any]],
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
        result = await run_in_threadpool(
            retry_failed_encode_prefix_action,
            str(body.get("prefix", "")).strip(),
            str(body.get("scope_membership_token", "")),
        )
        return JSONResponse(result)

    @app.post("/api/encode-queue/stop")
    def api_stop_encode_queue() -> JSONResponse:
        return JSONResponse(stop_encode_queue_action())

    @app.post("/api/calibration-queue/stop")
    def api_stop_calibration_queue() -> JSONResponse:
        return JSONResponse(stop_calibration_queue_action())


def register_child_recovery_routes(
        app: FastAPI,
        *,
        recover_children_action: Callable[[str, list[str], str | None], dict[str, Any]],
) -> None:
    async def dispatch(request: Request, *, apply: bool) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Recovery requires application/json.")
        origin = request.headers.get("origin")
        try:
            parsed_origin = urlsplit(origin) if origin is not None else None
        except ValueError:
            raise HTTPException(status_code=403, detail="Invalid recovery origin.") from None
        if request.headers.get("sec-fetch-site") == "cross-site" or (
                parsed_origin is not None and (
                    parsed_origin.scheme != request.url.scheme
                    or parsed_origin.netloc != request.url.netloc
                )
        ):
            raise HTTPException(status_code=403, detail="Recovery must originate from this controller.")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="A JSON recovery request is required.") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="A recovery request object is required.")
        parent_id = body.get("parent_job_id")
        child_ids = body.get("child_ids")
        if (
                not isinstance(parent_id, str) or not parent_id.strip()
                or not isinstance(child_ids, list) or not 1 <= len(child_ids) <= 100
                or any(not isinstance(value, str) or not value.strip() for value in child_ids)
                or len(set(child_ids)) != len(child_ids)
        ):
            raise HTTPException(status_code=400, detail="Specify one parent and 1–100 unique child IDs.")
        token = body.get("token") if apply else None
        if apply and (not isinstance(token, str) or not token):
            raise HTTPException(status_code=400, detail="Apply requires the preview token.")
        result = await run_in_threadpool(recover_children_action, parent_id, child_ids, token)
        return JSONResponse(result)

    @app.post(CHILD_RECOVERY_PREVIEW_PATH)
    async def preview_child_recovery(request: Request) -> JSONResponse:
        return await dispatch(request, apply=False)

    @app.post(CHILD_RECOVERY_APPLY_PATH)
    async def apply_child_recovery(request: Request) -> JSONResponse:
        return await dispatch(request, apply=True)
