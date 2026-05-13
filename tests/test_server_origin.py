"""Integration tests: origin capture on write, auto-scope filter on search.

We monkeypatch `bettermemory.origin.capture` so tests can choose what the
"current" origin looks like at write and search time, without setting up
a fake git repo for every case.
"""

from __future__ import annotations

import json
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
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


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
