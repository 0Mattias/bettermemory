"""Fixtures: the probe repository and the ask folder
(``gitsystem_probe_names.jsonl``, ``gitsystem_probe_names_unicode.jsonl``,
``gitsystem_probe.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitsystem/probe.rs``. This pins it to
`gitsystem`'s `_probe_name`, `PROBE_FOLDER`, `_where`, `_probe_repository`,
`_sweep_probes`, `_probe_stamps`, `_probe_holds` and `_clear_probe`.

The name files hold ``{"n": [[name, bytes], ...], "d": [name, ...]}``
answered in ``"r"``: `_probe_name`'s answer, or ``"UnicodeEncodeError"``.
Names are `_t`, bytes `_b`. They are pure and run on every system; a case
naming a code point past ASCII goes in ``_unicode``, since what `repr`
keeps unescaped is the Unicode data's ``isprintable``.

``gitsystem_probe.jsonl`` (POSIX only) builds a tree in a scratch folder
``R`` holding the state directory ``state`` and a folder outside it,
``out``, then runs one function with the state directory ``R/state``:

- ``t``: the tree, each entry made in order: ``["d", rel]`` a folder,
  ``["f", rel, bytes]`` a file (bytes `_b`, or ``{"p": name}`` the probe
  file of that name), ``["l", rel, target]`` a symbolic link (a target
  starting ``@/`` is under ``R``), ``["p", rel]`` a FIFO. Then each
  ``["t", rel, seconds, nanoseconds]`` sets a modification time (a link's
  own), then each ``["m", rel, mode]`` a mode, last first. ``P`` in a
  path is the probe, ``state/`` and `PROBE_FOLDER`. Every folder and file
  made is 0755 or 0644 until a mode is set.
- ``o``: ``["sweep", now]``, ``["clear"]``, ``["holds"]``,
  ``["stamps"]``, ``["repo", now]`` or ``["where"]``; ``now`` is a float
  spelled by `repr`.
- ``r``: what it answered (an exception's class name where it raised):
  for ``repo`` null or ``[real, same]``, ``same`` whether the stamps it
  took equal the stamps taken again after; for ``where`` null or ``[real,
  ceiling]``; for ``stamps`` each stamp's ``[size, mode]`` or null.
  Paths under ``R`` are ``@`` and the rest.
- ``a``: the tree after, walked without following a link: ``[rel, kind,
  mode & 0o077, what]``, kind ``d`` ``f`` ``l`` ``p``, ``what`` a file's
  `_b` (``#`` and the first 16 hex digits of its SHA-256 past 48 bytes)
  or a link's target. The owner's mode bits are left out: where
  `ensure_owner_only_dir` makes a folder they are the umask's.

``gitsystem_probe_python.jsonl`` (POSIX only, Python-gated) holds the
cases, placed by input, where `Path.is_symlink` or `Path.is_dir` looks
through a folder that cannot be searched: 3.13 raises the error there,
3.14 answers False.

A probe file that is a FIFO is no case here: the reference's
``read_bytes`` waits on it for a writer (the native port reads it without
waiting; its own test). No case depends on the order a folder is listed
in: where a removal can fail partway, each folder it walks holds one
entry (`_refuse_partial_order` refuses any other tree).
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _daemon_client, gitsystem

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b
from .gen_gitsystem_record import _t

P = gitsystem.PROBE_FOLDER
#: The whole-second time each dated entry is set from.
T = 1_000_000_000
HOUR = gitsystem.REASK_SECONDS
#: The newest `now` at which an entry dated `T` is kept, and the oldest at
#: which it is removed.
KEPT_AT = math.nextafter(T + HOUR, 0)
GONE_AT = float(T + HOUR)
OTHER = "git-probe-0123456789abcdef"


# -- names -------------------------------------------------------------------


def _name_cases() -> Iterator[tuple[list[tuple[str, bytes]], list[str]]]:
    files = list(gitsystem._PROBE_FILES.items())
    folders = list(gitsystem._PROBE_FOLDERS)
    yield files, folders
    yield list(reversed(files)), folders
    yield files, list(reversed(folders))
    yield [], []
    yield [], ["a"]
    yield [("a", b"")], []
    yield [("a", b"x")], ["a", "b"]
    yield [("b", b"1"), ("a", b"2"), ("B", b"3"), ("", b"4")], ["z"]
    for data in (
        b"'",
        b'"',
        b"'\"",
        b"\\",
        b"\\'",
        b"\t\n\r",
        b"\x00\x01\x1f\x7f\x80\xff",
        b"a'b",
        b"a\"b'c",
        bytes(range(256)),
    ):
        yield [("f", data)], []
    for text in (
        "'",
        '"',
        "'\"",
        "\\",
        "\t\n\r\x00\x1f\x7f",
        "a'b",
        "x" * 300,
    ):
        yield [(text, b"")], [text]
        yield [], [text, "y"]
    # Past ASCII: kept (printable) or escaped.
    for text in (
        "é",
        " ",
        "­",
        "͸",
        " ",
        "　",
        "\ud800",
        "\udfff",
        "\U0001f600",
        "\U000e0001",
        "\U0010ffff",
        "\x80\x9f",
        "aé'",
    ):
        yield [(text, b"x")], []
        yield [], [text]
    yield [("￿", b"1"), ("\U00010000", b"2"), ("é", b"3")], []
    yield [("\x85", b"1"), ("\x84", b"2")], ["\x85"]


def _name_answer(files: list[tuple[str, bytes]], folders: list[str]) -> str:
    try:
        return gitsystem._probe_name(dict(files), tuple(folders))
    except UnicodeEncodeError:
        return "UnicodeEncodeError"


def _past_ascii(files: list[tuple[str, bytes]], folders: list[str]) -> bool:
    return any(ord(c) > 0x7F for t in [*(n for n, _ in files), *folders] for c in t)


def _names_file(unicode: bool) -> bytes:
    lines = [
        {
            "n": [[_t(n), _b(d)] for n, d in files],
            "d": [_t(n) for n in folders],
            "r": _name_answer(files, folders),
        }
        for files, folders in _name_cases()
        if _past_ascii(files, folders) == unicode
    ]
    name = "gitsystem_probe_names_unicode" if unicode else "gitsystem_probe_names"
    header: dict[str, Any] = {"fixture": name, "cases": len(lines)}
    return jsonl([header, *lines])


# -- trees -------------------------------------------------------------------

Tree = list[list[Any]]


def _probe(
    *, skip: tuple[str, ...] = (), extra: Tree | None = None, under: str = "P"
) -> Tree:
    """The probe as it is built (``["B", under, skip]``, `_expanded`),
    less the entries `skip` names, then `extra`."""
    return [["B", under, list(skip)], *(extra or [])]


def _built(under: str, skip: list[str]) -> Tree:
    out: Tree = [["d", under]]
    for name in gitsystem._PROBE_FOLDERS:
        if name not in skip:
            out.append(["d", f"{under}/{name}"])
    for name in gitsystem._PROBE_FILES:
        if name not in skip:
            out.append(["f", f"{under}/{name}", {"p": name}])
    return out


#: What ``["O"]`` in a tree stands for: folders and files outside the
#: state directory that links in the probe name.
OUTSIDE: Tree = [
    ["d", "out"],
    ["d", "state"],
    ["d", "out/r"],
    ["d", "out/full"],
    ["f", "out/full/q", b"x"],
    ["f", "out/full/p", gitsystem._PROBE_FILES["remotes/p"]],
    ["d", "out/c"],
    ["d", "out/probe"],
    ["f", "out/f", b"outside"],
    ["f", "out/head", {"p": "HEAD"}],
    ["f", "out/other", b"other"],
]


def _expanded(tree: Tree) -> Tree:
    out: Tree = []
    for e in tree:
        if e[0] == "O":
            out.extend(OUTSIDE)
        elif e[0] == "B":
            out.extend(_built(e[1], e[2]))
        else:
            out.append(e)
    return out


def _tree_cases() -> Iterator[tuple[Tree, list[Any]]]:
    whole = _probe()
    # holds, stamps, clear and repo over a probe holding something
    # unexpected.
    shapes: list[Tree] = [
        whole,
        _probe(skip=("config",)),
        _probe(skip=("remotes/p",)),
        _probe(skip=("compat-differ/config",)),
        _probe(skip=("objects",)),
        _probe(skip=("compat-differ/refs",)),
        _probe(extra=[["f", "P/extra", b"x"]]),
        _probe(extra=[["f", "P/remotes/q", b"x"]]),
        _probe(
            extra=[
                ["d", "P/compat-differ/hooks"],
                ["f", "P/compat-differ/hooks/h", b"x"],
            ]
        ),
        _probe(extra=[["f", "P/objects/pack", b"x"]]),
        _probe(extra=[["l", "P/extra", "@/out/f"]]),
        _probe(extra=[["l", "P/extra", "@/out"]]),
        _probe(extra=[["l", "P/extra", "nothing"]]),
        _probe(extra=[["p", "P/extra"]]),
        _probe(extra=[["d", "P/x"], ["l", "P/x/out", "@/out"]]),
        _probe(skip=("HEAD",), extra=[["f", "P/HEAD", b"ref: refs/heads/other\n"]]),
        _probe(skip=("HEAD",), extra=[["f", "P/HEAD", b"ref: refs/heads/main\n\n"]]),
        _probe(skip=("HEAD",), extra=[["f", "P/HEAD", b"ref: refs/heads/mai"]]),
        _probe(skip=("HEAD",), extra=[["d", "P/HEAD"]]),
        _probe(skip=("HEAD",), extra=[["d", "P/HEAD"], ["f", "P/HEAD/x", b"y"]]),
        _probe(skip=("HEAD",), extra=[["l", "P/HEAD", "@/out/head"]]),
        _probe(skip=("HEAD",), extra=[["l", "P/HEAD", "@/out/other"]]),
        _probe(skip=("HEAD",), extra=[["l", "P/HEAD", "@/out/none"]]),
        _probe(skip=("HEAD",), extra=[["l", "P/HEAD", "objects"]]),
        _probe(skip=("objects",), extra=[["f", "P/objects", b"x"]]),
        _probe(skip=("objects",), extra=[["p", "P/objects"]]),
        _probe(skip=("objects",), extra=[["l", "P/objects", "@/out"]]),
        _probe(skip=("objects",), extra=[["l", "P/objects", "@/out/none"]]),
        _probe(skip=("objects",), extra=[["l", "P/objects", "refs"]]),
        _probe(
            skip=("remotes", "remotes/p"),
            extra=[["l", "P/remotes", "@/out/r"]],
        ),
        _probe(
            skip=("remotes", "remotes/p"),
            extra=[["l", "P/remotes", "@/out/full"]],
        ),
        _probe(
            skip=(
                "compat-differ",
                "compat-differ/objects",
                "compat-differ/refs",
                "compat-differ/HEAD",
                "compat-differ/config",
            ),
            extra=[["l", "P/compat-differ", "@/out/c"]],
        ),
        _probe(extra=[["m", "P/config", 0o000]]),
        _probe(extra=[["m", "P/remotes", 0o000]]),
        _probe(extra=[["m", "P/remotes", 0o300]]),
        _probe(extra=[["m", "P/objects", 0o000]]),
        _probe(extra=[["m", "P/config", 0o666]]),
        _probe(extra=[["m", "P", 0o777]]),
        _probe(
            extra=[
                ["d", "P/junk"],
                ["d", "P/junk/locked"],
                ["m", "P/junk/locked", 0o000],
            ]
        ),
        _probe(
            extra=[["d", "P/junk"], ["f", "P/junk/f", b"x"], ["m", "P/junk", 0o500]]
        ),
        _probe(extra=[["f", "P/remotes/q", b"x"], ["m", "P/remotes", 0o500]]),
        # Listed but not searchable: kept entries known by the listing's
        # type alone.
        _probe(extra=[["m", "P/remotes", 0o444]]),
        _probe(extra=[["m", "P/remotes", 0o666]]),
        _probe(extra=[["m", "P/compat-differ", 0o444]]),
        _probe(extra=[["m", "P/compat-differ", 0o666]]),
        _probe(extra=[["f", "P/remotes/q", b"x"], ["m", "P/remotes", 0o444]]),
        # A link or folder check made through a folder that cannot be
        # searched (`_python`).
        _probe(extra=[["m", "state", 0o000]]),
        _probe(
            under="state/git-ask",
            extra=[["l", "P", "@/state/git-ask"], ["m", "state/git-ask", 0o000]],
        ),
        [["d", "P"]],
        [["l", "P", "@/out/probe"]],
        [["l", "P", "@/out/none"]],
        [["l", "P", "@/out/full"]],
        [["f", "P", b"x"]],
        [["p", "P"]],
        [],
        _probe(under="out/built", extra=[["l", "P", "@/out/built"]]),
    ]
    for shape in shapes:
        tree = [["O"], *shape]
        for op in (["holds"], ["stamps"], ["clear"], ["repo", KEPT_AT]):
            if op[0] == "repo" and _fifo_read(shape):
                continue
            yield tree, op
    # The sweep, at the edge on both sides.
    dated: Tree = [
        ["d", f"state/{OTHER}"],
        ["f", f"state/{OTHER}/HEAD", b"x"],
        ["l", f"state/{OTHER}/out", "@/out"],
        ["d", f"state/{OTHER}/sub"],
        ["l", f"state/{OTHER}/sub/f", "@/out/f"],
        ["d", "state/git-probe"],
        ["f", "state/git-probe-fedcba9876543210", b"x"],
        ["l", "state/git-probe-aaaaaaaaaaaaaaaa", "@/out"],
        ["l", "state/git-probe-bbbbbbbbbbbbbbbb", "@/out/f"],
        ["l", "state/git-probe-cccccccccccccccc", "none"],
        ["p", "state/git-probe-dddddddddddddddd"],
        # Names that only resemble a probe's.
        ["d", "state/git-probe-ABCDEF0123456789"],
        ["d", "state/git-probe-0123456789abcde"],
        ["d", "state/git-probe-0123456789abcdef0"],
        ["d", "state/git-probe-"],
        ["d", "state/git-probes"],
        ["d", "state/xgit-probe"],
        ["d", "state/Git-probe-1"],
        ["d", "state/git-probe-0123456789abcdeg"],
        ["d", "state/git-probe_0123456789abcdef"],
        ["f", "state/git-ask", b"x"],
        ["d", "state/" + P],
    ]
    dates = [["t", e[1], T, 0] for e in dated if e[1].count("/") == 1]
    base: Tree = [["d", "out"], ["f", "out/f", b"outside"], ["d", "state"]]
    for now in (KEPT_AT, GONE_AT, float(T + 10 * HOUR), float(T), 0.0):
        yield base + dated + dates, ["sweep", now]
    # The edge with nanoseconds, and a time past now.
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["t", f"state/{OTHER}", T, 500_000_000],
        ],
        ["sweep", T + HOUR + 0.5],
    )
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["t", f"state/{OTHER}", T, 500_000_000],
        ],
        ["sweep", math.nextafter(T + HOUR + 0.5, 0)],
    )
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["t", f"state/{OTHER}", T + 2 * int(HOUR), 0],
        ],
        ["sweep", GONE_AT],
    )
    # What a sweep cannot remove, and what it reads.
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["d", f"state/{OTHER}/locked"],
            ["f", f"state/{OTHER}/locked/f", b"x"],
            ["t", f"state/{OTHER}", T, 0],
            ["m", f"state/{OTHER}/locked", 0o500],
        ],
        ["sweep", GONE_AT],
    )
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["d", f"state/{OTHER}/locked"],
            ["t", f"state/{OTHER}", T, 0],
            ["m", f"state/{OTHER}/locked", 0o000],
        ],
        ["sweep", GONE_AT],
    )
    yield (
        base
        + [
            ["f", f"state/{OTHER}", b"x"],
            ["t", f"state/{OTHER}", T, 0],
            ["m", "state", 0o500],
        ],
        ["sweep", GONE_AT],
    )
    yield base + [["m", "state", 0o000]], ["sweep", GONE_AT]
    yield base + [["m", "state", 0o300]], ["sweep", GONE_AT]
    yield [["d", "out"], ["f", "state", b"x"]], ["sweep", GONE_AT]
    yield [["d", "out"]], ["sweep", GONE_AT]
    hidden: Tree = [
        ["d", "out"],
        ["d", "out/g"],
        ["d", "out/g/st"],
        ["l", "state", "@/out/g/st"],
        ["m", "out/g", 0o000],
    ]
    yield hidden, ["sweep", GONE_AT]
    yield hidden, ["repo", GONE_AT]
    yield (
        [
            ["d", "out"],
            ["d", "out/st"],
            ["d", f"out/st/{OTHER}"],
            ["t", f"out/st/{OTHER}", T, 0],
            ["l", "state", "@/out/st"],
        ],
        ["sweep", GONE_AT],
    )
    # The sweep inside the build: an old probe of other content goes, the
    # probe's own name never.
    yield (
        base
        + [
            ["d", f"state/{OTHER}"],
            ["f", f"state/{OTHER}/HEAD", b"x"],
            ["t", f"state/{OTHER}", T, 0],
        ]
        + _probe(extra=[["t", "P", T, 0]]),
        ["repo", GONE_AT],
    )
    yield base + [["m", "state", 0o300]], ["repo", GONE_AT]
    yield [["d", "out"]], ["repo", GONE_AT]
    yield [["d", "out"], ["f", "state", b"x"]], ["repo", GONE_AT]
    # The ask folder.
    asked: list[Tree] = [
        [],
        [["d", "state"]],
        [["d", "state"], ["d", "state/git-ask"]],
        [["d", "state"], ["d", "state/git-ask"], ["m", "state/git-ask", 0o777]],
        [["d", "state"], ["d", "state/git-ask"], ["f", "state/git-ask/x", b""]],
        [["d", "state"], ["d", "state/git-ask"], ["d", "state/git-ask/.git"]],
        [["d", "state"], ["d", "state/git-ask"], ["l", "state/git-ask/x", "none"]],
        [["d", "state"], ["d", "state/git-ask"], ["m", "state/git-ask", 0o000]],
        [["d", "state"], ["d", "state/git-ask"], ["m", "state/git-ask", 0o300]],
        [["d", "state"], ["f", "state/git-ask", b"x"]],
        [["d", "state"], ["p", "state/git-ask"]],
        [["d", "state"], ["l", "state/git-ask", "@/out/empty"]],
        [["d", "state"], ["l", "state/git-ask", "@/out/full"]],
        [["d", "state"], ["l", "state/git-ask", "@/out/none"]],
        [["d", "state"], ["l", "state/git-ask", "@/out/f"]],
        [["d", "state"], ["l", "state/git-ask", "git-ask"]],
        [["d", "state"], ["m", "state", 0o500]],
        [["f", "state", b"x"]],
        [["l", "state", "@/out/empty"]],
    ]
    for tree in asked:
        yield (
            [
                ["d", "out"],
                ["d", "out/empty"],
                ["d", "out/full"],
                ["f", "out/full/x", b"x"],
                ["f", "out/f", b"x"],
                *tree,
            ],
            ["where"],
        )


def _fifo_read(shape: Tree) -> bool:
    """Whether the build would read a FIFO as a probe file."""
    files = {f"P/{n}" for n in gitsystem._PROBE_FILES}
    return any(e[0] == "p" and e[1] in files for e in shape)


def _restricted(tree: Tree, bits: int) -> set[str]:
    """The folders the tree sets a mode lacking any of `bits` on."""
    folders = {_rel(e[1]) for e in _expanded(tree) if e[0] == "d"}
    return {
        _rel(e[1])
        for e in _expanded(tree)
        if e[0] == "m" and e[2] & bits != bits and _rel(e[1]) in folders
    }


def _searched(tree: Tree, rel: str) -> list[str]:
    """The folders a look-up of `rel` searches through, links in the tree
    followed (an over-count: the last one is followed too)."""
    links = {_rel(e[1]): e[2] for e in _expanded(tree) if e[0] == "l"}
    parts = _rel(rel).split("/")
    cur: list[str] = []
    seen: list[str] = []
    hops = 0
    while parts and hops < 40:
        name = parts.pop(0)
        cand = "/".join([*cur, name])
        if cand in links:
            hops += 1
            target = links[cand]
            new = (
                target[2:].split("/")
                if target.startswith("@/")
                else [*cur, *target.split("/")]
            )
            parts, cur = new + parts, []
            continue
        if parts:
            seen.append(cand)
        cur.append(name)
    return seen


#: The paths each function asks `Path.is_symlink` or `Path.is_dir` of.
CHECKED = {
    "sweep": ["state"],
    "clear": ["P", "P/remotes", "P/compat-differ"],
    "repo": ["state", "P", "P/remotes", "P/compat-differ"],
}


def _python(tree: Tree, op: list[Any]) -> bool:
    """Whether a link or folder check is made through a folder that cannot
    be searched: 3.13 raises the error there, 3.14 answers False."""
    blind = _restricted(tree, 0o100)
    return any(
        f in blind for rel in CHECKED.get(op[0], []) for f in _searched(tree, rel)
    )


def _refuse_partial_order(tree: Tree) -> None:
    """Refuse a tree where a removal can fail partway in a folder holding
    more than one entry: which entries remain then is the walk's order
    (3.13's `rmtree` walks in reverse, the port forward), not pinned."""
    entries = _expanded(tree)
    listing = {"state", *(_rel(f) for f in ("P", "P/remotes", "P/compat-differ"))}
    made = [_rel(e[1]) for e in entries if e[0] in "dflp"]

    def children(folder: str) -> int:
        return sum(1 for m in made if m.rsplit("/", 1)[0] == folder and "/" in m)

    for folder in _restricted(tree, 0o300):
        if folder in listing or folder.startswith("out"):
            continue
        chain = []
        at = folder
        while "/" in at and at not in listing:
            chain.append(at)
            at = at.rsplit("/", 1)[0]
        if not chain:
            continue
        top = chain[-1]
        if top.startswith("state/") and top.count("/") == 1:
            name = top[len("state/") :]
            # The sweep removes only another probe's name.
            if name == P or not gitsystem._PROBE_NAMES.fullmatch(name):
                continue
        for at in chain:
            assert children(at) <= 1, f"a removal can fail partway in {at}: {tree}"


def _rel(rel: str) -> str:
    return "state/" + P + rel[1:] if rel == "P" or rel.startswith("P/") else rel


def _make(root: Path, tree: Tree) -> None:
    modes: list[tuple[Path, int]] = []
    for entry in _expanded(tree):
        kind, path = entry[0], root / _rel(entry[1])
        if kind == "d":
            path.mkdir()
            os.chmod(path, 0o755)
        elif kind == "f":
            data = entry[2]
            path.write_bytes(
                gitsystem._PROBE_FILES[data["p"]] if isinstance(data, dict) else data
            )
            os.chmod(path, 0o644)
        elif kind == "l":
            target = entry[2]
            os.symlink(
                str(root) + target[1:] if target.startswith("@/") else target, path
            )
        elif kind == "p":
            os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
        elif kind == "t":
            ns = entry[2] * 1_000_000_000 + entry[3]
            os.utime(path, ns=(ns, ns), follow_symlinks=False)
        elif kind == "m":
            modes.append((path, entry[2]))
    for path, mode in reversed(modes):
        os.chmod(path, mode)


def _label(root: str, path: str) -> str:
    if path == root or path.startswith(root + "/"):
        return "@" + path[len(root) :]
    return path


def _walk(root: Path) -> list[list[Any]]:
    out: list[list[Any]] = []

    def visit(path: Path, rel: str) -> None:
        st = os.lstat(path)
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            out.append([rel, "l", mode & 0o077, _label(str(root), os.readlink(path))])
        elif stat.S_ISDIR(st.st_mode):
            out.append([rel, "d", mode & 0o077, None])
            if mode & 0o500 != 0o500:
                os.chmod(path, 0o700)
            for name in sorted(os.listdir(path)):
                visit(path / name, f"{rel}/{name}" if rel else name)
            os.chmod(path, mode)
        elif stat.S_ISREG(st.st_mode):
            if mode & 0o400 == 0:
                os.chmod(path, 0o600)
            data = path.read_bytes()
            os.chmod(path, mode)
            what = (
                _b(data)
                if len(data) <= 48
                else "#" + hashlib.sha256(data).hexdigest()[:16]
            )
            out.append([rel, "f", mode & 0o077, what])
        elif stat.S_ISFIFO(st.st_mode):
            out.append([rel, "p", mode & 0o077, None])
        else:
            out.append([rel, "o", mode & 0o077, None])

    for name in sorted(os.listdir(root)):
        visit(root / name, name)
    return out


def _stamp(s: Any) -> Any:
    return None if s is None else [s[2], s[4]]


def _run(root: Path, op: list[Any]) -> Any:
    state = root / "state"
    probe = state / P
    clock = types.SimpleNamespace(time=lambda: op[1] if len(op) > 1 else 0.0)
    with (
        mock.patch.object(_daemon_client, "resolve_state_dir", lambda: state),
        mock.patch.object(gitsystem, "time", clock),
    ):
        try:
            if op[0] == "sweep":
                gitsystem._sweep_probes(state)
                return None
            if op[0] == "clear":
                gitsystem._clear_probe(probe)
                return None
            if op[0] == "holds":
                return gitsystem._probe_holds(os.path.realpath(probe))
            if op[0] == "stamps":
                return [
                    _stamp(s) for s in gitsystem._probe_stamps(os.path.realpath(probe))
                ]
            if op[0] == "repo":
                got = gitsystem._probe_repository()
                if got is None:
                    return None
                real, before = got
                return [
                    _label(str(root), real),
                    before == gitsystem._probe_stamps(real),
                ]
            assert op[0] == "where"
            where = gitsystem._where()
            if where is None:
                return None
            real, env = where
            assert list(env) == ["GIT_CEILING_DIRECTORIES"]
            return [
                _label(str(root), real),
                _label(str(root), env["GIT_CEILING_DIRECTORIES"]),
            ]
        except OSError as e:
            return type(e).__name__


def _spell_tree(tree: Tree) -> Tree:
    return [
        [*e[:2], _b(e[2]), *e[3:]] if e[0] == "f" and isinstance(e[2], bytes) else e
        for e in tree
    ]


def _tree_file(python: bool) -> bytes:
    refuse_root()
    lines: list[dict[str, Any]] = []
    cases = [(t, o) for t, o in _tree_cases() if _python(t, o) == python]
    for tree, _ in cases:
        _refuse_partial_order(tree)
    with _sandbox.world() as world:
        base = Path(os.path.realpath(world.root / "gitsystem-probe"))
        base.mkdir()
        for i, (tree, op) in enumerate(cases):
            root = base / str(i)
            root.mkdir()
            os.chmod(root, 0o755)
            _make(root, tree)
            before = _walk(root)
            answer = _run(root, op)
            after = _walk(root)
            _open_up(root)
            shutil.rmtree(root)
            spelled_op = [op[0], *(repr(float(v)) for v in op[1:])]
            line = {"t": _spell_tree(tree), "o": spelled_op, "r": answer}
            line.update(_diff(before, after))
            lines.append(line)
    header = {
        "fixture": "gitsystem_probe_python" if python else "gitsystem_probe",
        "cases": len(lines),
        "probe": P,
        "files": [[n, _b(d)] for n, d in gitsystem._PROBE_FILES.items()],
        "folders": list(gitsystem._PROBE_FOLDERS),
        "outside": _spell_tree(OUTSIDE),
    }
    return jsonl([header, *lines])


def _diff(before: list[list[Any]], after: list[list[Any]]) -> dict[str, Any]:
    """What the run changed: ``"g"`` the paths gone, ``"n"`` the entries
    new or changed (`_walk`'s), each sorted; empty ones left out."""
    was = {e[0]: e for e in before}
    now = {e[0]: e for e in after}
    out: dict[str, Any] = {}
    gone = sorted(p for p in was if p not in now)
    new = [now[p] for p in sorted(now) if was.get(p) != now[p]]
    if gone:
        out["g"] = gone
    if new:
        out["n"] = new
    return out


def _open_up(root: Path) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o700)


def generate() -> dict[str, bytes]:
    return {"gitsystem_probe.jsonl": _tree_file(False)}


def generate_python() -> dict[str, bytes]:
    return {"gitsystem_probe_python.jsonl": _tree_file(True)}


def generate_names() -> dict[str, bytes]:
    return {"gitsystem_probe_names.jsonl": _names_file(False)}


def generate_names_unicode() -> dict[str, bytes]:
    return {"gitsystem_probe_names_unicode.jsonl": _names_file(True)}
