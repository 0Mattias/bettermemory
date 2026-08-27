"""Addendum 15 — own-encoder rerank probes over the default arm's window.

Three passes, three venvs, one cache:

  cache  (.venv)      run the shipped lexical pipeline on one half and
                      persist, per question: the distinct-session ranking
                      at full depth plus the text of the top --k-cache
                      sessions. The ranking is byte-identical to run.py's
                      (same store build, same run_search call, same
                      distinct_sessions collapse).
  embed  (fastvenv    embed cached questions + session texts with one
          or          own-built encoder: 'w2' (numpy, W2 run-1) or 'f1'
          .eval-venv) (torch, stage-P ckpt-4000). Writes an .npz.
  score  (any venv    RRF-fuse the cosine reranking of the top-K window
          w/ numpy)   with the lexical ranks and rescore the half over
                      the declared (K, w) grid. Baseline recall is
                      recomputed from the same cache, so every delta is
                      paired on identical inputs.

The declared knobs (Addendum 15, memory-resident) are K in {25, 50} and
w in {0.5, 1.0}; dev half is 'even' (Lane L's declared tuning surface),
holdout 'odd'. Session text is the session's rounds joined in order and
truncated at cache time — an implementation constant, not a knob. F1
sequences are TILED to cfg.seq rather than padded (the park smoke
lesson: the model never saw pad floods; pooling reads only the first
copy). Halves are not publishable as full-corpus figures.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

SESSION_TEXT_CHARS = 6000  # cache-time truncation; encoders re-truncate to seq
RRF_K = 60  # the fusion constant the engine's own hybrid uses


# ---------------------------------------------------------------------------
# cache — runs under the repo venv; the only pass that imports the engine
# ---------------------------------------------------------------------------


def cmd_cache(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_HERE))
    import shutil
    import tempfile

    import run as lme  # noqa: E402  (bench/longmemeval/run.py)
    from bettermemory.store import Store  # noqa: E402

    corpus_path = Path(args.corpus)
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    corpus = json.loads(corpus_path.read_bytes().decode("utf-8"))
    idx = {"even": range(0, len(corpus), 2), "odd": range(1, len(corpus), 2)}[args.half]
    instances = [corpus[i] for i in idx]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, inst in enumerate(instances):
            evidence = list(dict.fromkeys(inst["answer_session_ids"]))
            if not evidence:
                continue
            root = Path(tempfile.mkdtemp(prefix="bm-lme-rr-"))
            try:
                id_to_session, _ = lme.build_question_store(root, inst)
                memories = Store(root).load_all()
                hits = lme.run_search(
                    memories,
                    inst["question"],
                    max_results=lme.RETRIEVAL_DEPTH,
                    mode="hybrid",
                    rescue_expansion=lme.RESCUE_EXPANSION,
                    conversational=lme.CONVERSATIONAL,
                    now=lme.question_now(inst),
                )
                ranked = lme.distinct_sessions([h.id for h in hits], id_to_session)
            finally:
                shutil.rmtree(root, ignore_errors=True)

            sid_to_session = dict(
                zip(inst["haystack_session_ids"], inst["haystack_sessions"])
            )
            texts = {
                sid: " ".join(lme.rounds_of(sid_to_session[sid]))[:SESSION_TEXT_CHARS]
                for sid in ranked[: args.k_cache]
                if sid in sid_to_session
            }
            fh.write(
                json.dumps(
                    {
                        "qid": inst.get("question_id", ""),
                        "type": inst.get("question_type", "unknown"),
                        "question": inst["question"],
                        "evidence": evidence,
                        "ranked": ranked,
                        "texts": texts,
                    }
                )
                + "\n"
            )
            n_written += 1
            if (i + 1) % 25 == 0:
                print(f"  cached {i + 1}/{len(instances)}", file=sys.stderr)
    print(f"cache: {n_written} questions -> {out}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# embed — one own-built encoder, no engine imports
# ---------------------------------------------------------------------------


def _load_cache(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _embed_w2(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sys.path.insert(0, str((_HERE / ".." / "w").resolve()))
    from w2_train import W2Encoder  # noqa: E402

    enc = W2Encoder.load(Path("/Volumes/data/bettermemory/runs/w2-2026-08-21"))

    def embed(texts: list[str]):
        return enc.encode_texts(texts)

    return {"embed": embed, "arm": "w2-rr"}


def _embed_f1(rows: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    import torch
    from tokenizers import Tokenizer

    root = Path("/Volumes/data/bettermemory/f1-park-2026-08-22")
    sys.path.insert(0, str(root / "stageA"))
    import f1_train_torch as T  # noqa: E402

    run_dir = root / "stageA" / "p1-2026-08-22"
    meta = json.loads((run_dir / "run_meta.json").read_text())
    cfg = T.Config(argparse.Namespace(**meta["config"]), meta["vocab"])
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    params = {
        k: v.float().to(device)
        for k, v in torch.load(
            run_dir / "ckpt" / "step0004000.pt",
            map_location="cpu",
            weights_only=False,
        )["params"].items()
    }
    tok = Tokenizer.from_file(str(root / "stageA" / "tokenizer" / "tokenizer.json"))
    masks = T.attention_masks(cfg, device)
    rope = T._rope_cache(cfg, device)
    seq = cfg.seq

    @torch.no_grad()
    def embed(texts: list[str], batch: int = 64):
        vecs = np.empty((len(texts), cfg.hidden), dtype=np.float32)
        ids_rows = []
        pool_lens = []
        for text in texts:
            ids = tok.encode(text).ids[:seq] or [0]
            pool_lens.append(len(ids))
            # Tile to fill the sequence: the model trained on dense
            # packed text and never saw pad floods (park smoke lesson).
            reps = (seq + len(ids) - 1) // len(ids)
            ids_rows.append((ids * reps)[:seq])
        for lo in range(0, len(texts), batch):
            hi = min(lo + batch, len(texts))
            x = torch.tensor(ids_rows[lo:hi], dtype=torch.long, device=device)
            h = T.forward_trunk(params, cfg, x, masks, rope)  # (B, S, d)
            for j in range(hi - lo):
                n = pool_lens[lo + j]
                v = h[j, :n].mean(dim=0)
                v = v / v.norm().clamp_min(1e-12)
                vecs[lo + j] = v.float().cpu().numpy()
        return vecs

    return {"embed": embed, "arm": "f1-rr"}


def cmd_embed(args: argparse.Namespace) -> int:
    import numpy as np

    rows = _load_cache(Path(args.cache))
    backend = {"w2": _embed_w2, "f1": _embed_f1}[args.arm](rows)
    embed = backend["embed"]

    q_texts = [r["question"] for r in rows]
    q_vecs = embed(q_texts)

    sid_order: list[list[str]] = []
    flat_texts: list[str] = []
    for r in rows:
        sids = list(r["texts"].keys())
        sid_order.append(sids)
        flat_texts.extend(r["texts"][s] for s in sids)
        if len(flat_texts) % 2000 < len(sids):
            print(f"  queued {len(flat_texts)} session texts", file=sys.stderr)
    s_vecs = embed(flat_texts)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        arm=backend["arm"],
        q_vecs=q_vecs,
        s_vecs=s_vecs,
        qids=np.array([r["qid"] for r in rows]),
        sid_order=np.array([json.dumps(s) for s in sid_order], dtype=object),
    )
    print(
        f"embed[{backend['arm']}]: {len(rows)} questions, "
        f"{len(flat_texts)} session texts -> {out}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# score — pure arithmetic over cache + embeddings
# ---------------------------------------------------------------------------

K_VALUES = (1, 5, 10)


def _recall_tables(rows, ranked_by_qid):
    from collections import defaultdict

    macro = dict.fromkeys(K_VALUES, 0.0)
    by_type: dict[str, dict[int, float]] = defaultdict(
        lambda: dict.fromkeys(K_VALUES, 0.0)
    )
    type_n: dict[str, int] = defaultdict(int)
    for r in rows:
        ranked = ranked_by_qid[r["qid"]]
        evidence = set(r["evidence"])
        qt = r["type"]
        type_n[qt] += 1
        for k in K_VALUES:
            recall = len(set(ranked[:k]) & evidence) / len(evidence)
            macro[k] += recall
            by_type[qt][k] += recall
    n = len(rows)
    return (
        {k: round(v / n, 4) for k, v in macro.items()},
        {
            qt: {k: round(v / type_n[qt], 4) for k, v in t.items()}
            for qt, t in by_type.items()
        },
        dict(type_n),
    )


def cmd_score(args: argparse.Namespace) -> int:
    import numpy as np

    rows = _load_cache(Path(args.cache))
    z = np.load(args.embeds, allow_pickle=True)
    arm = str(z["arm"])
    q_vecs = z["q_vecs"]
    s_vecs = z["s_vecs"]
    sid_order = [json.loads(s) for s in z["sid_order"]]
    qids = [str(q) for q in z["qids"]]
    assert qids == [r["qid"] for r in rows], "cache/embeds row mismatch"

    offsets = np.cumsum([0] + [len(s) for s in sid_order])
    baseline_ranked = {r["qid"]: r["ranked"] for r in rows}
    base_macro, base_by_type, type_n = _recall_tables(rows, baseline_ranked)

    grid_out = []
    for K in args.k_grid:
        for w in args.w_grid:
            fused_by_qid = {}
            for i, r in enumerate(rows):
                ranked = r["ranked"]
                window = [s for s in ranked[:K] if s in set(sid_order[i])]
                idx_of = {s: j for j, s in enumerate(sid_order[i])}
                block = s_vecs[offsets[i] : offsets[i + 1]]
                cos = block @ q_vecs[i]
                cos_rank = {
                    s: rank
                    for rank, s in enumerate(
                        sorted(window, key=lambda s: -cos[idx_of[s]])
                    )
                }
                fused = sorted(
                    ranked,
                    key=lambda s, _lr={s: j for j, s in enumerate(ranked)}: (
                        -(
                            1.0 / (RRF_K + _lr[s])
                            + (w / (RRF_K + cos_rank[s]) if s in cos_rank else 0.0)
                        )
                    ),
                )
                fused_by_qid[r["qid"]] = fused
            macro, by_type, _ = _recall_tables(rows, fused_by_qid)
            grid_out.append(
                {
                    "K": K,
                    "w": w,
                    "macro": macro,
                    "by_type": by_type,
                    "delta_macro5": round(macro[5] - base_macro[5], 4),
                }
            )
            print(
                f"  [{arm}] K={K} w={w} macro@5={macro[5]:.4f} "
                f"(base {base_macro[5]:.4f}, delta {macro[5] - base_macro[5]:+.4f})",
                file=sys.stderr,
            )

    result = {
        "arm": arm,
        "half": args.half,
        "n": len(rows),
        "baseline_macro": base_macro,
        "baseline_by_type": base_by_type,
        "type_n": type_n,
        "grid": grid_out,
        "notes": [
            "half-corpus read; not publishable as a full-corpus figure",
            "baseline recomputed from the same cache; deltas are paired",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(f"score[{arm}]: -> {out}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cache")
    c.add_argument("--corpus", default="data/longmemeval_m_cleaned.json")
    c.add_argument("--half", choices=("even", "odd"), required=True)
    c.add_argument("--k-cache", type=int, default=50)
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_cache)

    e = sub.add_parser("embed")
    e.add_argument("--arm", choices=("w2", "f1"), required=True)
    e.add_argument("--cache", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(fn=cmd_embed)

    s = sub.add_parser("score")
    s.add_argument("--cache", required=True)
    s.add_argument("--embeds", required=True)
    s.add_argument("--half", choices=("even", "odd"), required=True)
    s.add_argument("--k-grid", type=int, nargs="+", default=[25, 50])
    s.add_argument("--w-grid", type=float, nargs="+", default=[0.5, 1.0])
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_score)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
