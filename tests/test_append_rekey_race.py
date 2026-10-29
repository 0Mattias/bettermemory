"""A rekey that lands between a connection's open and its write (ENG-81).

A connection reads its key once, at open. `Store._append` signed with
that key whatever meta said by the time the write transaction began, so a
rekey another connection committed in between (an owner's open that
replaced a lost key, under the same key ring or another) left the next row
signed with the old key inside the new key's segment, and `log verify`
read the store `tampered` though nobody tampered with it.

`_append` now reads ``meta.key_fingerprint`` again inside its transaction,
where the write lock holds it still. When the key ring's current key is
the one meta names, the connection takes it and signs with it. Meta is
not authenticated, so a key it names that the ring does not hold (a rekey
under another keys directory, an edit of the file) is not taken at its
word: the connection signs on under its own key, as before.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import log as chain
from bettermemory import store as store_module
from bettermemory.log import KeyRing, fingerprint
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import LOCAL, STORE_FILENAME, Store

#: How long the rekeying connection holds the write lock after it says so:
#: long enough for the other connection's write to reach its wait for the
#: lock, well inside the 5 s busy timeout that wait is bounded by.
_HOLD_SECONDS = 0.5


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


def _verified(path: Path, keys: Path) -> dict[str, Any]:
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        return reader.log_verify()


def test_a_rekey_committed_while_a_write_waits_is_signed_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer opened under the old key, a rekey took the write lock
    first, and the writer's transaction began once it committed: the row is
    signed with the new key and the log verifies."""
    keys = tmp_path / "keys"
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as created:
        store_id = created.store_id
        old = created.key_fingerprint
    writer = Store.open(path, keys_dir=keys, allow_rekey=True)
    try:
        writer.put_memory(_memory("signed under the first key"))
        # The key is lost from the ring (retired, so its segment stays
        # verifiable), and the next owner's open writes a new one.
        KeyRing(keys, store_id).retire_current()

        holding = threading.Event()
        failures: list[BaseException] = []
        # `store` signs through the name it imported from `log`.
        original = chain.append_row

        def slow_rekey(conn: Any, **kwargs: Any) -> Any:
            if (
                kwargs.get("kind") == chain.REKEY
                and threading.current_thread() is rekeyer
            ):
                holding.set()
                time.sleep(_HOLD_SECONDS)
            return original(conn, **kwargs)

        def rekey() -> None:
            try:
                Store.open(path, keys_dir=keys, allow_rekey=True).close()
            except BaseException as exc:  # noqa: BLE001 - reported below
                failures.append(exc)
                holding.set()

        monkeypatch.setattr(store_module, "append_row", slow_rekey)
        rekeyer = threading.Thread(target=rekey, daemon=True)
        rekeyer.start()
        assert holding.wait(10.0)
        # The rekey holds the write lock: this write waits for it, and its
        # transaction begins after the rekey committed.
        writer.put_memory(_memory("signed while the store was rekeyed"))
        rekeyer.join(10.0)
        assert not rekeyer.is_alive()
        assert failures == []
        new = KeyRing(keys, store_id).load_current()
        assert new is not None and fingerprint(new) != old
        rows = writer.log_rows()
        assert [row.kind for row in rows].count(chain.REKEY) == 1
        assert rows[-1].kind != chain.REKEY
    finally:
        writer.close()
    report = _verified(path, keys)
    assert report["status"] == "ok", report.get("findings", report)
    with Store.open(path, keys_dir=keys, allow_rekey=True) as again:
        again.put_memory(_memory("the owner writes on under the new key"))
        assert again.key_state.expected == fingerprint(new)
    assert _verified(path, keys)["status"] == "ok"


def test_a_key_meta_names_that_the_ring_does_not_hold_is_not_taken(
    tmp_path: Path,
) -> None:
    """Meta is not authenticated. A rekey under another keys directory (or
    an edit of meta) names a key this ring does not hold: the connection
    signs on under its own key, as before the re-read, and its rows read
    as this machine's; refusing would let anyone who can write the store
    file stop a running daemon's writes. The next owner's open closes the
    segment, and the rows stay local."""
    keys = tmp_path / "keys"
    elsewhere = tmp_path / "other-keys"
    path = tmp_path / "store" / STORE_FILENAME
    Store.create(path, keys_dir=keys).close()
    writer = Store.open(path, keys_dir=keys, allow_rekey=True)
    try:
        own = writer.key_state.expected
        writer.put_memory(_memory("signed under the first key"))
        Store.open(path, keys_dir=elsewhere, allow_rekey=True).close()
        assert writer.key_fingerprint != own
        after = _memory("signed under this machine's key after the rekey")
        writer.put_memory(after)
        assert writer.key_state.expected == own
        assert writer.provenance_for([after.id]) == {after.id: LOCAL}
    finally:
        writer.close()
    with Store.open(path, keys_dir=keys, allow_rekey=True) as owner:
        assert owner.provenance_for([after.id]) == {after.id: LOCAL}
