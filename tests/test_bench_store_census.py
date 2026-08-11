"""Tests for `bench/store_census.py`, round 8's pre-implementation check.

The census decided round 8 before any adaptation rule was written: if
cheap store statistics cannot tell the two corpora apart, a rule keyed
on them cannot move the leg's weight between their two optima. That
verdict rests on the statistics being computed correctly, so they are
pinned against hand-computable fixtures rather than the live corpora.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _EXPANSION_TABLES

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "store_census.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_store_census", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_store_census"] = module
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


def test_statistics_are_computed_over_the_scored_stream() -> None:
    """`_MemoryTokens.content` is the stopword-stripped stream the BM25
    legs price, so the census describes the population a ranker sees."""
    from bettermemory.search import _memory_tokens

    mems = [_memory("alpha beta gamma"), _memory("alpha delta")]
    got = census.store_statistics(mems)
    expected_len = sum(len(_memory_tokens(m).content) for m in mems) / 2
    assert got["docs"] == 2.0
    assert got["mean_doc_len"] == pytest.approx(expected_len)


def test_filler_share_counts_the_listed_stems() -> None:
    filler = sorted(_EXPANSION_TABLES.filler_stems)[0]
    mems = [_memory(f"{filler} kubernetes ingress controller topology")]
    got = census.store_statistics(mems)
    assert 0.0 < got["filler_tok_share"] < 1.0

    clean = census.store_statistics([_memory("kubernetes ingress controller")])
    assert clean["filler_tok_share"] == 0.0


def test_type_token_ratio_falls_with_repetition() -> None:
    varied = census.store_statistics([_memory("alpha beta gamma delta")])
    repeated = census.store_statistics([_memory("alpha alpha alpha alpha")])
    assert repeated["type_token_ratio"] < varied["type_token_ratio"]


def test_hapax_share_is_one_when_every_term_occurs_once() -> None:
    got = census.store_statistics([_memory("alpha beta gamma delta")])
    assert got["hapax_share"] == pytest.approx(1.0)


def test_compare_flags_only_a_real_separation() -> None:
    """The bar exists because a rule keyed on a statistic has to amplify
    that statistic's spread into the weight's spread. A near-constant
    input driving a 2x output is a fit, not a derivation."""
    assert census.SEPARATION_BAR == 2.0
    dev = {"a": 1.0, "b": 1.0, "c": 1.0}
    other = {"a": 2.5, "b": 1.1, "c": 0.3}
    got = census.compare(dev, other)
    assert got["a"]["separates"] is True  # far above
    assert got["b"]["separates"] is False  # near-constant
    assert got["c"]["separates"] is True  # far below


def test_the_committed_census_shows_no_statistic_separating() -> None:
    """Addendum 11's Gate 0. Pinned because the verdict is the whole
    round: if these numbers move, the adaptive family has to be
    re-preregistered rather than the conclusion quietly surviving."""
    path = _BENCH.parent / "retrieval" / "results" / "store-census-2026-08-11.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["any_statistic_separates"] is False
    ratios = [v["ratio"] for v in payload["comparison"].values()]
    assert max(ratios) < census.SEPARATION_BAR
    # The closest is a length artifact, not a register signal.
    assert payload["comparison"]["mean_doc_len"]["ratio"] == pytest.approx(
        1.70, abs=0.02
    )
    assert payload["comparison"]["filler_tok_share"]["ratio"] == pytest.approx(
        1.13, abs=0.02
    )
