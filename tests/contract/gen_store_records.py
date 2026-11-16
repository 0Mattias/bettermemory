"""Fixture: the plain public record writers, ``put_memory``, ``tombstone``
and ``delete_tombstone`` (``store_records.jsonl``).

The native build ports them as functions on the writable store
(``native/``, `store::writers`) and is held to this file. Each case starts
from the store `Store.create` makes under ``store_create.json``'s fixed
values, opened as the owner (as ``store_tx.jsonl``'s are), and runs its
steps: an edit between calls (the journal mode, the ``returned`` table
dropped, one SQL statement), or one writer call. Every clock the writers
read is fixed (`_fixed.fixed`), the log clock and the wall clock apart;
each call records the stamps each clock handed out, so a replay reads them
in the same order and as often.

After each call: the returned value (the record's canonical payload text,
as `log.canonical_payload(record_payload(...))` spells it; None for
``delete_tombstone``) or the refusal's class and text (``str(exc)``, a
KeyError subclass's being its message's ``repr``), and, as
``store_tx.jsonl`` records after each transaction, the rows appended
(every column), the state's digest, the head's text and the FTS5 index as
queries see it. ``home`` is ``RecordHome("global", None, "fallback")``
and ``standing`` ``StandingAnswer(0.5, "explicit", "test")``.

Gates, the same as ``store_tx.jsonl``'s and for the same reasons.
Unicode: the index text is the tokenizer's spelling and the digest's rows
its tokens. pydantic-core: the models decide what a payload holds and how
a record is dumped. POSIX only: the state includes modes and the binding's
resolved path. Not on the Python version: every text compared is the
product's own wording, or ``repr`` of a str, an int, a bool or a list,
which 3.11 to 3.14 spell alike. Not on the SQLite version: FTS5's shadow
tables are not compared, only what a ``MATCH`` returns.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.log import canonical_payload
from bettermemory.models import Memory, record_payload
from bettermemory.store import Store
from bettermemory.store_schema import RecordHome, StandingAnswer

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

#: The log clock's first reading in call k is LOG0 + k minutes.
LOG0 = datetime(2001, 2, 3, 5, 0, 0, 111111, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1, microseconds=1)
#: The wall clock: another day, another step, so a stamp says which.
WALL0 = datetime(2003, 4, 5, 6, 7, 8, 999000, tzinfo=timezone.utc)
WALL_STEP = timedelta(minutes=7, microseconds=3)

ABSENT = "absent"
TARGETS = ("verified_path_targets", "verified_absent_path_targets")


def rec(n: int, sup: list[int] | None = None, body: str | None = None, **f: Any):
    links = [{"type": "supersedes", "target_id": ulid(t)} for t in sup or []]
    if not any(k.endswith("path_targets") for k in f):
        return memory(n, body=body, links=links, **f)["memory"]
    # A record `record_payload` may refuse: dumped as the model holds it.
    record = memory(n, body=body, links=links)["memory"]
    record.update(f)
    dumped = Memory.model_validate(record).model_dump(mode="json")
    return {k: v for k, v in dumped.items() if v is not None or k not in TARGETS}


def put(
    n: int,
    sup: list[int] | None = None,
    *,
    body: str | None = None,
    provenance: Any = ABSENT,
    filename: Any = ABSENT,
    session: str | None = "s1",
    native: str = "same",
    home: bool = False,
    standing: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "call": "put",
        "memory": rec(n, sup, body, **fields),
        "session": session,
        "native": native,
    }
    if provenance != ABSENT:
        call["provenance"] = provenance
    if filename != ABSENT:
        call["filename"] = filename
    if home:
        call["home"] = True
    if standing:
        call["standing"] = True
    return call


def tomb(
    n: int | str,
    reason: str = "wrong",
    session: str | None = "s1",
    now: str | None = None,
) -> dict[str, Any]:
    return {
        "call": "tombstone",
        "id": n if isinstance(n, str) else ulid(n),
        "reason": reason,
        "session": session,
        "now": now,
        "native": "same",
    }


def undo(n: int | str, session: str | None = "s1") -> dict[str, Any]:
    return {
        "call": "delete_tombstone",
        "id": n if isinstance(n, str) else ulid(n),
        "session": session,
        "native": "same",
    }


def flag(what: str) -> dict[str, Any]:
    """``home=True`` or ``standing=True``, as keyword arguments."""
    return {what: True}


def sql(statement: str) -> dict[str, Any]:
    return {"edit": "sql", "sql": statement}


def cases() -> list[dict[str, Any]]:
    """Each: name, steps (edits and calls)."""
    one = ulid(1)
    return [
        {
            # A first put, re-puts keeping and replacing label and name.
            "name": "first_and_reput",
            "steps": [
                put(1),
                put(1, body="record one again about sqlite"),
                put(1, provenance="imported", filename="a.md"),
                put(1, filename=""),
                put(1, provenance="synced", filename="b.md"),
            ],
        },
        {
            # Each writable label on a new row, and the refused ones.
            "name": "provenance",
            "steps": [
                put(2, provenance="synced"),
                put(3, provenance="imported"),
                put(4, provenance="local"),
                put(5, provenance=None),
                put(6, provenance=""),
                put(6, provenance="bogus"),
                put(6, provenance="it's"),
                put(6, provenance=5),
                put(6, provenance=["local"]),
                put(6, provenance={"local": 1}),
            ],
        },
        {
            # The `or` fallbacks: a stored label that is empty or NULL-like.
            "name": "stored_label_blank",
            "steps": [
                put(1, provenance="imported", filename="x.md"),
                sql("UPDATE memories SET provenance = '', filename = NULL"),
                put(1, body="blank label falls back to local"),
            ],
        },
        {
            "name": "tombstoned",
            "steps": [
                put(1, filename="f.md", provenance="imported"),
                tomb(1, "it's wrong"),
                put(1, body="refused over the tombstone"),
                tomb(1, "again"),
                tomb(9),
                tomb("not-a-ulid"),
                undo(1),
                undo(1),
                undo("nothing here"),
                put(1, body="back after the tombstone went"),
                tomb(1, 'say "no"', session=None, now="2010-01-02T03:04:05.123456Z"),
                put(1),
            ],
        },
        {
            # Returned marks a put's links and a removal leave.
            "name": "marks",
            "steps": [
                put(1),
                put(2, [1]),
                tomb(2, "superseder removed"),
                put(3, [1]),
                put(3, [], body="link dropped"),
                put(4, [1], session="s2"),
                tomb(1, "the superseded one removed"),
                tomb(4, "now the superseder", now="2011-01-01T00:00:00Z"),
                undo(1),
            ],
        },
        {
            # A tombstone carries the row's links, rollup and name.
            "name": "tombstone_carries",
            "steps": [
                put(1),
                put(2),
                put(
                    3,
                    [1, 2],
                    filename="three.md",
                    provenance="synced",
                    corroborations=3,
                    last_corroborated="2001-02-03T04:05:07.000001Z",
                    verified_paths=["src/a.py"],
                    verified_path_targets={"src/a.py": None},
                ),
                tomb(3, "carried", session="rm"),
                undo(3, session=None),
            ],
        },
        {
            # A home and a standing answer written with the record; the same
            # home again writes no row.
            "name": "home_and_standing",
            "steps": [
                put(2, home=True),
                put(2, body="the same home again", home=True),
                put(3, home=True, standing=True),
            ],
        },
        {
            # `validate_path_targets`' refusal, through a put and through
            # a removal whose stored row no longer covers its list.
            "name": "path_targets",
            "steps": [
                put(1, verified_paths=["a", "b"], verified_path_targets={"a": None}),
                put(
                    1,
                    verified_absent_paths=["x"],
                    verified_absent_path_targets={"y": "z"},
                ),
                put(
                    2,
                    verified_paths=["a"],
                    verified_path_targets={"a": None, "b": None},
                ),
                sql(
                    'UPDATE memories SET verified_paths_json = \'["a", "c"]\' '
                    f"WHERE id = '{ulid(2)}'"
                ),
                tomb(2),
            ],
        },
        *(
            {
                # A home or standing answer with a call the reference refuses
                # for another reason first: that reason, nothing written.
                "name": f"{what}_order",
                "steps": [
                    put(1),
                    tomb(1),
                    put(1, **flag(what)),
                    put(2, provenance="bogus", **flag(what)),
                    put(1, provenance="bogus", **flag(what)),
                    put(
                        3,
                        verified_paths=["a"],
                        verified_path_targets={},
                        **flag(what),
                    ),
                ],
            }
            for what in ("home", "standing")
        ),
        {
            "name": "no_returned_table",
            "steps": [
                put(1),
                put(2, [1]),
                {"edit": "drop_returned"},
                tomb(2),
                put(3, [1]),
            ],
        },
        {
            "name": "rollback_journal",
            "steps": [
                {"edit": "journal", "mode": "DELETE"},
                put(1),
                put(2, [1]),
                tomb(2),
                undo(2),
                put(2, [1], provenance="imported"),
                tomb(one, "unicode reason é中"),
            ],
        },
    ]


# ---------------------------------------------------------------------------


def _call(store: Store, call: dict[str, Any]) -> str | None:
    what = call["call"]
    session = call["session"]
    if what == "put":
        m = Memory.model_validate(call["memory"])
        kwargs: dict[str, Any] = {"session": session}
        if "provenance" in call:
            kwargs["provenance"] = call["provenance"]
        if "filename" in call:
            kwargs["filename"] = call["filename"]
        if call.get("home"):
            kwargs["home"] = RecordHome("global", None, "fallback")
        if call.get("standing"):
            kwargs["standing"] = StandingAnswer(0.5, "explicit", "test")
        got = store.put_memory(m, **kwargs)
        return canonical_payload(record_payload(got))
    if what == "tombstone":
        now = call["now"]
        dead = store.tombstone(
            call["id"],
            call["reason"],
            session=session,
            now=None if now is None else datetime.fromisoformat(now),
        )
        return canonical_payload(record_payload(dead))
    if what == "delete_tombstone":
        store.delete_tombstone(call["id"], session=session)
        return None
    raise AssertionError(what)


def _edit(edit: dict[str, Any], store: Store) -> None:
    what = edit["edit"]
    if what == "journal":
        store.conn.execute(f"PRAGMA journal_mode = {edit['mode']}")
    elif what == "drop_returned":
        store.conn.execute("DROP TABLE returned")
        store.conn.commit()
    elif what == "sql":
        store.conn.execute(edit["sql"])
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
                with fixed(log=log, wall=wall, mint=ulid_mint(1000 * i)):
                    try:
                        outcome: dict[str, Any] = {
                            "written": True,
                            "returned": _call(store, step),
                        }
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"written": False, "refusal": _refusal(exc)}
                record: dict[str, Any] = {
                    "step": i,
                    "call": step,
                    "log_clock": log.stamps(),
                    "wall_clock": wall.stamps(),
                    "outcome": outcome,
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
            "fixture": "store_records",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
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
    return {"store_records.jsonl": text.encode("ascii")}
