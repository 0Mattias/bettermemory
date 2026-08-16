"""The W1 trainer: skip-gram with negative sampling, subword bags, sealed
determinism.

This is the from-scratch trainer `bench/w/W1_DECLARATION.md` §2
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


def iter_source_tokens(cap: int, ci_register: str | None) -> Iterator[list[str]]:
    """The trainer's token source: the pinned register, or the CI slice.

    The CI path exists for G3's reduced-register determinism check —
    one committed text file, blank-line-separated paragraphs as the
    document boundaries, the same tokenizer and cap semantics as the
    real register stream.
    """
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
    cap: int, min_count: int, vocab_cap: int, ci_register: str | None
) -> tuple[list[str], np.ndarray, int]:
    """(words by count-then-lexical order, their counts, total tokens read)."""
    counts: Counter[str] = Counter()
    total = 0
    for doc in iter_source_tokens(cap, ci_register):
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
    cap: int, words: list[str], ci_register: str | None
) -> tuple[np.ndarray, np.ndarray]:
    """(token ids, document ids) for the capped source prefix, OOV dropped."""
    index = {w: i for i, w in enumerate(words)}
    ids: list[np.ndarray] = []
    docs: list[np.ndarray] = []
    for doc_id, doc in enumerate(iter_source_tokens(cap, ci_register)):
        mapped = np.array([index[t] for t in doc if t in index], dtype=np.int32)
        if mapped.size:
            ids.append(mapped)
            docs.append(np.full(mapped.size, doc_id, dtype=np.int32))
    return np.concatenate(ids), np.concatenate(docs)


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
    words, counts, total_read = build_vocab(
        args.token_cap, args.min_count, args.vocab_cap, ci_register
    )
    vocab_size = len(words)
    ptr, bag_idx = build_bags(words, args.buckets)
    stream, stream_docs = materialize_stream(args.token_cap, words, ci_register)

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

    for epoch in range(args.epochs):
        kept_mask = rng.random(stream.size) < keep_prob[stream]
        kept = stream[kept_mask]
        kept_docs = stream_docs[kept_mask]
        widths = rng.integers(1, args.window + 1, size=kept.size)

        centers_parts: list[np.ndarray] = []
        contexts_parts: list[np.ndarray] = []
        for d in range(1, args.window + 1):
            forward = (widths[:-d] >= d) & (kept_docs[:-d] == kept_docs[d:])
            centers_parts.append(np.flatnonzero(forward))
            contexts_parts.append(np.flatnonzero(forward) + d)
            backward = (widths[d:] >= d) & (kept_docs[d:] == kept_docs[:-d])
            centers_parts.append(np.flatnonzero(backward) + d)
            contexts_parts.append(np.flatnonzero(backward))
        center_pos = np.concatenate(centers_parts).astype(np.int32)
        context_pos = np.concatenate(contexts_parts).astype(np.int32)
        del centers_parts, contexts_parts
        order = np.argsort(center_pos, kind="stable")
        center_ids = kept[center_pos[order]]
        context_ids = kept[context_pos[order]]
        del center_pos, context_pos, order

        n_pairs = center_ids.size
        epoch_pairs.append(n_pairs)
        if epoch == 0:
            total_planned = n_pairs * args.epochs
        print(
            f"epoch {epoch}: {n_pairs} pairs ({time.time() - t_start:.0f}s elapsed)",
            file=sys.stderr,
            flush=True,
        )

        for lo in range(0, n_pairs, args.batch):
            if lo and lo % (args.batch * 20_000) == 0:
                print(
                    f"  epoch {epoch}: {lo}/{n_pairs} pairs "
                    f"({time.time() - t_start:.0f}s)",
                    file=sys.stderr,
                    flush=True,
                )
            hi = min(lo + args.batch, n_pairs)
            c_batch = center_ids[lo:hi].astype(np.int64)
            o_batch = context_ids[lo:hi].astype(np.int64)
            batch_n = hi - lo
            negatives = np.searchsorted(
                neg_cum, rng.random((batch_n, args.negatives))
            ).astype(np.int64)

            lens = bag_lens[c_batch]
            cuts = np.concatenate(([0], np.cumsum(lens)[:-1]))
            flat = bag_idx[
                np.repeat(ptr[c_batch], lens)
                + (np.arange(lens.sum()) - np.repeat(cuts, lens))
            ]
            h = np.add.reduceat(inp[flat], cuts, axis=0) / lens[:, None].astype(
                np.float32
            )

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
