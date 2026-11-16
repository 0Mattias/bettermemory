"""Fixture: chunked runs, `batch`, `rename_scope` and `prune_tombstones`
(``store_bulk.jsonl``).

The native build ports `Store._in_chunks`, `Store.batch` and the two
chunked record writers (``native/``, `store::tx`, `store::bulk`) and is
held to this file. Each case starts from the store `Store.create` makes
under ``store_create.json``'s fixed values, opened as the owner, and runs
its steps: setting up records (`put_memory`, and `tombstone` with a given
removal time, every clock fixed: nothing here is compared but the state
they leave), raw SQL, or a call. Each call runs with every clock and mint
fixed (`_fixed.fixed`), the monotonic clock `Store._in_chunks` reads among
them: by default it starts at 100 and steps 1/32 of a second, so a chunk
closes after its fourth item (0.125 >= 0.1, where three items are
0.09375); a case may name its own schedule (``mono``, recorded with the
case), as the ``hold_*`` cases do to put readings at, just under and just
over the hold apart, still, backwards, or past it at once. The step
records, in the order made, every read of the log clock, the wall clock,
the mint and the monotonic clock, every pause between chunks (``sleep``)
and every ``BEGIN IMMEDIATE`` and ``COMMIT`` the store's connection ran
(``begin``, ``commit``: each chunk's boundaries); the stamps each clock
handed out; the returned value or the refusal's class and text; the rows
appended (their count, kinds and a digest, since a run writes many), the
state's digest, the head's text and the FTS5 index.

A pydantic ValidationError's text is recorded as its first line, in a
refusal and in `rename_scope`'s ``failed`` reasons alike: the rest of it
is pydantic's (its documentation URL names the pydantic version).

Gates, the same as ``store_tx.jsonl``'s and for its reasons: Unicode (the
index text is the tokenizer's), pydantic-core (a ValidationError's first
line; the models decide what a payload holds), POSIX only (the state reads
modes and the binding's resolved path). No text recorded is the
interpreter's own: the refusals are the product's (``TombstonedError``,
the apply's ``unknown provenance``), pydantic's first line, or
``Injected`` (the driver's); a UnicodeDecodeError's (a codec's) text is
recorded as None, its class compared. A blob in a stored text column is
read as the reference reads it (``blob_*``); where a blob would go on
into the payload as bytes the call is marked ``not_ported``. The
``lock_*`` cases have a second connection take a lock at the first pause
(``lock``, recorded with the case), so a later chunk's BEGIN or COMMIT
fails busy: sqlite3's OperationalError, its text None.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
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
from .gen_store_rows_written import Read
from .gen_store_tx import LOG0, LOG_STEP, WALL0, WALL_STEP
from .gen_store_write import _last_seq, _refusal, fts_view, log_rows, state_digest, ulid

#: The setup's one log-clock stamp.
SETUP = datetime(2001, 2, 3, 4, 0, 0, 500000, tzinfo=timezone.utc)
#: The monotonic clock of each call: its first reading and its step.
MONO0 = 100.0
MONO_STEP = 1 / 32


class Injected(Exception):
    """The failure a batch raises at a point it names."""


def record(n: int, scopes: list[str]) -> dict[str, Any]:
    return {
        "id": ulid(n),
        "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z",
        "scopes": scopes,
        "confidence": "high",
        "source": "explicit-statement",
        "body": f"record {n} about sqlite",
    }


def active(*spec: tuple[int, list[str]]) -> dict[str, Any]:
    return {"edit": "active", "records": [record(n, s) for n, s in spec]}


def removed(*spec: tuple[int, list[str], str]) -> dict[str, Any]:
    """Records put, then removed at the given time."""
    return {
        "edit": "removed",
        "records": [[record(n, s), at] for n, s, at in spec],
    }


def sql(statement: str) -> dict[str, Any]:
    return {"edit": "sql", "sql": statement}


def both_ways(n: int) -> dict[str, Any]:
    """Record n active and removed at once: its put raises TombstonedError
    (a KeyError, which no chunked writer catches)."""
    i = ulid(n)
    return sql(
        "INSERT INTO tombstones(id, created, updated, confidence, source, body, "
        "scopes_json, removed, removed_reason, provenance) "
        f"SELECT id, created, updated, confidence, source, body, scopes_json, "
        f"'2001-01-01T00:00:00.000000Z', 'twice', provenance "
        f"FROM memories WHERE id = '{i}'"
    )


def rename(
    old: str,
    new: str,
    tomb: bool = True,
    s: str | None = "s1",
    native: str = "same",
) -> dict[str, Any]:
    """`native` is ``same`` or ``not_ported`` (the native build refuses the
    call as not ported, with nothing written)."""
    return {
        "call": "rename",
        "old": old,
        "new": new,
        "tomb": tomb,
        "session": s,
        "native": native,
    }


def prune(older_s: int, now: str | None = None, s: str | None = "s1") -> dict:
    return {"call": "prune", "older_s": older_s, "now": now, "session": s}


def batch(*items: dict[str, Any]) -> dict[str, Any]:
    return {"call": "batch", "items": list(items), "session": "s1"}


def bput(n: int, scopes: list[str] | None = None) -> dict[str, Any]:
    return {"put": record(n, scopes or ["global"])}


def nested(*items: dict[str, Any], catch: bool = True) -> dict[str, Any]:
    return {"batch": list(items), "catch": catch}


FAIL: dict[str, Any] = {"fail": True}

A = "project:a"
B = "project:b"
OLD = "2001-01-10T00:00:00.000000Z"


def blob(table: str, column: str, hexed: str, n: int = 1) -> dict[str, Any]:
    """Record n's stored `column` set to the blob ``X'hexed'``."""
    return sql(f"UPDATE {table} SET {column} = X'{hexed}' WHERE id = '{ulid(n)}'")


def mono_at(schedule: dict[str, Any], k: int) -> float:
    """The k-th monotonic reading of a call: ``head`` in turn, then
    ``cycle`` round and round, or else ``step`` on from the head's last."""
    head = schedule["head"]
    if k < len(head):
        return float(head[k])
    if "cycle" in schedule:
        cycle = schedule["cycle"]
        return float(cycle[(k - len(head)) % len(cycle)])
    return float(head[-1] + schedule["step"] * (k - len(head) + 1))


#: The default schedule: 100, then 1/32 of a second a reading.
MONO = {"head": [MONO0], "step": MONO_STEP}


def many(k: int, start: int = 1) -> dict[str, Any]:
    return active(*[(n, ["global", A]) for n in range(start, start + k)])


def cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Runs shorter than a chunk, exactly one, one over, several, empty.
    for k in (0, 3, 4, 5, 12):
        out.append({"name": f"rename_{k}", "steps": [many(k), rename(A, B)]})
    out += [
        {
            "name": "rename_removed_too",
            "steps": [
                many(3),
                removed(
                    (20, [A], OLD),
                    (21, ["global", A, B], "2001-01-11T00:00:00.000000Z"),
                    (22, [B], OLD),
                ),
                rename(A, B),
                rename(B, A, tomb=False, s=None),
            ],
        },
        {
            # To a scope the records already hold: the list deduplicated.
            "name": "rename_onto_existing",
            "steps": [
                active((1, [A, B]), (2, [B, "global", A]), (3, [A])),
                rename(A, B),
            ],
        },
        {
            "name": "rename_same_and_none",
            "steps": [many(2), rename(A, A), rename("project:none", B)],
        },
        {
            # A scope that is a prefix of another is not the other.
            "name": "rename_prefix",
            "steps": [
                active(
                    (1, ["project:ab"]), (2, [A, "project:ab"]), (3, ["project:a-b"])
                ),
                rename(A, "project:c"),
                rename("project:a", "project:ab"),
            ],
        },
        {
            # A scope the model refuses: each active put fails into
            # `failed`; with tombstones the first removed one raises.
            "name": "invalid_scope",
            "steps": [
                many(5),
                removed((30, [A], OLD)),
                rename(A, "", tomb=False),
                rename(A, "bad scope", tomb=False),
                rename(A, "Project:A"),
            ],
        },
        {
            # An item that raises in the first, a middle and the last chunk
            # of three: the chunks before it stay committed.
            "name": "raise_first_chunk",
            "steps": [many(12), both_ways(2), rename(A, B)],
        },
        {
            "name": "raise_middle_chunk",
            "steps": [many(12), both_ways(6), rename(A, B), rename(A, B)],
        },
        {
            "name": "raise_last_chunk",
            "steps": [many(12), both_ways(12), rename(A, B)],
        },
        {
            "name": "prune",
            "steps": [
                removed(
                    *[
                        (n, ["global"], f"2001-01-{n:02d}T00:00:00.000000Z")
                        for n in range(1, 11)
                    ]
                ),
                prune(86400 * 400, now="2001-01-20T00:00:00+00:00"),
                prune(86400 * 15, now="2001-01-20T00:00:00+00:00"),
                prune(86400, now="2001-01-08T12:00:00+05:00"),
                prune(0),
                prune(0),
            ],
        },
        {
            # Everything, over three chunks.
            "name": "prune_all",
            "steps": [
                removed(
                    *[
                        (n, ["global"], f"2001-01-{n:02d}T00:00:00.000000Z")
                        for n in range(1, 11)
                    ]
                ),
                prune(0, now="2002-01-01T00:00:00+00:00"),
            ],
        },
        {
            "name": "prune_ties",
            "steps": [
                removed(
                    (5, ["global"], OLD),
                    (3, ["global"], OLD),
                    (4, ["global"], "2001-01-09T00:00:00.000000Z"),
                ),
                prune(1, s=None),
            ],
        },
        {
            "name": "batch",
            "steps": [
                batch(bput(1)),
                batch(bput(2), bput(3, [A]), bput(2, [B])),
                batch(bput(4), nested(bput(5), nested(bput(6))), bput(7)),
                batch(bput(8), nested(bput(9), FAIL), bput(10)),
                batch(bput(11), FAIL),
                batch(),
                removed((40, ["global"], OLD)),
                batch(bput(12), nested(bput(40))),
                batch(bput(13), bput(40)),
            ],
        },
    ]
    # A blob in a stored text column: `str(bytes)` is the label (`b'a'`,
    # which the apply refuses as unknown: caught into `failed` for an
    # active record, raised for a removed one), a filename goes on into the
    # payload as bytes (not ported natively: its values carry no bytes),
    # `json.loads(bytes)` reads the links. Each with bytes no codec reads.
    one = active((1, ["global", A]), (2, ["global", A]))
    dead = removed((1, ["global", A], OLD))
    # A label's bytes no codec reads (X'ff') are spelled b'\\xff', whose
    # reason here holds four backslashes in a row: what the host-fact check
    # takes for a UNC path. The native tests hold that one
    # (`store_bulk_robustness.rs`); here a quote, which `repr` escapes.
    for hexed, tag in (("61", ""), ("27", "_quote")):
        out += [
            {
                "name": f"blob_memory_provenance{tag}",
                "steps": [one, blob("memories", "provenance", hexed), rename(A, B)],
            },
            {
                "name": f"blob_tombstone_provenance{tag}",
                "steps": [dead, blob("tombstones", "provenance", hexed), rename(A, B)],
            },
        ]
    for hexed, tag in (("61", ""), ("ff", "_undecodable")):
        out += [
            {
                "name": f"blob_memory_filename{tag}",
                "steps": [
                    active((1, ["global", A])),
                    blob("memories", "filename", hexed),
                    rename(A, B, native="not_ported"),
                ],
            },
            {
                "name": f"blob_tombstone_filename{tag}",
                "steps": [
                    dead,
                    blob("tombstones", "filename", hexed),
                    rename(A, B, native="not_ported"),
                ],
            },
        ]
    for hexed, tag in (
        ("5b5d", ""),
        ("ff", "_undecodable"),
        ("fffe5b005d00", "_utf16"),
        ("5b2278225d", "_listed"),
    ):
        out.append(
            {
                "name": f"blob_tombstone_links{tag}",
                "steps": [dead, blob("tombstones", "links_json", hexed), rename(A, B)],
            }
        )
    # The chunk-hold check (`>=`) at its edge, each schedule over five
    # items: readings exactly the hold apart, just under, just over, a
    # clock that does not advance, one that goes backwards, and one past
    # the hold at the first item's reading.
    for name, schedule in (
        ("exact", {"head": [], "cycle": [0.0, 0.1]}),
        ("under", {"head": [], "cycle": [0.0, 0.09999999999999999]}),
        ("over", {"head": [], "cycle": [0.0, 0.10000000000000002]}),
        ("still", {"head": [], "cycle": [0.0]}),
        ("backwards", {"head": [0.0], "step": -MONO_STEP}),
        ("jump", {"head": [0.0, 5.0], "step": MONO_STEP}),
    ):
        out.append(
            {"name": f"hold_{name}", "mono": schedule, "steps": [many(5), rename(A, B)]}
        )
    # Each chunk settles its own returned marks: a stored link the record's
    # own list lacks is dropped by its put, which names the target (back in
    # force, so marked), here once in chunk 1 and once in chunk 2.
    out.append(
        {
            "name": "returned_per_chunk",
            "steps": [
                many(8),
                active((50, ["global"]), (51, ["global"])),
                sql(
                    "INSERT INTO memory_links(source_id, type, target_id, note) "
                    f"VALUES ('{ulid(1)}', 'supersedes', '{ulid(50)}', NULL), "
                    f"('{ulid(6)}', 'supersedes', '{ulid(51)}', NULL)"
                ),
                rename(A, B),
            ],
        }
    )
    # Another connection takes a lock at the first pause, after chunk 1
    # committed, and keeps it to the call's end; the store waits for no
    # lock (busy_timeout 0). ``write``: it holds the write lock, so chunk
    # 2's BEGIN fails. ``read``: in the rollback journal it holds a read
    # lock, so chunk 2 writes and its COMMIT fails. The store is compared
    # by its state (every table, the schema, the files present, the head),
    # not its bytes: the header names the SQLite library that last wrote
    # it (offset 96), freed cells keep what they held, and a WAL's salts
    # are random.
    wait_none = sql("PRAGMA busy_timeout = 0")
    rollback_journal = sql("PRAGMA journal_mode = DELETE")
    out += [
        {
            "name": "lock_begin",
            "lock": "write",
            "steps": [many(12), wait_none, rename(A, B), rename(A, B)],
        },
        {
            "name": "lock_commit",
            "lock": "read",
            "steps": [
                many(12),
                rollback_journal,
                wait_none,
                rename(A, B),
                rename(A, B),
            ],
        },
        {
            "name": "lock_prune",
            "lock": "write",
            "steps": [
                removed(
                    *[
                        (n, ["global"], f"2001-01-{n:02d}T00:00:00.000000Z")
                        for n in range(1, 11)
                    ]
                ),
                wait_none,
                prune(0, now="2002-01-01T00:00:00+00:00"),
                prune(0, now="2002-01-01T00:00:00+00:00"),
            ],
        },
    ]
    return out


# ---------------------------------------------------------------------------


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _setup(step: dict[str, Any], store: Store) -> None:
    log = Ticker(SETUP, timedelta(0))
    wall = Ticker(SETUP, timedelta(0))
    with fixed(log=log, wall=wall):
        if step["edit"] == "active":
            for r in step["records"]:
                store.put_memory(Memory.model_validate(r), session="setup")
        elif step["edit"] == "removed":
            for r, at in step["records"]:
                store.put_memory(Memory.model_validate(r), session="setup")
                store.tombstone(
                    r["id"], "old", session="setup", now=datetime.fromisoformat(at)
                )
        elif step["edit"] == "sql":
            store.conn.execute(step["sql"])
            store.conn.commit()
        else:
            raise AssertionError(step["edit"])


def _batch_items(store: Store, items: list[dict[str, Any]], session: Any) -> None:
    for item in items:
        if "put" in item:
            store.put_memory(Memory.model_validate(item["put"]), session=session)
        elif "fail" in item:
            raise Injected("here")
        else:
            try:
                with store.batch():
                    _batch_items(store, item["batch"], session)
            except Exception:
                if not item["catch"]:
                    raise


def _call(store: Store, call: dict[str, Any]) -> Any:
    what = call["call"]
    session = call["session"]
    if what == "rename":
        return store.rename_scope(
            call["old"], call["new"], include_tombstones=call["tomb"], session=session
        )
    if what == "prune":
        now = call["now"]
        return store.prune_tombstones(
            timedelta(seconds=call["older_s"]),
            now=None if now is None else datetime.fromisoformat(now),
            session=session,
        )
    if what == "batch":
        with store.batch():
            _batch_items(store, call["items"], session)
        return None
    raise AssertionError(what)


def _value(got: Any) -> Any:
    """`rename_scope`'s dict with each failed reason cut to its first line."""
    if isinstance(got, dict) and "failed" in got:
        got = {
            **got,
            "failed": [
                {**f, "reason": f["reason"].splitlines()[0]} for f in got["failed"]
            ],
        }
    return got


def _take(file: Path, lock: str) -> sqlite3.Connection:
    """A second connection holding the write lock (``write``) or a read
    lock (``read``)."""
    other = sqlite3.connect(file, isolation_level=None)
    if lock == "write":
        other.execute("BEGIN IMMEDIATE")
    else:
        other.execute("BEGIN")
        other.execute("SELECT count(*) FROM log").fetchall()
    return other


def _bulk_refusal(exc: BaseException) -> dict[str, Any]:
    """`_refusal`, with a codec's wording left out: a UnicodeDecodeError's
    text is the interpreter's; its class is compared."""
    if isinstance(exc, UnicodeDecodeError):
        return {"class": type(exc).__name__, "text": None}
    return _refusal(exc)


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        lock = case_def.get("lock")
        first_call = next(i for i, s in enumerate(case_def["steps"]) if "call" in s)
        try:
            opened = state(case, root)
            for i, step in enumerate(case_def["steps"]):
                if "edit" in step:
                    _setup(step, store)
                    steps_out.append({"edit": step})
                    continue
                reads: list[str] = []
                log = Read(LOG0 + timedelta(minutes=i), LOG_STEP, "log", reads)
                wall = Read(WALL0 + timedelta(hours=i), WALL_STEP, "wall", reads)
                mono: list[str] = []
                minted: list[str] = []
                inner = ulid_mint(1000 * i)

                def mint(
                    inner: Any = inner, minted: list[str] = minted, reads: Any = reads
                ) -> str:
                    reads.append("mint")
                    minted.append(inner())
                    return minted[-1]

                schedule = case_def.get("mono", MONO)

                def monotonic(
                    mono: list[str] = mono, reads: Any = reads, schedule: Any = schedule
                ) -> float:
                    reads.append("mono")
                    value = mono_at(schedule, len(mono))
                    mono.append(repr(value))
                    return value

                held: list[sqlite3.Connection] = []

                def sleep(seconds: float, reads: Any = reads, held: Any = held) -> None:
                    reads.append(f"sleep {seconds!r}")
                    if lock is not None and not held and i == first_call:
                        held.append(_take(file, lock))

                def trace(statement: str, reads: Any = reads) -> None:
                    if statement == "BEGIN IMMEDIATE":
                        reads.append("begin")
                    elif statement == "COMMIT":
                        reads.append("commit")

                before = _last_seq(file)
                store.conn.set_trace_callback(trace)
                with fixed(
                    log=log, wall=wall, mint=mint, monotonic=monotonic, sleep=sleep
                ):
                    try:
                        outcome: dict[str, Any] = {
                            "written": True,
                            "returned": _value(_call(store, step)),
                        }
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"written": False, "refusal": _bulk_refusal(exc)}
                store.conn.set_trace_callback(None)
                for other in held:
                    other.rollback()
                    other.close()
                rows = log_rows(file, before)
                steps_out.append(
                    {
                        "step": i,
                        "call": step,
                        "log_clock": log.stamps(),
                        "wall_clock": wall.stamps(),
                        "minted": minted,
                        "monotonic": mono,
                        "reads": reads,
                        "outcome": outcome,
                        "rows": {
                            "count": len(rows),
                            "kinds": [r[3] for r in rows],
                            "sha256": _digest(rows),
                        },
                        "state": state_digest(state(case, root)),
                        "fts": fts_view(file),
                    }
                )
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
        "mono": case_def.get("mono", MONO),
        "lock": case_def.get("lock"),
        "opened": opened,
        "refold": refold,
        "steps": steps_out,
    }


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_bulk",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
            "setup_ts": SETUP.isoformat(),
        }
    ]
    with world():
        runs = [run_case(c) for c in cases()]
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
    return {"store_bulk.jsonl": text.encode("ascii")}
