"""Fixture: what a restore's relink reads before it puts a record again
(``store_records_e.jsonl``): each linking record's provenance label under
the key ring's history and ``strict_provenance``, and the ids a
``refold`` earlier in the same process found unaccounted.

The native build ports the relink (``native/``, `store::writers`, part
W6r) and is held to this file beside ``store_records_d.jsonl``. A file of
its own: ``_d`` holds the writers' own cases near its budget, and these
need a store whose key changed, which takes many steps.

The cases run as ``store_records_d.jsonl``'s do, with four more steps,
each recorded with exactly what it wrote so the native side replays the
same bytes:

- ``rekey``: a ``rekey`` row naming the next key, signed by the current
  one and chained to the last row (its columns recorded), ``meta``'s
  fingerprint moved to the next key, its key file written as current and
  the old key kept as a retired key or dropped;
- ``drop``: a key file or the origin anchor removed from the ring;
- ``reopen``: the store closed and opened again under the ring as it now
  is, with ``strict_provenance`` as the step says (every opener here sets
  it, so no config is read);
- ``refold``: the reference's ``refold``, recording the ids it left in
  ``_unaccounted_memory_ids``, which the next restore's relink reads (a
  reopen starts without them);
- ``later_field``: a later build's field put on the row a record points
  at, the row signed again under the current key (its new payload and MAC
  recorded), so the record's pointer still verifies.

A restore re-signs each record it relinks under the current key, so each
scenario restores its own target, linked from records not yet put again.

Gates, the same as ``store_records_d.jsonl``'s, chosen by input. The
refusal texts follow ``store_records_d.jsonl``'s rule; nothing is nested
deep.
"""

from __future__ import annotations

import json
from typing import Any

from bettermemory import log as blog
from bettermemory.store import Store

from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS
from .gen_store_records_b import put, run_case, tomb
from .gen_store_records_d import _run as run_d
from .gen_store_records_d import link, linked, restore, sql
from .gen_store_write import ulid

KEYS = {"k1": bytes([0x11] * 32), "k2": bytes([0x22] * 32), "z": bytes([0x5A] * 32)}
REKEY_TS = "2001-02-03T09:00:00.000000Z"


def rekey(to: str, keep_old: bool) -> dict[str, Any]:
    return {"call": "rekey", "to": to, "keep_old": keep_old}


def drop(what: str) -> dict[str, Any]:
    """``what``: ``origin``, or a key named in `KEYS` (or ``k0``, the
    store's first key), dropped as a retired key."""
    return {"call": "drop", "what": what}


def reopen(strict: bool) -> dict[str, Any]:
    return {"call": "reopen", "strict": strict}


def refold() -> dict[str, Any]:
    return {"call": "refold"}


def later_field(n: int) -> dict[str, Any]:
    """A later build's field on the row record `n` points at, signed again
    under the current key so its pointer still verifies."""
    return {"call": "later_field", "id": ulid(n)}


def _keys(store: Store) -> Any:
    return store._keyring.keys_dir


def _retired_name(key: bytes) -> str:
    return f"{STORE_ID}.{blog.fingerprint(key)[: blog._RETIRED_PREFIX_CHARS]}.key"


def _run(store: Store, call: dict[str, Any]) -> Any:
    what = call["call"]
    keys = _keys(store)
    if what == "rekey":
        current = (keys / f"{STORE_ID}.key").read_bytes()
        new = KEYS[call["to"]]
        payload = {
            "old_fingerprint": blog.fingerprint(current),
            "new_fingerprint": blog.fingerprint(new),
        }
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        row = blog.append_row(
            conn,
            key=current,
            kind=blog.REKEY,
            payload=payload,
            session=None,
            ts=REKEY_TS,
        )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            (blog.fingerprint(new),),
        )
        conn.commit()
        (keys / f"{STORE_ID}.key").write_bytes(new)
        files: list[list[Any]] = [[f"{STORE_ID}.key", new.hex()]]
        if call["keep_old"]:
            (keys / _retired_name(current)).write_bytes(current)
            files.append([_retired_name(current), current.hex()])
        call["row"] = [
            row.seq,
            row.ts,
            row.session,
            row.kind,
            row.payload,
            row.prev_mac,
            row.mac,
        ]
        call["meta"] = blog.fingerprint(new)
        call["files"] = files
        return None
    if what == "drop":
        if call["what"] == "origin":
            name = f"{STORE_ID}.origin"
        else:
            name = _retired_name(KEYS[call["what"]])
        (keys / name).unlink()
        call["file"] = name
        return None
    if what == "later_field":
        key = (keys / f"{STORE_ID}.key").read_bytes()
        conn = store.conn
        row = conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac FROM log WHERE mac = "
            "(SELECT log_mac FROM memories WHERE id = ?)",
            (call["id"],),
        ).fetchone()
        payload = json.loads(row[4])
        payload["memory"]["later_field"] = 1
        text = blog.canonical_payload(payload)
        mac = blog.compute_mac(
            key,
            seq=row[0],
            ts=row[1],
            session=row[2],
            kind=row[3],
            payload=text,
            prev_mac=row[5],
        )
        edits: list[tuple[str, list[Any]]] = [
            ("UPDATE log SET payload = ?, mac = ? WHERE seq = ?", [text, mac, row[0]]),
            ("UPDATE memories SET log_mac = ? WHERE id = ?", [mac, call["id"]]),
        ]
        for statement, params in edits:
            conn.execute(statement, params)
        conn.commit()
        call["edits"] = [list(e) for e in edits]
        return None
    if what == "refold":
        store.refold()
        return sorted(store._unaccounted_memory_ids)
    return run_d(store, call)


def cases() -> list[dict[str, Any]]:
    k0_sources = [
        put(1),
        linked(2, link("extends", 1)),
        put(4),
        linked(5, link("extends", 4)),
        put(6),
        linked(7, link("extends", 6)),
        put(8),
        linked(9, link("extends", 8)),
        tomb(1),
        tomb(4),
        tomb(6),
        tomb(8),
    ]
    return [
        {
            # One rekey: the first key retired and held, then dropped, then
            # strict on, then strict off with the origin anchor gone.
            "name": "one_rekey",
            "steps": [
                *k0_sources,
                rekey("k1", keep_old=True),
                reopen(False),
                restore(1),
                {"call": "drop_k0"},
                restore(4),
                reopen(True),
                restore(6),
                reopen(False),
                drop("origin"),
                restore(8),
            ],
        },
        {
            # Two rekeys, the middle key not kept: its segment is foreign,
            # strict or not.
            "name": "two_rekeys_foreign",
            "steps": [
                put(1),
                linked(2, link("extends", 1)),
                tomb(1),
                rekey("z", keep_old=True),
                reopen(False),
                linked(3, link("extends", 1)),
                rekey("k1", keep_old=False),
                reopen(False),
                restore(1),
            ],
        },
        {
            # A hand-edited row whose pointer still verifies: after a
            # refold in the same process the relink passes it over.
            "name": "refold_memo",
            "steps": [
                put(1),
                linked(2, link("extends", 1)),
                linked(3, link("extends", 1)),
                tomb(1),
                sql("UPDATE memories SET body = 'edited by hand' WHERE id = ?", 2),
                refold(),
                restore(1),
            ],
        },
        {
            # A later build's field on a record whose pointer verifies: the
            # relink passes it over for the field, not for its label.
            "name": "later_field_signed",
            "steps": [
                put(1),
                linked(2, link("extends", 1)),
                linked(3, link("extends", 1)),
                tomb(1),
                later_field(2),
                restore(1),
            ],
        },
        {
            # The memo is the connection's: a reopen starts without it, so
            # the hand-edited record is relinked again.
            "name": "refold_then_reopen",
            "steps": [
                put(1),
                linked(2, link("extends", 1)),
                tomb(1),
                sql("UPDATE memories SET body = 'edited by hand' WHERE id = ?", 2),
                refold(),
                reopen(False),
                restore(1),
            ],
        },
    ]


def _run_all(store: Store, call: dict[str, Any]) -> Any:
    if call["call"] == "drop_k0":
        # The store's first key, retired by the rekey before it.
        keys = _keys(store)
        names = sorted(
            p.name
            for p in keys.iterdir()
            if p.name.endswith(".key")
            and p.name != f"{STORE_ID}.key"
            and not any(p.name == _retired_name(k) for k in KEYS.values())
        )
        (keys / names[0]).unlink()
        call["file"] = names[0]
        return None
    return _run(store, call)


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_records_e",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c, _run_all) for c in cases()]
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
    return {"store_records_e.jsonl": text.encode("ascii")}
