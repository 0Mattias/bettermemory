"""Fixture N1e: the fold check, `refold` and `log_verify`.

``fold.jsonl`` records the reference's `store_events.refold` and
`store_events.log_verify` reports for store files the native build rebuilds
itself: every case of ``store_files.jsonl`` (its base from
``store_bases.json`` and its recipe), the committed ``store_real.sqlite``,
and stores made from the intact base by a prelude and a recipe:

- a prelude is SQL (and a head file) this generator derives once, through
  the reference: signed log rows appended with the base's key (gen_log's
  ``KEY_A``, at a fixed stamp) and, for a mutation applied, the table rows
  the reference's own `store_rows._apply_mutation` writes for it, as the
  rows that changed (so the result is what the live write path leaves);
- ``populated`` fills every folded table and every membership table;
  ``drift`` signs an ``index_rebuilt`` row naming another tokenizer;
  ``bad_*`` sign rows whose payload cannot be applied (valid MACs);
- ``every_kind`` applies every kind in `MUTATION_KINDS` at least once
  (asserted: each must be applied without error), every delete kind and
  membership deletes among them;
- a recipe edits the tables behind the log (a planted, an edited and a
  deleted row in each table, the index text alone, hostile values).

Preludes are written once in the header and named by each case (a list,
applied in order). Runs of more than 64 ASCII characters are written as an
SQL expression built from a count, and a table entry with nothing
unaccounted or missing is written as its row count alone. The generator
asserts that no recipe makes a float: its SQL is scanned
(`_no_float_in_recipe`), and the built store is compared with the one its
prelude alone builds (`_no_float_made`: no changed cell is float text in a
TEXT-affinity column, none is a REAL). SQLite would store a REAL in a
TEXT-affinity column as its own version's spelling. It also asserts that every `*_edited` case reports something its
unedited base does not, except the blind spots in `BLIND_SPOTS`.

Each store is opened as the read-only commands open one
(`store_schema.connect_read_only`), and the two reports are taken on a
reader that carries what `refold` and `log_verify` read from a `Store`
(the connection, the key ring, meta's fingerprint and store id). A report
holds no path (``path`` is left out). A store the reference refuses to
open records the refusal's class; a report the reference raises out of
records the exception's class. Column values in labels are spelled as in
``store_files.jsonl`` (a float by its bits, a blob as hex).

Gates. The fold validates every record payload through the pydantic
models (which payloads are applied and each ValidationError's count are
pydantic-core's), and spells each memory's index text with the tokenizer,
whose fingerprint (compared with the one the log signs) and token text are
the Unicode data's. So it is gated on pydantic-core and Unicode. Not on
the Python patch release: the texts it records (`json`'s decode errors,
`int()`'s, sqlite3's binding errors) are 3.13's and do not change within
it, and no datetime goes through `fromisoformat`. Not on SQLite: the
stores are logical (SQL), no float is written into a text column, and
the native build is compared against its own SQLite (no gate there, as
for ``store_files.jsonl``). Not on the platform: no score bits, and no
path is recorded. It runs on every platform (file URIs come from
`connect_read_only`, nothing POSIX-only).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, cast

from bettermemory import store_events
from bettermemory.log import KeyRing, compute_mac
from bettermemory.store_rows import _apply_mutation, _known_columns
from bettermemory.store_schema import (
    _TABLE_KEYS,
    FOLDED_TABLES,
    MEMBERSHIP_TABLES,
    connect_read_only,
)
from bettermemory.store_admission import StoreRefusedError

from . import gen_store_files as sf
from ._common import jsonl
from .gen_log import KEY_A

TS = "2001-02-03T04:05:06.000000Z"
M2 = "01M7GGTCEEBDB241Y1CQTZHFS2"  # the intact base's memory at seq 3
M3 = "01M7GGTCEH0000000000000003"
M4 = "01M7GGTCEH0000000000000004"
M5 = "01M7GGTCEH0000000000000005"
T6 = "01M7GGTCEH0000000000000006"
E7 = "01M7GGTCEH0000000000000007"


def _memory(mid: str, body: str, links: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "filename": None,
        "memory": {
            "actor": None,
            "body": body,
            "category": None,
            "claims": [],
            "confidence": "medium",
            "corroborations": 0,
            "created": TS,
            "id": mid,
            "last_corroborated": None,
            "last_verified_at": None,
            "links": links,
            "origin": None,
            "scopes": ["fixture"],
            "source": "explicit-statement",
            "updated": TS,
            "verified_absent_paths": [],
            "verified_commits": [],
            "verified_head": None,
            "verified_paths": [],
            "verified_versions": [],
        },
        "provenance": "local",
    }


def _tombstone(tid: str, **extra: Any) -> dict[str, Any]:
    record = dict(_memory(tid, "Removed.\n", [])["memory"])
    for k in ("links", "corroborations", "last_corroborated"):
        record.pop(k)
    record.update(removed=TS, removed_reason="superseded", removed_session=None)
    return {
        "corroborations": 2,
        "filename": "old.md",
        "last_corroborated": TS,
        "links": [{"type": "depends_on", "target_id": M2}],
        "provenance": "imported",
        "tombstone": record,
        **extra,
    }


CHECKOUT = {
    "id": "co1",
    "project": "p1",
    "machine": "m1",
    "kind": "git",
    "object_device": "1",
    "object_inode": "2",
    "object_birth": None,
    "common_device": "1",
    "common_inode": "3",
    "common_birth": None,
    "git_dir": "/fixture/repo/.git",
    "token": "tok",
    "token_status": "live",
    "stamps_json": ["a", 1],
    "last_head": "c" * 40,
    "branch": "main",
    "remotes_json": [{"name": "origin"}],
    "manifest_json": None,
    "legacy": False,
    "retired": 0,
    "registered": TS,
    "unresolved_json": {"why": "x"},
}

#: Every table of the fold gets rows: (kind, payload) applied in order.
POPULATED: list[tuple[str, Any]] = [
    ("memory_put", _memory(M3, "Third.\n", [{"type": "extends", "target_id": M2}])),
    (
        "memory_put",
        _memory(
            M4,
            "Fourth links twice.\n",
            [
                {"type": "depends_on", "target_id": M3, "note": "n"},
                {"type": "depends_on", "target_id": M2, "note": "same"},
                {"type": "depends_on", "target_id": M2, "note": "same"},
            ],
        ),
    ),
    ("memory_put", _memory(M5, "Fifth.\n", [{"type": "depends_on", "target_id": M2}])),
    ("memory_delete", {"id": M3}),
    ("tombstone_put", _tombstone(T6)),
    (
        "episode_put",
        {
            "episode": {
                "body": "An episode.\n",
                "created": TS,
                "id": E7,
                "is_floor": True,
                "origin": None,
                "scopes": ["fixture"],
                "session_id": "sess_fixture",
                "swarm_id": "swarm-1",
                "takeaway": "t",
            }
        },
    ),
    (
        "verification_put",
        {
            "memory_id": M2,
            "machine_id": "m1",
            "verified_at": TS,
            "verified_head": "d" * 40,
            "verified_paths": ["a.py"],
        },
    ),
    ("verification_put", {"memory_id": M5, "machine_id": None, "verified_at": TS}),
    (
        "conflict_put",
        {
            "conflict": {
                "id": "c1",
                "a_id": M2,
                "b_id": M5,
                "summary_a": "a",
                "summary_b": "b",
                "similarity": 0.75,
                "method": "jaccard",
                "detector": "write",
                "created": TS,
                "status": "open",
                "verdict_ts": None,
                "note": None,
                "verdict_hash_a": None,
                "verdict_hash_b": None,
            }
        },
    ),
    (
        "import_put",
        {
            "source": "v8:/fixture/a.md",
            "content_hash": "ab" * 32,
            "imported_at": TS,
            "memory_id": M2,
        },
    ),
    (
        "quarantine_put",
        {
            "name": "bad.md",
            "reason": "parse",
            "detail": "x",
            "remote": None,
            "at": TS,
            "size": 12,
            "sha256": "cd" * 32,
        },
    ),
    (
        "machine_put",
        {
            "machine": {
                "id": "m1",
                "facts_json": {"os": "x"},
                "predecessor": None,
                "minted": TS,
            }
        },
    ),
    (
        "project_put",
        {
            "project": {
                "id": "p1",
                "name": "alpha",
                "merged_into": None,
                "provisional": None,
                "parent": None,
                "tenant": None,
                "folder": 1,
                "boundary": True,
                "minted": TS,
            }
        },
    ),
    (
        "project_witness_put",
        {
            "project_witness": {
                "project": "p1",
                "commit_id": "e" * 40,
                "arrival": 3,
                "source": "head",
                "fingerprint": None,
            }
        },
    ),
    (
        "project_blob_put",
        {"project_blob": {"project": "p1", "blob": "b1", "witness": "w"}},
    ),
    ("project_remote_put", {"project_remote": {"project": "p1", "remote_key": "r1"}}),
    ("project_remote_put", {"project_remote": {"project": "p1", "remote_key": "r2"}}),
    ("project_remote_delete", {"project": "p1", "remote_key": "r2"}),
    (
        "project_file_put",
        {"project_file": {"file_id": "f1", "project": "p1", "evidence": "file"}},
    ),
    (
        "declared_remote_put",
        {"declared_remote": {"remote_key": "r1", "project": "p1", "principal": "me"}},
    ),
    ("checkout_put", {"checkout": CHECKOUT}),
    (
        "checkout_view_put",
        {
            "checkout_view": {
                "checkout": "co1",
                "machine": "m1",
                "path": "/fixture/repo",
                "first_seen": TS,
                "last_seen": TS,
                "seen_order": 1,
            }
        },
    ),
    (
        "home_put",
        {
            "home": {
                "record_id": M2,
                "kind": "memory",
                "project": "p1",
                "checkout": "co1",
                "rule": "checkout",
                "decided": TS,
            }
        },
    ),
    (
        "proposal_put",
        {
            "proposal": {
                "id": "q1",
                "checkout": "co1",
                "kind": "join",
                "candidates_json": ["p1"],
                "evidence_json": {"k": [1, 2]},
                "reason": "r",
                "line": "l",
                "state": "open",
                "answer": None,
                "answered_by": None,
                "signature_json": None,
                "opened": TS,
            }
        },
    ),
    (
        "standing_put",
        {
            "standing": {
                "record_id": M2,
                "probability": 0.75,
                "body_sha256": "f" * 64,
                "source": "asked",
                "answered_by": "owner",
                "answered": TS,
            }
        },
    ),
    ("returned_put", {"returned": {"record_id": M5, "via": "restore", "returned": TS}}),
    (
        "native_member_put",
        {"native_member": {"member_kind": "k", "key_text": "x", "row_text": "{}"}},
    ),
]

M8 = "01M7GGTCEH0000000000000008"
E9 = "01M7GGTCEH0000000000000009"
T10 = "01M7GGTCEH000000000000000A"


def _second(kind: str, row: dict[str, Any]) -> list[tuple[str, Any]]:
    """A membership row put again over its own key (INSERT OR REPLACE
    overwriting it), then under another key and deleted again."""
    from bettermemory.store_schema import _MEMBERSHIP_SPECS

    spec = _MEMBERSHIP_SPECS[kind]
    other = {**row, **{k: f"{row[k]}-2" for k in spec.key}}
    return [
        (f"{kind}_put", {kind: row}),
        (f"{kind}_put", {kind: other}),
        (f"{kind}_delete", {k: other[k] for k in spec.key}),
    ]


#: Every mutation kind applied on the populated prelude: each delete, a
#: re-put replacing a memory's links, delete then tombstone then restore of
#: one id, a NULL-machine verification replaced, a checkout without its
#: optional column, a standing probability given as an int.
EVERY_KIND: list[tuple[str, Any]] = [
    (
        "memory_put",
        _memory(
            M4, "Fourth, links replaced.\n", [{"type": "extends", "target_id": M5}]
        ),
    ),
    ("memory_put", _memory(M8, "Eighth.\n", [{"type": "depends_on", "target_id": M2}])),
    ("memory_delete", {"id": M8}),
    (
        "tombstone_put",
        {**_tombstone(M8), "links": [{"type": "depends_on", "target_id": M2}]},
    ),
    ("tombstone_delete", {"id": M8}),
    (
        "memory_put",
        _memory(M8, "Eighth, restored.\n", [{"type": "depends_on", "target_id": M2}]),
    ),
    ("tombstone_put", _tombstone(T10)),
    ("tombstone_delete", {"id": T10}),
    (
        "episode_put",
        {
            "episode": {
                "body": "Another.\n",
                "created": TS,
                "id": E9,
                "session_id": "sess_fixture",
            }
        },
    ),
    ("episode_delete", {"id": E9}),
    (
        "verification_put",
        {"memory_id": M5, "machine_id": None, "verified_at": "2001-02-04T00:00:00Z"},
    ),
    ("verification_put", {"memory_id": M2, "machine_id": "m2", "verified_at": TS}),
    ("verification_delete", {"memory_id": M2, "machine_id": "m2"}),
    (
        "conflict_put",
        {
            "conflict": {
                "id": "c2",
                "a_id": M2,
                "b_id": M8,
                "summary_a": "a",
                "summary_b": "b",
                "similarity": 0.5,
                "method": "m",
                "detector": "d",
                "created": TS,
                "status": "open",
            }
        },
    ),
    ("conflict_delete", {"id": "c2"}),
    (
        "import_put",
        {"source": "v8:/fixture/b.md", "content_hash": "ef" * 32, "imported_at": TS},
    ),
    ("import_delete", {"source": "v8:/fixture/b.md"}),
    ("quarantine_put", {"name": "worse.md", "reason": "r", "at": TS}),
    ("quarantine_delete", {"name": "worse.md"}),
    (
        "checkout_put",
        {
            "checkout": {k: v for k, v in CHECKOUT.items() if k != "unresolved_json"}
            | {"id": "co2"}
        },
    ),
    # Success-path branches: a tombstone's corroborations as a string and
    # its links null, a memory with a filename, verification lists given
    # as a dict and as a string, re-puts over the same key.
    (
        "tombstone_put",
        _tombstone(T10, corroborations="3", links=None),
    ),
    ("tombstone_delete", {"id": T10}),
    (
        "memory_put",
        {**_memory(M8, "Eighth, filed.\n", []), "filename": "eighth.md"},
    ),
    (
        "verification_put",
        {
            "memory_id": M2,
            "machine_id": "m3",
            "verified_at": TS,
            "verified_paths": {"a.py": 1, "b.py": 2},
            "verified_commits": "ab",
        },
    ),
    ("verification_delete", {"memory_id": M2, "machine_id": "m3"}),
    (
        "conflict_put",
        {
            "conflict": {
                "id": "c1",
                "a_id": M2,
                "b_id": M5,
                "summary_a": "a, again",
                "summary_b": "b",
                "similarity": 0.25,
                "method": "jaccard",
                "detector": "write",
                "created": TS,
                "status": "open",
            }
        },
    ),
    (
        "import_put",
        {"source": "v8:/fixture/a.md", "content_hash": "ac" * 32, "imported_at": TS},
    ),
    ("quarantine_put", {"name": "bad.md", "reason": "parse, again", "at": TS}),
    (
        "standing_put",
        {
            "standing": {
                "record_id": M5,
                "probability": 1,
                "body_sha256": "0" * 64,
                "source": "asked",
                "answered_by": "owner",
                "answered": TS,
            }
        },
    ),
]

DRIFT: list[tuple[str, Any]] = [
    ("index_rebuilt", {"new_fingerprint": "0" * 64, "old_fingerprint": "1" * 64}),
]

DEEP = "[" * 20000

#: Rows whose payload cannot be applied, each the reference catches.
BAD_CAUGHT: list[tuple[str, Any]] = [
    ("memory_put", "{not json"),
    ("memory_put", ""),
    ("memory_put", '{"a":1,}'),
    ("memory_put", "[1,]"),
    ("memory_put", '"\\x"'),
    ("memory_put", '"\\u12"'),
    ("memory_put", '"\\ud800\\u12'),
    ("memory_put", "1 2"),
    ("memory_put", '{"a" 1}'),
    ("memory_put", "[1 2]"),
    ("memory_put", '"a\x01"'),
    ("memory_put", "\n\n  {1:2}"),
    ("memory_put", "﻿{}"),
    ("memory_put", "[" + "1" * 4400 + "]"),
    ("memory_put", "[1, 2]"),
    ("memory_put", '"text"'),
    ("memory_delete", {}),
    ("memory_put", {"memory": _memory(M3, "x", [])["memory"]}),
    ("memory_put", {**_memory(M3, "x", []), "provenance": "bogus"}),
    ("memory_put", {**_memory(M3, "x", []), "provenance": {"a": ["b"]}}),
    ("memory_put", {"memory": {}, "provenance": "local"}),
    ("memory_put", {"memory": {"id": "x"}, "provenance": "local"}),
    ("tombstone_put", _tombstone(T6, links="x")),
    ("tombstone_put", _tombstone(T6, corroborations="abc")),
    ("tombstone_put", _tombstone(T6, corroborations=[1])),
    ("tombstone_put", _tombstone(T6, corroborations=float("nan"))),
    ("tombstone_put", _tombstone(T6, corroborations="9" * 4400)),
    ("tombstone_put", _tombstone(T6, filename="a\udfff\ud800b")),
    ("episode_put", {"episode": {"id": "bad"}}),
    ("episode_delete", {"id": "\ud800x"}),
    ("verification_put", {"memory_id": M2, "machine_id": "m1"}),
    ("verification_put", {"memory_id": M2, "verified_at": TS, "verified_paths": 5}),
    ("machine_put", {}),
    ("machine_put", {"machine": [1]}),
    ("machine_put", {"machine": {"id": "m9", "minted": TS, "zz": 1, "aa": 2}}),
    ("machine_put", {"machine": {"id": "m9", "facts_json": {}, "predecessor": None}}),
    (
        "machine_put",
        {"machine": {"id": "", "facts_json": {}, "predecessor": None, "minted": TS}},
    ),
    (
        "machine_put",
        {"machine": {"id": "m9", "facts_json": {}, "predecessor": 1.5, "minted": TS}},
    ),
    (
        "machine_put",
        {
            "machine": {
                "id": "m\ud800",
                "facts_json": {},
                "predecessor": None,
                "minted": TS,
            }
        },
    ),
    ("project_remote_delete", {"project": "p1"}),
    ("project_remote_delete", {"project": "p1", "remote_key": 3}),
]

#: Rows whose payload the reference does not catch: each raises out of
#: `refold`, so each is its own case.
BAD_RAISED: dict[str, tuple[str, Any]] = {
    "raised_attribute": ("conflict_put", {"conflict": [1]}),
    "raised_programming": (
        "import_put",
        {"source": "s", "content_hash": "h", "imported_at": TS, "memory_id": [1]},
    ),
    "raised_overflow_bind": (
        "quarantine_put",
        {"name": "n", "reason": "r", "at": TS, "size": 2**70},
    ),
    "raised_integrity": ("conflict_put", {"conflict": {"id": "c9"}}),
    "raised_recursion": ("memory_put", DEEP),
    "raised_overflow_int": (
        "tombstone_put",
        json.dumps(_tombstone(T6, corroborations=float("inf")), sort_keys=True),
    ),
}


def _quote(name: str) -> str:
    return '"' + name + '"'


def _snapshot(conn: sqlite3.Connection) -> dict[str, dict[int, tuple[Any, ...]]]:
    tables = [
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if str(r[0]) in ("log", *FOLDED_TABLES, *MEMBERSHIP_TABLES)
    ]
    out: dict[str, dict[int, tuple[Any, ...]]] = {}
    for t in tables:
        cols = [str(c[1]) for c in conn.execute(f"PRAGMA table_info({_quote(t)})")]
        sel = ", ".join(_quote(c) for c in cols if c != "rowid")
        out[t] = {
            int(r[0]): tuple(r[1:])
            for r in conn.execute(f"SELECT rowid, {sel} FROM {_quote(t)}")
        }
    return out


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(c[1])
        for c in conn.execute(f"PRAGMA table_info({_quote(table)})")
        if str(c[1]) != "rowid"
    ]


#: Every mutation kind a prelude applied without a raise: the generator
#: refuses to write a fixture that leaves one of `MUTATION_KINDS` out.
APPLIED: set[str] = set()


def prelude(
    bases: dict[str, Any],
    start: list[dict[str, Any]],
    applied: list[tuple[str, Any]],
    signed: list[tuple[str, Any]] = [],  # noqa: B006 - never mutated
) -> list[dict[str, Any]]:
    """The recipe that, on the intact base with `start` applied, appends
    `applied` and then `signed`, each row signed with its key, with the
    table rows `_apply_mutation` writes for the `applied` ones; then the
    head file naming the last row."""
    rows = [(k, p, True) for k, p in applied] + [(k, p, False) for k, p in signed]
    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "case"
        sf._materialize(bases, "intact", start, case)
        conn = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        try:
            before = _snapshot(conn)
            for kind, payload, apply in rows:
                seq, prev = conn.execute(
                    "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                text = (
                    payload
                    if isinstance(payload, str)
                    else json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                mac = compute_mac(
                    KEY_A,
                    seq=seq + 1,
                    ts=TS,
                    session=None,
                    kind=kind,
                    payload=text,
                    prev_mac=prev,
                )
                conn.execute(
                    "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?)",
                    (seq + 1, TS, kind, text, prev, mac),
                )
                if apply:
                    _apply_mutation(conn, kind, json.loads(text), log_mac=mac)
                    APPLIED.add(kind)
            after = _snapshot(conn)
            statements: list[str] = []
            for table, now in after.items():
                was = before.get(table, {})
                cols = _columns(conn, table)
                target = ", ".join(["rowid", *(_quote(c) for c in cols)])
                for rowid in sorted(set(was) - set(now)):
                    statements.append(
                        f"DELETE FROM {_quote(table)} WHERE rowid = {rowid};"
                    )
                for rowid in sorted(now):
                    if was.get(rowid) != now[rowid]:
                        values = ", ".join(_literal(v) for v in (rowid, *now[rowid]))
                        statements.append(
                            f"INSERT OR REPLACE INTO {_quote(table)}({target}) VALUES ({values});"
                        )
            last_seq, last_mac = conn.execute(
                "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    head = json.dumps({"seq": last_seq, "mac": last_mac, "ts": TS})
    return [
        {"sql": "\n".join(statements)},
        {"write": ".head", "hex": head.encode("utf-8").hex()},
    ]


def _literal(v: Any) -> str:
    """`gen_store_files._literal`, with a run of one character longer than
    64 spelled as SQL that makes it (the deep and long payloads stay small
    in the file and are the same text on both sides)."""
    if not isinstance(v, str) or len(v) < 65 or "\x00" in v:
        return sf._literal(v)
    parts: list[str] = []
    i = 0
    while i < len(v):
        j = i
        while j < len(v) and v[j] == v[i]:
            j += 1
        if j - i > 64 and ord(v[i]) < 0x80:
            ch = sf._literal(v[i])
            parts.append(f"replace(hex(zeroblob({j - i})), '00', {ch})")
        else:
            parts.append(sf._literal(v[i:j]))
        i = j
    merged: list[str] = []
    for part in parts:
        if merged and part.startswith("'") and merged[-1].startswith("'"):
            merged[-1] = merged[-1][:-1] + part[1:]
        else:
            merged.append(part)
    return "(" + " || ".join(merged) + ")"


def _edits() -> dict[str, tuple[str, list[str]]]:
    """A planted, an edited and a deleted row in each table, over the
    populated prelude."""
    known = _known_columns()
    out: dict[str, tuple[str, list[str]]] = {}
    for table in (*FOLDED_TABLES, *MEMBERSHIP_TABLES):
        cols = [c for c in known[table] if c != "rowid"]
        keys = _TABLE_KEYS[table]
        first = keys[0]
        picked = ", ".join(f"{c} || '-planted'" if c == first else c for c in cols)
        listed = ", ".join(cols)
        rest = [c for c in cols if c not in keys]
        edit = rest[0] if rest else keys[-1]
        out[f"{table}_planted"] = (
            table,
            [
                f"INSERT INTO {table}({listed}) SELECT {picked} FROM {table} "
                "ORDER BY rowid LIMIT 1;"
            ],
        )
        out[f"{table}_edited"] = (
            table,
            [
                f"UPDATE {table} SET {edit} = COALESCE({edit}, '') || 'x' "
                f"WHERE rowid = (SELECT min(rowid) FROM {table});"
            ],
        )
        out[f"{table}_deleted"] = (
            table,
            [f"DELETE FROM {table} WHERE rowid = (SELECT max(rowid) FROM {table});"],
        )
    return out


FIRST_MEMORY = "WHERE rowid = (SELECT min(rowid) FROM memories)"

#: Hostile or odd live tables over the populated prelude.
HOSTILE: dict[str, list[str]] = {
    "index_text_edited": [
        f"UPDATE memories SET body_fts = body_fts || ' planted' {FIRST_MEMORY};"
    ],
    "scopes_fts_edited": [f"UPDATE memories SET scopes_fts = '' {FIRST_MEMORY};"],
    "text_not_utf8": [
        f"UPDATE memories SET body = CAST(X'61ff62' AS TEXT) {FIRST_MEMORY};"
    ],
    "blob_in_text": [f"UPDATE memories SET filename = X'00ff' {FIRST_MEMORY};"],
    "nul_in_text": [
        f"UPDATE memories SET body = body || char(0) || 'z' {FIRST_MEMORY};"
    ],
    "integer_in_text": [f"UPDATE memories SET corroborations = 'zero' {FIRST_MEMORY};"],
    "real_for_integer": ["UPDATE checkout_views SET seen_order = 1.5;"],
    "huge_value": [
        "UPDATE episodes SET body = replace(hex(zeroblob(40000)), '0', 'a') "
        "WHERE rowid = (SELECT min(rowid) FROM episodes);"
    ],
    "blob_key": ["INSERT INTO imports VALUES (X'6162', 'h', 't', NULL);"],
    "null_key_planted": ["INSERT INTO memory_links VALUES ('a', 'b', 'c', NULL);"],
    "duplicate_null_note_link": [
        "INSERT INTO memory_links SELECT * FROM memory_links WHERE note IS NULL LIMIT 1;"
    ],
    "keys_that_print_alike": [
        "INSERT INTO quarantine(name, reason, at) VALUES ('10', 'r', 't');",
        "INSERT INTO quarantine(name, reason, at) VALUES (X'3130', 'r', 't');",
        "INSERT INTO quarantine(name, reason, at) VALUES ('None', 'r', 't');",
        "INSERT INTO quarantine(name, reason, at) VALUES (NULL, 'r', 't');",
    ],
    "membership_table_absent": ["DROP TABLE returned;"],
    "membership_tables_absent": ["DROP TABLE standing; DROP TABLE checkout_views;"],
    "optional_column_absent": ["ALTER TABLE checkouts DROP COLUMN unresolved_json;"],
    "extra_column": ["ALTER TABLE returned ADD COLUMN extra TEXT;"],
    "missing_column": ["ALTER TABLE returned DROP COLUMN via;"],
    "memories_emptied": ["DELETE FROM memories;"],
}


def _value(v: Any) -> Any:
    if isinstance(v, bytes):
        return {"blob": v.hex()}
    if isinstance(v, float):
        return {"float": sf._bits(v)}
    if isinstance(v, int) and not isinstance(v, bool) and not -(2**63) <= v < 2**63:
        return {"int": str(v)}
    if isinstance(v, list):
        return [_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items()}
    return v


class _Reader:
    """What `refold` and `log_verify` read from a `Store`."""

    def refold(self) -> dict[str, Any]:
        return store_events.refold(cast(Any, self))

    def log_verify(self) -> dict[str, Any]:
        return store_events.log_verify(cast(Any, self))

    def __init__(self, conn: sqlite3.Connection, keys: Path, meta: dict[str, str]):
        self._conn = conn
        self._unaccounted_memory_ids: set[str] = set()
        self._store_id = meta["store_id"]
        self.key_fingerprint = meta["key_fingerprint"]
        self._keyring = KeyRing(keys, self._store_id)
        self._path = "<store>"


def _run(call: Any) -> Any:
    try:
        return _value(call())
    except Exception as exc:  # noqa: BLE001 - recorded as data
        return {"raised": type(exc).__name__}


def reports(case: Path) -> dict[str, Any]:
    try:
        conn, scratch = connect_read_only(case / "memory.sqlite", timeout=5.0)
    except (StoreRefusedError, sqlite3.Error, TypeError) as exc:
        return {"open": {"refused": type(exc).__name__}}
    try:
        meta = {
            str(r[0]): str(r[1]) for r in conn.execute("SELECT key, value FROM meta")
        }
        reader = _Reader(conn, case / "keys", meta)
        verified = _run(reader.log_verify)
        if isinstance(verified, dict):
            verified.pop("path", None)
        return {"refold": _run(reader.refold), "log_verify": verified}
    finally:
        conn.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


_FLOATISH = re.compile(
    r"\d\.|\.\d|\d[eE][+-]?\d|\b(?:real|float|double)\b"
    r"|\b(?:avg|total|sum|random|julianday|round|sqrt|pi|exp|ln|log|log10|log2|pow"
    r"|power|unixepoch|json_extract)\s*\(",
    re.IGNORECASE,
)


#: Recipes that make a float on purpose, each into a column whose affinity
#: keeps it a REAL (stored as its 8 bytes, never as SQLite's text).
REAL_KEPT_REAL = {
    "real_for_integer": "checkout_views.seen_order is INTEGER: 1.5 stays a REAL",
}


def _no_float_in_recipe(name: str, recipe: list[dict[str, Any]]) -> None:
    """No recipe SQL may make a float: SQLite spells a REAL stored in a
    column of TEXT affinity by its own version's rules (3.50.4 writes
    ``9.00719925474099e+15`` where 3.54.1 writes ``9007199254740992.0``),
    and each side rebuilds the stores with its own SQLite, so such a value
    would make the fixture SQLite's. The SQL is scanned, quoted text left
    out, for a REAL literal (a digit beside a point, an exponent) or a
    function or cast that can give a REAL. It is a first line only;
    `_no_float_made` checks the built store. (Preludes need no
    scan: their values are read back from what the reference stored, and a
    value read back from a TEXT column is text, written as quoted text.)
    The recipes in `REAL_KEPT_REAL` are exempt, each for its reason."""
    if name in REAL_KEPT_REAL:
        return
    for op in recipe:
        sql = op.get("sql")
        if not isinstance(sql, str):
            continue
        bare = re.sub(r"'(?:[^']|'')*'", "''", sql)
        bare = re.sub(r"X''", "", bare)
        found = _FLOATISH.search(bare)
        if found:
            raise AssertionError(
                f"{name}: recipe SQL that can make a float: {found[0]!r}"
            )


_FLOAT_TEXT = re.compile(
    r"\s*[+-]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][+-]?\d+|inf(inity)?|nan)\s*",
    re.IGNORECASE,
)


def _cells(
    store_file: Path,
) -> dict[tuple[str, str], tuple[bool, list[tuple[str, Any]]]]:
    """Every cell of every table, by (table, column): whether the column
    has TEXT affinity, and each cell's (typeof, value)."""
    conn = sqlite3.connect(Path(store_file).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        out: dict[tuple[str, str], tuple[bool, list[tuple[str, Any]]]] = {}
        tables = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(r[0]).startswith(("sqlite_", "memories_fts"))
        ]
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({_quote(table)})"):
                declared = str(col[2]).upper()
                text = any(t in declared for t in ("CHAR", "CLOB", "TEXT"))
                name = _quote(str(col[1]))
                cells = conn.execute(
                    f"SELECT typeof({name}), {name} FROM {_quote(table)}"
                ).fetchall()
                out[(table, str(col[1]))] = (text, [(str(k), v) for k, v in cells])
        return out
    finally:
        conn.close()


def _no_float_made(name: str, base: Path, built: Path) -> None:
    """Backing `_no_float_in_recipe` on the built store: against the store
    its prelude alone builds, no cell that changed (a (typeof, value) its
    column did not hold) is text in a TEXT-affinity column that reads as a
    float (a point, an exponent, inf, nan), and none is a REAL, unless the
    case is in `REAL_KEPT_REAL`."""
    before = _cells(base)
    for (table, column), (text, cells) in _cells(built).items():
        held = {(k, v) for k, v in before.get((table, column), (False, []))[1]}
        for kind, value in cells:
            if (kind, value) in held:
                continue
            if text and kind == "text" and _FLOAT_TEXT.fullmatch(str(value)):
                raise AssertionError(f"{name}: float text in {table}.{column}")
            if kind == "real" and name not in REAL_KEPT_REAL:
                raise AssertionError(f"{name}: a REAL made in {table}.{column}")


def _compact_tables(tables: dict[str, Any]) -> dict[str, Any]:
    """A table with nothing unaccounted and nothing missing as its row count
    alone."""
    return {
        name: entry["rows"]
        if not entry["unaccounted"] and not entry["missing"]
        else entry
        for name, entry in tables.items()
    }


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    refold = result.get("refold")
    verified = result.get("log_verify")
    if isinstance(refold, dict) and "tables" in refold:
        refold["tables"] = _compact_tables(refold["tables"])
        refold["membership"] = _compact_tables(refold["membership"])
    if isinstance(verified, dict) and isinstance(verified.get("fold"), dict):
        fold = verified["fold"]
        fold["tables"] = _compact_tables(fold["tables"])
        fold["membership"] = _compact_tables(fold["membership"])
        if isinstance(refold, dict) and fold == {
            k: refold.get(k) for k in ("status", "tables", "membership")
        }:
            verified["fold"] = "=refold"
    return result


Case = tuple[str, str, str, list[str], list[dict[str, Any]]]

#: Edits whose report equals their unedited base's on purpose: each pins a
#: blind spot of the reference.
BLIND_SPOTS = {
    # The log names another tokenizer: the index text is not compared.
    "drift_index_text_edited": "populated_drift",
}


def _no_edit_is_a_no_op(lines: list[dict[str, Any]]) -> None:
    """Every `*_edited` case reports something its unedited base does
    not (an edit that changes nothing pins nothing), except the blind
    spots named in `BLIND_SPOTS`."""
    by_name = {line["case"]: line for line in lines}

    def report(name: str) -> tuple[Any, Any]:
        line = by_name[name]
        return line.get("refold"), line.get("log_verify")

    for name in by_name:
        if not name.endswith("_edited"):
            continue
        if name.startswith("every_kind:"):
            base = "every_kind"
        elif name.startswith("drift_"):
            base = "populated_drift"
        else:
            base = "populated"
        same = report(name) == report(base)
        if same and BLIND_SPOTS.get(name) != base:
            raise AssertionError(f"{name}: the edit changes nothing {base} reports")
        if not same and name in BLIND_SPOTS:
            raise AssertionError(f"{name}: no longer a blind spot")


def generate() -> dict[str, bytes]:
    from bettermemory.store_schema import _MEMBERSHIP_SPECS, MUTATION_KINDS

    from ._sandbox import world

    APPLIED.clear()
    bases = sf.load_bases()
    every = list(EVERY_KIND)
    for kind, payload in POPULATED:
        member = kind.removesuffix("_put")
        if kind.endswith("_put") and member in _MEMBERSHIP_SPECS:
            every.extend(_second(member, payload[member]))
    pre: dict[str, list[dict[str, Any]]] = {}
    pre["populated"] = prelude(bases, [], POPULATED)
    pre["drift"] = prelude(bases, [], [], DRIFT)
    pre["drift_on_populated"] = prelude(bases, pre["populated"], [], DRIFT)
    pre["bad_on_populated"] = prelude(bases, pre["populated"], [], BAD_CAUGHT)
    pre["every_kind"] = prelude(bases, pre["populated"], every)
    on_every = [*pre["populated"], *pre["every_kind"]]
    pre["drift_on_every_kind"] = prelude(bases, on_every, [], DRIFT)
    for name, row in BAD_RAISED.items():
        pre[name] = prelude(bases, [], POPULATED[:3], [row])
    left_out = sorted(MUTATION_KINDS - APPLIED)
    if left_out:
        raise AssertionError(f"mutation kinds no prelude applies: {left_out}")

    cases: list[Case] = []
    for name, (what, base, recipe) in sf.cases(bases).items():
        cases.append((f"store_files:{name}", what, base, [], recipe))
    cases.append(("store_real", "store_real.sqlite as committed", "store_real", [], []))
    populated = ["populated"]
    cases.append(
        ("populated", "every table filled through the log", "intact", populated, [])
    )
    edits = _edits()
    for name, (table, sql) in edits.items():
        what = f"{table} edited behind the log"
        cases.append((name, what, "intact", populated, [{"sql": "\n".join(sql)}]))
    for name, sql in HOSTILE.items():
        what = "a live table edited behind the log"
        cases.append((name, what, "intact", populated, [{"sql": "\n".join(sql)}]))
    cases.append(("drift", "the log names another tokenizer", "intact", ["drift"], []))
    drifted = ["populated", "drift_on_populated"]
    cases.append(
        ("populated_drift", "populated, then another tokenizer", "intact", drifted, [])
    )
    for name, sql in (
        ("drift_index_text_edited", HOSTILE["index_text_edited"]),
        (
            "drift_body_edited",
            [f"UPDATE memories SET body = body || 'x' {FIRST_MEMORY};"],
        ),
    ):
        what = "another tokenizer, a memory edited"
        cases.append((name, what, "intact", drifted, [{"sql": "\n".join(sql)}]))
    bad = ["populated", "bad_on_populated"]
    cases.append(
        (
            "bad_caught",
            "payloads the fold reports as payload_invalid",
            "intact",
            bad,
            [],
        )
    )
    for name in BAD_RAISED:
        cases.append(
            (name, "a payload the reference does not catch", "intact", [name], [])
        )
    every_names = ["populated", "every_kind"]
    cases.append(
        ("every_kind", "every mutation kind applied", "intact", every_names, [])
    )
    for name, (table, sql) in edits.items():
        if name.endswith("_edited"):
            what = f"every kind applied, {table} edited behind the log"
            cases.append(
                (
                    f"every_kind:{name}",
                    what,
                    "intact",
                    every_names,
                    [{"sql": "\n".join(sql)}],
                )
            )
    cases.append(
        (
            "every_kind_drift",
            "every kind applied, then another tokenizer",
            "intact",
            [*every_names, "drift_on_every_kind"],
            [],
        )
    )
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "p"
        sf.rebuild(bases["intact"], probe)
        c = sqlite3.connect(probe / "memory.sqlite")
        mac1 = c.execute("SELECT mac FROM log WHERE seq = 1").fetchone()[0]
        c.close()
    head1 = json.dumps({"seq": 1, "mac": mac1, "ts": TS}).encode("utf-8").hex()
    emptied = "DELETE FROM memories; DELETE FROM tombstones; DELETE FROM episodes;"
    cases.append(
        (
            "empty",
            "the store_created row alone",
            "intact",
            [],
            [
                {"sql": f"DELETE FROM log WHERE seq > 1; {emptied}"},
                {"write": ".head", "hex": head1},
            ],
        )
    )
    cases.append(
        (
            "no_log",
            "no log row at all: the tokenizer from meta",
            "intact",
            [],
            [{"sql": f"DELETE FROM log; {emptied}"}],
        )
    )
    lines: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp, world():
        root = Path(tmp).resolve()
        for i, (name, what, base, names, recipe) in enumerate(cases):
            case = root / f"c{i}"
            if base == "store_real":
                case.mkdir()
                (case / "keys").mkdir()
                shutil.copyfile(sf._real_path(), case / "memory.sqlite")
                doc = json.loads(sf._real_doc_path().read_text(encoding="utf-8"))
                for suffix, text in doc["keys"].items():
                    (case / "keys" / f"{doc['store_id']}{suffix}").write_bytes(
                        bytes.fromhex(text)
                    )
            else:
                full = [op for n in names for op in pre[n]] + recipe
                own = not name.startswith("store_files:")
                if own:
                    _no_float_in_recipe(name, recipe)
                sf._materialize(bases, base, full, case)
                if own and recipe:
                    plain = root / f"b{i}"
                    sf._materialize(bases, base, full[: len(full) - len(recipe)], plain)
                    try:
                        _no_float_made(
                            name, plain / "memory.sqlite", case / "memory.sqlite"
                        )
                    except sqlite3.DatabaseError:
                        pass  # a file SQLite cannot read holds no cell
                    shutil.rmtree(plain, ignore_errors=True)
            line = {
                "case": name,
                "what": what,
                "base": base,
                "prelude": names,
                "recipe": recipe,
            }
            lines.append({**line, **_compact(reports(case))})
    _no_edit_is_a_no_op(lines)
    header = {
        "about": "store_events.refold and log_verify on fixture stores (N1e)",
        "preludes": pre,
        "stamp": TS,
        "table_entry": "a table with nothing unaccounted or missing is its row count",
        "fold_same": "log_verify's fold is '=refold' when it equals refold's",
    }
    return {"fold.jsonl": jsonl([header, *lines])}


__all__ = ["generate"]
