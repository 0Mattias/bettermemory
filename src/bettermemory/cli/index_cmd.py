"""`bettermemory index rebuild`: rebuild the store's full-text search index.

The repair the index checks name (ENG-100): every memory row's index text
(`body_fts`, `scopes_fts`) spelled again from its body and scopes by this
build's tokenizer, the FTS5 table rebuilt from those columns, and meta's
tokenizer fingerprint set, in one write transaction that one
`index_rebuilt` log row records (`store_index.rebuild_index`). No record
changes, so the fold and `log verify` read the store as before, and a
memory the index had lost, or an entry it held for text no row carries,
is found as its body says again.

A command of its own rather than an option of `doctor`: doctor reads
only, by contract (`bettermemory.doctor`'s module docstring, held by
`test_doctor_writes_nothing`), so that inspecting a store never changes
what it inspects, and a repair that writes belongs beside the other
writing commands. It opens the store as they do (`cli_context`, the
owner's open), without the membership migration at open, which is not
this command's to run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, NoReturn


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``index`` and its ``rebuild``."""
    help_text = (
        "The store's full-text search index. `index rebuild` spells every "
        "memory's index text again from its body and scopes and rebuilds the "
        "FTS5 table from it, the repair doctor and backup verify name for an "
        "index drift."
    )
    parser = sub.add_parser("index", help=help_text, description=help_text)
    index_sub = parser.add_subparsers(dest="index_cmd")
    text = (
        "Rebuild the store's full-text search index in one write transaction: "
        "every memory's index text spelled again from its body and scopes by "
        "this build's tokenizer, the FTS5 table rebuilt from it, and one "
        "index_rebuilt log row. No record changes. Exit 0 when rebuilt, 1 when "
        "the store cannot be written (busy, full, or open for reading only)."
    )
    rebuild = index_sub.add_parser("rebuild", help=text, description=text)
    rebuild.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit {status, path, reason, memories, respelled, old_fingerprint, "
            "new_fingerprint, seq, seconds} as JSON instead of text."
        ),
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory index``."""
    if args.index_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.index_cmd == "rebuild":
        _rebuild(as_json=args.json)
    sub_parser.error(f"unknown index subcommand: {args.index_cmd!r}")


def _refuse(why: str) -> NoReturn:
    sys.stderr.write(f"bettermemory index rebuild: {why}\n")
    raise SystemExit(1)


def _rebuild(*, as_json: bool) -> None:
    from ..store_index import REBUILT_FOR_REPAIR, cannot_write, rebuild_index
    from ._common import cli_context

    ctx = cli_context(migrate=False)
    store = ctx.store
    try:
        why = cannot_write(store)
        if why is not None:
            _refuse(f"the store at {store.path} cannot be rebuilt here: {why}")
        try:
            rebuilt = rebuild_index(store, reason=REBUILT_FOR_REPAIR)
        except sqlite3.Error as exc:
            _refuse(
                f"the store at {store.path} was not rebuilt ({exc}); nothing changed"
            )
    finally:
        store.close()
    assert rebuilt is not None  # a repair always rebuilds
    report: dict[str, Any] = {
        "status": "rebuilt",
        "path": str(store.path),
        "reason": rebuilt.reason,
        "memories": rebuilt.memories,
        "respelled": rebuilt.respelled,
        "old_fingerprint": rebuilt.old_fingerprint,
        "new_fingerprint": rebuilt.new_fingerprint,
        "seq": rebuilt.seq,
        "seconds": round(rebuilt.seconds, 6),
    }
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        old = (rebuilt.old_fingerprint or "none")[:16]
        sys.stdout.write(
            "bettermemory index rebuild: rebuilt\n"
            f"  store {report['path']}\n"
            f"  {rebuilt.memories} memories, {rebuilt.respelled} of them "
            "re-spelled from their bodies; the FTS5 index rebuilt from them\n"
            f"  tokenizer {rebuilt.new_fingerprint[:16]} (meta named {old})\n"
            f"  log row seq {rebuilt.seq} (index_rebuilt), "
            f"{rebuilt.seconds:.3f} s under the write lock\n"
        )
    raise SystemExit(0)


__all__ = ["add_subparser", "run"]
