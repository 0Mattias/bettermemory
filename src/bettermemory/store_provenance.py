"""The `Store` methods that label a record's provenance and trust.

`provenance_for` labels each record by how it entered the store, checking
its row's pointer into the log (`_pointer_verified`) under the segment map
and the origin anchor (`_segment_for`, `_origin_broken`), so a row that
entered outside the log reads `unaccounted`; `trust_rows`,
`unaccounted_ids`, `provenance_counts`, `links_with_status` and
`episode_provenance` read through it. Each is a function of `self: Store`
that `Store` binds by name. It lives apart from `store.py` to keep each
module under the package's 2,000-line bound (ENG-96); it imports
`store_schema`, and `store` only for type checking.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .log import Segment, chain_trust, compute_mac, same_mac
from .store_schema import LOCAL, UNACCOUNTED, _PROVENANCE_BATCH, TrustRow

if TYPE_CHECKING:
    from .store import Store


def provenance_for(self: Store, memory_ids: Sequence[str]) -> dict[str, str]:
    """``{id: label}`` for the active records asked. The label is the
    row's, unless the row's pointer into the log fails: the ``log_mac``
    names no row, names a row that put a different record, names a row
    whose MAC does not verify under the segment's key, or names a row
    in a foreign segment (one whose key this machine does not hold,
    after one whose key it does, or at or after the origin anchor),
    or, with ``strict_provenance`` or an origin anchor the log no
    longer matches, in any segment whose key is not held, that this
    machine's own key did not sign either. Such a row reads
    ``unaccounted``, as does one the last ``refold`` in this process
    found so. A row in a leading segment whose key is not held (a
    store copied here without its key) keeps its label. A store
    opened without its key cannot verify pointers and returns the row
    labels as stored; ``trust_rows`` says when that is the case."""
    ids = list(dict.fromkeys(memory_ids))
    if not ids:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), _PROVENANCE_BATCH):
        batch = ids[start : start + _PROVENANCE_BATCH]
        placeholders = ",".join("?" * len(batch))
        for row in self._conn.execute(
            "SELECT id, provenance, log_mac FROM memories "
            f"WHERE id IN ({placeholders})",
            batch,
        ):
            memory_id = str(row["id"])
            label = str(row["provenance"])
            if memory_id in self._unaccounted_memory_ids:
                label = UNACCOUNTED
            elif (
                self._pointer_verified(memory_id, "memory_put", row["log_mac"]) is False
            ):
                label = UNACCOUNTED
            out[memory_id] = label
    return out


def _segment_for(self: Store, seq: int) -> Segment | None:
    """The segment that holds the row at ``seq``, with its key when
    this machine holds it and whether it is foreign. The map is read
    off the rekey rows once per connection (`log.chain_segments`), so a
    row costs this walk over a handful of segments and nothing more.
    A connection that was open before a rekey row was appended keeps
    its map, and verifies a later row under the last segment it knows,
    which a row signed by another key fails. The origin anchor is
    checked with it (`log.chain_trust`): one more row and one HMAC."""
    if self._segments is None:
        trust = chain_trust(
            self._conn,
            keyring=self._keyring,
            current_fingerprint=self.key_fingerprint,
            current_key=self._verify_key,
        )
        self._segments, self._origin = trust.segments, trust.origin
    found: Segment | None = None
    for segment in self._segments:
        if found is not None and seq < segment.first_seq:
            break
        found = segment
    return found


def _origin_broken(self: Store) -> bool:
    """Does the origin anchor this connection's segment map was read
    under no longer match the log (`log.OriginCheck.broken`)? False
    before the map is read, and for a store with no anchor."""
    return self._origin is not None and self._origin.broken


def _pointer_verified(
    self: Store, record_id: str, kind: str, log_mac: Any
) -> bool | None:
    """Does the record's ``log_mac`` name a log row of ``kind`` that
    put this very record, and does that row's MAC verify under its
    segment's key? A row in a foreign segment is checked against this
    connection's own key instead, since nobody here holds the
    segment's: it verifies only when this machine appended it, and
    anything else is False. A row in a leading segment whose key is
    not held answers None (its label stands), or is checked the same
    way under ``strict_provenance`` or when the origin anchor no
    longer matches the log, since which segments lead is then not
    known. A row below seq 1 is False whatever its segment. None too
    when the store was opened without its key. The True and False
    answers are cached per (id, mac)."""
    own_key = self._verify_key
    if own_key is None:
        return None
    if not isinstance(log_mac, str) or not log_mac:
        return False
    cache_key = (record_id, log_mac)
    cached = self._pointer_cache.get(cache_key)
    if cached is not None:
        return cached
    raw = self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log WHERE mac = ?",
        (log_mac,),
    ).fetchone()
    verdict = False
    if raw is not None and str(raw["kind"]) == kind:
        try:
            payload = json.loads(raw["payload"])
            record = payload[kind.split("_", 1)[0]]
            named = str(record["id"])
        except (ValueError, KeyError, TypeError):
            named = ""
        # No store writes a row below seq 1 (`log.append_row`), and no
        # segment holds one (`log.chain_trust`): a record that names
        # one was put by an editor of the file, whatever its label.
        if named == record_id and int(raw["seq"]) > 0:
            segment = self._segment_for(int(raw["seq"]))
            if segment is None:
                return None
            key = segment.key
            if key is None:
                if (
                    not segment.foreign
                    and not self._origin_broken()
                    and not self.strict_provenance
                ):
                    # A leading segment: signed elsewhere, before this
                    # machine first signed the store. Its label stands.
                    return None
                # No key here signed the segment, but this machine
                # may have appended rows to it: a daemon that had the
                # store open while a forged rekey row went in, or an
                # opener whose repair could not be written, writes
                # under its own key into the segment that row opened.
                # Those rows verify under that key; a forged one
                # cannot, so it still reads unaccounted.
                key = own_key
            expected = compute_mac(
                key,
                seq=int(raw["seq"]),
                ts=str(raw["ts"]),
                session=None if raw["session"] is None else str(raw["session"]),
                kind=str(raw["kind"]),
                payload=str(raw["payload"]),
                prev_mac=str(raw["prev_mac"]),
            )
            verdict = same_mac(expected, log_mac)
    self._pointer_cache[cache_key] = verdict
    return verdict


def trust_rows(self: Store, memory_ids: Sequence[str]) -> dict[str, TrustRow] | None:
    """``{id: TrustRow}`` for the active records asked: the provenance
    verdict ``provenance_for`` gives and this host's verification stamp
    (the ``verifications`` row with no machine id), or None when the
    store was opened without its key, so no pointer can be verified and
    the response must say trust is unavailable."""
    if self._verify_key is None:
        return None
    labels = self.provenance_for(memory_ids)
    if not labels:
        return {}
    stamps: dict[str, str] = {}
    ids = list(labels)
    for start in range(0, len(ids), _PROVENANCE_BATCH):
        batch = ids[start : start + _PROVENANCE_BATCH]
        placeholders = ",".join("?" * len(batch))
        for row in self._conn.execute(
            "SELECT memory_id, verified_at FROM verifications "
            f"WHERE machine_id IS NULL AND memory_id IN ({placeholders})",
            batch,
        ):
            stamps[str(row["memory_id"])] = str(row["verified_at"])
    return {
        memory_id: TrustRow(label, stamps.get(memory_id))
        for memory_id, label in labels.items()
    }


def unaccounted_ids(self: Store) -> list[str]:
    """Every active record whose pointer into the log fails, newest
    first: the planted shape, found by one pass over the rows rather
    than by a refold."""
    out: list[tuple[str, str]] = []
    for row in self._conn.execute(
        "SELECT id, created, log_mac FROM memories ORDER BY created DESC, id"
    ):
        memory_id = str(row["id"])
        if (
            memory_id in self._unaccounted_memory_ids
            or self._pointer_verified(memory_id, "memory_put", row["log_mac"]) is False
        ):
            out.append((str(row["created"]), memory_id))
    return [memory_id for _, memory_id in out]


def provenance_counts(self: Store) -> dict[str, int]:
    """How many active records carry each label, with ``unaccounted``
    counted by the pointer check rather than by any stored label."""
    counts: dict[str, int] = {}
    unaccounted = set(self.unaccounted_ids())
    for row in self._conn.execute("SELECT id, provenance FROM memories"):
        label = UNACCOUNTED if str(row["id"]) in unaccounted else str(row["provenance"])
        counts[label] = counts.get(label, 0) + 1
    return counts


def links_with_status(
    self: Store, memory_id: str
) -> tuple[
    list[tuple[str, str, str | None]],
    list[tuple[str, str, str | None]],
    TrustRow | None,
]:
    """One record's outbound and inbound links and its trust row
    (None for an id the store does not hold, or when trust is
    unavailable)."""
    links = self.links_for_many([memory_id])[memory_id]
    trust = self.trust_rows([memory_id])
    return links[0], links[1], None if trust is None else trust.get(memory_id)


def episode_provenance(self: Store, episode_ids: Sequence[str]) -> dict[str, str]:
    """``{id: label}`` for the episodes asked: ``local`` when the row's
    pointer into the log verifies, ``unaccounted`` when it does not
    (the planted shape, a row in a foreign segment, or one in any
    segment whose key is not held under ``strict_provenance``, that
    this machine's own key did not sign either; `_pointer_verified`), the
    row's label as stored when the key is not on this machine. Unknown
    ids are omitted."""
    ids = list(dict.fromkeys(episode_ids))
    if not ids:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), _PROVENANCE_BATCH):
        batch = ids[start : start + _PROVENANCE_BATCH]
        placeholders = ",".join("?" * len(batch))
        for row in self._conn.execute(
            f"SELECT id, log_mac FROM episodes WHERE id IN ({placeholders})", batch
        ):
            episode_id = str(row["id"])
            verified = self._pointer_verified(episode_id, "episode_put", row["log_mac"])
            out[episode_id] = UNACCOUNTED if verified is False else LOCAL
    return out
