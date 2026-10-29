"""verify's reads of a repository's own files where githead's discovery
declines, and under other spellings of the same paths (design 3.6, E18 c).

`verify_anchoring.holds_commit` (a cited commit in the caller's store) and
`verify_anchoring._absent_by_sparsity` (a path a sparse cone leaves out) find
the repository through `githead.locate`. `githead.find_gitdir` and
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
from typing import Any

import pytest

from bettermemory import githead, verify, verify_anchoring
from bettermemory.githead import GitDir
from bettermemory.gitobjects import ObjectReader

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


def _sparse(tmp_path: Path, *options: str) -> Path:
    """A monorepo's sparse clone holding only ``api``, its sparse checkout
    set with `options`; its resolved root."""
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
    _git(sparse, "sparse-checkout", "set", *options, "api")
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


def test_a_tracked_path_the_cone_keeps_and_the_disk_lost_reads_missing(
    tmp_path: Path,
) -> None:
    """A path the sparse patterns keep, deleted from the disk, is a
    deletion: ``git status`` reports it, since its index entry carries no
    skip-worktree bit. The probe read whether HEAD tracks the path, which
    it does, so the deletion read as a path the cone leaves out and the
    anchored read reported it unconfirmed, not missing (ENG-169 G1)."""
    sparse = _sparse(tmp_path)
    (sparse / "api" / "main.py").unlink()
    assert _git(sparse, "status", "--porcelain") == "D api/main.py", "premise"
    assert not verify._absent_by_sparsity(str(sparse / "api" / "main.py"))
    shutil.rmtree(sparse / "api")
    assert not verify._absent_by_sparsity(str(sparse / "api"))
    assert not verify._absent_by_sparsity(str(sparse / "api" / "main.py"))
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))


@pytest.mark.parametrize("layout", ["full", "sparse-index", "version-4"])
def test_the_skip_worktree_bit_reads_in_every_index_layout_git_writes(
    tmp_path: Path, layout: str
) -> None:
    """The index git writes for a sparse checkout (version 3, the bit in
    each entry's second flags word), a sparse index (one directory entry,
    ``web/``, for everything the cone leaves out of ``web``) and a version
    4 index (each name compressed against the one before it) read alike."""
    options = ("--sparse-index",) if layout == "sparse-index" else ()
    sparse = _sparse(tmp_path, *options)
    if layout == "version-4":
        _git(sparse, "update-index", "--index-version", "4")
    if layout == "sparse-index":
        listed = _git(sparse, "ls-files", "--sparse").splitlines()
        assert "web/" in listed and "web/main.py" not in listed, "premise"
    (sparse / "api" / "main.py").unlink()
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert verify._absent_by_sparsity(str(sparse / "web"))
    assert not verify._absent_by_sparsity(str(sparse / "api" / "main.py"))
    assert not verify._absent_by_sparsity(str(sparse / "docs" / "main.py"))


def test_a_sha256_repository_s_index_reads_the_bit(tmp_path: Path) -> None:
    """A SHA-256 repository's index entries hold 32-byte object names, so
    each entry's flags sit twelve bytes further on; its config's
    ``extensions.objectFormat`` says so."""
    repo = tmp_path / "repo"
    repo.mkdir()
    made = subprocess.run(
        ["git", "init", "-q", "-b", "main", "--object-format=sha256"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip("this git cannot make a SHA-256 repository")
    for package in ("api", "web"):
        (repo / package).mkdir()
        (repo / package / "main.py").write_bytes(f"# {package}\n".encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "packages")
    _git(repo, "sparse-checkout", "set", "api")
    assert not (repo / "web").exists(), "premise"
    root = Path(os.path.realpath(repo))
    (root / "api" / "main.py").unlink()
    assert verify._absent_by_sparsity(str(root / "web" / "main.py"))
    assert not verify._absent_by_sparsity(str(root / "api" / "main.py"))


def test_a_split_index_is_not_read_and_convicts_no_absent_path(
    tmp_path: Path,
) -> None:
    """A split index holds only the entries that differ from a shared
    index the probe does not open; it declines, and every absent path in
    the sparse checkout reads as one the probe cannot judge (True, read
    unconfirmed), the one the cone leaves out and the deleted one alike:
    an unread index convicts nothing."""
    sparse = _sparse(tmp_path)
    _git(sparse, "update-index", "--split-index")
    assert any(p.name.startswith("sharedindex.") for p in (sparse / ".git").iterdir())
    (sparse / "api" / "main.py").unlink()
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert verify._absent_by_sparsity(str(sparse / "api" / "main.py"))


def test_absent_paths_read_the_index_once_and_no_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe built a new object reader per absent path, reading every
    pack index of the repository whole each time (ENG-169 G1). The index
    is parsed once while its stamp holds, and again once git rewrites it."""
    sparse = _sparse(tmp_path)
    readers: list[object] = []

    def reader(*args: Any) -> object:
        readers.append(args)
        return ObjectReader(*args)

    parses: list[int] = []
    real_parse = getattr(verify_anchoring, "_parse_skip_index", None)

    def parse(data: bytes, hash_len: int) -> object:
        parses.append(len(data))
        assert real_parse is not None
        return real_parse(data, hash_len)

    monkeypatch.setattr(verify_anchoring, "ObjectReader", reader)
    monkeypatch.setattr(verify_anchoring, "_parse_skip_index", parse, raising=False)
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert verify._absent_by_sparsity(str(sparse / "web"))
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert readers == []
    assert len(parses) == 1
    _git(sparse, "update-index", "--index-version", "4")
    assert verify._absent_by_sparsity(str(sparse / "web" / "main.py"))
    assert len(parses) == 2


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
