"""Prometheus metrics collector for PostgreSQL MCP Server.

This module implements comprehensive metrics collection using prometheus_client,
tracking query requests, LLM calls, database operations, security rejections and
schema cache freshness.

Each :class:`MetricsCollector` owns a private :class:`~prometheus_client.CollectorRegistry`
so that multiple collectors (for example one per test) never clash, and so that
:meth:`MetricsCollector.reset_all_metrics` can genuinely reset the counters -
which is impossible with the process-global default registry.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class MetricsCollector:
    """Centralized metrics collector using Prometheus client.

    This class provides singleton access to all application metrics,
    implementing the metrics specified in the implementation plan.

    Metrics Categories:
    - Query metrics: Request counts and durations
    - LLM metrics: API calls, latency, and token usage
    - Database metrics: Connection pool and query performance
    - Security metrics: Rejected queries
    - Cache metrics: Schema cache age

    Example:
        >>> metrics = MetricsCollector()
        >>> metrics.increment_query_request(status="success", database="mydb")
        >>> with metrics.time_query():
        ...     pass
    """

    _instance: MetricsCollector | None = None
    _prefix: str = "pg_mcp"
    _registry: CollectorRegistry

    def __new__(cls, prefix: str = "pg_mcp") -> MetricsCollector:
        """Ensure singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._prefix = prefix
            cls._instance._initialize_metrics()
        return cls._instance

    @property
    def registry(self) -> CollectorRegistry:
        """Return the private registry holding this collector's metrics."""
        return self._registry

    def _initialize_metrics(self) -> None:
        """Create (or re-create) every Prometheus metric in a fresh registry.

        Creating a new registry makes :meth:`reset_all_metrics` safe: the old
        collectors are dropped together with the old registry, so no duplicate
        time series are ever registered.
        """
        prefix = getattr(self, "_prefix", "pg_mcp")
        self._registry = CollectorRegistry()

        # Query Metrics
        self.query_requests: Counter = Counter(
            f"{prefix}_query_requests_total",
            "Total number of query requests processed",
            labelnames=["status", "database"],
            registry=self._registry,
        )

        self.query_duration: Histogram = Histogram(
            f"{prefix}_query_duration_seconds",
            "Query request processing duration in seconds",
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
            registry=self._registry,
        )

        # LLM Metrics
        self.llm_calls: Counter = Counter(
            f"{prefix}_llm_calls_total",
            "Total number of LLM API calls",
            labelnames=["operation"],
            registry=self._registry,
        )

        self.llm_latency: Histogram = Histogram(
            f"{prefix}_llm_latency_seconds",
            "LLM API call latency in seconds",
            labelnames=["operation"],
            buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
            registry=self._registry,
        )

        self.llm_tokens_used: Counter = Counter(
            f"{prefix}_llm_tokens_used",
            "Total number of tokens used by LLM",
            labelnames=["operation"],
            registry=self._registry,
        )

        # Security Metrics
        self.sql_rejected: Counter = Counter(
            f"{prefix}_sql_rejected_total",
            "Total number of SQL queries rejected by security checks",
            labelnames=["reason"],
            registry=self._registry,
        )

        # Database Metrics
        self.db_connections_active: Gauge = Gauge(
            f"{prefix}_db_connections_active",
            "Number of active database connections",
            labelnames=["database"],
            registry=self._registry,
        )

        self.db_query_duration: Histogram = Histogram(
            f"{prefix}_db_query_duration_seconds",
            "Database query execution duration in seconds",
            buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
            registry=self._registry,
        )

        # Cache Metrics
        self.schema_cache_age: Gauge = Gauge(
            f"{prefix}_schema_cache_age_seconds",
            "Age of the schema cache in seconds",
            labelnames=["database"],
            registry=self._registry,
        )

    # ------------------------------------------------------------------
    # Server / rendering
    # ------------------------------------------------------------------

    def start_metrics_server(self, port: int, addr: str = "0.0.0.0") -> None:  # noqa: S104
        """Start the Prometheus metrics HTTP server.

        Args:
            port: Port number to listen on for metrics scraping.
            addr: Interface to bind to. Defaults to all interfaces because a
                Prometheus scraper usually runs on another host; set
                ``127.0.0.1`` when the collector is local-only.

        Example:
            >>> metrics = MetricsCollector()
            >>> metrics.start_metrics_server(9090)  # doctest: +SKIP
            # Metrics available at http://localhost:9090/metrics
        """
        start_http_server(port, addr=addr, registry=self._registry)

    def render_latest(self) -> bytes:
        """Render the current metrics in Prometheus text exposition format.

        Returns:
            bytes: Prometheus text payload, as served on ``/metrics``.
        """
        return generate_latest(self._registry)

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def increment_query_request(self, status: str, database: str) -> None:
        """Increment query request counter.

        Args:
            status: Query status (success, error, validation_failed, etc.)
            database: Target database name.
        """
        self.query_requests.labels(status=status, database=database).inc()

    def observe_query_duration(self, duration: float) -> None:
        """Record total query handling duration.

        Args:
            duration: Duration in seconds.
        """
        self.query_duration.observe(duration)

    @contextmanager
    def time_query(self) -> Iterator[None]:
        """Context manager recording query duration in seconds.

        Yields:
            None
        """
        with self.query_duration.time():
            yield

    def increment_llm_call(self, operation: str) -> None:
        """Increment LLM call counter.

        Args:
            operation: Type of LLM operation (generate_sql, validate_result, etc.)
        """
        self.llm_calls.labels(operation=operation).inc()

    def observe_llm_latency(self, operation: str, duration: float) -> None:
        """Record LLM call latency.

        Args:
            operation: Type of LLM operation.
            duration: Duration in seconds.
        """
        self.llm_latency.labels(operation=operation).observe(duration)

    @contextmanager
    def time_llm_call(self, operation: str) -> Iterator[None]:
        """Context manager counting and timing an LLM call.

        Args:
            operation: Type of LLM operation.

        Yields:
            None
        """
        self.increment_llm_call(operation)
        with self.llm_latency.labels(operation=operation).time():
            yield

    def increment_llm_tokens(self, operation: str, tokens: int) -> None:
        """Increment LLM token usage counter.

        Args:
            operation: Type of LLM operation.
            tokens: Number of tokens used.
        """
        if tokens:
            self.llm_tokens_used.labels(operation=operation).inc(tokens)

    def increment_sql_rejected(self, reason: str) -> None:
        """Increment SQL rejection counter.

        Args:
            reason: Reason for rejection (ddl_detected, blocked_function, etc.)
        """
        self.sql_rejected.labels(reason=reason).inc()

    def set_db_connections_active(self, database: str, count: int) -> None:
        """Set active database connection count.

        Args:
            database: Database name.
            count: Number of active connections.
        """
        self.db_connections_active.labels(database=database).set(count)

    def observe_db_query_duration(self, duration: float, database: str | None = None) -> None:
        """Record database query duration.

        Args:
            duration: Duration in seconds.
            database: Optional database name. Accepted so callers can pass the
                target database even though the histogram itself is unlabelled
                (kept label-free for backwards compatibility with dashboards).
        """
        del database  # Label-free histogram by design.
        self.db_query_duration.observe(duration)

    def time_db_query(self, database: str | None = None) -> AbstractContextManager[None]:
        """Return a context manager timing a database query.

        Args:
            database: Optional database name.

        Returns:
            Context manager that observes the elapsed time.
        """
        return self.db_query_duration.time()

    def set_schema_cache_age(self, database: str, age_seconds: float) -> None:
        """Set schema cache age.

        Args:
            database: Database name.
            age_seconds: Cache age in seconds.
        """
        self.schema_cache_age.labels(database=database).set(age_seconds)

    # ------------------------------------------------------------------
    # Design-doc compatible attribute aliases
    # ------------------------------------------------------------------

    @property
    def query_requests_total(self) -> Counter:
        """Alias for :attr:`query_requests`."""
        return self.query_requests

    @property
    def query_duration_seconds(self) -> Histogram:
        """Alias for :attr:`query_duration`."""
        return self.query_duration

    @property
    def llm_calls_total(self) -> Counter:
        """Alias for :attr:`llm_calls`."""
        return self.llm_calls

    @property
    def llm_latency_seconds(self) -> Histogram:
        """Alias for :attr:`llm_latency`."""
        return self.llm_latency

    @property
    def sql_rejected_total(self) -> Counter:
        """Alias for :attr:`sql_rejected`."""
        return self.sql_rejected

    @property
    def db_query_duration_seconds(self) -> Histogram:
        """Alias for :attr:`db_query_duration`."""
        return self.db_query_duration

    @property
    def schema_cache_age_seconds(self) -> Gauge:
        """Alias for :attr:`schema_cache_age`."""
        return self.schema_cache_age

    def reset_all_metrics(self) -> None:
        """Reset all metrics to their initial (empty) state.

        Because every collector owns a private registry, resetting simply means
        building a brand new registry. That is why this method actually works,
        unlike re-creating metrics in the global default registry.
        """
        self._initialize_metrics()


# Singleton instance
metrics = MetricsCollector()
