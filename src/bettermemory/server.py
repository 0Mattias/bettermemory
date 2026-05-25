"""MCP server entry point and tool registration.

The full tool surface (mirrored in `prompts.SYSTEM_PROMPT_ADDENDUM` so
the consuming model sees an identical list):

- Retrieval: memory_search, memory_show, memory_list, memory_scope_overview
- Writing:   memory_write (+ _confirm / _cancel staged-write pair),
             memory_update
- Lifecycle: memory_remove, memory_restore, memory_list_tombstones
- Verification: memory_verify
- Curation:  memory_record_use, memory_health, memory_audit_turn,
             memory_rename_scope
- Session:   memory_scope_disable / memory_scope_enable

The handler implementations live on `ToolHandlers` in `_handlers.py`;
response-shape helpers live on `ResponseBuilder` in `_response.py`. This
module's `_register_tools` is the thin wiring layer: it instantiates one
of each per server, then binds the methods against the FastMCP instance.
Tests reach handlers via `mcp._tool_manager.get_tool(name).fn` — `fn`
ends up being the bound method, and `inspect.signature` strips `self`,
so the JSON schema and call surface are identical to direct registration.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from ._handlers import (
    DESC_MEMORY_AUDIT_TURN,
    DESC_MEMORY_HEALTH,
    DESC_MEMORY_LIST,
    DESC_MEMORY_LIST_TOMBSTONES,
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
from .origin import capture as capture_origin  # noqa: F401
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .session import (
    SessionSource,
    SessionState,
    get_default_registry,
)
from .store import Store


# ``capture_origin`` is unused inside this module — every live call site
# moved with the CLI extraction (the handlers in `_handlers.py` import
# their own binding from `.origin`). Kept importable here because
# `tests/test_server_origin.py` and `tests/test_server_commit_drift.py`
# defensively monkeypatch `bettermemory.server.capture_origin`; removing
# the binding would AttributeError on the patch even though the test's
# active code path never calls the symbol.


log = logging.getLogger("bettermemory")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


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
        recorder = Recorder(
            root=config.resolved_directory(),
            session_id=recorder_session_id,
            enabled=config.telemetry.enabled,
            max_bytes=config.telemetry.max_bytes,
            log_queries_verbatim=config.telemetry.log_queries_verbatim,
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
            "Verify before relying. When `staleness_verdict` isn't "
            "fresh, spot-check one claim; memory_verify(id, "
            "verified_paths=…) if it holds, memory_update first if "
            "drifted."
        ),
    )

    _register_tools(
        mcp, config=config, store=store, sessions=sessions, recorder=recorder
    )
    return mcp


def _resolve_semantic_provider_and_model(
    config: Config,
) -> tuple[str | None, str | None]:
    """Pick the active embedding provider + its model name from config.

    Returns `(provider, model_name)` where provider is `"torch"` /
    `"fastembed"` and model_name is the matching config knob's value.
    Returns `(None, None)` when no provider is available (neither
    extra installed AND `semantic_provider = "auto"`) — callers treat
    that as the Jaccard fallback signal.

    Honours `[behavior] semantic_provider` even when the corresponding
    extra isn't installed; the per-provider WARNING fires in
    `semantic.get_model` once the load attempt runs.
    """
    from .semantic import resolve_provider

    chosen = resolve_provider(config.behavior.semantic_provider)
    if chosen == "torch":
        return chosen, config.behavior.semantic_model_name
    if chosen == "fastembed":
        return chosen, config.behavior.semantic_model_fastembed
    return None, None


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when `semantic_dedup = true` and an
    extra is installed. Returns None otherwise — callers treat None as
    the Jaccard fallback signal. The first call after `semantic_dedup`
    is enabled pays the model-load cost (~1-2s); subsequent calls hit
    `semantic.get_model`'s in-memory cache.
    """
    if not config.behavior.semantic_dedup:
        return None
    from .semantic import Provider, get_model

    provider, model_name = _resolve_semantic_provider_and_model(config)
    if provider is None or model_name is None:
        # No extra installed and no explicit provider preference; let
        # get_model() emit its WARNING via the default torch path so
        # the user sees the install hint.
        return get_model(config.behavior.semantic_model_name)
    return get_model(model_name, provider=cast(Provider, provider))


def _configure_persistent_embeddings(config: Config, store: Store) -> None:
    """Hook the persistent embedding cache to the active store dir when
    semantic dedup is enabled. The cache file lives next to the events
    log and the memory bodies so it shares the same trust boundary —
    nothing new in the permissions story. No-op when semantic dedup is
    off; when off, the in-memory cache is unused too, so persistence
    would be a write-only cycle."""
    if not config.behavior.semantic_dedup:
        return
    from .semantic import Provider, configure_persistent_cache

    provider, model_name = _resolve_semantic_provider_and_model(config)
    if provider is None or model_name is None:
        # No active provider — leave the persistent cache disabled so
        # we don't create a `.embeddings.<model>.npz` file we'd never
        # hydrate from.
        return
    configure_persistent_cache(
        store.root, model_name, provider=cast(Provider, provider)
    )


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

    # Order matches the module docstring's tool list above so a reader
    # can scan top-to-bottom and see all eighteen tools at once.
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
    mcp.tool(name="memory_restore", description=DESC_MEMORY_RESTORE)(
        handlers.memory_restore
    )
    mcp.tool(name="memory_list_tombstones", description=DESC_MEMORY_LIST_TOMBSTONES)(
        handlers.memory_list_tombstones
    )

    mcp.tool(name="memory_verify", description=DESC_MEMORY_VERIFY)(
        handlers.memory_verify
    )

    mcp.tool(name="memory_record_use", description=DESC_MEMORY_RECORD_USE)(
        handlers.memory_record_use
    )
    mcp.tool(name="memory_health", description=DESC_MEMORY_HEALTH)(
        handlers.memory_health
    )
    mcp.tool(name="memory_audit_turn", description=DESC_MEMORY_AUDIT_TURN)(
        handlers.memory_audit_turn
    )
    mcp.tool(name="memory_rename_scope", description=DESC_MEMORY_RENAME_SCOPE)(
        handlers.memory_rename_scope
    )

    mcp.tool(name="memory_scope_disable", description=DESC_MEMORY_SCOPE_DISABLE)(
        handlers.memory_scope_disable
    )
    mcp.tool(name="memory_scope_enable", description=DESC_MEMORY_SCOPE_ENABLE)(
        handlers.memory_scope_enable
    )



# ---------------------------------------------------------------------------
# CLI entry point — thin re-export shim
# ---------------------------------------------------------------------------
#
# The argparse setup and every `_cli_*` subcommand handler moved into the
# `bettermemory.cli` package (audit finding H10). Two surface contracts
# this module still has to honour for back-compat:
#
# 1. The `bettermemory` console script (`[project.scripts]` in
#    `pyproject.toml`) was registered as `bettermemory.server:main`, and
#    every existing install ships that entry point. Re-exporting `main`
#    here keeps the script working without bumping pyproject — older
#    wheels already on PyPI still resolve.
# 2. `tests/test_export.py` monkeypatches `bettermemory.server.load_config`
#    and the test_server_origin / test_server_commit_drift suites
#    monkeypatch `bettermemory.server.capture_origin`. Both names stay
#    importable at this module path; the CLI helpers route their
#    `load_config()` call through `bettermemory.server` so the patch
#    still wins.
# 3. `tests/test_consolidate.py` and `tests/test_export.py` import
#    `_cli_export`, `_cli_consolidate_acknowledge_debt`, and
#    `_cli_consolidate_acknowledge_misses` directly from
#    `bettermemory.server`. The re-exports below preserve those import
#    paths after the move into `cli/`.


def main() -> None:
    """CLI entry point — delegates to ``bettermemory.cli:main``.

    Kept here so the historical ``bettermemory.server:main`` entry point
    (registered in ``pyproject.toml`` and pinned by every wheel already
    on PyPI) continues to resolve. New code should import from
    ``bettermemory.cli`` directly.
    """
    from .cli import main as _main

    _main()


# Re-exports for the test suite. `_cli_export` is exercised directly by
# `tests/test_export.py`; the two `_cli_consolidate_acknowledge_*`
# helpers by `tests/test_consolidate.py`. Pulling them through here lets
# the tests keep their `from bettermemory.server import …` lines without
# the refactor cascading into every test file.
from .cli.consolidate import (  # noqa: E402
    _cli_consolidate_acknowledge_debt,
    _cli_consolidate_acknowledge_misses,
)
from .cli.export import _cli_export  # noqa: E402


# Re-export the prompt for consumers who import the package. `load_config`
# and `capture_origin` are exposed so the test-monkeypatch contracts
# documented above pass mypy's `Module ... does not explicitly export
# attribute` check.
__all__ = [
    "build_server",
    "main",
    "SYSTEM_PROMPT_ADDENDUM",
    "load_config",
    "capture_origin",
    "_cli_export",
    "_cli_consolidate_acknowledge_debt",
    "_cli_consolidate_acknowledge_misses",
]
