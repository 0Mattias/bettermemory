"""Fixtures: the path helpers and the legacy ask rule of
`membership_records` (unit 1: `legacy_asks`, `_same_path`, `_flavour`,
`_path_under`, `_rebase`, `_basename`, `_same_number`,
`_common_by_pointer`, `_dir_key`, `_dir_below`).

Three files, one JSON object per line, keys sorted, ASCII:

- ``membership_records_paths.jsonl``, not gated: every case whose answer
  is the same under CPython 3.11.15, 3.12.13 and 3.13.13 (checked by
  running this generator under each) and on every host, and the posix
  digest row.
- ``membership_records_paths_python.jsonl``, gated on the Python release:
  the cases `_release_bound` picks by their input, those where a path
  read with ``ntpath`` starts with one separator, or with a separator
  then a colon, or holds ``\\\\?\\`` or ``\\\\.\\``: 3.11 and 3.12 read
  such a root, an absolute path and a drive otherwise than 3.13 does.
- ``membership_records_paths_platform.jsonl``, gated on the platform and
  the Python release: the cases `_host_bound` picks by their input, and
  both ``ntpath`` digest rows. On a Windows host CPython's ``ntpath``
  asks the OS where on a POSIX host it computes: `normcase` lowers
  through ``LCMapStringEx``, which leaves U+0130 as it is where
  `str.lower` writes ``i`` and U+0307, and lowers each letter alone, so
  not to the final sigma U+03C2 `str.lower` writes at the end of a
  word; `abspath`, which `relpath` calls, goes through
  ``_getfullpathname``, which reads the process's own drive and folder
  and not the patched ``os.getcwd``.
  `normpath` and `splitroot` (and through it `join`, `split`, `dirname`
  and `basename`) are native there too; the Windows parity run of
  2026-11-20 found them answering as the Python forms on every row it
  compared. `isabs` is Python on every host.

A case is ``{"f": function, "a": [args], "o": output}``. The host's path
module, where the function reads one (`_common_by_pointer`, `_dir_key`,
`_dir_below`), is the first argument, ``"posix"`` or ``"nt"``. `_rebase`
runs with ``os.getcwd`` answering ``cwd`` (the last argument) or, where
that is null, raising FileNotFoundError; ``nt._getfullpathname`` is
asked only on Windows, in rows of the platform file. An ObjectKey is ``[device,
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
UTF-8 (surrogates passed). The ungated file holds a posix sweep. The
platform file holds an ``ntpath`` sweep marked ``"stable": true``, which
leaves out `_rebase` and draws only triples whose other cases `_release_bound` passes and
whose `_path_under` joins no path `_short_unc` reads (two leading
separators naming no server and share, which 3.11's `ntpath.join`
follows with a separator of its own), and the full ``ntpath`` sweep,
those calls with it. Both draw U+03A3 and U+0130 into paths `_dir_key`
folds with ``ntpath``, so both follow the host. Recorded paths are
synthetic.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
import struct
import types
from collections.abc import Iterator
from typing import Any, cast
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


def _host_bound(f: str, args: list[Any]) -> bool:
    """Whether CPython's own answer to the case follows the host: on
    Windows `ntpath.normcase` lowers through ``LCMapStringEx``, which
    lowers past ASCII otherwise than `str.lower` (`_dir_key` under
    ``ntpath`` of a path holding a letter past ASCII), and `relpath`
    makes both paths absolute through ``_getfullpathname`` and compares
    their parts through that `normcase` (`_rebase` from a root read with
    ``ntpath``)."""
    if f == "dir_key":
        return args[0] == "nt" and not str(args[1]).isascii()
    if f == "rebase":
        return mr._flavour(args[1]) is ntpath
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


def _lines(python: bool | None) -> bytes:
    """The rows of one file: the ungated (``python`` False), the
    release-bound (True) or the host-bound (None)."""
    rows: list[str] = []
    for f, args in _cases():
        if python is None:
            if not _host_bound(f, args):
                continue
        elif _host_bound(f, args) or _release_bound(f, args) != python:
            continue
        row = {"f": f, "a": _spelled(args), "o": _spelled(_call(f, args))}
        rows.append(json.dumps(row, sort_keys=True))
    sweeps = [(True, False), (True, True)] if python is None else []
    if python is False:
        sweeps = [(False, False)]
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


def generate_platform() -> dict[str, bytes]:
    return {"membership_records_paths_platform.jsonl": _lines(None)}


# ---------------------------------------------------------------------------
# Units 2 and 3: the manifest's JSON and match, and the checkout records.
#
# ``membership_records_records.jsonl``, not gated: one JSON object per
# line, keys sorted, ASCII (a lone surrogate written as JSON's escape).
#
# - ``{"f": "from_json", "a": raw, "o": out}``: `Manifest.from_json(raw)`,
#   ``out`` its `to_json()`, null, or ``{"raised": class}`` (the class
#   alone: the text follows the release).
# - ``{"f": "match", "a": [a, b], "o": bits, "r": repr}``:
#   `manifest_match` of two manifests (their `to_json()` or null), the
#   answer's IEEE bits in hex and its repr.
# - ``{"f": "checkout", "a": {"token", "status", "key"}, "o": {...}}``:
#   `token_superseded`, `lineage` and `token_refutes` of each found token.
# - ``{"f": "views", "a": [[machine, order], ...], "m": machine, "o":
#   {"on": [index], "newest": index}}``: `views_on` and `newest_view`.
# - ``{"f": "live_key", "a": [checkout, view, checkout, view], "o":
#   bool}``: whether `_live_key` of the two pairs is equal.
# - ``{"f": "found_token", "a": [status, token, written], "o": text}``.
# - ``{"f": "match_sweep", "seed", "n", "o"}``: `_RECORDS_SWEEP` pairs of
#   manifests drawn by `_Rng` (`_manifest`), each answer's bits as 16 hex
#   digits, one per line, and ``o`` the SHA-256 of the lines.
# ---------------------------------------------------------------------------

from bettermemory import checkout_token as ct  # noqa: E402

_RECORDS_SEED = 20261121
_RECORDS_SWEEP = 3000
_NONCE = "0123456789ABC"
_TOKEN = "0123456789ABCDEFGHJKMNPQRS"
_OTHER = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"


def _remint(token: str, device: str, inode: str) -> str:
    return _NONCE + ct._link(ct.Lineage(token, device, inode), _NONCE)


def _bits(x: float) -> str:
    return struct.pack(">d", x).hex()


def _from_json_cases() -> list[Any]:
    big = "9" * 4300
    return [
        None,
        1,
        "m",
        [],
        ["name", "m"],
        {},
        {"name": 1},
        {"name": None},
        {"name": ["m"]},
        {"Name": "m"},
        {"name": "m"},
        {"name": ""},
        {"name": "m", "entries": None},
        {"name": "m", "entries": False},
        {"name": "m", "entries": 0},
        {"name": "m", "entries": 0.0},
        {"name": "m", "entries": -0.0},
        {"name": "m", "entries": ""},
        {"name": "m", "entries": []},
        {"name": "m", "entries": {}},
        {"name": "m", "entries": True},
        {"name": "m", "entries": 1},
        {"name": "m", "entries": 2.5},
        {"name": "m", "entries": float("nan")},
        {"name": "m", "entries": "ab"},
        {"name": "m", "entries": "a"},
        {"name": "m", "entries": {"a1": 0, "b2": 1}},
        {"name": "m", "entries": {"ab": 0}},
        {"name": "m", "entries": {"abc": 0}},
        {"name": "m", "entries": [["a", 1]]},
        {"name": "m", "entries": [["a", 1], ["a", 1], ["b", 2]]},
        {"name": "m", "entries": [["a"]]},
        {"name": "m", "entries": [[]]},
        {"name": "m", "entries": [["a", 1, 2]]},
        {"name": "m", "entries": ["a7"]},
        {"name": "m", "entries": ["a"]},
        {"name": "m", "entries": ["abc"]},
        {"name": "m", "entries": [""]},
        {"name": "m", "entries": ["ab"]},
        {"name": "m", "entries": [{"p": 1, "3": 2}]},
        {"name": "m", "entries": [{"p": 1}]},
        {"name": "m", "entries": [1]},
        {"name": "m", "entries": [None]},
        {"name": "m", "entries": [1.5]},
        {"name": "m", "entries": [True]},
        {"name": "m", "entries": [["a", 1], 5, ["b", "x"]]},
        {"name": "m", "entries": [["a", "x"], 5]},
        {"name": "m", "entries": [[1, 2]]},
        {"name": "m", "entries": [[2.5, 2]]},
        {"name": "m", "entries": [[1e16, 2]]},
        {"name": "m", "entries": [[True, 2]]},
        {"name": "m", "entries": [[None, 2]]},
        {"name": "m", "entries": [[[1, "a"], 2]]},
        {"name": "m", "entries": [[{"k": [None, True]}, 2]]},
        {"name": "m", "entries": [[float("nan"), 2]]},
        {"name": "m", "entries": [[float("-inf"), 2]]},
        {"name": "m", "entries": [["p", True]]},
        {"name": "m", "entries": [["p", False]]},
        {"name": "m", "entries": [["p", 2.9]]},
        {"name": "m", "entries": [["p", -2.9]]},
        {"name": "m", "entries": [["p", -0.0]]},
        {"name": "m", "entries": [["p", 1e300]]},
        {"name": "m", "entries": [["p", float("inf")]]},
        {"name": "m", "entries": [["p", float("nan")]]},
        {"name": "m", "entries": [["p", None]]},
        {"name": "m", "entries": [["p", [1]]]},
        {"name": "m", "entries": [["p", {}]]},
        {"name": "m", "entries": [["p", " 1_0 "]]},
        {"name": "m", "entries": [["p", "1__0"]]},
        {"name": "m", "entries": [["p", "_1"]]},
        {"name": "m", "entries": [["p", "+5"]]},
        {"name": "m", "entries": [["p", "-0"]]},
        {"name": "m", "entries": [["p", "007"]]},
        {"name": "m", "entries": [["p", ""]]},
        {"name": "m", "entries": [["p", " "]]},
        {"name": "m", "entries": [["p", "1.0"]]},
        {"name": "m", "entries": [["p", "0x10"]]},
        {"name": "m", "entries": [["p", "٣٤"]]},
        {"name": "m", "entries": [["p", " 12　"]]},
        {"name": "m", "entries": [["p", "²"]]},
        {"name": "m", "entries": [["p", big]]},
        {"name": "m", "entries": [["p", big + "9"]]},
        {"name": "m", "entries": [["p", "-" + big]]},
        {"name": "m", "entries": [["p", 10**40]]},
        {"name": "m", "entries": [["p", -(10**40)]]},
        {"name": "\ud800", "entries": [["\udfff", 1]]},
        {"name": "m", "entries": [["\U0001f600", 1]]},
        {"name": "m", "entries": ["\U0001f6001"]},
        {"name": "m", "name2": "x", "entries": [["a", 1]], "other": [1]},
    ]


def _manifest_json(name: str, entries: list[tuple[str, int]]) -> dict[str, Any]:
    return mr.Manifest(name, tuple(entries)).to_json()


def _match_cases() -> list[tuple[Any, Any]]:
    m = _manifest_json
    e3 = [("a", 1), ("b", 2), ("c", 3)]
    return [
        (None, None),
        (None, m("x", e3)),
        (m("x", e3), None),
        (m("x", []), m("x", [])),
        (m("x", e3), m("y", e3)),
        (m("x", e3), m("x", e3)),
        (m("x", e3), m("x", [])),
        (m("x", []), m("x", e3)),
        (m("x", e3), m("x", e3[:1])),
        (m("x", e3), m("x", [("a", 1), ("b", 3)])),
        (m("x", e3), m("x", [("d", 1)])),
        (m("x", e3 + e3), m("x", e3[:2])),
        (m("x", [("a", 1)] * 5), m("x", [("a", 1), ("b", 1)])),
        (
            m("x", [(str(i), i) for i in range(10)]),
            m("x", [(str(i), i) for i in range(3, 10)]),
        ),
        (
            m("x", [(str(i), 0) for i in range(49)]),
            m("x", [(str(i), 0) for i in range(1, 50)]),
        ),
        (m("x", [("a", 10**30)]), m("x", [("a", 10**30)])),
        (m("x", [("a", 1)]), m("x", [("a", -1)])),
        (m("", [("a", 1)]), m("", [("a", 1)])),
    ]


def _checkout(**kw: Any) -> mr.Checkout:
    base: dict[str, Any] = {
        "id": "k1",
        "project": "p1",
        "machine": "m1",
        "kind": "primary",
        "object_key": mr.ObjectKey("10", "20", "30"),
        "common_object_key": None,
        "git_dir": "/r/.git",
        "token": _TOKEN,
        "token_status": "own",
        "stamps": (),
        "last_head": "h1",
        "branch": "main",
        "remotes": (),
        "manifest": None,
        "legacy": False,
        "retired": False,
        "registered": "t0",
    }
    base.update(kw)
    return mr.Checkout(**base)


def _key_json(key: mr.ObjectKey | None) -> Any:
    return None if key is None else [key.device, key.inode, key.birth]


def _checkout_cases() -> list[dict[str, Any]]:
    k = mr.ObjectKey
    re1 = _remint(_TOKEN, "10", "20")
    found = [
        None,
        "",
        _TOKEN,
        _OTHER,
        re1,
        _remint(_TOKEN, "10", "21"),
        _remint(_OTHER, "10", "20"),
        re1.lower(),
        re1[:-1],
        "x",
        "\ud800",
    ]
    cases = []
    for token in [None, "", _TOKEN, "not a token", _TOKEN.lower()]:
        for status in ["own", "superseded", "Superseded", "written", ""]:
            for key in [
                k("10", "20", "30"),
                None,
                k("10", "x"),
                k("-1", "007"),
                k("١", "2"),
                k("1\n", "2"),
                k("", "2"),
            ]:
                if (token, status) != (_TOKEN, "own") and key != k("10", "20", "30"):
                    continue
                cases.append(
                    {
                        "token": token,
                        "status": status,
                        "key": _key_json(key),
                        "found": found,
                    }
                )
    return cases


def _checkout_row(case: dict[str, Any]) -> dict[str, Any]:
    key = case["key"]
    ck = _checkout(
        token=case["token"],
        token_status=case["status"],
        object_key=None if key is None else mr.ObjectKey(*key),
    )
    lin = ck.lineage
    refutes = []
    for f in case["found"]:
        try:
            out: Any = ck.token_refutes(f)
        except Exception as e:  # noqa: BLE001
            out = {"raised": type(e).__name__}
        refutes.append([f, out])
    return {
        "f": "checkout",
        "a": {"token": case["token"], "status": case["status"], "key": key},
        "o": {
            "superseded": ck.token_superseded,
            "lineage": None if lin is None else list(lin),
            "refutes": refutes,
        },
    }


def _views_cases() -> list[tuple[list[tuple[str, int]], str]]:
    return [
        ([], "m1"),
        ([("m1", 1)], "m1"),
        ([("m1", 1)], "m2"),
        ([("m1", 3), ("m2", 1), ("m1", 1), ("m1", 2)], "m1"),
        ([("m1", 2), ("m1", 2), ("m2", 2)], "m1"),
        ([("m2", 5), ("m1", 5), ("m1", 4), ("m2", 5)], "m2"),
        ([("m1", -1), ("m1", -(2**63)), ("m1", 2**63 - 1), ("m1", 2**63 - 1)], "m1"),
        ([("m1", 0), ("M1", 0), ("m1 ", 1)], "m1"),
    ]


def _views_row(views: list[tuple[str, int]], machine: str) -> dict[str, Any]:
    vs = tuple(
        mr.View("k1", m, f"/p{i}", "t0", "t1", o) for i, (m, o) in enumerate(views)
    )
    ck = _checkout(views=vs)
    on = [next(i for i, w in enumerate(vs) if w is v) for v in ck.views_on(machine)]
    nv = ck.newest_view
    newest = None if nv is None else next(i for i, w in enumerate(vs) if w is nv)
    return {
        "f": "views",
        "a": [list(v) for v in views],
        "m": machine,
        "o": {"on": on, "newest": newest},
    }


def _ck_json(ck: mr.Checkout) -> dict[str, Any]:
    return {
        "id": ck.id,
        "project": ck.project,
        "machine": ck.machine,
        "kind": ck.kind,
        "object_key": _key_json(ck.object_key),
        "common_object_key": _key_json(ck.common_object_key),
        "token": ck.token,
        "token_status": ck.token_status,
        "last_head": ck.last_head,
        "stamps": [list(s) for s in ck.stamps],
        "branch": ck.branch,
        "registered": ck.registered,
    }


def _view_json(v: mr.View) -> list[Any]:
    return [v.checkout, v.machine, v.path, v.first_seen, v.last_seen, v.order]


def _live_key_rows() -> list[dict[str, Any]]:
    st = mr.InitStamp
    s1 = (st("description", 1, 2, 3, 4),)
    base = _checkout(stamps=s1)
    v = mr.View("k1", "m1", "/p", "t0", "t1", 1)
    variants: list[tuple[mr.Checkout, mr.View]] = [
        (base, v),
        (_checkout(stamps=s1), mr.View("k2", "m1", "/p", "t9", "t9", 9)),
        (
            _checkout(
                stamps=s1,
                project="p2",
                machine="m9",
                git_dir="/x",
                branch="b",
                remotes=("r",),
                legacy=True,
                retired=True,
                registered="t5",
            ),
            v,
        ),
        (_checkout(stamps=s1, id="k2"), v),
        (_checkout(stamps=s1, kind="linked"), v),
        (_checkout(stamps=s1, object_key=mr.ObjectKey("10", "20", None)), v),
        (_checkout(stamps=s1, object_key=None), v),
        (_checkout(stamps=s1, common_object_key=mr.ObjectKey("10", "20", "30")), v),
        (_checkout(stamps=s1, token=None), v),
        (_checkout(stamps=s1, token_status="superseded"), v),
        (_checkout(stamps=s1, token_status="written"), v),
        (_checkout(stamps=s1, last_head=None), v),
        (_checkout(stamps=()), v),
        (_checkout(stamps=(st("description", 1, 2, 3, 5),)), v),
        (_checkout(stamps=s1 + s1), v),
        (base, mr.View("k1", "m2", "/p", "t0", "t1", 1)),
        (base, mr.View("k1", "m1", "/p/", "t0", "t1", 1)),
    ]
    rows = []
    for k2, v2 in variants:
        rows.append(
            {
                "f": "live_key",
                "a": [_ck_json(base), _view_json(v), _ck_json(k2), _view_json(v2)],
                "o": mr._live_key(base, v) == mr._live_key(k2, v2),
            }
        )
    return rows


def _found_token_rows() -> list[dict[str, Any]]:
    rows = []
    for status in ["own", "superseded", "written"]:
        for token in [None, "", "T"]:
            for written in [None, "", "W"]:
                loc = types.SimpleNamespace(token=token, token_written=written)
                ck = _checkout(token_status=status)
                rows.append(
                    {
                        "f": "found_token",
                        "a": [status, token, written],
                        "o": mr._found_token(ck, cast("mr.Location", loc)),
                    }
                )
    return rows


def _manifest(rng: _Rng) -> mr.Manifest | None:
    if rng.next(16) == 0:
        return None
    name = "ab"[rng.next(2)] if rng.next(8) == 0 else "n"
    entries = tuple(("pqrs"[rng.next(4)], rng.next(3)) for _ in range(rng.next(9)))
    return mr.Manifest(name, entries)


def _match_sweep() -> str:
    rng = _Rng(_RECORDS_SEED)
    lines = []
    for _ in range(_RECORDS_SWEEP):
        a, b = _manifest(rng), _manifest(rng)
        lines.append(_bits(mr.manifest_match(a, b)))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def generate_records() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for given in _from_json_cases():
        # The answer is the reference's on the value the file spells:
        # keys sorted, as the reader meets them.
        raw = json.loads(json.dumps(given, sort_keys=True))
        try:
            m = mr.Manifest.from_json(raw)
            out: Any = None if m is None else m.to_json()
        except Exception as e:  # noqa: BLE001
            out = {"raised": type(e).__name__}
        rows.append({"f": "from_json", "a": raw, "o": out})
    for a, b in _match_cases():
        x = mr.manifest_match(mr.Manifest.from_json(a), mr.Manifest.from_json(b))
        rows.append({"f": "match", "a": [a, b], "o": _bits(x), "r": repr(x)})
    rows.extend(_checkout_row(c) for c in _checkout_cases())
    rows.extend(_views_row(v, m) for v, m in _views_cases())
    rows.extend(_live_key_rows())
    rows.extend(_found_token_rows())
    rows.append(
        {
            "f": "match_sweep",
            "seed": _RECORDS_SEED,
            "n": _RECORDS_SWEEP,
            "o": _match_sweep(),
        }
    )
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    return {"membership_records_records.jsonl": text.encode()}


# ---------------------------------------------------------------------------
# Unit 4: the portable identity.
#
# ``membership_records_identity.jsonl``, not gated, and
# ``membership_records_identity_python.jsonl``, gated on the Python
# release: one JSON object per line, keys sorted, ASCII. No row holds a lone
# surrogate or a backslash (the golden checks refuse both in a plain
# string); the kills test covers them.
#
# - ``{"f": "identity", "a": raw, "o": out}``: `PortableIdentity.from_json`
#   of the value ``raw`` spells, ``out`` ``{"raised": [class, text]}`` or
#   ``{"json": to_json(), "digest": digest()}``. A row whose name or
#   witnesses hold a character past ASCII goes in the gated file:
#   `str.strip`, `str.lower` and the `isprintable` of `repr` follow the
#   release's Unicode tables.
# - ``{"f": "identity_sweep", "seed", "n", "o"}`` (ungated):
#   `_IDENTITY_SWEEP` documents drawn by `_Rng` (`_identity_doc`), each
#   answered as the digest and the parent (``<digest>|<parent>``) or
#   ``!<text>`` of the ValueError, one per line, ``o`` the SHA-256 of the
#   lines.
# ---------------------------------------------------------------------------

_IDENTITY_SEED = 20261122
_IDENTITY_SWEEP = 2000
_HEX40 = "0123456789abcdef" * 2 + "01234567"
_HEX64 = "fedcba9876543210" * 4
_ABSENT = object()
_SW_NAMES = ["", "p", "Proj", "a b"]
_SW_WITNESSES = [
    "a" * 40,
    "b" * 40,
    "0" * 64,
    " " + "c" * 40,
    "A" * 40,
    "a" * 39,
    "f" * 41,
    "1" * 64 + "\n",
    "a" * 40,
]
_SW_REMOTES = ["r1", "local:x", "r0", "r1", "local:"]
_SW_FILES = ["f2", "f1", "f2"]
_SW_FLAGS: list[Any] = [_ABSENT, True, False, 1]
_SW_PARENTS: list[Any] = [_ABSENT, None, 0, 3, -1, True]


def _identity_doc(rng: _Rng) -> dict[str, Any]:
    doc: dict[str, Any] = {"name": _SW_NAMES[rng.next(4)]}
    for key, pool in (("remotes", _SW_REMOTES), ("file_ids", _SW_FILES)):
        if rng.next(4):
            doc[key] = [pool[rng.next(len(pool))] for _ in range(rng.next(4))]
    if rng.next(4):
        doc["witnesses"] = [
            _SW_WITNESSES[rng.next(len(_SW_WITNESSES))] for _ in range(rng.next(5))
        ]
    for key in ("folder", "boundary"):
        v = _SW_FLAGS[rng.next(4)]
        if v is not _ABSENT:
            doc[key] = v
    p = _SW_PARENTS[rng.next(6)]
    if p is not _ABSENT:
        doc["parent"] = p
    return doc


def _identity_sweep() -> str:
    rng = _Rng(_IDENTITY_SEED)
    lines = []
    for _ in range(_IDENTITY_SWEEP):
        try:
            p = mr.PortableIdentity.from_json(_identity_doc(rng))
            lines.append(f"{p.digest()}|{p.parent}")
        except ValueError as e:
            lines.append(f"!{e}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _identity_cases() -> list[Any]:
    ok = {"name": "p"}
    cases: list[Any] = [None, True, 1, 1.5, "p", [], [["name", "p"]], {}, ok]
    v: Any
    # The name: missing, every wrong type, empty.
    for v in [None, True, 0, 1.0, "", [], ["p"], {"p": 1}, " ", "P", "\x00"]:
        cases.append({"name": v})
    # The parent: every wrong type, the bounds, big ints and bools.
    for v in [
        None,
        True,
        False,
        0,
        1,
        -1,
        2**63,
        2**64 + 1,
        -(2**70),
        10**400,
        1.0,
        0.0,
        "0",
        [0],
        {},
    ]:
        cases.append({"name": "p", "parent": v})
    # Each list: every wrong type, an empty or non-str element.
    for key in ("remotes", "witnesses", "file_ids"):
        for v in [
            None,
            True,
            0,
            "a",
            {},
            {"a": 1},
            [""],
            ["a", ""],
            [1],
            [None],
            [["a"]],
            [True],
            [],
        ]:
            cases.append({"name": "p", key: v})
    for key in ("folder", "boundary"):
        for v in [None, True, False, 0, 1, "true", [], 1.0]:
            cases.append({"name": "p", key: v})
    # The order of the checks: each pair of malformed fields.
    bad = {
        "name": "",
        "parent": -1,
        "remotes": 1,
        "witnesses": ["a" * 39],
        "file_ids": [""],
        "folder": 1,
        "boundary": 1,
    }
    keys = list(bad)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            doc = {"name": "p", a: bad[a], b: bad[b]}
            cases.append(doc)
    cases.append({**bad, "witnesses": [1]})
    # Witnesses: lengths, case, spaces, other letters, duplicates.
    for n in (0, 1, 39, 40, 41, 63, 64, 65, 80, 128):
        cases.append({"name": "p", "witnesses": [(_HEX64 * 2)[:n]]})
    for w in [
        _HEX40.upper(),
        " " + _HEX40 + " ",
        "\t" + _HEX40 + "\n",
        _HEX40 + "\x1f",
        _HEX40 + "\x0b\x0c\r",
        _HEX40[:39] + "g",
        _HEX40[:20] + " " + _HEX40[20:39],
        "x" * 200,
        "'" * 80,
        '"' * 80,
        "'\"" * 50,
        "\x00" * 40,
        _HEX64.upper() + " ",
        _HEX40 + _HEX64[:24],
        _HEX40 + _HEX64[:23],
    ]:
        cases.append({"name": "p", "witnesses": [w]})
    cases.append({"name": "p", "witnesses": [_HEX40, "a" * 39, "b" * 39]})
    many = [f"{i:040x}" for i in range(70)]
    cases.append({"name": "p", "witnesses": many})
    cases.append({"name": "p", "witnesses": many[:3] * 30 + many[3:]})
    cases.append({"name": "p", "witnesses": [_HEX40, _HEX40.upper(), " " + _HEX40]})
    cases.append({"name": "p", "witnesses": many[:64] + [_HEX40[:39]]})
    # Remotes and file ids: local keys, order, duplicates, past ASCII.
    cases.append(
        {
            "name": "p",
            "remotes": [
                "b",
                "local:/x",
                "a",
                "b",
                "local:",
                "Local:x",
                "local",
                " local:x",
                "é",
                "z",
            ],
        }
    )
    cases.append({"name": "p", "file_ids": ["f2", "f1", "f2", "F0", "é", "~"]})
    cases.append(
        {
            "name": "p",
            "remotes": ["local:a"],
            "file_ids": ["x"],
            "folder": True,
            "boundary": True,
            "parent": 7,
            "witnesses": [_HEX64, _HEX40, _HEX64],
        }
    )
    cases.append({"name": "p", "extra": 1, "parent": None, "folder": False})
    # Past ASCII in the name and the witnesses (gated).
    cases.append({"name": "été"})
    cases.append({"name": "\U0001f600"})
    cases.append({"name": "　"})
    for w in [
        "　" + _HEX40 + " ",
        "İ" * 40,
        "İ" * 80,
        "K" * 40,
        "ａ" * 40,
        "é" * 100,
        "\u0085" + _HEX40 + " ",
        "​" + _HEX40,
        "Σ" * 40,
        "\U0001f600" * 80,
        "͸" + _HEX40,
        "­" * 40,
        _HEX40[:39] + "İ",
    ]:
        cases.append({"name": "p", "witnesses": [w]})
    return cases


def _past_ascii(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    texts = [raw.get("name")]
    w = raw.get("witnesses")
    if isinstance(w, list):
        texts.extend(w)
    return any(isinstance(t, str) and not t.isascii() for t in texts)


def _identity_lines(python: bool) -> bytes:
    rows: list[dict[str, Any]] = []
    for raw in _identity_cases():
        if _past_ascii(raw) != python:
            continue
        try:
            p = mr.PortableIdentity.from_json(raw)
            out: Any = {"json": p.to_json(), "digest": p.digest()}
        except Exception as e:  # noqa: BLE001
            out = {"raised": [type(e).__name__, str(e)]}
        rows.append({"f": "identity", "a": raw, "o": out})
    if not python:
        rows.append(
            {
                "f": "identity_sweep",
                "seed": _IDENTITY_SEED,
                "n": _IDENTITY_SWEEP,
                "o": _identity_sweep(),
            }
        )
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    return text.encode()


def generate_identity() -> dict[str, bytes]:
    return {"membership_records_identity.jsonl": _identity_lines(False)}


def generate_identity_python() -> dict[str, bytes]:
    return {"membership_records_identity_python.jsonl": _identity_lines(True)}
