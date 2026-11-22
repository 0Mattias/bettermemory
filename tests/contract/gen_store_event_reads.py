"""Fixture: the event and capture readers of ``store_events.py`` and the
window floor (``store_event_reads.jsonl``, ``store_event_reads_naive.jsonl``).

The native build ports `iter_events`, `iter_events_backward`,
`capture_rows`, `_window_floor`, `events_since`, `events_of_kinds` and
`session_events_of_kinds` (``native/``, `store::event_reads`) and is held
to these files.

The first line is the ``log`` table's DDL as `store_schema` writes it, with
its four indexes. A ``store`` line names a store and the statements that
fill it (parameters tagged ``i``, ``s``, ``b`` as `gen_store_reads` tags
them). A read line names the store, the call and its arguments; the reads
run on the module's functions over an object holding the connection. A
text argument that holds a surrogate is ``{"cps": [...]}``. The answer is
``out``, the `json.dumps` of what the generator yielded, and ``raise``,
the class of what stopped it (absent when it ran out).

No row depends on the SQLite release: every read is ordered by ``seq`` or
asks ``MIN(seq)``, so the planner's choice changes no answer and no
``_sqlite`` sibling is written.

``store_event_reads_naive.jsonl`` holds the reads given a naive ``since``,
which the reference reads as the machine's local time. The generator sets
``TZ`` to a fixed zone (``offset`` seconds east) around those reads and
gives it back; `time.tzset` is POSIX only, so the file is POSIX only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from bettermemory import store_events
from bettermemory.store import Store

from ._common import jsonl

DDL = [
    "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, "
    "kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, "
    "mac TEXT NOT NULL)",
    "CREATE INDEX log_by_kind ON log(kind, seq)",
    "CREATE INDEX log_by_ts ON log(ts)",
    "CREATE INDEX log_by_mac ON log(mac)",
    "CREATE INDEX log_by_session ON log(session, seq)",
]

#: The fixed zone of the naive reads: UTC+05:30, as POSIX ``TZ`` spells it.
NAIVE_TZ = "XST-05:30"
NAIVE_OFFSET = 19800

INSERT = (
    "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
    "VALUES (?, ?, ?, ?, ?, 'p', 'm')"
)


def tag(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, int):
        return ["i", v]
    if isinstance(v, str):
        return ["s", v]
    return ["b", v.hex()]


def _row(seq: int, ts: Any, session: Any, kind: str, payload: Any) -> list[Any]:
    text = payload if isinstance(payload, (str, bytes, int)) else json.dumps(payload)
    return [INSERT, [seq, ts, session, kind, text]]


def _mixed() -> list[list[Any]]:
    return [
        _row(1, "2026-01-01T00:00:00Z", None, "store_created", {}),
        _row(
            2,
            "2026-01-01T00:00:05+00:00",
            "s1",
            "use",
            {"id": "m1", "ts": "x", "kind": "y", "session": "z", "n": 1},
        ),
        _row(3, "2026-01-01T00:00:03Z", "s2", "search", {"q": 'm1 "quoted"'}),
        _row(4, "2026-01-02T00:00:00Z", None, "use", "[1, 2]"),
        _row(5, "2026-01-02T00:00:00.500000Z", "s1", "use", "not json"),
        _row(6, "2026-01-03T00:00:00+00:00", "s1", "capture_open", {"capture": "c1"}),
        _row(7, "2026-01-03T00:00:00Z", None, "memory_put", {"id": "m1"}),
        _row(
            8,
            "2026-01-04T00:00:00Z",
            "s1",
            "recall",
            {"id": 'é\n"x\t', "f": 1.5, "big": 10**30},
        ),
        _row(9, "2026-01-04T00:00:00Z", "s1", "capture_member", {"id": "m1"}),
        _row(10, "2025-12-31T23:59:59Z", "s2", "use", {"id": "m2"}),
        _row(
            11, "2026-01-05T00:00:00Z", "sé", "use", {"id": "m3", "nan": float("nan")}
        ),
        _row(12, "2026-01-05T00:00:01Z", "s1", "capture_close", "[]"),
    ]


def _stores() -> dict[str, list[list[Any]]]:
    mixed = _mixed()
    return {
        "mixed": mixed,
        "no_ts_index": mixed + [["DROP INDEX log_by_ts", []]],
        "no_session_index": mixed + [["DROP INDEX log_by_session", []]],
        "empty": [],
        "bad_utf8": mixed[:4]
        + [["UPDATE log SET mac = CAST(X'ff' AS TEXT) WHERE seq = 3", []]],
        "int_payload": mixed[:3] + [_row(4, "2026-01-02T00:00:00Z", "s1", "use", 5)],
        "blob_payload": [
            _row(1, "2026-01-02T00:00:00Z", "s1", "use", b'{"a": 1}'),
            _row(2, "2026-01-02T00:00:01Z", "s1", "use", b"\xff"),
            _row(3, "2026-01-02T00:00:02Z", "s1", "use", {"b": 2}),
        ],
        "int_ts": [
            _row(1, 20260101, "s1", "use", {"a": 1}),
            _row(2, "2026", 7, "use", {}),
        ],
    }


class _ShimType:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    _event_from_raw = Store._event_from_raw
    _window_floor = store_events._window_floor


def _Shim(conn: sqlite3.Connection) -> Store:  # noqa: N802 - reads as the shim
    """An object holding the connection, typed as the `Store` it stands in
    for: the reads touch only `_conn`, `_event_from_raw` and
    `_window_floor`."""
    return cast(Store, _ShimType(conn))


def _untag(v: Any) -> Any:
    if v is None:
        return None
    kind, value = v
    return bytes.fromhex(value) if kind == "b" else value


def _connect(statements: list[list[Any]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for sql in DDL:
        conn.execute(sql)
    for sql, params in statements:
        conn.execute(sql, [_untag(tag(p)) for p in params])
    return conn


def _text(s: str) -> Any:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return {"cps": [ord(c) for c in s]}
    return s


def _run(gen: Any) -> dict[str, Any]:
    got: list[Any] = []
    out: dict[str, Any] = {}
    try:
        for item in gen:
            got.append(list(item) if isinstance(item, tuple) else item)
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        out["raise"] = type(exc).__name__
    out["out"] = json.dumps(got)
    return out


AWARE = [
    datetime(2026, 1, 1, tzinfo=timezone.utc),
    datetime(2026, 1, 1, 0, 0, 4, 999999, tzinfo=timezone.utc),
    datetime(2026, 1, 2, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    datetime(2026, 1, 3, tzinfo=timezone(timedelta(hours=-1, seconds=-30))),
    datetime(2025, 1, 1, tzinfo=timezone.utc),
    datetime(2027, 1, 1, tzinfo=timezone.utc),
    datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-2))),
    datetime(999, 6, 1, tzinfo=timezone.utc),
]

KIND_SETS: list[list[str]] = [
    [],
    ["use"],
    ["use", "recall", "use"],
    ["search", "capture_open", "memory_put"],
    ["nothing"],
    ["use\ud800"],
]

#: A naming as the file spells it: its listed ``head``, then ``n0``,
#: ``n1``, ... up to ``count`` more (the bound's sweep).
NAMINGS: list[tuple[list[str], int] | None] = [
    None,
    ([], 0),
    (["m1"], 0),
    (["m2", "m3"], 0),
    (['"quoted"'], 0),
    (['\u00e9\n"x\t'], 0),
    (["absent"], 0),
    (["m\ud800"], 0),
    ([], 200),
    (["m2"], 200),
    (["m2"], 199),
    (["m2", "m2"], 199),
]


def _naming(spelled: tuple[list[str], int] | None) -> list[str] | None:
    if spelled is None:
        return None
    head, count = spelled
    return head + [f"n{i}" for i in range(count)]


def _reads(name: str, statements: list[list[Any]]) -> list[dict[str, Any]]:
    """Every read over the ``mixed`` store and its index-less twins; the
    other stores get one of each call."""
    shim = _Shim(_connect(statements))
    full = name in ("mixed", "no_ts_index", "no_session_index")
    rows: list[dict[str, Any]] = []

    def add(call: str, args: dict[str, Any], gen: Any) -> None:
        rows.append({"store": name, "call": call, "args": args, **_run(gen)})

    for since_seq in (0, 3, -1, 99):
        add(
            "iter_events",
            {"since_seq": since_seq},
            store_events.iter_events(shim, since_seq),
        )
    add("iter_events_backward", {}, store_events.iter_events_backward(shim))
    for kinds in (
        ["capture_open", "capture_member", "capture_close"],
        ["use"],
        [],
        ["capture_member", "capture_member"],
        ["capture_undo"],
    ):
        for since_seq in (0, 6):
            add(
                "capture_rows",
                {"kinds": kinds, "since_seq": since_seq},
                store_events.capture_rows(shim, kinds, since_seq=since_seq),
            )
    prefixes = (
        "2026-01-01T00:00:00",
        "2026-01-02T00:00:00",
        "2026-01-09T00:00:00",
        "",
        "2026",
    )
    for prefix in prefixes if full else prefixes[:2]:
        for head in (None, 2, 0) if full else (None,):
            value = store_events._window_floor(shim, prefix, head_seq=head)
            rows.append(
                {
                    "store": name,
                    "call": "window_floor",
                    "args": {"prefix": prefix, "head_seq": head},
                    "floor": value,
                }
            )
    for since in AWARE if full else AWARE[:1] + AWARE[6:7]:
        add(
            "events_since",
            {"since": since.isoformat()},
            store_events.events_since(shim, since),
        )
    for kinds in KIND_SETS if full else KIND_SETS[1:3]:
        for since in AWARE[:3] + AWARE[6:7] if full else AWARE[:1]:
            add(
                "events_of_kinds",
                {
                    "kinds": [_text(k) for k in kinds],
                    "since": since.isoformat(),
                    "naming": None,
                },
                store_events.events_of_kinds(shim, kinds, since=since),
            )
    for spelled in NAMINGS if full else NAMINGS[:3]:
        naming = _naming(spelled)
        add(
            "events_of_kinds",
            {
                "kinds": ["use", "search", "recall"],
                "since": AWARE[4].isoformat(),
                "naming": None
                if spelled is None
                else {"head": [_text(n) for n in spelled[0]], "count": spelled[1]},
            },
            store_events.events_of_kinds(
                shim, ["use", "search", "recall"], since=AWARE[4], naming=naming
            ),
        )
    sessions = ("s1", "s2", "s\u00e9", "none", "s\ud800") if full else ("s1",)
    for session in sessions:
        for kinds in (["use", "recall"], ["capture_open", "use"], []):
            # A surrogate is read on a fresh connection: on one whose
            # statement cache holds the unhinted query, CPython's failed
            # bind reports the hint's stale "no such index" as
            # OperationalError (the README's reference finding).
            target = shim if "\ud800" not in session else _Shim(_connect(statements))
            add(
                "session_events_of_kinds",
                {"session": _text(session), "kinds": kinds},
                store_events.session_events_of_kinds(target, session, kinds),
            )
    return rows


def generate() -> dict[str, bytes]:
    lines: list[Any] = [{"ddl": DDL}]
    for name, statements in _stores().items():
        lines.append(
            {
                "store": name,
                "statements": [[s, [tag(p) for p in ps]] for s, ps in statements],
            }
        )
        lines.extend(_reads(name, statements))
    return {"store_event_reads.jsonl": jsonl(lines)}


NAIVE = [
    datetime(2026, 1, 1, 5, 30),
    datetime(2026, 1, 1, 5, 30, 4, 999999),
    datetime(2026, 1, 2, 5, 30),
    datetime(2026, 1, 9),
]


def _tzset() -> None:
    if sys.platform != "win32":
        time.tzset()


def generate_naive() -> dict[str, bytes]:
    """POSIX only (``POSIX_ONLY``): `time.tzset` is not on Windows."""
    held = os.environ.get("TZ")
    os.environ["TZ"] = NAIVE_TZ
    _tzset()
    try:
        lines: list[Any] = [{"ddl": DDL, "offset": NAIVE_OFFSET}]
        statements = _stores()["mixed"]
        lines.append(
            {
                "store": "mixed",
                "statements": [[s, [tag(p) for p in ps]] for s, ps in statements],
            }
        )
        shim = _Shim(_connect(statements))
        for since in NAIVE:
            lines.append(
                {
                    "store": "mixed",
                    "call": "events_since",
                    "args": {"since": since.isoformat()},
                    **_run(store_events.events_since(shim, since)),
                }
            )
            lines.append(
                {
                    "store": "mixed",
                    "call": "events_of_kinds",
                    "args": {
                        "kinds": ["use"],
                        "since": since.isoformat(),
                        "naming": None,
                    },
                    **_run(store_events.events_of_kinds(shim, ["use"], since=since)),
                }
            )
    finally:
        if held is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = held
        _tzset()
    return {"store_event_reads_naive.jsonl": jsonl(lines)}
