"""Unit tests for the retry/backoff helper."""

from __future__ import annotations

import random
from unittest.mock import AsyncMock

import pytest

from pg_mcp.config.settings import ResilienceConfig
from pg_mcp.models.errors import (
    DatabaseConnectionError,
    LLMTimeoutError,
    SecurityViolationError,
)
from pg_mcp.resilience.retry import (
    RetryPolicy,
    is_retryable,
    retry_async,
    with_retry,
)


class TestRetryPolicy:
    """Policy construction and delay computation."""

    def test_defaults(self) -> None:
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.max_attempts == 4
        assert policy.initial_delay == 1.0
        assert policy.backoff_factor == 2.0

    def test_from_config(self) -> None:
        policy = RetryPolicy.from_config(
            ResilienceConfig(
                max_retries=4,
                retry_delay=0.5,
                backoff_factor=3.0,
                max_retry_delay=10.0,
                retry_jitter=0.0,
            )
        )
        assert policy.max_retries == 4
        assert policy.initial_delay == 0.5
        assert policy.backoff_factor == 3.0
        assert policy.max_delay == 10.0
        assert policy.jitter == 0.0

    def test_exponential_schedule(self) -> None:
        policy = RetryPolicy(
            max_retries=4, initial_delay=1.0, backoff_factor=2.0, max_delay=100.0, jitter=0.0
        )
        assert policy.delays() == [1.0, 2.0, 4.0, 8.0]

    def test_delay_is_capped(self) -> None:
        policy = RetryPolicy(
            max_retries=5, initial_delay=1.0, backoff_factor=10.0, max_delay=5.0, jitter=0.0
        )
        assert policy.delays() == [1.0, 5.0, 5.0, 5.0, 5.0]

    def test_jitter_stays_within_bounds(self) -> None:
        policy = RetryPolicy(
            max_retries=3, initial_delay=2.0, backoff_factor=1.0, max_delay=10.0, jitter=0.5
        )
        rng = random.Random(1234)  # noqa: S311 - deterministic test jitter, not crypto
        delays = [policy.delay_for_attempt(0, rng=rng) for _ in range(50)]
        assert all(1.0 <= d <= 3.0 for d in delays)
        assert len(set(delays)) > 1  # jitter actually varies

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_retries": -1}, "max_retries"),
            ({"initial_delay": -1.0}, "initial_delay"),
            ({"backoff_factor": 0.5}, "backoff_factor"),
            ({"max_delay": -1.0}, "max_delay"),
            ({"jitter": 1.5}, "jitter"),
        ],
    )
    def test_invalid_policies_are_rejected(self, kwargs: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            RetryPolicy(**kwargs)


class TestIsRetryable:
    """Only transient failures are retried."""

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(),
            ConnectionResetError(),
            LLMTimeoutError("timed out"),
            DatabaseConnectionError("no route"),
        ],
    )
    def test_transient_errors_are_retryable(self, error: Exception) -> None:
        assert is_retryable(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("bad input"),
            SecurityViolationError("nope"),
            KeyError("missing"),
        ],
    )
    def test_deterministic_errors_are_not_retryable(self, error: Exception) -> None:
        assert is_retryable(error) is False


class TestRetryAsync:
    """Behaviour of `retry_async`."""

    @pytest.mark.asyncio
    async def test_returns_first_success(self) -> None:
        func = AsyncMock(return_value="ok")
        sleep = AsyncMock()

        result = await retry_async(func, policy=RetryPolicy(), sleep=sleep)

        assert result == "ok"
        func.assert_awaited_once()
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_transient_failures_then_succeeds(self) -> None:
        func = AsyncMock(side_effect=[TimeoutError("boom"), TimeoutError("boom"), "ok"])
        sleep = AsyncMock()
        policy = RetryPolicy(max_retries=3, initial_delay=1.0, backoff_factor=2.0, jitter=0.0)

        result = await retry_async(func, policy=policy, sleep=sleep)

        assert result == "ok"
        assert func.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self) -> None:
        func = AsyncMock(side_effect=TimeoutError("always down"))
        sleep = AsyncMock()

        with pytest.raises(TimeoutError):
            await retry_async(func, policy=RetryPolicy(max_retries=2), sleep=sleep)

        assert func.await_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_non_retryable_error_is_raised_immediately(self) -> None:
        func = AsyncMock(side_effect=SecurityViolationError("blocked"))
        sleep = AsyncMock()

        with pytest.raises(SecurityViolationError):
            await retry_async(func, policy=RetryPolicy(max_retries=5), sleep=sleep)

        func.assert_awaited_once()
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_retry_hook_receives_attempt_and_delay(self) -> None:
        func = AsyncMock(side_effect=[TimeoutError("x"), "ok"])
        sleep = AsyncMock()
        seen: list[tuple[int, str, float]] = []

        await retry_async(
            func,
            policy=RetryPolicy(max_retries=2, initial_delay=0.5, jitter=0.0),
            sleep=sleep,
            on_retry=lambda attempt, error, delay: seen.append(
                (attempt, type(error).__name__, delay)
            ),
        )

        assert seen == [(1, "TimeoutError", 0.5)]

    @pytest.mark.asyncio
    async def test_forwards_arguments(self) -> None:
        func = AsyncMock(return_value=7)

        result = await retry_async(func, 3, 4, policy=RetryPolicy(), sleep=AsyncMock(), key="value")

        assert result == 7
        func.assert_awaited_once_with(3, 4, key="value")


class TestWithRetryDecorator:
    """The decorator form mirrors `retry_async`."""

    @pytest.mark.asyncio
    async def test_decorator_retries(self) -> None:
        calls = {"n": 0}

        @with_retry(RetryPolicy(max_retries=2, initial_delay=0.0, jitter=0.0))
        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("reset")
            return "done"

        assert await flaky() == "done"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_decorator_preserves_metadata(self) -> None:
        @with_retry(RetryPolicy(max_retries=1))
        async def documented() -> None:
            """Docstring stays put."""

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Docstring stays put."
