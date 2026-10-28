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
) -> None:

    @app.get("/api/dashboard")
    def api_dashboard(preview_limit: PreviewLimit = None) -> JSONResponse:
        return JSONResponse(dashboard_payload(preview_limit))

    @app.get("/api/dashboard/scan-job")
    def api_dashboard_scan_job() -> JSONResponse:
        return JSONResponse(dashboard_scan_job_payload())
