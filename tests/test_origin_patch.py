"""Claim drift in a partial clone narrows over the commits whose blobs are
local (ENG-220).

No git the package runs fetches (ENG-218), so in a blobless clone a blob
the clone lacks reads as absent, and one ``git log -p`` over a window failed
whole on the first: the governed half fell back to the claimed file's
per-file count and named no claim. `origin_patch.local_patch_window` now
streams the commits whose blobs are local and counts the rest per file, and
the count is never below a full clone's.

Each clone is a ``file://`` clone of a scratch repository built with git's
plumbing, so a commit can hold a symbolic link, a gitlink or a binary file
on every platform. The test's own plain git fetches the blobs a case needs
local, as a user's checkout or diff would.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, _git, githead, origin, origin_patch
from bettermemory import verify as verify_mod
from bettermemory.claims import COMMIT_MARK, load_claims, parse_claim
from bettermemory.origin_patch import PatchWindow, local_patch_window
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ResolvedCommitDrift,
    resolve_commit_drift,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git_version() -> tuple[int, ...]:
    if shutil.which("git") is None:
        return ()
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    digits = out.split()[2].split(".")[:2]
    return tuple(int(d) for d in digits if d.isdigit())


#: A git before 2.44 ignores ``GIT_NO_LAZY_FETCH``: held by the empty
#: ``GIT_ALLOW_PROTOCOL`` alone, its blob check ends at the first blob the
#: clone lacks, and the window keeps the per-file count.
needs_lazy_fetch_pin = pytest.mark.skipif(
    _git_version() < (2, 44), reason="GIT_NO_LAZY_FETCH is read from git 2.44"
)
needs_reftable = pytest.mark.skipif(
    _git_version() < (2, 45), reason="git clones into a reftable from 2.45"
)
#: A git before 2.19 has no ``core.useReplaceRefs``: the variable alone
#: sets replace refs aside there, and no setting of a repository's can
#: turn them back on.
needs_use_replace_refs = pytest.mark.skipif(
    _git_version() < (2, 19), reason="core.useReplaceRefs is read from git 2.19"
)

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)

_FOO = "src/mod.py::foo"
_BAZ = "src/other.py::baz"
_GONE = "src/gone.py"
_F = "src/f.py"
_X, _Y, _Z = "src/x.py", "src/y.py", "src/z.py::foo"

#: A file's text or bytes, a symbolic link (``("link", target)``), a gitlink
#: (``("gitlink", commit)``), or None for a deletion.
Entry = str | bytes | tuple[str, str] | None
#: The commits of a history, each the paths it writes.
History = list[dict[str, Entry]]


def _module(foo: str, bar: int) -> str:
    return f"def foo({foo}):\n    return 0\n\n\ndef bar():\n    return {bar}\n"


def _other(n: int) -> str:
    return f"def baz():\n    return {n}\n"


def _long(n: int) -> str:
    return f"THIS_IS_A_LONG_LINE_{n} = 'padding padding padding'\n"


_Z0 = "def foo():\n    return 0\n"
_Z1 = "def foo():\n    return 1111111111111\n"

#: The symbol window: the first commit changes foo's def line, the other
#: three bar's body.
_SYMBOL: History = [
    {"src/mod.py": _module("", 0)},
    {"src/mod.py": _module("flag=False", 0)},
    {"src/mod.py": _module("flag=False", 1)},
    {"src/mod.py": _module("flag=False", 2)},
    {"src/mod.py": _module("flag=False", 3)},
]
#: Two claimed files: mod.py's foo changes in the first commit and bar in
#: the second; other.py's baz changes only in body, twice.
_TWO: History = [
    {"src/mod.py": _module("", 0), "src/other.py": _other(0)},
    {"src/mod.py": _module("flag=False", 0)},
    {"src/mod.py": _module("flag=False", 1)},
    {"src/other.py": _other(1)},
    {"src/other.py": _other(2)},
]
#: The path window: gone.py is edited twice and then deleted.
_PATH: History = [
    {"src/gone.py": _long(0), "src/keep.py": "x = 0\n"},
    {"src/gone.py": _long(1)},
    {"src/gone.py": _long(2)},
    {"src/gone.py": None},
]
#: f.py edited twice, made a symbolic link, then a file again.
_TO_LINK: History = [
    {_F: _long(0)},
    {_F: _long(1)},
    {_F: _long(2)},
    {_F: ("link", "some/long/symlink/target/one.py")},
    {_F: _long(4)},
]
#: f.py edited twice, then made a gitlink.
_TO_GITLINK: History = [
    {_F: _long(0)},
    {_F: _long(1)},
    {_F: _long(2)},
    {_F: ("gitlink", "1" * 40)},
]
#: f.py edited in every commit; a replacement of the third (`_replacement`)
#: deletes it.
_EDITS: History = [
    {_F: _long(0), "src/keep.py": "k = 1\n"},
    {_F: _long(1)},
    {_F: _long(2)},
    {_F: _long(3)},
    {_F: _long(4)},
    {_F: _long(5)},
]
#: x.py (short lines) is deleted in a local commit, so its path claim fires
#: implicating nothing; y.py is made binary or empty and deleted in counted
#: commits, a deletion the patch prints without a ``/dev/null`` header.
_BARE: dict[str, History] = {
    "binary": [
        {_X: "a = 1\n", _Y: _long(0), "src/z.py": _Z0},
        {_Y: _long(1)},
        {"src/z.py": _Z1},
        {_X: None},
        {_Y: b"\x00binary\x00" * 4},
        {_Y: None},
    ],
    "empty": [
        {_X: "a = 1\n", _Y: _long(0), "src/z.py": _Z0},
        {_Y: _long(1)},
        {"src/z.py": _Z1},
        {_X: None},
        {_Y: ""},
        {_Y: None, "src/z.py": _Z1 + "#" + _long(7)},
        {"src/z.py": _Z1 + "#" + _long(8)},
    ],
}


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, no inherited repository
    variables, and no no-fetch setting of the shell's: the test's own git
    fetches from the source as a user's would."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (*_git.SCRUBBED_ENV, *_git.NO_FETCH):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)


def _plain(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _object(repo: Path, data: bytes) -> str:
    done = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=data,
        capture_output=True,
        check=True,
    )
    return done.stdout.decode().strip()


class Partial:
    """A source repository whose first commit is verified and whose later
    commits are the window, and a ``file://`` clone of it.

    `commits` are the window's commits, oldest first, and `blobs[i]` the
    blobs, by path, of the tree the i-th commit of the history left (the
    first commit is 0)."""

    def __init__(self, base: Path, history: History) -> None:
        self.base = Path(os.path.realpath(base))
        self.source = self.base / "source"
        self.source.mkdir()
        _plain(self.source, "init", "-q")
        _plain(self.source, "config", "uploadpack.allowFilter", "true")
        shas: list[str] = []
        self.blobs: list[dict[str, str]] = []
        for i, files in enumerate(history):
            for rel, entry in files.items():
                self._stage(rel, entry)
            when = _T0 if i == 0 else _VERIFIED + timedelta(hours=i)
            _plain(self.source, "commit", "-q", "-m", f"c{i}", when=when)
            shas.append(_plain(self.source, "rev-parse", "HEAD"))
            self.blobs.append(self._tree_blobs())
        self.anchor = shas[0]
        self.commits = shas[1:]
        self.repo = self.base / "clone"

    def _stage(self, rel: str, entry: Entry) -> None:
        if entry is None:
            _plain(self.source, "update-index", "--force-remove", "--", rel)
            return
        if isinstance(entry, tuple) and entry[0] == "gitlink":
            mode, oid = "160000", entry[1]
        elif isinstance(entry, tuple):
            mode, oid = "120000", _object(self.source, entry[1].encode())
        else:
            data = entry if isinstance(entry, bytes) else entry.encode()
            mode, oid = "100644", _object(self.source, data)
        _plain(
            self.source, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{rel}"
        )

    def _tree_blobs(self) -> dict[str, str]:
        blobs: dict[str, str] = {}
        for line in _plain(self.source, "ls-tree", "-r", "HEAD").splitlines():
            meta, _, path = line.partition("\t")
            _, kind, oid = meta.split()
            if kind == "blob":
                blobs[path] = oid
        return blobs

    def clone(self, *, blobless: bool = True, reftable: bool = False) -> Path:
        args = ["clone", "-q"]
        if blobless:
            args.append("--filter=blob:none")
        if reftable:
            args.append("--ref-format=reftable")
        _plain(self.base, *args, self.source.as_uri(), str(self.repo))
        return self.repo

    def fetch(self, *versions: int, path: str | None = None) -> None:
        """Make local the blobs of the trees the given commits of the
        history left, or of `path` in them alone."""
        for i in versions:
            for name, blob in self.blobs[i].items():
                if path is None or name == path:
                    _plain(self.repo, "cat-file", "blob", blob)

    def held(self, blob: str) -> bool:
        """Whether the clone holds `blob`, asked with the no-fetch pins."""
        done = _git.run_git(["cat-file", "-e", blob], cwd=self.repo, timeout=10.0)
        assert done is not None
        return done.returncode == 0

    def resolve(self, claims: list[str], basis: str) -> ResolvedCommitDrift:
        walk = None
        if basis == BASIS_REACHABILITY:
            walk = origin.commits_since_anchor(
                self.repo, self.anchor, toplevel=self.repo
            )
            assert walk is not None and len(walk.commits) == len(self.commits)
        resolved = resolve_commit_drift(
            cwd=self.repo,
            since=_VERIFIED,
            unfiltered=len(self.commits),
            anchors=(),
            claims=load_claims(claims),
            toplevel=self.repo,
            walk=walk,
        )
        assert resolved is not None
        assert resolved.basis == basis
        return resolved


_BASES = [BASIS_REACHABILITY, BASIS_AUTHOR_DATE]
_CLONES = pytest.mark.parametrize(
    "blobless",
    [pytest.param(True, marks=needs_lazy_fetch_pin), False],
    ids=["blobless", "full"],
)


@pytest.mark.parametrize("basis", _BASES)
def test_a_full_clone_narrows_to_the_claim(tmp_path: Path, basis: str) -> None:
    """The control: one commit changed foo's def line, so one escalates."""
    partial = Partial(tmp_path, _SYMBOL)
    partial.clone(blobless=False)
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


@needs_lazy_fetch_pin
@pytest.mark.parametrize("basis", _BASES)
def test_a_claim_whose_blobs_are_all_local_narrows_as_in_a_full_clone(
    tmp_path: Path, basis: str
) -> None:
    """Several claims in one window. Every blob of mod.py's window is
    local, so its two commits are streamed and foo's def line escalates
    one, as in a full clone; other.py's two commits read blobs the clone
    lacks and are counted. 3, and foo named; 9.0.0rc18 counted all four
    and named nothing, and a full clone counts 1."""
    partial = Partial(tmp_path, _TWO)
    partial.clone()
    partial.fetch(0, 1, path="src/mod.py")
    assert not partial.held(partial.blobs[3]["src/other.py"]), "premise"
    resolved = partial.resolve([_FOO, _BAZ], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_FOO,))


@pytest.mark.parametrize("basis", _BASES)
def test_the_several_claims_window_in_a_full_clone(tmp_path: Path, basis: str) -> None:
    partial = Partial(tmp_path, _TWO)
    partial.clone(blobless=False)
    resolved = partial.resolve([_FOO, _BAZ], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


@needs_lazy_fetch_pin
@pytest.mark.parametrize("basis", _BASES)
def test_a_window_with_blobs_missing_narrows_over_the_local_commits(
    tmp_path: Path, basis: str
) -> None:
    """The blobs of the first three versions are local, so the first two
    window commits are streamed: the first changed foo's def line and
    escalates, the second only bar's body and does not. The third and
    fourth read the third commit's blob, which the clone lacks: each is
    counted, as the per-file count counts it. 3, where the per-file count
    is 4 and a full clone's 1; and foo is named."""
    partial = Partial(tmp_path, _SYMBOL)
    partial.clone()
    partial.fetch(0, 1, 2)
    missing = partial.blobs[3]["src/mod.py"]
    assert not partial.held(missing), "premise: the clone lacks it"
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_FOO,))
    assert not partial.held(missing), "no call fetched it"


@pytest.mark.parametrize("basis", _BASES)
def test_a_window_with_no_local_commit_counts_every_commit_per_file(
    tmp_path: Path, basis: str
) -> None:
    """A fresh blobless clone holds only the checkout's blobs, so every
    window commit reads one it lacks: the per-file count, as in
    9.0.0rc18, and nothing is fetched."""
    partial = Partial(tmp_path, _SYMBOL)
    partial.clone()
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (4, ())
    assert not any(partial.held(tree["src/mod.py"]) for tree in partial.blobs[:4])


@pytest.mark.parametrize("basis", _BASES)
@_CLONES
def test_a_deletion_in_a_counted_commit_still_counts_the_local_lines(
    tmp_path: Path, basis: str, blobless: bool
) -> None:
    """The path claim fires on its file's deletion and implicates every
    commit carrying lines for the file. The deletion reads a blob the
    clone lacks, so it is counted, not streamed; the trees show the
    deletion, so the claim still fires and the streamed edit is counted
    too: 3, as in a full clone, never 2."""
    partial = Partial(tmp_path, _PATH)
    partial.clone(blobless=blobless)
    if blobless:
        partial.fetch(0, 1)
        assert not partial.held(partial.blobs[2][_GONE]), "premise"
    resolved = partial.resolve([_GONE], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_GONE,))


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    ("history", "local", "expected"),
    [(_TO_LINK, (0, 1, 2), 4), (_TO_GITLINK, (0, 1), 3)],
    ids=["symlink", "gitlink"],
)
@_CLONES
def test_a_typechange_in_a_counted_commit_fires_the_path_claim(
    tmp_path: Path,
    basis: str,
    history: History,
    local: tuple[int, ...],
    expected: int,
    blobless: bool,
) -> None:
    """A file made a symbolic link or a gitlink: the patch prints the
    file's deletion (``+++ /dev/null``) and the new entry's creation, so a
    full clone's path claim fires and implicates every commit carrying
    lines for the path. The typechange reads a blob the clone lacks and is
    counted; its trees show a typechange (``T``), which the split marks as
    the deletion it prints, so the claim fires there too and the streamed
    edits count, as in a full clone."""
    partial = Partial(tmp_path, history)
    partial.clone(blobless=blobless)
    if blobless:
        partial.fetch(*local)
    resolved = partial.resolve([_F], basis)
    assert (resolved.count, resolved.claims_drifted) == (expected, (_F,))


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    ("kind", "local", "expected"),
    [("binary", (0, 1, 2, 3), 5), ("empty", (0, 1, 2, 3, 4), 6)],
)
@_CLONES
def test_a_claim_only_the_trees_fire_keeps_the_whole_window(
    tmp_path: Path,
    basis: str,
    kind: str,
    local: tuple[int, ...],
    expected: int,
    blobless: bool,
) -> None:
    """x.py's deletion is local and carries no line long enough to index,
    so its path claim fires implicating nothing, and a full clone counts
    the whole window. y.py's deletion, of a binary or empty file, is
    printed without a ``/dev/null`` header, so a full clone's y claim
    stays quiet; the split's trees show it, so y fires there and
    implicates its local edit. The whole window is still counted: in a
    split, any fired claim that implicates nothing takes it."""
    partial = Partial(tmp_path, _BARE[kind])
    partial.clone(blobless=blobless)
    if blobless:
        partial.fetch(*local)
    resolved = partial.resolve([_X, _Y, _Z], basis)
    assert resolved.count == expected
    assert _X in resolved.claims_drifted


@needs_lazy_fetch_pin
def test_the_split_keeps_its_listing_and_check_until_a_pack_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The split's listing and check are kept for the window: a second
    split runs no git where nothing was streamed. A fetch of the missing
    blobs writes a pack, which the key sees, so the next split lists and
    checks again and streams the commits now local."""
    partial = Partial(tmp_path, _SYMBOL)
    repo = partial.clone()
    if githead.gitdir_at(repo) is None:
        pytest.skip("the repository's files do not settle it on this platform")
    specs = [":(literal)src/mod.py"]
    calls: list[str] = []
    real = origin._git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str | None:
        calls.append(_git.subcommand(args))
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git", spy)
    first = local_patch_window(repo, partial.commits, specs, toplevel=repo)
    assert first is not None and len(first.counted) == 4
    assert calls == ["log", "cat-file"]
    calls.clear()
    assert local_patch_window(repo, partial.commits, specs, toplevel=repo) == first
    assert calls == []
    partial.fetch(0, 1, 2)
    again = local_patch_window(repo, partial.commits, specs, toplevel=repo)
    assert again is not None and len(again.counted) == 2
    assert calls == ["log", "cat-file", "log"]


def _replacement(partial: Partial) -> tuple[str, str]:
    """The window's third commit, and a commit on a branch of the source
    that replaces it: its parent's tree without f.py, so under ``git
    replace`` the third commit deletes f.py, which fires f.py's path claim
    and implicates every commit carrying its lines. Made before the clone,
    which fetches it."""
    source = partial.source
    third, parent = partial.commits[2], partial.commits[1]
    _plain(source, "read-tree", parent)
    _plain(source, "update-index", "--force-remove", "--", _F)
    tree = _plain(source, "write-tree")
    _plain(source, "read-tree", "HEAD")
    when = _VERIFIED + timedelta(hours=3)
    replacement = _plain(
        source, "commit-tree", tree, "-p", parent, "-m", "R", when=when
    )
    _plain(source, "branch", "replacement", replacement)
    return third, replacement


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize("config", [False, True], ids=["ref", "ref-and-config"])
def test_a_replace_ref_changes_no_full_clone_count(
    tmp_path: Path, basis: str, config: bool
) -> None:
    """A replace ref is the repository's own, untrusted, and rewrites what
    a commit changed under a head that stays. Claim drift reads the history
    as its objects hold it, whatever ``core.useReplaceRefs`` the repository
    sets (`origin._AS_STORED`, `origin._AS_STORED_CONFIG`): f.py is edited in
    every commit and deleted in none, so nothing escalates before the ref
    or after it, where git's own diff shows the deletion."""
    partial = Partial(tmp_path, _EDITS)
    third, replacement = _replacement(partial)
    repo = partial.clone(blobless=False)
    before = partial.resolve([_F], basis)
    assert (before.count, before.claims_drifted) == (0, ())
    _plain(repo, "replace", third, replacement)
    if config:
        _plain(repo, "config", "core.useReplaceRefs", "true")
    shown = _plain(repo, "diff-tree", "-r", "--name-status", third)
    assert f"D\t{_F}" in shown.splitlines(), "premise: git's own diff replaces it"
    _caches.clear_all()
    after = partial.resolve([_F], basis)
    assert (after.count, after.claims_drifted) == (0, ())


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize("how", ["replace", "grafts"])
def test_a_rewritten_parent_changes_no_window(
    tmp_path: Path, basis: str, how: str
) -> None:
    """A replacement, or a line of the grafts file, that makes the deletion
    a child of the first edit drops the second edit from what git's own log
    reaches. The window is read past both, so it keeps all three commits,
    each carrying gone.py's lines: 3, as before the rewrite."""
    partial = Partial(tmp_path, _PATH)
    repo = partial.clone(blobless=False)
    deletion, first = partial.commits[2], partial.commits[0]
    if how == "replace":
        _plain(repo, "replace", "--graft", deletion, first)
    else:
        (repo / ".git" / "info" / "grafts").write_text(f"{deletion} {first}\n")
    reached = _plain(repo, "log", "--format=%H").split()
    assert partial.commits[1] not in reached, "premise: git's own log drops it"
    resolved = partial.resolve([_GONE], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_GONE,))


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    "alone", [pytest.param("setting", marks=needs_use_replace_refs), "variable"]
)
def test_each_replace_pin_alone_holds_the_window(
    tmp_path: Path, basis: str, alone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every origin call carries two pins against replace refs, and either
    alone keeps the window past a replacement: 3. With no
    ``GIT_NO_REPLACE_OBJECTS`` and the repository setting
    ``core.useReplaceRefs=true``, git reads replace refs, as a git before
    2.42 would even with the variable set; the ``-c
    core.useReplaceRefs=false`` (`origin._AS_STORED_CONFIG`) is read after
    the repository's config on every git, so it holds. With no ``-c``, the
    variable holds, as it does alone on a git before 2.19, which has no
    such setting."""
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    if alone == "setting":
        monkeypatch.setattr(origin, "_AS_STORED", {"GIT_GRAFT_FILE": os.devnull})
    else:
        monkeypatch.setattr(origin, "_AS_STORED_CONFIG", ())
    partial = Partial(tmp_path, _PATH)
    repo = partial.clone(blobless=False)
    deletion, first = partial.commits[2], partial.commits[0]
    _plain(repo, "replace", "--graft", deletion, first)
    if alone == "setting":
        _plain(repo, "config", "core.useReplaceRefs", "true")
    reached = _plain(repo, "log", "--format=%H").split()
    assert partial.commits[1] not in reached, "premise: git's own log drops it"
    resolved = partial.resolve([_GONE], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_GONE,))


@needs_lazy_fetch_pin
@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    "reftable",
    [pytest.param(True, marks=needs_reftable), False],
    ids=["reftable", "files"],
)
def test_a_replace_ref_after_the_split_is_kept_moves_no_count(
    tmp_path: Path, basis: str, reftable: bool
) -> None:
    """ENG-220's review: in a reftable ref store a replace ref moves no
    file the split's memo stamped, so the kept listing read the history
    without it while a fresh one read it with it: 3, then 5 and f.py named
    once the memo was emptied. Every call now reads the objects as stored,
    so the count kept, the count after the ref and the count read afresh
    are one: the three commits that read a blob the clone lacks, never
    below the full clone's 0."""
    partial = Partial(tmp_path, _EDITS)
    third, replacement = _replacement(partial)
    repo = partial.clone(reftable=reftable)
    partial.fetch(0, 1, 2)
    assert not partial.held(partial.blobs[3][_F]), "premise"
    first = partial.resolve([_F], basis)
    assert (first.count, first.claims_drifted) == (3, ())
    if githead.gitdir_at(repo) is not None:
        assert origin_patch._MEMO, "premise: the split's listing is kept"
    _plain(repo, "replace", third, replacement)
    kept = partial.resolve([_F], basis)
    _caches.clear_all()
    fresh = partial.resolve([_F], basis)
    assert [(r.count, r.claims_drifted) for r in (kept, fresh)] == [(3, ())] * 2


def test_a_window_whose_blobs_are_all_local_is_not_split(tmp_path: Path) -> None:
    """Every blob local: the one-call stream did not fail for want of a
    blob, so the split does not answer for it, and the caller keeps the
    per-file count."""
    partial = Partial(tmp_path, _SYMBOL)
    repo = partial.clone(blobless=False)
    assert local_patch_window(repo, partial.commits, ["src/mod.py"]) is None


def _split_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream: str,
    claims: list[str],
) -> tuple[list[str], set[str] | None]:
    """`_weak_tier_evaluation` over commits a, b and c, the stream failing
    and the split streaming a and b and counting c."""
    window = PatchWindow(
        stream=stream,
        counted=frozenset({"c" * 40}),
        touched=frozenset(),
        deleted=frozenset(),
    )
    monkeypatch.setattr(verify_mod, "commit_patch_stream", lambda *a, **k: None)
    monkeypatch.setattr(verify_mod, "local_patch_window", lambda *a, **k: window)
    shas = ["a" * 40, "b" * 40, "c" * 40]
    specs = sorted({parse_claim(raw).rel_path for raw in claims})
    return verify_mod._weak_tier_evaluation(
        tmp_path, shas, specs, load_claims(claims), None
    )


def _deletion(path: str, line: str = "") -> str:
    hunk = f"@@ -1 +0,0 @@\n-{line}\n" if line else ""
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        f"{hunk}"
    )


def test_the_whole_window_rule_reads_the_local_commits_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A claim fired over the local commits that implicates none of them
    (a deletion with no lines) counts the whole window, the counted
    commits included, as it would over the whole window."""
    stream = f"{COMMIT_MARK}{'a' * 40}\n{_deletion(_X)}{COMMIT_MARK}{'b' * 40}\n"
    drifted, implicated = _split_evaluation(monkeypatch, tmp_path, stream, [_X])
    assert drifted == [_X]
    assert implicated == {"a" * 40, "b" * 40, "c" * 40}


def test_in_a_split_one_bare_claim_takes_the_whole_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """x fires implicating nothing while y implicates a: in a split the
    whole window is counted all the same, since y may have fired on a mark
    a full clone does not see, where x alone would have taken the whole
    window."""
    stream = (
        f"{COMMIT_MARK}{'a' * 40}\n{_deletion(_Y, _long(0).strip())}"
        f"{COMMIT_MARK}{'b' * 40}\n{_deletion(_X)}"
    )
    drifted, implicated = _split_evaluation(monkeypatch, tmp_path, stream, [_X, _Y])
    assert sorted(drifted) == [_X, _Y]
    assert implicated == {"a" * 40, "b" * 40, "c" * 40}


def test_the_raw_listing_reads_gitlinks_typechanges_and_quoted_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gitlink's object is a commit of the submodule, never a blob of the
    repository, so it is not asked for; a typechange keeps its status; a
    path git still quotes is read as the path it names."""
    sha = "d" * 40
    blob = "e" * 40
    module = "f" * 40
    raw = (
        f"{COMMIT_MARK}{sha}\n\n"
        f":160000 160000 {module} {'1' * 40} M\tvendor/lib\n"
        f":100644 160000 {blob} {module} T\tsrc/f.py\n"
        f':000000 100644 {"0" * 40} {blob} A\t"src/we\\"ird.py"\n'
    )
    monkeypatch.setattr(origin, "_git", lambda *a, **k: raw)
    changes = origin_patch._changes(tmp_path, [sha], ["src"])
    assert changes == {
        sha: [
            origin_patch.Change("vendor/lib", "M", ()),
            origin_patch.Change("src/f.py", "T", (blob,)),
            origin_patch.Change('src/we"ird.py', "A", (blob,)),
        ]
    }


def test_the_claims_the_tests_name_are_governed() -> None:
    assert parse_claim(_FOO).kind == "symbol"
    assert parse_claim(_F).kind == "path"
    assert parse_claim(_Z).kind == "symbol"
