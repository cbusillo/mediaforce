from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response


def register_poster_routes(app: FastAPI, *, show_poster: Callable[[str], Path | None]) -> None:
    @app.get("/api/app/posters/{show_prefix:path}")
    def api_app_show_poster(show_prefix: str) -> Response:
        poster = show_poster(show_prefix)
        if poster is None:
            return JSONResponse({"poster": "none"}, status_code=404)
        return FileResponse(poster, headers={"Cache-Control": "max-age=86400"})
