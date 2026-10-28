"""A key file that is there and cannot be read ends every command in one
line, not a traceback.

The owner's opener (`Store.open_or_create`, and `Store.open` with
``allow_rekey``) refuses to read an unreadable key as a missing one: it
would retire nothing, write a new key over the store's and rekey the log,
for a failure (a permission, a key or a key directory kept on a volume
that is not mounted) that may pass in a moment. It raises
`log.KeyUnreadableError` instead, which the CLI's openers turned into a
traceback. These tests pin what each entry path does now: one line on
stderr that names the key file, what reading it said and that nothing was
rekeyed, and the command's own code for a store it cannot open (1 where
it has none); the daemon writes the same line as the last of its log and
exits 1, which `up` quotes; the in-process hook commands still exit 0.
Each case checks that the key ring and the log are as they were.

The key is made unreadable three ways: a mode of 0 (POSIX, not as root),
a key file that is a symbolic link to nothing, and a keys directory that
is one.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import _daemon_client
from bettermemory import daemon as daemon_module
from bettermemory import log as chain
from bettermemory.cli import _common
from bettermemory.log import KeyRing, KeyUnreadableError
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

from .conftest import shielded_child_env

#: What the line says, whichever way the key cannot be read.
_NOT_REKEYED = "the store is not rekeyed over it"


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


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    """Every file of the ring but its locks, by name, read through any link."""
    return {
        path.name: path.read_bytes()
        for path in sorted(keys.glob(f"{store_id}.*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`bettermemory <argv>` in process: its exit code, 0 for a command
    that returns. Any other exception, a traceback's, propagates."""
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@dataclass
class Unreadable:
    """A store this host created, resolved by the CLI through
    BETTERMEMORY_DIR, whose key `make` leaves there and unreadable and
    `restore` gives back as it was."""

    how: str
    path: Path
    keys: Path
    store_id: str
    fingerprint: str
    reason: str
    ring_before: dict[str, bytes] = field(default_factory=dict)
    _undo: list[tuple[Path, Path]] = field(default_factory=list)

    @property
    def key_name(self) -> str:
        return f"{self.store_id}.key"

    def restore(self) -> None:
        if self.how == "mode":
            os.chmod(self.keys / self.key_name, 0o600)
        for moved, back in self._undo:
            if moved.exists():
                moved.rename(back)
        self._undo.clear()

    def assert_nothing_rekeyed(self) -> None:
        """The key ring and the log as they were before the command: no key
        retired or written, no binding or head moved, no rekey row."""
        self.restore()
        assert _ring_files(self.keys, self.store_id) == self.ring_before
        with Store.open(self.path, keys_dir=self.keys, allow_rekey=False) as reader:
            assert reader.has_key
            assert reader.meta()["key_fingerprint"] == self.fingerprint
            assert all(row.kind != chain.REKEY for row in reader.log_rows())


@pytest.fixture(params=["mode", "key_link", "dir_link"])
def unreadable(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Unreadable]:
    how = str(request.param)
    if how == "mode":
        _no_permission_bits()
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    # A link stands where the CLI looks for the keys directory (or for the
    # key in it), and the keys live on the "volume" it points at.
    volume = tmp_path / "volume"
    keys = keys_dir if how == "mode" else volume
    with Store.create(path, keys_dir=keys) as created:
        created.put_memory(_memory("written under the key"))
        store_id = created.store_id
        fingerprint = created.meta()["key_fingerprint"]
    reason = {"mode": "Permission denied"}.get(how, "symbolic link")
    case = Unreadable(how, path, keys, store_id, fingerprint, reason)
    if how == "key_link":
        keys_dir.mkdir(parents=True, exist_ok=True)
        for name in os.listdir(volume):
            if name != case.key_name:
                shutil.copyfile(volume / name, keys_dir / name)
        _symlink(keys_dir / case.key_name, volume / case.key_name, directory=False)
        case.keys = keys_dir
    elif how == "dir_link":
        link = tmp_path / "keys-link"
        _symlink(link, volume, directory=True)
        monkeypatch.setenv("BETTERMEMORY_KEYS_DIR", str(link))
        case.keys = link
    case.ring_before = _ring_files(case.keys, store_id)
    if how == "mode":
        os.chmod(case.keys / case.key_name, 0)
    else:
        unmounted = tmp_path / "unmounted"
        volume.rename(unmounted)
        case._undo.append((unmounted, volume))
    try:
        yield case
    finally:
        case.restore()


def _assert_the_line(err: str, case: Unreadable) -> str:
    """The one line on stderr, checked for what it must say."""
    assert "Traceback" not in err
    lines = err.splitlines()
    assert len(lines) == 1, err
    [line] = lines
    assert case.key_name in line
    assert "cannot be read" in line
    assert case.reason in line
    assert _NOT_REKEYED in line
    return line


# ---------------------------------------------------------------------------
# The CLI's openers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["health"], 1),
        (["rollback", "--by-actor", "some-client"], 1),
        (["serve"], 1),
        (["up", "--foreground", "--port", "0"], 1),
        (["membership", "migrate"], 2),
        (["membership", "migrate", "--dry-run"], 2),
    ],
)
def test_a_command_says_so_in_one_line_and_rekeys_nothing(
    unreadable: Unreadable,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
) -> None:
    """`cli_context` (every writing command, `up --foreground` among
    them), `serve` and `membership migrate`: the command's code for a
    store it cannot open, one line, and the key ring and log untouched."""

    def never(**_kwargs: object) -> int:
        raise AssertionError("a daemon served a store whose key it cannot read")

    monkeypatch.setattr(daemon_module, "serve", never)
    assert _run_cli(argv, monkeypatch) == code
    captured = capsys.readouterr()
    line = _assert_the_line(captured.err, unreadable)
    assert line.startswith("bettermemory: the key file ")
    assert captured.out == ""
    unreadable.assert_nothing_rekeyed()


def test_cli_context_ends_the_command_with_exit_1(
    unreadable: Unreadable, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        _common.cli_context()
    assert raised.value.code == 1
    _assert_the_line(capsys.readouterr().err, unreadable)
    unreadable.assert_nothing_rekeyed()


def test_the_shim_says_so_before_it_says_it_falls_back(
    unreadable: Unreadable,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """The daemon the shim started exited on the same key; the in-process
    fallback cannot open the store either, so the shim ends in its one
    line and exit 1, without the two warnings about a fallback."""
    from bettermemory import shim

    def exited(store_path: Path, *args: object, **kwargs: object) -> None:
        raise _daemon_client.DaemonExited(
            store_path, 1, "the daemon's own line", tmp_path / "daemon.log"
        )

    async def never(*_args: object) -> None:
        raise AssertionError("the shim forwarded to no daemon")

    monkeypatch.setattr(_daemon_client, "ensure_daemon", exited)
    monkeypatch.setattr(shim, "_serve_through_daemon", never)
    caplog.set_level(logging.INFO)
    with pytest.raises(SystemExit) as raised:
        shim.run_shim()
    assert raised.value.code == 1
    _assert_the_line(capsys.readouterr().err, unreadable)
    assert "in-process" not in caplog.text
    assert "daemon lookup failed" not in caplog.text
    unreadable.assert_nothing_rekeyed()


def test_the_shim_still_warns_first_when_its_fallback_fails_another_way(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an open that ends in a line of its own (a `SystemExit`) goes
    without the shim's warnings. A store the fallback cannot open for any
    other reason ends in its traceback, as it always did, with the
    daemon's failure and the log that failure names warned above it: the
    traceback alone would not say why no daemon was there."""
    from bettermemory import shim
    from bettermemory.cli import serve

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
    daemon_log = tmp_path / "daemon.log"

    def exited(store_path: Path, *args: object, **kwargs: object) -> None:
        raise _daemon_client.DaemonExited(
            store_path, 1, "the daemon's own line", daemon_log
        )

    def unopenable() -> serve.Opened:
        raise sqlite3.DatabaseError("file is not a database")

    def never(*_args: object) -> None:
        raise AssertionError("a store that did not open was served")

    monkeypatch.setattr(_daemon_client, "ensure_daemon", exited)
    monkeypatch.setattr(serve, "open_for_serve", unopenable)
    monkeypatch.setattr(serve, "run_serve", never)
    caplog.set_level(logging.WARNING)
    with pytest.raises(sqlite3.DatabaseError):
        shim.run_shim()
    assert "daemon lookup failed: DaemonExited" in caplog.text
    assert "the daemon's own line" in caplog.text
    assert str(daemon_log) in caplog.text
    assert "serving the store in-process" in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        ["session-start", "--session-id", "s1"],
        ["prompt-recall", "--prompt", "deploy steps", "--session-id", "s1"],
        ["audit-turn", "--session-id", "s1", "--dry-run"],
    ],
)
def test_an_in_process_hook_command_still_exits_0(
    unreadable: Unreadable,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """A hook never fails its turn, a key it cannot read included."""
    assert _run_cli(argv, monkeypatch) == 0
    assert "Traceback" not in capsys.readouterr().err
    unreadable.assert_nothing_rekeyed()


def test_the_line_stays_one_line_whatever_the_reason_holds(tmp_path: Path) -> None:
    """The reason quotes a link's target, a name anyone who can write the
    keys directory chose: a line break in it is printed as an escape."""
    exc = KeyUnreadableError(
        tmp_path / "k.key",
        FileNotFoundError(2, "No such file or directory"),
        reason="it is a symbolic link to /a\nb\x1b[2J, which is not there",
    )
    line = _common.key_unreadable_line(exc, prefix="bettermemory")
    assert "\n" not in line
    assert "\x1b" not in line
    assert "\\n" in line
    assert line.startswith(f"bettermemory: the key file {tmp_path / 'k.key'} ")


def test_a_missing_key_is_still_replaced_as_it_was(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a key that is there and unreadable ends the command: a store
    whose key is gone is rekeyed by the owner's opener, as before."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    with Store.create(directory / STORE_FILENAME, keys_dir=keys_dir) as created:
        store_id = created.store_id
    KeyRing(keys_dir, store_id).current_path().unlink()
    ctx = _common.cli_context()
    try:
        assert ctx.store.has_key
        assert any(row.kind == chain.REKEY for row in ctx.store.log_rows())
    finally:
        ctx.store.close()


# ---------------------------------------------------------------------------
# The daemon, as `up` and the hooks start it
# ---------------------------------------------------------------------------


def test_the_daemon_logs_one_line_and_up_quotes_it(
    unreadable: Unreadable, tmp_path: Path
) -> None:
    """A detached `up` spawns `up --foreground`, which exits 1 with the
    line as the last of its log and no traceback; `up` reports that exit,
    quoting the line, and exits 1. The keys directory reaches the spawned
    daemon as the link it is, not as the directory it pointed at, where
    the daemon found no key and rekeyed the store."""
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    env = shielded_child_env()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        APPDATA=str(home / "AppData" / "Roaming"),
        BETTERMEMORY_STATE_DIR=str(state_dir),
    )
    try:
        up = subprocess.run(
            [sys.executable, "-m", "bettermemory", "up", "--port", "0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        # A daemon that started after all (the failure under test) is
        # stopped, not left serving the test's store.
        for state_file in state_dir.glob("daemon-*.json"):
            state = _daemon_client.read_state_file(state_file)
            if state is not None:
                _daemon_client.shutdown_daemon(state, state_dir=state_dir, timeout=10)
    assert up.returncode == 1, up.stdout + up.stderr
    assert "exited with status 1 before it answered" in up.stderr
    line = _assert_the_line(up.stderr, unreadable)
    [log] = state_dir.glob("daemon-*.log")
    logged = log.read_text(encoding="utf-8")
    assert "Traceback" not in logged
    # Before it, at most the config's first-run notice (this home has none).
    said = [entry for entry in logged.splitlines() if unreadable.key_name in entry]
    assert len(said) == 1, logged
    assert logged.splitlines()[-1] == said[0]
    _assert_the_line(said[0], unreadable)
    # `up` quotes the log's last line, cut at `_last_log_line`'s bound.
    assert said[0][:200] in line
    unreadable.assert_nothing_rekeyed()
