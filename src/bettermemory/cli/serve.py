"""`bettermemory serve` — the default no-arg behavior.

Runs the MCP server over stdio. Not currently exposed as a named
subcommand on the CLI (the default-with-no-args path falls here
directly); kept as its own module so the dispatch logic stays uniform
and a future explicit ``bettermemory serve`` subcommand has an obvious
home.
"""

from __future__ import annotations

import logging
import sys

from ..builder import build_server
from ..config import load_config
from ..store import Store


log = logging.getLogger("bettermemory")


def run_serve() -> None:
    """Start the MCP server over stdio.

    Identical behavior to the pre-extraction ``_cli_serve``: configure
    root logging on stderr, load config, instantiate the Store at the
    resolved directory, then hand off to ``build_server(...).run("stdio")``.
    """
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
