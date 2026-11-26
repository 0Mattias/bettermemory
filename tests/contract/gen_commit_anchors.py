"""Fixtures: commit anchors through their checkout views
(``commit_anchors.jsonl``, not gated; ``commit_anchors_posix.jsonl``,
POSIX only).

The native build reads them in
``native/crates/bettermemory-core/src/commit_anchors.rs``. This pins
`commit_anchors`' `relative_under`, `expected_home` and
`project_commit_anchors`, through them `_paths.inside`,
`attestation_paths.absolute_spelling` and `ClaimAnchor.absolute`.

The process is the generator's: ``os.getcwd()`` answers the header's
``cwd``, ``$HOME`` is the header's ``home`` (or removed where a row's
``home`` is null, with the running user's password entry unknown), and
``pwd.getpwnam`` answers from the header's ``users`` (an unknown name
raises `KeyError`; a name holding NUL raises `ValueError`, as the real
one does). A `ClaimAnchor` is built from ``a``: ``root``, ``rule``,
``rr`` (`recorded_roots`), ``or`` (`origin_root`) and ``t``, a table
path -> [target, rule] standing for `translate` (any other path answers
itself under ``absolute``), absent for None; ``tl`` lists the paths
`translate` was asked, in order.

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "cwd", "home", "users"}``. Every
other line is a case, ``f`` naming it:

- ``"ru"``: `relative_under(p, r)`, ``o`` the answer or null;
- ``"eh"``: `expected_home(p, roots)`, ``o`` a bool;
- ``"pca"``: `project_commit_anchors(paths, anchor)` (``a`` null for
  None), ``o`` ``{"paths", "unmapped"}`` or ``{"raise": class name}``;
- ``"sweep"`` (POSIX file only): `relative_under(p, r)` for every root
  joined from 1 to ``rmax`` of ``alphabet`` and every path joined from 0
  to ``pmax``, in `itertools.product`'s order, roots outer; each answer
  ``-`` for None or ``=`` and the text, joined by newlines and a final
  newline, UTF-8: ``count`` and ``sha256``.

A row reaches `ntpath.relpath` (which asks Windows on Windows, through
`nt._getfullpathname`) or expands a ``~`` path (`ntpath.expanduser`
reads ``USERPROFILE``): such a row goes to the POSIX file. Every other
row answers the same on any host and goes to the file that is not gated.
Whether a row reads the working directory follows the Python release
(3.11 and 3.12 read ``\\a`` as absolute, 3.13 joins it to the working
directory), so it is not recorded; the answers are the same.
"""

from __future__ import annotations

import hashlib
import itertools
import ntpath
import os
import sys
from collections.abc import Callable
from typing import Any
from unittest import mock

from bettermemory import commit_anchors as ca
from bettermemory import verify_anchoring as va

from ._common import jsonl

CWD = "/w/cwd"
HOME = "/hd/u"
USERS = {"alice": "/hd/alice", "root": "/", "empty": "", "slashy": "//srv//x/"}

SWEEP_ALPHABET = ["/", "\\", "a", ".", "..", "C:", "b"]
SWEEP_RMAX = 3
SWEEP_PMAX = 3


class _FakePwd:
    @staticmethod
    def getpwnam(name: str) -> Any:
        if "\x00" in name:
            raise ValueError("embedded null character")
        if name not in USERS:
            raise KeyError(name)
        return type("pw", (), {"pw_dir": USERS[name]})()

    @staticmethod
    def getpwuid(uid: int) -> Any:
        raise KeyError(uid)


class _Seen:
    """What a row asked of the host."""

    def __init__(self) -> None:
        self.nt_relpath = 0
        self.tilde = 0


def _run(home: str | None, fn: Callable[[], Any]) -> tuple[Any, _Seen]:
    seen = _Seen()
    real_relpath = ntpath.relpath
    real_expanduser = os.path.expanduser

    def relpath(path: str, start: str | None = None) -> str:
        seen.nt_relpath += 1
        return real_relpath(path, start)

    def expanduser(path: str) -> str:
        if path.startswith("~"):
            seen.tilde += 1
        return real_expanduser(path)

    def getcwd() -> str:
        return CWD

    env = dict(os.environ)
    env.pop("HOME", None)
    if home is not None:
        env["HOME"] = home
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.dict(sys.modules, {"pwd": _FakePwd}),
        mock.patch.object(ntpath, "relpath", relpath),
        mock.patch.object(os.path, "expanduser", expanduser),
        mock.patch.object(os, "getcwd", getcwd),
    ):
        try:
            return fn(), seen
        except Exception as exc:  # noqa: BLE001 - the class is the answer
            return {"raise": type(exc).__name__}, seen


def _anchor(a: dict[str, Any] | None, asked: list[str]) -> va.ClaimAnchor | None:
    if a is None:
        return None
    table = a.get("t")
    translate = None
    if table is not None:

        def translate(path: str) -> tuple[str, str]:
            asked.append(path)
            target, rule = table.get(path, [path, "absolute"])
            return target, rule

    return va.ClaimAnchor(
        root=a.get("root"),
        rule=a["rule"],
        translate=translate,
        origin_root=a.get("or"),
        recorded_roots=tuple(a.get("rr", ())),
    )


def _ru(path: str, root: str | None) -> tuple[dict[str, Any], _Seen]:
    answer, seen = _run(HOME, lambda: ca.relative_under(path, root))
    return {"f": "ru", "p": path, "r": root, "o": answer}, seen


def _eh(path: str, roots: list[str]) -> tuple[dict[str, Any], _Seen]:
    answer, seen = _run(HOME, lambda: ca.expected_home(path, tuple(roots)))
    return {"f": "eh", "p": path, "roots": roots, "o": answer}, seen


def _pca(
    paths: list[str], a: dict[str, Any] | None, home: str | None = HOME
) -> tuple[dict[str, Any], _Seen]:
    asked: list[str] = []

    def call() -> Any:
        got = ca.project_commit_anchors(tuple(paths), _anchor(a, asked))
        return {"paths": list(got.paths), "unmapped": got.unmapped}

    answer, seen = _run(home, call)
    row = {"f": "pca", "paths": paths, "a": a, "home": home, "o": answer}
    if a is not None and "t" in a:
        row["tl"] = asked
    return row, seen


RU = [
    # posixpath: a root starting with "/".
    ("/a/b", "/a"),
    ("/a", "/a"),
    ("/a/", "/a"),
    ("/a/b/", "/a/"),
    ("/a//b", "/a"),
    ("/a/./b", "/a"),
    ("/a/../a/b", "/a"),
    ("/a/b", "/a/../a"),
    ("/a/b", "/a/./"),
    ("/ab", "/a"),
    ("/a\\b", "/a"),
    ("/a/b\\c", "/a"),
    ("/a/b\\..\\c", "/a"),
    ("/", "/"),
    ("/x", "/"),
    ("//a/b", "/a"),
    ("//a/b", "//a"),
    ("///a/b", "/a"),
    ("/a/b", "///a"),
    ("a/b", "/a"),
    ("", "/"),
    ("/a/b", None),
    ("/a/..b/c", "/a"),
    ("/a/b..", "/a"),
    ("/r/\u00e9/x", "/r/\u00e9"),
    # ntpath: any other root.
    ("C:\\a\\b", "C:\\a"),
    ("C:/a/b", "C:\\a"),
    ("C:\\a\\b\\", "C:\\a\\"),
    ("C:\\a\\b", "C:/a/"),
    ("C:\\a", "C:\\a"),
    ("C:\\x", "C:\\"),
    ("C:\\x", "C:"),
    ("C:a\\b", "C:a"),
    ("D:\\a\\b", "C:\\a"),
    ("C:\\a\\..\\b", "C:\\a"),
    ("C:/a/../b", "C:\\a"),
    ("C:\\ab", "C:\\a"),
    ("c:\\x\\y", "C:\\X"),
    ("C:\\x", "c:"),
    # A UNC root is the fixtures' synthetic share, spelled whole; a path
    # below it is spelled with slashes (the host-fact guard reads two
    # backslashes as a host path).
    ("//s/sh/x/y", "\\\\s\\sh"),
    ("//s/sh/x/", "\\\\s\\sh"),
    ("\\\\s\\sh", "\\\\s\\sh"),
    ("//s/sh2/x", "\\\\s\\sh"),
    ("\\/a", "\\"),
    ("\\/a\\b", "\\"),
    ("\\a\\b", "\\a"),
    ("a\\b", "a"),
    ("a/b/c", "a"),
    ("a/./b", "a"),
    (".", "."),
    ("", "."),
    ("x", "."),
    ("x", ""),
    ("", ""),
    ("C:\\a\\b..\\c", "C:\\a"),
]

EH = [
    ("/a/b", ["/x", "/a"]),
    ("/a", []),
    ("/a/b/", ["/a/b"]),
    ("/a", ["/a/"]),
    ("/ab", ["/a"]),
    ("C:\\a\\b", ["C:/a"]),
    ("C:\\a\\b", ["C:\\a"]),
    ("C:/a/b", ["C:\\a"]),
    ("x", [""]),
    ("", [""]),
    ("/", ["/"]),
    ("~/x", ["~"]),
]

ROOTED = {"root": "/live", "rule": "home_checkout"}


def _pcas() -> list[tuple[list[str], dict[str, Any] | None, str | None]]:
    same = ["/r/a/b", "/r/a//"]
    wide = ["/r/\u00e9/b", "/r/a//"]
    out: list[tuple[list[str], dict[str, Any] | None, str | None]] = [
        (["/x/a", "/x/a", "rel"], None, HOME),
        (
            ["/old/a", "/old/a"],
            {"root": "/live", "rule": "global_record", "rr": ["/old"]},
            HOME,
        ),
        ([], {**ROOTED, "rr": ["/old"]}, HOME),
        (["/r/a/b/c"], {**ROOTED, "rr": same}, HOME),
        (["/r/a/b/c"], {**ROOTED, "rr": same[::-1]}, HOME),
        (["/r/\u00e9/b/c", "/r/a/b/c"], {**ROOTED, "rr": wide}, HOME),
        (["/r/\u00e9/b/c", "/r/a/b/c"], {**ROOTED, "rr": wide[::-1]}, HOME),
        (["/r/a/x"], {**ROOTED, "rr": ["/r", "/r/a"]}, HOME),
        (["/r/a/x"], {**ROOTED, "rr": ["/r/a", "/r"]}, HOME),
        (
            ["/old/a/x", "/old/b/x", "/old/a/x", "/old/b/../a/x"],
            {**ROOTED, "rr": ["/old/a", "/old/b"]},
            HOME,
        ),
        (
            ["/orig/x", "/orig/y", "/else/z"],
            {
                **ROOTED,
                "or": "/orig",
                "t": {
                    "/orig/x": ["/live/x", "checkout"],
                    "/else/z": ["/live/z", "checkout"],
                },
            },
            HOME,
        ),
        (["/orig/x", "/orig/y"], {**ROOTED, "or": "", "t": {}}, HOME),
        (["/orig/x"], {**ROOTED, "or": "/orig", "t": {}}, HOME),
        (["/orig/x"], {**ROOTED, "or": "/orig"}, HOME),
        (["/live/x", "/live/x/", "/live/./x"], {**ROOTED, "rr": []}, HOME),
        (["rel/x", "./x", "x\\y"], {**ROOTED, "rr": ["/old"]}, HOME),
        (["/old/../x", "/old/a/../b"], {**ROOTED, "rr": ["/old/a"]}, HOME),
        (
            ["/old/a/../b"],
            {"root": "/old/a", "rule": "checkout", "rr": ["/old/x"]},
            HOME,
        ),
        (
            ["/old/a/../b"],
            {"root": None, "rule": "no_live_checkout", "rr": ["/old"]},
            HOME,
        ),
        (["/old/a/../b"], {"root": "", "rule": "checkout", "rr": ["/old"]}, HOME),
        (["/old/x/../b"], {"root": "/old", "rule": "checkout", "or": "/old"}, HOME),
        (["/else/../x"], {**ROOTED, "rr": ["/old"]}, HOME),
        (["/old/a\\..\\b"], {**ROOTED, "rr": ["/old/x"], "or": "/old"}, HOME),
        (
            ["/old/a", "/other/b"],
            {
                **ROOTED,
                "rr": ["/old"],
                "t": {
                    "/other/b": ["/live/b", "checkout"],
                    "/old/a": ["/q", "unanchored"],
                },
            },
            HOME,
        ),
        (["/elsewhere/q"], {**ROOTED, "rr": ["/old"], "t": {}}, HOME),
        (["/old2/q"], {**ROOTED, "or": "/old2", "t": {}}, HOME),
        (
            ["/old2/q"],
            {**ROOTED, "or": "/old2", "t": {"/old2/q": ["/live/q", "absolute"]}},
            HOME,
        ),
        (
            ["/old2/q"],
            {
                "root": None,
                "rule": "no_live_checkout",
                "or": "/old2",
                "t": {"/old2/q": ["/a", "x"]},
            },
            HOME,
        ),
        (["/live/a", "/old/a"], {**ROOTED, "rr": ["/old"]}, HOME),
        (["$HOME/x", "${HOME}/x", "$HOME"], {**ROOTED, "rr": ["/old"]}, HOME),
        (["C:\\x", "C:/x", "c:\\x"], {**ROOTED, "rr": ["/old"]}, HOME),
        (
            ["/old/x"],
            {"root": "/live", "rule": "project_checkout", "rr": ["/old", "/old"]},
            HOME,
        ),
        # A Windows view reached from POSIX (`relative_under` in ntpath).
        (
            ["C:\\old\\x", "C:/old/y", "C:\\other\\z"],
            {**ROOTED, "rr": ["C:\\old"]},
            HOME,
        ),
        (["//s/sh/x", "\\\\s\\sh"], {**ROOTED, "rr": ["\\\\s\\sh"]}, HOME),
        (
            ["C:\\old\\x"],
            {
                "root": "D:\\live",
                "rule": "checkout",
                "or": "C:\\old",
                "t": {"C:\\old\\x": ["D:\\live\\x", "checkout"]},
            },
            HOME,
        ),
        (
            ["/a/x"],
            {
                "root": "rel",
                "rule": "checkout",
                "or": "/a",
                "t": {"/a/x": ["rel/x", "checkout"]},
            },
            HOME,
        ),
        # `~` paths: `os.path.expanduser` with the home recorded.
        (["~/proj/x", "~/proj/y", "~"], {**ROOTED, "rr": ["/hd/u/proj"]}, HOME),
        (["~/proj/x"], {**ROOTED, "rr": ["/hd/u/proj"]}, None),
        (["~/proj/x"], {**ROOTED, "rr": ["/hd/u/proj"]}, "/hd/u/"),
        (["~/proj/x"], {**ROOTED, "rr": ["/proj"]}, "/"),
        (["~/proj/x"], {**ROOTED, "rr": ["/proj"]}, ""),
        (
            ["~alice/p/x", "~nobody/p/x", "~root/p/x"],
            {**ROOTED, "rr": ["/hd/alice/p", "/p"]},
            HOME,
        ),
        (["~slashy/x", "~empty/x"], {**ROOTED, "rr": ["//srv//x", "/x"]}, HOME),
        (["~al\x00ce/x"], {**ROOTED, "rr": ["/p"]}, HOME),
        (["~/../x"], {**ROOTED, "rr": ["/hd/u"]}, HOME),
        (["~/../x"], {**ROOTED, "rr": ["/hd"]}, HOME),
        (
            ["~/x"],
            {**ROOTED, "or": "/hd/u", "t": {"/hd/u/x": ["/live/x", "checkout"]}},
            HOME,
        ),
        (["~x"], {**ROOTED, "rr": ["/p"]}, HOME),
    ]
    return out


def _sweep() -> dict[str, Any]:
    roots = [
        "".join(p)
        for n in range(1, SWEEP_RMAX + 1)
        for p in itertools.product(SWEEP_ALPHABET, repeat=n)
    ]
    paths = [
        "".join(p)
        for n in range(SWEEP_PMAX + 1)
        for p in itertools.product(SWEEP_ALPHABET, repeat=n)
    ]

    def run() -> bytes:
        out = []
        for root in roots:
            for path in paths:
                answer = ca.relative_under(path, root)
                out.append("-" if answer is None else "=" + answer)
        return ("\n".join(out) + "\n").encode("utf-8")

    answers, _seen = _run(HOME, run)
    return {
        "f": "sweep",
        "alphabet": SWEEP_ALPHABET,
        "rmax": SWEEP_RMAX,
        "pmax": SWEEP_PMAX,
        "count": len(roots) * len(paths),
        "sha256": hashlib.sha256(answers).hexdigest(),
    }


def _rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plain: list[dict[str, Any]] = []
    posix: list[dict[str, Any]] = []
    made = [_ru(p, r) for p, r in RU]
    made += [_eh(p, roots) for p, roots in EH]
    made += [_pca(paths, a, home) for paths, a, home in _pcas()]
    for row, seen in made:
        if seen.nt_relpath or seen.tilde:
            posix.append(row)
        else:
            plain.append(row)
    return plain, posix


def _header(count: int) -> dict[str, Any]:
    return {"k": "header", "count": count, "cwd": CWD, "home": HOME, "users": USERS}


def generate() -> dict[str, bytes]:
    plain, _posix = _rows()
    return {"commit_anchors.jsonl": jsonl([_header(len(plain)), *plain])}


def generate_posix() -> dict[str, bytes]:
    _plain, posix = _rows()
    posix.append(_sweep())
    return {"commit_anchors_posix.jsonl": jsonl([_header(len(posix)), *posix])}
