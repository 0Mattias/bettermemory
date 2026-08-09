"""Tests for path-drift counts surfaced on every search hit + the
claims-only commit-drift gate + race-safety of load_all against
concurrent tombstoning.

Background: drift used to fire only on `expand_top=True` and only for the
top hit when its relevance was "high". Hits 2-5 in a default search carried
stale path claims silently. Surfacing cheap drift counts on every hit lets
the model self-triage which hit to expand without round-tripping through
memory_show.
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
# Drift counts on every hit
# ---------------------------------------------------------------------------


async def test_every_hit_carries_drift_counts(server: Any, tmp_path: Path) -> None:
    """A search response should include path_drift_checked and
    path_drift_missing on every hit — not just the top one — so the
    model can pick which hit to expand."""
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
    raw = await _call(server, "memory_search", query="tools healthy memory")
    hits = _hits(raw)
    assert len(hits) >= 2
    for hit in hits:
        assert "path_drift_checked" in hit
        assert "path_drift_missing" in hit
        assert isinstance(hit["path_drift_checked"], int)
        assert isinstance(hit["path_drift_missing"], int)


async def test_drift_counts_fire_for_missing_path(server: Any, tmp_path: Path) -> None:
    bogus = tmp_path / "definitely-does-not-exist-12345.txt"
    body = f"see the script at `{bogus}` for the deploy steps"
    await _call(server, "memory_write", content=body, scopes=["tools"])

    raw = await _call(server, "memory_search", query="deploy script tools")
    hits = _hits(raw)
    assert len(hits) >= 1
    assert hits[0]["path_drift_checked"] == 1
    assert hits[0]["path_drift_missing"] == 1


async def test_drift_counts_zero_when_path_exists(server: Any, tmp_path: Path) -> None:
    real = tmp_path / "exists.txt"
    real.write_text("x")
    await _call(
        server,
        "memory_write",
        content=f"deploy step lives at `{real}`",
        scopes=["tools"],
    )
    raw = await _call(server, "memory_search", query="deploy step lives")
    hits = _hits(raw)
    assert len(hits) >= 1
    assert hits[0]["path_drift_checked"] == 1
    assert hits[0]["path_drift_missing"] == 0


async def test_drift_counts_zero_for_pathless_body(server: Any) -> None:
    """A memory body with no path-shaped tokens should carry both
    counts at zero — it's a positive "nothing to spot-check" signal,
    not noise."""
    await _call(
        server,
        "memory_write",
        content="prefer code-driven tutorials over abstract explanations",
        scopes=["learning-style"],
    )
    raw = await _call(server, "memory_search", query="tutorials")
    hits = _hits(raw)
    assert hits[0]["path_drift_checked"] == 0
    assert hits[0]["path_drift_missing"] == 0


async def test_expand_top_still_surfaces_full_drift_report(
    server: Any, tmp_path: Path
) -> None:
    """When expand_top=True fires, the per-hit counts coexist with the
    full path_drift report on the top hit — the existing surface that
    surfaces actual missing paths is unchanged."""
    bogus = tmp_path / "missing.txt"
    body = f"deploy at `{bogus}`"
    await _call(server, "memory_write", content=body, scopes=["tools"])
    raw = await _call(
        server,
        "memory_search",
        query="deploy tools missing",
        expand_top=True,
    )
    hits = _hits(raw)
    top = hits[0]
    assert top["path_drift_missing"] == 1
    # When drift is found AND expand_top fires, full report is present.
    if "path_drift" in top and top["path_drift"]:
        assert str(bogus) in top["path_drift"]["missing"]


# ---------------------------------------------------------------------------
# Commit-drift gate parity — claims-only memories
# ---------------------------------------------------------------------------
#
# A claims-only memory (declared claims, citation-less body, no
# verified_paths) is fully governed by its claims — the declaration IS the
# anchor (`verify._resolve_with_claims`). All four commit-drift surfaces
# (per-hit search, memory_show, memory_health's commit_drift_debt, and the
# scope-overview curation rollup) must gate on the same anchors-or-claims
# condition, or the same memory reads a different staleness_verdict
# depending on which tool the model reaches for. Fixture pattern follows
# `test_server_claims.py`: tmp repo with controlled author dates,
# `capture_origin` monkeypatched to point at it.


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
    claims_repo_server, tmp_path: Path
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
    assert hit.get("commit_drift_count") == 1
    assert hit["claim_drift"] == {"checked": 1, "drifted": ["pkg/mod.py::handler"]}
    assert hit["staleness_verdict"] == "spot_check_recommended"

    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["commit_drift"]["commits_since_verify"] == 1
    assert shown["staleness_verdict"] == "spot_check_recommended"

    report = await _call(server, "memory_health")
    row = next(r for r in report["commit_drift_debt"]["rows"] if r["id"] == memory_id)
    assert row["commits_since_verify"] == 1

    overview = await _call(server, "memory_scope_overview")
    assert overview["curation_pending"]["drifted"] == 1


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
    assert hit.get("commit_drift_count") == 0
    assert hit["claim_drift"] == {"checked": 1, "drifted": []}
    assert hit["staleness_verdict"] == "fresh"

    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["commit_drift"]["status"] == "clean"
    assert shown["staleness_verdict"] == "fresh"


# ---------------------------------------------------------------------------
# Race-safety: load_all skips files that disappeared mid-iteration
# ---------------------------------------------------------------------------


def test_load_all_skips_disappeared_file(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file listed by `_iter_active_paths` may have moved to
    `.tombstones/` between listdir and read. The defensive catch
    in `load_all` should yield the remaining memories rather than
    crashing the whole call."""
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["tools"])

    real_load = store._load_path
    target_id = a.id
    seen: dict[str, bool] = {}

    def flaky_load(path: Path) -> Any:
        # The first time we see `a`'s file, simulate a concurrent move.
        memory = real_load(path)
        if memory.id == target_id and not seen.get("done"):
            seen["done"] = True
            raise FileNotFoundError(path)
        return memory

    monkeypatch.setattr(store, "_load_path", flaky_load)
    out = store.load_all()
    ids = {m.id for m in out}
    assert b.id in ids
    assert a.id not in ids


async def test_list_with_bodies_survives_tombstone_race(
    server: Any,
    memory_dir: Path,
) -> None:
    """memory_list(with_bodies=True) used to crash if a tombstone race
    raised FileNotFoundError mid-iteration. With load_all defensively
    catching OSError, the surviving memories come back cleanly."""
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    await _call(server, "memory_write", content="beta", scopes=["tools"])

    store = Store(memory_dir)
    # Tombstone `a` to simulate the file moving out from under any
    # in-flight iteration; subsequent memory_list calls should see
    # `b` only and not crash.
    store.tombstone(a["id"], reason="race")

    raw = await _call(server, "memory_list", with_bodies=True)
    rows = raw.get("result", raw) if isinstance(raw, dict) else raw
    bodies = " ".join(row.get("body", "") for row in rows)
    assert "beta" in bodies
    assert "alpha" not in bodies
