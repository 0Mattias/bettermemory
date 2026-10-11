"""The one staleness verdict `memory_show` and the session brief read.

`verdict.resolve_verdicts` computes a memory's verification status, path
drift, commit leg and `staleness_verdict`, the commit leg through the
search's per-hit memo (`_response._DRIFT_MEMO`); `memory_show` is routed
through it, and `verdict.memoised_verdicts` answers from the memo alone.
These tests pin, against a real temp git repository:

- agreement: the function, `memory_show` and `memory_search`'s hit read
  one verdict per memory (fresh, commit drift, never verified, path
  drift, a caller in another repository);
- the warm show: a second `memory_show` of a row nothing moved under
  runs no git process;
- the memo-only mode: unmemoised rows come back unresolved with no git
  process, and after `resolve_verdicts` read exactly what a full
  computation reads;
- invalidation: a commit, a `memory_update` of the body and a
  `memory_verify` each move the affected verdict on the next call.

Git processes are counted at `origin._git_result`, the one place every
git subprocess in the package is spawned.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import pytest

from bettermemory import _caches
from bettermemory import origin as origin_module
from bettermemory.config import Config
from bettermemory.models import Memory
from bettermemory.origin import Origin
from bettermemory.store import Store
from bettermemory.verdict import (
    MemoryVerdict,
    UnresolvedVerdict,
    memoised_verdicts,
    memory_verdict,
    resolve_verdicts,
)

from .test_server_commit_drift import (
    _REMOTE,
    _call,
    _commit_touching,
    _init_repo,
    _unwrap,
    server_with_fake_origin,  # noqa: F401  (the fixture, used by name)
)

_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

_STALE_AFTER_DAYS = Config().behavior.verification_stale_days
_ANCIENT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


@contextmanager
def _git_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every git process spawned inside the block, by its arguments."""
    calls: list[tuple[str, ...]] = []
    real = origin_module._git_result

    def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git_result", spy)
    try:
        yield calls
    finally:
        monkeypatch.setattr(origin_module, "_git_result", real)


def _repo(tmp_path: Path, name: str = "repo", *, remote: str = _REMOTE) -> Path:
    """A repository whose `notes.md` and `other.md` were both touched long
    before any stamp, so a citation of either is an in-repo anchor the
    quiescent classification reads as applicable; and `docs/guide.md`,
    whose directory segment makes it a citation path drift stats."""
    repo = tmp_path / name
    repo.mkdir()
    _init_repo(repo, remote=remote)
    _commit_touching(repo, "notes", when=_ANCIENT)
    _commit_touching(repo, "other", when=_ANCIENT, filename="other.md")
    (repo / "docs").mkdir()
    _commit_touching(repo, "guide", when=_ANCIENT, filename="docs/guide.md")
    return repo


def _origin(repo: Path, remote: str = _REMOTE) -> Origin:
    """The caller standing in `repo`. The worktree root is what
    `memory_verify` reads the stamp's head from (reachability basis) and
    what anchors the body's relative citations for path drift."""
    return Origin(cwd=str(repo), repo=remote, branch="main", worktree_root=str(repo))


def _verdicts(memories: list[Memory], origin: Origin | None) -> list[MemoryVerdict]:
    return resolve_verdicts(
        memories,
        caller_origin=origin,
        now=datetime.now(timezone.utc),
        stale_after_days=_STALE_AFTER_DAYS,
    )


def _memoised(
    memories: list[Memory], origin: Origin | None
) -> list[MemoryVerdict | UnresolvedVerdict]:
    return memoised_verdicts(
        memories,
        caller_origin=origin,
        now=datetime.now(timezone.utc),
        stale_after_days=_STALE_AFTER_DAYS,
    )


def _parts(verdict: MemoryVerdict) -> dict[str, Any]:
    """The verdict as `memory_show` spells it."""
    drift = verdict.path_drift
    return {
        "verification": verdict.verification.to_dict(),
        "staleness_verdict": verdict.staleness_verdict,
        "path_drift": (
            drift.to_dict()
            if (drift.has_drift or drift.verified or drift.expected_absent)
            else None
        ),
        "commit_drift": (
            verdict.commit_drift.to_dict() if verdict.commit_drift is not None else None
        ),
    }


def _shown_parts(shown: dict[str, Any]) -> dict[str, Any]:
    return {
        key: shown[key]
        for key in ("verification", "staleness_verdict", "path_drift", "commit_drift")
    }


async def _search_verdict(server: Any, memory_id: str, query: str) -> str:
    raw = await _call(server, "memory_search", query=query, expand_top=False)
    hit = next(h for h in _unwrap(raw) if h["id"] == memory_id)
    return hit["staleness_verdict"]


# ---------------------------------------------------------------------------
# (a) one verdict: the function, memory_show and memory_search agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("fresh", "fresh"),
        ("commit_drift", "spot_check_recommended"),
        ("never_verified", "spot_check_required"),
        ("path_drift", "spot_check_recommended"),
        ("caller_elsewhere", "fresh"),
    ],
)
async def test_verdict_agrees_with_show_and_search(
    server_with_fake_origin: Any,  # noqa: F811
    memory_dir: Path,
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    written = await _call(
        server,
        "memory_write",
        content=(
            "the widget rule lives in docs/guide.md"
            if case == "path_drift"
            else "the widget rule lives in notes.md"
        ),
        scopes=["tools"],
    )
    memory_id = written["id"]
    if case == "path_drift":
        await _call(
            server, "memory_verify", id=memory_id, verified_paths=["docs/guide.md"]
        )
    elif case != "never_verified":
        await _call(server, "memory_verify", id=memory_id)
    if case == "commit_drift":
        _commit_touching(repo, "post", when=_FUTURE)
    if case == "path_drift":
        # Gone from disk, never committed: the citation, resolved against
        # the memory's recorded worktree, is the claim-anchored miss; no
        # commit lands, so the commit leg stays clean.
        (repo / "docs" / "guide.md").unlink()
    caller = origin
    if case == "caller_elsewhere":
        other_remote = "git@github.com:example/other.git"
        other = _repo(tmp_path, "other-repo", remote=other_remote)
        caller = _origin(other, other_remote)
        server = server_with_fake_origin(caller)

    memory = Store(memory_dir).load_one(memory_id)
    (computed,) = _verdicts([memory], caller)
    assert computed.staleness_verdict == expected

    shown = await _call(server, "memory_show", id=memory_id)
    assert _parts(computed) == _shown_parts(shown)

    if case != "caller_elsewhere":
        # Auto-scope keeps another repository's memory out of the
        # caller's search, so only the show reads it from there.
        assert await _search_verdict(server, memory_id, "widget rule") == expected
    if case == "commit_drift":
        assert shown["commit_drift"]["status"] == "drift"
    if case in ("fresh", "path_drift"):
        assert shown["commit_drift"]["status"] == "clean"
    if case in ("never_verified", "caller_elsewhere"):
        assert shown["commit_drift"] is None


# ---------------------------------------------------------------------------
# (b) the warm show runs no git
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", ["reachability", "author-date"])
@pytest.mark.parametrize("drifted", [False, True])
async def test_second_memory_show_spawns_no_git(
    server_with_fake_origin: Any,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drifted: bool,
    basis: str,
) -> None:
    """The measured defect: every show forked git for its commit leg, the
    memo the search fills never consulted. A clean row (the quiescent
    applicability log) and a drifting one (the narrowing; a path-filtered
    log on the author-date basis) both run nothing the second time, on
    either basis. A caller with no worktree root stamps no head, so its
    rows count in author-date space."""
    repo = _repo(tmp_path)
    origin = (
        _origin(repo)
        if basis == "reachability"
        else Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    )
    server = server_with_fake_origin(origin)
    written = await _call(
        server, "memory_write", content="widget rule in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    if drifted:
        _commit_touching(repo, "post", when=_FUTURE)

    _caches.clear_all()
    with _git_calls(monkeypatch) as cold_calls:
        cold = await _call(server, "memory_show", id=written["id"])
    assert cold_calls, "the cold show must resolve the commit leg from git"
    assert cold["commit_drift"]["status"] == ("drift" if drifted else "clean")
    assert cold["commit_drift"]["basis"] == basis

    with _git_calls(monkeypatch) as warm_calls:
        warm = await _call(server, "memory_show", id=written["id"])
    assert warm_calls == []
    cold.pop("use_token")
    warm.pop("use_token")
    assert warm == cold


async def test_show_reads_the_row_a_search_resolved(
    server_with_fake_origin: Any,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One memo: a search's resolution serves the show of the same row."""
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    written = await _call(
        server, "memory_write", content="widget rule in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_touching(repo, "post", when=_FUTURE)

    _caches.clear_all()
    verdict = await _search_verdict(server, written["id"], "widget rule")
    with _git_calls(monkeypatch) as calls:
        shown = await _call(server, "memory_show", id=written["id"])
    assert calls == []
    assert shown["staleness_verdict"] == verdict == "spot_check_recommended"


# ---------------------------------------------------------------------------
# (c) memo-only mode
# ---------------------------------------------------------------------------


async def test_memoised_verdicts_resolve_nothing_then_match_a_full_computation(
    server_with_fake_origin: Any,  # noqa: F811
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    drifting = await _call(
        server, "memory_write", content="widget rule in notes.md", scopes=["tools"]
    )
    clean = await _call(
        server, "memory_write", content="gadget rule in other.md", scopes=["tools"]
    )
    untethered = await _call(
        server,
        "memory_write",
        content="prefer short commit subjects in this project",
        scopes=["tools"],
    )
    never = await _call(
        server, "memory_write", content="sprocket rule in notes.md", scopes=["tools"]
    )
    for row in (drifting, clean, untethered):
        await _call(server, "memory_verify", id=row["id"])
    _commit_touching(repo, "post", when=_FUTURE)
    store = Store(memory_dir)
    # Newest first, as the brief reads them.
    ids = [never["id"], untethered["id"], clean["id"], drifting["id"]]
    memories = [store.load_one(i) for i in ids]

    _caches.clear_all()
    with _git_calls(monkeypatch) as calls:
        first = _memoised(memories, origin)
    assert calls == []
    # The two rows whose leg applies are unresolved; the never-verified
    # and the untethered row need no git and read in full.
    assert [type(v) for v in first] == [
        MemoryVerdict,
        MemoryVerdict,
        UnresolvedVerdict,
        UnresolvedVerdict,
    ]
    assert [v.id for v in first] == ids
    never_verdict, untethered_verdict = first[0], first[1]
    assert isinstance(never_verdict, MemoryVerdict)
    assert isinstance(untethered_verdict, MemoryVerdict)
    assert never_verdict.staleness_verdict == "spot_check_required"
    assert untethered_verdict.commit_drift is None

    # The resolve call, from a worker thread as the brief's background
    # fill runs it, for the unresolved rows only.
    unresolved = [
        m for m, v in zip(memories, first) if isinstance(v, UnresolvedVerdict)
    ]
    await anyio.to_thread.run_sync(lambda: _verdicts(unresolved, origin))

    with _git_calls(monkeypatch) as calls:
        second = _memoised(memories, origin)
    assert calls == []
    by_id = {v.id: v for v in second if isinstance(v, MemoryVerdict)}
    assert list(by_id) == ids
    assert by_id[drifting["id"]].staleness_verdict == "spot_check_recommended"
    assert by_id[clean["id"]].staleness_verdict == "fresh"

    # Equal to a full computation from an empty memo, and to memory_show.
    _caches.clear_all()
    full = _verdicts(memories, origin)
    assert [_parts(v) for v in second if isinstance(v, MemoryVerdict)] == [
        _parts(v) for v in full
    ]
    for memory_id, verdict in zip(ids, full):
        shown = await _call(server, "memory_show", id=memory_id)
        assert _shown_parts(shown) == _parts(verdict)


async def test_memoised_verdicts_need_no_repository(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller in no repository has no commit leg to wait for: every row
    reads in full, with no git, and agrees with the resolving call."""
    store = Store(memory_dir)
    memory = store.write(content="widget rule in notes.md", scopes=["tools"])
    store.mark_verified(memory.id)
    memory = store.load_one(memory.id)
    for caller in (None, Origin(cwd=str(tmp_path), repo=None)):
        with _git_calls(monkeypatch) as calls:
            (memoised,) = _memoised([memory], caller)
        assert calls == []
        assert isinstance(memoised, MemoryVerdict)
        assert memoised == _verdicts([memory], caller)[0]


# ---------------------------------------------------------------------------
# (d) invalidation
# ---------------------------------------------------------------------------


async def _resolved_row(
    server: Any, memory_dir: Path, origin: Origin, content: str
) -> tuple[str, MemoryVerdict]:
    written = await _call(server, "memory_write", content=content, scopes=["tools"])
    await _call(server, "memory_verify", id=written["id"])
    memory = Store(memory_dir).load_one(written["id"])
    verdict = memory_verdict(
        memory,
        caller_origin=origin,
        now=datetime.now(timezone.utc),
        stale_after_days=_STALE_AFTER_DAYS,
    )
    return written["id"], verdict


async def test_a_commit_moves_the_verdict(
    server_with_fake_origin: Any,  # noqa: F811
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    memory_id, before = await _resolved_row(
        server, memory_dir, origin, "widget rule in notes.md"
    )
    assert before.staleness_verdict == "fresh"
    memory = Store(memory_dir).load_one(memory_id)
    assert _memoised([memory], origin) == [before]

    _commit_touching(repo, "post", when=_FUTURE)
    with _git_calls(monkeypatch) as calls:
        (after_head_move,) = _memoised([memory], origin)
    assert calls == []
    assert isinstance(after_head_move, UnresolvedVerdict)
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["staleness_verdict"] == "spot_check_recommended"
    assert shown["commit_drift"]["status"] == "drift"


async def test_a_body_update_moves_the_verdict(
    server_with_fake_origin: Any,  # noqa: F811
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    memory_id, before = await _resolved_row(
        server, memory_dir, origin, "widget rule in notes.md"
    )
    assert before.staleness_verdict == "fresh"
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["staleness_verdict"] == "fresh"

    # A new body clears the stamp: the next read is never-verified.
    await _call(
        server, "memory_update", id=memory_id, content="widget rule in other.md"
    )
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["staleness_verdict"] == "spot_check_required"
    assert shown["commit_drift"] is None
    # Re-verified, the row keys on the new anchors and the new stamp: the
    # memo holds nothing for it until a resolving call has run.
    await _call(server, "memory_verify", id=memory_id)
    memory = Store(memory_dir).load_one(memory_id)
    with _git_calls(monkeypatch) as calls:
        (memoised,) = _memoised([memory], origin)
    assert calls == []
    assert isinstance(memoised, UnresolvedVerdict)
    shown = await _call(server, "memory_show", id=memory_id)
    assert shown["staleness_verdict"] == "fresh"
    assert _memoised([memory], origin)[0] == memory_verdict(
        memory,
        caller_origin=origin,
        now=datetime.now(timezone.utc),
        stale_after_days=_STALE_AFTER_DAYS,
    )


async def test_a_verify_moves_the_verdict(
    server_with_fake_origin: Any,  # noqa: F811
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    origin = _origin(repo)
    server = server_with_fake_origin(origin)
    written = await _call(
        server, "memory_write", content="widget rule in notes.md", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    _commit_touching(repo, "post", when=_FUTURE)
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["staleness_verdict"] == "spot_check_recommended"

    await _call(server, "memory_verify", id=written["id"])
    memory = Store(memory_dir).load_one(written["id"])
    with _git_calls(monkeypatch) as calls:
        (memoised,) = _memoised([memory], origin)
    assert calls == []
    assert isinstance(memoised, UnresolvedVerdict)
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["staleness_verdict"] == "fresh"
    assert shown["commit_drift"]["status"] == "clean"
