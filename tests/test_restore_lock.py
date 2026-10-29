"""A restore holds its store against every writing opener (ENG-103).

The daemon and spawn locks keep a daemon out of a restore; `serve`, the
shim's in-process fallback, the CLI's writing commands and the hooks took
neither and could write into the store while it was swapped. The restore
lock (`bettermemory.restore_lock`) closes both orders of that race: a
restore is refused while any process has the store open (SQLite's own
locks prove it), and a writing open is refused while a restore holds the
lock (a lock on the store's directory, so a writing open makes no
file). A restore that dies, or fails after the swap, leaves its record
beside the store, which refuses every writing open until a restore
finishes or `backup unlock --yes` removes it.

The writers mostly run as subprocesses; a restore lets an open from its
own thread through, and refuses one from another thread of its process.
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import backup
from bettermemory.backup import create_backup, restore_bundle
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store, StoreRefusedError

from .conftest import shielded_child_env

#: How long a spawned `bettermemory` has to import, open and answer.
_CHILD_SECONDS = 60.0


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


def _put(store_file: Path, keys_dir: Path, *bodies: str) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        for body in bodies:
            opened.put_memory(_memory(body))


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    created = Store.create(directory / STORE_FILENAME, keys_dir=keys_dir)
    created.close()
    path = directory / STORE_FILENAME
    _put(path, keys_dir, "alpha", "beta")
    return path


@pytest.fixture
def bundle(store_file: Path) -> Path:
    return Path(create_backup(store_file)["bundle"])


@pytest.fixture
def child_env(tmp_path: Path) -> dict[str, str]:
    """The environment a spawned `bettermemory` runs in: this test's store
    and keys (through the variables the fixtures set), and a home, config
    and state directory of its own under the test's directory."""
    home = tmp_path / "home"
    env = shielded_child_env()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        APPDATA=str(home / "AppData" / "Roaming"),
        BETTERMEMORY_STATE_DIR=str(tmp_path / "state"),
    )
    return env


def _bettermemory(*argv: str) -> list[str]:
    return [sys.executable, "-m", "bettermemory", *argv]


class _Serve:
    """`bettermemory serve` in a child process, its stderr read on a
    thread, so a test can wait for it to open the store or to exit."""

    def __init__(self, env: dict[str, str]) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.said: list[str] = []
        self.process = subprocess.Popen(
            _bettermemory("serve"),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stderr is not None
        stderr = self.process.stderr

        def read() -> None:
            for line in stderr:
                self.lines.put(line)

        threading.Thread(target=read, daemon=True).start()

    def wait(self) -> str:
        """``open`` once it says which directory it serves (it has opened
        the store), ``exit`` once it has exited, whichever comes first."""
        deadline = time.monotonic() + _CHILD_SECONDS
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    self.drain()
                    return "exit"
                continue
            self.said.append(line)
            if "memory directory:" in line:
                return "open"
        raise AssertionError("serve neither opened the store nor exited:\n" + self.text)

    def drain(self) -> None:
        self.process.wait(timeout=_CHILD_SECONDS)
        time.sleep(0.2)
        while not self.lines.empty():
            self.said.append(self.lines.get())

    @property
    def text(self) -> str:
        return "".join(self.said)

    def stop(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=_CHILD_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


@pytest.fixture
def serve(child_env: dict[str, str]) -> Iterator[list[_Serve]]:
    """Every `serve` a test starts, stopped when it ends."""
    started: list[_Serve] = []
    yield started
    for one in started:
        one.stop()


def _aside(store_file: Path) -> list[Path]:
    return sorted(store_file.parent.glob(f"{STORE_FILENAME}.pre-restore-*"))


def test_a_restore_is_refused_while_serve_has_the_store_open(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    serve: list[_Serve],
) -> None:
    """The writer first: `serve` holds the store open, which takes no
    daemon lock. The restore is refused and changes nothing; once serve
    has closed, the same restore goes through."""
    _put(store_file, keys_dir, "gamma, after the backup")
    server = _Serve(child_env)
    serve.append(server)
    assert server.wait() == "open", server.text
    inode = store_file.stat().st_ino

    report = restore_bundle(bundle, store_file, apply=True)

    assert report["status"] == "refused", report
    assert f"a process has {store_file} open" in report["detail"]
    assert "close every session" in report["detail"]
    assert report["moved_aside"] == [] and _aside(store_file) == []
    assert store_file.stat().st_ino == inode
    assert report["keys_installed"] == [] and report["head"] is None
    # The lock was given back with no record left in it.
    from bettermemory import restore_lock

    assert restore_lock.read_marker(store_file) is None

    server.stop()
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]


def _restore_paused_after_the_swap(
    bundle: Path, store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Thread, threading.Event, dict[str, Any]]:
    """A restore on a thread, held right after it swapped the store file
    in (the keys and the head not yet the restored file's), until the
    returned event is set; its report lands in the returned dict."""
    reached, go = threading.Event(), threading.Event()
    real = backup._install_keys

    def paused(*args: Any, **kwargs: Any) -> None:
        reached.set()
        assert go.wait(_CHILD_SECONDS * 2)
        real(*args, **kwargs)

    monkeypatch.setattr(backup, "_install_keys", paused)
    result: dict[str, Any] = {}

    def run() -> None:
        result.update(restore_bundle(bundle, store_file, apply=True))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert reached.wait(_CHILD_SECONDS), "the restore never reached its swap"
    return thread, go, result


def test_serve_and_a_writing_command_are_refused_while_a_restore_runs(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    serve: list[_Serve],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore first: while it runs, `serve` (the shim's fallback opens
    the same way) and a writing CLI command are refused in one line each,
    exit 1, and write nothing; after it, both open the restored store."""
    _put(store_file, keys_dir, "gamma, after the backup")
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        server = _Serve(child_env)
        serve.append(server)
        assert server.wait() == "exit", server.text
        assert server.process.returncode == 1, server.text
        assert "a restore of it is running" in server.text
        assert "Traceback" not in server.text

        listed = subprocess.run(
            _bettermemory("episodes", "list"),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_CHILD_SECONDS,
        )
        assert listed.returncode == 1, listed.stdout + listed.stderr
        assert "a restore of it is running" in listed.stderr
        assert "Traceback" not in listed.stderr
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]

    listed = subprocess.run(
        _bettermemory("episodes", "list"),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        assert opened.log_verify()["status"] == "ok"


_CRASHED_RESTORE = """
import os, sys
from pathlib import Path
from bettermemory import restore_lock
held = restore_lock.acquire(Path(sys.argv[1]))
assert held is not None
held.mark(bundle=sys.argv[2], stage="moving the store aside")
os._exit(0)
"""


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def test_a_restore_that_died_refuses_writers_until_backup_unlock(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restore killed part way leaves its record beside the store and
    no process holding the lock: every writing open is refused, naming
    the two ways out, the plan of a new restore says so, and `backup
    unlock` shows the record, then removes it with --yes."""
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHED_RESTORE, str(store_file), str(bundle)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert crashed.returncode == 0, crashed.stderr
    lock = store_file.with_name(STORE_FILENAME + ".restore-lock")
    if sys.platform != "win32":
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    with pytest.raises(StoreRefusedError) as refused:
        Store.open(store_file, keys_dir=keys_dir)
    said = str(refused.value)
    assert "a restore of it did not finish" in said
    assert f"from {bundle}" in said and "stopped at moving the store aside" in said
    assert f"bettermemory backup restore {bundle} --yes" in said
    assert "bettermemory backup unlock --yes" in said

    listed = subprocess.run(
        _bettermemory("episodes", "list"),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert listed.returncode == 1 and "did not finish" in listed.stderr
    assert "Traceback" not in listed.stderr

    assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 3
    assert "a restore of this store did not finish" in capsys.readouterr().out

    before = lock.read_bytes()
    assert _run_cli(["backup", "unlock"], monkeypatch) == 3
    out = capsys.readouterr().out
    assert "records a restore that did not finish" in out and str(bundle) in out
    assert lock.read_bytes() == before

    assert _run_cli(["backup", "unlock", "--yes"], monkeypatch) == 0
    assert "cleared" in capsys.readouterr().out
    assert not lock.exists()
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        opened.put_memory(_memory("written once the lock was cleared"))
    assert _run_cli(["backup", "unlock"], monkeypatch) == 0
    assert "nothing to clear" in capsys.readouterr().out


def test_a_rerun_that_stops_before_the_swap_keeps_the_earlier_record(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore run again over one that did not finish takes its record
    over; when it too stops before replacing the store file, the store is
    still part restored, so the record stays and writers stay out."""
    from bettermemory import restore_lock

    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHED_RESTORE, str(store_file), str(bundle)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert crashed.returncode == 0, crashed.stderr

    def refused(*args: object, **kwargs: object) -> None:
        raise OSError("the rename was refused")

    monkeypatch.setattr(backup, "_swap_in", refused)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert report["lock"]["unfinished"] is not None
    assert report["lock"]["kept"] is True
    assert restore_lock.read_marker(store_file) is not None
    with pytest.raises(StoreRefusedError, match="did not finish"):
        Store.open(store_file, keys_dir=keys_dir)


def test_writing_opens_make_no_file_and_a_finished_restore_leaves_none(
    bundle: Path, store_file: Path, keys_dir: Path, tmp_path: Path
) -> None:
    """The lock is the store's directory: a writing open, a refused one of
    a file that is no store, and a restore that finished leave nothing
    beside the store file."""
    from bettermemory import restore_lock

    lock = restore_lock.lock_path(store_file)
    _put(store_file, keys_dir, "gamma, after the backup")
    assert not lock.exists()

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    assert not lock.exists()
    _put(store_file, keys_dir, "delta, after the restore")
    assert not lock.exists()

    junk = tmp_path / "junk" / STORE_FILENAME
    junk.parent.mkdir()
    junk.write_bytes(b"no database at all, " * 64)
    with pytest.raises(StoreRefusedError):
        Store.open(junk, keys_dir=keys_dir)
    assert sorted(p.name for p in junk.parent.iterdir()) == [STORE_FILENAME]


def test_a_restore_that_fails_after_the_swap_keeps_writers_out_until_one_finishes(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step after the swap fails: the store is part restored, so the
    lock keeps its record and the report says writers stay refused. A
    restore run again takes the record over, finishes, and clears it."""
    _put(store_file, keys_dir, "gamma, after the backup")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("the keys directory is full")

    with monkeypatch.context() as patched:
        patched.setattr(backup, "_append_record", broken)
        report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert report["lock"]["kept"] is True
    assert "writing opens of" in report["detail"]
    assert "bettermemory backup unlock --yes" in report["detail"]
    with pytest.raises(StoreRefusedError, match="did not finish"):
        Store.open(store_file, keys_dir=keys_dir)

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    unfinished = report["lock"]["unfinished"]
    assert unfinished["bundle"] == str(bundle) and unfinished["pid"] == os.getpid()
    assert report["lock"]["kept"] is False
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        assert opened.log_verify()["status"] == "ok"


def test_a_restore_that_fails_before_the_swap_leaves_no_record(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was replaced, so nothing stays refused."""

    def refused(*args: object, **kwargs: object) -> None:
        raise OSError("the rename was refused")

    monkeypatch.setattr(backup, "_swap_in", refused)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert report["lock"]["kept"] is False
    assert "writing opens of" not in report["detail"]
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        assert opened.log_verify()["status"] == "ok"


def test_the_probe_sees_a_connection_and_not_a_closed_one(store_file: Path) -> None:
    """SQLite's exclusive lock is refused while another connection has
    the store open, in this process too."""
    from bettermemory import restore_lock

    assert restore_lock.probe(store_file) is None
    other = sqlite3.connect(str(store_file))
    try:
        other.execute("SELECT count(*) FROM log").fetchone()
        reason = restore_lock.probe(store_file)
    finally:
        other.close()
    assert reason is not None and f"a process has {store_file} open" in reason
    assert restore_lock.probe(store_file) is None
    assert restore_lock.probe(store_file.with_name("missing.sqlite")) is None


def test_another_thread_of_the_restoring_process_is_refused(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the thread that holds the restore lock opens the store through
    it: a writing open from another thread of the same process waits and
    is refused as any other opener is (cold review L1)."""
    from bettermemory import restore_lock

    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.2)
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        with pytest.raises(StoreRefusedError, match="a restore of it is running"):
            Store.open(store_file, keys_dir=keys_dir)
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert result["status"] == "restored", result["detail"]


def test_a_store_switched_to_wal_by_its_open_is_seen_by_the_probe(
    store_file: Path,
) -> None:
    """A store in rollback-journal mode (copied in by hand) is switched to
    WAL by its first writing open, and a WAL connection holds SQLite's
    shared lock only from its next read: `_connect` reads once after the
    switch, under the restore lock, so a restore's probe sees the
    connection as soon as the open returns (cold review M2)."""
    from bettermemory import restore_lock
    from bettermemory.store import _connect

    plain = sqlite3.connect(str(store_file))
    try:
        assert plain.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        plain.close()
    opened = _connect(store_file)
    try:
        assert opened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert restore_lock.probe(store_file) is not None
    finally:
        opened.close()
    assert restore_lock.probe(store_file) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows locks the record file")
@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EINVAL])
def test_a_directory_flock_cannot_lock_falls_back_to_the_record_file(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: int,
) -> None:
    """A mount where flock on a directory fails as unsupported (ENOTSUP, as
    a `nolocks` NFS mount answers, or EINVAL) is no restore holding the
    lock: writing opens go on, the log says once that the restore lock is
    the record file there, as on Windows, a restore finishes, and a
    record a restore left refuses writers (cold review M1)."""
    import fcntl

    from bettermemory import restore_lock

    real = fcntl.flock

    def flock(fd: int, operation: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(code, os.strerror(code))
        real(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(restore_lock, "_RECORD_MODE_DIRS", set())
    with caplog.at_level(logging.WARNING, logger="bettermemory.restore_lock"):
        _put(store_file, keys_dir, "gamma, with no directory lock")
        _put(store_file, keys_dir, "delta, the same")
    said = [
        r.getMessage() for r in caplog.records if "flock cannot lock" in r.getMessage()
    ]
    assert len(said) == 1, said
    assert str(restore_lock.lock_path(store_file)) in said[0]

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    assert not restore_lock.lock_path(store_file).exists()

    held = restore_lock.acquire(store_file)
    assert held is not None and held.record
    held.mark(bundle=str(bundle), stage="moving the store aside")
    held.release(clear=False)
    with pytest.raises(StoreRefusedError, match="did not finish"):
        Store.open(store_file, keys_dir=keys_dir)


def test_doctor_fails_a_store_a_restore_did_not_finish(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doctor's store check opens the store read-only, so it never meets
    the refusal every writing open gets after a killed restore: its own
    `restore` check reads the record, fails with both ways out (doctor
    then exits 1), and is ok once `backup unlock --yes` removed it (cold
    review H2). Called directly: a whole doctor run reads the home's client
    settings, which tests/test_doctor.py fakes."""
    from bettermemory import doctor

    assert doctor.CHECK_IDS.index("restore") == doctor.CHECK_IDS.index("store") - 1
    context = doctor._Context(cwd=None, directory=store_file.parent)
    assert doctor.check_restore(context).status == "ok"
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHED_RESTORE, str(store_file), str(bundle)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert crashed.returncode == 0, crashed.stderr

    check = doctor.check_restore(context)
    assert check.status == "fail"
    assert check.summary.startswith("writes are refused: a restore did not finish (")
    assert "stopped at moving the store aside" in check.summary
    assert check.remedy is not None
    assert check.remedy.startswith(
        "run `bettermemory backup unlock --yes` after checking the store, or "
        "re-run the restore"
    )
    assert f"bettermemory backup restore {bundle} --yes" in check.remedy
    assert check.details["record"] == f"{store_file}.restore-lock"
    assert doctor.exit_code(doctor.overall_status([check])) == 1

    assert _run_cli(["backup", "unlock", "--yes"], monkeypatch) == 0
    capsys.readouterr()
    assert doctor.check_restore(context).status == "ok"


def test_doctor_warns_while_a_restore_runs(
    bundle: Path, store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import doctor

    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        check = doctor.check_restore(
            doctor._Context(cwd=None, directory=store_file.parent)
        )
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    assert check.status == "warn"
    assert check.summary == "writes are refused while a restore runs"
    assert result["status"] == "restored", result["detail"]


def test_a_read_only_command_is_refused_while_a_restore_runs(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    child_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`log verify` run while a restore held the store read a file, keys and
    head that did not yet agree and said `tampered`, exit 1, which a cron or
    SIEM gate reads as an attack. A read-only command now reads nothing
    while a restore runs: one line, exit 75; after it, the log verifies
    (cold review H3)."""
    _put(store_file, keys_dir, "gamma, after the backup")
    thread, go, result = _restore_paused_after_the_swap(bundle, store_file, monkeypatch)
    try:
        verified = subprocess.run(
            _bettermemory("log", "verify", "--json"),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_CHILD_SECONDS,
        )
        counted = subprocess.run(
            _bettermemory("stats"),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_CHILD_SECONDS,
        )
    finally:
        go.set()
        thread.join(_CHILD_SECONDS)
    said = (
        f"{store_file} is not opened: a restore is running; try again when it "
        "has finished"
    )
    assert verified.returncode == 75, verified.stdout + verified.stderr
    assert verified.stdout == ""
    assert said in verified.stderr
    assert "tampered" not in verified.stderr and "Traceback" not in verified.stderr
    assert counted.returncode == 75, counted.stdout + counted.stderr
    assert said in counted.stderr
    assert result["status"] == "restored", result["detail"]

    after = subprocess.run(
        _bettermemory("log", "verify", "--json"),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert after.returncode == 0, after.stdout + after.stderr


def test_a_hook_ends_with_the_refusal_before_it_starts_a_daemon(
    bundle: Path,
    store_file: Path,
    child_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prompt hook started a daemon on every prompt while a restore's
    record refused its open, each daemon exiting 1 with the line only in
    its log. The hooks' first check now reads the record and ends the hook,
    exit 0 as a hook must, with the refusal (cold review L11)."""
    from bettermemory.cli._common import exit_if_bundle_store

    exit_if_bundle_store(prefix="[bettermemory] prompt-recall")
    assert capsys.readouterr().err == ""
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHED_RESTORE, str(store_file), str(bundle)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
    )
    assert crashed.returncode == 0, crashed.stderr
    with pytest.raises(SystemExit) as ended:
        exit_if_bundle_store(prefix="[bettermemory] prompt-recall")
    assert ended.value.code == 0
    err = capsys.readouterr().err
    assert err.startswith(f"[bettermemory] prompt-recall: {store_file} is not opened: ")
    assert "a restore of it did not finish" in err
    assert "bettermemory backup unlock --yes" in err
