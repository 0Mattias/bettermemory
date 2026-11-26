"""Fixtures: the leaves of `scope_match` (``scope_match.jsonl``,
``scope_match_posix.jsonl``, ``scope_match_posix_python.jsonl``,
``scope_match_unicode.jsonl``, ``scope_match_volume.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/scope_match.rs``. This pins it to
`scope_match`'s `_MAX_MATCHES`, `_NOISY_NAME_STOPLIST`,
`ScopeMismatchEntry`, `ScopeMismatchReport`, `_roots_of`,
`_drop_ancestor_roots`, `_home_alias`, `_boundary_after`,
`_find_root_occurrence`, `_declared_root_spans`, `_declared_root_covers`
and `_trim_evidence`.

``scope_match.jsonl`` holds the leaves that read no separator and no home
(`_boundary_after`, `_find_root_occurrence`, `_trim_evidence`, the report
types) over ASCII texts, so every platform and release answers it alike.
The rest read ``os.sep``, ``os.altsep`` and ``Path.home()``, whose Windows
flavour answers otherwise, so ``scope_match_posix.jsonl`` and
``scope_match_unicode.jsonl`` are POSIX only, and the second, whose texts
run past ASCII, is gated on the Unicode data too. The rows of the first
under a home `Path.home()` reads by the release (``RELEASE_HOMES``) go in
``scope_match_posix_python.jsonl``, gated on the Python release.

In those two the home is recorded, never read: ``$HOME`` is the row's
``home`` (removed when null, with the password entry of the running user
unknown), and `_home_ignores_case` is replaced by a table: it answers True
for the paths the row's ``folds`` lists and False for any other, and
every path it is asked is logged in ``probes`` (in order; sorted for
`_declared_root_spans`, whose set of declared scopes is iterated in the
hash seed's order). A line is one of:

- ``{"f": "home", "home", "r"}``: ``_fold_altsep(str(Path.home()), ...)``;
- ``{"f": "roots", "by", "home", "folds", "r", "probes"}``:
  `_roots_of` of ``by`` (``[scope, [[cwd, count], ...]]`` in insertion
  order), answering ``[[scope, root], ...]``;
- ``{"f": "drop", "roots", "r"}``: `_drop_ancestor_roots`;
- ``{"f": "alias", "path", "home", "folds", "r", "probes"}``;
- ``{"f": "spans", "body", "declared", "roots", "home", "folds", "r",
  "probes"}``: `_declared_root_spans`, the spans sorted;
- ``{"f": "covers", "body", "idx", "root", "declared", "contracted",
  "home", "folds", "r", "probes"}``: `_declared_root_covers`;
- ``{"f": "boundary", "body", "end", "r"}``, ``{"f": "occurrence",
  "body", "needle", "r"}``, ``{"f": "trim", "body", "start", "end",
  "padding", "r"}`` (``padding`` absent: the default);
- ``{"f": "sweep", "seed", "count", "sha256"}``: the draws of `_sweep`.

``scope_match_volume.jsonl`` probes for real: a scratch folder (``@``,
resolved) holds ``Base``, ``Base/Work/Sub``, ``Proj`` and ``Proj/Child``,
``$HOME`` is ``@/Base``, and its rows (``roots``, ``alias``, ``spans``,
``covers``, without ``folds`` and ``probes``) answer what the volume
answers; the header's ``folds`` is whether it folds case. Its bytes follow
the volume, so it is gated on the platform.

A text holding a surrogate or two backslashes in a row is
``{"cps": [...]}``; a raise is ``{"raise": class name}``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import scope_match as sm
from bettermemory import verify_candidates as vc

from ._common import has_surrogate, jsonl

HOME = "/hd/u"


def _s(text: str) -> Any:
    if has_surrogate(text) or "\\\\" in text:
        return {"cps": [ord(ch) for ch in text]}
    return text


def _h(home: str | None) -> Any:
    return None if home is None else _s(home)


class _FakePwd:
    @staticmethod
    def getpwuid(uid: int) -> Any:
        raise KeyError(uid)


@contextmanager
def _world(home: str | None, probe: Callable[[str], bool]) -> Iterator[None]:
    env = dict(os.environ)
    env.pop("HOME", None)
    if home is not None:
        env["HOME"] = home
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.dict(sys.modules, {"pwd": _FakePwd}),
        mock.patch.object(sm, "_home_ignores_case", probe),
    ):
        yield


def _call(
    home: str | None, folds: list[str], fn: Callable[[], Any]
) -> tuple[Any, list[str]]:
    """``fn()`` in the row's world: its answer (or the class raised) and
    the paths probed, in order."""
    probes: list[str] = []

    def probe(path: str) -> bool:
        probes.append(path)
        return path in folds

    with _world(home, probe):
        try:
            return fn(), probes
        except Exception as exc:  # noqa: BLE001 - the class is the answer
            return {"raise": type(exc).__name__}, probes


# -- the leaves that read no separator and no home ----------------------------

BOUNDARY = [
    ("", 0),
    ("abc", 3),
    ("abc", 9),
    ("foo/x", 3),
    ("foobar", 3),
    ("foo-bar", 3),
    ("foo_bar", 3),
    ("foo9", 3),
    ("foo.bak", 3),
    ("foo.", 3),
    ("foo. x", 3),
    ("foo.\n", 3),
    ("foo.\t", 3),
    ("foo..", 3),
    ("foo./", 3),
    ("foo x", 3),
    ("foo)", 3),
    ("foo,", 3),
    ("foo:", 3),
    ("foo~", 3),
    ("foo\\x", 3),
    ("foo\x0b.", 3),
    ("foo.\x1c", 3),
    ("foo.\x1f", 3),
]

OCCURRENCE = [
    ("", ""),
    ("abc", ""),
    ("a b", ""),
    ("x/", ""),
    ("", "/a"),
    ("/a", "/a"),
    ("/ab", "/a"),
    ("see /a/foo/x.py", "/a/foo"),
    ("see /a/foo.bak and /a/foo/x.py", "/a/foo"),
    ("see /a/foobar then /a/foo-bar, /a/foo_bar", "/a/foo"),
    ("x/a/foo y", "/a/foo"),
    ("-/a/foo y", "/a/foo"),
    ("_/a/foo y", "/a/foo"),
    ("./a/foo y", "/a/foo"),
    ("ends /a/foo.", "/a/foo"),
    ("ends /a/foo. next", "/a/foo"),
    ("/a/foo.x /a/foo", "/a/foo"),
    ("aaaa", "aa"),
    ("baaa", "aa"),
    ("/x/x/x", "/x/x"),
    ("~/w/p and ~/w/p/q", "~/w/p"),
    ("a~/w/p", "~/w/p"),
    ("/a/foo\n", "/a/foo"),
    ("/A/FOO /a/foo", "/a/foo"),
    # The occurrence at 1 follows `x`; the one at 3 overlaps it and stands.
    ("x/././ ", "/./"),
]

TRIM = [
    ("", 0, 0, None),
    ("abc", 0, 3, None),
    ("abc", 1, 2, 0),
    ("abc", 1, 2, 1),
    ("abc", 3, 3, 0),
    ("abc", 9, 12, None),
    ("abc", 2, 1, 0),
    ("x" * 30 + "/a/b" + "y" * 30, 30, 34, None),
    ("x" * 24 + "/a/b" + "y" * 24, 24, 28, None),
    ("x" * 25 + "/a/b" + "y" * 25, 25, 29, None),
    ("  lead\n\n/a/b\t\ttail  ", 8, 12, None),
    ("one\r\ntwo /a three\x0b\x0cfour", 9, 11, None),
    ("\n\n\n/a\n\n\n", 3, 5, None),
    ("   ", 1, 2, None),
    ("a" * 60, 10, 50, 5),
    ("a  b" * 20, 30, 31, None),
    ("x\x1cy\x1dz\x1e/a\x1f", 6, 8, None),
]


def _rows_plain() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for body, end in BOUNDARY:
        rows.append(
            {
                "f": "boundary",
                "body": _s(body),
                "end": end,
                "r": sm._boundary_after(body, end),
            }
        )
    for body, needle in OCCURRENCE:
        hit = sm._find_root_occurrence(body, needle)
        rows.append(
            {
                "f": "occurrence",
                "body": _s(body),
                "needle": _s(needle),
                "r": list(hit) if hit else None,
            }
        )
    for body, start, end, padding in TRIM:
        row: dict[str, Any] = {
            "f": "trim",
            "body": _s(body),
            "start": start,
            "end": end,
        }
        if padding is None:
            got = sm._trim_evidence(body, start, end)
        else:
            row["padding"] = padding
            got = sm._trim_evidence(body, start, end, padding=padding)
        row["r"] = _s(got)
        rows.append(row)
    entries = [
        sm.ScopeMismatchEntry(
            kind="project_name",
            evidence="...see foo...",
            suggested_scope="projects:foo",
        ),
        sm.ScopeMismatchEntry(
            kind="project_root", evidence="/a/b", suggested_scope="projects:b"
        ),
    ]
    for entry in entries:
        rows.append(
            {
                "f": "entry",
                "kind": entry.kind,
                "evidence": entry.evidence,
                "scope": entry.suggested_scope,
                "r": list(entry.to_dict().items()),
            }
        )
    for matches in ([], entries[:1], entries):
        report = sm.ScopeMismatchReport(
            matches=tuple(matches),
            suggested_scopes=tuple(sorted(e.suggested_scope for e in matches)),
        )
        rows.append({"f": "report", "n": len(matches), "r": report.has_mismatch})
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows_plain()
    header = {
        "cases": len(rows),
        "max_matches": sm._MAX_MATCHES,
        "stoplist": sorted(sm._NOISY_NAME_STOPLIST),
        "padding": inspect.signature(sm._trim_evidence).parameters["padding"].default,
    }
    return {"scope_match.jsonl": jsonl([header, *rows])}


# -- the leaves that read the separators and the home ---------------------------


def _home_row(home: str | None) -> dict[str, Any]:
    got, _ = _call(
        home, [], lambda: vc._fold_altsep(str(Path.home()), os.sep, os.altsep)
    )
    return {
        "f": "home",
        "home": _h(home),
        "r": got if isinstance(got, dict) else _s(got),
    }


def _roots_row(
    by: list[tuple[str, list[tuple[str, int]]]], home: str | None, folds: list[str]
) -> dict[str, Any]:
    def run() -> Any:
        by_scope: dict[str, Counter[str]] = {}
        for scope, counts in by:
            counter: Counter[str] = by_scope.setdefault(scope, Counter())
            for cwd, n in counts:
                counter[cwd] += n
        return [[_s(s), _s(r)] for s, r in sm._roots_of(by_scope).items()]

    got, probes = _call(home, folds, run)
    return {
        "f": "roots",
        "by": [[_s(s), [[_s(c), n] for c, n in counts]] for s, counts in by],
        "home": _h(home),
        "folds": [_s(f) for f in folds],
        "r": got,
        "probes": [_s(p) for p in probes],
    }


def _drop_row(roots: list[tuple[str, str]]) -> dict[str, Any]:
    got = sm._drop_ancestor_roots(dict(roots))
    return {
        "f": "drop",
        "roots": [[_s(s), _s(r)] for s, r in roots],
        "r": [[_s(s), _s(r)] for s, r in got.items()],
    }


def _alias_row(path: str, home: str | None, folds: list[str]) -> dict[str, Any]:
    got, probes = _call(home, folds, lambda: sm._home_alias(path))
    return {
        "f": "alias",
        "path": _s(path),
        "home": _h(home),
        "folds": [_s(f) for f in folds],
        "r": got if isinstance(got, dict) or got is None else _s(got),
        "probes": [_s(p) for p in probes],
    }


def _spans_row(
    body: str,
    declared: list[str],
    roots: list[tuple[str, str]],
    home: str | None,
    folds: list[str],
) -> dict[str, Any]:
    got, probes = _call(
        home,
        folds,
        lambda: sorted(
            list(s) for s in sm._declared_root_spans(body, set(declared), dict(roots))
        ),
    )
    return {
        "f": "spans",
        "body": _s(body),
        "declared": [_s(d) for d in declared],
        "roots": [[_s(s), _s(r)] for s, r in roots],
        "home": _h(home),
        "folds": [_s(f) for f in folds],
        "r": got,
        "probes": [_s(p) for p in sorted(probes)],
    }


def _covers_row(
    body: str,
    idx: int,
    root: str,
    declared: list[str],
    contracted: bool,
    home: str | None,
    folds: list[str],
) -> dict[str, Any]:
    got, probes = _call(
        home,
        folds,
        lambda: sm._declared_root_covers(
            body, idx, root, set(declared), contracted=contracted
        ),
    )
    return {
        "f": "covers",
        "body": _s(body),
        "idx": idx,
        "root": _s(root),
        "declared": [_s(d) for d in declared],
        "contracted": contracted,
        "home": _h(home),
        "folds": [_s(f) for f in folds],
        "r": got,
        "probes": [_s(p) for p in probes],
    }


H = HOME
HOMES: list[str | None] = [
    H,
    "/hd/u/",
    "/",
    "",
    "//",
    "///",
    "//srv",
    "/a//b/./c",
    "rel",
    "rel/",
    "~e",
    None,
]

ROOTS: list[tuple[list[tuple[str, list[tuple[str, int]]]], str | None, list[str]]] = [
    ([], H, []),
    ([], None, []),
    ([], "~e", []),
    ([("projects:a", [("/w/a", 1)])], H, []),
    ([("projects:a", [("/w/a", 1)])], None, []),
    # most_common: the highest count, the first counted on a tie.
    ([("projects:a", [("/w/b", 2), ("/w/a", 2)])], H, []),
    ([("projects:a", [("/w/a", 2), ("/w/b", 2)])], H, []),
    ([("projects:a", [("/w/a", 1), ("/w/b", 3), ("/w/c", 3)])], H, []),
    ([("projects:a", [("/w/z", 0)])], H, []),
    ([("projects:a", [("/w/z", 0), ("/w/y", 0)])], H, []),
    # An empty count: `most_common(1)` is empty.
    ([("projects:a", [])], H, []),
    ([("projects:b", [("/w/b", 1)]), ("projects:a", [])], H, []),
    # The degenerate roots: the home and `/`, byte for byte.
    (
        [
            ("projects:a", [(H, 1)]),
            ("projects:b", [("/", 1)]),
            ("projects:c", [("/w/c", 1)]),
        ],
        H,
        [],
    ),
    ([("projects:a", [(H + "/", 1)]), ("projects:b", [("//", 1)])], H, []),
    # `/` alone, and beside a relative root no ancestor rule reaches.
    ([("projects:a", [("/", 1)])], H, []),
    ([("projects:a", [("/", 1)]), ("projects:b", [("rel", 1)])], H, []),
    ([("projects:a", [(H, 5), ("/w/a", 1)])], H, []),
    # The home re-cased: dropped only when the home's volume folds case,
    # probed once however many such roots.
    ([("projects:a", [("/HD/U", 1)]), ("projects:b", [("/hd/U", 1)])], H, []),
    ([("projects:a", [("/HD/U", 1)]), ("projects:b", [("/hd/U", 1)])], H, [H]),
    ([("projects:a", [("/HD/U", 1)])], H, ["/HD/U"]),
    ([("projects:a", [("/w/a", 1)]), ("projects:b", [("/w/b", 1)])], H, [H]),
    ([("projects:a", [("/HD/U/x", 1)])], H, [H]),
    # A home of `/` (also `""` and `//`): `/` is the home itself.
    ([("projects:a", [("/", 1)]), ("projects:b", [("/x", 1)])], "/", []),
    ([("projects:a", [("/", 1)]), ("projects:b", [("/x", 1)])], "", []),
    ([("projects:a", [("//", 1)]), ("projects:b", [("/x", 1)])], "//", []),
    (
        [("projects:a", [("//srv", 1)]), ("projects:b", [("//SRV", 1)])],
        "//srv",
        ["//srv"],
    ),
    ([("projects:a", [("rel", 1)]), ("projects:b", [("REL", 1)])], "rel/", []),
    ([("projects:a", [("rel", 1)]), ("projects:b", [("REL", 1)])], "rel/", ["rel"]),
    # Then the ancestors go.
    (
        [
            ("projects:a", [("/w", 1)]),
            ("projects:b", [("/w/b", 1)]),
            ("projects:c", [("/w/c", 1)]),
        ],
        H,
        [],
    ),
    ([("projects:a", [("/w", 1)]), ("projects:b", [(H, 1)])], H, []),
    ([("projects:a", [("/w", 1)]), ("projects:b", [("/W/b", 1)])], H, []),
    ([("projects:a", [("/w/a", 1)]), ("projects:a", [("/w/b", 1)])], H, []),
]

DROPS: list[list[tuple[str, str]]] = [
    [],
    [("s", "/")],
    [("s", "/a")],
    [("a", "/a"), ("b", "/a/b")],
    [("b", "/a/b"), ("a", "/a")],
    [("a", "/a/foo"), ("b", "/a/foobar")],
    [("a", "/a/foo"), ("b", "/a/foo-bar")],
    [("a", "/a/foo"), ("b", "/a/foo.d")],
    [("a", "/a/foo/"), ("b", "/a/foo/x")],
    [("a", "/a/foo//"), ("b", "/a/foo")],
    [("a", "/a"), ("b", "/a")],
    [("a", "/a"), ("b", "/a/")],
    [("a", "/"), ("b", "/x")],
    [("a", "//"), ("b", "/x")],
    [("a", "/"), ("b", "//")],
    [("a", "/"), ("b", "rel")],
    [("a", ""), ("b", "/x")],
    [("a", ""), ("b", "x")],
    [("a", "rel"), ("b", "rel/x")],
    [("a", "/x"), ("b", "/x/y"), ("c", "/x/y/z"), ("d", "/q")],
    [("a", "/x/y/z"), ("b", "/x/y"), ("c", "/x")],
    [("a", "/a/b"), ("b", "/A/b/c")],
    [("a", "/a\\b"), ("b", "/a\\b\\c")],
    [("a", "/a"), ("b", "/a//b")],
]

ALIASES: list[tuple[str, str | None, list[str]]] = [
    (H + "/x", H, []),
    (H + "/", H, []),
    (H, H, []),
    (H, H, [H]),
    (H + "x", H, []),
    (H + "x/y", H, [H]),
    ("/w/x", H, []),
    ("/w/x", H, [H]),
    ("", H, [H]),
    ("/HD/U/x", H, []),
    ("/HD/U/x", H, [H]),
    ("/hd/U/X/y", H, [H]),
    ("/HD/U", H, [H]),
    ("/HD/u/", H, [H]),
    ("/hd/u//x", H, []),
    (H + "/x", "/hd/u/", []),
    (H + "/x", "/hd//u", []),
    ("//x", "/", []),
    ("/x", "/", []),
    ("//x", "", []),
    ("///x", "//", []),
    ("//srv/x", "//srv", []),
    ("rel/x", "rel", []),
    ("rel/x", "rel/", []),
    ("REL/x", "rel", ["rel"]),
    (H + "/x", "~e", []),
    (H + "/x", None, []),
    ("/a/b/c/d", "/a//b/./c", []),
    (H + "\\x", H, []),
]

SPANS: list[tuple[str, list[str], list[tuple[str, str]], str | None, list[str]]] = [
    ("", ["projects:a"], [("projects:a", "/w/a")], H, []),
    ("see /w/a/x", [], [("projects:a", "/w/a")], H, []),
    ("see /w/a/x", ["projects:a"], [], H, []),
    ("see /w/a/x", ["projects:a"], [("projects:a", "")], H, []),
    ("see /w/a/x", ["projects:a"], [("projects:a", "/w/a")], H, []),
    ("/w/a /w/a/b /w/ab", ["projects:a"], [("projects:a", "/w/a")], H, []),
    # Re-cased occurrences count only when the root's volume folds case,
    # probed once per scope at the first.
    ("see /W/A/x and /w/A and /w/a", ["projects:a"], [("projects:a", "/w/a")], H, []),
    (
        "see /W/A/x and /w/A and /w/a",
        ["projects:a"],
        [("projects:a", "/w/a")],
        H,
        ["/w/a"],
    ),
    ("see /W/A/x and /w/A and /w/a", ["projects:a"], [("projects:a", "/w/a")], H, [H]),
    ("only /w/a here", ["projects:a"], [("projects:a", "/w/a")], H, ["/w/a"]),
    # Overlapping needles: the search restarts one past each start.
    ("aaaa", ["projects:a"], [("projects:a", "aa")], H, []),
    ("AaAa", ["projects:a"], [("projects:a", "aa")], H, []),
    ("AaAa", ["projects:a"], [("projects:a", "aa")], H, ["aa"]),
    ("/x/x/x/x", ["projects:a"], [("projects:a", "/x/x")], H, []),
    # The alias: a root under the home is searched as `~` too.
    ("~/p/q and /hd/u/p/q", ["projects:a"], [("projects:a", H + "/p")], H, []),
    ("~/P/q", ["projects:a"], [("projects:a", H + "/p")], H, []),
    ("~/P/q", ["projects:a"], [("projects:a", H + "/p")], H, [H + "/p"]),
    ("~/p", ["projects:a"], [("projects:a", "/HD/U/p")], H, [H]),
    ("~/p", ["projects:a"], [("projects:a", "/HD/U/p")], H, [H, "/HD/U/p"]),
    ("~", ["projects:a"], [("projects:a", H)], H, []),
    ("~/p", ["projects:a"], [("projects:a", H + "/p")], None, []),
    # Two scopes: each its own probe; the same root twice is two scopes.
    (
        "/W/A /w/b /W/B",
        ["projects:a", "projects:b"],
        [("projects:a", "/w/a"), ("projects:b", "/w/b")],
        H,
        ["/w/b"],
    ),
    (
        "/W/A /w/a",
        ["projects:a", "projects:b"],
        [("projects:a", "/w/a"), ("projects:b", "/w/a")],
        H,
        ["/w/a"],
    ),
    (
        "/w/a/b/c",
        ["projects:a", "projects:b"],
        [("projects:a", "/w/a"), ("projects:b", "/w/a/b")],
        H,
        [],
    ),
    # Escaped needles: re.escape keeps every special character literal.
    ("x a.b*c[d] (e)+f$ y", ["projects:a"], [("projects:a", "a.b*c[d] (e)+f$")], H, []),
    ("axbxc", ["projects:a"], [("projects:a", "a.b")], H, []),
    ("p\\q P\\Q", ["projects:a"], [("projects:a", "p\\q")], H, ["p\\q"]),
    ("a b\tc", ["projects:a"], [("projects:a", "b\tc")], H, []),
    ("the end /w/a", ["projects:z"], [("projects:a", "/w/a")], H, []),
    # A scope declared twice: the reference's set reads it once.
    (
        "see /W/p/x and /w/P",
        ["projects:a", "projects:a"],
        [("projects:a", "/w/P")],
        H,
        ["/w/P"],
    ),
    # An uncased prefix longer than the rest, met at every start; the one
    # match overlaps the prefixes before it.
    ("/" * 41 + "x", ["projects:a"], [("projects:a", "/" * 8 + "X")], H, []),
    (
        "/" * 41 + "x",
        ["projects:a"],
        [("projects:a", "/" * 8 + "X")],
        H,
        ["/" * 8 + "X"],
    ),
    (
        "1" * 30 + "2" + "1" * 9 + "2",
        ["projects:a"],
        [("projects:a", "1" * 9 + "2")],
        H,
        [],
    ),
]

COVERS: list[tuple[str, int, str, list[str], bool, str | None, list[str]]] = [
    ("/p/c/x", 0, "/p", [], False, H, []),
    ("/p/c/x", 0, "/p", ["/p/c"], False, H, []),
    ("/p/c", 0, "/p", ["/p/c"], False, H, []),
    ("/p/cd/x", 0, "/p", ["/p/c"], False, H, []),
    ("/p/c.bak", 0, "/p", ["/p/c"], False, H, []),
    ("/p/c. next", 0, "/p", ["/p/c"], False, H, []),
    ("/p/c.", 0, "/p", ["/p/c"], False, H, []),
    ("/p/d/x", 0, "/p", ["/p/c"], False, H, []),
    ("/p", 0, "/p", ["/p"], False, H, []),
    ("/p/c/x", 0, "/p", ["/q/c"], False, H, []),
    ("x /p/c/x", 2, "/p", ["/p/c"], False, H, []),
    ("x /p/c/x", 0, "/p", ["/p/c"], False, H, []),
    ("/p/c", 9, "/p", ["/p/c"], False, H, []),
    # In code point order: the first that covers answers.
    ("/p/cd/x", 0, "/p", ["/p/cd", "/p/c"], False, H, []),
    ("/p/c/x", 0, "/p", ["/p/cd", "/p/c"], False, H, []),
    # The declared root re-cased against the candidate: its own probe.
    ("/P/c/x", 0, "/p", ["/P/c"], False, H, []),
    ("/P/c/x", 0, "/p", ["/P/c"], False, H, ["/P/c"]),
    ("/P/c/x", 0, "/p", ["/P/c"], False, H, ["/p"]),
    ("/P/c/x", 0, "/p", ["/P/c"], False, H, [H]),
    # A declared root listed twice: the reference's set probes it once.
    ("/P/c/x", 0, "/p", ["/P/c", "/P/c"], False, H, []),
    # The body re-cased against the declared root: probed again.
    ("/P/C/x", 0, "/p", ["/p/c"], False, H, []),
    ("/P/C/x", 0, "/p", ["/p/c"], False, H, ["/p/c"]),
    ("/P/C/x", 0, "/P", ["/p/c"], False, H, ["/p/c"]),
    ("/P/C/x", 0, "/P", ["/p/c", "/p/q"], False, H, ["/p/c", "/p/q"]),
    # Contracted: the declared root spelled by its alias.
    ("~/p/c/x", 0, H + "/p", [H + "/p/c"], True, H, []),
    ("~/p/c/x", 0, H + "/p", ["/w/p/c"], True, H, []),
    ("~/p/c/x", 0, H + "/p", ["/HD/U/p/c"], True, H, []),
    ("~/p/c/x", 0, H + "/p", ["/HD/U/p/c"], True, H, ["/HD/U/p/c"]),
    ("~/p/c/x", 0, H + "/p", ["/HD/U/p/c"], True, H, ["/HD/U/p/c", H]),
    ("~/P/C/x", 0, H + "/p", [H + "/p/c"], True, H, [H + "/p/c"]),
    ("~/p/c/x", 0, H + "/p", [H + "/p/c"], True, None, []),
    ("~/p/c/x", 0, H + "/p", ["/p/c/x/y"], True, None, []),
    ("~/p/c/x", 0, H + "/p", [H + "/p/c"], True, "~e", []),
    ("/p/c/x", 0, "/p", ["/p/c"], True, H, []),
    ("~/c/x", 0, "/", ["/c"], True, "/", []),
]


# -- the sweep ----------------------------------------------------------------

SWEEP_SEED = 0x5C0BE_1EAF5
SWEEP_COUNT = 5000
SWEEP_HOME = "/h"
BODY_PIECES = [
    "/h",
    "/a",
    "/A",
    "/b",
    "~",
    "/",
    "a",
    "B",
    "x",
    "-",
    "_",
    ".",
    " ",
    "\n",
    "/h/a",
    "/H",
]
ROOT_PIECES = ["/a", "/A", "/b", "/h", "/H", "x", "-", "."]
_MASK = (1 << 64) - 1


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


def _draw(rng: _Rng) -> tuple[list[str], str, list[int], list[str]]:
    """One draw: the roots (scope ``s<i>`` each), the body, the indexes of
    the declared scopes and the paths the probe answers True for."""
    n = rng.next(4) + 1
    roots = []
    for _ in range(n):
        k = rng.next(3) + 1
        roots.append("".join(ROOT_PIECES[rng.next(len(ROOT_PIECES))] for _ in range(k)))
    body = "".join(BODY_PIECES[rng.next(len(BODY_PIECES))] for _ in range(rng.next(24)))
    declared_mask = rng.next(1 << n)
    folds_mask = rng.next(1 << (n + 1))
    declared = [i for i in range(n) if declared_mask >> i & 1]
    folds = [roots[i] for i in range(n) if folds_mask >> i & 1]
    if folds_mask >> n & 1:
        folds.append(SWEEP_HOME)
    return roots, body, declared, folds


def _sweep_line(
    roots: list[str], body: str, declared: list[int], folds: list[str]
) -> str:
    project_roots = {f"s{i}": r for i, r in enumerate(roots)}
    scopes = [f"s{i}" for i in declared]
    declared_roots = {project_roots[s] for s in scopes if project_roots.get(s)}
    spans, span_probes = _call(
        SWEEP_HOME,
        folds,
        lambda: sorted(
            list(s) for s in sm._declared_root_spans(body, set(scopes), project_roots)
        ),
    )
    per_root = []
    for root in roots:

        def one(root: str = root) -> list[Any]:
            occ = sm._find_root_occurrence(body, root)
            cov = None
            if occ is not None:
                cov = sm._declared_root_covers(
                    body, occ[0], root, declared_roots, contracted=False
                )
            alias = sm._home_alias(root)
            occ2 = cov2 = None
            if alias is not None:
                occ2 = sm._find_root_occurrence(body, alias)
                if occ2 is not None:
                    cov2 = sm._declared_root_covers(
                        body, occ2[0], root, declared_roots, contracted=True
                    )
            return [
                list(occ) if occ else None,
                cov,
                alias,
                list(occ2) if occ2 else None,
                cov2,
            ]

        got, probes = _call(SWEEP_HOME, folds, one)
        per_root.append([*got, probes])
    return json.dumps(
        [spans, sorted(span_probes), per_root], separators=(",", ":"), ensure_ascii=True
    )


def _sweep() -> dict[str, Any]:
    rng = _Rng(SWEEP_SEED)
    digest = hashlib.sha256()
    for _ in range(SWEEP_COUNT):
        digest.update((_sweep_line(*_draw(rng)) + "\n").encode("ascii"))
    return {
        "f": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "sha256": digest.hexdigest(),
    }


#: Homes whose `Path.home()` follows the release: through 3.13 a home that
#: expands to a text starting with ``~`` raises `RuntimeError`; from 3.14
#: only one that stays ``~`` does, and ``~e`` is a relative home.
RELEASE_HOMES = {"~e"}


def _posix_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [_home_row(h) for h in HOMES]
    rows += [_roots_row(by, home, folds) for by, home, folds in ROOTS]
    rows += [_drop_row(roots) for roots in DROPS]
    rows += [_alias_row(path, home, folds) for path, home, folds in ALIASES]
    rows += [_spans_row(*case) for case in SPANS]
    rows += [_covers_row(*case) for case in COVERS]
    return rows


def generate_posix() -> dict[str, bytes]:
    rows = [r for r in _posix_rows() if r.get("home") not in RELEASE_HOMES]
    rows.append(_sweep())
    header = {"cases": len(rows), "home": HOME}
    return {"scope_match_posix.jsonl": jsonl([header, *rows])}


def generate_posix_python() -> dict[str, bytes]:
    rows = [r for r in _posix_rows() if r.get("home") in RELEASE_HOMES]
    header = {"cases": len(rows), "home": HOME}
    return {"scope_match_posix_python.jsonl": jsonl([header, *rows])}


# -- past ASCII ---------------------------------------------------------------

KELVIN = "K"
LONG_S = "ſ"
DOTTED_I = "İ"
I_DOT = "i̇"

UNICODE_BOUNDARY = [
    ("foo.\x85", 3),
    ("fooé", 3),
    ("foo٣", 3),
    ("foo²", 3),
    ("fooⅧ", 3),
    ("foó", 3),
    ("foo x", 3),
    ("foo. ", 3),
    ("foo. ", 3),
    ("foo.　", 3),
    ("foo.​", 3),
    ("foo‐", 3),
    ("foo\ud800", 3),
    ("foo.\ud800", 3),
    ("foo\U0001d400", 3),
]

UNICODE_OCCURRENCE = [
    ("é/a/foo y", "/a/foo"),
    ("٣/a/foo y", "/a/foo"),
    (" /a/foo y", "/a/foo"),
    ("\ud800/a/foo y", "/a/foo"),
    ("/a/fooé /a/foo ", "/a/foo"),
    ("/a/föo/x", "/a/föo"),
    ("/a/FÖO/x", "/a/föo"),
]

UNICODE_TRIM = [
    ("x\x1cy\x1dz\x1e/a\x1f\x85", 6, 8, None),
    ("  x /a y　　z\u0085", 3, 5, None),
    ("é" * 30 + "/a" + "é" * 30, 30, 32, None),
    ("a  b c", 0, 1, None),
    ("\ud800 /a \udfff", 2, 4, None),
    ("​x​", 1, 2, None),
]

UNICODE_ROOTS: list[
    tuple[list[tuple[str, list[tuple[str, int]]]], str | None, list[str]]
] = [
    # `str.lower` lengthens U+0130: the home and the root compared lowered.
    ([("projects:a", [("/" + I_DOT, 1)])], "/" + DOTTED_I, []),
    ([("projects:a", [("/" + I_DOT, 1)])], "/" + DOTTED_I, ["/" + DOTTED_I]),
    ([("projects:a", [("/i", 1)])], "/" + DOTTED_I, ["/" + DOTTED_I]),
    ([("projects:a", [("/K", 1)])], "/" + KELVIN, ["/" + KELVIN]),
    ([("projects:a", [("/ß", 1)])], "/SS", ["/SS"]),
    ([("projects:a", [("/Σ", 1)])], "/σ", ["/σ"]),
    ([("projects:a", [("/ς", 1)])], "/σ", ["/σ"]),
    ([("projects:é", [("/w/é", 2), ("/w/e", 2)])], "/h", []),
]

UNICODE_DROPS: list[list[tuple[str, str]]] = [
    [("a", "/é"), ("b", "/é/x")],
    [("a", "/é"), ("b", "/É/x")],
    [("a", "/é"), ("b", "/é/x")],
    [("a", "/\ud800"), ("b", "/\ud800/x")],
]

UNICODE_ALIASES: list[tuple[str, str | None, list[str]]] = [
    ("/" + DOTTED_I + "/x", "/" + DOTTED_I, []),
    ("/" + I_DOT + "/x", "/" + DOTTED_I, ["/" + DOTTED_I]),
    ("/i/x", "/" + DOTTED_I, ["/" + DOTTED_I]),
    ("/" + DOTTED_I + "/x", "/" + I_DOT, ["/" + I_DOT]),
    # Shorter than the home's prefix yet equal to it lowered: `~` alone.
    ("/" + DOTTED_I + "/", "/" + I_DOT, ["/" + I_DOT]),
    ("/" + DOTTED_I + "/", "/" + I_DOT, []),
    ("/" + DOTTED_I + "/x", "/I", ["/I"]),
    ("/k/x", "/" + KELVIN, ["/" + KELVIN]),
    ("/" + KELVIN + "/x", "/k", ["/k"]),
    ("/É/x", "/é", ["/é"]),
    ("/é/\ud800", "/é", []),
    ("/SS/x", "/ß", ["/ß"]),
]

UNICODE_SPANS: list[
    tuple[str, list[str], list[tuple[str, str]], str | None, list[str]]
] = [
    # IGNORECASE folds one code point against one: the Kelvin sign and the
    # long s match `k` and `s`; `ß` never matches `ss`.
    (
        "/w/" + KELVIN + "it and /w/kit",
        ["projects:a"],
        [("projects:a", "/w/kit")],
        "/h",
        [],
    ),
    (
        "/w/" + KELVIN + "it and /w/kit",
        ["projects:a"],
        [("projects:a", "/w/kit")],
        "/h",
        ["/w/kit"],
    ),
    (
        "/w/" + LONG_S + "rc",
        ["projects:a"],
        [("projects:a", "/w/src")],
        "/h",
        ["/w/src"],
    ),
    (
        "/w/" + LONG_S + "rc",
        ["projects:a"],
        [("projects:a", "/w/SRC")],
        "/h",
        ["/w/SRC"],
    ),
    (
        "/w/straße /w/strasse",
        ["projects:a"],
        [("projects:a", "/w/strasse")],
        "/h",
        ["/w/strasse"],
    ),
    (
        "/w/STRASSE /w/straße",
        ["projects:a"],
        [("projects:a", "/w/straße")],
        "/h",
        ["/w/straße"],
    ),
    ("/w/ẞ", ["projects:a"], [("projects:a", "/w/ß")], "/h", ["/w/ß"]),
    (
        "/w/" + DOTTED_I + "x /w/ix",
        ["projects:a"],
        [("projects:a", "/w/ix")],
        "/h",
        ["/w/ix"],
    ),
    (
        "/w/" + I_DOT + "x",
        ["projects:a"],
        [("projects:a", "/w/" + DOTTED_I + "x")],
        "/h",
        ["/w/" + DOTTED_I + "x"],
    ),
    ("/w/ς /w/Σ /w/σ", ["projects:a"], [("projects:a", "/w/σ")], "/h", ["/w/σ"]),
    ("/w/É /w/é", ["projects:a"], [("projects:a", "/w/é")], "/h", []),
    ("~/É", ["projects:a"], [("projects:a", "/h/é")], "/h", ["/h/é"]),
    ("/w/\ud800 /w/\ud800", ["projects:a"], [("projects:a", "/w/\ud800")], "/h", []),
    (
        "/w/\U00010400 /w/\U00010428",
        ["projects:a"],
        [("projects:a", "/w/\U00010428")],
        "/h",
        ["/w/\U00010428"],
    ),
]

UNICODE_COVERS: list[tuple[str, int, str, list[str], bool, str | None, list[str]]] = [
    # Each prefix cut at the raw length, then lowered: `/İ/x`'s first three
    # code points lowered are not `/i̇`.
    (
        "/" + DOTTED_I + "/x",
        0,
        "/" + I_DOT,
        ["/" + DOTTED_I + "/x"],
        False,
        "/h",
        ["/" + DOTTED_I + "/x"],
    ),
    (
        "/" + I_DOT + "/x",
        0,
        "/" + DOTTED_I,
        ["/" + I_DOT + "/x"],
        False,
        "/h",
        ["/" + I_DOT + "/x"],
    ),
    ("/I/x/y", 0, "/i", ["/" + DOTTED_I + "/x"], False, "/h", ["/" + DOTTED_I + "/x"]),
    (
        "/" + DOTTED_I + "/x/y",
        0,
        "/" + DOTTED_I,
        ["/" + DOTTED_I + "/x"],
        False,
        "/h",
        [],
    ),
    (
        "/" + I_DOT + "/x/y",
        0,
        "/" + DOTTED_I,
        ["/" + DOTTED_I + "/x"],
        False,
        "/h",
        ["/" + DOTTED_I + "/x"],
    ),
    ("/p/É/x", 0, "/p", ["/p/é"], False, "/h", ["/p/é"]),
    ("/p/éé/x", 0, "/p", ["/p/é"], False, "/h", []),
    ("/p/é٣", 0, "/p", ["/p/é"], False, "/h", []),
    ("/p/é. ", 0, "/p", ["/p/é"], False, "/h", []),
    ("/p/k/x", 0, "/p", ["/p/" + KELVIN], False, "/h", ["/p/" + KELVIN]),
    ("~/É/x", 0, "/h", ["/h/é"], True, "/h", ["/h/é"]),
]


def generate_unicode() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for body, end in UNICODE_BOUNDARY:
        rows.append(
            {
                "f": "boundary",
                "body": _s(body),
                "end": end,
                "r": sm._boundary_after(body, end),
            }
        )
    for body, needle in UNICODE_OCCURRENCE:
        hit = sm._find_root_occurrence(body, needle)
        rows.append(
            {
                "f": "occurrence",
                "body": _s(body),
                "needle": _s(needle),
                "r": list(hit) if hit else None,
            }
        )
    for body, start, end, padding in UNICODE_TRIM:
        assert padding is None
        rows.append(
            {
                "f": "trim",
                "body": _s(body),
                "start": start,
                "end": end,
                "r": _s(sm._trim_evidence(body, start, end)),
            }
        )
    rows += [_home_row(h) for h in ["/" + DOTTED_I, "/é/", "/\udcff"]]
    rows += [_roots_row(by, home, folds) for by, home, folds in UNICODE_ROOTS]
    rows += [_drop_row(roots) for roots in UNICODE_DROPS]
    rows += [_alias_row(path, home, folds) for path, home, folds in UNICODE_ALIASES]
    rows += [_spans_row(*case) for case in UNICODE_SPANS]
    rows += [_covers_row(*case) for case in UNICODE_COVERS]
    header = {"cases": len(rows)}
    return {"scope_match_unicode.jsonl": jsonl([header, *rows])}


# -- the volume's own probe ---------------------------------------------------

VOLUME_DIRS = ["Base", "Base/Work", "Base/Work/Sub", "Proj", "Proj/Child"]

VOLUME_ROOTS: list[list[tuple[str, list[tuple[str, int]]]]] = [
    [
        ("projects:a", [("@/Base", 1)]),
        ("projects:b", [("@/BASE", 2)]),
        ("projects:c", [("@/Proj", 1)]),
    ],
    [("projects:b", [("@/bASE", 1)]), ("projects:d", [("@/Gone", 1)])],
]

VOLUME_ALIASES = ["@/Base/Work/x", "@/BASE/Work/x", "@/base", "@/base/", "@/Gone/x"]

VOLUME_SPANS: list[tuple[str, list[str], list[tuple[str, str]]]] = [
    ("see @/proj/x and @/Proj/y", ["projects:c"], [("projects:c", "@/Proj")]),
    ("see @/gone/x and @/Gone/y", ["projects:d"], [("projects:d", "@/Gone")]),
    ("~/WORK/x and ~/Work/y", ["projects:w"], [("projects:w", "@/Base/Work")]),
    ("~/Work/x", ["projects:w"], [("projects:w", "@/BASE/Work")]),
]

VOLUME_COVERS: list[tuple[str, int, str, list[str], bool]] = [
    ("@/PROJ/child/z", 0, "@/Proj", ["@/Proj/Child"], False),
    ("@/Proj/Child/z", 0, "@/proj", ["@/Proj/Child"], False),
    ("@/gone/Sub/z", 0, "@/gone", ["@/Gone/Sub"], False),
    ("~/Work/Sub/f", 0, "@/BASE/Work", ["@/BASE/Work/Sub"], True),
    ("~/work/sub/f", 0, "@/Base/Work", ["@/Base/Work/Sub"], True),
]


def _at(at: str, text: str) -> str:
    return text.replace("@", at)


def _back(at: str, text: str) -> Any:
    return _s(text.replace(at, "@"))


def generate_volume() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        at = os.path.realpath(scratch)
        for d in VOLUME_DIRS:
            os.makedirs(os.path.join(at, d))
        home = _at(at, "@/Base")
        real = vc._home_ignores_case
        with _world(home, real):
            folds = real(_at(at, "@/Proj"))

            for by in VOLUME_ROOTS:
                by_scope: dict[str, Counter[str]] = {}
                for scope, counts in by:
                    for cwd, n in counts:
                        by_scope.setdefault(scope, Counter())[_at(at, cwd)] += n
                kept = sm._roots_of(by_scope)
                rows.append(
                    {
                        "f": "roots",
                        "by": [[s, [[c, n] for c, n in counts]] for s, counts in by],
                        "r": [[s, _back(at, r)] for s, r in kept.items()],
                    }
                )
            for path in VOLUME_ALIASES:
                alias = sm._home_alias(_at(at, path))
                rows.append(
                    {
                        "f": "alias",
                        "path": path,
                        "r": None if alias is None else _back(at, alias),
                    }
                )
            for body, declared, roots in VOLUME_SPANS:
                real_body = _at(at, body)
                spans = sorted(
                    sm._declared_root_spans(
                        real_body, set(declared), {s: _at(at, r) for s, r in roots}
                    )
                )
                rows.append(
                    {
                        "f": "spans",
                        "body": body,
                        "declared": declared,
                        "roots": [[s, r] for s, r in roots],
                        "r": [
                            [
                                len(real_body[:a].replace(at, "@")),
                                len(real_body[:b].replace(at, "@")),
                            ]
                            for a, b in spans
                        ],
                    }
                )
            for body, idx, root, declared, contracted in VOLUME_COVERS:
                covered = sm._declared_root_covers(
                    _at(at, body),
                    idx,
                    _at(at, root),
                    {_at(at, d) for d in declared},
                    contracted=contracted,
                )
                rows.append(
                    {
                        "f": "covers",
                        "body": body,
                        "idx": idx,
                        "root": root,
                        "declared": declared,
                        "contracted": contracted,
                        "r": covered,
                    }
                )
    header = {"cases": len(rows), "folds": folds}
    return {"scope_match_volume.jsonl": jsonl([header, *rows])}
