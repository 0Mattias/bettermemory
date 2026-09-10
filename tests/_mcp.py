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
class _FakeClientInfo:
    name: str | None = None
    version: str | None = None


@dataclass
class _FakeClientParams:
    client_info: _FakeClientInfo | None = None


@dataclass
class _FakeSession:
    """Stands in for `ServerSession`: `client_params` is what the identity
    resolver reads (the handshake's `clientInfo`); the roots surface is
    forged by the identity tests themselves."""

    client_params: _FakeClientParams | None = None


@dataclass
class _FakeRequest:
    """Stands in for the transport's request object — the SDK reads
    `request_context.request.headers`, and stdio has no request at all."""

    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _FakeRequestContext:
    meta: dict[str, Any] | None = None
    request: _FakeRequest | None = None
    session: _FakeSession | None = None
    request_id: str | None = None


@dataclass
class _FakeCtx:
    request_context: _FakeRequestContext


def fake_ctx(
    session_id: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    client_info: tuple[str | None, str | None] | None = None,
    request_id: str | None = None,
    with_request: bool = True,
) -> Any:
    """A stand-in for the SDK's request-scoped `Context`, typed `Any`.

    `identity.resolve` reads three paths off `ctx.request_context`:
    `request.headers` (the `x-bettermemory-*` declarations and the
    transport's `mcp-session-id`), `session.client_params.client_info`
    (the `initialize` handshake's clientInfo), and the attested principal
    through the SDK's auth contextvar, which no forged object can reach.
    A duck-typed object with those paths is enough for a unit test;
    building a real `Context` would mean standing up a whole request
    context to set one header.

    `session_id` forges the transport session (`mcp-session-id`), the
    discriminator two HTTP clients of one server differ on — what the
    session-registry tests vary per "client". `headers` adds any other
    header verbatim; `client_info` is `(name, version)`.

    `with_request=False` forges the stdio shape: no request object, so no
    headers at all — the resolver must treat that as "nothing declared",
    not as an error, and bucket the call into the default session.

    It lives HERE, rather than in each test module, because it is
    knowledge about the SDK's request shape and that knowledge has moved
    twice: mcp 1.x exposed a `Context.client_id` property, 2.x left the
    key reachable only through the request's metadata map (which no
    client sends), and 7.10.0 retired that key for the resolved actor.

    Returned as `Any` so strict mypy accepts it where `for_request` expects
    a real `Context`; the stand-in is structurally compatible and the cast
    is purely a type-checker concession.
    """
    merged: dict[str, str] = {}
    if session_id is not None:
        merged["mcp-session-id"] = session_id
    if headers:
        merged.update(headers)
    request = _FakeRequest(headers=merged) if with_request else None
    session = None
    if client_info is not None:
        name, version = client_info
        session = _FakeSession(
            client_params=_FakeClientParams(
                client_info=_FakeClientInfo(name=name, version=version)
            )
        )
    return _FakeCtx(
        request_context=_FakeRequestContext(
            meta={}, request=request, session=session, request_id=request_id
        )
    )


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
