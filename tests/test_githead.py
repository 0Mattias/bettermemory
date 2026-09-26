"""Tests for githead.py: the commit HEAD names and a change signature,
read from the repository's files.

Every test builds a real repository with git under ``tmp_path`` and holds
the reader to git's own answers: `head_sha` to ``git rev-parse --verify
HEAD^{commit}``, `head_ref` to ``git symbolic-ref HEAD``, and the
directories `find_gitdir` reports to ``git rev-parse``'s. Where the reader
declines, the test shows that git refuses the state as well, or that the
files alone do not settle it.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bettermemory import githead
from bettermemory.origin import is_full_commit_sha

from .conftest import set_git_discovery_ceiling

_GIT_AVAILABLE = shutil.which("git") is not None

pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the reader answers on POSIX only and declines elsewhere",
)


@pytest.fixture(autouse=True)
def git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's git environment and configuration away from
    the repositories these tests build and from the reader: no inherited
    discovery override or ceiling, no global or system configuration on
    POSIX (a signing or hook setting there would break the fixtures'
    commits; off POSIX the one test that runs commits the way the rest of
    the suite does), and a fixed identity for the commits."""
    for name in (*githead._DISCOVERY_ENV, "GIT_CEILING_DIRECTORIES", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    if os.name == "posix":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _run(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "--initial-branch=main")
    return path


def _commit(path: Path, message: str) -> str:
    _run(path, "commit", "--allow-empty", "-m", message)
    return _run(path, "rev-parse", "HEAD")


def _git_head(cwd: Path) -> str | None:
    """What the reader's `head_sha` must equal whenever it answers."""
    result = _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return result.stdout.strip() if result.returncode == 0 else None


def _git_symref(cwd: Path) -> str | None:
    """What the reader's `head_ref` must equal whenever it answers."""
    result = _git(cwd, "symbolic-ref", "--quiet", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _assert_agrees_with_git(cwd: Path) -> githead.GitDir:
    """`find_gitdir` from `cwd` reports the directories git reports, and
    the reader resolves HEAD to git's answers. Returns what it found."""
    gd = githead.find_gitdir(cwd)
    assert gd is not None
    gitdir, common, toplevel = _run(
        cwd,
        "rev-parse",
        "--path-format=absolute",
        "--absolute-git-dir",
        "--git-common-dir",
        "--show-toplevel",
    ).splitlines()
    assert gd.gitdir.resolve() == Path(gitdir).resolve()
    assert gd.commondir == Path(common).resolve()
    assert gd.worktree_root == Path(toplevel).resolve()
    assert githead.head_sha(gd) == _git_head(cwd)
    assert githead.head_ref(gd) == _git_symref(cwd)
    assert githead.gitdir_at(gd.worktree_root) == gd
    return gd


def _mkfifo(path: Path) -> None:
    if sys.platform == "win32":
        # The tests that call this run on POSIX only; the branch keeps the
        # POSIX-only call out of the Windows type check.
        raise AssertionError("FIFOs are POSIX only")
    os.mkfifo(path)


def _undetermined(start: Path) -> bool:
    """Whether `signature` marks `start` as undetermined: two calls with
    nothing changed in between compare unequal."""
    return githead.signature(start) != githead.signature(start)


# ---------------------------------------------------------------------------
# head_sha and head_ref against git
# ---------------------------------------------------------------------------


@posix_only
def test_primary_checkout_on_a_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) == sha
    assert githead.head_ref(gd) == "refs/heads/main"
    assert gd.gitdir == gd.commondir == repo.resolve() / ".git"
    assert gd.worktree_root == repo.resolve()
    assert githead.head_bytes(gd) == b"ref: refs/heads/main\n"


@posix_only
def test_detached_head(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "one")
    _commit(repo, "two")
    _run(repo, "checkout", "--quiet", "--detach", first)
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) == first
    assert githead.head_ref(gd) is None
    assert githead.head_bytes(gd) == f"{first}\n".encode()


@posix_only
def test_branch_read_from_packed_refs(tmp_path: Path) -> None:
    """After ``git pack-refs --all`` the branch has no loose file; its
    line in packed-refs sits between the header and an annotated tag's
    peeled line, neither of which may be read as the branch."""
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    _run(repo, "tag", "--annotate", "v1", "--message", "release")
    _run(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    packed = (repo / ".git" / "packed-refs").read_text()
    assert packed.startswith("# pack-refs with:")
    assert f"{sha} refs/heads/main\n" in packed
    assert f"\n^{sha}\n" in packed
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) == sha


@posix_only
def test_loose_ref_shadows_a_stale_packed_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    old = _commit(repo, "one")
    _run(repo, "pack-refs", "--all")
    new = _commit(repo, "two")
    assert f"{old} refs/heads/main\n" in (repo / ".git" / "packed-refs").read_text()
    assert (repo / ".git" / "refs" / "heads" / "main").read_text().strip() == new
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) == new


@posix_only
def test_linked_worktree(tmp_path: Path) -> None:
    """A linked worktree's ``.git`` is a file naming its own git directory
    under the primary's ``worktrees/``, whose ``commondir`` file leads back
    to the shared refs. Each checkout keeps its own HEAD."""
    repo = _init_repo(tmp_path / "repo")
    base = _commit(repo, "one")
    tree = tmp_path / "tree"
    _run(repo, "worktree", "add", "--quiet", "-b", "feature", str(tree))
    tip = _commit(tree, "two")
    assert (tree / ".git").is_file()
    gd = _assert_agrees_with_git(tree)
    assert gd.gitdir == (repo / ".git" / "worktrees" / "tree").resolve()
    assert gd.commondir == (repo / ".git").resolve()
    assert gd.worktree_root == tree.resolve()
    assert githead.head_sha(gd) == tip
    assert githead.head_ref(gd) == "refs/heads/feature"
    primary = _assert_agrees_with_git(repo)
    assert githead.head_sha(primary) == base
    _run(tree, "checkout", "--quiet", "--detach", base)
    assert githead.head_sha(_assert_agrees_with_git(tree)) == base
    assert githead.head_ref(gd) is None
    assert githead.head_ref(primary) == "refs/heads/main"


@posix_only
def test_per_worktree_ref_is_read_from_the_worktree_git_directory(
    tmp_path: Path,
) -> None:
    """Refs under refs/worktree/ (like refs/bisect/ and refs/rewritten/)
    live in each worktree's own git directory, not the shared one."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    tree = tmp_path / "tree"
    _run(repo, "worktree", "add", "--quiet", "-b", "feature", str(tree))
    tip = _commit(tree, "two")
    _run(tree, "symbolic-ref", "refs/worktree/current", "refs/heads/feature")
    _run(tree, "symbolic-ref", "HEAD", "refs/worktree/current")
    gd = _assert_agrees_with_git(tree)
    assert (gd.gitdir / "refs" / "worktree" / "current").is_file()
    assert not (gd.commondir / "refs" / "worktree" / "current").exists()
    assert githead.head_sha(gd) == tip
    assert githead.head_ref(gd) == "refs/heads/feature"


@posix_only
def test_branch_name_with_slashes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    _run(repo, "checkout", "--quiet", "-b", "feature/deep/work")
    sha = _commit(repo, "two")
    gd = _assert_agrees_with_git(repo)
    assert githead.head_ref(gd) == "refs/heads/feature/deep/work"
    assert githead.head_sha(gd) == sha
    _run(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "feature" / "deep" / "work").exists()
    assert githead.head_sha(_assert_agrees_with_git(repo)) == sha


@posix_only
def test_unborn_branch(tmp_path: Path) -> None:
    """A repository with no commit: HEAD names a branch that does not
    exist yet. Git has no commit to print and still names the branch."""
    repo = _init_repo(tmp_path / "repo")
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) is None
    assert _git(repo, "rev-parse", "--verify", "HEAD^{commit}").returncode != 0
    assert githead.head_ref(gd) == "refs/heads/main"


@posix_only
def test_symbolic_chain_up_to_git_depth_limit(tmp_path: Path) -> None:
    """Git reads at most five refs, HEAD's included (SYMREF_MAXDEPTH):
    HEAD -> a3 -> a2 -> a1 -> main resolves, one more hop does not."""
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    _run(repo, "symbolic-ref", "refs/heads/a1", "refs/heads/main")
    for n in (2, 3, 4):
        _run(repo, "symbolic-ref", f"refs/heads/a{n}", f"refs/heads/a{n - 1}")
    _run(repo, "symbolic-ref", "HEAD", "refs/heads/a3")
    gd = _assert_agrees_with_git(repo)
    assert githead.head_sha(gd) == sha
    assert githead.head_ref(gd) == "refs/heads/main"
    _run(repo, "symbolic-ref", "HEAD", "refs/heads/a4")
    assert _git_head(repo) is None
    assert _git_symref(repo) is None
    assert githead.head_sha(gd) is None
    assert githead.head_ref(gd) is None


@posix_only
def test_head_on_a_tag_declines(tmp_path: Path) -> None:
    """HEAD pointed at an annotated tag stores the tag object's hash; git
    peels it to the commit, the reader declines rather than peel."""
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    _run(repo, "tag", "--annotate", "v1", "--message", "release")
    _run(repo, "symbolic-ref", "HEAD", "refs/tags/v1")
    tag_object = (repo / ".git" / "refs" / "tags" / "v1").read_text().strip()
    assert tag_object != sha
    assert _git_head(repo) == sha
    gd = githead.find_gitdir(repo)
    assert gd is not None
    assert githead.head_sha(gd) is None
    assert githead.head_ref(gd) == _git_symref(repo) == "refs/tags/v1"


@posix_only
@pytest.mark.parametrize(
    ("content", "agrees"),
    [
        (b"ref:refs/heads/main\n", True),
        (b"ref: \t refs/heads/main \r\n", True),
        (b"ref: refs/heads/main", True),
        (b"{sha_upper}\n", False),
        (b"{sha} FETCH_HEAD-style trailing text\n", False),
        (b"ref: refs/heads/../main\n", True),
        (b"ref: refs/heads/main.lock\n", True),
        (b"ref: refs/heads/ma in\n", True),
        (b"ref: heads/main\n", True),
    ],
)
def test_hand_written_head_is_read_as_git_reads_it_or_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes, agrees: bool
) -> None:
    """HEAD files git did not write. Wherever the reader answers, its
    answer is git's; where git accepts a shape the reader does not
    (uppercase hex, text after the hash) the reader declines. The
    ceiling keeps git from walking past a git directory it rejects."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    written = content.replace(b"{sha_upper}", sha.upper().encode()).replace(
        b"{sha}", sha.encode()
    )
    (repo / ".git" / "HEAD").write_bytes(written)
    gd = githead.GitDir(repo / ".git", repo / ".git", repo)
    assert githead.head_bytes(gd) == written
    if agrees:
        assert githead.head_sha(gd) == _git_head(repo)
        assert githead.head_ref(gd) == _git_symref(repo)
    else:
        assert _git_head(repo) == sha
        assert githead.head_sha(gd) is None


@posix_only
def test_symlinked_head_declines(tmp_path: Path) -> None:
    """Git's oldest HEAD format is a symbolic link into refs/, which git
    still reads; its content is the branch's hash, not its name, so the
    reader declines rather than take it for a detached HEAD."""
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    head = repo / ".git" / "HEAD"
    head.unlink()
    head.symlink_to("refs/heads/main")
    assert _git_head(repo) == sha
    gd = githead.find_gitdir(repo)
    assert gd is not None
    assert githead.head_bytes(gd) is None
    assert githead.head_sha(gd) is None
    assert githead.head_ref(gd) is None
    assert _undetermined(repo)


@posix_only
def test_reftable_repository_declines(tmp_path: Path) -> None:
    """A reftable repository keeps its refs in reftable/ and a stub HEAD
    naming refs/heads/.invalid, so neither HEAD's bytes nor the loose
    files say which branch is checked out."""
    repo = tmp_path / "repo"
    made = _git(
        tmp_path, "init", "--initial-branch=main", "--ref-format=reftable", str(repo)
    )
    if made.returncode != 0:
        pytest.skip("this git predates the reftable backend")
    _commit(repo, "one")
    assert (repo / ".git" / "HEAD").read_bytes() == b"ref: refs/heads/.invalid\n"
    assert _git_head(repo) is not None
    gd = githead.find_gitdir(repo)
    assert gd is not None
    assert githead.head_sha(gd) is None
    assert githead.head_ref(gd) is None
    assert _undetermined(repo)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@posix_only
def test_discovery_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository inside a directory that is not one: the walk from a
    subdirectory stops at the repository's root, and the plain directory
    above it is outside any repository."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    outer = tmp_path / "outer"
    repo = _init_repo(outer / "repo")
    _commit(repo, "one")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    gd = _assert_agrees_with_git(sub)
    assert gd.worktree_root == repo.resolve()
    assert githead.find_gitdir(outer) is None
    assert _git(outer, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.gitdir_at(sub) is None


@posix_only
def test_non_repository_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git(plain, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(plain) is None
    assert githead.gitdir_at(plain) is None
    assert githead.signature(plain) == githead.signature(plain)


@posix_only
def test_gitfile_with_a_relative_path(tmp_path: Path) -> None:
    """A ``.git`` file's relative path is relative to the directory that
    holds the file, the shape submodules use."""
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, "one")
    store = tmp_path / "store"
    store.mkdir()
    (repo / ".git").rename(store / "repo.git")
    (repo / ".git").write_text("gitdir: ../store/repo.git\n")
    gd = _assert_agrees_with_git(repo)
    assert gd.gitdir == gd.commondir == (store / "repo.git").resolve()
    assert githead.head_sha(gd) == sha


@posix_only
def test_submodule(tmp_path: Path) -> None:
    """A submodule's ``.git`` is a file with a relative path into the
    superproject's ``.git/modules``; its git directory is also its common
    directory."""
    library = _init_repo(tmp_path / "library")
    _commit(library, "library")
    project = _init_repo(tmp_path / "project")
    _commit(project, "project")
    _run(
        project,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "--quiet",
        "add",
        str(library),
        "vendor/library",
    )
    sub = project / "vendor" / "library"
    assert (sub / ".git").read_text().startswith("gitdir: ../../.git/modules/")
    gd = _assert_agrees_with_git(sub)
    assert gd.gitdir == gd.commondir
    assert gd.worktree_root == sub.resolve()
    inner = sub / "docs"
    inner.mkdir()
    assert _assert_agrees_with_git(inner) == gd
    assert (
        _assert_agrees_with_git(project / "vendor").worktree_root == project.resolve()
    )


@posix_only
def test_ceiling_stops_the_walk_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git honours a ceiling only as a strict ancestor of the directory it
    probes: below the ceiling the enclosing repository is out of reach, from
    the ceiling directory itself it is not."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    ceiling = repo / "a"
    deep = ceiling / "b"
    deep.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(ceiling))
    assert _git(deep, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(deep) is None
    assert _assert_agrees_with_git(ceiling).worktree_root == repo.resolve()


@posix_only
def test_ceiling_entries_after_an_empty_one_are_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git resolves symbolic links in ceiling entries, except in the
    entries after an empty one: a link to the ceiling then no longer
    matches the physical path being walked."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    ceiling = repo / "a"
    deep = ceiling / "b"
    deep.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(ceiling)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(link))
    assert _git(deep, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(deep) is None
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep + str(link))
    assert _assert_agrees_with_git(deep).worktree_root == repo.resolve()


@posix_only
@pytest.mark.parametrize("name", sorted(githead._DISCOVERY_ENV))
def test_discovery_environment_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """With GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR or another variable
    that moves git's discovery set, the walk is not what git does, so
    the reader declines even inside a repository and the signature never
    matches."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    values = {
        "GIT_DIR": str(repo / ".git"),
        "GIT_WORK_TREE": str(repo),
        "GIT_COMMON_DIR": str(repo / ".git"),
        "GIT_OBJECT_DIRECTORY": str(repo / ".git" / "objects"),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
    }
    monkeypatch.setenv(name, values[name])
    assert githead.find_gitdir(repo) is None
    assert githead.gitdir_at(repo) is None
    assert _undetermined(repo)


@posix_only
def test_gitfile_naming_a_missing_directory_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text(f"gitdir: {tmp_path / 'nowhere'}\n")
    assert _git(broken, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(broken) is None
    assert githead.gitdir_at(broken) is None
    assert _undetermined(broken)


@posix_only
@pytest.mark.parametrize("damage", ["garbage HEAD", "no objects", "no refs"])
def test_invalid_git_directory_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """A ``.git`` directory that fails git's `is_git_directory` is skipped
    by git's walk, which then answers for whatever lies above it; the
    reader declines instead of guessing, and a HEAD with garbage in it
    resolves to nothing."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    dot_git = repo / ".git"
    if damage == "garbage HEAD":
        (dot_git / "HEAD").write_bytes(b"garbage\n")
    else:
        shutil.rmtree(dot_git / damage.split()[1])
    assert _git(repo, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(repo) is None
    assert githead.gitdir_at(repo) is None
    assert _undetermined(repo)
    if damage == "garbage HEAD":
        gd = githead.GitDir(dot_git, dot_git, repo)
        assert githead.head_sha(gd) is None
        assert githead.head_ref(gd) is None


@posix_only
def test_bare_repository_and_the_inside_of_a_git_directory_decline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git answers a bare repository, and a directory inside ``.git``, with
    no working tree; the reader declines both."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    bare = tmp_path / "bare.git"
    _run(tmp_path, "init", "--quiet", "--bare", str(bare))
    assert _git(bare, "rev-parse", "--show-toplevel").returncode != 0
    assert githead.find_gitdir(bare) is None
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    for inside in (repo / ".git", repo / ".git" / "refs" / "heads"):
        assert _git(inside, "rev-parse", "--show-toplevel").returncode != 0
        assert githead.find_gitdir(inside) is None
        assert _undetermined(inside)


@posix_only
def test_a_fifo_where_a_file_belongs_declines_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's open of a FIFO named HEAD waits for a writer, so git is not
    run against one here. The reader opens without blocking and declines:
    for a HEAD in a directory the walk passes through, and for the
    repository's own HEAD."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    gd = githead.find_gitdir(repo)
    assert gd is not None
    plain = repo / "plain"
    plain.mkdir()
    _mkfifo(plain / "HEAD")
    assert githead.find_gitdir(plain) is None
    assert _undetermined(plain)
    head = repo / ".git" / "HEAD"
    head.unlink()
    _mkfifo(head)
    assert githead.head_bytes(gd) is None
    assert githead.head_sha(gd) is None
    assert githead.find_gitdir(repo) is None
    assert githead.gitdir_at(repo) is None


@posix_only
def test_repository_owned_by_another_user_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git refuses a repository another user owns unless the user's own
    configuration lists it in safe.directory, which the reader does not
    read."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    refused = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "dubious ownership" in refused.stderr
    monkeypatch.setattr(os, "geteuid", lambda: repo.stat().st_uid + 1)
    assert githead.find_gitdir(repo) is None
    assert githead.gitdir_at(repo) is None
    assert _undetermined(repo)


def test_the_reader_declines_off_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off POSIX git's ownership check reads security descriptors, which
    the reader does not, so discovery declines and callers ask git."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    monkeypatch.setattr(githead, "_POSIX", False)
    assert githead.find_gitdir(repo) is None
    assert githead.gitdir_at(repo) is None
    assert _undetermined(repo)


@pytest.mark.parametrize(
    "value",
    [
        "0" * 40,
        "0123456789abcdef" * 4,
        "a" * 64,
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "g" * 40,
        "",
    ],
)
def test_hash_shape_is_the_origin_predicate(value: str) -> None:
    """The reader accepts exactly the hashes `origin.is_full_commit_sha`
    accepts, without importing origin."""
    shaped = githead._FULL_HASH.fullmatch(value.encode()) is not None
    assert shaped == is_full_commit_sha(value)


@posix_only
def test_seeded_operation_sequence_agrees_with_git(tmp_path: Path) -> None:
    """A fixed pseudo-random sequence of the ref operations a working
    session performs, in a primary checkout and a linked worktree of one
    repository: after every step the reader agrees with git in both, and
    the signature's HEAD slot moves exactly when HEAD does."""
    rng = random.Random(20260926)
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "root")
    tree = tmp_path / "tree"
    _run(repo, "worktree", "add", "--quiet", "-b", "side", str(tree))
    checkouts = [repo, tree]
    names = ["main", "side"]
    counter = 0

    def step(where: Path) -> None:
        nonlocal counter
        counter += 1
        op = rng.choice(
            [
                "commit",
                "commit",
                "branch",
                "switch",
                "detach",
                "pack",
                "rename",
                "orphan",
                "tag",
            ]
        )
        if op == "commit":
            _commit(where, f"c{counter}")
        elif op == "branch":
            name = rng.choice(["feature", "fix/deep", "topic"]) + f"-{counter}"
            if _git_head(where) is None:
                _run(where, "checkout", "--quiet", "--orphan", name)
            else:
                _run(where, "checkout", "--quiet", "-b", name)
            names.append(name)
        elif op == "switch":
            taken = {_git_symref(other) for other in checkouts}
            free = [
                n
                for n in names
                if f"refs/heads/{n}" not in taken
                and _git(
                    where, "rev-parse", "--verify", "--quiet", f"refs/heads/{n}"
                ).returncode
                == 0
            ]
            if free:
                _run(where, "checkout", "--quiet", rng.choice(free))
        elif op == "detach":
            if _git_head(where) is not None:
                _run(where, "checkout", "--quiet", "--detach")
        elif op == "pack":
            _run(where, "pack-refs", "--all")
        elif op == "rename":
            current = _git_symref(where)
            if current is not None and _git_head(where) is not None:
                name = f"renamed-{counter}"
                _run(where, "branch", "-m", name)
                names.append(name)
        elif op == "orphan":
            name = f"orphan-{counter}"
            _run(where, "checkout", "--quiet", "--orphan", name)
            names.append(name)
        elif op == "tag" and _git_head(where) is not None:
            _run(where, "tag", "--annotate", f"t{counter}", "--message", "tag")

    for _ in range(30):
        where = rng.choice(checkouts)
        before = {c: githead.signature(c) for c in checkouts}
        step(where)
        for checkout in checkouts:
            gd = _assert_agrees_with_git(checkout)
            after = githead.signature(checkout)
            head_moved = githead.head_bytes(gd) != before[checkout][1]
            # The git-directory and HEAD slots move exactly when HEAD does;
            # the config slot also moves when an operation rewrites the
            # shared config (`git branch -m` renames its section), and
            # always reads the file as it now stands.
            assert (after[:2] != before[checkout][:2]) == head_moved
            status = (gd.commondir / "config").stat()
            assert after[2] == (status.st_mtime_ns, status.st_size)


# ---------------------------------------------------------------------------
# signature
# ---------------------------------------------------------------------------


@posix_only
def test_signature_is_stable_across_reads(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    _run(repo, "remote", "add", "origin", "git@github.com:example/foo.git")
    sub = repo / "sub"
    sub.mkdir()
    before = githead.signature(repo)
    _run(repo, "status", "--porcelain")
    _run(repo, "log", "-1")
    _run(repo, "rev-parse", "HEAD")
    assert githead.signature(repo) == before
    assert githead.signature(sub) == before
    gitdir, head, config, *env = before
    assert gitdir == str(repo.resolve() / ".git")
    assert head == b"ref: refs/heads/main\n"
    status = (repo / ".git" / "config").stat()
    assert config == (status.st_mtime_ns, status.st_size)
    assert env == [None, None, None]


@posix_only
def test_signature_changes_on_checkout_of_a_new_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    before = githead.signature(repo)
    _run(repo, "checkout", "--quiet", "-b", "feature")
    assert githead.signature(repo) != before


@posix_only
def test_signature_changes_on_remote_set_url(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    _run(repo, "remote", "add", "origin", "git@github.com:example/foo.git")
    before = githead.signature(repo)
    _run(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/example/foo-renamed.git",
    )
    assert githead.signature(repo) != before


@posix_only
def test_signature_changes_when_a_directory_becomes_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    before = githead.signature(project)
    assert before[:3] == (None, None, None)
    _run(project, "init", "--quiet", "--initial-branch=main")
    after = githead.signature(project)
    assert after != before
    assert after[0] == str(project.resolve() / ".git")


@posix_only
def test_signature_changes_with_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "one")
    before = githead.signature(repo)
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    after = githead.signature(repo)
    assert after != before
    assert after[:3] == before[:3]
