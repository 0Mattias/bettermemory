"""Fixture: the chain's segment map with the caller's key, and the rows
that say a store's history was replaced.

``log_segments.jsonl`` pins `log.chain_trust` (and so `chain_segments`)
on an in-memory log table: per case a chain (built row by row as
`gen_log_ring` builds them), a key ring given as the files of a keys
directory, the store's meta name (``current_fingerprint``) and the key
the caller holds (``current_key``). The chains and rings are written
once in the header and named by each case. The caller's key crosses
every chain: none, the ring's current key, a key the ring holds only as
retired, a key the ring does not hold, and a key whose fingerprint is the
meta name while the ring's current file holds another. Each segment is
recorded with its first seq, the name the log gives it, the fingerprint of
the key that holds it, whether it is foreign and its first row's seq,
beside the origin check's status.

``log_replaced.jsonl`` pins `history_replaced_kind` and
`history_replaced_row` over entries (JSON texts) holding every JSON type
where the two read. Neither file depends on the Python release, the
platform or the Unicode data.

``log_replaced_store.jsonl`` pins `append_history_replaced` on the store
`Store.create` makes under ``store_create.json``'s fixed values, opened as
the owner, as ``store_tx.jsonl``'s cases are: called with no transaction
open, inside an open transaction (committed, or failing after the call),
with a kind that is not one, and on a connection under ``PRAGMA
query_only``. Recorded: the answer, the rows appended (every column), the
head file's text and the clock readings. Its gates are ``store_tx``'s.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import log as blog
from bettermemory.store import Store
from bettermemory.store_schema import SCHEMA

from ._common import jsonl, text_field
from ._fixed import Ticker, fixed, ulid_mint
from ._sandbox import world
from .gen_log import STORE_ID as CREATED_ID
from .gen_log_ring import (
    _PREFIX,
    FP_A,
    FP_B,
    FP_C,
    KEY_A,
    KEY_B,
    KEY_C,
    STORE_ID,
    _hex,
    _note,
    _origin_doc,
    _payload,
    _row_record,
    chain,
    rekey_text,
)
from .gen_store_open import ENGINE, _engine, _made
from .gen_store_write import _last_seq, log_rows

KEY_D = bytes(range(96, 128))
FP_D = blog.fingerprint(KEY_D)


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------


def _chains() -> dict[str, list[blog.LogRow]]:
    created = (KEY_A, blog.STORE_CREATED, _payload({}))
    two = chain(
        [
            created,
            _note(KEY_A, 1),
            _note(KEY_A, 2),
            (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
            _note(KEY_B, 3),
            _note(KEY_B, 4),
        ]
    )
    rewritten = list(two)
    rewritten[0] = rewritten[0]._replace(payload=_payload({"edited": True}))
    return {
        "empty": [],
        "no_rekey_a": chain([created, _note(KEY_A, 1)]),
        "no_rekey_b": chain(
            [(KEY_B, blog.STORE_CREATED, _payload({})), _note(KEY_B, 1)]
        ),
        "no_rekey_c": chain(
            [(KEY_C, blog.STORE_CREATED, _payload({})), _note(KEY_C, 1)]
        ),
        "two_key": two,
        "several": chain(
            [
                created,
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
                _note(KEY_B, 1),
                (KEY_C, blog.REKEY, rekey_text(FP_B, FP_C)),
                _note(KEY_C, 2),
                (KEY_A, blog.REKEY, rekey_text(FP_C, FP_A)),
                _note(KEY_A, 3),
                (KEY_C, blog.REKEY, rekey_text(FP_A, FP_C)),
            ]
        ),
        "rekey_at_first_row": chain(
            [
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
                _note(KEY_B, 1),
                (KEY_C, blog.REKEY, rekey_text(FP_B, FP_C)),
            ]
        ),
        "rekey_names_nothing": chain(
            [
                created,
                (KEY_B, blog.REKEY, _payload({})),
                _note(KEY_B, 1),
                (KEY_C, blog.REKEY, rekey_text(FP_B, FP_C)),
            ]
        ),
        "rekey_first_names_nothing": chain(
            [
                created,
                (KEY_B, blog.REKEY, "not json"),
                (KEY_C, blog.REKEY, rekey_text(FP_B, FP_C)),
            ]
        ),
        "name_not_fingerprint": chain(
            [
                created,
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B.upper())),
                _note(KEY_B, 1),
                (KEY_C, blog.REKEY, rekey_text("\ud800", "\udcff" + FP_C[1:])),
                (KEY_A, blog.REKEY, rekey_text(FP_C, FP_A)),
            ]
        ),
        "leading_names_other": chain(
            [
                (KEY_C, blog.STORE_CREATED, _payload({})),
                _note(KEY_C, 1),
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
                _note(KEY_B, 2),
            ]
        ),
        "leading_names_unheld": chain(
            [
                (KEY_A, blog.STORE_CREATED, _payload({})),
                (KEY_B, blog.REKEY, rekey_text(FP_D, FP_B)),
                (KEY_D, blog.REKEY, rekey_text(FP_B, FP_D)),
                (KEY_A, blog.REKEY, rekey_text(FP_D, FP_A)),
            ]
        ),
        "first_row_rewritten": rewritten,
        "seq_zero_and_below": chain(
            [
                (KEY_D, blog.STORE_CREATED, _payload({})),
                (KEY_B, blog.REKEY, rekey_text(FP_D, FP_A)),
                (KEY_A, blog.REKEY, rekey_text(FP_C, FP_A)),
                _note(KEY_A, 1),
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
            ],
            seqs=[-5, 0, 1, 2, 3],
        ),
        "i64_ends": chain(
            [
                created,
                (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
            ],
            seqs=[1, 2**63 - 1],
        ),
    }


# ---------------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------------


def _rings(chains: dict[str, list[blog.LogRow]]) -> dict[str, dict[str, Any]]:
    two = chains["two_key"]
    a, b = (FP_A[:_PREFIX], KEY_A), (FP_B[:_PREFIX], KEY_B)
    c = (FP_C[:_PREFIX], KEY_C)

    def r(current: bytes | None, retired: list[Any], origin: bytes | None = None):
        return {"current": current, "retired": retired, "origin": origin}

    wrong = json.dumps({"seq": 2, "mac": "0" * 64}).encode()
    return {
        "b_a": r(KEY_B, [a]),
        "b_only": r(KEY_B, []),
        "a_only": r(KEY_A, []),
        "b_c": r(KEY_B, [c]),
        "c_a_b": r(KEY_C, [a, b]),
        "retired_only": r(None, [a, b]),
        "empty": r(None, []),
        "anchor_before": r(KEY_B, [], _origin_doc(two[1], FP_B)),
        "anchor_last_before": r(KEY_B, [], _origin_doc(two[2], None)),
        "anchor_at": r(KEY_B, [], _origin_doc(two[3], FP_B)),
        "anchor_after": r(KEY_B, [], _origin_doc(two[4], FP_B)),
        "anchor_first": r(KEY_B, [], _origin_doc(two[0], FP_A)),
        "anchor_mismatch": r(KEY_B, [], wrong),
        "anchor_unverifiable": r(KEY_B, [], _origin_doc(two[1], FP_A)),
        "anchor_signature": r(KEY_B, [a], _origin_doc(two[1], FP_B)),
        "anchor_none_held": r(None, [], _origin_doc(two[1], FP_A)),
    }


# The caller's key: (label, current_fingerprint, current_key). The meta
# name is the ring's current key's in each but the last, where the caller
# opened the store under C while the ring's current file holds another.
CALLERS: list[tuple[str, str, bytes | None]] = [
    ("none", FP_B, None),
    ("current", FP_B, KEY_B),
    ("retired", FP_A, KEY_A),
    ("unheld", FP_B, KEY_D),
    ("meta_other_file", FP_C, KEY_C),
]

SHAPES: list[tuple[str, list[str]]] = [
    ("empty", ["b_a", "empty"]),
    ("no_rekey_a", ["b_a", "b_only", "a_only"]),
    ("no_rekey_b", ["b_a", "empty"]),
    ("no_rekey_c", ["b_a", "b_c"]),
    ("two_key", ["b_a", "b_only", "a_only", "retired_only", "empty", "c_a_b"]),
    ("several", ["b_a", "b_c", "c_a_b", "empty"]),
    ("rekey_at_first_row", ["b_a", "b_c", "empty"]),
    ("rekey_names_nothing", ["b_a", "b_c"]),
    ("rekey_first_names_nothing", ["b_a", "b_c"]),
    ("name_not_fingerprint", ["b_a", "c_a_b"]),
    ("leading_names_other", ["b_a", "b_c", "b_only"]),
    ("leading_names_unheld", ["b_a", "b_only"]),
    ("first_row_rewritten", ["b_a", "b_only"]),
    ("seq_zero_and_below", ["b_a", "c_a_b"]),
    ("i64_ends", ["b_a", "b_only"]),
    (
        "two_key",
        [
            "anchor_before",
            "anchor_last_before",
            "anchor_at",
            "anchor_after",
            "anchor_first",
            "anchor_mismatch",
            "anchor_unverifiable",
            "anchor_signature",
            "anchor_none_held",
        ],
    ),
    ("several", ["anchor_before", "anchor_at"]),
]


def _keyring(tmp: Path, keys: dict[str, Any]) -> blog.KeyRing:
    kr = blog.KeyRing(tmp, STORE_ID)
    if keys["current"] is not None:
        kr.current_path().write_bytes(keys["current"])
    for name, key in keys["retired"]:
        (tmp / f"{STORE_ID}.{name}.key").write_bytes(key)
    if keys["origin"] is not None:
        kr.origin_path().write_bytes(keys["origin"])
    return kr


_LETTERS = {FP_A: "A", FP_B: "B", FP_C: "C", FP_D: "D"}


def _short(name: str) -> str:
    """A fingerprint of one of the four keys as ``@`` and its letter."""
    return "@" + _LETTERS[name] if name in _LETTERS else name


def _trust(
    rows: list[blog.LogRow],
    keys: dict[str, Any],
    current_fingerprint: str,
    current_key: bytes | None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        kr = _keyring(Path(tmp), keys)
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        try:
            trust = blog.chain_trust(
                conn,
                keyring=kr,
                current_fingerprint=current_fingerprint,
                current_key=current_key,
            )
            listed = blog.chain_segments(
                conn,
                keyring=kr,
                current_fingerprint=current_fingerprint,
                current_key=current_key,
            )
        except Exception as exc:  # noqa: BLE001 - the error type is the fixture
            return {"error": type(exc).__name__}
        finally:
            conn.close()
    assert listed == trust.segments
    segments = []
    for s in trust.segments:
        assert s.first_row is not None and s.first_row.seq == s.first_seq
        segments.append(
            {
                "first_seq": s.first_seq,
                **text_field("key_fingerprint", _short(s.key_fingerprint)),
                "key": None if s.key is None else _short(blog.fingerprint(s.key)),
                "held": s.held,
                "foreign": s.foreign,
                "first_row_seq": s.first_row.seq,
            }
        )
    origin = trust.origin.to_dict()
    origin["signer"] = (
        None if trust.origin.signer is None else _short(trust.origin.signer)
    )
    return {"segments": segments, "origin": origin}


def generate() -> dict[str, bytes]:
    chains = _chains()
    rings = _rings(chains)
    records: list[Any] = [
        {
            "fixture": "log_segments",
            "keys": {
                name: key.hex()
                for name, key in (
                    ("A", KEY_A),
                    ("B", KEY_B),
                    ("C", KEY_C),
                    ("D", KEY_D),
                )
            },
            "store_id": STORE_ID,
            "chains": {
                name: [_row_record(r) for r in rows] for name, rows in chains.items()
            },
            "rings": {
                name: {
                    "current": _hex(k["current"]),
                    "retired": [[n, key.hex()] for n, key in k["retired"]],
                    "origin": _hex(k["origin"]),
                }
                for name, k in rings.items()
            },
            "callers": {
                label: [
                    _short(fp),
                    None if key is None else _short(blog.fingerprint(key)),
                ]
                for label, fp, key in CALLERS
            },
            "result": (
                "case: chain/ring/caller; caller: [current_fingerprint, "
                "current_key]; a fingerprint of key X spelled @X. "
                "chain_trust(conn, keyring, current_fingerprint, current_key): "
                "each segment's first_seq, key_fingerprint, the fingerprint of "
                "its key (null when not held), held, foreign, first_row's seq; "
                "the origin check's to_dict() with its signer; the log table "
                "holds the chain's rows, chain_trust reads seq > 0"
            ),
        }
    ]
    for chain_name, ring_names in SHAPES:
        for ring_name in ring_names:
            for label, current_fp, current_key in CALLERS:
                records.append(
                    {
                        "case": f"{chain_name}/{ring_name}/{label}",
                        **_trust(
                            chains[chain_name],
                            rings[ring_name],
                            current_fp,
                            current_key,
                        ),
                    }
                )
    return {"log_segments.jsonl": jsonl(records)}


# ---------------------------------------------------------------------------
# history_replaced_kind and history_replaced_row
# ---------------------------------------------------------------------------

_ABSENT = object()


def _entry(**fields: Any) -> str:
    return json.dumps({k: v for k, v in fields.items() if v is not _ABSENT})


def _entries() -> list[str]:
    out: list[str] = []
    kinds = [
        _ABSENT,
        "store_restored",
        "history_accepted",
        "rekey",
        "",
        "Store_Restored",
        1,
        None,
        True,
        ["history_accepted"],
        {"kind": "history_accepted"},
    ]
    accepted = [_ABSENT, True, False, 1, 1.0, "true", None, [True]]
    for kind in kinds:
        for flag in accepted:
            out.append(_entry(kind=kind, accepted_history=flag))
    rows = [
        _ABSENT,
        None,
        {},
        {"seq": 5},
        {"seq": 5, "mac": "ab"},
        {"seq": 5, "mac": None},
        {"seq": 5, "mac": 7},
        {"mac": "ab"},
        {"seq": True, "mac": "ab"},
        {"seq": False},
        {"seq": 5.0},
        {"seq": "5"},
        {"seq": None},
        {"seq": [5]},
        {"seq": 2**70, "mac": "\udcff"},
        {"seq": -(2**63) - 1},
        {"seq": 0, "mac": ""},
        [5],
        "row",
        5,
    ]
    seqs = [_ABSENT, 7, True, 7.0, 2**64, -1, None, "7", [7]]
    for row in rows:
        for seq in seqs:
            out.append(_entry(row=row, seq=seq))
    # Repeated keys: json.loads keeps the last.
    out += [
        '{"kind": "rekey", "kind": "history_accepted"}',
        '{"kind": "history_accepted", "kind": 1}',
        '{"row": {"seq": 1}, "row": null, "seq": 3}',
        '{"row": {"seq": "x", "seq": 4, "mac": "m", "mac": 9}}',
        '{"accepted_history": true, "accepted_history": 1}',
    ]
    out += ["{}", '{"row": {"seq": 1}, "kind": "history_accepted", "seq": 2}']
    return out


def generate_replaced() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "log_replaced",
            "table": [[k, list(v)] for k, v in blog.HISTORY_REPLACED.items()],
            "result": (
                "kind: history_replaced_kind(json.loads(entry)); row: "
                "json.dumps(history_replaced_row(json.loads(entry)))"
            ),
        }
    ]
    for text in _entries():
        entry = json.loads(text)
        records.append(
            {
                "entry": text,
                "kind": blog.history_replaced_kind(entry),
                "row": json.dumps(blog.history_replaced_row(entry)),
            }
        )
    return {"log_replaced.jsonl": jsonl(records)}


# ---------------------------------------------------------------------------
# append_history_replaced on a store
# ---------------------------------------------------------------------------

LOG0 = datetime(2002, 3, 4, 5, 6, 7, 123456, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=2, microseconds=3)
WALL0 = datetime(2004, 5, 6, 7, 8, 9, 456000, tzinfo=timezone.utc)
WALL_STEP = timedelta(minutes=3)


class OwnerFails(Exception):
    """The owner's transaction failing after the call."""


def _store_cases() -> list[dict[str, Any]]:
    """Each: name and steps; a step calls append_history_replaced with
    ``kind`` and ``entry`` (a JSON text) in ``mode``: ``own`` (no
    transaction open), ``joined`` (inside one that commits) or
    ``joined_fails`` (inside one that fails after the call); ``query_only``
    sets the pragma on the store's connection first."""

    def call(kind: str, entry: str, mode: str = "own", query_only: bool = False):
        return {"kind": kind, "entry": entry, "mode": mode, "query_only": query_only}

    restore = json.dumps(
        {
            "at": "2001-01-01T00:00:00Z",
            "row": None,
            "from": {"seq": 3, "mac": "ab"},
            "note": "café \udcff",
            "big": 2**70,
            "list": [1, 2.5, None, True, {"z": 1, "a": 2}],
        }
    )
    accepted = json.dumps(
        {
            "engine_version": "0.1",
            "accepted_history": True,
            "row": {"seq": 9, "mac": "x"},
            "kind": "store_restored",
            "seq": 4,
            "tail": "last",
        }
    )
    return [
        {"name": "restore_own", "steps": [call("store_restored", restore)]},
        {"name": "accepted_own", "steps": [call("history_accepted", accepted)]},
        {
            "name": "twice_own",
            "steps": [
                call("history_accepted", "{}"),
                call("store_restored", '{"row": 1, "Row": 2}'),
            ],
        },
        {
            "name": "joined_commits",
            "steps": [call("store_restored", restore, "joined")],
        },
        {
            "name": "joined_fails",
            "steps": [
                call("history_accepted", accepted, "joined_fails"),
                call("store_restored", "{}"),
            ],
        },
        {
            "name": "not_a_kind",
            "steps": [
                call("rekey", "{}"),
                call('it\'s "x"', "{}"),
                call("\udcff\té", "{}"),
                call("", "{}", query_only=True),
            ],
        },
        {
            "name": "query_only",
            "steps": [
                call("store_restored", restore, query_only=True),
                call("history_accepted", "{}", "joined", query_only=True),
            ],
        },
    ]


def _store_step(store: Store, file: Path, step: dict[str, Any]) -> dict[str, Any]:
    entry = json.loads(step["entry"])
    if step["query_only"]:
        store.conn.execute("PRAGMA query_only = ON")
    try:
        if step["mode"] == "own":
            answer = blog.append_history_replaced(store, step["kind"], entry)
        else:
            try:
                with store._transaction():
                    answer = blog.append_history_replaced(store, step["kind"], entry)
                    if step["mode"] == "joined_fails":
                        raise OwnerFails("after the call")
            except OwnerFails:
                return {"answer": answer, "owner_failed": True}
        return {"answer": answer}
    except ValueError as exc:
        return {"error": "ValueError", **text_field("text", str(exc))}
    except sqlite3.Error as exc:
        return {"error": type(exc).__name__}
    finally:
        if step["query_only"]:
            store.conn.execute("PRAGMA query_only = OFF")


def _run_store_case(case_def: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        case = Path(raw) / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        head = case / "keys" / f"{CREATED_ID}.head"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
            try:
                for i, step in enumerate(case_def["steps"]):
                    log = Ticker(LOG0 + timedelta(minutes=i), LOG_STEP)
                    wall = Ticker(WALL0 + timedelta(hours=i), WALL_STEP)
                    before = _last_seq(file)
                    with fixed(log=log, wall=wall, mint=ulid_mint(1000 * i)):
                        out = _store_step(store, file, step)
                    steps.append(
                        {
                            **{k: v for k, v in step.items() if k != "kind"},
                            **text_field("kind", step["kind"]),
                            **out,
                            "log_clock": log.stamps(),
                            "wall_clock": wall.stamps(),
                            "rows": log_rows(file, before),
                            "head": head.read_text(encoding="utf-8")
                            if head.is_file()
                            else None,
                        }
                    )
            finally:
                store.close()
    return {"case": case_def["name"], "steps": steps}


def generate_store() -> dict[str, bytes]:
    lines: list[Any] = [
        {
            "fixture": "log_replaced_store",
            "engine_version": ENGINE,
            "store_id": CREATED_ID,
            "result": (
                "each step: append_history_replaced(store, kind, json.loads(entry)) "
                "as mode says; answer {seq, mac} or null, or error and text; the "
                "rows appended (seq, ts, session, kind, payload, prev_mac, mac), "
                "the head file's text after, the clocks read"
            ),
        }
    ]
    with world():
        lines += [_run_store_case(c) for c in _store_cases()]
    return {"log_replaced_store.jsonl": jsonl(lines)}
