"""异步求解任务接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_job_manager, require_api_key
from app.schemas.jobs import JobResponse
from app.schemas.request import SolveRequest
from app.schemas.response import ErrorEnvelope
from app.services.job_manager import JobManager

router = APIRouter(
    prefix="/api/v1",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)

_ERROR_RESPONSES = {
    404: {"model": ErrorEnvelope, "description": "Job not found"},
    422: {"model": ErrorEnvelope, "description": "Invalid problem or request"},
    429: {"model": ErrorEnvelope, "description": "Job capacity reached"},
    502: {"model": ErrorEnvelope, "description": "Matrix provider error"},
    503: {"model": ErrorEnvelope, "description": "Service unavailable"},
}


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a solve job",
    responses=_ERROR_RESPONSES,
)
def create_job(
    body: SolveRequest,
    manager: JobManager = Depends(get_job_manager),
) -> JobResponse:
    """提交求解任务并立即返回。通过 ``GET /api/v1/jobs/{job_id}`` 轮询结果。"""
    return manager.submit(body)


@router.get(
    "/jobs",
    response_model=list[JobResponse],
    summary="List recent solve jobs",
)
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    manager: JobManager = Depends(get_job_manager),
) -> list[JobResponse]:
    return manager.list(limit=limit)


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get a solve job",
    responses={404: _ERROR_RESPONSES[404]},
)
def get_job(
    job_id: str,
    manager: JobManager = Depends(get_job_manager),
) -> JobResponse:
    return manager.get(job_id)
