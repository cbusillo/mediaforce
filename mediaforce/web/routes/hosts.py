from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_host_routes(
        app: FastAPI,
        *,
        hosts_payload: Callable[[int], dict[str, Any]],
        start_host_action: Callable[[str], dict[str, Any]],
        prepare_host_action: Callable[[str, str | None], dict[str, Any]],
        reset_host_trust_action: Callable[[str], dict[str, Any]],
) -> None:
    @app.post("/api/hosts/start")
    async def api_host_start(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(start_host_action(str(body.get("host_key", "")).strip()))

    @app.post("/api/hosts/prepare")
    async def api_host_prepare(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(
            prepare_host_action(
                str(body.get("host_key", "")).strip(),
                str(body.get("remote_password", "")).strip() or None,
            )
        )

    @app.post("/api/hosts/reset-trust")
    async def api_host_reset_trust(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(reset_host_trust_action(str(body.get("host_key", "")).strip()))

    @app.get("/api/hosts")
    def api_hosts(compact: int = 0) -> JSONResponse:
        return JSONResponse(hosts_payload(compact))
