"""`bettermemory memory move <record id> --project <id>`: a declaration
(decision D6, design 2.1) that re-homes one record to another project, or
to ``global``. Like `bettermemory project join`, it refuses unless stdin is
a terminal, prints what it is about to do and asks for the target
project's name to be typed exactly before `Membership.declare` runs with
the OS user as the principal; the installer denies it to Claude Code.
"""

from __future__ import annotations

import argparse


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``memory`` and its ``move`` command."""
    help_text = (
        "Declarations about one record. `memory move` re-homes a record to "
        "another project (or global); it needs a terminal and the project's "
        "name typed to confirm."
    )
    parser = sub.add_parser("memory", help=help_text, description=help_text)
    memory_sub = parser.add_subparsers(dest="memory_cmd")
    text = (
        "Move one memory or episode to project ID (or `global`): its home "
        "names that project from here on, with a log row naming you. Needs a "
        "terminal and the project's name typed to confirm."
    )
    move = memory_sub.add_parser("move", help=text, description=text)
    move.add_argument("record", metavar="RECORD_ID", help="A memory or episode id.")
    move.add_argument(
        "--project",
        required=True,
        metavar="ID",
        help="The project id (P001), or `global`.",
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory memory ...``."""
    if args.memory_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.memory_cmd == "move":
        _move(args.record, args.project)
        return
    sub_parser.error(f"unknown memory command: {args.memory_cmd!r}")


def _move(record_id: str, project_id: str) -> None:
    from ..membership import GLOBAL, Declaration
    from .project_cmd import (
        confirm,
        context,
        named,
        open_model,
        principal,
        project_or_refuse,
        refuse,
        require_terminal,
    )

    require_terminal("bettermemory memory move")
    ctx = context()
    model = open_model(ctx)
    home = model.home(record_id)
    if home is None:
        refuse(f"{record_id} has no home recorded; nothing changed")
    target = GLOBAL if project_id == GLOBAL else project_or_refuse(model, project_id)
    if target == home.project:
        print(f"bettermemory: {record_id} is already in {named(model, target)}")
        return
    written = f" (written in checkout {home.checkout})" if home.checkout else ""
    print(
        f"bettermemory memory move: {home.kind} {record_id}{written} leaves "
        f"{named(model, home.project)} for {named(model, target)}"
    )
    expected = GLOBAL if target == GLOBAL else model.project(target).name
    confirm(expected, "the project's name")
    done = model.declare(
        Declaration("move", principal(), record=record_id, target=target)
    )
    print(f"bettermemory: {record_id} is in {named(model, done.project)}")


__all__ = ["add_subparser", "run"]
