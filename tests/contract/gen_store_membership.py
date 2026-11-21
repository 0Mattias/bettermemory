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
