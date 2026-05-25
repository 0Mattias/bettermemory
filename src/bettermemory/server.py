"""MCP server entry-point shim and historical re-export surface.

The MCP wiring (``build_server``, ``_register_tools``, the FastMCP
``instructions`` block, the per-tool ``mcp.tool(...)`` registrations)
lives in ``bettermemory.builder``. That move closed a load-time cycle:
``cli/serve.py`` can now import ``build_server`` at the module top
level without falling back to a lazy import, because ``builder.py`` is
a sibling of both ``server.py`` and the ``cli`` package and doesn't
back-edge through either.

What's left in this module:

* ``main()``: the historical ``bettermemory.server:main`` console-script
  entry point. Every wheel on PyPI was built with this entry point
  pinned in ``pyproject.toml``; the shim delegates into
  ``bettermemory.cli.main``.
* Re-export of ``build_server`` from ``bettermemory.builder`` so the
  forty+ test files that import ``from bettermemory.server import
  build_server`` keep resolving.
* Re-exports of ``SYSTEM_PROMPT_ADDENDUM`` and ``capture_origin`` for
  the same back-compat reason. ``test_server_origin`` and
  ``test_server_commit_drift`` patch
  ``bettermemory.server.capture_origin``; the binding must stay
  importable here.
* Re-exports of ``_cli_export`` /
  ``_cli_consolidate_acknowledge_debt`` /
  ``_cli_consolidate_acknowledge_misses`` for ``test_export`` and
  ``test_consolidate``.

The full tool surface (mirrored in ``prompts.SYSTEM_PROMPT_ADDENDUM``
so the consuming model sees an identical list):

- Retrieval: memory_search, memory_show, memory_list, memory_scope_overview
- Writing:   memory_write (+ _confirm / _cancel staged-write pair),
             memory_update
- Lifecycle: memory_remove, memory_restore, memory_list_tombstones
- Verification: memory_verify
- Curation:  memory_record_use, memory_health, memory_audit_turn,
             memory_rename_scope
- Session:   memory_scope_disable / memory_scope_enable
"""

from __future__ import annotations

from .builder import build_server
from .origin import capture as capture_origin  # noqa: F401
from .prompts import SYSTEM_PROMPT_ADDENDUM


# ``capture_origin`` is unused inside this module — every live call site
# moved with the CLI extraction (the handlers in `_handlers.py` import
# their own binding from `.origin`). Kept importable here because
# `tests/test_server_origin.py` and `tests/test_server_commit_drift.py`
# defensively monkeypatch `bettermemory.server.capture_origin`; removing
# the binding would AttributeError on the patch even though the test's
# active code path never calls the symbol.


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
# 2. The test_server_origin / test_server_commit_drift suites
#    monkeypatch `bettermemory.server.capture_origin`; the binding
#    stays importable at this module path. (The canonical patch target
#    for `load_config` is now `bettermemory.config.load_config` —
#    every CLI module imports it from `..config` directly, so no
#    back-edge through this module is needed.)
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


# Re-export the prompt for consumers who import the package.
# `capture_origin` is exposed so the test-monkeypatch contract
# documented above passes mypy's `Module ... does not explicitly export
# attribute` check. `build_server` is re-exported from
# `bettermemory.builder`, the canonical home post-Round-3.
__all__ = [
    "build_server",
    "main",
    "SYSTEM_PROMPT_ADDENDUM",
    "capture_origin",
    "_cli_export",
    "_cli_consolidate_acknowledge_debt",
    "_cli_consolidate_acknowledge_misses",
]
