"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components.

Everything the design promised is wired up here:

* one connection pool, security policy, validator and executor **per configured
  database**, so ``database`` in a request is genuinely honoured and each
  database can carry its own restrictions;
* a single shared circuit breaker and a concurrency limiter built from
  ``ResilienceConfig`` and handed to the orchestrator;
* a Prometheus metrics collector and request-id tracing that reach the actual
  request path;
* a ``health_check`` tool.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.health import HealthChecker
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import generate_request_id, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from asyncpg import Pool

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None
_health_checker: HealthChecker | None = None
_started_at: float = time.monotonic()


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Lifespan context manager for server initialization and cleanup.

    Startup:
        1. Load configuration from Settings
        2. Configure logging (with tracing context propagation)
        3. Create one connection pool per configured database
        4. Load the schema cache for every database
        5. Initialize the metrics collector and start the metrics HTTP server
        6. Build a per-database security policy, validator and executor
        7. Initialize the shared circuit breaker and rate limiter
        8. Build the query orchestrator and health checker

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools
        3. Stop the metrics HTTP server (if running)

    Yields:
        None

    Example:
        >>> async with lifespan(mcp):
        ...     # Server is running with all components initialized
        ...     pass
    """
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter, _health_checker, _started_at

    _started_at = time.monotonic()
    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        databases = _settings.resolved_databases()
        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
                "databases": [db.name for db in databases],
            },
        )

        # 3. Create database connection pools (one per database, concurrently)
        logger.info("Creating database connection pools...")
        _pools = await create_pools(databases)
        for db_config in databases:
            logger.info(
                f"Created connection pool for database '{db_config.name}'",
                extra={
                    "database": db_config.name,
                    "host": db_config.host,
                    "min_size": db_config.min_pool_size,
                    "max_size": db_config.max_pool_size,
                },
            )

        # 4. Initialize the metrics collector before anything else so service
        #    components can report into it.
        _metrics = MetricsCollector()
        if _settings.observability.metrics_enabled:
            _metrics.start_metrics_server(_settings.observability.metrics_port)
            logger.info(f"Metrics server started on port {_settings.observability.metrics_port}")
        for db_config in databases:
            pool = _pools[db_config.name]
            _metrics.set_db_connections_active(db_config.name, _pool_size(pool))

        # 5. Load the schema cache for every database
        logger.info("Initializing schema cache...")
        _schema_cache = SchemaCache(_settings.cache)
        for db_config in databases:
            try:
                schema = await _schema_cache.load(db_config.name, _pools[db_config.name])
                logger.info(
                    f"Schema loaded for '{db_config.name}'",
                    extra={"database": db_config.name, "tables": len(schema.tables)},
                )
            except Exception as e:
                # A single unreachable database must not take the whole server
                # down: it will be reported as degraded by `health_check` and
                # retried lazily on the first query.
                logger.warning(
                    f"Schema preload failed for '{db_config.name}': {e!s}",
                    extra={"database": db_config.name, "error_type": type(e).__name__},
                )

        # 6. Service components
        logger.info("Initializing service components...")
        sql_generator = SQLGenerator(_settings.openai)

        validators: dict[str, SQLValidator] = {}
        executors: dict[str, SQLExecutor] = {}
        for db_config in databases:
            security = _settings.security.effective_for(db_config.name)
            validators[db_config.name] = SQLValidator(security)
            executors[db_config.name] = SQLExecutor(
                pool=_pools[db_config.name],
                security_config=security,
                db_config=db_config,
                metrics=_metrics,
            )
            logger.info(
                f"Configured security policy for database '{db_config.name}'",
                extra={
                    "database": db_config.name,
                    "blocked_tables": security.blocked_tables,
                    "blocked_columns": security.blocked_columns,
                    "allow_explain": security.allow_explain,
                    "readonly_role": security.readonly_role,
                },
            )

        result_validator = ResultValidator(
            openai_config=_settings.openai,
            validation_config=_settings.validation,
        )

        # 7. Resilience components (single shared instances)
        logger.info("Initializing resilience components...")
        _circuit_breaker = CircuitBreaker(
            failure_threshold=_settings.resilience.circuit_breaker_threshold,
            recovery_timeout=_settings.resilience.circuit_breaker_timeout,
        )
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.max_concurrent_queries,
            llm_limit=_settings.resilience.max_concurrent_llm_calls,
        )

        # 8. Query orchestrator
        logger.info("Creating query orchestrator...")
        _orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validators=validators,
            sql_executors=executors,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            metrics=_metrics,
            rate_limiter=_rate_limiter,
            circuit_breaker=_circuit_breaker,
        )

        _health_checker = HealthChecker(
            settings=_settings,
            pools=_pools,
            orchestrator=_orchestrator,
            metrics_enabled=_settings.observability.metrics_enabled,
            started_at=_started_at,
        )

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": list(_pools.keys()),
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
                "rate_limit_enabled": _settings.resilience.rate_limit_enabled,
            },
        )

        yield

    finally:
        logger.info("Starting PostgreSQL MCP Server shutdown...")

        if _schema_cache is not None:
            try:
                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
                logger.info("Schema auto-refresh stopped")
            except TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        if _pools is not None:
            try:
                await close_pools(_pools, timeout=5.0)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")

        _orchestrator = None
        _health_checker = None
        logger.info("PostgreSQL MCP Server shutdown complete")


def _pool_size(pool: Pool) -> int:
    """Return the current size of a pool, tolerating test doubles.

    Args:
        pool: asyncpg pool (or a mock).

    Returns:
        int: Number of connections currently held, or 0 when unknown.
    """
    get_size = getattr(pool, "get_size", None)
    if get_size is None:
        return 0
    try:
        return int(get_size())
    except Exception:  # pragma: no cover - defensive
        return 0


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against a PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected. Use `list_databases` to discover the names.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - request_id (str): Identifier for end-to-end tracing
            - database (str): Database the query actually ran against
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - result_validation (dict): LLM confidence assessment, when enabled
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - low_confidence (bool): True when below the configured minimum
            - tokens_used (int): Number of LLM tokens consumed

    Examples:
        >>> result = await query("How many active users are there?")
        >>> print(result["data"]["rows"])

        >>> result = await query("Count all products", return_type="sql")
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed unless writes are explicitly enabled
        - Blocked tables/columns/functions are enforced per database
        - EXPLAIN is denied unless `security.allow_explain` is enabled
        - Question length is capped by `validation.max_question_length`
        - Query execution timeout and row limits are enforced
        - Queries run in read-only transactions by default
    """
    if _orchestrator is None:
        return {
            "success": False,
            "request_id": generate_request_id(),
            "error": {
                "code": "database_connection_error",
                "message": "Server not initialized properly",
                "details": None,
            },
            "tokens_used": 0,
        }

    # Validate return_type before doing any work
    if return_type not in ("sql", "result"):
        return {
            "success": False,
            "request_id": generate_request_id(),
            "error": {
                "code": "invalid_request",
                "message": f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
                "details": {"return_type": return_type},
            },
            "tokens_used": 0,
        }

    # Build the validated request model
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return {
            "success": False,
            "request_id": generate_request_id(),
            "error": {
                "code": "invalid_request",
                "message": f"Invalid request parameters: {e!s}",
                "details": {"error": str(e)},
            },
            "tokens_used": 0,
        }

    # Execute through the orchestrator inside a traced request context
    async with request_context(generate_request_id()) as request_id:
        try:
            response: QueryResponse = await _orchestrator.execute_query(request)
            result = response.to_dict()
            if not result.get("request_id"):
                result["request_id"] = request_id
            return result
        except Exception as e:
            logger.exception("Unexpected error in query tool")
            return {
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "internal_error",
                    "message": f"Internal server error: {e!s}",
                    "details": {"error_type": type(e).__name__},
                },
                "tokens_used": 0,
            }


@mcp.tool()
async def list_databases() -> dict[str, Any]:
    """List the databases this server can query.

    Returns:
        dict: ``success`` plus ``databases`` (names) and ``default`` (the name
        used when a request omits the ``database`` argument, or None when the
        name must be supplied explicitly).

    Example:
        >>> result = await list_databases()
        >>> result["databases"]
        ['analytics', 'sales']
    """
    if _settings is None:
        return {
            "success": False,
            "databases": [],
            "default": None,
            "error": {
                "code": "database_connection_error",
                "message": "Server not initialized properly",
                "details": None,
            },
        }

    names = _settings.database_names()
    return {
        "success": True,
        "databases": names,
        "default": names[0] if len(names) == 1 else None,
    }


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Report server readiness, database connectivity and resilience state.

    Returns:
        dict: A health report containing:
            - status: "healthy", "degraded" or "unhealthy"
            - uptime_seconds: Seconds since the server started
            - databases: Per-database pool and schema-cache details
            - circuit_breaker: LLM circuit breaker state
            - rate_limiter: Concurrency limiter statistics

    Example:
        >>> report = await health_check()
        >>> report["status"]
        'healthy'
    """
    if _health_checker is None:
        return {
            "status": "unhealthy",
            "service": "pg-mcp",
            "error": {
                "code": "database_connection_error",
                "message": "Server not initialized properly",
                "details": None,
            },
        }
    return _health_checker.snapshot().to_dict()


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)
