"""Fixtures: the loaded state of `gitobjects.ObjectReader`
(``gitobjects_files.jsonl``, ``gitobjects_files_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitobjects/files.rs``. This pins it to
`ObjectReader`'s ``object_dirs``, ``complete`` and ``evicted``, and through
them ``_load``, ``_object_format``, ``_object_dirs``, ``_link_alternates``,
``_list_new``, ``_rescan``, ``_open``, ``_read_whole``, ``_is_evicted`` and
``_dir_key``.

Each case builds a scratch tree under a root ``@`` (its real path, so no
link stands above it) and makes ``ObjectReader(@/<gd>)``:

- ``t``: the tree, each entry ``["d", rel]`` (a folder), ``["f", rel,
  bytes]`` (a file, bytes as `gen_gitobjects._sp` spells them), ``["l",
  rel, target]`` (a symbolic link, ``@`` in the target standing for the
  root), ``["p", rel]`` (a FIFO) or ``["m", rel, mode]`` (the owner's
  permission bits of what is there);
- ``st``: what ``os.stat`` claims of a path besides its own answer:
  ``["f", rel]`` ``st_flags`` SF_DATALESS, ``["a", rel]``
  ``st_file_attributes`` RECALL_ON_DATA_ACCESS, ``["o", rel]``
  ``st_flags`` of every other bit, ``["r", rel]`` a regular file's mode;
- ``gv``: the release `gitfile_rule._git_is_at_least` reads (null: none
  named), ``rc`` what `gitfile_rule._reads_compat` answers;
- ``gd``: the git directory below the root (default ``g``);
- ``q``: the questions in order: ``"d"`` (``object_dirs``), ``"c"``
  (``complete``), ``"e"`` (``evicted``), ``"r"`` (``_rescan``, the count
  of new indexes), or ``["w", rel, bytes]`` (a file written first).

Each answers ``a``, one per question: the answer (paths relative to the
root, ``/`` between components), null for a write, or ``{"raise":
class}``. ``s`` is the state afterwards: ``h`` the hash length, ``l`` the
index paths listed (sorted), ``p`` each loaded index's pack, ``u``
unreadable, ``e`` evicted.

``gitobjects_files.jsonl`` holds the cases that make no link, FIFO,
alternates or mode and resolve no path whose spelling is recorded: the
same bytes on every system. ``gitobjects_files_posix.jsonl`` holds the
rest, POSIX only: links, FIFOs, modes and the alternates walk, whose
directories are spelled by ``posixpath.realpath`` (on Windows
``ntpath.realpath`` asks the system and may answer another spelling).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from bettermemory import gitfile_rule, gitobjects

from ._common import jsonl
from .gen_gitobjects import _sp
from .gen_store_files import host_fact

_SF_DATALESS = 0x40000000
_RECALL = 0x00400000


def _idx(names: list[bytes], hash_len: int) -> bytes:
    """A version 2 pack index of `names` (sorted, each `hash_len` bytes),
    offsets 12 on, checksums zero."""
    names = sorted(names)
    fanout = [sum(1 for n in names if n[0] <= b) for b in range(256)]
    out = bytearray(b"\xfftOc\x00\x00\x00\x02")
    for count in fanout:
        out += count.to_bytes(4, "big")
    for name in names:
        out += name
    out += b"\x00" * (4 * len(names))
    for i in range(len(names)):
        out += (12 + i).to_bytes(4, "big")
    out += b"\x00" * (2 * hash_len)
    return bytes(out)


IDX1 = _idx([b"\x01" * 20], 20)
IDX256 = _idx([b"\x02" * 32], 32)
IDX0 = _idx([], 20)
CUT = IDX1[:-1]


def _sha256_config() -> bytes:
    return b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"


def _f(rel: str, data: bytes) -> list[Any]:
    return ["f", rel, _sp(data)]


def _spelled(v: Any) -> bytes:
    if isinstance(v, str):
        return bytes.fromhex(v)
    out = bytearray()
    for piece in v:
        if isinstance(piece, str):
            out += bytes.fromhex(piece)
        else:
            out += bytes.fromhex(piece[0]) * piece[1]
    return bytes(out)


def _case(name: str, t: list[Any], q: list[Any], **more: Any) -> dict[str, Any]:
    return {"name": name, "t": t, "q": q, **more}


BASE = [["d", "g/objects/pack"]]


def _plain_cases() -> list[dict[str, Any]]:
    cs = [
        _case("no config", BASE, ["d", "c"]),
        _case("no objects", [["d", "g"]], ["d", "c"]),
        _case("no git directory", [], ["c", "d"]),
        _case("objects is a file", [_f("g/objects", b"x")], ["d", "c"]),
        _case("objects without pack", [["d", "g/objects"]], ["c"]),
        _case("pack is a file", [_f("g/objects/pack", b"")], ["c", "d"]),
        _case("evicted alone loads nothing", BASE, ["e", "c", "e"]),
    ]
    configs: list[tuple[str, bytes]] = [
        (
            "sha1 v1",
            b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha1\n",
        ),
        ("sha256 v1", _sha256_config()),
        (
            "sha256 v0",
            b"[core]\n\trepositoryformatversion = 0\n[extensions]\n\tobjectformat = sha256\n",
        ),
        ("sha256 no version", b"[extensions]\n\tobjectformat = sha256\n"),
        ("version 2", b"[core]\n\trepositoryformatversion = 2\n"),
        (
            "unknown format",
            b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = md5\n",
        ),
        ("unparsable", b"[core\n"),
        ("empty", b""),
        (
            "unknown extension v1",
            b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tnosuch = 1\n",
        ),
    ]
    for label, text in configs:
        cs.append(_case(f"config {label}", [*BASE, _f("g/config", text)], ["c", "d"]))
    cs.append(_case("config a directory", [*BASE, ["d", "g/config"]], ["c"]))
    compat = (
        b"[core]\n\trepositoryformatversion = 1\n[extensions]\n"
        b"\tobjectformat = sha1\n\tcompatObjectFormat = sha256\n"
    )
    for gv in (None, [2, 0], [2, 45], [2, 54], [3, 0]):
        for rc in (None, False, True):
            cs.append(
                _case(
                    f"compat under {gv} reads {rc}",
                    [*BASE, _f("g/config", compat)],
                    ["c"],
                    gv=gv,
                    rc=rc,
                )
            )
    cs += [
        _case(
            "config evicted by flags",
            [*BASE, _f("g/config", b"")],
            ["c", "e"],
            st=[["f", "g/config"]],
        ),
        _case(
            "config evicted by attributes",
            [*BASE, _f("g/config", b"")],
            ["e", "c", "e"],
            st=[["a", "g/config"]],
        ),
        _case(
            "config other flags",
            [*BASE, _f("g/config", _sha256_config())],
            ["c"],
            st=[["o", "g/config"]],
        ),
        _case(
            "a sha1 index beside its pack",
            [*BASE, _f("g/objects/pack/p.idx", IDX1), _f("g/objects/pack/p.pack", b"")],
            ["c"],
        ),
        _case(
            "a sha1 index in a sha256 repository",
            [
                *BASE,
                _f("g/config", _sha256_config()),
                _f("g/objects/pack/p.idx", IDX1),
                _f("g/objects/pack/p.pack", b""),
            ],
            ["c"],
        ),
        _case(
            "a sha256 index",
            [
                *BASE,
                _f("g/config", _sha256_config()),
                _f("g/objects/pack/p.idx", IDX256),
                _f("g/objects/pack/p.pack", b""),
            ],
            ["c"],
        ),
        _case(
            "an index without its pack",
            [*BASE, _f("g/objects/pack/p.idx", IDX1)],
            ["c"],
        ),
        _case(
            "a truncated index",
            [*BASE, _f("g/objects/pack/p.idx", CUT), _f("g/objects/pack/p.pack", b"")],
            ["c"],
        ),
        _case(
            "a pack that is a folder",
            [*BASE, _f("g/objects/pack/p.idx", IDX1), ["d", "g/objects/pack/p.pack"]],
            ["c"],
        ),
        _case(
            "an index that is a folder",
            [*BASE, ["d", "g/objects/pack/p.idx"], _f("g/objects/pack/p.pack", b"")],
            ["c"],
        ),
        _case(
            "indexes sorted by bytes",
            [
                *BASE,
                *(
                    entry
                    for n in ("b", "Z", "a0", "_", "~")
                    for entry in (
                        _f(f"g/objects/pack/{n}x.idx", IDX0),
                        _f(f"g/objects/pack/{n}x.pack", b""),
                    )
                ),
                _f("g/objects/pack/q.idx.bak", IDX0),
                _f("g/objects/pack/q.IDX", IDX0),
                _f("g/objects/pack/.idx", IDX0),
                _f("g/objects/pack/.pack", b""),
            ],
            ["c", "d"],
        ),
        _case(
            "an evicted index",
            [*BASE, _f("g/objects/pack/p.idx", IDX1), _f("g/objects/pack/p.pack", b"")],
            ["c", "e"],
            st=[["f", "g/objects/pack/p.idx"]],
        ),
        _case(
            "an evicted pack",
            [*BASE, _f("g/objects/pack/p.idx", IDX1), _f("g/objects/pack/p.pack", b"")],
            ["c", "e"],
            st=[["a", "g/objects/pack/p.pack"]],
        ),
        _case(
            "the format unread lists no index",
            [
                *BASE,
                _f("g/config", b"[core]\n\trepositoryformatversion = 2\n"),
                _f("g/objects/pack/p.idx", CUT),
                _f("g/objects/pack/p.pack", b""),
            ],
            ["c"],
        ),
        _case(
            "rescan after a pack appears",
            [*BASE, _f("g/objects/pack/p.idx", IDX1)],
            [
                "c",
                ["w", "g/objects/pack/p.pack", _sp(b"")],
                "r",
                ["w", "g/objects/pack/q.idx", _sp(IDX1)],
                ["w", "g/objects/pack/q.pack", _sp(b"")],
                "r",
                "r",
                "c",
            ],
        ),
        _case(
            "rescan meets a truncated index",
            [*BASE],
            [
                "c",
                ["w", "g/objects/pack/z.idx", _sp(CUT)],
                ["w", "g/objects/pack/z.pack", _sp(b"")],
                "r",
                "c",
            ],
        ),
        _case("rescan with no format", [*BASE, _f("g/config", b"[core\n")], ["r", "c"]),
    ]
    for order in (["c", "d"], ["d", "c"], ["e", "c"]):
        cs.append(
            _case(
                f"a NUL in the git directory, {''.join(order)}",
                BASE,
                order,
                gd="g\x00x",
            )
        )
    return cs


def _posix_cases() -> list[dict[str, Any]]:
    o = "g/objects"
    alt = f"{o}/info/alternates"
    cs = [
        _case(
            "a relative alternate",
            [*BASE, ["d", "x/objects"], _f(alt, b"../../x/objects\n")],
            ["d", "c"],
        ),
        _case(
            "an absolute alternate",
            [*BASE, ["d", "x/objects"], _f(alt, b"@/x/objects\n")],
            ["d"],
        ),
        _case(
            "a C-quoted alternate",
            [
                *BASE,
                ["d", "x/obj ects"],
                _f(alt, b'"@/x/obj\\040ects"\n"../../x/obj\\145cts"\n'),
            ],
            ["d"],
        ),
        _case(
            "comments, blanks, missing and file entries",
            [
                *BASE,
                ["d", "x/objects"],
                _f("x/file", b""),
                _f(alt, b"# c\n\n@/nope\n@/x/file\n@/x/objects/\n@/x/objects\n"),
            ],
            ["d"],
        ),
        _case(
            "an alternate naming its own objects",
            [*BASE, _f(alt, b".\n@/g/objects\n@/g/./objects\n")],
            ["d"],
        ),
        _case(
            "a cycle",
            [
                *BASE,
                ["d", "x/objects/info"],
                _f(alt, b"@/x/objects\n"),
                _f("x/objects/info/alternates", b"@/g/objects\n@/x/objects\n"),
            ],
            ["d", "c"],
        ),
        _case(
            "a link to an alternate already listed",
            [
                *BASE,
                ["d", "x/objects"],
                ["l", "y", "@/x"],
                _f(alt, b"@/x/objects\n@/y/objects\n"),
            ],
            ["d"],
        ),
        _case(
            "a link listed before its target",
            [
                *BASE,
                ["d", "x/objects"],
                ["l", "y", "x"],
                _f(alt, b"@/y/objects\n@/x/objects\n"),
            ],
            ["d"],
        ),
        _case(
            "objects a link, relative entries against its real path",
            [
                ["d", "g"],
                ["d", "real/o/pack"],
                ["d", "real/x"],
                ["d", "g/x"],
                ["l", o, "@/real/o"],
                _f("real/o/info/alternates", b"../x\n"),
            ],
            ["d"],
        ),
        _case(
            "an alternate with packs",
            [
                *BASE,
                ["d", "x/objects/pack"],
                _f(alt, b"@/x/objects\n"),
                _f("x/objects/pack/a.idx", IDX1),
                _f("x/objects/pack/a.pack", b""),
                _f(f"{o}/pack/a.idx", CUT),
                _f(f"{o}/pack/a.pack", b""),
            ],
            ["c", "d"],
        ),
        _case("alternates a folder", [*BASE, ["d", alt]], ["d"]),
        _case("alternates a FIFO", [*BASE, ["p", alt]], ["d", "c"]),
        _case("config a FIFO", [*BASE, ["p", "g/config"]], ["c"]),
        _case(
            "config a FIFO stated regular",
            [*BASE, ["p", "g/config"]],
            ["c", "e"],
            st=[["r", "g/config"]],
        ),
        _case(
            "an index a FIFO",
            [*BASE, ["p", f"{o}/pack/p.idx"], _f(f"{o}/pack/p.pack", b"")],
            ["c"],
        ),
        _case(
            "a pack a FIFO",
            [*BASE, _f(f"{o}/pack/p.idx", IDX1), ["p", f"{o}/pack/p.pack"]],
            ["c"],
        ),
        _case(
            "an index stated regular, a FIFO",
            [*BASE, ["p", f"{o}/pack/p.idx"], _f(f"{o}/pack/p.pack", b"")],
            ["c"],
            st=[["r", f"{o}/pack/p.idx"]],
        ),
        _case(
            "an unreadable index",
            [
                *BASE,
                _f(f"{o}/pack/p.idx", IDX1),
                _f(f"{o}/pack/p.pack", b""),
                ["m", f"{o}/pack/p.idx", 0],
            ],
            ["c"],
        ),
        _case(
            "an unreadable config",
            [*BASE, _f("g/config", _sha256_config()), ["m", "g/config", 0]],
            ["c"],
        ),
        _case(
            "an unlistable pack folder",
            [
                *BASE,
                _f(f"{o}/pack/p.idx", CUT),
                _f(f"{o}/pack/p.pack", b""),
                ["m", f"{o}/pack", 0o300],
            ],
            ["c"],
        ),
        _case(
            "an evicted alternates file",
            [*BASE, ["d", "x/objects"], _f(alt, b"@/x/objects\n")],
            ["d", "c", "e"],
            st=[["f", alt]],
        ),
    ]
    # A chain x1 -> x2 -> ... ; files at depth 0 to 5 are read.
    for length in (5, 6, 7):
        tree: list[Any] = [*BASE, _f(alt, b"@/x1\n")]
        for i in range(1, length + 1):
            tree.append(["d", f"x{i}/info"])
            if i < length:
                tree.append(_f(f"x{i}/info/alternates", f"@/x{i + 1}\n".encode()))
        cs.append(_case(f"a chain of {length}", tree, ["d"]))
    for label, entry in (
        ("raw", b"x\x00y"),
        ("quoted", b'"x\\000y"'),
        ("absolute", b"@/x\x00"),
    ):
        for order in (["d", "c"], ["c", "d"], ["d", "d"], ["c", "e", "c"]):
            cs.append(
                _case(
                    f"a NUL {label}, {''.join(order)}",
                    [
                        *BASE,
                        ["d", "a/objects"],
                        _f(alt, b"@/a/objects\n" + entry + b"\n"),
                    ],
                    order,
                )
            )
    cs.append(
        _case(
            "a NUL in a nested alternates file",
            [
                *BASE,
                ["d", "a/objects/info"],
                _f(alt, b"@/a/objects\n"),
                _f("a/objects/info/alternates", b"z\x00\n"),
            ],
            ["d", "c"],
        )
    )
    return cs


@contextmanager
def _claims(root: bytes, st: list[list[str]]) -> Iterator[None]:
    real = os.stat
    claimed = {
        os.path.join(root, *(p.encode() for p in rel.split("/"))): kind
        for kind, rel in st
    }

    def fake(path: Any, *args: Any, **kwargs: Any) -> Any:
        answer = real(path, *args, **kwargs)
        kind = (
            claimed.get(os.fsencode(path)) if isinstance(path, (str, bytes)) else None
        )
        if kind is None:
            return answer
        mode = answer.st_mode
        flags = getattr(answer, "st_flags", 0)
        attributes = getattr(answer, "st_file_attributes", 0)
        if kind == "f":
            flags |= _SF_DATALESS
        elif kind == "a":
            attributes |= _RECALL
        elif kind == "o":
            flags |= 0xFFFFFFFF ^ _SF_DATALESS
            attributes |= 0xFFFFFFFF ^ _RECALL
        else:
            mode = stat.S_IFREG | 0o644
        return types.SimpleNamespace(
            st_mode=mode,
            st_flags=flags,
            st_file_attributes=attributes,
            st_size=answer.st_size,
        )

    with mock.patch.object(os, "stat", fake):
        yield


def _abs(root: bytes, rel: str) -> bytes:
    return os.path.join(root, *(p.encode("latin-1") for p in rel.split("/")))


def _write(root: bytes, rel: str, data: bytes) -> None:
    path = _abs(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _build(root: bytes, tree: list[Any]) -> None:
    modes = []
    for entry in tree:
        kind, rel = entry[0], entry[1]
        path = _abs(root, rel)
        if kind == "d":
            os.makedirs(path, exist_ok=True)
        elif kind == "f":
            _write(root, rel, _spelled(entry[2]).replace(b"@", root))
        elif kind == "l":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(entry[2].encode().replace(b"@", root), path)
        elif kind == "p":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
        else:
            modes.append((path, entry[2]))
    for path, mode in modes:
        os.chmod(path, mode)


def _rel(root: bytes, path: bytes) -> str:
    prefix = root + os.sep.encode()
    if not path.startswith(prefix):
        raise AssertionError(f"a path outside the scratch root: {path!r}")
    return path[len(prefix) :].replace(os.sep.encode(), b"/").decode("latin-1")


def _run(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = os.fsencode(os.path.realpath(tmp))
        _build(root, case["t"])
        try:
            return _ask(root, case)
        finally:
            for entry in case["t"]:
                if entry[0] == "m":
                    os.chmod(_abs(root, entry[1]), 0o755)


def _ask(root: bytes, case: dict[str, Any]) -> dict[str, Any]:
    gv, rc = case.get("gv", [2, 54]), case.get("rc", True)
    at_least = (
        (lambda release: None) if gv is None else (lambda release: tuple(gv) >= release)
    )
    git_dir = _abs(root, case.get("gd", "g"))
    answers: list[Any] = []
    with (
        mock.patch.object(gitfile_rule, "_git_is_at_least", at_least),
        mock.patch.object(gitfile_rule, "_reads_compat", lambda: rc),
        _claims(root, case.get("st", [])),
    ):
        reader = gitobjects.ObjectReader(git_dir)
        for q in case["q"]:
            try:
                if q == "d":
                    answers.append([_rel(root, d) for d in reader.object_dirs])
                elif q == "c":
                    answers.append(reader.complete)
                elif q == "e":
                    answers.append([_rel(root, p) for p in reader.evicted])
                elif q == "r":
                    answers.append(len(reader._rescan()))
                else:
                    _write(root, q[1], _spelled(q[2]))
                    answers.append(None)
            except ValueError as exc:
                answers.append({"raise": type(exc).__name__})
        state = {
            "h": reader._hash_len,
            "l": sorted(_rel(root, p) for p in reader._listed),
            "p": [_rel(root, p.pack) for p in reader._packs],
            "u": reader._unreadable,
            "e": [_rel(root, p) for p in reader.evicted],
        }
    return {**case, "a": answers, "s": state}


def _file(name: str, cases: list[dict[str, Any]]) -> dict[str, bytes]:
    rows = [_run(c) for c in cases]
    header = {"fixture": name, "cases": len(rows)}
    data = jsonl([header, *rows])
    if leak := host_fact(data):
        raise AssertionError(f"{name} holds a host fact: {leak}")
    return {f"{name}.jsonl": data}


def generate() -> dict[str, bytes]:
    return _file("gitobjects_files", _plain_cases())


def generate_posix() -> dict[str, bytes]:
    if sys.platform == "win32":
        return {}
    return _file("gitobjects_files_posix", _posix_cases())
