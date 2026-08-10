"""Does read-side coverage diversification have a signal to work with?

`README.md` explained the partial-recall failure mode as a coverage
problem: a two-event question carries vocabulary for both events, no
single session covers all of it, and `score_memory`'s
`0.5 + 0.5 * coverage` multiplier therefore cannot be satisfied by
either one alone. That story predicts something checkable — the evidence
session that gets DROPPED out of the top k should carry query terms the
surviving head does not.

This probe checks it. For every question where some evidence lands in the
top k and some does not, it takes each dropped session's best-ranked hit
and asks how many matched query terms it brings that the top-k hits do
not already carry between them. If that number is usually zero, the
dropped evidence is not distinguishable by coverage, and no amount of
tuning a coverage-aware re-ranker will find it.

Two things keep that from being a rigged question. First, the count is
reported against TWO reference sets, generous and strict — the headline
moves by nine points between them, so publishing only the flattering one
would be a choice rather than a measurement (`question_coverage`).
Second, `summarise` bounds an OMNISCIENT rescue that promotes exactly the
novel-carrying evidence with no false promotions, and reports the
precision a real re-ranker would face instead. A ceiling is what
separates "no design was found" from "no design exists".

Runs the lexical arm only: that is the arm the +3.2 rescue estimate was
made on, and the arm whose scorer the coverage story names. Costs about
one lexical arm (~5 minutes), and emits a JSON artifact so the finding is
a property of a committed file rather than of a re-run — the same rule
`question_record` in `run.py` exists to enforce.

    .venv/bin/python bench/longmemeval/coverage_probe.py \\
        --out results/coverage-probe-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

sys.path.insert(0, str(_HERE))
import run as lme  # noqa: E402

# Comfortably above the ~245 items a per-question store holds, so the
# ranking examined is the whole fused list rather than a window of it.
UNBOUNDED = 2000

K = 5


def question_coverage(
    ranked_hits: list[tuple[str, list[str]]],
    evidence: list[str],
    *,
    k: int = K,
) -> dict[str, Any]:
    """Everything this probe needs from one question.

    `ranked_hits` is `[(session_id, matched_terms), ...]` in rank order.
    Always returns a record; `partial` says whether the question is in the
    population a rescue could help — at least one evidence session inside
    the top k and at least one outside. A total miss has nothing to rescue
    toward and a complete hit needs nothing, so both are excluded from the
    novelty statistics while still counting toward `recall_at_k` (the
    oracle below is pooled over ALL questions, which is the only pooling
    comparable to a published macro figure).

    **Two reference sets, because the headline is definition-dependent
    and quoting only the favourable one would be exactly the kind of
    reporting this directory exists to refuse.** `novel_broad` counts
    matched terms the dropped hit carries that NO hit belonging to any
    top-k session carries — the most generous reference available, and
    the one that makes the strongest-sounding claim. `novel_strict`
    counts against one representative hit per top-k session (each
    session's best-ranked). Strict is the fairer analogue of what a
    re-ranker sitting between the fuse and the trim actually holds: a
    list head, not a session-aware union, since `search()` has no notion
    of the bench's sessions at all.

    A dropped session that never scored in any leg appears in no ranking
    and gets no record. Those are counted (`unrankable`) rather than
    dropped silently — they carry zero matched terms by construction, so
    omitting them quietly inflates the denominator's favourability.
    """
    ranked: list[str] = []
    best_hit: dict[str, list[str]] = {}
    for sid, matched in ranked_hits:
        if not sid:
            continue
        if sid not in best_hit:
            ranked.append(sid)
            best_hit[sid] = matched

    ev = set(evidence)
    surfaced = set(ranked[:k]) & ev
    rec: dict[str, Any] = {
        "recall_at_k": len(surfaced) / len(ev) if ev else 0.0,
        "n_evidence": len(ev),
        "partial": bool(surfaced) and surfaced != ev,
        "dropped": [],
        "unrankable": 0,
        # Non-evidence sessions below the top k that a novelty-keyed
        # rescue would also promote: the false positives that decide
        # whether the signal separates anything.
        "distractors_below_k": 0,
        "distractors_below_k_novel": 0,
        "distractor_novel_by_reference": {"broad": 0, "strict": 0, "top1": 0},
        "survivor_matched_terms": [],
    }
    if not rec["partial"]:
        return rec

    dropped = ev - surfaced
    top_k = set(ranked[:k])
    broad_terms: set[str] = set()
    for sid, matched in ranked_hits:
        if sid in top_k:
            broad_terms.update(matched)
    strict_terms: set[str] = set()
    for sid in ranked[:k]:
        strict_terms.update(best_hit[sid])
    # A third, deliberately loose reference: only the single top session
    # counts as "already seen". It is here because loosening the
    # reference is the obvious way to make novelty look like a usable
    # signal, and the table in `summarise` needs to show what that
    # actually buys.
    top1_terms = set(best_hit[ranked[0]]) if ranked else set()

    for sid in ranked[k:]:
        matched = set(best_hit[sid])
        novel = {
            "broad": len(matched - broad_terms),
            "strict": len(matched - strict_terms),
            "top1": len(matched - top1_terms),
        }
        if sid in ev:
            rec["dropped"].append(
                {
                    "novel_broad": novel["broad"],
                    "novel_strict": novel["strict"],
                    "novel_top1": novel["top1"],
                    "matched_terms": len(matched),
                    "item_rank": next(
                        i for i, (s, _m) in enumerate(ranked_hits) if s == sid
                    ),
                    "distinct_rank": ranked.index(sid),
                }
            )
        else:
            rec["distractors_below_k"] += 1
            if novel["strict"]:
                rec["distractors_below_k_novel"] += 1
            for ref, n in novel.items():
                if n:
                    rec["distractor_novel_by_reference"][ref] += 1

    # Like-for-like matched-term counts: one best hit per session on both
    # sides. Comparing a per-session median against a per-HIT mean over
    # the head is not a comparison, and it reverses the sign.
    rec["survivor_matched_terms"] = [len(best_hit[s]) for s in ranked[:k]]
    rec["unrankable"] = len(dropped) - len(rec["dropped"])
    return rec


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse per-question records into the reportable claim.

    `oracle_*` bounds an omniscient rescue: one that promotes exactly the
    dropped evidence sessions carrying at least one novel term, into the
    top k, with zero false promotions. No real re-ranker can do better,
    so if the oracle misses the gate the whole family is dead rather than
    merely untuned — which is the difference between "we did not find a
    design" and "there is not one to find".
    """
    partial = [r for r in records if r["partial"]]
    dropped = [d for r in partial for d in r["dropped"]]
    base = sum(r["recall_at_k"] for r in records) / len(records) if records else 0.0
    out: dict[str, Any] = {
        "questions": len(records),
        "partial_questions": len(partial),
        "macro_recall_at_k": round(base, 4),
        "dropped_sessions": len(dropped) + sum(r["unrankable"] for r in partial),
        "dropped_sessions_ranked": len(dropped),
        "dropped_sessions_unrankable": sum(r["unrankable"] for r in partial),
    }
    if not dropped:
        return out

    for label, key in (("broad", "novel_broad"), ("strict", "novel_strict")):
        novel = [d[key] for d in dropped]
        # Unrankable sessions carry no terms at all, so they belong in the
        # zero bucket rather than outside the denominator.
        zero = len([n for n in novel if n == 0]) + out["dropped_sessions_unrankable"]
        hist = Counter(novel)
        hist[0] += out["dropped_sessions_unrankable"]
        out[f"reference_{label}"] = {
            "zero_novel": zero,
            "zero_novel_fraction": round(zero / out["dropped_sessions"], 4),
            "novel_term_histogram": {str(n): c for n, c in sorted(hist.items())},
        }

    item_ranks = sorted(d["item_rank"] for d in dropped)
    distinct_ranks = sorted(d["distinct_rank"] for d in dropped)
    out["dropped_matched_terms_median"] = statistics.median(
        d["matched_terms"] for d in dropped
    )
    out["dropped_item_rank_median"] = statistics.median(item_ranks)
    out["dropped_item_rank_p90"] = item_ranks[int(0.9 * (len(item_ranks) - 1))]
    out["dropped_distinct_rank_median"] = statistics.median(distinct_ranks)

    out["survivor_matched_terms_median"] = statistics.median(
        n for r in partial for n in r["survivor_matched_terms"]
    )

    # Oracle ceilings, one per novelty reference, PLUS the no-filter bound.
    #
    # This table is the definition-independent form of the finding, and it
    # exists because the single-reference version invites an obvious
    # rebuttal: loosen the novelty test and the oracle goes up. It does —
    # but only by converging on `blind`, which promotes every dropped
    # session and needs no signal at all. What decides whether novelty is
    # a signal is not the ceiling, it is the PRECISION lift over blind
    # promotion. A filter that raises the ceiling while lowering precision
    # has found nothing; it has just stopped filtering.
    def ceiling(passes: Any) -> dict[str, Any]:
        gained = 0.0
        tp = 0
        for r in partial:
            n = len([d for d in r["dropped"] if passes(d)])
            tp += n
            surfaced = r["recall_at_k"] * r["n_evidence"]
            gained += min(surfaced + n, K) / r["n_evidence"] - r["recall_at_k"]
        return {"promoted": tp, "delta_points": round(100 * gained / len(records), 2)}

    rows: dict[str, dict[str, Any]] = {}
    blind_precision = 0.0
    for ref in ("blind", "broad", "strict", "top1"):
        if ref == "blind":
            row = ceiling(lambda _d: True)
            fp = sum(r["distractors_below_k"] for r in partial)
        else:
            row = ceiling(lambda d, ref=ref: d[f"novel_{ref}"] > 0)
            fp = sum(r["distractor_novel_by_reference"][ref] for r in partial)
        tp = row["promoted"]
        row["distractors_promoted"] = fp
        row["precision"] = round(tp / (tp + fp), 4) if tp + fp else 0.0
        if ref == "blind":
            blind_precision = row["precision"]
        row["precision_lift_over_blind"] = (
            round(row["precision"] / blind_precision, 3) if blind_precision else 0.0
        )
        row["macro_recall_at_k"] = round(base + row["delta_points"] / 100, 4)
        rows[ref] = row
    out["oracle_by_reference"] = rows
    out["oracle"] = rows["strict"]
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", default=str(lme.DEFAULT_CORPUS))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None, metavar="PATH")
    args = p.parse_args()

    corpus_path = Path(args.corpus).expanduser()
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    if not corpus_path.exists():
        print(f"missing corpus: {corpus_path}", file=sys.stderr)
        return 1

    sha = lme.corpus_fingerprint(corpus_path)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    instances = len(corpus)
    notes: list[str] = []
    if sha not in lme.KNOWN_CORPORA:
        notes.append(
            f"UNPINNED CORPUS — sha256 {sha[:16]}… is not a revision this "
            "harness has seen. Not comparable to published rows."
        )
    if args.limit:
        corpus = corpus[: args.limit]
        notes.append(
            f"SUBSET — first {len(corpus)} of {instances} instances. Not publishable."
        )

    records: list[dict[str, Any]] = []
    scored = 0
    started = time.time()
    for i, inst in enumerate(corpus):
        if not inst["answer_session_ids"]:
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-probe-"))
        try:
            id_to_session, _ = lme.build_question_store(root, inst)
            hits = run_search(
                Store(root).load_all(),
                inst["question"],
                max_results=UNBOUNDED,
                mode="hybrid",
            )
            ranked_hits = [(id_to_session.get(h.id, ""), h.match_terms) for h in hits]
        finally:
            shutil.rmtree(root, ignore_errors=True)
        scored += 1
        records.append(question_coverage(ranked_hits, inst["answer_session_ids"]))
        if (i + 1) % 50 == 0:
            print(
                f"  {i + 1}/{len(corpus)} "
                f"({(i + 1) / max(1e-9, time.time() - started):.1f} q/s)",
                file=sys.stderr,
            )

    payload = {
        "corpus": corpus_path.name,
        "corpus_sha256": sha,
        "instances": instances,
        "scored": scored,
        "arm": "lexical",
        "k": K,
        "seconds": round(time.time() - started, 1),
        "notes": notes,
        "summary": summarise(records),
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out_path = Path(args.out).expanduser()
        if not out_path.is_absolute():
            out_path = (_HERE / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
