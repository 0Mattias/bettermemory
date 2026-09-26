"""`bettermemory log verify`: the store's hash-chained log against its key,
its head checkpoint and a fold of its own rows."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``log`` subparser and its ``verify`` command."""
    help_text = (
        "The bettermemory 9 store's hash-chained log. `log verify` checks "
        "every row's MAC against the key kept outside the store, the chain "
        "between rows, the head checkpoint against the last row, and every "
        "table against a replay of the log. Exit 0 when the log verifies, "
        "1 when it is tampered or cannot be verified."
    )
    parser = sub.add_parser("log", help=help_text, description=help_text)
    log_sub = parser.add_subparsers(dest="log_cmd")
    verify_text = (
        "Verify the log: MACs, chain, head and fold. Prints a report and "
        "exits 0 only when every check passes."
    )
    verify = log_sub.add_parser("verify", help=verify_text, description=verify_text)
    verify.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory log ...``."""
    if args.log_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.log_cmd == "verify":
        _cli_log_verify(json_out=args.json, parser=sub_parser)
        return
    sub_parser.error(f"unknown log subcommand: {args.log_cmd!r}")


def _cli_log_verify(*, json_out: bool, parser: argparse.ArgumentParser) -> None:
    from ..config import load_config
    from ..sqlite_store import STORE_FILENAME, SqliteStore

    directory = load_config().resolved_directory()
    path = directory / STORE_FILENAME
    if not path.is_file():
        parser.error(f"no bettermemory 9 store at {path}")
    # `allow_rekey=False`: a verification must not write a rekey row into
    # the log it is verifying. A missing key is then reported, not fixed.
    store = SqliteStore.open(path, allow_rekey=False)
    try:
        report = store.log_verify()
    finally:
        store.close()
    if json_out:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))
    raise SystemExit(0 if report["status"] == "ok" else 1)


def render_report(report: dict[str, Any]) -> str:
    """The text form of a `log_verify` report."""
    head = report["head"]
    head_text = head["status"]
    if "seq" in head:
        head_text += f" at seq {head['seq']}"
    lines = [
        f"bettermemory log verify: {report['status']}",
        f"  store {report['path']}",
        f"  id {report['store_id']}",
        f"  rows {report['rows']} in {len(report['segments'])} segment(s), head {head_text}",
    ]
    for segment in report["segments"]:
        lines.append(
            f"  segment seq {segment['first_seq']} to {segment['last_seq']}: "
            f"{segment['status']} (key {segment['key_fingerprint'][:16]})"
        )
    fold = report["fold"]
    lines.append(f"  fold {fold['status']}")
    for table, counts in fold["tables"].items():
        if counts["unaccounted"] or counts["missing"]:
            lines.append(
                f"    {table}: {len(counts['unaccounted'])} unaccounted, "
                f"{len(counts['missing'])} missing"
            )
            for key in counts["unaccounted"]:
                lines.append(f"      unaccounted {key}")
            for key in counts["missing"]:
                lines.append(f"      missing {key}")
    if report["problems"]:
        lines.append("  problems")
        for problem in report["problems"]:
            lines.append(
                f"    seq {problem['seq']} ({problem['kind']}): {problem['problem']}"
            )
    return "\n".join(lines) + "\n"
