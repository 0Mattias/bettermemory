"""W3-P pre-declaration anatomy: the eight single-session-preference
misses at the incumbent default engine, reproduced and dissected.

Reproduces each miss ranking through the LongMemEval runner's own store
building and search invocation, asserts per-question parity with the
committed gate sidecar
(`bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json` —
whose preference rows are byte-identical to the L2 gate sidecar's, both
arms lane-on), and prints the miss table the W3-P declaration publishes:
for every miss, the ask, the gold evidence, the sessions that outrank
it, and the operationalized bridge need.

`NEEDS` below is the single source of the declared bridge-need token
sets. The W3-P Stage-0 register census imports it: census floor C is
defined over exactly these sets, fixed here before any corpus byte is
read. Editing NEEDS after the declaration commit voids the census.

Run: fastvenv/bin/python bench/w/w3p_anatomy.py
Artifact: bench/w/results/w3p-anatomy-2026-08-17.txt (stdout, dated).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SIDECAR = REPO / "bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json"
CORPUS = REPO / "bench/longmemeval/data/longmemeval_s_cleaned.json"

# The operationalized bridge needs — qid -> (ask-side tokens, gold-side
# tokens, register class, gloss). A token pair (a, b), a from the left
# set and b from the right, names vocabulary the ask and the gold do
# NOT share; the miss is attributable to that unbridged substitution.
# Tokens are lowercase, length >= 3, engine-tokenizer-compatible.
NEEDS: dict[str, tuple[frozenset[str], frozenset[str], str, str]] = {
    "75832dbd": (
        frozenset({"publication", "publications", "conference", "conferences"}),
        frozenset({"research", "dataset", "datasets", "learning", "medical"}),
        "academic/technical",
        "recent publications/conferences <-> the research-interest session"
        " (deep learning, medical image analysis)",
    ),
    "195a1a1b": (
        frozenset({"evening", "activities", "activity"}),
        frozenset({"schedule", "task", "tasks", "prioritize"}),
        "everyday-planning",
        "evening activities <-> the time-management/schedule session",
    ),
    "06f04340": (
        frozenset({"dinner", "serve", "homegrown"}),
        frozenset({"recipe", "recipes", "basil", "mint"}),
        "food",
        "what to serve for dinner <-> the herb-recipe session",
    ),
    "1a1907b4": (
        frozenset({"cocktail", "cocktails"}),
        frozenset({"drink", "drinks", "glass"}),
        "food/drink",
        "cocktail suggestions <-> the Pimm's Cup / Collins-glass session",
    ),
    "09d032c9": (
        frozenset({"battery"}),
        frozenset({"charging", "charger", "power"}),
        "consumer-tech",
        "phone battery life <-> the power-bank/charging-pad session",
    ),
    "95228167": (
        frozenset({"guitar"}),
        frozenset({"gibson", "fender", "stratocaster"}),
        "music-gear",
        "what to look for in a guitar <-> the Les Paul session",
    ),
    "505af2f5": (
        frozenset({"recipe"}),
        frozenset({"homemade", "making"}),
        "food",
        "creamer recipe <-> the making-my-own-creamer session",
    ),
    "d6233ab6": (
        frozenset({"nostalgic", "reunion"}),
        frozenset({"remember", "memories", "memory"}),
        "everyday-emotional",
        "nostalgic / reunion <-> the remembered-high-school session",
    ),
}


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "lme_run", REPO / "bench" / "longmemeval" / "run.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load bench/longmemeval/run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lme_run"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    run = _load_runner()
    from bettermemory.search import search as run_search
    from bettermemory.store import Store

    sidecar = json.loads(SIDECAR.read_text())
    expected: dict[str, int] = {}
    for row in sidecar["arms"]["lexical"]:
        if row["type"] != "single-session-preference":
            continue
        ranks = row["evidence_ranks"]
        if len(ranks) == 1 and ranks[0] is not None and ranks[0] >= 5:
            expected[row["qid"]] = ranks[0]
    if set(expected) != set(NEEDS):
        raise SystemExit(
            f"miss set drifted: sidecar {sorted(expected)} vs NEEDS {sorted(NEEDS)}"
        )

    corpus = json.loads(CORPUS.read_text())
    by_qid = {inst.get("question_id"): inst for inst in corpus}

    print("W3-P anatomy — the eight single-session-preference misses")
    print(f"sidecar: {SIDECAR.relative_to(REPO)}")
    print(f"corpus:  {CORPUS.relative_to(REPO)} (sha256 {sidecar['corpus_sha256']})")
    print(
        "engine: conversational lane on, rescue expansion off "
        "(the incumbent default arm)"
    )

    for qid, expected_rank in sorted(expected.items(), key=lambda kv: kv[1]):
        inst = by_qid[qid]
        root = Path(tempfile.mkdtemp(prefix="bm-w3p-anatomy-"))
        try:
            id_to_session, _ = run.build_question_store(root, inst)
            memories = Store(root).load_all()
            id_to_text = {m.id: m.body for m in memories}
            hits = run_search(
                memories,
                inst["question"],
                max_results=run.RETRIEVAL_DEPTH,
                mode="hybrid",
                rescue_expansion=False,
                conversational=True,
                now=run.question_now(inst),
            )
            ranked_items = [h.id for h in hits]
            ranked_sessions = run.distinct_sessions(ranked_items, id_to_session)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        gold = dict.fromkeys(inst["answer_session_ids"])
        (gold_sid,) = gold
        got = ranked_sessions.index(gold_sid) if gold_sid in ranked_sessions else None
        if got != expected_rank:
            raise SystemExit(
                f"{qid}: reproduced rank {got} != sidecar {expected_rank};"
                " the engine moved — re-earn the anatomy before using it"
            )

        left, right, register, gloss = NEEDS[qid]
        print("=" * 76)
        print(f"qid {qid}  gold session rank {got}  register: {register}")
        print(f"ask ({inst.get('question_date')}): {inst['question']}")
        gold_items = [
            (i, mid)
            for i, mid in enumerate(ranked_items)
            if id_to_session.get(mid) == gold_sid
        ]
        gi, gmid = gold_items[0]
        body = id_to_text[gmid][:400].replace("\n", "\n    ")
        print(f"gold (item rank {gi}):\n    {body}")
        print(f"bridge need: {gloss}")
        print(f"    ask-side  {sorted(left)}")
        print(f"    gold-side {sorted(right)}")
        outranker_snippets = []
        seen: set[str] = set()
        for mid in ranked_items:
            sid = id_to_session.get(mid)
            if sid is None or sid in seen or sid == gold_sid:
                continue
            seen.add(sid)
            outranker_snippets.append(id_to_text[mid][:120].replace("\n", " | "))
            if len(seen) >= 3:
                break
        print("top outrankers:")
        for snip in outranker_snippets:
            print(f"    - {snip}")
    print("=" * 76)
    print(f"misses: {len(expected)} of 30; class recall@5 22/30 = 0.7333")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
