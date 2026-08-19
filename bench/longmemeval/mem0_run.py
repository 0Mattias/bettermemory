"""LongMemEval — the mem0 arms.

Companion to `run.py` (bettermemory) and `cm_run.py` (claude-mem). Same
corpus, same labels, and the SAME attribution rule fixed in
PREREGISTRATION.md: one item per conversational round, rank items, map
each to its parent session, dedup preserving first occurrence, take the
first k DISTINCT sessions, score against `answer_session_ids`.

The arms are fixed by PREREGISTRATION.md addendum 13 and recon'd in
MEM0-ADAPTER.md. Two, both keyless and local, published side by side:

  mem0-base    `pip install mem0ai` — semantic-only; the BM25 keyword
               leg and spaCy entity boosts disable themselves at init,
               so the fused score reduces to raw cosine.
  mem0-extras  base + `mem0ai[extras]` + `mem0ai[nlp]` — their full
               hybrid, additively fused by their own scoring module,
               untuned.

The ARM IS THE ENVIRONMENT: which legs run is decided by what is
importable, not by a runtime flag, so this runner verifies the
environment matches the declared arm and refuses to run mislabeled.

THE DEPTH FORM, AND WHY THE HARNESS SETS IT. `Memory.search()` ships a
similarity threshold whose default drops candidates below it before
fusion. On stores this size the default form returns a single-digit
result list — a number that would measure list truncation, not
retrieval, claude-mem's recency window again in a different coat. The
harness therefore calls the public API at the shared retrieval depth
with the score cut at its validated floor (`threshold=0.0`; `None` is
coerced back to the shipped default upstream). The residual boundary —
candidates with negative semantic score are unreachable — is carried by
`n_ranked` per question, exactly as depth truncation is for our own
arms.

INGEST bypasses their extraction (`add(..., infer=False)`): mem0's real
product extracts facts before storage, so the arms are symmetric in
input and asymmetric in how much of mem0's design is exercised. This
may UNDERSTATE mem0 and is published beside any number. The LongMemEval
session id travels only in payload metadata, never in embedded content.

GATES, before any question is scored (addendum 13):

  G-ready  exact per-user store count == rounds offered, all users.
  G-iso    sampled searches under user filters return only session ids
           from the filtered user's own haystack.

Usage:

    .eval-venv/bin/python bench/longmemeval/mem0_run.py --arm base --limit 3
    .eval-venv/bin/python bench/longmemeval/mem0_run.py --arm base --json \
        --per-question results/per-question/mem0-base-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
DEFAULT_CORPUS = _HERE / "data" / "longmemeval_s_cleaned.json"

K_VALUES = (1, 5, 10)
RETRIEVAL_DEPTH = 200
SEARCH_THRESHOLD = 0.0  # the validated floor; None coerces to the default
COLLECTION = "lme_mem0"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMS = 384


def rounds_of(session: list[dict[str, Any]]) -> list[str]:
    """Identical pairing to run.py — every arm must see one corpus."""
    out: list[str] = []
    i = 0
    turns = list(session)
    while i < len(turns):
        parts = [f"{turns[i].get('role', '?')}: {turns[i].get('content', '')}"]
        if i + 1 < len(turns) and turns[i + 1].get("role") != turns[i].get("role"):
            parts.append(
                f"{turns[i + 1].get('role', '?')}: {turns[i + 1].get('content', '')}"
            )
            i += 2
        else:
            i += 1
        out.append("\n".join(parts))
    return out


def _leg_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for pkg in (
        "mem0ai",
        "qdrant-client",
        "sentence-transformers",
        "fastembed",
        "spacy",
    ):
        try:
            out[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            out[pkg] = None
    return out


def verify_arm(arm: str) -> dict[str, str | None]:
    """The arm is the environment; refuse to run mislabeled."""
    versions = _leg_versions()
    has_fastembed = importlib.util.find_spec("fastembed") is not None
    has_spacy = importlib.util.find_spec("spacy") is not None
    if arm == "base" and (has_fastembed or has_spacy):
        raise SystemExit(
            "arm=base but fastembed/spacy are importable — this environment "
            "would run the hybrid legs and publish them under the wrong label. "
            "Use the base venv."
        )
    if arm == "extras" and not (has_fastembed and has_spacy):
        raise SystemExit(
            "arm=extras but fastembed/spacy are not both importable — the "
            "hybrid legs would silently disable and the run would be the base "
            "arm wearing the extras label. Install mem0ai[extras] and "
            "mem0ai[nlp] in this venv."
        )
    return versions


def build_memory(store_dir: Path):
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    from mem0 import Memory

    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(store_dir / "qdrant"),
                "collection_name": COLLECTION,
                "embedding_model_dims": EMBED_DIMS,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {"model": EMBED_MODEL, "embedding_dims": EMBED_DIMS},
        },
        # Never called: every add() passes infer=False and search() makes
        # no chat calls. The dummy key satisfies eager client construction.
        "llm": {
            "provider": "openai",
            "config": {"api_key": "sk-unused-infer-false", "model": "gpt-5-mini"},
        },
    }
    return Memory.from_config(config)


def ingest(memory: Any, inst: dict[str, Any]) -> tuple[int, set[str]]:
    """One memory per round; session id in payload metadata only."""
    qid = inst["question_id"]
    dates = inst.get("haystack_dates") or []
    n = 0
    sids: set[str] = set()
    for idx, (sid, session) in enumerate(
        zip(inst["haystack_session_ids"], inst["haystack_sessions"])
    ):
        date = dates[idx] if idx < len(dates) else ""
        sids.add(sid)
        for body in rounds_of(session):
            text = f"[{date}]\n{body}" if date else body
            memory.add(text, user_id=qid, infer=False, metadata={"session_id": sid})
            n += 1
    return n, sids


def store_count(memory: Any, qid: str) -> int:
    """Exact per-user count read from the store — G-ready's ground truth."""
    from qdrant_client import models

    client = memory.vector_store.client
    return client.count(
        collection_name=COLLECTION,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(key="user_id", match=models.MatchValue(value=qid))
            ]
        ),
        exact=True,
    ).count


def search_sessions(memory: Any, qid: str, question: str) -> list[str]:
    """The real query path, at the declared depth form. Ranked distinct sids."""
    res = memory.search(
        question,
        filters={"user_id": qid},
        top_k=RETRIEVAL_DEPTH,
        threshold=SEARCH_THRESHOLD,
    )
    hits = res.get("results", res) if isinstance(res, dict) else res
    out: list[str] = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        sid = (h.get("metadata") or {}).get("session_id")
        if sid and sid not in out:
            out.append(sid)
    return out


@dataclass
class ArmResult:
    arm: str
    n: int = 0
    macro: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    hit: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    ceiling: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    total_evidence: int = 0
    empty: int = 0
    by_type: dict[str, dict[int, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    type_n: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seconds: float = 0.0

    def recall_macro(self, k: int) -> float:
        return self.macro[k] / self.n if self.n else 0.0

    def recall_micro(self, k: int) -> float:
        return self.hit[k] / self.total_evidence if self.total_evidence else 0.0

    def ceiling_at(self, k: int) -> float:
        return self.ceiling[k] / self.n if self.n else 0.0


def question_record(inst: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    """Same sidecar fields as run.py, so aggregates rebuild from rows."""
    rank_of = {sid: i for i, sid in enumerate(ranked)}
    evidence = list(dict.fromkeys(inst["answer_session_ids"]))
    return {
        "qid": inst.get("question_id", ""),
        "type": inst.get("question_type", "unknown"),
        "n_evidence": len(evidence),
        "evidence_ranks": [rank_of.get(sid) for sid in evidence],
        "n_ranked": len(ranked),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="mem0 arms on LongMemEval.")
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--arm", choices=("base", "extras"), required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--iso-sample", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.add_argument("--per-question", default=None)
    p.add_argument("--keep", action="store_true", help="Keep the store dir.")
    args = p.parse_args()

    os.environ.setdefault("MEM0_TELEMETRY", "False")
    versions = verify_arm(args.arm)
    arm_name = f"mem0-{args.arm}"

    corpus_path = Path(args.corpus)
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    raw = corpus_path.read_bytes()
    corpus_sha = hashlib.sha256(raw).hexdigest()
    corpus = json.loads(raw.decode("utf-8"))
    total = len(corpus)
    if args.limit:
        corpus = corpus[: args.limit]

    notes: list[str] = [
        "ingest bypasses mem0's LLM extraction (add(..., infer=False)); their "
        "real pipeline extracts facts before storage, so this may UNDERSTATE "
        "mem0. See PREREGISTRATION.md addendum 13.",
        "search runs the public API at the shared retrieval depth with the "
        "score cut at its validated floor; the shipped default truncates the "
        "result list on stores this size, and publishing that would measure "
        "the default, not retrieval. Candidates with negative semantic score "
        "remain unreachable; n_ranked carries the boundary per question.",
    ]
    if args.limit and args.limit < total:
        notes.append(
            f"SUBSET — first {len(corpus)} of {total}, not a stratified "
            "sample. Question-type mix is skewed; not publishable."
        )

    store_dir = Path(tempfile.mkdtemp(prefix=f"mem0-lme-{args.arm}-"))
    print(f"[{arm_name}] store at {store_dir}", file=sys.stderr)
    memory = build_memory(store_dir)

    offered: dict[str, int] = {}
    sid_sets: dict[str, set[str]] = {}
    rows: list[ArmResult] = []
    per_question: list[dict[str, Any]] = []
    gates: dict[str, Any] = {}
    try:
        t0 = time.time()
        for i, inst in enumerate(corpus):
            qid = inst["question_id"]
            n, sids = ingest(memory, inst)
            offered[qid] = n
            sid_sets[qid] = sids
            if (i + 1) % 10 == 0:
                rate = sum(offered.values()) / max(time.time() - t0, 1e-9)
                print(
                    f"  ingested {i + 1}/{len(corpus)} ({rate:.0f} rounds/s)",
                    file=sys.stderr,
                )
        ingest_seconds = time.time() - t0
        rounds_offered = sum(offered.values())

        # G-ready — exact per-user counts from the store. One mismatch stops
        # the run: scoring a partially written store is the half-built-index
        # failure, and it is cheaper to refuse than to retract.
        mismatches = []
        stored_total = 0
        for qid, n in offered.items():
            got = store_count(memory, qid)
            stored_total += got
            if got != n:
                mismatches.append((qid, n, got))
        gates["ready"] = {
            "users": len(offered),
            "rounds_offered": rounds_offered,
            "items_written": stored_total,
            "mismatches": len(mismatches),
        }
        if mismatches:
            for qid, want, got in mismatches[:10]:
                print(
                    f"G-ready MISMATCH {qid}: offered {want}, stored {got}",
                    file=sys.stderr,
                )
            raise SystemExit("G-ready failed — the store does not hold the corpus.")
        print(
            f"G-ready PASS: {len(offered)} users, {rounds_offered:,} rounds exact",
            file=sys.stderr,
        )

        # G-iso — the leak probe through the real query path.
        qids = [inst["question_id"] for inst in corpus]
        step = max(1, len(qids) // max(1, args.iso_sample))
        sampled = qids[::step]
        leaks = 0
        for qid in sampled:
            inst = next(x for x in corpus if x["question_id"] == qid)
            for sid in search_sessions(memory, qid, inst["question"]):
                if sid not in sid_sets[qid]:
                    leaks += 1
        gates["iso"] = {"sampled_users": len(sampled), "leaks": leaks}
        if leaks:
            raise SystemExit("G-iso failed — cross-user leak; the run is void.")
        print(f"G-iso PASS: {len(sampled)} users sampled, 0 leaks", file=sys.stderr)

        res = ArmResult(arm=arm_name)
        started = time.time()
        for i, inst in enumerate(corpus):
            evidence = set(inst["answer_session_ids"])
            if not evidence:
                continue
            qid = inst["question_id"]
            try:
                ranked = search_sessions(memory, qid, inst["question"])
            except Exception as exc:  # noqa: BLE001
                print(f"  [{arm_name}] {qid} FAILED: {exc}", file=sys.stderr)
                ranked = []
            if not ranked:
                res.empty += 1
            res.n += 1
            res.total_evidence += len(evidence)
            qtype = inst.get("question_type", "unknown")
            res.type_n[qtype] += 1
            for k in K_VALUES:
                got = set(ranked[:k]) & evidence
                recall = len(got) / len(evidence)
                res.macro[k] += recall
                res.hit[k] += len(got)
                res.ceiling[k] += min(k, len(evidence)) / len(evidence)
                res.by_type[qtype][k] += recall
            per_question.append(question_record(inst, ranked))
            if (i + 1) % 25 == 0:
                print(
                    f"  [{arm_name}] {i + 1}/{len(corpus)} "
                    f"macro@5={res.recall_macro(5):.3f}",
                    file=sys.stderr,
                )
        res.seconds = time.time() - started
        rows.append(res)
    finally:
        if not args.keep:
            shutil.rmtree(store_dir, ignore_errors=True)

    meta = {
        "provenance": {
            "arm": arm_name,
            "versions": versions,
            "embedder": EMBED_MODEL,
            "collection": COLLECTION,
            "retrieval_depth": RETRIEVAL_DEPTH,
            "search_threshold": SEARCH_THRESHOLD,
            "corpus_sha256": corpus_sha,
        },
        "corpus": corpus_path.name,
        "scored": rows[0].n if rows else 0,
        "instances": total,
        "ingest": {
            "rounds_offered": sum(offered.values()),
            "items_written": gates.get("ready", {}).get("items_written", 0),
            "shortfall": round(
                1
                - gates.get("ready", {}).get("items_written", 0)
                / max(1, sum(offered.values())),
                5,
            ),
            "seconds": round(ingest_seconds, 1),
        },
        "gates": gates,
        "notes": notes,
    }

    if args.per_question:
        pq_path = Path(args.per_question)
        if not pq_path.is_absolute():
            pq_path = (_HERE / pq_path).resolve()
        pq_path.parent.mkdir(parents=True, exist_ok=True)
        pq_path.write_text(
            json.dumps(
                {"provenance": meta["provenance"], "rows": per_question}, indent=1
            )
        )
        print(f"per-question sidecar: {pq_path}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    **meta,
                    "results": [
                        {
                            "arm": r.arm,
                            "n": r.n,
                            "seconds": round(r.seconds, 1),
                            "empty_result_questions": r.empty,
                            "macro": {
                                str(k): round(r.recall_macro(k), 4) for k in K_VALUES
                            },
                            "micro": {
                                str(k): round(r.recall_micro(k), 4) for k in K_VALUES
                            },
                            "ceiling": {
                                str(k): round(r.ceiling_at(k), 4) for k in K_VALUES
                            },
                            "by_type": {
                                t: {
                                    str(k): round(r.by_type[t][k] / r.type_n[t], 4)
                                    for k in K_VALUES
                                }
                                for t in sorted(r.type_n)
                            },
                            "type_n": dict(r.type_n),
                        }
                        for r in rows
                    ],
                },
                indent=2,
            )
        )
    else:
        print()
        print(f"corpus: {meta['corpus']}  scored: {meta['scored']}/{total}")
        for r in rows:
            cells = "".join(
                f"| {100 * r.recall_macro(k):>5.1f}% [{100 * r.ceiling_at(k):>4.0f}%] "
                for k in K_VALUES
            )
            print(f"| {r.arm:<11} {cells}| {r.n:>3} | {r.empty:>5} |")
        for note in notes:
            print(f"\nnote: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
