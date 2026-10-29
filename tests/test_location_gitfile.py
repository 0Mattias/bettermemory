"""A ``.git`` file is followed only where git's own pointers say the folder
is that git directory's working tree (ENG-158).

A ``.git`` file holds ``gitdir: <path>``. Followed whatever it names, one
planted in an extracted archive or in a cloned repository's folder keyed
that folder on another checkout's git directory: the session there
resolved SILENT as that checkout and was delivered its project's
memories. Discovery now follows a linked worktree's pointer when the
worktree's git directory names this file back, a submodule's when its
``core.worktree`` names this folder, a ``--separate-git-dir`` pointer
(git writes none back; design I22), and a pointer whose back-pointer
names nothing any more (a plain ``mv``); any other reads as a ``.git``
file that names nothing. Each is told by the filesystem object, not the
spelling (review M, D1 and G5): a symlink to another tree's ``.git``, the
same ``.git`` in another case on a case-insensitive volume, a forged
worktree directory whose ``commondir`` names another repository, and a
folder holding ``HEAD`` and such a ``commondir`` with no ``.git`` file at
all read as no repository, with birth time and without.

The repositories are real, built with git under ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory import location, location_git
from bettermemory.config import Config, StorageConfig
from bettermemory.location import OUTSIDE, REPO, capture, resolve_caller
from bettermemory.membership import GLOBAL, GLOBAL_OUTCOME
from bettermemory.store import Store

from .test_membership_security import _git, checkout, neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Store, Config]]:
    """A store under `tmp_path` and its config, git set neutral."""
    neutral_git(tmp_path, monkeypatch)
    made = Store(tmp_path / "store")
    yield made, Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    made.close()


def _plant(folder: Path, target: Path | str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    return folder


def _same(a: str | Path | None, b: str | Path) -> bool:
    return a is not None and os.path.samefile(a, b)


def test_a_planted_git_file_naming_a_sibling_repository_yields_no_membership(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """The review's scenario (G6): a folder whose ``.git`` file names
    another working tree's own ``.git`` resolves to no checkout and no
    project, registers no view of that checkout, and is delivered none of
    its records."""
    store, config = lab
    app = checkout(tmp_path / "work" / "app")
    known = resolve_caller(store, config, str(app))
    assert known.resolution.outcome != GLOBAL_OUTCOME
    record = store.write(
        content="the app's own note", scopes=["tools"], home=known.home()
    )
    planted = _plant(tmp_path / "downloads" / "archive", app / ".git")

    caller = resolve_caller(store, config, str(planted))

    res = caller.resolution
    assert (res.outcome, res.project, res.checkout) == (GLOBAL_OUTCOME, GLOBAL, None)
    assert capture(str(planted), write_token=True).state == OUTSIDE
    k = known.model.checkout(str(known.resolution.checkout))
    assert [v.path for v in k.views] == [str(app)]
    assert known.model.admit_recall(res, [record.id]).delivered == ()


PLANTS = ["case", "symlink", "forged_worktree", "head_and_commondir"]


def plant(folder: Path, app: Path, how: str) -> Path:
    """A folder that names `app`'s git directory the way `how` says:
    ``case`` a `.git` file naming ``<app>/.GIT``, ``symlink`` one naming a
    symlink to ``<app>/.git``, ``forged_worktree`` one naming a directory
    of its own that holds ``HEAD``, ``commondir`` naming ``<app>/.git`` and
    a ``gitdir`` naming the file back, ``head_and_commondir`` the folder
    itself holding ``HEAD`` and that ``commondir``, no `.git` file."""
    folder.mkdir(parents=True, exist_ok=True)
    head = (app / ".git" / "HEAD").read_text(encoding="utf-8")
    if how == "case":
        if not (app / ".GIT").exists():
            pytest.skip("a case-sensitive volume: .GIT names nothing here")
        return _plant(folder, app / ".GIT")
    if how == "symlink":
        try:
            (folder / "link").symlink_to(app / ".git", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform makes no symbolic link here")
        return _plant(folder, "link")
    if how == "forged_worktree":
        fake = folder / "fake"
        fake.mkdir()
        (fake / "HEAD").write_text(head, encoding="utf-8")
        (fake / "commondir").write_text(f"{app / '.git'}\n", encoding="utf-8")
        (fake / "gitdir").write_text(f"{folder / '.git'}\n", encoding="utf-8")
        return _plant(folder, "fake")
    (folder / "HEAD").write_text(head, encoding="utf-8")
    (folder / "commondir").write_text(f"{app / '.git'}\n", encoding="utf-8")
    return folder


@pytest.mark.parametrize("birth", [True, False], ids=["birth", "no_birth"])
@pytest.mark.parametrize("how", PLANTS)
def test_another_spelling_of_a_checkouts_git_directory_yields_no_membership(
    tmp_path: Path,
    lab: tuple[Store, Config],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
    birth: bool,
) -> None:
    """Review M's D1 and G5: a case variant, a symlink, a forged worktree
    directory and a folder holding ``HEAD`` and a ``commondir`` each name
    the app's git directory; each resolved silently as the app's checkout
    or joined its project. Each now reads as no repository, records no
    view of the app's checkout and is delivered none of its records, with
    birth time and without."""
    monkeypatch.setattr(location, "REPORT_BIRTH", birth)
    store, config = lab
    app = checkout(tmp_path / "work" / "app")
    known = resolve_caller(store, config, str(app))
    assert known.resolution.outcome != GLOBAL_OUTCOME
    record = store.write(
        content="the app's own note", scopes=["tools"], home=known.home()
    )
    planted = plant(tmp_path / "downloads" / "archive", app, how)

    caller = resolve_caller(store, config, str(planted))

    res = caller.resolution
    assert (res.outcome, res.project, res.checkout) == (GLOBAL_OUTCOME, GLOBAL, None)
    assert capture(str(planted), write_token=True, use_cache=False).state == OUTSIDE
    k = known.model.checkout(str(known.resolution.checkout))
    assert [v.path for v in k.views] == [str(app)]
    assert known.model.admit_recall(res, [record.id]).delivered == ()


@pytest.mark.parametrize("kind", ["worktree", "bare", "submodule"])
def test_a_pointer_at_a_git_directory_that_belongs_elsewhere_is_not_followed(
    tmp_path: Path, lab: tuple[Store, Config], kind: str
) -> None:
    """A ``.git`` file naming a live worktree's git directory, a bare
    repository, or a submodule's git directory whose ``core.worktree``
    names another folder reads as no repository, and says why."""
    app = checkout(tmp_path / "work" / "app")
    if kind == "worktree":
        _git(
            app, "worktree", "add", "-q", "-b", "feat", str(tmp_path / "work" / "feat")
        )
        target = app / ".git" / "worktrees" / "feat"
    elif kind == "bare":
        _git(tmp_path / "work", "clone", "-q", "--bare", str(app), "app.git")
        target = tmp_path / "work" / "app.git"
    else:
        lib = checkout(tmp_path / "work" / "lib")
        _git(
            app,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(lib),
            "deps/lib",
        )
        target = app / ".git" / "modules" / "deps" / "lib"
    planted = _plant(tmp_path / "downloads" / "archive", target)

    cap = capture(str(planted), write_token=False)

    assert cap.state == OUTSIDE
    why = location_git.gitfile_refusal(str(planted), str(planted / ".git"), str(target))
    assert why is not None and str(target) in why


def test_the_pointers_git_writes_are_followed(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A linked worktree (its git directory names this file back), the
    same worktree moved by a plain ``mv`` (the back-pointer names nothing
    until ``git worktree repair``), a submodule (``core.worktree`` names
    this folder) and a ``--separate-git-dir`` tree each read as the
    repository their ``.git`` file names."""
    app = checkout(tmp_path / "work" / "app")
    feat = tmp_path / "work" / "feat"
    _git(app, "worktree", "add", "-q", "-b", "feat", str(feat))
    lib = checkout(tmp_path / "work" / "lib")
    _git(
        app,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(lib),
        "deps/lib",
    )
    sep = tmp_path / "gitdirs" / "tree.git"
    sep.parent.mkdir(parents=True)
    tree = tmp_path / "work" / "tree"
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={sep}", str(tree))

    cases = {
        "worktree": (feat, app / ".git" / "worktrees" / "feat"),
        "submodule": (app / "deps" / "lib", app / ".git" / "modules" / "deps" / "lib"),
        "separate": (tree, sep),
    }
    for name, (where, git_dir) in cases.items():
        cap = capture(str(where), write_token=False, use_cache=False)
        assert cap.state == REPO, name
        assert _same(cap.git_dir, git_dir), name

    moved = tmp_path / "elsewhere" / "feat"
    moved.parent.mkdir()
    feat.rename(moved)
    cap = capture(str(moved), write_token=False, use_cache=False)
    assert cap.state == REPO and _same(cap.git_dir, app / ".git" / "worktrees" / "feat")


def test_a_view_through_a_planted_pointer_is_not_live(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Liveness reads a view's ``.git`` file by the same rule: a view of
    a checkout at a folder whose ``.git`` file names the checkout's own
    ``.git`` from outside (what a resolution there recorded before the
    rule) holds none of its git directory, and reads ``replaced``, while
    the checkout's own view stays live."""
    store, config = lab
    app = checkout(tmp_path / "work" / "app")
    seen = resolve_caller(store, config, str(app))
    assert seen.resolution.checkout is not None
    k = seen.model.checkout(str(seen.resolution.checkout))
    own = k.views_on(seen.model.machine)[-1]
    planted = _plant(tmp_path / "downloads" / "archive", app / ".git")
    through = dataclasses.replace(own, path=str(planted), order=own.order + 1)

    assert location.path_liveness(k, own) == "live"
    assert location.path_liveness(k, through) == "replaced"
