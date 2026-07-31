"""Integration tests: origin capture on write, auto-scope filter on search.

We monkeypatch `bettermemory.origin.capture` so tests can choose what the
"current" origin looks like at write and search time, without setting up
a fake git repo for every case.

The "checkout-path lifecycle" section at the bottom is the deliberate
exception: those cases are ABOUT what real `git rev-parse
--show-toplevel` output does over a checkout's life (a directory move,
a second live clone), so they feed the fixture a real `capture()` of a
real on-disk checkout rather than a hand-built `Origin`.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_factory(memory_dir: Path):
    """Build a server with a configurable `capture_origin` mock."""
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    def make(origin: Origin):
        # `capture_origin` is imported into `_handlers` where the tool
        # handler bodies live, and into `server` for `_cli_health`. Patch
        # both bindings so a server built by either path sees the mock.
        # Patching the name as imported, not `origin.capture` itself.
        import bettermemory._handlers as handlers_module
        import bettermemory.server as server_module

        captured = {"value": origin}

        def fake_capture(cwd: Path | None = None) -> Origin:
            return captured["value"]

        rec = Recorder(root=memory_dir, session_id=state.session_id)
        server = build_server(
            config=cfg,
            store=Store(memory_dir),
            state=state,
            recorder=rec,
        )
        # Override the imported references so the handlers see our fake.
        # setattr keeps mypy happy without a per-line ignore — capture_origin
        # is a module-level binding the handlers re-resolve at call time.
        setattr(handlers_module, "capture_origin", fake_capture)
        setattr(server_module, "capture_origin", fake_capture)
        return server, captured

    return make


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


# ---------------------------------------------------------------------------
# Origin captured on write
# ---------------------------------------------------------------------------


async def test_write_captures_origin_into_show(
    server_factory,
) -> None:
    server, _ = server_factory(
        Origin(
            cwd="/projects/foo",
            repo="git@github.com:example/foo.git",
            branch="main",
        )
    )
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["origin"] == {
        "cwd": "/projects/foo",
        "repo": "git@github.com:example/foo.git",
        "branch": "main",
    }


async def test_write_with_no_repo_persists_only_cwd(
    server_factory,
) -> None:
    server, _ = server_factory(Origin(cwd="/projects/scratch"))
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["origin"] == {"cwd": "/projects/scratch"}


async def test_origin_persists_through_disk_roundtrip(
    server_factory, memory_dir: Path
) -> None:
    server, _ = server_factory(
        Origin(
            cwd="/projects/foo",
            repo="git@github.com:example/foo.git",
            branch="dev",
        )
    )
    written = await _call(server, "memory_write", content="x", scopes=["tools"])

    # Load via a fresh Store — proves origin was persisted, not just held
    # in memory.
    fresh = Store(memory_dir)
    loaded = fresh.load_one(written["id"])
    assert loaded.origin is not None
    assert loaded.origin.repo == "git@github.com:example/foo.git"


async def test_origin_survives_memory_update(
    server_factory,
) -> None:
    server, _ = server_factory(
        Origin(
            cwd="/projects/foo",
            repo="git@github.com:example/foo.git",
            branch="main",
        )
    )
    written = await _call(server, "memory_write", content="initial", scopes=["tools"])
    await _call(server, "memory_update", id=written["id"], content="refined")
    shown = await _call(server, "memory_show", id=written["id"])
    # Update preserves origin — it's a property of the original write.
    assert shown["origin"]["repo"] == "git@github.com:example/foo.git"


# ---------------------------------------------------------------------------
# Auto-scope filter on memory_search
# ---------------------------------------------------------------------------


async def test_search_auto_scope_filters_other_repos(
    server_factory,
) -> None:
    """A memory written from repo A should not surface during a search
    from repo B when auto_scope=True (the default)."""
    server, captured = server_factory(Origin(repo="git@github.com:example/repo-a.git"))

    # Write while "in" repo A.
    await _call(
        server,
        "memory_write",
        content="kubernetes networking notes for repo A",
        scopes=["infrastructure"],
    )

    # Switch to "repo B" and search.
    captured["value"] = Origin(repo="git@github.com:example/repo-b.git")
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert hits == []


async def test_search_auto_scope_includes_same_repo(
    server_factory,
) -> None:
    server, _ = server_factory(Origin(repo="git@github.com:example/foo.git"))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_auto_scope_includes_global_memories(
    server_factory,
) -> None:
    """Memories written outside any git repo (origin.repo = None) are
    global and should surface from any project."""
    server, captured = server_factory(Origin(cwd="/projects/scratch"))

    written = await _call(
        server,
        "memory_write",
        content="global preference: tabs over spaces",
        scopes=["learning-style"],
    )

    captured["value"] = Origin(repo="git@github.com:example/anything.git")
    hits = _unwrap(await _call(server, "memory_search", query="tabs over spaces"))
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_auto_scope_false_returns_cross_project(
    server_factory,
) -> None:
    server, captured = server_factory(Origin(repo="git@github.com:example/repo-a.git"))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )

    captured["value"] = Origin(repo="git@github.com:example/repo-b.git")
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking",
            auto_scope=False,
        )
    )
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_caller_outside_repo_does_not_filter(
    server_factory,
) -> None:
    """When the caller is not in a repo (origin.repo = None), there's no
    project boundary to enforce — everything passes the auto-scope filter."""
    server, captured = server_factory(Origin(repo="git@github.com:example/foo.git"))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )

    # Caller is not in a repo.
    captured["value"] = Origin(cwd="/projects/scratch")
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert any(h["id"] == written["id"] for h in hits)


async def test_legacy_memory_without_origin_passes_filter(
    server_factory, memory_dir: Path
) -> None:
    """An existing on-disk memory that was written before the auto-scope
    feature shipped (no `origin` frontmatter) is global by definition."""
    server, _ = server_factory(Origin(repo="git@github.com:example/foo.git"))

    # Hand-craft a memory file without an `origin` block — the format we
    # had before this phase.
    legacy = memory_dir / "2025-01-01-legacy-fact.md"
    legacy.write_text(
        "---\n"
        "id: 01HXYZKEGACYJDKEGACY00000Z\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        "scopes:\n"
        "- tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "kubernetes networking notes from before the feature shipped\n",
        encoding="utf-8",
    )

    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert any(h["id"] == "01HXYZKEGACYJDKEGACY00000Z" for h in hits)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def test_search_records_auto_scope_and_repo_filter(
    server_factory, memory_dir: Path
) -> None:
    server, _ = server_factory(Origin(repo="git@github.com:example/foo.git"))
    await _call(server, "memory_search", query="anything")

    search_events = [e for e in iter_events(memory_dir) if e["kind"] == "search"]
    assert search_events
    e = search_events[-1]
    assert e["auto_scope"] is True
    assert e["repo_filter"] == "git@github.com:example/foo.git"


async def test_search_with_auto_scope_false_records_null_filter(
    server_factory, memory_dir: Path
) -> None:
    server, _ = server_factory(Origin(repo="git@github.com:example/foo.git"))
    await _call(server, "memory_search", query="anything", auto_scope=False)
    search_events = [e for e in iter_events(memory_dir) if e["kind"] == "search"]
    e = search_events[-1]
    assert e["auto_scope"] is False
    assert e["repo_filter"] is None


# ---------------------------------------------------------------------------
# Checkout-path lifecycle — `origin.worktree_root` is an absolute path
# frozen at write time, and the retrieval surface must not go dark when
# that path stops describing where the project lives.
#
# `tests/test_origin.py` pins the same rule at the `worktrees_match` unit
# level. These are the surface-level twins: the assertion is about what
# `memory_search` / `memory_scope_overview` actually return, because the
# failure this guards is a SILENT one — an empty result set carries no
# signal that a filter dropped everything.
# ---------------------------------------------------------------------------

_GIT_AVAILABLE = shutil.which("git") is not None
_REMOTE = "git@github.com:example/myapp.git"


def _init_checkout(path: Path, *, remote: str = _REMOTE) -> None:
    """A real primary checkout with `remote` as `origin` — enough for
    `capture()` to report a repo URL and a worktree root."""
    path.mkdir(parents=True)
    for args in (
        ["init", "--initial-branch=main"],
        ["remote", "add", "origin", remote],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_project_memories_survive_a_checkout_move(
    server_factory, tmp_path: Path
) -> None:
    """Rename the project directory and the project's memories still
    surface under auto_scope.

    The recorded `worktree_root` is the pre-move absolute path, so the
    strict-equality core of `worktrees_match` says "different worktree".
    The dead-worktree degrade is what saves it: the recorded root no
    longer exists, so there is no live workspace to isolate from and the
    filter falls back to repo-level matching. Without that leg every
    memory written before an ordinary `mv` would drop out of every
    auto-scoped search for the project, with an empty result set as the
    only symptom.
    """
    from bettermemory.origin import _primary_root_of, capture

    old = tmp_path / "projects" / "myapp"
    new = tmp_path / "Documents" / "projects" / "myapp"
    _init_checkout(old)

    _primary_root_of.cache_clear()
    server, captured = server_factory(capture(cwd=old))
    assert captured["value"].worktree_root == str(old.resolve())

    written = await _call(
        server,
        "memory_write",
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
    )
    before = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in before] == [written["id"]]

    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    assert not old.exists()
    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=new)
    # Same project by repo URL, different absolute root — the exact
    # shape the strict check would have excluded.
    assert captured["value"].repo == _REMOTE
    assert captured["value"].worktree_root == str(new.resolve())

    after = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in after] == [written["id"]]
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert overview["total"] == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_synced_memory_from_another_machine_surfaces_locally(
    server_factory, tmp_path: Path
) -> None:
    """A memory that arrived over `sync` from another machine carries
    that machine's absolute `worktree_root`, which cannot exist locally
    — it still surfaces for the same repo.

    Distinct from the move case only in that the path was never local to
    begin with; both land on the dead-worktree degrade. Pinned
    separately because a "resolve the recorded path relative to the
    current machine" style fix would pass the move test and fail this
    one.
    """
    from bettermemory.origin import _primary_root_of, capture

    foreign = "/home/ci-user/projects/myapp"
    server, captured = server_factory(
        Origin(cwd=foreign, repo=_REMOTE, branch="main", worktree_root=foreign)
    )
    written = await _call(
        server,
        "memory_write",
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
    )

    local = tmp_path / "Documents" / "projects" / "myapp"
    _init_checkout(local)
    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=local)
    assert captured["value"].worktree_root == str(local.resolve())

    hits = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in hits] == [written["id"]]
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert overview["total"] == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_second_live_checkout_of_one_repo_stays_isolated(
    server_factory, tmp_path: Path
) -> None:
    """The negative control for the two tests above, and the boundary of
    what the degrade covers.

    When the recorded root is still a live checkout on disk, the
    worktree filter keeps doing its job: a memory written in one
    checkout of a repo does not surface in a second, concurrently
    existing checkout of the same repo. So the degrade is keyed on the
    recorded workspace being GONE, not on "paths differ" — a fix that
    simply dropped the worktree clause would pass the move test and
    fail this one.
    """
    from bettermemory.origin import _primary_root_of, capture

    first = tmp_path / "clones" / "myapp"
    second = tmp_path / "clones" / "myapp-review"
    _init_checkout(first)
    _init_checkout(second)

    _primary_root_of.cache_clear()
    server, captured = server_factory(capture(cwd=first))
    await _call(
        server,
        "memory_write",
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
    )

    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=second)
    assert captured["value"].repo == _REMOTE
    assert first.exists()

    assert _unwrap(await _call(server, "memory_search", query="bcrypt")) == []


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_tombstone_count_survives_a_checkout_move(
    server_factory, tmp_path: Path
) -> None:
    """`recently_removed_in_worktree` survives the same directory move
    the active-memory surfaces do.

    The retrieval-path twin of
    `test_project_memories_survive_a_checkout_move`. The tombstone count
    is worktree-keyed like every other auto-scoped surface, but it was
    the one that compared roots with a raw `!=` instead of routing
    through the shared rule, so it never got the degrade: after an
    ordinary `mv` of the checkout the searches kept working and this
    count silently dropped to 0 — reporting "nothing was trimmed here"
    about a workspace that had just trimmed something.

    The negative control lives in `tests/test_server.py`'s
    `test_scope_overview_recently_removed_filtered_by_worktree`: two
    checkouts that are both LIVE on disk still do not see each other's
    removals.
    """
    from bettermemory.origin import _primary_root_of, capture

    old = tmp_path / "projects" / "myapp"
    new = tmp_path / "Documents" / "projects" / "myapp"
    _init_checkout(old)

    _primary_root_of.cache_clear()
    server, captured = server_factory(capture(cwd=old))
    written = await _call(
        server,
        "memory_write",
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
    )
    await _call(server, "memory_remove", id=written["id"], reason="superseded")
    before = _unwrap(await _call(server, "memory_scope_overview"))
    assert before["recently_removed_in_worktree"] == 1

    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    assert not old.exists()
    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=new)
    assert captured["value"].repo == _REMOTE
    assert captured["value"].worktree_root == str(new.resolve())

    after = _unwrap(await _call(server, "memory_scope_overview"))
    assert after["recently_removed_in_worktree"] == 1
