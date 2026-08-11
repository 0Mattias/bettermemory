"""Do from-scratch dense vectors emit expansion terms the gold actually contains?

P1a's gate is the standard this answers to, unchanged and restated so it
cannot drift: **a replacement expansion source must be at least as
precise as the committed tables it would replace** — 1.0x, measured
identically on both sides as the fraction of emitted terms appearing in
the gold document. `_hybrid_fuse` fuses by RANK, so a leg contributes
`_RESCUE_LEG_WEIGHT / (rrf_k + rank)` however thin its evidence is
(round 5's C5); the architecture has no way to discount an imprecise
leg; so a less precise source cannot help whatever its recall. Raw
store PPMI reached 0.46x of that bar and the line was killed there.

P1e asks whether a *trained* dense factorization does better than the
raw counts did — legal under the owner's 2026-08-11 doctrine update
because `bench/embed_train.py` builds the model from scratch, from
committed text, with no third-party weights and no network.

**Statistics only.** No ranking change, no threshold applied, no engine
code, no preregistration. A grid is swept and reported. Dev-side by
construction since it needs gold labels: `bench/heldout/` is NOT read.

Two readings are reported for every cell and neither is hidden:

- **precision** — the bar's own quantity, emitted terms that appear in
  the gold document over emitted terms;
- **new hits** — gold terms the static tables do not already emit,
  which is the only thing a replacement source can add.

Every choice that could flatter the mechanism is made in its favour and
said out loud, because a kill argued from a hobbled arm is worthless:
candidates are restricted to terms the collection actually contains (a
term absent from the store matches nothing, so charging the denominator
for it would understate the source), the filler/length/query-token
exclusions are applied BEFORE the top-k cut so k buys k usable terms,
and both raw and mean-centred vector readings are swept.

One instrument per run, each landing in its own directory's `results/`,
exactly as `bench/df_census.py` does it:

    .venv/bin/python bench/embed_census.py \\
        --vectors store=/tmp/store.json --vectors repo=/tmp/repo.json \\
        --instrument retrieval \\
        --out retrieval/results/embed-census-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402
from embed_train import (  # noqa: E402
    load,
    load_bench_module,
    mean_centre,
    unit_normalise,
)

# The P1a standard, quoted from bench/longmemeval/README.md's P1a table
# so this census is scored against a published number rather than one
# recomputed here and quietly allowed to move. The retrieval instrument's
# incumbent row is ALSO recomputed below, and the two are asserted equal
# — a mismatch means the token pipeline drifted and the comparison is
# void, which is worth catching loudly rather than reporting a ratio
# against a stale bar.
P1A_INCUMBENT_PRECISION = 0.2743
P1A_INCUMBENT_TERMS_PER_PROBE = 5.65
GATE_MULTIPLE = 1.0

TOP_K_GRID = (1, 2, 3, 5, 8)
# The similarity floor runs to 0.999 deliberately, and the top of it is
# not padding. Trained vectors on corpora this size sit at high mutual
# cosine: the first sweep stopped at 0.7 and every cell below it was
# byte-identical, so the grid never let the mechanism trade width for
# precision. The second stopped at 0.98 with precision still climbing,
# which would have made the grid's edge the finding. The floor now runs
# until the emitted set collapses, so "parity is only reachable by
# emitting almost nothing" is a measurement rather than an assumption.
TAU_GRID = (0.0, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 0.999)
POSTPROC_GRID = ("raw", "centred")

# LongMemEval instances scored. Disjoint from `embed_train.LME_TRAIN_SLICE`
# by construction, so the conversational arm is never asked about a store
# whose text it trained on.
LME_SCORED = 20

# Smallest emitted-term count a grid cell may rest on and still be read
# as a verdict. A conventional small-sample floor, fixed here rather
# than chosen after seeing which cells it excludes: the tightest cells
# in this grid emit a handful of terms, and a 2-of-4 cell would
# otherwise report 0.5 precision and "pass" a bar the mechanism never
# reached. It also decides when an instrument can carry the gate at all
# — see `verdict`.
MIN_GATE_TERMS = 30

_MIN_TERM_LEN = 3


def query_tokens_of(query: str) -> list[str]:
    """The token list `search()` would rank with — `df_census`'s route.

    Copied in behaviour, not in spirit: the ppmi census and the df
    census both take `_strip_stopwords(_expand_kebab(tokenize(q)))`, and
    a census that tokenized differently would not be comparable to the
    bar it is scored against.
    """
    return engine._strip_stopwords(engine._expand_kebab(engine.tokenize(query)))


def static_terms(query_tokens: list[str]) -> set[str]:
    """What the incumbent committed tables emit for this probe."""
    if not query_tokens:
        return set()
    return set(
        engine._expansion_terms_impl(
            list(dict.fromkeys(query_tokens)),
            engine._EXPANSION_TABLES,
            engine._stem_token,
        )
    )


def collection_vocabulary(memories: list[Memory]) -> set[str]:
    """Every content token the collection contains.

    The candidate pool. An expansion term outside it cannot match any
    document in this store, so emitting it is a no-op — including such
    terms in the denominator would charge the mechanism for terms that
    cost nothing. The incumbent tables are NOT restricted this way, so
    the comparison runs in the challenger's favour.
    """
    out: set[str] = set()
    for memory in memories:
        out.update(engine._memory_tokens(memory).content)
    return out


class Model:
    """One trained corpus, in one post-processing reading."""

    def __init__(self, vocab: list[str], vectors: dict[str, list[float]], mode: str):
        raw = [vectors[t] for t in vocab]
        prepared = unit_normalise(mean_centre(raw) if mode == "centred" else raw)
        self.mode = mode
        self.vocab = vocab
        self.vec = {t: prepared[i] for i, t in enumerate(vocab)}

    def ranked(self, token: str, candidates: list[str]) -> list[tuple[float, str]]:
        """`candidates` by descending cosine against `token`.

        Ties break on the term string so the same model and the same
        store always emit the same list.
        """
        source = self.vec.get(token)
        if source is None:
            return []
        out = []
        for term in candidates:
            other = self.vec[term]
            out.append((sum(a * b for a, b in zip(source, other)), term))
        out.sort(key=lambda x: (-x[0], x[1]))
        return out


def candidate_pool(
    collection: set[str], vocab_set: set[str], query_tokens: set[str]
) -> list[str]:
    """Terms this probe is allowed to emit, before the top-k cut.

    The 5.1.1 disjointness invariant, applied exactly as
    `ppmi_census.derive` applies it to a store-derived source: the
    caller's own tokens are out, filler stems are out, and terms under
    the length floor are out. Applied HERE rather than after the cut so
    a budget of k buys k usable terms — the generous reading.
    """
    pool = collection & vocab_set
    pool -= query_tokens
    pool -= engine._EXPANSION_TABLES.filler_stems
    return sorted(t for t in pool if len(t) >= _MIN_TERM_LEN)


def probe_record(
    model: Model,
    query: str,
    gold_terms: set[str],
    collection: set[str],
    vocab_set: set[str],
) -> dict[str, Any]:
    """One probe against one model reading, swept across the grid."""
    tokens = query_tokens_of(query)
    unique = list(dict.fromkeys(tokens))
    static = static_terms(tokens)
    in_vocab = [t for t in unique if t in vocab_set]
    pool = candidate_pool(collection, vocab_set, set(tokens))

    # One ranked list per in-vocabulary query token; the whole grid is
    # then slices of it, which is what keeps a 40-cell sweep affordable.
    ranked = {t: model.ranked(t, pool) for t in in_vocab}

    rec: dict[str, Any] = {
        "query_tokens": len(unique),
        "query_tokens_in_vocab": len(in_vocab),
        "candidate_pool": len(pool),
        "static_terms": len(static),
        "static_hits": len(static & gold_terms),
        "grid": {},
    }
    for tau in TAU_GRID:
        kept = {t: [(s, w) for s, w in lst if s >= tau] for t, lst in ranked.items()}
        for top_k in TOP_K_GRID:
            terms: set[str] = set()
            for lst in kept.values():
                terms.update(w for _s, w in lst[:top_k])
            rec["grid"][f"k{top_k}_t{tau:g}"] = {
                "terms": len(terms),
                "hits": len(terms & gold_terms),
                "new_hits": len((terms - static) & gold_terms),
            }
    return rec


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Reported on every precision in this census because the tightest
    cells are the interesting ones and they are also the smallest: the
    grid's best retrieval cell rests on ~120 emitted terms, where a
    point estimate cannot separate 0.19 from 0.35. Wilson rather than
    normal-approximation because these proportions sit near 0.2 with
    small n, which is exactly where the normal interval misbehaves.

    An interval does not soften the bar — the gate is still a point
    comparison, and addendum 8 fixed it that way. It stops the RECORD
    from over-reading a number in either direction.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = hits / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    spread = (
        z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    )
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def two_proportion_p(hits_a: int, n_a: int, hits_b: int, n_b: int) -> float:
    """Two-sided p for `p_a == p_b`, pooled normal approximation.

    P1a did not need this: raw PPMI came in at 0.46x on hundreds of
    terms, a gap no sample size argument could rescue. This grid's cells
    land within a few points of the incumbent on ~200 terms, where the
    difference between "misses the bar" and "cannot be told apart from
    the bar" is the entire finding. Reporting the ratio without it would
    let a 0.79x read as a kill when the data does not support one.
    """
    if n_a <= 0 or n_b <= 0:
        return 1.0
    pooled = (hits_a + hits_b) / (n_a + n_b)
    if pooled in (0.0, 1.0):
        return 1.0
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b))
    if se == 0.0:
        return 1.0
    z = (hits_a / n_a - hits_b / n_b) / se
    return math.erfc(abs(z) / math.sqrt(2.0))


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cell totals plus the incumbent row the bar is a ratio of."""
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
            "probes_with_a_new_hit": sum(1 for c in cells if c["new_hits"]),
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
        "incumbent_precision_ci95": [round(inc_lo, 4), round(inc_hi, 4)],
        "incumbent_terms_total": static_terms_total,
        "incumbent_hits_total": static_hits_total,
        "query_tokens_total": sum(r["query_tokens"] for r in records),
        "query_tokens_in_vocab": sum(r["query_tokens_in_vocab"] for r in records),
        "vocabulary_coverage": (
            round(
                sum(r["query_tokens_in_vocab"] for r in records)
                / sum(r["query_tokens"] for r in records),
                4,
            )
            if sum(r["query_tokens"] for r in records)
            else 0.0
        ),
        "incumbent_terms_per_probe": round(static_terms_total / n, 2) if n else 0.0,
        "incumbent_precision": incumbent,
        "grid": grid,
    }


def verdict(summary: dict[str, Any]) -> dict[str, Any]:
    """The gate, plus the two cells a reader would otherwise pick by eye.

    Addendum 8's bar is quoted unchanged — *the best grid cell reaches
    >= 1.0x the static tables' precision* — because a census that
    tightened the gate it was scored against would be answering a
    question nobody asked. What is added is arithmetic hygiene the P1a
    grid never needed: its cells all emitted 10-65 terms per probe, so
    no cell could win on a handful of terms, and this grid's tightest
    cells can.

    - `MIN_GATE_TERMS` keeps a cell resting on a dozen terms from
      carrying a verdict. It is a small-sample floor, not a threshold on
      the mechanism.
    - `gate_applicable` is false when the INCUMBENT row is itself under
      that floor. On a corpus where the committed tables barely fire,
      their precision is not a bar; saying so is more useful than
      publishing a ratio against five hits.
    - `best_at_or_above_budget` answers the replacement question
      directly: at the incumbent's own width or wider, how precise is
      the challenger? A source that matches on precision while emitting
      half as many terms is narrower, not better.
    """
    grid = summary["grid"]
    if not grid:
        return {"verdict": "EMPTY"}
    target = summary["incumbent_terms_per_probe"]
    eligible = {k: v for k, v in grid.items() if v["terms_total"] >= MIN_GATE_TERMS}
    at_budget = {k: v for k, v in eligible.items() if v["terms_per_probe"] >= target}
    applicable = summary["incumbent_terms_total"] >= MIN_GATE_TERMS

    def pick(cells: dict[str, Any]) -> str | None:
        if not cells:
            return None
        return max(
            cells, key=lambda k: (cells[k]["precision"], -cells[k]["terms_total"])
        )

    best_key = pick(eligible)
    budget_key = pick(at_budget)
    matched_key = min(grid, key=lambda k: (abs(grid[k]["terms_per_probe"] - target), k))
    out: dict[str, Any] = {
        "bar": (
            f"best cell precision >= {GATE_MULTIPLE}x the committed tables' "
            f"{summary['incumbent_precision']}"
        ),
        "min_gate_terms": MIN_GATE_TERMS,
        "gate_applicable": applicable,
        "best_cell": best_key,
        "best_precision": grid[best_key]["precision"] if best_key else 0.0,
        "best_precision_ci95": grid[best_key]["precision_ci95"] if best_key else None,
        "best_terms_per_probe": grid[best_key]["terms_per_probe"] if best_key else 0.0,
        "best_gate_multiple": grid[best_key]["gate_multiple"] if best_key else 0.0,
        "best_at_or_above_budget_cell": budget_key,
        "best_at_or_above_budget": grid[budget_key] if budget_key else None,
        "matched_budget_cell": matched_key,
        "matched_budget": grid[matched_key],
    }
    out["passes"] = bool(
        applicable and best_key and grid[best_key]["gate_multiple"] >= GATE_MULTIPLE
    )
    return out


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


def _retrieval_probes() -> tuple[
    list[tuple[str, str, str, set[str]]], list[Memory], dict[str, Any]
]:
    """The blind-authored gold set: (slug, probe, text, gold terms).

    The same two probes the PPMI census swept — as-asked and control.
    `requery` is excluded for the same reason it was there: it is the
    query a user does NOT type, and the rescue lane never sees it.
    """
    rr = load_bench_module("embed_census_ret", _HERE / "retrieval" / "run.py")
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-embed-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
        by_id = {m.id: m for m in memories}
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
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return (
        probes,
        memories,
        {
            "instrument": "retrieval",
            "corpus": rr.CORPUS.name,
            "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
            "collection_size": size,
        },
    )


def _lme_stores() -> tuple[
    list[tuple[str, str, set[str], list[Memory]]], dict[str, Any]
]:
    """LongMemEval's scored slice, one store per question.

    Gold terms are pooled over every memory belonging to an evidence
    session, which is the question's answer set as `run.py` defines it.
    Each question has its OWN collection, so the candidate pool and the
    incumbent row are both per-question — unlike the retrieval
    instrument, where one store serves every probe.
    """
    lr = load_bench_module("embed_census_lme", _HERE / "longmemeval" / "run.py")
    path = lr.DEFAULT_CORPUS
    corpus = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for inst in corpus[:LME_SCORED]:
        if not inst["answer_session_ids"]:
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-embed-lme-"))
        try:
            id_to_session, _n = lr.build_question_store(root, inst)
            memories = Store(root).load_all()
            evidence = set(inst["answer_session_ids"])
            gold_terms: set[str] = set()
            for memory in memories:
                if id_to_session.get(memory.id) in evidence:
                    gold_terms.update(engine._memory_tokens(memory).content)
            out.append(
                (inst.get("question_id", ""), inst["question"], gold_terms, memories)
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return out, {
        "instrument": "longmemeval",
        "corpus": path.name,
        "corpus_sha256": lr.corpus_fingerprint(path),
        "questions_scored": len(out),
        "train_slice_disjoint": True,
    }


def compact_rows(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Per-probe scalars, without the swept grid.

    The full per-probe grid is 8 arm-readings x 55 cells x 40 probes and
    would put this artifact several times over the repository's 500 kB
    added-file cap, so it is NOT persisted — the script is committed and
    deterministic, and re-running it reproduces every cell. What IS
    persisted is everything the published table's non-grid columns rest
    on: coverage, the incumbent row, and the candidate pool each probe
    was allowed to draw from.

    Stored once per corpus arm rather than once per arm-reading: every
    field here is a function of the arm's VOCABULARY, which `raw` and
    `centred` share, so keying them per reading stored each row twice.
    """
    return [
        {
            key: r[key],
            "probe": r["probe"],
            "query_tokens": r["query_tokens"],
            "query_tokens_in_vocab": r["query_tokens_in_vocab"],
            "candidate_pool": r["candidate_pool"],
            "static_terms": r["static_terms"],
            "static_hits": r["static_hits"],
        }
        for r in records
    ]


def source_manifest(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-source-entry provenance, digested rather than enumerated.

    The `repo` corpus is 255 files and a row each put this artifact 190
    kB over the added-file cap on provenance alone. `manifest_sha256`
    is a sha256 over the sorted `path sha256` lines, so a reader can
    still prove which bytes trained the model — they recompute the
    digest from the checkout — while the licence, the file count and the
    total size stay legible in the artifact itself.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in sources:
        grouped.setdefault((row["kind"], row["licence"]), []).append(row)
    out = []
    for (kind, licence), rows in sorted(grouped.items()):
        lines = "\n".join(
            f"{r['path']} {r['sha256']}" for r in sorted(rows, key=lambda r: r["path"])
        )
        out.append(
            {
                "kind": kind,
                "licence": licence,
                "files": len(rows),
                "bytes": sum(r["bytes"] for r in rows),
                "units": sum(r["units"] for r in rows),
                "manifest_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
            }
        )
    return out


def census_retrieval(models: dict[str, dict[str, Model]]) -> dict[str, Any]:
    probes, memories, meta = _retrieval_probes()
    collection = collection_vocabulary(memories)
    arms: dict[str, Any] = {}
    rows: dict[str, Any] = {}
    for arm, readings in models.items():
        for mode, model in readings.items():
            vocab_set = set(model.vocab)
            records = [
                {
                    "slug": slug,
                    "probe": probe,
                    **probe_record(model, text, gold, collection, vocab_set),
                }
                for slug, probe, text, gold in probes
            ]
            summary = summarise(records)
            arms[f"{arm}/{mode}"] = {
                "summary": summary,
                "verdict": verdict(summary),
            }
            rows.setdefault(arm, compact_rows(records, "slug"))
    return {
        **meta,
        "collection_vocabulary": len(collection),
        "arms": arms,
        "probes_by_arm": rows,
    }


def census_longmemeval(models: dict[str, dict[str, Model]]) -> dict[str, Any]:
    stores, meta = _lme_stores()
    prepared = [
        (qid, question, gold, collection_vocabulary(memories))
        for qid, question, gold, memories in stores
    ]
    arms: dict[str, Any] = {}
    rows: dict[str, Any] = {}
    for arm, readings in models.items():
        for mode, model in readings.items():
            vocab_set = set(model.vocab)
            records = [
                {
                    "qid": qid,
                    "probe": "asked",
                    **probe_record(model, question, gold, collection, vocab_set),
                }
                for qid, question, gold, collection in prepared
            ]
            summary = summarise(records)
            arms[f"{arm}/{mode}"] = {
                "summary": summary,
                "verdict": verdict(summary),
            }
            rows.setdefault(arm, compact_rows(records, "qid"))
    return {**meta, "arms": arms, "probes_by_arm": rows}


def main() -> int:
    p = argparse.ArgumentParser(
        description="P1e feasibility census. Statistics only; no engine code."
    )
    p.add_argument(
        "--vectors",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="A trained artifact from bench/embed_train.py. Repeatable.",
    )
    # One instrument per artifact, the way `bench/df_census.py` does it:
    # each instrument's census lands in its own directory's `results/`,
    # and a combined file would sit over the repository's added-file cap.
    p.add_argument("--instrument", choices=("retrieval", "longmemeval"), required=True)
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    started = time.time()
    models: dict[str, dict[str, Model]] = {}
    provenance: dict[str, Any] = {}
    for spec in args.vectors:
        name, _, raw_path = spec.partition("=")
        path = Path(raw_path).expanduser()
        vocab, vectors, meta = load(path)
        models[name] = {m: Model(vocab, vectors, m) for m in POSTPROC_GRID}
        provenance[name] = {
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "trainer_sha256": meta["trainer_sha256"],
            "corpus": meta["corpus"],
            "parameters": meta["parameters"],
            "corpus_stats": meta["corpus_stats"],
            "final_loss": meta["final_loss"],
            "sources": source_manifest(meta["sources"]),
        }

    payload: dict[str, Any] = {
        "kind": "bettermemory-p1e-census",
        "p1a_standard": {
            "incumbent_precision": P1A_INCUMBENT_PRECISION,
            "incumbent_terms_per_probe": P1A_INCUMBENT_TERMS_PER_PROBE,
            "gate_multiple": GATE_MULTIPLE,
            "published_in": "bench/longmemeval/README.md, P1a section",
        },
        "grid": {
            "top_k": list(TOP_K_GRID),
            "tau": list(TAU_GRID),
            "postproc": list(POSTPROC_GRID),
        },
        "note": (
            "STATISTICS ONLY — emitted-term precision against the gold "
            "document. No ranking change, no threshold applied, no engine "
            "integration, no preregistration. Dev-side; bench/heldout is "
            "not read."
        ),
        "models": provenance,
    }
    if args.instrument == "retrieval":
        payload["retrieval"] = census_retrieval(models)
        any_arm = next(iter(payload["retrieval"]["arms"].values()))
        got = any_arm["summary"]["incumbent_precision"]
        if abs(got - P1A_INCUMBENT_PRECISION) > 0.0001:
            raise SystemExit(
                f"incumbent precision recomputed as {got}, but P1a published "
                f"{P1A_INCUMBENT_PRECISION} — the token pipeline moved and this "
                "comparison is void until that is explained"
            )
    else:
        payload["longmemeval"] = census_longmemeval(models)
    payload["seconds"] = round(time.time() - started, 1)

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
