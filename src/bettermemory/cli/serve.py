"""`bettermemory serve`: the MCP server over stdio, in this process.

With no arguments `bettermemory` runs the stdio shim, which forwards
every request to the local daemon. `serve` runs the server itself, with
no daemon, and is listed in `bettermemory --help`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..builder import build_server
from ..config import load_config
from ..store import (
    STORE_FILENAME,
    Store,
    UnmigratedV8DirectoryError,
    refuse_unmigrated_v8,
)


log = logging.getLogger("bettermemory")


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    return sub.add_parser(
        "serve",
        help=(
            "Run the MCP server over stdio in this process, without the daemon. "
            "`bettermemory` with no arguments is the stdio shim in front of the daemon."
        ),
    )


def exit_if_unmigrated_v8(path: Path) -> None:
    """Refuse, in one line on stderr and exit 1, a store path whose
    directory holds an un-migrated bettermemory 8 store and no store
    file, so the shim and `serve` never print a traceback for it."""
    try:
        refuse_unmigrated_v8(path)
    except UnmigratedV8DirectoryError as exc:
        sys.stderr.write(f"bettermemory: {exc}\n")
        raise SystemExit(1) from None


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
    exit_if_unmigrated_v8(directory / STORE_FILENAME)
    store = Store.open_or_create(directory / STORE_FILENAME)

    log.info("memory directory: %s", directory)
    log.info(
        "telemetry: %s (the store's log at %s)",
        "on" if config.telemetry.enabled else "off",
        store.path,
    )
    log.info(
        "system prompt: server-level MCP `instructions` block carries "
        "the core policy; on Claude Code the block is truncated at "
        "~1.8KB so the long-form addendum (docs/system_prompt.md) or "
        "the plugin's SKILL.md carries the writing-discipline / "
        "scope-hygiene tail"
    )

    # No explicit `state=` — `build_server` defaults to the process-wide
    # `SessionRegistry`, which routes per request client id so a single
    # long-running server process can safely serve multiple MCP clients.
    # For stdio (one client per process) this collapses to a single state
    # under the default key — same observable behavior as before.
    mcp = build_server(config=config, store=store)
    mcp.run("stdio")
