"""Run the real MCP server against the offline demo backends.

This is the same FastMCP server, the same ``QueryOrchestrator`` and the same
tools as ``pg_mcp.server``; only the PostgreSQL pool and the LLM are replaced by
the deterministic doubles from :mod:`offline_demo`. It exists so the MCP stdio
protocol can be exercised - and screenshotted - without any external service.

Usage::

    python demo/offline_server.py      # speaks MCP over stdio
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import FastMCP
from offline_demo import DemoContext, build_context

from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.observability.logging import configure_logging
from pg_mcp.observability.tracing import generate_request_id, request_context

_context: DemoContext | None = None

mcp = FastMCP(
    "pg-mcp-offline",
    instructions=("Natural-language PostgreSQL query server running on offline demo backends."),
)


def context() -> DemoContext:
    """Return (and lazily build) the demo context."""
    global _context
    if _context is None:
        _context = build_context()
    return _context


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against a PostgreSQL database.

    Args:
        question: Natural language description of the query.
        database: Target database name (optional when only one is configured).
        return_type: "sql" to only generate SQL, "result" to execute it.

    Returns:
        dict: The structured query response.
    """
    ctx = context()
    async with request_context(generate_request_id()):
        response = await ctx.orchestrator.execute_query(
            QueryRequest(
                question=question,
                database=database,
                return_type=ReturnType(return_type),
            )
        )
    return response.to_dict()


@mcp.tool()
async def list_databases() -> dict[str, Any]:
    """List the databases this server can query.

    Returns:
        dict: ``success``, ``databases`` and the auto-selected ``default``.
    """
    names = context().settings.database_names()
    return {
        "success": True,
        "databases": names,
        "default": names[0] if len(names) == 1 else None,
    }


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Report server readiness, database connectivity and resilience state.

    Returns:
        dict: The health report.
    """
    return context().health.snapshot().to_dict()


def main() -> None:
    """Serve MCP over stdio."""
    configure_logging(level="WARNING", log_format="text")
    import anyio

    anyio.run(mcp.run_stdio_async)


if __name__ == "__main__":
    main()
