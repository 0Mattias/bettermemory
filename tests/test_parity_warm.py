"""Rank parity with the token cache warm.

The token cache in `search._memory_tokens` memoises a pure function on its
own inputs, so a warm process must rank exactly as a cold one. This holds
it to that on the rank-parity harness's own fixture: the 40-document
corpus and 12 questions of tests/test_bench_parity.py, with the index
threshold forced to 10 so the prefilter arm engages. Every query of the
fixture runs through both arms in one process, once with the cache cleared
before every search and once warm, and the two runs' rows must be equal:
ids, scores and relevance labels, and every other field of every hit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import bettermemory.search as search_module
from bettermemory.models import Memory
from bettermemory.store import Store

from .test_bench_parity import _rows, _subset, runner


def _keep_whole_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every hit whole in each arm's row, next to the ids, rounded
    scores and labels the parity artifact keeps."""
    real = runner._hits_to_dict

    def whole(hits: list[Any], *, engaged: bool, pool_size: int) -> dict[str, Any]:
        row = real(hits, engaged=engaged, pool_size=pool_size)
        row["hits"] = [hit.model_dump(mode="json") for hit in hits]
        return row

    monkeypatch.setattr(runner, "_hits_to_dict", whole)


def _run(
    store: Store,
    full: list[Memory],
    questions: list[dict[str, Any]],
    body_calls: list[str],
    *,
    cold: bool,
) -> tuple[list[dict[str, Any]], list[tuple[str, int, int]]]:
    """Every (question, probe) through both arms. Returns the rows and,
    per search, the arm, its pool size and how many candidate bodies it
    tokenised."""
    rows: list[dict[str, Any]] = []
    tokenised: list[tuple[str, int, int]] = []
    for question in questions:
        for probe in runner.PROBES:
            query = runner.query_for(question, probe)
            arms: dict[str, dict[str, Any]] = {}
            for arm in ("full", "prefilter"):
                if cold:
                    search_module.clear_token_cache()
                before = len(body_calls)
                if arm == "full":
                    arms[arm] = runner.rank_full(full, query)
                else:
                    arms[arm] = runner.rank_prefilter(store, query)
                tokenised.append(
                    (arm, arms[arm]["pool_size"], len(body_calls) - before)
                )
            rows.append(
                {
                    "slug": question["slug"],
                    "probe": probe,
                    "query": query,
                    **arms,
                }
            )
    return rows, tokenised


def test_cold_and_warm_runs_rank_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "10")
    _keep_whole_hits(monkeypatch)
    corpus_path, questions_path = _subset(tmp_path)
    runner.build_store(tmp_path / "store", corpus_path)
    questions = _rows(questions_path)

    with Store(tmp_path / "store") as store:
        full = store.load_all()
        assert len(full) == 40
        bodies = {m.body for m in full}
        assert len({(m.body, tuple(m.scopes)) for m in full}) == 40

        body_calls: list[str] = []
        real_tokenize = search_module.tokenize

        def counting(text: str) -> list[str]:
            if text in bodies:
                body_calls.append(text)
            return real_tokenize(text)

        monkeypatch.setattr(search_module, "tokenize", counting)

        cold_rows, cold_tokenised = _run(store, full, questions, body_calls, cold=True)
        # One uncleared pass fills the cache; the pass after it is served
        # from the cache for every candidate of every search.
        filling_rows, _ = _run(store, full, questions, body_calls, cold=False)
        warm_rows, warm_tokenised = _run(store, full, questions, body_calls, cold=False)

    assert len(cold_rows) == len(questions) * len(runner.PROBES) == 36
    # Cold: every search tokenised each of its candidate bodies once.
    assert all(n == pool for _, pool, n in cold_tokenised), cold_tokenised
    # Warm: no search tokenised a body.
    assert all(n == 0 for _, _, n in warm_tokenised), warm_tokenised

    assert warm_rows == cold_rows
    assert filling_rows == cold_rows
    for row in cold_rows:
        assert row["prefilter"]["engaged"] is True
        assert row["full"]["pool_size"] == 40
