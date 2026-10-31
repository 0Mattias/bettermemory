"""One record's history, read off the store's signed log.

`bettermemory memory history ID` answers "who changed this record, when,
and from what" without SQL. The answer is already on the log: every
mutation row carries the whole record after the change (``memory_put``,
``tombstone_put``, ``episode_put``), every delete row names the id, and
the telemetry rows of the tool calls behind them (``write``, ``update``,
``verify``, ``remove``, ``restore``, ``standing_guard``) name the id and
the actor of the request. This module finds those rows, says what each
one did to the record, and checks each row's MAC.

Four roles a row can play:

* ``record``: a row that changed the record or what sits beside it (its
  home, its standing answer, this host's verification stamp, the capture
  batch that wrote it, the v8 file it was imported from). The put rows
  are versions: each says which fields changed since the version before
  it, and ``--diff`` adds a unified diff of the body.
* ``event``: a telemetry row of a tool call that named the record. It
  carries the actor of the request, which a put row cannot: a put
  carries the record, and the record's ``actor`` is its author, kept
  across every later edit. The text output keeps the two apart
  ("actor" and "author"), and so does ``actor_source`` in the JSON.
* ``reference``: a row of another record that names this one: a link
  appearing on another record (``superseded_by`` for a ``supersedes``
  link, ``linked_from`` otherwise, ``unlinked`` when it goes) and a
  conflict filed against it. A link is reported when it appears and
  when it goes, not on every later put of the record that carries it.
* ``store``: a row that says the store's history was replaced
  (`log.HISTORY_REPLACED`) after the record's first row: a
  ``store_restored`` row, action ``rewound``, where `backup restore`
  put the whole store back to a bundle's snapshot, so what the record
  became in the log the restore replaced, past the bundle's head, is
  not on this log; or a ``history_accepted`` row, action ``accepted``,
  where `log accept-history` took the file's history as it stood. Its
  detail names the bundle's creation (a restore), the seq the log was
  put back to or accepted at and, when known, the seq the replaced log
  or head checkpoint reached.

Reads, such as a search that returned the record or a `memory_show` of
it, are not history and are left out, as is any row whose payload names
the id only in passing.

Each row's ``verified`` flag is its MAC checked under the key of the
chain segment it sits in (the segments are read off the ``rekey`` rows
the way ``log.verify_chain`` reads them): true when the MAC checks,
false when it does not, null when this host does not hold that key. It
is the same flag `log export` writes per row. A false flag, a live
record whose pointer into the log fails or a tombstone the log never put
(``provenance`` ``unaccounted``), and a record the log says should exist
but whose table lacks it (``missing``) are the findings: the command
exits 1 on any of them.

Cost: one scan of the log's payload column for the id (the id is a
ULID, so a substring test finds every row that names it and the payload
check below drops any that only contain it by accident), one more scan
per other record that ever linked to it (a version that drops the link
no longer names the id, so only that record's own versions and deletes
show the link going), one query for the rekey rows, and one HMAC per row
reported. A history is read
interactively, so the scan is affordable; nothing here runs on a hook
path. The store is opened read-only by the caller
(``Store.open(..., read_only=True)``, which writes nothing to the file
or beside it) and nothing here writes.

Every query runs inside one read transaction (`read_snapshot`). The
daemon keeps writing while a history is read, and the scans and the
table lookups are separate statements: without the snapshot, an episode
pruned between the log scan and the table lookup reads as ``missing``,
a tamper finding about a store nobody tampered with.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .log import (
    CAPTURE_MEMBER,
    HISTORY_REPLACED,
    REKEY,
    LogRow,
    compute_mac,
    same_mac,
)
from .store import IMPORTED, LOCAL, UNACCOUNTED, Store
from .store_returned import RELINK_MARK

FORMAT = "bettermemory-history"
FORMAT_VERSION = 1

# The three roles of a row (module docstring).
RECORD = "record"
EVENT = "event"
REFERENCE = "reference"
STORE = "store"

# Where a row's actor came from: the request that made it (a telemetry
# row), or the record's own author (a put row's record).
ACTOR_FROM_EVENT = "event"
ACTOR_FROM_RECORD = "record"

# The telemetry kinds of the tool calls that change a record, each naming
# it under ``id``, and the standing guard's row, naming every record it
# guarded under ``standing``.
_CHANGE_EVENTS = frozenset({"write", "update", "verify", "remove", "restore"})
_STANDING_GUARD = "standing_guard"

# The put rows whose payload carries the whole record under the kind's
# own prefix (``memory_put`` -> ``payload["memory"]``), and the rows that
# take a record out of its table.
_VERSION_KINDS = frozenset({"memory_put", "tombstone_put", "episode_put"})
_RECORD_DELETES = frozenset({"memory_delete", "tombstone_delete", "episode_delete"})
# The versions that carry links: a memory's own, a tombstone's beside it.
_LINKING_KINDS = frozenset({"memory_put", "tombstone_put"})
# The deletes of a record that links, each with the put that carries its
# links on when it comes right after: a removal's tombstone, a restore's
# memory. A delete without it (a pruned tombstone) takes the links away.
_LINK_CARRIED_BY = {"memory_delete": "tombstone_put", "tombstone_delete": "memory_put"}

# Fields a version compares on to say what changed. Identity and the
# timestamps every put moves are left out, as are the removal fields,
# which only a tombstone has.
_UNCOMPARED = frozenset(
    {"id", "created", "updated", "removed", "removed_reason", "removed_session"}
)

# Fields whose change alone is a corroboration: the dedup gate's
# recurrence bump (`Store.record_corroboration`), not an edit.
_CORROBORATION_FIELDS = frozenset({"corroborations", "last_corroborated"})

# The event fields shown as the event's detail, when set. Free text the
# request carried (a removal reason) is the caller's own; the bodies and
# quotes some events carry are not shown.
_EVENT_DETAIL = ("status", "reason", "fields", "door", "attribution", "triggered_from")


def printable(text: str) -> str:
    """`text` made safe to print on one terminal line: control and format
    characters (category Cc and Cf: escapes, line breaks, bidi controls,
    zero-width characters) are written as their Python escape (``\\x1b``,
    ``\\n``, ``\\u202e``), as are the line and paragraph separators (Zl
    and Zp, U+2028 and U+2029, which `str.splitlines` and the agent's
    reader both break a line at) and a lone surrogate (Cs, which a
    ``\\ud800`` escape in stored JSON decodes to and which a strict UTF-8
    stdout refuses to write); a tab is kept. The text comes from the
    store, which an editor of the file may have written; a raw escape
    sequence in a reason or a scope must not reach the terminal as one.
    The membership lines a session's first output carries quote paths and
    names through it too (ENG-154), so no directory name forges a line.
    The stand-in is ASCII, so it encodes in every code page: U+FFFD did
    not in cp1252, the one Windows gives a redirected stdout, and a path
    holding a soft hyphen (Cf, which cp1252 does encode) ended a report in
    UnicodeEncodeError."""
    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if ch != "\t" and unicodedata.category(ch) in _UNPRINTABLE
        else ch
        for ch in text
    )


#: The categories `printable` writes as escapes.
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


@contextlib.contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One read transaction around a report's queries, so every one of
    them sees the store as of the same commit while the daemon writes
    beside it (WAL: a reader holds its snapshot and never blocks a
    writer). Rolled back at the end: nothing was written. A caller that
    already holds a transaction (a ``Store.batch``) keeps its own."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")


@dataclass(frozen=True)
class HistoryRow:
    """One log row of a record's history, as `memory history` reports it."""

    seq: int
    ts: str
    session: str | None
    kind: str
    role: str
    action: str
    verified: bool | None
    actor: dict[str, Any] | None = None
    actor_source: str | None = None
    changed: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    diff: str | None = None

    def to_dict(self, *, diff: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "session": self.session,
            "kind": self.kind,
            "role": self.role,
            "action": self.action,
            "verified": self.verified,
            "actor": self.actor,
            "actor_source": self.actor_source,
            "changed": list(self.changed),
            "detail": dict(self.detail),
        }
        if diff:
            out["diff"] = self.diff
        return out


@dataclass(frozen=True)
class RecordHistory:
    """A record's rows in log order and what the tables hold of it now.

    ``record_kind`` is ``memory`` or ``episode`` (None when nothing says).
    ``state`` is where the record is now: ``active`` or ``tombstoned``
    from its table, ``pruned`` when the log's last word on it deletes its
    tombstone or episode, ``deleted`` when it deletes the memory without
    tombstoning it, ``missing`` when the log's last word puts it but no table
    holds it (deleted outside the store), ``unknown`` when neither the log
    nor any table names it. ``provenance`` is the verified provenance
    label of an active record; without a key to check it under it is the
    stored label, unverified, and ``key_present`` says so
    (`Store.can_verify`: the store's key, or this machine's own when meta
    names another and this machine has a history of the store). A
    tombstone's is None, or ``unaccounted`` when the log's last word on
    the record is not the ``tombstone_put`` that wrote it; None for every
    other state."""

    record_id: str
    record_kind: str | None
    state: str
    provenance: str | None
    key_present: bool
    rows: tuple[HistoryRow, ...]

    @property
    def known(self) -> bool:
        return self.state != "unknown"

    @property
    def findings(self) -> list[str]:
        """What makes the history read as tampered, one line each."""
        out = [
            f"seq {row.seq} ({row.kind}) fails its MAC"
            for row in self.rows
            if row.verified is False
        ]
        if self.provenance == UNACCOUNTED:
            out.append(
                f"the {self.state} record reads unaccounted: no log row this "
                "store signed put it as it stands"
            )
        if self.state == "missing":
            out.append(
                "the log puts this record but no table holds it: it was "
                "deleted outside the store"
            )
        return out

    @property
    def exit_code(self) -> int:
        """0 when the history reads clean, 1 on any finding."""
        return 1 if self.findings else 0

    def to_dict(self, *, diff: bool = False) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "id": self.record_id,
            "record_kind": self.record_kind,
            "state": self.state,
            "provenance": self.provenance,
            "key_present": self.key_present,
            "findings": self.findings,
            "rows": [row.to_dict(diff=diff) for row in self.rows],
        }


class _RowVerifier:
    """Checks a row's MAC under the key of the chain segment it sits in.

    The segments are read off the ``rekey`` rows once, as
    ``log.verify_chain`` and ``Store._segment_key`` read them: the first
    segment's key is the one the earliest rekey row says it replaced (the
    store's own fingerprint when there is none), and each rekey row starts
    a segment under the key it names. A key this host does not hold,
    current or retired, makes the row unverifiable (None), never a
    guess."""

    def __init__(self, store: Store) -> None:
        self._keyring = store.keyring
        first = store.key_fingerprint
        starts: list[tuple[int, str]] = []
        for raw in store.conn.execute(
            "SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq", (REKEY,)
        ):
            try:
                names = json.loads(str(raw[1]))
                old = str(names["old_fingerprint"])
                new = str(names["new_fingerprint"])
            except (ValueError, KeyError, TypeError):
                continue
            if not starts:
                first = old
            starts.append((int(raw[0]), new))
        self._segments = [(1, first), *starts]
        self._keys: dict[str, bytes | None] = {}

    def verified(self, row: LogRow) -> bool | None:
        name = self._segments[0][1]
        for first_seq, key_fingerprint in self._segments:
            if row.seq >= first_seq:
                name = key_fingerprint
        if name not in self._keys:
            self._keys[name] = self._keyring.key_for_fingerprint(name)
        key = self._keys[name]
        if key is None:
            return None
        expected = compute_mac(
            key,
            seq=row.seq,
            ts=row.ts,
            session=row.session,
            kind=row.kind,
            payload=row.payload,
            prev_mac=row.prev_mac,
        )
        return same_mac(expected, row.mac)


def _rows_naming(
    store: Store, record_id: str, *, kinds: tuple[str, ...] = ()
) -> list[LogRow]:
    """Every log row whose payload text contains `record_id`, in order;
    only rows of `kinds` when any are named."""
    where = "instr(payload, ?) > 0"
    params: tuple[str, ...] = (record_id,)
    if kinds:
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    return [
        LogRow(
            int(raw[0]),
            str(raw[1]),
            None if raw[2] is None else str(raw[2]),
            str(raw[3]),
            str(raw[4]),
            str(raw[5]),
            str(raw[6]),
        )
        for raw in store.conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE {where} ORDER BY seq",
            params,
        )
    ]


def _with_linking_versions(
    store: Store, record_id: str, rows: list[LogRow]
) -> tuple[list[tuple[LogRow, dict[str, Any] | None]], set[str]]:
    """`rows` parsed, with every version and delete of each other record
    that ever linked to `record_id` merged in, in log order; and those
    records.

    A version that drops a link no longer names the record, and neither
    does the delete that prunes the linking record's tombstone, so the
    scan for the record's id cannot see the link go. One more scan per
    linking record (there are seldom more than one or two) reads that
    record's versions and deletes, so a link is reported when it appears
    and when it goes."""
    parsed = {row.seq: (row, _payload(row)) for row in rows}
    sources: set[str] = set()
    for row, payload in parsed.values():
        if payload is None or row.kind not in _LINKING_KINDS:
            continue
        subject = _subject(row.kind, payload)
        if subject is not None and subject != record_id:
            if _link_types(row.kind, payload, record_id):
                sources.add(subject)
    kinds = tuple(sorted(_LINKING_KINDS | set(_LINK_CARRIED_BY)))
    for source in sorted(sources):
        for row in _rows_naming(store, source, kinds=kinds):
            if row.seq not in parsed:
                parsed[row.seq] = (row, _payload(row))
    return [parsed[seq] for seq in sorted(parsed)], sources


def _with_restores(
    store: Store, found: list[tuple[LogRow, dict[str, Any] | None]]
) -> list[tuple[LogRow, dict[str, Any] | None]]:
    """`found` with every row that says the store's history was replaced
    (`log.HISTORY_REPLACED`) after its first row merged in, in log order.
    One that came before the record's first row replaced a history the
    record was not yet in, and is not its history."""
    if not found:
        return found
    rows = {row.seq: (row, payload) for row, payload in found}
    for row in _replaced_rows(store, found[0][0].seq):
        rows.setdefault(row.seq, (row, _payload(row)))
    return [rows[seq] for seq in sorted(rows)]


def _replaced_rows(store: Store, after: int) -> Iterator[LogRow]:
    """The rows of the `log.HISTORY_REPLACED` kinds past seq `after`, in
    log order (the ``log_by_kind`` index finds them)."""
    kinds = sorted(HISTORY_REPLACED)
    marks = ", ".join("?" for _ in kinds)
    for raw in store.conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({marks}) AND seq > ? ORDER BY seq",
        (*kinds, after),
    ):
        yield LogRow(
            int(raw[0]),
            str(raw[1]),
            None if raw[2] is None else str(raw[2]),
            str(raw[3]),
            str(raw[4]),
            str(raw[5]),
            str(raw[6]),
        )


def history_replaced(store: Store) -> list[dict[str, Any]]:
    """Every row of `store`'s log that says its history was replaced
    (`log.HISTORY_REPLACED`), in log order, as ``{seq, ts, kind, action,
    ...detail}`` (`replaced_detail`): what `log verify` lists. A row
    whose payload does not parse is listed with no detail."""
    return [
        {
            "seq": row.seq,
            "ts": row.ts,
            "kind": row.kind,
            "action": HISTORY_REPLACED[row.kind][1],
            **replaced_detail(row.kind, _payload(row) or {}),
        }
        for row in _replaced_rows(store, 0)
    ]


def replaced_detail(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The detail of a row of `kind` that says the history was replaced
    (`log.HISTORY_REPLACED`): when the bundle was made (a restore), the
    seq the log was put back to or accepted at (``head_after``; a
    ``history_accepted`` row written without one, as `log
    accept-history` writes it, was accepted at its ``log_end_before``),
    and the seq the replaced log or head checkpoint reached (the later of
    ``head_before`` and ``log_end_before``, as
    `audit_export.rewinds_across` reads them), when known."""
    after = _mapping(payload.get("head_after"))
    if after is None and kind != "store_restored":
        after = _mapping(payload.get("log_end_before"))
    after = after or {}
    ends = [
        end.get("seq")
        for end in (
            _mapping(payload.get("head_before")),
            _mapping(payload.get("log_end_before")),
        )
        if end is not None
    ]
    known = [seq for seq in ends if type(seq) is int]
    return _set(
        {},
        bundle_created_at=payload.get("bundle_created_at"),
        to_seq=after.get("seq"),
        from_seq=max(known) if known else None,
    )


def _payload(row: LogRow) -> dict[str, Any] | None:
    try:
        payload = json.loads(row.payload)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _subject(kind: str, payload: Mapping[str, Any]) -> str | None:
    """The record a row of `kind` changes, or None for a kind that changes
    no record (or a payload that does not say)."""
    named: Any = None
    if kind in _VERSION_KINDS:
        record = _mapping(payload.get(kind.split("_", 1)[0]))
        named = None if record is None else record.get("id")
    elif kind in _RECORD_DELETES:
        named = payload.get("id")
    elif kind in ("verification_put", "verification_delete"):
        named = payload.get("memory_id")
    elif kind in ("home_put", "standing_put", "returned_put"):
        row = _mapping(payload.get(kind[: -len("_put")]))
        named = None if row is None else row.get("record_id")
    elif kind in ("home_delete", "standing_delete", "returned_delete"):
        named = payload.get("record_id")
    elif kind in (CAPTURE_MEMBER, "import_put"):
        named = payload.get("memory_id")
    return named if isinstance(named, str) else None


def _names_as_event(kind: str, payload: Mapping[str, Any], record_id: str) -> bool:
    if kind in _CHANGE_EVENTS:
        return payload.get("id") == record_id
    if kind == _STANDING_GUARD:
        standing = payload.get("standing")
        return isinstance(standing, list) and record_id in standing
    return False


def _link_types(kind: str, payload: Mapping[str, Any], record_id: str) -> set[str]:
    """The types of the links to `record_id` a put row of another record
    carries: a memory's own ``links``, a tombstone's beside it."""
    if kind == "memory_put":
        record = _mapping(payload.get("memory"))
        links = None if record is None else record.get("links")
    else:
        links = payload.get("links")
    if not isinstance(links, list):
        return set()
    return {
        str(link.get("type"))
        for link in links
        if isinstance(link, Mapping) and link.get("target_id") == record_id
    }


def _changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    """The compared fields whose value differs between two versions. Only
    fields both carry are compared, so a memory against the tombstone it
    came back from does not read every memory-only field as changed."""
    return tuple(
        sorted(
            name
            for name in set(before) & set(after)
            if name not in _UNCOMPARED and before[name] != after[name]
        )
    )


def _body_diff(before: str | None, after: str, *, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(),
        after.splitlines(),
        fromfile=old,
        tofile=new,
        lineterm="",
    )
    return "\n".join(lines)


def _set(detail: dict[str, Any], **values: Any) -> dict[str, Any]:
    """`detail` with each of `values` that is set (not None)."""
    for name, value in values.items():
        if value is not None:
            detail[name] = value
    return detail


def _event_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for name in _EVENT_DETAIL:
        value = payload.get(name)
        if value not in (None, "", [], {}):
            detail[name] = value
    if payload.get("forced") is True:
        detail["forced"] = True
    acknowledged = sorted(
        name[: -len("_acknowledged")]
        for name, value in payload.items()
        if name.endswith("_acknowledged") and value
    )
    if acknowledged:
        detail["acknowledged"] = acknowledged
    return detail


def record_history(store: Store, record_id: str) -> RecordHistory:
    """Every log row that made, changed, verified, removed or restored
    `record_id`, or names it from another record, in log order, with what
    each did and whether its MAC checks (module docstring). The log and
    the tables are read in one snapshot (`read_snapshot`)."""
    with read_snapshot(store.conn):
        return _read_history(store, record_id)


@dataclass
class _RecordWalk:
    """What `_read_history` carries from one of the record's own rows to the next."""

    record_kind: str | None = None
    previous: Mapping[str, Any] | None = None
    previous_body: str | None = None
    previous_seq: int | None = None
    homed: bool = False
    last_record_kind: str | None = None


def _record_row(
    walk: _RecordWalk,
    index: int,
    row: LogRow,
    *,
    payload: dict[str, Any],
    base: dict[str, Any],
    adjacent: Callable[[int, int], tuple[str, dict[str, Any]] | None],
    verifier: _RowVerifier,
) -> HistoryRow:
    """Read one of the record's own rows, carrying its versions in ``walk``; returns the row's history line."""
    action = row.kind
    actor: dict[str, Any] | None = None
    changed: tuple[str, ...] = ()
    detail: dict[str, Any] = {}
    diff: str | None = None
    before = adjacent(index, -1)
    after = adjacent(index, 1)
    if row.kind in _VERSION_KINDS:
        prefix = row.kind.split("_", 1)[0]
        record = dict(_mapping(payload.get(prefix)) or {})
        walk.record_kind = "episode" if prefix == "episode" else "memory"
        body = record.get("body")
        body = body if isinstance(body, str) else ""
        if walk.previous is not None:
            changed = _changed(walk.previous, record)
        if body != walk.previous_body:
            diff = _body_diff(
                walk.previous_body,
                body,
                old="(none)"
                if walk.previous_seq is None
                else f"seq {walk.previous_seq}",
                new=f"seq {row.seq}",
            )
        author = _mapping(record.get("actor"))
        if author:
            actor = dict(author)
        # A label other than `local` is worth a word: the record
        # came in through an import, or from another machine.
        label = payload.get("provenance")
        if label not in (None, LOCAL):
            detail["provenance"] = label
        if row.kind == "memory_put":
            if before is not None and before[0] == "tombstone_delete":
                action = "restored"
            elif payload.get(RELINK_MARK) is not None:
                # A restore put the record's link rows back (ENG-200):
                # nothing in it changed.
                action = "relinked"
                _set(detail, restored=payload.get(RELINK_MARK))
            elif walk.previous is None:
                action = "imported" if label == IMPORTED else "created"
            elif after is not None and after[0] == "verification_put":
                action = "verified"
            elif changed and set(changed) <= _CORROBORATION_FIELDS:
                action = "corroborated"
            else:
                action = "updated"
        elif row.kind == "tombstone_put":
            _set(detail, reason=record.get("removed_reason"))
            removing = before is not None and before[0] == "memory_delete"
            action = "tombstoned" if removing or walk.previous is None else "updated"
        else:
            action = "created" if walk.previous is None else "updated"
        walk.previous, walk.previous_body, walk.previous_seq = (
            record,
            body,
            row.seq,
        )
    elif row.kind == "memory_delete":
        walk.record_kind = walk.record_kind or "memory"
        tombstoning = after is not None and after[0] == "tombstone_put"
        action = "removed" if tombstoning else "deleted"
    elif row.kind == "tombstone_delete":
        walk.record_kind = walk.record_kind or "memory"
        restoring = after is not None and after[0] == "memory_put"
        action = "restored" if restoring else "pruned"
    elif row.kind == "episode_delete":
        walk.record_kind = walk.record_kind or "episode"
        action = "pruned"
    elif row.kind == "verification_put":
        action = "verification"
        _set(
            detail,
            machine_id=payload.get("machine_id"),
            verified_head=payload.get("verified_head"),
        )
    elif row.kind == "verification_delete":
        action = "verification_removed"
        _set(detail, machine_id=payload.get("machine_id"))
    elif row.kind == "home_put":
        home = _mapping(payload.get("home")) or {}
        action = "moved" if walk.homed else "homed"
        walk.homed = True
        _set(
            detail,
            project=home.get("project"),
            checkout=home.get("checkout"),
            rule=home.get("rule"),
        )
    elif row.kind == "home_delete":
        action = "unhomed"
    elif row.kind == "standing_put":
        standing = _mapping(payload.get("standing")) or {}
        action = "standing"
        _set(
            detail,
            probability=standing.get("probability"),
            source=standing.get("source"),
            answered_by=standing.get("answered_by"),
        )
    elif row.kind == "standing_delete":
        action = "standing_removed"
    elif row.kind == "returned_put":
        returned = _mapping(payload.get("returned")) or {}
        action = "returned"
        _set(detail, via=returned.get("via"))
    elif row.kind == "returned_delete":
        # A roster read releases the mark; removing the record, or a
        # record superseding it again, drops it (ENG-200).
        dropped = payload.get("dropped")
        action = "unmarked" if dropped is not None else "released"
        _set(detail, dropped=dropped)
    elif row.kind == CAPTURE_MEMBER:
        action = "captured"
        _set(
            detail,
            batch=payload.get("batch"),
            joined_by=payload.get("joined_by"),
        )
    elif row.kind == "import_put":
        action = "import"
        _set(detail, source=payload.get("source"))
    if row.kind in _VERSION_KINDS or row.kind in _RECORD_DELETES:
        walk.last_record_kind = row.kind
    return HistoryRow(
        **base,
        role=RECORD,
        action=action,
        verified=verifier.verified(row),
        actor=actor,
        actor_source=None if actor is None else ACTOR_FROM_RECORD,
        changed=changed,
        detail=detail,
        diff=diff,
    )


def _link_change_rows(
    row: LogRow,
    *,
    payload: dict[str, Any],
    base: dict[str, Any],
    subject: str,
    record_id: str,
    links_by_source: dict[str, set[str]],
    verifier: _RowVerifier,
) -> list[HistoryRow]:
    """Report the links to the record that a linking record's version adds or drops, advancing ``links_by_source`` to that version's links; returns those rows."""
    rows: list[HistoryRow] = []
    now = _link_types(row.kind, payload, record_id)
    was = links_by_source.get(subject, set())
    links_by_source[subject] = now
    for link_type in sorted(now - was):
        action = "superseded_by" if link_type == "supersedes" else "linked_from"
        rows.append(
            HistoryRow(
                **base,
                role=REFERENCE,
                action=action,
                verified=verifier.verified(row),
                detail={"source": subject, "type": link_type},
            )
        )
    for link_type in sorted(was - now):
        rows.append(
            HistoryRow(
                **base,
                role=REFERENCE,
                action="unlinked",
                verified=verifier.verified(row),
                detail={"source": subject, "type": link_type},
            )
        )
    return rows


def _read_history(store: Store, record_id: str) -> RecordHistory:
    verifier = _RowVerifier(store)
    found, linking = _with_linking_versions(
        store, record_id, _rows_naming(store, record_id)
    )
    found = _with_restores(store, found)

    def adjacent(
        index: int, step: int, subject: str = record_id
    ) -> tuple[str, dict[str, Any]] | None:
        """The row right before or after `found[index]` in the log (seq
        one apart), as (kind, payload), when it is one of `subject`'s."""
        other = index + step
        if not 0 <= other < len(found):
            return None
        row, payload = found[other]
        if payload is None or row.seq != found[index][0].seq + step:
            return None
        if _subject(row.kind, payload) != subject:
            return None
        return row.kind, payload

    rows: list[HistoryRow] = []
    walk = _RecordWalk()
    links_by_source: dict[str, set[str]] = {}

    for index, (row, payload) in enumerate(found):
        if payload is None:
            continue
        base: dict[str, Any] = {
            "seq": row.seq,
            "ts": row.ts,
            "session": row.session,
            "kind": row.kind,
        }
        if row.kind in HISTORY_REPLACED:
            rows.append(
                HistoryRow(
                    **base,
                    role=STORE,
                    action=HISTORY_REPLACED[row.kind][1],
                    verified=verifier.verified(row),
                    detail=replaced_detail(row.kind, payload),
                )
            )
            continue
        subject = _subject(row.kind, payload)
        if subject == record_id:
            rows.append(
                _record_row(
                    walk,
                    index,
                    row,
                    payload=payload,
                    base=base,
                    adjacent=adjacent,
                    verifier=verifier,
                )
            )
            continue
        if _names_as_event(row.kind, payload, record_id):
            event_actor = _mapping(payload.get("actor"))
            rows.append(
                HistoryRow(
                    **base,
                    role=EVENT,
                    action=row.kind,
                    verified=verifier.verified(row),
                    actor=dict(event_actor) if event_actor else None,
                    actor_source=ACTOR_FROM_EVENT if event_actor else None,
                    detail=_event_detail(payload),
                )
            )
            continue
        if row.kind in _LINK_CARRIED_BY and subject in linking:
            # A delete of a record that links here. Its links go with it
            # unless the put right after carries them on (the tombstone
            # of a removal, the memory of a restore); a pruned tombstone
            # takes them away for good.
            assert subject is not None
            following = adjacent(index, 1, subject)
            if following is None or following[0] != _LINK_CARRIED_BY[row.kind]:
                for link_type in sorted(links_by_source.pop(subject, set())):
                    rows.append(
                        HistoryRow(
                            **base,
                            role=REFERENCE,
                            action="unlinked",
                            verified=verifier.verified(row),
                            detail={"source": subject, "type": link_type},
                        )
                    )
            continue
        if row.kind in _LINKING_KINDS and subject in linking:
            # A version of a record that links here: report the links to
            # this record it adds or drops against its version before.
            rows.extend(
                _link_change_rows(
                    row,
                    payload=payload,
                    base=base,
                    subject=subject,
                    record_id=record_id,
                    links_by_source=links_by_source,
                    verifier=verifier,
                )
            )
            continue
        if row.kind == "conflict_put":
            conflict = _mapping(payload.get("conflict")) or {}
            pair = (conflict.get("a_id"), conflict.get("b_id"))
            if record_id in pair:
                other = pair[1] if pair[0] == record_id else pair[0]
                rows.append(
                    HistoryRow(
                        **base,
                        role=REFERENCE,
                        action="conflict",
                        verified=verifier.verified(row),
                        detail=_set(
                            {},
                            conflict=conflict.get("id"),
                            other=other,
                            status=conflict.get("status"),
                        ),
                    )
                )

    state, provenance = _state(store, record_id, walk.last_record_kind)
    if state in ("active", "tombstoned") and walk.record_kind is None:
        walk.record_kind = _table_kind(store, record_id)
    return RecordHistory(
        record_id=record_id,
        record_kind=walk.record_kind,
        state=state,
        provenance=provenance,
        key_present=store.can_verify,
        rows=tuple(rows),
    )


def _table_kind(store: Store, record_id: str) -> str | None:
    for table, kind in (
        ("memories", "memory"),
        ("tombstones", "memory"),
        ("episodes", "episode"),
    ):
        hit = store.conn.execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        if hit is not None:
            return kind
    return None


def _state(
    store: Store, record_id: str, last_record_kind: str | None
) -> tuple[str, str | None]:
    """Where the record is now and, for an active one, its verified
    provenance label (one indexed lookup and one HMAC)."""
    conn = store.conn
    if conn.execute("SELECT 1 FROM memories WHERE id = ?", (record_id,)).fetchone():
        return "active", store.provenance_for([record_id]).get(record_id)
    if conn.execute("SELECT 1 FROM episodes WHERE id = ?", (record_id,)).fetchone():
        return "active", store.episode_provenance([record_id]).get(record_id)
    if conn.execute("SELECT 1 FROM tombstones WHERE id = ?", (record_id,)).fetchone():
        # The store checks no tombstone's pointer into the log, but every
        # tombstone it holds was written by a `tombstone_put`, and nothing
        # after that row has touched it. A tombstone the log never put,
        # or put and then pruned or restored, was written outside the
        # store. A structural fact, so it holds without the key too.
        if last_record_kind != "tombstone_put":
            return "tombstoned", UNACCOUNTED
        return "tombstoned", None
    if last_record_kind is None:
        return "unknown", None
    if last_record_kind == "memory_delete":
        return "deleted", None
    if last_record_kind in _RECORD_DELETES:
        return "pruned", None
    return "missing", None


def _actor_text(actor: Mapping[str, Any]) -> str:
    parts = [
        f"{name}={actor[name]}"
        for name in ("client", "model", "principal")
        if isinstance(actor.get(name), str) and actor[name]
    ]
    return " ".join(parts) if parts else "undeclared"


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > 80:
        text = text[:77] + "..."
    return text


_VERIFIED_TEXT = {True: "mac ok", False: "MAC FAILS", None: "mac unverifiable"}


def render_text(history: RecordHistory, *, diff: bool = False) -> str:
    """The text form: a header, one line per row, the body diff under a
    version with ``--diff``, and the findings last. Every line goes
    through `printable`, the header too: the provenance label it names
    is the table's own column, which an editor of the file can set to
    anything and which a pointer that verifies does not check."""
    kind = history.record_kind or "record"
    count = len(history.rows)
    lines = [
        f"bettermemory memory history {history.record_id}: {kind}, "
        f"{history.state}, {count} row{'' if count == 1 else 's'}"
    ]
    if history.provenance is not None:
        # Only an active record's label rests on the key; a tombstone's
        # `unaccounted` is read off the log's shape.
        keyed = history.state == "active" and not history.key_present
        checked = " (unverified: no key)" if keyed else ""
        lines.append(f"  provenance {history.provenance}{checked}")
    if not history.key_present:
        lines.append("  key not held here: row MACs cannot be checked")
    for row in history.rows:
        line = (
            f"  seq {row.seq} {row.ts} {row.kind} {row.action} "
            f"[{_VERIFIED_TEXT[row.verified]}]"
        )
        if row.actor is not None:
            label = "author" if row.actor_source == ACTOR_FROM_RECORD else "actor"
            line += f" {label} {_actor_text(row.actor)}"
        if row.changed:
            line += f" changed {','.join(row.changed)}"
        for name, value in row.detail.items():
            line += f" {name}={_value_text(value)}"
        lines.append(line)
        if diff and row.diff:
            lines.extend(f"      {text}" for text in row.diff.split("\n"))
    findings = history.findings
    if findings:
        lines.append("  findings")
        lines.extend(f"    {text}" for text in findings)
    return "\n".join(printable(line) for line in lines) + "\n"


def render_json(history: RecordHistory, *, diff: bool = False) -> str:
    return json.dumps(history.to_dict(diff=diff), indent=2) + "\n"


__all__ = [
    "ACTOR_FROM_EVENT",
    "ACTOR_FROM_RECORD",
    "EVENT",
    "FORMAT",
    "FORMAT_VERSION",
    "RECORD",
    "REFERENCE",
    "STORE",
    "HistoryRow",
    "RecordHistory",
    "history_replaced",
    "printable",
    "read_snapshot",
    "record_history",
    "render_json",
    "render_text",
    "replaced_detail",
]
