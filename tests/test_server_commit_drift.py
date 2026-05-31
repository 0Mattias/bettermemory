"""Integration tests for the commit-drift staleness signal on the server.

`compute_commit_drift` and the `CommitDriftDebt` rollup are unit-tested in
`test_verify.py` and `test_health_commit_drift.py` respectively. These
tests cover the wiring on the MCP tools — that `memory_show`,
`memory_search(expand_top=True)`, and `memory_health` actually surface
the signal end-to-end against a real git repo and a real memory store.

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


@pytest.fixture
def server_with_fake_origin(memory_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a server whose `capture_origin` returns whatever the test
    chooses. Mirrors the pattern in `test_server_origin.py` so the test
    can simulate "caller is in repo X" without altering the process cwd.

    The fake origin's `cwd` is a real tmp path the test sets up as a git
    repo — `commits_since` and friends actually shell out against it, so
    the integration covers the real subprocess code path even though the
    capture step is mocked.

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
        server, "memory_write", content="durable memory body", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"], note="initial check")

    # Now add commits with timestamps in the far future, guaranteed to be
    # newer than the verify timestamp regardless of when the test runs.
    _commit_at(repo, "post-verify-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-verify-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

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
        server, "memory_write", content="durable memory body", scopes=["tools"]
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
        content="quokka migration ritual gathers attention",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_at(repo, "after", when=datetime(2099, 1, 1, tzinfo=timezone.utc))

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
        content="durable thing about widgets",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_at(repo, "post-1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "post-2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))

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
        server, "memory_write", content="alpha widget", scopes=["tools"]
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

    a = await _call(server, "memory_write", content="alpha widget", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta widget", scopes=["tools"])
    await _call(server, "memory_verify", id=a["id"])
    await _call(server, "memory_verify", id=b["id"])

    # Three commits after the verify: both memories should report drift=3.
    _commit_at(repo, "c1", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    _commit_at(repo, "c2", when=datetime(2099, 2, 1, tzinfo=timezone.utc))
    _commit_at(repo, "c3", when=datetime(2099, 3, 1, tzinfo=timezone.utc))

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
        server, "memory_write", content="alpha widget", scopes=["tools"]
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
