from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from mediaforce.web.routes.folders import request_body
from mediaforce.web.runtime import player_stream


def register_app_window_routes(
        app: FastAPI,
        *,
        main_window_payload: Callable[[], dict[str, Any]],
        tv_shows_payload: Callable[[], dict[str, Any]],
        movies_payload: Callable[[], dict[str, Any]],
        other_media_payload: Callable[[], dict[str, Any]],
        sample_payload: Callable[[str], dict[str, Any] | None],
        approve_sample_action: Callable[[str, Collection[str], str, str], dict[str, Any]],
        make_sample_action: Callable[[str, str | None, str | None], dict[str, Any]],
        compress_season_action: Callable[[str], dict[str, Any]],
        item_payload: Callable[[str], dict[str, Any] | None] | None = None,
        player_source: Callable[[int, str], Path | None] | None = None,
        activity_payload: Callable[[], dict[str, Any]] | None = None,
        computers_payload: Callable[[], dict[str, Any]] | None = None,
        kinds_payload: Callable[[], dict[str, Any]] | None = None,
        approve_kind_action: Callable[[str, str], dict[str, Any]] | None = None,
        stop_kind_action: Callable[[str], dict[str, Any]] | None = None,
) -> None:

    @app.get("/api/app/main-window")
    def api_app_main_window() -> JSONResponse:
        return JSONResponse(main_window_payload())

    if computers_payload is not None:
        read_computers = computers_payload

        @app.get("/api/app/computers")
        def api_app_computers() -> JSONResponse:
            return JSONResponse(read_computers())

    if activity_payload is not None:
        read_activity = activity_payload

        @app.get("/api/app/activity")
        def api_app_activity() -> JSONResponse:
            return JSONResponse(read_activity())

    @app.get("/api/app/tv-shows")
    def api_app_tv_shows() -> JSONResponse:
        return JSONResponse(tv_shows_payload())

    @app.get("/api/app/movies")
    def api_app_movies() -> JSONResponse:
        return JSONResponse(movies_payload())

    @app.get("/api/app/other")
    def api_app_other() -> JSONResponse:
        return JSONResponse(other_media_payload())

    if item_payload is not None:
        item_facts = item_payload

        @app.get("/api/app/items/{prefix:path}")
        def api_app_item(prefix: str) -> JSONResponse:
            facts = item_facts(prefix.strip("/"))
            return JSONResponse(facts or {}, status_code=200 if facts else 404)

    if player_source is not None:
        source_for = player_source

        @app.get("/api/app/player/{library_item_id}/{side}/{name}")
        def api_app_player(library_item_id: int, side: str, name: str) -> Response:
            source = source_for(library_item_id, side)
            if source is None:
                return JSONResponse({}, status_code=404)
            try:
                served = player_stream.serve(source, name)
            except player_stream.SegmentNotFound:
                return JSONResponse({}, status_code=404)
            return Response(served.body, media_type=served.media_type)

    if kinds_payload is not None and approve_kind_action is not None and stop_kind_action is not None:
        kinds_facts, approve_kind, stop_kind = kinds_payload, approve_kind_action, stop_kind_action

        @app.get("/api/app/kinds")
        def api_app_kinds() -> JSONResponse:
            return JSONResponse(kinds_facts())

        @app.post("/api/app/kinds/{kind}/approve")
        async def api_app_kind_approve(kind: str, request: Request) -> JSONResponse:
            body = await request_body(request)
            result = await run_in_threadpool(approve_kind, kind, str(body.get("from_prefix") or "").strip("/"))
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)

        @app.post("/api/app/kinds/{kind}/stop")
        def api_app_kind_stop(kind: str) -> JSONResponse:
            return JSONResponse(stop_kind(kind))

    @app.get("/api/app/samples/{prefix:path}")
    def api_app_sample(prefix: str) -> JSONResponse:
        facts = sample_payload(prefix.strip("/"))
        return JSONResponse(facts or {}, status_code=200 if facts else 404)

    @app.post("/api/app/samples/{prefix:path}/approve")
    async def api_app_sample_approve(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        confirm = body.get("confirm")
        result = await run_in_threadpool(
            approve_sample_action,
            prefix.strip("/"),
            {value for value in confirm if isinstance(value, str)} if isinstance(confirm, list) else set(),
            str(body.get("reviewed_draft_hash") or ""),
            str(body.get("approval_scope") or "season"),
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/app/samples/{prefix:path}/compress")
    async def api_app_sample_compress(prefix: str) -> JSONResponse:
        result = await run_in_threadpool(compress_season_action, prefix.strip("/"))
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/api/app/samples/{prefix:path}/make")
    async def api_app_sample_make(prefix: str, request: Request) -> JSONResponse:
        body = await request_body(request)
        size = body.get("size")
        balance = body.get("balance")
        result = await run_in_threadpool(
            make_sample_action, prefix.strip("/"), size if isinstance(size, str) else None,
            balance if isinstance(balance, str) else None,
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)
