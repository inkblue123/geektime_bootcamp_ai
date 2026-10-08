"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline,
including per-database routing, retry logic, error handling, resilience
integration and metrics emission.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMError,
    SecurityViolationError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator

VALID_RESULT = ValidationResult(
    is_valid=True,
    is_select=True,
    allows_data_modification=False,
    uses_blocked_functions=[],
    error_message=None,
)

VERSION_15 = "15.0"


def sample_schema(database_name: str = "test_db") -> DatabaseSchema:
    """Build a small schema for orchestrator tests."""
    return DatabaseSchema(
        database_name=database_name,
        tables=[
            TableInfo(
                schema_name="public",
                table_name="users",
                columns=[
                    ColumnInfo(
                        name="id",
                        data_type="integer",
                        is_nullable=False,
                        is_primary_key=True,
                    ),
                    ColumnInfo(name="name", data_type="varchar(255)", is_nullable=False),
                ],
            )
        ],
        version=VERSION_15,
    )


def make_validator(analysis: ValidationResult | None = None) -> MagicMock:
    """Build a validator stub whose ``analyze()`` reports a valid SELECT."""
    validator = MagicMock()
    validator.analyze.return_value = analysis if analysis is not None else VALID_RESULT
    return validator


def rejected_validator(
    message: str = "DELETE statements are not allowed",
    error_code: str = "security_violation",
    modifies_data: bool = True,
) -> MagicMock:
    """Build a validator stub that rejects every statement."""
    return make_validator(
        ValidationResult(
            is_valid=False,
            is_select=False,
            allows_data_modification=modifies_data,
            uses_blocked_functions=[],
            error_message=message,
            error_code=error_code,
        )
    )


def make_generator(sql: str = "SELECT * FROM users;", tokens: int | None = 42) -> AsyncMock:
    """Build a SQL generator stub returning ``sql`` via ``generate_with_usage``."""
    generator = AsyncMock()
    generator.generate_with_usage.return_value = (sql, tokens)
    generator.generate.return_value = sql
    return generator


def make_validation_result(confidence: int = 85) -> ResultValidationResult:
    """Build a result-validation payload with the given confidence."""
    return ResultValidationResult(
        confidence=confidence,
        explanation="Results match the question well",
        suggestion=None,
        is_acceptable=confidence >= 70,
    )


def make_orchestrator(
    pools: dict[str, Any] | None = None,
    *,
    generator: AsyncMock | None = None,
    validators: dict[str, Any] | None = None,
    executors: dict[str, Any] | None = None,
    result_validator: Any | None = None,
    schema_cache: Any | None = None,
    resilience: ResilienceConfig | None = None,
    validation: ValidationConfig | None = None,
    metrics: MetricsCollector | None = None,
    rate_limiter: MultiRateLimiter | None = None,
) -> QueryOrchestrator:
    """Construct an orchestrator with stubbed collaborators.

    Pool names drive the validator/executor key space so that multi-database
    routing can be exercised without extra boilerplate.
    """
    resolved_pools = {"test_db": MagicMock()} if pools is None else pools
    names = list(resolved_pools.keys()) or ["test_db"]

    resolved_validators = (
        validators if validators is not None else {name: make_validator() for name in names}
    )
    resolved_executors = (
        executors if executors is not None else {name: AsyncMock() for name in names}
    )

    return QueryOrchestrator(
        sql_generator=generator if generator is not None else make_generator(),
        sql_validators=resolved_validators,
        sql_executors=resolved_executors,
        result_validator=result_validator if result_validator is not None else MagicMock(),
        schema_cache=schema_cache if schema_cache is not None else MagicMock(),
        pools=resolved_pools,
        resilience_config=(
            resilience if resilience is not None else ResilienceConfig(rate_limit_enabled=False)
        ),
        validation_config=validation if validation is not None else ValidationConfig(),
        metrics=metrics,
        rate_limiter=rate_limiter,
    )


class TestDatabaseResolution:
    """Test database name resolution logic."""

    @pytest.fixture
    def mock_pools(self) -> dict[str, MagicMock]:
        """Create mock connection pools."""
        return {"db1": MagicMock(), "db2": MagicMock()}

    @pytest.fixture
    def orchestrator(self, mock_pools: dict[str, MagicMock]) -> QueryOrchestrator:
        """Create orchestrator with mocked components."""
        return make_orchestrator(mock_pools)

    def test_resolve_database_specified_valid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified valid database."""
        assert orchestrator._resolve_database("db1") == "db1"

    def test_resolve_database_specified_invalid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified but invalid database."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = make_orchestrator({"only_db": MagicMock()})
        assert orchestrator._resolve_database(None) == "only_db"

    def test_resolve_database_auto_select_multiple_fails(
        self, orchestrator: QueryOrchestrator
    ) -> None:
        """Test that auto-select fails when multiple databases available."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "multiple databases" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = make_orchestrator({})

        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()

    def test_available_databases(self, orchestrator: QueryOrchestrator) -> None:
        """The orchestrator advertises every database it can route to."""
        assert sorted(orchestrator.available_databases) == ["db1", "db2"]


class TestPerDatabaseRouting:
    """A request must use the executor and validator of the resolved database."""

    @pytest.mark.asyncio
    async def test_executor_selected_by_database(self) -> None:
        """The executor of the requested database is the one that runs the SQL."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = sample_schema()

        executor_a = AsyncMock()
        executor_a.execute.return_value = ([{"from": "a"}], 1)
        executor_b = AsyncMock()
        executor_b.execute.return_value = ([{"from": "b"}], 1)

        orchestrator = make_orchestrator(
            {"a": MagicMock(), "b": MagicMock()},
            executors={"a": executor_a, "b": executor_b},
            schema_cache=schema_cache,
            validation=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="b", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        assert response.database == "b"
        assert response.data.rows == [{"from": "b"}]
        executor_b.execute.assert_awaited_once()
        executor_a.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validator_selected_by_database(self) -> None:
        """The security policy of the resolved database is the one enforced."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = sample_schema()

        permissive = make_validator()
        restrictive = rejected_validator("Access to table 'public.secret_data' is not allowed")
        executor = AsyncMock()
        executor.execute.return_value = ([], 0)

        orchestrator = make_orchestrator(
            {"open": MagicMock(), "locked": MagicMock()},
            validators={"open": permissive, "locked": restrictive},
            executors={"open": executor, "locked": executor},
            schema_cache=schema_cache,
            resilience=ResilienceConfig(max_retries=0, rate_limit_enabled=False),
            validation=ValidationConfig(enabled=False),
        )

        ok = await orchestrator.execute_query(
            QueryRequest(question="q", database="open", return_type=ReturnType.RESULT)
        )
        blocked = await orchestrator.execute_query(
            QueryRequest(question="q", database="locked", return_type=ReturnType.RESULT)
        )

        assert ok.success is True
        assert blocked.success is False
        assert blocked.error is not None
        assert blocked.error.code == "security_violation"
        restrictive.analyze.assert_called()

    @pytest.mark.asyncio
    async def test_missing_executor_is_reported(self) -> None:
        """A database without an executor fails loudly instead of falling back."""
        orchestrator = make_orchestrator(
            {"a": MagicMock(), "b": MagicMock()},
            executors={"a": AsyncMock()},
            validation=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="b", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_connection_error"
        assert response.error.details["database"] == "b"


class TestQuestionLengthGuard:
    """`validation.max_question_length` must be enforced before the LLM call."""

    @pytest.mark.asyncio
    async def test_long_question_is_rejected(self) -> None:
        """An over-long question fails with QUESTION_TOO_LONG and no LLM call."""
        generator = make_generator()
        orchestrator = make_orchestrator(
            generator=generator,
            validation=ValidationConfig(max_question_length=20, enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="x" * 50, database="test_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "question_too_long"
        assert response.error.details == {"length": 50, "max_length": 20}
        generator.generate_with_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_question_at_limit_is_accepted(self) -> None:
        """A question exactly at the limit is allowed through."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = sample_schema()
        executor = AsyncMock()
        executor.execute.return_value = ([{"n": 1}], 1)
        executor.security_config = MagicMock(max_rows=100)

        orchestrator = make_orchestrator(
            schema_cache=schema_cache,
            executors={"test_db": executor},
            validation=ValidationConfig(max_question_length=20, enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="y" * 20, database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.success is True


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return sample_schema()

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(self, mock_schema: DatabaseSchema) -> None:
        """Test successful SQL generation on first attempt."""
        generator = make_generator("SELECT * FROM users;", tokens=11)
        validator = make_validator()
        orchestrator = make_orchestrator(
            generator=generator,
            validators={"test_db": validator},
            resilience=ResilienceConfig(max_retries=3, rate_limit_enabled=False),
        )

        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            validator=validator,
            database="test_db",
            request_id="test-123",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert tokens == 11
        generator.generate_with_usage.assert_awaited_once()
        validator.analyze.assert_called_once_with("SELECT * FROM users;")

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test retry logic when validation fails."""
        generator = AsyncMock()
        generator.generate_with_usage.side_effect = [
            ("SELECT * FROM user;", 5),
            ("SELECT * FROM users;", 7),
        ]

        validator = MagicMock()
        validator.analyze.side_effect = [
            ValidationResult(
                is_valid=False,
                is_select=True,
                allows_data_modification=False,
                error_message='relation "user" does not exist',
                error_code="sql_parse_error",
            ),
            VALID_RESULT,
        ]

        orchestrator = make_orchestrator(
            generator=generator,
            validators={"test_db": validator},
            resilience=ResilienceConfig(max_retries=3, rate_limit_enabled=False),
        )

        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            validator=validator,
            database="test_db",
            request_id="test-123",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert tokens == 12  # tokens accumulate across attempts
        assert generator.generate_with_usage.await_count == 2
        assert validator.analyze.call_count == 2

        second_call = generator.generate_with_usage.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(self, mock_schema: DatabaseSchema) -> None:
        """Test failure after exhausting all retries."""
        generator = make_generator("DELETE FROM users;")
        validator = rejected_validator("DELETE statements are not allowed")

        orchestrator = make_orchestrator(
            generator=generator,
            validators={"test_db": validator},
            resilience=ResilienceConfig(max_retries=2, rate_limit_enabled=False),
        )

        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                validator=validator,
                database="test_db",
                request_id="test-123",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # max_retries + 1 attempts in total (initial + retries)
        assert generator.generate_with_usage.await_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(self, mock_schema: DatabaseSchema) -> None:
        """Test that an open circuit breaker prevents SQL generation."""
        validator = make_validator()
        orchestrator = make_orchestrator(
            validators={"test_db": validator},
            resilience=ResilienceConfig(circuit_breaker_threshold=1, rate_limit_enabled=False),
        )

        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                validator=validator,
                database="test_db",
                request_id="test-123",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_sql_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors during generation."""
        generator = AsyncMock()
        generator.generate_with_usage.side_effect = RuntimeError("Unexpected error")
        validator = make_validator()

        orchestrator = make_orchestrator(
            generator=generator,
            validators={"test_db": validator},
            resilience=ResilienceConfig(max_retries=1, rate_limit_enabled=False),
        )

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                validator=validator,
                database="test_db",
                request_id="test-123",
            )

        assert "unexpectedly" in str(exc_info.value).lower()
        assert orchestrator.circuit_breaker.failure_count == 1


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        result_validator = AsyncMock()
        result_validator.validate.return_value = make_validation_result(85)

        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=True),
        )

        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation is not None
        assert validation.confidence == 85
        result_validator.validate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        result_validator = AsyncMock()
        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=False),
        )

        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation is None
        result_validator.validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        result_validator = AsyncMock()
        result_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=True),
        )

        validation = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert validation is None


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return sample_schema()

    @pytest.fixture
    def cached_schema(self, mock_schema: DatabaseSchema) -> MagicMock:
        """Schema cache already holding the sample schema."""
        cache = MagicMock()
        cache.get.return_value = mock_schema
        cache.get_cache_age.return_value = 1.0
        return cache

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(
        self, cached_schema: MagicMock, mock_schema: DatabaseSchema
    ) -> None:
        """Test executing query with return_type=SQL."""
        orchestrator = make_orchestrator(
            generator=make_generator("SELECT * FROM users;"),
            schema_cache=cached_schema,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="Get all users", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None
        assert response.error is None
        assert response.request_id is not None

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self, cached_schema: MagicMock) -> None:
        """Test executing query with return_type=RESULT."""
        executor = AsyncMock()
        executor.execute.return_value = (
            [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            2,
        )

        result_validator = AsyncMock()
        result_validator.validate.return_value = make_validation_result(90)

        orchestrator = make_orchestrator(
            generator=make_generator("SELECT id, name FROM users;"),
            executors={"test_db": executor},
            result_validator=result_validator,
            schema_cache=cached_schema,
            validation=ValidationConfig(enabled=True),
        )

        response = await orchestrator.execute_query(
            QueryRequest(
                question="Get all users", database="test_db", return_type=ReturnType.RESULT
            )
        )

        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert response.data.total_row_count == 2
        assert response.data.truncated is False
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.result_validation is not None
        assert response.low_confidence is False
        assert response.error is None

    @pytest.mark.asyncio
    async def test_low_confidence_results_are_flagged(self, cached_schema: MagicMock) -> None:
        """Results below `min_confidence_score` are flagged but still returned."""
        executor = AsyncMock()
        executor.execute.return_value = ([{"n": 1}], 1)

        result_validator = AsyncMock()
        result_validator.validate.return_value = make_validation_result(40)

        orchestrator = make_orchestrator(
            executors={"test_db": executor},
            result_validator=result_validator,
            schema_cache=cached_schema,
            validation=ValidationConfig(enabled=True, min_confidence_score=70),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        assert response.confidence == 40
        assert response.low_confidence is True

    @pytest.mark.asyncio
    async def test_truncated_results_are_reported(self, cached_schema: MagicMock) -> None:
        """Row-limit truncation is visible to the caller."""
        executor = AsyncMock()
        executor.execute.return_value = ([{"n": 1}, {"n": 2}], 10)

        orchestrator = make_orchestrator(
            executors={"test_db": executor},
            schema_cache=cached_schema,
            validation=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.data is not None
        assert response.data.row_count == 2
        assert response.data.total_row_count == 10
        assert response.data.truncated is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        loaded = sample_schema()
        cache = MagicMock()
        cache.get.return_value = None
        cache.load = AsyncMock(return_value=loaded)
        pool = MagicMock()

        orchestrator = make_orchestrator(
            pools={"test_db": pool},
            schema_cache=cache,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="Test query", database="test_db", return_type=ReturnType.SQL)
        )

        cache.load.assert_awaited_once_with("test_db", pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        cache = MagicMock()
        cache.get.return_value = None
        cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = make_orchestrator(schema_cache=cache)

        response = await orchestrator.execute_query(
            QueryRequest(question="Test query", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self, cached_schema: MagicMock) -> None:
        """Test handling of SQL validation errors."""
        validator = rejected_validator("DELETE not allowed")

        orchestrator = make_orchestrator(
            generator=make_generator("DELETE FROM users;"),
            validators={"test_db": validator},
            schema_cache=cached_schema,
            resilience=ResilienceConfig(max_retries=1, rate_limit_enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(
                question="Delete all users", database="test_db", return_type=ReturnType.SQL
            )
        )

        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self, cached_schema: MagicMock) -> None:
        """Test handling of SQL execution errors."""
        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = make_orchestrator(
            executors={"test_db": executor},
            schema_cache=cached_schema,
            validation=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(
                question="Get all users", database="test_db", return_type=ReturnType.RESULT
            )
        )

        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self) -> None:
        """Test handling of unexpected errors."""
        cache = MagicMock()
        cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(schema_cache=cache)

        response = await orchestrator.execute_query(
            QueryRequest(question="Get all users", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self) -> None:
        """Test auto-selecting database when only one available."""
        cache = MagicMock()
        cache.get.return_value = sample_schema()
        cache.get_cache_age.return_value = 0.5

        orchestrator = make_orchestrator(pools={"only_db": MagicMock()}, schema_cache=cache)

        response = await orchestrator.execute_query(
            QueryRequest(question="Test query", database=None, return_type=ReturnType.SQL)
        )

        assert response.success is True
        assert response.database == "only_db"
        cache.get.assert_called_once_with("only_db")


class TestObservabilityIntegration:
    """Metrics and tracing are emitted from the real request path."""

    @pytest.mark.asyncio
    async def test_query_metrics_are_emitted(self) -> None:
        """A successful request increments query and LLM counters."""
        metrics = MetricsCollector()
        metrics.reset_all_metrics()

        cache = MagicMock()
        cache.get.return_value = sample_schema()
        cache.get_cache_age.return_value = 2.0
        executor = AsyncMock()
        executor.execute.return_value = ([{"n": 1}], 1)

        orchestrator = make_orchestrator(
            executors={"test_db": executor},
            schema_cache=cache,
            metrics=metrics,
            validation=ValidationConfig(enabled=False),
        )

        await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        rendered = metrics.render_latest().decode()
        assert 'pg_mcp_query_requests_total{database="test_db",status="success"} 1.0' in rendered
        assert 'pg_mcp_llm_calls_total{operation="generate"} 1.0' in rendered
        assert "pg_mcp_llm_tokens_used" in rendered
        assert 'pg_mcp_schema_cache_age_seconds{database="test_db"} 2.0' in rendered

    @pytest.mark.asyncio
    async def test_rejected_sql_metric_is_emitted(self) -> None:
        """A policy rejection increments the rejection counter with a reason."""
        metrics = MetricsCollector()
        metrics.reset_all_metrics()

        cache = MagicMock()
        cache.get.return_value = sample_schema()

        orchestrator = make_orchestrator(
            generator=make_generator("SELECT * FROM secret_data;"),
            validators={"test_db": rejected_validator("Access to table 'secret_data'")},
            schema_cache=cache,
            metrics=metrics,
            resilience=ResilienceConfig(max_retries=0, rate_limit_enabled=False),
        )

        await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        rendered = metrics.render_latest().decode()
        assert 'pg_mcp_sql_rejected_total{reason="blocked_table"} 1.0' in rendered

    @pytest.mark.asyncio
    async def test_health_snapshot_reports_components(self) -> None:
        """The health snapshot surfaces cache, breaker and limiter state."""
        cache = MagicMock()
        cache.get_cache_age.return_value = 3.0

        orchestrator = make_orchestrator(
            pools={"a": MagicMock(), "b": MagicMock()},
            schema_cache=cache,
            rate_limiter=MultiRateLimiter(query_limit=4, llm_limit=2),
            resilience=ResilienceConfig(rate_limit_enabled=True),
        )

        snapshot = orchestrator.health_snapshot()

        assert sorted(snapshot["databases"]) == ["a", "b"]
        assert snapshot["circuit_breaker"]["state"] == "closed"
        assert snapshot["rate_limiter"]["queries"]["max_concurrent"] == 4
        assert snapshot["rate_limiter"]["llm"]["max_concurrent"] == 2


class TestRateLimitingIntegration:
    """The concurrency limiter is applied to the request path."""

    @pytest.mark.asyncio
    async def test_db_concurrency_is_bounded(self) -> None:
        """Only `max_concurrent_queries` database operations run at once."""
        limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        await limiter.query_limiter.acquire()  # occupy the only slot

        cache = MagicMock()
        cache.get.return_value = sample_schema()
        executor = AsyncMock()
        executor.execute.return_value = ([{"n": 1}], 1)

        orchestrator = make_orchestrator(
            executors={"test_db": executor},
            schema_cache=cache,
            rate_limiter=limiter,
            resilience=ResilienceConfig(
                rate_limit_enabled=True, rate_limit_timeout=0.1, max_retries=0
            ),
            validation=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        executor.execute.assert_not_awaited()
