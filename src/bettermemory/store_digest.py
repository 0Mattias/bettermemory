"""The store's digest: what a write asks of every record, without loading them.

A write compares its body with every active and removed record (the dedup
gates, the supersession detector) and reads every record's scopes and
working directory (the scope check). Loading and tokenising the corpus for
each of those made a write's cost grow with the store. The digest keeps a
narrow row per record instead (`digest_records`: its raw content tokens,
their count, its claim size, its scopes and its working directory) and the
postings of those tokens (`digest_tokens`), so a write reads the few
records that can score and nothing else.

Three rules keep it honest:

- It is derived. No log row carries it, no fold reads it, and a build that
  does not know it (an older release) leaves it alone. Dropping all three
  tables loses nothing: the next write builds them again.
- It follows the log. `digest_state` holds the sequence number and MAC of
  the last record mutation the digest has seen. A call first compares that
  with the log (`_mark`), and applies the mutation rows after it
  (`_ingest`) before it answers, whichever build wrote them. A different
  tokenizer, or a log whose row at that number no longer carries that MAC,
  rebuilds from the records.
- It never decides. `candidates` returns a superset of the records an
  exact scorer can act on (the bound is in its docstring); the scorers in
  `search_dedup` and `supersession` then run unchanged on that superset.
  Where the digest cannot be written (a store open for reading), every
  caller falls back to the full scan it replaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence, Set as AbstractSet
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from .search_dedup import (
    _CONTAINMENT_MIN_TOKENS,
    MEDIUM_SIMILARITY,
    raw_content_tokens,
)
from .search_lexicon import _STOPWORDS, _kebab_parts, tokenizer_fingerprint
from .store_rows import _dt, _load_json

if TYPE_CHECKING:
    from .models import Memory, TombstonedMemory
    from .store import Store

log = logging.getLogger("bettermemory.store")

#: The record mutations the digest follows: the kinds that put or delete a
#: row of `memories` or `tombstones`.
_KINDS = ("memory_put", "memory_delete", "tombstone_put", "tombstone_delete")

#: Records indexed per transaction while the digest is built, so a build on
#: a large store holds the write lock for one batch at a time.
_BUILD_BATCH = 2000

#: Ids per ``IN (...)`` list, under SQLite's oldest default variable limit.
_IN_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_records (
    doc INTEGER PRIMARY KEY,
    removed INTEGER NOT NULL DEFAULT 0,
    id TEXT NOT NULL DEFAULT '',
    rid INTEGER NOT NULL DEFAULT 0,
    n INTEGER NOT NULL DEFAULT 0,
    claim INTEGER NOT NULL DEFAULT 0,
    stamped INTEGER NOT NULL DEFAULT 0,
    tokens TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    cwd TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS digest_records_by_id ON digest_records(removed, id);
CREATE TABLE IF NOT EXISTS digest_tokens (
    token TEXT NOT NULL DEFAULT '',
    doc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token, doc)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS digest_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""

_TABLES = ("digest_records", "digest_tokens", "digest_state")


class DigestDoc(NamedTuple):
    """One record as the digest holds it. `n` is the count of its raw
    content tokens; `claim` the count of its provenance-stripped body's
    when that body is claim-sized (`supersession.claim_sized`), else 0;
    `stamped` says the body carries a provenance stamp, so `tokens` (the
    whole body's) are not the stripped body's."""

    id: str
    n: int
    claim: int
    stamped: bool
    tokens: frozenset[str]


class _Features(NamedTuple):
    tokens: frozenset[str]
    claim: int
    stamped: bool


def _features(body: str) -> _Features:
    """What the digest keeps of a body: the token set the scorers read
    (`search_dedup.raw_content_tokens`), and its claim size as the
    supersession detector reads it."""
    from .supersession import _strip_provenance, claim_sized

    tokens = raw_content_tokens(body)
    stripped = _strip_provenance(body)
    stamped = stripped != body
    claim = len(raw_content_tokens(stripped)) if claim_sized(stripped) else 0
    return _Features(tokens, claim, stamped)


def expanded(tokens: AbstractSet[str]) -> set[str]:
    """`tokens` with every compound's parts beside it, stopword parts left
    out: every token `_pairwise_content_jaccard` can count for the body,
    whatever it is compared with. The postings hold this set, and a query
    asks with it."""
    out = set(tokens)
    for token in tokens:
        out.update(p for p in _kebab_parts(token) if p not in _STOPWORDS)
    return out


_FINGERPRINT: str | None = None

#: Bodies whose features name the rules the digest was built under: a
#: compound, a stopword, a dotted number, a stamped claim and a body past
#: the claim size.
_PROBES = (
    "Claude-Code reads feature_flags from the daemon's config on 3.12.1.",
    "The and of to a an.",
    "Port moved to 9443.\n\n_(consolidate --llm --from-transcript: x)_",
    " ".join(f"word{i}." for i in range(120)),
)


def fingerprint() -> str:
    """The rules a digest row was computed under: the tokenizer's own
    fingerprint and what `_features` and `expanded` give the probes. A
    digest whose state names another is rebuilt."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        from . import supersession

        parts: list[Any] = [
            tokenizer_fingerprint(),
            supersession.MAX_CLAIM_TOKENS,
            supersession.MAX_CLAIM_SENTENCES,
            supersession._SENTENCE_RE.pattern,
            supersession._PROVENANCE_RE.pattern,
        ]
        for probe in _PROBES:
            found = _features(probe)
            parts.append(
                [
                    sorted(found.tokens),
                    sorted(expanded(found.tokens)),
                    found.claim,
                    found.stamped,
                ]
            )
        text = json.dumps(parts, sort_keys=True, ensure_ascii=True)
        _FINGERPRINT = hashlib.sha256(text.encode("ascii")).hexdigest()
    return _FINGERPRINT


# -- the state ---------------------------------------------------------------


def _state(conn: sqlite3.Connection) -> dict[str, str] | None:
    """`digest_state` as a dict, or None for a store with no digest."""
    try:
        return {
            str(k): str(v)
            for k, v in conn.execute("SELECT key, value FROM digest_state")
        }
    except sqlite3.OperationalError:
        return None


def _set_state(conn: sqlite3.Connection, **values: str) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO digest_state(key, value) VALUES (?, ?)",
        list(values.items()),
    )


def _head(conn: sqlite3.Connection) -> int:
    """The sequence number of the log's last record mutation, 0 with none.
    One probe per kind on `log_by_kind`."""
    head = 0
    for kind in _KINDS:
        row = conn.execute(
            "SELECT seq FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1", (kind,)
        ).fetchone()
        if row is not None and int(row[0]) > head:
            head = int(row[0])
    return head


def _mac_at(conn: sqlite3.Connection, seq: int) -> str:
    if seq <= 0:
        return ""
    row = conn.execute("SELECT mac FROM log WHERE seq = ?", (seq,)).fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _built(conn: sqlite3.Connection, state: dict[str, str] | None) -> int | None:
    """The sequence number the digest has seen, or None when it has to be
    rebuilt: no digest, another fingerprint, a build that did not finish,
    or a log whose row at that number is not the one the digest saw."""
    if state is None or state.get("fingerprint") != fingerprint():
        return None
    seen = state.get("seq")
    if seen is None or not seen.isdigit():
        return None
    if _mac_at(conn, int(seen)) != state.get("mac", ""):
        return None
    return int(seen)


# -- writing rows ------------------------------------------------------------


def _drop_entry(conn: sqlite3.Connection, doc: int, tokens: str) -> None:
    held = expanded(frozenset(tokens.split())) if tokens else set()
    conn.executemany(
        "DELETE FROM digest_tokens WHERE token = ? AND doc = ?",
        [(token, doc) for token in held],
    )
    conn.execute("DELETE FROM digest_records WHERE doc = ?", (doc,))


def _put_entry(
    conn: sqlite3.Connection,
    *,
    removed: bool,
    record_id: str,
    rid: int,
    body: str,
    scopes: Sequence[str],
    cwd: str,
) -> None:
    """Write one record's digest row and postings, replacing any it had."""
    found = _features(body)
    old = conn.execute(
        "SELECT doc, tokens FROM digest_records WHERE removed = ? AND id = ?",
        (int(removed), record_id),
    ).fetchone()
    if old is not None:
        _drop_entry(conn, int(old[0]), str(old[1]))
    cursor = conn.execute(
        "INSERT INTO digest_records(removed, id, rid, n, claim, stamped, tokens, scopes, cwd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(removed),
            record_id,
            rid,
            len(found.tokens),
            found.claim,
            int(found.stamped),
            " ".join(sorted(found.tokens)),
            json.dumps(list(scopes), ensure_ascii=False),
            cwd,
        ),
    )
    doc = cursor.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO digest_tokens(token, doc) VALUES (?, ?)",
        [(token, doc) for token in sorted(expanded(found.tokens))],
    )


def _cwd_of(record: Memory | TombstonedMemory) -> str:
    return (record.origin.cwd or "") if record.origin is not None else ""


def _index_record(conn: sqlite3.Connection, removed: bool, record_id: str) -> None:
    """Bring one id's digest row in `memories` (or `tombstones` under
    `removed`) to what that table holds now: written from the record as the
    store's own loader reads it, dropped when the table has no such row."""
    from .store_records import _MEMORY_SELECT, _TOMBSTONE_SELECT
    from .store_rows import _row_to_memory, _row_to_tombstone

    table = "tombstones" if removed else "memories"
    key = conn.execute(
        f"SELECT rowid FROM {table} WHERE id = ?", (record_id,)
    ).fetchone()
    if key is None:
        old = conn.execute(
            "SELECT doc, tokens FROM digest_records WHERE removed = ? AND id = ?",
            (int(removed), record_id),
        ).fetchone()
        if old is not None:
            _drop_entry(conn, int(old[0]), str(old[1]))
        return
    select = _TOMBSTONE_SELECT if removed else _MEMORY_SELECT
    row = conn.execute(select + " WHERE id = ?", (record_id,)).fetchone()
    record: Memory | TombstonedMemory = (
        _row_to_tombstone(row) if removed else _row_to_memory(row)
    )
    _put_entry(
        conn,
        removed=removed,
        record_id=record.id,
        rid=int(key[0]),
        body=record.body,
        scopes=record.scopes,
        cwd=_cwd_of(record),
    )


def _mutated_ids(conn: sqlite3.Connection, after: int) -> tuple[list[str], int]:
    """The ids the record mutations after sequence number `after` name, in
    first-seen order, and the last such row's number (`after` with none)."""
    marks = ", ".join("?" for _ in _KINDS)
    ids: dict[str, None] = {}
    last = after
    for row in conn.execute(
        f"SELECT seq, kind, payload FROM log WHERE kind IN ({marks}) AND seq > ? ORDER BY seq",
        (*_KINDS, after),
    ):
        payload = json.loads(row[2])
        kind = str(row[1])
        if kind == "memory_put":
            record_id = payload["memory"]["id"]
        elif kind == "tombstone_put":
            record_id = payload["tombstone"]["id"]
        else:
            record_id = payload["id"]
        ids[str(record_id)] = None
        last = int(row[0])
    return list(ids), last


def _ingest(conn: sqlite3.Connection, after: int) -> None:
    """Apply the record mutations after `after` and move the state to the
    last of them."""
    ids, last = _mutated_ids(conn, after)
    for record_id in ids:
        for removed in (False, True):
            _index_record(conn, removed, record_id)
    if last != after:
        _set_state(conn, seq=str(last), mac=_mac_at(conn, last))


# -- the build ---------------------------------------------------------------


def _build(self: Store) -> bool:
    """Empty the digest and index every record, one batch per transaction.
    The first transaction names this build in the state and reads the log's
    head; a batch that finds another build's name stops, and that build
    finishes. False when this build stopped."""
    token = secrets.token_hex(8)
    with self._transaction() as conn:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table}")
        start = _head(conn)
        _set_state(conn, fingerprint=fingerprint(), build=token)
    for removed in (False, True):
        table = "tombstones" if removed else "memories"
        after = 0
        while True:
            with self._transaction() as conn:
                if (_state(conn) or {}).get("build") != token:
                    return False
                keys = conn.execute(
                    f"SELECT rowid, id FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (after, _BUILD_BATCH),
                ).fetchall()
                for _rowid, record_id in keys:
                    _index_record(conn, removed, str(record_id))
            if len(keys) < _BUILD_BATCH:
                break
            after = int(keys[-1][0])
    with self._transaction() as conn:
        if (_state(conn) or {}).get("build") != token:
            return False
        conn.execute("DELETE FROM digest_state WHERE key = 'build'")
        _set_state(conn, seq=str(start), mac=_mac_at(conn, start))
    return True


# -- the store's methods -----------------------------------------------------


def _mark(conn: sqlite3.Connection) -> str | None:
    """What the digest has seen, as one string, when that is the log's last
    record mutation; None when the digest is behind or has to be rebuilt."""
    state = _state(conn)
    seen = _built(conn, state)
    if seen is None or state is None or seen != _head(conn):
        return None
    return f"{seen}:{state.get('mac', '')}"


def digest_mark(self: Store) -> str | None:
    """Bring the digest to the log's last record mutation and name the
    state it reached. None when it could not be (a store this open may not
    write, a busy lock, another process building it): the caller scans the
    records instead."""
    try:
        mark = _mark(self._conn)
        if mark is not None:
            return mark
        with self._transaction() as conn:
            seen = _built(conn, _state(conn))
            if seen is not None:
                _ingest(conn, seen)
                return _mark(conn)
        if not _build(self):
            return None
        with self._transaction() as conn:
            seen = _built(conn, _state(conn))
            if seen is None:
                return None
            _ingest(conn, seen)
            return _mark(conn)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "store %s: the digest could not be brought current (%s)", self._path, exc
        )
        return None


def digest_sync(self: Store) -> bool:
    """`digest_mark`, as whether the digest is current."""
    return self.digest_mark() is not None


#: Answers kept per store while the records stand as they were read.
_MEMO_ENTRIES = 16


def digest_memo(self: Store, key: Any, compute: Callable[[], Any]) -> Any:
    """`compute()`, or what it returned for `key` earlier while no record
    has changed since. A write's review is several calls carrying one
    body; each asks the same questions of the same records. An answer of
    None (the digest was not available) is never kept."""
    mark = self.digest_mark()
    if mark is None:
        return compute()
    held: dict[str, Any] = self.__dict__.setdefault("_digest_held", {})
    if held.get("mark") != mark:
        held.clear()
        held["mark"] = mark
        held["answers"] = OrderedDict()
    answers: OrderedDict[Any, Any] = held["answers"]
    if key in answers:
        answers.move_to_end(key)
        return answers[key]
    answer = compute()
    if answer is not None:
        answers[key] = answer
        while len(answers) > _MEMO_ENTRIES:
            answers.popitem(last=False)
    return answer


def _share(conn: sqlite3.Connection, mark: str, tokens: AbstractSet[str]) -> None:
    """Fill ``temp.digest_shared`` with, for every record holding a token
    of `expanded(tokens)`, how many it holds, beside its kind and sizes.
    The three questions a write asks (active, removed, claims) read this
    one count, so it is taken once: the table names the query it holds in
    ``temp.digest_asked``, in the same transaction as its rows, so a
    rollback or a new connection never leaves a name without them."""
    query = sorted(expanded(tokens))
    asked = hashlib.sha256(
        (mark + "\n" + "\n".join(query)).encode("utf-8", "surrogatepass")
    ).hexdigest()
    try:
        row = conn.execute("SELECT asked FROM temp.digest_asked").fetchone()
        if row is not None and row[0] == asked:
            return
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS digest_query(token TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS digest_shared("
            "doc INTEGER PRIMARY KEY, shared INTEGER, removed INTEGER, n INTEGER, claim INTEGER)"
        )
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS digest_asked(asked TEXT)")
    conn.execute("DELETE FROM temp.digest_asked")
    conn.execute("DELETE FROM temp.digest_query")
    conn.execute("DELETE FROM temp.digest_shared")
    conn.executemany(
        "INSERT INTO temp.digest_query(token) VALUES (?)", [(token,) for token in query]
    )
    conn.execute(
        "INSERT INTO temp.digest_shared(doc, shared, removed, n, claim) "
        "SELECT s.doc, s.shared, d.removed, d.n, d.claim FROM ("
        "  SELECT p.doc AS doc, COUNT(*) AS shared "
        "  FROM temp.digest_query q JOIN digest_tokens p ON p.token = q.token "
        "  GROUP BY p.doc) s JOIN digest_records d ON d.doc = s.doc"
    )
    conn.execute("INSERT INTO temp.digest_asked(asked) VALUES (?)", (asked,))


def digest_candidates(
    self: Store,
    tokens: AbstractSet[str],
    *,
    removed: bool,
    floor: float,
    claims: bool = False,
) -> list[DigestDoc] | None:
    """Every record `_pairwise_content_jaccard` can score at `floor` or
    above against a body whose raw content tokens are `tokens`, and
    possibly more; None when the digest is not available.

    The bound. Let `shared` be the tokens of `expanded(tokens)` the record's
    postings hold, `n` the count of `tokens` and `m` the record's (its
    `claim` under `claims`, which also keeps to claim-sized records and
    compares their provenance-stripped bodies). The scorer's two sets are
    subsets of the two expanded sets and supersets of the two raw sets, so
    its intersection is at most `shared` and its union at least
    ``max(n, m)``. A Jaccard of `floor` therefore needs
    ``shared >= floor * max(n, m)``. Its containment score counts only when
    the smaller set has `_CONTAINMENT_MIN_TOKENS` tokens and the score is
    `MEDIUM_SIMILARITY` or more, which needs ``shared`` to reach both
    ``MEDIUM_SIMILARITY * _CONTAINMENT_MIN_TOKENS`` and
    ``MEDIUM_SIMILARITY * min(n, m)``. A record that meets neither cannot
    score, and is left out.
    """
    if not tokens:
        return None
    mark = self.digest_mark()
    if mark is None:
        return None
    conn = self._conn
    size = "s.claim" if claims else "s.n"
    n = len(tokens)
    # Whole numbers only: thousandths of the floor rounded down, which can
    # only admit more.
    floor_milli = int(floor * 1000)
    medium_milli = int(MEDIUM_SIMILARITY * 1000)
    contained = -(-medium_milli * _CONTAINMENT_MIN_TOKENS // 1000)
    try:
        _share(conn, mark, tokens)
        rows = conn.execute(
            "SELECT d.id, d.n, d.claim, d.stamped, d.tokens "
            "FROM temp.digest_shared s JOIN digest_records d ON d.doc = s.doc "
            f"WHERE s.removed = ? AND {size} > 0 AND ("
            f"  s.shared * 1000 >= ? * MAX(?, {size}) "
            f"  OR (s.shared >= ? AND s.shared * 1000 >= ? * MIN(?, {size})))",
            (int(removed), floor_milli, n, contained, medium_milli, n),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning("store %s: the digest could not be read (%s)", self._path, exc)
        return None
    return [
        DigestDoc(
            id=str(row[0]),
            n=int(row[1]),
            claim=int(row[2]),
            stamped=bool(row[3]),
            tokens=frozenset(str(row[4]).split()),
        )
        for row in rows
    ]


def digest_scope_groups(self: Store) -> list[tuple[list[str], str, int]] | None:
    """The active records' (scopes, working directory, count) groups, in
    the order `load_all` first meets each; None when the digest is not
    available. What `scope_match` reads in place of every record."""
    if self.digest_mark() is None:
        return None
    try:
        rows = self._conn.execute(
            "SELECT scopes, cwd, COUNT(*), MIN(rid) FROM digest_records "
            "WHERE removed = 0 GROUP BY scopes, cwd ORDER BY MIN(rid)"
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning("store %s: the digest could not be read (%s)", self._path, exc)
        return None
    return [(list(json.loads(row[0])), str(row[1]), int(row[2])) for row in rows]


class LightRecord(NamedTuple):
    """The fields of a record a `SimilarHit` is built from, read without
    the rest of the row."""

    id: str
    scopes: list[str]
    confidence: str
    body: str
    created: datetime
    updated: datetime
    removed: datetime | None
    removed_reason: str | None


def light_records(
    self: Store, ids: Iterable[str], *, removed: bool
) -> list[LightRecord]:
    """`LightRecord`s of the records among `ids`, in the order the full
    loaders give them: row order for active records (`load_all`), most
    recently removed first for tombstones (`load_tombstones`)."""
    wanted = list(dict.fromkeys(ids))
    table = "tombstones" if removed else "memories"
    extra = ", removed, removed_reason" if removed else ""
    found: list[sqlite3.Row] = []
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        found.extend(
            self._conn.execute(
                f"SELECT rowid, id, scopes_json, confidence, body, created, updated{extra} "
                f"FROM {table} WHERE id IN ({marks})",
                chunk,
            )
        )
    if removed:
        found.sort(
            key=lambda row: (str(row["removed"]), int(row["rowid"])), reverse=True
        )
    else:
        found.sort(key=lambda row: int(row["rowid"]))
    return [
        LightRecord(
            id=str(row["id"]),
            scopes=_load_json(row["scopes_json"], []),
            confidence=str(row["confidence"]),
            body=str(row["body"]),
            created=_dt(row["created"]),
            updated=_dt(row["updated"]),
            removed=_dt(row["removed"]) if removed else None,
            removed_reason=row["removed_reason"] if removed else None,
        )
        for row in found
    ]


def bodies_of(self: Store, ids: Iterable[str]) -> dict[str, str]:
    """The bodies of the active records among `ids`, by id."""
    wanted = list(dict.fromkeys(ids))
    out: dict[str, str] = {}
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for row in self._conn.execute(
            f"SELECT id, body FROM memories WHERE id IN ({marks})", chunk
        ):
            out[str(row[0])] = str(row[1])
    return out
