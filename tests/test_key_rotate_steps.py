"""`bettermemory key rotate` at every step: a crash or a failure anywhere
leaves a store that opens and verifies, and an owner's open racing the
rotation never writes a key (cold review H2 and M1, ENG-106).

The rotation retired the current key (renamed it away) before it wrote
the new one, so for that moment the ring had no current key: an owner's
open there read the key missing, wrote a key of its own, and one write
replaced the other, leaving the store signed by a key no disk held. And
its put-back ran inside the transaction, so a failing COMMIT left the ring
on the new key and meta on the old. Now the old key is copied to its
retired name, the new one written as `<store_id>.key.pending`, the
transaction committed, and only then the pending key renamed over the
current one; an open that finds a committed rotation unfinished finishes
the rename (`store_keys.finish_rotation`), and a failure before the commit
takes back both files. The rotation takes the store's daemon and spawn
locks, and is refused while a daemon serves the store.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory import key_lifecycle as lifecycle
from bettermemory.log import REKEY, KeyRing, fingerprint
from bettermemory.store import STORE_FILENAME, Store

from .test_key_lifecycle import _ring_files, _rows, _store, _verify

STEPS = ("retired", "pending", "row", "commit", "rename")


def pending_key_path(ring: KeyRing) -> Path:
    """Where the rotation writes its new key before the commit
    (`store_keys.pending_key_path`), spelled out so this file reads the
    same against a build without it."""
    return ring.keys_dir / f"{ring.store_id}.key.pending"


class _Crash(BaseException):
    """The process dying at a step: nothing after it runs."""


def _inject(
    step: str, monkeypatch: pytest.MonkeyPatch, ring: KeyRing, fault: BaseException
) -> None:
    """Raise ``fault`` at ``step`` of the rotation, once the step's write
    is done (the commit fails as it is made)."""
    if step == "retired":
        real_once = lifecycle.write_key_once

        def once(path: Path, key: bytes) -> bool:
            real_once(path, key)
            raise fault

        monkeypatch.setattr(lifecycle, "write_key_once", once)
    elif step == "pending":
        real_write = lifecycle.atomic_write_bytes

        def write(target: Path, data: bytes, **kwargs: Any) -> None:
            real_write(target, data, **kwargs)
            if Path(target) == pending_key_path(ring):
                raise fault

        monkeypatch.setattr(lifecycle, "atomic_write_bytes", write)
    elif step in ("row", "commit"):
        real_append = Store._append
        armed: list[bool] = []

        def append(self: Store, conn: Any, kind: str, *args: Any, **kwargs: Any) -> Any:
            row = real_append(self, conn, kind, *args, **kwargs)
            if kind == REKEY:
                if step == "row":
                    raise fault
                armed.append(True)
            return row

        real_require = Store._require_open

        def require(self: Store, conn: Any) -> None:
            if armed:
                armed.clear()
                raise fault
            real_require(self, conn)

        monkeypatch.setattr(Store, "_append", append)
        monkeypatch.setattr(Store, "_require_open", require)
    else:

        def finish(*args: Any) -> None:
            raise fault

        monkeypatch.setattr(lifecycle, "finish_rotation", finish)


def _opens_and_verifies(path: Path, ring: KeyRing, old: bytes) -> str:
    """An owner's open after the event: it holds a key the ring holds, the
    key meta names, writes a row, and the log verifies; no key but the old
    one and the rotation's new one is anywhere in the ring. The fingerprint
    meta names."""
    with Store.open_or_create(path) as store:
        assert store.has_key, store.key_state.summary
        named = store.key_fingerprint
        store.write(content="written after the rotation's end", scopes=["infra"])
    current = ring.load_current()
    assert current is not None and fingerprint(current) == named
    held = {fingerprint(key) for key in ring.held_keys()}
    assert fingerprint(old) in held
    assert held <= {fingerprint(old), named}
    with Store.open(path, read_only=True) as reader:
        assert reader.key_state.state == "present"
    assert _verify(path) == "ok"
    return named


@pytest.mark.parametrize("step", STEPS)
def test_a_failure_at_each_step_leaves_the_ring_and_meta_agreeing(
    step: str, tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, store_id = _store(tmp_path / "home")
    ring = KeyRing(keys_dir, store_id)
    old = ring.load_current()
    assert old is not None
    before = _ring_files(keys_dir, store_id)
    with pytest.MonkeyPatch.context() as mp:
        if step == "rename":
            mp.setattr(lifecycle, "finish_rotation", lambda *args: None)
        else:
            _inject(step, mp, ring, RuntimeError("the disk went away"))
        report = lifecycle.rotate_key(path, apply=True)
    if step == "rename":
        assert report["status"] == "done", report
        assert str(pending_key_path(ring)) in report["detail"]
        assert ring.load_current() == old
        named = _opens_and_verifies(path, ring, old)
        assert named == report["new_fingerprint"]
        assert not pending_key_path(ring).exists()
        return
    assert report["status"] == "failed", report
    assert _ring_files(keys_dir, store_id) == before
    assert _rows(path, REKEY) == []
    assert _opens_and_verifies(path, ring, old) == fingerprint(old)


@pytest.mark.parametrize("step", STEPS)
def test_a_crash_at_each_step_leaves_a_store_that_opens_and_verifies(
    step: str, tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing after the step runs, the put-back included (meta cannot be
    read, as for a process that died): the files are as the step left
    them, and the next owner's open settles it."""
    path, store_id = _store(tmp_path / "home")
    ring = KeyRing(keys_dir, store_id)
    old = ring.load_current()
    assert old is not None
    with pytest.MonkeyPatch.context() as mp:
        _inject(step, mp, ring, _Crash())
        mp.setattr(lifecycle, "_meta_fingerprint", lambda store: None)
        with pytest.raises(_Crash):
            lifecycle.rotate_key(path, apply=True)
    named = _opens_and_verifies(path, ring, old)
    if step == "rename":
        assert named != fingerprint(old)
        assert ring.retired_path(fingerprint(old)).read_bytes() == old
        assert not pending_key_path(ring).exists()
        assert len(_rows(path, REKEY)) == 1
    else:
        assert named == fingerprint(old)
        assert _rows(path, REKEY) == []


@pytest.mark.parametrize("window", ["before_commit", "after_commit"])
def test_an_owners_open_racing_the_rotation_never_writes_a_key(
    window: str, tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner's open (a daemon start, a hook) reads the key outside the
    write lock. Inside the rotation's transaction it reads the old key,
    present; between the commit and the rename it finishes the rename. It
    never reads the key missing, so it never writes one, and its row signs
    with the key meta names."""
    path, store_id = _store(tmp_path / "home")
    ring = KeyRing(keys_dir, store_id)
    old = ring.load_current()
    assert old is not None
    created: list[str] = []
    real_create = KeyRing.create_key

    def recording(self: KeyRing) -> bytes:
        created.append(self.store_id)
        return real_create(self)

    monkeypatch.setattr(KeyRing, "create_key", recording)
    errors: list[BaseException] = []

    def racing_open() -> None:
        try:
            with Store.open_or_create(path) as racer:
                assert racer.has_key, racer.key_state.summary
                racer.write(content="written by a racing open", scopes=["infra"])
        except BaseException as exc:
            errors.append(exc)

    racers: list[threading.Thread] = []
    if window == "before_commit":
        real_write = lifecycle.atomic_write_bytes

        def then_race(target: Path, data: bytes, **kwargs: Any) -> None:
            real_write(target, data, **kwargs)
            if Path(target) == pending_key_path(ring):
                racer = threading.Thread(target=racing_open)
                racer.start()
                racers.append(racer)
                time.sleep(0.5)

        monkeypatch.setattr(lifecycle, "atomic_write_bytes", then_race)
    else:
        real_finish = lifecycle.finish_rotation

        def race_then_finish(*args: Any) -> Any:
            racing_open()
            return real_finish(*args)

        monkeypatch.setattr(lifecycle, "finish_rotation", race_then_finish)
    report = lifecycle.rotate_key(path, apply=True)
    for racer in racers:
        racer.join(60)
    assert errors == []
    assert report["status"] == "done", report
    assert report["detail"] is None, report
    assert created == []
    new = ring.load_current()
    assert new is not None and fingerprint(new) == report["new_fingerprint"]
    assert ring.retired_path(fingerprint(old)).read_bytes() == old
    assert not pending_key_path(ring).exists()
    assert _verify(path) == "ok"


def test_rotate_refuses_while_a_daemon_holds_the_store(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    before = _ring_files(keys_dir, store_id)
    state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
    state_dir.mkdir(parents=True, exist_ok=True)
    held = _daemon_client.try_lock(_daemon_client.lock_file_for(state_dir, path))
    assert held is not None
    try:
        report = lifecycle.rotate_key(path, apply=True)
    finally:
        _daemon_client.release_lock(held)
    assert report["status"] == "refused"
    assert "bettermemory down" in report["detail"]
    assert "key rotate --yes" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before
    assert _rows(path, REKEY) == []
    assert path.name == STORE_FILENAME
