"""`bettermemory up` names a held store lock when no daemon answered.

`ensure_daemon` starts no daemon while another process holds the store's
lock (a daemon starting, busy or hung): it waits for that one to answer
and returns None when none does within the wait. `up` reported that as
"no daemon could be started", which sent the reader looking for a start
that never happened. It now adds the shim's words for it, in the text and
in ``--json``'s ``error``, from a probe of the lock once the wait is over
(`_daemon_client.held_lock_reason`); `ensure_daemon` itself, and so the
hook's path, is unchanged. With the lock free the failure reads as before.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory._daemon_client import (
    held_lock_reason,
    lock_file_for,
    release_lock,
    resolve_state_dir,
    resolved_store_path,
    try_lock,
)
from bettermemory.server import main as cli_main

#: The wait `up` is given here, so a held lock costs the test a moment.
_WAIT = 0.3

_HELD = (
    "a process holds the store's lock (a daemon starting, busy or hung) "
    f"and none answered within {_WAIT:g} s"
)


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`bettermemory <argv>` in process; its exit code (0 when it returned)."""
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        cli_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def where(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(state dir, store path) as `up` resolves them, with a short wait and
    no daemon ever spawned."""
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(_daemon_client, "START_TIMEOUT_SECONDS", _WAIT)

    def spawn(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("a daemon was started beside the lock's holder")

    monkeypatch.setattr(_daemon_client, "_spawn", spawn)
    return resolve_state_dir(), resolved_store_path()


@pytest.fixture
def held(where: tuple[Path, Path]) -> Iterator[None]:
    """The store's lock, held by another open of it, as a daemon that is
    starting, busy or hung holds it."""
    handle = try_lock(lock_file_for(*where))
    assert handle is not None
    try:
        yield
    finally:
        release_lock(handle)


def test_up_names_the_held_lock_in_the_shims_words(
    where: tuple[Path, Path],
    held: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, store = where
    assert _main(["up"], monkeypatch) == 1
    assert capsys.readouterr().err == (
        f"bettermemory: no daemon could be started for {store}: {_HELD}\n"
    )


def test_up_json_names_the_held_lock_in_its_error(
    where: tuple[Path, Path],
    held: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, store = where
    assert _main(["up", "--json"], monkeypatch) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["state"] == "failed"
    assert failure["running"] is False
    assert failure["exit_status"] is None
    assert failure["error"] == (
        f"bettermemory: no daemon could be started for {store}: {_HELD}"
    )


def test_with_the_lock_free_the_failure_reads_as_before(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A start nobody answered, with nobody holding the lock, is not a held
    lock: the message stays the one `up` always gave."""
    state_dir, store = where
    # A lock file that exists, as every daemon that ran leaves one, and is
    # free.
    free = try_lock(lock_file_for(state_dir, store))
    assert free is not None
    release_lock(free)
    monkeypatch.setattr(_daemon_client, "ensure_daemon", lambda *a, **kw: None)
    assert _main(["up"], monkeypatch) == 1
    assert capsys.readouterr().err == (
        f"bettermemory: no daemon could be started for {store}\n"
    )


def test_the_reason_is_the_shims_and_the_probe_holds_nothing(
    where: tuple[Path, Path],
) -> None:
    """The shim's reconnect and `up` share this wording; the probe leaves a
    free lock free, so a daemon starting right after it takes it at once."""
    state_dir, store = where
    lock = lock_file_for(state_dir, store)
    # No lock file is a free lock, and the probe does not make one.
    assert held_lock_reason(state_dir, store, timeout=_WAIT) is None
    assert not lock.exists()
    # A lock file a daemon left behind, free: the probe gives it back.
    first = try_lock(lock)
    assert first is not None
    release_lock(first)
    assert held_lock_reason(state_dir, store, timeout=_WAIT) is None
    handle = try_lock(lock)
    assert handle is not None
    try:
        assert held_lock_reason(state_dir, store, timeout=_WAIT) == _HELD
        assert held_lock_reason(state_dir, store, timeout=8.0) == _HELD.replace(
            f"{_WAIT:g} s", "8 s"
        )
    finally:
        release_lock(handle)
