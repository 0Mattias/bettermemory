"""The key ring's binding to one store file, and a key ring that cannot
lose what it was given.

The key ring, the head checkpoint and the origin anchor are found by the
store id, a value the store file carries. A second file with that id (a
copy of the store in another directory, or a repository's store made to
borrow a known id) shared all three: the owner's opener retired the first
store's key for it, wrote a new one, moved the shared forward-only head
past the first store's log, or signed with the first store's key when
the copy's meta named it. `<store_id>.path` now names the one store file
the ring belongs to. What is pinned here:

1. A writing open binds the ring (at create, and on a ring from before
   bindings existed, trust on first use); a reader never does.
2. While the bound file still holds the store id, the owner's open of
   another file with that id writes nothing anywhere, opens for reading
   only (even when its key matches), and says so with both paths and the
   remedy. Its worker connections refuse writes too.
3. A bound file that was moved, deleted or replaced by another store is
   no claim: the ring is bound again and the open is an ordinary one. The
   same file under another name (a hard link, a case spelling) is the
   same store.
4. A key file that is there and cannot be read is never a lost key: the
   owner's open fails, naming it, and nothing is rekeyed; a reader opens
   without it; the reports that look at the key say what is wrong.
5. The origin anchor never moves after it is written. An anchored row
   whose key was lost reads `unverifiable`, not tampered, and the
   segments fall back to the rule without an anchor; a row rewritten
   under its own MAC while its key is still held stays a mismatch.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import backup, store_schema
from bettermemory import log as chain
from bettermemory import store as store_module
from bettermemory._fsutil import sqlite_file_uri
from bettermemory.backup import create_backup, restore_bundle, verify_bundle
from bettermemory.log import HistoryOrigin, KeyRing, KeyUnreadableError
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


def _edit(path: Path, sql: str, *params: Any) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _binding(keys: Path, store_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        KeyRing(keys, store_id).binding_path().read_text(encoding="utf-8")
    )
    return loaded


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    """Every file of the ring but the binding and the locks, by name."""
    return {
        path.name: path.read_bytes()
        for path in sorted(keys.glob(f"{store_id}.*"))
        if path.is_file()
        and not path.name.endswith(".lock")
        and not path.name.endswith(".path")
    }


def _copy(source: Path, target: Path) -> Path:
    """Copy a closed store file to ``target`` (a store file path)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def home(tmp_path: Path, keys: Path) -> tuple[Path, Memory, str]:
    """A store this host created and wrote one memory to, closed (its WAL
    checkpointed into the file): its file, the memory and the store id."""
    path = tmp_path / "home" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as created:
        genuine = _memory("the staging database listens on 5432")
        created.put_memory(genuine)
        store_id = created.store_id
    return path, genuine, store_id


# ---------------------------------------------------------------------------
# 1. Who binds
# ---------------------------------------------------------------------------


def test_create_binds_the_key_ring_to_its_store_file(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, store_id = home
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


@POSIX_ONLY
def test_the_binding_file_is_owner_only(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    _path, _genuine, store_id = home
    mode = KeyRing(keys, store_id).binding_path().stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_writing_open_binds_a_ring_from_before_bindings_and_a_reader_never_does(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    path, genuine, store_id = home
    ring = KeyRing(keys, store_id)
    ring.binding_path().unlink()
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id]) == {genuine.id: LOCAL}
    assert not ring.binding_path().exists()
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


def test_a_binding_that_does_not_read_is_replaced_by_the_next_writing_open(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, store_id = home
    ring = KeyRing(keys, store_id)
    for garbage in (
        "not json",
        "[]",
        '{"path": 7}',
        '{"path": ""}',
        json.dumps({"path": STORE_FILENAME}),
    ):
        ring.binding_path().write_text(garbage, encoding="utf-8")
        assert ring.read_binding() is None
        Store.open_or_create(path, keys_dir=keys).close()
        assert _binding(keys, store_id) == {"path": str(path.resolve())}
    ring.binding_path().write_bytes(b'{"path": "/\xff"}')
    assert ring.read_binding() is None
    Store.open_or_create(path, keys_dir=keys).close()
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


# ---------------------------------------------------------------------------
# 2. A second file with the id, while the bound one holds it
# ---------------------------------------------------------------------------


def test_a_copy_beside_the_bound_store_opens_for_reading_only(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A copy carries the store's key fingerprint, so before the binding
    the owner's opener signed its writes with the home store's key and
    moved the home store's head past the home log."""
    path, genuine, store_id = home
    copy = _copy(path, tmp_path / "repo" / ".claude-memory" / STORE_FILENAME)
    ring_before = _ring_files(keys, store_id)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(copy, keys_dir=keys) as twin:
            assert twin.has_key is False
            assert twin.can_verify is True
            assert twin.provenance_for([genuine.id]) == {genuine.id: LOCAL}
            rows = len(twin.log_rows())
            with pytest.raises(RuntimeError, match="open for reading only") as refused:
                twin.put_memory(_memory("written into the copy"))
            worker = twin.reopen()
            try:
                assert worker.has_key is False
                with pytest.raises(RuntimeError, match="belongs to the store at"):
                    worker.put_memory(_memory("written by a worker"))
            finally:
                worker.close()
            assert len(twin.log_rows()) == rows
    assert str(path.resolve()) in str(refused.value)
    assert str(KeyRing(keys, store_id).binding_path()) in str(refused.value)
    assert str(copy) in caplog.text
    assert str(path.resolve()) in caplog.text
    assert f"{store_id}.path" in caplog.text
    assert _ring_files(keys, store_id) == ring_before
    assert _binding(keys, store_id) == {"path": str(path.resolve())}
    with Store.open_or_create(path, keys_dir=keys) as owner:
        owner.put_memory(_memory("the home store still writes"))
        assert owner.log_verify()["status"] == "ok"


def test_a_store_borrowing_a_known_id_retires_nothing_and_moves_no_head(
    home: tuple[Path, Memory, str], keys: Path, tmp_path: Path
) -> None:
    """The audit's recipe: a repository's store with the home store's id, a
    key of its own and a far higher seq. Opening it retired the home key,
    wrote a new one and pushed the shared head to the borrowed log's end,
    so the home store read `tail_deleted` for good."""
    path, genuine, store_id = home
    borrowed = tmp_path / "repo" / ".claude-memory" / STORE_FILENAME
    with Store.create(borrowed, keys_dir=tmp_path / "attacker-keys") as made:
        planted = _memory("the production database password is in the wiki")
        made.put_memory(planted)
        borrowed_fingerprint = made.key_fingerprint
    _edit(borrowed, "UPDATE meta SET value = ? WHERE key = 'store_id'", store_id)
    _edit(borrowed, "UPDATE log SET seq = seq + 999990")
    ring_before = _ring_files(keys, store_id)
    with Store.open_or_create(borrowed, keys_dir=keys) as opened:
        assert opened.has_key is False
        assert opened.meta()["key_fingerprint"] == borrowed_fingerprint
        assert [r.kind for r in opened.log_rows()].count(chain.REKEY) == 0
        assert opened.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}
    assert _ring_files(keys, store_id) == ring_before
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
        assert owner.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        report = owner.log_verify()
    assert report["status"] == "ok"
    assert report["head"]["status"] == "ok"


def test_the_bound_store_is_seen_while_its_meta_is_only_in_its_wal(
    tmp_path: Path, keys: Path
) -> None:
    """A store a running daemon just created has its tables in its WAL;
    an immutable read of the file alone finds no meta and would read the
    bound store as gone, binding the ring to the borrower."""
    path = tmp_path / "home" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as live:
        store_id = live.store_id
        borrowed = tmp_path / "repo" / STORE_FILENAME
        with Store.create(borrowed, keys_dir=tmp_path / "attacker-keys"):
            pass
        _edit(borrowed, "UPDATE meta SET value = ? WHERE key = 'store_id'", store_id)
        ring_before = _ring_files(keys, store_id)
        with Store.open_or_create(borrowed, keys_dir=keys) as opened:
            assert opened.has_key is False
        assert _ring_files(keys, store_id) == ring_before
        assert _binding(keys, store_id) == {"path": str(path.resolve())}
        live.put_memory(_memory("the live store keeps writing"))
        assert live.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# 3. A bound file that no longer holds the store, and the same file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fate", ["moved", "deleted", "replaced"])
def test_a_bound_store_that_is_gone_releases_the_ring(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    fate: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, genuine, store_id = home
    target = tmp_path / "elsewhere" / STORE_FILENAME
    _copy(path, target)
    if fate == "moved":
        path.unlink()
    elif fate == "deleted":
        shutil.rmtree(path.parent)
    else:
        path.unlink()
        Store.create(path, keys_dir=tmp_path / "other-keys").close()
    with caplog.at_level(logging.INFO, logger="bettermemory.store"):
        with Store.open_or_create(target, keys_dir=keys) as moved:
            assert moved.has_key
            moved.put_memory(_memory("written where the store lives now"))
            assert moved.provenance_for([genuine.id]) == {genuine.id: LOCAL}
            assert moved.log_verify()["status"] == "ok"
    assert _binding(keys, store_id) == {"path": str(target.resolve())}
    assert "no longer holds" in caplog.text


def test_a_hard_link_to_the_bound_store_is_the_same_store(
    home: tuple[Path, Memory, str], keys: Path, tmp_path: Path
) -> None:
    path, _genuine, store_id = home
    linked = tmp_path / "linked" / STORE_FILENAME
    linked.parent.mkdir()
    try:
        os.link(path, linked)
    except OSError as exc:
        pytest.skip(f"no hard links here: {exc}")
    with Store.open_or_create(linked, keys_dir=keys) as same:
        assert same.has_key
        same.put_memory(_memory("written through the link"))
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


def test_another_spelling_of_the_bound_path_is_the_same_store(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, store_id = home
    spelled = path.parent.parent / path.parent.name.upper() / STORE_FILENAME
    if not spelled.exists():
        pytest.skip("this filesystem tells the case of a name apart")
    with Store.open_or_create(spelled, keys_dir=keys) as same:
        assert same.has_key
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


def test_a_restore_onto_its_own_path_keeps_the_ring_bound(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore moves the store aside under another name and puts the
    bundle's copy at the bound path: the same id at the same path."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        created.put_memory(_memory("before the backup"))
        store_id = created.store_id
    bundle = Path(create_backup(path)["bundle"])
    with Store.open_or_create(path, keys_dir=keys_dir) as store:
        store.put_memory(_memory("after the backup"))
    report = restore_bundle(bundle, path, apply=True)
    assert report["status"] == "restored", report["detail"]
    with Store.open_or_create(path, keys_dir=keys_dir) as restored:
        assert restored.has_key
        restored.put_memory(_memory("after the restore"))
        assert restored.log_verify()["status"] == "ok"
    assert _binding(keys_dir, store_id) == {"path": str(path.resolve())}


def test_a_key_ring_that_cannot_be_bound_never_fails_an_open(
    home: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _genuine, _store_id = home

    def refused(self: KeyRing, store_file: Path, **_: Any) -> chain.StoreBinding:
        raise PermissionError("read-only key directory")

    monkeypatch.setattr(KeyRing, "bind_store", refused)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys) as owner:
            assert owner.has_key
    assert "could not be bound" in caplog.text


def test_the_bound_store_id_is_read_without_writing_to_the_file(
    home: tuple[Path, Memory, str], tmp_path: Path
) -> None:
    path, _genuine, store_id = home
    before = sorted(p.name for p in path.parent.iterdir())
    assert store_module._store_id_at(path) == store_id
    assert sorted(p.name for p in path.parent.iterdir()) == before
    assert store_module._store_id_at(tmp_path / "nothing" / STORE_FILENAME) is None
    junk = tmp_path / "junk" / STORE_FILENAME
    junk.parent.mkdir()
    junk.write_bytes(b"not a database")
    assert store_module._store_id_at(junk) is None


# ---------------------------------------------------------------------------
# 4. A key file that cannot be read
# ---------------------------------------------------------------------------


def _load_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`KeyRing.load_current` as it answers a key file that is there and
    cannot be read, for the callers that only report on the key."""

    def load_current(self: KeyRing) -> bytes | None:
        raise KeyUnreadableError(
            self.current_path(), PermissionError(13, "Permission denied")
        )

    monkeypatch.setattr(KeyRing, "load_current", load_current)


def _unreadable(monkeypatch: pytest.MonkeyPatch, key_path: Path) -> None:
    """Make reading the key file fail as a permission does, once it is
    there: the transient failure a lost key must never be mistaken for."""
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == key_path:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_load_current_tells_a_missing_key_from_an_unreadable_one(
    tmp_path: Path,
) -> None:
    ring = KeyRing(tmp_path / "keys", "ab" * 16)
    assert ring.load_current() is None
    ring.ensure_dir()
    ring.current_path().mkdir()
    with pytest.raises(KeyUnreadableError) as raised:
        ring.load_current()
    assert raised.value.path == ring.current_path()
    assert str(ring.current_path()) in str(raised.value)
    assert isinstance(raised.value, OSError)
    assert ring.held_keys() == []
    with pytest.raises(KeyUnreadableError):
        ring.retire_current()


def test_an_unreadable_key_fails_the_owners_open_and_rekeys_nothing(
    home: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, genuine, store_id = home
    ring = KeyRing(keys, store_id)
    ring_before = _ring_files(keys, store_id)
    with monkeypatch.context() as patched:
        _unreadable(patched, ring.current_path())
        with pytest.raises(KeyUnreadableError, match="cannot be read"):
            Store.open_or_create(path, keys_dir=keys)
    assert _ring_files(keys, store_id) == ring_before
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
        assert all(row.kind != chain.REKEY for row in owner.log_rows())
        assert owner.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        assert owner.log_verify()["status"] == "ok"


def test_a_reader_opens_without_an_unreadable_key(
    home: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _genuine, store_id = home
    _unreadable(monkeypatch, KeyRing(keys, store_id).current_path())
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
            assert reader.has_key is False
    assert "opens without its key" in caplog.text


def test_doctor_names_an_unreadable_key(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import doctor

    directory = tmp_path / "store"
    with Store.create(directory / STORE_FILENAME, keys_dir=keys_dir) as created:
        store_id = created.store_id
    _unreadable(monkeypatch, KeyRing(keys_dir, store_id).current_path())
    checks = {
        check.id: check
        for check in doctor.check_store(doctor._Context(cwd=None, directory=directory))
    }
    assert checks["store"].status == doctor.OK
    assert checks["key"].status == doctor.FAIL
    assert "cannot be read" in checks["key"].summary


def test_posture_names_an_unreadable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import posture

    keys = tmp_path / "keys"
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as created:
        meta = created.meta()
    ring = KeyRing(keys, meta["store_id"])
    _unreadable(monkeypatch, ring.current_path())
    finding = posture._key(path, keys, meta, None, ring)
    assert finding.id == "key.match"
    assert finding.status == "high"
    assert "cannot read it" in finding.summary


def test_a_restore_over_an_unreadable_key_is_refused_before_anything_changes(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        store_id = created.store_id
    bundle = Path(create_backup(path)["bundle"])
    ring_before = _ring_files(keys_dir, store_id)
    store_before = path.read_bytes()
    _load_fails(monkeypatch)
    report = restore_bundle(bundle, path, apply=True)
    assert report["status"] == "refused"
    assert "cannot be read" in report["detail"]
    assert report["moved_aside"] == []
    assert _ring_files(keys_dir, store_id) == ring_before
    assert path.read_bytes() == store_before


def test_a_self_signed_refusal_names_an_unreadable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = tmp_path / "keys"
    store_id = "cd" * 16
    _load_fails(monkeypatch)
    text = backup._self_signed_refusal(
        {"store_id": store_id, "problems": [], "log": None}, keys
    )
    assert "cannot be read" in text


# ---------------------------------------------------------------------------
# 5. The origin anchor never moves
# ---------------------------------------------------------------------------


def test_a_lost_key_reads_unverifiable_and_falls_back_to_the_rule_without_an_anchor(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    """With the anchor's key lost, the split is the one without an anchor:
    the lost key's history leads (its labels stand), and a forged segment
    after the new key's is still foreign."""
    path, genuine, store_id = home
    ring = KeyRing(keys, store_id)
    anchored = ring.read_origin()
    ring.current_path().unlink()
    with Store.open_or_create(path, keys_dir=keys) as owner:
        written = _memory("written under the new key")
        owner.put_memory(written)
        new_fingerprint = owner.key_fingerprint
    forged_key = b"f" * chain.KEY_BYTES
    planted = _memory("planted after the new key's rows")
    conn = sqlite3.connect(str(path))
    try:
        chain.append_row(
            conn,
            key=forged_key,
            kind=chain.REKEY,
            payload={
                "old_fingerprint": new_fingerprint,
                "new_fingerprint": chain.fingerprint(forged_key),
            },
        )
        payload: dict[str, Any] = {
            "memory": planted.model_dump(mode="json"),
            "provenance": LOCAL,
            "filename": None,
        }
        row = chain.append_row(conn, key=forged_key, kind="memory_put", payload=payload)
        store_module._apply_mutation(conn, "memory_put", payload, log_mac=row.mac)
        conn.commit()
    finally:
        conn.close()
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.provenance_for([genuine.id, written.id, planted.id]) == {
            genuine.id: LOCAL,
            written.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = reader.log_verify()
    assert report["origin"] == {"status": "unverifiable", "seq": 1}
    found = {p["problem"] for p in report["problems"]}
    assert chain.FOREIGN_SEGMENT in found
    assert chain.ORIGIN_MISMATCH not in found
    assert ring.read_origin() == anchored


def test_a_row_rewritten_under_its_own_mac_while_its_key_is_held_is_a_mismatch(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, _store_id = home
    _edit(path, "UPDATE log SET ts = ts || ' ' WHERE seq = 1")
    with Store.open(path, keys_dir=keys) as reader:
        report = reader.log_verify()
    assert report["origin"] == {"status": "mismatch", "seq": 1, "reason": "signature"}
    assert report["status"] == "tampered"


def test_an_anchor_from_before_anchors_named_their_key(
    home: tuple[Path, Memory, str], keys: Path
) -> None:
    """An rc12 anchor names no key. A row no held key verifies then reads as
    it did (a mismatch), a reader leaves the anchor as it is, and the first
    writing open that sees the row verify names the key without moving the
    anchor; from then on a lost key reads `unverifiable`."""
    path, _genuine, store_id = home
    ring = KeyRing(keys, store_id)
    named = ring.read_origin()
    assert named is not None and named.key_fingerprint is not None
    legacy = {"seq": named.seq, "mac": named.mac}
    ring.origin_path().write_text(json.dumps(legacy), encoding="utf-8")

    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.log_verify()["origin"] == {"status": "ok", "seq": 1}
    assert json.loads(ring.origin_path().read_text(encoding="utf-8")) == legacy

    lost = ring.current_path().read_bytes()
    stand_in = KeyRing(keys, store_id)
    stand_in.retired_path("0" * 64).write_bytes(b"s" * chain.KEY_BYTES)
    ring.current_path().unlink()
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        origin = chain.check_origin(reader.conn, keyring=ring)
    assert origin.to_dict() == {"status": "mismatch", "seq": 1, "reason": "signature"}

    ring.current_path().write_bytes(lost)
    stand_in.retired_path("0" * 64).unlink()
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
    assert json.loads(ring.origin_path().read_text(encoding="utf-8")) == {
        **legacy,
        "key_fingerprint": named.key_fingerprint,
    }
    ring.current_path().unlink()
    ring.retired_path("1" * 64).write_bytes(b"s" * chain.KEY_BYTES)
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        origin = chain.check_origin(reader.conn, keyring=ring)
    assert origin.to_dict() == {"status": "unverifiable", "seq": 1}


@pytest.mark.parametrize("why", ["rewritten", "unwritable", "unreadable"])
def test_an_anchor_whose_key_cannot_be_named_stays_as_it_was(
    home: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    why: str,
) -> None:
    """Naming the key never fails the open: a row no held key verifies
    names nothing (it is the mismatch it was), and a ring or a log that
    cannot be read or written is said in the log."""
    path, _genuine, store_id = home
    ring = KeyRing(keys, store_id)
    named = ring.read_origin()
    assert named is not None
    legacy = {"seq": named.seq, "mac": named.mac}
    ring.origin_path().write_text(json.dumps(legacy), encoding="utf-8")
    if why == "rewritten":
        _edit(path, "UPDATE log SET ts = ts || ' ' WHERE seq = 1")
    elif why == "unwritable":

        def refused(self: KeyRing, origin: HistoryOrigin, key_fingerprint: str) -> bool:
            raise PermissionError("read-only key directory")

        monkeypatch.setattr(KeyRing, "name_origin_key", refused)
    else:

        def unreadable(*args: Any, **kwargs: Any) -> chain.OriginCheck:
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(store_module, "check_origin", unreadable)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys) as owner:
            assert owner.has_key
    assert json.loads(ring.origin_path().read_text(encoding="utf-8")) == legacy
    if why != "rewritten":
        assert "the origin anchor's key not named" in caplog.text


def test_naming_the_key_never_moves_or_rewrites_another_anchor(
    tmp_path: Path,
) -> None:
    ring = KeyRing(tmp_path / "keys", "ef" * 16)
    origin = HistoryOrigin(3, "1" * 64)
    assert ring.name_origin_key(origin, "2" * 64) is False
    assert ring.write_origin(seq=3, mac="1" * 64) is True
    assert ring.name_origin_key(HistoryOrigin(4, "1" * 64), "2" * 64) is False
    assert ring.read_origin() == origin
    assert ring.name_origin_key(origin, "2" * 64) is True
    named = ring.read_origin()
    assert named is not None and named.key_fingerprint == "2" * 64
    assert ring.name_origin_key(origin, "3" * 64) is False
    named = ring.read_origin()
    assert named is not None and named.key_fingerprint == "2" * 64


@pytest.mark.parametrize(
    ("raw", "named"),
    [
        ({"seq": 1, "mac": "1" * 64, "key_fingerprint": "2" * 64}, "2" * 64),
        ({"seq": 1, "mac": "1" * 64, "key_fingerprint": "2" * 63}, None),
        ({"seq": 1, "mac": "1" * 64, "key_fingerprint": 7}, None),
        ({"seq": 1, "mac": "1" * 64}, None),
    ],
)
def test_an_anchor_reads_its_key_only_in_the_shape_a_fingerprint_has(
    raw: dict[str, Any], named: str | None
) -> None:
    origin = chain.parse_origin(raw)
    assert origin is not None
    assert origin.key_fingerprint == named
    assert origin == HistoryOrigin(1, "1" * 64)


def test_a_bundle_of_a_store_whose_anchor_key_was_lost_is_not_tampered(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        created.put_memory(_memory("written under the key that is lost"))
        store_id = created.store_id
        lost = created.key_fingerprint
    KeyRing(keys_dir, store_id).current_path().unlink()
    with Store.open_or_create(path, keys_dir=keys_dir) as owner:
        owner.put_memory(_memory("written under the new key"))
    result = create_backup(path)
    assert result["manifest"]["origin"]["key_fingerprint"] == lost
    report = verify_bundle(Path(result["bundle"]))
    assert report["log"]["origin"] == {"status": "unverifiable", "seq": 1}
    assert report["log"]["status"] == "unverifiable"


# ---------------------------------------------------------------------------
# 6. What a failure that passes in a moment must not decide
# ---------------------------------------------------------------------------


def _no_permission_bits() -> None:
    """Skip where a mode of 0 does not stop this account reading."""
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symbolic links here: {exc}")


@pytest.mark.parametrize("what", ["key", "directory"])
def test_a_key_linked_to_a_volume_not_mounted_is_unreadable_not_missing(
    tmp_path: Path, what: str
) -> None:
    """A key file, or the key directory, kept on another volume through a
    symbolic link reads as not found while that volume is not mounted.
    Read as a missing key, the owner's opener wrote a new key over the
    link and rekeyed the log for good."""
    from bettermemory import posture

    volume = tmp_path / "volume"
    keys = tmp_path / "keys"
    path = tmp_path / "home" / STORE_FILENAME
    with Store.create(path, keys_dir=volume) as created:
        created.put_memory(_memory("written under the key on the volume"))
        store_id = created.store_id
        meta = created.meta()
    ring = KeyRing(keys, store_id)
    if what == "key":
        keys.mkdir()
        for name in os.listdir(volume):
            if name != f"{store_id}.key":
                shutil.copyfile(volume / name, keys / name)
        _symlink(ring.current_path(), volume / f"{store_id}.key", directory=False)
    else:
        _symlink(keys, volume, directory=True)
    with Store.open_or_create(path, keys_dir=keys) as mounted:
        assert mounted.has_key
    unmounted = tmp_path / "unmounted"
    volume.rename(unmounted)
    with pytest.raises(KeyUnreadableError, match="symbolic link") as raised:
        Store.open_or_create(path, keys_dir=keys)
    assert raised.value.path == ring.current_path()
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        assert reader.has_key is False
    finding = posture._key(path, keys, meta, None, ring)
    assert finding.status == "high"
    assert "cannot read it" in finding.summary
    assert "nothing is rekeyed" in finding.summary
    unmounted.rename(volume)
    assert (keys if what == "directory" else ring.current_path()).is_symlink()
    with Store.open_or_create(path, keys_dir=keys) as back:
        assert back.has_key
        assert all(row.kind != chain.REKEY for row in back.log_rows())
        assert back.log_verify()["status"] == "ok"


def test_a_readers_open_of_a_second_file_signs_nothing_with_the_bound_key(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reader's open whose key matches can write (the session-start hook
    records the brief it shows). On a copy of the bound store it signed
    with that store's key and moved its head, so the bound store read
    `tail_deleted` for good. It reads the binding, and writes none."""
    path, genuine, store_id = home
    copy = _copy(path, tmp_path / "repo" / ".claude-memory" / STORE_FILENAME)
    ring_before = _ring_files(keys, store_id)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open(copy, keys_dir=keys, allow_rekey=False) as twin:
            assert twin.has_key is False
            assert twin.can_verify is True
            assert twin.provenance_for([genuine.id]) == {genuine.id: LOCAL}
            with pytest.raises(RuntimeError, match="belongs to the store at"):
                twin.put_memory(_memory("recorded into the copy"))
    assert str(path.resolve()) in caplog.text
    assert _ring_files(keys, store_id) == ring_before
    assert _binding(keys, store_id) == {"path": str(path.resolve())}
    with Store.open(path, keys_dir=keys, allow_rekey=False) as bound:
        assert bound.has_key
        bound.put_memory(_memory("the bound store's reader still writes"))
        assert bound.log_verify()["status"] == "ok"

    # A ring from before bindings is no claim, and a reader writes none.
    KeyRing(keys, store_id).binding_path().unlink()
    with Store.open(copy, keys_dir=keys, allow_rekey=False) as twin:
        assert twin.has_key
    assert not KeyRing(keys, store_id).binding_path().exists()


def test_a_bound_store_that_cannot_be_read_keeps_the_ring(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bound store this account cannot read for a moment (a directory
    that cannot be searched, a network home) says nothing about whether it
    still holds the store. Read as gone, the ring was bound to the copy,
    which then signed for it and moved its head, and the bound store
    opened for reading only once it was back."""
    _no_permission_bits()
    path, genuine, store_id = home
    copy = _copy(path, tmp_path / "repo" / STORE_FILENAME)
    ring_before = _ring_files(keys, store_id)
    os.chmod(path.parent, 0)
    try:
        with pytest.raises(OSError):
            store_module._store_id_at(path)
        with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
            with Store.open_or_create(copy, keys_dir=keys) as twin:
                assert twin.has_key is False
    finally:
        os.chmod(path.parent, 0o700)
    assert "could not be read to tell" in caplog.text
    assert _binding(keys, store_id) == {"path": str(path.resolve())}
    assert _ring_files(keys, store_id) == ring_before
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.has_key
        owner.put_memory(_memory("the bound store writes once it is back"))
        assert owner.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        assert owner.log_verify()["status"] == "ok"


def test_a_bound_store_sqlite_cannot_open_by_its_uri_keeps_the_ring(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLite refuses an open whose URI it will not take with SQLITE_ERROR,
    the code a database with no `meta` table gives its query. `Path.as_uri`
    wrote such a URI for a store on a Windows network share (its server as
    the authority) before `_fsutil.sqlite_file_uri`; read as no store
    there, the bound store released the ring to every second file, which
    then signed with its key and moved its head. The same refusal is made
    here on any host: the URI the id read builds is given a server."""
    path, _genuine, store_id = home
    copy = _copy(path, tmp_path / "repo" / STORE_FILENAME)
    ring_before = _ring_files(keys, store_id)
    real_connect = sqlite3.connect

    def unc_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if (
            kwargs.get("uri")
            and isinstance(database, str)
            and database.startswith(sqlite_file_uri(path.resolve()))
        ):
            database = database.replace("file:///", "file://server/", 1)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", unc_connect)
    with pytest.raises(OSError, match="invalid uri authority"):
        store_module._store_id_at(path)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(copy, keys_dir=keys) as twin:
            assert twin.has_key is False
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert "could not be read to tell" in caplog.text
    assert _binding(keys, store_id) == {"path": str(path.resolve())}
    assert _ring_files(keys, store_id) == ring_before


def test_the_bound_store_id_read_answers_what_a_file_is(tmp_path: Path) -> None:
    """A file that is no store is an answer (the binding may move); one
    that cannot be opened is not."""
    empty = tmp_path / "empty" / STORE_FILENAME
    empty.parent.mkdir()
    empty.write_bytes(b"")
    assert store_module._store_id_at(empty) is None
    no_meta = tmp_path / "no-meta" / STORE_FILENAME
    no_meta.parent.mkdir()
    conn = sqlite3.connect(str(no_meta))
    try:
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
    finally:
        conn.close()
    assert store_module._store_id_at(no_meta) is None
    assert store_module._store_id_at(tmp_path) is None
    loop = tmp_path / "loop" / STORE_FILENAME
    loop.parent.mkdir()
    try:
        loop.symlink_to(loop)
    except (OSError, NotImplementedError):
        pass
    else:
        assert store_module._store_id_at(loop) is None
    _no_permission_bits()
    locked = tmp_path / "locked" / STORE_FILENAME
    with Store.create(locked, keys_dir=tmp_path / "keys"):
        pass
    os.chmod(locked, 0)
    try:
        with pytest.raises(OSError, match="could not be read"):
            store_module._store_id_at(locked)
    finally:
        os.chmod(locked, 0o600)


def test_a_windows_reparse_cycle_reads_as_no_file_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows reports a symbolic link that loops as
    ERROR_CANT_RESOLVE_FILENAME (1921), not ELOOP: it is no file there,
    as the loop is on POSIX; any other error still cannot tell."""
    bound = tmp_path / "loop" / STORE_FILENAME
    real_stat = Path.stat
    code = 1921

    def windows_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self == bound:
            exc = OSError(22, "The name of the file cannot be resolved by the system")
            exc.winerror = code  # type: ignore[attr-defined,unused-ignore]
            raise exc
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", windows_stat)
    assert store_module._store_id_at(bound) is None
    code = 21  # ERROR_NOT_READY: a volume not there now
    with pytest.raises(OSError):
        store_module._store_id_at(bound)


def test_an_anchor_that_cannot_be_read_is_never_written_over(
    home: tuple[Path, Memory, str],
    keys: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Read as no anchor, an anchor file this account could not read for a
    moment was written over by the next writing open: after a lost key, at
    the rekey row, which is the move an anchor must never make."""
    _no_permission_bits()
    path, _genuine, store_id = home
    ring = KeyRing(keys, store_id)
    anchored = json.loads(ring.origin_path().read_text(encoding="utf-8"))
    ring.current_path().unlink()
    os.chmod(ring.origin_path(), 0)
    try:
        with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
            with Store.open_or_create(path, keys_dir=keys) as owner:
                assert owner.has_key
                assert owner.log_rows()[-1].kind == chain.REKEY
    finally:
        os.chmod(ring.origin_path(), 0o600)
    assert "origin anchor for" in caplog.text
    assert json.loads(ring.origin_path().read_text(encoding="utf-8")) == anchored


def test_a_binding_that_cannot_be_read_is_never_written_over(
    home: tuple[Path, Memory, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Read as no binding, a binding this account cannot read for a moment
    was replaced by the next writing open's own file: a copy's, which the
    bound store then belonged to."""
    _no_permission_bits()
    path, _genuine, store_id = home
    copy = _copy(path, tmp_path / "repo" / STORE_FILENAME)
    binding = KeyRing(keys, store_id).binding_path()
    os.chmod(binding, 0)
    try:
        with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
            Store.open_or_create(copy, keys_dir=keys).close()
    finally:
        os.chmod(binding, 0o600)
    assert "could not be bound" in caplog.text
    assert _binding(keys, store_id) == {"path": str(path.resolve())}


def test_a_restore_binds_the_key_ring_to_the_restored_file(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore makes the ring's keys, head and anchor the restored
    file's. Left bound to the store it was taken from, still there in its
    old directory, the restored store opened for reading only although the
    restore reported it restored."""
    old = tmp_path / "old" / STORE_FILENAME
    with Store.create(old, keys_dir=keys_dir) as created:
        kept = _memory("written before the backup")
        created.put_memory(kept)
        store_id = created.store_id
    bundle = Path(create_backup(old)["bundle"])
    directory = tmp_path / "new"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    planned = restore_bundle(bundle, path, apply=False)
    assert planned["plan"]["binding"] == {
        "before": str(old.resolve()),
        "after": str(path.resolve()),
    }
    report = restore_bundle(bundle, path, apply=True)
    assert report["status"] == "restored", report["detail"]
    assert report["binding"] == planned["plan"]["binding"]
    record = json.loads(Path(report["record"]).read_text(encoding="utf-8"))
    entry = record["restores"][-1]
    assert entry["binding_before"] == str(old.resolve())
    assert entry["binding_after"] == str(path.resolve())
    assert _binding(keys_dir, store_id) == {"path": str(path.resolve())}
    with Store.open_or_create(path, keys_dir=keys_dir) as restored:
        assert restored.has_key
        restored.put_memory(_memory("written after the restore"))
        assert restored.provenance_for([kept.id]) == {kept.id: LOCAL}
        assert restored.log_verify()["status"] == "ok"
    with Store.open_or_create(old, keys_dir=keys_dir) as left:
        assert left.has_key is False


# ---------------------------------------------------------------------------
# The store id a ring holds (ENG-173)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../elsewhere/**/planted",
        "../x",
        "**",
        "a/b",
        "a\\b",
        "",
        "ab" * 15 + "a",
        "AB" * 16,
        "ab" * 16 + "\n",
        "ab" * 17,
    ],
)
def test_a_key_ring_holds_a_store_id_and_nothing_else(tmp_path: Path, bad: str) -> None:
    """Every path a ring reads or writes is the keys directory joined with
    the id, so a ring refuses any id but the 32 lowercase hex digits
    `Store.create` writes: no caller can build one that reads, writes or
    globs outside the keys directory."""
    with pytest.raises(ValueError, match="not a store id"):
        KeyRing(tmp_path / "keys", bad)
    ring = KeyRing(tmp_path / "keys", "0f" * 16)
    assert ring.current_path().parent == tmp_path / "keys"


def test_the_key_ring_and_the_store_agree_on_the_store_id_shape() -> None:
    """`store_schema._STORE_ID_PATTERN` (what `Store.open` refuses a store by) and
    `log.STORE_ID_PATTERN` (what a key ring holds) are one shape. The ring's
    module holds the pattern because `store` imports `log`; until `store`
    reads it from there, this pins the two together."""
    assert chain.STORE_ID_PATTERN.pattern == store_schema._STORE_ID_PATTERN.pattern
    assert chain.STORE_ID_PATTERN.flags == store_schema._STORE_ID_PATTERN.flags
