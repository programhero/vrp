"""无需鉴权的健康检查与服务信息接口。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health", summary="Service health and version")
def health(request: Request) -> dict:
    settings = request.app.state.settings
    started_at = getattr(request.app.state, "started_at", None)
    uptime = round(time.monotonic() - started_at, 3) if started_at else None
    return {
        "status": "ok",
        "version": settings.version,
        "uptime_seconds": uptime,
    }


@router.get("/", include_in_schema=False)
def root(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "service": settings.app_name,
        "version": settings.version,
        "api": "/api/v1",
        "docs": "/docs",
        "health": "/health",
    }
