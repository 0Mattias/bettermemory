"""The hash-chained log of a bettermemory 9 store.

Every mutation of the store and every telemetry event is one row of the
``log`` table. Each row carries a MAC over its own fields and the MAC of
the row before it, so the log is a chain: editing a row, inserting one
without the key, or removing one from the middle breaks it. The key is
32 random bytes kept OUTSIDE the store, in the user's config directory,
and the store holds only the key's SHA-256 fingerprint; an editor with
sqlite3 and the file has nothing to sign with. A head checkpoint beside
the key records the last row's seq and MAC, so a deleted tail, which the
chain alone cannot see, is caught by comparing the two.

What this module owns:

* the MAC primitive (``compute_mac``) and the canonical payload text it
  is computed over;
* the key ring: the current key, retired keys, the head checkpoint
  (``KeyRing``);
* appending a row inside a caller's transaction (``append_row``);
* verifying the chain, its segments and the head (``verify_chain``).

What it does not own: the meaning of a row. The store
(``bettermemory.store``) decides which kinds are mutations, folds
them back into tables, and merges that fold with the chain report.

Segments. A store opened without its key keeps working: the store
writes a fresh key and appends a ``rekey`` row naming the old and the
new fingerprints, MAC'd with the new key. Verification splits the chain
at those rows and verifies each segment with the key its fingerprint
names, current or retired; a segment whose key is absent is reported as
unverifiable, never guessed at.

Held and foreign segments. A segment is *held* when this machine's key
ring has its key. The segments before the first held one are the
documented case of a store copied here without its key: their rows were
signed somewhere else, before this machine first signed the store. A
segment whose key is not held and that comes AFTER a held one is
*foreign*: a ``rekey`` row nobody here wrote, appended to history this
machine signed. Writing one needs only the store file, never the key,
so its existence is evidence of tampering, and ``verify_chain`` reports
it as ``foreign_segment`` (``chain_segments`` gives the store the same
map for its reads). The names a ``rekey`` row carries are not
authenticated until the row's own MAC is checked, and the leading
segment's name is one its own key never signed: the first ``rekey``
row's ``old_fingerprint`` (MAC'd, if at all, by the next segment's key)
or the store's meta. So a leading segment whose named key is not held is
also tried against every key the ring holds, by its first row's MAC: a
forged first ``rekey`` row alone cannot rename this machine's own history
into the copied-store case. The check reads the log, though, and nothing
outside it records where this machine's history begins: an editor who
also rewrites the row that opens that history (seq 1, or this machine's
first ``rekey`` row), so that no held key verifies it, makes the history
and every segment after it up to this machine's next ``rekey`` row look
like a copied store's, whose rows keep their labels. Only
``[security] strict_provenance`` reads those ``unaccounted``;
``verify_chain`` reports their segments ``unverifiable``.

Not defended: an attacker holding the user's own key and config
directory. The design keeps the two apart from the store file so that
copying, syncing or editing the store never carries the key with it.
"""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import platformdirs

from ._fsutil import (
    atomic_write_bytes,
    ensure_owner_only_dir,
    flock_excl,
    replace_atomic,
)
from .time_utils import isoformat_utc

log = logging.getLogger("bettermemory.log")

# The prev_mac of the first row: 32 zero bytes, hex.
GENESIS_MAC = "00" * 32
KEY_BYTES = 32

# Control rows. `store_created` is seq 1 of every store; `rekey` marks a
# key change and starts a new segment; `migrate_v8` opens a migration
# from a v8 directory and carries what it imported. None is a mutation
# of a table, and none is telemetry. The migration row is not named
# `migrate` because v8 had a telemetry kind of that name (the origin
# backfill), and imported events keep their kinds.
#
# The capture rows are control rows too, so a capture batch is on the log
# with telemetry off: `capture_open` when the daemon asks the chat model
# to save a stretch, `capture_member` for each write the batch's token
# committed (written with the memory, so an interrupted batch is still
# undoable), `capture_close` when the batch settles, with the transcript
# offset it captured to, `capture_undo` when the owner reverts one, and
# `capture_relations` when the model answers the relations its writes left
# pending. Everything capture keeps across a restart is read back from these.
STORE_CREATED = "store_created"
REKEY = "rekey"
MIGRATE_V8 = "migrate_v8"
CAPTURE_OPEN = "capture_open"
CAPTURE_MEMBER = "capture_member"
CAPTURE_CLOSE = "capture_close"
CAPTURE_UNDO = "capture_undo"
CAPTURE_RELATIONS = "capture_relations"
CAPTURE_KINDS = frozenset(
    {CAPTURE_OPEN, CAPTURE_MEMBER, CAPTURE_CLOSE, CAPTURE_UNDO, CAPTURE_RELATIONS}
)
CONTROL_KINDS = frozenset({STORE_CREATED, REKEY, MIGRATE_V8}) | CAPTURE_KINDS

# A retired key is filed under the first characters of its fingerprint.
_RETIRED_PREFIX_CHARS = 16

# A segment whose key this machine does not hold, after one whose key it
# does (see the module docstring): rows appended without the key to
# history this machine signed.
FOREIGN_SEGMENT = "foreign_segment"

# Problems that mean the log was changed by something other than the
# store. Anything else in a report is a warning or a gap in what can be
# checked, never a verdict.
TAMPER_PROBLEMS = frozenset(
    {"mac_mismatch", "chain_break", "seq_gap", "final_key_mismatch", FOREIGN_SEGMENT}
)


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """The one JSON text a payload has: sorted keys, no whitespace,
    unicode kept. The MAC is computed over this text and the text is what
    the row stores, so a verifier never re-serialises anything."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def _utcnow_iso() -> str:
    return isoformat_utc(datetime.now(timezone.utc))


def compute_mac(
    key: bytes,
    *,
    seq: int,
    ts: str,
    session: str | None,
    kind: str,
    payload: str,
    prev_mac: str,
) -> str:
    """HMAC-SHA256 over the row's fields, each length-prefixed so no two
    field boundaries can be confused. A missing session is encoded as its
    own value, distinct from an empty string."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in (str(seq), ts, session, kind, payload, prev_mac):
        if part is None:
            mac.update(b"\x00")
            continue
        data = part.encode("utf-8")
        mac.update(b"\x01")
        mac.update(len(data).to_bytes(8, "big"))
        mac.update(data)
    return mac.hexdigest()


def default_keys_dir() -> Path:
    """Where keys and heads live: the user's config directory, never the
    store. Patched by tests; the store honours ``BETTERMEMORY_KEYS_DIR``
    ahead of it (`store.resolve_keys_dir`), and the CLI takes that."""
    return Path(platformdirs.user_config_dir("bettermemory")) / "keys"


@dataclass(frozen=True)
class Head:
    """The head checkpoint: the last row the store wrote, as seen from
    outside the store."""

    seq: int
    mac: str
    ts: str


@dataclass
class KeyRing:
    """One store's keys and head checkpoint under ``keys_dir``.

    Files: ``<store_id>.key`` (the current key), ``<store_id>.<prefix>.key``
    (a retired key, by fingerprint prefix) and ``<store_id>.head``.
    """

    keys_dir: Path
    store_id: str

    def __post_init__(self) -> None:
        self.keys_dir = Path(self.keys_dir)

    def current_path(self) -> Path:
        return self.keys_dir / f"{self.store_id}.key"

    def head_path(self) -> Path:
        return self.keys_dir / f"{self.store_id}.head"

    def retired_path(self, key_fingerprint: str) -> Path:
        return (
            self.keys_dir
            / f"{self.store_id}.{key_fingerprint[:_RETIRED_PREFIX_CHARS]}.key"
        )

    def ensure_dir(self) -> None:
        ensure_owner_only_dir(self.keys_dir, parents=True)

    def create_key(self) -> bytes:
        """Write a fresh current key, owner-only, and return it."""
        self.ensure_dir()
        key = secrets.token_bytes(KEY_BYTES)
        atomic_write_bytes(self.current_path(), key, mode_before_rename=0o600)
        return key

    def load_current(self) -> bytes | None:
        try:
            return self.current_path().read_bytes()
        except OSError:
            return None

    def retire_current(self) -> str | None:
        """Move the current key file aside under its fingerprint, so the
        segment it signed stays verifiable. Returns the fingerprint, or
        None when there was no current key."""
        key = self.load_current()
        if key is None:
            return None
        key_fingerprint = fingerprint(key)
        target = self.retired_path(key_fingerprint)
        if target.exists():
            self.current_path().unlink()
        else:
            replace_atomic(self.current_path(), target)
        return key_fingerprint

    def key_for_fingerprint(self, key_fingerprint: str) -> bytes | None:
        """The key with this fingerprint, current or retired, or None."""
        for path in (self.current_path(), self.retired_path(key_fingerprint)):
            try:
                key = path.read_bytes()
            except OSError:
                continue
            if fingerprint(key) == key_fingerprint:
                return key
        return None

    def held_keys(self) -> list[bytes]:
        """Every key the ring holds for this store: the current one first,
        then the retired ones in file-name order. What a segment whose
        name the log may give wrongly is tried against (`chain_segments`);
        a key file that cannot be read is skipped."""
        keys: list[bytes] = []
        current = self.load_current()
        if current is not None:
            keys.append(current)
        pattern = f"{glob.escape(self.store_id)}.*.key"
        try:
            retired = sorted(self.keys_dir.glob(pattern))
        except OSError:
            retired = []
        for path in retired:
            try:
                key = path.read_bytes()
            except OSError:
                continue
            if key not in keys:
                keys.append(key)
        return keys

    def read_head(self) -> Head | None:
        try:
            raw = json.loads(self.head_path().read_text(encoding="utf-8"))
            return Head(
                seq=int(raw["seq"]), mac=str(raw["mac"]), ts=str(raw.get("ts", ""))
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write_head(self, *, seq: int, mac: str) -> None:
        """Record the last row, atomically and only forward: a writer that
        commits second but reaches this call first must not be rewound by
        the one behind it."""
        self.ensure_dir()
        with flock_excl(self.head_path()):
            current = self.read_head()
            if current is not None and current.seq >= seq:
                return
            payload = json.dumps({"seq": seq, "mac": mac, "ts": _utcnow_iso()})
            atomic_write_bytes(
                self.head_path(), payload.encode("utf-8"), mode_before_rename=0o600
            )


class LogRow(NamedTuple):
    seq: int
    ts: str
    session: str | None
    kind: str
    payload: str
    prev_mac: str
    mac: str


def _row(raw: sqlite3.Row | tuple[Any, ...]) -> LogRow:
    return LogRow(
        int(raw[0]),
        str(raw[1]),
        None if raw[2] is None else str(raw[2]),
        str(raw[3]),
        str(raw[4]),
        str(raw[5]),
        str(raw[6]),
    )


_SELECT = "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log"


def last_row(conn: sqlite3.Connection) -> LogRow | None:
    raw = conn.execute(f"{_SELECT} ORDER BY seq DESC LIMIT 1").fetchone()
    return None if raw is None else _row(raw)


def iter_rows(conn: sqlite3.Connection, since_seq: int = 0) -> Iterator[LogRow]:
    for raw in conn.execute(f"{_SELECT} WHERE seq > ? ORDER BY seq", (int(since_seq),)):
        yield _row(raw)


def append_row(
    conn: sqlite3.Connection,
    *,
    key: bytes,
    kind: str,
    payload: Mapping[str, Any],
    session: str | None = None,
    ts: str | None = None,
) -> LogRow:
    """Append one row inside the caller's transaction. The caller holds
    the write lock (``BEGIN IMMEDIATE``), which is what makes the seq and
    the prev_mac read here the ones the row is chained to."""
    previous = last_row(conn)
    seq = 1 if previous is None else previous.seq + 1
    prev_mac = GENESIS_MAC if previous is None else previous.mac
    stamp = ts or _utcnow_iso()
    text = canonical_payload(payload)
    mac = compute_mac(
        key,
        seq=seq,
        ts=stamp,
        session=session,
        kind=kind,
        payload=text,
        prev_mac=prev_mac,
    )
    conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seq, stamp, session, kind, text, prev_mac, mac),
    )
    return LogRow(seq, stamp, session, kind, text, prev_mac, mac)


@dataclass
class ChainReport:
    """What verifying the chain found. ``status`` is ``tampered`` when a
    row fails its MAC, the chain breaks, a seq is skipped, the final
    segment's key is not the store's, a segment whose key is absent
    follows one whose key is held (``foreign_segment``), or the head names
    a row the log no longer holds; ``unverifiable`` when a leading
    segment's key is absent or there is no head; ``ok`` otherwise."""

    rows: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    head: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows": self.rows,
            "segments": list(self.segments),
            "problems": list(self.problems),
            "head": dict(self.head),
        }


def _rekey_fingerprints(row: LogRow) -> tuple[str, str] | None:
    try:
        payload = json.loads(row.payload)
        old = payload["old_fingerprint"]
        new = payload["new_fingerprint"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return old, new


def row_verifies(key: bytes, row: LogRow) -> bool:
    """Does ``row``'s MAC verify under ``key``?"""
    expected = compute_mac(
        key,
        seq=row.seq,
        ts=row.ts,
        session=row.session,
        kind=row.kind,
        payload=row.payload,
        prev_mac=row.prev_mac,
    )
    return hmac.compare_digest(expected, row.mac)


@dataclass(frozen=True)
class Segment:
    """One run of the chain under one key: from ``first_row`` (the log's
    first row, or a ``rekey`` row) to the row before the next ``rekey``.

    ``key_fingerprint`` is the key the log names for it, or, for a leading
    segment whose named key is not held, the held key its first row's MAC
    verifies under. ``key`` is that key when this machine holds it and
    None otherwise. ``foreign`` marks a segment whose key is not held and
    that comes after a segment whose key is: rows appended without the
    key to history this machine signed (the module docstring)."""

    first_seq: int
    key_fingerprint: str
    key: bytes | None = field(default=None, repr=False, compare=False)
    foreign: bool = False
    first_row: LogRow | None = field(default=None, repr=False, compare=False)

    @property
    def held(self) -> bool:
        return self.key is not None


def _leading_key(
    keyring: KeyRing,
    named: str,
    first_row: LogRow,
    current_key: bytes | None,
) -> tuple[str, bytes | None]:
    """The leading segment's fingerprint and key. Its own key never signed
    its name (the first ``rekey`` row's ``old_fingerprint``, or the
    store's meta when no ``rekey`` row exists), so when the named key is
    not held the first row is tried against every key the ring holds: a
    writer without the key who renames this machine's own history must
    not turn it into the copied-store case, whose rows keep their labels.
    A key that verifies the first row is the key that signed it. A first
    row rewritten as well verifies under none, and the rename stands (the
    module docstring)."""
    if current_key is not None and fingerprint(current_key) == named:
        return named, current_key
    key = keyring.key_for_fingerprint(named) if named else None
    if key is not None:
        return named, key
    candidates = keyring.held_keys()
    if current_key is not None and current_key not in candidates:
        candidates.insert(0, current_key)
    for candidate in candidates:
        if row_verifies(candidate, first_row):
            return fingerprint(candidate), candidate
    return named, None


def _segments_from(
    first_row: LogRow | None,
    rekey_rows: Iterable[LogRow],
    *,
    keyring: KeyRing,
    current_fingerprint: str,
    current_key: bytes | None,
) -> list[Segment]:
    """The segment map from the log's first row and its ``rekey`` rows in
    seq order: the one definition `verify_chain` and the store's reads
    share. A ``rekey`` row whose payload names no fingerprints opens a
    segment under the empty name, which no key holds."""
    if first_row is None:
        return []
    starts: list[tuple[LogRow, str]] = []
    leading_name = current_fingerprint
    for position, row in enumerate(rekey_rows):
        names = _rekey_fingerprints(row)
        if position == 0 and names is not None:
            leading_name = names[0]
        starts.append((row, "" if names is None else names[1]))
    segments: list[Segment] = []
    held_seen = False
    if not starts or starts[0][0].seq != first_row.seq:
        name, key = _leading_key(keyring, leading_name, first_row, current_key)
        segments.append(Segment(first_row.seq, name, key, False, first_row))
        held_seen = key is not None
    keys: dict[str, bytes | None] = {}
    if current_key is not None:
        keys[fingerprint(current_key)] = current_key
    for row, name in starts:
        if name not in keys:
            keys[name] = keyring.key_for_fingerprint(name) if name else None
        key = keys[name]
        segments.append(Segment(row.seq, name, key, key is None and held_seen, row))
        held_seen = held_seen or key is not None
    return segments


def chain_segments(
    conn: sqlite3.Connection,
    *,
    keyring: KeyRing,
    current_fingerprint: str,
    current_key: bytes | None = None,
) -> list[Segment]:
    """The chain's segments, in seq order, each with the key that verifies
    it when this machine holds one and whether it is foreign. Reads the
    ``rekey`` rows through the ``log_by_kind`` index and the first row by
    its primary key, so it costs a few rows, never a scan of the log.
    ``current_fingerprint`` names the leading segment when the log has no
    ``rekey`` row (the store's meta); ``current_key``, the key the caller
    already holds, is used without reading its file again."""
    first = conn.execute(f"{_SELECT} ORDER BY seq LIMIT 1").fetchone()
    rekeys = [
        _row(raw)
        for raw in conn.execute(f"{_SELECT} WHERE kind = ? ORDER BY seq", (REKEY,))
    ]
    return _segments_from(
        None if first is None else _row(first),
        rekeys,
        keyring=keyring,
        current_fingerprint=current_fingerprint,
        current_key=current_key,
    )


def last_rekey_name(conn: sqlite3.Connection) -> str | None:
    """The fingerprint the log's last ``rekey`` row names as the new key
    (the empty string when its payload names none), or None when the log
    has no ``rekey`` row. One indexed lookup."""
    raw = conn.execute(
        f"{_SELECT} WHERE kind = ? ORDER BY seq DESC LIMIT 1", (REKEY,)
    ).fetchone()
    if raw is None:
        return None
    names = _rekey_fingerprints(_row(raw))
    return "" if names is None else names[1]


def verify_chain(
    conn: sqlite3.Connection, *, keyring: KeyRing, current_fingerprint: str
) -> ChainReport:
    """Walk every row in seq order, checking contiguity, linkage and the
    MAC under the segment's key; then the head against the last row. A
    segment is verified under the key `chain_segments` would give it, so
    the audit and the store's reads agree on which rows a key covers, and
    a foreign segment is reported as ``foreign_segment`` with its range."""
    rows = list(iter_rows(conn))
    report = ChainReport(rows=len(rows))
    problems = report.problems

    def problem(row: LogRow, name: str, **detail: Any) -> None:
        problems.append({"seq": row.seq, "kind": row.kind, "problem": name, **detail})

    # The leading segment's name is the earliest rekey row's
    # `old_fingerprint`; with no rekey rows the store's current key
    # signed it all. `_segments_from` checks the name against the first
    # row's MAC when the named key is not held here.
    segments = _segments_from(
        rows[0] if rows else None,
        [row for row in rows if row.kind == REKEY],
        keyring=keyring,
        current_fingerprint=current_fingerprint,
        current_key=None,
    )
    starts = {s.first_seq: s for s in segments}

    segment: dict[str, Any] | None = None
    key: bytes | None = None
    expected_seq = 1
    previous: LogRow | None = None
    foreign: list[dict[str, Any]] = []

    def open_segment(row: LogRow, named: Segment) -> None:
        nonlocal segment, key
        key = named.key
        segment = {
            "first_seq": row.seq,
            "last_seq": row.seq,
            "key_fingerprint": named.key_fingerprint,
            "status": "ok" if key is not None else "unverifiable",
            "rows": 0,
        }
        report.segments.append(segment)
        if named.foreign:
            foreign.append(segment)

    for row in rows:
        if row.kind == REKEY and _rekey_fingerprints(row) is None:
            # The chain continues under a key nothing names; every
            # following row is unverifiable until the next rekey.
            problem(row, "payload_invalid")
        if row.seq in starts:
            open_segment(row, starts[row.seq])
        assert segment is not None
        segment["last_seq"] = row.seq
        segment["rows"] += 1
        if row.seq != expected_seq:
            problem(row, "seq_gap", expected=expected_seq)
            expected_seq = row.seq
        expected_seq += 1
        linked_to = GENESIS_MAC if previous is None else previous.mac
        if row.prev_mac != linked_to:
            problem(row, "chain_break")
        if key is not None and not row_verifies(key, row):
            problem(row, "mac_mismatch")
            segment["status"] = "tampered"
        previous = row

    for stray in foreign:
        problems.append(
            {
                "seq": stray["first_seq"],
                "kind": REKEY,
                "problem": FOREIGN_SEGMENT,
                "first_seq": stray["first_seq"],
                "last_seq": stray["last_seq"],
                "key_fingerprint": stray["key_fingerprint"],
            }
        )

    if segment is not None and segment["key_fingerprint"] != current_fingerprint:
        problems.append(
            {
                "seq": segment["last_seq"],
                "kind": REKEY,
                "problem": "final_key_mismatch",
                "expected": current_fingerprint,
                "found": segment["key_fingerprint"],
            }
        )

    head = keyring.read_head()
    last = rows[-1] if rows else None
    if head is None:
        report.head = {"status": "absent"}
    elif last is None:
        report.head = {"status": "tail_deleted", "seq": head.seq, "mac": head.mac}
    elif head.seq > last.seq:
        report.head = {"status": "tail_deleted", "seq": head.seq, "mac": head.mac}
    elif head.seq == last.seq:
        matches = hmac.compare_digest(head.mac, last.mac)
        report.head = {
            "status": "ok" if matches else "mismatch",
            "seq": head.seq,
            "mac": head.mac,
        }
    else:
        named = next((r for r in rows if r.seq == head.seq), None)
        matches = named is not None and hmac.compare_digest(head.mac, named.mac)
        report.head = {
            "status": "stale" if matches else "mismatch",
            "seq": head.seq,
            "mac": head.mac,
        }

    tampered = any(p["problem"] in TAMPER_PROBLEMS for p in problems) or report.head[
        "status"
    ] in {"tail_deleted", "mismatch"}
    unverifiable = report.head["status"] == "absent" or any(
        s["status"] == "unverifiable" for s in report.segments
    )
    if tampered:
        report.status = "tampered"
    elif unverifiable:
        report.status = "unverifiable"
    else:
        report.status = "ok"
    return report


__all__ = [
    "CAPTURE_CLOSE",
    "CAPTURE_KINDS",
    "CAPTURE_MEMBER",
    "CAPTURE_OPEN",
    "CAPTURE_RELATIONS",
    "CAPTURE_UNDO",
    "CONTROL_KINDS",
    "FOREIGN_SEGMENT",
    "GENESIS_MAC",
    "KEY_BYTES",
    "MIGRATE_V8",
    "REKEY",
    "STORE_CREATED",
    "TAMPER_PROBLEMS",
    "ChainReport",
    "Head",
    "KeyRing",
    "LogRow",
    "Segment",
    "append_row",
    "canonical_payload",
    "chain_segments",
    "compute_mac",
    "default_keys_dir",
    "fingerprint",
    "iter_rows",
    "last_rekey_name",
    "last_row",
    "row_verifies",
    "verify_chain",
]
