"""`bettermemory init` — onboard a fresh install with an MCP config snippet."""

from __future__ import annotations

import argparse
import sys


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``init`` subparser on the parent parser."""
    parser = sub.add_parser(
        "init",
        help=(
            "Onboard a fresh install: print the MCP config snippet, or "
            "auto-patch a known client's config. Idempotent."
        ),
    )
    parser.add_argument(
        "--client",
        type=str,
        default=None,
        choices=["claude-code", "claude-desktop", "cursor", "continue", "cline"],
        help=(
            "Auto-patch the named client's MCP config. Without this "
            "flag, init runs in show-and-tell mode: prints the snippet "
            "and the common config locations so you can copy by hand."
        ),
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help=(
            "Just print the JSON snippet (and target path, when --client "
            "is set) without writing anything. Useful for piping into "
            "jq or for review before applying."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON view (binary path, snippet, known clients).",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Server key under `mcpServers`. Default: `bettermemory` "
            "(specific enough to never collide with another MCP server). "
            "Override only if you have a strong reason — Claude Code's "
            "tool names are prefixed with this key."
        ),
    )
    parser.add_argument(
        "--with-addendum",
        action="store_true",
        help=(
            "Also print docs/system_prompt.md (the long-form policy). "
            "The MCP `instructions` block carries the core rules at "
            "the system-prompt level on every compliant client, but "
            "Claude Code truncates it at ~1.8KB. Print the addendum "
            "and paste into your CLAUDE.md to keep the writing-"
            "discipline / scope-hygiene / verification-ceremony "
            "detail in scope. The Claude Code plugin ships the same "
            "content as a SKILL.md — you don't need both."
        ),
    )
    parser.add_argument(
        "--config-path",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the default target file for --client. Use this "
            "to write into a project-scoped MCP config instead of the "
            "user-scoped default."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory init``."""
    from pathlib import Path as _Path

    from ..init import cli_init

    try:
        cli_init(
            client=args.client,
            print_only=args.print_only,
            json_out=args.json,
            name=args.name,
            with_addendum=args.with_addendum,
            config_path=_Path(args.config_path) if args.config_path else None,
        )
    except (OSError, ValueError) as exc:
        # The config-patch write (patch_client_config -> mkdir /
        # atomic_write_bytes) can fail on an unwritable or non-directory
        # --config-path parent (PermissionError / NotADirectoryError /
        # ENOSPC). Render a clean error + exit 2 instead of a raw
        # traceback, mirroring the ImportError -> exit-2 pattern in
        # `bettermemory ui`. (A plain nonexistent path does NOT reach here
        # — mkdir(parents=True) creates the tree; only a genuinely
        # unwritable or non-directory ancestor raises.)
        #
        # patch_client_config also raises ValueError on a malformed existing
        # config (bad JSON / non-object root / non-object mcpServers) and on
        # a concurrent-write race — those are equally user-facing "fix and
        # re-run" conditions, not bugs, so they get the same clean exit 2
        # rather than escaping as a raw traceback / exit 1.
        sys.stderr.write(f"bettermemory init: error: {exc}\n")
        raise SystemExit(2) from exc
