from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.solver_service import SolverService


@pytest.fixture
def settings() -> Settings:
    return Settings(
        job_workers=1,
        max_jobs=20,
        job_ttl_seconds=3600,
        max_time_limit_seconds=60,
    )


@pytest.fixture
def solver(settings: Settings) -> SolverService:
    return SolverService(settings)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
