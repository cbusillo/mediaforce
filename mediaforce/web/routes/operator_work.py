from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mediaforce.library.evidence_queue import EvidenceQueueConflict

def register_operator_work_routes(
        app: FastAPI,
        *,
        refresh_catalog_action: Callable[[], dict[str, Any]],
) -> None:
    @app.post("/api/catalog/refresh")
    async def api_refresh_catalog() -> JSONResponse:
        return JSONResponse(await _run_action(refresh_catalog_action))

async def _run_action(action: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        return await run_in_threadpool(action, *args)
    except EvidenceQueueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
