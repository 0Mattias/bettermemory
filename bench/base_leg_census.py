"""TRUE labels for the BASE legs: does each leg's vote improve the fusion?

Addendum 9 closed with a named future hypothesis — H-fusion-general:
`_hybrid_fuse` reads rank, not evidence, for the BASE legs too, so a
keyword or BM25 leg whose rank-1 rests on one coincidental token votes
exactly as hard as one whose rank-1 matched four query terms. Rounds 6
and 7 measured the rescue-leg version of that hypothesis and confirmed
the mechanism — the curve, not the idea, was what failed their gates.
This instrument produces the equivalent DEV EVIDENCE for the base pair,
before any preregistration fixes a curve against it.

For each dev question x probe it runs the shipped hybrid ranker three
times over the same store: as shipped (both base legs at weight 1.0),
with the keyword leg withheld (weights [0, 1]), and with the BM25 leg
withheld (weights [1, 0]). The label for a leg is where the gold
document lands with and without that leg's vote — the same
counterfactual `bench/leg_labels.py` measures for the rescue leg, built
the same way: the weight driven to zero, everything else the shipped
engine.

**Statistics only.** No curve, no constant, no ranking change ships
from here: every arm is the shipped engine plus a weight override the
fusion already supports, and the summary tabulates labels against
evidence strata without selecting anything.

Dev-side by construction: it needs gold labels, which only
`bench/retrieval/` has. No held-out instrument is read.

    .venv/bin/python bench/base_leg_census.py \\
        --out retrieval/results/base-leg-labels-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Generator
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402

# Same windows as bench/leg_labels.py, for the same reasons: the bench
# headlines recall@1 and recall@5, and a gold document pushed out of
# the head must be observed rather than censored into "absent".
K = 5
DEPTH = 50

LEGS = ("keyword", "bm25")


@contextlib.contextmanager
def _fusion_tap(override: list[float] | None) -> Generator[dict[str, Any]]:
    """Intercept the base pair's `_hybrid_fuse` call inside `search()`.

    The base pair is the only weightless two-leg call the hybrid path
    makes (the rescue leg never engages here — `rescue_expansion` stays
    off), so matching on that shape is exact. `override`, when given,
    replaces the pair's implicit [1.0, 1.0]; the yielded dict's
    "rankings" snapshots the pair so the leg shapes can be read off the
    very lists that voted.
    """
    real = engine._hybrid_fuse
    tap: dict[str, Any] = {"rankings": None}

    def tapped(
        rankings: list[list[tuple[Memory, float, list[str]]]],
        *,
        rrf_k: int,
        weights: list[float] | None = None,
    ) -> list[tuple[Memory, float, list[str]]]:
        if weights is None and len(rankings) == len(LEGS):
            tap["rankings"] = rankings
            weights = override
        return real(rankings, rrf_k=rrf_k, weights=weights)

    engine._hybrid_fuse = tapped
    try:
        yield tap
    finally:
        engine._hybrid_fuse = real


def _rank_of(hits: list[Any], gold: str) -> int | None:
    for i, hit in enumerate(hits):
        if hit.id == gold:
            return i
    return None


def _verdict(with_leg: int | None, without: int | None) -> dict[str, Any]:
    """`bench/leg_labels.py`'s label, verbatim semantics."""
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


def _query_unique(text: str) -> int:
    raw = engine._expand_kebab(engine.tokenize(text))
    return len(set(engine._strip_stopwords(raw)))


def label_base_legs(
    memories: list[Memory], query: str, gold: str
) -> dict[str, Any] | None:
    """Both base legs' shapes and counterfactual labels for one probe.

    Unlike the rescue leg there is no engagement gate — the base pair
    always votes — so every probe with a non-empty token stream yields a
    record. A leg whose withholding changes nothing observable is
    recorded as `neutral` with `identical_ordering` set, NOT dropped:
    a weight curve fires on every leg, so the population it would touch
    has to include the legs it cannot move.
    """
    uniq = _query_unique(query)
    if not uniq:
        return None

    with _fusion_tap(None) as tap:
        voting = engine.search(memories, query, max_results=DEPTH)
    rankings = tap["rankings"]
    if rankings is None:
        return None  # single-leg degenerate path; nothing to weight

    shapes: dict[str, dict[str, Any]] = {}
    tops: dict[str, str | None] = {}
    for name, leg in zip(LEGS, rankings, strict=True):
        m = engine._leg_top_evidence(leg)
        order = engine._id_order(leg)
        # The raw id is a per-build ULID and would make the artifact
        # non-reproducible; only within-run comparisons of it are kept.
        tops[name] = order[0] if order else None
        shapes[name] = {
            "evidence": m,
            "coverage": round(m / uniq, 4),
            "leg_size": len(leg),
            "top_is_gold": bool(order) and order[0] == gold,
        }

    withheld_hits: dict[str, list[Any]] = {}
    for name, weights in (("keyword", [0.0, 1.0]), ("bm25", [1.0, 0.0])):
        with _fusion_tap(weights):
            withheld_hits[name] = engine.search(memories, query, max_results=DEPTH)

    voting_ids = [h.id for h in voting]
    gold_with = _rank_of(voting, gold)
    legs: dict[str, dict[str, Any]] = {}
    for name in LEGS:
        gold_without = _rank_of(withheld_hits[name], gold)
        label = _verdict(gold_with, gold_without)
        other = LEGS[1] if name == LEGS[0] else LEGS[0]
        legs[name] = {
            **shapes[name],
            **label,
            "identical_ordering": voting_ids == [h.id for h in withheld_hits[name]],
            "evidence_delta": shapes[name]["evidence"] - shapes[other]["evidence"],
        }
    return {
        "query_unique": uniq,
        "tops_agree": tops["keyword"] == tops["bm25"],
        "legs": legs,
    }


def _rr() -> Any:
    """The dev runner module, imported from its own directory once."""
    sys.path.insert(0, str(_HERE / "retrieval"))
    import run  # type: ignore[import-not-found]  # noqa: PLC0415

    return run


def _records(pad_to: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rr = _rr()
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-baseleg-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=pad_to)
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
                record = label_base_legs(memories, text, gold)
                if record is None:
                    continue
                out.append({"slug": q["slug"], "probe": probe, **record})
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return out, {
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
        "pad_to": pad_to,
    }


def _evidence_stratum(m: int) -> str:
    if m >= 4:
        return "4+"
    return str(m)


def _delta_stratum(d: int) -> str:
    if d <= -2:
        return "-2-"
    if d >= 2:
        return "+2+"
    return f"{d:+d}" if d else "0"


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Labels against evidence strata, per leg. Tabulation, no selection."""

    def table(rows: list[dict[str, Any]], stratum_fn: Any) -> dict[str, Any]:
        strata: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            strata.setdefault(stratum_fn(r), []).append(r)
        out: dict[str, Any] = {}
        for name in sorted(strata):
            group = strata[name]
            helped = sum(1 for g in group if g["helped"])
            hurt = sum(1 for g in group if g["verdict"] in ("broke", "harmed"))
            up1 = sum(
                1
                for g in group
                if g["gold_rank_with_leg"] == 0 and g["gold_rank_without_leg"] != 0
            )
            down1 = sum(
                1
                for g in group
                if g["gold_rank_without_leg"] == 0 and g["gold_rank_with_leg"] != 0
            )
            out[name] = {
                "n": len(group),
                "helped": helped,
                "hurt": hurt,
                "neutral": len(group) - helped - hurt,
                "pct_helped": round(helped / len(group), 4),
                "gold_to_rank1": up1,
                "gold_off_rank1": down1,
            }
        return out

    summary: dict[str, Any] = {
        "probes": len(records),
        "tops_agree": sum(1 for r in records if r["tops_agree"]),
    }
    for leg in LEGS:
        rows = [r["legs"][leg] for r in records]
        summary[leg] = {
            "verdicts": dict(Counter(r["verdict"] for r in rows)),
            "identical_ordering": sum(1 for r in rows if r["identical_ordering"]),
            "by_evidence": table(rows, lambda r: _evidence_stratum(r["evidence"])),
            "by_evidence_delta": table(
                rows, lambda r: _delta_stratum(r["evidence_delta"])
            ),
        }
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="True base-leg labels. No ranking change.")
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Relative paths resolve against bench/, like the other runners.",
    )
    args = p.parse_args()

    rr = _rr()
    started = time.time()
    arms: dict[str, Any] = {}
    for label, pad in (("unpadded", None), ("padded600", 600)):
        records, meta = _records(pad)
        arms[label] = {**meta, "summary": summarise(records), "records": records}
    payload = {
        "provenance": rr._provenance(),
        "k": K,
        "depth": DEPTH,
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — each base leg's counterfactual label (the "
            "gold document's fused rank with the leg voting and with its "
            "weight driven to zero) against the evidence behind the leg's "
            "own rank-1, over the shipped engine. H-fusion-general's design "
            "census; no curve, no constant, no ranking change."
        ),
        "arms": arms,
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
