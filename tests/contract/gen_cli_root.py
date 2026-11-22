"""Fixtures: the CLI root, `cli/__init__.py`'s `_build_parser`,
`_add_hook_subparser` and `main` (``cli_root.jsonl``,
``cli_root_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/cli/root.rs``.

`main` runs over ``a`` (``sys.argv[1:]``) with every command module's
subparser builder replaced by a stand-in that adds the command's parser
with ``help=`` and ``description=`` both ``"<name> help"`` and one option
``--opt`` (default None), and every handler replaced by one that records
its arm and the ``prog`` of each parser it was passed. The hook subparser
is the reference's own. `_daemon_client.hook_main` answers 7,
`shim.run_shim` records that it ran, `__version__` is ``0.0.test`` and the
formatter is 78 columns wide (``COLUMNS=80``). Each row holds one of:

- ``hook``: the code `route_hook_word` exited with and ``called``, the
  argument `hook_main` received;
- ``shim``: true where no subcommand was given;
- ``arm``: the arm that ran, ``root`` and ``sub`` (the ``prog`` of the
  parser passed as ``root_parser=`` and ``sub_parser=``, null where none
  was) and ``ns``, the namespace;
- ``code``, ``out`` and ``err``: the parser's exit.

The parser's exits (help, version, errors and the unknown subcommand
error) are `argparse`'s texts, which follow the Python release: they go
in ``cli_root_python.jsonl``, written under 3.13.13 alone, as are rows
whose argv holds ``--``, which releases read otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from typing import Any
from unittest import mock

import bettermemory
from bettermemory import _daemon_client, cli, shim

from ._common import jsonl

REPRODUCES = "3.13.13"

#: The name, the module attribute of `bettermemory.cli`, the builder and
#: the handler, in `_build_parser`'s order (without ``hook``).
MODULES = [
    ("health", "health_cmd", "add_subparser", "run"),
    ("init", "init_cmd", "add_subparser", "run"),
    ("migrate", "migrate", "add_subparser", "run"),
    ("export", "export", "add_subparser", "run"),
    ("tombstones", "tombstones", "add_subparser", "run"),
    ("rollback", "rollback", "add_subparser", "run"),
    ("episodes", "episodes", "add_subparser", "run"),
    ("log", "log_cmd", "add_subparser", "run"),
    ("index", "index_cmd", "add_subparser", "run"),
    ("key", "key_cmd", "add_subparser", "run"),
    ("project", "project_cmd", "add_subparser", "run"),
    ("memory", "memory_move_cmd", "add_subparser", "run"),
    ("membership", "membership_cmd", "add_subparser", "run"),
    ("eval", "eval_cmd", "add_subparser", "run"),
    ("review", "review_cmd", "add_subparser", "run"),
    ("capture", "capture_cmd", "add_subparser", "run"),
    ("recall", "recall_cmd", "add_subparser", "run"),
    ("rename-scope", "rename_scope", "add_subparser", "run"),
    ("try", "try_cmd", "add_subparser", "run"),
    ("backup", "backup_cmd", "add_subparser", "run"),
    ("service", "service_cmd", "add_subparser", "run"),
    ("doctor", "doctor_cmd", "add_subparser", "run"),
    ("security", "security_cmd", "add_subparser", "run"),
    ("stats", "stats_cmd", "add_subparser", "run"),
    ("completion", "completion_cmd", "add_subparser", "run"),
    ("config", "config_cmd", "add_subparser", "run"),
    ("up", "daemon_cmd", "add_up", "run_up"),
    ("down", "daemon_cmd", "add_down", "run_down"),
    ("status", "daemon_cmd", "add_status", "run_status"),
    ("serve", "serve", "add_subparser", "run_serve"),
]


def _cases() -> list[list[str]]:
    names = [m[0] for m in MODULES]
    cases: list[list[str]] = [[]]
    cases += [[n] for n in names]
    cases += [
        ["migrate", "--opt", "x"],
        ["rename-scope", "--opt=y"],
        ["serve", "--opt", "1"],
        ["hook"],
        ["hook", "stop"],
        ["session-start"],
        ["audit-turn", "x"],
        ["prompt-recall", "--quiet"],
        ["--", "hook", "stop"],
        ["--", "hook", "prompt", "--quiet", "--dry-run", "--prompt", "p"],
        ["--", "hook", "start"],
        ["--", "health"],
        ["Hook", "stop"],
        ["--version"],
        ["--vers"],
        ["--version", "health"],
        ["-h"],
        ["health", "-h"],
        ["nope"],
        ["health", "extra"],
        ["--opt"],
        ["health", "--opt"],
        ["", "x"],
    ]
    return cases


def _run(argv: list[str]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    patches: list[Any] = []

    def stand_in(name: str) -> Any:
        def add(sub: Any) -> Any:
            p = sub.add_parser(name, help=f"{name} help", description=f"{name} help")
            p.add_argument("--opt", default=None)
            return p

        return add

    def recorder(name: str) -> Any:
        def run(*args: Any, **kw: Any) -> None:
            root = kw.get("root_parser")
            sub = kw.get("sub_parser")
            seen["arm"] = name
            seen["root"] = None if root is None else root.prog
            seen["sub"] = None if sub is None else sub.prog

        return run

    for name, mod, add, run in MODULES:
        target = getattr(cli, mod)
        patches.append(mock.patch.object(target, add, stand_in(name)))
        patches.append(mock.patch.object(target, run, recorder(name)))

    def hook_main(args: list[str]) -> int:
        seen["called"] = list(args)
        return 7

    def run_shim() -> None:
        seen["shim"] = True

    patches += [
        mock.patch.object(_daemon_client, "hook_main", hook_main),
        mock.patch.object(shim, "run_shim", run_shim),
        mock.patch.object(bettermemory, "__version__", "0.0.test", create=True),
        mock.patch.dict(os.environ, {"COLUMNS": "80", "LINES": "24"}),
        mock.patch.object(sys, "argv", ["bettermemory", *argv]),
    ]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        ns_seen: list[Any] = []
        real_parse = argparse.ArgumentParser.parse_args

        def parse_args(self: Any, *a: Any, **kw: Any) -> Any:
            ns = real_parse(self, *a, **kw)
            ns_seen.append(ns)
            return ns

        stack.enter_context(
            mock.patch.object(argparse.ArgumentParser, "parse_args", parse_args)
        )
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            cli.main()
        except SystemExit as e:
            if "called" in seen:
                return {"hook": e.code, "called": seen["called"]}
            return {"code": e.code, "out": out.getvalue(), "err": err.getvalue()}
    if seen.get("shim"):
        return {"shim": True}
    ns = [[k, v] for k, v in vars(ns_seen[-1]).items()]
    return {"arm": seen["arm"], "root": seen["root"], "sub": seen["sub"], "ns": ns}


def _gated(argv: list[str], outcome: dict[str, Any]) -> bool:
    return "code" in outcome or "--" in argv


def _rows(gated: bool) -> list[dict[str, Any]]:
    rows = []
    for argv in _cases():
        outcome = _run(argv)
        if _gated(argv, outcome) == gated:
            rows.append({"a": argv, **outcome})
    return rows


def _header(name: str) -> dict[str, Any]:
    return {"fixture": name, "python": REPRODUCES, "version": "0.0.test", "w": 78}


def generate() -> dict[str, bytes]:
    rows = _rows(False)
    header = {**_header("cli_root"), "cases": len(rows)}
    return {"cli_root.jsonl": jsonl([header, *rows])}


def generate_python() -> dict[str, bytes]:
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header = _header("cli_root_python")
    if running != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = running
        return {"cli_root_python.jsonl": jsonl([header])}
    rows = _rows(True)
    header["cases"] = len(rows)
    return {"cli_root_python.jsonl": jsonl([header, *rows])}
