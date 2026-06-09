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
# function). The smoke pins three things per handler:
#
#   1. The DESC constant is a non-empty string (so the FastMCP schema
#      renders something the model can actually read).
#   2. The handler is a coroutine function (async->sync flips are loud
#      breakage at the dispatch boundary).
#   3. The FULL parameter snapshot: parameter names, ordering, and
#      defaults. Pinning the whole list (not a subset) is what makes
#      this signature-drift detection — adding / removing / renaming
#      ANY parameter, or flipping a default that gates behaviour
#      (``force=False`` -> ``force=True``, etc.), fails the snapshot.
#      Type annotations are deliberately NOT pinned (Pyright catches
#      those at lint time; capturing them here would create noisy
#      Optional[X] / X | None equivalence churn).
#
# All current handlers take POSITIONAL_OR_KEYWORD parameters only; the
# helper enforces that as a structural invariant so a future refactor
# that introduces keyword-only params (e.g. ``*, force: bool``) trips
# the assertion and lands a deliberate update here.
# ---------------------------------------------------------------------------


_MISSING = inspect.Parameter.empty


def _snapshot_params(
    handler: object,
) -> list[tuple[str, object]]:
    """Return ``[(name, default), ...]`` for every parameter on ``handler``.

    Defaults are ``inspect.Parameter.empty`` for required parameters and
    the literal default value otherwise — so a snapshot like
    ``[("deps", _MISSING), ("force", False)]`` will fail loudly if
    either ``deps`` becomes optional or ``force`` flips to ``True``.

    Asserts every parameter is POSITIONAL_OR_KEYWORD because that's the
    structural invariant the whole handler package follows today; a
    future move to KEYWORD_ONLY-or-anything-else is a real design
    change that should land an explicit update here rather than slip
    through silently.
    """
    sig = inspect.signature(handler)  # type: ignore[arg-type]
    snapshot: list[tuple[str, object]] = []
    for name, param in sig.parameters.items():
        assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"{handler!r} parameter {name!r} kind drifted to {param.kind!r} "
            "— update the snapshot helper if this is intentional"
        )
        snapshot.append((name, param.default))
    return snapshot


def test_handlers_audit_turn_direct_import() -> None:
    from bettermemory.handlers.audit_turn import (
        DESC_MEMORY_AUDIT_TURN,
        memory_audit_turn,
    )

    assert isinstance(DESC_MEMORY_AUDIT_TURN, str) and DESC_MEMORY_AUDIT_TURN
    assert inspect.iscoroutinefunction(memory_audit_turn)
    assert _snapshot_params(memory_audit_turn) == [
        ("deps", _MISSING),
        ("user_message", _MISSING),
        ("assistant_response", None),
        ("lookback_seconds", None),
        ("ctx", None),
    ]


def test_handlers_health_direct_import() -> None:
    from bettermemory.handlers.health import DESC_MEMORY_HEALTH, memory_health

    assert isinstance(DESC_MEMORY_HEALTH, str) and DESC_MEMORY_HEALTH
    assert inspect.iscoroutinefunction(memory_health)
    assert _snapshot_params(memory_health) == [
        ("deps", _MISSING),
        ("window_days", 30),
        ("heavily_used_top_k", 10),
        ("min_applied", None),
        ("ctx", None),
    ]


def test_handlers_list_active_direct_import() -> None:
    from bettermemory.handlers.list_active import DESC_MEMORY_LIST, memory_list

    assert isinstance(DESC_MEMORY_LIST, str) and DESC_MEMORY_LIST
    assert inspect.iscoroutinefunction(memory_list)
    assert _snapshot_params(memory_list) == [
        ("deps", _MISSING),
        ("scopes", None),
        ("with_bodies", False),
        ("ctx", None),
    ]


def test_handlers_record_use_direct_import() -> None:
    from bettermemory.handlers.record_use import (
        DESC_MEMORY_RECORD_USE,
        memory_record_use,
    )

    assert isinstance(DESC_MEMORY_RECORD_USE, str) and DESC_MEMORY_RECORD_USE
    assert inspect.iscoroutinefunction(memory_record_use)
    assert _snapshot_params(memory_record_use) == [
        ("deps", _MISSING),
        ("memory_ids", _MISSING),
        ("outcome", _MISSING),
        ("note", None),
        ("claim_excerpts", None),
        ("ctx", None),
    ]


def test_handlers_remove_direct_import() -> None:
    from bettermemory.handlers.remove import DESC_MEMORY_REMOVE, memory_remove

    assert isinstance(DESC_MEMORY_REMOVE, str) and DESC_MEMORY_REMOVE
    assert inspect.iscoroutinefunction(memory_remove)
    assert _snapshot_params(memory_remove) == [
        ("deps", _MISSING),
        ("id", _MISSING),
        ("reason", _MISSING),
        ("ctx", None),
    ]


def test_handlers_rename_scope_direct_import() -> None:
    from bettermemory.handlers.rename_scope import (
        DESC_MEMORY_RENAME_SCOPE,
        memory_rename_scope,
    )

    assert isinstance(DESC_MEMORY_RENAME_SCOPE, str) and DESC_MEMORY_RENAME_SCOPE
    assert inspect.iscoroutinefunction(memory_rename_scope)
    assert _snapshot_params(memory_rename_scope) == [
        ("deps", _MISSING),
        ("old_scope", _MISSING),
        ("new_scope", _MISSING),
        ("include_tombstones", True),
        ("ctx", None),
    ]


def test_handlers_restore_direct_import() -> None:
    from bettermemory.handlers.restore import DESC_MEMORY_RESTORE, memory_restore

    assert isinstance(DESC_MEMORY_RESTORE, str) and DESC_MEMORY_RESTORE
    assert inspect.iscoroutinefunction(memory_restore)
    assert _snapshot_params(memory_restore) == [
        ("deps", _MISSING),
        ("id", _MISSING),
        ("ctx", None),
    ]


def test_handlers_scope_overview_direct_import() -> None:
    from bettermemory.handlers.scope_overview import (
        DESC_MEMORY_SCOPE_OVERVIEW,
        memory_scope_overview,
    )

    assert isinstance(DESC_MEMORY_SCOPE_OVERVIEW, str) and DESC_MEMORY_SCOPE_OVERVIEW
    assert inspect.iscoroutinefunction(memory_scope_overview)
    assert _snapshot_params(memory_scope_overview) == [
        ("deps", _MISSING),
        ("auto_scope", True),
        ("ctx", None),
    ]


def test_handlers_scope_toggle_direct_import() -> None:
    """Two distinct entry points: ``disable`` and ``enable``. Both pinned
    so a typo on one (renamed param, swapped async/sync) doesn't slip in.
    The pair is structurally symmetric — same parameter snapshot — so a
    drift that breaks the symmetry would also trip these assertions."""
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
    expected = [
        ("deps", _MISSING),
        ("scope", _MISSING),
        ("ctx", None),
    ]
    assert _snapshot_params(memory_scope_disable) == expected
    assert _snapshot_params(memory_scope_enable) == expected


def test_handlers_search_direct_import() -> None:
    from bettermemory.handlers.search import DESC_MEMORY_SEARCH, memory_search

    assert isinstance(DESC_MEMORY_SEARCH, str) and DESC_MEMORY_SEARCH
    assert inspect.iscoroutinefunction(memory_search)
    assert _snapshot_params(memory_search) == [
        ("deps", _MISSING),
        ("query", _MISSING),
        ("scopes", None),
        ("max_results", None),
        ("expand_top", False),
        ("auto_scope", True),
        ("since_prior_session", False),
        ("mode", None),
        ("ctx", None),
    ]


def test_handlers_show_direct_import() -> None:
    from bettermemory.handlers.show import DESC_MEMORY_SHOW, memory_show

    assert isinstance(DESC_MEMORY_SHOW, str) and DESC_MEMORY_SHOW
    assert inspect.iscoroutinefunction(memory_show)
    assert _snapshot_params(memory_show) == [
        ("deps", _MISSING),
        ("id", _MISSING),
        ("ctx", None),
    ]


def test_handlers_tombstones_direct_import() -> None:
    from bettermemory.handlers.tombstones import (
        DESC_MEMORY_LIST_TOMBSTONES,
        memory_list_tombstones,
    )

    assert isinstance(DESC_MEMORY_LIST_TOMBSTONES, str) and DESC_MEMORY_LIST_TOMBSTONES
    assert inspect.iscoroutinefunction(memory_list_tombstones)
    assert _snapshot_params(memory_list_tombstones) == [
        ("deps", _MISSING),
        ("scopes", None),
        ("ctx", None),
    ]


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
    assert _snapshot_params(memory_update) == [
        ("deps", _MISSING),
        ("id", _MISSING),
        ("content", None),
        ("scopes", None),
        ("confidence", None),
        ("category", None),
        ("links", None),
        ("acknowledge_credential", False),
        ("ctx", None),
    ]


def test_handlers_verify_direct_import() -> None:
    from bettermemory.handlers.verify import DESC_MEMORY_VERIFY, memory_verify

    assert isinstance(DESC_MEMORY_VERIFY, str) and DESC_MEMORY_VERIFY
    assert inspect.iscoroutinefunction(memory_verify)
    assert _snapshot_params(memory_verify) == [
        ("deps", _MISSING),
        ("id", _MISSING),
        ("note", None),
        ("verified_paths", None),
        ("verified_commits", None),
        ("verified_versions", None),
        ("ctx", None),
    ]


def test_handlers_write_direct_import() -> None:
    """Three distinct entry points: ``write``, ``write_confirm``,
    ``write_cancel``. The trio is the pending-write lifecycle and a
    signature drift on any one breaks the others' contract — pin all
    three. ``memory_write``'s parameter list is the longest of any
    handler (the acknowledge_* / groundedness_* / force flags) and the
    most failure-prone surface for silent drops — a removed
    ``acknowledge_ungrounded`` would have slipped past the pre-snapshot
    subset check, which is exactly the gap this rewrite closes."""
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
    assert _snapshot_params(memory_write) == [
        ("deps", _MISSING),
        ("content", _MISSING),
        ("scopes", _MISSING),
        ("confidence", "medium"),
        ("source", "explicit-statement"),
        ("force", False),
        ("acknowledge_transient", False),
        ("acknowledge_scope_mismatch", False),
        ("acknowledge_ungrounded", False),
        ("acknowledge_credential", False),
        ("category", "fact"),
        ("groundedness_check", False),
        ("source_transcript", None),
        ("ctx", None),
    ]
    confirm_cancel_expected = [
        ("deps", _MISSING),
        ("pending_id", _MISSING),
        ("ctx", None),
    ]
    assert _snapshot_params(memory_write_confirm) == confirm_cancel_expected
    assert _snapshot_params(memory_write_cancel) == confirm_cancel_expected


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
