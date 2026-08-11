"""Store-derived co-occurrence: does it connect a query to its gold?

The campaign's four completed rounds all conditioned WHICH legs vote.
Round 5 closed that line: three structurally different rules land within
0.004 of each other at held-out macro@5, so the remaining harm is in
what the legs contain rather than in which of them speak. C1 said this
from the start — identical code flips sign between a technical corpus
and a conversational one, so the expansion vocabulary itself has to
become a function of the store.

P1a is that mechanism: instead of committed static tables, derive a
term's expansion candidates from the collection being ranked, by
counting which terms co-occur with it and keeping the ones whose
co-occurrence is higher than chance (positive pointwise mutual
information).

This module measures whether that signal exists before any engine code
is written. For each dev probe it derives associates for the query's
tokens from the pool, and asks the only question that matters:

    do the derived terms appear in the GOLD document, and how much
    junk rides along?

**Statistics only.** No ranking change, no engine integration, no
threshold applied — a parameter grid is swept and reported, and
`bench/longmemeval/PREREGISTRATION.md` fixes the values from it.

Dev-side by construction: it needs gold labels. The held-out instrument
under `bench/heldout/` is NOT read.

    .venv/bin/python bench/ppmi_census.py --out retrieval/results/ppmi-census-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
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

# The grid swept. Every axis is a candidate parameter for addendum 8.
MIN_DF_GRID = (2, 3, 5)
SHIFT_GRID = (1.0, 2.0, 4.0)
TOP_K_GRID = (2, 3, 5, 8)

# PPMI is clamped before ranking so a single freak pair in a small store
# cannot dominate a term's associate list. log(1000) is far above any
# value a real association reaches at these collection sizes; the clamp
# is a guard rail, not a tuning knob.
PPMI_CLAMP = math.log(1000.0)


def document_terms(memories: list[Memory]) -> list[set[str]]:
    """One set of content tokens per memory — the co-occurrence unit.

    A memory body is one or two sentences, so the document IS the
    window. That removes a window-size parameter rather than choosing
    one, which matters when every constant has to be justified from dev
    evidence.
    """
    return [set(engine._memory_tokens(m).content) for m in memories]


def associates(
    docs: list[set[str]],
    term: str,
    *,
    min_df: int,
    shift: float,
    top_k: int,
) -> list[tuple[str, float]]:
    """The `top_k` terms most associated with `term`, by shifted PPMI.

    Pure counting over the collection being ranked:

        ppmi(a,b) = max(0, log( p(a,b) / (p(a) p(b)) ) - log(shift))

    with probabilities estimated by document frequency over `docs`.
    `min_df` keeps hapax pairs out — in a small store a term appearing
    once alongside another produces an enormous, meaningless PPMI, and
    that is the failure mode this floor exists for.

    Deterministic: ties break on the term string, so the same store
    always yields the same list.
    """
    n = len(docs)
    if n == 0:
        return []
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d)
    if df[term] < min_df:
        return []
    co: Counter[str] = Counter()
    for d in docs:
        if term in d:
            co.update(d)
    co.pop(term, None)

    log_shift = math.log(shift)
    out: list[tuple[str, float]] = []
    p_a = df[term] / n
    for other, joint in co.items():
        if df[other] < min_df:
            continue
        p_ab = joint / n
        p_b = df[other] / n
        value = math.log(p_ab / (p_a * p_b)) - log_shift
        if value <= 0.0:
            continue
        out.append((other, min(value, PPMI_CLAMP)))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:top_k]


def derive(
    docs: list[set[str]],
    query_tokens: list[str],
    *,
    min_df: int,
    shift: float,
    top_k: int,
) -> list[str]:
    """Every associate of every query token, deduped and sorted.

    Query tokens themselves are excluded, and so are filler stems — the
    5.1.1 disjointness invariant applies to a store-derived source
    exactly as it applies to the committed tables, because the df-floor
    that deflates filler still only covers the caller's own tokens.
    """
    qset = set(query_tokens)
    found: set[str] = set()
    for tok in dict.fromkeys(query_tokens):
        for other, _weight in associates(
            docs, tok, min_df=min_df, shift=shift, top_k=top_k
        ):
            found.add(other)
    found -= qset
    found -= engine._EXPANSION_TABLES.filler_stems
    return sorted(t for t in found if len(t) >= 3)


def _probe_record(
    memories: list[Memory],
    docs: list[set[str]],
    query: str,
    gold_terms: set[str],
) -> dict[str, Any]:
    """One probe, swept across the grid."""
    raw = engine._expand_kebab(engine.tokenize(query))
    query_tokens = engine._strip_stopwords(raw)
    static = set(
        engine._expansion_terms_impl(
            list(dict.fromkeys(query_tokens)),
            engine._EXPANSION_TABLES,
            engine._stem_token,
        )
    )
    rec: dict[str, Any] = {
        "query_tokens": len(set(query_tokens)),
        "static_terms": len(static),
        "static_hits": len(static & gold_terms),
        "grid": {},
    }
    for min_df in MIN_DF_GRID:
        for shift in SHIFT_GRID:
            for top_k in TOP_K_GRID:
                terms = set(
                    derive(docs, query_tokens, min_df=min_df, shift=shift, top_k=top_k)
                )
                key = f"df{min_df}_s{shift:g}_k{top_k}"
                rec["grid"][key] = {
                    "terms": len(terms),
                    "hits": len(terms & gold_terms),
                    "new_hits": len((terms - static) & gold_terms),
                }
    return rec


def run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sys.path.insert(0, str(_HERE / "retrieval"))
    import run as rr  # type: ignore[import-not-found]  # noqa: PLC0415

    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-ppmi-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        docs = document_terms(memories)
        by_id = {m.id: m for m in memories}
        records: list[dict[str, Any]] = []
        for q in questions:
            gold = by_id[slug_to_id[q["slug"]]]
            gold_terms = set(engine._memory_tokens(gold).content)
            for probe, text in (
                ("asked", q["question"]),
                ("control", rr.strip_question_words(q["question"])),
            ):
                records.append(
                    {
                        "slug": q["slug"],
                        "probe": probe,
                        **_probe_record(memories, docs, text, gold_terms),
                    }
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return records, {
        "instrument": "retrieval",
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-grid-cell totals. `new_hits` is the number that matters: a
    derived term already in the static tables buys nothing."""
    keys = sorted(records[0]["grid"]) if records else []
    grid: dict[str, Any] = {}
    for key in keys:
        cells = [r["grid"][key] for r in records]
        terms = sum(c["terms"] for c in cells)
        grid[key] = {
            "terms_total": terms,
            "hits_total": sum(c["hits"] for c in cells),
            "new_hits_total": sum(c["new_hits"] for c in cells),
            "probes_with_a_new_hit": sum(1 for c in cells if c["new_hits"]),
            "precision": round(sum(c["hits"] for c in cells) / terms, 4)
            if terms
            else 0.0,
            "terms_per_probe": round(terms / len(cells), 2) if cells else 0.0,
        }
    return {
        "probes": len(records),
        "static_terms_total": sum(r["static_terms"] for r in records),
        "static_hits_total": sum(r["static_hits"] for r in records),
        "grid": grid,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Store-derived PPMI census. No ranking change."
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Relative paths resolve against bench/, like the other runners.",
    )
    args = p.parse_args()

    started = time.time()
    records, meta = run()
    payload = {
        **meta,
        "ppmi_clamp": round(PPMI_CLAMP, 6),
        "grid": {
            "min_df": list(MIN_DF_GRID),
            "shift": list(SHIFT_GRID),
            "top_k": list(TOP_K_GRID),
        },
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — store-derived co-occurrence associates and "
            "whether they appear in the gold document. No ranking change, no "
            "threshold applied. Dev-side; bench/heldout is not read."
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
