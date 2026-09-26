"""Recurrence as evidence: the corroboration rollup and its consumers.

A dedup-rejected memory_write IS the stored claim re-entering a
conversation — `Store.record_corroboration` bumps a persisted rollup
(`corroborations`, `last_corroborated`) on the matched memory without
touching `updated`. Consumers: the freshest-touch curation window and
memory_show surfacing (absent while zero).

Ranking is NOT a consumer. 8.0.0 removed the `[behavior]
corroboration_boost` nudge (deprecated in 7.6.0): a corroboration needs
raw Jaccard >= 0.75 between two independently written bodies, which
prose-sized memories do not reach, so the nudge never fired. The flag
was off by default, so removing it must leave the shipped ranking
byte-identical — `test_rollup_never_moves_a_ranking` pins that the rollup
has no path into any scorer. How a config still setting the key loads
is tested in `test_config.py`.

The write-handler hook is once-per-(memory, session)
(`SessionState.corroborated_ids`) and best-effort — a telemetry bump
must never turn a clean duplicate rejection into an error.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import _freshest_touch_ts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import search
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _memory(body: str, *, corroborations: int = 0) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_T,
        updated=_T,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
        corroborations=corroborations,
    )


# ---------------------------------------------------------------------------
# Ranking: the rollup has no path into any scorer
# ---------------------------------------------------------------------------


def _ranking(hits: list[Any]) -> list[tuple[str, float, str, list[str]]]:
    return [(h.id, h.score, h.relevance, h.match_terms) for h in hits]


def test_rollup_never_moves_a_ranking() -> None:
    """The removal's byte-identity pin. `corroboration_boost` defaulted
    off, so the shipped ranking never read the rollup; with the flag gone
    nothing can. Rank one corpus twice — once carrying corroborations,
    once with every count zeroed — and require the same ids, scores,
    labels and matched terms in every mode.

    The identical-body twins are the case the old factor existed to
    flip: equal leg scores, so only the (created, id) tiebreaker orders
    them, and a +10% nudge on the corroborated twin would have reversed
    it. The weak/strong pair is the relevance case a nudge must never
    override. Both have to stay put."""
    twin_plain = _memory("alpha beta gamma")
    twin_corroborated = _memory("alpha beta gamma", corroborations=5)
    strong = _memory("alpha alpha alpha alpha alpha delta")
    weak = _memory("alpha lone unrelated body text delta", corroborations=100_000)
    other = _memory("beta gamma epsilon notes", corroborations=1)
    corroborated = [twin_plain, twin_corroborated, strong, weak, other]
    zeroed = [m.model_copy(update={"corroborations": 0}) for m in corroborated]

    for mode in ("keyword", "bm25", "hybrid"):
        for query in ("alpha beta gamma", "alpha", "delta alpha", "gamma notes"):
            with_rollup = search(corroborated, query, now=_T, mode=mode)
            without = search(zeroed, query, now=_T, mode=mode)
            assert with_rollup, (mode, query)
            assert _ranking(with_rollup) == _ranking(without), (mode, query)


# ---------------------------------------------------------------------------
# Store: persistence round-trip + the no-updated-bump contract
# ---------------------------------------------------------------------------


def test_record_corroboration_bumps_rollup_not_updated(tmp_path: Path) -> None:
    store = Store(tmp_path / "memories")
    written = store.write(content="postgres runs on port 5432", scopes=["infra"])
    assert written.corroborations == 0 and written.last_corroborated is None

    bumped = store.record_corroboration(written.id)
    assert bumped.corroborations == 1
    assert bumped.last_corroborated is not None
    assert bumped.updated == written.updated, (
        "a recurrence is not a rewrite — `updated` must not move"
    )
    assert bumped.last_verified_at is None, "nothing was checked against reality"

    # Round-trip through the store.
    reloaded = store.load_one(written.id)
    assert reloaded.corroborations == 1
    assert reloaded.last_corroborated == bumped.last_corroborated

    again = store.record_corroboration(written.id)
    assert again.corroborations == 2


def test_zero_rollup_round_trips_as_absent(tmp_path: Path) -> None:
    """A never-corroborated memory carries the zero rollup, and the record
    the store hands back is the record it took: the absence-as-signal
    shape memory_show reads (no key while zero) has nothing to invent."""
    store = Store(tmp_path / "memories")
    written = store.write(content="alpha beta", scopes=["tools"])
    assert written.corroborations == 0
    assert written.last_corroborated is None
    reloaded = store.load_one(written.id)
    assert reloaded.corroborations == 0
    assert reloaded.last_corroborated is None
    assert reloaded == written


# ---------------------------------------------------------------------------
# Health: corroboration is a freshness touch
# ---------------------------------------------------------------------------


def test_freshest_touch_includes_corroboration() -> None:
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert _freshest_touch_ts(old, old, None, recent) == recent.timestamp()
    assert _freshest_touch_ts(old, old, None, None) == old.timestamp()
    # The freshest of all four wins regardless of which axis it is.
    fresher_verify = recent + timedelta(days=1)
    assert (
        _freshest_touch_ts(old, old, fresher_verify, recent)
        == fresher_verify.timestamp()
    )


# ---------------------------------------------------------------------------
# End-to-end: the duplicate-rejection hook
# ---------------------------------------------------------------------------


def _build(store: Store, **behavior: Any) -> Any:
    """A server over `store`; a second call is a new session on the same
    store, which is what the once-per-session rule is measured against."""
    cfg = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        behavior=BehaviorConfig(**behavior),
    )
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id, enabled=True)
    return build_server(config=cfg, store=store, state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_e2e_duplicate_write_records_corroboration(store: Store) -> None:
    server = _build(store)
    first = await _call(
        server,
        "memory_write",
        content="postgres runs on port 5432 in the homelab",
        scopes=["infrastructure"],
    )
    mid = first["id"]

    dup = await _call(
        server,
        "memory_write",
        content="postgres runs on port 5432 in the homelab",
        scopes=["infrastructure"],
    )
    assert dup["status"] == "duplicate"
    assert dup["corroboration_recorded"] is True
    assert dup["corroborations"] == 1

    shown = _unwrap(await _call(server, "memory_show", id=mid))
    assert shown["corroborations"] == 1
    assert shown["last_corroborated"] is not None

    # Same session, same claim again: the recurrence was already
    # credited — once per (memory, session).
    dup2 = await _call(
        server,
        "memory_write",
        content="postgres runs on port 5432 in the homelab",
        scopes=["infrastructure"],
    )
    assert dup2["status"] == "duplicate"
    assert dup2["corroboration_recorded"] is False
    shown2 = _unwrap(await _call(server, "memory_show", id=mid))
    assert shown2["corroborations"] == 1

    # A NEW session is a new opportunity — build a second server over
    # the same store (fresh SessionState) and re-enter the claim.
    server2 = _build(store)
    dup3 = await _call(
        server2,
        "memory_write",
        content="postgres runs on port 5432 in the homelab",
        scopes=["infrastructure"],
    )
    assert dup3["status"] == "duplicate"
    assert dup3["corroboration_recorded"] is True
    assert dup3["corroborations"] == 2


async def test_e2e_duplicate_event_carries_corroborated_id(store: Store) -> None:
    server = _build(store)
    first = await _call(
        server, "memory_write", content="redis caches sessions", scopes=["infra"]
    )
    await _call(
        server, "memory_write", content="redis caches sessions", scopes=["infra"]
    )
    write_events = [
        e
        for e in store.iter_events()
        if e.get("kind") == "write" and e.get("status") == "duplicate"
    ]
    assert write_events, "duplicate write event missing"
    assert write_events[-1].get("corroborated_id") == first["id"]


async def test_e2e_forced_write_does_not_corroborate(store: Store) -> None:
    """force=True skips the dedup gate entirely — the caller asserts the
    new memory is meaningfully different, so no recurrence is credited."""
    server = _build(store)
    first = await _call(
        server, "memory_write", content="nginx fronts the homelab", scopes=["infra"]
    )
    forced = await _call(
        server,
        "memory_write",
        content="nginx fronts the homelab",
        scopes=["infra"],
        force=True,
    )
    assert forced["status"] == "committed"
    shown = _unwrap(await _call(server, "memory_show", id=first["id"]))
    assert "corroborations" not in shown
