"""Emit the W1 neighbor table from a trained run directory.

The emission step of `bench/w/W1_DECLARATION.md` §4: compose one
vector per vocabulary word (mean of its bag — own row plus hashed
n-gram rows, the same composition the trainer optimized), find nearest
neighbors by cosine over the whole vocabulary, and keep only pairs
that clear every declared filter. The output is a generated Python
module in the exact idiom of the hand tables it would replace — a
readable ``SURFACE_NEIGHBORS`` dict of surface forms — plus a run
JSON carrying the emission parameters and the sha256 of everything.

Filters, in order (declared defaults; finals recorded in the JSON):

- cosine floor: a neighbor must score at least ``--floor``;
- mutuality: each must rank within ``--mutual-rank`` of the other's
  neighbor list — one-directional attraction is how promiscuous hub
  words sneak in;
- the leg's own hygiene, mirrored at emission so the committed table
  never carries an entry the leg would discard: both words pass the
  engine's live stemmer, clear the leg's minimum emitted length, and
  neither stems into `QUERY_FILLER_WORDS`;
- identity-after-stem: a pair whose two words stem to the same token
  adds no vocabulary the query lacks (that is `morph_variants`' job)
  and is dropped;
- head-term budget: entries are admitted in vocabulary frequency
  order (most frequent head word first) until ``--max-entries``.

Everything is deterministic: ties in the neighbor ranking are broken
by vocabulary index, and the generated module renders sorted.
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
import hashlib  # noqa: E402  (env pins must precede numpy)
import json  # noqa: E402  (env pins must precede numpy)
import sys  # noqa: E402  (env pins must precede numpy)
from pathlib import Path  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from w1_train import build_bags  # noqa: E402

from bettermemory.expansion import build_tables, morph_variants  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402

_MIN_EMIT_LEN = 3  # mirrors expansion._MIN_EXPANSION_LEN at the leg


def compose_vectors(
    run_dir: Path, buckets: int, remove_components: int = 0
) -> tuple[list[str], np.ndarray]:
    """(vocab words, L2-normalized composed vectors, vocab order).

    The composed set is mean-centered before normalization, always:
    skip-gram training grows a large common direction shared by every
    vector (measured on this trainer's own smoke runs, where it held
    every pairwise cosine near 1.0 and buried the signal), and
    removing it is the standard post-fit correction. Setting
    ``remove_components`` additionally projects out that many top
    principal directions via a deterministic single-thread SVD — an
    emission parameter under the declaration's tuning protocol,
    recorded in the run JSON.
    """
    words = (run_dir / "vocab.txt").read_text().splitlines()
    inp = np.load(run_dir / "inp.npy")
    ptr, bag_idx = build_bags(words, buckets)
    lens = np.diff(ptr).astype(np.float32)
    sums = np.add.reduceat(inp[bag_idx], ptr[:-1], axis=0)
    composed = sums / lens[:, None]
    composed = composed - composed.mean(axis=0)
    if remove_components:
        _, _, vt = np.linalg.svd(composed, full_matrices=False)
        top = vt[:remove_components]
        composed = composed - (composed @ top.T) @ top
    norms = np.linalg.norm(composed, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return words, (composed / norms).astype(np.float32)


def topk_neighbors(
    vectors: np.ndarray, k: int, chunk: int = 2048
) -> tuple[np.ndarray, np.ndarray]:
    """(indices, cosines) of each row's top-k neighbors, self excluded.

    Chunked matmul over the normalized matrix; ties broken by index
    via a stable final sort so the emission is deterministic.
    """
    n = vectors.shape[0]
    all_idx = np.empty((n, k), dtype=np.int64)
    all_cos = np.empty((n, k), dtype=np.float32)
    for lo in range(0, n, chunk):
        hi = min(lo + chunk, n)
        scores = vectors[lo:hi] @ vectors.T
        scores[np.arange(hi - lo), np.arange(lo, hi)] = -2.0
        part = np.argpartition(-scores, k, axis=1)[:, :k]
        part_scores = np.take_along_axis(scores, part, axis=1)
        order = np.lexsort((part, -part_scores), axis=1)
        all_idx[lo:hi] = np.take_along_axis(part, order, axis=1)
        all_cos[lo:hi] = np.take_along_axis(part_scores, order, axis=1)
    return all_idx, all_cos


def emit(args: argparse.Namespace) -> dict[str, object]:
    run_dir = Path(args.run)
    meta = json.loads((run_dir / "meta.json").read_text())
    words, vectors = compose_vectors(
        run_dir, int(meta["buckets"]), args.remove_components
    )

    tables = build_tables(_stem_token)
    stems = [_stem_token(w) for w in words]
    eligible = np.array(
        [
            len(s) >= _MIN_EMIT_LEN and s not in tables.filler_stems and w.isalpha()
            for w, s in zip(words, stems)
        ]
    )

    idx, cos = topk_neighbors(vectors, args.mutual_rank)

    neighbors: dict[str, list[str]] = {}
    for head in range(args.head_min_rank, len(words)):
        if not eligible[head]:
            continue
        rule_covered = (
            morph_variants(stems[head], _stem_token)
            if args.drop_rule_covered
            else frozenset()
        )
        kept: list[str] = []
        for pos in range(args.mutual_rank):
            other = int(idx[head, pos])
            if cos[head, pos] < args.floor:
                break
            if not eligible[other]:
                continue
            if stems[other] == stems[head]:
                continue
            if stems[other] in rule_covered:
                continue
            if head not in idx[other, : args.mutual_rank]:
                continue
            kept.append(words[other])
            if len(kept) == args.per_term:
                break
        if kept:
            neighbors[words[head]] = kept
        if len(neighbors) == args.max_entries:
            break

    # Renders in the repo formatter's own canonical shape so the
    # committed artifact's bytes are exactly what this emitter wrote
    # and the recorded sha256 stays true through the format gate: a
    # one-element tuple collapses to a single line (its trailing comma
    # is syntax, not a magic comma), multi-element tuples explode one
    # neighbor per line.
    table_lines = ["SURFACE_NEIGHBORS: dict[str, tuple[str, ...]] = {"]
    for head in sorted(neighbors):
        row = neighbors[head]
        if len(row) == 1:
            table_lines.append(f'    "{head}": ("{row[0]}",),')
        else:
            table_lines.append(f'    "{head}": (')
            for word in row:
                table_lines.append(f'        "{word}",')
            table_lines.append("    ),")
    table_lines.append("}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        '"""W1 learned neighbor table — GENERATED by bench/w/w1_emit.py.\n'
        "\n"
        "Derived artifact of the run this file's sibling JSON records;\n"
        "regenerate it from the pinned register, never edit it. Surface\n"
        "forms in the hand-table idiom: consumers stem through\n"
        "`expansion.build_tables` exactly like `SYNONYM_GROUPS`.\n"
        '"""\n\n'
    )
    out.write_text(header + "\n".join(table_lines) + "\n")

    table_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    record = {
        "run_meta": meta,
        "emission": {
            "floor": args.floor,
            "remove_components": args.remove_components,
            "drop_rule_covered": args.drop_rule_covered,
            "head_min_rank": args.head_min_rank,
            "mutual_rank": args.mutual_rank,
            "per_term": args.per_term,
            "max_entries": args.max_entries,
            "entries": len(neighbors),
            "table_path": str(out),
            "table_sha256": table_sha,
        },
    }
    record_path = out.with_suffix(".json")
    record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit the W1 neighbor table from a trained run."
    )
    parser.add_argument("--run", required=True, help="trainer --out directory")
    parser.add_argument("--out", required=True, help="generated table module path")
    parser.add_argument("--floor", type=float, default=0.60)
    parser.add_argument("--remove-components", type=int, default=0)
    parser.add_argument(
        "--drop-rule-covered",
        action="store_true",
        help="drop neighbors morph_variants already generates at query time",
    )
    parser.add_argument(
        "--head-min-rank",
        type=int,
        default=0,
        help="skip heads above this vocabulary frequency rank (hub words)",
    )
    parser.add_argument("--mutual-rank", type=int, default=8)
    parser.add_argument("--per-term", type=int, default=4)
    parser.add_argument("--max-entries", type=int, default=5_000)
    record = emit(parser.parse_args())
    emission = record["emission"]
    assert isinstance(emission, dict)
    print(json.dumps(emission))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
