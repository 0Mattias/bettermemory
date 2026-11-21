"""Fixtures: `origin_dates`' git-asking leaves (``origin_dates_reads.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_dates.rs``. This pins it to
`_read_author_timestamps`, `_hashed_seconds`, `_HEAD_PROBES`,
`_refused_head`, `_unborn_or_broken`, `_unread_entry`,
`_extended_author_timestamps` and `_marked_seconds`.

No git runs and no file is read: ``origin._git``, ``origin._git_result``,
``origin_history.git_counts``, ``_git.note_failed`` and the leaves of the
files and of discovery (``githead.gitdir_at``, ``locate``,
``head_unborn``, ``packed_names``, ``entry_above`` and
``gitfile_rule.discovery_refusal``) are the case's. Those leaves are
pinned by their own units.

A row names ``f`` (``read``, ``refused``, ``unborn``, ``entry``,
``extended``, ``hashed`` or ``marked``) and holds:

- ``g``: git's answers, in order, each a string (stdout), ``null`` (a
  non-zero exit, no count moves), ``"T"`` (the timeout: the first count
  moves) or ``"M"`` (no binary: the second count moves); for a
  ``_git_result`` call ``[returncode, stdout]``, or ``"T"``, ``"M"``;
- ``l``: the leaves: ``gd`` (``"at"``, ``"loc"`` or ``null``),
  ``unborn`` (``head_unborn``), ``packed``, ``refusal``, ``above``;
- ``r``: the answer: dates as ISO text, a ``HistoryUnread`` value or
  ``null``;
- ``c``: the calls in order, each ``[kind, args, timeout, verbatim,
  empty_ok]`` (kind ``g`` or ``R``) or a leaf's name (``packed`` with
  the name asked);
- ``n``: the failures noted.

Text with a lone surrogate is a list of code points. Every row holds
on every platform and Python release: the file is not gated.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest import mock

from bettermemory import _git as git_runner
from bettermemory import gitfile_rule, githead, origin, origin_dates, origin_history
from bettermemory.origin_history import HistoryUnread

from ._common import jsonl

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 64
UPPER = "A" * 40
G = "g" * 40
F = "f" * 40
TICK = "`" * 40


def _t(text: str) -> Any:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return [ord(c) for c in text]
    return text


def _spell(value: Any) -> Any:
    return _t(value) if isinstance(value, str) else value


def _out(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, HistoryUnread):
        return value.value
    if isinstance(value, list):
        return [v.isoformat() if isinstance(v, datetime) else v for v in value]
    if isinstance(value, tuple):
        return [_t(v) if isinstance(v, str) else v for v in value]
    raise TypeError(value)


class _Script:
    def __init__(self, answers: list[Any], leaves: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.leaves = leaves
        self.counts = [0, 0]
        self.calls: list[Any] = []
        self.failed = 0

    def _next(self) -> Any:
        a = self.answers.pop(0)
        if a == "T":
            self.counts[0] += 1
            return None, True
        if a == "M":
            self.counts[1] += 1
            return None, True
        return a, False

    def git(
        self,
        cwd: Any,
        *args: str,
        timeout: float = 1.0,
        empty_ok: bool = False,
        verbatim: bool = False,
        **_: Any,
    ) -> str | None:
        self.calls.append(["g", [_t(a) for a in args], timeout, verbatim, empty_ok])
        a, _gone = self._next()
        return a

    def git_result(self, cwd: Any, *args: str, timeout: float = 1.0, **_: Any) -> Any:
        self.calls.append(["R", [_t(a) for a in args], timeout, False, False])
        a, gone = self._next()
        if gone or a is None:
            return None
        return subprocess.CompletedProcess(list(args), a[0], a[1], "")

    def note_failed(self) -> None:
        self.failed += 1

    def _gd(self, which: str) -> Any:
        self.calls.append(which)
        return object() if self.leaves.get("gd") == which else None

    def head_unborn(self, gd: Any) -> Any:
        self.calls.append("unborn")
        return self.leaves.get("unborn")

    def packed_names(self, gd: Any, name: str) -> bool:
        self.calls.append(["packed", _t(name)])
        return bool(self.leaves.get("packed"))

    def refusal(self, cwd: Any) -> Any:
        self.calls.append("refusal")
        return object() if self.leaves.get("refusal") else None

    def above(self, cwd: Any) -> bool:
        self.calls.append("above")
        return bool(self.leaves.get("above"))

    def run(self, call: Any) -> Any:
        with (
            mock.patch.object(origin, "_git", self.git),
            mock.patch.object(origin, "_git_result", self.git_result),
            mock.patch.object(origin_history, "git_counts", lambda: tuple(self.counts)),
            mock.patch.object(git_runner, "note_failed", self.note_failed),
            mock.patch.object(githead, "gitdir_at", lambda cwd: self._gd("at")),
            mock.patch.object(githead, "locate", lambda cwd, **kw: self._gd("loc")),
            mock.patch.object(githead, "head_unborn", self.head_unborn),
            mock.patch.object(githead, "packed_names", self.packed_names),
            mock.patch.object(githead, "entry_above", self.above),
            mock.patch.object(gitfile_rule, "discovery_refusal", self.refusal),
        ):
            return call()


def _case(
    f: str, g: list[Any], lv: dict[str, Any], call: Any, **row: Any
) -> dict[str, Any]:
    s = _Script(g, lv)
    r = s.run(call)
    assert not s.answers, (f, row)
    spelled = [[a[0], _t(a[1])] if isinstance(a, list) else _spell(a) for a in g]
    return {
        "f": f,
        **row,
        "g": spelled,
        "l": lv,
        "r": _out(r),
        "c": s.calls,
        "n": s.failed,
    }


def _reads() -> Iterator[dict[str, Any]]:
    from pathlib import Path

    cwd = Path("R")
    ok = {"gd": None}
    uncut: list[tuple[str, list[Any]]] = [
        ("HEAD", ["100\n50\n"]),
        ("HEAD", ["5"]),
        ("HEAD", ["5\r\n"]),
        ("HEAD", [" 7 \n0\n"]),
        ("HEAD", ["253402300799\n253402300800\n99999999999999999999\n"]),
        ("HEAD", ["0\n"]),
        ("HEAD", [""]),
        ("HEAD", ["\n"]),
        ("HEAD", ["5\n\n"]),
        ("HEAD", ["12x\n"]),
        ("HEAD", ["-5\n"]),
        ("HEAD", ["+5\n"]),
        ("HEAD", ["٥\n"]),
        ("HEAD", ["5\udcff\n"]),
        ("HEAD", ["T"]),
        ("HEAD", ["M"]),
        (A, ["T"]),
        (A, ["M"]),
        (A, [None]),
        (D, [None]),
        (UPPER, [None, "T"]),
        ("main", [None, None]),
        ("HEAD", [None, ".git", ".git"]),
        (F, [None]),
        (G, [None, None]),
        (TICK, [None, None]),
        ("g" * 64, [None, None]),
    ]
    for rev, g in uncut:
        yield _case(
            "read",
            g,
            ok,
            lambda rev=rev: origin_dates._read_author_timestamps(cwd, rev),
            rev=_t(rev),
            cut=[],
        )
    cut1 = frozenset({C})
    cut2 = frozenset({C, D})
    cuts: list[tuple[frozenset[str], list[Any]]] = [
        (cut1, [f"{A} 100\n{B} 50\n"]),
        (cut1, [f"{A} 100\n{C} 50\n"]),
        (cut1, [f"{C} 50\n{A} x\n"]),
        (cut2, [f"{D} 7\n"]),
        (cut1, [""]),
        (cut1, [f"{A}  5\n"]),
        (cut1, [f"{A}\n"]),
        (cut1, [f"{A} \n"]),
        (cut1, [f"{UPPER} 5\n"]),
        (cut1, [f"{A[:39]} 5\n"]),
        (cut1, [f"{A} 99999999999999999999\n"]),
        (cut1, ["T"]),
        (cut1, [None, "M"]),
    ]
    for cut, g in cuts:
        yield _case(
            "read",
            g,
            ok,
            lambda cut=cut: origin_dates._read_author_timestamps(cwd, "HEAD", cut),
            rev="HEAD",
            cut=sorted(cut),
        )
    leaves = [
        {"refusal": True, "above": True},
        {"refusal": False, "above": True},
        {"refusal": False, "above": False},
    ]
    for lv in leaves:
        yield _case("refused", [None], lv, lambda: origin_dates._refused_head(cwd))
        yield _case("entry", [], lv, lambda: origin_dates._unread_entry(cwd))
    for g in (["T"], ["M"], [".git", "T"], [".git", "M"], [".git", "x"], ["", ""]):
        yield _case("refused", g, {}, lambda: origin_dates._refused_head(cwd))
    unborn: list[tuple[dict[str, Any], list[Any]]] = [
        ({"gd": "at", "unborn": True}, []),
        ({"gd": "at", "unborn": False}, []),
        ({"gd": "loc", "unborn": True}, []),
        ({"gd": "loc", "unborn": False}, []),
        ({"gd": "at", "unborn": None}, ["refs/heads/main", [2, ""]]),
        ({"gd": "at", "unborn": None, "packed": True}, ["refs/heads/main", [2, ""]]),
        ({"gd": None, "packed": True}, ["refs/heads/main", [2, ""]]),
        ({"gd": None}, ["refs/heads/main", [1, ""]]),
        ({"gd": None}, ["refs/heads/main", [0, ""]]),
        ({"gd": None}, ["refs/heads/main", [128, ""]]),
        ({"gd": None}, ["refs/heads/main", "T"]),
        ({"gd": None}, ["refs/heads/main", "M"]),
        ({"gd": None}, ["HEAD"]),
        ({"gd": None}, ["refs"]),
        ({"gd": None}, ["refs/", [2, ""]]),
        ({"gd": None}, ["refs/\udcff", [2, ""]]),
        ({"gd": None}, [None]),
        ({"gd": None}, ["T"]),
        ({"gd": None}, ["M"]),
    ]
    for lv, g in unborn:
        yield _case("unborn", g, lv, lambda: origin_dates._unborn_or_broken(cwd))
    yield _case(
        "refused",
        [".git", None, "refs/heads/x", [2, ""]],
        {"gd": None},
        lambda: origin_dates._refused_head(cwd),
    )
    yield _case(
        "refused",
        [".git", None],
        {"gd": "at", "unborn": True},
        lambda: origin_dates._refused_head(cwd),
    )
    ext: list[tuple[str, list[int], str, list[str], list[Any]]] = [
        (A, [10, 30], B, [], [[0, f">{B} 20\n>{C} 40\n-{A} 10\n"]]),
        (A, [10, 30], B, [], [[0, f"<{B} 5\n-{A} 99\n"]]),
        (A, [10], B, [], [[0, f"-{A} 10\n"]]),
        (A, [], B, [], [[0, f">{B} 20\n-{C} 10\n"]]),
        (A, [10], B, [], [[0, f">{B} 20\n"]]),
        (A, [10], B, [], [[0, ""]]),
        (A, [10], B, [], [[1, f"-{A} 10\n"]]),
        (A, [10], B, [], [[128, ""]]),
        (A, [10], B, [], ["T"]),
        (A, [10], B, [], ["M"]),
        (A, [10], B, [], [[0, f"+{B} 20\n-{A} 10\n"]]),
        (A, [10], B, [], [[0, f">{B} x\n-{A} 10\n"]]),
        (A, [10], B, [], [[0, f"-{A} x\n"]]),
        (A, [10], B, [], [[0, f"\n-{A} 10\n"]]),
        (A, [10], B, [C], [[0, f">{C} 20\n-{A} 10\n"]]),
        (A, [10], B, [C], [[0, f">{B} 20\n-{A} 10\n-{C} 5\n"]]),
        ("HEAD", [10], B, [], [[0, ">" + B + " 20\n-HEAD 0\n"]]),
        (A, [10], B, [], [[0, f">{B} 99999999999999999999\n-{A} 1\n"]]),
        (A, [5], "x\udcff", [], [[0, f"-{A} 1\n"]]),
        (
            A,
            [10, 30, 50],
            B,
            [],
            [[0, f">{B} 40\n>{C} 30\n<{B} 5\n>{C} 60\n>{B} 10\n-{A} 0\n"]],
        ),
        (A, [30], B, [], [[0, f">{B} 30\n>{C} 30\n-{A} 0\n"]]),
        (
            A,
            [10, 20],
            B,
            [],
            [[0, f">{B} 99999999999999999999\n>{C} 15\n>{B} 0\n-{A} 0\n"]],
        ),
        (A, [], B, [], [[0, f">{B} 3\n>{C} 2\n>{B} 1\n-{A} 0\n"]]),
    ]
    for base_head, base, head, cut_list, g in ext:
        dates = origin_history.author_dates(base)
        yield _case(
            "extended",
            g,
            {},
            lambda bh=base_head, d=dates, h=head, c=frozenset(cut_list): (
                origin_dates._extended_author_timestamps(cwd, bh, d, h, c)
            ),
            base_head=_t(base_head),
            base=base,
            head=_t(head),
            cut=cut_list,
        )
    lines = [
        "",
        " ",
        f"{A} 5",
        f"{A} 5 6",
        f"{A}  5",
        f"{A} ",
        A,
        f" {A} 5",
        f"{D} 0",
        f"{UPPER} 5",
        f"{A}5",
        f"{A} \t5\t",
        f"{A} 5\udcff",
        f">{A} 5",
        f"<{A} 5",
        f"-{A} 5",
        f"-{A} x",
        f"+{A} 5",
        f"-{A}",
        ">",
        "-",
        f"> {A} 5",
        f"\u0001{A} 5",
        f">{A} 253402300800",
        f"{F} 5",
        f"{G} 5",
        f"{TICK} 5",
        f">{F} 5",
        f">{G} 5",
        f"-{TICK} 5",
        f"{'f' * 64} 5",
        f"{'g' * 64} 5",
    ]
    for line in lines:
        yield {
            "f": "hashed",
            "line": _t(line),
            "r": _out(origin_dates._hashed_seconds(line)),
        }
        yield {
            "f": "marked",
            "line": _t(line),
            "r": _out(origin_dates._marked_seconds(line)),
        }


def _file() -> bytes:
    header = {
        "probes": [list(p) for p in origin_dates._HEAD_PROBES],
        "walk_mark": origin._WALK_MARK,
    }
    return jsonl([header, *_reads()])


def generate() -> dict[str, bytes]:
    return {"origin_dates_reads.jsonl": _file()}
