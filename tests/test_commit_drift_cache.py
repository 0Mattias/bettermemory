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
import time
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


def _git_input(repo: Path, text: str, *args: str) -> str:
    """`_git` with `text` on git's standard input, sent as UTF-8 bytes: a
    text-mode pipe on Windows ends each line in CRLF, and --index-info
    reads the CR as the last byte of the path."""
    return (
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            input=text.encode("utf-8"),
        )
        .stdout.decode("utf-8")
        .strip()
    )


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
    verified_paths: list[str] | None = None,
) -> str:
    memory = store.write(
        content=content, scopes=["tools"], origin=caller, claims=claims
    )
    _stamp(
        store,
        monkeypatch,
        memory.id,
        at=at,
        verified_head=verified_head,
        verified_paths=verified_paths,
    )
    return memory.id


def _stamp(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
    *,
    at: datetime,
    verified_head: str | None = None,
    verified_paths: list[str] | None = None,
) -> None:
    """`mark_verified` with the stamp's instant pinned, so the fixtures'
    commit dates fall on a known side of it."""
    import bettermemory.store as store_module

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "utcnow", lambda: at)
        store.mark_verified(
            memory_id, verified_head=verified_head, verified_paths=verified_paths
        )


def _attach(
    memories: list[Any],
    caller: Origin,
    monkeypatch: pytest.MonkeyPatch,
    *,
    query: str = "widget rule",
    first: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, ...]]]:
    """One search's hits, decorated; and every git argv the decoration
    spawned. Any process at all is recorded, not only `origin._git`'s.
    The hits whose ids are in `first` are decorated before the rest, each
    group in rank order, so a test can place a change between two hits'
    resolutions."""
    hits = run_search(memories, query, max_results=50)
    if first:
        hits.sort(key=lambda hit: hit.id not in first)
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


def _age(*paths: Path) -> None:
    """Set the mtime of each file under `paths` an hour back. A test that
    rewrites a file moments after its fixture wrote it could otherwise
    land within one tick of the filesystem's clock (a jiffy where the
    kernel stamps from a coarse clock), where a stamp that also got its
    old inode number back reads as unchanged: the limit `githead.stamp`
    declares, which no test here means to exercise."""
    past = time.time_ns() - 3600 * 10**9
    for path in paths:
        for found in [path, *path.rglob("*")] if path.is_dir() else [path]:
            if found.is_file():
                os.utime(found, ns=(past, past))


def _point_head(repo: Path, move: str, to: str) -> None:
    """Move the checkout's head: `git switch` to the branch `to` for
    "switch", `git reset --soft` of the checked-out branch to the commit
    `to` for "reset"."""
    if move == "switch":
        _git(repo, "switch", "-q", to)
    else:
        _git(repo, "reset", "-q", "--soft", to)


@files_only
@pytest.mark.parametrize("dead", [False, True], ids=["live anchors", "dead anchor"])
@pytest.mark.parametrize("move", ["switch", "reset"])
def test_a_head_that_leaves_and_returns_during_the_attach_stores_nothing(
    move: str,
    dead: bool,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head moved to another commit and back while one attach runs (a
    branch switched and switched back, or the checked-out branch reset to
    another commit and back) reads at the end as the head the attach
    settled at the start, while a path-filtered log in between read the
    other head's history. Git writes HEAD and a branch's ref through a lock
    file and a rename, so the move and the return leave the file with a new
    inode and ctime: the attach reads `githead.signature` when it settles
    the head and again at the end, and keeps nothing when the two differ.
    With a dead anchor, hits verified at a commit the repository lacks are
    resolved first and the head leaves as their walk fails, so the attach's
    record of dead walks, keyed on the settled head, answers while the head
    is elsewhere. The other head holds the same tree as this one, so a
    switch to it and back rewrites HEAD and no file of the working tree:
    only the signature can see it."""
    repo = _repo(tmp_path)
    base = _commit(
        repo, "c0", when=_day(0), files={"src/app.py": _MODULE, "README.md": "r\n"}
    )
    _commit(
        repo,
        "c1",
        when=_day(60),
        files={"src/app.py": _MODULE.replace("TIMEOUT = 30", "TIMEOUT = 31")},
    )
    head = _commit(repo, "c2", when=_day(70), files={"README.md": "r2\n"})
    # The same tree, one commit on c0, authored before the stamp: its
    # history has no change to src/app.py after it.
    other = _git(
        repo, "commit-tree", f"{head}^{{tree}}", "-p", base, "-m", "s1", when=_day(5)
    )
    _git(repo, "branch", "side", other)
    store = Store(memory_dir)
    caller = _caller(repo)
    author = _write(
        store,
        caller,
        monkeypatch,
        f"widget rule a lives in {repo / 'src' / 'app.py'}",
        at=_day(30),
    )
    first = [
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule {i} lives in {repo / 'README.md'}",
            at=_day(30),
            verified_head="b" * 40,
        )
        for i in range(2 if dead else 0)
    ]
    memories = store.load_all()
    _age(repo / ".git" / "HEAD", repo / ".git" / "refs")
    away, back = ("side", "main") if move == "switch" else (other, head)
    real_log = origin.commit_author_timestamps_touching_pathspecs
    real_walk = origin._walk_reachable
    moved: list[str] = []

    def leave() -> None:
        if not moved:
            _point_head(repo, move, away)
            moved.append(away)

    def walk_then_leave(*args: Any, **kwargs: Any) -> Any:
        walk = real_walk(*args, **kwargs)
        leave()
        return walk

    def read_away_then_return(cwd: Any, pathspecs: Any, **kwargs: Any) -> Any:
        if "src/app.py" not in pathspecs:
            return real_log(cwd, pathspecs, **kwargs)
        leave()
        try:
            return real_log(cwd, pathspecs, **kwargs)
        finally:
            _point_head(repo, move, back)

    with monkeypatch.context() as patch:
        patch.setattr(
            verify, "commit_author_timestamps_touching_pathspecs", read_away_then_return
        )
        if dead:
            patch.setattr(origin, "_walk_reachable", walk_then_leave)
        during, calls = _attach(memories, caller, monkeypatch, first=first)
    assert moved, "premise: the head left during the attach"
    assert _git(repo, "rev-parse", "HEAD") == head, "premise: and came back"
    assert _drift(during, author) == (0, "author-date"), (
        "premise: the log read the other head's history"
    )
    if dead:
        assert len(_walks(calls)) == 1, "premise: one dead walk, remembered"

    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _drift(uncached, author) == (1, "author-date")
    assert cached == uncached


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


@files_only
@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(
            [("config", "branch.worktree-agent-a1.remote", "origin")],
            id="a branch named for a worktree",
        ),
        pytest.param(
            [("remote", "add", "worktrees", "git@example.com:me/worktree.git")],
            id="a remote named worktrees",
        ),
        pytest.param(
            [("config", "extensions.worktreeConfig", "true")],
            id="worktreeConfig and no config.worktree",
        ),
        pytest.param(
            [
                ("config", "extensions.worktreeConfig", "true"),
                ("config", "--worktree", "user.name", "someone"),
            ],
            id="a config.worktree that moves nothing",
        ),
    ],
)
def test_the_files_answer_where_the_config_only_mentions_worktree(
    commands: list[tuple[str, ...]], tmp_path: Path
) -> None:
    """The configuration is read as git reads it: `core.worktree` set in
    the common config or in config.worktree moves the root, and a branch,
    a remote or `extensions.worktreeConfig` whose name holds the word does
    not, so the files answer there as git does."""
    repo = _repo(tmp_path)
    head = _commit(repo, "seed", when=_T0)
    for command in commands:
        _git(repo, *command)
    git_root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    assert git_root == repo.resolve(), "premise: git names the checkout"
    assert origin.toplevel_and_head_from_files(repo) == (git_root, head)


@files_only
def test_the_files_decline_where_config_worktree_moves_the_root(tmp_path: Path) -> None:
    """Under extensions.worktreeConfig, `core.worktree` in config.worktree
    moves the root git names, as it does in the common config."""
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    head = _commit(repo, "seed", when=_T0)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.worktree", str(elsewhere))
    assert origin.toplevel_and_head_from_files(repo) is None
    assert origin.repo_toplevel_and_head(repo) == (elsewhere.resolve(), head)


@files_only
@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(b'[core "worktre\\e\x00"]\n\tx = {elsewhere}\n', id="escaped"),
        pytest.param(b'[core "worktree\x00"]\n\tx = {elsewhere}\n', id="literal"),
    ],
)
def test_the_files_decline_where_a_nul_byte_names_core_worktree(
    tail: bytes, tmp_path: Path
) -> None:
    """Git ends a variable's name at a NUL byte, so a quoted subsection
    `worktree\\0` (or an escaped spelling of it) sets core.worktree. A
    config holding a NUL byte declines."""
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _commit(repo, "seed", when=_T0)
    config = repo / ".git" / "config"
    config.write_bytes(
        config.read_bytes() + tail.replace(b"{elsewhere}", os.fsencode(elsewhere))
    )
    git_root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    assert git_root == elsewhere.resolve(), "premise: git reads core.worktree"
    assert origin.toplevel_and_head_from_files(repo) is None


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


def _attributes_file(source: str, repo: Path, config_home: Path) -> Path:
    """The attributes file `source` names: the working tree's root
    .gitattributes, the one in the claimed file's directory, the common
    directory's info/attributes, or the global attributes file."""
    return {
        "root": repo / ".gitattributes",
        "directory": repo / "src" / ".gitattributes",
        "info": repo / ".git" / "info" / "attributes",
        "global": config_home / "git" / "attributes",
    }[source]


@pytest.mark.parametrize("source", ["root", "directory", "info", "global"])
def test_an_attribute_file_created_and_removed_during_the_attach_stores_nothing(
    source: str,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attributes file created before one hit's patch stream runs and
    removed once it returns is absent at the start of the attach and at
    its end, so its own stamp reads the same both times, while that hit's
    stream read `-diff` from it. Creating or removing a file changes its
    directory's (mtime, ctime): the attach reads the stamps of the
    directories that hold the attribute files it keyed on when it keys
    them and again at the end, and keeps nothing when one moved."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = _attributes_file(source, repo, git_config_home)
    real_stream = origin.commit_patch_stream
    created: list[Path] = []

    def create_read_remove(*args: Any, **kwargs: Any) -> Any:
        if created:
            return real_stream(*args, **kwargs)
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text("*.py -diff\n", encoding="utf-8")
        created.append(attributes)
        try:
            return real_stream(*args, **kwargs)
        finally:
            attributes.unlink()

    with monkeypatch.context() as patch:
        patch.setattr(verify, "commit_patch_stream", create_read_remove)
        during, _ = _attach(memories, caller, monkeypatch)
    assert created and not attributes.exists(), "premise: created and removed"
    assert sorted(_counts(during, ids)) == [0, 2], (
        "premise: one hit's stream read the attribute, the other's did not"
    )

    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [0, 0]
    assert cached == uncached


def test_a_later_chain_through_a_changed_directory_stores_nothing(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two governed files share the root's .gitattributes. The attach keys
    the chain of the second only when its first hit comes up, after the
    first file's patch stream ran with a root .gitattributes created and
    removed: the root is held again then, and must keep the stamp read
    when the first chain was keyed, or the check at the end compares the
    root with itself."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    _commit(repo, "helper", when=_day(1), files={"lib/x.py": _UTIL})
    store = Store(memory_dir)
    later = _write(
        store,
        caller,
        monkeypatch,
        "widget rule c: the helper is declared in the x module",
        at=_day(10),
        claims=["lib/x.py::helper"],
    )
    memories = store.load_all()
    attributes = repo / ".gitattributes"
    real_stream = origin.commit_patch_stream
    created: list[Path] = []

    def create_read_remove(*args: Any, **kwargs: Any) -> Any:
        if created:
            return real_stream(*args, **kwargs)
        attributes.write_text("*.py -diff\n", encoding="utf-8")
        created.append(attributes)
        try:
            return real_stream(*args, **kwargs)
        finally:
            attributes.unlink()

    with monkeypatch.context() as patch:
        patch.setattr(verify, "commit_patch_stream", create_read_remove)
        during, _ = _attach(memories, caller, monkeypatch, first=ids)
    assert created and not attributes.exists(), "premise: created and removed"
    assert "commit_drift_count" in _by_id(during)[later], "premise: keyed after"
    assert sorted(_counts(during, ids)) == [0, 2], (
        "premise: one hit's stream read the attribute"
    )

    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [0, 0]
    assert cached == uncached


def test_a_directory_held_twice_keeps_its_first_stamp(tmp_path: Path) -> None:
    """The check at the end of a search compares each directory with the
    stamp read before anything could change in it: a directory held again
    after a file was created and removed in it keeps its first stamp, and
    the check sees the change."""
    past = time.time_ns() - 3600 * 10**9
    os.utime(tmp_path, ns=(past, past))
    held: dict[str, tuple[int, int]] = {}
    origin._hold_directory(tmp_path, held)
    first = held[str(tmp_path)]
    assert origin.directories_held(held)
    (tmp_path / "attributes").write_text("*.py -diff\n", encoding="utf-8")
    (tmp_path / "attributes").unlink()
    origin._hold_directory(tmp_path, held)
    assert held == {str(tmp_path): first}
    assert not origin.directories_held(held)


def test_a_missing_directory_is_held_through_its_nearest_ancestor(
    tmp_path: Path,
) -> None:
    """A directory that does not exist yet holds no stamp of its own, and
    creating it (to put an attributes file in it) changes its parent's."""
    past = time.time_ns() - 3600 * 10**9
    os.utime(tmp_path, ns=(past, past))
    held: dict[str, tuple[int, int]] = {}
    origin._hold_directory(tmp_path / "git" / "info", held)
    assert list(held) == [str(tmp_path)]
    assert origin.directories_held(held)
    (tmp_path / "git").mkdir()
    (tmp_path / "git").rmdir()
    assert not origin.directories_held(held)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file"
)
@pytest.mark.parametrize("readable_first", [True, False], ids=["to 000", "to 644"])
@pytest.mark.parametrize("source", ["root", "info", "global"])
def test_a_permission_change_to_an_attribute_file_is_a_new_key(
    source: str,
    readable_first: bool,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git reads an attributes file it cannot open as absent: it warns and
    exits 0. A chmod of a `*.py -diff` file to 000 therefore moves git's
    answer from the binary 2 to the weak tier's 0, and a chmod back moves
    it back, while the file's mtime, size and inode stay as they were. The
    stamp carries st_ctime_ns and st_mode, which a chmod moves, so the warm
    attach answers as the cold one in both directions."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = _attributes_file(source, repo, git_config_home)
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.py -diff\n", encoding="utf-8")
    if not readable_first:
        attributes.chmod(0)
    try:
        for _ in range(2):
            before, calls = _attach(memories, caller, monkeypatch)
        assert _counts(before, ids) == ([2, 2] if readable_first else [0, 0])
        if _FILES:
            assert calls == [], "premise: the memo answers"

        status = attributes.stat()
        attributes.chmod(0 if readable_first else 0o644)
        after = attributes.stat()
        assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
            status.st_mtime_ns,
            status.st_size,
            status.st_ino,
        ), "premise: only the mode moved"
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _counts(uncached, ids) == ([0, 0] if readable_first else [2, 2]), (
            "premise: git reads the unreadable file as absent"
        )
        assert cached == uncached
    finally:
        attributes.chmod(0o644)


@files_only
def test_a_repository_config_that_sets_log_follow_keeps_the_hit_out_of_the_memo(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With log.follow in the repository's config, the patch stream's
    single-pathspec `git log -p` follows the claimed file across a rename,
    and the commit that renamed it takes the diff attribute of the rename
    source's path, from a .gitattributes no chain of the claimed path
    covers. A config that sets log.follow keeps the hit out of the memo, so
    an untracked lib/.gitattributes written between two attaches is read by
    both."""
    repo = _repo(tmp_path)
    _git(repo, "config", "log.follow", "true")
    anchor = _commit(
        repo,
        "c1",
        when=_day(0),
        files={"lib/app.py": _MODULE, "src/keep.txt": "k\n"},
    )
    _git(repo, "mv", "lib/app.py", "src/app.py")
    renamed = _MODULE.replace("def handler():", "def handler(x=None):")
    _commit(repo, "c2", when=_day(20), files={"src/app.py": renamed})
    _commit(
        repo,
        "c3",
        when=_day(30),
        files={
            "src/app.py": renamed.replace(
                "def other():\n    return 1", "def other():\n    return 3"
            )
        },
    )
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
            claims=["src/app.py::handler"],
            verified_head=anchor,
        ),
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule b: {body}",
            at=_day(10),
            claims=["src/app.py::handler"],
        ),
    ]
    memories = store.load_all()
    for _ in range(2):
        before, calls = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [1, 1]

    (repo / "lib").mkdir(exist_ok=True)
    (repo / "lib" / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) != [1, 1], (
        "premise: the rename source's attributes moved git"
    )
    assert cached == uncached
    assert calls, "the second attach resolved through git again"
    assert len(_response._DRIFT_MEMO) == 0


# Config files, and what git reads from each: the variables `git config
# --file <file> --list --name-only` names, or None where git refuses the
# file. The attribute files decline where git reads log.follow and nowhere
# else, so the reader must name exactly git's variables, whatever the case,
# the spacing, the comments, the quoting and the continued values.
_CONFIG_TEXTS = [
    pytest.param(b"[log]\n\tfollow = true\n", id="log.follow"),
    pytest.param(b"[LOG]\n\tFOLLOW\n", id="upper case without a value"),
    pytest.param(b"[log] follow = true\n", id="key on the header line"),
    pytest.param(b"[Log]\nFollow=yes\n", id="mixed case without spaces"),
    pytest.param(b"[core] [log] follow", id="two headers and no newline"),
    pytest.param(b"\xef\xbb\xbf[log]\n\tfollow = 1\n", id="byte order mark"),
    pytest.param(b"[log]\r\n\tfollow = true\r\n", id="CRLF"),
    pytest.param(
        b"[log]\n  # c\n  ; c\n\n\tfollow\t=\ttrue # c\n", id="comments and tabs"
    ),
    pytest.param(b"[log]\n\tdate = iso\n\tfollow\n", id="second key"),
    pytest.param(b"[log]\n\tdate = x \\\n\n\tfollow\n", id="continued into a blank"),
    pytest.param(b"[log]\n\tdate = iso \\\n\tfollow = true\n", id="continued value"),
    pytest.param(
        b"[log]\n\tdate = iso # \\\n\tfollow = true\n", id="backslash in a comment"
    ),
    pytest.param(
        b'[log]\n\tdate = "iso # \\\n\tfollow = true"\n', id="continued quoted value"
    ),
    pytest.param(b'[log "follow"]\n\tx = 1\n', id="subsection named follow"),
    pytest.param(b"[Log.Follow]\n\tx = 1\n", id="dotted subsection"),
    pytest.param(b"[push]\n\tfollowTags = true\n", id="push.followTags"),
    pytest.param(
        b'[branch "follow-up"]\n\tremote = origin\n\tmerge = refs/heads/follow-up\n',
        id="branch follow-up",
    ),
    pytest.param(
        b'[remote "follower"]\n\turl = git@example.com:me/follow.git\n'
        b"\tfetch = +refs/heads/*:refs/remotes/follower/*\n",
        id="remote follower",
    ),
    pytest.param(b"[alias]\n\tfollow = log --follow\n", id="alias.follow"),
    pytest.param(b"# log.follow = true\n[core]\n\tbare = false\n", id="commented out"),
    pytest.param(b"[log]\n\tfollow-up = true\n\tfollows\n", id="longer keys"),
    pytest.param(b'[sect "x"] key = 1 [log] follow\n', id="header inside a value"),
    pytest.param(b'[sect "a\\"b"]\n\tkey\n', id="escaped quote in a subsection"),
    pytest.param(b"[log]\n\tfollow # c\n", id="comment after a bare key"),
    pytest.param(b"[log\n\tfollow\n", id="unclosed header"),
    pytest.param(b"[log ]\n\tfollow\n", id="space before the bracket"),
    pytest.param(b'[log]\n\tfollow = "open\n', id="unclosed quote"),
    pytest.param(b"[log]\n\tfollow = a \\q\n", id="unknown escape"),
    pytest.param(b"\xef\xbb[log]\n\tfollow\n", id="partial byte order mark"),
    pytest.param(b"[log]\n\tfollow\r= true\n", id="carriage return after the key"),
    pytest.param(b"", id="empty"),
]


def _git_variable_names(text: bytes, tmp_path: Path) -> set[bytes] | None:
    """The variables git reads from a config file holding `text`, or None
    when git refuses the file."""
    path = tmp_path / "config-under-test"
    path.write_bytes(text)
    result = subprocess.run(
        ["git", "config", "--file", str(path), "--list", "--name-only"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return {line for line in result.stdout.split(b"\n") if line}


@pytest.mark.parametrize("text", _CONFIG_TEXTS)
def test_the_config_reader_names_the_variables_git_reads(
    text: bytes, tmp_path: Path
) -> None:
    """`origin._config_variable_names` reads a config file as git's parser
    does (config.c): sections and their quoted or dotted subsections, keys
    with or without a value, comments, quoted and continued values, CRLF
    and a byte order mark, and it refuses the files git refuses."""
    assert origin._config_variable_names(text) == _git_variable_names(text, tmp_path)


@files_only
@pytest.mark.parametrize("text", _CONFIG_TEXTS)
def test_the_attribute_files_decline_where_git_reads_log_follow(
    text: bytes, git_config_home: Path, tmp_path: Path
) -> None:
    """The repository's config keeps the attribute files unkeyed exactly
    where git reads log.follow from it, in whatever case, spacing or
    layout git accepts; push.followTags, a branch, remote or subsection
    named with `follow`, a value or a comment holding it key them. A file
    git refuses keys nothing either."""
    names = _git_variable_names(text, tmp_path)
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    (repo / ".git" / "config").write_bytes(text)
    signature = origin.attribute_files_signature(repo, repo)
    if names is None or b"log.follow" in names:
        assert signature is None
    else:
        assert signature is not None


@files_only
@pytest.mark.parametrize(
    "commands",
    [
        pytest.param([("config", "push.followTags", "true")], id="push.followTags"),
        pytest.param(
            [
                ("config", "branch.follow-up.remote", "origin"),
                ("config", "branch.follow-up.merge", "refs/heads/follow-up"),
            ],
            id="branch follow-up",
        ),
        pytest.param(
            [("remote", "add", "follower", "git@example.com:me/follow.git")],
            id="remote follower",
        ),
    ],
)
def test_a_config_that_names_follow_without_log_follow_keeps_the_memo(
    commands: list[tuple[str, ...]],
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push.followTags, or a branch or a remote whose name holds `follow`,
    sets no log.follow, so the patch stream reads the attributes it reads
    without them. The governed hits are memoised: the warm attach forks
    nothing and answers as the cold one, and an attributes file written
    afterwards is read by the next attach."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    for command in commands:
        _git(repo, *command)
    for _ in range(2):
        before, calls = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [0, 0]
    assert calls == [], "the memo answers"

    (repo / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [2, 2], "premise: the attribute moved git"
    assert cached == uncached


# Config texts that set a variable through a NUL byte, which ends the name
# git keeps: `[log "follow\0"] x` is log.follow to git, and an escaped
# letter spells the word without its literal bytes.
_NUL_TEXTS = [
    pytest.param(b'[log "follow\x00"]\n\tx = true\n', id="log.follow"),
    pytest.param(b'[log "fo\\llow\x00"]\n\tx = true\n', id="log.follow escaped"),
    pytest.param(b'[core "attributesfil\\e\x00"]\n\tx = {file}\n', id="attributesfile"),
    pytest.param(
        b"[core]\n\tbare = false\n# a comment \x00 holding a NUL\n", id="comment"
    ),
]


@pytest.mark.parametrize("text", _NUL_TEXTS)
def test_the_config_reader_declines_a_file_holding_a_nul_byte(text: bytes) -> None:
    """Git keeps a variable's name only up to a NUL byte in it, which the
    reader does not model: a config holding one is read as none git could
    be shown to agree with, and every caller declines."""
    assert origin._config_variable_names(text) is None


@files_only
@pytest.mark.parametrize("text", _NUL_TEXTS)
def test_the_attribute_files_decline_where_the_config_holds_a_nul_byte(
    text: bytes, git_config_home: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    config = repo / ".git" / "config"
    elsewhere = os.fsencode(tmp_path / "attributes-elsewhere")
    config.write_bytes(config.read_bytes() + text.replace(b"{file}", elsewhere))
    assert origin.attribute_files_signature(repo, repo) is None


@files_only
def test_log_follow_set_through_a_nul_byte_keeps_the_hit_out_of_the_memo(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[log "follow\\0"] x = true` makes git read log.follow = true, so
    the patch stream follows the claimed file across its rename, as in
    test_a_repository_config_that_sets_log_follow_keeps_the_hit_out_of_the_memo:
    an untracked lib/.gitattributes written between two attaches is read
    by both."""
    repo = _repo(tmp_path)
    config = repo / ".git" / "config"
    config.write_bytes(config.read_bytes() + b'[log "follow\x00"]\n\tx = true\n')
    assert _git(repo, "config", "--bool", "--get", "log.follow") == "true", (
        "premise: git reads log.follow"
    )
    anchor = _commit(
        repo,
        "c1",
        when=_day(0),
        files={"lib/app.py": _MODULE, "src/keep.txt": "k\n"},
    )
    _git(repo, "mv", "lib/app.py", "src/app.py")
    renamed = _MODULE.replace("def handler():", "def handler(x=None):")
    _commit(repo, "c2", when=_day(20), files={"src/app.py": renamed})
    _commit(
        repo,
        "c3",
        when=_day(30),
        files={
            "src/app.py": renamed.replace(
                "def other():\n    return 1", "def other():\n    return 3"
            )
        },
    )
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
            claims=["src/app.py::handler"],
            verified_head=anchor,
        ),
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule b: {body}",
            at=_day(10),
            claims=["src/app.py::handler"],
        ),
    ]
    memories = store.load_all()
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [1, 1]

    (repo / "lib").mkdir(exist_ok=True)
    (repo / "lib" / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) != [1, 1], (
        "premise: the rename source's attributes moved git"
    )
    assert cached == uncached


@files_only
def test_the_repository_config_is_parsed_once_per_stamp(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config's variables are read once per state of the file (its
    stamp), not on every search: a large config that holds the word
    `follow` would otherwise be parsed again by each warm search. A
    rewrite of the file is read once more."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    _git(repo, "config", "push.followTags", "true")
    parsed: list[int] = []
    real = origin._config_variable_names

    def counted(data: bytes) -> set[bytes] | None:
        parsed.append(len(data))
        return real(data)

    monkeypatch.setattr(origin, "_config_variable_names", counted)
    for _ in range(3):
        out, _ = _attach(memories, caller, monkeypatch)
    assert _counts(out, ids) == [0, 0]
    config_size = (repo / ".git" / "config").stat().st_size
    assert parsed.count(config_size) == 1, parsed

    _git(repo, "config", "push.default", "simple")
    for _ in range(2):
        _attach(memories, caller, monkeypatch)
    config_size = (repo / ".git" / "config").stat().st_size
    assert parsed.count(config_size) == 1, parsed


def _worktree_config(repo: Path, text: str) -> None:
    """Turn extensions.worktreeConfig on and write `text` as the
    repository's config.worktree, which git then reads after the common
    config."""
    _git(repo, "config", "extensions.worktreeConfig", "true")
    (repo / ".git" / "config.worktree").write_text(text, encoding="utf-8")


def _renamed_claim(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> tuple[Origin, list[Any], list[str]]:
    """The claimed module renamed after the stamp (lib/app.py to
    src/app.py), so a patch stream that follows renames diffs the renaming
    commit with the attributes of the rename source's path, and two
    memories claiming src/app.py::handler, one counted from an anchor and
    one on author date: each counts 1 unless lib/.gitattributes turns the
    diff off."""
    anchor = _commit(
        repo,
        "c1",
        when=_day(0),
        files={"lib/app.py": _MODULE, "src/keep.txt": "k\n"},
    )
    _git(repo, "mv", "lib/app.py", "src/app.py")
    renamed = _MODULE.replace("def handler():", "def handler(x=None):")
    _commit(repo, "c2", when=_day(20), files={"src/app.py": renamed})
    _commit(
        repo,
        "c3",
        when=_day(30),
        files={
            "src/app.py": renamed.replace(
                "def other():\n    return 1", "def other():\n    return 3"
            )
        },
    )
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
            claims=["src/app.py::handler"],
            verified_head=anchor,
        ),
        _write(
            store,
            caller,
            monkeypatch,
            f"widget rule b: {body}",
            at=_day(10),
            claims=["src/app.py::handler"],
        ),
    ]
    return caller, store.load_all(), ids


@files_only
def test_log_follow_in_config_worktree_keeps_the_hit_out_of_the_memo(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under extensions.worktreeConfig git reads config.worktree after the
    common config, so log.follow set there makes the patch stream follow
    the claimed file across its rename as it does from the common config:
    the attribute files decline, and an untracked lib/.gitattributes
    written between two attaches is read by both."""
    repo = _repo(tmp_path)
    _worktree_config(repo, "[log]\n\tfollow = true\n")
    assert _git(repo, "config", "--bool", "--get", "log.follow") == "true", (
        "premise: git reads config.worktree"
    )
    caller, memories, ids = _renamed_claim(tmp_path, memory_dir, monkeypatch, repo)
    assert origin.attribute_files_signature(repo, repo) is None
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [1, 1]

    (repo / "lib").mkdir(exist_ok=True)
    (repo / "lib" / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) != [1, 1], (
        "premise: the rename source's attributes moved git"
    )
    assert cached == uncached


@files_only
def test_core_attributesfile_in_config_worktree_keeps_the_hit_out_of_the_memo(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """core.attributesFile set in config.worktree sends git to a file the
    attribute files do not stamp, as it does from the common config: the
    hits with governed claims stay out of the memo, so an edit of that file
    between two attaches is read by both."""
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = tmp_path / "attributes-elsewhere"
    attributes.write_text("", encoding="utf-8")
    _worktree_config(repo, f"[core]\n\tattributesFile = {attributes}\n")
    assert _git(repo, "config", "--get", "core.attributesFile") == str(attributes), (
        "premise: git reads config.worktree"
    )
    assert origin.attribute_files_signature(repo, repo) is None
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _counts(before, ids) == [0, 0]

    attributes.write_text("*.py -diff\n", encoding="utf-8")
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [2, 2], "premise: the attributes file moved git"
    assert cached == uncached


@files_only
def test_config_worktree_is_read_only_where_the_extension_is_on(
    git_config_home: Path, tmp_path: Path
) -> None:
    """Without extensions.worktreeConfig git does not read config.worktree,
    and neither signature stamps it: log.follow written there keys the
    attribute files like any other repository, and a rewrite moves no walk
    key. With the extension on, the attribute files decline on the
    log.follow it sets and a rewrite is a new walk key."""
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    root = repo.resolve()
    worktree_config = repo / ".git" / "config.worktree"
    walk = origin.walk_files_signature(root)
    attributes = origin.attribute_files_signature(repo, repo)
    assert walk is not None and attributes is not None

    worktree_config.write_text("[log]\n\tfollow = true\n", encoding="utf-8")
    unread = subprocess.run(
        ["git", "config", "--bool", "--get", "log.follow"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert unread.returncode == 1, "premise: git reads no config.worktree"
    assert origin.walk_files_signature(root) == walk
    assert origin.attribute_files_signature(repo, repo) == attributes

    _git(repo, "config", "extensions.worktreeConfig", "true")
    assert origin.attribute_files_signature(repo, repo) is None
    keyed = origin.walk_files_signature(root)
    assert keyed is not None
    worktree_config.write_text(
        "[log]\n\tfollow = true\n\tshowRoot = false\n", encoding="utf-8"
    )
    assert origin.walk_files_signature(root) != keyed
    worktree_config.write_text("", encoding="utf-8")
    assert origin.attribute_files_signature(repo, repo) is not None


@files_only
def test_the_linked_worktrees_own_config_worktree_is_the_one_read(
    git_config_home: Path, tmp_path: Path
) -> None:
    """`git sparse-checkout set` in a linked worktree turns
    extensions.worktreeConfig on and writes that worktree's own
    config.worktree, under .git/worktrees/<name>. The signatures read the
    file of the worktree they are asked about: log.follow written there
    declines the linked worktree's attribute files and leaves the primary
    checkout's keyed."""
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a/x.txt": "x\n", "b/y.txt": "y\n"})
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), "main")
    _git(linked, "sparse-checkout", "set", "--cone", "a")
    assert _git(repo, "config", "--get", "extensions.worktreeConfig") == "true", (
        "premise: sparse-checkout turned the extension on"
    )
    gitdir = Path(_git(linked, "rev-parse", "--absolute-git-dir"))
    worktree_config = gitdir / "config.worktree"
    assert worktree_config.is_file(), "premise: the worktree's own config"
    assert origin.attribute_files_signature(linked, linked) is not None
    worktree_config.write_text(
        worktree_config.read_text(encoding="utf-8") + "[log]\n\tfollow = true\n",
        encoding="utf-8",
    )
    assert _git(linked, "config", "--bool", "--get", "log.follow") == "true"
    assert origin.attribute_files_signature(linked, linked) is None
    assert origin.attribute_files_signature(repo, repo) is not None


def test_an_attributes_file_created_with_its_directory_during_the_attach_stores_nothing(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the global attributes file's directory does not exist, the
    check holds its nearest existing ancestor. A directory and an
    attributes file in it created before one hit's patch stream and removed
    with the directory once the stream returns leave the file, and the
    directory, absent at the start of the attach and at its end: only the
    ancestor's (mtime, ctime) moved, and nothing is kept."""
    config_home = tmp_path / "xdg-without-git"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    repo, caller, memories, ids = _claimed(
        tmp_path, memory_dir, monkeypatch, claim="src/app.py::handler"
    )
    attributes = config_home / "git" / "attributes"
    real_stream = origin.commit_patch_stream
    created: list[Path] = []

    def create_read_remove(*args: Any, **kwargs: Any) -> Any:
        if created:
            return real_stream(*args, **kwargs)
        attributes.parent.mkdir()
        attributes.write_text("*.py -diff\n", encoding="utf-8")
        created.append(attributes)
        try:
            return real_stream(*args, **kwargs)
        finally:
            shutil.rmtree(attributes.parent)

    with monkeypatch.context() as patch:
        patch.setattr(verify, "commit_patch_stream", create_read_remove)
        during, _ = _attach(memories, caller, monkeypatch)
    assert created and not attributes.parent.exists(), "premise: created and removed"
    assert sorted(_counts(during, ids)) == [0, 2], (
        "premise: one hit's stream read the attribute, the other's did not"
    )

    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _counts(uncached, ids) == [0, 0]
    assert cached == uncached


def test_a_plain_hit_is_keyed_on_the_repository_config(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit with no claim and no recorded head reads no attribute file
    and walks nothing, but its path-filtered log reads the repository's
    config: with log.follow there, a log over the one cited path follows
    it across a rename and counts the commit that edited the rename
    source after the stamp too. Every key carries the config's stamp, so
    the warm attach answers as the cold one after the setting is written
    and after it is removed."""
    repo = _repo(tmp_path)
    _commit(
        repo, "c1", when=_day(0), files={"lib/app.py": _MODULE, "src/keep.txt": "k\n"}
    )
    _commit(
        repo,
        "c2",
        when=_day(20),
        files={"lib/app.py": _MODULE.replace("TIMEOUT = 30", "TIMEOUT = 31")},
    )
    _git(repo, "mv", "lib/app.py", "src/app.py")
    _commit(repo, "c3 rename", when=_day(30))
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store,
        caller,
        monkeypatch,
        f"widget rule a lives in {repo / 'src' / 'app.py'}",
        at=_day(10),
    )
    memories = store.load_all()
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (1, "author-date")

    for command, expected in (
        (("config", "log.follow", "true"), 2),
        (("config", "--unset", "log.follow"), 1),
    ):
        _git(repo, *command)
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "author-date"), (
            "premise: the setting moved the path-filtered log"
        )
        assert cached == uncached


def test_a_governed_path_holding_a_nul_byte_keys_nothing(tmp_path: Path) -> None:
    """A path no file can have, which `os.stat` refuses with ValueError, is
    one the chain cannot settle: None, as for any stat that fails."""
    assert origin.gitattributes_signature(tmp_path, ["src/a\x00b.py"]) is None
    assert origin.gitattributes_signature(tmp_path, ["src\x00/b.py"]) is None


@pytest.mark.parametrize(
    ("xdg", "home", "expected"),
    [
        (None, "", "/.config/git/attributes"),
        ("", "", "/.config/git/attributes"),
        (None, "/h", "/h/.config/git/attributes"),
        ("/x", "/h", "/x/git/attributes"),
        (None, None, None),
    ],
)
@files_only
def test_the_global_attributes_file_is_the_one_git_names(
    xdg: str | None,
    home: str | None,
    expected: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git's `xdg_config_home_for`: $XDG_CONFIG_HOME/git/attributes when
    that is set and not empty, else $HOME/.config/git/attributes whenever
    HOME is set, empty or not (an empty HOME names /.config/git/attributes),
    and no file with neither."""
    for name, value in (("XDG_CONFIG_HOME", xdg), ("HOME", home)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert origin._global_attributes_path(tmp_path) == expected


def test_a_relative_global_attributes_file_is_read_from_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git runs from the root, so a relative XDG_CONFIG_HOME or HOME names
    a file below it."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "home")
    expected = os.path.join(tmp_path, "home/.config/git/attributes")
    assert origin._global_attributes_path(tmp_path) == expected


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
# The walk and the files beside the history it lists
# ---------------------------------------------------------------------------

_UTIL = "def helper():\n    return 1\n"


def _root_in_range(
    tmp_path: Path,
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim: bool,
) -> tuple[Path, Origin, list[Any], str]:
    """A repository whose verified range holds a second root commit: an
    unrelated history adding lib/util.py, merged after the stamp, as
    `git subtree add` or `merge --allow-unrelated-histories` leaves it. The
    walk's --name-only listing names lib/util.py for that root commit only
    while log.showRoot is true, git's default. One memory, verified at the
    first root and anchored on lib/util.py by a claim or by its body."""
    repo = _repo(tmp_path)
    anchor = _commit(repo, "c0", when=_day(0), files={"src/app.py": _MODULE})
    _git(repo, "switch", "-q", "--orphan", "imported")
    _commit(repo, "r0", when=_day(15), files={"lib/util.py": _UTIL})
    _git(repo, "switch", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-edit",
        "--allow-unrelated-histories",
        "imported",
        when=_day(20),
    )
    store = Store(memory_dir)
    caller = _caller(repo)
    if claim:
        memory_id = _write(
            store,
            caller,
            monkeypatch,
            "widget rule u: the helper is declared in the util module",
            at=_day(10),
            claims=["lib/util.py::helper"],
            verified_head=anchor,
        )
    else:
        memory_id = _write(
            store,
            caller,
            monkeypatch,
            f"widget rule u lives in {repo / 'lib' / 'util.py'}",
            at=_day(10),
            verified_head=anchor,
        )
    return repo, caller, store.load_all(), memory_id


@pytest.mark.parametrize("surface", ["claim hit", "attested hit", "health"])
def test_the_walk_is_keyed_on_the_repository_config(
    surface: str,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log.showRoot = false in the repository's config drops the root
    commit's paths from the walk's listing, so a hit anchored on a file only
    that root commit added counts 0 instead of 1, under an unchanged head.
    The walk memo and the per-hit key carry the config's stamp: the search
    and the health rollup, which share the walk memo, answer as the cold
    code after the setting is written and after it is removed."""
    from bettermemory import health

    repo, caller, memories, memory_id = _root_in_range(
        tmp_path, memory_dir, monkeypatch, claim=surface == "claim hit"
    )

    def answer() -> Any:
        if surface == "health":
            report = health.compute_health(memories, [], caller_origin=caller, now=_NOW)
            assert report.commit_drift_debt is not None
            return report.commit_drift_debt.total_drifted
        out, _ = _attach(memories, caller, monkeypatch)
        return out

    def count(answered: Any) -> Any:
        return answered if surface == "health" else _drift(answered, memory_id)

    def drifting(n: int) -> Any:
        return n if surface == "health" else (n, "reachability")

    for _ in range(2):
        before = answer()
    assert count(before) == drifting(1)

    for command, expected in (
        (("config", "log.showRoot", "false"), 0),
        (("config", "--unset", "log.showRoot"), 1),
    ):
        _git(repo, *command)
        cached = answer()
        _caches.clear_all()
        uncached = answer()
        assert count(uncached) == drifting(expected), (
            "premise: the setting moved the walk"
        )
        assert cached == uncached


def _pinned_submodule(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Origin, list[Any], str]:
    """A superproject whose submodule `sub` was added and then moved after
    the stamp, and one memory attested on `sub`, verified at the commit
    before the submodule was added: its walk lists the gitlink's two
    changes unless `submodule.sub.ignore` drops them."""
    source = _repo(tmp_path, "source")
    _commit(source, "s1", when=_day(0), files={"a.txt": "a\n"})
    _commit(source, "s2", when=_day(1), files={"a.txt": "b\n"})
    repo = _repo(tmp_path, "super")
    anchor = _commit(repo, "c0", when=_day(0), files={"README.md": "r\n"})
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(source),
        "sub",
    )
    _git(repo, "commit", "-q", "-m", "add sub", when=_day(20))
    _git(repo / "sub", "checkout", "-q", "HEAD~1")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-q", "-m", "move sub", when=_day(30))
    store = Store(memory_dir)
    caller = _caller(repo)
    memory_id = _write(
        store,
        caller,
        monkeypatch,
        "widget rule s: the submodule pin",
        at=_day(10),
        verified_head=anchor,
        verified_paths=["sub"],
    )
    return repo, caller, store.load_all(), memory_id


def test_the_walk_is_keyed_on_the_working_trees_gitmodules(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submodule.<name>.ignore = all in the working tree's .gitmodules drops
    the gitlink's changes from the walk's --name-only listing, while the
    path-limited log still lists them: a hit attested on the submodule's
    path, verified before it was added, counts 0 with the setting and 2
    without it, under an unchanged head. The walk memo and the per-hit key
    carry the .gitmodules stamp."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    ignore = ("config", "-f", ".gitmodules", "submodule.sub.ignore")
    _git(repo, *ignore, "all")
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (0, "reachability")

    for command, expected in (
        (("config", "-f", ".gitmodules", "--unset", "submodule.sub.ignore"), 2),
        ((*ignore, "all"), 0),
    ):
        _git(repo, *command)
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "reachability"), (
            "premise: the setting moved the walk"
        )
        assert cached == uncached


def test_the_walk_is_keyed_on_config_worktree(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """diff.ignoreSubmodules = all in config.worktree, which git reads under
    extensions.worktreeConfig, drops the gitlink's changes from the walk's
    --name-only listing as it does from the common config: a hit attested
    on the submodule's path counts 0 with the setting and 2 without it,
    under an unchanged head. The walk memo and the per-hit key carry
    config.worktree's stamp."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    _git(repo, "config", "extensions.worktreeConfig", "true")
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (2, "reachability")

    worktree_config = repo / ".git" / "config.worktree"
    for text, expected in (("[diff]\n\tignoreSubmodules = all\n", 0), ("", 2)):
        worktree_config.write_text(text, encoding="utf-8")
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "reachability"), (
            "premise: the setting moved the walk"
        )
        assert cached == uncached


def test_the_walk_is_keyed_on_the_indexs_gitmodules_where_the_working_tree_has_none(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the working tree has no .gitmodules, git reads the index's
    copy, then HEAD's, for submodule.<name>.ignore. The index's copy sets
    ignore = all and the working tree's is removed; then the index's copy
    is reset to HEAD's, which sets none, and later set again. Only the
    index changes each time, under an unchanged head, and the warm attach
    answers as the cold one after each change."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    ignore = ("config", "-f", ".gitmodules", "submodule.sub.ignore", "all")

    def stage_ignore() -> None:
        _git(repo, "checkout", "-q", "--", ".gitmodules")
        _git(repo, *ignore)
        _git(repo, "add", ".gitmodules")
        (repo / ".gitmodules").unlink()

    def reset_index() -> None:
        _git(repo, "reset", "-q", "--", ".gitmodules")

    stage_ignore()
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (0, "reachability")

    for change, expected in ((reset_index, 2), (stage_ignore, 0)):
        change()
        assert not (repo / ".gitmodules").exists()
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "reachability"), (
            "premise: the index's copy moved the walk"
        )
        assert cached == uncached


def _unmerge(repo: Path, path: str = ".gitmodules") -> None:
    """Replace the index's stage-0 entry for `path` with stages 1 to 3 of
    the same blob, the shape a conflicted merge leaves, with the working
    tree untouched."""
    blob = _git(repo, "rev-parse", f":{path}")
    _git_input(repo, f"0 {'0' * 40}\t{path}\n", "update-index", "--index-info")
    _git_input(
        repo,
        "".join(f"100644 {blob} {stage}\t{path}\n" for stage in (1, 2, 3)),
        "update-index",
        "--index-info",
    )


def test_an_unmerged_gitmodules_in_the_index_is_keyed_while_the_working_tree_has_one(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git's repo_read_gitmodules reads the index first, and while the index
    holds .gitmodules unmerged it reads no .gitmodules at all, the working
    tree's included. With ignore = all committed and in the working tree,
    the index's copy goes unmerged (the walk lists the gitlink's two
    changes) and `git add` then resolves it (none): the working tree's file
    is untouched both times, and the warm attach answers as the cold one."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    _git(repo, "config", "-f", ".gitmodules", "submodule.sub.ignore", "all")
    _git(repo, "add", ".gitmodules")
    _git(repo, "commit", "-q", "-m", "ignore sub", when=_day(35))
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (0, "reachability")
    working_tree_copy = githead.stamp(repo / ".gitmodules")

    for change, expected in (
        (lambda: _unmerge(repo), 2),
        (lambda: _git(repo, "add", ".gitmodules"), 0),
    ):
        change()
        assert githead.stamp(repo / ".gitmodules") == working_tree_copy
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "reachability"), (
            "premise: the index moved the walk"
        )
        assert cached == uncached


def test_a_merge_conflict_on_gitmodules_resolved_by_git_add_reads_as_git(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow that reaches it: a merge conflicts on .gitmodules, the user
    writes a resolution that sets ignore = all into the working tree, a
    search runs (git reads no .gitmodules while the conflict stands), and
    `git add .gitmodules` then resolves it (git reads the working tree's).
    Only the index moves between the two searches."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "config", "-f", ".gitmodules", "submodule.sub.branch", "side")
    _git(repo, "commit", "-q", "-am", "side gitmodules", when=_day(31))
    _git(repo, "checkout", "-q", "main")
    _git(repo, "config", "-f", ".gitmodules", "submodule.sub.branch", "trunk")
    _git(repo, "commit", "-q", "-am", "main gitmodules", when=_day(32))
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    merged = subprocess.run(
        ["git", "merge", "side"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert _git(repo, "ls-files", "-u", "--", ".gitmodules"), (
        f"premise: a conflict: {merged.stdout} {merged.stderr}"
    )
    # git reads a backslash in a config value as an escape and refuses the
    # file at an unknown one (a Windows path's \U), so the URL is written
    # with forward slashes.
    (repo / ".gitmodules").write_text(
        '[submodule "sub"]\n\tpath = sub\n'
        f"\turl = {(tmp_path / 'source').as_posix()}\n"
        "\tbranch = trunk\n\tignore = all\n",
        encoding="utf-8",
    )
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (2, "reachability")
    working_tree_copy = githead.stamp(repo / ".gitmodules")

    _git(repo, "add", ".gitmodules")
    assert githead.stamp(repo / ".gitmodules") == working_tree_copy
    cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
    assert _drift(uncached, memory_id) == (0, "reachability"), (
        "premise: the resolution moved the walk"
    )
    assert cached == uncached


def _edit_and_write_index(repo: Path, write: str, n: int) -> None:
    """A write to the index that leaves .gitmodules as it was: `git add` of
    an edited file, or `git status` refreshing the index after a file was
    rewritten with its own bytes and a new mtime."""
    target = repo / "src" / "app.py"
    if write == "add":
        target.write_text(_MODULE + f"\n# edit {n}\n", encoding="utf-8")
        _git(repo, "add", "src/app.py")
    else:
        target.write_bytes(target.read_bytes())
        ahead = time.time() + 10 * (n + 1)
        os.utime(target, (ahead, ahead))
        _git(repo, "status", "--porcelain")


@files_only
@pytest.mark.parametrize("write", ["add", "refresh"])
def test_an_index_write_that_leaves_gitmodules_alone_keeps_the_memo(
    write: str,
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a repository with no .gitmodules anywhere, git reads HEAD's copy
    for the walk, which the key's head names, so a write to the index that
    adds no .gitmodules entry changes nothing git reads. The attach after it
    is answered by the memo, forks nothing and answers as the cold one, and
    the walk memo gains no entry."""
    repo, caller, memories, memory_id = _root_in_range(
        tmp_path, memory_dir, monkeypatch, claim=False
    )
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (1, "reachability")
    walks = len(origin._WALK_MEMO)
    index = repo / ".git" / "index"
    for n in range(3):
        stamped = githead.stamp(index)
        _edit_and_write_index(repo, write, n)
        assert githead.stamp(index) != stamped, "premise: git wrote the index"
        warm, calls = _attach(memories, caller, monkeypatch)
        assert calls == [], "the memo answers"
        assert warm == before
    assert len(origin._WALK_MEMO) == walks
    _caches.clear_all()
    cold, _ = _attach(memories, caller, monkeypatch)
    assert cold == before


@files_only
def test_the_walk_files_carry_the_indexs_gitmodules_entry_and_not_its_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the signature holds for the index is what git reads there: the
    stage-0 .gitmodules entry (its mode and object) while the working tree
    has none, and whether the entry is unmerged whatever the working tree
    holds. A write to the index that leaves the entry as it was leaves the
    signature as it was. GIT_INDEX_FILE names an index these reads do not
    open, so nothing is keyed while it is set."""
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    root = repo.resolve()

    absent = origin.walk_files_signature(root)
    assert absent is not None
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    assert origin.walk_files_signature(root) == absent, "no .gitmodules entry"

    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n', encoding="utf-8")
    _git(repo, "add", ".gitmodules")
    (repo / ".gitmodules").unlink()
    staged = origin.walk_files_signature(root)
    assert staged is not None and staged != absent
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    assert origin.walk_files_signature(root) == staged, "the entry is unchanged"

    _git(repo, "checkout", "-q", "--", ".gitmodules")
    present = origin.walk_files_signature(root)
    assert present is not None and present != staged
    _unmerge(repo)
    unmerged = origin.walk_files_signature(root)
    assert unmerged is not None and unmerged != present

    monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git" / "index"))
    assert origin.walk_files_signature(root) is None


@files_only
@pytest.mark.parametrize("version", [2, 3, 4])
def test_the_index_reader_finds_gitmodules_as_git_lists_it(
    version: int, tmp_path: Path
) -> None:
    """The index is read in-process up to its .gitmodules entry, in each
    format git writes: version 2, version 3 with the extended flags a
    skip-worktree or intent-to-add entry needs, and version 4's
    prefix-compressed names; with names of 4,095 bytes and more (whose
    length the entry's flags do not hold) and dotted names that sort
    before it."""
    repo = _repo(tmp_path)
    _commit(
        repo,
        "c0",
        when=_T0,
        files={
            ".a": "a\n",
            ".gitattributes": "* text\n",
            ".github/workflows/ci.yml": "ci\n",
            "zz.txt": "z\n",
        },
    )
    blob = _git(repo, "rev-parse", "HEAD:.a")
    long_name = ".a-dir/" + "/".join(["n" * 200] * 24) + "/long.txt"
    assert len(long_name.encode()) > 4095
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{long_name}")
    _git(repo, "update-index", f"--index-version={version}")
    if version == 3:
        _git(repo, "update-index", "--skip-worktree", ".a")
    gd = githead.gitdir_at(repo.resolve())
    assert gd is not None
    assert origin._index_gitmodules(gd) is None

    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n', encoding="utf-8")
    _git(repo, "add", ".gitmodules")
    if version == 3:
        _git(repo, "update-index", "--skip-worktree", ".gitmodules")
    listed = _git(repo, "ls-files", "-s", "--", ".gitmodules").split()
    assert origin._index_gitmodules(gd) == (int(listed[0], 8), listed[1])
    _unmerge(repo)
    assert origin._index_gitmodules(gd) == origin._INDEX_UNMERGED


@files_only
@pytest.mark.parametrize("version", [2, 3, 4])
def test_the_index_reader_reads_on_past_its_first_read(
    version: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader reads the index's first bytes and reads further, four
    times as far each time, while the scan has not reached .gitmodules; so
    an index whose entries before .gitmodules outrun the first read is read
    as git lists it. The first read is shrunk to 64 bytes here, and 3,000
    dotted names sort before .gitmodules."""
    monkeypatch.setattr(origin, "_INDEX_FIRST_READ", 64)
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    blob = _git(repo, "rev-parse", "HEAD:a.txt")
    _git_input(
        repo,
        "".join(f"100644 {blob}\t.a/{n:05d}.txt\n" for n in range(3000)),
        "update-index",
        "--add",
        "--index-info",
    )
    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n', encoding="utf-8")
    _git(repo, "add", ".gitmodules")
    _git(repo, "update-index", f"--index-version={version}")
    if version == 3:
        _git(repo, "update-index", "--skip-worktree", ".gitmodules")
    gd = githead.gitdir_at(repo.resolve())
    assert gd is not None
    assert (repo / ".git" / "index").stat().st_size > 64 * 4**3
    listed = _git(repo, "ls-files", "-s", "--", ".gitmodules").split()
    assert origin._index_gitmodules(gd) == (int(listed[0], 8), listed[1])


@files_only
def test_the_index_reader_reads_an_intent_to_add_entry(tmp_path: Path) -> None:
    """`git add -N` leaves an entry with the intent-to-add flag and the
    empty blob, the object git then reads as the index's .gitmodules."""
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n', encoding="utf-8")
    _git(repo, "add", "-N", ".gitmodules")
    gd = githead.gitdir_at(repo.resolve())
    assert gd is not None
    listed = _git(repo, "ls-files", "-s", "--", ".gitmodules").split()
    assert origin._index_gitmodules(gd) == (int(listed[0], 8), listed[1])


@files_only
@pytest.mark.parametrize("shape", ["split index", "unknown version", "sha256"])
def test_an_index_the_reader_cannot_settle_keys_nothing(
    shape: str, tmp_path: Path
) -> None:
    """A split index keeps entries in a shared index the reader does not
    open, an index of a version it does not know may lay entries out
    otherwise, and a SHA-256 repository's entries are wider: the walk files
    decline, and what would key on them is not memoised."""
    repo = _repo(tmp_path)
    _commit(repo, "c0", when=_T0, files={"a.txt": "a\n"})
    root = repo.resolve()
    assert origin.walk_files_signature(root) is not None
    if shape == "split index":
        _git(repo, "config", "core.splitIndex", "true")
        _git(repo, "update-index", "--split-index")
    elif shape == "unknown version":
        index = repo / ".git" / "index"
        data = bytearray(index.read_bytes())
        data[4:8] = (5).to_bytes(4, "big")
        index.write_bytes(bytes(data))
    else:
        _git(repo, "config", "extensions.objectFormat", "sha256")
    assert origin.walk_files_signature(root) is None


@files_only
def test_a_split_index_answers_as_git(
    git_config_home: Path,
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a split index the walk files decline, so a change to the
    index's .gitmodules, which may live in the shared index, is read by
    every search as git reads it."""
    repo, caller, memories, memory_id = _pinned_submodule(
        tmp_path, memory_dir, monkeypatch
    )
    _git(repo, "config", "core.splitIndex", "true")
    _git(repo, "update-index", "--split-index")
    ignore = ("config", "-f", ".gitmodules", "submodule.sub.ignore", "all")

    def stage_ignore() -> None:
        _git(repo, "checkout", "-q", "--", ".gitmodules")
        _git(repo, *ignore)
        _git(repo, "add", ".gitmodules")
        (repo / ".gitmodules").unlink()

    def reset_index() -> None:
        _git(repo, "reset", "-q", "--", ".gitmodules")

    stage_ignore()
    for _ in range(2):
        before, _ = _attach(memories, caller, monkeypatch)
    assert _drift(before, memory_id) == (0, "reachability")
    for change, expected in ((reset_index, 2), (stage_ignore, 0)):
        change()
        cached, uncached = _cached_and_uncached(memories, caller, monkeypatch)
        assert _drift(uncached, memory_id) == (expected, "reachability")
        assert cached == uncached


def test_a_walk_whose_files_cannot_be_read_is_kept_for_the_pass_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where githead declines (here GIT_DIR names the git directory), the
    stamps a walk is keyed on cannot be read, so no memo keeps the walk
    past the call. A pass over many memories keeps it in its own mapping,
    so hits at one anchor fork one walk per pass."""
    repo = _repo(tmp_path)
    anchor = _commit(repo, "a", when=_T0, files={"a.txt": "a\n"})
    head = _commit(repo, "b", when=_T1, files={"b.txt": "b\n"})
    root = repo.resolve()
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    assert origin.walk_files_signature(root) is None
    calls: list[tuple[str, ...]] = []
    real_git = origin._git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git", spy)
    first = origin.commits_since_anchor(repo, anchor, toplevel=root, head=head)
    second = origin.commits_since_anchor(repo, anchor, toplevel=root, head=head)
    assert first is not None and first == second
    assert len(_walks(calls)) == 2
    assert len(origin._WALK_MEMO) == 0

    walked: dict[tuple[str, str, str], Any] = {}
    for _ in range(3):
        walk = origin.commits_since_anchor(
            repo, anchor, toplevel=root, head=head, walked=walked
        )
        assert walk == first
    assert len(_walks(calls)) == 3, "one walk for the pass"
    assert len(origin._WALK_MEMO) == 0


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
    so the other thread inserts at once. The files the walk is keyed on
    are stubbed: neither root is a repository."""
    monkeypatch.setattr(origin, "_WALK_MEMO_CAP", 1)
    files = ("stub",)
    monkeypatch.setattr(
        origin, "walk_files_signature", lambda root, directories=None: files
    )
    anchor = "a" * 40
    touched, evicting = Path("/touched"), Path("/evicting")
    first = origin.commits_since_anchor(touched, anchor, toplevel=touched, head=anchor)
    assert first is not None
    memo = _Interleaved(
        origin._WALK_MEMO,
        (str(touched), anchor, anchor, files),
        lambda: origin.commits_since_anchor(
            evicting, anchor, toplevel=evicting, head=anchor
        ),
    )
    monkeypatch.setattr(origin, "_WALK_MEMO", memo)
    walk = origin.commits_since_anchor(touched, anchor, toplevel=touched, head=anchor)
    assert walk is first
    memo.others[0].join()
    assert memo.errors == []
    assert list(memo) == [(str(evicting), anchor, anchor, files)]


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
