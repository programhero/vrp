from __future__ import annotations

from itertools import pairwise

import pytest

from app.config import Settings
from app.core.errors import InvalidProblemError
from app.schemas.request import SolveRequest
from app.schemas.response import SolutionStatus, StopKind
from app.services.solver_service import SolverService

FEASIBLE = {SolutionStatus.feasible, SolutionStatus.optimal}


def _solve(solver: SolverService, payload: dict):
    return solver.solve(SolveRequest.model_validate(payload))


def _cvrp_payload() -> dict:
    return {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1000, "y": 0, "demand": 4},
            {"id": "b", "x": 2000, "y": 0, "demand": 4},
            {"id": "c", "x": 2000, "y": 1000, "demand": 4},
            {"id": "d", "x": 0, "y": 1000, "demand": 4},
        ],
        "vehicles": [
            {"id": "v1", "start_depot": "depot", "capacity": 10},
            {"id": "v2", "start_depot": "depot", "capacity": 10},
        ],
        "solver": {"time_limit_seconds": 0.25},
    }


def test_cvrp_routes_and_capacity(solver: SolverService):
    result = _solve(solver, _cvrp_payload())
    assert result.status in FEASIBLE
    assert result.summary.stops_total == 4
    assert result.summary.stops_served == 4
    assert result.summary.stops_dropped == 0
    assert result.dropped_stops == []
    assert len(result.routes) == 2
    demands = {stop["id"]: stop["demand"] for stop in _cvrp_payload()["stops"]}
    total_distance = 0.0
    total_cost = 0.0
    for route in result.routes:
        assert route.capacity == [10.0]
        assert route.stops[0].kind is StopKind.depot
        assert route.stops[0].id == "depot"
        assert route.stops[-1].kind is StopKind.depot
        assert route.stops[-1].id == "depot"
        assert route.stops[-1].load == [0.0]
        assert route.stops[0].arrival_time_s == route.start_time_s
        assert route.stops[-1].arrival_time_s == route.end_time_s
        assert route.duration_s == pytest.approx(
            route.travel_time_s + route.service_time_s + route.waiting_time_s
        )
        customer_stops = [stop for stop in route.stops if stop.kind is StopKind.stop]
        assert len(customer_stops) == 2
        expected_load = sum(demands[stop.id] for stop in customer_stops)
        assert route.initial_load == [float(expected_load)]
        for stop in route.stops:
            assert stop.load[0] >= 0
            assert stop.load[0] <= 10
            assert stop.departure_time_s == pytest.approx(stop.arrival_time_s + stop.service_time_s)
        for previous, current in pairwise(route.stops):
            assert current.cumulative_distance_m > previous.cumulative_distance_m
        total_distance += route.distance_m
        total_cost += route.cost
    assert result.summary.total_distance_m == pytest.approx(total_distance)
    assert result.summary.vehicles_used == 2
    assert result.objective_value == pytest.approx(total_cost)


def test_solution_respects_capacity_at_every_stop(solver: SolverService):
    payload = _cvrp_payload()
    payload["vehicles"][0]["capacity"] = 8
    payload["vehicles"][1]["capacity"] = 8
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.summary.stops_served == 4
    for route in result.routes:
        for stop in route.stops:
            assert 0 <= stop.load[0] <= 8


def test_unlimited_capacity(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1000, "y": 0, "demand": 10},
            {"id": "b", "x": 2000, "y": 0, "demand": 20},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert len(result.routes) == 1
    assert result.routes[0].capacity is None
    assert result.routes[0].initial_load == [30.0]
    assert result.routes[0].stops[-1].load == [0.0]


def test_empty_stop_set(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.routes == []
    assert result.summary.vehicles_used == 0
    assert result.summary.stops_total == 0


def test_time_windows_waiting_and_service(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1000, "y": 0, "service_time": 60, "time_window": [0, 1000]},
            {
                "id": "b",
                "x": 2000,
                "y": 0,
                "service_time": 60,
                "time_window": [5000, 6000],
            },
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot", "time_window": [0, 10000]}],
        "solver": {"time_limit_seconds": 0.25},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert len(result.routes) == 1
    route = result.routes[0]
    windows = {"a": (0, 1000), "b": (5000, 6000)}
    by_id = {stop.id: stop for stop in route.stops}
    for stop_id, (start, end) in windows.items():
        assert start <= by_id[stop_id].arrival_time_s <= end
    assert by_id["b"].waiting_time_s > 4000
    assert route.service_time_s == 120
    assert route.end_time_s <= 10000
    assert route.waiting_time_s == pytest.approx(
        route.duration_s - route.travel_time_s - route.service_time_s
    )


def test_pickup_delivery_shipment(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "p1", "x": 1000, "y": 0},
            {"id": "d1", "x": 3000, "y": 0},
            {"id": "c1", "x": 1000, "y": 1000, "demand": 3},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot", "capacity": 8}],
        "shipments": [{"id": "s1", "pickup": "p1", "delivery": "d1", "demand": 5}],
        "solver": {"time_limit_seconds": 0.25},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert len(result.routes) == 1
    route = result.routes[0]
    assert route.initial_load == [3.0]
    assert route.stops[-1].load == [0.0]
    ids = [stop.id for stop in route.stops]
    assert ids.index("p1") < ids.index("d1")
    pickup = next(stop for stop in route.stops if stop.id == "p1")
    delivery = next(stop for stop in route.stops if stop.id == "d1")
    assert pickup.shipment_id == "s1"
    assert pickup.shipment_role.value == "pickup"
    assert delivery.shipment_id == "s1"
    assert delivery.shipment_role.value == "delivery"
    assert delivery.load[0] == pytest.approx(pickup.load[0] + 5)
    for stop in route.stops:
        assert 0 <= stop.load[0] <= 8


def test_optional_stop_is_dropped(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "near", "x": 100, "y": 0},
            {"id": "far", "x": 1_000_000, "y": 0, "drop_penalty": 10},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert [stop.id for stop in result.dropped_stops] == ["far"]
    assert result.dropped_stops[0].drop_penalty == 10
    assert result.summary.stops_served == 1
    assert len(result.routes) == 1
    assert [stop.id for stop in result.routes[0].stops] == [
        "depot",
        "near",
        "depot",
    ]
    assert result.objective_value == pytest.approx(sum(route.cost for route in result.routes) + 10)


def test_optional_stop_infeasible_time_window_is_dropped(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "near", "x": 10, "y": 0},
            {
                "id": "late",
                "x": 1000,
                "y": 0,
                "time_window": [0, 50],
                "drop_penalty": 5,
            },
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 0.25},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert "late" in [stop.id for stop in result.dropped_stops]
    assert "late" not in [stop.id for route in result.routes for stop in route.stops]


def test_skills_restrict_vehicle_assignment(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "frozen", "x": 1000, "y": 0, "required_skills": ["cold"]},
            {"id": "normal", "x": 2000, "y": 0},
        ],
        "vehicles": [
            {"id": "cold_truck", "start_depot": "depot", "skills": ["cold"]},
            {"id": "dry_truck", "start_depot": "depot"},
        ],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    route_for_frozen = next(
        route for route in result.routes if any(stop.id == "frozen" for stop in route.stops)
    )
    assert route_for_frozen.vehicle_id == "cold_truck"


def test_multi_dimension_multi_depot_and_unused_depot(solver: SolverService):
    payload = {
        "depots": [
            {"id": "d1", "x": 0, "y": 0},
            {"id": "d2", "x": 10000, "y": 0},
            {"id": "unused", "x": 5000, "y": 5000},
        ],
        "stops": [
            {"id": "a", "x": 1000, "y": 0, "demand": [3, 2]},
            {"id": "b", "x": 9000, "y": 0, "demand": [2, 4]},
            {
                "id": "cold",
                "x": 5000,
                "y": 0,
                "demand": [1, 1],
                "required_skills": ["cold"],
            },
        ],
        "vehicles": [
            {
                "id": "v1",
                "start_depot": "d1",
                "capacity": [5, 5],
                "skills": ["cold"],
            },
            {"id": "v2", "start_depot": "d2", "capacity": [5, 5]},
        ],
        "solver": {"time_limit_seconds": 0.25},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.summary.stops_served == 3
    assert len(result.routes) == 2
    stop_demands = {
        "a": [3, 2],
        "b": [2, 4],
        "cold": [1, 1],
    }
    for route in result.routes:
        assert route.capacity == [5.0, 5.0]
        assert route.stops[-1].load == [0.0, 0.0]
        customer_stops = [stop for stop in route.stops if stop.kind is StopKind.stop]
        expected = [
            sum(stop_demands[stop.id][dimension] for stop in customer_stops)
            for dimension in range(2)
        ]
        assert route.initial_load == pytest.approx(expected)
        for stop in route.stops:
            assert 0 <= stop.load[0] <= 5
            assert 0 <= stop.load[1] <= 5
        assert "unused" not in [stop.id for stop in route.stops]
        assert route.stops[0].id == route.stops[-1].id
    cold_route = next(
        route for route in result.routes if any(stop.id == "cold" for stop in route.stops)
    )
    assert cold_route.vehicle_id == "v1"


def test_fixed_cost_encourages_single_vehicle(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 10, "y": 0},
            {"id": "b", "x": 20, "y": 0},
        ],
        "vehicles": [
            {
                "id": "v1",
                "start_depot": "depot",
                "capacity": 100,
                "fixed_cost": 100,
            },
            {
                "id": "v2",
                "start_depot": "depot",
                "capacity": 100,
                "fixed_cost": 100,
            },
        ],
        "solver": {"time_limit_seconds": 0.25},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.summary.vehicles_used == 1
    assert result.routes[0].fixed_cost == 100
    assert result.routes[0].cost >= 100
    assert result.objective_value == pytest.approx(result.routes[0].cost)


def test_precomputed_asymmetric_solution(solver: SolverService):
    payload = {
        "depots": [{"id": "depot"}],
        "stops": [{"id": "a"}],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "matrix": {
            "distances": [[0, 10], [20, 0]],
            "durations": [[0, 5], [7, 0]],
        },
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.metadata.matrix_provider == "precomputed"
    route = result.routes[0]
    assert route.distance_m == 30
    assert route.travel_time_s == 12
    assert route.end_time_s == 12
    assert result.objective_value == pytest.approx(30)


def test_max_distance_infeasible(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [{"id": "a", "x": 1000, "y": 0}],
        "vehicles": [
            {
                "id": "v1",
                "start_depot": "depot",
                "max_distance": 100,
            }
        ],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in {
        SolutionStatus.infeasible,
        SolutionStatus.no_solution,
    }
    assert result.routes == []
    assert result.summary.stops_served == 0


def test_infeasible_shipment_time_windows(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "p1", "x": 1000, "y": 0, "time_window": [1000, 2000]},
            {"id": "d1", "x": 2000, "y": 0, "time_window": [0, 500]},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "shipments": [{"id": "s1", "pickup": "p1", "delivery": "d1", "demand": 1}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in {
        SolutionStatus.infeasible,
        SolutionStatus.no_solution,
    }
    assert result.routes == []


def test_all_optional_stops_can_be_dropped(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1_000_000, "y": 0, "drop_penalty": 2},
            {"id": "b", "x": 0, "y": 1_000_000, "drop_penalty": 3},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.routes == []
    assert {stop.id for stop in result.dropped_stops} == {"a", "b"}
    assert result.summary.stops_served == 0
    assert result.summary.stops_dropped == 2
    assert result.objective_value == pytest.approx(5)


def test_max_duration_constraint(solver: SolverService):
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [
            {"id": "a", "x": 1000, "y": 0},
            {"id": "b", "x": 2000, "y": 0},
        ],
        "vehicles": [{"id": "v1", "start_depot": "depot", "max_duration": 1000}],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    assert result.summary.stops_served == 2
    assert result.routes[0].duration_s <= 1000


def test_different_start_and_end_depots(solver: SolverService):
    payload = {
        "depots": [
            {"id": "A", "x": 0, "y": 0},
            {"id": "B", "x": 5000, "y": 0},
        ],
        "stops": [{"id": "s", "x": 1000, "y": 0, "demand": 1}],
        "vehicles": [
            {
                "id": "v",
                "start_depot": "A",
                "end_depot": "B",
                "capacity": 5,
            }
        ],
        "solver": {"time_limit_seconds": 0.2},
    }
    result = _solve(solver, payload)
    assert result.status in FEASIBLE
    route = result.routes[0]
    assert route.stops[0].id == "A"
    assert route.stops[-1].id == "B"
    assert route.distance_m == 5000


def test_time_limit_cannot_exceed_server_limit():
    settings = Settings(max_time_limit_seconds=1)
    service = SolverService(settings)
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [{"id": "a", "x": 1, "y": 0}],
        "vehicles": [{"id": "v1", "start_depot": "depot"}],
        "solver": {"time_limit_seconds": 2},
    }
    with pytest.raises(InvalidProblemError, match="exceeds"):
        _solve(service, payload)
