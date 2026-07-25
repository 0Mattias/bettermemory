"""Cross-episode pattern candidates: detection, dismissal persistence,
and the episode_patterns promote/dismiss surface."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import bettermemory.patterns as patterns_mod
from bettermemory._fsutil import flock_excl as _real_flock_excl
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.episodes import EpisodeStore
from bettermemory.events import Recorder
from bettermemory.models import Episode, generate_ulid
from bettermemory.origin import Origin
from bettermemory.patterns import (
    PatternDismissals,
    clusterable_episodes,
    find_episode_patterns,
)
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

_T = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _episode(
    body: str,
    *,
    session: str,
    takeaway: str | None = None,
    is_floor: bool = False,
    offset_minutes: int = 0,
) -> Episode:
    return Episode(
        id=generate_ulid(),
        session_id=session,
        created=_T + timedelta(minutes=offset_minutes),
        body=body,
        takeaway=takeaway,
        is_floor=is_floor,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_pattern_needs_three_distinct_sessions() -> None:
    mk = lambda s, i: _episode(  # noqa: E731
        "the caddy reverse proxy dropped websocket connections again today",
        session=s,
        offset_minutes=i,
    )
    two_sessions = [mk("sess-a", 0), mk("sess-a", 1), mk("sess-b", 2)]
    assert find_episode_patterns(two_sessions) == []

    three_sessions = [mk("sess-a", 0), mk("sess-b", 1), mk("sess-c", 2)]
    patterns = find_episode_patterns(three_sessions)
    assert len(patterns) == 1
    p = patterns[0]
    assert len(p.episode_ids) == 3
    assert len(p.session_ids) == 3
    assert any("websocket" in t or "caddy" in t for t in p.terms)
    assert len(p.snippets) == 3


def test_floors_and_noise_are_excluded() -> None:
    eps = [
        _episode("", session="sess-a", is_floor=True),
        _episode("unrelated one-off note about lunch", session="sess-b"),
        _episode("another unrelated note about weather", session="sess-c"),
    ]
    assert find_episode_patterns(eps) == []


def test_ubiquitous_vocabulary_is_not_a_pattern() -> None:
    """A term in ~every episode is project vocabulary. Distinct topics
    that all mention it must not fuse into one mega-pattern via that
    term alone."""
    eps = []
    for i, (s, topic) in enumerate(
        [
            ("sess-a", "proxy websocket drops"),
            ("sess-b", "proxy websocket drops"),
            ("sess-c", "proxy websocket drops"),
            ("sess-d", "sqlite index rebuild slow"),
            ("sess-e", "sqlite index rebuild slow"),
            ("sess-f", "sqlite index rebuild slow"),
        ]
    ):
        eps.append(
            _episode(f"bettermemory work today: {topic}", session=s, offset_minutes=i)
        )
    patterns = find_episode_patterns(eps)
    # "bettermemory"/"today" span all six episodes (> 60% ubiquity) so the
    # clusters must come from the topic terms — two patterns, not one.
    assert len(patterns) == 2
    sizes = sorted(len(p.episode_ids) for p in patterns)
    assert sizes == [3, 3]


def test_pattern_id_is_member_stable() -> None:
    eps = [
        _episode(
            "nginx certificate renewal failed silently", session=s, offset_minutes=i
        )
        for i, s in enumerate(["sess-a", "sess-b", "sess-c"])
    ]
    a = find_episode_patterns(eps)[0]
    b = find_episode_patterns(list(reversed(eps)))[0]
    assert a.id == b.id

    # A new member episode changes the id — fresh evidence reopens a
    # dismissed pattern under a new identity.
    eps.append(_episode("nginx certificate renewal failed silently", session="sess-d"))
    c = find_episode_patterns(eps)[0]
    assert c.id != a.id


def test_dismissals_persist_and_gc(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    root.mkdir()
    d = PatternDismissals(root)
    d.dismiss("pat-abc", ["e1", "e2", "e3"])
    assert d.dismissed_ids({"e1", "zzz"}) == {"pat-abc"}
    # All members gone → the row GCs.
    assert d.dismissed_ids({"other"}) == set()
    assert d.load() == []


# ---------------------------------------------------------------------------
# End-to-end through the tool surface
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
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


async def _journal_across_sessions(
    memory_dir: Path, bodies: list[str], *, scopes: list[str] | None = None
) -> Any:
    """Write each body from a FRESH server (fresh SessionState → distinct
    session directory), mirroring how a real journal accumulates across
    days. Returns the last server for follow-up calls."""
    server = None
    for body in bodies:
        server = _build(memory_dir)
        kwargs: dict[str, Any] = {}
        if scopes is not None:
            kwargs["scopes"] = scopes
        await _call(
            server,
            "episode_write",
            body=body,
            takeaway=body.split(".")[0],
            **kwargs,
        )
    assert server is not None
    return server


async def test_e2e_list_promote_deletes_members(memory_dir: Path) -> None:
    body = (
        "the tailscale exit node dropped again mid-sync. Restarting tailscaled "
        "fixed it, same as last time."
    )
    server = await _journal_across_sessions(memory_dir, [body, body, body])

    listing = _unwrap(await _call(server, "episode_patterns"))
    assert listing["patterns"], f"expected a pattern, got {listing}"
    pattern = listing["patterns"][0]
    assert pattern["distinct_sessions"] == 3
    assert len(pattern["snippets"]) == 3

    promoted = _unwrap(
        await _call(
            server,
            "episode_patterns",
            promote=pattern["id"],
            body=(
                "The tailscale exit node recurrently drops mid-sync; "
                "restarting tailscaled recovers it."
            ),
            scopes=["infrastructure"],
        )
    )
    assert promoted["status"] == "committed", promoted
    assert promoted["promoted_from_pattern"] == pattern["id"]
    assert promoted["episodes_deleted"] == 3

    # Members are gone → the pattern no longer lists.
    after = _unwrap(await _call(server, "episode_patterns"))
    assert after["patterns"] == []

    # And the durable memory is retrievable.
    hits = _unwrap(await _call(server, "memory_search", query="tailscale exit node"))
    assert hits and hits[0]["id"] == promoted["id"]


async def test_e2e_dismiss_is_sticky_until_new_evidence(memory_dir: Path) -> None:
    body = "the restic prune job overlapped the snapshot window again"
    server = await _journal_across_sessions(memory_dir, [body, body, body])

    listing = _unwrap(await _call(server, "episode_patterns"))
    pid = listing["patterns"][0]["id"]
    out = _unwrap(await _call(server, "episode_patterns", dismiss=pid))
    assert out["dismissed"] == pid

    again = _unwrap(await _call(server, "episode_patterns"))
    assert again["patterns"] == []

    # A fourth session journaling the same theme = new member set = new
    # id → legitimately resurfaces.
    server = await _journal_across_sessions(memory_dir, [body])
    fresh = _unwrap(await _call(server, "episode_patterns"))
    assert fresh["patterns"] and fresh["patterns"][0]["id"] != pid


async def test_e2e_promote_requires_authored_body(memory_dir: Path) -> None:
    body = "the diun watcher flagged a stale grafana image once more"
    server = await _journal_across_sessions(memory_dir, [body, body, body])
    listing = _unwrap(await _call(server, "episode_patterns"))
    pid = listing["patterns"][0]["id"]
    with pytest.raises(Exception):
        await _call(server, "episode_patterns", promote=pid, scopes=["infrastructure"])
    with pytest.raises(Exception):
        await _call(server, "episode_patterns", promote=pid, body="x y z")


def _blank_episode_body(memory_dir: Path, body_marker: str) -> str:
    """Blank the body of the episode whose file contains `body_marker`,
    leaving a loadable file with an EMPTY body — the shape a legacy or
    external writer leaves behind (`EpisodeStore.write` itself refuses an
    empty body). Returns its id. Frontmatter round-trip, the same
    technique `tests/test_episodes.py::_corrupt_scopes_to_scalar` uses."""
    from bettermemory import _frontmatter as frontmatter

    target = next(
        p
        for p in (memory_dir / "episodes").rglob("*.md")
        if body_marker in p.read_text(encoding="utf-8")
    )
    post = frontmatter.load(target)
    post.content = ""
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return str(post.metadata["id"])


async def test_patterns_listing_separates_scanned_from_clustered(
    memory_dir: Path,
) -> None:
    """`episodes_scanned` is the VISIBLE pool; `episodes_clustered` is
    what detection actually ran over. They are different numbers whenever
    the pool holds a floor or an empty-body episode, and the response
    used to publish only the first one under the second one's
    description — overstating the evidence base in any store where
    `episode_handoff` has ever written a session-tag anchor.

    Fixture makes the gap real: four themed episodes, one of them blanked
    (empty body), plus a floor. Pool 5, clustered 3, pattern of 3."""
    body = "the tailscale exit node dropped again mid-sync"
    await _journal_across_sessions(memory_dir, [body] * 3)
    # A fourth session journals a body that is uniquely markable, then
    # gets blanked — so it stays in the pool but leaves the cluster.
    await _journal_across_sessions(memory_dir, [f"{body} plus a doomed marker"])
    blanked = _blank_episode_body(memory_dir, "doomed marker")

    store = EpisodeStore(memory_dir)
    floor = store.write_floor(session_id=sorted(store.iter_session_ids())[0])

    on_disk = _episode_ids_on_disk(memory_dir)
    assert len(on_disk) == 5
    assert {blanked, floor.id} <= on_disk

    listing = _unwrap(await _call(_build(memory_dir), "episode_patterns"))
    assert listing["episodes_scanned"] == 5, (
        f"the floor and the blanked episode were read and are visible, so "
        f"they count as scanned; got {listing['episodes_scanned']}"
    )
    assert listing["episodes_clustered"] == 3, (
        f"detection drops floors and empty bodies before clustering, so the "
        f"evidence base is 3, not 5; got {listing['episodes_clustered']}"
    )
    # And the reported evidence base is the detector's real input: the one
    # pattern is made of exactly the three clusterable episodes.
    assert len(listing["patterns"]) == 1, listing
    members = set(listing["patterns"][0]["episode_ids"])
    assert len(members) == 3
    assert blanked not in members and floor.id not in members


def test_clusterable_episodes_is_the_predicate_detection_uses() -> None:
    """The unit-level half: the handler reports `len(clusterable_
    episodes(...))` precisely so the number cannot drift from what
    `find_episode_patterns` clusters. Pin that they agree on the
    excluded shapes."""
    eps = [
        _episode(_THEME_A, session="sess-a", offset_minutes=0),
        _episode(_THEME_A, session="sess-b", offset_minutes=1),
        _episode(_THEME_A, session="sess-c", offset_minutes=2),
        _episode("(session-tag floor)", session="sess-d", is_floor=True),
        _episode("   ", session="sess-e"),
    ]
    clusterable = clusterable_episodes(eps)
    assert len(eps) == 5 and len(clusterable) == 3
    pattern = find_episode_patterns(eps)[0]
    assert set(pattern.episode_ids) == {ep.id for ep in clusterable}


# ---------------------------------------------------------------------------
# Read-surface filters — episode_patterns must honor the same two hides
# `episode_search` / `episode_handoff` enforce ("episodes are the third
# leg"). Pre-fix it consulted NEITHER: it walked every session dir under
# the shared memory root, surfaced sibling-worktree and scope-disabled
# journal bodies, and — on a committed promote — bulk-DELETED those
# member episodes across the isolation boundary.
# ---------------------------------------------------------------------------


_THEME_A = "the tailscale exit node dropped again mid-sync"
_THEME_B = "the restic prune job overlapped the snapshot window"


def _make_capture(origin: Origin) -> Any:
    """`capture_origin` stand-in returning a fixed origin — the
    monkey-patch pattern from `tests/test_episode_search_isolation.py`."""

    def _capture(cwd: Any = None) -> Origin:
        return origin

    return _capture


def _set_origin(monkeypatch: Any, origin: Origin) -> None:
    """Point BOTH shim sites at `origin` (handlers route capture_origin
    through `bettermemory._handlers`; the server module has its own)."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin))


def _two_worktree_origins(memory_dir: Path) -> tuple[Origin, Origin]:
    """Two sibling worktrees of ONE repo sharing ONE memory root. The
    directories must EXIST: a nonexistent recorded worktree triggers the
    deliberate dead-worktree degrade in `worktrees_match`, which would
    make the foreign episodes visible again and void the test."""
    wt_a = memory_dir.parent / "wt-repo-a"
    wt_b = memory_dir.parent / "wt-repo-b"
    wt_a.mkdir(parents=True, exist_ok=True)
    wt_b.mkdir(parents=True, exist_ok=True)
    repo = "git@github.com:example/repo.git"
    return (
        Origin(cwd=str(wt_a), repo=repo, branch="a", worktree_root=str(wt_a)),
        Origin(cwd=str(wt_b), repo=repo, branch="b", worktree_root=str(wt_b)),
    )


def _episode_ids_on_disk(memory_dir: Path) -> set[str]:
    store = EpisodeStore(memory_dir)
    out: set[str] = set()
    for sid in store.iter_session_ids():
        out.update(ep.id for ep in store.list_by_session(sid))
    return out


async def _journal_two_worktrees(
    memory_dir: Path, monkeypatch: Any
) -> tuple[Origin, Origin, Any, Any]:
    """Worktree A journals theme A across 3 sessions; worktree B journals
    a DIFFERENT theme across 3 more. Returns both origins and both
    servers (each server's SessionState is the one its calls route to)."""
    origin_a, origin_b = _two_worktree_origins(memory_dir)

    _set_origin(monkeypatch, origin_a)
    server_a = await _journal_across_sessions(memory_dir, [_THEME_A] * 3)

    _set_origin(monkeypatch, origin_b)
    server_b = await _journal_across_sessions(memory_dir, [_THEME_B] * 3)

    return origin_a, origin_b, server_a, server_b


async def test_patterns_listing_drops_other_worktree(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """Default `auto_scope=True` confines the listing walk to the
    caller's worktree. Pre-fix a caller in B saw A's journal bodies via
    the pattern snippets — the same asymmetric cross-worktree leak
    `episode_search`'s bare-walk filter exists to close."""
    _origin_a, origin_b, _server_a, server_b = await _journal_two_worktrees(
        memory_dir, monkeypatch
    )

    _set_origin(monkeypatch, origin_b)
    listing = _unwrap(await _call(server_b, "episode_patterns"))
    assert listing["episodes_scanned"] == 3, (
        f"episodes_scanned must report the VISIBLE pool (B's 3), not the "
        f"on-disk total (6); got {listing['episodes_scanned']}"
    )
    assert len(listing["patterns"]) == 1, listing
    snippets = [s["snippet"] for s in listing["patterns"][0]["snippets"]]
    assert all("restic" in s for s in snippets), snippets
    assert not any("tailscale" in s for s in snippets), (
        f"worktree A's journal bodies leaked into B's pattern snippets: {snippets}"
    )


async def test_patterns_auto_scope_false_sweeps_all_worktrees(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """`auto_scope=False` is the explicit escape hatch — the same opt-out
    `episode_search` and `memory_search` offer. It restores the full
    cross-worktree sweep."""
    _origin_a, origin_b, _server_a, server_b = await _journal_two_worktrees(
        memory_dir, monkeypatch
    )

    _set_origin(monkeypatch, origin_b)
    swept = _unwrap(await _call(server_b, "episode_patterns", auto_scope=False))
    assert swept["episodes_scanned"] == 6
    assert len(swept["patterns"]) == 2, swept
    all_snippets = " ".join(
        s["snippet"] for p in swept["patterns"] for s in p["snippets"]
    )
    assert "tailscale" in all_snippets and "restic" in all_snippets


async def test_patterns_promote_cannot_delete_other_worktree_episodes(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """The destructive half of the leak: on a committed promote the
    member episodes are DELETED. With the walk unguarded, a caller in B
    could promote a pattern made entirely of A's episodes and wipe A's
    journal across the isolation boundary. Under the filter that
    pattern id isn't a live candidate for B at all, so neither promote
    nor dismiss can reach it — and A's episodes stay on disk."""
    _origin_a, origin_b, _server_a, server_b = await _journal_two_worktrees(
        memory_dir, monkeypatch
    )

    _set_origin(monkeypatch, origin_b)
    # Learn A's pattern id through the explicit escape hatch, then try to
    # act on it from the DEFAULT (scoped) surface.
    swept = _unwrap(await _call(server_b, "episode_patterns", auto_scope=False))
    a_pattern = next(
        p for p in swept["patterns"] if "tailscale" in p["snippets"][0]["snippet"]
    )
    before = _episode_ids_on_disk(memory_dir)
    assert set(a_pattern["episode_ids"]) <= before

    with pytest.raises(Exception):
        await _call(
            server_b,
            "episode_patterns",
            promote=a_pattern["id"],
            body="Tailscale exit nodes drop mid-sync and need a tailscaled restart.",
            scopes=["infrastructure"],
        )
    with pytest.raises(Exception):
        await _call(server_b, "episode_patterns", dismiss=a_pattern["id"])

    assert _episode_ids_on_disk(memory_dir) == before, (
        "a cross-worktree promote must not delete the foreign worktree's episodes"
    )


async def test_promote_by_explicit_id_deliberately_crosses_worktrees(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """CONTRAST to the test above — the carve-out, pinned so a future
    "make promote match patterns" reflex fails loudly instead of silently
    breaking swarm consolidation.

    `episode_patterns` filters because it DISCOVERS its own delete set: a
    bare cross-session walk picks the members and the caller commits to
    them sight-unseen, so the read filters are the only bound on what it
    can destroy. `episode_promote` takes one caller-typed 26-char ULID —
    the explicit id IS the bound, the same carve-out `episode_search`
    makes for `swarm_id` / `parent_session_id` and `episode_handoff`
    makes for `prior_session_id`, and the one every by-id durable surface
    (`memory_show` … the destructive `memory_remove`) already makes.

    Filtering here would break the fan-in it exists to serve: a
    coordinator gathers sub-agent takeaways via the explicitly-exempt
    `episode_search(swarm_id=…)` — each sub-agent in its own worktree —
    and promoting the good ones is the endpoint of that gather. Under a
    worktree filter every such promote would fail "no episode with id …"
    for an id the server had just returned.

    Also pins the blast radius: promotion deletes EXACTLY the named
    episode. A's two other journal entries survive untouched.
    """
    origin_a, origin_b, _server_a, server_b = await _journal_two_worktrees(
        memory_dir, monkeypatch
    )

    _set_origin(monkeypatch, origin_b)
    # Learn one of A's episode ids through a deliberate cross-tree read.
    # `auto_scope=False` is the DESIGNED route to a foreign episode id —
    # it is not the only one (see
    # `test_default_scoped_listing_can_hand_out_a_foreign_episode_id`),
    # and the carve-out doesn't need it to be. This mirrors how a
    # coordinator gets ids out of a swarm fan-in.
    foreign = _unwrap(await _call(server_b, "episode_search", auto_scope=False))
    a_eps = [e for e in foreign if "tailscale" in e["body"]]
    assert len(a_eps) == 3, f"expected A's 3 episodes via the escape hatch: {a_eps}"
    target = a_eps[0]["id"]
    before = _episode_ids_on_disk(memory_dir)

    promoted = _unwrap(
        await _call(
            server_b,
            "episode_promote",
            episode_id=target,
            scopes=["infrastructure"],
        )
    )
    assert promoted["status"] == "committed", (
        f"an explicit ULID must promote across the worktree boundary — this is "
        f"the swarm-consolidation endpoint, not a leak; got {promoted}"
    )
    assert promoted["promoted_from_episode_id"] == target

    after = _episode_ids_on_disk(memory_dir)
    assert after == before - {target}, (
        "promote must delete EXACTLY the named episode — the explicit ULID is "
        f"the bound on the blast radius; removed {before - after}"
    )

    # And the relocation the module docstring warns about is real: the
    # durable memory carries B's origin, so it has left A's default
    # auto-scoped retrieval (still reachable by id / auto_scope=False).
    _set_origin(monkeypatch, origin_a)
    server_a2 = _build(memory_dir)
    scoped = _unwrap(await _call(server_a2, "memory_search", query="tailscale exit"))
    assert promoted["id"] not in [h["id"] for h in scoped], (
        "promoted memory is stamped with the promoter's worktree; if it were "
        "visible to A's auto-scoped search the relocation caveat would be stale"
    )
    shown = _unwrap(await _call(server_a2, "memory_show", id=promoted["id"]))
    assert shown["id"] == promoted["id"], "by-id access must still reach it"


async def test_default_scoped_listing_can_hand_out_a_foreign_episode_id(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """The `episode_promote` carve-out rests on the SELECTOR — promote
    unlinks exactly the episode whose ULID the caller typed — and NOT on
    "a foreign id only ever arrives through a deliberate cross-tree
    read". That stronger claim is false: both default-scoped walks filter
    through the permissive `origin.worktrees_match`, which answers True
    whenever either side carries no worktree information.

    Here the caller runs OUTSIDE any git checkout (`worktree_root=None`,
    the shape `capture()` returns for a non-repo cwd), so it has no
    boundary to enforce and the default-scoped `episode_patterns` /
    `episode_search` walks hand it ids belonging to worktree A — which is
    alive on disk, so no dead-worktree degrade is doing the work. Promote
    then acts on one, deleting a foreign worktree's journal entry with no
    escape hatch anywhere in the sequence.

    This is not a bug report against the filters (permissiveness is their
    documented trade — see `worktrees_match` and
    `test_episode_search_auto_scope_no_worktree_caller_sees_all`); it
    pins the honest strength of the promote docstring's safety argument
    so a future reader can't be stopped from checking by a false
    absolute."""
    wt_a = memory_dir.parent / "wt-repo-a"
    wt_a.mkdir(parents=True, exist_ok=True)
    origin_a = Origin(
        cwd=str(wt_a),
        repo="git@github.com:example/repo.git",
        branch="a",
        worktree_root=str(wt_a),
    )
    _set_origin(monkeypatch, origin_a)
    await _journal_across_sessions(memory_dir, [_THEME_A] * 3)

    # Caller outside any git checkout — no repo, no worktree.
    _set_origin(monkeypatch, Origin(cwd=str(memory_dir.parent / "no-checkout")))
    server = _build(memory_dir)

    listing = _unwrap(await _call(server, "episode_patterns"))
    foreign_ids = [e for p in listing["patterns"] for e in p["episode_ids"]]
    assert len(foreign_ids) == 3, (
        "a DEFAULT-scoped episode_patterns listing hands a worktree-less "
        f"caller worktree A's episode ids: {listing}"
    )
    searched = _unwrap(await _call(server, "episode_search"))
    assert {e["id"] for e in searched} == set(foreign_ids), (
        "default-scoped episode_search is the same story — no auto_scope=False, "
        f"no swarm_id, no parent_session_id: {searched}"
    )

    # And promote acts on one, across the boundary, with no opt-in anywhere.
    before = _episode_ids_on_disk(memory_dir)
    promoted = _unwrap(
        await _call(
            server,
            "episode_promote",
            episode_id=foreign_ids[0],
            scopes=["infrastructure"],
        )
    )
    assert promoted["status"] == "committed", promoted
    assert _episode_ids_on_disk(memory_dir) == before - {foreign_ids[0]}, (
        "the delete is still bounded by the named id — that is the bound the "
        "carve-out actually rests on"
    )


async def test_patterns_promote_from_a_linked_worktree_deletes_the_primarys_journal(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """The destructive reach of the LINKED-worktree relaxation, pinned
    because `DESC_EPISODE_PATTERNS` now tells the model this leg exists.

    `worktrees_match` relaxes for a caller in a linked `git worktree`
    whose primary checkout is where the episode was written — deliberate,
    and the reason is in that function: strict equality made every memory
    written in the primary invisible to the ephemeral checkouts an agent
    harness spawns (this project's own audit-loop fan-out is one). The
    consequence on THIS surface is destructive rather than merely
    visible: the relaxed pool is also what a committed promote
    bulk-deletes from, so a pattern promoted out of a throwaway worktree
    unlinks the primary checkout's journal entries.

    Not a bug report against the relaxation — it is the documented trade,
    and the delete-set line in the handler says the set is exactly as
    strong as the filters and no stronger. What this pins is the DESC
    staying honest about it: the earlier copy claimed the read filters
    "keep the commit-time member DELETION inside your own worktree",
    which this sequence falsifies with the primary alive on disk (so no
    dead-worktree degrade is doing the work) and no `auto_scope=False`
    anywhere.
    """
    primary = memory_dir.parent / "primary-checkout"
    (primary / ".git" / "worktrees" / "ephemeral").mkdir(parents=True)
    linked = memory_dir.parent / "linked-worktree"
    linked.mkdir(parents=True)
    # A linked worktree's root carries a `.git` FILE pointing into the
    # primary's admin dir — the shape `_primary_root_of` reads. Built with
    # `Path` so the separator is native on every platform.
    (linked / ".git").write_text(
        f"gitdir: {primary / '.git' / 'worktrees' / 'ephemeral'}\n", encoding="utf-8"
    )

    repo = "git@github.com:example/repo.git"
    # `_primary_root_of` resolves the primary root it derives, so the
    # episodes must record the resolved path for the relaxation to fire.
    _set_origin(
        monkeypatch,
        Origin(
            cwd=str(primary),
            repo=repo,
            branch="main",
            worktree_root=str(primary.resolve()),
        ),
    )
    await _journal_across_sessions(memory_dir, [_THEME_A] * 3)
    before = _episode_ids_on_disk(memory_dir)
    assert len(before) == 3
    assert primary.exists(), "the primary must be ALIVE or the degrade explains it"

    _set_origin(
        monkeypatch,
        Origin(
            cwd=str(linked), repo=repo, branch="ephemeral", worktree_root=str(linked)
        ),
    )
    server = _build(memory_dir)
    listing = _unwrap(await _call(server, "episode_patterns"))
    assert listing["episodes_scanned"] == 3, (
        f"a caller in a linked worktree sees the primary checkout's journal "
        f"under the DEFAULT auto_scope: {listing}"
    )
    assert len(listing["patterns"]) == 1, listing

    promoted = _unwrap(
        await _call(
            server,
            "episode_patterns",
            promote=listing["patterns"][0]["id"],
            body="Tailscale exit nodes drop mid-sync; restarting tailscaled clears it.",
            scopes=["infrastructure"],
        )
    )
    assert promoted["status"] == "committed", promoted
    assert promoted["episodes_deleted"] == 3, promoted
    assert _episode_ids_on_disk(memory_dir) == set(), (
        "the promote deleted the PRIMARY checkout's journal entries from a "
        "linked worktree — the reach the DESC has to state, not deny"
    )


async def test_promote_by_explicit_id_ignores_disabled_scopes(
    memory_dir: Path,
) -> None:
    """The scope half of the same carve-out. `memory_scope_disable` is a
    retrieval-time hide for DISCOVERY surfaces, not an access-control
    gate: no by-id surface in the server (`memory_show`, `memory_update`,
    `memory_verify`, `memory_restore`, `memory_remove`) consults it, and
    answering "no such episode" about an id the caller is holding would
    be the wrong contract. Contrast
    `test_patterns_listing_honors_disabled_scopes`, where the hide DOES
    gate promote/dismiss because there the candidate pool is discovered
    rather than named."""
    body = "the diun watcher flagged a stale grafana image once more"
    server = await _journal_across_sessions(
        memory_dir, [body] * 3, scopes=["projects:zeta"]
    )
    eps = _unwrap(await _call(server, "episode_search"))
    assert eps, "expected the journal entries to be visible before the hide"
    target = eps[0]["id"]

    await _call(server, "memory_scope_disable", scope="projects:zeta")
    # The discovery surface is now blind to them...
    assert _unwrap(await _call(server, "episode_search")) == []
    # ...but the explicitly-named id still promotes, and still deletes.
    promoted = _unwrap(
        await _call(
            server,
            "episode_promote",
            episode_id=target,
            scopes=["projects:zeta"],
        )
    )
    assert promoted["status"] == "committed", (
        f"an explicitly-named episode must stay promotable while its scope is "
        f"session-disabled (scope_disable hides discovery, not by-id access); "
        f"got {promoted}"
    )
    assert target not in _episode_ids_on_disk(memory_dir)


async def test_patterns_listing_honors_disabled_scopes(memory_dir: Path) -> None:
    """`scope_disable` is an explicit user hide honored uniformly across
    the read surface. Pre-fix `episode_patterns` never consulted
    `state.disabled_scopes`, so it surfaced snippets from a scope the
    user had just asked to hide."""
    body = "the diun watcher flagged a stale grafana image once more"
    server = await _journal_across_sessions(
        memory_dir, [body] * 3, scopes=["projects:zeta"]
    )

    listing = _unwrap(await _call(server, "episode_patterns"))
    assert listing["patterns"], listing
    pid = listing["patterns"][0]["id"]

    await _call(server, "memory_scope_disable", scope="projects:zeta")
    hidden = _unwrap(await _call(server, "episode_patterns"))
    assert hidden["patterns"] == [], hidden
    assert hidden["episodes_scanned"] == 0

    # The hide gates the ACTION surfaces too — a candidate you can't see
    # is neither promotable nor dismissible.
    with pytest.raises(Exception):
        await _call(server, "episode_patterns", dismiss=pid)

    # Re-enabling restores the candidate under the same (member-stable) id.
    await _call(server, "memory_scope_enable", scope="projects:zeta")
    restored = _unwrap(await _call(server, "episode_patterns"))
    assert [p["id"] for p in restored["patterns"]] == [pid]


async def test_dismissal_from_another_worktree_is_not_gcd(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """The dismissal GC keys on episodes that still exist ON DISK, not on
    the caller-filtered pool. Otherwise a listing call from worktree A
    would read B's dismissal rows as "all members aged out" and collect
    them, resurfacing a pattern B had deliberately hidden."""
    origin_a, origin_b, _server_a, server_b = await _journal_two_worktrees(
        memory_dir, monkeypatch
    )

    _set_origin(monkeypatch, origin_b)
    listing_b = _unwrap(await _call(server_b, "episode_patterns"))
    pid_b = listing_b["patterns"][0]["id"]
    await _call(server_b, "episode_patterns", dismiss=pid_b)

    # Worktree A lists. B's episodes are invisible to A, but they are very
    # much alive on disk, so B's dismissal row must survive A's GC pass.
    _set_origin(monkeypatch, origin_a)
    server_a2 = _build(memory_dir)
    await _call(server_a2, "episode_patterns")
    assert {str(r["id"]) for r in PatternDismissals(memory_dir).load()} == {pid_b}

    # And B still sees it dismissed.
    _set_origin(monkeypatch, origin_b)
    again = _unwrap(await _call(server_b, "episode_patterns"))
    assert again["patterns"] == [], again


# ---------------------------------------------------------------------------
# PatternDismissals flock race — the GC rewrite must not persist a
# pre-lock snapshot.
# ---------------------------------------------------------------------------


def test_dismissed_ids_gc_preserves_a_concurrent_dismissal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A `dismiss()` landing between the reader's load and the reader's
    lock must survive the GC rewrite.

    Pre-fix `dismissed_ids` loaded the rows OUTSIDE the flock and took
    `flock_excl` only around the rewrite, so it persisted a stale
    pre-lock snapshot: any peer dismissal that landed in that window was
    silently erased and the pattern resurfaced — a user action undone
    with no trace.

    The interleave is injected at the reader's lock ACQUISITION, which
    is exactly the boundary the two orderings differ on. A complete,
    properly-locked peer `dismiss()` runs there (the guard flag is set
    first, so the nested call takes the real lock rather than recursing;
    nothing is held at that point, so there is no self-deadlock).

    Under the OLD order the reader's snapshot predates the peer row, the
    rewrite truncates it away, and `pat-peer` is gone. Under lock-then-
    load-then-rewrite the reader loads after the peer committed and
    keeps it.
    """
    root = tmp_path / "memories"
    root.mkdir()
    reader = PatternDismissals(root)
    # A row whose members have ALL aged out — this is what makes the GC
    # rewrite fire on the next read (pre-fix the lock was only taken
    # when a rewrite was due, so the race needed a live GC to bite).
    reader.dismiss("pat-dead", ["gone-1", "gone-2"])

    peer_ran = {"done": False}
    # Take the real implementation from its home module — `patterns` only
    # re-binds it as an import, which is not a public re-export.
    real_flock = _real_flock_excl

    @contextlib.contextmanager
    def _flock_with_peer_dismiss(path: Path, **kwargs: Any) -> Iterator[None]:
        if not peer_ran["done"]:
            peer_ran["done"] = True
            PatternDismissals(root).dismiss("pat-peer", ["live-1"])
        with real_flock(path, **kwargs):
            yield

    monkeypatch.setattr(patterns_mod, "flock_excl", _flock_with_peer_dismiss)

    surviving = reader.dismissed_ids({"live-1"})

    assert peer_ran["done"], "the interleave never fired — test is not exercising it"
    assert "pat-peer" in surviving, (
        "a peer dismissal that landed before the GC took its lock was erased "
        "by the rewrite (stale pre-lock snapshot)"
    )
    assert {str(r["id"]) for r in reader.load()} == {"pat-peer"}, (
        "the GC must drop the aged-out row and keep the concurrent one"
    )
