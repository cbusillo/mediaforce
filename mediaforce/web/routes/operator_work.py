from collections.abc import Callable, Sequence
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mediaforce.library.evidence_queue import EvidenceQueueConflict

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


def register_operator_work_routes(
        app: FastAPI,
        *,
        operator_work_payload: Callable[..., dict[str, Any]],
        pause_background_work_action: Callable[[], dict[str, Any]],
        resume_background_work_action: Callable[[], dict[str, Any]],
        refresh_catalog_action: Callable[[], dict[str, Any]],
        prepare_evidence_action: Callable[[str, Sequence[str] | None, int], dict[str, Any]],
        pause_evidence_action: Callable[[], dict[str, Any]],
        resume_evidence_action: Callable[[], dict[str, Any]],
        cancel_evidence_action: Callable[[], dict[str, Any]],
) -> None:
    @app.get("/api/operator-work")
    def api_operator_work(
            offset: Offset = 0,
            limit: Limit = 25,
            evidence_kind: str | None = None,
            state: str | None = None,
            media_root: str | None = None,
            work_status: str | None = None,
    ) -> JSONResponse:
        try:
            payload = operator_work_payload(
                backlog_offset=offset,
                backlog_limit=limit,
                evidence_kind=evidence_kind,
                evidence_state=state,
                media_root=media_root,
                work_status=work_status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.post("/api/background-work/pause")
    async def api_pause_background_work() -> JSONResponse:
        return JSONResponse(await _run_action(pause_background_work_action))

    @app.post("/api/background-work/resume")
    async def api_resume_background_work() -> JSONResponse:
        return JSONResponse(await _run_action(resume_background_work_action))

    @app.post("/api/catalog/refresh")
    async def api_refresh_catalog() -> JSONResponse:
        return JSONResponse(await _run_action(refresh_catalog_action))

    @app.post("/api/evidence-work/prepare")
    async def api_prepare_evidence(request: Request) -> JSONResponse:
        body = await request.json()
        raw_kinds = body.get("evidence_kinds")
        if raw_kinds is not None and not isinstance(raw_kinds, list):
            raise HTTPException(status_code=400, detail="Evidence kinds must be a list.")
        evidence_kinds = [str(kind) for kind in raw_kinds] if isinstance(raw_kinds, list) else None
        try:
            limit = int(body.get("limit", 25))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Evidence work limit must be a number.") from exc
        return JSONResponse(
            await _run_action(
                prepare_evidence_action,
                str(body.get("prefix") or ""),
                evidence_kinds,
                limit,
            )
        )

    @app.post("/api/evidence-work/pause")
    async def api_pause_evidence() -> JSONResponse:
        return JSONResponse(await _run_action(pause_evidence_action))

    @app.post("/api/evidence-work/resume")
    async def api_resume_evidence() -> JSONResponse:
        return JSONResponse(await _run_action(resume_evidence_action))

    @app.post("/api/evidence-work/cancel")
    async def api_cancel_evidence() -> JSONResponse:
        return JSONResponse(await _run_action(cancel_evidence_action))


async def _run_action(action: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        return await run_in_threadpool(action, *args)
    except EvidenceQueueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
