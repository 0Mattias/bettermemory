"""Origin metadata follows a ``.git`` entry only where membership does
(ENG-193).

Git follows any ``.git`` file or link. The origin family read the
repository through git and through `githead`, which followed any pointer
too, so a record written in a folder whose ``.git`` entry named another
checkout's git directory carried that checkout's toplevel, remote and HEAD
as its origin, and verify anchoring read them, while membership refused
the same entry (ENG-158, ENG-206; review M G6). `githead` now reads
membership's rule (`gitfile_rule`), and `origin` runs no git process where
git's walk would reach a repository membership refuses, so the folder's
origin is that of a folder outside any repository whichever way it is
asked: the capture, the toplevel and head, the history, a commit's
reachability, the history key and the object reads verify anchoring
makes. The shapes git writes keep their origin, and a git directory kept
apart that no record names a tree for yet is followed as membership
follows it, recorded by membership's capture alone; a write that captured
its origin through such a follow captures it again once its caller's
resolution has made the record.

Git's walk passes over a ``.git`` directory, a link to one, or a folder
that fails its own test of a git directory and goes on to the folder
above, so an entry it passes over hides no plant above it: the walk that
decides whether git runs stops only where git's does.

The repositories are real, built with git under ``tmp_path``; the state
directory, where membership keeps its records, is the test's own
(``tests/conftest.py``).
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import (
    gitdir_pin,
    gitfile_rule,
    githead,
    location,
    origin,
    origin_gitfiles,
    origin_history,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.location import resolve_caller
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.verify_anchoring import holds_commit

from ._mcp import call_tool
from .test_location_gitdir_pin import _forget_records, _plant, _separate
from .test_location_gitfile import PLANTS, plant
from .test_membership_security import _git, checkout, neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: The app's remote, which an origin captured through a plant carried.
REMOTE = "https://example.invalid/acme/app.git"


@pytest.fixture(autouse=True)
def git_set_neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed identity, git's walk
    stopped above `tmp_path`."""
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """The folder of every git process the package starts from here on,
    as `_git.run_git` is asked to run it."""
    seen: list[Path | None] = []
    real = git_runner.run_git

    def spy(args: Any, **kwargs: Any) -> Any:
        cwd = kwargs.get("cwd")
        seen.append(None if cwd is None else Path(cwd))
        return real(args, **kwargs)

    monkeypatch.setattr(git_runner, "run_git", spy)
    return seen


@pytest.fixture(params=["native", "off_posix"])
def as_platform(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """The platform `githead` reads the files as: this one, and one off
    POSIX (``githead._POSIX`` False, the constant its discovery reads for
    that, as on Windows). Off POSIX, where git's ownership check reads
    security descriptors, `githead.find_gitdir` and `githead.signature`
    decline before they read a file, and the origin cache keys a capture
    on what `githead.locate` reads instead (`_keyed`)."""
    if request.param == "off_posix":
        monkeypatch.setattr(githead, "_POSIX", False)
    return str(request.param)


def _keyed(where: Path) -> Any:
    """The signature the origin cache keeps a capture of `where` against
    (`origin_gitfiles.capture_files`): `githead.signature` where its
    discovery settles the repository, else, off POSIX among those, the one
    read without git's ownership check (``owner=False``), which reads the
    rule as discovery does. Equal across two calls only where a capture is
    kept, on every platform."""
    return origin_gitfiles.capture_files(where)[0]


def _same(a: str | Path | None, b: str | Path) -> bool:
    return a is not None and os.path.samefile(a, b)


def _inside(path: Path | None, folder: Path) -> bool:
    """Whether `path` is `folder` or lies below it, by the resolved path."""
    if path is None:
        return False
    here, there = os.path.realpath(path), os.path.realpath(folder)
    return here == there or here.startswith(there + os.sep)


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD")


def _link(link: Path, to: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(to, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform makes no symbolic link here")


#: The plants: a ``.git`` file naming the app's own ``.git`` (the review's
#: scenario), a ``.git`` that is a symbolic link to it, a hard link to a
#: linked worktree's ``.git`` file, a ``.git`` file naming a
#: ``--separate-git-dir`` directory recorded for its own tree, and review
#: M's D1 and G5 spellings (`test_location_gitfile.plant`).
SHAPES = ["gitfile", "dotgit_link", "hardlink", "recorded_separate", *PLANTS]


def _planted(tmp_path: Path, app: Path, how: str) -> tuple[Path, Path]:
    """A folder planted the way `how` says, and the working tree whose
    repository it names."""
    folder = tmp_path / "downloads" / "archive"
    if how == "gitfile":
        return _plant(folder, app / ".git"), app
    if how == "dotgit_link":
        _link(folder / ".git", app / ".git")
        return folder, app
    if how == "hardlink":
        feat = app.parent / "feat"
        _git(app, "worktree", "add", "-q", "-b", "feat", str(feat))
        folder.mkdir(parents=True)
        try:
            os.link(feat / ".git", folder / ".git")
        except (OSError, NotImplementedError):
            pytest.skip("this volume makes no hard link here")
        return folder, feat
    if how == "recorded_separate":
        tree, sep = _separate(tmp_path, "kept")
        _git(tree, "remote", "add", "origin", REMOTE)
        # Membership's capture records the tree it follows first.
        assert location.capture(str(tree), write_token=False).state == location.REPO
        pin = gitdir_pin.recorded(str(sep))
        assert pin is not None and _same(pin.tree, tree)
        return _plant(folder, sep), tree
    return plant(folder, app, how), app


@pytest.mark.parametrize("how", SHAPES)
def test_a_refused_git_entry_gives_no_origin_and_runs_no_git(
    tmp_path: Path, runs: list[Path | None], how: str
) -> None:
    """A folder whose ``.git`` entry names another checkout's git
    directory, and a folder below it, have the origin of a folder outside
    any repository along every path the origin family reads one by: the
    capture's remote, branch and toplevel, the toplevel and head (from the
    files and from git), HEAD's commit, its history, a commit's
    reachability, the history key, and the object store verify anchoring
    reads. Its signature is undetermined, so no capture of it is kept. No
    git process runs there; the checkout itself keeps its origin."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    planted, owner = _planted(tmp_path, app, how)
    head = _head(owner)
    inner = planted / "inner"
    inner.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    nowhere = origin.capture(outside)
    runs.clear()

    for folder in (planted, inner):
        got = origin.capture(folder)
        assert (got.repo, got.branch, got.worktree_root) == (None, None, None), folder
        assert got.git_indeterminate is nowhere.git_indeterminate is False
        assert origin.repo_toplevel(folder) is None
        assert origin.repo_toplevel_and_head(folder) is None
        assert origin.toplevel_and_head_from_files(folder) is None
        assert origin.head_sha(folder) is None
        assert origin.commit_author_timestamps(folder) is None
        assert origin.commit_reachable(folder, head) is None
        assert origin_history.located_key(folder) is None
        assert githead.find_gitdir(folder) is None
        assert githead.gitdir_at(folder) is None
        assert githead.locate(folder) is None
        assert _keyed(folder) != _keyed(folder)
        assert not holds_commit(folder, head)
    assert [cwd for cwd in runs if _inside(cwd, planted)] == []

    kept = origin.capture(owner)
    assert kept.repo == REMOTE and _same(kept.worktree_root, owner)
    assert origin.head_sha(owner) == head
    assert holds_commit(owner, head)


#: Entries git's walk passes over (``is_git_directory`` fails), each in a
#: folder below a planted ``.git`` file: a ``.git`` directory holding only
#: a HEAD, one beside objects and refs whose HEAD ``validate_headref``
#: refuses, a ``.git`` link to a folder holding only a HEAD, and the folder
#: itself holding a refused HEAD beside objects and refs.
PASSED_OVER = ["head_only", "refused_head", "link_to_head_only", "folder_refused_head"]


def _passed_over(folder: Path, how: str, tmp_path: Path) -> None:
    """Put in `folder` the entry `how` names, which git's walk passes over."""
    if how == "link_to_head_only":
        junk = tmp_path / "junk"
        junk.mkdir()
        (junk / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        _link(folder / ".git", junk)
        return
    holder = folder if how == "folder_refused_head" else folder / ".git"
    holder.mkdir(parents=True, exist_ok=True)
    if how == "head_only":
        (holder / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        return
    (holder / "HEAD").write_text("nonsense\n", encoding="utf-8")
    (holder / "objects").mkdir()
    (holder / "refs").mkdir()


@pytest.mark.parametrize("how", PASSED_OVER)
def test_an_entry_git_passes_over_hides_no_plant_above_it(
    tmp_path: Path, runs: list[Path | None], how: str
) -> None:
    """Git's walk passes over a ``.git`` directory, a ``.git`` link to a
    directory and a folder that fail its test of a git directory (a HEAD
    ``validate_headref`` refuses, no objects or refs) and goes on to the
    folder above, so git run below a planted ``.git`` file reads, through
    such an entry, the checkout the plant names. The walk that decides
    whether git runs stopped at the entry, read it as a repository
    membership follows, and let git run: the origin there carried the
    checkout's remote and HEAD. It stops only where git's walk stops, so
    the origin is that of a folder outside any repository and no git runs
    there."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    planted = _plant(tmp_path / "downloads" / "archive", app / ".git")
    sub = planted / "sub"
    sub.mkdir()
    _passed_over(sub, how, tmp_path)
    # Git passes over the entry and reads the checkout the plant names.
    assert _git(sub, "remote", "get-url", "origin") == REMOTE
    head = _head(app)
    runs.clear()

    got = origin.capture(sub)
    assert (got.repo, got.branch, got.worktree_root) == (None, None, None)
    assert origin.repo_toplevel_and_head(sub) is None
    assert origin.head_sha(sub) is None
    assert origin.commit_reachable(sub, head) is None
    assert _keyed(sub) != _keyed(sub)
    assert not holds_commit(sub, head)
    assert [cwd for cwd in runs if _inside(cwd, planted)] == []


def test_a_plant_inside_a_checkout_is_placed_in_it_by_membership_and_has_no_origin(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """A ``.git`` file planted in a folder of the user's own checkout (a
    vendored package naming another checkout's ``.git``): membership passes
    over the entry it refuses and places the folder, and a folder below
    it, in the enclosing checkout, while git's walk stops at the entry, so
    the origin, for which git is not run there, is that of a folder
    outside any repository (docs/internals.md, the origin of a refused
    entry). Both read the checkout the plant names nowhere; the enclosing
    checkout keeps its origin."""
    other = checkout(tmp_path / "work" / "other", remote=REMOTE)
    mine = checkout(
        tmp_path / "work" / "mine", remote="https://example.invalid/acme/mine.git"
    )
    vendored = _plant(mine / "vendor" / "pkg", other / ".git")
    deep = vendored / "deep"
    deep.mkdir()
    runs.clear()

    for folder in (vendored, deep):
        cap = location.capture(str(folder), write_token=False)
        assert cap.state == location.REPO and _same(cap.root, mine), folder
        got = origin.capture(folder)
        assert (got.repo, got.branch, got.worktree_root) == (None, None, None)
        assert origin.repo_toplevel_and_head(folder) is None
    assert [cwd for cwd in runs if _inside(cwd, vendored)] == []
    assert _same(origin.capture(mine).worktree_root, mine)


@pytest.mark.parametrize("branch", ["packed", "unborn"])
def test_a_head_linked_into_refs_is_keyed_on_its_own_checkout(
    tmp_path: Path, branch: str
) -> None:
    """Git's oldest HEAD format, a symbolic link into ``refs/`` (git writes
    it under ``core.preferSymlinkRefs``), names a branch that may have no
    loose ref (packed, or not born yet), so the link reaches nothing; git
    reads the repository all the same (``validate_headref``). Membership
    read no repository there and keyed a checkout nested in another on the
    enclosing one; it is keyed on itself now, as the origin reads it."""
    enclosing = checkout(tmp_path / "work" / "enc")
    repo = enclosing / "inner"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", REMOTE)
    if branch == "packed":
        (repo / "f").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "one")
        _git(repo, "pack-refs", "--all")
    head = repo / ".git" / "HEAD"
    head.unlink()
    try:
        head.symlink_to("refs/heads/main")
    except (OSError, NotImplementedError):
        pytest.skip("this platform makes no symbolic link here")
    assert _same(_git(repo, "rev-parse", "--show-toplevel"), repo)

    cap = location.capture(str(repo), write_token=False)
    assert cap.state == location.REPO and _same(cap.root, repo)
    got = origin.capture(repo)
    assert got.repo == REMOTE and _same(got.worktree_root, repo)


@pytest.mark.parametrize("how", PASSED_OVER)
def test_membership_passes_over_what_git_passes_over_so_the_origin_settles(
    tmp_path: Path, how: str
) -> None:
    """Membership's discovery stopped at a ``.git`` directory, a link or a
    folder git's walk passes over (it asked only for a HEAD), where the
    origin's walk, stopping where git's does, went on to a ``.git`` file
    above naming a git directory kept apart with no tree recorded. The
    origin followed that file; a write's caller resolution, stopped below
    it, recorded nothing, so the origin captured again still rested on the
    unrecorded follow and was stored so. Membership's discovery stops by
    git's test too now: both stop at the file, membership's capture records
    its folder, and the origin captured after it is settled."""
    tree, sep = _separate(tmp_path, "kept")
    _git(tree, "remote", "add", "origin", REMOTE)
    folder = _plant(tmp_path / "downloads" / "archive", sep)
    sub = folder / "sub"
    sub.mkdir()
    _passed_over(sub, how, tmp_path)
    # Git passes over the entry and reads the git directory the file names.
    assert _same(_git(sub, "rev-parse", "--show-toplevel"), folder)

    first = origin.capture(sub)
    assert first.repo == REMOTE and origin.follow_unsettled(first)
    cap = location.capture(str(sub), write_token=False)
    assert cap.state == location.REPO and _same(cap.root, folder)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, folder)
    again = origin.capture(sub)
    assert again.repo == REMOTE and _same(again.worktree_root, folder)
    assert not origin.follow_unsettled(again)


@pytest.mark.parametrize("how", ["refused_head", "folder_refused_head"])
def test_a_head_git_does_not_read_leaves_the_signature_on_the_checkout_above(
    tmp_path: Path, how: str
) -> None:
    """Membership's signature reads HEAD once a prompt: its walk takes a
    ``HEAD`` by its presence and the signature tests the bytes it then
    reads by git's rule. Below a ``.git`` directory, or in a folder, whose
    HEAD git does not read as one (beside ``objects`` and ``refs``), that
    walk stops where git's goes on to the checkout above. The signature
    then walks as git does, so it is kept against the checkout the capture
    is of, neither declined nor kept against the entry git passes over,
    and a commit in that checkout is seen at the next prompt."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    sub = app / "vendor"
    sub.mkdir()
    _passed_over(sub, how, tmp_path)
    assert _same(_git(sub, "rev-parse", "--show-toplevel"), app)

    cap = location.capture(str(sub), write_token=False)
    assert cap.state == location.REPO and _same(cap.root, app)
    before = location.signature(str(sub))
    assert before is not None and _same(before[4], app / ".git")
    (app / "more.txt").write_text("more\n", encoding="utf-8")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "more")
    assert location.signature(str(sub)) != before
    assert location.capture(str(sub), write_token=False).head == _head(app)


def test_an_object_directory_in_the_environment_moves_neither_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's test of a git directory checks ``$GIT_OBJECT_DIRECTORY`` in
    place of a candidate's ``objects`` where that is set, and the hook and
    the server run in the user's environment. Membership reads each
    repository by its own files, and so does the origin's walk, so both
    pass over a ``.git`` holding a HEAD, refs and a config but no
    ``objects``; the origin's git, given the variable, stopped there and
    read that folder's config as the repository. No git the package runs
    is given it now, so the origin is the checkout's above, as membership
    keys the folder."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    sub = app / "vendor"
    dotgit = sub / ".git"
    (dotgit / "refs").mkdir(parents=True)
    (dotgit / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (dotgit / "config").write_text(
        '[remote "origin"]\n\turl = https://example.invalid/other.git\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(app / ".git" / "objects"))
    # Git given the variable stops at the entry; without it, at the checkout.
    assert _same(_git(sub, "rev-parse", "--show-toplevel"), sub)

    cap = location.capture(str(sub), write_token=False)
    assert cap.state == location.REPO and _same(cap.root, app)
    got = origin.capture(sub)
    assert got.repo == REMOTE and _same(got.worktree_root, app)


def test_an_object_directory_in_the_environment_keeps_the_origin_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[Path | None],
    as_platform: str,
) -> None:
    """``GIT_OBJECT_DIRECTORY`` in the server's environment reaches no git
    the package runs (`_git.SCRUBBED_ENV`), so it moves nothing the files
    are read for: they still settle the repository, the origin's capture
    is kept, and the next capture there runs no git. Read as a variable
    that moves discovery, it had every capture there ask git again."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    empty = tmp_path / "objects"
    empty.mkdir()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(empty))
    if githead._POSIX:
        found = githead.find_gitdir(app)
        assert found is not None and _same(found.worktree_root, app)
    assert _keyed(app) == _keyed(app)
    first = origin.capture(app)
    asked = len(runs)
    again = origin.capture(app)
    assert len(runs) == asked
    assert again == first
    assert again.repo == REMOTE and again.branch == "main"
    assert origin.head_sha(app) == _head(app)


def test_a_recorded_tree_that_does_not_answer_refuses_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder naming a git directory kept apart whose record names a
    tree on a share that no longer answers (its stat hangs, as
    tests/test_membership_hung_view.py hangs one): every reader of the rule
    reads the recorded tree, and read unbounded each held its caller for as
    long as the share hung. It is read within the bound every recorded
    path is read within (`location_reads.bounded_read`), so the follow is
    refused at once past it, as one whose ``.git`` cannot be read: origin's
    walk, the signature and membership's capture all return, and the
    folder's origin is that of a folder outside any repository."""
    tree, sep = _separate(tmp_path, "kept")
    _git(tree, "remote", "add", "origin", REMOTE)
    location.capture(str(tree), write_token=False)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None
    planted = _plant(tmp_path / "downloads" / "archive", sep)
    hung = {pin.tree, os.path.realpath(pin.tree)}
    release = threading.Event()
    real = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in hung:
            release.wait(60)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    out: dict[str, Any] = {}

    def read() -> None:
        out["why"] = gitfile_rule.discovery_refusal(str(planted))
        out["signature"] = _keyed(planted)
        out["origin"] = origin.capture(planted)
        out["membership"] = location.capture(str(planted), write_token=False)

    worker = threading.Thread(target=read, daemon=True)
    try:
        worker.start()
        worker.join(gitfile_rule._RECORDED_BUDGET * 4 + 5)
        assert not worker.is_alive(), "a reader waited on the recorded tree"
    finally:
        release.set()
    assert "did not answer" in out["why"]
    assert out["signature"] != _keyed(planted)
    assert (out["origin"].repo, out["origin"].worktree_root) == (None, None)
    assert out["membership"].git_dir is None or not _same(
        out["membership"].git_dir, sep
    )


def test_a_recorded_tree_reached_through_a_symlink_starts_no_probe_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--separate-git-dir`` checkout recorded at its resolved path,
    and a session in it reached through a symbolic link. Membership's
    signature walks the cwd as given, so every capture there asked whether
    the record named the linked spelling and read the recorded tree on a
    probe thread to tell: one thread on every prompt, for a follow that
    stood each time. The record is compared with the folder's resolved
    path before any read, so a capture at steady state starts no probe
    thread and still follows the git directory."""
    tree, sep = _separate(tmp_path, "kept")
    location.capture(str(tree), write_token=False)
    link = tmp_path / "links" / "kept"
    _link(link, tree)
    first = location.capture(str(link), write_token=False)
    assert first.state == location.REPO and _same(first.git_dir, sep)
    started: list[str] = []
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        started.append(self.name)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    for _ in range(5):
        again = location.capture(str(link), write_token=False)
        assert again.state == location.REPO and _same(again.git_dir, sep)
    assert [name for name in started if name.startswith("bettermemory-probe")] == []


def test_a_commondir_naming_a_missing_folder_is_passed_over_as_git_passes_it(
    tmp_path: Path,
) -> None:
    """Git resolves a ``commondir`` allowing its last component to be
    missing (``strbuf_realpath``), finds no ``objects`` there and walks on;
    it dies only where a folder before the last is missing. A ``.git``
    holding such a ``commondir`` read as one git dies on, so the walk
    stopped there, with no origin, where git reads the checkout above."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    dotgit = app / "sub" / ".git"
    dotgit.mkdir(parents=True)
    (dotgit / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (dotgit / "commondir").write_text("../missing\n", encoding="utf-8")
    assert _same(_git(dotgit.parent, "rev-parse", "--show-toplevel"), app)

    assert gitfile_rule._git_reads(str(dotgit)) is False
    assert _same(origin.capture(dotgit.parent).worktree_root, app)


@pytest.mark.parametrize("record", [True, False], ids=["recorded", "unrecorded"])
def test_the_shapes_git_writes_keep_their_origin(
    tmp_path: Path, record: bool, as_platform: str
) -> None:
    """An ordinary checkout, a linked worktree, a submodule and a
    ``--separate-git-dir`` tree read from its own folder, recorded or not
    yet, each keep the origin git gives them: the remote, the toplevel and
    the head, from the capture, from the files and from git. The files are
    read as `githead.locate` reads them on every platform, and as
    `githead.find_gitdir` reads them on POSIX, where git's ownership check
    is the owner's id; off POSIX it declines before it reads a file, as on
    9.0.0rc17 (`as_platform`)."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
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
    tree, sep = _separate(tmp_path, "kept")
    _git(tree, "remote", "add", "origin", REMOTE)
    if record:
        location.capture(str(tree), write_token=False)
        assert gitdir_pin.recorded(str(sep)) is not None

    for name, where in {
        "primary": app,
        "worktree": feat,
        "submodule": app / "deps" / "lib",
        "separate": tree,
    }.items():
        head = _head(where)
        got = origin.capture(where)
        assert _same(got.worktree_root, where), name
        if name != "submodule":
            assert got.repo == REMOTE, name
        located = origin.repo_toplevel_and_head(where)
        assert located is not None and _same(located[0], where), name
        assert located[1] == head, name
        assert origin.head_sha(where) == head, name
        found = githead.locate(where)
        assert found is not None and _same(found.worktree_root, where), name
        if githead._POSIX:
            found = githead.find_gitdir(where)
            assert found is not None and _same(found.worktree_root, where), name
        else:
            assert githead.find_gitdir(where) is None, name
        assert holds_commit(where, head), name


def test_a_separate_git_directory_with_no_record_is_followed_and_never_recorded_by_origin(
    tmp_path: Path, runs: list[Path | None], as_platform: str
) -> None:
    """With no tree recorded for a git directory kept apart, origin
    follows its ``.git`` file as membership's rule does, and writes no
    record: membership's capture records the first tree it follows. Until
    then the signature the origin cache keys on is undetermined and no
    capture is kept, so each asks git again; once the record names the
    folder the capture is kept and the next one runs no git. Off POSIX
    that signature is the one read without git's ownership check
    (`_keyed`), and it settles as on POSIX."""
    tree, sep = _separate(tmp_path)
    _git(tree, "remote", "add", "origin", REMOTE)

    got = origin.capture(tree)
    assert got.repo == REMOTE and _same(got.worktree_root, tree)
    assert gitdir_pin.recorded(str(sep)) is None
    assert _keyed(tree) != _keyed(tree)
    asked = len(runs)
    origin.capture(tree)
    assert len(runs) > asked

    location.capture(str(tree), write_token=False)
    pin = gitdir_pin.recorded(str(sep))
    assert pin is not None and _same(pin.tree, tree)
    assert _keyed(tree) == _keyed(tree)
    origin.capture(tree)
    asked = len(runs)
    again = origin.capture(tree)
    assert len(runs) == asked
    assert again.repo == REMOTE and _same(again.worktree_root, tree)


def test_a_plant_followed_before_any_record_is_refused_once_the_tree_is_recorded(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """A folder naming a git directory kept apart before any tree is
    recorded for it is followed, as membership's rule follows it then (and
    membership's capture there would record it). That capture is not kept,
    so once membership's capture records the checkout's own tree, the
    folder's next origin is no repository's, and no git runs there."""
    tree, sep = _separate(tmp_path)
    _git(tree, "remote", "add", "origin", REMOTE)
    planted = _plant(tmp_path / "downloads" / "archive", sep)

    first = origin.capture(planted)
    assert first.repo == REMOTE and _same(first.worktree_root, planted)
    assert gitdir_pin.recorded(str(sep)) is None

    location.capture(str(tree), write_token=False)
    runs.clear()
    again = origin.capture(planted)
    assert (again.repo, again.branch, again.worktree_root) == (None, None, None)
    assert origin.head_sha(planted) is None
    assert [cwd for cwd in runs if _inside(cwd, planted)] == []


class _Unrecorded:
    """A store that saw a ``--separate-git-dir`` checkout before records
    were kept (9.0.0rc17), a folder whose planted ``.git`` file names its
    git directory, no record, and a server whose calls stand in that
    folder."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = Store(tmp_path / "store")
        config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
        self.tree, self.sep = _separate(tmp_path, "kept")
        _git(self.tree, "remote", "add", "origin", REMOTE)
        resolve_caller(self.store, config, str(self.tree))
        planted = _plant(tmp_path / "downloads" / "archive", self.sep)
        _forget_records()
        # Premise: with no record, the origin follows the planted file.
        assert origin.capture(planted).repo == REMOTE
        import bettermemory._handlers as handlers_module

        monkeypatch.setattr(
            handlers_module, "capture_origin", lambda cwd=None: origin.capture(planted)
        )
        self.state = SessionState()
        self.server = build_server(
            config=config,
            store=self.store,
            state=self.state,
            recorder=Recorder(store=self.store, session_id=self.state.session_id),
        )

    def recorded_for_the_tree(self) -> bool:
        pin = gitdir_pin.recorded(str(self.sep))
        return pin is not None and _same(pin.tree, self.tree)


async def test_a_write_through_an_unrecorded_follow_reads_its_origin_after_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write from a folder whose ``.git`` file names a git directory
    kept apart that no record names a tree for (a checkout the store saw
    before 9.0.0rc18 kept records): the origin captured first follows the
    file, as membership's rule does until a record names a tree. The
    write's caller resolution then makes the record, for the checkout's
    own tree, which the store saw first (`location._earlier_tree`), and
    refuses the folder. The write captured its origin before resolving
    its caller, so the record carried the checkout's remote and toplevel;
    it now captures it again after the resolution, so the record carries
    the origin of a folder outside any repository, and a claim declared
    there is refused as one outside any worktree."""
    lab = _Unrecorded(tmp_path, monkeypatch)
    note = {
        "content": "the kept checkout prints its greeting from main.py on start",
        "scopes": ["tools"],
    }
    try:
        written = await call_tool(lab.server, "memory_write", note)
        assert "id" in written, written
        stored = lab.store.load_one(written["id"])
        assert stored.origin is not None
        assert (stored.origin.repo, stored.origin.worktree_root) == (None, None)
        assert lab.recorded_for_the_tree()
        with pytest.raises(Exception, match="not inside a git worktree"):
            await call_tool(lab.server, "memory_write", {**note, "claims": ["main.py"]})
    finally:
        lab.store.close()


async def test_an_episode_through_an_unrecorded_follow_reads_its_origin_after_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An episode written from the same folder: its handler resolves the
    caller after capturing the origin too, so the episode carried the
    checkout's remote and toplevel; the origin is captured again once the
    resolution made the record, and the episode carries the origin of a
    folder outside any repository."""
    lab = _Unrecorded(tmp_path, monkeypatch)
    try:
        await call_tool(
            lab.server,
            "episode",
            {"action": "write", "body": "read the greeting path in the kept checkout"},
        )
        (episode,) = lab.store.episodes_by_session(lab.state.session_id)
        assert episode.origin is not None
        assert (episode.origin.repo, episode.origin.worktree_root) == (None, None)
        assert lab.recorded_for_the_tree()
    finally:
        lab.store.close()


def test_a_caller_asking_what_git_does_reads_the_entry_as_git_does(
    tmp_path: Path,
) -> None:
    """`githead.locate` with ``as_git`` reads a refused entry as git
    follows it, for the audit and the key export, which ask whether a
    ``git add`` there writes into a repository's history: a planted
    pointer says it does, into the checkout it names."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    planted = _plant(tmp_path / "downloads" / "archive", app / ".git")

    assert githead.locate(planted) is None
    found = githead.locate(planted, as_git=True)
    assert found is not None and _same(found.commondir, app / ".git")
