"""The origin anchor: where this host's history of a store begins, kept
outside the store file.

Segment trust (`test_segment_trust.py`) reads `unaccounted` for a log
segment whose key this host does not hold when it comes after one whose
key it does. Read off the log alone, that rule could be walked around: an
editor of the store file rewrote the row that opens this host's history
(seq 1, keeping its `mac` column, or this host's first `rekey` row on a
store copied in) so that no held key verified it, then forged a `rekey`
row naming a random key and planted rows after it. This host's history
and the forged segment then both read as a copied store's leading
history, whose labels stand: the planted memory read `local` and
`log verify` said only `unverifiable`.

The anchor, `<store_id>.origin` in the key ring, records the seq and MAC
of the first row this host signed. What is pinned here:

1. The bypass, with and without the meta edit and on a store copied in,
   reads `unaccounted` and verifies `tampered` with `origin_mismatch`.
2. The plain recipe is still caught, with the anchor holding.
3. A store under one key throughout is unchanged.
4. A store copied in is anchored at the rekey row its opener writes, and
   its history from before keeps its labels.
5. A store without an anchor is anchored by its first writing open, never
   by a reader (trust on first use, and its limit).
6. Backup and restore carry the anchor; a restore never leaves a store
   judged against an anchor of a history it does not hold.
7. `strict_provenance` combinations, a deleted or re-MAC'd anchored row,
   a lost key, and the surfaces that read provenance.

The attacker is the editor with sqlite3 and the store file, never the
keys directory: a second connection that appends rows signed with a key
of its own and applies the table rows the way the store would.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import log as chain
from bettermemory import posture
from bettermemory import store as store_module
from bettermemory.backup import create_backup, restore_bundle, verify_bundle
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.log import HistoryOrigin, KeyRing, parse_origin
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import LOCAL, STORE_FILENAME, UNACCOUNTED, Store

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def _outside(path: Path) -> sqlite3.Connection:
    """The editor with sqlite3 and no key."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _edit(path: Path, sql: str, *params: Any) -> None:
    conn = _outside(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@dataclass
class Forgery:
    key_fingerprint: str
    rekey_seq: int
    last_seq: int


def _forge(
    path: Path,
    *,
    old_fingerprint: str,
    memory: Memory | None = None,
    edit_meta: bool = True,
) -> Forgery:
    """Append a forged `rekey` row and, with `memory`, a `memory_put` row
    after it, both MAC'd with the editor's own key, and apply the put to
    the table with the forged row's MAC. With `edit_meta`, point meta's
    `key_fingerprint` at the editor's key."""
    forged_key = secrets.token_bytes(chain.KEY_BYTES)
    forged = chain.fingerprint(forged_key)
    conn = _outside(path)
    try:
        rekey = chain.append_row(
            conn,
            key=forged_key,
            kind=chain.REKEY,
            payload={"old_fingerprint": old_fingerprint, "new_fingerprint": forged},
        )
        last = rekey
        if memory is not None:
            payload: dict[str, Any] = {
                "memory": memory.model_dump(mode="json"),
                "provenance": LOCAL,
                "filename": None,
            }
            last = chain.append_row(
                conn, key=forged_key, kind="memory_put", payload=payload
            )
            store_module._apply_mutation(conn, "memory_put", payload, log_mac=last.mac)
        if edit_meta:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", (forged,)
            )
        conn.commit()
    finally:
        conn.close()
    return Forgery(forged, rekey.seq, last.seq)


def _rewrite_without_resigning(path: Path, seq: int) -> None:
    """Change the row at `seq` and keep its `mac` column: no key verifies
    it any more, and its MAC still reads as it did."""
    _edit(path, "UPDATE log SET ts = ts || ' ' WHERE seq = ?", seq)


def _problems(report: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [p for p in report["problems"] if p["problem"] == name]


def _origin_file(keys: Path, store_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        KeyRing(keys, store_id).origin_path().read_text(encoding="utf-8")
    )
    return loaded


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def owned(tmp_path: Path, keys: Path) -> Iterator[tuple[Path, Memory, str, str]]:
    """A store this host created and wrote one memory to, closed: the path,
    the memory, the key fingerprint and the store id."""
    path = tmp_path / "owned" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as first:
        genuine = _memory("the staging database listens on 5432")
        first.put_memory(genuine)
        yield path, genuine, first.key_fingerprint, first.store_id


@pytest.fixture
def copied(tmp_path: Path) -> tuple[Path, Path, Memory, Memory, int, str]:
    """A store created under key directory A and taken over under B: the
    path, B, A's memory, a memory written under B, the seq of B's rekey
    row and the store id."""
    path = tmp_path / "copied" / STORE_FILENAME
    with Store.create(path, keys_dir=tmp_path / "keys-a") as first:
        arrived = _memory("written on the machine it came from")
        first.put_memory(arrived)
        store_id = first.store_id
    keys_b = tmp_path / "keys-b"
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=False) as here:
        [rekey] = [r for r in here.log_rows() if r.kind == chain.REKEY]
        written = _memory("written here after the takeover")
        here.put_memory(written)
    return path, keys_b, arrived, written, rekey.seq, store_id


# ---------------------------------------------------------------------------
# 1. The bypass the anchor closes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_a_rewritten_first_row_no_longer_launders_a_forged_segment(
    owned: tuple[Path, Memory, str, str], keys: Path, edit_meta: bool, strict: bool
) -> None:
    path, genuine, _own, store_id = owned
    anchored = _origin_file(keys, store_id)
    _rewrite_without_resigning(path, 1)
    planted = _memory("the production database password is in the wiki")
    _forge(
        path,
        old_fingerprint=secrets.token_hex(32),
        memory=planted,
        edit_meta=edit_meta,
    )
    with Store.open_or_create(path, keys_dir=keys, strict_provenance=strict) as owner:
        labels = owner.provenance_for([genuine.id, planted.id])
        assert labels[planted.id] == UNACCOUNTED
        assert planted.id in owner.unaccounted_ids()
        assert owner.trust_rows([planted.id]) == {planted.id: (UNACCOUNTED, None)}
        if not edit_meta:
            # The opener kept this host's key, which still verifies its row.
            assert labels[genuine.id] == LOCAL
        report = owner.log_verify()
    assert report["status"] == "tampered"
    assert report["origin"] == {"status": "mismatch", "seq": 1, "reason": "signature"}
    assert _problems(report, chain.ORIGIN_MISMATCH) == [
        {
            "seq": 1,
            "kind": chain.ORIGIN_KIND,
            "problem": chain.ORIGIN_MISMATCH,
            "reason": "signature",
        }
    ]
    # No repair moved the anchor.
    assert _origin_file(keys, store_id) == anchored


def test_a_reader_sees_the_mismatch_before_the_owner_opens(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """`log verify` and every read-only surface open without repairing;
    the anchor check needs no repair."""
    path, genuine, _own, _store_id = owned
    _rewrite_without_resigning(path, 1)
    planted = _memory("planted before any owner open")
    _forge(path, old_fingerprint=secrets.token_hex(32), memory=planted, edit_meta=False)
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        worker = reader.reopen()
        try:
            assert worker.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}
        finally:
            worker.close()
        report = reader.log_verify()
    assert report["status"] == "tampered"
    assert report["origin"]["status"] == "mismatch"


@pytest.mark.parametrize("rewrite_first_row", [True, False])
def test_a_reader_sees_the_forgery_when_meta_names_the_forged_key(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    rewrite_first_row: bool,
) -> None:
    """With meta's `key_fingerprint` pointed at the forged key, a reader
    found no key for the store and bound none, so no pointer was checked
    and the planted record kept its stored `local` while `log verify`
    reported tampering. This host's key signs a segment of the log, so it
    is the store's whatever meta names: a reader checks pointers under it,
    and still refuses to write."""
    path, genuine, own, _store_id = owned
    if rewrite_first_row:
        _rewrite_without_resigning(path, 1)
    planted = _memory("planted, meta pointed at the forged key")
    _forge(path, old_fingerprint=own, memory=planted, edit_meta=True)
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.has_key is False
        assert reader.can_verify is True
        labels = reader.provenance_for([genuine.id, planted.id])
        assert labels[planted.id] == UNACCOUNTED
        if not rewrite_first_row:
            assert labels[genuine.id] == LOCAL
        assert reader.unaccounted_ids() and planted.id in reader.unaccounted_ids()
        worker = reader.reopen()
        try:
            assert worker.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}
        finally:
            worker.close()
        with pytest.raises(RuntimeError, match="open for reading only"):
            reader.put_memory(_memory("a reader never writes"))
        assert reader.log_verify()["status"] == "tampered"


def test_the_bypass_on_a_store_copied_in_is_caught_at_its_rekey_row(
    copied: tuple[Path, Path, Memory, Memory, int, str],
) -> None:
    """On a store copied here, this host's history begins at the rekey row
    its opener wrote. Renaming that row's key (its MAC column kept) makes
    this host's segment one no key here holds, so a forged segment after
    it would lead; the anchor at that row says otherwise."""
    path, keys_b, arrived, written, rekey_seq, _store_id = copied
    _edit(
        path,
        "UPDATE log SET payload = json_set(payload, '$.new_fingerprint', ?) "
        "WHERE seq = ?",
        secrets.token_hex(32),
        rekey_seq,
    )
    planted = _memory("planted behind a renamed takeover")
    _forge(path, old_fingerprint=secrets.token_hex(32), memory=planted, edit_meta=False)
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=False) as here:
        assert here.provenance_for([arrived.id, written.id, planted.id]) == {
            # The copied history cannot be told from a forgery now, and
            # nothing here verifies it.
            arrived.id: UNACCOUNTED,
            written.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = here.log_verify()
    assert report["status"] == "tampered"
    assert report["origin"] == {
        "status": "mismatch",
        "seq": rekey_seq,
        "reason": "signature",
    }


def _insert_row(
    conn: sqlite3.Connection,
    *,
    key: bytes,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    prev_mac: str,
) -> str:
    """Insert a row at `seq`, MAC'd under `key`: the editor picks the seq,
    where `log.append_row` only ever appends. Returns the row's MAC."""
    text = chain.canonical_payload(payload)
    ts = "2026-01-01T00:00:00Z"
    mac = chain.compute_mac(
        key, seq=seq, ts=ts, session=None, kind=kind, payload=text, prev_mac=prev_mac
    )
    conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (seq, ts, kind, text, prev_mac, mac),
    )
    return mac


def _put_payload(memory: Memory) -> dict[str, Any]:
    return {
        "memory": memory.model_dump(mode="json"),
        "provenance": LOCAL,
        "filename": None,
    }


def test_a_row_put_before_seq_one_cannot_pass_as_copied_history(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """Without touching the anchored row, an editor can still open the log
    with a row of its own ahead of seq 1 and rename the leading segment.
    No store writes a row below seq 1, so the segment map starts at seq 1
    as `log verify` does, and a record such a row put reads `unaccounted`,
    with the anchor or without it."""
    path, genuine, _own, _store_id = owned
    forged_key = secrets.token_bytes(chain.KEY_BYTES)
    planted = _memory("planted at seq zero")
    payload = _put_payload(planted)
    conn = _outside(path)
    try:
        mac = _insert_row(
            conn,
            key=forged_key,
            seq=0,
            kind="memory_put",
            payload=payload,
            prev_mac=chain.GENESIS_MAC,
        )
        store_module._apply_mutation(conn, "memory_put", payload, log_mac=mac)
        conn.commit()
    finally:
        conn.close()
    _forge(path, old_fingerprint=secrets.token_hex(32), edit_meta=False)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = owner.log_verify()
    # `log verify` walks the rows from seq 1 (`log.iter_rows`), so it never
    # sees the row before it; its fold finds the record that row put.
    assert report["origin"] == {"status": "ok", "seq": 1}
    assert report["status"] == "tampered"
    assert planted.id in report["unaccounted_ids"]

    KeyRing(keys, _store_id).origin_path().unlink()
    with Store.open(path, keys_dir=keys) as reader:
        assert reader.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}


@pytest.mark.parametrize("opener", ["owner", "reader"])
def test_a_segment_put_below_seq_one_never_reads_as_history_before_the_anchor(
    owned: tuple[Path, Memory, str, str], keys: Path, opener: str
) -> None:
    """The editor's rows below seq 1 as a segment of their own: a first row,
    a `rekey` row naming a key nobody holds, the planted put, and a second
    `rekey` row at seq 0 naming this host's key, so the segment this host's
    history sits in starts before the anchor. Read off those rows, the
    planted segment would end before the anchor at seq 1 and lead, and its
    label would stand. The segment map starts at seq 1, so it is no
    segment, and the planted record reads `unaccounted`."""
    path, genuine, own, store_id = owned
    forged_key = secrets.token_bytes(chain.KEY_BYTES)
    forged = chain.fingerprint(forged_key)
    planted = _memory("planted in a segment of its own below seq one")
    payload = _put_payload(planted)
    conn = _outside(path)
    try:
        prev = _insert_row(
            conn,
            key=forged_key,
            seq=-3,
            kind=chain.STORE_CREATED,
            payload={"store_id": store_id},
            prev_mac=chain.GENESIS_MAC,
        )
        prev = _insert_row(
            conn,
            key=forged_key,
            seq=-2,
            kind=chain.REKEY,
            payload={
                "old_fingerprint": secrets.token_hex(32),
                "new_fingerprint": forged,
            },
            prev_mac=prev,
        )
        put = _insert_row(
            conn,
            key=forged_key,
            seq=-1,
            kind="memory_put",
            payload=payload,
            prev_mac=prev,
        )
        store_module._apply_mutation(conn, "memory_put", payload, log_mac=put)
        _insert_row(
            conn,
            key=forged_key,
            seq=0,
            kind=chain.REKEY,
            payload={"old_fingerprint": forged, "new_fingerprint": own},
            prev_mac=put,
        )
        conn.commit()
    finally:
        conn.close()
    with (
        Store.open_or_create(path, keys_dir=keys, strict_provenance=False)
        if opener == "owner"
        else Store.open(path, keys_dir=keys, strict_provenance=False)
    ) as store:
        assert store.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        assert store.trust_rows([planted.id]) == {planted.id: (UNACCOUNTED, None)}
        trust = chain.chain_trust(
            store.conn,
            keyring=store.keyring,
            current_fingerprint=own,
            current_key=None,
        )
        report = store.log_verify()
    assert [s.first_seq for s in trust.segments] == [1]
    assert trust.origin.holds
    assert report["status"] == "tampered"
    assert planted.id in report["unaccounted_ids"]


def test_a_row_below_seq_one_on_a_store_copied_in_is_not_its_history(
    copied: tuple[Path, Path, Memory, Memory, int, str],
) -> None:
    """On a store copied here, the history before the takeover leads and
    keeps its labels. A row below seq 1 is none of it: no store wrote it,
    so the record it put reads `unaccounted` though the segment it would
    fall in leads."""
    path, keys_b, arrived, _written, _rekey_seq, _store_id = copied
    planted = _memory("planted under the copied history")
    payload = _put_payload(planted)
    conn = _outside(path)
    try:
        mac = _insert_row(
            conn,
            key=secrets.token_bytes(chain.KEY_BYTES),
            seq=0,
            kind="memory_put",
            payload=payload,
            prev_mac=chain.GENESIS_MAC,
        )
        store_module._apply_mutation(conn, "memory_put", payload, log_mac=mac)
        conn.commit()
    finally:
        conn.close()
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=False) as here:
        assert here.provenance_for([arrived.id, planted.id]) == {
            arrived.id: LOCAL,
            planted.id: UNACCOUNTED,
        }


def test_a_row_below_seq_one_never_makes_the_opener_retire_its_key(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """One row at seq 0 and a meta edit: read off that row, the leading
    segment's first row verified under no key here, so the opener found
    its key signing none of the log, retired it and rekeyed, and this
    host's own records read `unaccounted` under the anchor. From seq 1 on,
    the key signs the log and stays."""
    path, genuine, own, store_id = owned
    conn = _outside(path)
    try:
        _insert_row(
            conn,
            key=secrets.token_bytes(chain.KEY_BYTES),
            seq=0,
            kind="note",
            payload={"note": "ahead of the store's first row"},
            prev_mac=chain.GENESIS_MAC,
        )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            (secrets.token_hex(32),),
        )
        conn.commit()
    finally:
        conn.close()
    with Store.open_or_create(path, keys_dir=keys, strict_provenance=False) as owner:
        assert owner.key_fingerprint == own
        assert all(row.kind != chain.REKEY for row in owner.log_rows())
        assert owner.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        report = owner.log_verify()
    assert report["status"] == "ok"
    assert report["origin"] == {"status": "ok", "seq": 1}
    assert list(keys.glob(f"{store_id}.*.key")) == []


# ---------------------------------------------------------------------------
# 2. The plain recipe, under an anchor that holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_the_plain_recipe_is_still_caught(
    owned: tuple[Path, Memory, str, str], keys: Path, edit_meta: bool
) -> None:
    path, genuine, own, store_id = owned
    planted = _memory("planted after this host's rows")
    forgery = _forge(path, old_fingerprint=own, memory=planted, edit_meta=edit_meta)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = owner.log_verify()
    assert report["status"] == "tampered"
    assert report["origin"] == {"status": "ok", "seq": 1}
    assert [p["first_seq"] for p in _problems(report, chain.FOREIGN_SEGMENT)] == [
        forgery.rekey_seq
    ]
    assert _problems(report, chain.ORIGIN_MISMATCH) == []
    assert _origin_file(keys, store_id)["seq"] == 1


# ---------------------------------------------------------------------------
# 3. One key throughout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_a_store_under_one_key_is_anchored_at_seq_one_and_unchanged(
    tmp_path: Path, keys: Path, strict: bool
) -> None:
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys, strict_provenance=strict) as first:
        memories = [_memory(f"fact number {n}") for n in range(3)]
        for memory in memories:
            first.put_memory(memory)
        store_id = first.store_id
        own = first.key_fingerprint
        created = first.log_rows()[0]
        before = first.log_verify()
        rows_before = len(first.log_rows())
    assert _origin_file(keys, store_id) == {
        "seq": 1,
        "mac": created.mac,
        "key_fingerprint": own,
    }
    for _ in range(2):
        with Store.open_or_create(
            path, keys_dir=keys, strict_provenance=strict
        ) as again:
            assert len(again.log_rows()) == rows_before
            assert again.provenance_for([m.id for m in memories]) == {
                m.id: LOCAL for m in memories
            }
            assert again.unaccounted_ids() == []
            assert again.log_verify() == before
    assert before["status"] == "ok"
    assert before["problems"] == []
    assert before["origin"] == {"status": "ok", "seq": 1}


@POSIX_ONLY
def test_the_anchor_file_is_owner_only(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    _path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    assert stat.S_IMODE(ring.origin_path().stat().st_mode) == 0o600
    ring.replace_origin(HistoryOrigin(seq=1, mac="1" * 64))
    assert stat.S_IMODE(ring.origin_path().stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 4. A store copied in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_a_copied_store_is_anchored_at_its_takeover_and_keeps_its_labels(
    copied: tuple[Path, Path, Memory, Memory, int, str], strict: bool
) -> None:
    path, keys_b, arrived, written, rekey_seq, store_id = copied
    with Store.open(path, keys_dir=keys_b) as here:
        rekey = next(r for r in here.log_rows() if r.seq == rekey_seq)
        own = here.key_fingerprint
    assert _origin_file(keys_b, store_id) == {
        "seq": rekey_seq,
        "mac": rekey.mac,
        "key_fingerprint": own,
    }
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=strict) as here:
        assert here.provenance_for([arrived.id, written.id]) == {
            arrived.id: UNACCOUNTED if strict else LOCAL,
            written.id: LOCAL,
        }
        report = here.log_verify()
    assert report["status"] == "unverifiable"
    assert report["problems"] == []
    assert report["origin"] == {"status": "ok", "seq": rekey_seq}


def test_a_held_segment_before_an_unheld_one_before_the_anchor_is_not_foreign(
    tmp_path: Path,
) -> None:
    """Created under Y, carried to X, then taken over here under A, with
    Y's key filed here as a retired key: X's segment comes after a held
    segment, which the rule without an anchor calls foreign. It ends before
    this host's history begins, so it is history the store arrived with."""
    path = tmp_path / STORE_FILENAME
    keys_y, keys_x, keys_a = (tmp_path / name for name in ("y", "x", "a"))
    with Store.create(path, keys_dir=keys_y) as first:
        from_y = _memory("written under Y")
        first.put_memory(from_y)
        fingerprint_y = first.key_fingerprint
        key_y = first.keyring.current_path().read_bytes()
        store_id = first.store_id
    with Store.open_or_create(path, keys_dir=keys_x) as carried:
        from_x = _memory("written under X")
        carried.put_memory(from_x)
    ring_a = KeyRing(keys_a, store_id)
    ring_a.ensure_dir()
    ring_a.retired_path(fingerprint_y).write_bytes(key_y)
    with Store.open_or_create(path, keys_dir=keys_a, strict_provenance=False) as here:
        assert here.provenance_for([from_y.id, from_x.id]) == {
            from_y.id: LOCAL,
            from_x.id: LOCAL,
        }
        report = here.log_verify()
    assert report["status"] == "unverifiable"
    assert _problems(report, chain.FOREIGN_SEGMENT) == []
    assert [s["status"] for s in report["segments"]] == ["ok", "unverifiable", "ok"]

    # The rule without an anchor reads it as foreign.
    ring_a.origin_path().unlink()
    with Store.open(path, keys_dir=keys_a) as reader:
        assert reader.provenance_for([from_x.id]) == {from_x.id: UNACCOUNTED}
        assert reader.log_verify()["status"] == "tampered"


def test_a_store_moved_away_and_back_reads_as_segment_trust_documents(
    tmp_path: Path,
) -> None:
    """A to B and back to A: A's anchor stays at seq 1, so B's segment is
    after it and foreign, `log verify` reads tampered, and filing B's key
    here as a retired key vouches for it, as without the anchor."""
    path = tmp_path / STORE_FILENAME
    keys_a, keys_b = tmp_path / "keys-a", tmp_path / "keys-b"
    with Store.create(path, keys_dir=keys_a) as first:
        here = _memory("written here")
        first.put_memory(here)
        store_id = first.store_id
    with Store.open_or_create(path, keys_dir=keys_b) as away:
        there = _memory("written on the other machine")
        away.put_memory(there)
        fingerprint_b = away.key_fingerprint
        key_b = away.keyring.current_path().read_bytes()
    with Store.open_or_create(path, keys_dir=keys_a) as back:
        assert back.provenance_for([here.id, there.id]) == {
            here.id: LOCAL,
            there.id: UNACCOUNTED,
        }
        report = back.log_verify()
        back.keyring.retired_path(fingerprint_b).write_bytes(key_b)
    assert report["status"] == "tampered"
    assert report["origin"] == {"status": "ok", "seq": 1}
    assert _origin_file(keys_a, store_id)["seq"] == 1
    with Store.open_or_create(path, keys_dir=keys_a) as vouched:
        assert vouched.provenance_for([there.id]) == {there.id: LOCAL}
        assert vouched.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# 5. Upgrade: trust on first use
# ---------------------------------------------------------------------------


def test_a_store_without_an_anchor_is_anchored_by_a_writing_open_only(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    path, genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = _origin_file(keys, store_id)
    ring.origin_path().unlink()

    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        report = reader.log_verify()
    # An absent anchor is no finding.
    assert report["origin"] == {"status": "absent"}
    assert report["status"] == "ok"
    assert not ring.origin_path().exists()

    with Store.open_or_create(path, keys_dir=keys) as owner:
        rows = len(owner.log_rows())
        assert owner.log_verify()["origin"] == {"status": "ok", "seq": 1}
    assert _origin_file(keys, store_id) == anchored
    with Store.open_or_create(path, keys_dir=keys) as again:
        assert len(again.log_rows()) == rows


def test_the_upgrade_anchors_the_earliest_segment_this_host_holds(
    copied: tuple[Path, Path, Memory, Memory, int, str], tmp_path: Path
) -> None:
    """A store copied in before the anchor existed: its earliest held
    segment is the one this host's takeover opened, unless the ring also
    holds the key of an earlier one."""
    path, keys_b, _arrived, _written, rekey_seq, store_id = copied
    ring_b = KeyRing(keys_b, store_id)
    ring_b.origin_path().unlink()
    Store.open_or_create(path, keys_dir=keys_b).close()
    assert _origin_file(keys_b, store_id)["seq"] == rekey_seq

    ring_b.origin_path().unlink()
    key_a = KeyRing(tmp_path / "keys-a", store_id).load_current()
    assert key_a is not None
    ring_b.retired_path(chain.fingerprint(key_a)).write_bytes(key_a)
    Store.open_or_create(path, keys_dir=keys_b).close()
    assert _origin_file(keys_b, store_id)["seq"] == 1


def test_the_upgrade_writes_nothing_on_a_first_row_that_does_not_verify(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    ring.origin_path().unlink()
    _rewrite_without_resigning(path, 1)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys) as owner:
            report = owner.log_verify()
    assert not ring.origin_path().exists()
    assert report["origin"] == {"status": "absent"}
    assert report["status"] == "tampered"
    assert "no origin anchor is written" in caplog.text


def test_the_upgrade_writes_nothing_when_no_segment_is_held(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store from before the anchor whose history was renamed and whose
    repair cannot be written: no segment of its log is under a key held
    here, so there is nothing to anchor, and nothing is written."""
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    ring.origin_path().unlink()
    _rewrite_without_resigning(path, 1)
    _forge(path, old_fingerprint=secrets.token_hex(32), edit_meta=False)

    @contextlib.contextmanager
    def refused(self: Store) -> Iterator[sqlite3.Connection]:
        raise sqlite3.OperationalError("attempt to write a readonly database")
        yield self.conn  # pragma: no cover - never reached

    monkeypatch.setattr(Store, "_transaction", refused)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
    assert not ring.origin_path().exists()


def test_an_edit_made_before_the_upgrade_is_not_caught_by_the_anchor(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """The documented limit of trust on first use: a store from before the
    anchor, edited before its first writing open under this build, is
    anchored at the owner's own repair row, and the bypass stands unless
    `strict_provenance` is on, as it did without the anchor."""
    path, _genuine, _own, store_id = owned
    KeyRing(keys, store_id).origin_path().unlink()
    _rewrite_without_resigning(path, 1)
    planted = _memory("planted before the upgrade")
    _forge(path, old_fingerprint=secrets.token_hex(32), memory=planted, edit_meta=False)
    with Store.open_or_create(path, keys_dir=keys, strict_provenance=False) as owner:
        closing = owner.log_rows()[-1]
        assert closing.kind == chain.REKEY
        assert owner.provenance_for([planted.id]) == {planted.id: LOCAL}
    assert _origin_file(keys, store_id)["seq"] == closing.seq
    with Store.open(path, keys_dir=keys, strict_provenance=True) as strict:
        assert strict.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}


def test_an_anchor_file_that_does_not_read_is_replaced_by_the_next_writing_open(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = _origin_file(keys, store_id)
    ring.origin_path().write_text("not an anchor", encoding="utf-8")
    with Store.open(path, keys_dir=keys) as reader:
        assert reader.log_verify()["origin"] == {"status": "absent"}
    Store.open_or_create(path, keys_dir=keys).close()
    assert _origin_file(keys, store_id) == anchored


# ---------------------------------------------------------------------------
# 6. Backup and restore
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_store(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A store the CLI and the backup module resolve, under the suite's
    keys directory, with two memories."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as store:
        store.put_memory(_memory("alpha"))
        store.put_memory(_memory("beta"))
        return path, store.store_id


def test_a_bundle_records_the_anchor_and_a_restore_keeps_it_consistent(
    cli_store: tuple[Path, str], keys_dir: Path
) -> None:
    path, store_id = cli_store
    ring = KeyRing(keys_dir, store_id)
    anchored = ring.read_origin()
    assert anchored is not None
    result = create_backup(path)
    bundle = Path(result["bundle"])
    assert result["manifest"]["origin"] == anchored.to_dict()
    assert result["warnings"] == []
    verified = verify_bundle(bundle)
    assert verified["status"] == "ok"
    assert verified["origin"] == anchored.to_dict()
    assert verified["log"]["origin"] == {"status": "ok", "seq": 1}

    with Store.open_or_create(path, keys_dir=keys_dir) as store:
        store.put_memory(_memory("gamma, after the backup"))
    planned = restore_bundle(bundle, path, apply=False)
    assert planned["plan"]["origin"] == {
        "before": anchored.to_dict(),
        "after": anchored.to_dict(),
    }
    report = restore_bundle(bundle, path, apply=True)
    assert report["status"] == "restored", report["detail"]
    assert report["log"]["origin"] == {"status": "ok", "seq": 1}
    assert report["origin"] == {
        "before": anchored.to_dict(),
        "after": anchored.to_dict(),
    }
    assert ring.read_origin() == anchored
    record = json.loads((keys_dir / f"{store_id}.backup_restore.json").read_text())
    assert record["restores"][-1]["origin_after"] == anchored.to_dict()


def test_a_restore_of_a_bundle_without_an_anchor_removes_the_stale_one(
    cli_store: tuple[Path, str], keys_dir: Path
) -> None:
    path, store_id = cli_store
    ring = KeyRing(keys_dir, store_id)
    anchored = ring.read_origin()
    assert anchored is not None
    ring.origin_path().unlink()
    bundle = Path(create_backup(path)["bundle"])
    assert "origin" not in json.loads((bundle / "manifest.json").read_text())

    # A writing open anchors the live store again; the bundle has none.
    Store.open_or_create(path, keys_dir=keys_dir).close()
    assert ring.read_origin() == anchored
    report = restore_bundle(bundle, path, apply=True)
    assert report["status"] == "restored", report["detail"]
    assert report["origin"] == {"before": anchored.to_dict(), "after": None}
    assert not ring.origin_path().exists()
    assert report["log"]["origin"] == {"status": "absent"}
    Store.open_or_create(path, keys_dir=keys_dir).close()
    assert ring.read_origin() == anchored


def test_a_restore_over_a_store_taken_over_here_installs_the_bundles_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle made on the machine a store came from, restored over this
    host's copy of it after this host took it over: the bundle's key and
    anchor replace this host's, so the restored store is judged against
    the history it holds. The bundle is signed by a key only it carries
    here, so that takes the caller's word (`trust_bundle_key`)."""
    source_keys = tmp_path / "source-keys"
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=source_keys) as source:
        source.put_memory(_memory("from the source machine"))
        store_id = source.store_id
    bundle = Path(create_backup(path, keys_dir=source_keys, include_key=True)["bundle"])
    here_keys = tmp_path / "here-keys"
    monkeypatch.setenv(KEYS_DIR_ENV, str(here_keys))
    with Store.open_or_create(path, keys_dir=here_keys) as here:
        takeover = here.log_rows()[-1]
    ring = KeyRing(here_keys, store_id)
    assert ring.read_origin() == HistoryOrigin(takeover.seq, takeover.mac)

    report = restore_bundle(bundle, path, apply=True, keys_dir=here_keys)
    assert report["status"] == "refused"
    assert ring.read_origin() == HistoryOrigin(takeover.seq, takeover.mac)
    report = restore_bundle(
        bundle, path, apply=True, keys_dir=here_keys, trust_bundle_key=True
    )
    assert report["status"] == "restored", report["detail"]
    source_origin = KeyRing(source_keys, store_id).read_origin()
    assert ring.read_origin() == source_origin
    assert report["log"]["status"] == "ok"
    assert report["log"]["origin"] == {"status": "ok", "seq": 1}


def test_a_bundles_anchor_is_signed_and_shaped(
    cli_store: tuple[Path, str], keys_dir: Path
) -> None:
    path, _store_id = cli_store
    bundle = Path(create_backup(path)["bundle"])
    manifest_path = bundle / "manifest.json"
    original = json.loads(manifest_path.read_text())

    moved = dict(original, origin={"seq": 2, "mac": original["origin"]["mac"]})
    manifest_path.write_text(json.dumps(moved))
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert {p["problem"] for p in report["problems"]} >= {"manifest_mac_mismatch"}

    manifest_path.write_text(json.dumps(dict(original, origin={"seq": 1})))
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert [p["problem"] for p in report["problems"]] == ["manifest_invalid"]


def test_a_bundle_verified_where_no_key_is_held_is_unverifiable_not_tampered(
    cli_store: tuple[Path, str], tmp_path: Path
) -> None:
    path, _store_id = cli_store
    bundle = Path(create_backup(path)["bundle"])
    report = verify_bundle(bundle, keys_dir=tmp_path / "no-keys")
    assert report["status"] == "unverifiable"
    assert report["log"]["origin"] == {"status": "unverifiable", "seq": 1}
    assert report["log"]["status"] == "unverifiable"


def test_an_anchor_past_the_snapshot_is_left_out_of_the_bundle(
    cli_store: tuple[Path, str], keys_dir: Path
) -> None:
    path, store_id = cli_store
    ring = KeyRing(keys_dir, store_id)
    ring.replace_origin(HistoryOrigin(seq=99, mac="0" * 64))
    result = create_backup(path)
    assert "origin" not in result["manifest"]
    assert any("past the snapshot" in warning for warning in result["warnings"])


# ---------------------------------------------------------------------------
# 7. The anchored row, a lost key, and the switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        ("DELETE FROM log WHERE seq = 1", "missing"),
        ("UPDATE log SET mac = '" + "ab" * 32 + "' WHERE seq = 1", "mac"),
        ("UPDATE log SET mac = 'not ascii: é' WHERE seq = 1", "mac"),
    ],
    ids=["deleted", "re-maced", "non-ascii-mac"],
)
def test_the_anchored_row_deleted_or_re_maced_is_a_mismatch(
    owned: tuple[Path, Memory, str, str], keys: Path, edit: str, reason: str
) -> None:
    path, _genuine, _own, _store_id = owned
    _edit(path, edit)
    with Store.open(path, keys_dir=keys) as reader:
        origin = chain.check_origin(
            reader.conn, keyring=reader.keyring, current_key=None
        )
    assert origin.to_dict() == {"status": "mismatch", "seq": 1, "reason": reason}
    if reason == "missing":
        with Store.open(path, keys_dir=keys) as reader:
            report = reader.log_verify()
        assert report["status"] == "tampered"
        assert report["origin"] == {"status": "mismatch", "seq": 1, "reason": reason}


def test_strict_provenance_and_the_anchor_together(
    copied: tuple[Path, Path, Memory, Memory, int, str],
) -> None:
    """Under an anchor that holds, the switch still decides the copied
    history's labels; under a broken one the labels go either way."""
    path, keys_b, arrived, written, rekey_seq, _store_id = copied
    for strict in (False, True):
        with Store.open(path, keys_dir=keys_b, strict_provenance=strict) as here:
            assert here.provenance_for([arrived.id]) == {
                arrived.id: UNACCOUNTED if strict else LOCAL
            }
    _rewrite_without_resigning(path, rekey_seq)
    for strict in (False, True):
        with Store.open(path, keys_dir=keys_b, strict_provenance=strict) as here:
            assert here.provenance_for([arrived.id, written.id]) == {
                arrived.id: UNACCOUNTED,
                written.id: LOCAL,
            }


def test_a_lost_key_never_moves_the_anchor_and_reads_unverifiable(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key file is gone (the user's doing: an editor of the store file
    cannot reach the keys directory). Nothing here can verify the anchored
    row any more, and the anchor names a key the ring no longer holds: it
    stays where it is and reads `unverifiable`, the rows before the new
    key's rekey row read as a copied store's, and nothing reads as
    tampering. Putting the key back makes the anchor hold again, which a
    moved anchor could never do."""
    path, genuine, own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = ring.read_origin()
    assert anchored is not None and anchored.key_fingerprint == own
    lost = ring.current_path().read_bytes()
    ring.current_path().unlink()
    with Store.open(path, keys_dir=keys) as reader:
        report = reader.log_verify()
    # No key at all: the anchor cannot be checked, which is not a verdict.
    assert report["origin"] == {"status": "unverifiable", "seq": 1}
    assert report["status"] == "unverifiable"
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys) as owner:
            rekey = owner.log_rows()[-1]
            assert owner.provenance_for([genuine.id]) == {genuine.id: LOCAL}
            report = owner.log_verify()
    assert rekey.kind == chain.REKEY
    assert _origin_file(keys, store_id) == anchored.to_dict()
    assert report["origin"] == {"status": "unverifiable", "seq": 1}
    assert report["status"] == "unverifiable"
    assert _problems(report, chain.ORIGIN_MISMATCH) == []
    assert "origin anchor moves" not in caplog.text

    ring.retired_path(own).write_bytes(lost)
    with Store.open(path, keys_dir=keys) as reader:
        report = reader.log_verify()
    assert report["origin"] == {"status": "ok", "seq": 1}


def test_a_lost_key_keeps_an_anchor_a_retired_key_still_verifies(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = ring.read_origin()
    ring.retire_current()
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.log_verify()["status"] == "ok"
    assert ring.read_origin() == anchored


def test_a_lost_key_keeps_an_anchor_whose_row_was_edited(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = ring.read_origin()
    _edit(path, "UPDATE log SET mac = ? WHERE seq = 1", "cd" * 32)
    ring.current_path().unlink()
    with Store.open_or_create(path, keys_dir=keys) as owner:
        report = owner.log_verify()
    assert ring.read_origin() == anchored
    assert report["origin"]["status"] == "mismatch"
    assert report["status"] == "tampered"


def test_a_lost_key_never_replaces_the_anchor(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)
    anchored = ring.read_origin()
    ring.current_path().unlink()

    def refused(self: KeyRing, origin: HistoryOrigin | None) -> HistoryOrigin | None:
        raise AssertionError("the opener moved the origin anchor")

    monkeypatch.setattr(KeyRing, "replace_origin", refused)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
    assert ring.read_origin() == anchored


def _copy_store(source: Path, target: Path) -> None:
    """Copy a closed store file over another, as a user would, with the
    target's WAL files gone first so SQLite does not replay them."""
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def test_a_store_file_copied_over_again_reads_as_a_rewritten_history(
    tmp_path: Path,
) -> None:
    """The documented false positive. This host took over a store copied
    from another machine, which kept writing to its own copy; the user
    then copies that machine's file over this host's copy again. The row
    at the anchor is now another one, which is what a rewrite looks like,
    so it reads `origin_mismatch`. Deleting the anchor and the head
    accepts the replaced history, on purpose, and the next writing open
    anchors it again."""
    source, here = tmp_path / "a" / STORE_FILENAME, tmp_path / "b" / STORE_FILENAME
    keys_a, keys_b = tmp_path / "keys-a", tmp_path / "keys-b"
    with Store.create(source, keys_dir=keys_a) as created:
        created.put_memory(_memory("written on the source machine"))
        store_id = created.store_id
    _copy_store(source, here)
    with Store.open_or_create(here, keys_dir=keys_b) as taken:
        taken.put_memory(_memory("written here after the takeover"))
    anchored = KeyRing(keys_b, store_id).read_origin()
    assert anchored is not None and anchored.seq == 3
    with Store.open_or_create(source, keys_dir=keys_a) as kept:
        kept.put_memory(_memory("the source machine kept writing"))
    _copy_store(source, here)

    with Store.open_or_create(here, keys_dir=keys_b) as again:
        report = again.log_verify()
    assert report["status"] == "tampered"
    assert report["origin"] == {"status": "mismatch", "seq": 3, "reason": "mac"}

    ring = KeyRing(keys_b, store_id)
    ring.origin_path().unlink()
    ring.head_path().unlink()
    with Store.open_or_create(here, keys_dir=keys_b) as accepted:
        accepted.put_memory(_memory("written here after accepting the copy"))
        report = accepted.log_verify()
    assert report["origin"]["status"] == "ok"
    assert report["status"] == "unverifiable"
    assert _problems(report, chain.ORIGIN_MISMATCH) == []


def test_a_key_ring_that_cannot_be_written_never_fails_an_open(
    tmp_path: Path,
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refused(
        self: KeyRing, *, seq: int, mac: str, key_fingerprint: str | None = None
    ) -> bool:
        raise PermissionError("read-only key directory")

    monkeypatch.setattr(KeyRing, "write_origin", refused)
    path = tmp_path / STORE_FILENAME
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.create(path, keys_dir=keys) as created:
            assert created.log_verify()["origin"] == {"status": "absent"}
        Store.open_or_create(path, keys_dir=keys).close()
    assert "origin anchor for" in caplog.text


def test_a_log_that_cannot_be_read_never_fails_the_upgrade(
    owned: tuple[Path, Memory, str, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _genuine, _own, store_id = owned
    KeyRing(keys, store_id).origin_path().unlink()

    def unreadable(*args: Any, **kwargs: Any) -> list[chain.Segment]:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store_module, "chain_segments", unreadable)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        Store.open_or_create(path, keys_dir=keys).close()
    assert "the log could not be read" in caplog.text


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------


def test_log_verify_reports_the_anchor_in_json_and_text(
    cli_store: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bettermemory.cli import main

    path, _store_id = cli_store

    def run(argv: list[str]) -> int:
        monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
        with pytest.raises(SystemExit) as exc:
            main()
        return int(exc.value.code or 0)

    assert run(["log", "verify", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["origin"] == {"status": "ok", "seq": 1}
    assert run(["log", "verify"]) == 0
    assert "  origin ok at seq 1\n" in capsys.readouterr().out

    _rewrite_without_resigning(path, 1)
    assert run(["log", "verify"]) == 1
    text = capsys.readouterr().out
    assert "  origin mismatch at seq 1 (signature)\n" in text
    assert "seq 1 (origin): origin_mismatch" in text


def test_memory_history_reads_provenance_through_the_anchor(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    from bettermemory.history import record_history

    path, genuine, _own, _store_id = owned
    _rewrite_without_resigning(path, 1)
    planted = _memory("planted for the history surface")
    _forge(path, old_fingerprint=secrets.token_hex(32), memory=planted, edit_meta=False)
    with Store.open(path, keys_dir=keys) as reader:
        assert record_history(reader, planted.id).provenance == UNACCOUNTED
        assert record_history(reader, genuine.id).provenance == LOCAL


def test_the_security_audit_grades_the_anchor_file(
    owned: tuple[Path, Memory, str, str], keys: Path, tmp_path: Path
) -> None:
    if not hasattr(os, "geteuid"):
        pytest.skip("no effective uid on this platform")
    path, _genuine, _own, store_id = owned
    ring = KeyRing(keys, store_id)

    def graded() -> str:
        findings = posture._modes(
            platform="linux",
            store_dir=path.parent,
            store_file=path,
            keys_dir=keys,
            keyring=ring,
            state_dir=tmp_path / "state",
        )
        [finding] = [f for f in findings if f.id == "mode.origin"]
        return finding.status

    assert graded() == "ok"
    os.chmod(ring.origin_path(), 0o644)
    assert graded() == "warn"
    os.chmod(ring.origin_path(), 0o666)
    assert graded() == "high"


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_the_key_ring_writes_an_anchor_once_and_replaces_it_on_the_record(
    tmp_path: Path,
) -> None:
    ring = KeyRing(tmp_path / "keys", "a" * 32)
    assert ring.read_origin() is None
    assert ring.write_origin(seq=3, mac="1" * 64) is True
    assert ring.write_origin(seq=9, mac="2" * 64) is False
    assert ring.read_origin() == HistoryOrigin(3, "1" * 64)
    assert ring.replace_origin(HistoryOrigin(5, "3" * 64)) == HistoryOrigin(3, "1" * 64)
    assert ring.read_origin() == HistoryOrigin(5, "3" * 64)
    assert ring.replace_origin(None) == HistoryOrigin(5, "3" * 64)
    assert not ring.origin_path().exists()
    assert ring.replace_origin(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"seq": 1},
        {"mac": "1" * 64},
        {"seq": 0, "mac": "1" * 64},
        {"seq": True, "mac": "1" * 64},
        {"seq": "1", "mac": "1" * 64},
        {"seq": 1, "mac": "1" * 63},
        {"seq": 1, "mac": "A" * 64},
        {"seq": 2**63, "mac": "1" * 64},
    ],
)
def test_only_a_seq_and_a_mac_read_as_an_anchor(raw: Any) -> None:
    assert parse_origin(raw) is None
    assert parse_origin({"seq": 1, "mac": "1" * 64}) == HistoryOrigin(1, "1" * 64)
    assert parse_origin({"seq": 2**63 - 1, "mac": "1" * 64}) == HistoryOrigin(
        2**63 - 1, "1" * 64
    )


def test_an_anchor_past_what_sqlite_holds_reads_as_none(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """A seq SQLite's INTEGER cannot hold names no row, and binding it to
    the lookup raises OverflowError: as an anchor it would fail every read
    of a record's provenance. It reads as no anchor, as any file that does
    not parse does."""
    path, genuine, _own, store_id = owned
    KeyRing(keys, store_id).origin_path().write_text(
        json.dumps({"seq": 2**64, "mac": "1" * 64}), encoding="utf-8"
    )
    with Store.open(path, keys_dir=keys) as reader:
        assert reader.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        assert reader.log_verify()["origin"] == {"status": "absent"}


def test_without_an_anchor_the_segment_map_is_the_rule_without_one(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    path, _genuine, own, store_id = owned
    forgery = _forge(path, old_fingerprint=own, edit_meta=False)
    KeyRing(keys, store_id).origin_path().unlink()
    with Store.open(path, keys_dir=keys) as reader:
        trust = chain.chain_trust(
            reader.conn,
            keyring=reader.keyring,
            current_fingerprint=own,
            current_key=None,
        )
    assert trust.origin == chain.OriginCheck(chain.ORIGIN_ABSENT)
    assert [(s.first_seq, s.foreign) for s in trust.segments] == [
        (1, False),
        (forgery.rekey_seq, True),
    ]


# ---------------------------------------------------------------------------
# A MAC column that is not ASCII
# ---------------------------------------------------------------------------


def test_a_first_row_mac_that_is_not_ascii_reads_as_a_mismatch(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """`hmac.compare_digest` raises TypeError on a str that is not ASCII.
    With meta pointed at a key nobody holds and seq 1's MAC column set to
    non-ASCII text, the reader's first pointer check, `memory history`,
    `log verify` and the owner's open (a daemon's start) all ended in
    that TypeError. The row now reads as one that does not verify."""
    from bettermemory.history import record_history

    path, genuine, _fingerprint, _store_id = owned
    other = chain.fingerprint(secrets.token_bytes(chain.KEY_BYTES))
    _edit(path, "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", other)
    _edit(path, "UPDATE log SET mac = ? WHERE seq = 1", "é" * 64)

    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id])[genuine.id] in (LOCAL, UNACCOUNTED)
        assert record_history(reader, genuine.id).provenance in (LOCAL, UNACCOUNTED)
        report = reader.log_verify()
    assert report["status"] == "tampered"
    assert _problems(report, "origin_mismatch")
    with Store.open(path, keys_dir=keys) as owner:
        assert owner.log_verify()["status"] == "tampered"


def test_a_last_row_and_a_pointer_mac_that_are_not_ascii_read_as_mismatches(
    owned: tuple[Path, Memory, str, str], keys: Path
) -> None:
    """The head checkpoint's MAC is compared with the last row's, and a
    record's pointer with the row it names: an editor's non-ASCII text in
    either column is a mismatch (`head` mismatch, the record
    `unaccounted`), not a TypeError from `log verify` or from every read
    that checks the pointer."""
    path, genuine, _fingerprint, _store_id = owned
    forged = "é" * 64
    _edit(path, "UPDATE log SET mac = ? WHERE seq = (SELECT MAX(seq) FROM log)", forged)
    _edit(path, "UPDATE memories SET log_mac = ? WHERE id = ?", forged, genuine.id)
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id])[genuine.id] == UNACCOUNTED
        report = reader.log_verify()
    assert report["status"] == "tampered"
    assert report["head"]["status"] == "mismatch"
