"""The managed config's registry value is read across a Group Policy
rewrite (ENG-93 item 10).

A Group Policy Preferences registry item with the Replace action, and a
refresh of registry-based policy, delete the value and write it again; a
config load in that gap read no managed config, and a daemon started
then ran its whole life without the pins. Four contracts, each pinned
with the behaviour it promises:

1. **Across the gap.** A key found without its value, written within
   `managed._REWRITE_RECENT` seconds, is read again after each pause in
   `managed._REWRITE_PAUSES` (half a second in all) until the value is
   there, the key gone on a later read or deleted under an open read
   included.
2. **No wait without a rewrite.** A key absent at the first read is no
   managed config at once, as on every machine without one; so is an
   older key without its value (a retired one), and a value there at the
   first read is read at once.
3. **Bounded.** A key still without its value after the window is no
   managed config, with one warning per process naming the key; a later
   load in the process that finds the same write does not wait again.
4. **Still fail-closed.** Any other failure of the registry during the
   window stops the load as before, and `config check` reads across the
   gap as the loader does.

No test waits: `managed._sleep` is replaced with a recorder, and the
registry is a fake that plays back one state per read, with the key's
last-write time set per test (`FakeRegistry.last_write`, now by default).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import managed as _managed
from bettermemory.cli import config_cmd
from bettermemory.config import load_config

from .test_managed_windows import _KEY, KEY_READ, REG_SZ, FakeRegistry, _Key

_TEXT = "[telemetry]\nenabled = false\n"


class _KeyDeleted(OSError):
    """What winreg raises for a handle on a key deleted after it was
    opened: Windows' ERROR_KEY_DELETED."""

    winerror = 1018


class GapRegistry(FakeRegistry):
    """A registry whose value is being rewritten. Each read (an OpenKey,
    then a QueryValueEx) plays the next of `states`, and the last one holds
    from then on: ``value`` (there), ``no value`` (the key without it),
    ``no key``, ``deleted`` (the key deleted under the open read) or
    ``denied`` (an error that is no absence)."""

    def __init__(self, *states: str) -> None:
        super().__init__()
        self.states = list(states)
        self.reads = 0

    def _state(self) -> str:
        return self.states[min(self.reads, len(self.states)) - 1]

    def _open_key(
        self, hive: int, sub_key: str, reserved: int = 0, access: int = KEY_READ
    ) -> _Key:
        self.reads += 1
        state = self._state()
        if state == "no key":
            self.keys.pop(_KEY, None)
        elif state == "no value":
            self.keys[_KEY] = {}
        else:
            self.set(_TEXT, REG_SZ)
        return super()._open_key(hive, sub_key, reserved, access)

    def _query_value(self, key: _Key, name: str) -> tuple[Any, int]:
        state = self._state()
        if state == "deleted":
            raise _KeyDeleted(22, "the key has been marked for deletion")
        if state == "denied":
            raise PermissionError(13, "Access is denied")
        return super()._query_value(key, name)

    def _query_info(self, key: _Key) -> tuple[int, int, int]:
        if self._state() == "deleted":
            raise _KeyDeleted(22, "the key has been marked for deletion")
        return super()._query_info(key)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The pauses the reader took, recorded instead of waited."""
    slept: list[float] = []
    monkeypatch.setattr(_managed, "_sleep", slept.append)
    monkeypatch.setattr(_managed, "_WAITED_OUT", {})
    return slept


def _windows(monkeypatch: pytest.MonkeyPatch, *states: str) -> GapRegistry:
    """This process as Windows runs it, over a registry playing `states`."""
    fake = GapRegistry(*states)
    monkeypatch.setattr(_managed, "_winreg", lambda: fake.module)
    monkeypatch.setattr(_managed, "_platform", lambda: "win32")
    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    return fake


def _written(seconds_ago: float) -> int:
    """A last-write time `seconds_ago` before now, as a FILETIME."""
    return int((time.time() - seconds_ago + 11_644_473_600) * 10**7)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "without its" in r.getMessage()]


def _user_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[telemetry]\nenabled = true\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Across the gap
# ---------------------------------------------------------------------------


def test_a_value_read_empty_once_then_present_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The gap a Replace leaves: the first read finds the key without
    its value, the next finds the value, and the pins are in force."""
    fake = _windows(monkeypatch, "no value", "value")
    loaded = load_config(_user_config(tmp_path))
    assert loaded.telemetry.enabled is False
    assert loaded.managed_path == _managed.MANAGED_REGISTRY
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})
    assert fake.reads == 2
    assert sleeps == [0.05]


def test_the_value_is_awaited_for_the_whole_window(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = _windows(
        monkeypatch, "no value", "no value", "no value", "no value", "value"
    )
    assert _managed.read_registry_text() == _TEXT
    assert fake.reads == 5
    assert sleeps == list(_managed._REWRITE_PAUSES)
    assert sum(sleeps) == pytest.approx(0.5)


def test_a_rewrite_of_the_whole_key_is_read_across(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Once the key was found, a later read that finds it gone is the
    same rewrite, not the end of the managed config."""
    _windows(monkeypatch, "no value", "no key", "value")
    assert _managed.read_registry_text() == _TEXT
    assert sleeps == [0.05, 0.1]


def test_a_key_deleted_under_an_open_read_is_read_again(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    _windows(monkeypatch, "deleted", "value")
    assert _managed.read_registry_text() == _TEXT
    assert sleeps == [0.05]


# ---------------------------------------------------------------------------
# 2. No wait without a rewrite
# ---------------------------------------------------------------------------


def test_an_absent_key_is_no_managed_config_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Every machine without a managed config: one read, no pause."""
    fake = _windows(monkeypatch, "no key", "value")
    loaded = load_config(_user_config(tmp_path))
    assert loaded.managed_path is None
    assert loaded.telemetry.enabled is True
    assert fake.reads == 1
    assert sleeps == []


def test_an_old_key_without_its_value_is_no_managed_config_at_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retired managed config: the value removed with ``reg delete /v``
    or a policy set back to Not Configured, the key left behind. A hook
    loads the config on every tool call, so this costs one read, no
    pause and no warning."""
    fake = _windows(monkeypatch, "no value")
    fake.last_write = _written(3600)
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path))
    assert loaded.managed_path is None
    assert loaded.telemetry.enabled is True
    assert fake.reads == 1
    assert sleeps == []
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("seconds_ago", "waits"),
    [
        (0.0, True),
        (_managed._REWRITE_RECENT - 1, True),
        (_managed._REWRITE_RECENT + 1, False),
    ],
)
def test_only_a_key_written_within_the_window_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    seconds_ago: float,
    waits: bool,
) -> None:
    fake = _windows(monkeypatch, "no value", "value")
    fake.last_write = _written(seconds_ago)
    assert _managed.read_registry_text() == (_TEXT if waits else None)
    assert sleeps == ([0.05] if waits else [])


def test_a_value_there_at_once_is_read_without_waiting(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = _windows(monkeypatch, "value")
    assert _managed.read_registry_text() == _TEXT
    assert fake.reads == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# 3. Bounded
# ---------------------------------------------------------------------------


def test_a_key_left_without_its_value_is_no_managed_config_with_one_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The second load finds the write the first waited out: one read,
    no pause, no second warning."""
    fake = _windows(monkeypatch, "no value")
    fake.last_write = _written(1)
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        first = load_config(_user_config(tmp_path))
        second = load_config(_user_config(tmp_path))
    assert first.managed_path is None and second.managed_path is None
    assert first.telemetry.enabled is True
    assert fake.reads == 1 + len(_managed._REWRITE_PAUSES) + 1
    assert sleeps == list(_managed._REWRITE_PAUSES)
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "HKLM\\SOFTWARE\\Policies\\bettermemory" in warnings[0]
    assert "ManagedConfig value after 0.5 s" in warnings[0]


def test_a_later_write_without_its_value_is_waited_for_again(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The memo is per write: a key rewritten since the window was waited
    out is a new rewrite, waited for again, with no second warning."""
    fake = _windows(monkeypatch, "no value")
    fake.last_write = _written(2)
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        assert _managed.read_registry_text() is None
        fake.last_write = _written(1)
        assert _managed.read_registry_text() is None
    assert sleeps == 2 * list(_managed._REWRITE_PAUSES)
    assert len(_warnings(caplog)) == 1


def test_a_key_gone_by_the_end_of_the_window_warns_nothing(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key deleted whole while the value was awaited: the managed
    config was retired, and nothing is left to warn about."""
    _windows(monkeypatch, "no value", "no key")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        assert _managed.read_registry_text() is None
    assert sleeps == list(_managed._REWRITE_PAUSES)
    assert caplog.records == []
    assert _managed._WAITED_OUT == {}


# ---------------------------------------------------------------------------
# 4. Still fail-closed
# ---------------------------------------------------------------------------


def test_another_failure_during_the_window_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    _windows(monkeypatch, "no value", "denied")
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    assert "could not be read" in str(caught.value)
    assert str(_managed.MANAGED_REGISTRY) in str(caught.value)
    assert sleeps == [0.05]


def test_config_check_reads_across_the_gap(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    _windows(monkeypatch, "no value", "value")
    report = config_cmd.check_report()
    managed = report["files"][1]
    assert managed["path"] == str(_managed.MANAGED_REGISTRY)
    assert managed["exists"] is True and managed["parses"] is True
    assert report["loads"] is True
    assert sleeps == [0.05]
