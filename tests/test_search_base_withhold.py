"""Tests for round 9's base-leg withholding (`_BASE_LEG_TRAILING_WITHHOLD`).

The mechanism: in the hybrid path, a base leg whose rank-1 candidate
matched strictly fewer query terms than its peer's is withheld from the
fusion (weight 0.0); ties return `None` and fuse byte-identically to
the pre-round-9 engine. Preregistered in the LongMemEval
preregistration addendum 12; derivation on the
constant in search.py.

These tests pin the three properties the preregistration leans on:
the shipped default is inert, ties are byte-identical with the
mechanism on, and a genuine evidence split hands the fusion to the
leading leg. Plus the declared scope exclusion: the stopword fallback
never sees a weight override.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bettermemory.search as engine
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _base_leg_weights, search


def _memory(body: str, *, offset_seconds: int = 0) -> Memory:
    # Distinct, monotone `created` stamps keep every tiebreak
    # deterministic without mocking the clock.
    created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=offset_seconds
    )
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _leg(*matched_counts: int) -> list[tuple[Memory, float, list[str]]]:
    """A ranking whose rank-1 matched `matched_counts[0]` terms.

    Scores descend with position so `_leg_top_evidence`'s
    (score, created, id) max lands on the first entry.
    """
    out: list[tuple[Memory, float, list[str]]] = []
    for i, count in enumerate(matched_counts):
        mem = _memory(f"doc {i}", offset_seconds=i)
        out.append(
            (mem, float(len(matched_counts) - i), [f"t{j}" for j in range(count)])
        )
    return out


def test_shipped_default_is_off_and_inert() -> None:
    """The constant ships False until the round-9 gates pass, and while
    False the helper returns None for ANY pair — the fusion is the
    pre-round-9 engine byte-for-byte (None means unweighted RRF)."""
    assert engine._BASE_LEG_TRAILING_WITHHOLD is False
    assert _base_leg_weights([_leg(3), _leg(1)]) is None


def test_tie_returns_none(monkeypatch) -> None:
    """Equal rank-1 evidence — 80% of dev probes — must return None,
    not [1.0, 1.0]: None is the arm the fusion treats as byte-identical
    to the pre-weights code path (P64's blast-radius bound)."""
    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    assert _base_leg_weights([_leg(2, 1), _leg(2, 2)]) is None
    assert _base_leg_weights([_leg(0), _leg(0)]) is None


def test_trailing_leg_is_withheld(monkeypatch) -> None:
    """The leading leg keeps its full vote; the trailing leg gets 0.0 —
    withholding, not damping. Both orders, so the rule is symmetric."""
    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    assert _base_leg_weights([_leg(3), _leg(1)]) == [1.0, 0.0]
    assert _base_leg_weights([_leg(1), _leg(3)]) == [0.0, 1.0]


def test_non_pair_rankings_are_untouched(monkeypatch) -> None:
    """The rule is defined on the base PAIR only. A single-leg or
    three-leg rankings list (a future ranker joining the fusion) must
    fall back to the shipped flat weights rather than guess."""
    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    assert _base_leg_weights([_leg(3)]) is None
    assert _base_leg_weights([_leg(3), _leg(1), _leg(2)]) is None


def test_tied_store_is_byte_identical_end_to_end(monkeypatch) -> None:
    """A single-token query matches exactly one term at every leg's
    rank-1, so the legs tie and mechanism-on output must equal
    mechanism-off output in ids AND scores — P64 at search() level."""
    memories = [
        _memory("python list comprehension notes", offset_seconds=0),
        _memory("python decorators and closures", offset_seconds=1),
        _memory("kubernetes networking runbook", offset_seconds=2),
    ]
    off = search(memories, "python", mode="hybrid")
    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    on = search(memories, "python", mode="hybrid")
    assert [(h.id, h.score) for h in off] == [(h.id, h.score) for h in on]


def test_split_evidence_hands_the_fusion_to_the_leading_leg(monkeypatch) -> None:
    """Construct a genuine evidence split: the keyword leg's rank-1
    covers all three query terms while BM25's tf-saturated favourite
    matches only one. With the mechanism on, the fused head must be the
    leading leg's candidate — the trailing leg no longer votes it down.

    This is the census's counterfactual arm reproduced in miniature:
    weight-zero withholding, everything else the shipped engine.
    """
    full_match = _memory("alpha beta gamma project notes", offset_seconds=0)
    tf_heavy = _memory(
        "alpha alpha alpha alpha alpha alpha alpha alpha", offset_seconds=1
    )
    fillers = [
        _memory(f"beta gamma filler document {i}", offset_seconds=2 + i)
        for i in range(6)
    ]
    memories = [full_match, tf_heavy, *fillers]

    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    on = search(memories, "alpha beta gamma", mode="hybrid", max_results=3)
    assert on[0].id == full_match.id


def test_stopword_fallback_never_sees_a_weight_override(monkeypatch) -> None:
    """Addendum 12's declared scope exclusion: the stopword fallback's
    TF stream has different matched semantics, so the base pair fuses
    with weights=None there even with the mechanism on. Spied at the
    fusion boundary rather than inferred from output."""
    monkeypatch.setattr(engine, "_BASE_LEG_TRAILING_WITHHOLD", True)
    seen: list[list[float] | None] = []
    real = engine._hybrid_fuse

    def spy(
        rankings: list[list[tuple[Memory, float, list[str]]]],
        *,
        rrf_k: int,
        weights: list[float] | None = None,
    ) -> list[tuple[Memory, float, list[str]]]:
        seen.append(weights)
        return real(rankings, rrf_k=rrf_k, weights=weights)

    monkeypatch.setattr(engine, "_hybrid_fuse", spy)
    memories = [
        _memory("what would you do for this", offset_seconds=0),
        _memory("how it should have been done", offset_seconds=1),
    ]
    search(memories, "what should", mode="hybrid", allow_empty_query=True)
    assert seen, "the fallback path never reached the fusion"
    assert all(w is None for w in seen)
