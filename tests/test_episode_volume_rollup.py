"""G3 — episode-volume visibility in `memory_health`.

Episode walks are O(total episodes) and episode GC
(`EpisodeStore.prune_old_sessions`, 30-day TTL) fires ONLY on
`episode_write` and `bettermemory episodes prune`. A read-only loop —
one that calls `episode_handoff` / `episode_search` every tick and never
writes — therefore never collects, and the journal subtree grows with no
surface reporting it. `memory_health.episode_volume` is that report.

The two failure modes this file exists to catch, in order:

1. **Ships as a permanent null.** `compute_health` takes a memory list
   and an event iterable and never sees `root`, so wiring the gauge
   there would leave `episode_volume is None` on every production path
   while every hand-built unit fixture passed. The tool-level test drives
   `server.call_tool("memory_health")` after a real `episode_write` for
   exactly that reason.
2. **Buys visibility by adding a walk to a hot path.** The AC is
   "no new walk on the hot path", and the counter tests below are the
   proof: the per-turn surfaces must not touch the episode subtree, and
   the source-level guards pin both the caller set of
   `report_for_directory` and the fact that `volume()` has exactly one
   call site in the package.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.episodes import (
    DEFAULT_EPISODE_TTL_DAYS,
    EpisodeStore,
    EpisodeVolume,
)
from bettermemory.events import Recorder
from bettermemory.health import report_for_directory
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


_DAY = 24 * 60 * 60


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    return build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _backdate(session_dir: Path, *, days_old: int) -> None:
    past = time.time() - (days_old * _DAY)
    for entry in session_dir.iterdir():
        if entry.is_file():
            os.utime(entry, (past, past))


# ---------------------------------------------------------------------------
# The gauge itself
# ---------------------------------------------------------------------------


def test_episode_volume_counts_sessions_episodes_and_bytes(
    memory_dir: Path,
) -> None:
    """`volume()` reports the subtree's real shape: session directories,
    `.md` episode files, and their summed on-disk size."""
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_a", body="first body")
    store.write(session_id="sess_a", body="second body")
    store.write(session_id="sess_b", body="third body")

    vol = store.volume()

    assert isinstance(vol, EpisodeVolume)
    assert vol.sessions == 2
    assert vol.episodes == 3
    assert vol.ttl_days == DEFAULT_EPISODE_TTL_DAYS
    on_disk = sum(
        p.stat().st_size for p in store.episodes_dir.rglob("*.md") if p.is_file()
    )
    assert on_disk > 0
    assert vol.bytes == on_disk


def test_episode_volume_is_all_zero_before_the_subtree_exists(
    memory_dir: Path,
) -> None:
    """A store that never wrote an episode has no `episodes/` directory at
    all (it is created lazily on first write). The gauge must report zeroes
    rather than crashing the whole health report on a fresh install."""
    store = EpisodeStore(memory_dir)
    assert not store.episodes_dir.exists()

    vol = store.volume()

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
    memory_dir: Path,
) -> None:
    """The load-bearing parity: `prunable_sessions` is only useful if it
    agrees with the GC it is predicting.

    `volume()` must not carry its own transcription of the TTL rule —
    that is how the CLI dry-run and the store drifted apart before, and a
    third copy for the health rollup would have made it worse. Seed all
    four cases `prune_old_sessions` distinguishes (fresh, past-TTL, an
    empty directory it also reclaims, and a second past-TTL session),
    snapshot the prediction, then run the real prune and compare.
    """
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_fresh", body="written just now")
    store.write(session_id="sess_old_one", body="ancient")
    store.write(session_id="sess_old_two", body="also ancient")
    _backdate(store.episodes_dir / "sess_old_one", days_old=40)
    _backdate(store.episodes_dir / "sess_old_two", days_old=99)
    # An empty session directory — `prune_old_sessions` reclaims these
    # regardless of mtime, so the gauge has to count them too.
    (store.episodes_dir / "sess_empty").mkdir()

    predicted_ids = set(store.prunable_session_ids())
    predicted_count = store.volume().prunable_sessions

    assert predicted_count == len(predicted_ids)

    actually_pruned = set(store.prune_old_sessions())

    assert predicted_ids == actually_pruned
    assert predicted_ids == {"sess_old_one", "sess_old_two", "sess_empty"}
    assert (store.episodes_dir / "sess_fresh").exists()

    # And the gauge tracks the deletion: post-prune the store is one
    # session lighter with nothing left to collect.
    after = store.volume()
    assert after.sessions == 1
    assert after.episodes == 1
    assert after.prunable_sessions == 0


def test_prunable_sessions_is_zero_for_a_non_positive_ttl(
    memory_dir: Path,
) -> None:
    """`prune_old_sessions` early-returns [] for `ttl_days <= 0` — a
    non-positive TTL is a no-op, never "collect everything". The gauge
    mirrors that guard, or it would report a whole store as collectable
    while a real prune deletes nothing."""
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_ancient", body="ancient")
    _backdate(store.episodes_dir / "sess_ancient", days_old=400)

    assert store.prunable_session_ids(ttl_days=0) == []
    assert store.volume(ttl_days=0).prunable_sessions == 0
    assert store.prune_old_sessions(ttl_days=0) == []
    assert store.volume(ttl_days=30).prunable_sessions == 1


def test_volume_skips_symlinks_exactly_like_the_prune_walk(
    memory_dir: Path, tmp_path: Path
) -> None:
    """The gauge resolves "regular file, not a symlink" with one `lstat`
    + `S_ISREG` where `_newest_mtime_in_dir` spells it `is_file() and not
    is_symlink()`. Those must stay the same predicate: a plain `stat()`
    follows the link, so a symlink into a hot file outside the subtree
    would both inflate `bytes` and hold the session off the TTL forever
    while the real prune collected it anyway.
    """
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_link", body="the only real episode")
    session_dir = store.episodes_dir / "sess_link"
    real_bytes = sum(p.stat().st_size for p in session_dir.iterdir())

    outsider = tmp_path / "outside.md"
    outsider.write_text("x" * 100_000)
    # `.md`-suffixed and freshly-touched: maximally tempting to both the
    # episode count and the mtime cutoff.
    (session_dir / "zzz_link.md").symlink_to(outsider)
    _backdate(session_dir, days_old=90)

    vol = store.volume()
    assert vol.episodes == 1, "a symlink was counted as an episode"
    assert vol.bytes == real_bytes, "a symlink's target was counted in bytes"
    assert vol.prunable_sessions == 1

    # ...and the GC agrees, which is the point.
    assert store.prune_old_sessions() == ["sess_link"]


def test_volume_never_parses_episode_frontmatter(memory_dir: Path) -> None:
    """The gauge is stat-only. `list_by_session` frontmatter-parses every
    file it touches; doing that here would turn a cheap size reading into
    a full journal parse on a surface that only wants a number."""
    store = EpisodeStore(memory_dir)
    for i in range(5):
        store.write(session_id=f"sess_{i}", body=f"body {i}")

    loads: list[Path] = []
    original = EpisodeStore._load_path

    def _spy(self: EpisodeStore, path: Path) -> Any:
        loads.append(path)
        return original(self, path)

    EpisodeStore._load_path = _spy  # type: ignore[method-assign]
    try:
        vol = store.volume()
    finally:
        EpisodeStore._load_path = original  # type: ignore[method-assign]

    assert vol.episodes == 5
    assert loads == [], (
        "EpisodeStore.volume() parsed episode frontmatter — it must read "
        f"nothing but directory entries and stat results. Parsed: {loads}"
    )


def test_volume_survives_a_session_directory_vanishing_mid_walk(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only gauge must not be able to crash the whole health
    report. Both walks race real mutation — a peer `episode_write`'s
    prune can `rmtree` a session directory, and an individual file can go
    between the listing and the stat — so each degrades to "nothing here"
    the way `_newest_mtime_in_dir` does on OSError.
    """
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_gone", body="about to vanish")
    store.write(session_id="sess_here", body="survives")

    real_iterdir = Path.iterdir

    def _iterdir(self: Path) -> Any:
        if self.name == "sess_gone":
            raise OSError("vanished between listings")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    vol = store.volume()
    monkeypatch.undo()

    # The vanished session still counts as a directory we saw, with
    # nothing in it — and an empty session is collectable, which is what
    # `prune_old_sessions` would conclude about it too.
    assert vol.sessions == 2
    assert vol.episodes == 1
    assert vol.prunable_sessions == 1


def test_volume_skips_a_file_that_disappears_between_listing_and_stat(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-entry half of the same race: the directory listing succeeds,
    then one file is gone by the time we stat it."""
    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_a", body="one")
    store.write(session_id="sess_a", body="two")
    doomed = sorted((store.episodes_dir / "sess_a").iterdir())[0].name

    real_lstat = Path.lstat

    def _lstat(self: Path, **kwargs: Any) -> Any:
        if self.name == doomed:
            raise OSError("unlinked under us")
        return real_lstat(self, **kwargs)

    monkeypatch.setattr(Path, "lstat", _lstat)
    vol = store.volume()
    monkeypatch.undo()

    assert vol.sessions == 1
    assert vol.episodes == 1


def test_health_render_text_surfaces_collectable_sessions(
    memory_dir: Path,
) -> None:
    """`bettermemory health` prints the gauge, and names the prune command
    when there is something to collect — the CLI half of "make journal
    growth visible"."""
    from bettermemory.health import render_text

    store = EpisodeStore(memory_dir)
    store.write(session_id="sess_fresh", body="recent")
    store.write(session_id="sess_stale", body="ancient")
    _backdate(store.episodes_dir / "sess_stale", days_old=60)

    text = render_text(report_for_directory(memory_dir))

    assert "Episodes:" in text
    assert "2 in 2 sessions" in text
    assert "past the 30-day TTL" in text
    assert "bettermemory episodes prune" in text

    # Nothing collectable -> no call to action, just the size line.
    store.prune_old_sessions()
    clean = render_text(report_for_directory(memory_dir))
    assert "Episodes:" in clean
    assert "bettermemory episodes prune" not in clean


# ---------------------------------------------------------------------------
# Wiring — the "ships as a permanent null" guard
# ---------------------------------------------------------------------------


async def test_memory_health_reports_episode_volume_through_the_tool(
    server: Any,
) -> None:
    """The anti-inert control: drive the real MCP tool after a real
    `episode_write` and read the number off the wire.

    A `compute_health` unit test proves nothing here — `compute_health`
    never receives `root`, so a gauge wired there is `None` on every
    production path with every hand-built fixture still green.
    """
    written = await _call(server, "episode_write", body="an iteration takeaway")
    assert written["status"] == "committed"

    report = await _call(server, "memory_health")

    assert "episode_volume" in report, (
        "memory_health returned no `episode_volume` key — the rollup is "
        "not on the wire."
    )
    volume = report["episode_volume"]
    assert volume is not None, (
        "`episode_volume` came back null from the live MCP tool after a "
        "real episode_write. That is the signature of wiring the gauge "
        "into `compute_health` (which never sees `root`) instead of "
        "`report_for_directory`."
    )
    assert volume["episodes"] >= 1
    assert volume["sessions"] >= 1
    assert volume["bytes"] > 0
    assert volume["prunable_sessions"] == 0
    assert volume["ttl_days"] == DEFAULT_EPISODE_TTL_DAYS


def test_report_for_directory_populates_the_gauge_and_compute_health_does_not(
    memory_dir: Path,
) -> None:
    """Both halves of the contract in one place: the entry point that has
    a `root` populates the field; the pure function that does not, leaves
    it None for offline tooling and unit fixtures."""
    from bettermemory.health import compute_health

    EpisodeStore(memory_dir).write(session_id="sess_x", body="journal entry")

    assert report_for_directory(memory_dir).episode_volume is not None
    assert compute_health([], iter(())).episode_volume is None


def test_report_for_directory_does_not_parse_the_journal(
    memory_dir: Path,
) -> None:
    """Attaching the gauge must not make `memory_health` read episode
    bodies. If this ever fails, someone reached for `list_by_session`."""
    store = EpisodeStore(memory_dir)
    for i in range(4):
        store.write(session_id=f"sess_{i}", body=f"body {i}")

    loads: list[Path] = []
    original = EpisodeStore._load_path

    def _spy(self: EpisodeStore, path: Path) -> Any:
        loads.append(path)
        return original(self, path)

    EpisodeStore._load_path = _spy  # type: ignore[method-assign]
    try:
        report = report_for_directory(memory_dir)
    finally:
        EpisodeStore._load_path = original  # type: ignore[method-assign]

    assert report.episode_volume is not None
    assert report.episode_volume.episodes == 4
    assert loads == []


# ---------------------------------------------------------------------------
# "No new walk on the hot path" — the AC, proved two ways
# ---------------------------------------------------------------------------


async def test_no_per_turn_tool_walks_the_episode_subtree(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural half of the AC.

    Count every entry into the subtree walk, then drive the surfaces a
    session actually calls per turn — including the session-start hot path
    (`memory_scope_overview`) and both episode reads. None of them may
    trip the counter. `memory_health` is the positive control: without it
    a counter that never fires would prove only that the patch missed.
    """
    walks: list[str] = []
    original_scan = EpisodeStore._scan_sessions
    original_volume = EpisodeStore.volume

    def _scan_spy(self: EpisodeStore) -> Any:
        walks.append("_scan_sessions")
        return original_scan(self)

    def _volume_spy(self: EpisodeStore, **kwargs: Any) -> Any:
        walks.append("volume")
        return original_volume(self, **kwargs)

    monkeypatch.setattr(EpisodeStore, "_scan_sessions", _scan_spy)
    monkeypatch.setattr(EpisodeStore, "volume", _volume_spy)

    await _call(
        server,
        "memory_write",
        content="a durable project fact",
        scopes=["projects:demo"],
    )
    await _call(server, "memory_search", query="durable")
    await _call(server, "memory_list")
    await _call(server, "memory_scope_overview")
    await _call(server, "episode_write", body="iteration state")
    await _call(server, "episode_handoff")
    await _call(server, "episode_search")

    assert walks == [], (
        "a per-turn tool walked the episode subtree for the volume gauge. "
        f"Entries: {walks}. The gauge belongs to `memory_health` only — "
        "the AC for this feature is that visibility costs nothing on the "
        "hot path."
    )

    await _call(server, "memory_health")

    assert "volume" in walks, (
        "memory_health did not walk the subtree either — the counter "
        "above proved nothing. Check the monkeypatch target."
    )


def test_the_volume_gauge_has_exactly_one_call_site_in_the_package() -> None:
    """Structural half of the AC — the call graph, pinned.

    A behavioural counter only covers the tools this file happens to
    drive. This one covers the whole package: `volume()` may be called
    from `health.py` and nowhere else, so no future handler, hook or CLI
    fast path can quietly acquire a subtree walk.

    `prunable_session_ids` is the shared TTL predicate and is expected in
    the episodes CLI too — that call site is a dry-run of the prune it
    mirrors, not a per-turn read.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    volume_callers = set()
    predicate_callers = set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(src))
        # An attribute call, so `def volume(` (the definition, inside
        # episodes.py) does not register as a call site.
        if re.search(r"\.volume\(", text):
            volume_callers.add(rel)
        if re.search(r"\.prunable_session_ids\(", text):
            predicate_callers.add(rel)

    assert volume_callers == {"health.py"}, (
        "EpisodeStore.volume() gained a call site outside health.py: "
        f"{sorted(volume_callers)}. Every other surface pays this walk "
        "per turn; health does not."
    )
    assert predicate_callers == {str(Path("cli") / "episodes.py")}, (
        "the shared TTL predicate gained an unexpected caller: "
        f"{sorted(predicate_callers)}."
    )


def test_report_for_directory_callers_are_the_three_curation_surfaces() -> None:
    """The gauge's cost is bounded by who calls `report_for_directory`.

    All three are deliberate curation passes — the `memory_health` MCP
    tool (whose own DESC says "don't call on every turn"), `bettermemory
    health`, and the local web dashboard. Notably NOT
    `memory_scope_overview`, which every session calls at start-up and
    which computes its `curation_pending` rollup without this function.
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
            if "report_for_directory(" in line:
                callers.add(rel)
                break

    assert callers == {
        str(Path("handlers") / "health.py"),
        str(Path("cli") / "health_cmd.py"),
        "web.py",
    }, (
        "the caller set of `report_for_directory` changed: "
        f"{sorted(callers)}. It now carries an episode-subtree walk — a "
        "new caller on a per-turn path silently re-prices every turn."
    )


# ---------------------------------------------------------------------------
# Prose — five surfaces claimed a blanket exclusion that is no longer true
# ---------------------------------------------------------------------------


def test_every_episode_surface_qualifies_the_memory_health_exclusion() -> None:
    """Episodes are excluded from `memory_health` — except now their
    aggregate volume is not.

    Five surfaces documented the old blanket claim and no test pinned any
    of them, which is precisely why the claim could go stale unnoticed.
    Each must now say `episode_volume` where it names `memory_health`.
    """
    root = Path(__file__).resolve().parents[1]
    surfaces = [
        root / "src" / "bettermemory" / "episodes.py",
        root / "src" / "bettermemory" / "handlers" / "episode_write.py",
        root / "src" / "bettermemory" / "handlers" / "episode_search.py",
        root / "docs" / "api.md",
        root / "plugin" / "skills" / "bettermemory" / "SKILL.md",
    ]
    missing = []
    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        assert "memory_health" in text, f"{path.name} no longer names memory_health"
        if "episode_volume" not in text:
            missing.append(str(path.relative_to(root)))

    assert missing == [], (
        "these surfaces still describe episodes as excluded from "
        f"memory_health without naming the volume exception: {missing}. "
        "memory_health reports `episode_volume` — the aggregate only, "
        "never episode content — and prose that omits it is now wrong."
    )
