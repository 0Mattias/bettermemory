"""The commit-drift cache: the per-hit resolution memoised in
`_response.attach_commit_drift_counts`, the whole-history author dates
memoised per (root, head) in `origin.commit_author_timestamps`, and the root
and the head read from the repository's files (`githead`).

Real repositories and real stores under `tmp_path`; the `ResponseBuilder` is
called directly, as the process-count pins in `test_server_commit_drift.py`
call it. Every memo is exact: a warm answer is the cold answer, and each
input of the resolution is in its key, so a moved head, a new stamp, a
rewritten body or a changed claim is resolved again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, _response, githead, origin, verify
from bettermemory._response import ResponseBuilder
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.origin import Origin
from bettermemory.search import search as run_search
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

# The repository's files answer the root and the head on POSIX only
# (`githead` declines elsewhere). Off POSIX every search runs from git,
# uncached, as it did before the memos, and reads the same hits.
_FILES = os.name == "posix"
files_only = pytest.mark.skipif(
    not _FILES, reason="the files answer the root and the head on POSIX only"
)

_REMOTE = "git@github.com:example/foo.git"
_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_V1 = datetime(2025, 6, 1, tzinfo=timezone.utc)
_T1 = datetime(2025, 7, 1, tzinfo=timezone.utc)
_V2 = datetime(2025, 8, 1, tzinfo=timezone.utc)
_T2 = datetime(2025, 9, 1, tzinfo=timezone.utc)
_NOW = datetime(2025, 10, 1, tzinfo=timezone.utc)

# A module with two claimable bindings: the weak tier reads which one a
# commit's hunks moved.
_MODULE = '''\
"""Module under claim."""

TIMEOUT = 30


def handler():
    return 1


def other():
    return 1
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    if when is not None:
        iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_DATE=iso, GIT_COMMITTER_DATE=iso)
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "remote", "add", "origin", _REMOTE)
    return repo


def _commit(
    repo: Path, message: str, *, when: datetime, files: dict[str, str] | None = None
) -> str:
    for rel, content in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-q", "-m", message, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _caller(cwd: Path) -> Origin:
    """The caller's origin as the suite builds it, with no worktree root:
    the root and the head come from the walk up from `cwd`."""
    return Origin(cwd=str(cwd), repo=_REMOTE, branch="main")


def _write(
    store: Store,
    caller: Origin,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    *,
    at: datetime = _V1,
    claims: list[str] | None = None,
    verified_head: str | None = None,
) -> str:
    memory = store.write(
        content=content, scopes=["tools"], origin=caller, claims=claims
    )
    _stamp(store, monkeypatch, memory.id, at=at, verified_head=verified_head)
    return memory.id


def _stamp(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
    *,
    at: datetime,
    verified_head: str | None = None,
) -> None:
    """`mark_verified` with the stamp's instant pinned, so the fixtures'
    commit dates fall on a known side of it."""
    import bettermemory.store as store_module

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "utcnow", lambda: at)
        store.mark_verified(memory_id, verified_head=verified_head)


def _attach(
    memories: list[Any],
    caller: Origin,
    monkeypatch: pytest.MonkeyPatch,
    *,
    query: str = "widget rule",
) -> tuple[list[dict[str, Any]], list[tuple[str, ...]]]:
    """One search's hits, decorated; and every git argv the decoration
    spawned. Any process at all is recorded, not only `origin._git`'s."""
    hits = run_search(memories, query, max_results=50)
    builder = ResponseBuilder(stale_after_days=30)
    out = [builder.hit_to_dict(hit, now=_NOW) for hit in hits]
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(argv[1:]))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    try:
        builder.attach_commit_drift_counts(out, hits, memories, caller_origin=caller)
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)
    return out, calls


def _by_id(out: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {hit["id"]: hit for hit in out}


def _whole_history_logs(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if c[:2] == ("log", "--format=%aI") and "--" not in c]


# ---------------------------------------------------------------------------
# The per-hit memo
# ---------------------------------------------------------------------------


def test_a_warm_attach_forks_nothing_and_answers_the_same(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every shape of hit at once: an author-date count, a reachability
    count, a narrowed zero, an omission and a claim block. The second
    attach reads them all from the memo, forks nothing, and returns the
    same dicts, key for key and in the same order."""
    repo = _repo(tmp_path)
    anchor = _commit(
        repo,
        "seed",
        when=_T0,
        files={
            "notes0.md": "a\n",
            "notes1.md": "b\n",
            "quiet.md": "q\n",
            "pkg/mod.py": _MODULE,
        },
    )
    store = Store(memory_dir)
    caller = _caller(repo)
    author = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    reach = _write(
        store,
        caller,
        monkeypatch,
        f"widget rule b lives in {repo / 'notes1.md'}",
        verified_head=anchor,
    )
    quiet = _write(
        store, caller, monkeypatch, f"widget rule c lives in {repo / 'quiet.md'}"
    )
    escape = _write(
        store,
        caller,
        monkeypatch,
        "widget rule d: the router config lives at /data/compose/.env on the board",
    )
    claimed = _write(
        store,
        caller,
        monkeypatch,
        "widget rule e is anchored in pkg/mod.py",
        claims=["pkg/mod.py::handler"],
    )
    _commit(
        repo,
        "post",
        when=_T1,
        files={
            "notes0.md": "a2\n",
            "notes1.md": "b2\n",
            "pkg/mod.py": _MODULE.replace("def other():", "def other(flag=False):"),
        },
    )

    memories = store.load_all()
    cold, cold_calls = _attach(memories, caller, monkeypatch)
    hits = _by_id(cold)
    assert (hits[author]["commit_drift_count"], hits[author]["commit_drift_basis"]) == (
        1,
        "author-date",
    )
    assert (hits[reach]["commit_drift_count"], hits[reach]["commit_drift_basis"]) == (
        1,
        "reachability",
    )
    assert hits[quiet]["commit_drift_count"] == 0
    assert "commit_drift_count" not in hits[escape]
    assert hits[claimed]["commit_drift_count"] == 0
    assert hits[claimed]["claim_drift"] == {"checked": 1, "drifted": []}
    assert cold_calls, "the cold attach resolves through git"

    warm, warm_calls = _attach(memories, caller, monkeypatch)
    assert warm == cold
    if _FILES:
        assert warm_calls == []
    assert [list(hit) for hit in warm] == [list(hit) for hit in cold], "key order"


def test_a_commit_moves_the_head_and_the_next_attach_counts_again(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    _commit(repo, "post 1", when=_T1, files={"notes0.md": "a1\n"})

    first, _ = _attach(store.load_all(), caller, monkeypatch)
    assert _by_id(first)[memory_id]["commit_drift_count"] == 1

    _commit(repo, "post 2", when=_T2, files={"notes0.md": "a2\n"})
    second, calls = _attach(store.load_all(), caller, monkeypatch)
    assert _by_id(second)[memory_id]["commit_drift_count"] == 2
    assert len(_whole_history_logs(calls)) == 1, "the new head's history is read once"


def test_a_new_stamp_is_a_new_key(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mark_verified` moves the verify instant; the same head, anchors
    and claims then resolve again against it."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    head = _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})

    first, _ = _attach(store.load_all(), caller, monkeypatch)
    assert _by_id(first)[memory_id]["commit_drift_count"] == 1

    _stamp(store, monkeypatch, memory_id, at=_V2)
    second, calls = _attach(store.load_all(), caller, monkeypatch)
    assert _git(repo, "rev-parse", "HEAD") == head, "premise: the head did not move"
    assert _by_id(second)[memory_id]["commit_drift_count"] == 0
    assert calls, "the new stamp resolved through git"
    if _FILES:
        assert _whole_history_logs(calls) == [], "the head's history is memoised"


def test_a_rewritten_body_with_other_anchors_is_a_new_key(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n", "notes1.md": "b\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    _commit(repo, "post", when=_T1, files={"notes1.md": "b1\n"})

    memories = store.load_all()
    first, _ = _attach(memories, caller, monkeypatch)
    assert _by_id(first)[memory_id]["commit_drift_count"] == 0

    rewritten = [
        m.model_copy(update={"body": f"widget rule a lives in {repo / 'notes1.md'}\n"})
        if m.id == memory_id
        else m
        for m in memories
    ]
    second, calls = _attach(rewritten, caller, monkeypatch)
    assert _by_id(second)[memory_id]["commit_drift_count"] == 1
    assert calls, "the new anchors resolved through git"


def test_a_changed_claim_is_a_new_key(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"pkg/mod.py": _MODULE})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store,
        caller,
        monkeypatch,
        "widget rule e is anchored in pkg/mod.py",
        claims=["pkg/mod.py::handler"],
    )
    _commit(
        repo,
        "widen other",
        when=_T1,
        files={"pkg/mod.py": _MODULE.replace("def other():", "def other(flag=False):")},
    )

    memories = store.load_all()
    first, _ = _attach(memories, caller, monkeypatch)
    hit = _by_id(first)[memory_id]
    assert (hit["commit_drift_count"], hit["claim_drift"]) == (
        0,
        {"checked": 1, "drifted": []},
    )

    reclaimed = [
        m.model_copy(update={"claims": ["pkg/mod.py::other"]})
        if m.id == memory_id
        else m
        for m in memories
    ]
    second, calls = _attach(reclaimed, caller, monkeypatch)
    hit = _by_id(second)[memory_id]
    assert (hit["commit_drift_count"], hit["claim_drift"]) == (
        1,
        {"checked": 1, "drifted": ["pkg/mod.py::other"]},
    )
    assert calls, "the new claim resolved through git"


def test_the_root_and_a_subdirectory_count_the_same(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers in one repository share the root's history; each has
    its own per-hit entries, and they read the same counts."""
    repo = _repo(tmp_path)
    anchor = _commit(
        repo,
        "seed",
        when=_T0,
        files={"notes0.md": "a\n", "src/mod.py": "X = 1\n", "quiet.md": "q\n"},
    )
    store = Store(memory_dir)
    caller = _caller(repo)
    ids = [
        _write(
            store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
        ),
        _write(store, caller, monkeypatch, "widget rule b is in src/mod.py"),
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule c lives in {repo / 'quiet.md'}",
            verified_head=anchor,
        ),
    ]
    _commit(
        repo, "post", when=_T1, files={"notes0.md": "a1\n", "src/mod.py": "X = 2\n"}
    )

    memories = store.load_all()
    at_root, _ = _attach(memories, caller, monkeypatch)
    in_subdirectory, calls = _attach(memories, _caller(repo / "src"), monkeypatch)
    assert in_subdirectory == at_root
    assert [_by_id(at_root)[i].get("commit_drift_count") for i in ids] == [1, 1, 0]
    if _FILES:
        assert _whole_history_logs(calls) == [], "one history per root and head"


def test_an_omitted_count_stays_omitted_from_the_memo(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The omission is memoised as an answer of its own: anchors that
    escape the repository, and a phantom anchor no commit ever touched,
    read absent on the warm attach too, without a process."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    escape = _write(
        store,
        caller,
        monkeypatch,
        "widget rule d: the router config lives at /data/compose/.env on the board",
    )
    phantom = _write(store, caller, monkeypatch, "widget rule p is in src/never.py")
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})

    memories = store.load_all()
    cold, cold_calls = _attach(memories, caller, monkeypatch)
    warm, warm_calls = _attach(memories, caller, monkeypatch)
    for out in (cold, warm):
        hits = _by_id(out)
        assert "commit_drift_count" not in hits[escape]
        assert "commit_drift_count" not in hits[phantom]
    assert cold_calls
    assert warm == cold
    if _FILES:
        assert warm_calls == []


@files_only
def test_the_memo_is_bounded(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the bound the least recently used entry goes; a hit whose entry
    went resolves again, and only it."""
    repo = _repo(tmp_path)
    names = [f"notes{i}.md" for i in range(3)]
    _commit(repo, "seed", when=_T0, files={name: "a\n" for name in names})
    store = Store(memory_dir)
    caller = _caller(repo)
    for i, name in enumerate(names):
        _write(store, caller, monkeypatch, f"widget rule {i} lives in {repo / name}")
    _commit(repo, "post", when=_T1, files={name: "b\n" for name in names})

    monkeypatch.setattr(_response, "_DRIFT_MEMO_CAP", 2)
    memories = store.load_all()
    cold, _ = _attach(memories, caller, monkeypatch)
    assert len(_response._DRIFT_MEMO) == 2
    warm, calls = _attach(memories, caller, monkeypatch)
    assert warm == cold
    assert len(calls) == 1, "one evicted hit: its one path-filtered log"
    assert calls[0][:3] == ("log", "--format=%aI", "HEAD") and "--" in calls[0]
    assert len(_response._DRIFT_MEMO) == 2


def test_a_git_process_that_could_not_run_is_not_memoised(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout folds into the conservative count, which is what the
    uncached code answers on that call and not on the next. The memo
    keeps no value computed across such a failure."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n", "notes1.md": "b\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    _commit(repo, "post 1", when=_T1, files={"notes0.md": "a1\n"})
    _commit(repo, "post 2", when=_T2, files={"notes1.md": "b1\n"})

    memories = store.load_all()
    real_run = subprocess.run

    def filtered_log_times_out(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "--" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", filtered_log_times_out)
    hits = run_search(memories, "widget rule", max_results=50)
    builder = ResponseBuilder(stale_after_days=30)
    out = [builder.hit_to_dict(hit, now=_NOW) for hit in hits]
    builder.attach_commit_drift_counts(out, hits, memories, caller_origin=caller)
    monkeypatch.setattr(subprocess, "run", real_run)
    assert _by_id(out)[memory_id]["commit_drift_count"] == 2, "the unfiltered fallback"

    after, calls = _attach(memories, caller, monkeypatch)
    assert _by_id(after)[memory_id]["commit_drift_count"] == 1
    assert calls, "resolved again once git could run"


def test_a_head_that_moves_during_the_attach_stores_nothing(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path-filtered logs read the head git finds when they run, which
    a commit landing mid-search moves away from the head the key names.
    Nothing from such an attach is kept: back at the first head, the
    count is that head's own, not the mixed one."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    first_head = _commit(repo, "post 1", when=_T1, files={"notes0.md": "a1\n"})

    real_touching = origin.commit_author_timestamps_touching_pathspecs
    moved: list[str] = []

    def commit_then_read(*args: Any, **kwargs: Any) -> Any:
        if not moved:
            moved.append(_commit(repo, "post 2", when=_T2, files={"notes0.md": "a2\n"}))
        return real_touching(*args, **kwargs)

    monkeypatch.setattr(
        verify, "commit_author_timestamps_touching_pathspecs", commit_then_read
    )
    memories = store.load_all()
    _attach(memories, caller, monkeypatch)
    monkeypatch.setattr(
        verify, "commit_author_timestamps_touching_pathspecs", real_touching
    )
    assert moved, "premise: the head moved during the attach"

    _git(repo, "reset", "-q", "--hard", first_head)
    back, calls = _attach(memories, caller, monkeypatch)
    assert _by_id(back)[memory_id]["commit_drift_count"] == 1
    assert calls, "resolved again at the first head"


def test_the_memos_are_registered_with_the_cache_registry() -> None:
    """Each memo is emptied by a registered clearer that takes its lock."""
    assert _response._clear_drift_memo in _caches._CLEARERS
    assert origin._clear_timestamps_memo in _caches._CLEARERS
    assert origin._clear_walk_memo in _caches._CLEARERS
    assert (_response._DRIFT_MEMO_CAP, origin._TIMESTAMPS_MEMO_CAP) == (5000, 16)
    assert origin._WALK_MEMO_CAP == 128


# ---------------------------------------------------------------------------
# The head from the files, and git where they do not settle it
# ---------------------------------------------------------------------------


@files_only
def test_the_files_answer_what_git_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    assert origin.toplevel_and_head_from_files(repo) is None, "no commit yet"
    assert origin.repo_toplevel_and_head(repo) is None
    _commit(repo, "seed", when=_T0, files={"a.txt": "a\n"})
    for cwd in (repo, repo / "src"):
        git_root = Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve()
        git_head = _git(cwd, "rev-parse", "HEAD")
        calls: list[tuple[str, ...]] = []
        real_run = subprocess.run

        def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(tuple(argv[1:]))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        try:
            from_files = origin.toplevel_and_head_from_files(cwd)
            located = origin.repo_toplevel_and_head(cwd)
        finally:
            monkeypatch.setattr(subprocess, "run", real_run)
        assert from_files == (git_root, git_head)
        assert located == (git_root, git_head)
        assert calls == []


def test_where_the_files_do_not_settle_the_head_git_answers_the_same(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the reader declining, the root and head come from `git
    rev-parse` and nothing is memoised, exactly as before the cache; the
    decorated hits are the ones the files' path produces."""
    repo = _repo(tmp_path)
    anchor = _commit(
        repo,
        "seed",
        when=_T0,
        files={"notes0.md": "a\n", "notes1.md": "b\n", "pkg/mod.py": _MODULE},
    )
    store = Store(memory_dir)
    caller = _caller(repo)
    _write(store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}")
    _write(
        store,
        caller,
        monkeypatch,
        f"widget rule b lives in {repo / 'notes1.md'}",
        verified_head=anchor,
    )
    _write(
        store,
        caller,
        monkeypatch,
        "widget rule e is anchored in pkg/mod.py",
        claims=["pkg/mod.py::other"],
    )
    _commit(
        repo,
        "post",
        when=_T1,
        files={
            "notes0.md": "a2\n",
            "notes1.md": "b2\n",
            "pkg/mod.py": _MODULE.replace("def other():", "def other(flag=False):"),
        },
    )
    memories = store.load_all()
    from_files, _ = _attach(memories, caller, monkeypatch)

    _caches.clear_all()
    monkeypatch.setattr(githead, "head_sha", lambda gd: None)
    for _ in range(2):
        from_git, calls = _attach(memories, caller, monkeypatch)
        assert from_git == from_files
        assert ("rev-parse", "--show-toplevel", "HEAD") in calls
        assert ("log", "--format=%aI", "HEAD") in calls
    assert len(_response._DRIFT_MEMO) == 0
    assert len(origin._TIMESTAMPS_MEMO) == 0


@files_only
def test_the_files_decline_where_git_names_another_working_tree(
    tmp_path: Path,
) -> None:
    """`core.worktree` moves the root git names, and a true `core.bare`
    leaves it none. The reader walks to the directory holding `.git`
    either way, so it declines and git answers."""
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    head = _commit(repo, "seed", when=_T0)
    _git(repo, "config", "core.worktree", str(elsewhere))
    assert origin.toplevel_and_head_from_files(repo) is None
    assert origin.repo_toplevel_and_head(repo) == (elsewhere.resolve(), head)
    _git(repo, "config", "--unset", "core.worktree")
    _git(repo, "config", "core.bare", "true")
    assert origin.toplevel_and_head_from_files(repo) is None
    assert origin.repo_toplevel_and_head(repo) is None, "git: no working tree"
    _git(repo, "config", "core.bare", "false")
    assert origin.toplevel_and_head_from_files(repo) == (repo.resolve(), head)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="the kernel spelling is read on macOS"
)
def test_the_root_is_spelled_the_way_git_prints_it(tmp_path: Path) -> None:
    """On a filesystem that ignores case, a caller can name the checkout
    in a spelling the disk does not hold; git prints the disk's."""
    repo = _repo(tmp_path, "Repo")
    head = _commit(repo, "seed", when=_T0)
    spelled = tmp_path / "REPO"
    if not spelled.exists():
        pytest.skip("case-sensitive filesystem")
    git_root = Path(_git(spelled, "rev-parse", "--show-toplevel")).resolve()
    assert git_root.name == "Repo"
    assert origin.toplevel_and_head_from_files(spelled) == (git_root, head)
    assert origin.repo_toplevel_and_head(spelled) == (git_root, head)


# ---------------------------------------------------------------------------
# The whole-history author dates, and the walk
# ---------------------------------------------------------------------------


@files_only
def test_the_whole_history_is_memoised_per_root_and_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    head = _commit(repo, "seed", when=_T0)
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(argv[1:]))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    first = origin.commit_author_timestamps(repo)
    again = origin.commit_author_timestamps(repo / "src")
    assert first == [_T0]
    assert again is first
    assert calls == [("log", "--format=%aI", head)], (
        "the log names the head it is keyed on"
    )

    moved = _commit(repo, "post", when=_T1)
    calls.clear()
    after = origin.commit_author_timestamps(repo)
    assert after == [_T0, _T1]
    assert calls == [("log", "--format=%aI", moved)]
    assert first == [_T0], "the earlier head's list is its own"


def test_a_failed_whole_history_read_is_not_memoised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0)
    real_run = subprocess.run

    def times_out(argv: Any, *args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))

    monkeypatch.setattr(subprocess, "run", times_out)
    assert origin.commit_author_timestamps(repo) is None
    monkeypatch.setattr(subprocess, "run", real_run)
    assert origin.commit_author_timestamps(repo) == [_T0]


@files_only
def test_the_whole_history_memo_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(origin, "_TIMESTAMPS_MEMO_CAP", 1)
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")
    _commit(first, "seed", when=_T0)
    _commit(second, "seed", when=_T1)
    assert origin.commit_author_timestamps(first) == [_T0]
    assert origin.commit_author_timestamps(second) == [_T1]
    assert len(origin._TIMESTAMPS_MEMO) == 1
    assert origin.commit_author_timestamps(first) == [_T0]


def test_the_walk_runs_to_the_head_it_is_keyed_on(tmp_path: Path) -> None:
    """The walk memo is keyed on a head, so the range it stores must end
    there, whatever HEAD names by the time the walk runs."""
    repo = _repo(tmp_path)
    anchor = _commit(repo, "a", when=_T0, files={"a.txt": "a\n"})
    middle = _commit(repo, "b", when=_T1, files={"b.txt": "b\n"})
    _commit(repo, "c", when=_T2, files={"c.txt": "c\n"})
    walk = origin.commits_since_anchor(
        repo, anchor, toplevel=repo.resolve(), head=middle
    )
    assert walk is not None
    assert walk.commits == (middle,)
    assert walk.touched == {"b.txt": frozenset({middle})}


# ---------------------------------------------------------------------------
# The surfaces stay in lockstep
# ---------------------------------------------------------------------------


async def test_memory_show_reads_the_same_block_around_a_warm_search(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_show reads the same `commit_drift` block before and after two
    searches that fill every memo, and the search hits carry that block's
    count and basis. The two surfaces share the whole-history memo and the
    head; neither memo changes what either surface says."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module

    repo = _repo(tmp_path)
    anchor = _commit(
        repo, "seed", when=_T0, files={"notes0.md": "a\n", "notes1.md": "b\n"}
    )
    store = Store(memory_dir)
    caller = _caller(repo)
    author = _write(
        store, caller, monkeypatch, f"quokka widget rule lives in {repo / 'notes0.md'}"
    )
    reach = _write(
        store,
        caller,
        monkeypatch,
        f"quokka widget lore lives in {repo / 'notes1.md'}",
        verified_head=anchor,
    )
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n", "notes1.md": "b1\n"})

    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    monkeypatch.setattr(handlers_module, "capture_origin", lambda cwd=None: caller)
    monkeypatch.setattr(server_module, "capture_origin", lambda cwd=None: caller)

    before = {
        i: await _mcp_call(server, "memory_show", {"id": i}) for i in (author, reach)
    }
    for _ in range(2):
        raw = await _mcp_call(server, "memory_search", {"query": "quokka widget"})
        hits = raw.get("result", raw) if isinstance(raw, dict) else raw
    after = {
        i: await _mcp_call(server, "memory_show", {"id": i}) for i in (author, reach)
    }
    by_id = {hit["id"]: hit for hit in hits}
    for memory_id in (author, reach):
        block = before[memory_id]["commit_drift"]
        assert block is not None and block["commits_since_verify"] == 1
        assert after[memory_id]["commit_drift"] == block
        assert (
            after[memory_id]["staleness_verdict"]
            == before[memory_id]["staleness_verdict"]
        )
        assert by_id[memory_id]["commit_drift_count"] == block["commits_since_verify"]
        assert by_id[memory_id]["commit_drift_basis"] == block["basis"]


# ---------------------------------------------------------------------------
# A git process that failed is not an answer
# ---------------------------------------------------------------------------


def _day(days: int) -> datetime:
    return _T0 + timedelta(days=days)


def _walks(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [call for call in calls if "--boundary" in call]


def _drift(out: list[dict[str, Any]], memory_id: str) -> tuple[Any, Any]:
    hit = _by_id(out)[memory_id]
    return hit.get("commit_drift_count"), hit.get("commit_drift_basis")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file"
)
def test_a_git_process_that_exited_non_zero_is_not_memoised(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that ran and failed folds into the same fallback as one
    that could not run: the unfiltered count, the author-date basis. The
    uncached code takes that fallback on the call that met the failure and
    not on the next, so no memo keeps a value computed across one. The
    failure is read permission withheld from one loose tree object for the
    first attach, a stand-in for an object briefly unreadable: the walk and
    the path-filtered logs read trees and exit 128, while the whole-history
    log reads only commits and answers."""
    repo = _repo(tmp_path)
    anchor = _commit(
        repo, "c1", when=_day(0), files={"src/app.py": _MODULE, "README.md": "r\n"}
    )
    _commit(repo, "c2", when=_day(20), files={"README.md": "r2\n"})
    unreadable = _commit(repo, "c3", when=_day(30), files={"README.md": "r3\n"})
    _commit(
        repo,
        "c4",
        when=_day(40),
        files={"src/app.py": _MODULE.replace("TIMEOUT = 30", "TIMEOUT = 31")},
    )
    _commit(repo, "c5", when=_day(50), files={"README.md": "r5\n"})
    _commit(repo, "c6", when=_day(60), files={"README.md": "r6\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    body = "the timeout lives in src/app.py"
    author = _write(store, caller, monkeypatch, f"widget rule a: {body}", at=_day(10))
    reach = _write(
        store,
        caller,
        monkeypatch,
        f"widget rule b: {body}",
        at=_day(10),
        verified_head=anchor,
    )
    quiet = _write(store, caller, monkeypatch, f"widget rule c: {body}", at=_day(45))
    tree = _git(repo, "rev-parse", f"{unreadable}^{{tree}}")
    victim = repo / ".git" / "objects" / tree[:2] / tree[2:]
    assert victim.is_file(), "premise: the tree is a loose object"
    memories = store.load_all()
    order = (author, reach, quiet)

    mode = victim.stat().st_mode
    victim.chmod(0)
    try:
        during, _ = _attach(memories, caller, monkeypatch)
    finally:
        victim.chmod(mode)
    assert [_drift(during, i) for i in order] == [
        (5, "author-date"),
        (5, "author-date"),
        (2, "author-date"),
    ], "premise: the failure took the fallbacks"
    assert len(_response._DRIFT_MEMO) == 0
    assert len(origin._WALK_MEMO) == 0

    cached, _ = _attach(memories, caller, monkeypatch)
    _caches.clear_all()
    uncached, _ = _attach(memories, caller, monkeypatch)
    assert [_drift(uncached, i) for i in order] == [
        (1, "author-date"),
        (1, "reachability"),
        (0, "author-date"),
    ]
    assert cached == uncached


@pytest.mark.parametrize("kind", ["missing", "rewritten"])
def test_a_dead_anchor_forks_once_per_attach_and_is_not_memoised(
    kind: str, memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk that comes back None is not kept past the attach that asked
    for it: a missing anchor fails the process, and a failure may clear;
    an anchor HEAD no longer descends from answers None without failing,
    and is not kept either. Within one attach it is remembered, so three
    hits at one dead anchor fork one walk. A hit resolved around a walk git
    answered is kept per hit; one resolved around a failed walk is not,
    and the next attach walks once again."""
    repo = _repo(tmp_path)
    names = [f"notes{i}.md" for i in range(3)]
    _commit(repo, "seed", when=_T0, files={name: "a\n" for name in names})
    if kind == "missing":
        dead = "b" * 40
    else:
        dead = _commit(repo, "amended away", when=_day(1), files={names[0]: "x\n"})
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
    store = Store(memory_dir)
    caller = _caller(repo)
    ids = [
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule {i} lives in {repo / name}",
            verified_head=dead,
        )
        for i, name in enumerate(names)
    ]
    _commit(repo, "post", when=_T1, files={name: "b\n" for name in names})
    memories = store.load_all()

    cold, cold_calls = _attach(memories, caller, monkeypatch)
    assert {_drift(cold, i) for i in ids} == {(1, "author-date")}
    assert len(_walks(cold_calls)) == 1, "three hits at one dead anchor: one walk"
    assert [key for key in origin._WALK_MEMO if key[1] == dead] == []

    warm, warm_calls = _attach(memories, caller, monkeypatch)
    assert warm == cold
    if _FILES:
        if kind == "missing":
            assert len(_response._DRIFT_MEMO) == 0, "resolved around a failure"
            assert len(_walks(warm_calls)) == 1, "the failed walk runs again"
        else:
            assert len(_response._DRIFT_MEMO) == 3
            assert warm_calls == []


@pytest.mark.parametrize("rollup", ["commit_drift_debt", "curation drifted"])
def test_a_health_pass_walks_a_dead_anchor_once(
    rollup: str, memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The health rollups resolve every memory in one pass and remember
    that pass's dead anchors, as a search does: three memories verified at
    one missing anchor fork one walk, and nothing of it outlives the
    pass."""
    from bettermemory import health

    repo = _repo(tmp_path)
    names = [f"notes{i}.md" for i in range(3)]
    _commit(repo, "seed", when=_T0, files={name: "a\n" for name in names})
    store = Store(memory_dir)
    caller = _caller(repo)
    for i, name in enumerate(names):
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule {i} lives in {repo / name}",
            verified_head="b" * 40,
        )
    _commit(repo, "post", when=_T1, files={name: "b\n" for name in names})
    memories = store.load_all()
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(argv[1:]))
        return real_run(argv, *args, **kwargs)

    for _ in range(2):
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "run", spy)
            if rollup == "commit_drift_debt":
                report = health.compute_health(
                    memories, [], caller_origin=caller, now=_NOW
                )
                assert report.commit_drift_debt is not None
                drifted = report.commit_drift_debt.total_drifted
            else:
                counts = health.curation_counts(
                    memories, [], caller_origin=caller, now=_NOW
                )
                drifted = counts["drifted"]
        assert drifted == 3
        assert len(_walks(calls)) == 1
    assert len(origin._WALK_MEMO) == 0


# ---------------------------------------------------------------------------
# The attribute files the patch stream reads
# ---------------------------------------------------------------------------


@pytest.fixture
def git_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Git's global and system configuration kept off the developer's own:
    an empty global file, no system file, and XDG_CONFIG_HOME under the
    test's directory, where git looks for the global attributes file."""
    home = tmp_path / "xdg"
    (home / "git").mkdir(parents=True)
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return home


def _claimed(
    tmp_path: Path,
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim: str,
) -> tuple[Path, Origin, list[Any], list[str]]:
    """A repository whose two later commits change `other` in the claimed
    module, and two memories claiming `claim`, one counted from an anchor
    and one on author date. The weak tier reads the hunks and finds the
    claim untouched, so both count 0; where the hunks read as binary it
    cannot index them and both count the 2 commits that touched the file."""
    repo = _repo(tmp_path)
    anchor = _commit(repo, "c1", when=_day(0), files={"src/app.py": _MODULE})
    store = Store(memory_dir)
    caller = _caller(repo)
    body = "the handler is declared in the app module"
    ids = [
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule a: {body}",
            at=_day(10),
            claims=[claim],
            verified_head=anchor,
        ),
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule b: {body}",
            at=_day(10),
            claims=[claim],
        ),
    ]
    for n in (2, 3):
        changed = _MODULE.replace(
            "def other():\n    return 1", f"def other():\n    return {n}"
        )
        _commit(repo, f"c{n}", when=_day(10 * n), files={"src/app.py": changed})
    return repo, caller, store.load_all(), ids


def _counts(out: list[dict[str, Any]], ids: list[str]) -> list[Any]:
    return [_by_id(out)[i].get("commit_drift_count") for i in ids]


def _cached_and_uncached(
    memories: list[Any], caller: Origin, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The attach the memos answer now, then the one every memo cleared
    answers; the cached one runs first, so it reads nothing the other
    computed."""
    cached, _ = _attach(memories, caller, monkeypatch)
    _caches.clear_all()
    uncached, _ = _attach(memories, caller, monkeypatch)
    return cached, uncached


@pytest.mark.parametrize("source", ["root", "directory", "info", "global"])
def test_an_attribute_file_the_patch_stream_reads_is_in_the_key(
    source: str,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git log -p` reads the diff attribute from the working tree's
    .gitattributes on the claimed file's path, from info/attributes and
    from the global attributes file. `-diff` there turns the claimed file's
    hunks into "Binary files differ", which the weak tier cannot index, and
    the governed half counts any touch. An edit to any of them, committed
    or not, moves git's answer under an unchanged head: the warm attach
    answers as the cold one after the edit and after its removal."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = {
        "root": repo / ".gitattributes",
        "directory": repo / "src" / ".gitattributes",
        "info": repo / ".git" / "info" / "attributes",
        "global": git_config_home / "git" / "attributes",
    }[source]
    for _ in range(2):
        before, calls = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [0, 0]
    if _FILES:
        assert calls == [], "premise: the memo answers"

    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [2, 2], "premise: the attribute moved git"
    assert cached == uncached

    attributes.unlink()
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [0, 0]
    assert cached == uncached


@pytest.mark.parametrize("shape", ["directory claim", "attributes file in config"])
def test_attributes_the_files_cannot_settle_are_never_memoised(
    shape: str,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A governed spec that names a directory takes the diff attribute of
    every file below it from .gitattributes files anywhere under it, and a
    core.attributesFile in the repository's configuration names a file no
    stat of the repository covers. Neither is settled by a handful of
    stats, so such a hit resolves through git on every attach, as the
    uncached code does."""
    claim = "src" if shape == "directory claim" else "src/app.py::handler"
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim=claim
    )
    if shape == "directory claim":
        attributes = repo / "src" / ".gitattributes"
    else:
        attributes = tmp_path / "attributes"
        attributes.write_text("", encoding="utf-8")
        _git(repo, "config", "core.attributesFile", str(attributes))
    for _ in range(2):
        before, calls = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [0, 0]
    assert calls, "resolved through git again"
    assert len(_response._DRIFT_MEMO) == 0

    attributes.write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [2, 2], "premise: the attribute moved git"
    assert cached == uncached


def test_an_attribute_file_that_moves_during_the_attach_stores_nothing(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patch stream reads the attribute files as it runs, so one
    written after the hits were keyed and removed before the next attach
    would leave that attach's answer under a key that reads as unchanged.
    Nothing from such an attach is kept."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = repo / ".gitattributes"
    real_stream = origin.commit_patch_stream

    def write_then_read(*args: Any, **kwargs: Any) -> Any:
        attributes.write_text("*.py -diff\n", encoding="utf-8")
        return real_stream(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(verify, "commit_patch_stream", write_then_read)
        during, _ = _attach(memories, caller, monkeypatch)
    assert _counts(during, ids) == [2, 2], "premise: the stream read the attribute"
    assert len(_response._DRIFT_MEMO) == 0

    attributes.unlink()
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [0, 0]
    assert cached == uncached


def test_a_hit_without_governed_claims_reads_no_attribute_file(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a governed claim reaches the patch stream, so only a hit that
    carries one stats the attribute files and keys on them."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n", "pkg/mod.py": _MODULE})
    store = Store(memory_dir)
    caller = _caller(repo)
    plain = _write(
        store, caller, monkeypatch, f"widget rule a lives in {repo / 'notes0.md'}"
    )
    claimed = _write(
        store,
        caller,
        monkeypatch,
        "widget rule e is anchored in pkg/mod.py",
        claims=["pkg/mod.py::handler"],
    )
    _commit(
        repo,
        "post",
        when=_T1,
        files={
            "notes0.md": "a1\n",
            "pkg/mod.py": _MODULE.replace("def other():", "def other(flag=False):"),
        },
    )
    memories = store.load_all()
    stats: list[str] = []

    def spy(real: Any) -> Any:
        def counted(path: Any, *args: Any, **kwargs: Any) -> Any:
            if os.path.basename(os.fspath(path)) in (".gitattributes", "attributes"):
                stats.append(os.fspath(path))
            return real(path, *args, **kwargs)

        return counted

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", spy(os.stat))
        patch.setattr(os, "lstat", spy(os.lstat))
        _attach([m for m in memories if m.id == plain], caller, monkeypatch)
        assert stats == []
        _attach([m for m in memories if m.id == claimed], caller, monkeypatch)
    if _FILES:
        assert stats, "the claim-carrying hit keys on the attribute files"
        attributes_by_claims = {key[4]: key[7] for key in _response._DRIFT_MEMO}
        assert set(attributes_by_claims) == {(), ("pkg/mod.py::handler",)}
        assert attributes_by_claims[()] is None
        assert attributes_by_claims[("pkg/mod.py::handler",)] is not None


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


class _Interleaved(OrderedDict[Any, Any]):
    """A memo whose look-up of one key starts another thread's work and
    gives it 100 ms before returning: the other thread inserts past a bound
    of one. Under the memo's lock it waits for the lock, and evicts only
    after the hit has been touched; without the lock the key is gone before
    `move_to_end`, which raises KeyError. The token cache's test is the
    model (`tests/test_token_cache.py`)."""

    def __init__(self, source: OrderedDict[Any, Any], key: Any, work: Any) -> None:
        super().__init__(source)
        self.key = key
        self.work = work
        self.others: list[threading.Thread] = []
        self.errors: list[BaseException] = []

    def _run(self) -> None:
        try:
            self.work()
        except BaseException as exc:  # recorded for the test to read
            self.errors.append(exc)

    def _interleave(self, key: Any) -> None:
        if key == self.key and not self.others:
            other = threading.Thread(target=self._run)
            self.others.append(other)
            other.start()
            other.join(timeout=0.1)

    def get(self, key: Any, default: Any = None) -> Any:
        value = super().get(key, default)
        self._interleave(key)
        return value

    def __contains__(self, key: object) -> bool:
        found = super().__contains__(key)
        self._interleave(key)
        return found


def test_the_whole_history_memo_touches_a_hit_under_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read is stubbed, so the other thread inserts at once."""
    monkeypatch.setattr(origin, "_TIMESTAMPS_MEMO_CAP", 1)
    monkeypatch.setattr(origin, "_read_author_timestamps", lambda cwd, revision: [_T0])
    touched = (Path("/touched"), "a" * 40)
    evicting = (Path("/evicting"), "b" * 40)
    first = origin.commit_author_timestamps(touched[0], located=touched)
    memo = _Interleaved(
        origin._TIMESTAMPS_MEMO,
        (str(touched[0]), touched[1]),
        lambda: origin.commit_author_timestamps(evicting[0], located=evicting),
    )
    monkeypatch.setattr(origin, "_TIMESTAMPS_MEMO", memo)
    assert origin.commit_author_timestamps(touched[0], located=touched) is first
    memo.others[0].join()
    assert memo.errors == []
    assert list(memo) == [(str(evicting[0]), evicting[1])]


def test_the_walk_memo_touches_a_hit_under_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anchor at its head is an empty walk, stored without a process,
    so the other thread inserts at once."""
    monkeypatch.setattr(origin, "_WALK_MEMO_CAP", 1)
    anchor = "a" * 40
    touched, evicting = Path("/touched"), Path("/evicting")
    first = origin.commits_since_anchor(touched, anchor, toplevel=touched, head=anchor)
    assert first is not None
    memo = _Interleaved(
        origin._WALK_MEMO,
        (str(touched), anchor, anchor),
        lambda: origin.commits_since_anchor(
            evicting, anchor, toplevel=evicting, head=anchor
        ),
    )
    monkeypatch.setattr(origin, "_WALK_MEMO", memo)
    walk = origin.commits_since_anchor(touched, anchor, toplevel=touched, head=anchor)
    assert walk is first
    memo.others[0].join()
    assert memo.errors == []
    assert list(memo) == [(str(evicting), anchor, anchor)]


@files_only
def test_the_drift_memo_touches_a_hit_under_its_lock(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two hits whose anchors escape the repository resolve without a
    process once the head's history is read, so the other thread's attach
    runs inside the 100 ms."""
    repo = _repo(tmp_path)
    _commit(repo, "seed", when=_T0, files={"notes0.md": "a\n"})
    store = Store(memory_dir)
    caller = _caller(repo)
    touched = _write(
        store,
        caller,
        monkeypatch,
        "widget rule a: the router config lives at /data/compose/.env on the board",
    )
    evicting = _write(
        store,
        caller,
        monkeypatch,
        "widget rule b: the switch config lives at /data/switch/.env on the board",
    )
    _commit(repo, "post", when=_T1, files={"notes0.md": "a1\n"})
    memories = store.load_all()
    builder = ResponseBuilder(stale_after_days=30)

    def attach_one(memory_id: str) -> list[dict[str, Any]]:
        subset = [m for m in memories if m.id == memory_id]
        hits = run_search(subset, "widget rule", max_results=50)
        out = [builder.hit_to_dict(hit, now=_NOW) for hit in hits]
        builder.attach_commit_drift_counts(out, hits, subset, caller_origin=caller)
        return out

    first = attach_one(touched)
    (touched_key,) = _response._DRIFT_MEMO
    monkeypatch.setattr(_response, "_DRIFT_MEMO_CAP", 1)
    memo = _Interleaved(
        _response._DRIFT_MEMO, touched_key, lambda: attach_one(evicting)
    )
    monkeypatch.setattr(_response, "_DRIFT_MEMO", memo)
    assert attach_one(touched) == first
    memo.others[0].join()
    assert memo.errors == []
    assert len(memo) == 1 and touched_key not in memo
