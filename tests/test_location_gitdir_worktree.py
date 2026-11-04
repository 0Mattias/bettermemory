"""A git directory whose config names another working tree (ENG-211).

A folder's own ``.git`` directory was followed with the folder as the
checkout's root, whatever that directory's config said. Where it sets
``core.worktree`` to another folder, git reads that folder as the working
tree: ``git rev-parse --show-toplevel``, and the origin capture, which runs
git, named it, so a record written there carried a membership root and an
origin ``worktree_root`` that disagreed, and the origin anchored its claims
in a tree that was not the checkout's. git writes no pointer back from the
folder it names, so nothing tells a folder of the user's that a planted
config names from the checkout's own, and the shape is refused by both
readers: membership passes over the entry, and the origin family runs no
git where it would read it. A ``core.worktree`` naming the folder itself,
absolute or relative, keeps both on it, and so does a config past the
remotes' bound that names none. The config is read as git reads a
repository's format (tests/test_location_gitdir_format.py).

The repositories are real, built with git under ``tmp_path``; the state
directory is the test's own (``tests/conftest.py``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import gitconfig, githead, location, location_audit, origin

from .test_location_gitdir_pin import _separate
from .test_membership_security import _git, checkout, neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: The checkout's remote, which an origin read through the shape carried.
REMOTE = "https://example.invalid/acme/app.git"


@pytest.fixture(autouse=True)
def git_set_neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed identity, git's walk
    stopped above `tmp_path`."""
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """The folder of every git process the package starts from here on."""
    seen: list[Path | None] = []
    real = git_runner.run_git

    def spy(args: Any, **kwargs: Any) -> Any:
        cwd = kwargs.get("cwd")
        seen.append(None if cwd is None else Path(cwd))
        return real(args, **kwargs)

    monkeypatch.setattr(git_runner, "run_git", spy)
    return seen


def _same(a: str | Path | None, b: str | Path) -> bool:
    return a is not None and os.path.samefile(a, b)


def _inside(path: Path | None, folder: Path) -> bool:
    if path is None:
        return False
    here, there = os.path.realpath(path), os.path.realpath(folder)
    return here == there or here.startswith(there + os.sep)


def _set(git_dir: Path, name: str, value: str) -> None:
    """Set `name` in the config of the git directory `git_dir`, as ``git
    config`` writes it."""
    _git(git_dir.parent, "config", "--file", str(git_dir / "config"), name, value)


def _membership(where: Path) -> location.Capture:
    return location.capture(str(where), write_token=False, use_cache=False)


def _no_origin(where: Path) -> None:
    """The origin family reads `where` as a folder outside any repository,
    determinate: the capture, the toplevel and head, and the files."""
    got = origin.capture(where)
    assert (got.repo, got.branch, got.worktree_root) == (None, None, None), where
    assert got.git_indeterminate is False, where
    assert origin.repo_toplevel_and_head(where) is None
    assert githead.locate(where) is None


#: Where the session stands: the folder holding the ``.git`` directory, a
#: folder below it, and the inside of the git directory itself.
WHERE = ["folder", "below", "inside"]


def _at(app: Path, where: str) -> Path:
    here = {"folder": app, "below": app / "src", "inside": app / ".git"}[where]
    here.mkdir(exist_ok=True)
    return here


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
def test_a_core_worktree_naming_another_folder_is_refused_by_both_readers(
    tmp_path: Path, runs: list[Path | None], spelling: str, where: str
) -> None:
    """The issue's shape: the checkout's own ``.git`` directory names
    another folder (another checkout of the user's here) as its working
    tree, absolutely or relative to the git directory as git takes it. git
    roots the checkout there; membership rooted it at the folder. Both now
    read no repository there: membership passes over the entry, the origin
    is that of a folder outside any repository with no git run, the
    security audit warns and says how to make the config name the folder,
    and the folder named keeps its own checkout in both."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    other = checkout(tmp_path / "work" / "other")
    _set(
        app / ".git",
        "core.worktree",
        str(other) if spelling == "absolute" else "../../other",
    )
    assert _same(_git(app, "rev-parse", "--show-toplevel"), other)
    here = _at(app, where)
    runs.clear()

    cap = _membership(here)
    assert cap.state == location.OUTSIDE and cap.git_dir is None, cap
    _no_origin(here)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []
    found = location_audit.gitfile_posture(str(here))
    assert found["status"] == "warn", found
    assert found["summary"].endswith("(core.worktree)"), found
    assert "remove core.worktree" in found["fix"], found

    assert _same(_membership(other).root, other)
    assert _same(origin.capture(other).worktree_root, other)


@pytest.mark.parametrize("where", ["folder", "inside"])
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
def test_a_core_worktree_naming_the_folder_itself_keeps_both_readers_on_it(
    tmp_path: Path, spelling: str, where: str
) -> None:
    """A ``core.worktree`` naming the folder the ``.git`` directory sits
    in, by its path or as ``..`` from the git directory, is where git roots
    the checkout, from the folder and from inside the git directory: both
    readers root it there, with its remote."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    _set(app / ".git", "core.worktree", str(app) if spelling == "absolute" else "..")
    here = _at(app, where)
    assert _same(_git(here, "rev-parse", "--show-toplevel"), app)

    cap = _membership(here)
    assert cap.state == location.REPO and _same(cap.root, app), cap
    got = origin.capture(here)
    assert got.repo == REMOTE and _same(got.worktree_root, app)


def _worktree_config(git_dir: Path, value: str) -> None:
    """``core.worktree`` set to `value` in `git_dir`'s ``config.worktree``,
    which git reads under ``extensions.worktreeConfig``."""
    _set(git_dir, "extensions.worktreeConfig", "true")
    (git_dir / "config.worktree").write_text(
        f"[core]\n\tworktree = {value}\n", encoding="utf-8"
    )


#: A quoted subsection git ends at its NUL byte, so ``[core "worktree\0"]``
#: sets ``core.worktree`` to git: as written, and with an escape git undoes.
NUL_SPELLINGS = {
    "nul_literal": b'[core "worktree\x00"]\n\tx = {other}\n',
    "nul_escaped": b'[core "worktre\\e\x00"]\n\tx = {other}\n',
}


@pytest.mark.parametrize("how", ["bare", "nothing", "worktree_config", *NUL_SPELLINGS])
def test_a_config_that_moves_the_tree_off_the_folder_is_refused_by_both_readers(
    tmp_path: Path, runs: list[Path | None], how: str
) -> None:
    """The other ways a git directory's config moves git's working tree off
    the folder holding it: ``core.bare`` true (git reads none, and the
    origin was indeterminate), a ``core.worktree`` naming nothing (git dies
    there), one in ``config.worktree`` under ``extensions.worktreeConfig``,
    which git reads after the config, and one set through a NUL byte that
    ends the name, as git ends it. Each is refused by both readers."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    other = tmp_path / "work" / "other"
    other.mkdir()
    if how == "bare":
        _set(app / ".git", "core.bare", "true")
    elif how == "nothing":
        _set(app / ".git", "core.worktree", "../../nowhere")
    elif how == "worktree_config":
        _worktree_config(app / ".git", str(other))
    else:
        config = app / ".git" / "config"
        tail = NUL_SPELLINGS[how].replace(b"{other}", os.fsencode(other))
        config.write_bytes(config.read_bytes() + tail)
    if how not in ("bare", "nothing"):
        assert _same(_git(app, "rev-parse", "--show-toplevel"), other)
    runs.clear()

    for here in (app, _at(app, "below")):
        assert _membership(here).state == location.OUTSIDE, here
        _no_origin(here)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []


def test_a_nul_byte_elsewhere_in_the_config_keeps_both_readers_on_the_folder(
    tmp_path: Path,
) -> None:
    """A NUL byte that hides another name (``[log "follow\\0"]``, which git
    reads as ``log.follow``) moves no working tree: both readers keep the
    checkout at its folder."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    config = app / ".git" / "config"
    config.write_bytes(config.read_bytes() + b'[log "follow\x00"]\n\tx = true\n')
    assert _same(_git(app, "rev-parse", "--show-toplevel"), app)

    cap = _membership(app)
    assert cap.state == location.REPO and _same(cap.root, app), cap
    assert _same(origin.capture(app).worktree_root, app)


def test_a_separate_git_directory_naming_another_tree_in_its_worktree_config_is_refused(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """A ``.git`` file is followed only where the git directory it names
    names this folder as its working tree (ENG-158), and that is read where
    git reads it, ``config.worktree`` too: a ``--separate-git-dir``
    directory whose ``config.worktree`` names another folder was followed
    from the tree, which git roots in that folder."""
    tree, sep = _separate(tmp_path)
    other = tmp_path / "work" / "other"
    other.mkdir()
    _worktree_config(sep, str(other))
    assert _same(_git(tree, "rev-parse", "--show-toplevel"), other)
    runs.clear()

    assert _membership(tree).state == location.OUTSIDE
    _no_origin(tree)
    assert [cwd for cwd in runs if _inside(cwd, tree)] == []


@pytest.mark.parametrize("names", [False, True])
def test_a_config_past_the_remote_bound_is_read_whole(
    tmp_path: Path, names: bool
) -> None:
    """A config grown past the 1 MiB the remotes are read within (a branch
    section per branch) is an ordinary repository's, and git reads a
    ``core.worktree`` from it wherever it stands: it is read whole (within
    `gitconfig._FORMAT_LIMIT`, tests/test_location_gitdir_format.py), so a
    checkout whose config names no working tree stays followed by both
    readers, and one whose config names another folder is refused."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    if names:
        _set(app / ".git", "core.worktree", "../../other")
        (tmp_path / "work" / "other").mkdir()
    config = app / ".git" / "config"
    with config.open("a", encoding="utf-8") as fh:
        fh.writelines(
            f'[branch "b{i}"]\n\tremote = origin\n\tmerge = refs/heads/b{i}\n'
            for i in range(30000)
        )
    assert config.stat().st_size > gitconfig._CONFIG_LIMIT

    cap = _membership(app)
    if names:
        assert cap.state == location.OUTSIDE
        _no_origin(app)
    else:
        assert cap.state == location.REPO and _same(cap.root, app)
        assert _same(origin.capture(app).worktree_root, app)
