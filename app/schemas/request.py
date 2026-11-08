"""VRP HTTP API 请求模型。

单位
----
* 坐标：``lat``/``lon`` 为度，或 ``x``/``y`` 为米
* 距离：米
* 时间：秒
* 需求/容量：任意非负数值（支持小数）
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.vrp.quantities import (
    is_zero_quantity,
    quantity_dimension,
    validate_quantity,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeWindow(StrictModel):
    """服务时间窗；也接受 ``["earliest", "latest"]`` 形式。"""

    start: float = Field(ge=0, le=1e15)
    end: float = Field(ge=0, le=1e15)

    @model_validator(mode="before")
    @classmethod
    def _accept_pair(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("time_window must be [start, end]")
            return {"start": value[0], "end": value[1]}
        return value

    @model_validator(mode="after")
    def _check_bounds(self) -> TimeWindow:
        if self.end < self.start:
            raise ValueError("time_window.end must be >= time_window.start")
        return self


class _Location(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    x: float | None = None
    y: float | None = None


class Depot(_Location):
    """仓库。仓库仅作为车辆路线的起点/终点。"""


class Stop(_Location):
    """客户停靠点。

    ``demand`` 表示需要配送到该点的货物。货物在仓库装车、在该点卸下，
    因此路线上的载荷跟踪值会递减。成对的取送货请使用 ``shipments``。
    """

    demand: float | list[float] | None = None
    service_time: float = Field(default=0.0, ge=0, le=1e15)
    time_window: TimeWindow | None = None
    required_skills: list[str] = Field(default_factory=list)
    drop_penalty: float = Field(default=0.0, ge=0, le=1e15)


class Vehicle(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    start_depot: str
    end_depot: str | None = None
    capacity: float | list[float] | None = None
    time_window: TimeWindow | None = None
    max_duration: float | None = Field(default=None, ge=0, le=1e15)
    max_distance: float | None = Field(default=None, ge=0, le=1e15)
    fixed_cost: float = Field(default=0.0, ge=0, le=1e15)
    cost_multiplier: float = Field(default=1.0, ge=0, le=1e6)
    skills: list[str] = Field(default_factory=list)


class Shipment(StrictModel):
    """两个停靠点之间必做的取送货对。"""

    id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    pickup: str
    delivery: str
    demand: float | list[float]


class MatrixProvider(str, Enum):
    auto = "auto"
    haversine = "haversine"
    euclidean = "euclidean"
    precomputed = "precomputed"
    osrm = "osrm"


class MatrixOptions(StrictModel):
    provider: MatrixProvider = MatrixProvider.auto
    speed_mps: float = Field(default=10.0, gt=0, le=1e6)
    distances: list[list[float]] | None = None
    durations: list[list[float]] | None = None


class ObjectiveWeights(StrictModel):
    """整数目标函数的权重。

    弧成本 = ``distance * distance + travel_time * duration``，再乘以车辆的
    ``cost_multiplier``。放弃惩罚和车辆固定成本使用相同目标单位。
    """

    distance: float = Field(default=1.0, ge=0, le=1e6)
    duration: float = Field(default=0.0, ge=0, le=1e6)


class FirstSolutionStrategy(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    PATH_CHEAPEST_ARC = "PATH_CHEAPEST_ARC"
    PATH_MOST_CONSTRAINED_ARC = "PATH_MOST_CONSTRAINED_ARC"
    SAVINGS = "SAVINGS"
    GLOBAL_CHEAPEST_ARC = "GLOBAL_CHEAPEST_ARC"
    LOCAL_CHEAPEST_INSERTION = "LOCAL_CHEAPEST_INSERTION"
    PARALLEL_CHEAPEST_INSERTION = "PARALLEL_CHEAPEST_INSERTION"
    CHRISTOFIDES = "CHRISTOFIDES"
    SEQUENTIAL_CHEAPEST_INSERTION = "SEQUENTIAL_CHEAPEST_INSERTION"


class LocalSearchMetaheuristic(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    GREEDY_DESCENT = "GREEDY_DESCENT"
    GUIDED_LOCAL_SEARCH = "GUIDED_LOCAL_SEARCH"
    SIMULATED_ANNEALING = "SIMULATED_ANNEALING"
    TABU_SEARCH = "TABU_SEARCH"
    GENERIC_TABU_SEARCH = "GENERIC_TABU_SEARCH"


class SolverOptions(StrictModel):
    time_limit_seconds: float = Field(default=5.0, gt=0, le=3600)

    first_solution_strategy: FirstSolutionStrategy = FirstSolutionStrategy.AUTOMATIC
    local_search_metaheuristic: LocalSearchMetaheuristic = (
        LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    log_search: bool = False


class SolveRequest(StrictModel):
    """一个完整的 VRP 实例。

    矩阵节点顺序（``matrix.provider = "precomputed"`` 时）为：按请求顺序排列
    所有仓库，再按请求顺序排列所有停靠点。
    """

    depots: list[Depot] = Field(min_length=1)
    stops: list[Stop] = Field(default_factory=list)
    vehicles: list[Vehicle] = Field(min_length=1)
    shipments: list[Shipment] = Field(default_factory=list)
    matrix: MatrixOptions = Field(default_factory=MatrixOptions)
    objective: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    solver: SolverOptions = Field(default_factory=SolverOptions)

    @model_validator(mode="after")
    def _validate_instance(self) -> SolveRequest:
        self._validate_ids_and_references()
        self._validate_shipments()
        self._validate_matrix_options()
        self._validate_quantities()
        return self

    def _validate_ids_and_references(self) -> None:
        depot_ids = [depot.id for depot in self.depots]
        stop_ids = [stop.id for stop in self.stops]
        vehicle_ids = [vehicle.id for vehicle in self.vehicles]

        _require_unique(depot_ids, "depot")
        _require_unique(stop_ids, "stop")
        _require_unique(vehicle_ids, "vehicle")
        overlap = set(depot_ids) & set(stop_ids)
        if overlap:
            raise ValueError("depot and stop ids must not overlap: " + ", ".join(sorted(overlap)))

        depot_set = set(depot_ids)
        for vehicle in self.vehicles:
            if vehicle.start_depot not in depot_set:
                raise ValueError(
                    f"vehicle '{vehicle.id}' references unknown start_depot '{vehicle.start_depot}'"
                )
            if vehicle.end_depot is not None and vehicle.end_depot not in depot_set:
                raise ValueError(
                    f"vehicle '{vehicle.id}' references unknown end_depot '{vehicle.end_depot}'"
                )

    def _validate_shipments(self) -> None:
        if not self.shipments:
            return
        stop_by_id = {stop.id: stop for stop in self.stops}
        _require_unique([shipment.id for shipment in self.shipments], "shipment")

        referenced: set[str] = set()
        for shipment in self.shipments:
            if shipment.pickup == shipment.delivery:
                raise ValueError(
                    f"shipment '{shipment.id}' must use different pickup and delivery stops"
                )
            for role, stop_id in (("pickup", shipment.pickup), ("delivery", shipment.delivery)):
                stop = stop_by_id.get(stop_id)
                if stop is None:
                    raise ValueError(
                        f"shipment '{shipment.id}' references unknown {role} stop '{stop_id}'"
                    )
                if stop_id in referenced:
                    raise ValueError(f"stop '{stop_id}' is referenced by more than one shipment")
                referenced.add(stop_id)
                if not is_zero_quantity(stop.demand):
                    raise ValueError(
                        f"shipment {role} stop '{stop_id}' must not declare its own demand; "
                        "use the shipment demand instead"
                    )
                if stop.drop_penalty > 0:
                    raise ValueError(
                        f"shipment {role} stop '{stop_id}' cannot be optional "
                        "(drop_penalty must be 0)"
                    )

    def _validate_matrix_options(self) -> None:
        matrix = self.matrix
        has_distances = matrix.distances is not None
        has_durations = matrix.durations is not None

        if matrix.provider == MatrixProvider.precomputed and not has_distances:
            raise ValueError("matrix.distances is required when provider is 'precomputed'")
        if matrix.provider not in (MatrixProvider.auto, MatrixProvider.precomputed):
            if has_distances or has_durations:
                raise ValueError(
                    f"matrix.distances/durations cannot be used with provider "
                    f"'{matrix.provider.value}'"
                )
        if matrix.provider == MatrixProvider.auto and has_durations and not has_distances:
            raise ValueError("matrix.durations requires matrix.distances when provider is 'auto'")

    def _validate_quantities(self) -> None:
        values = []
        values.extend(vehicle.capacity for vehicle in self.vehicles)
        values.extend(stop.demand for stop in self.stops)
        values.extend(shipment.demand for shipment in self.shipments)
        dimension = quantity_dimension(values)
        if dimension > 10:
            raise ValueError("at most 10 capacity dimensions are supported")

        for vehicle in self.vehicles:
            validate_quantity(vehicle.capacity, dimension, f"vehicle '{vehicle.id}' capacity")
        for stop in self.stops:
            validate_quantity(stop.demand, dimension, f"stop '{stop.id}' demand")
        for shipment in self.shipments:
            validate_quantity(shipment.demand, dimension, f"shipment '{shipment.id}' demand")


def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label} id(s): " + ", ".join(duplicates))
