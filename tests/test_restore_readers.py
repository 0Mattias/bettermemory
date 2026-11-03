"""Reads of a store hold every restore off for their whole length, and a
writer outside the package is bounded by the signed chain (ENG-208).

9.0.0rc14 refused a read-only command that started while a restore ran,
but one that started before it could read the store file, then the keys,
the head and the anchor beside it, on either side of the restore's swap,
and `doctor`'s store check and `security audit` read the store without
even that check. A read now takes the restore lock shared and holds it
until its store closes (`restore_lock.reading`; on Windows it holds the
store file open instead, which a restore's swap cannot rename), so a
restore waits for it and is refused, and `doctor` and the audit say they
did not check a store a restore holds.

A process that writes the store file through a SQLite connection of its
own takes no lock, and no lock can hold it off. What bounds it is what
bounds any writer without the store's key: a row it appends carries no MAC
the key made, and the restore's own `log verify`, run on the restored
store before it reports, reads the rows it wrote into the restored file
by then.

`backup unlock --yes` takes the lock exclusively, as a restore does, so a
read holding it shared keeps it waiting too; it then says a command is
reading the store, not that the restore whose record it clears runs.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import restore_lock
from bettermemory.backup import create_backup, restore_bundle
from bettermemory.store import STORE_FILENAME, Store

from .test_restore_lock import (
    _CHILD_SECONDS,
    _aside,
    _put,
    _restore_paused_after_the_swap,
    _run_cli,
)


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    Store.create(directory / STORE_FILENAME, keys_dir=keys_dir).close()
    path = directory / STORE_FILENAME
    _put(path, keys_dir, "alpha", "beta")
    return path


@pytest.fixture
def bundle(store_file: Path) -> Path:
    return Path(create_backup(store_file)["bundle"])


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore waits half a second for the lock, an opener a fifth: the
    shipped waits (`RESTORE_WAIT_SECONDS`, `OPEN_WAIT_SECONDS`) are for a
    person, not a test."""
    real = restore_lock.acquire

    def acquire(store_file: Path, *, wait: float = 0.5) -> Any:
        return real(store_file, wait=min(wait, 0.5))

    monkeypatch.setattr(restore_lock, "acquire", acquire)
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.2)


def test_a_read_that_began_before_a_restore_holds_it_off_until_it_closes(
    bundle: Path, store_file: Path, keys_dir: Path, quick: None
) -> None:
    """A reader opened before the restore, as `log verify` opens: the
    restore cannot replace the store under it. On POSIX it waits for the
    lock the reader holds shared and is refused; on Windows the reader
    holds the store file open, and the swap's rename is refused. Either way
    nothing changed, and the reader's verification reads the store it
    opened. Once the reader closes, the same restore goes through."""
    _put(store_file, keys_dir, "gamma, after the backup")
    inode = store_file.stat().st_ino
    with Store.open(store_file, keys_dir=keys_dir, read_only=True) as reader:
        report = restore_bundle(bundle, store_file, apply=True)
        assert reader.log_verify()["status"] == "ok"
    if sys.platform == "win32":
        assert report["status"] == "failed", report
        windows = "Windows does not rename a file another process has open"
        assert windows in report["detail"]
        assert report["lock"]["kept"] is False
    else:
        assert report["status"] == "refused", report
        assert "opening or reading the store" in report["detail"]
    assert report["moved_aside"] == [] and _aside(store_file) == []
    assert store_file.stat().st_ino == inode
    assert restore_lock.read_marker(store_file) is None

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]


def test_a_read_is_refused_while_a_restore_runs_and_holds_nothing_after(
    bundle: Path, store_file: Path, keys_dir: Path, quick: None, monkeypatch: Any
) -> None:
    """A read that comes while a restore holds the store is refused, exit
    75 from the commands (`RestoreRunningError`), and keeps nothing that
    would hold the restore up."""
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        with pytest.raises(restore_lock.RestoreRunningError):
            Store.open(store_file, keys_dir=keys_dir, read_only=True)
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]


def test_doctor_does_not_read_a_store_a_restore_holds(
    bundle: Path, store_file: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`doctor`'s store, key and index checks opened the store as a reader
    with no look at the restore lock, and read a file, keys and head that
    did not yet agree. They now say they did not check it, and why."""
    from bettermemory import doctor
    from bettermemory.doctor_store import check_index, check_store

    context = doctor._Context(cwd=None, directory=store_file.parent)
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        checks = check_store(context)
        index = check_index(context)
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]
    assert [(c.id, c.status) for c in checks] == [("store", "warn"), ("key", "warn")]
    for check in (*checks, index):
        assert check.summary == "not checked: a restore of the store is running"
        assert check.remedy == (
            "run `bettermemory doctor` again when the restore has finished"
        )
    after = check_store(doctor._Context(cwd=None, directory=store_file.parent))
    assert [c.status for c in after] == ["ok", "ok"]


def test_security_audit_does_not_check_the_key_of_a_store_a_restore_holds(
    bundle: Path,
    store_file: Path,
    tmp_path: Path,
    quick: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit read the store's meta and the key beside it with no look
    at the restore lock, and graded a key the restore was about to install
    or retire. While a restore runs it now reads neither, and says so in a
    warning; after it, the key is ok."""
    from bettermemory import posture, sandbox_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(
        sandbox_settings, "claude_managed_settings_path", lambda platform=None: managed
    )
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(tmp_path / "state"))

    def key_finding() -> Any:
        (found,) = [
            f for f in posture.audit(cwd=tmp_path).findings if f.id == "key.match"
        ]
        return found

    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        during = key_finding()
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]
    assert during.status == "warn"
    assert during.summary.startswith("not checked: a restore is running")
    assert during.fix == (
        "run `bettermemory security audit` again when the restore has finished"
    )
    assert key_finding().status == "ok"


@pytest.mark.skipif(
    sys.platform == "win32", reason="the audit checks no file modes on Windows"
)
def test_security_audit_says_it_did_not_check_the_key_rings_files_during_a_restore(
    bundle: Path,
    store_file: Path,
    tmp_path: Path,
    quick: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ring's files (key, retired keys, head, anchor, binding) are named
    by the store id in the store's meta, which the audit does not read
    while a restore runs: their mode findings went missing, with nothing
    to say so. Each now says it was not checked, and why; after the
    restore they are graded again."""
    from bettermemory import posture, sandbox_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(
        sandbox_settings, "claude_managed_settings_path", lambda platform=None: managed
    )
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(tmp_path / "state"))
    ring = ("mode.key", "mode.retired_key", "mode.head", "mode.origin", "mode.binding")

    def ring_findings() -> dict[str, Any]:
        return {f.id: f for f in posture.audit(cwd=tmp_path).findings if f.id in ring}

    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        during = ring_findings()
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]
    assert sorted(during) == sorted(ring)
    for finding in during.values():
        assert finding.status == "skip"
        assert "not checked: a restore is running" in finding.summary
    after = ring_findings()
    assert after["mode.key"].status == "ok"
    assert "not checked" not in after["mode.key"].summary


def test_backup_unlock_names_a_read_that_holds_the_lock_not_the_restore(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    quick: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore failed after its swap and left its record. The operator
    runs `log verify` (a read, here held open) and, while it reads, `backup
    unlock --yes`. Where the lock is the store's directory the read holds
    it shared, so the unlock waits and gives up: it said a restore holds
    the lock, naming the dead restore's process. It now says a command is
    reading or opening the store, and the next unlock, after the read,
    clears the record. On Windows a read holds the store file, not the
    lock, and the unlock clears at once."""
    from bettermemory import backup
    from bettermemory.cli.backup_cmd import render_unlock

    def failing(*args: Any, **kwargs: Any) -> None:
        raise OSError("the keys directory stopped answering")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(backup, "_install_keys", failing)
        failed = restore_bundle(bundle, store_file, apply=True)
    assert failed["status"] == "failed" and failed["lock"]["kept"] is True

    reader = Store.open(store_file, keys_dir=keys_dir, read_only=True)
    try:
        report = restore_lock.unlock(store_file, apply=True)
    finally:
        reader.close()
    if sys.platform == "win32":
        assert report["status"] == "cleared", report
        return
    assert report["status"] == "held", report
    assert report["holder"] == restore_lock.HOLDER_READER
    assert report["marker"] == restore_lock.read_marker(store_file) is not None
    said = render_unlock(report)
    assert "a command is reading or opening the store" in said
    assert "a restore holds" not in said
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"


def test_a_writer_outside_the_package_is_caught_by_the_restores_own_log_verify(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The design's boundary, shown: a process with a SQLite connection of
    its own (not this package's code) writes a row into the store while a
    restore runs, after its swap. No lock held it off; the row carries no
    MAC the store's key made, so the restore's own `log verify` reads the
    restored store `tampered` and the restore reports failed, keeping
    writers out, and `log verify` says the same afterwards."""
    _put(store_file, keys_dir, "gamma, after the backup")
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        foreign = sqlite3.connect(str(store_file), isolation_level=None)
        try:
            seq, mac = foreign.execute(
                "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            foreign.execute(
                "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
                "VALUES (?, '2026-11-03T00:00:00.000000Z', NULL, 'note', '{}', ?, ?)",
                (seq + 1, mac, "0" * 64),
            )
        finally:
            foreign.close()
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "failed", result
    assert result["detail"] == (
        "the restored store reads tampered under `log verify`; writing opens of "
        f"{store_file} are refused until it is finished: run the restore again, "
        "or keep the store as it stands with `bettermemory backup unlock --yes`"
    )
    assert result["log"]["status"] == "tampered"
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    capsys.readouterr()
    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 1
    assert '"tampered"' in capsys.readouterr().out
