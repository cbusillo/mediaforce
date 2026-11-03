from ipaddress import ip_address

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mediaforce.core.config import MediaforceConfig
from mediaforce.library.original_registration import apply_original_preview, list_original_previews
from mediaforce.web.routes.folders import request_body


def register_original_routes(app: FastAPI, config: MediaforceConfig) -> None:
    @app.get("/api/app/originals")
    def originals() -> JSONResponse:
        return JSONResponse({"previews": list_original_previews(config)})

    @app.post("/api/app/originals/apply")
    async def apply_originals(request: Request) -> JSONResponse:
        try:
            if (request.client is None or not ip_address(request.client.host).is_loopback
                    or request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None):
                raise ValueError("Confirm original files in the Mac app")
            body = await request_body(request)
            if set(body) != {"preview_id", "manifest_hash"}:
                raise ValueError("Only the exact Mac approval reference is accepted")
            result = await run_in_threadpool(apply_original_preview, config, str(body["preview_id"]), str(body["manifest_hash"]))
            return JSONResponse(result)
        except (ValueError, OSError) as error:
            return JSONResponse({"ok": False, "reason": str(error)}, status_code=409)
