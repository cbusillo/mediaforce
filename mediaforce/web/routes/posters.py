from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response


def register_poster_routes(app: FastAPI, *, title_poster: Callable[[str], Path | None]) -> None:
    """A show's or movie's poster for the Mac app, by its title prefix ("tv/Show" or "movies/Title (Year)")."""

    @app.get("/api/app/posters/{title_prefix:path}")
    def api_app_title_poster(title_prefix: str) -> Response:
        poster = title_poster(title_prefix)
        if poster is None:
            return JSONResponse({"poster": "none"}, status_code=404)
        return FileResponse(poster, headers={"Cache-Control": "max-age=86400"})
