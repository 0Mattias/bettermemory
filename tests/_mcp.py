"""One place that knows the MCP SDK's tool-invocation return shape.

Forty-one test modules used to carry their own byte-identical copy of the
unpack below, and six more reach into a tool's schema attributes directly.
That is fine while the SDK's shape holds still and expensive the moment it
moves — and it did. `mcp` 2.0.0 removed `mcp.server.fastmcp` entirely (no
shim, no deprecation path, no overlap version), changed `call_tool`'s
return from a bare list or a 2-tuple to a `CallToolResult`, and renamed
`Tool.inputSchema` / `outputSchema` to snake_case. Measured against the
tree at 3.32.0, that was 44 unpack sites across 44 files and 39 attribute
reads across 6.

Routing every one of them through this module first is what made the port
a handful of edits in one file instead of 83 spread across the suite. The
helpers were written to accept BOTH majors so they were correct before the
bump and after it, with no flag day in between; now that the floor is
`mcp>=2.0.0` the 1.x accommodations are gone, because a branch no
installable configuration can reach is not compatibility, it is untested
code that reads like a promise.

`probe_server` still returns `Any`. With one major in play that is no
longer about hiding a branch — it is that the probe exists to be handed to
the schema helpers below and to private-attribute reach-throughs, none of
which want a narrowed type, and the two title-scrub oracles construct it
precisely because it is NOT the thing `build_server` returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer


@dataclass
class _FakeRequestContext:
    meta: dict[str, Any] | None = field(default=None)


@dataclass
class _FakeCtx:
    request_context: _FakeRequestContext


def fake_ctx(client_id: str | None = None, *, with_meta: bool = True) -> Any:
    """A stand-in for the SDK's request-scoped `Context`, typed `Any`.

    `SessionRegistry._key_for_ctx` reads exactly one thing —
    `ctx.request_context.meta["client_id"]` — so a duck-typed object with
    that path is enough for a unit test. Building a real `Context` would
    mean standing up a whole request context to set one key.

    It lives HERE, rather than in each test module, because it is knowledge
    about the SDK's request shape and that knowledge just moved: mcp 1.x
    exposed the id as a `Context.client_id` property, 2.x dropped the
    property and left the key reachable through `_meta`, an open TypedDict.
    Two test modules had byte-identical private copies of the old shape and
    both broke on that one change — the same duplication tax this module
    was created to stop paying for `call_tool`.

    `with_meta=False` forges the other real absence: a transport that sent
    no `_meta` at all, where `meta` is None rather than a map missing the
    key. Both must bucket into the default session, and they reach
    `_key_for_ctx` by different branches.

    Returned as `Any` so strict mypy accepts it where `for_request` expects
    a real `Context`; the stand-in is structurally compatible and the cast
    is purely a type-checker concession.
    """
    if not with_meta:
        return _FakeCtx(request_context=_FakeRequestContext(meta=None))
    meta: dict[str, Any] = {} if client_id is None else {"client_id": client_id}
    return _FakeCtx(request_context=_FakeRequestContext(meta=meta))


def probe_server(name: str) -> Any:
    """A bare SDK server object, for tests that need one outside `build_server`.

    Two title-scrub oracles and the footprint probe construct a pristine
    server to compare against the one this project builds — the point being
    that it never went through `_strip_schema_titles`. Centralising the
    construction here is what kept those files collectable across the port:
    they used to import the server class at module scope, so the rename
    would have failed them at COLLECTION, taking the scrub's only two
    guards down at exactly the moment the port needed them. `builder.py`
    fails the scrub silently by design, so that window mattered.
    """
    return MCPServer(name)


async def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a tool and return its structured payload.

    Prefers the structured result — every tool in this project returns a
    JSON object — and falls back to parsing the first text content block,
    which is what the SDK hands back for a tool with no output schema.

    Reads the `CallToolResult` mcp 2.x returns via `getattr` rather than an
    isinstance check, so this module needs no type import from `mcp` and
    cannot itself break on a rename. The 1.x shapes it used to accept — a
    bare `list[ContentBlock]` and a `(content, structured)` 2-tuple — are
    unreachable under the current floor and were dropped with it.
    """
    result = await server.call_tool(name, arguments)

    structured = getattr(result, "structured_content", None)
    content = getattr(result, "content", None)

    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def input_schema(tool: Any) -> dict[str, Any]:
    """A SERVED tool's JSON input schema.

    Served, meaning an element of `await server.list_tools()`. The registry
    object behind `_tool_manager.get_tool(name)` is a DIFFERENT type that
    carries the schema as `.parameters` and `.fn_metadata.output_schema`,
    and it has never had the attribute this reads — under 1.x or 2.x.
    Passing one here raises rather than silently returning the wrong thing.

    The WIRE spelling is `inputSchema` and always was: `mcp_types.MCPModel`
    sets an alias generator and the transport serialises `by_alias=True`,
    so a client sees the same bytes across both majors. Only the Python
    attribute moved, `inputSchema` -> `input_schema`, which is why the port
    was a minor and not a break.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        raise AttributeError(f"{tool!r} exposes no `input_schema`")
    return dict(schema)


def output_schema(tool: Any) -> dict[str, Any] | None:
    """A served tool's JSON output schema, or None when it has none.

    Same rename as :func:`input_schema`. `None` is a real answer here — a
    tool without a structured return carries no output schema — so an
    absent attribute and an attribute set to `None` are not distinguished,
    and this does not raise the way `input_schema` does.
    """
    schema = getattr(tool, "output_schema", None)
    return dict(schema) if schema is not None else None


__all__ = ["call_tool", "fake_ctx", "input_schema", "output_schema", "probe_server"]
