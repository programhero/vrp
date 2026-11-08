"""共享的 FastAPI 依赖项。"""

from __future__ import annotations

import secrets

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.config import Settings
from app.core.errors import UnauthorizedError
from app.services.job_manager import JobManager
from app.services.solver_service import SolverService

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required only when the server sets VRP_API_KEY.",
)


def require_api_key(request: Request, provided: str | None = Security(_api_key_header)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.api_key:
        return
    if not provided or not secrets.compare_digest(
        provided.encode("utf-8"), settings.api_key.encode("utf-8")
    ):
        raise UnauthorizedError("A valid X-API-Key header is required")


def get_solver_service(request: Request) -> SolverService:
    return request.app.state.solver_service


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager
