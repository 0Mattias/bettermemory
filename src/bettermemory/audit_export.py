"""The signed log as an audit trail: `bettermemory log export`.

The log is the store's own receipt of every change and every telemetry
event (``bettermemory.log``), but it could be read only as a verdict
(`log verify`), as the membership rows of one record (`project why`) or
through raw sqlite3. None of those is something a SIEM ingests. This
module turns the log into JSON Lines, one object per row, in seq order,
so an auditor can ship who changed what and when to the tools a team
already watches, incrementally and without shipping the memories
themselves.

The line schema (``bettermemory.audit/1``), one JSON object per row:

* ``schema``: ``"bettermemory.audit/1"``;
* ``store_id``: the store the row belongs to, so one index can hold the
  exports of many stores;
* ``seq``, ``ts`` (the row's stamp, UTC ISO 8601 with a ``Z``),
  ``session`` (the row's session column, or null) and ``kind``;
* ``class``: ``mutation`` (a row the fold applies to a table, membership
  tables included), ``control`` (`store_created`, `rekey`, `migrate_v8`
  and the capture rows), ``membership`` (the membership observability
  rows) or ``telemetry`` (everything else);
* ``actor``: the actor block the row itself carries (a telemetry row's
  ``actor``), or null;
* ``record_actor``: the actor a put row's record names (``memory.actor``
  on a `memory_put`, ``tombstone.actor`` on a `tombstone_put`), or null.
  It says who wrote the record, which on a put written by a verify or a
  corroboration is not who caused this row; that row's telemetry twin
  carries the caller as ``actor``;
* ``record_ids``: the memory, tombstone and episode ids the row names
  (`record_ids` says how each kind is read), possibly empty;
* ``payload``: the row's payload, its free text redacted unless
  ``--include-bodies`` (below);
* ``mac`` and ``prev_mac``: the row's MAC and the MAC it is chained to,
  hex, exactly as the log holds them. A consumer that keeps the last
  MAC it saw can check the next export's first ``prev_mac`` against it;
* ``key_fingerprint``: the fingerprint of the key the row's segment is
  signed with, read off the rekey rows the way `log.verify_chain` reads
  them; null in a segment whose rekey row names no key;
* ``verified``: the row's MAC recomputed under that key and compared,
  true or false; null when this host does not hold the key.

``verified`` is a per-row check and nothing more: it does not see a
deleted tail, a row missing from the middle (the ``prev_mac`` link
does), or a table that differs from the log. `log verify` is still the
verdict; the export is the trail.

Redaction. By default every string under a free-text field name
(`REDACTED_FIELDS`: bodies, takeaways, removal reasons, notes, the
user's quoted words, conflict summaries and claims, and the pieces of
bodies and replies the telemetry rows carry: a recall's or a miss's hit
snippets, a use's claim excerpts, a refused write's claim phrases), at
any depth of the payload, becomes
``{"sha256": <hex of its UTF-8>, "len": <characters>}``.
A list of strings under such a name becomes a list of those. The hash
lets a consumer see that two rows carry the same text, and a body it
already holds be matched, without the text leaving the host. Ids,
scopes, timestamps, provenance, paths, repository and branch names stay
as they are: they are what an audit is about. Queries are redacted in
the log itself (`events.redact_query`) and that stand-in passes through
as stored; a query that is still text (a row written some other way) is
redacted here like a body. A payload that is not a JSON object is
redacted whole. With
``include_bodies`` every payload is exported as the log holds it, every
version of every body included, which makes the output as sensitive as
the store file.

Streaming. The rows are read through one cursor inside one read
transaction, so the export is a consistent snapshot even while the
daemon writes, and is never held in memory: each line is written as its
row is read. The segment map is the only thing read up front, from the
rekey rows alone (an indexed lookup). The output is deterministic: the
same log gives the same bytes, with nothing time-dependent in a line, so
two exports of one range can be compared with ``cmp``.

Incremental. ``since_seq`` exports the rows after it and ``until_seq``
the rows up to and including it, so a cron job passes the last seq it
exported (`ExportSummary.through_seq`) and gets only what is new.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from .log import CONTROL_KINDS, REKEY, KeyRing, compute_mac
from .store import MEMBERSHIP_EVENT_KINDS, MUTATION_KINDS, Store
from .time_utils import isoformat_utc, parse_event_ts

AUDIT_SCHEMA = "bettermemory.audit/1"

# The largest seq a range or an anchor can name: SQLite's largest
# INTEGER. A larger Python int cannot be bound to a statement at all
# (OverflowError), so it is refused as an argument instead.
MAX_SEQ = 2**63 - 1

# The payload fields that hold free text: what a memory, a tombstone or
# an episode says (`body`, `takeaway`, `claims`, the links' `note`), why
# a record was removed (`removed_reason`, the `remove` and `miss_ack`
# rows' `reason`), what a verifier or a conflict verdict noted (`note`),
# the first lines of two conflicting bodies (`summary_a`, `summary_b`)
# and the user's words a change rests on (`said`; a boolean `said`, as
# the capture rows carry it, is not text and passes through). The
# telemetry rows carry pieces of the same text: the body window each
# hit of a `prompt_recall`, `recall_held` or `search_miss` row shows
# (`top_hits[].snippet`), the reply sentence a `use` row says a memory
# shaped (`claim_excerpts`) and the body phrases a refused `write` or
# `update` quotes (`claim_phrases`). `query` and `probe_query` are
# listed for a query that is still text: the store redacts both before
# a row is signed, and that stand-in (a dict of hash, preview and
# length) holds no listed name, so it passes through as stored. Matched
# by name at any depth, so a field nested in a review's answers is
# caught where it sits. A `reason` that is a code (`cap`) is hashed
# too: a name-based rule cannot tell a code from a sentence, and a
# hash of a code still groups. A free-text field a later row kind
# adds must be named here, or it is exported as it stands.
REDACTED_FIELDS = frozenset(
    {
        "body",
        "claim_excerpts",
        "claim_phrases",
        "claims",
        "note",
        "probe_query",
        "query",
        "reason",
        "removed_reason",
        "said",
        "snippet",
        "summary_a",
        "summary_b",
        "takeaway",
    }
)

CLASS_MUTATION = "mutation"
CLASS_CONTROL = "control"
CLASS_MEMBERSHIP = "membership"
CLASS_TELEMETRY = "telemetry"

# Where a put row keeps the record it puts, by kind: the record's ids
# and its actor are read from there.
_PUT_RECORDS = {
    "memory_put": "memory",
    "tombstone_put": "tombstone",
    "episode_put": "episode",
}
_RECORD_DELETES = frozenset({"memory_delete", "tombstone_delete", "episode_delete"})
_ACTOR_RECORDS = {"memory_put": "memory", "tombstone_put": "tombstone"}
# The membership rows keyed by the record they describe.
_RECORD_KEYED = {
    "home_put": "home",
    "standing_put": "standing",
}
_RECORD_KEYED_DELETES = frozenset({"home_delete", "standing_delete"})
# The fields a telemetry or control row names a record under.
_ID_FIELDS = ("id", "memory_id", "record_id")


def row_class(kind: str) -> str:
    """The class a row of ``kind`` belongs to (see the module docstring)."""
    if kind in MUTATION_KINDS:
        return CLASS_MUTATION
    if kind in CONTROL_KINDS:
        return CLASS_CONTROL
    if kind in MEMBERSHIP_EVENT_KINDS:
        return CLASS_MEMBERSHIP
    return CLASS_TELEMETRY


def redact_text(text: str) -> dict[str, Any]:
    """The stand-in a redacted string is exported as: the SHA-256 of its
    UTF-8 bytes and its length in characters, as `events.redact_query`
    counts a query's."""
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
    return {"sha256": digest, "len": len(text)}


def _redact_field(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_field(item) for item in value]
    if isinstance(value, dict):
        return redact_payload(value)
    return value


def redact_payload(value: Any) -> Any:
    """``value`` with every string under a `REDACTED_FIELDS` name, at any
    depth, replaced by `redact_text`; everything else as it is."""
    if isinstance(value, dict):
        return {
            key: _redact_field(item) if key in REDACTED_FIELDS else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def record_ids(kind: str, payload: Any) -> list[str]:
    """The memory, tombstone and episode ids a row names, in order and
    without repeats.

    A put row names the record it puts (``memory.id``, ``tombstone.id``,
    ``episode.id``) and a delete row the ``id`` it deletes; a
    verification row its ``memory_id``; a conflict row the two records
    it pairs (``a_id``, ``b_id``; the conflict's own id is not a record);
    an import row the ``memory_id`` it brought in; a ``homes`` or
    ``standing`` row the ``record_id`` it describes. Any other row names
    what its top-level ``id``, ``memory_id`` and ``record_id`` hold, when
    they are text: a `show`, an `update` or a `capture_member` row, for
    example. A list of results (a search's ``returned``) is not read: it
    is in the payload for a consumer that wants it.
    """
    if not isinstance(payload, dict):
        return []
    found: list[str | None] = []
    if kind in _PUT_RECORDS:
        record = payload.get(_PUT_RECORDS[kind])
        if isinstance(record, dict):
            found.append(_text(record.get("id")))
    elif kind in _RECORD_DELETES:
        found.append(_text(payload.get("id")))
    elif kind in ("verification_put", "verification_delete", "import_put"):
        found.append(_text(payload.get("memory_id")))
    elif kind == "conflict_put":
        record = payload.get("conflict")
        if isinstance(record, dict):
            found.extend((_text(record.get("a_id")), _text(record.get("b_id"))))
    elif kind in _RECORD_KEYED:
        record = payload.get(_RECORD_KEYED[kind])
        if isinstance(record, dict):
            found.append(_text(record.get("record_id")))
    elif kind in _RECORD_KEYED_DELETES:
        found.append(_text(payload.get("record_id")))
    elif kind not in MUTATION_KINDS:
        found.extend(_text(payload.get(name)) for name in _ID_FIELDS)
    out: list[str] = []
    for item in found:
        if item is not None and item not in out:
            out.append(item)
    return out


def row_actor(payload: Any) -> dict[str, Any] | None:
    """The actor block a row carries itself (a telemetry row's
    ``actor``), or None."""
    if isinstance(payload, dict):
        actor = payload.get("actor")
        if isinstance(actor, dict):
            return actor
    return None


def record_actor(kind: str, payload: Any) -> dict[str, Any] | None:
    """The actor the record of a `memory_put` or `tombstone_put` names,
    or None."""
    name = _ACTOR_RECORDS.get(kind)
    if name is None or not isinstance(payload, dict):
        return None
    record = payload.get(name)
    if not isinstance(record, dict):
        return None
    actor = record.get("actor")
    return actor if isinstance(actor, dict) else None


def utc_ts(value: str) -> str:
    """A row's stamp as UTC ISO 8601 with a ``Z``. The store writes that
    shape already, and such a stamp is kept byte for byte; an imported
    v8 event may carry an offset or none (read as UTC), and is rendered
    in UTC. A stamp that does not parse is kept as stored."""
    parsed = parse_event_ts(value)
    if parsed is None or value.endswith("Z"):
        return value
    return isoformat_utc(parsed.astimezone(timezone.utc))


def _rekey_names(payload: str) -> tuple[str, str] | None:
    """The old and new fingerprints a rekey row's payload names, read as
    `log.verify_chain` reads them; None for a payload that names none."""
    try:
        names = json.loads(payload)
        old = names["old_fingerprint"]
        new = names["new_fingerprint"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return old, new


def same_mac(expected: str, found: str) -> bool:
    """Whether a MAC column holds ``expected``, in constant time. Compared
    as UTF-8 bytes: `hmac.compare_digest` refuses a str with a non-ASCII
    character, and a hand-edited column can hold one, which must read as
    a mismatch, not stop the export or the anchor check at that row."""
    return hmac.compare_digest(
        expected.encode("utf-8", errors="surrogatepass"),
        found.encode("utf-8", errors="surrogatepass"),
    )


@dataclass(frozen=True)
class Segments:
    """Which key signed which rows: ``starts[i]`` is the seq of the
    rekey row that opens segment ``i``, ``fingerprints[i]`` the key it
    names (None when its payload names none), and ``first`` the key of
    every row before the first rekey row."""

    first: str
    starts: tuple[int, ...]
    fingerprints: tuple[str | None, ...]

    def fingerprint_for(self, seq: int) -> str | None:
        index = bisect.bisect_right(self.starts, seq) - 1
        return self.first if index < 0 else self.fingerprints[index]


def segment_map(conn: sqlite3.Connection, *, current_fingerprint: str) -> Segments:
    """The segments of the log on ``conn``, by the rules
    `log.verify_chain` splits the chain with: the first rekey row's old
    fingerprint names the key of the rows before it (the store's current
    key when no rekey row exists, or when the first one names nothing),
    and every rekey row opens a segment under the new fingerprint it
    names. Reads the rekey rows only, through the kind index."""
    first = current_fingerprint
    starts: list[int] = []
    fingerprints: list[str | None] = []
    for seq, payload in conn.execute(
        "SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq", (REKEY,)
    ):
        names = _rekey_names(str(payload))
        if not starts and names is not None:
            first = names[0]
        starts.append(int(seq))
        fingerprints.append(None if names is None else names[1])
    return Segments(first, tuple(starts), tuple(fingerprints))


class _RowVerifier:
    """One HMAC per row under its segment's key, each key looked up once."""

    def __init__(self, keyring: KeyRing, segments: Segments) -> None:
        self._keyring = keyring
        self._segments = segments
        self._keys: dict[str, bytes | None] = {}

    def check(
        self,
        *,
        seq: int,
        ts: str,
        session: str | None,
        kind: str,
        payload: str,
        prev_mac: str,
        mac: str,
    ) -> tuple[str | None, bool | None]:
        """The segment's key fingerprint and the verdict on the row's
        MAC: True or False, None when the key is not held here."""
        name = self._segments.fingerprint_for(seq)
        if name is None:
            return None, None
        if name not in self._keys:
            self._keys[name] = self._keyring.key_for_fingerprint(name)
        key = self._keys[name]
        if key is None:
            return name, None
        expected = compute_mac(
            key,
            seq=seq,
            ts=ts,
            session=session,
            kind=kind,
            payload=payload,
            prev_mac=prev_mac,
        )
        return name, same_mac(expected, mac)


def dumps_line(line: Mapping[str, Any]) -> str:
    """One export line's text, newline included: sorted keys, no
    whitespace, ASCII only (a terminal or pipe in any locale can carry
    it, and every JSON reader decodes the escapes)."""
    return json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n"


@contextlib.contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One read transaction around the block, so every read in it sees
    the same log however many rows the daemon appends meanwhile; rolled
    back, since nothing in it writes. Inside a caller's transaction the
    caller's snapshot serves."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")


@dataclass(frozen=True)
class ExportSummary:
    """What one export wrote. ``through_seq`` is the cursor for the next
    run (``--since-seq``): the last seq the export covered, whether or
    not a kind filter let that row through, and never behind the
    ``since_seq`` it was given. ``log_last_seq`` is the log's last row
    when the export read it (None for an empty log)."""

    store_id: str
    rows: int
    first_seq: int | None
    last_seq: int | None
    through_seq: int
    log_last_seq: int | None
    include_bodies: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "rows": self.rows,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "through_seq": self.through_seq,
            "log_last_seq": self.log_last_seq,
            "include_bodies": self.include_bodies,
        }


def _select(
    since_seq: int, until_seq: int | None, kinds: Collection[str] | None
) -> tuple[str, list[Any]]:
    sql = "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log WHERE seq > ?"
    params: list[Any] = [int(since_seq)]
    if until_seq is not None:
        sql += " AND seq <= ?"
        params.append(int(until_seq))
    if kinds is not None:
        names = sorted(set(kinds))
        sql += f" AND kind IN ({', '.join('?' for _ in names)})"
        params.extend(names)
    return sql + " ORDER BY seq", params


def _audit_lines(
    store: Store,
    *,
    since_seq: int,
    until_seq: int | None,
    kinds: Collection[str] | None,
    include_bodies: bool,
) -> Iterator[dict[str, Any]]:
    """The export's lines, one per row, read through one cursor. The
    caller holds the read snapshot."""
    conn = store.conn
    verifier = _RowVerifier(
        store.keyring,
        segment_map(conn, current_fingerprint=store.key_fingerprint),
    )
    store_id = store.store_id
    sql, params = _select(since_seq, until_seq, kinds)
    for seq, ts, session, kind, text, prev_mac, mac in conn.execute(sql, params):
        seq = int(seq)
        ts = str(ts)
        session = None if session is None else str(session)
        kind = str(kind)
        text = str(text)
        prev_mac = str(prev_mac)
        mac = str(mac)
        fingerprint, verified = verifier.check(
            seq=seq,
            ts=ts,
            session=session,
            kind=kind,
            payload=text,
            prev_mac=prev_mac,
            mac=mac,
        )
        # A payload the store wrote is canonical JSON a few levels deep.
        # Text that is not JSON, or JSON nested past what the parser or
        # the redaction walk recurses through, only a hand edit writes:
        # it is exported as its text (redacted whole by default), so one
        # edited row reads as one unverified line and never ends the
        # export there, which would blind every run after it as well.
        try:
            payload: Any = json.loads(text)
        except (ValueError, RecursionError):
            payload = None
            parsed = False
        else:
            parsed = True
        if include_bodies:
            exported: Any = payload if parsed else text
        elif isinstance(payload, dict):
            try:
                exported = redact_payload(payload)
            except RecursionError:
                exported = redact_text(text)
        else:
            exported = redact_text(text)
        yield {
            "schema": AUDIT_SCHEMA,
            "store_id": store_id,
            "seq": seq,
            "ts": utc_ts(ts),
            "session": session,
            "kind": kind,
            "class": row_class(kind),
            "actor": row_actor(payload),
            "record_actor": record_actor(kind, payload),
            "record_ids": record_ids(kind, payload),
            "payload": exported,
            "mac": mac,
            "prev_mac": prev_mac,
            "key_fingerprint": fingerprint,
            "verified": verified,
        }


def export(
    store: Store,
    write: Callable[[str], object],
    *,
    since_seq: int = 0,
    until_seq: int | None = None,
    kinds: Collection[str] | None = None,
    include_bodies: bool = False,
) -> ExportSummary:
    """Write the export's lines to ``write``, one call per line, in seq
    order: the rows after ``since_seq`` up to and including
    ``until_seq`` (the log's end when None), of ``kinds`` when given.
    Read-only: one read transaction, rolled back."""
    if since_seq < 0:
        raise ValueError("since_seq must be 0 or more")
    if until_seq is not None and until_seq < since_seq:
        raise ValueError("until_seq must be at least since_seq")
    if max(since_seq, until_seq or 0) > MAX_SEQ:
        raise ValueError(f"a seq is at most {MAX_SEQ}")
    rows = 0
    first_seq: int | None = None
    last_seq: int | None = None
    with read_snapshot(store.conn):
        raw = store.conn.execute("SELECT MAX(seq) FROM log").fetchone()[0]
        log_last_seq = None if raw is None else int(raw)
        for line in _audit_lines(
            store,
            since_seq=since_seq,
            until_seq=until_seq,
            kinds=kinds,
            include_bodies=include_bodies,
        ):
            write(dumps_line(line))
            rows += 1
            if first_seq is None:
                first_seq = int(line["seq"])
            last_seq = int(line["seq"])
    through = since_seq
    if log_last_seq is not None:
        end = log_last_seq if until_seq is None else min(until_seq, log_last_seq)
        through = max(since_seq, end)
    return ExportSummary(
        store_id=store.store_id,
        rows=rows,
        first_seq=first_seq,
        last_seq=last_seq,
        through_seq=through,
        log_last_seq=log_last_seq,
        include_bodies=include_bodies,
    )


def export_lines(
    store: Store,
    *,
    since_seq: int = 0,
    until_seq: int | None = None,
    kinds: Collection[str] | None = None,
    include_bodies: bool = False,
) -> list[dict[str, Any]]:
    """The export as parsed lines, for a caller that wants them in hand
    (a test, a small range); the CLI streams through `export`."""
    out: list[dict[str, Any]] = []
    export(
        store,
        lambda text: out.append(json.loads(text)),
        since_seq=since_seq,
        until_seq=until_seq,
        kinds=kinds,
        include_bodies=include_bodies,
    )
    return out


__all__ = [
    "AUDIT_SCHEMA",
    "CLASS_CONTROL",
    "CLASS_MEMBERSHIP",
    "CLASS_MUTATION",
    "CLASS_TELEMETRY",
    "MAX_SEQ",
    "REDACTED_FIELDS",
    "ExportSummary",
    "Segments",
    "dumps_line",
    "export",
    "export_lines",
    "read_snapshot",
    "record_actor",
    "record_ids",
    "redact_payload",
    "redact_text",
    "row_actor",
    "row_class",
    "same_mac",
    "segment_map",
    "utc_ts",
]
