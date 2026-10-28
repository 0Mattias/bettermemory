"""The organisation-managed config on Windows: a registry value, not a file.

Four contracts, each pinned with the behaviour it promises:

1. **Where it is read.** On Windows the managed TOML is the value
   ``ManagedConfig`` under ``HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\bettermemory``,
   read through the 64-bit registry view. No file is read there, nothing
   under ProgramData above all, and no environment variable moves it. The
   other platforms read their file exactly as before.
2. **What the text gets.** A REG_SZ, or a REG_MULTI_SZ whose strings are
   its lines, goes through the managed file's own checks: one text pins
   the same settings, or fails with the same error, from either place.
3. **When it fails closed.** An absent key or value is the only "no
   managed config". A key that cannot be opened, a value that cannot be
   read, or a value of another type stops the load with an error naming
   the value.
4. **What the reports say.** `config path`, `config show` and `config
   check` name the source ``registry HKLM\\SOFTWARE\\Policies\\bettermemory\\
   ManagedConfig``, and the shipped example's Windows instructions deploy a
   value that loads.

No test here touches the machine's registry, on Windows or anywhere else.
An autouse fixture hands every test a fake `winreg` module through
`managed._winreg`, and the Windows selection goes through `managed._platform`,
so every test runs on every platform.
"""

from __future__ import annotations

import logging
import re
import sys
import types
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from bettermemory import config as _config
from bettermemory import managed as _managed
from bettermemory.cli import config_cmd
from bettermemory.config import Config, load_config

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "managed.toml"
#: The seams as production has them, taken at import time, before the
#: fixtures replace them for each test.
_REAL_MANAGED_CONFIG_PATH = _managed.managed_config_path
_REAL_WINREG = _managed._winreg
#: How every report and error names Windows' managed config.
_LABEL = "registry HKLM\\SOFTWARE\\Policies\\bettermemory\\ManagedConfig"
_KEY = "SOFTWARE\\Policies\\bettermemory"

# winreg's numbers, as the module has them on Windows.
HKEY_LOCAL_MACHINE = 0x80000002
KEY_READ = 0x20019
KEY_WOW64_64KEY = 0x0100
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_MULTI_SZ = 7


class _Key:
    """An open registry key: a context manager, as winreg's handle is."""

    def __init__(self, values: dict[str, tuple[Any, int]]) -> None:
        self.values = values

    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeRegistry:
    """HKEY_LOCAL_MACHINE as the reader sees it, behind a module that has
    winreg's names. Values live in the 64-bit view only: a reader that
    opens a key without ``KEY_WOW64_64KEY`` is a 32-bit process on a
    Windows that redirects the key under WOW6432Node (none does today,
    ``SOFTWARE\\Policies`` being shared), where an administrator wrote
    nothing."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[Any, int]]] = {}
        self.open_error: OSError | None = None
        self.query_error: OSError | None = None
        self.opened: list[tuple[int, str, int, int]] = []
        self.module = types.ModuleType("winreg")
        for name, number in (
            ("HKEY_LOCAL_MACHINE", HKEY_LOCAL_MACHINE),
            ("KEY_READ", KEY_READ),
            ("KEY_WOW64_64KEY", KEY_WOW64_64KEY),
            ("REG_SZ", REG_SZ),
            ("REG_EXPAND_SZ", REG_EXPAND_SZ),
            ("REG_BINARY", REG_BINARY),
            ("REG_DWORD", REG_DWORD),
            ("REG_MULTI_SZ", REG_MULTI_SZ),
        ):
            setattr(self.module, name, number)
        setattr(self.module, "OpenKey", self._open_key)
        setattr(self.module, "QueryValueEx", self._query_value)

    def set(self, value: Any, kind: int, *, name: str = "ManagedConfig") -> None:
        self.keys.setdefault(_KEY, {})[name] = (value, kind)

    def _open_key(
        self, hive: int, sub_key: str, reserved: int = 0, access: int = KEY_READ
    ) -> _Key:
        self.opened.append((hive, sub_key, reserved, access))
        if self.open_error is not None:
            raise self.open_error
        if (
            hive != HKEY_LOCAL_MACHINE
            or not access & KEY_WOW64_64KEY
            or sub_key not in self.keys
        ):
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return _Key(self.keys[sub_key])

    def _query_value(self, key: _Key, name: str) -> tuple[Any, int]:
        if self.query_error is not None:
            raise self.query_error
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return key.values[name]


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    """Every test here reads a fake registry, never the machine's."""
    fake = FakeRegistry()
    monkeypatch.setattr(_managed, "_winreg", lambda: fake.module)
    return fake


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry) -> FakeRegistry:
    """This process as Windows runs it: the production seams, with the
    platform reading win32 and the fake registry behind them."""
    monkeypatch.setattr(_managed, "_platform", lambda: "win32")
    monkeypatch.setattr(_managed, "managed_config_path", _REAL_MANAGED_CONFIG_PATH)
    return registry


def _user_config(tmp_path: Path, text: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _settings(config: Config) -> dict[str, object]:
    """Every setting of `config`, without where it was read from."""
    out = asdict(config)
    out.pop("config_path")
    out.pop("managed_path")
    return out


def _notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# 1. Where it is read
# ---------------------------------------------------------------------------


def test_windows_reads_the_registry_value_and_no_file(windows: FakeRegistry) -> None:
    assert _managed.system_path("win32") is None
    assert _managed.managed_config_path() is None
    source = _managed.managed_config_source()
    assert source == _managed.MANAGED_REGISTRY
    assert str(source) == _LABEL


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", Path("/Library/Application Support/bettermemory/managed.toml")),
        ("linux", Path("/etc/bettermemory/managed.toml")),
        ("freebsd14", Path("/etc/bettermemory/managed.toml")),
    ],
)
def test_every_other_platform_still_reads_its_file(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: Path
) -> None:
    monkeypatch.setattr(_managed, "_platform", lambda: platform)
    monkeypatch.setattr(_managed, "managed_config_path", _REAL_MANAGED_CONFIG_PATH)
    assert _managed.managed_config_source() == expected


def test_the_suite_fixture_keeps_every_other_test_off_the_registry(
    tmp_path: Path,
    managed_config_path: Path,
    registry: FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conftest fixture points the file seam at a scratch path on
    every platform, and the file seam decides first, so a test that does
    not ask for the registry never opens it, on Windows either."""
    monkeypatch.setattr(_managed, "_platform", lambda: "win32")
    assert _managed.managed_config_source() == managed_config_path
    load_config(_user_config(tmp_path))
    assert registry.opened == []


def test_the_value_is_read_from_hklm_through_the_64_bit_view(
    tmp_path: Path, windows: FakeRegistry
) -> None:
    """The fake keeps its values in the 64-bit view: without
    ``KEY_WOW64_64KEY`` a 32-bit Python would find nothing and run
    without the pins."""
    windows.set("[telemetry]\nenabled = false\n", REG_SZ)
    loaded = load_config(_user_config(tmp_path))
    assert loaded.telemetry.enabled is False
    assert windows.opened == [(HKEY_LOCAL_MACHINE, _KEY, 0, KEY_READ | KEY_WOW64_64KEY)]


def test_program_data_is_never_read(
    tmp_path: Path, windows: FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standard account can create folders under ProgramData, so a file
    planted there, where the variables a user sets point, pins nothing."""
    program_data = tmp_path / "ProgramData"
    planted = program_data / "bettermemory" / "managed.toml"
    planted.parent.mkdir(parents=True)
    planted.write_text("[telemetry]\nenabled = false\n", encoding="utf-8")
    for name in ("ProgramData", "PROGRAMDATA", "ALLUSERSPROFILE"):
        monkeypatch.setenv(name, str(program_data))
    for name in ("XDG_CONFIG_DIRS", _config.ENV_DIR_OVERRIDE, _config.KEYS_DIR_ENV):
        monkeypatch.setenv(name, str(tmp_path / name))

    loaded = load_config(_user_config(tmp_path))

    assert loaded.managed_path is None
    assert loaded.telemetry.enabled is True
    assert _managed.managed_config_source() == _managed.MANAGED_REGISTRY
    assert [key for _hive, key, _r, _a in windows.opened] == [_KEY]


def test_off_windows_the_real_registry_seam_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing selects the registry off Windows; were it selected, the
    load would stop rather than run without the pins."""
    if sys.platform == "win32":
        pytest.skip("Windows has the registry; no test here reads the real one")
    monkeypatch.setattr(_managed, "_winreg", _REAL_WINREG)
    with pytest.raises(ValueError, match="could not be read") as exc:
        _managed.read_registry_text()
    assert _LABEL in str(exc.value)


def test_on_windows_the_registry_seam_is_the_stdlib_module() -> None:
    if sys.platform != "win32":
        pytest.skip("winreg exists only on Windows")
    import importlib

    # Importing the module reads nothing from the registry.
    assert _REAL_WINREG() is importlib.import_module("winreg")


# ---------------------------------------------------------------------------
# 2. What the text gets
# ---------------------------------------------------------------------------


def test_a_reg_sz_value_pins_its_settings_with_one_notice_per_key(
    tmp_path: Path, windows: FakeRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    windows.set(
        "[telemetry]\nenabled = false\n[policy]\nforbid_force = true\n"
        "forbid_acknowledge = ['credential']\n",
        REG_SZ,
    )
    # `enabled = true` restates the code default, so it draws no notice
    # (config._restates_default); the user's own `forbid_acknowledge`
    # choice is what the pin overrides, and the one notice names it.
    user = _user_config(
        tmp_path,
        "[telemetry]\nenabled = true\n[policy]\nforbid_acknowledge = ['transient']\n",
    )

    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(user)

    assert loaded.telemetry.enabled is False
    assert loaded.policy.forbid_force is True
    assert loaded.policy.forbid_acknowledge == ["credential"]
    assert loaded.managed_path == _managed.MANAGED_REGISTRY
    assert loaded.managed_keys == frozenset(
        {"telemetry.enabled", "policy.forbid_force", "policy.forbid_acknowledge"}
    )
    pinned = [n for n in _notices(caplog) if "pins" in n]
    assert len(pinned) == 1, pinned
    assert _LABEL in pinned[0]
    assert "[policy] `forbid_acknowledge`" in pinned[0]


def test_a_reg_multi_sz_value_is_its_strings_one_per_line(
    tmp_path: Path, windows: FakeRegistry
) -> None:
    windows.set(
        [
            "[storage]",
            "cwd_store = false",
            "[policy]",
            "forbid_acknowledge = ['credential']",
        ],
        REG_MULTI_SZ,
    )
    loaded = load_config(_user_config(tmp_path))
    assert loaded.storage.cwd_store is False
    assert loaded.policy.forbid_acknowledge == ["credential"]
    assert loaded.managed_keys == frozenset(
        {"storage.cwd_store", "policy.forbid_acknowledge"}
    )


#: Texts that load and texts that do not, each checked from a file and
#: from the registry. The byte-order mark is the one Windows PowerShell 5.1
#: writes; the file drops it in its utf-8-sig decoding.
_PARITY_TEXTS = [
    "[telemetry]\nenabled = false\n[policy]\nforbid_force = true\n",
    "\ufeff[storage]\ncwd_store = false\n",
    "",
    "# nothing pinned yet\n",
    "[unknown_section]\nkey = 1\n[telemetry]\nunknown_key = 2\n",
    "telemetry = false\n",
    "[storage\ncwd_store = false\n",
    '[policy]\nforbid_acknowledge = ["credentials"]\n',
    '[daemon]\nport = "high"\n',
    '[storage]\ncwd_store = "maybe"\n',
    "[scopes]\nallowed = 5\n",
]


def _outcome(user: Path, source: object) -> tuple[Any, ...]:
    """What loading `user` under the managed text yields: the settings and
    the pinned keys, or the error with the source's name taken out."""
    try:
        loaded = load_config(user)
    except ValueError as exc:
        return ("error", str(exc).replace(str(source), "<source>"))
    return ("ok", _settings(loaded), loaded.managed_keys, loaded.managed_path)


@pytest.mark.parametrize("text", _PARITY_TEXTS)
def test_the_text_gets_exactly_the_managed_files_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: FakeRegistry,
    text: str,
) -> None:
    user = _user_config(tmp_path, "[telemetry]\nenabled = true\n")
    managed_file = tmp_path / "managed.toml"
    managed_file.write_bytes(text.encode("utf-8"))

    monkeypatch.setattr(_managed, "managed_config_path", lambda: managed_file)
    from_file = _outcome(user, managed_file)
    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    registry.set(text, REG_SZ)
    from_registry = _outcome(user, _managed.MANAGED_REGISTRY)

    assert from_file[0] == from_registry[0]
    if from_file[0] == "error":
        assert from_file == from_registry
        assert "<source>" in from_registry[1]
    else:
        assert from_file[1:3] == from_registry[1:3]
        assert from_file[3] == managed_file
        assert from_registry[3] == _managed.MANAGED_REGISTRY


def test_errors_and_notices_in_the_text_name_the_registry_value(
    tmp_path: Path,
    windows: FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A removed key and a renamed one are notices keyed on the source,
    which for a registry value has no path to resolve."""
    monkeypatch.setattr(_config, "_DEPRECATED_KEY_WARNED_PATHS", set())
    removed = next(iter(_config._REMOVED_BEHAVIOR_KEYS))
    windows.set(
        f"[behavior]\n{removed} = true\nendorsement_debt_ratio_threshold = 0.5\n",
        REG_SZ,
    )
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path))
    assert loaded.behavior.cold_endorsement_ratio_threshold == 0.5
    notices = _notices(caplog)
    assert any(removed in n and _LABEL in n for n in notices), notices
    assert any("endorsement_debt_ratio_threshold" in n and _LABEL in n for n in notices)

    windows.set("[daemon]\nport = []\n", REG_SZ)
    with pytest.raises(ValueError) as exc:
        load_config(_user_config(tmp_path))
    assert str(exc.value).startswith(f"malformed config in {_LABEL}: [daemon] port")


def test_the_registry_parse_records_no_file_as_the_config_path() -> None:
    parsed = _config._config_from_data({}, _managed.MANAGED_REGISTRY)
    assert parsed.config_path is None


# ---------------------------------------------------------------------------
# 3. When it fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["no key", "no value", "another value"])
def test_an_absent_key_or_value_is_no_managed_config(
    tmp_path: Path,
    windows: FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Exactly what a machine without a managed file loads."""
    if where == "no value":
        windows.keys[_KEY] = {}
    elif where == "another value":
        windows.set("[telemetry]\nenabled = false\n", REG_SZ, name="Other")
    user = _user_config(tmp_path, "[telemetry]\nenabled = true\n")

    loaded = load_config(user)

    assert loaded.managed_path is None
    assert loaded.managed_keys == frozenset()
    assert _managed.read_managed_registry() is None
    monkeypatch.setattr(
        _managed, "managed_config_path", lambda: tmp_path / "absent.toml"
    )
    assert asdict(loaded) == asdict(load_config(user))


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("open", PermissionError(13, "Access is denied")),
        ("query", PermissionError(13, "Access is denied")),
        ("query", OSError(234, "More data is available")),
    ],
)
def test_a_value_that_cannot_be_read_fails_closed(
    tmp_path: Path, windows: FakeRegistry, stage: str, error: OSError
) -> None:
    windows.set("[telemetry]\nenabled = false\n", REG_SZ)
    if stage == "open":
        windows.open_error = error
    else:
        windows.query_error = error
    with pytest.raises(ValueError) as exc:
        load_config(_user_config(tmp_path))
    message = str(exc.value)
    assert message.startswith(f"managed config {_LABEL} could not be read")
    assert error.strerror is not None and error.strerror in message
    assert "will not start without the settings it pins" in message


@pytest.mark.parametrize(
    ("value", "kind", "name"),
    [
        ("[telemetry]\nenabled = false\n", REG_EXPAND_SZ, "REG_EXPAND_SZ"),
        (1, REG_DWORD, "REG_DWORD"),
        (b"[telemetry]\nenabled = false\n", REG_BINARY, "REG_BINARY"),
        (None, 9, "type 9"),
    ],
)
def test_a_value_of_another_type_fails_closed(
    tmp_path: Path, windows: FakeRegistry, value: object, kind: int, name: str
) -> None:
    """A REG_EXPAND_SZ is refused, not expanded: expansion substitutes
    environment variables, which are the user's."""
    windows.set(value, kind)
    with pytest.raises(ValueError) as exc:
        load_config(_user_config(tmp_path))
    message = str(exc.value)
    assert message.startswith(
        f"managed config {_LABEL} is a {name} value; the managed TOML is read "
        "only from a REG_SZ or REG_MULTI_SZ value"
    )
    assert "will not start without the settings it pins" in message


@pytest.mark.parametrize(
    ("value", "kind", "name"),
    [(None, REG_SZ, "REG_SZ"), (["[telemetry]", 1], REG_MULTI_SZ, "REG_MULTI_SZ")],
)
def test_a_text_type_whose_data_is_not_text_fails_closed(
    tmp_path: Path, windows: FakeRegistry, value: object, kind: int, name: str
) -> None:
    windows.set(value, kind)
    with pytest.raises(ValueError) as exc:
        load_config(_user_config(tmp_path))
    assert str(exc.value).startswith(
        f"managed config {_LABEL} is a {name} value whose data does not read as text"
    )


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ('[storage]\ndirectory = "C:/store\udc80"\n', REG_SZ),
        (["# \ud800", "[telemetry]", "enabled = false"], REG_MULTI_SZ),
    ],
)
def test_text_no_file_could_hold_fails_closed_as_the_file_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: FakeRegistry,
    value: str | list[str],
    kind: int,
) -> None:
    """An unpaired surrogate is the registry's form of bytes that are not
    UTF-8: its UTF-16 holds one, TOML parses it inside a string or a
    comment, and a pinned directory would carry it to the store's open.
    The file refuses those bytes, so the value refuses the text, with the
    same error naming its source, and the check reports the same."""
    user = _user_config(tmp_path)
    text = value if isinstance(value, str) else "\n".join(value)
    managed_file = tmp_path / "managed.toml"
    managed_file.write_bytes(text.encode("utf-8", "surrogatepass"))
    monkeypatch.setattr(_managed, "managed_config_path", lambda: managed_file)
    with pytest.raises(ValueError) as from_file:
        load_config(user)
    assert str(from_file.value).startswith(
        f"malformed managed config in {managed_file}: "
    )

    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    registry.set(value, kind)
    with pytest.raises(ValueError) as from_registry:
        load_config(user)
    message = str(from_registry.value)
    assert message.startswith(f"malformed managed config in {_LABEL}: ")
    assert "surrogates not allowed" in message
    message.encode("utf-8")  # the error itself prints

    _config.default_config_path().write_text("", encoding="utf-8")
    report = config_cmd.check_report()
    assert report["loads"] is False
    [unreadable] = [f for f in report["findings"] if f["code"] == "unreadable"]
    assert unreadable["file"] == "managed"
    assert unreadable["message"].startswith(f"malformed managed config in {_LABEL}: ")


# ---------------------------------------------------------------------------
# 4. What the reports say
# ---------------------------------------------------------------------------


def test_config_path_names_the_registry_value(windows: FakeRegistry) -> None:
    report = config_cmd.path_report()
    assert report["managed"] == {
        "path": _LABEL,
        "exists": False,
        "read": True,
        "pinned": [],
        "note": None,
    }
    text = config_cmd.render_path(report)
    assert f"managed file  {_LABEL} (none deployed: nothing is pinned)" in text

    windows.set("[telemetry]\nenabled = false\n[policy]\nforbid_force = true\n", REG_SZ)
    report = config_cmd.path_report()
    assert report["managed"]["exists"] is True
    assert report["managed"]["pinned"] == ["policy.forbid_force", "telemetry.enabled"]
    text = config_cmd.render_path(report)
    assert f"managed file  {_LABEL} (present, pins 2 settings)" in text


def test_config_path_counts_an_unreadable_value_as_present(
    windows: FakeRegistry,
) -> None:
    windows.set("[telemetry]\nenabled = false\n", REG_SZ)
    windows.query_error = PermissionError(13, "Access is denied")
    report = config_cmd.path_report()
    assert report["managed"]["exists"] is True
    assert report["error"] is not None and _LABEL in report["error"]


def test_config_show_names_the_registry_value(windows: FakeRegistry) -> None:
    _config.default_config_path().write_text(
        "[telemetry]\nenabled = true\n", encoding="utf-8"
    )
    windows.set("[telemetry]\nenabled = false\n", REG_SZ)

    report = config_cmd.show_report()

    assert report["managed"]["path"] == _LABEL
    assert report["managed"]["exists"] is True
    assert report["managed"]["parses"] is True
    assert report["settings"]["telemetry"]["enabled"] == {
        "value": False,
        "source": "managed",
        "user_value": True,
    }
    text = config_cmd.render_show(report)
    assert f"# managed file: {_LABEL}\n" in text
    assert "enabled = false" in text


def test_config_check_reads_the_registry_value(windows: FakeRegistry) -> None:
    _config.default_config_path().write_text("", encoding="utf-8")
    windows.set(["[telemetry]", "enabeld = false"], REG_MULTI_SZ)

    report = config_cmd.check_report()

    assert report["files"][1]["path"] == _LABEL
    [finding] = report["findings"]
    assert (finding["level"], finding["code"], finding["file"]) == (
        "warning",
        "unknown_key",
        "managed",
    )
    assert f"managed file: {_LABEL}\n" in config_cmd.render_check(report)


@pytest.mark.parametrize(
    ("value", "kind", "fragment"),
    [
        ("[storage\n", REG_SZ, f"malformed managed config in {_LABEL}:"),
        (1, REG_DWORD, f"managed config {_LABEL} is a REG_DWORD value"),
    ],
)
def test_config_check_reports_a_value_that_does_not_load(
    windows: FakeRegistry, value: object, kind: int, fragment: str
) -> None:
    _config.default_config_path().write_text("", encoding="utf-8")
    windows.set(value, kind)

    report = config_cmd.check_report()

    assert report["exit_code"] == config_cmd.EXIT_ERROR
    assert report["loads"] is False
    unreadable = [f for f in report["findings"] if f["code"] == "unreadable"]
    assert [f["file"] for f in unreadable] == ["managed"]
    assert fragment in unreadable[0]["message"]


def test_config_check_reports_a_registry_value_that_is_not_a_table(
    windows: FakeRegistry,
) -> None:
    _config.default_config_path().write_text("", encoding="utf-8")
    windows.set("telemetry = false\n", REG_SZ)
    report = config_cmd.check_report()
    codes = [(f["code"], f["file"]) for f in report["findings"]]
    assert ("not_a_table", "managed") in codes
    assert report["exit_code"] == config_cmd.EXIT_ERROR


def test_config_check_strips_the_registry_label_from_a_value_error(
    windows: FakeRegistry,
) -> None:
    _config.default_config_path().write_text("", encoding="utf-8")
    windows.set('[daemon]\nport = "high"\n', REG_SZ)
    report = config_cmd.check_report()
    [finding] = [f for f in report["findings"] if f["code"] == "invalid_value"]
    assert finding["file"] == "managed"
    assert finding["message"].startswith("[daemon] port = 'high' must be")


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def _example_header() -> str:
    return _EXAMPLE.read_text(encoding="utf-8").split("\n[storage]")[0]


def test_the_example_tells_a_windows_admin_where_the_value_goes() -> None:
    header = _example_header()
    assert "none in this release" not in header
    assert "HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\bettermemory" in header
    assert "ManagedConfig" in header
    assert "Nothing under ProgramData is read." in header
    assert "Group Policy" in header and "Intune" in header


def test_the_examples_reg_add_line_deploys_a_value_that_loads(
    tmp_path: Path, windows: FakeRegistry
) -> None:
    """The command the example gives, as reg.exe stores it: REG_MULTI_SZ,
    one string per ``\\0``, in the 64-bit view of the key bettermemory
    reads."""
    [command] = [
        line.removeprefix("#").strip()
        for line in _example_header().splitlines()
        if "reg add" in line
    ]
    match = re.fullmatch(
        r'reg add "HKLM\\SOFTWARE\\Policies\\bettermemory" /v ManagedConfig '
        r'/t REG_MULTI_SZ /reg:64 /f /d "(?P<data>[^"]*)"',
        command,
    )
    assert match is not None, command
    windows.set(match["data"].split("\\0"), REG_MULTI_SZ)

    loaded = load_config(_user_config(tmp_path))

    assert loaded.managed_path == _managed.MANAGED_REGISTRY
    assert loaded.storage.cwd_store is False
    assert loaded.policy.forbid_acknowledge == ["credential"]


def test_the_whole_example_as_a_reg_sz_pins_what_the_file_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry
) -> None:
    """The PowerShell lines store the file's text as one REG_SZ."""
    user = _user_config(tmp_path)
    monkeypatch.setattr(_managed, "managed_config_path", lambda: _EXAMPLE)
    from_file = load_config(user)
    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    registry.set(_EXAMPLE.read_text(encoding="utf-8"), REG_SZ)
    from_registry = load_config(user)
    assert from_registry.managed_keys == from_file.managed_keys != frozenset()
    assert _settings(from_registry) == _settings(from_file)
