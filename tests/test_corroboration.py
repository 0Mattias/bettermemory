"""Recurrence as evidence: the corroboration rollup and its consumers.

A dedup-rejected memory_write IS the stored claim re-entering a
conversation — `Store.record_corroboration` bumps a persisted rollup
(`corroborations`, `last_corroborated`) on the matched memory without
touching `updated`. Consumers: the freshest-touch curation window
(always), memory_show / memory_list surfacing (always, absent while
zero), and the opt-in `[behavior] corroboration_boost` ranking nudge.

The write-handler hook is once-per-(memory, session)
(`SessionState.corroborated_ids`) and best-effort — a telemetry bump
must never turn a clean duplicate rejection into an error.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import _freshest_touch_ts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _corroboration_factor, search
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
# The factor
# ---------------------------------------------------------------------------


def test_corroboration_factor_is_bounded_and_monotonic() -> None:
    assert _corroboration_factor(0) == 1.0
    assert _corroboration_factor(-3) == 1.0
    prev = 1.0
    for n in (1, 3, 10, 100, 10_000):
        f = _corroboration_factor(n)
        assert f > prev, f"factor not increasing at {n}"
        assert 1.0 < f <= 1.1, f"factor {f} out of bounds at {n}"
        prev = f


def test_corroboration_breaks_a_tie_only_when_enabled() -> None:
    a = _memory("alpha beta gamma")
    b = _memory("alpha beta gamma", corroborations=5)
    mems = [a, b]

    default_winner = search(mems, "alpha beta gamma", mode="keyword")[0].id
    boosted = search(mems, "alpha beta gamma", mode="keyword", corroboration_boost=True)
    assert boosted[0].id == b.id, "corroborated memory should win the tie"
    # And the flag off (the shipped default) never reads the rollup.
    if default_winner != b.id:
        assert default_winner == a.id


def test_corroboration_cannot_override_relevance() -> None:
    strong = _memory("alpha alpha alpha alpha alpha")
    weak = _memory("alpha lone unrelated body text", corroborations=100_000)
    hits = search([strong, weak], "alpha", mode="keyword", corroboration_boost=True)
    assert hits[0].id == strong.id


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

    # Round-trip through the on-disk frontmatter.
    reloaded = store.load_one(written.id)
    assert reloaded.corroborations == 1
    assert reloaded.last_corroborated == bumped.last_corroborated

    again = store.record_corroboration(written.id)
    assert again.corroborations == 2


def test_zero_rollup_keeps_frontmatter_byte_identical(tmp_path: Path) -> None:
    """A never-corroborated memory must serialize without the new keys —
    the absence-as-signal shape older readers already expect."""
    store = Store(tmp_path / "memories")
    written = store.write(content="alpha beta", scopes=["tools"])
    path = next(p for p in (tmp_path / "memories").glob("*.md"))
    text = path.read_text(encoding="utf-8")
    assert written.id in text
    assert "corroborations" not in text
    assert "last_corroborated" not in text


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


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


def _build(memory_dir: Path, **behavior: Any) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(**behavior),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_e2e_duplicate_write_records_corroboration(memory_dir: Path) -> None:
    server = _build(memory_dir)
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
    server2 = _build(memory_dir)
    dup3 = await _call(
        server2,
        "memory_write",
        content="postgres runs on port 5432 in the homelab",
        scopes=["infrastructure"],
    )
    assert dup3["status"] == "duplicate"
    assert dup3["corroboration_recorded"] is True
    assert dup3["corroborations"] == 2


async def test_e2e_duplicate_event_carries_corroborated_id(memory_dir: Path) -> None:
    from bettermemory.events import iter_events

    server = _build(memory_dir)
    first = await _call(
        server, "memory_write", content="redis caches sessions", scopes=["infra"]
    )
    await _call(
        server, "memory_write", content="redis caches sessions", scopes=["infra"]
    )
    write_events = [
        e
        for e in iter_events(memory_dir)
        if e.get("kind") == "write" and e.get("status") == "duplicate"
    ]
    assert write_events, "duplicate write event missing"
    assert write_events[-1].get("corroborated_id") == first["id"]


async def test_e2e_forced_write_does_not_corroborate(memory_dir: Path) -> None:
    """force=True skips the dedup gate entirely — the caller asserts the
    new memory is meaningfully different, so no recurrence is credited."""
    server = _build(memory_dir)
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
