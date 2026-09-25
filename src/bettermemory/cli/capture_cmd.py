"""`bettermemory capture` — distil a Claude Code session transcript into
dated memories through the write gates (`bettermemory.capture`).

Named `capture_cmd` rather than `capture` for the reason
`cli/__init__.py` gives for `health_cmd`: `bettermemory/capture.py` is the
module this command drives, and two `capture.py` files would be the
basename collision that rename removed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._common import CliContext, cli_context, cli_recorder


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``capture`` subparser."""
    from ..capture import DEFAULT_MAX_SEGMENTS

    help_text = (
        "Distil a Claude Code session transcript into dated memories, "
        "written through the same gates as memory_write and tagged "
        "`session-capture`. Reads from where the last capture of the "
        "session stopped; --dry-run shows what would be saved. The "
        "memories are written by the model the session was talking to, "
        "on Claude Code's own login. With [capture] enabled the hooks run "
        "it in the background."
    )
    parser = sub.add_parser("capture", help=help_text, description=help_text)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--transcript",
        metavar="PATH",
        help="The session's JSONL transcript (Claude Code's transcript_path).",
    )
    target.add_argument(
        "--pending",
        action="store_true",
        help=(
            "Capture the sessions the hooks registered that went quiet "
            "with something uncaptured (what the SessionStart hook runs)."
        ),
    )
    parser.add_argument(
        "--session-id",
        default=None,
        metavar="ID",
        help="Session id. Defaults to the transcript's file name.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Call the model and run every gate, but write nothing: no "
            "memories, no watermark, no segment files."
        ),
    )
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help=(
            "The session is still going on: capture its whole segments, "
            "hold the newest back, and exit at once if another capture of "
            "it is running (what the Stop hook runs)."
        ),
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=DEFAULT_MAX_SEGMENTS,
        metavar="N",
        help=(
            "Segments (about 12k tokens each) to process in this run; the "
            f"rest wait for the next. Default {DEFAULT_MAX_SEGMENTS}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch entry for ``bettermemory capture``."""
    from ..capture import (
        CHILD_ENV,
        TRIGGER,
        CaptureBusy,
        CaptureError,
        capture_transcript,
        render_text,
    )

    if os.environ.get(CHILD_ENV):
        # Started inside a capture's own model call: capturing that
        # would feed a capture back into itself.
        return
    if args.max_segments < 1:
        print("capture: --max-segments must be at least 1", file=sys.stderr)
        sys.exit(2)
    if args.pending and (args.session_id or args.checkpoint):
        print(
            "capture: --pending takes neither --session-id nor --checkpoint",
            file=sys.stderr,
        )
        sys.exit(2)

    ctx = cli_context()
    if args.pending:
        _run_pending(args, ctx)
        return
    transcript = Path(args.transcript)
    session = args.session_id or transcript.stem
    try:
        recorder = cli_recorder(
            ctx,
            attribution="cli_capture",
            session_id=session,
            triggered_from=TRIGGER,
        )
        report = capture_transcript(
            store=ctx.store,
            config=ctx.config,
            recorder=recorder,
            transcript=transcript,
            session_id=args.session_id,
            dry_run=args.dry_run,
            max_segments=args.max_segments,
            hold_tail=args.checkpoint,
            wait=not args.checkpoint,
        )
    except CaptureBusy as exc:
        # A checkpoint defers to the capture already running: that run
        # reads on to the same lines.
        print(f"capture: {exc}", file=sys.stderr)
        return
    except CaptureError as exc:
        print(f"capture: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    if any(s.status == "failed" for s in report.segments):
        sys.exit(1)


def _run_pending(args: argparse.Namespace, ctx: CliContext) -> None:
    """`--pending`: capture each session `capture_hook.pending_sessions`
    names, one after another. A session another capture is running, or
    one that cannot be read, is left for the next sweep; the exit status
    is 1 only when a model call failed."""
    from ..capture import (
        TRIGGER,
        CaptureBusy,
        CaptureError,
        capture_transcript,
        render_text,
    )
    from ..capture_hook import pending_sessions

    pending = pending_sessions(
        ctx.directory, idle_seconds=ctx.config.capture.idle_minutes * 60
    )
    if not pending:
        if not args.json:
            print("capture: no session is waiting")
        else:
            print("[]")
        return
    reports = []
    failed = False
    for item in pending:
        recorder = cli_recorder(
            ctx,
            attribution="cli_capture",
            session_id=item.session_id,
            triggered_from=TRIGGER,
        )
        try:
            report = capture_transcript(
                store=ctx.store,
                config=ctx.config,
                recorder=recorder,
                transcript=item.transcript,
                session_id=item.session_id,
                dry_run=args.dry_run,
                max_segments=args.max_segments,
                wait=False,
            )
        except CaptureBusy:
            continue
        except CaptureError as exc:
            print(f"capture: {item.session_id}: {exc}", file=sys.stderr)
            continue
        reports.append(report)
        if any(s.status == "failed" for s in report.segments):
            failed = True
            # The same login would fail the next session too.
            break
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        print("\n\n".join(render_text(r) for r in reports))
    if failed:
        sys.exit(1)
