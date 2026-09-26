"""Recall-gate decisions on the store's own labelled hits, one instrument per run (private data; output stays local).

Instance: (user prompt, the assistant's search query, one returned memory). Label: 1 if the assistant
opened the memory with memory_show or recorded it applied; 0 if it recorded it ignored and never opened it.
Question: one noul, identical across instruments. Baseline: the lexical ranking (relevance label, then score).
Metrics: AUC, Brier, ECE, and precision/recall at the baseline's alert budget (the number of hits the
baseline labels "high"), plus precision at matched recall.
"""

from __future__ import annotations
import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import instruments

Q = {
    "helps": {
        "type": "noul",
        "instructions": "This stored memory contains something the assistant needs in order to answer the user's message well.",
        "criteria": {
            "true": "The memory is needed for a good answer to this message.",
            "false": "The memory is not needed for this message.",
        },
    }
}
REL = {"high": 3, "medium": 2, "low": 1, None: 0}


def state_for(r: dict, body_chars: int) -> str:
    body = (r.get("body") or r.get("snippet") or "")[:body_chars]
    return f"User message:\n{(r.get('prompt') or '')[:1500]}\n\nSearch query the assistant used:\n{r.get('query') or ''}\n\nStored memory (scopes: {', '.join(r.get('scopes') or [])}):\n{body}"


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def ece(pairs, bins=10):
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        cell = [
            (p, y) for p, y in pairs if (lo <= p < hi) or (b == bins - 1 and p == 1.0)
        ]
        if cell:
            tot += (
                len(cell)
                / len(pairs)
                * abs(
                    sum(y for _, y in cell) / len(cell)
                    - sum(p for p, _ in cell) / len(cell)
                )
            )
    return round(tot, 4)


def prec_rec_at_budget(scores, labels, budget):
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    top = order[:budget]
    tp = sum(labels[i] for i in top)
    P = sum(labels)
    return {
        "budget": budget,
        "precision": round(tp / budget, 4) if budget else None,
        "recall": round(tp / P, 4) if P else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument(
        "--instrument",
        required=True,
        choices=["kev", "semif", "laya", "chat", "jev", "export"],
    )
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--max-cost", type=float, default=1.0, help="Jev: stop after this many dollars"
    )
    ap.add_argument(
        "--estimate", action="store_true", help="print the projected Jev cost and exit"
    )
    ap.add_argument("--semif-exe")
    ap.add_argument("--semif-backend", default="mlx")
    ap.add_argument("--laya-max-len", type=int, default=None)
    ap.add_argument("--body-chars", type=int, default=1800)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sample", type=int, default=None)
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.data)]
    inst_rows = [
        r
        for r in rows
        if (r["opened"] or r["explicit"] == "applied")
        or ((not r["opened"]) and r["explicit"] == "ignored")
    ]
    for r in inst_rows:
        r["label"] = 1 if (r["opened"] or r["explicit"] == "applied") else 0
    inst_rows = [
        r for r in inst_rows if r.get("prompt") and (r.get("body") or r.get("snippet"))
    ]
    random.Random(a.seed).shuffle(inst_rows)
    if a.sample:
        inst_rows = inst_rows[: a.sample]
    if a.limit:
        inst_rows = inst_rows[: a.limit]
    labels = [r["label"] for r in inst_rows]
    base_scores = [
        REL[r.get("relevance")] * 10 + (r.get("score") or 0.0) for r in inst_rows
    ]
    budget = sum(1 for r in inst_rows if r.get("relevance") == "high")
    if a.instrument == "export":
        with open(a.out, "w") as fh:
            for i, r in enumerate(inst_rows):
                fh.write(
                    json.dumps(
                        {
                            "i": i,
                            "id": r["memory_id"],
                            "state": state_for(r, a.body_chars),
                            "label": r["label"],
                            "relevance": r.get("relevance"),
                            "rank": r.get("rank"),
                            "score": r.get("score"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(
            json.dumps(
                {
                    "exported": len(inst_rows),
                    "positives": sum(labels),
                    "budget_high": budget,
                    "baseline": {
                        "auc": auc(base_scores, labels),
                        "at_budget": prec_rec_at_budget(base_scores, labels, budget),
                    },
                }
            )
        )
        return 0
    if a.estimate:
        print(
            json.dumps(
                instruments.estimate_jev_cost(
                    [state_for(r, a.body_chars) for r in inst_rows],
                    [Q] * len(inst_rows),
                )
            )
        )
        return 0
    inst = instruments.make(a.instrument, a)
    t0 = time.time()
    probs = []
    dump = Path(a.out).with_suffix(".answers.json")
    if dump.exists():
        probs = json.load(open(dump))
        print(f"reusing {len(probs)} saved answers", file=sys.stderr)
    elif a.instrument == "semif":
        for i, r in enumerate(inst_rows):
            inst.queue(str(i), state_for(r, a.body_chars), Q)
        ans = inst.flush()
        probs = [ans[str(i)]["helps"]["noul"] for i in range(len(inst_rows))]
    else:
        for i, r in enumerate(inst_rows):
            probs.append(inst.ask(state_for(r, a.body_chars), Q)["helps"]["noul"])
            if i % 25 == 0:
                print(f"  {i + 1}/{len(inst_rows)}", file=sys.stderr)
    dump.write_text(json.dumps(probs))
    pairs = list(zip(probs, labels))
    res = {
        "instrument": inst.name,
        "version": inst.version,
        "n": len(inst_rows),
        "positives": sum(labels),
        "seconds": round(time.time() - t0, 1),
        "budget_high": budget,
        "baseline": {
            "auc": auc(base_scores, labels),
            "at_budget": prec_rec_at_budget(base_scores, labels, budget),
        },
        "arm": {
            "auc": auc(probs, labels),
            "brier": round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 4),
            "ece": ece(pairs),
            "at_budget": prec_rec_at_budget(probs, labels, budget),
            "at_tau": {
                str(t): {
                    "flagged": sum(p >= t for p in probs),
                    "precision": (
                        round(
                            sum(y for p, y in pairs if p >= t)
                            / max(1, sum(p >= t for p in probs)),
                            4,
                        )
                    ),
                    "recall": round(
                        sum(y for p, y in pairs if p >= t) / max(1, sum(labels)), 4
                    ),
                }
                for t in (0.5, 0.7, 0.9)
            },
        },
        "items": [
            {
                "id": r["memory_id"],
                "label": r["label"],
                "p": p,
                "relevance": r.get("relevance"),
                "rank": r.get("rank"),
                "score": r.get("score"),
                "transcript": r["transcript"],
            }
            for r, p in zip(inst_rows, probs)
        ],
        "question": Q,
        "usage": getattr(inst, "usage", None),
        "body_chars": a.body_chars,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(
        json.dumps(
            {k: v for k, v in res.items() if k not in ("items", "question")}, indent=1
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
