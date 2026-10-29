"""`bettermemory memory move <record id> --project <id>`: a declaration
(design 2.1, 8.8) that re-homes one record to another project, or to
``global``. Like `bettermemory project join`, it prints what it is about
to do and runs `Membership.declare` with the OS user as the principal,
from any stdin.

`bettermemory memory history <record id>` reads instead of declaring:
every log row that made, changed, verified, removed or restored the
record, in log order, with each row's actor and whether its MAC checks
(`bettermemory.history`). It opens the store read-only, as `log verify`
does, so reading a history never creates, migrates or rekeys a store.
"""

from __future__ import annotations

import argparse
import sys


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``memory`` and its ``move`` and ``history`` commands."""
    help_text = (
        "One record at a time. `memory move` re-homes a record to another "
        "project (or global); `memory history` lists every log row that "
        "made, changed or removed it."
    )
    parser = sub.add_parser("memory", help=help_text, description=help_text)
    memory_sub = parser.add_subparsers(dest="memory_cmd")
    text = (
        "Move one memory or episode to project ID (or `global`): its home "
        "names that project from here on, with a log row naming you."
    )
    move = memory_sub.add_parser("move", help=text, description=text)
    move.add_argument("record", metavar="RECORD_ID", help="A memory or episode id.")
    move.add_argument(
        "--project",
        required=True,
        metavar="ID",
        help="The project id (P001), or `global`.",
    )
    history_text = (
        "Every log row that made, changed, verified, removed or restored one "
        "memory or episode, in log order: seq, time, kind, what the row did, "
        "the actor of the tool call (or the record's author on a put row) and "
        "whether the row's MAC checks under this host's key. Links and "
        "conflicts from other records that name it are listed as references. "
        "Read-only. Exit 0 when the history reads clean, 1 when a row fails "
        "its MAC or the live record has no log row that put it as it stands, "
        "2 when no store or no record by that id is found, or the config or "
        "the store file cannot be read."
    )
    history = memory_sub.add_parser(
        "history", help=history_text, description=history_text
    )
    history.add_argument("record", metavar="RECORD_ID", help="A memory or episode id.")
    history.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Add a unified diff of the body under every version that changed "
            "it, the first version against nothing. Bodies are printed as "
            "stored."
        ),
    )
    history.add_argument(
        "--json",
        action="store_true",
        help="Emit the history as JSON (format bettermemory-history, version 1).",
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
    if args.memory_cmd == "history":
        _history(args.record, diff=args.diff, json_out=args.json, parser=sub_parser)
        return
    sub_parser.error(f"unknown memory command: {args.memory_cmd!r}")


def _move(record_id: str, project_id: str) -> None:
    from ..membership import GLOBAL, Declaration
    from .project_cmd import (
        context,
        named,
        open_model,
        principal,
        project_or_refuse,
        refuse,
    )

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
    done = model.declare(
        Declaration("move", principal(), record=record_id, target=target)
    )
    print(f"bettermemory: {record_id} is in {named(model, done.project)}")


def _history(
    record_id: str, *, diff: bool, json_out: bool, parser: argparse.ArgumentParser
) -> None:
    from ..history import record_history, render_json, render_text
    from ..models import is_valid_ulid
    from ._common import open_read_only, read_only_store_path

    # A record id is a ULID. Anything else is refused before the log is
    # scanned: the scan is a substring test, and a short or empty needle
    # would match rows that have nothing to do with any one record.
    if not is_valid_ulid(record_id):
        parser.error(f"{record_id!r} is not a record id (a 26-character ULID)")
    # The config without its first-run write. A read-only open: reading
    # a history must not write a rekey row, as `log verify` must not.
    # Without the key the rows are listed and their MACs reported as
    # unverifiable. A config or a store file that cannot be read exits 2,
    # never 1, which is a finding here.
    path = read_only_store_path(parser)
    store = open_read_only(parser, path)
    try:
        history = record_history(store, record_id)
    finally:
        store.close()
    if not history.known:
        sys.stderr.write(f"bettermemory: no record {record_id} in {path}\n")
        raise SystemExit(2)
    if json_out:
        sys.stdout.write(render_json(history, diff=diff))
    else:
        sys.stdout.write(render_text(history, diff=diff))
    raise SystemExit(history.exit_code)


__all__ = ["add_subparser", "run"]
