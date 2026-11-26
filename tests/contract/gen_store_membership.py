"""Fixture: the `Store` methods of the membership tables
(``store_membership.jsonl``): `put_membership`, `delete_membership` (and
the `_clear_native` each runs), `record_membership`, `membership_rows`
(over legacy rows, native rows and batches past ``_PROVENANCE_BATCH``),
`next_membership_id`, `next_membership_number`, `committed_seq`,
`in_batch`, `wrote_record` and `membership_store_id`
(`membership_log` and `reissue_membership_store_id` are left to
``store_membership-2``).

The native build ports them (``native/``, `store::membership`) and is held
to this file. The cases run as ``store_records_b.jsonl``'s do
(`gen_store_records_b.run_case`: each step's log rows, clocks, state
digest and index recorded), with these calls:

- ``put``, ``delete``, ``record``: the writer, its returned row as
  ``[seq, kind]``;
- ``rows``, ``next_id``, ``next_number``, ``wrote``,
  ``store_id``, ``committed_seq``, ``in_batch``: the read;
- ``batch``: the inner calls inside one ``Store.batch()``, each inner
  answer in order;
- ``sql``: statements run and committed on the store's connection (native
  rows put in directly, meta changed);
- ``fill``: ``n`` rows put directly into ``project_remotes``
  (``project`` ``P00<i % 7>``, ``remote_key`` ``r<i:05d>``), for reads
  past one batch.

Every answer is recorded as ``encoded_json`` text (sorted keys, ASCII,
compact), so a lone surrogate survives; an answer longer than 40 rows is
recorded as its row count and the SHA-256 of that text. An argument that
holds a lone surrogate is given as ``{"$json": <its encoded_json>}``. A
surrogate's native write, which the native build refuses, is not run
here (its log row would spell a JSON escape four backslashes deep, which
`host_fact` reads as a host path); `store_membership_kills.rs` pins the
refusal.

Gates, the same as ``store_records_b.jsonl``'s: the state digest's index
text is the tokenizer's (Unicode), a refusal's class may be pydantic's,
and the stores are POSIX files. A RuntimeError naming the store's path
spells the path ``<store>``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from typing import Any

from bettermemory.native_text import encoded_json
from bettermemory.store import Store

from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS
from .gen_store_records_b import run_case
from .gen_store_write import _refusal

LONG = 40


def arg(v: Any) -> Any:
    if isinstance(v, dict) and set(v) == {"$json"}:
        return json.loads(v["$json"])
    return v


def native(text: Any) -> dict[str, str]:
    return {"$json": encoded_json(text)}


def answer(value: Any) -> Any:
    if isinstance(value, list) and len(value) > LONG:
        text = encoded_json(value)
        return {
            "count": len(value),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return encoded_json(value)


def _row(row: Any) -> list[Any]:
    return [row.seq, row.kind]


def _one(store: Store, call: dict[str, Any]) -> Any:
    what = call["call"]
    session = call.get("session")
    if what == "put":
        return _row(
            store.put_membership(call["kind"], arg(call["row"]), session=session)
        )
    if what == "delete":
        return _row(
            store.delete_membership(call["kind"], arg(call["key"]), session=session)
        )
    if what == "record":
        return _row(
            store.record_membership(call["kind"], arg(call["payload"]), session=session)
        )
    if what == "rows":
        column_in = call.get("column_in")
        return store.membership_rows(
            call["table"],
            where=arg(call.get("where")),
            column_in=None if column_in is None else (column_in[0], arg(column_in[1])),
            order_by=call.get("order_by", ()),
        )
    if what == "next_id":
        return store.next_membership_id(call["table"], call["prefix"])
    if what == "next_number":
        return store.next_membership_number(call["table"], call["column"])
    if what == "committed_seq":
        return store.committed_seq()
    if what == "in_batch":
        return store.in_batch
    if what == "wrote":
        return store.wrote_record(call["id"])
    if what == "store_id":
        return store.membership_store_id
    if what == "sql":
        for statement, params in call["statements"]:
            store._conn.execute(statement, [arg(p) for p in params])
        store._conn.commit()
        return None
    if what == "fill":
        store._conn.executemany(
            "INSERT INTO project_remotes(project, remote_key) VALUES (?, ?)",
            [(f"P00{i % 7}", f"r{i:05d}") for i in range(call["n"])],
        )
        store._conn.commit()
        return None
    raise AssertionError(what)


def _run(store: Store, call: dict[str, Any]) -> Any:
    if call["call"] == "batch":
        out: list[Any] = []
        with ExitStack() as stack:
            stack.enter_context(store.batch())
            for inner in call["steps"]:
                out.append(answer(_one(store, inner)))
        return out
    return answer(_one(store, call))


def scrubbed(exc: BaseException, step: dict[str, Any]) -> dict[str, Any]:
    """`_refusal`, a store's path (a temporary one) spelled ``<store>``."""
    out = _refusal(exc)
    text = out["text"]
    if isinstance(text, str) and text.startswith("store ") and " carries " in text:
        out["text"] = "store <store> carries" + text.split(" carries", 1)[1]
    return out


def put(kind: str, row: Any, **kw: Any) -> dict[str, Any]:
    return {"call": "put", "kind": kind, "row": row, **kw}


def delete(kind: str, key: Any, **kw: Any) -> dict[str, Any]:
    return {"call": "delete", "kind": kind, "key": key, **kw}


def record(kind: str, payload: Any, **kw: Any) -> dict[str, Any]:
    return {"call": "record", "kind": kind, "payload": payload, **kw}


def rows(table: str, **kw: Any) -> dict[str, Any]:
    return {"call": "rows", "table": table, **kw}


def call(what: str, **kw: Any) -> dict[str, Any]:
    return {"call": what, **kw}


def sql(*statements: tuple[str, list[Any]]) -> dict[str, Any]:
    return {"call": "sql", "statements": [list(s) for s in statements]}


def machine(i: Any, **kw: Any) -> dict[str, Any]:
    return {
        "id": i,
        "facts_json": {"os": "x"},
        "predecessor": None,
        "minted": "t",
        **kw,
    }


def remote(project: str, key: str) -> dict[str, Any]:
    return {"project": project, "remote_key": key}


def witness(project: str, commit: str, arrival: Any) -> dict[str, Any]:
    return {
        "project": project,
        "commit_id": commit,
        "arrival": arrival,
        "source": "s",
        "fingerprint": None,
    }


def native_row(kind: str, key: list[Any], row: Any) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO native_membership_rows(member_kind, key_text, row_text) "
        "VALUES (?, ?, ?)",
        [kind, encoded_json(key), encoded_json(row)],
    )


HOME = {
    "record_id": "R1",
    "kind": "memory",
    "project": "global",
    "checkout": None,
    "rule": "fallback",
    "decided": "t",
}
SUR = "r\udc80"


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "writes",
            "steps": [
                call("committed_seq"),
                call("in_batch"),
                call("store_id"),
                put("machine", machine("M001")),
                put("project_remote", remote("P001", "r1"), session="s1"),
                put("project_remote", remote("P001", "r1")),
                put("nope", {}),
                put("machine", machine("M002", extra=1, zeta=2)),
                put("machine", {"id": "M003"}),
                put("machine", machine("M004", minted=1.5)),
                put("machine", machine("")),
                put("machine", machine(7)),
                put("machine", machine("M005", minted=True)),
                put("project_witness", witness("P001", "c1", 3)),
                put("project_witness", witness("P001", "c2", 9)),
                delete("project_remote", remote("P001", "r1")),
                delete("project_remote", {"project": "P001"}),
                delete("project_remote", {**remote("P001", "r9"), "x": 1}),
                delete("project_remote", remote("P001", "")),
                delete("nope", {}),
                record("checkout_registered", {"checkout": "C1", "n": 1}),
                record("store_forked", {"checkout": "C2"}, session="s2"),
                record("machine_put", {}),
                record("nope", {}),
                rows("machines"),
                rows("machines", where={"predecessor": None}, order_by=["-id"]),
                rows("project_witnesses", order_by=["--arrival", "commit_id"]),
                rows("project_witnesses", where={"arrival": 9}),
                rows("project_witnesses", column_in=["commit_id", ["c2", "c1", "c2"]]),
                rows("project_witnesses", column_in=["commit_id", []]),
                rows("project_witnesses", column_in=["commit_id", [["c1"]]]),
                rows("project_witnesses", where={"nope": 1}),
                rows("project_witnesses", order_by=["-nope"]),
                rows("project_witnesses", column_in=["nope", ["x"]]),
                rows("nope"),
                call("next_id", table="machines", prefix="M"),
                call("next_id", table="projects", prefix="P"),
                call("next_id", table="machines", prefix="_"),
                call("next_id", table="project_remotes", prefix="P"),
                call("next_id", table="nope", prefix="P"),
                call("next_number", table="project_witnesses", column="arrival"),
                call("next_number", table="checkout_views", column="seen_order"),
                call("next_number", table="project_witnesses", column="nope"),
                call("next_number", table="nope", column="x"),
                call("next_number", table="machines", column="id"),
                call("wrote", id="R1"),
                put("home", HOME),
                call("wrote", id="R1"),
                call("wrote", id="R2"),
                call("store_id"),
                sql(("DELETE FROM meta WHERE key = 'membership_store_id'", [])),
                call("store_id"),
            ],
        },
        {
            "name": "batches",
            "steps": [
                call(
                    "batch",
                    steps=[
                        call("committed_seq"),
                        call("in_batch"),
                        put("project_remote", remote("P001", "r1")),
                        call("committed_seq"),
                        record("proposal_opened", {"checkout": "C1"}),
                        call("committed_seq"),
                        call("next_id", table="machines", prefix="M"),
                        put("machine", machine("M001")),
                        call("next_id", table="machines", prefix="M"),
                        call("store_id"),
                    ],
                ),
                call("committed_seq"),
                call(
                    "batch",
                    steps=[
                        put("project_remote", remote("P002", "r2")),
                        put("project_remote", remote("P002", "")),
                    ],
                ),
                rows("project_remotes"),
                call("fill", n=1100),
                rows(
                    "project_remotes",
                    column_in=["remote_key", [f"r{i:05d}" for i in range(0, 1300, 1)]],
                ),
                rows(
                    "project_remotes",
                    column_in=[
                        "remote_key",
                        [f"r{i:05d}" for i in range(1299, -1, -2)],
                    ],
                    order_by=["-project", "remote_key"],
                ),
                rows(
                    "project_remotes",
                    where={"project": "P003"},
                    column_in=["remote_key", [f"r{i:05d}" for i in range(0, 1100, 3)]],
                    order_by=["-remote_key"],
                ),
                rows(
                    "project_remotes",
                    where={"project": "P003"},
                    column_in=["remote_key", [f"r{i:05d}" for i in range(0, 400)]],
                ),
                rows(
                    "project_remotes",
                    column_in=["remote_key", [f"r{i:05d}" for i in range(0, 1100, 50)]],
                ),
            ],
        },
        {
            "name": "native",
            "steps": [
                put("project_remote", remote("P001", "r1")),
                put("project_remote", remote("P001", "r2")),
                put("project_witness", witness("P001", "c1", 3)),
                sql(
                    native_row(
                        "project_remote",
                        ["P001", "r2"],
                        {"project": "P001", "remote_key": "r2"},
                    ),
                    native_row("project_remote", ["P001", SUR], remote("P001", SUR)),
                    native_row("project_remote", ["P009", "r0"], remote("P009", "r0")),
                    native_row(
                        "project_remote", ["P009", "wrong"], remote("P009", "r5")
                    ),
                    (
                        "INSERT INTO native_membership_rows VALUES ('project_remote', 'k1', '[1]')",
                        [],
                    ),
                    (
                        "INSERT INTO native_membership_rows VALUES ('project_remote', 'k2', '{')",
                        [],
                    ),
                    native_row(
                        "project_witness", ["P001", "c7"], witness("P001", "c7", "12")
                    ),
                    native_row(
                        "project_witness", ["P001", "c8"], witness("P001", "c8", 5.5)
                    ),
                ),
                rows("project_remotes"),
                rows("project_remotes", order_by=["-remote_key"]),
                rows("project_remotes", where={"project": "P009"}),
                rows(
                    "project_remotes",
                    column_in=["remote_key", native(["r2", SUR, "r0"])],
                ),
                rows("project_remotes", column_in=["remote_key", native([SUR])]),
                rows("project_remotes", where=native({"remote_key": SUR})),
                rows("project_witnesses"),
                call("next_number", table="project_witnesses", column="arrival"),
                put("project_remote", remote("P001", "r2"), session="s"),
                rows("project_remotes"),
                delete("project_remote", remote("P009", "r0")),
                sql(native_row("project_remote", ["P001", "r1"], remote("P001", "r1"))),
                sql(
                    (
                        "UPDATE meta SET value = '4' WHERE key = 'min_writer_revision'",
                        [],
                    )
                ),
                put("project_remote", remote("P001", "r1")),
                delete("project_remote", remote("P001", "r1")),
                put("project_remote", native(remote("P001", SUR))),
                sql(
                    (
                        "UPDATE meta SET value = '5' WHERE key = 'min_writer_revision'",
                        [],
                    )
                ),
                sql(
                    native_row(
                        "project_witness", ["P001", "c9"], witness("P001", "c9", "x")
                    )
                ),
                call("next_number", table="project_witnesses", column="arrival"),
            ],
        },
    ]


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_membership",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c, _run, scrubbed) for c in cases()]
    opened = runs[0]["opened"]
    for run in runs:
        if run.pop("opened") != opened:
            raise AssertionError(f"{run['case']} opened another state")
    lines[0]["opened"] = opened
    lines += runs
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_membership.jsonl": text.encode("ascii")}


# ---------------------------------------------------------------------------
# store_membership_native.jsonl: unit store_membership-2.
#
# The surrogate branches of `put_membership` and `delete_membership`
# (`store_native.legacy_row` and the `native_member` rows), `membership_log`
# and `reissue_membership_store_id`, run as `generate`'s cases are, with
# these calls beside its own:
#
# - ``log``: `membership_log(kinds, since_seq=, checkout=)` read to the
#   end (an absent ``kinds`` is None; ``since_seq`` defaults to 0),
#   recorded as the rows' sequence numbers and the SHA-256 of the
#   `encoded_json` text of the rows, each ``[seq, ts, session, kind,
#   payload, prev_mac, mac]``;
# - ``reissue``: `reissue_membership_store_id(record_id=, source=,
#   session=)` with `secrets.token_hex` scripted to answer ``token``;
# - ``table``: every row of a table as SQLite holds it, in rowid order
#   (past `TABLE_ROWS` rows, their count and the SHA-256 of their
#   `encoded_json` text).
#
# A text the file would otherwise spell with four backslashes in a row (a
# native row's escape inside a payload, inside the line's JSON) trips the
# golden directory's `host_fact` guard, so an answer or a log row's payload
# whose text holds two backslashes in a row is written as ``{"hex": <its
# UTF-8 bytes>}``.


def spelled(text: Any) -> Any:
    """`text`, or its UTF-8 bytes as hex where it holds two backslashes
    in a row."""
    if isinstance(text, str) and "\\\\" in text:
        return {"hex": text.encode("utf-8").hex()}
    return text


class _Tokens:
    """`secrets` as `store_membership` reads it: `token_hex` answers the
    step's scripted token."""

    def __init__(self) -> None:
        self.queue: list[str] = []

    def token_hex(self, nbytes: int | None = None) -> str:
        if nbytes != 16 or not self.queue:
            raise AssertionError("an unscripted token")
        return self.queue.pop(0)


TOKENS = _Tokens()


def _native_one(store: Store, call: dict[str, Any]) -> Any:
    what = call["call"]
    if what == "log":
        kinds = call.get("kinds")
        rows = store.membership_log(
            None if kinds is None else arg(kinds),
            since_seq=call.get("since_seq", 0),
            checkout=arg(call.get("checkout")),
        )
        return Logged([list(row) for row in rows])
    if what == "reissue":
        TOKENS.queue.append(call["token"])
        return list(
            store.reissue_membership_store_id(
                record_id=call["record"],
                source=call["source"],
                session=call.get("session"),
            )
        )
    if what == "table":
        return Table(
            list(r)
            for r in store._conn.execute(
                f"SELECT * FROM {call['table']} ORDER BY rowid"
            )
        )
    return _one(store, call)


class Logged(list[Any]):
    """`membership_log`'s rows, recorded by `native_answer`."""


class Table(list[Any]):
    """A table's rows, recorded by `native_answer`."""


#: A table of more rows than this is recorded as its count and digest.
TABLE_ROWS = 8


def native_answer(value: Any) -> Any:
    """`answer`, `spelled`; `membership_log`'s rows as their sequence
    numbers and the SHA-256 of their `encoded_json` text (each row is
    in the step's own log rows already); a long `Table` as its count and
    digest."""
    if isinstance(value, Logged):
        text = encoded_json(list(value))
        return {
            "seqs": [row[0] for row in value],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    if isinstance(value, Table) and len(value) > TABLE_ROWS:
        text = encoded_json(list(value))
        return {
            "count": len(value),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return spelled(answer(list(value) if isinstance(value, Table) else value))


def _native_run(store: Store, call: dict[str, Any]) -> Any:
    if call["call"] == "batch":
        out: list[Any] = []
        with ExitStack() as stack:
            stack.enter_context(store.batch())
            for inner in call["steps"]:
                out.append(native_answer(_native_one(store, inner)))
        return out
    return native_answer(_native_one(store, call))


def native_cases() -> list[dict[str, Any]]:
    s = SUR
    m = machine
    project = {
        "id": "P001",
        "name": "alpha",
        "merged_into": None,
        "provisional": None,
        "parent": None,
        "tenant": None,
        "folder": 0,
        "boundary": 0,
        "minted": "t",
    }
    checkout = {
        "id": "C001",
        "project": "P001",
        "machine": "M001",
        "kind": "main",
        "object_device": "1",
        "object_inode": "2",
        "object_birth": None,
        "common_device": "1",
        "common_inode": "2",
        "common_birth": None,
        "git_dir": "g",
        "token": "k",
        "token_status": "ok",
        "stamps_json": ["a"],
        "last_head": None,
        "branch": "main",
        "remotes_json": ["r"],
        "manifest_json": None,
        "legacy": 0,
        "retired": 0,
        "registered": "t",
    }
    view = {
        "checkout": "C001",
        "machine": "M001",
        "path": "p",
        "first_seen": "t",
        "last_seen": "t",
        "seen_order": 1,
    }
    proposal = {
        "id": "Q001",
        "checkout": "C001",
        "kind": "join",
        "candidates_json": ["P001"],
        "evidence_json": {"e": 1},
        "reason": "because",
        "line": "a line",
        "state": "open",
        "answer": None,
        "answered_by": None,
        "signature_json": None,
        "opened": "t",
    }
    standing = {
        "record_id": "R1",
        "probability": 0.5,
        "body_sha256": "ab",
        "source": "s",
        "answered_by": "u",
        "answered": "t",
    }
    blob = {"project": "P001", "blob": "b1", "witness": "c1"}
    file = {"file_id": "F1", "project": "P001", "evidence": "e"}
    declared = {"remote_key": "k1", "project": "P001", "principal": "u"}
    returned = {"record_id": "R1", "via": "recall", "returned": "t"}
    member = {"member_kind": "machine", "key_text": "x", "row_text": "{}"}

    def n(row: dict[str, Any], **kw: Any) -> dict[str, str]:
        return native({**row, **kw})

    return [
        {
            "name": "native_writes",
            "steps": [
                # A surrogate in a key column: the native row alone.
                put("machine", n(m(f"M{s}")), session="s1"),
                # Each kind of non-key column: a `_json` dict, list and
                # str, a nullable column, a `name`, `line` and `reason`.
                put("machine", n(m("M001", facts_json={"os": s}))),
                put("machine", n(m("M002", facts_json=[s]), predecessor=s)),
                put("machine", n(m("M003", minted=s))),
                put("project", n(project, name=s)),
                put("project", n(project, id=f"P{s}")),
                put("project", n(project, id="P002", merged_into=s, tenant=s)),
                put("project_witness", n(witness("P001", s, 1))),
                put("project_witness", n(witness("P001", "c1", 2), fingerprint=s)),
                put("project_witness", n(witness("P001", "c2", 3), source=s)),
                put("project_blob", n(blob, blob=s)),
                put("project_blob", n(blob, witness=s)),
                put("project_remote", n(remote(s, "r1"))),
                put("project_file", n(file, file_id=s)),
                put("project_file", n(file, evidence=s)),
                put("declared_remote", n(declared, remote_key=s)),
                put("declared_remote", n(declared, principal=s)),
                put("checkout", n(checkout, id=s)),
                put(
                    "checkout",
                    n(
                        checkout,
                        stamps_json=[s],
                        remotes_json=(s,),
                        manifest_json=s,
                        git_dir=s,
                        unresolved_json={s: 1},
                    ),
                ),
                put("checkout", n(checkout, id="C002", branch=s, registered=s)),
                put("checkout_view", n(view, path=s)),
                put("checkout_view", n(view, path="q", first_seen=s)),
                put("home", n(HOME, record_id=s)),
                put("home", n(HOME, checkout=s)),
                put("proposal", n(proposal, id=s)),
                put(
                    "proposal",
                    n(
                        proposal,
                        candidates_json=[s],
                        evidence_json={"e": s},
                        signature_json=s,
                        reason=f"why {s}",
                        line=f"é{s}{s}",
                        answer=s,
                    ),
                ),
                put("standing", n(standing, record_id=s)),
                put("standing", n(standing, source=s)),
                put("returned", n(returned, record_id=s)),
                put("returned", n(returned, via=s)),
                put("native_member", n(member, key_text=s)),
                put("native_member", n(member, row_text=s)),
                # Checked before the transaction: a bad row writes nothing.
                put("machine", n(m(f"M{s}"), extra=s)),
                put("machine", n(m(f"M{s}"), minted=1.5)),
                {"call": "table", "table": "native_membership_rows"},
                {"call": "table", "table": "machines"},
                {"call": "table", "table": "proposals"},
                {"call": "table", "table": "checkouts"},
                rows("machines"),
                rows("proposals"),
            ],
        },
        {
            "name": "native_twins",
            "steps": [
                put("machine", n(m("M001", facts_json={"os": s}))),
                rows("machines"),
                # A later plain put clears the native twin.
                put("machine", m("M001"), session="s2"),
                {"call": "table", "table": "native_membership_rows"},
                put("project_remote", n(remote("P001", s))),
                put("project_remote", remote("P001", "r1")),
                # A surrogate delete: the native row's delete alone.
                delete("project_remote", n(remote("P001", s)), session="s3"),
                delete("project_remote", n(remote("P001", s))),
                delete("machine", n({"id": f"M{s}"})),
                # Only the key's columns are read.
                delete("project_remote", n(remote("P001", "r1"), extra=s)),
                {"call": "table", "table": "native_membership_rows"},
                {"call": "table", "table": "project_remotes"},
                # A store held to older writers refuses the native write
                # inside the transaction, after the row's own checks.
                sql(
                    (
                        "UPDATE meta SET value = '4' WHERE key = 'min_writer_revision'",
                        [],
                    )
                ),
                put("project_remote", n(remote("P001", s))),
                delete("project_remote", n(remote("P001", s))),
                put("machine", n(m(f"M{s}"), extra=1)),
                sql(("DELETE FROM meta WHERE key = 'min_writer_revision'", [])),
                put("project_remote", n(remote("P001", s))),
                sql(
                    (
                        "INSERT INTO meta(key, value) VALUES ('min_writer_revision', '5')",
                        [],
                    )
                ),
                # Inside a batch: a refused legacy put takes back only its
                # own call.
                call(
                    "batch",
                    steps=[
                        put("machine", n(m("M009", facts_json=[s]))),
                        put("project_remote", n(remote("P003", s))),
                    ],
                ),
                call(
                    "batch",
                    steps=[
                        put("project_remote", n(remote("P004", s))),
                        put("machine", n(m("M008", minted=s))),
                    ],
                ),
                rows("project_remotes"),
            ],
        },
        {
            "name": "log",
            "steps": [
                put("machine", m("M001"), session="s1"),
                record("checkout_registered", {"checkout": "C1"}, session="s2"),
                record("checkout_moved", {"checkout": 5}),
                record("checkout_retired", {"checkout": True}),
                record("checkout_rekeyed", {"checkout": {"a": 1}}),
                record("proposal_opened", {"checkout": None}),
                record("store_forked", {"checkout": 1.0}),
                record("witness_put", {"checkout": "c1"}),
                record("project_minted", {"other": "C1"}),
                put("project_remote", n(remote("P001", s)), session="s4"),
                delete("machine", {"id": "M001"}),
                {"call": "log"},
                {"call": "log", "kinds": []},
                {"call": "log", "kinds": ["memory_put", "store_created", "nope"]},
                {
                    "call": "log",
                    "kinds": [
                        "witness_put",
                        5,
                        "machine_put",
                        "memory_put",
                        "native_member_put",
                        "checkout_moved",
                        None,
                        "machine_put",
                    ],
                },
                {"call": "log", "kinds": ["machine_put", ["x"]]},
                {"call": "log", "kinds": [{"x": 1}]},
                {"call": "log", "since_seq": -1, "kinds": ["machine_put"]},
                {"call": "log", "since_seq": 4},
                {"call": "log", "since_seq": 5},
                {"call": "log", "since_seq": 11},
                {"call": "log", "since_seq": 12},
                {"call": "log", "since_seq": True, "kinds": ["machine_put"]},
                {"call": "log", "since_seq": 9223372036854775807},
                {"call": "log", "checkout": "C1"},
                {"call": "log", "checkout": "c1"},
                {"call": "log", "checkout": 5},
                {"call": "log", "checkout": "5"},
                {"call": "log", "checkout": True},
                {"call": "log", "checkout": 1},
                {"call": "log", "checkout": 1.0},
                {"call": "log", "checkout": '{"a":1}'},
                {"call": "log", "checkout": ""},
                {"call": "log", "checkout": "C1", "kinds": ["checkout_moved"]},
                {"call": "log", "checkout": "C1", "since_seq": 6},
                {"call": "log", "checkout": ["C1"]},
                {"call": "log", "checkout": "C1", "kinds": []},
                call(
                    "batch",
                    steps=[
                        record("checkout_registered", {"checkout": "C1"}),
                        {"call": "log", "checkout": "C1", "since_seq": 10},
                    ],
                ),
                # A payload that is not JSON names no checkout.
                sql(
                    (
                        "INSERT INTO log(ts, session, kind, payload, prev_mac, mac) "
                        "VALUES ('t', NULL, 'checkout_registered', 'not json', 'p', 'm')",
                        [],
                    ),
                    (
                        "INSERT INTO log(ts, session, kind, payload, prev_mac, mac) "
                        "VALUES ('t', 's', 'checkout_moved', "
                        "'{\"checkout\":\"C1\"', 'p', 'm')",
                        [],
                    ),
                ),
                {"call": "log", "since_seq": 12},
                {"call": "log", "since_seq": 12, "checkout": "C1"},
            ],
        },
        {
            "name": "reissue",
            "steps": [
                call("store_id"),
                {
                    "call": "reissue",
                    "record": "R1",
                    "source": "import",
                    "token": "a" * 32,
                    "session": "s1",
                },
                call("store_id"),
                {"call": "reissue", "record": "R2", "source": "", "token": "b" * 32},
                call(
                    "batch",
                    steps=[
                        put("machine", m("M001")),
                        {
                            "call": "reissue",
                            "record": "R3",
                            "source": "sync",
                            "token": "c" * 32,
                        },
                        call("store_id"),
                    ],
                ),
                {"call": "log", "kinds": ["store_forked"]},
                sql(("DELETE FROM meta WHERE key = 'membership_store_id'", [])),
                {"call": "reissue", "record": "R4", "source": "s", "token": "d" * 32},
                call(
                    "batch",
                    steps=[
                        put("machine", m("M002")),
                        {
                            "call": "reissue",
                            "record": "R5",
                            "source": "s",
                            "token": "e" * 32,
                        },
                    ],
                ),
                {"call": "table", "table": "meta"},
            ],
        },
    ]


def _native_rows(run: dict[str, Any]) -> dict[str, Any]:
    for step in run["steps"]:
        for row in step["rows"]:
            row[4] = spelled(row[4])
    return run


def generate_native() -> dict[str, bytes]:
    from unittest.mock import patch

    from bettermemory import store_membership

    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_membership_native",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world(), patch.object(store_membership, "secrets", TOKENS):
        runs = [
            _native_rows(run_case(c, _native_run, scrubbed)) for c in native_cases()
        ]
    if TOKENS.queue:
        raise AssertionError(f"tokens left over: {TOKENS.queue}")
    opened = runs[0]["opened"]
    for run in runs:
        if run.pop("opened") != opened:
            raise AssertionError(f"{run['case']} opened another state")
    lines[0]["opened"] = opened
    lines += runs
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_membership_native.jsonl": text.encode("ascii")}
