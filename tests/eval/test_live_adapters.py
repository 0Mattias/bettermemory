"""Tests for the live competitor lane (`tests/eval/live_adapters.py`).

Everything here is deterministic by default: prerequisites are
monkeypatched so CI never accidentally drives a live bridge — GitHub
runners DO ship node/npx, so a bare PATH probe would go live under
plain `pytest`, which is exactly the invocation CI uses (no marker
deselection). The real end-to-end runs are opt-in behind
``BM_EVAL_LIVE=1`` (set by `tests/eval/run_live.sh`) and self-skip
everywhere else.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil

import pytest

from .adapters import SystemUnavailable, default_adapters
from .live_adapters import (
    Mem0LiveAdapter,
    ServerMemoryLiveAdapter,
    live_adapters,
    query_tokens,
    rank_entities,
)
from .workload import default_workload

_LIVE = os.environ.get("BM_EVAL_LIVE") == "1"
_live_only = pytest.mark.skipif(
    not _LIVE, reason="BM_EVAL_LIVE=1 required — maintainer live lane"
)


# ---------------------------------------------------------------------------
# stub/live parity — the matrix must be row-identical across modes
# ---------------------------------------------------------------------------


def test_stub_live_parity_names_and_capabilities():
    stubs = default_adapters()
    live = live_adapters()
    assert [a.name for a in stubs] == [a.name for a in live]
    for stub, live_a in zip(stubs, live):
        assert stub.capabilities() == live_a.capabilities()


# ---------------------------------------------------------------------------
# prerequisite probes degrade to the honest stub row — deterministically
# ---------------------------------------------------------------------------


def test_mem0_unavailable_without_package(monkeypatch: pytest.MonkeyPatch):
    """Block the import outright so the test holds even on a machine
    where mem0ai happens to be installed."""
    real_import = builtins.__import__

    def _no_mem0(name: str, *args, **kwargs):
        if name == "mem0" or name.startswith("mem0."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_mem0)
    with pytest.raises(SystemUnavailable) as exc:
        Mem0LiveAdapter().run(default_workload(), k=5)
    assert "mem0ai not importable" in exc.value.reason


def test_server_memory_unavailable_without_npx(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "tests.eval.live_adapters.shutil.which", lambda _name: None
    )
    with pytest.raises(SystemUnavailable) as exc:
        ServerMemoryLiveAdapter().run(default_workload(), k=5)
    assert "npx" in exc.value.reason


# ---------------------------------------------------------------------------
# the --live CLI seam
# ---------------------------------------------------------------------------


def test_live_flag_uses_live_roster(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`main(["--live"])` must route through live_adapters(). Swap the
    factory for the stub roster so the test never launches anything."""
    from . import comparative
    from . import live_adapters as live_mod

    monkeypatch.setattr(live_mod, "live_adapters", lambda: default_adapters())
    rc = comparative.main(["--live", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["results"]) == 5
    by_name = {r["name"]: r for r in data["results"]}
    assert by_name["bettermemory"]["ran"] is True


# ---------------------------------------------------------------------------
# the server-memory ranking helper — pure, testable without node
# ---------------------------------------------------------------------------


def test_query_tokens_lowercases_dedupes_and_drops_short():
    assert query_tokens("Pytest or unittest for PYTHON testing? or") == [
        "pytest",
        "unittest",
        "for",
        "python",
        "testing",
    ]


def test_rank_entities_scores_distinct_tokens_ties_lexicographic():
    hits = {"a": {"x", "y"}, "b": {"x"}, "c": {"y"}, "d": {"z"}}
    # x and y both match two distinct tokens -> tie broken by name;
    # z matches one; k truncates.
    assert rank_entities(hits, k=3) == ["x", "y", "z"]
    assert rank_entities(hits, k=1) == ["x"]
    assert rank_entities({}, k=5) == []


def test_server_memory_accommodation_is_necessary():
    """Documents WHY the bridge donates a tokenized-OR ranker: the
    server's native search_nodes is a whole-query substring match, and
    no gold probe's full query appears verbatim in any fact body — the
    raw server would score 0/7 on this workload by construction."""
    wl = default_workload()
    bodies = [f.body.lower() for f in wl.facts]
    for probe in wl.gold_probes:
        assert not any(probe.query.lower() in body for body in bodies), probe.query


# ---------------------------------------------------------------------------
# real live runs — BM_EVAL_LIVE=1 only (run_live.sh)
# ---------------------------------------------------------------------------


@_live_only
def test_live_mem0_run_bounds():
    pytest.importorskip("mem0")
    result = Mem0LiveAdapter().run(default_workload(), k=5)
    assert result.ran is True
    assert result.gold_total == 7
    assert result.eval_report is None  # no fabricated trio lanes
    assert result.recall_at_k is not None
    assert 0.0 <= result.recall_at_k <= 1.0
    assert result.system_version


@_live_only
def test_live_server_memory_roundtrip():
    if shutil.which("npx") is None:
        pytest.skip("npx not on PATH")
    result = ServerMemoryLiveAdapter().run(default_workload(), k=5)
    assert result.ran is True
    assert result.gold_total == 7
    assert result.eval_report is None
    assert result.recall_at_k is not None
    assert 0.0 <= result.recall_at_k <= 1.0
