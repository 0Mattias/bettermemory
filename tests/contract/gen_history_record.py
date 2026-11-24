"""Fixture: `history`'s fourth unit, one record's whole history
(``history_record.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/history_record.rs``. This pins it
to `record_history`, `_read_history` with its ``adjacent``, `_state`,
`_table_kind`, `replaced_detail` and `RecordHistory` (``known``,
``findings``, ``exit_code``, ``to_dict``).

Each line is one store, built as ``gen_store_provenance`` builds its
cases: `Store.create` under ``store_create.json``'s fixed values, log
rows appended with `log.append_row` under a chosen key and applied with
`_apply_mutation`, every clock fixed, then plants by SQL. Recorded: the
rows of the five tables the reader reads (``meta``, ``memories``,
``episodes``, ``tombstones``, ``log``; a blob is ``{"$bytes": hex}``)
and, per opening (a key ring laid out, then `Store.open(read_only=True)`
under a ``strict_provenance``), the ring's files, the fingerprint the
open verifies under, and per id asked (``id``, or ``id_cps`` when it
holds a surrogate) either ``r``, `record_history(store, id).to_dict(
diff=True)` as ``json.dumps`` writes it with ``ensure_ascii`` and compact
separators (its keys in their order, a lone surrogate escaped; the
reference's values are all JSON's, read from payloads by `json.loads`,
so the text is exact and about a third of `_common.neutral`'s size),
with ``exit`` and ``known``, or ``raised``, the exception's class.

The last line is a sweep: a store built by a seeded script of writes,
updates, removals, restores, prunes, links, telemetry and tampering over
a handful of records, its tables recorded in full; every record's
history (and three ids no row names as a subject) is asked, and the
answers are pinned by count and the SHA-256 of their lines, each
``json.dumps([id, to_dict(diff=True), exit_code, known])`` with
``ensure_ascii`` and compact separators.

Gates, the same as ``store_provenance.jsonl``'s and for the same
reasons: Unicode (the recorded rows include the tokenizer's columns),
pydantic-core (the records are built through the models) and POSIX
only (the reference's create and open write and check the key ring's
modes). Not on the Python version: no text recorded is the
interpreter's; a payload nested past the recursion limit is left to the
robustness test.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from bettermemory.history import record_history
from bettermemory.store import Store

from . import gen_store_create
from ._common import text_field
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_log import KEY_A, STORE_ID
from .gen_store_provenance import (
    KEY_B,
    KEY_Z,
    TS0,
    Builder,
    _cell,
    _keys,
    _ring,
    e,
    fp,
    m,
)
from .gen_store_write import memory, tombstone

TABLES = ("meta", "memories", "episodes", "tombstones", "log")


def _tables(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in TABLES:
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        columns = [d[0] for d in cursor.description]
        out[table] = {
            "columns": columns,
            "rows": [[_cell(v) for v in row] for row in cursor],
        }
    return out


def _ask(store: Store, record_id: str) -> dict[str, Any]:
    out: dict[str, Any] = text_field("id", record_id)
    try:
        history = record_history(store, record_id)
    except Exception as exc:
        out["raised"] = type(exc).__name__
        return out
    out["r"] = _dumps(history.to_dict(diff=True))
    out["exit"] = history.exit_code
    out["known"] = history.known
    return out


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _line(store: Store, record_id: str) -> str:
    history = record_history(store, record_id)
    answer = [record_id, history.to_dict(diff=True), history.exit_code, history.known]
    return _dumps(answer) + "\n"


def _built(build: Callable[[Builder], None], tmp: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp / "c"
    root.mkdir()
    with gen_store_create._fixed():
        Store.create(root, keys_dir=root / "keys").close()
    file = root / "memory.sqlite"
    conn = sqlite3.connect(file, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    wall = Ticker(TS0, timedelta(milliseconds=1))
    with fixed(log=Ticker(TS0, timedelta(seconds=1)), wall=wall):
        build(Builder(conn))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    tables = _tables(conn)
    conn.close()
    return root, tables


def _open(root: Path, opening: dict[str, Any]) -> tuple[Store, dict[str, Any]]:
    _ring(root / "keys", opening.get("ring", {}))
    store = Store.open(
        root / "memory.sqlite",
        keys_dir=root / "keys",
        read_only=True,
        strict_provenance=opening.get("strict", False),
    )
    verify = store._verify_key
    return store, {
        "keys": _keys(root / "keys"),
        "strict": opening.get("strict", False),
        "verifies": None if verify is None else fp(verify),
    }


def run_store(case: dict[str, Any]) -> dict[str, Any]:
    with world(), tempfile.TemporaryDirectory() as tmp:
        root, tables = _built(case["build"], Path(tmp))
        openings = []
        for opening in case["openings"]:
            store, out = _open(root, opening)
            try:
                out["asks"] = [_ask(store, i) for i in opening["ids"]]
            finally:
                store.close()
            openings.append(out)
    return {"store": case["name"], "tables": tables, "openings": openings}


# ---------------------------------------------------------------------------
# The stores
# ---------------------------------------------------------------------------


def link(target: str, kind: str = "extends") -> dict[str, Any]:
    return {"type": kind, "target_id": target, "note": None}


def put(b: Builder, n: int, **fields: Any) -> Any:
    return b.put(KEY_A, "memory_put", memory(n, **fields))


def remove(b: Builder, n: int, links: list[dict[str, Any]] | None = None) -> None:
    b.put(KEY_A, "memory_delete", {"id": m(n)})
    dead = tombstone(n)
    dead["links"] = links or []
    b.put(KEY_A, "tombstone_put", dead)


def restore(b: Builder, n: int, **fields: Any) -> None:
    b.put(KEY_A, "tombstone_delete", {"id": m(n)})
    put(b, n, **fields)


def note(b: Builder, kind: str, payload: Any) -> Any:
    """A row appended to the log only, its payload as given."""
    return b.row(KEY_A, kind, payload)


def _life(b: Builder) -> None:
    """Every record branch, state and reference over one store."""
    # Before the first row of m(1): a replaced row the reader skips.
    note(b, "store_restored", {"head_after": {"seq": 1}, "note": m(1)})
    # m(1): created, updated (body and fields), verified, corroborated.
    put(b, 1, body="first line\nsecond line\n")
    note(
        b, "write", {"id": m(1), "actor": {"client": "c", "user": "u"}, "status": "ok"}
    )
    put(b, 1, body="first line\nsecond changed\n", confidence="low")
    put(b, 1, body="first line\nsecond changed\n", confidence="low", tags=["t"])
    b.put(
        KEY_A,
        "verification_put",
        {
            "memory_id": m(1),
            "machine_id": "mach",
            "verified_at": "2003-01-01T00:00:00Z",
        },
    )
    note(b, "update", {"id": m(1), "actor": {}, "fields": ["body"]})
    note(b, "verify", {"id": m(1)})
    note(b, "standing_guard", {"standing": [m(1), m(2)]})
    put(b, 1, body="first line\nsecond changed\n", confidence="low", corroborations=2)
    # Conflicts naming m(1) first, second, both; not a mapping; no status.
    note(
        b,
        "conflict_put",
        {"conflict": {"id": "c1", "a_id": m(1), "b_id": m(2), "status": "open"}},
    )
    note(
        b,
        "conflict_put",
        {"conflict": {"id": "c2", "a_id": m(3), "b_id": m(1), "status": None}},
    )
    note(b, "conflict_put", {"conflict": {"id": "c3", "a_id": m(1), "b_id": m(1)}})
    note(b, "conflict_put", {"conflict": [m(1)]})
    note(b, "conflict_put", {"conflict": {"a_id": None, "b_id": m(1), "status": "x"}})
    # Rows that say the history was replaced, after m(1)'s first row and
    # before every other record's.
    note(
        b,
        "store_restored",
        {
            "bundle_created_at": "2003-01-02T00:00:00Z",
            "head_after": {"seq": 4},
            "head_before": {"seq": 9},
            "log_end_before": {"seq": 12},
        },
    )
    note(
        b,
        "store_restored",
        {"log_end_before": {"seq": 7}, "head_before": {"seq": True}},
    )
    note(b, "history_accepted", {"log_end_before": {"seq": 10**30}, "note": m(1)})
    note(b, "history_accepted", {"head_after": {}, "head_before": {"seq": -3}})
    note(b, "history_accepted", {"head_after": {"seq": None}, "log_end_before": "x"})
    # m(2): removed (delete then its tombstone, adjacent): tombstoned.
    put(b, 2)
    remove(b, 2)
    # m(3): removed and restored: active.
    put(b, 3)
    remove(b, 3)
    restore(b, 3, body="restored body")
    # m(4): removed and pruned.
    put(b, 4)
    remove(b, 4)
    b.put(KEY_A, "tombstone_delete", {"id": m(4)})
    # m(5) deleted, the next row in the log the tombstone of m(7).
    put(b, 5)
    put(b, 7)
    b.put(KEY_A, "memory_delete", {"id": m(7)})
    b.put(KEY_A, "memory_delete", {"id": m(5)})
    b.put(KEY_A, "tombstone_put", tombstone(7))
    # m(6): deleted outside the store.
    put(b, 6)
    b.sql("DELETE FROM memories WHERE id = ?", m(6))
    # m(8): a tombstone no row put, planted by SQL from m(2)'s.
    columns = [r[1] for r in b.conn.execute("PRAGMA table_info(tombstones)")]
    picked = ", ".join("?" if c == "id" else c for c in columns)
    b.sql(
        f"INSERT INTO tombstones ({', '.join(columns)}) "
        f"SELECT {picked} FROM tombstones WHERE id = ?",
        m(8),
        m(2),
    )
    # m(9): removed, then a tombstone_delete the tables never saw.
    put(b, 9)
    remove(b, 9)
    note(b, "tombstone_delete", {"id": m(9)})
    # m(11): its delete and tombstone with an unrelated row between.
    put(b, 11)
    b.put(KEY_A, "memory_delete", {"id": m(11)})
    put(b, 12)
    b.put(KEY_A, "tombstone_put", tombstone(11))
    # m(13): delete, then rows naming it whose payload is no object,
    # then its tombstone; m(14): a gap in seq between delete and tombstone.
    put(b, 13)
    b.put(KEY_A, "memory_delete", {"id": m(13)})
    junk = note(b, "remark", {"text": "x"})
    b.sql("UPDATE log SET payload = ? WHERE seq = ?", f"not json {m(13)}", junk.seq)
    blob = note(b, "remark", {"text": m(13)})
    b.sql("UPDATE log SET payload = CAST(payload AS BLOB) WHERE seq = ?", blob.seq)
    listed = note(b, "remark", {"text": "y"})
    b.sql("UPDATE log SET payload = ? WHERE seq = ?", json.dumps([m(13)]), listed.seq)
    b.put(KEY_A, "tombstone_put", tombstone(13))
    put(b, 14)
    gap = b.put(KEY_A, "memory_delete", {"id": m(14)})
    dead = dict(tombstone(14))
    b.row(KEY_A, "tombstone_put", dead, seq=gap.seq + 3)
    # Episodes: one put, one pruned.
    b.episode(1)
    b.episode(2)
    b.put(KEY_A, "episode_delete", {"id": e(2)})
    # m(20) linked to: m(21) links, changes type, drops, links again with
    # two types, is removed (links carried) and pruned (unlinked).
    put(b, 20)
    put(b, 21, links=[link(m(20), "extends")])
    put(b, 21, links=[link(m(20), "depends_on")])
    put(b, 21, links=[])
    both = [link(m(20), "depends_on"), link(m(20), "contradicts")]
    put(b, 21, links=both)
    remove(b, 21, links=both)
    b.put(KEY_A, "tombstone_delete", {"id": m(21)})
    # m(22) supersedes m(20); m(23) links, is removed and restored.
    put(b, 22, links=[link(m(20), "supersedes")])
    put(b, 23, links=[link(m(20))])
    remove(b, 23, links=[link(m(20))])
    restore(b, 23, links=[link(m(20))])
    # A row tampered with: m(1)'s last version.
    b.sql(
        "UPDATE log SET mac = ? WHERE seq = (SELECT max(seq) FROM log "
        "WHERE kind = 'memory_put' AND instr(payload, ?) > 0)",
        "00" * 32,
        m(1),
    )


LIFE_IDS = [
    m(1),
    m(2),
    m(3),
    m(4),
    m(5),
    m(6),
    m(7),
    m(8),
    m(9),
    m(11),
    m(13),
    m(14),
    e(1),
    e(2),
    m(20),
    m(21),
    m(999),
    m(1)[:-1],
    "",
    "a\x00b",
    "\ud800",
    m(1) + "\udc80",
]


def _rekeyed(b: Builder) -> None:
    """Rows under KEY_A, a rekey to KEY_B, rows under KEY_B."""
    put(b, 1)
    put(b, 1, body="second")
    b.rekey(KEY_A, fp(KEY_A), fp(KEY_B))
    b.put(KEY_B, "memory_put", memory(1, body="third"))
    b.sql("UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", fp(KEY_B))


def _foreign(b: Builder) -> None:
    """A store whose meta names a key this host does not hold."""
    put(b, 1, provenance="local")
    b.sql("UPDATE memories SET provenance = 'imported' WHERE id = ?", m(1))
    b.sql("UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", fp(KEY_Z))


def _carrier(b: Builder) -> None:
    """A link kept only when the row after the removal is its carrier."""
    # m(31) links m(30), is removed with no tombstone after it and put
    # again: the row after the memory_delete is a memory_put, not the
    # tombstone_put that carries links, so the link is unlinked and the
    # put links it again.
    put(b, 30)
    put(b, 31, links=[link(m(30))])
    b.put(KEY_A, "memory_delete", {"id": m(31)})
    put(b, 31, links=[link(m(30))])


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "life",
            "build": _life,
            "openings": [
                {"ids": LIFE_IDS},
                {"ids": [m(3), m(8), e(1)], "strict": True},
                {"ids": [m(3), m(8)], "ring": {"current": None}},
            ],
        },
        {
            "name": "rekeyed",
            "build": _rekeyed,
            "openings": [
                {"ids": [m(1)], "ring": {"current": KEY_B, "retired": [KEY_A]}},
                {"ids": [m(1)], "ring": {"current": KEY_B}},
                {"ids": [m(1)], "ring": {"current": KEY_A}},
            ],
        },
        {"name": "carrier", "build": _carrier, "openings": [{"ids": [m(30)]}]},
        {
            "name": "foreign_key",
            "build": _foreign,
            "openings": [
                {"ids": [m(1)]},
                {"ids": [m(1)], "ring": {"origin": None}},
                {"ids": [m(1)], "ring": {"current": KEY_B, "origin": None}},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

SWEEP_SEED = 9041
SWEEP_STEPS = 48
SWEEP_RECORDS = [40, 41, 42, 43, 44]
SWEEP_TYPES = ["contradicts", "depends_on", "extends", "supersedes"]


def _sweep(b: Builder) -> None:
    rng = random.Random(SWEEP_SEED)
    state = dict.fromkeys(SWEEP_RECORDS, "none")
    version = dict.fromkeys(SWEEP_RECORDS, 0)

    def links(n: int) -> list[dict[str, Any]]:
        out = []
        for other in SWEEP_RECORDS:
            if other != n and rng.random() < 0.3:
                out.append(link(m(other), rng.choice(SWEEP_TYPES)))
        return out

    def write(n: int) -> None:
        version[n] += 1
        lines = [f"line {i} v{rng.randrange(3)}" for i in range(rng.randrange(1, 4))]
        put(b, n, body="\n".join(lines), links=links(n))
        state[n] = "active"

    for _ in range(SWEEP_STEPS):
        n = rng.choice(SWEEP_RECORDS)
        op = rng.randrange(8)
        if state[n] in ("none", "pruned"):
            write(n)
        elif state[n] == "active" and op < 3:
            write(n)
        elif state[n] == "active" and op < 5:
            remove(b, n, links=links(n))
            state[n] = "tombstoned"
        elif state[n] == "tombstoned" and op < 3:
            b.put(KEY_A, "tombstone_delete", {"id": m(n)})
            write(n)
        elif state[n] == "tombstoned" and op < 5:
            b.put(KEY_A, "tombstone_delete", {"id": m(n)})
            state[n] = "pruned"
        elif op == 5:
            note(
                b,
                rng.choice(["write", "update", "verify"]),
                {"id": m(n), "actor": {"a": n}},
            )
        elif op == 6:
            other = rng.choice(SWEEP_RECORDS)
            note(
                b,
                "conflict_put",
                {"conflict": {"id": f"c{n}", "a_id": m(n), "b_id": m(other)}},
            )
        else:
            note(b, "remark", {"text": "unrelated"})
    for _ in range(4):
        b.sql(
            "UPDATE log SET mac = ? WHERE seq = ?",
            "11" * 32,
            rng.randrange(1, 40),
        )


def sweep() -> dict[str, Any]:
    ids = [m(n) for n in SWEEP_RECORDS] + [m(999), m(40)[:-2], "01J"]
    with world(), tempfile.TemporaryDirectory() as tmp:
        root, tables = _built(_sweep, Path(tmp))
        store, out = _open(root, {})
        try:
            lines = [_line(store, i) for i in ids]
        finally:
            store.close()
    digest = hashlib.sha256("".join(lines).encode("ascii")).hexdigest()
    return {
        "store": "sweep",
        "tables": tables,
        "sweep": {**out, "ids": ids, "count": len(lines), "sha256": digest},
    }


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {"fixture": "history_record", "store_id": STORE_ID, "tables": list(TABLES)}
    ]
    lines += [run_store(case) for case in cases()]
    lines.append(sweep())
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"history_record.jsonl": text.encode("ascii")}
