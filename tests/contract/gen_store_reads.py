"""Fixture: the connection leaves of ``store.py`` and its admitted reads
(``store_reads.jsonl``, ``store_reads_python.jsonl``).

The native build ports `note_residue`, `error_code_name`, `retryable_code`,
`Store.set_busy_timeout`, `Store.document_frequencies` outside the memo,
`Store._term_masks`, `Store.scope_counts`, `Store.scopes_for`,
`Store.category_rows` and `Store.brief_rows` (``native/``, `store::reads`)
and is held to these files.

A ``store`` case is the SQL that builds a small store in memory (bare
``memories`` and ``homes`` tables without column types, so a hostile row
keeps the type it was written with, and the ``memories_fts`` table); each
statement's parameters are tagged as `gen_store_rows` tags them. A read
case names the store and an admission predicate: ``all``, ``none``,
``scope:<s>`` (the scopes hold the text ``s``), ``project:<p>`` (the home
project is the text ``p``), ``nohome`` (no home), ``actor:<c>`` (an actor
whose client is ``c``, for the document frequencies). The reads are run
on unbound `Store` methods over an object holding the connection. A
refusal is ``{"raise": class}``.

``store_reads_python.jsonl`` holds the name CPython gives every SQLite
result code (`sqlite_errorname`), which is the release's table, and the
busy timeouts past 32 bits of milliseconds, which SQLite 3.50.4 reads as
0 and a later SQLite ignores.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import types
from typing import Any, Callable

from bettermemory import store
from bettermemory.store import Store

from ._common import jsonl

#: The rows of the ``many`` store, which the file names by rule (row `i`
#: is ``m{i:04d}``, scopes ``["s{i % 3}"]``, body ``word`` for odd `i`,
#: else ``other``) rather than spelling its SQL.
MANY = 1200
#: An answer whose compact JSON is longer than this is pinned as its
#: length and SHA-256.
LONG = 1500


def _out(value: Any) -> Any:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(text) <= LONG:
        return value
    return {"digest": [len(text), hashlib.sha256(text.encode()).hexdigest()]}


_PRIMARY = {
    "OK", "ERROR", "INTERNAL", "PERM", "ABORT", "BUSY", "LOCKED", "NOMEM",
    "READONLY", "INTERRUPT", "IOERR", "CORRUPT", "NOTFOUND", "FULL",
    "CANTOPEN", "PROTOCOL", "EMPTY", "SCHEMA", "TOOBIG", "CONSTRAINT",
    "MISMATCH", "MISUSE", "NOLFS", "AUTH", "FORMAT", "RANGE", "NOTADB",
    "NOTICE", "WARNING", "ROW", "DONE",
}  # fmt: skip

_SCHEMA = [
    "CREATE TABLE memories (id, scopes_json, category, provenance, updated, "
    "actor_client, actor_model)",
    "CREATE TABLE homes (record_id, project)",
    "CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts)",
]


def tag(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, int):
        return ["i", v]
    if isinstance(v, float):
        return ["f", repr(v)]
    if isinstance(v, str):
        return ["s", v]
    return ["b", v.hex()]


def _mem(
    rowid: int,
    id_: Any,
    scopes: Any,
    body: str,
    *,
    category: Any = "fact",
    provenance: Any = "stated",
    updated: Any = "2026-01-01",
    client: Any = None,
    model: Any = None,
    home: Any = None,
) -> list[list[Any]]:
    out = [
        [
            "INSERT INTO memories (rowid, id, scopes_json, category, provenance, "
            "updated, actor_client, actor_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [rowid, id_, scopes, category, provenance, updated, client, model],
        ],
        [
            "INSERT INTO memories_fts (rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            [rowid, body, scopes if isinstance(scopes, str) else ""],
        ],
    ]
    if home is not None:
        out.append(
            ["INSERT INTO homes (record_id, project) VALUES (?, ?)", [id_, home]]
        )
    return out


def _stores() -> dict[str, list[list[Any]]]:
    plain: list[list[Any]] = []
    plain += _mem(1, "a1", '["alpha", "beta"]', "the quick fox", home="p1", client="c1")
    plain += _mem(2, "a2", '["beta"]', "lazy dog jumps", home="p2", model="m1")
    plain += _mem(3, "a3", "[]", "quick dog", category="howto", home="p1")
    plain += _mem(
        4, "a4", '["gamma"]', "fox fox", provenance="inferred", updated="2026-03-01"
    )
    plain += _mem(
        5, "a5", '["alpha"]', 'say "quoted" words', updated="2026-03-01", client="c2"
    )
    plain += _mem(6, "a6", "{}", "quick object", category=None)
    plain += _mem(7, "a7", None, "quick none")
    plain += _mem(8, "a8", '"alpha"', "quick text scopes", provenance="inferred")
    odd: list[list[Any]] = []
    odd += _mem(1, "b1", '[1, true, 1.0, null, "1"]', "one", home="p1")
    odd += _mem(2, "b2", "[NaN, NaN, Infinity, 2.5, 3.0, 3]", "two")
    odd += _mem(3, 77, '["x"]', "integer id", home="p1")
    odd += _mem(4, 2.5, '["x", "x"]', "real id")
    odd += _mem(5, "b5", b'["bytes"]', "blob scopes", home=b"p1")
    odd += _mem(6, "b6", '["y"]', "integer home", home=12)
    odd += _mem(7, "b7", '["\\u00e9t\\u00e9", "z"]', "accent", updated=None)
    bad_list: list[list[Any]] = []
    bad_list += _mem(1, "c1", '["ok", ["nested"]]', "nested list")
    bad_dict: list[list[Any]] = []
    bad_dict += _mem(1, "d1", '[{"k": 1}]', "nested dict")
    bad_json: list[list[Any]] = []
    bad_json += _mem(1, "e1", '["fine"]', "fine")
    bad_json += _mem(2, "e2", "[not json", "broken")
    bad_actor: list[list[Any]] = []
    bad_actor += _mem(1, "f1", '["s"]', "number client", client=5)
    blob_actor: list[list[Any]] = []
    blob_actor += _mem(1, "g1", '["s"]', "blob client", client=b"cb")
    # sqlite3 decodes every column of a row when it fetches the row, so a
    # text that is not UTF-8 raises OperationalError in any selected column,
    # on a row the read skips or refuses as well.
    undecodable = ["UPDATE memories SET id = CAST(X'ff' AS TEXT) WHERE rowid = 2", []]
    bad_text_id: list[list[Any]] = []
    bad_text_id += _mem(1, "r1", '["alpha"]', "fox")
    bad_text_id += _mem(2, "r2", "{}", "fox")
    bad_text_id.append(undecodable)
    bad_text_home: list[list[Any]] = []
    bad_text_home += _mem(1, "r1", '["alpha"]', "fox")
    bad_text_home.append(
        [
            "INSERT INTO homes (record_id, project) VALUES ('r1', CAST(X'ff' AS TEXT))",
            [],
        ]
    )
    bad_text_home.append(["UPDATE memories SET scopes_json = '{}'", []])
    bad_text_scopes: list[list[Any]] = []
    bad_text_scopes += _mem(1, "r1", '["alpha"]', "fox", client=5)
    bad_text_scopes.append(
        ["UPDATE memories SET scopes_json = CAST(X'ff' AS TEXT)", []]
    )
    bad_text_skipped: list[list[Any]] = []
    bad_text_skipped += _mem(1, "r1", "{}", "word")
    bad_text_skipped.append(["UPDATE memories SET id = CAST(X'ff' AS TEXT)", []])
    duplicate_id: list[list[Any]] = []
    duplicate_id += _mem(1, "a", '["x"]', "first")
    duplicate_id += _mem(2, "a", '["y"]', "second")
    many: list[list[Any]] = []
    for i in range(1, MANY + 1):
        many += _mem(i, f"m{i:04d}", f'["s{i % 3}"]', "word" if i % 2 else "other")
    return {
        "plain": plain,
        "odd": odd,
        "bad_list": bad_list,
        "bad_dict": bad_dict,
        "bad_json": bad_json,
        "bad_actor": bad_actor,
        "blob_actor": blob_actor,
        "bad_text_id": bad_text_id,
        "bad_text_home": bad_text_home,
        "bad_text_scopes": bad_text_scopes,
        "bad_text_skipped": bad_text_skipped,
        "duplicate_id": duplicate_id,
        "empty": [],
        "many": many,
    }


def _connect(statements: list[list[Any]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for sql in _SCHEMA:
        conn.execute(sql)
    for sql, params in statements:
        conn.execute(sql, params)
    return conn


def _fake(statements: list[list[Any]]) -> Any:
    """An object holding a connection to the store, as the unbound reads
    read it."""
    return types.SimpleNamespace(_conn=_connect(statements), _tx_depth=0)


def _admit(name: str) -> Callable[..., bool]:
    kind, _, arg = name.partition(":")

    def admit(scopes: list[Any], home: Any, actor: Any = None) -> bool:
        if kind == "all":
            return True
        if kind == "none":
            return False
        if kind == "scope":
            return arg in scopes
        if kind == "project":
            return home == arg
        if kind == "nohome":
            return home is None
        if kind == "actor":
            return actor is not None and actor.client == arg
        raise AssertionError(name)

    return admit


def _run(fn: Callable[[], Any], shape: Callable[[Any], Any]) -> Any:
    try:
        return _out(shape(fn()))
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raise": type(exc).__name__}


def _key(v: Any) -> Any:
    return repr(v)


def _read_cases(stores: dict[str, list[list[Any]]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for name, statements in stores.items():
        if name == "many":
            cases.append({"op": "store", "name": name, "many": MANY})
            continue
        sql = [[s, [tag(v) for v in p]] for s, p in statements]
        cases.append({"op": "store", "name": name, "sql": sql})
    admits = ["all", "none", "scope:alpha", "scope:x", "project:p1", "nohome"]
    term_sets = [
        [],
        [""],
        ["quick"],
        ["fox", "quick", "fox", ""],
        ["alpha", "beta", "missing"],
        ['"quoted"', "say"],
        ["word", "s1"],
        ["x", "one", "1"],
    ]
    for name in stores:
        for admit in [*admits, "actor:c1", "actor:c2"]:
            for terms in term_sets:
                fake = _fake(stores[name])
                cases.append(
                    {
                        "op": "doc_freqs",
                        "store": name,
                        "admit": admit,
                        "terms": terms,
                        "out": _run(
                            lambda: Store.document_frequencies(
                                fake, terms, admit=_admit(admit)
                            ),
                            lambda r: None if r is None else [r[0], r[1], r[2]],
                        ),
                    }
                )
        for admit in admits:
            fake = _fake(stores[name])
            cases.append(
                {
                    "op": "scope_counts",
                    "store": name,
                    "admit": admit,
                    "out": _run(
                        lambda: Store.scope_counts(fake, admit=_admit(admit)),
                        lambda r: [r[0], [[_key(k), n] for k, n in r[1].items()]],
                    ),
                }
            )
            for category in ["fact", "howto", "none"]:
                cases.append(
                    {
                        "op": "category_rows",
                        "store": name,
                        "admit": admit,
                        "category": category,
                        "out": _run(
                            lambda: Store.category_rows(
                                fake, category=category, admit=_admit(admit)
                            ),
                            list,
                        ),
                    }
                )
            for labels, exclude in [
                ([], None),
                (["stated"], None),
                (["inferred", "stated", "stated"], None),
                (["stated", "inferred"], "fact"),
                (["inferred"], "howto"),
            ]:
                cases.append(
                    {
                        "op": "brief_rows",
                        "store": name,
                        "admit": admit,
                        "provenance": labels,
                        "exclude": exclude,
                        "out": _run(
                            lambda: Store.brief_rows(
                                fake,
                                admit=_admit(admit),
                                provenance=labels,
                                exclude_category=exclude,
                            ),
                            list,
                        ),
                    }
                )
        fake = _fake(stores[name])
        id_sets = [
            [],
            ["a1", "a3", "a1", "zz", "a7", "a6", "a8"],
            ["b1", "b2", "77", "2.5", "b5", "b7"],
            ["c1", "d1", "e1", "e2"],
            ["a", "r1", "r2"],
            [f"m{i:04d}" for i in range(MANY, 0, -1)],
        ]
        for ids in id_sets:
            cases.append(
                {
                    "op": "scopes_for",
                    "store": name,
                    "ids": ids if len(ids) < 100 else {"many_desc": len(ids)},
                    "out": _run(
                        lambda: Store.scopes_for(fake, ids),
                        lambda r: [[k, v] for k, v in r.items()],
                    ),
                }
            )
        for term in ["quick", "fox", "alpha", 'quo"ted', "word", "nothing"]:
            fake = _fake(stores[name])
            rids = [
                r[0]
                for r in fake._conn.execute(
                    "SELECT rowid FROM memories ORDER BY rowid DESC"
                )
            ]
            kept = [rid for rid in rids if rid % 4]
            inputs = types.SimpleNamespace(index={rid: i for i, rid in enumerate(kept)})
            width = len(inputs.index)

            def masks(term: str = term, inputs: Any = inputs, fake: Any = fake) -> Any:
                return Store._term_masks(fake, term, inputs)

            def bits(m: int, width: int = width) -> list[int]:
                return [p for p in range(width) if m >> p & 1]

            cases.append(
                {
                    "op": "term_masks",
                    "store": name,
                    "term": term,
                    "out": _run(masks, lambda r: [bits(r[0]), bits(r[1])]),
                }
            )
    return cases


def _code_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for class_ in ["OperationalError", "IntegrityError", "DatabaseError", "ValueError"]:
        for code in [
            None,
            "5",
            0,
            5,
            6,
            13,
            1,
            9,
            10,
            12,
            261,
            262,
            517,
            773,
            13 + 256,
            1 + 1024,
            -251,
            -1,
            2**40 + 5,
            0xFF,
        ]:
            exc: Any = getattr(sqlite3, class_, ValueError)("x")
            if code is not None:
                exc.sqlite_errorcode = code
            cases.append(
                {
                    "op": "retryable",
                    "class": class_,
                    "code": code,
                    "out": store.retryable_code(exc),
                }
            )
    cases.append({"op": "names", "busy": store.STORE_BUSY, "full": store.STORE_FULL})
    live = [
        ("syntax", ["SELEC 1"]),
        (
            "unique",
            [
                "CREATE TABLE t (a UNIQUE)",
                "INSERT INTO t VALUES (1)",
                "INSERT INTO t VALUES (1)",
            ],
        ),
        ("notnull", ["CREATE TABLE t (a NOT NULL)", "INSERT INTO t VALUES (NULL)"]),
        ("check", ["CREATE TABLE t (a CHECK (a > 0))", "INSERT INTO t VALUES (0)"]),
        (
            "primarykey",
            [
                "CREATE TABLE t (a PRIMARY KEY)",
                "INSERT INTO t VALUES (1)",
                "INSERT INTO t VALUES (1)",
            ],
        ),
        (
            "mismatch",
            ["CREATE TABLE t (a INTEGER PRIMARY KEY)", "INSERT INTO t VALUES ('x')"],
        ),
        ("readonly", ["PRAGMA query_only = 1", "CREATE TABLE t (a)"]),
        ("toobig", ["SELECT zeroblob(2000000000)"]),
        (
            "fts_syntax",
            [
                "CREATE VIRTUAL TABLE f USING fts5(a)",
                "SELECT * FROM f WHERE f MATCH '\"'",
            ],
        ),
        ("no_table", ["SELECT * FROM nothing"]),
    ]
    for label, statements in live:
        conn = sqlite3.connect(":memory:")
        out: Any = None
        try:
            for sql in statements:
                conn.execute(sql)
        except sqlite3.Error as exc:
            out = [
                type(exc).__name__,
                exc.sqlite_errorcode,
                store.error_code_name(exc),
                store.retryable_code(exc),
            ]
        conn.close()
        cases.append({"op": "live", "label": label, "sql": statements, "out": out})
    plain: list[BaseException] = [
        ValueError("x"),
        OSError("x"),
        sqlite3.OperationalError("x"),
    ]
    for raised in plain:
        cases.append(
            {
                "op": "name_none",
                "class": type(raised).__name__,
                "out": store.error_code_name(raised),
            }
        )
    for then in ["it was rolled back", ""]:
        cases.append(
            {
                "op": "residue",
                "connection": "the store",
                "then": then,
                "out": f"{'the store'} was left inside a transaction; {then}",
            }
        )
    return cases


#: Timeouts whose milliseconds do not fit 32 bits: SQLite 3.50.4 sets
#: the timeout to 0 for them, a later SQLite leaves it as it was, so they
#: are pinned in the file gated on the release and on SQLite.
_WIDE_TIMEOUTS = [2147483.648, 9.5e9, 1e300, 1e305]


def _timeout_cases(values: list[float] | None = None) -> list[dict[str, Any]]:
    cases = []
    for seconds in values or [
        0.0, -0.0, 0.0005, 0.001, 1.5, 5.0, 2147483.647, 1e306, 5e-324,
        -1.0, -5e-324, math.nan, math.inf, -math.inf,
    ]:  # fmt: skip
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA busy_timeout = 4321")
        fake: Any = types.SimpleNamespace(_conn=conn, _busy_timeout=None)
        try:
            Store.set_busy_timeout(fake, seconds)
            out: Any = {"ok": conn.execute("PRAGMA busy_timeout").fetchone()[0]}
        except Exception as exc:  # noqa: BLE001 - the class and text are the answer
            out = {"raise": [type(exc).__name__, str(exc)]}
        conn.close()
        cases.append({"op": "busy_timeout", "seconds": repr(seconds), "out": out})
    return cases


def _file(cases: list[dict[str, Any]], **header: Any) -> bytes:
    return jsonl([{"cases": len(cases), **header}, *cases])


def generate() -> dict[str, bytes]:
    cases = _code_cases() + _timeout_cases() + _read_cases(_stores())
    return {"store_reads.jsonl": _file(cases)}


def generate_python() -> dict[str, bytes]:
    names: dict[int, str] = {}
    for attr in sorted(dir(sqlite3)):
        if not attr.startswith("SQLITE_"):
            continue
        if attr[len("SQLITE_") :].split("_")[0] not in _PRIMARY:
            continue
        value: int = getattr(sqlite3, attr)
        assert value not in names, attr
        names[value] = attr
    # The names come from the module's SQLITE_* constants, which CPython's
    # _sqlite builds from the same error_codes table it reads when it
    # names a raised error. The errors below are raised for real, and each
    # name sqlite3 attaches must be the constant's, or the generator stops.
    for statements in [
        ["SELEC 1"],
        [
            "CREATE TABLE t (a UNIQUE)",
            "INSERT INTO t VALUES (1)",
            "INSERT INTO t VALUES (1)",
        ],
        ["CREATE TABLE t (a NOT NULL)", "INSERT INTO t VALUES (NULL)"],
        ["CREATE TABLE t (a INTEGER PRIMARY KEY)", "INSERT INTO t VALUES ('x')"],
        ["PRAGMA query_only = 1", "CREATE TABLE t (a)"],
        ["SELECT zeroblob(2000000000)"],
    ]:
        conn = sqlite3.connect(":memory:")
        try:
            for sql in statements:
                conn.execute(sql)
        except sqlite3.Error as raised:
            assert raised.sqlite_errorname == names[raised.sqlite_errorcode], statements
        else:
            raise AssertionError(statements)
        finally:
            conn.close()
    cases: list[dict[str, Any]] = []
    for code in sorted(names) + [29, 99, 102, 8714, 70000, -1]:
        exc: Any = sqlite3.OperationalError("x")
        exc.sqlite_errorcode = code
        exc.sqlite_errorname = names.get(code)
        cases.append({"op": "name", "code": code, "out": store.error_code_name(exc)})
    cases += _timeout_cases(_WIDE_TIMEOUTS)
    return {"store_reads_python.jsonl": _file(cases, sqlite=sqlite3.sqlite_version)}
