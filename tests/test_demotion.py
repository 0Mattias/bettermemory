"""Usage-aware ranking, negative direction: the bounded demotion factor
and its wiring.

The factor mirrors `_endorsement_factor` (test_endorsement.py) with a
slightly deeper cap (-15%): it slides recently-rejected memories down a
near-tie but can never bury a strongly-relevant hit. Opt-in via
`[behavior] outcome_demotion`; with the flag off (the shipped default)
the ranker is byte-identical to before — pinned here and by the
unchanged test_search* suites.

"Active" negative semantics (what stops counting) are pinned at the
`_active_negative_counts` layer: window expiry, non-auto applied
supersession, and resolution clearing (memory_update / memory_verify
postdating the event).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.handlers.search import _active_negative_counts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _demotion_factor, search
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

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


# ---------------------------------------------------------------------------
# The factor itself
# ---------------------------------------------------------------------------


def test_demotion_factor_is_bounded_and_monotonic() -> None:
    assert _demotion_factor(0, 0) == 1.0
    assert _demotion_factor(-5, -5) == 1.0  # defensive: negatives are neutral
    prev = 1.0
    for n in (1, 3, 10, 100, 10_000):
        f = _demotion_factor(n, 0)
        assert f < prev, f"factor not decreasing at ignored={n}"
        assert 0.85 <= f < 1.0, f"factor {f} out of bounds at ignored={n}"
        prev = f


def test_demotion_contradicted_outweighs_ignored() -> None:
    """One contradicted (the stored claim was WRONG) demotes more than one
    ignored (often the query's fault), and exactly as much as two
    ignored — the documented 2x weighting."""
    assert _demotion_factor(0, 1) < _demotion_factor(1, 0)
    assert _demotion_factor(0, 1) == _demotion_factor(2, 0)


def test_demotion_breaks_a_tie() -> None:
    """Two memories that tie on relevance + recency: an active negative
    count flips the result away from the rejected one."""
    a = _memory("alpha beta gamma")
    b = _memory("alpha beta gamma")
    mems = [a, b]

    default_winner = search(mems, "alpha beta gamma", mode="keyword")[0].id

    demoted = search(
        mems,
        "alpha beta gamma",
        mode="keyword",
        negative_by_id={default_winner: (1, 0)},
    )
    assert demoted[0].id != default_winner, "demotion should flip the tie"


def test_demotion_cannot_bury_relevance() -> None:
    """The -15% floor means a heavily-rejected strong match still beats a
    weak match — demotion is a tie-breaker, not an eraser."""
    strong = _memory("alpha alpha alpha alpha alpha")
    weak = _memory("alpha lone unrelated body text")
    hits = search(
        [strong, weak],
        "alpha",
        mode="keyword",
        negative_by_id={strong.id: (100_000, 100_000)},
    )
    assert hits[0].id == strong.id


def test_demotion_unset_is_neutral() -> None:
    """negative_by_id=None (the default) must produce the identical ranking
    to passing it explicitly empty — the no-op guarantee that keeps the
    shipped default byte-stable."""
    mems = [_memory("alpha beta"), _memory("alpha gamma"), _memory("alpha delta")]
    a = [h.id for h in search(mems, "alpha", mode="hybrid")]
    b = [h.id for h in search(mems, "alpha", mode="hybrid", negative_by_id=None)]
    c = [h.id for h in search(mems, "alpha", mode="hybrid", negative_by_id={})]
    assert a == b == c


def test_demotion_composes_with_endorsement() -> None:
    """Endorsement on one memory + demotion on the other move a tie in
    the same direction — the two factors compose multiplicatively and
    never cancel each other's sign."""
    a = _memory("alpha beta gamma")
    b = _memory("alpha beta gamma")
    hits = search(
        [a, b],
        "alpha beta gamma",
        mode="keyword",
        applied_by_id={a.id: 5},
        negative_by_id={b.id: (0, 2)},
    )
    assert hits[0].id == a.id


# ---------------------------------------------------------------------------
# _active_negative_counts: the "active" semantics
# ---------------------------------------------------------------------------


def _ts(now: datetime, *, ago_days: float = 0.0, ago_seconds: float = 0.0) -> str:
    delta = timedelta(days=ago_days, seconds=ago_seconds)
    return (now - delta).isoformat().replace("+00:00", "Z")


_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)  # resolution floor, pre-window


def _use(
    ts: str, outcome: str, ids: list[str], *, auto: bool = False
) -> dict[str, Any]:
    return {"ts": ts, "kind": "use", "outcome": outcome, "auto": auto, "ids": ids}


def test_counts_window_expiry_and_kind_filter() -> None:
    events = [
        _use(_ts(_NOW, ago_days=31), "ignored", ["m1"]),  # aged out
        _use(_ts(_NOW, ago_days=2), "ignored", ["m1"]),
        _use(_ts(_NOW, ago_days=2), "corrected", ["m1"]),  # audit-only
        {"ts": _ts(_NOW, ago_days=1), "kind": "search", "returned": ["m1"]},
        _use(_ts(_NOW, ago_days=1), "contradicted", ["m2", "off"]),
        _use(_ts(_NOW, ago_days=1), "contradicted", ["off"]),  # off-set id
    ]
    counts = _active_negative_counts(
        events,
        {"m1", "m2"},
        now=_NOW,
        window_days=30,
        resolution_ts_by_id={"m1": _OLD, "m2": _OLD},
    )
    assert counts == {"m1": (1, 0), "m2": (0, 1)}


def test_counts_nonauto_applied_supersedes_but_auto_does_not() -> None:
    events = [
        _use(_ts(_NOW, ago_days=5), "ignored", ["m1"]),
        _use(_ts(_NOW, ago_days=4), "applied", ["m1"]),  # genuine → clears
        _use(_ts(_NOW, ago_days=5), "contradicted", ["m2"]),
        _use(_ts(_NOW, ago_days=4), "applied", ["m2"], auto=True),  # no judgment
        # order matters: a negative AFTER the clearing applied re-counts
        _use(_ts(_NOW, ago_days=3), "ignored", ["m1"]),
    ]
    counts = _active_negative_counts(
        events,
        {"m1", "m2"},
        now=_NOW,
        window_days=30,
        resolution_ts_by_id={"m1": _OLD, "m2": _OLD},
    )
    assert counts == {"m1": (1, 0), "m2": (0, 1)}


def test_counts_resolution_timestamp_clears() -> None:
    """A negative at or before max(updated, last_verified_at) judged a body
    that has since been rewritten or re-attested — it no longer counts.
    Negatives after the resolution still do."""
    resolution = _NOW - timedelta(days=3)
    events = [
        _use(_ts(_NOW, ago_days=5), "contradicted", ["m1"]),  # pre-resolution
        _use(_ts(_NOW, ago_days=1), "ignored", ["m1"]),  # post-resolution
        _use(_ts(_NOW, ago_days=5), "contradicted", ["m2"]),  # pre, never fixed
    ]
    counts = _active_negative_counts(
        events,
        {"m1", "m2"},
        now=_NOW,
        window_days=30,
        resolution_ts_by_id={"m1": resolution, "m2": _OLD},
    )
    assert counts == {"m1": (1, 0), "m2": (0, 1)}


def test_counts_sparse_and_legacy_ids_key() -> None:
    events = [
        {
            "ts": _ts(_NOW, ago_days=1),
            "kind": "use",
            "outcome": "ignored",
            "memory_ids": ["m1"],  # legacy field name
        },
        _use(_ts(_NOW, ago_days=1), "applied", ["m2"]),
    ]
    counts = _active_negative_counts(
        events,
        {"m1", "m2"},
        now=_NOW,
        window_days=30,
        resolution_ts_by_id={"m1": _OLD, "m2": _OLD},
    )
    assert counts == {"m1": (1, 0)}  # m2 absent, not (0, 0)


# ---------------------------------------------------------------------------
# End-to-end through the server (flag on) + byte-stability (flag off)
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture(autouse=True)
def _no_semantic_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests on the two lexical legs, whatever is installed.

    They turn on a bounded (<=10%) demotion factor and assert it flips a
    NEAR-TIE, which only means anything against a fixed set of ranking
    legs. Once `hybrid` began resolving a semantic model from a
    merely-installed extra, the fused order changed and the pair stopped
    being tied — so the suite passed or failed on whether the machine had
    the extra. Pinning the mode instead would swap the scorer these ties
    were tuned against; pinning importability keeps the exact ranking and
    removes only the new leg.
    """
    monkeypatch.setattr(
        "bettermemory.semantic_setup._embeddings_extra_importable", lambda: False
    )


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


async def _seed(server: Any, body: str, *, force: bool = False) -> str:
    res = await _call(
        server, "memory_write", content=body, scopes=["tools"], force=force
    )
    assert res.get("id"), f"seed write failed: {res}"
    return res["id"]


async def test_e2e_ignored_demotes_score(memory_dir: Path) -> None:
    # mode="keyword": hybrid scores are RRF outputs (rank-only), so a
    # single-memory demotion is invisible there by design — the raw
    # scorer surfaces the factor directly.
    server = _build(memory_dir, outcome_demotion=True)
    mid = await _seed(server, "python list comprehension notes")

    before = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    after = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )

    assert after[0]["id"] == mid
    assert after[0]["score"] < before[0]["score"], (
        "an active ignored outcome must lower the score with outcome_demotion on"
    )


async def test_e2e_flag_off_is_byte_stable(memory_dir: Path) -> None:
    server = _build(memory_dir)  # shipped default: no demotion
    mid = await _seed(server, "python list comprehension notes")

    before = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    after = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )

    assert after[0]["score"] == before[0]["score"], (
        "with the flag off, negative outcomes must not touch ranking"
    )
    # The annotation still surfaces — informational path is flag-independent.
    assert "recent_negative_outcomes" in after[0]


async def test_e2e_tie_flip_and_update_clears(memory_dir: Path) -> None:
    """A rejected near-tie loses its rank; fixing the memory via
    memory_update makes the rejection stop counting (the judged body no
    longer exists) and the demotion lifts."""
    server = _build(memory_dir, outcome_demotion=True)
    a = await _seed(server, "alpha beta gamma delta workflow")
    await _seed(server, "alpha beta gamma epsilon pipeline", force=True)

    first = _unwrap(await _call(server, "memory_search", query="alpha beta gamma"))
    top = first[0]["id"]

    await _call(
        server,
        "memory_record_use",
        memory_ids=[top],
        outcome="contradicted",
        note="stale claim",
    )
    flipped = _unwrap(await _call(server, "memory_search", query="alpha beta gamma"))
    assert flipped[0]["id"] != top, "contradicted near-tie should lose the top slot"

    # Fix the contradicted memory: updated now postdates the negative
    # event, so the demotion clears and its score recovers to at least
    # the untouched sibling's class (exact rank depends on recency).
    fixed_body = (
        "alpha beta gamma delta workflow"
        if top == a
        else "alpha beta gamma epsilon pipeline"
    )
    await _call(server, "memory_update", id=top, content=fixed_body + " v2")
    recovered = _unwrap(await _call(server, "memory_search", query="alpha beta gamma"))
    top_hit = next(h for h in recovered if h["id"] == top)
    assert (
        "recent_negative_outcomes" not in top_hit or top_hit["recent_negative_outcomes"]
    ), "sanity: hit shape intact"
    assert recovered[0]["id"] == top, (
        "after memory_update the negative no longer counts and the "
        "fresher memory should retake the tie"
    )


async def test_e2e_applied_supersedes_demotion(memory_dir: Path) -> None:
    server = _build(memory_dir, outcome_demotion=True)
    mid = await _seed(server, "python list comprehension notes")

    baseline = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")
    after = _unwrap(
        await _call(server, "memory_search", query="python", mode="keyword")
    )

    assert after[0]["score"] == pytest.approx(baseline[0]["score"]), (
        "a genuine applied after the rejection re-validates the memory; "
        "the demotion must fully clear"
    )


async def test_e2e_event_read_window_widens_only_with_flag(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler's one event read serves endorsement (600s attribution),
    demotion (30d), and the annotation. Contract: the requested window is
    600s with only endorsement on, and the full negative window once
    demotion is on — which is also what upgrades the annotation's 30-day
    contract from best-effort to guaranteed."""
    import bettermemory.events as events_mod

    requested: list[int] = []
    real = events_mod.iter_events_window

    def spy(root: Path, window_seconds: int, **kwargs: Any) -> Any:
        requested.append(window_seconds)
        return real(root, window_seconds, **kwargs)

    monkeypatch.setattr(events_mod, "iter_events_window", spy)

    server = _build(memory_dir, endorsement_boost=True)
    await _seed(server, "python list comprehension notes")
    _unwrap(await _call(server, "memory_search", query="python"))
    assert requested and max(requested) == 600

    requested.clear()
    server2 = _build(memory_dir, endorsement_boost=True, outcome_demotion=True)
    _unwrap(await _call(server2, "memory_search", query="python"))
    assert requested and max(requested) == 30 * 86400
