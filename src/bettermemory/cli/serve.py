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
from typing import NamedTuple

from ..builder import build_server
from ..config import Config, load_config
from ..store import (
    STORE_FILENAME,
    Store,
    UnmigratedV8DirectoryError,
    refuse_unmigrated_v8,
)
from ._common import (
    exit_if_bundle_file,
    migration_line,
    open_owner_store,
    run_migration_at_open,
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


class Opened(NamedTuple):
    """The config, the storage directory and the store `serve` serves."""

    config: Config
    directory: Path
    store: Store


def open_for_serve() -> Opened:
    """The store `serve` serves, opened as the owner's process opens it,
    or the command ended in one line on stderr and exit 1: a directory
    holding an un-migrated bettermemory 8 store and no store file, a
    store file inside a backup bundle (serving it would write into the
    bundle's snapshot and fail its hash), and a key file that is there
    and cannot be read (`_common.open_owner_store`; nothing is rekeyed).

    The shim calls this before it says it falls back to serving in
    process, so a store the fallback cannot open ends the shim in that
    one line, not after two warnings about a server that never starts."""
    config = load_config()
    directory = config.resolved_directory()
    exit_if_unmigrated_v8(directory / STORE_FILENAME)
    exit_if_bundle_file(directory / STORE_FILENAME)
    store = open_owner_store(directory / STORE_FILENAME)
    return Opened(config=config, directory=directory, store=store)


def run_serve(opened: Opened | None = None) -> None:
    """Start the MCP server over stdio.

    Identical behavior to the pre-extraction ``_cli_serve``: configure
    root logging on stderr, load config, instantiate the Store at the
    resolved directory (`open_for_serve`, unless the caller opened it
    already), then hand off to ``build_server(...).run("stdio")``.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config, directory, store = opened if opened is not None else open_for_serve()
    # The migration's run at open (design 8.6), logged rather than printed
    # apart: this process's stderr is the server's log.
    report = run_migration_at_open(store, config, level=logging.WARNING)
    if report is not None:
        log.info("%s", migration_line(report))

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
