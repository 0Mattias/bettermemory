"""`bettermemory key show|export|import|adopt|rotate` (ENG-106).

Recovering a store from a lost key, a store copied with its keys
directory, or a key that should be replaced meant placing or deleting
`<store_id>.<prefix>.key`, `.origin`, `.head` and `.path` files by hand.
Pinned here, for each command: what it does to the ring and the store,
the row of its own kind it writes (signed with the store's key), and its
refusals (the wrong store, a key that is not this store's or a file that
does not carry the ring's history, a ring bound elsewhere, a file this
user cannot read, a running daemon), each of which changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory import key_lifecycle as lifecycle
from bettermemory.cli import main as cli_main
from bettermemory.log import KEY_ADOPTED, KEY_IMPORTED, REKEY, KeyRing, fingerprint
from bettermemory.store import STORE_FILENAME, Store

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _store(directory: Path, *, notes: int = 2) -> tuple[Path, str]:
    """A store at ``directory`` (resolved, as the CLI resolves it) with a
    few memories, closed: its file and its id."""
    path = directory.resolve() / STORE_FILENAME
    with Store.create(path) as store:
        for n in range(notes):
            store.write(content=f"the staging port is {5430 + n}", scopes=["infra"])
        return path, store.store_id


def _ring(keys: Path, store_id: str) -> KeyRing:
    return KeyRing(keys, store_id)


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


def _verify(path: Path) -> str:
    with Store.open(path, read_only=True) as store:
        return str(store.log_verify()["status"])


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted(keys.glob(f"{store_id}.*"))
        if p.is_file() and not p.name.endswith(".lock")
    }


def _cli(
    argv: list[str],
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    with pytest.raises(SystemExit) as exc:
        cli_main()
    out = capsys.readouterr()
    code = exc.value.code
    return (code if isinstance(code, int) else 1), out.out, out.err


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_reads_the_key_and_never_prints_one(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, store_id = _store(tmp_path / "home")
    key = _ring(keys_dir, store_id).load_current()
    assert key is not None
    report = lifecycle.describe(path)
    assert report["status"] == "present"
    assert report["keys"] == [{"fingerprint": fingerprint(key), "current": True}]
    assert report["head"]["status"] == "ok"
    assert report["origin"]["status"] == "ok"
    assert report["origin"]["key_fingerprint"] == fingerprint(key)
    assert report["binding"] == {"path": str(path), "this_file": True}
    code, out, _ = _cli(["key", "show"], path.parent, monkeypatch, capsys)
    assert code == 0
    assert "bettermemory key show: present" in out
    assert key.hex() not in out
    code, out, _ = _cli(["key", "show", "--json"], path.parent, monkeypatch, capsys)
    assert key.hex() not in out
    assert json.loads(out)["store_id"] == store_id


def test_show_exits_1_when_the_key_is_not_present(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, store_id = _store(tmp_path / "home")
    _ring(keys_dir, store_id).current_path().unlink()
    code, out, _ = _cli(["key", "show"], path.parent, monkeypatch, capsys)
    assert code == 1
    assert "bettermemory key show: missing" in out
    assert "fix:" in out


def test_show_without_a_store_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "empty").mkdir()
    code, _, err = _cli(["key", "show"], tmp_path / "empty", monkeypatch, capsys)
    assert code == 2
    assert "no bettermemory 9 store" in err


# ---------------------------------------------------------------------------
# export and import
# ---------------------------------------------------------------------------


def test_export_writes_every_held_key_owner_only(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    first = _ring(keys_dir, store_id).load_current()
    assert lifecycle.rotate_key(path, apply=True)["status"] == "done"
    second = _ring(keys_dir, store_id).load_current()
    assert first is not None and second is not None and first != second
    out = tmp_path / "kept" / "keys.json"
    out.parent.mkdir()
    report = lifecycle.export_keys(path, out)
    assert report["status"] == "done"
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["format"] == lifecycle.EXPORT_FORMAT
    assert document["store_id"] == store_id
    exported = {entry["fingerprint"]: entry for entry in document["keys"]}
    assert exported[fingerprint(second)]["current"] is True
    assert exported[fingerprint(first)]["current"] is False
    assert bytes.fromhex(exported[fingerprint(first)]["key"]) == first
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert _rows(path, KEY_IMPORTED) == []


def test_export_refuses_a_path_in_the_keys_directory(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, store_id = _store(tmp_path / "home")
    before = _ring_files(keys_dir, store_id)
    target = keys_dir / "export.json"
    code, _, err = _cli(
        ["key", "export", "--out", str(target)], path.parent, monkeypatch, capsys
    )
    assert code == 2
    assert "keys directory" in err
    assert not target.exists()
    assert _ring_files(keys_dir, store_id) == before


def test_a_lost_key_imports_back_as_the_current_key_without_a_rekey(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    key = ring.load_current()
    assert key is not None
    out = tmp_path / "kept.json"
    assert lifecycle.export_keys(path, out)["status"] == "done"
    ring.current_path().unlink()
    assert lifecycle.describe(path)["status"] == "missing"
    code, text, _ = _cli(["key", "import", str(out)], path.parent, monkeypatch, capsys)
    assert code == 0, text
    assert ring.load_current() == key
    assert _rows(path, REKEY) == []
    [record] = _rows(path, KEY_IMPORTED)
    assert record["installed"] == [{"fingerprint": fingerprint(key), "as": "current"}]
    assert record["source"] == str(out)
    assert _verify(path) == "ok"


def test_a_key_lost_and_rekeyed_imports_back_as_a_retired_key(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The daemon's open rekeyed the store when its key went missing, so the
    rows signed before read unverifiable; the old key file, kept as a keys
    directory holds it, is filed back as a retired key and they verify."""
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    kept = tmp_path / "kept" / f"{store_id}.key"
    kept.parent.mkdir()
    shutil.copyfile(ring.current_path(), kept)
    old = kept.read_bytes()
    ring.current_path().unlink()
    Store.open_or_create(path).close()
    assert _verify(path) == "unverifiable"
    report = lifecycle.import_keys(path, kept)
    assert report["status"] == "done", report
    assert report["keys"] == [{"fingerprint": fingerprint(old), "action": "retired"}]
    assert ring.retired_path(fingerprint(old)).read_bytes() == old
    assert _verify(path) == "ok"
    assert report["log"] == "ok"


def test_import_refuses_another_stores_key(tmp_path: Path, keys_dir: Path) -> None:
    path, store_id = _store(tmp_path / "home")
    other, _ = _store(tmp_path / "other")
    out = tmp_path / "other.json"
    assert lifecycle.export_keys(other, out)["status"] == "done"
    before = _ring_files(keys_dir, store_id)
    report = lifecycle.import_keys(path, out)
    assert report["status"] == "refused"
    assert "import it into that store" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before
    assert _rows(path, KEY_IMPORTED) == []


def test_import_refuses_a_key_that_signs_nothing_here(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    planted = tmp_path / f"{store_id}.key"
    planted.write_bytes(os.urandom(32))
    before = _ring_files(keys_dir, store_id)
    report = lifecycle.import_keys(path, planted)
    assert report["status"] == "refused"
    assert "signs a segment" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before


def test_import_refuses_a_file_it_cannot_read(tmp_path: Path, keys_dir: Path) -> None:
    path, _ = _store(tmp_path / "home")
    report = lifecycle.import_keys(path, tmp_path / "nowhere.json")
    assert report["status"] == "refused"
    assert "cannot be read" in report["detail"]
    assert str(tmp_path / "nowhere.json") in report["detail"]


def test_import_refuses_when_nothing_could_sign_its_record(
    tmp_path: Path, keys_dir: Path
) -> None:
    """A retired key alone, into a ring that lost the store's own key and was
    not rekeyed yet: filing it would be a ring change with no row."""
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    old = ring.load_current()
    assert old is not None
    assert lifecycle.rotate_key(path, apply=True)["status"] == "done"
    kept = tmp_path / f"{store_id}.{fingerprint(old)[:16]}.key"
    kept.write_bytes(old)
    ring.retired_path(fingerprint(old)).unlink()
    ring.current_path().unlink()
    before = _ring_files(keys_dir, store_id)
    report = lifecycle.import_keys(path, kept)
    assert report["status"] == "refused"
    assert "nothing could sign" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before


def test_import_of_keys_held_already_changes_nothing(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    out = tmp_path / "kept.json"
    lifecycle.export_keys(path, out)
    report = lifecycle.import_keys(path, out)
    assert report["status"] == "unchanged"
    assert _rows(path, KEY_IMPORTED) == []


def test_a_retired_copy_of_the_missing_key_imports_back_as_current(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The store's key is missing while the ring holds a retired copy of
    it (what a rotation that failed inside its transaction left before
    9.0.0rc14's fold): the store reads missing and its remedy names `key
    import`, so the import files the key as the current one rather than
    answering that the ring holds it already (cold review M2)."""
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    key = ring.load_current()
    assert key is not None
    out = tmp_path / "kept.json"
    assert lifecycle.export_keys(path, out)["status"] == "done"
    ring.retired_path(fingerprint(key)).write_bytes(key)
    ring.current_path().unlink()
    assert lifecycle.describe(path)["status"] == "missing"
    report = lifecycle.import_keys(path, out)
    assert report["status"] == "done", report
    assert report["keys"] == [{"fingerprint": fingerprint(key), "action": "current"}]
    assert ring.load_current() == key
    assert _rows(path, REKEY) == []
    assert _verify(path) == "ok"


# ---------------------------------------------------------------------------
# a ring bound elsewhere, and adopt
# ---------------------------------------------------------------------------


def _copy_beside(path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True)
    copy = target_dir.resolve() / STORE_FILENAME
    shutil.copyfile(path, copy)
    return copy


def test_a_ring_bound_elsewhere_refuses_export_import_and_rotate(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    copy = _copy_beside(path, tmp_path / "moved")
    out = tmp_path / "kept.json"
    assert lifecycle.export_keys(path, out)["status"] == "done"
    before = _ring_files(keys_dir, store_id)
    for report in (
        lifecycle.export_keys(copy, tmp_path / "copy.json"),
        lifecycle.import_keys(copy, out),
        lifecycle.rotate_key(copy, apply=True),
    ):
        assert report["status"] == "refused", report
        assert "bound elsewhere" in report["detail"]
        assert "key adopt" in report["detail"] or ".path" in report["detail"]
    assert not (tmp_path / "copy.json").exists()
    assert _ring_files(keys_dir, store_id) == before


def test_adopt_binds_the_ring_to_a_copy_on_the_record(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store copied with its keys directory opens for reading only while
    the ring is bound to its original; `key adopt --yes` binds it to the
    copy, which then writes, and the original reads only."""
    path, store_id = _store(tmp_path / "home")
    copy = _copy_beside(path, tmp_path / "moved")
    with Store.open_or_create(copy) as refused:
        assert refused.has_key is False
    code, out, _ = _cli(["key", "adopt"], copy.parent, monkeypatch, capsys)
    assert code == 3
    assert "plan" in out
    assert _ring(keys_dir, store_id).read_binding() == path
    code, out, _ = _cli(["key", "adopt", "--yes"], copy.parent, monkeypatch, capsys)
    assert code == 0, out
    assert _ring(keys_dir, store_id).read_binding() == copy
    [record] = _rows(copy, KEY_ADOPTED)
    assert record == {
        "binding_before": str(path),
        "binding_after": str(copy),
        "previous_holds_store": True,
    }
    with Store.open_or_create(copy) as adopted:
        assert adopted.has_key
        adopted.write(content="written in the adopted copy", scopes=["infra"])
    with Store.open_or_create(path) as original:
        assert original.has_key is False
    assert _verify(copy) == "ok"


def test_adopt_of_a_ring_bound_here_already_changes_nothing(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, _ = _store(tmp_path / "home")
    report = lifecycle.adopt_ring(path, apply=True)
    assert report["status"] == "unchanged"
    assert _rows(path, KEY_ADOPTED) == []


def test_adopt_refuses_a_file_that_does_not_reach_the_head(
    tmp_path: Path, keys_dir: Path
) -> None:
    """A copy taken before the original wrote on: the ring's head names a
    row the copy does not hold, so its history is not the ring's; that is
    `log accept-history`'s to accept on purpose."""
    path, store_id = _store(tmp_path / "home")
    copy = _copy_beside(path, tmp_path / "moved")
    with Store.open_or_create(path) as original:
        original.write(content="written after the copy was taken", scopes=["infra"])
    before = _ring_files(keys_dir, store_id)
    report = lifecycle.adopt_ring(copy, apply=True)
    assert report["status"] == "refused"
    assert "before the key ring's head" in report["detail"]
    assert "accept-history" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before


def test_adopt_refuses_a_ring_without_the_stores_key(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    copy = _copy_beside(path, tmp_path / "moved")
    _ring(keys_dir, store_id).current_path().unlink()
    report = lifecycle.adopt_ring(copy, apply=True)
    assert report["status"] == "refused"
    assert "key import" in report["detail"]
    assert _ring(keys_dir, store_id).read_binding() == path


def test_adopt_refuses_while_a_daemon_holds_the_store(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    copy = _copy_beside(path, tmp_path / "moved")
    state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
    state_dir.mkdir(parents=True, exist_ok=True)
    held = _daemon_client.try_lock(_daemon_client.lock_file_for(state_dir, path))
    assert held is not None
    try:
        report = lifecycle.adopt_ring(copy, apply=True)
    finally:
        _daemon_client.release_lock(held)
    assert report["status"] == "refused"
    assert "bettermemory down" in report["detail"]
    assert _ring(keys_dir, store_id).read_binding() == path


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def test_rotate_writes_a_new_key_and_keeps_the_old_one(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    old = ring.load_current()
    assert old is not None
    origin = ring.read_origin()
    code, out, _ = _cli(["key", "rotate"], path.parent, monkeypatch, capsys)
    assert code == 3
    assert ring.load_current() == old
    code, out, _ = _cli(["key", "rotate", "--yes"], path.parent, monkeypatch, capsys)
    assert code == 0, out
    new = ring.load_current()
    assert new is not None and new != old
    assert ring.retired_path(fingerprint(old)).read_bytes() == old
    assert ring.read_origin() == origin
    [rekey] = _rows(path, REKEY)
    assert rekey == {
        "old_fingerprint": fingerprint(old),
        "new_fingerprint": fingerprint(new),
        "cause": "rotate",
    }
    with Store.open(path, read_only=True) as store:
        assert store.key_fingerprint == fingerprint(new)
    assert _verify(path) == "ok"


def test_a_connection_open_across_a_rotation_signs_with_the_new_key(
    tmp_path: Path, keys_dir: Path
) -> None:
    """A daemon's connection read its key at open; the rotation commits
    under the write lock, and its next row signs with the new key
    (`store_keys._signing_key`), so the log still verifies."""
    path, store_id = _store(tmp_path / "home")
    with Store.open_or_create(path) as serving:
        assert lifecycle.rotate_key(path, apply=True)["status"] == "done"
        serving.write(content="written after the rotation", scopes=["infra"])
    assert _verify(path) == "ok"


def test_rotate_refuses_a_missing_key(tmp_path: Path, keys_dir: Path) -> None:
    path, store_id = _store(tmp_path / "home")
    _ring(keys_dir, store_id).current_path().unlink()
    report = lifecycle.rotate_key(path, apply=True)
    assert report["status"] == "refused"
    assert "no key for this store" in report["detail"]
    assert _rows(path, REKEY) == []


@POSIX_ONLY
def test_an_unreadable_key_file_is_named_and_nothing_changes(
    tmp_path: Path, keys_dir: Path
) -> None:
    path, store_id = _store(tmp_path / "home")
    ring = _ring(keys_dir, store_id)
    key_file = ring.current_path()
    key_file.unlink()
    key_file.symlink_to(tmp_path / "on-a-volume-not-mounted.key")
    before = _ring_files(keys_dir, store_id)
    for report in (
        lifecycle.export_keys(path, tmp_path / "out.json"),
        lifecycle.rotate_key(path, apply=True),
        lifecycle.adopt_ring(path, apply=True),
    ):
        assert report["status"] == "refused", report
        assert str(key_file) in report["detail"]
        assert "make it readable" in report["detail"]
    assert _ring_files(keys_dir, store_id) == before
