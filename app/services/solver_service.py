"""高层求解服务。"""

from __future__ import annotations

import logging
import time

from app.config import Settings
from app.core.errors import InvalidProblemError
from app.schemas.request import SolveRequest
from app.schemas.response import SolveResponse
from app.vrp.matrix import build_matrices
from app.vrp.model import VRPModel

logger = logging.getLogger(__name__)


class SolverService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def solve(self, request: SolveRequest) -> SolveResponse:
        limit = request.solver.time_limit_seconds
        maximum = self._settings.max_time_limit_seconds
        if limit > maximum + 1e-9:
            raise InvalidProblemError(
                f"solver.time_limit_seconds ({limit:g}) exceeds the configured "
                f"maximum of {maximum:g}"
            )
        if len(request.vehicles) > self._settings.max_vehicles:
            raise InvalidProblemError(
                f"instance has {len(request.vehicles)} vehicles, exceeding the "
                f"configured limit of {self._settings.max_vehicles}"
            )
        started = time.perf_counter()
        matrices = build_matrices(request, self._settings)
        logger.info(
            "VRP solve started: locations=%d vehicles=%d shipments=%d provider=%s time_limit=%.2fs",
            len(request.depots) + len(request.stops),
            len(request.vehicles),
            len(request.shipments),
            matrices.provider,
            request.solver.time_limit_seconds,
        )
        response = VRPModel(request, matrices, self._settings).solve()
        # 统计端到端引擎耗时，包含矩阵获取。
        response.solve_time_seconds = round(time.perf_counter() - started, 6)
        logger.info(
            "VRP solve finished: status=%s ortools=%s routes=%d dropped=%d "
            "distance=%.1fm elapsed=%.3fs",
            response.status.value,
            response.ortools_status,
            len(response.routes),
            len(response.dropped_stops),
            response.summary.total_distance_m,
            response.solve_time_seconds,
        )
        return response
