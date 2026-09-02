"""Shared helpers for the CLI subcommand modules.

Folds the duplicated ``load_config() + Store(directory)`` pattern that
every ``_cli_*`` handler used to repeat (audit finding M8). One call to
:func:`cli_context` returns the resolved ``Config``, the storage
directory ``Path``, and a ``Store`` rooted at that directory — the three
fixtures every subcommand needs.

Kept minimal: no logging configuration, no I/O beyond the config read.
The serve / ui subcommands need extra setup (``logging.basicConfig``,
extras imports) that doesn't belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Config, load_config
from ..store import Store

if TYPE_CHECKING:
    from ..events import Recorder


@dataclass(frozen=True)
class CliContext:
    """Resolved fixtures for a CLI subcommand invocation.

    The three fields cover the duplicated setup every ``_cli_*`` handler
    used to repeat by hand. ``frozen=True`` because the context is a
    snapshot — handlers that mutate state do so on ``store``, never on
    the context wrapper itself.
    """

    config: Config
    directory: Path
    store: Store


def cli_context() -> CliContext:
    """Resolve the active config, storage directory, and Store.

    Replaces the ``config = load_config(); directory =
    config.resolved_directory(); store = Store(directory)`` triple that
    appeared in every CLI handler. Callers that only need a subset still
    pay the full triple — ``load_config`` re-reads the TOML on every
    call (it holds no cache), which is one small file parse per CLI
    invocation, nowhere near a hot path.
    """
    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)
    return CliContext(config=config, directory=directory, store=store)


def cli_recorder(ctx: CliContext, *, session_id: str | None = None) -> Recorder:
    """An event recorder for a CLI command that mutates the store.

    Mirrors the server's recorder construction (`builder.py`) so CLI
    writes land in the same audit log under the same telemetry posture:
    `[telemetry] enabled = false` turns the log off here too, and the
    rotation cap and verbatim-query redaction follow the same config.
    Every CLI path that creates or rewrites a memory records through
    this, because the provenance derivation at `index.rebuild` joins on
    those events: a mutation that records nothing reads as unaccounted
    on the next rebuild.

    `session_id` lets a command reuse the id it already stamped on
    tombstones (consolidate); the default mints a fresh one.
    """
    from ..events import Recorder
    from ..session import SessionState

    return Recorder(
        root=ctx.directory,
        session_id=session_id or SessionState().session_id,
        enabled=ctx.config.telemetry.enabled,
        max_bytes=ctx.config.telemetry.max_bytes,
        log_queries_verbatim=ctx.config.telemetry.log_queries_verbatim,
    )
