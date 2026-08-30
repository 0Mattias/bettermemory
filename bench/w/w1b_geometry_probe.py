"""The W1b geometry probe: what the trained space holds, pair by pair.

A training-internal signal, the class the W1b declaration §6
licenses without limit ("committed neighbor spot-lists"). It grades no
bar and moves no verdict; it exists so the unit's diagnosis is auditable
rather than asserted.

The probe asks one question of the trained vectors: for a named pair of
surface forms, how close are they, and does each reach the other inside
the emission rule's mutual-rank window? Two families are named
deliberately, because the contrast between them IS the finding:

* MORPHOLOGICAL pairs (config/configuration, deploy/deployment) — shared
  character n-grams, and the hand tables already cover this family by
  rule, so `--drop-rule-covered` discards it at emission time.
* CROSS-FORM pairs (toggle/flag, undo/rollback) — the bridges the hand
  `SYNONYM_GROUPS` table earns its keep on, and the ones the dev
  instrument's rescued questions turn on.

Cosine is over the same subword-composed vectors `w1_emit.py` ranks
with, and the rank columns are that vector's position in the other's
full neighbour ordering, so a pair inside the emission window is one
where both ranks fall under the run's `--mutual-rank`.

Run: fastvenv/bin/python bench/w/w1b_geometry_probe.py --run <dir> \
        --out bench/w/results/w1b-geometry-<date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

import numpy as np  # noqa: E402  (env pins must precede numpy)

sys.path.insert(0, str(Path(__file__).parent))

from w1_emit import compose_vectors  # noqa: E402

# The two families, frozen here so the artifact's shape is the probe's
# claim and not a run-time choice.
MORPHOLOGICAL: tuple[tuple[str, str], ...] = (
    ("config", "configuration"),
    ("deploy", "deployment"),
    ("auth", "authentication"),
    ("cache", "caching"),
)
CROSS_FORM: tuple[tuple[str, str], ...] = (
    ("toggle", "flag"),
    ("undo", "rollback"),
    ("revert", "rollback"),
    ("timeout", "expiry"),
    ("flag", "boolean"),
    ("error", "exception"),
)
NEAR_SYNONYM: tuple[tuple[str, str], ...] = (
    ("undo", "revert"),
    ("toggle", "switch"),
    ("toggle", "checkbox"),
    ("bug", "defect"),
    ("database", "db"),
)
FAMILIES = (
    ("morphological", MORPHOLOGICAL),
    ("cross_form", CROSS_FORM),
    ("near_synonym", NEAR_SYNONYM),
)


def probe(
    words: list[str], vectors: np.ndarray, pairs: tuple[tuple[str, str], ...]
) -> list[dict[str, object]]:
    index = {w: i for i, w in enumerate(words)}
    rows: list[dict[str, object]] = []
    for left, right in pairs:
        if left not in index or right not in index:
            rows.append(
                {
                    "left": left,
                    "right": right,
                    "in_vocabulary": False,
                }
            )
            continue
        li, ri = index[left], index[right]
        left_scores = vectors[li] @ vectors.T
        right_scores = vectors[ri] @ vectors.T
        cosine = float(left_scores[ri])
        rows.append(
            {
                "left": left,
                "right": right,
                "in_vocabulary": True,
                "cosine": round(cosine, 4),
                "left_vocab_rank": li,
                "right_vocab_rank": ri,
                "rank_left_to_right": int((left_scores > left_scores[ri]).sum()),
                "rank_right_to_left": int((right_scores > right_scores[li]).sum()),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="trainer --out directory")
    parser.add_argument("--out", required=True, help="probe artifact path")
    parser.add_argument(
        "--mutual-rank",
        type=int,
        default=64,
        help="the emission window this probe reports membership against",
    )
    args = parser.parse_args()

    run_dir = Path(args.run)
    meta = json.loads((run_dir / "meta.json").read_text())
    words, vectors = compose_vectors(run_dir, int(meta["buckets"]), 0)

    families: dict[str, list[dict[str, object]]] = {}
    for name, pairs in FAMILIES:
        rows = probe(words, vectors, pairs)
        for row in rows:
            if row.get("in_vocabulary"):
                row["inside_emission_window"] = bool(
                    int(str(row["rank_left_to_right"])) < args.mutual_rank
                    and int(str(row["rank_right_to_left"])) < args.mutual_rank
                )
        families[name] = rows

    record: dict[str, object] = {
        "probe": {
            "run": str(run_dir),
            "mutual_rank_window": args.mutual_rank,
            "vectors_sha256": meta["sha256"]["inp.npy"],
            "vocab_size": meta["vocab_size"],
        },
        "families": families,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")

    for name, rows in families.items():
        inside = sum(1 for r in rows if r.get("inside_emission_window"))
        print(f"{name}: {inside}/{len(rows)} inside the mutual-rank window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
