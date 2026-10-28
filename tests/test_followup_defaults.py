"""Three follow-ups on defaults and discovery, each pinned with the
behaviour it promises:

1. **The pinned-key notice is for a choice.** A user line for a key the
   managed file pins is logged once as ignored, unless the line spells the
   code's own default for the key: it restates the default and expresses
   no choice the pin overrides. `config check` and `config show` still
   report the line, since they read the files themselves.
2. **The first-run config.toml pins nothing.** Every setting in
   `DEFAULT_CONFIG` is commented out with its prose kept, so a fresh
   install loads exactly `Config()` and a later release's changed default
   reaches it. A file that exists is never rewritten.
3. **No second daemon beside a lock holder.** While another process holds
   the store's lock, `ensure_daemon` and the shim's reconnect start no
   daemon: they wait, within their timeout, for the holder to answer and
   prove itself, and give up at the timeout. With the lock free the start
   is what it was.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory import config as _config
from bettermemory._daemon_client import (
    lock_file_for,
    package_version,
    read_state,
    release_lock,
    try_lock,
    write_state,
)
from bettermemory.cli import config_cmd
from bettermemory.config import DEFAULT_CONFIG, Config, load_config

from .test_daemon_identity import TOKEN, _another_store, _Listener, _proving, _state


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _pin_notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "pins" in r.getMessage()]


# ---------------------------------------------------------------------------
# 1. The pinned-key notice
# ---------------------------------------------------------------------------


def test_a_pinned_line_restating_the_default_draws_no_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`[telemetry] enabled = true` is in every config.toml an earlier
    release wrote, and it is the default: a managed file that pins
    telemetry off overrides it without a notice on every process."""
    managed_config_path.write_text("[telemetry]\nenabled = false\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, "[telemetry]\nenabled = true\n"))
    assert loaded.telemetry.enabled is False
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})
    assert _pin_notices(caplog) == []


def test_a_pinned_line_choosing_another_value_still_draws_the_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text("[daemon]\nport = 7400\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, "[daemon]\nport = 7401\n"))
    assert loaded.daemon.port == 7400
    notices = _pin_notices(caplog)
    assert len(notices) == 1, notices
    assert "[daemon] `port`" in notices[0]


@pytest.mark.parametrize(
    ("managed", "user", "notice"),
    [
        # The default spelled as written in an earlier DEFAULT_CONFIG.
        ("[daemon]\nport = 7400\n", "[daemon]\nport = 7397\n", False),
        # An int restates a float default of the same value.
        (
            "[behavior]\nrecency_boost_half_life_days = 7.0\n",
            "[behavior]\nrecency_boost_half_life_days = 30\n",
            False,
        ),
        ('[scopes]\nallowed = ["corp"]\n', "[scopes]\nallowed = []\n", False),
        (
            '[behavior]\nsearch_mode = "bm25"\n',
            '[behavior]\nsearch_mode = "hybrid"\n',
            False,
        ),
        # A boolean restates only a boolean, though Python takes False == 0.
        (
            "[behavior]\ntombstone_retention_days = 9\n",
            "[behavior]\ntombstone_retention_days = false\n",
            True,
        ),
        # A spelling the loader would coerce is the user's choice of it.
        ("[telemetry]\nenabled = false\n", '[telemetry]\nenabled = "true"\n', True),
        # The legacy prompt_recall boolean is not the default's spelling.
        (
            '[behavior]\nprompt_recall = "off"\n',
            "[behavior]\nprompt_recall = true\n",
            True,
        ),
    ],
)
def test_only_a_line_spelling_the_code_default_is_a_restatement(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    user: str,
    notice: bool,
) -> None:
    managed_config_path.write_text(managed, "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        load_config(_user_config(tmp_path, user))
    assert bool(_pin_notices(caplog)) is notice, _pin_notices(caplog)


def test_check_and_show_still_report_a_pinned_line_restating_the_default(
    managed_config_path: Path,
) -> None:
    """The commands read the files, not the log: the restating line is
    still a `pinned` note in `config check` and the user's value is still
    named in `config show`."""
    managed_config_path.write_text("[telemetry]\nenabled = false\n", "utf-8")
    _config.default_config_path().write_text(
        "[telemetry]\nenabled = true\n", encoding="utf-8"
    )
    report = config_cmd.check_report()
    pinned = [f for f in report["findings"] if f["code"] == "pinned"]
    assert [(f["level"], f["section"], f["key"]) for f in pinned] == [
        ("note", "telemetry", "enabled")
    ]
    assert report["exit_code"] == 0
    entry = config_cmd.show_report()["settings"]["telemetry"]["enabled"]
    assert entry == {"value": False, "source": "managed", "user_value": True}


# ---------------------------------------------------------------------------
# 2. The first-run config.toml
# ---------------------------------------------------------------------------


def _setting_keys() -> list[tuple[str, str]]:
    """Every (section, key) `Config` reads, from its dataclasses."""
    keys: list[tuple[str, str]] = []
    for section in dataclasses.fields(Config):
        value = getattr(Config(), section.name)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            keys.extend((section.name, f.name) for f in dataclasses.fields(value))
    return keys


def test_the_shipped_config_sets_no_key() -> None:
    """Each section header stays, with nothing under it but comments."""
    parsed = tomllib.loads(DEFAULT_CONFIG)
    assert set(parsed) == {section for section, _ in _setting_keys()}
    assert all(table == {} for table in parsed.values()), parsed


def test_every_setting_is_in_the_shipped_config_commented_out() -> None:
    """A user who opens the file finds every setting, under its section,
    ready to uncomment."""
    for section, key in _setting_keys():
        block = DEFAULT_CONFIG.split(f"\n[{section}]\n")[1].split("\n[")[0]
        assert f"\n# {key} = " in block, (section, key)
        assert f"\n{key} =" not in block, (section, key)


def test_the_shipped_config_loads_as_exactly_the_code_defaults(
    tmp_path: Path,
) -> None:
    loaded = load_config(_user_config(tmp_path, DEFAULT_CONFIG))
    assert dataclasses.replace(loaded, config_path=None) == Config()


def test_a_fresh_install_follows_a_changed_code_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the first-run file restates a default, so the default a
    release ships is the one in force. The loader's default for `[daemon]
    port` is patched here to stand in for a release that changes it."""
    path = tmp_path / "config.toml"
    load_config(path)
    real = _config._coerce_int

    def shifted(value: object, default: int, **kwargs: Any) -> int:
        if kwargs.get("label") == "[daemon] port":
            default = 7500
        return real(value, default, **kwargs)

    monkeypatch.setattr(_config, "_coerce_int", shifted)
    assert load_config(path).daemon.port == 7500


def test_the_first_run_writes_the_commented_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    load_config(path)
    assert path.read_text(encoding="utf-8") == DEFAULT_CONFIG


def test_an_existing_config_is_never_rewritten(tmp_path: Path) -> None:
    """A config.toml an earlier release wrote, every key spelled at that
    release's default, is read as it is and left byte for byte."""
    earlier = (
        "# bettermemory config\n[behavior]\ndefault_max_results = 5\n"
        "search_mode = 'keyword'\n[daemon]\nport = 7397\n"
    )
    path = _user_config(tmp_path, earlier)
    before = path.stat().st_mtime_ns
    loaded = load_config(path)
    assert loaded.behavior.search_mode == "keyword"
    assert path.read_text(encoding="utf-8") == earlier
    assert path.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# 3. No second daemon beside a lock holder
# ---------------------------------------------------------------------------


@pytest.fixture
def started_listeners() -> Iterator[list[_Listener]]:
    made: list[_Listener] = []
    yield made
    for listener in made:
        listener.stop()


class _Spawns:
    """`_spawn` replaced: each call recorded with the time it came, and
    refused, so no daemon is ever started here."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.at: list[float] = []
        monkeypatch.setattr(_daemon_client, "_spawn", self)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.at.append(time.monotonic())
        raise OSError("no daemon is started in this test")


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> _Spawns:
    return _Spawns(monkeypatch)


@pytest.fixture
def store_file(tmp_path: Path) -> Path:
    return tmp_path / "memory.sqlite"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def held(state_dir: Path, store_file: Path) -> Iterator[list[Any]]:
    """The store's lock, held by another open of it, as a daemon that is
    starting or busy holds it. A test may release it early (the list is
    emptied then)."""
    handle = try_lock(lock_file_for(state_dir, store_file))
    assert handle is not None
    holder = [handle]
    yield holder
    for remaining in holder:
        release_lock(remaining)


def _ensure(state_dir: Path, store_file: Path, timeout: float) -> dict[str, Any] | None:
    return _daemon_client.ensure_daemon(
        state_dir, store_file, version=package_version(), timeout=timeout, env={}
    )


def _after(seconds: float, action: Any) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.start()
    return timer


def test_a_silent_holder_gets_no_daemon_beside_it(
    state_dir: Path, store_file: Path, held: list[Any], spawns: _Spawns
) -> None:
    """Nothing answers and the lock stays held: the start gives up at the
    timeout, as a start nobody answered does, having started nothing."""
    began = time.monotonic()
    assert _ensure(state_dir, store_file, timeout=0.6) is None
    assert time.monotonic() - began >= 0.6
    assert spawns.at == []


def test_a_slow_holder_is_waited_for_and_used_once_it_proves_itself(
    state_dir: Path,
    store_file: Path,
    held: list[Any],
    spawns: _Spawns,
    started_listeners: list[_Listener],
) -> None:
    """The holder writes its state file and listens a moment after the
    client looked: the client answers with it, and its health request
    carried a nonce the holder proved."""

    def come_up() -> None:
        listener = _Listener(_proving(TOKEN, store_file))
        started_listeners.append(listener)
        write_state(state_dir, _state(listener.port, store_file))

    timer = _after(0.3, come_up)
    try:
        found = _ensure(state_dir, store_file, timeout=10.0)
    finally:
        timer.join()
    assert found is not None
    assert found["port"] == started_listeners[0].port
    assert spawns.at == []
    assert started_listeners[0].gets
    assert all("nonce=" in path for path in started_listeners[0].gets)


def test_a_holder_that_does_not_prove_itself_is_never_taken(
    state_dir: Path,
    store_file: Path,
    held: list[Any],
    spawns: _Spawns,
    started_listeners: list[_Listener],
) -> None:
    """Something answers at the state file's port without the proof: it is
    not the holder, nothing is posted to it, and the wait runs out."""
    listener = _Listener(_another_store())
    started_listeners.append(listener)
    write_state(state_dir, _state(listener.port, store_file))
    assert _ensure(state_dir, store_file, timeout=0.6) is None
    assert spawns.at == []
    assert listener.posts == []


def test_the_start_goes_ahead_once_the_holder_lets_the_lock_go(
    state_dir: Path, store_file: Path, held: list[Any], spawns: _Spawns
) -> None:
    """The holder exits without ever answering: with the lock free the
    client starts a daemon, within the same timeout, as it always did."""
    released: list[float] = []

    def let_go() -> None:
        # The time is taken before the release: the client may see the
        # lock free and start before this thread runs again.
        released.append(time.monotonic())
        release_lock(held.pop())

    timer = _after(0.3, let_go)
    try:
        assert _ensure(state_dir, store_file, timeout=10.0) is None
    finally:
        timer.join()
    assert len(spawns.at) == 1
    assert spawns.at[0] >= released[0]


def test_with_the_lock_free_the_start_is_immediate_and_makes_no_lock_file(
    state_dir: Path, store_file: Path, spawns: _Spawns
) -> None:
    """The free-lock path is unchanged: one start, at once, and the probe
    creates no lock file of its own."""
    began = time.monotonic()
    assert _ensure(state_dir, store_file, timeout=10.0) is None
    assert len(spawns.at) == 1
    assert spawns.at[0] - began < 2.0
    assert not lock_file_for(state_dir, store_file).exists()


def test_the_shims_reconnect_starts_no_daemon_beside_a_holder(
    tmp_path: Path,
    state_dir: Path,
    store_file: Path,
    held: list[Any],
    spawns: _Spawns,
    started_listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim's `find()`, which starts a daemon when its own went away,
    waits for the holder too and answers with it once it proves itself."""
    from bettermemory import shim

    store_file.write_bytes(b"")
    monkeypatch.setattr(_daemon_client, "resolved_store_path", lambda: store_file)
    monkeypatch.setattr(_daemon_client, "resolve_state_dir", lambda: state_dir)
    monkeypatch.setattr(
        _daemon_client, "ensure_daemon", lambda *a, **k: {"port": 1, "pid": 1}
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    found: list[tuple[dict[str, Any] | None, str | None]] = []

    async def serve(state: dict[str, Any], version: str, find: Any) -> None:
        def come_up() -> None:
            listener = _Listener(_proving(TOKEN, store_file))
            started_listeners.append(listener)
            write_state(state_dir, _state(listener.port, store_file))

        timer = _after(0.3, come_up)
        try:
            found.append(find())
        finally:
            timer.join()

    monkeypatch.setattr(shim, "_serve_through_daemon", serve)
    shim.run_shim()
    [(state, error)] = found
    assert error is None
    assert state is not None and state["port"] == started_listeners[0].port
    assert spawns.at == []
    assert read_state(state_dir, store_file) == state


class _Running:
    """A spawned child that never exits and never answers."""

    returncode = None

    def poll(self) -> None:
        return None


def _shim_find_once(
    state_dir: Path, store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any] | None, str | None]:
    """What the shim's `find()` answers on one reconnect, with a short
    start timeout."""
    from bettermemory import shim

    store_file.write_bytes(b"")
    monkeypatch.setattr(_daemon_client, "START_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(_daemon_client, "resolved_store_path", lambda: store_file)
    monkeypatch.setattr(_daemon_client, "resolve_state_dir", lambda: state_dir)
    monkeypatch.setattr(
        _daemon_client, "ensure_daemon", lambda *a, **k: {"port": 1, "pid": 1}
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    found: list[tuple[dict[str, Any] | None, str | None]] = []

    async def serve(state: dict[str, Any], version: str, find: Any) -> None:
        found.append(find())

    monkeypatch.setattr(shim, "_serve_through_daemon", serve)
    shim.run_shim()
    [answer] = found
    return answer


def test_the_shims_reason_names_a_silent_holder_not_a_start(
    state_dir: Path,
    store_file: Path,
    held: list[Any],
    spawns: _Spawns,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beside a holder that never answers the reconnect starts nothing,
    so the reason it gives the request names the held lock, not a start
    that did not happen."""
    state, why = _shim_find_once(state_dir, store_file, monkeypatch)
    assert state is None
    assert spawns.at == []
    assert why is not None
    assert "holds the store's lock" in why
    assert "of a start" not in why


def test_the_shims_reason_still_names_a_start_nobody_answered(
    tmp_path: Path,
    state_dir: Path,
    store_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the lock free a daemon is started; when it never answers, the
    reason is the start nobody answered, as it was."""
    starts: list[Path] = []

    def spawn(state_dir: Path, store_path: Path, **kwargs: Any) -> Any:
        starts.append(Path(store_path))
        return _daemon_client.Started(
            child=_Running(),  # type: ignore[arg-type]
            log=tmp_path / "daemon.log",
            log_offset=0,
        )

    monkeypatch.setattr(_daemon_client, "_spawn", spawn)
    state, why = _shim_find_once(state_dir, store_file, monkeypatch)
    assert state is None
    assert starts == [store_file]
    assert why == "none answered within 0.4 s of a start"
