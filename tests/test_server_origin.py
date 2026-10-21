"""Integration tests: origin capture on write, auto-scope filter on search.

We monkeypatch `bettermemory.origin.capture` so tests can choose what the
"current" origin looks like at write and search time. The origin a write
records is data; the caller's membership is resolved where the origin's
``cwd`` stands (`handlers._shared.request_cwd`, design 3.1), and a record
reaches a search by its home (design 3.5). The search cases therefore
stand the caller in real repositories built under the test's temporary
directory; the origin-on-write cases need none.

The "checkout-path lifecycle" section at the bottom is ABOUT what a
checkout's life does to delivery (a directory move, another machine's
checkout of the project, a second clone), so it feeds the fixture a real
`capture()` of a real on-disk checkout.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.membership import (
    GLOBAL,
    SEARCH_AUTO_SCOPE,
    SEARCH_UNSCOPED,
    Membership,
    RecordHome,
)
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from .test_membership_security import checkout

REPO_A = "git@github.com:example/repo-a.git"
REPO_B = "git@github.com:example/repo-b.git"


@pytest.fixture
def server_factory(memory_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a server with a configurable `capture_origin` mock.

    The patch goes through `monkeypatch` so it is restored at teardown:
    a bare `setattr` leaks the fake into every later test in the run
    that relies on the real `capture_origin`."""
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)

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

        rec = Recorder(store=store, session_id=state.session_id)
        server = build_server(
            config=cfg,
            store=store,
            state=state,
            recorder=rec,
        )
        # Override the imported references so the handlers see our fake:
        # capture_origin is a module-level binding the handlers re-resolve
        # at call time.
        monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
        monkeypatch.setattr(server_module, "capture_origin", fake_capture)
        return server, captured

    return make


@pytest.fixture
def places(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Two repositories, each its own project (``a``, ``b``), and a folder
    outside any repository (``scratch``)."""
    base = tmp_path_factory.mktemp("places")
    scratch = base / "scratch"
    scratch.mkdir()
    return {
        "a": checkout(base / "repo-a", remote=REPO_A),
        "b": checkout(base / "repo-b", remote=REPO_B),
        "scratch": scratch,
    }


def _at(path: Path, repo: str | None = None) -> Origin:
    """The origin of a caller standing at `path`."""
    return Origin(cwd=str(path), repo=repo)


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
    # `source` is the read surface's label for an origin that names no
    # channel: it was captured from the process cwd (identity, 7.10.0).
    assert shown["origin"] == {
        "cwd": "/projects/foo",
        "repo": "git@github.com:example/foo.git",
        "branch": "main",
        "source": "process-cwd",
    }


async def test_write_with_no_repo_persists_only_cwd(
    server_factory,
) -> None:
    server, _ = server_factory(Origin(cwd="/projects/scratch"))
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["origin"] == {"cwd": "/projects/scratch", "source": "process-cwd"}


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
    server_factory, places: dict[str, Path]
) -> None:
    """A memory written in repo A's project does not surface during a
    search from repo B's when auto_scope=True (the default)."""
    server, captured = server_factory(_at(places["a"], REPO_A))

    # Write while in repo A.
    await _call(
        server,
        "memory_write",
        content="kubernetes networking notes for repo A",
        scopes=["infrastructure"],
    )

    # Switch to repo B and search.
    captured["value"] = _at(places["b"], REPO_B)
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert hits == []


async def test_search_auto_scope_includes_same_repo(
    server_factory, places: dict[str, Path]
) -> None:
    server, _ = server_factory(_at(places["a"], REPO_A))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_auto_scope_includes_global_memories(
    server_factory, places: dict[str, Path]
) -> None:
    """Memories written outside any repository are homed ``global`` and
    surface from any project (design 3.5)."""
    server, captured = server_factory(_at(places["scratch"]))

    written = await _call(
        server,
        "memory_write",
        content="global preference: tabs over spaces",
        scopes=["learning-style"],
    )

    captured["value"] = _at(places["a"], REPO_A)
    hits = _unwrap(await _call(server, "memory_search", query="tabs over spaces"))
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_auto_scope_false_returns_cross_project(
    server_factory, places: dict[str, Path]
) -> None:
    server, captured = server_factory(_at(places["a"], REPO_A))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )

    captured["value"] = _at(places["b"], REPO_B)
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking",
            auto_scope=False,
        )
    )
    assert any(h["id"] == written["id"] for h in hits)


async def test_search_caller_outside_any_project_reaches_projects_unscoped(
    server_factory, places: dict[str, Path]
) -> None:
    """A caller outside any project has no project of its own: its
    auto-scoped search admits the records written outside any project,
    and its unscoped search every project's as well (design 3.5, the
    table's outside column). A project's record is a request away, never
    pushed."""
    server, captured = server_factory(_at(places["a"], REPO_A))
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )

    # The caller is not in a repository.
    captured["value"] = _at(places["scratch"])
    scoped = _unwrap(
        await _call(server, "memory_search", query="kubernetes networking")
    )
    assert all(h["id"] != written["id"] for h in scoped)
    unscoped = _unwrap(
        await _call(
            server, "memory_search", query="kubernetes networking", auto_scope=False
        )
    )
    assert any(h["id"] == written["id"] for h in unscoped)


async def test_legacy_memory_without_origin_passes_filter(
    server_factory, memory_dir: Path, places: dict[str, Path]
) -> None:
    """A memory that carries no `origin` at all (the shape every record
    had before the auto-scope feature shipped) is homed ``global`` by the
    migration (rule ``global:no_origin``, design 2.1) and surfaces from
    any project. A record with no home row is admitted nowhere."""
    server, _ = server_factory(_at(places["a"], REPO_A))

    # Seed straight through the store with no origin block, the way a
    # record from before this phase reads, with the home the migration
    # gives it.
    seeded = Store.open(memory_dir)
    legacy = seeded.write(
        content="kubernetes networking notes from before the feature shipped",
        scopes=["tools"],
        origin=None,
        home=RecordHome(GLOBAL, None, "global:no_origin"),
    )
    homeless = seeded.write(
        content="kubernetes networking notes with no home row",
        scopes=["tools"],
        origin=None,
    )

    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert any(h["id"] == legacy.id for h in hits)
    assert all(h["id"] != homeless.id for h in hits)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def test_search_records_auto_scope_and_its_surface(
    server_factory, memory_dir: Path, places: dict[str, Path]
) -> None:
    """The search row names the membership surface it delivered through
    (design 3.5); the origin columns decide nothing, so no repository
    filter is recorded."""
    server, _ = server_factory(_at(places["a"], REPO_A))
    await _call(server, "memory_search", query="anything")

    events = Store.open(memory_dir).iter_events()
    search_events = [e for e in events if e["kind"] == "search"]
    assert search_events
    e = search_events[-1]
    assert e["auto_scope"] is True
    assert e["membership_surface"] == SEARCH_AUTO_SCOPE
    assert "repo_filter" not in e
    assert "membership" not in e


async def test_search_with_auto_scope_false_records_the_unscoped_surface(
    server_factory, memory_dir: Path, places: dict[str, Path]
) -> None:
    server, _ = server_factory(_at(places["a"], REPO_A))
    await _call(server, "memory_search", query="anything", auto_scope=False)
    events = Store.open(memory_dir).iter_events()
    search_events = [e for e in events if e["kind"] == "search"]
    e = search_events[-1]
    assert e["auto_scope"] is False
    assert e["membership_surface"] == SEARCH_UNSCOPED
    assert "repo_filter" not in e


# ---------------------------------------------------------------------------
# Checkout-path lifecycle: `origin.worktree_root` is an absolute path
# frozen at write time, and the retrieval surface must not go dark when
# that path stops describing where the project lives. Delivery reads the
# record's home, a project, never the recorded path (design 3.5; design 4,
# failures 1 and 3).
#
# The assertions are about what `memory_search` actually returns, because
# the failure this guards is a SILENT one: an empty result set carries no
# signal that a filter dropped everything.
# ---------------------------------------------------------------------------

_GIT_AVAILABLE = shutil.which("git") is not None
_REMOTE = "git@github.com:example/myapp.git"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_project_memories_survive_a_checkout_move(
    server_factory, tmp_path: Path
) -> None:
    """Rename the project directory and the project's memories still
    surface under auto_scope.

    The recorded `worktree_root` is the pre-move absolute path. The
    record's home is the project, and the moved folder is the same
    checkout (the same git directory, witness O), so the caller at the
    new path stands in the project the record is homed in. Without that
    every memory written before an ordinary `mv` would drop out of every
    auto-scoped search for the project, with an empty result set as the
    only symptom.
    """
    from bettermemory.origin import _primary_root_of, capture

    old = tmp_path / "projects" / "myapp"
    new = tmp_path / "Documents" / "projects" / "myapp"
    checkout(old, remote=_REMOTE)

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
    # Same repository, different absolute root: the exact shape a
    # path-keyed check would have excluded.
    assert captured["value"].repo == _REMOTE
    assert captured["value"].worktree_root == str(new.resolve())

    after = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in after] == [written["id"]]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_synced_memory_from_another_machine_surfaces_locally(
    server_factory, memory_dir: Path, tmp_path: Path
) -> None:
    """A memory written on another machine carries that machine's
    absolute `worktree_root`, which cannot exist locally, and is homed in
    that machine's checkout of the project: it still surfaces in a local
    clone of the repository, which is the same project (D1: other
    machines of one project share its memories).

    Distinct from the move case in that the path was never local to
    begin with, and the home names a checkout of another machine.
    """
    from bettermemory.origin import _primary_root_of, capture

    foreign = "/home/ci-user/projects/myapp"
    laptop = checkout(tmp_path / "laptop" / "myapp", remote=_REMOTE)
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store.open(memory_dir)
    there = Membership(store, "machine-laptop").resolve(
        location.capture_location(
            str(laptop), machine="machine-laptop", session=None, config=config
        )
    )
    assert there.home is not None
    written = store.write(
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
        origin=Origin(cwd=foreign, repo=_REMOTE, branch="main", worktree_root=foreign),
        home=there.home,
    )

    local = tmp_path / "Documents" / "projects" / "myapp"
    local.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(laptop), str(local)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", _REMOTE],
        cwd=local,
        check=True,
        capture_output=True,
    )
    _primary_root_of.cache_clear()
    server, captured = server_factory(capture(cwd=local))
    assert captured["value"].worktree_root == str(local.resolve())

    hits = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in hits] == [written.id]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_second_live_checkout_of_one_repo_shares_and_a_stranger_does_not(
    server_factory, tmp_path: Path
) -> None:
    """The negative control for the two tests above, and the boundary of
    what sharing covers.

    A second live clone of the repository is the same project (D1:
    clones of one project share its memories), so a memory written in
    the first surfaces in the second while both exist. A repository
    that only names the same remote URL, with none of the project's
    commits, is not taken for it on that one witness: the caller is
    asked, and until it answers nothing from another project is shown
    there (design 3.2). So sharing is keyed on the checkout being the
    project's, never on "the remote URL matches".
    """
    from bettermemory.origin import _primary_root_of, capture

    first = tmp_path / "clones" / "myapp"
    second = tmp_path / "clones" / "myapp-review"
    stranger = tmp_path / "clones" / "myapp-stranger"
    checkout(first, remote=_REMOTE)
    subprocess.run(
        ["git", "clone", "-q", str(first), str(second)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", _REMOTE],
        cwd=second,
        check=True,
        capture_output=True,
    )
    checkout(stranger, remote=_REMOTE)

    _primary_root_of.cache_clear()
    server, captured = server_factory(capture(cwd=first))
    written = await _call(
        server,
        "memory_write",
        content="myapp hashes passwords with bcrypt at cost factor 12",
        scopes=["projects:myapp"],
    )

    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=second)
    assert captured["value"].repo == _REMOTE
    assert first.exists()
    shared = _unwrap(await _call(server, "memory_search", query="bcrypt"))
    assert [h["id"] for h in shared] == [written["id"]]

    _primary_root_of.cache_clear()
    captured["value"] = capture(cwd=stranger)
    assert captured["value"].repo == _REMOTE
    assert _unwrap(await _call(server, "memory_search", query="bcrypt")) == []
