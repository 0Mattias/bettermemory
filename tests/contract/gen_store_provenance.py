"""Fixture: a record's provenance, read (``store_provenance.jsonl``).

The native build ports `store_provenance.py`'s readers (``native/``,
`store::provenance`) and is held to this file. Each case starts from the
store `Store.create` makes under ``store_create.json``'s fixed values (its
key, ``KEY_A``, its store id), then appends log rows with the reference's
own `log.append_row` under a chosen key and applies each record mutation
with `_apply_mutation`, as `_mutate` does, every clock fixed; then plants
by SQL (a label, a pointer, a log row altered) and lays out the key ring
(keys removed, retired, added; the origin anchor rewritten). The store is
then opened read-only (`Store.open(..., read_only=True)`) under the case's
``strict_provenance`` and every reader is asked: `provenance_for`,
`trust_rows`, `unaccounted_ids`, `provenance_counts`, `links_with_status`
and `episode_provenance`, on one connection, in the case's order.

Recorded: the rows of the six tables the readers read (``meta``,
``memories``, ``episodes``, ``log``, ``verifications``, ``memory_links``),
the key ring's key and origin files, whether the open verifies
(`can_verify`) and under which fingerprint, and each answer (dicts as
lists of pairs, in their order) or the exception's class and text. A
blob is ``{"$bytes": hex}``.

Gates, the same as ``store_tx.jsonl``'s and for the same reasons. Unicode:
the rows include ``body_fts`` and ``scopes_fts``, the tokenizer's spelling.
pydantic-core: the records are built through the models. POSIX only: the
reference's create and open write and check the key ring's modes and its
binding. Not on the Python version: no text recorded is the
interpreter's (a payload nested past the recursion limit is left to the
cross test, which runs the reference itself). Not on the SQLite version:
the answers' order is the one SQLite gives these statements, which no
release since FTS5 has changed for an `IN` list on a unique column.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import log as blog
from bettermemory.models import Episode
from bettermemory.store import Store
from bettermemory.store_native import prepare_mutation
from bettermemory.store_rows import _apply_mutation

from . import gen_store_create
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_log import KEY_A, STORE_ID
from .gen_store_write import memory, ulid

KEY_B = bytes(range(32, 64))
KEY_Z = bytes(range(64, 96))
TABLES = ("meta", "memories", "episodes", "log", "verifications", "memory_links")
TS0 = datetime(2003, 4, 5, 6, 7, 8, 900000, tzinfo=timezone.utc)


def fp(key: bytes) -> str:
    return blog.fingerprint(key)


def m(n: int) -> str:
    return ulid(n)


def e(n: int) -> str:
    return ulid(n, "01E")


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, float):
        return {"$float": repr(value)}
    return value


class Builder:
    """The case's store: rows appended under a chosen key, mutations
    applied as `_mutate` applies them."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.clock = 0

    def row(
        self, key: bytes, kind: str, payload: dict[str, Any], seq: int | None = None
    ) -> blog.LogRow:
        self.clock += 1
        ts = (TS0 + timedelta(seconds=self.clock)).isoformat().replace("+00:00", "Z")
        if seq is None:
            return blog.append_row(
                self.conn, key=key, kind=kind, payload=payload, session="s1", ts=ts
            )
        text = blog.canonical_payload(payload)
        mac = blog.compute_mac(
            key,
            seq=seq,
            ts=ts,
            session="s1",
            kind=kind,
            payload=text,
            prev_mac=blog.GENESIS_MAC,
        )
        self.conn.execute(
            "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, ts, "s1", kind, text, blog.GENESIS_MAC, mac),
        )
        return blog.LogRow(seq, ts, "s1", kind, text, blog.GENESIS_MAC, mac)

    def put(self, key: bytes, kind: str, payload: dict[str, Any]) -> blog.LogRow:
        payload = dict(prepare_mutation(self.conn, kind, payload))
        row = self.row(key, kind, payload)
        _apply_mutation(self.conn, kind, payload, log_mac=row.mac)
        return row

    def mem(self, n: int, key: bytes = KEY_A, provenance: str = "local") -> blog.LogRow:
        payload = memory(n)
        payload["provenance"] = provenance
        return self.put(key, "memory_put", payload)

    def episode(self, n: int, key: bytes = KEY_A) -> blog.LogRow:
        ep = Episode.model_validate(
            {
                "id": e(n),
                "session_id": "s-ep",
                "created": "2002-03-04T05:06:07.000008Z",
                "body": f"episode {n}\n",
            }
        )
        return self.put(key, "episode_put", {"episode": ep.model_dump(mode="json")})

    def rekey(self, signer: bytes, old: str, new: str) -> blog.LogRow:
        payload = {"old_fingerprint": old, "new_fingerprint": new}
        return self.row(signer, blog.REKEY, payload)

    def sql(self, statement: str, *params: Any) -> None:
        self.conn.execute(statement, params)


def _ring(keys: Path, ring: dict[str, Any]) -> None:
    """Lay out the key ring: ``current`` (a key, None, ``KEY_A`` when not
    given), ``retired``
    (keys), ``origin`` (None to remove, ``keep``, or a dict to write)."""
    current = keys / f"{STORE_ID}.key"
    current.unlink(missing_ok=True)
    for old in keys.glob(f"{STORE_ID}.*.key"):
        old.unlink()
    key = ring.get("current", KEY_A)
    if key is not None:
        current.write_bytes(key)
    for key in ring.get("retired", []):
        (keys / f"{STORE_ID}.{fp(key)[:16]}.key").write_bytes(key)
    origin = ring.get("origin", "keep")
    path = keys / f"{STORE_ID}.origin"
    if origin is None:
        path.unlink(missing_ok=True)
    elif origin != "keep":
        path.write_text(json.dumps(origin))


def _answer(call: Callable[[], Any], shape: Callable[[Any], Any]) -> dict[str, Any]:
    try:
        value = call()
    except Exception as exc:
        return {"raised": [type(exc).__name__, str(exc)]}
    return {"returned": shape(value)}


def _pairs(d: dict[str, Any]) -> list[list[Any]]:
    return [[k, v] for k, v in d.items()]


def _links(links: list[tuple[Any, Any, Any]]) -> list[list[Any]]:
    return [[_cell(a), _cell(b), _cell(c)] for a, b, c in links]


def _trust(row: Any) -> Any:
    return None if row is None else [row.provenance, row.verified_locally_at]


def ask(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    call = query["call"]
    ids: list[str] = query.get("ids") or []
    if call == "provenance_for":
        return _answer(lambda: store.provenance_for(ids), _pairs)
    if call == "trust_rows":
        return _answer(
            lambda: store.trust_rows(ids),
            lambda r: None if r is None else [[k, *_trust(v)] for k, v in r.items()],
        )
    if call == "unaccounted_ids":
        return _answer(store.unaccounted_ids, list)
    if call == "provenance_counts":
        return _answer(store.provenance_counts, _pairs)
    if call == "links_with_status":
        return _answer(
            lambda: store.links_with_status(query["id"]),
            lambda r: {"out": _links(r[0]), "in": _links(r[1]), "trust": _trust(r[2])},
        )
    if call == "episode_provenance":
        return _answer(lambda: store.episode_provenance(ids), _pairs)
    raise AssertionError(call)


def every(
    ids: list[str], episodes: list[str], links: list[str]
) -> list[dict[str, Any]]:
    """Every reader, over `ids` with an unknown id and a repeat."""
    asked = [*ids, ids[0] if ids else m(999), m(999)]
    out: list[dict[str, Any]] = [
        {"call": "provenance_for", "ids": asked},
        {"call": "trust_rows", "ids": asked},
        {"call": "unaccounted_ids"},
        {"call": "provenance_counts"},
        {"call": "episode_provenance", "ids": [*episodes, e(999)]},
    ]
    out += [{"call": "links_with_status", "id": i} for i in links]
    return out


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


def _keys(keys: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(keys.iterdir()):
        name = path.name.replace(STORE_ID, "<id>")
        if path.name.endswith(".key"):
            out[name] = path.read_bytes().hex()
        elif path.name.endswith(".origin"):
            out[name] = path.read_text()
    return out


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    with world(), tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "c"
        root.mkdir()
        with gen_store_create._fixed():
            Store.create(root, keys_dir=root / "keys").close()
        file = root / "memory.sqlite"
        conn = sqlite3.connect(file, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        wall = Ticker(TS0, timedelta(milliseconds=1))
        with fixed(log=Ticker(TS0, timedelta(seconds=1)), wall=wall):
            case["build"](Builder(conn))
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = _tables(conn)
        conn.close()
        _ring(root / "keys", case.get("ring", {}))
        keys = _keys(root / "keys")
        store = Store.open(
            file,
            keys_dir=root / "keys",
            read_only=True,
            strict_provenance=case.get("strict", False),
        )
        try:
            verify = store._verify_key
            answers = [{**q, **ask(store, q)} for q in case["queries"]]
        finally:
            store.close()
    return {
        "case": case["name"],
        "strict": case.get("strict", False),
        "verifies": None if verify is None else fp(verify),
        "tables": tables,
        "keys": keys,
        "queries": answers,
    }


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def _labels(b: Builder) -> None:
    """Every stored label, and every way a pointer fails or holds."""
    first = b.mem(1)
    b.mem(2, provenance="imported")
    b.mem(3, provenance="synced")
    b.mem(4)
    b.sql("UPDATE memories SET provenance = 'unaccounted' WHERE id = ?", m(4))
    b.mem(5)
    b.sql("UPDATE memories SET provenance = 'elsewhere' WHERE id = ?", m(5))
    b.mem(6)
    b.sql("UPDATE memories SET log_mac = '' WHERE id = ?", m(6))
    b.mem(7)
    b.sql("UPDATE memories SET log_mac = NULL WHERE id = ?", m(7))
    b.mem(8)
    b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", "ab" * 32, m(8))
    b.mem(9)
    # Names another record's row.
    b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", first.mac, m(9))
    altered = b.mem(10)
    b.sql(
        "UPDATE log SET payload = replace(payload, 'record number', 'edited') "
        "WHERE seq = ?",
        altered.seq,
    )
    b.mem(11)
    b.sql("UPDATE memories SET log_mac = CAST(log_mac AS BLOB) WHERE id = ?", m(11))
    # A row of another kind under the pointer.
    ep = b.episode(1)
    b.mem(12)
    b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", ep.mac, m(12))
    # Rows signed under the key whose payloads name the record oddly.
    for n, payload in (
        (13, "not json"),
        (14, ["memory"]),
        (15, {"memory": ["id"]}),
        (16, {"memory": {"body": "no id"}}),
        (17, {"other": {"id": m(17)}}),
        (18, {"memory": "id"}),
        (19, {"memory": {"id": [m(19)]}}),
    ):
        b.mem(n)
        if isinstance(payload, str):
            row = b.row(KEY_A, "memory_put", {})
            b.sql("UPDATE log SET payload = ? WHERE seq = ?", payload, row.seq)
            mac = blog.compute_mac(
                KEY_A,
                seq=row.seq,
                ts=row.ts,
                session=row.session,
                kind=row.kind,
                payload=payload,
                prev_mac=row.prev_mac,
            )
            b.sql("UPDATE log SET mac = ? WHERE seq = ?", mac, row.seq)
        else:
            mac = b.row(KEY_A, "memory_put", payload).mac  # type: ignore[arg-type]
        b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", mac, m(n))
    # An id that is not a ULID, named by a number the payload holds.
    numeric = b.row(KEY_A, "memory_put", {"memory": {"id": 20}})
    b.mem(20)
    b.sql("UPDATE memories SET id = '20', log_mac = ? WHERE id = ?", numeric.mac, m(20))
    # A row below seq 1, signed under the key.
    low = b.row(KEY_A, "memory_put", {"memory": {"id": m(21)}}, seq=0)
    b.mem(21)
    b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", low.mac, m(21))
    # Episodes: verified, no pointer, another kind's row, altered.
    b.episode(2)
    b.sql("UPDATE episodes SET log_mac = NULL WHERE id = ?", e(2))
    b.episode(3)
    b.sql("UPDATE episodes SET log_mac = ? WHERE id = ?", first.mac, e(3))
    ep4 = b.episode(4)
    b.sql("UPDATE log SET ts = 'x' WHERE seq = ?", ep4.seq)
    # This host's stamps and another machine's; links of every target.
    b.sql(
        "INSERT INTO verifications (memory_id, machine_id, verified_at) "
        "VALUES (?, NULL, '2005-01-01T00:00:00Z'), (?, 'm-other', '2005-02-02T00:00:00Z'), "
        "(?, NULL, '2005-03-03T00:00:00Z')",
        m(1),
        m(2),
        m(10),
    )
    b.sql(
        "INSERT INTO memory_links (source_id, type, target_id, note) VALUES "
        "(?, 'refines', ?, NULL), (?, 'refines', ?, 'gone'), (?, 'cites', ?, 'x'), "
        "(?, 'cites', ?, NULL), (?, 'cites', ?, NULL), (?, 'cites', ?, 'n')",
        m(1),
        m(2),
        m(1),
        m(98),
        m(1),
        m(10),
        m(3),
        m(1),
        m(1),
        m(4),
        m(1),
        m(4),
    )


LABEL_IDS = [m(n) for n in range(1, 22) if n != 20] + ["20"]
LABEL_EPISODES = [e(n) for n in range(1, 5)]
LABEL_LINKS = [m(1), m(10), m(98), m(2)]


def _rekeyed(b: Builder) -> None:
    """Rows under A, a rekey to B, rows under B."""
    b.mem(1)
    altered = b.mem(2)
    b.sql("UPDATE log SET session = 's9' WHERE seq = ?", altered.seq)
    b.episode(1)
    b.rekey(KEY_B, fp(KEY_A), fp(KEY_B))
    b.mem(3, key=KEY_B)
    forged = b.mem(4, key=KEY_B)
    b.sql(
        "UPDATE log SET kind = 'memory_put' , prev_mac = 'x' WHERE seq = ?", forged.seq
    )
    b.episode(2, key=KEY_B)
    b.sql("UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", fp(KEY_B))


def _foreign(b: Builder) -> None:
    """A held segment, a rekey to a key not held, rows signed by that key
    and a row this machine's key signed into it."""
    b.mem(1)
    b.rekey(KEY_Z, fp(KEY_A), fp(KEY_Z))
    b.mem(2, key=KEY_Z)
    b.mem(3, key=KEY_A)
    b.episode(1, key=KEY_Z)
    b.episode(2, key=KEY_A)


def _signed_as(b: Builder, row: blog.LogRow, value: Any, key: bytes = KEY_A) -> str:
    """Store ``value`` as the row's payload and sign the row over
    ``str(value)``, the text the reference's MAC reads; the new MAC."""
    mac = blog.compute_mac(
        key,
        seq=row.seq,
        ts=row.ts,
        session=row.session,
        kind=row.kind,
        payload=str(value),
        prev_mac=row.prev_mac,
    )
    b.sql("UPDATE log SET payload = ?, mac = ? WHERE seq = ?", value, mac, row.seq)
    return mac


def _named(n: int) -> str:
    return json.dumps({"memory": {"id": m(n)}}, separators=(",", ":"))


def blob_payloads() -> list[tuple[bytes, bool]]:
    """(payload bytes, signed over their ``repr``), record n = 1 + index."""
    out: list[tuple[bytes, bool]] = []
    for n, (encode, signed) in enumerate(
        [
            (lambda t: t.encode(), False),
            (lambda t: t.encode(), True),
            (lambda t: t.encode("utf-8-sig"), True),
            (lambda t: t.encode("utf-16-le"), True),
            (lambda t: t.encode("utf-16-be"), True),
            (lambda t: b"\xff\xfe" + t.encode("utf-16-le"), True),
            (lambda t: b"\xfe\xff" + t.encode("utf-16-be"), True),
            (lambda t: t.encode("utf-32-le"), True),
            (lambda t: t.encode("utf-32-be"), True),
            (lambda t: b"\xff\xfe\x00\x00" + t.encode("utf-32-le"), True),
            (lambda t: b"\x00\x00\xfe\xff" + t.encode("utf-32-be"), True),
            (lambda t: b"\x80" + t.encode(), True),
            (lambda t: t.encode("utf-16-le")[:-1], True),
            (lambda t: b"", True),
            (lambda t: t.replace("{", "{'").encode(), True),
        ],
        start=1,
    ):
        out.append((encode(_named(n)), signed))
    return out


def _blobs(b: Builder) -> None:
    """Log payloads stored as bytes, and rows of another kind."""
    for n, (payload, signed) in enumerate(blob_payloads(), start=1):
        b.mem(n)
        row = b.row(KEY_A, "memory_put", {"memory": {"id": m(n)}})
        if signed:
            mac = _signed_as(b, row, payload)
        else:
            b.sql("UPDATE log SET payload = ? WHERE seq = ?", payload, row.seq)
            mac = row.mac
        b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", mac, m(n))
    # Correctly signed rows of another kind that name the record.
    for n, kind in ((40, "memory_delete"), (41, "Memory_put"), (42, "memory_put ")):
        b.mem(n)
        mac = b.row(KEY_A, kind, {"memory": {"id": m(n)}}).mac
        b.sql("UPDATE memories SET log_mac = ? WHERE id = ?", mac, m(n))


BLOB_IDS = [m(n) for n in range(1, len(blob_payloads()) + 1)] + [m(40), m(41), m(42)]


def _plain(b: Builder) -> None:
    b.mem(1)
    b.mem(2, provenance="imported")
    tampered = b.mem(3)
    b.sql("UPDATE log SET payload = payload || ' ' WHERE seq = ?", tampered.seq)
    b.episode(1)


def _many(b: Builder) -> None:
    for n in range(1, 4):
        b.mem(n)
    b.sql("UPDATE memories SET log_mac = NULL WHERE id = ?", m(2))


def _nothing(b: Builder) -> None:
    return None


PLAIN_IDS = [m(1), m(2), m(3)]
REKEYED_IDS = [m(1), m(2), m(3), m(4)]
FOREIGN_IDS = [m(1), m(2), m(3)]
ORIGIN_SEQ1 = "keep"


def _origin_text(mac: str, seq: int = 1, key: bytes | None = KEY_A) -> dict[str, Any]:
    out: dict[str, Any] = {"seq": seq, "mac": mac}
    if key is not None:
        out["key_fingerprint"] = fp(key)
    return out


def _many_ids() -> list[str]:
    ids = [m(1000 + n) for n in range(502)]
    ids[0], ids[499], ids[500], ids[501] = m(3), m(2), m(1), m(3)
    return ids


def cases() -> list[dict[str, Any]]:
    rekeyed_q = every(REKEYED_IDS, [e(1), e(2)], [m(1)])
    return [
        {
            "name": "labels",
            "build": _labels,
            "queries": every(LABEL_IDS, LABEL_EPISODES, LABEL_LINKS),
        },
        {
            "name": "key_missing",
            "build": _plain,
            "ring": {"current": None},
            "queries": every(PLAIN_IDS, [e(1)], [m(1)]),
        },
        {
            "name": "meta_names_another_key",
            "build": _plain,
            "ring": {"current": KEY_A},
            "queries": every(PLAIN_IDS, [e(1)], [m(1)]),
            "edit_meta": True,
        },
        {
            "name": "rekeyed_both_held",
            "build": _rekeyed,
            "ring": {"current": KEY_B, "retired": [KEY_A]},
            "queries": rekeyed_q,
        },
        {
            "name": "rekeyed_leading_key_absent",
            "build": _rekeyed,
            "ring": {"current": KEY_B},
            "queries": rekeyed_q,
        },
        {
            "name": "rekeyed_leading_key_absent_strict",
            "build": _rekeyed,
            "ring": {"current": KEY_B},
            "strict": True,
            "queries": rekeyed_q,
        },
        {
            "name": "rekeyed_no_origin",
            "build": _rekeyed,
            "ring": {"current": KEY_B, "origin": None},
            "queries": rekeyed_q,
        },
        {
            "name": "rekeyed_origin_mac_differs",
            "build": _rekeyed,
            "ring": {"current": KEY_B, "origin": _origin_text("cd" * 32)},
            "queries": rekeyed_q,
        },
        {
            "name": "rekeyed_origin_row_missing",
            "build": _rekeyed,
            "ring": {"current": KEY_B, "origin": _origin_text("cd" * 32, seq=900)},
            "queries": rekeyed_q,
        },
        {
            "name": "foreign_segment",
            "build": _foreign,
            "ring": {"current": KEY_A},
            "queries": every(FOREIGN_IDS, [e(1), e(2)], [m(2)]),
        },
        {
            "name": "foreign_segment_no_origin",
            "build": _foreign,
            "ring": {"current": KEY_A, "origin": None},
            "queries": every(FOREIGN_IDS, [e(1), e(2)], []),
        },
        {
            "name": "batches",
            "build": _many,
            "queries": [
                {"call": "provenance_for", "ids": _many_ids()},
                {"call": "trust_rows", "ids": _many_ids()},
                {"call": "episode_provenance", "ids": [e(n) for n in range(501)]},
            ],
        },
        {
            "name": "blob_payloads_and_kinds",
            "build": _blobs,
            "queries": every(BLOB_IDS, [], []),
        },
        {
            "name": "empty",
            "build": _nothing,
            "queries": [
                *every([], [], [m(1)]),
                {"call": "provenance_for", "ids": []},
                {"call": "trust_rows", "ids": []},
                {"call": "episode_provenance", "ids": []},
            ],
        },
    ]


#: `str(bytes)` samples: every byte value, and each quote rule.
REPR_SAMPLES = [
    bytes(range(256)),
    b"",
    b"'",
    b'"',
    b"'\"",
    b"a'b",
    b'a"b',
    b"\\'\t\n\r",
]

#: `json.loads(bytes)`'s decoding: detection and `surrogatepass`.
JSON_BYTES = [
    b"",
    b"a",
    b"ab",
    b"\x00a",
    b"a\x00",
    b"abc",
    b"\x00\xd8",
    b"\x00\xdc",
    b"\x00\xd8a\x00",
    b"\x00\xd8\x00\xdc",
    b"\xff\xfe\x00\xd8\x00\xdc",
    b"\xfe\xff\xd8\x00",
    b"\xfe\xff\xd8",
    b"a\x00\x00\x00\x00\xd8\x00\x00",
    b"\x00\x00\x00a\x00\x11\x00\x00",
    b"a\x00\x00\x00\x00\x00\x11\x00",
    b"\xff\xfe\x00\x00a\x00\x00",
    b"\x00\x00\xfe\xff\x00\x00\x00a",
    b"\xef\xbb\xbf{}",
    b"\xef\xbb\xbf\xef\xbb\xbf",
    b"\xed\xa0\x80",
    b"\xed\xa0\x80\xed\xb0\x80",
    b"\xed\xa0",
    b"\xc0\x80",
    b"\xc2\x80",
    b"\xe0\x9f\xbf",
    b"\xe0\xa0\x80",
    b"\xf0\x8f\xbf\xbf",
    b"\xf0\x90\x80\x80",
    b"\xf4\x8f\xbf\xbf",
    b"\xf4\x90\x80\x80",
    b"\xf5\x80\x80\x80",
    b"\x80",
    b"\xff",
    b"a\xff\x00b",
    "caf\u00e9 \U0001f600".encode(),
    "caf\u00e9 \U0001f600".encode("utf-16-le"),
    "caf\u00e9 \U0001f600".encode("utf-32-be"),
]


def json_bytes() -> list[list[Any]]:
    out: list[list[Any]] = []
    for sample in JSON_BYTES:
        try:
            text = sample.decode(json.detect_encoding(sample), "surrogatepass")
        except UnicodeDecodeError:
            out.append([sample.hex(), None])
        else:
            out.append([sample.hex(), [ord(c) for c in text]])
    return out


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_provenance",
            "store_id": STORE_ID,
            "tables": list(TABLES),
            "json_bytes": json_bytes(),
            "bytes_repr": [[v.hex(), str(v).encode().hex()] for v in REPR_SAMPLES],
        }
    ]
    for case in cases():
        if case.pop("edit_meta", False):
            build = case["build"]

            def edited(b: Builder, build: Any = build) -> None:
                build(b)
                b.sql(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                    fp(KEY_Z),
                )

            case["build"] = edited
        lines.append(run_case(case))
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_provenance.jsonl": text.encode("ascii")}
