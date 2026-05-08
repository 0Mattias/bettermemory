"""Unit tests for origin.py — working-context capture and repo matching."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory.origin import Origin, capture, repos_match


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
