"""业务异常，映射为结构化 HTTP 错误响应。"""

from __future__ import annotations

from typing import Any


class VRPError(Exception):
    """可安全暴露给 API 客户端的错误基类。"""

    status_code: int = 400
    code: str = "vrp_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class InvalidProblemError(VRPError):
    status_code = 422
    code = "invalid_problem"


class MatrixError(VRPError):
    status_code = 422
    code = "invalid_matrix"


class MatrixProviderError(VRPError):
    status_code = 502
    code = "matrix_provider_error"


class NotFoundError(VRPError):
    status_code = 404
    code = "not_found"


class UnauthorizedError(VRPError):
    status_code = 401
    code = "unauthorized"


class JobCapacityError(VRPError):
    status_code = 429
    code = "job_capacity_reached"
