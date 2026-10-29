"""`bettermemory log accept-history --yes` (ENG-106).

A store file whose history was replaced reads `tampered` for good: a
file copied over this host's copy again from the machine it came from
(`origin_mismatch`), an origin anchor from before 9.0.0rc13 whose key was
lost (`origin_mismatch`, reason `signature`: nothing tells the loss from
a rewrite), or an older copy put back by hand (head `tail_deleted`). The
recovery was deleting `<store_id>.origin` and `<store_id>.head` by hand.
Pinned here: the command does that on the record (a `history_accepted`
row signed with the store's key, and an entry in the rewind record beside
the head), re-anchors at the first row of the earliest segment a held key
signs, rewinds the head, plans without `--yes`, refuses while a daemon
holds the store or the key cannot be read, changes nothing when there is
nothing to accept, and puts the anchor and head back when it fails.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory import key_lifecycle as lifecycle
from bettermemory import store as store_module
from bettermemory.audit_export import rewinds_across
from bettermemory.cli import main as cli_main
from bettermemory.log import HISTORY_ACCEPTED, HistoryOrigin, KeyRing
from bettermemory.store import STORE_FILENAME, Store

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="symbolic links")


def _write(store: Store, n: int) -> None:
    for i in range(n):
        store.write(content=f"the cache holds {i} entries", scopes=["infra"])


def _verify(path: Path, keys: Path) -> dict[str, Any]:
    with Store.open(path, keys_dir=keys, read_only=True) as store:
        report: dict[str, Any] = store.log_verify()
        return report


def _rows(path: Path, kind: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            json.loads(payload)
            for (payload,) in conn.execute(
                "SELECT payload FROM log WHERE kind = ? ORDER BY seq", (kind,)
            )
        ]
    finally:
        conn.close()


def _ring_state(ring: KeyRing) -> tuple[bytes | None, bytes | None, bytes | None]:
    def read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    return read(ring.origin_path()), read(ring.head_path()), read(ring.binding_path())


def _cli(
    argv: list[str],
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    with pytest.raises(SystemExit) as exc:
        cli_main()
    code = exc.value.code
    return (code if isinstance(code, int) else 1), capsys.readouterr().out


def _copied_over_again(tmp_path: Path, keys_dir: Path) -> tuple[Path, str]:
    """This host's copy of a store from another machine, then the other
    machine's file copied over it again: the anchor this host wrote at its
    takeover now names a row the file does not hold as it was."""
    other_keys = tmp_path / "machine-a-keys"
    source = tmp_path / "machine-a" / STORE_FILENAME
    with Store.create(source, keys_dir=other_keys) as created:
        _write(created, 2)
        store_id = created.store_id
    here = (tmp_path / "here").resolve() / STORE_FILENAME
    here.parent.mkdir()
    shutil.copyfile(source, here)
    with Store.open_or_create(here) as taken_over:
        _write(taken_over, 2)
    with Store.open_or_create(source, keys_dir=other_keys) as again:
        _write(again, 4)
    shutil.copyfile(source, here)
    with Store.open_or_create(here) as reopened:
        assert reopened.has_key
    return here, store_id


def test_a_store_copied_over_again_is_accepted_on_the_record(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    here, store_id = _copied_over_again(tmp_path, keys_dir)
    before = _verify(here, keys_dir)
    assert before["status"] == "tampered"
    assert before["origin"]["status"] == "mismatch"
    ring = KeyRing(keys_dir, store_id)
    origin_before = ring.read_origin()

    code, out = _cli(["log", "accept-history"], here.parent, monkeypatch, capsys)
    assert code == 3
    assert "plan" in out
    assert ring.read_origin() == origin_before

    code, out = _cli(
        ["log", "accept-history", "--yes"], here.parent, monkeypatch, capsys
    )
    assert code == 0, out
    after = _verify(here, keys_dir)
    assert after["status"] != "tampered"
    assert after["origin"]["status"] == "ok"
    assert after["head"]["status"] == "ok"
    [record] = _rows(here, HISTORY_ACCEPTED)
    assert origin_before is not None
    assert record["origin_before"] == origin_before.to_dict()
    assert record["origin_status_before"] == "mismatch"
    assert record["origin_after"] == ring.read_origin().to_dict()  # type: ignore[union-attr]
    assert record["binding_after"] == str(here)
    restores = json.loads(
        (keys_dir / f"{store_id}.backup_restore.json").read_text(encoding="utf-8")
    )["restores"]
    assert restores[-1]["accepted_history"] is True


def test_an_older_copy_put_back_rewinds_the_head_on_the_record(
    tmp_path: Path, keys_dir: Path
) -> None:
    path = (tmp_path / "home").resolve() / STORE_FILENAME
    with Store.create(path) as created:
        _write(created, 2)
        store_id = created.store_id
    kept = tmp_path / "kept.sqlite"
    shutil.copyfile(path, kept)
    with Store.open_or_create(path) as store:
        _write(store, 5)
    ring = KeyRing(keys_dir, store_id)
    head_before = ring.read_head()
    assert head_before is not None
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    shutil.copyfile(kept, path)
    assert _verify(path, keys_dir)["head"]["status"] == "tail_deleted"
    report = lifecycle.accept_history(path, apply=True)
    assert report["status"] == "done", report
    assert report["head"]["before"]["seq"] == head_before.seq
    end = report["log_end_before"]["seq"]
    assert end < head_before.seq
    assert report["log"] == "ok"
    # A cursor an export took past the old copy's end is warned about.
    [rewind] = rewinds_across(keys_dir, store_id, head_before.seq)
    assert (rewind["from_seq"], rewind["to_seq"]) == (head_before.seq, end)


def test_an_rc12_anchor_whose_key_was_lost_is_accepted(
    tmp_path: Path, keys_dir: Path
) -> None:
    """An anchor written before rc13 names no key; once that key is lost,
    a held key's failing to verify the row reads as a rewrite."""
    path = (tmp_path / "home").resolve() / STORE_FILENAME
    with Store.create(path) as created:
        _write(created, 2)
        store_id = created.store_id
    ring = KeyRing(keys_dir, store_id)
    origin = ring.read_origin()
    assert origin is not None
    ring.replace_origin(HistoryOrigin(seq=origin.seq, mac=origin.mac))
    ring.current_path().unlink()
    with Store.open_or_create(path) as rekeyed:
        assert rekeyed.has_key
    before = _verify(path, keys_dir)
    assert before["status"] == "tampered"
    assert before["origin"] == {"status": "mismatch", "seq": 1, "reason": "signature"}
    report = lifecycle.accept_history(path, apply=True)
    assert report["status"] == "done", report
    after = _verify(path, keys_dir)
    assert after["status"] == "unverifiable"
    assert after["origin"]["status"] == "ok"
    new_origin = ring.read_origin()
    assert new_origin is not None and new_origin.seq > 1
    assert new_origin.key_fingerprint is not None


def test_nothing_to_accept_changes_nothing(tmp_path: Path, keys_dir: Path) -> None:
    path = (tmp_path / "home").resolve() / STORE_FILENAME
    with Store.create(path) as created:
        _write(created, 1)
        store_id = created.store_id
    ring = KeyRing(keys_dir, store_id)
    before = _ring_state(ring)
    report = lifecycle.accept_history(path, apply=True)
    assert report["status"] == "unchanged"
    assert "nothing to accept" in report["detail"]
    assert _ring_state(ring) == before
    assert _rows(path, HISTORY_ACCEPTED) == []


def test_accept_history_refuses_while_a_daemon_holds_the_store(
    tmp_path: Path, keys_dir: Path
) -> None:
    here, store_id = _copied_over_again(tmp_path, keys_dir)
    ring = KeyRing(keys_dir, store_id)
    before = _ring_state(ring)
    state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
    state_dir.mkdir(parents=True, exist_ok=True)
    held = _daemon_client.try_lock(_daemon_client.lock_file_for(state_dir, here))
    assert held is not None
    try:
        report = lifecycle.accept_history(here, apply=True)
    finally:
        _daemon_client.release_lock(held)
    assert report["status"] == "refused"
    assert "bettermemory down" in report["detail"]
    assert "accept-history --yes" in report["detail"]
    assert _ring_state(ring) == before


@POSIX_ONLY
def test_accept_history_refuses_a_key_it_cannot_read(
    tmp_path: Path, keys_dir: Path
) -> None:
    here, store_id = _copied_over_again(tmp_path, keys_dir)
    ring = KeyRing(keys_dir, store_id)
    ring.current_path().unlink()
    ring.current_path().symlink_to(tmp_path / "on-a-volume-not-mounted.key")
    before = _ring_state(ring)
    report = lifecycle.accept_history(here, apply=True)
    assert report["status"] == "refused"
    assert str(ring.current_path()) in report["detail"]
    assert _ring_state(ring) == before


def test_a_failed_owners_open_puts_the_anchor_and_head_back(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here, store_id = _copied_over_again(tmp_path, keys_dir)
    ring = KeyRing(keys_dir, store_id)
    before = _ring_state(ring)
    original = store_module.Store.open.__func__  # type: ignore[attr-defined]

    def failing(cls: type[Store], /, path: Any, **kwargs: Any) -> Store:
        if kwargs.get("allow_rekey"):
            raise OSError("the disk went away")
        result: Store = original(cls, path, **kwargs)
        return result

    monkeypatch.setattr(store_module.Store, "open", classmethod(failing))
    report = lifecycle.accept_history(here, apply=True)
    assert report["status"] == "failed"
    assert "put back" in report["detail"]
    assert _ring_state(ring) == before
    assert _rows(here, HISTORY_ACCEPTED) == []
