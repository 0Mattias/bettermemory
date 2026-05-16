"""Tests for the git-based sync wrapper (T4.1 of the v1.6 plan).

Tests use a local bare repository as the "remote" so push and pull
exercise real git transport without needing network access. Each
test sets up its own pair of stores via tmp_path so the suite stays
hermetic.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import sync
from bettermemory.store import Store


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH — sync tests need git installed",
)


def _git(cwd: Path, *args: str) -> str:
    """Helper for ad-hoc git in tests. Raises with stderr on failure."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    # Identity so commits work in CI.
    _git(d.parent, "config", "--global", "user.email", "test@example.com")
    _git(d.parent, "config", "--global", "user.name", "Test")
    return d


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A local bare repo to act as the sync target. Same disk, no
    network. Initialise with `main` as the default branch so it
    matches `sync.init`'s default."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_repo_and_gitignore(memory_dir: Path) -> None:
    """First-time init: creates a git repo on the default branch and
    writes the canonical .gitignore. Idempotent on repeat — the
    test below covers that case."""
    result = sync.init(memory_dir)
    assert (memory_dir / ".git").is_dir()
    assert (memory_dir / ".gitignore").exists()
    ignore = (memory_dir / ".gitignore").read_text()
    assert ".index.sqlite" in ignore
    assert ".events.jsonl" in ignore
    assert result["already_repo"] is False


def test_init_is_idempotent(memory_dir: Path) -> None:
    """Running init twice should not fail or duplicate state. The
    second run reports already_repo=True; the .gitignore is
    refreshed in-place rather than re-appended."""
    sync.init(memory_dir)
    result = sync.init(memory_dir)
    assert result["already_repo"] is True
    # .gitignore should not be duplicated.
    ignore = (memory_dir / ".gitignore").read_text()
    assert ignore.count(".index.sqlite\n") == 1


def test_init_sets_remote(memory_dir: Path, bare_remote: Path) -> None:
    """When `--remote` is passed, init adds (or updates) the origin
    remote. The actual URL ends up in `git remote get-url origin`."""
    sync.init(memory_dir, remote=str(bare_remote))
    url = _git(memory_dir, "remote", "get-url", "origin").strip()
    assert url == str(bare_remote)


def test_init_updates_remote_on_second_run(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """A second init with a different --remote replaces the URL
    rather than failing. Users iterating on remote setup shouldn't
    have to remember whether they've initialised yet."""
    other = tmp_path / "other.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(other)],
        check=True,
        capture_output=True,
    )
    sync.init(memory_dir, remote=str(other))
    sync.init(memory_dir, remote=str(bare_remote))
    url = _git(memory_dir, "remote", "get-url", "origin").strip()
    assert url == str(bare_remote)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_on_non_repo_returns_is_repo_false(tmp_path: Path) -> None:
    """A plain directory should report is_repo=False rather than
    raise. Lets the CLI render a helpful "run init first" message."""
    plain = tmp_path / "plain"
    plain.mkdir()
    st = sync.status(plain)
    assert st.is_repo is False
    assert st.branch is None
    assert st.remote_url is None


def test_status_on_repo_reports_branch_and_changes(memory_dir: Path) -> None:
    """After init + a write, status should show the current branch
    and a non-empty modified/untracked list."""
    sync.init(memory_dir)
    store = Store(memory_dir)
    store.write(content="test memory", scopes=["tools"])
    st = sync.status(memory_dir)
    assert st.is_repo is True
    assert st.branch == "main"
    assert st.has_changes is True


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_commits_and_pushes(memory_dir: Path, bare_remote: Path) -> None:
    """End-to-end push: init with remote, write a memory, push.
    The committed=True + pushed=True flags must both fire on the
    first push of new content."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="durable fact", scopes=["tools"])

    result = sync.push(memory_dir)
    assert result["committed"] is True
    assert result["pushed"] is True

    # Confirm the commit landed on the bare remote.
    log = _git(bare_remote, "log", "--oneline")
    assert "bettermemory: sync" in log


def test_push_no_op_when_nothing_changed(memory_dir: Path, bare_remote: Path) -> None:
    """A push when there are no local changes should NOT create an
    empty commit. The committed=False signal is how the CLI tells
    the user "nothing to do" without an error."""
    sync.init(memory_dir, remote=str(bare_remote))
    store = Store(memory_dir)
    store.write(content="initial", scopes=["tools"])
    sync.push(memory_dir)

    # Second push with no changes.
    result = sync.push(memory_dir)
    assert result["committed"] is False
    assert result["pushed"] is True


def test_push_errors_without_remote(memory_dir: Path) -> None:
    """A push against a repo with no `origin` should raise SyncError
    with an actionable message. The CLI catches this and renders a
    clean error."""
    sync.init(memory_dir)  # no --remote passed
    Store(memory_dir).write(content="x", scopes=["tools"])
    with pytest.raises(sync.SyncError, match="no remote"):
        sync.push(memory_dir)


def test_push_errors_on_non_repo(tmp_path: Path) -> None:
    """A push against a non-repo directory should raise SyncError
    pointing to `sync init`."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sync.SyncError, match="not a git repo"):
        sync.push(plain)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_rebuilds_index(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """End-to-end pull: write+push from one clone, pull from another,
    verify the FTS5 index was rebuilt to reflect the pulled
    files."""
    # Set up source clone, write a memory, push.
    sync.init(memory_dir, remote=str(bare_remote))
    src_store = Store(memory_dir)
    src_store.write(content="python list comprehension", scopes=["tools"])
    sync.push(memory_dir)

    # Set up a separate clone that pulls.
    other_dir = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other_dir)],
        check=True,
        capture_output=True,
    )

    result = sync.pull(other_dir)
    assert result["pulled"] is True
    assert result["reindexed"] is True
    assert result["indexed_count"] == 1

    # The pulled file should be searchable via the rebuilt index.
    from bettermemory import index as _index

    candidates = _index.query(other_dir, "python")
    assert candidates


def test_pull_no_reindex_flag(
    memory_dir: Path, bare_remote: Path, tmp_path: Path
) -> None:
    """`pull(reindex=False)` skips the rebuild — useful in batched
    sync scripts that defer the rebuild to the end."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    other_dir = tmp_path / "other_clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(other_dir)],
        check=True,
        capture_output=True,
    )

    result = sync.pull(other_dir, reindex=False)
    assert result["pulled"] is True
    assert result["reindexed"] is False
    assert result["indexed_count"] is None


def test_pull_errors_without_remote(memory_dir: Path) -> None:
    """A pull against a repo with no `origin` should raise SyncError."""
    sync.init(memory_dir)
    with pytest.raises(sync.SyncError, match="no remote"):
        sync.pull(memory_dir)


# ---------------------------------------------------------------------------
# auto
# ---------------------------------------------------------------------------


def test_auto_pulls_then_pushes(memory_dir: Path, bare_remote: Path) -> None:
    """`auto` is pull + push in one call. With nothing on the remote
    yet, the pull is effectively a no-op; the push commits and
    sends. Both sub-results should be present in the return."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="hello", scopes=["tools"])

    # Wait — pull with no remote tracking yet would fail. Initialize
    # the remote first by pushing once so HEAD has an upstream.
    sync.push(memory_dir)

    # Add another change, then auto.
    Store(memory_dir).write(content="another fact", scopes=["tools"])
    result = sync.auto(memory_dir)
    assert "pull" in result
    assert "push" in result
    pull = result["pull"]
    push = result["push"]
    assert isinstance(pull, dict)
    assert isinstance(push, dict)
    assert pull["pulled"] is True
    assert push["pushed"] is True
