"""Tests for reciprocal rank fusion in search.py.

RRF combines multiple ranked lists into one without needing the underlying
scores to be on the same scale — critical for hybrid retrieval where
BM25, the keyword scorer, and cosine similarity all live on different
scales.
"""

from __future__ import annotations

import pytest

from bettermemory.search import reciprocal_rank_fusion


def test_rrf_single_ranker_preserves_order() -> None:
    """With one ranker, RRF should produce monotonically-decreasing
    scores in the same order as the input list. The exact scores
    rescale (1/(k+rank)) but the order is invariant."""
    fused = reciprocal_rank_fusion([["a", "b", "c", "d"]], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    assert [mid for mid, _ in ranked] == ["a", "b", "c", "d"]


def test_rrf_consensus_promotes_doc_to_top() -> None:
    """A doc that ranks well across all rankers should rise to the top.
    Classic RRF demo: each ranker has a unique #1, but a doc that's #2
    everywhere wins overall."""
    fused = reciprocal_rank_fusion(
        [
            ["x", "shared", "a"],
            ["y", "shared", "b"],
            ["z", "shared", "c"],
        ],
        k=60,
    )
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    assert ranked[0][0] == "shared"


def test_rrf_doc_absent_from_rankers_not_in_output() -> None:
    """A doc that no ranker surfaced should be absent from the fused
    map entirely — callers must not have to special-case "score == 0"
    to know a doc didn't appear."""
    fused = reciprocal_rank_fusion(
        [
            ["a", "b"],
            ["b", "c"],
        ],
        k=60,
    )
    assert set(fused.keys()) == {"a", "b", "c"}
    assert "d" not in fused


def test_rrf_empty_input_returns_empty_dict() -> None:
    assert reciprocal_rank_fusion([], k=60) == {}


def test_rrf_each_ranker_empty_returns_empty_dict() -> None:
    """Multiple rankers, all empty — defensive case for "no ranker
    produced any hits"; commonly happens when query tokens are
    all stopwords."""
    assert reciprocal_rank_fusion([[], [], []], k=60) == {}


def test_rrf_invalid_k_raises() -> None:
    """k must be positive. Negative or zero k would either divide by
    zero or invert the rank order; the original paper requires k>0
    and a non-trivial value (typically 60) so terms at high ranks
    don't dominate."""
    with pytest.raises(ValueError, match="k must be positive"):
        reciprocal_rank_fusion([["a"]], k=0)
    with pytest.raises(ValueError, match="k must be positive"):
        reciprocal_rank_fusion([["a"]], k=-5)


def test_rrf_duplicates_within_one_ranker_use_first_position() -> None:
    """A doc id appearing twice in the same ranker shouldn't double-count.
    First (best) position wins. Matches the "one rank per (ranker, doc)"
    contract from the original paper — the second appearance is a
    misranking, not a separate signal."""
    fused = reciprocal_rank_fusion([["a", "b", "a"]], k=60)
    only_a = reciprocal_rank_fusion([["a", "b"]], k=60)
    # Both should give 'a' the same score: 1/(60+1).
    assert abs(fused["a"] - only_a["a"]) < 1e-9


def test_rrf_k_controls_smoothing() -> None:
    """Smaller k makes top ranks dominate more; larger k spreads weight
    further down the list. Sanity check: top-of-list score with k=10
    should exceed top-of-list with k=60 (smaller divisor → larger value)."""
    small_k = reciprocal_rank_fusion([["a", "b"]], k=10)
    large_k = reciprocal_rank_fusion([["a", "b"]], k=60)
    assert small_k["a"] > large_k["a"]
    # And the ratio between rank-1 and rank-2 should also be larger for
    # small k — that's the "top dominates" property.
    assert (small_k["a"] / small_k["b"]) > (large_k["a"] / large_k["b"])


def test_rrf_no_consensus_top_is_union_of_per_ranker_tops() -> None:
    """When rankers disagree completely (no doc shared between any of
    them), the fused list's top should be the per-ranker #1s, all tied
    at 1/(k+1). Sanity check for the "no agreement" pathological case —
    RRF degrades gracefully rather than picking one ranker arbitrarily."""
    fused = reciprocal_rank_fusion(
        [
            ["a"],
            ["b"],
            ["c"],
        ],
        k=60,
    )
    # All three should have identical scores.
    assert fused["a"] == fused["b"] == fused["c"]
