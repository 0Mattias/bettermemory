"""Requery census: two-pass feedback requery on the dev instrument.

`bench/REQUERY_CENSUS_DECLARATION.md` fixes everything this script
computes — the 8-cell family (kept x F x M), the engagement gate at
the engine's own shipped coverage constant, both passes' reads, the
oracle-min diagnostic, and the license/twitch/park ladder — and was
committed before this file existed. This is the mechanical half.

    .venv/bin/python bench/requery_census.py \\
        --out retrieval/results/requery-census-YYYY-MM-DD.json

DEV-ONLY. Nothing under `bench/longmemeval/`, `bench/msc/` or
`bench/heldout/` contributes a byte to this artifact; the imports
below pull committed helper FUNCTIONS from sibling bench modules,
and no code path here opens either uncommitted corpus. The engine
is invoked through its public search entry point, twice per engaged
probe, and nothing under `src/` changes.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory  # noqa: E402
import bettermemory.search as engine  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

# Committed helpers, imported rather than mirrored: the register
# census's one-pass df counter and engine-pipeline token readers, and
# the dev runner (as the register census loads it) for store and
# probe construction. Module import executes no corpus access.
from register_df_census import _doc_tokens, _query_tokens, rr, term_df  # noqa: E402

DEPTH = 50
KEPTS = ("all", "hooked")
FS = (3, 5)
MS = (5, 10)
PRIMARY_CELL = "all_f3_m5"
LICENSE_R1 = 0.50
TWITCH_R1 = 0.45


def _cell_name(kept: str, f: int, m: int) -> str:
    return f"{kept}_f{f}_m{m}"


def _rank0_of(gold_id: str, hits: list[Any]) -> int | None:
    for i, h in enumerate(hits):
        if h.id == gold_id:
            return i
    return None


def _better(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _recalls(ranks: list[int | None]) -> dict[str, float | int]:
    n = len(ranks)
    return {
        "n": n,
        "recall_at_1": round(sum(1 for r in ranks if r == 0) / n, 4) if n else 0.0,
        "recall_at_5": (
            round(sum(1 for r in ranks if r is not None and r <= 4) / n, 4)
            if n
            else 0.0
        ),
    }


def engagement(qtoks: list[str], hits: list[Any]) -> bool:
    """The declaration's gate: the engine's own coverage arithmetic at
    its shipped constant. An empty result engages by definition."""
    unique = set(qtoks)
    if not unique:
        return False
    if not hits:
        return True
    covered = len(set(hits[0].match_terms) & unique)
    return (covered / len(unique)) < engine._RESCUE_COVERAGE_GATE


def build_requery(
    qtoks: list[str],
    fb_sets: list[set[str]],
    doc_sets: list[set[str]],
    *,
    kept_axis: str,
    m: int,
) -> tuple[list[str], list[str], list[str]]:
    """(kept, dropped, added) for one engaged probe and one cell."""
    if kept_axis == "hooked":
        kept = [t for t in qtoks if any(t in s for s in fb_sets)]
        if not kept:
            kept = list(qtoks)
    else:
        kept = list(qtoks)
    dropped = [t for t in qtoks if t not in set(kept)]

    candidates = sorted(set().union(*fb_sets) - set(qtoks)) if fb_sets else []
    df = term_df(doc_sets, candidates)
    n = len(doc_sets)
    scored = sorted(
        (
            (-(sum(1 for s in fb_sets if t in s) * math.log(n / df[t])), t)
            for t in candidates
            if df[t] > 0
        ),
    )
    added = [t for _, t in scored[:m]]
    return kept, dropped, added


def main() -> int:
    p = argparse.ArgumentParser(
        description="Two-pass feedback requery census. Dev instrument only."
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Write the census here. A RELATIVE path resolves against this "
            "script's own directory (bench/), matching the other runners."
        ),
    )
    args = p.parse_args()

    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-requery-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    id_to_set = {m.id: _doc_tokens(m) for m in memories}
    doc_sets = [id_to_set[m.id] for m in memories]

    # Pass 1 once per probe: ranks, engagement, feedback material.
    probes: list[dict[str, Any]] = []
    for q in questions:
        gold_id = slug_to_id[q["slug"]]
        for kind in ("asked", "control"):
            text = rr._query_for(q, kind)
            qtoks = _query_tokens(text)
            hits = run_search(
                memories,
                text,
                max_results=DEPTH,
                mode="hybrid",
                rescue_expansion=False,
            )
            probes.append(
                {
                    "slug": q["slug"],
                    "probe": kind,
                    "gold_id": gold_id,
                    "qtoks": qtoks,
                    "rank1": _rank0_of(gold_id, hits),
                    "engaged": engagement(qtoks, hits),
                    "hit_ids": [h.id for h in hits],
                }
            )

    baseline = {
        kind: _recalls([p_["rank1"] for p_ in probes if p_["probe"] == kind])
        for kind in ("asked", "control")
    }
    engaged_counts = {
        kind: sum(1 for p_ in probes if p_["probe"] == kind and p_["engaged"])
        for kind in ("asked", "control")
    }

    cells: dict[str, Any] = {}
    for kept_axis, f, m in product(KEPTS, FS, MS):
        name = _cell_name(kept_axis, f, m)
        records: list[dict[str, Any]] = []
        ranks_cell: dict[str, list[int | None]] = {"asked": [], "control": []}
        ranks_oracle: dict[str, list[int | None]] = {"asked": [], "control": []}
        pools: dict[str, dict[str, list[tuple[int | None, int | None]]]] = {
            "far_absent": {"asked": [], "control": []},
            "hit1": {"asked": [], "control": []},
        }
        for p_ in probes:
            kind = p_["probe"]
            rank1 = p_["rank1"]
            if p_["engaged"]:
                fb_sets = [id_to_set[i] for i in p_["hit_ids"][:f]]
                kept, dropped, added = build_requery(
                    p_["qtoks"], fb_sets, doc_sets, kept_axis=kept_axis, m=m
                )
                q2 = " ".join(kept + added)
                hits2 = run_search(
                    memories,
                    q2,
                    max_results=DEPTH,
                    mode="hybrid",
                    rescue_expansion=False,
                )
                rank2 = _rank0_of(p_["gold_id"], hits2)
                gold_set = id_to_set[p_["gold_id"]]
                records.append(
                    {
                        "slug": p_["slug"],
                        "probe": kind,
                        "rank1": rank1,
                        "rank2": rank2,
                        "kept": kept,
                        "dropped": dropped,
                        "added": added,
                        "added_in_gold": sum(1 for t in added if t in gold_set),
                    }
                )
                mech = rank2
            else:
                mech = rank1
                rank2 = None
            ranks_cell[kind].append(mech)
            ranks_oracle[kind].append(_better(rank1, rank2) if p_["engaged"] else rank1)
            pair = (rank1, mech)
            if rank1 is None or rank1 >= 10:
                pools["far_absent"][kind].append(pair)
            if rank1 == 0:
                pools["hit1"][kind].append(pair)

        cells[name] = {
            "mechanism": {k: _recalls(v) for k, v in ranks_cell.items()},
            "oracle_min": {k: _recalls(v) for k, v in ranks_oracle.items()},
            "pools": {
                pool: {
                    kind: {
                        "baseline": _recalls([a for a, _ in pairs]),
                        "mechanism": _recalls([b for _, b in pairs]),
                    }
                    for kind, pairs in kinds.items()
                }
                for pool, kinds in pools.items()
            },
            "engaged_records": records,
        }

    def _asked_r1(cell: str) -> float:
        return float(cells[cell]["mechanism"]["asked"]["recall_at_1"])

    primary_r1 = _asked_r1(PRIMARY_CELL)
    primary_r5 = float(cells[PRIMARY_CELL]["mechanism"]["asked"]["recall_at_5"])
    base_r5 = float(baseline["asked"]["recall_at_5"])
    best_cell = max(cells, key=_asked_r1)
    best_r1 = _asked_r1(best_cell)
    if primary_r1 >= LICENSE_R1 and primary_r5 >= base_r5:
        outcome = "license"
    elif best_r1 >= LICENSE_R1:
        outcome = "anti-gate-shopping-follow-up"
    elif best_r1 >= TWITCH_R1:
        outcome = "twitch"
    else:
        outcome = "park"

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_HERE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    payload = {
        "provenance": {
            "generated": date.today().isoformat(),
            "git_commit": commit,
            "bettermemory_version": bettermemory.__version__,
            "declaration": "bench/REQUERY_CENSUS_DECLARATION.md",
        },
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "collection_size": size,
        "depth": DEPTH,
        "coverage_gate": engine._RESCUE_COVERAGE_GATE,
        "primary_cell": PRIMARY_CELL,
        "note": (
            "DEV-ONLY census. Full-corpus in-process ranking (the pre-3.30 "
            "arm shape); the prefilter regime is out of scope. Engaged "
            "probes take pass 2 wholesale — no acceptance rule exists in "
            "any cell; oracle_min bounds what any rule could buy."
        ),
        "baseline_pass1": baseline,
        "engaged": engaged_counts,
        "probes_pass1": [
            {
                "slug": p_["slug"],
                "probe": p_["probe"],
                "rank1": p_["rank1"],
                "engaged": p_["engaged"],
                "content_tokens": len(p_["qtoks"]),
            }
            for p_ in probes
        ],
        "cells": cells,
        "readiness": {
            "outcome": outcome,
            "primary_asked_recall_at_1": round(primary_r1, 4),
            "primary_asked_recall_at_5": round(primary_r5, 4),
            "baseline_asked_recall_at_5": round(base_r5, 4),
            "best_cell": best_cell,
            "best_cell_asked_recall_at_1": round(best_r1, 4),
            "license_bar": LICENSE_R1,
            "twitch_bar": TWITCH_R1,
        },
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
