"""Fixtures: the plain reads of the index and the records that the search
path and ``in_force`` call (``store_reads_seams.jsonl``,
``store_reads_seams_platform.jsonl``, ``store_reads_seams_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/store/index_reads.rs`` and
``record_reads.rs``. This pins them to `store_index`'s `_ranked_batches`,
`rows_updated_since` and `inbound_links`, and to `store_records`'
`body_of`, `get_memory`, `has_memory`, `get_many`, `light_many` and
`light_by_ids` (``seed_tokens=False``), `memory_ids`, `count_memories`,
`has_memories`, `filename_for` and `load_one` (`_active_or_raise`).

Each function is called with a stand-in whose ``_conn`` is an in-memory
SQLite connection holding `store_schema.SCHEMA` (``sqlite3.Row`` rows),
as the reference's `Store` holds it. The header records every row of the
tables the reads touch (``memories``, ``memory_links``, ``tombstones``,
``log``), in insertion order, so the native side builds the same file:
the records are written by `store_rows._write_memory_row`, and some rows
are put in by hand (an id that is no ULID, a link whose source has no
row, a ``links_json`` of ``[]`` beside link rows, a stamp spelled with
``+00:00``).

A case is ``{"store", "fn", "args", "out"}``: ``out`` is ``{"ok": value}``
(a record as its canonical payload text) or ``{"raised": class, "text":
str(exc)}`` (``text`` only where the class is the reference's own, not
a pydantic ValidationError). ``ranked`` cases give ``[text, batch]``
and answer the batches; ``since`` cases give the datetime's
``isoformat()``.

``bulk`` is a store of `BULK_N` rows made by a rule the native test
repeats (`_bulk_rows`): its cases record the count and SHA-256 of the
answer's JSON instead of the answer.

``store_reads_seams_platform.jsonl`` holds the bulk ``ranked`` case
whose order follows ``bm25()`` score bits that differ by one unit in the
last place between two bodies (`_PLATFORM_CASES`).

``store_reads_seams_python.jsonl`` holds the ``ranked`` cases whose
answer is CPython's ``Cursor.fetchmany`` size check, which changed in
3.13 (a size of zero or less answers every row before 3.13; a negative one raises ValueError
in 3.13 and 3.14 with two wordings; past ``2**31 - 1`` it overflows a C
int before 3.13 and a uint32 after). All bodies and queries are ASCII.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from bettermemory import store_index, store_records
from bettermemory.log import canonical_payload
from bettermemory.models import Memory, record_payload
from bettermemory.search import fts_index_text
from bettermemory.store_rows import _write_memory_row
from bettermemory.store_schema import SCHEMA

from ._common import jsonl
from .gen_store_write import ulid

BULK_N = 1203
TABLES = ("memories", "memory_links", "tombstones", "log")
STAMP = "2001-02-03T04:05:06.000000Z"
ODD_ID = "01JÉ日本"  # no ULID: É and two CJK letters
NATIVE_ORIGIN = {"cwd": "/w", "repo": "git@example.invalid:x.git"}


class _Seat:
    _active_or_raise = store_records._active_or_raise

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _mem(n: int, body: str, updated: str = STAMP, **fields: Any) -> Memory:
    record: dict[str, Any] = {
        "id": ulid(n),
        "created": STAMP,
        "updated": updated,
        "scopes": ["global"],
        "confidence": "high",
        "source": "explicit-statement",
        "body": body,
    }
    record.update(fields)
    return Memory.model_validate(record)


def _link(kind: str, n: int, note: str | None = None) -> dict[str, Any]:
    return {"type": kind, "target_id": ulid(n), "note": note}


def _full(conn: sqlite3.Connection) -> None:
    put = [
        (_mem(1, "alpha alpha alpha beta"), "a.md", None),
        (_mem(2, "alpha beta gamma words more text"), None, None),
        (
            _mem(
                3,
                "gamma delta",
                "2001-02-03T04:05:06.500000Z",
                links=[_link("supersedes", 1), _link("extends", 2, "why")],
            ),
            "c.md",
            None,
        ),
        (
            _mem(
                4,
                "alpha kebab-case",
                "2001-02-04T00:00:00Z",
                links=[
                    _link("contradicts", 1),
                    _link("supersedes", 9),
                    _link("depends_on", 77),
                ],
            ),
            None,
            None,
        ),
        (
            _mem(
                5, "beta beta", "2002-01-01T00:00:00Z", links=[_link("supersedes", 1)]
            ),
            None,
            "mac5",
        ),
        (
            _mem(
                6, "alpha alpha", "2001-02-04T00:00:00Z", links=[_link("supersedes", 1)]
            ),
            None,
            "mac6",
        ),
        (_mem(11, "eleven lives on"), None, None),
    ]
    for memory, filename, mac in put:
        _write_memory_row(
            conn, memory, provenance="local", filename=filename, log_mac=mac
        )
    payload5 = {"memory": {"id": ulid(5)}, "native_origin": json.dumps(NATIVE_ORIGIN)}
    conn.execute(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (1, ?, NULL, 'memory_put', ?, '', 'mac5')",
        (STAMP, json.dumps(payload5)),
    )
    conn.execute(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (2, ?, NULL, 'tombstone_put', ?, 'mac5', 'mac6')",
        (STAMP, json.dumps({"memory": {"id": ulid(6)}, "native_origin": "{}"})),
    )
    row = dict(
        conn.execute("SELECT * FROM memories WHERE id = ?", (ulid(2),)).fetchone()
    )
    odd = dict(
        row, rowid=40, id=ODD_ID, updated="2001-02-03T04:05:06+00:00", links_json="[]"
    )
    plus = dict(row, rowid=41, id=ulid(12), updated="2001-02-03T04:05:07+00:00")
    for extra in (odd, plus):
        cols = ",".join(extra)
        conn.execute(
            f"INSERT INTO memories ({cols}) VALUES ({','.join('?' * len(extra))})",
            tuple(extra.values()),
        )
    for source, kind, target, note in (
        (ODD_ID, "supersedes", ulid(1), None),
        (ulid(12), "supersedes", ulid(1), "x"),
        (ulid(12), "supersedes", ulid(1), "y"),
        (ulid(50), "supersedes", ulid(1), None),
        (ulid(1), "extends", ODD_ID, None),
    ):
        conn.execute(
            "INSERT INTO memory_links (source_id, type, target_id, note) VALUES (?, ?, ?, ?)",
            (source, kind, target, note),
        )
    tomb = {
        k: v
        for k, v in row.items()
        if k not in ("rowid", "body_fts", "scopes_text", "scopes_fts")
    }
    for n, reason in (
        (9, "duplicate of one"),
        (10, "café\nline 'two'"),
        (11, "shadowed"),
    ):
        dead = dict(
            tomb, id=ulid(n), removed=STAMP, removed_reason=reason, removed_session=None
        )
        cols = ",".join(dead)
        conn.execute(
            f"INSERT INTO tombstones ({cols}) VALUES ({','.join('?' * len(dead))})",
            tuple(dead.values()),
        )


def _bulk_body(i: int) -> str:
    return " ".join(["alpha"] * (1 + i % 4) + ["filler"] * (i % 7))


def _bulk(conn: sqlite3.Connection) -> None:
    """`BULK_N` rows, row ``i`` (from 1) with id ``ulid(i)``, body
    `_bulk_body`, updated in second ``i % 60`` and each linking to the
    next with ``supersedes``."""
    for i in range(1, BULK_N + 1):
        updated = f"2001-02-03T04:05:{i % 60:02d}Z"
        memory = _mem(i, _bulk_body(i), updated, links=[_link("supersedes", i + 1)])
        _write_memory_row(conn, memory, provenance="local", filename=None, log_mac=None)


def _dump(conn: sqlite3.Connection) -> dict[str, Any]:
    out = {}
    for table in TABLES:
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        out[table] = {
            "columns": [d[0] for d in cur.description],
            "rows": [list(r) for r in cur.fetchall()],
        }
    return out


def _value(value: Any) -> Any:
    if isinstance(value, Memory):
        return canonical_payload(record_payload(value))
    if isinstance(value, list):
        return [_value(v) for v in value]
    if isinstance(value, tuple):
        return [_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _value(v) for k, v in sorted(value.items())}
    return value


def _call(seat: _Seat, fn: str, args: list[Any]) -> Any:
    if fn == "ranked":
        return list(store_index._ranked_batches(seat, args[0], args[1]))  # type: ignore[arg-type]
    if fn == "since":
        return store_index.rows_updated_since(seat, datetime.fromisoformat(args[0]))  # type: ignore[arg-type]
    if fn == "inbound":
        return store_index.inbound_links(seat, args[0], args[1])  # type: ignore[arg-type]
    return getattr(store_records, fn)(seat, *args)


def _outcome(seat: _Seat, fn: str, args: list[Any], digest: bool) -> dict[str, Any]:
    try:
        value = _value(_call(seat, fn, args))
    except Exception as exc:
        out: dict[str, Any] = {"raised": type(exc).__name__}
        if type(exc).__name__ != "ValidationError":
            out["text"] = str(exc)
        return out
    if digest:
        text = json.dumps(value, sort_keys=True, ensure_ascii=True)
        return {
            "count": len(value),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return {"ok": value}


def _cases() -> list[tuple[str, str, list[Any]]]:
    u = ulid
    out: list[tuple[str, str, list[Any]]] = []
    for text in ("alpha", "beta", "gamma delta", "kebab-case", "words", "nothing"):
        for batch in (1, 3, 1000):
            out.append(("full", "ranked", [text, batch]))
    for text, batch in (
        ("", 1),
        ("   ", 1),
        ("　\t", 3),
        ("!!! ...", 3),
        ("ALPHA Alpha", 2),
        ("beta beta beta", 1),
    ):
        out.append(("full", "ranked", [text, batch]))
    out.append(("empty", "ranked", ["alpha", 3]))
    for since in (
        "2001-02-03T04:05:06+00:00",
        "2001-02-03T04:05:06.999999+00:00",
        "2001-02-03T04:05:07+00:00",
        "2001-02-03T06:05:06+02:00",
        "2001-02-03T04:05:05.000001-00:00:01",
        "2001-02-04T00:00:00+00:00",
        "2003-01-01T00:00:00+00:00",
        "1999-01-01T00:00:00+00:00",
        "0001-01-01T00:30:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ):
        out.append(("full", "since", [since]))
    out.append(("empty", "since", ["2001-02-03T04:05:06+00:00"]))
    for ids, kinds in (
        ([u(1)], ["supersedes", "contradicts", "extends", "depends_on"]),
        ([u(1), u(2), u(77), u(9), u(1)], ["supersedes"]),
        ([u(2), ODD_ID], ["extends", "extends"]),
        ([u(1)], []),
        ([], ["supersedes"]),
        ([u(1)], ["nope"]),
        ([u(9), u(77)], ["supersedes", "depends_on"]),
    ):
        out.append(("full", "inbound", [ids, kinds]))
    for fn in ("body_of", "get_memory", "has_memory", "filename_for"):
        for key in (u(1), u(2), u(5), u(9), u(77), ODD_ID, "", u(12)):
            out.append(("full", fn, [key]))
    for ids in ([u(1), u(77), u(1), u(2)], [], [u(5), u(6)], [u(3), ODD_ID]):
        out.append(("full", "get_many", [ids]))
    for rowids in ([4, 1, 4, 99, 2], [], [5, 6], [-1, 0, 41], [40]):
        out.append(("full", "light_many", [rowids]))
    for ids in ([u(3), u(1), u(3), u(77)], [], [u(5), u(9)], [ODD_ID]):
        out.append(("full", "light_by_ids", [ids]))
    for store in ("full", "empty"):
        for fn in ("memory_ids", "count_memories", "has_memories"):
            out.append((store, fn, []))
    for key in (
        u(1),
        u(5),
        u(9),
        u(10),
        u(11),
        u(77),
        "abc",
        "",
        u(1).lower(),
        ODD_ID,
        u(1) + " ",
        "01J" + "U" * 23,
    ):
        out.append(("full", "load_one", [key]))
    return out


#: A bulk case's argument spelled by its rule: ``@rowids`` is the row
#: numbers ``BULK_N + 2`` down to 1, ``@ids`` the ids ``ulid(1)`` to
#: ``ulid(BULK_N + 2)``.
def _bulk_arg(arg: Any) -> Any:
    if arg == "@rowids":
        return list(range(BULK_N + 2, 0, -1))
    if arg == "@ids":
        return [ulid(i) for i in range(1, BULK_N + 3)]
    return arg


def _bulk_cases() -> list[tuple[str, str, list[Any]]]:
    return [
        ("bulk", "ranked", ["alpha", 1000]),
        ("bulk", "since", ["2001-02-03T04:05:30+00:00"]),
        ("bulk", "light_many", ["@rowids"]),
        ("bulk", "light_by_ids", ["@ids"]),
        ("bulk", "inbound", ["@ids", ["supersedes"]]),
        ("bulk", "memory_ids", []),
    ]


#: Bulk ``ranked`` cases whose order turns on ``bm25()`` score bits: under
#: "alpha filler" two classes of body score one unit in the last place
#: apart (-3.0035701352031004e-06 and -3.0035701352031e-06 on
#: darwin-arm64), and the platform's `log` and double arithmetic decide
#: which ranks first: CI's Linux runners answer another digest.
_PLATFORM_CASES: list[tuple[str, str, list[Any]]] = [
    ("bulk", "ranked", ["alpha filler", 7]),
]


_PYTHON_CASES: list[tuple[str, str, list[Any]]] = [
    ("full", "ranked", ["alpha", 0]),
    ("full", "ranked", ["alpha", -1]),
    ("full", "ranked", ["alpha", 2**32 - 1]),
    ("full", "ranked", ["alpha", 2**32]),
    ("full", "ranked", ["", -1]),
]


def _stores() -> dict[str, sqlite3.Connection]:
    full = _connect()
    _full(full)
    bulk = _connect()
    _bulk(bulk)
    return {"full": full, "empty": _connect(), "bulk": bulk}


def _file(
    name: str,
    cases: list[tuple[str, str, list[Any]]],
    tables: bool,
    bulk: bool,
) -> bytes:
    stores = _stores()
    lines = []
    for store, fn, args in cases:
        real = [_bulk_arg(a) for a in args] if store == "bulk" else args
        out = _outcome(_Seat(stores[store]), fn, real, store == "bulk")
        lines.append({"store": store, "fn": fn, "args": args, "out": out})
    header: dict[str, Any] = {"fixture": name, "cases": len(lines)}
    if tables:
        header["tables"] = _dump(stores["full"])
    if bulk:
        header["bulk"] = {
            "n": BULK_N,
            "first": _dump_row(stores["bulk"], 1),
            "body_fts_7": fts_index_text(_bulk_body(7)),
        }
    return jsonl([header, *lines])


def _dump_row(conn: sqlite3.Connection, rowid: int) -> dict[str, Any]:
    return dict(
        conn.execute("SELECT * FROM memories WHERE rowid = ?", (rowid,)).fetchone()
    )


def generate() -> dict[str, bytes]:
    return {
        "store_reads_seams.jsonl": _file(
            "store_reads_seams", _cases() + _bulk_cases(), True, True
        )
    }


def generate_platform() -> dict[str, bytes]:
    """The bulk ``ranked`` cases whose order follows ``bm25()`` score bits
    (the file is gated on the platform). The header carries the bulk rule
    and no tables: the native test builds the bulk store from it."""
    return {
        "store_reads_seams_platform.jsonl": _file(
            "store_reads_seams_platform", _PLATFORM_CASES, False, True
        )
    }


def generate_python() -> dict[str, bytes]:
    """The ``fetchmany`` size checks, as the running CPython answers them
    (the file is gated on the release)."""
    return {
        "store_reads_seams_python.jsonl": _file(
            "store_reads_seams_python", _PYTHON_CASES, False, False
        )
    }
