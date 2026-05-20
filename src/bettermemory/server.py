"""MCP server entry point and tool registration.

The full tool surface (mirrored in `prompts.SYSTEM_PROMPT_ADDENDUM` so
the consuming model sees an identical list):

- Retrieval: memory_search, memory_show, memory_list, memory_scope_overview
- Writing:   memory_write (+ _confirm / _cancel staged-write pair),
             memory_update
- Lifecycle: memory_remove, memory_restore, memory_list_tombstones
- Verification: memory_verify
- Curation:  memory_record_use, memory_health, memory_rename_scope
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
import sys
from typing import Any

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
from ._response import ResponseBuilder, isoformat
from .config import Config, load_config
from .events import Recorder
from .health import report_for_directory
from .models import utcnow, validate_scope
from .origin import capture as capture_origin
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .session import (
    SessionSource,
    SessionState,
    get_default_registry,
)
from .store import Store


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
            "resolve. Skip generic factual or self-contained "
            "technical questions.\n\n"
            "Writing is the OPPOSITE axis: PROACTIVE. memory_write is "
            "a routine reflex — reach for it whenever something "
            "durable enters the conversation. Triggers: user states a "
            "preference (→ category='user-inference', server stages "
            "pending); a project decision the user concurred with (→ "
            "category='fact', commits immediately, announce); a "
            "tool/infra/config fact becomes part of the work; a unit "
            "of work finishes with a why git won't capture. Don't wait "
            'for "remember that" — the user pays you to forget. '
            "Durability check, dedup, and pending tier are guardrails; "
            "your job is to capture.\n\n"
            "Session-start: memory_scope_overview returns counts plus "
            "curation_pending. If total=0, skip memory_search unless "
            "asked. memory_search auto-scopes to caller's repo.\n\n"
            "When a retrieved memory shapes your reply, say so briefly "
            '("Using your stored preference for…"). memory_record_use '
            "auto-commits as `applied` ~2 turns later; call explicitly "
            "to override.\n\n"
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


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when `semantic_dedup = true` and the
    extras are installed. Returns None otherwise — callers treat None as
    the Jaccard fallback signal. The first call after `semantic_dedup`
    is enabled pays the model-load cost (~1-2s); subsequent calls hit
    `semantic.get_model`'s in-memory cache.
    """
    if not config.behavior.semantic_dedup:
        return None
    from .semantic import get_model

    return get_model(config.behavior.semantic_model_name)


def _configure_persistent_embeddings(config: Config, store: Store) -> None:
    """Hook the persistent embedding cache to the active store dir when
    semantic dedup is enabled. The cache file lives next to the events
    log and the memory bodies so it shares the same trust boundary —
    nothing new in the permissions story. No-op when semantic dedup is
    off; when off, the in-memory cache is unused too, so persistence
    would be a write-only cycle."""
    if not config.behavior.semantic_dedup:
        return
    from .semantic import configure_persistent_cache

    configure_persistent_cache(store.root, config.behavior.semantic_model_name)


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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point. By default runs the MCP server over stdio
    (`bettermemory`). Subcommands provide offline tooling: `bettermemory
    health` prints the aggregate report, mirroring the `memory_health`
    tool in human-readable form."""
    import argparse

    from . import __version__

    parser = argparse.ArgumentParser(
        prog="bettermemory",
        description=(
            "Persistent memory for Claude Code, retrieved on demand. "
            "Run with no arguments to start the MCP server over stdio."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bettermemory {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd")

    health_parser = sub.add_parser(
        "health", help="Print the aggregate memory health report."
    )
    health_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    health_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Window in days for the dead-weight cutoff. Memories created "
            "more than this many days ago with no `applied` events are "
            "flagged. Default: 30."
        ),
    )
    health_parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="How many heavily-used memories to list. Default: 10.",
    )
    health_parser.add_argument(
        "--min-applied",
        type=int,
        default=None,
        help=(
            "Minimum applied_count for inclusion in heavily_used. Default "
            "comes from config.toml `behavior.heavily_used_min_applied` "
            "(typically 3). Lower to 1 on a fresh store to see anything "
            "that's been applied at least once."
        ),
    )

    doctor_parser = sub.add_parser(
        "doctor",
        help=(
            "Diagnose install state. Runs a series of checks: binary on "
            "PATH, config loadable, storage dir writable, memories parse, "
            "event log writable, semantic-dedup extras present (when "
            "enabled), MCP client configs cross-checked against the "
            "currently-resolved binary path. Exits 0/1/2 for ok/warn/fail."
        ),
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (machine-readable) instead of human-readable text.",
    )

    init_parser = sub.add_parser(
        "init",
        help=(
            "Onboard a fresh install: print the MCP config snippet, or "
            "auto-patch a known client's config. Idempotent."
        ),
    )
    init_parser.add_argument(
        "--client",
        type=str,
        default=None,
        choices=["claude-code", "claude-desktop", "cursor", "continue", "cline"],
        help=(
            "Auto-patch the named client's MCP config. Without this "
            "flag, init runs in show-and-tell mode: prints the snippet "
            "and the common config locations so you can copy by hand."
        ),
    )
    init_parser.add_argument(
        "--print-only",
        action="store_true",
        help=(
            "Just print the JSON snippet (and target path, when --client "
            "is set) without writing anything. Useful for piping into "
            "jq or for review before applying."
        ),
    )
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON view (binary path, snippet, known clients).",
    )
    init_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Server key under `mcpServers`. Default: `bettermemory` "
            "(specific enough to never collide with another MCP server). "
            "Override only if you have a strong reason — Claude Code's "
            "tool names are prefixed with this key."
        ),
    )
    init_parser.add_argument(
        "--with-addendum",
        action="store_true",
        help=(
            "Also print docs/system_prompt.md (the long-form policy). "
            "The MCP `instructions` block carries the core rules at "
            "the system-prompt level on every compliant client, but "
            "Claude Code truncates it at ~1.8KB. Print the addendum "
            "and paste into your CLAUDE.md to keep the writing-"
            "discipline / scope-hygiene / verification-ceremony "
            "detail in scope. The Claude Code plugin ships the same "
            "content as a SKILL.md — you don't need both."
        ),
    )
    init_parser.add_argument(
        "--config-path",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the default target file for --client. Use this "
            "to write into a project-scoped MCP config instead of the "
            "user-scoped default."
        ),
    )

    migrate_parser = sub.add_parser(
        "migrate",
        help=(
            "One-shot data migrations. Use `migrate origin` to backfill "
            "the origin field on memories written before that field "
            "existed."
        ),
    )
    migrate_sub = migrate_parser.add_subparsers(dest="migrate_cmd")
    origin_parser = migrate_sub.add_parser(
        "origin",
        help=(
            "Backfill origin frontmatter on legacy memories. Idempotent: "
            "memories that already have an origin field are skipped."
        ),
    )
    origin_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    origin_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help=(
            "Force-tag every legacy memory with this remote URL. Use "
            "when the auto-inference from the parent directory isn't "
            "right (e.g. global memory dir that you know belongs to one "
            "repo)."
        ),
    )
    origin_parser.add_argument(
        "--scope-repo",
        action="append",
        default=[],
        metavar="SCOPE=URL",
        help=(
            "Route memories by scope: tag any memory carrying SCOPE "
            "with the given remote URL. Repeat for multiple scopes "
            "(e.g. --scope-repo projects:foo=git@github.com:me/foo.git "
            "--scope-repo projects:bar=git@github.com:me/bar.git). "
            "Memories whose scopes match nothing in the map fall through "
            "to --repo (if given) or are left untagged. The right tool "
            "for a global memory dir whose memories already use "
            "projects:<name> tags."
        ),
    )

    export_parser = sub.add_parser(
        "export",
        help=(
            "Dump all active memories (and tombstones, by default) to a "
            "self-describing JSON document. The format is round-trippable "
            "and intended for backup, migration between machines, or "
            "feeding an external indexer. Writes to stdout unless "
            "--output is given."
        ),
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the export to PATH instead of stdout. Use this for "
            "scripted backups (`bettermemory export -o backup.json`)."
        ),
    )
    export_parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Skip tombstoned memories. By default the export includes "
            "them so a restored archive carries the same removal-reason "
            "audit trail; use this when you only want the live set."
        ),
    )
    export_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to memories tagged with at least one of the given "
            "scopes. Repeat to widen the filter. Applies to both active "
            "and tombstoned records."
        ),
    )

    tombstones_parser = sub.add_parser(
        "tombstones",
        help=(
            "Inspect and prune the tombstone (removed-memory) audit log. "
            "Subcommands: list, prune."
        ),
    )
    tombstones_sub = tombstones_parser.add_subparsers(dest="tombstones_cmd")

    tlist_parser = tombstones_sub.add_parser(
        "list", help="Print all tombstones with removal metadata."
    )
    tlist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    tlist_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to tombstones tagged with at least one of the given "
            "scopes. Repeat to widen the filter."
        ),
    )

    tprune_parser = tombstones_sub.add_parser(
        "prune",
        help=(
            "Hard-delete tombstones older than --older-than days. "
            "Active memories are unaffected. Default value comes from "
            "config.toml `behavior.tombstone_retention_days`; if that's 0 "
            "(the default), --older-than is required."
        ),
    )
    tprune_parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "Cutoff in days. Tombstones whose `removed` timestamp is older "
            "than this are deleted. Required if no default is configured."
        ),
    )
    tprune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    tprune_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    ui_parser = sub.add_parser(
        "ui",
        help=(
            "Run the local web UI (FastAPI). Read-mostly: browse "
            "memories, run memory_verify, see memory_health rollups. "
            "Requires the `[ui]` extra: pip install bettermemory[ui]."
        ),
    )
    ui_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help=(
            "Bind host. Default 127.0.0.1 (local only). Pass 0.0.0.0 "
            "to expose on a trusted network (the server logs a warning "
            "in that case since the UI surfaces curation data)."
        ),
    )
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port. Default: 8765.",
    )

    sync_parser = sub.add_parser(
        "sync",
        help=(
            "Sync the memory directory across hosts via git. Subcommands: "
            "init (set up the dir as a git repo + sensible .gitignore), "
            "status (show pending changes and remote tracking), "
            "push (commit + push), pull (rebase-pull + rebuild the index), "
            "auto (pull then push — the shell-alias / cron one-shot)."
        ),
    )
    sync_sub = sync_parser.add_subparsers(dest="sync_cmd")

    sync_init_parser = sync_sub.add_parser(
        "init", help="Initialise the memory dir as a git repo."
    )
    sync_init_parser.add_argument(
        "--remote",
        type=str,
        default=None,
        metavar="URL",
        help=(
            "Add (or update) `origin` to this remote URL. Without the "
            "flag, init only creates the repo + .gitignore — you can "
            "set the remote later with `git remote add origin <url>`."
        ),
    )
    sync_init_parser.add_argument(
        "--default-branch",
        type=str,
        default="main",
        help='Initial branch name. Default: "main".',
    )
    sync_init_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_status_parser = sync_sub.add_parser(
        "status", help="Show pending changes and remote tracking."
    )
    sync_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_push_parser = sync_sub.add_parser(
        "push", help="Stage everything, commit (if changes), push."
    )
    sync_push_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_push_parser.add_argument(
        "--message",
        "-m",
        type=str,
        default=None,
        help=(
            "Commit message. Default: `bettermemory: sync`. Override "
            "when scripting a sync after a known set of edits."
        ),
    )
    sync_push_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_pull_parser = sync_sub.add_parser(
        "pull", help="Rebase-pull + rebuild the FTS5 index."
    )
    sync_pull_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_pull_parser.add_argument(
        "--no-reindex",
        action="store_true",
        help=(
            "Skip the post-pull `reindex`. Useful in scripts that batch "
            "multiple sync operations and want to defer index rebuild "
            "to the end."
        ),
    )
    sync_pull_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_auto_parser = sync_sub.add_parser(
        "auto", help="Pull-rebase, then push. The shell-alias one-shot."
    )
    sync_auto_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_auto_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    reindex_parser = sub.add_parser(
        "reindex",
        help=(
            "Rebuild the SQLite FTS5 index from the on-disk memories. "
            "The index is normally kept live by Store hooks on every "
            "write / update / tombstone; rerun this when the memory "
            "directory was edited outside the runtime (hand-edits, "
            "external sync, restored backup) so the index catches up. "
            "Safe to run anytime — atomic, transactional, leaves the "
            "prior index intact on partial failure."
        ),
    )
    reindex_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    audit_turn_parser = sub.add_parser(
        "audit-turn",
        help=(
            "Run a silent-miss audit for the just-completed turn. "
            "Intended as a Claude Code Stop hook target: reads the "
            "hook's stdin JSON (`session_id`, `transcript_path`) and "
            "calls `audit.probe_for_miss` against the active store. "
            "Use --transcript-path + --session-id to invoke manually "
            "for debugging. Always exits 0 so a hook misfire never "
            "breaks the turn-end pipeline."
        ),
    )
    audit_turn_parser.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        help="Override the transcript path from the Stop hook payload.",
    )
    audit_turn_parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Override the session id from the Stop hook payload.",
    )
    audit_turn_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Skip the JSON summary on stdout. Events still land in the log.",
    )

    consolidate_parser = sub.add_parser(
        "consolidate",
        help=(
            "Offline consolidation: dedup near-duplicates, demote "
            "never-applied memories to ambient, suggest cold-scope "
            "archival and scope-typo renames. Dry-run by default; "
            "--apply commits dedup tombstones and demotions. Cold-"
            "scope and scope-typo passes stay suggest-only."
        ),
    )
    consolidate_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually commit dedup tombstones and category demotions "
            "to disk. Without this flag, the command prints what it "
            "would do without touching the store."
        ),
    )
    consolidate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    consolidate_parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help=(
            "Demotion window in days. Memories created more than this "
            "many days ago with retrieval count greater than zero and "
            "applied count of zero are proposed for demotion to ambient. "
            "Default: 30 (matches the dead-weight rule in memory_health)."
        ),
    )
    consolidate_parser.add_argument(
        "--cold-scope-days",
        type=int,
        default=180,
        help=(
            "Cold-scope cutoff in days. A scope whose newest memory is "
            "older than this AND with no applied events on any memory "
            "in the scope is suggested for archival. Suggest-only; "
            "auto-archiving a scope is too blunt without review. "
            "Default: 180."
        ),
    )
    consolidate_parser.add_argument(
        "--semantic-threshold",
        type=float,
        default=None,
        help=(
            "Cosine threshold for the semantic dedup pass (default 0.85). "
            "When the embeddings extra is not installed the pass falls "
            "back to Jaccard at 0.75 — this flag is ignored in that case."
        ),
    )
    consolidate_parser.add_argument(
        "--typo-distance",
        type=int,
        default=2,
        help=(
            "Levenshtein cutoff for the scope-typo detector. Default 2 "
            "catches one-character typos and small transpositions; "
            "raise to 3 to surface more pairs at the cost of false "
            "positives."
        ),
    )

    args = parser.parse_args()
    if args.cmd == "health":
        _cli_health(
            json_out=args.json,
            days=args.days,
            top_k=args.top_k,
            min_applied=args.min_applied,
        )
        return
    if args.cmd == "doctor":
        from .doctor import cli_doctor

        raise SystemExit(cli_doctor(json_out=args.json))
    if args.cmd == "init":
        from pathlib import Path as _Path

        from .init import cli_init

        cli_init(
            client=args.client,
            print_only=args.print_only,
            json_out=args.json,
            name=args.name,
            with_addendum=args.with_addendum,
            config_path=_Path(args.config_path) if args.config_path else None,
        )
        return
    if args.cmd == "migrate":
        if args.migrate_cmd == "origin":
            scope_repo_map: dict[str, str] = {}
            for entry in args.scope_repo:
                if "=" not in entry:
                    parser.error(f"--scope-repo expects SCOPE=URL, got: {entry!r}")
                scope, url = entry.split("=", 1)
                scope = scope.strip()
                url = url.strip()
                if not scope or not url:
                    parser.error(
                        f"--scope-repo expects non-empty SCOPE and URL, got: {entry!r}"
                    )
                scope_repo_map[scope] = url
            _cli_migrate_origin(
                dry_run=args.dry_run,
                force_repo=args.repo,
                scope_repo_map=scope_repo_map,
            )
            return
        migrate_parser.print_help()
        return
    if args.cmd == "tombstones":
        if args.tombstones_cmd == "list":
            _cli_tombstones_list(json_out=args.json, scopes=args.scope or None)
            return
        if args.tombstones_cmd == "prune":
            _cli_tombstones_prune(
                older_than_days=args.older_than,
                dry_run=args.dry_run,
                json_out=args.json,
                parser=parser,
            )
            return
        tombstones_parser.print_help()
        return
    if args.cmd == "export":
        _cli_export(
            output=args.output,
            include_tombstones=not args.no_tombstones,
            scopes=args.scope or None,
        )
        return
    if args.cmd == "ui":
        _cli_ui(host=args.host, port=args.port)
        return
    if args.cmd == "reindex":
        _cli_reindex(json_out=args.json)
        return
    if args.cmd == "sync":
        if args.sync_cmd == "init":
            _cli_sync_init(
                remote=args.remote,
                default_branch=args.default_branch,
                json_out=args.json,
            )
            return
        if args.sync_cmd == "status":
            _cli_sync_status(json_out=args.json)
            return
        if args.sync_cmd == "push":
            _cli_sync_push(
                remote=args.remote,
                message=args.message,
                json_out=args.json,
            )
            return
        if args.sync_cmd == "pull":
            _cli_sync_pull(
                remote=args.remote,
                reindex=not args.no_reindex,
                json_out=args.json,
            )
            return
        if args.sync_cmd == "auto":
            _cli_sync_auto(remote=args.remote, json_out=args.json)
            return
        sync_parser.print_help()
        return
    if args.cmd == "consolidate":
        _cli_consolidate(
            apply=args.apply,
            json_out=args.json,
            window_days=args.window_days,
            cold_scope_days=args.cold_scope_days,
            semantic_threshold=args.semantic_threshold,
            typo_distance=args.typo_distance,
        )
        return
    if args.cmd == "audit-turn":
        from .hook import main as _hook_main

        raise SystemExit(
            _hook_main(
                [
                    *(
                        ["--transcript-path", args.transcript_path]
                        if args.transcript_path
                        else []
                    ),
                    *(["--session-id", args.session_id] if args.session_id else []),
                    *(["--quiet"] if args.quiet else []),
                ]
            )
        )

    _cli_serve()


def _cli_serve() -> None:
    """The default no-arg behaviour: run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    log.info("memory directory: %s", directory)
    log.info(
        "telemetry: %s (event log at %s/.events.jsonl)",
        "on" if config.telemetry.enabled else "off",
        directory,
    )
    log.info(
        "system prompt: server-level MCP `instructions` block carries "
        "the core policy; on Claude Code the block is truncated at "
        "~1.8KB so the long-form addendum (docs/system_prompt.md) or "
        "the plugin's SKILL.md carries the writing-discipline / "
        "scope-hygiene tail"
    )

    # No explicit `state=` — `build_server` defaults to the process-wide
    # `SessionRegistry`, which routes per `Context.client_id` so a single
    # long-running server process can safely serve multiple MCP clients.
    # For stdio (one client per process) this collapses to a single state
    # under the default key — same observable behavior as before.
    mcp = build_server(config=config, store=store)
    mcp.run("stdio")


def _cli_health(
    *,
    json_out: bool,
    days: int,
    top_k: int,
    min_applied: int | None = None,
) -> None:
    """`bettermemory health` — print the aggregate report."""
    from .health import render_json, render_text

    config = load_config()
    directory = config.resolved_directory()
    # `--min-applied` overrides the config default; fall through to the
    # configured value when the flag wasn't passed. Avoids forcing the user
    # to pass the same number to every CLI invocation.
    threshold = (
        min_applied
        if min_applied is not None
        else config.behavior.heavily_used_min_applied
    )
    report = report_for_directory(
        directory,
        window_days=days,
        heavily_used_top_k=top_k,
        heavily_used_min_applied=threshold,
        verification_stale_days=config.behavior.verification_stale_days,
        # Capture caller origin so the CLI rendering picks up the
        # commit-drift bucket when run from inside a project whose
        # memories live in this store.
        caller_origin=capture_origin(),
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))


def _cli_tombstones_list(*, json_out: bool, scopes: list[str] | None) -> None:
    """`bettermemory tombstones list` — print removed memories."""
    import json as _json

    config = load_config()
    store = Store(config.resolved_directory())
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    summaries = store.list_tombstones(scopes=scopes)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                [
                    {
                        "id": s.id,
                        "scopes": s.scopes,
                        "summary": s.summary,
                        "created": isoformat(s.created),
                        "removed": isoformat(s.removed),
                        "removed_reason": s.removed_reason,
                        "removed_session": s.removed_session,
                    }
                    for s in summaries
                ],
                indent=2,
            )
            + "\n"
        )
        return

    if not summaries:
        sys.stdout.write("No tombstones.\n")
        return

    sys.stdout.write(f"Tombstones ({len(summaries)}):\n")
    for s in summaries:
        sess = s.removed_session or "<no session>"
        sys.stdout.write(
            f"  {s.id} [removed={isoformat(s.removed)}, "
            f"session={sess}] {','.join(s.scopes)}: {s.summary}\n"
            f"    reason: {s.removed_reason}\n"
        )


def _cli_tombstones_prune(
    *,
    older_than_days: int | None,
    dry_run: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory tombstones prune` — hard-delete old tombstones."""
    import json as _json
    from datetime import timedelta

    config = load_config()
    days = (
        older_than_days
        if older_than_days is not None
        else config.behavior.tombstone_retention_days
    )
    if days is None or days <= 0:
        # Hard refusal — pruning everything by accident would be a foot-gun.
        parser.error(
            "--older-than is required (no default configured). Pass an "
            "explicit cutoff in days, or set "
            "`behavior.tombstone_retention_days` in config.toml."
        )
    cutoff = timedelta(days=days)

    store = Store(config.resolved_directory())

    if dry_run:
        # Use load_tombstones to inspect; don't call prune which deletes.
        now = utcnow()
        candidates = [t for t in store.load_tombstones() if t.removed < (now - cutoff)]
        ids = [t.id for t in candidates]
        if json_out:
            sys.stdout.write(
                _json.dumps({"would_delete": ids, "cutoff_days": days}, indent=2) + "\n"
            )
            return
        if not ids:
            sys.stdout.write(f"No tombstones older than {days} days.\n")
            return
        sys.stdout.write(
            f"Would delete {len(ids)} tombstone(s) older than {days} days:\n"
        )
        for t in candidates:
            sys.stdout.write(
                f"  {t.id} [removed={isoformat(t.removed)}]: {t.removed_reason}\n"
            )
        sys.stdout.write("(Dry run — re-run without --dry-run to apply.)\n")
        return

    pruned_ids = store.prune_tombstones(cutoff)
    if json_out:
        sys.stdout.write(
            _json.dumps({"deleted": pruned_ids, "cutoff_days": days}, indent=2) + "\n"
        )
        return
    if not pruned_ids:
        sys.stdout.write(f"No tombstones older than {days} days.\n")
        return
    sys.stdout.write(
        f"Deleted {len(pruned_ids)} tombstone(s) older than {days} days:\n"
    )
    for memory_id in pruned_ids:
        sys.stdout.write(f"  {memory_id}\n")


def _cli_ui(*, host: str, port: int) -> None:
    """`bettermemory ui` — run the local web UI.

    Catches the ImportError raised when the [ui] extra is missing and
    renders a clean install hint instead of a Python traceback.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    try:
        from . import web as _web

        _web.serve(config, host=host, port=port)
    except ImportError as exc:
        sys.stderr.write(
            "bettermemory ui requires the [ui] extra. Install with:\n"
            "  pip install 'bettermemory[ui]'\n"
            f"(original error: {exc})\n"
        )
        raise SystemExit(2) from exc


def _cli_sync_init(*, remote: str | None, default_branch: str, json_out: bool) -> None:
    """`bettermemory sync init` — set up the memory dir as a git repo."""
    import json as _json

    from . import sync as _sync

    config = load_config()
    directory = config.resolved_directory()

    try:
        result = _sync.init(directory, remote=remote, default_branch=default_branch)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync init failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Initialised sync in {result['root']}.\n")
    actions = result.get("actions", []) or []
    if isinstance(actions, list):
        for action in actions:
            sys.stdout.write(f"  - {action}\n")


def _cli_sync_status(*, json_out: bool) -> None:
    """`bettermemory sync status` — show pending changes + remote
    tracking."""
    import json as _json

    from . import sync as _sync

    config = load_config()
    directory = config.resolved_directory()
    st = _sync.status(directory)

    if json_out:
        sys.stdout.write(_json.dumps(st.to_dict(), indent=2) + "\n")
        return

    if not st.is_repo:
        sys.stdout.write(
            f"{directory} is not a git repo. Run `bettermemory sync init` "
            "to set up sync.\n"
        )
        return

    sys.stdout.write(f"Memory directory: {directory}\n")
    sys.stdout.write(f"  branch: {st.branch or '<detached>'}\n")
    sys.stdout.write(f"  remote: {st.remote_url or '<none>'}\n")
    if st.remote_url:
        sys.stdout.write(f"  ahead: {st.ahead}  behind: {st.behind}\n")
    sys.stdout.write(
        f"  untracked: {len(st.untracked)}  modified: {len(st.modified)}\n"
    )
    if st.modified:
        sys.stdout.write("  modified files:\n")
        for path in st.modified[:10]:
            sys.stdout.write(f"    {path}\n")
        if len(st.modified) > 10:
            sys.stdout.write(f"    ... and {len(st.modified) - 10} more\n")


def _cli_sync_push(*, remote: str, message: str | None, json_out: bool) -> None:
    """`bettermemory sync push` — stage, commit, push."""
    import json as _json

    from . import sync as _sync

    config = load_config()
    directory = config.resolved_directory()
    eff_message = message or _sync.DEFAULT_COMMIT_MESSAGE
    try:
        result = _sync.push(directory, remote=remote, message=eff_message)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync push failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    if result["committed"]:
        sys.stdout.write(f"Committed and pushed to {remote}.\n")
    else:
        sys.stdout.write(
            f"No local changes to commit; pushed prior commits to {remote}.\n"
        )


def _cli_sync_pull(*, remote: str, reindex: bool, json_out: bool) -> None:
    """`bettermemory sync pull` — rebase-pull + rebuild index."""
    import json as _json

    from . import sync as _sync

    config = load_config()
    directory = config.resolved_directory()
    try:
        result = _sync.pull(directory, remote=remote, reindex=reindex)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync pull failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Pulled from {remote}.\n")
    if reindex:
        sys.stdout.write(f"  reindexed {result.get('indexed_count', 0)} memories\n")
    else:
        sys.stdout.write(
            "  --no-reindex passed: run `bettermemory reindex` when ready\n"
        )


def _cli_sync_auto(*, remote: str, json_out: bool) -> None:
    """`bettermemory sync auto` — pull then push, one-shot."""
    import json as _json

    from . import sync as _sync

    config = load_config()
    directory = config.resolved_directory()
    try:
        result = _sync.auto(directory, remote=remote)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync auto failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Auto-sync complete (remote={remote}).\n")


def _cli_reindex(*, json_out: bool) -> None:
    """`bettermemory reindex` — drop and rebuild the FTS5 index from
    the on-disk memories.

    Reports before/after counts so a partial corruption shows up as
    "indexed 234 of 250" instead of silently. The rebuild itself is
    transactional — if it fails partway, the prior index is intact
    and the caller sees the failure rather than a half-built index.
    """
    import json as _json

    from . import index as _index

    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    before = _index.status(directory)
    memories = store.load_all()
    count = _index.rebuild(directory, memories)
    after = _index.status(directory)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                {
                    "indexed": count,
                    "before": before,
                    "after": after,
                    "directory": str(directory),
                },
                indent=2,
            )
            + "\n"
        )
        return

    sys.stdout.write(
        f"Reindexed {count} memories from {directory}.\n"
        f"  before: {before.get('indexed_count', 0)} indexed, "
        f"{before.get('size_bytes', 0)} bytes\n"
        f"  after:  {after.get('indexed_count', 0)} indexed, "
        f"{after.get('size_bytes', 0)} bytes\n"
    )


def _cli_consolidate(
    *,
    apply: bool,
    json_out: bool,
    window_days: int,
    cold_scope_days: int,
    semantic_threshold: float | None,
    typo_distance: int,
) -> None:
    """`bettermemory consolidate` — offline curation pass.

    Runs four passes: near-duplicate dedup, demote-never-applied,
    cold-scope suggestions, scope-typo suggestions. Dry-run by
    default; `--apply` commits dedup tombstones and category
    demotions. Cold-scope and scope-typo passes stay suggest-only
    regardless — they touch shape that a human should review.
    """
    from .consolidate import consolidate, render_json, render_text
    from .semantic import get_model

    config = load_config()
    store = Store(config.resolved_directory())

    # Resolve the semantic model if the embeddings extra is installed.
    # `get_model` returns None on a clean install without the extra,
    # which the dedup pass treats as the "fall back to Jaccard" signal.
    semantic_model = (
        get_model(config.behavior.semantic_model_name)
        if config.behavior.semantic_dedup
        else None
    )

    # Build a session id so tombstones produced by --apply carry a
    # caller-attributable record. Matches the SessionState pattern used
    # by `_cli_serve`; here we don't need the full state object, just
    # the id field for the tombstone frontmatter.
    from .session import SessionState as _SessionState

    session_id = _SessionState().session_id

    report = consolidate(
        store,
        semantic_model=semantic_model,
        semantic_threshold=semantic_threshold,
        window_days=window_days,
        cold_scope_days=cold_scope_days,
        typo_distance=typo_distance,
        apply=apply,
        session_id=session_id,
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))


def _cli_export(
    *,
    output: str | None,
    include_tombstones: bool,
    scopes: list[str] | None,
) -> None:
    """`bettermemory export` — dump active (and optionally tombstoned)
    memories to a self-describing JSON document.

    Format (`format_version: 1`):

        {
          "format_version": 1,
          "exported_at": "2026-05-09T12:34:56Z",
          "source_directory": "/Users/me/.claude-memory",
          "active_memories":     [<full Memory dict>, ...],
          "tombstoned_memories": [<full TombstonedMemory dict>, ...]
        }

    `tombstoned_memories` is omitted entirely when --no-tombstones is
    passed (vs. emitted as []) so a consumer can distinguish "not
    requested" from "no tombstones present". Each memory dict mirrors
    the Pydantic model — id, created, updated, scopes, confidence,
    source, body, origin, last_verified_at — and tombstones add
    removed / removed_reason / removed_session.

    The shape is intended to be round-trippable: a future
    `bettermemory import` can recreate active records and tombstones
    from this document with no loss. Bump format_version on any
    breaking change.
    """
    import json as _json
    from pathlib import Path as _Path

    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    scope_set = set(scopes) if scopes else None

    active = store.load_all()
    if scope_set is not None:
        active = [m for m in active if scope_set.intersection(m.scopes)]

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": isoformat(utcnow()),
        "source_directory": str(directory),
        "active_memories": [m.model_dump(mode="json") for m in active],
    }
    tombstoned_count = 0
    if include_tombstones:
        tombstoned = store.load_tombstones()
        if scope_set is not None:
            tombstoned = [t for t in tombstoned if scope_set.intersection(t.scopes)]
        payload["tombstoned_memories"] = [t.model_dump(mode="json") for t in tombstoned]
        tombstoned_count = len(tombstoned)

    text = _json.dumps(payload, indent=2)

    if output:
        out_path = _Path(output)
        out_path.write_text(text + "\n", encoding="utf-8")
        summary = f"Exported {len(active)} active memories"
        if include_tombstones:
            summary += f" + {tombstoned_count} tombstones"
        summary += f" to {out_path}\n"
        # Status line goes to stderr so `-o` callers can still pipe
        # the file path on stdout if they want; consistent with how
        # most CLI tools split status from data.
        sys.stderr.write(summary)
        return

    sys.stdout.write(text + "\n")


def _cli_migrate_origin(
    *,
    dry_run: bool,
    force_repo: str | None,
    scope_repo_map: dict[str, str],
) -> None:
    """`bettermemory migrate origin` — backfill origin on legacy memories."""
    from .migrate import (
        infer_origin_for_memory_dir,
        migrate_origin_in_directory,
    )

    config = load_config()
    memory_dir = config.resolved_directory()

    print(f"Scanning {memory_dir}...")
    print()

    if scope_repo_map:
        print("Routing by scope:")
        for scope, url in scope_repo_map.items():
            print(f"  {scope:<32} -> {url}")
        print()

    if force_repo is not None:
        print(f"Fallback: untagged memories -> {force_repo!r}")
    else:
        inferred = infer_origin_for_memory_dir(memory_dir)
        if scope_repo_map and inferred is None:
            print(
                "Fallback: untagged memories left alone "
                "(no --repo and no auto-inference)."
            )
        elif scope_repo_map is None or not scope_repo_map:
            if inferred is None:
                print(
                    f"  Parent of memory dir: {memory_dir.parent}\n"
                    f"  No git remote detected.\n"
                    f"\n"
                    f"This appears to be a global memory directory — "
                    f"memories here probably came from many projects, "
                    f"and tagging them all with one repo would be "
                    f"misinformation. Nothing to do.\n"
                    f"\n"
                    f"Options:\n"
                    f"  --repo <url>                       "
                    f"force-tag every memory\n"
                    f"  --scope-repo projects:foo=<url>    "
                    f"route by scope (multi)"
                )
                return
            print(f"  Inferred repo:   {inferred.repo}")
            print(f"  cwd:             {inferred.cwd}")
            print("  branch:          (left null — original branch unknown)")

    print()
    report = migrate_origin_in_directory(
        memory_dir,
        force_repo=force_repo,
        scope_repo_map=scope_repo_map or None,
        dry_run=dry_run,
    )

    print("Results:")
    print(f"  Scanned:           {report.scanned}")
    print(f"  Already had origin: {report.already_had_origin}")
    print(f"  {'Would update' if dry_run else 'Updated':<18} {report.updated}")
    if report.malformed:
        print(f"  Malformed (skipped): {len(report.malformed)}")
        for path in report.malformed[:5]:
            print(f"    - {path}")
        if len(report.malformed) > 5:
            print(f"    ... and {len(report.malformed) - 5} more")

    if dry_run and report.updated:
        print()
        print("(Dry run — no changes written. Re-run without --dry-run to apply.)")


# Re-export the prompt for consumers who import the package.
__all__ = ["build_server", "main", "SYSTEM_PROMPT_ADDENDUM"]
