"""Unit tests for the Prometheus metrics collector."""

from __future__ import annotations

from pg_mcp.observability.metrics import MetricsCollector


def fresh_metrics() -> MetricsCollector:
    """Return the singleton with an empty registry."""
    collector = MetricsCollector()
    collector.reset_all_metrics()
    return collector


class TestMetricsCollector:
    """Metrics are recorded, rendered and resettable."""

    def test_singleton(self) -> None:
        assert MetricsCollector() is MetricsCollector()

    def test_query_requests_are_labelled(self) -> None:
        metrics = fresh_metrics()
        metrics.increment_query_request(status="success", database="analytics")
        metrics.increment_query_request(status="error", database="analytics")

        rendered = metrics.render_latest().decode()

        assert 'pg_mcp_query_requests_total{database="analytics",status="success"} 1.0' in rendered
        assert 'pg_mcp_query_requests_total{database="analytics",status="error"} 1.0' in rendered

    def test_llm_helpers(self) -> None:
        metrics = fresh_metrics()
        with metrics.time_llm_call("generate"):
            pass
        metrics.increment_llm_tokens("generate", 120)

        rendered = metrics.render_latest().decode()

        assert 'pg_mcp_llm_calls_total{operation="generate"} 1.0' in rendered
        assert 'pg_mcp_llm_tokens_used_total{operation="generate"} 120.0' in rendered
        assert "pg_mcp_llm_latency_seconds_count" in rendered

    def test_tokens_of_zero_are_ignored(self) -> None:
        metrics = fresh_metrics()
        metrics.increment_llm_tokens("generate", 0)

        samples = [
            line
            for line in metrics.render_latest().decode().splitlines()
            if line.startswith("pg_mcp_llm_tokens_used_total{")
        ]
        assert samples == []

    def test_query_duration_context_manager(self) -> None:
        metrics = fresh_metrics()
        with metrics.time_query():
            pass

        assert "pg_mcp_query_duration_seconds_count 1.0" in metrics.render_latest().decode()

    def test_sql_rejections_are_labelled(self) -> None:
        metrics = fresh_metrics()
        metrics.increment_sql_rejected("blocked_table")

        assert (
            'pg_mcp_sql_rejected_total{reason="blocked_table"} 1.0'
            in metrics.render_latest().decode()
        )

    def test_gauges(self) -> None:
        metrics = fresh_metrics()
        metrics.set_db_connections_active("analytics", 4)
        metrics.set_schema_cache_age("analytics", 12.5)

        rendered = metrics.render_latest().decode()

        assert 'pg_mcp_db_connections_active{database="analytics"} 4.0' in rendered
        assert 'pg_mcp_schema_cache_age_seconds{database="analytics"} 12.5' in rendered

    def test_reset_clears_all_series(self) -> None:
        metrics = fresh_metrics()
        metrics.increment_query_request(status="success", database="analytics")
        assert "pg_mcp_query_requests_total{" in metrics.render_latest().decode()

        metrics.reset_all_metrics()

        samples = [
            line
            for line in metrics.render_latest().decode().splitlines()
            if line.startswith("pg_mcp_query_requests_total{")
        ]
        assert samples == []

    def test_reset_is_repeatable(self) -> None:
        """Resetting must never hit a duplicate-timeseries error."""
        metrics = MetricsCollector()
        for _ in range(3):
            metrics.reset_all_metrics()
            metrics.increment_sql_rejected("parse_error")
        assert 'pg_mcp_sql_rejected_total{reason="parse_error"} 1.0' in (
            metrics.render_latest().decode()
        )

    def test_design_doc_attribute_aliases(self) -> None:
        metrics = fresh_metrics()
        assert metrics.query_requests_total is metrics.query_requests
        assert metrics.query_duration_seconds is metrics.query_duration
        assert metrics.llm_calls_total is metrics.llm_calls
        assert metrics.llm_latency_seconds is metrics.llm_latency
        assert metrics.sql_rejected_total is metrics.sql_rejected
        assert metrics.db_query_duration_seconds is metrics.db_query_duration
        assert metrics.schema_cache_age_seconds is metrics.schema_cache_age
