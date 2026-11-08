"""FastAPI 应用工厂与 ASGI 入口。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import routes_health, routes_jobs, routes_solve
from app.config import Settings, get_settings
from app.core.errors import VRPError
from app.core.logging import configure_logging
from app.services.job_manager import JobManager
from app.services.solver_service import SolverService

logger = logging.getLogger(__name__)

_DESCRIPTION = """
A general-purpose Vehicle Routing Problem service built on Google OR-Tools.

Supported features:
* heterogeneous fleets, multi-dimensional capacities and multiple depots
* time windows, service times, vehicle shifts and maximum route duration/distance
* mandatory pickup-and-delivery shipments
* optional stops with drop penalties
* skills / allowed-vehicle restrictions
* haversine, euclidean, precomputed and OSRM distance/duration matrices

Units are meters, seconds and arbitrary capacity units. See `/docs` for the
full request and response schemas.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = time.monotonic()
        app.state.settings = settings
        solver = SolverService(settings)
        jobs = JobManager(solver, settings)
        app.state.solver_service = solver
        app.state.job_manager = jobs
        logger.info(
            "%s %s started (locations<=%d, vehicles<=%d, job workers=%d)",
            settings.app_name,
            settings.version,
            settings.max_locations,
            settings.max_vehicles,
            settings.job_workers,
        )
        try:
            yield
        finally:
            jobs.shutdown()
            logger.info("%s stopped", settings.app_name)

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "routing", "description": "Synchronous VRP solving"},
            {"name": "jobs", "description": "Asynchronous solve jobs"},
            {"name": "health", "description": "Health and version information"},
        ],
    )
    app.state.settings = settings

    @app.exception_handler(VRPError)
    async def vrp_error_handler(_request: Request, exc: VRPError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error for %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error",
                }
            },
        )

    app.include_router(routes_health.router)
    app.include_router(routes_solve.router)
    app.include_router(routes_jobs.router)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        log_level=_settings.log_level.lower(),
    )
