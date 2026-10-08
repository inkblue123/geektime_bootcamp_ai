"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.

Tests under ``tests/integration`` and ``tests/e2e`` need a real PostgreSQL
database (and, for some of them, an OpenAI key). They are skipped unless
``PG_MCP_LIVE_TESTS=1`` is set, so the default ``pytest`` run is fully
deterministic and offline.
"""

import os
from pathlib import Path

import pytest

from pg_mcp.config.settings import reset_settings

LIVE_TEST_ENV = "PG_MCP_LIVE_TESTS"
LIVE_TEST_DIRS = ("integration", "e2e")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip live-service test suites unless explicitly enabled.

    Args:
        config: The active pytest configuration (unused).
        items: Collected test items, filtered in place.
    """
    if os.environ.get(LIVE_TEST_ENV) == "1":
        return

    skip_marker = pytest.mark.skip(
        reason=f"requires a live PostgreSQL/OpenAI environment; set {LIVE_TEST_ENV}=1 to run"
    )

    for item in items:
        parts = Path(str(item.fspath)).parts
        if any(directory in parts for directory in LIVE_TEST_DIRS):
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable the metrics HTTP server for tests to avoid port conflicts."""
    previous = os.environ.get("OBSERVABILITY_METRICS_ENABLED")
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    if previous is None:
        os.environ.pop("OBSERVABILITY_METRICS_ENABLED", None)
    else:
        os.environ["OBSERVABILITY_METRICS_ENABLED"] = previous
