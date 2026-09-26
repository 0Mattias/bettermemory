"""MCP SDK wiring layer — instantiate a server, bind every tool.

Pre-Round-3 ``build_server`` and ``_register_tools`` lived in
``server.py``. The colocation was historical: ``server.py`` was both the
MCP-wiring module and the CLI ``main()`` shim. After the CLI extraction
into ``bettermemory.cli`` (Round-2 audit finding H10) the CLI package
needed ``build_server`` from ``cli/serve.py:run_serve``; a top-level
``from ..server import build_server`` would have re-opened a load-time
cycle because ``server.py`` imports back from ``cli.consolidate`` and
``cli.export`` at the bottom to preserve historical test re-exports.
That cycle was previously dodged with a lazy ``from ..server import
build_server`` inside ``run_serve`` and a comment explaining it. This
module is the structural fix: lifting the wiring helpers to a sibling
of both ``server.py`` and ``cli/`` means ``cli/serve.py`` can do a
top-level ``from ..builder import build_server`` with no cycle, and
``server.py`` is left with the CLI shim + the historical re-export
surface only.

What's here:

* ``build_server(...)``: the entry point both tests and the CLI call.
  Takes optional ``config`` / ``store`` / ``state`` / ``recorder``
  injections (tests use them for hermeticity; ``run_serve`` lets
  ``load_config`` resolve everything).
* ``_register_tools(mcp, ...)``: binds each ``ToolHandlers`` method
  against the ``MCPServer`` instance, one ``mcp.tool(...)`` call per tool.

``server.py`` re-exports ``build_server`` so any out-of-tree caller
and the full test suite (forty+ files import ``from bettermemory.server
import build_server``) keeps working without churn.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from ._handlers import (
    DESC_EPISODE,
    DESC_MEMORY_ADMIN,
    DESC_MEMORY_RECORD_USE,
    DESC_MEMORY_REMOVE,
    DESC_MEMORY_SEARCH,
    DESC_MEMORY_SHOW,
    DESC_MEMORY_UPDATE,
    DESC_MEMORY_VERIFY,
    DESC_MEMORY_WRITE,
    ToolHandlers,
)
from . import identity
from ._response import ResponseBuilder
from .config import Config, load_config
from .events import Recorder
from .session import (
    SessionSource,
    SessionState,
    get_default_registry,
)
from .store import STORE_FILENAME, Store, StoreSource


log = logging.getLogger("bettermemory")


# The server-level instructions block. Every MCP client surfaces it at
# the system-prompt level; Claude Code truncates it past about 1.8 KB,
# so it carries the contract and nothing else. The nine tool
# descriptions say what each tool does; the addendum in `prompts.py`
# carries the long-form policy for clients that paste it in.
INSTRUCTIONS = (
    "Persistent memory between sessions lives in this server's tools; "
    "keep it there, not in ad-hoc files beside them, since later sessions "
    "see only what these tools hold.\n\n"
    "Retrieval is opt-in: nothing stored is in your context until you "
    "call memory_search. Search when the user references shared context "
    "you lack or a request is ambiguous in a way stored preferences could "
    "resolve; skip generic and self-contained questions.\n\n"
    "Writing is proactive: call memory_write whenever something durable "
    "enters the conversation (a preference, a decision, a tool or config "
    "fact, finished work with its why); do not wait to be asked. A "
    "refusal is cheap: rephrase, or pass the acknowledge flag it names.\n\n"
    "Verify before relying: when a hit's staleness_verdict is not fresh, "
    "check one claim, then memory_verify if it holds or memory_update if "
    "it drifted.\n\n"
    "When a stored memory shapes your reply, say so briefly. Retrievals "
    "settle as applied on their own. At a loop iteration's entry, call "
    "episode(action='handoff') first."
)


def build_server(
    *,
    config: Config | None = None,
    store: Store | None = None,
    state: SessionState | SessionSource | None = None,
    recorder: Recorder | None = None,
    # The per-request store seam. `None` means "every request is served
    # from `store`", which is the behaviour every caller has today.
    store_source: StoreSource | None = None,
) -> MCPServer:
    """Return a configured `MCPServer` instance.

    Tests pass in their own `store`, `state`, and `recorder` to keep
    things hermetic. The real entry point in `main()` lets
    `load_config` resolve everything. When `recorder` is None, one is
    constructed from `config` — `enabled=False` in the telemetry
    config makes every event a no-op.

    The `state` argument accepts two shapes:

    * A bare `SessionState` (back-compat / single-client tests):
      every request resolves to the same state regardless of the
      client id the request carries. The MVP single-process/stdio
      assumption — what every test in the suite still uses.
    * A `SessionRegistry` (multi-client): each distinct
      client id gets its own `SessionState`, so pending
      writes / disabled scopes / use-tokens from one MCP client
      can't leak into another. `main()` uses the
      process-wide `get_default_registry()` for production runs.

    Passing `state=None` defaults to the process-wide registry. The
    recorder's `session_id` is still a process-level audit tag
    (per-client event correlation is a separate concern); for the
    common stdio case the recorder session_id matches the resolved
    state's session_id, so this is identical to the old behavior.

    `store_source` is the store's counterpart to `state`, and it is
    deliberately NOT symmetric with it yet: `state` resolves a
    different object per client, while the shipped
    `DefaultStoreSource` returns one store for every request. The
    parameter exists so the seam is reachable and testable; the
    tenancy policy that would use it is a later unit.
    """
    config = config or load_config()
    store = store or Store.open_or_create(config.resolved_directory() / STORE_FILENAME)
    sessions: SessionSource = state if state is not None else get_default_registry()
    if recorder is None:
        # The recorder needs a stable session_id at construction time;
        # in the multi-client SessionRegistry case there isn't one
        # canonical session_id (each client has its own), so we read
        # the "default" (no-ctx) state to get something stable. Single-
        # client tests pass a SessionState directly and get that same
        # state's session_id — unchanged from the pre-registry behavior.
        recorder_session_id = sessions.for_request(None).session_id
        # Capture the server's worktree once at construction (stable for
        # the process lifetime) so events carry it for episode_handoff's
        # worktree match (queue #28). Routed through the `_handlers`
        # shim to honor the test-monkeypatch contract other origin
        # captures use.
        from . import _handlers as _h

        recorder = Recorder(
            store=store,
            session_id=recorder_session_id,
            enabled=config.telemetry.enabled,
            worktree_root=_h.capture_origin().worktree_root,
        )

    # Imported lazily, for the same reason `_handlers` is above: `__init__`
    # imports this module before it binds `__version__`, so a module-level
    # `from . import __version__` is a circular import. By call time the
    # package is fully initialised. Reading it from one place rather than
    # re-deriving it here keeps `serverInfo.version` and
    # `bettermemory.__version__` from being able to disagree.
    from . import __version__

    mcp = MCPServer(
        "bettermemory",
        # `version` is what a client receives as `serverInfo.version` on
        # `initialize`, and it must be passed explicitly. mcp 1.x's
        # lowlevel server defaulted an unset version to the *mcp package's*
        # own version, so this server spent its whole 1.x life reporting
        # the SDK's version as its own — wrong, but non-empty and easy to
        # miss. 2.x replaced that fallback with `version: str = ""`, which
        # turns the same omission into an empty string on the wire. Neither
        # is the right answer; this project's own version is, and passing
        # it makes the field mean what its name says.
        version=__version__,
        # The server-level instructions block is the canonical "what is
        # this server" message every MCP client surfaces at the
        # system-prompt level. Empirically validated on Claude Code
        # 2.1.x: the block lands in the "MCP Server Instructions"
        # section of the system prompt. Claude Code truncates the block
        # if it exceeds roughly 1.8KB. The cut is mid-sentence, with
        # an ellipsis. Keep this body comfortably under that ceiling
        # (~1500 chars is the working budget). Detail beyond what fits
        # belongs on the individual tool descriptions, which are not
        # subject to the same truncation. The optional system-prompt
        # addendum (`docs/system_prompt.md` /
        # `bettermemory.SYSTEM_PROMPT_ADDENDUM`) carries the long form
        # for clients that want it pasted into a project CLAUDE.md.
        # The instructions-length regression test in tests/test_server.py
        # guards the budget.
        instructions=INSTRUCTIONS,
    )

    # Wire-path identity (`identity.middleware`): before a `tools/call`
    # reaches its handler, ask a roots-capable client where it works —
    # once per connection — and publish the resolved caller for the
    # request's task. In-process callers never pass through here; they
    # reach the same resolution through `sessions.for_request`.
    mcp.middleware.append(identity.middleware)

    _register_tools(
        mcp,
        config=config,
        store=store,
        sessions=sessions,
        recorder=recorder,
        store_source=store_source,
    )
    return mcp


def _register_tools(
    mcp: MCPServer,
    *,
    config: Config,
    store: Store,
    sessions: SessionSource,
    recorder: Recorder,
    store_source: StoreSource | None = None,
) -> None:
    """Bind each `ToolHandlers` method against the `MCPServer` instance.

    `sessions` is the SessionSource captured by every handler. Each
    handler resolves its per-request `state` by calling
    `sessions.for_request(ctx)` at entry, before `_advance_turn` —
    either the same shared SessionState (when a bare SessionState
    is passed) or the per-client SessionState (when a SessionRegistry
    is passed). The handler body uses the resolved `state` exactly
    as before; the routing layer is invisible past the entry line.
    """
    responses = ResponseBuilder(
        stale_after_days=config.behavior.verification_stale_days
    )
    handlers = ToolHandlers(
        config=config,
        store=store,
        sessions=sessions,
        recorder=recorder,
        responses=responses,
        store_source=store_source,
    )

    mcp.tool(name="memory_search", description=DESC_MEMORY_SEARCH)(
        handlers.memory_search
    )
    mcp.tool(name="memory_show", description=DESC_MEMORY_SHOW)(handlers.memory_show)
    mcp.tool(name="memory_write", description=DESC_MEMORY_WRITE)(handlers.memory_write)
    mcp.tool(name="memory_update", description=DESC_MEMORY_UPDATE)(
        handlers.memory_update
    )
    mcp.tool(name="memory_remove", description=DESC_MEMORY_REMOVE)(
        handlers.memory_remove
    )
    mcp.tool(name="memory_verify", description=DESC_MEMORY_VERIFY)(
        handlers.memory_verify
    )
    mcp.tool(name="memory_record_use", description=DESC_MEMORY_RECORD_USE)(
        handlers.memory_record_use
    )
    mcp.tool(name="episode", description=DESC_EPISODE)(handlers.episode)
    mcp.tool(name="memory_admin", description=DESC_MEMORY_ADMIN)(handlers.memory_admin)

    _strip_schema_titles(mcp)


def _strip_titles(node: object) -> None:
    """Delete pydantic's auto-generated `title` annotations and the
    information-free `default: null` entries, in place.

    `title` in JSON Schema is a display annotation: nothing validates
    against it, and no client behaviour depends on it. Pydantic emits one
    per property (`content` -> `"title": "Content"`) plus one per schema
    (`"title": "memory_writeArguments"`), and every byte of that ships to
    every client on every turn. `default: null` is the same kind of byte
    on every optional parameter.

    Structure-aware on purpose. Values under `properties` / `$defs` /
    `definitions` are keyed by CALLER-CHOSEN names, so a parameter
    literally named `title` would be deleted from the wire by a naive
    recursive walk — the schema would keep validating and the parameter
    would silently stop being advertised. Those maps are descended into
    by value only; `title` is removed from schema NODES. No tool has a
    parameter by that name today, which is exactly why the guard has to
    be structural rather than a name check.
    """
    if isinstance(node, dict):
        node.pop("title", None)
        # `default: null` on an optional parameter says nothing the
        # schema does not already say (absent from `required`, `null`
        # admitted), so it goes the same way; a default that carries a
        # value, such as `false` or `30`, stays.
        if "default" in node and node["default"] is None:
            del node["default"]
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions") and isinstance(
                value, dict
            ):
                for sub in value.values():
                    _strip_titles(sub)
            else:
                _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


def _strip_schema_titles(mcp: MCPServer) -> None:
    """Scrub the served schemas after registration.

    There is no SDK hook: `Tool.from_function` hard-codes
    `parameters = arg_model.model_json_schema(by_alias=True)`, and
    `MCPServer.list_tools` serves that dict verbatim as the served
    tool's `input_schema`. So the only place to do this is the
    registry, after the fact.

    Two deliberate choices:

    * Both legs are mutated IN PLACE rather than replaced. The MCP
      `Tool.output_schema` is a `cached_property` over
      `fn_metadata.output_schema`. Measured on the installed SDK: the
      cache is COLD at this point (nothing reads `Tool.output_schema`
      between `add_tool` and here), so assigning a new dict would in fact
      work today — but it would work by accident of ordering. On a warm
      cache assignment is silently ignored and the titles ship, while
      in-place mutation is correct either way. The difference is pinned
      against the SDK by
      `test_assigning_a_new_output_schema_would_be_silently_ignored` in
      `tests/test_schema_title_scrub.py`, so the reason this is not
      written the more obvious way stays checkable rather than folklore.
    * The output leg is scrubbed, NOT removed. Registering with
      `structured_output=False` would drop `structuredContent` from every
      tool result — a wire-shape change. `FuncMetadata.convert_result`
      only tests `output_schema is not None` and validates through
      `output_model`, never through this dict, so scrubbing keeps
      structured output on and keeps it validating.

    Both accesses stay feature-detected rather than pinned to an SDK
    version, and that is unchanged by the 2.x port even though the floor
    moved. `_tool_manager._tools` is private; the whole reach-through
    survived the major intact (verified attribute by attribute against a
    real 2.0.0 install), which is evidence that it is stable, not that it
    is guaranteed. The floor is `mcp>=2.0.0,<3.0.0`, and the reason it
    reads that way is the port — `mcp.server.fastmcp` does not exist in
    2.x and there is no overlap version, so this module could not import
    a server class at all without moving. What has NOT changed is the
    manner the floor binds: raising it to protect a size optimisation
    would still be a real install-compat break in exchange for nothing.
    A floor moves only as a deliberate, announced, changelog'd act, never
    as a side effect of a tidy-up here. See
    `docs/incidents/2026-07-31-mcp-2-unbounded-constraint.md` for why the
    `<3.0.0` half is load-bearing rather than reflexive pessimism.

    A SILENT NO-OP is the failure mode that matters here: if a future SDK
    moves either attribute, this returns quietly and every served schema
    regrows by ~2.8k chars with no diff anywhere in this repo. Two guards
    watch for it — `tests/test_schema_title_scrub.py` measures that the
    scrub still finds something to scrub, and
    `tests/test_resident_footprint.py`'s remainder ceiling is set below
    the un-scrubbed total so the regrowth cannot fit under it.
    """
    registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
    if not isinstance(registry, dict):
        log.debug(
            "MCP tool registry is not at `_tool_manager._tools`; skipping the "
            "schema title scrub. Served schemas are correct, just larger."
        )
        return
    for tool in registry.values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            _strip_titles(params)
        output_schema = getattr(
            getattr(tool, "fn_metadata", None), "output_schema", None
        )
        if isinstance(output_schema, dict):
            _strip_titles(output_schema)


__all__ = ["build_server", "_register_tools"]
