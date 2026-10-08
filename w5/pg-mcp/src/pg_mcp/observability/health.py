"""Health and readiness reporting.

The implementation plan (Phase 9) requires a health endpoint on the server but
does not constrain its shape. This module provides a small, testable checker
that the ``health_check`` MCP tool and the offline demo both use.

A report is *healthy* when every configured database has a live connection
pool, *degraded* when the LLM circuit breaker is open (queries can still be
generated for cached SQL but generation is failing fast), and *unhealthy* when
no database is reachable.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from asyncpg import Pool

    from pg_mcp.config.settings import Settings
    from pg_mcp.services.orchestrator import QueryOrchestrator


class HealthStatus(StrEnum):
    """Overall health classification."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DatabaseHealth(BaseModel):
    """Health of a single database target."""

    name: str = Field(..., description="Database name")
    host: str = Field(..., description="Database host")
    reachable: bool = Field(..., description="Whether a connection pool exists and is open")
    pool_size: int | None = Field(None, description="Current number of connections in the pool")
    pool_idle: int | None = Field(None, description="Number of idle connections in the pool")
    schema_cached: bool = Field(False, description="Whether the schema is present in the cache")
    schema_cache_age_seconds: float | None = Field(
        None, description="Age of the cached schema in seconds"
    )


class HealthReport(BaseModel):
    """Structured health report returned by the ``health_check`` tool."""

    status: HealthStatus = Field(..., description="Overall health classification")
    service: str = Field(default="pg-mcp", description="Service name")
    environment: str = Field(..., description="Deployment environment")
    checked_at: str = Field(..., description="ISO-8601 UTC timestamp of the check")
    uptime_seconds: float = Field(..., description="Seconds since the server started")
    metrics_enabled: bool = Field(..., description="Whether Prometheus metrics are enabled")
    databases: list[DatabaseHealth] = Field(
        default_factory=list, description="Per-database health details"
    )
    circuit_breaker: dict[str, Any] | None = Field(
        None, description="LLM circuit breaker state, when available"
    )
    rate_limiter: dict[str, Any] | None = Field(
        None, description="Rate limiter statistics, when available"
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the report for an MCP tool response.

        Returns:
            dict: JSON-compatible representation.
        """
        return self.model_dump()


class HealthChecker:
    """Builds :class:`HealthReport` snapshots from live components.

    Example:
        >>> checker = HealthChecker(settings, pools={"mydb": pool})
        >>> report = checker.snapshot()
        >>> report.status
        <HealthStatus.HEALTHY: 'healthy'>
    """

    def __init__(
        self,
        settings: Settings,
        pools: dict[str, Pool] | None = None,
        orchestrator: QueryOrchestrator | None = None,
        metrics_enabled: bool = False,
        started_at: float | None = None,
    ) -> None:
        """Initialize the health checker.

        Args:
            settings: Application settings.
            pools: Live connection pools keyed by database name.
            orchestrator: Optional orchestrator used for cache/circuit details.
            metrics_enabled: Whether the Prometheus endpoint is serving.
            started_at: Monotonic timestamp of process start.
        """
        self.settings = settings
        self.pools = pools or {}
        self.orchestrator = orchestrator
        self.metrics_enabled = metrics_enabled
        self.started_at = started_at if started_at is not None else time.monotonic()

    def snapshot(self) -> HealthReport:
        """Collect a health report.

        Returns:
            HealthReport: Current health of every configured database plus the
            resilience components.
        """
        databases = [
            self._database_health(db.name, db.host) for db in self.settings.resolved_databases()
        ]

        reachable = [db for db in databases if db.reachable]
        if not reachable:
            status = HealthStatus.UNHEALTHY
        elif len(reachable) < len(databases) or self._circuit_open():
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.HEALTHY

        return HealthReport(
            status=status,
            environment=self.settings.environment,
            checked_at=datetime.now(UTC).isoformat(),
            uptime_seconds=round(time.monotonic() - self.started_at, 3),
            metrics_enabled=self.metrics_enabled,
            databases=databases,
            circuit_breaker=self._circuit_snapshot(),
            rate_limiter=self._rate_limiter_snapshot(),
        )

    def _database_health(self, name: str, host: str) -> DatabaseHealth:
        """Build the health record for one database."""
        pool = self.pools.get(name)
        reachable = pool is not None and not getattr(pool, "is_closing", lambda: False)()

        pool_size: int | None = None
        pool_idle: int | None = None
        if pool is not None:
            pool_size = _safe_call(pool, "get_size")
            pool_idle = _safe_call(pool, "get_idle_size")

        cache_age: float | None = None
        schema_cached = False
        if self.orchestrator is not None:
            cache_age = self.orchestrator.schema_cache.get_cache_age(name)
            schema_cached = self.orchestrator.schema_cache.get(name) is not None

        return DatabaseHealth(
            name=name,
            host=host,
            reachable=reachable,
            pool_size=pool_size,
            pool_idle=pool_idle,
            schema_cached=schema_cached,
            schema_cache_age_seconds=round(cache_age, 3) if cache_age is not None else None,
        )

    def _circuit_open(self) -> bool:
        """Whether the LLM circuit breaker is currently open."""
        if self.orchestrator is None:
            return False
        return str(self.orchestrator.circuit_breaker.state) == "open"

    def _circuit_snapshot(self) -> dict[str, Any] | None:
        """Return circuit breaker details, when an orchestrator is attached."""
        if self.orchestrator is None:
            return None
        snapshot: dict[str, Any] = self.orchestrator.health_snapshot()["circuit_breaker"]
        return snapshot

    def _rate_limiter_snapshot(self) -> dict[str, Any] | None:
        """Return rate limiter statistics, when an orchestrator is attached."""
        if self.orchestrator is None:
            return None
        snapshot: dict[str, Any] | None = self.orchestrator.health_snapshot()["rate_limiter"]
        return snapshot


def _safe_call(obj: Any, method: str) -> int | None:
    """Call ``obj.method()`` and return None if it is unavailable or fails."""
    func = getattr(obj, method, None)
    if func is None:
        return None
    try:
        value = func()
    except Exception:  # pragma: no cover - defensive
        return None
    return int(value) if isinstance(value, int) else None
