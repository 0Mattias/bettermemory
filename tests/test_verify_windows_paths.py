"""verify's reads of a repository's own files where githead's discovery
declines, and under other spellings of the same paths (design 3.6, E18 c).

`verify.holds_commit` (a cited commit in the caller's store) and
`verify._absent_by_sparsity` (a path a sparse cone leaves out) find the
repository through `githead.locate`. `githead.find_gitdir` and
`githead.gitdir_at` decline off POSIX (`githead._POSIX`), so on Windows
neither read answered before; every test here runs with that decline.
Each read is then given its path spelled another way than the
repository's resolved root: through a symbolic link (macOS's temporary
directory is reached through one, ``/var`` for ``/private/var``), and in
another case where the filesystem folds it. Windows' short 8.3 names are
the same mismatch, resolved the same way, and cannot be made here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import githead, verify
from bettermemory.githead import GitDir

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture(autouse=True)
def windows_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed author, no inherited
    repository variables, and githead's discovery declining as on Windows."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    monkeypatch.setattr(githead, "_POSIX", False)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(path: Path) -> tuple[Path, str]:
    """A repository with one commit; its resolved root and HEAD."""
    (path / "src").mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "src" / "a.py").write_bytes(f"# {os.urandom(6).hex()}\n".encode())
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "one")
    return Path(os.path.realpath(path)), _git(path, "rev-parse", "HEAD")


def _spellings(tmp_path: Path, target: Path, *, case: bool = True) -> list[Path]:
    """Other spellings of the existing directory `target`: through a
    symbolic link to its parent, through macOS's ``/var`` where the
    temporary directory lies under ``/private/var``, and, with `case`, with
    its last component's case swapped where the filesystem folds case. A
    spelling this platform cannot make is left out. ``realpath`` resolves
    the links everywhere and the case only on Windows."""
    out: list[Path] = []
    alias = tmp_path / "alias-of-parent"
    try:
        if not alias.exists():
            alias.symlink_to(target.parent, target_is_directory=True)
        out.append(alias / target.name)
    except OSError:
        pass
    text = str(target)
    if text.startswith("/private/var/") and os.path.realpath("/var") == "/private/var":
        out.append(Path(text[len("/private") :]))
    swapped = target.parent / target.name.swapcase()
    if case and swapped.exists() and os.path.samefile(swapped, target):
        out.append(swapped)
    for spelled in out:
        assert str(spelled) != str(target) and os.path.samefile(spelled, target)
    return out


# ---------------------------------------------------------------------------
# githead.locate
# ---------------------------------------------------------------------------


def test_locate_reads_the_repository_where_discovery_declines(tmp_path: Path) -> None:
    root, _head = _repo(tmp_path / "game")
    assert githead.find_gitdir(root) is None, "premise: discovery declines"
    assert githead.gitdir_at(root) is None, "premise: discovery declines"
    expected = GitDir(root / ".git", root / ".git", root)
    assert githead.locate(root / "src") == expected
    assert githead.locate(root, walk=False) == expected
    assert githead.locate(root / "src", walk=False) is None
    assert githead.locate(tmp_path) is None


def test_locate_resolves_another_spelling_to_the_root(tmp_path: Path) -> None:
    root, _head = _repo(tmp_path / "game")
    spellings = _spellings(tmp_path, root)
    if not spellings:
        pytest.skip("no other spelling of a directory can be made here")
    for spelled in spellings:
        found = githead.locate(spelled / "src")
        assert found is not None, spelled
        assert found.worktree_root == Path(os.path.realpath(spelled)), spelled
        assert os.path.samefile(found.worktree_root, root), spelled


def test_locate_reads_a_linked_worktree_through_its_git_file(tmp_path: Path) -> None:
    root, _head = _repo(tmp_path / "game")
    linked = tmp_path / "game-feature"
    _git(root, "worktree", "add", "-q", "-b", "feature", str(linked))
    real = Path(os.path.realpath(linked))
    found = githead.locate(linked / "src")
    assert found is not None
    assert found.worktree_root == real
    assert found.commondir == root / ".git"
    assert found.gitdir == root / ".git" / "worktrees" / "game-feature"


def test_locate_stops_below_a_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _head = _repo(tmp_path / "outer")
    inner = root / "a" / "b"
    inner.mkdir(parents=True)
    assert githead.locate(inner) is not None
    ceilings = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES", os.pathsep.join([ceilings, str(root / "a")])
    )
    assert githead.locate(inner) is None
    assert githead.locate(root / "a") is not None, "a ceiling is no strict ancestor"


# ---------------------------------------------------------------------------
# A cited commit in the caller's store
# ---------------------------------------------------------------------------


def test_holds_commit_reads_the_callers_store(tmp_path: Path) -> None:
    root, head = _repo(tmp_path / "game")
    assert verify.holds_commit(root, head)
    assert verify.holds_commit(str(root), head)
    assert not verify.holds_commit(root, "0" * 40)
    assert not verify.holds_commit(root / "src", head), "the root, not below it"


def test_holds_commit_reads_a_local_clone_whose_source_moved(tmp_path: Path) -> None:
    """`git clone <path>`: the clone's objects are links to or copies of
    the source's, and the source then moves away."""
    home, head = _repo(tmp_path / "Desktop" / "game")
    clone = tmp_path / "clone" / "game"
    clone.parent.mkdir(parents=True)
    _git(clone.parent, "clone", "-q", str(home), str(clone))
    os.rename(home, tmp_path / "Desktop" / "moved")
    assert verify.holds_commit(clone, head)


def test_holds_commit_reads_the_store_under_another_spelling(tmp_path: Path) -> None:
    root, head = _repo(tmp_path / "game")
    spellings = _spellings(tmp_path, root)
    if not spellings:
        pytest.skip("no other spelling of a directory can be made here")
    for spelled in spellings:
        assert verify.holds_commit(spelled, head), spelled
        assert verify.holds_commit(str(spelled), head), spelled


# ---------------------------------------------------------------------------
# A path a sparse cone leaves out (E18 c)
# ---------------------------------------------------------------------------


def _sparse(tmp_path: Path) -> Path:
    """A monorepo's sparse clone holding only ``api``; its resolved root."""
    mono = tmp_path / "mono" / "repo"
    _repo(mono)
    for package in ("api", "web"):
        (mono / package).mkdir()
        (mono / package / "main.py").write_bytes(f"# {package}\n".encode())
    _git(mono, "add", "-A")
    _git(mono, "commit", "-q", "-m", "packages")
    sparse = tmp_path / "sparse" / "repo"
    sparse.parent.mkdir(parents=True)
    _git(sparse.parent, "clone", "-q", "--no-checkout", mono.as_uri(), str(sparse))
    _git(sparse, "sparse-checkout", "set", "api")
    _git(sparse, "checkout", "-q", "main")
    assert not (sparse / "web").exists()
    return Path(os.path.realpath(sparse))


def test_a_path_the_cone_leaves_out_is_absent_by_sparsity(tmp_path: Path) -> None:
    sparse = _sparse(tmp_path)
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert verify._absent_by_sparsity(str(sparse / "web"))
    assert not verify._absent_by_sparsity(str(sparse / "docs" / "main.py"))
    assert not verify._absent_by_sparsity(str(sparse / "api" / "gone.py"))


def test_a_path_the_cone_leaves_out_reads_the_same_under_another_spelling(
    tmp_path: Path,
) -> None:
    sparse = _sparse(tmp_path)
    spellings = _spellings(tmp_path, sparse)
    if not spellings:
        pytest.skip("no other spelling of a directory can be made here")
    for spelled in spellings:
        assert verify._absent_by_sparsity(str(spelled / "web" / "main.py")), spelled
        assert not verify._absent_by_sparsity(str(spelled / "docs" / "x.py")), spelled


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def test_parts_below_compares_resolved_paths(tmp_path: Path) -> None:
    root = tmp_path / "Repo"
    (root / "sub").mkdir(parents=True)
    real = Path(os.path.realpath(root))
    assert verify._parts_below(str(root / "sub" / "x.py"), root) == ["sub", "x.py"]
    assert verify._parts_below(str(root), real) == []
    assert verify._parts_below(str(tmp_path / "elsewhere"), root) is None
    assert verify._parts_below(str(tmp_path), root) is None
    for spelled in _spellings(tmp_path, root, case=False):
        assert verify._parts_below(str(spelled / "sub"), real) == ["sub"], spelled
        assert verify._parts_below(str(real / "sub"), spelled) == ["sub"], spelled


def test_parts_below_folds_case_where_the_platform_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.path.normcase`` on Windows lowercases (and turns ``/`` into
    ``\\``): with it, a root spelled in another case still holds the path,
    and the parts keep the path's own spelling."""
    root = tmp_path / "Repo"
    root.mkdir()
    other = str(tmp_path / "REPO" / "Sub" / "x.py")
    with monkeypatch.context() as patched:
        patched.setattr(os.path, "normcase", str.lower)
        assert verify._parts_below(other, root) == ["Sub", "x.py"]
