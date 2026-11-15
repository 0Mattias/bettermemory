"""The instrument panel: what bettermemory costs and gives in real use.

One read-only program over a copy of a store and the local Claude Code
transcripts. It prints and writes one receipt of aggregates:

  eval        the helped rate (Y1), the endorsement and silent-miss rates,
              the write review's calls and characters per committed write
              (Y6) and the recall surfaces, as `bettermemory eval` computes
              them (`store_side.eval_section`).
  verdicts    Y5: of the served shows whose verdict asked for a check, the
              share whose check found drift (`store_side.verdict_section`).
  pointers    the brief's and the recall's settled pointers by rank,
              applied or unused (`store_side.pointer_section`).
  records     every active record's lifetime use by age and standing:
              never retrieved, never opened, never applied
              (`store_side.record_section`).
  transcripts Y2: characters per bettermemory tool response and per hook
              injection, the hooks' wall time as Claude Code measured it
              (process start included), calls and characters per committed
              write, and bettermemory's share of the context that tools and
              hooks added in the sessions it took part in
              (`transcripts.read`).
  latency     Y3: the newest bench/scale receipt at 1x, 10x and 100x, cited
              by file and commit, not re-run.

The store is copied through a read-only connection into a scratch
directory (`store_side.snapshot`) unless `--copy` names a copy already
made. The record rows name records, so they are written only to
`--private-out`, which must lie outside the repository; the receipt holds
aggregates only: no memory id, prompt, query, path or body.

    python bench/panel/run.py --since 2026-11-04 \\
        --out bench/panel/results/panel-2026-11-15.json \\
        --private-out ~/.cache/bettermemory-v9/receipts/panel-2026-11-15
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT / "src"))

import store_side  # noqa: E402
import transcripts  # noqa: E402

#: The scales bench/scale measures.
SCALES = ("x1", "x10", "x100")


def program_sha256() -> str:
    digest = hashlib.sha256()
    for name in ("run.py", "store_side.py", "transcripts.py"):
        digest.update((_HERE / name).read_bytes())
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def _day(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _timed_arms(node: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Every arm of a scale receipt with a p50, by its dotted path."""
    out: dict[str, dict[str, Any]] = {}
    if isinstance(node, dict):
        if "p50_ms" in node:
            out[prefix] = {k: node[k] for k in ("n", "p50_ms", "p95_ms") if k in node}
            return out
        for key, value in node.items():
            out.update(_timed_arms(value, f"{prefix}.{key}" if prefix else key))
    return out


def latency_section(results: Path) -> dict[str, Any]:
    """The newest `measure` receipt per scale, its timed arms cited."""
    out: dict[str, Any] = {"source": "bench/scale, cited, not re-run"}
    for scale in SCALES:
        newest: tuple[str, Path] | None = None
        for path in results.glob(f"scale-*-{scale}-measure*.json"):
            try:
                at = str(json.loads(path.read_text(encoding="utf-8")).get("at") or "")
            except (OSError, ValueError):
                continue
            if newest is None or at > newest[0]:
                newest = (at, path)
        if newest is None:
            out[scale] = None
            continue
        data = json.loads(newest[1].read_text(encoding="utf-8"))
        out[scale] = {
            "file": newest[1].name,
            "commit": data.get("commit"),
            "at": data.get("at"),
            "arms": _timed_arms((data.get("measure") or {}).get("arms") or {}),
        }
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _pct(rate: dict[str, Any] | None) -> str:
    if not rate or rate.get("rate") is None:
        return "n/a"
    return f"{100 * rate['rate']:.1f}% ({rate['numerator']}/{rate['denominator']})"


def render(receipt: dict[str, Any]) -> str:
    """A short text reading of the receipt."""
    lines: list[str] = []
    store = receipt.get("store") or {}
    ev = store.get("eval") or {}
    if ev:
        helped = ev["memory_helped_rate"]
        lines.append(
            f"helped rate {_pct(helped)}, 95% CI {helped.get('ci95_lower', 0):.3f} to "
            f"{helped.get('ci95_upper', 0):.3f}; endorsement {_pct(ev['endorsement_rate'])}; "
            f"silent miss {_pct(ev['silent_miss_rate'])}"
        )
        review = ev.get("review") or {}
        lines.append(
            f"write review: {review.get('calls_per_committed_write')} calls per committed "
            f"write, response p50 {review.get('response_chars_p50')} chars"
        )
        for name in ("brief", "recall"):
            surface = (ev.get("surfaces") or {}).get(name) or {}
            if surface:
                lines.append(
                    f"{name}: {surface.get('shown')} pointers shown, opened "
                    f"{_pct(surface.get('shown_opened_rate'))}, used "
                    f"{_pct(surface.get('shown_used_rate'))}, chars p50 {surface.get('chars_p50')}"
                )
    verdicts = store.get("verdicts") or {}
    if verdicts:
        flagged, fresh = verdicts["flagged"], verdicts["fresh"]
        lines.append(
            f"verdicts: {_pct(verdicts['flagged_share'])} of shows flagged; after a flagged "
            f"show, verified {_pct(flagged['verified_share'])}, updated "
            f"{_pct(flagged['updated_share'])}; after a fresh one, updated "
            f"{_pct(fresh['updated_share'])}; lift {verdicts['update_lift_flagged_over_fresh']}"
        )
    records = store.get("records") or {}
    if records:
        lines.append(f"records: {records['records']} active")
        for group in records["groups"]:
            lines.append(
                f"  {group['age']:>6} {'standing' if group['standing'] else 'other   '} "
                f"{group['records']:5d}  never retrieved {_pct(group['never_retrieved'])}, "
                f"never applied explicitly {_pct(group['never_applied_explicitly'])}"
            )
    tx = receipt.get("transcripts") or {}
    if tx:
        sessions = tx["sessions"]
        lines.append(
            f"transcripts: {sessions['with_bettermemory']} sessions; bettermemory's share of "
            f"tool and hook context {100 * (sessions['bettermemory_share'] or 0):.1f}%"
        )
        for tool, q in list(tx["tool_responses"].items())[:10]:
            lines.append(
                f"  {tool:22s} {q['n']:5d} calls, chars p50 {q['p50']}, p90 {q['p90']}, total {q['total']:.0f}"
            )
        for hook, block in tx["hooks"].items():
            chars, ms = block["injected_chars"], block["wall_ms"]
            lines.append(
                f"  hook {hook:28s} {chars['n']:4d} runs, injected p50 {chars['p50']}, wall p50 {ms['p50']} ms, p90 {ms['p90']} ms"
            )
        for tool, block in tx["writes"].items():
            lines.append(
                f"  {tool}: {block['calls_per_committed']} calls and {block['chars_per_committed']} chars per committed"
            )
    latency = receipt.get("latency") or {}
    for scale in SCALES:
        block = latency.get(scale)
        if not block:
            continue
        arms = block["arms"]
        picks = (
            "hook_prompt",
            "hook_session_start",
            "memory_search",
            "memory_write.first_call",
        )
        lines.append(
            f"latency {scale} ({block['file']}): "
            + ", ".join(f"{a} {arms[a]['p50_ms']} ms" for a in picks if a in arms)
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(os.path.expanduser("~/.bettermemory/memory.sqlite")),
        help="the store file to copy read-only (default: ~/.bettermemory/memory.sqlite)",
    )
    parser.add_argument(
        "--copy", type=Path, help="a store copy already made; no snapshot is taken"
    )
    parser.add_argument("--transcripts", type=Path, default=transcripts.default_root())
    parser.add_argument("--no-transcripts", action="store_true")
    parser.add_argument(
        "--since", help="window start, an ISO date or time (UTC when bare)"
    )
    parser.add_argument("--until", help="window end, exclusive")
    parser.add_argument(
        "--scale-results", type=Path, default=_ROOT / "bench" / "scale" / "results"
    )
    parser.add_argument("--out", type=Path, help="the receipt (aggregates only)")
    parser.add_argument(
        "--private-out",
        type=Path,
        help="a directory outside the repository for the per-record rows",
    )
    args = parser.parse_args(argv)
    since, until = _day(args.since), _day(args.until)
    now = datetime.now(timezone.utc)

    if args.private_out is not None and _inside(args.private_out, _ROOT):
        parser.error("--private-out names records; it must lie outside the repository")

    # A snapshot the program takes holds the whole store, so it is read
    # and deleted with its directory; a `--copy` is the caller's to keep.
    with tempfile.TemporaryDirectory(prefix="bettermemory-panel-") as scratch:
        if args.copy is not None:
            copy = args.copy
        else:
            copy = store_side.snapshot(args.store, Path(scratch) / "store.sqlite")
        conn = store_side.open_copy(copy)
        try:
            store, rows = store_side.read(conn, since=since, until=until, now=now)
        finally:
            conn.close()

    receipt: dict[str, Any] = {
        "kind": "instrument-panel",
        "run_at": now.isoformat(),
        "commit": _git("rev-parse", "HEAD"),
        "src_dirty": bool(_git("status", "--porcelain", "--", "src", "bench/panel")),
        "version": _git("describe", "--tags", "--always"),
        "program_sha256": program_sha256(),
        "machine": {"system": platform.system(), "python": platform.python_version()},
        "window": {"since": args.since, "until": args.until},
        "store": store,
        "transcripts": None
        if args.no_transcripts
        else transcripts.read(args.transcripts, since=since, until=until),
        "latency": latency_section(args.scale_results),
    }
    print(render(receipt))
    if args.out is not None:
        if args.out.exists():
            parser.error(f"{args.out} exists; a receipt is never rewritten")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.private_out is not None:
        args.private_out.mkdir(parents=True, exist_ok=True)
        (args.private_out / "records.json").write_text(
            store_side.dumps_rows(rows), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
