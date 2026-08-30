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
prompts are in the retrieval-bench notes.

THE ARMS. Two of them mirror what a real user actually gets, rather than
artificial mode flags:

  lexical   mode="hybrid", no embedding model  — a default install

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

THE THRESHOLD, AND THE TWO KNOBS THAT REACH IT. The default corpus sits
below `_INDEX_THRESHOLD_DEFAULT` (500), so retrieval scores the whole
corpus. Above that threshold production prefilters through SQLite bm25
and every other ranker only REORDERS that top-50 — no ranker can
surface a document bm25 never nominated. The 3.29.0 default flip
was justified entirely by a below-threshold measurement, which is the
sharpest fair criticism of it.

Reaching the other regime takes BOTH knobs, and conflating them is how
this runner spent four published artifacts measuring the wrong thing:

  --pad-to N     grows the CORPUS past the threshold. Padding changes
                 the corpus, so a padded run is reported as its own row
                 and never merged with an unpadded one.
  --prefilter    picks the CODE PATH. Default `off` ranks the full
                 corpus in-process. `on` drives production's own loader
                 (`handlers.search.resolve_search_pool`), so bm25
                 nominates the pool and corpus-IDF prices the terms.

`--pad-to` alone measures dilution, not prefiltering: the pool is still
the whole corpus. `--prefilter both` runs the same queries against the
same store twice and reports the paired difference, which is the only
form in which the prefilter's recall cost is a measurement rather than
two numbers from two runs. Every question in an `on` arm is checked for
engagement and the run FAILS if any of them silently fell back — see
`run_arm_prefiltered`.

Usage:

    venv/bin/python bench/retrieval/run.py                  # both arms
    venv/bin/python bench/retrieval/run.py --json           # machine-readable
    venv/bin/python bench/retrieval/run.py --pad-to 600     # above-threshold
    venv/bin/python bench/retrieval/run.py --arms lexical   # skip the model
    venv/bin/python bench/retrieval/run.py --pad-to 600 --prefilter both
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
# ...and `bench/`, for the shared interval module. Same reason: the
# instrument has to stay runnable straight from a checkout.
_BENCH = Path(__file__).resolve().parents[1]
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))


from interval import min_n_for, read_delta, wilson  # noqa: E402
from bettermemory import index as _index  # noqa: E402
from bettermemory._handlers import (  # noqa: E402
    _PREFILTER_CAP,
    resolve_index_threshold,
)
from bettermemory.handlers.search import resolve_search_pool  # noqa: E402
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

# `_PREFILTER_CAP` gets the opposite treatment — imported, not copied.
# INDEX_THRESHOLD labels a run, so a silent drift there would mislabel a
# result and must be caught by a test; the cap is a measured property of
# the pool the prefilter actually served, so the artifact should follow
# production wherever it moves rather than record a stale 50.
PREFILTER_CAP = _PREFILTER_CAP

# The env var `_handlers.resolve_index_threshold()` re-reads on EVERY
# search, so `--index-threshold` can flip the regime with no rebuild.
# Written inside `main()` ONLY: tests/test_bench_retrieval.py exec's this
# module at pytest COLLECTION time, so a module-scope write would leak
# the prefilter regime into every other test in the session.
INDEX_THRESHOLD_ENV = "BETTERMEMORY_INDEX_THRESHOLD"

# Whether the arms rank with the 5.1 rescue-expansion repairs
# (`search.search(rescue_expansion=...)`). Module-level and defaulting
# to the PRODUCT default (off) so a bare invocation measures what a
# default install ranks with; `--rescue-expansion on` reproduces the
# 2026-08-09 lane artifacts. Set inside `main()` from the flag.
RESCUE_EXPANSION = False

# Whether the rescue leg's vote is conditioned on its own separation
# (`search._RESCUE_LEG_MIN_EVIDENCE`, rounds 3-5). Module-level and
# defaulting to the SHIPPED behaviour; `off` drives the leg's
# evidence floor to zero, which is the pre-cap engine and the paired
# control addenda 5, 6 and 7 all require as arm 2. Nothing here changes a default install:
# the cap lives inside the opt-in lane either way.
LEG_MARGIN_CAP = True

# Whether the arms rank with the Lane L conversational repairs
# (`search.search(conversational=...)`, bench/l/L1_DECLARATION.md).
# Module-level, defaulting to the PRODUCT default, same shape as
# RESCUE_EXPANSION. The product default flipped ON at 6.1.0 (the L1
# ship) and this default flipped with it. The lane is measured INERT on
# this instrument — the L1 gate's dev results block is byte-identical
# lane-on vs lane-off (bench/l/results/gate-dev-conv-2026-08-16.json) —
# so the flip changes no number here; `--conversational off` stays the
# committed-baseline arm for exactness.
CONVERSATIONAL = True

# Digest of the corpus the four committed artifacts ran against. The
# `off` half of a `--prefilter both` run re-measures exactly what
# `v2-padded600-2026-07-26.json` already recorded, which is what turns it
# from a second unvalidated number into a harness self-check — but only
# while the corpus is byte-identical, so the claim is gated on the digest
# instead of asserted in prose.
_V2_CORPUS_SHA256 = "c40acee95ce1bb70ac6ea788e0fb4a9a1c6eff1fc55fe4569b651b5b156ea2ea"

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


def _provenance() -> dict[str, object]:
    """Version + commit + platform stamp for the emitted artifact.

    An artifact that cannot say which engine produced it ages into a
    number nobody can re-derive — the exact failure this benchmark was
    built to end. Best-effort: outside a git checkout the commit reads
    None rather than failing the run. `tree_dirty` counts tracked
    modifications only, so a run's own freshly written result files
    (untracked) don't mark it dirty.
    """
    import platform
    import subprocess
    from datetime import date

    import bettermemory

    commit: str | None = None
    tree_dirty: bool | None = None
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
            or None
        )
        tree_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
        )
    except OSError:
        pass
    return {
        "bettermemory_version": bettermemory.__version__,
        "commit": commit,
        "tree_dirty": tree_dirty,
        "date": date.today().isoformat(),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


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


def _query_for(q: dict[str, Any], probe: str) -> str:
    """The one place a probe name becomes a query string.

    Shared by both arms on purpose: the prefilter measurement is a PAIRED
    comparison, and it is only paired if the two arms ask byte-identical
    questions. A second copy of this three-way branch is how the two
    halves would drift apart without either of them looking wrong.
    """
    if probe == "asked":
        return str(q["question"])
    if probe == "requery":
        return str(q["requery"])
    return strip_question_words(q["question"])


@dataclass
class ArmResult:
    """One (arm, probe, prefilter) cell of the report.

    Everything past `hits_at` describes the CANDIDATE POOL rather than the
    ranking, and exists because a prefilter measurement that only reports
    recall cannot distinguish "the prefilter cost nothing" from "the
    prefilter never ran". `engaged` is the integrity counter;
    `nominated` is the ceiling recall could possibly have reached.
    """

    arm: str
    probe: str
    n: int = 0
    hits_at: dict[int, int] = field(default_factory=dict)
    prefilter: bool = False
    engaged: int = 0
    nominated: int = 0
    pool_sizes: list[int] = field(default_factory=list)
    # Queries whose pool came back un-prefiltered. Non-empty means the
    # run measured `load_all` while claiming to measure the prefilter.
    unengaged: list[str] = field(default_factory=list)
    # Per-question outcomes, kept so the arms can be compared PAIRED.
    # `hits_at` alone forces an unpaired test, which throws away the
    # instrument's one statistical advantage: both arms answer the same
    # questions, so the questions both got and both missed carry no
    # information about the difference between them.
    per_question: list[dict[str, Any]] = field(default_factory=list)

    def hit_vector(self, k: int) -> list[bool]:
        """Per-question hit/miss at depth k, in question order."""
        return [bool(r["hit_at"].get(str(k), False)) for r in self.per_question]

    def recall(self, k: int) -> float:
        return self.hits_at.get(k, 0) / self.n if self.n else 0.0

    def nomination_rate(self) -> float:
        """Share of questions whose gold document reached the pool at all.

        A hard ceiling on `recall(k)`: no ranker can return a document its
        pool does not contain. At 1.0 nomination cannot explain any part
        of a recall difference, so whatever delta remains is re-ranking.
        """
        return self.nominated / self.n if self.n else 0.0

    def mean_pool(self) -> float:
        return sum(self.pool_sizes) / len(self.pool_sizes) if self.pool_sizes else 0.0


def run_arm(
    memories: list[Any],
    questions: list[dict[str, Any]],
    slug_to_id: dict[str, str],
    *,
    arm: str,
    probe: str,
) -> ArmResult:
    """Rank the full corpus in-process — the pre-3.30 arm, unchanged.

    Keeps `memories: list[Any]` as its first parameter rather than a
    `Store`, because that signature is what makes the four committed
    artifacts still reproducible byte-for-byte.
    """
    result = ArmResult(arm=arm, probe=probe, prefilter=False)
    # Constant across questions, so hoisted: the whole point of this arm
    # is that the pool never depends on the query.
    pool_ids = {m.id for m in memories}
    for q in questions:
        gold_id = slug_to_id.get(q["slug"])
        if gold_id is None:
            continue
        query = _query_for(q, probe)
        hits = run_search(
            memories,
            query,
            max_results=max(K_VALUES),
            mode="hybrid",
            rescue_expansion=RESCUE_EXPANSION,
            conversational=CONVERSATIONAL,
        )
        ranked = [h.id for h in hits]
        result.n += 1
        result.pool_sizes.append(len(memories))
        # Definitionally 1.0 over a full corpus — recorded rather than
        # assumed so the paired report reads off one measurement, and so
        # a broken store shows up as a nomination rate below 1 instead of
        # as an unexplained recall drop.
        if gold_id in pool_ids:
            result.nominated += 1
        hit_at: dict[str, bool] = {}
        for k in K_VALUES:
            hit = gold_id in ranked[:k]
            hit_at[str(k)] = hit
            if hit:
                result.hits_at[k] = result.hits_at.get(k, 0) + 1
        result.per_question.append({"slug": q["slug"], "hit_at": hit_at})
    return result


def run_arm_prefiltered(
    store: Store,
    questions: list[dict[str, Any]],
    slug_to_id: dict[str, str],
    *,
    arm: str,
    probe: str,
) -> ArmResult:
    """Rank what production would actually have ranked.

    `run_arm` hands `search.search` a list someone else built. Production
    never does that: `memory_search` and both silent-miss producers go
    through `handlers.search.resolve_search_pool`, which above the index
    threshold serves a bm25-nominated slice capped at `PREFILTER_CAP` and
    hands back a corpus-statistics provider so term rarity is still
    priced against the whole store. Driving that call is the entire
    difference between this arm and the other one, and `--pad-to` alone
    never crossed it.

    THE FILTERS STAY NONE, AND THAT IS LOAD-BEARING. `resolve_search_pool`
    runs a cap-starvation guard that reloads the full corpus and clears
    the prefiltered flag when a post-cap filter leaves too few survivors.
    That guard is gated on at least one of `repo_filter`, `worktree_filter`
    or `excluded_scopes` being set — so passing any of them "to look more
    like production" would let a saturated slice silently turn this arm
    back into `run_arm`, and the report would print prefilter numbers
    that are not. `min_survivors` is required by the signature and
    unreachable for the same reason.
    """
    result = ArmResult(arm=arm, probe=probe, prefilter=True)
    for q in questions:
        gold_id = slug_to_id.get(q["slug"])
        if gold_id is None:
            continue
        query = _query_for(q, probe)
        pool = resolve_search_pool(
            store,
            query,
            scopes=None,
            excluded_scopes=None,
            repo_filter=None,
            worktree_filter=None,
            min_survivors=max(K_VALUES),
        )
        # An exact IFF, not a heuristic: `resolve_search_pool` attaches a
        # corpus-statistics provider if and only if the FTS path served
        # the pool. All seven fallbacks (empty query, index absent /
        # corrupt / needing rebuild, count below threshold, index read
        # raising, empty FTS match set, every candidate unloadable, and
        # the starvation reload) return the full corpus with the flag
        # clear, and every one of them looks like a successful cheap
        # search from here.
        if pool.corpus_stats_provider is None:
            result.unengaged.append(query)
        else:
            result.engaged += 1
        hits = run_search(
            pool.memories,
            query,
            max_results=max(K_VALUES),
            mode="hybrid",
            rescue_expansion=RESCUE_EXPANSION,
            conversational=CONVERSATIONAL,
            # The part `run_arm` cannot pass. Without it a capped pool is
            # scored with pool-derived document frequencies, which prices
            # a term that is rare in the store as common in the slice —
            # a strawman prefilter that loses recall this arm would then
            # blame on the cap.
            corpus_stats_provider=pool.corpus_stats_provider,
        )
        ranked = [h.id for h in hits]
        result.n += 1
        result.pool_sizes.append(len(pool.memories))
        if gold_id in {m.id for m in pool.memories}:
            result.nominated += 1
        hit_at: dict[str, bool] = {}
        for k in K_VALUES:
            hit = gold_id in ranked[:k]
            hit_at[str(k)] = hit
            if hit:
                result.hits_at[k] = result.hits_at.get(k, 0) + 1
        result.per_question.append({"slug": q["slug"], "hit_at": hit_at})
    return result


@dataclass
class Delta:
    """The paired difference for one (arm, probe) cell.

    Positive `recall_loss_at_k` means the prefilter cost recall. Negative
    is a real outcome, not a bug: both arms price terms against the same
    document frequencies (the off arm's pool IS the corpus, and the on
    arm gets the corpus-statistics provider), so the only asymmetry left
    is that the on arm ranks 50 candidates instead of all of them — and
    dropping a competitor that was outranking gold moves gold up.
    """

    arm: str
    probe: str
    recall_loss_at: dict[int, float]
    gold_nomination_rate: float


def paired_deltas(rows: list[ArmResult]) -> list[Delta]:
    """Subtract the on-arm from the off-arm cell by cell.

    Pairs only cells that ran in the SAME process against the SAME store,
    which is the whole discipline here: comparing an above-threshold
    prefiltered run against a differently-sized committed artifact would
    confound the prefilter with corpus size.
    """
    off = {(r.arm, r.probe): r for r in rows if not r.prefilter}
    deltas = []
    for on in rows:
        if not on.prefilter or (on.arm, on.probe) not in off:
            continue
        base = off[(on.arm, on.probe)]
        deltas.append(
            Delta(
                arm=on.arm,
                probe=on.probe,
                recall_loss_at={k: base.recall(k) - on.recall(k) for k in K_VALUES},
                gold_nomination_rate=on.nomination_rate(),
            )
        )
    return deltas


def engagement_failure(root: Path, rows: list[ArmResult]) -> str | None:
    """Diagnose an on-arm that never reached the prefilter.

    Returns None when every prefiltered arm asked questions and every one
    of those questions engaged, otherwise a report naming the regime the
    store was actually in. This is the only
    thing standing between an honest measurement and a set of numbers
    that look like one: every fallback in the loader returns the full
    corpus quietly, so a run that fell back scores like an ordinary
    `run_arm` and prints under a `prefilter: true` heading.
    """
    # An arm that asked NOTHING is a failure too, not a pass by absence of
    # evidence: `recall()` is 0.0 over zero questions, so `paired_deltas`
    # reports a 0.0 loss — the exact shape of "the prefilter cost nothing".
    # Only a `--corpus` whose slugs miss `questions.jsonl` reaches it, but
    # that flag exists and the guard is worth nothing if it has a hole.
    failed = [r for r in rows if r.prefilter and (r.unengaged or not r.n)]
    if not failed:
        return None
    status = _index.status(root)
    lines = [
        "PREFILTER NEVER ENGAGED — this run measured the full corpus "
        "while reporting it as prefiltered. Refusing to emit.",
        f"  index: exists={status.get('exists')} "
        f"corrupt={status.get('corrupt')} "
        f"needs_rebuild={status.get('needs_rebuild')} "
        f"indexed_count={status.get('indexed_count')} "
        f"(threshold in force: {resolve_index_threshold()})",
    ]
    for r in failed:
        if not r.n:
            lines.append(
                f"  {r.arm}/{r.probe}: no question matched the corpus, so this "
                f"arm measured nothing"
            )
            continue
        lines.append(
            f"  {r.arm}/{r.probe}: {len(r.unengaged)}/{r.n} queries fell back, "
            f"first: {r.unengaged[0]!r}"
        )
    lines.append(
        "  If indexed_count is below the threshold, pass --pad-to 600 or "
        "--index-threshold N. If it is above, the FTS match set was empty "
        "for those queries or the candidates would not load."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _format_text(
    rows: list[ArmResult],
    corpus_n: int,
    notes: list[str],
    name: str,
    deltas: list[Delta] | None = None,
) -> str:
    # The pool columns appear only when a prefiltered arm ran, so a
    # default run's report stays byte-identical to every earlier one —
    # this addition is not allowed to make old output look changed.
    show_pool = any(r.prefilter for r in rows)
    out = [
        f"corpus: {name} — {corpus_n} memories "
        f"({'ABOVE' if corpus_n >= INDEX_THRESHOLD else 'below'} the "
        f"{INDEX_THRESHOLD}-memory index threshold)",
        "",
    ]
    if show_pool:
        out += [
            "| arm      | probe   | prefilter | recall@1 | recall@5 "
            "| pool | gold in pool | n  |",
            "|----------|---------|-----------|----------|----------"
            "|------|--------------|----|",
        ]
    else:
        out += [
            "| arm      | probe   | recall@1 | recall@5 | n  |",
            "|----------|---------|----------|----------|----|",
        ]
    for r in rows:
        core = f"| {100 * r.recall(1):>6.0f}%  | {100 * r.recall(5):>6.0f}%  "
        if show_pool:
            out.append(
                f"| {r.arm:<8} | {r.probe:<7} "
                f"| {'on ' if r.prefilter else 'off':<9} "
                + core
                + f"| {r.mean_pool():>4.0f} "
                f"| {100 * r.nomination_rate():>10.0f}%  "
                f"| {r.n:>2} |"
            )
        else:
            out.append(f"| {r.arm:<8} | {r.probe:<7} " + core + f"| {r.n:>2} |")
    if deltas:
        out += [
            "",
            "recall lost to the prefilter (off minus on, in points):",
            "| arm      | probe   | @1  | @5  |",
            "|----------|---------|-----|-----|",
        ]
        for d in deltas:
            out.append(
                f"| {d.arm:<8} | {d.probe:<7} "
                f"| {100 * d.recall_loss_at[1]:>+3.0f} "
                f"| {100 * d.recall_loss_at[5]:>+3.0f} |"
            )
    out += _reading_section(rows)
    if notes:
        out += [""] + [f"note: {n}" for n in notes]
    return "\n".join(out) + "\n"


def _reading_section(rows: list[ArmResult]) -> list[str]:
    """How much of each recall figure is the instrument rather than the engine.

    Appended BELOW the table rather than added as columns, for the same
    reason `show_pool` is conditional: a default run's table stays
    byte-identical to every earlier one, so this addition cannot make
    old output look changed.

    The paired rows compare probes WITHIN this run, which is the only
    comparison a single invocation can make honestly. The comparison
    the gate reads actually want — one table against another — spans
    invocations, and `--compare` does it against a prior artifact that
    carries `per_question`.
    """
    if not rows:
        return []
    n = rows[0].n
    out = [
        "",
        f"reading — 95% Wilson intervals on n={n}:",
        "| arm      | probe   | recall@1 95% CI  | recall@5 95% CI  |",
        "|----------|---------|------------------|------------------|",
    ]
    for r in rows:
        lo1, hi1 = wilson(r.hits_at.get(1, 0), r.n)
        lo5, hi5 = wilson(r.hits_at.get(5, 0), r.n)
        out.append(
            f"| {r.arm:<8} | {r.probe:<7} "
            f"| [{100 * lo1:>4.0f}%, {100 * hi1:>4.0f}%]  "
            f"| [{100 * lo5:>4.0f}%, {100 * hi5:>4.0f}%]  |"
        )
    # The resolution floor, stated rather than left for a reader to
    # derive. The floor in QUESTIONS is a property of the test, not of
    # the instrument: six discordant questions all moving one way is
    # p=0.031 whether n is twenty or twelve hundred. What n changes is
    # what those six questions are WORTH — 30 points here, 5 points at
    # n=120 — and that is the whole argument for growing the corpus.
    floor = _one_way_floor(n)
    out += [
        "",
        f"paired resolution floor: {floor} questions moving one way "
        f"(McNemar exact, alpha=0.05) — at n={n} that is "
        f"{100 * floor / n:.0f} points.",
        f"separating 55% from 60% at 80% power would need "
        f"~{min_n_for(0.55, 0.60):,} questions per arm.",
    ]
    base = rows[0]
    if len(rows) > 1:
        out += ["", f"paired against {base.arm}/{base.probe} (same questions):"]
        for r in rows[1:]:
            for k in K_VALUES:
                d = read_delta(r.hit_vector(k), base.hit_vector(k))
                out.append(
                    f"  @{k} "
                    + d.line(f"{r.arm}/{r.probe}", f"{base.arm}/{base.probe}")
                )
    return out


def _format_comparison(rows: list[ArmResult], prior_path: Path) -> str:
    """Paired reading of this run against a prior artifact.

    Pairs on question SLUG rather than position, so a comparison
    survives a reordered question file and refuses a mismatched one
    instead of quietly comparing different questions to each other.
    """
    try:
        prior = json.loads(prior_path.read_text())
    except (OSError, ValueError) as exc:
        return f"\n--compare: cannot read {prior_path}: {exc}\n"
    out = ["", f"paired against {prior_path.name} (McNemar exact, by slug):"]
    # Two artifact shapes in the tree. `run.py --json` writes `results`
    # at the top level; the Lane W gate wrapper nests that whole payload
    # under `runner` and adds its own keys beside it. Unwrap rather than
    # require the caller to know which they have.
    prior_rows = prior.get("results") or prior.get("runner", {}).get("results", [])
    if not prior_rows:
        out.append(f"  no results rows found in {prior_path.name}.")
        return "\n".join(out) + "\n"
    unpairable = [
        f"{r.get('arm')}/{r.get('probe')}"
        for r in prior_rows
        if not r.get("per_question")
    ]
    if unpairable:
        out.append(
            f"  {len(unpairable)} of {len(prior_rows)} prior cell(s) carry no "
            f"per-question record ({', '.join(sorted(set(unpairable)))}) — "
            f"written before that field existed. Unpairable, and NOT "
            f"compared unpaired."
        )
    if len(unpairable) == len(prior_rows):
        out.append(
            "  nothing to compare. Every published artifact predating this "
            "field is in that position: the paired reading is available "
            "from here forward, not retroactively."
        )
    for r in rows:
        match = next(
            (
                pr
                for pr in prior_rows
                if pr.get("arm") == r.arm
                and pr.get("probe") == r.probe
                and pr.get("prefilter", False) == r.prefilter
                and pr.get("per_question")
            ),
            None,
        )
        if match is None:
            continue
        prior_by_slug = {q["slug"]: q for q in match["per_question"]}
        mine_by_slug = {q["slug"]: q for q in r.per_question}
        shared = sorted(set(prior_by_slug) & set(mine_by_slug))
        if len(shared) != len(mine_by_slug) or len(shared) != len(prior_by_slug):
            out.append(
                f"  {r.arm}/{r.probe}: question sets differ "
                f"({len(mine_by_slug)} here, {len(prior_by_slug)} prior, "
                f"{len(shared)} shared) — comparing the shared "
                f"{len(shared)} only."
            )
        for k in K_VALUES:
            mine = [bool(mine_by_slug[sl]["hit_at"].get(str(k))) for sl in shared]
            theirs = [bool(prior_by_slug[sl]["hit_at"].get(str(k))) for sl in shared]
            d = read_delta(mine, theirs)
            out.append(f"  @{k} " + d.line(f"{r.arm}/{r.probe} (now)", "prior"))
    return "\n".join(out) + "\n"


def _one_way_floor(n: int) -> int:
    """Smallest all-one-direction discordant count reaching p<0.05."""
    from interval import mcnemar_exact as _mx

    for d in range(1, n + 1):
        if _mx(d, 0) < 0.05:
            return d
    return n + 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "recall@k on a blind-authored gold set, with and without an "
            "embedding model routed into ranking."
        ),
    )
    parser.add_argument(
        "--arms",
        default="lexical",
        help="Arms to run. Only `lexical` exists.",
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
        "--prefilter",
        choices=("off", "on", "both"),
        default="off",
        help=(
            "Which code path ranks. 'off' (default) ranks the full corpus "
            "in-process and reproduces the pre-3.30 artifacts. 'on' drives "
            "production's resolve_search_pool, so bm25 nominates the pool. "
            "'both' runs the same queries twice and reports the difference."
        ),
    )
    parser.add_argument(
        "--index-threshold",
        type=int,
        default=None,
        help=(
            f"Override {INDEX_THRESHOLD_ENV} for this run, to reach the "
            f"prefilter regime without padding the corpus. Must be > 0: "
            f"resolve_index_threshold() silently falls back to "
            f"{INDEX_THRESHOLD} at <= 0, which would quietly measure the "
            f"wrong regime."
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
    parser.add_argument(
        "--questions",
        type=str,
        default=str(QUESTIONS),
        help=(
            "Questions JSONL. Defaults to the canonical questions.jsonl. "
            "Point at questions-v2.jsonl for the original-twenty cell — "
            "either against corpus-v2.jsonl (the I1 integrity anchor, the "
            "old instrument reproduced whole) or against the full corpus "
            "(the original-twenty subset scored in the expanded field). "
            "A question whose slug has no gold document in the corpus is a "
            "hard error, not a silent exclusion."
        ),
    )
    parser.add_argument(
        "--rescue-expansion",
        choices=("on", "off"),
        default="off",
        help=(
            "Rank with the 5.1 rescue-expansion repairs (filler df-floor + "
            "gated vocabulary leg). Default off — the product default, after "
            "the lane's preregistered LongMemEval check killed default-on. "
            "'on' reproduces the *-2026-08-09 lane artifacts."
        ),
    )
    parser.add_argument(
        "--leg-margin-cap",
        choices=("on", "off"),
        default="on",
        help=(
            "Condition the rescue leg's vote on its own separation "
            "(round 3, addendum 5). Default on — the shipped lane "
            "behaviour. 'off' is the pre-round-3 engine, the paired "
            "control for the capped arm."
        ),
    )
    parser.add_argument(
        "--evidence-scaling",
        choices=("on", "off"),
        default="off",
        help=(
            "Scale the rescue leg's weight by its evidence instead of the "
            "shipped flat weight. Default off — the shipped lane form. 'on' "
            "reproduces the round-6/7 arms."
        ),
    )
    parser.add_argument(
        "--base-withhold",
        choices=("on", "off"),
        default="off",
        help=(
            "Withhold the trailing base leg from hybrid fusion (round 9, "
            "addendum 12). Default off — the shipped engine. 'on' is the "
            "mechanism arm."
        ),
    )
    parser.add_argument(
        "--conversational",
        choices=("on", "off"),
        default="on",
        help=(
            "Rank with the Lane L conversational repairs "
            "(bench/l/L1_DECLARATION.md). Default on — the product "
            "default since 6.1.0. Measured inert on this instrument "
            "(no dev query carries a temporal reading)."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--compare",
        metavar="PRIOR.json",
        help=(
            "Read this run PAIRED against a prior artifact, question by "
            "question (McNemar exact). The comparison every gate read "
            "makes — one table against another — spans two invocations, "
            "so it cannot be done from a single run's rows. Requires the "
            "prior artifact to carry `per_question`; artifacts written "
            "before that field existed are reported as unpairable rather "
            "than silently compared unpaired."
        ),
    )
    args = parser.parse_args()

    # Module-level so the two arm runners read one flag without a
    # signature change (`run_arm`'s signature is pinned by the committed
    # artifacts' reproducibility note above it).
    global RESCUE_EXPANSION, LEG_MARGIN_CAP, CONVERSATIONAL
    RESCUE_EXPANSION = args.rescue_expansion == "on"
    LEG_MARGIN_CAP = args.leg_margin_cap == "on"
    CONVERSATIONAL = args.conversational == "on"

    import bettermemory.search as _engine

    if args.evidence_scaling == "on":
        _engine._RESCUE_LEG_EVIDENCE_SCALING = True
    if args.base_withhold == "on":
        _engine._BASE_LEG_TRAILING_WITHHOLD = True
    if not LEG_MARGIN_CAP:
        _engine._RESCUE_LEG_MIN_EVIDENCE = 0

    corpus_path = Path(args.corpus).expanduser()
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    questions_path = Path(args.questions).expanduser()
    if not questions_path.is_absolute():
        questions_path = (_HERE / questions_path).resolve()
    if not corpus_path.exists() or not questions_path.exists():
        print(f"missing corpus or questions under {_HERE}", file=sys.stderr)
        return 1

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    notes: list[str] = []
    modes = {"off": [False], "on": [True], "both": [False, True]}[args.prefilter]

    if args.index_threshold is not None:
        if args.index_threshold <= 0:
            print(
                f"--index-threshold must be > 0; resolve_index_threshold() "
                f"treats <= 0 as unset and falls back to {INDEX_THRESHOLD}, "
                f"so the run would silently measure the wrong regime.",
                file=sys.stderr,
            )
            return 1
        # Function-scoped on purpose — see INDEX_THRESHOLD_ENV.
        os.environ[INDEX_THRESHOLD_ENV] = str(args.index_threshold)
        notes.append(
            f"{INDEX_THRESHOLD_ENV} forced to {args.index_threshold} for this "
            f"run, so the prefilter regime was reached without padding. "
            f"Production's threshold is {INDEX_THRESHOLD}."
        )

    if "semantic" in arms:
        arms = [a for a in arms if a != "semantic"]
        notes.append(
            "semantic arm REMOVED — the product ships no embedding "
            "models (stripped in 4.0.0, door C reentry 5.5.0 revoked "
            "by owner doctrine in 6.0.0); the dated R1/R2 artifacts "
            "remain the record."
        )

    if RESCUE_EXPANSION:
        notes.append(
            "rescue-expansion repairs ON (the 5.1 lane: filler df-floor + "
            "coverage-gated vocabulary leg). The product DEFAULT is off — "
            "the lane's preregistered held-out check killed default-on; "
            "see the README's 5.1 section and bench/longmemeval/."
        )

    root = Path(tempfile.mkdtemp(prefix="bm-retrieval-"))
    try:
        slug_to_id, corpus_n = build_store(root, corpus_path, pad_to=args.pad_to)
        store = Store(root)
        # Both arms start from this one store: the off arm gets the whole
        # thing up front, the on arm lets production choose per query.
        memories = store.load_all()
        questions = _read_jsonl(questions_path)
        # `run_arm` skips a question whose slug has no gold document, so a
        # mismatched --corpus/--questions pair would score only the
        # intersection and report it under the full file's name — 20
        # questions answered, "120-question instrument" on the artifact.
        # Cheap to make impossible, so it is.
        orphans = [q["slug"] for q in questions if q["slug"] not in slug_to_id]
        if orphans:
            print(
                f"{len(orphans)} of {len(questions)} questions in "
                f"{questions_path.name} have no gold document in "
                f"{corpus_path.name}: {orphans[:5]}"
                f"{' ...' if len(orphans) > 5 else ''}\n"
                f"Refusing to run — the result would be scored over the "
                f"intersection and labelled with the full question count.",
                file=sys.stderr,
            )
            return 1

        rows: list[ArmResult] = []
        for arm in arms:
            for probe in ("asked", "requery", "control"):
                for prefilter in modes:
                    rows.append(
                        run_arm_prefiltered(
                            store,
                            questions,
                            slug_to_id,
                            arm=arm,
                            probe=probe,
                        )
                        if prefilter
                        else run_arm(
                            memories,
                            questions,
                            slug_to_id,
                            arm=arm,
                            probe=probe,
                        )
                    )
        # Inside the `try`, because the diagnostic reads the index that
        # the `finally` is about to delete.
        failure = engagement_failure(root, rows)
        if failure is not None:
            print(failure, file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)

    deltas = paired_deltas(rows)

    if corpus_n >= INDEX_THRESHOLD and args.prefilter == "off":
        # Gated: with a prefiltered arm in the report this note is simply
        # false, and a stale caveat that contradicts the artifact beside
        # it is worse than no caveat.
        notes.append(
            "corpus is above the index threshold, but this runner ranks the "
            "full corpus — production would prefilter through SQLite bm25 "
            "first. Treat this as an upper bound."
        )
    if True in modes:
        notes.append(
            "prefilter=on drives the production handler path "
            "(handlers.search.resolve_search_pool): the FTS5 candidate slice "
            f"capped at {PREFILTER_CAP}, ranked with the corpus-IDF provider. "
            "Every question was checked for engagement; the run exits "
            "non-zero if any pool came back un-prefiltered."
        )
        notes.append(
            "scope, repo, worktree and excluded-scope filters are all unset, "
            "so the cap-starvation guard cannot reload the full corpus and "
            "silently turn the on-arm into the off-arm."
        )
    if deltas:
        baseline = None
        if corpus_fingerprint(corpus_path) == _V2_CORPUS_SHA256:
            baseline = {
                None: "v2-unpadded-2026-07-26.json",
                600: "v2-padded600-2026-07-26.json",
            }.get(args.pad_to)
        # Gated on the LANE as well as the corpus, for the reason the
        # index-threshold note above is gated: the reference artifacts
        # were measured before 5.1 and are lane-off by construction, so
        # under `--rescue-expansion on` the off half ranks with repairs
        # the reference never had and reproduces nothing. The three
        # committed `prefilter-*-2026-08-09.json` files carry this note
        # in error — their own rows falsify it (asked 0.45/0.85 against
        # v2-padded600's 0.25/0.60). They are receipts and are left as
        # measured; the erratum is in README.md and the claim is gated
        # here so it cannot be emitted again.
        if baseline is not None and not RESCUE_EXPANSION:
            notes.append(
                f"the prefilter=off half re-measures {baseline} on the same "
                f"corpus digest, so its rows double as a regression check on "
                f"the harness itself."
            )
        elif baseline is not None:
            notes.append(
                f"the prefilter=off half ranks with the rescue-expansion "
                f"repairs, which {baseline} predates — it is a fresh "
                f"lane-on measurement, NOT a reproduction of that artifact."
            )
        else:
            notes.append(
                "corpus digest or padding matches no committed artifact, so "
                "the prefilter=off half is a fresh measurement rather than a "
                "reproduction of one."
            )

    if args.json:
        print(
            json.dumps(
                {
                    "provenance": _provenance(),
                    "corpus": corpus_path.name,
                    "corpus_sha256": corpus_fingerprint(corpus_path),
                    "corpus_size": corpus_n,
                    "questions": questions_path.name,
                    "questions_sha256": corpus_fingerprint(questions_path),
                    "questions_n": len(questions),
                    "index_threshold": INDEX_THRESHOLD,
                    "index_threshold_forced": args.index_threshold,
                    "above_threshold": corpus_n >= INDEX_THRESHOLD,
                    "padded": bool(args.pad_to),
                    "prefilter_mode": args.prefilter,
                    "prefilter_cap": PREFILTER_CAP,
                    "rescue_expansion": RESCUE_EXPANSION,
                    "conversational": CONVERSATIONAL,
                    "leg_margin_cap": LEG_MARGIN_CAP,
                    "evidence_scaling": args.evidence_scaling == "on",
                    "base_withhold": args.base_withhold == "on",
                    "notes": notes,
                    "results": [
                        {
                            "arm": r.arm,
                            "probe": r.probe,
                            "prefilter": r.prefilter,
                            "n": r.n,
                            "recall_at_1": round(r.recall(1), 4),
                            "recall_at_5": round(r.recall(5), 4),
                            # Additive. Every recall figure this
                            # instrument publishes rests on a finite
                            # question set — twenty before I1, 120
                            # after — and the point estimates above are
                            # unchanged; these say how much of the
                            # number is the instrument.
                            "recall_at_1_ci95": [
                                round(v, 4) for v in wilson(r.hits_at.get(1, 0), r.n)
                            ],
                            "recall_at_5_ci95": [
                                round(v, 4) for v in wilson(r.hits_at.get(5, 0), r.n)
                            ],
                            "per_question": r.per_question,
                            "engaged": r.engaged,
                            "gold_nominated": round(r.nomination_rate(), 4),
                            "mean_pool_size": round(r.mean_pool(), 2),
                        }
                        for r in rows
                    ],
                    "prefilter_delta": [
                        {
                            "arm": d.arm,
                            "probe": d.probe,
                            "recall_loss_at_1": round(d.recall_loss_at[1], 4),
                            "recall_loss_at_5": round(d.recall_loss_at[5], 4),
                            "gold_nomination_rate": round(d.gold_nomination_rate, 4),
                        }
                        for d in deltas
                    ],
                },
                indent=2,
            )
        )
    else:
        print(_format_text(rows, corpus_n, notes, corpus_path.name, deltas))
    if args.compare:
        print(_format_comparison(rows, Path(args.compare)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
