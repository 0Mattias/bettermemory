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
    commit_author_timestamps_touching_pathspecs,
    commits_since,
    commits_since_touching_paths,
    commits_touching_pathspecs,
    repos_match,
    resolve_repo_pathspecs,
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
# commits_since() / commit_author_timestamps() — git plumbing
#
# commits_since is DEPRECATED (slated for removal in 4.0; superseded by
# commit_author_timestamps + bisect_right, the author-date source all three
# commit-drift surfaces share). The behavior tests below still pin the 3.x
# contract verbatim; every commits_since call is wrapped in pytest.warns so
# the suite stays green under `-W error` / filterwarnings=error
# DeprecationWarning filters.
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


def test_commits_since_is_deprecated() -> None:
    """Every call — even one that early-returns None — must announce the
    deprecation, so a future reader can't silently wire the committer-date
    `--since` semantics (rebase-inflatable, inclusive-whole-second boundary)
    back into the drift path that deliberately abandoned them."""
    with pytest.warns(
        DeprecationWarning,
        match=r"commits_since is deprecated.*removed in.*4\.0",
    ):
        commits_since(None, datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_commits_since_returns_none_for_none_cwd() -> None:
    with pytest.warns(DeprecationWarning, match="commits_since is deprecated"):
        out = commits_since(None, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert out is None


def test_commits_since_returns_none_outside_repo(tmp_path: Path) -> None:
    """A directory with no `.git` is not a repo — count is None, not 0."""
    with pytest.warns(DeprecationWarning, match="commits_since is deprecated"):
        out = commits_since(tmp_path, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert out is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_zero_when_no_commits_after_anchor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _make_commit(tmp_path, "first", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Anchor strictly after the only commit.
    with pytest.warns(DeprecationWarning, match="commits_since is deprecated"):
        out = commits_since(tmp_path, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert out == 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_counts_commits_after_anchor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _make_commit(tmp_path, "old", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new1", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new2", when=datetime(2026, 2, 2, tzinfo=timezone.utc))
    with pytest.warns(DeprecationWarning, match="commits_since is deprecated"):
        out = commits_since(tmp_path, datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert out == 2


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_naive_datetime_treated_as_utc(tmp_path: Path) -> None:
    """A `datetime` without tzinfo is normalised to UTC — same convention
    used by `compute_verification_status` and the rest of the store."""
    _init_repo(tmp_path)
    _make_commit(tmp_path, "old", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_commit(tmp_path, "new", when=datetime(2026, 2, 1, tzinfo=timezone.utc))
    naive_anchor = datetime(2026, 1, 15)  # no tzinfo
    with pytest.warns(DeprecationWarning, match="commits_since is deprecated"):
        out = commits_since(tmp_path, naive_anchor)
    assert out == 1


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
# resolve_repo_pathspecs — anchor resolution at the git boundary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_resolve_repo_pathspecs_drops_repo_root(tmp_path: Path) -> None:
    """A citation of the repo root itself ("the project lives at X") is a
    location claim, not a content claim — as a pathspec it would be "."
    and match every commit, so it must not survive resolution. All
    spellings of the root collapse to the same drop: absolute, trailing
    slash, and the bare relative "."."""
    _init_repo(tmp_path)
    _make_commit(tmp_path, "first", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    resolved = resolve_repo_pathspecs(
        tmp_path,
        [str(tmp_path), str(tmp_path) + "/", "."],
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
# commits_since_touching_paths / commits_touching_pathspecs — DEPRECATED
# committer-date family (slated for removal in 4.0; superseded by
# resolve_repo_pathspecs + commit_author_timestamps_touching_pathspecs via
# verify.resolve_commit_drift_count). The behavior tests below still pin the
# 3.x contract verbatim; every call is wrapped in pytest.warns so the suite
# stays green under `-W error` / filterwarnings=error DeprecationWarning
# filters. The composition warns exactly ONCE per call — it routes through
# the module-private impl, not the deprecated public primitive (the
# single-warn seam pinned at the end of this section).
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


def test_commits_since_touching_paths_is_deprecated() -> None:
    """Every call — even one that early-returns None — must announce the
    deprecation, so a future reader can't silently wire the committer-date
    `--since` semantics (rebase-inflatable) and the None-on-all-dropped
    contract back into the drift path that deliberately abandoned them."""
    with pytest.warns(
        DeprecationWarning,
        match=r"commits_since_touching_paths is deprecated.*removed in.*4\.0",
    ):
        commits_since_touching_paths(
            None,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            ["x.py"],
        )


def test_commits_since_touching_paths_returns_none_for_none_cwd() -> None:
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        out = commits_since_touching_paths(
            None,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            ["/tmp/x"],
        )
    assert out is None


def test_commits_since_touching_paths_returns_none_for_empty_paths(
    tmp_path: Path,
) -> None:
    """No paths means no useful filter — the caller falls back to the
    unfiltered count via the verify.py wrapper."""
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        out = commits_since_touching_paths(
            tmp_path,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            [],
        )
    assert out is None


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
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
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
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
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
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        out = commits_since_touching_paths(
            tmp_path,
            datetime(2025, 12, 1, tzinfo=timezone.utc),
            ["/nonexistent/outside-repo.txt"],
        )
    assert out is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_touching_paths_counts_from_repo_subdirectory(
    tmp_path: Path,
) -> None:
    """Regression: pathspecs must anchor at the repo root regardless of the
    caller's cwd. The MCP server / agent is frequently launched from or
    chdir'd into a SUBDIRECTORY of the repo; git resolves a plain
    root-relative pathspec (``src/foo.py``) relative to the invocation cwd,
    so from a subdir it matched nothing and rev-list returned 0 — silently
    reporting a genuinely-drifted verified path as clean (the unsafe
    direction). The ``:/`` (``:(top)``) magic prefix anchors at the top of
    the working tree. Before the fix the subdir cases below returned 0.
    """
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "src/tracked.txt",
        content="initial",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "src/tracked.txt",
        content="updated",
        when=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    subdir = tmp_path / "src"
    nested_abs = str(subdir / "tracked.txt")
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)

    # Baseline from the repo root: one post-`since` commit touched the path.
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        assert commits_since_touching_paths(tmp_path, since, [nested_abs]) == 1
    # From a SUBDIRECTORY with an absolute verified path — the bug returned 0.
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        assert commits_since_touching_paths(subdir, since, [nested_abs]) == 1
    # The relative-input form (treated as repo-root-relative) is subdir-safe too.
    with pytest.warns(DeprecationWarning, match="commits_since_touching_paths"):
        assert commits_since_touching_paths(subdir, since, ["src/tracked.txt"]) == 1


def test_commits_touching_pathspecs_is_deprecated() -> None:
    """Every call — even one that early-returns None — must announce the
    deprecation. Same fence as `commits_since_touching_paths`: at 4.0 its
    only production caller (that deprecated composition) disappears, and an
    exported committer-date `--since` counter left warning-free would invite
    a future reader to re-wire the rebase-inflatable semantics the
    author-date drift path deliberately abandoned."""
    with pytest.warns(
        DeprecationWarning,
        match=r"commits_touching_pathspecs is deprecated.*removed in.*4\.0",
    ):
        commits_touching_pathspecs(
            None,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            ["x.py"],
        )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_touching_pathspecs_behavior_unchanged_while_deprecated(
    tmp_path: Path,
) -> None:
    """The deprecation is announce-only for the 3.x line: the public wrapper
    still delegates to the real count (a warn-and-forget-to-return refactor
    would pass the announce test above but break this pin)."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="initial",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="updated",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    with pytest.warns(DeprecationWarning, match="commits_touching_pathspecs"):
        out = commits_touching_pathspecs(
            tmp_path,
            datetime(2026, 1, 15, tzinfo=timezone.utc),
            ["tracked.txt"],
        )
    assert out == 1


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_commits_since_touching_paths_warns_once_per_call(tmp_path: Path) -> None:
    """The single-warn seam: the deprecated composition routes through the
    module-private `_commits_touching_pathspecs_impl`, NOT the deprecated
    public `commits_touching_pathspecs` wrapper — one entry point, exactly
    one DeprecationWarning, attributed to the caller's line. The fixture
    must reach the inner count (successful full composition), otherwise an
    early return would trivially satisfy the once-only assertion."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "tracked.txt",
        content="initial",
        when=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    with pytest.warns(DeprecationWarning) as record:
        out = commits_since_touching_paths(
            tmp_path,
            datetime(2026, 1, 15, tzinfo=timezone.utc),
            [str(tmp_path / "tracked.txt")],
        )
    assert out == 1  # inner count reached — the seam was actually exercised
    deprecations = [w for w in record if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "commits_since_touching_paths is deprecated" in str(deprecations[0].message)


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


def test_author_timestamps_touching_none_outside_repo(tmp_path: Path) -> None:
    """Not a git repo — existence is unknowable, so None (NOT []). The caller
    must keep its conservative count rather than treat every anchor as a
    phantom on an infrastructure failure."""
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
