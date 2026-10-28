"""`bettermemory prompt-recall` — Claude Code UserPromptSubmit recall.

A suffix-free basename would be safe today (no handlers/prompt_recall
sibling exists), but the module carries the cmd suffix anyway: the
command is hook-facing like `audit_turn_cmd` / `session_start_cmd`,
and a future in-process handler twin is exactly the collision the
round-3 rename existed to prevent, so the name follows its siblings.
"""

from __future__ import annotations

import argparse


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``prompt-recall`` subparser on the parent parser."""
    help_text = (
        "Pointer recall for a just-submitted prompt. Intended as a Claude "
        "Code UserPromptSubmit hook target: reads the hook's stdin JSON "
        "(`session_id`, `prompt`), runs the probe the Stop hook's "
        "silent-miss audit runs (same pool and ranker — "
        "`hook._probe_message`), and prints up to three pointers (id, "
        "relevance, summary, snippet) to relevant memories written in this "
        "repository that this session segment has not already seen. Claude "
        "Code injects stdout into the model's context; empty stdout adds "
        "nothing, which is the common case. A delivery is recorded as a "
        "`prompt_recall` event, which the audit counts as retrieval, so the "
        "same turn is not re-flagged. Widen with `[behavior] prompt_recall "
        '= "everywhere"`, disable with `"off"`. Use --prompt + '
        "--session-id to invoke manually for debugging. Always exits 0 "
        "so a hook misfire never blocks a prompt."
    )
    parser = sub.add_parser("prompt-recall", help=help_text, description=help_text)
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Override the prompt text from the UserPromptSubmit payload.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Override the session id from the UserPromptSubmit payload.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory prompt-recall``. A store file
    inside a backup bundle is not opened: a stderr line and exit 0, the
    hook's code for a store it cannot read."""
    from ..hook import prompt_main as _prompt_main
    from ._common import exit_if_bundle_store

    exit_if_bundle_store(prefix="[bettermemory] prompt-recall")

    raise SystemExit(
        _prompt_main(
            [
                *(["--prompt", args.prompt] if args.prompt else []),
                *(["--session-id", args.session_id] if args.session_id else []),
            ]
        )
    )
