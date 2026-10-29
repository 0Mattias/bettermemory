"""A key ring from before 9.0.0rc13 binds only to the file that carries its
history (ENG-93 item 6).

A ring from before rc13 has an origin anchor and a head but no binding
(`<store_id>.path`), and the first owner's open after the upgrade bound it
to whichever file it opened. When that was a file that borrowed the
store's id (a repository's `./.claude-memory/` opened before the home
store), it took the ring as in rc12: the home store's key signed the
borrower's rows, the head moved past the home log, which read `tampered`
(`tail_deleted`) from then on, and the home store opened for reading only.

Pinned here: an unbound ring that records a history binds only to the
file whose row at the anchor's seq carries the anchor's MAC and whose log
reaches the head (`store_keys.ring_fit`, the rule `key adopt` binds by).
The genuine file binds at its first owner's open. A file that borrowed
the id, and an older copy of the store, read `unclaimed` for the owner's
opener and for a reader, bind nothing, sign nothing and move nothing, and
the genuine store opens after them with its log intact. A ring that
records no history binds on first use, as before. The genuine store put
back from an older copy is accepted on the record by `log accept-history`.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import key_lifecycle as lifecycle
from bettermemory.log import KeyRing
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store


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


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted(keys.glob(f"{store_id}.*"))
        if not p.name.endswith(".lock")
    }


def _unbind(keys: Path, store_id: str) -> None:
    """The ring as a release before rc13 left it: anchor and head, no
    binding."""
    KeyRing(keys, store_id).binding_path().unlink()


def _home(tmp_path: Path, keys: Path) -> tuple[Path, str]:
    path = (tmp_path / "home").resolve() / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as created:
        created.put_memory(_memory("the staging database listens on 5432"))
        store_id = created.store_id
    return path, store_id


def _older_copy(home: Path, tmp_path: Path, keys: Path) -> Path:
    """A copy of the home store taken before the home store wrote again."""
    copy = (tmp_path / "repo" / ".claude-memory").resolve() / STORE_FILENAME
    copy.parent.mkdir(parents=True)
    shutil.copyfile(home, copy)
    with Store.open(home, keys_dir=keys, allow_rekey=True) as store:
        store.put_memory(_memory("the home store writes after the copy"))
    return copy


def _borrowed(home: Path, store_id: str, tmp_path: Path) -> Path:
    """A store of its own, made under another keys directory, whose meta
    then names the home store's id and key: a file made to borrow it."""
    path = (tmp_path / "repo" / ".claude-memory").resolve() / STORE_FILENAME
    with Store.create(path, keys_dir=tmp_path / "elsewhere-keys") as other:
        other.put_memory(_memory("a repository's own memory"))
    with sqlite3.connect(str(home)) as source:
        (fingerprint,) = source.execute(
            "SELECT value FROM meta WHERE key = 'key_fingerprint'"
        ).fetchone()
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'store_id'", (store_id,)
            )
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                (fingerprint,),
            )
    finally:
        conn.close()
    return path


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    return (tmp_path / "keys").resolve()


def test_the_genuine_file_binds_at_its_first_owners_open(
    tmp_path: Path, keys: Path
) -> None:
    home, store_id = _home(tmp_path, keys)
    _unbind(keys, store_id)
    with Store.open_or_create(home, keys_dir=keys) as store:
        assert store.has_key
        assert store.key_state.state == "present"
        store.put_memory(_memory("written after the upgrade"))
        assert store.log_verify()["status"] == "ok"
    assert KeyRing(keys, store_id).read_binding() == home


@pytest.mark.parametrize("second", ["borrowed", "older_copy"])
def test_a_second_file_does_not_take_an_unbound_ring(
    second: str, tmp_path: Path, keys: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home, store_id = _home(tmp_path, keys)
    if second == "borrowed":
        other = _borrowed(home, store_id, tmp_path)
        why = "carries another MAC than the key ring's origin anchor names"
    else:
        other = _older_copy(home, tmp_path, keys)
        why = "before the key ring's head checkpoint"
    _unbind(keys, store_id)
    ring_before = _ring_files(keys, store_id)

    with Store.open_or_create(other, keys_dir=keys) as owner:
        assert not owner.has_key
        state = owner.key_state
        assert state.state == "unclaimed"
        assert why in state.summary
        assert state.remedy is not None
        assert "bettermemory key adopt --yes" in state.remedy
        assert "accept-history" not in state.remedy
        assert "at seq" in state.remedy
        with pytest.raises(RuntimeError, match="open for reading only"):
            owner.put_memory(_memory("refused"))
    assert "is bound to no store file yet" in caplog.text
    with Store.open(other, keys_dir=keys, allow_rekey=False) as reader:
        assert not reader.has_key
        assert reader.key_state.state == "unclaimed"
    assert _ring_files(keys, store_id) == ring_before

    with Store.open_or_create(home, keys_dir=keys) as genuine:
        assert genuine.has_key
        genuine.put_memory(_memory("the home store writes on"))
        assert genuine.log_verify()["status"] == "ok"
    assert KeyRing(keys, store_id).read_binding() == home
    # Bound to the home store now, the second file reads bound elsewhere.
    with Store.open_or_create(other, keys_dir=keys) as after:
        assert after.key_state.state == "bound_elsewhere"


def test_a_ring_that_records_no_history_binds_on_first_use(
    tmp_path: Path, keys: Path
) -> None:
    """Without an anchor or a head there is nothing to tell a file by: the
    first owner's open binds the ring, as before."""
    home, store_id = _home(tmp_path, keys)
    other = _borrowed(home, store_id, tmp_path)
    ring = KeyRing(keys, store_id)
    for path in (ring.binding_path(), ring.origin_path(), ring.head_path()):
        path.unlink()
    with Store.open_or_create(other, keys_dir=keys) as owner:
        assert owner.key_state.state != "unclaimed"
    assert ring.read_binding() == other


def test_the_genuine_store_put_back_from_an_older_copy_is_accepted(
    tmp_path: Path, keys: Path
) -> None:
    """The store itself, put back from a copy taken before its last
    writes, does not reach the head: unclaimed until `log accept-history
    --yes` accepts it on the record and binds the ring to it."""
    home, store_id = _home(tmp_path, keys)
    kept = tmp_path / "kept.sqlite"
    shutil.copyfile(home, kept)
    with Store.open(home, keys_dir=keys, allow_rekey=True) as store:
        store.put_memory(_memory("written after the copy was kept"))
    _unbind(keys, store_id)
    shutil.copyfile(kept, home)
    with Store.open_or_create(home, keys_dir=keys) as owner:
        assert owner.key_state.state == "unclaimed"
    report = lifecycle.accept_history(home, apply=True, keys_dir=keys)
    assert report["status"] == "done", report
    with Store.open_or_create(home, keys_dir=keys) as store:
        assert store.has_key
        assert store.log_verify()["status"] == "ok"
    assert KeyRing(keys, store_id).read_binding() == home
