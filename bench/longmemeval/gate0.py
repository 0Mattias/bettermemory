"""Score Gate 0 of PREREGISTRATION.md addendum 4, round 2's pre-run kill.

Addendum 4 declares two offline checks that run **before** any gated
recall number is produced, on the argument this directory already
learned once: *"the oracle ceiling is cheap to compute and would have
closed this item in an afternoon instead of a phase"*. Both are pure
functions of committed artifacts — the df census plus the two
per-question sidecars — so the verdict is reproducible from the
repository rather than from a re-run.

- **Gate 0a (separability)** — the median df/N of emitted live terms on
  the regressed held-out questions must be at least 5x the median on
  the dev set's leg-engaging asked probes. The reading of "the dev
  set's rescued questions" is not allowed to be chosen after the fact,
  so all four readings are computed and the verdict is the worst case.
- **Gate 0b (reachability)** — at tau the gate must alter the emitted
  term set on at least 20 of the 25 regressed questions, and must
  change `a89d7624`'s set.

Failing either publishes "df does not identify the promiscuous class"
as the result and ENDS the experiment: no gate is implemented, no
gated arm runs, `rescue_expansion` stays opt-in.

    .venv/bin/python bench/longmemeval/gate0.py \\
        --out results/gate0-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_RESULTS = _HERE / "results"
_PQ = _RESULTS / "per-question"

# Fixed by addendum 4 before the census existed. Inherited verbatim from
# the round-2 draft written during the 2026-08-10 audit sweep.
TAU = 0.05
SEPARABILITY_MULTIPLE = 5.0
REACHABILITY_MIN_QUESTIONS = 20
CLEANEST_PROOF_QID = "a89d7624"

# The artifacts the verdict is computed from, pinned by name so a
# reader can check the verdict against the same bytes.
CENSUS = _RESULTS / "df-census-2026-08-10.json"
BASELINE_PQ = _PQ / "baseline-reproduced-2026-08-09.json"
LANE_PQ = _PQ / "rebaseline-lane-2026-08-10.json"


def recall_at(record: dict[str, Any], k: int) -> float:
    """One question's recall@k from its evidence ranks."""
    n = record["n_evidence"]
    if not n:
        return 0.0
    hit = [r for r in record["evidence_ranks"] if r is not None and r < k]
    return len(hit) / n


def regressed_qids(baseline: dict[str, Any], lane: dict[str, Any]) -> list[str]:
    """Questions the ungated lane moved DOWN at k=5 — the population the
    gate exists to repair."""
    return sorted(
        q for q in baseline if recall_at(lane[q], 5) < recall_at(baseline[q], 5)
    )


def live_ratios(records: dict[str, Any], qids: list[str]) -> list[float]:
    """df/N of every emitted term with a non-zero df, pooled over `qids`.

    Zero-df terms are excluded deliberately: `morph_variants` is a rule
    and emits non-words that match nothing, and letting them pile into
    the lowest band would drag any median down and make the gate look
    better than it is.
    """
    return [t["df_ratio"] for q in qids for t in records[q]["terms"] if t["df"] > 0]


def dev_readings(dev_records: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Every defensible reading of "the dev set's rescued questions".

    Reported together because picking one after seeing the answer is
    exactly the move a pre-registration exists to prevent.
    """

    def pool(rows: list[dict[str, Any]]) -> list[float]:
        return [t["df_ratio"] for r in rows for t in r["terms"] if t["df"] > 0]

    return {
        "all probes": pool(dev_records),
        "asked probe only": pool([r for r in dev_records if r["probe"] == "asked"]),
        "asked probe, leg engaged": pool(
            [r for r in dev_records if r["probe"] == "asked" and r["engaged"]]
        ),
        "asked+control, leg engaged": pool([r for r in dev_records if r["engaged"]]),
    }


def score(
    held_out: dict[str, Any],
    dev_records: list[dict[str, Any]],
    regressed: list[str],
    *,
    tau: float = TAU,
) -> dict[str, Any]:
    """Both gates, as addendum 4 defines them."""
    reg_ratios = live_ratios(held_out, regressed)
    reg_median = statistics.median(reg_ratios) if reg_ratios else 0.0

    readings: dict[str, Any] = {}
    worst_ratio = None
    for label, vals in dev_readings(dev_records).items():
        dev_median = statistics.median(vals) if vals else 0.0
        ratio = (reg_median / dev_median) if dev_median else 0.0
        readings[label] = {
            "dev_median_df_ratio": round(dev_median, 6),
            "separability_multiple": round(ratio, 3),
            "passes": ratio >= SEPARABILITY_MULTIPLE,
            "n_terms": len(vals),
        }
        worst_ratio = ratio if worst_ratio is None else min(worst_ratio, ratio)

    gate_0a = {
        "requirement": f"median df/N on regressed questions >= {SEPARABILITY_MULTIPLE}x dev set",
        "regressed_questions": len(regressed),
        "regressed_median_df_ratio": round(reg_median, 6),
        "regressed_n_terms": len(reg_ratios),
        "readings": readings,
        "worst_case_multiple": round(worst_ratio or 0.0, 3),
        "passes": all(r["passes"] for r in readings.values()),
    }

    altered = [
        q for q in regressed if any(t["df_ratio"] > tau for t in held_out[q]["terms"])
    ]
    proof = next(
        (q for q in held_out if q.startswith(CLEANEST_PROOF_QID)),
        None,
    )
    proof_altered = bool(
        proof and any(t["df_ratio"] > tau for t in held_out[proof]["terms"])
    )
    gate_0b = {
        "requirement": (
            f"the gate alters the emitted set on >= {REACHABILITY_MIN_QUESTIONS} "
            f"of {len(regressed)} regressed questions at tau={tau}"
        ),
        "tau": tau,
        "regressed_questions_altered": len(altered),
        "cleanest_proof_qid": proof,
        "cleanest_proof_altered": proof_altered,
        "passes": len(altered) >= REACHABILITY_MIN_QUESTIONS and proof_altered,
    }

    # The sweep is reported, not used: it shows there is no tau where the
    # gate reaches the failure without hitting the dev set at least as
    # hard, which is the substance of the kill rather than a tuning note.
    sweep = []
    dev_engaged = [r for r in dev_records if r["engaged"]]
    for t in (0.02, 0.05, 0.10, 0.20, 0.35, 0.50):
        sweep.append(
            {
                "tau": t,
                "regressed_altered": sum(
                    1
                    for q in regressed
                    if any(x["df_ratio"] > t for x in held_out[q]["terms"])
                ),
                "dev_engaged_probes_altered": sum(
                    1 for r in dev_engaged if any(x["df_ratio"] > t for x in r["terms"])
                ),
                "dev_engaged_probes": len(dev_engaged),
            }
        )

    passes = gate_0a["passes"] and gate_0b["passes"]
    return {
        "verdict": "PASS" if passes else "KILL",
        "gate_0a_separability": gate_0a,
        "gate_0b_reachability": gate_0b,
        "tau_sweep": sweep,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None, metavar="PATH")
    p.add_argument(
        "--dev-census",
        required=True,
        metavar="PATH",
        help="The bench/retrieval df census (bench/df_census.py --instrument retrieval).",
    )
    args = p.parse_args()

    for path in (CENSUS, BASELINE_PQ, LANE_PQ):
        if not path.exists():
            print(f"missing artifact: {path}", file=sys.stderr)
            return 1

    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    held_out = {r["question_id"]: r for r in census["records"]}
    dev = json.loads(Path(args.dev_census).expanduser().read_text(encoding="utf-8"))

    baseline = {
        r["qid"]: r
        for r in json.loads(BASELINE_PQ.read_text(encoding="utf-8"))["arms"]["lexical"]
    }
    lane_payload = json.loads(LANE_PQ.read_text(encoding="utf-8"))
    lane = {r["qid"]: r for r in lane_payload["arms"]["lexical"]}
    if set(baseline) != set(lane):
        print("baseline and lane sidecars score different questions", file=sys.stderr)
        return 1

    regressed = regressed_qids(baseline, lane)
    payload = {
        "preregistration": "PREREGISTRATION.md addendum 4 (2026-08-10)",
        "gate": "Gate 0 — the pre-run kill",
        "inputs": {
            "held_out_census": CENSUS.name,
            "dev_census": Path(args.dev_census).name,
            "baseline_per_question": BASELINE_PQ.name,
            "lane_per_question": LANE_PQ.name,
            "lane_engine_commit": lane_payload["provenance"]["commit"],
            "corpus_sha256": census["corpus_sha256"],
        },
        "note": (
            "Computed from committed artifacts only — no run. Thresholds are "
            "addendum 4's, fixed before the census that judges them existed."
        ),
        **score(held_out, dev["records"], regressed),
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
