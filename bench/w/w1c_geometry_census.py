"""The W1C geometry census: the expanded cross-form family, both windows.

`bench/w/W1C_DECLARATION.md` §2 fixes everything this script computes
and was committed before any window-15 vector existed. The probe
machinery is imported from the committed `w1b_geometry_probe.py`,
unchanged; the three frozen families are carried byte for byte; and the
EXPANDED cross-form family is derived mechanically from the committed
hand table (`src/bettermemory/expansion.py::SYNONYM_GROUPS`): every
unordered within-group pair whose members map to different stems under
the engine's own stemmer, one surface form per stem (first occurrence
in group order). The derivation selects nothing.

Run (after the window-15 training completes):

    fastvenv/bin/python bench/w/w1c_geometry_census.py \\
        --run-w5 /Volumes/data/bettermemory/runs/w1b-2026-08-18 \\
        --run-w15 /Volumes/data/bettermemory/runs/w1c-2026-08-20 \\
        --out bench/w/results/w1c-geometry-<date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from itertools import combinations
from pathlib import Path

for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent.parent / "src"
for _p in (str(_HERE), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bettermemory.expansion import SYNONYM_GROUPS  # noqa: E402
from bettermemory.search import _stem_token  # noqa: E402

from w1_emit import compose_vectors  # noqa: E402
from w1b_geometry_probe import FAMILIES, probe  # noqa: E402


def expanded_cross_form() -> tuple[tuple[str, str], ...]:
    """The declaration's derivation rule, verbatim: within each group,
    one surface form per stem (first in group order), then every
    unordered pair of distinct stems."""
    pairs: list[tuple[str, str]] = []
    for group in SYNONYM_GROUPS:
        stem_to_surface: dict[str, str] = {}
        for surface in group:
            stem = _stem_token(surface)
            stem_to_surface.setdefault(stem, surface)
        surfaces = list(stem_to_surface.values())
        pairs.extend(combinations(surfaces, 2))
    return tuple(pairs)


def read_run(run_dir: Path, mutual_rank: int) -> dict[str, object]:
    meta = json.loads((run_dir / "meta.json").read_text())
    words, vectors = compose_vectors(run_dir, int(meta["buckets"]), 0)
    families: dict[str, list[dict[str, object]]] = {}
    all_families = tuple(FAMILIES) + (("cross_form_expanded", expanded_cross_form()),)
    for name, pair_list in all_families:
        rows = probe(words, vectors, pair_list)
        for row in rows:
            if row.get("in_vocabulary"):
                row["inside_emission_window"] = bool(
                    int(str(row["rank_left_to_right"])) < mutual_rank
                    and int(str(row["rank_right_to_left"])) < mutual_rank
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
    return {
        "run": str(run_dir),
        "window": meta.get("window"),
        "vectors_sha256": meta["sha256"]["inp.npy"],
        "vocab_size": meta["vocab_size"],
        "fractions": fractions,
        "families": families,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-w5", required=True)
    parser.add_argument("--run-w15", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mutual-rank", type=int, default=64)
    args = parser.parse_args()

    record: dict[str, object] = {
        "declaration": "bench/w/W1C_DECLARATION.md",
        "mutual_rank_window": args.mutual_rank,
        "expanded_pairs": [list(p) for p in expanded_cross_form()],
        "readings": {
            "w5": read_run(Path(args.run_w5), args.mutual_rank),
            "w15": read_run(Path(args.run_w15), args.mutual_rank),
        },
    }

    w15 = record["readings"]["w15"]["fractions"]["cross_form_expanded"]  # type: ignore[index]
    in_vocab = int(w15["in_vocabulary"])
    inside = int(w15["inside"])
    share = inside / in_vocab if in_vocab else 0.0
    morph = record["readings"]["w15"]["fractions"]["morphological"]  # type: ignore[index]
    morph_share = (
        int(morph["inside"]) / int(morph["in_vocabulary"])
        if int(morph["in_vocabulary"])
        else 0.0
    )
    if share >= 0.50:
        outcome = "window-is-the-wall"
    elif share < 0.20 and morph_share >= 0.75:
        outcome = "objective-is-the-wall"
    else:
        outcome = "partial"
    record["readiness"] = {
        "outcome": outcome,
        "expanded_inside_w15": inside,
        "expanded_in_vocab_w15": in_vocab,
        "expanded_share_w15": round(share, 4),
        "morphological_share_w15": round(morph_share, 4),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    for wname in ("w5", "w15"):
        fr = record["readings"][wname]["fractions"]  # type: ignore[index]
        line = ", ".join(
            f"{name} {v['inside']}/{v['in_vocabulary']}" for name, v in fr.items()
        )
        print(f"{wname}: {line}")
    print(f"outcome: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
