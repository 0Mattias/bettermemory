"""Fixture: the store's document frequencies and ranking statistics over
a real store (``store_docfreq_store.jsonl``, ``store_docfreq_store_b.jsonl``
and, for an entry carried over a moved head, ``store_docfreq_advance.jsonl``;
`FILES` names the cases each holds).

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
end (``memories`` with its row numbers, ``homes``, ``log``,
``tombstones`` and the three digest tables), so a replay lays them into
its own store before it asks: the log as the rows appended, ``memories``
and ``digest_records`` as the rows new or changed and the keys gone,
any other table in full, and none with its column list, which a replay
reads from its own file (the header holds each table's columns in the
opened store, which the replay's open must match); each answer (dicts as pairs, the average
as ``repr``, an integer past 64 bits as its decimal text under
``$int``); the memo's keys (the path under the case's root as
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

A step's ``plant`` holds log rows planted after the step's tables were
taken, as the statements that insert them (a run of ``home_delete`` rows
naming records no store holds, a payload that does not read): a replay
runs them after it lays the tables. Each memo entry's inputs are recorded
after the step (``entries``, in the memo's order): head, groups (scopes
as JSON text, home, actor, the positions set in the mask), lengths,
index, group keys, ``group_of``, ``length_of``, version and ``changed``,
in full up to ``ENTRY_INLINE`` bytes of JSON text and as that text's
SHA-256 past it. An entry the memo carried over a moved head
(`_advanced_inputs`) is compared with the reference's field for field.

Gates, ``store_tx.jsonl``'s and for the same reasons. Unicode: the index
text is the tokenizer's spelling and the digest's rows its tokens.
pydantic-core: the records are built through the models. POSIX only: the
create and open write and check the key ring's modes and binding. Not on
the Python version and not on the SQLite version: no text recorded is the
interpreter's or SQLite's.
"""

from __future__ import annotations

import hashlib
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
    if type(value) is int and not -(2**63) <= value < 2**63:
        # `int()` of a digest column past 64 bits, which a JSON reader
        # holds only as a float.
        return {"$int": str(value)}
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


#: The longest canonical entry text written out in full.
ENTRY_INLINE = 500


def _entry(inputs: Any) -> Any:
    """A memo entry's inputs in canonical form, or the SHA-256 of its JSON
    text where that is longer than `ENTRY_INLINE`."""

    def bits(mask: int) -> list[int]:
        return [p for p in range(mask.bit_length()) if mask >> p & 1]

    def listed(values: Any) -> Any:
        return None if values is None else list(values)

    groups = [
        [
            json.dumps(scopes, ensure_ascii=True, separators=(",", ":")),
            _cell(home),
            None if actor is None else [actor.client, actor.model],
            bits(mask),
        ]
        for scopes, home, actor, mask in inputs.groups
    ]
    form = [
        list(inputs.head),
        groups,
        # A group's total is a Python int, past 64 bits as `_cell` spells it.
        None if inputs.lengths is None else [_cell(n) for n in inputs.lengths],
        [[_cell(rid), position] for rid, position in sorted(inputs.index.items())],
        [[_cell(v) for v in key] for key in inputs.keys],
        listed(inputs.group_of),
        listed(inputs.length_of),
        inputs.version,
        [[version, _cell(rid), position] for version, rid, position in inputs.changed],
    ]
    text = json.dumps(form, ensure_ascii=True, separators=(",", ":"))
    if len(text) <= ENTRY_INLINE:
        return form
    return {"sha256": hashlib.sha256(text.encode("ascii")).hexdigest()}


def entries_view() -> list[Any]:
    return [_entry(entry) for entry in store_index._DOC_FREQS.values()]


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
            # Numbers past 64 bits in the digest (planted: an INTEGER
            # column holds a REAL past 2**63, a numeral text with
            # underscores stays text). `int()` reads them whole: `rows`
            # hands them on, the grouping keys a row number past 64 bits
            # and skips a length past them on a row whose scopes are not a
            # list, and `array('q')` refuses one on a row whose scopes are
            # (OverflowError), after an earlier row's refused actor
            # (ValidationError) and before a later row's scopes that are
            # not JSON. The step asked on another spelling reads afresh.
            "name": "digest_big_numbers",
            "steps": [
                {"writes": [put(n) for n in range(1, 6)], "asks": DIGEST[:1]},
                {
                    "sql": [
                        f"UPDATE digest_records SET rid = 1e20 WHERE id = '{ulid(1)}'",
                        "UPDATE digest_records SET rid = '18_446_744_073_709_551_617' "
                        f"WHERE id = '{ulid(2)}'",
                        "UPDATE digest_records SET scopes = '\"g\"', dl = -1e20 "
                        f"WHERE id = '{ulid(3)}'",
                    ],
                    "asks": DIGEST[1:] + ask("record"),
                },
                {
                    "conn": "spelled",
                    "sql": [
                        f"UPDATE digest_records SET dl = 1e20 WHERE id = '{ulid(4)}'"
                    ],
                    "asks": DIGEST[1:] + ask("record"),
                },
                {
                    "conn": "spelled",
                    "sql": [
                        f"UPDATE digest_records SET client = X'FF' WHERE id = '{ulid(2)}'"
                    ],
                    "asks": ask("record"),
                },
                {
                    "conn": "spelled",
                    "sql": [
                        f"UPDATE digest_records SET client = NULL WHERE id = '{ulid(2)}'",
                        f"UPDATE digest_records SET scopes = 'no' WHERE id = '{ulid(5)}'",
                    ],
                    "asks": DIGEST[1:] + ask("record"),
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
        *advance_cases(),
    ]


def _names(count: int, spread: int | None = None) -> list[Any]:
    """A run of planted ``home_delete`` rows naming records no store
    holds: `count` rows, the names repeating every `spread` rows."""
    number = "i" if spread is None else f"(i % {spread})"
    return [["home_delete", f"'{{\"record_id\": \"p' || {number} || '\"}}'", count]]


def _one(kind: str, payload: str) -> list[Any]:
    return [[kind, payload, 1]]


def advance_cases() -> list[dict[str, Any]]:
    """An entry kept at an older head, brought to the new one by the
    record rows the log names (`_advanced_inputs`), or read again."""
    three = [put(1), put(2, body="second record body"), put(3, scopes=["alpha"])]
    rid4 = f"'{ulid(4)}'"
    home_row = "(SELECT MAX(seq) FROM log WHERE kind = 'home_put')"
    return [
        {
            # Each write moves a row: in place, to a new position, to a
            # new group at the end (scopes, actor client and model, home),
            # back to a group the entry holds; the kept terms are patched.
            # Then the digest cannot be brought current on a reader and
            # under `query_only`: the advance's own `digest_mark` and the
            # read's both warn.
            "name": "advance_moves",
            "steps": [
                {"writes": three, "asks": ask("sqlite", "record")},
                {
                    "writes": [put(2, body="second record sqlite")],
                    "asks": ask("sqlite", "record"),
                },
                {"writes": [put(4)], "asks": ask("record")[:2]},
                {
                    "writes": [put(1, scopes=["beta"])],
                    "asks": ask("record", admit="scope:beta")[:2],
                },
                {
                    "writes": [put(2, actor={"client": "c9"})],
                    "asks": ask("record", admit="client:c9")[:2],
                },
                {
                    "writes": [put(3, actor={"model": "m9"}), put(4, scopes=["beta"])],
                    "asks": ask("record", "sqlite", admit="noactor")[:2],
                },
                {
                    "writes": [home(1, "proj")],
                    "asks": ask("record", admit="project:proj")[:2],
                },
                {"writes": [home_delete(1)], "asks": ask("record", admit="nohome")[:2]},
                {"writes": [put(5), put(5, body="fifth")], "asks": ask("fifth")[:2]},
                {"writes": [put(6)], "conn": "read_only", "asks": ask("record")[:2]},
                {"writes": [put(7)], "conn": "query_only", "asks": ask("record")[:2]},
            ],
        },
        {
            # A digest row's scopes stop being a list (planted, named by a
            # home row the digest does not follow): the row leaves every
            # group and stays in `changed`; a new row whose scopes are not
            # a list takes no position; then a list again.
            "name": "advance_scopes_not_list",
            "steps": [
                {"writes": three, "asks": ask("record")[:2]},
                {
                    "writes": [home(1, "proj")],
                    "edit": "sql:UPDATE digest_records SET scopes = '\"g\"' WHERE id = ?",
                    "asks": ask("record")[:2],
                },
                {"writes": [put(4)], "asks": DIGEST[:1]},
                {
                    "sql": [
                        f"UPDATE digest_records SET scopes = '{{}}' WHERE id = {rid4}"
                    ],
                    "asks": ask("record")[:2],
                },
                {"writes": [put(1, scopes=["gamma"])], "asks": ask("record")[:2]},
            ],
        },
        {
            # A memory_delete, then a rebuild row, in the span: read again.
            # Then the digest stops answering: an entry read without it
            # carries no lengths and is read again at the next head.
            "name": "advance_not_followed",
            "steps": [
                {"writes": three, "asks": ask("record")[:2]},
                {"writes": [delete(1)], "asks": ask("record")[:2]},
                {"writes": [put(4)], "edit": "rebuilt_row", "asks": ask("record")[:2]},
                {"writes": [put(5)], "asks": ask("record")[:2]},
                {"writes": [put(6)], "edit": "fingerprint", "asks": ask("record")[:2]},
                {"writes": [put(7)], "asks": ask("record")[:2]},
            ],
        },
        {
            # `_ADVANCE_ROWS`: the ids named and the entry's `changed`
            # together, 256 followed and 257 read again; a name repeated
            # counts once.
            "name": "advance_bound",
            "steps": [
                {"writes": three, "asks": ask("record")[:1]},
                {"writes": [put(1), put(2), put(3)], "asks": ask("record")[:1]},
                {"plant": _names(253), "asks": ask("record")[:1]},
                {"plant": _names(254), "asks": ask("record")[:1]},
                {"plant": _names(256), "asks": ask("record")[:1]},
                {"plant": _names(257), "asks": ask("record")[:1]},
                {"plant": _names(600, 256), "asks": ask("record")[:1]},
                {"plant": _names(600, 257), "asks": ask("record")[:1]},
            ],
        },
        {
            # The log's row at the entry's head (a home row, so the
            # digest's own row stays) carries another MAC, then is gone:
            # another history, read again.
            "name": "advance_head_row",
            "steps": [
                {"writes": [*three, home(1, "p")], "asks": ask("record")[:1]},
                {
                    "writes": [put(4)],
                    "plant": [f"UPDATE log SET mac = 'remade' WHERE seq = {home_row}"],
                    "asks": ask("record")[:1],
                },
                {"writes": [home(2, "q")], "asks": ask("record")[:1]},
                {
                    "writes": [put(5)],
                    "plant": [f"DELETE FROM log WHERE seq = {home_row}"],
                    "asks": ask("record")[:1],
                },
                {"writes": [put(6)], "asks": ask("record")[:1]},
            ],
        },
        {
            # Planted home rows whose payloads read or do not: an id that
            # is a number, a list, bytes of JSON, the record of a real row
            # named again; not JSON, no key, a value that is not a dict;
            # then an id holding a lone surrogate (UnicodeEncodeError) and
            # a payload that is not UTF-8 (OperationalError), both raised.
            "name": "advance_payloads",
            "steps": [
                {"writes": three, "asks": ask("record")[:1]},
                {
                    "plant": _one("home_delete", "'{\"record_id\": 7}'"),
                    "asks": ask("record")[:1],
                },
                {
                    "plant": _one("home_put", '\'{"home": {"record_id": [1, "a"]}}\''),
                    "asks": ask("record")[:1],
                },
                {
                    "plant": _one(
                        "home_delete", f'CAST(\'{{"record_id": "{ulid(2)}"}}\' AS BLOB)'
                    ),
                    "asks": ask("record")[:1],
                },
                {"plant": _one("home_put", "'no'"), "asks": ask("record")[:1]},
                {
                    "plant": _one("home_put", "'{\"home\": {}}'"),
                    "asks": ask("record")[:1],
                },
                {
                    "plant": _one("home_put", "'{\"home\": [1]}'"),
                    "asks": ask("record")[:1],
                },
                {"plant": _one("home_delete", "'[]'"), "asks": ask("record")[:1]},
                {"plant": _one("home_delete", "X'FF'"), "asks": ask("record")[:1]},
                {
                    "plant": _one("home_delete", '\'{"record_id": "\\ud800"}\''),
                    "asks": ask("record")[:1],
                },
                {"writes": [put(4)], "asks": ask("record")[:1]},
                {
                    "plant": _one("home_delete", "CAST(X'FF' AS TEXT)"),
                    "asks": ask("record")[:1],
                },
            ],
        },
        {
            # Numbers past 64 bits in the rows the log names (planted).
            # A length past them on a row whose scopes are not a list is
            # not read; a row number past them is a new key, so the record
            # counts at its old position and its new one, and a kept term
            # patched from it alone binds it (OverflowError), from it and
            # a lower row number does not. A length past them on a row
            # whose scopes are a list is OverflowError, after an earlier
            # row's refused actor (ValidationError). The row number past
            # 64 bits then named again keeps its position.
            "name": "advance_big_numbers",
            "steps": [
                {"writes": three, "asks": ask("record")[:2]},
                {
                    "writes": [home(1, "proj")],
                    "sql": [
                        "UPDATE digest_records SET scopes = '\"g\"', dl = 1e20 "
                        f"WHERE id = '{ulid(1)}'"
                    ],
                    "asks": ask("record")[:2],
                },
                {
                    "writes": [home(2, "proj")],
                    "sql": [
                        "UPDATE digest_records SET rid = '18_446_744_073_709_551_617' "
                        f"WHERE id = '{ulid(2)}'"
                    ],
                    "asks": ask("record")[:2],
                },
                {"writes": [home(3, "proj")], "asks": ask("record")[:2]},
                {"writes": [put(4)], "asks": ask("record")[:2]},
                {
                    "writes": [home(4, "q")],
                    "sql": [f"UPDATE digest_records SET dl = 1e20 WHERE id = {rid4}"],
                    "asks": ask("record")[:2],
                },
                {
                    "writes": [home(3, "q")],
                    "sql": [
                        f"UPDATE digest_records SET client = X'FF' WHERE id = '{ulid(3)}'"
                    ],
                    "asks": ask("record")[:2],
                },
                {
                    "writes": [home_delete(2)],
                    "sql": [
                        f"UPDATE digest_records SET client = NULL WHERE id = '{ulid(3)}'",
                        f"UPDATE digest_records SET dl = 5 WHERE id = {rid4}",
                    ],
                    "asks": ask("record")[:2],
                },
            ],
        },
        {
            # A digest row changed with no log row is not read by the
            # advance: it keeps its old group; a new memo key reads it.
            # Then the digest's join fails after the advance's
            # `digest_mark`: None with no warning, then the read's warning.
            "name": "advance_digest_unread",
            "steps": [
                {"writes": three, "asks": ask("record", admit="noactor")},
                {
                    "writes": [put(4)],
                    "edit": "sql:UPDATE digest_records SET client = 'zz' WHERE id = ?",
                    "asks": ask("record", admit="noactor"),
                },
                {"conn": "spelled", "asks": ask("record", admit="noactor")[:2]},
                {
                    "writes": [put(5)],
                    "sql": [
                        "DROP TABLE homes",
                        "CREATE VIEW homes AS SELECT * FROM nothing_here",
                    ],
                    "asks": ask("record")[:2],
                },
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


def _planted(conn: sqlite3.Connection, kind: str, payload: str, count: int) -> str:
    """The statement that appends `count` log rows of `kind` after the
    log's last row, each payload the SQL expression `payload` of the row's
    number ``i`` (1 up)."""
    base = conn.execute("SELECT MAX(seq) FROM log").fetchone()[0]
    return (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        f"WHERE i < {count}) INSERT INTO log (seq, ts, session, kind, payload, "
        f"prev_mac, mac) SELECT {base} + i, l.ts, l.session, '{kind}', {payload}, "
        f"l.mac, 'planted-' || ({base} + i) FROM n, log l WHERE l.seq = {base}"
    )


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    """The case as the reference answers it."""
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
                    planted = []
                    for item in step.get("plant", []):
                        if isinstance(item, str):
                            planted.append(item)
                        else:
                            planted.append(_planted(store.conn, *item))
                        store.conn.execute(planted[-1])
                    if planted:
                        store.conn.commit()
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
                if batch:
                    # The replay holds the log as the batch saw it when it
                    # asked: rows the batch wrote after (the `returned` rows
                    # its end writes) are laid with the next step.
                    last["log"] = changed.get("log", seen_before["log"])
                extra = {"sql": step["sql"]} if "sql" in step else {}
                if planted:
                    extra["plant"] = planted
                steps_out.append(
                    {
                        **extra,
                        "step": i,
                        "conn": step.get("conn", "plain"),
                        "tables": _appended(changed, seen_before),
                        "answers": answers,
                        "memo": memo_view(root),
                        "entries": entries_view(),
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


#: Tables whose first column is a unique key a row is laid by: recorded
#: as the rows new or changed since the last step's (``upsert``) and the
#: keys gone (``delete``).
KEYED = ("memories", "digest_records")


def _appended(changed: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
    """The log, which only grows, as the rows after the last step's; a
    keyed table as the rows it changed."""
    out = _log_appended(dict(changed), last)
    for table in KEYED:
        now, was = out.get(table), last.get(table)
        if now is None or was is None or now["columns"] != was["columns"]:
            continue
        old = {row[0]: row for row in was["rows"]}
        new = {row[0]: row for row in now["rows"]}
        if len(old) != len(was["rows"]) or len(new) != len(now["rows"]):
            continue
        out[table] = {
            "columns": now["columns"],
            "delete": [key for key in old if key not in new],
            "upsert": [row for row in now["rows"] if old.get(row[0]) != row],
        }
    # A replay reads a table's columns from its own file, whose schema is
    # this one's (a step's ``sql`` runs there first).
    return {t: {k: v for k, v in c.items() if k != "columns"} for t, c in out.items()}


def _log_appended(out: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
    """The log, which only grows, as the rows after the last step's."""
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
    # A table a step's edit changed is recorded as the batch saw it, even
    # where the batch's writes put it back as the last step left it.
    view = {t: v for t, v in seen.items() if last.get(t) != v or t in changed}
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


def opened_columns() -> dict[str, list[str]]:
    """Each table's columns in the store every case opens, which a replay
    checks its own open against: a step's rows are laid by position."""
    with tempfile.TemporaryDirectory() as raw:
        case = Path(raw) / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            Store.open(file, keys_dir=case / "keys", allow_rekey=True).close()
        return {table: content["columns"] for table, content in tables(file).items()}


#: The fixture's files and the first and last case each holds, in the
#: order `cases` lists them (each file under the 150 KB a golden file
#: keeps to).
FILES = {
    "store_docfreq_store": ("empty_store", "digest_client_bytes"),
    "store_docfreq_store_b": ("digest_rid_twice", "read_only"),
    "store_docfreq_advance": ("advance_moves", "advance_digest_unread"),
}


def _file(name: str) -> dict[str, bytes]:
    every = cases()
    names = [c["name"] for c in every]
    first, last = FILES[name]
    chosen = every[names.index(first) : names.index(last) + 1]
    with world():
        lines: list[dict[str, Any]] = [
            {
                "fixture": name,
                "engine_version": ENGINE,
                "store_id": STORE_ID,
                "columns": opened_columns(),
            }
        ]
        lines += [run_case(c) for c in chosen]
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {f"{name}.jsonl": text.encode("ascii")}


def generate() -> dict[str, bytes]:
    return _file("store_docfreq_store")


def generate_b() -> dict[str, bytes]:
    return _file("store_docfreq_store_b")


def generate_advance() -> dict[str, bytes]:
    return _file("store_docfreq_advance")
