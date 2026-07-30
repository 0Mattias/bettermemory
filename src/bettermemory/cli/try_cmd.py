"""`bettermemory try` — an instant, zero-network demo of the staleness verdict.

bettermemory's headline differentiator — every retrieved fact carries a
freshness signal (path drift / commit drift / calendar age) — is invisible on
a fresh store, because nothing has drifted yet. A new evaluator can run for
weeks before a real path moves. This command makes it happen on demand: it
writes a memory that cites a file, attests it while the file exists, then
deletes the file — and shows the very next search flagging the memory
(`staleness_verdict: spot_check_recommended`, `path_drift.missing` populated).

Everything runs in an isolated temp store; the user's real store is never
touched, and nothing hits the network.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    help_text = (
        "Instant offline demo: watch a memory go stale when a file it cites moves."
    )
    parser = sub.add_parser("try", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw search hit (the exact MCP-shaped dict) instead of the narrated walkthrough.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    # Lazy imports so `bettermemory --help` stays cheap.
    import json
    import sys
    import tempfile
    from pathlib import Path

    from .._response import ResponseBuilder
    from ..models import Category, utcnow
    from ..search import search as run_search
    from ..store import Store

    with tempfile.TemporaryDirectory(prefix="bettermemory-try-") as tmp:
        root = Path(tmp)
        store = Store(root / "store")

        # A file the memory will cite. Multi-segment with an extension so the
        # path-drift extractor treats it as a real path (single-segment,
        # extensionless, and placeholder paths are deliberately ignored).
        target = root / "src" / "auth" / "session.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def validate_session_token(tok): ...\n", encoding="utf-8")

        body = (
            f"The session-auth token validator lives at `{target}` and rejects "
            "expired tokens before the request reaches the handler."
        )
        mem = store.write(
            content=body, scopes=["projects:demo"], category=Category.FACT
        )

        # Attest it WHILE the file exists, so the verdict below is driven by
        # path drift specifically — not the never-verified default that a
        # brand-new memory would otherwise carry.
        #
        # `verified_paths` is part of the attestation, not decoration. Until
        # path drift got a provenance split this line stamped only the
        # timestamp, and the demo still reproduced because ANY missing path
        # escalated the verdict — including one merely scraped out of prose.
        # The verdict now escalates on claim-anchored evidence only, so the
        # demo has to attest the way its own narration already claimed it
        # did ("attested it while the file existed"). Same three steps, and
        # the sentence is true now.
        store.update(
            mem.model_copy(
                update={
                    "last_verified_at": utcnow(),
                    "verified_paths": [str(target)],
                }
            )
        )

        # …the code gets refactored and that file moves / is deleted.
        target.unlink()

        hits = run_search(
            store.load_all(),
            "session auth token validator",
            mode="keyword",
        )
        rb = ResponseBuilder(stale_after_days=30)
        row = rb.hit_to_dict(hits[0], now=utcnow()) if hits else None

        reproduced = bool(
            row is not None
            and row.get("staleness_verdict") != "fresh"
            and row.get("path_drift", {}).get("missing")
        )

        if args.json:
            sys.stdout.write(json.dumps(row, indent=2, default=str) + "\n")
        else:
            # The memory cites — and drift-checks — the absolute path, but a
            # raw /var/folders/… tempdir buries the one thing the demo is
            # showing. Narrate paths relative to the throwaway root. The
            # narration says so, and never presents a rewritten path as a
            # verbatim quote of the stored body: `--json` shows the real
            # absolute paths, and the two must not appear to disagree.
            sys.stdout.write(_narrate(target, root, row))

        # Exit non-zero if the demo somehow failed to reproduce the drift —
        # that doubles as a self-test of the whole verify→drift→verdict path.
        raise SystemExit(0 if reproduced else 1)


def _narrate(target: Path, root: Path, row: dict[str, Any] | None) -> str:
    if row is None:
        return (
            "bettermemory try: the demo search returned no hit — that's a bug, "
            "please report it.\n"
        )
    from pathlib import Path

    verification = row.get("verification", {}).get("status", "?")
    verdict = row.get("staleness_verdict", "?")

    def _rel(p: str) -> str:
        """Path-aware, so Windows' backslashes strip as cleanly as POSIX's.
        Anything outside the throwaway root is left exactly as it came."""
        try:
            return Path(p).relative_to(root).as_posix()
        except ValueError:
            return p

    missing = [_rel(p) for p in row.get("path_drift", {}).get("missing", [])]
    lines = [
        "bettermemory try — a memory going stale, live. Offline, instant.",
        "",
        "1. Stored a memory that cites a file, and attested it while the file existed:",
        f"     cites: {_rel(str(target))}",
        f"     verification.status: {verification}",
        "",
        "2. The code got refactored — that file moved / was deleted.",
        "",
        "3. Your very next search flags the memory automatically:",
        f"     staleness_verdict:  {verdict}",
        f"     path_drift.missing: {missing}",
        "",
        "That's the difference: the model is TOLD a memory may have rotted (a path",
        "it cited is gone) before it relies on it — instead of quoting a stale fact.",
        "",
        "Paths above are shown relative to the throwaway store root; the memory",
        "itself cites absolute paths, which is what `bettermemory try --json`",
        "prints. Nothing was written to your real store, and nothing hit the",
        "network.",
        "",
    ]
    return "\n".join(lines)
