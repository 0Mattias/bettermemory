"""An estimator designed for THIS regime, after the textbook one missed.

`bench/embed_census.py` ran the word2vec/GloVe family honestly and it
did not clear P1a's bar: 0.79x at the incumbent's term budget, 0.989x at
a budget half its width. It also diagnosed exactly why, and the two
findings are the design brief for this module:

1. **The only corpus that works is the store being ranked.** 23x more
   text raised query-token coverage from 68.6% to 91.4% and dropped
   precision from 0.79x to 0.35x. Off-domain text is worse than no
   text, so the estimator has to be excellent on ~35,000 tokens rather
   than adequate on millions.
2. **A third of query tokens have no vector at all.** At 35k tokens a
   `min_count` of 5 leaves 31.4% of the probes' tokens out of the
   vocabulary — and the campaign's own failure taxonomy says the
   missing ones are *morphological* ("splitting" against a body that
   says "split") and *clipped* ("creds" against "credential").

The textbook answer to (1) is more data, which this regime cannot have,
and to (2) is subword embeddings trained over a large corpus, which is
(1) again. So this file does neither. It invents two mechanisms sized
for a personal memory store, and measures them against the same bar
with the same discipline:

**A. The agreement rule — a count-dense hybrid.** P1a's raw PPMI reached
0.46x and the dense factorization reached 0.79x. They are different
estimators of the same structure — PPMI unbiased and noisy, the
factorization smoothed and biased — so the design hypothesis was that
their errors are largely independent, and that a term both rank highly
is far likelier to be a real associate than one either ranks highly
alone. The rule emits only the INTERSECTION of the two top-k lists,
buying precision with recall, which is the trade the bar rewards.

**That hypothesis is false, and this file is how we know.** Measured, it
is WORSE than the dense rule alone: 0.44x at the incumbent's width
against the plain model's 0.79x. The reason is visible in hindsight and
is the real result here — GloVe factorizes the very matrix PPMI reads,
so the two are not independent estimators at all. What they agree on is
the high-count pairs, and high-count pairs are the frequent, least
discriminating terms. Independence was the whole premise and the data
withdrew it. See the census record for the variant this diagnosis
points at, which is named and deliberately NOT tested here.

**B. N-gram bridging — coverage without a bigger corpus.** An
out-of-vocabulary query token is given a vector composed from the
in-vocabulary terms it shares character n-grams with, weighted by
overlap. No new training, no new parameters, no corpus: it reuses the
model already trained and exploits the one thing English morphology
guarantees — that 'splitting' and 'split' share most of their
characters. This is the morphology class the rescue lane already
attacks with hand-written rules, reached by a different route.

It does what it was built to do — coverage 0.686 to 0.796, 50 tokens
given vectors they had none for — and it does not move precision. Worth
recording as a mechanism that WORKS and is aimed at the wrong quantity:
the bar prices precision, and coverage is recall.

**Statistics only.** No ranking change, no engine code, no
preregistration. Dev-side; `bench/heldout/` is NOT read.

    .venv/bin/python bench/embed_hybrid.py --vectors store=/tmp/store.json \\
        --out retrieval/results/embed-hybrid-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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
    summarise,
    verdict,
)
from embed_train import load, load_bench_module  # noqa: E402

# The agreement rule's two dials. `SPARSE_K` is how deep the PPMI list
# is consulted and `DENSE_K` how deep the cosine list is; the emitted
# set is their intersection, cut to `TOP_K`. Both are swept rather than
# chosen — the census reports the grid and fixes nothing.
SPARSE_K_GRID = (5, 10, 20, 40)
DENSE_K_GRID = (5, 10, 20, 40)
TOP_K_GRID = (1, 2, 3, 5)

# PPMI settings inherited verbatim from P1a's census so the sparse half
# of the hybrid is the SAME estimator that was measured at 0.46x. A
# hybrid built on a differently-tuned PPMI would not be composing the
# two published results.
PPMI_MIN_DF = 2
PPMI_SHIFT = 1.0

# N-gram bridging. 3-5 characters with word-boundary markers, and a
# bridge is only built from the `BRIDGE_NEIGHBOURS` best-overlapping
# in-vocabulary terms — a token that resembles nothing gets no vector
# rather than a vector averaged over the whole lexicon.
NGRAM_MIN = 3
NGRAM_MAX = 5
BRIDGE_NEIGHBOURS = 5
BRIDGE_MIN_OVERLAP = 0.30


def ngrams(term: str) -> frozenset[str]:
    """Character n-grams of `term`, with boundary markers.

    The markers matter: without them 'split' and 'unsplittable' share
    every n-gram of 'split' and look equally related, while with them
    the prefix gram '<sp' only matches words that actually start that
    way. Morphology is mostly an affix phenomenon, so the boundaries
    carry much of the signal.
    """
    padded = f"<{term}>"
    out = set()
    for size in range(NGRAM_MIN, NGRAM_MAX + 1):
        for i in range(len(padded) - size + 1):
            out.add(padded[i : i + size])
    return frozenset(out)


def bridge(
    token: str, model: Model, gram_index: dict[str, frozenset[str]]
) -> list[float] | None:
    """A vector for an out-of-vocabulary token, composed from its kin.

    Overlap is Jaccard over n-gram sets; the `BRIDGE_NEIGHBOURS` best
    are averaged with the overlap as the weight, and the result is
    L2-normalised so it lives on the same sphere as a trained vector.

    Returns None when nothing clears `BRIDGE_MIN_OVERLAP` — the honest
    outcome for a token the store's vocabulary genuinely cannot reach,
    and one the census counts under coverage rather than hiding.
    """
    if token in model.vec:
        return model.vec[token]
    mine = ngrams(token)
    if not mine:
        return None
    scored: list[tuple[float, str]] = []
    for term, theirs in gram_index.items():
        union = len(mine | theirs)
        if not union:
            continue
        overlap = len(mine & theirs) / union
        if overlap >= BRIDGE_MIN_OVERLAP:
            scored.append((overlap, term))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen = scored[:BRIDGE_NEIGHBOURS]
    dim = len(next(iter(model.vec.values())))
    acc = [0.0] * dim
    for weight, term in chosen:
        vec = model.vec[term]
        for d in range(dim):
            acc[d] += weight * vec[d]
    norm = sum(v * v for v in acc) ** 0.5
    return [v / norm for v in acc] if norm > 0.0 else None


_PPMI = load_bench_module("embed_hybrid_ppmi", _HERE / "ppmi_census.py")


def sparse_ranked(
    docs: list[set[str]], token: str, pool: set[str], depth: int
) -> list[str]:
    """The PPMI associates of `token`, restricted to `pool`.

    P1a's `associates` verbatim — imported once at module scope, not
    reimplemented, so the sparse half of this hybrid is provably the
    estimator that census measured rather than a lookalike.
    """
    pairs = _PPMI.associates(
        docs, token, min_df=PPMI_MIN_DF, shift=PPMI_SHIFT, top_k=10**6
    )
    out = [term for term, _weight in pairs if term in pool]
    return out[:depth]


def dense_ranked(
    vector: list[float], model: Model, pool: list[str], depth: int
) -> list[str]:
    """The cosine neighbours of a vector, restricted to `pool`."""
    scored = [(sum(a * b for a, b in zip(vector, model.vec[t])), t) for t in pool]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _s, t in scored[:depth]]


def probe_record(
    model: Model,
    docs: list[set[str]],
    query: str,
    gold_terms: set[str],
    collection: set[str],
    gram_index: dict[str, frozenset[str]],
    *,
    bridging: bool,
) -> dict[str, Any]:
    """One probe under the agreement rule, swept across the grid."""
    tokens = query_tokens_of(query)
    unique = list(dict.fromkeys(tokens))
    static = static_terms(tokens)
    vocab_set = set(model.vocab)
    pool = candidate_pool(collection, vocab_set, set(tokens))
    pool_set = set(pool)

    vectors: dict[str, list[float]] = {}
    for token in unique:
        if token in model.vec:
            vectors[token] = model.vec[token]
        elif bridging:
            built = bridge(token, model, gram_index)
            if built is not None:
                vectors[token] = built

    deepest_sparse = max(SPARSE_K_GRID)
    deepest_dense = max(DENSE_K_GRID)
    sparse: dict[str, list[str]] = {}
    dense: dict[str, list[str]] = {}
    for token in unique:
        sparse[token] = sparse_ranked(docs, token, pool_set, deepest_sparse)
        if token in vectors:
            dense[token] = dense_ranked(vectors[token], model, pool, deepest_dense)

    rec: dict[str, Any] = {
        "query_tokens": len(unique),
        "query_tokens_in_vocab": len(vectors),
        "query_tokens_bridged": sum(1 for t in vectors if t not in model.vec),
        "candidate_pool": len(pool),
        "static_terms": len(static),
        "static_hits": len(static & gold_terms),
        "grid": {},
    }
    for s_k in SPARSE_K_GRID:
        for d_k in DENSE_K_GRID:
            agreed: dict[str, list[str]] = {}
            for token in unique:
                if token not in dense:
                    continue
                head = set(sparse[token][:s_k])
                agreed[token] = [t for t in dense[token][:d_k] if t in head]
            for top_k in TOP_K_GRID:
                terms: set[str] = set()
                for lst in agreed.values():
                    terms.update(lst[:top_k])
                rec["grid"][f"s{s_k}_d{d_k}_k{top_k}"] = {
                    "terms": len(terms),
                    "hits": len(terms & gold_terms),
                    "new_hits": len((terms - static) & gold_terms),
                }
    return rec


def run(model: Model, *, bridging: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rr = load_bench_module("embed_hybrid_ret", _HERE / "retrieval" / "run.py")
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-hybrid-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        by_id = {m.id: m for m in memories}
        docs = [set(engine._memory_tokens(m).content) for m in memories]
        collection = collection_vocabulary(memories)
        gram_index = {t: ngrams(t) for t in model.vocab}
        records = []
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
                        **probe_record(
                            model,
                            docs,
                            text,
                            gold_terms,
                            collection,
                            gram_index,
                            bridging=bridging,
                        ),
                    }
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return records, {
        "instrument": "retrieval",
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
        "collection_vocabulary": len(collection),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Agreement-rule + n-gram-bridging census. Statistics only."
    )
    p.add_argument("--vectors", required=True, metavar="NAME=PATH")
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    started = time.time()
    name, _, raw_path = args.vectors.partition("=")
    path = Path(raw_path).expanduser()
    vocab, vectors, meta = load(path)

    arms: dict[str, Any] = {}
    instrument: dict[str, Any] = {}
    for mode in ("raw", "centred"):
        model = Model(vocab, vectors, mode)
        for bridging in (False, True):
            records, instrument = run(model, bridging=bridging)
            summary = summarise(records)
            summary["query_tokens_bridged"] = sum(
                r["query_tokens_bridged"] for r in records
            )
            label = f"{mode}+bridge" if bridging else mode
            arms[label] = {"summary": summary, "verdict": verdict(summary)}

    payload = {
        "kind": "bettermemory-p1e-hybrid-census",
        "p1a_standard": {
            "incumbent_precision": P1A_INCUMBENT_PRECISION,
            "gate_multiple": GATE_MULTIPLE,
            "min_gate_terms": MIN_GATE_TERMS,
        },
        "mechanisms": {
            "agreement_rule": (
                "emit only terms ranked highly by BOTH shifted PPMI over the "
                "store and cosine over the trained vectors — two estimators "
                "of the same structure whose errors are largely independent"
            ),
            "ngram_bridging": (
                "an out-of-vocabulary query token borrows a vector from the "
                "in-vocabulary terms it shares character n-grams with, "
                f"Jaccard-weighted over the best {BRIDGE_NEIGHBOURS}"
            ),
        },
        "grid": {
            "sparse_k": list(SPARSE_K_GRID),
            "dense_k": list(DENSE_K_GRID),
            "top_k": list(TOP_K_GRID),
            "ppmi_min_df": PPMI_MIN_DF,
            "ppmi_shift": PPMI_SHIFT,
            "ngram_range": [NGRAM_MIN, NGRAM_MAX],
            "bridge_neighbours": BRIDGE_NEIGHBOURS,
            "bridge_min_overlap": BRIDGE_MIN_OVERLAP,
        },
        "note": (
            "STATISTICS ONLY — emitted-term precision against the gold "
            "document, scored against P1a's unchanged gate. No ranking "
            "change, no engine integration, no preregistration. Dev-side; "
            "bench/heldout is not read."
        ),
        "model": {
            "name": name,
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "trainer_sha256": meta["trainer_sha256"],
            "corpus": meta["corpus"],
            "parameters": meta["parameters"],
            "corpus_stats": meta["corpus_stats"],
            "sources": source_manifest(meta["sources"]),
        },
        **instrument,
        "arms": arms,
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
