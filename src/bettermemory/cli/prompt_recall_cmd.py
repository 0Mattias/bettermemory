"""`bettermemory prompt-recall` — Claude Code UserPromptSubmit recall.

The `_cmd`-free basename is safe here (no `handlers/prompt_recall.py`
sibling exists), but the module keeps the suffix-free name short of one
anyway: the command is hook-facing like `audit_turn_cmd` /
`session_start_cmd`, and a future in-process handler twin is exactly
the collision the round-3 rename existed to prevent, so the name
follows its siblings.
"""

from __future__ import annotations

import argparse


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``prompt-recall`` subparser on the parent parser."""
    help_text = (
        "Score-gated memory recall for a just-submitted prompt. "
        "Intended as a Claude Code UserPromptSubmit hook target: reads "
        "the hook's stdin JSON (`session_id`, `prompt`), runs the SAME "
        "probe the Stop hook's silent-miss audit runs (same pool, "
        "ranker, threshold rule, and shields — `hook._probe_message`), "
        "and prints a one-hit pointer block (id + scopes + snippet) "
        "only when that probe says the turn would otherwise be flagged "
        "a silent miss. Claude Code injects stdout into the model's "
        "context; empty stdout adds nothing, which is the common case "
        "(~2% of audited turns fire). A delivered recall is recorded "
        "as a `prompt_recall` event, which the audit counts as "
        "retrieval — so the same turn is not re-flagged and a second "
        "injection is suppressed for the attribution window. Disable "
        "with `[behavior] prompt_recall = false`. Use --prompt + "
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
    """Dispatch handler for ``bettermemory prompt-recall``."""
    from ..hook import prompt_main as _prompt_main

    raise SystemExit(
        _prompt_main(
            [
                *(["--prompt", args.prompt] if args.prompt else []),
                *(["--session-id", args.session_id] if args.session_id else []),
            ]
        )
    )
