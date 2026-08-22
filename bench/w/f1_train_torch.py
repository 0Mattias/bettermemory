"""F1 stage-P trainer — a ~150M own-architecture MLM encoder on packed shards.

The unit's backbone: 22 pre-norm transformer layers, hidden 768, GeGLU
feed-forward, rotary positions, alternating attention (global every
third layer, sliding-window 128 otherwise), no biases, no dropout,
masked-language-model objective at a 30% masking rate with the
standard 80/10/10 corruption split. Everything is trained from
scratch; no borrowed weights anywhere.

Reproducibility is the F-receipts tier: one run, every input pinned
(shard manifest + tokenizer sha), every random draw derived from
counter-based streams so the derivation is replayable — parameter
init from one PCG64 in sorted-name order, and per-sequence source
choice and masking from Philox streams keyed by the global sequence
index, so a resumed run consumes exactly the draws a straight run
would. torch owns no randomness. Deterministic kernels are pinned the
way the F0 probes validated: ``use_deterministic_algorithms``, cuBLAS
workspace config, SDPA restricted to the efficient/math backends,
eager mode, TF32 off.

The dataloader reads uint16 shards memory-mapped in manifest order,
one sequential read pointer per source; each sequence's source is
drawn from the declared mixture table (probability proportional to
unique tokens times the declared repeat weight). Checkpoints carry
weights, optimizer state, the pointers, and the step — resume is
exact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import numpy as np
import torch

# Declared mixture: repeat weight per source. Sampling probability is
# proportional to (unique tokens in the packed subset) x (this weight).
REPEAT_WEIGHTS: dict[str, float] = {
    "stackexchange": 3.0,
    "wikimedia": 2.5,
    "ubuntu_irc": 4.0,
    "youtube": 2.0,
    "wikiteam": 2.0,
    "github_archive": 2.0,
    "cccc": 2.0,
    "stackv2_edu": 1.5,
    "stackv2_html": 1.5,
    "peS2o": 1.5,
    "pubmed": 1.5,
    "arxiv_papers": 1.5,
    "arxiv_abstracts": 1.5,
    "doab": 1.0,
    "project_gutenberg": 1.0,
    "pre_1929_books": 1.0,
    "caselaw_access_project": 1.0,
    "library_of_congress": 1.0,
    "biodiversity_heritage_library": 1.0,
    "usgpo": 1.0,
    "uspto": 1.0,
    "regulations": 1.0,
    "uk_hansard": 1.0,
    "news": 1.0,
    "libretexts": 1.0,
    "oercommons": 1.0,
    "pressbooks": 1.0,
    "public_domain_review": 1.0,
    "foodista": 1.0,
    "python_enhancement_proposals": 1.0,
    "data_provenance_initiative": 1.0,
}

MASK_RATE = 0.30
KEEP_SPLIT = (0.8, 0.1, 0.1)  # [MASK] / random token / keep
PAD_ID, UNK_ID, CLS_ID, SEP_ID, MASK_ID, EOS_ID = range(6)
N_SPECIALS = 6


def _pin_determinism() -> None:
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
    torch.backends.cuda.enable_math_sdp(True)


# ---------------------------------------------------------------- model


class Config:
    def __init__(self, args: argparse.Namespace, vocab_size: int) -> None:
        self.vocab = vocab_size
        self.hidden = args.hidden
        self.layers = args.layers
        self.heads = args.heads
        self.head_dim = args.hidden // args.heads
        self.ffn = args.ffn
        self.seq = args.seq
        self.window = args.window
        self.global_every = args.global_every
        self.rope_theta = args.rope_theta


def _rope_cache(cfg: Config, device: torch.device) -> tuple[torch.Tensor, torch.Tensor]:
    half = cfg.head_dim // 2
    freqs = cfg.rope_theta ** (
        -torch.arange(0, half, dtype=torch.float32, device=device) / half
    )
    pos = torch.arange(cfg.seq, dtype=torch.float32, device=device)
    ang = torch.outer(pos, freqs)
    return torch.cos(ang), torch.sin(ang)


def _apply_rope(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    # x: (batch, heads, seq, head_dim); rotate pairs (even, odd).
    x1, x2 = x[..., 0::2], x[..., 1::2]
    out = torch.empty_like(x)
    out[..., 0::2] = x1 * cos - x2 * sin
    out[..., 1::2] = x1 * sin + x2 * cos
    return out


def init_params(cfg: Config, seed: int) -> dict[str, torch.Tensor]:
    rng = np.random.default_rng(np.random.PCG64(seed))
    shapes: dict[str, tuple[int, ...]] = {"emb": (cfg.vocab, cfg.hidden)}
    for i in range(cfg.layers):
        shapes[f"l{i:02d}.attn_qkv"] = (cfg.hidden, 3 * cfg.hidden)
        shapes[f"l{i:02d}.attn_out"] = (cfg.hidden, cfg.hidden)
        shapes[f"l{i:02d}.ffn_in"] = (cfg.hidden, 2 * cfg.ffn)
        shapes[f"l{i:02d}.ffn_out"] = (cfg.ffn, cfg.hidden)
        shapes[f"l{i:02d}.norm1"] = (cfg.hidden,)
        shapes[f"l{i:02d}.norm2"] = (cfg.hidden,)
    shapes["final_norm"] = (cfg.hidden,)
    shapes["head_dense"] = (cfg.hidden, cfg.hidden)
    shapes["head_norm"] = (cfg.hidden,)
    shapes["head_bias"] = (cfg.vocab,)
    params: dict[str, torch.Tensor] = {}
    for name in sorted(shapes):
        shape = shapes[name]
        if name.endswith(("norm1", "norm2", "final_norm", "head_norm")):
            arr = np.ones(shape, dtype=np.float32)
        elif name == "head_bias":
            arr = np.zeros(shape, dtype=np.float32)
        else:
            arr = rng.normal(0.0, 0.02, size=shape).astype(np.float32)
        params[name] = torch.from_numpy(arr)
    return params


def _norm(x: torch.Tensor, weight: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.layer_norm(x, (x.shape[-1],), weight=weight, eps=1e-5)


def forward_trunk(
    params: dict[str, torch.Tensor],
    cfg: Config,
    ids: torch.Tensor,
    masks: dict[str, torch.Tensor],
    rope: tuple[torch.Tensor, torch.Tensor],
) -> torch.Tensor:
    cos, sin = rope
    x = torch.nn.functional.embedding(ids, params["emb"])
    for i in range(cfg.layers):
        p = f"l{i:02d}."
        h = _norm(x, params[p + "norm1"])
        qkv = h @ params[p + "attn_qkv"]
        b, s, _ = qkv.shape
        qkv = qkv.view(b, s, 3, cfg.heads, cfg.head_dim).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        q = _apply_rope(q, cos, sin)
        k = _apply_rope(k, cos, sin)
        mask = masks["global" if i % cfg.global_every == 0 else "local"]
        att = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=mask)
        att = att.permute(0, 2, 1, 3).reshape(b, s, cfg.hidden)
        x = x + att @ params[p + "attn_out"]
        h = _norm(x, params[p + "norm2"])
        gate_up = h @ params[p + "ffn_in"]
        gate, up = gate_up.chunk(2, dim=-1)
        x = x + (torch.nn.functional.gelu(gate) * up) @ params[p + "ffn_out"]
    return _norm(x, params["final_norm"])


def mlm_loss(
    params: dict[str, torch.Tensor],
    hidden: torch.Tensor,
    labels: torch.Tensor,
    chunk: int = 16384,
) -> torch.Tensor:
    """Cross-entropy over the masked positions only, head applied in chunks.

    Projecting every position against the full vocabulary materializes a
    seq x vocab logits tensor measured in tens of GB; selecting the ~30%
    masked rows first and slicing the head keeps the peak small. The
    slice order is fixed, so the summed loss is the declared computation.
    """
    flat_labels = labels.view(-1)
    sel = flat_labels != -100
    h = hidden.view(-1, hidden.shape[-1])[sel]
    y = flat_labels[sel]
    h = _norm(torch.nn.functional.gelu(h @ params["head_dense"]), params["head_norm"])
    total = torch.zeros((), device=hidden.device, dtype=torch.float32)
    for start in range(0, h.shape[0], chunk):
        logits = (
            h[start : start + chunk] @ params["emb"].T + params["head_bias"]
        ).float()
        total = total + torch.nn.functional.cross_entropy(
            logits, y[start : start + chunk], reduction="sum"
        )
    return total / y.shape[0]


def attention_masks(cfg: Config, device: torch.device) -> dict[str, torch.Tensor]:
    idx = torch.arange(cfg.seq, device=device)
    dist = (idx[None, :] - idx[:, None]).abs()
    local = dist <= cfg.window // 2
    return {"global": None, "local": local[None, None, :, :]}


# ------------------------------------------------------------- data


class MixtureReader:
    """Sequential per-source pointers over memory-mapped shards, mixture-sampled."""

    def __init__(self, shard_dir: Path, manifest: dict, seq: int, seed: int) -> None:
        self.seq = seq
        self.seed = seed
        self.sources: list[str] = []
        self.streams: dict[str, list[np.memmap]] = {}
        self.sizes: dict[str, list[int]] = {}
        self.totals: dict[str, int] = {}
        per_source: dict[str, list[dict]] = {}
        for shard in manifest["shards"]:
            per_source.setdefault(shard["source"], []).append(shard)
        for source in sorted(per_source):
            shards = sorted(per_source[source], key=lambda s: s["output"])
            maps = [np.memmap(s["output"], dtype=np.uint16, mode="r") for s in shards]
            self.sources.append(source)
            self.streams[source] = maps
            self.sizes[source] = [m.size for m in maps]
            self.totals[source] = sum(self.sizes[source])
        weights = np.array(
            [self.totals[s] * REPEAT_WEIGHTS[s] for s in self.sources],
            dtype=np.float64,
        )
        self.probs = weights / weights.sum()
        self.cum = np.cumsum(self.probs)
        self.pointers: dict[str, int] = {s: 0 for s in self.sources}

    def _read(self, source: str, n: int) -> np.ndarray:
        out = np.empty(n, dtype=np.uint16)
        got = 0
        pos = self.pointers[source]
        total = self.totals[source]
        while got < n:
            pos %= total
            acc = 0
            for m, size in zip(self.streams[source], self.sizes[source]):
                if pos < acc + size:
                    off = pos - acc
                    take = min(n - got, size - off)
                    out[got : got + take] = m[off : off + take]
                    got += take
                    pos += take
                    break
                acc += size
        self.pointers[source] = pos % total
        return out

    def sequence(self, seq_index: int) -> tuple[np.ndarray, str]:
        rng = np.random.Generator(np.random.Philox(key=self.seed, counter=seq_index))
        u = rng.random()
        source = self.sources[int(np.searchsorted(self.cum, u, side="right"))]
        return self._read(source, self.seq), source


def corrupt(
    tokens: np.ndarray, seq_index: int, seed: int, vocab: int
) -> tuple[np.ndarray, np.ndarray]:
    """30% MLM corruption, 80/10/10, drawn from a per-sequence Philox stream."""
    rng = np.random.Generator(np.random.Philox(key=seed, counter=seq_index))
    maskable = tokens >= N_SPECIALS
    u = rng.random(tokens.size)
    chosen = maskable & (u < MASK_RATE)
    labels = np.where(chosen, tokens.astype(np.int64), -100)
    kind = rng.random(tokens.size)
    corrupted = tokens.copy()
    to_mask = chosen & (kind < KEEP_SPLIT[0])
    to_rand = chosen & (kind >= KEEP_SPLIT[0]) & (kind < KEEP_SPLIT[0] + KEEP_SPLIT[1])
    corrupted[to_mask] = MASK_ID
    rand_ids = rng.integers(N_SPECIALS, vocab, size=int(to_rand.sum()), dtype=np.int64)
    corrupted[to_rand] = rand_ids.astype(np.uint16)
    return corrupted, labels


# ---------------------------------------------------------- training


def _lr_at(step: int, args: argparse.Namespace) -> float:
    if step < args.warmup:
        return args.lr * (step + 1) / args.warmup
    decay_start = int(args.steps * 0.9)
    if step < decay_start:
        return args.lr
    frac = (step - decay_start) / max(1, args.steps - decay_start)
    return args.lr * max(0.01, 1.0 - math.sqrt(frac))


def _checkpoint(
    out_dir: Path,
    step: int,
    params: dict[str, torch.Tensor],
    opt: torch.optim.Optimizer,
    reader: MixtureReader,
    keep: int,
) -> None:
    ckpt_dir = out_dir / "ckpt"
    ckpt_dir.mkdir(exist_ok=True)
    path = ckpt_dir / f"step{step:07d}.pt"
    tmp = path.with_suffix(".tmp")
    torch.save(
        {
            "step": step,
            "params": {k: v.detach().cpu() for k, v in params.items()},
            "opt": opt.state_dict(),
            "pointers": dict(reader.pointers),
        },
        tmp,
    )
    os.replace(tmp, path)
    kept = sorted(ckpt_dir.glob("step*.pt"))
    for old in kept[:-keep]:
        if int(old.stem.removeprefix("step")) % 10000 != 0:
            old.unlink()


def train(args: argparse.Namespace) -> int:
    _pin_determinism()
    device = torch.device(args.device)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    tok_meta = json.loads(Path(args.tokenizer_manifest).read_text())
    vocab = int(tok_meta["vocab_size"])
    manifest = json.loads((Path(args.shards) / "MANIFEST.json").read_text())

    cfg = Config(args, vocab)
    params = init_params(cfg, args.seed)
    reader = MixtureReader(Path(args.shards), manifest, cfg.seq, args.seed + 1)

    start_step = 0
    resume_state = None
    resume = sorted((out_dir / "ckpt").glob("step*.pt")) if args.resume else []
    if resume:
        resume_state = torch.load(resume[-1], map_location="cpu", weights_only=False)
        params = resume_state["params"]
        reader.pointers.update(resume_state["pointers"])
        start_step = resume_state["step"]
        print(f"resuming from {resume[-1]} at step {start_step}", flush=True)

    for k in params:
        params[k] = params[k].to(device).requires_grad_(True)
    no_decay = [
        params[k]
        for k in sorted(params)
        if k.endswith(("norm1", "norm2", "final_norm", "head_norm", "head_bias"))
    ]
    decay = [
        params[k]
        for k in sorted(params)
        if not k.endswith(("norm1", "norm2", "final_norm", "head_norm", "head_bias"))
    ]
    opt = torch.optim.AdamW(
        [
            {"params": decay, "weight_decay": args.weight_decay},
            {"params": no_decay, "weight_decay": 0.0},
        ],
        lr=args.lr,
        betas=(0.9, 0.98),
        eps=1e-6,
    )
    if resume_state is not None:
        opt.load_state_dict(resume_state["opt"])
        del resume_state
    masks = attention_masks(cfg, device)
    rope = _rope_cache(cfg, device)
    names = sorted(params)

    run_meta = {
        "unit": "F1 stage P",
        "config": {k: v for k, v in vars(args).items()},
        "vocab": vocab,
        "tokenizer_sha256": tok_meta["tokenizer_sha256"],
        "shards_total_tokens": manifest["total_tokens"],
        "mixture_probs": {s: float(p) for s, p in zip(reader.sources, reader.probs)},
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "device_name": torch.cuda.get_device_name(0)
        if device.type == "cuda"
        else "cpu",
        "param_count": int(sum(p.numel() for p in params.values())),
    }
    (out_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=1) + "\n")
    metrics = (out_dir / "metrics.jsonl").open("a")

    seqs_per_step = args.micro_batch * args.accum
    t0 = time.time()
    for step in range(start_step, args.steps):
        lr = _lr_at(step, args)
        for group in opt.param_groups:
            group["lr"] = lr
        opt.zero_grad(set_to_none=True)
        step_loss = 0.0
        for micro in range(args.accum):
            base = step * seqs_per_step + micro * args.micro_batch
            toks = np.empty((args.micro_batch, cfg.seq), dtype=np.uint16)
            labs = np.empty((args.micro_batch, cfg.seq), dtype=np.int64)
            for j in range(args.micro_batch):
                raw, _ = reader.sequence(base + j)
                toks[j], labs[j] = corrupt(raw, base + j, args.seed + 2, vocab)
            ids = torch.from_numpy(toks.astype(np.int64)).to(device)
            labels = torch.from_numpy(labs).to(device)
            with torch.autocast(
                "cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"
            ):
                hidden = forward_trunk(params, cfg, ids, masks, rope)
                loss = mlm_loss(params, hidden, labels)
            (loss / args.accum).backward()
            step_loss += float(loss.detach()) / args.accum
        torch.nn.utils.clip_grad_norm_([params[k] for k in names], 1.0)
        opt.step()

        if (step + 1) % args.log_every == 0:
            elapsed = time.time() - t0
            done = step + 1 - start_step
            tok_s = done * seqs_per_step * cfg.seq / max(elapsed, 1e-9)
            line = {
                "step": step + 1,
                "loss": round(step_loss, 4),
                "lr": lr,
                "tok_per_s": int(tok_s),
                "elapsed_s": int(elapsed),
            }
            metrics.write(json.dumps(line) + "\n")
            metrics.flush()
            print(json.dumps(line), flush=True)
        if (step + 1) % args.ckpt_every == 0 or step + 1 == args.steps:
            _checkpoint(out_dir, step + 1, params, opt, reader, keep=3)
        if args.max_hours and (time.time() - t0) / 3600 > args.max_hours:
            _checkpoint(out_dir, step + 1, params, opt, reader, keep=3)
            print(f"wall ceiling reached at step {step + 1}; parked", flush=True)
            return 0

    weights = {k: params[k].detach().cpu().numpy() for k in names}
    np.savez(out_dir / "weights_p.npz", **weights)
    blob = (out_dir / "weights_p.npz").read_bytes()
    print(
        f"stage P complete; weights sha256 {hashlib.sha256(blob).hexdigest()}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True)
    parser.add_argument("--tokenizer-manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=20260822)
    parser.add_argument("--hidden", type=int, default=768)
    parser.add_argument("--layers", type=int, default=22)
    parser.add_argument("--heads", type=int, default=12)
    parser.add_argument("--ffn", type=int, default=1152)
    parser.add_argument("--seq", type=int, default=1024)
    parser.add_argument("--window", type=int, default=128)
    parser.add_argument("--global-every", type=int, default=3)
    parser.add_argument("--rope-theta", type=float, default=10000.0)
    parser.add_argument("--micro-batch", type=int, default=256)
    parser.add_argument("--accum", type=int, default=18)
    parser.add_argument("--steps", type=int, default=55000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--lr", type=float, default=8e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-5)
    parser.add_argument("--log-every", type=int, default=50)
    parser.add_argument("--ckpt-every", type=int, default=1000)
    parser.add_argument("--max-hours", type=float, default=0.0)
    parser.add_argument("--resume", action="store_true")
    return train(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
