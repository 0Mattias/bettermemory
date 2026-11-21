"""Fixture: the leaves of ``store_rows`` the native build shares between
the fold and the readers (``store_rows_leaves.jsonl``,
``store_rows_leaves_python.jsonl``, ``store_rows_columns.jsonl``).

The native build ports `_iso_opt`, `_upsert_sql`, `_sync_links`, `_membership_spec`,
`_membership_column`, `_MEMBERSHIP_BY_TABLE`, `_sort_rows`,
`_known_columns`, `_verification_row`, `_event_from_columns`,
`Store._event_from_raw` and the kind sets that decide which log rows are
telemetry (``native/``, `store::rows`) and is held to these files.

A value a row holds as SQLite gives it is tagged: ``["i", n]``, ``["f",
repr]``, ``["s", text]``, ``["b", hex]`` or null. A refusal is ``{"raise":
[class, str(exc)]}`` (the text only where the reference words it). An
event is its ``json.dumps``.

``sort`` cases: each row carries its index in ``n``; the answer is the
order of ``n`` after the sort. ``sort_sweep`` pins thousands of seeded
lists as a digest: the rule is a 64-bit LCG (`_Lcg`), the Rust test makes
the same lists, and the digest is the SHA-256 of one answer line per list.
Every list of that sweep is shorter than 64 rows, where CPython's sort is
one run sorted by binary insertion. ``sort_sweep_large`` does the same
over lists of 64 to 400 rows, which CPython sorts as several runs merged
in powersort's order with galloping merges; the port makes the same
comparisons there too.

``store_rows_columns.jsonl`` holds `_known_columns` as plain lists, under
the reference SQLite's version (the header names it): gated on SQLite.
``store_rows_leaves_python.jsonl`` holds payloads nested near the
recursion budget `json.loads` stops at, and sorts whose answer follows the
release's `list.sort` (a TypeError's pair of types, the order a NaN
leaves): both follow the Python release.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import types
from typing import Any

from bettermemory import store_rows
from bettermemory.log import CONTROL_KINDS
from bettermemory.store import Store
from bettermemory.store_schema import (
    MEMBERSHIP_EVENT_KINDS,
    MEMBERSHIP_MUTATION_KINDS,
    MUTATION_KINDS,
    SCHEMA,
    _MEMBERSHIP_BY_TABLE,
    _MEMBERSHIP_SPECS,
    _NOT_TELEMETRY,
)

from ._common import jsonl

SWEEP_LISTS = 4000
SWEEP_SEED = 20261121


def tag(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        raise AssertionError(v)
    if isinstance(v, int):
        return ["i", v]
    if isinstance(v, float):
        return ["f", repr(v)]
    if isinstance(v, str):
        return ["s", v]
    if isinstance(v, bytes):
        return ["b", v.hex()]
    raise AssertionError(v)


def _refusal(exc: BaseException, worded: bool = True) -> dict[str, Any]:
    return {"raise": [type(exc).__name__, str(exc) if worded else None]}


def _text(s: str) -> Any:
    """A name as the file spells it: plain when it is ASCII, else its code
    points (a lone surrogate has no JSON spelling serde reads)."""
    return s if s.isascii() else [ord(c) for c in s]


def _upsert_cases() -> list[dict[str, Any]]:
    cases = []
    for table, columns, key in [
        ("memories", store_rows._MEMORY_COLUMNS, "id"),
        ("tombstones", store_rows._TOMBSTONE_COLUMNS, "id"),
        ("t", ("a",), "a"),
        ("t", ("a", "k", "b"), "k"),
        ("t", ("a", "b"), "k"),
    ]:
        cases.append(
            {
                "op": "upsert",
                "table": table,
                "columns": list(columns),
                "key": key,
                "out": store_rows._upsert_sql(table, columns, key),
            }
        )
    return cases


class _Conn:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def execute(self, sql: str, values: tuple[Any, ...]) -> None:
        self.calls.append([sql, [tag(v) for v in values]])

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        for row in rows:
            self.execute(sql, row)


def _link_cases() -> list[dict[str, Any]]:
    out = []
    cases: list[list[tuple[str, str, str | None]]] = [
        [],
        [("supersedes", "B", None)],
        [("related", "B", None), ("related", "B", None), ("related", "B", "n")],
        [("a", "C", "x"), ("b", "C", "x"), ("a", "C", "x"), ("a", "D", None)],
        [("t", "B", ""), ("t", "B", None), ("t", "B", "")],
    ]
    for links in cases:
        memory = types.SimpleNamespace(
            id="A",
            links=[
                types.SimpleNamespace(
                    type=types.SimpleNamespace(value=t), target_id=g, note=n
                )
                for t, g, n in links
            ],
        )
        conn = _Conn()
        store_rows._sync_links(conn, memory)  # type: ignore[arg-type]
        out.append({"op": "links", "id": "A", "links": links, "out": conn.calls})
    return out


BAD_NAMES = [
    "",
    "memory",
    "Project",
    "machine_put",
    " machine",
    "x'y",
    'x"y',
    "é",
    "\udc80",
    "\x00",
    "machines",
]


def _spec_cases() -> list[dict[str, Any]]:
    cases = []
    kinds = list(_MEMBERSHIP_SPECS) + BAD_NAMES
    for kind in kinds:
        try:
            spec = store_rows._membership_spec(kind)
            out: Any = {"table": spec.table, "kind": spec.kind}
        except ValueError as exc:
            out = _refusal(exc)
        cases.append({"op": "spec", "k": _text(kind), "out": out})
    tables = list(_MEMBERSHIP_BY_TABLE) + ["memories", "machine", "", "Projects"]
    for table in tables:
        found = _MEMBERSHIP_BY_TABLE.get(table)
        cases.append(
            {"op": "by_table", "k": table, "out": None if found is None else found.kind}
        )
    for kind, spec in _MEMBERSHIP_SPECS.items():
        others = [c for s in _MEMBERSHIP_SPECS.values() for c in s.columns]
        names = list(spec.columns) + BAD_NAMES + ["rowid", spec.columns[0] + " "]
        names += [c for c in dict.fromkeys(others) if c not in spec.columns][:3]
        for name in names:
            try:
                store_rows._membership_column(spec, name)
                out = None
            except ValueError as exc:
                out = _refusal(exc)
            cases.append({"op": "column", "kind": kind, "k": _text(name), "out": out})
    return cases


def _sort(rows: list[dict[str, Any]], order_by: list[str]) -> Any:
    rows = [dict(r, n=i) for i, r in enumerate(rows)]
    try:
        store_rows._sort_rows(rows, order_by)
    except (TypeError, KeyError) as exc:
        return _refusal(exc)
    return [r["n"] for r in rows]


def _sort_case(rows: list[dict[str, Any]], order_by: list[str]) -> dict[str, Any]:
    return {
        "op": "sort",
        "rows": [{k: tag(v) for k, v in r.items()} for r in rows],
        "by": order_by,
        "out": _sort(rows, order_by),
    }


def _sort_cases() -> list[dict[str, Any]]:
    big = 2**63 - 1
    r: list[tuple[list[dict[str, Any]], list[str]]] = [
        ([], ["a"]),
        ([{"a": 1}], ["a"]),
        ([{"a": 1}], ["b"]),
        ([], ["b"]),
        ([{"a": 1}, {"b": 2}], ["a"]),
        ([{"a": 2}, {"a": None}, {"a": 1}, {"a": None}, {"a": 0}], ["a"]),
        ([{"a": 2}, {"a": None}, {"a": 1}, {"a": None}, {"a": 0}], ["-a"]),
        ([{"a": 2}, {"a": None}, {"a": 1}, {"a": None}, {"a": 0}], ["--a"]),
        ([{"a": 0}, {"a": None}, {"a": 0}, {"a": None}], ["a"]),
        ([{"a": 0}, {"a": None}, {"a": 0}, {"a": None}], ["-a"]),
        ([{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 1, "b": 3}], ["-a"]),
        ([{"a": 3}, {"a": 1}, {"a": 3}, {"a": 2}, {"a": 1}, {"a": 3}], ["-a"]),
        ([{"a": 3}, {"a": 1}, {"a": 3}, {"a": 2}, {"a": 1}, {"a": 3}], ["a"]),
        ([{"a": "b", "b": 1}, {"a": "a", "b": 2}, {"a": "b", "b": 0}], ["a", "-b"]),
        ([{"a": "b", "b": 1}, {"a": "a", "b": 2}, {"a": "b", "b": 0}], ["-b", "a"]),
        ([{"a": 1, "b": None}, {"a": 1, "b": "x"}, {"a": 0, "b": None}], ["a", "b"]),
        ([{"a": 1.5}, {"a": 1}, {"a": 2}, {"a": 1.0}, {"a": -0.0}, {"a": 0}], ["a"]),
        ([{"a": big}, {"a": float(big)}, {"a": big - 1}, {"a": -big - 1}], ["a"]),
        ([{"a": float("inf")}, {"a": float("-inf")}, {"a": 0}, {"a": None}], ["-a"]),
        ([{"a": 2**53 + 1}, {"a": float(2**53)}, {"a": 2**53}], ["a"]),
        ([{"a": float(2**63)}, {"a": big}], ["a"]),
        ([{"a": big}, {"a": float(2**63)}], ["-a"]),
        ([{"a": float(-(2**63))}, {"a": -big - 1}], ["a"]),
        ([{"a": "é"}, {"a": "z"}, {"a": "\U0001f600"}, {"a": "￿"}], ["a"]),
        ([{"a": b"\x01"}, {"a": b""}, {"a": b"\x00\xff"}, {"a": None}], ["a"]),
        ([{"a": 1}, {"a": "x"}], ["a"]),
        ([{"a": "x"}, {"a": 1}], ["a"]),
        ([{"a": 1}, {"a": "x"}], ["-a"]),
        ([{"a": "x"}, {"a": b"x"}], ["a"]),
        ([{"a": 1.5}, {"a": b"x"}], ["a"]),
        ([{"a": 1}, {"a": None}, {"a": "x"}, {"a": 2}], ["a"]),
        ([{"a": 1}, {"a": 2}, {"a": 3}, {"a": "x"}], ["a"]),
        ([{"a": 3}, {"a": 2}, {"a": 1}, {"a": "x"}], ["a"]),
        ([{"a": 1, "b": "x"}, {"a": 2, "b": 1}], ["b", "a"]),
        ([{"a": 1, "b": "x"}, {"a": 2, "b": 1}], ["a", "b"]),
        ([{"a": 1, "b": "x"}, {"a": 1, "b": 1}], ["a", "c"]),
        ([{"a": 1}, {"a": 1}, {"a": "x"}], ["-a"]),
        ([{"a": 2}, {"a": 2}, {"a": 1}, {"a": 1}, {"a": 3}], ["a"]),
        ([{"a": 1}, {"a": 1}, {"a": 2}, {"a": 1}], ["a"]),
        ([{"a": i % 5} for i in range(70)], ["-a"]),
        ([{"a": (i * 37) % 101, "b": i % 3} for i in range(200)], ["b", "-a"]),
        ([{"a": None if i % 4 == 0 else str(i % 9)} for i in range(64)], ["a"]),
    ]
    return [_sort_case(rows, by) for rows, by in r]


class _Lcg:
    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self, n: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.x >> 33) % n


_SWEEP_VALUES: list[Any] = [None, 0, 1, 2, -1, 1.0, 1.5, "", "a", "b", b"", b"a"]


def _sweep(palettes: int) -> dict[str, Any]:
    """The seeded sweep over the first ``palettes`` value pools: 0
    numbers and null, 1 text and null, 2 anything (mixed types, whose
    TypeError names the pair CPython's sort compares first, which follows
    the release)."""
    rng = _Lcg(SWEEP_SEED)
    digest = hashlib.sha256()
    for _ in range(SWEEP_LISTS):
        n = rng.next(64)
        palette = rng.next(palettes)
        pool = {
            0: _SWEEP_VALUES[:7],
            1: [None, "", "a", "b"],
            2: _SWEEP_VALUES,
        }[palette]
        rows = []
        for _ in range(n):
            rows.append(
                {"a": pool[rng.next(len(pool))], "b": pool[rng.next(len(pool))]}
            )
        by = [["a"], ["-a"], ["a", "-b"], ["-b", "a"]][rng.next(4)]
        answer = _sort(rows, by)
        digest.update((json.dumps(answer) + "\n").encode())
    return {
        "op": "sort_sweep",
        "seed": SWEEP_SEED,
        "palettes": palettes,
        "lists": SWEEP_LISTS,
        "values": [tag(v) for v in _SWEEP_VALUES],
        "sha256": digest.hexdigest(),
    }


def _verification_cases() -> list[dict[str, Any]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    target = '{"verified_path_targets": {"a": "f"}}'
    rows: list[tuple[str, dict[str, Any], Any]] = [
        ("defaults", {"memory_id": "M", "verified_at": "T"}, None),
        (
            "full",
            {
                "memory_id": "M",
                "machine_id": "X",
                "verified_at": "T",
                "verified_head": "H",
                "verified_paths_json": '["a", "b"]',
                "verified_commits_json": '["c"]',
                "verified_versions_json": '[{"v": 1.5}]',
                "verified_absent_paths_json": "[]",
            },
            None,
        ),
        (
            "bytes",
            {"memory_id": "M", "verified_at": "T", "verified_paths_json": b'["x"]'},
            None,
        ),
        (
            "object",
            {"memory_id": "M", "verified_at": "T", "verified_paths_json": '{"k": 1}'},
            None,
        ),
        (
            "bad json",
            {"memory_id": "M", "verified_at": "T", "verified_commits_json": "[1,"},
            None,
        ),
        (
            "number",
            {"memory_id": "M", "verified_at": "T", "verified_versions_json": 5},
            None,
        ),
        (
            "null list",
            {"memory_id": "M", "verified_at": "T", "verified_paths_json": "null"},
            None,
        ),
        (
            "int columns",
            {"memory_id": 7, "verified_at": 1.5, "machine_id": b"\x00"},
            None,
        ),
        (
            "targets",
            {"memory_id": "M", "verified_at": "T"},
            '{"memory": {"verified_path_targets": {"a": "f"},'
            ' "verified_absent_path_targets": {}}}',
        ),
        ("targets plain", {"memory_id": "M", "verified_at": "T"}, target),
        (
            "targets tombstone",
            {"memory_id": "M", "verified_at": "T"},
            '{"tombstone": ' + target + "}",
        ),
        ("targets null", {"memory_id": "M", "verified_at": "T"}, "null"),
        ("targets bad", {"memory_id": "M", "verified_at": "T"}, "{"),
        ("targets list", {"memory_id": "M", "verified_at": "T"}, "[1]"),
        (
            "targets memory list",
            {"memory_id": "M", "verified_at": "T"},
            '{"memory": [], "verified_path_targets": 1}',
        ),
        ("targets number", {"memory_id": "M", "verified_at": "T"}, 3),
        ("targets empty", {"memory_id": "M", "verified_at": "T"}, ""),
        (
            "targets deep",
            {"memory_id": "M", "verified_at": "T"},
            '{"verified_path_targets": {"a": {"x": [1]}, "b": null},'
            ' "verified_absent_path_targets": [1, {"b": 2}]}',
        ),
        (
            "targets memory null",
            {"memory_id": "M", "verified_at": "T"},
            '{"memory": null, "verified_path_targets": 1}',
        ),
    ]
    cases = []
    for i, (label, columns, attestation) in enumerate(rows):
        if columns["memory_id"] == "M":
            columns["memory_id"] = f"M{i}"
        names = ", ".join(columns)
        conn.execute(
            f"INSERT INTO verifications({names}) VALUES "
            f"({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )
        rowid = conn.execute("SELECT max(rowid) FROM verifications").fetchone()[0]
        select = "SELECT memory_id, machine_id, verified_at, verified_head, " + (
            "verified_paths_json, verified_commits_json, verified_versions_json, "
            "verified_absent_paths_json"
        )
        for with_att in [False, True] if attestation is not None else [False]:
            sql = select + (", ? AS attestation_payload" if with_att else "")
            sql += " FROM verifications WHERE rowid = ?"
            args = (attestation, rowid) if with_att else (rowid,)
            row = conn.execute(sql, args).fetchone()
            cases.append(_verification_case(label, row))
    row = conn.execute(
        "SELECT memory_id, machine_id, verified_at FROM verifications LIMIT 1"
    ).fetchone()
    cases.append(_verification_case("missing column", row))
    # A row that names an id: the payload's record must carry the same.
    for label, ident, record_id in [
        ("id same", "'M1'", '"M1"'),
        ("id other", "'M1'", '"M2"'),
        ("id absent", "'M1'", None),
        ("id null both", "NULL", "null"),
        ("id null record", "NULL", None),
        ("id int float", "7", "7.0"),
        ("id int true", "1", "true"),
        ("id int text", "7", '"7"'),
        ("id real", "1.5", "1.5"),
        ("id blob", "X'4d31'", '"M1"'),
    ]:
        inner = '"verified_path_targets": {"a": "f"}'
        if record_id is not None:
            inner = f'"id": {record_id}, ' + inner
        row = conn.execute(
            f"SELECT {ident} AS id, memory_id, machine_id, verified_at, "
            "verified_head, verified_paths_json, verified_commits_json, "
            "verified_versions_json, verified_absent_paths_json, "
            "? AS attestation_payload FROM verifications LIMIT 1",
            ("{" + inner + "}",),
        ).fetchone()
        cases.append(_verification_case(label, row))
    # Names that repeat or differ in case: ``row[name]`` is the first
    # column equal to it, ASCII case aside; ``name in row.keys()`` is exact.
    lists = (
        "NULL AS verified_head, NULL AS verified_paths_json, "
        "NULL AS verified_commits_json, NULL AS verified_versions_json, "
        "NULL AS verified_absent_paths_json"
    )
    quoted = '\'{"verified_path_targets": {"a": "f"}}\''
    for label, select in [
        ("repeated memory_id", "'m1' AS memory_id, 'm2' AS memory_id"),
        ("upper machine_id", "'m1' AS memory_id, 'X' AS MACHINE_ID"),
        ("upper payload", f"'m1' AS memory_id, {quoted} AS ATTESTATION_PAYLOAD"),
        (
            "upper payload first",
            f"'m1' AS memory_id, {quoted} AS ATTESTATION_PAYLOAD, "
            "'[1]' AS attestation_payload",
        ),
        (
            "upper id",
            f"'m1' AS memory_id, {quoted} AS attestation_payload, 'M9' AS ID",
        ),
        (
            "upper id first",
            "'m1' AS memory_id, 'M9' AS ID, 'M1' AS id, "
            '\'{"id": "M1", "verified_path_targets": {"a": "f"}}\' '
            "AS attestation_payload",
        ),
    ]:
        sql = f"SELECT {select}, 'x' AS machine_id, 't' AS verified_at, {lists}"
        cases.append(_verification_case(label, conn.execute(sql).fetchone()))
    conn.close()
    return cases


def _verification_case(label: str, row: sqlite3.Row) -> dict[str, Any]:
    columns = [[k, tag(v)] for k, v in zip(row.keys(), tuple(row))]
    try:
        got = store_rows._verification_row(row)
        out: Any = [
            [
                k,
                tag(v)
                if k in ("memory_id", "machine_id", "verified_at", "verified_head")
                else json.dumps(v),
            ]
            for k, v in got.items()
        ]
    except (ValueError, TypeError, IndexError) as exc:
        out = _refusal(exc, worded=False)
    return {"op": "verification", "label": label, "row": columns, "out": out}


TELEMETRY = ["recall", "search", "", "memory_put_x", "Memory_put", "put", "xé"]


def _event(ts: str, session: str | None, kind: str, payload: Any) -> Any:
    try:
        event = store_rows._event_from_columns(ts, session, kind, payload)
    except (TypeError, RecursionError) as exc:
        return _refusal(exc, worded=False)
    return None if event is None else json.dumps(event)


def _event_shape(payload: str) -> Any:
    """Whether a deep payload parsed: the event's keys, None, or the
    refusal's class."""
    try:
        event = store_rows._event_from_columns("T", None, "recall", payload)
    except RecursionError as exc:
        return _refusal(exc, worded=False)
    return None if event is None else list(event)


def _event_case(
    ts: str, session: str | None, kind: str, payload: Any
) -> dict[str, Any]:
    return {
        "op": "event",
        "ts": ts,
        "session": session,
        "kind": _text(kind),
        "payload": tag(payload),
        "out": _event(ts, session, kind, payload),
    }


def _event_cases() -> list[dict[str, Any]]:
    cases = []
    body = '{"q": "x", "n": 1}'
    for kind in sorted(_NOT_TELEMETRY):
        cases.append(_event_case("T", "S", kind, body))
    payloads: list[Any] = [
        body,
        '{"ts": "other", "session": "s2", "kind": "k2", "q": 1}',
        '{"z": 1, "a": [1, 2.5, null, true], "m": {"x": "\\u00e9"}}',
        '{"a": 1, "a": 2, "b": 3}',
        '{"s": "\\ud800"}',
        "{}",
        "[]",
        "[1]",
        "1",
        '"text"',
        "null",
        "true",
        "",
        "{",
        "{} x",
        "NaN",
        '{"x": NaN, "y": -Infinity, "z": 1e400}',
        '{"big": ' + "9" * 30 + "}",
        '{"big": ' + "9" * 4301 + "}",
        b'{"b": 1}',
        b'\xef\xbb\xbf{"b": 2}',
        b"\xff\xfe{\x00}\x00",
        b"\xff\xff",
        b'{"b": "\xc3"}',
        5,
        2.5,
        None,
    ]
    for kind in TELEMETRY:
        for payload in payloads if kind == "recall" else payloads[:2]:
            for session in ["S", None] if kind == "recall" else ["S"]:
                cases.append(_event_case("2004-01-02T03:04:05", session, kind, payload))
    cases.append(_event_case("", "", "\udc80", body))
    return cases


#: Rows whose names repeat or differ in case: ``sqlite3.Row`` answers the
#: first column whose name equals, ASCII case aside.
RAW_NAMED: list[list[tuple[str, Any]]] = [
    [("TS", "T"), ("session", "S"), ("kind", "recall"), ("payload", "{}")],
    [("ts", "T"), ("Session", "S"), ("KIND", "recall"), ("Payload", '{"a": 1}')],
    [("ts", "A"), ("ts", "B"), ("session", "S"), ("kind", "recall"), ("payload", "{}")],
    [
        ("ts", "T"),
        ("session", "S"),
        ("kind", "memory_put"),
        ("kind", "recall"),
        ("payload", '{"a": 1}'),
    ],
    [("ts", "T"), ("session", "S"), ("kind", "recall"), ("payload", "{")],
]


def _raw_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"ts": "T", "session": "S", "kind": "recall", "payload": '{"a": 1}'},
        {"ts": "T", "session": None, "kind": "recall", "payload": '{"a": 1}'},
        {"ts": 5, "session": 6, "kind": "recall", "payload": '{"a": 1}'},
        {"ts": 1.5, "session": b"s'", "kind": "recall", "payload": b'{"a": 1}'},
        {"ts": "T", "session": "S", "kind": b"recall", "payload": "{}"},
        {"ts": None, "session": "S", "kind": "memory_put", "payload": "{"},
        {"ts": "T", "session": "S", "kind": None, "payload": "{}"},
        {"ts": "T", "session": "S", "kind": "recall", "payload": None},
        {"ts": "T", "session": "S", "kind": "rekey"},
        {"ts": "T", "kind": "recall", "payload": "{}"},
        {"session": "S", "kind": "recall", "payload": "{}"},
    ]
    cases = []
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    named = [list(row.items()) for row in rows] + RAW_NAMED
    for pairs in named:
        names = [n for n, _ in pairs]
        raw = conn.execute(
            "SELECT " + ", ".join(f"? AS {n}" for n in names),
            tuple(v for _, v in pairs),
        ).fetchone()
        try:
            event = Store._event_from_raw(None, raw)  # type: ignore[arg-type]
            out: Any = None if event is None else json.dumps(event)
        except (TypeError, IndexError) as exc:
            out = _refusal(exc, worded=False)
        cases.append({"op": "raw", "row": [[k, tag(v)] for k, v in pairs], "out": out})
    conn.close()
    return cases


def _iso_cases() -> list[dict[str, Any]]:
    texts: list[str | None] = [
        None,
        "2024-01-02T03:04:05",
        "2024-01-02T03:04:05.000001",
        "2024-01-02T03:04:05.120000+00:00",
        "2024-01-02T03:04:05-05:30",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59.999999-00:00:01",
    ]
    return [
        {
            "op": "iso_opt",
            "in": t,
            "out": store_rows._iso_opt(
                None if t is None else datetime.datetime.fromisoformat(t)
            ),
        }
        for t in texts
    ]


def _kind_cases() -> list[dict[str, Any]]:
    return [
        {"op": "kinds", "name": name, "kinds": sorted(kinds)}
        for name, kinds in [
            ("MUTATION_KINDS", MUTATION_KINDS),
            ("MEMBERSHIP_MUTATION_KINDS", MEMBERSHIP_MUTATION_KINDS),
            ("CONTROL_KINDS", CONTROL_KINDS),
            ("MEMBERSHIP_EVENT_KINDS", MEMBERSHIP_EVENT_KINDS),
            ("_NOT_TELEMETRY", _NOT_TELEMETRY),
        ]
    ]


def _file(cases: list[dict[str, Any]], **header: Any) -> bytes:
    return jsonl([{"cases": len(cases), **header}, *cases])


def generate() -> dict[str, bytes]:
    cases = (
        _upsert_cases()
        + _link_cases()
        + _spec_cases()
        + _sort_cases()
        + [_sweep(2)]
        + _verification_cases()
        + _event_cases()
        + _raw_cases()
        + _kind_cases()
        + _iso_cases()
    )
    return {"store_rows_leaves.jsonl": _file(cases)}


def _nest(open_: str, n: int, inner: str, close: str) -> str:
    return '{"x": ' + open_ * n + inner + close * n + "}"


def _run_split_cases() -> list[dict[str, Any]]:
    """Lists of 32 to 63 rows sorted as one run: a short natural run, then
    binary insertion of the rest, so the str at the middle is compared
    first as the item inserted. Split into two runs it would open the
    second run and be compared first as the earlier item."""
    cases = []
    for n in range(32, 64):
        values: list[Any] = [1, 0, 5, 2] + [(i * 7) % 13 for i in range(4, n)]
        values[(n >> 1) + (n & 1)] = "x"
        cases.append(_sort_case([{"a": v} for v in values], ["a"]))
    return cases


LARGE_LISTS = 600


def _sweep_large(name: str, pool: list[Any]) -> dict[str, Any]:
    """Seeded lists of 64 to 400 rows over ``pool``, by two names: the
    runs are merged as the release's ``list.sort`` merges them, which
    decides the order a NaN leaves and the pair a TypeError names."""
    rng = _Lcg(SWEEP_SEED)
    digest = hashlib.sha256()
    for _ in range(LARGE_LISTS):
        n = 64 + rng.next(337)
        rows = [
            {"a": pool[rng.next(len(pool))], "b": pool[rng.next(len(pool))]}
            for _ in range(n)
        ]
        by = [["a", "b"], ["-a", "b"], ["a", "-b"], ["-a", "-b"]][rng.next(4)]
        digest.update((json.dumps(_sort(rows, by)) + "\n").encode())
    return {
        "op": "sort_sweep_large",
        "name": name,
        "seed": SWEEP_SEED,
        "lists": LARGE_LISTS,
        "values": [tag(v) for v in pool],
        "sha256": digest.hexdigest(),
    }


def _verification_nest_cases() -> list[dict[str, Any]]:
    """An attestation payload nested past ``json.loads``' recursion budget
    is no targets (``_path_targets`` swallows RecursionError)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cases = []
    for n in [100, 100000]:
        nest = ["[", n, "", "]"]
        row = conn.execute(
            "SELECT 'm1' AS memory_id, 'x' AS machine_id, 't' AS verified_at, "
            "NULL AS verified_head, NULL AS verified_paths_json, "
            "NULL AS verified_commits_json, NULL AS verified_versions_json, "
            "NULL AS verified_absent_paths_json, ? AS attestation_payload",
            (_nest("[", n, "", "]"),),
        ).fetchone()
        case = _verification_case("nest", row)
        case["row"] = case["row"][:-1]
        cases.append({**case, "op": "verification_nest", "nest": nest})
    conn.close()
    return cases


def generate_python() -> dict[str, bytes]:
    cases = []
    for n in [100, 990, 1000, 4990, 20000, 100000]:
        for open_, inner, close in [("[", "", "]"), ('{"a":', "1", "}")]:
            cases.append(
                {
                    "op": "event_nest",
                    "nest": [open_, n, inner, close],
                    "out": _event_shape(_nest(open_, n, inner, close)),
                }
            )
    cases.append(_sweep(3))
    cases += _run_split_cases()
    cases.append(_sweep_large("nan", [1, 2, None, float("nan")]))
    cases.append(_sweep_large("mixed", _SWEEP_VALUES))
    cases += _verification_nest_cases()
    return {"store_rows_leaves_python.jsonl": _file(cases)}


def generate_columns() -> dict[str, bytes]:
    known = store_rows._known_columns()
    cases = [{"table": t, "columns": list(c)} for t, c in known.items()]
    return {"store_rows_columns.jsonl": _file(cases, sqlite=sqlite3.sqlite_version)}
