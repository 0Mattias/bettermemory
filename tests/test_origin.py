"""Unit tests for origin.py — working-context capture and repo matching."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.origin import (
    Origin,
    capture,
    commit_author_timestamps,
    commits_since,
    commits_since_touching_paths,
    repos_match,
    should_include_for_caller,
    worktrees_match,
)


# ---------------------------------------------------------------------------
# capture() — non-git directory
# ---------------------------------------------------------------------------


def test_capture_in_non_repo_directory(tmp_path: Path) -> None:
    """A directory that isn't inside any git repo gets cwd populated and
    repo/branch null."""
    origin = capture(cwd=tmp_path)
    assert origin.cwd == str(tmp_path.resolve())
    assert origin.repo is None
    assert origin.branch is None


# ---------------------------------------------------------------------------
# capture() — real git repo (skipped if git not on PATH)
# ---------------------------------------------------------------------------


_GIT_AVAILABLE = shutil.which("git") is not None


def _init_repo(path: Path, *, remote: str | None = None) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if remote is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=path,
            check=True,
            capture_output=True,
        )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_in_repo_with_origin_remote(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote="git@github.com:example/repo.git")
    origin = capture(cwd=tmp_path)
    assert origin.repo == "git@github.com:example/repo.git"
    assert origin.branch == "main"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_in_repo_without_remote(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    origin = capture(cwd=tmp_path)
    # `git config --get remote.origin.url` exits non-zero with no remote.
    assert origin.repo is None
    # We don't bother fetching branch when there's no repo origin — the
    # auto-scope filter doesn't need it without a repo URL to compare.
    assert origin.branch is None


# ---------------------------------------------------------------------------
# repos_match()
# ---------------------------------------------------------------------------


def test_repos_match_same_url_string() -> None:
    url = "git@github.com:example/repo.git"
    assert repos_match(url, url)


def test_repos_match_https_vs_ssh() -> None:
    """SSH and HTTPS forms of the same repo describe one project."""
    assert repos_match(
        "git@github.com:example/repo.git",
        "https://github.com/example/repo.git",
    )
    # And without the .git suffix.
    assert repos_match(
        "git@github.com:example/repo.git",
        "https://github.com/example/repo",
    )


def test_repos_match_case_insensitive_owner() -> None:
    """GitHub treats org names case-insensitively in URLs."""
    assert repos_match(
        "git@github.com:Example/repo.git",
        "git@github.com:example/repo.git",
    )


def test_repos_match_different_repos() -> None:
    assert not repos_match(
        "git@github.com:example/repo-a.git",
        "git@github.com:example/repo-b.git",
    )


def test_repos_match_different_owners() -> None:
    assert not repos_match(
        "git@github.com:alice/repo.git",
        "git@github.com:bob/repo.git",
    )


def test_repos_match_different_hosts() -> None:
    assert not repos_match(
        "git@github.com:example/repo.git",
        "git@gitlab.com:example/repo.git",
    )


def test_repos_match_null_memory_repo_is_global() -> None:
    """A memory with no origin.repo is "global" — it matches any caller."""
    assert repos_match(None, "git@github.com:example/repo.git")


def test_repos_match_null_current_repo_is_unfiltered() -> None:
    """When the caller isn't in a repo, we have no project boundary; treat
    everything as a match."""
    assert repos_match("git@github.com:example/repo.git", None)


def test_repos_match_both_null() -> None:
    assert repos_match(None, None)


def test_repos_match_unparseable_falls_back_to_string_equality() -> None:
    """Opaque URLs that aren't SSH or HTTPS forms compare by raw string —
    we'd rather fail closed (different) than collide two unrelated repos."""
    # "fileserver:project.git" looks SSH-like to the regex (it'll match)
    # so use truly opaque garbage that the SSH regex rejects.
    weird_a = "weird-protocol-a"
    weird_b = "weird-protocol-b"
    assert not repos_match(weird_a, weird_b)
    assert repos_match(weird_a, weird_a)


# ---------------------------------------------------------------------------
# Origin model
# ---------------------------------------------------------------------------


def test_origin_default_is_all_null() -> None:
    o = Origin()
    assert o.cwd is None
    assert o.repo is None
    assert o.branch is None


def test_origin_serializes_to_dict() -> None:
    o = Origin(
        cwd="/tmp/x",
        repo="git@github.com:example/repo.git",
        branch="main",
    )
    payload = o.model_dump(mode="json", exclude_none=True)
    assert payload == {
        "cwd": "/tmp/x",
        "repo": "git@github.com:example/repo.git",
        "branch": "main",
    }


def test_origin_excludes_none_fields_when_serialized() -> None:
    o = Origin(cwd="/tmp/x")
    payload = o.model_dump(mode="json", exclude_none=True)
    assert payload == {"cwd": "/tmp/x"}


# ---------------------------------------------------------------------------
# commits_since() / commit_author_timestamps() — git plumbing
# ---------------------------------------------------------------------------
#
# Both shell out to git. Tests use real temp repos with controlled author
# timestamps via GIT_AUTHOR_DATE / GIT_COMMITTER_DATE so the assertions
# are deterministic. Skipped on machines without git on PATH — same
# pattern as `capture()` tests above.


def _make_commit(
    cwd: Path,
    message: str,
    *,
    when: datetime,
) -> None:
    """Create an empty commit at the given timestamp.

    Both author and committer dates are pinned so `git log` output is
    deterministic regardless of when the test ran. The author identity
    is also pinned to avoid relying on the test machine's git config —
    some CI environments don't have user.name set globally.
    """
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
        cwd=cwd,
        check=True,
        capture_output=True,
        env=env,
    )


def test_commits_since_returns_none_for_none_cwd() -> None:
    assert commits_since(None, datetime(2026, 1, 1, tzinfo=timezone.utc)) is None


def test_commits_since_returns_none_outside_repo(tmp_path: Path) -> None:
    """A directory with no `.git` is not a repo — count is None, not 0."""
    assert commits_since(tmp_path, datetime(2026, 1, 1, tzinfo=timezone.utc)) is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_zero_when_no_commits_after_anchor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _make_commit(tmp_path, "first", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Anchor strictly after the only commit.
    assert commits_since(tmp_path, datetime(2026, 1, 2, tzinfo=timezone.utc)) == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_counts_commits_after_anchor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _make_commit(tmp_path, "old", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new1", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new2", when=datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert commits_since(tmp_path, datetime(2026, 1, 15, tzinfo=timezone.utc)) == 2


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_naive_datetime_treated_as_utc(tmp_path: Path) -> None:
    """A `datetime` without tzinfo is normalised to UTC — same convention
    used by `compute_verification_status` and the rest of the store."""
    _init_repo(tmp_path)
    _make_commit(tmp_path, "old", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    naive_anchor = datetime(2026, 1, 15)  # no tzinfo
    assert commits_since(tmp_path, naive_anchor) == 1


def test_commit_author_timestamps_returns_none_for_none_cwd() -> None:
    assert commit_author_timestamps(None) is None


def test_commit_author_timestamps_returns_none_outside_repo(tmp_path: Path) -> None:
    assert commit_author_timestamps(tmp_path) is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_author_timestamps_returns_all_commits(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _make_commit(tmp_path, "a", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "b", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "c", when=datetime(2026, 3, 1, tzinfo=timezone.utc))
    timestamps = commit_author_timestamps(tmp_path)
    assert timestamps is not None
    assert len(timestamps) == 3
    # Every entry must be timezone-aware so downstream comparisons don't
    # raise on naive-vs-aware mixing.
    assert all(t.tzinfo is not None for t in timestamps)


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_author_timestamps_returns_none_on_empty_repo(tmp_path: Path) -> None:
    """A freshly-init'd repo with no commits — `git log HEAD` exits
    non-zero and we surface that as None, not an empty list. The empty-list
    branch in the function exists for the "git ran but every line failed
    to parse" edge case that's hard to provoke in real life."""
    _init_repo(tmp_path)
    assert commit_author_timestamps(tmp_path) is None


# ---------------------------------------------------------------------------
# commits_since_touching_paths — path-filtered count for verified_claims
# ---------------------------------------------------------------------------


def _commit_file(
    cwd: Path,
    relpath: str,
    *,
    content: str,
    when: datetime,
) -> None:
    """Create or modify a file and commit it at the given timestamp."""
    target = cwd / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(
        ["git", "add", relpath],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", f"touch {relpath}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=env,
    )


def test_commits_since_touching_paths_returns_none_for_none_cwd() -> None:
    assert (
        commits_since_touching_paths(
            None,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            ["/tmp/x"],
        )
        is None
    )


def test_commits_since_touching_paths_returns_none_for_empty_paths(
    tmp_path: Path,
) -> None:
    """No paths means no useful filter — the caller falls back to the
    unfiltered count via the verify.py wrapper."""
    assert (
        commits_since_touching_paths(
            tmp_path,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            [],
        )
        is None
    )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_touching_paths_zero_when_unrelated_files_changed(
    tmp_path: Path,
) -> None:
    """A path-filter that targets a file no commit has touched returns 0,
    even when other files have moved."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "other.txt",
        content="initial",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "other.txt",
        content="changed",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    target_path = str(tmp_path / "tracked.txt")
    out = commits_since_touching_paths(
        tmp_path,
        datetime(2026, 1, 15, tzinfo=timezone.utc),
        [target_path],
    )
    assert out == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_touching_paths_counts_relevant_commits(
    tmp_path: Path,
) -> None:
    """Commits that touched the named path get counted; others don't."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="initial",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "other.txt",
        content="initial",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="updated",
        when=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    out = commits_since_touching_paths(
        tmp_path,
        datetime(2026, 1, 15, tzinfo=timezone.utc),
        [str(tmp_path / "tracked.txt")],
    )
    assert out == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_touching_paths_drops_paths_outside_repo(
    tmp_path: Path,
) -> None:
    """A path that resolves outside the repo can't be filtered on; the
    function returns None so the caller falls back to the unfiltered
    count rather than under-reporting."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="x",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # Path outside the repo.
    out = commits_since_touching_paths(
        tmp_path,
        datetime(2025, 12, 1, tzinfo=timezone.utc),
        ["/nonexistent/outside-repo.txt"],
    )
    assert out is None


# ---------------------------------------------------------------------------
# Worktree isolation — `worktree_root` capture and the secondary filter
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_populates_worktree_root_in_repo(tmp_path: Path) -> None:
    """Inside a primary checkout, `worktree_root` matches the repo's
    own root — `git rev-parse --show-toplevel`. Pin so the additive
    field actually shows up at write time, not just in tests that
    construct Origin by hand."""
    _init_repo(tmp_path, remote="git@github.com:example/repo.git")
    origin = capture(cwd=tmp_path)
    assert origin.worktree_root == str(tmp_path.resolve())


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_worktree_null_outside_repo(tmp_path: Path) -> None:
    """Without a repo there's no worktree to capture — keeps the field
    null instead of falling back to cwd, so the auto-scope filter's
    "both sides set → strict-equal" gate stays a no-op for non-repo
    callers."""
    origin = capture(cwd=tmp_path)
    assert origin.worktree_root is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_distinguishes_two_worktrees_of_one_repo(
    tmp_path: Path,
) -> None:
    """The headline of the audit-flagged worktree-leakage scenario:
    two `git worktree add` checkouts of one repo share `repo` but
    have *different* `worktree_root` paths, which is what lets the
    secondary filter tell them apart."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _init_repo(primary, remote="git@github.com:example/repo.git")
    # Need at least one commit before `git worktree add` will work.
    (primary / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "README.md"],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    secondary = tmp_path / "secondary"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature-x", str(secondary)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    primary_origin = capture(cwd=primary)
    secondary_origin = capture(cwd=secondary)
    # Same repo URL on both sides.
    assert primary_origin.repo == secondary_origin.repo
    # But distinct worktree roots — the discriminator the filter rides on.
    assert primary_origin.worktree_root != secondary_origin.worktree_root
    assert primary_origin.worktree_root == str(primary.resolve())
    assert secondary_origin.worktree_root == str(secondary.resolve())


def test_worktrees_match_null_either_side_is_global() -> None:
    """Legacy memory (no `worktree_root`) or caller outside any
    repo — either case has no boundary to enforce, so the filter
    falls back to repo-only matching."""
    assert worktrees_match(None, "/some/worktree")
    assert worktrees_match("/some/worktree", None)
    assert worktrees_match(None, None)


def test_worktrees_match_equal_paths() -> None:
    assert worktrees_match("/a/b/c", "/a/b/c")


def test_worktrees_match_different_paths() -> None:
    assert not worktrees_match("/repo/main", "/repo/feature-x")


def test_should_include_filters_cross_worktree_same_repo() -> None:
    """The integration: same repo, different worktree → exclude.
    Without the worktree layer this returned True (auditable as
    repo-only matching), letting feature-branch memories leak into
    the bug-fix worktree."""
    memory_origin = Origin(
        cwd="/Users/me/repo-feature/src/foo.py",
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root="/Users/me/repo-feature",
    )
    assert not should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        caller_worktree_root="/Users/me/repo-bugfix",
    )


def test_should_include_passes_same_worktree() -> None:
    """Inverse of the cross-worktree test — same worktree must still
    surface its own memories. Guards against an over-aggressive
    filter that would silently hide everything."""
    memory_origin = Origin(
        repo="git@github.com:example/repo.git",
        worktree_root="/Users/me/repo-feature",
    )
    assert should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        caller_worktree_root="/Users/me/repo-feature",
    )


def test_should_include_legacy_memory_passes_worktree_filter() -> None:
    """A legacy memory with no `worktree_root` field must still
    surface — the new filter is additive and may not silently hide
    writes that predate it."""
    memory_origin = Origin(
        repo="git@github.com:example/repo.git",
        worktree_root=None,  # legacy
    )
    assert should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        caller_worktree_root="/Users/me/repo-feature",
    )


def test_should_include_caller_without_worktree_passes_through() -> None:
    """Caller hasn't captured a worktree (e.g. running outside a git
    checkout, or a search call that didn't pass `caller_worktree_root`).
    The filter falls back to repo-only matching — the `caller_worktree_root`
    default of None preserves pre-audit behaviour."""
    memory_origin = Origin(
        repo="git@github.com:example/repo.git",
        worktree_root="/Users/me/repo-feature",
    )
    assert should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        # caller_worktree_root omitted entirely
    )


def test_should_include_cross_repo_still_filters_first() -> None:
    """Worktree filter is layered *after* the repo check. Cross-repo
    memories must still be excluded, even if they happen to share a
    worktree path with the caller (vanishingly unlikely in practice
    but we lock the ordering anyway)."""
    memory_origin = Origin(
        repo="git@github.com:other/different.git",
        worktree_root="/Users/me/repo-feature",
    )
    assert not should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        caller_worktree_root="/Users/me/repo-feature",
    )
