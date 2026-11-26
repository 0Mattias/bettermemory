"""Fixtures: the session join read off the event log (``sessions.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/sessions.rs``. This pins
`sessions`' `_OUT_OF_PROCESS_TRIGGERS`, `_disabled_scopes_from_events`,
`_latest_in_process_session`, `_session_of`, `harness_joined_sessions`,
`hook_spans`, `runs_beside`, `live_harness_sessions`, `bridged_session`
and `joined_sessions`.

One file, not gated: every id, stamp and worktree is ASCII, every stamp
carries an offset (or reads as UTC), and `datetime.isoformat`, set and
dict order and `TypeError` on an unhashable member test are the same
under 3.11 to 3.14 on every system. One JSON object per line, keys
sorted. The first line is the header: ``triggers`` (the sorted members)
and ``count`` (the lines after it). Every other line is one of:

- ``{"k": "log", "name": ..., "ev": [...], "hs": [...], "wt": [...],
  "ss": [...], ...}``: one hand-built log (plain JSON objects, read as
  `json.loads` reads them) and every function over it. ``hs`` are the
  harness sessions tried (null, the empty one, every `str` id and stamp
  the log holds, and one it does not), ``wt`` the worktrees (null, every
  `str` worktree the log holds, and one it does not), ``ss`` the
  `server_session` values (null, then every `str` id). The answers:
  ``of`` (`_session_of` of each event), ``spans`` (`hook_spans` as
  ``[[session, first, last], ...]`` in its order, each stamp its
  `isoformat`), ``beside`` (`runs_beside(spans, o, c)` for ``o`` and
  ``c`` over the spans' sessions and one absent name, rows by ``o``),
  ``latest`` (per ``wt``), ``hj`` and ``live`` (per ``hs``), ``bridged``
  and ``joined`` (per ``hs``, then per ``wt``), ``disabled`` (per ``wt``,
  then per ``ss``). A set is its sorted list; an exception is
  ``{"x": class}``.
- ``{"k": "sweep", ...}``: ``count`` logs drawn from ``seed`` by `_Draw`
  out of the pools the row holds (``"~"`` in a pool: the key is absent),
  each through `bridged_session` and `joined_sessions` for every
  ``hs`` and ``wt``, `live_harness_sessions` for every ``hs`` and
  `_disabled_scopes_from_events` for every ``wt``; ``sha256`` is the
  digest of the lines `_sweep_lines` spells, joined by newlines.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from functools import partial
from typing import Any

from bettermemory import sessions as ss
from bettermemory.events import HARNESS_SESSION_FIELD

from ._common import jsonl

H = HARNESS_SESSION_FIELD
_MASK = (1 << 64) - 1
SWEEP_SEED = 20261125
SWEEP_COUNT = 5000
ABSENT = "~"


def _ts(minute: int, offset: str = "+00:00") -> str:
    """A stamp at 12:`minute` UTC, spelled in `offset` (`+HH:MM`)."""
    sign = 1 if offset[0] == "+" else -1
    shift = sign * (int(offset[1:3]) * 60 + int(offset[4:6]))
    total = 12 * 60 + minute + shift
    return f"2026-01-01T{total // 60:02d}:{total % 60:02d}:00{offset}"


def _x(fn: Callable[[], Any]) -> Any:
    try:
        return _spell(fn())
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}


def _spell(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return [[k, a.isoformat(), b.isoformat()] for k, (a, b) in value.items()]
    return value


def _strs(events: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for event in events:
        for key in keys:
            value = event.get(key)
            if isinstance(value, str) and value and value not in out:
                out.append(value)
    return out


def _log(name: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    hs: list[str | None] = [None, ""]
    hs += _strs(events, ("session", "session_id", H)) + ["h_absent"]
    wt: list[str | None] = [None, *_strs(events, ("worktree_root",)), "/wt/absent"]
    sv: list[str | None] = [None, *_strs(events, ("session", "session_id"))]
    row: dict[str, Any] = {"k": "log", "name": name, "ev": events}
    row.update(hs=hs, wt=wt, ss=sv)
    row["of"] = [ss._session_of(e) for e in events]
    try:
        spans = ss.hook_spans(events)
    except TypeError:
        spans = None
    row["spans"] = _x(lambda: ss.hook_spans(events))
    names = ([*spans] if spans is not None else []) + ["h_absent"]
    row["beside"] = (
        None
        if spans is None
        else [[ss.runs_beside(spans, o, c) for c in names] for o in names]
    )
    row["latest"] = [
        _x(partial(ss._latest_in_process_session, events, worktree_root=w)) for w in wt
    ]
    row["hj"] = [_x(partial(ss.harness_joined_sessions, events, h)) for h in hs]
    row["live"] = [_x(partial(ss.live_harness_sessions, events, h)) for h in hs]
    row["bridged"] = [
        [
            _x(partial(ss.bridged_session, events, harness_session=h, worktree_root=w))
            for w in wt
        ]
        for h in hs
    ]
    row["joined"] = [
        [
            _x(partial(ss.joined_sessions, events, harness_session=h, worktree_root=w))
            for w in wt
        ]
        for h in hs
    ]
    row["disabled"] = [
        [
            _x(
                partial(
                    ss._disabled_scopes_from_events,
                    events,
                    worktree_root=w,
                    server_session=s,
                )
            )
            for s in sv
        ]
        for w in wt
    ]
    return row


def _tool(session: Any, minute: int, **more: Any) -> dict[str, Any]:
    """A server tool row (no trigger)."""
    return {"kind": "search", "session": session, "ts": _ts(minute), **more}


def _hook(
    session: Any, minute: int, trigger: Any = "stop_hook", **more: Any
) -> dict[str, Any]:
    """A hook row recorded under a transcript session id."""
    return {
        "kind": "turn_audited",
        "session": session,
        "triggered_from": trigger,
        "ts": _ts(minute),
        **more,
    }


def _logs() -> list[tuple[str, list[dict[str, Any]]]]:
    cases: list[tuple[str, list[dict[str, Any]]]] = [("empty", [])]
    # Each trigger hides its row from the in-process walks; anything else
    # (absent, None, an int, a bool, another str, the empty one) does not.
    for trigger in sorted(ss._OUT_OF_PROCESS_TRIGGERS):
        cases.append(
            (f"trigger {trigger}", [_tool("sess_a", 1), _hook("h1", 2, trigger)])
        )
    others: list[Any] = ["mcp", "", None, 3, True, 0.0, "STOP_HOOK"]
    for i, other in enumerate(others):
        cases.append(
            (
                f"in-process trigger {i}",
                [_tool("sess_a", 1), _hook("sess_b", 2, other)],
            )
        )
    cases.append(
        ("in-process trigger absent", [_tool("sess_a", 1), _tool("sess_b", 2)])
    )
    # An unhashable trigger raises where the walk reaches it.
    for name, bad in (("list", ["stop_hook"]), ("dict", {"a": 1}), ("empty list", [])):
        cases.append(
            (f"unhashable {name} newest", [_tool("sess_a", 1), _hook("h1", 2, bad)])
        )
        cases.append(
            (
                f"unhashable {name} oldest",
                [
                    _hook("h1", 1, bad),
                    _tool("sess_a", 2, harness_session="h2"),
                    _hook("h2", 3),
                ],
            )
        )
    # The session fallbacks: every falsy `session` hands over to
    # `session_id`; a truthy non-str is kept and skipped.
    falsy: list[Any] = ["", 0, False, [], {}, None]
    for i, value in enumerate(falsy):
        cases.append(
            (
                f"session falsy {i}",
                [
                    {
                        "kind": "search",
                        "session": value,
                        "session_id": "sess_legacy",
                        "ts": _ts(1),
                    },
                    {
                        "kind": "turn_audited",
                        "session": value,
                        "session_id": "h1",
                        "triggered_from": "prompt_hook",
                        "ts": _ts(2),
                    },
                ],
            )
        )
        cases.append(
            (
                f"session_id falsy {i}",
                [
                    _tool("sess_a", 1),
                    {
                        "kind": "search",
                        "session": "",
                        "session_id": value,
                        "ts": _ts(2),
                    },
                ],
            )
        )
    cases.append(
        (
            "session absent",
            [
                _tool("sess_a", 1),
                {"kind": "search", "session_id": "sess_legacy", "ts": _ts(2)},
            ],
        )
    )
    cases.append(
        (
            "session non-str truthy",
            [
                _tool("sess_a", 1),
                {"kind": "search", "session": 5, "session_id": "sess_b", "ts": _ts(2)},
                {"kind": "search", "session": ["x"], "ts": _ts(3)},
            ],
        )
    )
    cases.append(
        (
            "session empty both",
            [
                _tool("sess_a", 1, harness_session="h1"),
                {
                    "kind": "search",
                    "session": "",
                    "session_id": "",
                    "ts": _ts(2),
                    H: "h1",
                },
                _hook("h1", 3),
            ],
        )
    )
    # The worktree: the newest stamped match, else the newest of any.
    cases.append(
        (
            "worktree match",
            [
                _tool("sess_a", 1, worktree_root="/wt/a"),
                _tool("sess_b", 2, worktree_root="/wt/b"),
                _tool("sess_c", 3),
                _tool("sess_d", 4, worktree_root=5),
            ],
        )
    )
    cases.append(
        (
            "worktree fallback",
            [
                _tool("sess_a", 1, worktree_root="/wt/b"),
                _tool("sess_b", 2, worktree_root="/wt/b"),
            ],
        )
    )
    # The scope replay.
    cases.append(
        (
            "scopes",
            [
                {
                    "kind": "scope_disable",
                    "session": "sess_a",
                    "scope": "s1",
                    "ts": _ts(1),
                },
                {
                    "kind": "scope_disable",
                    "session": "sess_a",
                    "scope": "s2",
                    "ts": _ts(2),
                },
                {
                    "kind": "scope_enable",
                    "session": "sess_a",
                    "scope": "s1",
                    "ts": _ts(3),
                },
                {"kind": "scope_disable", "session": "sess_a", "scope": 5},
                {"kind": "scope_disable", "session": "sess_a", "scope": ["s3"]},
                {"kind": "scope_disable", "session": "sess_a", "scope": None},
                {"kind": "scope_disable", "session": "sess_a"},
                {"kind": "scope_disable", "session": "sess_b", "scope": "s4"},
                {"kind": "scope_disable", "session_id": "sess_a", "scope": "s5"},
                {"kind": ["scope_disable"], "session": "sess_a", "scope": "s6"},
                {"kind": "SCOPE_DISABLE", "session": "sess_a", "scope": "s7"},
                {"kind": "scope_enable", "session": "sess_a", "scope": "s9"},
                {"kind": "scope_disable", "session": "sess_a", "scope": ""},
                {
                    "kind": "scope_disable",
                    "session": "sess_a",
                    "scope": "s1",
                    "triggered_from": "stop_hook",
                },
                _hook("h1", 4, kind="scope_disable", scope="s8"),
            ],
        )
    )
    cases.append(
        (
            "scopes re-enabled then disabled",
            [
                {"kind": "scope_enable", "session": "sess_a", "scope": "s1"},
                {"kind": "scope_disable", "session": "sess_a", "scope": "s1"},
                {"kind": "scope_enable", "session": "sess_a", "scope": "s2"},
                {
                    "kind": "scope_disable",
                    "session": "sess_b",
                    "scope": "s2",
                    "worktree_root": "/wt/b",
                },
                {"kind": "search", "session": "sess_a", "worktree_root": "/wt/a"},
            ],
        )
    )
    # The harness stamp: only a non-empty str names a conversation.
    cases.append(
        (
            "stamps not str",
            [
                _tool("sess_a", 1, harness_session=5),
                _tool("sess_b", 2, harness_session=["h1"]),
                _tool("sess_c", 3, harness_session=""),
                _tool("sess_d", 4, harness_session=None),
                _tool(0, 5, harness_session="h1"),
                _hook("h1", 6),
            ],
        )
    )
    cases.append(
        (
            "stamp own newest",
            [
                _hook("h1", 1),
                _tool("sess_old", 2, harness_session="h1"),
                _tool("sess_new", 3, harness_session="h1"),
                _tool("sess_other", 4),
                _hook("sess_hook", 5, harness_session="h1"),
            ],
        )
    )
    # bridged_session's docstring: a server stamped before a /clear is
    # found by the bridge (its old conversation went quiet).
    cases.append(
        (
            "clear, own server",
            [
                _hook("h_old", 1),
                _tool("sess_a", 2, harness_session="h_old", worktree_root="/wt/a"),
                _hook("h_old", 3),
                _hook("h_new", 5),
                _tool("sess_a", 6, harness_session="h_old", worktree_root="/wt/a"),
            ],
        )
    )
    # A /clear in one window while the other window's turn began before
    # it: both stamps write since this conversation's first row, so both
    # servers are set aside.
    cases.append(
        (
            "clear, other window mid-turn",
            [
                _hook("h_old", 1),
                _tool("sess_a", 2, harness_session="h_old", worktree_root="/wt/a"),
                _hook("h_b", 3),
                _tool("sess_b", 4, harness_session="h_b", worktree_root="/wt/a"),
                _hook("h_new", 5),
                _tool("sess_a", 6, harness_session="h_old", worktree_root="/wt/a"),
                _tool("sess_b", 7, harness_session="h_b", worktree_root="/wt/a"),
                _tool("sess_u", 8, worktree_root="/wt/a"),
            ],
        )
    )
    # The same, the other window's server in another worktree: with the
    # worktree given only one stamp counts.
    cases.append(
        (
            "clear, other window elsewhere",
            [
                _hook("h_old", 1),
                _tool("sess_a", 2, harness_session="h_old", worktree_root="/wt/a"),
                _hook("h_b", 3),
                _hook("h_new", 5),
                _tool("sess_a", 6, harness_session="h_old", worktree_root="/wt/a"),
                _tool("sess_b", 7, harness_session="h_b", worktree_root="/wt/b"),
            ],
        )
    )
    # Only the other window's server wrote since the clear: the foreign join.
    cases.append(
        (
            "clear, only the other server wrote",
            [
                _hook("h_old", 1),
                _tool("sess_a", 2, harness_session="h_old"),
                _hook("h_b", 3),
                _hook("h_new", 5),
                _tool("sess_b", 7, harness_session="h_b"),
            ],
        )
    )
    # The other window's hooks still run: its server is foreign.
    cases.append(
        (
            "other window live",
            [
                _hook("h_new", 1),
                _tool("sess_b", 2, harness_session="h_b"),
                _hook("h_b", 3),
                _tool("sess_u", 4),
                _tool("sess_b", 5, harness_session="h_b"),
            ],
        )
    )
    # A /resume: no hook row of its own yet, so every conversation with one
    # runs beside it; from its second call its first row anchors the rule.
    cases.append(
        (
            "resume, first call",
            [
                _hook("h_prev", 1),
                _tool("sess_a", 2, harness_session="h_prev"),
                _tool("sess_u", 3),
            ],
        )
    )
    cases.append(
        (
            "resume, second call",
            [
                _hook("h_prev", 1),
                _tool("sess_a", 2, harness_session="h_prev"),
                _hook("h_res", 3),
                _tool("sess_a", 4, harness_session="h_prev"),
            ],
        )
    )
    # A stamp whose conversation has no hook row is never beside.
    cases.append(
        (
            "stamp without hooks",
            [_hook("h_new", 1), _tool("sess_a", 2, harness_session="h_ghost")],
        )
    )
    # The newest stamp per session wins (`setdefault` over the reversed walk).
    cases.append(
        (
            "restamped session",
            [
                _hook("h_x", 1),
                _hook("h_new", 2),
                _tool("sess_a", 3, harness_session="h_x"),
                _tool("sess_a", 4, harness_session="h_y"),
                _hook("h_y", 5),
            ],
        )
    )
    # runs_beside at equality, spelled in two offsets.
    cases.append(
        (
            "beside at equality",
            [
                _hook("h_a", 1),
                _hook("h_b", 3, ts=_ts(3, "+02:00")),
                _hook("h_a", 3, ts=_ts(3, "-05:30")),
                _tool("sess_a", 4, harness_session="h_a"),
            ],
        )
    )
    cases.append(
        (
            "beside one microsecond short",
            [
                _hook("h_a", 1),
                _hook("h_a", 2, ts="2026-01-01T12:02:59.999999+00:00"),
                _hook("h_b", 3),
            ],
        )
    )
    # hook_spans: equal instants in other offsets keep the earlier row's
    # spelling, at both ends; naive, Z, unparsable and non-str stamps.
    cases.append(
        (
            "spans",
            [
                _hook("h_a", 5),
                _hook("h_a", 5, ts=_ts(5, "+02:00")),
                _hook("h_a", 1, ts=_ts(1, "-03:00")),
                _hook("h_a", 1),
                _hook("h_a", 9, ts=_ts(9, "+05:45")),
                _hook("h_a", 9),
                _hook("h_b", 0, ts="2026-01-01T12:00:00"),
                _hook("h_b", 0, ts="2026-01-01T12:30:00Z"),
                _hook("h_b", 0, ts="2026-01-01T12:10:00.5+00:00"),
                _hook("h_c", 0, ts="garbage"),
                _hook("h_c", 0, ts=1767268800),
                _hook("h_c", 0, ts=None),
                _hook("", 4),
                _hook("h_d", 0, ts="2026-01-01T12:00:00+05:30:15"),
                _hook(None, 4, session_id="h_e"),
                _hook(["h_f"], 4),
                {"kind": "x", "session": "h_g", "triggered_from": "prompt_hook"},
            ],
        )
    )
    cases.append(
        (
            "live",
            [
                _hook("h_a", 1, trigger="prompt_hook"),
                _hook("h_b", 2, trigger="session_hook"),
                _hook("h_c", 3, trigger="session_capture"),
                _hook("h_a", 4),
                _hook("h_d", 5),
                _hook("h_b", 6, ts=_ts(4, "+01:00")),
            ],
        )
    )
    # The harness join over several servers; hook rows stamped are skipped.
    cases.append(
        (
            "harness join",
            [
                _tool("sess_a", 1, harness_session="h1"),
                _tool("sess_b", 2, harness_session="h1"),
                _tool("sess_c", 3, harness_session="h2"),
                _hook("sess_d", 4, harness_session="h1"),
                {"kind": "search", "session_id": "sess_e", H: "h1"},
                {"kind": "search", "session": 0, H: "h1"},
            ],
        )
    )
    # Bridge set-aside reaching an event with no session (kept).
    cases.append(
        (
            "bridge keeps sessionless",
            [
                _hook("h_b", 1),
                _hook("h_new", 2),
                _tool("sess_b", 3, harness_session="h_b"),
                {"kind": "search", "ts": _ts(4)},
                _tool(7, 5),
            ],
        )
    )
    # Stamps before this conversation's first row do not count.
    cases.append(
        (
            "stamps before own first",
            [
                _hook("h_x", 1),
                _hook("h_y", 1),
                _tool("sess_x", 2, harness_session="h_x"),
                _tool("sess_y", 3, harness_session="h_y"),
                _hook("h_new", 4),
                _tool("sess_x", 5, harness_session="h_x"),
                _tool("sess_y", 6, harness_session="h_y", ts="bad"),
                _tool("sess_z", 7),
            ],
        )
    )
    return cases


# -- the sweep ---------------------------------------------------------------


class _Draw:
    """`nxt` steps x = x * 6364136223846793005 + 1442695040888963407 mod
    2**64 and gives x >> 33; `pick(n)` is `nxt() % n`."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def nxt(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & _MASK
        return self.x >> 33

    def pick(self, n: int) -> int:
        return self.nxt() % n


# Field order of a drawn event, and each field's pool. ``"~"``: absent.
POOLS: list[tuple[str, list[Any]]] = [
    (
        "kind",
        ["search", "search", "scope_disable", "scope_enable", "turn_audited", ABSENT],
    ),
    (
        "triggered_from",
        [ABSENT] * 8
        + ["stop_hook", "prompt_hook", "session_hook", "session_capture"] * 2
        + [None, "mcp", 3, ["stop_hook"]],
    ),
    ("session", ["sess_a", "sess_b", "sess_c", "h1", "h2", "h3", "", 0, None, ABSENT]),
    ("session_id", [ABSENT] * 6 + ["sess_a", "h1", "", False]),
    (H, [ABSENT] * 4 + ["h1", "h2", "h3", "", 5]),
    ("worktree_root", [ABSENT, "/wt/a", "/wt/a", "/wt/b"]),
    ("scope", [ABSENT, "s1", "s2", 7]),
    (
        "ts",
        [_ts(m) for m in range(6)]
        + [_ts(2, "+02:00"), _ts(4, "-01:00"), "2026-01-01T12:03:00", "x", 9, ABSENT],
    ),
]
SWEEP_HS: list[str | None] = [None, "", "h1", "h2", "h3"]
SWEEP_WT: list[str | None] = [None, "/wt/a", "/wt/b"]


def _draw_log(d: _Draw) -> list[dict[str, Any]]:
    events = []
    for _ in range(1 + d.pick(10)):
        event: dict[str, Any] = {}
        for key, pool in POOLS:
            value = pool[d.pick(len(pool))]
            if value != ABSENT:
                event[key] = value
        events.append(event)
    return events


def _line(value: Any) -> str:
    """One answer: ``~`` for None, a str as is, a set as ``{a,b}``
    sorted, an exception as ``!`` and its class."""
    if value is None:
        return "~"
    if isinstance(value, set):
        return "{" + ",".join(sorted(value)) + "}"
    return str(value)


def _call(fn: Callable[[], Any]) -> str:
    try:
        return _line(fn())
    except Exception as exc:  # the class is the answer
        return "!" + type(exc).__name__


def _sweep_lines() -> list[str]:
    """Per log ``i``, in order: for each ``hs`` the line
    ``i h=<h> live=<live>`` and for each ``wt`` the line
    ``i h=<h> w=<w> b=<bridged> j=<joined>``; then for each ``wt`` the line
    ``i w=<w> d=<disabled>``. A null ``h`` or ``w`` is spelled ``~``."""
    d = _Draw(SWEEP_SEED)
    lines: list[str] = []
    for i in range(SWEEP_COUNT):
        events = _draw_log(d)
        for h in SWEEP_HS:
            live = _call(lambda: ss.live_harness_sessions(events, h))
            lines.append(f"{i} h={_line(h)} live={live}")
            for w in SWEEP_WT:
                b = _call(
                    lambda: ss.bridged_session(
                        events, harness_session=h, worktree_root=w
                    )
                )
                j = _call(
                    lambda: ss.joined_sessions(
                        events, harness_session=h, worktree_root=w
                    )
                )
                lines.append(f"{i} h={_line(h)} w={_line(w)} b={b} j={j}")
        for w in SWEEP_WT:
            dis = _call(
                lambda: ss._disabled_scopes_from_events(events, worktree_root=w)
            )
            lines.append(f"{i} w={_line(w)} d={dis}")
    return lines


def _sweep_row() -> dict[str, Any]:
    lines = _sweep_lines()
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "pools": [[key, pool] for key, pool in POOLS],
        "hs": SWEEP_HS,
        "wt": SWEEP_WT,
        "lines": len(lines),
        "sha256": hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = [_log(name, events) for name, events in _logs()]
    rows.append(_sweep_row())
    header = {
        "k": "consts",
        "triggers": sorted(ss._OUT_OF_PROCESS_TRIGGERS),
        "count": len(rows),
    }
    return {"sessions.jsonl": jsonl([header, *rows])}
