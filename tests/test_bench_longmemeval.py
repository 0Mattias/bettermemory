"""Invariant tests for the LongMemEval comparison harness.

This directory produces the project's only COMPARATIVE number, against a
competitor, on labels neither party authored. That raises the cost of a
quiet defect above what the other bench suites carry: a harness bug here
does not merely produce a wrong self-measurement, it publishes a false
claim about someone else's software. Two such bugs have already been
caught by hand — a 90-day recency window that scored claude-mem 0.0 on
every question, and a fixed sleep that queried a half-built vector index
and scored them 7.5% instead of 87.5%.

Both were invisible to the test suite because they lived in wiring, not
in data. What CAN be pinned mechanically is pinned here.

The 265 MB corpus is NOT committed, so every test below runs against
either pure functions or committed constants. Tests needing the corpus
skip cleanly when it is absent, which is the normal state in CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "longmemeval"
_CORPUS = _HERE / "data" / "longmemeval_s_cleaned.json"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bm = _load("bench_lme_run", _HERE / "run.py")
cm = _load("bench_lme_cm_run", _HERE / "cm_run.py")


# ---------------------------------------------------------------------------
# The two arms must see ONE corpus
# ---------------------------------------------------------------------------


def test_both_runners_pair_rounds_identically() -> None:
    """The single most dangerous divergence in this directory.

    bettermemory and claude-mem are scored by two separate runners. If
    they chunk the conversation differently, the comparison silently
    stops being a comparison — one system would be answering from
    different units than the other, and no output would look wrong.
    """
    cases = [
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        [{"role": "user", "content": "a"}],
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        [],
    ]
    for session in cases:
        assert bm.rounds_of(session) == cm.rounds_of(session), session


def test_a_trailing_unpaired_turn_is_kept_not_dropped() -> None:
    """Sessions that end on a user message exist in the corpus, and the
    labels still point at them. Dropping the tail would delete evidence."""
    session = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    rounds = bm.rounds_of(session)
    assert len(rounds) == 2
    assert "three" in rounds[-1]


def test_consecutive_same_role_turns_do_not_merge() -> None:
    session = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]
    assert len(bm.rounds_of(session)) == 2


# ---------------------------------------------------------------------------
# The attribution rule
# ---------------------------------------------------------------------------


def test_distinct_sessions_collapses_first_occurrence_wins() -> None:
    ranked = ["m1", "m2", "m3", "m4"]
    id_to_session = {"m1": "sA", "m2": "sB", "m3": "sA", "m4": "sC"}
    assert bm.distinct_sessions(ranked, id_to_session) == ["sA", "sB", "sC"]


def test_distinct_sessions_ignores_unknown_ids() -> None:
    assert bm.distinct_sessions(["x"], {}) == []


def test_retrieval_depth_exceeds_the_largest_k() -> None:
    """Depth is collapsed to distinct sessions AFTER ranking, so a depth
    at or below max(k) could never yield k distinct sessions."""
    assert bm.RETRIEVAL_DEPTH > max(bm.K_VALUES)
    assert cm.RETRIEVAL_DEPTH > max(cm.K_VALUES)


def test_both_runners_score_the_same_k_values() -> None:
    assert bm.K_VALUES == cm.K_VALUES


def test_both_runners_use_the_same_retrieval_depth() -> None:
    """Different depths would give one system more chances to surface a
    distinct session than the other."""
    assert bm.RETRIEVAL_DEPTH == cm.RETRIEVAL_DEPTH


# ---------------------------------------------------------------------------
# Guards that exist because a specific bug happened
# ---------------------------------------------------------------------------


def test_oracle_variant_is_pinned_and_flagged_unpublishable() -> None:
    """`longmemeval_oracle.json` contains evidence sessions and NO
    distractors, so any working retriever scores ~1.0 against it."""
    assert bm.ORACLE_SHA in bm.KNOWN_CORPORA
    source = (_HERE / "run.py").read_text()
    assert "MUST NOT be published" in source


def test_claude_mem_date_window_spans_the_corpus_era() -> None:
    """claude-mem applies a default 90-day recency window when no range
    is passed, which discards every match on this 2023-dated corpus and
    scores them 0.0 for reasons unrelated to retrieval."""
    assert cm.DATE_START <= "2023-05-01"
    assert cm.DATE_END >= "2023-06-01"


def test_claude_mem_runner_uses_the_spelling_that_is_not_ignored() -> None:
    """`startDate`/`endDate` are accepted and SILENTLY IGNORED. A harness
    using that spelling produces the false zero above while appearing to
    have handled it."""
    source = (_HERE / "cm_run.py").read_text()
    assert "dateStart" in source and "dateEnd" in source
    assert '"startDate"' not in source


def test_claude_mem_runner_waits_for_the_vector_index() -> None:
    """A fixed sleep scored claude-mem 7.5% instead of 87.5% by querying
    a half-built index. Readiness must be measured."""
    assert hasattr(cm.Worker, "await_chroma_backfill")


def test_session_id_never_enters_retrievable_content() -> None:
    """The label must not be retrievable as text, or it leaks into the
    thing being measured."""
    source = (_HERE / "run.py").read_text()
    assert "id_to_session[memory.id] = sid" in source
    # The body written to the store is the round text plus a date line.
    assert 'text = f"[{date}]\\n{body}" if date else body' in source


# ---------------------------------------------------------------------------
# Corpus-dependent (skipped without the 265 MB download)
# ---------------------------------------------------------------------------

_needs_corpus = pytest.mark.skipif(
    not _CORPUS.exists(), reason="LongMemEval corpus not downloaded"
)


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))


@_needs_corpus
def test_corpus_matches_the_pinned_checksum() -> None:
    sha = bm.corpus_fingerprint(_CORPUS)
    assert sha in bm.KNOWN_CORPORA, (
        "corpus is not a revision this runner has scored; results would "
        "not be comparable to published rows"
    )


@_needs_corpus
def test_corpus_is_the_full_500_instances(corpus: list[dict]) -> None:
    assert len(corpus) == 500


@_needs_corpus
def test_every_evidence_id_is_present_in_its_own_haystack(
    corpus: list[dict],
) -> None:
    for inst in corpus:
        assert set(inst["answer_session_ids"]).issubset(
            set(inst["haystack_session_ids"])
        ), inst["question_id"]


@_needs_corpus
def test_no_question_has_zero_evidence_sessions(corpus: list[dict]) -> None:
    """Abstention items would have none — the distributed corpus has no
    abstention questions, and the runner's guard depends on that."""
    assert all(inst["answer_session_ids"] for inst in corpus)


@_needs_corpus
def test_haystack_arrays_are_index_aligned(corpus: list[dict]) -> None:
    for inst in corpus:
        n = len(inst["haystack_sessions"])
        assert len(inst["haystack_session_ids"]) == n
        assert len(inst["haystack_dates"]) == n
