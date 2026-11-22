"""Fixtures: `origin_history`'s leaves and its memo (``origin_history.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_history.rs``. No git runs and
no file is read: the runner's counts and notes, the monotonic clock and
the thread's tally of walks are the case's.

The first line is a header with the count of the cases. Then one case a
line, by ``f``:

- ``const``: each constant, ``os.devnull`` spelled ``{devnull}``;
- ``unanswered``: ``b`` and ``n``, the runner's counts before and now,
  ``named``; ``r`` the member's value or null;
- ``note_unread``: ``why``; ``r`` the runner's notes made;
- ``read_lines``: ``raw`` (code points) read by ``rd`` (``seconds`` or
  ``instant``); ``r`` the values (an instant as `isoformat`) or null;
- ``author_instant``: ``v`` (decimal); ``r`` its `isoformat`;
- ``author_dates``: ``v`` (decimal each); ``r`` the `isoformat` of each;
- ``log_stdin``: ``rev`` and ``ps`` (code points each); ``r`` hex or null;
- ``shas``: a `ReachableWalk` (``t``: pairs of a path and its commits,
  ``c``: the commits, ``listed``) and ``ps``; ``r`` the commits, sorted;
- ``memo``: steps over one memo from empty, each ``[op, ...]`` and its
  answer, then ``e`` and ``x``, the entries and the failures, oldest
  first. A list of dates is ``n`` copies of the epoch, named by the index
  of the step that remembered it; an answer names the list it returns.
  ``recall`` ``[key, guard, cut, now]``; ``checked`` ``[key]``; ``base``
  ``[root, guard, cut]``; ``remember`` ``[key, n, guard, cut]``; ``fail``
  ``[key, guard, why, now]``. A guard is JSON, its lists read as tuples;
- ``unwalked``: steps ``["note", why]``, ``["count"]`` and
  ``["since", before]`` over a fresh tally, each with its answer.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest import mock

from pathlib import Path

from bettermemory import _git, gitfile_rule, origin_history as oh

from ._common import jsonl

U = oh.HistoryUnread


def _cps(text: str) -> list[int]:
    return [ord(c) for c in text]


def _dev(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(os.devnull, "{devnull}")
    if isinstance(value, dict):
        return [[k, _dev(v)] for k, v in value.items()]
    return [_dev(v) for v in value]


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _consts() -> dict[str, Any]:
    return {
        "f": "const",
        "env": _dev(oh.AS_STORED_ENV),
        "config": list(oh.AS_STORED_CONFIG),
        "diff": _dev(oh.AS_STORED_DIFF),
        "paths": list(oh.AS_STORED_PATHS),
        "cap": oh._TIMESTAMPS_MEMO_CAP,
        "slots": oh._TIMESTAMPS_MEMO_SLOTS,
        "failure": oh._FAILURE_SECONDS,
        "cuts_max": oh._CUTS_MAX,
        "history_env": list(oh._HISTORY_ENV),
        "members": [[m.name, m.value] for m in U],
    }


def _unanswered() -> Iterator[dict[str, Any]]:
    for before in ((0, 0), (3, 5)):
        for now in (
            before,
            (before[0] + 1, before[1]),
            (before[0], before[1] + 1),
            (before[0] + 1, before[1] + 1),
            (before[0] - 1, before[1]),
        ):
            for named in (False, True):
                with mock.patch.object(oh, "git_counts", return_value=now):
                    r = oh.unanswered(before, named=named)
                yield {
                    "f": "unanswered",
                    "b": list(before),
                    "n": list(now),
                    "named": named,
                    "r": None if r is None else r.value,
                }


def _notes() -> Iterator[dict[str, Any]]:
    for why in U:
        made: list[str] = []
        with (
            mock.patch.object(_git, "note_timed_out", lambda: made.append("t")),
            mock.patch.object(_git, "note_failed", lambda: made.append("f")),
        ):
            oh.note_unread(why)
        yield {"f": "note_unread", "why": why.value, "r": made}


_RAWS = [
    "",
    "\n",
    "\n\n",
    "1",
    "1\n",
    "1\n2\n3\n",
    "1\n\n2\n",
    "1\n2",
    " 1 \n\t2\r\n",
    "1\r2\n",
    "1\x0b\n",
    "1 \n",
    "x\n",
    "١\n",
    "0\n00\n007\n",
    "253402300799\n253402300800\n99999999999999999999999999\n",
    "-1\n",
    "+1\n",
    "1.5\n",
    "\ud800\n",
    "2024-01-02T03:04:05+02:00\n",
    "2024-01-02T03:04:05\n2024-01-02\n",
    "2024-01-02T03:04:05Z\nbad\n",
    " 2024-01-02T03:04:05-00:30 \n",
    "\n2024-01-02\n",
    "2024-01-02T03:04:05+02:00",
    "9999-12-31T23:59:59-01:00\n",
]


def _read_lines() -> Iterator[dict[str, Any]]:
    readers = {"seconds": oh.seconds, "instant": oh.instant}
    for raw in _RAWS:
        for name, read in readers.items():
            out = oh.read_lines(raw, read)
            yield {
                "f": "read_lines",
                "raw": _cps(raw),
                "rd": name,
                "r": None if out is None else [_iso(v) for v in out],
            }


_LAST = oh._LAST_SECOND
_FIRST = -62135596800
_VALUES = [
    0,
    1,
    -1,
    86399,
    86400,
    1700000000,
    _LAST - 1,
    _LAST,
    _LAST + 1,
    _LAST + 2,
    _FIRST,
    _FIRST + 1,
    _FIRST - 1,
    10**12,
    -(10**12),
    86399999999999,
    86400000000000,
    -86400000000000,
    10**18,
    -(10**18),
    2**63,
    -(2**63),
    2**127,
    -(2**127),
    10**60,
    -(10**60),
]


def _instants() -> Iterator[dict[str, Any]]:
    for v in _VALUES:
        yield {
            "f": "author_instant",
            "v": str(v),
            "r": oh.author_instant(v).isoformat(),
        }
    for values in ([], [3, 1, 2], [_LAST + 1, 0, -(10**20), 5, 5], _VALUES):
        yield {
            "f": "author_dates",
            "v": [str(v) for v in values],
            "r": [d.isoformat() for d in oh.author_dates(list(values))],
        }


_STDIN = [
    ([], []),
    (["HEAD"], []),
    (["a", "b"], ["p", "q/r"]),
    ([], ["-p"]),
    (["-a"], []),
    (["a-"], ["--"]),
    (["--"], []),
    ([""], []),
    ([], [""]),
    (["a\nb"], []),
    ([], ["a\rb"]),
    (["a\r"], []),
    (["HEAD\x00x"], ["p\x00"]),
    (["café"], ["\U0001f600"]),
    (["\udc80"], ["\udcff"]),
    (["\udc7f"], []),
    ([], ["\ud800"]),
    ([], ["\udd00"]),
    (["^a", "a..b"], [":(glob)*"]),
    ([" "], ["\t"]),
]


def _stdin() -> Iterator[dict[str, Any]]:
    for revs, specs in _STDIN:
        out = oh.log_stdin(revs, specs)
        yield {
            "f": "log_stdin",
            "rev": [_cps(r) for r in revs],
            "ps": [_cps(p) for p in specs],
            "r": None if out is None else out.hex(),
        }


_TOUCHED = {
    "a": frozenset({"c1"}),
    "a/b": frozenset({"c2"}),
    "a/b/c": frozenset({"c3"}),
    "a.b": frozenset({"c4"}),
    "a-b": frozenset({"c5"}),
    "ab": frozenset({"c6"}),
    "/": frozenset({"c7"}),
    "/x": frozenset({"c8"}),
    "": frozenset({"c9"}),
    "é/f": frozenset({"c10"}),
    "\udc80/g": frozenset({"c11"}),
    "z": frozenset(),
}
_SPECS = [
    [],
    ["a"],
    ["a/"],
    ["a//"],
    ["a/b"],
    ["a/b/"],
    ["ab"],
    ["a.b"],
    [""],
    ["/"],
    ["//"],
    ["x"],
    ["é"],
    ["\udc80"],
    ["a", "ab", "a/b/c"],
    ["z"],
    ["A"],
]


def _shas() -> Iterator[dict[str, Any]]:
    commits = ("c1", "c2", "c0")
    for listed in (True, False):
        walk = oh.ReachableWalk("an", "hd", commits, _TOUCHED, listed=listed)
        for specs in _SPECS:
            yield {
                "f": "shas",
                "t": [[_cps(k), sorted(v)] for k, v in _TOUCHED.items()],
                "c": list(commits),
                "listed": listed,
                "ps": [_cps(s) for s in specs],
                "r": sorted(walk.shas_touching(specs)),
            }


def _tuple(value: Any) -> Any:
    return tuple(_tuple(v) for v in value) if isinstance(value, list) else value


def _run_memo(steps: list[list[Any]]) -> dict[str, Any]:
    oh._clear_timestamps_memo()
    names: dict[int, int] = {}
    clock = [0.0]
    out = []

    def name(dates: Any) -> Any:
        return None if dates is None else names[id(dates)]

    with mock.patch.object(time, "monotonic", lambda: clock[0]):
        for i, step in enumerate(steps):
            op, *a = step
            if op == "recall":
                clock[0] = a[3]
                cut = None if a[2] is None else frozenset(a[2])
                dates, why = oh.recall(tuple(a[0]), _tuple(a[1]), cut)
                r: Any = [name(dates), None if why is None else why.value]
            elif op == "checked":
                got = oh.checked_cut(tuple(a[0]))
                r = None if got is None else sorted(got)
            elif op == "base":
                base = oh.base_for(a[0], _tuple(a[1]), frozenset(a[2]))
                r = None if base is None else [base[0], name(base[1]), sorted(base[2])]
            elif op == "remember":
                epoch = oh._EPOCH
                dates = [epoch] * a[1]
                names[id(dates)] = i
                r = oh.remember(tuple(a[0]), dates, _tuple(a[2]), frozenset(a[3]))
                if not r:
                    del names[id(dates)]
            else:
                clock[0] = a[3]
                oh.remember_failure(tuple(a[0]), _tuple(a[1]), U(a[2]))
                r = None
            out.append([step, r])
    entries = [
        [list(k), names[id(e.dates)], e.guard, sorted(e.cut)]
        for k, e in oh._TIMESTAMPS_MEMO.items()
    ]
    failed = [
        [list(k), at, g, w.value] for k, (at, g, w) in oh._TIMESTAMPS_FAILED.items()
    ]
    oh._clear_timestamps_memo()
    return {"f": "memo", "s": out, "e": entries, "x": failed}


K = ["r", "h1"]
K2 = ["r", "h2"]
G = [[1, 2], None, "x"]
G2 = [[1, 3], None, "x"]
F = oh._FAILURE_SECONDS
SLOTS = oh._TIMESTAMPS_MEMO_SLOTS
CAP = oh._TIMESTAMPS_MEMO_CAP


def _memo_cases() -> Iterator[list[list[Any]]]:
    # Recall against cuts: a subset, equal, a superset, None.
    for cut in ([], ["a"], ["a", "b"], ["c"], None):
        yield [
            ["remember", K, 3, G, ["a", "b"]],
            ["recall", K, G, cut, 0.0],
            ["checked", K],
        ]
    yield [["checked", K], ["recall", K, G, [], 0.0]]
    # Failures: inside and outside the window, the clock gone back, other
    # guards, None guards, the call budget.
    for dt in (0.0, 1.0, F - 1, F - 1e-9, F, F + 1, -1.0, -1e-9):
        yield [
            ["fail", K, G, "timeout", 100.0],
            ["recall", K, G, [], 100.0 + dt],
            ["recall", K, G, [], 100.0],
        ]
    for g in (G2, None, [[1, 2], None, "x"]):
        yield [
            ["fail", K, G, "error", 0.0],
            ["recall", K, g, None, 1.0],
            ["recall", K, G, None, 1.0],
        ]
    yield [["fail", K, None, "shallow", 0.0], ["recall", K, None, None, 1.0]]
    yield [["fail", K, G, "call_budget", 0.0], ["recall", K, G, None, 1.0]]
    for why in [m.value for m in U]:
        yield [["fail", K, G, why, 5.0], ["recall", K, G, None, 6.0]]
    # A kept entry answers before a failure; a refused cut falls to the failure.
    yield [
        ["remember", K, 2, G, ["a"]],
        ["fail", K, G, "error", 0.0],
        ["recall", K, G, ["a"], 1.0],
        ["recall", K, G, ["b"], 1.0],
        ["recall", K, G, None, 1.0],
    ]
    # remember clears the failure.
    yield [
        ["fail", K, G, "error", 0.0],
        ["remember", K, 1, G, []],
        ["recall", K, G, None, 1.0],
    ]
    # Re-remembering a key moves it last.
    yield [
        ["remember", K, 1, G, []],
        ["remember", K2, 1, G, []],
        ["remember", K, 2, G2, ["q"]],
        ["base", "r", G, []],
        ["base", "r", G2, ["q"]],
    ]
    # base_for: root, guard, cut each must match; the most recently used wins.
    yield [
        ["remember", K, 1, G, ["a"]],
        ["remember", K2, 2, G, ["a"]],
        ["base", "r", G, ["a"]],
        ["recall", K, G, [], 0.0],
        ["base", "r", G, ["a"]],
        ["base", "r", G, []],
        ["base", "r", G2, ["a"]],
        ["base", "s", G, ["a"]],
        ["base", "r", None, ["a"]],
        ["base", "r", G, ["a", "b"]],
    ]
    yield [["remember", K, 1, None, []], ["base", "r", None, []], ["base", "r", G, []]]
    # The cap of entries and of failures.
    yield [["remember", ["r", f"h{i}"], 1, G, []] for i in range(CAP + 2)] + [
        ["recall", ["r", "h0"], G, [], 0.0],
        ["recall", ["r", "h2"], G, [], 0.0],
        ["remember", ["r", "new"], 1, G, []],
        ["checked", ["r", "h3"]],
    ]
    yield [["fail", ["r", f"h{i}"], G, "error", float(i)] for i in range(CAP + 3)] + [
        ["fail", ["r", "h5"], G, "timeout", 30.0],
        ["fail", ["r", "z"], G, "error", 30.0],
    ]
    # The slots: exactly full, one over, and older entries evicted.
    yield [
        ["remember", K, SLOTS, G, []],
        ["remember", K2, SLOTS + 1, G, []],
        ["recall", K2, G, [], 0.0],
    ]
    yield [
        ["remember", K, SLOTS // 2, G, []],
        ["remember", K2, SLOTS // 2, G, []],
        ["remember", ["r", "h3"], 1, G, []],
        ["remember", ["r", "h4"], 0, G, []],
    ]
    yield [["remember", K, 5, G, []], ["remember", K2, SLOTS, G, []]]
    yield [["remember", K, 5, G, []], ["remember", K, SLOTS, G, []], ["checked", K]]
    yield [
        ["fail", K, G, "error", 0.0],
        ["remember", K, SLOTS + 1, G, []],
        ["recall", K, G, None, 1.0],
    ]


_UNWALKED_CASES: list[list[list[Any]]] = [
    [["count"], ["since", 0], ["since", 1]],
    [["note", "error"], ["count"], ["since", 0], ["since", 1], ["since", 2]],
    [["note", "error"], ["note", "timeout"], ["since", 1], ["since", 2], ["count"]],
    [["note", "shallow"], ["note", "call_budget"], ["since", 5]],
]


def _unwalked() -> Iterator[dict[str, Any]]:
    for steps in _UNWALKED_CASES:
        tally = oh._UNWALKED
        tally.count = 0
        tally.why = U.TIMEOUT
        out = []
        for step in steps:
            if step[0] == "note":
                oh.note_unwalked(U(step[1]))
                r: Any = None
            elif step[0] == "count":
                r = oh.unwalked()
            else:
                why = oh.given_up_since(step[1])
                r = None if why is None else why.value
            out.append([step, r])
        tally.count = 0
        tally.why = U.TIMEOUT
        yield {"f": "unwalked", "s": out}


def generate() -> dict[str, bytes]:
    cases: list[dict[str, Any]] = [_consts()]
    cases += _unanswered()
    cases += _notes()
    cases += _read_lines()
    cases += _instants()
    cases += _stdin()
    cases += _shas()
    cases += (_run_memo(steps) for steps in _memo_cases())
    cases += _unwalked()
    return {"origin_history.jsonl": jsonl([{"cases": len(cases)}, *cases])}


# `cuts_in` over shallow files (``origin_history_cuts.jsonl``). Each case
# is a tree written in a fresh temporary directory, entered with chdir, so
# no row holds its path: ``t`` maps a relative path to its bytes (a text,
# or ``{"p": prefix, "x": char, "n": count}``), ``p`` is the path
# `cuts_in` is given, ``r`` the commits sorted, null, or ``{"e": class}``.
# The git on PATH is data: release 2.54, `_reads_compat` True. The file of
# ``CUTS_MAX + 1`` bytes is the Rust test's own (one 40-digit record and
# ``0`` repeated), pinned there as null. Nothing follows the platform: the
# paths are relative, spelled with ``/``, and no case leans on an error
# class a system words differently (a path through a regular file is
# NotADirectoryError on POSIX and FileNotFoundError on Windows, so it is
# the Rust kills test's case, not a row).

_SHA1 = "[core]\n\trepositoryformatversion = 0\n"
_SHA256 = (
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"
)
_SHA1_V1 = (
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha1\n"
)
_UNKNOWN = (
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha512\n"
)
_V2 = "[core]\n\trepositoryformatversion = 2\n"
_A = "0123456789abcdef0123456789abcdef01234567"
_B = "fedcba9876543210fedcba9876543210fedcba98"
_L = "ab" * 32


def _cut_cases() -> Iterator[tuple[dict[str, Any], str]]:
    def repo(shallow: Any, config: str | None = _SHA1) -> dict[str, Any]:
        t: dict[str, Any] = {"repo/shallow": shallow}
        if config is not None:
            t["repo/config"] = config
        return t

    yield {"repo/config": _SHA1}, "repo/shallow"
    yield {}, "repo/shallow"
    yield repo(""), "repo/shallow"
    yield repo("", None), "repo/shallow"
    yield repo("", _UNKNOWN), "repo/shallow"
    yield repo(_A + "\n"), "repo/shallow"
    yield repo(_A), "repo/shallow"
    yield repo(_A + "\n" + _B + "\n"), "repo/shallow"
    yield repo(_B + "\n" + _A + "\n" + _B + "\n"), "repo/shallow"
    yield repo(_A + "\r\n"), "repo/shallow"
    yield repo(_A + "\r\n" + _B + "\r\n"), "repo/shallow"
    yield repo(_A + "ff\n"), "repo/shallow"
    yield repo(_A + "zz junk\n"), "repo/shallow"
    yield repo(_A + " tail\n"), "repo/shallow"
    yield repo(_A[:39] + "\n"), "repo/shallow"
    yield repo(_A[:39]), "repo/shallow"
    yield repo(_A.upper() + "\n"), "repo/shallow"
    yield repo(_A[:20] + "G" + _A[21:] + "\n"), "repo/shallow"
    yield repo(_A + "\n\n"), "repo/shallow"
    yield repo("\n"), "repo/shallow"
    yield repo("\n" + _A + "\n"), "repo/shallow"
    yield repo(_A + "\n" + _B), "repo/shallow"
    yield repo(_A + "\n", None), "repo/shallow"
    yield repo(_L + "\n", _SHA256), "repo/shallow"
    yield repo(_L.upper() + "x\n", _SHA256), "repo/shallow"
    yield repo(_A + "\n", _SHA256), "repo/shallow"
    yield repo(_L + "\n", _SHA1), "repo/shallow"
    yield repo(_A + "\n", _SHA1_V1), "repo/shallow"
    yield repo(_A + "\n", _UNKNOWN), "repo/shallow"
    yield repo(_A + "\n", _V2), "repo/shallow"
    yield repo(_A + "\n", "not a config ["), "repo/shallow"
    yield {"shallow": _A + "\n", "config": _SHA1}, "shallow"
    yield {"shallow": _A + "\n"}, "shallow"
    yield repo(_A + "\n"), "repo//shallow"
    yield repo(_A + "\n"), "repo/./shallow"
    yield repo(_A + "\n"), "repo/shallow/."
    yield repo(_A + "\n"), "./repo/shallow/"
    yield {**repo(_A + "\n"), "repo/sub/x": ""}, "repo/sub/../shallow"
    yield repo(_A + "\n"), "repo"
    yield repo(_A + "\n"), ""
    yield repo(_A + "\n"), "repo/sh\x00allow"
    yield (
        {
            "repo/shallow": {"p": _A, "x": "0", "n": oh._CUTS_MAX - 40},
            "repo/config": _SHA1,
        },
        "repo/shallow",
    )
    yield (
        {"repo/shallow": {"p": "", "x": _A + "\n", "n": 1000}, "repo/config": _SHA1},
        "repo/shallow",
    )


def _content(value: Any) -> bytes:
    if isinstance(value, dict):
        return (value["p"] + value["x"] * value["n"]).encode()
    return value.encode()


def _cut(tree: dict[str, Any], path: str) -> dict[str, Any]:
    saved = os.getcwd()
    with tempfile.TemporaryDirectory() as raw:
        os.chdir(raw)
        try:
            for rel, value in tree.items():
                Path(rel).parent.mkdir(parents=True, exist_ok=True)
                Path(rel).write_bytes(_content(value))
            with (
                mock.patch.object(
                    gitfile_rule, "_git_is_at_least", lambda want: (2, 54) >= want
                ),
                mock.patch.object(gitfile_rule, "_reads_compat", lambda: True),
            ):
                try:
                    out = oh.cuts_in(Path(path))
                except ValueError as exc:
                    r: Any = {"e": type(exc).__name__}
                else:
                    r = None if out is None else sorted(out)
        finally:
            os.chdir(saved)
    return {"t": tree, "p": path, "r": r}


def generate_cuts() -> dict[str, bytes]:
    cases = [_cut(tree, path) for tree, path in _cut_cases()]
    return {"origin_history_cuts.jsonl": jsonl([{"cases": len(cases)}, *cases])}
