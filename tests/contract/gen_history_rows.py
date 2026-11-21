"""Fixtures: `history`'s first unit, the rows that name a record
(``history_rows.jsonl``, ``history_rows_python.jsonl``), and its
second, `_RowVerifier` and `_set` (``history_rows_mac.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/history.rs``. This pins it to
`read_snapshot`, `_rows_naming`, `_with_linking_versions`,
`_with_restores`, `_replaced_rows`, `_payload`, `_mapping`, `_subject`,
`_names_as_event` and `_link_types`.

The store is the reference's ``log`` table (its statements, cut out of
`store_schema.SCHEMA`, are the header's ``ddl``) in an in-memory SQLite
database: those functions read nothing else of a store. A case's ``t``
is its rows, each ``{"s": seq, "k": kind, "p": payload}`` with ``ts``,
``se`` (session), ``pm`` (prev_mac) and ``m`` (mac) when they are not
``"t"``, null, ``"pm"`` and ``"m"``. A cell is a string (text), null,
``{"b": hex}`` (a blob), ``{"x": hex}`` (those bytes as text, which
need not be UTF-8) or ``{"nest": [open, n, inner, close, pre, post]}``
(``pre``, ``open`` ``n`` times, ``inner``, ``close`` ``n`` times and
``post``, as text; ``pre`` and ``post`` empty when left out).

Each call ``q`` holds ``do`` and its answer ``r`` (or ``raised``, the
class raised, with ``col`` for a column that is not UTF-8):

- ``rows``: `_rows_naming(store, id, kinds=k)`; ``r`` the rows;
- ``links``: `_with_linking_versions(store, id, rows)`, the rows those
  of ``from`` (seqs, in that order) or, without it, `_rows_naming`'s
  for ``id``; ``r`` ``{"f": [[row, parsed], ...], "src": [...]}``;
- ``restores``: `_with_restores(store, found)`, found being the rows of
  ``from`` each with its `_payload`; ``r`` ``[[row, parsed], ...]``;
- ``replaced``: `list(_replaced_rows(store, after))`;
- ``snapshot``: `read_snapshot` with ``mode`` ``plain``, ``held`` (a
  transaction is open before) or ``ends`` (the body commits); ``r``
  ``[in_transaction inside, in_transaction after]``.

A row is its seq when every cell is the table's text as given; else
``[seq, ts, session, kind, payload, prev_mac, mac]`` as `str` spells
them. ``parsed`` is 1 when `_payload` read a dict.

Pure cases, by ``f``: ``payload`` (``p`` a payload text, or ``pn`` a
``nest`` spelling of one; ``r`` the dict
in `_common.neutral` form, or null), ``parsed`` (whether `_payload`
read a dict: a value nested that deep has no `neutral` form),
``subject`` (``kind``, ``p``),
``names`` (``kind``, ``p``, ``id``) and ``types`` (``kind``, ``p``,
``id``; ``r`` sorted). An id or a text holding a lone surrogate is
under ``*_cps``.

A case goes in ``_python`` where a payload nests near what `json.loads`
reads before RecursionError, which is the interpreter's. Each nests half
``depth`` or less, or ten levels past it: the exact bound moves with the
caller's own C stack (a nest of 9,997 objects raises inside this
generator, and of 9,988 under pytest, and reads at the top level of an
interpreter).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import types
from typing import Any

from bettermemory import history as h
from bettermemory.log import LogRow, compute_mac, fingerprint, is_fingerprint
from bettermemory.store_schema import SCHEMA

from ._common import has_surrogate, jsonl, neutral, text_field

_START = "CREATE TABLE IF NOT EXISTS log ("
_END = "CREATE TABLE IF NOT EXISTS verifications"
DDL = SCHEMA[SCHEMA.index(_START) : SCHEMA.index(_END)].strip() + "\n"

A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
B = "01BX5ZZKBKACTAV9WEVGEMMVRZ"
C = "01CX5ZZKBKACTAV9WEVGEMMVS0"
D = "01DX5ZZKBKACTAV9WEVGEMMVS1"
DEPTH = 9998


def _cell(value: Any) -> Any:
    if isinstance(value, dict):
        if "b" in value:
            return bytes.fromhex(value["b"])
        if "nest" in value:
            o, n, inner, c, *around = value["nest"]
            pre, post = around or ["", ""]
            return pre + o * n + inner + c * n + post
    return value


def _insert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cells = [
        row["s"],
        row.get("ts", "t"),
        row.get("se"),
        row["k"],
        row["p"],
        row.get("pm", "pm"),
        row.get("m", "m"),
    ]
    marks = []
    params: list[Any] = []
    for cell in cells:
        if isinstance(cell, dict) and "x" in cell:
            marks.append("CAST(? AS TEXT)")
            params.append(bytes.fromhex(cell["x"]))
        else:
            marks.append("?")
            params.append(_cell(cell))
    conn.execute(f"INSERT INTO log VALUES ({', '.join(marks)})", params)


def _store(table: list[dict[str, Any]]) -> Any:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    for row in table:
        _insert(conn, row)
    conn.commit()
    return types.SimpleNamespace(conn=conn)


def _plain(table: list[dict[str, Any]]) -> dict[int, list[str | None]]:
    """The rows whose every cell is text as given: their `str` spelling."""
    out: dict[int, list[str | None]] = {}
    for row in table:
        cells = [row.get("ts", "t"), row.get("se"), row["k"], row["p"]]
        cells = [_cell(c) for c in [*cells, row.get("pm", "pm"), row.get("m", "m")]]
        if all(c is None or isinstance(c, str) for c in cells):
            out[row["s"]] = [c for c in cells]
    return out


def _row(row: LogRow, plain: dict[int, list[str | None]]) -> Any:
    cells = [row.ts, row.session, row.kind, row.payload, row.prev_mac, row.mac]
    if plain.get(row.seq) == cells:
        return row.seq
    return [row.seq, *cells]


def _raised(e: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"raised": type(e).__name__}
    text = str(e)
    if text.startswith("Could not decode to UTF-8 column '"):
        out["col"] = text.split("'")[1]
    elif isinstance(e, sqlite3.Error):
        out["msg"] = text
    return out


def _id(q: dict[str, Any]) -> str:
    if "id_cps" in q:
        return "".join(chr(c) for c in q["id_cps"])
    return str(q["id"])


def _pick(store: Any, seqs: list[int]) -> list[LogRow]:
    every = {row.seq: row for row in h._rows_naming(store, "")}
    return [every[s] for s in seqs]


def _call(store: Any, q: dict[str, Any], plain: dict[int, list[str | None]]) -> Any:
    do = q["do"]
    if do == "rows":
        kinds = tuple(
            "".join(chr(c) for c in k) if isinstance(k, list) else k
            for k in q.get("k", [])
        )
        return [_row(r, plain) for r in h._rows_naming(store, _id(q), kinds=kinds)]
    if do == "links":
        rid = _id(q)
        rows = _pick(store, q["from"]) if "from" in q else h._rows_naming(store, rid)
        found, sources = h._with_linking_versions(store, rid, rows)
        return {
            "f": [[_row(r, plain), int(p is not None)] for r, p in found],
            "src": [text_field("v", s) for s in sorted(sources)],
        }
    if do == "restores":
        found = [(r, h._payload(r)) for r in _pick(store, q["from"])]
        return [
            [_row(r, plain), int(p is not None)]
            for r, p in h._with_restores(store, found)
        ]
    if do == "replaced":
        return [_row(r, plain) for r in h._replaced_rows(store, q["after"])]
    if do == "snapshot":
        conn = store.conn
        if q["mode"] == "held":
            conn.execute("BEGIN")
        inside = None
        try:
            with h.read_snapshot(conn):
                inside = conn.in_transaction
                if q["mode"] == "ends":
                    conn.execute("COMMIT")
        finally:
            after = conn.in_transaction
            if q["mode"] == "held":
                conn.execute("ROLLBACK")
        return [inside, after]
    raise AssertionError(do)


def _table_case(
    name: str, table: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> Any:
    store = _store(table)
    plain = _plain(table)
    out = []
    for q in calls:
        try:
            answer: dict[str, Any] = {"r": _call(store, q, plain)}
        except (sqlite3.Error, UnicodeEncodeError, RecursionError, ValueError) as e:
            answer = _raised(e)
        if q["do"] == "snapshot" and q["mode"] == "ends" and "raised" in answer:
            answer["after"] = store.conn.in_transaction
        out.append({**q, **answer})
    store.conn.close()
    return {"f": "table", "case": name, "t": table, "q": out}


def _pure(f: str, kind: str, p: Any, rid: str | None = None) -> dict[str, Any]:
    case: dict[str, Any] = {"f": f, "kind": kind}
    if isinstance(p, dict):
        case["pn"] = p["nest"]
        p = _cell(p)
    else:
        case.update(text_field("p", p))
    if rid is not None:
        case.update(text_field("id", rid))
    try:
        if f in ("payload", "parsed"):
            row = LogRow(1, "t", None, kind, p, "pm", "m")
            got = h._payload(row)
            if f == "parsed":
                case["r"] = got is not None
            else:
                case["r"] = None if got is None else neutral(got)
            return case
        payload = h._payload(LogRow(1, "t", None, kind, p, "pm", "m"))
        assert payload is not None, p
        if f == "subject":
            s = h._subject(kind, payload)
            case["r"] = None if s is None else text_field("v", s)
        elif f == "names":
            assert rid is not None
            case["r"] = h._names_as_event(kind, payload, rid)
        elif f == "types":
            assert rid is not None
            case["r"] = [
                text_field("v", t) for t in sorted(h._link_types(kind, payload, rid))
            ]
    except (ValueError, RecursionError) as e:
        case.update(_raised(e))
    return case


def _mem(rid: str, links: list[Any] | None = None, **extra: Any) -> str:
    import json

    record: dict[str, Any] = {"id": rid, "body": "b"}
    if links is not None:
        record["links"] = links
    record.update(extra)
    return json.dumps({"memory": record})


def _tomb(rid: str, links: list[Any] | None = None) -> str:
    import json

    out: dict[str, Any] = {"tombstone": {"id": rid}}
    if links is not None:
        out["links"] = links
    return json.dumps(out)


def _link(target: str, kind: Any = "related") -> dict[str, Any]:
    return {"type": kind, "target_id": target}


def _pure_cases() -> list[dict[str, Any]]:
    import json

    out: list[dict[str, Any]] = []
    for p in [
        "{}",
        '{"a": 1, "a": 2}',
        "[]",
        '"text"',
        "1",
        "null",
        "not json",
        "",
        '{"a": NaN, "b": Infinity, "c": -0.0, "d": 1e400}',
        '{"s": "\\ud800", "t": "\\u00e9\\ud83d\\ude00"}',
        '  {"x": [1, {"y": null}]}  ',
        '{"big": ' + "9" * 4300 + "}",
        '{"big": ' + "9" * 4301 + "}",
        "{'single': 1}",
        '{"a": 1} trailing',
        "﻿{}",
    ]:
        out.append(_pure("payload", "memory_put", p))
    subject_payloads = {
        "memory_put": [
            '{"memory": {"id": "A"}}',
            '{"memory": {"id": 5}}',
            '{"memory": ["A"]}',
            "{}",
        ],
        "tombstone_put": [
            '{"tombstone": {"id": "\\ud801"}}',
            '{"memory": {"id": "A"}}',
        ],
        "episode_put": ['{"episode": {"id": "E"}}'],
        "memory_delete": ['{"id": "A"}', '{"id": null}'],
        "tombstone_delete": ['{"id": "A"}'],
        "episode_delete": ['{"id": ["A"]}'],
        "verification_put": ['{"memory_id": "A"}', '{"id": "A"}'],
        "verification_delete": ['{"memory_id": "A"}'],
        "home_put": ['{"home": {"record_id": "A"}}', '{"home": "A"}'],
        "standing_put": ['{"standing": {"record_id": "A"}}'],
        "returned_put": ['{"returned": {"record_id": "A"}}', '{"record_id": "A"}'],
        "home_delete": ['{"record_id": "A"}'],
        "standing_delete": ['{"record_id": true}'],
        "returned_delete": ['{"record_id": "A"}'],
        "capture_member": ['{"memory_id": "A"}'],
        "import_put": ['{"memory_id": "A"}'],
        "write": ['{"id": "A"}'],
        "memory_put_x": ['{"memory": {"id": "A"}}'],
        "Memory_put": ['{"memory": {"id": "A"}}'],
        "": ['{"id": "A"}'],
    }
    for kind, ps in subject_payloads.items():
        for p in ps:
            out.append(_pure("subject", kind, p))
    names = [
        ("write", '{"id": "A"}', "A"),
        ("update", '{"id": "A"}', "B"),
        ("verify", '{"id": ["A"]}', "A"),
        ("remove", "{}", "A"),
        ("restore", '{"id": "\\ud800"}', "\ud800"),
        ("standing_guard", '{"standing": ["B", "A"]}', "A"),
        ("standing_guard", '{"standing": "A"}', "A"),
        ("standing_guard", '{"standing": {"A": 1}}', "A"),
        ("standing_guard", '{"standing": [["A"]]}', "A"),
        ("standing_guard", '{"id": "A"}', "A"),
        ("memory_put", '{"id": "A"}', "A"),
        ("search", '{"id": "A"}', "A"),
        ("write", '{"id": "A"}', ""),
        ("write", '{"id": ""}', ""),
    ]
    for kind, p, rid in names:
        out.append(_pure("names", kind, p, rid))
    many = [
        _link("A", t)
        for t in ["supersedes", "related", "supersedes", None, 1, 1.5, True]
    ]
    types_cases = [
        ("memory_put", _mem("B", [_link("A")]), "A"),
        ("memory_put", _mem("B", many), "A"),
        ("memory_put", _mem("B", [_link("A"), _link("C", "x")]), "C"),
        ("memory_put", _mem("B", [{"target_id": "A"}]), "A"),
        ("memory_put", _mem("B", [_link("A", {"k": [1, "v"]})]), "A"),
        ("memory_put", _mem("B", [_link("A", "\ud800")]), "A"),
        ("memory_put", _mem("B", [_link("A", [1e16, -0.0, 1e-7])]), "A"),
        ("memory_put", _mem("B", [["A"], "A", None, _link("A")]), "A"),
        ("memory_put", _mem("B", {"x": _link("A")}), "A"),  # type: ignore[arg-type]
        ("memory_put", _mem("B"), "A"),
        ("memory_put", json.dumps({"memory": "B", "links": [_link("A")]}), "A"),
        ("memory_put", json.dumps({"links": [_link("A")]}), "A"),
        ("tombstone_put", _tomb("B", [_link("A")]), "A"),
        (
            "tombstone_put",
            json.dumps({"tombstone": {"id": "B", "links": [_link("A")]}}),
            "A",
        ),
        ("episode_put", json.dumps({"links": [_link("A", "e")]}), "A"),
        ("anything", json.dumps({"links": [_link("A", "z"), _link("A", "a")]}), "A"),
        ("memory_put", _mem("B", [_link("a")]), "A"),
        ("memory_put", _mem("B", [{"type": "t", "target_id": ["A"]}]), "A"),
    ]
    for kind, p, rid in types_cases:
        out.append(_pure("types", kind, p, rid))
    return out


def _table_cases() -> list[dict[str, Any]]:
    links_table = [
        {"s": 1, "k": "memory_put", "p": _mem(A)},
        {"s": 2, "k": "write", "p": '{"id": "%s"}' % A},
        {"s": 3, "k": "memory_put", "p": _mem(B, [_link(A, "supersedes")])},
        {"s": 4, "k": "memory_put", "p": _mem(B, [])},
        {"s": 5, "k": "memory_delete", "p": '{"id": "%s"}' % B},
        {"s": 6, "k": "tombstone_put", "p": _tomb(B, [])},
        {"s": 7, "k": "tombstone_put", "p": _tomb(C, [_link(A)])},
        {"s": 8, "k": "tombstone_delete", "p": '{"id": "%s"}' % C},
        {"s": 9, "k": "search", "p": '{"q": "%s %s"}' % (B, C)},
        {"s": 10, "k": "store_restored", "p": '{"bundle_created_at": "x"}'},
        {
            "s": 11,
            "k": "episode_put",
            "p": '{"episode": {"id": "%s", "links": []}}' % B,
        },
        {"s": 12, "k": "history_accepted", "p": "not json"},
        {"s": 13, "k": "memory_put", "p": _mem(D, [_link(A, "x"), _link(A, "y")])},
        {"s": 14, "k": "memory_put", "p": _mem(A, [_link(A, "self")])},
        {"s": 15, "k": "memory_put", "p": _mem("x" + D + "y")},
        {"s": 16, "k": "standing_guard", "p": '{"standing": ["%s"]}' % A},
        {"s": 17, "k": "store_restored", "p": "{}"},
    ]
    cases = [
        _table_case(
            "links",
            links_table,
            [
                {"do": "rows", "id": A},
                {"do": "rows", "id": A, "k": ["memory_put", "write"]},
                {"do": "rows", "id": A, "k": ["nothing"]},
                {"do": "rows", "id": B, "k": ["memory_put", "memory_put"]},
                {"do": "rows", "id": "01ARZ3"},
                {"do": "rows", "id": "zzz"},
                {"do": "rows", "id": "01arz3ndektsv4rrffq69g5fav"},
                {"do": "links", "id": A},
                {"do": "links", "id": B},
                {"do": "links", "id": D},
                {"do": "links", "id": A, "from": []},
                {"do": "links", "id": A, "from": [13, 3, 13, 7]},
                {"do": "links", "id": A, "from": [14]},
                {"do": "restores", "from": []},
                {"do": "restores", "from": [1, 2]},
                {"do": "restores", "from": [11, 1]},
                {"do": "restores", "from": [10, 12, 10]},
                {"do": "restores", "from": [17]},
                {"do": "replaced", "after": 0},
                {"do": "replaced", "after": 10},
                {"do": "replaced", "after": 17},
                {"do": "replaced", "after": -(2**63)},
                {"do": "snapshot", "mode": "plain"},
                {"do": "snapshot", "mode": "held"},
                {"do": "snapshot", "mode": "ends"},
            ],
        ),
        _table_case(
            "empty",
            [],
            [
                {"do": "rows", "id": ""},
                {"do": "links", "id": A},
                {"do": "replaced", "after": 0},
                {"do": "snapshot", "mode": "plain"},
            ],
        ),
        _table_case(
            "cells",
            [
                {"s": -5, "k": "memory_put", "p": _mem(A), "se": "s1"},
                {"s": 0, "k": "write", "p": '{"id": "%s"}' % A, "ts": {"b": "00ff"}},
                {"s": 3, "k": {"b": "6b"}, "p": {"b": (A.encode().hex())}},
                {"s": 4, "k": "store_restored", "p": "{}", "se": {"b": ""}},
                {"s": 5, "k": "history_accepted", "p": "{}", "pm": {"b": "2722"}},
                {"s": 6, "k": "memory_put", "p": _mem(A), "m": {"x": "c3a9"}},
                {"s": 2**63 - 1, "k": "history_accepted", "p": "{}"},
                {"s": 7, "k": "x", "p": "é %s \U0001f600" % A, "se": "é"},
            ],
            [
                {"do": "rows", "id": A},
                {"do": "rows", "id": ""},
                {"do": "rows", "id": "é"},
                {"do": "rows", "id": "\U0001f600"},
                {"do": "replaced", "after": 0},
                {"do": "replaced", "after": -6},
                {"do": "restores", "from": [-5]},
                {"do": "links", "id": A, "from": [-5, 0]},
            ],
        ),
        _table_case(
            "not utf-8",
            [
                {"s": 1, "k": "memory_put", "p": _mem(A)},
                {
                    "s": 2,
                    "k": "memory_put",
                    "p": _mem(A),
                    "ts": {"x": "ff"},
                    "m": {"x": "fe"},
                },
                {"s": 3, "k": "store_restored", "p": "{}", "se": {"x": "80"}},
                {"s": 4, "k": "store_restored", "p": {"x": "ff"}},
            ],
            [
                {"do": "rows", "id": A},
                {"do": "rows", "id": A, "k": ["store_restored"]},
                {"do": "replaced", "after": 0},
                {"do": "replaced", "after": 3},
                {"do": "rows", "id": "{}"},
            ],
        ),
        _table_case(
            "surrogates",
            [
                {"s": 1, "k": "memory_put", "p": _mem(A)},
                {"s": 2, "k": "memory_put", "p": _mem("\ud800" + B, [_link(A)])},
                {"s": 3, "k": "memory_put", "p": _mem("B\udfff", [_link(A)])},
                {"s": 4, "k": "memory_put", "p": "\x00" + _mem(C, [_link(A)])},
                {"s": 5, "k": "memory_put", "p": _mem(C + "\x00", [_link(A)])},
            ],
            [
                {"do": "rows", "id_cps": [0xD800]},
                {"do": "rows", "id": A, "k": [[0xDC00]]},
                {"do": "rows", "id": "\x00"},
                {"do": "rows", "id": C + "\x00"},
                {"do": "links", "id": A},
                {"do": "links", "id": A, "from": [1, 3]},
                {"do": "links", "id": A, "from": [5, 4]},
            ],
        ),
    ]
    return cases


def _python_cases() -> list[dict[str, Any]]:
    deep: dict[str, Any] = {"nest": ["[", DEPTH // 2, "", "]"]}
    deeper: dict[str, Any] = {"nest": ["[", DEPTH + 10, "", "]"]}
    inner = (
        '{"memory": {"id": "%s", "links": [{"type": "t", "target_id": "%s"}]}, "x": '
        % (B, A)
    )
    return [
        _pure("parsed", "memory_put", deep),
        _pure("parsed", "memory_put", deeper),
        _pure("parsed", "memory_put", {"nest": ['{"a":', DEPTH // 2, "1", "}"]}),
        _pure("parsed", "memory_put", {"nest": ['{"a":', DEPTH // 3, "1", "}"]}),
        _pure("parsed", "memory_put", {"nest": ['{"a":', DEPTH + 10, "1", "}"]}),
        _pure(
            "types", "memory_put", {"nest": ["[", DEPTH // 2, "", "]", inner, "}"]}, A
        ),
        _table_case(
            "deep",
            [
                {"s": 1, "k": "memory_put", "p": _mem(A)},
                {"s": 2, "k": "store_restored", "p": deep},
                {"s": 3, "k": "history_accepted", "p": deeper},
                {
                    "s": 4,
                    "k": "memory_put",
                    "p": {"nest": ["[", DEPTH + 10, "", "]", inner, "}"]},
                },
                {"s": 5, "k": "memory_put", "p": _mem(B) + " " + A},
                {
                    "s": 6,
                    "k": "memory_delete",
                    "p": {"nest": ["[", DEPTH + 10, B, "]"]},
                },
                {"s": 7, "k": "memory_put", "p": _mem(C, [_link(A)])},
                {
                    "s": 8,
                    "k": "memory_delete",
                    "p": {"nest": ["[", DEPTH + 10, '"%s"' % C, "]"]},
                },
                {"s": 9, "k": "memory_put", "p": _mem(D, [_link(A)])},
                {
                    "s": 10,
                    "k": "tombstone_delete",
                    "p": {"nest": ["[", DEPTH // 2, '"%s"' % D, "]"]},
                },
            ],
            [
                {"do": "rows", "id": A},
                {"do": "links", "id": A, "from": [1]},
                {"do": "links", "id": A, "from": [1, 4]},
                {"do": "links", "id": A, "from": [7]},
                {"do": "links", "id": A, "from": [9]},
                {"do": "links", "id": A},
                {"do": "restores", "from": [1]},
                {"do": "restores", "from": [3]},
                {"do": "restores", "from": [2]},
            ],
        ),
    ]


def _file(cases: list[dict[str, Any]]) -> bytes:
    for case in cases:
        for value in case.values():
            assert not (isinstance(value, str) and has_surrogate(value)), case
    header = {"cases": len(cases), "ddl": DDL, "depth": DEPTH}
    return jsonl([header, *cases])


def generate() -> dict[str, bytes]:
    return {"history_rows.jsonl": _file(_pure_cases() + _table_cases())}


def generate_python() -> dict[str, bytes]:
    return {"history_rows_python.jsonl": _file(_python_cases())}


# ---------------------------------------------------------------------------
# `_RowVerifier` and `_set` (``history_rows_mac.jsonl``)
# ---------------------------------------------------------------------------
#
# Each case's ``t`` is a log table as above, with ``mk`` naming the key
# whose `compute_mac` over the row is its ``mac`` (``m`` stays literal
# when ``mk`` is absent). The keys are ``KEYS`` by name: SHA-256 of the
# name's bytes. The store is duck-typed: ``conn``, ``key_fingerprint``
# (``fp``: a key's fingerprint by name, or literal text under ``fp_text``)
# and a ``keyring`` holding ``current`` and ``retired`` keys by name,
# whose ``key_for_fingerprint`` is `KeyRing.key_for_fingerprint` without
# files: a fingerprint-shaped name, then the current key and the retired
# key filed under the name's 16-character prefix, matched by fingerprint.
# ``r`` is ``[[seq, verified], ...]`` for each row of ``check`` (every row
# when absent), or ``raised``. ``set`` cases hold ``d`` (the detail's
# pairs) and ``v`` (the values' pairs, null meaning None) and ``r`` the
# pairs `_set` leaves.

KEYS = {name: hashlib.sha256(name.encode()).digest() for name in ("k1", "k2", "k3")}


def _fp(name: str) -> str:
    return fingerprint(KEYS[name])


class _Ring:
    def __init__(self, current: str | None, retired: list[str]) -> None:
        self.current = KEYS[current] if current else None
        self.retired = {_fp(n)[:16]: KEYS[n] for n in retired}

    def key_for_fingerprint(self, name: str) -> bytes | None:
        if not is_fingerprint(name):
            return None
        for key in (self.current, self.retired.get(name[:16])):
            if key is not None and fingerprint(key) == name:
                return key
        return None


def _rekey(old: Any, new: Any) -> str:
    return json.dumps({"old_fingerprint": old, "new_fingerprint": new})


def _macked(row: dict[str, Any]) -> dict[str, Any]:
    if "mk" not in row:
        return row
    out = dict(row)
    out["m"] = compute_mac(
        KEYS[row["mk"]],
        seq=row["s"],
        ts=row.get("ts", "t"),
        session=row.get("se"),
        kind=row["k"],
        payload=row["p"],
        prev_mac=row.get("pm", "pm"),
    )
    return out


def _mac_case(name: str, table: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    case: dict[str, Any] = {"name": name, "t": table, **kw}
    store = _store([_macked(row) for row in table])
    store.key_fingerprint = (
        kw["fp_text"] if "fp_text" in kw else _fp(kw.get("fp", "k1"))
    )
    store.keyring = _Ring(kw.get("current", "k1"), kw.get("retired", []))
    try:
        verifier = h._RowVerifier(store)
    except (sqlite3.Error, RecursionError) as e:
        case.update(_raised(e))
        return case
    rows = {row.seq: row for row in h._rows_naming(store, "{")}
    seqs = kw.get("check", sorted(rows))
    case["r"] = [[s, verifier.verified(rows[s])] for s in seqs]
    return case


def _mac_cases() -> list[dict[str, Any]]:
    F1, F2, F3 = _fp("k1"), _fp("k2"), _fp("k3")
    w = {"k": "write", "p": "{}"}
    return [
        _mac_case("no rekey", [{"s": 1, **w, "mk": "k1"}, {"s": 2, **w, "mk": "k1"}]),
        _mac_case("no rekey, key absent", [{"s": 1, **w, "mk": "k1"}], current="k2"),
        _mac_case("tampered", [{"s": 1, **w, "mk": "k1"}, {"s": 2, **w}]),
        _mac_case(
            "wrong key",
            [{"s": 1, **w, "mk": "k2"}],
            current="k1",
            retired=["k2"],
        ),
        _mac_case(
            "one rekey",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 3, **w, "mk": "k2"},
                {"s": 4, **w, "mk": "k1"},
            ],
            fp="k2",
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "one rekey, old key only retired, store fingerprint ignored",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 5, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 6, **w, "mk": "k2"},
            ],
            fp="k3",
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "old key absent",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 3, **w, "mk": "k2"},
            ],
            current="k2",
        ),
        _mac_case(
            "several rekeys",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 3, **w, "mk": "k2"},
                {"s": 4, "k": "rekey", "p": _rekey(F2, F3), "mk": "k3"},
                {"s": 5, **w, "mk": "k3"},
                {"s": 6, **w, "mk": "k2"},
            ],
            current="k3",
            retired=["k1", "k2"],
        ),
        _mac_case(
            "rekey at seq 0 and below",
            [
                {"s": -2, **w, "mk": "k1"},
                {"s": 0, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 1, **w, "mk": "k2"},
                {"s": 2, **w, "mk": "k1"},
            ],
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "rekey at seq 1 after the first segment",
            [
                {"s": 1, "k": "rekey", "p": _rekey(F1, F2), "mk": "k2"},
                {"s": 2, **w, "mk": "k2"},
            ],
            current="k2",
        ),
        _mac_case(
            "skipped rows: bad JSON, blob, list, str, int, null, missing keys",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": "{not json"},
                {"s": 3, "k": "rekey", "p": {"b": _rekey(F1, F2).encode().hex()}},
                {"s": 4, "k": "rekey", "p": json.dumps([F1, F2])},
                {"s": 5, "k": "rekey", "p": json.dumps("old_fingerprint")},
                {"s": 6, "k": "rekey", "p": "7"},
                {"s": 7, "k": "rekey", "p": "null"},
                {"s": 8, "k": "rekey", "p": json.dumps({"old_fingerprint": F1})},
                {"s": 9, "k": "rekey", "p": json.dumps({"new_fingerprint": F2})},
                {"s": 10, "k": "rekey", "p": "1" * 4301},
                {"s": 11, **w, "mk": "k1"},
            ],
            check=[1, 11],
        ),
        _mac_case(
            "non-str fingerprints through str()",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": _rekey(None, [F2])},
                {"s": 3, **w, "mk": "k2"},
                {"s": 4, "k": "rekey", "p": _rekey(1.0, True)},
                {"s": 5, **w, "mk": "k2"},
                {"s": 6, "k": "rekey", "p": _rekey({"a": [1, None]}, F2)},
                {"s": 7, **w, "mk": "k2"},
            ],
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "first segment named by an unusable fingerprint",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": _rekey(F1.upper(), F2)},
                {"s": 3, **w, "mk": "k2"},
            ],
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "duplicate keys in a payload: the last wins",
            [
                {"s": 1, **w, "mk": "k1"},
                {
                    "s": 2,
                    "k": "rekey",
                    "p": '{"new_fingerprint":"x","old_fingerprint":"%s","new_fingerprint":"%s"}'
                    % (F1, F2),
                },
                {"s": 3, **w, "mk": "k2"},
            ],
            current="k2",
            retired=["k1"],
        ),
        _mac_case(
            "store fingerprint not a fingerprint",
            [{"s": 1, **w, "mk": "k1"}],
            fp_text="k1",
        ),
        _mac_case(
            "store fingerprint empty",
            [{"s": 1, **w, "mk": "k1"}],
            fp_text="",
        ),
        _mac_case(
            "payload not UTF-8",
            [
                {"s": 1, **w, "mk": "k1"},
                {"s": 2, "k": "rekey", "p": {"x": "ff"}},
            ],
        ),
        _mac_case(
            "a non-UTF-8 payload of another kind is not read",
            [{"s": 1, **w, "mk": "k1"}, {"s": 2, "k": "write", "p": {"x": "ff"}}],
            check=[1],
        ),
        _mac_case(
            "session and fields in the MAC",
            [
                {
                    "s": 1,
                    "k": "write",
                    "p": "{}",
                    "se": "s",
                    "ts": "x",
                    "pm": "",
                    "mk": "k1",
                },
                {"s": 2, "k": "write", "p": "{}", "se": "", "mk": "k1"},
            ],
        ),
    ]


def _two_at_one_seq() -> dict[str, Any]:
    """Two rekeys at one seq cannot sit in the table (seq is its key);
    the nearest is two adjacent rekeys, the last at or before a row
    winning."""
    F1, F2, F3 = _fp("k1"), _fp("k2"), _fp("k3")
    w = {"k": "write", "p": "{}"}
    return _mac_case(
        "adjacent rekeys",
        [
            {"s": 1, **w, "mk": "k1"},
            {"s": 3, "k": "rekey", "p": _rekey(F1, F2)},
            {"s": 4, "k": "rekey", "p": _rekey(F2, F3)},
            {"s": 5, **w, "mk": "k3"},
        ],
        current="k3",
        retired=["k1", "k2"],
    )


def _set_cases() -> list[dict[str, Any]]:
    out = []
    cases: list[tuple[list[list[Any]], list[list[Any]]]] = [
        ([], []),
        ([], [["a", 1], ["b", None], ["c", "x"]]),
        ([["a", 0], ["b", 1]], [["a", 2], ["c", None], ["d", False]]),
        ([["a", None]], [["a", None], ["b", 0]]),
        ([["z", 1]], [["y", ""], ["z", []]]),
    ]
    for d, v in cases:
        detail = {str(k): x for k, x in d}
        out.append(
            {
                "name": "set",
                "d": d,
                "v": v,
                "r": list(h._set(detail, **{str(k): x for k, x in v}).items()),
            }
        )
    return out


def generate_mac() -> dict[str, bytes]:
    cases = _mac_cases() + [_two_at_one_seq()] + _set_cases()
    header = {"cases": len(cases), "ddl": DDL, "keys": sorted(KEYS)}
    return {"history_rows_mac.jsonl": jsonl([header, *cases])}
