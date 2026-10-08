"""Drive the offline MCP server over the real stdio protocol.

The script launches ``demo/offline_server.py`` as a subprocess and speaks MCP
JSON-RPC to it through the official client library, exercising:

1. ``initialize``           - protocol handshake
2. ``tools/list``           - tool discovery
3. ``tools/call query``     - a successful multi-database query
4. ``tools/call query``     - a security rejection
5. ``tools/call health_check`` - readiness report

Usage::

    python demo/mcp_stdio_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from offline_demo import Screen

ROOT = Path(__file__).resolve().parent.parent
SERVER = Path(__file__).resolve().parent / "offline_server.py"


def abbreviate(payload: object, limit: int = 200) -> str:
    """Render a JSON payload, truncating it to ``limit`` characters."""
    text = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= limit else f"{text[:limit]}..."


def attr(obj: object, *names: str, default: object = None) -> object:
    """Return the first present attribute among ``names``.

    The MCP SDK renamed several public attributes between major versions
    (``serverInfo`` -> ``server_info``), so tolerate both spellings.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def tool_payload(result: object) -> dict:
    """Extract the structured payload returned by a FastMCP tool call."""
    structured = attr(result, "structuredContent", "structured_content")
    if isinstance(structured, dict):
        # FastMCP wraps non-object returns in {"result": ...}
        return structured.get("result", structured) if set(structured) == {"result"} else structured

    content = attr(result, "content", default=[]) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


async def run_smoke() -> Screen:
    """Run the MCP stdio smoke test and collect a transcript.

    Returns:
        Screen: Transcript chunk describing the exchange.
    """
    lines: list[str] = []
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env=None,
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):  # noqa: SIM117
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            server_info = attr(init, "serverInfo", "server_info")
            lines.append(
                f"[1] initialize        -> server={attr(server_info, 'name')!r} "
                f"version={attr(server_info, 'version')!r} "
                f"protocol={attr(init, 'protocolVersion', 'protocol_version')!r}"
            )

            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            lines.append(f"[2] tools/list        -> {names}")
            for tool in tools.tools:
                schema = attr(tool, "inputSchema", "input_schema", default={}) or {}
                lines.append(f"      - {tool.name}(required={schema.get('required', [])})")

            lines.append("")
            lines.append(
                '    tools/call query  {"question": "How many users do we have?",'
                ' "database": "sales"}'
            )
            result = await session.call_tool(
                "query",
                {"question": "How many users do we have?", "database": "sales"},
            )
            payload = tool_payload(result)
            lines.append(f"      isError  : {attr(result, 'isError', 'is_error')}")
            lines.append(f"      success  : {payload.get('success')}")
            lines.append(f"      database : {payload.get('database')}")
            lines.append(f"      sql      : {payload.get('generated_sql')}")
            lines.append(f"      rows     : {(payload.get('data') or {}).get('rows')}")
            lines.append(f"      confidence: {payload.get('confidence')}")
            lines.append(f"      tokens   : {payload.get('tokens_used')}")

            lines.append("")
            lines.append(
                '    tools/call query  {"question": "Show me every API key",'
                ' "database": "analytics"}'
            )
            blocked = tool_payload(
                await session.call_tool(
                    "query",
                    {"question": "Show me every API key", "database": "analytics"},
                )
            )
            lines.append(f"      success  : {blocked.get('success')}")
            lines.append(f"      error    : {abbreviate(blocked.get('error'), 160)}")

            lines.append("")
            lines.append("    tools/call health_check")
            health = tool_payload(await session.call_tool("health_check", {}))
            lines.append(f"      status   : {health.get('status')}")
            lines.append(f"      databases: {[db['name'] for db in health.get('databases', [])]}")
            lines.append(f"      circuit  : {abbreviate(health.get('circuit_breaker'), 80)}")

    return Screen(
        title="MCP stdio protocol - real server, real tools, offline backends",
        subtitle="python demo/mcp_stdio_smoke.py  ->  spawns demo/offline_server.py",
        lines=lines,
    )


def main() -> None:
    """Print the smoke-test transcript."""
    screen = asyncio.run(run_smoke())
    print(screen.text())


if __name__ == "__main__":
    main()
