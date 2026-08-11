"""Tests for `bench/ppmi_census.py`, P1a's pre-implementation evidence.

The census decided P1a before any engine code was written, so its
arithmetic is load-bearing: a PPMI implementation that silently computed
something other than PPMI would have produced a verdict about the wrong
mechanism. The association maths and the exclusion rules are pinned
against hand-computable fixtures rather than against the live corpus.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _EXPANSION_TABLES

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "ppmi_census.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_ppmi_census", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_ppmi_census"] = module
    spec.loader.exec_module(module)
    return module


census = _load()


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


# ---------------------------------------------------------------------------
# The association arithmetic
# ---------------------------------------------------------------------------


def test_ppmi_matches_the_hand_computation() -> None:
    """Four documents; 'a' and 'b' always co-occur, so their joint
    probability is 4x the product of their marginals when each appears
    in half the collection."""
    docs = [{"a", "b"}, {"a", "b"}, {"c"}, {"c"}]
    got = dict(census.associates(docs, "a", min_df=1, shift=1.0, top_k=5))
    # p(a)=p(b)=0.5, p(a,b)=0.5 -> log(0.5/0.25) = log 2
    assert got["b"] == pytest.approx(math.log(2.0))


def test_the_shift_subtracts_log_shift() -> None:
    docs = [{"a", "b"}, {"a", "b"}, {"c"}, {"c"}]
    unshifted = dict(census.associates(docs, "a", min_df=1, shift=1.0, top_k=5))["b"]
    shifted = census.associates(docs, "a", min_df=1, shift=2.0, top_k=5)
    # log 2 - log 2 == 0, and non-positive associations are dropped.
    assert unshifted == pytest.approx(math.log(2.0))
    assert shifted == []


def test_negative_association_is_dropped_not_returned() -> None:
    """PPMI is POSITIVE pointwise mutual information: terms that
    co-occur less than chance carry no expansion signal."""
    docs = [{"a", "x"}, {"a", "y"}, {"b", "z"}, {"b", "w"}, {"a", "q"}, {"b", "r"}]
    assert all(
        v > 0 for _t, v in census.associates(docs, "a", min_df=1, shift=1.0, top_k=9)
    )


def test_min_df_keeps_hapax_pairs_out() -> None:
    """In a small store a term seen once beside another produces an
    enormous, meaningless PPMI — the failure the floor exists for."""
    docs = [{"a", "rare"}, {"a", "common"}, {"a", "common"}, {"common"}]
    loose = dict(census.associates(docs, "a", min_df=1, shift=1.0, top_k=5))
    strict = dict(census.associates(docs, "a", min_df=2, shift=1.0, top_k=5))
    assert "rare" in loose
    assert "rare" not in strict


def test_a_term_below_min_df_has_no_associates() -> None:
    docs = [{"a", "b"}, {"c"}, {"c"}]
    assert census.associates(docs, "a", min_df=2, shift=1.0, top_k=5) == []


def test_the_clamp_bounds_a_freak_pair() -> None:
    assert census.PPMI_CLAMP == pytest.approx(math.log(1000.0))
    docs = [{"a", "b"}] + [{"c"} for _ in range(999)]
    got = census.associates(docs, "a", min_df=1, shift=1.0, top_k=5)
    assert all(v <= census.PPMI_CLAMP for _t, v in got)


def test_associates_are_deterministic_under_ties() -> None:
    """Ties break on the term string, so the same store always yields
    the same list — the reproducibility the bench rules require."""
    # The pair must co-occur MORE than chance, so the collection needs
    # documents without them — a term present in every document has zero
    # association by definition.
    docs = [{"a", "m", "n"}, {"a", "m", "n"}, {"x"}, {"x"}]
    got = [t for t, _v in census.associates(docs, "a", min_df=1, shift=1.0, top_k=2)]
    assert got == ["m", "n"]


def test_top_k_truncates_by_weight() -> None:
    docs = [
        {"a", "strong"},
        {"a", "strong"},
        {"a", "weak"},
        {"weak"},
        {"weak"},
        {"weak"},
    ]
    got = [t for t, _v in census.associates(docs, "a", min_df=1, shift=1.0, top_k=1)]
    assert got == ["strong"]


# ---------------------------------------------------------------------------
# The exclusion rules the lane already enforces
# ---------------------------------------------------------------------------


def test_derive_excludes_query_tokens_and_filler_and_short_terms() -> None:
    """A store-derived source is held to the same disjointness invariant
    as the committed tables: the df-floor that deflates filler still
    only covers the caller's own tokens, so filler emitted from ANY
    source re-enters at full corpus-rare IDF."""
    filler = next(iter(_EXPANSION_TABLES.filler_stems))
    docs = [
        {"query", filler, "ab", "genuine"},
        {"query", filler, "ab", "genuine"},
        {"unrelated"},
        {"unrelated"},
    ]
    got = census.derive(docs, ["query"], min_df=1, shift=1.0, top_k=9)
    assert "query" not in got
    assert filler not in got
    assert "ab" not in got, "terms under the length floor must not be emitted"
    assert "genuine" in got


def test_document_terms_uses_the_bm25_content_stream() -> None:
    """The co-occurrence unit is the stopword-stripped stream the legs
    score against, so the census counts the population a ranker prices."""
    from bettermemory.search import _memory_tokens

    m = _memory("Credential injection for containers uses mounted files.")
    assert census.document_terms([m]) == [set(_memory_tokens(m).content)]


# ---------------------------------------------------------------------------
# The committed census the verdict is read off
# ---------------------------------------------------------------------------


def test_the_committed_census_shows_no_cell_reaching_parity() -> None:
    """Addendum 8's Gate 0 asks whether a store-derived source can match
    the incumbent's precision. Pinned because that comparison is the
    whole verdict: if these numbers move, P1a has to be re-preregistered
    rather than the conclusion quietly surviving.
    """
    path = _BENCH.parent / "retrieval" / "results" / "ppmi-census-2026-08-11.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    s = payload["summary"]
    static = s["static_hits_total"] / s["static_terms_total"]
    assert static == pytest.approx(0.2743, abs=1e-3)

    best = max(v["precision"] for v in s["grid"].values())
    assert best == pytest.approx(0.1253, abs=1e-3)
    assert best < static, "no grid cell may reach the incumbent's precision"
    assert best / static < 0.5

    # The signal is real even though the precision is not: PPMI finds
    # gold terms the static tables miss.
    assert max(v["new_hits_total"] for v in s["grid"].values()) > 100
