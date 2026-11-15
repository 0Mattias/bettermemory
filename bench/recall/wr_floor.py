"""Unit WR, measurement 1: a lexical floor on the review's related set.

Reads the graded (write, record) pairs of ENG-115 stage 1 (its private
receipts: `candidates.json` holds each write's text, the engine's top-k
ids for k = 1..10, the cue targets and the record bodies as they stood at
grading; `judged.json` holds the judges' answers) and, for each pair,
counts the distinct content tokens the write and the record share, with
the review's own tokenizer (`search.raw_content_tokens`, the one
`review.related_entry` windows on). It then reports, for floors F >= 1,
recall of each related label, the share of unrelated pairs kept, and the
share of writes left with no pair at or above F (the one-call share), and
the same for the engine's rank as a cut.

Aggregates only are written to `--out`, which must lie outside the
repository: no id, no text.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RELATED_LABELS = ("update", "conflict", "restates", "extends")


def pair_label(final: dict[str, Any], passes: dict[str, Any], tag: str) -> str | None:
    """The label of the judge whose side decided the pair, or None ungraded."""
    order = {
        "agreed": "record",
        "record_only": "record",
        "second_only": "second",
        "settled": "settle",
    }
    who = order.get(final.get("by", ""))
    if who is None:
        return None
    for judge, p in passes.items():
        if p.get("_role") == who:
            return p["answers"].get(tag)
    return None


def floor_table(
    pairs: list[dict[str, Any]], writes: int, key: str, top: int
) -> list[dict]:
    """Per floor F: recall per related label, unrelated kept, one-call share."""
    by_label: dict[str, list[int]] = defaultdict(list)
    per_write: dict[int, int] = defaultdict(int)
    for p in pairs:
        by_label[p["label"]].append(p[key])
        per_write[p["write"]] = max(per_write[p["write"]], p[key])
    rows = []
    for f in range(1, top + 1):
        row: dict[str, Any] = {"F": f}
        for lab, vals in sorted(by_label.items()):
            row[lab] = round(sum(v >= f for v in vals) / len(vals), 3)
        row["one_call"] = round(
            sum(1 for w in range(writes) if per_write.get(w, 0) < f) / writes, 3
        )
        rows.append(row)
    return rows


def best_floor(rows: list[dict]) -> dict | None:
    """The largest F keeping every update, conflict and restates pair and
    at least 0.95 of extends."""
    ok = [
        r
        for r in rows
        if all(r.get(k, 1.0) == 1.0 for k in ("update", "conflict", "restates"))
        and r.get("extends", 1.0) >= 0.95
    ]
    return ok[-1] if ok else None


def distribution(pairs: list[dict[str, Any]], key: str) -> dict[str, dict]:
    out = {}
    groups: dict[str, list[int]] = defaultdict(list)
    for p in pairs:
        groups[p["label"]].append(p[key])
    for lab, vals in sorted(groups.items()):
        vals.sort()
        out[lab] = {
            "n": len(vals),
            "min": vals[0],
            "p10": vals[int(0.1 * (len(vals) - 1))],
            "median": median(vals),
            "p90": vals[int(0.9 * (len(vals) - 1))],
            "max": vals[-1],
            "hist": dict(sorted(Counter(min(v, 20) for v in vals).items())),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--receipts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    if out.is_relative_to(REPO):
        raise SystemExit("--out must lie outside the repository")
    from bettermemory.search import raw_content_tokens

    cand = json.loads((args.receipts / "candidates.json").read_text())["readings"]
    judged = json.loads((args.receipts / "judged.json").read_text())
    roles = {v: k for k, v in judged["judges"].items()}
    pairs: list[dict[str, Any]] = []
    missing_text = 0
    write_tokens: list[int] = []
    sides = Counter()
    for w, (record, row) in enumerate(sorted(judged["results"].items())):
        reading = cand[record]
        passes = {j: dict(p, _role=roles[j]) for j, p in row["passes"].items()}
        wt = raw_content_tokens(reading["content"])
        write_tokens.append(len(wt))
        rank = {}
        for k in sorted(reading["engine"], key=int):
            for i in reading["engine"][k]:
                rank.setdefault(i, int(k))
        cue = set(reading["cue"])
        for mid, final in row["final"].items():
            body = reading["bodies"].get(mid)
            if body is None:
                missing_text += 1
                continue
            if final["related"] is None:
                continue
            lab = pair_label(final, passes, row["labels"][mid])
            label = lab if final["related"] and lab in RELATED_LABELS else "unrelated"
            if final["related"] and lab not in RELATED_LABELS:
                label = "related_unlabelled"
            sides[(final["related"], label)] += 1
            pairs.append(
                {
                    "write": w,
                    "label": label,
                    "overlap": len(wt & raw_content_tokens(body)),
                    # the share of the write's content tokens the record holds, in percent
                    "coverage": round(
                        100 * len(wt & raw_content_tokens(body)) / max(1, len(wt))
                    ),
                    # rank as a score: 11 - rank, so a higher floor is a tighter cut
                    "rank_score": 11 - rank.get(mid, 11),
                    "cue": mid in cue,
                }
            )
    writes = len(judged["results"])
    rows = floor_table(pairs, writes, "overlap", 30)
    rank_rows = floor_table(pairs, writes, "rank_score", 10)
    cov_rows = floor_table(pairs, writes, "coverage", 100)
    per_write_max = sorted(
        max(p["overlap"] for p in pairs if p["write"] == w) for w in range(writes)
    )
    cue_pairs = Counter((p["label"], p["cue"]) for p in pairs)
    result = {
        "tokenizer": "bettermemory.search.raw_content_tokens (write content vs record body)",
        "writes": writes,
        "pairs": len(pairs),
        "pairs_without_text": missing_text,
        "labels": {f"{k[0]}:{k[1]}": v for k, v in sorted(sides.items(), key=str)},
        "overlap_distribution": distribution(pairs, "overlap"),
        "overlap_floors": rows,
        "overlap_best_floor": best_floor(rows),
        "write_content_tokens": {
            "median": median(write_tokens),
            "min": min(write_tokens),
            "max": max(write_tokens),
        },
        "per_write_max_overlap": {
            "min": per_write_max[0],
            "p10": per_write_max[int(0.1 * (writes - 1))],
            "median": median(per_write_max),
        },
        "coverage_distribution": distribution(pairs, "coverage"),
        "coverage_floors": cov_rows,
        "coverage_best_floor": best_floor(cov_rows),
        "rank_floors": [dict(r, rank_at_most=11 - r["F"]) for r in rank_rows],
        "rank_best_floor": best_floor(rank_rows),
        "cue_pairs": {
            f"{k[0]}:{k[1]}": v for k, v in sorted(cue_pairs.items(), key=str)
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("writes", "pairs", "overlap_best_floor")}))


if __name__ == "__main__":
    main()
