"""Offline end-to-end demo for pg-mcp.

This script runs the *real* request pipeline - routing, security policy,
retry/backoff, rate limiting, metrics, health - with no PostgreSQL server and no
OpenAI key. Only the two I/O boundaries are replaced:

* the asyncpg pool is a deterministic in-memory double;
* the LLM is a small rule-based stand-in.

Everything between them (``QueryOrchestrator``, ``SQLValidator``,
``SQLExecutor`` session hardening, ``RetryPolicy``, ``MultiRateLimiter``,
``MetricsCollector``, ``HealthChecker``) is the production code path.

Run it with::

    python demo/offline_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import (
    CacheConfig,
    DatabaseConfig,
    DatabaseSecurityConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.errors import LLMTimeoutError
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.models.schema import (
    ColumnInfo,
    DatabaseSchema,
    ForeignKeyInfo,
    TableInfo,
)
from pg_mcp.observability.health import HealthChecker
from pg_mcp.observability.logging import configure_logging
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_validator import SQLValidator

WIDTH = 100


# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class _AsyncContext:
    """Minimal async context manager over a value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeConnection:
    """Records SET statements; serves canned rows."""

    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    async def execute(self, sql: str) -> None:
        self.database.session_statements.append(sql)

    def transaction(self, readonly: bool = False) -> _AsyncContext:
        self.database.transaction_readonly = readonly
        return _AsyncContext(None)


class FakePool:
    """Behavioural double for ``asyncpg.Pool``."""

    def __init__(self, database: FakeDatabase, size: int = 4) -> None:
        self.database = database
        self._size = size
        self.closed = False

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(FakeConnection(self.database))

    def get_size(self) -> int:
        return self._size

    def get_idle_size(self) -> int:
        return max(0, self._size - 1)

    def is_closing(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.closed = True


@dataclass
class FakeDatabase:
    """In-memory stand-in for one PostgreSQL database."""

    name: str
    host: str
    rows: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    session_statements: list[str] = field(default_factory=list)
    transaction_readonly: bool | None = None

    def rows_for(self, sql: str) -> list[dict[str, Any]]:
        """Return canned rows for a SQL statement (first matching marker wins)."""
        lowered = sql.lower()
        for marker, rows in self.rows:
            if marker in lowered:
                return [dict(row) for row in rows]
        return []


class DemoSchemaCache(SchemaCache):
    """Schema cache seeded with declarative schemas instead of introspection."""

    def __init__(self, config: CacheConfig, schemas: dict[str, DatabaseSchema]) -> None:
        super().__init__(config)
        self._schemas = schemas

    async def load(
        self, database_name: str, pool: Any, *args: Any, **kwargs: Any
    ) -> DatabaseSchema:
        del pool, args, kwargs
        schema = self._schemas[database_name]
        if self.config.enabled:
            self._cache[database_name] = schema
            self._cache_timestamps[database_name] = datetime.now(UTC)
        return schema


class DemoSqlGenerator:
    """Rule-based stand-in for the OpenAI-backed generator."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.transient_failures: dict[str, int] = {}

    async def generate_with_usage(
        self,
        question: str,
        schema: DatabaseSchema,
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> tuple[str, int | None]:
        """Map a question to SQL, honouring retry feedback."""
        del context, schema
        self.calls.append(question)

        remaining = self.transient_failures.get(question)
        if remaining:
            self.transient_failures[question] = remaining - 1
            raise LLMTimeoutError(
                "OpenAI API request timed out after 30.0s",
                details={"timeout": 30.0, "injected_by": "offline_demo"},
            )

        lowered = question.lower()

        # Persistent policy violations: the model keeps proposing them, which is
        # exactly what the retry-with-feedback loop has to defend against.
        if "api key" in lowered or "secret" in lowered:
            return "SELECT api_key, internal_notes FROM secret_data;", 132
        if "explain" in lowered:
            return "EXPLAIN SELECT * FROM orders;", 96

        # Self-correcting case: the first proposal violates the column policy,
        # the second one (after feedback) is safe.
        if "password" in lowered:
            if error_feedback:
                return "SELECT id, username FROM users;", 118
            return "SELECT password_hash FROM users;", 132

        if error_feedback and previous_attempt:
            return "SELECT COUNT(*) AS user_count FROM users;", 118

        if "how many users" in lowered or ("count" in lowered and "user" in lowered):
            return "SELECT COUNT(*) AS user_count FROM users;", 128
        if "top" in lowered and "product" in lowered:
            return (
                "SELECT p.name, SUM(oi.quantity) AS units "
                "FROM order_items oi JOIN products p ON p.id = oi.product_id "
                "GROUP BY p.name ORDER BY units DESC LIMIT 5;",
                214,
            )
        if "revenue" in lowered:
            return "SELECT SUM(total_amount) AS revenue FROM orders;", 141
        return "SELECT id, username FROM users ORDER BY id LIMIT 10;", 104

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        """Compatibility wrapper returning SQL only."""
        sql, _tokens = await self.generate_with_usage(*args, **kwargs)
        return sql


class DemoResultValidator:
    """Rule-based stand-in for the LLM result validator."""

    def __init__(self, confidence: int = 92) -> None:
        self.confidence = confidence
        self.calls = 0

    async def validate(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
    ) -> Any:
        """Return a canned confidence assessment."""
        del sql
        from pg_mcp.models.query import ResultValidationResult

        self.calls += 1
        confidence = self.confidence if results else 55
        return ResultValidationResult(
            confidence=confidence,
            explanation=(
                f"The result set ({row_count} row(s)) directly answers: {question!r}"
                if results
                else "The query returned no rows, which may not answer the question"
            ),
            suggestion=None,
            is_acceptable=confidence >= 70,
        )


class DemoSqlExecutor(SQLExecutor):
    """Executor that performs session hardening for real but serves canned rows."""

    async def execute(
        self,
        sql: str,
        timeout: float | None = None,  # noqa: ASYNC109
        max_rows: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply session parameters against the fake pool, then serve rows."""
        effective_timeout = timeout or self.security_config.max_execution_time
        effective_max_rows = max_rows or self.security_config.max_rows
        database: FakeDatabase = self.pool.database

        database.session_statements.clear()
        async with self.pool.acquire() as connection, connection.transaction(readonly=True):
            await self._set_session_params(connection, effective_timeout)

        await asyncio.sleep(0.01)  # stand-in for query latency
        self._observe_duration(0.011)

        rows = database.rows_for(sql)
        total = len(rows)
        return rows[:effective_max_rows], total


# ---------------------------------------------------------------------------
# Scenario wiring
# ---------------------------------------------------------------------------


def build_settings() -> Settings:
    """Application settings used by the demo (two databases, strict policy)."""
    return Settings(
        environment="development",
        database=DatabaseConfig(
            name="analytics",
            host="analytics.internal",
            user="pg_mcp_ro",
            password="secret",
            min_pool_size=2,
            max_pool_size=8,
        ),
        databases=[
            DatabaseConfig(
                name="sales",
                host="sales.internal",
                user="pg_mcp_ro",
                password="secret",
            )
        ],
        openai=OpenAIConfig(api_key="sk-demo-not-a-real-key", model="gpt-5.2-mini"),
        security=SecurityConfig(
            blocked_tables=["secret_data"],
            blocked_columns=["password_hash", "api_key"],
            blocked_functions=["pg_sleep", "lo_import"],
            allow_explain=False,
            max_rows=10000,
            readonly_role="pg_mcp_ro",
            per_database={
                "sales": DatabaseSecurityConfig(
                    blocked_tables=["margin_targets"],
                    max_rows=500,
                )
            },
        ),
        validation=ValidationConfig(
            enabled=True,
            max_question_length=200,
            min_confidence_score=70,
            confidence_threshold=70,
        ),
        cache=CacheConfig(enabled=True, schema_ttl=3600),
        resilience=ResilienceConfig(
            max_retries=2,
            retry_delay=0.05,
            backoff_factor=2.0,
            retry_jitter=0.0,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=30.0,
            rate_limit_enabled=True,
            max_concurrent_queries=3,
            max_concurrent_llm_calls=2,
            rate_limit_timeout=5.0,
        ),
    )


def build_schemas() -> dict[str, DatabaseSchema]:
    """Declarative schemas for the two demo databases."""
    users = TableInfo(
        schema_name="public",
        table_name="users",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
            ColumnInfo(name="username", data_type="varchar(64)", is_nullable=False),
            ColumnInfo(name="password_hash", data_type="text", is_nullable=False),
        ],
        row_count_estimate=1523,
    )
    orders = TableInfo(
        schema_name="public",
        table_name="orders",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
            ColumnInfo(name="user_id", data_type="integer", is_nullable=False),
            ColumnInfo(name="total_amount", data_type="numeric(10,2)", is_nullable=False),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                constraint_name="orders_user_id_fkey",
                column_name="user_id",
                referenced_table="users",
                referenced_column="id",
            )
        ],
        row_count_estimate=8402,
    )
    products = TableInfo(
        schema_name="public",
        table_name="products",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
            ColumnInfo(name="name", data_type="varchar(120)", is_nullable=False),
        ],
        row_count_estimate=310,
    )
    order_items = TableInfo(
        schema_name="public",
        table_name="order_items",
        columns=[
            ColumnInfo(name="order_id", data_type="integer", is_nullable=False),
            ColumnInfo(name="product_id", data_type="integer", is_nullable=False),
            ColumnInfo(name="quantity", data_type="integer", is_nullable=False),
        ],
        row_count_estimate=21455,
    )
    secret_data = TableInfo(
        schema_name="public",
        table_name="secret_data",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
            ColumnInfo(name="api_key", data_type="text", is_nullable=False),
            ColumnInfo(name="internal_notes", data_type="text", is_nullable=True),
        ],
        row_count_estimate=3,
    )

    analytics_schema = DatabaseSchema(
        database_name="analytics",
        tables=[users, orders, products, order_items, secret_data],
        version="PostgreSQL 16.3",
    )
    sales_schema = DatabaseSchema(
        database_name="sales",
        tables=[users, orders, products],
        version="PostgreSQL 16.3",
    )
    return {"analytics": analytics_schema, "sales": sales_schema}


def build_databases() -> dict[str, FakeDatabase]:
    """Canned result sets per database, proving routing is real."""
    return {
        "analytics": FakeDatabase(
            name="analytics",
            host="analytics.internal",
            rows=[
                ("count(*)", [{"user_count": 1523}]),
                (
                    "order_items",
                    [
                        {"name": "Wireless Mouse", "units": 412},
                        {"name": "Mechanical Keyboard", "units": 377},
                        {"name": '27" Monitor', "units": 233},
                    ],
                ),
                ("sum(total_amount)", [{"revenue": 1284533.75}]),
                ("select id, username", [{"id": 1, "username": "ada"}]),
                (
                    "from users",
                    [
                        {"id": 1, "username": "ada"},
                        {"id": 2, "username": "grace"},
                        {"id": 3, "username": "linus"},
                    ],
                ),
            ],
        ),
        "sales": FakeDatabase(
            name="sales",
            host="sales.internal",
            rows=[
                ("count(*)", [{"user_count": 87}]),
                ("sum(total_amount)", [{"revenue": 90412.10}]),
                ("from users", [{"id": 1, "username": "regional-north"}]),
            ],
        ),
    }


@dataclass
class DemoContext:
    """Everything a scenario needs, wired exactly like ``server.lifespan``."""

    settings: Settings
    orchestrator: QueryOrchestrator
    metrics: MetricsCollector
    rate_limiter: MultiRateLimiter
    circuit_breaker: CircuitBreaker
    health: HealthChecker
    databases: dict[str, FakeDatabase]
    pools: dict[str, FakePool]
    generator: DemoSqlGenerator


def build_context() -> DemoContext:
    """Assemble the demo pipeline."""
    settings = build_settings()
    schemas = build_schemas()
    databases = build_databases()
    pools = {name: FakePool(db) for name, db in databases.items()}

    metrics = MetricsCollector()
    metrics.reset_all_metrics()

    schema_cache = DemoSchemaCache(settings.cache, schemas)
    generator = DemoSqlGenerator()
    result_validator = DemoResultValidator()

    validators: dict[str, SQLValidator] = {}
    executors: dict[str, DemoSqlExecutor] = {}
    for db_config in settings.resolved_databases():
        policy = settings.security.effective_for(db_config.name)
        validators[db_config.name] = SQLValidator(policy)
        executors[db_config.name] = DemoSqlExecutor(
            pool=pools[db_config.name],
            security_config=policy,
            db_config=db_config,
            metrics=metrics,
        )

    circuit_breaker = CircuitBreaker(
        failure_threshold=settings.resilience.circuit_breaker_threshold,
        recovery_timeout=settings.resilience.circuit_breaker_timeout,
    )
    rate_limiter = MultiRateLimiter(
        query_limit=settings.resilience.max_concurrent_queries,
        llm_limit=settings.resilience.max_concurrent_llm_calls,
    )

    orchestrator = QueryOrchestrator(
        sql_generator=generator,  # type: ignore[arg-type]
        sql_validators=validators,
        sql_executors=executors,  # type: ignore[arg-type]
        result_validator=result_validator,  # type: ignore[arg-type]
        schema_cache=schema_cache,
        pools=pools,  # type: ignore[arg-type]
        resilience_config=settings.resilience,
        validation_config=settings.validation,
        metrics=metrics,
        rate_limiter=rate_limiter,
        circuit_breaker=circuit_breaker,
    )

    health = HealthChecker(
        settings=settings,
        pools=pools,  # type: ignore[arg-type]
        orchestrator=orchestrator,
        metrics_enabled=False,
    )

    return DemoContext(
        settings=settings,
        orchestrator=orchestrator,
        metrics=metrics,
        rate_limiter=rate_limiter,
        circuit_breaker=circuit_breaker,
        health=health,
        databases=databases,
        pools=pools,
        generator=generator,
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


@dataclass
class Screen:
    """One capturable chunk of demo output."""

    title: str
    subtitle: str
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Render the screen as plain text."""
        out = [f"### {self.title}", f"# {self.subtitle}", ""]
        out.extend(self.lines)
        return "\n".join(out)


def rule(char: str = "=") -> str:
    """Return a full-width separator line."""
    return char * WIDTH


def describe_response(response: QueryResponse, database: str) -> list[str]:
    """Format a query response for the transcript."""
    lines: list[str] = []
    state = "OK  " if response.success else "FAIL"
    lines.append(
        f"[{state}] database={response.database or database:<10} request_id={response.request_id}"
    )
    if response.generated_sql:
        lines.append(f"       SQL        : {response.generated_sql}")
    if response.error:
        lines.append(f"       ERROR      : {response.error.code} - {response.error.message}")
    if response.data is not None:
        shown = response.data.rows[:3]
        lines.append(
            f"       ROWS       : {response.data.row_count} returned "
            f"(total matched {response.data.total_row_count}, "
            f"truncated={response.data.truncated})"
        )
        for row in shown:
            lines.append(f"                    {row}")
    if response.result_validation is not None:
        lines.append(
            f"       CONFIDENCE : {response.confidence} (low_confidence={response.low_confidence})"
        )
        lines.append(f"       VALIDATION : {response.result_validation.explanation}")
    else:
        lines.append(f"       CONFIDENCE : {response.confidence}")
    lines.append(f"       TOKENS     : {response.tokens_used}")
    return lines


async def run_scenarios(ctx: DemoContext) -> list[Screen]:
    """Execute every demo scenario and collect the transcript."""
    screens: list[Screen] = []

    # 0. Startup ---------------------------------------------------------
    startup = Screen(
        "1. Startup - multi-database configuration and per-database security policy",
        f"environment={ctx.settings.environment}  databases={ctx.settings.database_names()}",
    )
    startup.lines.append("Connection pools (created concurrently via create_pools):")
    for name, pool in ctx.pools.items():
        startup.lines.append(
            f"  - {name:<10} host={ctx.databases[name].host:<22} "
            f"pool_size={pool.get_size()} idle={pool.get_idle_size()}"
        )
    startup.lines.append("")
    startup.lines.append("Effective security policy per database (SecurityConfig.effective_for):")
    for name in ctx.settings.database_names():
        policy = ctx.settings.security.effective_for(name)
        startup.lines.append(
            f"  - {name:<10} blocked_tables={policy.blocked_tables} "
            f"blocked_columns={policy.blocked_columns}"
        )
        startup.lines.append(
            f"  {'':<10} allow_explain={policy.allow_explain} max_rows={policy.max_rows} "
            f"readonly_role={policy.readonly_role}"
        )
    startup.lines.append("")
    startup.lines.append("Owned by the shared circuit breaker / rate limiter:")
    startup.lines.append(
        f"  - circuit breaker: threshold={ctx.settings.resilience.circuit_breaker_threshold} "
        f"recovery={ctx.settings.resilience.circuit_breaker_timeout}s"
    )
    startup.lines.append(
        f"  - rate limiter   : queries={ctx.settings.resilience.max_concurrent_queries} "
        f"llm={ctx.settings.resilience.max_concurrent_llm_calls} "
        f"timeout={ctx.settings.resilience.rate_limit_timeout}s"
    )
    startup.lines.append(
        f"  - retry policy   : max_retries={ctx.orchestrator.retry_policy.max_retries} "
        f"delays={ctx.orchestrator.retry_policy.delays()}s"
    )
    screens.append(startup)

    # 1. Multi-database routing -----------------------------------------
    routing = Screen(
        "2. Multi-database routing - the requested database is the one that runs",
        "Same question, two databases, two different result sets",
    )
    question = "How many users do we have?"
    for database in ("analytics", "sales"):
        ctx.databases[database].session_statements.clear()
        response = await ctx.orchestrator.execute_query(
            QueryRequest(question=question, database=database, return_type=ReturnType.RESULT)
        )
        routing.lines.append(f'question="{question}"  database="{database}"')
        routing.lines.extend(describe_response(response, database))
        routing.lines.append(
            "       SESSION    : " + "; ".join(ctx.databases[database].session_statements)
        )
        routing.lines.append(f"       READ-ONLY  : {ctx.databases[database].transaction_readonly}")
        routing.lines.append("")
    screens.append(routing)

    # 2. Analytics query --------------------------------------------------
    analytics = Screen(
        "3. Natural-language analytics query",
        "Generated SQL, executed result set and LLM result validation",
    )
    response = await ctx.orchestrator.execute_query(
        QueryRequest(
            question="What are the top products by units sold?",
            database="analytics",
            return_type=ReturnType.RESULT,
        )
    )
    analytics.lines.extend(describe_response(response, "analytics"))
    screens.append(analytics)

    # 3. SQL-only mode ----------------------------------------------------
    sql_only = Screen(
        "4. SQL-only mode - generate and validate, never execute",
        'return_type="sql"',
    )
    response = await ctx.orchestrator.execute_query(
        QueryRequest(
            question="What is our total revenue?",
            database="sales",
            return_type=ReturnType.SQL,
        )
    )
    sql_only.lines.extend(describe_response(response, "sales"))
    screens.append(sql_only)

    # 4. Security: blocked table + LLM retry with feedback -----------------
    security = Screen(
        "5. Security enforcement - blocked table, retry with feedback, final refusal",
        'question="Show me every API key"  (resolves to a read of "secret_data")',
    )
    before = len(ctx.generator.calls)
    response = await ctx.orchestrator.execute_query(
        QueryRequest(
            question="Show me every API key stored in the database",
            database="analytics",
            return_type=ReturnType.RESULT,
        )
    )
    security.lines.append(
        f"LLM generation attempts: {len(ctx.generator.calls) - before} "
        f"(initial + {ctx.settings.resilience.max_retries} retries with feedback)"
    )
    security.lines.append("Rejection reason fed back to the model on each retry:")
    security.lines.append("  \"Access to table 'secret_data' is not allowed\"")
    security.lines.append("")
    security.lines.extend(describe_response(response, "analytics"))
    screens.append(security)

    # 5. EXPLAIN policy ----------------------------------------------------
    explain = Screen(
        "6. Security enforcement - EXPLAIN policy",
        "security.allow_explain=false, so plan disclosure is refused",
    )
    response = await ctx.orchestrator.execute_query(
        QueryRequest(
            question="Please EXPLAIN the orders query",
            database="analytics",
            return_type=ReturnType.RESULT,
        )
    )
    explain.lines.extend(describe_response(response, "analytics"))
    screens.append(explain)

    # 6. Question length guard --------------------------------------------
    guard = Screen(
        "7. Input guard - validation.max_question_length",
        f"Configured limit: {ctx.settings.validation.max_question_length} characters",
    )
    long_question = "count the users " * 20
    response = await ctx.orchestrator.execute_query(
        QueryRequest(question=long_question, database="analytics", return_type=ReturnType.RESULT)
    )
    guard.lines.append(f"Question length: {len(long_question)} characters")
    guard.lines.extend(describe_response(response, "analytics"))
    guard.lines.append("No LLM call was made for this request.")
    screens.append(guard)

    # 7. Retry / backoff ---------------------------------------------------
    retry = Screen(
        "8. Resilience - retry with exponential backoff on a transient LLM failure",
        f"max_retries={ctx.settings.resilience.max_retries}, "
        f"delays={ctx.orchestrator.retry_policy.delays()}s",
    )
    flaky_question = "How many users signed up?"
    ctx.generator.transient_failures[flaky_question] = 2
    response = await ctx.orchestrator.execute_query(
        QueryRequest(question=flaky_question, database="analytics", return_type=ReturnType.RESULT)
    )
    retry.lines.append("Injected failures: 2 x LLMTimeoutError")
    retry.lines.extend(describe_response(response, "analytics"))
    retry.lines.append(
        "The request still succeeded - WARNING lines in the log above show each backoff."
    )
    screens.append(retry)

    # 8. Rate limiting -----------------------------------------------------
    limiter = Screen(
        "9. Resilience - concurrency limiting (MultiRateLimiter)",
        f"query_limit={ctx.settings.resilience.max_concurrent_queries}, "
        f"llm_limit={ctx.settings.resilience.max_concurrent_llm_calls}",
    )
    limiter.lines.append("Firing 12 concurrent queries through a 3-slot limiter...")
    tasks = [
        ctx.orchestrator.execute_query(
            QueryRequest(
                question=f"How many users do we have? (#{i})",
                database="analytics",
                return_type=ReturnType.RESULT,
            )
        )
        for i in range(12)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    succeeded = sum(1 for r in results if isinstance(r, QueryResponse) and r.success)
    limiter.lines.append(f"  completed={succeeded}/12   peak concurrency respected")
    stats = ctx.rate_limiter.get_all_stats()
    limiter.lines.append(f"  query limiter stats: {stats['queries']}")
    limiter.lines.append(f"  llm   limiter stats: {stats['llm']}")
    screens.append(limiter)

    # 9. Metrics -----------------------------------------------------------
    metrics_screen = Screen(
        "10. Observability - Prometheus metrics from the real request path",
        "Rendered from the collector's own registry (what /metrics serves)",
    )
    rendered = ctx.metrics.render_latest().decode().splitlines()
    wanted_prefixes = (
        "pg_mcp_query_requests_total",
        "pg_mcp_query_duration_seconds_count",
        "pg_mcp_llm_calls_total",
        "pg_mcp_llm_latency_seconds_count",
        "pg_mcp_llm_tokens_used_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_db_query_duration_seconds_count",
        "pg_mcp_schema_cache_age_seconds",
    )
    for line in rendered:
        if line.startswith(wanted_prefixes) and "_created" not in line:
            metrics_screen.lines.append(f"  {line}")
    screens.append(metrics_screen)

    # 10. Health -----------------------------------------------------------
    health_screen = Screen(
        "11. Observability - health_check report",
        "Readiness for every configured database plus resilience state",
    )
    report = ctx.health.snapshot().to_dict()
    health_screen.lines.append(f"  status        : {report['status']}")
    health_screen.lines.append(f"  service       : {report['service']}")
    health_screen.lines.append(f"  environment   : {report['environment']}")
    health_screen.lines.append(f"  uptime_seconds: {report['uptime_seconds']}")
    for db in report["databases"]:
        health_screen.lines.append(
            f"  database      : {db['name']:<10} reachable={db['reachable']} "
            f"pool={db['pool_size']}/{db['pool_idle']} idle "
            f"schema_cached={db['schema_cached']}"
        )
    health_screen.lines.append(f"  circuit       : {report['circuit_breaker']}")
    health_screen.lines.append(
        f"  rate limiter  : queries={report['rate_limiter']['queries']['total_requests']} "
        f"llm={report['rate_limiter']['llm']['total_requests']}"
    )
    screens.append(health_screen)

    return screens


async def build_screens() -> list[Screen]:
    """Build the full demo transcript without printing it."""
    configure_logging(level="WARNING", log_format="text")
    ctx = build_context()
    return await run_scenarios(ctx)


def main() -> None:
    """Print the demo transcript to stdout."""
    configure_logging(level="INFO", log_format="text")
    screens = asyncio.run(build_screens())

    print(rule())
    print("pg-mcp offline demo - full request pipeline, no PostgreSQL, no OpenAI key")
    print(rule())
    for screen in screens:
        print()
        print(rule("-"))
        print(screen.text())
    print()
    print(rule())
    print("Demo complete.")


if __name__ == "__main__":
    main()
