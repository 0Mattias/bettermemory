"""Regression cuts for the two `episode_search` handler fixes.

(a) SORT BUG — the handler used to re-sort by the rendered ISO-8601
    *string* form of `created` before applying the most-recent-N cap.
    `datetime.isoformat()` omits the fractional-seconds component when
    `microsecond == 0`, and lexically `"."` (0x2E) sorts before `"Z"`
    (0x5A), so a whole-second / bare-date episode (`…T12:00:00Z`) sorted
    AFTER a same-second fractional one (`…T12:00:00.500000Z`). That
    mis-sorted the whole-second episode as the newest of its second, so
    it was kept in the most-recent-N window while a genuinely-newer
    fractional episode was dropped — and the surfaced order was
    non-chronological, violating the docstring's "oldest-first within
    most-recent-N". The fix sorts the filtered `Episode` objects by the
    `created` DATETIME (mirroring `list_by_session` / `list_by_swarm`),
    slices, THEN renders.

(b) ISOLATION GAP — `episode_search` had no worktree filter, unlike
    `memory_search` (auto-scopes by default) and `episode_handoff`
    (strict worktree equality at iteration entry). With no args it walked
    every session dir under the shared memory root and returned journal
    bodies from ALL worktrees sharing that root — the asymmetric
    cross-worktree leak `episode_handoff` exists to prevent. The fix adds
    an opt-in `auto_scope` param (default True) that drops episodes whose
    `origin.worktree_root` doesn't match the caller's; legacy / None-origin
    episodes pass through, and `auto_scope=False` is the explicit
    cross-worktree escape hatch.

Both tests drive the registered MCP tool through `build_server` /
`call_tool`, the same idiom the rest of the episode handler tests use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import Config, StorageConfig
from bettermemory.episodes import EpisodeStore
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return the parsed JSON payload (mirrors the
    helper in `tests/test_server.py`)."""
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _make_capture(origin: Origin) -> Any:
    """Build a `capture_origin` stand-in returning a fixed origin —
    matches the monkey-patch pattern in `tests/test_server.py`."""

    def _capture(cwd: Any = None) -> Origin:
        return origin

    return _capture


# ---------------------------------------------------------------------------
# (a) SORT BUG — datetime-keyed sort vs lossy ISO-string sort
# ---------------------------------------------------------------------------


async def test_episode_search_sorts_by_created_datetime_not_iso_string(
    memory_dir: Path,
) -> None:
    """The most-recent-N window must be computed on the `created`
    DATETIME, not the rendered ISO string.

    Three episodes in one second-neighbourhood:
      - whole-second  `2026-05-31T12:00:00Z`        (microsecond == 0)
      - fractional    `2026-05-31T12:00:00.500000Z` (same second, later)
      - newer         `2026-05-31T12:00:01.000000Z` (a full second later)

    True chronological order is [whole, fractional, newer]; the
    most-recent-2 window is therefore [fractional, newer].

    Under the OLD string sort, `isoformat()` drops the `.000000` on the
    whole-second value, so the keys sort as
      "…12:00:00.500000Z" < "…12:00:00Z" < "…12:00:01.000000Z"
    (because "." < "Z"), i.e. [fractional, whole, newer]. The tail-2
    slice then keeps [whole, newer] — dropping the genuinely-newer
    `fractional` episode and surfacing a non-chronological window. This
    test pins that the whole-second episode is the one that ages out,
    and that the surfaced order is chronological.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = EpisodeStore(memory_dir)

    # Single session; `created` is set explicitly via `now=` so the sort
    # key — not ULID/insertion order — is the sole discriminator. Write
    # them in an order that does NOT match `created` order to prove the
    # handler's own sort is what orders the output.
    sid = "sess_sortbug01"
    whole = store.write(
        session_id=sid,
        body="whole-second body",
        takeaway="whole",
        now=datetime(2026, 5, 31, 12, 0, 0, 0, tzinfo=timezone.utc),
    )
    store.write(
        session_id=sid,
        body="newer body",
        takeaway="newer",
        now=datetime(2026, 5, 31, 12, 0, 1, 0, tzinfo=timezone.utc),
    )
    frac = store.write(
        session_id=sid,
        body="fractional body",
        takeaway="fractional",
        now=datetime(2026, 5, 31, 12, 0, 0, 500000, tzinfo=timezone.utc),
    )

    # Sanity: `isoformat()` really does drop fractional seconds at
    # microsecond == 0, which is the lossy property the bug rode on.
    assert whole.created.isoformat().endswith("12:00:00+00:00")
    assert frac.created.isoformat().endswith("12:00:00.500000+00:00")

    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = _unwrap(await _call(server, "episode_search", max_results=2))

    takeaways = [e["takeaway"] for e in res]
    # Correct most-recent-2 in chronological order. The buggy string sort
    # would yield ["whole", "newer"] (and drop "fractional").
    assert takeaways == ["fractional", "newer"], (
        f"expected datetime-keyed most-recent-2 [fractional, newer]; got {takeaways}"
    )
    # The genuinely-newer fractional episode must NOT be the one dropped.
    assert frac.id in {e["id"] for e in res}
    # The whole-second episode is the correct one to age out of the window.
    assert whole.id not in {e["id"] for e in res}


async def test_episode_search_full_set_is_chronological_oldest_first(
    memory_dir: Path,
) -> None:
    """With no cap pressure, the whole-second / fractional pair must still
    come back oldest-first (the docstring contract). The buggy string sort
    put the fractional value before the whole-second value of the same
    second; the datetime sort orders them correctly."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = EpisodeStore(memory_dir)

    sid = "sess_sortbug02"
    store.write(
        session_id=sid,
        body="b-frac",
        takeaway="fractional",
        now=datetime(2026, 5, 31, 12, 0, 0, 500000, tzinfo=timezone.utc),
    )
    store.write(
        session_id=sid,
        body="b-whole",
        takeaway="whole",
        now=datetime(2026, 5, 31, 12, 0, 0, 0, tzinfo=timezone.utc),
    )

    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = _unwrap(await _call(server, "episode_search"))
    # Oldest first: whole-second (12:00:00.000000) precedes the
    # same-second fractional value (12:00:00.500000).
    assert [e["takeaway"] for e in res] == ["whole", "fractional"]


# ---------------------------------------------------------------------------
# (b) ISOLATION GAP — opt-in worktree auto-scope
# ---------------------------------------------------------------------------


async def test_episode_search_auto_scope_drops_other_worktree(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """By default (`auto_scope=True`) episode_search drops episodes
    written from a different worktree of the same repository that shares
    the memory root — the asymmetric cross-worktree leak `episode_handoff`
    already guards against. A legacy / None-origin episode passes through
    (do not hide pre-field writes)."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/wt/repo-a",
        repo="git@github.com:example/repo.git",
        branch="feature-a",
        worktree_root="/wt/repo-a",
    )
    origin_b = Origin(
        cwd="/wt/repo-b",
        repo="git@github.com:example/repo.git",
        branch="feature-b",
        worktree_root="/wt/repo-b",
    )

    # Worktree A writes an episode (origin captured from A).
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A body", takeaway="from A")

    # Worktree B (sibling checkout, same repo + same memory root) writes
    # its own episode.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode_write", body="B body", takeaway="from B")

    # A legacy episode with NO captured origin (pre-field write). Written
    # via the store directly under its own session so it isn't stamped
    # with any worktree — it must remain visible to every caller.
    EpisodeStore(memory_dir).write(
        session_id="sess_legacy01",
        body="legacy body",
        takeaway="legacy",
        origin=None,
    )

    # Caller in worktree A, auto_scope defaulted True. Sees A's own
    # episode + the legacy one; B's cross-worktree episode is dropped.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    res = _unwrap(await _call(server_a, "episode_search"))
    takeaways = {e["takeaway"] for e in res}
    assert takeaways == {"from A", "legacy"}, (
        f"auto_scope should drop the sibling worktree's episode and keep "
        f"the legacy (None-origin) one; got {takeaways}"
    )
    assert "from B" not in takeaways


async def test_episode_search_auto_scope_false_sweeps_all_worktrees(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """`auto_scope=False` is the explicit escape hatch — it restores the
    full cross-worktree sweep (every session dir sharing the memory
    root), so a caller can deliberately gather every worktree's journal."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/wt/repo-a",
        repo="git@github.com:example/repo.git",
        branch="feature-a",
        worktree_root="/wt/repo-a",
    )
    origin_b = Origin(
        cwd="/wt/repo-b",
        repo="git@github.com:example/repo.git",
        branch="feature-b",
        worktree_root="/wt/repo-b",
    )

    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A body", takeaway="from A")

    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode_write", body="B body", takeaway="from B")

    # Caller in worktree A, but opting OUT of isolation. Sees BOTH.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    res = _unwrap(await _call(server_a, "episode_search", auto_scope=False))
    assert {e["takeaway"] for e in res} == {"from A", "from B"}


async def test_episode_search_auto_scope_no_worktree_caller_sees_all(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """A caller running outside any git checkout (origin.worktree_root is
    None) has no worktree boundary to enforce, so even with auto_scope on
    it sees every episode — the permissive `worktrees_match` (either side
    None → True), matching how `memory_search` treats a repo-less caller.
    """
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/wt/repo-a",
        repo="git@github.com:example/repo.git",
        branch="feature-a",
        worktree_root="/wt/repo-a",
    )
    origin_none = Origin(
        cwd="/tmp/no-checkout", repo=None, branch=None, worktree_root=None
    )

    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A body", takeaway="from A")

    # Caller with no worktree, auto_scope defaulted True. Permissive
    # match → the named-worktree episode still passes through.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_none))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_none))
    server_none = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = _unwrap(await _call(server_none, "episode_search"))
    assert {e["takeaway"] for e in res} == {"from A"}


# ---------------------------------------------------------------------------
# (c) EXPLICIT-SELECTION CARVE-OUT — `swarm_id` / `parent_session_id` name a
#     cohort or a specific session and are deliberate cross-worktree reads;
#     the worktree filter must NOT apply to them. (Regression: the first cut
#     of the auto_scope filter applied it UNIFORMLY, so a coordinator's swarm
#     fan-in across sub-agent worktrees silently returned [] — defeating the
#     N:1 fan-in primitive `list_by_swarm` exists for.)
# ---------------------------------------------------------------------------


async def test_episode_search_swarm_fan_in_crosses_worktrees(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """A coordinator's `episode_search(swarm_id=...)` must gather EVERY
    sub-agent's episode even though each sub-agent ran in its OWN worktree
    (the canonical parallel-isolation pattern). An explicit swarm_id is
    deliberate cross-worktree intent, so the default auto_scope filter must
    NOT drop the cohort. Pre-fix the uniform filter compared each sub-agent's
    worktree against the coordinator's and returned []."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    repo = "git@github.com:example/repo.git"
    origin_a = Origin(
        cwd="/wt/agent1", repo=repo, branch="a", worktree_root="/wt/agent1"
    )
    origin_b = Origin(
        cwd="/wt/agent2", repo=repo, branch="b", worktree_root="/wt/agent2"
    )
    origin_coord = Origin(
        cwd="/wt/coord", repo=repo, branch="c", worktree_root="/wt/coord"
    )

    # Two sub-agents, each in its OWN worktree, tag episodes with the
    # coordinator's cohort id.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode_write",
        body="A body",
        takeaway="from agent1",
        swarm_id="coord",
    )

    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_b,
        "episode_write",
        body="B body",
        takeaway="from agent2",
        swarm_id="coord",
    )

    # Coordinator, in its OWN (third) worktree, default auto_scope. Must see
    # BOTH sub-agents — the explicit swarm_id is exempt from worktree scoping.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_coord))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_coord))
    server_coord = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = _unwrap(await _call(server_coord, "episode_search", swarm_id="coord"))
    takeaways = {e["takeaway"] for e in res}
    assert takeaways == {"from agent1", "from agent2"}, (
        f"swarm fan-in must cross worktrees (explicit swarm_id is deliberate "
        f"cross-tree intent); got {takeaways}"
    )


async def test_episode_search_explicit_parent_session_crosses_worktrees(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Naming an explicit `parent_session_id` is the caller's deliberate
    selection of one session, so it must read across worktrees — mirroring
    how `episode_handoff` respects an explicit `prior_session_id`. The
    default auto_scope filter must NOT drop a named session that lives in
    another worktree."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    repo = "git@github.com:example/repo.git"
    origin_a = Origin(
        cwd="/wt/repo-a", repo=repo, branch="a", worktree_root="/wt/repo-a"
    )
    origin_b = Origin(
        cwd="/wt/repo-b", repo=repo, branch="b", worktree_root="/wt/repo-b"
    )

    # Agent A writes in worktree A; capture its session id from the write.
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    write_res = await _call(server_a, "episode_write", body="A body", takeaway="from A")
    sid_a = write_res["session_id"]

    # Caller in worktree B explicitly names A's session. Must get A's episode
    # back under default auto_scope (explicit selection = cross-tree consent).
    monkeypatch.setattr(handlers_module, "capture_origin", _make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", _make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = _unwrap(await _call(server_b, "episode_search", parent_session_id=sid_a))
    assert {e["takeaway"] for e in res} == {"from A"}, (
        "explicit parent_session_id must read across worktrees, not be auto-scoped out"
    )
