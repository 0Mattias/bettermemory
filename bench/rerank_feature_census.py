"""P2a feature census: does anything a rerank could use order gold above
the distractors that outrank it?

The P2A census declaration fixes everything this script computes
— the feature family, every direction, the pair rule, both corpora's
pools, and the readiness criterion — and was committed before this ran.
This file is the mechanical half: it replays the shipped engine over
the dev bench and LongMemEval, reads gold-vs-distractor feature pairs
off the fusion's own inputs, and tabulates. No feature weight is
fitted, no ranking changes, no engine code is touched.

Self-checking by construction: the recomputed dev gold ranks must match
`bench/retrieval/results/base-leg-labels-2026-08-12.json` and the
recomputed LongMemEval evidence ranks must match
`bench/longmemeval/results/per-question/round9-off-pq-2026-08-12.json`,
or the run FAILS — the round-9 reproduction property used as a
guardrail rather than re-earned trust.

    .venv/bin/python bench/rerank_feature_census.py \\
        --out retrieval/results/rerank-feature-census-YYYY-MM-DD.json

Statistics only. Dev-side plus the committed-corpus LongMemEval copy;
no file under bench/heldout/ is read.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.handlers.search import resolve_search_pool  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402

from embed_train import load_bench_module  # noqa: E402

# The declaration's constants, verbatim. Windows are 1-indexed gold
# ranks ("2..8" = the seven ranks behind the top hit); DEPTH_DEV is the
# labels artifact's observation window, used only to translate the
# recomputed full-depth ranks into that artifact's convention for the
# self-check.
WINDOWS = (5, 8, 10)
PRIMARY_WINDOW = 8
DEPTH_DEV = 50
ELIGIBLE = (
    "leg_agreement",
    "best_leg_rank",
    "evidence_max",
    "evidence_sum",
    "coverage",
)
R1_SHARE = 0.60
R1_MIN_PAIRS = 25
R2_SHARE = 0.50
R2_MIN_PAIRS = 50
RECENCY_SHARE = 0.60
RECENCY_MIN_PAIRS = 50

DEV_LABELS = _HERE / "retrieval" / "results" / "base-leg-labels-2026-08-12.json"
LME_SIDECAR = (
    _HERE / "longmemeval" / "results" / "per-question" / "round9-off-pq-2026-08-12.json"
)

# The runner writes "[<date>]\n<body>"; the corpus's dates are
# zero-padded, so lexicographic order on the bracket contents is
# chronological. Any deviation voids the recency read (recorded, not
# skipped) per the declaration.
_DATE_PREFIX = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \(\w{3}\) \d{2}:\d{2})\]")


@contextlib.contextmanager
def _fusion_tap() -> Generator[dict[str, Any]]:
    """Capture the base pair's inputs AND fused output inside `search()`.

    Same match rule as `bench/base_leg_census.py`: with the rescue lane
    off, the weightless two-leg call is the base fusion, exactly once
    per search.
    """
    real = engine._hybrid_fuse
    tap: dict[str, Any] = {"rankings": None, "fused": None}

    def tapped(
        rankings: list[list[tuple[Memory, float, list[str]]]],
        *,
        rrf_k: int,
        weights: list[float] | None = None,
    ) -> list[tuple[Memory, float, list[str]]]:
        out = real(rankings, rrf_k=rrf_k, weights=weights)
        if weights is None and len(rankings) == 2:
            tap["rankings"] = rankings
            tap["fused"] = out
        return out

    engine._hybrid_fuse = tapped
    try:
        yield tap
    finally:
        engine._hybrid_fuse = real


def _query_unique(text: str) -> int:
    raw = engine._expand_kebab(engine.tokenize(text))
    return len(set(engine._strip_stopwords(raw)))


def _candidate_features(
    rankings: list[list[tuple[Memory, float, list[str]]]],
    fused: list[tuple[Memory, float, list[str]]],
    uniq: int,
) -> list[dict[str, Any]]:
    """The declaration's feature table, per fused candidate, in fused order."""
    leg_rank: list[dict[str, int]] = []
    leg_evidence: list[dict[str, int]] = []
    leg_sizes: list[int] = []
    for leg in rankings:
        order = engine._id_order(leg)
        leg_rank.append({mid: i for i, mid in enumerate(order)})
        leg_evidence.append({m.id: len(terms) for m, _, terms in leg})
        leg_sizes.append(len(leg))

    out: list[dict[str, Any]] = []
    for pos, (memory, score, _terms) in enumerate(fused):
        ranks = [leg_rank[i].get(memory.id, leg_sizes[i]) for i in range(len(rankings))]
        present = [memory.id in leg_rank[i] for i in range(len(rankings))]
        evidence = [leg_evidence[i].get(memory.id, 0) for i in range(len(rankings))]
        ev_max = max(evidence)
        out.append(
            {
                "fused_rank": pos,
                "fused_score": round(score, 6),
                "leg_agreement": all(present),
                "best_leg_rank": min(ranks),
                "evidence_max": ev_max,
                "evidence_sum": sum(evidence),
                "coverage": round(ev_max / uniq, 4),
                "length_tokens": len(engine.tokenize(memory.body)),
                "_body": memory.body,
            }
        )
    return out


def _compare(feature: str, gold: dict[str, Any], other: dict[str, Any]) -> str:
    """Win / tie / loss under the declaration's per-feature direction."""
    g, d = gold[feature], other[feature]
    if feature == "leg_agreement":
        if g and not d:
            return "win"
        if d and not g:
            return "loss"
        return "tie"
    if feature == "best_leg_rank":
        if g < d:
            return "win"
        if g == d:
            return "tie"
        return "loss"
    if g > d:
        return "win"
    if g == d:
        return "tie"
    return "loss"


def _strip_features(feats: dict[str, Any]) -> dict[str, Any]:
    """The artifact copy: no content, no per-build identifiers."""
    return {k: v for k, v in feats.items() if not k.startswith("_")}


def _near_miss_record(
    gold: dict[str, Any], above: list[dict[str, Any]], top_score: float
) -> dict[str, Any]:
    return {
        "gold_rank": gold["fused_rank"] + 1,  # 1-indexed in the artifact
        "gold": _strip_features(gold),
        "distractors_above": [_strip_features(d) for d in above],
        "deficit_to_top": round(top_score - gold["fused_score"], 6),
        "deficit_to_above": round(above[-1]["fused_score"] - gold["fused_score"], 6),
    }


def _stratum(rank: int | None) -> str:
    if rank is None:
        return "absent"
    if rank == 0:
        return "hit@1"
    if rank <= 4:
        return "near(2-5)"
    if rank <= 9:
        return "mid(6-10)"
    return "far(11+)"


# ---------------------------------------------------------------------------
# Dev instrument
# ---------------------------------------------------------------------------


def _dev_expected(labels_path: Path) -> dict[str, dict[tuple[str, str], int | None]]:
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    out: dict[str, dict[tuple[str, str], int | None]] = {}
    for regime, arm in labels["arms"].items():
        expected: dict[tuple[str, str], int | None] = {}
        for record in arm["records"]:
            expected[(record["slug"], record["probe"])] = record["legs"]["keyword"][
                "gold_rank_with_leg"
            ]
        out[regime] = expected
    return out


def dev_arm(
    rr: Any, pad_to: int | None, expected: dict[tuple[str, str], int | None]
) -> dict[str, Any]:
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-p2acensus-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=pad_to)
        memories = Store(root).load_all()
        records: dict[str, list[dict[str, Any]]] = {
            "asked": [],
            "requery": [],
            "control": [],
        }
        strata: dict[str, dict[str, int]] = {
            p: {} for p in ("asked", "requery", "control")
        }
        ranks: dict[tuple[str, str], int | None] = {}
        for q in questions:
            gold_id = slug_to_id[q["slug"]]
            for probe in ("asked", "requery", "control"):
                query = rr._query_for(q, probe)
                uniq = _query_unique(query)
                if not uniq:
                    raise SystemExit(f"empty token stream: {q['slug']}/{probe}")
                with _fusion_tap() as tap:
                    engine.search(memories, query, max_results=DEPTH_DEV)
                if tap["fused"] is None:
                    raise SystemExit(f"no base fusion call: {q['slug']}/{probe}")
                feats = _candidate_features(tap["rankings"], tap["fused"], uniq)
                gold_pos = next(
                    (i for i, (m, _, _) in enumerate(tap["fused"]) if m.id == gold_id),
                    None,
                )
                observed = (
                    gold_pos if gold_pos is not None and gold_pos < DEPTH_DEV else None
                )
                want = expected[(q["slug"], probe)]
                if observed != want:
                    raise SystemExit(
                        f"dev rank mismatch {q['slug']}/{probe}: "
                        f"recomputed {observed}, labels artifact {want}"
                    )
                ranks[(q["slug"], probe)] = gold_pos
                stratum = _stratum(gold_pos)
                strata[probe][stratum] = strata[probe].get(stratum, 0) + 1
                if gold_pos is not None and 1 <= gold_pos <= max(WINDOWS) - 1:
                    records[probe].append(
                        {
                            "slug": q["slug"],
                            **_near_miss_record(
                                feats[gold_pos],
                                feats[:gold_pos],
                                feats[0]["fused_score"],
                            ),
                        }
                    )
        return {
            "collection_size": size,
            "pad_to": pad_to,
            "strata": {p: dict(sorted(strata[p].items())) for p in strata},
            "near_misses": records,
            "_ranks": ranks,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def prefilter_reachability(
    rr: Any, ranks: dict[tuple[str, str], int | None]
) -> dict[str, Any]:
    """Padded-600, production's own loader: is gold in the served pool?

    Engagement is checked the way `run_arm_prefiltered` checks it — a
    pool without a corpus-statistics provider was NOT prefiltered, and
    counting it would mislabel reachability, so the run fails instead.
    """
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-p2aprefilter-"))
    try:
        slug_to_id, _size = rr.build_store(root, rr.CORPUS, pad_to=600)
        store = Store(root)
        by_stratum: dict[str, dict[str, int]] = {}
        for q in questions:
            gold_id = slug_to_id[q["slug"]]
            for probe in ("asked", "control"):
                query = rr._query_for(q, probe)
                pool = resolve_search_pool(
                    store,
                    query,
                    scopes=None,
                    excluded_scopes=None,
                    repo_filter=None,
                    worktree_filter=None,
                    min_survivors=max(rr.K_VALUES),
                )
                if pool.corpus_stats_provider is None:
                    raise SystemExit(f"prefilter unengaged: {q['slug']}/{probe}")
                stratum = _stratum(ranks[(q["slug"], probe)])
                cell = by_stratum.setdefault(stratum, {"probes": 0, "gold_in_pool": 0})
                cell["probes"] += 1
                if any(m.id == gold_id for m in pool.memories):
                    cell["gold_in_pool"] += 1
        return dict(sorted(by_stratum.items()))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# LongMemEval instrument
# ---------------------------------------------------------------------------


def lme_arm(lr: Any, progress: bool) -> dict[str, Any]:
    corpus = json.loads(lr.DEFAULT_CORPUS.read_text(encoding="utf-8"))
    sidecar = json.loads(LME_SIDECAR.read_text(encoding="utf-8"))
    expected = {row["qid"]: row["evidence_ranks"] for row in sidecar["arms"]["lexical"]}

    records: list[dict[str, Any]] = []
    recency_violations = 0
    n_questions = 0
    started = time.time()
    for i, inst in enumerate(corpus):
        evidence_sids = set(inst["answer_session_ids"])
        if not evidence_sids:
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-p2alme-"))
        try:
            id_to_session, _n = lr.build_question_store(root, inst)
            memories = Store(root).load_all()
            with _fusion_tap() as tap:
                hits = lr.run_search(
                    memories,
                    inst["question"],
                    max_results=lr.RETRIEVAL_DEPTH,
                    mode="hybrid",
                    rescue_expansion=lr.RESCUE_EXPANSION,
                )
            ranked = lr.distinct_sessions([h.id for h in hits], id_to_session)
            got = lr.question_record(inst, ranked)["evidence_ranks"]
            want = expected[inst["question_id"]]
            if got != want:
                raise SystemExit(
                    f"LME rank mismatch {inst['question_id']}: "
                    f"recomputed {got}, sidecar {want}"
                )
            if tap["fused"] is None:
                raise SystemExit(f"no base fusion call: {inst['question_id']}")

            uniq = _query_unique(inst["question"])
            feats = _candidate_features(tap["rankings"], tap["fused"], uniq)
            session_rows: list[tuple[str, dict[str, Any]]] = []
            seen: set[str] = set()
            for feat in feats:
                mem_id = tap["fused"][feat["fused_rank"]][0].id
                sid = id_to_session.get(mem_id)
                if sid is None or sid in seen:
                    continue
                seen.add(sid)
                session_rows.append((sid, feat))

            for rank, (sid, feat) in enumerate(session_rows):
                if sid not in evidence_sids:
                    continue
                if not 1 <= rank <= max(WINDOWS) - 1:
                    continue
                above = [f for s, f in session_rows[:rank] if s not in evidence_sids]
                if not above:
                    continue
                record = {
                    "qid": inst["question_id"],
                    "type": inst.get("question_type", "unknown"),
                    **_near_miss_record(feat, above, session_rows[0][1]["fused_score"]),
                }
                prefix = _DATE_PREFIX.match(feat["_body"])
                above_prefixes = [_DATE_PREFIX.match(f["_body"]) for f in above]
                if prefix is None or any(m is None for m in above_prefixes):
                    recency_violations += 1
                else:
                    record["gold_date"] = prefix.group(1)
                    record["distractor_dates"] = [
                        m.group(1) for m in above_prefixes if m is not None
                    ]
                records.append(record)
            n_questions += 1
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if progress and (i + 1) % 50 == 0:
            rate = (i + 1) / max(1e-9, time.time() - started)
            print(f"  [lme] {i + 1}/{len(corpus)} ({rate:.1f} q/s)", file=sys.stderr)

    return {
        "corpus": lr.DEFAULT_CORPUS.name,
        "corpus_sha256": lr.corpus_fingerprint(lr.DEFAULT_CORPUS),
        "questions_scored": n_questions,
        "recency_voided": recency_violations > 0,
        "recency_violations": recency_violations,
        "near_misses": records,
    }


# ---------------------------------------------------------------------------
# Tabulation
# ---------------------------------------------------------------------------


def _table(
    records: list[dict[str, Any]], window: int, features: tuple[str, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    in_window = [r for r in records if r["gold_rank"] <= window]
    for feature in features:
        wins = ties = losses = 0
        for r in in_window:
            for d in r["distractors_above"]:
                verdict = _compare(feature, r["gold"], d)
                wins += verdict == "win"
                ties += verdict == "tie"
                losses += verdict == "loss"
        effective = wins + losses
        out[feature] = {
            "wins": wins,
            "ties": ties,
            "losses": losses,
            "share": round(wins / effective, 4) if effective else None,
        }
    out["n_near_misses"] = len(in_window)
    return out


def _recency_table(records: list[dict[str, Any]], window: int) -> dict[str, Any]:
    wins = ties = losses = 0
    for r in records:
        if r["gold_rank"] > window or "gold_date" not in r:
            continue
        for date in r["distractor_dates"]:
            if r["gold_date"] > date:
                wins += 1
            elif r["gold_date"] == date:
                ties += 1
            else:
                losses += 1
    effective = wins + losses
    return {
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "share": round(wins / effective, 4) if effective else None,
    }


def _ceilings(records: list[dict[str, Any]], window: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    in_window = [r for r in records if r["gold_rank"] <= window]
    any_count = 0
    for r in in_window:
        r["_swept"] = [
            f
            for f in ELIGIBLE
            if all(_compare(f, r["gold"], d) == "win" for d in r["distractors_above"])
        ]
        any_count += bool(r["_swept"])
    for feature in ELIGIBLE:
        out[feature] = sum(1 for r in in_window if feature in r["_swept"])
    for r in in_window:
        del r["_swept"]
    out["any_eligible"] = any_count
    out["n_near_misses"] = len(in_window)
    return out


def _length_shape(records: list[dict[str, Any]]) -> dict[str, Any]:
    def dist(values: list[int]) -> dict[str, int] | None:
        if not values:
            return None
        ordered = sorted(values)
        return {
            "n": len(ordered),
            "p25": ordered[len(ordered) // 4],
            "median": ordered[len(ordered) // 2],
            "p75": ordered[(3 * len(ordered)) // 4],
        }

    return {
        "gold": dist([r["gold"]["length_tokens"] for r in records]),
        "distractors_above": dist(
            [d["length_tokens"] for r in records for d in r["distractors_above"]]
        ),
    }


def _deficit_shape(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None

    def med(values: list[float]) -> float:
        ordered = sorted(values)
        return round(ordered[len(ordered) // 2], 6)

    return {
        "n": len(records),
        "median_deficit_to_top": med([r["deficit_to_top"] for r in records]),
        "median_deficit_to_above": med([r["deficit_to_above"] for r in records]),
    }


def readiness(dev: dict[str, Any], lme: dict[str, Any]) -> dict[str, Any]:
    pooled: list[dict[str, Any]] = []
    for regime in ("unpadded", "padded600"):
        for probe in ("asked", "control"):
            pooled.extend(dev[regime]["near_misses"][probe])
    r1_table = _table(pooled, PRIMARY_WINDOW, ELIGIBLE)
    qualifiers = [
        f
        for f in ELIGIBLE
        if r1_table[f]["share"] is not None
        and r1_table[f]["share"] >= R1_SHARE
        and r1_table[f]["wins"] + r1_table[f]["losses"] >= R1_MIN_PAIRS
    ]
    lme_table = _table(lme["near_misses"], PRIMARY_WINDOW, ELIGIBLE)
    q_prime = [
        f
        for f in qualifiers
        if lme_table[f]["share"] is not None
        and lme_table[f]["share"] >= R2_SHARE
        and lme_table[f]["wins"] + lme_table[f]["losses"] >= R2_MIN_PAIRS
    ]
    recency = _recency_table(lme["near_misses"], PRIMARY_WINDOW)
    recency_admitted = (
        not lme["recency_voided"]
        and recency["share"] is not None
        and recency["share"] >= RECENCY_SHARE
        and recency["wins"] + recency["losses"] >= RECENCY_MIN_PAIRS
    )
    return {
        "r1_pool": "dev asked+control, both regimes, window 2..8",
        "r1_table": r1_table,
        "r1_qualifiers": qualifiers,
        "r2_table": lme_table,
        "q_prime": q_prime,
        "recency_lme": recency,
        "recency_admitted": recency_admitted,
        "addendum13_licensed": bool(q_prime),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="P2a feature census. Statistics only.")
    p.add_argument("--out", default=None, metavar="PATH")
    p.add_argument("--progress", action="store_true")
    args = p.parse_args()

    rr = load_bench_module("p2a_dev_run", _HERE / "retrieval" / "run.py")
    lr = load_bench_module("p2a_lme_run", _HERE / "longmemeval" / "run.py")
    expected = _dev_expected(DEV_LABELS)

    dev: dict[str, Any] = {}
    for regime, pad in (("unpadded", None), ("padded600", 600)):
        dev[regime] = dev_arm(rr, pad, expected[regime])
    reachability = prefilter_reachability(rr, dev["padded600"].pop("_ranks"))
    dev["unpadded"].pop("_ranks")
    lme = lme_arm(lr, args.progress)

    def corpus_tables(
        records_by_class: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for probe, records in records_by_class.items():
            out[probe] = {
                f"window_2_{w}": _table(records, w, ELIGIBLE) for w in WINDOWS
            }
            out[probe]["ceilings"] = {
                f"window_2_{w}": _ceilings(records, w) for w in WINDOWS
            }
            out[probe]["length_shape"] = _length_shape(records)
            out[probe]["deficit_shape"] = _deficit_shape(records)
        return out

    summary: dict[str, Any] = {"dev": {}, "lme": {}}
    for regime in ("unpadded", "padded600"):
        summary["dev"][regime] = corpus_tables(dev[regime]["near_misses"])
    summary["lme"] = {
        f"window_2_{w}": _table(lme["near_misses"], w, ELIGIBLE) for w in WINDOWS
    }
    summary["lme"]["recency"] = (
        {"voided": True, "violations": lme["recency_violations"]}
        if lme["recency_voided"]
        else {f"window_2_{w}": _recency_table(lme["near_misses"], w) for w in WINDOWS}
    )
    summary["lme"]["ceilings"] = {
        f"window_2_{w}": _ceilings(lme["near_misses"], w) for w in WINDOWS
    }
    summary["lme"]["length_shape"] = _length_shape(lme["near_misses"])
    summary["lme"]["deficit_shape"] = _deficit_shape(lme["near_misses"])

    payload = {
        "provenance": rr._provenance(),
        "declaration": "bench/P2A_CENSUS_DECLARATION.md",
        "windows": list(WINDOWS),
        "primary_window": PRIMARY_WINDOW,
        "note": (
            "STATISTICS ONLY — gold-vs-distractor feature pairs read off "
            "the shipped engine's own fusion inputs, tabulated under "
            "directions declared before any number existed. Dev ranks "
            "validated against base-leg-labels-2026-08-12.json; "
            "LongMemEval evidence ranks validated against "
            "round9-off-pq-2026-08-12.json. No fit, no ranking change."
        ),
        "dev": dev,
        "prefilter_reachability_padded600": reachability,
        "lme": lme,
        "summary": summary,
        "readiness": readiness(dev, lme),
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
