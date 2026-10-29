"""`bettermemory security`: the install's security posture, from the CLI.

A thin group. Each subcommand's work lives in its own module, which this
file registers and dispatches to: `security audit` (`posture`), the
read-only check of modes, owners, the key, where the store lives, the
daemon's address and Claude Code's sandbox settings; and `security sandbox`
(`sandbox_settings`), which prints the Claude Code sandbox block for this
host's real paths; and `security scan` (`secret_scan`), the secret scan
over stored bodies, episodes and the signed log. None writes a row, a
key or a file of its own (the scan's open switches a store file saved in
rollback-journal mode to WAL, as every opener does, and so refuses a
backup bundle's file), and none opens the store through `cli_context` or
`Store.open_or_create`, which create and rekey.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from ..sandbox_settings import FORMATS, SandboxPlan
from ..secret_scan import add_scan_parser, run_scan


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``security`` group and its subcommands."""
    help_text = (
        "Security checks for this install. `security audit` reports whether "
        "the store, the key and the daemon's files are as SECURITY.md assumes; "
        "`security sandbox` prints Claude Code sandbox settings for this "
        "host's paths; `security scan` finds credentials kept in the store. None "
        "writes a row, a key or a file of its own."
    )
    parser = sub.add_parser("security", help=help_text, description=help_text)
    security_sub = parser.add_subparsers(dest="security_cmd")

    audit_text = (
        "Read-only posture audit: the modes and owners of the store, its WAL "
        "files, the keys, the head checkpoint, the daemon's state file and "
        "log; the key present and matching the store; the store and the keys "
        "outside git working trees and synced folders; a store chosen by "
        "./.claude-memory; the daemon on 127.0.0.1 only; the config's "
        "telemetry and policy settings; and Claude Code's sandbox settings "
        "against `security sandbox`. Never chmods, rekeys or writes. Exit 0 "
        "when nothing needs attention, 1 when any finding is high, 2 when the "
        "worst is a warning."
    )
    audit = security_sub.add_parser("audit", help=audit_text, description=audit_text)
    audit.add_argument(
        "--json",
        action="store_true",
        help="Emit the findings as JSON instead of text.",
    )

    sandbox_text = (
        "Print the Claude Code sandbox settings for this host: sandbox on and "
        "locked, and the store, keys, config and state directories denied to "
        "the agent's shell and to Claude Code's Read and Edit tools, spelled "
        "for this platform (macOS keeps them under ~/Library/Application "
        "Support) and with any BETTERMEMORY_* override applied. The block goes "
        "to stdout and the notes to stderr; nothing is written. Exit 0, or 1 "
        "when the config does not load."
    )
    sandbox = security_sub.add_parser(
        "sandbox", help=sandbox_text, description=sandbox_text
    )
    sandbox.add_argument(
        "--format",
        choices=FORMATS,
        default="settings",
        help=(
            "Which Claude Code settings file the block is for: `settings`, the "
            "user's settings (~/.claude/settings.json, or "
            "$CLAUDE_CONFIG_DIR/settings.json when that is set; the default), "
            "or `managed`, the managed-settings.json nothing below overrides. "
            "The keys are the same; the notes name the file."
        ),
    )
    sandbox.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit one JSON object: the block under `settings`, with the "
            "directories it was built from, the daemon's port and the notes."
        ),
    )
    add_scan_parser(security_sub)
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory security ...``."""
    if args.security_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    runner = _RUNNERS.get(args.security_cmd)
    if runner is None:
        sub_parser.error(f"unknown security subcommand: {args.security_cmd!r}")
    runner(args)


def _run_audit(args: argparse.Namespace) -> None:
    from ..posture import audit, render

    report = audit()
    if args.json:
        sys.stdout.write(json.dumps(report.to_json(), indent=2) + "\n")
    else:
        sys.stdout.write(render(report))
    raise SystemExit(report.exit_code)


def _run_sandbox(args: argparse.Namespace) -> None:
    from ..sandbox_settings import build_plan, home_dir, load_config_read_only

    try:
        config = load_config_read_only()
    except (ValueError, OSError) as exc:
        sys.stderr.write(
            f"bettermemory security sandbox: the config does not load: {exc}\n"
        )
        raise SystemExit(1) from None
    plan = build_plan(config, fmt=args.format)
    if args.json:
        sys.stdout.write(json.dumps(plan.to_json(home_dir()), indent=2) + "\n")
        return
    sys.stdout.write(json.dumps(plan.settings, indent=2) + "\n")
    sys.stderr.write(render_notes(plan))


def render_notes(plan: SandboxPlan) -> str:
    """The stderr half of the text output: what the block was built from
    and the notes, so stdout stays the block alone and pipes into a file."""
    which = "managed settings" if plan.format == "managed" else "user settings"
    lines = [f"# bettermemory security sandbox: for {plan.destination} ({which})"]
    for loc in plan.locations:
        mark = "" if loc in plan.denied else " (covered by another entry)"
        lines.append(f"#   {loc.role}: {loc.path} [{loc.source}]{mark}")
    for note in plan.notes:
        lines.append(f"# {note}")
    return "\n".join(lines) + "\n"


_RUNNERS: dict[str, Callable[[argparse.Namespace], None]] = {
    "audit": _run_audit,
    "sandbox": _run_sandbox,
    "scan": run_scan,
}
