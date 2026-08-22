"""The F0 trainer: the W2 recipe class ported to torch, GPU-resident.

The Lane F program frame admits an accelerator training stack as a
bench-side dependency; this file is that admission's first unit. It is
NOT a new recipe: architecture, hyperparameters, data plumbing, loss
math, optimizer math, and the RNG consumption contract are the W2
trainer's (`w2_train.py`), imported where importable and mirrored
where the math moves to torch. The intended reading: two trainers, one
recipe class — the numpy one is the CPU-era reference, this one is the
accelerator derivation, and the reduced-fixture check compares them
loss-for-loss.

Fidelity contract (what makes the artifact the same CLASS):

* **All randomness stays in numpy.** One `numpy.random.Generator`
  (PCG64, the declared seed) is consumed in exactly `w2_train.py`'s
  order: parameter init in sorted-name order, stage A's three per-batch
  corruption draws, stage B's one permutation per epoch. torch draws
  nothing. Identical seed + identical inputs therefore yield identical
  init bytes, identical masks, identical batch order — the only
  divergence from the CPU trainer is kernel arithmetic.
* **Same shapes, same math.** Pre-LN blocks, explicit
  softmax(qk/sqrt(hd) + key_bias)v attention (no fused SDPA — the
  formula stays visible and the mask semantics stay w2_train's),
  tanh-GELU via the same constant, tied-embedding MLM head, mean-pool +
  L2 InfoNCE at the declared temperature. `torch.optim.Adam` computes
  the same update as w2_train's own Adam (betas 0.9/0.999, eps 1e-8,
  bias correction in the same algebraic form);
  `clip_grad_norm_` is the same global-norm scale; the LR schedule is
  the same per-step linear decay floored at 5%.
* **Same artifact format.** vocab.txt / merges.txt / pretrain.npy /
  weights.npy / meta.json, the flat vector in sorted-name order —
  `w2_measure.py`, `w2_geometry_probe.py`, and `W2Encoder.load` read an
  F0 run directory unchanged. meta.json carries a `torch` block
  (version, device, dtype, TF32 state, determinism flags) beside the
  W2 fields.
* **Determinism recipe** (validated on the pod 2026-08-21, 3/3 bitwise):
  `CUBLAS_WORKSPACE_CONFIG=:4096:8` exported before cuda init,
  `torch.use_deterministic_algorithms(True)`, cudnn benchmark off,
  TF32 OFF by default (fp32 means fp32 in the reference reading; a
  `--tf32` run records itself as such). F-strict is read by the
  double-run: same invocation twice, every emitted byte equal.
"""

from __future__ import annotations

import os

# The workspace pin must precede any cuBLAS initialization; the BLAS
# single-thread pins ride along for the numpy-side data plumbing.
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402  (env pins must precede the stacks)
import json  # noqa: E402  (env pins must precede the stacks)
import math  # noqa: E402  (env pins must precede the stacks)
import sys  # noqa: E402  (env pins must precede the stacks)
import time  # noqa: E402  (env pins must precede the stacks)
from collections import Counter  # noqa: E402  (env pins must precede the stacks)
from pathlib import Path  # noqa: E402  (env pins must precede the stacks)

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from w1b_corpus import CACHE_PATH, _sha256_file, build_cache  # noqa: E402
from w2_tokenizer import Bpe, load_tokenizer, save_tokenizer, train_bpe  # noqa: E402
from w2_train import (  # noqa: E402
    _GELU_C,
    _LN_EPS,
    _NEG_BIG,
    _merge_pairs,
    build_chunks,
    build_pairs,
    draw_corruption,
    flatten_params,
    init_params,
    iter_cache_words,
    param_shapes,
    unflatten_params,
)


# --- torch mirror of the encoder -----------------------------------------


def _to_device(
    params: dict[str, np.ndarray], device: str, dtype: torch.dtype
) -> dict[str, torch.Tensor]:
    return {
        name: torch.from_numpy(value).to(device=device, dtype=dtype).requires_grad_()
        for name, value in params.items()
    }


def _to_numpy(params: dict[str, torch.Tensor]) -> dict[str, np.ndarray]:
    return {
        name: value.detach().to("cpu", torch.float32).numpy()
        for name, value in params.items()
    }


def encoder_forward(
    params: dict[str, torch.Tensor], cfg: argparse.Namespace, ids: torch.Tensor
) -> torch.Tensor:
    """ids (B, S) int64 on device, 0 = pad. Returns final-LN output (B, S, d)."""
    batch, seq = ids.shape
    heads, dim = cfg.heads, cfg.dim
    hd = dim // heads
    pad = ids == 0
    x = params["emb"][ids] + params["pos"][:seq]
    key_bias = torch.where(
        pad[:, None, None, :],
        x.new_tensor(float(_NEG_BIG)),
        x.new_tensor(0.0),
    )
    for i in range(cfg.layers):
        p = f"blocks.{i:02d}."
        h = F.layer_norm(
            x, (dim,), params[p + "ln1.g"], params[p + "ln1.b"], eps=_LN_EPS
        )
        q = h @ params[p + "wq"] + params[p + "bq"]
        k = h @ params[p + "wk"] + params[p + "bk"]
        v = h @ params[p + "wv"] + params[p + "bv"]
        qh = q.view(batch, seq, heads, hd).transpose(1, 2)
        kh = k.view(batch, seq, heads, hd).transpose(1, 2)
        vh = v.view(batch, seq, heads, hd).transpose(1, 2)
        scores = qh @ kh.transpose(-1, -2) / math.sqrt(hd) + key_bias
        probs = torch.softmax(scores, dim=-1)
        ctx = probs @ vh
        merged = ctx.transpose(1, 2).reshape(batch, seq, dim)
        x = x + merged @ params[p + "wo"] + params[p + "bo"]
        h2 = F.layer_norm(
            x, (dim,), params[p + "ln2.g"], params[p + "ln2.b"], eps=_LN_EPS
        )
        pre = h2 @ params[p + "ffn.w1"] + params[p + "ffn.b1"]
        act = 0.5 * pre * (1.0 + torch.tanh(_GELU_C * (pre + 0.044715 * pre**3)))
        x = x + act @ params[p + "ffn.w2"] + params[p + "ffn.b2"]
    return F.layer_norm(
        x, (dim,), params["final_ln.g"], params["final_ln.b"], eps=_LN_EPS
    )


def pool(out: torch.Tensor, ids: torch.Tensor) -> torch.Tensor:
    """Mean over non-pad positions, then L2 normalization — w2_train.pool_forward."""
    mask = (ids != 0).to(out.dtype)[:, :, None]
    n = mask.sum(dim=1).clamp_min(1.0)
    pooled = (out * mask).sum(dim=1) / n
    return pooled / pooled.norm(dim=1, keepdim=True).clamp_min(1e-12)


# --- losses ----------------------------------------------------------------


def mlm_batch_loss(
    params: dict[str, torch.Tensor],
    cfg: argparse.Namespace,
    ids: np.ndarray,
    sel: np.ndarray,
    corrupted: np.ndarray,
    device: str,
) -> tuple[torch.Tensor | None, int]:
    n_masked = int(sel.sum())
    if n_masked == 0:
        return None, 0
    corrupted_t = torch.from_numpy(corrupted.astype(np.int64)).to(device)
    sel_t = torch.from_numpy(sel).to(device)
    targets = torch.from_numpy(ids[sel].astype(np.int64)).to(device)
    out = encoder_forward(params, cfg, corrupted_t)
    h = out[sel_t]
    logits = h @ params["emb"].T + params["mlm_bias"]
    return F.cross_entropy(logits.float(), targets), n_masked


def infonce_batch_loss(
    params: dict[str, torch.Tensor],
    cfg: argparse.Namespace,
    pair_ids: np.ndarray,
    device: str,
) -> torch.Tensor:
    batch = pair_ids.shape[0]
    left = torch.from_numpy(pair_ids[:, 0].astype(np.int64)).to(device)
    right = torch.from_numpy(pair_ids[:, 1].astype(np.int64)).to(device)
    zl = pool(encoder_forward(params, cfg, left), left)
    zr = pool(encoder_forward(params, cfg, right), right)
    sims = (zl @ zr.T).float() / cfg.tau
    labels = torch.arange(batch, device=device)
    return 0.5 * (F.cross_entropy(sims, labels) + F.cross_entropy(sims.T, labels))


# --- the run ---------------------------------------------------------------


def _lr_for(base: float, done: int, total: int) -> float:
    return base * max(0.05, 1.0 - done / max(1, total))


def _set_lr(opt: torch.optim.Optimizer, lr: float) -> None:
    for group in opt.param_groups:
        group["lr"] = lr


def _fresh_adam(params: dict[str, torch.Tensor]) -> torch.optim.Optimizer:
    ordered = [params[name] for name in sorted(params)]
    return torch.optim.Adam(ordered, lr=0.0, betas=(0.9, 0.999), eps=1e-8)


def train(args: argparse.Namespace) -> dict[str, object]:
    t_start = time.time()
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = bool(args.tf32)
    torch.backends.cudnn.allow_tf32 = bool(args.tf32)
    dtype = {"fp32": torch.float32, "bf16": torch.bfloat16}[args.dtype]
    device = args.device

    rng = np.random.Generator(np.random.PCG64(args.seed))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cache_path: Path | None = None
    cache_meta: dict[str, object] | None = None
    if args.ci_register is None and args.stage in ("ab", "a"):
        cache_path = Path(args.cache) if args.cache else CACHE_PATH
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
        np_params = unflatten_params(np.load(source / "pretrain.npy"), shapes)
        np.save(out / "pretrain.npy", flatten_params(np_params))
        params = _to_device(np_params, device, dtype)
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
        params = _to_device(init_params(rng, shapes), device, dtype)

        print("stage A: masked-token pretraining...", file=sys.stderr, flush=True)
        chunks = build_chunks(
            bpe, cache_path, args.ci_register, args.token_cap, args.seq
        )
        chunk_count = int(chunks.shape[0])
        mask_id = 1
        adam = _fresh_adam(params)
        mlm_losses = []
        total_batches = args.mlm_epochs * math.ceil(chunk_count / args.mlm_batch)
        done = 0
        for _ in range(args.mlm_epochs):
            for lo in range(0, chunk_count, args.mlm_batch):
                batch_ids = chunks[lo : lo + args.mlm_batch]
                sel, corrupted = draw_corruption(
                    args, batch_ids, rng, mask_id, len(vocab)
                )
                loss, _n_masked = mlm_batch_loss(
                    params, args, batch_ids, sel, corrupted, device
                )
                if loss is not None:
                    adam.zero_grad(set_to_none=True)
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        [params[n] for n in sorted(params)], args.clip
                    )
                    _set_lr(adam, _lr_for(args.mlm_lr, done, total_batches))
                    adam.step()
                done += 1
                if done % 200 == 0 or done == total_batches:
                    loss_val = float(loss.detach()) if loss is not None else 0.0
                    mlm_losses.append(round(loss_val, 4))
                    print(
                        f"  A {done}/{total_batches} loss {loss_val:.4f} "
                        f"({time.time() - t_start:.0f}s)",
                        file=sys.stderr,
                        flush=True,
                    )
        del chunks
        np.save(out / "pretrain.npy", flatten_params(_to_numpy(params)))
        if args.stage == "a":
            return _finish_torch(
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
    adam_b = _fresh_adam(params)
    pair_losses: list[float] = []
    total_batches = args.pair_epochs * math.ceil(n_pairs / args.pair_batch)
    done = 0
    for _ in range(args.pair_epochs):
        order = rng.permutation(n_pairs)
        for lo in range(0, n_pairs, args.pair_batch):
            batch_ids = pair_ids[order[lo : lo + args.pair_batch]]
            if batch_ids.shape[0] < 2:
                continue
            loss = infonce_batch_loss(params, args, batch_ids, device)
            adam_b.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                [params[n] for n in sorted(params)], args.clip
            )
            _set_lr(adam_b, _lr_for(args.pair_lr, done, total_batches))
            adam_b.step()
            done += 1
            if done % 100 == 0 or done == total_batches:
                pair_losses.append(round(float(loss.detach()), 4))
                print(
                    f"  B {done}/{total_batches} loss {float(loss.detach()):.4f} "
                    f"({time.time() - t_start:.0f}s)",
                    file=sys.stderr,
                    flush=True,
                )
    np.save(out / "weights.npy", flatten_params(_to_numpy(params)))
    return _finish_torch(
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


def _finish_torch(
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
    """w2_train._finish's meta layout plus the torch block."""
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
        "numpy_version": np.__version__,
        "torch": {
            "version": torch.__version__,
            "device": args.device,
            "device_name": (
                torch.cuda.get_device_name(0)
                if args.device.startswith("cuda") and torch.cuda.is_available()
                else args.device
            ),
            "dtype": args.dtype,
            "tf32": bool(args.tf32),
            "deterministic_algorithms": True,
            "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
        },
        "wall_seconds": round(time.time() - t_start, 1),
        "sha256": {name: _sha256_file(out / name) for name in files},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(
        description="The F0 torch trainer — the W2 recipe class on an accelerator."
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
        "--cache",
        default=None,
        help="token cache path override (default: w1b_corpus.CACHE_PATH)",
    )
    parser.add_argument("--cache-expect-sha", default=None)
    parser.add_argument("--ci-register", default=None)
    parser.add_argument("--stage", choices=("ab", "a", "b"), default="ab")
    parser.add_argument("--pretrained", default=None)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--dtype", choices=("fp32", "bf16"), default="fp32")
    parser.add_argument(
        "--tf32",
        action="store_true",
        help="allow TF32 matmul (recorded in meta; off = reference fp32 reading)",
    )
    args = parser.parse_args()
    if args.stage == "b" and not args.pretrained:
        parser.error("--stage b requires --pretrained")
    meta = train(args)
    print(json.dumps({k: meta[k] for k in ("tokens_read", "chunks", "wall_seconds")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
