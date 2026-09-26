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
(``bettermemory.sqlite_store``) decides which kinds are mutations, folds
them back into tables, and merges that fold with the chain report.

Segments. A store opened without its key keeps working: the store
writes a fresh key and appends a ``rekey`` row naming the old and the
new fingerprints, MAC'd with the new key. Verification splits the chain
at those rows and verifies each segment with the key its fingerprint
names, current or retired; a segment whose key is absent is reported as
unverifiable, never guessed at.

Not defended: an attacker holding the user's own key and config
directory. The design keeps the two apart from the store file so that
copying, syncing or editing the store never carries the key with it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
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
# key change and starts a new segment. Neither is a mutation of a table.
STORE_CREATED = "store_created"
REKEY = "rekey"
CONTROL_KINDS = frozenset({STORE_CREATED, REKEY})

# A retired key is filed under the first characters of its fingerprint.
_RETIRED_PREFIX_CHARS = 16

# Problems that mean the log was changed by something other than the
# store. Anything else in a report is a warning or a gap in what can be
# checked, never a verdict.
TAMPER_PROBLEMS = frozenset(
    {"mac_mismatch", "chain_break", "seq_gap", "final_key_mismatch"}
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
    store. Patched by tests; the CLI takes the default."""
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
    segment's key is not the store's, or the head names a row the log no
    longer holds; ``unverifiable`` when a segment's key is absent or there
    is no head; ``ok`` otherwise."""

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


def verify_chain(
    conn: sqlite3.Connection, *, keyring: KeyRing, current_fingerprint: str
) -> ChainReport:
    """Walk every row in seq order, checking contiguity, linkage and the
    MAC under the segment's key; then the head against the last row."""
    rows = list(iter_rows(conn))
    report = ChainReport(rows=len(rows))
    problems = report.problems

    def problem(row: LogRow, name: str, **detail: Any) -> None:
        problems.append({"seq": row.seq, "kind": row.kind, "problem": name, **detail})

    # The first segment's key: the earliest rekey row says which key it
    # replaced; with no rekey rows the store's current key signed it all.
    first_fingerprint = current_fingerprint
    for row in rows:
        if row.kind == REKEY:
            names = _rekey_fingerprints(row)
            if names is not None:
                first_fingerprint = names[0]
            break

    segment: dict[str, Any] | None = None
    key: bytes | None = None
    expected_seq = 1
    previous: LogRow | None = None

    def open_segment(row: LogRow, key_fingerprint: str) -> None:
        nonlocal segment, key
        key = keyring.key_for_fingerprint(key_fingerprint)
        segment = {
            "first_seq": row.seq,
            "last_seq": row.seq,
            "key_fingerprint": key_fingerprint,
            "status": "ok" if key is not None else "unverifiable",
            "rows": 0,
        }
        report.segments.append(segment)

    for row in rows:
        if row.kind == REKEY:
            names = _rekey_fingerprints(row)
            if names is None:
                problem(row, "payload_invalid")
                # The chain continues under a key nothing names; every
                # following row is unverifiable until the next rekey.
                open_segment(row, "")
            else:
                open_segment(row, names[1])
        elif segment is None:
            open_segment(row, first_fingerprint)
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
        if key is not None:
            expected_mac = compute_mac(
                key,
                seq=row.seq,
                ts=row.ts,
                session=row.session,
                kind=row.kind,
                payload=row.payload,
                prev_mac=row.prev_mac,
            )
            if not hmac.compare_digest(expected_mac, row.mac):
                problem(row, "mac_mismatch")
                segment["status"] = "tampered"
        previous = row

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
    "CONTROL_KINDS",
    "GENESIS_MAC",
    "KEY_BYTES",
    "REKEY",
    "STORE_CREATED",
    "TAMPER_PROBLEMS",
    "ChainReport",
    "Head",
    "KeyRing",
    "LogRow",
    "append_row",
    "canonical_payload",
    "compute_mac",
    "default_keys_dir",
    "fingerprint",
    "iter_rows",
    "last_row",
    "verify_chain",
]
