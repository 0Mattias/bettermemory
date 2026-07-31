"""LongMemEval — session-level retrieval recall against third-party labels.

WHY THIS EXISTS. `bench/retrieval/` closes with "Any competitor. This is a
self-measurement. Nothing here licenses a comparative claim." This runner
is the thing that licenses one. It scores retrieval against evidence
labels that neither this project nor claude-mem authored, on a corpus
neither of us built.

THE INSTRUMENT, AND WHY IT SURVIVED THE QUESTION THAT KILLED THE LAST ONE.
LongMemEval (Wu et al., ICLR 2025, arXiv:2410.10813v2, MIT). Its
predecessor here, LongMemCode, was retired before an adapter was written:
84.5% of its labels derive from the structural bundle format of the
vendor whose own memory system tops its scoreboard. LongMemEval was asked
the same question first and passed — evidence-session labels are
construction-time ground truth established by INSERTION (human experts
author the questions, hand-decompose each answer into evidence
statements, an evidence session is authored AROUND each statement, and
those sessions are then shuffled into a haystack of unrelated ones). No
retriever or embedding model appears anywhere in construction, distractor
selection included. Full reasoning: PREREGISTRATION.md.

THE ATTRIBUTION RULE IS THE WHOLE COMPARISON. LongMemEval scores against
SESSION ids. Neither system under test stores sessions — bettermemory
stores memories, claude-mem stores observations. So the mapping from a
returned item back to a session decides the result, and it is fixed in
PREREGISTRATION.md rather than chosen after seeing numbers:

  ingest unit   one item per conversational round (user msg + reply),
                held identical across both systems
  scoring       rank items, map each to its parent session, dedup
                preserving first occurrence, take the first k DISTINCT
                sessions, score against answer_session_ids

  recall@k  =  |top-k distinct sessions ∩ evidence| / |evidence|
               macro-averaged over questions (micro also reported)

THE CEILING IS PUBLISHED, NOT HIDDEN. 324 of 500 questions carry two or
more evidence sessions (1→176, 2→250, 3→41, 4→19, 5→11, 6→3), so for any
question where |evidence| > k, recall@k is bounded below 1 BY
CONSTRUCTION. At k=1 that ceiling binds on 324 of 500. Every figure is
printed next to the maximum achievable value at that k, because a recall
number quoted without its ceiling reads as a failure of the retriever
when it is arithmetic.

WHAT THIS DELIBERATELY DOES NOT MEASURE — the guardrail bypass. Ingest
goes through `Store.write`, the raw storage layer, NOT through
`memory_write`. The handler enforces dedup, transient-marker rejection,
credential screening and a pending-confirm flow for user-inference;
pushing 500 synthetic haystacks through it would measure the WRITE
POLICY, not retrieval. The cost is real and is stated rather than
buried: this benchmark measures store + retrieval, and the capture
guardrails that are part of the product's value are switched off for it.

TIMESTAMPS ARE INCLUDED, IDENTICALLY FOR BOTH SYSTEMS. Each round's body
carries its session date as a leading line. 133 of 500 questions are
temporal-reasoning; withholding the dates would make that class
unanswerable for everyone and measure nothing. The session ID itself is
NEVER written into any body or scope — it lives only in a side map, so it
cannot be retrieved as content.

Usage:

    venv/bin/python bench/longmemeval/run.py --limit 20        # smoke
    venv/bin/python bench/longmemeval/run.py                   # full
    venv/bin/python bench/longmemeval/run.py --json
    venv/bin/python bench/longmemeval/run.py --corpus data/longmemeval_oracle.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add `src/` to sys.path so this script is runnable without an install.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data"
DEFAULT_CORPUS = DATA / "longmemeval_s_cleaned.json"

# Pinned so a result file names the exact bytes it scored. The DISTRIBUTED
# corpus is `-cleaned`, a revision published after the paper — any
# comparison to a printed baseline must name that gap rather than assume
# the two are the same corpus. See PREREGISTRATION.md addendum.
KNOWN_CORPORA = {
    "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442": (
        "longmemeval_s_cleaned.json"
    ),
    "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c": (
        "longmemeval_oracle.json"
    ),
}

# The oracle variant holds evidence sessions and NO distractors, so any
# retriever that is not actively broken scores ~1.0. It validates adapter
# plumbing and is BARRED from producing a published figure.
ORACLE_SHA = "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c"

K_VALUES = (1, 5, 10)

# Item-level retrieval depth, collapsed afterwards to distinct sessions.
# Must exceed max(K_VALUES) by enough that k distinct sessions are
# reachable even when a single session monopolises the head of the
# ranking. Median haystack is 245 rounds across 48 sessions, so ~5 rounds
# per session; 200 gives a distinct-session yield far above k=10 while
# staying well under the smallest haystack (198 rounds). Questions that
# still fail to yield k distinct sessions are reported as
# depth-truncated, never silently scored as misses.
RETRIEVAL_DEPTH = 200

INDEX_THRESHOLD = 500  # mirrors `_handlers._INDEX_THRESHOLD_DEFAULT`

SCOPE = ["longmemeval"]


def corpus_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def rounds_of(session: list[dict[str, Any]]) -> list[str]:
    """Pair turns into rounds: one user message plus its assistant reply.

    A trailing unpaired turn is emitted alone rather than dropped — the
    corpus contains sessions that end on a user message, and silently
    discarding the tail would remove evidence the labels still point at.
    """
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


def build_question_store(
    root: Path, inst: dict[str, Any]
) -> tuple[dict[str, str], int]:
    """Write one instance's haystack. Returns (memory id -> session id, items).

    The session id is NEVER placed in the body or the scopes. It would be
    retrievable content if it were, and the labels would leak into the
    thing being measured.
    """
    store = Store(root)
    id_to_session: dict[str, str] = {}
    dates = inst.get("haystack_dates") or []
    n = 0
    for idx, (sid, session) in enumerate(
        zip(inst["haystack_session_ids"], inst["haystack_sessions"])
    ):
        date = dates[idx] if idx < len(dates) else ""
        for body in rounds_of(session):
            text = f"[{date}]\n{body}" if date else body
            memory = store.write(content=text, scopes=SCOPE)
            id_to_session[memory.id] = sid
            n += 1
    return id_to_session, n


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def distinct_sessions(
    ranked_ids: list[str], id_to_session: dict[str, str]
) -> list[str]:
    """Collapse an item ranking to distinct sessions, first occurrence wins."""
    seen: list[str] = []
    known = set()
    for mid in ranked_ids:
        sid = id_to_session.get(mid)
        if sid is not None and sid not in known:
            known.add(sid)
            seen.append(sid)
    return seen


def question_record(inst: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    """One question's retrieval outcome, in the smallest form every
    published aggregate can be rebuilt from.

    `evidence_ranks` holds the 0-based rank of each evidence session in
    the DISTINCT-SESSION ranking, positionally aligned with the deduped
    `answer_session_ids`; `null` means that session never surfaced within
    `RETRIEVAL_DEPTH`. Given these plus `n_ranked`:

        recall@k  = len([r for r in evidence_ranks if r is not None and r < k])
                    / n_evidence
        complete@k = recall@k == 1.0     partial@k = 0 < recall@k < 1
        depth-truncated@k = n_ranked < k

    which is the whole of the by-type table AND the partial/complete
    split that motivated read-side rescue. That split was measured once
    by a throwaway re-run because this runner persisted `by_type`
    aggregates only; anything derived from a run that is not in its own
    result file has to be re-earned every time somebody asks.
    """
    rank_of = {sid: i for i, sid in enumerate(ranked)}
    evidence = list(dict.fromkeys(inst["answer_session_ids"]))
    return {
        "qid": inst.get("question_id", ""),
        "type": inst.get("question_type", "unknown"),
        "n_evidence": len(evidence),
        "evidence_ranks": [rank_of.get(sid) for sid in evidence],
        "n_ranked": len(ranked),
    }


@dataclass
class ArmResult:
    arm: str
    n: int = 0
    # macro: per-question recall summed, divided by n at report time
    macro: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    # micro: intersection counts and evidence counts summed
    hit: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    total_evidence: int = 0
    ceiling: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    truncated: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    by_type: dict[str, dict[int, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    type_n: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    items_written: int = 0
    rounds_offered: int = 0
    dup_session_questions: int = 0
    seconds: float = 0.0
    # Always collected (500 small dicts); written out only under
    # --per-question, so the published summary shape stays byte-stable.
    per_question: list[dict[str, Any]] = field(default_factory=list)

    def recall_macro(self, k: int) -> float:
        return self.macro[k] / self.n if self.n else 0.0

    def recall_micro(self, k: int) -> float:
        return self.hit[k] / self.total_evidence if self.total_evidence else 0.0

    def ceiling_at(self, k: int) -> float:
        return self.ceiling[k] / self.n if self.n else 0.0

    def type_recall(self, qtype: str, k: int) -> float:
        n = self.type_n[qtype]
        return self.by_type[qtype][k] / n if n else 0.0


def run_arm(
    corpus: list[dict[str, Any]],
    *,
    arm: str,
    semantic_model: Any | None,
    progress: bool,
) -> ArmResult:
    res = ArmResult(arm=arm)
    started = time.time()
    for i, inst in enumerate(corpus):
        evidence = set(inst["answer_session_ids"])
        if not evidence:
            # Abstention items have no evidence session, so recall is
            # undefined. The distributed corpus contains none of these,
            # but the guard stays: an upstream revision that restores
            # them must not silently score 0 for both systems.
            continue

        sids = inst["haystack_session_ids"]
        if len(set(sids)) != len(sids):
            res.dup_session_questions += 1

        root = Path(tempfile.mkdtemp(prefix="bm-lme-"))
        try:
            id_to_session, n_items = build_question_store(root, inst)
            memories = Store(root).load_all()
            hits = run_search(
                memories,
                inst["question"],
                max_results=RETRIEVAL_DEPTH,
                mode="hybrid",
                semantic_model=semantic_model,
            )
            ranked = distinct_sessions([h.id for h in hits], id_to_session)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        res.per_question.append(question_record(inst, ranked))
        res.items_written += n_items
        res.rounds_offered += sum(len(rounds_of(s)) for s in inst["haystack_sessions"])
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
            if len(ranked) < k:
                res.truncated[k] += 1

        if progress and (i + 1) % 25 == 0:
            done = i + 1
            rate = done / max(1e-9, time.time() - started)
            print(
                f"  [{arm}] {done}/{len(corpus)} "
                f"({rate:.1f} q/s, {res.recall_macro(5):.3f} macro recall@5 so far)",
                file=sys.stderr,
            )
    res.seconds = time.time() - started
    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _format_text(rows: list[ArmResult], meta: dict[str, Any]) -> str:
    out = [
        f"corpus: {meta['corpus']} ({meta['instances']} instances, "
        f"{meta['scored']} scored)",
        f"sha256: {meta['corpus_sha256']}",
        "",
        "session-level recall@k — macro-averaged, [ceiling] in brackets",
        "",
        "| arm      " + "".join(f"|{('@' + str(k)):^16}" for k in K_VALUES) + "|   n |",
        "|----------" + "|" + "|".join("-" * 16 for _ in K_VALUES) + "|-----|",
    ]
    for r in rows:
        cells = "".join(
            f"| {100 * r.recall_macro(k):>5.1f}% [{100 * r.ceiling_at(k):>4.0f}%] "
            for k in K_VALUES
        )
        out.append(f"| {r.arm:<8} {cells}| {r.n:>3} |")

    out += ["", "micro-averaged (evidence-weighted)", ""]
    out += [
        "| arm      " + "".join(f"|{('@' + str(k)):^8}" for k in K_VALUES) + "|",
        "|----------" + "|" + "|".join("-" * 8 for _ in K_VALUES) + "|",
    ]
    for r in rows:
        cells = "".join(f"| {100 * r.recall_micro(k):>5.1f}% " for k in K_VALUES)
        out.append(f"| {r.arm:<8} {cells}|")

    types = sorted({t for r in rows for t in r.type_n})
    if types:
        out += ["", "macro recall@5 by question type", ""]
        out += [
            "| question type                  |"
            + "".join(f"{r.arm:^10}|" for r in rows)
            + "   n |",
            "|--------------------------------|"
            + "".join("-" * 10 + "|" for _ in rows)
            + "-----|",
        ]
        for t in types:
            cells = "".join(f"{100 * r.type_recall(t, 5):>9.1f}%|" for r in rows)
            n_t = max(r.type_n[t] for r in rows)
            out.append(f"| {t:<30} |{cells}{n_t:>4} |")

    for r in rows:
        trunc = {k: v for k, v in r.truncated.items() if v}
        out.append(
            f"\n{r.arm}: {r.items_written:,} items written from "
            f"{r.rounds_offered:,} rounds offered "
            f"({100 * (1 - r.items_written / max(1, r.rounds_offered)):.2f}% shortfall), "
            f"{r.dup_session_questions} questions with duplicate session ids, "
            f"{r.seconds:.0f}s" + (f", depth-truncated at {trunc}" if trunc else "")
        )

    for note in meta.get("notes", []):
        out.append(f"\nnote: {note}")
    return "\n".join(out) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Session-level retrieval recall on LongMemEval.",
    )
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--arms", default="lexical,semantic")
    p.add_argument("--limit", type=int, default=None, help="First N instances (smoke).")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--per-question",
        default=None,
        metavar="PATH",
        help=(
            "Also write a per-question sidecar JSON to PATH. Carries this "
            "run's meta and, per arm, one record per scored question "
            "(see question_record). The published summary keeps its own "
            "shape; this is a separate dated artifact."
        ),
    )
    args = p.parse_args()

    corpus_path = Path(args.corpus).expanduser()
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    if not corpus_path.exists():
        print(
            f"missing corpus: {corpus_path}\n"
            "fetch it with:\n"
            "  mkdir -p bench/longmemeval/data && cd bench/longmemeval/data\n"
            "  curl -LO https://huggingface.co/datasets/xiaowu0162/"
            "longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json",
            file=sys.stderr,
        )
        return 1

    sha = corpus_fingerprint(corpus_path)
    notes: list[str] = []
    if sha not in KNOWN_CORPORA:
        notes.append(
            f"UNPINNED CORPUS — sha256 {sha[:16]}… is not a revision this "
            "runner has seen. Results are not comparable to published rows."
        )
    if sha == ORACLE_SHA:
        notes.append(
            "ORACLE VARIANT — contains evidence sessions and no distractors, "
            "so any working retriever scores ~1.0. Plumbing validation only; "
            "this figure MUST NOT be published as a result."
        )

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    instances = len(corpus)
    if args.limit:
        corpus = corpus[: args.limit]
        notes.append(
            f"SUBSET — first {len(corpus)} of {instances} instances, not a "
            "stratified sample. Question-type mix is skewed; not publishable."
        )

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    semantic_model = None
    if "semantic" in arms:
        from bettermemory.semantic import get_model, resolve_provider

        semantic_model = get_model(provider=resolve_provider())
        if semantic_model is None:
            arms = [a for a in arms if a != "semantic"]
            notes.append(
                "semantic arm SKIPPED — no embeddings extra importable. "
                "Install `bettermemory[embeddings]` to run it."
            )

    rows = [
        run_arm(
            corpus,
            arm=arm,
            semantic_model=semantic_model if arm == "semantic" else None,
            # Progress goes to stderr and the report to stdout, so a
            # --json run can still say where it is. Gating this on
            # `not args.json` bought nothing and made a 27-minute run
            # opaque enough that its liveness had to be inferred by
            # sampling temp directories.
            progress=not args.quiet,
        )
        for arm in arms
    ]

    notes.append(
        "claude-mem arms are NOT implemented yet — this run measures "
        "bettermemory only and licenses no comparative claim."
    )
    notes.append(
        f"ingest bypasses memory_write guardrails by design (Store.write, "
        f"src/bettermemory/store.py:411); stores hold ~245 items, below the "
        f"{INDEX_THRESHOLD}-item index threshold, so bm25 prefiltering never "
        "engages and the full store is ranked."
    )

    meta = {
        "corpus": corpus_path.name,
        "corpus_sha256": sha,
        "instances": instances,
        "scored": rows[0].n if rows else 0,
        "retrieval_depth": RETRIEVAL_DEPTH,
        "k_values": list(K_VALUES),
        "notes": notes,
    }

    if args.per_question:
        pq_path = Path(args.per_question).expanduser()
        if not pq_path.is_absolute():
            pq_path = (_HERE / pq_path).resolve()
        pq_path.parent.mkdir(parents=True, exist_ok=True)
        # `meta` rides along so a sidecar carries its own SUBSET /
        # UNPINNED / ORACLE notes: a per-question file separated from its
        # summary must still be able to disqualify itself.
        pq_path.write_text(
            json.dumps(
                {**meta, "arms": {r.arm: r.per_question for r in rows}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"per-question records written to {pq_path}", file=sys.stderr)

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
                            "items_written": r.items_written,
                            "rounds_offered": r.rounds_offered,
                            "dup_session_questions": r.dup_session_questions,
                            "macro": {
                                str(k): round(r.recall_macro(k), 4) for k in K_VALUES
                            },
                            "micro": {
                                str(k): round(r.recall_micro(k), 4) for k in K_VALUES
                            },
                            "ceiling": {
                                str(k): round(r.ceiling_at(k), 4) for k in K_VALUES
                            },
                            "depth_truncated": {
                                str(k): r.truncated[k] for k in K_VALUES
                            },
                            "by_type": {
                                t: {
                                    str(k): round(r.type_recall(t, k), 4)
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
        print(_format_text(rows, meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
