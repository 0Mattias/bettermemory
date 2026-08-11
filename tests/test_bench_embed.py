"""Tests for `bench/embed_train.py` and `bench/embed_census.py`, P1e's evidence.

The census decides P1e before any engine code is written, exactly as
P1a's did, so its arithmetic is load-bearing twice over. A trainer that
silently optimised something other than the GloVe objective would
produce a verdict about a mechanism nobody proposed; and a census whose
emission rule quietly differed from the one the incumbent is measured
under would produce a ratio between two different quantities.

Both are therefore pinned against hand-computable fixtures rather than
against the live corpora — the same discipline
`tests/test_bench_ppmi_census.py` applies to the PPMI census — plus one
property no fixture can express: that the trainer recovers structure
that is demonstrably present, which is what separates "the corpus has
no signal" from "the trainer cannot find one".
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory.search import _EXPANSION_TABLES

_BENCH = Path(__file__).resolve().parents[1] / "bench"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _BENCH / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


trainer = _load("bench_embed_train", "embed_train.py")
census = _load("bench_embed_census", "embed_census.py")
hybrid = _load("bench_embed_hybrid", "embed_hybrid.py")


# ---------------------------------------------------------------------------
# Reading committed text
# ---------------------------------------------------------------------------


def test_paragraphs_unwrap_hard_wrapped_prose() -> None:
    """This repository wraps at ~72 columns, so a line is not a unit."""
    text = "one two\nthree four\n\nfive six\n"
    assert trainer._paragraphs(text) == ["one two three four", "five six"]


def test_paragraphs_drop_empty_blocks() -> None:
    assert trainer._paragraphs("\n\n   \n\nalpha\n\n") == ["alpha"]


def test_python_prose_keeps_english_and_drops_code() -> None:
    """Identifiers are not sentences: a window model fed `store.write`
    learns the call graph, which is not the vocabulary being measured."""
    source = '"""Module about credentials."""\n\n\ndef f(x):\n    # rotate the token\n    return x.some_identifier_name\n'
    chunks = trainer._python_prose(source)
    joined = " ".join(chunks)
    assert "credentials" in joined
    assert "rotate the token" in joined
    assert "some_identifier_name" not in joined


def test_python_prose_survives_an_unparseable_file() -> None:
    assert trainer._python_prose("def (:::") == []


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def test_vocabulary_is_ordered_by_count_then_term(monkeypatch) -> None:
    """Index assignment has to be a function of the corpus alone —
    float addition is not associative, so an unstable order is a
    different model."""
    streams = [["b"] * 3 + ["a"] * 3 + ["c"] * 2]
    monkeypatch.setattr(trainer, "MIN_COUNT", 1)
    vocab, index = trainer.build_vocab(streams)
    assert vocab == ["a", "b", "c"]
    assert index == {"a": 0, "b": 1, "c": 2}


def test_vocabulary_applies_the_count_floor() -> None:
    streams = [["keep"] * trainer.MIN_COUNT + ["drop"] * (trainer.MIN_COUNT - 1)]
    vocab, _index = trainer.build_vocab(streams)
    assert vocab == ["keep"]


def test_cooccurrence_matches_the_hand_computation() -> None:
    """Three tokens in a row, window 5: `a` sees `b` at distance 1 and
    `c` at distance 2, so 1/1 and 1/2. Both directions accumulate, so
    the matrix is symmetric."""
    index = {"a": 0, "b": 1, "c": 2}
    counts = trainer.cooccurrence([["a", "b", "c"]], index, window=5)
    assert counts[(0, 1)] == pytest.approx(1.0)
    assert counts[(1, 0)] == pytest.approx(1.0)
    assert counts[(0, 2)] == pytest.approx(0.5)
    assert counts[(2, 0)] == pytest.approx(0.5)
    assert counts[(1, 2)] == pytest.approx(1.0)


def test_the_window_bounds_the_reach() -> None:
    index = {"a": 0, "b": 1, "c": 2}
    counts = trainer.cooccurrence([["a", "b", "c"]], index, window=1)
    assert (0, 2) not in counts
    assert counts[(0, 1)] == pytest.approx(1.0)


def test_an_out_of_vocabulary_token_does_not_close_the_window() -> None:
    """Dropping a rare word must not make its neighbours adjacent, or
    the model learns co-occurrences the text does not contain."""
    index = {"a": 0, "c": 1}
    counts = trainer.cooccurrence([["a", "gone", "c"]], index, window=5)
    assert counts[(0, 1)] == pytest.approx(0.5), "distance must still be 2"


def test_cooccurrence_does_not_pair_across_units() -> None:
    index = {"a": 0, "b": 1}
    assert trainer.cooccurrence([["a"], ["b"]], index, window=5) == {}


# ---------------------------------------------------------------------------
# The objective
# ---------------------------------------------------------------------------


def test_training_drives_the_prediction_towards_log_x() -> None:
    """GloVe's residual is `w_i . c_j + b_i + b_j - log X_ij`. One cell,
    many epochs: the model must be able to represent its own target, or
    every number downstream is measuring an optimiser that cannot fit a
    single point."""
    counts = {(0, 1): 20.0, (1, 0): 20.0}
    vectors, losses = trainer.train(counts, 2, dim=4, epochs=400, lr=0.1, seed=1)
    assert losses[-1] < losses[0]
    assert losses[-1] < 1e-3
    assert len(vectors) == 2 and len(vectors[0]) == 4


def test_the_weighting_function_saturates_at_xmax() -> None:
    """`f(x) = (x/XMAX)^ALPHA` below XMAX and exactly 1 at or above it.

    Pinned because it is the only place a co-occurrence count enters the
    objective other than through its logarithm: a weighting that did not
    damp rare pairs would report on a different estimator.
    """
    assert trainer.weight(trainer.XMAX) == 1.0
    assert trainer.weight(trainer.XMAX * 10) == 1.0
    assert trainer.weight(trainer.XMAX / 2) == pytest.approx(0.5**trainer.ALPHA)
    assert trainer.weight(1.0) == pytest.approx((1.0 / trainer.XMAX) ** trainer.ALPHA)
    assert trainer.weight(1.0) < trainer.weight(10.0) < 1.0


def test_a_zero_learning_rate_freezes_the_model() -> None:
    """The loss the trainer reports is the objective at the CURRENT
    parameters, so with no step the two epochs must agree."""
    counts = {(0, 1): 30.0, (1, 0): 30.0}
    _v, losses = trainer.train(counts, 2, dim=2, epochs=2, lr=0.0, seed=1)
    assert losses[0] == losses[1]


def test_training_is_deterministic() -> None:
    counts = {(0, 1): 3.0, (1, 0): 3.0, (1, 2): 5.0, (2, 1): 5.0}
    first, _ = trainer.train(counts, 3, dim=8, epochs=20, seed=trainer.SEED)
    second, _ = trainer.train(counts, 3, dim=8, epochs=20, seed=trainer.SEED)
    assert first == second, "identical inputs must give identical floats"


def test_a_different_seed_gives_a_different_model() -> None:
    """The determinism above has to come from the seed rather than from
    the trainer having no randomness to begin with."""
    counts = {(0, 1): 3.0, (1, 0): 3.0, (1, 2): 5.0, (2, 1): 5.0}
    first, _ = trainer.train(counts, 3, dim=8, epochs=20, seed=1)
    second, _ = trainer.train(counts, 3, dim=8, epochs=20, seed=2)
    assert first != second


def test_the_trainer_recovers_structure_that_is_present() -> None:
    """Two disjoint topics, each word only ever seen beside its own.

    This is the control the census's negative result depends on: without
    it, "the vectors carry no usable neighbours" cannot be told apart
    from "the trainer does not work".
    """
    left = ["alpha", "bravo", "charlie", "delta"]
    right = ["mike", "november", "oscar", "papa"]
    rng = random.Random(7)
    streams = []
    for _ in range(300):
        streams.append([rng.choice(left) for _ in range(10)])
        streams.append([rng.choice(right) for _ in range(10)])
    vocab, index = trainer.build_vocab(streams)
    counts = trainer.cooccurrence(streams, index)
    vectors, _ = trainer.train(counts, len(vocab), dim=16, epochs=15, seed=3)
    unit = trainer.unit_normalise(vectors)
    lookup = {t: unit[i] for i, t in enumerate(vocab)}

    def cos(a: str, b: str) -> float:
        return sum(x * y for x, y in zip(lookup[a], lookup[b]))

    within = min(cos(a, b) for a in left for b in left if a != b)
    across = max(cos(a, b) for a in left for b in right)
    assert within > across + 0.3


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def test_unit_normalise_leaves_a_zero_vector_alone() -> None:
    assert trainer.unit_normalise([[0.0, 0.0]]) == [[0.0, 0.0]]


def test_unit_normalise_makes_the_norm_one() -> None:
    out = trainer.unit_normalise([[3.0, 4.0]])[0]
    assert math.sqrt(sum(v * v for v in out)) == pytest.approx(1.0)


def test_mean_centre_removes_the_common_component() -> None:
    centred = trainer.mean_centre([[1.0, 2.0], [3.0, 4.0]])
    assert centred == [[-1.0, -1.0], [1.0, 1.0]]


# ---------------------------------------------------------------------------
# The census's emission rule
# ---------------------------------------------------------------------------


def test_candidate_pool_enforces_the_disjointness_invariant() -> None:
    """The 5.1.1 invariant applies to a trained source exactly as it
    applies to the committed tables and to P1a's derived one."""
    filler = next(iter(_EXPANSION_TABLES.filler_stems))
    collection = {"credential", "ab", "queri", filler, "mine"}
    pool = census.candidate_pool(collection, collection, {"mine"})
    assert "credential" in pool
    assert "ab" not in pool, "length floor"
    assert filler not in pool, "filler stems"
    assert "mine" not in pool, "the caller's own tokens"


def test_candidate_pool_is_restricted_to_the_model_vocabulary() -> None:
    pool = census.candidate_pool({"alpha", "bravo"}, {"alpha"}, set())
    assert pool == ["alpha"]


def test_candidate_pool_is_sorted() -> None:
    pool = census.candidate_pool(
        {"zulu", "alpha", "mike"}, {"zulu", "alpha", "mike"}, set()
    )
    assert pool == sorted(pool)


def test_ranked_neighbours_break_ties_on_the_term() -> None:
    """Two candidates at identical cosine must come back in a fixed
    order, or the same store emits different terms on different runs."""
    model = census.Model(
        ["q", "zulu", "alpha"],
        {"q": [1.0, 0.0], "zulu": [1.0, 0.0], "alpha": [1.0, 0.0]},
        "raw",
    )
    ranked = model.ranked("q", ["zulu", "alpha"])
    assert [t for _s, t in ranked] == ["alpha", "zulu"]


def test_ranked_returns_nothing_for_an_out_of_vocabulary_token() -> None:
    model = census.Model(["a"], {"a": [1.0, 0.0]}, "raw")
    assert model.ranked("absent", ["a"]) == []


def test_summarise_computes_precision_over_pooled_terms() -> None:
    """Pooled, not averaged per probe: the bar is quoted as a fraction
    of emitted terms, and a per-probe mean is a different statistic."""
    records = [
        {
            "query_tokens": 2,
            "query_tokens_in_vocab": 1,
            "static_terms": 4,
            "static_hits": 1,
            "grid": {"k1_t0": {"terms": 6, "hits": 3, "new_hits": 2}},
        },
        {
            "query_tokens": 2,
            "query_tokens_in_vocab": 2,
            "static_terms": 6,
            "static_hits": 2,
            "grid": {"k1_t0": {"terms": 4, "hits": 0, "new_hits": 0}},
        },
    ]
    summary = census.summarise(records)
    assert summary["incumbent_precision"] == pytest.approx(0.3)
    assert summary["vocabulary_coverage"] == pytest.approx(0.75)
    cell = summary["grid"]["k1_t0"]
    assert cell["precision"] == pytest.approx(0.3)
    assert cell["terms_per_probe"] == pytest.approx(5.0)
    assert cell["gate_multiple"] == pytest.approx(1.0)
    assert cell["probes_with_a_new_hit"] == 1


def _cell(precision: float, per_probe: float, total: int, multiple: float) -> dict:
    return {
        "precision": precision,
        "terms_per_probe": per_probe,
        "terms_total": total,
        "gate_multiple": multiple,
        "precision_ci95": [0.0, 1.0],
    }


def _summary(incumbent: float, per_probe: float, total: int, grid: dict) -> dict:
    return {
        "incumbent_precision": incumbent,
        "incumbent_terms_per_probe": per_probe,
        "incumbent_terms_total": total,
        "grid": grid,
    }


def test_verdict_scores_the_best_cell_against_the_incumbent() -> None:
    summary = _summary(
        0.2,
        5.0,
        200,
        {
            "wide": _cell(0.1, 5.0, 50, 0.5),
            "tight": _cell(0.3, 1.0, 40, 1.5),
        },
    )
    out = census.verdict(summary)
    assert out["best_cell"] == "tight"
    assert out["passes"] is True
    assert out["matched_budget_cell"] == "wide", "closest to the incumbent's width"
    assert out["best_at_or_above_budget_cell"] == "wide", "the only cell that wide"


def test_verdict_fails_when_no_cell_reaches_parity() -> None:
    summary = _summary(0.2743, 5.65, 226, {"a": _cell(0.19, 7.6, 300, 0.703)})
    assert census.verdict(summary)["passes"] is False


def test_a_cell_below_the_sample_floor_cannot_carry_the_verdict() -> None:
    """A 2-of-4 cell reports 0.5 precision. Without a floor it would
    'pass' a bar the mechanism never reached — and this grid's tightest
    cells really do collapse to a handful of terms."""
    summary = _summary(
        0.2,
        5.0,
        200,
        {
            "wide": _cell(0.1, 5.0, 50, 0.5),
            "toosmall": _cell(0.5, 0.1, census.MIN_GATE_TERMS - 1, 2.5),
        },
    )
    out = census.verdict(summary)
    assert out["best_cell"] == "wide"
    assert out["passes"] is False


def test_the_gate_does_not_apply_when_the_incumbent_row_is_tiny() -> None:
    """On LongMemEval the committed tables emit twelve terms across
    twenty probes. A precision computed on twelve terms is not a bar,
    and reporting a ratio against it would be worse than reporting
    nothing."""
    summary = _summary(0.4167, 0.6, 12, {"a": _cell(0.52, 2.9, 58, 1.241)})
    out = census.verdict(summary)
    assert out["gate_applicable"] is False
    assert out["passes"] is False, "a ratio above 1.0 cannot pass a void gate"


def test_wilson_brackets_the_point_estimate() -> None:
    lo, hi = census.wilson(62, 226)
    assert lo < 62 / 226 < hi
    assert (lo, hi) == pytest.approx((0.2203, 0.3359), abs=1e-3)


def test_wilson_narrows_as_the_sample_grows() -> None:
    narrow = census.wilson(200, 1000)
    wide = census.wilson(2, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_wilson_stays_inside_the_unit_interval() -> None:
    assert census.wilson(0, 5)[0] == 0.0
    assert census.wilson(5, 5)[1] == 1.0
    assert census.wilson(0, 0) == (0.0, 0.0)


def test_two_proportion_p_separates_p1a_from_p1e() -> None:
    """The number the record's "unresolvable" claim rests on. P1a's gap
    is real at these sample sizes; P1e's is not, and a census that could
    not tell them apart would be reporting the same verdict for two
    different measurements."""
    p1a = census.two_proportion_p(49, 391, 62, 226)
    p1e = census.two_proportion_p(49, 226, 62, 226)
    assert p1a < 0.01
    assert p1e > 0.10
    assert census.two_proportion_p(62, 226, 62, 226) == pytest.approx(1.0)


def test_the_census_quotes_the_published_p1a_bar() -> None:
    """The ratio is against a number the repository already published.
    Recomputing the bar here and letting it drift would turn a gate into
    a moving target."""
    assert census.P1A_INCUMBENT_PRECISION == 0.2743
    assert census.GATE_MULTIPLE == 1.0


# ---------------------------------------------------------------------------
# The designed-for-regime estimator
# ---------------------------------------------------------------------------


def test_ngrams_carry_word_boundaries() -> None:
    """Without the markers 'split' and 'unsplittable' share every gram
    of 'split' and look equally related. Morphology is mostly an affix
    phenomenon, so the boundaries carry much of the signal."""
    grams = hybrid.ngrams("split")
    assert "<sp" in grams, "a prefix gram anchored to the word start"
    assert "it>" in grams, "a suffix gram anchored to the word end"
    assert "spl" in grams
    assert all(hybrid.NGRAM_MIN <= len(g) <= hybrid.NGRAM_MAX for g in grams)
    # 'unsplittable' contains 'split' but starts nowhere near it, so the
    # anchored grams are exactly what keeps the two apart.
    assert "<sp" not in hybrid.ngrams("unsplittable")


def test_bridging_reaches_a_morphological_variant() -> None:
    """The mechanism's whole purpose: 'splitting' has no vector, 'split'
    does, and they share most of their characters."""
    model = census.Model(
        ["split", "unrelated"], {"split": [1.0, 0.0], "unrelated": [0.0, 1.0]}, "raw"
    )
    index = {t: hybrid.ngrams(t) for t in model.vocab}
    built = hybrid.bridge("splitting", model, index)
    assert built is not None
    assert built[0] > 0.9, "must land on 'split', not on 'unrelated'"


def test_bridging_refuses_a_token_it_cannot_reach() -> None:
    """A token resembling nothing gets no vector rather than a vector
    averaged over the whole lexicon — the census counts that honestly
    under coverage."""
    model = census.Model(["credential"], {"credential": [1.0, 0.0]}, "raw")
    index = {t: hybrid.ngrams(t) for t in model.vocab}
    assert hybrid.bridge("zzqqxx", model, index) is None


def test_bridging_is_a_no_op_for_a_known_token() -> None:
    model = census.Model(["split"], {"split": [1.0, 0.0]}, "raw")
    index = {t: hybrid.ngrams(t) for t in model.vocab}
    assert hybrid.bridge("split", model, index) == model.vec["split"]


def test_the_hybrid_reuses_p1a_ppmi_settings_verbatim() -> None:
    """The sparse half has to be the estimator P1a published, or the
    hybrid is composing something else and the 0.46x reference does not
    apply to it."""
    assert hybrid.PPMI_MIN_DF in hybrid._PPMI.MIN_DF_GRID
    assert hybrid.PPMI_SHIFT in hybrid._PPMI.SHIFT_GRID


def test_the_committed_hybrid_census_records_its_own_negative() -> None:
    """The agreement rule was the invention and the data withdrew its
    premise. Pinned so the record cannot drift back to the hypothesis:
    intersecting two estimators of the SAME matrix is worse than the
    dense one alone at the incumbent's width."""
    path = _BENCH / "retrieval" / "results" / "embed-hybrid-2026-08-11.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    arms = payload["arms"]
    for name, arm in arms.items():
        assert arm["verdict"]["passes"] is False, name

    at_width = arms["raw"]["verdict"]["best_at_or_above_budget"]
    assert at_width is not None
    assert at_width["gate_multiple"] < 0.6, (
        "the agreement rule must be recorded as WORSE than the plain "
        "dense model's 0.79x at comparable width"
    )

    # Bridging did what it was built to do, and it was aimed at recall
    # while the bar prices precision.
    assert (
        arms["raw+bridge"]["summary"]["vocabulary_coverage"]
        > arms["raw"]["summary"]["vocabulary_coverage"]
    )
    assert arms["raw+bridge"]["summary"]["query_tokens_bridged"] > 0


# ---------------------------------------------------------------------------
# The committed census the verdict is read off
# ---------------------------------------------------------------------------


def test_the_committed_census_shows_no_arm_reaching_parity() -> None:
    """P1e's bar is P1a's, unchanged: a replacement expansion source has
    to be at least as precise as the committed tables. Pinned because
    that comparison is the whole verdict — if these numbers move, P1e
    has to be re-censused rather than the conclusion quietly surviving.
    """
    path = _BENCH / "retrieval" / "results" / "embed-census-2026-08-11.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["p1a_standard"]["incumbent_precision"] == 0.2743

    arms = payload["retrieval"]["arms"]
    assert arms, "the retrieval instrument must be scored"
    for name, arm in arms.items():
        summary = arm["summary"]
        assert summary["incumbent_precision"] == pytest.approx(0.2743, abs=1e-3), name
        assert arm["verdict"]["passes"] is False, name
        assert arm["verdict"]["best_gate_multiple"] < 1.0, name

    # The dense factorization beats the raw counts it replaces and still
    # misses the bar. Both halves matter: the first says the mechanism is
    # not inert, the second is the verdict.
    best = max(a["verdict"]["best_gate_multiple"] for a in arms.values())
    assert best > 0.46, "must improve on P1a's raw-PPMI 0.46x"
    assert best < 1.0, "and must still miss parity"


def test_the_committed_census_shows_more_text_making_it_worse() -> None:
    """The census's sharpest finding, and the one a future reader is
    most likely to doubt: a corpus 23x larger with better query-token
    coverage is markedly LESS precise. If that inverts, the conclusion
    that the corpus rather than the estimator is the wall no longer
    follows from these artifacts."""
    path = _BENCH / "retrieval" / "results" / "embed-census-2026-08-11.json"
    arms = json.loads(path.read_text(encoding="utf-8"))["retrieval"]["arms"]
    store = arms["store/centred"]
    repo = arms["repo/centred"]

    assert (
        repo["summary"]["vocabulary_coverage"]
        > (store["summary"]["vocabulary_coverage"])
    ), "the larger corpus must cover more of the query vocabulary"
    assert repo["verdict"]["best_gate_multiple"] < (
        store["verdict"]["best_gate_multiple"] / 2
    ), "and must still be far less precise"


def test_the_committed_census_voids_the_gate_on_longmemeval() -> None:
    """The conversational instrument's incumbent row is twelve terms.
    Pinned so a future run cannot quietly start publishing a ratio
    against it."""
    path = _BENCH / "longmemeval" / "results" / "embed-census-2026-08-11.json"
    arms = json.loads(path.read_text(encoding="utf-8"))["longmemeval"]["arms"]
    for name, arm in arms.items():
        assert arm["verdict"]["gate_applicable"] is False, name
        assert arm["verdict"]["passes"] is False, name
    incumbent = next(iter(arms.values()))["summary"]
    assert incumbent["incumbent_terms_total"] < census.MIN_GATE_TERMS


def test_the_committed_sensitivity_forecloses_undertraining() -> None:
    """Ten times the epochs fits the matrix twenty times better and
    halves the precision. Without this the headline reads as a lucky
    under-trained corner rather than the declared default."""
    path = _BENCH / "retrieval" / "results" / "embed-sensitivity-2026-08-11.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    arms = payload["retrieval"]["arms"]
    default = arms["d64-e15/centred"]
    longest = arms["d64-e150/centred"]

    assert payload["models"]["d64-e150"]["final_loss"] < (
        payload["models"]["d64-e15"]["final_loss"] / 10
    ), "more epochs must fit the objective far better"
    assert longest["verdict"]["best_gate_multiple"] < (
        default["verdict"]["best_gate_multiple"] / 2
    ), "and must still be far less precise"
    assert default["verdict"]["best_gate_multiple"] == max(
        a["verdict"]["best_gate_multiple"] for a in arms.values()
    ), "the declared default must be the sweep's best, not a cherry-pick"
