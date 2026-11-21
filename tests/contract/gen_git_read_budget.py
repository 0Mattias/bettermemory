"""Fixtures: the shared Git-read deadline and the git call tallies
(``git_read_budget.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/git_read_budget.rs`` and
``native/crates/bettermemory-core/src/git_calls.rs``. This pins it to
`git_read_budget`, the whole module, and to `_git`'s tallies,
`printed_path`, `subcommand`, the counts `run_git` adds around one call
and the message `log_nonzero_exit` logs.

One JSON object per line, keys sorted, ASCII, not gated. The first line
is the header (``{"k": "header", "cases": n, "read_seconds": ...}``).
Every other line is one of:

- ``{"k": "prog", "clock": [...], "ops": [...], "out": [...], "n": n}``:
  a program run in a fresh context with a fresh `_PROGRESS`, `_clock`
  answering the scripted values in turn (``n`` reads were made). Each op
  appends its events to ``out``; an op that raises appends
  ``["raise", class]`` where the exception leaves the program, and the
  program stops there. Ops:
  - ``["scope", store, name, body]``: `call_scope`;
  - ``["call", store, body]``: a `call` (and an `async_call`, in
    ``"acall"``) of a function named ``m.f`` whose first argument has the
    store (null: no first argument);
  - ``["health", limit, body]``: `health_call` with ``estate_root_limit``
    absent (``"-"``), None (null) or a number;
  - ``["read", root, home, body]``: `reading`;
  - ``["unread", first, body]`` and ``["hit", first, body]``: the
    decorated reader, its first argument ``[cwd, at_home]`` or null;
    ``out`` gets ``["unread", value]`` or ``["hit", [flag, value]]``;
  - ``["allow", timeout, cwd, git_dir]``: module `allowance`;
  - ``["expired"]``, ``["exh"]``: module `expired`, `exhausted`;
  - ``["wait", body]``: `wait_for` over a lock that grants; ``out`` gets
    ``["wait", acquired, timeouts asked]`` (``"-"`` for no timeout);
  - ``["raise", "D" or "V"]``: ReadDeadline or ValueError;
  - ``["resume", rows]``: `for_resume`, a row's root itself;
  - ``["set", field, value]``: a field of the current budget;
  - ``["ballow", requested, root, home]``, ``["bexh"]``,
    ``["bfinish"]``: the current budget's methods;
  - ``["state"]``: the current budget's fields, the root, and whether
    the active budget is the owner;
  - ``["progress", store, name]``: the resume `_PROGRESS` holds;
  - ``["git", timeout, cwd, git_dir, spawn, isdir]``: `run_git` with
    `subprocess.run` raising (``"nf"`` FileNotFoundError, ``"to"``
    TimeoutExpired, ``"os"`` OSError, ``"v"`` ValueError) or returning
    (``"ok"``) and `os.path.isdir` answering ``isdir``; ``out`` gets the
    timeout given, the answer and the counts' moves.
  A store is null, ``"u"`` (a list: unhashable), ``"str"`` (a str: no
  weak reference) or a label, one object per label in the program. A
  float is its `repr`.
- ``{"k": "notes", "notes": [...], "o": [failed, timed_out]}``:
  `note_failed` (``"f"``) and `note_timed_out` (``"t"``) in turn.
- ``{"k": "printed", "t": text, "win": bool, "o": text}``.
- ``{"k": "sub", "a": args, "o": text}``.
- ``{"k": "nonzero", "a": args, "rc": n, "w": where, "e": stderr, "o":
  message}``: stderr as text, or as hex bytes under ``"eb"``.
"""

from __future__ import annotations

import contextvars
import subprocess
from typing import Any
from unittest import mock
from weakref import WeakKeyDictionary

from bettermemory import _git, git_read_budget as grb

from ._common import jsonl


def _f(x: Any) -> Any:
    return repr(x) if isinstance(x, float) else x


def _root(r: Any) -> Any:
    return None if r is None else [r[0], r[1]]


class _Run:
    def __init__(self, clock: list[str]) -> None:
        self.clock = [float(c) for c in clock]
        self.n = 0
        self.out: list[Any] = []
        self.stores: dict[str, Any] = {}

    def tick(self) -> float:
        value = self.clock[self.n]
        self.n += 1
        return value

    def store(self, label: Any) -> Any:
        if label is None:
            return None
        if label == "u":
            return []
        if label == "str":
            return "store"

        class Store:
            pass

        return self.stores.setdefault(label, Store())

    def body(self, ops: list[Any]) -> None:
        for op in ops:
            self.op(op)

    def current(self) -> grb.ReadBudget | None:
        return grb._ACTIVE.get() or grb._OWNER.get()

    def op(self, op: list[Any]) -> None:
        kind = op[0]
        out = self.out
        if kind == "scope":
            with grb.call_scope(self.store(op[1]), op[2]):
                self.body(op[3])
        elif kind in ("call", "acall"):
            body = op[2]

            def fn(*args: Any) -> None:
                self.body(body)

            fn.__module__ = "m"
            fn.__qualname__ = "f"
            holder = type("H", (), {"store": self.store(op[1])})()
            args = () if op[1] is None else (holder,)
            if kind == "call":
                grb.call(fn)(*args)
            else:

                async def afn(*args: Any) -> None:
                    self.body(body)

                afn.__module__ = "m"
                afn.__qualname__ = "f"
                coro = grb.async_call(afn)(*args)
                try:
                    coro.send(None)
                except StopIteration:
                    pass
        elif kind == "health":

            def h(**kw: Any) -> None:
                self.body(op[2])

            kw = {} if op[1] == "-" else {"estate_root_limit": op[1]}
            grb.health_call(h)(**kw)
        elif kind == "read":
            with grb.reading(op[1], home=op[2]):
                self.body(op[3])
        elif kind in ("unread", "hit"):
            first = op[1]

            def r(*args: Any) -> Any:
                self.body(op[2])
                return "ok" if kind == "unread" else (False, "ok")

            args = ()
            if first is not None:
                args = (type("A", (), {"cwd": first[0], "at_home": first[1]})(),)
            got: Any = (grb.unread if kind == "unread" else grb.hit)(r)(*args)
            if kind == "unread":
                out.append(["unread", getattr(got, "value", got)])
            else:
                out.append(["hit", [got[0], getattr(got[1], "value", got[1])]])
        elif kind == "allow":
            given, limited = grb.allowance(float(op[1]), op[2], op[3])
            out.append(["allow", _f(given), limited])
        elif kind == "expired":
            out.append(["expired", grb.expired()])
        elif kind == "exh":
            grb.exhausted()
        elif kind == "wait":
            asked: list[Any] = []

            class Lock:
                def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                    asked.append("-" if timeout == -1 else _f(timeout))
                    return True

                def release(self) -> None:
                    asked.append("rel")

            with grb.wait_for(Lock()) as held:  # type: ignore[arg-type]
                self.body(op[1])
            out.append(["wait", held, asked])
        elif kind == "raise":
            raise grb.ReadDeadline() if op[1] == "D" else ValueError("v")
        elif kind == "resume":
            out.append(["resume", grb.for_resume(op[1], lambda row: row)])
        elif kind == "set":
            budget = self.current()
            assert budget is not None
            value = op[2]
            if op[1] == "deadline" and value is not None:
                value = float(value)
            elif op[1] in ("last_root", "deferred", "resume") and value is not None:
                value = (value[0], value[1])
            setattr(budget, op[1], value)
        elif kind == "ballow":
            budget = self.current()
            assert budget is not None
            given, limited = budget.allowance(float(op[1]), op[2], op[3])
            out.append(["ballow", _f(given), limited])
        elif kind == "bexh":
            budget = self.current()
            assert budget is not None
            budget.exhausted()
        elif kind == "bfinish":
            budget = self.current()
            assert budget is not None
            budget.finish()
        elif kind == "state":
            b = self.current()
            if b is None:
                out.append(["state", None])
            else:
                out.append(
                    [
                        "state",
                        {
                            "deadline": _f(b.deadline),
                            "first_home": b.first_home,
                            "last_root": _root(b.last_root),
                            "deferred": _root(b.deferred),
                            "resume": _root(b.resume),
                            "progress": b.progress is not None,
                            "root": _root(grb._ROOT.get()),
                            "same": grb._ACTIVE.get() is grb._OWNER.get(),
                            "owner": grb._OWNER.get() is not None,
                            "active": grb._ACTIVE.get() is not None,
                        },
                    ]
                )
        elif kind == "progress":
            try:
                p = grb._PROGRESS.get(self.store(op[1]), {}).get(op[2])
            except TypeError:
                p = None
            out.append(["progress", "absent" if p is None else _root(p.resume)])
        elif kind == "git":
            self.git(op)
        else:
            raise AssertionError(kind)

    def git(self, op: list[Any]) -> None:
        _, timeout, cwd, git_dir, spawn, isdir = op
        given: list[Any] = []

        def run(command: Any, **kw: Any) -> Any:
            given.append(_f(kw["timeout"]))
            if spawn == "nf":
                raise FileNotFoundError(2, "x")
            if spawn == "to":
                raise subprocess.TimeoutExpired(command, kw["timeout"])
            if spawn == "os":
                raise OSError(13, "x")
            if spawn == "v":
                raise ValueError("embedded null byte")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        asked: list[bool] = []

        def is_dir(path: Any) -> bool:
            asked.append(True)
            return bool(isdir)

        before = (
            _git.unanswered_calls(),
            _git.failed_calls(),
            _git.timed_out_calls(),
            _git.missing_calls(),
        )
        answer: Any
        caught: BaseException | None = None
        with (
            mock.patch("subprocess.run", run),
            mock.patch("os.path.isdir", is_dir),
        ):
            try:
                answer = _git.run_git(
                    ["log"], cwd=cwd, git_dir=git_dir, timeout=float(timeout)
                )
                answer = answer is not None
            except (grb.ReadDeadline, ValueError) as exc:
                answer = type(exc).__name__
                caught = exc
        after = (
            _git.unanswered_calls(),
            _git.failed_calls(),
            _git.timed_out_calls(),
            _git.missing_calls(),
        )
        moved = [a - b for a, b in zip(after, before, strict=True)]
        self.out.append(["git", given, answer, moved, len(asked)])
        if caught is not None:
            raise caught


def _program(clock: list[str], ops: list[Any]) -> dict[str, Any]:
    run = _Run(clock)

    def go() -> None:
        try:
            run.body(ops)
        except (grb.ReadDeadline, ValueError, AssertionError) as exc:
            run.out.append(["raise", type(exc).__name__])

    with (
        mock.patch.object(grb, "_clock", run.tick),
        mock.patch.object(grb, "_PROGRESS", WeakKeyDictionary()),
    ):
        contextvars.Context().run(go)
    return {"k": "prog", "clock": clock, "ops": ops, "out": run.out, "n": run.n}


S = ["state"]


def _programs() -> list[tuple[list[str], list[Any]]]:
    p: list[tuple[list[str], list[Any]]] = []
    # Budget allowance: before, at and past the deadline.
    for clock, req in [
        (["0.0"], "1.0"),
        (["0.0"], "5.0"),
        (["0.0"], "7.5"),
        (["10.0", "14.5"], "1.0"),
        (["10.0", "15.0"], "1.0"),
        (["10.0", "16.0"], "1.0"),
        (["0.1", "0.1"], "0.1"),
        (["1e308", "1e308"], "1.0"),
        (["inf"], "1.0"),
        (["nan"], "1.0"),
        (["0.0"], "nan"),
        (["0.0"], "-1.0"),
        (["0.0"], "inf"),
    ]:
        steps = [["ballow", req, "/r", True] for _ in clock]
        p.append((clock, [["read", "/r", True, [*steps, S]]]))
    # Exhausted with first_home, last_root and deferred combinations.
    for first, last, deferred in [
        (None, None, None),
        (None, ["/a", True], None),
        (None, ["/a", False], None),
        ("/a", ["/a", True], None),
        ("/b", ["/a", True], None),
        ("/a", ["/a", True], ["/z", False]),
        ("/a", ["/a", False], None),
    ]:
        p.append(
            (
                [],
                [
                    [
                        "read",
                        "/r",
                        True,
                        [
                            ["set", "first_home", first],
                            ["set", "last_root", last],
                            ["set", "deferred", deferred],
                            S,
                            ["scope", None, "", [["bexh"]]],
                        ],
                    ]
                ],
            )
        )
    # first_home is set only for a home root, only once, and not past.
    p.append(
        (
            ["0.0", "1.0", "2.0", "6.0", "7.0"],
            [
                [
                    "read",
                    "/r",
                    True,
                    [
                        ["ballow", "1.0", "/x", False],
                        S,
                        ["ballow", "1.0", "/h", True],
                        ["ballow", "9.0", "/k", True],
                        S,
                        ["unread", None, [["ballow", "1.0", "/k", True]]],
                        S,
                        ["ballow", "1.0", "/q", True],
                    ],
                ]
            ],
        )
    )
    # Nested scopes and readings.
    p.append(
        (
            [],
            [
                S,
                [
                    "scope",
                    "a",
                    "n",
                    [
                        S,
                        ["set", "deferred", ["/d", True]],
                        ["scope", "b", "other", [S, ["set", "deadline", "3.0"]]],
                        S,
                        ["read", "/one", False, [S, ["read", "/two", True, [S]], S]],
                        S,
                    ],
                ],
                S,
                ["progress", "a", "n"],
                ["progress", "b", "other"],
                ["read", "/solo", True, [S, ["scope", "a", "n", [S]], S]],
                ["progress", "a", "n"],
            ],
        )
    )
    # Progress across two calls; unhashable and str stores keep none.
    for store in ["a", "u", "str", None]:
        p.append(
            (
                ["0.0", "6.0", "20.0"],
                [
                    [
                        "call",
                        store,
                        [
                            S,
                            ["read", "/h1", True, [["ballow", "1.0", "/h1", True]]],
                            ["read", "/h2", True, [["ballow", "1.0", "/h2", True]]],
                        ],
                    ],
                    ["raise", "V"],
                ],
            )
        )
        p.append(
            (
                ["0.0", "6.0"],
                [
                    [
                        "call",
                        store,
                        [
                            [
                                "unread",
                                ["/h1", True],
                                [
                                    ["allow", "1.0", None, None],
                                    ["allow", "1.0", None, None],
                                ],
                            ]
                        ],
                    ],
                    ["progress", store, "m.f"],
                    ["acall", store, [S, ["resume", ["/h0", "/h1", "/h2", "/h1"]]]],
                    ["progress", store, "m.f"],
                    ["call", store, [S]],
                ],
            )
        )
    # finish writes resume even when the body raises.
    p.append(
        (
            [],
            [
                [
                    "scope",
                    "a",
                    "x",
                    [["set", "deferred", ["/q", False]], ["raise", "V"]],
                ],
                ["progress", "a", "x"],
            ],
        )
    )
    p.append(
        (
            [],
            [
                ["scope", "a", "x", [["set", "deferred", ["/q", True]]]],
                ["scope", "a", "x", [S, ["bfinish"], ["set", "deferred", None]]],
                ["progress", "a", "x"],
                ["scope", "a", "y", [S]],
            ],
        )
    )
    # health_call.
    for limit in ["-", None, 3, 0]:
        p.append(([], [["health", limit, [S]]]))
    p.append(([], [["scope", "a", "z", [["health", None, [S]], ["health", "-", [S]]]]]))
    # expired before, at and past a deadline; none started; no budget.
    p.append(
        (
            ["0.0", "4.0", "5.0", "6.0"],
            [
                ["expired"],
                [
                    "scope",
                    None,
                    "",
                    [
                        ["expired"],
                        ["read", "/r", True, [["ballow", "1.0", "/r", True]]],
                        ["expired"],
                        ["expired"],
                        ["expired"],
                    ],
                ],
            ],
        )
    )
    p.append(
        (["7.0"], [["read", "/r", True, [["set", "deadline", "nan"], ["expired"]]]])
    )
    p.append(
        (["7.0"], [["read", "/r", True, [["set", "deadline", "7.0"], ["expired"]]]])
    )
    # A call_scope inside a reading with no owner: the active budget (the
    # reading's) comes before the owner (the new call budget).
    p.append(
        (
            ["1.0"],
            [
                [
                    "read",
                    "/r",
                    True,
                    [["set", "deadline", "0.0"], ["scope", None, "", [["expired"]]]],
                ]
            ],
        )
    )
    # wait_for: no owner, time left, none left, a reading alone.
    p.append(([], [["wait", [S]]]))
    p.append((["2.0"], [["scope", None, "", [["wait", [S]]]]]))
    p.append(
        (["9.0"], [["scope", None, "", [["set", "deadline", "9.0"], ["wait", [S]]]]])
    )
    p.append(
        (["9.5"], [["scope", None, "", [["set", "deadline", "9.0"], ["wait", [S]]]]])
    )
    p.append(
        (["nan"], [["scope", None, "", [["set", "deadline", "9.0"], ["wait", []]]]])
    )
    p.append(([], [["read", "/r", True, [["wait", []]]]]))
    p.append((["1.0"], [["scope", None, "", [["wait", [["raise", "V"]]]]]]))
    # for_resume edges.
    rows = ["/a", None, "/b", "/a", "/c"]
    for resume in [None, ["/a", False], ["/a", True], ["/c", True], ["/x", False]]:
        p.append(
            (
                [],
                [
                    ["resume", rows],
                    [
                        "scope",
                        None,
                        "",
                        [
                            ["set", "resume", resume],
                            ["resume", rows],
                            ["resume", []],
                            ["read", "/r", True, [["resume", rows]]],
                        ],
                    ],
                ],
            )
        )
    p.append(
        (
            [],
            [
                [
                    "read",
                    "/r",
                    True,
                    [["set", "resume", ["/a", False]], ["resume", rows]],
                ]
            ],
        )
    )
    # unread and hit fallbacks; other errors pass.
    for kind in ["unread", "hit"]:
        for body in [[], [["raise", "D"]], [["raise", "V"]], [["exh"]]]:
            p.append(([], [[kind, None, body], S]))
        p.append(([], [[kind, ["/w", 1], [S, ["exh"]]], S]))
        p.append(([], [["read", "/r", False, [[kind, ["/w", ""], [S]], S]]]))
        p.append(
            ([], [["scope", "a", "q", [[kind, ["/w", 0], [S, ["raise", "D"]]], S]]])
        )
    # Module allowance and exhausted without an active budget.
    p.append(([], [["allow", "2.5", "/c", None], ["exh"]]))
    p.append(([], [["scope", None, "", [["allow", "2.5", "/c", None], ["exh"]]]]))
    for cwd, gd in [("/c", "/g"), (None, "/g"), (None, None), ("", "/g")]:
        p.append((["0.0"], [["unread", None, [["allow", "2.5", cwd, gd], S]]]))
    p.append(
        (
            ["0.0", "6.0"],
            [
                [
                    "unread",
                    None,
                    [["allow", "2.5", "/c", None], ["allow", "1.0", "/c", None]],
                ],
            ],
        )
    )
    # run_git's counts.
    for spawn in ["ok", "nf", "to", "os", "v"]:
        for cwd, isdir in [(None, True), ("/c", True), ("/c", False)]:
            p.append(([], [["git", "1.0", cwd, None, spawn, isdir]]))
    for spawn in ["ok", "to", "nf"]:
        for clock in [["0.0", "1.0"], ["0.0", "5.0"], ["4.5", "9.5"], ["4.5", "9.0"]]:
            p.append(
                (
                    clock,
                    [
                        [
                            "read",
                            "/r",
                            True,
                            [["git", "1.0", "/c", None, spawn, True], S],
                        ]
                    ],
                )
            )
    p.append(
        (
            ["0.0", "5.0"],
            [["read", "/r", True, [["git", "1.0", "/c", None, "nf", True]]]],
        )
    )
    p.append(
        (
            ["0.0", "4.5", "9.0", "9.5"],
            [
                [
                    "read",
                    "/r",
                    True,
                    [
                        ["git", "1.0", "/c", None, "ok", True],
                        ["git", "1.0", "/c", None, "to", True],
                        S,
                    ],
                ]
            ],
        )
    )
    p.append(
        (
            ["0.0", "6.0"],
            [
                [
                    "scope",
                    None,
                    "",
                    [
                        [
                            "unread",
                            ["/w", False],
                            [
                                ["git", "9.0", None, "/g", "ok", True],
                                ["git", "1.0", None, "/g", "ok", True],
                            ],
                        ],
                        S,
                    ],
                ],
            ],
        )
    )
    return p


def _texts() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for notes in [[], ["f"], ["t"], ["f", "t", "t", "f"]]:
        from bettermemory._git import _FAILED, _TIMED_OUT

        before = (_FAILED.count, _TIMED_OUT.count)
        for n in notes:
            (_git.note_failed if n == "f" else _git.note_timed_out)()
        rows.append(
            {
                "k": "notes",
                "notes": notes,
                "o": [_FAILED.count - before[0], _TIMED_OUT.count - before[1]],
            }
        )
    for text in [
        "",
        "\n",
        "\r\n",
        "\r",
        "a",
        "a\n",
        "a\r\n",
        "a\r",
        "a\r\r\n",
        "a\n\n",
        "a\r\n\n",
        " a \n",
        "a\nb\n",
        "a\rb",
        "\r\n\r\n",
        "é\r\n",
    ]:
        for win in [False, True]:
            with mock.patch.object(_git, "_WINDOWS", win):
                rows.append(
                    {
                        "k": "printed",
                        "t": text,
                        "win": win,
                        "o": _git.printed_path(text),
                    }
                )
    for args in [
        [],
        ["-c"],
        ["-c", "a=b"],
        ["-c", "a=b", "log"],
        ["-c", "a=b", "-c"],
        ["-c", "a=b", "-c", "c=d", "rev-parse", "x"],
        ["log", "-c", "a"],
        ["-C", "x", "log"],
        [""],
        ["-c", "a=b", ""],
    ]:
        rows.append({"k": "sub", "a": args, "o": _git.subcommand(args)})
    for args, rc, where, err in [
        (["log"], 128, "/r", "fatal: bad\nmore"),
        (["-c", "a=b", "show"], 1, "None", ""),
        (["status"], -9, "/r", "  \n\t\n"),
        (["log"], 2, "/r", "\n\nfatal: x\r\ny"),
        (["log"], 2, "/r", "a\rb"),
        (["log"], 2, "/r", "a\x0bb"),
        (["log"], 2, "/r", "a\x1cb"),
        (["log"], 2, "/r", "a\x85b"),
        (["log"], 2, "/r", "a b"),
        (["log"], 2, "/r", "　x "),
        (["log"], 2, "/r", "a\x1fb"),
        ([], 0, "", "x"),
    ]:
        result = subprocess.CompletedProcess(args, rc, "", err)
        with mock.patch.object(_git.log, "debug") as debug:
            _git.log_nonzero_exit(args, result, where)
        fmt, *values = debug.call_args.args
        rows.append(
            {
                "k": "nonzero",
                "a": args,
                "rc": rc,
                "w": where,
                "e": err,
                "o": fmt % tuple(values),
            }
        )
    for raw in [
        b"fatal: \xff\xfe bad\n",
        b"\xe2\x80\xa8x",
        b"ok\xc3",
        b"",
        b"\xed\xa0\x80z",
    ]:
        raw_result: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess(
            ["log"], 1, "", raw
        )
        with mock.patch.object(_git.log, "debug") as debug:
            _git.log_nonzero_exit(["log"], raw_result, "/r")
        fmt, *values = debug.call_args.args
        rows.append(
            {
                "k": "nonzero",
                "a": ["log"],
                "rc": 1,
                "w": "/r",
                "eb": raw.hex(),
                "o": fmt % tuple(values),
            }
        )
    return rows


def generate() -> dict[str, bytes]:
    # `_git.log`'s warnings are the reference's own noise here, not rows.
    with mock.patch.object(_git.log, "disabled", True):
        rows: list[dict[str, Any]] = [_program(c, o) for c, o in _programs()]
        rows.extend(_texts())
    header = {"k": "header", "cases": len(rows), "read_seconds": repr(grb.READ_SECONDS)}
    return {"git_read_budget.jsonl": jsonl([header, *rows])}
