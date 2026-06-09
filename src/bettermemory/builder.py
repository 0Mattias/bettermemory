"""FastMCP wiring layer — instantiate a server, bind every tool.

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
  against the FastMCP instance, one ``mcp.tool(...)`` call per tool.

``server.py`` re-exports ``build_server`` so any out-of-tree caller
and the full test suite (forty+ files import ``from bettermemory.server
import build_server``) keeps working without churn.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from ._handlers import (
    DESC_EPISODE_HANDOFF,
    DESC_EPISODE_PROMOTE,
    DESC_EPISODE_SEARCH,
    DESC_EPISODE_WRITE,
    DESC_MEMORY_ACKNOWLEDGE_MISS,
    DESC_MEMORY_AUDIT_TURN,
    DESC_MEMORY_CURATE,
    DESC_MEMORY_HEALTH,
    DESC_MEMORY_LIST,
    DESC_MEMORY_LIST_TOMBSTONES,
    DESC_MEMORY_PROPOSALS,
    DESC_MEMORY_RECORD_USE,
    DESC_MEMORY_REMOVE,
    DESC_MEMORY_RENAME_SCOPE,
    DESC_MEMORY_RESTORE,
    DESC_MEMORY_SCOPE_DISABLE,
    DESC_MEMORY_SCOPE_ENABLE,
    DESC_MEMORY_SCOPE_OVERVIEW,
    DESC_MEMORY_SEARCH,
    DESC_MEMORY_SHOW,
    DESC_MEMORY_UPDATE,
    DESC_MEMORY_VERIFY,
    DESC_MEMORY_WRITE,
    DESC_MEMORY_WRITE_CANCEL,
    DESC_MEMORY_WRITE_CONFIRM,
    ToolHandlers,
)
from ._response import ResponseBuilder
from .config import Config, load_config
from .events import Recorder
from .semantic_setup import (
    _configure_persistent_embeddings,
    _semantic_model_or_none,
)
from .session import (
    SessionSource,
    SessionState,
    get_default_registry,
)
from .store import Store


log = logging.getLogger("bettermemory")


def build_server(
    *,
    config: Config | None = None,
    store: Store | None = None,
    state: SessionState | SessionSource | None = None,
    recorder: Recorder | None = None,
) -> FastMCP:
    """Return a configured FastMCP instance.

    Tests pass in their own `store`, `state`, and `recorder` to keep
    things hermetic. The real entry point in `main()` lets
    `load_config` resolve everything. When `recorder` is None, one is
    constructed from `config` — `enabled=False` in the telemetry
    config makes every event a no-op.

    The `state` argument accepts two shapes:

    * A bare `SessionState` (back-compat / single-client tests):
      every request resolves to the same state regardless of the
      FastMCP `Context.client_id`. The MVP single-process/stdio
      assumption — what every test in the suite still uses.
    * A `SessionRegistry` (multi-client): each distinct
      `Context.client_id` gets its own `SessionState`, so pending
      writes / disabled scopes / use-tokens from one MCP client
      can't leak into another. `main()` uses the
      process-wide `get_default_registry()` for production runs.

    Passing `state=None` defaults to the process-wide registry. The
    recorder's `session_id` is still a process-level audit tag
    (per-client event correlation is a separate concern); for the
    common stdio case the recorder session_id matches the resolved
    state's session_id, so this is identical to the old behavior.
    """
    config = config or load_config()
    store = store or Store(config.resolved_directory())
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
            root=config.resolved_directory(),
            session_id=recorder_session_id,
            enabled=config.telemetry.enabled,
            max_bytes=config.telemetry.max_bytes,
            log_queries_verbatim=config.telemetry.log_queries_verbatim,
            worktree_root=_h.capture_origin().worktree_root,
        )

    # Wire the persistent embedding cache to this store's directory. The
    # configure call doesn't load anything from disk yet; hydration is
    # lazy on the first cached_embed call so non-semantic-dedup sessions
    # never touch the file.
    _configure_persistent_embeddings(config, store)

    mcp = FastMCP(
        "bettermemory",
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
        instructions=(
            "Persistent memory between sessions lives in this server's "
            "MCP tools (listed below). Don't fragment memory across "
            "ad-hoc files alongside; future sessions only see what "
            "these tools surface.\n\n"
            "Memory is OPT-IN retrieval. Stored memories are NOT in "
            "your context unless you call memory_search. Default to "
            "NOT retrieving — false positives hurt more than false "
            "negatives. Call only when the user references shared "
            'context ("my project", "the script we wrote") or a '
            "request is ambiguous in a way stored preferences could "
            "resolve. Skip generic factual / self-contained "
            "technical questions.\n\n"
            "Writing is the OPPOSITE axis: PROACTIVE. memory_write is "
            "a routine reflex — reach for it whenever something "
            "durable enters the conversation. Triggers: user states a "
            "preference (→ category='user-inference', stages pending); "
            "a project decision user concurred with (→ "
            "category='fact', commits, announce); a tool/infra/config "
            "fact; a unit of work finishes with a why git won't "
            'capture. Don\'t wait for "remember that" — your job is to '
            "capture. Rejects are cheap — re-issue with the suggested "
            "fix or pass `acknowledge_*`. Writes that bounce are not "
            "failures.\n\n"
            "Session-start: memory_scope_overview returns counts plus "
            "curation_pending. If total=0, skip memory_search unless "
            "asked. memory_search auto-scopes to caller's repo + "
            "worktree.\n\n"
            "When a retrieved memory shapes your reply, say so briefly "
            '("Using your stored preference for…"). memory_record_use '
            "auto-commits as `applied` ~2 turns later; call to "
            "override.\n\n"
            "Verify before relying. When staleness_verdict isn't fresh, "
            "spot-check; memory_verify if it holds, memory_update if "
            "drifted.\n\n"
            "For /loop iterations: episode_handoff at entry, "
            "episode_write(takeaway=…) at exit."
        ),
    )

    _register_tools(
        mcp, config=config, store=store, sessions=sessions, recorder=recorder
    )
    return mcp


def _register_tools(
    mcp: FastMCP,
    *,
    config: Config,
    store: Store,
    sessions: SessionSource,
    recorder: Recorder,
) -> None:
    """Bind each `ToolHandlers` method against the FastMCP instance.

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
        semantic_model_factory=_semantic_model_or_none,
    )

    # Order matches `server.py`'s module docstring's tool list so a reader
    # can scan top-to-bottom. The DEFAULT surface is lean: the curation /
    # power-user tools at the bottom are gated behind `[behavior]
    # full_tool_surface` so the typical client doesn't pay their (long)
    # descriptions in context on every turn. See BehaviorConfig.
    # full_tool_surface for the rationale and the dogfood measurement.
    mcp.tool(name="memory_search", description=DESC_MEMORY_SEARCH)(
        handlers.memory_search
    )
    mcp.tool(name="memory_show", description=DESC_MEMORY_SHOW)(handlers.memory_show)
    mcp.tool(name="memory_list", description=DESC_MEMORY_LIST)(handlers.memory_list)
    mcp.tool(name="memory_scope_overview", description=DESC_MEMORY_SCOPE_OVERVIEW)(
        handlers.memory_scope_overview
    )

    mcp.tool(name="memory_write", description=DESC_MEMORY_WRITE)(handlers.memory_write)
    mcp.tool(name="memory_write_confirm", description=DESC_MEMORY_WRITE_CONFIRM)(
        handlers.memory_write_confirm
    )
    mcp.tool(name="memory_write_cancel", description=DESC_MEMORY_WRITE_CANCEL)(
        handlers.memory_write_cancel
    )
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
    mcp.tool(name="memory_audit_turn", description=DESC_MEMORY_AUDIT_TURN)(
        handlers.memory_audit_turn
    )

    mcp.tool(name="memory_scope_disable", description=DESC_MEMORY_SCOPE_DISABLE)(
        handlers.memory_scope_disable
    )
    mcp.tool(name="memory_scope_enable", description=DESC_MEMORY_SCOPE_ENABLE)(
        handlers.memory_scope_enable
    )

    # Episode-tier tools — sibling to memory, journal-shaped writes for
    # run-state and iteration takeaways the durability gate rejects. Always
    # registered: /loop, audit-loop and curate-loop drive episode_handoff /
    # episode_write directly, and the server instructions reference them.
    mcp.tool(name="episode_write", description=DESC_EPISODE_WRITE)(
        handlers.episode_write
    )
    mcp.tool(name="episode_handoff", description=DESC_EPISODE_HANDOFF)(
        handlers.episode_handoff
    )
    mcp.tool(name="episode_search", description=DESC_EPISODE_SEARCH)(
        handlers.episode_search
    )
    mcp.tool(name="episode_promote", description=DESC_EPISODE_PROMOTE)(
        handlers.episode_promote
    )

    # `memory_proposals` is the UI for the opt-in [proposals] write-reflex
    # queue, so it surfaces whenever that feature is on — even under the lean
    # surface — and otherwise only under the full surface.
    if config.behavior.full_tool_surface or config.proposals.auto_propose:
        mcp.tool(name="memory_proposals", description=DESC_MEMORY_PROPOSALS)(
            handlers.memory_proposals
        )

    # Curation / power-user tools — gated out of the lean default surface.
    # Each had 0-8 organic calls across 190 dogfood sessions and is reachable
    # via the `bettermemory` CLI. The curate-loop skill drives memory_health /
    # memory_acknowledge_miss / memory_restore as MCP tools, so it requires
    # `full_tool_surface = true`.
    if config.behavior.full_tool_surface:
        mcp.tool(name="memory_restore", description=DESC_MEMORY_RESTORE)(
            handlers.memory_restore
        )
        mcp.tool(
            name="memory_list_tombstones", description=DESC_MEMORY_LIST_TOMBSTONES
        )(handlers.memory_list_tombstones)
        mcp.tool(name="memory_health", description=DESC_MEMORY_HEALTH)(
            handlers.memory_health
        )
        mcp.tool(name="memory_curate", description=DESC_MEMORY_CURATE)(
            handlers.memory_curate
        )
        mcp.tool(
            name="memory_acknowledge_miss", description=DESC_MEMORY_ACKNOWLEDGE_MISS
        )(handlers.memory_acknowledge_miss)
        mcp.tool(name="memory_rename_scope", description=DESC_MEMORY_RENAME_SCOPE)(
            handlers.memory_rename_scope
        )


__all__ = ["build_server", "_register_tools"]
