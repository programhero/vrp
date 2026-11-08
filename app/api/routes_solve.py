"""同步求解接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_solver_service, require_api_key
from app.schemas.request import SolveRequest
from app.schemas.response import ErrorEnvelope, SolveResponse
from app.services.solver_service import SolverService

router = APIRouter(
    prefix="/api/v1",
    tags=["routing"],
    dependencies=[Depends(require_api_key)],
)

_ERROR_RESPONSES = {
    422: {"model": ErrorEnvelope, "description": "Invalid problem or request"},
    502: {"model": ErrorEnvelope, "description": "Matrix provider error"},
    503: {"model": ErrorEnvelope, "description": "Matrix provider not configured"},
}


@router.post(
    "/solve",
    response_model=SolveResponse,
    summary="Solve a vehicle routing problem",
    responses=_ERROR_RESPONSES,
)
def solve(
    body: SolveRequest,
    service: SolverService = Depends(get_solver_service),
) -> SolveResponse:
    """在 ``solver.time_limit_seconds`` 内使用 OR-Tools 构建并求解实例。"""
    return service.solve(body)
