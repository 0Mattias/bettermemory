"""Tests for `bench/df_census.py`, round 2's pre-run statistic.

This census is the ONLY thing round 2 reads from the held-out corpus
before it runs, and the threshold in
`bench/longmemeval/PREREGISTRATION.md` addendum 4 is read straight off
its output. A census that silently measured a different quantity than
the gate will price with would move that threshold without anything
noticing — the output is aggregates, so a wrong number looks exactly
like a right one.

So the two places it could diverge from the engine are pinned here:
the query pipeline (it must be `search()`'s, not a hand-mirror) and the
document-frequency count (it must be the stream the BM25 legs score
against). Both are pinned against the engine's own helpers rather than
against literals, so an engine change fails this file instead of
quietly shifting the census.

`bench/` is not a package, so the module is loaded by file location the
same way a bench run would execute it.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bettermemory.expansion import expansion_terms
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _EXPANSION_TABLES,
    _RESCUE_COVERAGE_GATE,
    _expand_kebab,
    _memory_tokens,
    _stem_token,
    _strip_stopwords,
    tokenize,
)

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "df_census.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_df_census", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_df_census"] = module
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
# The query pipeline is the engine's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "do we ever rip the old toggles back out",
        "where do the creds go",
        "who owns split-testing here",
        "i was wondering about the deploy",
        "the",  # stopword-only: strips to nothing
        "",
    ],
)
def test_query_tokens_match_the_engine_route(query: str) -> None:
    """Tokenize, expand kebab compounds, strip stopwords — in that
    order, through the engine's own helpers. A census that tokenized
    differently would census terms no leg can emit."""
    assert census.query_tokens_of(query) == _strip_stopwords(
        _expand_kebab(tokenize(query))
    )


@pytest.mark.parametrize(
    "query",
    [
        "do we ever rip the old toggles back out",
        "where do the creds go",
        "who owns split-testing here",
        "kubernetes ingress controller",  # tables know none of it
        "the",
    ],
)
def test_emitted_terms_come_from_the_real_build_site(query: str) -> None:
    """The census must ask `expansion_terms` itself. Re-deriving the
    union here is how query-side and census-side views drift, which is
    the same argument `expansion.py` makes for having one build site."""
    toks = census.query_tokens_of(query)
    expected = (
        expansion_terms(list(dict.fromkeys(toks)), _EXPANSION_TABLES, _stem_token)
        if toks
        else []
    )
    assert census.emitted_terms(query) == expected


def test_emitted_terms_never_include_filler() -> None:
    """The 5.1.1 disjointness invariant, restated at the census: if
    filler leaked back into the emitted list, every df statistic below
    would be measuring the wrong population."""
    for query in ("i was wondering where the creds are", "still thinking about it"):
        leaked = set(census.emitted_terms(query)) & _EXPANSION_TABLES.filler_stems
        assert not leaked, f"{query!r} leaked filler into the census: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# df is counted against the stream the scorer prices with
# ---------------------------------------------------------------------------


def test_df_counts_documents_not_occurrences() -> None:
    """Document frequency, not term frequency: a body repeating a term
    five times still counts once."""
    memories = [
        _memory("credential credential credential credential credential"),
        _memory("unrelated inventory shelving notes"),
    ]
    assert census.term_df(memories, ["credential"]) == {"credential": 1}


def test_df_uses_the_bm25_content_stream() -> None:
    """Counted against `_MemoryTokens.content` — the stopword-stripped
    stream the BM25 legs score against — so the census prices the same
    population a gate would."""
    memories = [_memory("Credential injection for containers uses mounted files.")]
    stream = set(_memory_tokens(memories[0]).content)
    for term in ("credential", "contain", "mounted"):
        expected = 1 if term in stream else 0
        assert census.term_df(memories, [term])[term] == expected, term


def test_df_of_an_absent_term_is_zero_and_kept() -> None:
    """Zero-df terms stay in the record. `morph_variants` is a rule and
    emits non-words; dropping them silently would inflate the live
    population the threshold is read off."""
    memories = [_memory("nothing relevant here at all")]
    assert census.term_df(memories, ["guesed"]) == {"guesed": 0}


# ---------------------------------------------------------------------------
# The engagement flag
# ---------------------------------------------------------------------------


def test_engagement_is_the_gate_arithmetic_not_the_leg_label() -> None:
    """A confident query does not engage; a paraphrase-only one does.

    `matched_leg == "expansion"` is NOT the signal — that label only
    appears for a hit the base legs missed entirely, so it undercounts
    engagement on a corpus where the leg reorders rather than rescues.
    """
    memories = [
        _memory("Feature flag removal has an owner and a deadline."),
        _memory("Credential injection for containers uses mounted files."),
        _memory("The reconciliation job runs at 0300 UTC."),
    ]
    assert census.gate_engages(memories, "feature flag removal owner deadline") is False
    assert census.gate_engages(memories, "do we ever rip the old toggles back out")


def test_engagement_restores_the_gate_it_borrows() -> None:
    """The probe silences the leg by moving the module-level gate. A
    leaked mutation would disable the rescue for every later caller in
    the process — including the recall runs this census precedes."""
    memories = [_memory("anything at all here")]
    census.gate_engages(memories, "where do the creds go")
    import bettermemory.search as engine

    assert engine._RESCUE_COVERAGE_GATE == _RESCUE_COVERAGE_GATE


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def test_summary_separates_live_terms_from_zero_df_noise() -> None:
    """Zero-df terms are counted but never enter the ratio statistics:
    they match nothing, and letting them pile into the lowest band
    would make any threshold look generous."""
    records = [
        {
            "emitted": 3,
            "engaged": True,
            "median_df_ratio_live": 0.1,
            "terms": [
                {"term": "a", "df": 10, "df_ratio": 0.1},
                {"term": "b", "df": 0, "df_ratio": 0.0},
                {"term": "c", "df": 0, "df_ratio": 0.0},
            ],
        }
    ]
    out = census.summarise(records)
    assert out["emitted_terms"] == 3
    assert out["emitted_terms_with_df_gt0"] == 1
    assert out["questions_engaging_the_leg"] == 1
    assert out["df_ratio_bands_live"] == {"[0.1,0.2)": 1}
    assert out["df_ratio_live"]["p50"] == 0.1


def test_summary_survives_an_empty_population() -> None:
    assert census.summarise([])["emitted_terms"] == 0
    empty: list[dict[str, Any]] = [
        {"emitted": 0, "engaged": False, "median_df_ratio_live": None, "terms": []}
    ]
    assert census.summarise(empty)["emitted_terms_with_df_gt0"] == 0
