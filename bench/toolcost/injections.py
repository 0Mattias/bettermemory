"""Characters of the two hook injections, re-rendered from a store's log.

The session brief and the prompt recall each record what they delivered:
`session_brief` names its standing and work ids, `prompt_recall` its
shown ids and the probe's top hits (relevance and query snippet). This
script re-renders every recorded delivery with the checkout's own
renderers (`recall_brief.render_brief`, `recall.render_recall_block`)
over the same records, so two checkouts measured on one snapshot compare
on the same inputs. The marks are the snapshot's: a disputed mark for a
record in a pending conflict pair, and, on a checkout whose brief names
in-force records, an extended-by note naming the newest active record
that extends it (`in_force`'s admission and held checks not repeated).

It reads a copy, never a live store: pass a snapshot taken with
`bench/panel/store_side.snapshot`. Output is aggregates only.

Usage:

    .venv/bin/python bench/toolcost/injections.py SNAPSHOT [--since DATE] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PANEL = Path(__file__).resolve().parents[1] / "panel"


def _quantiles(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": statistics.median(ordered),
        "p90": ordered[int(0.9 * (len(ordered) - 1))],
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 1),
    }


def measure(snapshot: Path, since: str = "", each: bool = False) -> dict[str, Any]:
    from bettermemory import recall
    from bettermemory.recall import RecallPointer, render_recall_block
    from bettermemory.recall_admission import disputed_marks, summarize

    # A checkout that names in-force records on a pointer line has this
    # helper; one that leaves them to memory_show has not.
    in_force_marks = getattr(recall, "_in_force_marks", lambda *_: [])
    from bettermemory.recall_brief import render_brief

    sys.path.insert(0, str(_PANEL))
    import store_side

    conn = store_side.open_copy(snapshot)
    try:
        events = [
            ev for ev in store_side.events(conn) if str(ev.get("ts") or "") >= since
        ]
        bodies = dict(conn.execute("SELECT id, body FROM memories"))
        pairs = list(
            conn.execute(
                "SELECT a_id, b_id, id FROM conflicts WHERE status = 'pending'"
            )
        )
        extended_by: dict[str, str] = {}
        for target, source in conn.execute(
            "SELECT l.target_id, l.source_id FROM memory_links l "
            "JOIN memories m ON m.id = l.source_id "
            "WHERE l.type = 'extends' ORDER BY m.updated, m.id"
        ):
            extended_by[target] = source
    finally:
        conn.close()

    disputed = disputed_marks(pairs)
    # A checkout whose recall line prints the probe's coverage label has
    # the field; one that leaves it out has not.
    with_relevance = "relevance" in RecallPointer.__dataclass_fields__

    def relevance(hit: dict[str, Any]) -> dict[str, str]:
        if not with_relevance:
            return {}
        return {"relevance": str(hit.get("relevance") or "medium")}

    def marks(ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for memory_id in ids:
            parts = [disputed.get(memory_id, "")]
            parts.extend(in_force_marks(None, extended_by.get(memory_id)))
            text = " ".join(part for part in parts if part)
            if text:
                out[memory_id] = text
        return out

    def memory(memory_id: str) -> Any:
        # The renderers read a record's id and body alone; a removed
        # record has no body and its delivery is skipped.
        body = bodies.get(memory_id)
        return None if body is None else SimpleNamespace(id=memory_id, body=body)

    brief_chars: list[int] = []
    brief_recorded: list[int] = []
    brief_same_ids = 0
    for ev in events:
        if ev.get("kind") != "session_brief" or not ev.get("ids"):
            continue
        if "standing" not in ev and "work" not in ev:
            # A brief recorded before the two lists were named.
            continue
        standing = [memory(i) for i in ev.get("standing") or []]
        work = [memory(i) for i in ev.get("work") or []]
        if any(m is None for m in [*standing, *work]):
            continue
        text, ids = render_brief(
            [m for m in standing if m],
            [m for m in work if m],
            marks([*(ev.get("standing") or []), *(ev.get("work") or [])]),
        )
        brief_chars.append(len(text))
        brief_recorded.append(int(ev.get("chars") or 0))
        brief_same_ids += ids == list(ev["ids"])

    recall_chars: list[int] = []
    recall_recorded: list[int] = []
    recall_same_ids = 0
    for ev in events:
        if ev.get("kind") != "prompt_recall" or not ev.get("shown_ids"):
            continue
        hits = {h.get("id"): h for h in ev.get("top_hits") or []}
        pointers = []
        for memory_id in ev["shown_ids"]:
            record = memory(memory_id)
            if record is None:
                break
            hit = hits.get(memory_id, {})
            pointers.append(
                RecallPointer(
                    id=memory_id,
                    **relevance(hit),
                    summary=summarize(record.body),
                    snippet=str(hit.get("snippet") or ""),
                    disputed=disputed.get(memory_id),
                )
            )
        else:
            text, ids = render_recall_block(pointers)
            recall_chars.append(len(text))
            recall_recorded.append(int(ev.get("injected_chars") or 0))
            recall_same_ids += ids == list(ev["shown_ids"])

    lengths = (
        {
            "brief_each": [brief_chars, brief_recorded],
            "recall_each": [recall_chars, recall_recorded],
        }
        if each
        else {}
    )
    return {
        **lengths,
        "unit": "characters",
        "since": since or None,
        "brief": {
            "rendered": _quantiles(brief_chars),
            "recorded_at_delivery": _quantiles(brief_recorded),
            "same_ids": brief_same_ids,
        },
        "recall": {
            "rendered": _quantiles(recall_chars),
            "recorded_at_delivery": _quantiles(recall_recorded),
            "same_ids": recall_same_ids,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--since", default="", help="ISO date; older rows skipped")
    parser.add_argument(
        "--each",
        action="store_true",
        help="also list each delivery's rendered and recorded length, in log order",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.snapshot.exists():
        raise SystemExit(f"no snapshot at {args.snapshot}")
    try:
        result = measure(args.snapshot, args.since, args.each)
    except sqlite3.Error as exc:
        raise SystemExit(f"snapshot unreadable: {exc}") from exc
    print(json.dumps(result, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
