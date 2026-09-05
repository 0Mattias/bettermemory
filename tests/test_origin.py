"""Unit tests for origin.py — working-context capture and repo matching."""

from __future__ import annotations

import errno
import ast
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.origin import (
    Origin,
    capture,
    commit_author_timestamps,
    commit_author_timestamps_touching_pathspecs,
    commit_patch_stream,
    repos_match,
    resolve_repo_pathspecs,
    should_include_for_caller,
    worktrees_match,
)

from .conftest import set_git_discovery_ceiling


# ---------------------------------------------------------------------------
# capture() — non-git directory
# ---------------------------------------------------------------------------


def test_capture_in_non_repo_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that isn't inside any git repo gets cwd populated and
    repo/branch null. The discovery ceiling keeps the premise honest when
    tmp_path itself sits under a real checkout (poisoned basetemp/TMPDIR)."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    origin = capture(cwd=tmp_path)
    assert origin.cwd == str(tmp_path.resolve())
    assert origin.repo is None
    assert origin.branch is None


def test_capture_when_cwd_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `Path.cwd()` raises FileNotFoundError because the working directory
    was deleted (Stop-hook scenario: user `rm -rf`s their dir before turn
    end), return an all-null Origin instead of propagating. Without this,
    the audit hook leaks the OSError to stderr and Claude Code shows it as
    a turn-end error banner.
    """

    def _boom() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_boom))

    origin = capture()
    assert origin.cwd is None
    assert origin.repo is None
    assert origin.branch is None
    assert origin.worktree_root is None


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
    # `git remote get-url origin` exits non-zero with no remote, and the
    # `git remote` enumeration fallback lists nothing — there's no repo
    # identity to record.
    assert origin.repo is None
    # But it IS a git checkout: the worktree discriminator (and branch)
    # must survive so the worktree filter still applies to local-only
    # repos instead of the whole origin collapsing to null.
    assert origin.worktree_root == str(tmp_path.resolve())
    assert origin.branch == "main"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_in_repo_with_upstream_only_remote(tmp_path: Path) -> None:
    """A checkout whose only remote is 'upstream' (triangular fork
    workflows, `git clone -o`, clone.defaultRemoteName) must still capture
    the repo identity. Before the `git remote` enumeration fallback,
    repo AND worktree_root came back null, so its writes went global and
    a caller searching from it matched every project's memories — the
    false-positive direction the module docstring names the most
    embarrassing failure mode."""
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "upstream", "git@github.com:example/repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    assert origin.repo == "git@github.com:example/repo.git"
    assert origin.worktree_root == str(tmp_path.resolve())
    assert origin.branch == "main"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_in_repo_with_custom_named_remote(tmp_path: Path) -> None:
    """Same as the upstream-only case for an arbitrary remote name."""
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "github", "https://github.com/example/repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    assert origin.repo == "https://github.com/example/repo.git"
    assert origin.worktree_root == str(tmp_path.resolve())


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_push_mirror_keeps_canonical_fetch_url(tmp_path: Path) -> None:
    """`git remote set-url --add origin <mirror>` (the canonical
    two-remote mirroring recipe) makes remote.origin.url multi-valued;
    `git config --get` returns the LAST value (the mirror) while fetch
    uses the FIRST. capture() must record the canonical fetch URL, or the
    day a mirror is added every previously written memory for the repo
    goes invisible."""
    _init_repo(tmp_path, remote="git@github.com:owner/repo.git")
    subprocess.run(
        [
            "git",
            "remote",
            "set-url",
            "--add",
            "origin",
            "git@gitlab.com:owner/repo-mirror.git",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    assert origin.repo == "git@github.com:owner/repo.git"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_capture_expands_insteadof_alias(tmp_path: Path) -> None:
    """url.<base>.insteadOf shorthands must be captured EXPANDED — the URL
    git actually fetches from — not as the raw alias, which _parse_remote
    can't parse and which never matches the canonical spelling from
    another clone."""
    _init_repo(tmp_path, remote="gh:example/repo")
    subprocess.run(
        ["git", "config", "url.git@github.com:.insteadOf", "gh:"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    assert origin.repo == "git@github.com:example/repo"
    assert repos_match(origin.repo, "git@github.com:example/repo.git")
    assert repos_match(origin.repo, "https://github.com/example/repo")


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_old_idiom_multi_url_stored_origin_matches_new_capture(
    tmp_path: Path,
) -> None:
    """Releases through v3.9.0 captured origin.repo via `git config --get
    remote.origin.url`, which returns the LAST value of a multi-valued
    remote (the push mirror). The current idiom (`git remote get-url`)
    returns the FIRST. A store stamped under the old idiom must keep
    matching the new capture, or the repo's existing memories silently
    vanish from auto-scope: capture() collects the remote's other
    official spellings and repos_match recognizes the old stored one."""
    _init_repo(tmp_path, remote="git@github.com:legacyowner/multiurl.git")
    subprocess.run(
        [
            "git",
            "remote",
            "set-url",
            "--add",
            "origin",
            "git@gitlab.com:legacyowner/multiurl-mirror.git",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    # Forward semantics unchanged (the round-84 fix): FIRST URL wins.
    assert origin.repo == "git@github.com:legacyowner/multiurl.git"
    # The alternates ride on the caller-side Origin, never the dump —
    # the on-disk/event payload shape is unchanged.
    assert origin._repo_url_alternates == (
        "git@gitlab.com:legacyowner/multiurl-mirror.git",
    )
    assert "_repo_url_alternates" not in origin.model_dump()
    # Old idiom stored the LAST configured URL; it must match the caller.
    old_stored = "git@gitlab.com:legacyowner/multiurl-mirror.git"
    assert repos_match(old_stored, origin.repo)
    assert should_include_for_caller(Origin(repo=old_stored), origin.repo)
    # Never-widen: an unrelated repo on the mirror host still mismatches.
    assert not repos_match("git@gitlab.com:legacyowner/unrelated.git", origin.repo)


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_old_idiom_insteadof_alias_stored_origin_matches_new_capture(
    tmp_path: Path,
) -> None:
    """The other old/new capture divergence: `config --get` returned the
    RAW insteadOf alias ('gh:owner/repo'), which parses with the alias
    as the host — so raw-equality never engages against the expanded
    capture. The capture-side alternates bridge it."""
    _init_repo(tmp_path, remote="gh:legacyowner/aliasrepo")
    subprocess.run(
        ["git", "config", "url.git@github.com:.insteadOf", "gh:"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    origin = capture(cwd=tmp_path)
    # Forward semantics unchanged: captured EXPANDED.
    assert origin.repo == "git@github.com:legacyowner/aliasrepo"
    assert repos_match("gh:legacyowner/aliasrepo", origin.repo)
    assert should_include_for_caller(
        Origin(repo="gh:legacyowner/aliasrepo"), origin.repo
    )
    # Never-widen: a different repo spelled through the same alias
    # scheme still mismatches.
    assert not repos_match("gh:legacyowner/other", origin.repo)


def test_repos_match_caller_alternates_parameter() -> None:
    """Explicit `caller_alternates` merges extra spellings of the
    CALLER's own remote without consulting the process registry; a
    non-matching memory spelling stays excluded."""
    assert repos_match(
        "gh:paramowner/repo",
        "git@github.com:paramowner/repo",
        caller_alternates=("gh:paramowner/repo",),
    )
    assert not repos_match(
        "gh:paramowner/other",
        "git@github.com:paramowner/repo",
        caller_alternates=("gh:paramowner/repo",),
    )


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
    # Scp-ish colon-path shapes ("fileserver:project.git") DO parse since
    # the userless/single-segment relaxation, so use truly opaque garbage
    # with no colon at all — the SSH regex rejects it and there's no
    # scheme, so _parse_remote returns None on both sides.
    weird_a = "weird-protocol-a"
    weird_b = "weird-protocol-b"
    assert not repos_match(weird_a, weird_b)
    assert repos_match(weird_a, weird_a)


# ---------------------------------------------------------------------------
# repos_match() — remote-shape normalization (2026-06-09 extractor hunt)
# ---------------------------------------------------------------------------


def test_repos_match_azure_devops_ssh_vs_https() -> None:
    """Azure DevOps clone URLs are protocol-asymmetric (SSH carries a
    'v3/' prefix, HTTPS a '_git' segment); both official forms must
    canonicalize to one ('dev.azure.com', org, 'project/repo') triple."""
    assert repos_match(
        "git@ssh.dev.azure.com:v3/contoso/WebApp/WebApp",
        "https://contoso@dev.azure.com/contoso/WebApp/_git/WebApp",
    )


def test_repos_match_azure_devops_legacy_vs_modern() -> None:
    """Old {org}.visualstudio.com clone URLs linger in long-lived
    checkouts; they're the same repositories as the dev.azure.com forms."""
    assert repos_match(
        "https://contoso.visualstudio.com/WebApp/_git/WebApp",
        "https://dev.azure.com/contoso/WebApp/_git/WebApp",
    )
    # DefaultCollection-era URLs and the legacy SSH host too.
    assert repos_match(
        "https://contoso.visualstudio.com/DefaultCollection/WebApp/_git/WebApp",
        "git@vs-ssh.visualstudio.com:v3/contoso/WebApp/WebApp",
    )


def test_repos_match_azure_devops_different_projects_dont_match() -> None:
    """Same org, different project — the canonicalization must not widen
    matching beyond the single repository."""
    assert not repos_match(
        "git@ssh.dev.azure.com:v3/contoso/ProjA/repo",
        "https://dev.azure.com/contoso/ProjB/_git/repo",
    )


def test_repos_match_bitbucket_server_scm_prefix() -> None:
    """Bitbucket Server/DC: '/scm/' in the HTTPS clone URL is a fixed
    routing prefix, not an owner — the SSH form omits it."""
    assert repos_match(
        "ssh://git@bitbucket.example.com:7999/proj/repo.git",
        "https://bitbucket.example.com/scm/proj/repo.git",
    )


def test_repos_match_bitbucket_scm_different_projects_dont_match() -> None:
    assert not repos_match(
        "https://bitbucket.example.com/scm/proj-a/repo.git",
        "https://bitbucket.example.com/scm/proj-b/repo.git",
    )


def test_repos_match_single_char_owner_not_treated_as_route_prefix() -> None:
    """github.com/a/repo has a REAL single-char owner 'a' — the
    route-prefix strip only fires when the remainder still contains '/'."""
    assert repos_match(
        "https://github.com/a/repo.git",
        "git@github.com:a/repo.git",
    )
    assert not repos_match(
        "https://github.com/a/repo.git",
        "https://github.com/b/repo.git",
    )


def test_repos_match_gerrit_authenticated_vs_anonymous_https() -> None:
    """Gerrit prepends '/a/' to authenticated HTTP clone URLs; the
    anonymous URL of the SAME project omits it. Two HTTPS clones differing
    only by whether credentials were configured must match."""
    assert repos_match(
        "https://gerrit.corp.com/a/tools/build",
        "https://gerrit.corp.com/tools/build",
    )


def test_repos_match_nested_namespace_prefix_not_stripped_on_generic_hosts() -> None:
    """A top-level group literally named 'scm' or 'a' on a
    nested-namespace host (GitLab subgroups, self-managed hybrids) is a
    REAL namespace, not a vendor routing prefix — the strip fires only
    when the hostname carries the vendor's name ('bitbucket'/'gerrit').
    Unconditionally stripping merged distinct repositories, violating
    the module's never-widen invariant; vendor instances behind neutral
    hostnames degrade to the tolerated false-negative direction
    instead."""
    assert not repos_match(
        "https://gitlab.com/scm/team/proj.git",
        "https://gitlab.com/team/proj.git",
    )
    assert not repos_match(
        "https://gitlab.com/a/team/proj",
        "https://gitlab.com/team/proj",
    )
    # And the same repo's SSH form (never prefix-stripped) matches its
    # own HTTPS form again on non-vendor hosts.
    assert repos_match(
        "git@gitlab.com:scm/team/proj.git",
        "https://gitlab.com/scm/team/proj.git",
    )


def test_repos_match_ssh_over_443_alias_hosts() -> None:
    """ssh.github.com / altssh.gitlab.com are first-party fallback
    hostnames for the same service (documented port-22-blocked-network
    workarounds), normalized via the fixed alias table."""
    assert repos_match(
        "ssh://git@ssh.github.com:443/example/repo.git",
        "git@github.com:example/repo.git",
    )
    assert repos_match(
        "ssh://git@altssh.gitlab.com:443/example/repo.git",
        "git@gitlab.com:example/repo.git",
    )


def test_repos_match_git_plus_ssh_schemes() -> None:
    """git+ssh:// and ssh+git:// are genuine git SSH-transport aliases —
    they must match every other spelling of the same repo."""
    from bettermemory.origin import _parse_remote

    for alias in (
        "git+ssh://git@github.com/example/repo.git",
        "ssh+git://git@github.com/example/repo.git",
    ):
        assert _parse_remote(alias) == ("github.com", "example", "repo")
        assert repos_match(alias, "git@github.com:example/repo.git")
        assert repos_match(alias, "https://github.com/example/repo")
        assert repos_match(alias, "ssh://git@github.com/example/repo.git")


def test_repos_match_single_segment_paths_normalize() -> None:
    """gitolite / Gerrit-SSH / cgit-root remotes have no owner segment;
    .git-suffix and transport normalization still apply. The empty-owner
    sentinel keeps this collision-free — single-segment only ever matches
    single-segment on the same host."""
    assert repos_match(
        "git@git.example.com:dotfiles.git",
        "git@git.example.com:dotfiles",
    )
    assert repos_match(
        "https://git.zx2c4.com/wireguard-tools",
        "https://git.zx2c4.com/wireguard-tools.git",
    )
    assert repos_match(
        "ssh://mattias@gerrit.example.com:29418/infra-tools",
        "https://gerrit.example.com/infra-tools",
    )
    # Different single-segment repos on the same host stay distinct.
    assert not repos_match(
        "git@git.example.com:dotfiles",
        "git@git.example.com:scripts",
    )


def test_repos_match_userless_scp_forms() -> None:
    """git documents the scp-like user part as optional ('[user@]host:path')
    — the standard ssh-config Host-alias multi-account setup emits exactly
    this form."""
    assert repos_match(
        "gitbox:team/project.git",
        "gitbox:team/project",
    )
    assert repos_match(
        "gitbox:team/project.git",
        "git@gitbox:team/project.git",
    )
    assert repos_match(
        "server.example.com:repos/project.git",
        "ssh://deploy@server.example.com/repos/project.git",
    )
    assert not repos_match(
        "gitbox:team/project.git",
        "gitbox:other/project.git",
    )


def test_repos_match_local_paths_stay_unparseable() -> None:
    """Windows drive paths (single-char 'host') and slash-before-colon
    local paths must NOT parse as scp remotes — they keep the raw-equality
    fallback so the userless relaxation can't swallow non-remote strings."""
    from bettermemory.origin import _parse_remote

    assert _parse_remote("C:/Users/foo/repo") is None
    assert _parse_remote("./local/path:odd") is None
    # Raw equality: the .git variant of a drive path is NOT normalized.
    assert not repos_match("C:/Users/foo/repo", "C:/Users/foo/repo.git")
    assert repos_match("C:/Users/foo/repo", "C:/Users/foo/repo")


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
# commit_author_timestamps() — git plumbing
#
# The committer-date `commits_since` this section also covered was
# deprecated against 4.0, re-targeted twice, and removed in 7.0.0;
# commit_author_timestamps + bisect_right is the author-date source all
# three commit-drift surfaces share.
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


def test_commit_author_timestamps_returns_none_for_none_cwd() -> None:
    assert commit_author_timestamps(None) is None


def test_commit_author_timestamps_returns_none_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_git_discovery_ceiling(tmp_path, monkeypatch)
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
def test_commit_author_timestamps_returns_ascending(tmp_path: Path) -> None:
    """Every caller `bisect_right`s this list, so ascending is a CONTRACT.

    Authored out of chronological order so git's own emit order (newest
    commit first, i.e. Mar, Jan, Feb here) is neither ascending nor a
    reversal of it — a function that forgot to sort cannot pass by luck.
    """
    _init_repo(tmp_path)
    _make_commit(tmp_path, "b", when=datetime(2026, 2, 1, 12, tzinfo=timezone.utc))
    _make_commit(tmp_path, "a", when=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    _make_commit(tmp_path, "c", when=datetime(2026, 3, 1, 12, tzinfo=timezone.utc))
    timestamps = commit_author_timestamps(tmp_path)
    assert timestamps is not None
    assert timestamps == sorted(timestamps), "must be ascending for bisect_right"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_author_timestamps_touching_pathspecs_returns_ascending(
    tmp_path: Path,
) -> None:
    """Same contract for the path-filtered variant.

    `resolve_commit_drift_count` bisects this directly; when only the
    unfiltered source was sorted, this one silently returned git's
    newest-first order and the bisect read against a descending list.

    Needs real file commits — `_make_commit` makes EMPTY ones, which no
    pathspec filter can match.
    """
    _init_repo(tmp_path)
    for i, when in enumerate(
        (
            datetime(2026, 2, 1, 12, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            datetime(2026, 3, 1, 12, tzinfo=timezone.utc),
        )
    ):
        (tmp_path / "f.txt").write_text(f"rev {i}\n")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        _make_commit(tmp_path, f"touch {i}", when=when)
    timestamps = commit_author_timestamps_touching_pathspecs(tmp_path, ["f.txt"])
    assert timestamps is not None and len(timestamps) == 3
    assert timestamps == sorted(timestamps), "must be ascending for bisect_right"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_author_timestamps_returns_none_on_empty_repo(tmp_path: Path) -> None:
    """A freshly-init'd repo with no commits — `git log HEAD` exits
    non-zero and we surface that as None, not an empty list. The empty-list
    branch in the function exists for the "git ran but every line failed
    to parse" edge case that's hard to provoke in real life."""
    _init_repo(tmp_path)
    assert commit_author_timestamps(tmp_path) is None


# ---------------------------------------------------------------------------
# resolve_repo_pathspecs — anchor resolution at the git boundary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_repo_pathspecs_drops_repo_root(tmp_path: Path) -> None:
    """A citation of the repo root itself ("the project lives at X") is a
    location claim, not a content claim — as a pathspec it would be "."
    and match every commit, so it must not survive resolution. All
    spellings of the root collapse to the same drop: absolute, trailing
    native separator (os.sep), and the bare relative "."."""
    _init_repo(tmp_path)
    _make_commit(tmp_path, "first", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    resolved = resolve_repo_pathspecs(
        tmp_path,
        [str(tmp_path), str(tmp_path) + os.sep, "."],
    )
    assert resolved == []


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_repo_pathspecs_keeps_files_alongside_dropped_root(
    tmp_path: Path,
) -> None:
    """The root drop is surgical: discriminating anchors in the same
    input list survive resolution untouched."""
    _init_repo(tmp_path)
    _make_commit(tmp_path, "first", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    resolved = resolve_repo_pathspecs(
        tmp_path,
        [str(tmp_path), "src/mod.py"],
    )
    assert resolved == ["src/mod.py"]


# ---------------------------------------------------------------------------
# _commit_file — a file-touching commit helper for the pathspec tests below.
# The committer-date family it once served (commits_since_touching_paths,
# commits_touching_pathspecs) was deprecated against 4.0, re-targeted twice,
# and removed in 7.0.0; resolve_repo_pathspecs +
# commit_author_timestamps_touching_pathspecs via
# verify.resolve_commit_drift_count is the author-date replacement.
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


# ---------------------------------------------------------------------------
# Deprecation fence — the pyproject.toml filterwarnings gate itself.
#
# The module-scoped filter line ("error::DeprecationWarning:bettermemory")
# keys on the module the warning is ATTRIBUTED to, and our deprecations warn
# with stacklevel=2 — attribution lands on the CALLER's frame. So that line
# only escalates deprecated-API use from bettermemory's own frames; an
# unwrapped deprecated call made from a TEST module is attributed to
# `tests.test_*`, never matches, and sailed through green ("1 passed,
# 1 warning") — the pytest.warns wrapper discipline in this file rested on
# review, not on the gate. The message-scoped twin line escalates by TEXT
# (every bettermemory deprecation message names the package), caller-frame
# agnostic. These tests pin the fence mechanically: the config line literals,
# the regex-vs-emitted-messages match (a reworded warn text can't silently
# slip outside the fence), third-party immunity, and — end to end — a
# subprocess pytest run proving an unwrapped test-frame call ERRORS under
# this exact pyproject config.
# ---------------------------------------------------------------------------

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Literal twins of the two pyproject filterwarnings entries. Kept as literals
# — and asserted verbatim-present in the parsed config below — so the regex
# assertions here can never drift from what pytest actually applies.
_MODULE_FENCE_LINE = "error::DeprecationWarning:bettermemory"
_MESSAGE_FENCE_LINE = (
    "error:.*deprecated and will be removed in bettermemory:DeprecationWarning"
)

# Probe executed by the subprocess fence test: one unwrapped deprecated call
# (must ERROR under the fence) and one pytest.warns-wrapped call (must PASS —
# pytest.warns swallows the warning before the ini filters see it, which is
# exactly the sanctioned idiom the rest of this file uses).
_FENCE_PROBE = '''\
"""Throwaway probe run in a pytest subprocess by the fence test."""

import warnings


import pytest

_MESSAGE = (
    "probe_api is deprecated and will be removed in bettermemory 9.0; "
    "use probe_api_v2"
)


def _deprecated_api() -> None:
    warnings.warn(_MESSAGE, DeprecationWarning, stacklevel=2)


def test_unwrapped_deprecated_call() -> None:
    _deprecated_api()


def test_wrapped_deprecated_call() -> None:
    with pytest.warns(DeprecationWarning, match="probe_api is deprecated"):
        _deprecated_api()
'''


def _pyproject_filterwarnings() -> list[str]:
    with _PYPROJECT.open("rb") as fh:
        filters = tomllib.load(fh)["tool"]["pytest"]["ini_options"]["filterwarnings"]
    assert isinstance(filters, list)
    return filters


def _fence_message_regex() -> re.Pattern[str]:
    """The message field of the fence line, compiled exactly the way the
    warnings machinery will: pytest parses ini filterwarnings entries as
    `action:message:category:module:lineno` with message kept as a RAW regex
    (`parse_warning_filter(..., escape=False)`), and `warnings.warn_explicit`
    applies it via `re.compile(message).match(str(warning_message))`."""
    return re.compile(_MESSAGE_FENCE_LINE.split(":")[1])


def test_deprecation_fence_lines_present_in_pyproject() -> None:
    """Both fence lines, verbatim. The module-scoped line covers deprecated
    calls made FROM bettermemory frames (whatever their message); the
    message-scoped line covers bettermemory-authored deprecation messages
    whatever the calling frame. Removing either reopens a gap, so this pins
    the pair — and anchors the in-process regex tests below to the literal
    pytest actually loads."""
    filters = _pyproject_filterwarnings()
    assert _MODULE_FENCE_LINE in filters
    assert _MESSAGE_FENCE_LINE in filters


def _deprecation_messages_in_source() -> list[tuple[str, str | None]]:
    """`(location, message)` for every `warnings.warn(..., DeprecationWarning)`
    in the package, read statically. The message is the literal text (the
    constant parts of an f-string), or None when it is not a literal at
    all, which the fence test treats as a failure: the fence keys on text,
    so the text has to be visible where the call is made."""
    src = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
    found: list[tuple[str, str | None]] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "warn"
                and isinstance(func.value, ast.Name)
                and func.value.id == "warnings"
            ):
                continue
            categories = list(node.args[1:2]) + [
                kw.value for kw in node.keywords if kw.arg == "category"
            ]
            if not any(
                isinstance(c, ast.Name) and c.id == "DeprecationWarning"
                for c in categories
            ):
                continue
            message = node.args[0] if node.args else None
            text: str | None = None
            if isinstance(message, ast.Constant) and isinstance(message.value, str):
                text = message.value
            elif isinstance(message, ast.JoinedStr):
                text = "".join(
                    v.value
                    for v in message.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            found.append((f"{path.name}:{node.lineno}", text))
    return found


def test_deprecation_fence_covers_every_deprecation_warning_in_source() -> None:
    """Every DeprecationWarning the package emits must fall inside the
    fence. This is the anti-drift pin for the WARN TEXTS: reword a
    deprecation message so it stops saying "deprecated and will be removed
    in bettermemory" and the message-scoped filter silently stops
    escalating unwrapped test-frame calls of it — this test turns that
    silence into a failure. At 7.0.0 nothing is deprecated (the origin.py
    trio the fence was built around left in that release), so the scan
    finds no call today; the next deprecation lands inside the fence or
    fails here."""
    fence = _fence_message_regex()
    for location, text in _deprecation_messages_in_source():
        assert text is not None, f"{location}: the deprecation message is not a literal"
        assert fence.match(text), f"{location}: {text!r} escapes the fence"


def test_deprecation_fence_regex_ignores_third_party_texts() -> None:
    """The discriminator is the package name INSIDE the message — "removed in
    bettermemory" — not the word "deprecated". Representative third-party
    shapes (including one with the full "deprecated and will be removed in"
    prefix but another package's name) must NOT match, so adding the
    message-scoped error line cannot escalate dependencies' deprecation
    warnings and break the suite on an unrelated upgrade."""
    fence = _fence_message_regex()
    third_party_texts = [
        "ham() is deprecated and will be removed in spam 5.0; use eggs()",
        "np.float_ is deprecated and will be removed in NumPy 2.0",
        "datetime.datetime.utcnow() is deprecated and scheduled for removal "
        "in a future version",
        "pkg_resources is deprecated as an API",
        "the imp module is deprecated in favour of importlib",
    ]
    for text in third_party_texts:
        assert fence.match(text) is None, text


def test_deprecation_fence_escalates_unwrapped_calls_from_test_frames(
    tmp_path: Path,
) -> None:
    """End-to-end fence proof, through pytest's real config parsing: a test
    module calling deprecated API unwrapped must FAIL the run, while the
    pytest.warns-wrapped twin in the same probe still passes. Before the
    message-scoped line landed, this exact probe was '1 passed, 1 warning' —
    warnings.warn(..., stacklevel=2) attributes the warning to the probe's
    own module, which the module-scoped filter never matches. The probe runs
    against THIS repo's pyproject.toml (`-c`), so the pin exercises the real
    line, not a re-simulation that could drift from the config."""
    probe = tmp_path / "test_fence_probe.py"
    probe.write_text(_FENCE_PROBE, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)  # keep the probe run hermetic
    src = Path(__file__).resolve().parents[1] / "src"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src) if not existing else str(src) + os.pathsep + existing
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(_PYPROJECT),
            "-p",
            "no:cacheprovider",
            # CLI beats PY_COLORS / FORCE_COLOR inherited from the invoking
            # shell: without this, a color-forcing env ANSI-wraps the
            # subprocess summary line and the literal "1 failed, 1 passed"
            # match below false-fails a perfectly healthy fence.
            "--color=no",
            str(probe),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    # Exit 1 is "tests ran, some failed" — a collection/usage error (2/4)
    # would mean the probe never exercised the fence at all.
    assert out.returncode == 1, out.stdout + out.stderr
    assert "1 failed, 1 passed" in out.stdout, out.stdout
    # The failure is the UNWRAPPED call, failed BY the escalated warning.
    assert "test_unwrapped_deprecated_call" in out.stdout
    assert (
        "DeprecationWarning: probe_api is deprecated and will be removed "
        "in bettermemory" in out.stdout
    ), out.stdout


# ---------------------------------------------------------------------------
# commit_author_timestamps_touching_pathspecs — the three-valued contract
# `verify.resolve_commit_drift_count` depends on. `None` (git can't answer)
# must stay distinguishable from `[]` (clean answer: every anchor is a
# phantom), because the caller maps the first to a conservative count and the
# second to "not applicable". These pin the primitive directly; the
# drift-level consequences are pinned in test_verify.py.
# ---------------------------------------------------------------------------


def test_author_timestamps_touching_none_for_none_cwd() -> None:
    assert commit_author_timestamps_touching_pathspecs(None, ["x.py"]) is None


def test_author_timestamps_touching_none_for_empty_pathspecs(tmp_path: Path) -> None:
    assert commit_author_timestamps_touching_pathspecs(tmp_path, []) is None


def test_author_timestamps_touching_none_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a git repo — existence is unknowable, so None (NOT []). The caller
    must keep its conservative count rather than treat every anchor as a
    phantom on an infrastructure failure."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    assert commit_author_timestamps_touching_pathspecs(tmp_path, ["x.py"]) is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_author_timestamps_touching_returns_author_dates_for_touched_file(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "real.py",
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    stamps = commit_author_timestamps_touching_pathspecs(tmp_path, ["real.py"])
    assert stamps is not None
    assert [ts.astimezone(timezone.utc) for ts in stamps] == [
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    ]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_author_timestamps_touching_empty_for_phantom_pathspec(tmp_path: Path) -> None:
    """A pathspec no commit ever touched yields a CONFIRMED phantom — the
    empty list, which the caller distinguishes from the None a git failure
    yields. That split is the whole point of `_git(empty_ok=True)`: `git log`
    exits 0 with empty stdout here, and non-zero when git itself can't run."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "real.py",
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert (
        commit_author_timestamps_touching_pathspecs(tmp_path, ["never/existed.py"])
        == []
    )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_author_timestamps_touching_nonempty_for_deleted_file(tmp_path: Path) -> None:
    """A since-deleted file is REAL, not phantom: its add and its removal are
    both commits that touched it, so it stays in the log. This is what keeps
    deleted-file commit drift working under the phantom-anchor guard."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "gone.py",
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    (tmp_path / "gone.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", "rm gone.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )
    stamps = commit_author_timestamps_touching_pathspecs(tmp_path, ["gone.py"])
    assert stamps, "a deleted-but-cited file must stay in history, not read as phantom"


# ---------------------------------------------------------------------------
# commit_patch_stream — the diff shape is pinned against user git config
# ---------------------------------------------------------------------------


def _set_repo_config(cwd: Path, key: str, value: str) -> None:
    """Write repo-local git config — the same inherited-config channel a
    hostile ~/.gitconfig reaches `git log` through, without touching the
    developer's real config."""
    subprocess.run(
        ["git", "config", key, value],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _delete_and_commit(cwd: Path, relpath: str) -> None:
    (cwd / relpath).unlink()
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "commit", "-m", f"rm {relpath}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=env,
    )


def _all_shas(cwd: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_patch_stream_pins_prefixes_against_noprefix_config(
    tmp_path: Path,
) -> None:
    """`diff.noprefix=true` in inherited config drops the `a/`/`b/` header
    prefixes, and a DELETION diff then parses to NOTHING: without a
    `--- a/` line there is no path in hand at `+++ /dev/null`, the hunk
    is skipped, and `parse_mismatches` stays 0 — a deleted claimed file
    measured zero commit drift and read fresh. The `-c` pinning must win
    over the inherited value, end to end through the parser."""
    _init_repo(tmp_path)
    _set_repo_config(tmp_path, "diff.noprefix", "true")
    _commit_file(
        tmp_path,
        "mod.py",
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _delete_and_commit(tmp_path, "mod.py")
    stream = commit_patch_stream(tmp_path, _all_shas(tmp_path), ["mod.py"])
    assert stream is not None
    assert "--- a/mod.py" in stream

    from bettermemory.claims import build_binding_index

    index = build_binding_index(stream)
    assert "mod.py" in index["deleted"]
    assert index["parse_mismatches"] == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_patch_stream_pins_custom_src_dst_prefixes(tmp_path: Path) -> None:
    """`diff.srcPrefix`/`diff.dstPrefix` (git >= 2.45) substitute arbitrary
    prefixes and `diff.mnemonicPrefix` swaps in c//w/ pairs — every path
    would be indexed under the wrong spelling. Pinning restores `a/`/`b/`.
    On older gits the unknown keys are ignored and the assertion holds
    trivially, so the test is deterministic across versions."""
    _init_repo(tmp_path)
    _set_repo_config(tmp_path, "diff.srcPrefix", "left/")
    _set_repo_config(tmp_path, "diff.dstPrefix", "right/")
    _set_repo_config(tmp_path, "diff.mnemonicPrefix", "true")
    _commit_file(
        tmp_path,
        "mod.py",
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "mod.py",
        content="x = 2\n",
        when=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    stream = commit_patch_stream(tmp_path, _all_shas(tmp_path), ["mod.py"])
    assert stream is not None
    assert "--- a/mod.py" in stream
    assert "+++ b/mod.py" in stream

    from bettermemory.claims import build_binding_index

    index = build_binding_index(stream)
    assert index["files"] == {"mod.py"}
    assert index["parse_mismatches"] == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commit_patch_stream_pins_quotepath_for_non_ascii_paths(
    tmp_path: Path,
) -> None:
    """`core.quotePath=true` — git's DEFAULT — octal-escapes non-ASCII
    path bytes, so a claimed non-ASCII file is indexed under its quoted
    spelling (`"b/mod\\303\\274l.py"`) and never equals the claim's
    rel_path. Pinning quotePath off emits the raw UTF-8 path. Cyrillic
    has no NFD decomposition, so the spelling is stable across APFS."""
    _init_repo(tmp_path)
    _set_repo_config(tmp_path, "core.quotePath", "true")
    name = "модуль.py"
    _commit_file(
        tmp_path,
        name,
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    stream = commit_patch_stream(tmp_path, _all_shas(tmp_path), [name])
    assert stream is not None
    assert f"+++ b/{name}" in stream
    assert "\\320" not in stream, "octal-escaped spelling leaked through"

    from bettermemory.claims import build_binding_index

    index = build_binding_index(stream)
    assert name in index["files"]


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
@pytest.mark.skipif(
    sys.platform == "win32", reason="'\"' is illegal in Windows filenames"
)
def test_commit_patch_stream_dequotes_embedded_quote_path(tmp_path: Path) -> None:
    """A path with an embedded double quote stays C-quoted even under
    `core.quotePath=false` (git always escapes quotes, backslashes, and
    control bytes), so the producer-side `_dequote_patch_headers` rewrite
    is what lets the parser record its deletion."""
    _init_repo(tmp_path)
    name = 'we"ird.py'
    _commit_file(
        tmp_path,
        name,
        content="x = 1\n",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _delete_and_commit(tmp_path, name)
    stream = commit_patch_stream(tmp_path, _all_shas(tmp_path), [name])
    assert stream is not None
    assert f"--- a/{name}" in stream

    from bettermemory.claims import build_binding_index

    index = build_binding_index(stream)
    assert name in index["deleted"]


def test_dequote_patch_headers_rewrites_only_complete_prefixed_headers() -> None:
    """The rewrite fires only when the whole header remainder decodes as
    ONE complete C-quoted string whose body carries the pinned prefix —
    content lines that merely resemble quoted headers, malformed quoting,
    and prefix-less strings all pass through byte-exact."""
    from bettermemory.origin import _dequote_patch_headers

    quoted_deletion = '--- "a/we\\"ird.py"'
    octal_header = '+++ "b/ctl\\001x.py"'
    content_line = '--- "just removed text"'
    unterminated = '--- "a/unterminated'
    stream = "\n".join(
        [quoted_deletion, "+++ /dev/null", octal_header, content_line, unterminated]
    )
    out = _dequote_patch_headers(stream).split("\n")
    assert out[0] == '--- a/we"ird.py'
    assert out[1] == "+++ /dev/null"
    assert out[2] == "+++ b/ctl\x01x.py"
    assert out[3] == content_line
    assert out[4] == unterminated

    # The zero-quoted-header fast path returns the identical object.
    plain = "--- a/mod.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1"
    assert _dequote_patch_headers(plain) is plain


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
def test_capture_worktree_null_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a repo there's no worktree to capture — keeps the field
    null instead of falling back to cwd, so the auto-scope filter's
    "both sides set → strict-equal" gate stays a no-op for non-repo
    callers."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
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


def test_worktrees_match_different_paths(tmp_path: Path) -> None:
    """Two distinct LIVE checkouts stay isolated. The fixtures must exist
    on disk — a nonexistent memory worktree now triggers the deliberate
    dead-worktree degrade (see `worktrees_match`)."""
    a = tmp_path / "main"
    b = tmp_path / "feature-x"
    a.mkdir()
    b.mkdir()
    assert not worktrees_match(str(a), str(b))


def test_should_include_filters_cross_worktree_same_repo(tmp_path: Path) -> None:
    """The integration: same repo, different LIVE worktree → exclude.
    Without the worktree layer this returned True (auditable as
    repo-only matching), letting feature-branch memories leak into
    the bug-fix worktree. Fixtures exist on disk so the dead-worktree
    degrade doesn't apply."""
    feature = tmp_path / "repo-feature"
    bugfix = tmp_path / "repo-bugfix"
    feature.mkdir()
    bugfix.mkdir()
    memory_origin = Origin(
        cwd=str(feature / "src" / "foo.py"),
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root=str(feature),
    )
    assert not should_include_for_caller(
        memory_origin,
        "git@github.com:example/repo.git",
        caller_worktree_root=str(bugfix),
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


# ---------------------------------------------------------------------------
# Linked-worktree relaxations in worktrees_match (2026-06-09 hunt, HIGH)
# ---------------------------------------------------------------------------


def _make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Fabricate the on-disk shape of a primary checkout plus one linked
    worktree without shelling out to git: the linked worktree's root
    carries a `.git` FILE pointing at `<primary>/.git/worktrees/<name>`."""
    primary = tmp_path / "primary"
    (primary / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {primary}/.git/worktrees/wt1\n")
    return primary, wt


def test_caller_in_linked_worktree_sees_primary_memories(tmp_path: Path) -> None:
    from bettermemory.origin import _primary_root_of, worktrees_match

    primary, wt = _make_linked_worktree(tmp_path)
    _primary_root_of.cache_clear()
    assert worktrees_match(str(primary), str(wt)) is True


def test_live_sibling_worktrees_stay_isolated(tmp_path: Path) -> None:
    from bettermemory.origin import _primary_root_of, worktrees_match

    primary, wt = _make_linked_worktree(tmp_path)
    sibling = tmp_path / "wt2"
    (primary / ".git" / "worktrees" / "wt2").mkdir()
    sibling.mkdir()
    (sibling / ".git").write_text(f"gitdir: {primary}/.git/worktrees/wt2\n")
    _primary_root_of.cache_clear()
    # memory written in LIVE sibling wt2, caller in wt1: still isolated.
    assert worktrees_match(str(sibling), str(wt)) is False


def test_primary_caller_does_not_see_live_worktree_memories(
    tmp_path: Path,
) -> None:
    from bettermemory.origin import _primary_root_of, worktrees_match

    primary, wt = _make_linked_worktree(tmp_path)
    _primary_root_of.cache_clear()
    # memory written in LIVE linked worktree, caller in primary: isolated
    # (the original worktree-leakage design, preserved).
    assert worktrees_match(str(wt), str(primary)) is False


def test_dead_worktree_memory_degrades_to_repo_match(tmp_path: Path) -> None:
    from bettermemory.origin import _primary_root_of, worktrees_match

    _primary_root_of.cache_clear()
    gone = tmp_path / "deleted-ephemeral-worktree"
    caller = tmp_path / "anywhere"
    caller.mkdir()
    assert worktrees_match(str(gone), str(caller)) is True


def test_distinct_existing_checkouts_still_isolated(tmp_path: Path) -> None:
    from bettermemory.origin import _primary_root_of, worktrees_match

    _primary_root_of.cache_clear()
    a = tmp_path / "clone-a"
    b = tmp_path / "clone-b"
    (a / ".git").mkdir(parents=True)
    (b / ".git").mkdir(parents=True)
    assert worktrees_match(str(a), str(b)) is False


# ---------------------------------------------------------------------------
# The degrade's boundary: "gone" vs "I could not find out"
#
# The dead-worktree degrade widens what a caller sees, so keying it on any
# failed stat made every transiently-unstattable path read as a dead one —
# an unmounted volume, a detached share, a permission-denied parent, a
# checkout under another user account. Those hold the isolation instead;
# only a positive "nothing is there" degrades.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the fixture clears POSIX search permission on a parent directory to "
        "make a LIVE path unstattable; Windows mode bits map onto the "
        "read-only attribute and cannot reproduce that shape"
    ),
)
def test_unstattable_live_worktree_keeps_isolation(tmp_path: Path) -> None:
    """A worktree that is still there but cannot be stat'd stays isolated.

    The distinction the degrade turns on: this path EXISTS — only the
    parent directory refuses to be searched. Treating that as death let
    a transient condition (a stale mount, a detached network share, a
    parent whose permissions changed) silently widen auto-scope for as
    long as it lasted, on the surface whose whole job is to keep one
    workspace's notes out of another's.
    """
    from bettermemory.origin import _primary_root_of, worktrees_match

    locked = tmp_path / "locked-parent"
    memory_wt = locked / "clone-a"
    (memory_wt / ".git").mkdir(parents=True)
    caller = tmp_path / "clone-b"
    (caller / ".git").mkdir(parents=True)

    os.chmod(locked, 0o000)
    try:
        try:
            os.stat(memory_wt)
        except OSError:
            pass
        else:
            pytest.skip("a 0o000 parent still yields a stat (running as root?)")
        _primary_root_of.cache_clear()
        assert worktrees_match(str(memory_wt), str(caller)) is False
    finally:
        # Restore before tmp_path teardown needs to walk back in.
        os.chmod(locked, 0o700)


class _StatRaises:
    """Stand-in for the `os` module in origin's namespace whose `stat`
    always raises. Patching origin's own binding rather than `os.stat`
    globally keeps the fake off every other stat the interpreter makes
    while the test runs."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def stat(self, path: object) -> object:
        raise self._exc


@pytest.mark.parametrize(
    "err",
    [errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG],
)
def test_path_intrinsic_errnos_read_as_gone(
    err: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of these is the OS answering "nothing resolves at that path",
    independent of who is asking — so the degrade fires."""
    from bettermemory import origin as origin_mod

    monkeypatch.setattr(origin_mod, "os", _StatRaises(OSError(err, "no")))
    assert origin_mod._worktree_root_is_gone("anywhere") is True


@pytest.mark.parametrize(
    "err",
    [
        errno.EACCES,
        errno.EPERM,
        errno.ENOTCONN,
        errno.ETIMEDOUT,
        errno.ESTALE,
        errno.EIO,
    ],
)
def test_indeterminate_errnos_hold_the_isolation(
    err: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access refusals and the unreachable-device family are "I could not
    find out", which is not evidence of death. The unclassified default is
    the same, which is why the errno set enumerates the GONE side only —
    an errno nobody has classified holds the boundary rather than opening
    it."""
    from bettermemory import origin as origin_mod

    monkeypatch.setattr(origin_mod, "os", _StatRaises(OSError(err, "no")))
    assert origin_mod._worktree_root_is_gone("anywhere") is False


def test_unnameable_path_reads_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.stat` rejecting the string before the OS sees it (embedded NUL,
    un-encodable surrogate) is an answer, not a refusal to answer."""
    from bettermemory import origin as origin_mod

    monkeypatch.setattr(origin_mod, "os", _StatRaises(ValueError("embedded null")))
    assert origin_mod._worktree_root_is_gone("anywhere") is True


@pytest.mark.parametrize(
    ("winerror", "expected"),
    [
        # ERROR_INVALID_NAME / ERROR_CANT_RESOLVE_FILENAME — path-intrinsic,
        # and `pathlib` treats both as not-exists too.
        (123, True),
        (1921, True),
        # ERROR_NOT_READY — "the drive exists but is not accessible", i.e. a
        # removable or disconnected volume. `Path.exists()` reports it as
        # not-exists; for an isolation boundary that is precisely the
        # unmounted-volume fail-open, so this one must NOT degrade.
        (21, False),
    ],
)
def test_windows_error_codes_are_classified_by_intent(
    winerror: int, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows-only codes CPython does not fold into one of the errnos.

    Runnable off Windows because the classification reads `winerror` off
    the exception with `getattr`: the fixture sets the attribute the way
    the platform would, so the branch is exercised on every leg of the
    matrix rather than only the one nobody runs locally."""
    from bettermemory import origin as origin_mod

    exc = OSError(errno.EINVAL, "windows")
    setattr(exc, "winerror", winerror)
    monkeypatch.setattr(origin_mod, "os", _StatRaises(exc))
    assert origin_mod._worktree_root_is_gone("anywhere") is expected


# ---------------------------------------------------------------------------
# The reachable walk — commit drift's anchor in commit-graph space
# ---------------------------------------------------------------------------


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_env(when: datetime) -> dict[str, str]:
    iso = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_DATE=iso,
        GIT_COMMITTER_DATE=iso,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return env


def _merge_repo(root: Path) -> dict[str, str]:
    """A history whose window holds a branch AUTHORED before the anchor
    and MERGED after it — the shape the author-date count misses.

        * after       (c.txt)            2025-03-02
        *   merge feat                   2025-03-01
        |\\
        | * old feature (src/feat.py)    2025-01-02   <- authored BEFORE
        * | main work  (b.txt)           2025-02-01   <- the ANCHOR
        |/
        * base        (a.txt)            2025-01-01
    """
    _init_repo(root, remote="git@github.com:example/repo.git")
    _commit_file(
        root, "a.txt", content="a\n", when=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    base = _git_out(root, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "-b", "feat"], cwd=root, check=True)
    _commit_file(
        root,
        "src/feat.py",
        content="X = 1\n",
        when=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    feature = _git_out(root, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
    _commit_file(
        root, "b.txt", content="b\n", when=datetime(2025, 2, 1, tzinfo=timezone.utc)
    )
    anchor = _git_out(root, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "feat", "-m", "merge feat"],
        cwd=root,
        check=True,
        capture_output=True,
        env=_git_env(datetime(2025, 3, 1, tzinfo=timezone.utc)),
    )
    merge = _git_out(root, "rev-parse", "HEAD")
    _commit_file(
        root, "c.txt", content="c\n", when=datetime(2025, 3, 2, tzinfo=timezone.utc)
    )
    after = _git_out(root, "rev-parse", "HEAD")
    return {
        "base": base,
        "feature": feature,
        "anchor": anchor,
        "merge": merge,
        "after": after,
    }


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_repo_toplevel_and_head_answers_both_from_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import origin as origin_module
    from bettermemory.origin import repo_toplevel_and_head

    _init_repo(tmp_path)
    assert repo_toplevel_and_head(tmp_path) is None, "no commit yet: no HEAD"
    _commit_file(
        tmp_path, "a.txt", content="a\n", when=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    calls: list[tuple[str, ...]] = []
    real_git = origin_module._git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str | None:
        calls.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git", spy)
    (tmp_path / "src").mkdir()
    located = repo_toplevel_and_head(tmp_path / "src")
    assert located is not None, "asked from a subdirectory: the root is the repo's"
    root, head = located
    assert root == tmp_path.resolve()
    assert head == _git_out(tmp_path, "rev-parse", "HEAD")
    assert len(calls) == 1 and calls[0][:2] == ("rev-parse", "--show-toplevel")


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_head_sha_and_commit_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory.origin import commit_reachable, head_sha

    outside = tmp_path / "outside"
    outside.mkdir()
    set_git_discovery_ceiling(outside, monkeypatch)
    assert head_sha(outside) is None
    assert commit_reachable(outside, "a" * 40) is None, "not a repository: no answer"

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    assert head_sha(repo) is None, "no commit yet"
    assert commit_reachable(repo, "a" * 40) is None, "no HEAD yet: no answer"
    _commit_file(
        repo, "a.txt", content="a\n", when=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    first = _git_out(repo, "rev-parse", "HEAD")
    _commit_file(
        repo, "b.txt", content="b\n", when=datetime(2025, 1, 2, tzinfo=timezone.utc)
    )
    head = _git_out(repo, "rev-parse", "HEAD")
    assert head_sha(repo) == head
    assert commit_reachable(repo, head) is True, "HEAD itself"
    assert commit_reachable(repo, first) is True, "an ancestor"
    assert commit_reachable(repo, "a" * 40) is False, "never here"
    assert commit_reachable(repo, "main") is None, "not a full hash: never asked"
    subprocess.run(
        ["git", "commit", "--amend", "-q", "-m", "b, rewritten"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=_git_env(datetime(2025, 1, 3, tzinfo=timezone.utc)),
    )
    # The object is still in the store (dangling); nothing descends from it.
    assert _git_out(repo, "cat-file", "-t", head) == "commit"
    assert commit_reachable(repo, head) is False
    assert commit_reachable(repo, first) is True


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_holds_a_branch_authored_before_the_anchor(tmp_path: Path) -> None:
    """The defect the anchor exists for, at the git boundary: the
    feature commit predates the anchor in author-date space and sits
    inside ``anchor..HEAD`` in reachability space."""
    from bettermemory.origin import commits_since_anchor

    shas = _merge_repo(tmp_path)
    walk = commits_since_anchor(tmp_path, shas["anchor"])
    assert walk is not None
    assert walk.anchor == shas["anchor"]
    assert walk.head == shas["after"]
    assert set(walk.commits) == {shas["after"], shas["merge"], shas["feature"]}
    # The merge commit carries no paths of its own; its branch's commit
    # carries the file it brought in.
    assert walk.touched == {
        "c.txt": frozenset({shas["after"]}),
        "src/feat.py": frozenset({shas["feature"]}),
    }
    assert walk.shas_touching(["src/feat.py"]) == {shas["feature"]}
    assert walk.shas_touching(["src"]) == {shas["feature"]}, "a directory spec"
    assert walk.shas_touching(["c.txt", "b.txt"]) == {shas["after"]}
    assert walk.shas_touching(["a.txt"]) == set(), "untouched in the range"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_from_head_itself_is_empty_not_absent(tmp_path: Path) -> None:
    from bettermemory.origin import commits_since_anchor

    shas = _merge_repo(tmp_path)
    walk = commits_since_anchor(tmp_path, shas["after"])
    assert walk is not None
    assert walk.commits == ()
    assert walk.shas_touching(["c.txt"]) == set()


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_refuses_an_anchor_head_no_longer_descends_from(
    tmp_path: Path,
) -> None:
    """A rewritten history (the anchor amended away) and a checkout that
    moved backwards both read None — the author-date count stands."""
    from bettermemory.origin import commits_since_anchor

    shas = _merge_repo(tmp_path)
    subprocess.run(
        ["git", "commit", "--amend", "-q", "-m", "after, rewritten"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=_git_env(datetime(2025, 3, 3, tzinfo=timezone.utc)),
    )
    assert commits_since_anchor(tmp_path, shas["after"]) is None
    # The anchor's parent is still an ancestor: the walk from it counts
    # the rewritten commit in place of the old one.
    walk = commits_since_anchor(tmp_path, shas["merge"])
    assert walk is not None
    assert len(walk.commits) == 1 and walk.commits[0] != shas["after"]

    subprocess.run(
        ["git", "reset", "-q", "--hard", shas["anchor"]],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert commits_since_anchor(tmp_path, shas["merge"]) is None, "HEAD is behind"
    assert commits_since_anchor(tmp_path, "b" * 40) is None, "does not resolve"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_never_hands_git_anything_but_a_full_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import origin as origin_module
    from bettermemory.origin import commits_since_anchor

    _merge_repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    real_git = origin_module._git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str | None:
        calls.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git", spy)
    for bad in ("main", "HEAD~1", "--output=/tmp/x", "3b1f9c0"):
        assert commits_since_anchor(tmp_path, bad) is None
    assert calls == []


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_is_memoised_per_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One process per (root, anchor, head); a commit landing changes
    the head and so the key, so a long-lived server never reuses a walk
    the tree has moved past. A dead anchor is memoised as None too."""
    from bettermemory import origin as origin_module
    from bettermemory.origin import commits_since_anchor, repo_toplevel_and_head

    shas = _merge_repo(tmp_path)
    located = repo_toplevel_and_head(tmp_path)
    assert located is not None
    root, head = located
    calls: list[tuple[str, ...]] = []
    real_git = origin_module._git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str | None:
        calls.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git", spy)
    first = commits_since_anchor(tmp_path, shas["anchor"], toplevel=root, head=head)
    second = commits_since_anchor(tmp_path, shas["anchor"], toplevel=root, head=head)
    assert first is second and first is not None
    assert len(calls) == 1 and calls[0][2:4] == ("log", "--boundary")

    assert commits_since_anchor(tmp_path, "b" * 40, toplevel=root, head=head) is None
    assert commits_since_anchor(tmp_path, "b" * 40, toplevel=root, head=head) is None
    assert len(calls) == 2, "the dead anchor forked once"

    _commit_file(
        tmp_path, "d.txt", content="d\n", when=datetime(2025, 4, 1, tzinfo=timezone.utc)
    )
    moved = repo_toplevel_and_head(tmp_path)
    assert moved is not None and moved[1] != head
    calls.clear()
    third = commits_since_anchor(tmp_path, shas["anchor"], toplevel=root, head=moved[1])
    assert third is not None and len(third.commits) == len(first.commits) + 1
    assert len(calls) == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_the_walk_reads_a_non_ascii_path_as_its_own_spelling(tmp_path: Path) -> None:
    """`core.quotePath` defaults to octal-escaping non-ASCII bytes, under
    which ``modül.py`` would never equal the resolved pathspec and a
    change to it would read as untouched."""
    from bettermemory.origin import commits_since_anchor

    shas = _merge_repo(tmp_path)
    _commit_file(
        tmp_path,
        "src/modül.py",
        content="Y = 2\n",
        when=datetime(2025, 4, 1, tzinfo=timezone.utc),
    )
    walk = commits_since_anchor(tmp_path, shas["after"])
    assert walk is not None
    assert list(walk.touched) == ["src/modül.py"]
    assert walk.shas_touching(["src/modül.py"]) == set(walk.commits)
