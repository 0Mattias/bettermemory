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
* Re-exports of ``build_server`` and ``_register_tools`` from
  ``bettermemory.builder`` so the forty+ test files that import
  ``from bettermemory.server import build_server`` keep resolving.
* Re-exports of ``SYSTEM_PROMPT_ADDENDUM``, ``load_config``, and
  ``capture_origin`` for the same back-compat reason. ``test_export``
  used to patch ``bettermemory.server.load_config`` (Round-3 moved the
  patch to ``bettermemory.config.load_config``, but the re-export
  remains for any out-of-tree caller). ``test_server_origin`` and
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

import logging

from .builder import _register_tools, build_server
from .config import load_config
from .origin import capture as capture_origin  # noqa: F401
from .prompts import SYSTEM_PROMPT_ADDENDUM


# ``capture_origin`` is unused inside this module — every live call site
# moved with the CLI extraction (the handlers in `_handlers.py` import
# their own binding from `.origin`). Kept importable here because
# `tests/test_server_origin.py` and `tests/test_server_commit_drift.py`
# defensively monkeypatch `bettermemory.server.capture_origin`; removing
# the binding would AttributeError on the patch even though the test's
# active code path never calls the symbol.


log = logging.getLogger("bettermemory")


# Round-3 audit fix: the three semantic-setup helpers moved to
# ``bettermemory.semantic_setup`` so ``cli/`` can import them without
# back-edging through this module. Re-exported here so any out-of-tree
# caller keeps its existing import path. ``builder.py`` reaches the
# canonical home directly.
from .semantic_setup import (  # noqa: E402
    _configure_persistent_embeddings,
    _resolve_semantic_provider_and_model,
    _semantic_model_or_none,
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
# attribute` check. `build_server` and `_register_tools` are re-exported
# from `bettermemory.builder`, the canonical home post-Round-3.
__all__ = [
    "build_server",
    "_register_tools",
    "main",
    "SYSTEM_PROMPT_ADDENDUM",
    "load_config",
    "capture_origin",
    "_cli_export",
    "_cli_consolidate_acknowledge_debt",
    "_cli_consolidate_acknowledge_misses",
]
