"""Tests for the origin cache in origin.py.

`capture` answers a second call for the same resolved directory from the
first while the first is younger than `ORIGIN_CACHE_SECONDS` and the
directory's `githead.signature` reads as it did when the first call began.
The contract is exactness: what the cache answers is what the uncached
capture returns for the same directory, field for field, and only the time
changes.

Every test builds real repositories with git under ``tmp_path``. The clock
the cache reads (`time.monotonic`) stands still for each test and moves only
where a test moves it, so no assertion depends on how long git takes.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, githead, identity, origin
from bettermemory.origin import Origin, capture

from .conftest import set_git_discovery_ceiling

_GIT_AVAILABLE = shutil.which("git") is not None

pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the signature settles only on POSIX; elsewhere nothing is cached",
)

_REMOTE = "git@github.com:example/repo.git"

# The four probes a capture runs in a checkout whose remote is `origin`.
_PROBES = [
    ("rev-parse", "--show-toplevel"),
    ("remote", "get-url", "origin"),
    ("config", "--get-all", "remote.origin.url"),
    ("symbolic-ref", "--short", "HEAD"),
]


class _Clock:
    """A monotonic clock that stands still until a test moves it. It starts
    at a whole number of seconds, so sums and differences on it are exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's git environment away from the repositories the
    tests build: no inherited discovery override, a discovery ceiling above
    ``tmp_path``, no global or system configuration on POSIX, a fixed
    identity for commits, and no declared workspace."""
    for name in (*githead._DISCOVERY_ENV, "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    if os.name == "posix":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
    monkeypatch.delenv(identity.ENV_WORKSPACE, raising=False)
    identity._CURRENT.set(None)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    stopped = _Clock()
    monkeypatch.setattr(time, "monotonic", stopped)
    return stopped


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """The arguments of every git process `origin` runs, in order."""
    calls: list[tuple[str, ...]] = []
    real = origin._git_result

    def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git_result", spy)
    return calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(path: Path, *, remote: str | None = _REMOTE) -> Path:
    path.mkdir(parents=True)
    _run(path, "init", "--quiet", "--initial-branch=main")
    if remote is not None:
        _run(path, "remote", "add", "origin", remote)
    return path


def _commit(path: Path) -> None:
    _run(path, "commit", "--quiet", "--allow-empty", "--message", "commit")


def _key(directory: Path) -> str:
    return str(directory.resolve())


def _same(got: Origin, want: Origin) -> None:
    """`got` is `want` field for field: the dump, the fields set at
    construction, both private attributes, and equality."""
    assert type(got) is Origin and type(want) is Origin
    assert got.model_dump() == want.model_dump()
    assert got.model_fields_set == want.model_fields_set
    assert got.__pydantic_private__ == want.__pydantic_private__
    assert got == want


def _uncached(directory: Path, **kwargs: Any) -> Origin:
    """What `capture` returns for `directory` with no cache in the way. The
    cache is left as it was."""
    saved = dict(origin._ORIGIN_CACHE)
    origin._ORIGIN_CACHE.clear()
    try:
        return capture(directory, **kwargs)
    finally:
        origin._ORIGIN_CACHE.clear()
        origin._ORIGIN_CACHE.update(saved)


# ---------------------------------------------------------------------------
# Hits and the lifetime
# ---------------------------------------------------------------------------


def test_the_lifetime_is_ten_minutes() -> None:
    assert origin.ORIGIN_CACHE_SECONDS == 600.0


@posix_only
def test_a_capture_after_a_lone_hooks_idle_gap_runs_no_git(
    tmp_path: Path, probes: list[tuple[str, ...]], clock: _Clock
) -> None:
    """A session-start hook fires alone, seconds or minutes after the
    capture before it: 2.2 s is the idle gap bench/daemon/latency.py
    measures a lone call after, and ten minutes less a millisecond the
    last instant the capture answers. Both run no git and answer as the
    uncached capture; at ten minutes the probes run again."""
    repo = _repo(tmp_path / "repo")
    began = clock.now
    first = capture(repo)
    for gap in (2.2, origin.ORIGIN_CACHE_SECONDS - 0.001):
        probes.clear()
        clock.now = began + gap
        got = capture(repo)
        assert probes == [], gap
        _same(got, first)
        _same(got, _uncached(repo))
    probes.clear()
    clock.now = began + origin.ORIGIN_CACHE_SECONDS
    _same(capture(repo), first)
    assert probes == _PROBES


@posix_only
def test_a_lone_session_start_block_after_idle_reuses_the_capture(
    tmp_path: Path, probes: list[tuple[str, ...]], clock: _Clock
) -> None:
    """The daemon's session-start endpoint builds its block with
    `hook.session_start_block`, which captures the caller's origin. Two
    session starts 2.2 s apart, the shape of a session-start firing alone,
    run the four probes once, and read the same block."""
    from bettermemory import hook
    from bettermemory.store import Store

    repo = _repo(tmp_path / "repo")
    with Store(tmp_path / "store") as store:
        store.write(
            content="the release checklist lives in docs",
            scopes=["tools"],
            origin=capture(repo),
        )
        _caches.clear_all()
        probes.clear()
        first = hook.session_start_block(store, cwd=repo)
        assert probes == _PROBES
        assert first[0] is not None
        probes.clear()
        clock.advance(2.2)
        assert hook.session_start_block(store, cwd=repo) == first
        assert probes == []


@posix_only
def test_a_second_capture_inside_the_lifetime_runs_no_git(
    tmp_path: Path, probes: list[tuple[str, ...]], clock: _Clock
) -> None:
    repo = _repo(tmp_path / "repo")
    first = capture(repo)
    assert probes == _PROBES
    assert first.repo == _REMOTE
    assert first.branch == "main"
    assert first.worktree_root == _key(repo)
    probes.clear()
    clock.advance(origin.ORIGIN_CACHE_SECONDS - 0.001)
    second = capture(repo)
    assert probes == []
    _same(second, first)
    _same(second, _uncached(repo))


@posix_only
def test_after_the_lifetime_the_four_probes_run_again(
    tmp_path: Path, probes: list[tuple[str, ...]], clock: _Clock
) -> None:
    repo = _repo(tmp_path / "repo")
    first = capture(repo)
    probes.clear()
    clock.advance(origin.ORIGIN_CACHE_SECONDS)
    second = capture(repo)
    assert probes == _PROBES
    _same(second, first)
    # The new capture starts a lifetime of its own.
    probes.clear()
    clock.advance(origin.ORIGIN_CACHE_SECONDS - 0.001)
    _same(capture(repo), first)
    assert probes == []


@posix_only
def test_a_hit_carries_the_callers_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, ...]]
) -> None:
    """The directory is resolved as before, from the declaration or the
    process cwd, and only then looked up; the label on the answer is the
    one this call's channel gives, never the one the cached call had."""
    repo = _repo(tmp_path / "repo")
    assert capture(repo).source is None
    probes.clear()
    labelled = capture(repo, source="caller")
    assert probes == []
    assert labelled.source == "caller"
    _same(labelled, _uncached(repo, source="caller"))
    probes.clear()
    monkeypatch.setenv(identity.ENV_WORKSPACE, str(repo))
    declared = capture()
    assert probes == []
    assert declared.source == identity.SOURCE_ENV
    assert declared.cwd == _key(repo)
    monkeypatch.delenv(identity.ENV_WORKSPACE)
    monkeypatch.chdir(repo)
    here = capture()
    assert probes == []
    assert here.source == identity.SOURCE_PROCESS_CWD
    _same(here, _uncached(repo, source=identity.SOURCE_PROCESS_CWD))


# ---------------------------------------------------------------------------
# What the signature sees is never answered from the cache
# ---------------------------------------------------------------------------


@posix_only
def test_a_new_branch_inside_the_lifetime_is_seen_by_the_next_capture(
    tmp_path: Path, probes: list[tuple[str, ...]]
) -> None:
    repo = _repo(tmp_path / "repo")
    assert capture(repo).branch == "main"
    _run(repo, "checkout", "--quiet", "-b", "other")
    probes.clear()
    after = capture(repo)
    assert probes == _PROBES
    assert after.branch == "other"
    _same(after, _uncached(repo))


@posix_only
def test_a_detached_head_inside_the_lifetime_is_seen_by_the_next_capture(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    assert capture(repo).branch == "main"
    _run(repo, "checkout", "--quiet", "--detach")
    after = capture(repo)
    assert after.branch is None
    assert after.worktree_root == _key(repo)
    _same(after, _uncached(repo))


@posix_only
def test_a_remote_set_url_inside_the_lifetime_is_seen_by_the_next_capture(
    tmp_path: Path, probes: list[tuple[str, ...]]
) -> None:
    repo = _repo(tmp_path / "repo")
    assert capture(repo).repo == _REMOTE
    renamed = "https://github.com/example/renamed.git"
    _run(repo, "remote", "set-url", "origin", renamed)
    probes.clear()
    after = capture(repo)
    assert probes == _PROBES
    assert after.repo == renamed
    _same(after, _uncached(repo))


@posix_only
def test_a_repository_created_or_removed_inside_the_lifetime_is_seen(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plain = capture(project)
    assert plain.worktree_root is None and plain.repo is None
    assert plain.git_indeterminate is False
    # Git said "not a repository", an answer, and the answer is cached.
    assert _key(project) in origin._ORIGIN_CACHE
    _run(project, "init", "--quiet", "--initial-branch=main")
    created = capture(project)
    assert created.worktree_root == _key(project)
    assert created.branch == "main"
    _same(created, _uncached(project))
    shutil.rmtree(project / ".git")
    removed = capture(project)
    assert removed.worktree_root is None and removed.branch is None
    _same(removed, plain)


@posix_only
def test_a_changed_ceiling_inside_the_lifetime_is_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, ...]]
) -> None:
    """GIT_CEILING_DIRECTORIES is part of the signature, so a changed value
    asks git again even where the answer comes out the same."""
    repo = _repo(tmp_path / "repo")
    first = capture(repo)
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        os.environ["GIT_CEILING_DIRECTORIES"] + os.pathsep + str(tmp_path),
    )
    probes.clear()
    _same(capture(repo), first)
    assert probes == _PROBES


@posix_only
def test_core_worktree_in_config_worktree_inside_the_lifetime_is_seen(
    tmp_path: Path,
) -> None:
    """Under extensions.worktreeConfig, git reads core.worktree from
    config.worktree in the git directory, which moves the root the capture
    records; its stamp is in the capture's signature."""
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    _run(repo, "config", "extensions.worktreeConfig", "true")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert capture(repo).worktree_root == _key(repo)
    (repo / ".git" / "config.worktree").write_text(
        f"[core]\n\tworktree = {elsewhere}\n", encoding="utf-8"
    )
    after = capture(repo)
    uncached = _uncached(repo)
    assert uncached.worktree_root == _key(elsewhere), "premise: git moved the root"
    _same(after, uncached)


def _ambiguous(repo: Path, change: str) -> None:
    """Make the branch's short name ambiguous to `git symbolic-ref --short
    HEAD`, which then prints `heads/<branch>` (or the full name), with no
    file the signature stamped before touched."""
    head = _run(repo, "rev-parse", "HEAD")
    if change == "refs/<b>":
        _run(repo, "update-ref", "refs/main", head)
    elif change == "tag":
        _run(repo, "tag", "main")
    elif change == "<gitdir>/<b>":
        (repo / ".git" / "main").write_text(head + "\n", encoding="utf-8")
    elif change == "heads/<b> behind refs/<b>":
        _run(repo, "update-ref", "refs/main", head)
        assert _uncached(repo).branch == "heads/main"
        (repo / ".git" / "heads").mkdir()
        (repo / ".git" / "heads" / "main").write_text(head + "\n", encoding="utf-8")
    else:
        raise AssertionError(change)


@posix_only
@pytest.mark.parametrize(
    "change", ["refs/<b>", "tag", "<gitdir>/<b>", "heads/<b> behind refs/<b>"]
)
def test_a_ref_that_changes_the_branchs_short_name_is_seen(
    change: str, tmp_path: Path
) -> None:
    """`git symbolic-ref --short HEAD` shortens refs/heads/<b> to <b> only
    while no ref it tries first resolves the same short name: <b> itself (a
    file in the git directory), refs/<b> and refs/tags/<b>, then heads/<b>
    for the next shortening. The capture's signature stamps each ref file
    that decides the name, so a ref created there inside the lifetime is
    read by the next capture."""
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    before = capture(repo)
    assert before.branch == "main"
    _ambiguous(repo, change)
    after = capture(repo)
    uncached = _uncached(repo)
    assert uncached.branch != "main", "premise: git shortens the name otherwise"
    _same(after, uncached)


@posix_only
def test_a_stash_on_a_branch_named_stash_is_seen(tmp_path: Path) -> None:
    """On a branch named stash, `git stash` writes refs/stash, and the
    branch's short name turns to heads/stash."""
    repo = _repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _commit(repo)
    _run(repo, "checkout", "--quiet", "-b", "stash")
    assert capture(repo).branch == "stash"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    _run(repo, "stash", "--quiet")
    after = capture(repo)
    uncached = _uncached(repo)
    assert uncached.branch == "heads/stash", "premise: git shortens it so"
    _same(after, uncached)


def _rename_only_in_spelling(
    tmp_path: Path, shape: str
) -> tuple[Path, Path, Path] | None:
    """A repository whose directory, or one above it, can be renamed so
    that only the case or the Unicode normalisation of its name changes:
    ``(the repository as first named, the path to rename, its new name)``,
    or None where the filesystem holds one spelling per name and the old
    name would stop resolving."""
    import unicodedata

    if shape == "normalisation":
        old = unicodedata.normalize("NFC", "café")
        new = unicodedata.normalize("NFD", "café")
        repo = _repo(tmp_path / old)
        if not (tmp_path / new).exists():
            return None
        return repo, tmp_path / old, tmp_path / new
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        return None
    if shape == "repository":
        repo = _repo(tmp_path / "Repo")
        return repo, tmp_path / "Repo", tmp_path / "repo"
    repo = _repo(tmp_path / "Projects" / "r")
    return repo, tmp_path / "Projects", tmp_path / "projects"


@pytest.mark.skipif(
    sys.platform != "darwin", reason="the kernel's spelling is read on macOS"
)
@pytest.mark.parametrize("gap", [0.0, 2.5])
@pytest.mark.parametrize("shape", ["repository", "ancestor", "normalisation"])
def test_a_rename_that_changes_only_the_spelling_is_seen(
    shape: str, gap: float, tmp_path: Path, clock: _Clock
) -> None:
    """On a filesystem that ignores case or Unicode normalisation (macOS's
    default), a rename of the repository's directory, or of one above it,
    that changes only the spelling of its name leaves the caller's old
    spelling resolving to the same files, while `git rev-parse
    --show-toplevel` prints the new one. The signature carries the kernel's
    spelling of the root (`_spelled_as_git`), so the next capture, at once
    or after the idle gap of a lone hook, runs the probes and records the
    root git prints."""
    made = _rename_only_in_spelling(tmp_path, shape)
    if made is None:
        pytest.skip("the filesystem holds one spelling per name")
    repo, old, new = made
    _commit(repo)
    first = capture(repo)
    assert first.worktree_root == _key(repo)
    os.rename(old, new)
    clock.advance(gap)
    after = capture(repo)
    uncached = _uncached(repo)
    assert uncached.worktree_root != first.worktree_root, (
        "premise: git prints the new spelling"
    )
    _same(after, uncached)


@posix_only
def test_the_signature_carries_the_kernels_spelling_of_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture's signature holds the root spelled as git prints it
    (`_spelled_as_git`: the kernel's path on macOS, the resolved path
    elsewhere), and equals no other signature where that spelling cannot
    be read, so such a directory's capture is never kept."""
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    signature = origin._capture_signature(repo)
    assert signature == origin._capture_signature(repo)
    spelled = origin._spelled_as_git(repo.resolve())
    assert spelled is not None
    extra = signature[-1]
    assert isinstance(extra, tuple) and str(spelled) in extra
    monkeypatch.setattr(origin, "_spelled_as_git", lambda root: None)
    unreadable = origin._capture_signature(repo)
    assert unreadable != origin._capture_signature(repo)
    capture(repo)
    assert _key(repo) not in origin._ORIGIN_CACHE


@posix_only
@pytest.mark.parametrize("moved", [600.0, -1.0], ids=["forward", "back"])
def test_the_lifetime_runs_on_the_wall_clock_too(
    moved: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probes: list[tuple[str, ...]],
    clock: _Clock,
) -> None:
    """The monotonic clock does not run while the machine sleeps, so a
    capture could outlive ten minutes of wall time by any length of sleep.
    It expires when either clock says the lifetime has passed, and when the
    wall clock reads before the capture began."""
    wall = [5_000_000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    repo = _repo(tmp_path / "repo")
    first = capture(repo)
    probes.clear()
    clock.advance(1.0)
    wall[0] += moved
    _same(capture(repo), first)
    assert probes == _PROBES


@posix_only
def test_the_latency_benchs_cold_call_makes_the_capture_stale_and_only_that(
    tmp_path: Path, probes: list[tuple[str, ...]]
) -> None:
    """bench/daemon/latency.py's hook_service_cold moves the ctime of the
    repository's HEAD before each call: HEAD's bytes and mtime are as they
    were, so git reads the repository as before, and the next capture runs
    the four probes and answers as the uncached one."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "latency_bench",
        Path(__file__).resolve().parents[1] / "bench" / "daemon" / "latency.py",
    )
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    repo = _repo(tmp_path / "repo")
    _commit(repo)
    first = capture(repo)
    head = repo / ".git" / "HEAD"
    before = os.stat(head)
    content = head.read_bytes()
    probes.clear()
    bench._make_capture_stale(repo)
    after = os.stat(head)
    assert head.read_bytes() == content
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ctime_ns != before.st_ctime_ns
    _same(capture(repo), first)
    assert probes == _PROBES


def test_where_the_signature_cannot_settle_every_capture_runs_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, ...]]
) -> None:
    """Off POSIX, and wherever the files do not settle what git would find,
    the signature is a marker equal to no other, so nothing is kept and
    each capture asks git."""
    monkeypatch.setattr(githead, "_POSIX", False)
    repo = _repo(tmp_path / "repo")
    first = capture(repo)
    second = capture(repo)
    assert probes == _PROBES + _PROBES
    assert not origin._ORIGIN_CACHE
    _same(second, first)


@posix_only
def test_a_change_while_the_probes_run_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature is read before the probes and again after them, and
    the capture is kept only when the two agree. A branch switched after
    the first probe and switched back once the capture returned leaves the
    signature as it was before the probes; kept, the capture would answer
    the next call with the branch that no longer holds."""
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    real = origin._git_result
    switched = False

    def switch_after_the_first_probe(cwd: Path, *args: str, **kwargs: Any) -> Any:
        nonlocal switched
        result = real(cwd, *args, **kwargs)
        if not switched:
            switched = True
            _run(repo, "checkout", "--quiet", "-b", "moved")
        return result

    monkeypatch.setattr(origin, "_git_result", switch_after_the_first_probe)
    during = capture(repo)
    assert during.branch == "moved"
    assert _key(repo) not in origin._ORIGIN_CACHE
    _run(repo, "checkout", "--quiet", "main")
    after = capture(repo)
    assert after.branch == "main"
    _same(after, _uncached(repo))


@posix_only
@pytest.mark.parametrize("move", ["symbolic-ref", "checkout"])
def test_a_head_that_leaves_and_returns_while_the_probes_run_is_not_cached(
    move: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD pointed at another branch as the branch probe starts and back
    once it returns: HEAD's bytes before and after the probes are the
    same, while the probe read the other branch. Git rewrites HEAD through
    a lock file and a rename, which leaves it with a new inode and ctime,
    so the signature before the probes differs from the one after them
    and the capture is not kept."""
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    _run(repo, "branch", "side")
    real_branch = origin._git_branch

    def point(branch: str) -> None:
        if move == "symbolic-ref":
            _run(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        else:
            _run(repo, "checkout", "--quiet", branch)

    def read_away_then_return(cwd: Path) -> str | None:
        point("side")
        try:
            return real_branch(cwd)
        finally:
            point("main")

    monkeypatch.setattr(origin, "_git_branch", read_away_then_return)
    during = capture(repo)
    monkeypatch.setattr(origin, "_git_branch", real_branch)
    assert during.branch == "side", "premise: the probe read the other branch"
    assert _run(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _key(repo) not in origin._ORIGIN_CACHE
    after = capture(repo)
    assert after.branch == "main"
    _same(after, _uncached(repo))


# ---------------------------------------------------------------------------
# What is not cached
# ---------------------------------------------------------------------------


def test_a_capture_git_could_not_run_for_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    real = origin._git_result
    available = False
    calls: list[tuple[str, ...]] = []

    def unavailable_until_told(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real(cwd, *args, **kwargs) if available else None

    monkeypatch.setattr(origin, "_git_result", unavailable_until_told)
    unknown = capture(repo)
    assert unknown.git_indeterminate is True
    assert unknown.worktree_root is None and unknown.repo is None
    assert _key(repo) not in origin._ORIGIN_CACHE
    available = True
    calls.clear()
    answered = capture(repo)
    assert calls == _PROBES
    assert answered.git_indeterminate is False
    assert answered.repo == _REMOTE and answered.worktree_root == _key(repo)


@posix_only
def test_a_capture_whose_later_probe_could_not_run_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first probe answered, so the origin is not indeterminate, but
    the branch probe timed out: the capture is returned as before and not
    kept, since the next call may get git's answer."""
    repo = _repo(tmp_path / "repo")
    real_run = subprocess.run

    def branch_probe_times_out(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv[:2] == ["git", "symbolic-ref"]:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1.0))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", branch_probe_times_out)
    cut_short = capture(repo)
    assert cut_short.worktree_root == _key(repo) and cut_short.repo == _REMOTE
    assert cut_short.branch is None
    assert cut_short.git_indeterminate is False
    assert _key(repo) not in origin._ORIGIN_CACHE
    monkeypatch.setattr(subprocess, "run", real_run)
    answered = capture(repo)
    assert answered.branch == "main"
    assert _key(repo) in origin._ORIGIN_CACHE


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@posix_only
def test_two_directories_are_two_entries(
    tmp_path: Path, probes: list[tuple[str, ...]]
) -> None:
    other_remote = "https://github.com/example/other.git"
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second", remote=other_remote)
    inside = first / "src"
    inside.mkdir()
    assert capture(first).repo == _REMOTE
    assert capture(second).repo == other_remote
    below = capture(inside)
    assert below.cwd == _key(inside) and below.worktree_root == _key(first)
    assert list(origin._ORIGIN_CACHE) == [_key(first), _key(second), _key(inside)]
    probes.clear()
    assert capture(first).repo == _REMOTE
    assert capture(second).repo == other_remote
    assert capture(inside).cwd == _key(inside)
    assert probes == []


@posix_only
def test_alternates_are_registered_on_a_hit_as_on_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, ...]]
) -> None:
    """The spellings `git config --get-all` reported for the remote reach
    `_CALLER_REPO_ALTERNATES` on every capture, cached or not, with the
    same effect on the registry: here the registry is full, and the hit
    evicts what the uncached capture evicts."""
    mirror = "git@gitlab.com:example/repo-mirror.git"
    repo = _repo(tmp_path / "repo")
    _run(repo, "remote", "set-url", "--add", "origin", mirror)
    registry: dict[str, tuple[str, ...]] = {}
    monkeypatch.setattr(origin, "_CALLER_REPO_ALTERNATES", registry)
    first = capture(repo)
    assert first._repo_url_alternates == (mirror,)
    assert registry == {_REMOTE: (mirror,)}
    filled = {
        f"https://example.com/{n}.git": ()
        for n in range(origin._CALLER_REPO_ALTERNATES_CAP)
    }
    registry.clear()
    registry.update(filled)
    probes.clear()
    hit = capture(repo)
    assert probes == []
    assert hit._repo_url_alternates == (mirror,)
    after_hit = list(registry.items())
    assert (_REMOTE, (mirror,)) in after_hit
    registry.clear()
    registry.update(filled)
    _same(hit, _uncached(repo))
    assert list(registry.items()) == after_hit


@posix_only
def test_a_hit_returns_a_fresh_origin_the_caller_cannot_poison(
    tmp_path: Path,
) -> None:
    mirror = "git@gitlab.com:example/repo-mirror.git"
    repo = _repo(tmp_path / "repo")
    _run(repo, "remote", "set-url", "--add", "origin", mirror)
    first = capture(repo)
    entry = origin._ORIGIN_CACHE[_key(repo)]
    assert first is not entry.origin
    second = capture(repo)
    assert second is not entry.origin and second is not first
    for returned in (first, second):
        returned._git_indeterminate = True
        returned._repo_url_alternates = ("poison",)
        returned.repo = "poison"
        returned.branch = "poison"
        returned.worktree_root = "poison"
        returned.cwd = "poison"
    third = capture(repo)
    assert third is not entry.origin
    assert third.repo == _REMOTE and third.branch == "main"
    assert third._repo_url_alternates == (mirror,)
    assert third.git_indeterminate is False
    _same(third, _uncached(repo))


@posix_only
def test_the_bound_evicts_the_oldest_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, ...]]
) -> None:
    monkeypatch.setattr(origin, "_ORIGIN_CACHE_CAP", 2)
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for directory in (a, b, c):
        directory.mkdir()
        capture(directory)
    assert list(origin._ORIGIN_CACHE) == [_key(b), _key(c)]
    probes.clear()
    capture(b)
    assert probes == []
    capture(a)
    assert probes == [("rev-parse", "--show-toplevel")]
    assert list(origin._ORIGIN_CACHE) == [_key(c), _key(a)]


@posix_only
def test_the_cache_holds_at_most_256_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_a_repository(cwd: Path, *args: str, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", "fatal: not a git repository\n"
        )

    monkeypatch.setattr(origin, "_git_result", not_a_repository)
    directories = []
    for n in range(257):
        directory = tmp_path / f"d{n:03d}"
        directory.mkdir()
        capture(directory)
        directories.append(directory)
    assert list(origin._ORIGIN_CACHE) == [_key(d) for d in directories[1:]]


@posix_only
def test_clear_all_empties_the_origin_cache(tmp_path: Path) -> None:
    capture(_repo(tmp_path / "repo"))
    assert origin._ORIGIN_CACHE
    _caches.clear_all()
    assert not origin._ORIGIN_CACHE


# ---------------------------------------------------------------------------
# A sequence of changes
# ---------------------------------------------------------------------------


@posix_only
def test_seeded_sequence_matches_the_uncached_capture(
    tmp_path: Path, probes: list[tuple[str, ...]]
) -> None:
    """Branch, remote and repository changes in a fixed pseudo-random
    order, in a primary checkout, a linked worktree of it and a directory
    inside the primary that becomes a repository of its own and stops
    being one. After every step each directory is captured: a capture that
    ran no git equals the uncached capture of the same directory, one that
    ran the probes is the uncached capture, and after a step that changes
    nothing no capture runs git."""
    rng = random.Random(20260926)
    primary = _repo(tmp_path / "primary")
    _commit(primary)
    linked = tmp_path / "linked"
    _run(primary, "worktree", "add", "--quiet", "-b", "side", str(linked))
    nested = primary / "vendor" / "lib"
    nested.mkdir(parents=True)
    directories = [primary, linked, nested]
    operations = [
        "status",
        "status",
        "commit",
        "branch",
        "switch",
        "detach",
        "set-url",
        "mirror",
        "remove",
        "rename",
        "nest",
        "nest",
    ]
    rng.shuffle(operations)
    counter = 0

    def url() -> str:
        # A new length on every call, so the config's size moves even where
        # the filesystem's clock is coarse.
        return f"https://example.com/{'r' * counter}.git"

    def step(op: str) -> bool:
        """Run `op`; False when it changes nothing a capture reads."""
        nonlocal counter
        counter += 1
        where = rng.choice([primary, linked])
        remotes = _run(where, "remote").split()
        if op == "status":
            _run(where, "status", "--porcelain")
            return False
        if op == "commit":
            _commit(where)
        elif op == "branch":
            _run(where, "checkout", "--quiet", "-b", f"b{counter}")
        elif op == "switch":
            branches = _run(
                where, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/"
            ).split()
            _run(
                where,
                "checkout",
                "--quiet",
                "--ignore-other-worktrees",
                rng.choice(branches),
            )
        elif op == "detach":
            _run(where, "checkout", "--quiet", "--detach")
        elif op == "nest":
            if (nested / ".git").exists():
                shutil.rmtree(nested / ".git")
            else:
                _run(nested, "init", "--quiet", "--initial-branch=trunk")
        elif "origin" not in remotes:
            _run(where, "remote", "add", "origin", url())
        elif op == "set-url":
            # The first URL by name, so a mirror added earlier stays.
            first = _run(where, "remote", "get-url", "origin")
            _run(where, "remote", "set-url", "origin", url(), f"^{re.escape(first)}$")
        elif op == "mirror":
            _run(where, "remote", "set-url", "--add", "origin", url())
        elif op == "remove":
            _run(where, "remote", "remove", "origin")
        elif op == "rename":
            _run(where, "remote", "rename", "origin", f"remote{counter}")
        return True

    for directory in directories:
        capture(directory)
    hits_after_a_change = 0
    for op in operations:
        changed = step(op)
        for directory in directories:
            probes.clear()
            got = capture(directory)
            if probes:
                assert changed, f"{op} changed nothing, yet {directory} ran git"
                continue
            hits_after_a_change += changed
            _same(got, _uncached(directory))
    # Some captures after a change were answered from the cache, so the
    # comparison above covered the stale answers a signature could miss.
    assert hits_after_a_change > 0
