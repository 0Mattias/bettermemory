"""Census of the rescue leg's OWN evidence, per question.

Round 2 killed df-gating and named why in the process: `_hybrid_fuse`
fuses by RANK, so the expansion leg contributes
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` no matter how thin the evidence
behind its rank-1 is. A leg built of rare-but-wrong terms still emits a
confident-looking rank-1 and still collects 0.7 of a vote. The harmful
terms turned out to be individually RARE, so the damage is not in the
vocabulary — it is in the voting.

This instrument measures the voting side. For every question it records
what the leg actually had when it voted:

- `top_score` — the leg's rank-1 BM25 score, its raw confidence;
- `margin` — rank-1 minus rank-2, how separated that confidence is;
- `top_matched` — how many synthesized terms the rank-1 candidate hit;
- `leg_size` — how many candidates the leg ranked at all.

and, on an instrument with gold labels, whether the leg's rank-1 was
actually right. That pairing is what makes a threshold derivable: a cap
is only worth having if weak-evidence legs are disproportionately wrong.

**Statistics only.** No gate, no cap, no ranking change — the shipped
engine runs untouched and this reads what it produced.

The leg is reconstructed through the engine's own helpers, with the
same arguments `search()` passes, rather than re-derived. The
reconstruction is pinned against `search()`'s observable output in
`tests/test_bench_leg_census.py`: every hit the engine labels
`matched_leg="expansion"` must appear in the reconstructed leg.

    .venv/bin/python bench/leg_census.py --instrument retrieval \\
        --out retrieval/results/leg-census-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402


def leg_for(memories: list[Memory], query: str) -> dict[str, Any] | None:
    """Rebuild the expansion leg `search()` would score, or None.

    None means the leg never ran for this query: the coverage gate
    stayed shut, the query stripped to nothing, or the tables
    synthesized no terms. Each argument below mirrors the engine's own
    call site; the defaults are the ones both bench runners use (no
    provider, no usage-aware factors), so this is the leg the recall
    runs actually scored.
    """
    raw = engine._expand_kebab(engine.tokenize(query))
    query_tokens = engine._strip_stopwords(raw)
    if not query_tokens:
        return None  # stopword fallback: the rescue is skipped entirely

    # The gate reads the FLOORED base fusion, so reproduce that first.
    query_unique = len(set(query_tokens))
    saved = engine._RESCUE_COVERAGE_GATE
    engine._RESCUE_COVERAGE_GATE = -1.0
    try:
        base = engine.search(memories, query, max_results=1, rescue_expansion=True)
    finally:
        engine._RESCUE_COVERAGE_GATE = saved
    covered = len(set(base[0].match_terms) & set(query_tokens)) if base else 0
    coverage = covered / query_unique if query_unique else 0.0
    if coverage >= saved:
        return None  # confident base ranking: the leg never engages

    exp_terms = engine._expansion_terms_impl(
        list(dict.fromkeys(query_tokens)), engine._EXPANSION_TABLES, engine._stem_token
    )
    if not exp_terms:
        return None

    candidate_tokens = [engine._memory_tokens(m) for m in memories]
    stats = engine._filler_floor_stats(None, query_tokens, len(memories))
    # `now` and `half_life_days` are the engine's own defaults at this
    # call site; both bench runners leave them alone, so the leg scored
    # here is the leg the recall runs scored.
    leg = engine._score_bm25(
        memories,
        exp_terms,
        now=datetime.now(timezone.utc),
        half_life_days=30.0,
        candidate_tokens=candidate_tokens,
        corpus_stats=stats,
    )
    if not leg:
        return None
    ordered = sorted(leg, key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    top_score = ordered[0][1]
    runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
    return {
        "coverage": round(coverage, 4),
        "leg_size": len(ordered),
        "top_score": round(top_score, 6),
        "runner_up_score": round(runner_up, 6),
        "margin": round(top_score - runner_up, 6),
        "margin_ratio": round((top_score - runner_up) / top_score, 4)
        if top_score
        else 0.0,
        "top_matched": len(ordered[0][2]),
        "terms": len(exp_terms),
        "top_id": ordered[0][0].id,
        "ranked_ids": [m.id for m, _, _ in ordered[:10]],
    }


def _retrieval_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Instrument A: gold-labelled, so leg correctness is observable."""
    sys.path.insert(0, str(_HERE / "retrieval"))
    import run as rr  # type: ignore[import-not-found]  # noqa: PLC0415

    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-legcensus-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        records: list[dict[str, Any]] = []
        for q in questions:
            gold = slug_to_id[q["slug"]]
            probes = (
                ("asked", q["question"]),
                ("requery", q["requery"]),
                ("control", rr.strip_question_words(q["question"])),
            )
            for probe, text in probes:
                leg = leg_for(memories, text)
                rec: dict[str, Any] = {
                    "slug": q["slug"],
                    "probe": probe,
                    "engaged": leg is not None,
                }
                if leg is not None:
                    rec.update(leg)
                    rec["leg_top_is_gold"] = leg["top_id"] == gold
                    ranked = leg["ranked_ids"]
                    rec["leg_gold_rank"] = (
                        ranked.index(gold) if gold in ranked else None
                    )
                    rec.pop("top_id")
                    rec.pop("ranked_ids")
                records.append(rec)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return records, {
        "instrument": "retrieval",
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool the per-question records, split by whether the leg was RIGHT.

    The split is the whole point. A cap keyed on leg evidence is only
    derivable if the legs that voted wrong had visibly worse evidence
    than the legs that voted right; if the two distributions sit on top
    of each other the mechanism is dead the same way df-gating was, and
    that is worth finding out before anything is preregistered.
    """
    engaged = [r for r in records if r["engaged"]]
    right = [r for r in engaged if r.get("leg_top_is_gold")]
    wrong = [r for r in engaged if r.get("leg_top_is_gold") is False]

    def stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        vals = sorted(r[key] for r in rows)
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
        "questions": len(records),
        "engaged": len(engaged),
        "leg_top_is_gold": len(right),
        "leg_top_is_wrong": len(wrong),
    }
    for key in ("top_score", "margin", "margin_ratio", "top_matched", "leg_size"):
        out[key] = {
            "right": stats(right, key),
            "wrong": stats(wrong, key),
            "all_engaged": stats(engaged, key),
        }
    return out


def _longmemeval_records(
    limit: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Instrument B, POST-RUN only.

    Addendum 5 reads nothing from the held-out corpus before its arms
    run — that is round 3's discipline improvement over round 2. This
    path exists to score P16 ("the cap fires on at least 40% of
    engaging questions") AFTER the arms are published, which measures
    what already happened rather than feeding a parameter. No gold
    label here, so no correctness split.
    """
    sys.path.insert(0, str(_HERE / "longmemeval"))
    import run as lr  # type: ignore[import-not-found]  # noqa: PLC0415

    path = lr.DEFAULT_CORPUS
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if limit:
        corpus = corpus[:limit]
    records: list[dict[str, Any]] = []
    for inst in corpus:
        if not inst["answer_session_ids"]:
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-legcensus-"))
        try:
            lr.build_question_store(root, inst)
            memories = Store(root).load_all()
            leg = leg_for(memories, inst["question"])
            rec: dict[str, Any] = {
                "question_id": inst.get("question_id", ""),
                "question_type": inst.get("question_type", ""),
                "engaged": leg is not None,
            }
            if leg is not None:
                rec.update(leg)
                rec.pop("top_id")
                rec.pop("ranked_ids")
            records.append(rec)
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return records, {
        "instrument": "longmemeval",
        "corpus": path.name,
        "corpus_sha256": lr.corpus_fingerprint(path),
        "instances": len(corpus),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Rescue-leg evidence census. No ranking change."
    )
    p.add_argument(
        "--instrument", choices=("retrieval", "longmemeval"), default="retrieval"
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "A RELATIVE path resolves against this script's directory "
            "(bench/), matching the other runners."
        ),
    )
    args = p.parse_args()

    started = time.time()
    if args.instrument == "retrieval":
        records, meta = _retrieval_records()
    else:
        records, meta = _longmemeval_records(args.limit)
    payload = {
        **meta,
        "coverage_gate": engine._RESCUE_COVERAGE_GATE,
        "leg_weight": engine._RESCUE_LEG_WEIGHT,
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — the rescue leg's own evidence per question. No "
            "gate, no cap, no ranking change. Dev-side instrument: the "
            "held-out corpus is not read."
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
