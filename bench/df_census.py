"""Document-frequency census of the rescue lane's EMITTED expansion terms.

Statistics only. This script computes no recall, reads no label, and
touches no ranking — it answers one question on either instrument:

    when the rescue leg fires, how common in the collection being
    ranked are the terms it synthesizes?

That number is the input to round 2's design decision (is an emitted
term's df a usable separator between the vocabulary that helps a
technical store and the vocabulary that harms a conversational one),
and it is deliberately produced BEFORE any gate exists in code, so a
threshold can be fixed from corpus structure rather than fitted to an
outcome. `bench/longmemeval/PREREGISTRATION.md` addendum 4 declares
exhaustively what is read from the held-out corpus: this file's output
and nothing else.

Precedent for inspecting the held-out corpus's structure before running
it: addendum 1 fixed the recall@k metric after reading the corpus's
evidence-count distribution, for the same reason — a design that has to
be corrected after seeing outcomes is how a pre-registration becomes
decoration.

The pipeline mirrors `search()` exactly rather than approximating it:
the same tokenizer, the same stopword strip, the same
`expansion_terms` build site, the same coverage gate. A census computed
off a hand-mirrored token pipeline would measure a different quantity
than the one a gate would see.

    .venv/bin/python bench/df_census.py --instrument retrieval
    .venv/bin/python bench/df_census.py --instrument longmemeval \\
        --out bench/longmemeval/results/df-census-YYYY-MM-DD.json
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

from bettermemory.expansion import expansion_terms  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.search import (  # noqa: E402
    _EXPANSION_TABLES,
    _RESCUE_COVERAGE_GATE,
    _expand_kebab,
    _memory_tokens,
    _stem_token,
    _strip_stopwords,
    search,
    tokenize,
)
from bettermemory.store import Store  # noqa: E402


def query_tokens_of(query: str) -> list[str]:
    """The token list `search()` would rank with, by the same route.

    `search()` tokenizes, expands kebab compounds, strips stopwords,
    and falls back to the unstripped stream when stripping empties the
    query (`search.py`'s stopword-fallback branch). The rescue leg is
    skipped entirely on that fallback, so a census that ignored it
    would count terms no leg can emit.
    """
    raw = _expand_kebab(tokenize(query))
    stripped = _strip_stopwords(raw)
    return stripped


def emitted_terms(query: str) -> list[str]:
    """The terms the rescue leg would synthesize for `query`."""
    toks = query_tokens_of(query)
    if not toks:
        return []
    return expansion_terms(list(dict.fromkeys(toks)), _EXPANSION_TABLES, _stem_token)


def gate_engages(memories: list[Memory], query: str) -> bool:
    """Would the coverage gate open the rescue leg for this question?

    Derived from the engine, not re-implemented: the gate asks whether
    the FLOORED base fusion's top hit covers `_RESCUE_COVERAGE_GATE` of
    the query's unique tokens. So run the lane with the leg silenced
    the same way `--ablate floor-only` silences it, read the top hit's
    `match_terms`, and apply the gate's own arithmetic. Re-deriving the
    fusion here instead would be exactly the hand-mirrored pipeline the
    module docstring rejects.

    Note this is NOT "a hit reported matched_leg=expansion" — that
    label only appears for a hit the base legs missed entirely, so it
    undercounts engagement badly on a corpus where the leg reorders
    rather than rescues.
    """
    toks = query_tokens_of(query)
    query_unique = len(set(toks))
    if not query_unique:
        return False
    import bettermemory.search as engine  # noqa: PLC0415

    saved = engine._RESCUE_COVERAGE_GATE
    engine._RESCUE_COVERAGE_GATE = -1.0
    try:
        hits = search(memories, query, max_results=1, rescue_expansion=True)
    finally:
        engine._RESCUE_COVERAGE_GATE = saved
    if not hits:
        return True  # coverage 0.0 < gate
    covered = len(set(hits[0].match_terms) & set(toks))
    return (covered / query_unique) < saved


def term_df(memories: list[Memory], terms: list[str]) -> dict[str, int]:
    """Document frequency of each term over the collection, one pass.

    Counted against `_MemoryTokens.content` — the stopword-stripped
    stream the BM25 legs score against, which is the stream whose df a
    gate would price with. One pass over the memories, not one per
    term: the per-term shape is what the cost note in addendum 4
    rejects.
    """
    df = dict.fromkeys(terms, 0)
    if not terms:
        return df
    wanted = set(terms)
    for memory in memories:
        present = wanted & set(_memory_tokens(memory).content)
        for tok in present:
            df[tok] += 1
    return df


def question_census(memories: list[Memory], query: str) -> dict[str, Any]:
    """One question's emitted-term df/N record."""
    terms = emitted_terms(query)
    n = len(memories)
    df = term_df(memories, terms)
    ratios = {t: (df[t] / n if n else 0.0) for t in terms}
    live = {t: r for t, r in ratios.items() if df[t] > 0}
    return {
        "n_collection": n,
        "emitted": len(terms),
        "emitted_with_df_gt0": len(live),
        "engaged": gate_engages(memories, query) if terms else False,
        "terms": [
            {"term": t, "df": df[t], "df_ratio": round(ratios[t], 6)} for t in terms
        ],
        "max_df_ratio": round(max(ratios.values()), 6) if ratios else 0.0,
        "median_df_ratio_live": (
            round(statistics.median(live.values()), 6) if live else None
        ),
    }


_BANDS = (0.0, 0.01, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 1.01)


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool the per-question records into the distribution τ is read off.

    Two populations are reported and never merged: every emitted term,
    and only the terms with a non-zero df. `morph_variants` is a rule,
    so a query like "guessing" emits non-words ('gue', 'guesed')
    alongside 'guess'; they match nothing, cost nothing, and would
    otherwise pile into the lowest band and make any gate look
    generous.
    """
    live = [t for r in records for t in r["terms"] if t["df"] > 0]
    ratios = sorted(t["df_ratio"] for t in live)
    bands: Counter[str] = Counter()
    for ratio in ratios:
        for lo, hi in zip(_BANDS, _BANDS[1:]):
            if lo <= ratio < hi:
                bands[f"[{lo:g},{hi:g})"] += 1
                break
    engaged = [r for r in records if r["engaged"]]
    out: dict[str, Any] = {
        "questions": len(records),
        "questions_engaging_the_leg": len(engaged),
        "emitted_terms": sum(r["emitted"] for r in records),
        "emitted_terms_with_df_gt0": len(live),
        "df_ratio_bands_live": dict(bands),
    }
    if ratios:
        out["df_ratio_live"] = {
            "min": round(ratios[0], 6),
            "p50": round(statistics.median(ratios), 6),
            "p75": round(ratios[int(0.75 * (len(ratios) - 1))], 6),
            "p90": round(ratios[int(0.90 * (len(ratios) - 1))], 6),
            "p95": round(ratios[int(0.95 * (len(ratios) - 1))], 6),
            "max": round(ratios[-1], 6),
        }
    per_q = [r["median_df_ratio_live"] for r in records if r["median_df_ratio_live"]]
    if per_q:
        out["per_question_median_df_ratio"] = {
            "p50": round(statistics.median(per_q), 6),
            "questions": len(per_q),
        }
    return out


def _retrieval_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Instrument A: the blind-authored gold set, as-asked probe."""
    sys.path.insert(0, str(_HERE / "retrieval"))
    import run as rr  # type: ignore[import-not-found]  # noqa: PLC0415

    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-census-"))
    try:
        _slugs, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        records = []
        for q in questions:
            # The three probes the instrument scores, built by the
            # runner's own helpers so the census reads the same strings
            # the recall runs do.
            probes = (
                ("asked", q["question"]),
                ("requery", q["requery"]),
                ("control", rr.strip_question_words(q["question"])),
            )
            for probe, text in probes:
                records.append(
                    {
                        "probe": probe,
                        "slug": q["slug"],
                        **question_census(memories, text),
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


def _longmemeval_records(
    limit: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Instrument B: the held-out conversational corpus."""
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
        root = Path(tempfile.mkdtemp(prefix="bm-census-"))
        try:
            lr.build_question_store(root, inst)
            memories = Store(root).load_all()
            records.append(
                {
                    "question_id": inst.get("question_id", ""),
                    "question_type": inst.get("question_type", ""),
                    **question_census(memories, inst["question"]),
                }
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return records, {
        "instrument": "longmemeval",
        "corpus": path.name,
        "corpus_sha256": lr.corpus_fingerprint(path),
        "instances": len(corpus),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Emitted-term df census. No recall.")
    p.add_argument("--instrument", choices=("retrieval", "longmemeval"), required=True)
    p.add_argument("--limit", type=int, default=None, help="First N instances (smoke).")
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Write the census here. A RELATIVE path resolves against this "
            "script's own directory (bench/), matching the other runners — "
            "so pass `longmemeval/results/x.json`, not "
            "`bench/longmemeval/results/x.json`, or give an absolute path."
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
        "coverage_gate": _RESCUE_COVERAGE_GATE,
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — emitted-term document frequencies. No recall, "
            "no labels, no ranking outcome. Declared in "
            "bench/longmemeval/PREREGISTRATION.md addendum 4 as the whole of "
            "what round 2 reads from the held-out corpus before it runs."
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
