"""Fixture N1b: store files and the reference's answers about them.

The native build's read side (``native/crates/bettermemory-core/src/store``)
reads store files. The files are committed as logical dumps, not as bytes
(SQLite's bytes are not reproducible across versions, and a store this
build creates is about 650 KB of 8 KB pages):

- ``store_bases.json``: a few base stores, each as the SQL that rebuilds it
  (its page size and journal mode, each table's text and rows with their
  rowids, then the full-text table, rebuilt from its content table, then
  the indexes and triggers, in ``sqlite_master`` order) and the files of
  its keys directory. The bases are written once through the reference's
  own paths (`Store.create`, `Store.write`, `tombstone`, `write_episode`,
  `key_lifecycle.rotate_key`, and `log.append_history_replaced` for the
  restored and accepted bases' rows, with neutral paths) by ``python -m
  tests.contract.gen_store_files --bases``, in a sandbox with the fixed
  store id and keys gen_log uses; that run also proves each rebuilt store
  answers exactly as the store it was dumped from.
- ``store_files.jsonl``: every case, a base and a recipe of edits applied
  to it after it is rebuilt (SQL, a key file removed or written, the
  journal mode, the file truncated), and the reference's answers about the
  result: the admission's refusal, or the version contract's outcome, the
  pragmas, ``sqlite_master``, meta, every table's rows, the memories,
  tombstones and episodes as `store_rows` reads them, the
  `log.verify_chain` report, `history.history_replaced`, and for the FTS
  base the prefilter per query, filters included. Cases whose content is
  their base's with an edit (the tamperings, the version and journal
  variants) pin their rows by digest (sha256 of their compact JSON) and
  everything else in full.

- ``store_real.sqlite`` and ``store_real.json``: one file as the reference's
  SQLite wrote it (``--real``), kept as bytes so the claim that a file
  written by SQLite 3.50.4 reads the same under the native build's bundled
  SQLite stays tested; its document records the SQLite that wrote it (and
  checks the file header agrees), its keys, and the answers about it.

`generate` rebuilds every case from the committed bases and asks the
reference again, so a drift in how the reference reads a store changes
``store_files.jsonl``; the Rust tests rebuild the same stores from the same
bases and compare with the committed answers, without Python. A message
that names the store's path names it ``<store>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import sys
import tempfile
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import store_versions
from bettermemory.history import history_replaced
from bettermemory.key_lifecycle import rotate_key
from bettermemory.log import HISTORY_ACCEPTED, STORE_RESTORED, append_history_replaced
from bettermemory.log import KeyRing, verify_chain
from bettermemory.models import Category, Confidence, Source
from bettermemory.search_lexicon import fts_match_query
from bettermemory.store import Store
from bettermemory.store_admission import LaterStoreError, StoreRefusedError
from bettermemory.store_index import query_candidates, ranked_rows
from bettermemory.store_rows import _row_to_episode, _row_to_memory, _row_to_tombstone
from bettermemory import store_schema
from bettermemory.store_schema import SCHEMA_REVISION, connect_read_only

from ._common import json_doc, jsonl
from .gen_log import KEY_A, KEY_B, STORE_ID
from .gen_store import _BODIES, _QUERIES

GOLDEN = Path(__file__).parent / "golden"
MEMBERSHIP_ID = "fedcba9876543210fedcba9876543210"
STORE = "<store>"
_PRAGMAS = (
    "application_id",
    "user_version",
    "journal_mode",
    "page_size",
    "encoding",
    "auto_vacuum",
)
#: The moment the restore case's bundle is made at.


@contextmanager
def _fixed_random() -> Iterator[None]:
    hexes = [STORE_ID, MEMBERSHIP_ID]
    keys = [KEY_B, KEY_A]

    def token_hex(nbytes: int | None = None) -> str:
        return hexes.pop(0) if hexes else os.urandom(nbytes or 32).hex()

    def token_bytes(nbytes: int | None = None) -> bytes:
        if nbytes == 32 and keys:
            return keys.pop()
        return os.urandom(32 if nbytes is None else nbytes)

    with (
        patch.object(secrets, "token_hex", token_hex),
        patch.object(secrets, "token_bytes", token_bytes),
    ):
        yield


# ---------------------------------------------------------------------------
# Values, dumps and rebuilds
# ---------------------------------------------------------------------------


def _bits(v: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', v))[0]:016x}"


def _value(v: Any) -> Any:
    """A column value as SQLite returned it to Python: int and str as
    JSON, a float as its bits, a blob as hex."""
    if v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return {"float": _bits(v)}
    if isinstance(v, bytes):
        return {"blob": v.hex()}
    raise TypeError(type(v).__name__)


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    if "\x00" in v:
        return f"CAST(X'{v.encode('utf-8').hex()}' AS TEXT)"
    return "'" + v.replace("'", "''") + "'"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def dump(store_file: Path, keys_dir: Path) -> dict[str, Any]:
    """The SQL that rebuilds `store_file`, and its keys directory's files."""
    conn = sqlite3.connect(f"file:{store_file}?mode=ro&immutable=1", uri=True)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
        ).fetchall()
        virtual = [
            n for k, n, _t, s in master if k == "table" and "VIRTUAL TABLE" in (s or "")
        ]
        shadows = tuple(f"{n}_" for n in virtual)
        statements: list[str] = []
        for kind, name, _table, sql in master:
            if (
                kind != "table"
                or name in virtual
                or name.startswith(("sqlite_", *shadows))
            ):
                continue
            statements.append(sql)
            info = conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
            columns = [str(c[1]) for c in info]
            pks = [c for c in info if c[5]]
            alias = len(pks) == 1 and str(pks[0][2]).upper() == "INTEGER"
            without = "WITHOUT ROWID" in sql.upper()
            names = columns if alias or without else ["rowid", *columns]
            select = ", ".join(_quote(c) for c in columns)
            if names[0] == "rowid" and "rowid" not in columns:
                select = "rowid, " + select
            order = (
                "rowid"
                if not without
                else ", ".join(str(i + 1) for i in range(len(columns)))
            )
            target = ", ".join(_quote(c) for c in names)
            for row in conn.execute(
                f"SELECT {select} FROM {_quote(name)} ORDER BY {order}"
            ):
                values = ", ".join(_literal(v) for v in row)
                statements.append(
                    f"INSERT INTO {_quote(name)}({target}) VALUES ({values})"
                )
        for name in virtual:
            sql = next(s for k, n, _t, s in master if n == name)
            statements.append(sql)
            statements.append(
                f"INSERT INTO {_quote(name)}({_quote(name)}) VALUES ('rebuild')"
            )
        for kind, _name, _table, sql in master:
            if kind in ("index", "trigger") and sql is not None:
                statements.append(sql)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        with open(store_file, "rb") as handle:
            header = handle.read(20)
        wal = len(header) == 20 and 2 in (header[18], header[19])
    finally:
        conn.close()
    return {
        "page_size": page_size,
        "journal_mode": "wal" if wal else "delete",
        "statements": statements,
        "keys": {
            p.name[len(STORE_ID) :]: p.read_bytes().hex()
            for p in sorted(keys_dir.iterdir())
            if p.name.startswith(STORE_ID) and p.suffix not in (".lock", ".path")
        },
    }


def rebuild(base: dict[str, Any], case: Path) -> None:
    """Write `base` as ``case/memory.sqlite`` and ``case/keys``."""
    case.mkdir(parents=True)
    (case / "keys").mkdir()
    conn = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
    try:
        conn.execute(f"PRAGMA page_size = {int(base['page_size'])}")
        for sql in base["statements"]:
            conn.execute(sql)
        conn.execute(f"PRAGMA journal_mode = {base['journal_mode']}")
    finally:
        conn.close()
    for suffix, text in base["keys"].items():
        (case / "keys" / f"{STORE_ID}{suffix}").write_bytes(bytes.fromhex(text))


def apply(recipe: list[dict[str, Any]], case: Path) -> None:
    """Apply a case's edits to its rebuilt base, in order."""
    store_file = case / "memory.sqlite"
    for op in recipe:
        if "sql" in op:
            conn = sqlite3.connect(store_file, isolation_level=None)
            try:
                conn.executescript(op["sql"])
            finally:
                conn.close()
        elif "unlink" in op:
            for path in sorted((case / "keys").glob(f"{STORE_ID}{op['unlink']}")):
                path.unlink()
        elif "write" in op:
            (case / "keys" / f"{STORE_ID}{op['write']}").write_bytes(
                bytes.fromhex(op["hex"])
            )
        elif "truncate" in op:
            with open(store_file, "r+b") as handle:
                handle.truncate(int(op["truncate"]))
        else:
            raise ValueError(op)


# ---------------------------------------------------------------------------
# The reference's answers
# ---------------------------------------------------------------------------


def _iso(v: Any) -> Any:
    return None if v is None else v.isoformat()


def _record(m: Any) -> dict[str, Any]:
    category = (
        None if m.category is None else str(getattr(m.category, "value", m.category))
    )
    return {
        "id": m.id,
        "created": _iso(m.created),
        "updated": _iso(m.updated),
        "scopes": list(m.scopes),
        "confidence": m.confidence.value,
        "source": m.source.value,
        "body": m.body,
        "category": category,
        "last_verified_at": _iso(m.last_verified_at),
        "verified_paths": list(m.verified_paths),
        "verified_commits": list(m.verified_commits),
        "verified_versions": list(m.verified_versions),
        "verified_absent_paths": list(m.verified_absent_paths),
        "claims": list(m.claims),
        "verified_head": m.verified_head,
    }


def _all_rows(conn: sqlite3.Connection, table: str) -> str:
    """Every row of `table`, ordered by every column in turn (a table
    WITHOUT ROWID has no rowid to order by)."""
    count = len(conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall())
    order = ", ".join(str(i + 1) for i in range(count))
    return f"SELECT * FROM {_quote(table)} ORDER BY {order}"


def digest(value: Any) -> str:
    """sha256 of `value`'s compact JSON, keys sorted, UTF-8."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _placed(text: str | None, path: Path) -> str | None:
    return None if text is None else text.replace(str(path), STORE)


#: The filtered prefilter queries the FTS case also runs
#: (`query_candidates` with scopes, client, model and a cap).
_FILTERED = (
    ("docker", {"scopes": ["tools"]}),
    ("the", {"scopes": ["db", "misc"], "max_results": 3}),
    ("api", {"client": "nobody"}),
    ("postgres", {"model": "nobody"}),
    ("cache policy", {"scopes": ["misc"], "max_results": 1}),
)


def answers(case: Path, version: str, *, full: bool) -> dict[str, Any]:
    store_file = case / "memory.sqlite"
    out: dict[str, Any] = {}
    try:
        conn, scratch = connect_read_only(store_file, timeout=5.0)
    except (sqlite3.Error, TypeError) as exc:
        # What connect_read_only lets out unrefused: recorded as data.
        return {
            "open": {
                "outcome": "raised",
                "type": type(exc).__name__,
                "message": _placed(str(exc), store_file),
            }
        }
    except StoreRefusedError as exc:
        refused: dict[str, Any] = {
            "outcome": "refused_store",
            "type": type(exc).__name__,
            "why": _placed(exc.why, store_file),
            "message": _placed(str(exc), store_file),
            "timed_out": exc.timed_out,
            "holds_meta": exc.holds_meta,
        }
        if isinstance(exc, LaterStoreError):
            refused.update(key=exc.key, needed=exc.needed)
        return {"open": refused}
    try:
        out["pragmas"] = {
            p: _value(conn.execute(f"PRAGMA {p}").fetchone()[0]) for p in _PRAGMAS
        }
        master = [
            [_value(x) for x in row]
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]
        meta = {
            str(r[0]): str(r[1]) for r in conn.execute("SELECT key, value FROM meta")
        }
        try:
            verdict = store_versions.check_versions(conn, store_file, dict(meta), False)
            out["open"] = {
                "outcome": "read_only" if verdict.read_only else "readable",
                "reason": _placed(verdict.reason, store_file),
                "revision": verdict.revision,
                "min_reader": verdict.min_reader,
                "min_writer": verdict.min_writer,
            }
        except store_versions.StoreVersionError as exc:
            out["open"] = {
                "outcome": "refused",
                "message": _placed(str(exc), store_file),
                "key": exc.key,
                "needed": exc.needed,
            }
        conn.execute("PRAGMA query_only = OFF")
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        rows = {
            name: [
                [_value(v) for v in row] for row in conn.execute(_all_rows(conn, name))
            ]
            for name in tables
            if not name.startswith("memories_fts")
        }
        records = {
            "memories": [
                _record(_row_to_memory(r))
                for r in conn.execute("SELECT * FROM memories ORDER BY rowid")
            ],
            "tombstones": [
                {
                    **_record(t),
                    "removed": _iso(t.removed),
                    "removed_reason": t.removed_reason,
                    "removed_session": t.removed_session,
                }
                for t in map(
                    _row_to_tombstone,
                    conn.execute("SELECT * FROM tombstones ORDER BY rowid"),
                )
            ],
            "episodes": [
                {
                    "id": e.id,
                    "session_id": e.session_id,
                    "created": _iso(e.created),
                    "body": e.body,
                    "scopes": list(e.scopes),
                    "takeaway": e.takeaway,
                    "is_floor": e.is_floor,
                    "swarm_id": e.swarm_id,
                }
                for e in map(
                    _row_to_episode,
                    conn.execute("SELECT * FROM episodes ORDER BY rowid"),
                )
            ],
        }
        out["row_counts"] = {k: len(v) for k, v in rows.items()}
        if full:
            out["sqlite_master"] = master
            out["meta"] = meta
            out["tables"] = rows
            out.update(records)
        else:
            out["sqlite_master_sha256"] = digest(master)
            out["meta_sha256"] = digest(meta)
            out["tables_sha256"] = digest(rows)
            out["records_sha256"] = digest(records)
        ring = KeyRing(case / "keys", meta["store_id"])
        out["chain"] = verify_chain(
            conn, keyring=ring, current_fingerprint=meta["key_fingerprint"]
        ).to_dict()
        out["history_replaced"] = history_replaced(types.SimpleNamespace(conn=conn))  # type: ignore[arg-type]
    finally:
        conn.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return out


def fts_answers(case: Path) -> list[dict[str, Any]]:
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys", read_only=True)
    try:
        out = []
        queries: list[tuple[str, dict[str, Any]]] = [(q, {}) for q in _QUERIES]
        queries += list(_FILTERED)
        for query, options in queries:
            rows = query_candidates(store, query, **options)
            out.append(
                {
                    "query": query,
                    "options": options,
                    "match_query": fts_match_query(query),
                    "candidates": [[i, _bits(s)] for i, s in rows],
                    "ranked_rows": ranked_rows(store, query),
                }
            )
        return out
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The bases, written once through the reference's own paths
# ---------------------------------------------------------------------------


def _create(case: Path) -> Store:
    case.mkdir(parents=True)
    return Store.create(case, keys_dir=case / "keys")


def _intact(case: Path) -> None:
    store = _create(case)
    try:
        a = store.write(content="Use docker compose for staging.", scopes=["tools"])
        store.write(
            content="Postgres 16.3 replaced 15.4.",
            scopes=["db", "infra"],
            confidence=Confidence.HIGH,
            category=Category.FACT,
            claims=["postgres 16.3"],
        )
        store.write(
            content="Prefer snake_case in Python.",
            scopes=["style"],
            source=Source.INFERRED,
            confidence=Confidence.LOW,
        )
        store.tombstone(a.id, "superseded", session="sess_0000000000000001")
        store.write_episode(session_id="sess_0000000000000001", body="Set up staging.")
        store.write_episode(
            session_id="sess_0000000000000002",
            body="Upgraded postgres.",
            scopes=["db"],
            takeaway="pin versions",
        )
    finally:
        store.close()


def _write_one(case: Path, content: str) -> None:
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys", allow_rekey=True)
    try:
        store.write(content=content, scopes=["more"])
    finally:
        store.close()


def _rekeyed(case: Path) -> None:
    _intact(case)
    report = rotate_key(case / "memory.sqlite", apply=True, keys_dir=case / "keys")
    if report.get("status") != "done":
        raise AssertionError(f"rotate refused: {report}")
    _write_one(case, "Written under the new key.")


# A real restore (`backup.restore_bundle`, through `restore_rows`) and a
# real accept-history (`key_lifecycle.accept_history`, key_lifecycle.py
# near line 753 and 1211) record the store's and the bundle's resolved
# paths in the signed row: a host fact no committed fixture may carry. So
# these two bases append their row directly, through the reference's own
# `log.append_history_replaced`, with every path a neutral string and every
# other field of the type and shape the real paths write.
_FIXTURE_STORE = "/fixture/store/memory.sqlite"
_FIXTURE_BUNDLE = "/fixture/bundles/20010203T040506Z"
_RESTORED_AT = "2001-02-03T05:06:07.000000Z"


def _anchor(case: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The store's head ``{seq, mac}`` and its origin anchor as written."""
    with sqlite3.connect(f"file:{case / 'memory.sqlite'}?mode=ro", uri=True) as conn:
        seq, mac = conn.execute(
            "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    conn.close()
    origin = json.loads(
        (case / "keys" / f"{STORE_ID}.origin").read_text(encoding="utf-8")
    )
    anchor = {k: origin[k] for k in ("key_fingerprint", "mac", "seq")}
    return {"mac": mac, "seq": seq}, anchor


def _append_replaced(case: Path, kind: str, entry: dict[str, Any]) -> None:
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys")
    try:
        if append_history_replaced(store, kind, entry) is None:
            raise AssertionError(f"{kind}: the store would not append")
    finally:
        store.close()


def _restored(case: Path) -> None:
    """The state a restore leaves: the bundle's rows (here `_intact`'s, as
    the real bundle held) and then the store_restored row. The real restore
    also discarded one later write (head_before names it), moved the old
    file aside and rewrote the head file; the rows are what the reader
    reads."""
    _intact(case)
    head, origin = _anchor(case)
    _append_replaced(
        case,
        STORE_RESTORED,
        {
            "binding_after": _FIXTURE_STORE,
            "binding_before": _FIXTURE_STORE,
            "bundle": _FIXTURE_BUNDLE,
            "bundle_created_at": "2001-02-03T04:05:06Z",
            "head_after": head,
            "head_before": {"mac": "ab" * 32, "seq": head["seq"] + 1},
            "keys_installed": [],
            "log_end_before": {"path": _FIXTURE_STORE, "seq": head["seq"] + 1},
            "moved_aside": [
                {
                    "from": _FIXTURE_STORE,
                    "to": _FIXTURE_STORE + ".pre-restore-20010203T050607Z",
                }
            ],
            "origin_after": origin,
            "origin_before": origin,
            "replaced_store_id": STORE_ID,
            "restored_at": _RESTORED_AT,
            "store": _FIXTURE_STORE,
            "store_id": STORE_ID,
            "trusted_bundle_key": None,
        },
    )


def _accepted(case: Path) -> None:
    """The state an accept-history leaves: the history, the origin file
    anchored to it (as accept_history rewrites it; here it never moved),
    and the history_accepted row recording the mismatched anchor."""
    _intact(case)
    head, origin = _anchor(case)
    _append_replaced(
        case,
        HISTORY_ACCEPTED,
        {
            "accepted_history": True,
            "binding_after": _FIXTURE_STORE,
            "binding_before": _FIXTURE_STORE,
            "head_after": head,
            "head_before": head,
            "head_status_before": "ok",
            "log_end_before": head,
            "origin_after": origin,
            "origin_before": {**origin, "mac": "0" * 64},
            "origin_status_before": "mismatch",
            "replaced_store_id": STORE_ID,
            "restored_at": _RESTORED_AT,
            "store": _FIXTURE_STORE,
            "store_id": STORE_ID,
        },
    )


def _fts(case: Path) -> None:
    store = _create(case)
    try:
        for scope, body in _BODIES:
            store.write(content=body, scopes=[scope])
    finally:
        store.close()


BASES: dict[str, Callable[[Path], None]] = {
    "intact": _intact,
    "rekeyed": _rekeyed,
    "restored": _restored,
    "accepted": _accepted,
    "fts": _fts,
}


def _origin_zeroed(bases: dict[str, Any]) -> str:
    data = json.loads(bytes.fromhex(bases["intact"]["keys"][".origin"]))
    data["mac"] = "0" * 64
    return json.dumps(data).encode("utf-8").hex()


def _sql(text: str) -> dict[str, Any]:
    return {"sql": text}


def _view_loop(name: str, columns: str) -> str:
    return (
        f"DROP TABLE {name}; CREATE VIEW {name} AS WITH RECURSIVE c(x) AS "
        f"(SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT {columns} FROM c;"
    )


def cases(bases: dict[str, Any]) -> dict[str, tuple[str, str, list[dict[str, Any]]]]:
    """Each case: (what it is, its base, its recipe)."""
    log_cols = (
        "x AS seq, 't' AS ts, NULL AS session, 'k' AS kind, '{}' AS payload, "
        "'' AS prev_mac, '' AS mac"
    )
    return {
        "intact": (
            "records in memories, tombstones, episodes; head and origin",
            "intact",
            [],
        ),
        "rekeyed": ("a key rotation, then a write: two segments", "rekeyed", []),
        "restored": (
            "a backup restored over a later write: a store_restored row",
            "restored",
            [],
        ),
        "accepted": (
            "a broken origin accepted: a history_accepted row",
            "accepted",
            [],
        ),
        "fts": ("search.jsonl's corpus written through Store.write", "fts", []),
        "no_head": ("the .head file removed", "intact", [{"unlink": ".head"}]),
        "no_origin": ("the .origin file removed", "intact", [{"unlink": ".origin"}]),
        "origin_broken": (
            "the .origin file's mac zeroed",
            "intact",
            [{"write": ".origin", "hex": _origin_zeroed(bases)}],
        ),
        "tampered_payload": (
            "seq 2's payload edited",
            "intact",
            [_sql("UPDATE log SET payload = payload || ' ' WHERE seq = 2;")],
        ),
        "tampered_mac": (
            "seq 3's mac edited",
            "intact",
            [_sql("UPDATE log SET mac = '" + "f" * 64 + "' WHERE seq = 3;")],
        ),
        "deleted_row": (
            "seq 3 deleted",
            "intact",
            [_sql("DELETE FROM log WHERE seq = 3;")],
        ),
        "truncated_tail": (
            "the last two rows deleted",
            "intact",
            [_sql("DELETE FROM log WHERE seq >= (SELECT MAX(seq) - 1 FROM log);")],
        ),
        "older_revision": (
            "schema_revision 5, its step 6 index dropped",
            "intact",
            [
                _sql(
                    "DROP INDEX IF EXISTS log_by_session; UPDATE meta SET value = '5' "
                    "WHERE key = 'schema_revision';"
                )
            ],
        ),
        "reader_above": (
            "min_reader_revision above this build",
            "intact",
            [
                _sql(
                    f"UPDATE meta SET value = '{SCHEMA_REVISION + 1}' WHERE key = 'min_reader_revision';"
                )
            ],
        ),
        "writer_above": (
            "min_writer_revision above this build",
            "intact",
            [
                _sql(
                    f"UPDATE meta SET value = '{SCHEMA_REVISION + 1}' WHERE key = 'min_writer_revision';"
                )
            ],
        ),
        "rollback_journal": (
            "journal_mode DELETE",
            "intact",
            [_sql("PRAGMA journal_mode = DELETE;")],
        ),
        "retired_key_missing": (
            "the retired key file removed after a rotation",
            "rekeyed",
            [{"unlink": ".*.key"}],
        ),
        # Refusals (`store_admission`). The reviewer's four first.
        "refuse_log_recursive_view": (
            "log replaced by a view over an endless recursive CTE",
            "intact",
            [_sql(_view_loop("log", log_cols))],
        ),
        "refuse_memories_recursive_view": (
            "memories replaced by a view over an endless recursive CTE",
            "intact",
            [
                _sql(
                    "DROP TRIGGER memories_ai; DROP TRIGGER memories_ad; DROP TRIGGER memories_au; "
                    "DROP TRIGGER memory_links_cleanup; DROP TABLE memories_fts; "
                    + _view_loop("memories", "x AS rowid, 'id' AS id")
                )
            ],
        ),
        "refuse_generated_column": (
            "a generated column on memories building a 100 MB string",
            "intact",
            [
                _sql(
                    "ALTER TABLE memories ADD COLUMN g TEXT GENERATED ALWAYS AS "
                    "(replace(hex(zeroblob(50000000)), '0', 'x')) VIRTUAL;"
                )
            ],
        ),
        "refuse_filtering_view": (
            "log replaced by a view that drops rows",
            "rollback_journal_base",
            [
                _sql(
                    "CREATE TABLE log_rows AS SELECT * FROM log; DROP TABLE log; "
                    "CREATE VIEW log AS SELECT * FROM log_rows WHERE seq <> 3;"
                )
            ],
        ),
        "refuse_trigger": (
            "a trigger this build never writes",
            "intact",
            [_sql("CREATE TRIGGER t_extra AFTER INSERT ON meta BEGIN SELECT 1; END;")],
        ),
        "refuse_own_trigger_changed": (
            "memories_ai with another body",
            "intact",
            [
                _sql(
                    "DROP TRIGGER memories_ai; CREATE TRIGGER memories_ai AFTER INSERT ON "
                    "memories BEGIN SELECT 1; END;"
                )
            ],
        ),
        "refuse_virtual_table": (
            "a virtual table this build never makes",
            "intact",
            [_sql("CREATE VIRTUAL TABLE extra_fts USING fts5(a);")],
        ),
        "refuse_index_expression": (
            "an index on an expression",
            "intact",
            [_sql("CREATE INDEX extra_lower ON memories(lower(body));")],
        ),
        "refuse_index_where": (
            "a partial index",
            "intact",
            [_sql("CREATE INDEX extra_partial ON memories(body) WHERE body <> '';")],
        ),
        "refuse_unique_index": (
            "a unique index on a table this build writes",
            "intact",
            [_sql("CREATE UNIQUE INDEX extra_unique ON memories(body);")],
        ),
        "refuse_default_expression": (
            "a table with a default that is not a literal",
            "intact",
            [_sql("CREATE TABLE extra_default (a TEXT DEFAULT (abs(1)));")],
        ),
        "refuse_check": (
            "a table with a check",
            "intact",
            [_sql("CREATE TABLE extra_check (a INTEGER CHECK (a > 0));")],
        ),
        "refuse_foreign_key": (
            "a table with a foreign key",
            "intact",
            [_sql("CREATE TABLE extra_fk (a TEXT REFERENCES meta(key));")],
        ),
        "refuse_missing_column": (
            "episodes without its swarm_id column",
            "intact",
            [_sql("ALTER TABLE episodes DROP COLUMN swarm_id;")],
        ),
        "refuse_long_schema_text": (
            "a table whose text runs past 16 KiB",
            "intact",
            [_sql("CREATE TABLE extra_long (a /* " + "x" * 20000 + " */);")],
        ),
        "refuse_damaged": (
            "the file cut at 4096 bytes",
            "intact",
            [{"truncate": 4096}],
        ),
        "refuse_later_revision": (
            "a view, and meta naming schema revision 7",
            "intact",
            [
                _sql(
                    "CREATE VIEW extra_view AS SELECT 1; UPDATE meta SET value = '7' "
                    "WHERE key = 'schema_revision';"
                )
            ],
        ),
        "refuse_later_reader": (
            "a view, and meta naming reader revision 8",
            "intact",
            [
                _sql(
                    "CREATE VIEW extra_view AS SELECT 1; UPDATE meta SET value = '7' "
                    "WHERE key = 'schema_revision'; UPDATE meta SET value = '8' "
                    "WHERE key = 'min_reader_revision';"
                )
            ],
        ),
        "admitted_later_table": (
            "a plain table and an index on columns a later build may add",
            "intact",
            [
                _sql(
                    "CREATE TABLE later_table (a TEXT, b INTEGER); CREATE INDEX later_ix ON later_table(a);"
                )
            ],
        ),
        "bad_utf8_name": (
            "a table whose name and tbl_name are not UTF-8 (x\\xff)",
            "intact",
            [_sql(_renamed("CREATE TABLE xx(a, b)", "(a, b)"))],
        ),
        "bad_utf8_hides_generated": (
            "a table named x\\xff whose SQL holds a generated column",
            "intact",
            [
                _sql(
                    _renamed(
                        "CREATE TABLE xx(a, b)", "(a, b AS (zeroblob(1000000000)))"
                    )
                )
            ],
        ),
        "bad_utf8_comment": (
            "a new table whose SQL has a byte that is not UTF-8 in a comment",
            "intact",
            [
                _sql(
                    "CREATE TABLE yy(a /* c */);"
                    "PRAGMA writable_schema=ON;"
                    "UPDATE sqlite_master SET sql = 'CREATE TABLE yy(a /* ' "
                    "|| CAST(x'ff' AS TEXT) || ' */)' WHERE name = 'yy';"
                )
            ],
        ),
        "blob_sql": (
            "a table whose sql is stored as a BLOB",
            "intact",
            [
                _sql(
                    "CREATE TABLE xx(a, b);"
                    "PRAGMA writable_schema=ON;"
                    "UPDATE sqlite_master SET sql = CAST(sql AS BLOB) WHERE name = 'xx';"
                )
            ],
        ),
    }


def _renamed(create: str, columns: str) -> str:
    """`create`, then its row renamed to x\\xff with SQL to match."""
    return (
        f"{create};"
        "PRAGMA writable_schema=ON;"
        "UPDATE sqlite_master SET name = CAST(x'78ff' AS TEXT), "
        "tbl_name = CAST(x'78ff' AS TEXT), "
        "sql = 'CREATE TABLE \"x' || CAST(x'ff' AS TEXT) || "
        f"'\"{columns}' WHERE name = 'xx';"
    )


#: The cases whose rows are pinned in full; the others by digest.
FULL = frozenset({"intact", "rekeyed", "restored", "accepted", "fts"})


def _materialize(
    bases: dict[str, Any], base: str, recipe: list[dict[str, Any]], case: Path
) -> None:
    if base == "rollback_journal_base":
        rebuild(bases["intact"], case)
        apply([_sql("PRAGMA journal_mode = DELETE;")], case)
    else:
        rebuild(bases[base], case)
    apply(recipe, case)


def build(bases: dict[str, Any], out: Path) -> list[dict[str, Any]]:
    """Every case under `out` and the reference's answers about each."""
    from bettermemory import __version__

    from ._sandbox import world

    records: list[dict[str, Any]] = []
    out = out.resolve()
    with world():
        for name, (what, base, recipe) in cases(bases).items():
            case = out / name
            _materialize(bases, base, recipe, case)
            result = answers(case, __version__, full=name in FULL)
            if name == "fts":
                result["fts"] = fts_answers(case)
            records.append(
                {"case": name, "what": what, "base": base, "recipe": recipe, **result}
            )
    return records


def _bases_path() -> Path:
    return GOLDEN / "store_bases.json"


def load_bases() -> dict[str, Any]:
    return dict(json.loads(_bases_path().read_text(encoding="ascii"))["bases"])


_SYNTHETIC = (
    "the restored and accepted bases' store_restored and history_accepted rows "
    "were appended directly through log.append_history_replaced with synthetic "
    "neutral paths (/fixture/...), because the product records the store's and "
    "bundle's resolved paths (backup.restore_bundle via restore_rows; "
    "key_lifecycle.accept_history), a host fact; not reproduced: the rewind of "
    "a later write, the old file moved aside, the head file and origin file "
    "rewrites; the reader reads only the rows"
)


def _bases_doc(bases: dict[str, Any]) -> bytes:
    return json_doc(
        {
            "fixture": "store_bases",
            "note": (
                "each base store as the SQL that rebuilds it and its keys "
                "directory's files (hex, by the suffix after the store id); written "
                "by `python -m tests.contract.gen_store_files --bases`; " + _SYNTHETIC
            ),
            "store_id": STORE_ID,
            "bases": bases,
        }
    )


def generate() -> dict[str, bytes]:
    from bettermemory import __version__

    bases = load_bases()
    with tempfile.TemporaryDirectory() as tmp:
        records = build(bases, Path(tmp))
    header = {
        "fixture": "store_files",
        "reference_version": __version__,
        "store_id": STORE_ID,
        "full": sorted(FULL),
        "deliberate_differences": {
            "blob_sql": (
                "the reference raises TypeError from _blanked "
                "(src/bettermemory/store_admission.py:113-115, a str pattern on "
                "bytes) when sqlite_master.sql is a BLOB, a bug not ported; the "
                "native reader refuses schema text that is not TEXT"
            ),
        },
        "raised": (
            "outcome 'raised' records what connect_read_only lets out unrefused; "
            "for schema text that is not UTF-8 the native reader refuses and "
            "carries the same OperationalError message"
        ),
        "note": (
            "each case: a base of store_bases.json rebuilt, its recipe applied, "
            "and the reference's answers; <store> stands for the store file's "
            "path. query_candidates orders by bm25 alone (ORDER BY score ASC "
            "LIMIT ?, no tie break), as the reference's SQL does; " + _SYNTHETIC
        ),
    }
    schema = {
        "texts": [
            store_schema.SCHEMA,
            store_schema.MEMBERSHIP_SCHEMA,
            store_schema.ADDITIVE_SCHEMA,
            store_schema.PROPOSALS_INDEX_SCHEMA,
            store_schema.SESSION_INDEX_SCHEMA,
        ],
        "optional_columns": {
            k: sorted(v) for k, v in store_schema._OPTIONAL_COLUMNS.items()
        },
    }
    out = {
        "store_files.jsonl": jsonl([header, *records]),
        "store_bases.json": _bases_doc(bases),
        "store_admission.json": json_doc(schema),
        "store_real.json": _real_doc(
            json.loads(_real_doc_path().read_text(encoding="utf-8"))["sqlite_version"]
        ),
    }
    out["store_real.sqlite"] = _real_path().read_bytes()
    for name, data in out.items():
        if leak := host_fact(data):
            raise AssertionError(f"{name} carries a host fact: {leak!r}")
    return out


# Paths other fixtures spell on purpose as values to encode, each the
# whole token, quotes included, exactly as it appears in its file (the
# second is the first inside a JSON string): nothing longer is exempt.
SYNTHETIC_PATHS = (
    '"/tmp/x y/\\u00e9.md"',
    '"/tmp/a b"',
    '\\"/tmp/a b\\"',
    '"/home/fixture/.local/share/bettermemory/store/memory.sqlite"',
    '"C:\\\\Users\\\\example\\\\file.txt"',
    '"C:\\\\Users\\\\fixture\\\\AppData\\\\Local\\\\bettermemory'
    '\\\\store\\\\memory.sqlite"',
)

# What a host's path looks like: POSIX temp and home roots, macOS volumes,
# Windows drive, UNC and verbatim prefixes (as text and as JSON escapes).
_HOST_PATTERNS = (
    "/private",
    "/var/folders",
    "/Users",
    "/tmp",
    "/home/",
    "/Volumes/",
    "/root/",
    "claude-501",
    "\\\\?\\",
    "\\\\\\\\?\\\\",
    "\\\\\\\\",
    ":\\Users",
    ":\\\\Users",
    ":/Users",
    "AppData",
)


def host_fact(data: bytes) -> str | None:
    """The first host path, user name or host name in `data` (text or the
    bytes of a binary file, read as Latin-1 too), if any."""
    import getpass
    import re
    import socket

    texts = [data.decode("utf-8", "replace"), data.decode("latin-1")]
    needles = list(_HOST_PATTERNS)
    for extra in (getpass.getuser(), socket.gethostname().split(".")[0]):
        if len(extra) >= 3:
            needles.append(extra)
    drive = re.compile(r"\b[A-Za-z]:(\\\\|\\|/)(?:Users|Windows|Documents)")
    for text in texts:
        for synthetic in SYNTHETIC_PATHS:
            text = text.replace(synthetic, '""')
        # The fixture world's HOME (`<scratch>/home/x`) is not a host's
        # /home/; only that one component is dropped, so a user or host name
        # further down the path is still caught.
        text = re.sub(r"<scratch>/home(?=/|\b)", "<scratch>", text)
        if hit := next((n for n in needles if n in text), None):
            return hit
        if found := drive.search(text):
            return found.group(0)
    return None


def write_bases() -> None:
    """Write every base through the reference, prove each rebuilt store
    answers as its original, and write store_bases.json."""
    from bettermemory import __version__

    from ._sandbox import world

    bases: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp, world():
        root = Path(tmp)
        for name, make in BASES.items():
            original = root / "original" / name
            with _fixed_random():
                make(original)
            bases[name] = dump(original / "memory.sqlite", original / "keys")
            copy = root / "rebuilt" / name
            rebuild(bases[name], copy)
            want = answers(original, __version__, full=True)
            got = answers(copy, __version__, full=True)
            want.pop("sqlite_master"), got.pop("sqlite_master")
            want_master = [r for r in _master(original)]
            got_master = [r for r in _master(copy)]
            if want != got or want_master != got_master:
                differs = sorted(k for k in want if want[k] != got.get(k))
                if want_master != got_master:
                    differs.append("sqlite_master")
                detail: dict[str, Any] = {
                    k: (want.get(k), got.get(k)) for k in differs if k != "tables"
                }
                if "tables" in differs:
                    detail["tables"] = sorted(
                        t
                        for t in want["tables"]
                        if want["tables"][t] != got["tables"].get(t)
                    )
                raise AssertionError(
                    f"{name}: the rebuilt store answers otherwise: {detail}"
                )
            if name == "fts" and fts_answers(original) != fts_answers(copy):
                raise AssertionError("fts: the rebuilt store ranks otherwise")
    _bases_path().write_bytes(_bases_doc(bases))


# ---------------------------------------------------------------------------
# One real file, as the reference's SQLite wrote it
# ---------------------------------------------------------------------------

#: The page size the real file is compacted to: SQLite's smallest. The
#: store sets 8 KiB when it creates a file, and nothing it reads checks it.
REAL_PAGE_SIZE = 512


def _real_path() -> Path:
    return GOLDEN / "store_real.sqlite"


def _real_doc_path() -> Path:
    return GOLDEN / "store_real.json"


def _real(case: Path) -> None:
    """`_intact`'s records, `_fts`'s corpus, then compacted (VACUUM at
    `REAL_PAGE_SIZE`) and put back in WAL mode, by the reference's own
    SQLite."""
    _intact(case)
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys")
    try:
        for scope, body in _BODIES:
            store.write(content=body, scopes=[scope])
    finally:
        store.close()
    conn = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(f"PRAGMA page_size = {REAL_PAGE_SIZE}")
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()


_VOLATILE = (
    (re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)?"), "<ts>"),
    (re.compile(r"[0-9A-HJKMNP-TV-Z]{26}"), "<ulid>"),
    (re.compile(r"[0-9a-f]{64}"), "<hex>"),
)


def _masked(dumped: dict[str, Any]) -> dict[str, Any]:
    """A dump with what the clock mints (times, ULIDs, and the MACs and
    digests over them) masked: what two builds of `_real` share."""

    def mask(text: str) -> str:
        for pattern, stand in _VOLATILE:
            text = pattern.sub(stand, text)
        return text

    return {
        "page_size": dumped["page_size"],
        "journal_mode": dumped["journal_mode"],
        "statements": [mask(s) for s in dumped["statements"]],
        "keys": {
            k: mask(bytes.fromhex(v).decode("latin-1"))
            for k, v in dumped["keys"].items()
        },
    }


def _writer_version(store_file: Path) -> str:
    """The SQLite that last wrote `store_file`: its header's version-valid-for
    number (bytes 96-99), as ``X.Y.Z``."""
    with open(store_file, "rb") as handle:
        header = handle.read(100)
    n = struct.unpack(">I", header[96:100])[0]
    return f"{n // 1000000}.{n // 1000 % 1000}.{n % 1000}"


def _real_answers(store_bytes: bytes, keys: dict[str, str], version: str) -> Any:
    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "real"
        (case / "keys").mkdir(parents=True)
        (case / "memory.sqlite").write_bytes(store_bytes)
        for suffix, text in keys.items():
            (case / "keys" / f"{STORE_ID}{suffix}").write_bytes(bytes.fromhex(text))
        result = answers(case, version, full=True)
        result["fts"] = fts_answers(case)
        return result


def _real_doc(written_by: str) -> bytes:
    """store_real.json from the committed file and keys: its provenance,
    and the reference's answers about it as it is."""
    from bettermemory import __version__

    from ._sandbox import world

    committed = json.loads(_real_doc_path().read_text(encoding="utf-8"))
    store_bytes = _real_path().read_bytes()
    if _writer_version(_real_path()) != written_by:
        raise AssertionError(
            f"store_real.sqlite was last written by SQLite "
            f"{_writer_version(_real_path())}, its header says {written_by}"
        )
    with world():
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh"
            with _fixed_random():
                _real(fresh)
            committed_case = Path(tmp) / "committed"
            (committed_case / "keys").mkdir(parents=True)
            (committed_case / "memory.sqlite").write_bytes(store_bytes)
            for suffix, text in committed["keys"].items():
                (committed_case / "keys" / f"{STORE_ID}{suffix}").write_bytes(
                    bytes.fromhex(text)
                )
            want = _masked(dump(fresh / "memory.sqlite", fresh / "keys"))
            got = _masked(
                dump(committed_case / "memory.sqlite", committed_case / "keys")
            )
            if want != got:
                raise AssertionError(
                    "store_real.sqlite no longer dumps as a fresh build does; "
                    "rewrite it with --real"
                )
        result = _real_answers(store_bytes, committed["keys"], __version__)
    return json_doc(
        {
            "fixture": "store_real",
            "note": (
                "store_real.sqlite as the reference's SQLite wrote it (not "
                "rebuilt): _intact's records and the FTS corpus, VACUUMed at "
                f"page size {REAL_PAGE_SIZE}, in WAL mode; its bytes are not "
                "reproducible, so generate compares a logical dump of it, "
                "clock-minted values masked, with a fresh build, and records "
                "the reference's answers about it as it is; written by "
                "`python -m tests.contract.gen_store_files --real`"
            ),
            "reference_version": __version__,
            "sqlite_version": written_by,
            "file_sha256": hashlib.sha256(store_bytes).hexdigest(),
            "file_bytes": len(store_bytes),
            "store_id": STORE_ID,
            "keys": committed["keys"],
            "answers": result,
        }
    )


def write_real() -> None:
    """Write store_real.sqlite through the reference, and its document."""
    from ._sandbox import world

    with world(), tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "real"
        with _fixed_random():
            _real(case)
        dumped = dump(case / "memory.sqlite", case / "keys")
        _real_path().write_bytes((case / "memory.sqlite").read_bytes())
        _real_doc_path().write_bytes(
            json_doc({"keys": dumped["keys"], "sqlite_version": sqlite3.sqlite_version})
        )
    _real_doc_path().write_bytes(_real_doc(sqlite3.sqlite_version))


def _master(case: Path) -> list[Any]:
    conn = sqlite3.connect(
        f"file:{case / 'memory.sqlite'}?mode=ro&immutable=1", uri=True
    )
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if argv == ["--bases"]:
        write_bases()
        return 0
    if argv == ["--real"]:
        write_real()
        return 0
    print(
        "usage: python -m tests.contract.gen_store_files --bases | --real",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
