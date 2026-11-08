"""多维容量与需求的辅助函数。

OR-Tools 要求数量为整数。用户输入为浮点数，因此内部按 ``QUANTITY_SCALE``
（1000）缩放，并在响应中还原。
"""

from __future__ import annotations

import math
from typing import TypeAlias

Quantity: TypeAlias = float | list[float] | None

QUANTITY_SCALE = 1000
MAX_QUANTITY = 1e12


def quantity_dimension(values: list[Quantity]) -> int:
    """返回一组数量值使用的维度数。"""
    dimension = 1
    for value in values:
        if isinstance(value, list) and len(value) > dimension:
            dimension = len(value)
    return dimension


def validate_quantity(value: Quantity, dimension: int, field_name: str) -> None:
    """按问题的全局维度数校验数量值。"""
    if value is None:
        return
    if isinstance(value, list):
        if len(value) != dimension:
            raise ValueError(f"{field_name} must contain {dimension} value(s), got {len(value)}")
        for index, item in enumerate(value):
            _validate_scalar(item, f"{field_name}[{index}]")
        return

    _validate_scalar(value, field_name)
    if dimension > 1 and float(value) != 0.0:
        raise ValueError(
            f"{field_name} is a scalar but the problem uses {dimension} capacity "
            f"dimensions; provide a list of {dimension} numbers"
        )


def to_vector(value: Quantity, dimension: int) -> list[float]:
    """将标量/None/列表数量扩展为固定长度向量。

    假定该值已通过 ``dimension`` 校验。
    """
    if value is None:
        return [0.0] * dimension
    if isinstance(value, list):
        return [float(item) for item in value]
    if dimension == 1:
        return [float(value)]
    if float(value) == 0.0:
        return [0.0] * dimension
    # 防御性处理：schema 已校验，走到这里说明存在编程错误。
    raise ValueError("scalar quantity cannot be expanded to multiple dimensions")


def to_scaled(value: Quantity, dimension: int) -> list[int]:
    """返回供 OR-Tools 使用的整数缩放数量向量。"""
    return [int(round(item * QUANTITY_SCALE)) for item in to_vector(value, dimension)]


def from_scaled(value: int) -> float:
    """将内部整数量还原为用户单位。"""
    return value / QUANTITY_SCALE


def is_zero_quantity(value: Quantity) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return all(float(item) == 0.0 for item in value)
    return float(value) == 0.0


def _validate_scalar(value: float, field_name: str) -> None:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    if number < 0:
        raise ValueError(f"{field_name} must be >= 0")
    if number > MAX_QUANTITY:
        raise ValueError(f"{field_name} must be <= {MAX_QUANTITY:g}")
