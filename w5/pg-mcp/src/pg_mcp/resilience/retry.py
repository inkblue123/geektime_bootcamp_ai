"""Retry with exponential backoff.

This module implements the retry/backoff behaviour promised by
:class:`~pg_mcp.config.settings.ResilienceConfig`. It is used by the query
orchestrator to survive *transient* failures of the LLM provider and of the
database connection layer, while deliberately never retrying deterministic
failures such as security violations, SQL syntax errors or validation errors.

Design notes
------------
* ``max_retries`` counts *additional* attempts, so a policy with
  ``max_retries=3`` performs at most 4 attempts.
* The delay before retry ``n`` (0-based) is
  ``min(retry_delay * backoff_factor ** n, max_retry_delay)``.
* ``retry_jitter`` applies a symmetric +/- fraction to that delay so that many
  concurrent clients do not retry in lock-step against a recovering service.
* Sleeping is injected (``sleep`` argument) so tests never actually wait.

Example:
    >>> import asyncio
    >>> from pg_mcp.resilience.retry import RetryPolicy, retry_async
    >>> policy = RetryPolicy(max_retries=2, initial_delay=0.0)
    >>> async def flaky() -> str:
    ...     raise TimeoutError("nope")
    >>> asyncio.run(retry_async(flaky, policy=policy))
    Traceback (most recent call last):
    ...
    TimeoutError: nope
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from pg_mcp.models.errors import (
    DatabaseConnectionError,
    ExecutionTimeoutError,
    LLMTimeoutError,
    LLMUnavailableError,
    RateLimitExceededError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pg_mcp.config.settings import ResilienceConfig

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

#: Exceptions that are considered transient and therefore retryable.
#:
#: Note that :class:`~pg_mcp.models.errors.SecurityViolationError`,
#: :class:`~pg_mcp.models.errors.SQLParseError`,
#: :class:`~pg_mcp.models.errors.ValidationError` and
#: :class:`~pg_mcp.models.errors.DatabaseError` are intentionally absent: they
#: describe deterministic problems that a retry cannot fix. Only the explicit
#: connectivity/timeout family is retried.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    LLMTimeoutError,
    LLMUnavailableError,
    DatabaseConnectionError,
    ExecutionTimeoutError,
    RateLimitExceededError,
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Immutable description of a retry/backoff strategy.

    Attributes:
        max_retries: Number of *additional* attempts after the first one.
        initial_delay: Delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after every attempt.
        max_delay: Upper bound for a single computed delay.
        jitter: Symmetric randomisation applied to the computed delay,
            expressed as a fraction in ``[0, 1]``. ``0`` disables jitter.
    """

    max_retries: int = 3
    initial_delay: float = 1.0
    backoff_factor: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        """Validate the policy bounds."""
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.initial_delay < 0:
            raise ValueError("initial_delay must be >= 0")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if self.max_delay < 0:
            raise ValueError("max_delay must be >= 0")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    @classmethod
    def from_config(cls, config: ResilienceConfig) -> RetryPolicy:
        """Build a policy from application configuration.

        Args:
            config: The resilience configuration section.

        Returns:
            RetryPolicy: Policy mirroring ``config``.
        """
        return cls(
            max_retries=config.max_retries,
            initial_delay=config.retry_delay,
            backoff_factor=config.backoff_factor,
            max_delay=config.max_retry_delay,
            jitter=config.retry_jitter,
        )

    @property
    def max_attempts(self) -> int:
        """Total number of attempts allowed (first try plus retries)."""
        return self.max_retries + 1

    def delay_for_attempt(self, attempt: int, rng: random.Random | None = None) -> float:
        """Compute how long to wait before a given retry.

        Args:
            attempt: Zero-based retry index (0 = wait before the 2nd attempt).
            rng: Optional random source, used to make jitter deterministic in
                tests. Defaults to the module-level :mod:`random` functions.

        Returns:
            float: Delay in seconds, clamped to ``[0, max_delay]``.
        """
        delay = self.initial_delay * (self.backoff_factor**attempt)
        delay = min(delay, self.max_delay)

        if self.jitter > 0 and delay > 0:
            source = rng or random
            delay *= 1 + source.uniform(-self.jitter, self.jitter)

        return max(0.0, min(delay, self.max_delay))

    def delays(self) -> list[float]:
        """Return the deterministic (jitter-free) delay schedule.

        Returns:
            list[float]: One delay per retry, in order.

        Example:
            >>> RetryPolicy(max_retries=3, initial_delay=1, backoff_factor=2).delays()
            [1.0, 2.0, 4.0]
        """
        return [
            min(self.initial_delay * self.backoff_factor**attempt, self.max_delay)
            for attempt in range(self.max_retries)
        ]


def is_retryable(
    error: BaseException,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
) -> bool:
    """Check whether an exception should be retried.

    Args:
        error: The exception instance raised by the failed attempt.
        retry_on: Tuple of exception types considered transient.

    Returns:
        bool: True when the error is transient.

    Example:
        >>> is_retryable(TimeoutError())
        True
        >>> is_retryable(ValueError("bad input"))
        False
    """
    return isinstance(error, retry_on)


async def retry_async(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    policy: RetryPolicy,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    operation: str | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng: random.Random | None = None,
    **kwargs: Any,
) -> T:
    """Await ``func`` with retry/backoff on transient failures.

    Args:
        func: Async callable to execute.
        *args: Positional arguments forwarded to ``func``.
        policy: Retry/backoff policy.
        retry_on: Exception types that trigger another attempt.
        on_retry: Optional hook ``(attempt, error, delay)`` invoked before each
            sleep. Useful for logging and metrics.
        operation: Human-readable operation name used in log messages.
        sleep: Injectable sleep coroutine (defaults to :func:`asyncio.sleep`).
        rng: Injectable random source used for jitter.
        **kwargs: Keyword arguments forwarded to ``func``.

    Returns:
        T: Whatever ``func`` returns on the first successful attempt.

    Raises:
        BaseException: The last exception raised by ``func`` when every attempt
            fails, or any non-retryable exception immediately.

    Example:
        >>> async def unstable() -> str:
        ...     return "ok"
        >>> import asyncio
        >>> asyncio.run(retry_async(unstable, policy=RetryPolicy()))
        'ok'
    """
    sleeper = sleep or asyncio.sleep
    name = operation or getattr(func, "__name__", "operation")
    attempts = policy.max_attempts

    for attempt in range(attempts):
        try:
            return await func(*args, **kwargs)
        except BaseException as error:
            is_last = attempt >= attempts - 1
            if is_last or not is_retryable(error, retry_on):
                raise

            delay = policy.delay_for_attempt(attempt, rng=rng)
            logger.warning(
                "Retrying after transient failure",
                extra={
                    "operation": name,
                    "attempt": attempt + 1,
                    "max_attempts": attempts,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            if on_retry is not None:
                on_retry(attempt + 1, error, delay)

            if delay > 0:
                await sleeper(delay)

    # ``range(attempts)`` always either returns or raises, so this is defensive.
    raise RuntimeError(f"retry_async exhausted attempts for {name}")  # pragma: no cover


def with_retry(
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
    operation: str | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator form of :func:`retry_async`.

    Args:
        policy: Retry/backoff policy.
        retry_on: Exception types that trigger another attempt.
        operation: Operation name used in logs (defaults to the function name).

    Returns:
        Callable: A decorator that wraps async functions with retry semantics.

    Example:
        >>> @with_retry(RetryPolicy(max_retries=1))
        ... async def fetch() -> int:
        ...     return 1
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await retry_async(
                func,
                *args,
                policy=policy,
                retry_on=retry_on,
                operation=operation or func.__name__,
                **kwargs,  # type: ignore[arg-type]
            )

        return wrapper

    return decorator
