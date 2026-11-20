"""Fixtures: the path helpers and the legacy ask rule of
`membership_records` (unit 1: `legacy_asks`, `_same_path`, `_flavour`,
`_path_under`, `_rebase`, `_basename`, `_same_number`,
`_common_by_pointer`, `_dir_key`, `_dir_below`).

Two files, one JSON object per line, keys sorted, ASCII:

- ``membership_records_paths.jsonl``, not gated: every case whose answer
  is the same under CPython 3.11.15, 3.12.13 and 3.13.13 (checked by
  running this generator under each), and two digest rows.
- ``membership_records_paths_python.jsonl``, gated on the Python release:
  the cases `_release_bound` picks by their input, those where a path
  read with ``ntpath`` starts with one separator, or with a separator
  then a colon, or holds ``\\\\?\\`` or ``\\\\.\\``: 3.11 and 3.12 read
  such a root, an absolute path and a drive otherwise than 3.13 does.

A case is ``{"f": function, "a": [args], "o": output}``. The host's path
module, where the function reads one (`_common_by_pointer`, `_dir_key`,
`_dir_below`), is the first argument, ``"posix"`` or ``"nt"``. `_rebase`
runs with ``os.getcwd`` answering ``cwd`` (the last argument) or, where
that is null, raising FileNotFoundError; ``nt._getfullpathname`` is never
asked (the generator runs off Windows). An ObjectKey is ``[device,
inode, birth]`` or null.

A text is a JSON string where it holds no backslash and no surrogate,
``{"c": [code points]}`` where it holds a surrogate, else ``{"b":
[parts]}``, the text split at each backslash (a backslash is never
written as JSON's escaped pair: the host-fact guard reads four as a
Windows path). An output is a text, a boolean, null, or ``{"raised":
[class, text]}``.

A digest row ``{"f": "sweep", "seed", "n", "flavour", "o"}`` pins ``n``
cases drawn by `_Rng` from ``seed`` (`_sweep`): each case is a path
triple and a selector, answered by every function above, the answers
written by `_answer_line` and joined, and ``o`` the SHA-256 of their
UTF-8 (surrogates passed). The ungated file holds a posix sweep and an
``ntpath`` sweep marked ``"stable": true``, which leaves out `_rebase`
and draws only triples whose other cases `_release_bound` passes and
whose `_path_under` joins no path `_short_unc` reads (two leading
separators naming no server and share, which 3.11's `ntpath.join`
follows with a separator of its own); the gated file holds the full
``ntpath`` sweep, those calls with it. Recorded paths are
synthetic.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
import types
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory import membership_records as mr

_SEED = 20261119
_SWEEP = 4000

#: The pieces a swept path is built from.
_PIECES = (
    "a",
    "b",
    "Ab",
    "/",
    "\\",
    ".",
    "..",
    ":",
    "C:",
    "c:",
    "x",
    "worktrees",
    "\u03a3",
    "\u0130",
    " ",
)


class _Rng:
    """A 64-bit linear congruential generator (Knuth's MMIX constants),
    its top 32 bits drawn; the native test runs the same."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self, bound: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.x >> 32) % bound


def _path(rng: _Rng) -> str:
    return "".join(_PIECES[rng.next(len(_PIECES))] for _ in range(rng.next(7)))


def _host(name: str) -> Any:
    return posixpath if name == "posix" else ntpath


def _raised(e: BaseException) -> dict[str, Any]:
    text = str(e) if isinstance(e, ValueError) else ""
    return {"raised": [type(e).__name__, text]}


def _call(f: str, args: list[Any]) -> Any:
    """The reference's answer, or what it raised."""
    try:
        if f == "legacy_asks":
            return mr.legacy_asks(rule=args[0], same_name=args[1], compatible=args[2])
        if f == "same_path":
            return mr._same_path(args[0], args[1])
        if f == "flavour":
            return "nt" if mr._flavour(args[0]) is ntpath else "posix"
        if f == "path_under":
            return mr._path_under(args[0], *args[1])
        if f == "basename":
            return mr._basename(args[0])
        if f == "same_number":
            keys = [None if k is None else mr.ObjectKey(*k) for k in args]
            return mr._same_number(keys[0], keys[1])
        if f == "rebase":
            cwd = args[3]

            def getcwd() -> str:
                if cwd is None:
                    raise FileNotFoundError(2, "No such file or directory")
                return str(cwd)

            with mock.patch("os.getcwd", getcwd):
                return mr._rebase(args[0], args[1], args[2])
        if f == "common_by_pointer":
            fake = types.SimpleNamespace(path=_host(args[0]))
            with mock.patch.object(mr, "os", fake):
                return mr._common_by_pointer(args[1], args[2])
        if f == "dir_key":
            with mock.patch.object(mr, "_DIR_PATHS", _host(args[0])):
                return mr._dir_key(args[1])
        if f == "dir_below":
            with mock.patch.object(mr, "_DIR_PATHS", _host(args[0])):
                return mr._dir_below(args[1], args[2])
    except (ValueError, OSError) as e:
        return _raised(e)
    raise AssertionError(f)


def _text(t: str) -> Any:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in t):
        return {"c": [ord(c) for c in t]}
    if "\\" in t:
        return {"b": t.split("\\")}
    return t


def _spelled(v: Any) -> Any:
    if isinstance(v, str):
        return _text(v)
    if isinstance(v, list):
        return [_spelled(x) for x in v]
    return v


def _nt_paths(f: str, args: list[Any]) -> list[str]:
    """The arguments of a case that a release's ``ntpath`` reads."""
    texts = [a for a in args if isinstance(a, str)]
    if f == "rebase":
        return texts
    if f in ("common_by_pointer", "dir_key", "dir_below"):
        return texts[1:] if args[0] == "nt" else []
    if f == "path_under":
        return [args[0], *args[1]] if mr._flavour(args[0]) is ntpath else []
    return []


def _short_unc(p: str) -> bool:
    """Whether `p` (backslashes only) begins with two separators but names
    no server and share: an empty server (a third separator follows) or
    share, or no separator after the server. 3.11's `ntpath.splitdrive`
    reads no drive there, or a drive ending in a separator, after which
    its `ntpath.join` writes another; from 3.12 `ntpath.splitroot` reads
    the whole head as a UNC drive and `join` adds none."""
    if p[:2] != "\\\\":
        return False
    server, _, rest = p[2:].partition("\\")
    share = rest.partition("\\")[0]
    return not (server and share)


def _release_bound(f: str, args: list[Any]) -> bool:
    """Whether a release reads the case's ``ntpath`` input otherwise."""
    for p in _nt_paths(f, args):
        head = p[:3].replace("/", "\\")
        one_sep = head[:1] == "\\" and head[1:2] != "\\"
        if one_sep or "\\:" in p.replace("/", "\\"):
            return True
        if "\\\\?\\" in p.replace("/", "\\") or "\\\\.\\" in p.replace("/", "\\"):
            return True
        if f == "rebase" and not (
            ntpath.isabs(p) and len(head) == 3 and head[1] == ":"
        ):
            return mr._flavour(args[1]) is ntpath or mr._flavour(args[2]) is ntpath
    return False


def _short_unc_join(f: str, args: list[Any]) -> bool:
    """Whether `_path_under` joins through ``ntpath`` with a path that
    `_short_unc` reads: the stable sweep redraws such a triple. The
    fixed cases are routed by `_release_bound` alone: none joins such a
    path, and the other functions read one alike from 3.11 to 3.14."""
    return f == "path_under" and any(
        _short_unc(p.replace("/", "\\")) for p in _nt_paths(f, args)
    )


_KEYS = [None, ["1", "2", None], ["1", "2", "9"], ["1", "3", None], ["2", "2", None]]

_RULES = ["ambiguous", "none", "unique", "", "Ambiguous", "none ", "\u212aone"]

_PATHS = [
    "",
    "/",
    "//",
    "///",
    "/a",
    "/a/",
    "/a//b/",
    "a",
    "a/b",
    "./a",
    "../a",
    "C:",
    "C:\\",
    "C:/",
    "c:\\A\\B\\",
    "C:\\a\\..\\..\\b",
    "C:a",
    "1:\\a",
    "\\\\srv\\share\\x",
    "\\\\srv\\share",
    "\\a",
    "/a\\b",
    "a\\b",
    "\\\\?\\C:\\x",
    "\\\\.\\dev",
    "\\\\?\\C:\\x\\..\\y",
    "\\:\\x",
    "/:x",
    "D:\\\u03a3\\\u0130",
    "/w/\u03a3",
    "/a/\ud800",
]


def _cases() -> Iterator[tuple[str, list[Any]]]:
    for rule in _RULES:
        for same_name in (False, True):
            for compatible in (False, True):
                yield "legacy_asks", [rule, same_name, compatible]
    for a in _KEYS:
        for b in _KEYS:
            yield "same_number", [a, b]
    for p in _PATHS:
        yield "flavour", [p]
        yield "basename", [p]
        for host in ("posix", "nt"):
            yield "dir_key", [host, p]
    pairs = [
        ("/a", "/a/"),
        ("/a", "/a//"),
        ("/", "//"),
        ("C:\\", "C:"),
        ("C:\\a", "C:\\a\\"),
        ("C:\\a", "c:\\a"),
        ("a\\", "a/"),
        ("", "/"),
        ("", ""),
        ("/a\\", "/a"),
    ]
    for left, right in pairs:
        yield "same_path", [left, right]
    for base, parts in [
        ("/a", ["b"]),
        ("/a/", ["b", "c"]),
        ("/a", ["/b", "c"]),
        ("/a", []),
        ("", ["b"]),
        ("/a", ["", "b"]),
        ("/a", ["C:\\x"]),
        ("C:\\a", ["b"]),
        ("C:\\a", ["b", "D:\\c"]),
        ("C:\\a", ["c:x"]),
        ("C:\\a", ["\\b"]),
        ("C:", ["b"]),
        ("\\\\srv\\share", ["x"]),
        ("a\\b", ["c/d"]),
        ("C:\\a", ["\u03a3:x"]),
        ("\u03a3:\\a", ["\u03c3:x"]),
    ]:
        yield "path_under", [base, parts]
    cwd = "/w/cwd"
    for path, root, target in [
        ("/v/a/b", "/v", "/t"),
        ("/v/a/b", "/v/", "/t/"),
        ("/v", "/v", "/t"),
        ("/v/a/../b", "/v", "/t"),
        ("/x/a", "/v", "/t"),
        ("/v/a/b", "/v", "C:\\t"),
        ("C:\\v\\a\\b", "C:\\v", "/t"),
        ("C:\\v\\a\\b", "C:\\v", "D:\\t"),
        ("c:\\V\\a", "C:\\v", "/t"),
        ("D:\\v\\a", "C:\\v", "/t"),
        ("", "/v", "/t"),
        ("v/a", "v", "t"),
        ("v/a", "/w/cwd/v", "/t"),
        ("/v/a", "v", "/t"),
        ("/v/a\\b", "/v", "C:\\t"),
        ("\\\\s\\h\\a", "\\\\s\\h", "/t"),
        ("\\\\s\\h\\a", "\\\\S\\H", "/t"),
        ("/v/a", "/v", "t"),
        ("/v/a", "/v", ""),
        ("C:\\v\\a", "C:\\v", "C:"),
        ("C:\\v\\\u03a3", "C:\\v", "/t"),
        ("\\v\\a", "\\v", "/t"),
        ("/v/a", "/v", "\\t"),
        ("\\\\?\\C:\\v\\..\\a", "\\\\?\\C:\\v", "/t"),
        ("\\:\\a", "\\:", "/t"),
        ("/C:b", "a\\x", ".t."),
        ("\\.:w", "a\\b", "c:"),
    ]:
        yield "rebase", [path, root, target, cwd]
    for path, root, target in [
        ("v/a", "v", "/t"),
        ("/v/a", "/v", "/t"),
        ("", "v", "/t"),
    ]:
        yield "rebase", [path, root, target, None]
    for host in ("posix", "nt"):
        for kind in [None, "linked", "primary", "bare", "Linked", "other"]:
            for git_dir in [
                None,
                "",
                "/r/.git/worktrees/w",
                "/r/.git/worktrees/w/",
                "/r/.git/Worktrees/w",
                "/r/.git",
                "/r/.git//",
                "C:\\r\\.git\\worktrees\\w",
                "worktrees/w",
                "w",
                "/",
            ]:
                yield "common_by_pointer", [host, kind, git_dir]
        for here, root in [
            ("/a/b", "/a"),
            ("/a/b", "/a/"),
            ("/a/b", "/a//"),
            ("/a", "/a"),
            ("/ab", "/a"),
            ("/a/b", "/"),
            ("/a/b", ""),
            ("/", ""),
            ("", ""),
            ("c:\\a\\b", "c:\\a"),
            ("c:\\a\\b", "c:\\"),
            ("c:\\a\\b", "c:\\a\\\\"),
            ("c:\\a", "c:\\a"),
            ("\\a", ""),
        ]:
            yield "dir_below", [host, here, root]


def _answer_line(v: Any) -> str:
    """One answer as the sweep digests it."""
    if isinstance(v, bool):
        return "t" if v else "f"
    if v is None:
        return "n"
    if isinstance(v, dict):
        cls, text = v["raised"]
        return "e:" + cls + ":" + text
    return "s:" + str(v)


def _sweep(nt: bool, stable: bool = False) -> tuple[int, str]:
    """Swept cases: three paths drawn by `_Rng`, and every function
    asked of them (`_sweep_cases`). With ``nt`` false only paths that
    hold no backslash and no drive are drawn (redrawn until so). With
    ``stable`` true `_rebase` is not asked and a triple is redrawn until
    `_release_bound` passes every other case asked of it, so its answers
    are the same under every release."""
    rng = _Rng(_SEED + int(nt) + int(stable))
    out: list[str] = []
    for _ in range(_SWEEP):
        while True:
            a, b, c = _path(rng), _path(rng), _path(rng)
            if stable and any(
                _release_bound(f, args) or _short_unc_join(f, args)
                for f, args in _sweep_cases(a, b, c)
                if f != "rebase"
            ):
                continue
            if nt or all(mr._flavour(p) is posixpath for p in (a, b, c)):
                break
        cases = _sweep_cases(a, b, c)
        if stable:
            cases = [(f, args) for f, args in cases if f != "rebase"]
        out.extend(_answer_line(_call(f, args)) for f, args in cases)
    data = "\n".join(out).encode("utf-8", "surrogatepass")
    return _SWEEP, hashlib.sha256(data).hexdigest()


def _sweep_cases(a: str, b: str, c: str) -> list[tuple[str, list[Any]]]:
    host_nt = ["nt" if mr._flavour(a) is ntpath else "posix"]
    return [
        ("same_path", [a, b]),
        ("flavour", [a]),
        ("path_under", [a, [b, c]]),
        ("basename", [a]),
        ("rebase", [a, b, c, "/w/cwd"]),
        ("common_by_pointer", [*host_nt, "linked", a]),
        ("dir_key", [*host_nt, a]),
        ("dir_below", [*host_nt, a, b]),
    ]


def _lines(python: bool) -> bytes:
    rows: list[str] = []
    for f, args in _cases():
        if _release_bound(f, args) != python:
            continue
        row = {"f": f, "a": _spelled(args), "o": _spelled(_call(f, args))}
        rows.append(json.dumps(row, sort_keys=True))
    sweeps = [(True, False)] if python else [(False, False), (True, True)]
    for nt, stable in sweeps:
        n, digest = _sweep(nt, stable)
        digest_row: dict[str, Any] = {
            "f": "sweep",
            "seed": _SEED + int(nt) + int(stable),
            "n": n,
            "flavour": nt,
            "o": digest,
        }
        if stable:
            digest_row["stable"] = True
        rows.append(json.dumps(digest_row, sort_keys=True))
    return ("\n".join(rows) + "\n").encode()


def generate() -> dict[str, bytes]:
    return {"membership_records_paths.jsonl": _lines(False)}


def generate_python() -> dict[str, bytes]:
    return {"membership_records_paths_python.jsonl": _lines(True)}
