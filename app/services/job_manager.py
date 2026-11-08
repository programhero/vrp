"""长耗时求解的内存异步任务执行。"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.errors import JobCapacityError, NotFoundError, VRPError
from app.schemas.jobs import JobResponse, JobState
from app.schemas.request import SolveRequest
from app.schemas.response import ErrorDetail, SolveResponse
from app.services.solver_service import SolverService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Job:
    job_id: str
    status: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    solve_time_seconds: float | None = None
    result: SolveResponse | None = field(default=None)
    error: ErrorDetail | None = field(default=None)


class JobManager:
    """线程安全、有容量上限的内存任务注册表。

    进程退出后任务会丢失。已完成任务在 ``job_ttl_seconds`` 后过期，并通过
    淘汰机制保证最多保留 ``max_jobs`` 条记录。
    """

    def __init__(self, solver: SolverService, settings: Settings) -> None:
        self._solver = solver
        self._settings = settings
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.job_workers,
            thread_name_prefix="vrp-job",
        )

    def submit(self, request: SolveRequest) -> JobResponse:
        now = datetime.now(timezone.utc)
        job_id = uuid.uuid4().hex
        with self._lock:
            self._prune_locked(now)
            self._make_room_locked()
            job = _Job(job_id=job_id, status=JobState.queued, created_at=now)
            self._jobs[job_id] = job
            response = _to_response(job)
            try:
                self._executor.submit(self._execute, job_id, request)
            except RuntimeError as exc:
                self._jobs.pop(job_id, None)
                raise VRPError(
                    "the job executor is shutting down",
                    status_code=503,
                    code="job_executor_unavailable",
                ) from exc
        return response

    def get(self, job_id: str) -> JobResponse:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"job '{job_id}' was not found")
            return _to_response(job)

    def list(self, limit: int = 50) -> list[JobResponse]:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            jobs = list(self._jobs.values())[-limit:]
            jobs.reverse()
            return [_to_response(job) for job in jobs]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _execute(self, job_id: str, request: SolveRequest) -> None:
        started = datetime.now(timezone.utc)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JobState.running
            job.started_at = started

        try:
            result = self._solver.solve(request)
        except VRPError as exc:
            self._finish_failed(job_id, started, exc.as_dict())
            logger.warning("Job %s failed: %s", job_id, exc.message)
        except Exception:
            logger.exception("Job %s failed with an unexpected error", job_id)
            self._finish_failed(
                job_id,
                started,
                {"code": "internal_error", "message": "Internal solver error"},
            )
        else:
            finished = datetime.now(timezone.utc)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.status = JobState.succeeded
                job.finished_at = finished
                job.solve_time_seconds = result.solve_time_seconds
                job.result = result

    def _finish_failed(self, job_id: str, started: datetime, error: dict) -> None:
        finished = datetime.now(timezone.utc)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JobState.failed
            job.finished_at = finished
            job.solve_time_seconds = round((finished - started).total_seconds(), 6)
            job.error = ErrorDetail(**error)

    def _prune_locked(self, now: datetime) -> None:
        ttl = self._settings.job_ttl_seconds
        if ttl <= 0:
            expired = [job_id for job_id, job in self._jobs.items() if job.finished_at is not None]
        else:
            cutoff = now - timedelta(seconds=ttl)
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job.finished_at is not None and job.finished_at < cutoff
            ]
        for job_id in expired:
            del self._jobs[job_id]

    def _make_room_locked(self) -> None:
        while len(self._jobs) >= self._settings.max_jobs:
            finished = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.status in (JobState.succeeded, JobState.failed)
                ),
                None,
            )
            if finished is None:
                raise JobCapacityError("the job queue is full; retry after running jobs complete")
            del self._jobs[finished]


def _to_response(job: _Job) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        solve_time_seconds=job.solve_time_seconds,
        result=job.result,
        error=job.error,
    )
