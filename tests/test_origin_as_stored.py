"""Claim drift reads the history as its objects hold it (ENG-220's review).

A repository's commit-graph and shallow file are its own, written by git
for speed or left by a shallow clone, and a hostile ``.git`` can forge
either. The graph names each commit's parents, root tree and changed-path
Bloom filter in place of the objects, and git read it for the windows while
the patch read the objects: a forged graph hid the commit that changed a
claim, and the count read 0. No origin call reads it now
(`origin_history.AS_STORED_CONFIG`). The shallow file cuts the history at the
commits it lists, behind which git reads nothing: a range that holds a cut
is not walked, and a shallow repository's dates are not read
(`origin_history.cuts`), so no count is read where the commits behind the
cut may be the window's, and the row reads unread
(`origin_history.HistoryUnread.SHALLOW`), never caught up.

Each repository is built with git's plumbing from `test_origin_patch`'s
helpers; the graph is forged by rewriting its chunks in place.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import time
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from bettermemory import _caches, _git, origin, origin_dates, origin_history, verify
from bettermemory.claims import load_claims
from bettermemory.verify import ResolvedCommitDrift
from bettermemory.verify_basis import CommitDriftUnread

from .test_origin_patch import (  # noqa: F401
    _BASES,
    _FOO,
    _SYMBOL,
    _T0,
    _VERIFIED,
    History,
    Partial,
    _git_version,
    _module,
    _object,
    _plain,
    neutral_git,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

needs_changed_paths = pytest.mark.skipif(
    _git_version() < (2, 27), reason="git writes changed-path filters from 2.27"
)

_MOD = "src/mod.py"
#: foo's def line changes in the third window commit, after two edits of
#: bar's body, so a shallow clone three commits deep is cut just before it.
_LATE: History = [
    {_MOD: _module("", 0)},
    {_MOD: _module("", 1)},
    {_MOD: _module("", 2)},
    {_MOD: _module("flag=False", 2)},
    {_MOD: _module("flag=False", 3)},
]


def _chunks(data: bytes | bytearray) -> dict[bytes, tuple[int, int]]:
    """The commit-graph's chunks, by id, as (start, end) offsets."""
    assert data[:4] == b"CGPH"
    table = [
        (
            bytes(data[8 + 12 * i : 12 + 12 * i]),
            struct.unpack(">Q", data[12 + 12 * i : 20 + 12 * i])[0],
        )
        for i in range(data[6] + 1)
    ]
    return {cid: (start, table[i + 1][1]) for i, (cid, start) in enumerate(table[:-1])}


def _forge_graph(
    repo: Path,
    *,
    parent: tuple[str, str] | None = None,
    tree: tuple[list[str], str] | None = None,
    blind: bool = False,
) -> None:
    """Rewrite the commit-graph in place: a commit's first parent, the root
    tree of some commits, or every changed-path filter emptied, so each
    reads as changing no path."""
    path = repo / ".git" / "objects" / "info" / "commit-graph"
    data = bytearray(path.read_bytes())
    chunks = _chunks(data)
    start, end = chunks[b"OIDL"]
    oids = [bytes(data[i : i + 20]).hex() for i in range(start, end, 20)]
    commits = chunks[b"CDAT"][0]
    if parent is not None:
        child, new = parent
        struct.pack_into(
            ">I", data, commits + oids.index(child) * 36 + 20, oids.index(new)
        )
    if tree is not None:
        shas, root = tree
        for sha in shas:
            entry = commits + oids.index(sha) * 36
            data[entry : entry + 20] = bytes.fromhex(root)
    if blind:
        start, end = chunks[b"BDAT"]
        data[start + 12 : end] = bytes(end - start - 12)
    path.chmod(0o644)
    path.write_bytes(bytes(data))


def _touching(repo: Path) -> list[str]:
    """The commits the user's own git lists as changing src/mod.py."""
    return _plain(repo, "log", "--format=%H", "--", _MOD).split()


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    "forgery", ["parent", "tree", pytest.param("filter", marks=needs_changed_paths)]
)
def test_a_forged_commit_graph_moves_no_count(
    tmp_path: Path, basis: str, forgery: str
) -> None:
    """The first window commit changes foo's def line: 1, foo named, in a
    full clone. A graph that gives the second commit the anchor for its
    parent, gives every window commit the anchor's tree, or empties every
    changed-path filter hides that commit from the user's own git, and
    hid it from the windows while the patch read the objects: the count
    read 0. Read past the graph, the count is 1 again."""
    partial = Partial(tmp_path, _SYMBOL)
    repo = partial.clone(blobless=False)
    write = ["commit-graph", "write", "--reachable"]
    _plain(repo, *write, *(["--changed-paths"] if forgery == "filter" else []))
    changed = partial.commits[0]
    assert changed in _touching(repo), "premise: the graph is honest"
    if forgery == "parent":
        _forge_graph(repo, parent=(partial.commits[1], partial.anchor))
        assert changed not in _plain(repo, "log", "--format=%H").split(), "premise"
    elif forgery == "tree":
        root = _plain(repo, "rev-parse", f"{partial.anchor}^{{tree}}")
        _forge_graph(repo, tree=(partial.commits, root))
        # git before 2.38 lists the commits past the graph's trees (ENG-227).
        if _git_version() >= (2, 38):
            assert not set(partial.commits) & set(_touching(repo)), "premise"
    else:
        _forge_graph(repo, blind=True)
        assert changed not in _touching(repo), "premise"
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


def _commit(
    repo: Path, files: dict[str, str | tuple[str, str] | None], hours: float
) -> str:
    """A commit of `files` (None deletes one, ``("gitlink", commit)`` is a
    gitlink) dated `hours` after the verify, or before it where negative."""
    _stage(repo, files)
    when = _VERIFIED + timedelta(hours=hours)
    _plain(repo, "commit", "-q", "--allow-empty", "-m", f"at {hours}", when=when)
    return _plain(repo, "rev-parse", "HEAD")


def _stage(repo: Path, files: Mapping[str, str | tuple[str, str] | None]) -> None:
    for rel, text in files.items():
        if text is None:
            _plain(repo, "update-index", "--force-remove", "--", rel)
        elif isinstance(text, tuple):
            info = f"160000,{text[1]},{rel}"
            _plain(repo, "update-index", "--add", "--cacheinfo", info)
        else:
            oid = _object(repo, text.encode())
            _plain(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{rel}")


def _hit(
    repo: Path,
    claims: list[str],
    verified_head: str | None,
    anchors: tuple[str, ...] = (),
) -> ResolvedCommitDrift | None:
    """One search hit's drift, cold, as the recall path resolves it: on
    the walk from `verified_head` where it answers, else on dates."""
    got = _unread_or_hit(repo, claims, verified_head, anchors)
    assert not isinstance(got, CommitDriftUnread), "the history was read"
    return got


def _unread_or_hit(
    repo: Path,
    claims: list[str],
    verified_head: str | None,
    anchors: tuple[str, ...] = (),
) -> ResolvedCommitDrift | CommitDriftUnread | None:
    """`_hit`, where the history may not be read in time."""
    _caches.clear_all()
    return verify.resolve_hit_drift(
        cwd=repo,
        toplevel=repo,
        head=_plain(repo, "rev-parse", "HEAD"),
        timestamps=lambda: origin_dates.commit_author_timestamps(repo),
        anchors=anchors,
        claims=load_claims(claims),
        since=_VERIFIED,
        verified_head=verified_head,
        walked={},
    )


def _backdated(repo: Path) -> tuple[str, str, list[str]]:
    """foo's def line changes after the verify, under a commit authored
    before it (a rebased one): cut there, the change is behind the cut and
    the cut before the window's dates."""
    anchor = _commit(repo, {_MOD: _module("", 0)}, -1)
    _commit(repo, {_MOD: _module("flag=False", 0)}, 1)
    cut = _commit(repo, {_MOD: _module("flag=False", 1)}, -0.5)
    _commit(repo, {_MOD: _module("flag=False", 2)}, 3)
    return anchor, cut, [_FOO]


def _side(repo: Path) -> tuple[str, str, list[str]]:
    """A side branch deletes src/mod.py and adds it back, and is merged:
    cut at the commit that adds it back, the deletion that fires the path
    claim is behind the cut, which the walk from the anchor still reaches
    through the main line."""
    anchor = _commit(repo, {_MOD: _module("", 0), "src/keep.py": "k = 1\n"}, -1)
    _plain(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {_MOD: None}, 1)
    cut = _commit(repo, {_MOD: _module("", 0)}, 2)
    _plain(repo, "checkout", "-q", "-f", "-")
    _plain(repo, "read-tree", "--reset", "HEAD")
    _commit(repo, {"src/other.py": "o = 1\n"}, 3)
    tree = _plain(repo, "merge-tree", "--write-tree", "HEAD", "side").split()[0]
    when = _VERIFIED + timedelta(hours=4)
    merge = _plain(
        repo, "commit-tree", tree, "-p", "HEAD", "-p", "side", "-m", "m", when=when
    )
    _plain(repo, "reset", "-q", "--hard", merge)
    return anchor, cut, [_MOD]


@pytest.mark.skipif(_git_version() < (2, 38), reason="git merge-tree --write-tree")
@pytest.mark.parametrize("build", [_backdated, _side], ids=["backdated", "side"])
def test_a_forged_shallow_file_reads_no_count_behind_its_cut(
    tmp_path: Path, build: object
) -> None:
    """Without the cut the walk counts the change. A shallow file written
    by hand cuts the history where it hides the change: before, the walk
    declined (the anchor behind the cut) or walked past it (the cut a root
    on the side branch), the dates missed it, and the count read 0 with
    nothing named, from the stamp's head and without one. Now no count is
    read behind a cut: the hit's count is omitted, never a measured 0, and
    the hit reads unread for the cut (`SHALLOW`), never not applicable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    anchor, cut, claims = build(repo)  # type: ignore[operator]
    truth = _hit(repo, claims, anchor)
    assert truth is not None and truth.count >= 1 and truth.claims_drifted
    (repo / ".git" / "shallow").write_text(f"{cut}\n")
    assert len(_plain(repo, "log", "--format=%H").split()) < 5, "premise: cut"
    shallow = CommitDriftUnread.SHALLOW
    assert _unread_or_hit(repo, claims, anchor) is shallow
    assert _unread_or_hit(repo, claims, None) is shallow


def test_a_shallow_clone_counts_where_the_range_holds_no_cut(tmp_path: Path) -> None:
    """A clone three commits deep is cut at the commit before the change.
    Stamped there, the walk's range holds the change and the commit after
    it and no cut, and counts as a full clone does: 1, foo named: the
    window is wholly inside the shallow history. Stamped at a commit behind
    the cut, the window crosses it and no count is read: the hit is unread
    for the cut (`SHALLOW`)."""
    partial = Partial(tmp_path, _LATE)
    full = tmp_path / "full"
    _plain(tmp_path, "clone", "-q", partial.source.as_uri(), str(full))
    shallow = tmp_path / "shallow"
    _plain(tmp_path, "clone", "-q", "--depth=3", partial.source.as_uri(), str(shallow))
    held, behind = partial.commits[1], partial.commits[0]
    assert (shallow / ".git" / "shallow").read_text().split() == [held], "premise"
    for repo in (full, shallow):
        got = _hit(repo, [_FOO], held)
        assert got is not None and (got.count, got.claims_drifted) == (1, (_FOO,))
    assert _hit(full, [_FOO], behind) is not None
    assert _unread_or_hit(shallow, [_FOO], behind) is CommitDriftUnread.SHALLOW


def test_the_cut_is_read_from_the_common_directory(tmp_path: Path) -> None:
    """`origin_history.cuts` reads the shallow file git reads: empty with
    none, its commits with one, and None for a root holding no
    repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    assert origin_history.cuts(repo) == frozenset()
    (repo / ".git" / "shallow").write_text("A" * 40 + "\n" + "b" * 40 + "\n")
    assert origin_history.cuts(repo) == frozenset({"a" * 40, "b" * 40})
    assert origin_history.cuts(tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------
# What the repository sets moves no count (ENG-220's settings audit)
# ---------------------------------------------------------------------------

_BESIDE = "src/two.py"
#: Claimed beside each case's claim and touched by no commit, while bar's
#: edit puts two.py in the patch the weak tier indexes: a claimed file the
#: patch leaves out then reads as untouched, where alone it fails the parse
#: and the hit keeps the per-file count.
_QUIET = f"{_BESIDE}::foo"


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    return repo


def _both(
    repo: Path, claims: list[str], anchor: str, anchors: tuple[str, ...] = ()
) -> list[tuple[int, tuple[str, ...]] | None]:
    """The hit's count and the claims it names, cold, on the walk from
    `anchor` and on dates."""
    out: list[tuple[int, tuple[str, ...]] | None] = []
    for head in (anchor, None):
        got = _hit(repo, claims, head, anchors)
        out.append(None if got is None else (got.count, got.claims_drifted))
    return out


def test_a_root_commit_is_read_as_its_creation(tmp_path: Path) -> None:
    """An unrelated history merged after the verify brings src/mod.py with
    foo's def line changed, from its root commit: 1, foo named, on both
    bases. With log.showRoot = false git diffs no root commit: the walk
    listed no path for it and the patch nothing, so with two.py's edit
    indexed beside it foo read as untouched, and the count 0. Every origin
    call pins it (`origin_history.AS_STORED_CONFIG`)."""
    repo = _repository(tmp_path)
    anchor = _commit(repo, {_MOD: _module("", 0), _BESIDE: _module("", 0)}, -1)
    _commit(repo, {_BESIDE: _module("", 1)}, 1)
    branch = _plain(repo, "symbolic-ref", "--short", "HEAD")
    _plain(repo, "checkout", "-q", "--orphan", "imported")
    _plain(repo, "read-tree", "--empty")
    root = _commit(repo, {_MOD: _module("flag=False", 0)}, 2)
    _plain(repo, "checkout", "-q", "-f", branch)
    _plain(repo, "read-tree", "--reset", "HEAD")
    blob = _object(repo, _module("flag=False", 0).encode())
    _plain(repo, "update-index", "--cacheinfo", f"100644,{blob},{_MOD}")
    tree = _plain(repo, "write-tree")
    when = _VERIFIED + timedelta(hours=3)
    merge = _plain(
        repo, "commit-tree", tree, "-p", "HEAD", "-p", root, "-m", "m", when=when
    )
    _plain(repo, "reset", "-q", "--hard", merge)
    claims = [_FOO, _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (_FOO,))] * 2
    _plain(repo, "config", "log.showRoot", "false")
    assert _both(repo, claims, anchor) == truth


def test_a_claimed_path_is_not_followed_across_a_rename(tmp_path: Path) -> None:
    """src/mod.py is deleted after the verify, and lib/x.py, the same module
    with another bar, is then renamed into its place: foo's lines leave
    with the deletion and come back with the creation, 2 on both bases.
    With log.follow set, a log over the one claimed path followed it back
    across the rename to lib/x.py: the dates' window lost the deletion, and
    the patch read the renaming commit as an edit of lib/x.py whose one
    hunk is bar's, so the count read 0. Every origin call pins it off
    (`origin_history.AS_STORED_CONFIG`)."""
    repo = _repository(tmp_path)
    anchor = _commit(repo, {_MOD: _module("", 0), "lib/x.py": _module("", 1)}, -1)
    _commit(repo, {_MOD: None}, 1)
    _commit(repo, {"lib/x.py": None, _MOD: _module("", 2)}, 2)
    truth = _both(repo, [_FOO], anchor)
    assert truth == [(2, (_FOO,))] * 2
    _plain(repo, "config", "log.follow", "true")
    assert _both(repo, [_FOO], anchor) == truth


@pytest.mark.parametrize("renames", [None, "true", "copies"])
def test_a_rename_is_read_as_a_deletion_and_a_creation(
    tmp_path: Path, renames: str | None
) -> None:
    """src/mod.py is renamed to src/new.py after the verify, its text
    unchanged, and two.py's bar is edited: foo's lines leave src/mod.py,
    and src/new.py, claimed absent, is created. Git's own default, and
    diff.renames set either way, printed the rename as a header without
    hunks: neither line reached the weak tier, which, with two.py indexed
    beside it, read both claims as untouched, 0. Every origin call pins
    renames off (`origin_history.AS_STORED_CONFIG`)."""
    repo = _repository(tmp_path)
    anchor = _commit(repo, {_MOD: _module("", 0), _BESIDE: _module("", 0)}, -1)
    _commit(repo, {_MOD: None, "src/new.py": _module("", 0)}, 1)
    _commit(repo, {_BESIDE: _module("", 1)}, 2)
    claims = [_FOO, "!src/new.py", _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (_FOO, "!src/new.py"))] * 2
    if renames is not None:
        _plain(repo, "config", "diff.renames", renames)
    assert _both(repo, claims, anchor) == truth


@pytest.mark.parametrize(
    "setting", ["-diff", "binary", "a binary driver", "core.bigFileThreshold"]
)
def test_a_claimed_file_is_diffed_as_text(tmp_path: Path, setting: str) -> None:
    """foo's def line changes after the verify, and two.py's bar is edited:
    1, foo named, on both bases. An attribute that turns the diff off, a
    diff driver marked binary, or a blob over core.bigFileThreshold made
    git print "Binary files differ" for src/mod.py, with no header and no
    hunk, so with two.py indexed beside it foo read as untouched, 0. Every
    file is diffed as text (`origin_history.AS_STORED_DIFF`)."""
    repo = _repository(tmp_path)
    anchor = _commit(repo, {_MOD: _module("", 0), _BESIDE: _module("", 0)}, -1)
    changed = _commit(repo, {_MOD: _module("flag=False", 0)}, 1)
    _commit(repo, {_BESIDE: _module("", 1)}, 2)
    claims = [_FOO, _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (_FOO,))] * 2
    attributes = repo / ".git" / "info" / "attributes"
    attributes.parent.mkdir(exist_ok=True)
    if setting == "a binary driver":
        attributes.write_text(f"{_MOD} diff=opaque\n")
        _plain(repo, "config", "diff.opaque.binary", "true")
    elif setting == "core.bigFileThreshold":
        _plain(repo, "config", "core.bigFileThreshold", "1")
    else:
        attributes.write_text(f"{_MOD} {setting}\n")
    shown = _plain(repo, "log", "-p", "--format=", "-1", changed, "--", _MOD)
    assert "Binary files" in shown, "premise: git's own patch reads it as binary"
    assert _both(repo, claims, anchor) == truth


@pytest.mark.parametrize("where", ["diff.ignoreSubmodules", ".gitmodules"])
def test_a_gitlinks_change_is_never_ignored(tmp_path: Path, where: str) -> None:
    """The gitlink src/sub is deleted after the verify, and two.py's bar is
    edited: the path claim on src/sub fires, 1 on both bases. With
    diff.ignoreSubmodules = all, or submodule.<name>.ignore = all in the
    working tree's .gitmodules, git dropped the gitlink's change from the
    walk's listing and from the patch: the walk's window lost it, and with
    two.py indexed beside it the dates' patch read the claim as untouched,
    0. Every log that prints a diff pins `--ignore-submodules=none`
    (`origin_history.AS_STORED_DIFF`), the one spelling that outranks
    .gitmodules."""
    repo = _repository(tmp_path)
    sub = "src/sub"
    gitlink = ("gitlink", "1" * 40)
    anchor = _commit(repo, {sub: gitlink, _BESIDE: _module("", 0)}, -1)
    _commit(repo, {sub: None}, 1)
    _commit(repo, {_BESIDE: _module("", 1)}, 2)
    claims = [sub, _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (sub,))] * 2
    if where == ".gitmodules":
        (repo / ".gitmodules").write_text(
            f'[submodule "sub"]\n\tpath = {sub}\n\turl = ./sub\n\tignore = all\n'
        )
    else:
        _plain(repo, "config", "diff.ignoreSubmodules", "all")
    shown = _plain(repo, "log", "--format=", "--name-only", "-2")
    assert sub not in shown.split(), "premise: git's own log drops the gitlink"
    assert _both(repo, claims, anchor) == truth


_APP = "src/app.py"


def _arranged(*names: str) -> str:
    """A module of `return 1` functions in the given order. From (handler,
    other) to (other, handler, a, b) and on to (a, handler, other, b), git's
    default algorithm leaves handler's lines alone and patience moves
    them."""
    head = '"""Module under claim."""\n\nTIMEOUT = 30\n'
    return head + "".join(f"\n\ndef {name}():\n    return 1\n" for name in names)


@pytest.mark.parametrize("name", _git.RESHAPING_ENV)
def test_the_environment_reshapes_no_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """foo's def line changes after the verify while handler's functions
    are rearranged around it: 1, foo named, on both bases. In the
    environment of the process that reads it, ``GIT_DIFF_OPTS`` put
    context lines in the ``-U0`` patch, which the parser reads as a
    mismatch, so the hit fell back to the per-file count with nothing
    named, and ``GIT_ATTR_SOURCE`` read the attributes from a tree, here
    one naming a driver whose algorithm is patience, so handler read
    touched. ``GIT_EXTERNAL_DIFF`` and the counters git hands an external
    diff move nothing past ``--no-ext-diff``. Each is removed from every
    git the package runs (`_git.RESHAPING_ENV`)."""
    if name == "GIT_ATTR_SOURCE" and _git_version() < (2, 42):
        pytest.skip("git reads GIT_ATTR_SOURCE and a driver's algorithm from 2.42")
    repo = _repository(tmp_path)
    _plain(repo, "config", "diff.lever.algorithm", "patience")
    anchor = _commit(
        repo, {_APP: _arranged("handler", "other"), _MOD: _module("", 0)}, -1
    )
    first = _commit(
        repo,
        {_APP: _arranged("other", "handler", "a", "b"), _MOD: _module("flag=False", 0)},
        1,
    )
    _commit(repo, {_APP: _arranged("a", "handler", "other", "b")}, 2)
    claims = [f"{_APP}::handler", _FOO]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (_FOO,))] * 2
    blob = _object(repo, b"*.py diff=lever\n")
    tree = (
        subprocess.run(
            ["git", "mktree"],
            cwd=repo,
            input=f"100644 blob {blob}\t.gitattributes\n".encode(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    value = {
        "GIT_DIFF_OPTS": "--unified=3",
        "GIT_ATTR_SOURCE": tree,
        "GIT_EXTERNAL_DIFF": "false",
    }.get(name, "1")
    monkeypatch.setenv(name, value)
    if name in ("GIT_DIFF_OPTS", "GIT_ATTR_SOURCE"):
        shown = _plain(repo, "log", "-p", "-U0", "--format=", "-1", first, "--", _APP)
        assert "-def handler():" in shown or "\n " in shown, "premise: git reshapes it"
    assert _both(repo, claims, anchor) == truth


def _index_info(repo: Path, entries: list[tuple[str, str, str]]) -> None:
    """Stage `entries` (mode, object, path) from git's standard input, so
    no argument of git's is precomposed on the way in."""
    subprocess.run(
        ["git", "-c", "core.precomposeUnicode=false", "update-index", "--index-info"],
        cwd=repo,
        input="".join(f"{m} {o}\t{p}\n" for m, o, p in entries).encode("utf-8"),
        check=True,
    )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="git precomposes its arguments on macOS only"
)
def test_a_decomposed_path_is_matched_as_stored(tmp_path: Path) -> None:
    """A tree that stores ``src/café.py`` decomposed (``e`` and a combining
    accent, as a git that did not precompose committed it) deletes it
    after the verify, and two.py's bar is edited; a path claim spelled as
    the tree stores it fires, 2 on both bases. With
    ``core.precomposeUnicode=true`` git rewrote the claim's pathspec to the
    composed spelling, which names no path the tree holds: the logs and the
    patch missed the file, and the claim read 0. The logs that take a
    claim's paths pin it off (`origin_history.AS_STORED_PATHS`)."""
    repo = _repository(tmp_path)
    stored = unicodedata.normalize("NFD", "src/café.py")
    body = _object(repo, _module("", 0).encode())
    _index_info(repo, [("100644", body, stored), ("100644", body, _BESIDE)])
    when = _VERIFIED - timedelta(hours=1)
    _plain(repo, "commit", "-q", "-m", "anchor", when=when)
    anchor = _plain(repo, "rev-parse", "HEAD")
    _index_info(repo, [("0", "0" * 40, stored)])
    _plain(repo, "commit", "-q", "-m", "gone", when=_VERIFIED + timedelta(hours=1))
    _commit(repo, {_BESIDE: _module("", 1)}, 2)
    claims = [stored, _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(2, (stored,))] * 2
    _plain(repo, "config", "core.precomposeUnicode", "true")
    shown = _plain(repo, "log", "--format=%H", "--", f":(literal){stored}")
    assert not shown, "premise: git's own log misses the decomposed path"
    assert _both(repo, claims, anchor) == truth


#: A claimed file whose folder and name each hold an accent, in each
#: spelling a tree may store: composed, decomposed, and a folder a
#: decomposing filesystem named holding a file another tool named.
_ACCENTED = "src/dé/café.py"
_SPELLED = {
    "NFC": unicodedata.normalize("NFC", _ACCENTED),
    "NFD": unicodedata.normalize("NFD", _ACCENTED),
    "mixed": unicodedata.normalize("NFD", "src/dé/")
    + unicodedata.normalize("NFC", "café.py"),
}
#: What the claim counts: foo's def line changed, the one commit; the file
#: deleted, the whole window, since its lines are too short to name the
#: commit that removed them (`verify._weak_tier_evaluation`).
_COUNTED = {"edit": 1, "delete": 2}


def _accented(repo: Path, stored: list[str], change: str) -> str:
    """The files `stored` before the verify, with two.py, then `change`
    made to the last of them after it, then bar's edit in two.py; the
    anchor. Staged from git's standard input, so each path is stored as
    spelled."""
    body = _object(repo, _module("", 0).encode())
    _index_info(
        repo, [("100644", body, path) for path in stored] + [("100644", body, _BESIDE)]
    )
    _plain(repo, "commit", "-q", "-m", "anchor", when=_VERIFIED - timedelta(hours=1))
    anchor = _plain(repo, "rev-parse", "HEAD")
    if change == "delete":
        _index_info(repo, [("0", "0" * 40, stored[-1])])
    else:
        text = _module("flag=False", 0) if change == "edit" else _module("", 1)
        _index_info(repo, [("100644", _object(repo, text.encode()), stored[-1])])
    _plain(repo, "commit", "-q", "-m", change, when=_VERIFIED + timedelta(hours=1))
    _commit(repo, {_BESIDE: _module("", 1)}, 2)
    return anchor


@pytest.mark.parametrize("change", ["edit", "delete"])
@pytest.mark.parametrize(
    ("claimed", "stored"),
    [
        ("NFC", "NFD"),
        ("NFD", "NFC"),
        ("NFC", "mixed"),
        ("NFD", "mixed"),
        ("NFC", "NFC"),
        ("NFD", "NFD"),
    ],
)
def test_a_claim_reads_the_tree_s_spelling_of_its_path(
    tmp_path: Path, claimed: str, stored: str, change: str
) -> None:
    """A claim spelled composed against a tree that stores its path
    decomposed (a git on macOS that did not precompose committed it, with
    ``core.precomposeUnicode`` off or before git had it), or the reverse:
    foo's def line changes after the verify, or the file is deleted, and
    two.py's bar is edited. The claim fires on both bases, as where it is
    spelled as stored (`_COUNTED`). It matched no path: the logs and the
    walk missed the file, the patch named it otherwise, and the claim read
    0. A spec the tree does not hold under its own spelling is looked for
    in each other (`verify_spelling.spelled`), and a claim read under each
    the stream holds (`verify._index_paths`)."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [_SPELLED[stored]], change)
    claim = _SPELLED[claimed] + ("::foo" if change == "edit" else "")
    if claimed != stored:
        spec = f":(literal){_SPELLED[claimed]}"
        pinned = ("-c", "core.precomposeUnicode=false")
        shown = _plain(repo, *pinned, "log", "--format=%H", "--", spec)
        assert not shown, "premise: git's own log misses the claimed spelling"
    assert _both(repo, [claim, _QUIET], anchor) == [(_COUNTED[change], (claim,))] * 2


@pytest.mark.parametrize("on", ["changed", "kept"])
@pytest.mark.parametrize("change", ["edit", "delete"])
@pytest.mark.parametrize("claimed", ["NFC", "NFD"])
def test_twins_read_each_claim_on_its_own_file(
    tmp_path: Path, claimed: str, change: str, on: str
) -> None:
    """A tree that stores both spellings of one name holds two files
    (twins), which a filesystem that ignores normalisation shows as one;
    `change` is made to one of them. A claim spelled as the changed twin
    fires (`_COUNTED`) on both bases. One spelled as the kept twin reads 0
    on the walk, which reads a claim under its own spelling where the
    anchor's tree, the tree it was verified against, holds it (ENG-220's
    review, round C, and round D for the tree); read under every spelling,
    the change to the other twin fired it too. Dates have no such tree and
    read every spelling, so there the kept twin's claim fires as the
    changed one's does: an over-count on the fallback basis, never an
    undercount (`verify_spelling.spelled`)."""
    other = "NFD" if claimed == "NFC" else "NFC"
    repo = _repository(tmp_path)
    twins = [_SPELLED[other], _SPELLED[claimed]]
    anchor = _accented(repo, twins if on == "changed" else twins[::-1], change)
    claim = _SPELLED[claimed] + ("::foo" if change == "edit" else "")
    fired = (_COUNTED[change], (claim,))
    walked = fired if on == "changed" else (0, ())
    assert _both(repo, [claim, _QUIET], anchor) == [walked, fired]


def _twins(repo: Path) -> str:
    """Both spellings of café.py stored as two files before the verify;
    after it foo's def line changes in the decomposed one, and then the
    composed one is deleted. The anchor."""
    body = _object(repo, _module("", 0).encode())
    _index_info(repo, [("100644", body, _SPELLED[form]) for form in ("NFC", "NFD")])
    _plain(repo, "commit", "-q", "-m", "anchor", when=_VERIFIED - timedelta(hours=1))
    anchor = _plain(repo, "rev-parse", "HEAD")
    edited = _object(repo, _module("flag=False", 0).encode())
    _index_info(repo, [("100644", edited, _SPELLED["NFD"])])
    _plain(repo, "commit", "-q", "-m", "edit", when=_VERIFIED + timedelta(hours=1))
    _index_info(repo, [("0", "0" * 40, _SPELLED["NFC"])])
    _plain(repo, "commit", "-q", "-m", "delete", when=_VERIFIED + timedelta(hours=2))
    return anchor


@pytest.mark.parametrize(
    ("claim", "walked", "dated"),
    [
        (_SPELLED["NFC"] + "::foo", 1, 2),
        (_SPELLED["NFD"] + "::foo", 1, 2),
        (_SPELLED["NFD"], 0, 1),
        (_SPELLED["NFC"], 1, 1),
    ],
)
def test_twins_one_edited_and_the_other_deleted_read_exactly(
    tmp_path: Path, claim: str, walked: int, dated: int
) -> None:
    """The review's twins (round C): foo's def line changes in the
    decomposed twin, and the composed one is deleted. On the walk, foo on
    either twin is its one commit, 1; the decomposed path, never deleted,
    0; the composed path, deleted, 1. Dates read every spelling (round D):
    foo on either reads both commits, 2, and the decomposed path 1 with
    the claim named, for the other file's deletion; never under the
    walk's."""
    repo = _repository(tmp_path)
    anchor = _twins(repo)
    assert _both(repo, [claim], anchor) == [
        (count, (claim,) if count else ()) for count in (walked, dated)
    ]


def _verified_decomposed(repo: Path, then: str, hours: float) -> str:
    """The anchor holds café.py decomposed alone, the file a composed
    claim on it was verified against; after the verify foo's def line
    changes in it, and then (`then`), `hours` after the verify, a composed
    twin without foo is added beside it, or the file is renamed to its
    composed spelling as it stands. The anchor."""
    body = _object(repo, _module("", 0).encode())
    _index_info(repo, [("100644", body, _SPELLED["NFD"])])
    _plain(repo, "commit", "-q", "-m", "anchor", when=_VERIFIED - timedelta(hours=1))
    anchor = _plain(repo, "rev-parse", "HEAD")
    edited = _object(repo, _module("flag=False", 0).encode())
    _index_info(repo, [("100644", edited, _SPELLED["NFD"])])
    _plain(repo, "commit", "-q", "-m", "edit", when=_VERIFIED + timedelta(hours=1))
    if then == "twin":
        twin = _object(repo, b"X = 1\n")
        _index_info(repo, [("100644", twin, _SPELLED["NFC"])])
    else:
        _index_info(
            repo,
            [("0", "0" * 40, _SPELLED["NFD"]), ("100644", edited, _SPELLED["NFC"])],
        )
    _plain(repo, "commit", "-q", "-m", then, when=_VERIFIED + timedelta(hours=hours))
    return anchor


@pytest.mark.parametrize(
    ("then", "hours", "claimed", "cited"),
    [
        ("twin", 2, (1, 1), (2, 2)),
        ("rename", 2, (2, 2), (2, 2)),
        ("rename", -0.5, (2, 1), (2, 1)),
    ],
    ids=["twin", "rename", "back-dated rename"],
)
def test_a_claim_is_spelled_as_the_tree_it_was_verified_against(
    tmp_path: Path,
    then: str,
    hours: float,
    claimed: tuple[int, int],
    cited: tuple[int, int],
) -> None:
    """foo claimed composed on a file the anchor holds decomposed alone, and
    its def line changed there after the verify. Then a composed twin
    without foo is added, or the file is renamed to the composed spelling.
    The head's tree holds the claim's own spelling either way, and read
    under it alone the edit was missed: the twin read 0 on both bases, the
    rename 1, and the rename back-dated 1 on the walk and 0 on dates
    (ENG-220's review, round D). Spelled as the anchor's tree, which does
    not hold the composed name, every spelling is read on the walk: the
    edit counts, 1, and the rename's deletion and creation of foo's def
    another, 2. Dates read every spelling and count what was authored
    after the verify: the back-dated rename is missed there, as every
    back-dated commit is, never the edit. The file cited composed, with no
    claim, counts each commit that touched either spelling the same way."""
    repo = _repository(tmp_path)
    anchor = _verified_decomposed(repo, then, hours)
    claim = _SPELLED["NFC"] + "::foo"
    assert _both(repo, [claim], anchor) == [(count, (claim,)) for count in claimed]
    got = _both(repo, [], anchor, (_SPELLED["NFC"],))
    assert got == [(count, ()) for count in cited]


#: A file seven accented components deep, past the spellings bound
#: (`claims.spellings_capped`), and stored with its components' forms
#: mixed: composed and decomposed by turns, as no whole form spells it.
_DEEP = "/".join(f"dé{i}" for i in range(6)) + "/café.py"
_DEEP_MIXED = "/".join(
    unicodedata.normalize("NFC" if i % 2 else "NFD", part)
    for i, part in enumerate(_DEEP.split("/"))
)


@pytest.mark.parametrize("beside", [True, False])
@pytest.mark.parametrize(
    ("stored", "claimed", "truth"),
    [
        (_DEEP_MIXED, unicodedata.normalize("NFC", _DEEP), "conservative"),
        (_DEEP_MIXED, unicodedata.normalize("NFD", _DEEP), "conservative"),
        (_DEEP_MIXED, _DEEP_MIXED, "exact"),
        (
            unicodedata.normalize("NFD", _DEEP),
            unicodedata.normalize("NFC", _DEEP),
            "exact",
        ),
    ],
)
def test_a_tree_mixing_forms_past_the_bound_keeps_the_conservative_count(
    tmp_path: Path, stored: str, claimed: str, truth: str, beside: bool
) -> None:
    """Past the spellings bound only the whole forms of a path are looked
    for, so a claim on a file the tree stores with its components' forms
    mixed names no path the tree holds: foo's def line changes, and
    two.py's bar is edited. The hit keeps the conservative count, the
    whole window, 2, on both bases, with two.py's foo claimed `beside`
    it or not. It matched nothing: it read 0 beside two.py's claim, and
    alone as not applicable (ENG-220's review, round C). Spelled as
    stored, or against a tree holding a whole form, the claim reads its
    one commit."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [stored], "edit")
    claim = f"{claimed}::foo"
    expected = (2, ()) if truth == "conservative" else (1, (claim,))
    claims = [claim, _QUIET] if beside else [claim]
    assert _both(repo, claims, anchor) == [expected] * 2


def test_a_whole_form_past_the_bound_deleted_since_the_stamp_is_read(
    tmp_path: Path,
) -> None:
    """foo claimed composed on a file past the spellings bound that the
    anchor stores decomposed, deleted after the verify, and two.py's bar
    edited: the walk reads the anchor's tree, which holds a whole form, and
    dates the deletion, authored after the stamp, of one the head no longer
    holds; foo's commit, 1, on both bases. Declined for the head's tree
    alone, dates kept the conservative count, 2."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [unicodedata.normalize("NFD", _DEEP)], "delete")
    claim = unicodedata.normalize("NFC", _DEEP) + "::foo"
    assert _both(repo, [claim, _QUIET], anchor) == [(1, (claim,))] * 2


def test_a_cited_path_mixing_forms_past_the_bound_keeps_the_conservative_count(
    tmp_path: Path,
) -> None:
    """The body cites, composed, a file the tree stores with its forms
    mixed past the spellings bound, and two.py, and claims nothing: the
    file is edited after the verify, and then two.py. The hit keeps the
    conservative count, 2, on both bases, and so does the count the
    status surfaces read on dates. The cited file matched nothing, so
    only two.py's edit counted, 1."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [_DEEP_MIXED], "bar")
    cited = (unicodedata.normalize("NFC", _DEEP), _BESIDE)
    assert _both(repo, [], anchor, cited) == [(2, ())] * 2
    _caches.clear_all()
    count = verify.resolve_commit_drift_count(
        cwd=repo, since=_VERIFIED, unfiltered=2, anchors=cited, toplevel=repo
    )
    assert count == 2


def test_a_quiet_tree_mixing_forms_past_the_bound_keeps_the_signal(
    tmp_path: Path,
) -> None:
    """No commit since the verify, a claim on a file the tree stores with
    its forms mixed past the spellings bound, and one on a file no commit
    touched: the first claim is never read as phantom, so the quiescent
    check keeps the signal. Looked for in its whole forms only, it
    touched no commit, and with the phantom beside it the memory read as
    not applicable. A path past the bound that no tree holds keeps the
    signal too: its mixed forms are not looked for."""
    repo = _repository(tmp_path)
    body = _object(repo, _module("", 0).encode())
    _index_info(repo, [("100644", body, _DEEP_MIXED)])
    _plain(repo, "commit", "-q", "-m", "anchor", when=_VERIFIED - timedelta(hours=1))
    phantom = "src/nowhere.py::foo"
    claims = load_claims([unicodedata.normalize("NFC", _DEEP) + "::foo", phantom])
    _caches.clear_all()
    assert verify._quiescent_drift_applicable(repo, (), claims, toplevel=repo)
    claims = load_claims([unicodedata.normalize("NFC", _DEEP[:-3]) + "x.py::foo"])
    _caches.clear_all()
    assert verify._quiescent_drift_applicable(repo, (), claims, toplevel=repo)
    _caches.clear_all()
    assert not verify._quiescent_drift_applicable(
        repo, (), load_claims([phantom]), toplevel=repo
    ), "premise: a phantom alone is not applicable"


def test_an_unaddressable_claim_keeps_each_spelling_of_its_file_any_touch(
    tmp_path: Path,
) -> None:
    """A container literal claimed on a file spelled composed, and foo on it
    spelled decomposed: the file stays in the any-touch leg whichever
    spelling each claim uses, so bar's edit in it counts, 1 on both bases.
    Blocked by spelling alone, the decomposed one was governed and the
    composed one left the any-touch leg as the governed file's, and the
    edit read 0 (`claims.governed_claim_paths`)."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [_SPELLED["NFC"]], "bar")
    claims = [_SPELLED["NFD"] + "::foo", _SPELLED["NFC"] + "::LIMITS=[1, 2]"]
    assert _both(repo, claims, anchor) == [(1, ())] * 2


@pytest.mark.parametrize("cited", ["NFD", "NFC"])
def test_a_file_cited_in_one_spelling_and_claimed_in_another_is_the_claims(
    tmp_path: Path, cited: str
) -> None:
    """The body cites the file in either spelling and claims foo on it
    decomposed, as the tree stores it: the file is the claim's, so bar's
    edit in it escalates nothing, 0 on both bases, as where both are
    spelled alike. Kept apart by spelling, the cited one stayed in the
    any-touch leg and counted the edit (`verify._claim_legs`)."""
    repo = _repository(tmp_path)
    anchor = _accented(repo, [_SPELLED["NFD"]], "bar")
    claims = [_SPELLED["NFD"] + "::foo"]
    assert _both(repo, claims, anchor, (_SPELLED[cited],)) == [(0, ())] * 2


def test_each_spelling_of_a_path_is_looked_for() -> None:
    """Every component as written, composed and decomposed, in every
    combination; an ASCII path has the one spelling; past the bound, the
    whole path in each form, and the path is marked as past it."""
    from bettermemory.claims import (
        MAX_SPELLINGS,
        path_key,
        spellings_capped,
        stored_spellings,
    )

    six = "/".join(["é"] * 6)
    assert len(stored_spellings(six)) == MAX_SPELLINGS == 64
    assert not spellings_capped(six)
    assert spellings_capped(six + "/é")
    assert not spellings_capped(_SPELLED["NFC"])
    assert not spellings_capped("/".join(["e"] * 9))
    assert spellings_capped(_DEEP) and spellings_capped(_DEEP_MIXED)

    spellings = stored_spellings(_SPELLED["NFC"])
    assert spellings[0] == _SPELLED["NFC"]
    assert set(spellings) == {
        unicodedata.normalize(folder, "src/dé/")
        + unicodedata.normalize(name, "café.py")
        for folder in ("NFC", "NFD")
        for name in ("NFC", "NFD")
    }
    assert {path_key(path) for path in spellings} == {_SPELLED["NFC"]}
    assert stored_spellings("src/plain.py") == ("src/plain.py",)
    deep = "/".join(["é"] * 7)
    assert stored_spellings(deep) == (deep, unicodedata.normalize("NFD", deep))


# ---------------------------------------------------------------------------
# What a format prints, and how its lines are read (ENG-220's re-review)
# ---------------------------------------------------------------------------


def _dated(
    repo: Path,
    files: Mapping[str, str | tuple[str, str] | None],
    hours: float,
    offset: str,
    *,
    encoding: str | None = None,
    message: bytes = b"m\n",
) -> str:
    """`_commit` with the author at `offset` from UTC, and the message's
    bytes stored in `encoding` (git's ``encoding`` header)."""
    _stage(repo, files)
    when = _VERIFIED + timedelta(hours=hours)
    stamp = f"@{int(when.timestamp())} {offset}"
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_AUTHOR_DATE=stamp,
        GIT_COMMITTER_DATE=stamp,
    )
    pin = ["-c", f"i18n.commitEncoding={encoding}"] if encoding else []
    subprocess.run(
        ["git", *pin, "commit", "-q", "--allow-empty", "-F", "-"],
        cwd=repo,
        input=message,
        env=env,
        capture_output=True,
        check=True,
    )
    return _plain(repo, "rev-parse", "HEAD")


def _east_and_west(
    repo: Path, *, encoding: str | None = None, message: bytes = b"m\n"
) -> str:
    """A history whose authors sit east and west of UTC: bar's body edited
    (UTC-5), foo's def line changed (UTC+2), two.py's bar edited (UTC+9).
    The anchor, at UTC, is returned."""
    anchor = _dated(repo, {_MOD: _module("", 0), _BESIDE: _module("", 0)}, -1, "+0000")
    for files, hours, offset in (
        ({_MOD: _module("", 1)}, 1, "-0500"),
        ({_MOD: _module("flag=False", 1)}, 2, "+0200"),
        ({_BESIDE: _module("", 1)}, 3, "+0900"),
    ):
        _dated(repo, files, hours, offset, encoding=encoding, message=message)
    return anchor


@pytest.mark.parametrize("key", ["i18n.logOutputEncoding", "i18n.commitEncoding"])
def test_a_log_output_encoding_moves_no_count(tmp_path: Path, key: str) -> None:
    """foo's def line changes once, and src/mod.py twice, after the verify:
    a claim on foo counts 1, foo named, and a memory that cites src/mod.py
    and claims nothing counts 2, on both bases. Git re-encodes every line a
    format prints into ``i18n.logOutputEncoding`` (``i18n.commitEncoding``
    where that is unset): in UTF-7 the walk's ``\\x01`` mark and the ``+``
    of a date east of UTC are spelled otherwise, so the walk declined and
    the dates carrying a ``+`` dropped out: the claim counted 0 and the
    citing memory 2 against 4. Every origin call prints UTF-8
    (`origin_history.AS_STORED_CONFIG`)."""
    repo = _repository(tmp_path)
    anchor = _east_and_west(repo)
    claims = [_FOO, _QUIET]
    truth = _both(repo, claims, anchor)
    cited = _both(repo, [], anchor, (_MOD,))
    assert truth == [(1, (_FOO,))] * 2
    assert cited == [(2, ())] * 2
    _plain(repo, "config", key, "UTF-7")
    dates = _plain(repo, "log", "--format=%aI")
    assert "+-02:00" in dates, "premise: git's own log re-encodes the dates"
    assert _both(repo, claims, anchor) == truth
    assert _both(repo, [], anchor, (_MOD,)) == cited


@pytest.mark.parametrize(
    ("encoding", "message"),
    [("ISO-8859-1", "café +1 \x01\n".encode("latin-1")), ("UTF-7", b"a+-b\n")],
    ids=["latin-1", "utf-7"],
)
def test_a_commits_stored_encoding_moves_no_count(
    tmp_path: Path, encoding: str, message: bytes
) -> None:
    """The window's commits store their messages in an encoding other than
    UTF-8, named in each commit's ``encoding`` header. Git re-encodes a
    message into the output encoding, and no format the package reads
    prints a message, so the counts are those of the same history in
    UTF-8: 1, foo named, and 2 for the citing memory, on both bases."""
    repo = _repository(tmp_path)
    anchor = _east_and_west(repo, encoding=encoding, message=message)
    stored = subprocess.run(
        ["git", "cat-file", "commit", "HEAD"], cwd=repo, capture_output=True
    ).stdout
    assert f"encoding {encoding}\n".encode() in stored, "premise: the header"
    assert _both(repo, [_FOO, _QUIET], anchor) == [(1, (_FOO,))] * 2
    assert _both(repo, [], anchor, (_MOD,)) == [(2, ())] * 2


_SHA = "a" * 40


@pytest.mark.parametrize(
    ("reader", "raw"),
    [
        ("dates", "2025-01-01T02:00:00-05:00\n2025-01-01T03:00:00+-02:00"),
        ("pairs", f"2025-01-01T02:00:00-05:00 {_SHA}\n2025-01-01T03:00:00Z"),
        ("pairs", f"2025-01-01T02:00:00-05:00 {_SHA}\n2025-01-01T03:00:00Z abc"),
        ("whole", "1735700400\n+AAE-1735704000"),
        ("walk", f"\x01-{_SHA}\n\x01>{_SHA[:39]}\nsrc/mod.py"),
    ],
    ids=["a garbled offset", "no hash", "a short hash", "a garbled date", "a mark"],
)
def test_a_line_the_format_did_not_print_declines_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, raw: str
) -> None:
    """Git prints every line of a log's format. A line that reads as none
    (a date a re-encoding spelled otherwise, a hash or a mark it garbled)
    dropped out of the count, which read lower; it declines the read now,
    counted as a failed git call, so the hit keeps its fallback and no memo
    keeps the value; the whole history's read says why it was not read
    (`ERROR`), and the row reads unread."""
    monkeypatch.setattr(origin, "_git", lambda *a, **k: raw)
    failed = origin.failed_git_calls()
    got: object
    if reader == "dates":
        got = origin_dates.commit_author_timestamps_touching_pathspecs(
            tmp_path, ["src/mod.py"], toplevel=tmp_path
        )
    elif reader == "pairs":
        got = origin_dates.commit_author_sha_pairs_touching_pathspecs(
            tmp_path, ["src/mod.py"], toplevel=tmp_path
        )
    elif reader == "whole":
        got = origin_dates._read_author_timestamps(tmp_path, "HEAD")
    else:
        got = origin._walk_reachable(tmp_path, _SHA, "b" * 40)
    assert got is (CommitDriftUnread.ERROR if reader == "whole" else None)
    assert origin.failed_git_calls() == failed + 1


def test_an_extension_with_a_garbled_line_reads_the_history_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dates a head adds to an earlier head's are read the same way: a
    line that reads as none sends the caller to the whole history."""
    raw = f"-{_SHA} 1735700400\n>{'b' * 40} +AAE-1735704000\n"
    done = subprocess.CompletedProcess(["git"], 0, stdout=raw, stderr="")
    monkeypatch.setattr(origin, "_git_result", lambda *a, **k: done)
    assert (
        origin_dates._extended_author_timestamps(
            tmp_path, _SHA, [], "b" * 40, frozenset()
        )
        is None
    )
    done.stdout = raw.replace("+AAE-", "")
    got = origin_dates._extended_author_timestamps(
        tmp_path, _SHA, [], "b" * 40, frozenset()
    )
    assert got == [datetime(2025, 1, 1, 4, tzinfo=timezone.utc)]


def test_a_date_past_what_a_datetime_holds_is_counted() -> None:
    """A forged author date past year 9999 was skipped, and its commit,
    after any verify, dropped out of the count; it reads as the latest
    date now, and one before year 1 as the earliest."""
    got = origin_history.author_dates([10**15, 0, -(10**15)])
    assert got == [
        datetime.min.replace(tzinfo=timezone.utc),
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime.max.replace(tzinfo=timezone.utc),
    ]


@pytest.mark.parametrize("order", ["missing", "reversed"])
def test_an_order_file_moves_no_count(tmp_path: Path, order: str) -> None:
    """foo's def line changes after the verify, and two.py's bar is edited:
    1, foo named, on both bases. ``diff.orderFile`` naming a file that is
    not there failed every diff git printed, so the walk declined and the
    dates counted the claimed file's commits; one that names a file only
    orders a commit's files. ``-O`` with the null device, an empty order,
    outranks it (`origin_history.AS_STORED_DIFF`)."""
    repo = _repository(tmp_path)
    anchor = _commit(repo, {_MOD: _module("", 0), _BESIDE: _module("", 0)}, -1)
    _commit(repo, {_MOD: _module("", 1), _BESIDE: _module("", 1)}, 1)
    _commit(repo, {_MOD: _module("flag=False", 1)}, 2)
    claims = [_FOO, _QUIET]
    truth = _both(repo, claims, anchor)
    assert truth == [(1, (_FOO,))] * 2
    named = tmp_path / "order"
    if order == "reversed":
        named.write_text(f"{_BESIDE}\n{_MOD}\n")
    _plain(repo, "config", "diff.orderFile", str(named))
    shown = subprocess.run(
        ["git", "log", "--name-only", "-1"], cwd=repo, capture_output=True
    )
    assert (shown.returncode != 0) is (order == "missing"), "premise"
    assert _both(repo, claims, anchor) == truth


@pytest.mark.parametrize("deepen", ["--unshallow", "--deepen=5"])
def test_a_deepened_clone_is_read_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deepen: str
) -> None:
    """A shallow clone's whole-history dates are not read, and the failure
    is remembered for a minute. It is remembered with the stamp of the
    shallow file, so a clone deepened within the minute, which rewrites or
    removes the file, is read at once, in the same process and at the same
    head: its dates are a full clone's."""
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    partial = Partial(tmp_path, _LATE)
    full = tmp_path / "full"
    _plain(tmp_path, "clone", "-q", partial.source.as_uri(), str(full))
    shallow = tmp_path / "shallow"
    _plain(tmp_path, "clone", "-q", "--depth=3", partial.source.as_uri(), str(shallow))
    every = origin_dates.commit_author_timestamps(full)
    assert isinstance(every, list) and len(every) == len(_LATE)
    shallow_read = CommitDriftUnread.SHALLOW
    assert origin_dates.commit_author_timestamps(shallow) is shallow_read
    assert origin_dates.commit_author_timestamps(shallow) is shallow_read, "remembered"
    _plain(shallow, "fetch", "-q", deepen)
    assert origin_dates.commit_author_timestamps(shallow) == every


def test_git_output_keeps_its_carriage_returns(tmp_path: Path) -> None:
    """`_git.run_git` decodes git's output from its bytes: a ``\\r`` in a
    blob, alone or before a line feed, reads as written, and only a line
    feed ends a line."""
    repo = _repository(tmp_path)
    data = b"a\rb\r\nc\n"
    blob = _object(repo, data)
    done = _git.run_git(["cat-file", "blob", blob], cwd=repo)
    assert done is not None and done.stdout == data.decode()
    raw = _git.run_git(["cat-file", "blob", blob], cwd=repo, text=False)
    assert raw is not None and raw.stdout == data


# ---------------------------------------------------------------------------
# A claimed name git quotes (ENG-220's re-review)
# ---------------------------------------------------------------------------

#: Windows takes no control character in a name.
_POSIX_NAME = pytest.mark.skipif(
    os.name == "nt", reason="a Windows name holds no control character"
)


@_POSIX_NAME
@pytest.mark.parametrize("empty", [False, True], ids=["text", "empty"])
@pytest.mark.parametrize("name", ["src/a\nb.py", "src/c\rr.py", "src/t\tab.py"])
def test_a_stored_claim_on_a_name_git_quotes_keeps_the_conservative_count(
    tmp_path: Path, name: str, empty: bool
) -> None:
    """A claimed file whose name holds a control character is deleted after
    the verify, beside an edit of two.py's bar. git quotes the name in every
    patch, so no header named it as the claim does: the claim read 0, or 1
    naming no claim where the file was empty. Declaration refuses such a
    path now; one stored before keeps the conservative count, the whole
    window, on both bases, never 0."""
    repo = _repository(tmp_path)
    text = "" if empty else "LONG_ENOUGH_TO_ANCHOR = 'padding padding'\n"
    anchor = _commit(repo, {name: text, _BESIDE: _module("", 0)}, -1)
    _commit(repo, {_BESIDE: _module("", 1)}, 1)
    _commit(repo, {name: None}, 2)
    assert _both(repo, [name, _QUIET], anchor) == [(2, ())] * 2


@pytest.mark.parametrize("clone", ["full", "blobless", "treeless"])
def test_the_tree_check_reads_names_alone_and_fetches_nothing(
    tmp_path: Path, clone: str
) -> None:
    """The anchor's tree check asks git for each name's object name alone
    (`--batch-check=%(objectname)`), no type or size: a full clone holds
    the decomposed file and its folder, the composed spelling neither. git
    still asks whether the object is in the repository, so a clone with no
    checkout whose filter left the blob out reads the file as not held, and
    one that left the trees out reads nothing held: the read falls back to
    every spelling there, never fewer (ENG-220's review, round D). No object
    is fetched. Git from 2.39 to 2.44 ends the check at the first object
    the clone lacks ("could not fetch ... from promisor remote", lazy
    fetching disabled or not), so a partial clone's read has no answer and
    declines (ENG-227: 2.39.5, 2.43.7 and 2.44.4 exit 128; 2.38.5 and
    2.45.4 name the object missing and go on)."""
    from bettermemory import verify_spelling

    source = _repository(tmp_path)
    _plain(source, "config", "uploadpack.allowFilter", "true")
    _index_info(source, [("100644", _object(source, b"A = 1\n"), _SPELLED["NFD"])])
    _plain(source, "commit", "-q", "-m", "anchor", when=_VERIFIED)
    anchor = _plain(source, "rev-parse", "HEAD")
    extra = {
        "full": [],
        "blobless": ["--filter=blob:none", "--no-checkout"],
        "treeless": ["--filter=tree:0", "--no-checkout"],
    }[clone]
    dest = tmp_path / clone
    _plain(tmp_path, "clone", "-q", *extra, source.as_uri(), str(dest))
    objects = _plain(dest, "count-objects", "-v")
    folder = _SPELLED["NFD"].rsplit("/", 1)[0]
    names = {_SPELLED["NFD"], _SPELLED["NFC"], folder}
    held = verify_spelling._held(dest, anchor, names)
    expected: dict[str, set[str] | None] = {
        "full": {_SPELLED["NFD"], folder},
        "blobless": {folder},
        "treeless": set(),
    }
    if clone != "full" and (2, 39) <= _git_version() < (2, 45):
        expected[clone] = None
    assert held == expected[clone]
    assert _plain(dest, "count-objects", "-v") == objects, "nothing fetched"


@pytest.mark.parametrize("name", ["src/a\ud800.py", "src/é\udbff.py", "src/a\udc80.py"])
def test_a_claim_on_a_lone_surrogate_keeps_the_conservative_count(
    tmp_path: Path, name: str
) -> None:
    """foo claimed on a path holding a lone surrogate, stored before
    declaration refused one, beside foo on src/mod.py, whose def line the
    rebased commit changes: the hit keeps the walk's whole length, 2, and
    on dates every commit since the stamp, 1, as a claim holding a control
    character does; it raised UnicodeEncodeError, or read the other claim
    alone (ENG-220's review, round D). The tree check skips such a name,
    which is never held, rather than raise."""
    from bettermemory import verify_spelling

    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    claims = [f"{name}::foo", f"{_MOD}::foo"]
    assert _both(repo, claims, anchor) == [(2, ()), (1, ())]
    _caches.clear_all()
    assert verify_spelling._held(repo, "HEAD", {name, _MOD}) == {_MOD}


@pytest.mark.parametrize("walked", [False, True], ids=["dates", "walk"])
def test_a_claim_holding_a_control_character_is_not_read(
    monkeypatch: pytest.MonkeyPatch, walked: bool
) -> None:
    """The decline needs no git: both entry points answer the conservative
    count before a claim is read or a log run, on every platform."""

    def unread(*args: object, **kwargs: object) -> None:
        raise AssertionError("a log ran over a claim the readers decline")

    for name in (
        "commit_author_sha_pairs_touching_pathspecs",
        "commit_author_timestamps_touching_pathspecs",
        "commit_patch_stream",
    ):
        monkeypatch.setattr(verify, name, unread)
    walk = None
    if walked:
        walk = origin.ReachableWalk(
            anchor=_SHA, head="b" * 40, commits=("b" * 40, "c" * 40), touched={}
        )
    claims = load_claims(["src/a\nb.py", _QUIET])
    nowhere = Path(os.devnull)
    want = 2 if walked else 5
    resolved = verify.resolve_commit_drift(
        cwd=nowhere,
        since=_VERIFIED,
        unfiltered=5,
        anchors=(),
        claims=claims,
        toplevel=nowhere,
        walk=walk,
    )
    assert resolved is not None
    assert (resolved.count, resolved.claims_checked, resolved.claims_drifted) == (
        want,
        2,
        (),
    )
    count = verify.resolve_commit_drift_count(
        cwd=nowhere,
        since=_VERIFIED,
        unfiltered=5,
        anchors=(),
        claims=claims,
        toplevel=nowhere,
        walk=walk,
    )
    assert count == want


# ---------------------------------------------------------------------------
# A walk that times out fails closed (ENG-220's review, round C)
# ---------------------------------------------------------------------------


def _timing_out(monkeypatch: pytest.MonkeyPatch, *flags: str) -> list[list[str]]:
    """Every git process whose argv holds one of `flags` meets its timeout;
    the argvs that did, in order."""
    real = subprocess.run
    met: list[list[str]] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if any(flag in argv for flag in flags):
            met.append(list(argv))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return met


def _rebased(repo: Path) -> str:
    """foo's def line changes on top of the verified head under a commit
    authored before the verify (a rebased or back-dated one), and bar's
    body after it; the anchor."""
    anchor = _commit(repo, {_MOD: _module("", 0)}, -2)
    _commit(repo, {_MOD: _module("flag=False", 0)}, -1)
    _commit(repo, {_MOD: _module("flag=False", 1)}, 1)
    return anchor


def _seen(got: ResolvedCommitDrift | None) -> tuple[int, tuple[str, ...], str] | None:
    return None if got is None else (got.count, got.claims_drifted, got.basis)


def test_a_walk_whose_names_time_out_keeps_the_conservative_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foo's def line changes on top of the verified head under a commit
    authored before the verify: the walk reads it, 1 with foo named, and
    the author dates miss it, 0. Where the walk's names meet their
    timeout, the hit read those dates, 0; its commits are now listed
    alone and each counts, 2 on the reachable basis, and no date is read.
    Out of the walk memo, it is kept a minute apart (`_WALK_TIMED_OUT`, round
    E), and for the pass (`walked`) where no memo keys it; served again
    from either, it counts its timeout again (round D)."""
    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    assert _seen(_hit(repo, [_FOO], anchor)) == (1, (_FOO,), verify.BASIS_REACHABILITY)
    dated = _seen(_hit(repo, [_FOO], None))
    assert dated == (0, (), verify.BASIS_AUTHOR_DATE), "premise: the dates miss it"
    met = _timing_out(monkeypatch, "--name-only")
    assert _seen(_hit(repo, [_FOO], anchor)) == (2, (), verify.BASIS_REACHABILITY)
    assert len(met) == 1 and not origin._WALK_MEMO
    head = _plain(repo, "rev-parse", "HEAD")
    failed, timed_out = _git.failed_calls(), _git.timed_out_calls()
    kept = origin.commits_since_anchor(repo, anchor, toplevel=repo, head=head)
    assert kept is not None and not kept.listed and len(kept.commits) == 2
    assert kept.touched == {} and kept.shas_touching(["x"]) == set(kept.commits)
    assert len(met) == 1, "kept a minute"
    assert (_git.failed_calls(), _git.timed_out_calls()) == (failed + 1, timed_out + 1)
    monkeypatch.setattr(origin, "walk_files_signature", lambda *a, **k: None)
    walked: origin.Walked = {}
    first = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert first is not None and not first.listed and len(met) == 2
    failed, timed_out = _git.failed_calls(), _git.timed_out_calls()
    again = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert again is first and len(met) == 2, "one walk for the pass"
    assert (_git.failed_calls(), _git.timed_out_calls()) == (failed + 1, timed_out + 1)


def test_a_walk_no_memo_keys_is_served_again_as_it_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk that listed its names, which the process-wide memo cannot key
    (the files beside the history unreadable), is kept for the pass and
    served again with no process and no failure counted: only a walk whose
    names met their timeout counts its timeout again."""
    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    head = _plain(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(origin, "walk_files_signature", lambda *a, **k: None)
    _caches.clear_all()
    walked: origin.Walked = {}
    first = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert first is not None and first.listed
    failed, timed_out = _git.failed_calls(), _git.timed_out_calls()
    met = _timing_out(monkeypatch, "--boundary")
    again = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert again is first and met == []
    assert (_git.failed_calls(), _git.timed_out_calls()) == (failed, timed_out)


def test_a_walk_whose_listing_times_out_too_reads_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk's names and the listing of its commits alone both meet
    their timeout: the search hit's resolution (foo claimed, which the
    author dates read as 0) is `CommitDriftUnread`, neither a count nor
    the None of a leg that does not apply, and the health rollup
    (src/mod.py cited, which the dates read as 1 where the walk reads 2)
    gives the memory no row and counts it unread; neither reads the
    author dates that miss the rebased commit. A later hit at the anchor
    in the same pass runs no walk and is unread too, counted as a
    timeout. What the surfaces say of it: the tests below and
    `test_commit_drift_cache`."""
    from bettermemory import health_stats, verify_basis

    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    head = _plain(repo, "rev-parse", "HEAD")

    def rollup() -> tuple[list[int], dict[str, CommitDriftUnread]]:
        _caches.clear_all()
        history = origin_dates.commit_author_timestamps(repo)
        assert history is not None
        rows, unread = health_stats._drift_rows_for_candidates(
            [_stats("m")],
            root=repo,
            timestamps=history,
            toplevel=repo,
            anchor_paths_by_id={"m": (_MOD,)},
            claims_by_id=None,
            verified_head_by_id={"m": anchor},
            head=head,
        )
        return [row.commits_since_verify for row in rows], unread

    assert rollup() == ([2], {}), "premise: the walk reads both commits"
    met = _timing_out(monkeypatch, "--boundary")
    assert _unread_or_hit(repo, [_FOO], anchor) is CommitDriftUnread.TIMEOUT
    assert len(met) == 2, "the walk, then its listing"
    _caches.clear_all()
    counted = verify_basis._count_since_verify(repo, _VERIFIED, anchor)
    assert counted is CommitDriftUnread.TIMEOUT
    assert rollup() == ([], {"m": CommitDriftUnread.TIMEOUT})
    # Where no memo keys the walk (the files unreadable), the pass keeps it.
    monkeypatch.setattr(origin, "walk_files_signature", lambda *a, **k: None)
    _caches.clear_all()
    walked: origin.Walked = {}
    for _ in range(2):
        timed_out = _git.timed_out_calls()
        unwalked = origin_history.unwalked()
        got = verify.resolve_hit_drift(
            cwd=repo,
            toplevel=repo,
            head=head,
            timestamps=lambda: origin_dates.commit_author_timestamps(repo),
            anchors=(),
            claims=load_claims([_FOO]),
            since=_VERIFIED,
            verified_head=anchor,
            walked=walked,
        )
        assert got is CommitDriftUnread.TIMEOUT
        assert origin_history.unwalked() == unwalked + 1
        assert _git.timed_out_calls() > timed_out
    assert len(met) == 2 * 3 + 2, "the second hit ran no walk"


def _unread_trees(tmp_path: Path, shape: str) -> tuple[Path, str]:
    """A repository holding `_rebased`'s history, and its anchor, with the
    trees the walk's names read unreadable to git by `shape`: none, the
    anchor's tree object corrupt, or a treeless clone's trees not fetched
    (git fetches none: `_git` sets GIT_NO_LAZY_FETCH)."""
    source = _repository(tmp_path)
    anchor = _rebased(source)
    if shape == "treeless clone":
        _plain(source, "config", "uploadpack.allowFilter", "true")
        clone = tmp_path / "clone"
        _plain(tmp_path, "clone", "-q", "--filter=tree:0", source.as_uri(), str(clone))
        return clone, anchor
    if shape == "corrupt tree":
        tree = _plain(source, "rev-parse", f"{anchor}^{{tree}}")
        loose = source / ".git" / "objects" / tree[:2] / tree[2:]
        loose.chmod(0o644)
        loose.write_bytes(b"not a zlib stream")
    return source, anchor


@pytest.mark.parametrize("shape", ["read", "corrupt tree", "treeless clone"])
def test_a_walk_whose_trees_git_cannot_read_lists_its_commits_alone(
    tmp_path: Path, shape: str
) -> None:
    """foo's def line changes on top of the verified head under a commit
    authored before the verify, which the walk reads, 1 with foo named, and
    the author dates miss. Where git cannot read the trees the walk's names
    diff (the anchor's tree corrupt, a treeless clone), the walk lists its
    commits alone and every one counts, 2, on the search hit, memory_show's
    count and the health rollup, where the hit read those dates, 0
    (ENG-220's review, round F). A memory stamped with no head counts on
    the dates on either basis, the claim's narrowing kept where its patch
    cannot be read either: 1, never below the 0 it reads with the trees."""
    from bettermemory import health_stats, verify_basis

    repo, anchor = _unread_trees(tmp_path, shape)
    head = _plain(repo, "rev-parse", "HEAD")
    read = shape == "read"
    walked = (1, (_FOO,)) if read else (2, ())
    assert _seen(_hit(repo, [_FOO], anchor)) == (*walked, verify.BASIS_REACHABILITY)
    dated = (0 if read else 1, (), verify.BASIS_AUTHOR_DATE)
    assert _seen(_hit(repo, [_FOO], None)) == dated
    _caches.clear_all()
    counted = verify_basis._count_since_verify(repo, _VERIFIED, anchor)
    assert not isinstance(counted, CommitDriftUnread) and counted is not None
    count, basis, walk, _ = counted
    assert (count, basis) == (2, verify.BASIS_REACHABILITY)
    assert walk is not None and walk.listed == read and not walk.timed_out
    _caches.clear_all()
    counted = verify_basis._count_since_verify(repo, _VERIFIED, None)
    assert counted == (1, verify.BASIS_AUTHOR_DATE, None, None)
    history = origin_dates.commit_author_timestamps(repo)
    assert isinstance(history, list)
    for verified_head, want in (
        (anchor, (2, verify.BASIS_REACHABILITY)),
        (None, (1, verify.BASIS_AUTHOR_DATE)),
    ):
        _caches.clear_all()
        rows, unread = health_stats._drift_rows_for_candidates(
            [_stats("m")],
            root=repo,
            timestamps=history,
            toplevel=repo,
            anchor_paths_by_id={"m": (_MOD,)},
            claims_by_id=None,
            verified_head_by_id={"m": verified_head} if verified_head else None,
            head=head,
        )
        assert [(row.commits_since_verify, row.basis) for row in rows] == [want]
        assert unread == {}
    assert not origin._WALK_MEMO or read, "a walk listed alone is not kept"
    assert not origin._WALK_TIMED_OUT, "nor kept as a timeout's"


def test_a_walk_listed_alone_after_git_failed_is_served_as_a_failure(
    tmp_path: Path,
) -> None:
    """Where git failed on the walk's names, the commits listed alone are
    kept for the pass and served again counting a failure, not a timeout,
    so a caller keeps nothing from it, and nothing past the pass."""
    repo, anchor = _unread_trees(tmp_path, "treeless clone")
    head = _plain(repo, "rev-parse", "HEAD")
    _caches.clear_all()
    walked: origin.Walked = {}
    first = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert first is not None and not first.listed and not first.timed_out
    failed, timed_out = _git.failed_calls(), _git.timed_out_calls()
    again = origin.commits_since_anchor(
        repo, anchor, toplevel=repo, head=head, walked=walked
    )
    assert again is first
    assert (_git.failed_calls(), _git.timed_out_calls()) == (failed + 1, timed_out)
    assert not origin._WALK_MEMO and not origin._WALK_TIMED_OUT


def _failing(
    monkeypatch: pytest.MonkeyPatch, names: str, listing: str, held: str = "read"
) -> list[str]:
    """The walk's names, the listing of its commits alone and the check
    that the repository holds the anchor (``cat-file -e``) answer as
    named: ``read`` as git does, ``exit 128``, ``killed`` (exit -9) or
    ``timeout``. What each of the three met, in order."""
    real = subprocess.run
    met: list[str] = []
    marked = f"--format={origin._WALK_MARK}%m%H"

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if marked in argv:
            step = "names" if "--name-only" in argv else "listing"
            how = names if step == "names" else listing
        elif "cat-file" in argv and "-e" in argv:
            step, how = "held", held
        else:
            return real(argv, *args, **kwargs)
        met.append(step)
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        if how == "exit 128":
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object\n")
        if how == "killed":
            return subprocess.CompletedProcess(argv, -9, b"", b"")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return met


#: How the walk's names and the listing of its commits alone fail, with
#: the anchor held: neither met a timeout in the first two, the names did
#: in the third (ENG-220's review, round G).
_LISTING_FAILS = [
    ("exit 128", "exit 128"),
    ("exit 128", "killed"),
    ("timeout", "killed"),
]


@pytest.mark.parametrize(("names", "listing"), _LISTING_FAILS)
def test_a_listing_that_fails_from_a_held_anchor_reads_unread(
    names: str, listing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foo's def line changes on top of the verified head under a commit
    authored before the verify: the walk reads 1, the author dates 0.
    Where git fails on the walk's names and on the listing of its commits
    alone, the hit read those dates, 0, as for an anchor the repository
    does not hold; it holds this one (``git cat-file -e``), so the search
    hit, memory_show's count and the health rollup read `error`, unread,
    and none reads the dates (ENG-220's review, round G). A later hit at
    the anchor in the same pass runs no walk and is unread too, counted
    as a failure; one whose names met their timeout is kept a minute."""
    from bettermemory import health_stats, verify_basis

    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    head = _plain(repo, "rev-parse", "HEAD")
    assert _seen(_hit(repo, [_FOO], anchor)) == (1, (_FOO,), verify.BASIS_REACHABILITY)
    met = _failing(monkeypatch, names, listing)
    error = CommitDriftUnread.ERROR
    assert _unread_or_hit(repo, [_FOO], anchor) is error
    assert met == ["names", "listing", "held"]
    _caches.clear_all()
    assert verify_basis._count_since_verify(repo, _VERIFIED, anchor) is error
    _caches.clear_all()
    history = origin_dates.commit_author_timestamps(repo)
    assert isinstance(history, list)
    rows, unread = health_stats._drift_rows_for_candidates(
        [_stats("m")],
        root=repo,
        timestamps=history,
        toplevel=repo,
        anchor_paths_by_id={"m": (_MOD,)},
        claims_by_id=None,
        verified_head_by_id={"m": anchor},
        head=head,
    )
    assert (rows, unread) == ([], {"m": error})
    assert bool(origin._WALK_TIMED_OUT) == (names == "timeout")
    if names == "timeout":
        met.clear()
        unwalked = origin_history.unwalked()
        assert (
            origin.commits_since_anchor(repo, anchor, toplevel=repo, head=head) is None
        )
        assert origin_history.given_up_since(unwalked) is error and met == []
    monkeypatch.setattr(origin, "walk_files_signature", lambda *a, **k: None)
    _caches.clear_all()
    met.clear()
    walked: origin.Walked = {}
    for served in (False, True):
        failed, unwalked = _git.failed_calls(), origin_history.unwalked()
        timed_out = _git.timed_out_calls()
        got = verify.resolve_hit_drift(
            cwd=repo,
            toplevel=repo,
            head=head,
            timestamps=lambda: origin_dates.commit_author_timestamps(repo),
            anchors=(),
            claims=load_claims([_FOO]),
            since=_VERIFIED,
            verified_head=anchor,
            walked=walked,
        )
        assert got is error
        assert origin_history.unwalked() == unwalked + 1
        assert origin_history.given_up_since(unwalked) is error
        assert _git.failed_calls() > failed
        if served:
            assert _git.timed_out_calls() == timed_out, "a failure, not a timeout"
    assert met == ["names", "listing", "held"], "the second hit ran no walk"


def test_a_listing_that_answers_after_the_names_failed_reads_the_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git fails on the walk's names and the listing answers with no walk,
    here an empty one, the head moved back behind the anchor: the dates
    are the basis, as for a walk whose names were read, and the anchor's
    presence is not asked."""
    repo = _repository(tmp_path)
    _rebased(repo)
    anchor = _plain(repo, "rev-parse", "HEAD")
    _plain(repo, "reset", "-q", "--hard", "HEAD~1")
    met = _failing(monkeypatch, "exit 128", "read")
    got = _unread_or_hit(repo, [_FOO], anchor)
    assert not isinstance(got, CommitDriftUnread) and got is not None
    assert got.basis == verify.BASIS_AUTHOR_DATE
    assert met == ["names", "listing"]


@pytest.mark.parametrize(("names", "listing"), _LISTING_FAILS)
def test_a_listing_that_fails_from_an_anchor_not_held_reads_the_dates(
    names: str, listing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same failures from an anchor the repository does not hold
    (``git cat-file -e`` exits 1) give no walk, and the count reads the
    author dates, as git's own refusal of such an anchor does, with real
    git and no failure simulated."""
    repo = _repository(tmp_path)
    _rebased(repo)
    gone = "f" * 40
    dated = (0, (), verify.BASIS_AUTHOR_DATE)
    assert _seen(_hit(repo, [_FOO], gone)) == dated, "git refuses it"
    met = _failing(monkeypatch, names, listing)
    assert _seen(_hit(repo, [_FOO], gone)) == dated
    assert met == ["names", "listing", "held"]
    assert not origin._WALK_TIMED_OUT


@pytest.mark.parametrize(
    ("held", "why"),
    [("timeout", "timeout"), ("killed", "error"), ("exit 128", None)],
)
def test_a_held_check_that_does_not_answer_is_no_evidence_the_anchor_is_gone(
    held: str, why: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that the repository holds the anchor meets its timeout:
    the walk is given up on as a timeout, unread, and kept a minute; one
    killed by a signal answers nothing either, and reads `error`, unread,
    for the pass alone. Only an exit git gives for an object it does not
    hold, or one that is no commit (128 with the ``^{commit}`` peel),
    reads as an anchor not held, and the dates."""
    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    met = _failing(monkeypatch, "exit 128", "exit 128", held)
    got = _unread_or_hit(repo, [_FOO], anchor)
    if why is None:
        assert not isinstance(got, CommitDriftUnread)
        assert _seen(got) == (0, (), verify.BASIS_AUTHOR_DATE)
    else:
        assert got is CommitDriftUnread(why)
    assert len(origin._WALK_TIMED_OUT) == (why == "timeout")
    assert met == ["names", "listing", "held"]


@pytest.mark.parametrize("flag", ["--boundary", "--name-only"])
def test_a_walk_that_met_its_timeout_is_walked_once_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """The health rollup has no per-row memo, so each call walked m's
    anchor again, a 5 s bound or two each time (ENG-220's review, round
    E). The walk's names meet their timeout (the commits listed alone,
    counted 2) or its listing does too (given up, m unread). Called again
    within the minute, the rollup runs no walk and reads m the same,
    counted as a timeout again, and given up again where it was; past the
    minute, and after a commit moves the head, it walks again. The walk
    memo keeps neither."""
    from bettermemory import health
    from bettermemory.origin import Origin

    repo = _repository(tmp_path)
    _plain(repo, "remote", "add", "origin", _URL)
    anchor = _rebased(repo)
    given_up = flag == "--boundary"
    outcome = ([], 1) if given_up else ([2], 0)
    per = 2 if given_up else 1

    def debt() -> tuple[list[int], int]:
        got = health._compute_commit_drift_debt(
            by_id={"m": _stats("m")},
            origin_repo_by_id={"m": _URL},
            caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
            anchor_paths_by_id={"m": (_MOD,)},
            claims_by_id=None,
            verified_head_by_id={"m": anchor},
        )
        assert got is not None
        return [row.commits_since_verify for row in got.rows], got.unread

    def walks() -> int:
        """The walks that met their timeout; the dates' extension after a
        commit (`origin_dates`, ``--format=%m%H %at``) holds the flag too."""
        return sum("--format=%m%H %at" not in argv for argv in met)

    _caches.clear_all()
    met = _timing_out(monkeypatch, flag)
    assert debt() == outcome and walks() == per
    timed_out, unwalked = _git.timed_out_calls(), origin_history.unwalked()
    assert debt() == outcome and walks() == per, "no walk within the minute"
    assert _git.timed_out_calls() > timed_out
    assert origin_history.unwalked() == unwalked + given_up
    assert not origin._WALK_MEMO and len(origin._WALK_TIMED_OUT) == 1
    monkeypatch.setattr(origin, "_WALK_TIMED_OUT_SECONDS", 0.0)
    assert debt() == outcome and walks() == 2 * per, "past the minute"
    monkeypatch.setattr(origin, "_WALK_TIMED_OUT_SECONDS", 60.0)
    _commit(repo, {"notes.md": "n\n"}, 2)
    moved = ([], 1) if given_up else ([3], 0)
    assert debt() == moved and walks() == 3 * per, "a commit moves the key"
    assert debt() == moved and walks() == 3 * per


def test_a_walk_that_met_its_timeout_is_not_kept_where_the_files_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files beside the history change while the walk runs, as a fetch
    or a replace ref written meanwhile changes them: its outcome is not
    kept under the key read before it, and the next call walks again."""
    repo = _repository(tmp_path)
    anchor = _rebased(repo)
    head = _plain(repo, "rev-parse", "HEAD")
    real_signature = origin.walk_files_signature
    moved = [0]

    def signature(toplevel: Path, **kwargs: Any) -> Any:
        read = real_signature(toplevel, **kwargs)
        return None if read is None else (*read, moved[0])

    monkeypatch.setattr(origin, "walk_files_signature", signature)
    real = subprocess.run
    met: list[Any] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "--boundary" in argv:
            met.append(argv)
            moved[0] += 1
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5.0))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    for _ in range(2):
        assert (
            origin.commits_since_anchor(repo, anchor, toplevel=repo, head=head) is None
        )
        assert not origin._WALK_TIMED_OUT
    assert len(met) == 4, "walked again"


_URL = "git@github.com:example/as-stored.git"


def _stats(memory_id: str) -> Any:
    from bettermemory.health_model import MemoryStats

    return MemoryStats(
        id=memory_id,
        scopes=[],
        summary="s",
        created=_VERIFIED,
        updated=_VERIFIED,
        last_verified_at=_VERIFIED,
    )


class _Elsewhere:
    """An anchoring stand-in that resolves every cited commit at `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def for_record(self, memory_id: str) -> _Elsewhere:
        return self

    def commit_root(self, cited: str | None) -> tuple[str, str]:
        return str(self.root), "stand-in"


@pytest.mark.parametrize("drifting", [False, True], ids=["alone", "beside a row"])
def test_the_health_rollups_count_a_walk_given_up_as_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drifting: bool
) -> None:
    """Where the walk from m's anchor and its listing meet their timeout,
    m gets no row and is counted under `unread`, alone (where the rollup
    read as caught up) and beside d, which the dates count: in
    `commit_drift_debt`, counted at the caller's checkout or at a root its
    cited commit resolves at, and in the estate check's groups, at a live
    view and at a recorded worktree; and the text report says so."""
    from types import SimpleNamespace

    from bettermemory import health, health_render
    from bettermemory.health_model import CrossRepoDrift
    from bettermemory.origin import Origin

    repo = _repository(tmp_path)
    _plain(repo, "remote", "add", "origin", _URL)
    anchor = _rebased(repo)
    ids = ["m", "d"] if drifting else ["m"]
    candidates = [_stats(memory_id) for memory_id in ids]
    legs: dict[str, Any] = {
        "anchor_paths_by_id": {memory_id: (_MOD,) for memory_id in ids},
        "claims_by_id": None,
        "verified_head_by_id": {"m": anchor},
    }
    met = _timing_out(monkeypatch, "--boundary")
    _caches.clear_all()
    debt = health._compute_commit_drift_debt(
        by_id={stats.id: stats for stats in candidates},
        origin_repo_by_id={memory_id: _URL for memory_id in ids},
        caller_origin=Origin(cwd=str(repo), repo=_URL, branch=None),
        **legs,
    )
    assert met, "premise: the walk met its timeout"
    assert debt is not None
    drifted = ["d"] if drifting else []
    assert ([row.id for row in debt.rows], debt.unread) == (drifted, 1)
    assert debt.to_dict()["unread"] == 1
    assert debt.to_dict()["unread_causes"] == {"timeout": 1}
    assert debt.to_dict()["unread_ids"] == ["m"]
    history = origin_dates.commit_author_timestamps(repo)
    assert history is not None
    rows, unconfirmed, unread = health._drift_rows_by_root(
        candidates,
        cwd_path=repo,
        timestamps=history,
        toplevel=None,
        head=None,
        anchoring=cast(Any, _Elsewhere(repo)),
        **legs,
    )
    assert ([row.id for row in rows], unconfirmed) == (drifted, 0)
    assert unread == {"m": CommitDriftUnread.TIMEOUT}
    live = health._cross_repo_group(_URL, str(repo), candidates, **legs)
    skipped: list[dict[str, str]] = []
    recorded = health._recorded_root_group(
        _URL, str(repo), repo, candidates, skipped, **legs
    )
    assert skipped == []
    for group in (live, recorded):
        assert group is not None
        assert (group.total_drifted, group.unread) == (len(drifted), 1)
        assert group.to_dict()["unread"] == 1
    assert live is not None
    report = SimpleNamespace(
        commit_drift_debt=debt, cross_repo_drift=CrossRepoDrift(groups=[live])
    )
    text = "\n".join(health_render._drift_lines(cast(Any, report)))
    assert "unread (1: timeout 1)" in text and "caught up" not in text
    assert f"{len(drifted)} drifted, 1 unread of {len(ids)} candidate(s)" in text
