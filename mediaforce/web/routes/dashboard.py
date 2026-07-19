from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

PreviewLimit = Annotated[int | None, Query(ge=0)]


def register_dashboard_routes(
        app: FastAPI,
        *,
        dashboard_payload: Callable[[int | None], dict[str, Any]],
        dashboard_scan_job_payload: Callable[[], dict[str, Any] | None],
        dashboard_folders_payload: Callable[[bool], dict[str, Any]],
        dashboard_library_payload: Callable[[], dict[str, Any]],
        dashboard_library_details_payload: Callable[[], dict[str, Any]],
        dashboard_movie_library_payload: Callable[[], dict[str, Any]],
        dashboard_movie_library_details_payload: Callable[[], dict[str, Any]],
        dashboard_other_library_payload: Callable[[], dict[str, Any]],
        dashboard_other_library_details_payload: Callable[[], dict[str, Any]],
) -> None:
    @app.get("/api/dashboard")
    def api_dashboard(preview_limit: PreviewLimit = None) -> JSONResponse:
        return JSONResponse(dashboard_payload(preview_limit))

    @app.get("/api/dashboard/scan-job")
    def api_dashboard_scan_job() -> JSONResponse:
        return JSONResponse(dashboard_scan_job_payload())

    @app.get("/api/dashboard/folders")
    def api_dashboard_folders(include_series: int = 1) -> JSONResponse:
        return JSONResponse(dashboard_folders_payload(bool(include_series)))

    @app.get("/api/dashboard/library")
    def api_dashboard_library() -> JSONResponse:
        return JSONResponse(dashboard_library_payload())

    @app.get("/api/dashboard/library/details")
    def api_dashboard_library_details() -> JSONResponse:
        return JSONResponse(dashboard_library_details_payload())

    @app.get("/api/dashboard/library/movies")
    def api_dashboard_movie_library() -> JSONResponse:
        return JSONResponse(dashboard_movie_library_payload())

    @app.get("/api/dashboard/library/movies/details")
    def api_dashboard_movie_library_details() -> JSONResponse:
        return JSONResponse(dashboard_movie_library_details_payload())

    @app.get("/api/dashboard/library/other")
    def api_dashboard_other_library() -> JSONResponse:
        return JSONResponse(dashboard_other_library_payload())

    @app.get("/api/dashboard/library/other/details")
    def api_dashboard_other_library_details() -> JSONResponse:
        return JSONResponse(dashboard_other_library_details_payload())
