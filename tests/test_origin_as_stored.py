"""Claim drift reads the history as its objects hold it (ENG-220's review).

A repository's commit-graph and shallow file are its own, written by git
for speed or left by a shallow clone, and a hostile ``.git`` can forge
either. The graph names each commit's parents, root tree and changed-path
Bloom filter in place of the objects, and git read it for the windows while
the patch read the objects: a forged graph hid the commit that changed a
claim, and the count read 0. No origin call reads it now
(`origin._AS_STORED_CONFIG`). The shallow file cuts the history at the
commits it lists, behind which git reads nothing: a range that holds a cut
is not walked, and a shallow repository's dates are not read
(`origin_history.cuts`), so no count is read where the commits behind the
cut may be the window's.

Each repository is built with git's plumbing from `test_origin_patch`'s
helpers; the graph is forged by rewriting its chunks in place.
"""

from __future__ import annotations

import shutil
import struct
from datetime import timedelta
from pathlib import Path

import pytest

from bettermemory import _caches, _response, origin, origin_history
from bettermemory.claims import load_claims
from bettermemory.verify import ResolvedCommitDrift

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
        assert not set(partial.commits) & set(_touching(repo)), "premise"
    else:
        _forge_graph(repo, blind=True)
        assert changed not in _touching(repo), "premise"
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


def _commit(repo: Path, files: dict[str, str | None], hours: float) -> str:
    """A commit of `files` (None deletes one) dated `hours` after the
    verify, or before it where negative."""
    for rel, text in files.items():
        if text is None:
            _plain(repo, "update-index", "--force-remove", "--", rel)
        else:
            oid = _object(repo, text.encode())
            _plain(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{rel}")
    when = _VERIFIED + timedelta(hours=hours)
    _plain(repo, "commit", "-q", "--allow-empty", "-m", f"at {hours}", when=when)
    return _plain(repo, "rev-parse", "HEAD")


def _hit(
    repo: Path, claims: list[str], verified_head: str | None
) -> ResolvedCommitDrift | None:
    """One search hit's drift, cold, as the recall path resolves it: on
    the walk from `verified_head` where it answers, else on dates."""
    _caches.clear_all()
    return _response._resolve_hit_drift(
        cwd=repo,
        toplevel=repo,
        head=_plain(repo, "rev-parse", "HEAD"),
        timestamps=lambda: origin.commit_author_timestamps(repo),
        anchors=(),
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
    read behind a cut: the hit's count is omitted (None), never a measured
    0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    anchor, cut, claims = build(repo)  # type: ignore[operator]
    truth = _hit(repo, claims, anchor)
    assert truth is not None and truth.count >= 1 and truth.claims_drifted
    (repo / ".git" / "shallow").write_text(f"{cut}\n")
    assert len(_plain(repo, "log", "--format=%H").split()) < 5, "premise: cut"
    assert _hit(repo, claims, anchor) is None
    assert _hit(repo, claims, None) is None


def test_a_shallow_clone_counts_where_the_range_holds_no_cut(tmp_path: Path) -> None:
    """A clone three commits deep is cut at the commit before the change.
    Stamped there, the walk's range holds the change and the commit after
    it and no cut, and counts as a full clone does: 1, foo named. Stamped
    at a commit behind the cut, no count is read."""
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
    assert _hit(shallow, [_FOO], behind) is None


def test_the_cut_is_read_from_the_common_directory(tmp_path: Path) -> None:
    """`origin_history.cuts` reads the shallow file git reads: empty with
    none, its commits with one, and None for a root holding no
    repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    assert origin_history.cuts(repo) == frozenset()
    (repo / ".git" / "shallow").write_text("A" * 40 + "\n\n" + "b" * 40 + "\n")
    assert origin_history.cuts(repo) == frozenset({"a" * 40, "b" * 40})
    assert origin_history.cuts(tmp_path / "nowhere") is None
