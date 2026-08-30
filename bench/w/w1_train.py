"""The W1 trainer: skip-gram with negative sampling, subword bags, sealed
determinism.

This is the from-scratch trainer the W1 declaration §2
declares: this repository's own code, no third-party weights anywhere,
numpy as the declared bench-side build dependency. Every stochastic
choice — subsampling, window widths, negative draws, initialization —
comes from ONE seeded `numpy.random.Generator` consumed in a fixed
order, and every reduction that touches float32 accumulation runs
through a stable-sort + `reduceat` path rather than hardware-order
scatter, so a retrain from the pinned register reproduces the emitted
bytes exactly. BLAS is pinned to one thread before numpy is imported;
the process is single-threaded end to end.

Model shape (declared defaults, overridable per the declaration's
tuning protocol; the run artifact records the values used): input
matrix holds one row per vocabulary word plus 2^19 hashed rows for
character 3-5-grams of ``<word>``; a word's vector is the MEAN of its
bag (own row + n-gram rows), fastText-style, which is what lets
"config" and "configuration" share geometry through shared prefixes.
The context matrix is word-rows only. Training emits both-direction
skip-gram pairs inside per-position dynamic windows that never cross
a document boundary, with the standard unigram^0.75 negative table
and the standard subsample-discard rule.

Outputs land in ``--out``: ``inp.npy`` (the vectors blob G3 hashes),
``ctx.npy``, ``vocab.txt``, and ``meta.json`` (sorted keys) carrying
hyperparameters, the token/pair counts actually processed, and the
sha256 of each sibling file. `w1_emit.py` composes vectors and emits
the neighbor table from these files; nothing at inference time ever
loads them.

W1b's declared revision (the W1b declaration §2): each
epoch's pair set is enumerated in fixed-size segments of the kept
stream instead of all at once, holding the update mathematics, the
batch schedule, the decay arithmetic, the RNG consumption order, and
the global pair order fixed — segments are consecutive center ranges,
each sorted ascending-center with the same tie order the monolithic
enumeration produced, and batches are carried across segment edges so
the batch decomposition is invariant to the segment size. ``--segment``
is a memory knob, never a result knob; the CI leg asserts the emitted
bytes do not move under it. ``--w1b`` selects the W1B register slice:
the derived token cache is rebuilt from the pinned bytes at the start
of every run and its sha256 lands in the run meta.
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
import hashlib  # noqa: E402  (env pins must precede numpy)
import json  # noqa: E402  (env pins must precede numpy)
import sys  # noqa: E402  (env pins must precede numpy)
import time  # noqa: E402  (env pins must precede numpy)
from collections import Counter  # noqa: E402  (env pins must precede numpy)
from pathlib import Path  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

sys.path.insert(0, str(Path(__file__).parent))

from w1_corpus import iter_register_tokens, tokenize  # noqa: E402
from w1b_corpus import build_cache, iter_cache_docs  # noqa: E402

from collections.abc import Iterator  # noqa: E402  (env pins must precede numpy)

_NGRAM_LO = 3
_NGRAM_HI = 5
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = (1 << 64) - 1


def _fnv1a(data: bytes) -> int:
    """FNV-1a 64-bit — our own stable hash; Python's hash() is salted."""
    h = _FNV_OFFSET
    for byte in data:
        h = ((h ^ byte) * _FNV_PRIME) & _MASK64
    return h


def word_ngrams(word: str) -> list[str]:
    wrapped = f"<{word}>"
    return [
        wrapped[i : i + n]
        for n in range(_NGRAM_LO, _NGRAM_HI + 1)
        for i in range(len(wrapped) - n + 1)
    ]


def iter_source_tokens(
    cap: int, ci_register: str | None, cache: Path | None = None
) -> Iterator[list[str]]:
    """The trainer's token source: the register, the CI slice, or the cache.

    The CI path exists for G3's reduced-register determinism check —
    one committed text file, blank-line-separated paragraphs as the
    document boundaries, the same tokenizer and cap semantics as the
    real register stream. The cache path is W1b's: pre-tokenized
    documents written by `w1b_corpus.build_cache`, the cap applied
    identically (the cache already embeds the declared budgets, so the
    cap binds only as a guard).
    """
    if cache is not None:
        remaining = cap
        for tokens in iter_cache_docs(cache):
            if len(tokens) >= remaining:
                yield tokens[:remaining]
                return
            remaining -= len(tokens)
            yield tokens
        return
    if ci_register is None:
        yield from iter_register_tokens(cap)
        return
    remaining = cap
    text = Path(ci_register).read_text(encoding="utf-8")
    for paragraph in text.split("\n\n"):
        tokens = tokenize(paragraph)
        if not tokens:
            continue
        if len(tokens) >= remaining:
            yield tokens[:remaining]
            return
        remaining -= len(tokens)
        yield tokens


def build_vocab(
    cap: int,
    min_count: int,
    vocab_cap: int,
    ci_register: str | None,
    cache: Path | None = None,
) -> tuple[list[str], np.ndarray, int]:
    """(words by count-then-lexical order, their counts, total tokens read)."""
    counts: Counter[str] = Counter()
    total = 0
    for doc in iter_source_tokens(cap, ci_register, cache):
        counts.update(doc)
        total += len(doc)
    kept = sorted(
        ((w, c) for w, c in counts.items() if c >= min_count),
        key=lambda item: (-item[1], item[0]),
    )[:vocab_cap]
    words = [w for w, _ in kept]
    return words, np.array([c for _, c in kept], dtype=np.int64), total


def build_bags(words: list[str], buckets: int) -> tuple[np.ndarray, np.ndarray]:
    """CSR (ptr, idx) of each word's input rows: own row + hashed n-grams."""
    ptr = np.zeros(len(words) + 1, dtype=np.int64)
    idx: list[int] = []
    for i, word in enumerate(words):
        rows = [i] + [
            len(words) + _fnv1a(g.encode()) % buckets for g in word_ngrams(word)
        ]
        idx.extend(rows)
        ptr[i + 1] = len(idx)
    return ptr, np.array(idx, dtype=np.int64)


def materialize_stream(
    cap: int, words: list[str], ci_register: str | None, cache: Path | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """(token ids, document ids) for the capped source prefix, OOV dropped."""
    index = {w: i for i, w in enumerate(words)}
    ids: list[np.ndarray] = []
    docs: list[np.ndarray] = []
    for doc_id, doc in enumerate(iter_source_tokens(cap, ci_register, cache)):
        mapped = np.array([index[t] for t in doc if t in index], dtype=np.int32)
        if mapped.size:
            ids.append(mapped)
            docs.append(np.full(mapped.size, doc_id, dtype=np.int32))
    return np.concatenate(ids), np.concatenate(docs)


# Fixed block length for the per-epoch RNG draws. A code constant, not
# a knob: chunking the draws bounds peak memory without changing the
# values drawn (the generator fills sequentially), and it must never
# vary with --segment or the invariance contract would be meaningless.
_RNG_BLOCK = 1 << 24


def _segment_add(matrix: np.ndarray, rows: np.ndarray, grads: np.ndarray) -> None:
    """matrix[rows] += grads with a fixed accumulation order.

    Stable-sort the row ids, sum duplicate rows' gradients with
    ``reduceat`` (left-to-right in sorted order), then one fancy-index
    add on unique rows — the same result as ``np.add.at`` but with an
    accumulation order the retrain reproduces exactly, and several
    times faster on batches this size.
    """
    order = np.argsort(rows, kind="stable")
    sorted_rows = rows[order]
    boundaries = np.flatnonzero(
        np.concatenate(([True], sorted_rows[1:] != sorted_rows[:-1]))
    )
    summed = np.add.reduceat(grads[order], boundaries, axis=0)
    matrix[sorted_rows[boundaries]] += summed


def train(args: argparse.Namespace) -> dict[str, object]:
    t_start = time.time()
    rng = np.random.Generator(np.random.PCG64(args.seed))

    ci_register = getattr(args, "ci_register", None)
    cache_meta: dict[str, object] | None = None
    cache_path: Path | None = None
    if getattr(args, "w1b", False):
        print(
            "building the W1b token cache from the pinned register...",
            file=sys.stderr,
            flush=True,
        )
        cache_meta = build_cache()
        cache_path = Path(str(cache_meta["path"]))

    words, counts, total_read = build_vocab(
        args.token_cap, args.min_count, args.vocab_cap, ci_register, cache_path
    )
    vocab_size = len(words)
    ptr, bag_idx = build_bags(words, args.buckets)
    stream, stream_docs = materialize_stream(
        args.token_cap, words, ci_register, cache_path
    )

    inp = (
        rng.random((vocab_size + args.buckets, args.dim), dtype=np.float32) - 0.5
    ) / args.dim
    ctx = np.zeros((vocab_size, args.dim), dtype=np.float32)

    freq = counts / counts.sum()
    neg_cum = np.cumsum(freq**0.75)
    neg_cum /= neg_cum[-1]
    keep_prob = np.minimum(1.0, np.sqrt(args.subsample / freq) + args.subsample / freq)

    bag_lens = np.diff(ptr)
    pairs_done = 0
    total_planned = 0
    epoch_pairs: list[int] = []

    def step(c_ids: np.ndarray, o_ids: np.ndarray) -> None:
        """One SGNS batch update — W1's body, byte for byte in effect."""
        nonlocal pairs_done
        c_batch = c_ids.astype(np.int64)
        o_batch = o_ids.astype(np.int64)
        batch_n = c_batch.size
        negatives = np.searchsorted(
            neg_cum, rng.random((batch_n, args.negatives))
        ).astype(np.int64)

        lens = bag_lens[c_batch]
        cuts = np.concatenate(([0], np.cumsum(lens)[:-1]))
        flat = bag_idx[
            np.repeat(ptr[c_batch], lens)
            + (np.arange(lens.sum()) - np.repeat(cuts, lens))
        ]
        h = np.add.reduceat(inp[flat], cuts, axis=0) / lens[:, None].astype(np.float32)

        targets = np.concatenate([o_batch[:, None], negatives], axis=1)
        labels = np.zeros((batch_n, 1 + args.negatives), dtype=np.float32)
        labels[:, 0] = 1.0
        ctx_rows = ctx[targets]
        logits = np.clip(np.einsum("bd,bkd->bk", h, ctx_rows), -30.0, 30.0)
        sig = 1.0 / (1.0 + np.exp(-logits))

        alpha = args.lr * max(1e-4, 1.0 - pairs_done / max(1, total_planned))
        g = (labels - sig) * alpha

        g_h = np.einsum("bk,bkd->bd", g, ctx_rows)
        g_ctx = g[:, :, None] * h[:, None, :]
        _segment_add(ctx, targets.ravel(), g_ctx.reshape(-1, args.dim))
        _segment_add(
            inp,
            flat,
            np.repeat(g_h / lens[:, None].astype(np.float32), lens, axis=0),
        )
        pairs_done += batch_n

    seg = args.segment
    for epoch in range(args.epochs):
        # Per-epoch draws in W1's order — keep-mask, then widths, then
        # per-batch negatives inside step() — chunked at the fixed
        # block so peak memory stays bounded at any corpus scale.
        kept_mask = np.empty(stream.size, dtype=bool)
        for lo in range(0, stream.size, _RNG_BLOCK):
            hi = min(lo + _RNG_BLOCK, stream.size)
            kept_mask[lo:hi] = rng.random(hi - lo) < keep_prob[stream[lo:hi]]
        kept = stream[kept_mask]
        kept_docs = stream_docs[kept_mask]
        del kept_mask
        widths = np.empty(kept.size, dtype=np.int8)
        for lo in range(0, kept.size, _RNG_BLOCK):
            hi = min(lo + _RNG_BLOCK, kept.size)
            widths[lo:hi] = rng.integers(1, args.window + 1, size=hi - lo).astype(
                np.int8
            )

        # Counting pass: the epoch's pair total before any update, so
        # the decay arithmetic sees exactly what the enumeration emits.
        n_pairs = 0
        for d in range(1, args.window + 1):
            if kept.size > d:
                n_pairs += int(
                    ((widths[:-d] >= d) & (kept_docs[:-d] == kept_docs[d:])).sum()
                )
                n_pairs += int(
                    ((widths[d:] >= d) & (kept_docs[d:] == kept_docs[:-d])).sum()
                )
        epoch_pairs.append(n_pairs)
        if epoch == 0:
            total_planned = n_pairs * args.epochs
        print(
            f"epoch {epoch}: {n_pairs} pairs ({time.time() - t_start:.0f}s elapsed)",
            file=sys.stderr,
            flush=True,
        )

        # Segmented enumeration: consecutive center ranges, each sorted
        # ascending-center with the monolithic version's tie order
        # (d=1 forward, d=1 backward, d=2 forward, ...), batches carried
        # across segment edges so the batch decomposition — and with it
        # every negative draw and decay step — is invariant to seg.
        done_in_epoch = 0
        next_report = args.batch * 20_000
        pend_c = np.empty(0, dtype=np.int32)
        pend_o = np.empty(0, dtype=np.int32)
        for s in range(0, kept.size, seg):
            e = min(s + seg, kept.size)
            parts_c: list[np.ndarray] = []
            parts_o: list[np.ndarray] = []
            for d in range(1, args.window + 1):
                hi = min(e, kept.size - d)
                if hi > s:
                    forward = (widths[s:hi] >= d) & (
                        kept_docs[s:hi] == kept_docs[s + d : hi + d]
                    )
                    pos = (np.flatnonzero(forward) + s).astype(np.int32)
                    parts_c.append(pos)
                    parts_o.append(pos + d)
                lo_c = max(s, d)
                if e > lo_c:
                    backward = (widths[lo_c:e] >= d) & (
                        kept_docs[lo_c:e] == kept_docs[lo_c - d : e - d]
                    )
                    pos = (np.flatnonzero(backward) + lo_c).astype(np.int32)
                    parts_c.append(pos)
                    parts_o.append(pos - d)
            if not parts_c:
                continue
            c_pos = np.concatenate(parts_c)
            o_pos = np.concatenate(parts_o)
            del parts_c, parts_o
            order = np.argsort(c_pos, kind="stable")
            seg_c = kept[c_pos[order]]
            seg_o = kept[o_pos[order]]
            del c_pos, o_pos, order
            if pend_c.size:
                seg_c = np.concatenate([pend_c, seg_c])
                seg_o = np.concatenate([pend_o, seg_o])
            n_full = (seg_c.size // args.batch) * args.batch
            for lo_b in range(0, n_full, args.batch):
                step(seg_c[lo_b : lo_b + args.batch], seg_o[lo_b : lo_b + args.batch])
                done_in_epoch += args.batch
                if done_in_epoch >= next_report:
                    print(
                        f"  epoch {epoch}: {done_in_epoch}/{n_pairs} pairs "
                        f"({time.time() - t_start:.0f}s)",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_report += args.batch * 20_000
            pend_c = seg_c[n_full:].copy()
            pend_o = seg_o[n_full:].copy()
            del seg_c, seg_o
        if pend_c.size:
            step(pend_c, pend_o)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "inp.npy", inp)
    np.save(out / "ctx.npy", ctx)
    (out / "vocab.txt").write_text("\n".join(words) + "\n")

    def _sha(name: str) -> str:
        return hashlib.sha256((out / name).read_bytes()).hexdigest()

    meta: dict[str, object] = {
        "seed": args.seed,
        "token_cap": args.token_cap,
        "tokens_read": total_read,
        "vocab_size": vocab_size,
        "dim": args.dim,
        "buckets": args.buckets,
        "window": args.window,
        "negatives": args.negatives,
        "epochs": args.epochs,
        "min_count": args.min_count,
        "vocab_cap": args.vocab_cap,
        "subsample": args.subsample,
        "lr": args.lr,
        "batch": args.batch,
        "ci_register": ci_register,
        "segment": args.segment,
        "w1b_cache": cache_meta,
        "epoch_pairs": epoch_pairs,
        "pairs_done": pairs_done,
        "numpy_version": np.__version__,
        "wall_seconds": round(time.time() - t_start, 1),
        "sha256": {
            "inp.npy": _sha("inp.npy"),
            "ctx.npy": _sha("ctx.npy"),
            "vocab.txt": _sha("vocab.txt"),
        },
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(
        description="The W1 SGNS trainer over the pinned register."
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260816)
    parser.add_argument("--token-cap", type=int, default=50_000_000)
    parser.add_argument("--dim", type=int, default=100)
    parser.add_argument("--buckets", type=int, default=2**19)
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--negatives", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--min-count", type=int, default=10)
    parser.add_argument("--vocab-cap", type=int, default=75_000)
    parser.add_argument("--subsample", type=float, default=1e-4)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--batch", type=int, default=8_192)
    parser.add_argument(
        "--ci-register",
        default=None,
        help="G3 reduced-register mode: train from this text file "
        "instead of the pinned register",
    )
    parser.add_argument(
        "--w1b",
        action="store_true",
        help="train from the W1b register slice; rebuilds the derived "
        "token cache from the pinned bytes before reading",
    )
    parser.add_argument(
        "--segment",
        type=int,
        default=8_000_000,
        help="epoch enumeration segment length in kept-stream tokens; "
        "a memory knob the emitted bytes are invariant to",
    )
    meta = train(parser.parse_args())
    print(
        json.dumps(
            {
                k: meta[k]
                for k in ("tokens_read", "vocab_size", "pairs_done", "wall_seconds")
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
