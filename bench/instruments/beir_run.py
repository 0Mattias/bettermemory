"""Score an encoder artifact on a pinned BEIR dataset — nDCG@10, recall@100, MRR@10.

The read: encode every corpus document (title + text) and every query
with the artifact under test, rank by cosine, score against the
dataset's test qrels with an own scoring implementation. Ties break by
document id, so a read is a pure function of the artifact and the
pinned bytes.

Backends: ``w2`` drives any committed W2-recipe run directory through
``W2Encoder`` (the same forward the dev instrument uses). Further
backends join as their artifacts exist; the engine's lexical arm is an
open seam, not scored here.

Every result records the dataset zip sha from PINS.json, the weights
sha of the artifact, and the full per-query rank evidence for the
tabled metrics.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "w"))

PINS_PATH = _HERE / "PINS.json"
DATA_DIR = _HERE / "data"


def _load_corpus(path: Path) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    texts: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            ids.append(str(row["_id"]))
            title = row.get("title") or ""
            body = row.get("text") or ""
            texts.append(f"{title}\n{body}".strip())
    return ids, texts


def _load_queries(path: Path) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    texts: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            ids.append(str(row["_id"]))
            texts.append(row["text"])
    return ids, texts


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = next(reader)
        assert header[0].startswith("query"), f"unexpected qrels header: {header}"
        for qid, did, score in reader:
            qrels.setdefault(qid, {})[did] = int(score)
    return qrels


def _dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_query(
    ranked_ids: list[str], rels: dict[str, int], k_ndcg: int, k_recall: int
) -> dict[str, float]:
    gains = [rels.get(d, 0) for d in ranked_ids[:k_ndcg]]
    ideal = sorted(rels.values(), reverse=True)[:k_ndcg]
    ndcg = _dcg(gains) / _dcg(ideal) if any(ideal) else 0.0
    relevant = {d for d, g in rels.items() if g > 0}
    hit = len(relevant & set(ranked_ids[:k_recall]))
    recall = hit / len(relevant) if relevant else 0.0
    mrr = 0.0
    for i, d in enumerate(ranked_ids[:k_ndcg]):
        if rels.get(d, 0) > 0:
            mrr = 1.0 / (i + 1)
            break
    return {"ndcg": ndcg, "recall": recall, "mrr": mrr}


def _encoder(backend: str, run_dir: Path):
    if backend == "w2":
        from w2_train import W2Encoder

        return W2Encoder.load(run_dir)
    raise SystemExit(f"unknown backend: {backend}")


def run(args: argparse.Namespace) -> int:
    t0 = time.time()
    dataset_dir = DATA_DIR / args.dataset
    pins = json.loads(PINS_PATH.read_text())
    if args.dataset not in pins:
        raise SystemExit(f"{args.dataset} is not pinned; run beir_fetch.py --pin first")

    doc_ids, doc_texts = _load_corpus(dataset_dir / "corpus.jsonl")
    query_ids, query_texts = _load_queries(dataset_dir / "queries.jsonl")
    qrels = _load_qrels(dataset_dir / "qrels" / "test.tsv")
    scored_qids = [q for q in query_ids if q in qrels]

    encoder = _encoder(args.backend, Path(args.run))
    weights_sha = hashlib.sha256(
        (Path(args.run) / "weights.npy").read_bytes()
    ).hexdigest()

    print(
        f"{args.dataset}: {len(doc_ids)} docs, {len(scored_qids)} scored queries",
        flush=True,
    )
    doc_vecs = encoder.encode_texts(doc_texts)
    print(f"corpus encoded in {time.time() - t0:.1f}s", flush=True)
    query_vecs = encoder.encode_texts(
        [query_texts[query_ids.index(q)] for q in scored_qids]
    )

    order_ids = np.array(doc_ids)
    per_query: list[dict[str, object]] = []
    totals = {"ndcg": 0.0, "recall": 0.0, "mrr": 0.0}
    for qi, qid in enumerate(scored_qids):
        sims = doc_vecs @ query_vecs[qi]
        # ties break by document id: sort by (-sim, doc_id)
        top = np.argsort(sims)[::-1][: max(args.k_recall * 2, 200)]
        top = sorted(top, key=lambda i: (-float(sims[i]), str(order_ids[i])))
        ranked = [str(order_ids[i]) for i in top[: args.k_recall]]
        metrics = score_query(ranked, qrels[qid], args.k_ndcg, args.k_recall)
        for key, val in metrics.items():
            totals[key] += val
        per_query.append({"qid": qid, **metrics, "top10": ranked[:10]})

    n = len(scored_qids)
    summary = {
        "dataset": args.dataset,
        "zip_sha256": pins[args.dataset]["zip_sha256"],
        "backend": args.backend,
        "run": str(args.run),
        "weights_sha256": weights_sha,
        "queries": n,
        "docs": len(doc_ids),
        f"ndcg@{args.k_ndcg}": round(totals["ndcg"] / n, 4),
        f"recall@{args.k_recall}": round(totals["recall"] / n, 4),
        f"mrr@{args.k_ndcg}": round(totals["mrr"] / n, 4),
        "wall_s": round(time.time() - t0, 1),
    }
    artifact = {"summary": summary, "per_query": per_query}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=1) + "\n")
    print(json.dumps(summary, indent=1), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--backend", default="w2", choices=("w2",))
    parser.add_argument("--run", required=True, help="artifact run directory")
    parser.add_argument("--out", required=True, help="result JSON path")
    parser.add_argument("--k-ndcg", type=int, default=10)
    parser.add_argument("--k-recall", type=int, default=100)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
