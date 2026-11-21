"""Fixture: the readers of `location_git` that need neither discovery nor
the object reader's answers beyond a tag's peel
(``location_git_readers.jsonl`` and ``location_git_readers_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_git.rs``.

``location_git_readers.jsonl`` (ungated) holds synthetic histories run
through `reachable_among` and `walk`. A history is ``"n"`` commits whose
ids are `_id` (the first 40 hex digits of SHA-256 of ``c<i>``), ``"p"``
each commit's parents by index, ``"t"`` each committer time (absent:
`committed` not given). A given commit is an index or a text given as
is. Answers are `_line`'s text, or ``{"n": length, "h": SHA-256}`` of it
where it is longer than 600 characters. The last row is a digest over
`SWEEP` seeded random histories (`_sweep`): the rule is `_Rng`
(xorshift64*), the count and the SHA-256 of every answer line in order.

``location_git_readers_posix.jsonl`` (POSIX only: links and mode-000
entries; names are UTF-8, which macOS's file systems require) holds trees built in scratch, each a list of
items (`_build`), and the answers of `_ref_tips` over the common
directory ``g``, `_manifest` of the folder ``proj`` and `_resolve_head`
of ``g`` (or its worktree ``g/worktrees/w``). Not run as root.
"""

from __future__ import annotations

import hashlib
import os
import zlib
from collections.abc import Iterator
from typing import Any

from bettermemory import location_git

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b

SWEEP = 2000
SEED = 0x9E3779B97F4A7C15
_MASK = (1 << 64) - 1
_LONG = 600


def _id(i: int) -> str:
    return hashlib.sha256(f"c{i}".encode()).hexdigest()[:40]


def _hash(seed: str, i: int) -> str:
    return hashlib.sha256(f"{seed}{i}".encode()).hexdigest()[:40]


class _Rng:
    """xorshift64*: the sweep's rule, mirrored by the Rust test."""

    def __init__(self, seed: int) -> None:
        self.state = seed

    def next(self, bound: int) -> int:
        x = self.state
        x ^= x >> 12
        x ^= (x << 25) & _MASK
        x ^= x >> 27
        self.state = x
        return ((x * 0x2545F4914F6CDD1D) & _MASK) % bound


# -- histories ---------------------------------------------------------------


class _History:
    def __init__(self, parents: list[list[int]], upper: bool = False) -> None:
        self.ids = [_id(i) for i in range(len(parents))]
        self.index = {sha: i for i, sha in enumerate(self.ids)}
        self.lines = parents
        self.upper = upper

    def parents(self, sha: str) -> list[str]:
        i = self.index.get(sha.lower())
        if i is None:
            return []
        out = [self.ids[p] for p in self.lines[i]]
        return [p.upper() for p in out] if self.upper else out

    def fingerprint(self, sha: str) -> str | None:
        i = self.index.get(sha.lower())
        return None if i is None or i % 3 == 0 else f"f{i}"

    def name(self, sha: str) -> str:
        i = self.index.get(sha.lower())
        return sha if i is None else str(i)


def _given(h: _History, given: list[Any]) -> list[str]:
    return [h.ids[g] if isinstance(g, int) else g for g in given]


def _reach_line(h: _History, reach: location_git.Reach) -> str:
    rel = ";".join(
        h.name(c) + ":" + ",".join(h.name(o) for o in sorted(reach[c]))
        for c in sorted(reach)
    )
    return f"{rel}|{int(reach.bounded)}|{reach.handled}"


def _walk_line(h: _History, out: dict[str, Any]) -> str:
    return ",".join(
        f"{h.name(s)}/{fp or '-'}/" + "+".join(h.name(p) for p in ps)
        for s, (fp, ps) in out.items()
    )


def _shown(line: str) -> Any:
    if len(line) <= _LONG:
        return line
    return {"n": len(line), "h": hashlib.sha256(line.encode()).hexdigest()}


def _reach_row(
    parents: list[list[int]],
    given: list[Any],
    times: list[int] | None = None,
    limit: int = location_git.WALK_LIMIT,
    edges: int = location_git.EDGE_LIMIT,
    upper: bool = False,
    note: str = "",
) -> dict[str, Any]:
    h = _History(parents, upper)
    committed = None if times is None else (lambda s: times[h.index.get(s.lower(), 0)])
    reach = location_git.reachable_among(
        h, _given(h, given), limit=limit, edges=edges, committed=committed
    )
    row: dict[str, Any] = {"fn": "reach", "note": note, "p": parents, "g": given}
    if times is not None:
        row["t"] = times
    if upper:
        row["up"] = 1
    row.update({"limit": limit, "edges": edges, "a": _shown(_reach_line(h, reach))})
    return row


def _walk_row(
    parents: list[list[int]], head: Any, tips: list[int], note: str = ""
) -> dict[str, Any]:
    h = _History(parents)
    head_sha = h.ids[head] if isinstance(head, int) else head
    out = location_git.walk(h, head_sha, [h.ids[t] for t in tips])
    return {
        "fn": "walk",
        "note": note,
        "p": parents,
        "head": head,
        "tips": tips,
        "a": _shown(_walk_line(h, out)),
    }


def _chain(n: int) -> list[list[int]]:
    return [[i + 1] if i + 1 < n else [] for i in range(n)]


def _history_rows() -> Iterator[dict[str, Any]]:
    yield _reach_row([], [], note="nothing given")
    yield _reach_row(_chain(5), [0, 4], note="a chain, child and root")
    yield _reach_row(
        _chain(5), [4, 0, 0, "zz", _id(0).upper()], note="dups, bad, upper"
    )
    yield _reach_row(_chain(5), [0, 4], upper=True, note="parents spelled upper")
    yield _reach_row(_chain(5), [0, "0" * 64], note="a 64-digit id not in the history")
    merge = [[1, 2], [3], [3], []]
    yield _reach_row(merge, [0, 1, 2, 3], note="a diamond")
    yield _reach_row(merge, [0, 1, 2, 3], times=[40, 30, 20, 10], note="times falling")
    yield _reach_row(merge, [0, 3], times=[10, 30, 20, 40], note="a skewed clock")
    yield _reach_row(_chain(8), [0, 7], times=[0] * 8, note="time zero")
    big = (1 << 64) - 1
    yield _reach_row(
        _chain(4), [0, 3], times=[big, big, 0, big], note="times at 2**64-1"
    )
    yield _reach_row(_chain(4), [0, 3], times=[0, 0, big, 0], note="a rise of 2**64-1")
    for limit in (1, 2, 3, 4):
        yield _reach_row(_chain(6), [0, 5], limit=limit, note=f"limit {limit}")
    for edges in (0, 1, 3, 5):
        yield _reach_row(_chain(6), [0, 5], edges=edges, note=f"edges {edges}")
    octo = [[1, 2, 3, 4], [5], [5], [5], [5], []]
    yield _reach_row(octo, [0, 5], edges=4, note="an octopus at the edge bound")
    yield _reach_row(octo, [0, 5], edges=3, note="an octopus past the edge bound")
    skew = [[1], [2], [3], [4], [5], []]
    yield _reach_row(
        skew,
        [0, 2, 5],
        times=[100, 90, 80, 200, 70, 60],
        limit=4,
        note="held, released, cut",
    )
    yield _reach_row(
        skew, [0, 5], times=[100, 90, 80, 10, 70, 60], note="held below the floor"
    )
    yield _reach_row(
        skew, [0, 5], times=[100, 90, 80, 10, 70, 60], limit=4, note="held then bounded"
    )
    yield _reach_row(_chain(300), list(range(0, 260, 2)), note="130 given on a chain")
    yield _reach_row(_chain(300), list(range(0, 300)), limit=128, note="past the limit")
    yield _reach_row([[0]], [0], note="a commit its own parent")
    yield _reach_row([[1], [0]], [0, 1], note="a cycle")
    yield _walk_row([], None, [], note="nothing")
    yield _walk_row(_chain(4), 0, [], note="a chain from head")
    yield _walk_row(merge, None, [3, 1], note="unborn: the tips")
    yield _walk_row(merge, "", [2], note="empty head: the tips")
    yield _walk_row(merge, "zz", [], note="a head not in the history")
    yield _walk_row(_chain(600), 0, [], note="past WALK_LIMIT")
    yield _walk_row([[1, 1], [0]], 0, [], note="a cycle and a repeated parent")


def _random(rng: _Rng) -> tuple[list[list[int]], list[int] | None, list[int], int, int]:
    n = 1 + rng.next(30)
    parents = [
        [i + 1 + rng.next(n - i - 1) for _ in range(rng.next(4))] if i < n - 1 else []
        for i in range(n)
    ]
    times: list[int] | None = [1000 - 10 * i for i in range(n)]
    for i in range(n):
        if rng.next(3) == 0:
            times[i] += rng.next(101) - 50  # type: ignore[index]
    if rng.next(5) == 0:
        times = None
    given = sorted({rng.next(n) for _ in range(1 + rng.next(8))})
    limit = 1 + rng.next(40)
    edges = 1 + rng.next(60) if rng.next(2) else location_git.EDGE_LIMIT
    return parents, times, given, limit, edges


def _sweep() -> dict[str, Any]:
    rng = _Rng(SEED)
    digest = hashlib.sha256()
    for _ in range(SWEEP):
        parents, times, given, limit, edges = _random(rng)
        h = _History(parents)
        committed = None if times is None else (lambda s, t=times: t[h.index[s]])
        reach = location_git.reachable_among(
            h, [h.ids[g] for g in given], limit=limit, edges=edges, committed=committed
        )
        out = location_git.walk(h, h.ids[given[0]] if given[0] % 2 else None, h.ids[:2])
        digest.update(
            (_reach_line(h, reach) + "#" + _walk_line(h, out) + "\n").encode()
        )
    return {"fn": "sweep", "seed": SEED, "count": SWEEP, "h": digest.hexdigest()}


def _history_file() -> bytes:
    rows = [*_history_rows(), _sweep()]
    return jsonl([{"fixture": "location_git_readers", "cases": len(rows)}, *rows])


# -- trees -------------------------------------------------------------------

#: The ten ASCII bytes `str.strip` and `str.split` take as whitespace.
SPACES = b"\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f "


#: Each object's deflate stream, written out rather than made by
#: `zlib.compress`: the bytes a compressor writes follow its implementation
#: (zlib, zlib-ng), and this ungated file must regenerate the same on every
#: runner. Only `zlib.decompress` is asked, and it is checked against the
#: object it must give.
_DEFLATED = {
    b"commit": bytes.fromhex(
        "789c5dcc310a80300c4051e79e22bb4b28694940bc4b93a6e8500a52efaf38fac7377c"
        "1bbd9f13849679b90329c7542d5334cdae5230a327d2c6354b646eea4e8214ca3d8f71"
        "4181adec80b0e25bb06f36fdefa1870742191ccb"
    ),
    b"tag": bytes.fromhex(
        "789c15cc310a80300c4051e79c22bb4b12452d8877a94d5a148a2241f0f6ea5fdef63d"
        "160cdc1ceb6ec97162c943144ddad348aaa644d328d275894d7396cccc8102f8731aa6"
        "a3d6cdc1bfc5cd3fc52e8c38c705095bfa02a8f0028f0e1ba6"
    ),
}


def _object(kind: bytes, body: bytes) -> tuple[str, bytes]:
    raw = kind + b" " + str(len(body)).encode() + b"\0" + body
    deflated = _DEFLATED[kind]
    if zlib.decompress(deflated) != raw:
        raise AssertionError(
            f"the {kind.decode()} object's stream gives another object"
        )
    return hashlib.sha1(raw).hexdigest(), deflated


_COMMIT_SHA, _COMMIT = _object(
    b"commit",
    b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
    b"author a <a> 0 +0000\ncommitter a <a> 0 +0000\n\nm\n",
)
_TAG_SHA, _TAG = _object(
    b"tag",
    f"object {_COMMIT_SHA}\ntype commit\ntag v1\ntagger a <a> 0 +0000\n\nm\n".encode(),
)


def _objects() -> list[list[Any]]:
    return [
        ["f", f"objects/{s[:2]}/{s[2:]}".encode(), body]
        for s, body in ((_COMMIT_SHA, _COMMIT), (_TAG_SHA, _TAG))
    ]


def _build(base: bytes, items: list[list[Any]]) -> None:
    modes = []
    for item in items:
        kind, rel = item[0], item[1]
        path = base + b"/" + rel
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if kind == "f":
            with open(path, "wb") as fh:
                fh.write(item[2])
        elif kind == "d":
            os.makedirs(path, exist_ok=True)
        elif kind == "l":
            os.symlink(item[2], path)
        elif kind == "m":
            modes.append(path)
        elif kind == "n":
            os.makedirs(path, exist_ok=True)
            for i in range(item[2]):
                with open(path + b"/f%04d" % i, "wb") as fh:
                    fh.write(b"x" * item[3])
        elif kind == "r":
            os.makedirs(path, exist_ok=True)
            for i in range(item[2]):
                with open(path + b"/%03d" % i, "wb") as fh:
                    fh.write(_hash(item[3], i).encode() + b"\n")
        elif kind == "q":
            with open(path, "wb") as fh:
                fh.write(b"# pack-refs with: peeled fully-peeled sorted \n")
                for i in range(item[2]):
                    fh.write(f"{_hash(item[3], i)} refs/heads/p{i:03d}\n".encode())
    for path in modes:
        os.chmod(path, 0)


def _record(items: list[list[Any]]) -> list[list[Any]]:
    return [[_b(x) if isinstance(x, bytes) else x for x in item] for item in items]


def _open_up(base: bytes) -> None:
    for top, dirs, files in os.walk(base):
        for name in dirs + files:
            path = os.path.join(top, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)


def _tip_cases() -> Iterator[tuple[str, list[list[Any]]]]:
    h = _hash("t", 0)
    yield "no refs", []
    yield "one loose", [["f", b"refs/heads/main", h.encode() + b"\n"]]
    for b in SPACES:
        yield (
            f"space {b:#x}",
            [["f", b"refs/heads/main", bytes([b]) + h.encode() + bytes([b])]],
        )
    yield "upper", [["f", b"refs/heads/main", h.upper().encode()]]
    yield "39 digits", [["f", b"refs/heads/main", h[:39].encode()]]
    yield (
        "64 digits",
        [["f", b"refs/heads/main", _hash("t", 1).encode() + h[:24].encode()]],
    )
    yield "a 0xff byte", [["f", b"refs/heads/main", h.encode() + b"\xff"]]
    yield "128 kept", [["f", b"refs/heads/main", b" " * 88 + h.encode() + b"junk"]]
    yield "128 cut", [["f", b"refs/heads/main", b" " * 89 + h.encode()]]
    yield "junk inside 128", [["f", b"refs/heads/main", h.encode() + b"\njunk"]]
    yield "junk past 128", [["f", b"refs/heads/main", h.encode() + b" " * 88 + b"junk"]]
    yield (
        "70 loose",
        [["r", b"refs/heads", 40, "a"], ["r", b"refs/remotes/origin", 30, "b"]],
    )
    yield (
        "names in str order",
        [
            ["r", b"refs/heads/a", 63, "c"],
            ["f", "refs/heads/b/\u65e5".encode(), _hash("u", 0).encode()],
            ["f", "refs/heads/b/\u00e9".encode(), _hash("u", 1).encode()],
        ],
    )
    yield (
        "files before folders",
        [
            ["r", b"refs/heads/a", 63, "d"],
            ["f", b"refs/heads/z", _hash("u", 2).encode()],
            ["f", b"refs/heads/a0", _hash("u", 3).encode()],
        ],
    )
    yield (
        "links",
        [
            ["f", b"other/x", _hash("v", 0).encode()],
            ["f", b"other2", _hash("v", 1).encode()],
            ["l", b"refs/heads/dir", b"../../other"],
            ["l", b"refs/heads/file", b"../../other2"],
            ["l", b"refs/heads/broken", b"nowhere"],
        ],
    )
    yield (
        "unreadable",
        [
            ["f", b"refs/heads/a", _hash("w", 0).encode()],
            ["f", b"refs/heads/b", _hash("w", 1).encode()],
            ["f", b"refs/heads/sub/c", _hash("w", 2).encode()],
            ["m", b"refs/heads/b"],
            ["m", b"refs/heads/sub"],
        ],
    )
    yield "refs/heads a file", [["f", b"refs/heads", h.encode()]]
    lines = [
        b"# pack-refs with: peeled\n",
        _hash("p", 0).encode() + b" refs/heads/a\n",
        b"^" + _hash("p", 1).encode() + b"\n",
        _hash("p", 2).encode() + b" refs/tags/t\n",
        _hash("p", 3).encode() + b"\trefs/remotes/o/b\r\n",
        _hash("p", 4).encode() + b"\x0brefs/heads/c\x1c\n",
        _hash("p", 5).encode() + b" refs/heads/d extra\n",
        _hash("p", 6).upper().encode() + b" refs/heads/e\n",
        b"nothash refs/heads/f\n",
        _hash("p", 7).encode() + b" refs/heads\n",
        _hash("p", 8).encode() + b" refs/heads/\xffg\n",
        b"\xff" + _hash("p", 9).encode() + b" refs/heads/h\n",
        _hash("p", 10).encode() + b"\x85refs/heads/i\n",
        b"  " + _hash("p", 11).encode() + b"   refs/heads/j",
    ]
    yield "packed lines", [["f", b"packed-refs", b"".join(lines)]]
    yield "packed past 64", [["q", b"packed-refs", 70, "q"]]
    yield (
        "loose and packed",
        [
            ["r", b"refs/heads", 10, "q"],
            ["q", b"packed-refs", 60, "q"],
        ],
    )
    yield "packed a folder", [["d", b"packed-refs"]]
    yield "packed unreadable", [["q", b"packed-refs", 3, "r"], ["m", b"packed-refs"]]


def _manifest_cases() -> Iterator[tuple[str, list[list[Any]], bool]]:
    yield "empty", [["d", b"."]], False
    yield (
        "files",
        [
            ["f", b"a", b"12345"],
            ["f", b"bettermemory.toml", b"x"],
            ["f", b"sub/bettermemory.toml", b"xy"],
            ["f", b".git", b"gitdir: x"],
            ["f", b".git2", b""],
            ["f", b"sub/.git/config", b"zzz"],
            ["f", b"B", b""],
            ["f", b"sub/deeper/c", b"1"],
        ],
        False,
    )
    yield "trailing slash", [["f", b"a", b"1"]], True
    yield (
        "links",
        [
            ["f", b"t/x", b"abc"],
            ["l", b"ld", b"t"],
            ["l", b"lf", b"t/x"],
            ["l", b"lb", b"nowhere/at/all"],
        ],
        False,
    )
    yield (
        "unreadable",
        [
            ["f", b"u/x", b"1"],
            ["f", b"v", b"22"],
            ["m", b"u"],
            ["m", b"v"],
        ],
        False,
    )
    yield (
        "names past ASCII",
        [
            ["f", "\u65e5".encode(), b"1"],
            ["f", "\u00e9".encode(), b"22"],
            ["f", "\u00ff/a".encode(), b"333"],
            ["f", b"z", b""],
        ],
        False,
    )
    yield "at the limit", [["n", b".", 4096, 1], ["f", b"zz/after", b"1"]], False
    yield "past the limit", [["n", b".", 4100, 2]], False
    yield (
        "stack order",
        [
            ["n", b"a", 3000, 0],
            ["n", b"b", 1000, 3],
            ["n", b"b/c", 200, 1],
            ["f", b"b/.git/x", b"1"],
            ["f", b"b/bettermemory.toml", b"12"],
            ["n", b"d", 10, 1],
            ["f", b"root", b"1"],
        ],
        False,
    )


def _head_cases() -> Iterator[tuple[str, list[list[Any]], bool, str | None]]:
    h = _hash("h", 0)
    main = ["f", b"refs/heads/main", h.encode() + b"\n"]
    yield "branch", [["f", b"HEAD", b"ref: refs/heads/main\n"], main], False, None
    yield "unborn", [["f", b"HEAD", b"ref: refs/heads/main\n"]], False, None
    yield "detached upper", [["f", b"HEAD", h.upper().encode() + b"\n"]], False, None
    yield "no HEAD", [], False, None
    yield (
        "a nested branch",
        [
            ["f", b"HEAD", b"ref: refs/heads/f/x\n"],
            ["f", b"refs/heads/f/x", h.encode()],
        ],
        False,
        None,
    )
    yield (
        "a tag",
        [
            ["f", b"HEAD", b"ref: refs/tags/v1\n"],
            ["f", b"refs/tags/v1", _TAG_SHA.encode() + b"\n"],
            *_objects(),
        ],
        False,
        _TAG_SHA,
    )
    yield (
        "a tag without its object",
        [
            ["f", b"HEAD", b"ref: refs/tags/v1\n"],
            ["f", b"refs/tags/v1", h.encode() + b"\n"],
        ],
        False,
        h,
    )
    yield (
        "a remote ref at a commit",
        [
            ["f", b"HEAD", b"ref: refs/remotes/o/m\n"],
            ["f", b"refs/remotes/o/m", _COMMIT_SHA.encode() + b"\n"],
            *_objects(),
        ],
        False,
        _COMMIT_SHA,
    )
    yield (
        "a worktree",
        [
            ["f", b"worktrees/w/HEAD", b"ref: refs/heads/main\n"],
            ["f", b"HEAD", b"ref: refs/heads/other\n"],
            main,
        ],
        True,
        None,
    )
    yield (
        "packed branch",
        [
            ["f", b"HEAD", b"ref: refs/heads/main\n"],
            ["f", b"packed-refs", f"{h} refs/heads/main\n".encode()],
        ],
        False,
        None,
    )


def _show_manifest(found: tuple[str, tuple[tuple[str, int], ...]]) -> Any:
    name, entries = found
    rows = [f"{_b(os.fsencode(p))} {s}" for p, s in entries]
    return {"name": _b(os.fsencode(name)), "e": _shown("\n".join(rows))}


def _posix_file() -> bytes:
    refuse_root()
    rows: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        top = os.fsencode(world.root) + b"/readers"
        n = 0
        for note, items in _tip_cases():
            base = top + b"/%d" % n
            n += 1
            _build(base + b"/g", items)
            os.makedirs(base + b"/g", exist_ok=True)
            g = location_git._Git(
                None, os.fsdecode(base + b"/g"), os.fsdecode(base + b"/g"), None, "bare"
            )
            rows.append(
                {
                    "fn": "tips",
                    "note": note,
                    "t": _record(items),
                    "a": location_git._ref_tips(g),
                }
            )
            _open_up(base)
        for note, items, slash in _manifest_cases():
            base = top + b"/%d" % n
            n += 1
            _build(base + b"/proj", items)
            root = os.fsdecode(base + b"/proj") + ("/" if slash else "")
            found = location_git._manifest(root)
            row: dict[str, Any] = {"fn": "manifest", "note": note, "t": _record(items)}
            if slash:
                row["slash"] = 1
            rows.append({**row, "a": _show_manifest(found)})
            _open_up(base)
        for note, items, worktree, peel in _head_cases():
            base = top + b"/%d" % n
            n += 1
            _build(base + b"/g", items)
            os.makedirs(base + b"/g", exist_ok=True)
            gd = base + (b"/g/worktrees/w" if worktree else b"/g")
            g = location_git._Git(
                None, os.fsdecode(gd), os.fsdecode(base + b"/g"), None, "primary"
            )
            sha, branch = location_git._resolve_head(g)
            row = {"fn": "head", "note": note, "t": _record(items)}
            if worktree:
                row["w"] = 1
            if peel is not None:
                row["peel"] = peel
            rows.append({**row, "a": [sha, branch]})
    return jsonl([{"fixture": "location_git_readers_posix", "cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"location_git_readers.jsonl": _history_file()}


def generate_posix() -> dict[str, bytes]:
    return {"location_git_readers_posix.jsonl": _posix_file()}
