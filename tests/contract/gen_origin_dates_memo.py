"""Fixtures: `origin_dates`' memoised reader (``origin_dates_memo.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_dates/memo.rs``. This pins it
to `_instant`, `commit_author_timestamps` (under
`git_read_budget.unread`) and `_remember_dates`.

No git runs and no file is read. Every leaf is the case's: git
(``origin._git``, ``origin._git_result``, the thread's counts,
``_git.note_failed`` and ``note_timed_out``), the files git's discovery
reads (as in ``origin_dates_reads.jsonl``),
``origin.toplevel_and_head_from_files``, ``origin_history.located_key``,
``history_guard`` and ``cuts``, the hold on the shallow file
(``_hold_shallow`` and ``_shallow_held``) and ``time.monotonic``. The
leaves are pinned by their own units.

A ``scenario`` row is a sequence of calls over one memo, cleared before
the first. Each step holds:

- ``cwd`` (``null`` for None), ``located`` (where passed), ``now``;
- ``g``: git's answers in order, as in ``origin_dates_reads.jsonl``, and
  ``"D"``, a call that raises ``ReadDeadline``; ``{"rep": line, "n": n}``
  is ``line`` repeated ``n`` times;
- ``l``: the leaves: ``files`` and ``lk`` (a key or ``null``),
  ``guard``, ``cuts`` (answers in order: a list of hashes, ``null`` or
  ``"VE"`` for a ValueError), ``hold``, ``held`` (answers in order), and
  ``gd``, ``unborn``, ``packed``, ``refusal``, ``above``;
- ``r``: the answer (ISO dates, a ``HistoryUnread`` value, ``null``, or
  ``"ValueError"``); ``c``: the calls in order; ``n`` and ``t``: the
  failures and timeouts noted; ``m``: the memo after the call, its
  entries ``[root, head, dates, guard, cut]`` and failures ``[root,
  head, at, guard, why]``, oldest first.

An ``instant`` row pins `_instant` of an ISO date as the big-endian bits
of the float; the ``instant_sweep`` row pins it over seeded UTC instants
(the rule in `_sweep`) by count and SHA-256. Every row holds on every
platform and Python release: the file is not gated.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Any
from unittest import mock

from bettermemory import _git as git_runner
from bettermemory import (
    git_read_budget,
    gitfile_rule,
    githead,
    origin,
    origin_dates,
    origin_history,
)
from bettermemory.origin_history import HistoryUnread

from ._common import jsonl

A = "a" * 40
B = "b" * 40
C = "c" * 40
S = "5" * 40
ROOT = "/r1"
OTHER = "/r2"


def _expand(answers: list[Any]) -> list[Any]:
    return [a["rep"] * a["n"] if isinstance(a, dict) else a for a in answers]


def _out(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, HistoryUnread):
        return value.value
    if isinstance(value, list):
        return [v.isoformat() for v in value]
    raise TypeError(value)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _Step:
    def __init__(self, step: dict[str, Any]) -> None:
        self.answers = _expand(step.get("g", []))
        self.leaves = step.get("l", {})
        self.cuts = list(self.leaves.get("cuts", []))
        self.held = list(self.leaves.get("held", []))
        self.calls: list[Any] = []
        self.failed = 0
        self.timed = 0

    def _next(self) -> Any:
        a = self.answers.pop(0)
        if a == "D":
            raise git_read_budget.ReadDeadline()
        return a

    def git(self, cwd: Any, *args: str, timeout: float = 1.0, **kw: Any) -> Any:
        self.calls.append(
            [
                "g",
                list(args),
                timeout,
                kw.get("verbatim", False),
                kw.get("empty_ok", False),
            ]
        )
        a = self._next()
        if a == "T":
            self.counts[0] += 1
            return None
        if a == "M":
            self.counts[1] += 1
            return None
        return a

    def git_result(self, cwd: Any, *args: str, timeout: float = 1.0, **_: Any) -> Any:
        self.calls.append(["R", list(args), timeout, False, False])
        a = self._next()
        if a == "T":
            self.counts[0] += 1
            return None
        if a == "M":
            self.counts[1] += 1
            return None
        if a is None:
            return None
        return subprocess.CompletedProcess(list(args), a[0], a[1], "")

    def note_failed(self) -> None:
        self.failed += 1

    def note_timed_out(self) -> None:
        self.timed += 1

    def _gd(self, which: str) -> Any:
        self.calls.append(which)
        return object() if self.leaves.get("gd") == which else None

    def files(self, cwd: Any) -> Any:
        self.calls.append("files")
        v = self.leaves.get("files")
        return None if v is None else (PurePosixPath(v[0]), v[1])

    def located_key(self, cwd: Any) -> Any:
        self.calls.append("lk")
        v = self.leaves.get("lk")
        return None if v is None else (v[0], v[1])

    def guard(self, root: Path) -> Any:
        self.calls.append(["guard", _posix(root)])
        return self.leaves.get("guard")

    def cut(self, start: Path, *, walk: bool = False) -> Any:
        self.calls.append(["cuts", _posix(start), walk])
        a = self.cuts.pop(0)
        if a == "VE":
            raise ValueError("embedded null byte")
        return None if a is None else frozenset(a)

    def hold(self, start: Path, *, walk: bool = False) -> Any:
        self.calls.append(["hold", _posix(start), walk])
        return self.leaves.get("hold")

    def shallow_held(
        self, start: Path, cut: frozenset[str], held: Any, *, walk: bool = False
    ) -> bool:
        self.calls.append(["held", _posix(start), sorted(cut), held, walk])
        return bool(self.held.pop(0))

    def run(self, clock: _Clock, call: Any) -> Any:
        self.counts = [0, 0]
        lv = self.leaves
        with (
            mock.patch.object(origin, "_git", self.git),
            mock.patch.object(origin, "_git_result", self.git_result),
            mock.patch.object(origin, "toplevel_and_head_from_files", self.files),
            mock.patch.object(origin_history, "git_counts", lambda: tuple(self.counts)),
            mock.patch.object(origin_history, "located_key", self.located_key),
            mock.patch.object(origin_history, "history_guard", self.guard),
            mock.patch.object(origin_history, "cuts", self.cut),
            mock.patch.object(origin_history, "time", clock),
            mock.patch.object(origin_dates, "_hold_shallow", self.hold),
            mock.patch.object(origin_dates, "_shallow_held", self.shallow_held),
            mock.patch.object(git_runner, "note_failed", self.note_failed),
            mock.patch.object(git_runner, "note_timed_out", self.note_timed_out),
            mock.patch.object(githead, "gitdir_at", lambda cwd: self._gd("at")),
            mock.patch.object(githead, "locate", lambda cwd, **kw: self._gd("loc")),
            mock.patch.object(
                githead,
                "head_unborn",
                lambda gd: self._leaf("unborn", lv.get("unborn")),
            ),
            mock.patch.object(
                githead,
                "packed_names",
                lambda gd, name: self._leaf(["packed", name], bool(lv.get("packed"))),
            ),
            mock.patch.object(
                githead,
                "entry_above",
                lambda cwd: self._leaf("above", bool(lv.get("above"))),
            ),
            mock.patch.object(
                gitfile_rule,
                "discovery_refusal",
                lambda cwd: self._leaf(
                    "refusal", object() if lv.get("refusal") else None
                ),
            ),
        ):
            try:
                return call()
            except ValueError:
                return "ValueError"

    def _leaf(self, name: Any, value: Any) -> Any:
        self.calls.append(name)
        return value


def _posix(p: PurePath) -> str:
    """A root or start as the rows spell it on every host: the reference
    rebuilds it with the host's `Path`, which spells `/r1` as `\\r1` on
    Windows, so the doubles record its POSIX form."""
    return p.as_posix()


def _memo() -> Any:
    entries = [
        [root, head, _out(e.dates), e.guard, sorted(e.cut)]
        for (root, head), e in origin_history._TIMESTAMPS_MEMO.items()
    ]
    failed = [
        [root, head, at, guard, why.value]
        for (root, head), (at, guard, why) in origin_history._TIMESTAMPS_FAILED.items()
    ]
    return {"e": entries, "f": failed}


def _scenario(name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    origin_history._clear_timestamps_memo()
    clock = _Clock()
    done = []
    for step in steps:
        clock.now = step["now"]
        s = _Step(step)
        cwd = None if step.get("cwd") is None else Path(step["cwd"])
        kw: dict[str, Any] = {}
        if "located" in step:
            kw["located"] = (PurePosixPath(step["located"][0]), step["located"][1])
        r = s.run(clock, lambda: origin_dates.commit_author_timestamps(cwd, **kw))
        assert not s.answers and not s.cuts and not s.held, (name, step)
        done.append(
            {
                **step,
                "r": r if r == "ValueError" else _out(r),
                "c": s.calls,
                "n": s.failed,
                "t": s.timed,
                "m": _memo(),
            }
        )
    origin_history._clear_timestamps_memo()
    return {"f": "scenario", "name": name, "steps": done}


def _st(now: float, g: list[Any], cwd: Any = "W", **leaves: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"cwd": cwd, "now": now, "g": g, "l": leaves}
    return step


KEYED = {"files": [ROOT, A], "guard": "g1", "hold": "h1"}


def _keyed(**over: Any) -> dict[str, Any]:
    return {**KEYED, **over}


def _scenarios() -> Iterator[dict[str, Any]]:
    ext = f">{B} 300\n-{A} 0\n"
    yield _scenario("none", [_st(0.0, [], cwd=None)])
    yield _scenario(
        "fresh then hit",
        [
            _st(1.0, ["200\n100\n"], **_keyed(cuts=[[]], held=[True])),
            _st(2.0, [], **_keyed(cuts=[[]])),
        ],
    )
    yield _scenario(
        "located passed",
        [
            {
                **_st(1.0, ["5\n"], guard=None, hold=None, cuts=[[]], held=[True]),
                "located": [OTHER, C],
            }
        ],
    )
    yield _scenario(
        "located key",
        [
            _st(
                1.0,
                ["7\n"],
                files=None,
                lk=[OTHER, B],
                guard="g",
                hold="h",
                cuts=[[]],
                held=[True],
            )
        ],
    )
    yield _scenario(
        "extended after a new commit",
        [
            _st(1.0, ["200\n100\n"], **_keyed(cuts=[[]], held=[True])),
            _st(2.0, [[0, ext]], **_keyed(files=[ROOT, B], cuts=[[]], held=[True])),
            _st(3.0, [], **_keyed(files=[ROOT, B], cuts=[[]])),
        ],
    )
    yield _scenario(
        "extension refused reads whole",
        [
            _st(1.0, ["100\n"], **_keyed(cuts=[[]], held=[True])),
            _st(
                2.0,
                [[128, ""], "50\n"],
                **_keyed(files=[ROOT, B], cuts=[[]], held=[True]),
            ),
        ],
    )
    yield _scenario(
        "extension reaches a cut",
        [
            _st(1.0, [f"{A} 100\n"], **_keyed(cuts=[[S]], held=[True])),
            _st(
                2.0, [[0, f">{S} 300\n-{A} 0\n"]], **_keyed(files=[ROOT, B], cuts=[[S]])
            ),
            _st(3.0, [], **_keyed(files=[ROOT, B], cuts=[[S]])),
        ],
    )
    yield _scenario(
        "no base without guard",
        [
            _st(1.0, ["100\n"], **_keyed(guard=None, cuts=[[]], held=[True])),
            _st(
                2.0,
                ["9\n"],
                **_keyed(guard=None, files=[ROOT, B], cuts=[[]], held=[True]),
            ),
        ],
    )
    yield _scenario(
        "shallow deepened",
        [
            _st(1.0, [f"{A} 100\n"], **_keyed(cuts=[[S]], held=[True])),
            _st(2.0, [], **_keyed(cuts=[[S]])),
            _st(3.0, [f"{A} 100\n"], **_keyed(cuts=[[S, C]], held=[True])),
            _st(4.0, [], **_keyed(cuts=[[]])),
        ],
    )
    yield _scenario(
        "shallow file unread",
        [
            _st(1.0, [], **_keyed(cuts=[None])),
            _st(2.0, [], **_keyed(cuts=[None])),
            _st(3.0, [], **_keyed(guard="g2", cuts=[None])),
        ],
    )
    yield _scenario(
        "history reaches a cut",
        [
            _st(1.0, [f"{S} 100\n"], **_keyed(cuts=[[S]])),
            _st(30.0, [], **_keyed(cuts=[[S]])),
            _st(61.0, [f"{A} 100\n"], **_keyed(cuts=[[S]], held=[True])),
        ],
    )
    yield _scenario(
        "cut moved during the log",
        [
            _st(1.0, ["100\n"], **_keyed(cuts=[[]], held=[False])),
            _st(2.0, ["100\n"], **_keyed(cuts=[[]], held=[True])),
        ],
    )
    yield _scenario(
        "failure remembered then expired",
        [
            _st(10.0, ["T"], **_keyed(cuts=[[]])),
            _st(20.0, [], **_keyed(cuts=[[]])),
            _st(69.999, [], **_keyed(cuts=[[]])),
            _st(70.0, ["M"], **_keyed(cuts=[[]])),
            _st(71.0, ["100\n"], **_keyed(cuts=[[]], held=[True])),
        ],
    )
    yield _scenario(
        "failure under another guard",
        [
            _st(10.0, ["x\n"], **_keyed(cuts=[[]])),
            _st(11.0, ["100\n"], **_keyed(guard="g2", cuts=[[]], held=[True])),
        ],
    )
    yield _scenario(
        "clock went back",
        [
            _st(10.0, [None], **_keyed(cuts=[[]])),
            _st(9.0, ["100\n"], **_keyed(cuts=[[]], held=[True])),
        ],
    )
    yield _scenario(
        "over budget",
        [
            _st(
                1.0,
                [{"rep": "1\n", "n": origin_history._TIMESTAMPS_MEMO_SLOTS + 1}],
                **_keyed(cuts=[[]], held=[True]),
            ),
            _st(2.0, [], **_keyed(cuts=[[]])),
        ],
    )
    yield _scenario(
        "deadline",
        [
            _st(1.0, ["D"], **_keyed(cuts=[[]])),
            _st(2.0, ["100\n"], **_keyed(cuts=[[]], held=[True])),
            _st(3.0, ["D"], **_keyed(files=[ROOT, B], cuts=[[]])),
            _st(4.0, ["D"], files=None, lk=None, cuts=[[]], hold="h"),
        ],
    )
    yield _scenario(
        "value error",
        [
            _st(1.0, [], **_keyed(cuts=["VE"])),
            _st(2.0, [], files=None, lk=None, cuts=["VE"]),
        ],
    )
    # No key: the history from HEAD, never kept.
    head = {"files": None, "lk": None}
    yield _scenario(
        "head path",
        [
            _st(1.0, ["100\n"], **head, cuts=[[]], hold="h", held=[True]),
            _st(2.0, ["100\n"], **head, cuts=[[]], hold=None, held=[False]),
            _st(3.0, [f"{A} 100\n"], **head, cuts=[[S]], hold="h", held=[True]),
            _st(4.0, [f"{S} 100\n"], **head, cuts=[[S]], hold="h"),
            _st(5.0, ["T"], **head, cuts=[[]], hold="h"),
            _st(
                6.0,
                [None, "x", None],
                **head,
                cuts=[[]],
                hold="h",
                gd="at",
                unborn=True,
            ),
            _st(7.0, [None, None], **head, cuts=[[]], hold="h", above=True),
        ],
    )
    yield _scenario(
        "head path unread shallow",
        [
            _st(1.0, [], **head, cuts=[None], gd="at"),
            _st(2.0, [], **head, cuts=[None], gd="loc"),
            _st(3.0, [], **head, cuts=[None]),
            _st(4.0, [], **head, cuts=[None], above=True),
            _st(5.0, [], **head, cuts=[None], above=True, refusal=True),
        ],
    )
    many = [
        _st(
            float(i),
            [f"{i + 1}\n"],
            **_keyed(files=[ROOT, f"{i:040x}"], guard=i, cuts=[[]], held=[True]),
        )
        for i in range(origin_history._TIMESTAMPS_MEMO_CAP + 2)
    ]
    yield _scenario("eviction", many)
    fails = [
        _st(float(i), ["x\n"], **_keyed(files=[ROOT, f"{i:040x}"], cuts=[[]]))
        for i in range(origin_history._TIMESTAMPS_MEMO_CAP + 2)
    ]
    yield _scenario("failure eviction", fails)


INSTANTS = [
    "1970-01-01T00:00:00+00:00",
    "1970-01-01T00:00:00.000001+00:00",
    "1969-12-31T23:59:59.999999+00:00",
    "0001-01-01T00:00:00+00:00",
    "0001-01-01T00:00:00+23:59",
    "9999-12-31T23:59:59.999999+00:00",
    "9999-12-31T23:59:59.999999-23:59",
    "2026-11-21T12:34:56.789012+05:30",
    "2001-09-09T01:46:40.123457+00:00",
    "2255-06-05T23:47:34.740992+00:00",
    "2255-06-05T23:47:34.740993+00:00",
]

SWEEP_SEED = 20261121
SWEEP_COUNT = 4000
_LO = -62_135_596_800_000_000
_SPAN = 253_402_300_799_999_999 - _LO + 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _bits(x: float) -> str:
    return struct.pack(">d", x).hex()


def _sweep() -> list[int]:
    """Microseconds since the epoch: a 64-bit LCG from the seed, each state
    taken modulo the span of a datetime's instants, plus the low bound."""
    x = SWEEP_SEED
    out = []
    for _ in range(SWEEP_COUNT):
        x = (x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        out.append(_LO + x % _SPAN)
    return out


def _instants() -> Iterator[dict[str, Any]]:
    for text in INSTANTS:
        stamp = datetime.fromisoformat(text)
        yield {"f": "instant", "at": text, "r": _bits(origin_dates._instant(stamp))}
    h = hashlib.sha256()
    for us in _sweep():
        stamp = _EPOCH + timedelta(microseconds=us)
        h.update(bytes.fromhex(_bits(origin_dates._instant(stamp))))
    yield {
        "f": "instant_sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "sha256": h.hexdigest(),
    }


def generate() -> dict[str, bytes]:
    return {"origin_dates_memo.jsonl": jsonl([*_instants(), *_scenarios()])}
