"""Tests for what a search hit says about drift: the verdict on every
hit and the reason that names what to check, the claims-only commit-drift
gate, and race-safety of load_all against concurrent tombstoning.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.verify import commit_drift_anchor_paths

_GIT_AVAILABLE = shutil.which("git") is not None
_REMOTE = "git@github.com:example/claims-only.git"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _hits(raw: Any) -> list[dict[str, Any]]:
    """Unwrap the SDK's structured content envelope."""
    if isinstance(raw, dict) and "result" in raw:
        return raw["result"]
    return raw


# ---------------------------------------------------------------------------
# The verdict and its reason on every hit
# ---------------------------------------------------------------------------


#: The keys a search hit may carry; the rest of a record is memory_show's.
_HIT_KEYS = {
    "id",
    "snippet",
    "relevance",
    "staleness_verdict",
    "reason",
    "superseded_by",
    "contradicts",
    "held",
    "body",
    "use_token",
}


async def test_every_hit_carries_drift_counts(server: Any, tmp_path: Path) -> None:
    """Every hit carries the verdict its path checks give, in the compact
    shape; the per-path detail is memory_show's."""
    real_path = tmp_path / "real.txt"
    real_path.write_text("real")
    await _call(
        server,
        "memory_write",
        content=f"healthy memory referencing `{real_path}`",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_write",
        content="another healthy memory mentioning the same `tools` topic",
        scopes=["tools"],
    )
    hits = _hits(await _call(server, "memory_search", query="tools healthy memory"))
    assert len(hits) >= 2
    for hit in hits:
        assert set(hit) <= _HIT_KEYS
        assert hit["staleness_verdict"] == "fresh"
        assert "reason" not in hit
    shown = [await _call(server, "memory_show", id=hit["id"]) for hit in hits]
    assert [s.get("paths") for s in shown].count({str(real_path): "present"}) == 1


async def test_drift_counts_fire_for_missing_path(server: Any, tmp_path: Path) -> None:
    bogus = tmp_path / "definitely-does-not-exist-12345.txt"
    body = f"see the script at `{bogus}` for the deploy steps"
    await _call(server, "memory_write", content=body, scopes=["tools"])

    hits = _hits(await _call(server, "memory_search", query="deploy script tools"))
    assert hits[0]["staleness_verdict"] == "spot_check_recommended"
    assert str(bogus) in hits[0]["reason"]
    shown = await _call(server, "memory_show", id=hits[0]["id"])
    assert shown["paths"] == {str(bogus): "missing"}


async def test_drift_counts_zero_when_path_exists(server: Any, tmp_path: Path) -> None:
    real = tmp_path / "exists.txt"
    real.write_text("x")
    await _call(
        server,
        "memory_write",
        content=f"deploy step lives at `{real}`",
        scopes=["tools"],
    )
    hits = _hits(await _call(server, "memory_search", query="deploy step lives"))
    assert hits[0]["staleness_verdict"] == "fresh"
    assert "reason" not in hits[0]
    shown = await _call(server, "memory_show", id=hits[0]["id"])
    assert shown["paths"] == {str(real): "present"}


async def test_drift_counts_zero_for_pathless_body(server: Any) -> None:
    """Never verified and nothing testable: the calendar alone asks for no
    check."""
    await _call(
        server,
        "memory_write",
        content="prefer code-driven tutorials over abstract explanations",
        scopes=["learning-style"],
    )
    hits = _hits(await _call(server, "memory_search", query="tutorials"))
    assert hits[0]["staleness_verdict"] == "fresh"
    assert "reason" not in hits[0]
    shown = await _call(server, "memory_show", id=hits[0]["id"])
    assert "paths" not in shown


async def test_expand_top_still_surfaces_full_drift_report(
    server: Any, tmp_path: Path
) -> None:
    bogus = tmp_path / "missing.txt"
    body = f"deploy at `{bogus}`"
    await _call(server, "memory_write", content=body, scopes=["tools"])
    hits = _hits(
        await _call(
            server, "memory_search", query="deploy tools missing", expand_top=True
        )
    )
    top = hits[0]
    assert top["body"].strip() == body
    assert str(bogus) in top["reason"]
    shown = await _call(server, "memory_show", id=top["id"])
    assert shown["paths"][str(bogus)] == "missing"


_CLAIMED_MODULE = '''\
"""Module under claim."""


def handler():
    return 1
'''

# No path-shaped token anywhere — `commit_drift_anchor_paths` must come
# back empty or the memory is anchor-carrying, not claims-only. Each test
# asserts that precondition so a future extractor change that starts
# matching one of these words breaks the premise loudly.
_CLAIMS_ONLY_BODY = "The handler binding owns the drift escalation policy here"


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


@pytest.fixture
def claims_repo_server(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Factory: a server whose `capture_origin` reports a tmp repo carrying
    one claimable module. `stale_days` feeds `verification_stale_days` so a
    test can drive the calendar-stale branch of the verdict ladder without
    backdating timestamps (0 = any verified memory is immediately stale).
    `worktree_root` is set — the write-side claim oracle resolves declared
    claims against it."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(_CLAIMED_MODULE)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "remote", "add", "origin", _REMOTE)
    _git(repo, "add", ".")
    _git(
        repo, "commit", "-m", "initial", when=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )

    def make(*, stale_days: int = 30):
        origin = Origin(
            cwd=str(repo), repo=_REMOTE, branch="main", worktree_root=str(repo)
        )
        state = SessionState()
        cfg = Config(
            storage=StorageConfig(directory=str(memory_dir)),
            behavior=BehaviorConfig(verification_stale_days=stale_days),
        )

        import bettermemory._handlers as handlers_module

        store = Store(memory_dir)
        rec = Recorder(store=store, session_id=state.session_id)
        server = build_server(config=cfg, store=store, state=state, recorder=rec)

        def fake_capture(cwd: Path | None = None) -> Origin:
            return origin

        monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)
        return server, repo

    return make


async def _write_claims_only(server: Any) -> str:
    assert commit_drift_anchor_paths(_CLAIMS_ONLY_BODY, ()) == ()
    written = await _call(
        server,
        "memory_write",
        content=_CLAIMS_ONLY_BODY,
        scopes=["tools"],
        claims=["pkg/mod.py::handler"],
    )
    assert written["status"] == "committed"
    return written["id"]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_claims_only_memory_drift_agrees_across_surfaces(
    claims_repo_server, tmp_path: Path, memory_dir: Path
) -> None:
    """A post-verify commit touches the claimed binding. Every surface must
    report the same measurement: per-hit count 1 with the claim named and
    verdict `spot_check_recommended` — not a hit with no count at all whose
    verdict stays wherever `hit_to_dict` left it while the other three
    surfaces escalate."""
    server, repo = claims_repo_server()
    memory_id = await _write_claims_only(server)
    await _call(server, "memory_verify", id=memory_id)

    target = repo / "pkg" / "mod.py"
    target.write_text(
        target.read_text().replace("def handler():", "def handler(flag=None):")
    )
    _git(repo, "add", "pkg/mod.py")
    _git(
        repo,
        "commit",
        "-m",
        "change handler signature",
        when=datetime(2030, 6, 1, tzinfo=timezone.utc),
    )

    raw = await _call(server, "memory_search", query="handler drift escalation policy")
    hit = next(h for h in _hits(raw) if h["id"] == memory_id)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert "pkg/mod.py::handler" in hit["reason"]

    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["commit_drift"]["commits_since_verify"] == 1
    assert shown["commit_drift"]["claim_drift"] == {
        "checked": 1,
        "drifted": ["pkg/mod.py::handler"],
    }
    assert shown["staleness_verdict"] == "spot_check_recommended"

    report = await _call(
        server, "memory_admin", action="health", section="commit_drift_debt"
    )
    row = next(r for r in report["commit_drift_debt"]["rows"] if r["id"] == memory_id)
    assert row["commits_since_verify"] == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
async def test_claims_only_stale_memory_with_clean_claims_demotes_on_search(
    claims_repo_server, tmp_path: Path
) -> None:
    """The other direction: calendar-stale, but the only post-verify commit
    never touched the claimed file. The stale-demotion arm of
    `verdict_from_signals` needs a MEASURED zero — a hit skipped past the
    count passes None and stays `spot_check_required` while memory_show
    demotes the same memory to `fresh`."""
    server, repo = claims_repo_server(stale_days=0)
    memory_id = await _write_claims_only(server)
    await _call(server, "memory_verify", id=memory_id)

    (repo / "unrelated.md").write_text("churn\n")
    _git(repo, "add", "unrelated.md")
    _git(
        repo,
        "commit",
        "-m",
        "unrelated churn",
        when=datetime(2030, 6, 1, tzinfo=timezone.utc),
    )

    raw = await _call(server, "memory_search", query="handler drift escalation policy")
    hit = next(h for h in _hits(raw) if h["id"] == memory_id)
    assert hit["staleness_verdict"] == "fresh"

    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["commit_drift"]["commits_since_verify"] == 0
    assert shown["commit_drift"]["claim_drift"] == {"checked": 1, "drifted": []}
    assert shown["staleness_verdict"] == "fresh"
