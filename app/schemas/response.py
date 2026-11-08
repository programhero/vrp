"""求解结果响应模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SolutionStatus(str, Enum):
    optimal = "optimal"
    feasible = "feasible"
    infeasible = "infeasible"
    no_solution = "no_solution"


class StopKind(str, Enum):
    depot = "depot"
    stop = "stop"


class ShipmentRole(str, Enum):
    pickup = "pickup"
    delivery = "delivery"


class RouteStop(BaseModel):
    sequence: int
    kind: StopKind
    id: str
    name: str | None = None
    shipment_id: str | None = None
    shipment_role: ShipmentRole | None = None
    arrival_time_s: float
    waiting_time_s: float
    service_time_s: float
    departure_time_s: float
    cumulative_distance_m: float
    load: list[float]


class Route(BaseModel):
    vehicle_id: str
    vehicle_index: int
    start_time_s: float
    end_time_s: float
    duration_s: float
    distance_m: float
    travel_time_s: float
    waiting_time_s: float
    service_time_s: float
    initial_load: list[float]
    capacity: list[float] | None = None
    fixed_cost: float
    cost: float
    stops: list[RouteStop]


class DroppedStop(BaseModel):
    id: str
    name: str | None = None
    drop_penalty: float


class SolutionSummary(BaseModel):
    vehicles_used: int
    stops_total: int
    stops_served: int
    stops_dropped: int
    total_distance_m: float
    total_duration_s: float
    total_travel_time_s: float
    total_waiting_time_s: float
    total_service_time_s: float


class SolveMetadata(BaseModel):
    matrix_provider: str
    depot_count: int
    stop_count: int
    location_count: int
    vehicle_count: int
    shipment_count: int
    time_limit_seconds: float
    objective_distance_weight: float
    objective_duration_weight: float


class SolveResponse(BaseModel):
    status: SolutionStatus
    message: str | None = None
    objective_value: float | None = None
    solve_time_seconds: float
    ortools_status: str
    routes: list[Route] = Field(default_factory=list)
    dropped_stops: list[DroppedStop] = Field(default_factory=list)
    summary: SolutionSummary
    metadata: SolveMetadata


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
