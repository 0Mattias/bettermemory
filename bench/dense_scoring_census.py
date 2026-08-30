"""Dense scoring census: can store-trained geometry point at the right
DOCUMENT, on the pool lexical retrieval cannot reach?

The dense-scoring-census declaration fixes everything this
script computes — the 8-cell family, every pooling definition, the
pools, the reach bar, and the routing/parking criterion — and was
committed before this ran. This file is the mechanical half: it trains
the store model with `bench/embed_train.py`'s own pipeline at its
declared defaults, scores documents by pooled cosine, and tabulates
gold ranks. No fusion, no weights, no engine path.

    .venv/bin/python bench/dense_scoring_census.py \\
        --out retrieval/results/dense-scoring-census-YYYY-MM-DD.json

Statistics only. Dev-side plus the declared 20-question LongMemEval
glance; no file under bench/heldout/ is read.
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

import bettermemory.search as engine  # noqa: E402
from bettermemory.store import Store  # noqa: E402

import embed_train  # noqa: E402
from embed_census import Model  # noqa: E402
from embed_hybrid import bridge, ngrams  # noqa: E402
from embed_train import load_bench_module  # noqa: E402

POOLINGS = ("mean", "idf")
POSTPROCS = ("raw", "centred")
BRIDGINGS = (False, True)
PRIMARY_CELL = "mean_centred_bridge"
REACH_RANK = 10
R1_MIN_REACHED = 5
LME_SLICE = 20

DEV_LABELS = _HERE / "retrieval" / "results" / "base-leg-labels-2026-08-12.json"


def _cell_name(pooling: str, postproc: str, bridging: bool) -> str:
    return f"{pooling}_{postproc}_{'bridge' if bridging else 'nobridge'}"


def _query_tokens(text: str) -> list[str]:
    raw = engine._expand_kebab(engine.tokenize(text))
    return sorted(set(engine._strip_stopwords(raw)))


def _doc_tokens(memory: Any) -> set[str]:
    return set(engine._memory_tokens(memory).content)


def _pool_vector(
    tokens: list[str],
    model: Model,
    *,
    weights: dict[str, float] | None,
    default_weight: float = 1.0,
    gram_index: dict[str, frozenset[str]] | None,
) -> tuple[list[float] | None, int, int]:
    """(pooled unit vector | None, tokens bridged, tokens dropped).

    `weights` is the idf map (None = uniform); `default_weight` is the
    declaration's df-clamped-at-1 value ln(N) for tokens in no document
    (a bridged query token can be one); `gram_index` non-None turns the
    query-side bridge on. Tokens with non-positive weight contribute
    nothing, per the declaration.
    """
    dim = len(next(iter(model.vec.values())))
    acc = [0.0] * dim
    total = 0.0
    bridged = dropped = 0
    for token in tokens:
        vec = model.vec.get(token)
        if vec is None and gram_index is not None:
            vec = bridge(token, model, gram_index)
            if vec is not None:
                bridged += 1
        if vec is None:
            dropped += 1
            continue
        weight = 1.0 if weights is None else weights.get(token, default_weight)
        if weight <= 0.0:
            dropped += 1
            continue
        for d in range(dim):
            acc[d] += weight * vec[d]
        total += weight
    if total <= 0.0:
        return None, bridged, dropped
    norm = math.sqrt(sum(v * v for v in acc))
    if norm <= 0.0:
        return None, bridged, dropped
    return [v / norm for v in acc], bridged, dropped


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _idf_weights(doc_token_sets: list[set[str]]) -> dict[str, float]:
    n = len(doc_token_sets)
    df: dict[str, int] = {}
    for tokens in doc_token_sets:
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    return {t: math.log(n / max(d, 1)) for t, d in df.items()}


def _stratum(rank_0: int | None) -> str:
    if rank_0 is None:
        return "absent"
    if rank_0 == 0:
        return "hit@1"
    if rank_0 <= 4:
        return "near(2-5)"
    if rank_0 <= 9:
        return "mid(6-10)"
    return "far(11+)"


def _quartiles(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p25": ordered[len(ordered) // 4],
        "median": ordered[len(ordered) // 2],
        "p75": ordered[(3 * len(ordered)) // 4],
    }


# ---------------------------------------------------------------------------
# Dev instrument
# ---------------------------------------------------------------------------


def _dev_pools() -> dict[tuple[str, str], str]:
    """(slug, probe) -> shipped stratum, unpadded, from the labels artifact."""
    labels = json.loads(DEV_LABELS.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], str] = {}
    for record in labels["arms"]["unpadded"]["records"]:
        rank = record["legs"]["keyword"]["gold_rank_with_leg"]
        out[(record["slug"], record["probe"])] = _stratum(rank)
    return out


def dev_census(rr: Any, payload_vectors: dict[str, Any]) -> dict[str, Any]:
    questions = rr._read_jsonl(rr.QUESTIONS)
    corpus_rows = rr._read_jsonl(rr.CORPUS)
    row_index = {row["slug"]: i for i, row in enumerate(corpus_rows)}
    strata = _dev_pools()

    root = Path(tempfile.mkdtemp(prefix="bm-densecensus-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    id_to_slug = {v: k for k, v in slug_to_id.items()}
    docs = sorted(
        ((row_index[id_to_slug[m.id]], m) for m in memories), key=lambda x: x[0]
    )
    doc_token_sets = [_doc_tokens(m) for _, m in docs]
    idf = _idf_weights(doc_token_sets)

    vocab = payload_vectors["vocab"]
    vectors = {t: payload_vectors["vectors"][i] for i, t in enumerate(vocab)}
    models = {mode: Model(vocab, vectors, mode) for mode in POSTPROCS}
    gram_indexes = {
        mode: {t: ngrams(t) for t in models[mode].vocab} for mode in POSTPROCS
    }

    cells: dict[str, Any] = {}
    for pooling in POOLINGS:
        for postproc in POSTPROCS:
            for bridging in BRIDGINGS:
                model = models[postproc]
                weights = idf if pooling == "idf" else None
                clamped = math.log(len(doc_token_sets))
                doc_vecs: list[tuple[int, list[float]]] = []
                unpooled_docs = 0
                for i, tokens in enumerate(doc_token_sets):
                    vec, _, _ = _pool_vector(
                        sorted(tokens),
                        model,
                        weights=weights,
                        default_weight=clamped,
                        gram_index=None,
                    )
                    if vec is None:
                        unpooled_docs += 1
                    else:
                        doc_vecs.append((i, vec))

                probes: list[dict[str, Any]] = []
                for q in questions:
                    gold_row = row_index[q["slug"]]
                    for probe in ("asked", "requery", "control"):
                        query = rr._query_for(q, probe)
                        tokens = _query_tokens(query)
                        qvec, bridged, dropped = _pool_vector(
                            tokens,
                            model,
                            weights=weights,
                            default_weight=clamped,
                            gram_index=gram_indexes[postproc] if bridging else None,
                        )
                        if qvec is None:
                            rank = None
                        else:
                            scored = sorted(
                                ((-_dot(qvec, dv), i) for i, dv in doc_vecs),
                            )
                            rank = next(
                                (
                                    pos + 1
                                    for pos, (_, i) in enumerate(scored)
                                    if i == gold_row
                                ),
                                None,
                            )
                        probes.append(
                            {
                                "slug": q["slug"],
                                "probe": probe,
                                "stratum": strata[(q["slug"], probe)],
                                "dense_rank": rank,
                                "query_tokens": len(tokens),
                                "bridged": bridged,
                                "dropped": dropped,
                            }
                        )
                cells[_cell_name(pooling, postproc, bridging)] = {
                    "unpooled_docs": unpooled_docs,
                    "probes": probes,
                }

    return {
        "collection_size": size,
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "cells": cells,
    }


def summarise_dev(dev: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, cell in dev["cells"].items():
        far_absent = [
            p
            for p in cell["probes"]
            if p["probe"] in ("asked", "control")
            and p["stratum"] in ("far(11+)", "absent")
        ]
        hit1 = [
            p
            for p in cell["probes"]
            if p["probe"] in ("asked", "control") and p["stratum"] == "hit@1"
        ]
        reached = sum(
            1
            for p in far_absent
            if p["dense_rank"] is not None and p["dense_rank"] <= REACH_RANK
        )
        by_stratum: dict[str, Any] = {}
        for probe_class in ("asked", "control", "requery"):
            rows = [p for p in cell["probes"] if p["probe"] == probe_class]
            by_stratum[probe_class] = {
                stratum: _quartiles(
                    [
                        p["dense_rank"]
                        for p in rows
                        if p["stratum"] == stratum and p["dense_rank"] is not None
                    ]
                )
                for stratum in sorted({p["stratum"] for p in rows})
            }
        hit1_all = sorted(
            (p["dense_rank"] if p["dense_rank"] is not None else math.inf) for p in hit1
        )
        out[name] = {
            "far_absent_pool": len(far_absent),
            "far_absent_reached_at_10": reached,
            "far_absent_unreached_none": sum(
                1 for p in far_absent if p["dense_rank"] is None
            ),
            "hit1_ranks": _quartiles(
                [p["dense_rank"] for p in hit1 if p["dense_rank"] is not None]
            ),
            "hit1_unpooled": sum(1 for p in hit1 if p["dense_rank"] is None),
            # The declaration's R2 median: an unpooled gold is worse
            # than any rank, so it sits at +inf rather than dropping
            # out of the pool.
            "hit1_median_none_as_inf": (
                None
                if not hit1_all
                else (
                    "inf"
                    if math.isinf(hit1_all[len(hit1_all) // 2])
                    else hit1_all[len(hit1_all) // 2]
                )
            ),
            "unpooled_docs": cell["unpooled_docs"],
            "by_stratum": by_stratum,
        }
    return out


# ---------------------------------------------------------------------------
# LongMemEval glance
# ---------------------------------------------------------------------------


def _train_units(units: list[str]) -> tuple[list[str], dict[str, list[float]]]:
    """`embed_train.build`'s core over an arbitrary unit list, its
    constants and seed untouched."""
    streams = embed_train.token_streams(units)
    vocab, index = embed_train.build_vocab(streams)
    counts = embed_train.cooccurrence(streams, index)
    counts = {k: v for k, v in counts.items() if v >= embed_train.MIN_COOC}
    if not vocab or not counts:
        return [], {}
    vectors, _losses = embed_train.train(counts, len(vocab))
    return vocab, {t: vectors[i] for i, t in enumerate(vocab)}


def lme_glance(lr: Any, progress: bool) -> dict[str, Any]:
    corpus = json.loads(lr.DEFAULT_CORPUS.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    started = time.time()
    for i, inst in enumerate(corpus[:LME_SLICE]):
        root = Path(tempfile.mkdtemp(prefix="bm-denselme-"))
        try:
            id_to_session, n_items = lr.build_question_store(root, inst)
            memories = Store(root).load_all()
        finally:
            shutil.rmtree(root, ignore_errors=True)

        vocab, vectors = _train_units([m.body for m in memories])
        if not vocab:
            rows.append(
                {
                    "qid": inst["question_id"],
                    "type": inst.get("question_type", "unknown"),
                    "trained": False,
                }
            )
            continue
        model = Model(vocab, vectors, "centred")
        gram_index = {t: ngrams(t) for t in model.vocab}

        item_vecs: list[tuple[str, str, list[float]]] = []
        for m in memories:
            vec, _, _ = _pool_vector(
                sorted(_doc_tokens(m)), model, weights=None, gram_index=None
            )
            if vec is not None:
                digest = hashlib.sha256(m.body.encode("utf-8")).hexdigest()
                item_vecs.append((m.id, digest, vec))

        tokens = _query_tokens(inst["question"])
        qvec, bridged, dropped = _pool_vector(
            tokens, model, weights=None, gram_index=gram_index
        )
        evidence = list(dict.fromkeys(inst["answer_session_ids"]))
        if qvec is None:
            ranks: list[int | None] = [None for _ in evidence]
        else:
            scored = sorted(
                ((-_dot(qvec, vec), digest, mid) for mid, digest, vec in item_vecs)
            )
            session_rank: dict[str, int] = {}
            for _, _, mid in scored:
                sid = id_to_session.get(mid)
                if sid is not None and sid not in session_rank:
                    session_rank[sid] = len(session_rank)
            ranks = [
                session_rank[sid] + 1 if sid in session_rank else None
                for sid in evidence
            ]
        rows.append(
            {
                "qid": inst["question_id"],
                "type": inst.get("question_type", "unknown"),
                "trained": True,
                "vocab": len(vocab),
                "items": n_items,
                "items_pooled": len(item_vecs),
                "query_bridged": bridged,
                "query_dropped": dropped,
                "n_evidence": len(evidence),
                "evidence_dense_ranks": ranks,
            }
        )
        if progress:
            rate = (i + 1) / max(1e-9, time.time() - started)
            print(f"  [lme] {i + 1}/{LME_SLICE} ({rate:.2f} q/s)", file=sys.stderr)
    return {
        "corpus": lr.DEFAULT_CORPUS.name,
        "corpus_sha256": lr.corpus_fingerprint(lr.DEFAULT_CORPUS),
        "slice": LME_SLICE,
        "cell": PRIMARY_CELL,
        "questions": rows,
    }


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def readiness(summary: dict[str, Any]) -> dict[str, Any]:
    primary = summary[PRIMARY_CELL]
    r1 = primary["far_absent_reached_at_10"] >= R1_MIN_REACHED
    median = primary["hit1_median_none_as_inf"]
    r2 = median is not None and median != "inf" and median <= REACH_RANK
    family_reach = {
        name: cell["far_absent_reached_at_10"] for name, cell in summary.items()
    }
    any_cell_reaches = any(v >= R1_MIN_REACHED for v in family_reach.values())
    return {
        "primary_cell": PRIMARY_CELL,
        "R1_reach": r1,
        "R2_preservation": r2,
        "family_far_absent_reached": family_reach,
        "prereg_licensed": r1,
        "routing": (
            "leg or rerank-window"
            if r1 and r2
            else "rerank-window only"
            if r1
            else None
        ),
        "parked": not r1 and not any_cell_reaches,
        "anti_gate_shopping_fired": not r1 and any_cell_reaches,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Dense scoring census. Statistics only.")
    p.add_argument("--out", default=None, metavar="PATH")
    p.add_argument("--progress", action="store_true")
    args = p.parse_args()

    rr = load_bench_module("dense_dev_run", _HERE / "retrieval" / "run.py")
    lr = load_bench_module("dense_lme_run", _HERE / "longmemeval" / "run.py")

    if args.progress:
        print("training store model...", file=sys.stderr)
    model_payload = embed_train.build(
        "store", dim=embed_train.DIM, epochs=embed_train.EPOCHS
    )

    dev = dev_census(rr, model_payload)
    summary = summarise_dev(dev)
    glance = lme_glance(lr, args.progress)

    payload = {
        "provenance": rr._provenance(),
        "declaration": "bench/DENSE_SCORING_CENSUS_DECLARATION.md",
        "model": {
            "corpus": model_payload["corpus"],
            "trainer_sha256": model_payload["trainer_sha256"],
            "parameters": model_payload["parameters"],
            "corpus_stats": model_payload["corpus_stats"],
            "corpus_manifest_sha256": model_payload["corpus_manifest_sha256"],
            "final_loss": model_payload["final_loss"],
        },
        "grid": {
            "pooling": list(POOLINGS),
            "postproc": list(POSTPROCS),
            "bridging": ["off", "on"],
        },
        "reach_rank": REACH_RANK,
        "note": (
            "STATISTICS ONLY — gold document ranks under pooled "
            "query-document cosine from a store-trained model, over the "
            "declared 8-cell family. Pools read off "
            "base-leg-labels-2026-08-12.json. No fusion, no ranking "
            "change, no engine integration."
        ),
        "dev": dev,
        "summary": summary,
        "lme_glance": glance,
        "readiness": readiness(summary),
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
