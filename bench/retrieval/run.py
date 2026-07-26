"""Retrieval benchmark — recall@k on a committed, blind-authored gold set.

WHY THIS EXISTS. The project's strongest retrieval claim (recall@1 rising
from 10% to 30% once an embedding model routes into ranking) reversed a
shipped default in 3.29.0, and until now it lived only in a commit
message and five docstrings — with the store size cited inconsistently as
185 in two places and 190 in four. A number that changed a default and
that nobody can re-derive is not evidence. This runner replaces it with
an artifact: committed corpus, committed questions, committed method,
one command.

BLIND AUTHORING. The corpus and the questions were written by different
authors that never saw each other's output. The only vocabulary they
shared is a kebab-case topic slug. This matters more than it sounds: the
usual way a retrieval benchmark flatters itself is that whoever wrote the
questions had the documents in front of them, so the questions
accidentally quote the documents and every ranker looks good. Here that
leak is closed structurally, not promised. Provenance and the exact
prompts are in `bench/retrieval/README.md`.

THE ARMS. Two of them mirror what a real user actually gets, rather than
artificial mode flags:

  lexical   mode="hybrid", no embedding model  — a default install
  semantic  mode="hybrid", embedding model     — `bettermemory[embeddings]`

and each is probed three ways:

  asked     the question as a developer would actually type it
  requery   the same need re-expressed in concrete nouns, second attempt
  control   the question with interrogative words stripped

The control is the arm that keeps the story honest. If `control` scores
the same as `asked`, the lift from `requery` is VOCABULARY (the caller
guessing words the document contains) and no amount of prompt-wording
guidance recovers it. If `control` scores like `requery`, the lift was
merely phrasing and the guidance is the cheaper fix. Reporting only
asked-vs-requery would leave that ambiguous, which is exactly how a
measurement becomes a talking point.

A KNOWN LIMIT, STATED UP FRONT. The default corpus sits below
`_INDEX_THRESHOLD_DEFAULT` (500), so retrieval scores the whole corpus.
Above that threshold production prefilters through SQLite bm25 and every
other ranker only REORDERS that top-50 — so a semantic leg cannot surface
a document bm25 never nominated. The 3.29.0 default flip was justified
entirely by a below-threshold measurement, which is the sharpest fair
criticism of it. `--pad-to` exists to measure the other regime: it appends
generated filler until the corpus crosses the threshold, and the report
records which regime each run was in. Padding changes the corpus, so a
padded run is reported as its own row and never merged with an unpadded
one.

Usage:

    venv/bin/python bench/retrieval/run.py                  # both arms
    venv/bin/python bench/retrieval/run.py --json           # machine-readable
    venv/bin/python bench/retrieval/run.py --pad-to 600     # above-threshold
    venv/bin/python bench/retrieval/run.py --arms lexical   # skip the model
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
import tempfile
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
CORPUS = _HERE / "corpus.jsonl"
QUESTIONS = _HERE / "questions.jsonl"

# Mirrors `_handlers._INDEX_THRESHOLD_DEFAULT`. Duplicated rather than
# imported because the point is to report which REGIME a run was in, and
# a silent import drift would make the label wrong without failing.
# tests/test_bench_retrieval.py cross-pins the two.
INDEX_THRESHOLD = 500

K_VALUES = (1, 5)

# Stripped for the control arm. Interrogatives and discourse filler only —
# never content words, or the control would stop being a control.
_QUESTION_WORDS = frozenset(
    """
    what why how when where which who whose whom did do does is are was were
    we our us i my me you your the a an again ever still about around
    deal with something anything some any there here that this those these
    ok okay hey so just really actually kinda sorta maybe remember
    """.split()
)


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# Deliberately OFF-DOMAIN. The first version of this list used plausible
# ops vocabulary (`cutover`, `rollout`, `telemetry`, `cache`) and
# tests/test_bench_retrieval.py caught it overlapping gold documents on
# eight terms — which would have let filler compete for gold probes and
# turned the threshold experiment into a measurement of this generator.
# Mineral and botanical terms cannot collide with a software-operations
# corpus, and the test pins that they do not.
#
# The cost is stated rather than hidden: because filler is off-domain,
# `--pad-to` is a corpus-SIZE control, not realistic competition. A real
# 600-memory store would contain 400+ documents that genuinely contend.
# See the README's threshold caveat.
_FILLER_VOCAB = (
    "basalt feldspar gneiss olivine quartzite schist granite obsidian "
    "sediment alluvium moraine tundra lichen bracken sedge yarrow "
    "hawthorn juniper alder rowan bramble heather gorse thistle "
    "estuary headland moorland fenland"
).split()


def _filler_body(seed: int) -> str:
    """Generate padding that cannot be retrieved by a gold probe.

    Padding exists to move the corpus across the index threshold, not to
    be findable — if filler ever won a gold probe the threshold
    experiment would be measuring this function instead of the ranker.
    """
    rng = random.Random(seed)
    words = rng.sample(_FILLER_VOCAB, k=min(14, len(_FILLER_VOCAB)))
    return (
        f"Field note {seed}. "
        + " ".join(words).capitalize()
        + ". Catalogued during a survey pass."
    )


def corpus_fingerprint(path: Path) -> str:
    """SHA-256 of the corpus file.

    Every published result records this. A benchmark number that does not
    name the corpus it ran against stops being reproducible the moment
    the corpus changes — which is exactly the failure this directory
    exists to fix, so it would be absurd to reproduce it here.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_store(
    root: Path, corpus_path: Path, *, pad_to: int | None
) -> tuple[dict[str, str], int]:
    """Write the corpus into `root`. Returns (slug -> memory id, corpus size)."""
    store = Store(root)
    slug_to_id: dict[str, str] = {}
    n = 0
    for row in _read_jsonl(corpus_path):
        memory = store.write(content=row["body"], scopes=row["scopes"])
        slug_to_id[row["slug"]] = memory.id
        n += 1
    if pad_to:
        for i in range(max(0, pad_to - n)):
            store.write(content=_filler_body(i), scopes=["ops-routine"])
            n += 1
    return slug_to_id, n


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def strip_question_words(text: str) -> str:
    kept = [
        w
        for w in text.replace("?", " ").replace(",", " ").split()
        if w.lower().strip(".!'\"") not in _QUESTION_WORDS
    ]
    return " ".join(kept)


@dataclass
class ArmResult:
    arm: str
    probe: str
    n: int = 0
    hits_at: dict[int, int] = field(default_factory=dict)

    def recall(self, k: int) -> float:
        return self.hits_at.get(k, 0) / self.n if self.n else 0.0


def run_arm(
    memories: list[Any],
    questions: list[dict[str, Any]],
    slug_to_id: dict[str, str],
    *,
    arm: str,
    probe: str,
    semantic_model: Any | None,
) -> ArmResult:
    result = ArmResult(arm=arm, probe=probe)
    for q in questions:
        gold_id = slug_to_id.get(q["slug"])
        if gold_id is None:
            continue
        if probe == "asked":
            query = q["question"]
        elif probe == "requery":
            query = q["requery"]
        else:
            query = strip_question_words(q["question"])
        hits = run_search(
            memories,
            query,
            max_results=max(K_VALUES),
            mode="hybrid",
            semantic_model=semantic_model,
        )
        ranked = [h.id for h in hits]
        result.n += 1
        for k in K_VALUES:
            if gold_id in ranked[:k]:
                result.hits_at[k] = result.hits_at.get(k, 0) + 1
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _format_text(
    rows: list[ArmResult], corpus_n: int, notes: list[str], name: str
) -> str:
    out = [
        f"corpus: {name} — {corpus_n} memories "
        f"({'ABOVE' if corpus_n >= INDEX_THRESHOLD else 'below'} the "
        f"{INDEX_THRESHOLD}-memory index threshold)",
        "",
        "| arm      | probe   | recall@1 | recall@5 | n  |",
        "|----------|---------|----------|----------|----|",
    ]
    for r in rows:
        out.append(
            f"| {r.arm:<8} | {r.probe:<7} "
            f"| {100 * r.recall(1):>6.0f}%  "
            f"| {100 * r.recall(5):>6.0f}%  "
            f"| {r.n:>2} |"
        )
    if notes:
        out += [""] + [f"note: {n}" for n in notes]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "recall@k on a blind-authored gold set, with and without an "
            "embedding model routed into ranking."
        ),
    )
    parser.add_argument(
        "--arms",
        default="lexical,semantic",
        help="Comma-separated subset of `lexical,semantic`. Default both.",
    )
    parser.add_argument(
        "--pad-to",
        type=int,
        default=None,
        help=(
            f"Pad the corpus with filler to N memories, to measure the "
            f"above-{INDEX_THRESHOLD} prefilter regime. Reported separately."
        ),
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default=str(CORPUS),
        help=(
            "Corpus JSONL. Defaults to the canonical corpus.jsonl. Point at "
            "corpus-v1.jsonl to reproduce the superseded first-run figures."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    corpus_path = Path(args.corpus).expanduser()
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    if not corpus_path.exists() or not QUESTIONS.exists():
        print(f"missing corpus or questions under {_HERE}", file=sys.stderr)
        return 1

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    notes: list[str] = []

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

    root = Path(tempfile.mkdtemp(prefix="bm-retrieval-"))
    try:
        slug_to_id, corpus_n = build_store(root, corpus_path, pad_to=args.pad_to)
        memories = Store(root).load_all()
        questions = _read_jsonl(QUESTIONS)

        rows: list[ArmResult] = []
        for arm in arms:
            model = semantic_model if arm == "semantic" else None
            for probe in ("asked", "requery", "control"):
                rows.append(
                    run_arm(
                        memories,
                        questions,
                        slug_to_id,
                        arm=arm,
                        probe=probe,
                        semantic_model=model,
                    )
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if corpus_n >= INDEX_THRESHOLD:
        notes.append(
            "corpus is above the index threshold, but this runner ranks the "
            "full corpus — production would prefilter through SQLite bm25 "
            "first. Treat this as an upper bound on the semantic arm."
        )

    if args.json:
        print(
            json.dumps(
                {
                    "corpus": corpus_path.name,
                    "corpus_sha256": corpus_fingerprint(corpus_path),
                    "corpus_size": corpus_n,
                    "index_threshold": INDEX_THRESHOLD,
                    "above_threshold": corpus_n >= INDEX_THRESHOLD,
                    "padded": bool(args.pad_to),
                    "notes": notes,
                    "results": [
                        {
                            "arm": r.arm,
                            "probe": r.probe,
                            "n": r.n,
                            "recall_at_1": round(r.recall(1), 4),
                            "recall_at_5": round(r.recall(5), 4),
                        }
                        for r in rows
                    ],
                },
                indent=2,
            )
        )
    else:
        print(_format_text(rows, corpus_n, notes, corpus_path.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
