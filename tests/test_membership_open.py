"""The migration's run at open (design 8.6) at every entry point that
opens the store: the CLI's context, the daemon, `bettermemory serve`, the
session-start hook of the path with no daemon, and the recall's own open on
the in-process prompt-recall path.

Each test builds a store whose records carry origins as rc3 recorded them
and no home, in a repository made under the test's directory, and opens it
through one entry point: the open homes every record, and a second open of
the migrated store writes nothing (the log's row count is unchanged). The
session-start hook logs the run and prints nothing on stderr; a store
opened without its key is left as it is, and the next open with the key
migrates it. Without a machine id (`location.process_machine` raises
OSError) each entry point logs one line, opens the store as it is, and the
next open migrates. Two foreground starts of the daemon on one store run
the migration once. `bettermemory prompt-recall` with a daemon opens no
store before it posts (design 8.9: the per-prompt path pays no migration
check). Git runs with no user or system configuration and a fixed author.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import __version__, _daemon_client, _entry, location
from bettermemory import daemon as _daemon
from bettermemory._daemon_client import health, read_state, shutdown_daemon
from bettermemory.cli import _common, prompt_recall_cmd, session_start_cmd
from bettermemory.cli import serve as serve_cmd
from bettermemory.config import ENV_DIR_OVERRIDE, KEYS_DIR_ENV, load_config
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import STORE_FILENAME, Store

from .conftest import shielded_child_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

# A fixed instant from an epoch count: no rule reads it.
_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
_REMOTE = "https://example.invalid/acme/open.git"


# ---------------------------------------------------------------------------
# A neutral git and the store rc3 left
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system configuration reaches git, a fixed author and
    committer, and no variable that points git elsewhere."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "fixture@example.invalid")
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
    ):
        monkeypatch.delenv(var, raising=False)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def make_repo(path: Path) -> str:
    """A repository with one commit and a remote; returns the commit."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"fixture {path.name}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "first")
    _git(path, "remote", "add", "origin", _REMOTE)
    return _git(path, "rev-parse", "HEAD")


@dataclass
class Left:
    """A store with records and no home rows, as rc3 left it."""

    directory: Path
    repo: Path
    records: int

    @property
    def path(self) -> Path:
        return self.directory / STORE_FILENAME


def write_unhomed(directory: Path, repo: Path, head: str) -> int:
    """Two memories and an episode written in `repo` and one memory with
    no origin, through the store and with no home; returns the count."""
    with Store(directory) as store:
        for n in range(2):
            store.put_memory(
                Memory(
                    id=generate_ulid(),
                    created=_NOW,
                    updated=_NOW,
                    scopes=["tools"],
                    confidence=Confidence.MEDIUM,
                    source=Source.EXPLICIT,
                    body=f"the release of {repo.name} runs from tag {n}\n",
                    origin=Origin(cwd=str(repo), repo=_REMOTE, worktree_root=str(repo)),
                    verified_head=head,
                )
            )
        store.put_memory(
            Memory(
                id=generate_ulid(),
                created=_NOW,
                updated=_NOW,
                scopes=["global"],
                confidence=Confidence.MEDIUM,
                source=Source.EXPLICIT,
                body="written with no origin\n",
            )
        )
        store.write_episode(
            session_id="s-open",
            body=f"worked on {repo.name}",
            origin=Origin(cwd=str(repo), worktree_root=str(repo)),
            now=_NOW,
        )
    return 4


@pytest.fixture
def left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Left:
    """The store under `BETTERMEMORY_DIR`, where every entry point's config
    resolves it."""
    tmp = tmp_path.resolve()
    repo = tmp / "Developer" / "app"
    head = make_repo(repo)
    directory = tmp / "store"
    records = write_unhomed(directory, repo, head)
    monkeypatch.setenv(ENV_DIR_OVERRIDE, str(directory))
    return Left(directory=directory, repo=repo, records=records)


def unhomed_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return sum(
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS "
                    "(SELECT 1 FROM homes h WHERE h.record_id = t.id)"
                ).fetchone()[0]
            )
            for table in ("memories", "tombstones", "episodes")
        )
    finally:
        conn.close()


def log_rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM log").fetchone()[0])
    finally:
        conn.close()


def migrated_rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM log WHERE kind = 'membership_migrated'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def homes(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM homes").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The entry points, in process
# ---------------------------------------------------------------------------


def open_cli_context() -> None:
    _common.cli_context().store.close()


def open_serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """`bettermemory serve` with a server that returns at once."""

    class Server:
        def run(self, transport: str) -> None:
            assert transport == "stdio"

    def build_server(**kwargs: Any) -> Server:
        kwargs["store"].close()
        return Server()

    monkeypatch.setattr(serve_cmd, "build_server", build_server)
    return serve_cmd.run_serve


def open_session_start() -> None:
    """The in-process session-start body with no session id: it opens the
    store and records nothing of its own."""
    assert session_start_cmd._build_context_block() is None


@pytest.fixture(params=["cli_context", "serve", "session_start"])
def entry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Callable[[], None]]]:
    name = str(request.param)
    if name == "cli_context":
        yield name, open_cli_context
    elif name == "serve":
        yield name, open_serve(monkeypatch)
    else:
        yield name, open_session_start


def test_each_entry_point_migrates_at_open_and_writes_nothing_after(
    left: Left,
    entry: tuple[str, Callable[[], None]],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Design 8.6: the first open homes every record in one run and one
    ``membership_migrated`` row; a second open of the migrated store
    writes nothing. The CLI says so on stderr in one line; `serve` and the
    session-start hook log it, and the hook prints nothing on stderr."""
    name, open_store = entry
    assert unhomed_count(left.path) == left.records
    caplog.set_level(logging.INFO)
    open_store()
    assert unhomed_count(left.path) == 0
    assert homes(left.path) == left.records
    assert migrated_rows(left.path) == 1
    err = capsys.readouterr().err
    logged = [
        r.getMessage()
        for r in caplog.records
        if "membership migrated" in r.getMessage()
    ]
    if name == "cli_context":
        assert err.count("membership migrated") == 1, err
        assert f"{left.records} records homed" in err
    else:
        assert "membership" not in err, err
        assert len(logged) == 1, logged
        assert logged[0].startswith(
            f"membership migrated: {left.records} records homed"
        )
    rows = log_rows(left.path)
    caplog.clear()
    open_store()
    assert log_rows(left.path) == rows
    assert migrated_rows(left.path) == 1
    assert "membership" not in capsys.readouterr().err
    assert not [r for r in caplog.records if "membership migrated" in r.getMessage()]


def test_each_entry_point_without_a_machine_id_opens_and_migrates_next_time(
    left: Left,
    entry: tuple[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Design 8.6 at an open where this process has no machine id: one log
    line, the entry point runs on the store as it is, and the next open
    with a machine id migrates."""
    name, open_store = entry
    real = location.process_machine

    def no_machine() -> Any:
        raise PermissionError("the state directory is not writable")

    monkeypatch.setattr(location, "process_machine", no_machine)
    caplog.set_level(logging.INFO)
    rows = log_rows(left.path)
    open_store()
    assert unhomed_count(left.path) == left.records
    assert migrated_rows(left.path) == 0
    assert log_rows(left.path) == rows
    lines = [r for r in caplog.records if "no machine id" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    expected = logging.INFO if name == "session_start" else logging.WARNING
    assert lines[0].levelno == expected
    monkeypatch.setattr(location, "process_machine", real)
    open_store()
    assert unhomed_count(left.path) == 0
    assert migrated_rows(left.path) == 1


def test_a_hook_leaves_a_store_opened_without_its_key(
    left: Left,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The session-start hook opens the store without a rekey: a store
    whose key is not on this machine opens for reading and is not migrated;
    the next open with a key (here the CLI's, which re-keys) migrates it."""
    elsewhere = tmp_path / "other-keys"
    elsewhere.mkdir()
    monkeypatch.setenv(KEYS_DIR_ENV, str(elsewhere))
    rows = log_rows(left.path)
    open_session_start()
    assert unhomed_count(left.path) == left.records
    assert log_rows(left.path) == rows
    assert "membership" not in capsys.readouterr().err
    open_cli_context()
    assert unhomed_count(left.path) == 0
    assert migrated_rows(left.path) == 1


def test_the_daemon_start_migrates_once_and_logs_its_totals(
    left: Left, caplog: pytest.LogCaptureFixture
) -> None:
    """`daemon._migrate_at_start`, the call `_serve_locked` makes under the
    store's lock: one info line with the run's totals, and nothing on a
    store with every record homed."""
    caplog.set_level(logging.INFO, logger="bettermemory.daemon")
    config = load_config()
    with Store.open(left.path) as store:
        _daemon._migrate_at_start(store, config)
        rows = log_rows(left.path)
        _daemon._migrate_at_start(store, config)
    assert log_rows(left.path) == rows
    assert unhomed_count(left.path) == 0
    lines = [
        r.getMessage()
        for r in caplog.records
        if "membership migrated" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    assert lines[0].startswith(
        f"membership migrated: {left.records} records homed (3 memories, "
        "0 removed memories, 1 episodes)"
    )


def test_the_daemon_without_a_machine_id_serves_the_store_as_it_is(
    left: Left, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a machine id the daemon's start logs one warning, writes
    nothing, and serves the store as it is; the next start tries again."""

    def no_machine() -> Any:
        raise PermissionError("the state directory is not writable")

    real_machine = location.process_machine
    monkeypatch.setattr(location, "process_machine", no_machine)
    caplog.set_level(logging.INFO, logger="bettermemory.daemon")
    with Store.open(left.path) as store:
        rows = log_rows(left.path)
        _daemon._migrate_at_start(store, load_config())
    assert log_rows(left.path) == rows
    assert unhomed_count(left.path) == left.records
    warned = [r for r in caplog.records if "no machine id" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    # Only the machine patch is lifted: an undo would also lift the key
    # directory the fixtures set, and the store would open without its key.
    monkeypatch.setattr(location, "process_machine", real_machine)
    with Store.open(left.path) as store:
        _daemon._migrate_at_start(store, load_config())
    assert unhomed_count(left.path) == 0


# ---------------------------------------------------------------------------
# `bettermemory prompt-recall`
# ---------------------------------------------------------------------------


def _prompt_payload(monkeypatch: pytest.MonkeyPatch, repo: Path, session: str) -> None:
    """The UserPromptSubmit payload on stdin: a prompt in `repo`."""
    payload = {
        "session_id": session,
        "prompt": "how is the release of app tagged",
        "cwd": str(repo),
    }
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))


def _count_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the `Store` openers called from here on (`Store(...)`
    itself goes through `open_or_create`), one entry per call."""
    opened: list[str] = []
    for name in ("open", "open_or_create"):
        real = getattr(Store, name)

        def counted(
            cls: type[Store],
            /,
            *args: Any,
            _name: str = name,
            _real: Any = real,
            **kw: Any,
        ) -> Store:
            opened.append(_name)
            return _real(*args, **kw)

        monkeypatch.setattr(Store, name, classmethod(counted))
    return opened


def test_prompt_recall_with_a_daemon_opens_no_store_before_it_posts(
    left: Left,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory prompt-recall` goes to the daemon client
    (`_entry.main` routes the hook words to `_daemon_client.hook_main`).
    With a daemon's state file present and its /health answering, the
    client posts the prompt having opened no store of its own: the daemon
    migrated the store at its start (`daemon._migrate_at_start`), and a
    prompt pays no migration check (design 8.6, 8.9). The unhomed store is
    left as it is."""
    state_dir = _daemon_client.resolve_state_dir()
    store_path = _daemon_client.resolved_store_path()
    assert store_path == left.path
    # Port 1: nothing listens there, and /health is answered in process.
    _daemon_client.write_state(
        state_dir,
        {
            "pid": os.getpid(),
            "port": 1,
            "token": "t",
            "version": __version__,
            "store": str(store_path),
            "started": _NOW.isoformat().replace("+00:00", "Z"),
        },
    )
    # The daemon the state file names: its pid and store, and the proof
    # of its token when asked for one (`_daemon_client.proves`).
    monkeypatch.setattr(
        _daemon_client,
        "health",
        lambda port, timeout=1.0, nonce=None: {
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "store": str(store_path),
            **({"proof": _daemon_client.health_proof("t", nonce)} if nonce else {}),
        },
    )
    opened = _count_opens(monkeypatch)
    posted: list[tuple[str, list[str]]] = []

    def post(
        port: int, token: str, path: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        posted.append((path, list(opened)))
        return {"stdout": ""}

    monkeypatch.setattr(_daemon_client, "post", post)
    _prompt_payload(monkeypatch, left.repo, "s-daemon")
    monkeypatch.setattr(sys, "argv", ["bettermemory", "prompt-recall"])
    try:
        with pytest.raises(SystemExit) as exited:
            _entry.main()
    finally:
        _daemon_client.state_file_for(state_dir, store_path).unlink(missing_ok=True)
    assert exited.value.code == 0
    assert capsys.readouterr().err == ""
    assert posted == [(_daemon_client.HOOK_PATH + "prompt", [])]
    assert opened == []
    assert unhomed_count(left.path) == left.records


def prompt_recall_in_process(
    monkeypatch: pytest.MonkeyPatch, repo: Path, session: str
) -> None:
    """`prompt_recall_cmd.run`, the path with no daemon, on a prompt in
    `repo`: the recall opens the store itself (`recall._open_store`)."""
    _prompt_payload(monkeypatch, repo, session)
    with pytest.raises(SystemExit) as exited:
        prompt_recall_cmd.run(argparse.Namespace(prompt=None, session_id=None))
    assert exited.value.code == 0


def test_prompt_recall_in_process_migrates_an_unhomed_store_once(
    left: Left,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Design 8.6 on the in-process prompt-recall path: the recall's own
    open of the store homes every record in one run and one
    ``membership_migrated`` row; the next prompt finds every record homed
    and adds no row of its own. Nothing is printed on stderr, which Claude
    Code shows the user."""
    assert unhomed_count(left.path) == left.records
    for session in ("s-first", "s-second"):
        prompt_recall_in_process(monkeypatch, left.repo, session)
        assert unhomed_count(left.path) == 0
        assert homes(left.path) == left.records
        assert migrated_rows(left.path) == 1
        assert "membership" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The daemon, as a process
# ---------------------------------------------------------------------------


def _child_env(tmp_path: Path, left: Left) -> dict[str, str]:
    """The environment of a daemon this test starts: every directory it
    resolves (store, keys, state, home and the user directories under it)
    under the test's own."""
    env = shielded_child_env()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(
        {
            "BETTERMEMORY_DIR": str(left.directory),
            "BETTERMEMORY_KEYS_DIR": os.environ[KEYS_DIR_ENV],
            "BETTERMEMORY_STATE_DIR": str(tmp_path / "daemon-state"),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }
    )
    env.pop("BETTERMEMORY_WORKSPACE", None)
    return env


def _foreground_start(tmp_path: Path, left: Left, env: dict[str, str], n: int) -> str:
    """`bettermemory up --foreground --port 0` until it serves, then
    stopped through its shutdown endpoint; returns what it logged."""
    log_path = tmp_path / f"daemon-{n}.log"
    state_dir = Path(env["BETTERMEMORY_STATE_DIR"])
    with log_path.open("wb") as log_file:
        child = subprocess.Popen(
            [sys.executable, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
            env=env,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            state = None
            while time.monotonic() < deadline:
                state = read_state(state_dir, left.path)
                if state is not None and health(int(state["port"]), timeout=0.5):
                    break
                if child.poll() is not None:
                    raise AssertionError(
                        f"the daemon exited {child.returncode}: "
                        + log_path.read_text(encoding="utf-8", errors="replace")
                    )
                time.sleep(0.05)
            else:
                raise AssertionError("the daemon never answered")
            assert state is not None
            assert shutdown_daemon(state, state_dir=state_dir, timeout=20.0)
            assert child.wait(timeout=30) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)
    return log_path.read_text(encoding="utf-8", errors="replace")


def test_two_foreground_starts_run_the_migration_once(
    left: Left, tmp_path: Path
) -> None:
    """Design 8.6 at the daemon's open: `up --foreground` leaves the
    migration to the daemon, which runs it under the store's lock before
    it binds. The first start homes every record and logs one line; the
    second finds nothing to home and writes nothing."""
    env = _child_env(tmp_path, left)
    first = _foreground_start(tmp_path, left, env, 1)
    assert first.count("membership migrated:") == 1, first
    # Logged by the daemon, not printed by the CLI's context before the
    # lock was taken.
    migrated = first.index(
        f"[INFO] bettermemory.daemon: membership migrated: {left.records} records "
        "homed (3 memories, 0 removed memories, 1 episodes)"
    )
    # Before the port is bound: the daemon logs its port once it is.
    assert migrated < first.index("[INFO] bettermemory.daemon: bettermemory daemon ")
    assert unhomed_count(left.path) == 0
    assert migrated_rows(left.path) == 1
    rows = log_rows(left.path)
    second = _foreground_start(tmp_path, left, env, 2)
    assert "membership migrated" not in second, second
    assert log_rows(left.path) == rows
    assert migrated_rows(left.path) == 1
