"""从环境变量加载的应用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "OR-Tools VRP Service"
    version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    api_key: str | None = None
    max_locations: int = 1000
    max_vehicles: int = 1000
    max_time_limit_seconds: float = 300.0
    max_jobs: int = 500
    job_ttl_seconds: int = 3600
    job_workers: int = 2

    osrm_base_url: str | None = None
    osrm_profile: str = "driving"
    osrm_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.max_locations < 1:
            raise ValueError("max_locations must be >= 1")
        if self.max_vehicles < 1:
            raise ValueError("max_vehicles must be >= 1")
        if self.max_time_limit_seconds <= 0:
            raise ValueError("max_time_limit_seconds must be > 0")
        if self.max_jobs < 1:
            raise ValueError("max_jobs must be >= 1")
        if self.job_ttl_seconds < 0:
            raise ValueError("job_ttl_seconds must be >= 0")
        if self.job_workers < 1:
            raise ValueError("job_workers must be >= 1")
        if self.osrm_timeout_seconds <= 0:
            raise ValueError("osrm_timeout_seconds must be > 0")

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            app_name=os.getenv("VRP_APP_NAME", defaults.app_name),
            version=defaults.version,
            host=os.getenv("VRP_HOST", defaults.host),
            port=_env_int("VRP_PORT", defaults.port),
            log_level=os.getenv("VRP_LOG_LEVEL", defaults.log_level).upper(),
            api_key=_env_optional("VRP_API_KEY"),
            max_locations=_env_int("VRP_MAX_LOCATIONS", defaults.max_locations),
            max_vehicles=_env_int("VRP_MAX_VEHICLES", defaults.max_vehicles),
            max_time_limit_seconds=_env_float(
                "VRP_MAX_TIME_LIMIT_SECONDS", defaults.max_time_limit_seconds
            ),
            max_jobs=_env_int("VRP_MAX_JOBS", defaults.max_jobs),
            job_ttl_seconds=_env_int("VRP_JOB_TTL_SECONDS", defaults.job_ttl_seconds),
            job_workers=_env_int("VRP_JOB_WORKERS", defaults.job_workers),
            osrm_base_url=_env_optional("VRP_OSRM_BASE_URL"),
            osrm_profile=os.getenv("VRP_OSRM_PROFILE", defaults.osrm_profile),
            osrm_timeout_seconds=_env_float(
                "VRP_OSRM_TIMEOUT_SECONDS", defaults.osrm_timeout_seconds
            ),
        )


def _env_optional(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
