from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _payload(time_limit: float = 0.2) -> dict:
    return {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1000, "y": 0, "demand": 1},
            {"id": "b", "x": 2000, "y": 0, "demand": 1},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot", "capacity": 10}],
        "solver": {"time_limit_seconds": time_limit},
    }


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_health_and_root(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["version"] == "1.0.0"

    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["api"] == "/api/v1"


def test_openapi_schema(client: TestClient):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    assert "/api/v1/solve" in paths
    assert "/api/v1/jobs" in paths
    assert "/api/v1/jobs/{job_id}" in paths
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert any(scheme.get("type") == "apiKey" for scheme in schemes.values())


def test_solve_endpoint(client: TestClient):
    response = client.post("/api/v1/solve", json=_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("feasible", "optimal")
    assert body["summary"]["stops_served"] == 2
    assert body["summary"]["stops_total"] == 2
    assert len(body["routes"]) == 1
    assert body["metadata"]["matrix_provider"] == "euclidean"
    assert body["solve_time_seconds"] >= 0


def test_validation_error_envelope(client: TestClient):
    payload = _payload()
    payload["unexpected"] = True
    response = client.post("/api/v1/solve", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list)

    response = client.post("/api/v1/solve", json={"depots": []})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_domain_error_envelope(client: TestClient):
    payload = _payload()
    payload["stops"][0]["required_skills"] = ["cold"]
    response = client.post("/api/v1/solve", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_problem"


def test_time_limit_over_server_max(client: TestClient):
    payload = _payload(time_limit=61)
    response = client.post("/api/v1/solve", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_problem"
    assert "maximum" in response.json()["error"]["message"]


def test_unknown_route_uses_error_envelope(client: TestClient):
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_job_lifecycle_and_listing(client: TestClient):
    create = client.post("/api/v1/jobs", json=_payload())
    assert create.status_code == 202
    created = create.json()
    assert created["status"] in ("queued", "running", "succeeded")
    assert created["job_id"]

    body = _wait_for_job(client, created["job_id"])
    assert body["status"] == "succeeded"
    assert body["result"]["status"] in ("feasible", "optimal")
    assert body["result"]["summary"]["stops_served"] == 2
    assert body["error"] is None
    assert body["solve_time_seconds"] is not None

    listing = client.get("/api/v1/jobs", params={"limit": 10})
    assert listing.status_code == 200
    assert any(job["job_id"] == created["job_id"] for job in listing.json())

    missing = client.get("/api/v1/jobs/not-a-real-job")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_failed_job_reports_error(client: TestClient):
    create = client.post("/api/v1/jobs", json=_payload(time_limit=61))
    assert create.status_code == 202
    body = _wait_for_job(client, create.json()["job_id"])
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_problem"
    assert body["result"] is None


def test_api_key_authentication():
    settings = Settings(
        api_key="s3cret",
        job_workers=1,
        max_time_limit_seconds=60,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        missing = client.post("/api/v1/solve", json=_payload())
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "unauthorized"

        wrong = client.post(
            "/api/v1/solve",
            json=_payload(),
            headers={"X-API-Key": "wrong"},
        )
        assert wrong.status_code == 401

        authorized = client.post(
            "/api/v1/solve",
            json=_payload(),
            headers={"X-API-Key": "s3cret"},
        )
        assert authorized.status_code == 200

        jobs_without_key = client.get("/api/v1/jobs")
        assert jobs_without_key.status_code == 401
