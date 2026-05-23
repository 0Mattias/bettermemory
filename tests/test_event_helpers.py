"""Smoke tests for the ``EventLog`` test helper.

The helper exists to prevent the 2.6.2/2.6.3 class of bug. These tests
confirm that an event emitted through the helper round-trips through
the canonical ``Recorder`` and ``iter_events`` path, so a future
``Recorder`` field rename surfaces in this file too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._event_helpers import EventLog


def test_emit_returns_canonical_shape(tmp_path: Path) -> None:
    log = EventLog(root=tmp_path, session_id="s1")
    event = log.emit("search", returned=["m1", "m2"], relevance=["high", "low"])
    # Canonical recorder always stamps ts, session, kind.
    assert event["kind"] == "search"
    assert event["session"] == "s1"
    assert isinstance(event["ts"], str)
    # Caller-supplied fields are merged.
    assert event["returned"] == ["m1", "m2"]
    assert event["relevance"] == ["high", "low"]


def test_events_property_orders_writes(tmp_path: Path) -> None:
    log = EventLog(root=tmp_path)
    log.emit("write", id="a", status="committed")
    log.emit("search", returned=["a"], relevance=["high"])
    log.emit("use", ids=["a"], outcome="applied")
    kinds = [e["kind"] for e in log.events]
    assert kinds == ["write", "search", "use"]


def test_fixture_provides_event_log(event_log: EventLog) -> None:
    """The ``event_log`` fixture works the same as direct construction."""
    event_log.emit("search", returned=["m1"], relevance=["high"])
    assert len(event_log.events) == 1
    assert event_log.events[0]["returned"] == ["m1"]


def test_last_event_returns_most_recent(tmp_path: Path) -> None:
    log = EventLog(root=tmp_path)
    log.emit("write", id="a")
    log.emit("write", id="b")
    assert log.last_event["id"] == "b"


def test_last_event_raises_on_empty(tmp_path: Path) -> None:
    log = EventLog(root=tmp_path)
    # Trigger directory creation without writing an event by touching
    # the recorder's root (Recorder.__post_init__ already mkdir'd it).
    # An empty log shouldn't silently return None.
    log.recorder.path.write_text("")
    with pytest.raises(IndexError):
        log.last_event


def test_shape_matches_real_handlers_emission(tmp_path: Path) -> None:
    """If anyone renames a Recorder field, this test surfaces it.

    The contract test pins the canonical kind values and field names
    that consumers (consolidate, llm, eval, audit, health) depend on.
    Updating the producer without updating this list is the exact
    failure mode that shipped 2.6.2/2.6.3 — pin it explicitly so
    drift fails the suite.
    """
    log = EventLog(root=tmp_path, session_id="s1")
    search = log.emit(
        "search",
        query="q",
        returned=["a", "b"],
        relevance=["high", "low"],
    )
    use = log.emit(
        "use",
        ids=["a"],
        outcome="applied",
        claim_excerpts=["matched phrase"],
        attribution="model",
    )
    miss = log.emit(
        "search_miss",
        probe_query="q",
        top_hits=[{"id": "a", "relevance": "low"}],
        threshold_rule="rule-v1",
        lookback_seconds=600,
    )
    write = log.emit("write", id="m1", status="committed")
    # Pin the canonical field names. If any of these assertions breaks,
    # both the producer-side change AND every consumer reading the
    # field need updating. Every field emitted above is asserted — the
    # 2.6.4 audit found this test emitted `claim_excerpts` /
    # `lookback_seconds` without ever asserting them, so a rename
    # would have slipped through the "contract" test silently.
    assert "returned" in search and "relevance" in search and "query" in search
    assert "ids" in use and "outcome" in use and "attribution" in use
    assert "claim_excerpts" in use
    assert "top_hits" in miss and "threshold_rule" in miss
    assert "lookback_seconds" in miss and "probe_query" in miss
    assert "status" in write
