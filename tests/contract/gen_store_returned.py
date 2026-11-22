"""Fixture: the backfill half of ``store_returned.py``
(``store_returned.jsonl``, ``store_returned_posix.jsonl``).

The native build ports ``missing_links``, ``returned_rows``, ``backfill``
and ``ensure_backfilled`` (``native/``, `store::returned`) and is held to
these files.

``store_returned.jsonl`` pins ``missing_links`` and ``returned_rows``
over a bare in-memory connection whose tables hold exactly the cells a
row names (each cell typed: ``text``, ``int``, ``real``, ``blob`` or
null), so odd column types and odd link shapes are covered without a
store. A raise is recorded by its class only: the wording of an
unhashable value's TypeError changed in CPython 3.14. Nothing in it
follows the platform or the release, so it is not gated.

``store_returned_posix.jsonl`` runs whole stores as
``store_records_e.jsonl`` does (`gen_store_records_b.run_case`), with
these steps besides the record writers: ``exec`` (statements run
without parameters and committed, which is how an earlier build's
history is made: a record deleted under the log, the index rows of a
restore dropped, the marks and their rows taken away, the table
dropped), ``backfill``, ``ensure`` (``has_key`` given, the log lines
caught), ``missing`` and ``marks``. Its gates are
``store_records_e.jsonl``'s: the state digest is POSIX, the index text
the tokenizer's, and refusals pydantic's.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import store_returned
from bettermemory.store import Store
from bettermemory.store_schema import RETURNED_BACKFILL_KEY

from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS
from .gen_store_records_b import put, run_case, tomb
from .gen_store_records_d import link, linked, restore
from .gen_store_records_e import _run_all
from .gen_store_write import ulid

TABLES = [
    "CREATE TABLE memories (id, links_json)",
    "CREATE TABLE memory_links (source_id, type, target_id, note)",
    "CREATE TABLE returned (record_id, via, returned)",
]


def _cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if isinstance(value, int):
        return {"int": value}
    if isinstance(value, float):
        return {"real": value}
    return {"text": value}


def u(n: int) -> str:
    return ulid(n)


def links(*items: Any) -> str:
    return json.dumps(list(items))


def sup(n: int, **extra: Any) -> dict[str, Any]:
    return {"type": "supersedes", "target_id": u(n), **extra}


def _pure_cases() -> list[dict[str, Any]]:
    one = {"memories": [[u(1), "[]"]]}
    cases: list[dict[str, Any]] = [
        {"name": "empty", "memories": [], "links": []},
        {
            "name": "one_missing",
            "memories": [[u(1), "[]"], [u(2), links(sup(1))]],
            "links": [],
        },
        {
            "name": "indexed",
            "memories": [[u(1), "[]"], [u(2), links(sup(1))]],
            "links": [[u(2), "supersedes", u(1), None]],
        },
        {
            "name": "note_differs",
            "memories": [[u(1), "[]"], [u(2), links(sup(1, note="a"))]],
            "links": [[u(2), "supersedes", u(1), "b"]],
        },
        {
            "name": "note_int_against_real",
            "memories": [[u(1), "[]"], [u(2), links(sup(1, note=1))]],
            "links": [[u(2), "supersedes", u(1), 1.0]],
        },
        {
            "name": "note_true_against_int",
            "memories": [[u(1), "[]"], [u(2), links(sup(1, note=True))]],
            "links": [[u(2), "supersedes", u(1), 1]],
        },
        {
            "name": "note_real_against_int",
            "memories": [[u(1), "[]"], [u(2), links(sup(1, note=2.0))]],
            "links": [[u(2), "supersedes", u(1), 2]],
        },
        {
            "name": "note_text_against_blob",
            "memories": [[u(1), "[]"], [u(2), links(sup(1, note="a"))]],
            "links": [[u(2), "supersedes", u(1), b"a"]],
        },
        {
            "name": "type_differs",
            "memories": [[u(1), "[]"], [u(2), links(sup(1))]],
            "links": [[u(2), "extends", u(1), None]],
        },
        {
            "name": "type_int",
            "memories": [
                [u(1), "[]"],
                [u(2), links({"type": 5, "target_id": u(1)})],
            ],
            "links": [[u(2), 5, u(1), None]],
        },
        {
            "name": "type_absent",
            "memories": [[u(1), "[]"], [u(2), links({"target_id": u(1)})]],
            "links": [],
        },
        {
            "name": "target_inactive",
            "memories": [[u(2), links(sup(1))]],
            "links": [],
        },
        {
            "name": "self_link",
            "memories": [[u(1), links(sup(1))]],
            "links": [],
        },
        {
            "name": "least_of_several",
            "memories": [
                [u(1), "[]"],
                [u(3), "[]"],
                [u(4), links(sup(3), sup(1), {"type": "extends", "target_id": u(1)})],
            ],
            "links": [[u(4), "supersedes", u(1), None]],
        },
        {
            "name": "id_order",
            "memories": [
                [u(1), "[]"],
                [u(5), links(sup(1))],
                [u(3), links(sup(1))],
            ],
            "links": [],
        },
        {
            "name": "id_int_source",
            "memories": [["7", "[]"], [8, json.dumps([{"target_id": "7"}])]],
            "links": [["8", "None", "7", None]],
        },
        {
            "name": "index_source_int",
            "memories": [["7", "[]"], ["8", json.dumps([{"target_id": "7"}])]],
            "links": [[8, None, "7", None]],
        },
        {"name": "invalid_json", "memories": [[u(1), "[x"]], "links": []},
        {"name": "links_int_column", "memories": [[u(1), 5]], "links": []},
        {"name": "links_null_text", "memories": [[u(1), "null"]], "links": []},
        {"name": "links_number", "memories": [[u(1), "5"]], "links": []},
        {"name": "links_true", "memories": [[u(1), "true"]], "links": []},
        {"name": "links_str", "memories": [[u(1), '"abc"']], "links": []},
        {
            "name": "links_object",
            "memories": [[u(1), json.dumps({"target_id": u(1)})]],
            "links": [],
        },
        {
            "name": "link_not_object",
            "memories": [[u(1), "[]"], [u(2), json.dumps([1, "x", None, [u(1)]])]],
            "links": [],
        },
        {
            "name": "target_list",
            "memories": [[u(1), json.dumps([{"target_id": [1]}])]],
            "links": [],
        },
        {
            "name": "target_object_inactive",
            "memories": [[u(1), json.dumps([{"target_id": {"a": 1}}])]],
            "links": [],
        },
        {
            "name": "target_int",
            "memories": [[u(1), json.dumps([{"target_id": 1}])]],
            "links": [],
        },
        {
            "name": "type_list",
            "memories": [
                [u(1), "[]"],
                [u(2), json.dumps([{"type": [1], "target_id": u(1)}])],
            ],
            "links": [],
        },
        {
            "name": "note_object",
            "memories": [
                [u(1), "[]"],
                [u(2), json.dumps([{"note": {}, "target_id": u(1)}])],
            ],
            "links": [],
        },
        {
            "name": "note_list_target_inactive",
            "memories": [[u(2), json.dumps([{"note": [], "target_id": u(9)}])]],
            "links": [],
        },
        {
            "name": "repeated_key_last_wins",
            "memories": [
                [u(1), "[]"],
                [u(2), '[{"target_id": "x", "target_id": "' + u(1) + '"}]'],
            ],
            "links": [],
        },
        {
            "name": "error_after_a_record",
            "memories": [[u(1), "[]"], [u(2), links(sup(1))], [u(3), "7"]],
            "links": [],
        },
    ]
    for c in cases:
        c["call"] = "missing_links"
    marks = [
        [u(1), u(2), "2001-02-03T04:05:06.000000Z"],
        [u(3), u(3), "2001-02-03T04:05:07.000000Z"],
        [u(5), None, 5],
    ]
    for name, ids in [
        ("every_mark", None),
        ("none_asked", []),
        ("some_asked", [u(3), u(1), u(3), u(9)]),
        ("null_via", [u(5)]),
        # Read 500 at a time: the ids span three batches, the marks two.
        ("batched", [u(n) for n in range(2000, 3100)] + [u(5), u(1)]),
    ]:
        cases.append(
            {
                "name": f"marks_{name}",
                **one,
                "links": [],
                "returned": marks,
                "call": "returned_rows",
                "ids": ids,
            }
        )
    for name, ids in [("no_table_every", None), ("no_table_some", [u(1)])]:
        cases.append(
            {
                "name": f"marks_{name}",
                **one,
                "links": [],
                "returned": None,
                "call": "returned_rows",
                "ids": ids,
            }
        )
    # A str holding a lone surrogate equals no text the database holds,
    # though an id or a type may be its escape spelled out.
    escape = "\\ud800"
    cases += [
        {
            "name": "surrogate_target",
            "memories": [
                [escape, "[]"],
                ["a", '[{"type": "supersedes", "target_id": "\\ud800"}]'],
            ],
            "links": [],
            "call": "missing_links",
        },
        {
            "name": "surrogate_type",
            "memories": [
                [u(1), "[]"],
                [u(2), f'[{{"type": "\\ud800", "target_id": "{u(1)}"}}]'],
            ],
            "links": [[u(2), escape, u(1), None]],
            "call": "missing_links",
        },
    ]
    # Records whose str(id) repeats: missing_links keeps one pair per
    # str(id), at the first one's place with the last one's target.
    target = '[{"type": "supersedes", "target_id": "%s"}]'
    cases += [
        {
            "name": "missing_repeated_id",
            "memories": [
                ["b", target % "b"],
                ["b", target % u(5)],
                [u(5), "[]"],
                ["c", target % "c"],
            ],
            "links": [],
            "call": "missing_links",
        },
        {
            "name": "missing_int_and_text_id",
            "memories": [
                [7, target % u(5)],
                ["7", target % "c"],
                [u(5), "[]"],
                ["c", "[]"],
            ],
            "links": [],
            "call": "missing_links",
        },
        {
            "name": "missing_two_null_ids",
            "memories": [
                [None, target % u(5)],
                [None, target % "c"],
                [u(5), "[]"],
                ["c", "[]"],
            ],
            "links": [],
            "call": "missing_links",
        },
    ]
    # A repeated record id: the first one's place, the last one's values.
    for name, ids in [("repeated_every", None), ("repeated_some", ["a"])]:
        cases.append(
            {
                "name": f"marks_{name}",
                **one,
                "links": [],
                "returned": [["a", "x", "1"], ["b", "z", "3"], ["a", "y", "2"]],
                "call": "returned_rows",
                "ids": ids,
            }
        )
    cases.append(
        {
            "name": "marks_no_table_none_asked",
            **one,
            "links": [],
            "returned": None,
            "call": "returned_rows",
            "ids": [],
        }
    )
    return cases


def _pure_run(case: dict[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(TABLES[0])
        conn.execute(TABLES[1])
        if case.get("returned", []) is not None:
            conn.execute(TABLES[2])
        for row in case["memories"]:
            conn.execute("INSERT INTO memories VALUES (?, ?)", row)
        for row in case["links"]:
            conn.execute("INSERT INTO memory_links VALUES (?, ?, ?, ?)", row)
        for row in case.get("returned") or []:
            conn.execute("INSERT INTO returned VALUES (?, ?, ?)", row)
        try:
            if case["call"] == "missing_links":
                got: Any = [list(p) for p in store_returned.missing_links(conn).items()]
            else:
                rows = store_returned.returned_rows(
                    SimpleNamespace(_conn=conn),  # type: ignore[arg-type]
                    case["ids"],
                )
                got = [[k, v["via"], v["returned"]] for k, v in rows.items()]
            outcome: dict[str, Any] = {"answer": got}
        except Exception as exc:  # noqa: BLE001 - the raise is the record
            outcome = {"raised": type(exc).__name__}
    finally:
        conn.close()
    out = {
        "name": case["name"],
        "call": case["call"],
        "memories": [[_cell(v) for v in r] for r in case["memories"]],
        "links": [[_cell(v) for v in r] for r in case["links"]],
        "returned": None
        if case.get("returned", []) is None
        else [[_cell(v) for v in r] for r in case.get("returned") or []],
        **outcome,
    }
    if case["call"] == "returned_rows":
        ids = case["ids"]
        if ids is not None and len(ids) > 20:
            out["ids"] = {"range": [2000, 3100], "then": ids[-2:]}
        else:
            out["ids"] = ids
    return out


# The store cases.


def exec_(*statements: str) -> dict[str, Any]:
    return {"call": "exec", "sql": list(statements)}


PENDING = exec_(
    f"INSERT OR REPLACE INTO meta (key, value) VALUES ('{RETURNED_BACKFILL_KEY}', "
    "'pending')"
)
FORGET_MARKS = exec_(
    "DELETE FROM returned",
    "DELETE FROM log WHERE kind IN ('returned_put', 'returned_delete')",
)


def drop_record(n: int) -> dict[str, Any]:
    return exec_(f"DELETE FROM memories WHERE id = '{u(n)}'")


FIRST = "(SELECT min(seq) FROM log WHERE kind = 'memory_put')"
SECOND = "(SELECT max(seq) FROM log WHERE kind = 'memory_put')"


def put_payload(*pairs: str) -> dict[str, Any]:
    """Record puts' payloads replaced by hand: each row a subquery picks,
    then its new text."""
    return exec_(
        *(
            f"UPDATE log SET payload = '{text}' WHERE seq = {where}"
            for where, text in zip(pairs[::2], pairs[1::2], strict=True)
        )
    )


def backfill(session: str | None = "s9") -> dict[str, Any]:
    return {"call": "backfill", "session": session}


def ensure(has_key: bool = True) -> dict[str, Any]:
    return {"call": "ensure", "has_key": has_key}


def missing() -> dict[str, Any]:
    return {"call": "missing"}


def marks(*ns: int, every: bool = False) -> dict[str, Any]:
    return {"call": "marks", "ids": None if every else [u(n) for n in ns]}


def _store_cases() -> list[dict[str, Any]]:
    return [
        {
            # A chain whose head an earlier build deleted under the log:
            # the middle record is back in force, the first still history.
            "name": "supersedes_chain",
            "steps": [
                put(1),
                put(2, [1]),
                put(3, [2]),
                drop_record(3),
                marks(every=True),
                ensure(),
                PENDING,
                ensure(has_key=False),
                ensure(),
                marks(every=True),
                marks(2, 2, 1),
                ensure(),
                PENDING,
                ensure(),
            ],
        },
        {
            # A restore an earlier build wrote: its mark and rows gone.
            "name": "restored_tombstone",
            "steps": [
                put(1),
                put(4),
                tomb(4),
                tomb(1),
                restore(1),
                restore(4),
                marks(every=True),
                FORGET_MARKS,
                backfill(),
                marks(every=True),
                backfill(None),
            ],
        },
        {
            # The index rows a restore left out: the linking record is put
            # again, and the record it supersedes stays history.
            "name": "missing_link_rows",
            "steps": [
                put(1),
                linked(2, link("supersedes", 1)),
                linked(3, {"type": "supersedes", "target_id": u(1), "note": "n"}),
                exec_(f"DELETE FROM memory_links WHERE target_id = '{u(1)}'"),
                missing(),
                backfill(),
                missing(),
                marks(every=True),
            ],
        },
        {
            # Marked by this build: left to this build, its mark gone or not.
            "name": "already_marked",
            "steps": [
                put(1),
                put(2, [1]),
                tomb(2),
                marks(every=True),
                backfill(),
                exec_("DELETE FROM returned"),
                backfill(),
                marks(every=True),
            ],
        },
        {
            # A link into itself names nothing; an order of several ids.
            "name": "self_link_and_order",
            "steps": [
                put(5),
                put(3),
                put(1),
                exec_(
                    "UPDATE log SET payload = json_set(payload, '$.memory.links', "
                    f'json(\'[{{"type": "supersedes", "target_id": "{u(1)}"}}]\')) '
                    f"WHERE json_extract(payload, '$.memory.id') = '{u(1)}'"
                ),
                put(7, [5, 3]),
                drop_record(7),
                put(8, [9]),
                backfill(),
            ],
        },
        {
            # A store whose file lacks the table (read-only at step 2).
            "name": "no_table",
            "steps": [
                put(1),
                put(2, [1]),
                drop_record(2),
                exec_("DROP TABLE returned"),
                marks(every=True),
                marks(1),
                PENDING,
                ensure(),
                marks(every=True),
            ],
        },
        {
            # A tombstone_delete payload that is not JSON: json_extract
            # raises, and the hook logs it.
            "name": "malformed_log",
            "steps": [
                put(1),
                tomb(1),
                restore(1),
                exec_("UPDATE log SET payload = '{x' WHERE kind = 'tombstone_delete'"),
                PENDING,
                ensure(),
            ],
        },
        {
            # A record a tombstone_delete row names that is no longer
            # active: not marked.
            "name": "named_inactive",
            "steps": [
                put(1),
                tomb(1),
                restore(1),
                FORGET_MARKS,
                drop_record(1),
                backfill(),
                marks(every=True),
            ],
        },
        {
            # A supersedes target holding a lone surrogate: sqlite3 refuses
            # to bind it (UnicodeEncodeError, a ValueError), so the backfill
            # rolls back and the hook logs it and keeps the key.
            "name": "surrogate_target",
            "steps": [
                put(1),
                put(2),
                put_payload(
                    SECOND,
                    '{"memory": {"id": "x", "links": '
                    '[{"type": "supersedes", "target_id": "\\ud800"}]}}',
                ),
                backfill(),
                PENDING,
                ensure(),
                ensure(),
            ],
        },
        {
            # A linking record whose row entered outside the log
            # (unaccounted): the relink passes it over.
            "name": "missing_link_unaccounted",
            "steps": [
                put(1),
                linked(2, link("supersedes", 1)),
                exec_(
                    f"DELETE FROM memory_links WHERE target_id = '{u(1)}'",
                    f"UPDATE memories SET log_mac = 'broken' WHERE id = '{u(2)}'",
                ),
                missing(),
                backfill(),
                missing(),
                marks(every=True),
            ],
        },
        {
            # Two record puts that fail the fold, at seqs of one and two
            # digits: the problems ordered by seq as a number.
            "name": "problems_by_seq",
            "steps": [
                *(put(n) for n in range(1, 11)),
                put_payload("2", '{"memory": 0}', "10", '{"memory": 0}'),
                backfill(),
            ],
        },
        {
            # Record puts the backfill passes over or raises on.
            "name": "odd_puts",
            "steps": [
                put(1),
                put(2),
                put_payload(FIRST, "[1]", SECOND, '{"memory": 0}'),
                backfill(),
                put_payload(FIRST, '{"memory": [1]}'),
                backfill(),
                PENDING,
                ensure(),
                put_payload(FIRST, '{"memory": {"links": 3}}'),
                backfill(),
                put_payload(FIRST, '{"memory": {"id": "x", "links": "ab"}}'),
                backfill(),
            ],
        },
    ]


def _run_store(store: Store, call: dict[str, Any]) -> Any:
    what = call["call"]
    if what == "exec":
        for statement in call["sql"]:
            store.conn.execute(statement)
        store.conn.commit()
        return None
    if what == "backfill":
        return store_returned.backfill(store, session=call["session"])
    if what == "ensure":
        lines: list[list[str]] = []

        class Catch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append([record.levelname, record.getMessage()])

        logger = logging.getLogger("bettermemory.store")
        handler = Catch(level=logging.DEBUG)
        level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(
                Store, "has_key", new_callable=mock.PropertyMock
            ) as has_key:
                has_key.return_value = call["has_key"]
                got = store_returned.ensure_backfilled(store)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
        return {"marked": got, "log": lines}
    if what == "missing":
        return [list(p) for p in store_returned.missing_links(store.conn).items()]
    if what == "marks":
        rows = store_returned.returned_rows(store, call["ids"])
        return [[k, v["via"], v["returned"]] for k, v in rows.items()]
    return _run_all(store, call)


def _dump(lines: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    ).encode("ascii")


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [{"fixture": "store_returned", "tables": TABLES}]
    lines += [_pure_run(c) for c in _pure_cases()]
    return {"store_returned.jsonl": _dump(lines)}


def generate_posix() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_returned_posix",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c, _run_store) for c in _store_cases()]
    opened = runs[0]["opened"]
    for run in runs:
        if run.pop("opened") != opened:
            raise AssertionError(f"{run['case']} opened another state")
    lines[0]["opened"] = opened
    lines += runs
    return {"store_returned_posix.jsonl": _dump(lines)}
