"""Unit tests for the health/readiness reporter."""

from __future__ import annotations

from unittest.mock import MagicMock

from pg_mcp.config.settings import DatabaseConfig, OpenAIConfig, Settings
from pg_mcp.observability.health import HealthChecker, HealthStatus


def make_pool(size: int = 5, idle: int = 4, closing: bool = False) -> MagicMock:
    """Build a pool double exposing the asyncpg sizing API."""
    pool = MagicMock()
    pool.get_size.return_value = size
    pool.get_idle_size.return_value = idle
    pool.is_closing.return_value = closing
    return pool


def make_settings(**kwargs: object) -> Settings:
    """Build settings without reading the ambient environment."""
    kwargs.setdefault("openai", OpenAIConfig(api_key="sk-test-offline"))
    return Settings(**kwargs)  # type: ignore[arg-type]


class TestHealthChecker:
    """Health snapshot semantics."""

    def test_healthy_when_every_database_is_reachable(self) -> None:
        settings = make_settings(
            database=DatabaseConfig(name="primary", host="primary.internal"),
            databases=[DatabaseConfig(name="analytics", host="analytics.internal")],
        )
        checker = HealthChecker(
            settings=settings,
            pools={"primary": make_pool(), "analytics": make_pool()},
        )

        report = checker.snapshot()

        assert report.status is HealthStatus.HEALTHY
        assert {db.name for db in report.databases} == {"primary", "analytics"}
        assert all(db.reachable for db in report.databases)
        assert report.databases[0].pool_size == 5
        assert report.databases[0].pool_idle == 4

    def test_degraded_when_one_database_is_missing(self) -> None:
        settings = make_settings(
            databases=[DatabaseConfig(name="analytics", host="analytics.internal")]
        )
        checker = HealthChecker(settings=settings, pools={"postgres": make_pool()})

        report = checker.snapshot()

        assert report.status is HealthStatus.DEGRADED
        assert next(db for db in report.databases if db.name == "analytics").reachable is False

    def test_unhealthy_when_no_database_is_reachable(self) -> None:
        settings = make_settings()
        checker = HealthChecker(settings=settings, pools={})

        report = checker.snapshot()

        assert report.status is HealthStatus.UNHEALTHY

    def test_closing_pool_counts_as_unreachable(self) -> None:
        settings = make_settings()
        checker = HealthChecker(settings=settings, pools={"postgres": make_pool(closing=True)})

        assert checker.snapshot().status is HealthStatus.UNHEALTHY

    def test_degraded_when_circuit_is_open(self) -> None:
        settings = make_settings()
        orchestrator = MagicMock()
        orchestrator.circuit_breaker.state = "open"
        orchestrator.circuit_breaker.failure_count = 7
        orchestrator.health_snapshot.return_value = {
            "circuit_breaker": {"state": "open", "failure_count": 7},
            "rate_limiter": {"queries": {"max_concurrent": 10}, "llm": {"max_concurrent": 5}},
        }
        orchestrator.schema_cache.get_cache_age.return_value = 12.5
        orchestrator.schema_cache.get.return_value = MagicMock()

        checker = HealthChecker(
            settings=settings,
            pools={"postgres": make_pool()},
            orchestrator=orchestrator,
        )

        report = checker.snapshot()

        assert report.status is HealthStatus.DEGRADED
        assert report.circuit_breaker == {"state": "open", "failure_count": 7}
        assert report.databases[0].schema_cached is True
        assert report.databases[0].schema_cache_age_seconds == 12.5

    def test_report_serialises_for_mcp(self) -> None:
        settings = make_settings()
        checker = HealthChecker(settings=settings, pools={"postgres": make_pool()})

        payload = checker.snapshot().to_dict()

        assert payload["status"] == "healthy"
        assert payload["service"] == "pg-mcp"
        assert isinstance(payload["uptime_seconds"], float)
        assert payload["checked_at"].endswith("+00:00")

    def test_pool_errors_do_not_break_the_report(self) -> None:
        settings = make_settings()
        broken = MagicMock()
        broken.get_size.side_effect = RuntimeError("pool is gone")
        broken.is_closing.return_value = False

        report = HealthChecker(settings=settings, pools={"postgres": broken}).snapshot()

        assert report.databases[0].pool_size is None
        assert report.status is HealthStatus.HEALTHY
