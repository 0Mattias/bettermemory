"""Direct-import smoke tests at the boundaries of the ``bettermemory.handlers.*``
and ``bettermemory.cli.*`` packages.

Every other test suite reaches the per-tool handler bodies through the
``ToolHandlers`` facade in ``_handlers.py`` or the back-compat re-exports
on ``server.py``. That layer hides the per-module direct-call surface:
a refactor that renames a parameter in ``handlers/health.py::memory_health``
or changes ``handlers/write.py::memory_write``'s signature can pass the
facade-based suite while silently breaking any out-of-tree consumer who
does ``from bettermemory.handlers.health import memory_health``.

These tests are intentionally tiny — they import the symbol directly,
confirm it's callable / instantiable, and check one shape property (a
DESC constant is a non-empty string, an ``add_subparser`` actually
registers the subcommand, ``--help`` exits 0). The deep functional
coverage lives elsewhere; this file's job is signature-drift detection
at the package boundary.

Test count is tight on purpose. One smoke per public module, two only
when the module exposes two distinct entry points worth pinning
separately (e.g. ``handlers/scope_toggle.py`` has the enable/disable
pair; ``handlers/write.py`` has the write/confirm/cancel trio).
Underscore-prefixed modules (``handlers/_shared.py``, ``cli/_common.py``)
are skipped per the underscore-private convention.
"""

from __future__ import annotations

import argparse
import inspect

import pytest


# ---------------------------------------------------------------------------
# handlers.* — per-tool MCP handler modules
#
# Each handler module exports ``DESC_<TOOL>`` (string) + ``<tool>`` (async
# function). The smoke is "the symbols resolve via direct import, the
# DESC is non-empty, the function is a coroutine function". A signature-
# drift regression (renamed parameter, missing default, async->sync
# change) would fail one of these import-or-assert steps before the
# facade tests would notice.
# ---------------------------------------------------------------------------


def test_handlers_audit_turn_direct_import() -> None:
    from bettermemory.handlers.audit_turn import (
        DESC_MEMORY_AUDIT_TURN,
        memory_audit_turn,
    )

    assert isinstance(DESC_MEMORY_AUDIT_TURN, str) and DESC_MEMORY_AUDIT_TURN
    assert inspect.iscoroutinefunction(memory_audit_turn)
    params = inspect.signature(memory_audit_turn).parameters
    assert "deps" in params
    assert "user_message" in params


def test_handlers_health_direct_import() -> None:
    from bettermemory.handlers.health import DESC_MEMORY_HEALTH, memory_health

    assert isinstance(DESC_MEMORY_HEALTH, str) and DESC_MEMORY_HEALTH
    assert inspect.iscoroutinefunction(memory_health)
    params = inspect.signature(memory_health).parameters
    assert "deps" in params
    assert "window_days" in params


def test_handlers_list_active_direct_import() -> None:
    from bettermemory.handlers.list_active import DESC_MEMORY_LIST, memory_list

    assert isinstance(DESC_MEMORY_LIST, str) and DESC_MEMORY_LIST
    assert inspect.iscoroutinefunction(memory_list)
    params = inspect.signature(memory_list).parameters
    assert "deps" in params
    assert "with_bodies" in params


def test_handlers_record_use_direct_import() -> None:
    from bettermemory.handlers.record_use import (
        DESC_MEMORY_RECORD_USE,
        memory_record_use,
    )

    assert isinstance(DESC_MEMORY_RECORD_USE, str) and DESC_MEMORY_RECORD_USE
    assert inspect.iscoroutinefunction(memory_record_use)
    params = inspect.signature(memory_record_use).parameters
    assert "deps" in params
    assert "memory_ids" in params
    assert "outcome" in params


def test_handlers_remove_direct_import() -> None:
    from bettermemory.handlers.remove import DESC_MEMORY_REMOVE, memory_remove

    assert isinstance(DESC_MEMORY_REMOVE, str) and DESC_MEMORY_REMOVE
    assert inspect.iscoroutinefunction(memory_remove)
    params = inspect.signature(memory_remove).parameters
    assert "deps" in params
    assert "id" in params
    assert "reason" in params


def test_handlers_rename_scope_direct_import() -> None:
    from bettermemory.handlers.rename_scope import (
        DESC_MEMORY_RENAME_SCOPE,
        memory_rename_scope,
    )

    assert isinstance(DESC_MEMORY_RENAME_SCOPE, str) and DESC_MEMORY_RENAME_SCOPE
    assert inspect.iscoroutinefunction(memory_rename_scope)
    params = inspect.signature(memory_rename_scope).parameters
    assert "deps" in params
    assert "old_scope" in params
    assert "new_scope" in params


def test_handlers_restore_direct_import() -> None:
    from bettermemory.handlers.restore import DESC_MEMORY_RESTORE, memory_restore

    assert isinstance(DESC_MEMORY_RESTORE, str) and DESC_MEMORY_RESTORE
    assert inspect.iscoroutinefunction(memory_restore)
    params = inspect.signature(memory_restore).parameters
    assert "deps" in params
    assert "id" in params


def test_handlers_scope_overview_direct_import() -> None:
    from bettermemory.handlers.scope_overview import (
        DESC_MEMORY_SCOPE_OVERVIEW,
        memory_scope_overview,
    )

    assert isinstance(DESC_MEMORY_SCOPE_OVERVIEW, str) and DESC_MEMORY_SCOPE_OVERVIEW
    assert inspect.iscoroutinefunction(memory_scope_overview)
    params = inspect.signature(memory_scope_overview).parameters
    assert "deps" in params
    assert "auto_scope" in params


def test_handlers_scope_toggle_direct_import() -> None:
    """Two distinct entry points: ``disable`` and ``enable``. Both pinned
    so a typo on one (renamed param, swapped async/sync) doesn't slip in."""
    from bettermemory.handlers.scope_toggle import (
        DESC_MEMORY_SCOPE_DISABLE,
        DESC_MEMORY_SCOPE_ENABLE,
        memory_scope_disable,
        memory_scope_enable,
    )

    assert isinstance(DESC_MEMORY_SCOPE_DISABLE, str) and DESC_MEMORY_SCOPE_DISABLE
    assert isinstance(DESC_MEMORY_SCOPE_ENABLE, str) and DESC_MEMORY_SCOPE_ENABLE
    assert inspect.iscoroutinefunction(memory_scope_disable)
    assert inspect.iscoroutinefunction(memory_scope_enable)
    assert "scope" in inspect.signature(memory_scope_disable).parameters
    assert "scope" in inspect.signature(memory_scope_enable).parameters


def test_handlers_search_direct_import() -> None:
    from bettermemory.handlers.search import DESC_MEMORY_SEARCH, memory_search

    assert isinstance(DESC_MEMORY_SEARCH, str) and DESC_MEMORY_SEARCH
    assert inspect.iscoroutinefunction(memory_search)
    params = inspect.signature(memory_search).parameters
    assert "deps" in params
    assert "query" in params
    assert "mode" in params


def test_handlers_show_direct_import() -> None:
    from bettermemory.handlers.show import DESC_MEMORY_SHOW, memory_show

    assert isinstance(DESC_MEMORY_SHOW, str) and DESC_MEMORY_SHOW
    assert inspect.iscoroutinefunction(memory_show)
    params = inspect.signature(memory_show).parameters
    assert "deps" in params
    assert "id" in params


def test_handlers_tombstones_direct_import() -> None:
    from bettermemory.handlers.tombstones import (
        DESC_MEMORY_LIST_TOMBSTONES,
        memory_list_tombstones,
    )

    assert isinstance(DESC_MEMORY_LIST_TOMBSTONES, str) and DESC_MEMORY_LIST_TOMBSTONES
    assert inspect.iscoroutinefunction(memory_list_tombstones)
    params = inspect.signature(memory_list_tombstones).parameters
    assert "deps" in params
    assert "scopes" in params


def test_handlers_update_direct_import() -> None:
    from bettermemory.handlers.update import (
        DESC_MEMORY_LINKS_TAIL,
        DESC_MEMORY_UPDATE,
        memory_update,
    )

    assert isinstance(DESC_MEMORY_UPDATE, str) and DESC_MEMORY_UPDATE
    # DESC_MEMORY_LINKS_TAIL is consumed by DESC_MEMORY_UPDATE; pin it
    # separately so a refactor that drops the shared tail is caught.
    assert isinstance(DESC_MEMORY_LINKS_TAIL, str) and DESC_MEMORY_LINKS_TAIL
    assert inspect.iscoroutinefunction(memory_update)
    params = inspect.signature(memory_update).parameters
    assert "deps" in params
    assert "id" in params
    assert "links" in params


def test_handlers_verify_direct_import() -> None:
    from bettermemory.handlers.verify import DESC_MEMORY_VERIFY, memory_verify

    assert isinstance(DESC_MEMORY_VERIFY, str) and DESC_MEMORY_VERIFY
    assert inspect.iscoroutinefunction(memory_verify)
    params = inspect.signature(memory_verify).parameters
    assert "deps" in params
    assert "id" in params
    assert "verified_paths" in params


def test_handlers_write_direct_import() -> None:
    """Three distinct entry points: ``write``, ``write_confirm``,
    ``write_cancel``. The trio is the pending-write lifecycle and a
    signature drift on any one breaks the others' contract — pin all
    three."""
    from bettermemory.handlers.write import (
        DESC_MEMORY_WRITE,
        DESC_MEMORY_WRITE_CANCEL,
        DESC_MEMORY_WRITE_CONFIRM,
        memory_write,
        memory_write_cancel,
        memory_write_confirm,
    )

    assert isinstance(DESC_MEMORY_WRITE, str) and DESC_MEMORY_WRITE
    assert isinstance(DESC_MEMORY_WRITE_CONFIRM, str) and DESC_MEMORY_WRITE_CONFIRM
    assert isinstance(DESC_MEMORY_WRITE_CANCEL, str) and DESC_MEMORY_WRITE_CANCEL
    assert inspect.iscoroutinefunction(memory_write)
    assert inspect.iscoroutinefunction(memory_write_confirm)
    assert inspect.iscoroutinefunction(memory_write_cancel)
    write_params = inspect.signature(memory_write).parameters
    assert {"deps", "content", "scopes", "category"}.issubset(write_params)
    assert "pending_id" in inspect.signature(memory_write_confirm).parameters
    assert "pending_id" in inspect.signature(memory_write_cancel).parameters


# ---------------------------------------------------------------------------
# cli.* — per-subcommand argparse builders + ``run`` dispatchers
#
# Each subcommand module exports ``add_subparser(sub)`` + ``run(args)``.
# The right minimal call is "build a root parser, hand the module a
# subparser slot, ask for --help on the registered subcommand" — that
# exercises the argparse builder end-to-end (positional/keyword wiring,
# choices, defaults) and confirms ``--help`` exits 0 like every other
# argparse handler. ``run(...)`` is exercised by the existing
# ``test_cli_smoke.py`` suite via the dispatcher; we don't re-call it
# here, the smoke is the direct-import path.
#
# ``serve.py`` uses ``run_serve()`` (no subparser); ``cli/__init__.py``
# exposes ``main()``. Both get their own smoke.
# ---------------------------------------------------------------------------


def _registered_parser(module: object, name: str) -> argparse.ArgumentParser:
    """Build a fresh root parser, ask the CLI module to register its
    subparser slot, and return the resulting subparser.

    Mirrors the shape ``cli/__init__.py::_build_parser`` uses so each
    module's ``add_subparser`` runs in its real registration context.
    Returning the subparser lets callers run ``--help`` against it
    directly.
    """
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    registered = module.add_subparser(sub)  # type: ignore[attr-defined]
    assert isinstance(registered, argparse.ArgumentParser), (
        f"{name}.add_subparser must return the registered subparser"
    )
    return registered


def test_cli_init_main_direct_import() -> None:
    """``cli.main`` is the package entry point ``bettermemory.__main__``
    routes through. Pin that the direct import works and that it's a
    callable (not just a re-export tuple or string)."""
    from bettermemory.cli import main

    assert callable(main)
    # The ``__all__`` advertises only ``main`` — pin so a refactor that
    # widens the public surface lands deliberately.
    import bettermemory.cli as cli_pkg

    assert "main" in cli_pkg.__all__


def test_cli_audit_turn_cmd_direct_import() -> None:
    from bettermemory.cli import audit_turn_cmd

    subparser = _registered_parser(audit_turn_cmd, "audit_turn_cmd")
    assert callable(audit_turn_cmd.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_consolidate_direct_import() -> None:
    from bettermemory.cli import consolidate

    subparser = _registered_parser(consolidate, "consolidate")
    assert callable(consolidate.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_doctor_direct_import() -> None:
    from bettermemory.cli import doctor

    subparser = _registered_parser(doctor, "doctor")
    assert callable(doctor.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_eval_direct_import() -> None:
    from bettermemory.cli import eval as eval_cmd

    subparser = _registered_parser(eval_cmd, "eval")
    assert callable(eval_cmd.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_export_direct_import() -> None:
    from bettermemory.cli import export

    subparser = _registered_parser(export, "export")
    assert callable(export.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_health_cmd_direct_import() -> None:
    from bettermemory.cli import health_cmd

    subparser = _registered_parser(health_cmd, "health_cmd")
    assert callable(health_cmd.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_ingest_direct_import() -> None:
    from bettermemory.cli import ingest

    subparser = _registered_parser(ingest, "ingest")
    assert callable(ingest.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_init_subcmd_direct_import() -> None:
    """Note: ``cli/init.py`` is the ``bettermemory init`` subcommand
    module, distinct from ``cli/__init__.py::main`` covered above."""
    from bettermemory.cli import init as init_cmd

    subparser = _registered_parser(init_cmd, "init")
    assert callable(init_cmd.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_migrate_direct_import() -> None:
    from bettermemory.cli import migrate

    subparser = _registered_parser(migrate, "migrate")
    assert callable(migrate.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_reindex_direct_import() -> None:
    from bettermemory.cli import reindex

    subparser = _registered_parser(reindex, "reindex")
    assert callable(reindex.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_serve_direct_import() -> None:
    """``serve`` is the default no-arg behaviour and does NOT register a
    subparser — it exposes ``run_serve()`` directly. Smoke is "the
    direct import resolves to a callable"; we don't invoke it (it
    would start the MCP server over stdio)."""
    from bettermemory.cli.serve import run_serve

    assert callable(run_serve)


def test_cli_sync_direct_import() -> None:
    from bettermemory.cli import sync

    subparser = _registered_parser(sync, "sync")
    assert callable(sync.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_tombstones_direct_import() -> None:
    from bettermemory.cli import tombstones

    subparser = _registered_parser(tombstones, "tombstones")
    assert callable(tombstones.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0


def test_cli_ui_direct_import() -> None:
    from bettermemory.cli import ui

    subparser = _registered_parser(ui, "ui")
    assert callable(ui.run)
    with pytest.raises(SystemExit) as exc:
        subparser.parse_args(["--help"])
    assert exc.value.code == 0
