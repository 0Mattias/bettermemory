"""The W2 geometry census: the trained encoder's term space, pair by pair.

The W2 declaration §6: a term's vector is the encoder's
pooled output on the bare surface form; the term inventory is the
words of the pretraining token cache at min-count 10 capped at
150,000 (W1b's vocabulary rule, carried); the pair enumeration and
the emission-window criterion are imported from the committed probes
(`w1b_geometry_probe.FAMILIES`, `w1c_geometry_census.expanded_cross_form`,
mutual-rank window unchanged). Training-internal — it grades no bar
and moves no verdict; the control row (timeout/expiry, unsupported in
the training data) is read by the record against the expectation the
declaration states in advance.

Run (after stage B):

    fastvenv/bin/python bench/w/w2_geometry_probe.py \\
        --run <trainer --out dir> \\
        --out bench/w/results/w2-geometry-<date>.json
"""

from __future__ import annotations

import os

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
import sys  # noqa: E402  (env pins must precede numpy)
from collections import Counter  # noqa: E402  (env pins must precede numpy)
from pathlib import Path  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

sys.path.insert(0, str(Path(__file__).parent))

from w1b_corpus import CACHE_PATH  # noqa: E402
from w1b_geometry_probe import FAMILIES, probe  # noqa: E402
from w1c_geometry_census import expanded_cross_form  # noqa: E402
from w2_train import (  # noqa: E402
    W2Encoder,
    encoder_forward,
    iter_cache_words,
    pool_forward,
)

# W1b's vocabulary rule, carried: min-count 10, the 150,000 most
# frequent words, count-then-lexical order.
_MIN_COUNT = 10
_VOCAB_CAP = 150_000


def term_inventory(
    cache_path: Path | None, ci_register: str | None, cap: int
) -> list[str]:
    counts: Counter[str] = Counter()
    for tokens in iter_cache_words(cache_path, ci_register, cap):
        counts.update(tokens)
    kept = sorted(
        ((w, c) for w, c in counts.items() if c >= _MIN_COUNT),
        key=lambda item: (-item[1], item[0]),
    )[:_VOCAB_CAP]
    return [w for w, _ in kept]


def term_vectors(encoder: W2Encoder, words: list[str], batch: int) -> np.ndarray:
    """Pooled encoder outputs on bare surface forms, padded per batch."""
    out = np.empty((len(words), encoder.cfg.dim), dtype=np.float32)
    for lo in range(0, len(words), batch):
        chunk = words[lo : lo + batch]
        encoded = [encoder.bpe.encode_word(w) or (encoder.bpe.unk_id,) for w in chunk]
        width = min(encoder.cfg.seq, max(len(e) for e in encoded))
        ids = np.zeros((len(chunk), width), dtype=np.int32)
        for i, e in enumerate(encoded):
            ids[i, : min(width, len(e))] = e[:width]
        hidden, _ = encoder_forward(encoder.params, encoder.cfg, ids)
        out[lo : lo + len(chunk)] = pool_forward(hidden, ids)[0]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="trainer --out directory")
    parser.add_argument("--out", required=True, help="probe artifact path")
    parser.add_argument("--mutual-rank", type=int, default=64)
    parser.add_argument("--batch", type=int, default=2048)
    parser.add_argument(
        "--token-cap",
        type=int,
        default=200_000_000,
        help="inventory pass cap — the trainer's declared default",
    )
    parser.add_argument(
        "--ci-register",
        default=None,
        help="reduced mode: inventory from this text file instead of the cache",
    )
    args = parser.parse_args()

    run_dir = Path(args.run)
    meta = json.loads((run_dir / "meta.json").read_text())
    encoder = W2Encoder.load(run_dir)

    cache_path = None if args.ci_register else CACHE_PATH
    words = term_inventory(cache_path, args.ci_register, args.token_cap)
    print(f"inventory: {len(words)} terms", file=sys.stderr, flush=True)
    vectors = term_vectors(encoder, words, args.batch)

    families: dict[str, list[dict[str, object]]] = {}
    all_families = tuple(FAMILIES) + (("cross_form_expanded", expanded_cross_form()),)
    for name, pair_list in all_families:
        rows = probe(words, vectors, pair_list)
        for row in rows:
            if row.get("in_vocabulary"):
                row["inside_emission_window"] = bool(
                    int(str(row["rank_left_to_right"])) < args.mutual_rank
                    and int(str(row["rank_right_to_left"])) < args.mutual_rank
                )
        families[name] = rows

    fractions = {
        name: {
            "in_vocabulary": sum(1 for r in rows if r.get("in_vocabulary")),
            "inside": sum(1 for r in rows if r.get("inside_emission_window")),
            "total": len(rows),
        }
        for name, rows in families.items()
    }
    record: dict[str, object] = {
        "declaration": "bench/w/W2_DECLARATION.md",
        "probe": {
            "run": str(run_dir),
            "mutual_rank_window": args.mutual_rank,
            "weights_sha256": meta["sha256"]["weights.npy"],
            "inventory_terms": len(words),
            "min_count": _MIN_COUNT,
            "vocab_cap": _VOCAB_CAP,
        },
        "fractions": fractions,
        "families": families,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    for name, fr in fractions.items():
        print(f"{name}: {fr['inside']}/{fr['in_vocabulary']} inside the window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
