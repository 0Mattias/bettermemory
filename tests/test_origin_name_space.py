"""A path git prints keeps the blank space its name ends or begins with.

A folder may be named ``trail `` or ``tab\\t``. git prints its toplevel as
it is, with a newline after it, and the origin family took the line with
``strip``, which took the name's own space or tab off: the toplevel named
another path, no folder membership roots the checkout at, and the origin
kept no root, while `repo_toplevel_and_head`, answering from the files,
named the folder. A file named ``note `` changed in a commit read as
``note`` in the reachable walk the same way. Each reader now takes off
the line end alone (`_git.output_lines`), and the security audit's read of
a store's working tree with it.
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


NAMES = {"trailing_space": "trail ", "trailing_tab": "tab\t", "leading_space": " lead"}


def _head(where: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=where, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("name", list(NAMES.values()), ids=list(NAMES))
def test_a_folder_named_with_blank_space_keeps_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """git, membership, the origin's capture, `repo_toplevel` and
    `repo_toplevel_and_head` (from the files and from git) name the one
    folder."""
    app = tmp_path / "work" / name
    checkout(app, remote=REMOTE)
    printed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=app, capture_output=True, text=True
    ).stdout
    assert printed.endswith(name + "\n"), repr(printed)

    assert _same(_membership(app).root, app)
    got = origin.capture(app)
    assert got.repo == REMOTE and got.worktree_root == os.path.realpath(app)
    assert origin.repo_toplevel(app) == Path(os.path.realpath(app))
    head = _head(app)
    assert origin.repo_toplevel_and_head(app) == (Path(os.path.realpath(app)), head)
    monkeypatch.setattr(origin, "toplevel_and_head_from_files", lambda cwd: None)
    assert origin.repo_toplevel_and_head(app) == (Path(os.path.realpath(app)), head)


@pytest.mark.parametrize("name", ["note ", " note", "tab\tnote"])
def test_a_file_named_with_blank_space_is_touched_as_named(
    tmp_path: Path, name: str
) -> None:
    """The reachable walk reads a changed file's name whole: one a space
    ends or begins (printed as it is) and one holding a tab (quoted)."""
    app = tmp_path / "work" / "app"
    checkout(app, remote=REMOTE)
    anchor = _head(app)
    (app / name).write_text("changed\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "a file with blank space in its name")
    walk = origin._walk_reachable(Path(os.path.realpath(app)), anchor, _head(app))
    assert walk is not None
    assert set(walk.touched) == {name}, walk.touched


def test_the_audit_reads_a_store_s_working_tree_whole(tmp_path: Path) -> None:
    """A store inside a working tree whose folder's name ends in a space is
    read as inside that tree."""
    repo = tmp_path / "shared "
    store = repo / ".claude-memory"
    store.mkdir(parents=True)
    _git(repo, "init", "-q")
    found = posture.git_finding(
        "store.git", "store", store, store / STORE_FILENAME, ignored_ok=True
    )
    assert os.path.samefile(found.details["worktree"], repo), found


@pytest.mark.parametrize(
    ("stdout", "cr_ends", "lines"),
    [
        ("/w/trail \n", False, ["/w/trail "]),
        ("/w/tab\t\n", False, ["/w/tab\t"]),
        ("/w/a\r\n", False, ["/w/a\r"]),
        ("/w/a\r\n", True, ["/w/a"]),
        ("/w/a \r\n", True, ["/w/a "]),
        ("/w/a\n0123\n", False, ["/w/a", "0123"]),
        ("", False, []),
        ("/w/no-end", False, ["/w/no-end"]),
    ],
)
def test_only_the_line_end_comes_off(
    stdout: str, cr_ends: bool, lines: list[str]
) -> None:
    """The LF, and on Windows a CR before it (where no name holds one);
    elsewhere a CR is the name's own."""
    assert git_runner.output_lines(stdout, cr_ends=cr_ends) == lines
