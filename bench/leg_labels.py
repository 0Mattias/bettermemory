"""TRUE labels for the rescue leg: did its vote IMPROVE the fused result?

Rounds 3 and 4 both calibrated their withholding rule against a proxy —
"the leg's rank-1 is the gold document" — and both paid for it on the
dev set (three questions at recall@5, then two). The proxy's flaw was
stated as a confound in both preregistrations and confirmed by both:
**a leg whose rank-1 is wrong can still lift the gold document**, so
"incorrect" was only ever an upper bound on "harmful", and every
threshold derived from it withheld legs that were helping.

This instrument replaces the proxy with the thing itself. For each
engaged leg it runs the shipped ranker twice over the same store and
query — once with the leg voting, once with it withheld — and records
where the gold document lands each time. The label is the difference.

**Statistics only.** No cap, no threshold, no ranking change ships from
here: both runs use the shipped engine, and the second is produced by
the same suppression the lane already performs when a leg is withheld.

Dev-side by construction: it needs gold labels, which only
`bench/retrieval/` has. The held-out corpus is not read.

    .venv/bin/python bench/leg_labels.py \\
        --out retrieval/results/leg-labels-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402

# The bench reports recall@1 and recall@5, so the label records both the
# raw rank movement and whether the leg crossed the k=5 boundary the
# headline is scored on.
K = 5
# Deep enough that a gold document pushed out of the head is still
# observed rather than censored into "absent".
DEPTH = 50


def _rank_of(hits: list[Any], gold: str) -> int | None:
    for i, hit in enumerate(hits):
        if hit.id == gold:
            return i
    return None


def label_leg(memories: list[Memory], query: str, gold: str) -> dict[str, Any] | None:
    """Where the gold lands with the leg voting, and with it withheld.

    Returns None when the leg never engages — there is nothing to label.

    Both arms run the shipped `search()`. The withheld arm is produced
    by driving the cap's PREDICATE to "no opinion" rather than by
    raising its constant: most legs fail OPEN by design (fewer than
    three candidates, or everything tied below rank 1), so a constant
    high enough to suppress a judgeable leg still lets those vote, and
    the counterfactual would silently be missing exactly the legs the
    cap cannot see. Patching the predicate suppresses every leg, which
    is the counterfactual the label needs.
    """
    saved_predicate = engine._leg_standout
    saved_standout = engine._RESCUE_LEG_STANDOUT
    saved_gate = engine._RESCUE_COVERAGE_GATE

    # Does the leg engage at all? The gate reads the floored base
    # fusion, so silence the leg the way the census does and compare.
    engine._RESCUE_COVERAGE_GATE = -1.0
    try:
        base = engine.search(memories, query, max_results=1, rescue_expansion=True)
    finally:
        engine._RESCUE_COVERAGE_GATE = saved_gate
    raw = engine._expand_kebab(engine.tokenize(query))
    query_tokens = engine._strip_stopwords(raw)
    query_unique = len(set(query_tokens))
    if not query_unique:
        return None
    covered = len(set(base[0].match_terms) & set(query_tokens)) if base else 0
    if (covered / query_unique) >= saved_gate:
        return None  # confident base ranking: no leg to label

    try:
        engine._RESCUE_LEG_STANDOUT = 0.0  # every leg votes
        voting = engine.search(
            memories, query, max_results=DEPTH, rescue_expansion=True
        )
        engine._leg_standout = lambda leg: 0.0  # every leg withheld
        engine._RESCUE_LEG_STANDOUT = 1.0
        withheld = engine.search(
            memories, query, max_results=DEPTH, rescue_expansion=True
        )
    finally:
        engine._leg_standout = saved_predicate
        engine._RESCUE_LEG_STANDOUT = saved_standout

    with_leg = _rank_of(voting, gold)
    without = _rank_of(withheld, gold)
    if [h.id for h in voting] == [h.id for h in withheld]:
        # The leg voted and changed nothing observable — a real
        # outcome, but not one a withholding rule can be judged on.
        return None

    # Absent from the depth window is worse than any observed rank.
    a = DEPTH if with_leg is None else with_leg
    b = DEPTH if without is None else without
    in_k_with = with_leg is not None and with_leg < K
    in_k_without = without is not None and without < K
    if in_k_with and not in_k_without:
        verdict = "rescued"
    elif in_k_without and not in_k_with:
        verdict = "broke"
    elif a < b:
        verdict = "improved"
    elif a > b:
        verdict = "harmed"
    else:
        verdict = "neutral"
    return {
        "gold_rank_with_leg": with_leg,
        "gold_rank_without_leg": without,
        "rank_delta": b - a,  # positive = the leg moved gold UP
        "crossed_k": verdict in ("rescued", "broke"),
        "verdict": verdict,
        "helped": verdict in ("rescued", "improved"),
    }


def _records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sys.path.insert(0, str(_HERE / "retrieval"))
    import run as rr  # type: ignore[import-not-found]  # noqa: PLC0415

    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-leglabel-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        out: list[dict[str, Any]] = []
        for q in questions:
            gold = slug_to_id[q["slug"]]
            probes = (
                ("asked", q["question"]),
                ("requery", q["requery"]),
                ("control", rr.strip_question_words(q["question"])),
            )
            for probe, text in probes:
                label = label_leg(memories, text, gold)
                if label is None:
                    continue
                # The leg's own shape, so a criterion can be derived
                # against the label without a second instrument.
                shape = _leg_shape(memories, text)
                out.append({"slug": q["slug"], "probe": probe, **label, **shape})
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return out, {
        "instrument": "retrieval",
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
    }


def _leg_shape(memories: list[Memory], query: str) -> dict[str, Any]:
    """The candidate signals, read off the same leg the label describes."""
    sys.path.insert(0, str(_HERE))
    import leg_census as lc  # type: ignore[import-not-found]  # noqa: PLC0415

    leg = lc.leg_for(memories, query)
    if leg is None:
        return {}
    return {
        "margin_ratio": leg["margin_ratio"],
        "standout": lc_standout(leg),
        "top_score": leg["top_score"],
        "top_matched": leg["top_matched"],
        "leg_size": leg["leg_size"],
    }


def lc_standout(leg: dict[str, Any]) -> float:
    """Round 4's statistic, recomputed from a census record."""
    gaps = leg["gaps"]
    if len(gaps) < 2:
        return float("inf")
    others = gaps[1:]
    mean_other = sum(others) / len(others)
    if mean_other <= 0:
        return float("inf") if gaps[0] > 0 else 0.0
    return gaps[0] / mean_other


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The label distribution, and how each candidate signal separates it."""
    helped = [r for r in records if r["helped"]]
    hurt = [r for r in records if r["verdict"] in ("broke", "harmed")]
    neutral = [r for r in records if r["verdict"] == "neutral"]

    def band(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        vals = sorted(
            r[key] for r in rows if key in r and r[key] not in (None, float("inf"))
        )
        if not vals:
            return None
        return {
            "n": len(vals),
            "min": round(vals[0], 4),
            "p25": round(vals[len(vals) // 4], 4),
            "p50": round(statistics.median(vals), 4),
            "p75": round(vals[3 * len(vals) // 4], 4),
            "max": round(vals[-1], 4),
        }

    out: dict[str, Any] = {
        "labelled_legs": len(records),
        "verdicts": dict(Counter(r["verdict"] for r in records)),
        "helped": len(helped),
        "hurt": len(hurt),
        "neutral": len(neutral),
    }
    for key in ("margin_ratio", "standout", "top_score", "top_matched", "leg_size"):
        out[key] = {
            "helped": band(helped, key),
            "hurt": band(hurt, key),
            "neutral": band(neutral, key),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="True leg labels. No ranking change.")
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Relative paths resolve against bench/, like the other runners.",
    )
    args = p.parse_args()

    started = time.time()
    records, meta = _records()
    payload = {
        **meta,
        "k": K,
        "depth": DEPTH,
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — the gold document's fused rank with the rescue "
            "leg voting and with it withheld, over the shipped engine. No cap, "
            "no threshold, no ranking change. Dev-side: needs gold labels."
        ),
        "summary": summarise(records),
        "records": records,
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
