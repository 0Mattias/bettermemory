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
into the copied-store case.

The origin anchor. That check reads the log, and an editor of the file
can also rewrite the row that opens this machine's history (seq 1, or
this machine's first ``rekey`` row) so that no held key verifies it; the
history and every segment after it would then look like a copied store's,
whose rows keep their labels. So where this machine's history begins is
recorded OUTSIDE the store, in the key ring beside the key and the head:
``<store_id>.origin`` holds the seq and the MAC column of the first row
this machine's key ring signed for the store (`KeyRing.write_origin`).
The store writes it once, at `Store.create` (seq 1) or when it takes over
a store copied here (the ``rekey`` row its opener writes), and on a store
from before the anchor existed at its first writing open, as the first
row of the earliest segment this machine holds (trust on first use: an
edit made before that open is not caught by the anchor). Every check of
the segments checks the anchor first (`check_origin`): the row at its seq
must exist, carry its MAC, and verify under a key the ring holds. When it
does, a segment whose key is not held is *leading* only when it ends
before that seq, and *foreign* otherwise, whatever came before it. When
it does not, ``verify_chain`` reports ``origin_mismatch``, and since the
split between leading and foreign can no longer be trusted, the store's
reads treat every segment whose key is not held as foreign. A store with
no anchor keeps the rule above. Copying a store file over this machine's
copy again, from the machine it came from, changes the history at the
anchor and reads as ``origin_mismatch`` too: deleting
``<store_id>.origin`` and ``<store_id>.head`` accepts the replaced history,
and trusts the file again on purpose.

Not defended: an attacker holding the user's own key and config
directory. The design keeps the two apart from the store file so that
copying, syncing or editing the store never carries the key with it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import glob
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
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

# The origin anchor (the module docstring) names a row the log no longer
# holds, a row whose MAC column changed, or a row no key this machine
# holds verifies: the history where this machine's own rows begin was
# rewritten, so which segments are leading cannot be read off the log.
ORIGIN_MISMATCH = "origin_mismatch"

# Problems that mean the log was changed by something other than the
# store. Anything else in a report is a warning or a gap in what can be
# checked, never a verdict.
TAMPER_PROBLEMS = frozenset(
    {
        "mac_mismatch",
        "chain_break",
        "seq_gap",
        "final_key_mismatch",
        FOREIGN_SEGMENT,
        ORIGIN_MISMATCH,
    }
)

# What a check of the origin anchor finds (`check_origin`). ``absent``:
# no anchor, which is a store from before the anchor existed or one only
# ever opened for reading here, and is no finding. ``unverifiable``: the
# row is there with the anchor's MAC, but the ring holds no key at all to
# check it with. ``mismatch`` is the tamper finding.
ORIGIN_OK = "ok"
ORIGIN_ABSENT = "absent"
ORIGIN_MISMATCHED = "mismatch"
ORIGIN_UNVERIFIABLE = "unverifiable"

# The kind a problem about the anchor carries in a report: it names what
# was checked, as `anchor_mismatch` names the anchor, since the row it
# points at may be gone.
ORIGIN_KIND = "origin"

_HEX_MAC = re.compile(r"[0-9a-f]{64}")

# The largest seq SQLite's INTEGER holds. An anchor past it names no row
# the log can hold, and binding it to a query raises OverflowError, so it
# reads as no anchor (`parse_origin`) rather than failing every read.
_MAX_SEQ = 2**63 - 1


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


@dataclass(frozen=True)
class HistoryOrigin:
    """The origin anchor: where this machine's history of a store begins,
    as the seq and the MAC column of the first row its key ring signed
    (the module docstring). Kept beside the key, outside the store, so an
    editor of the store file cannot move it."""

    seq: int
    mac: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "mac": self.mac}


def parse_origin(raw: Any) -> HistoryOrigin | None:
    """The anchor ``raw`` describes, or None when it is not one: an object
    with a positive integer ``seq`` that SQLite can hold and a ``mac`` of
    64 lowercase hex characters, the shape `compute_mac` gives. Anything
    else is read as no anchor, as an unreadable head is read as no head."""
    if not isinstance(raw, dict):
        return None
    seq, mac = raw.get("seq"), raw.get("mac")
    if not isinstance(seq, int) or isinstance(seq, bool) or not 1 <= seq <= _MAX_SEQ:
        return None
    if not isinstance(mac, str) or _HEX_MAC.fullmatch(mac) is None:
        return None
    return HistoryOrigin(seq=seq, mac=mac)


@dataclass
class KeyRing:
    """One store's keys, head checkpoint and origin anchor under
    ``keys_dir``.

    Files: ``<store_id>.key`` (the current key), ``<store_id>.<prefix>.key``
    (a retired key, by fingerprint prefix), ``<store_id>.head`` and
    ``<store_id>.origin``.
    """

    keys_dir: Path
    store_id: str

    def __post_init__(self) -> None:
        self.keys_dir = Path(self.keys_dir)

    def current_path(self) -> Path:
        return self.keys_dir / f"{self.store_id}.key"

    def head_path(self) -> Path:
        return self.keys_dir / f"{self.store_id}.head"

    def origin_path(self) -> Path:
        return self.keys_dir / f"{self.store_id}.origin"

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

    def read_origin(self) -> HistoryOrigin | None:
        """The origin anchor, or None when there is none or the file does
        not read as one (`parse_origin`)."""
        try:
            raw = json.loads(self.origin_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return parse_origin(raw)

    def write_origin(self, *, seq: int, mac: str) -> bool:
        """Record where this machine's history of the store begins, once:
        when an anchor is already there nothing is written, whatever the
        caller names. Under the anchor's own lock, so two openers taking
        over one store write one anchor; atomic and owner-only from the
        moment it is visible, as the head is. True when written. A file
        that does not read as an anchor is replaced, as an unreadable head
        is: it anchors nothing."""
        self.ensure_dir()
        with flock_excl(self.origin_path()):
            if self.read_origin() is not None:
                return False
            self._write_origin_file(HistoryOrigin(seq=seq, mac=mac))
        return True

    def replace_origin(self, origin: HistoryOrigin | None) -> HistoryOrigin | None:
        """Install ``origin`` whatever the anchor says now, or remove the
        anchor when ``origin`` is None, and return the one it replaced.
        Only two callers move an anchor, each on the record: a restore
        puts back the bundle's (`backup.restore_bundle`), and the owner's
        opener moves one no key this machine still holds can verify, after
        this machine lost the key that signed it (`Store.open`). Nothing
        that only edits the store file reaches either."""
        self.ensure_dir()
        with flock_excl(self.origin_path()):
            before = self.read_origin()
            if origin is None:
                with contextlib.suppress(FileNotFoundError):
                    self.origin_path().unlink()
            else:
                self._write_origin_file(origin)
        return before

    def _write_origin_file(self, origin: HistoryOrigin) -> None:
        payload = json.dumps(origin.to_dict()).encode("utf-8")
        atomic_write_bytes(self.origin_path(), payload, mode_before_rename=0o600)


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
    follows one whose key is held (``foreign_segment``), the origin
    anchor no longer matches the log (``origin_mismatch``), or the head
    names a row the log no longer holds; ``unverifiable`` when a leading
    segment's key is absent, there is no head, or the ring holds no key
    to check the anchor with; ``ok`` otherwise. ``origin`` is the anchor
    check (`OriginCheck.to_dict`); an absent anchor changes no status."""

    rows: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    head: dict[str, Any] = field(default_factory=dict)
    origin: dict[str, Any] = field(default_factory=lambda: {"status": ORIGIN_ABSENT})
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rows": self.rows,
            "segments": list(self.segments),
            "problems": list(self.problems),
            "head": dict(self.head),
            "origin": dict(self.origin),
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


def _same_mac(stored: str, anchored: str) -> bool:
    """Does the MAC column ``stored`` equal the anchor's ``anchored``? As
    bytes: `hmac.compare_digest` raises TypeError on a str that is not
    ASCII, and the column is whatever an editor wrote into it."""
    return hmac.compare_digest(stored.encode("utf-8"), anchored.encode("utf-8"))


@dataclass(frozen=True)
class OriginCheck:
    """What checking the origin anchor against the log found: ``status``
    is one of ``ok``, ``absent``, ``mismatch`` or ``unverifiable`` (the
    ``ORIGIN_*`` names), ``seq`` the anchor's seq when there is one, and
    ``reason`` why a mismatch is one: ``missing`` (no row at that seq),
    ``mac`` (the row carries another MAC) or ``signature`` (no key the
    ring holds verifies the row)."""

    status: str
    seq: int | None = None
    reason: str | None = None

    @property
    def holds(self) -> bool:
        return self.status == ORIGIN_OK

    @property
    def broken(self) -> bool:
        return self.status == ORIGIN_MISMATCHED

    def to_dict(self) -> dict[str, Any]:
        """``{"status", "seq"?, "reason"?}``, in the style of a report's
        head: ``seq`` only when there is an anchor, ``reason`` only on a
        mismatch."""
        out: dict[str, Any] = {"status": self.status}
        if self.seq is not None:
            out["seq"] = self.seq
        if self.reason is not None:
            out["reason"] = self.reason
        return out

    def problem(self) -> dict[str, Any]:
        """The ``origin_mismatch`` problem a report carries for a broken
        anchor (`broken`)."""
        return {
            "seq": self.seq,
            "kind": ORIGIN_KIND,
            "problem": ORIGIN_MISMATCH,
            "reason": self.reason,
        }


def _ring_keys(keyring: KeyRing, current_key: bytes | None) -> Iterator[bytes]:
    """The keys an anchored row is tried against, lazily: the caller's own
    key first, so a store under one key spends one HMAC and reads no key
    file, then every key the ring holds."""
    if current_key is not None:
        yield current_key
    for key in keyring.held_keys():
        if key != current_key:
            yield key


def _check_origin(
    origin: HistoryOrigin | None,
    row: LogRow | None,
    keys: Callable[[], Iterable[bytes]],
) -> OriginCheck:
    """Check ``origin`` against ``row``, the log's row at its seq (None
    when there is none): the row must be there, carry the anchor's MAC,
    and verify under one of ``keys``. The first two need no key, so a
    ring that holds none still sees a row deleted or replaced; it only
    cannot say whether the row is still the one its key signed."""
    if origin is None:
        return OriginCheck(ORIGIN_ABSENT)
    if row is None:
        return OriginCheck(ORIGIN_MISMATCHED, origin.seq, "missing")
    if not _same_mac(row.mac, origin.mac):
        return OriginCheck(ORIGIN_MISMATCHED, origin.seq, "mac")
    tried = False
    for key in keys():
        tried = True
        if row_verifies(key, row):
            return OriginCheck(ORIGIN_OK, origin.seq)
    if not tried:
        return OriginCheck(ORIGIN_UNVERIFIABLE, origin.seq)
    return OriginCheck(ORIGIN_MISMATCHED, origin.seq, "signature")


def check_origin(
    conn: sqlite3.Connection,
    *,
    keyring: KeyRing,
    current_key: bytes | None = None,
) -> OriginCheck:
    """Check the ring's origin anchor against the log on ``conn``: one
    file read, one lookup by primary key and, for a store whose anchored
    row its current key signed, one HMAC. ``current_key``, the key the
    caller already holds, is tried first without reading its file
    again."""
    origin = keyring.read_origin()
    row: LogRow | None = None
    if origin is not None:
        raw = conn.execute(f"{_SELECT} WHERE seq = ?", (origin.seq,)).fetchone()
        row = None if raw is None else _row(raw)
    return _check_origin(origin, row, lambda: _ring_keys(keyring, current_key))


@dataclass(frozen=True)
class Segment:
    """One run of the chain under one key: from ``first_row`` (the log's
    first row, or a ``rekey`` row) to the row before the next ``rekey``.

    ``key_fingerprint`` is the key the log names for it, or, for a leading
    segment whose named key is not held, the held key its first row's MAC
    verifies under. ``key`` is that key when this machine holds it and
    None otherwise. ``foreign`` marks a segment whose key is not held and
    that comes after a segment whose key is: rows appended without the
    key to history this machine signed (the module docstring). Under an
    origin anchor that holds, it marks instead a segment whose key is not
    held and that does not end before the anchor's seq."""

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
    module docstring), unless the origin anchor says where this machine's
    history begins (`check_origin`)."""
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


def _anchored(segments: list[Segment], origin_seq: int) -> list[Segment]:
    """The segments under an origin anchor that holds: a segment whose key
    is not held is foreign unless it ends before ``origin_seq`` (the next
    segment starts at or before it), whatever came before it. A segment
    that ends before the anchor is history this store had before this
    machine first signed it: a copied store's, leading. A held segment is
    never foreign."""
    out: list[Segment] = []
    for position, segment in enumerate(segments):
        following = segments[position + 1] if position + 1 < len(segments) else None
        ends_before = following is not None and following.first_seq <= origin_seq
        foreign = not segment.held and not ends_before
        out.append(
            segment
            if segment.foreign == foreign
            else dataclasses.replace(segment, foreign=foreign)
        )
    return out


def _segments_from(
    first_row: LogRow | None,
    rekey_rows: Iterable[LogRow],
    *,
    keyring: KeyRing,
    current_fingerprint: str,
    current_key: bytes | None,
    origin_seq: int | None = None,
) -> list[Segment]:
    """The segment map from the log's first row and its ``rekey`` rows in
    seq order: the one definition `verify_chain` and the store's reads
    share. A ``rekey`` row whose payload names no fingerprints opens a
    segment under the empty name, which no key holds. ``origin_seq`` is
    the seq of an origin anchor that holds (`check_origin`); with it,
    which segments are foreign is read off the anchor (`_anchored`), and
    without it off the order of held and unheld segments."""
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
    if origin_seq is not None:
        return _anchored(segments, origin_seq)
    return segments


class ChainTrust(NamedTuple):
    """The chain's segments and the origin anchor check they were read
    under (`chain_trust`)."""

    segments: list[Segment]
    origin: OriginCheck


def chain_trust(
    conn: sqlite3.Connection,
    *,
    keyring: KeyRing,
    current_fingerprint: str,
    current_key: bytes | None = None,
) -> ChainTrust:
    """The chain's segments, in seq order, each with the key that verifies
    it when this machine holds one and whether it is foreign, and the
    check of the origin anchor that decided which are. Reads the ``rekey``
    rows through the ``log_by_kind`` index, and the first row and the
    anchored row by primary key, so it costs a few rows and one HMAC for
    the anchor, never a scan of the log. ``current_fingerprint`` names the
    leading segment when the log has no ``rekey`` row (the store's meta);
    ``current_key``, the key the caller already holds, is used without
    reading its file again.

    Only the rows from seq 1 on are read, the rows `verify_chain` walks
    (`iter_rows`). No store writes a row below seq 1 (`append_row`), so
    one there is an editor's, and a map read from it would let that
    editor open the log with a segment of its own: ending it with a
    ``rekey`` row that names this machine's key puts it before an anchor
    at seq 1, where it would read as a copied store's history."""
    origin = check_origin(conn, keyring=keyring, current_key=current_key)
    first = conn.execute(f"{_SELECT} WHERE seq > 0 ORDER BY seq LIMIT 1").fetchone()
    rekeys = [
        _row(raw)
        for raw in conn.execute(
            f"{_SELECT} WHERE kind = ? AND seq > 0 ORDER BY seq", (REKEY,)
        )
    ]
    segments = _segments_from(
        None if first is None else _row(first),
        rekeys,
        keyring=keyring,
        current_fingerprint=current_fingerprint,
        current_key=current_key,
        origin_seq=origin.seq if origin.holds else None,
    )
    return ChainTrust(segments, origin)


def chain_segments(
    conn: sqlite3.Connection,
    *,
    keyring: KeyRing,
    current_fingerprint: str,
    current_key: bytes | None = None,
) -> list[Segment]:
    """The segments of `chain_trust`, without the anchor check they were
    read under."""
    return chain_trust(
        conn,
        keyring=keyring,
        current_fingerprint=current_fingerprint,
        current_key=current_key,
    ).segments


def last_rekey_name(conn: sqlite3.Connection) -> str | None:
    """The fingerprint the log's last ``rekey`` row names as the new key
    (the empty string when its payload names none), or None when the log
    has no ``rekey`` row. One indexed lookup, over the rows `chain_trust`
    reads (seq 1 on)."""
    raw = conn.execute(
        f"{_SELECT} WHERE kind = ? AND seq > 0 ORDER BY seq DESC LIMIT 1", (REKEY,)
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
    a foreign segment is reported as ``foreign_segment`` with its range.
    The origin anchor is checked first, as `chain_trust` checks it; a
    broken one is ``origin_mismatch``, and the foreign segments are then
    the ones that follow a held segment, the rule without an anchor."""
    rows = list(iter_rows(conn))
    report = ChainReport(rows=len(rows))
    problems = report.problems

    def problem(row: LogRow, name: str, **detail: Any) -> None:
        problems.append({"seq": row.seq, "kind": row.kind, "problem": name, **detail})

    anchor = keyring.read_origin()
    anchored_row = (
        None
        if anchor is None
        else next((row for row in rows if row.seq == anchor.seq), None)
    )
    origin = _check_origin(anchor, anchored_row, lambda: _ring_keys(keyring, None))
    report.origin = origin.to_dict()
    if origin.broken:
        problems.append(origin.problem())

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
        origin_seq=origin.seq if origin.holds else None,
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
    unverifiable = (
        report.head["status"] == "absent"
        or origin.status == ORIGIN_UNVERIFIABLE
        or any(s["status"] == "unverifiable" for s in report.segments)
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
    "ORIGIN_ABSENT",
    "ORIGIN_KIND",
    "ORIGIN_MISMATCH",
    "ORIGIN_MISMATCHED",
    "ORIGIN_OK",
    "ORIGIN_UNVERIFIABLE",
    "REKEY",
    "STORE_CREATED",
    "TAMPER_PROBLEMS",
    "ChainReport",
    "ChainTrust",
    "Head",
    "HistoryOrigin",
    "KeyRing",
    "LogRow",
    "OriginCheck",
    "Segment",
    "append_row",
    "canonical_payload",
    "chain_segments",
    "chain_trust",
    "check_origin",
    "compute_mac",
    "default_keys_dir",
    "fingerprint",
    "iter_rows",
    "last_rekey_name",
    "last_row",
    "parse_origin",
    "row_verifies",
    "verify_chain",
]
