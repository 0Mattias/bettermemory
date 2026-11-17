"""Fixtures: what git's answers say (``gitsystem_answers.jsonl``,
``gitsystem_answers_unicode.jsonl``, ``gitsystem_answers_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitsystem/answers.rs``. This pins it
to `gitsystem`'s `_own_files`, `_variables`, `_REMOTE_ANSWERS`, the
reading of the answers inside `_reads_remotes`, the parse inside
`_older_system`, `_absolute`, and `gitversion.parse`.

Each case is ``{"f": function, "i": input, ...}`` answered in ``"r"``:

- ``"version"``: `gitversion.parse` of the bytes decoded as `run_git`
  decodes them (UTF-8, ``replace``). The answer is the three parts as
  decimal texts, or null.
- ``"absolute"``: `_absolute` of the text (UTF-8, ``surrogatepass``)
  under ``"fl"``, ``"posix"`` (`posixpath`) or ``"nt"`` (`ntpath`).
- ``"own"``: `_own_files` of the bytes.
- ``"older"``: `_older_system` given ``git config --system --list
  --show-origin -z`` exiting ``"rc"`` with the bytes.
- ``"variables"``: `_variables` of the decoded bytes, as ``[name,
  value]`` pairs in the dict's order.
- ``"probe"``: `_reads_remotes` given the three questions' answers:
  ``"rm"`` and ``"px"`` are ``[status, bytes]`` or null (no answer),
  ``"cp"`` a status or null, and ``"h"`` whether the probe stamped and
  held as before. The answer is ``[legacy, resets, prefix, differ,
  hasconfig]``.

For ``"own"`` and ``"older"``, the flavour decides `os.fsdecode` (POSIX:
``surrogateescape``; Windows: ``surrogatepass``, which raises on bytes
that are not UTF-8) and `_absolute`. Both are driven here as plain
functions of the input, so a run on any host gives the same answers.
Where the reference raises, ``x`` names the exception.

Inputs are `_b` spellings, or `_content` recipes for the enormous ones.
Text out is `_t`.

The cases are placed by their input:

- ``_unicode``: a text input with a code point past ASCII, where
  `str.strip`, ``re``'s ``\\d``, `int` and `str.upper` read the Unicode
  data.
- ``_python``: a Windows path that starts with a separator then a colon
  (3.11's `ntpath.splitdrive` takes that for a drive; 3.13's does not),
  and a digit run past 4,300 (`int`'s limit, which the interpreter
  sets).
"""

from __future__ import annotations

import math
import ntpath
import os
import posixpath
import subprocess
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory import _git, gitsystem, gitversion

from ._common import jsonl
from .gen_gitconfig_file import _b, _Content, _content, _expand
from .gen_gitsystem_record import _t

FLAVOURS = {"posix": posixpath, "nt": ntpath}
#: The longest path a Windows-flavour case gives (`_cases`).
WINDOWS_MAX_PATH = 30_000
#: `_absolute` itself, taken before any patch: `_own_files` and
#: `_older_system` call it with the host's `os.path`, so the generator
#: hands it the flavour of the case.
_ABSOLUTE = gitsystem._absolute


def _decoder(flavour: str) -> Any:
    errors = "surrogateescape" if flavour == "posix" else "surrogatepass"
    return lambda b: bytes(b).decode("utf-8", errors)


def _spell(data: _Content) -> Any:
    return _b(data) if isinstance(data, bytes) else _content(data)


def _answer(case: dict[str, Any], data: bytes) -> Any:
    f = case["f"]
    fl = case.get("fl", "posix")
    module = FLAVOURS[fl]
    try:
        if f == "version":
            got = gitversion.parse(data.decode("utf-8", "replace"))
            return None if got is None else [str(x) for x in got]
        if f == "absolute":
            return gitsystem._absolute(data.decode("utf-8", "surrogatepass"), module)
        if f == "variables":
            found = gitsystem._variables(data.decode("utf-8", "replace"))
            return [[_t(k), _t(v)] for k, v in found.items()]
        with (
            mock.patch.object(os, "fsdecode", _decoder(fl)),
            mock.patch.object(gitsystem, "_absolute", lambda p: _ABSOLUTE(p, module)),
        ):
            if f == "own":
                return [_t(p) for p in gitsystem._own_files(data)]
            if f == "older":
                done = subprocess.CompletedProcess([], case["rc"], data, b"")
                with mock.patch.object(_git, "run_git", lambda *a, **k: done):
                    return _t(gitsystem._older_system("folder", {}, math.inf) or "")
        raise ValueError(f)
    except (UnicodeDecodeError, ValueError) as exc:
        return {"x": type(exc).__name__}


def _probe(case: dict[str, Any]) -> Any:
    def done(given: Any) -> Any:
        if given is None:
            return None
        status, out = given
        return subprocess.CompletedProcess(
            [], status, _expand(out).decode("utf-8", "replace"), ""
        )

    answers = {
        gitsystem._REMOTE_ASK: done(case["rm"]),
        gitsystem._PREFIX_ASK: done(case["px"]),
        gitsystem._COMPAT_ASK: None
        if case["cp"] is None
        else subprocess.CompletedProcess([], case["cp"], "", ""),
    }
    with (
        mock.patch.object(gitsystem, "_probe_repository", lambda: ("/r", [])),
        mock.patch.object(
            gitsystem, "_probe_stamps", lambda _r: [] if case["h"] else [1]
        ),
        mock.patch.object(gitsystem, "_probe_holds", lambda _r: True),
        mock.patch.object(gitsystem, "_probe_ask", lambda q, *_a: answers[q]),
    ):
        legacy, resets, prefix, differ, hasconfig = gitsystem._reads_remotes(
            "folder", {}, math.inf
        )
    return [legacy, resets, None if prefix is None else _t(prefix), differ, hasconfig]


def _non_ascii(data: bytes) -> bool:
    return any(ord(c) > 0x7F for c in data.decode("utf-8", "replace"))


def _place(case: dict[str, Any], data: bytes) -> str:
    """Which file a case goes in, by its input."""
    f = case["f"]
    if f == "version" and any(len(run) > 4300 for run in _digit_runs(data)):
        return "python"
    if case.get("fl") == "nt":
        paths = [data] if f == "absolute" else _origins(data)
        if any(p[:1] in (b"\\", b"/") and p[1:2] == b":" for p in paths):
            return "python"
        if _non_ascii(b"".join(paths)):
            return "unicode"
    if f in ("version", "probe") and _non_ascii(data):
        return "unicode"
    return ""


def _digit_runs(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    runs, run = [], ""
    for c in text:
        if c.isdecimal():
            run += c
        else:
            runs.append(run)
            run = ""
    return [*runs, run]


def _origins(data: bytes) -> list[bytes]:
    return [p[5:] for p in data.split(b"\0") if p.startswith(b"file:")]


def _z(*fields: bytes) -> bytes:
    return b"".join(f + b"\0" for f in fields)


def _cases() -> Iterator[tuple[dict[str, Any], _Content]]:
    # gitversion.parse
    for text in (
        b"git version 2.54.0",
        b"git version 2.54.0\n",
        b"  git version 2.39.5 (Apple Git-154)\r\n",
        b"git version 2.42.0.windows.1",
        b"git version 2.56.GIT",
        b"git version 2.45.0.rc1",
        b"git version 2.45.0-rc1",
        b"git version 2.45",
        b"git version 2",
        b"git version 2.",
        b"git version .2.3",
        b"git version  2.3.4",
        b"Git version 2.3.4",
        b"git  version 2.3.4",
        b"version 2.3.4",
        b"git version 2.3.4.5",
        b"git version 2.3-4",
        b"git version 2.3+4",
        b"git version 2.3 4",
        b"git version 2.3_4",
        b"git version 02.003.0004",
        b"git version 99999999999999999999.0.0",
        b"git version 65536.65536.65536",
        b"git version 2.3.4\x00junk",
        b"git version 2.3.\x00",
        b"\x00git version 2.3.4",
        b"git version 2.3\xff.4",
        b"\xffgit version 2.3.4",
        b"git version \xd9\xa2.\xd9\xa3.4",
        b"\xe3\x80\x80git version 2.3.4\xe3\x80\x80",
        b"\x1cgit version 2.3.4\x1f",
        b"\x85git version 2.3.4",
        b"git version 2.3.4\n\ngit version 9.9.9",
        b"",
        b"   ",
        b"git version ",
    ):
        yield {"f": "version"}, text
    yield {"f": "version"}, [(b"git version 1", 1), (b"0", 4300), (b".2.3", 1)]
    yield {"f": "version"}, [(b"git version 1", 1), (b"0", 4299), (b".2.3", 1)]
    yield {"f": "version"}, [(b"git version 2.3.4 ", 1), (b"x", 1 << 20)]
    yield {"f": "version"}, [(b" ", 1 << 20), (b"git version 2.3.4", 1)]
    # _absolute, both flavours
    for text in (
        b"",
        b"/",
        b"/etc/gitconfig",
        b"etc/gitconfig",
        b"\\x",
        b"C:",
        b"C:x",
        b"C:\\x",
        b"C:/x",
        b"c:\\",
        b"1:\\x",
        b"ab\\x",
        b"ab/x",
        b"a\\x",
        b"::\\x",
        b"\\\\server\\share\\x",
        b"\\\\server\\share",
        b"\\\\server",
        b"//server/share/x",
        b"\\\\?\\C:\\x",
        b"\\\\?\\UNC\\server\\share\\x",
        b"\\\\?\\unc\\server\\share\\x",
        b"\\\\?\\UNC\\server\\share",
        b"\\\\?\\unc\\server\\share",
        b"\\\\?\\uNc\\server\\share",
        b"\\\\?\\UnC\\server\\share\\x",
        b"\\\\?\\UNC\\",
        b"\\\\?\\unc\\",
        b"\\\\?\\UNC\\s",
        b"\\\\.\\pipe\\x",
        b"\\\\\\x\\y",
        b"\\/server/share/x",
        b"/:x",
        b"\\:\\x",
        b"\\:/x",
        b"\xc3\xa9:\\x",
        b"\\\\?\\\xc3\xbcNC\\s\\h\\x",
        b"\\\\\xc3\xa9\\s\\x",
        b"\xed\xa0\x80:\\x",
    ):
        for fl in ("posix", "nt"):
            yield {"f": "absolute", "fl": fl}, text
    # _own_files
    sys_file = _z(b"system", b"file:/etc/gitconfig", b"k\nv")
    owns: list[_Content] = [
        b"",
        b"\x00",
        b"\x00\x00",
        sys_file,
        sys_file[:-1],
        sys_file[:-4],
        b"system\x00file:/etc/gitconfig",
        _z(b"unknown", b"file:/u", b"k\nv"),
        _z(b"global", b"file:/g", b"k\nv"),
        _z(b"local", b"file:/l", b"k\nv"),
        _z(b"worktree", b"file:/w", b"k\nv"),
        _z(b"command", b"command line:", b"k\nv"),
        _z(b"system", b"command line:", b"k\nv"),
        _z(b"system", b"blob:abc", b"k\nv"),
        _z(b"system", b"standard input:", b"k\nv"),
        _z(b"system", b"File:/x", b"k\nv"),
        _z(b"system", b"file//a", b"k\nv"),
        _z(b"System", b"file:/x", b"k\nv"),
        _z(b"system ", b"file:/x", b"k\nv"),
        _z(b"system", b"file:", b"k\nv"),
        _z(b"system", b"file:relative", b"k\nv"),
        _z(b"system", b"file:C:\\x", b"k\nv"),
        _z(b"system", b"file:C:/x", b"k\nv"),
        _z(b"system", b"file:\\\\s\\h\\x", b"k\nv"),
        _z(b"system", b"file:/a\r", b"k\r\nv\r"),
        _z(b"system", b"file:/\xff\xfe", b"k\nv"),
        _z(b"system", b"file:/\xed\xa0\x80", b"k\nv"),
        _z(b"system", b"file:/\xc3\xa9", b"k\nv"),
        _z(b"system", b"file:C:\\\xff", b"k\nv"),
        _z(b"global", b"file:C:\\\xff", b"k\nv"),
        _z(b"system", b"file:/a", b"k\nv", b"system", b"file:/a", b"k2\nv"),
        _z(b"system", b"file:/a", b"k", b"unknown", b"file:/b", b"", b"system"),
        _z(
            *[
                f(i)
                for i in range(12)
                for f in (
                    lambda i: b"system",
                    lambda i: b"file:/%d" % i,
                    lambda i: b"k\nv",
                )
            ]
        ),
        _z(
            *[
                f(i)
                for i in range(10)
                for f in (
                    lambda i: b"system",
                    lambda i: b"file:C:\\%d" % i,
                    lambda i: b"k\nv",
                )
            ]
        )
        + _z(b"system", b"file:C:\\\xff", b"k\nv"),
        _z(b"file:/x", b"system", b"k\nv"),
        b"system\x00file:/a\x00k\nv\x00system\x00",
        [(sys_file, 100_000)],
        [(_z(b"system", b"file:/x"), 1), (b"\x00", 1 << 16)],
    ]
    for listing in owns:
        for fl in ("posix", "nt"):
            yield {"f": "own", "fl": fl}, listing
    # The parse inside _older_system
    olders: list[tuple[int, _Content]] = [
        (0, b""),
        (0, b"file:/etc/gitconfig\x00k\nv\x00"),
        (0, b"file:/etc/gitconfig"),
        (1, b"file:/etc/gitconfig\x00k\nv\x00"),
        (128, b"file:/etc/gitconfig\x00k\nv\x00"),
        (-1, b"file:/etc/gitconfig\x00"),
        (0, b"\x00file:/etc/gitconfig\x00"),
        (0, b"command line:\x00k\nv\x00"),
        (0, b"blob:abc\x00k\nv\x00"),
        (0, b"standard input:\x00k\nv\x00"),
        (0, b"file:etc/gitconfig\x00"),
        (0, b"file:\x00"),
        (0, b"file:C:\\ProgramData\\Git\\config\x00k\nv\x00"),
        (0, b"file:C:/Program Files/Git/etc/gitconfig\x00"),
        (0, b"file:/x\r\n\x00"),
        (0, b"file:/\xff\x00"),
        (0, b"file:C:\\\xff\x00"),
        (0, b"file:/\xed\xa0\x80\x00"),
        (0, [(b"file:/", 1), (b"a", 4095), (b"\x00", 1)]),
        (0, [(b"file:/", 1), (b"a", 4096), (b"\x00", 1)]),
        (0, [(b"file:/", 1), (b"\xc3\xa9", 4095), (b"\x00", 1)]),
        (0, [(b"file:/", 1), (b"a", 1 << 20)]),
    ]
    for rc, out in olders:
        # Under the Windows flavour every path stays under 32,767
        # characters: past that, Windows' C `nt._path_splitroot_ex` may
        # refuse what the pure-Python `ntpath` reads (a native test
        # covers the long Windows path instead).
        long = len(_expand(out)) > WINDOWS_MAX_PATH
        for fl in ("posix",) if long else ("posix", "nt"):
            yield {"f": "older", "fl": fl, "rc": rc}, out
    # _variables
    for listing in (
        b"",
        b"\x00",
        b"a\nb\x00",
        b"a\nb",
        b"a\x00",
        b"a\n\x00",
        b"a\nb\nc\x00",
        b"a\nb\x00a\nc\x00b\nd\x00a\ne\x00",
        b"a\r\nb\r\x00",
        b"\nb\x00",
        b"a\xffb\nc\xfe\x00",
    ):
        yield {"f": "variables"}, listing
    yield {"f": "variables"}, [(b"x.y\nz\x00", 100_000)]
    # The answers inside _reads_remotes
    pfx = b"bettermemory.prefix\n/usr/z\x00"
    has = b"bettermemory.hasconfig\nyes\x00"
    probes: list[dict[str, Any]] = [
        {"rm": [0, b"bettermemory-probe-b\n"], "px": [0, pfx + has], "cp": 0},
        {"rm": [0, b"bettermemory-probe-a\n"], "px": [0, pfx], "cp": 1},
        {"rm": [0, b"p\n"], "px": [0, pfx], "cp": None},
        {"rm": [0, b"p\r\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b" p \t\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b"P\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b"q\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b""], "px": [0, pfx], "cp": 0},
        {"rm": [0, b"p\np\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b"\xe3\x80\x80p\n"], "px": [0, pfx], "cp": 0},
        {"rm": [0, b"p\xff\n"], "px": [0, pfx], "cp": 0},
        {"rm": [1, b"p\n"], "px": [0, pfx], "cp": 0},
        {"rm": None, "px": [0, pfx], "cp": 0},
        {"rm": [0, b"p\n"], "px": [1, pfx], "cp": 0},
        {"rm": [0, b"p\n"], "px": None, "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, pfx], "cp": 0, "h": False},
        {"rm": [0, b"p\n"], "px": [0, b""], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, has], "cp": 0},
        {
            "rm": [0, b"p\n"],
            "px": [0, b"bettermemory.prefix\n%(prefix)/z\x00"],
            "cp": 0,
        },
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/z\x00"], "cp": 0},
        {
            "rm": [0, b"p\n"],
            "px": [0, b"bettermemory.prefix\n/usr/y\x00" + has],
            "cp": 0,
        },
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/usr/z\r\n"], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/usr/z\r\n\x00"], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/usr/z"], "cp": 0},
        {
            "rm": [0, b"p\n"],
            "px": [0, b"bettermemory.prefix\n/a/z\x00bettermemory.prefix\n/b/z\x00"],
            "cp": 0,
        },
        {
            "rm": [0, b"p\n"],
            "px": [0, b"bettermemory.prefix\n/a/z\x00bettermemory.prefix\n/b\x00"],
            "cp": 0,
        },
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\x00" + has], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/\xffz/z\x00"], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\nC:\\x/z\x00"], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/a\nb/z\x00"], "cp": 0},
        {
            "rm": [0, b"p\n"],
            "px": [0, b"bettermemory.hasconfig\nyes\x00bettermemory.prefix\n/u/z\x00"],
            "cp": -9,
        },
        {"rm": [0, b"p\n"], "px": [0, b"bettermemory.prefix\n/za\x00"], "cp": 0},
        {"rm": [0, b"p\n"], "px": [0, b"Bettermemory.prefix\n/u/z\x00"], "cp": 0},
    ]
    for probe in probes:
        yield {"f": "probe", "h": True, **probe}, b""


def _file(which: str) -> bytes:
    lines: list[dict[str, Any]] = []
    for case, content in _cases():
        data = _expand(content)
        if case["f"] == "probe":
            text = b"".join(_expand(v[1]) for v in (case["rm"], case["px"]) if v)
            if _place({"f": "probe"}, text) != which:
                continue
            record = {
                **{k: v for k, v in case.items() if k not in ("rm", "px")},
                "rm": None
                if case["rm"] is None
                else [case["rm"][0], _spell(case["rm"][1])],
                "px": None
                if case["px"] is None
                else [case["px"][0], _spell(case["px"][1])],
            }
            lines.append({**record, "r": _probe(case)})
            continue
        if _place(case, data) != which:
            continue
        lines.append({**case, "i": _spell(content), "r": _answer(case, data)})
    name = "gitsystem_answers" + (f"_{which}" if which else "")
    return jsonl([{"fixture": name, "cases": len(lines)}, *lines])


def generate() -> dict[str, bytes]:
    return {"gitsystem_answers.jsonl": _file("")}


def generate_unicode() -> dict[str, bytes]:
    return {"gitsystem_answers_unicode.jsonl": _file("unicode")}


def generate_python() -> dict[str, bytes]:
    return {"gitsystem_answers_python.jsonl": _file("python")}
