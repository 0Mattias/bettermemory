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
probe = _load("bench_lme_coverage_probe", _HERE / "coverage_probe.py")


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
# Per-question records
#
# The partial/complete split that motivated read-side rescue was measured
# by a throwaway re-run, because this runner persisted `by_type`
# aggregates only. Per-question records exist so that analysis is a
# property of the committed artifact. Their whole value is that the
# published aggregates are DERIVABLE from them — a record set that
# disagrees with the summary it ships beside is worse than no record set,
# so the agreement is pinned rather than assumed.
# ---------------------------------------------------------------------------


def test_question_record_ranks_align_with_deduped_evidence() -> None:
    inst = {
        "question_id": "q1",
        "question_type": "multi-session",
        "answer_session_ids": ["sB", "sA", "sB"],
    }
    rec = bm.question_record(inst, ["sC", "sA", "sB"])
    # Deduped in first-occurrence order, and each rank is that session's
    # position in the distinct-session ranking.
    assert rec["n_evidence"] == 2
    assert rec["evidence_ranks"] == [2, 1]
    assert rec["n_ranked"] == 3
    assert rec["qid"] == "q1"
    assert rec["type"] == "multi-session"


def test_question_record_marks_unretrieved_evidence_as_null() -> None:
    """`null` has to mean 'never surfaced within the depth', not rank 0 —
    the two are opposite outcomes and JSON has no other way to say it."""
    inst = {"question_id": "q2", "answer_session_ids": ["sA", "sGONE"]}
    rec = bm.question_record(inst, ["sA"])
    assert rec["evidence_ranks"] == [0, None]
    assert rec["type"] == "unknown"


def _synthetic_instance(
    qid: str,
    qtype: str,
    question: str,
    sessions: list[tuple[str, list[str]]],
    evidence: list[str],
) -> dict:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": question,
        "answer_session_ids": evidence,
        "haystack_session_ids": [sid for sid, _ in sessions],
        "haystack_sessions": [
            [{"role": "user", "content": t} for t in turns] for _, turns in sessions
        ],
        "haystack_dates": ["" for _ in sessions],
    }


def test_per_question_records_reproduce_the_aggregate_recall() -> None:
    """Derive every published aggregate from the records and compare to
    the aggregate the runner computed independently."""
    corpus = [
        _synthetic_instance(
            "qa",
            "single-session-user",
            "kangaroo",
            [("sA", ["I saw a kangaroo"]), ("sB", ["unrelated pottery"])],
            ["sA"],
        ),
        _synthetic_instance(
            "qb",
            "multi-session",
            "kangaroo pottery",
            [
                ("sC", ["a kangaroo hopped past"]),
                ("sD", ["the pottery kiln"]),
                ("sE", ["nothing whatsoever"]),
            ],
            ["sC", "sD"],
        ),
    ]
    res = bm.run_arm(corpus, arm="lexical", semantic_model=None, progress=False)

    assert len(res.per_question) == res.n == 2
    assert [r["qid"] for r in res.per_question] == ["qa", "qb"]

    for k in bm.K_VALUES:
        derived = sum(
            len([r for r in rec["evidence_ranks"] if r is not None and r < k])
            / rec["n_evidence"]
            for rec in res.per_question
        ) / len(res.per_question)
        assert derived == pytest.approx(res.recall_macro(k)), k

        derived_trunc = len([rec for rec in res.per_question if rec["n_ranked"] < k])
        assert derived_trunc == res.truncated[k], k

    for qtype in res.type_n:
        recs = [rec for rec in res.per_question if rec["type"] == qtype]
        derived = sum(
            len([r for r in rec["evidence_ranks"] if r is not None and r < 5])
            / rec["n_evidence"]
            for rec in recs
        ) / len(recs)
        assert derived == pytest.approx(res.type_recall(qtype, 5)), qtype


def test_per_question_sidecar_carries_the_disqualifying_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sidecar read apart from its summary must still be able to say
    'this run is not publishable'."""
    corpus_path = tmp_path / "tiny.json"
    corpus_path.write_text(
        json.dumps(
            [
                _synthetic_instance(
                    "qa",
                    "single-session-user",
                    "kangaroo",
                    [("sA", ["I saw a kangaroo"])],
                    ["sA"],
                )
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "pq.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--corpus",
            str(corpus_path),
            "--arms",
            "lexical",
            "--json",
            "--quiet",
            "--per-question",
            str(out),
        ],
    )
    assert bm.main() == 0
    capsys.readouterr()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert any("UNPINNED CORPUS" in n for n in payload["notes"])
    assert payload["retrieval_depth"] == bm.RETRIEVAL_DEPTH
    assert [r["qid"] for r in payload["arms"]["lexical"]] == ["qa"]


# ---------------------------------------------------------------------------
# The coverage probe
#
# It exists to answer one question — does the evidence a search DROPS
# carry query terms the survivors do not? — and its answer is now quoted
# in the README as the reason read-side diversification was closed. So
# the population it selects and the quantity it counts are pinned: a
# probe that quietly scored the wrong questions would retire a real
# feature on a false reading.
# ---------------------------------------------------------------------------


def test_coverage_probe_marks_only_partial_questions() -> None:
    """Complete hits have nothing to rescue toward and total misses have
    nothing to rescue FROM; neither belongs in the novelty statistics."""
    complete = probe.question_coverage(
        [("sA", ["x"]), ("sB", ["y"])], ["sA", "sB"], k=5
    )
    assert complete["partial"] is False and complete["recall_at_k"] == 1.0

    miss = probe.question_coverage([("sX", ["x"]), ("sY", ["y"])], ["sA"], k=1)
    assert miss["partial"] is False and miss["recall_at_k"] == 0.0

    partial = probe.question_coverage(
        [("sA", ["x"]), ("sX", ["x"]), ("sB", ["y"])], ["sA", "sB"], k=2
    )
    assert partial["partial"] is True
    assert partial["recall_at_k"] == 0.5
    assert len(partial["dropped"]) == 1


def test_coverage_probe_reports_both_reference_sets() -> None:
    """The headline is definition-dependent, so both definitions ship.

    `novel_broad` counts against every hit of every top-k session — the
    most generous reference, and the one that makes the strongest-sounding
    claim. `novel_strict` counts against one representative hit per top-k
    session, which is the fairer analogue of the list head a re-ranker
    actually holds.
    """
    ranked = [
        ("sA", ["alpha"]),
        ("sC", ["beta"]),
        ("sA", ["gamma"]),  # a SECOND hit of a top-k session, ranked below
        ("sB", ["alpha", "gamma"]),  # the dropped evidence
    ]
    got = probe.question_coverage(ranked, ["sA", "sB"], k=2)
    d = got["dropped"][0]
    # Broad folds in sA's second hit, so 'gamma' is already covered.
    assert d["novel_broad"] == 0
    # Strict uses only each top-k session's BEST hit, so 'gamma' is new.
    assert d["novel_strict"] == 1
    assert d["matched_terms"] == 2
    assert d["distinct_rank"] == 2


def test_coverage_probe_counts_dropped_sessions_it_cannot_rank() -> None:
    """A dropped session that scored in no leg appears in no ranking. It
    still happened, and quietly leaving it out of the denominator would
    flatter the zero-novelty fraction."""
    got = probe.question_coverage([("sA", ["x"]), ("sZ", ["x"])], ["sA", "sGHOST"], k=1)
    assert got["partial"] is True
    assert got["dropped"] == []
    assert got["unrankable"] == 1

    s = probe.summarise([got])
    assert s["dropped_sessions"] == 1
    assert s["dropped_sessions_ranked"] == 0
    assert s["dropped_sessions_unrankable"] == 1


def test_coverage_probe_counts_the_distractors_a_rescue_would_also_promote() -> None:
    """Novelty is only a usable signal if evidence carries it and
    non-evidence does not. The false-positive count is what decides that."""
    ranked = [
        ("sA", ["alpha"]),
        ("sX", ["novel1"]),  # distractor below k, carries a novel term
        ("sY", ["alpha"]),  # distractor below k, carries nothing new
        ("sB", ["novel2"]),  # the dropped evidence
    ]
    got = probe.question_coverage(ranked, ["sA", "sB"], k=1)
    assert got["distractors_below_k"] == 2
    assert got["distractors_below_k_novel"] == 1


def test_coverage_probe_oracle_bounds_a_perfect_rescue() -> None:
    """The oracle promotes exactly the novel-carrying dropped evidence,
    with zero false promotions. Nothing real can beat it, so it is the
    number that turns 'we found no design' into 'there is none'."""
    partial = probe.question_coverage(
        [("sA", ["alpha"]), ("sX", ["alpha"]), ("sB", ["novel"])],
        ["sA", "sB"],
        k=1,
    )
    complete = probe.question_coverage([("sC", ["z"])], ["sC"], k=1)
    s = probe.summarise([partial, complete])
    assert s["macro_recall_at_k"] == pytest.approx(0.75)  # (0.5 + 1.0) / 2
    assert s["oracle"]["promoted"] == 1
    assert s["oracle"]["macro_recall_at_k"] == pytest.approx(1.0)
    # One question goes 0.5 -> 1.0; pooled over two questions that is
    # +0.25 macro, i.e. 25 points.
    assert s["oracle"]["delta_points"] == pytest.approx(25.0)
    assert s["oracle"]["precision"] == pytest.approx(1.0)


def test_coverage_probe_prices_a_loose_novelty_reference_against_no_filter() -> None:
    """Loosening the novelty test raises the ceiling — the table has to
    show that it does so by converging on promoting everything, which
    needs no signal at all. Precision relative to `blind` is what
    separates a real filter from a permissive one.
    """
    rec = probe.question_coverage(
        [
            ("sA", ["alpha"]),
            ("sC", ["beta"]),  # a top-k session carrying 'beta'
            ("sX", ["beta"]),  # distractor: novel vs top1, not vs strict
            ("sB", ["beta"]),  # dropped evidence, same shape as sX
        ],
        ["sA", "sB"],
        k=2,
    )
    s = probe.summarise([rec])
    blind = s["oracle_by_reference"]["blind"]
    strict = s["oracle_by_reference"]["strict"]
    top1 = s["oracle_by_reference"]["top1"]

    # `blind` promotes the dropped session without asking anything.
    assert blind["promoted"] == 1 and blind["distractors_promoted"] == 1
    assert blind["precision_lift_over_blind"] == pytest.approx(1.0)
    # `strict` sees 'beta' already in the top-k head, so it promotes
    # nothing and its ceiling is zero.
    assert strict["promoted"] == 0 and strict["delta_points"] == pytest.approx(0.0)
    # `top1` only knows sA, so 'beta' reads novel — it recovers blind's
    # ceiling and blind's precision, i.e. it has stopped filtering.
    assert top1["promoted"] == 1
    assert top1["delta_points"] == pytest.approx(blind["delta_points"])
    assert top1["precision_lift_over_blind"] == pytest.approx(1.0)


def test_coverage_probe_summary_survives_an_empty_population() -> None:
    """A `--limit` smoke over single-session questions finds no partials;
    that must report cleanly, not divide by zero."""
    assert probe.summarise([])["dropped_sessions"] == 0
    only_complete = probe.question_coverage([("sA", ["x"])], ["sA"], k=1)
    assert probe.summarise([only_complete])["dropped_sessions"] == 0


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
