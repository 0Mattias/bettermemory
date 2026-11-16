"""Fixture: write transactions of several mutations, with nested scopes
and the ``returned`` marks their commits settle (``store_tx.jsonl``).

The native build runs record writes in a transaction (``native/``,
`store::tx`, `Transaction`) and is held to this file. Each case starts
from the store `Store.create` makes under ``store_create.json``'s fixed
values, opened as the owner (as ``store_write.jsonl``'s are), and runs its
steps: an edit between transactions (the journal mode, the ``returned``
table dropped), or a transaction. A transaction is ``store._transaction()``
around a list of items, each one of: a mutation (``store._mutate``); a
nested scope (``with store._transaction():`` around items, optionally
failing at its end), whose failure the driver catches at the scope, so the
outer transaction goes on; a failure (an exception raised there, which
fails the scope it is in, or the whole transaction at the outermost
level); ``store.release_returned``; or a record named for the commit's
settle directly (``_tx_returns.setdefault``, as ``_put_restored`` names
it). Every clock and mint the writers read is fixed (`_fixed.fixed`), the
log clock and the wall clock apart: each transaction records the stamps
each clock handed out, so a replay reads them in the same order and as
often.

After each transaction: the outcome (committed, or the refusal's class
and text), the errors each failed scope returned, and, as
``store_write.jsonl`` records after each step, the rows appended (every
column), the state's digest (every table, meta, the key ring's texts and
modes), the head's text and the FTS5 index as queries see it; the opened
store's state, the same for every case, once in the header. A
transaction the native unit refuses (``not_ported:<class>``) records how
many rows the reference wrote instead.

Gates, the same as ``store_write.jsonl``'s and for the same reasons.
Unicode: the index text is the tokenizer's spelling and the digest's rows
its tokens. pydantic-core: a refused payload's text is the first line of a
ValidationError, and the models decide what a payload holds. POSIX only:
the state includes modes and the binding's resolved path. Not on the
Python version: every text compared is the product's own wording or
pydantic's first line. Not on the SQLite version: FTS5's shadow tables are
not compared, only what a ``MATCH`` returns.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.models import Memory
from bettermemory.store import Store

from ._fixed import Ticker, fixed, ulid_mint
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS, _engine, _made, state
from .gen_store_write import (
    _last_seq,
    _refusal,
    fts_view,
    log_rows,
    memory,
    state_digest,
    ulid,
)

#: The log clock's first reading in transaction k is LOG0 + k minutes; each
#: reading after it a second and a microsecond later.
LOG0 = datetime(2001, 2, 3, 5, 0, 0, 111111, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1, microseconds=1)
#: The wall clock: another day, another step, so a stamp says which.
WALL0 = datetime(2003, 4, 5, 6, 7, 8, 999000, tzinfo=timezone.utc)
WALL_STEP = timedelta(minutes=7, microseconds=3)


class Injected(Exception):
    """The failure a case raises at a point it names."""


def _a(n: int) -> str:
    return ulid(n)


def put(n: int, sup: list[int] | None = None, body: str | None = None, s: str = "s1"):
    links = [{"type": "supersedes", "target_id": _a(t)} for t in sup or []]
    return {
        "op": "memory_put",
        "payload": memory(n, body=body, links=links),
        "session": s,
    }


def delete(n: int, s: str = "s1") -> dict[str, Any]:
    return {"op": "memory_delete", "payload": {"id": _a(n)}, "session": s}


def scope(*items: dict[str, Any], fail: bool = False) -> dict[str, Any]:
    return {"scope": list(items), "fail": fail}


FAIL: dict[str, Any] = {"fail": True}


def release(n: int, s: str = "s1") -> dict[str, Any]:
    return {"release": _a(n), "session": s}


def name(n: int, via: int, s: str = "s1") -> dict[str, Any]:
    return {"name": _a(n), "via": _a(via), "session": s}


def tx(*items: dict[str, Any], native: str = "same") -> dict[str, Any]:
    return {"tx": list(items), "native": native}


def _failing_at(steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """`steps` in a nested scope failing before the first, the middle and
    the last step (after the ones before it ran)."""
    out = []
    for at in sorted({0, len(steps) // 2, len(steps) - 1}):
        out.append([*steps[:at], FAIL, *steps[at:]])
    return out


def cases() -> list[dict[str, Any]]:
    """Each: name, steps (edits and transactions)."""
    out: list[dict[str, Any]] = [
        {"name": "zero", "steps": [tx(), tx(put(1)), tx()]},
        {
            # settle: put a mark (a removal brings a record back), a
            # deletion as removed (the marked record removed).
            "name": "mark_then_removed",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(delete(2)),
                tx(delete(1)),
            ],
        },
        {
            # settle: a deletion as superseded (a put supersedes a marked
            # record again), then marked again when that link goes.
            "name": "mark_then_superseded",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(delete(2, s="s2")),
                tx(put(3, [1])),
                tx(put(3, [], body="link dropped")),
            ],
        },
        {
            # A put that drops one link and adds another: one record back,
            # one marked one out of force again, in one flush (the put,
            # then the deletion).
            "name": "relinked",
            "steps": [
                tx(put(1), put(2), put(3, [1])),
                tx(put(4, [2]), delete(4)),
                tx(put(3, [2], body="now supersedes two")),
            ],
        },
        {
            # Several marks in one flush, in record-id order, whatever order
            # the links were named in.
            "name": "many_marks",
            "steps": [
                tx(put(5), put(3), put(4), put(9, [5, 3, 4])),
                tx(delete(9)),
                tx(put(10, [3, 4, 5]), put(11, [5])),
                tx(delete(10), put(11, [], body="no link")),
            ],
        },
        {
            # A chain C -> B -> A: removing C brings B back, A stays under B.
            "name": "chain",
            "steps": [
                tx(put(1), put(2, [1]), put(3, [2])),
                tx(delete(3)),
                tx(delete(2)),
            ],
        },
        {
            # A diamond: D supersedes B and C, both supersede A.
            "name": "diamond",
            "steps": [
                tx(put(1), put(2, [1]), put(3, [1]), put(4, [2, 3])),
                tx(delete(4)),
                tx(delete(2)),
                tx(delete(3)),
            ],
        },
        {
            # Already marked: named again while still in force, nothing.
            # A record named twice in one transaction keeps the first
            # naming's via and session.
            "name": "named_twice",
            "steps": [
                tx(put(1), put(2, [1]), put(3, [1])),
                tx(delete(2, s="first"), delete(3, s="second")),
                tx(
                    put(4, [1], s="third"), put(4, [], body="dropped again", s="fourth")
                ),
                tx(put(5), put(6, [5]), name(5, 6, s="named"), delete(6, s="deleted")),
            ],
        },
        {
            # A candidate that ends the transaction where it started: a link
            # added and dropped again, a record removed and put back.
            "name": "where_it_started",
            "steps": [
                tx(put(1), put(2)),
                tx(put(3, [1]), put(3, [], body="gone again")),
                tx(put(4, [2]), delete(4)),
                tx(put(5, [2]), delete(5), put(5, [2])),
            ],
        },
        {
            "name": "release",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(release(1)),
                tx(delete(2)),
                tx(release(1, s="roster")),
                tx(release(1)),
                tx(delete(1), release(1)),
            ],
        },
        {
            # No `returned` table (a store step 2 could not write): settle
            # writes nothing, release finds no mark.
            "name": "no_returned_table",
            "steps": [
                tx(put(1), put(2, [1])),
                {"edit": "drop_returned"},
                tx(delete(2)),
                tx(release(1)),
            ],
        },
        {
            "name": "rollback_journal",
            "steps": [
                {"edit": "journal", "mode": "DELETE"},
                tx(put(1), put(2, [1])),
                tx(delete(2), scope(put(3), FAIL), put(4, [1])),
                tx(delete(4)),
            ],
        },
        {
            # A failure in the outermost scope: nothing written, and the
            # next transaction goes on.
            "name": "outer_failure",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(delete(2), put(3), FAIL),
                tx(put(3), FAIL, put(4)),
                tx(FAIL),
                tx(delete(2)),
            ],
        },
        {
            # A refused mutation: inside a scope it rolls back the scope;
            # in the outermost scope the whole transaction.
            "name": "refused_inside",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(
                    scope(
                        delete(2),
                        {"op": "memory_put", "payload": {"memory": {"id": "x"}}},
                    ),
                    put(3),
                ),
                tx(
                    delete(2),
                    {"op": "memory_delete", "payload": {}, "session": None},
                ),
                tx(scope(delete(2)), scope(scope(put(4, [1]), FAIL), put(5))),
            ],
        },
        {
            "name": "not_ported",
            "steps": [
                tx(put(1)),
                tx(
                    put(2),
                    {"op": "home_put", "payload": {"home": {}}, "session": "s1"},
                    native="not_ported:kind",
                ),
                tx(put(3)),
            ],
        },
    ]
    # Nested failures at the first, middle and last step of a scope that
    # changes marks, with the outer transaction going on, and with the
    # outer failing after it too.
    inner = [delete(2), put(3, [1]), put(3, [], body="relinked"), put(4)]
    for k, items in enumerate(_failing_at(inner)):
        for outer_fails in (False, True):
            tail = [FAIL] if outer_fails else []
            out.append(
                {
                    "name": f"nested_{k}_{'outer_fails' if outer_fails else 'goes_on'}",
                    "steps": [
                        tx(put(1), put(2, [1]), put(5)),
                        tx(put(6), scope(*items), delete(5), *tail),
                        tx(delete(2)),
                    ],
                }
            )
    # The scope failing at its end, and a scope inside a scope.
    out.append(
        {
            "name": "nested_deep",
            "steps": [
                tx(put(1), put(2, [1])),
                tx(scope(*inner, fail=True), put(7)),
                tx(scope(delete(2), scope(put(3, [1]), fail=True)), put(8)),
                tx(scope(scope(delete(1)), release(2), fail=True), scope(release(1))),
            ],
        }
    )
    return out


# ---------------------------------------------------------------------------


def _run_items(
    store: Store, conn: Any, items: list[dict[str, Any]], errors: list[Any]
) -> None:
    for item in items:
        if "scope" in item:
            try:
                with store._transaction():
                    _run_items(store, conn, item["scope"], errors)
                    if item["fail"]:
                        raise Injected("the scope's end")
            except Exception as exc:  # noqa: BLE001 - the scope's error is the record
                errors.append(_refusal(exc))
        elif "fail" in item:
            raise Injected("here")
        elif "release" in item:
            store.release_returned(item["release"], session=item["session"])
        elif "name" in item:
            store._tx_returns.setdefault(item["name"], (item["via"], item["session"]))
        else:
            payload = json.loads(json.dumps(item["payload"]))
            store._mutate(conn, item["op"], payload, session=item.get("session"))


def _edit(edit: dict[str, Any], store: Store) -> None:
    what = edit["edit"]
    if what == "journal":
        store.conn.execute(f"PRAGMA journal_mode = {edit['mode']}")
    elif what == "drop_returned":
        store.conn.execute("DROP TABLE returned")
        store.conn.commit()
    else:
        raise AssertionError(what)


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            opened = state(case, root)
            for i, step in enumerate(case_def["steps"]):
                if "edit" in step:
                    _edit(step, store)
                    steps_out.append({"edit": step})
                    continue
                log = Ticker(LOG0 + timedelta(minutes=i), LOG_STEP)
                wall = Ticker(WALL0 + timedelta(hours=i), WALL_STEP)
                before = _last_seq(file)
                errors: list[Any] = []
                with fixed(log=log, wall=wall, mint=ulid_mint(1000 * i)):
                    try:
                        with store._transaction() as conn:
                            _run_items(store, conn, step["tx"], errors)
                        outcome: dict[str, Any] = {"written": True}
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"written": False, "refusal": _refusal(exc)}
                record: dict[str, Any] = {
                    "step": i,
                    "tx": step["tx"],
                    "native": step["native"],
                    "log_clock": log.stamps(),
                    "wall_clock": wall.stamps(),
                    "outcome": outcome,
                    "scope_errors": errors,
                }
                if step["native"] != "same":
                    record["reference_wrote"] = _last_seq(file) - before
                else:
                    record["rows"] = log_rows(file, before)
                    record["state"] = state_digest(state(case, root))
                    record["fts"] = fts_view(file)
                steps_out.append(record)
            folded = store.refold()
            refold = {
                "status": folded["status"],
                "problems": sorted(
                    [p["seq"], p["kind"], p["detail"]] for p in folded["problems"]
                ),
            }
        finally:
            store.close()
    return {
        "case": case_def["name"],
        "opened": opened,
        "refold": refold,
        "steps": steps_out,
    }


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_tx",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
            "memory_fields": list(Memory.model_fields),
        }
    ]
    # In the scratch world: no owner's config, keys or store is read.
    with world():
        runs = [run_case(c) for c in cases()]
    # Every case opens the same created store: its state is recorded once.
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
    return {"store_tx.jsonl": text.encode("ascii")}
