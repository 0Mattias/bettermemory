"""Usage-aware ranking: the bounded endorsement factor and its wiring.

The factor mirrors the recency boost (capped at +10%), so it breaks
near-ties in favour of memories the model has deliberately applied, but can
never override the relevance signal. It is opt-in via
`[behavior] endorsement_boost`; with the flag off (the shipped default) the
ranker is byte-identical to before — pinned here and by the unchanged
test_search* suites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.handlers.search import _explicit_applied_counts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _endorsement_factor, search
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from ._mcp import call_tool as _mcp_call

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _memory(body: str, *, created: datetime = _T) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def test_endorsement_factor_is_bounded_and_monotonic() -> None:
    assert _endorsement_factor(0) == 1.0
    assert _endorsement_factor(-5) == 1.0  # defensive: negatives are neutral
    # Strictly increasing in the count, and capped at +10% (the bound is
    # reached exactly at large n once exp() underflows to 0.0).
    prev = 1.0
    for n in (1, 3, 10, 100, 10_000):
        f = _endorsement_factor(n)
        assert f > prev, f"factor not increasing at {n}"
        assert 1.0 < f <= 1.1, f"factor {f} out of bounds at {n}"
        prev = f


def test_endorsement_breaks_a_tie() -> None:
    """Two memories that tie on relevance + recency sort by id by default;
    an endorsement count flips the result to the endorsed one."""
    a = _memory("alpha beta gamma")
    b = _memory("alpha beta gamma")
    mems = [a, b]

    default_winner = search(mems, "alpha beta gamma", mode="keyword")[0].id
    underdog = b.id if default_winner == a.id else a.id

    boosted = search(
        mems, "alpha beta gamma", mode="keyword", applied_by_id={underdog: 10}
    )
    assert boosted[0].id == underdog, "endorsement should win the tie"


def test_endorsement_cannot_override_relevance() -> None:
    """The +10% cap means a heavily-endorsed weak match still loses to a
    strongly-relevant memory — endorsement is a tie-breaker, not a lever."""
    strong = _memory("alpha alpha alpha alpha alpha")
    weak = _memory("alpha lone unrelated body text")
    hits = search(
        [strong, weak], "alpha", mode="keyword", applied_by_id={weak.id: 100_000}
    )
    assert hits[0].id == strong.id


def test_endorsement_unset_is_neutral() -> None:
    """applied_by_id=None (the default) must produce the identical ranking
    to passing it explicitly empty — the no-op guarantee that keeps the
    shipped default byte-stable."""
    mems = [_memory("alpha beta"), _memory("alpha gamma"), _memory("alpha delta")]
    a = [h.id for h in search(mems, "alpha", mode="hybrid")]
    b = [h.id for h in search(mems, "alpha", mode="hybrid", applied_by_id=None)]
    c = [h.id for h in search(mems, "alpha", mode="hybrid", applied_by_id={})]
    assert a == b == c


def _ts(now: datetime, *, ago: int) -> str:
    """Canonical ``…Z`` event ts, `ago` seconds before `now`."""
    return (now - timedelta(seconds=ago)).isoformat().replace("+00:00", "Z")


def test_explicit_applied_counts_excludes_auto_and_filters_ids() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts = _ts(now, ago=100)  # comfortably inside the 600s window
    events: list[dict[str, Any]] = [
        {
            "ts": ts,
            "kind": "use",
            "outcome": "applied",
            "auto": False,
            "ids": ["m1", "m1"],
        },
        {
            "ts": ts,
            "kind": "use",
            "outcome": "applied",
            "auto": True,
            "ids": ["m1"],
        },  # auto
        {
            "ts": ts,
            "kind": "use",
            "outcome": "ignored",
            "auto": False,
            "ids": ["m1"],
        },  # not applied
        {
            "ts": ts,
            "kind": "use",
            "outcome": "applied",
            "auto": False,
            "ids": ["m2", "off"],
        },
        {"ts": ts, "kind": "search", "returned": ["m1"]},  # not a use event
        {
            "ts": ts,
            "kind": "use",
            "outcome": "applied",
            "memory_ids": ["m2"],
        },  # legacy key
    ]
    counts = _explicit_applied_counts(
        events, {"m1", "m2"}, now=now, lookback_seconds=600
    )
    assert counts == {"m1": 2, "m2": 2}  # auto + ignored + off-set id all excluded


def test_explicit_applied_counts_enforces_its_own_cutoff() -> None:
    """The window is a MANDATORY, self-enforced argument: handing the tally an
    OVER-WIDE event list (an apply from 1800s ago, plus one with no `ts` at
    all) still yields only the in-600s count. This is the structural guard the
    fix adds — the function no longer trusts callers to pre-window. Reverting
    the internal `ts` drop re-counts the stale/undatable applies and this
    assertion fails."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    events: list[dict[str, Any]] = [
        # In-window: the only apply that must count.
        {
            "ts": _ts(now, ago=100),
            "kind": "use",
            "outcome": "applied",
            "auto": False,
            "ids": ["m1"],
        },
        # Out-of-window (inside a 3600s dedup read, outside 600s): dropped.
        {
            "ts": _ts(now, ago=1800),
            "kind": "use",
            "outcome": "applied",
            "auto": False,
            "ids": ["m1"],
        },
        # No parseable ts — unprovable, so dropped rather than counted.
        {"kind": "use", "outcome": "applied", "auto": False, "ids": ["m1"]},
    ]
    counts = _explicit_applied_counts(events, {"m1"}, now=now, lookback_seconds=600)
    assert counts == {"m1": 1}


def test_config_endorsement_boost_defaults_off_and_loads() -> None:
    assert BehaviorConfig().endorsement_boost is False
    assert BehaviorConfig(endorsement_boost=True).endorsement_boost is True


async def _search_ids(server: Any) -> list[str]:
    structured = await _mcp_call(
        server, "memory_search", {"query": "alpha beta gamma", "auto_scope": False}
    )
    # The SDK wraps a list return under "result".
    hits = (
        structured.get("result", structured)
        if isinstance(structured, dict)
        else structured
    )
    return [h["id"] for h in hits]


async def test_handler_endorsement_enabled_uses_applied_events(tmp_path: Path) -> None:
    """End-to-end wiring: with the flag on, the handler reads the event log,
    tallies explicit applies, and the endorsed memory wins a tie. With the
    flag off, the same applied events are ignored."""
    store = Store(tmp_path)
    a = store.write(content="alpha beta gamma", scopes=["tools"])
    b = store.write(content="alpha beta gamma", scopes=["tools"])

    def _server(*, boost: bool) -> Any:
        cfg = Config(
            storage=StorageConfig(directory=str(tmp_path)),
            behavior=BehaviorConfig(endorsement_boost=boost),
        )
        return build_server(config=cfg, store=Store(tmp_path), state=SessionState())

    server_off = _server(boost=False)
    default_order = await _search_ids(server_off)
    underdog = b.id if default_order[0] == a.id else a.id

    rec = Recorder(root=tmp_path, session_id="test-session", enabled=True)
    for _ in range(15):
        rec.record("use", ids=[underdog], outcome="applied", auto=False)

    # Flag OFF: applied events ignored, default order preserved.
    assert await _search_ids(_server(boost=False)) == default_order

    # Flag ON: the endorsed underdog climbs to the top.
    assert (await _search_ids(_server(boost=True)))[0] == underdog


def test_explicit_applied_counts_survives_malformed_id_fields() -> None:
    """The tally iterated the event's raw `ids` field, so one malformed event
    in the plaintext, hand-editable log — `"ids": 42` (TypeError: not
    iterable) or `"ids": [["m1"]]` (unhashable list at the set lookup) —
    failed EVERY memory_search and memory_audit_turn call under
    endorsement_boost. These are the exact poison shapes 3.15.0 hardened
    health.py's `_event_id_list` against while this walk, reading the very
    same events, stayed raw. The shared normalizer must drop malformed
    containers and elements and still count the well-formed ids around them.
    Reverting the loop to the raw field makes the first poison event raise
    and this test fail."""
    now = datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    poison_and_real: list[dict[str, Any]] = [
        {"kind": "use", "outcome": "applied", "ts": ts, "ids": 42},
        {"kind": "use", "outcome": "applied", "ts": ts, "ids": [["m1"]]},
        {"kind": "use", "outcome": "applied", "ts": ts, "ids": {"m1": 1}},
        {"kind": "use", "outcome": "applied", "ts": ts, "ids": ["m1", 7, ["x"]]},
        {"kind": "use", "outcome": "applied", "ts": ts, "memory_ids": "m2"},
    ]
    counts = _explicit_applied_counts(
        poison_and_real,
        {"m1", "m2"},
        now=now,
        lookback_seconds=600,
    )
    # The lone well-formed element and the bare-string legacy shape count;
    # every malformed container/element is dropped, none of them crash.
    assert counts == {"m1": 1, "m2": 1}
