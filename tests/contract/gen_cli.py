"""Fixture 10: the CLI surface (``cli_surface.json``, ungated).

- ``parsers``: ``bettermemory`` and every subcommand as the argparse
  parsers declare them (`cli._build_parser`), read from the parser objects
  and not from the rendered ``--help``, whose wrapping follows the terminal
  width and whose wording follows the Python version: each parser's
  description and, for a subcommand, the help its parent lists it with;
  each argument's flags or name, destination, action, nargs, default,
  type, choices, required, help and metavar.
- ``runs``: ``--version`` and the main refusals, run in process: the exit
  code, the stream written, and its first (or, for a parser error, its
  last) line. A line argparse words differently across Python versions
  (a usage line, which wraps with the terminal; an invalid choice, which
  3.14 lists without quotes) is cut at a fixed prefix, named in
  ``match``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory.cli import _build_parser

from ._common import json_doc
from ._sandbox import PLACEHOLDERS, Scrubber, world

#: The served version every run here reports.
VERSION = "0.0.0+fixture"

_ACTIONS = {
    "_StoreAction": "store",
    "_StoreTrueAction": "store_true",
    "_StoreFalseAction": "store_false",
    "_StoreConstAction": "store_const",
    "_AppendAction": "append",
    "_CountAction": "count",
    "_HelpAction": "help",
    "_VersionAction": "version",
    "_SubParsersAction": "subcommands",
    "BooleanOptionalAction": "boolean_optional",
}


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value]
    raise TypeError(f"no neutral form for a default of {type(value).__name__}")


def _type_name(kind: Any) -> str | None:
    if kind is None:
        return None
    return str(getattr(kind, "__name__", type(kind).__name__))


def _argument(action: argparse.Action) -> dict[str, Any]:
    name = _ACTIONS[type(action).__name__]
    out: dict[str, Any] = {
        "action": name,
        "flags": list(action.option_strings),
        "dest": action.dest,
        "required": bool(action.required),
        "help": action.help,
    }
    if name in ("help", "version"):
        return out
    if name == "subcommands":
        out["choices"] = list(action.choices or {})
        return out
    nargs = action.nargs
    out.update(
        nargs=nargs if nargs is None or isinstance(nargs, int) else str(nargs),
        default=_scalar(action.default),
        type=_type_name(action.type),
        choices=None
        if action.choices is None
        else [_scalar(c) for c in action.choices],
        metavar=_scalar(action.metavar),
    )
    if name == "store_const":
        out["const"] = _scalar(action.const)
    return out


#: The texts every value converter is run on (type_rules).
_TYPE_PROBES = [
    "0",
    "1",
    "7",
    " 7 ",
    "+7",
    "1_000",
    "-1",
    "1.5",
    "0x10",
    "x",
    "",
    "365",
    "366",
    "3660",
    "3661",
    "\u0661",
]


def _type_rules() -> dict[str, Any]:
    """Each value converter an argument names, other than ``str`` (which
    takes the text as given), run on `_TYPE_PROBES`: the value, or the
    refusal argparse prints after ``argument <flag>: ``. argparse prints an
    ArgumentTypeError's text as it is, and ``invalid <name> value:
    <repr>`` for a ValueError or TypeError."""
    root, _ = _build_parser()
    found: dict[str, Any] = {}

    def walk(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in dict.fromkeys(action.choices.values()):
                    walk(sub)
            elif action.type is not None and action.type is not str:
                found.setdefault(str(_type_name(action.type)), action.type)

    walk(root)
    out: dict[str, Any] = {}
    for name, convert in sorted(found.items()):
        reads = []
        for text in _TYPE_PROBES:
            try:
                reads.append({"text": text, "value": _scalar(convert(text))})
            except argparse.ArgumentTypeError as exc:
                reads.append({"text": text, "refused": str(exc)})
            except (TypeError, ValueError):
                reads.append(
                    {"text": text, "refused": f"invalid {name} value: {text!r}"}
                )
        out[name] = reads
    return out


def _parsers() -> list[dict[str, Any]]:
    root, _ = _build_parser()
    out: list[dict[str, Any]] = []

    def walk(parser: argparse.ArgumentParser, path: list[str], help_text: Any) -> None:
        entry: dict[str, Any] = {
            "command": path,
            "description": parser.description,
            "help": help_text,
            "arguments": [],
        }
        children: list[tuple[argparse.ArgumentParser, list[str], Any]] = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                listed = {a.dest: a.help for a in action._choices_actions}
                seen: dict[int, str] = {}
                aliases: dict[str, list[str]] = {}
                for name, sub in action.choices.items():
                    if id(sub) in seen:
                        aliases[seen[id(sub)]].append(name)
                        continue
                    seen[id(sub)] = name
                    aliases[name] = []
                    children.append((sub, [*path, name], listed.get(name)))
                record = _argument(action)
                record["choices"] = list(aliases)
                record["aliases"] = {k: v for k, v in aliases.items() if v}
                entry["arguments"].append(record)
            else:
                entry["arguments"].append(_argument(action))
        out.append(entry)
        for sub, sub_path, sub_help in children:
            walk(sub, sub_path, sub_help)

    walk(root, [], None)
    return out


def _cli(argv: list[str]) -> tuple[int, str, str]:
    from bettermemory.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        patch.object(sys, "argv", ["bettermemory", *argv]),
        patch.object(bettermemory, "__version__", VERSION),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


#: (argv, which line: first, last, or words (the whole output's words
#: joined by one space), the prefix kept or None for the whole line).
_RUNS: list[tuple[list[str], str, str | None]] = [
    (["--version"], "words", None),
    (["config"], "first", "usage: bettermemory config"),
    (["security"], "first", "usage: bettermemory security"),
    (["key"], "first", "usage: bettermemory key"),
    (["nosuch"], "last", "bettermemory: error: argument cmd: invalid choice: "),
    (
        ["config", "nosuch"],
        "last",
        "bettermemory config: error: argument config_cmd: invalid choice: ",
    ),
    (["rename-scope"], "last", None),
    (["health", "--min-applied", "many"], "last", None),
    (["stats", "--since", "0"], "last", None),
    (["config", "check", "--bogus"], "last", None),
]


def _runs() -> list[dict[str, Any]]:
    out = []
    with world() as w:
        scrub = Scrubber(w.root, now=w.now)
        for argv, which, prefix in _RUNS:
            code, stdout, stderr = _cli(argv)
            stream, text = ("stdout", stdout) if stdout else ("stderr", stderr)
            lines = text.splitlines()
            if which == "words":
                # argparse wraps the version string to the terminal width.
                line = " ".join(text.split())
            else:
                line = (lines[0] if which == "first" else lines[-1]) if lines else ""
            record: dict[str, Any] = {
                "argv": argv,
                "exit_code": code,
                "stream": stream,
                "line": which,
            }
            if prefix is None:
                record["match"] = "exact"
                record["text"] = scrub(line)
            else:
                if not line.startswith(prefix):
                    raise AssertionError(f"{argv}: {line!r} lacks {prefix!r}")
                record["match"] = "prefix"
                record["text"] = prefix
            out.append(record)
    return out


def generate() -> dict[str, bytes]:
    doc = {
        "fixture": "cli_surface",
        "read": (
            "parsers: one entry per parser, depth first in declaration order; "
            "command is the words after bettermemory ([] for the root). Each "
            "argument: action (store, store_true, append, help, version, "
            "subcommands, ...), flags ([] for a positional), dest, required, "
            "help (argparse's %-format, unexpanded), and for a value-taking "
            "one nargs (null: one value; ?, *, + as strings), default, type "
            '(the converter\'s name: null and "str" both take the text as '
            "given; any other is ruled in type_rules), choices, metavar. A "
            "subcommands argument lists its choices in order and any aliases. "
            "type_rules: each converter other than str, run on the probe "
            "texts: the value it reads, or the refusal argparse prints after "
            "'argument <flag>: ' (exit 2). "
            "runs: argv -> exit_code, the stream written (stdout when "
            "anything went there), and its first or last line (words: the "
            "whole output's words joined by one space, since argparse wraps "
            "the version to the terminal width): exact, or "
            "only its prefix where argparse's wording varies (a usage line "
            "wraps with the terminal; an invalid choice's list is quoted "
            "differently across Python versions)."
        ),
        "setup": (
            "cli.main in process with sys.argv set and bettermemory."
            f"__version__ = {VERSION!r}; every location variable points into "
            "<scratch> (_sandbox.world), FORCE_COLOR, PYTHON_COLORS, CLICOLOR "
            "and CLICOLOR_FORCE cleared, NO_COLOR=1 and TERM=dumb."
        ),
        "contract": (
            "What a native CLI must reproduce: command and subcommand names "
            "and aliases, their order, flags, dest where it is user-visible "
            "(a positional's name in usage and errors, choices of a "
            "subcommands argument), defaults, choices, required, nargs, the "
            "value rules in type_rules, exit codes and the recorded output "
            "lines and prefixes. Reference only, a description of the "
            "Python reference rather than a contract: action names (argparse "
            "classes), type names (Python converter names), metavar, and "
            "help and description texts (argparse %-format, unexpanded)."
        ),
        "placeholders": {"<scratch>": PLACEHOLDERS["<scratch>"]},
        "version": VERSION,
        "type_rules": _type_rules(),
        "parsers": _parsers(),
        "runs": _runs(),
    }
    return {"cli_surface.json": json_doc(doc)}
