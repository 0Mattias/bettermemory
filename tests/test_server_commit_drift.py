"""Integration tests for the commit-drift staleness signal on the server.

`compute_commit_drift` is unit-tested in `test_verify.py`. The
`CommitDriftDebt` rollup has no unit-test module of its own — it is
covered here, through `memory_health`, alongside the rest of the
wiring. These tests cover that wiring on the MCP tools — that
`memory_show`, `memory_search(expand_top=True)`, and `memory_health`
actually surface the signal end-to-end against a real git repo and a
real memory store.

Each test sets up a tmp repo with controlled commit timestamps, tells the
server's `capture_origin` to return an Origin pointing at that repo, and
asserts on the JSON the tools return.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


_GIT_AVAILABLE = shutil.which("git") is not None
_REMOTE = "git@github.com:example/foo.git"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, remote: str = _REMOTE) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _commit_at(path: Path, message: str, *, when: datetime) -> None:
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _commit_touching(
    path: Path, message: str, *, when: datetime, filename: str = "notes.md"
) -> None:
    """Commit that TOUCHES a file — the claim-anchored drift policy only
    counts commits touching a memory's cited/attested paths, so
    drift-expecting fixtures must move the cited file, not just HEAD
    (`_commit_at`'s --allow-empty commits are invisible to the filter)."""
    target = path / filename
    with target.open("a") as fh:
        fh.write(f"{message}\n")
    subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


def _commit_split(
    path: Path,
    message: str,
    *,
    author_when: datetime,
    committer_when: datetime,
) -> None:
    """Commit with DIFFERENT author and committer dates — the on-disk shape
    a rebase leaves behind (rebase preserves author date, rewrites committer
    date). Used to pin the commit-drift unification: the author-timestamp
    path memory_show/search/health share must ignore the rewritten committer
    date, where the old `git rev-list --since` (committer-date) path counted
    it as phantom drift.
    """
    author_iso = author_when.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    committer_iso = committer_when.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = author_iso
    env["GIT_COMMITTER_DATE"] = committer_iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )


@pytest.fixture
def server_with_fake_origin(memory_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a server whose `capture_origin` returns whatever the test
    chooses. Mirrors the pattern in `test_server_origin.py` so the test
    can simulate "caller is in repo X" without altering the process cwd.

    The fake origin's `cwd` is a real tmp path the test sets up as a git
    repo — `commit_author_timestamps` and friends actually shell out
    against it, so the integration covers the real subprocess code path
    even though the capture step is mocked.

    Patching goes through `monkeypatch` (not bare `setattr`) so the mock is
    RESTORED at teardown. A bare setattr leaks the patched `capture_origin`
    into later tests in the same session — e.g.
    `test_server_v12_features.py::test_memory_show_verdict_fresh_after_verify`,
    which relies on the real capture_origin — flipping their staleness
    verdict. That cross-file flake passes in isolation but fails in a
    multi-file run (and did so identically on main before this fix).
    """
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    def make(origin: Origin):
        # `capture_origin` is imported into `_handlers` where the tool
        # handler bodies live, and into `server` for `_cli_health`. Patch
        # both bindings so a server built by either path sees the mock.
        import bettermemory._handlers as handlers_module
        import bettermemory.server as server_module

        rec = Recorder(root=memory_dir, session_id=state.session_id)
        server = build_server(
            config=cfg,
            store=Store(memory_dir),
            state=state,
            recorder=rec,
        )

        def fake_capture(cwd: Path | None = None) -> Origin:
            return origin

        monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
        monkeypatch.setattr(server_module, "capture_origin", fake_capture)
        return server

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
# memory_show
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_show_drift_when_commits_after_verify(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The load-bearing case: memory was verified, then commits landed.
    `commit_drift.status` is "drift" with a positive count and an
    actionable recommendation, even when verification.status is "fresh"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # One commit before any memory work — gives the repo a HEAD so verify
    # can be issued meaningfully. Timestamp doesn't matter; the verify
    # call will record "now".
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable memory body about notes.md",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"], note="initial check")

    # Now add commits touching the cited file, with timestamps in the far
    # future — guaranteed newer than the verify timestamp regardless of
    # when the test runs, and visible to the claim-anchored filter.
    _commit_touching(
        repo, "post-verify-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc)
    )
    _commit_touching(
        repo, "post-verify-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc)
    )

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["commit_drift"] is not None
    assert shown["commit_drift"]["status"] == "drift"
    assert shown["commit_drift"]["commits_since_verify"] == 2
    assert "memory_verify" in shown["commit_drift"]["recommendation"]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_show_clean_when_no_commits_after_verify(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Repo matches, memory verified, no commits since: the bucket
    surfaces `clean` so the consumer can see we checked. Distinct from
    the null branch ("nothing to say")."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # Far-past commit only — verify will record "now", so commits_since_verify is 0.
    _commit_at(repo, "ancient", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable memory body about notes.md",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["commit_drift"] is not None
    assert shown["commit_drift"]["status"] == "clean"
    assert shown["commit_drift"]["commits_since_verify"] == 0
    assert shown["commit_drift"]["recommendation"] is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_show_null_when_caller_not_in_matching_repo(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Memory was written from repo A; caller is in repo B. The signal
    stays null — we have no checkout of A to count against."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    _init_repo(repo_a, remote=_REMOTE)
    _commit_at(repo_a, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    other_remote = "git@github.com:example/other.git"
    _init_repo(repo_b, remote=other_remote)
    _commit_at(repo_b, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    # Write the memory while "in" repo A.
    server_a = server_with_fake_origin(
        Origin(cwd=str(repo_a), repo=_REMOTE, branch="main")
    )
    written = await _call(server_a, "memory_write", content="durable", scopes=["tools"])
    await _call(server_a, "memory_verify", id=written["id"])

    # Now show while "in" repo B — fresh server with a different fake origin.
    server_b = server_with_fake_origin(
        Origin(cwd=str(repo_b), repo=other_remote, branch="main")
    )
    shown = await _call(server_b, "memory_show", id=written["id"])
    assert shown["commit_drift"] is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_show_null_when_memory_never_verified(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """No verify has been issued yet — verification.status is "never",
    which already maxes the alarm. Adding a commit-drift signal would be
    duplicate noise; the null branch keeps the response shape clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(server, "memory_write", content="durable", scopes=["tools"])
    # Deliberately skip memory_verify.

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["verification"]["status"] == "never"
    assert shown["commit_drift"] is None


# ---------------------------------------------------------------------------
# memory_search expand_top
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_expand_top_includes_commit_drift_on_drift(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The expanded top hit should carry commit_drift parallel to
    path_drift — single round-trip triage, no follow-up memory_show."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    # Write a memory whose body is unique enough to reach "high" relevance
    # on a targeted search.
    written = await _call(
        server,
        "memory_write",
        content="quokka migration ritual gathers attention in notes.md",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_touching(repo, "after", when=datetime(2099, 1, 1, tzinfo=timezone.utc))

    raw = await _call(
        server, "memory_search", query="quokka migration ritual", expand_top=True
    )
    hits = _unwrap(raw)
    assert hits and hits[0]["relevance"] == "high"
    # Top hit got expanded, so commit_drift is attached.
    assert "commit_drift" in hits[0]
    assert hits[0]["commit_drift"]["status"] == "drift"
    assert hits[0]["commit_drift"]["commits_since_verify"] == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_non_expanded_hits_do_not_carry_full_commit_drift(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The full `commit_drift` block (status / recommendation) stays
    opt-in via expand_top, mirroring `path_drift` — the lightweight
    `commit_drift_count` integer is the per-hit triage signal instead."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable thing about widgets",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])

    raw = await _call(
        server, "memory_search", query="widgets durable", expand_top=False
    )
    hits = _unwrap(raw)
    assert hits
    for hit in hits:
        assert "commit_drift" not in hit


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_carry_commit_drift_count_when_drifted(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The cheap per-hit signal: `commit_drift_count` is attached to each
    hit whose memory matches the caller's repo and has been verified.
    Lets the model self-triage which hit to expand without a memory_show
    round-trip — parallel to `path_drift_missing`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable thing about widgets in notes.md",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_touching(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_touching(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

    raw = await _call(
        server, "memory_search", query="widgets durable", expand_top=False
    )
    hits = _unwrap(raw)
    assert hits
    # Exactly one matching memory; it should carry the count.
    target = next(h for h in hits if h["id"] == written["id"])
    assert target["commit_drift_count"] == 2


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_commit_drift_count_honors_verified_paths(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Regression: the per-hit commit_drift_count ignored verified_paths, so a
    memory the user attested as stable for a specific path still read as
    drifted / spot_check_recommended on the loud search surface — defeating
    the feature there and disagreeing with memory_show. With the fix, a hit
    whose verified_paths were untouched by the post-verify commits reads
    commit_drift_count=0 / fresh, narrowed exactly like memory_show. (Compare
    the test above: identical setup minus verified_paths gives count == 2.)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # A real tracked file the memory will be verified against.
    stable = repo / "stable.py"
    stable.write_text("x = 1\n")
    subprocess.run(
        ["git", "add", "stable.py"], cwd=repo, check=True, capture_output=True
    )
    _commit_at(repo, "add stable.py", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="durable thing about widgets", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"], verified_paths=[str(stable)])
    # Post-verify commits that do NOT touch stable.py (empty commits).
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

    raw = await _call(
        server, "memory_search", query="widgets durable", expand_top=False
    )
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    # Narrowed to the verified path (untouched by the post-verify commits) -> 0,
    # not the unfiltered 2 — and the verdict is fresh, matching memory_show.
    assert target["commit_drift_count"] == 0
    assert target["staleness_verdict"] == "fresh"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_carry_zero_commit_drift_count_when_clean(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """A hit anchored to the matching repo with no commits since verify
    carries `commit_drift_count: 0` — positive evidence the calendar
    verification still reflects reality, distinct from "field absent"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "ancient", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="alpha widget in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])

    raw = await _call(server, "memory_search", query="alpha widget", expand_top=False)
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    assert target["commit_drift_count"] == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_omit_commit_drift_count_when_caller_outside_repo(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """When the caller isn't in any repo, the field is OMITTED from
    every hit (not set to null, not zero). Absence-as-signal mirrors
    the path_drift contract and keeps the hit shape uniform — a
    consumer can branch on `'commit_drift_count' in hit` cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    # Write while "in" the repo so the memory has an origin.
    server_in_repo = server_with_fake_origin(
        Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    )
    written = await _call(
        server_in_repo, "memory_write", content="durable beta", scopes=["tools"]
    )
    await _call(server_in_repo, "memory_verify", id=written["id"])

    # Now search while "not in any repo" — Origin with cwd but no repo.
    server_no_repo = server_with_fake_origin(
        Origin(cwd=str(tmp_path), repo=None, branch=None)
    )
    raw = await _call(
        server_no_repo,
        "memory_search",
        query="beta durable",
        expand_top=False,
        auto_scope=False,
    )
    hits = _unwrap(raw)
    assert hits
    for hit in hits:
        assert "commit_drift_count" not in hit


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_omit_commit_drift_count_when_never_verified(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """A hit with no last_verified_at has no anchor — the field is
    omitted (verification.status="never" already maxes the alarm, no
    point duplicating)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="unverified gamma", scopes=["tools"]
    )
    # Deliberately skip memory_verify.

    raw = await _call(
        server, "memory_search", query="gamma unverified", expand_top=False
    )
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    assert target["verification"]["status"] == "never"
    assert "commit_drift_count" not in target


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_drift_count_git_cost_shape(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The COST paragraph on `attach_commit_drift_counts`, made checkable.

    The docstring used to promise "one `commit_author_timestamps` call for
    the whole search … independent of result count", which was false: every
    hit with drift to narrow forks a second, path-filtered `git log` inside
    `resolve_commit_drift_count`. That mattered because a cost contract
    nobody can check invites the next author to add per-hit work believing
    the loop is free. So the real arithmetic is pinned here instead:
    `2 + <drifting anchored hits>` git processes, with the two gates
    (count > 0, anchors present) holding the ordinary shapes at 2.
    """
    from bettermemory import origin as origin_module
    from bettermemory._response import ResponseBuilder
    from bettermemory.search import search as run_search

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    anchors = [repo / f"notes{i}.md" for i in range(3)]
    for path in anchors:
        path.write_text("anchor\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", path.name], cwd=repo, check=True, capture_output=True
        )
    _commit_at(repo, "seed", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    store = Store(memory_dir)
    for i, path in enumerate(anchors):
        memory = store.write(
            content=f"widget rule number {i} lives in {path}",
            scopes=["tools"],
            origin=origin,
        )
        store.mark_verified(memory.id)
    # One post-verify commit touching every anchor, so all three hits have
    # drift to narrow and each reaches `resolve_commit_drift_count`.
    for path in anchors:
        path.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    _commit_touching(
        repo,
        "post",
        when=datetime(2099, 1, 1, tzinfo=timezone.utc),
        filename="extra.md",
    )

    def count_git_calls(bodies_cite_paths: bool) -> tuple[int, int]:
        memories = store.load_all()
        if not bodies_cite_paths:
            # Strip the citations in memory only: same hits, same repo, same
            # verify timestamps — but no claim anchors, so the untethered
            # gate short-circuits before any per-hit git work.
            memories = [
                m.model_copy(update={"body": f"widget rule number {i} lives here"})
                for i, m in enumerate(memories)
            ]
        hits = run_search(memories, "widget rule", max_results=50)
        builder = ResponseBuilder(stale_after_days=30)
        now = datetime.now(timezone.utc)
        out = [builder.hit_to_dict(h, now=now) for h in hits]
        calls: list[tuple[str, ...]] = []
        real_git = origin_module._git

        def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
            calls.append(args)
            return real_git(cwd, *args, **kwargs)

        monkeypatch.setattr(origin_module, "_git", spy)
        try:
            builder.attach_commit_drift_counts(
                out, hits, memories, caller_origin=origin
            )
        finally:
            monkeypatch.setattr(origin_module, "_git", real_git)
        annotated = sum(1 for hit in out if "commit_drift_count" in hit)
        # The two per-search calls, in order: the unfiltered author-date log
        # and the one repo-root resolution the per-hit narrowing reuses.
        assert calls[0][:2] == ("log", "--format=%aI")
        assert calls[1][:2] == ("rev-parse", "--show-toplevel")
        return len(calls), annotated

    forks, annotated = count_git_calls(bodies_cite_paths=True)
    assert annotated == 3, "fixture must produce three drifting anchored hits"
    assert forks == 2 + annotated

    # Same three hits, no claim anchors: the gate keeps the per-hit path
    # closed, so the cost falls back to the two per-search calls.
    forks, annotated = count_git_calls(bodies_cite_paths=False)
    assert annotated == 0
    assert forks == 2


# ---------------------------------------------------------------------------
# memory_health
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_lists_drifted_rows(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """memory_health surfaces a `commit_drift_debt` rollup when the server
    is in a repo whose memories are anchored to it — drifted rows are
    listed with their commit count, sorted most-commits-ahead first."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    a = await _call(
        server, "memory_write", content="alpha widget in notes.md", scopes=["tools"]
    )
    b = await _call(
        server, "memory_write", content="beta widget in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=a["id"])
    await _call(server, "memory_verify", id=b["id"])

    # Three commits touching the cited file after the verify: both
    # memories should report drift=3.
    _commit_touching(repo, "c1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_touching(repo, "c2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))
    _commit_touching(repo, "c3", when=datetime(2099, 3, 1, tzinfo=timezone.utc))

    report = await _call(server, "memory_health")
    cd = report["commit_drift_debt"]
    assert cd is not None
    assert cd["current_repo"] == _REMOTE
    assert cd["current_cwd"] == str(repo)
    assert cd["total_drifted"] == 2
    counts = sorted(r["commits_since_verify"] for r in cd["rows"])
    assert counts == [3, 3]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_clean_when_caught_up(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Memory anchored to the repo, verified after the only commit:
    rollup emits an empty rows list with `total_drifted=0`. Distinct from
    the None branch — the consumer can tell "we checked, all clean" from
    "we couldn't check"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "old", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="alpha widget in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])

    report = await _call(server, "memory_health")
    cd = report["commit_drift_debt"]
    assert cd is not None
    assert cd["total_drifted"] == 0
    assert cd["rows"] == []


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_null_when_no_anchored_memories(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Caller is in a repo, but no memory in the store has an origin
    matching it. Suppress the rollup entirely — surfacing an empty
    bucket with a populated `current_repo` would be misleading."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    # Memory written from a different repo (we point the fake at a different
    # cwd / remote first).
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _init_repo(other_repo, remote="git@github.com:example/other.git")
    _commit_at(other_repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))
    server_other = server_with_fake_origin(
        Origin(
            cwd=str(other_repo),
            repo="git@github.com:example/other.git",
            branch="main",
        )
    )
    written = await _call(
        server_other, "memory_write", content="durable", scopes=["tools"]
    )
    await _call(server_other, "memory_verify", id=written["id"])

    # Now run memory_health from the original repo, which has no anchored memory.
    server = server_with_fake_origin(Origin(cwd=str(repo), repo=_REMOTE, branch="main"))
    report = await _call(server, "memory_health")
    assert report["commit_drift_debt"] is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_null_when_caller_not_in_repo(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Server isn't in any repo (Origin with cwd but no repo) — the
    rollup is meaningless without a project anchor."""
    server = server_with_fake_origin(Origin(cwd=str(tmp_path), repo=None, branch=None))

    await _call(server, "memory_write", content="durable", scopes=["tools"])

    report = await _call(server, "memory_health")
    assert report["commit_drift_debt"] is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_honors_verified_paths(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Regression: the `commit_drift_debt` rollup ignored verified_paths, so
    a memory the user attested as stable for a specific path still counted
    as drifted on the loud `memory_health` surface — nagging on a stable
    memory and disagreeing with memory_show / memory_search. With the fix, a
    memory whose verified_paths were untouched by the post-verify commits
    drops out of the rollup (total_drifted=0), narrowed exactly like the
    per-hit surfaces. (Compare `..._lists_drifted_rows`: identical setup
    minus verified_paths gives total_drifted=2.)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    stable = repo / "stable.py"
    stable.write_text("x = 1\n")
    subprocess.run(
        ["git", "add", "stable.py"], cwd=repo, check=True, capture_output=True
    )
    _commit_at(repo, "add stable.py", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="alpha widget", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"], verified_paths=[str(stable)])
    # Post-verify commits that do NOT touch stable.py (empty commits).
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

    report = await _call(server, "memory_health")
    cd = report["commit_drift_debt"]
    assert cd is not None
    # Narrowed to the verified path (untouched) -> not the unfiltered 2.
    assert cd["total_drifted"] == 0
    assert cd["rows"] == []


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_scope_overview_curation_drifted_honors_verified_paths(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Regression: `curation_counts` (which drives `memory_scope_overview`'s
    `curation_pending.drifted`) was the only commit-drift surface that never
    applied the verified_paths filter — so the session-start hint reported
    drift on memories attested as stable, the loudest false-positive of all.
    With the fix, a memory whose verified_paths were untouched by post-verify
    commits is not counted as drifted in the session-start rollup.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    stable = repo / "stable.py"
    stable.write_text("x = 1\n")
    subprocess.run(
        ["git", "add", "stable.py"], cwd=repo, check=True, capture_output=True
    )
    _commit_at(repo, "add stable.py", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="alpha widget", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"], verified_paths=[str(stable)])
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

    res = await _call(server, "memory_scope_overview")
    assert res["curation_pending"]["drifted"] == 0


# ---------------------------------------------------------------------------
# Cross-surface agreement on the commit-drift count
# ---------------------------------------------------------------------------
#
# memory_show, memory_search and memory_health each compute commit drift
# from the caller's checkout. They must agree on the count for the SAME
# memory, or the product's headline staleness guarantee tells the model
# three different things depending on which tool it reaches for. Two
# independent ways they could disagree are pinned here:
#
#   1. date source — memory_show used to count via `git rev-list --since`
#      (COMMITTER date), while search/health bisect over `%aI` AUTHOR
#      timestamps. A rebase preserves author date but rewrites committer
#      date (and `sync` rebases on every pull), so the same memory read
#      drifted on memory_show yet clean on memory_search.
#   2. boundary — `git rev-list --since` is INCLUSIVE and whole-second;
#      the bisect path is strictly-greater at microsecond precision. A
#      commit landing in the same UTC second as `last_verified_at` (but
#      microseconds before it) counted as drift on memory_show alone,
#      with ZERO rebases involved.
#
# Both are closed by routing memory_show's `compute_commit_drift` onto the
# same `commit_author_timestamps` + `bisect_right` path the other two use.


async def _drift_counts_for(
    server: Any, memory_id: str
) -> tuple[int | None, int | None, int | None]:
    """Return `(show_count, search_count, health_count)` for `memory_id`.

    Each element is the commit-drift count that surface reports, or None
    when that surface omits the signal entirely (which is itself a form of
    agreement to assert on). `health_count` reads the row out of
    `commit_drift_debt`: a row exists only for a drifted memory, so a
    caught-up memory (no row) reports 0, mirroring how the per-hit
    search count and `memory_show.commit_drift` report 0 on `clean`.
    """
    shown = await _call(server, "memory_show", id=memory_id)
    show_count: int | None = (
        shown["commit_drift"]["commits_since_verify"]
        if shown.get("commit_drift") is not None
        else None
    )

    raw = await _call(
        server, "memory_search", query="widgets durable", expand_top=False
    )
    hits = _unwrap(raw)
    search_count: int | None = None
    for hit in hits:
        if hit["id"] == memory_id:
            search_count = hit.get("commit_drift_count")
            break

    report = await _call(server, "memory_health")
    cd = report["commit_drift_debt"]
    health_count: int | None = None
    if cd is not None:
        row = next((r for r in cd["rows"] if r["id"] == memory_id), None)
        # A drifted memory has a row; a caught-up one doesn't (count 0),
        # matching the 0 the other two surfaces report on `clean`.
        health_count = row["commits_since_verify"] if row is not None else 0
    return show_count, search_count, health_count


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_commit_drift_count_agrees_across_surfaces_after_rebase(
    server_with_fake_origin, memory_dir: Path, tmp_path: Path
) -> None:
    """Rebase axis: a commit AUTHORED before the verify but COMMITTED after
    it (what a rebase leaves on disk) must not count as drift on any
    surface — the work predates the verification. The old memory_show path
    counted committer date and reported phantom drift; with the unification
    all three surfaces read the author date and agree on 0.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable thing about widgets in notes.md",
        scopes=["tools"],
    )
    # Verify stamps "now" (real wall-clock), which is after the 2025 author
    # dates below and before the rewritten-into-the-future committer date.
    await _call(server, "memory_verify", id=written["id"])

    # The rebase shape: authored in early 2025 (before the verify), committer
    # date rewritten far into the future (after the verify). Author-date
    # counting must treat this as NOT drift; committer-date counting (the old
    # memory_show path) would have flagged it.
    _commit_split(
        repo,
        "rebased commit",
        author_when=datetime(2025, 1, 2, tzinfo=timezone.utc),
        committer_when=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )

    show_count, search_count, health_count = await _drift_counts_for(
        server, written["id"]
    )
    # All three agree, and agree on the CORRECT author-date answer: the
    # commit's work predates the verify, so it isn't drift.
    assert show_count == 0
    assert search_count == 0
    assert health_count == 0
    assert show_count == search_count == health_count


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_commit_drift_count_agrees_across_surfaces_same_second_boundary(
    server_with_fake_origin, memory_dir: Path, tmp_path: Path
) -> None:
    """Boundary axis (zero rebases): a commit whose author timestamp lands in
    the SAME UTC second as `last_verified_at`, but strictly before it at
    sub-second precision, must not count as drift. The old memory_show path
    (`git rev-list --since`, inclusive + whole-second) counted it; the
    strictly-greater microsecond bisect the other surfaces use does not.
    After the unification all three report 0.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "anchor", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="durable thing about widgets in notes.md",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])

    # Read the verify timestamp back off disk (microseconds preserved) and
    # place a commit at the WHOLE-SECOND FLOOR of it. That commit is <=
    # last_verified_at, so the strictly-greater bisect path excludes it (0
    # drift), while `git rev-list --since` truncates last_verified_at to the
    # same whole second and counts the commit INCLUSIVELY (the old
    # memory_show divergence this fix removes).
    stored = Store(memory_dir).load_one(written["id"])
    verified_at = stored.last_verified_at
    assert verified_at is not None
    floor_second = verified_at.replace(microsecond=0)
    _commit_at(repo, "same-second-as-verify", when=floor_second)

    show_count, search_count, health_count = await _drift_counts_for(
        server, written["id"]
    )
    # A commit at-or-before the verify instant is not drift on the
    # strictly-greater boundary — all three surfaces report 0 in lockstep.
    assert show_count == 0
    assert search_count == 0
    assert health_count == 0
    assert show_count == search_count == health_count


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_verified_paths_does_not_resurrect_same_second_commit(
    server_with_fake_origin, memory_dir: Path, tmp_path: Path
) -> None:
    """The verified-paths narrowing must NEVER turn a clean author-bisect
    count (0) back into drift. `attach_commit_drift_counts` narrows through
    `verify.resolve_commit_drift_count`, which maps the anchors to
    repo-relative pathspecs (`resolve_repo_pathspecs`) and bisects the
    touching commits' AUTHOR timestamps
    (`commit_author_timestamps_touching_pathspecs`) on the same
    strictly-greater boundary as the unfiltered bisect — a commit that
    TOUCHES a verified path in the same UTC second as `last_verified_at`
    is excluded by the narrowing exactly as the authoritative unfiltered
    count excludes it. On top of that, the `count > 0` guard (mirroring
    memory_show / the health rollups — the four narrowing sites must gate
    identically) skips the narrowing entirely when the bisect already said
    clean. Historically the narrowing ran through the committer-date
    INCLUSIVE whole-second `commits_since_touching_paths` (now deprecated,
    zero production callers), which DID count exactly this commit:
    pre-guard this hit read commit_drift_count=1. With the guard — and the
    author-date unification beneath it — it reads 0 / fresh, in lockstep
    with memory_show.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    stable = repo / "stable.py"
    stable.write_text("x = 1\n")
    subprocess.run(
        ["git", "add", "stable.py"], cwd=repo, check=True, capture_output=True
    )
    _commit_at(repo, "add stable.py", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server, "memory_write", content="durable thing about widgets", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"], verified_paths=[str(stable)])

    # A commit that TOUCHES the verified path, placed at the whole-second
    # FLOOR of last_verified_at (<= the verify instant). The author-date
    # bisect excludes it (unfiltered count 0); the deprecated committer-date
    # INCLUSIVE narrowing counted exactly this shape as phantom drift.
    stored = Store(memory_dir).load_one(written["id"])
    assert stored.last_verified_at is not None
    floor_second = stored.last_verified_at.replace(microsecond=0)
    stable.write_text("x = 2\n")
    subprocess.run(
        ["git", "add", "stable.py"], cwd=repo, check=True, capture_output=True
    )
    iso = floor_second.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_DATE": iso,
            "GIT_COMMITTER_DATE": iso,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    subprocess.run(
        ["git", "commit", "-m", "touch stable.py same-second-as-verify"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )

    raw = await _call(
        server, "memory_search", query="widgets durable", expand_top=False
    )
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    # The narrowing must not resurrect the same-second commit the bisect
    # already excluded — stays 0 / fresh, in lockstep with memory_show.
    assert target["commit_drift_count"] == 0
    assert target["staleness_verdict"] == "fresh"


# ---------------------------------------------------------------------------
# Claim-anchored exemption — a memory citing no paths cannot commit-drift
# (measured 100% false-positive before the gate: 12/12 at 3.13.0, 24/24 at
# 3.16.0). The untethered class: preferences, lessons, strategy notes,
# reflections that merely ORIGINATED in the repo.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_show_commit_drift_null_for_untethered_memory(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """Commits landed since verify, but the memory cites no paths — the
    signal is NOT APPLICABLE (null), not drift. The bare repo-wide count
    says nothing about a claim-less memory; calendar staleness remains
    its backstop. Reverting the claim-anchored gate makes this fail with
    a drift/2 block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="workflow preference: keep cost checkpoints on long runs",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["commit_drift"] is None
    # And the verdict stays fresh — the repo moving is not evidence
    # against a claim-less memory.
    assert shown["staleness_verdict"] == "fresh"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_omit_commit_drift_count_for_untethered(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The per-hit triage integer is omitted for an untethered memory —
    same absence-as-signal contract as the other not-applicable branches,
    and the verdict stays fresh on the loud search surface."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="workflow preference about delta reviews before merging",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))

    raw = await _call(
        server, "memory_search", query="delta reviews preference", expand_top=False
    )
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    assert "commit_drift_count" not in target
    assert target["staleness_verdict"] == "fresh"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_search_hits_omit_count_for_untethered_even_when_caught_up(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The count == 0 leg is what pins the anchors gate itself: with
    commits since verify (count > 0), omission is additionally protected
    by resolve_commit_drift_count's empty-anchors None, so only a
    caught-up untethered hit can distinguish gate-present (field
    omitted) from gate-bypassed (a stamped `commit_drift_count: 0`).
    Untethered means NOT APPLICABLE, never a reassuring zero — a zero
    would imply the claims were checked against commits, and there are
    no claims to check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # Only an ancient commit — the wall-clock verify below lands after
    # it, so the author-date bisect reads 0 for this memory.
    _commit_at(repo, "ancient", when=datetime(2020, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    written = await _call(
        server,
        "memory_write",
        content="workflow preference about epsilon retros after shipping",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])

    raw = await _call(
        server, "memory_search", query="epsilon retros preference", expand_top=False
    )
    hits = _unwrap(raw)
    target = next(h for h in hits if h["id"] == written["id"])
    assert "commit_drift_count" not in target
    assert target["staleness_verdict"] == "fresh"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_memory_health_commit_drift_debt_excludes_untethered(
    server_with_fake_origin, tmp_path: Path
) -> None:
    """The rollup counts only claim-anchored memories: an anchored memory
    whose cited file was touched drifts; an untethered memory verified at
    the same instant does not appear at all. This is the health-surface
    half of the claim-kind policy (the dogfood 24-row pile was 100%
    untethered noise)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_at(repo, "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)

    anchored = await _call(
        server, "memory_write", content="alpha widget in notes.md", scopes=["tools"]
    )
    untethered = await _call(
        server,
        "memory_write",
        content="workflow preference: rotate strategies when audits plateau",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=anchored["id"])
    await _call(server, "memory_verify", id=untethered["id"])

    _commit_touching(repo, "c1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))

    report = await _call(server, "memory_health")
    cd = report["commit_drift_debt"]
    assert cd is not None
    assert cd["total_drifted"] == 1
    assert [r["id"] for r in cd["rows"]] == [anchored["id"]]

    # The curation rollup agrees — scope_overview's `drifted` counts only
    # the anchored memory (lockstep between the two health surfaces).
    overview = await _call(server, "memory_scope_overview")
    assert overview["curation_pending"]["drifted"] == 1
