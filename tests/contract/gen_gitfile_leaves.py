"""Fixtures: the leaves of the rule a ``.git`` entry is followed by
(``gitfile_leaves.jsonl``, ``gitfile_leaves_posix.jsonl``).

The native build ports `gitfile_rule` unit by unit (native/README.md,
"The rule a .git entry is followed by"); these pin its first unit, the
leaves every later one reads through, to the reference.

``gitfile_leaves.jsonl`` holds what is a function of its inputs alone. The
first line is a header (the count); then one line per case, by ``"k"``:

- ``pf``: `gitfile_rule._parse_gitfile(data, dotgit)` with the host's
  ``os.path`` taken as ``posixpath`` (``px``) and as ``ntpath`` (``nt``):
  ``d`` the bytes (``=`` and the text where it is printable ASCII without a
  backslash, else hex, or ``[[piece, count], ...]`` repeated and joined),
  ``g`` the ``.git`` path for each flavour (spelled the same way), and the
  answer, the path spelled as its UTF-8 bytes, or null.
- ``hr``: `_headref_content(data)`, a boolean.
- ``jf``: `_judge_format("config", gitconfig.git_entries(data))` and
  `object_format(data)` (``of``), with the git on PATH taken as data:
  ``gv`` the release `_git_is_at_least` reads (null: git has not named
  one) and ``rc`` what `_reads_compat` answers. ``j`` is ``{"why": text}``
  or the format: ``v`` the version, ``wc`` worktreeConfig, ``t`` the
  ``core.bare`` and ``core.worktree`` taken as ``[[name, value], ...]``,
  ``h`` the object format; absent where git cannot parse the file.
- ``pg``: `_location_probe.path_gone` and `path_missing` of an exception
  made here: ``e`` ``value`` (a ValueError), ``other`` (a RuntimeError) or
  an OSError of the errno named ``n`` (the class CPython picks for it is
  ``c``), with a Windows code ``w`` where one is set.
- ``br``: `location_reads.bounded_read` (``one``) or `bounded_reads` of
  jobs ``[path, what, outcome]``: an outcome ``{"v": value}`` returns it,
  ``{"x": 1}`` raises an OSError; every job of a path in ``hung`` waits on
  an event that is set only after the case is answered, so it never
  answers within the budget. The answer lists each job's value, ``"U"``
  (`UNANSWERED`) or ``"N"`` (`NOT_READ`); ``raises`` the same with
  ``raises=True``, ``{"raised": class}`` where it raised.

``gitfile_leaves_posix.jsonl`` holds `_gitfile_target(path)` (``gt``) and
`_small_text(path)` (``st``, the text spelled as its UTF-8 bytes) of real
trees, written as `gen_gitconfig_file` writes them (``f``, ``d``, ``l``,
``h``, ``m``, ``t``, the same keys), with the case's directory recorded
as ``<scratch>``. No FIFO is read: the reference opens with a blocking
``open()`` there, which waits for a writer (native/README.md records it).

Gates. ``gitfile_leaves`` has none: the parse runs ``posixpath`` and
``ntpath`` on every host (the module's ``os`` swapped for one whose
``path`` is the flavour's), and no case holds a spelling those modules
read otherwise from 3.11 to 3.14 (``ntpath.isabs`` of a lone leading
separator, a ``\\\\?\\`` or ``\\\\.\\`` prefix: the README names them);
`str.strip`'s set of spaces and UTF-8's replacement are the same under
every Unicode version CI runs; the errnos are named, not numbered, and
each exists on every platform; a bounded read's answer here does not
depend on speed (a healthy read answers at once within a quarter second,
a hung one never does). ``gitfile_leaves_posix`` is POSIX only
(`generate.POSIX_ONLY`): its trees hold symbolic links, mode-000 files
and directories and paths through a file. Nothing in it depends on case
folding, the link-hop bound (no chain of 30 links), a name only Linux
stores, root, or the Python release (``open`` and ``read`` answer alike
from 3.11 to 3.14). No git runs and no environment variable is read.
"""

from __future__ import annotations

import errno
import hashlib
import ntpath
import os
import posixpath
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bettermemory import _location_probe, gitconfig, gitfile_rule, location_reads

from . import _sandbox
from ._common import jsonl
from .gen_gitconfig_file import Case, _content, _expand

_Content = bytes | list[tuple[bytes, int]]

LIMIT = 1 << 20
FORMAT_LIMIT = 1 << 21
#: How long a bounded read here waits.
BUDGET = 0.25


def _b(data: bytes) -> str:
    if all(0x20 <= b < 0x7F and b != 0x5C for b in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _path_out(text: str | None) -> Any:
    """A path or a text as its bytes (`_b`), or ``{"n": length, "sha256":
    hex}`` past 256 bytes."""
    if text is None:
        return None
    data = text.encode("utf-8", "surrogateescape")
    if len(data) > 256:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _b(data)


def _lone_root(data: bytes) -> bool:
    """Whether the ``gitdir:`` target in `data` starts with one separator
    alone: ``ntpath.isabs`` reads it as absolute before 3.13 and as
    relative from 3.13, so its ``nt`` answer is not recorded."""
    text = data.decode("utf-8", "replace").strip()
    target = text[len("gitdir:") :].strip() if text.startswith("gitdir:") else ""
    return target[:1] in ("/", "\\") and target[1:2] not in ("/", "\\")


# ---------------------------------------------------------------------------
# What a `gitdir:` file's bytes name
# ---------------------------------------------------------------------------

_DOTGIT = {"px": "/w/r/.git", "nt": "C:\\w\\r\\.git"}
_DOTGITS: list[dict[str, str]] = [
    _DOTGIT,
    {"px": "/.git", "nt": "C:\\.git"},
    {"px": "r/.git", "nt": "\\\\srv\\sh\\r\\.git"},
    {"px": "/w/\udcff/.git", "nt": "D:r\\.git"},
]


def _gitdir_data() -> Iterator[_Content]:
    yield from (
        b"gitdir: x",
        b"gitdir:x",
        b"gitdir: x\n",
        b"gitdir: x\r\n",
        b"gitdir:\tx\t\r\n",
        b"  gitdir: x  ",
        b"\n\r\ngitdir: x",
        b"gitdir: x\ny",
        b"gitdir: x y",
        b"gitdir: ",
        b"gitdir:",
        b"gitdir:\n\t ",
        b"",
        b"x",
        b"\n",
        b"GITDIR: x",
        b"Gitdir: x",
        b"gitdir : x",
        b"\xef\xbb\xbfgitdir: x",
        b"gitdir: \xef\xbb\xbfx",
        b"gitdir: a\x00b",
        b"\x00gitdir: x",
        b"gitdir: \xff\xfe",
        b"gitdir: a\xc3",
        b"gitdir: \xed\xa0\x80z",
        b"gitdir: \xf0\x9f\x98",
        b"gitdir: caf\xc3\xa9",
        b"\x0bgitdir: x\x0c",
        b"\x1cgitdir: x\x1f",
        b"gitdir: x\xc2\xa0",
        b"gitdir: x\xe2\x80\x83",
        b"gitdir: \xe3\x80\x80x",
        b"\xc2\x85gitdir: x\xe2\x80\xa8",
        b"gitdir: x\xe2\x80\x8b",
        b"gitdir: ../x",
        b"gitdir: ../../..",
        b"gitdir: ../../../../../..",
        b"gitdir: a/./b//c/",
        b"gitdir: .",
        b"gitdir: ..",
        b"gitdir: /abs/p",
        b"gitdir: //abs/p",
        b"gitdir: ///abs/p",
        b"gitdir: /a/../..",
        b"gitdir: x/..",
        b"gitdir: ~/x",
        b"gitdir: C:\\x\\y",
        b"gitdir: C:x",
        b"gitdir: c:/x/../y",
        b"gitdir: D:",
        b"gitdir: c:\\",
        b"gitdir: \\\\srv\\sh\\x\\..\\y",
        b"gitdir: //srv/sh/x",
        b"gitdir: a\\b",
        b"gitdir: a/b\\..\\c",
        b"gitdir: x:\\y",
        b"gitdir: worktrees/w",
        b"gitdir: ../.git/worktrees/w",
        [(b"gitdir: x", 1), (b" ", LIMIT - 9)],
        [(b"gitdir: x", 1), (b" ", LIMIT - 8)],
        [(b"gitdir: ", 1), (b"a", LIMIT - 8)],
        [(b"gitdir: ", 1), (b"a", LIMIT - 7)],
    )


def _flavoured(flavour: str) -> Any:
    module = posixpath if flavour == "px" else ntpath
    return patch.object(gitfile_rule, "os", SimpleNamespace(path=module))


def _parse_cases() -> Iterator[dict[str, Any]]:
    for data in _gitdir_data():
        for dotgit in _DOTGITS[:1] if not isinstance(data, bytes) else _DOTGITS:
            raw = _expand(data)
            out: dict[str, Any] = {
                "k": "pf",
                "d": _content(data),
                "g": {f: _path_out(dotgit[f]) for f in ("px", "nt")},
            }
            for flavour in ("px", "nt"):
                if flavour == "nt" and _lone_root(raw):
                    continue
                with _flavoured(flavour):
                    out[flavour] = _path_out(
                        gitfile_rule._parse_gitfile(raw, dotgit[flavour])
                    )
            yield out


# ---------------------------------------------------------------------------
# git's validate_headref of a HEAD's bytes
# ---------------------------------------------------------------------------

_HEX = b"0123456789abcdef" * 2 + b"01234567"


def _headref_cases() -> Iterator[dict[str, Any]]:
    datas: list[_Content] = [
        b"",
        b"ref: refs/heads/main\n",
        b"ref:refs/x",
        b"ref: \t\n\r refs/",
        b"ref:\x0brefs/x",
        b"ref:\x0crefs/x",
        b"ref: ref/x",
        b"ref: refs",
        b" ref: refs/x",
        b"REF: refs/x",
        _HEX,
        _HEX[:39],
        _HEX.upper(),
        _HEX[:20] + _HEX[20:].upper() + b" junk",
        _HEX[:39] + b"g",
        b" " + _HEX,
        [(b"ref:", 1), (b" ", 246), (b"refs/", 1)],
        [(b"ref:", 1), (b" ", 247), (b"refs/", 1)],
        [(b"ref:", 1), (b" ", 300), (b"refs/", 1)],
        [(_HEX, 1), (b"\x00", 300)],
    ]
    for data in datas:
        yield {
            "k": "hr",
            "d": _content(data),
            "a": gitfile_rule._headref_content(_expand(data)),
        }


# ---------------------------------------------------------------------------
# A repository's format from its config's bytes
# ---------------------------------------------------------------------------

V1 = b"[core]\n\trepositoryformatversion = 1\n"
V0 = b"[core]\n\trepositoryformatversion = 0\n"


def _format_data() -> Iterator[tuple[_Content, tuple[int, int] | None, bool | None]]:
    new = (2, 54)
    for data in (
        b"",
        b"[core]\n\tbare = false\n",
        V1,
        V0,
        V1 + b"[extensions]\n\tobjectformat = sha256\n",
        V1 + b"[extensions]\n\tobjectFormat = SHA256\n",
        V1 + b"[extensions]\n\tobjectformat = md5\n",
        V1 + b"[extensions]\n\tobjectformat\n",
        V1 + b"[extensions]\n\tobjectformat = sha256\x00z\n",
        V1 + b"[extensions]\n\tobjectformat = sha256\n\tobjectformat = sha1\n",
        V0 + b"[extensions]\n\tobjectformat = sha256\n",
        b"[extensions]\n\tobjectformat = sha256\n",
        b"[core]\n\trepositoryformatversion = 2\n",
        b"[core]\n\trepositoryformatversion = -2\n[extensions]\n\tobjectformat = sha256\n",
        b"[core]\n\trepositoryformatversion = x\n",
        b"[core]\n\trepositoryformatversion\n",
        b"[core]\n\trepositoryformatversion = 1k\n",
        b"[core]\n\trepositoryformatversion = 0x1\n",
        b"[core]\n\trepositoryformatversion = 2\n\trepositoryformatversion = 1\n",
        V1 + b"[extensions]\n\tunknownext = 1\n",
        V0 + b"[extensions]\n\tunknownext = 1\n",
        V1 + b'[extensions "a"]\n\tb = 1\n',
        V0 + b"[extensions]\n\tnoop-v1 = x\n",
        V0 + b"[extensions]\n\tnoop\n\tpreciousobjects = yes\n",
        V0 + b"[extensions]\n\tpreciousobjects = maybe\n",
        V0 + b"[extensions]\n\tpartialclone\n",
        V0 + b"[extensions]\n\tpartialclone = origin\n",
        V0 + b"[extensions]\n\tworktreeconfig = true\n",
        V0 + b"[extensions]\n\tworktreeConfig = maybe\n",
        b"[extensions]\n\tworktreeconfig\n",
        V1 + b"[core]\n\tbare = maybe\n",
        V1 + b"[core]\n\tbare\n\tworktree = /w/x\n",
        V1 + b"[core]\n\tworktree\n",
        V1 + b"[core]\n\tworktree =\n\tbare = no\n",
        V1 + b"[core]\n\tworktree = a\n\tworktree = b\n",
        b"[core\n",
        b"[core]\n\tbare = \xff\n",
        [(b"#", FORMAT_LIMIT), (b"\n", 1)],
        [(b"#", FORMAT_LIMIT - 1), (b"\n", 1)],
    ):
        yield data, new, True
    for gv in ((2, 43), (2, 44), (2, 47), (2, 48), (2, 53), (2, 54), None):
        for ext in (b"refstorage = reftable", b"relativeworktrees = true"):
            yield V1 + b"[extensions]\n\t" + ext + b"\n", gv, True
        yield V1 + b"[extensions]\n\tsubmodulepathconfig = true\n", gv, True
    yield V0 + b"[extensions]\n\trefstorage = reftable\n", None, True
    yield b"[extensions]\n\trefstorage = files\n", None, True
    yield V1 + b"[extensions]\n\trefstorage = sqlite\n", new, True
    compat = b"[extensions]\n\tobjectformat = sha256\n\tcompatobjectformat = sha1\n"
    for rc in (True, False, None):
        yield V1 + compat, new, rc
        yield b"[core]\n\trepositoryformatversion = -2\n" + compat, new, rc
    yield compat, new, None
    yield V1 + b"[extensions]\n\tcompatobjectformat = sha1\n", new, True
    yield V1 + b"[extensions]\n\tcompatobjectformat = sha256\n", new, True
    yield V1 + compat + b"\tcompatobjectformat = sha1\n", new, True
    yield V1 + b"[extensions]\n\tcompatobjectformat = md5\n", new, True
    yield V1 + b"[extensions]\n\tcompatobjectformat\n", new, True
    yield V0 + compat, new, True


def _format_cases() -> Iterator[dict[str, Any]]:
    for data, gv, rc in _format_data():
        raw = _expand(data)

        def at_least(release: tuple[int, int], gv: Any = gv) -> bool | None:
            return None if gv is None else gv >= release

        with (
            patch.object(gitfile_rule, "_git_is_at_least", at_least),
            patch.object(gitfile_rule, "_reads_compat", lambda rc=rc: rc),
        ):
            out: dict[str, Any] = {
                "k": "jf",
                "d": _content(data),
                "gv": None if gv is None else list(gv),
                "rc": rc,
            }
            of = gitfile_rule.object_format(raw)
            out["of"] = None if of is None else _b(of)
            entries = gitconfig.git_entries(raw) if len(raw) <= FORMAT_LIMIT else None
            if entries is not None:
                read = gitfile_rule._judge_format("config", entries)
                if isinstance(read, str):
                    out["j"] = {"why": read}
                else:
                    out["j"] = {
                        "v": read.version,
                        "wc": read.worktree_config,
                        "t": [
                            [_b(k), None if v is None else _b(v)]
                            for k, v in sorted(read.taken.items())
                        ],
                        "h": _b(read.object_format),
                    }
        yield out


# ---------------------------------------------------------------------------
# The reading of an error as nothing at a path
# ---------------------------------------------------------------------------

_ERRNOS = (
    "ENOENT",
    "ENOTDIR",
    "ELOOP",
    "ENAMETOOLONG",
    "EACCES",
    "EPERM",
    "EIO",
    "EISDIR",
    "ENXIO",
    "ETIMEDOUT",
    "ENOTCONN",
    "EEXIST",
    "EINVAL",
)
_WINERRORS = (2, 3, 5, 15, 21, 53, 67, 123, 206, 267, 1921)


def _probe_cases() -> Iterator[dict[str, Any]]:
    def answered(exc: BaseException, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "k": "pg",
            **record,
            "gone": _location_probe.path_gone(exc),
            "missing": _location_probe.path_missing(exc),
        }

    yield answered(ValueError("embedded null byte"), {"e": "value"})
    yield answered(RuntimeError("x"), {"e": "other"})
    for name in _ERRNOS:
        exc = OSError(getattr(errno, name), "x")
        yield answered(exc, {"e": "os", "n": name, "c": type(exc).__name__})
        for code in _WINERRORS if name in ("ENOENT", "EACCES", "EIO") else ():
            # A Windows error as CPython raises it on any host: the class
            # its errno picks, carrying the code.
            cls = type(exc)
            win = type(f"Win{cls.__name__}", (cls,), {"winerror": code})(
                getattr(errno, name), "x"
            )
            yield answered(win, {"e": "os", "n": name, "c": cls.__name__, "w": code})


# ---------------------------------------------------------------------------
# Bounded reads with the time bound as data
# ---------------------------------------------------------------------------

_BOUNDED: list[dict[str, Any]] = [
    {"one": True, "jobs": [["a", "w", {"v": 1}]], "hung": []},
    {"one": True, "jobs": [["a", "w", {"x": 1}]], "hung": []},
    {"one": True, "jobs": [["a", "w", {"v": 1}]], "hung": ["a"]},
    {"jobs": [], "hung": []},
    {
        "jobs": [
            ["a", "w", {"v": 1}],
            ["b", "w", {"x": 1}],
            ["a", "u", {"v": "two"}],
            ["c", "w", {"v": 3}],
        ],
        "hung": [],
    },
    {
        "jobs": [
            ["a", "w", {"v": 1}],
            ["h", "w", {"v": 2}],
            ["b", "w", {"x": 1}],
            ["h", "u", {"v": 4}],
            ["c", "w", {"v": [5]}],
        ],
        "hung": ["h"],
    },
    {
        "jobs": [["h", "w", {"v": 1}], ["g", "w", {"v": 2}], ["h", "w", {"v": 3}]],
        "hung": ["h", "g"],
    },
]


def _bounded_cases() -> Iterator[dict[str, Any]]:
    for n, case in enumerate(_BOUNDED):
        release = threading.Event()

        def job(path: str, outcome: dict[str, Any], hung: bool) -> Callable[[], Any]:
            def read() -> Any:
                if hung:
                    release.wait()
                if "x" in outcome:
                    raise OSError(errno.EIO, "x")
                return outcome["v"]

            return read

        def jobs(stem: str, case: dict[str, Any] = case) -> list[Any]:
            # A path of its own for each run, so no flight of an earlier
            # case is on the board.
            return [
                (
                    f"gitfile-leaves/{n}/{stem}/{path}",
                    what,
                    job(path, outcome, path in case["hung"]),
                )
                for path, what, outcome in case["jobs"]
            ]

        def shown(value: Any) -> Any:
            if value is location_reads.UNANSWERED:
                return "U"
            if value is location_reads.NOT_READ:
                return "N"
            return {"v": value}

        out: dict[str, Any] = {"k": "br", **case}
        try:
            if case.get("one"):
                path, what, read = jobs("one")[0]
                try:
                    out["a"] = shown(
                        location_reads.bounded_read(path, what, read, BUDGET)
                    )
                except OSError as exc:
                    out["a"] = {"raised": type(exc).__name__}
            else:
                out["a"] = [
                    shown(v) for v in location_reads.bounded_reads(jobs("all"), BUDGET)
                ]
                try:
                    out["raises"] = [
                        shown(v)
                        for v in location_reads.bounded_reads(
                            jobs("raises"), BUDGET, raises=True
                        )
                    ]
                except OSError as exc:
                    out["raises"] = {"raised": type(exc).__name__}
        finally:
            release.set()
        yield out


def generate() -> dict[str, bytes]:
    cases = [
        *_parse_cases(),
        *_headref_cases(),
        *_format_cases(),
        *_probe_cases(),
        *_bounded_cases(),
    ]
    header = {"fixture": "gitfile_leaves", "cases": len(cases)}
    return {"gitfile_leaves.jsonl": jsonl([header, *cases])}


# ---------------------------------------------------------------------------
# The reads of real trees (POSIX)
# ---------------------------------------------------------------------------


def _tree_cases() -> Iterator[Case]:
    g = ".git"
    yield Case(g, {g: b"gitdir: x\n"})
    yield Case(
        "in/.git",
        {"in/.git": b"gitdir: ../other/.git/worktrees/w\r\n"},
        dirs=("other",),
    )
    yield Case(g, {g: b"gitdir: x/../y/./z//"})
    yield Case(g, {g: b"  gitdir:\t \xe3\x80\x80sub\xc2\xa0\n"})
    yield Case(g, {g: b"gitdir: caf\xc3\xa9 \xff\n"})
    yield Case(g, {g: b"\xef\xbb\xbfgitdir: x\n"})
    yield Case(g, {g: b"gitdir:"})
    yield Case(g, {g: b""})
    yield Case(g, {g: b"ref: refs/heads/main\n"})
    yield Case(g, {g: b"gitdir: a\x00b\n"})
    yield Case(g, {g: [(b"gitdir: x", 1), (b"\n", LIMIT - 9)]})
    yield Case(g, {g: [(b"gitdir: x", 1), (b"\n", LIMIT - 8)]})
    yield Case(g, {g: [(b"gitdir: x", 1), (b"\n", LIMIT + 4096)]})
    yield Case("in/.git", {"in/.git": b"gitdir: ../x\n"})
    yield Case("in/deeper/.git", {"in/deeper/.git": b"gitdir: ../../x"})
    # Links: to a file, to a file in another folder (the path is the
    # link's own, not its target's), dangling, a loop, to a folder, a
    # chain, through a linked folder.
    yield Case(g, {"f": b"gitdir: x\n"}, links={g: "f"})
    yield Case(
        "in/.git",
        {"other/f": b"gitdir: ../y\n"},
        dirs=("in",),
        links={"in/.git": "../other/f"},
    )
    yield Case(g, {}, links={g: "nothing"})
    yield Case(g, {}, links={g: "loop", "loop": ".git"})
    yield Case(g, {}, dirs=("d",), links={g: "d"})
    yield Case(
        g,
        {"f": b"gitdir: z"},
        links={g: "l1", "l1": "l2", "l2": "l3", "l3": "f"},
    )
    yield Case("in/.git", {"real/.git": b"gitdir: q\n"}, links={"in": "real"})
    yield Case(g, {}, links={g: "/dev/null"})
    # Folders, missing paths, a path through a file, permissions.
    yield Case(g, {}, dirs=(g,))
    yield Case(g, {})
    yield Case("f/.git", {"f": b"gitdir: x"})
    yield Case(g, {g: b"gitdir: x\n"}, locked=(g,))
    yield Case("in/.git", {"in/.git": b"gitdir: x\n"}, locked=("in",))
    yield Case("in/.git", {"in/.git": b"gitdir: x\n"}, modes={"in": 0o600})
    yield Case(g, {g: b"gitdir: x\n"}, modes={g: 0o200})
    # A hard link reads as the file.
    yield Case(g, {"f": b"gitdir: h\n"}, hard={g: "f"})
    # The trailing separator of a file's path.
    yield Case(".git/", {g: b"gitdir: x\n"})


def _tree_answer(case: Case, root: Path) -> dict[str, Any]:
    for d in case.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
    for rel, existing in case.hard.items():
        os.link(root / existing, root / rel)
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    by_depth = sorted(case.modes, key=lambda rel: rel.count("/"))
    for rel in reversed(by_depth):
        os.chmod(root / rel, case.modes[rel])
    prefix = str(root)

    def label(text: str | None) -> str | None:
        return None if text is None else _path_out(text.replace(prefix, "<scratch>"))

    name = prefix + "/" + case.target
    try:
        return {
            "gt": label(gitfile_rule._gitfile_target(name)),
            "st": label(gitfile_rule._small_text(name)),
        }
    finally:
        for rel in by_depth:
            os.chmod(root / rel, 0o700)


def generate_posix() -> dict[str, bytes]:
    cases = list(_tree_cases())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitfile-leaves"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            answer = _tree_answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "gitfile_leaves_posix", "cases": len(cases)}
    return {"gitfile_leaves_posix.jsonl": jsonl([header, *lines])}
