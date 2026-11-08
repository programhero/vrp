from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.request import SolveRequest


def _payload(**overrides) -> dict:
    payload = {
        "depots": [{"id": "depot", "x": 0, "y": 0}],
        "stops": [{"id": "a", "x": 1000, "y": 0, "demand": 1}],
        "vehicles": [{"id": "v1", "start_depot": "depot", "capacity": 10}],
    }
    payload.update(overrides)
    return payload


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        SolveRequest.model_validate(_payload(unexpected=True))


def test_duplicate_stop_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate stop id"):
        SolveRequest.model_validate(
            _payload(
                stops=[
                    {"id": "a", "x": 1, "y": 0},
                    {"id": "a", "x": 2, "y": 0},
                ]
            )
        )


def test_depot_and_stop_ids_must_not_overlap():
    with pytest.raises(ValidationError, match="must not overlap"):
        SolveRequest.model_validate(_payload(stops=[{"id": "depot", "x": 1, "y": 0}]))


def test_unknown_depot_reference_rejected():
    with pytest.raises(ValidationError, match="unknown start_depot"):
        SolveRequest.model_validate(_payload(vehicles=[{"id": "v1", "start_depot": "missing"}]))


def test_negative_demand_rejected():
    with pytest.raises(ValidationError, match="must be >= 0"):
        SolveRequest.model_validate(_payload(stops=[{"id": "a", "x": 1, "y": 0, "demand": -1}]))


def test_inconsistent_quantity_dimensions_rejected():
    with pytest.raises(ValidationError, match="provide a list"):
        SolveRequest.model_validate(
            _payload(
                stops=[{"id": "a", "x": 1, "y": 0, "demand": 2}],
                vehicles=[
                    {
                        "id": "v1",
                        "start_depot": "depot",
                        "capacity": [10, 10],
                    }
                ],
            )
        )


def test_more_than_ten_quantity_dimensions_rejected():
    capacity = list(range(1, 12))
    with pytest.raises(ValidationError, match="at most 10 capacity dimensions"):
        SolveRequest.model_validate(
            _payload(vehicles=[{"id": "v1", "start_depot": "depot", "capacity": capacity}])
        )


def test_shipment_stop_cannot_declare_demand():
    with pytest.raises(ValidationError, match="must not declare its own demand"):
        SolveRequest.model_validate(
            _payload(
                stops=[
                    {"id": "p", "x": 1, "y": 0, "demand": 1},
                    {"id": "d", "x": 2, "y": 0},
                ],
                shipments=[{"id": "s", "pickup": "p", "delivery": "d", "demand": 1}],
            )
        )


def test_shipment_stop_cannot_be_optional():
    with pytest.raises(ValidationError, match="cannot be optional"):
        SolveRequest.model_validate(
            _payload(
                stops=[
                    {"id": "p", "x": 1, "y": 0, "drop_penalty": 10},
                    {"id": "d", "x": 2, "y": 0},
                ],
                shipments=[{"id": "s", "pickup": "p", "delivery": "d", "demand": 1}],
            )
        )


def test_shipment_stop_can_only_belong_to_one_shipment():
    with pytest.raises(ValidationError, match="more than one shipment"):
        SolveRequest.model_validate(
            _payload(
                stops=[
                    {"id": "p", "x": 1, "y": 0},
                    {"id": "d", "x": 2, "y": 0},
                    {"id": "d2", "x": 3, "y": 0},
                ],
                shipments=[
                    {"id": "s1", "pickup": "p", "delivery": "d", "demand": 1},
                    {"id": "s2", "pickup": "p", "delivery": "d2", "demand": 1},
                ],
            )
        )


def test_precomputed_provider_requires_distances():
    with pytest.raises(ValidationError, match="matrix.distances is required"):
        SolveRequest.model_validate(_payload(matrix={"provider": "precomputed"}))


def test_coordinate_provider_rejects_precomputed_matrices():
    with pytest.raises(ValidationError, match="cannot be used with provider"):
        SolveRequest.model_validate(
            _payload(
                matrix={
                    "provider": "haversine",
                    "distances": [[0, 1], [1, 0]],
                }
            )
        )


def test_time_window_pair_and_order_validation():
    request = SolveRequest.model_validate(
        _payload(
            stops=[
                {
                    "id": "a",
                    "x": 1000,
                    "y": 0,
                    "time_window": [10, 20],
                }
            ]
        )
    )
    assert request.stops[0].time_window is not None
    assert request.stops[0].time_window.start == 10

    with pytest.raises(ValidationError, match="end must be >= time_window.start"):
        SolveRequest.model_validate(
            _payload(stops=[{"id": "a", "x": 1, "y": 0, "time_window": [20, 10]}])
        )


def test_time_window_must_have_two_values():
    with pytest.raises(ValidationError, match="must be"):
        SolveRequest.model_validate(
            _payload(stops=[{"id": "a", "x": 1, "y": 0, "time_window": [1, 2, 3]}])
        )


def test_quantity_magnitude_limit():
    with pytest.raises(ValidationError, match="must be"):
        SolveRequest.model_validate(_payload(stops=[{"id": "a", "x": 1, "y": 0, "demand": 1e13}]))
