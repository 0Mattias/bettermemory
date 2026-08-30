"""Requery census 2: the frozen family, on the instrument that can see it.

The requery-census-2 declaration fixes everything this script
computes and was committed before this file existed. The mechanism is
the parent census's, byte-frozen by IMPORT from its mechanical half
(`bench/requery_census.py`) — the two passes, the engagement gate at
the engine's shipped coverage constant, the pass-2 construction, the
8-cell family and its primary. What changes is instrument-side, per
the declaration: the I1 pair (120 questions / 1,080 documents) and the
shipped prefilter regime — both passes go through
`resolve_search_pool`, so pass 2 earns a fresh nomination.

    .venv/bin/python bench/requery_census2.py \\
        --out retrieval/results/requery-census2-YYYY-MM-DD.json

DEV-ONLY. Nothing under `bench/longmemeval/`, `bench/msc/` or
`bench/heldout/` contributes a byte. The engine is invoked through its
public pooled search path, twice per engaged probe, and nothing under
`src/` changes. The cited I1 artifact is read for one purpose: the
declaration's cross-runner gate — if this script's own pass-1 asked
row disagrees with the committed I1 asked row beyond rounding, the run
stops and the disagreement is the finding.
"""

from __future__ import annotations

import argparse
import json
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
from bettermemory.handlers.search import resolve_search_pool  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

from interval import mcnemar_exact  # noqa: E402
from register_df_census import _doc_tokens, _query_tokens, rr  # noqa: E402
from requery_census import (  # noqa: E402
    DEPTH,
    FS,
    KEPTS,
    MS,
    PRIMARY_CELL,
    _better,
    _cell_name,
    _rank0_of,
    _recalls,
    build_requery,
    engagement,
)

LICENSE_R1 = 0.45
TWITCH_R1 = 0.40
ALPHA = 0.05
I1_ARTIFACT = _HERE / "retrieval" / "results" / "i1-full120-off-2026-08-18.json"


def _pooled_search(store: Store, text: str) -> tuple[list[Any], set[str], bool]:
    """The shipped path, exactly as the I1 runner's prefiltered arm
    drives it: filters None (load-bearing — see run_arm_prefiltered's
    docstring), the pool's own corpus-statistics provider passed
    through. Returns (hits, gold-pool-membership test input, provider
    engagement bit)."""
    pool = resolve_search_pool(
        store,
        text,
        scopes=None,
        excluded_scopes=None,
        repo_filter=None,
        worktree_filter=None,
        min_survivors=DEPTH,
    )
    hits = run_search(
        pool.memories,
        text,
        max_results=DEPTH,
        mode="hybrid",
        rescue_expansion=rr.RESCUE_EXPANSION,
        conversational=rr.CONVERSATIONAL,
        corpus_stats_provider=pool.corpus_stats_provider,
    )
    pool_ids = {m.id for m in pool.memories}
    return hits, pool_ids, pool.corpus_stats_provider is not None


def _mcnemar(pairs: list[tuple[int | None, int | None]], k: int) -> dict[str, Any]:
    """Paired exact read, cell vs pass-1 baseline, at depth k."""

    def hit(r: int | None) -> bool:
        return r is not None and r <= k - 1

    only_base = sum(1 for a, b in pairs if hit(a) and not hit(b))
    only_mech = sum(1 for a, b in pairs if hit(b) and not hit(a))
    return {
        "only_baseline": only_base,
        "only_mechanism": only_mech,
        "p": round(mcnemar_exact(only_base, only_mech), 6),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Requery census 2 — I1 instrument, shipped prefilter regime."
    )
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    questions = rr._read_jsonl(rr.QUESTIONS)
    i1 = json.loads(I1_ARTIFACT.read_text(encoding="utf-8"))
    corpus_sha = rr.corpus_fingerprint(rr.CORPUS)
    if corpus_sha != i1["corpus_sha256"]:
        raise SystemExit(
            "corpus fingerprint does not match the cited I1 artifact — "
            "the instrument moved; re-declare before running."
        )
    i1_asked = next(
        r
        for r in i1["results"]
        if r["arm"] == "lexical" and r["probe"] == "asked" and r["prefilter"]
    )

    root = Path(tempfile.mkdtemp(prefix="bm-requery2-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        store = Store(root)
        memories = store.load_all()
        id_to_set = {m.id: _doc_tokens(m) for m in memories}
        doc_sets = [id_to_set[m.id] for m in memories]

        # Pass 1 once per probe, through the shipped pooled path.
        probes: list[dict[str, Any]] = []
        for q in questions:
            gold_id = slug_to_id[q["slug"]]
            for kind in ("asked", "control"):
                text = rr._query_for(q, kind)
                qtoks = _query_tokens(text)
                hits, pool_ids, provider = _pooled_search(store, text)
                probes.append(
                    {
                        "slug": q["slug"],
                        "probe": kind,
                        "gold_id": gold_id,
                        "qtoks": qtoks,
                        "rank1": _rank0_of(gold_id, hits),
                        "gold_in_pool1": gold_id in pool_ids,
                        "prefilter_engaged1": provider,
                        "engaged": engagement(qtoks, hits),
                        "hit_ids": [h.id for h in hits],
                    }
                )

        baseline = {
            kind: _recalls([p_["rank1"] for p_ in probes if p_["probe"] == kind])
            for kind in ("asked", "control")
        }
        # The declaration's cross-runner gate: this script's asked row
        # must reproduce the committed I1 asked row beyond rounding.
        gate = {
            "i1_asked": {
                "recall_at_1": i1_asked["recall_at_1"],
                "recall_at_5": i1_asked["recall_at_5"],
            },
            "census_asked": {
                "recall_at_1": baseline["asked"]["recall_at_1"],
                "recall_at_5": baseline["asked"]["recall_at_5"],
            },
        }
        if (
            baseline["asked"]["recall_at_1"] != i1_asked["recall_at_1"]
            or baseline["asked"]["recall_at_5"] != i1_asked["recall_at_5"]
        ):
            print(json.dumps({"cross_runner_gate": gate}, indent=2))
            raise SystemExit(
                "cross-runner gate: pass-1 asked row disagrees with the "
                "committed I1 artifact — the disagreement is the finding."
            )

        engaged_counts = {
            kind: sum(1 for p_ in probes if p_["probe"] == kind and p_["engaged"])
            for kind in ("asked", "control")
        }
        gold_in_pool1 = {
            kind: round(
                sum(1 for p_ in probes if p_["probe"] == kind and p_["gold_in_pool1"])
                / max(1, sum(1 for p_ in probes if p_["probe"] == kind)),
                4,
            )
            for kind in ("asked", "control")
        }

        cells: dict[str, Any] = {}
        for kept_axis, f, m in product(KEPTS, FS, MS):
            name = _cell_name(kept_axis, f, m)
            records: list[dict[str, Any]] = []
            ranks_cell: dict[str, list[int | None]] = {"asked": [], "control": []}
            ranks_oracle: dict[str, list[int | None]] = {"asked": [], "control": []}
            pairs_all: dict[str, list[tuple[int | None, int | None]]] = {
                "asked": [],
                "control": [],
            }
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
                    hits2, pool_ids2, _ = _pooled_search(store, q2)
                    rank2 = _rank0_of(p_["gold_id"], hits2)
                    gold_set = id_to_set[p_["gold_id"]]
                    records.append(
                        {
                            "slug": p_["slug"],
                            "probe": kind,
                            "rank1": rank1,
                            "rank2": rank2,
                            "gold_in_pool1": p_["gold_in_pool1"],
                            "gold_in_pool2": p_["gold_id"] in pool_ids2,
                            "kept": kept,
                            "dropped": dropped,
                            "added": added,
                            "added_in_gold": sum(1 for t in added if t in gold_set),
                        }
                    )
                    mech = rank2
                else:
                    mech = rank1
                ranks_cell[kind].append(mech)
                ranks_oracle[kind].append(
                    _better(rank1, mech) if p_["engaged"] else rank1
                )
                pair = (rank1, mech)
                pairs_all[kind].append(pair)
                if rank1 is None or rank1 >= 10:
                    pools["far_absent"][kind].append(pair)
                if rank1 == 0:
                    pools["hit1"][kind].append(pair)

            cells[name] = {
                "mechanism": {k: _recalls(v) for k, v in ranks_cell.items()},
                "oracle_min": {k: _recalls(v) for k, v in ranks_oracle.items()},
                "mcnemar_vs_baseline": {
                    kind: {
                        "at_1": _mcnemar(pairs, 1),
                        "at_5": _mcnemar(pairs, 5),
                    }
                    for kind, pairs in pairs_all.items()
                },
                "pools": {
                    pool: {
                        kind: {
                            "baseline": _recalls([a for a, _ in prs]),
                            "mechanism": _recalls([b for _, b in prs]),
                        }
                        for kind, prs in kinds.items()
                    }
                    for pool, kinds in pools.items()
                },
                "engaged_records": records,
            }
    finally:
        shutil.rmtree(root, ignore_errors=True)

    def _asked(cell: str, key: str) -> float:
        return float(cells[cell]["mechanism"]["asked"][key])

    base_r5 = float(baseline["asked"]["recall_at_5"])

    def _licenses(cell: str) -> bool:
        return (
            _asked(cell, "recall_at_1") >= LICENSE_R1
            and _asked(cell, "recall_at_5") >= base_r5
            and cells[cell]["mcnemar_vs_baseline"]["asked"]["at_1"]["p"] < ALPHA
        )

    def _twitches(cell: str) -> bool:
        return (
            _asked(cell, "recall_at_1") >= TWITCH_R1
            and _asked(cell, "recall_at_5") >= base_r5
        )

    if _licenses(PRIMARY_CELL):
        outcome = "license"
    elif any(_licenses(c) for c in cells):
        outcome = "anti-gate-shopping-follow-up"
    elif any(_twitches(c) for c in cells):
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

    best_cell = max(cells, key=lambda c: _asked(c, "recall_at_1"))
    payload = {
        "provenance": {
            "generated": date.today().isoformat(),
            "git_commit": commit,
            "bettermemory_version": bettermemory.__version__,
            "declaration": "bench/REQUERY_CENSUS_2_DECLARATION.md",
            "parent_census": "bench/retrieval/results/requery-census-2026-08-13.json",
        },
        "corpus": rr.CORPUS.name,
        "corpus_sha256": corpus_sha,
        "collection_size": size,
        "depth": DEPTH,
        "coverage_gate": engine._RESCUE_COVERAGE_GATE,
        "primary_cell": PRIMARY_CELL,
        "regime": {
            "prefilter": True,
            "rescue_expansion": rr.RESCUE_EXPANSION,
            "conversational": rr.CONVERSATIONAL,
            "index_threshold": rr.INDEX_THRESHOLD,
        },
        "note": (
            "DEV-ONLY census, the declared follow-up to the 2026-08-13 "
            "park. Shipped prefilter regime on both passes — pass 2 "
            "re-enters resolve_search_pool and earns a fresh nomination. "
            "Engaged probes take pass 2 wholesale; no acceptance rule "
            "exists in any cell; oracle_min bounds what any rule could "
            "buy."
        ),
        "cross_runner_gate": gate,
        "baseline_pass1": baseline,
        "engaged": engaged_counts,
        "gold_in_pool_pass1": gold_in_pool1,
        "probes_pass1": [
            {
                "slug": p_["slug"],
                "probe": p_["probe"],
                "rank1": p_["rank1"],
                "gold_in_pool1": p_["gold_in_pool1"],
                "engaged": p_["engaged"],
                "content_tokens": len(p_["qtoks"]),
            }
            for p_ in probes
        ],
        "cells": cells,
        "readiness": {
            "outcome": outcome,
            "primary_asked_recall_at_1": round(_asked(PRIMARY_CELL, "recall_at_1"), 4),
            "primary_asked_recall_at_5": round(_asked(PRIMARY_CELL, "recall_at_5"), 4),
            "primary_mcnemar_at_1_p": cells[PRIMARY_CELL]["mcnemar_vs_baseline"][
                "asked"
            ]["at_1"]["p"],
            "baseline_asked_recall_at_5": round(base_r5, 4),
            "best_cell": best_cell,
            "best_cell_asked_recall_at_1": round(_asked(best_cell, "recall_at_1"), 4),
            "license_bar": LICENSE_R1,
            "twitch_bar": TWITCH_R1,
            "alpha": ALPHA,
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
