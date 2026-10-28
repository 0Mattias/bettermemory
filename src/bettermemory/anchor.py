"""External chain anchors: `bettermemory log anchor` and
`bettermemory log verify --anchor FILE`.

The head checkpoint is the one defence against a deleted tail, and it
sits beside the key (`log.KeyRing.head_path`). A process that holds the
key can therefore rewrite a past row, recompute the MAC of every row
after it, rewrite the head, and `log verify` reads ok: every check it
makes is one the key holder can satisfy. SECURITY.md concedes this ("A
process that holds the key"). What the key holder cannot rewrite is a
record that left the host. An anchor is that record: a few fields that
pin the log up to one seq, meant to be kept where the host cannot reach
(a ticket, a private git repository, a WORM bucket, the SIEM).

An anchor (format ``bettermemory.anchor/1``) holds:

* ``store_id``: the store it pins;
* ``seq``: the log's last row when it was taken;
* ``mac``: that row's MAC, hex, as the log holds it;
* ``chain_sha256``: the SHA-256 over the chain up to ``seq``, defined
  exactly as: for every row with ``seq`` at most the anchor's, in
  ascending seq order, the UTF-8 bytes of the row's ``mac`` column
  followed by one newline byte (``0x0a``); the hex digest of the
  concatenation. It is what
  ``sqlite3 memory.sqlite "SELECT mac FROM log WHERE seq <= N ORDER BY seq" | shasum -a 256``
  prints, so an auditor can recompute it without bettermemory;
* ``created_at``: when it was taken, UTC ISO 8601.

Checking. ``log verify --anchor FILE`` reads each anchor for this store
and asks whether the log still holds a row at its seq whose MAC is the
anchor's, and whether the chain digest up to that seq is the anchor's.
Every MAC covers the MAC before it, so a key holder's rewrite of any row
at or below the seq changes the MAC at the seq, and a rewrite that keeps
the anchored row's MAC column while changing a row below it changes the
digest; a deleted tail, even with the head rewound to match, leaves no
row at the seq. Each case is the problem ``anchor_mismatch`` (with a
``reason``: ``mac``, ``digest`` or ``missing``), which makes the report
``tampered``. An anchor naming another store is reported as
``other_store`` and changes nothing, so one file can hold the anchors
of several stores. Checking needs no key: an anchor pins a copy of the
store on a machine that has none.

What an anchor is not: a defence at write time. It shows after the fact
that history up to its seq was rewritten; rows after the newest anchor
are pinned by nothing but the key, and a key holder who also holds the
anchors defeats them. It discloses the store id, a seq and MACs, never
content and never the key: an HMAC says nothing of what it covers to a
reader without the key.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._fsutil import bounded_read
from .audit_export import MAX_SEQ, read_snapshot, same_mac
from .log import last_row
from .store import Store
from .time_utils import isoformat_utc

ANCHOR_FORMAT = "bettermemory.anchor/1"

# The problem a failed anchor adds to a `log verify` report.
ANCHOR_MISMATCH = "anchor_mismatch"

STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_OTHER_STORE = "other_store"

# Why an anchor for this store failed.
REASON_MISSING = "missing"
REASON_MAC = "mac"
REASON_DIGEST = "digest"

# An anchor file is a handful of lines; one that is larger than this is
# not one, and is refused before it is read.
MAX_ANCHOR_FILE_BYTES = 16 * 1024 * 1024

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Anchor:
    """One anchor; `to_dict` is its JSON form."""

    store_id: str
    seq: int
    mac: str
    chain_sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": ANCHOR_FORMAT,
            "store_id": self.store_id,
            "seq": self.seq,
            "mac": self.mac,
            "chain_sha256": self.chain_sha256,
            "created_at": self.created_at,
        }

    def to_line(self) -> str:
        """The anchor as one line of compact, sorted JSON with its
        newline: a file of one anchor, or one line appended to a file of
        many."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def chain_digests(conn: sqlite3.Connection, seqs: Iterable[int]) -> dict[int, str]:
    """The chain digest (see the module docstring) at each of ``seqs``,
    in one pass over the MACs up to the largest: the running hash is read
    out as the walk passes each seq. A seq the log has no row at still
    gets the digest of the rows below it."""
    wanted = sorted({int(seq) for seq in seqs})
    if not wanted:
        return {}
    digest = hashlib.sha256()
    out: dict[int, str] = {}
    index = 0
    for seq, mac in conn.execute(
        "SELECT seq, mac FROM log WHERE seq <= ? ORDER BY seq", (wanted[-1],)
    ):
        while index < len(wanted) and wanted[index] < int(seq):
            out[wanted[index]] = digest.hexdigest()
            index += 1
        digest.update(str(mac).encode("utf-8") + b"\n")
    for seq in wanted[index:]:
        out[seq] = digest.hexdigest()
    return out


def take(store: Store, *, now: datetime | None = None) -> Anchor:
    """An anchor at the log's last row, read in one snapshot. Read-only.
    Raises ValueError for a log with no rows: there is nothing to pin."""
    conn = store.conn
    with read_snapshot(conn):
        last = last_row(conn)
        if last is None:
            raise ValueError(f"the log of the store at {store.path} holds no rows")
        digest = chain_digests(conn, [last.seq])[last.seq]
    created = now or datetime.now(timezone.utc)
    return Anchor(
        store_id=store.store_id,
        seq=last.seq,
        mac=last.mac,
        chain_sha256=digest,
        created_at=isoformat_utc(created),
    )


class AnchorFileError(ValueError):
    """An anchor file that cannot be read as anchors: the message names
    the file and, for a JSON Lines file, the line."""


def parse_anchor(raw: Any, *, where: str) -> Anchor:
    """One anchor from its JSON form, every field checked. ``where``
    names the file (and line) for the error."""
    if not isinstance(raw, Mapping):
        raise AnchorFileError(f"{where}: an anchor is a JSON object")
    if raw.get("format") != ANCHOR_FORMAT:
        raise AnchorFileError(
            f"{where}: not a {ANCHOR_FORMAT} anchor (format {raw.get('format')!r})"
        )
    store_id = raw.get("store_id")
    seq = raw.get("seq")
    mac = raw.get("mac")
    digest = raw.get("chain_sha256")
    created = raw.get("created_at")
    if not isinstance(store_id, str) or not store_id:
        raise AnchorFileError(f"{where}: store_id must be non-empty text")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= MAX_SEQ:
        raise AnchorFileError(
            f"{where}: seq must be a whole number from 1 to {MAX_SEQ}"
        )
    if not isinstance(mac, str) or not _HEX64.match(mac):
        raise AnchorFileError(f"{where}: mac must be 64 lowercase hex characters")
    if not isinstance(digest, str) or not _HEX64.match(digest):
        raise AnchorFileError(
            f"{where}: chain_sha256 must be 64 lowercase hex characters"
        )
    if not isinstance(created, str) or not created:
        raise AnchorFileError(f"{where}: created_at must be non-empty text")
    return Anchor(
        store_id=store_id,
        seq=seq,
        mac=mac,
        chain_sha256=digest,
        created_at=created,
    )


def load_anchors(path: Path | str) -> list[Anchor]:
    """The anchors in a file: one JSON object (what ``log anchor
    --out`` writes), or JSON Lines of them (anchors appended over time),
    blank lines skipped. Raises AnchorFileError on a file that cannot be
    read, holds no anchor, or holds anything that is not one."""
    path = Path(path)
    try:
        # `utf-8-sig`: an anchor kept off the host comes back through
        # whatever tool kept it, and a Windows editor or PowerShell's
        # UTF-8 writer puts a byte-order mark first, which `json.loads`
        # refuses. Without one the text decodes as plain UTF-8.
        text = bounded_read(path, MAX_ANCHOR_FILE_BYTES).decode("utf-8-sig")
    except (OSError, ValueError) as exc:
        raise AnchorFileError(f"{path}: cannot read an anchor file: {exc}") from exc
    # A whole file that parses is one anchor (a JSON Lines file of one
    # line included); one that does not is read line by line.
    try:
        whole = json.loads(text)
    except ValueError:
        pass
    else:
        return [parse_anchor(whole, where=str(path))]
    anchors: list[Anchor] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise AnchorFileError(f"{where}: not JSON ({exc})") from exc
        anchors.append(parse_anchor(raw, where=where))
    if not anchors:
        raise AnchorFileError(f"{path}: holds no anchor")
    return anchors


def check_anchors(
    conn: sqlite3.Connection,
    store_id: str,
    anchors: Sequence[tuple[str, Anchor]],
) -> list[dict[str, Any]]:
    """Each ``(source, anchor)`` checked against the log on ``conn``, in
    the order given, in one snapshot. An entry carries the source file,
    the anchor's store id, seq and created_at, its ``status`` (``ok``,
    ``mismatch`` or ``other_store``) and, for a mismatch, the
    ``reason``."""
    out: list[dict[str, Any]] = []
    with read_snapshot(conn):
        mine = [anchor for _, anchor in anchors if anchor.store_id == store_id]
        digests = chain_digests(conn, (anchor.seq for anchor in mine))
        for source, anchor in anchors:
            entry: dict[str, Any] = {
                "file": source,
                "store_id": anchor.store_id,
                "seq": anchor.seq,
                "created_at": anchor.created_at,
            }
            if anchor.store_id != store_id:
                entry["status"] = STATUS_OTHER_STORE
                out.append(entry)
                continue
            row = conn.execute(
                "SELECT mac FROM log WHERE seq = ?", (anchor.seq,)
            ).fetchone()
            reason: str | None = None
            if row is None:
                reason = REASON_MISSING
            elif not same_mac(anchor.mac, str(row[0])):
                reason = REASON_MAC
            elif not same_mac(digests[anchor.seq], anchor.chain_sha256):
                # As bytes, as the MAC above: `parse_anchor` holds a file's
                # digest to 64 hex characters, but `check_anchors` takes
                # any `Anchor`, and one not read through it can carry a
                # character `hmac.compare_digest` raises TypeError on.
                reason = REASON_DIGEST
            if reason is None:
                entry["status"] = STATUS_OK
            else:
                entry["status"] = STATUS_MISMATCH
                entry["reason"] = reason
            out.append(entry)
    return out


def merge_into_report(report: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Fold anchor results into a `Store.log_verify` report, in place:
    the results under ``anchors``, an ``anchor_mismatch`` problem for
    each mismatch (sorted in with the others, as `log_verify` sorts
    them), and the status ``tampered`` when there is one. An
    ``other_store`` entry changes nothing but the list."""
    report["anchors"] = results
    problems = list(report["problems"])
    for entry in results:
        if entry["status"] != STATUS_MISMATCH:
            continue
        problems.append(
            {
                "seq": entry["seq"],
                "kind": "anchor",
                "problem": ANCHOR_MISMATCH,
                "reason": entry["reason"],
                "anchor": entry["file"],
            }
        )
    problems.sort(key=lambda p: (int(p["seq"]), str(p["problem"])))
    report["problems"] = problems
    if any(entry["status"] == STATUS_MISMATCH for entry in results):
        report["status"] = "tampered"


__all__ = [
    "ANCHOR_FORMAT",
    "ANCHOR_MISMATCH",
    "MAX_ANCHOR_FILE_BYTES",
    "REASON_DIGEST",
    "REASON_MAC",
    "REASON_MISSING",
    "STATUS_MISMATCH",
    "STATUS_OK",
    "STATUS_OTHER_STORE",
    "Anchor",
    "AnchorFileError",
    "chain_digests",
    "check_anchors",
    "load_anchors",
    "merge_into_report",
    "parse_anchor",
    "take",
]
