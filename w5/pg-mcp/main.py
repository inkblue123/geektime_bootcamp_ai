"""Entry point for running the PostgreSQL MCP Server.

This module exists so the project can be started either as a module
(``python -m pg_mcp``), through the installed console script (``pg-mcp``) or
directly from a checkout (``python main.py``).

The server speaks MCP over stdio, so it is normally launched by an MCP client
such as Claude Desktop rather than by hand. See ``QUICKSTART.md`` for the
supported ways to run and exercise it.

Example:
    >>> python main.py  # doctest: +SKIP
"""

from pg_mcp.__main__ import main

if __name__ == "__main__":
    main()
