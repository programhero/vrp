"""距离与时长矩阵提供方。

支持的 provider：

* ``precomputed`` —— 请求中显式提供的方阵
* ``haversine`` —— 由 ``lat``/``lon`` 坐标计算的大圆距离
* ``euclidean`` —— 由 ``x``/``y`` 坐标计算的直线距离（米）
* ``osrm`` —— 服务端配置的 OSRM ``/table`` 服务

预计算矩阵的节点顺序：先按请求顺序排列所有仓库，再按请求顺序排列所有停靠点。
基于坐标的 provider 内部使用相同顺序。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol, TypeAlias

import httpx

from app.config import Settings
from app.core.errors import InvalidProblemError, MatrixError, MatrixProviderError
from app.schemas.request import MatrixOptions, MatrixProvider, SolveRequest

EARTH_RADIUS_M = 6_371_008.8
MAX_MATRIX_VALUE = 1e15

Point: TypeAlias = tuple[float, float]


class _Located(Protocol):
    id: str
    lat: float | None
    lon: float | None
    x: float | None
    y: float | None


@dataclass(frozen=True, slots=True)
class Matrices:
    distances: list[list[int]]
    durations: list[list[int]]
    provider: str

    @property
    def size(self) -> int:
        return len(self.distances)


def build_matrices(request: SolveRequest, settings: Settings) -> Matrices:
    """构建整数距离（米）和时长（秒）矩阵。"""
    locations = [*request.depots, *request.stops]
    size = len(locations)
    if size > settings.max_locations:
        raise InvalidProblemError(
            f"instance has {size} locations, exceeding the configured limit of "
            f"{settings.max_locations}"
        )

    provider = _resolve_provider(request.matrix, locations)
    if provider is MatrixProvider.precomputed:
        return _build_precomputed(request.matrix, size)

    points = _require_points(locations, provider)
    speed = request.matrix.speed_mps

    if provider is MatrixProvider.osrm:
        base_url = settings.osrm_base_url
        if not base_url:
            raise MatrixProviderError(
                "OSRM provider requested but VRP_OSRM_BASE_URL is not configured",
                status_code=503,
                code="osrm_not_configured",
            )
        raw_distances, raw_durations = fetch_osrm_table(
            base_url=base_url,
            profile=settings.osrm_profile,
            points=points,
            timeout_seconds=settings.osrm_timeout_seconds,
        )
        distances = _convert_matrix(raw_distances, size, "OSRM distances")
        durations = _convert_matrix(raw_durations, size, "OSRM durations")
        return Matrices(distances=distances, durations=durations, provider=provider.value)

    distances_float = _distance_matrix(points, provider)
    distances = _convert_matrix(distances_float, size, "distance matrix")
    durations = _duration_matrix(distances, speed)
    return Matrices(distances=distances, durations=durations, provider=provider.value)


def _resolve_provider(options: MatrixOptions, locations: list[_Located]) -> MatrixProvider:
    provider = options.provider
    if provider is not MatrixProvider.auto:
        return provider
    if options.distances is not None:
        return MatrixProvider.precomputed

    if all(_has_latlon(location) for location in locations):
        return MatrixProvider.haversine
    if all(_has_xy(location) for location in locations):
        return MatrixProvider.euclidean
    raise MatrixError(
        "cannot infer a matrix provider: provide matrix.distances, lat/lon "
        "coordinates, or x/y coordinates for every location"
    )


def _has_latlon(location: _Located) -> bool:
    return location.lat is not None and location.lon is not None


def _has_xy(location: _Located) -> bool:
    return location.x is not None and location.y is not None


def _latlon_point(location: _Located) -> Point:
    if location.lat is None or location.lon is None:  # pragma: no cover - 已提前校验
        raise AssertionError("location has no lat/lon")
    return float(location.lat), float(location.lon)


def _xy_point(location: _Located) -> Point:
    if location.x is None or location.y is None:  # pragma: no cover - 已提前校验
        raise AssertionError("location has no x/y")
    return float(location.x), float(location.y)


def _require_points(locations: list[_Located], provider: MatrixProvider) -> list[Point]:
    if provider in (MatrixProvider.haversine, MatrixProvider.osrm):
        missing = [location.id for location in locations if not _has_latlon(location)]
        if missing:
            raise MatrixError(
                f"provider '{provider.value}' requires lat and lon for every location; "
                f"missing coordinates for: {_preview(missing)}"
            )
        points = [_latlon_point(location) for location in locations]
    elif provider is MatrixProvider.euclidean:
        missing = [location.id for location in locations if not _has_xy(location)]
        if missing:
            raise MatrixError(
                "provider 'euclidean' requires x and y for every location; "
                f"missing coordinates for: {_preview(missing)}"
            )
        points = [_xy_point(location) for location in locations]
    else:  # pragma: no cover - 所有 provider 均已被调用方处理
        raise MatrixError(f"provider '{provider.value}' does not use coordinates")

    for index, point in enumerate(points):
        if not all(math.isfinite(value) for value in point):
            location_id = locations[index].id
            raise MatrixError(f"location '{location_id}' has a non-finite coordinate")
    return points


def _distance_matrix(points: list[Point], provider: MatrixProvider) -> list[list[float]]:
    size = len(points)
    matrix = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            if provider is MatrixProvider.haversine:
                distance = haversine_m(points[i], points[j])
            else:
                distance = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
            matrix[i][j] = distance
            matrix[j][i] = distance
    return matrix


def _duration_matrix(distances: list[list[int]], speed_mps: float) -> list[list[int]]:
    size = len(distances)
    matrix = [[0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            value = distances[i][j] / speed_mps
            if not math.isfinite(value) or value < 0 or value > MAX_MATRIX_VALUE:
                raise MatrixError(
                    "computed travel duration is invalid; check matrix.speed_mps and coordinates"
                )
            matrix[i][j] = int(round(value))
    return matrix


def _build_precomputed(options: MatrixOptions, size: int) -> Matrices:
    assert options.distances is not None  # 请求 schema 已校验
    distances = _convert_matrix(options.distances, size, "matrix.distances")
    if options.durations is not None:
        durations = _convert_matrix(options.durations, size, "matrix.durations")
    else:
        durations = _duration_matrix(distances, options.speed_mps)
    return Matrices(
        distances=distances,
        durations=durations,
        provider=MatrixProvider.precomputed.value,
    )


def _convert_matrix(values: list[list[float]], size: int, name: str) -> list[list[int]]:
    if len(values) != size:
        raise MatrixError(f"{name} must have {size} rows, got {len(values)}")
    result: list[list[int]] = []
    for row_index, row in enumerate(values):
        if len(row) != size:
            raise MatrixError(f"{name} row {row_index} must have {size} columns, got {len(row)}")
        converted_row: list[int] = []
        for column_index, value in enumerate(row):
            number = float(value)
            if not math.isfinite(number):
                raise MatrixError(f"{name}[{row_index}][{column_index}] is not finite")
            if number < 0:
                raise MatrixError(f"{name}[{row_index}][{column_index}] must be >= 0")
            if number > MAX_MATRIX_VALUE:
                raise MatrixError(
                    f"{name}[{row_index}][{column_index}] exceeds the supported "
                    f"maximum of {MAX_MATRIX_VALUE:g}"
                )
            converted_row.append(int(round(number)))
        result.append(converted_row)
    return result


def haversine_m(first: Point, second: Point) -> float:
    """返回大圆距离（米）。每个点为 (lat, lon) 度。"""
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


_OSRM_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def fetch_osrm_table(
    *,
    base_url: str,
    profile: str,
    points: list[Point],
    timeout_seconds: float,
) -> tuple[list[list[float]], list[list[float]]]:
    """调用 OSRM table 服务。点为 (lat, lon)。

    保留为模块级函数，便于在测试中 mock。
    """
    if not _OSRM_PROFILE_RE.match(profile):
        raise MatrixProviderError(
            "VRP_OSRM_PROFILE may only contain letters, digits, '_' and '-'",
            code="osrm_invalid_profile",
        )
    coordinates = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)
    url = f"{base_url.rstrip('/')}/table/v1/{profile}/{coordinates}"
    try:
        response = httpx.get(
            url,
            params={"annotations": "distance,duration"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MatrixProviderError(f"OSRM table request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise MatrixProviderError("OSRM returned a non-JSON response") from exc

    if not isinstance(payload, dict) or payload.get("code") != "Ok":
        code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        raise MatrixProviderError(
            f"OSRM table request failed with code '{code}'" + (f": {message}" if message else "")
        )

    distances = payload.get("distances")
    durations = payload.get("durations")
    if distances is None or durations is None:
        raise MatrixProviderError(
            "OSRM response did not include both distance and duration matrices"
        )
    _validate_osrm_shape(distances, len(points), "distances")
    _validate_osrm_shape(durations, len(points), "durations")
    return distances, durations


def _validate_osrm_shape(values: object, size: int, name: str) -> None:
    if not isinstance(values, list) or len(values) != size:
        raise MatrixProviderError(f"OSRM {name} matrix has an unexpected shape")
    for row_index, row in enumerate(values):
        if not isinstance(row, list) or len(row) != size:
            raise MatrixProviderError(f"OSRM {name} matrix has an unexpected shape")
        for column_index, value in enumerate(row):
            if value is None:
                raise MatrixProviderError(
                    f"OSRM {name} matrix contains an unreachable pair at "
                    f"[{row_index}][{column_index}]"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MatrixProviderError(f"OSRM {name} matrix contains a non-numeric value")
            if not math.isfinite(float(value)) or value < 0:
                raise MatrixProviderError(
                    f"OSRM {name} matrix contains an invalid value at [{row_index}][{column_index}]"
                )


def _preview(values: list[str], limit: int = 10) -> str:
    shown = ", ".join(values[:limit])
    if len(values) > limit:
        shown += f", ... ({len(values) - limit} more)"
    return shown
