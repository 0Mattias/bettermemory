"""Fixture: the store's document frequencies and ranking statistics over
a real store (``store_docfreq_store.jsonl``).

The native build ports the store's side of the document frequency memo
(``native/``, `store::docfreq_store`) and is held to this file. Each case
starts from the store `Store.create` makes under ``store_create.json``'s
fixed values, opened as the owner (as ``store_tx.jsonl``'s are), and runs
its steps. A step first writes through the reference's own writers
(`_mutate` inside `_transaction`, every clock fixed) or edits the store
(the digest's state planted, a pragma), then asks `ranking_statistics`
and `document_frequencies` (``memoised`` True and False) with an admit
predicate the fixture names, on a connection the step names: the owner's
outside a transaction, inside `store.batch()` (after a nested write that
failed, or after the connection's own ``ROLLBACK``), under ``query_only``,
against a second connection holding the write lock, or a second store
opened ``read_only=True`` (under another spelling of the path).

Recorded per step: the tables the step changed since the last step's
end, in full (``memories`` with its row numbers, ``homes``, ``log``,
``tombstones`` and the three digest tables), so a replay lays them into
its own store before it asks; each answer (dicts as pairs, the average
as ``repr``); the memo's keys (the path under the case's root as
``<root>``) and each one's terms kept; the digest's state and active
rows after the step; whether the head checkpoint file is unchanged; and
each warning the ``bettermemory.store`` logger took, as its format, the
exception's class and its SQLite result code, and the format's arguments
(the message is the format with them put in), the root spelled
``<root>`` and the exception's text spelled ``<exc>`` where that text is
SQLite's or a library's wording (kept where it is the store's own: a
`StoreVersionError`, the rolled-back transaction's).

A step may also ask the digest itself (``mark``: `digest_mark(build=
False)`; ``rows``: `digest_admission_rows`; ``head``: `_memory_head`),
on the connection inside a batch that commits or rolls back, or on one
left inside a transaction no call owns (``residue``). A step's ``sql``
holds statements a replay runs before laying the tables, for what the
tables do not carry (a view, ``meta``, a table's constraints).

Each case runs twice, the second time with `store_index._advanced_inputs`
answering None, as the native build has it (not ported: the memo reads
its inputs again where the reference carries an entry over a moved
head). Where a step's memo or warnings differ between the runs, the
second run's are recorded beside them (``memo_fresh``,
``warnings_fresh``): the entry carried over keeps the terms asked before
the head moved, and its own `digest_mark` call logs a second warning.

Gates, ``store_tx.jsonl``'s and for the same reasons. Unicode: the index
text is the tokenizer's spelling and the digest's rows its tokens.
pydantic-core: the records are built through the models. POSIX only: the
create and open write and check the key ring's modes and binding. Not on
the Python version and not on the SQLite version: no text recorded is the
interpreter's or SQLite's.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bettermemory.store as store_module
from bettermemory import store_index
from bettermemory.store import Store

from ._fixed import Ticker, fixed, ulid_mint
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, _engine, _made
from .gen_store_write import memory, ulid

LOG0 = datetime(2001, 2, 3, 5, 0, 0, 111111, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1, microseconds=1)
WALL0 = datetime(2003, 4, 5, 6, 7, 8, 999000, tzinfo=timezone.utc)
WALL_STEP = timedelta(minutes=7, microseconds=3)

#: The tables a replay lays in, and the order of their rows.
TABLES = {
    "memories": "SELECT rowid AS rowid, * FROM memories ORDER BY rowid",
    "homes": "SELECT * FROM homes ORDER BY record_id",
    "log": "SELECT * FROM log ORDER BY seq",
    "tombstones": "SELECT * FROM tombstones ORDER BY id",
    "digest_records": "SELECT * FROM digest_records ORDER BY doc",
    "digest_tokens": "SELECT * FROM digest_tokens ORDER BY token, doc",
    "digest_state": "SELECT * FROM digest_state ORDER BY key",
}


class Injected(Exception):
    """A nested write's failure."""


# -- the writes --------------------------------------------------------------


def put(n: int, body: str | None = None, **fields: Any) -> dict[str, Any]:
    return {"op": "memory_put", "payload": memory(n, body=body, **fields)}


def delete(n: int) -> dict[str, Any]:
    return {"op": "memory_delete", "payload": {"id": ulid(n)}}


def home(n: int, project: str) -> dict[str, Any]:
    return {"home": n, "project": project}


def home_delete(n: int) -> dict[str, Any]:
    return {"op": "home_delete", "payload": {"record_id": ulid(n)}}


def _write(store: Store, item: dict[str, Any]) -> None:
    with store._transaction() as conn:
        if "home" in item:
            row = {
                "record_id": ulid(item["home"]),
                "kind": "memory",
                "project": item["project"],
                "checkout": None,
                "rule": "fixture",
                "decided": "2001-02-03T04:05:06.000000Z",
            }
            store._mutate(conn, "home_put", {"home": row}, session="s1")
        else:
            payload = json.loads(json.dumps(item["payload"]))
            store._mutate(conn, item["op"], payload, session="s1")


# -- the questions -------------------------------------------------------------

#: Every predicate a step can name: what a replay evaluates the same way.
#: ``all``: every row; ``none``: no row; ``scope:<s>``: `s` among the
#: scopes; ``project:<p>``: the home names `p`; ``homebytes:<hex>``: the
#: home is those bytes (a BLOB); ``nohome``: no home;
#: ``client:<c>``: an actor whose client is `c`; ``noactor``: no actor.


def admit_of(name: str):  # noqa: ANN201 - a predicate
    kind, _, arg = name.partition(":")

    def admit(scopes: list[Any], project: Any, actor: Any) -> bool:
        if kind == "homebytes":
            return project == bytes.fromhex(arg)
        if kind == "all":
            return True
        if kind == "none":
            return False
        if kind == "scope":
            return arg in scopes
        if kind == "project":
            return project == arg
        if kind == "nohome":
            return project is None
        if kind == "client":
            return actor is not None and actor.client == arg
        if kind == "noactor":
            return actor is None
        raise AssertionError(name)

    return admit


def _answer(call: str, store: Store, terms: list[str], admit: str) -> Any:
    try:
        if call == "mark":
            return {"returned": store.digest_mark(build=False)}
        if call == "rows":
            rows = store.digest_admission_rows()
            got_rows = None if rows is None else [[_cell(v) for v in r] for r in rows]
            return {"returned": got_rows}
        if call == "head":
            return {"returned": list(store_index._memory_head(store))}
        if call == "rank":
            got: Any = store.ranking_statistics(terms, admit=admit_of(admit))
        else:
            got = store.document_frequencies(
                terms, admit=admit_of(admit), memoised=call == "memo"
            )
    except Exception as exc:  # noqa: BLE001 - the raise is the record
        return {"raised": type(exc).__name__}
    if got is None:
        return {"returned": None}
    out = [got[0], sorted(got[1].items()), sorted(got[2].items())]
    if call == "rank":
        out.append(None if got[3] is None else repr(got[3]))
    return {"returned": out}


# -- the record ----------------------------------------------------------------


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, float):
        return {"$float": repr(value)}
    return value


def tables(file: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    try:
        out = {}
        for table, sql in TABLES.items():
            try:
                cur = conn.execute(sql)
                out[table] = {
                    "columns": [d[0] for d in cur.description],
                    "rows": [[_cell(v) for v in row] for row in cur],
                }
            except sqlite3.OperationalError:
                # A table a step's ``sql`` made unreadable (a view over
                # nothing, one that overflows at a row): left out, and the
                # replay's ``sql`` makes it.
                continue
        return out
    finally:
        conn.close()


def digest_after(file: Path) -> dict[str, Any]:
    t = tables(file)
    cols = t["digest_records"]["columns"]
    keep = [
        cols.index(c) for c in ("rid", "removed", "dl", "scopes", "client", "model")
    ]
    return {
        "state": t["digest_state"]["rows"],
        "records": [[r[i] for i in keep] for r in t["digest_records"]["rows"]],
        "tokens": len(t["digest_tokens"]["rows"]),
    }


def _own_wording(exc: BaseException) -> bool:
    """Whether an exception's text is the store's own, which the native
    build words the same: not SQLite's, the interpreter's or a library's."""
    return type(exc).__name__ == "StoreVersionError" or str(exc).startswith(
        "the write transaction on store "
    )


class Taken(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[Any] = []
        self.root = ""

    def emit(self, record: logging.LogRecord) -> None:
        args = list(record.args) if isinstance(record.args, tuple) else []
        exc = args[-1] if args and isinstance(args[-1], BaseException) else None
        if exc is not None and not _own_wording(exc):
            args[-1] = "<exc>"
        spelled = [str(a).replace(self.root, "<root>") for a in args]
        self.lines.append(
            [
                record.levelname,
                record.msg,
                None if exc is None else type(exc).__name__,
                getattr(exc, "sqlite_errorcode", None),
                spelled,
            ]
        )


def memo_view(root: Path) -> list[Any]:
    out = []
    for (path, store_id), entry in store_index._DOC_FREQS.items():
        spelled = str(path).replace(str(root), "<root>", 1)
        out.append([spelled, store_id, list(entry.terms)])
    return out


# -- the cases -----------------------------------------------------------------

HEAD = [["head", [], "all"]]
DIGEST = [["mark", [], "all"], ["rows", [], "all"]]

ASK_ALL = [
    ["rank", ["sqlite", "record"], "all"],
    ["memo", ["sqlite", "record"], "all"],
    ["plain", ["sqlite", "record"], "all"],
]


def ask(*terms: str, admit: str = "all") -> list[list[Any]]:
    return [[c, list(terms), admit] for c in ("rank", "memo", "plain")]


def _overflowing(n: int) -> str:
    """A view in place of ``homes`` whose project overflows for record
    `n`: the step that reaches that row fails."""
    return (
        "CREATE VIEW homes AS SELECT record_id, kind, CASE WHEN "
        f"record_id = '{ulid(n)}' THEN abs(-9223372036854775807 - 1) "
        "ELSE project END AS project, checkout, rule, decided FROM homes_t"
    )


def cases() -> list[dict[str, Any]]:
    three = [put(1), put(2, body="second record body"), put(3, scopes=["alpha"])]
    return [
        {
            "name": "empty_store",
            "steps": [{"asks": ask("sqlite") + ask() + ask("", "")}],
        },
        {
            # The open built the digest; writes after it leave it behind by
            # one, then by several; each ask applies what it is behind by.
            "name": "digest_behind",
            "steps": [
                {
                    "writes": three[:1],
                    "asks": ask("sqlite", "record") + [["memo", ["", "sqlite"], "all"]],
                },
                {"asks": ask("sqlite", "record")},
                {"writes": three[1:], "asks": ask("sqlite", "record", "body")},
                {
                    "writes": [put(4), put(5, body="sqlite sqlite sqlite"), put(6)],
                    "asks": ask("sqlite", "number"),
                },
            ],
        },
        {
            "name": "no_digest_state",
            "steps": [
                {
                    "writes": three,
                    "edit": "drop_state",
                    "asks": ask("sqlite", "record"),
                },
                {"asks": ask("sqlite")},
                {
                    # A new memo key reads the memory rows: scopes that are
                    # not JSON (JSONDecodeError on every path).
                    "conn": "spelled",
                    "edit": "sql:UPDATE memories SET scopes_json = 'no' WHERE id = ?",
                    "asks": ask("sqlite")[:2],
                },
            ],
        },
        {
            "name": "other_fingerprint",
            "steps": [
                {"writes": three, "edit": "fingerprint", "asks": ask("sqlite", "body")},
            ],
        },
        {
            "name": "scopes_not_list",
            "steps": [
                {
                    "writes": three,
                    "edit": "scopes_not_list",
                    "asks": ask("record", "second"),
                },
                {"edit": "fingerprint", "asks": ask("record", "second")},
            ],
        },
        {
            "name": "actor_not_text",
            "steps": [
                {"writes": three, "edit": "fingerprint", "asks": ask("record")},
                {"writes": [put(4)], "edit": "actor_int", "asks": ask("record")},
                {
                    # The first row's scopes are not JSON: the grouping
                    # meets them before the second row's actor.
                    "writes": [put(5)],
                    "edit": "sql:UPDATE memories SET scopes_json = 'no' WHERE id = ?",
                    "asks": ask("record"),
                },
            ],
        },
        {
            "name": "write_between_calls",
            "steps": [
                {"writes": three, "asks": ask("sqlite", "record")},
                {
                    "writes": [put(2, body="rewritten sqlite text")],
                    "asks": ask("sqlite", "rewritten"),
                },
                {"writes": [delete(1)], "asks": ask("sqlite", "record")},
                {
                    "writes": [put(7, body="record seven")],
                    "asks": ask("record", "seven"),
                },
            ],
        },
        {
            "name": "homes",
            "steps": [
                {
                    "writes": [*three, home(1, "alpha"), home(2, "beta")],
                    "asks": ask("sqlite", "record", admit="project:alpha")
                    + ask("record", admit="nohome"),
                },
                {
                    "writes": [home_delete(1)],
                    "asks": ask("sqlite", "record", admit="project:alpha")
                    + ask("record", admit="nohome"),
                },
            ],
        },
        {
            "name": "tombstone",
            "steps": [
                {"writes": [*three, delete(2)], "asks": ask("record", "second")},
                {
                    "writes": [put(2, body="second record back")],
                    "asks": ask("record", "back"),
                },
            ],
        },
        {
            "name": "terms_odd",
            "steps": [
                {
                    "writes": [put(1, body='a "quoted" word and NEAR AND OR'), put(2)],
                    "asks": ask(
                        '"quoted"', "quoted", "NEAR", "AND", "*", "x OR y", "body_fts"
                    ),
                }
            ],
        },
        {
            "name": "scopes_only",
            "steps": [
                {
                    "writes": [
                        put(1, scopes=["sqlite"]),
                        put(2, scopes=["other"], body="plain words"),
                    ],
                    "asks": ask("sqlite", "other", "plain"),
                }
            ],
        },
        {
            "name": "admit_by",
            "steps": [
                {
                    "writes": [
                        put(1, scopes=["alpha"]),
                        put(2, scopes=["beta"], actor={"client": "c1", "model": "m1"}),
                        put(3, scopes=["alpha", "beta"], actor={"client": "c2"}),
                        put(4, actor={"model": "m2"}),
                        home(3, "proj"),
                    ],
                    "asks": ask("sqlite", "record", admit="scope:alpha")
                    + ask("sqlite", admit="scope:beta")
                    + ask("sqlite", admit="client:c1")
                    + ask("record", admit="noactor")
                    + ask("record", admit="project:proj")
                    + ask("record", admit="none"),
                }
            ],
        },
        {
            "name": "in_batch",
            "steps": [
                {"writes": three, "asks": ask("sqlite")},
                {
                    "conn": "batch",
                    "writes": [put(4, body="batch sqlite")],
                    "asks": ask("sqlite", "batch"),
                },
                {"asks": ask("sqlite", "batch")},
                {
                    "conn": "batch_failed",
                    "writes": [put(5, body="kept sqlite")],
                    "asks": ask("sqlite", "kept"),
                },
                {
                    "conn": "batch_rolled",
                    "writes": [put(6, body="gone sqlite")],
                    "asks": ask("sqlite", "gone"),
                },
                {"asks": ask("sqlite", "gone", "kept")},
            ],
        },
        {
            # The digest asked directly inside a transaction: joined under a
            # savepoint, committed with the batch or rolled back with it; on
            # a transaction SQLite rolled back; on a connection left inside
            # one no call owns.
            "name": "digest_in_transaction",
            "steps": [
                {"writes": three[:1], "asks": []},
                {"conn": "batch", "asks": DIGEST},
                {"writes": three[1:2], "asks": []},
                {"conn": "batch_raised", "asks": DIGEST},
                {"conn": "batch_rolled", "asks": DIGEST},
                {"conn": "residue", "asks": DIGEST + ask("sqlite")[:1]},
            ],
        },
        {
            # TEXT affinity keeps a BLOB as it is: `admit` gets the bytes.
            "name": "home_bytes",
            "steps": [
                {
                    "writes": [three[0], home(1, "alpha")],
                    "edit": "sql:UPDATE homes SET project = X'7031' WHERE record_id = ?",
                    "asks": ask("record", admit="homebytes:7031") + DIGEST,
                },
            ],
        },
        {
            # A log with no memory row; then a rebuild row (planted) moves
            # the memo's key past the last write.
            "name": "memory_head",
            "steps": [
                {"asks": HEAD},
                {"writes": three[:1], "asks": HEAD + ask("sqlite")[:1]},
                {"edit": "rebuilt_row", "asks": HEAD + ask("sqlite")[:1]},
            ],
        },
        {
            "name": "digest_rows_bad",
            "steps": [
                {"writes": three[:1], "asks": ask("record")},
                {"edit": "seq_huge", "asks": ask("record")[:2] + DIGEST[:1]},
            ],
        },
        {
            # The digest's rows hand the actor columns on as they are: a
            # client stored as bytes (planted) is bytes in `rows`, and the
            # grouping's `Actor` decodes it or refuses it.
            "name": "digest_client_bytes",
            "steps": [
                {"writes": three[:1], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE digest_records SET client = X'6331' WHERE id = ?",
                    "asks": DIGEST + ask("record", admit="client:c1")[:1],
                },
                {
                    "conn": "spelled",
                    "edit": "sql:UPDATE digest_records SET client = X'FF' WHERE id = ?",
                    "asks": DIGEST + ask("record")[:2],
                },
            ],
        },
        {
            # The digest holds one row number twice (planted: no constraint
            # forbids it). The grouping gives each row read the index's
            # length before it, so the second row's position is one past
            # the index, inside the masks' last byte: the memo answers.
            "name": "digest_rid_twice",
            "steps": [
                {"writes": three[:2], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE digest_records SET rid = 1 WHERE id <> ?",
                    "asks": ask("record") + DIGEST[1:],
                },
            ],
        },
        {
            "name": "digest_rid_twice_first",
            "steps": [
                {"writes": three[:2], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE digest_records SET rid = 2 WHERE id = ?",
                    "asks": ask("record") + DIGEST[1:],
                },
            ],
        },
        {
            # Nine rows on one row number: the index stays one long, so
            # every row after the first is at position one.
            "name": "digest_rid_all_one",
            "steps": [
                {"writes": [put(n) for n in range(1, 10)], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE digest_records SET rid = 1 WHERE id <> ?",
                    "asks": ask("record") + DIGEST[1:],
                },
            ],
        },
        {
            # Eight row numbers, then the ninth row on the first: its
            # position, eight, is past the one byte a width of eight makes
            # (IndexError).
            "name": "digest_rid_past_bytes",
            "steps": [
                {"writes": [put(n) for n in range(1, 10)], "asks": DIGEST[:1]},
                {
                    "sql": [
                        f"UPDATE digest_records SET rid = 1 WHERE id = '{ulid(9)}'"
                    ],
                    "asks": ask("record") + DIGEST[1:],
                },
            ],
        },
        {
            # The memory rows read without the digest. sqlite3 steps to the
            # next row before it hands one on: a step that fails (a view
            # whose project overflows at one record) raises before the row
            # ahead of it is grouped, so the first row's `Actor` refuses
            # its client when the third fails, and nothing is grouped when
            # the second fails.
            "name": "fallback_order",
            "steps": [
                {
                    "writes": [*three, home(2, "beta"), home(3, "gamma")],
                    "edit": "fingerprint",
                    "asks": ask("record")[:2],
                },
                {
                    "writes": [put(4)],
                    "edit": "sql:UPDATE memories SET actor_client = X'FF' WHERE id = ?",
                    "sql": ["ALTER TABLE homes RENAME TO homes_t", _overflowing(3)],
                    "asks": ask("record"),
                },
                {
                    "writes": [put(5)],
                    "edit": "sql:UPDATE memories SET scopes_json = 'no' WHERE id = ?",
                    "sql": ["DROP VIEW homes", _overflowing(2)],
                    "asks": ask("record")[:2],
                },
            ],
        },
        {
            # A log payload `_mutated_ids` cannot read, one per step, each
            # named by the class the reference raises.
            "name": "payload_bad",
            "steps": [
                {"writes": three[:1], "asks": DIGEST[:1]},
                {
                    "edit": "log:memory_put '[1]'",
                    "asks": DIGEST[:1] + ask("record")[:1],
                },
                {"edit": "log:memory_put '{}'", "asks": DIGEST[:1]},
                {
                    "edit": 'log:tombstone_put \'{"tombstone": "t"}\'',
                    "asks": DIGEST[:1],
                },
                {"edit": "log:memory_delete '{\"x\": 1}'", "asks": DIGEST[:1]},
                {"edit": "log:memory_put X'FF'", "asks": DIGEST[:1]},
                {"edit": "log:memory_put 'no'", "asks": DIGEST[:1]},
                # An id holding a lone surrogate reads; binding it refuses
                # it (UnicodeEncodeError), after every payload read.
                {
                    "edit": 'log:memory_delete \'{"id": "\\ud800"}\'',
                    "asks": DIGEST + ask("record")[:1],
                },
                {
                    "edit": 'log:memory_delete \'{"id": "\\ud800"}\''
                    " | memory_put '{}'",
                    "asks": DIGEST[:1],
                },
            ],
        },
        {
            # A record a planted log row names does not load: named by the
            # loader's own class (a links column that is not JSON, then
            # bytes that do not decode), read again after the rollback.
            "name": "record_unloadable",
            "steps": [
                {"writes": three[:2], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE memories SET links_json = 'no' WHERE id = ?",
                    "asks": [],
                },
                {
                    "edit": f'log:memory_delete \'{{"id": "{ulid(1)}"}}\'',
                    "asks": DIGEST[:1] + ask("record")[:1],
                },
                {
                    "edit": "sql:UPDATE memories SET links_json = X'FF' WHERE id = ?",
                    "asks": [],
                },
                {
                    "edit": f'log:memory_delete \'{{"id": "{ulid(1)}"}}\'',
                    "asks": DIGEST[:1],
                },
                # The record named first fails to load before the id after
                # it, which holds a lone surrogate, is bound.
                {
                    "edit": f'log:memory_delete \'{{"id": "{ulid(1)}"}}\''
                    ' | memory_delete \'{"id": "\\ud800"}\'',
                    "asks": DIGEST[:1],
                },
                # The other order: the id holding a lone surrogate is bound
                # first and refused (UnicodeEncodeError) before the record
                # named after it is read.
                {
                    "edit": 'log:memory_delete \'{"id": "\\ud800"}\''
                    f' | memory_delete \'{{"id": "{ulid(1)}"}}\'',
                    "asks": DIGEST[:1],
                },
            ],
        },
        {
            # The digest's rows: a length that does not read on the row
            # ahead of a step that fails (a view in place of `homes` that
            # overflows at the second record). The step fails first, so the
            # second warning and None, before `int()` meets the length.
            "name": "digest_row_ahead",
            "steps": [
                {"writes": [*three[:2], home(2, "beta")], "asks": DIGEST[:1]},
                {
                    "edit": "sql:UPDATE digest_records SET dl = 'x' WHERE id = ?",
                    "sql": ["ALTER TABLE homes RENAME TO homes_t", _overflowing(2)],
                    "asks": DIGEST[1:] + ask("record")[:2],
                },
            ],
        },
        {
            # The digest's table made again without its NOT NULL (planted),
            # a length NULL: int(None) raises TypeError, uncaught.
            "name": "digest_length_null",
            "steps": [
                {"writes": three[:1], "asks": ask("record")},
                {
                    "sql": [
                        "ALTER TABLE digest_records RENAME TO digest_old",
                        "CREATE TABLE digest_records AS SELECT * FROM digest_old",
                        "DROP TABLE digest_old",
                        f"UPDATE digest_records SET dl = NULL WHERE id = '{ulid(1)}'",
                    ],
                    "asks": ask("record") + DIGEST,
                },
            ],
        },
        {
            # The digest is current; its rows' join meets a view over nothing.
            "name": "digest_not_read",
            "steps": [
                {"writes": three[:1], "asks": ask("record")},
                {
                    "sql": [
                        "DROP TABLE homes",
                        "CREATE VIEW homes AS SELECT * FROM nothing_here",
                    ],
                    "asks": DIGEST + ask("record"),
                },
            ],
        },
        {
            # The writer check fails on meta that is not a whole number.
            "name": "writer_versions",
            "steps": [
                {"writes": three[:1], "asks": []},
                {
                    "writes": three[1:2],
                    "sql": [
                        "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_writer_revision', 'x')"
                    ],
                    "asks": DIGEST + ask("record"),
                },
            ],
        },
        {
            # The memo carries its entry over a moved head in the
            # reference; a busy writer makes both digest reads warn there.
            "name": "advance_busy",
            "steps": [
                {"writes": three[:1], "asks": ask("sqlite")},
                {"writes": three[1:], "conn": "busy", "asks": ask("sqlite")[:2]},
            ],
        },
        {
            "name": "query_only",
            "steps": [
                {
                    "writes": three,
                    "conn": "query_only",
                    "asks": ask("sqlite", "record"),
                },
                {"asks": ask("sqlite", "record")},
            ],
        },
        {
            "name": "busy_writer",
            "steps": [
                {"writes": three, "conn": "busy", "asks": ask("sqlite", "record")},
                {"asks": ask("sqlite", "record")},
            ],
        },
        {
            "name": "read_only",
            "steps": [
                {"writes": three, "conn": "read_only", "asks": ask("sqlite", "record")},
                {"conn": "spelled", "asks": ask("sqlite")},
                {"asks": ask("sqlite")},
            ],
        },
    ]


# -- the driver ------------------------------------------------------------------


def _edit(store: Store, edit: str) -> None:
    if edit == "drop_state":
        store.conn.execute("DELETE FROM digest_state")
    elif edit == "fingerprint":
        store.conn.execute(
            "UPDATE digest_state SET value = 'another' WHERE key = 'fingerprint'"
        )
    elif edit == "scopes_not_list":
        # Planted: no writer writes them. The digest keeps the lists it
        # read; the memory rows hold what is not one.
        store.conn.execute(
            "UPDATE memories SET scopes_json = '\"global\"' WHERE id = ?", (ulid(2),)
        )
        store.conn.execute(
            "UPDATE memories SET scopes_json = '{\"k\": 1}' WHERE id = ?", (ulid(3),)
        )
    elif edit.startswith("sql:"):
        store.conn.execute(edit[4:], (ulid(1),))
    elif edit == "rebuilt_row":
        seq = store.conn.execute("SELECT MAX(seq) FROM log").fetchone()[0] + 1
        store.conn.execute(
            "INSERT INTO log (seq, ts, kind, payload, prev_mac, mac, session) "
            "SELECT ?, ts, 'index_rebuilt', '{}', mac, 'rebuilt-mac', session "
            "FROM log WHERE seq = ?",
            (seq, seq - 1),
        )
    elif edit.startswith("log:"):
        # Planted: the digest moved to the log's last row, then a row past
        # it whose payload `_mutated_ids` cannot read.
        # Several rows are separated by `` | ``.
        seq, mac = store.conn.execute(
            "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        for key, value in (("seq", str(seq)), ("mac", mac)):
            store.conn.execute(
                "UPDATE digest_state SET value = ? WHERE key = ?", (value, key)
            )
        for item in edit[4:].split(" | "):
            kind, payload = item.split(" ", 1)
            store.conn.execute(
                "INSERT INTO log (seq, ts, kind, payload, prev_mac, mac, session) "
                f"SELECT ?, ts, ?, {payload}, mac, ?, session FROM log WHERE seq = ?",
                (seq + 1, kind, f"planted-{seq + 1}", seq),
            )
            seq += 1
    elif edit == "seq_huge":
        store.conn.execute(
            "UPDATE digest_state SET value = '99999999999999999999' WHERE key = 'seq'"
        )
    elif edit == "actor_int":
        # TEXT affinity keeps a number as its text: a blob stays a blob.
        store.conn.execute(
            "UPDATE memories SET actor_client = X'FF' WHERE id = ?", (ulid(2),)
        )
        store.conn.execute(
            "UPDATE memories SET actor_model = X'6D6F64656C' WHERE id = ?", (ulid(3),)
        )
    else:
        raise AssertionError(edit)
    store.conn.commit()


def _ask_all(store: Store, asks: list[list[Any]]) -> list[Any]:
    return [[*a, _answer(a[0], store, a[1], a[2])] for a in asks]


def _step(store: Store, step: dict[str, Any], case: Path, root: Path) -> list[Any]:
    conn = step.get("conn", "plain")
    asks = step["asks"]
    if conn == "plain":
        return _ask_all(store, asks)
    if conn.startswith("batch"):
        answers: list[Any] = []
        try:
            with store.batch():
                for item in step.get("writes", []):
                    _write(store, item)
                if conn == "batch_failed":
                    try:
                        with store._transaction() as tx:
                            store._mutate(
                                tx, "memory_put", put(9)["payload"], session="s1"
                            )
                            raise Injected("nested")
                    except Injected:
                        pass
                if conn == "batch_rolled":
                    store.conn.execute("ROLLBACK")
                answers = _ask_all(store, asks)
                if conn == "batch_raised":
                    raise Injected("the batch")
        except (sqlite3.Error, Injected):
            pass
        return answers
    if conn == "residue":
        store.conn.execute("BEGIN")
        store_module._residue_logged = False
        try:
            return _ask_all(store, asks)
        finally:
            if store.conn.in_transaction:
                store.conn.execute("ROLLBACK")
    if conn == "query_only":
        store.conn.execute("PRAGMA query_only = ON")
        try:
            return _ask_all(store, asks)
        finally:
            store.conn.execute("PRAGMA query_only = OFF")
    if conn == "busy":
        other = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        saved = store.busy_timeout
        store.set_busy_timeout(0)
        try:
            return _ask_all(store, asks)
        finally:
            store.set_busy_timeout(saved)
            other.execute("ROLLBACK")
            other.close()
    if conn in ("read_only", "spelled"):
        path = case / "memory.sqlite"
        if conn == "spelled":
            path = case / ".." / case.name / "memory.sqlite"
        reader = Store.open(path, keys_dir=case / "keys", read_only=True)
        try:
            return _ask_all(reader, asks)
        finally:
            reader.close()
    raise AssertionError(conn)


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    """The case as the reference answers it, with ``memo_fresh`` and
    ``warnings_fresh`` where the run without `_advanced_inputs` differs."""
    out = _run(case_def)
    advanced = store_index._advanced_inputs
    store_index._advanced_inputs = lambda *_a, **_k: None
    try:
        fresh = _run(case_def)
    finally:
        store_index._advanced_inputs = advanced
    for step, again in zip(out["steps"], fresh["steps"], strict=True):
        for field in ("memo", "warnings"):
            if again[field] != step[field]:
                step[f"{field}_fresh"] = again[field]
    return out


def _run(case_def: dict[str, Any]) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    store_index._clear_doc_freqs()
    taken = Taken()
    logger = logging.getLogger("bettermemory.store")
    logger.addHandler(taken)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        taken.root = str(root)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        head = case / "keys" / f"{STORE_ID}.head"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            # The opened store is the same for every case and the replay's
            # own open makes it: only what a step changed is recorded.
            last: dict[str, Any] = tables(file)
            for i, step in enumerate(case_def["steps"]):
                with fixed(
                    log=Ticker(LOG0 + timedelta(minutes=i), LOG_STEP),
                    wall=Ticker(WALL0 + timedelta(hours=i), WALL_STEP),
                    mint=ulid_mint(1000 * i),
                ):
                    batch = step.get("conn", "").startswith("batch")
                    if not batch:
                        for item in step.get("writes", []):
                            _write(store, item)
                    if "edit" in step:
                        _edit(store, step["edit"])
                    for statement in step.get("sql", []):
                        store.conn.execute(statement)
                    if "sql" in step:
                        store.conn.commit()
                    now = tables(file)
                    seen_before = last
                    changed = {t: v for t, v in now.items() if last.get(t) != v}
                    before = head.read_bytes() if head.exists() else None
                    taken.lines.clear()
                    if batch:
                        # The batch's own writes: the replay lays them in
                        # committed and asks inside a transaction of its own.
                        answers, changed = _batch_step(store, step, file, changed, last)
                    else:
                        answers = _step(store, step, case, root)
                    after = head.read_bytes() if head.exists() else None
                last = tables(file)
                extra = {"sql": step["sql"]} if "sql" in step else {}
                steps_out.append(
                    {
                        **extra,
                        "step": i,
                        "conn": step.get("conn", "plain"),
                        "tables": _appended(changed, seen_before),
                        "answers": answers,
                        "memo": memo_view(root),
                        "digest": digest_after(file),
                        # A batch's commit moves the head; the replay's
                        # transaction commits nothing.
                        "head_unchanged": None if batch else before == after,
                        "warnings": list(taken.lines),
                    }
                )
        finally:
            store.close()
            logger.removeHandler(taken)
    return {"case": case_def["name"], "steps": steps_out}


def _appended(changed: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
    """The log, which only grows, as the rows after the last step's."""
    out = dict(changed)
    log = out.get("log")
    if log is not None:
        before = last["log"]["rows"]
        if log["rows"][: len(before)] == before:
            out["log"] = {
                "columns": log["columns"],
                "append": log["rows"][len(before) :],
            }
    return out


def _batch_step(
    store: Store,
    step: dict[str, Any],
    file: Path,
    changed: dict[str, Any],
    last: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """The batch's answers, and the tables as the batch's connection saw
    them when it asked (its uncommitted writes included)."""
    seen: dict[str, Any] = {}
    real = _ask_all

    def spy(s: Store, asks: list[list[Any]]) -> list[Any]:
        seen.update(_conn_tables(s.conn))
        return real(s, asks)

    globals()["_ask_all"] = spy
    try:
        answers = _step(store, step, file.parent, file.parent.parent)
    finally:
        globals()["_ask_all"] = real
    view = {t: v for t, v in seen.items() if last.get(t) != v}
    return answers, {**changed, **view}


def _conn_tables(conn: sqlite3.Connection) -> dict[str, Any]:
    out = {}
    for table, sql in TABLES.items():
        cur = conn.execute(sql)
        out[table] = {
            "columns": [d[0] for d in cur.description],
            "rows": [[_cell(v) for v in row] for row in cur],
        }
    return out


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_docfreq_store",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
        }
    ]
    with world():
        lines += [run_case(c) for c in cases()]
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_docfreq_store.jsonl": text.encode("ascii")}
