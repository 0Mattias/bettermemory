"""Integration tests for claims-at-write on the MCP tools.

The oracle and the wire syntax are unit-tested in `test_claims.py`; the
detector is exercised through `tests/test_bench_rot.py` (the bench
imports the shipped functions). What this module owns is the WIRING:
that `memory_write` refuses a false claim and stores a true one, that
`memory_verify` re-checks stored claims before stamping, that a body
edit clears them, and — the reason the feature exists — that the
commit-drift leg narrows to claimed bindings end-to-end against a real
git repo: method-body churn in a claimed file reads `clean` where the
per-file incumbent nagged, and an edit to the claimed binding itself
reads `drift` with the claim named.

Fixture pattern follows `test_server_commit_drift.py`: a tmp repo with
controlled author dates, `capture_origin` monkeypatched to point at it,
assertions on the JSON the tools return.
"""

from __future__ import annotations

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

from ._mcp import call_tool as _mcp_call

_GIT_AVAILABLE = shutil.which("git") is not None
_REMOTE = "git@github.com:example/claims.git"

pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")


MODULE_SOURCE = '''\
"""Module under claim."""

TIMEOUT = 30


def handler():
    return 1


def other():
    return 1
'''


def _git(repo: Path, *args: str, when: datetime | None = None) -> None:
    env = os.environ.copy()
    if when is not None:
        iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env["GIT_AUTHOR_DATE"] = iso
        env["GIT_COMMITTER_DATE"] = iso
    env.setdefault("GIT_AUTHOR_NAME", "Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(MODULE_SOURCE)
    (repo / "notes.md").write_text("notes\n")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "remote", "add", "origin", _REMOTE)
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-m",
        "initial",
        when=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return repo


def _edit_and_commit(
    repo: Path, rel: str, old: str, new: str, message: str, *, when: datetime
) -> None:
    target = repo / rel
    target.write_text(target.read_text().replace(old, new))
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message, when=when)


# A wall-clock-safe "after the verify" instant: the verify stamps real
# now, so drift-side commits are authored far in the future. Author-date
# space makes this exact — no sleeping, no clock coupling.
_FUTURE = datetime(2030, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def server_in_repo(memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A server whose `capture_origin` reports the tmp repo — worktree
    root included, which is what the write-side claim oracle resolves
    against (`test_server_commit_drift.py`'s fixture predates
    `worktree_root` and omits it; claims cannot)."""
    repo = _make_repo(tmp_path)
    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
    monkeypatch.setattr(server_module, "capture_origin", fake_capture)
    return server, repo


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def _write_claimed(server: Any, *claims: str) -> str:
    written = await _call(
        server,
        "memory_write",
        content="The drift gate lives in `pkg/mod.py` beside notes.md.",
        scopes=["tools"],
        claims=list(claims),
    )
    assert written["status"] == "committed"
    return written["id"]


# ---------------------------------------------------------------------------
# Write-side gate
# ---------------------------------------------------------------------------


async def test_write_stores_true_claims_and_show_returns_them(
    server_in_repo,
) -> None:
    server, _repo = server_in_repo
    memory_id = await _write_claimed(
        server, "pkg/mod.py::handler", "pkg/mod.py::TIMEOUT=30"
    )
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["claims"] == ["pkg/mod.py::handler", "pkg/mod.py::TIMEOUT=30"]


async def test_write_refuses_false_claim_with_reason(server_in_repo) -> None:
    server, _repo = server_in_repo
    with pytest.raises(Exception, match="do not hold"):
        await _call(
            server,
            "memory_write",
            content="Wrong claim about pkg/mod.py.",
            scopes=["tools"],
            claims=["pkg/mod.py::TIMEOUT=999"],
        )
    with pytest.raises(Exception, match="does not exist"):
        await _call(
            server,
            "memory_write",
            content="Claim on a file that is not there.",
            scopes=["tools"],
            claims=["pkg/gone.py"],
        )


async def test_write_refuses_claims_without_worktree(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worktree, no claims — the oracle has nothing to check against."""
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=Recorder(root=memory_dir, session_id=state.session_id),
    )

    def fake_capture(cwd: Path | None = None) -> Origin:
        return Origin(cwd="/somewhere", repo=None, branch=None, worktree_root=None)

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
    monkeypatch.setattr(server_module, "capture_origin", fake_capture)

    with pytest.raises(Exception, match="worktree"):
        await _call(
            server,
            "memory_write",
            content="A body claiming code from nowhere.",
            scopes=["tools"],
            claims=["pkg/mod.py::handler"],
        )


async def test_verify_claims_fallback_for_legacy_origin_without_worktree(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-worktree_root record (`origin.repo` only) can still take
    claims at verify time when the caller sits in a matching checkout —
    the caller's tree speaks for the memory's, under the same
    `repos_match` rule the commit leg applies. The 3.40.0 backfill
    measured the population this unblocks: 8 of 128 repo-matched
    records refused solely for the missing worktree field."""
    repo = _make_repo(tmp_path)
    state = SessionState()
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=Recorder(root=memory_dir, session_id=state.session_id),
    )

    legacy = Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=None)

    def capture_legacy(cwd: Path | None = None) -> Origin:
        return legacy

    monkeypatch.setattr(handlers_module, "capture_origin", capture_legacy)
    monkeypatch.setattr(server_module, "capture_origin", capture_legacy)
    written = await _call(
        server,
        "memory_write",
        content="Legacy-era record citing pkg/mod.py from before worktree capture.",
        scopes=["tools"],
    )
    assert written["status"] == "committed"

    modern = Origin(cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo))

    def capture_modern(cwd: Path | None = None) -> Origin:
        return modern

    monkeypatch.setattr(handlers_module, "capture_origin", capture_modern)
    monkeypatch.setattr(server_module, "capture_origin", capture_modern)
    verified = await _call(
        server, "memory_verify", id=written["id"], claims=["pkg/mod.py::handler"]
    )
    assert verified["claims"] == ["pkg/mod.py::handler"]


# ---------------------------------------------------------------------------
# Verify-side: stored-claim re-check, REPLACE semantics
# ---------------------------------------------------------------------------


async def test_verify_refuses_when_stored_claim_went_false(
    server_in_repo,
) -> None:
    server, repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::TIMEOUT=30")
    verified = await _call(server, "memory_verify", id=memory_id)
    assert verified["claims"] == ["pkg/mod.py::TIMEOUT=30"]

    # The world moves: the constant changes value.
    _edit_and_commit(
        repo,
        "pkg/mod.py",
        "TIMEOUT = 30",
        "TIMEOUT = 60",
        "bump timeout",
        when=_FUTURE,
    )
    with pytest.raises(Exception, match="no longer hold"):
        await _call(server, "memory_verify", id=memory_id)

    # `claims=[]` is the explicit clear-and-stamp escape; audited, and it
    # drops the memory back to incumbent-governed drift. On a body whose
    # cited file resolves the clear has to come with an attestation — a
    # stamp attesting nothing is refused since 7.3.0.
    with pytest.raises(Exception, match="attests none of them"):
        await _call(server, "memory_verify", id=memory_id, claims=[])
    cleared = await _call(
        server, "memory_verify", id=memory_id, claims=[], verified_paths=["pkg/mod.py"]
    )
    assert cleared["claims"] == []
    assert cleared["verified_paths"] == ["pkg/mod.py"]


async def test_verify_replaces_claims(server_in_repo) -> None:
    server, _repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    reverified = await _call(
        server, "memory_verify", id=memory_id, claims=["pkg/mod.py::other"]
    )
    assert reverified["claims"] == ["pkg/mod.py::other"]
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["claims"] == ["pkg/mod.py::other"]


async def test_update_body_edit_clears_claims(server_in_repo) -> None:
    server, _repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    updated = await _call(
        server,
        "memory_update",
        id=memory_id,
        content="A rewritten body that no longer cites that module.",
    )
    assert updated.get("status") != "stale"
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["claims"] == []
    assert shown["last_verified_at"] is None


# ---------------------------------------------------------------------------
# The read side — claim-governed narrowing, end to end
# ---------------------------------------------------------------------------


async def test_body_churn_in_claimed_file_reads_clean(server_in_repo) -> None:
    """THE HEADLINE. A commit edits `other()`'s body inside the claimed
    file. The per-file incumbent counted that commit (any-touch); the
    claim-governed leg checks the touched lines against the claimed
    binding, finds no implication, and the memory reads CLEAN — with
    the claim evaluation reported, so the model can see why."""
    server, repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    await _call(server, "memory_verify", id=memory_id)

    _edit_and_commit(
        repo,
        "pkg/mod.py",
        "def other():\n    return 1",
        "def other():\n    return 2",
        "tweak other's body",
        when=_FUTURE,
    )

    shown = await _call(server, "memory_show", id=memory_id)
    drift = shown["commit_drift"]
    assert drift is not None
    assert drift["status"] == "clean"
    assert drift["commits_since_verify"] == 0
    assert drift["claim_drift"] == {"checked": 1, "drifted": []}
    assert shown["staleness_verdict"] == "fresh"


async def test_touching_claimed_binding_reads_drift_naming_the_claim(
    server_in_repo,
) -> None:
    server, repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    await _call(server, "memory_verify", id=memory_id)

    _edit_and_commit(
        repo,
        "pkg/mod.py",
        "def handler():",
        "def handler(flag=None):",
        "change handler signature",
        when=_FUTURE,
    )

    shown = await _call(server, "memory_show", id=memory_id)
    drift = shown["commit_drift"]
    assert drift["status"] == "drift"
    assert drift["commits_since_verify"] == 1
    assert drift["claim_drift"]["checked"] == 1
    assert drift["claim_drift"]["drifted"] == ["pkg/mod.py::handler"]
    assert "pkg/mod.py::handler" in drift["recommendation"]
    assert shown["staleness_verdict"] == "spot_check_recommended"


async def test_unclaimed_cited_file_keeps_any_touch_rule(server_in_repo) -> None:
    """Declaring a claim narrows ONLY that file. The body also cites
    notes.md with no claim on it — a commit touching notes.md still
    escalates under the incumbent rule."""
    server, repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    await _call(
        server, "memory_verify", id=memory_id, verified_paths=[str(repo / "notes.md")]
    )

    (repo / "notes.md").write_text("notes\nmore notes\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-m", "touch notes", when=_FUTURE)

    shown = await _call(server, "memory_show", id=memory_id)
    drift = shown["commit_drift"]
    assert drift["status"] == "drift"
    assert drift["commits_since_verify"] == 1
    # The claim itself did not drift; the ungoverned anchor did.
    assert drift["claim_drift"] == {"checked": 1, "drifted": []}


async def test_search_hit_carries_claim_drift(server_in_repo) -> None:
    server, repo = server_in_repo
    memory_id = await _write_claimed(server, "pkg/mod.py::handler")
    await _call(server, "memory_verify", id=memory_id)

    _edit_and_commit(
        repo,
        "pkg/mod.py",
        "def handler(",
        "def handler_renamed(",
        "rename handler",
        when=_FUTURE,
    )

    hits = await _call(server, "memory_search", query="drift gate mod.py")
    hits = hits["result"] if isinstance(hits, dict) and "result" in hits else hits
    hit = next(h for h in hits if h["id"] == memory_id)
    assert hit["commit_drift_count"] == 1
    assert hit["claim_drift"]["drifted"] == ["pkg/mod.py::handler"]
    assert hit["staleness_verdict"] == "spot_check_recommended"


# ---------------------------------------------------------------------------
# The absence shape end to end (T2)
# ---------------------------------------------------------------------------


async def _write_absence_claimed(server: Any) -> str:
    """A body telling the deletion story its claim asserts: pkg/legacy.py
    is gone on purpose. The body CITES the deleted path — the dominant
    real pattern per T1's cohort D — so the citation anchor and the
    governed claim path coincide, as they will in live records."""
    written = await _call(
        server,
        "memory_write",
        content=(
            "pkg/legacy.py was deleted on purpose; its retry logic moved "
            "into pkg/mod.py and must not come back."
        ),
        scopes=["tools"],
        claims=["!pkg/legacy.py"],
    )
    assert written["status"] == "committed"
    return written["id"]


async def test_write_refuses_absence_claim_while_path_exists(
    server_in_repo,
) -> None:
    server, _repo = server_in_repo
    with pytest.raises(Exception, match="absence claim"):
        await _call(
            server,
            "memory_write",
            content="Claims pkg/mod.py stays deleted while it plainly exists.",
            scopes=["tools"],
            claims=["!pkg/mod.py"],
        )


async def test_write_stores_absence_claim_when_path_is_gone(
    server_in_repo,
) -> None:
    server, _repo = server_in_repo
    memory_id = await _write_absence_claimed(server)
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["claims"] == ["!pkg/legacy.py"]


async def test_verify_refuses_stamp_when_absent_path_reappears(
    server_in_repo,
) -> None:
    """Escalation-on-reappearance at the strongest surface, with no
    verify-side code: the stored re-check inherits the inverted oracle,
    so `last_verified_at` cannot be stamped over a path that came back."""
    server, repo = server_in_repo
    memory_id = await _write_absence_claimed(server)
    verified = await _call(server, "memory_verify", id=memory_id)
    assert verified["claims"] == ["!pkg/legacy.py"]

    (repo / "pkg" / "legacy.py").write_text("BACK_FROM_THE_DEAD = True\n")
    _git(repo, "add", "pkg/legacy.py")
    _git(repo, "commit", "-m", "resurrect legacy module", when=_FUTURE)

    with pytest.raises(Exception, match="exists in the worktree"):
        await _call(server, "memory_verify", id=memory_id)


async def test_reappearance_escalates_commit_drift_naming_the_claim(
    server_in_repo,
) -> None:
    """The read-side mirror of the headline: a commit re-creating the
    claimed-absent path implicates the claim, escalates the verdict,
    and names `!pkg/legacy.py` so the model sees which polarity fired."""
    server, repo = server_in_repo
    memory_id = await _write_absence_claimed(server)
    await _call(server, "memory_verify", id=memory_id)

    (repo / "pkg" / "legacy.py").write_text(
        "RESURRECTED_CONSTANT_VALUE = 'back from the dead'\n"
    )
    _git(repo, "add", "pkg/legacy.py")
    _git(repo, "commit", "-m", "resurrect legacy module", when=_FUTURE)

    shown = await _call(server, "memory_show", id=memory_id)
    drift = shown["commit_drift"]
    assert drift is not None
    assert drift["status"] == "drift"
    assert drift["commits_since_verify"] == 1
    assert drift["claim_drift"]["checked"] == 1
    assert drift["claim_drift"]["drifted"] == ["!pkg/legacy.py"]
    assert shown["staleness_verdict"] == "spot_check_recommended"
