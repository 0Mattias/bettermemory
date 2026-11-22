"""Fixtures: the digest's questions (``store_digest_query.jsonl``,
``store_digest_query_sqlite.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/store/digest_query.rs``. This pins
it to `store_digest`'s `_mark`, `_share`, `digest_candidates` and
`digest_shares`.

Each line is one case over a fresh in-memory database holding the
digest's tables (`store_digest._SCHEMA`), and is one of two kinds.

``"k": "mark"``: `_mark(conn)` over ``st`` (``digest_state``'s rows, or
null for no such table) and ``log`` (rows ``[seq, kind, mac]`` of a table
``log(seq INTEGER PRIMARY KEY, kind TEXT, mac TEXT)``); a state value
``@fp`` stands for `fingerprint()`. ``r`` is the answer.

``"k": "q"``: ``recs``, rows ``[doc, removed, id, n, claim, stamped,
tokens]`` that `_put_entry` wrote for the case's bodies (its postings are
`expanded` of the tokens), then the steps ``s``, in turn on the same
connection, against a store whose ``digest_mark()`` answers ``mark`` (a
step's ``mark`` replaces it from then on). A step may first run ``pre``:
``["row", doc, removed, id, n, claim, stamped, tokens]`` (one more
record, written as `_put_entry` wrote it) or ``["sql", text]``. Then it
asks ``do`` (``"c"`` `digest_candidates`, ``"s"`` `digest_shares`) with
tokens ``t``, ``rm`` (removed), ``f`` (the floor's repr) and ``cl``
(claims). It answers ``r``: null, ``{"raise": class}``, or the rows sorted
by id (a candidate ``[id, n, claim, stamped, tokens sorted and joined]``,
a share ``[id, n, claim, stamped, shared]``); ``w``, whether a warning was
logged; and ``a``, ``temp.digest_asked``'s row afterwards (null with no
such table).

The rows' order is SQLite's (the query has no ORDER BY):
``store_digest_query_sqlite.jsonl`` holds, per case, each step's ids in
the order returned, and is gated on the SQLite version.

Every token and body is ASCII, so no answer follows the Unicode data or
the Python release.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from bettermemory import store_digest as sd

from ._common import jsonl

_W = [f"w{i}" for i in range(40)]


def _body(words: list[str]) -> str:
    return " ".join(words) + "."


def _bodies() -> list[tuple[bool, str, str]]:
    """(removed, id, body) of the main store."""
    return [
        (False, "a1", _body(_W[0:10])),
        (False, "a2", _body(_W[0:5] + _W[20:25])),
        (False, "a3", _body(_W[0:3])),
        (False, "a4", _body(_W[0:20])),
        (False, "a5", _body(["feature-flags", "daemon-config", "port"] + _W[30:35])),
        (
            False,
            "a6",
            "Port moved to 9443.\n\n_(consolidate --llm --from-transcript: x)_",
        ),
        (False, "a7", " ".join(f"{w}." for w in _W[0:30] * 4)),
        (False, "a8", _body(_W[10:20])),
        (True, "r1", _body(_W[0:10])),
        (True, "r2", _body(_W[0:4] + _W[30:36])),
        (True, "a1", _body(_W[5:15])),
        (False, "e0", "The and of to a an."),
    ]


class _Store:
    def __init__(self, conn: sqlite3.Connection, mark: str | None) -> None:
        self._conn = conn
        self._path = "S"
        self.mark = mark

    def digest_mark(self) -> str | None:
        return self.mark


class _Caught(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def _rows(conn: sqlite3.Connection, where: str = "") -> list[list[Any]]:
    return [
        list(r)
        for r in conn.execute(
            "SELECT doc, removed, id, n, claim, stamped, tokens FROM digest_records "
            + where
            + " ORDER BY doc"
        )
    ]


def _put(conn: sqlite3.Connection, removed: bool, rid: str, body: str) -> list[Any]:
    sd._put_entry(
        conn, removed=removed, record_id=rid, rid=0, body=body, scopes="[]", cwd=""
    )
    return _rows(
        conn,
        f"WHERE doc = {int(conn.execute('SELECT max(doc) FROM digest_records').fetchone()[0])}",
    )[0]


def _tokens(body: str) -> list[str]:
    return sorted(sd._features(body).tokens)


Q = _tokens(_body(_W[0:10]))
FLOORS = ["0.0", "0.25", "0.4", "0.999", "1.0"]


def _steps() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in FLOORS:
        for rm in (False, True):
            out.append({"do": "c", "t": Q, "rm": rm, "f": f, "cl": False})
            out.append({"do": "s", "t": Q, "rm": rm, "f": f, "cl": True})
    out += [
        {"do": "c", "t": [], "rm": False, "f": "0.4", "cl": False},
        {"do": "s", "t": _W[0:8], "rm": False, "f": "0.4", "cl": False},
        {"do": "s", "t": _W[0:8] + ["port"], "rm": False, "f": "0.9", "cl": False},
        {
            "do": "s",
            "t": ["feature-flags", "port"],
            "rm": False,
            "f": "0.4",
            "cl": False,
        },
        {
            "do": "c",
            "t": ["feature", "flags", "daemon"],
            "rm": False,
            "f": "0.5",
            "cl": False,
        },
        {
            "do": "c",
            "t": ["port", "moved", "9443"],
            "rm": False,
            "f": "0.25",
            "cl": True,
        },
        {"do": "s", "t": _W[0:3], "rm": False, "f": "-0.0004", "cl": False},
        {"do": "s", "t": _W[0:3], "rm": False, "f": "-0.5", "cl": False},
        {"do": "s", "t": _W[0:3], "rm": False, "f": "2.0", "cl": False},
        {"do": "s", "t": _W[0:3], "rm": False, "f": "nan", "cl": False},
        {"do": "c", "t": _W[0:3], "rm": False, "f": "inf", "cl": False},
        {"do": "c", "t": _W[0:3], "rm": False, "f": "-inf", "cl": False},
        {"do": "s", "t": _W[0:4], "rm": False, "f": "9223372036854775.0", "cl": False},
        {"do": "s", "t": _W[0:4], "rm": False, "f": "9.3e15", "cl": False},
        {
            "do": "s",
            "t": _W[0:4],
            "rm": False,
            "f": "-9223372036854775.808",
            "cl": False,
        },
        {"do": "s", "t": _W[0:4], "rm": False, "f": "0.25", "cl": False, "mark": None},
        {"do": "s", "t": _W[0:4], "rm": False, "f": "0.25", "cl": False, "mark": "9:m"},
        # The same question again under the same mark: the shared counts
        # are not taken again, so a record written since is not seen.
        {
            "do": "s",
            "t": _W[0:4],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": ["put", False, "n1", _body(_W[0:4])],
        },
        {
            "do": "s",
            "t": _W[0:4],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "mark": "10:m",
        },
        {
            "do": "s",
            "t": _W[0:4],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": ["sql", "DROP TABLE temp.digest_asked"],
        },
        {
            "do": "c",
            "t": _W[0:6],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": [
                "sql",
                "UPDATE digest_records SET n = 2.75, stamped = 'x' WHERE id = 'a3'",
            ],
        },
        {
            "do": "c",
            "t": _W[0:6],
            "rm": False,
            "f": "0.0",
            "cl": False,
            "pre": [
                "sql",
                "UPDATE digest_records SET tokens = 'w0\twx\x1fwy  ' WHERE id = 'a3'",
            ],
        },
        {
            "do": "c",
            "t": _W[0:7],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": ["sql", "ALTER TABLE digest_records RENAME TO gone"],
        },
        {
            "do": "s",
            "t": _W[0:8],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": ["sql", "ALTER TABLE gone RENAME TO digest_records"],
        },
        {
            "do": "c",
            "t": _W[0:9],
            "rm": False,
            "f": "0.25",
            "cl": False,
            "pre": ["sql", "DROP TABLE temp.digest_shared"],
        },
    ]
    # Hand-edited sizes of a3, whose tokens an earlier step made w0 wx wy (the writer stores integers): int() reads a
    # numeral text or bytes, a real truncated or as large as it is, and
    # raises on any other text or an infinity. Each asks a new
    # token set, so the shared counts are counted again. They run before
    # the last three steps, which leave the digest unreadable.
    edits = [
        ("n", "'\u0667'"),
        ("claim", "' -1_2 '"),
        ("n", "X'3920'"),
        ("n", "1e300"),
        ("n", "2.5"),
        ("n", "'x'"),
        ("n", "X'd9a7'"),
        ("n", "'\u0667\u0667'"),
        ("claim", "1e999"),
    ]
    tail = out[-3:]
    del out[-3:]
    for i, (column, value) in enumerate(edits):
        out.append(
            {
                "do": "cs"[i % 2],
                "t": ["w0", "wx", "wy", f"x{i}"],
                "rm": False,
                "f": "0.0",
                "cl": False,
                "pre": [
                    "sql",
                    f"UPDATE digest_records SET n = 3, claim = 3, {column} = {value} "
                    "WHERE id = 'a3'",
                ],
            }
        )
    out.extend(tail)
    return out


def _floor(text: str) -> float:
    return float(text)


def _ask(store: _Store, step: dict[str, Any]) -> tuple[Any, list[str] | None]:
    ask = sd.digest_candidates if step["do"] == "c" else sd.digest_shares
    try:
        rows: Any = ask(
            store,  # type: ignore[arg-type]
            frozenset(step["t"]),
            removed=step["rm"],
            floor=_floor(step["f"]),
            claims=step["cl"],
        )
    except (ValueError, OverflowError) as exc:
        return {"raise": type(exc).__name__}, None
    if rows is None:
        return None, None
    order = [r.id for r in rows]
    if step["do"] == "c":
        out = [
            [r.id, r.n, r.claim, r.stamped, " ".join(sorted(r.tokens))] for r in rows
        ]
    else:
        out = [[r.id, r.n, r.claim, r.stamped, r.shared] for r in rows]
    return sorted(out), order


def _asked(conn: sqlite3.Connection) -> Any:
    try:
        row = conn.execute("SELECT asked FROM temp.digest_asked").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else row[0]


def _query_case() -> tuple[dict[str, Any], list[Any]]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(sd._SCHEMA)
    for removed, rid, body in _bodies():
        _put(conn, removed, rid, body)
    case: dict[str, Any] = {"k": "q", "recs": _rows(conn), "mark": "7:mac", "s": []}
    store = _Store(conn, "7:mac")
    caught = _Caught()
    sd.log.addHandler(caught)
    orders: list[Any] = []
    try:
        for step in _steps():
            step = dict(step)
            if "mark" in step:
                store.mark = step["mark"]
            pre = step.get("pre")
            if pre is not None and pre[0] == "put":
                step["pre"] = ["row", *_put(conn, pre[1], pre[2], pre[3])]
            elif pre is not None:
                conn.execute(pre[1])
            before = caught.count
            step["r"], order = _ask(store, step)
            step["w"] = caught.count > before
            step["a"] = _asked(conn)
            case["s"].append(step)
            orders.append(order)
    finally:
        sd.log.removeHandler(caught)
        conn.close()
    return case, orders


_LOG = [[1, "store_created", "m1"], [2, "memory_put", "m2"], [3, "memory_delete", None]]


def _mark_cases() -> list[dict[str, Any]]:
    good = [["fingerprint", "@fp"], ["seq", "3"], ["mac", ""]]
    return [
        {"st": None, "log": _LOG},
        {"st": [], "log": _LOG},
        {"st": good, "log": _LOG},
        {"st": [["fingerprint", "@fp"], ["seq", "3"]], "log": _LOG},
        {"st": [["fingerprint", "x"], ["seq", "3"], ["mac", ""]], "log": _LOG},
        {"st": [["fingerprint", "@fp"], ["seq", "2"], ["mac", "m2"]], "log": _LOG},
        {"st": [["fingerprint", "@fp"], ["seq", "2"], ["mac", "m2"]], "log": _LOG[:2]},
        {"st": [["fingerprint", "@fp"], ["seq", "2"], ["mac", "zz"]], "log": _LOG[:2]},
        {"st": [["fingerprint", "@fp"], ["seq", "+2"], ["mac", "m2"]], "log": _LOG[:2]},
        {"st": [["fingerprint", "@fp"], ["seq", "0"], ["mac", ""]], "log": []},
        {
            "st": [["fingerprint", "@fp"], ["seq", "0"]],
            "log": [[1, "store_created", "m1"]],
        },
        {"st": [["fingerprint", "@fp"], ["seq", ""], ["mac", ""]], "log": []},
    ]


def _mark(case: dict[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE log(seq INTEGER PRIMARY KEY, kind TEXT, mac TEXT)")
    conn.executemany("INSERT INTO log(seq, kind, mac) VALUES (?, ?, ?)", case["log"])
    if case["st"] is not None:
        conn.execute("CREATE TABLE digest_state(key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO digest_state(key, value) VALUES (?, ?)",
            [(k, sd.fingerprint() if v == "@fp" else v) for k, v in case["st"]],
        )
    answer = sd._mark(conn)
    conn.close()
    return {"k": "mark", **case, "r": answer}


def generate() -> dict[str, bytes]:
    case, _ = _query_case()
    lines = [_mark(c) for c in _mark_cases()] + [case]
    return {"store_digest_query.jsonl": jsonl(lines)}


def generate_sqlite() -> dict[str, bytes]:
    _, orders = _query_case()
    return {
        "store_digest_query_sqlite.jsonl": jsonl(
            [{"sqlite": sqlite3.sqlite_version, "orders": orders}]
        )
    }
