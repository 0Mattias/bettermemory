"""Cross-episode pattern candidates: detection, dismissal persistence,
and the episode_patterns promote/dismiss surface."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.models import Episode, generate_ulid
from bettermemory.patterns import PatternDismissals, find_episode_patterns
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
        _episode("nginx certificate renewal failed silently", session=s, offset_minutes=i)
        for i, s in enumerate(["sess-a", "sess-b", "sess-c"])
    ]
    a = find_episode_patterns(eps)[0]
    b = find_episode_patterns(list(reversed(eps)))[0]
    assert a.id == b.id

    # A new member episode changes the id — fresh evidence reopens a
    # dismissed pattern under a new identity.
    eps.append(
        _episode("nginx certificate renewal failed silently", session="sess-d")
    )
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


async def _journal_across_sessions(memory_dir: Path, bodies: list[str]) -> Any:
    """Write each body from a FRESH server (fresh SessionState → distinct
    session directory), mirroring how a real journal accumulates across
    days. Returns the last server for follow-up calls."""
    server = None
    for body in bodies:
        server = _build(memory_dir)
        await _call(
            server,
            "episode_write",
            body=body,
            takeaway=body.split(".")[0],
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
