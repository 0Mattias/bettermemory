"""The read-only commands write nothing to a store file or beside it (ENG-104).

`log verify`, `log export`, `log anchor`, `security scan --store`,
`doctor`, `stats` and `memory history` opened the store through the
writing opener with ``allow_rekey=False``: no rekey, but the open still
switched a rollback-journal file to WAL (its header's bytes), made the
file owner-only, added the membership tables an older build's store
lacks, and created the ``-wal`` and ``-shm`` it then removed. They now
open with ``read_only=True`` (`store_schema.connect_read_only`): SQLite's
``mode=ro`` in place where that creates nothing, a private copy for a
closed WAL-mode store.

Each command runs in process against three stores: a current one (closed,
WAL, as this build leaves it), an older one (an earlier build's store
without the membership tables, saved in rollback-journal mode at 0640, as
a bundle's snapshot copied out is), and a live one (another connection
holds it open, so its ``-wal`` and ``-shm`` are there). After each, the
file's bytes, mode and modification time, the names beside it and its
directory's modification time (which an entry created and removed again
moves) are what they were, and no private copy is left. A live store's
``-shm`` is the one file a reader writes, as every reader of it does; its
name and mode are checked, not its bytes. Names this change adds are read
through their modules where they are used, so the file collects on a
build without them and each test fails there on its own.
"""

from __future__ import annotations

import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import _daemon_client, claude_paths, sandbox_settings
from bettermemory import store as store_module
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.server import main as cli_main
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_schema import MEMBERSHIP_TABLES

from .conftest import set_git_discovery_ceiling, shielded_child_env

COMMANDS: dict[str, list[str]] = {
    "log verify": ["log", "verify"],
    "log export": ["log", "export"],
    "log anchor": ["log", "anchor"],
    "security scan --store": ["security", "scan", "--store", "{store}"],
    "doctor": ["doctor"],
    "stats": ["stats"],
    "memory history": ["memory", "history", "{record}"],
}
SHAPES = ("current", "older", "live")


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


@pytest.fixture
def quiet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor reads Claude Code's settings and the daemon's state: a fake
    home, the managed settings path and the state directory under the
    test's directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, raising=False)
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(
        sandbox_settings, "claude_managed_settings_path", lambda platform=None: managed
    )
    monkeypatch.setenv(_daemon_client.STATE_DIR_ENV, str(tmp_path / "state"))
    set_git_discovery_ceiling(tmp_path, monkeypatch)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where `tempfile` puts a private copy, so a copy left behind shows."""
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def _sql(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


def _store(tmp_path: Path, shape: str) -> tuple[Path, str, sqlite3.Connection | None]:
    """A store of `shape`, the id of a memory in it, and the connection
    that holds a live one open."""
    path = (tmp_path / "stores" / shape / STORE_FILENAME).resolve()
    with Store.create(path) as created:
        memory = _memory("the staging database listens on 5432")
        created.put_memory(memory)
    if shape == "older":
        _sql(
            path,
            *(f"DROP TABLE {table}" for table in MEMBERSHIP_TABLES),
            "DROP INDEX log_corroborations",
            "PRAGMA journal_mode = DELETE",
        )
        path.chmod(0o640)
    holder = None
    if shape == "live":
        holder = sqlite3.connect(str(path), isolation_level=None)
        holder.execute("SELECT COUNT(*) FROM log").fetchone()
    return path, memory.id, holder


def _state(path: Path) -> dict[str, object]:
    """The file's bytes, mode and mtime, every name beside it with its
    mode, and its directory's mtime. A live store's ``-shm`` is written by
    every reader of it, so its bytes are not part of this."""
    status = path.stat()
    return {
        "bytes": path.read_bytes(),
        "mode": stat.S_IMODE(status.st_mode),
        "mtime": status.st_mtime_ns,
        "beside": sorted(
            (p.name, stat.S_IMODE(p.stat().st_mode)) for p in path.parent.iterdir()
        ),
        "directory_mtime": path.parent.stat().st_mtime_ns,
    }


def _run(
    argv: list[str],
    *,
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store.parent))
    code = 0
    try:
        cli_main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[dict[str, tuple[Path, str]]]:
    made: dict[str, tuple[Path, str]] = {}
    holders: list[sqlite3.Connection] = []
    for shape in SHAPES:
        path, record, holder = _store(tmp_path, shape)
        made[shape] = (path, record)
        if holder is not None:
            holders.append(holder)
    yield made
    for holder in holders:
        holder.close()


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("command", list(COMMANDS))
def test_the_command_leaves_the_store_as_it_was(
    command: str,
    shape: str,
    stores: dict[str, tuple[Path, str]],
    scratch: Path,
    quiet_home: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, record = stores[shape]
    argv = [part.format(store=path, record=record) for part in COMMANDS[command]]
    before = _state(path)
    code, out, err = _run(argv, store=path, monkeypatch=monkeypatch, capsys=capsys)
    after = _state(path)
    assert "cannot read the store" not in err, err
    assert "is not opened" not in err, err
    assert code in (0, 1, 2, 3), (code, err)
    assert out or err
    for part in ("bytes", "mode", "mtime", "beside", "directory_mtime"):
        assert after[part] == before[part], part
    assert list(scratch.iterdir()) == []


def test_a_closed_wal_store_is_read_from_a_private_copy(
    tmp_path: Path, scratch: Path
) -> None:
    path, record, _ = _store(tmp_path, "current")
    before = _state(path)
    store = Store.open(path, read_only=True)
    try:
        copies = [p for p in scratch.rglob(STORE_FILENAME)]
        assert len(copies) == 1
        if sys.platform != "win32":  # no mode bits there; the user's own temp
            assert stat.S_IMODE(copies[0].stat().st_mode) == 0o600
            assert stat.S_IMODE(copies[0].parent.stat().st_mode) == 0o700
        assert store.path == path
        assert store.get_memory(record) is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.put_memory(_memory("written through a read-only open"))
    finally:
        store.close()
    assert list(scratch.iterdir()) == []
    assert _state(path) == before


@pytest.mark.parametrize("shape", ["older", "live"])
def test_a_store_that_creates_nothing_is_read_in_place(
    shape: str, tmp_path: Path, scratch: Path
) -> None:
    path, record, holder = _store(tmp_path, shape)
    try:
        with Store.open(path, read_only=True) as store:
            assert list(scratch.iterdir()) == []
            assert store.get_memory(record) is not None
            assert store.status()["path"] == str(path)
    finally:
        if holder is not None:
            holder.close()


def test_the_read_only_open_refuses_what_every_opener_refuses(
    tmp_path: Path, scratch: Path
) -> None:
    """One refusal path: a store whose meta is a view is refused by the
    read-only open with the writing opener's error, and no copy is left."""
    path, _, _ = _store(tmp_path, "current")
    _sql(
        path,
        "ALTER TABLE meta RENAME TO meta_kept",
        "CREATE VIEW meta AS SELECT key, value FROM meta_kept",
    )
    before = _state(path)
    with pytest.raises(store_module.StoreRefusedError) as refused:
        Store.open(path, read_only=True)
    assert "not a store this build wrote" in str(refused.value)
    assert list(scratch.iterdir()) == []
    assert _state(path) == before


def test_a_copy_left_open_goes_with_the_store_and_at_exit(
    tmp_path: Path, scratch: Path
) -> None:
    """The review's L2: a read-only open that was never closed left its
    copy, the whole store, in the temporary directory after the process
    exited. In a child (this suite's `close_opened_stores` holds every
    store a test opens until it ends): a store dropped unclosed takes its
    copy with it when it is collected, its connection closed first
    (Windows refuses to remove a file a handle is open on, so the copy
    outlived the process there), and one still open at exit too."""
    path, _, _ = _store(tmp_path, "current")
    env = shielded_child_env()
    env.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
    child = (
        "import gc, sqlite3, sys\n"
        "from pathlib import Path\n"
        "from bettermemory.store import Store\n"
        "store, scratch = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "def count(): return len(list(scratch.iterdir()))\n"
        "dropped = Store.open(store, read_only=True)\n"
        "conn = dropped._conn\n"
        "seen = [count()]\n"
        "del dropped\n"
        "gc.collect()\n"
        "seen.append(count())\n"
        "try:\n"
        "    conn.execute('SELECT 1')\n"
        "except sqlite3.ProgrammingError:\n"
        "    seen.append('closed')\n"
        "else:\n"
        "    seen.append('open')\n"
        "kept = Store.open(store, read_only=True)\n"
        "seen.append(count())\n"
        "print(*seen)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", child, str(path), str(scratch)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", "0", "closed", "1"]
    assert list(scratch.iterdir()) == []


def test_a_read_only_open_never_rekeys(tmp_path: Path) -> None:
    path, _, _ = _store(tmp_path, "current")
    with pytest.raises(ValueError, match="never rekeys"):
        Store.open(path, read_only=True, allow_rekey=True)
