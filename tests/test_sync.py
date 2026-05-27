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
from bettermemory.doctor import DOCTOR_PROBE_FILENAME
from bettermemory.events import EVENT_LOG_FILENAME
from bettermemory.index import INDEX_FILENAME
from bettermemory.semantic import (
    EMBEDDING_FILENAME_PREFIX,
    EMBEDDING_FILENAME_SUFFIX,
)
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
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    # Redirect git's global config to a per-test tmp file. Without
    # this, the `git config --global user.{email,name}` calls below
    # silently overwrite the developer's ~/.gitconfig on every local
    # run. `GIT_CONFIG_GLOBAL` is git's documented sandbox mechanism
    # (since git 2.32) and is inherited by subprocesses through the
    # process env, so the redirect covers every git invocation any
    # test makes for the duration of the test.
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
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


def test_init_uses_atomic_write_for_gitignore(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .gitignore writer must route through `atomic_write_bytes` so
    a power loss / process kill mid-write can't leave a truncated
    gitignore. A truncated gitignore lets the next `sync push` commit
    event logs / lockfiles / index sqlite to the remote — exactly the
    privacy regression the canonical gitignore is there to prevent.
    Pre-3.1.0 this was a plain `gitignore.write_text(...)`."""
    from bettermemory import _fsutil

    calls: list[tuple[Path, bytes]] = []
    real = _fsutil.atomic_write_bytes

    def spy(path: Path, data: bytes, *, mode: int | None = None) -> None:
        calls.append((path, data))
        real(path, data, mode=mode)

    # The `sync.py` module imports `atomic_write_bytes` by name (from
    # `._fsutil import atomic_write_bytes`), so the spy must replace
    # the name in `sync`'s module namespace — patching `_fsutil` itself
    # wouldn't shadow the local binding.
    monkeypatch.setattr(sync, "atomic_write_bytes", spy, raising=False)
    result = sync.init(memory_dir)
    assert result["already_repo"] is False
    assert len(calls) == 1, (
        f"expected exactly one atomic_write_bytes call for the "
        f".gitignore; got {len(calls)}. A regression to "
        f"`gitignore.write_text(...)` would surface as zero calls."
    )
    path, data = calls[0]
    assert path == memory_dir / ".gitignore"
    assert b".index.sqlite" in data
    assert b".events.jsonl" in data


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


def test_status_porcelain_parses_modified_path_cleanly(
    memory_dir: Path,
) -> None:
    """Porcelain output is `XY␣path`. For modified-not-staged files
    the X char is a space (` M filename`); a partition on the first
    space would drop the status char into the path and store the
    modified file as `"M filename"`. Lock in that the path is parsed
    cleanly so the CLI shows the actual filename in `sync status`."""
    sync.init(memory_dir)
    store = Store(memory_dir)
    memory = store.write(content="original body", scopes=["tools"])

    # First commit so the file is tracked.
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-m", "seed")

    # Modify in place — the resulting status code is " M" (space, M).
    path = store._find_path_for_id(memory.id)
    assert path is not None
    path.write_text(path.read_text() + "\nappended line\n", encoding="utf-8")

    st = sync.status(memory_dir)
    assert st.is_repo is True
    assert len(st.modified) == 1
    parsed = st.modified[0]
    # The path must NOT carry a leading "M " from the status code.
    assert not parsed.startswith("M ")
    assert parsed.endswith(".md")


def test_status_redacts_credentialed_remote_url(memory_dir: Path) -> None:
    """A remote URL with embedded credentials must not surface in
    SyncStatus.remote_url. The credential lives in git config (where
    it belongs); CLI output / `--json` payloads should not echo it."""
    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    sync.init(memory_dir, remote=secret_url)
    st = sync.status(memory_dir)
    assert st.remote_url is not None
    assert "ghp_topsecret" not in st.remote_url
    assert "alice" not in st.remote_url
    assert "github.com/example/repo.git" in st.remote_url


def test_redact_url_helper_handles_common_shapes() -> None:
    """Direct unit coverage on _redact_url for the cases the SyncStatus
    test doesn't exercise: ssh URLs, anonymous HTTPS, malformed input."""
    from bettermemory.sync import _redact_url

    # SSH URL — `git@host:path` has no scheme; the `git@` is a username
    # not a token, leave it alone.
    assert _redact_url("git@github.com:foo/bar.git") == "git@github.com:foo/bar.git"
    # Anonymous HTTPS — nothing to redact.
    assert (
        _redact_url("https://github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    # Token-bearing HTTPS — strip the userinfo.
    assert (
        _redact_url("https://x-access-token:abc123@github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    # None passthrough.
    assert _redact_url(None) is None


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


def test_push_redacts_credentialed_url_in_error(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a failed `git push` echoes the full remote URL into
    stderr, including the userinfo segment for HTTPS-token auth. The
    SyncError must redact that before raising — otherwise the
    credential lands in CLI output and any log capture downstream.

    The original `_run_git` already redacts via the default check=True
    path; sync.push uses check=False (it wants to attach the
    "rebase --continue" hint), so the redact wrapper has to be applied
    in its own SyncError construction — which the fix added."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])

    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    fake_stderr = f"fatal: unable to access '{secret_url}': connection refused"
    original_run_git = sync._run_git

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "push":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr=fake_stderr,
            )
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.push(memory_dir)
    error_text = str(excinfo.value)
    assert "ghp_topsecret" not in error_text, (
        f"token leaked into SyncError: {error_text}"
    )
    assert "alice" not in error_text, f"username leaked into SyncError: {error_text}"
    # And confirm the redaction marker shows up — useful to debug the
    # error without exposing what was hidden.
    assert "<redacted>" in error_text or "redacted" in error_text


def test_pull_redacts_credentialed_url_in_error(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric to the push redaction test: a failed `git pull --rebase`
    echoes the credentialed URL into stderr, and the SyncError must
    redact it. The pull path attaches a conflict-resolution hint, so
    like push it builds its own SyncError with the raw text — the
    redact wrapper had to be applied there too."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    secret_url = "https://alice:ghp_topsecret@github.com/example/repo.git"
    fake_stderr = f"fatal: unable to access '{secret_url}': connection refused"
    original_run_git = sync._run_git

    def fake_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "pull":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr=fake_stderr,
            )
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", fake_run_git)

    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(memory_dir)
    error_text = str(excinfo.value)
    assert "ghp_topsecret" not in error_text, (
        f"token leaked into SyncError: {error_text}"
    )
    assert "alice" not in error_text, f"username leaked into SyncError: {error_text}"


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


# ---------------------------------------------------------------------------
# Name validation (F-S2 / F-S3) and pull --no-tags (L6)
# ---------------------------------------------------------------------------


def test_init_rejects_unsafe_default_branch(memory_dir: Path) -> None:
    """`default_branch` is positional to `git init --initial-branch`.
    A value starting with `-` (or containing shell-meaningful chars)
    could in some git versions get parsed as a flag rather than a
    branch name. The validator rejects anything outside the conservative
    safe set."""
    with pytest.raises(sync.SyncError, match="default_branch"):
        sync.init(memory_dir, default_branch="--exec=evil")


def test_push_rejects_unsafe_remote_name(memory_dir: Path, bare_remote: Path) -> None:
    """Same rule on the `remote` arg passed to push: validate against
    the safe charset before letting it through to `git push <remote>`."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    with pytest.raises(sync.SyncError, match="remote"):
        sync.push(memory_dir, remote="--exec=evil")


def test_pull_rejects_unsafe_remote_name(memory_dir: Path) -> None:
    """Same rule on the `remote` arg passed to pull."""
    sync.init(memory_dir)
    with pytest.raises(sync.SyncError, match="remote"):
        sync.pull(memory_dir, remote="--upload-pack=evil")


def test_pull_uses_no_tags(
    memory_dir: Path,
    bare_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `git pull --rebase` must include `--no-tags`. A
    hostile or sloppy remote pushing refs under `refs/tags/` would
    otherwise be silently mirrored into the local `.git/refs/tags/`,
    where a tag named `main` could shadow the branch."""
    sync.init(memory_dir, remote=str(bare_remote))
    Store(memory_dir).write(content="x", scopes=["tools"])
    sync.push(memory_dir)

    captured_args: list[list[str]] = []
    original_run_git = sync._run_git

    def capturing_run_git(
        root: Path, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        captured_args.append(list(args))
        return original_run_git(root, args, check=check)

    monkeypatch.setattr(sync, "_run_git", capturing_run_git)
    sync.pull(memory_dir, reindex=False)

    pull_calls = [a for a in captured_args if a and a[0] == "pull"]
    assert pull_calls, f"no pull subcommand in captured args: {captured_args}"
    assert "--no-tags" in pull_calls[0], (
        f"`git pull` missing --no-tags: {pull_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Filename-constant cross-module parity (Class 6 — closed by this commit).
#
# `_GITIGNORE_LINES` in `sync.py` enumerates the regenerable / transient
# filenames the runtime writes alongside the canonical markdown store —
# the FTS5 index (`.index.sqlite` + its `-shm` / `-wal` sidecars), the
# event log (`.events.jsonl` + rotated `.events.jsonl.*.gz`), the
# embedding cache (`.embeddings.<safe>.npz`), and the doctor probe
# (`.doctor-probe`). Each filename is also defined as a canonical
# constant in the module that *writes* the file:
#
#   - `events.py:EVENT_LOG_FILENAME`            → `.events.jsonl`
#   - `index.py:INDEX_FILENAME`                 → `.index.sqlite`
#   - `semantic.py:EMBEDDING_FILENAME_PREFIX`   → `.embeddings.`
#     `semantic.py:EMBEDDING_FILENAME_SUFFIX`   → `.npz`
#   - `doctor.py:DOCTOR_PROBE_FILENAME`         → `.doctor-probe`
#
# Hazard: a future rename of any canonical filename constant updates
# the writer but, prior to this commit, would leave the sync gitignore
# referring to a stale literal — silently checking the newly-named
# regenerable file into the user's sync repo. Class 6 in the
# tick-23 Branch B audit.
#
# Fix shape: `sync.py` now IMPORTS the constants instead of hardcoding
# the literals; this guard asserts the constant values actually appear
# in `_GITIGNORE_LINES`, so a future contributor who imports the
# constant but accidentally drops it from the list still trips the
# pin.
#
# Negative-controls verified at commit time (see commit message for
# detail).
# ---------------------------------------------------------------------------


def test_gitignore_lines_include_canonical_filename_constants() -> None:
    """`_GITIGNORE_LINES` (`sync.py`) MUST include the canonical
    filename constants from the modules that write those files.
    Renaming a constant updates the writer but, without this guard,
    silently leaves the gitignore referring to a stale literal — and
    the now-tracked regenerable file lands in the user's sync repo.

    Closes Class 6 (filename-constant cross-module parity) from the
    tick-23 Branch B audit."""
    assert INDEX_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing INDEX_FILENAME "
        f"({INDEX_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    assert EVENT_LOG_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing EVENT_LOG_FILENAME "
        f"({EVENT_LOG_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    assert DOCTOR_PROBE_FILENAME in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing DOCTOR_PROBE_FILENAME "
        f"({DOCTOR_PROBE_FILENAME!r}); see sync.py:_GITIGNORE_LINES"
    )
    # Embedding cache is a glob; assert it was built from the lifted
    # prefix/suffix constants. A rename of either half without
    # updating the sync glob would strand the rebuilt cache in git.
    embedding_glob = f"{EMBEDDING_FILENAME_PREFIX}*{EMBEDDING_FILENAME_SUFFIX}"
    assert embedding_glob in sync._GITIGNORE_LINES, (
        f"sync._GITIGNORE_LINES missing embedding-cache glob "
        f"({embedding_glob!r}) built from "
        f"semantic.EMBEDDING_FILENAME_PREFIX / SUFFIX"
    )
