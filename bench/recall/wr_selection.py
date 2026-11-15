"""Unit WR, measurement 2: a model-free selection of the standing roster.

Replays, from a read-only snapshot of a store (`bench/panel/store_side.
snapshot`), every review chain that showed a standing roster and was
answered with `named`: write chains (the `write` row's `review_answers`)
and update chains (an update's standing roster, `door` set on its rows).
For each it compares the roster the chain listed with what a model-free
selection would have listed:

  scope   roster records sharing a scope with the write's scopes;
  token   roster records any sentence of the write (`review._write_sentences`)
          shares at least one content token with (`search.raw_content_tokens`),
          every match kept, no top-k;
  union   scope or token, the design's proposal.

The write's text is the committed record's body in the snapshot (an
update's: the updated record's body); a record edited after the chain is
counted as a caveat. The roster records' bodies and scopes are read from
the snapshot as they stand now. Aggregates only go to `--out`, which must
lie outside the repository: no id, no text, no scope name.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

REPO = Path(__file__).resolve().parents[2]
#: The roster line's frame: ``<26-char id> (<date>) `` before the text.
LINE_FRAME = 26 + 2 + 10 + 2
ROSTER_TEXT_CHARS = 120
SLACK = timedelta(seconds=5)
TOKEN_K = (2, 3, 4, 5, 6, 8)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def line_chars(body: str) -> int:
    return LINE_FRAME + min(ROSTER_TEXT_CHARS, len(" ".join(body.split())))


def select(
    content: str,
    scopes: set[str],
    records: dict[str, tuple[str, set[str]]],
) -> dict[str, set[str]]:
    """The scope, token and union selections over `records` (id -> body, scopes)."""
    from bettermemory.review import _write_sentences
    from bettermemory.search import raw_content_tokens

    sentence_tokens = [raw_content_tokens(s) for s in _write_sentences(content)]
    scope_sel, token_sel = set(), set()
    best: dict[str, int] = {}
    for rid, (body, rscopes) in records.items():
        if scopes & rscopes:
            scope_sel.add(rid)
        btoks = raw_content_tokens(body)
        best[rid] = max((len(t & btoks) for t in sentence_tokens), default=0)
        if best[rid]:
            token_sel.add(rid)
    out = {"scope": scope_sel, "token": token_sel, "union": scope_sel | token_sel}
    # Narrower token selections: a sentence sharing at least k tokens.
    for k in TOKEN_K:
        out[f"token>={k}"] = {r for r, n in best.items() if n >= k}
    return out


def _q(values: list[int], q: float) -> int:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def chains(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every answered roster chain: kind, root ts, roster ids, named ids, record id."""
    issued = [e for e in events if e.get("kind") == "review_issued"]
    by_id = {e["review_id"]: e for e in issued if isinstance(e.get("review_id"), str)}
    out = []
    for e in events:
        if e.get("kind") not in ("write", "update"):
            continue
        answers = e.get("review_answers")
        rid = e.get("review_id")
        if (
            not isinstance(answers, dict)
            or "named" not in answers
            or not isinstance(rid, str)
        ):
            continue
        row = by_id.get(rid)
        root = (row or {}).get("parent_review_id") or rid
        rows = [
            x for x in issued if root in (x.get("review_id"), x.get("parent_review_id"))
        ]
        roster = {i for x in rows for i in (x.get("roster") or [])}
        if not roster:
            continue
        out.append(
            {
                "kind": e["kind"],
                "ts": e["ts"],
                "record": e.get("id"),
                "scopes": e.get("scopes"),
                "roster": roster,
                "named": set(answers["named"] or []),
            }
        )
    return out


def run(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> dict[str, Any]:
    mem = {
        r[0]: (r[1], set(json.loads(r[2])), r[3])
        for r in conn.execute("SELECT id, body, scopes_json, updated FROM memories")
    }
    report: dict[str, Any] = {}
    for kind in ("write", "update"):
        rows = [c for c in chains(events) if c["kind"] == kind]
        stats: dict[str, Any] = {"chains": len(rows)}
        unresolved = Counter()
        sizes: dict[str, list[int]] = {
            k: []
            for k in (
                "roster",
                "scope",
                "token",
                "union",
                *(f"token>={k}" for k in TOKEN_K),
            )
        }
        chars: dict[str, list[int]] = {k: [] for k in sizes}
        named_total, kept = 0, Counter()
        misses = Counter()
        caveats = Counter()
        fixture_like = 0
        for c in rows:
            own = mem.get(c["record"])
            if own is None:
                unresolved["write_record_gone"] += 1
                continue
            content, own_scopes, updated = own
            if _ts(updated) > _ts(c["ts"]) + SLACK:
                caveats["write_record_edited_since"] += 1
            scopes = set(c["scopes"]) if isinstance(c["scopes"], list) else own_scopes
            records = {}
            for rid in c["roster"]:
                if rid not in mem:
                    caveats["roster_record_gone"] += 1
                    continue
                body, rscopes, rupd = mem[rid]
                if _ts(rupd) > _ts(c["ts"]) + SLACK:
                    caveats["roster_record_edited_since"] += 1
                records[rid] = (body, rscopes)
            sel = select(content, scopes, records)
            sizes["roster"].append(len(c["roster"]))
            chars["roster"].append(sum(line_chars(b) for b, _ in records.values()))
            for k in sel:
                sizes[k].append(len(sel[k]))
                chars[k].append(sum(line_chars(records[i][0]) for i in sel[k]))
            for n in c["named"]:
                if n not in records:
                    caveats["named_record_gone"] += 1
                    continue
                named_total += 1
                for k in sel:
                    kept[k] += n in sel[k]
                if n not in sel["union"]:
                    misses["no shared token and no shared scope"] += 1
                elif n not in sel["token"]:
                    misses[
                        "token-only would miss: no shared token (held by scope)"
                    ] += 1
                if n not in sel["scope"] and n in sel["token"]:
                    misses["scope-only would miss: another scope (held by token)"] += 1
            fixture_like += 1
        stats["reconstructed"] = fixture_like
        stats["unresolved"] = dict(unresolved)
        stats["caveats"] = dict(caveats)
        stats["chains_naming_any"] = sum(1 for c in rows if c["named"])
        stats["named"] = named_total
        stats["recall"] = {
            k: round(kept[k] / named_total, 3) if named_total else None for k in kept
        }
        stats["misses"] = dict(misses)
        if sizes["roster"]:
            stats["lines"] = {
                k: {"median": median(v), "p90": _q(v, 0.9), "max": max(v)}
                for k, v in sizes.items()
            }
            stats["chars"] = {
                k: {"median": median(v), "p90": _q(v, 0.9), "max": max(v)}
                for k, v in chars.items()
            }
            stats["union_share_of_roster_lines"] = round(
                sum(sizes["union"]) / max(1, sum(sizes["roster"])), 3
            )
        report[kind] = stats
    return report


def fixture_case() -> dict[str, bool]:
    """Whether the selection holds the replayed implicit-break case of
    tests/test_write_roster.py (its ruling, and not its other ruling, by token)."""
    import sys

    sys.path.insert(0, str(REPO))
    from tests.test_write_roster import OTHER_RULING, PLAN, RULING

    sel = select(
        PLAN, set(), {"ruling": (RULING, set()), "other": (OTHER_RULING, set())}
    )
    return {
        "ruling_held_by_token": "ruling" in sel["token"],
        "other_ruling_held_by_token": "other" in sel["token"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    if out.is_relative_to(REPO):
        raise SystemExit("--out must lie outside the repository")
    from panel.store_side import events, open_copy

    conn = open_copy(args.snapshot)
    try:
        result = run(conn, events(conn))
    finally:
        conn.close()
    result["fixture"] = fixture_case()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
