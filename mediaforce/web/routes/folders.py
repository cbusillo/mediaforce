from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse


def register_folder_routes(
        app: FastAPI,
        *,
        folder_status_payload: Callable[[str], dict[str, Any]],
        folder_content_payload: Callable[[str], tuple[dict[str, Any], int]],
        download_review_compare_action: Callable[[str], FileResponse],
        folder_ai_tune_action: Callable[[str, str, str], dict[str, Any]],
        folder_ai_tune_preview_action: Callable[[str, str, str], dict[str, Any]],
        folder_ai_tune_confirm_action: Callable[[str, str], dict[str, Any]],
        clear_folder_tuning_action: Callable[[str], dict[str, Any]],
        queue_folder_encode_action: Callable[[str, str, bool], dict[str, Any]],
        validate_folder_outputs_action: Callable[[str], dict[str, Any]],
        promote_folder_outputs_action: Callable[[str], dict[str, Any]],
        save_profile_action: Callable[[str, bool, str], dict[str, Any]],
) -> None:
    @app.get("/api/folders/{prefix:path}/status")
    def api_folder_status(prefix: str) -> JSONResponse:
        return JSONResponse(folder_status_payload(prefix.strip("/")))

    @app.get("/api/folders/{prefix:path}/review-compare/download")
    def api_folder_review_compare_download(prefix: str) -> FileResponse:
        return download_review_compare_action(prefix.strip("/"))

    @app.get("/api/folders/{prefix:path}")
    def api_folder(prefix: str) -> JSONResponse:
        context, status_code = folder_content_payload(prefix.strip("/"))
        return JSONResponse(context, status_code=status_code)

    @app.post("/api/folders/{prefix:path}/ai-tune")
    async def api_folder_ai_tune(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = folder_ai_tune_action(prefix.strip("/"), str(body.get("note", "")), str(body.get("host_key", "")))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/ai-tune/preview")
    async def api_folder_ai_tune_preview(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = folder_ai_tune_preview_action(
            prefix.strip("/"),
            str(body.get("note", "")),
            str(body.get("host_key", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/ai-tune/confirm")
    async def api_folder_ai_tune_confirm(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = folder_ai_tune_confirm_action(
            prefix.strip("/"),
            str(body.get("proposal_id", "")),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/clear-tuning")
    def api_folder_clear_tuning(prefix: str) -> JSONResponse:
        result = clear_folder_tuning_action(prefix.strip("/"))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/queue-encode")
    async def api_folder_queue_encode(prefix: str, request: Request) -> JSONResponse:
        body = await request.json()
        result = queue_folder_encode_action(
            prefix.strip("/"),
            str(body.get("notes", "")),
            bool(body.get("bypass_schedule", False)),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/validate-outputs")
    def api_folder_validate_outputs(prefix: str) -> JSONResponse:
        result = validate_folder_outputs_action(prefix.strip("/"))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/promote-outputs")
    def api_folder_promote_outputs(prefix: str) -> JSONResponse:
        result = promote_folder_outputs_action(prefix.strip("/"))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/folders/{prefix:path}/save-profile")
    async def api_folder_save_profile(prefix: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse(
            save_profile_action(
                prefix.strip("/"),
                bool(body.get("confirm_high_impact", False)),
                str(body.get("reviewed_draft_hash", "")),
            )
        )
