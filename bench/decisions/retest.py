"""Repeat-judgment check for the chat arm: the 200-case sample judged on
2026-09-25 against the same cases judged again in the full 771-case run.

Both judgments are blind, by the same model, on identical states. The
per-case files are private (they align to memory ids); only the
aggregates are written.

    python bench/decisions/retest.py --old OLD.answers.json --old-map OLD_blind_map.json \
        --new NEW.answers.json --data recall_hits.jsonl --out results/retest-recall-chat.json
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

REL = {"high": 3, "medium": 2, "low": 1, None: 0}


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return round(cov / (va * vb), 4) if va and vb else float("nan")


def auc(scores: list[float], labels: list[int]) -> float | None:
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def instance_rows(rows: list[dict], seed: int = 7) -> list[dict]:
    """The driver's own filter and order (decisions_recall.py)."""
    xs = [
        r
        for r in rows
        if (r["opened"] or r["explicit"] == "applied")
        or ((not r["opened"]) and r["explicit"] == "ignored")
    ]
    for r in xs:
        r["label"] = 1 if (r["opened"] or r["explicit"] == "applied") else 0
    xs = [r for r in xs if r.get("prompt") and (r.get("body") or r.get("snippet"))]
    random.Random(seed).shuffle(xs)
    return xs


def compare(old: list[float], new_at: list[float], labels: list[int]) -> dict:
    n = len(old)
    agree = sum((a >= 0.5) == (b >= 0.5) for a, b in zip(old, new_at))
    diffs = [abs(a - b) for a, b in zip(old, new_at)]
    return {
        "n": n,
        "pearson": pearson(old, new_at),
        "spearman": pearson(ranks(old), ranks(new_at)),
        "same_side_of_0.5": round(agree / n, 4),
        "mean_abs_diff": round(sum(diffs) / n, 4),
        "within_0.2": round(sum(d <= 0.2 for d in diffs) / n, 4),
        "auc_old": auc(old, labels),
        "auc_new_same_cases": auc(new_at, labels),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--old", required=True, help="the 200-case answers list, sample order"
    )
    ap.add_argument(
        "--old-map",
        required=True,
        help="opaque id -> sample index, from the 2026-09-25 run",
    )
    ap.add_argument(
        "--new", required=True, help="the 771-case answers list, export order"
    )
    ap.add_argument(
        "--data", required=True, help="recall_hits.jsonl the runs were built from"
    )
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    old = json.load(open(a.old))
    new = json.load(open(a.new))
    rows = instance_rows([json.loads(line) for line in open(a.data)])
    if len(new) != len(rows):
        raise SystemExit(
            f"new answers {len(new)} do not match the {len(rows)} instance rows"
        )
    # the sample was the first --sample rows of the same shuffled order
    sample = rows[: a.sample]
    if len(old) != len(sample):
        raise SystemExit(
            f"old answers {len(old)} do not match the {len(sample)}-row sample"
        )
    labels = [r["label"] for r in sample]
    new_at = new[: a.sample]
    out = {
        "what": "the chat arm's 2026-09-25 sample judgments against its full-run judgments on the same cases",
        "instrument": "claude-fable-5.1-in-session",
        **compare(old, new_at, labels),
    }
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
