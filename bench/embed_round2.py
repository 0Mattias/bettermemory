"""Census 2 of the P1e family, run exactly as `P1E_CENSUS2_DECLARATION.md` fixes it.

That document is committed BEFORE this file produces a number, and it
fixes everything a result could otherwise be shopped against: the 128
cells, both readings of P1a's bar, the primary cell that alone carries
the verdict, the readiness criterion, and the parking criterion. This
module is its executable form and adds nothing to it.

The three things worth reading here rather than there:

**The veto.** For query token `t` and candidate `c`, emit `c` only if
`ppmi(t, c) > 0` under P1a's own `associates`. A veto, not a selector —
the dense model still chooses and ranks, and the counts only remove
candidates they do not independently support. Census 1's agreement rule
intersected the two top-k lists and measured worse than the dense model
alone, because rank agreement selects for high-count and therefore
undiscriminating pairs; a positivity veto has no such preference.

**Reading B.** The incumbent re-estimated at a narrow challenger's own
width by uniform subsampling, so a narrow cell cannot win on width
alone. The declaration predicts this reproduces 0.2743 at every width,
because the committed tables emit an unordered set with no score to
narrow by. A material drift would invalidate every narrow-cell
comparison in census 1, including its 0.989x headline, and that is
stated there in advance rather than discovered here.

**The primary cell.** `store / centred / k=2 / tau=0.99 /
veto=ppmi_positive / bridging=off`, selected by the declaration's stated
rule and not by prospects. Its census-1 value without the veto is
already published (0.1923, 0.701x, 9.10 per probe), so the veto's effect
is a within-cell delta against a number in the record. The other 127
cells are the family's shape and cannot carry the verdict.

**Statistics only.** No ranking change, no engine code, no
preregistration. Dev-side; `bench/heldout/` is NOT read.

    .venv/bin/python bench/embed_round2.py --vectors store=/tmp/store.json \\
        --out retrieval/results/embed-census2-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

sys.path.insert(0, str(_HERE))

import bettermemory.search as engine  # noqa: E402
from bettermemory.store import Store  # noqa: E402
from embed_census import (  # noqa: E402
    GATE_MULTIPLE,
    MIN_GATE_TERMS,
    P1A_INCUMBENT_PRECISION,
    Model,
    candidate_pool,
    collection_vocabulary,
    query_tokens_of,
    source_manifest,
    static_terms,
    two_proportion_p,
    wilson,
)
from embed_hybrid import PPMI_MIN_DF, PPMI_SHIFT, _PPMI, bridge, ngrams  # noqa: E402
from embed_train import load, load_bench_module  # noqa: E402

# ---------------------------------------------------------------------------
# The declared family. 4 x 4 x 2 x 2 cells per postproc reading, 128 total.
# Nothing may be added here after the declaration commit.
# ---------------------------------------------------------------------------

TOP_K_GRID = (1, 2, 3, 5)
TAU_GRID = (0.95, 0.98, 0.99, 0.995)
VETO_MODES = ("none", "ppmi_positive")
BRIDGING_MODES = (False, True)
POSTPROC_GRID = ("raw", "centred")

PRIMARY_ARM = "centred"
PRIMARY_CELL = "k2_t0.99_ppmi_positive_nobridge"

# Reading A's floor and R3's bound are the incumbent's own published
# figures. Both are RECOMPUTED at run time and asserted against these,
# so a token-pipeline drift voids the run instead of quietly rebasing
# the bar. Source: retrieval/results/embed-census-2026-08-11.json.
READING_A_MIN_WIDTH = 5.65
R3_CI_LOWER_BOUND = 0.2203

# Reading B: uniform subsampling of the incumbent's emitted terms.
READING_B_SEEDS = 2000
READING_B_SEED = 20260811


def dense_ranked(
    vector: list[float], model: Model, pool: list[str]
) -> list[tuple[float, str]]:
    """Every candidate by descending cosine, ties broken on the term."""
    scored = [(sum(a * b for a, b in zip(vector, model.vec[t])), t) for t in pool]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


def ppmi_positive(docs: list[set[str]], token: str, pool: set[str]) -> set[str]:
    """Candidates with an above-chance co-occurrence with `token`.

    P1a's `associates` verbatim at an unbounded top-k, so the veto is
    the estimator that census measured rather than a lookalike. Note it
    also drops candidates below `min_df` — that is inherent to P1a's
    estimator and is part of what the declaration put under test.
    """
    pairs = _PPMI.associates(
        docs, token, min_df=PPMI_MIN_DF, shift=PPMI_SHIFT, top_k=10**6
    )
    return {term for term, _weight in pairs if term in pool}


def probe_record(
    model: Model,
    docs: list[set[str]],
    query: str,
    gold_terms: set[str],
    collection: set[str],
    gram_index: dict[str, frozenset[str]],
) -> dict[str, Any]:
    """One probe across every declared cell, in both bridging modes."""
    tokens = query_tokens_of(query)
    unique = list(dict.fromkeys(tokens))
    static = static_terms(tokens)
    vocab_set = set(model.vocab)
    pool = candidate_pool(collection, vocab_set, set(tokens))
    pool_set = set(pool)

    ranked: dict[bool, dict[str, list[tuple[float, str]]]] = {}
    bridged_counts: dict[bool, int] = {}
    for bridging in BRIDGING_MODES:
        vectors: dict[str, list[float]] = {}
        for token in unique:
            if token in model.vec:
                vectors[token] = model.vec[token]
            elif bridging:
                built = bridge(token, model, gram_index)
                if built is not None:
                    vectors[token] = built
        ranked[bridging] = {t: dense_ranked(v, model, pool) for t, v in vectors.items()}
        bridged_counts[bridging] = sum(1 for t in vectors if t not in model.vec)

    allowed = {
        "none": None,
        "ppmi_positive": {t: ppmi_positive(docs, t, pool_set) for t in unique},
    }

    rec: dict[str, Any] = {
        "query_tokens": len(unique),
        "query_tokens_in_vocab": len(ranked[False]),
        "query_tokens_with_bridge": len(ranked[True]),
        "query_tokens_bridged": bridged_counts[True],
        "candidate_pool": len(pool),
        "static_terms": len(static),
        "static_hits": len(static & gold_terms),
        "static_hit_flags": sorted(static & gold_terms),
        "grid": {},
    }
    for bridging in BRIDGING_MODES:
        label = "bridge" if bridging else "nobridge"
        for veto in VETO_MODES:
            gate = allowed[veto]
            for tau in TAU_GRID:
                kept: dict[str, list[str]] = {}
                for token, lst in ranked[bridging].items():
                    ok = gate[token] if gate is not None else None
                    kept[token] = [
                        term
                        for sim, term in lst
                        if sim >= tau and (ok is None or term in ok)
                    ]
                for top_k in TOP_K_GRID:
                    terms: set[str] = set()
                    for lst2 in kept.values():
                        terms.update(lst2[:top_k])
                    key = f"k{top_k}_t{tau:g}_{veto}_{label}"
                    rec["grid"][key] = {
                        "terms": len(terms),
                        "hits": len(terms & gold_terms),
                        "new_hits": len((terms - static) & gold_terms),
                    }
    return rec


def reading_b(pool_flags: list[bool], width: float, probes: int) -> dict[str, Any]:
    """The incumbent re-estimated at `width` terms per probe.

    Uniform subsampling without replacement over the incumbent's pooled
    emitted terms. Unbiased by construction, which is exactly the point:
    the declaration predicts this reproduces the incumbent's precision at
    every width, because the committed tables emit an unordered set with
    no score a narrower cut could exploit.
    """
    target = int(round(width * probes))
    total = len(pool_flags)
    if target <= 0 or target > total:
        return {"width": round(width, 2), "target_terms": target, "applicable": False}
    rng = random.Random(READING_B_SEED)
    draws = []
    for _ in range(READING_B_SEEDS):
        draws.append(sum(rng.sample(pool_flags, target)) / target)
    draws.sort()
    return {
        "width": round(width, 2),
        "target_terms": target,
        "applicable": True,
        "mean": round(statistics.fmean(draws), 4),
        "p05": round(draws[int(0.05 * (READING_B_SEEDS - 1))], 4),
        "p95": round(draws[int(0.95 * (READING_B_SEEDS - 1))], 4),
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cell totals against the incumbent recomputed on the same probes."""
    static_terms_total = sum(r["static_terms"] for r in records)
    static_hits_total = sum(r["static_hits"] for r in records)
    n = len(records)
    incumbent = (
        round(static_hits_total / static_terms_total, 4) if static_terms_total else 0.0
    )
    inc_lo, inc_hi = wilson(static_hits_total, static_terms_total)
    grid: dict[str, Any] = {}
    for key in sorted(records[0]["grid"]) if records else []:
        cells = [r["grid"][key] for r in records]
        terms = sum(c["terms"] for c in cells)
        hits = sum(c["hits"] for c in cells)
        precision = round(hits / terms, 4) if terms else 0.0
        lo, hi = wilson(hits, terms)
        grid[key] = {
            "terms_total": terms,
            "hits_total": hits,
            "new_hits_total": sum(c["new_hits"] for c in cells),
            "precision": precision,
            "precision_ci95": [round(lo, 4), round(hi, 4)],
            "terms_per_probe": round(terms / n, 2) if n else 0.0,
            "gate_multiple": round(precision / incumbent, 3) if incumbent else 0.0,
            "p_vs_incumbent": round(
                two_proportion_p(hits, terms, static_hits_total, static_terms_total), 4
            ),
        }
    return {
        "probes": n,
        "incumbent_precision": incumbent,
        "incumbent_precision_ci95": [round(inc_lo, 4), round(inc_hi, 4)],
        "incumbent_terms_total": static_terms_total,
        "incumbent_hits_total": static_hits_total,
        "incumbent_terms_per_probe": round(static_terms_total / n, 2) if n else 0.0,
        "grid": grid,
    }


def readiness(arms: dict[str, Any]) -> dict[str, Any]:
    """R1-R4 on the primary cell, then the family-wide parking test.

    Both are the declaration's, evaluated mechanically. `eligible` below
    is the anti-gate-shopping bookkeeping: cells other than the primary
    that would have satisfied R1-R4 are counted and named, and are
    explicitly recorded as licensing at most a census-3 declaration.
    """
    summary = arms[PRIMARY_ARM]["summary"]
    grid = summary["grid"]
    cell = grid.get(PRIMARY_CELL)
    if cell is None:
        return {"verdict": "VOID", "reason": f"primary cell {PRIMARY_CELL} not in grid"}

    r1 = cell["gate_multiple"] >= GATE_MULTIPLE
    r2 = cell["terms_per_probe"] >= READING_A_MIN_WIDTH
    r3 = cell["precision_ci95"][0] >= R3_CI_LOWER_BOUND
    r4 = cell["terms_total"] >= MIN_GATE_TERMS
    licensed = r1 and r2 and r3 and r4

    def qualifies(c: dict[str, Any]) -> bool:
        return (
            c["gate_multiple"] >= GATE_MULTIPLE
            and c["terms_per_probe"] >= READING_A_MIN_WIDTH
            and c["precision_ci95"][0] >= R3_CI_LOWER_BOUND
            and c["terms_total"] >= MIN_GATE_TERMS
        )

    at_width_pass = sorted(
        f"{arm}/{key}"
        for arm, a in arms.items()
        for key, c in a["summary"]["grid"].items()
        if c["gate_multiple"] >= GATE_MULTIPLE
        and c["terms_per_probe"] >= READING_A_MIN_WIDTH
        and c["terms_total"] >= MIN_GATE_TERMS
    )
    others = sorted(
        f"{arm}/{key}"
        for arm, a in arms.items()
        for key, c in a["summary"]["grid"].items()
        if qualifies(c) and not (arm == PRIMARY_ARM and key == PRIMARY_CELL)
    )
    parked = (not r1) and not at_width_pass

    if licensed:
        verdict = "LICENSES THE P1e PREREGISTRATION"
    elif parked:
        verdict = "PARKS THE LANE"
    else:
        verdict = "NEITHER — census-3 declaration at most"
    return {
        "primary_arm": PRIMARY_ARM,
        "primary_cell": PRIMARY_CELL,
        "primary": cell,
        "R1_gate_multiple_ge_1": r1,
        "R2_width_ge_incumbent": r2,
        "R3_ci_lower_ge_incumbent_lower": r3,
        "R4_min_sample": r4,
        "licensed": licensed,
        "parked": parked,
        "cells_passing_at_width": at_width_pass,
        "non_primary_cells_meeting_R1_R4": others,
        "anti_gate_shopping": (
            "A non-primary cell meeting R1-R4 licenses at most a census-3 "
            "declaration naming it as that census's primary. It does not "
            "license a preregistration; the maximum of 128 cells is not a "
            "preregistered hypothesis."
        ),
        "verdict": verdict,
    }


def run(model_by_mode: dict[str, Model]) -> tuple[dict[str, Any], dict[str, Any]]:
    rr = load_bench_module("embed_round2_ret", _HERE / "retrieval" / "run.py")
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-round2-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        by_id = {m.id: m for m in memories}
        docs = [set(engine._memory_tokens(m).content) for m in memories]
        collection = collection_vocabulary(memories)
        probes = []
        for q in questions:
            gold = by_id[slug_to_id[q["slug"]]]
            gold_terms = set(engine._memory_tokens(gold).content)
            probes.append((q["slug"], "asked", q["question"], gold_terms))
            probes.append(
                (
                    q["slug"],
                    "control",
                    rr.strip_question_words(q["question"]),
                    gold_terms,
                )
            )
        arms: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        for mode, model in model_by_mode.items():
            gram_index = {t: ngrams(t) for t in model.vocab}
            records = [
                {
                    "slug": slug,
                    "probe": probe,
                    **probe_record(model, docs, text, gold, collection, gram_index),
                }
                for slug, probe, text, gold in probes
            ]
            arms[mode] = {"summary": summarise(records)}
            if not rows:
                rows = records
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Reading B needs the incumbent's per-term hit flags, pooled.
    pool_flags: list[bool] = []
    for r in rows:
        hits = len(r["static_hit_flags"])
        pool_flags.extend([True] * hits + [False] * (r["static_terms"] - hits))
    return arms, {
        "instrument": "retrieval",
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
        "collection_vocabulary": len(collection),
        "incumbent_pool_terms": len(pool_flags),
        "_pool_flags": pool_flags,
        "_probes": len(rows),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="P1e census 2, as declared. Statistics only; no engine code."
    )
    p.add_argument("--vectors", required=True, metavar="NAME=PATH")
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    started = time.time()
    name, _, raw_path = args.vectors.partition("=")
    path = Path(raw_path).expanduser()
    vocab, vectors, meta = load(path)
    models = {mode: Model(vocab, vectors, mode) for mode in POSTPROC_GRID}

    arms, instrument = run(models)
    pool_flags = instrument.pop("_pool_flags")
    probes = instrument.pop("_probes")

    summary = arms[PRIMARY_ARM]["summary"]
    if abs(summary["incumbent_precision"] - P1A_INCUMBENT_PRECISION) > 0.0001:
        raise SystemExit(
            f"incumbent recomputed as {summary['incumbent_precision']}, but the "
            f"declaration fixed {P1A_INCUMBENT_PRECISION} — the token pipeline "
            "moved and this comparison is void"
        )
    if abs(summary["incumbent_terms_per_probe"] - READING_A_MIN_WIDTH) > 0.01:
        raise SystemExit(
            f"incumbent width recomputed as {summary['incumbent_terms_per_probe']}, "
            f"but Reading A was declared against {READING_A_MIN_WIDTH}"
        )

    widths = sorted(
        {
            c["terms_per_probe"]
            for a in arms.values()
            for c in a["summary"]["grid"].values()
            if c["terms_total"] >= MIN_GATE_TERMS
        }
    )
    reading_b_curve = [reading_b(pool_flags, w, probes) for w in widths]

    payload = {
        "kind": "bettermemory-p1e-census2",
        "declaration": "bench/P1E_CENSUS2_DECLARATION.md",
        "declared_family": {
            "top_k": list(TOP_K_GRID),
            "tau": list(TAU_GRID),
            "veto": list(VETO_MODES),
            "bridging": ["off", "on"],
            "postproc": list(POSTPROC_GRID),
            "cells": len(TOP_K_GRID)
            * len(TAU_GRID)
            * len(VETO_MODES)
            * len(BRIDGING_MODES)
            * len(POSTPROC_GRID),
        },
        "bars": {
            "gate": (
                "best cell precision >= 1.0x the committed tables' precision "
                "(addendum 8, quoted unchanged)"
            ),
            "incumbent_precision": P1A_INCUMBENT_PRECISION,
            "reading_a_min_terms_per_probe": READING_A_MIN_WIDTH,
            "r3_ci_lower_bound": R3_CI_LOWER_BOUND,
            "min_gate_terms": MIN_GATE_TERMS,
            "reading_b_seeds": READING_B_SEEDS,
        },
        "note": (
            "STATISTICS ONLY — emitted-term precision against the gold "
            "document, scored against bars fixed in "
            "bench/P1E_CENSUS2_DECLARATION.md before this file produced a "
            "number. No ranking change, no engine code, no preregistration. "
            "Dev-side; bench/heldout is not read."
        ),
        "model": {
            "name": name,
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "trainer_sha256": meta["trainer_sha256"],
            "corpus": meta["corpus"],
            "corpus_manifest_sha256": meta["corpus_manifest_sha256"],
            "parameters": meta["parameters"],
            "corpus_stats": meta["corpus_stats"],
            "sources": source_manifest(meta["sources"]),
        },
        **instrument,
        "reading_b_incumbent_by_width": reading_b_curve,
        "arms": arms,
        "readiness": readiness(arms),
        "seconds": round(time.time() - started, 1),
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
