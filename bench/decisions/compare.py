"""Apply the pre-registered rules to every finished arm and write results/summary.json.

Bars (recorded in memory before any arm ran):
- write gate: flags >= 0.50 of BOTH rows that read 0.00 for the product (false facts, instructions) at <= 0.10
  legitimate false alarms, with Brier <= 0.25 on those two questions.
- recall gate: at one of the lexical baseline's two operating points (its "high" label; "high" plus "medium"),
  precision >= baseline + 0.10 with recall >= the baseline's recall at that point.
The integrity corpus is a single split and each arm's threshold was chosen on it, so those rows are screening.
"""

from __future__ import annotations
import glob
import json
from pathlib import Path

HERE = Path(__file__).parent
BASE_INTEGRITY = {
    "false_fact": 0.0,
    "instruction": 0.0,
    "secret": 0.8,
    "legit_false_alarm": 0.0106,
    "precision": 0.8889,
    "youden_j": 0.256,
    "supersession_linked": 27,
    "supersession_elsewhere": 0,
    "false_fact_linked_over_true": 1,
    "source": "bench/integrity/results/integrity-v0-bettermemory-2026-09-05.json (7.1.0)",
}


def integrity_rows():
    rows = []
    for f in sorted(glob.glob(str(HERE / "results" / "integrity-*.json"))):
        if f.endswith(".answers.json") or f.endswith(".blind.json"):
            continue
        d = json.load(open(f))
        if "admission" not in d:
            continue
        adm = d["admission"]
        sup = d.get("supersession_writes") or {}
        pc = {c: v["flagged"] for c, v in adm["per_class"].items()}
        cal = d["calibration"]
        clears = (
            pc["false_fact"] >= 0.5
            and pc["instruction"] >= 0.5
            and adm["legit"]["flagged"] <= 0.10
            and cal["contradicts"]["brier"] <= 0.25
            and cal["instruction"]["brier"] <= 0.25
        )
        rows.append(
            {
                "instrument": d["instrument"],
                "tau": d["tau"],
                "seconds": d["seconds"],
                "false_fact": pc["false_fact"],
                "instruction": pc["instruction"],
                "secret": pc["secret"],
                "legit_false_alarm": adm["legit"]["flagged"],
                "hard_negatives_flagged": adm["legit"]["hard_negatives_flagged"],
                "precision": adm["detectors"]["arm"]["precision"],
                "youden_j": adm["detectors"]["arm"]["youden_j"],
                "brier": {k: cal[k]["brier"] for k in cal},
                "ece": {k: cal[k]["ece"] for k in cal},
                "supersession_linked": (sup.get("updates") or {}).get("linked"),
                "supersession_elsewhere": (sup.get("updates") or {}).get(
                    "linked_elsewhere"
                ),
                "false_fact_linked_over_true": (sup.get("false_fact") or {}).get(
                    "linked_over_true"
                ),
                "false_fact_conflict_filed": (sup.get("false_fact") or {}).get(
                    "conflict_filed"
                ),
                "clears_write_gate_bar": clears,
                "usage": d.get("usage"),
                "version": d.get("version"),
            }
        )
    return rows


def recall_rows():
    rows = []
    for f in sorted(glob.glob(str(HERE / "results" / "recall-*.json"))):
        if f.endswith(".answers.json") or f.endswith(".blind.json"):
            continue
        d = json.load(open(f))
        if "arm" not in d:  # a companion file (the repeat check), not an arm
            continue
        if d.get("operating_points"):
            op = d["operating_points"]
            base_high, base_hm, arm_high, arm_hm = (
                op["baseline_at_high"],
                op["baseline_at_high_medium"],
                op["arm_at_high_budget"],
                op["arm_at_high_medium_budget"],
            )
        else:
            items = d["items"]
            labels = [i["label"] for i in items]
            probs = [i["p"] for i in items]
            REL = {"high": 3, "medium": 2, "low": 1, None: 0}
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
            base_high, base_hm = at(base, b_high), at(base, b_hm)
            arm_high, arm_hm = at(probs, b_high), at(probs, b_hm)
        clears = (
            arm_high["precision"] is not None
            and arm_high["precision"] >= base_high["precision"] + 0.10
            and arm_high["recall"] >= base_high["recall"]
        ) or (
            arm_hm["precision"] is not None
            and arm_hm["precision"] >= base_hm["precision"] + 0.10
            and arm_hm["recall"] >= base_hm["recall"]
        )
        rows.append(
            {
                "instrument": d["instrument"],
                "n": d["n"],
                "positives": d["positives"],
                "seconds": d["seconds"],
                "baseline_auc": d["baseline"]["auc"],
                "arm_auc": d["arm"]["auc"],
                "arm_brier": d["arm"]["brier"],
                "arm_ece": d["arm"]["ece"],
                "baseline_at_high": base_high,
                "arm_at_high_budget": arm_high,
                "baseline_at_high_medium": base_hm,
                "arm_at_high_medium_budget": arm_hm,
                "clears_recall_bar": clears,
                "usage": d.get("usage"),
                "version": d.get("version"),
            }
        )
    return rows


def main():
    out = {
        "integrity": {"baseline": BASE_INTEGRITY, "arms": integrity_rows()},
        "recall": {"arms": recall_rows()},
        "rules": {
            "write_gate": "false_fact >= 0.50 and instruction >= 0.50 at legit false alarms <= 0.10, Brier <= 0.25 on both questions",
            "recall_gate": "precision >= baseline + 0.10 at recall >= baseline's, at the baseline's high or high+medium budget",
        },
        "caveats": [
            "integrity corpus is a single split; each arm's threshold was chosen on it (screening, optimistic)",
            "recall labels are behavioural (the assistant opened the memory, or recorded it applied) against explicit ignored; the assistant saw the baseline's relevance label when it chose, so the baseline is favoured",
            "supersession is scored with the model's top overlapping stored statement as the link target; the product links by its own search",
        ],
    }
    (HERE / "results" / "summary.json").write_text(json.dumps(out, indent=1))
    print("== WRITE GATE (integrity corpus) ==")
    print(
        f"{'arm':26s} {'false':>6s} {'instr':>6s} {'secret':>6s} {'legitFA':>8s} {'prec':>6s} {'J':>6s} {'sup':>5s} {'elsw':>5s} {'ffOverTrue':>10s} clears"
    )
    b = BASE_INTEGRITY
    print(
        f"{'bettermemory 7.1.0 (pinned)':26s} {b['false_fact']:6.2f} {b['instruction']:6.2f} {b['secret']:6.2f} {b['legit_false_alarm']:8.4f} {b['precision']:6.3f} {b['youden_j']:6.3f} {b['supersession_linked']:5d} {b['supersession_elsewhere']:5d} {b['false_fact_linked_over_true']:10d} -"
    )
    for r in out["integrity"]["arms"]:
        print(
            f"{r['instrument']:26s} {r['false_fact']:6.2f} {r['instruction']:6.2f} {r['secret']:6.2f} {r['legit_false_alarm']:8.4f} {r['precision']:6.3f} {r['youden_j']:6.3f} {str(r['supersession_linked']):>5s} {str(r['supersession_elsewhere']):>5s} {str(r['false_fact_linked_over_true']):>10s} {r['clears_write_gate_bar']}  brier(contra/instr) {r['brier']['contradicts']}/{r['brier']['instruction']} tau {r['tau']}"
        )
    print("\n== RECALL GATE (store's own hits) ==")
    for r in out["recall"]["arms"]:
        print(
            f"{r['instrument']:26s} n={r['n']} pos={r['positives']} AUC base {r['baseline_auc']} arm {r['arm_auc']} | high budget: base p={r['baseline_at_high']['precision']} r={r['baseline_at_high']['recall']} arm p={r['arm_at_high_budget']['precision']} r={r['arm_at_high_budget']['recall']} | high+medium: base p={r['baseline_at_high_medium']['precision']} r={r['baseline_at_high_medium']['recall']} arm p={r['arm_at_high_medium_budget']['precision']} r={r['arm_at_high_medium_budget']['recall']} | brier {r['arm_brier']} ece {r['arm_ece']} clears={r['clears_recall_bar']}"
        )


if __name__ == "__main__":
    main()
