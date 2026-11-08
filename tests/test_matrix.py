from __future__ import annotations

import pytest

from app.config import Settings
from app.core.errors import InvalidProblemError, MatrixError, MatrixProviderError
from app.schemas.request import SolveRequest
from app.vrp import matrix as matrix_module
from app.vrp.matrix import build_matrices, fetch_osrm_table, haversine_m


def _request(**overrides) -> SolveRequest:
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [{"id": "a", "x": 1000, "y": 0}],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
    }
    payload.update(overrides)
    return SolveRequest.model_validate(payload)


def test_haversine_distance():
    distance = haversine_m((0.0, 0.0), (0.0, 1.0))
    assert 111_000 < distance < 111_300


def test_auto_euclidean_matrix_and_duration():
    matrices = build_matrices(_request(), Settings())
    assert matrices.provider == "euclidean"
    assert matrices.distances == [[0, 1000], [1000, 0]]
    assert matrices.durations == [[0, 100], [100, 0]]


def test_auto_haversine_matrix():
    request = _request(
        depots=[{"id": "depot", "lat": 31.23, "lon": 121.47}],
        stops=[{"id": "a", "lat": 31.24, "lon": 121.47}],
    )
    matrices = build_matrices(request, Settings())
    assert matrices.provider == "haversine"
    assert matrices.distances[0][1] > 1000
    assert matrices.distances[0][1] == matrices.distances[1][0]
    assert matrices.durations[0][1] == round(matrices.distances[0][1] / 10)


def test_precomputed_asymmetric_matrix():
    request = _request(
        matrix={
            "distances": [[0, 10], [20, 0]],
            "durations": [[0, 5], [7, 0]],
        },
        solver={"time_limit_seconds": 0.2},
    )
    matrices = build_matrices(request, Settings())
    assert matrices.provider == "precomputed"
    assert matrices.distances == [[0, 10], [20, 0]]
    assert matrices.durations == [[0, 5], [7, 0]]


def test_precomputed_durations_fall_back_to_speed():
    request = _request(
        matrix={
            "distances": [[0, 100], [100, 0]],
            "speed_mps": 20,
        }
    )
    matrices = build_matrices(request, Settings())
    assert matrices.durations == [[0, 5], [5, 0]]


def test_precomputed_requires_square_matrix():
    request = _request(matrix={"distances": [[0]]})
    with pytest.raises(MatrixError, match="must have 2 rows"):
        build_matrices(request, Settings())


def test_haversine_requires_coordinates():
    request = _request(matrix={"provider": "haversine"})
    with pytest.raises(MatrixError, match="requires lat and lon"):
        build_matrices(request, Settings())


def test_location_limit():
    request = _request(
        stops=[
            {"id": "a", "x": 1000, "y": 0},
            {"id": "b", "x": 2000, "y": 0},
        ]
    )
    with pytest.raises(InvalidProblemError, match="exceeding"):
        build_matrices(request, Settings(max_locations=2))


def test_osrm_uses_server_configuration(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_fetch(*, base_url, profile, points, timeout_seconds):
        captured.update(
            base_url=base_url,
            profile=profile,
            points=points,
            timeout_seconds=timeout_seconds,
        )
        size = len(points)
        return (
            [[0.0 if i == j else 1234.6 for j in range(size)] for i in range(size)],
            [[0.0 if i == j else 99.0 for j in range(size)] for i in range(size)],
        )

    monkeypatch.setattr(matrix_module, "fetch_osrm_table", fake_fetch)
    request = _request(
        depots=[{"id": "depot", "lat": 31.23, "lon": 121.47}],
        stops=[{"id": "a", "lat": 31.24, "lon": 121.47}],
        matrix={"provider": "osrm"},
    )
    settings = Settings(
        osrm_base_url="http://osrm.internal:5000",
        osrm_profile="driving",
        osrm_timeout_seconds=3,
    )
    matrices = build_matrices(request, settings)
    assert matrices.provider == "osrm"
    assert matrices.distances == [[0, 1235], [1235, 0]]
    assert matrices.durations == [[0, 99], [99, 0]]
    assert captured["base_url"] == "http://osrm.internal:5000"
    assert captured["profile"] == "driving"
    assert captured["timeout_seconds"] == 3
    assert captured["points"] == [(31.23, 121.47), (31.24, 121.47)]


def test_osrm_not_configured():
    request = _request(
        depots=[{"id": "depot", "lat": 31.23, "lon": 121.47}],
        stops=[{"id": "a", "lat": 31.24, "lon": 121.47}],
        matrix={"provider": "osrm"},
    )
    with pytest.raises(MatrixProviderError, match="not configured") as exc_info:
        build_matrices(request, Settings())
    assert exc_info.value.status_code == 503


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_osrm_table_success(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "code": "Ok",
                "distances": [[0, 12.4], [13.6, 0]],
                "durations": [[0, 2.2], [3.3, 0]],
            }
        )

    monkeypatch.setattr(matrix_module.httpx, "get", fake_get)
    distances, durations = fetch_osrm_table(
        base_url="http://osrm.internal:5000/",
        profile="driving",
        points=[(31.23, 121.47), (31.24, 121.48)],
        timeout_seconds=2,
    )
    assert captured["url"] == (
        "http://osrm.internal:5000/table/v1/driving/121.4700000,31.2300000;121.4800000,31.2400000"
    )
    assert captured["params"] == {"annotations": "distance,duration"}
    assert captured["timeout"] == 2
    assert distances == [[0, 12.4], [13.6, 0]]
    assert durations == [[0, 2.2], [3.3, 0]]


def test_fetch_osrm_table_error_code(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        matrix_module.httpx,
        "get",
        lambda *args, **kwargs: _FakeResponse({"code": "NoTable"}),
    )
    with pytest.raises(MatrixProviderError, match="NoTable"):
        fetch_osrm_table(
            base_url="http://osrm.internal:5000",
            profile="driving",
            points=[(0.0, 0.0)],
            timeout_seconds=1,
        )


def test_fetch_osrm_table_rejects_unreachable_pair(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        matrix_module.httpx,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            {
                "code": "Ok",
                "distances": [[0, None], [1, 0]],
                "durations": [[0, 1], [1, 0]],
            }
        ),
    )
    with pytest.raises(MatrixProviderError, match="unreachable"):
        fetch_osrm_table(
            base_url="http://osrm.internal:5000",
            profile="driving",
            points=[(0.0, 0.0), (1.0, 1.0)],
            timeout_seconds=1,
        )


def test_fetch_osrm_table_http_error(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def fail_get(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(matrix_module.httpx, "get", fail_get)
    with pytest.raises(MatrixProviderError, match="request failed"):
        fetch_osrm_table(
            base_url="http://osrm.internal:5000",
            profile="driving",
            points=[(0.0, 0.0)],
            timeout_seconds=1,
        )


def test_fetch_osrm_table_rejects_bad_profile():
    with pytest.raises(MatrixProviderError, match="VRP_OSRM_PROFILE"):
        fetch_osrm_table(
            base_url="http://osrm.internal:5000",
            profile="../driving",
            points=[(0.0, 0.0)],
            timeout_seconds=1,
        )
