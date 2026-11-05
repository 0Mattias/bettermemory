"""A path git prints keeps the blank space and the newlines its name holds.

A folder may be named ``trail ``, ``tab\\t`` or ``nl\\nx``. git prints its
toplevel as it is, with a newline after it, and the origin family took the
line with ``strip``, which took the name's own space or tab off: the
toplevel named another path, no folder membership roots the checkout at,
and the origin kept no root, while `repo_toplevel_and_head`, answering
from the files, named the folder. A file named ``note `` changed in a
commit read as ``note`` in the reachable walk the same way. d1d6aa94 took
the line end alone off (``output_lines``), which split a name holding a
newline in two, and the origin of ``nl\\nx`` kept no root (review H). A
path git prints alone now loses only the newline after it
(`_git.printed_path`), the root before the hash is all before the last
newline, and the walk splits lines only where git quotes every name that
holds one; the security audit's read of a store's working tree with them.
A name ending in a CR keeps it as well, since git's output is decoded from
its bytes (ENG-220's re-review); only on Windows, where no name holds one,
does a CR before git's newline come off.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bettermemory import _git as git_runner
from bettermemory import origin, posture
from bettermemory.store import STORE_FILENAME

from .test_location_gitdir_worktree import REMOTE, _membership, _same
from .test_location_gitdir_worktree import pytestmark as pytestmark
from .test_membership_security import _git, checkout, neutral_git


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, git's walk stopped above
    `tmp_path` (`neutral_git`)."""
    neutral_git(tmp_path, monkeypatch)


#: Windows takes no control character in a name (a tab, a newline, a CR),
#: and drops a space that ends one (Win32 path normalisation): those names
#: are POSIX's.
_POSIX_NAME = pytest.mark.skipif(
    os.name == "nt", reason="a Windows name holds no control character"
)

NAMES = [
    pytest.param("trail ", id="trailing_space", marks=_POSIX_NAME),
    pytest.param("tab\t", id="trailing_tab", marks=_POSIX_NAME),
    pytest.param(" lead", id="leading_space"),
    pytest.param("nl\nx", id="newline", marks=_POSIX_NAME),
    pytest.param("end\n", id="trailing_newline", marks=_POSIX_NAME),
    pytest.param("cr\r", id="trailing_cr", marks=_POSIX_NAME),
]


def _head(where: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=where, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("name", NAMES)
def test_a_folder_named_with_blank_space_keeps_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """git, membership, the origin's capture, `repo_toplevel` and
    `repo_toplevel_and_head` (from the files and from git) name the one
    folder."""
    app = tmp_path / "work" / name
    checkout(app, remote=REMOTE)
    printed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=app, capture_output=True
    ).stdout.decode()
    assert printed.endswith(name + "\n"), repr(printed)

    assert _same(_membership(app).root, app)
    got = origin.capture(app)
    assert got.repo == REMOTE and got.worktree_root == os.path.realpath(app)
    assert origin.repo_toplevel(app) == Path(os.path.realpath(app))
    head = _head(app)
    assert origin.repo_toplevel_and_head(app) == (Path(os.path.realpath(app)), head)
    monkeypatch.setattr(origin, "toplevel_and_head_from_files", lambda cwd: None)
    assert origin.repo_toplevel_and_head(app) == (Path(os.path.realpath(app)), head)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("note ", marks=_POSIX_NAME),
        " note",
        pytest.param("tab\tnote", marks=_POSIX_NAME),
        pytest.param("nl\nnote", marks=_POSIX_NAME),
        pytest.param("cr\rnote", marks=_POSIX_NAME),
    ],
)
def test_a_file_named_with_blank_space_is_touched_as_named(
    tmp_path: Path, name: str
) -> None:
    """The reachable walk reads a changed file's name whole: one a space
    ends or begins (printed as it is), and one holding a tab, a newline or
    a CR (quoted, so on one line, and its escape undone)."""
    app = tmp_path / "work" / "app"
    checkout(app, remote=REMOTE)
    anchor = _head(app)
    (app / name).write_text("changed\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "a file with blank space in its name")
    walk = origin._walk_reachable(Path(os.path.realpath(app)), anchor, _head(app))
    assert walk is not None
    assert set(walk.touched) == {name}, walk.touched


@_POSIX_NAME
@pytest.mark.parametrize("name", ["shared ", "shared\nnl"])
def test_the_audit_reads_a_store_s_working_tree_whole(
    tmp_path: Path, name: str
) -> None:
    """A store inside a working tree whose folder's name ends in a space,
    or holds a newline, is read as inside that tree."""
    repo = tmp_path / name
    store = repo / ".claude-memory"
    store.mkdir(parents=True)
    _git(repo, "init", "-q")
    found = posture.git_finding(
        "store.git", "store", store, store / STORE_FILENAME, ignored_ok=True
    )
    assert os.path.samefile(found.details["worktree"], repo), found


@pytest.mark.parametrize(
    ("stdout", "path"),
    [
        ("/w/trail \n", "/w/trail "),
        ("/w/tab\t\n", "/w/tab\t"),
        ("/w/nl\nx\n", "/w/nl\nx"),
        ("/w/end\n\n", "/w/end\n"),
        ("/w/no-end", "/w/no-end"),
        ("", ""),
    ],
)
def test_only_git_s_newline_comes_off(stdout: str, path: str) -> None:
    """The one newline git ends the path with; never blank space, nor a
    newline the name holds."""
    assert git_runner.printed_path(stdout) == path


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
@pytest.mark.parametrize(
    ("stdout", "posix", "windows_path"),
    [
        ("/w/cr\r\n", "/w/cr\r", "/w/cr"),
        ("/w/cr\r", "/w/cr\r", "/w/cr\r"),
        ("/w/plain\n", "/w/plain", "/w/plain"),
    ],
)
def test_a_cr_before_git_s_newline_comes_off_on_windows_alone(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    posix: str,
    windows_path: str,
    windows: bool,
) -> None:
    """git ends a line with an LF alone, on Windows too, so a CR before it
    is the name's own, which a POSIX name may end in. On Windows, where no
    name holds a CR, one before the LF comes off with it; nowhere else."""
    monkeypatch.setattr(git_runner, "_WINDOWS", windows)
    assert git_runner.printed_path(stdout) == (windows_path if windows else posix)


def test_git_s_output_reaches_a_reader_with_its_cr(tmp_path: Path) -> None:
    """`run_git` decodes git's output from its bytes, every CR kept (it
    read text once, which turned a CR LF into an LF and a name ending in a
    CR into another): a value holding a CR, printed before git's LF,
    reaches the reader as git wrote it. The value is quoted, which every
    git reads its CR from; ``git config x.y`` before 2.43 writes one
    unquoted, which git reads with its CR trimmed (ENG-227)."""
    repo = checkout(tmp_path / "work" / "app")
    with (repo / ".git" / "config").open("ab") as fh:
        fh.write(b'[x]\n\ty = "v\r"\n')
    done = git_runner.run_git(["config", "x.y"], cwd=repo)
    assert done is not None and done.returncode == 0
    assert done.stdout == "v\r\n", repr(done.stdout)
