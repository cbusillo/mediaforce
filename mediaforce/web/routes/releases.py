from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mediaforce.web.runtime.release_work import ReleaseWorkSnapshot


def register_release_routes(app: FastAPI, *, work_snapshot: Callable[[], ReleaseWorkSnapshot]) -> None:
    @app.get("/api/release/work")
    def api_release_work() -> JSONResponse:
        return JSONResponse(work_snapshot(), headers={"Cache-Control": "no-store"})
