"""Storage-layer tests for `EpisodeStore`.

Handler-level tests live in `tests/test_server.py` alongside the other
MCP tool tests. The cuts here pin the on-disk shape and prune semantics
so a future refactor can't silently break the format or the TTL
contract.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bettermemory.episodes import EpisodeStore
from bettermemory.origin import Origin


@pytest.fixture
def episode_store(tmp_path: Path) -> EpisodeStore:
    return EpisodeStore(tmp_path)


def test_write_creates_session_dir_lazily(episode_store: EpisodeStore) -> None:
    """The `episodes/` subtree only appears on first write — a fresh
    install with no episodes leaves no empty dir behind."""
    assert not episode_store.episodes_dir.exists()
    episode_store.write(session_id="sess_aaaa1111", body="hello")
    assert episode_store.episodes_dir.exists()
    assert (episode_store.episodes_dir / "sess_aaaa1111").is_dir()


def test_write_persists_takeaway_and_scopes(episode_store: EpisodeStore) -> None:
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="iteration body",
        takeaway="one-line summary",
        scopes=["projects:foo", "tools"],
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert len(loaded) == 1
    assert loaded[0].id == ep.id
    assert loaded[0].takeaway == "one-line summary"
    assert loaded[0].scopes == ["projects:foo", "tools"]


def test_write_persists_origin(episode_store: EpisodeStore) -> None:
    origin = Origin(
        cwd="/tmp/work",
        repo="https://github.com/0Mattias/example",
        branch="main",
        worktree_root="/tmp/work",
    )
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="origin test",
        origin=origin,
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert loaded[0].origin is not None
    assert loaded[0].origin.repo == "https://github.com/0Mattias/example"
    assert loaded[0].id == ep.id


def test_list_by_session_sorts_oldest_first(episode_store: EpisodeStore) -> None:
    """ULIDs sort lexically by creation timestamp; list_by_session
    surfaces them oldest first so a handoff caller can take the most
    recent N from the tail."""
    a = episode_store.write(session_id="sess_aaaa1111", body="first")
    time.sleep(0.005)  # ms-resolution ULID needs a beat to bump
    b = episode_store.write(session_id="sess_aaaa1111", body="second")
    eps = episode_store.list_by_session("sess_aaaa1111")
    assert [e.id for e in eps] == [a.id, b.id]


def test_rejects_traversal_in_session_id(episode_store: EpisodeStore) -> None:
    """A session_id containing `/` or `..` would let a hostile caller
    escape the episodes subtree. Reject at the storage boundary."""
    with pytest.raises(ValueError):
        episode_store.write(session_id="../etc/passwd", body="x")
    with pytest.raises(ValueError):
        episode_store.write(session_id="a/b", body="x")


def test_prune_drops_sessions_past_ttl(episode_store: EpisodeStore) -> None:
    """Sessions whose newest episode mtime is past the TTL get rmtree'd."""
    episode_store.write(session_id="sess_old", body="ancient")
    episode_store.write(session_id="sess_new", body="fresh")

    # Backdate the "old" session's files past the TTL.
    old_dir = episode_store.episodes_dir / "sess_old"
    for f in old_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)  # 40 days ago
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_old" in pruned
    assert "sess_new" not in pruned
    assert not (episode_store.episodes_dir / "sess_old").exists()
    assert (episode_store.episodes_dir / "sess_new").exists()


def test_prune_respects_keep_session_id(episode_store: EpisodeStore) -> None:
    """The active session's dir is exempt from pruning even if its
    newest mtime is past the TTL — a session that paused for >30d
    shouldn't lose its own scratch when it resumes writing."""
    episode_store.write(session_id="sess_active", body="paused-then-resumed")
    active_dir = episode_store.episodes_dir / "sess_active"
    for f in active_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(
        ttl_days=30, keep_session_id="sess_active"
    )
    assert "sess_active" not in pruned
    assert (episode_store.episodes_dir / "sess_active").exists()


def test_prune_zero_ttl_is_noop(episode_store: EpisodeStore) -> None:
    """`ttl_days <= 0` disables the prune entirely. Used by callers
    that want to manage retention explicitly via the CLI rather than
    on every write."""
    episode_store.write(session_id="sess_aaaa1111", body="any")
    pruned = episode_store.prune_old_sessions(ttl_days=0)
    assert pruned == []
    assert (episode_store.episodes_dir / "sess_aaaa1111").exists()


def test_excluded_from_memory_store_iteration(tmp_path: Path) -> None:
    """Episodes must not appear in `Store.load_all` — episodes live in
    a sibling subdirectory (`episodes/`), so the memory store's
    `_iter_active_paths` (which uses `iterdir` on the root) should
    skip directory entries naturally. This pin catches a future
    refactor that accidentally recurses or globs."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    ep_store = EpisodeStore(tmp_path)
    ep_store.write(session_id="sess_aaaa1111", body="not a memory")

    memories = store.load_all()
    assert memories == []
