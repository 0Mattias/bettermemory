"""Command metadata and portable selections from the production Python CLI parser."""

from __future__ import annotations

import argparse
import os
from typing import Any
from unittest.mock import patch

from bettermemory.cli import _build_parser

from ._common import jsonl
from .gen_argparse import _t
from .gen_cli_log_args import _parse


def _action(action: argparse.Action) -> dict[str, Any]:
    # The native parser represents actions which exit without assigning a
    # namespace attribute (help and version) with a suppressed destination.
    exits = isinstance(action, (argparse._HelpAction, argparse._VersionAction))
    return {
        "kind": type(action).__name__,
        "flags": action.option_strings,
        "dest": None if exits else action.dest,
        "default": [] if action.default == argparse.SUPPRESS else [action.default],
        "metavar": action.metavar,
        "required": action.required,
        "help": action.help,
        "choices": None if action.choices is None else list(action.choices),
        "version": getattr(action, "version", None),
    }


def _node(parser: argparse.ArgumentParser) -> dict[str, Any]:
    choices = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    return {
        "prog": parser.prog,
        "description": parser.description,
        "actions": [_action(a) for a in parser._actions],
        "choice_help": (
            [[a.dest, a.help] for a in choices._choices_actions] if choices else []
        ),
    }


def generate() -> dict[str, bytes]:
    version = "3.7-registry"
    env = {"COLUMNS": "512", "PYTHON_COLORS": "0", "NO_COLOR": "1"}
    with patch.dict(os.environ, env), patch("bettermemory.__version__", version):
        root, commands = _build_parser()
        log = commands["log"]
        log_action = next(
            a for a in log._actions if isinstance(a, argparse._SubParsersAction)
        )
        root_action = next(
            a for a in root._actions if isinstance(a, argparse._SubParsersAction)
        )
        rows: list[Any] = [
            {"path": path, "node": _node(parser)}
            for path, parser in (
                ([], root),
                (["log"], log),
                (["log", "verify"], log_action.choices["verify"]),
                (["hook"], root_action.choices["hook"]),
            )
        ]
        for argv in (
            [],
            ["--version"],
            ["log"],
            ["log", "verify"],
            ["log", "verify", "--anchor", "one", "--json", "--a=\udcff", "--a="],
            ["log", "verify", "--anchor"],
            ["log", "verify", "--anchor", "--help"],
            ["log", "verify", "--unknown"],
            ["hook", "session-start", "--quiet", "--session-id", "id"],
            ["hook", "stop", "--dry-run", "--transcript-path", "session.json"],
            ["hook", "prompt", "--prompt=\udc80"],
        ):
            rows.append(
                {"argv": [_t(arg) for arg in argv], "outcome": _parse(root, argv)}
            )
    return {"cli_registry.jsonl": jsonl(rows)}
