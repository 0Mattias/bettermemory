"""G3: episode-volume visibility in the health report.

Episode GC (`Store.prune_episode_sessions`, 30-day TTL) fires ONLY on
`episode(action="write")` and `bettermemory episodes prune`. A read-only
loop, one that calls the handoff every tick and never writes, therefore
never collects, and the journal grows with no surface reporting it.
`episode_volume` on the health report is that report.

The two failure modes this file exists to catch, in order:

1. **Ships as a permanent null.** `compute_health` takes a memory list
   and an event iterable and never sees the store, so wiring the gauge
   there would leave `episode_volume is None` on every production path
   while every hand-built unit fixture passed. The tool-level test drives
   `memory_admin(action="health")` after a real episode write for exactly
   that reason.
2. **Buys visibility by adding a read to a hot path.** The AC is "no new
   read on the hot path", and the counter tests below are the proof: the
   per-turn surfaces must not read the gauge, and the source-level guards
   pin both the caller set of `report_for_store` and the fact that
   `episode_volume()` has exactly one call site in the package.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import store as store_module
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import report_for_store
from bettermemory.models import utcnow
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import DEFAULT_EPISODE_TTL_DAYS, EpisodeVolume, Store

from ._mcp import call_tool as _mcp_call


@pytest.fixture
def server(store: Store) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(store.path.parent)))
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id)
    return build_server(config=cfg, store=store, state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


def _days_ago(days: int) -> Any:
    return utcnow() - timedelta(days=days)


def _stored_bytes(store: Store) -> int:
    """The gauge's own definition, computed from the rows: the UTF-8 size
    of every body and takeaway."""
    total = 0
    for sid in store.episode_session_ids():
        for episode in store.episodes_by_session(sid):
            total += len(episode.body.encode("utf-8"))
            total += len((episode.takeaway or "").encode("utf-8"))
    return total


# ---------------------------------------------------------------------------
# The gauge itself
# ---------------------------------------------------------------------------


def test_episode_volume_counts_sessions_episodes_and_bytes(store: Store) -> None:
    """`episode_volume()` reports the journal's real shape: sessions,
    episode rows, and their summed body and takeaway size."""
    store.write_episode(session_id="sess_a", body="first body")
    store.write_episode(session_id="sess_a", body="second body", takeaway="two")
    store.write_episode(session_id="sess_b", body="third body")

    vol = store.episode_volume()

    assert isinstance(vol, EpisodeVolume)
    assert vol.sessions == 2
    assert vol.episodes == 3
    assert vol.ttl_days == DEFAULT_EPISODE_TTL_DAYS
    assert _stored_bytes(store) > 0
    assert vol.bytes == _stored_bytes(store)


def test_episode_volume_is_all_zero_on_a_fresh_store(store: Store) -> None:
    """A store that never wrote an episode reports zeroes rather than
    crashing the whole health report on a fresh install."""
    vol = store.episode_volume()

    assert (vol.sessions, vol.episodes, vol.bytes, vol.prunable_sessions) == (
        0,
        0,
        0,
        0,
    )
    # Still self-describing: a consumer reading `prunable_sessions == 0`
    # needs to know which TTL produced that zero.
    assert vol.ttl_days == DEFAULT_EPISODE_TTL_DAYS


def test_prunable_sessions_predicts_exactly_what_prune_collects(
    store: Store,
) -> None:
    """The load-bearing parity: `prunable_sessions` is only useful if it
    agrees with the GC it is predicting.

    `episode_volume()` must not carry its own transcription of the TTL
    rule; that is how the CLI dry-run and the store drifted apart before,
    and a third copy for the health rollup would have made it worse.
    Seed the cases `prune_episode_sessions` distinguishes (fresh, past
    the TTL, a second past-TTL session), snapshot the prediction, then
    run the real prune and compare.
    """
    store.write_episode(session_id="sess_fresh", body="written just now")
    store.write_episode(session_id="sess_old_one", body="ancient", now=_days_ago(40))
    store.write_episode(
        session_id="sess_old_two", body="also ancient", now=_days_ago(99)
    )

    predicted_ids = set(store.prunable_episode_sessions())
    predicted_count = store.episode_volume().prunable_sessions

    assert predicted_count == len(predicted_ids)

    actually_pruned = set(store.prune_episode_sessions())

    assert predicted_ids == actually_pruned
    assert predicted_ids == {"sess_old_one", "sess_old_two"}
    assert store.episodes_by_session("sess_fresh")

    # And the gauge tracks the deletion: post-prune the store is two
    # sessions lighter with nothing left to collect.
    after = store.episode_volume()
    assert after.sessions == 1
    assert after.episodes == 1
    assert after.prunable_sessions == 0


def test_prunable_sessions_is_zero_for_a_non_positive_ttl(store: Store) -> None:
    """`prune_episode_sessions` early-returns [] for `ttl_days <= 0`: a
    non-positive TTL is a no-op, never "collect everything". The gauge
    mirrors that guard, or it would report a whole store as collectable
    while a real prune deletes nothing."""
    store.write_episode(session_id="sess_ancient", body="ancient", now=_days_ago(400))

    assert store.prunable_episode_sessions(ttl_days=0) == []
    assert store.episode_volume(ttl_days=0).prunable_sessions == 0
    assert store.prune_episode_sessions(ttl_days=0) == []
    assert store.episode_volume(ttl_days=30).prunable_sessions == 1


def test_health_render_text_surfaces_collectable_sessions(store: Store) -> None:
    """`bettermemory health` prints the gauge, and names the prune command
    when there is something to collect: the CLI half of "make journal
    growth visible"."""
    from bettermemory.health import render_text

    store.write_episode(session_id="sess_fresh", body="recent")
    store.write_episode(session_id="sess_stale", body="ancient", now=_days_ago(60))

    text = render_text(report_for_store(store))

    assert "Episodes:" in text
    assert "2 in 2 sessions" in text
    assert "past the 30-day TTL" in text
    assert "bettermemory episodes prune" in text

    # Nothing collectable -> no call to action, just the size line.
    store.prune_episode_sessions()
    clean = render_text(report_for_store(store))
    assert "Episodes:" in clean
    assert "bettermemory episodes prune" not in clean


# ---------------------------------------------------------------------------
# Wiring: the "ships as a permanent null" guard
# ---------------------------------------------------------------------------


async def test_memory_health_reports_episode_volume_through_the_tool(
    server: Any,
) -> None:
    """The anti-inert control: drive the real MCP tool after a real
    episode write and read the number off the wire.

    A `compute_health` unit test proves nothing here: `compute_health`
    never receives the store, so a gauge wired there is `None` on every
    production path with every hand-built fixture still green.
    """
    written = await _call(
        server, "episode", action="write", body="an iteration takeaway"
    )
    assert written["status"] == "committed"

    report = await _call(server, "memory_admin", action="health")

    assert "episode_volume" in report, (
        "the health report carries no `episode_volume` key; the rollup is "
        "not on the wire."
    )
    volume = report["episode_volume"]
    assert volume is not None, (
        "`episode_volume` came back null from the live MCP tool after a "
        "real episode write. That is the signature of wiring the gauge "
        "into `compute_health` (which never sees the store) instead of "
        "`report_for_store`."
    )
    assert volume["episodes"] >= 1
    assert volume["sessions"] >= 1
    assert volume["bytes"] > 0
    assert volume["prunable_sessions"] == 0
    assert volume["ttl_days"] == DEFAULT_EPISODE_TTL_DAYS


def test_report_for_store_populates_the_gauge_and_compute_health_does_not(
    store: Store,
) -> None:
    """Both halves of the contract in one place: the entry point that has
    the store populates the field; the pure function that does not,
    leaves it None for offline tooling and unit fixtures."""
    from bettermemory.health import compute_health

    store.write_episode(session_id="sess_x", body="journal entry")

    assert report_for_store(store).episode_volume is not None
    assert compute_health([], iter(())).episode_volume is None


def test_report_for_store_does_not_materialise_episodes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attaching the gauge must not make the health report load episode
    rows. If this ever fails, someone reached for `episodes_by_session`."""
    for i in range(4):
        store.write_episode(session_id=f"sess_{i}", body=f"body {i}")

    loads: list[Any] = []
    original = store_module._row_to_episode

    def _spy(row: Any) -> Any:
        loads.append(row)
        return original(row)

    monkeypatch.setattr(store_module, "_row_to_episode", _spy)
    report = report_for_store(store)

    assert report.episode_volume is not None
    assert report.episode_volume.episodes == 4
    assert loads == []


# ---------------------------------------------------------------------------
# "No new read on the hot path": the AC, proved two ways
# ---------------------------------------------------------------------------


async def test_no_per_turn_tool_reads_the_episode_volume(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural half of the AC.

    Count every entry into the gauge, then drive the surfaces a session
    actually calls per turn, including both episode actions. None of
    them may trip the counter. The health report is the positive
    control: without it a counter that never fires would prove only that
    the patch missed.
    """
    reads: list[str] = []
    original_volume = Store.episode_volume

    def _volume_spy(self: Store, **kwargs: Any) -> Any:
        reads.append("episode_volume")
        return original_volume(self, **kwargs)

    monkeypatch.setattr(Store, "episode_volume", _volume_spy)

    await _call(
        server,
        "memory_write",
        content="a durable project fact",
        scopes=["projects:demo"],
    )
    await _call(server, "memory_search", query="durable")
    await _call(server, "episode", action="write", body="iteration state")
    await _call(server, "episode", action="handoff")

    assert reads == [], (
        "a per-turn tool read the episode volume gauge. "
        f"Entries: {reads}. The gauge belongs to the health report only; "
        "the AC for this feature is that visibility costs nothing on the "
        "hot path."
    )

    await _call(server, "memory_admin", action="health")

    assert "episode_volume" in reads, (
        "the health report did not read the gauge either; the counter "
        "above proved nothing. Check the monkeypatch target."
    )


def test_the_volume_gauge_has_exactly_one_call_site_in_the_package() -> None:
    """Structural half of the AC: the call graph, pinned.

    A behavioural counter only covers the tools this file happens to
    drive. This one covers the whole package: `episode_volume()` may be
    called from `health.py` and nowhere else, so no future handler, hook
    or CLI fast path can quietly acquire the read.

    `prunable_episode_sessions` is the shared TTL predicate: the store
    calls it from the gauge and from the prune it mirrors, and the
    episodes CLI from its dry run. None of those is a per-turn read.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    volume_callers = set()
    predicate_callers = set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(src))
        # An attribute call, so `def episode_volume(` (the definition,
        # inside store.py) does not register as a call site.
        if re.search(r"\.episode_volume\(", text):
            volume_callers.add(rel)
        if re.search(r"\.prunable_episode_sessions\(", text):
            predicate_callers.add(rel)

    assert volume_callers == {"health.py"}, (
        "Store.episode_volume() gained a call site outside health.py: "
        f"{sorted(volume_callers)}. Every other surface pays this read "
        "per turn; health does not."
    )
    assert predicate_callers == {"store.py", str(Path("cli") / "episodes.py")}, (
        "the shared TTL predicate gained an unexpected caller: "
        f"{sorted(predicate_callers)}."
    )


def test_report_for_store_callers_are_the_curation_surfaces() -> None:
    """The gauge's cost is bounded by who calls `report_for_store`.

    Both are deliberate curation passes: `memory_admin(action="health")`
    and `bettermemory health`. Nothing a session calls every turn.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    callers = set()
    for path in src.rglob("*.py"):
        rel = str(path.relative_to(src))
        if rel == "health.py":
            continue  # the definition
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if "report_for_store(" in line:
                callers.add(rel)
                break

    assert callers == {
        str(Path("handlers") / "health.py"),
        str(Path("cli") / "health_cmd.py"),
    }, (
        "the caller set of `report_for_store` changed: "
        f"{sorted(callers)}. It carries the episode volume read; a new "
        "caller on a per-turn path silently re-prices every turn."
    )
