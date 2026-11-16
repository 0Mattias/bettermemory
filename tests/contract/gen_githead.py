"""Fixture: HEAD and refs read from a git directory's files
(``githead.jsonl``, ``githead_posix.jsonl``).

The native build reads HEAD and the refs its chain names with a reader of
its own; this pins it to the resolution half of `bettermemory.githead`:
`head_bytes`, `_resolve`, `head_sha`, `head_ref`, `head_unborn` and
`packed_names`. Discovery, the signature and the object peel of
`head_commit` are other units' and are not read here (a HEAD that would be
peeled is recorded through `_resolve`'s answer).

Each case is a git directory as data, written as real files in a fresh
directory of the scratch world (`_sandbox.world`) and read by the
reference. The first line is a header (the counts); then one line per
case:

- ``"w"``: the layout, 0 for a primary git directory ``g`` (also the
  common directory), 1 for a linked worktree's ``g/worktrees/w`` with
  common directory ``g``;
- ``"f"``: the files, path (relative to the case's directory) to bytes:
  ``=`` and the text where it is made of `_SAFE` characters, else hex, or
  ``[[piece, count], ...]``, the pieces repeated and joined (a recipe for
  the large files); `_SAFE` holds letters, digits, space, ``\n``, ``\r``,
  ``\t`` and ``.,=_:-+*~%()[]#;!@'/{}^?&$`` (no backslash, no ``<>|"``); ``"d"``: directories made where a file might be
  expected; ``"l"`` (POSIX file only): symbolic links, path to target;
  ``"p"`` (POSIX file only): FIFOs;
- ``"q"``: the names `packed_names` is asked about, left out where it is
  ``refs/heads/main`` alone;
- ``"r"``: the answers. ``b``: `head_bytes` (bytes as above, or
  ``{"n": length, "sha256": hex}`` past 256 bytes, or null), left out
  where it is the ``HEAD`` written; ``v``: `_resolve` as ``[name or null,
  hash or null]`` or ``"x"`` where it declines (`_Unsettled`) or ``"o"``
  where an OSError or ValueError rose; ``sn``: whether `head_sha` and
  `head_ref` answered (``1``) or not (``0``), each asserted here to be
  ``v``'s hash and name where it answered; ``c``: `head_commit` run to
  its first object read (the reader stopped there): ``"a"`` where it
  answered with no read, ``"p"`` where it asked to read (peel) an
  object, null where it declined first, its hash asserted here to be
  ``v``'s; ``u``: `head_unborn`; ``p``:
  `packed_names` of each name of ``q``, as a string of ``0`` and ``1``.

No answer holds a path: the reference returns bytes, hashes and ref
names, and the case's directory is the only path, which is never
recorded.

Gates. ``githead.jsonl`` is not gated: its cases hold regular files,
directories and missing files only, with ref names every file system
stores as given (ASCII, no character Windows refuses, no name that differs
from another only by case, so a case-folding file system answers as
Linux's does, no Windows device name, short paths), and the reference
reads them with bytes methods and `re` over bytes with ASCII classes; the
same bytes under Python 3.11 and 3.13, on macOS and (by construction) on
Linux and Windows. ``githead_posix.jsonl`` is POSIX only
(`generate.POSIX_ONLY`): symbolic links (refused by ``O_NOFOLLOW``,
which Windows lacks, so the reference follows them there), FIFOs, a file
where a directory of the path should be (``NotADirectoryError`` on POSIX,
``FileNotFoundError`` on Windows, which `_lookup` treats differently),
names with ``<>|"`` and non-ASCII UTF-8, Windows device names, and long
names. Nothing in it depends on case folding or Unicode normalization.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from unittest import mock
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from bettermemory import githead, gitobjects
from bettermemory.gitfile_rule import _REF_LIMIT, _Unsettled

from . import _sandbox
from ._common import jsonl

_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,=_:-+*~%()[]#;!@'/{}^?&$\n\r\t"
)

#: A file's content: bytes, or a recipe of pieces and counts.
_Content = bytes | list[tuple[bytes, int]]

H1 = b"1" * 40
H2 = b"2" * 40
H3 = b"3" * 40
H64 = b"a" * 64
HB64 = b"b" * 64
MAIN = b"refs/heads/main"


def _b(data: bytes) -> str:
    if all(b in _SAFE for b in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _content(data: _Content) -> Any:
    if isinstance(data, bytes):
        return _b(data)
    return [[_b(piece), count] for piece, count in data]


def _expand(data: _Content) -> bytes:
    if isinstance(data, bytes):
        return data
    return b"".join(piece * count for piece, count in data)


def _expand_out(text: str | None) -> bytes | None:
    if text is None:
        return None
    return text[1:].encode("ascii") if text.startswith("=") else bytes.fromhex(text)


def _out(data: bytes | None) -> Any:
    if data is None:
        return None
    if len(data) > 256:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _b(data)


class Case:
    def __init__(
        self,
        files: Mapping[str, _Content],
        *,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        worktree: bool = False,
        names: tuple[bytes, ...] = (),
    ) -> None:
        self.files = dict(files)
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.worktree = worktree
        self.names = names

    def key(self) -> str:
        return json.dumps(self.record(), sort_keys=True)

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"w": int(self.worktree)}
        out["f"] = {k: _content(v) for k, v in self.files.items()}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.links:
            out["l"] = dict(self.links)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.queries() != [MAIN]:
            out["q"] = [_b(n) for n in self.queries()]
        return out

    def queries(self) -> list[bytes]:
        out = [MAIN, *self.names]
        head = self.files.get(self.gitdir() + "/HEAD")
        if isinstance(head, bytes) and head.startswith(b"ref:"):
            target = head[4:].strip(b" \t\n\r")
            if target and target not in out and b"\n" not in target:
                out.append(target)
        return out

    def head(self) -> bytes | None:
        """HEAD's bytes as written, where they are bytes."""
        head = self.files.get(self.gitdir() + "/HEAD")
        return head if isinstance(head, bytes) else None

    def gitdir(self) -> str:
        return "g/worktrees/w" if self.worktree else "g"


def _answer(case: Case, root: Path) -> dict[str, Any]:
    for d in case.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    for rel in case.fifos:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
    (root / "g").mkdir(exist_ok=True)
    gd = githead.GitDir(root / case.gitdir(), root / "g", root)
    try:
        name, sha = githead._resolve(gd)
        resolved: Any = [None if name is None else _b(name), sha]
    except _Unsettled:
        resolved = "x"
    except (OSError, ValueError):
        resolved = "o"
    head = githead.head_bytes(gd)
    out: dict[str, Any] = (
        {} if head is not None and head == case.head() else {"b": _out(head)}
    )
    sha = githead.head_sha(gd)
    ref = githead.head_ref(gd)
    # `head_sha` is `_resolve`'s hash or None, `head_ref` its name or None:
    # each is recorded as which of the two it is, and checked here.
    assert sha is None or (isinstance(resolved, list) and sha == resolved[1])
    assert ref is None or (
        isinstance(resolved, list) and ref.encode() == _expand_out(resolved[0])
    )
    commit = _head_commit(gd)
    if commit is not None:
        # The answer, or the hash the object read would start from, is
        # `_resolve`'s hash: recorded as which of the two it is.
        assert isinstance(resolved, list) and commit[1] == resolved[1]
    return out | {
        "v": resolved,
        "c": None if commit is None else commit[0],
        "sn": f"{int(sha is not None)}{int(ref is not None)}",
        "u": githead.head_unborn(gd),
        "p": "".join(str(int(githead.packed_names(gd, n))) for n in case.queries()),
    }


def _head_commit(gd: githead.GitDir) -> tuple[str, str] | None:
    """`head_commit` run to its first object read: ``("a", hash)`` where it
    answers without one, ``("p", hash)`` where it asks the object reader
    for that hash (the read is stopped there and answers None), None where
    it declines before any read."""
    asked: list[str] = []

    def read(self: object, sha: str, **_: object) -> None:
        asked.append(sha)
        return None

    with mock.patch.object(gitobjects.ObjectReader, "read", read):
        answer = githead.head_commit(gd)
    if asked:
        assert answer is None
        return ("p", asked[0])
    return None if answer is None else ("a", answer)


def _head(text: bytes, **more: _Content) -> dict[str, _Content]:
    files: dict[str, _Content] = {"g/HEAD": text}
    for key, value in more.items():
        files[key] = value
    return files


def _g(path: bytes) -> str:
    return "g/" + path.decode()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

_SUFFIXES = [
    b"",
    b"\n",
    b"\r\n",
    b"\r",
    b" ",
    b"\t",
    b"\n\n",
    b" \n",
    b"\t\r\n",
    b"\x0b\n",
    b"\x0c\n",
    b"\x00",
    b"\n\x00",
    b"\nx",
    b" x\n",
]
_GAPS = [b"", b" ", b"\t", b"  ", b"\n", b"\r", b"\x0b"]
_PREFIXES = [
    b"ref:",
    b"ref: ",
    b" ref: ",
    b"\nref: ",
    b"REF: ",
    b"ref :",
    b"Ref: ",
    b"ref:: ",
]

_HASHES = [
    H1,
    H64,
    b"A" * 40,
    b"aB" * 20,
    b"1" * 39,
    b"1" * 41,
    b"a" * 63,
    b"a" * 65,
    b"g" + b"1" * 39,
    b"1" * 40 + b" x",
    b"1" * 40 + b"\x00",
    b"1" * 40 + b"x",
    b"0" * 40,
    b"f" * 64,
    b"1" * 104,
    b"1" * 80,
    b"1" * 20,
    b"",
    b"1" * 40 + b"\n" + b"2" * 40,
    b" " + b"1" * 40,
]

#: Names under the rules of `_valid_refname`, stored as given everywhere.
_NAMES_PLAIN = [
    b"refs/heads/main",
    b"refs/heads/a/b",
    b"refs/heads/x-y_z.1",
    b"refs/heads/@",
    b"refs/heads/a@b",
    b"refs/heads/{a}",
    b"refs/tags/v1",
    b"refs/remotes/o/m",
    b"refs/heads/lock",
    b"refs/heads/a.locks",
    b"refs/heads/a.lock.b",
    b"refs/x",
    b"refs/heads/a+b",
    b"refs/heads/a=b",
    b"refs/heads/a,b",
    b"refs/heads/a%b",
    b"refs/heads/a#b",
    b"refs/heads/a!b",
    b"refs/heads/a$b",
    b"refs/heads/a&b",
    b"refs/heads/a'b",
    b"refs/heads/a;b",
    b"refs/heads/a(b)",
    b"refs/notes/commits",
    b"refs/stash",
    b"refs/heads/.invalid",
]
#: Names `_parse` refuses (never looked up, so no file system decides).
_NAMES_BAD = [
    b"refs/heads/a..b",
    b"refs/heads/.a",
    b"refs/heads/a.lock",
    b"refs/heads/a.",
    b"refs/heads/a/",
    b"refs/heads//a",
    b"refs/heads/a@{b",
    b"refs/heads/a b",
    b"refs/heads/a~",
    b"refs/heads/a^",
    b"refs/heads/a:b",
    b"refs/heads/a?",
    b"refs/heads/a*",
    b"refs/heads/a[",
    b"refs/heads/a\\b",
    b"refs/heads/a\x7f",
    b"refs/heads/a\x01",
    b"refs/heads",
    b"refs/",
    b"HEAD",
    b"heads/main",
    b"refs/heads/a.lock/b",
    b"refs/heads/.lock",
    b"refs/heads/@{",
    b"refs/heads/a\x00",
    b"refs//heads",
    b"/refs/heads/a",
    b"refs/heads/a/.b",
    b"refs/heads/..",
    b"ref/heads/main",
    b"Refs/heads/main",
    b"refs",
    b"",
]
#: Names `_valid_refname` admits that not every file system stores as given.
_NAMES_POSIX = [
    b"refs/heads/a<b",
    b"refs/heads/a>b",
    b"refs/heads/a|b",
    b'refs/heads/a"b',
    b"refs/heads/\xc3\xa9",
    b"refs/heads/\xe2\x82\xac",
    b"refs/heads/con",
    b"refs/heads/nul",
    b"refs/heads/aux.txt",
    b"refs/heads/com1",
    b"refs/heads/" + b"n" * 200,
    b"refs/heads/" + b"/".join([b"d" * 50] * 8),
]


def _packed_file(*records: tuple[bytes, bytes], header: bytes | None = None) -> bytes:
    out = b"" if header is None else header
    for sha, name in records:
        out += sha + b" " + name + b"\n"
    return out


def _forms() -> Iterator[Case]:
    """HEAD's forms: symbolic, detached, malformed, with each whitespace."""
    # Each prefix with each gap, and each gap with each line end, under
    # the two prefixes git writes and reads.
    for prefix in _PREFIXES:
        for gap in _GAPS:
            head = prefix + gap + MAIN + b"\n"
            yield Case(_head(head, **{"g/refs/heads/main": H1 + b"\n"}))
    for prefix in (b"ref:", b"ref: "):
        for gap in (b"", b" ", b"\t", b"\n"):
            for suffix in _SUFFIXES:
                head = prefix + gap + MAIN + suffix
                yield Case(_head(head, **{"g/refs/heads/main": H1 + b"\n"}))
    for prefix in (b"ref: ",):
        for suffix in _SUFFIXES:
            yield Case(_head(prefix + MAIN + suffix))
    for sha in _HASHES:
        for suffix in (b"", b"\n", b"\r\n", b" \t\n", b"\n\n"):
            yield Case(_head(sha + suffix))
        for suffix in (b"\n", b" \t\n"):
            # The same text as a loose ref HEAD names.
            yield Case(
                _head(b"ref: refs/heads/main\n", **{"g/refs/heads/main": sha + suffix})
            )
            yield Case(
                _head(b"ref: refs/tags/v1\n", **{"g/refs/tags/v1": sha + suffix})
            )
    for head in (
        b"",
        b"\n",
        b"\x00" * 40,
        b"\x00",
        b"\xff\xfe",
        b"ref: \xff\n",
        b"ref: refs/heads/main\x00\n",
        b"x" * 40,
        b"\xef\xbb\xbfref: refs/heads/main\n",
    ):
        yield Case(_head(head))
    # No HEAD at all; HEAD a directory; HEAD empty with a branch.
    yield Case({})
    yield Case({}, dirs=("g/HEAD",))
    yield Case({"g/refs/heads/main": H1 + b"\n"})


def _names() -> Iterator[Case]:
    for name in _NAMES_PLAIN:
        head = b"ref: " + name + b"\n"
        yield Case(_head(head))
        yield Case(_head(head, **{_g(name): H1 + b"\n"}))
        yield Case(_head(head, **{"g/packed-refs": _packed_file((H2, name))}))
        yield Case(
            _head(
                head,
                **{_g(name): H1 + b"\n", "g/packed-refs": _packed_file((H2, name))},
            )
        )
    for name in _NAMES_BAD:
        yield Case(_head(b"ref: " + name + b"\n"))
        if (
            b"\x00" not in name
            and name
            and not name.startswith(b"/")
            and b"\\" not in name
        ):
            # A loose symbolic ref to the bad name, and the bad name's
            # record in packed-refs.
            yield Case(
                _head(
                    b"ref: refs/heads/main\n",
                    **{"g/refs/heads/main": b"ref: " + name + b"\n"},
                )
            )
            yield Case(
                _head(
                    b"ref: refs/heads/main\n",
                    **{"g/packed-refs": _packed_file((H1, MAIN), (H2, name))},
                ),
                names=(name,),
            )


_HEADERS = [
    None,
    b"# pack-refs with: peeled fully-peeled sorted \n",
    b"# pack-refs with: peeled fully-peeled \n",
    b"# pack-refs with:\n",
    b"# pack-refs with: sorted\n",
    b"# pack-refs with:sorted\n",
    b"# pack-refs with: \tsorted\x0b\n",
    b"# pack-refs with: Sorted\n",
    b"# pack-refs with: unsorted\n",
    b"# pack-refs with: a..b\n",
    b"# pack-refs with: x\x00y\n",
    b"# pack-refs\n",
    b"#\n",
    b"# other\n",
    b"#pack-refs with: sorted\n",
    b"# pack-refs with: .sorted\n",
]

_RECORD_SETS: list[list[bytes]] = [
    [H1 + b" refs/heads/main\n"],
    [H2 + b" refs/heads/a\n", H1 + b" refs/heads/main\n", H3 + b" refs/tags/z\n"],
    [H3 + b" refs/tags/z\n", H1 + b" refs/heads/main\n", H2 + b" refs/heads/a\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main\n", H1 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main\n", b"^" + H2 + b"\n"],
    [H1 + b" refs/heads/main\n", b"^" + H2 + b"\n", b"^" + H3 + b"\n"],
    [b"^" + H2 + b"\n", H1 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main\r\n"],
    [H1 + b" refs/heads/main\r\n", H2 + b" refs/heads/zz\r\n"],
    [H2 + b" refs/heads/a\r\n", H1 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/n"],
    [H64 + b" refs/heads/main\n"],
    [H64 + b" refs/heads/main\n", b"^" + HB64 + b"\n"],
    [H64 + b" refs/heads/main\n", b"^" + H2 + b"\n"],
    [H64 + b" refs/heads/a\n", H1 + b" refs/heads/main\n"],
    [b"A" * 40 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/a..b\n", H2 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/a b\n"],
    [H1 + b"  refs/heads/main\n"],
    [H1 + b"\trefs/heads/main\n"],
    [H1 + b" refs/heads/main \n"],
    [H1 + b" refs/heads/main\n", b"\n"],
    [b"\n", H1 + b" refs/heads/main\n"],
    [H1 + b" refs/heads/main\n", b"x\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/a\x00b\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/a/\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/.x\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/x.lock\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/x.lock/y\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/x@{y\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/x.\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads//x\n"],
    [H1 + b" refs/heads/main\n", H2 + b" refs/heads/\xff\n"],
    [H2 + b" refs/heads/a\n", H3 + b" refs/heads/b\n"],
    [H1 + b" refs/heads/main/x\n"],
    [H1 + b" xrefs/heads/main\n"],
    [H1[:20] + b" refs/heads/main\n"],
    [H1 + b" refs/heads/mai"],
    [H1 + b" refs/heads/ma\n"],
    [
        H1 + b" " + MAIN + b"\n",
        H2 + b" refs/heads/main2\n",
        H3 + b" refs/heads/main.x\n",
    ],
    [H2 + b" refs/heads/main2\n", H1 + b" " + MAIN + b"\n"],
    [H2 + b" refs/heads/mai\n", H1 + b" " + MAIN + b"\n"],
    [H2 + b" refs/heads/Main\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" " + MAIN + b"\n", H2 + b" refs/heads/Main\n"],
    # Each alternative of `_BAD_NAME` alone, in a file the shape admits.
    [H1 + b" .x\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a..b\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a@{b\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads//a\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/.a\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a/\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a.lock/b\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a.lock\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a.\n", H1 + b" " + MAIN + b"\n"],
    [H1 + b" refs/heads/a@b\n", H1 + b" " + MAIN + b"\n"],
]


def _packed_cases() -> Iterator[Case]:
    """``packed-refs``: each header with each record set, loose against
    packed, and the edge files."""
    # Every record set under no header, a sorted one and an unsorted
    # one; every header over the record sets that tell headers apart.
    for header in _HEADERS[:3]:
        for records in _RECORD_SETS:
            data = (header or b"") + b"".join(records)
            yield Case(_head(b"ref: refs/heads/main\n", **{"g/packed-refs": data}))
    for header in _HEADERS:
        for index in (0, 1, 2, 8, 13, 18):
            data = (header or b"") + b"".join(_RECORD_SETS[index])
            yield Case(_head(b"ref: refs/heads/main\n", **{"g/packed-refs": data}))
    for data in (
        b"",
        b"\n",
        b"# pack-refs with: peeled\n",
        b"\x00" * 64,
        b"\xff\n",
        b"# pack-refs with: peeled",
        b" \n",
    ):
        yield Case(_head(b"ref: refs/heads/main\n", **{"g/packed-refs": data}))
    # The header line holding the needle.
    yield Case(
        _head(
            b"ref: refs/heads/main\n",
            **{
                "g/packed-refs": b"# pack-refs with: refs/heads/main\n"
                + H2
                + b" refs/heads/a\n"
            },
        )
    )
    # Loose against packed.
    for loose in (H1 + b"\n", b"ref: refs/heads/other\n", b"garbage\n", b"", b"\x00"):
        for packed in (
            _packed_file((H2, MAIN)),
            _packed_file((H2, MAIN)) + b"x\n",
            b"",
        ):
            yield Case(
                _head(
                    b"ref: refs/heads/main\n",
                    **{
                        "g/refs/heads/main": loose,
                        "g/packed-refs": packed,
                        "g/refs/heads/other": H3 + b"\n",
                    },
                )
            )
    # A directory where the loose ref would be: packed-refs is read.
    for packed in (_packed_file((H2, MAIN)), b"", b"x\n"):
        files = _head(b"ref: refs/heads/main\n")
        if packed:
            files["g/packed-refs"] = packed
        yield Case(files, dirs=("g/refs/heads/main",))
        yield Case(files, dirs=("g/refs/heads/main/x",))
    # packed-refs a directory.
    yield Case(_head(b"ref: refs/heads/main\n"), dirs=("g/packed-refs",))
    yield Case(
        _head(b"ref: refs/heads/main\n", **{"g/refs/heads/main": H1 + b"\n"}),
        dirs=("g/packed-refs",),
    )
    # A tag peeled line, and HEAD at a tag.
    yield Case(
        _head(
            b"ref: refs/tags/v1\n",
            **{
                "g/packed-refs": _packed_file((H1, b"refs/tags/v1"), header=_HEADERS[1])
                + b"^"
                + H2
                + b"\n"
            },
        )
    )


def _chains() -> Iterator[Case]:
    """Symbolic chains at and past git's depth, and cycles."""
    for length in range(0, 7):
        for end in ("loose", "packed", "none", "garbage"):
            files: dict[str, _Content] = {}
            names = [b"refs/heads/s%d" % i for i in range(length)] + [MAIN]
            files["g/HEAD"] = b"ref: " + names[0] + b"\n"
            for here, there in zip(names, names[1:]):
                files[_g(here)] = b"ref: " + there + b"\n"
            if end == "loose":
                files[_g(MAIN)] = H1 + b"\n"
            elif end == "packed":
                files["g/packed-refs"] = _packed_file((H1, MAIN))
            elif end == "garbage":
                files[_g(MAIN)] = b"garbage\n"
            yield Case(files)
    yield Case(
        _head(b"ref: refs/heads/a\n", **{"g/refs/heads/a": b"ref: refs/heads/a\n"})
    )
    yield Case(
        _head(
            b"ref: refs/heads/a\n",
            **{
                "g/refs/heads/a": b"ref: refs/heads/b\n",
                "g/refs/heads/b": b"ref: refs/heads/a\n",
            },
        )
    )
    yield Case(
        _head(
            b"ref: refs/heads/a\n",
            **{
                "g/refs/heads/a": b"ref: refs/heads/b\n",
                "g/refs/heads/b": b"ref: refs/heads/c\n",
                "g/refs/heads/c": b"ref: refs/heads/a\n",
            },
        )
    )
    # A chain leaving refs/heads.
    yield Case(
        _head(
            b"ref: refs/heads/a\n",
            **{"g/refs/heads/a": b"ref: refs/tags/t\n", "g/refs/tags/t": H1 + b"\n"},
        )
    )
    yield Case(
        _head(
            b"ref: refs/tags/t\n",
            **{"g/refs/tags/t": b"ref: refs/heads/a\n", "g/refs/heads/a": H1 + b"\n"},
        )
    )
    yield Case(_head(b"ref: refs/heads/a\n", **{"g/refs/heads/a": b"ref: HEAD\n"}))
    yield Case(
        _head(b"ref: refs/heads/a\n", **{"g/refs/heads/a": b"ref: refs/heads/a..b\n"})
    )


def _worktrees() -> Iterator[Case]:
    """A linked worktree: HEAD and per-worktree refs under its git
    directory, the rest under the common one."""
    wt = "g/worktrees/w/"
    yield Case(
        {wt + "HEAD": b"ref: refs/heads/main\n", "g/refs/heads/main": H1 + b"\n"},
        worktree=True,
    )
    yield Case(
        {wt + "HEAD": b"ref: refs/heads/main\n", wt + "refs/heads/main": H1 + b"\n"},
        worktree=True,
    )
    yield Case(
        {
            wt + "HEAD": b"ref: refs/heads/main\n",
            "g/packed-refs": _packed_file((H2, MAIN)),
        },
        worktree=True,
    )
    yield Case({wt + "HEAD": H1 + b"\n"}, worktree=True)
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n", "g/refs/heads/main": H1 + b"\n"},
        worktree=True,
    )
    yield Case(
        {
            wt + "packed-refs": _packed_file((H2, MAIN)),
            wt + "HEAD": b"ref: refs/heads/main\n",
        },
        worktree=True,
    )
    for prefix in (b"refs/worktree/", b"refs/bisect/", b"refs/rewritten/"):
        name = prefix + b"x"
        head = b"ref: " + name + b"\n"
        yield Case({wt + "HEAD": head, wt + name.decode(): H1 + b"\n"}, worktree=True)
        yield Case({wt + "HEAD": head, "g/" + name.decode(): H1 + b"\n"}, worktree=True)
        yield Case(
            {wt + "HEAD": head, "g/packed-refs": _packed_file((H1, name))},
            worktree=True,
        )
        yield Case({wt + "HEAD": head, wt + name.decode(): b"garbage\n"}, worktree=True)
        yield Case(
            {
                wt + "HEAD": head,
                wt + name.decode(): b"ref: refs/heads/main\n",
                "g/refs/heads/main": H2 + b"\n",
            },
            worktree=True,
        )
        yield Case({wt + "HEAD": head}, dirs=(wt + name.decode(),), worktree=True)
        yield Case(
            {wt + "HEAD": head, "g/packed-refs": _packed_file((H1, name))},
            dirs=(wt + name.decode(),),
            worktree=True,
        )
        # Per-worktree in a primary directory.
        yield Case({"g/HEAD": head, "g/" + name.decode(): H1 + b"\n"})
        yield Case({"g/HEAD": head, "g/packed-refs": _packed_file((H1, name))})
    # Prefixes that are not per-worktree.
    for name in (b"refs/worktreex", b"refs/worktrees/x", b"refs/bisectx/y"):
        yield Case(
            {wt + "HEAD": b"ref: " + name + b"\n", "g/" + name.decode(): H1 + b"\n"},
            worktree=True,
        )


def _storage() -> Iterator[Case]:
    """Ref storages other than files, met through their files."""
    reftable_config = b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\trefStorage = reftable\n"
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/.invalid\n",
            "g/config": reftable_config,
            "g/refs/heads": b"this repository uses the reftable format\n",
            "g/reftable/tables.list": b"0x000000000001-0x000000000002-01234567.ref\n",
        }
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/config": reftable_config,
            "g/reftable/tables.list": b"",
        }
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/config": reftable_config,
            "g/refs/heads/main": H1 + b"\n",
        }
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/config": b"[extensions]\n\trefStorage = files\n\tobjectFormat = sha256\n",
            "g/refs/heads/main": H64 + b"\n",
        }
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/config": b"[extensions]\n\tobjectFormat = sha1\n",
            "g/refs/heads/main": H64 + b"\n",
        }
    )


def _sizes() -> Iterator[Case]:
    """Each file at its bound and one past."""
    limit = _REF_LIMIT
    line = b"ref: refs/heads/main"
    for size in (limit - 1, limit, limit + 1, limit + 2):
        head: list[tuple[bytes, int]] = [(line, 1), (b" ", size - len(line))]
        yield Case({"g/HEAD": head, "g/refs/heads/main": H1 + b"\n"})
        ref: list[tuple[bytes, int]] = [(H1, 1), (b"\n", size - 40)]
        yield Case({"g/HEAD": line + b"\n", "g/refs/heads/main": ref})
        name = b"refs/heads/" + b"n" * 30
        yield Case(
            {
                "g/HEAD": [(b"ref: " + name, 1), (b"\n", size - 5 - len(name))],
                "g/" + name.decode(): H1 + b"\n",
            }
        )
    packed_limit = githead._PACKED_LIMIT
    main = H1 + b" " + MAIN + b"\n"
    for size in (packed_limit, packed_limit + 1):
        # A record whose long name pads the file to `size`.
        pad = size - len(main) - 42 - 1
        yield Case(
            {
                "g/HEAD": line + b"\n",
                "g/packed-refs": [
                    (H2 + b" refs/heads/a", 1),
                    (b"x", pad - 11),
                    (b"\n", 1),
                    (main, 1),
                ],
            }
        )
    # Many records, sorted and with the needle in the middle.
    header = b"# pack-refs with: peeled fully-peeled sorted \n"
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/m\n",
            "g/packed-refs": [
                (header, 1),
                (H2 + b" refs/heads/a\n", 1),
                (H3 + b" refs/heads/k\n", 1),
                (H1 + b" refs/heads/m\n", 1),
                (H2 + b" refs/heads/z\n", 1),
            ],
        }
    )


def _plain() -> Iterator[Case]:
    yield from _forms()
    yield from _names()
    yield from _packed_cases()
    yield from _chains()
    yield from _worktrees()
    yield from _storage()
    yield from _sizes()


def _posix() -> Iterator[Case]:
    # Names that are not ASCII, looked up but not on disk: Windows decodes
    # a bytes path as UTF-8 and raises ValueError on one that is not.
    for head in (b"ref: refs/heads/\xff\n", b"ref: refs/heads/\xc3\xa9\n"):
        yield Case(_head(head))
        yield Case(_head(head, **{"g/packed-refs": _packed_file((H1, head[5:-1]))}))
    for name in _NAMES_POSIX:
        head = b"ref: " + name + b"\n"
        yield Case(_head(head))
        yield Case(_head(head, **{_g(name): H1 + b"\n"}))
        yield Case(_head(head, **{"g/packed-refs": _packed_file((H2, name))}))
    main = {"g/refs/heads/main": H1 + b"\n"}
    # Symbolic links.
    yield Case(main, links={"g/HEAD": "refs/heads/main"})
    yield Case(main, links={"g/HEAD": "../g/refs/heads/main"})
    yield Case({}, links={"g/HEAD": "missing"})
    yield Case({}, links={"g/HEAD": "HEAD"})
    yield Case({"g/real": b"ref: refs/heads/main\n", **main}, links={"g/HEAD": "real"})
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n", "g/refs/heads/real": H1 + b"\n"},
        links={"g/refs/heads/main": "real"},
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/packed-refs": _packed_file((H2, MAIN)),
        },
        links={"g/refs/heads/main": "missing"},
    )
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n", "g/other": _packed_file((H2, MAIN))},
        links={"g/packed-refs": "other"},
    )
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n"}, links={"g/packed-refs": "missing"}
    )
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n"}, links={"g/packed-refs": "packed-refs"}
    )
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n", "g/elsewhere/main": H1 + b"\n"},
        links={"g/refs/heads": "../elsewhere"},
    )
    yield Case(
        {
            "g/HEAD": b"ref: refs/heads/main\n",
            "g/packed-refs": _packed_file((H2, MAIN)),
        },
        dirs=("g/refs/heads/d",),
        links={"g/refs/heads/main": "d"},
    )
    yield Case(
        {
            "g/worktrees/w/HEAD": b"ref: refs/worktree/x\n",
            "g/worktrees/w/real": H1 + b"\n",
        },
        links={"g/worktrees/w/refs/worktree/x": "../../real"},
        worktree=True,
    )
    # FIFOs.
    yield Case(main, fifos=("g/HEAD",))
    yield Case({"g/HEAD": b"ref: refs/heads/main\n"}, fifos=("g/refs/heads/main",))
    yield Case({"g/HEAD": b"ref: refs/heads/main\n"}, fifos=("g/packed-refs",))
    yield Case({"g/HEAD": b"ref: refs/heads/main\n", **main}, fifos=("g/packed-refs",))
    # A file where a directory of the path should be.
    for packed in (b"", _packed_file((H2, MAIN))):
        files = {"g/HEAD": b"ref: refs/heads/main\n", "g/refs/heads": b"x\n"}
        if packed:
            files["g/packed-refs"] = packed
        yield Case(files)
        files = {"g/HEAD": b"ref: refs/heads/main\n", "g/refs": b"x\n"}
        if packed:
            files["g/packed-refs"] = packed
        yield Case(files)
    yield Case(
        {"g/HEAD": b"ref: refs/heads/main\n", "g/refs/heads/main": H1 + b"\n"},
        links={"g/packed-refs": "refs/heads/main/x"},
    )
    yield Case(
        {"g/worktrees/w/HEAD": b"ref: refs/worktree/x\n", "g/worktrees/w/refs": b"x\n"},
        worktree=True,
    )
    yield Case({}, links={"g/HEAD": "refs/heads/main/x"})


def _unique(cases: Iterator[Case]) -> list[Case]:
    seen: set[str] = set()
    out = []
    for case in cases:
        key = case.key()
        if key not in seen:
            seen.add(key)
            out.append(case)
    return out


def _file(name: str, cases: list[Case]) -> bytes:
    lines: list[dict[str, Any]] = []
    declined = 0
    with _sandbox.world() as world:
        base = world.root / "githead-cases"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            answer = _answer(case, root)
            shutil.rmtree(root)
            if answer["v"] in ("x", "o"):
                declined += 1
            lines.append({**case.record(), "r": answer})
    header = {"fixture": name, "cases": len(cases), "declined": declined}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"githead.jsonl": _file("githead", _unique(_plain()))}


def generate_posix() -> dict[str, bytes]:
    return {"githead_posix.jsonl": _file("githead_posix", _unique(_posix()))}
