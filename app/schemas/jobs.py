"""异步求解任务的响应模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from app.schemas.response import ErrorDetail, SolveResponse


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class JobResponse(BaseModel):
    job_id: str
    status: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    solve_time_seconds: float | None = None
    result: SolveResponse | None = None
    error: ErrorDetail | None = None
