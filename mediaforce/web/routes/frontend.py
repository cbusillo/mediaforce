from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


def register_frontend_routes(
        app: FastAPI,
        *,
        frontend_build_dir: Path,
        frontend_index_path: Callable[[], Path],
) -> None:
    @app.get("/", include_in_schema=False)
    def frontend_root() -> FileResponse:
        return FileResponse(frontend_index_path())

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_catchall(path: str) -> FileResponse:
        requested = (frontend_build_dir / path).resolve()
        if requested.is_file() and requested.is_relative_to(frontend_build_dir.resolve()):
            return FileResponse(requested)
        return FileResponse(frontend_index_path())
