"""`bettermemory session-end` — Claude Code SessionEnd hook: start the
capture of the session that just ended (`capture_hook.on_session_end`).

Named with the cmd suffix like its hook-facing siblings
(`session_start_cmd`, `audit_turn_cmd`, `prompt_recall_cmd`).
"""

from __future__ import annotations

import argparse
import sys


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``session-end`` subparser on the parent parser."""
    help_text = (
        "Start the background capture of a session that just ended. "
        "Intended as a Claude Code SessionEnd hook target: reads the "
        "hook's stdin JSON (`session_id`, `transcript_path`) and, with "
        "[capture] enabled, starts `bettermemory capture` for that "
        "transcript as a detached process, then returns, well inside the "
        "hook's time budget. Does nothing while capture is off. Always "
        "exits 0 so a hook misfire never delays the exit."
    )
    parser = sub.add_parser("session-end", help=help_text, description=help_text)
    parser.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        help="Override the transcript path from the SessionEnd payload.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Override the session id from the SessionEnd payload.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory session-end``. Always exits 0."""
    try:
        payload: dict[str, object] = {}
        if args.transcript_path is None or args.session_id is None:
            from .._fsutil import bounded_stream_read
            from ..hook import _STDIN_PAYLOAD_CAP_BYTES, _read_payload

            try:
                raw = bounded_stream_read(sys.stdin.buffer, _STDIN_PAYLOAD_CAP_BYTES)
            except ValueError:
                raise SystemExit(0) from None
            payload = _read_payload(raw.decode("utf-8", errors="replace"))

        from ..capture_hook import on_session_end
        from ..config import load_config

        on_session_end(
            load_config(),
            args.session_id or payload.get("session_id"),
            args.transcript_path or payload.get("transcript_path"),
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — a hook must never delay the exit
        print(f"bettermemory session-end: {exc}", file=sys.stderr)
    raise SystemExit(0)
