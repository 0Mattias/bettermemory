"""A hook's state rows end before the harness kills the hook (ENG-108).

Claude Code kills a hook at the timeout the plugin's hooks.json gives it,
10 s for the prompt. A state row is tried twice, each try waiting up to
the 5 s busy timeout, so under a held lock it failed after about 11 s:
the hook was gone before the daemon answered, a row that landed late
recorded a recall the model never saw, and a failed brief said nothing
but an exit-0 stderr line. Now a hook body runs under a deadline
(`events.within_hook_budget`), its harness timeout as hooks.json and the
client share it (`_daemon_client.HOOK_HARNESS_TIMEOUT_SECONDS`) less a
margin; no row is tried past it, the hook still fails closed, and its one
stderr line says the store was busy, what was not shown, and to run
`bettermemory doctor`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import time
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client, events
from bettermemory.events import Recorder, StoreBusy
from bettermemory.recall import RecallState, session_brief
from bettermemory.store import Store

from .test_plugin import PLUGIN_HOOKS_PATH
from .test_recall_prompt import PromptWorld

SESSION = "transcript-recall"

# The harness's name for each hook event, by the client's.
_HARNESS_EVENTS = {
    "session-start": "SessionStart",
    "stop": "Stop",
    "prompt": "UserPromptSubmit",
}


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


@contextlib.contextmanager
def _locked(store: Store) -> Iterator[sqlite3.Connection]:
    holder = sqlite3.connect(store.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield holder
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()


def _short_budget(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Every hook's budget is `seconds`: the harness timeouts shrunk, the
    margin kept, so the deadline is read through the one table."""
    margin = _daemon_client.HOOK_MARGIN_SECONDS
    monkeypatch.setattr(
        _daemon_client,
        "HOOK_HARNESS_TIMEOUT_SECONDS",
        {name: seconds + margin for name in _HARNESS_EVENTS},
    )


def test_the_manifest_gives_each_hook_the_timeout_the_hook_reads() -> None:
    body = json.loads(PLUGIN_HOOKS_PATH.read_text(encoding="utf-8"))
    for name, harness in _HARNESS_EVENTS.items():
        (timeout,) = {
            hook["timeout"]
            for entry in body["hooks"][harness]
            for hook in entry.get("hooks", [])
            if hook.get("type") == "command"
        }
        assert timeout == _daemon_client.HOOK_HARNESS_TIMEOUT_SECONDS[name], name


def test_each_budget_ends_before_the_harness_or_the_client_gives_up() -> None:
    for name in _HARNESS_EVENTS:
        budget = _daemon_client.hook_budget_seconds(name)
        assert 0 < budget
        assert budget + _daemon_client.HOOK_MARGIN_SECONDS <= min(
            _daemon_client.HOOK_HARNESS_TIMEOUT_SECONDS[name],
            _daemon_client.HOOK_TIMEOUT_SECONDS,
        )
    assert _daemon_client.hook_budget_seconds("prompt") == 8.0


def test_a_recall_under_a_held_lock_fails_within_its_budget(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer takes the lock just as the `prompt_recall` row is
    written and keeps it. Measured with the real busy timeout: unbounded,
    the row's two tries wait about 10 s; under a 0.5 s budget the hook
    gives up within it, shows nothing and claims nothing."""
    memory = world.write()
    state = RecallState()
    store = world.store
    assert store.busy_timeout == 5.0
    _short_budget(monkeypatch, 0.5)
    real = store.record_event
    waited: list[float] = []

    def record_event(kind: str, **fields: Any) -> Any:
        if kind != "prompt_recall":
            return real(kind, **fields)
        with _locked(store):
            started = time.monotonic()
            try:
                return real(kind, **fields)
            finally:
                waited.append(time.monotonic() - started)

    with monkeypatch.context() as patched:
        patched.setattr(store, "record_event", record_event)
        with pytest.raises(StoreBusy) as raised:
            world.recall(state=state)
    (elapsed,) = waited
    assert elapsed < 1.0, elapsed
    said = str(raised.value)
    assert "the store was busy, so the recall was not shown" in said
    assert "`bettermemory doctor`" in said
    # The connection waits its own busy timeout again for the next call.
    assert world.store.busy_timeout == 5.0
    assert world.recall(state=state).ids == (memory.id,)


def test_a_brief_under_a_held_lock_fails_within_its_budget(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write()
    _short_budget(monkeypatch, 0.5)
    with _locked(world.store):
        started = time.monotonic()
        with pytest.raises(StoreBusy, match="the brief was not shown"):
            session_brief(
                session_id=SESSION,
                source="startup",
                origin=world.caller,
                config=world.config,
                store=world.store,
                state=RecallState(),
            )
    assert time.monotonic() - started < 2.0


def test_no_try_begins_past_the_deadline(store: Store) -> None:
    """Past the deadline a row is tried once, without waiting for the
    lock, and not again; without one it waits each try's busy timeout."""
    store.set_busy_timeout(5.0)
    with _locked(store):
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.record_event(
                "show",
                session="s1",
                best_effort=False,
                attempts=events.STATE_WRITE_ATTEMPTS,
                deadline=time.monotonic() - 1.0,
                id="m-1",
            )
        assert time.monotonic() - started < 1.0
    assert store.busy_timeout == 5.0


def test_a_row_inside_the_budget_lands_when_the_lock_is_given_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _short_budget(monkeypatch, 3.0)
    recorder = Recorder(store=store, session_id="s1", enabled=False)
    with events.within_hook_budget("prompt"):
        assert events.deadline() is not None
        recorder.record("show", id="m-1")
    assert events.deadline() is None
    assert [e["id"] for e in store.iter_events()] == ["m-1"]


def test_an_error_that_is_not_a_busy_store_passes_through() -> None:
    with pytest.raises(ValueError, match="other"):
        with events.within_hook_budget("prompt"):
            raise ValueError("other")


def test_the_client_prints_the_daemons_account_of_a_failed_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The daemon answers a failed hook body 500 with the error in its
    body; the client's one stderr line is that account, not the status."""
    said = (
        "StoreBusy: the store was busy, so the recall was not shown: another "
        "writer held it past this hook's time; run `bettermemory doctor`"
    )
    monkeypatch.setattr(
        _daemon_client, "resolved_store_path", lambda: tmp_path / "store.db"
    )
    monkeypatch.setattr(
        _daemon_client, "ensure_daemon", lambda *a, **k: {"port": 1, "token": "t"}
    )

    def failed(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:1/api/v1/hooks/prompt",
            500,
            "Internal Server Error",
            Message(),
            io.BytesIO(json.dumps({"stdout": "", "error": said}).encode()),
        )

    monkeypatch.setattr(_daemon_client, "post", failed)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert _daemon_client.hook_main(["hook", "prompt"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"bettermemory hook prompt: {said}\n"
