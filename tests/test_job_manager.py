from __future__ import annotations

import threading
import time

import pytest

from app.config import Settings
from app.core.errors import JobCapacityError
from app.schemas.jobs import JobState
from app.schemas.request import SolveRequest
from app.services.job_manager import JobManager
from app.services.solver_service import SolverService


def _request() -> SolveRequest:
    return SolveRequest.model_validate(
        {
            "depots": [{"id": "depot", "x": 0, "y": 0}],
            "stops": [{"id": "a", "x": 100, "y": 0}],
            "vehicles": [{"id": "v", "start_depot": "depot"}],
            "solver": {"time_limit_seconds": 0.1},
        }
    )


class _BlockingSolver:
    def __init__(self, settings: Settings) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._inner = SolverService(settings)

    def solve(self, request: SolveRequest):
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError("blocking solver was not released")
        return self._inner.solve(request)


def test_job_capacity_and_background_completion():
    settings = Settings(
        job_workers=1,
        max_jobs=1,
        job_ttl_seconds=3600,
        max_time_limit_seconds=10,
    )
    solver = _BlockingSolver(settings)
    manager = JobManager(solver, settings)
    try:
        first = manager.submit(_request())
        assert solver.started.wait(5), "job did not start"

        with pytest.raises(JobCapacityError):
            manager.submit(_request())

        solver.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = manager.get(first.job_id)
            if job.status is JobState.succeeded:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("job did not complete")
        assert job.result is not None
        assert job.result.summary.stops_served == 1
        assert job.solve_time_seconds is not None
    finally:
        solver.release.set()
        manager.shutdown()
