"""The security audit names a ``.git`` file membership does not follow
(ENG-158).

Discovery reads a ``.git`` file that names another checkout's git
directory as naming nothing, so membership delivers that checkout's
memories nowhere else; git itself still follows the file, and a git
command in that folder acts on the other repository. `bettermemory
security audit` now says so for the working directory and its parents
(``membership.gitfile``: ``warn``), says when the repository found is a
``--separate-git-dir`` pointer that carries no pointer back (``info``:
followed as git follows it, a planted one too), and is ``ok`` otherwise.
Each spelling review M found membership following (a case variant, a
symlink, a forged worktree directory, a folder holding ``HEAD`` and a
``commondir``) warns, and the warning's fix is a check by hand that
suggests no command rewriting a worktree's registration (review M D4).

The repositories are real, built with git under ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location, location_git, posture

from .test_location_gitfile import PLANTS, _plant, plant
from .test_membership_security import _git, checkout, neutral_git
from .test_posture import Layout, layout  # noqa: F401  (the fixture, shared)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_ID = "membership.gitfile"


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)


def _finding(cwd: Path) -> dict[str, Any]:
    found: dict[str, Any] = location_git.gitfile_posture(str(cwd))
    return found


def test_the_audit_warns_of_a_planted_git_file(
    tmp_path: Path,
    layout: Layout,  # noqa: F811
) -> None:
    """A folder whose ``.git`` file names a sibling repository's own
    ``.git`` warns, naming the file and the working tree it belongs to;
    the warning holds from a folder below it, and the audit carries it."""
    app = checkout(tmp_path / "work" / "app")
    planted = _plant(tmp_path / "downloads" / "archive", app / ".git")
    below = planted / "src"
    below.mkdir()

    for where in (planted, below):
        found = _finding(where)
        assert found["status"] == "warn", found
        assert os.path.samefile(found["path"], planted / ".git")
        (refused,) = found["details"]["refused"]
        assert os.path.samefile(refused["target"], app / ".git")
        assert str(app) in found["summary"] or os.path.realpath(app) in found["summary"]

    report = posture.audit(cwd=planted)
    (finding,) = [f for f in report.findings if f.id == _ID]
    assert finding.status == "warn"
    assert report.status in ("warn", "high")


def test_a_repository_or_worktree_of_its_own_is_ok(tmp_path: Path) -> None:
    """A repository's own ``.git`` directory, a linked worktree whose git
    directory names its ``.git`` file back, and a folder outside any
    repository are ``ok``."""
    app = checkout(tmp_path / "work" / "app")
    feat = tmp_path / "work" / "feat"
    _git(app, "worktree", "add", "-q", "-b", "feat", str(feat))
    outside = tmp_path / "plain"
    outside.mkdir()
    for where in (app, feat, outside):
        assert _finding(where)["status"] == "ok", where


def test_a_separate_git_dir_pointer_is_named_as_followed(tmp_path: Path) -> None:
    """A ``--separate-git-dir`` checkout's ``.git`` file is followed with no
    pointer back to check, so the audit names it (``info``), with the git
    directory it names."""
    tree, sep = tmp_path / "tree", tmp_path / "sep"
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={sep}", str(tree))
    found = _finding(tree)
    assert found["status"] == "info", found
    assert os.path.samefile(found["details"]["target"], sep)


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
@pytest.mark.parametrize("how", PLANTS)
def test_the_audit_warns_of_each_spelling_of_another_checkouts_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, birth: bool
) -> None:
    """Review M D1 and G5: a case variant, a symlink, a forged worktree
    directory and a folder holding ``HEAD`` and a ``commondir`` each name
    the app's git directory. Each was followed (and read ``info`` as a
    ``--separate-git-dir`` pointer, or ``ok``); each now warns, naming the
    plant, with birth time and without."""
    monkeypatch.setattr(location, "REPORT_BIRTH", birth)
    app = checkout(tmp_path / "work" / "app")
    planted = plant(tmp_path / "downloads" / "archive", app, how)

    found = _finding(planted)

    assert found["status"] == "warn", found
    named = planted if how == "head_and_commondir" else planted / ".git"
    assert os.path.samefile(found["path"], named)
    assert "worktrees" in found["fix"] and "remove" in found["fix"]


def test_the_warnings_fix_is_a_check_by_hand(tmp_path: Path) -> None:
    """Review M D4: the fix named `git worktree repair`, which run in a copy
    of a live worktree moves the live worktree's registration to the copy.
    It now names no command that rewrites a registration: it says how to
    check where the file points by hand, and to remove a plant."""
    app = checkout(tmp_path / "work" / "app")
    feat = tmp_path / "work" / "feat"
    _git(app, "worktree", "add", "-q", "-b", "feat", str(feat))
    copy = tmp_path / "downloads" / "feat"
    shutil.copytree(feat, copy, symlinks=True)

    found = _finding(copy)

    assert found["status"] == "warn", found
    fix = found["fix"]
    assert "repair" not in fix and "git worktree" not in fix
    assert "<common>/worktrees/<name>" in fix
    assert "remove its .git file" in fix
    registered = (app / ".git" / "worktrees" / "feat" / "gitdir").read_text()
    assert os.path.samefile(registered.strip(), feat / ".git")
    assert _finding(feat)["status"] == "ok"
