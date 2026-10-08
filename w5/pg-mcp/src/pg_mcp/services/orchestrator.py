"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation.

Beyond plain coordination it is the single place where the cross-cutting
production concerns are enforced:

* **Per-database routing** - the validator and executor are selected from the
  database resolved for the request, so a request can never silently run
  against the wrong database.
* **Input guards** - ``ValidationConfig.max_question_length`` is enforced before
  any LLM call is made.
* **Resilience** - LLM and database calls are wrapped by the configured
  concurrency limiters, the circuit breaker and retry/backoff policy.
* **Observability** - every request carries a ``request_id`` and emits the
  Prometheus metrics declared for it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from pg_mcp.models.errors import (
    DatabaseConnectionError,
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMUnavailableError,
    PgMcpError,
    QuestionTooLongError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ResultValidationResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.tracing import generate_request_id, get_request_id, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.retry import RetryPolicy, retry_async

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from asyncpg import Pool

    from pg_mcp.cache.schema_cache import SchemaCache
    from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
    from pg_mcp.observability.metrics import MetricsCollector
    from pg_mcp.resilience.rate_limiter import MultiRateLimiter
    from pg_mcp.services.result_validator import ResultValidator
    from pg_mcp.services.sql_executor import SQLExecutor
    from pg_mcp.services.sql_generator import SQLGenerator
    from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation per target database. It implements retry logic with error
    feedback, the circuit breaker pattern for fault tolerance, concurrency
    limiting and comprehensive error handling.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validators={"mydb": validator},
        ...     sql_executors={"mydb": executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(
        ...     QueryRequest(question="How many users?")
        ... )
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validators: Mapping[str, SQLValidator],
        sql_executors: Mapping[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: Mapping[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        metrics: MetricsCollector | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validators: Per-database SQL validators, keyed by database name.
                Each validator carries its own effective security policy.
            sql_executors: Per-database SQL executors, keyed by database name.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Database name to connection pool mapping.
            resilience_config: Retry, circuit breaker and rate limiting settings.
            validation_config: Validation settings including thresholds.
            metrics: Optional Prometheus metrics collector.
            rate_limiter: Optional concurrency limiter for LLM and DB operations.
            circuit_breaker: Optional shared circuit breaker for LLM calls. A new
                one is created from ``resilience_config`` when omitted.
        """
        self.sql_generator = sql_generator
        self.sql_validators = dict(sql_validators)
        self.sql_executors = dict(sql_executors)
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = dict(pools)
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.metrics = metrics
        self.rate_limiter = rate_limiter

        # Single shared circuit breaker instance (never instantiated twice).
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

        self.retry_policy = RetryPolicy.from_config(resilience_config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available_databases(self) -> list[str]:
        """Names of every database the orchestrator can route requests to."""
        return list(self.pools.keys())

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute the complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Assign a request_id for full-chain tracing
        2. Enforce the configured question-length limit
        3. Resolve the target database and pick its validator/executor
        4. Load the schema from cache (or introspect it)
        5. Generate and validate SQL with retry logic
        6. Execute SQL (when ``return_type == RESULT``)
        7. Validate results (optional, never fatal)
        8. Return a structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        request_id = get_request_id() or generate_request_id()
        started = time.perf_counter()
        database_name: str | None = None
        status = "error"

        async with request_context(request_id):
            logger.info(
                "Starting query execution",
                extra={
                    "request_id": request_id,
                    "question_length": len(request.question),
                    "database": request.database,
                    "return_type": str(request.return_type),
                },
            )

            try:
                response = await self._execute_query_inner(request, request_id)
                database_name = response.database
                if response.success:
                    status = "success"
                elif response.error is not None:
                    status = response.error.code
                else:
                    status = "error"
                return response
            except PgMcpError as e:
                status = str(e.code)
                logger.warning(
                    "Query execution failed with known error",
                    extra={
                        "request_id": request_id,
                        "error_code": str(e.code),
                        "error_message": e.message,
                    },
                )
                return QueryResponse(
                    success=False,
                    request_id=request_id,
                    database=database_name,
                    generated_sql=None,
                    validation=None,
                    data=None,
                    result_validation=None,
                    error=ErrorDetail(
                        code=str(e.code),
                        message=e.message,
                        details=e.details,
                    ),
                    confidence=0,
                    tokens_used=0,
                )
            except Exception as e:
                logger.exception(
                    "Query execution failed with unexpected error",
                    extra={"request_id": request_id},
                )
                return QueryResponse(
                    success=False,
                    request_id=request_id,
                    database=database_name,
                    generated_sql=None,
                    validation=None,
                    data=None,
                    result_validation=None,
                    error=ErrorDetail(
                        code=str(ErrorCode.INTERNAL_ERROR),
                        message=f"Internal server error: {e!s}",
                        details={"error_type": type(e).__name__},
                    ),
                    confidence=0,
                    tokens_used=0,
                )
            finally:
                duration = time.perf_counter() - started
                if self.metrics is not None:
                    self.metrics.observe_query_duration(duration)
                    self.metrics.increment_query_request(
                        status=status, database=database_name or "unknown"
                    )
                logger.info(
                    "Query execution finished",
                    extra={
                        "request_id": request_id,
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    async def _execute_query_inner(self, request: QueryRequest, request_id: str) -> QueryResponse:
        """Run the pipeline once inside an established request context."""
        # Step 1: enforce the configured input length guard before any LLM call
        self._check_question_length(request.question)

        # Step 2: resolve database and its per-database components
        database_name = self._resolve_database(request.database)
        validator = self._get_validator(database_name)
        executor = self._get_executor(database_name)

        logger.debug(
            "Resolved database",
            extra={"request_id": request_id, "database": database_name},
        )

        # Step 3: obtain the schema
        schema = await self._get_schema(database_name, request_id)

        # Step 4: generate and validate SQL with retry logic
        generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
            question=request.question,
            schema=schema,
            validator=validator,
            database=database_name,
            request_id=request_id,
        )

        # Step 5: short-circuit for SQL-only requests
        if request.return_type == ReturnType.SQL:
            logger.info(
                "Returning SQL only",
                extra={"request_id": request_id, "sql_length": len(generated_sql)},
            )
            return QueryResponse(
                success=True,
                request_id=request_id,
                database=database_name,
                generated_sql=generated_sql,
                validation=validation_result,
                data=None,
                result_validation=None,
                error=None,
                confidence=100,
                tokens_used=tokens_used,
            )

        # Step 6: execute SQL (rate limited, retried on transient failures)
        logger.debug("Executing SQL", extra={"request_id": request_id})
        start_time = time.perf_counter()

        results, total_count = await self._run_db_operation(executor.execute, generated_sql)

        execution_time_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "SQL executed successfully",
            extra={
                "request_id": request_id,
                "database": database_name,
                "row_count": len(results),
                "total_row_count": total_count,
                "execution_time_ms": round(execution_time_ms, 2),
            },
        )

        # Step 7: validate results (non-blocking, failures never fail the request)
        result_validation = await self._validate_results_safely(
            question=request.question,
            sql=generated_sql,
            results=results,
            row_count=total_count,
            request_id=request_id,
        )

        confidence = result_validation.confidence if result_validation is not None else 100
        low_confidence = confidence < self.validation_config.min_confidence_score
        if low_confidence:
            logger.warning(
                "Result confidence below configured minimum",
                extra={
                    "request_id": request_id,
                    "confidence": confidence,
                    "min_confidence_score": self.validation_config.min_confidence_score,
                },
            )

        # Step 8: build the successful response
        query_result = QueryResult(
            columns=list(results[0].keys()) if results else [],
            rows=results,
            row_count=len(results),
            total_row_count=total_count,
            truncated=total_count > len(results),
            execution_time_ms=execution_time_ms,
        )

        return QueryResponse(
            success=True,
            request_id=request_id,
            database=database_name,
            generated_sql=generated_sql,
            validation=validation_result,
            data=query_result,
            result_validation=result_validation,
            error=None,
            confidence=confidence,
            low_confidence=low_confidence,
            tokens_used=tokens_used,
        )

    def _check_question_length(self, question: str) -> None:
        """Reject questions longer than the configured maximum.

        Args:
            question: The user's natural language question.

        Raises:
            QuestionTooLongError: If the question exceeds the configured limit.
        """
        limit = self.validation_config.max_question_length
        if len(question) > limit:
            raise QuestionTooLongError(
                message=f"Question exceeds the maximum length of {limit} characters",
                details={"length": len(question), "max_length": limit},
            )

    def _get_validator(self, database_name: str) -> SQLValidator:
        """Return the validator bound to ``database_name``.

        Args:
            database_name: Resolved database name.

        Returns:
            SQLValidator: The database-specific validator.

        Raises:
            DatabaseConnectionError: If no validator is registered.
        """
        validator = self.sql_validators.get(database_name)
        if validator is None:
            raise DatabaseConnectionError(
                message=f"No validator configured for database '{database_name}'",
                details={"database": database_name},
            )
        return validator

    def _get_executor(self, database_name: str) -> SQLExecutor:
        """Return the executor bound to ``database_name``.

        Args:
            database_name: Resolved database name.

        Returns:
            SQLExecutor: The database-specific executor.

        Raises:
            DatabaseConnectionError: If no executor is registered. This is what
                prevents a request from silently running on the wrong database.
        """
        executor = self.sql_executors.get(database_name)
        if executor is None:
            raise DatabaseConnectionError(
                message=f"Database '{database_name}' is not available",
                details={
                    "database": database_name,
                    "available_databases": self.available_databases,
                },
            )
        return executor

    async def _get_schema(self, database_name: str, request_id: str) -> Any:
        """Load the schema for a database from cache or introspection.

        Args:
            database_name: Resolved database name.
            request_id: Request ID for tracing.

        Returns:
            DatabaseSchema: The database schema.

        Raises:
            SchemaLoadError: If introspection fails.
        """
        schema = self.schema_cache.get(database_name)
        if schema is not None:
            self._report_cache_age(database_name)
            return schema

        pool = self.pools.get(database_name)
        if pool is None:
            raise DatabaseConnectionError(
                message=f"No connection pool available for database '{database_name}'",
                details={"database": database_name},
            )

        try:
            schema = await self._run_db_operation(self.schema_cache.load, database_name, pool)
        except PgMcpError:
            raise
        except Exception as e:
            raise SchemaLoadError(
                message=f"Failed to load schema for database '{database_name}': {e!s}",
                details={"database": database_name, "error": str(e)},
            ) from e

        self._report_cache_age(database_name)
        logger.debug(
            "Schema loaded",
            extra={
                "request_id": request_id,
                "database": database_name,
                "tables": len(schema.tables),
            },
        )
        return schema

    def _report_cache_age(self, database_name: str) -> None:
        """Publish the schema cache age gauge if the cache tracks it."""
        if self.metrics is None:
            return
        age = self.schema_cache.get_cache_age(database_name)
        if age is not None:
            self.metrics.set_schema_cache_age(database_name, age)

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb"
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one
        """
        if database is not None:
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": self.available_databases,
                    },
                )
            return database

        available_dbs = self.available_databases
        if len(available_dbs) == 0:
            raise DatabaseError(message="No databases configured", details={})
        if len(available_dbs) == 1:
            return available_dbs[0]

        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    # ------------------------------------------------------------------
    # Resilience helpers
    # ------------------------------------------------------------------

    @property
    def _rate_limiting_enabled(self) -> bool:
        """Whether concurrency limiting should be applied."""
        return self.rate_limiter is not None and self.resilience_config.rate_limit_enabled

    def _active_limiter(self) -> MultiRateLimiter | None:
        """Return the limiter to use, or None when limiting is disabled."""
        return self.rate_limiter if self._rate_limiting_enabled else None

    async def _run_db_operation(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a database operation under the query limiter and retry policy.

        Args:
            func: Async callable performing the database work.
            *args: Positional arguments for ``func``.
            **kwargs: Keyword arguments for ``func``.

        Returns:
            Any: The value returned by ``func``.

        Raises:
            RateLimitExceededError: If no concurrency slot became available.
        """
        limiter = self._active_limiter()
        if limiter is None:
            return await retry_async(func, *args, policy=self.retry_policy, **kwargs)

        timeout = self.resilience_config.rate_limit_timeout
        try:
            async with limiter.for_queries(timeout=timeout):
                return await retry_async(func, *args, policy=self.retry_policy, **kwargs)
        except TimeoutError as e:
            raise RateLimitExceededError(
                message="Too many concurrent database operations, please retry later",
                details={"timeout_seconds": timeout},
            ) from e

    async def _call_llm(self, operation: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run an LLM call under the rate limiter, with metrics and retries.

        Args:
            operation: Metric label, e.g. ``"generate"`` or ``"validate"``.
            factory: Zero-argument callable returning the awaitable to run. A
                factory is used (rather than a bare coroutine) so that a rate
                limit rejection cannot leave an un-awaited coroutine behind.

        Returns:
            Any: The awaited result.

        Raises:
            RateLimitExceededError: If no concurrency slot became available.
        """
        timeout = self.resilience_config.rate_limit_timeout
        started = time.perf_counter()
        limiter = self._active_limiter()

        try:
            if limiter is not None:
                async with limiter.for_llm(timeout=timeout):
                    result = await retry_async(
                        factory, policy=self.retry_policy, operation=operation
                    )
            else:
                result = await retry_async(factory, policy=self.retry_policy, operation=operation)
        except TimeoutError as e:
            raise RateLimitExceededError(
                message="Too many concurrent LLM calls, please retry later",
                details={"timeout_seconds": timeout, "operation": operation},
            ) from e
        finally:
            if self.metrics is not None:
                self.metrics.increment_llm_call(operation)
                self.metrics.observe_llm_latency(operation, time.perf_counter() - started)

        return result

    # ------------------------------------------------------------------
    # SQL generation
    # ------------------------------------------------------------------

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        validator: SQLValidator,
        database: str,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry logic on validation failures.

        The loop:
        1. Checks the circuit breaker state
        2. Generates SQL using the LLM
        3. Analyses the SQL against the database's security policy
        4. On policy rejection, retries with the failure fed back to the LLM
        5. Records success/failure to the circuit breaker

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            validator: Security validator for the resolved database.
            database: Resolved database name (for metrics labels).
            request_id: Request ID for tracking.

        Returns:
            tuple: ``(generated_sql, validation_result, tokens_used)``

        Raises:
            LLMError: If the circuit breaker is open or generation fails.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.
        """
        if not self.circuit_breaker.allow_request():
            raise LLMUnavailableError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": str(self.circuit_breaker.state),
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            logger.debug(
                "Generating SQL",
                extra={
                    "request_id": request_id,
                    "attempt": attempt + 1,
                    "max_attempts": max_retries + 1,
                },
            )

            generated_sql, attempt_tokens = await self._generate_once(
                question=question,
                schema=schema,
                previous_sql=previous_sql,
                error_feedback=error_feedback,
                request_id=request_id,
            )
            if attempt_tokens:
                tokens_used = (tokens_used or 0) + attempt_tokens
                if self.metrics is not None:
                    self.metrics.increment_llm_tokens("generate", attempt_tokens)

            logger.debug(
                "SQL generated",
                extra={"request_id": request_id, "sql_length": len(generated_sql)},
            )

            # Analyse (never raises) so the outcome can be reported truthfully.
            validation_result = validator.analyze(generated_sql)

            if not validation_result.is_valid:
                reason = self._rejection_reason(validation_result)
                if self.metrics is not None:
                    self.metrics.increment_sql_rejected(reason)

                if attempt < max_retries:
                    logger.warning(
                        "SQL rejected by security policy, retrying with feedback",
                        extra={
                            "request_id": request_id,
                            "database": database,
                            "attempt": attempt + 1,
                            "reason": reason,
                            "error": validation_result.error_message,
                        },
                    )
                    previous_sql = generated_sql
                    error_feedback = validation_result.error_message or "SQL rejected"
                    continue

                self.circuit_breaker.record_failure()
                logger.error(
                    "SQL rejected after all retries",
                    extra={
                        "request_id": request_id,
                        "database": database,
                        "attempts": attempt + 1,
                        "reason": reason,
                        "error": validation_result.error_message,
                    },
                )
                self._raise_for_validation(validation_result)

            # Validation successful
            self.circuit_breaker.record_success()
            logger.info(
                "SQL generated and validated successfully",
                extra={"request_id": request_id, "attempts": attempt + 1},
            )
            return generated_sql, validation_result, tokens_used

        # Defensive: the loop always returns or raises.
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _generate_once(
        self,
        question: str,
        schema: Any,
        previous_sql: str | None,
        error_feedback: str | None,
        request_id: str,
    ) -> tuple[str, int | None]:
        """Run a single LLM generation attempt.

        Unexpected failures are converted into :class:`LLMError` and recorded
        against the circuit breaker so transient provider outages eventually
        fail fast instead of hammering the API.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            previous_sql: SQL from the previous attempt, if any.
            error_feedback: Reason the previous attempt was rejected, if any.
            request_id: Request ID for tracing.

        Returns:
            tuple[str, int | None]: Generated SQL and tokens used (if reported).

        Raises:
            LLMError: When the provider call fails unexpectedly.
            RateLimitExceededError: When no LLM concurrency slot is available.
        """
        try:
            generated: tuple[str, int | None] = await self._call_llm(
                "generate",
                lambda: self.sql_generator.generate_with_usage(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=error_feedback,
                ),
            )
        except RateLimitExceededError:
            # Local backpressure is not an LLM outage: do not trip the breaker.
            raise
        except PgMcpError:
            self.circuit_breaker.record_failure()
            logger.warning(
                "LLM generation failed",
                extra={
                    "request_id": request_id,
                    "circuit_state": str(self.circuit_breaker.state),
                },
            )
            raise
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.exception(
                "Unexpected error during SQL generation",
                extra={"request_id": request_id},
            )
            raise LLMError(
                message=f"SQL generation failed unexpectedly: {e!s}",
                details={"error_type": type(e).__name__},
            ) from e

        return generated

    @staticmethod
    def _rejection_reason(validation_result: ValidationResult) -> str:
        """Map a failed validation to a stable metric label.

        Args:
            validation_result: The failed validation result.

        Returns:
            str: One of ``parse_error``, ``blocked_function``, ``blocked_table``,
            ``blocked_column``, ``explain_denied``, ``write_operation``,
            ``statement_type`` or ``security_violation``.
        """
        message = (validation_result.error_message or "").lower()
        if validation_result.error_code == str(ErrorCode.SQL_PARSE_ERROR):
            return "parse_error"
        if "explain" in message:
            return "explain_denied"
        if "function" in message:
            return "blocked_function"
        if "table" in message:
            return "blocked_table"
        if "column" in message:
            return "blocked_column"
        if validation_result.allows_data_modification:
            return "write_operation"
        if "not allowed" in message:
            return "statement_type"
        return "security_violation"

    @staticmethod
    def _raise_for_validation(validation_result: ValidationResult) -> None:
        """Raise the exception matching a failed validation result.

        Args:
            validation_result: The failed validation result.

        Raises:
            SQLParseError: When the SQL could not be parsed.
            SecurityViolationError: When the SQL violated the security policy.
        """
        message = validation_result.error_message or "SQL failed security validation"
        if validation_result.error_code == str(ErrorCode.SQL_PARSE_ERROR):
            raise SQLParseError(message, details={"sql_error": message})
        raise SecurityViolationError(message)

    # ------------------------------------------------------------------
    # Execution and result validation
    # ------------------------------------------------------------------

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> ResultValidationResult | None:
        """Validate query results with error handling (non-blocking).

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            ResultValidationResult | None: The validation outcome, or None when
            validation is disabled or failed. Callers treat None as confidence
            100 so a flaky validator can never fail a working query.
        """
        if not self.validation_config.enabled:
            return None

        try:
            logger.debug("Validating results", extra={"request_id": request_id})
            validation: ResultValidationResult = await self._call_llm(
                "validate",
                lambda: self.result_validator.validate(
                    question=question,
                    sql=sql,
                    results=results,
                    row_count=row_count,
                ),
            )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation.confidence,
                    "is_acceptable": validation.is_acceptable,
                },
            )
            return validation

        except Exception as e:
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={"request_id": request_id, "error": str(e)},
            )
            return None

    # ------------------------------------------------------------------
    # Health reporting
    # ------------------------------------------------------------------

    def health_snapshot(self) -> dict[str, Any]:
        """Collect a point-in-time health snapshot of the orchestrator.

        Returns:
            dict: Database list, schema cache ages, circuit breaker state and
            limiter statistics. Intended for the MCP ``health_check`` tool.
        """
        return {
            "databases": self.available_databases,
            "schema_cache": {
                name: self.schema_cache.get_cache_age(name) for name in self.available_databases
            },
            "circuit_breaker": {
                "state": str(self.circuit_breaker.state),
                "failure_count": self.circuit_breaker.failure_count,
            },
            "rate_limiter": (
                self.rate_limiter.get_all_stats() if self.rate_limiter is not None else None
            ),
            "retry_policy": {
                "max_retries": self.retry_policy.max_retries,
                "delays_seconds": self.retry_policy.delays(),
            },
        }

    @staticmethod
    def new_request_id() -> str:
        """Generate a request ID in the documented ``req_<hex>`` format.

        Returns:
            str: New request identifier.
        """
        return f"req_{uuid.uuid4().hex[:12]}"
