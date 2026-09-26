"""Copy results into the repo's bench/decisions/results with private rows removed.

Integrity results are public (the corpus is in the repo). Recall results carry the owner's memory ids,
labels and transcript names per row: those rows are dropped; the aggregates and the run provenance stay.
"""

import json
import shutil
import sys
from pathlib import Path

src = Path(sys.argv[1]) / "results"
dst = Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
for f in sorted(src.glob("*.json")):
    if f.name.endswith(".answers.json") or f.name.endswith(".blind.json"):
        continue
    if f.name.startswith("recall-"):
        d = json.load(open(f))
        items = d.get("items") or []
        if items and "operating_points" not in d:
            REL = {"high": 3, "medium": 2, "low": 1, None: 0}
            labels = [i["label"] for i in items]
            probs = [i["p"] for i in items]
            base = [REL[i["relevance"]] * 10 + (i["score"] or 0) for i in items]
            P = sum(labels)

            def at(scores, budget):
                order = sorted(range(len(scores)), key=lambda k: -scores[k])[:budget]
                tp = sum(labels[k] for k in order)
                return {
                    "budget": budget,
                    "precision": round(tp / budget, 4) if budget else None,
                    "recall": round(tp / P, 4) if P else None,
                }

            b_high = sum(1 for i in items if i["relevance"] == "high")
            b_hm = sum(1 for i in items if i["relevance"] in ("high", "medium"))
            d["operating_points"] = {
                "baseline_at_high": at(base, b_high),
                "arm_at_high_budget": at(probs, b_high),
                "baseline_at_high_medium": at(base, b_hm),
                "arm_at_high_medium_budget": at(probs, b_hm),
            }
        n_items = len(d.pop("items", []))
        d["items_omitted"] = n_items
        d["items_note"] = (
            "per-memory rows omitted: they name memories and sessions from the owner's private store"
        )
        (dst / f.name).write_text(json.dumps(d, indent=1))
        print("stripped", f.name, n_items, "rows")
    else:
        shutil.copy(f, dst / f.name)
        print("copied", f.name)
