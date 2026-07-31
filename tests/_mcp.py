"""One place that knows the MCP SDK's tool-invocation return shape.

Forty-one test modules used to carry their own byte-identical copy of the
unpack below, and six more reach into a tool's schema attributes directly.
That is fine while the SDK's shape holds still and expensive the moment it
moves — and it is about to. `mcp` 2.0.0 removes `mcp.server.fastmcp`
entirely (no shim, no deprecation path, no overlap version), changes
`call_tool`'s return from a bare list or a 2-tuple to a `CallToolResult`,
and renames `Tool.inputSchema` / `outputSchema` to snake_case. Measured
against this tree at 3.32.0, that is 44 unpack sites across 44 files and
39 attribute reads across 6.

Routing every one of them through this module first makes the port a
handful of edits in one file instead of 83 spread across the suite, and it
costs nothing in the meantime: the helpers below are deliberately written
to accept BOTH shapes, so they are correct under 1.x today and under 2.x
after the bump, with no flag day in between.

Nothing here is a compatibility shim for the PACKAGE. `src/` still imports
`mcp.server.fastmcp` directly and still breaks under 2.0.0 — that is the
port's job, and hiding it behind a fork of exported types across two
type-checkers is the thing the entry brief argues against. This is a test
convenience, and `probe_server` below is deliberately typed `Any` for the
same reason: a test probe may branch on what is installed, but no branched
TYPE may leak out of this module and into the suite's annotations.
"""

from __future__ import annotations

import json
from typing import Any


def probe_server(name: str) -> Any:
    """A bare SDK server object, for tests that need one outside `build_server`.

    Two title-scrub oracles and the footprint probe construct a pristine
    server to compare against the one this project builds — the point being
    that it never went through `_strip_schema_titles`. They imported
    `FastMCP` at module scope, so under mcp 2.0.0 (where
    `mcp.server.fastmcp` does not exist at all) both files fail to COLLECT,
    taking the scrub's only two guards down with them at exactly the moment
    the port needs them. `builder.py` fails the scrub silently by design, so
    that window matters.

    Returns `Any` on purpose. The branch below is a fact about the installed
    package, not a type the suite should reason about, and a union leaking
    into annotations is how a two-line probe turns into the permanently
    forked type surface the port is trying to avoid.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # pragma: no cover - exercised only under mcp 2.x
        # Unresolvable while mcp 1.x is the installed major, which is
        # exactly the situation the ignore is for; it becomes live and the
        # other branch's becomes dead the moment the floor moves.
        from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

        return MCPServer(name)
    return FastMCP(name)


async def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a tool and return its structured payload.

    Prefers the structured result — every tool in this project returns a
    JSON object — and falls back to parsing the first text content block,
    which is what the older SDKs hand back for a tool with no output
    schema.

    Accepts every shape the SDK has returned or is about to:

    * ``(content, structured)`` — mcp 1.x `FastMCP.call_tool`.
    * a bare ``list[ContentBlock]`` — the same method's other 1.x branch.
    * an object with ``.structured_content`` / ``.content`` — the
      ``CallToolResult`` mcp 2.0.0 returns.

    Discriminated by shape rather than by an SDK version check, so it needs
    no import from `mcp` and cannot itself break on the rename.
    """
    result = await server.call_tool(name, arguments)

    structured: Any = None
    content: Any = None
    if isinstance(result, tuple):
        content, structured = result
    elif isinstance(result, list):
        content = result
    else:
        # `CallToolResult`-shaped. `getattr` rather than an isinstance
        # check so this module never imports from `mcp` — the whole point
        # is that it survives the package being reorganised under it.
        structured = getattr(result, "structured_content", None)
        content = getattr(result, "content", None)

    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def input_schema(tool: Any) -> dict[str, Any]:
    """A SERVED tool's JSON input schema, under either attribute name.

    Served, meaning an element of `await server.list_tools()`. The registry
    object behind `_tool_manager.get_tool(name)` is a DIFFERENT type that
    carries the schema as `.parameters` and `.fn_metadata.output_schema`,
    and it has never had either attribute this reads — under 1.x or 2.0.0.
    Passing one here raises rather than silently returning the wrong thing.

    1.x spells it `inputSchema`, 2.0.0 spells it `input_schema`. The WIRE
    is unchanged in both — `mcp_types.MCPModel` sets an alias generator and
    the transport serialises `by_alias=True`, so a client sees `inputSchema`
    either way. Only the Python attribute moved.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    if schema is None:
        raise AttributeError(
            f"{tool!r} exposes neither `input_schema` nor `inputSchema`"
        )
    return dict(schema)


def output_schema(tool: Any) -> dict[str, Any] | None:
    """A registered tool's JSON output schema, or None when it has none.

    Same rename as :func:`input_schema`. `None` is a real answer here — a
    tool without a structured return carries no output schema — so an
    absent attribute and an attribute set to `None` are not distinguished.
    """
    schema = getattr(tool, "output_schema", None)
    if schema is None:
        schema = getattr(tool, "outputSchema", None)
    return dict(schema) if schema is not None else None


__all__ = ["call_tool", "input_schema", "output_schema"]
