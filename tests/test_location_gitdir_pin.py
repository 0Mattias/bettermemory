"""A ``.git`` file naming a ``--separate-git-dir`` directory is followed
only from the working tree recorded for it (ENG-206).

git keeps no pointer back for a git directory kept apart with
``--separate-git-dir``: its config names no ``core.worktree`` and nothing
in it names the tree. Until 9.0.0rc18 any ``.git`` file naming one was
followed, so a folder an attacker controls, or an extracted archive,
holding a ``.git`` file that named the separate git directory of one of
the user's checkouts resolved as that checkout and was delivered its
memories. bettermemory now keeps the pointer git does not
(`gitdir_pin`, in the state directory): the first capture that follows
the file records it, and a ``.git`` file elsewhere naming the same git
directory reads as no repository while the recorded one still names
it, however the plant spells the git directory. A recorded tree moved
with a plain ``mv``, removed, or pointed at another git directory gives
way to the next tree that follows the git directory.

The repositories are real, built with git under ``tmp_path``; the state
directory is the test's own (``tests/conftest.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import gitdir_pin, location, location_audit, location_git
from bettermemory.config import Config, StorageConfig
from bettermemory.location import OUTSIDE, REPO, capture, resolve_caller
from bettermemory.membership import GLOBAL_OUTCOME
from bettermemory.store import Store

from .test_membership_security import _git, neutral_git

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


def _separate(tmp_path: Path, name: str = "app") -> tuple[Path, Path]:
    """A ``--separate-git-dir`` checkout with one commit: (tree, git dir)."""
    sep = tmp_path / "gitdirs" / f"{name}.git"
    sep.parent.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "work" / name
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={sep}", str(tree))
    (tree / "main.py").write_text("print(1)\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "one")
    return tree, sep


def _plant(folder: Path, target: Path | str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    return folder


def _see(where: Path, *, write_token: bool = True) -> location.Capture:
    return capture(str(where), write_token=write_token, use_cache=False)


def _same(a: str | Path | None, b: str | Path) -> bool:
    return a is not None and os.path.samefile(a, b)


def _finding(cwd: Path) -> dict[str, Any]:
    found: dict[str, Any] = location_audit.gitfile_posture(str(cwd))
    return found


def test_a_planted_git_file_naming_a_separate_git_dir_yields_no_membership(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """The issue's scenario: a folder whose ``.git`` file names the
    separate git directory of a checkout already seen resolves to no
    checkout and no project and is delivered none of its records, while
    the checkout itself still resolves."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    assert known.resolution.outcome != GLOBAL_OUTCOME
    assert known.resolution.checkout is not None
    record = store.write(
        content="the app's own note", scopes=["tools"], home=known.home()
    )
    planted = _plant(tmp_path / "downloads" / "archive", sep)

    cap = _see(planted)
    assert cap.state == OUTSIDE, cap
    there = resolve_caller(store, config, str(planted))
    assert there.resolution.checkout is None
    assert there.resolution.outcome == GLOBAL_OUTCOME
    assert there.resolution.project != known.resolution.project
    k = there.model.checkout(str(known.resolution.checkout))
    assert not any(_same(v.path, planted) for v in k.views_on(there.model.machine))
    assert there.model.admit_recall(there.resolution, [record.id]).delivered == ()

    again = resolve_caller(store, config, str(tree))
    assert again.resolution.checkout == known.resolution.checkout
    admitted = again.model.admit_recall(again.resolution, [record.id])
    assert admitted.delivered == (record.id,)


def test_the_first_capture_records_the_tree_whatever_the_token_policy(
    tmp_path: Path,
) -> None:
    """The first capture that follows the ``.git`` file records it in the
    state directory, also where the token is not written
    (``membership.write_token = false``); the git directory gains nothing
    beyond what git put there, and a second capture writes nothing."""
    tree, sep = _separate(tmp_path)
    before = sorted(p.relative_to(sep) for p in sep.rglob("*"))
    assert gitdir_pin.recorded(str(sep)) is None

    cap = _see(tree, write_token=False)
    assert cap.state == REPO and _same(cap.git_dir, sep)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)
    assert _same(pin.git_dir, sep)
    assert pin.file.parent == gitdir_pin.pins_dir()
    assert sorted(p.relative_to(sep) for p in sep.rglob("*")) == before
    stamp = pin.file.stat().st_mtime_ns

    _see(tree, write_token=False)
    assert gitdir_pin.recorded(str(sep)) == pin
    assert pin.file.stat().st_mtime_ns == stamp
    if sys.platform != "win32":
        assert pin.file.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("spelling", ["path", "symlink", "relative", "case"])
def test_a_plant_is_refused_however_it_spells_the_git_directory(
    tmp_path: Path, spelling: str
) -> None:
    """A record is matched by the git directory's filesystem object, so a
    plant naming it by its path, through a symlink, relative to itself, or
    in another case on a case-insensitive volume reads as no repository,
    and its refusal names the recorded tree."""
    tree, sep = _separate(tmp_path)
    assert _see(tree).state == REPO
    folder = tmp_path / "downloads" / "archive"
    folder.mkdir(parents=True)
    if spelling == "path":
        target: Path | str = sep
    elif spelling == "symlink":
        link = tmp_path / "elsewhere"
        try:
            link.symlink_to(sep.parent, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"no symlinks here: {exc}")
        target = link / sep.name
    elif spelling == "relative":
        target = os.path.relpath(sep, folder)
    else:
        target = str(sep).swapcase()
        if not os.path.exists(target):
            pytest.skip("the volume does not fold case")
    planted = _plant(folder, target)

    assert _see(planted).state == OUTSIDE
    why = location_git.gitfile_refusal(
        str(planted),
        str(planted / ".git"),
        location_git._gitfile_target(str(planted / ".git")) or "",
    )
    assert why is not None and "working tree" in why
    assert os.path.realpath(tree) in why or str(tree) in why
    found = _finding(planted)
    assert found["status"] == "warn", found
    assert "--separate-git-dir" in found["fix"]


def test_a_tree_moved_with_mv_is_followed_and_recorded_again(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """The recorded ``.git`` file gone (the tree moved with a plain
    ``mv``), the tree at its new path is followed as the same checkout and
    recorded there; a plant made before the move and seen after it is
    still refused, now naming the new path."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    moved = tmp_path / "moved" / "app"
    moved.parent.mkdir()
    tree.rename(moved)

    again = resolve_caller(store, config, str(moved))
    assert again.resolution.checkout == known.resolution.checkout
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, moved)

    assert _see(planted).state == OUTSIDE
    found = _finding(planted)
    assert found["status"] == "warn" and "moved" in found["summary"], found


def test_a_recorded_tree_that_names_another_git_directory_gives_way(
    tmp_path: Path,
) -> None:
    """The recorded tree's ``.git`` file now names another git directory
    (the folder made a checkout of something else), so the record names
    no tree of this one, and the next tree that follows it is recorded."""
    tree, sep = _separate(tmp_path)
    assert _see(tree).state == REPO
    other_tree, other = _separate(tmp_path, "other")
    (tree / ".git").write_text(f"gitdir: {other}\n", encoding="utf-8")
    second = _plant(tmp_path / "work" / "second", sep)

    cap = _see(second)
    assert cap.state == REPO and _same(cap.git_dir, sep)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, second)
    assert _same(_see(other_tree).git_dir, other)


def test_a_view_at_a_second_tree_holds_none_of_the_git_directory(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Liveness reads a view's ``.git`` file by the same rule: a copy of
    the recorded tree names the git directory but is not its tree, so the
    witness exchange finds no git directory there."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    resolve_caller(store, config, str(tree))
    second = tmp_path / "work" / "copy"
    shutil.copytree(tree, second)

    assert _same(location._git_dir_at(str(tree), location.PRIMARY), sep)
    assert location._git_dir_at(str(second), location.PRIMARY) is None


def test_the_audit_names_whether_a_tree_is_recorded(tmp_path: Path) -> None:
    """Before any capture the audit says no tree is recorded (``info``,
    the first folder seen becomes it); after one it is ``ok``, naming the
    record."""
    tree, sep = _separate(tmp_path)
    found = _finding(tree)
    assert found["status"] == "info", found
    assert os.path.samefile(found["details"]["target"], sep)
    assert found["details"]["recorded"] is None

    _see(tree)
    found = _finding(tree)
    assert found["status"] == "ok", found
    assert os.path.samefile(found["details"]["recorded"], tree)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX modes; root writes past them",
)
def test_a_state_directory_that_refuses_the_write_leaves_the_follow_as_it_was(
    tmp_path: Path,
) -> None:
    """Where the state directory refuses the record, the tree is followed
    as before and the audit warns that no tree can be recorded; the next
    capture once the directory takes writes records it."""
    tree, sep = _separate(tmp_path)
    root = gitdir_pin.pins_dir()
    root.mkdir(parents=True)
    root.chmod(0o500)
    try:
        assert _see(tree).state == REPO
        assert gitdir_pin.recorded(str(sep)) is None
        found = _finding(tree)
        assert found["status"] == "warn" and "cannot be written" in found["summary"]
    finally:
        root.chmod(0o700)
    _see(tree)
    assert gitdir_pin.recorded(str(sep)) is not None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        json.dumps({"format": 2, "git_dir": "/a", "tree": "/b"}).encode(),
        json.dumps({"format": 1, "git_dir": "a", "tree": "/b"}).encode(),
        json.dumps({"format": 1, "git_dir": "/a", "tree": 7}).encode(),
        b"{" + b" " * (1 << 16) + b"}",
    ],
    ids=["text", "bytes", "list", "format", "relative", "type", "oversize"],
)
def test_a_file_that_is_not_a_record_is_passed_over(
    tmp_path: Path, body: bytes
) -> None:
    """A file at a record's name that is not a record is read as none,
    and the next capture replaces it with the record."""
    tree, sep = _separate(tmp_path)
    root = gitdir_pin.pins_dir()
    root.mkdir(parents=True)
    (root / gitdir_pin._name(os.stat(sep))).write_bytes(body)
    assert gitdir_pin.recorded(str(sep)) is None
    _see(tree)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)


def _forget_records() -> None:
    """The records' folder as a store from before 9.0.0rc18 left it: no
    record, and no capture of any path cached."""
    shutil.rmtree(gitdir_pin.pins_dir(), ignore_errors=True)
    location._clear_cache()


def test_a_checkout_seen_before_the_records_keeps_the_tree_it_was_seen_at(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A checkout the store saw before any record was kept (an upgrade
    from 9.0.0rc17): a plant seen first after the upgrade is not recorded
    as the tree, since the store's earliest view of the checkout is
    another folder whose ``.git`` file still names its git directory; the
    plant resolves to nothing and the checkout's tree keeps resolving."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    _forget_records()

    there = resolve_caller(store, config, str(planted))
    assert there.resolution.checkout is None
    assert there.resolution.outcome == GLOBAL_OUTCOME
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)
    again = resolve_caller(store, config, str(tree))
    assert again.resolution.checkout == known.resolution.checkout


def test_the_earliest_view_decides_between_two_trees_seen_before_the_records(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two folders naming the git directory were both seen before any
    record was kept, the tree and then a plant (which 9.0.0rc17 followed,
    recording a view of the checkout there): whichever is seen first
    after the upgrade, the record names the one the store saw first."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    with monkeypatch.context() as before:
        # 9.0.0rc17's rule: no record is read, none written.
        before.setattr(location_git, "_recorded_refusal", lambda *a: None)
        before.setattr(location, "record_working_tree", lambda *a: "kept")
        _forget_records()
        through = resolve_caller(store, config, str(planted))
    assert through.resolution.checkout == known.resolution.checkout

    for first in (planted, tree):
        _forget_records()
        resolve_caller(store, config, str(first))
        pin = gitdir_pin.recorded(str(sep))
        assert pin is not None and _same(pin.tree, tree), first
        assert _see(planted).state == OUTSIDE
        assert _see(tree).state == REPO


def _link(link: Path, to: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(to, target_is_directory=to.is_dir())
    except OSError as exc:
        pytest.skip(f"no symlinks here: {exc}")


@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_a_link_to_the_recorded_trees_git_file_is_refused(
    tmp_path: Path, lab: tuple[Store, Config], how: str
) -> None:
    """Review finding 1: a plant whose ``.git`` is a symbolic or a hard
    link to the recorded tree's own ``.git`` file names the git directory
    through the right file but from another folder. It reads as no
    repository, the record keeps naming the tree, and the audit warns."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    planted = tmp_path / "downloads" / "archive"
    if how == "symlink":
        _link(planted / ".git", tree / ".git")
    else:
        planted.mkdir(parents=True)
        try:
            os.link(tree / ".git", planted / ".git")
        except OSError as exc:
            pytest.skip(f"no hard links here: {exc}")

    there = resolve_caller(store, config, str(planted))
    assert there.resolution.checkout is None
    assert _see(planted).state == OUTSIDE
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)
    assert _finding(planted)["status"] == "warn"
    assert resolve_caller(store, config, str(tree)).resolution.checkout == (
        known.resolution.checkout
    )


def test_a_git_directory_reached_through_a_symlinked_git_is_judged_as_a_pointer(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Review finding 1, the older half: a ``.git`` that is a symbolic
    link to another working tree's own ``.git`` directory, to a linked
    worktree's ``.git`` file, or to a recorded separate git directory,
    was followed as the folder's own repository. Each now reads as no
    repository; a ``.git`` link to a git directory nothing else claims is
    still followed, and recorded as that folder's tree."""
    store, config = lab
    app = tmp_path / "work" / "app"
    app.mkdir(parents=True)
    _git(app, "init", "-q", "-b", "main")
    _git(app, "commit", "-q", "--allow-empty", "-m", "one")
    feat = tmp_path / "work" / "feat"
    _git(app, "worktree", "add", "-q", "-b", "feat", str(feat))
    tree, sep = _separate(tmp_path)
    assert _see(tree).state == REPO
    for name, to in (
        ("own", app / ".git"),
        ("worktree", feat / ".git"),
        ("separate", sep),
    ):
        planted = tmp_path / "downloads" / name
        _link(planted / ".git", to)
        assert _see(planted).state == OUTSIDE, name
        assert _finding(planted)["status"] == "warn", name
        assert resolve_caller(store, config, str(planted)).resolution.checkout is None

    vault = tmp_path / "vault" / "kept.git"
    vault.parent.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", "--bare", str(vault))
    _git(vault, "config", "core.bare", "false")
    mine = tmp_path / "work" / "mine"
    _link(mine / ".git", vault)
    cap = _see(mine)
    assert cap.state == REPO and _same(cap.git_dir, vault)
    pin = gitdir_pin.recorded(str(vault))
    assert pin is not None and _same(pin.tree, mine)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX modes; root reads past them",
)
def test_a_recorded_git_that_cannot_be_read_refuses_and_keeps_the_record(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Review finding 2: where the recorded tree's ``.git`` cannot be
    read, what it names cannot be told, so a plant is refused and the
    record is not handed over; once it reads again, the tree resolves as
    before."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    known = resolve_caller(store, config, str(tree))
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    (tree / ".git").chmod(0)
    try:
        assert _see(planted).state == OUTSIDE
        found = _finding(planted)
        assert found["status"] == "warn" and "could not be read" in found["summary"]
        pin = gitdir_pin.recorded(str(sep))
        assert pin is not None and _same(pin.tree, tree)
    finally:
        (tree / ".git").chmod(0o644)
    location._clear_cache()
    again = resolve_caller(store, config, str(tree))
    assert again.resolution.checkout == known.resolution.checkout


def test_what_git_keeps_a_pointer_for_is_not_recorded(tmp_path: Path) -> None:
    """A linked worktree of a separate git directory (its own git
    directory under ``worktrees/``, with a pointer back) and an absorbed
    submodule (``core.worktree``) are not kept apart and leave no record;
    a ``git clone --separate-git-dir`` checkout is recorded like an
    ``init`` one, and the separate tree stays followed beside its
    worktree."""
    tree, sep = _separate(tmp_path)
    wt = tmp_path / "work" / "wt"
    _git(tree, "worktree", "add", "-q", "-b", "wt", str(wt))
    cap = _see(wt)
    assert cap.state == REPO and cap.kind == location.LINKED and not cap.kept_apart
    assert gitdir_pin.recorded(cap.git_dir or "") is None
    assert _see(tree).state == REPO

    lib = tmp_path / "work" / "lib"
    lib.mkdir()
    _git(lib, "init", "-q", "-b", "main")
    _git(lib, "commit", "-q", "--allow-empty", "-m", "lib")
    _git(
        tree,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(lib),
        "deps/lib",
    )
    sub = _see(tree / "deps" / "lib")
    assert sub.state == REPO and not sub.kept_apart and not sub.tree_recorded
    assert gitdir_pin.recorded(sub.git_dir or "") is None

    sep2 = tmp_path / "gitdirs" / "clone.git"
    cloned = tmp_path / "work" / "clone"
    _git(tmp_path, "clone", "-q", f"--separate-git-dir={sep2}", str(tree), str(cloned))
    cap = _see(cloned)
    assert cap.state == REPO and cap.kept_apart and cap.tree_recorded
    pin = gitdir_pin.recorded(str(sep2))
    assert pin is not None and _same(pin.tree, cloned)


def test_a_capture_that_records_nothing_leaves_no_record(tmp_path: Path) -> None:
    """The migration's probe reads only (``record_tree=False``): it
    follows the separate git directory and records nothing."""
    tree, sep = _separate(tmp_path)
    cap = capture(str(tree), write_token=False, use_cache=False, record_tree=False)
    assert cap.state == REPO and cap.kept_apart and not cap.tree_recorded
    assert gitdir_pin.recorded(str(sep)) is None


def test_of_two_first_records_made_at_once_one_stands(tmp_path: Path) -> None:
    """A first record is created only where none is: of two made at once
    for one git directory, exactly one is written, and the record names
    that one's folder."""
    import threading

    _tree, sep = _separate(tmp_path)
    folders = [tmp_path / "work" / f"t{i}" for i in range(2)]
    for folder in folders:
        folder.mkdir()
    barrier = threading.Barrier(2)
    wrote: dict[int, bool] = {}

    def first(i: int) -> None:
        barrier.wait()
        wrote[i] = gitdir_pin.put(str(sep), str(folders[i]))

    threads = [threading.Thread(target=first, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wrote.values()) == [False, True]
    (winner,) = [i for i, ok in wrote.items() if ok]
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, folders[winner])


def test_a_git_moved_out_to_a_folder_of_its_own_is_followed_and_recorded(
    tmp_path: Path,
) -> None:
    """Re-review finding 1: a repository whose ``.git`` was moved into a
    folder that holds nothing else (out of a synced folder, say), with a
    link left in its place, is followed as before, as a git directory kept
    apart: its folder is recorded, and a plant linking to the same
    ``.git`` from another folder is refused."""
    holder = tmp_path / "gitstore" / "app"
    holder.mkdir(parents=True)
    _git(holder, "init", "-q", "-b", "main")
    _git(holder, "commit", "-q", "--allow-empty", "-m", "one")
    tree = tmp_path / "work" / "app"
    _link(tree / ".git", holder / ".git")
    (tree / "main.py").write_text("print(1)\n", encoding="utf-8")

    cap = _see(tree)
    assert cap.state == REPO and cap.kept_apart and _same(cap.git_dir, holder / ".git")
    pin = gitdir_pin.recorded(str(holder / ".git"))
    assert pin is not None and _same(pin.tree, tree)
    planted = tmp_path / "downloads" / "archive"
    _link(planted / ".git", holder / ".git")
    assert _see(planted).state == OUTSIDE
    assert _see(tree).state == REPO


def test_a_link_plant_the_store_saw_first_is_not_recorded_as_the_tree(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review finding 2: a plant whose ``.git`` links to the tree's own
    ``.git`` file, seen by 9.0.0rc17 before the tree, names the git
    directory for no folder of its own, so the store's earliest view is
    passed over for the tree's, and the tree keeps resolving."""
    store, config = lab
    tree, sep = _separate(tmp_path)
    planted = tmp_path / "downloads" / "archive"
    _link(planted / ".git", tree / ".git")
    with monkeypatch.context() as before:
        before.setattr(location_git, "_recorded_refusal", lambda *a: None)
        before.setattr(location_git, "_working_tree_of", lambda path: None)
        before.setattr(location, "record_working_tree", lambda *a: "kept")
        _forget_records()
        through = resolve_caller(store, config, str(planted))
        known = resolve_caller(store, config, str(tree))
    assert through.resolution.checkout == known.resolution.checkout

    _forget_records()
    again = resolve_caller(store, config, str(tree))
    assert again.resolution.checkout == known.resolution.checkout
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)
    assert _see(planted).state == OUTSIDE


def test_the_store_check_finds_views_whose_device_number_changed(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Re-review finding 3: a remount can give the git directory another
    device number; the first record's check matches the store's views by
    inode, each confirmed by its ``.git`` naming the git directory, so the
    tree the store saw first is still recorded."""
    import dataclasses

    store, config = lab
    tree, sep = _separate(tmp_path)
    resolve_caller(store, config, str(tree))
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    _forget_records()
    cap = _see(planted)
    assert cap.tree_recorded and cap.object_key is not None
    renumbered = dataclasses.replace(
        cap, object_key=("1" + cap.object_key[0], *cap.object_key[1:])
    )
    model = resolve_caller(store, config, str(tree)).model
    _forget_records()
    gitdir_pin.put(str(sep), str(planted))
    assert location._earlier_tree(model, renumbered)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)


def test_a_capture_that_loses_the_first_record_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review finding 4: where another folder's first record lands
    between a capture's follow and its own record, the capture is made
    again under that record, so the folder that lost is not followed."""
    tree, sep = _separate(tmp_path)
    second = _plant(tmp_path / "downloads" / "archive", sep)
    real_put = gitdir_pin.put

    def lost(git_dir: str, tree_: str, previous: Any = None) -> bool:
        real_put(git_dir, str(tree), previous)
        return False

    with monkeypatch.context() as raced:
        raced.setattr(gitdir_pin, "put", lost)
        cap = _see(second)
    assert cap.state == OUTSIDE, cap
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)


def test_a_windows_junction_reads_as_the_link_it_is() -> None:
    """Re-review finding 5: CPython's ``lstat`` reports a Windows
    directory junction as a directory; its reparse tag makes it a link
    here, as a symbolic link is."""
    import stat as stat_mod
    from types import SimpleNamespace

    def fake(mode: int, tag: int = 0) -> Any:
        return SimpleNamespace(st_mode=mode, st_reparse_tag=tag)

    assert location_git._is_link(fake(stat_mod.S_IFLNK | 0o777))
    assert location_git._is_link(fake(stat_mod.S_IFDIR | 0o755, 0xA0000003))
    assert not location_git._is_link(fake(stat_mod.S_IFDIR | 0o755))
    assert not location_git._is_link(fake(stat_mod.S_IFREG | 0o644))


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows'")
def test_a_junction_to_another_trees_git_or_a_separate_git_dir_is_refused(
    tmp_path: Path,
) -> None:
    """Re-review finding 5, on Windows: a ``.git`` junction (``mklink
    /J``) to another working tree's ``.git`` or to a recorded separate git
    directory reads as no repository."""
    import _winapi  # type: ignore[import-not-found,unused-ignore]

    app = tmp_path / "work" / "app"
    app.mkdir(parents=True)
    _git(app, "init", "-q", "-b", "main")
    (app / "main.py").write_text("print(1)\n", encoding="utf-8")
    tree, sep = _separate(tmp_path)
    assert _see(tree).state == REPO
    for name, to in (("own", app / ".git"), ("separate", sep)):
        planted = tmp_path / "downloads" / name
        planted.mkdir(parents=True)
        _winapi.CreateJunction(str(to), str(planted / ".git"))  # type: ignore[attr-defined,unused-ignore]
        assert _see(planted).state == OUTSIDE, name
