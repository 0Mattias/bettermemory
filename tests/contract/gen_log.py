"""Fixture 3: the HMAC-chained log (`bettermemory.log`).

Two fixed keys sign a chain with rows of every kind the product writes or
reads from a v8 import (`kinds`, `payloads`), a ``rekey`` row
from the first key to the second midway (signed, as `store_open` signs
it, by the new key). Each row carries its payload as a
neutral value, the exact stored payload text and the MAC. Tampered
variants carry the expected `verify_chain` report.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from bettermemory import log as blog
from bettermemory.time_utils import isoformat_utc
from bettermemory.store_schema import SCHEMA

from ._common import jsonl, neutral, text_field
from .gen_store_path import log_rows
from .kinds import call_sites, writable_kinds
from .payloads import (
    LEGACY_SHAPES,
    from_shape,
    from_site,
    store_payloads,
    table_payload,
)

KEY_A = bytes(range(32))
KEY_B = bytes(range(32, 64))
STORE_ID = "0123456789abcdef0123456789abcdef"
_BASE = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def kinds() -> list[str]:
    """Every kind the tree writes (`kinds.writable_kinds`) and every kind
    it reads only from a v8 import (`payloads.LEGACY_SHAPES`), without the
    two the chain places itself (``store_created`` first, ``rekey``)."""
    every = set(writable_kinds()) | set(LEGACY_SHAPES)
    every.discard(blog.STORE_CREATED)
    every.discard(blog.REKEY)
    return sorted(every)


#: Payloads that exercise the serialiser rather than a kind's schema:
#: floats, nesting, non-ASCII and astral text, a surrogate, a datetime and
#: a path through ``default=str``. Written under the ``write`` kind.
SERIALISER_PAYLOADS: list[dict[str, Any]] = [
    {"id": "rec0001", "body": "Zürich 東京 \U0001f600", "score": 0.1 + 2},
    {"nested": {"z": [1, 2.5, None, True], "a": {"é": "x"}}, "kind": "write"},
    {"neg_zero": -0.0, "big": 1e300, "tiny": 5e-324, "int_float": 3.0},
    {"surrogate": "file\udcff", "ok": "fine"},
    {"when": _BASE, "path": PurePosixPath("/tmp/a b")},
    {},
]


def entries() -> list[tuple[str, dict[str, Any], str]]:
    """(kind, payload, shape source) for every row after ``store_created``,
    in order: each kind's rows, then the serialiser rows."""
    stored = store_payloads(log_rows())
    sites, _ = call_sites()
    out: list[tuple[str, dict[str, Any], str]] = []
    for kind in kinds():
        seen: list[dict[str, Any]] = []
        table = table_payload(kind)
        if kind in stored:
            seen.append(stored[kind])
            out.append((kind, stored[kind], "store"))
        elif table is not None:
            seen.append(table)
            out.append((kind, table, "table"))
        else:
            for site in sites:
                if site.kind != kind or site.keys is None:
                    continue
                payload = from_site(site)
                if payload not in seen:
                    seen.append(payload)
                    out.append((kind, payload, f"call_site {site.path}:{site.line}"))
        if kind in LEGACY_SHAPES:
            out.append((kind, from_shape(LEGACY_SHAPES[kind]), "legacy"))
        elif not seen:
            out.append((kind, {}, "none"))
    out.extend(("write", p, "serialiser") for p in SERIALISER_PAYLOADS)
    return out


def _ts(i: int) -> str:
    stamp = _BASE.replace(second=(5 + i) % 60, minute=(4 + i // 60) % 60)
    if i % 3 == 0:
        return isoformat_utc(stamp)
    return isoformat_utc(stamp.replace(microsecond=1000 * i % 1000000))


def _session(i: int) -> str | None:
    return [None, "sess-1", "", "sessão-2"][i % 4]


def build_rows() -> tuple[list[dict[str, Any]], list[blog.LogRow]]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    meta: list[dict[str, Any]] = []
    rows: list[blog.LogRow] = []

    def add(
        key: bytes, kind: str, payload: dict[str, Any], i: int, source: str = "chain"
    ) -> None:
        row = blog.append_row(
            conn, key=key, kind=kind, payload=payload, session=_session(i), ts=_ts(i)
        )
        rows.append(row)
        meta.append(
            {"payload": payload, "key": "A" if key == KEY_A else "B", "source": source}
        )

    add(KEY_A, blog.STORE_CREATED, store_payloads(log_rows())[blog.STORE_CREATED], 0)
    rest = entries()
    half = len(rest) // 2
    for i, (kind, payload, source) in enumerate(rest[:half], start=1):
        add(KEY_A, kind, payload, i, source)
    add(
        KEY_B,
        blog.REKEY,
        {
            "old_fingerprint": blog.fingerprint(KEY_A),
            "new_fingerprint": blog.fingerprint(KEY_B),
        },
        half + 1,
    )
    for i, (kind, payload, source) in enumerate(rest[half:], start=half + 2):
        add(KEY_B, kind, payload, i, source)
    conn.close()
    return meta, rows


def _verify(rows: list[blog.LogRow], head: tuple[int, str] | None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        keys_dir = Path(tmp)
        ring = blog.KeyRing(keys_dir, STORE_ID)
        ring.current_path().write_bytes(KEY_B)
        ring.retired_path(blog.fingerprint(KEY_A)).write_bytes(KEY_A)
        if head is not None:
            ring.head_path().write_text(
                json.dumps({"seq": head[0], "mac": head[1], "ts": ""}), encoding="utf-8"
            )
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        report = blog.verify_chain(
            conn, keyring=ring, current_fingerprint=blog.fingerprint(KEY_B)
        )
        conn.close()
    return report.to_dict()


def _variants(rows: list[blog.LogRow]) -> list[dict[str, Any]]:
    """Each variant as a change to the base rows: ``edit`` sets one field of
    the row at ``index`` (0-based), ``drop`` removes rows by index, and
    ``head`` is the head file the ring holds (None: no file)."""
    head = {"seq": rows[-1].seq, "mac": rows[-1].mac}
    rekeys = [i for i, r in enumerate(rows) if r.kind == blog.REKEY]
    return [
        {"variant": "intact", "head": head},
        {"variant": "intact_no_head", "head": None},
        {
            "variant": "intact_stale_head",
            "head": {"seq": rows[-3].seq, "mac": rows[-3].mac},
        },
        {
            "variant": "payload_edited",
            "edit": [3, "payload", '{"id":"edited"}'],
            "head": head,
        },
        {
            "variant": "payload_whitespace",
            "edit": [4, "payload", " " + rows[4].payload],
            "head": head,
        },
        {
            "variant": "ts_edited",
            "edit": [5, "ts", "2026-01-02T03:04:05Z"],
            "head": head,
        },
        {"variant": "session_none_to_empty", "edit": [4, "session", ""], "head": head},
        {
            "variant": "session_empty_to_none",
            "edit": [2, "session", None],
            "head": head,
        },
        {"variant": "kind_edited", "edit": [7, "kind", "write"], "head": head},
        {"variant": "mac_zeroed", "edit": [8, "mac", "0" * 64], "head": head},
        {
            "variant": "mac_uppercase",
            "edit": [9, "mac", rows[9].mac.upper()],
            "head": head,
        },
        {
            "variant": "prev_mac_edited",
            "edit": [10, "prev_mac", "f" * 64],
            "head": head,
        },
        {"variant": "middle_row_deleted", "drop": [12], "head": head},
        {
            "variant": "tail_deleted",
            "drop": [len(rows) - 2, len(rows) - 1],
            "head": head,
        },
        {"variant": "head_mac_wrong", "head": {"seq": rows[-1].seq, "mac": "1" * 64}},
        {
            "variant": "head_past_end",
            "head": {"seq": rows[-1].seq + 1, "mac": rows[-1].mac},
        },
        {
            "variant": "rekey_payload_broken",
            "edit": [rekeys[0], "payload", "{}"],
            "head": head,
        },
    ]


def _apply(rows: list[blog.LogRow], variant: dict[str, Any]) -> list[blog.LogRow]:
    out = list(rows)
    if "edit" in variant:
        index, name, value = variant["edit"]
        out[index] = out[index]._replace(**{name: value})
    if "drop" in variant:
        out = [r for i, r in enumerate(out) if i not in variant["drop"]]
    return out


def _row_record(row: blog.LogRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "ts": row.ts,
        "session": row.session,
        "kind": row.kind,
        **text_field("payload", row.payload),
        "payload_hex": row.payload.encode("utf-8").hex(),
        "prev_mac": row.prev_mac,
        "mac": row.mac,
    }


def generate() -> dict[str, bytes]:
    meta, rows = build_rows()
    records: list[Any] = [
        {
            "fixture": "log_chain",
            "keys": {
                "A": KEY_A.hex(),
                "B": KEY_B.hex(),
                "A_fingerprint": blog.fingerprint(KEY_A),
                "B_fingerprint": blog.fingerprint(KEY_B),
            },
            "store_id": STORE_ID,
            "genesis_mac": blog.GENESIS_MAC,
            "mac_recipe": (
                "HMAC-SHA256(key) over parts (str(seq), ts, session, kind, "
                "payload, prev_mac): None -> 0x00; str -> 0x01 + "
                "u64 big-endian len(utf8) + utf8"
            ),
            "head": {"seq": rows[-1].seq, "mac": rows[-1].mac},
            "kinds": kinds(),
            "legacy_kinds": sorted(LEGACY_SHAPES),
            "shape_sources": (
                "store: a row the store-path build wrote, values synthetic; "
                "call_site <file:line>: the keys that call names, values of "
                "the type their expression reads as; legacy: a kind written "
                "only by a v8 import, keys and types of such rows; none: no "
                "source shapes it; table: a membership mutation from its table's columns; serialiser: serialiser coverage; chain: "
                "the chain's own rows"
            ),
            "verify_setup": (
                "key ring holds B as current and A retired; no origin anchor; "
                "current_fingerprint = B_fingerprint; head file as given"
            ),
        }
    ]
    for m, row in zip(meta, rows, strict=True):
        records.append(
            {
                "row": _row_record(row),
                "payload_input": neutral(m["payload"]),
                "key": m["key"],
                "shape_source": m["source"],
            }
        )
    for variant in _variants(rows):
        head = variant["head"]
        report = _verify(
            _apply(rows, variant), None if head is None else (head["seq"], head["mac"])
        )
        records.append({**variant, "report": report})
    return {"log_chain.jsonl": jsonl(records)}
