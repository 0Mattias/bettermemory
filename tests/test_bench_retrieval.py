"""Integrity tests for the committed retrieval gold set.

The corpus and questions are data, not code, so nothing else in the suite
would notice them rotting. The failure mode is quiet and expensive: a
duplicated slug, a gold document that lost its question, or a corpus that
drifted below the class mix it claims to mirror would all still *run* and
still produce a number — just a number that means something other than
what the README says it means.

So the invariants the published figures depend on are pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "retrieval"
_RUNNER = _HERE / "run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_retrieval_run", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_retrieval_run"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def _rows(name: str) -> list[dict]:
    path = _HERE / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


CORPUS = _rows("corpus.jsonl")
QUESTIONS = _rows("questions.jsonl")


# ---------------------------------------------------------------------------
# Corpus integrity
# ---------------------------------------------------------------------------


def test_every_gold_document_has_exactly_one_question() -> None:
    gold = {r["slug"] for r in CORPUS if r["gold"]}
    asked = [q["slug"] for q in QUESTIONS]
    assert gold == set(asked), (
        f"gold-without-question: {sorted(gold - set(asked))}; "
        f"question-without-gold: {sorted(set(asked) - gold)}"
    )
    assert len(asked) == len(set(asked)), "duplicate question slugs"


def test_slugs_are_unique_across_the_whole_corpus() -> None:
    """A duplicate slug silently overwrites a gold id in the slug->id map,
    which would make one probe unanswerable and quietly lower recall."""
    slugs = [r["slug"] for r in CORPUS]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    assert not dupes, f"duplicate slugs: {sorted(dupes)}"


def test_no_distractor_is_secretly_a_gold_topic() -> None:
    gold = {r["slug"] for r in CORPUS if r["gold"]}
    distractors = {r["slug"] for r in CORPUS if not r["gold"]}
    assert not (gold & distractors)


def test_corpus_is_large_enough_for_retrieval_to_be_nontrivial() -> None:
    """20 gold documents in a field of 20 would make recall meaningless."""
    assert len([r for r in CORPUS if r["gold"]]) == 20
    assert len(CORPUS) >= 150


def test_class_mix_matches_what_a_real_store_measured() -> None:
    """The README claims the corpus mirrors the ~64/36 checkable-literal
    split that bench/claims.py measured on a real store. If the corpus
    drifts literal-dense it silently flatters lexical retrieval, so the
    claim is pinned rather than trusted."""
    share = sum(1 for r in CORPUS if r["has_checkable_literal"]) / len(CORPUS)
    assert 0.55 <= share <= 0.72, f"checkable-literal share drifted to {share:.1%}"


def test_documents_carry_enough_text_to_rank() -> None:
    short = [r["slug"] for r in CORPUS if len(r["body"]) < 300]
    assert not short, f"documents too short to be realistic: {short[:5]}"


def test_every_document_has_at_least_one_scope() -> None:
    assert all(r["scopes"] for r in CORPUS)
    assert not [r["slug"] for r in CORPUS if "general" in r["scopes"]]


# ---------------------------------------------------------------------------
# Blindness — the property the whole set rests on
# ---------------------------------------------------------------------------


def test_questions_do_not_simply_restate_their_slug() -> None:
    """The slug is the only vocabulary shared between the two authors. If a
    question just re-spells its slug, the blind construction is decorative
    and the `asked` probe stops modelling how anyone actually types."""
    offenders = []
    for q in QUESTIONS:
        slug_words = {w for w in q["slug"].split("-") if len(w) > 3}
        asked = set(re.findall(r"[a-z]+", q["question"].lower()))
        if slug_words and len(slug_words & asked) / len(slug_words) > 0.8:
            offenders.append(q["slug"])
    assert not offenders, f"questions restating their slug: {offenders}"


def test_asked_and_requery_are_actually_different_probes() -> None:
    """The measurement IS the gap between them. If an author split the
    difference, the arm stops testing anything."""
    for q in QUESTIONS:
        assert q["question"].strip() != q["requery"].strip()
        assert "?" not in q["requery"], (
            f"requery for {q['slug']} is still a question: {q['requery']!r}"
        )


# ---------------------------------------------------------------------------
# Runner behaviour
# ---------------------------------------------------------------------------


def test_index_threshold_is_cross_pinned_to_production() -> None:
    """run.py duplicates `_INDEX_THRESHOLD_DEFAULT` so it can LABEL which
    regime a run was in. A silent drift would mislabel the result without
    failing anything, so the copy is pinned to the original here."""
    from bettermemory import _handlers

    assert runner.INDEX_THRESHOLD == _handlers._INDEX_THRESHOLD_DEFAULT


def test_control_probe_strips_interrogatives_but_keeps_content() -> None:
    stripped = runner.strip_question_words(
        "why did we move the pooling out of the app again?"
    )
    lowered = stripped.lower()
    assert "pooling" in lowered and "app" in lowered
    for dropped in ("why", "did", "we", "again"):
        assert dropped not in lowered.split()


def test_control_never_empties_a_realistic_question() -> None:
    """A control arm that reduced a query to nothing would score 0% and be
    read as a finding about vocabulary rather than a bug in the stripper."""
    for q in QUESTIONS:
        assert runner.strip_question_words(q["question"]).strip()


@pytest.mark.parametrize("k", [1, 5])
def test_k_values_are_within_the_search_cap(k: int) -> None:
    from bettermemory.handlers.search import MAX_SEARCH_RESULTS

    assert k in runner.K_VALUES
    assert k <= MAX_SEARCH_RESULTS


def test_filler_shares_no_vocabulary_with_gold_documents() -> None:
    """Padding exists to move the corpus across the index threshold. If
    filler could win a gold probe, the threshold experiment would be
    measuring the filler generator instead of the ranker."""
    gold_text = " ".join(r["body"] for r in CORPUS if r["gold"]).lower()
    gold_terms = set(re.findall(r"[a-z]{5,}", gold_text))
    for seed in range(25):
        filler = set(re.findall(r"[a-z]{5,}", runner._filler_body(seed).lower()))
        overlap = filler & gold_terms
        assert len(overlap) <= 6, f"filler seed {seed} overlaps gold: {sorted(overlap)}"
