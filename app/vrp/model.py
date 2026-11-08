"""构建并求解 OR-Tools 路径模型。

建模说明
--------
* 每个停靠点对应一个模型节点；每辆车有两个专用的虚拟端点节点（起点和终点）。
  因此仓库永远不会作为路线内部停靠点被插入。
* 送货建模为负的载荷变化，shipment 建模为 pickup 正变化加 delivery 负变化。
  每个容量维度都从自由的初始载荷开始，且必须在结束时归零，从而跟踪车辆的真实载荷。
* 行驶时间为距离 / 速度（或预计算时长），出发节点的服务时间加入时间维度的
  transit。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.config import Settings
from app.core.errors import InvalidProblemError
from app.schemas.request import (
    FirstSolutionStrategy,
    LocalSearchMetaheuristic,
    Shipment,
    SolveRequest,
    Stop,
    Vehicle,
)
from app.schemas.response import (
    DroppedStop,
    Route,
    RouteStop,
    ShipmentRole,
    SolutionStatus,
    SolutionSummary,
    SolveMetadata,
    SolveResponse,
    StopKind,
)
from app.vrp.matrix import Matrices
from app.vrp.quantities import (
    from_scaled,
    quantity_dimension,
    to_scaled,
    to_vector,
)

logger = logging.getLogger(__name__)

OBJECTIVE_SCALE = 1000
_INT64_SAFE_LIMIT = 4_000_000_000_000_000_000
_MAX_HORIZON = 8_000_000_000_000_000_000

_ROUTING_OPTIMAL = routing_enums_pb2.RoutingSearchStatus.ROUTING_OPTIMAL
_ROUTING_INFEASIBLE = routing_enums_pb2.RoutingSearchStatus.ROUTING_INFEASIBLE
_ROUTING_INVALID = routing_enums_pb2.RoutingSearchStatus.ROUTING_INVALID

_FIRST_SOLUTION_STRATEGIES = {
    FirstSolutionStrategy.AUTOMATIC: routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC,
    FirstSolutionStrategy.PATH_CHEAPEST_ARC: (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    ),
    FirstSolutionStrategy.PATH_MOST_CONSTRAINED_ARC: (
        routing_enums_pb2.FirstSolutionStrategy.PATH_MOST_CONSTRAINED_ARC
    ),
    FirstSolutionStrategy.SAVINGS: routing_enums_pb2.FirstSolutionStrategy.SAVINGS,
    FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC: (
        routing_enums_pb2.FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC
    ),
    FirstSolutionStrategy.LOCAL_CHEAPEST_INSERTION: (
        routing_enums_pb2.FirstSolutionStrategy.LOCAL_CHEAPEST_INSERTION
    ),
    FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION: (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    ),
    FirstSolutionStrategy.CHRISTOFIDES: routing_enums_pb2.FirstSolutionStrategy.CHRISTOFIDES,
    FirstSolutionStrategy.SEQUENTIAL_CHEAPEST_INSERTION: (
        routing_enums_pb2.FirstSolutionStrategy.SEQUENTIAL_CHEAPEST_INSERTION
    ),
}

_METAHEURISTICS = {
    LocalSearchMetaheuristic.AUTOMATIC: (routing_enums_pb2.LocalSearchMetaheuristic.AUTOMATIC),
    LocalSearchMetaheuristic.GREEDY_DESCENT: (
        routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
    ),
    LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH: (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    ),
    LocalSearchMetaheuristic.SIMULATED_ANNEALING: (
        routing_enums_pb2.LocalSearchMetaheuristic.SIMULATED_ANNEALING
    ),
    LocalSearchMetaheuristic.TABU_SEARCH: (routing_enums_pb2.LocalSearchMetaheuristic.TABU_SEARCH),
    LocalSearchMetaheuristic.GENERIC_TABU_SEARCH: (
        routing_enums_pb2.LocalSearchMetaheuristic.GENERIC_TABU_SEARCH
    ),
}


@dataclass(slots=True)
class _ExtractedRoute:
    route: Route
    visited_stop_ids: set[str]


class VRPModel:
    """单个待求解的 VRP 实例。"""

    def __init__(self, request: SolveRequest, matrices: Matrices, settings: Settings) -> None:
        self._request = request
        self._matrices = matrices
        self._settings = settings
        self._stops: list[Stop] = request.stops
        self._vehicles: list[Vehicle] = request.vehicles
        self._stop_count = len(request.stops)
        self._vehicle_count = len(request.vehicles)
        self._depot_index = {depot.id: index for index, depot in enumerate(request.depots)}
        self._depot_by_id = {depot.id: depot for depot in request.depots}
        self._stop_index = {stop.id: index for index, stop in enumerate(request.stops)}
        self._roles: dict[int, tuple[Shipment, ShipmentRole]] = {}

        self._validate_limits()
        self._prepare_layout()
        self._prepare_eligibility()
        self._guard_objective_magnitude()

        self._horizon_distance = self._compute_distance_horizon()
        self._horizon_time = self._compute_time_horizon()
        self._search_parameters = self._build_search_parameters()

        self._manager: pywrapcp.RoutingIndexManager
        self._routing: pywrapcp.RoutingModel
        self._distance_dim: pywrapcp.RoutingDimension
        self._time_dim: pywrapcp.RoutingDimension
        self._capacity_dims: list[pywrapcp.RoutingDimension] = []
        self._callback_refs: list[Callable[..., int]] = []

    # ------------------------------------------------------------------
    # 校验与准备
    # ------------------------------------------------------------------
    def _validate_limits(self) -> None:
        if self._vehicle_count > self._settings.max_vehicles:
            raise InvalidProblemError(
                f"instance has {self._vehicle_count} vehicles, exceeding the configured "
                f"limit of {self._settings.max_vehicles}"
            )
        time_limit = self._request.solver.time_limit_seconds
        if time_limit > self._settings.max_time_limit_seconds + 1e-9:
            raise InvalidProblemError(
                f"solver.time_limit_seconds ({time_limit:g}) exceeds the configured "
                f"maximum of {self._settings.max_time_limit_seconds:g}"
            )

    def _prepare_layout(self) -> None:
        depot_count = len(self._depot_index)
        stop_count = self._stop_count
        vehicle_count = self._vehicle_count

        values = [vehicle.capacity for vehicle in self._vehicles]
        values.extend(stop.demand for stop in self._stops)
        values.extend(shipment.demand for shipment in self._request.shipments)
        self._quantity_dims = quantity_dimension(values)

        # 模型节点顺序：先是所有停靠点，然后依次是每辆车的（起点、终点）。
        self._start_nodes = [stop_count + 2 * vehicle for vehicle in range(vehicle_count)]
        self._end_nodes = [stop_count + 2 * vehicle + 1 for vehicle in range(vehicle_count)]
        self._model_physical = [depot_count + index for index in range(stop_count)]
        for vehicle in self._vehicles:
            self._model_physical.append(self._depot_index[vehicle.start_depot])
            end_depot = vehicle.end_depot or vehicle.start_depot
            self._model_physical.append(self._depot_index[end_depot])
        self._end_depot_ids = [
            vehicle.end_depot or vehicle.start_depot for vehicle in self._vehicles
        ]

        model_nodes = stop_count + 2 * vehicle_count
        self._service = [0] * model_nodes
        for index, stop in enumerate(self._stops):
            self._service[index] = int(round(stop.service_time))

        # 按模型节点记录的载荷变化：送货为负（卸货）；shipment 在 pickup
        # 装货（正）、在 delivery 卸货（负）。
        dims = self._quantity_dims
        self._demand = [[0] * dims for _ in range(model_nodes)]
        for index, stop in enumerate(self._stops):
            self._demand[index] = [-value for value in to_scaled(stop.demand, dims)]

        for shipment in self._request.shipments:
            pickup_index = self._stop_index[shipment.pickup]
            delivery_index = self._stop_index[shipment.delivery]
            demand = to_scaled(shipment.demand, dims)
            for dimension in range(dims):
                self._demand[pickup_index][dimension] += demand[dimension]
                self._demand[delivery_index][dimension] -= demand[dimension]
            self._roles[pickup_index] = (shipment, ShipmentRole.pickup)
            self._roles[delivery_index] = (shipment, ShipmentRole.delivery)

        total_absolute = [0] * dims
        for demand in self._demand:
            for dimension in range(dims):
                total_absolute[dimension] += abs(demand[dimension])
        unlimited = [max(1, total + 1) for total in total_absolute]

        self._capacity_scaled: list[list[int]] = []
        self._capacity_user: list[list[float] | None] = []
        for vehicle in self._vehicles:
            if vehicle.capacity is None:
                self._capacity_scaled.append(list(unlimited))
                self._capacity_user.append(None)
            else:
                self._capacity_scaled.append(to_scaled(vehicle.capacity, dims))
                self._capacity_user.append(to_vector(vehicle.capacity, dims))

    def _prepare_eligibility(self) -> None:
        vehicle_skills = [set(vehicle.skills) for vehicle in self._vehicles]
        self._eligible_vehicles: list[list[int]] = []
        for stop in self._stops:
            required = set(stop.required_skills)
            eligible = [
                index
                for index in range(self._vehicle_count)
                if required.issubset(vehicle_skills[index])
            ]
            if not eligible:
                raise InvalidProblemError(
                    f"stop '{stop.id}' requires skill(s) {sorted(required)} that no "
                    "vehicle provides"
                )
            self._eligible_vehicles.append(eligible)

        for shipment in self._request.shipments:
            pickup = self._stop_index[shipment.pickup]
            delivery = self._stop_index[shipment.delivery]
            if not set(self._eligible_vehicles[pickup]) & set(self._eligible_vehicles[delivery]):
                raise InvalidProblemError(
                    f"shipment '{shipment.id}' pickup and delivery require skills that "
                    "no single vehicle provides"
                )

    def _guard_objective_magnitude(self) -> None:
        max_matrix_value = 0
        for matrix in (self._matrices.distances, self._matrices.durations):
            for row in matrix:
                if row:
                    max_matrix_value = max(max_matrix_value, max(row))
        weights = self._request.objective
        max_multiplier = max(vehicle.cost_multiplier for vehicle in self._vehicles)
        per_arc = (
            math.ceil(
                max_matrix_value
                * (weights.distance + weights.duration)
                * max_multiplier
                * OBJECTIVE_SCALE
            )
            + 1
        )
        max_arcs = self._stop_count + self._vehicle_count
        fixed_total = sum(
            int(round(vehicle.fixed_cost * OBJECTIVE_SCALE)) for vehicle in self._vehicles
        )
        penalty_total = sum(
            max(1, int(round(stop.drop_penalty * OBJECTIVE_SCALE)))
            for stop in self._stops
            if stop.drop_penalty > 0
        )
        estimated = per_arc * max_arcs + fixed_total + penalty_total
        if estimated > _INT64_SAFE_LIMIT:
            raise InvalidProblemError(
                "objective coefficients are too large to be represented safely; "
                "reduce distances/durations, weights, cost multipliers, fixed costs, "
                "or drop penalties"
            )

    def _compute_distance_horizon(self) -> int:
        max_arc = 0
        for row in self._matrices.distances:
            if row:
                max_arc = max(max_arc, max(row))
        horizon = (self._stop_count + 1) * max_arc
        if horizon > _MAX_HORIZON:
            raise InvalidProblemError("distance matrix values are too large")
        return max(1, horizon)

    def _compute_time_horizon(self) -> int:
        max_window_end = 0.0
        for stop in self._stops:
            if stop.time_window is not None:
                max_window_end = max(max_window_end, stop.time_window.end)
        for vehicle in self._vehicles:
            if vehicle.time_window is not None:
                max_window_end = max(max_window_end, vehicle.time_window.end)

        service_total = sum(int(round(stop.service_time)) for stop in self._stops)
        max_arc = 0
        for row in self._matrices.durations:
            if row:
                max_arc = max(max_arc, max(row))
        horizon = math.ceil(max_window_end) + service_total + (self._stop_count + 1) * max_arc + 60
        if horizon > _MAX_HORIZON:
            raise InvalidProblemError("travel times or time windows are too large")
        return int(horizon)

    def _build_search_parameters(self):
        options = self._request.solver
        parameters = pywrapcp.DefaultRoutingSearchParameters()
        parameters.first_solution_strategy = _FIRST_SOLUTION_STRATEGIES[
            options.first_solution_strategy
        ]
        parameters.local_search_metaheuristic = _METAHEURISTICS[options.local_search_metaheuristic]
        parameters.time_limit.FromMilliseconds(
            max(1, int(round(options.time_limit_seconds * 1000)))
        )
        parameters.log_search = options.log_search
        return parameters

    # ------------------------------------------------------------------
    # 模型构建
    # ------------------------------------------------------------------
    def _build_model(self) -> None:
        stop_count = self._stop_count
        vehicle_count = self._vehicle_count
        model_nodes = stop_count + 2 * vehicle_count
        manager = pywrapcp.RoutingIndexManager(
            model_nodes,
            vehicle_count,
            self._start_nodes,
            self._end_nodes,
        )
        routing = pywrapcp.RoutingModel(manager)
        self._manager = manager
        self._routing = routing

        def distance_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return self._matrices.distances[self._model_physical[from_node]][
                self._model_physical[to_node]
            ]

        def time_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return (
                self._matrices.durations[self._model_physical[from_node]][
                    self._model_physical[to_node]
                ]
                + self._service[from_node]
            )

        self._callback_refs.extend([distance_callback, time_callback])
        distance_evaluator = routing.RegisterTransitCallback(distance_callback)
        time_evaluator = routing.RegisterTransitCallback(time_callback)

        for vehicle_index, vehicle in enumerate(self._vehicles):

            def cost_callback(from_index: int, to_index: int, index: int = vehicle_index) -> int:
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                return self._arc_cost_scaled(index, from_node, to_node)

            self._callback_refs.append(cost_callback)
            cost_evaluator = routing.RegisterTransitCallback(cost_callback)
            routing.SetArcCostEvaluatorOfVehicle(cost_evaluator, vehicle_index)
            fixed_cost = int(round(vehicle.fixed_cost * OBJECTIVE_SCALE))
            if fixed_cost:
                routing.SetFixedCostOfVehicle(fixed_cost, vehicle_index)

        # 容量：每个数量维度对应一个维度。载荷跟踪可以从已装载状态
        # 开始（送货），并且必须在路线结束时归零。
        for dimension in range(self._quantity_dims):

            def demand_callback(from_index: int, dim: int = dimension) -> int:
                return self._demand[manager.IndexToNode(from_index)][dim]

            self._callback_refs.append(demand_callback)
            demand_evaluator = routing.RegisterUnaryTransitCallback(demand_callback)
            capacities = [
                self._capacity_scaled[vehicle][dimension] for vehicle in range(vehicle_count)
            ]
            name = f"Capacity_{dimension}"
            routing.AddDimensionWithVehicleCapacity(
                demand_evaluator,
                0,
                capacities,
                False,
                name,
            )
            capacity_dimension = routing.GetDimensionOrDie(name)
            for vehicle in range(vehicle_count):
                capacity_dimension.CumulVar(routing.End(vehicle)).SetRange(0, 0)
            self._capacity_dims.append(capacity_dimension)

        # 距离维度：用于结果上报和单车距离上限。
        routing.AddDimension(distance_evaluator, 0, self._horizon_distance, True, "Distance")
        self._distance_dim = routing.GetDimensionOrDie("Distance")
        for vehicle_index, vehicle in enumerate(self._vehicles):
            if vehicle.max_distance is not None:
                self._distance_dim.SetSpanUpperBoundForVehicle(
                    int(round(vehicle.max_distance)), vehicle_index
                )

        # 时间维度：slack 以时间跨度上界为上限，从而允许等待。
        routing.AddDimension(
            time_evaluator,
            self._horizon_time,
            self._horizon_time,
            False,
            "Time",
        )
        self._time_dim = routing.GetDimensionOrDie("Time")
        for stop_index, stop in enumerate(self._stops):
            if stop.time_window is None:
                continue
            index = manager.NodeToIndex(stop_index)
            self._time_dim.CumulVar(index).SetRange(
                int(round(stop.time_window.start)),
                int(round(stop.time_window.end)),
            )

        for vehicle_index, vehicle in enumerate(self._vehicles):
            if vehicle.time_window is not None:
                start = int(round(vehicle.time_window.start))
                end = int(round(vehicle.time_window.end))
            else:
                start, end = 0, self._horizon_time
            self._time_dim.CumulVar(routing.Start(vehicle_index)).SetRange(start, end)
            self._time_dim.CumulVar(routing.End(vehicle_index)).SetRange(start, end)
            if vehicle.max_duration is not None:
                self._time_dim.SetSpanUpperBoundForVehicle(
                    int(round(vehicle.max_duration)), vehicle_index
                )

        # 节点约束。
        for stop_index, stop in enumerate(self._stops):
            node_index = manager.NodeToIndex(stop_index)
            if stop.required_skills:
                # 此 OR-Tools 构建未提供可用的 SetAllowedVehiclesForIndex
                # 绑定（它要求 absl::Span），因此通过显式约束排除不允许的车辆。
                allowed = set(self._eligible_vehicles[stop_index])
                vehicle_var = routing.VehicleVar(node_index)
                for vehicle_index in range(vehicle_count):
                    if vehicle_index not in allowed:
                        routing.solver().Add(vehicle_var != vehicle_index)
            if stop.drop_penalty > 0:
                penalty = max(1, int(round(stop.drop_penalty * OBJECTIVE_SCALE)))
                routing.AddDisjunction([node_index], penalty)

        for shipment in self._request.shipments:
            pickup_index = manager.NodeToIndex(self._stop_index[shipment.pickup])
            delivery_index = manager.NodeToIndex(self._stop_index[shipment.delivery])
            routing.AddPickupAndDelivery(pickup_index, delivery_index)
            routing.solver().Add(
                routing.VehicleVar(pickup_index) == routing.VehicleVar(delivery_index)
            )
            routing.solver().Add(
                self._time_dim.CumulVar(pickup_index) <= self._time_dim.CumulVar(delivery_index)
            )

    def _arc_cost_scaled(self, vehicle_index: int, from_node: int, to_node: int) -> int:
        from_physical = self._model_physical[from_node]
        to_physical = self._model_physical[to_node]
        weights = self._request.objective
        distance = self._matrices.distances[from_physical][to_physical]
        duration = self._matrices.durations[from_physical][to_physical]
        multiplier = self._vehicles[vehicle_index].cost_multiplier
        raw = (distance * weights.distance + duration * weights.duration) * multiplier
        return int(round(raw * OBJECTIVE_SCALE))

    # ------------------------------------------------------------------
    # 求解与结果抽取
    # ------------------------------------------------------------------
    def solve(self) -> SolveResponse:
        self._build_model()
        started = time.perf_counter()
        solution = self._routing.SolveWithParameters(self._search_parameters)
        solve_seconds = time.perf_counter() - started
        status_code = self._routing.status()
        ortools_status = _routing_status_name(status_code)

        if solution is None:
            return self._no_solution_response(status_code, ortools_status, solve_seconds)

        extracted: list[_ExtractedRoute] = []
        for vehicle_index in range(self._vehicle_count):
            result = self._extract_route(vehicle_index, solution)
            if result is not None:
                extracted.append(result)

        visited: set[str] = set()
        for item in extracted:
            visited.update(item.visited_stop_ids)
        routes = [item.route for item in extracted]
        dropped = [
            DroppedStop(
                id=stop.id,
                name=stop.name,
                drop_penalty=stop.drop_penalty,
            )
            for stop in self._stops
            if stop.id not in visited
        ]

        served = len(visited)
        summary = SolutionSummary(
            vehicles_used=len(routes),
            stops_total=self._stop_count,
            stops_served=served,
            stops_dropped=self._stop_count - served,
            total_distance_m=sum(route.distance_m for route in routes),
            total_duration_s=sum(route.duration_s for route in routes),
            total_travel_time_s=sum(route.travel_time_s for route in routes),
            total_waiting_time_s=sum(route.waiting_time_s for route in routes),
            total_service_time_s=sum(route.service_time_s for route in routes),
        )
        status = (
            SolutionStatus.optimal if status_code == _ROUTING_OPTIMAL else SolutionStatus.feasible
        )
        objective_value = solution.ObjectiveValue() / OBJECTIVE_SCALE
        return SolveResponse(
            status=status,
            objective_value=objective_value,
            solve_time_seconds=solve_seconds,
            ortools_status=ortools_status,
            routes=routes,
            dropped_stops=dropped,
            summary=summary,
            metadata=self._metadata(),
        )

    def _no_solution_response(
        self, status_code: int, ortools_status: str, solve_seconds: float
    ) -> SolveResponse:
        if status_code in (_ROUTING_INFEASIBLE, _ROUTING_INVALID):
            status = SolutionStatus.infeasible
            message = "No feasible solution exists for the given constraints."
        else:
            status = SolutionStatus.no_solution
            message = "No solution was found within the time limit."
        return SolveResponse(
            status=status,
            message=message,
            objective_value=None,
            solve_time_seconds=solve_seconds,
            ortools_status=ortools_status,
            routes=[],
            dropped_stops=[],
            summary=SolutionSummary(
                vehicles_used=0,
                stops_total=self._stop_count,
                stops_served=0,
                stops_dropped=0,
                total_distance_m=0.0,
                total_duration_s=0.0,
                total_travel_time_s=0.0,
                total_waiting_time_s=0.0,
                total_service_time_s=0.0,
            ),
            metadata=self._metadata(),
        )

    def _extract_route(
        self, vehicle_index: int, solution: pywrapcp.Assignment
    ) -> _ExtractedRoute | None:
        routing = self._routing
        manager = self._manager
        vehicle = self._vehicles[vehicle_index]

        start_index = routing.Start(vehicle_index)
        start_time = int(solution.Value(self._time_dim.CumulVar(start_index)))
        start_load = self._loads_at(solution, start_index)

        start_depot = self._depot_by_id[vehicle.start_depot]
        end_depot = self._depot_by_id[self._end_depot_ids[vehicle_index]]

        stops: list[RouteStop] = [
            RouteStop(
                sequence=0,
                kind=StopKind.depot,
                id=start_depot.id,
                name=start_depot.name,
                arrival_time_s=float(start_time),
                waiting_time_s=0.0,
                service_time_s=0.0,
                departure_time_s=float(start_time),
                cumulative_distance_m=0.0,
                load=_load_vector(start_load),
            )
        ]

        previous_node = manager.IndexToNode(start_index)
        previous_arrival = start_time
        previous_service = 0
        index = start_index
        travel_total = 0
        service_total = 0
        cost_scaled = 0
        visited_stop_ids: set[str] = set()
        customer_visits = 0
        end_time = start_time
        end_distance = 0
        end_load = start_load

        while True:
            next_index = solution.Value(routing.NextVar(index))
            next_node = manager.IndexToNode(next_index)
            arrival = int(solution.Value(self._time_dim.CumulVar(next_index)))
            cumulative_distance = int(solution.Value(self._distance_dim.CumulVar(next_index)))
            load = self._loads_at(solution, next_index)

            from_physical = self._model_physical[previous_node]
            to_physical = self._model_physical[next_node]
            travel = self._matrices.durations[from_physical][to_physical]
            expected_arrival = previous_arrival + previous_service + travel
            waiting = max(0, arrival - expected_arrival)

            travel_total += travel
            cost_scaled += self._arc_cost_scaled(vehicle_index, previous_node, next_node)

            if routing.IsEnd(next_index):
                end_time = arrival
                end_distance = cumulative_distance
                end_load = load
                stops.append(
                    RouteStop(
                        sequence=len(stops),
                        kind=StopKind.depot,
                        id=end_depot.id,
                        name=end_depot.name,
                        arrival_time_s=float(arrival),
                        waiting_time_s=0.0,
                        service_time_s=0.0,
                        departure_time_s=float(arrival),
                        cumulative_distance_m=float(cumulative_distance),
                        load=_load_vector(load),
                    )
                )
                break

            if next_node >= self._stop_count:
                raise RuntimeError(
                    "route visits a virtual endpoint of another vehicle; "
                    "the routing model is inconsistent"
                )
            stop = self._stops[next_node]
            service = self._service[next_node]
            role = self._roles.get(next_node)
            shipment = role[0] if role else None
            shipment_role = role[1] if role else None

            stops.append(
                RouteStop(
                    sequence=len(stops),
                    kind=StopKind.stop,
                    id=stop.id,
                    name=stop.name,
                    shipment_id=shipment.id if shipment else None,
                    shipment_role=shipment_role,
                    arrival_time_s=float(arrival),
                    waiting_time_s=float(waiting),
                    service_time_s=float(service),
                    departure_time_s=float(arrival + service),
                    cumulative_distance_m=float(cumulative_distance),
                    load=_load_vector(load),
                )
            )
            customer_visits += 1
            visited_stop_ids.add(stop.id)
            service_total += service

            previous_node = next_node
            previous_arrival = arrival
            previous_service = service
            index = next_index

        if customer_visits == 0:
            return None
        if any(value != 0 for value in end_load):
            raise RuntimeError("a route ended with a non-zero load")

        fixed_scaled = int(round(vehicle.fixed_cost * OBJECTIVE_SCALE))
        duration = end_time - start_time
        waiting_total = max(0, duration - travel_total - service_total)
        route = Route(
            vehicle_id=vehicle.id,
            vehicle_index=vehicle_index,
            start_time_s=float(start_time),
            end_time_s=float(end_time),
            duration_s=float(duration),
            distance_m=float(end_distance),
            travel_time_s=float(travel_total),
            waiting_time_s=float(waiting_total),
            service_time_s=float(service_total),
            initial_load=_load_vector(start_load),
            capacity=self._capacity_user[vehicle_index],
            fixed_cost=vehicle.fixed_cost,
            cost=(fixed_scaled + cost_scaled) / OBJECTIVE_SCALE,
            stops=stops,
        )
        return _ExtractedRoute(route=route, visited_stop_ids=visited_stop_ids)

    def _loads_at(self, solution: pywrapcp.Assignment, index: int) -> list[int]:
        return [int(solution.Value(dimension.CumulVar(index))) for dimension in self._capacity_dims]

    def _metadata(self) -> SolveMetadata:
        request = self._request
        return SolveMetadata(
            matrix_provider=self._matrices.provider,
            depot_count=len(request.depots),
            stop_count=len(request.stops),
            location_count=len(request.depots) + len(request.stops),
            vehicle_count=len(request.vehicles),
            shipment_count=len(request.shipments),
            time_limit_seconds=request.solver.time_limit_seconds,
            objective_distance_weight=request.objective.distance,
            objective_duration_weight=request.objective.duration,
        )


def _load_vector(values: list[int]) -> list[float]:
    return [from_scaled(value) for value in values]


def _routing_status_names() -> dict[int, str]:
    """兼容旧版枚举包装器和新版嵌套消息枚举。"""
    status_class = routing_enums_pb2.RoutingSearchStatus
    try:
        enum_descriptor = status_class.DESCRIPTOR.enum_types_by_name["Value"]
    except (AttributeError, KeyError):
        enum_descriptor = None
    if enum_descriptor is not None:
        return {value.number: value.name for value in enum_descriptor.values}

    names: dict[int, str] = {}
    for value in range(8):
        try:
            names[value] = status_class.Name(value)
        except (AttributeError, ValueError):
            break
    return names


_ROUTING_STATUS_NAMES = _routing_status_names()


def _routing_status_name(status_code: int) -> str:
    return _ROUTING_STATUS_NAMES.get(status_code, str(status_code))
