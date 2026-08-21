"""The W2 trainer: a small transformer encoder, own code end to end.

This is the trainer `bench/w/W2_DECLARATION.md` §2 declares: this
repository's own forward and backward passes, its own Adam, its own
BPE (`w2_tokenizer.py`), numpy as the sole bench-side dependency — no
torch, no accelerator stack, no third-party weights anywhere in the
chain. The accelerator waiver the program frame offered W2 is not
taken: training runs on CPU, single process, BLAS pinned to one
thread before numpy loads a backend, one seeded
`numpy.random.Generator` consumed in a fixed order, and every
scatter-accumulation runs through the stable-order `_segment_add`
path `w1_train.py` established — a retrain from the pinned inputs
reproduces the emitted bytes exactly at the recorded configuration.

Two stages, one parameter set:

* **Stage A — masked-token pretraining** over the W1b register slice
  (the derived token cache, hash-verified or rebuilt from the pinned
  bytes). Documents bound sequences; each document's BPE id stream is
  chopped into sequence-cap windows; 15% of non-pad positions are
  masked 80/10/10; the loss is softmax cross-entropy against the tied
  input embedding.
* **Stage B — contrastive tuning** over the census's derived pair
  file: both prose sides encoded, pairs kept when both sides reach
  the declared token floor, truncated at the sequence cap; symmetric
  InfoNCE over in-batch negatives at the declared temperature, cosine
  over mean-pooled L2-normalized vectors.

RNG consumption order, fixed: parameter initialization in sorted
parameter-name order; stage A per batch — mask-selection uniforms,
branch uniforms, replacement-token integers; stage B — one
permutation per epoch and nothing else (in-batch negatives draw
nothing). The batch decomposition is a pure function of the stream
and the declared batch sizes.

Outputs land in ``--out``: ``vocab.txt`` and ``merges.txt`` (the
tokenizer), ``pretrain.npy`` (stage A's flat parameter vector),
``weights.npy`` (the final flat parameter vector), and ``meta.json``
(sorted keys) carrying hyperparameters, the parameter manifest, the
counts actually processed, loss-curve samples, and the sha256 of
every sibling file. The flat vector is the concatenation of every
parameter in sorted-name order — one byte-stable file to hash, one
manifest to rebuild the dict from.
"""

from __future__ import annotations

import os

# BLAS single-thread pins must land before numpy loads a backend; the
# declaration's determinism tier depends on them.
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402  (env pins must precede numpy)
import json  # noqa: E402  (env pins must precede numpy)
import math  # noqa: E402  (env pins must precede numpy)
import sys  # noqa: E402  (env pins must precede numpy)
import time  # noqa: E402  (env pins must precede numpy)
from collections import Counter  # noqa: E402  (env pins must precede numpy)
from typing import Any  # noqa: E402  (env pins must precede numpy)
from collections.abc import Iterator  # noqa: E402  (env pins must precede numpy)
from pathlib import Path  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

sys.path.insert(0, str(Path(__file__).parent))

from w1_corpus import tokenize  # noqa: E402
from w1_train import _segment_add  # noqa: E402
from w1b_corpus import CACHE_PATH, _sha256_file, build_cache, iter_cache_docs  # noqa: E402
from w2_tokenizer import (  # noqa: E402
    Bpe,
    keep_pair,
    load_tokenizer,
    parse_pair_row,
    save_tokenizer,
    train_bpe,
)

_GELU_C = math.sqrt(2.0 / math.pi)
_LN_EPS = 1e-5
_ADAM_EPS = 1e-8
_NEG_BIG = np.float32(-1e9)

# [pad], [mask], [unk] — replacement draws in the MLM 80/10/10 branch
# start above these ids so a corruption never writes a special token.
SPECIAL_IDS = (0, 1, 2)


# --- parameters ----------------------------------------------------------


def param_shapes(
    cfg: argparse.Namespace, vocab_size: int
) -> dict[str, tuple[int, ...]]:
    shapes: dict[str, tuple[int, ...]] = {
        "emb": (vocab_size, cfg.dim),
        "pos": (cfg.seq, cfg.dim),
        "final_ln.g": (cfg.dim,),
        "final_ln.b": (cfg.dim,),
        "mlm_bias": (vocab_size,),
    }
    for i in range(cfg.layers):
        p = f"blocks.{i:02d}."
        shapes[p + "ln1.g"] = (cfg.dim,)
        shapes[p + "ln1.b"] = (cfg.dim,)
        shapes[p + "ln2.g"] = (cfg.dim,)
        shapes[p + "ln2.b"] = (cfg.dim,)
        for name in ("wq", "wk", "wv", "wo"):
            shapes[p + name] = (cfg.dim, cfg.dim)
        for name in ("bq", "bk", "bv", "bo"):
            shapes[p + name] = (cfg.dim,)
        shapes[p + "ffn.w1"] = (cfg.dim, cfg.ffn)
        shapes[p + "ffn.b1"] = (cfg.ffn,)
        shapes[p + "ffn.w2"] = (cfg.ffn, cfg.dim)
        shapes[p + "ffn.b2"] = (cfg.dim,)
    return shapes


def init_params(
    rng: np.random.Generator, shapes: dict[str, tuple[int, ...]]
) -> dict[str, np.ndarray]:
    """Initialize in sorted-name order — the declared RNG consumption order."""
    params: dict[str, np.ndarray] = {}
    for name in sorted(shapes):
        shape = shapes[name]
        if name.endswith((".g",)):
            params[name] = np.ones(shape, dtype=np.float32)
        elif name.endswith((".b", "bq", "bk", "bv", "bo", "ffn.b1", "ffn.b2")) or (
            name == "mlm_bias"
        ):
            params[name] = np.zeros(shape, dtype=np.float32)
        else:
            params[name] = rng.normal(0.0, 0.02, size=shape).astype(np.float32)
    return params


def flatten_params(params: dict[str, np.ndarray]) -> np.ndarray:
    return np.concatenate([params[name].ravel() for name in sorted(params)])


def unflatten_params(
    flat: np.ndarray, shapes: dict[str, tuple[int, ...]]
) -> dict[str, np.ndarray]:
    params: dict[str, np.ndarray] = {}
    at = 0
    for name in sorted(shapes):
        size = int(np.prod(shapes[name]))
        params[name] = flat[at : at + size].reshape(shapes[name]).astype(np.float32)
        at += size
    if at != flat.size:
        raise ValueError(f"flat vector holds {flat.size} values, manifest {at}")
    return params


# --- primitive forwards/backwards ---------------------------------------


def _ln_forward(
    x: np.ndarray, g: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]:
    mu = x.mean(axis=-1, keepdims=True)
    xc = x - mu
    inv = 1.0 / np.sqrt((xc * xc).mean(axis=-1, keepdims=True) + _LN_EPS)
    xhat = xc * inv
    return xhat * g + b, (xhat, inv)


def _ln_backward(
    dy: np.ndarray,
    cache: tuple[np.ndarray, np.ndarray],
    g: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xhat, inv = cache
    dg = (dy * xhat).sum(axis=tuple(range(dy.ndim - 1)))
    db = dy.sum(axis=tuple(range(dy.ndim - 1)))
    dxhat = dy * g
    dx = inv * (
        dxhat
        - dxhat.mean(axis=-1, keepdims=True)
        - xhat * (dxhat * xhat).mean(axis=-1, keepdims=True)
    )
    return dx.astype(np.float32), dg.astype(np.float32), db.astype(np.float32)


def _gelu_forward(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.tanh(_GELU_C * (x + 0.044715 * x**3))
    return (0.5 * x * (1.0 + t)).astype(np.float32), t


def _gelu_backward(dy: np.ndarray, x: np.ndarray, t: np.ndarray) -> np.ndarray:
    du = (1.0 - t * t) * _GELU_C * (1.0 + 3 * 0.044715 * x * x)
    return (dy * (0.5 * (1.0 + t) + 0.5 * x * du)).astype(np.float32)


def _softmax_last(x: np.ndarray) -> np.ndarray:
    shifted = x - x.max(axis=-1, keepdims=True)
    e = np.exp(shifted)
    return (e / e.sum(axis=-1, keepdims=True)).astype(np.float32)


# --- the encoder ---------------------------------------------------------


def encoder_forward(
    params: dict[str, np.ndarray], cfg: argparse.Namespace, ids: np.ndarray
) -> tuple[np.ndarray, dict[str, Any]]:
    """ids (B, S) int32, 0 = pad. Returns final-LN output (B, S, d) + cache."""
    batch, seq = ids.shape
    heads, dim = cfg.heads, cfg.dim
    hd = dim // heads
    pad = ids == 0
    x = params["emb"][ids] + params["pos"][:seq]
    key_bias = np.where(pad[:, None, None, :], _NEG_BIG, np.float32(0.0))
    cache: dict[str, Any] = {"ids": ids, "pad": pad, "blocks": []}
    for i in range(cfg.layers):
        p = f"blocks.{i:02d}."
        h, ln1_cache = _ln_forward(x, params[p + "ln1.g"], params[p + "ln1.b"])
        q = h @ params[p + "wq"] + params[p + "bq"]
        k = h @ params[p + "wk"] + params[p + "bk"]
        v = h @ params[p + "wv"] + params[p + "bv"]

        def split(m: np.ndarray) -> np.ndarray:
            return m.reshape(batch, seq, heads, hd).transpose(0, 2, 1, 3)

        qh, kh, vh = split(q), split(k), split(v)
        scores = qh @ kh.transpose(0, 1, 3, 2) / np.float32(math.sqrt(hd)) + key_bias
        probs = _softmax_last(scores)
        ctx = probs @ vh
        merged = ctx.transpose(0, 2, 1, 3).reshape(batch, seq, dim)
        attn_out = merged @ params[p + "wo"] + params[p + "bo"]
        x = x + attn_out
        h2, ln2_cache = _ln_forward(x, params[p + "ln2.g"], params[p + "ln2.b"])
        pre = h2 @ params[p + "ffn.w1"] + params[p + "ffn.b1"]
        act, tanh_cache = _gelu_forward(pre)
        x = x + act @ params[p + "ffn.w2"] + params[p + "ffn.b2"]
        cache["blocks"].append(
            {
                "ln1": ln1_cache,
                "h": h,
                "qh": qh,
                "kh": kh,
                "vh": vh,
                "probs": probs,
                "merged": merged,
                "ln2": ln2_cache,
                "h2": h2,
                "pre": pre,
                "act": act,
                "tanh": tanh_cache,
            }
        )
    out, final_cache = _ln_forward(x, params["final_ln.g"], params["final_ln.b"])
    cache["final_ln"] = final_cache
    return out, cache


def encoder_backward(
    params: dict[str, np.ndarray],
    cfg: argparse.Namespace,
    cache: dict[str, Any],
    dout: np.ndarray,
    grads: dict[str, np.ndarray],
) -> None:
    """Accumulate parameter gradients for one forward's cache."""
    ids: np.ndarray = cache["ids"]
    batch, seq = ids.shape
    heads, dim = cfg.heads, cfg.dim
    hd = dim // heads

    dx, dg, db = _ln_backward(dout, cache["final_ln"], params["final_ln.g"])
    grads["final_ln.g"] += dg
    grads["final_ln.b"] += db

    blocks: list[dict[str, Any]] = cache["blocks"]
    for i in range(cfg.layers - 1, -1, -1):
        p = f"blocks.{i:02d}."
        blk = blocks[i]
        # FFN half: x_out = x_mid + gelu(ln2(x_mid) @ w1 + b1) @ w2 + b2
        act, pre, h2, tanh = blk["act"], blk["pre"], blk["h2"], blk["tanh"]
        dact = dx @ params[p + "ffn.w2"].T
        grads[p + "ffn.w2"] += act.reshape(-1, cfg.ffn).T @ dx.reshape(-1, dim)
        grads[p + "ffn.b2"] += dx.sum(axis=(0, 1))
        dpre = _gelu_backward(dact, pre, tanh)
        dh2 = dpre @ params[p + "ffn.w1"].T
        grads[p + "ffn.w1"] += h2.reshape(-1, dim).T @ dpre.reshape(-1, cfg.ffn)
        grads[p + "ffn.b1"] += dpre.sum(axis=(0, 1))
        dmid, dg2, db2 = _ln_backward(dh2, blk["ln2"], params[p + "ln2.g"])
        grads[p + "ln2.g"] += dg2
        grads[p + "ln2.b"] += db2
        dx = dx + dmid

        # Attention half: x_mid = x_in + (softmax(qk)v merged) @ wo + bo
        merged = blk["merged"]
        grads[p + "wo"] += merged.reshape(-1, dim).T @ dx.reshape(-1, dim)
        grads[p + "bo"] += dx.sum(axis=(0, 1))
        dmerged = dx @ params[p + "wo"].T
        dctx = dmerged.reshape(batch, seq, heads, hd).transpose(0, 2, 1, 3)
        probs, qh, kh, vh = blk["probs"], blk["qh"], blk["kh"], blk["vh"]
        dprobs = dctx @ vh.transpose(0, 1, 3, 2)
        dvh = probs.transpose(0, 1, 3, 2) @ dctx
        dscores = probs * (dprobs - (dprobs * probs).sum(axis=-1, keepdims=True))
        dscores /= np.float32(math.sqrt(hd))
        dqh = dscores @ kh
        dkh = dscores.transpose(0, 1, 3, 2) @ qh

        def merge(m: np.ndarray) -> np.ndarray:
            return m.transpose(0, 2, 1, 3).reshape(batch, seq, dim)

        dq, dk, dv = merge(dqh), merge(dkh), merge(dvh)
        h = blk["h"]
        h_flat = h.reshape(-1, dim)
        grads[p + "wq"] += h_flat.T @ dq.reshape(-1, dim)
        grads[p + "wk"] += h_flat.T @ dk.reshape(-1, dim)
        grads[p + "wv"] += h_flat.T @ dv.reshape(-1, dim)
        grads[p + "bq"] += dq.sum(axis=(0, 1))
        grads[p + "bk"] += dk.sum(axis=(0, 1))
        grads[p + "bv"] += dv.sum(axis=(0, 1))
        dh = dq @ params[p + "wq"].T + dk @ params[p + "wk"].T + dv @ params[p + "wv"].T
        din, dg1, db1 = _ln_backward(dh, blk["ln1"], params[p + "ln1.g"])
        grads[p + "ln1.g"] += dg1
        grads[p + "ln1.b"] += db1
        dx = dx + din

    flat_ids = ids.ravel().astype(np.int64)
    _segment_add(grads["emb"], flat_ids, dx.reshape(-1, dim))
    pos_ids = np.tile(np.arange(seq, dtype=np.int64), batch)
    _segment_add(grads["pos"], pos_ids, dx.reshape(-1, dim))


def pool_forward(
    out: np.ndarray, ids: np.ndarray
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Mean over non-pad positions, then L2 normalization."""
    mask = (ids != 0).astype(np.float32)[:, :, None]
    n = np.maximum(mask.sum(axis=1), 1.0)
    pooled = (out * mask).sum(axis=1) / n
    norm = np.maximum(np.sqrt((pooled * pooled).sum(axis=1, keepdims=True)), 1e-12)
    z = (pooled / norm).astype(np.float32)
    return z, (mask, n, z, norm)


def pool_backward(
    dz: np.ndarray, cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
) -> np.ndarray:
    mask, n, z, norm = cache
    dpooled = (dz - z * (dz * z).sum(axis=1, keepdims=True)) / norm
    return (dpooled[:, None, :] * mask / n[:, None, :]).astype(np.float32)


# --- Adam ---------------------------------------------------------------


class Adam:
    def __init__(self, params: dict[str, np.ndarray], clip: float) -> None:
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0
        self.clip = clip

    def step(
        self,
        params: dict[str, np.ndarray],
        grads: dict[str, np.ndarray],
        lr: float,
    ) -> float:
        norm_sq = 0.0
        for name in sorted(grads):
            norm_sq += float((grads[name].astype(np.float64) ** 2).sum())
        norm = math.sqrt(norm_sq)
        scale = np.float32(min(1.0, self.clip / norm) if norm > 0 else 1.0)
        self.t += 1
        bc1 = 1.0 - 0.9**self.t
        bc2 = 1.0 - 0.999**self.t
        for name in sorted(params):
            g = grads[name] * scale
            m = self.m[name]
            v = self.v[name]
            m *= 0.9
            m += 0.1 * g
            v *= 0.999
            v += 0.001 * g * g
            params[name] -= np.float32(lr) * (m / bc1) / (np.sqrt(v / bc2) + _ADAM_EPS)
        return norm


def zero_grads(params: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {k: np.zeros_like(v) for k, v in params.items()}


# --- stage A: masked-token pretraining ----------------------------------


def iter_cache_words(
    cache_path: Path | None, ci_register: str | None, cap: int
) -> Iterator[list[str]]:
    """Word-token documents from the cache or the CI register, capped."""
    remaining = cap
    if ci_register is not None:
        text = Path(ci_register).read_text(encoding="utf-8")
        docs: Iterator[list[str]] = (
            tokenize(p) for p in text.split("\n\n") if tokenize(p)
        )
    else:
        assert cache_path is not None
        docs = iter_cache_docs(cache_path)
    for tokens in docs:
        if len(tokens) >= remaining:
            yield tokens[:remaining]
            return
        remaining -= len(tokens)
        yield tokens


def build_chunks(
    bpe: Bpe,
    cache_path: Path | None,
    ci_register: str | None,
    cap: int,
    seq: int,
) -> np.ndarray:
    """(N, seq) int32 id windows, document-bounded, short tails >= 8 kept."""
    rows: list[np.ndarray] = []
    for tokens in iter_cache_words(cache_path, ci_register, cap):
        ids = bpe.encode_words(tokens)
        for lo in range(0, len(ids), seq):
            window = ids[lo : lo + seq]
            if len(window) < 8:
                continue
            row = np.zeros(seq, dtype=np.int32)
            row[: len(window)] = window
            rows.append(row)
    if not rows:
        raise ValueError("stage A produced no sequences — empty source?")
    return np.stack(rows)


def draw_corruption(
    cfg: argparse.Namespace,
    ids: np.ndarray,
    rng: np.random.Generator,
    mask_id: int,
    vocab_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """(selection mask, corrupted ids) — the batch's three RNG draws."""
    non_pad = ids != 0
    sel = (rng.random(ids.shape) < cfg.mask_rate) & non_pad
    branch = rng.random(ids.shape)
    replacement = rng.integers(len(SPECIAL_IDS), vocab_size, size=ids.shape)
    corrupted = ids.copy()
    corrupted[sel & (branch < 0.8)] = mask_id
    random_spots = sel & (branch >= 0.8) & (branch < 0.9)
    corrupted[random_spots] = replacement[random_spots].astype(np.int32)
    return sel, corrupted


def mlm_batch_loss(
    params: dict[str, np.ndarray],
    cfg: argparse.Namespace,
    ids: np.ndarray,
    sel: np.ndarray,
    corrupted: np.ndarray,
    grads: dict[str, np.ndarray],
) -> tuple[float, int]:
    """One masked-LM batch: forward, loss, backward. Returns (loss, n_masked)."""
    n_masked = int(sel.sum())
    if n_masked == 0:
        return 0.0, 0

    out, cache = encoder_forward(params, cfg, corrupted)
    h = out[sel]
    logits = h @ params["emb"].T + params["mlm_bias"]
    probs = _softmax_last(logits)
    targets = ids[sel].astype(np.int64)
    rows = np.arange(n_masked)
    loss = float(-np.log(np.maximum(probs[rows, targets], 1e-12)).mean())

    dlogits = probs
    dlogits[rows, targets] -= 1.0
    dlogits /= np.float32(n_masked)
    grads["mlm_bias"] += dlogits.sum(axis=0)
    grads["emb"] += dlogits.T @ h
    dh = dlogits @ params["emb"]
    dout = np.zeros_like(out)
    dout[sel] = dh
    encoder_backward(params, cfg, cache, dout, grads)
    return loss, n_masked


# --- stage B: contrastive tuning ----------------------------------------


def build_pairs(
    bpe: Bpe, tsv_path: Path, seq: int, min_tokens: int, pair_cap: int | None
) -> tuple[np.ndarray, dict[str, int]]:
    """(N, 2, seq) int32 padded pair ids + read accounting."""
    counts = {"rows": 0, "malformed": 0, "dropped_short": 0, "kept": 0}
    rows: list[np.ndarray] = []
    with tsv_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            counts["rows"] += 1
            parsed = parse_pair_row(line)
            if parsed is None:
                counts["malformed"] += 1
                continue
            left_ids = bpe.encode_words(tokenize(parsed[0]))
            right_ids = bpe.encode_words(tokenize(parsed[1]))
            if not keep_pair(left_ids, right_ids, min_tokens):
                counts["dropped_short"] += 1
                continue
            row = np.zeros((2, seq), dtype=np.int32)
            row[0, : min(seq, len(left_ids))] = left_ids[:seq]
            row[1, : min(seq, len(right_ids))] = right_ids[:seq]
            rows.append(row)
            counts["kept"] += 1
            if pair_cap is not None and counts["kept"] >= pair_cap:
                break
    if not rows:
        raise ValueError("stage B kept no pairs — wrong file?")
    return np.stack(rows), counts


def infonce_batch_loss(
    params: dict[str, np.ndarray],
    cfg: argparse.Namespace,
    pair_ids: np.ndarray,
    grads: dict[str, np.ndarray],
) -> float:
    """One symmetric InfoNCE batch over in-batch negatives."""
    batch = pair_ids.shape[0]
    left_out, left_cache = encoder_forward(params, cfg, pair_ids[:, 0])
    right_out, right_cache = encoder_forward(params, cfg, pair_ids[:, 1])
    zl, pl = pool_forward(left_out, pair_ids[:, 0])
    zr, pr = pool_forward(right_out, pair_ids[:, 1])
    sims = (zl @ zr.T) / np.float32(cfg.tau)
    rows = np.arange(batch)
    probs_row = _softmax_last(sims)
    probs_col = _softmax_last(sims.T)
    loss = 0.5 * float(
        -np.log(np.maximum(probs_row[rows, rows], 1e-12)).mean()
        - np.log(np.maximum(probs_col[rows, rows], 1e-12)).mean()
    )
    dsims_row = probs_row.copy()
    dsims_row[rows, rows] -= 1.0
    dsims_col = probs_col.copy()
    dsims_col[rows, rows] -= 1.0
    dsims = (0.5 / batch) * (dsims_row + dsims_col.T) / np.float32(cfg.tau)
    dzl = dsims @ zr
    dzr = dsims.T @ zl
    encoder_backward(params, cfg, left_cache, pool_backward(dzl, pl), grads)
    encoder_backward(params, cfg, right_cache, pool_backward(dzr, pr), grads)
    return loss


# --- inference -----------------------------------------------------------


class W2Encoder:
    """Inference over a committed run directory — forward only."""

    def __init__(
        self,
        params: dict[str, np.ndarray],
        cfg: argparse.Namespace,
        bpe: Bpe,
    ) -> None:
        self.params = params
        self.cfg = cfg
        self.bpe = bpe

    @classmethod
    def load(cls, run_dir: Path) -> "W2Encoder":
        meta = json.loads((run_dir / "meta.json").read_text())
        cfg = argparse.Namespace(**meta["config"])
        bpe = load_tokenizer(run_dir)
        shapes = {name: tuple(shape) for name, shape in meta["manifest"]}
        flat = np.load(run_dir / "weights.npy")
        return cls(unflatten_params(flat, shapes), cfg, bpe)

    def encode_texts(self, texts: list[str], batch: int = 256) -> np.ndarray:
        seq = self.cfg.seq
        ids = np.zeros((len(texts), seq), dtype=np.int32)
        for i, text in enumerate(texts):
            encoded = self.bpe.encode_words(tokenize(text)) or [self.bpe.unk_id]
            ids[i, : min(seq, len(encoded))] = encoded[:seq]
        out = np.empty((len(texts), self.cfg.dim), dtype=np.float32)
        for lo in range(0, len(texts), batch):
            hi = min(lo + batch, len(texts))
            hidden, _ = encoder_forward(self.params, self.cfg, ids[lo:hi])
            out[lo:hi] = pool_forward(hidden, ids[lo:hi])[0]
        return out


# --- the run -------------------------------------------------------------


def _sha_bytes(path: Path) -> str:
    return _sha256_file(path)


def train(args: argparse.Namespace) -> dict[str, object]:
    t_start = time.time()
    rng = np.random.Generator(np.random.PCG64(args.seed))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cache_path: Path | None = None
    cache_meta: dict[str, object] | None = None
    if args.ci_register is None and args.stage in ("ab", "a"):
        cache_path = CACHE_PATH
        if args.cache_expect_sha:
            got = _sha256_file(cache_path)
            if got != args.cache_expect_sha:
                raise SystemExit(
                    f"token cache sha mismatch: {got} != {args.cache_expect_sha}"
                )
            cache_meta = {"path": str(cache_path), "sha256": got, "verified": True}
        else:
            print("building the W1b token cache...", file=sys.stderr, flush=True)
            cache_meta = build_cache()

    if args.stage == "b":
        source = Path(args.pretrained)
        bpe = load_tokenizer(source)
        save_tokenizer(out, bpe.vocab, _merge_pairs(source))
        prior_meta = json.loads((source / "meta.json").read_text())
        shapes = {name: tuple(shape) for name, shape in prior_meta["manifest"]}
        params = unflatten_params(np.load(source / "pretrain.npy"), shapes)
        np.save(out / "pretrain.npy", flatten_params(params))
        chunk_count = 0
        mlm_losses: list[float] = []
        tokens_read = 0
    else:
        print("stage 0: learning the BPE table...", file=sys.stderr, flush=True)
        type_counts: Counter[str] = Counter()
        tokens_read = 0
        for tokens in iter_cache_words(cache_path, args.ci_register, args.token_cap):
            type_counts.update(tokens)
            tokens_read += len(tokens)
        vocab, merges = train_bpe(type_counts, args.vocab_size)
        save_tokenizer(out, vocab, merges)
        bpe = Bpe(vocab, merges)
        print(
            f"  vocab {len(vocab)} over {tokens_read} cache tokens "
            f"({time.time() - t_start:.0f}s)",
            file=sys.stderr,
            flush=True,
        )

        shapes = param_shapes(args, len(vocab))
        params = init_params(rng, shapes)

        print("stage A: masked-token pretraining...", file=sys.stderr, flush=True)
        chunks = build_chunks(
            bpe, cache_path, args.ci_register, args.token_cap, args.seq
        )
        chunk_count = int(chunks.shape[0])
        mask_id = 1
        adam = Adam(params, args.clip)
        mlm_losses = []
        total_batches = args.mlm_epochs * math.ceil(chunk_count / args.mlm_batch)
        done = 0
        for _ in range(args.mlm_epochs):
            for lo in range(0, chunk_count, args.mlm_batch):
                batch_ids = chunks[lo : lo + args.mlm_batch]
                grads = zero_grads(params)
                sel, corrupted = draw_corruption(
                    args, batch_ids, rng, mask_id, len(vocab)
                )
                loss, n_masked = mlm_batch_loss(
                    params, args, batch_ids, sel, corrupted, grads
                )
                if n_masked:
                    lr = args.mlm_lr * max(0.05, 1.0 - done / max(1, total_batches))
                    adam.step(params, grads, lr)
                done += 1
                if done % 200 == 0 or done == total_batches:
                    mlm_losses.append(round(loss, 4))
                    print(
                        f"  A {done}/{total_batches} loss {loss:.4f} "
                        f"({time.time() - t_start:.0f}s)",
                        file=sys.stderr,
                        flush=True,
                    )
        del chunks
        np.save(out / "pretrain.npy", flatten_params(params))
        if args.stage == "a":
            return _finish(
                args,
                out,
                t_start,
                tokens_read,
                chunk_count,
                mlm_losses,
                [],
                {"rows": 0},
                cache_meta,
                None,
                shapes,
                final=False,
            )

    print("stage B: contrastive tuning...", file=sys.stderr, flush=True)
    pairs_path = Path(args.pairs_tsv)
    pairs_sha = _sha256_file(pairs_path)
    if args.pairs_expect_sha and pairs_sha != args.pairs_expect_sha:
        raise SystemExit(
            f"pair file sha mismatch: {pairs_sha} != {args.pairs_expect_sha}"
        )
    pair_ids, pair_counts = build_pairs(
        bpe, pairs_path, args.seq, args.pair_min_tokens, args.pair_cap
    )
    n_pairs = int(pair_ids.shape[0])
    print(
        f"  {n_pairs} pairs kept of {pair_counts['rows']} rows "
        f"({time.time() - t_start:.0f}s)",
        file=sys.stderr,
        flush=True,
    )
    adam_b = Adam(params, args.clip)
    pair_losses: list[float] = []
    total_batches = args.pair_epochs * math.ceil(n_pairs / args.pair_batch)
    done = 0
    for _ in range(args.pair_epochs):
        order = rng.permutation(n_pairs)
        for lo in range(0, n_pairs, args.pair_batch):
            batch_ids = pair_ids[order[lo : lo + args.pair_batch]]
            if batch_ids.shape[0] < 2:
                continue
            grads = zero_grads(params)
            loss = infonce_batch_loss(params, args, batch_ids, grads)
            lr = args.pair_lr * max(0.05, 1.0 - done / max(1, total_batches))
            adam_b.step(params, grads, lr)
            done += 1
            if done % 100 == 0 or done == total_batches:
                pair_losses.append(round(loss, 4))
                print(
                    f"  B {done}/{total_batches} loss {loss:.4f} "
                    f"({time.time() - t_start:.0f}s)",
                    file=sys.stderr,
                    flush=True,
                )
    np.save(out / "weights.npy", flatten_params(params))
    return _finish(
        args,
        out,
        t_start,
        tokens_read,
        chunk_count,
        mlm_losses,
        pair_losses,
        pair_counts,
        cache_meta,
        pairs_sha,
        shapes,
        final=True,
    )


def _merge_pairs(run_dir: Path) -> list[tuple[str, str]]:
    return [
        (line.split(" ", 1)[0], line.split(" ", 1)[1])
        for line in (run_dir / "merges.txt").read_text(encoding="utf-8").splitlines()
        if line
    ]


def _finish(
    args: argparse.Namespace,
    out: Path,
    t_start: float,
    tokens_read: int,
    chunk_count: int,
    mlm_losses: list[float],
    pair_losses: list[float],
    pair_counts: dict[str, int],
    cache_meta: dict[str, object] | None,
    pairs_sha: str | None,
    shapes: dict[str, tuple[int, ...]],
    *,
    final: bool,
) -> dict[str, object]:
    files = ["vocab.txt", "merges.txt", "pretrain.npy"]
    if final:
        files.append("weights.npy")
    config = {
        k: getattr(args, k)
        for k in (
            "seed",
            "token_cap",
            "vocab_size",
            "dim",
            "layers",
            "heads",
            "ffn",
            "seq",
            "mlm_epochs",
            "mlm_batch",
            "mlm_lr",
            "mask_rate",
            "pair_epochs",
            "pair_batch",
            "pair_lr",
            "tau",
            "pair_min_tokens",
            "clip",
            "stage",
        )
    }
    meta: dict[str, object] = {
        "config": config,
        "manifest": [[name, list(shapes[name])] for name in sorted(shapes)],
        "tokens_read": tokens_read,
        "chunks": chunk_count,
        "pair_counts": pair_counts,
        "cache": cache_meta,
        "pairs_tsv": str(args.pairs_tsv) if final else None,
        "pairs_sha256": pairs_sha,
        "ci_register": args.ci_register,
        "pretrained_from": args.pretrained,
        "mlm_loss_samples": mlm_losses,
        "pair_loss_samples": pair_losses,
        "blas_pins": {
            var: os.environ.get(var)
            for var in (
                "OPENBLAS_NUM_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
        "numpy_version": np.__version__,
        "wall_seconds": round(time.time() - t_start, 1),
        "sha256": {name: _sha_bytes(out / name) for name in files},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(
        description="The W2 contrastive-encoder trainer over the pinned register."
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--token-cap", type=int, default=200_000_000)
    parser.add_argument("--vocab-size", type=int, default=32_768)
    parser.add_argument("--dim", type=int, default=192)
    parser.add_argument("--layers", type=int, default=4)
    parser.add_argument("--heads", type=int, default=4)
    parser.add_argument("--ffn", type=int, default=768)
    parser.add_argument("--seq", type=int, default=128)
    parser.add_argument("--mlm-epochs", type=int, default=1)
    parser.add_argument("--mlm-batch", type=int, default=64)
    parser.add_argument("--mlm-lr", type=float, default=3e-4)
    parser.add_argument("--mask-rate", type=float, default=0.15)
    parser.add_argument("--pair-epochs", type=int, default=2)
    parser.add_argument("--pair-batch", type=int, default=256)
    parser.add_argument("--pair-lr", type=float, default=1e-4)
    parser.add_argument("--tau", type=float, default=0.05)
    parser.add_argument("--pair-min-tokens", type=int, default=8)
    parser.add_argument("--pair-cap", type=int, default=None)
    parser.add_argument("--clip", type=float, default=1.0)
    parser.add_argument(
        "--pairs-tsv",
        default="bench/w/corpus/derived/w2-so-bodies-2026-08-20.tsv",
    )
    parser.add_argument("--pairs-expect-sha", default=None)
    parser.add_argument(
        "--cache-expect-sha",
        default=None,
        help="hash-verify the existing token cache instead of rebuilding",
    )
    parser.add_argument(
        "--ci-register",
        default=None,
        help="G3 reduced mode: pretrain from this text file, no pinned reads",
    )
    parser.add_argument("--stage", choices=("ab", "a", "b"), default="ab")
    parser.add_argument(
        "--pretrained",
        default=None,
        help="stage b: the run directory holding tokenizer + pretrain.npy",
    )
    args = parser.parse_args()
    if args.stage == "b" and not args.pretrained:
        parser.error("--stage b requires --pretrained")
    meta = train(args)
    print(json.dumps({k: meta[k] for k in ("tokens_read", "chunks", "wall_seconds")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
