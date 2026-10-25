from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from mediaforce.web.routes.folders import request_body
from mediaforce.web.runtime.app_item import PlayerUnavailable


def register_app_window_routes(
        app: FastAPI,
        *,
        main_window_payload: Callable[[], dict[str, Any]],
        tv_shows_payload: Callable[[], dict[str, Any]],
        movies_payload: Callable[[], dict[str, Any]],
        other_media_payload: Callable[[], dict[str, Any]],
        sample_payload: Callable[[str], dict[str, Any] | None],
        approve_sample_action: Callable[[str, Collection[str], str], dict[str, Any]],
        make_sample_action: Callable[[str, str | None], dict[str, Any]],
        compress_season_action: Callable[[str], dict[str, Any]],
        item_payload: Callable[[str], dict[str, Any] | None] | None = None,
        player_file: Callable[[int, str], Path | None] | None = None,
) -> None:
    @app.get("/api/app/main-window")
    def api_app_main_window() -> JSONResponse:
        return JSONResponse(main_window_payload())

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

    if player_file is not None:
        playable = player_file

        @app.get("/api/app/player/{library_item_id}/{side}")
        def api_app_player(library_item_id: int, side: str) -> Response:
            try:
                path = playable(library_item_id, side)
            except PlayerUnavailable as unavailable:
                return JSONResponse({"code": unavailable.code}, status_code=503)
            if path is None:
                return JSONResponse({}, status_code=404)
            return FileResponse(path, media_type="video/mp4")

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
        result = await run_in_threadpool(make_sample_action, prefix.strip("/"), size if isinstance(size, str) else None)
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)
