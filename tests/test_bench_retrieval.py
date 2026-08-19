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

import ast
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "retrieval"
_RUNNER = _HERE / "run.py"
_RESULTS = _HERE / "results"


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
    """120 gold documents in a field of 120 would make recall meaningless."""
    assert len([r for r in CORPUS if r["gold"]]) == 120
    assert len(CORPUS) >= 1000


def test_every_gold_topic_has_near_duplicate_competition() -> None:
    """The v1 corpus scored far too high because each gold document was the
    only one in its subsystem, so rare terms survived IDF weighting in a
    way they never would in a real store. v2 exists to supply that
    competition; a gold topic that lost its near-duplicates would silently
    become easy again and lift the published recall."""
    per_gold: dict[str, int] = {}
    for row in CORPUS:
        near = row.get("near_slug")
        if near:
            per_gold[near] = per_gold.get(near, 0) + 1
    gold = {r["slug"] for r in CORPUS if r["gold"]}
    thin = sorted(g for g in gold if per_gold.get(g, 0) < 5)
    assert not thin, f"gold topics with fewer than 5 near-duplicates: {thin}"


def test_near_duplicates_never_point_at_a_missing_gold_topic() -> None:
    gold = {r["slug"] for r in CORPUS if r["gold"]}
    dangling = sorted({r["near_slug"] for r in CORPUS if r.get("near_slug")} - gold)
    assert not dangling, f"near_slug values with no gold document: {dangling}"


def test_v1_corpus_is_retained_so_published_figures_stay_reproducible() -> None:
    """The v1 numbers are the record of a pre-registered prediction being
    scored. Deleting the corpus they ran against would turn that record
    into a story."""
    v1 = _HERE / "corpus-v1.jsonl"
    assert v1.exists(), "corpus-v1.jsonl was removed; v1 results are now unverifiable"
    rows = [json.loads(line) for line in v1.read_text().splitlines() if line.strip()]
    assert len([r for r in rows if r["gold"]]) == 20


def test_v2_instrument_is_retained_so_pre_i1_figures_stay_reproducible() -> None:
    """Every dev-side figure the campaign published before I1 was measured
    on the 180-document / 20-question instrument. I1 expanded it to
    1,080 / 120; the old pair is retained beside the new one so those
    figures stay checkable, and so I1-G1's integrity anchor has
    something to run against. run.py pins this corpus digest as
    `_V2_CORPUS_SHA256` to decide whether a run reproduces a committed
    artifact, so a byte of drift here silently breaks that claim too."""
    corpus = _HERE / "corpus-v2.jsonl"
    questions = _HERE / "questions-v2.jsonl"
    assert corpus.exists(), (
        "corpus-v2.jsonl was removed; pre-I1 results are now unverifiable"
    )
    assert questions.exists(), (
        "questions-v2.jsonl was removed; pre-I1 results are now unverifiable"
    )
    rows = [
        json.loads(line) for line in corpus.read_text().splitlines() if line.strip()
    ]
    assert len(rows) == 180
    assert len([r for r in rows if r["gold"]]) == 20
    qs = [
        json.loads(line) for line in questions.read_text().splitlines() if line.strip()
    ]
    assert len(qs) == 20
    digest = hashlib.sha256(corpus.read_bytes()).hexdigest()
    assert digest == runner._V2_CORPUS_SHA256, (
        f"corpus-v2.jsonl digest {digest} no longer matches run.py's "
        f"_V2_CORPUS_SHA256 — the reproduction claim it gates is now false"
    )


def test_the_original_twenty_are_carried_verbatim_into_the_expanded_corpus() -> None:
    """I1 §3.1 freezes the original topics 'slug for slug, byte for byte'.
    That is what keeps every published figure on this instrument
    checkable, and what makes the original-twenty subset cell a like-for-
    like comparison rather than a re-authored approximation."""
    v2_corpus = (_HERE / "corpus-v2.jsonl").read_text()
    v2_questions = (_HERE / "questions-v2.jsonl").read_text()
    assert (_HERE / "corpus.jsonl").read_text().startswith(v2_corpus)
    assert (_HERE / "questions.jsonl").read_text().startswith(v2_questions)


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


# ---------------------------------------------------------------------------
# The prefiltered arm
#
# Every one of the loader's fallbacks returns the full corpus quietly, so a
# prefilter arm that fell back scores like an ordinary full-corpus run and
# prints under a `prefilter: true` heading. Nothing about the numbers looks
# wrong. These tests pin the machinery that tells the difference.
# ---------------------------------------------------------------------------


def _fake_pool(memories: list[Any], *, prefiltered: bool) -> Any:
    """A `SearchPool` shaped like the two outcomes that matter.

    `corpus_stats_provider is not None` is `resolve_search_pool`'s exact
    IFF for "the FTS path served this pool", so a stand-in only has to get
    that one field right.
    """
    from bettermemory.handlers.search import SearchPool

    return SearchPool(
        memories=memories,
        corpus_stats_provider=(lambda terms: None) if prefiltered else None,
    )


def _two_questions() -> tuple[list[dict], dict[str, str]]:
    picked = QUESTIONS[:2]
    return picked, {q["slug"]: f"id-{i}" for i, q in enumerate(picked)}


def test_index_threshold_env_name_is_the_one_production_reads() -> None:
    """`INDEX_THRESHOLD_ENV` is a second copy of a string that only means
    anything if it matches. Pinned by behaviour rather than by equality so
    a rename on either side has to move both."""
    import os

    from bettermemory import _handlers

    original = os.environ.get(runner.INDEX_THRESHOLD_ENV)
    os.environ[runner.INDEX_THRESHOLD_ENV] = "7"
    try:
        assert _handlers.resolve_index_threshold() == 7
    finally:
        if original is None:
            del os.environ[runner.INDEX_THRESHOLD_ENV]
        else:
            os.environ[runner.INDEX_THRESHOLD_ENV] = original


def test_the_runner_never_touches_the_environment_at_import_time() -> None:
    """This module exec's run.py at pytest COLLECTION time, so anything
    run.py does at module scope happens once inside the pytest process and
    stays done. A module-level `os.environ[...] = ...` would put every
    later test's store into the prefilter regime, which is a whole-suite
    behaviour change with no failing test anywhere near the cause."""
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    module_scope = [
        node
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    offenders = []
    for node in module_scope:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr in {"environ", "putenv"}:
                offenders.append(ast.unparse(sub))
    assert not offenders, f"run.py touches the environment at import: {offenders}"


def test_the_prefiltered_arm_never_passes_a_post_cap_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve_search_pool` reloads the whole corpus and clears the
    prefiltered flag when a post-cap filter starves a saturated slice. That
    guard is gated on `repo_filter` / `worktree_filter` / `excluded_scopes`,
    so passing any of them here would let the arm quietly measure
    `load_all` on exactly the queries where the cap binds hardest."""
    from bettermemory.store import Store

    seen: list[dict[str, Any]] = []

    def recorder(store: Any, query: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return _fake_pool([], prefiltered=True)

    monkeypatch.setattr(runner, "resolve_search_pool", recorder)
    questions, slug_to_id = _two_questions()
    runner.run_arm_prefiltered(
        Store(tmp_path),
        questions,
        slug_to_id,
        arm="lexical",
        probe="asked",
    )
    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs["scopes"] is None
        assert kwargs["excluded_scopes"] is None
        assert kwargs["repo_filter"] is None
        assert kwargs["worktree_filter"] is None


def test_the_prefiltered_arm_records_every_query_whose_pool_fell_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory.store import Store

    monkeypatch.setattr(
        runner,
        "resolve_search_pool",
        lambda store, query, **kw: _fake_pool([], prefiltered=False),
    )
    questions, slug_to_id = _two_questions()
    result = runner.run_arm_prefiltered(
        Store(tmp_path),
        questions,
        slug_to_id,
        arm="lexical",
        probe="asked",
    )
    assert result.engaged == 0
    assert result.unengaged == [q["question"] for q in questions]


def test_engagement_failure_stays_quiet_when_every_pool_engaged(
    tmp_path: Path,
) -> None:
    row = runner.ArmResult(arm="lexical", probe="asked", n=20, prefilter=True)
    row.engaged = 20
    assert runner.engagement_failure(tmp_path, [row]) is None


def test_engagement_failure_names_the_regime_and_the_query(tmp_path: Path) -> None:
    """A run that fell back has to say WHICH way it fell back, or the next
    person re-runs it blind. The index census separates "corpus below the
    threshold" from "the FTS match set was empty"."""
    row = runner.ArmResult(arm="lexical", probe="control", n=20, prefilter=True)
    row.engaged = 19
    row.unengaged = ["pooling app"]
    report = runner.engagement_failure(tmp_path, [row])
    assert report is not None
    assert "indexed_count" in report
    assert "lexical/control: 1/20" in report
    assert "'pooling app'" in report


def test_an_arm_that_asked_nothing_fails_instead_of_passing_vacuously(
    tmp_path: Path,
) -> None:
    """Zero questions is the one way to hold `unengaged` empty without ever
    engaging. `recall()` is 0.0 over zero questions, so the paired delta
    comes out 0.0 — byte-identical to the report a prefilter that cost
    nothing produces. A `--corpus` whose slugs miss `questions.jsonl` is all
    it takes, so the guard has to judge the absence of evidence too."""
    row = runner.ArmResult(arm="lexical", probe="asked", n=0, prefilter=True)
    assert row.unengaged == []
    report = runner.engagement_failure(tmp_path, [row])
    assert report is not None
    assert "no question matched the corpus" in report


def test_a_full_corpus_arm_is_never_mistaken_for_an_engaged_one(
    tmp_path: Path,
) -> None:
    """`run_arm` leaves `engaged` at zero, and the guard only judges rows
    that claim to be prefiltered — otherwise every default run would fail
    the integrity check it is not making a claim about."""
    row = runner.ArmResult(arm="lexical", probe="asked", n=20, prefilter=False)
    assert row.engaged == 0
    assert runner.engagement_failure(tmp_path, [row]) is None


def test_main_refuses_to_emit_when_the_prefilter_did_not_engage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole integrity of the artifact rests on this exit code. A run
    that fell back must be impossible to mistake for a result, so it fails
    loudly instead of printing numbers nobody can tell apart."""
    monkeypatch.setattr(
        runner,
        "resolve_search_pool",
        lambda store, query, **kw: _fake_pool(
            list(store.load_all()), prefiltered=False
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--arms", "lexical", "--prefilter", "on", "--json"]
    )
    assert runner.main() == 1
    captured = capsys.readouterr()
    assert "PREFILTER NEVER ENGAGED" in captured.err
    assert not captured.out


def test_a_zero_index_threshold_is_rejected_rather_than_silently_defaulted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`resolve_index_threshold` treats `<= 0` as unset and returns 500, so
    accepting it would run the below-threshold regime under an
    above-threshold label — the one failure this whole arm exists to make
    impossible."""
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--index-threshold", "0", "--prefilter", "on"]
    )
    assert runner.main() == 1
    assert "must be > 0" in capsys.readouterr().err


def test_the_default_report_shape_is_unchanged_by_the_new_columns() -> None:
    """The four committed artifacts and every earlier text run have to stay
    reproducible, so the pool columns appear only once a prefiltered arm is
    in the report."""
    off = runner.ArmResult(arm="lexical", probe="asked", n=20, prefilter=False)
    text = runner._format_text([off], 180, [], "corpus.jsonl")
    assert "| arm      | probe   | recall@1 | recall@5 | n  |" in text
    assert "prefilter" not in text

    on = runner.ArmResult(arm="lexical", probe="asked", n=20, prefilter=True)
    assert "prefilter" in runner._format_text([off, on], 600, [], "corpus.jsonl")


def test_paired_deltas_only_subtract_cells_that_ran_side_by_side() -> None:
    """The oracle for a prefiltered arm is the SAME queries on the SAME
    store in the SAME process. Pairing against a committed artifact from a
    different corpus size would confound the prefilter with dilution, which
    is the error the padded runs already made once."""
    off = runner.ArmResult(
        arm="lexical", probe="asked", n=20, hits_at={1: 5, 5: 12}, prefilter=False
    )
    on = runner.ArmResult(
        arm="lexical", probe="asked", n=20, hits_at={1: 6, 5: 12}, prefilter=True
    )
    on.nominated = 19
    unpaired = runner.ArmResult(arm="semantic", probe="asked", n=20, prefilter=True)

    deltas = runner.paired_deltas([off, on, unpaired])
    assert [(d.arm, d.probe) for d in deltas] == [("lexical", "asked")]
    # Negative: the prefilter ranked BETTER than the full corpus here.
    assert deltas[0].recall_loss_at[1] == pytest.approx(-0.05)
    assert deltas[0].recall_loss_at[5] == pytest.approx(0.0)
    assert deltas[0].gold_nomination_rate == pytest.approx(0.95)


def test_the_prefilter_really_engages_on_every_committed_question(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one test that drives the real loader end to end.

    Everything above pins the machinery with stand-ins; this pins that the
    machinery has something to report — a live 600-memory store, the
    production pool resolver, and 60 queries that all have to come back
    prefiltered. It also re-derives the committed artifact's prefiltered
    rows, so those numbers stay a measurement rather than a memory.
    """
    # `main()` writes the module-level RESCUE_EXPANSION via `global` when
    # the flag below is parsed; registering the attr with monkeypatch
    # first means teardown restores the module default (off) even after
    # that write, so no later test in this session measures rescue-on
    # by leakage — the same containment INDEX_THRESHOLD_ENV gets.
    monkeypatch.setattr(runner, "RESCUE_EXPANSION", False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            # I1 expanded corpus.jsonl in place (180 -> 1,080 documents,
            # 20 -> 120 questions) and retained the original pair as
            # corpus-v2.jsonl / questions-v2.jsonl. This test reproduces a
            # golden measured on that original pair and asserts n == 20
            # below, so it names the corpus it means rather than
            # inheriting whichever one currently sits at corpus.jsonl.
            "--corpus",
            "corpus-v2.jsonl",
            "--questions",
            "questions-v2.jsonl",
            "--pad-to",
            "600",
            "--prefilter",
            "on",
            "--arms",
            "lexical",
            "--rescue-expansion",
            "on",
            "--json",
        ],
    )
    assert runner.main() == 0
    live = json.loads(capsys.readouterr().out)

    # The golden tracks the CURRENT in-lane engine, not a dated receipt.
    # Each round's leg-conditioning rule changes what the lane ranks, so
    # this pointer moves with the code while every superseded artifact
    # stays committed as the record of the engine that produced it. It
    # therefore keeps meaning "the harness reproduces what it published"
    # rather than "the engine never changes".
    committed = json.loads(
        (_RESULTS / "shipped-prefilter-above-threshold-2026-08-11.json").read_text()
    )
    published = {
        (r["arm"], r["probe"]): r for r in committed["results"] if r["prefilter"]
    }
    assert live["results"], "no rows produced"
    for row in live["results"]:
        assert row["prefilter"] is True
        assert row["engaged"] == row["n"] == 20, row
        assert row["mean_pool_size"] <= committed["prefilter_cap"], row
        # A pool cannot return what it never contained.
        assert row["recall_at_5"] <= row["gold_nominated"], row
        # Compared key by key over what the artifact PUBLISHED, not by
        # whole-dict equality. The runner now emits interval fields and
        # a per-question record beside the same measurements, and an
        # additive field is not a divergence — freezing the schema here
        # would mean no measurement could ever gain a reading without
        # looking like the engine had changed. Every published key must
        # still be present and equal, so a dropped or altered field
        # fails exactly as before.
        prior = published[(row["arm"], row["probe"])]
        missing = sorted(set(prior) - set(row))
        assert not missing, f"published keys dropped from the live row: {missing}"
        assert {k: row[k] for k in prior} == prior, (
            "live run diverged from the committed artifact — re-run "
            "bench/retrieval/run.py and update results/ if ranking changed"
        )


# ---------------------------------------------------------------------------
# The published prefilter artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "prefilter-above-threshold-2026-07-30.json",
        "prefilter-forced-180-2026-07-30.json",
        # The 5.1 lane pair. Left uncovered when they landed, which is
        # backwards: `test_prefilter_live_run_matches_the_committed_artifact`
        # uses the above-threshold file as its GOLDEN, so the artifact the
        # suite trusts most was the one nothing checked.
        "prefilter-above-threshold-2026-08-09.json",
        "prefilter-forced-180-2026-08-09.json",
    ],
)
def test_prefilter_artifacts_are_internally_consistent(name: str) -> None:
    """These files are the evidence, so they are checked for the properties
    a reader would otherwise have to take on trust: that the prefiltered
    rows really engaged, that the deltas are the subtraction they claim to
    be, and that both halves ran against the corpus on disk."""
    art = json.loads((_RESULTS / name).read_text(encoding="utf-8"))
    # Resolved by DIGEST, not by the filename the artifact recorded. Every
    # one of these ran against the 180-document corpus while it was called
    # corpus.jsonl; I1 expanded that filename in place and retained the
    # exact bytes as corpus-v2.jsonl. Checking the digest against the files
    # actually in the tree keeps the property this line is for — the corpus
    # this artifact was measured on is still here, byte for byte — and is
    # strictly stronger than trusting a name that can be reused.
    on_disk = {
        runner.corpus_fingerprint(path): path.name
        for path in sorted(_HERE.glob("corpus*.jsonl"))
    }
    assert art["corpus_sha256"] in on_disk, (
        f"{name} names corpus {art['corpus']} at {art['corpus_sha256'][:12]}, "
        f"which is no longer any corpus in the tree "
        f"({sorted(on_disk.values())}) — the artifact is unverifiable"
    )
    assert art["index_threshold"] == runner.INDEX_THRESHOLD
    assert art["prefilter_cap"] == runner.PREFILTER_CAP
    assert art["prefilter_mode"] == "both"

    rows = {(r["arm"], r["probe"], r["prefilter"]): r for r in art["results"]}
    for (_, _, prefilter), row in rows.items():
        if prefilter:
            assert row["engaged"] == row["n"], row
            assert row["mean_pool_size"] <= art["prefilter_cap"], row
        else:
            # The full corpus contains the gold document by construction.
            assert row["gold_nominated"] == 1.0, row
            assert row["mean_pool_size"] == art["corpus_size"], row
        assert row["recall_at_5"] <= row["gold_nominated"], row

    assert art["prefilter_delta"], "a `both` run must publish its deltas"
    for delta in art["prefilter_delta"]:
        on = rows[(delta["arm"], delta["probe"], True)]
        off = rows[(delta["arm"], delta["probe"], False)]
        for k in (1, 5):
            expected = off[f"recall_at_{k}"] - on[f"recall_at_{k}"]
            assert delta[f"recall_loss_at_{k}"] == pytest.approx(expected)
        assert delta["gold_nomination_rate"] == on["gold_nominated"]


def test_the_harness_self_check_note_is_gated_on_the_lane() -> None:
    """The `both` run's off half re-measures a committed 2026-07-26
    artifact — but only lane-off. Those references predate 5.1 and are
    lane-off by construction, so under `--rescue-expansion on` the off
    half ranks with repairs they never had and reproduces nothing.

    The three committed `prefilter-*-2026-08-09.json` files carry the
    self-check note in error and their own rows falsify it (as-asked
    45%/85% against v2-padded600's 25%/60%). They are receipts and stay
    as measured; the erratum is in README.md. This pins the gate so the
    claim cannot be emitted from a lane-on run again."""
    source = (_HERE / "run.py").read_text(encoding="utf-8")
    assert "if baseline is not None and not RESCUE_EXPANSION:" in source, (
        "the harness self-check note is no longer gated on the lane"
    )

    published = json.loads(
        (_RESULTS / "prefilter-above-threshold-2026-08-09.json").read_text(
            encoding="utf-8"
        )
    )
    assert published["rescue_expansion"] is True
    reference = json.loads(
        (_RESULTS / "v2-padded600-2026-07-26.json").read_text(encoding="utf-8")
    )
    off = {
        (r["arm"], r["probe"]): r for r in published["results"] if not r["prefilter"]
    }
    before = {(r["arm"], r["probe"]): r for r in reference["results"]}
    key = ("lexical", "asked")
    assert off[key]["recall_at_5"] != before[key]["recall_at_5"], (
        "the lane-on off half now matches the pre-5.1 reference, which "
        "would make the erratum in README.md wrong — re-read both"
    )


def test_the_padded_prefilter_artifact_reproduces_its_predecessor() -> None:
    """The off half of the paired run re-measures what
    `v2-padded600-2026-07-26.json` recorded three days earlier, on the same
    corpus digest. That is what makes the on half credible: the harness is
    shown to reproduce a published number before it is used to produce a
    new one."""
    new = json.loads(
        (_RESULTS / "prefilter-above-threshold-2026-07-30.json").read_text()
    )
    old = json.loads((_RESULTS / "v2-padded600-2026-07-26.json").read_text())
    assert new["corpus_sha256"] == old["corpus_sha256"]
    assert new["corpus_size"] == old["corpus_size"]

    published = {(r["arm"], r["probe"]): r for r in old["results"]}
    reproduced = [r for r in new["results"] if not r["prefilter"]]
    assert reproduced, "no full-corpus rows to compare"
    for row in reproduced:
        before = published[(row["arm"], row["probe"])]
        assert row["recall_at_1"] == before["recall_at_1"], row
        assert row["recall_at_5"] == before["recall_at_5"], row
