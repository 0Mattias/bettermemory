"""`bettermemory security sandbox`: the Claude Code sandbox block for this
host's real paths (`sandbox_settings`).

docs/installation.md section 5 gave the block with the Linux paths only, so
on macOS, where the config, keys and state directories all live under
``~/Library/Application Support/bettermemory``, a pasted block denied two
directories that do not exist and left the key and the daemon's token
readable to the sandboxed shell. These tests build the block for each
platform's layout through monkeypatched `platformdirs`, with a fake home
directory, and pin: the Linux layout reproduces the documented block entry
for entry; the macOS layout names the Application Support directory once;
every ``BETTERMEMORY_*`` override and ``[storage] directory`` reaches the
block in both path grammars (``/x`` in ``denyRead``, ``//x`` in the rules);
and nothing is written, not even the default config.toml `load_config`
creates on a fresh account.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path, PureWindowsPath

import platformdirs
import pytest

from bettermemory import _daemon_client
from bettermemory import config as _config
from bettermemory import log as _log
from bettermemory import sandbox_settings
from bettermemory.config import Config
from bettermemory.sandbox_settings import (
    Location,
    build_plan,
    denied_locations,
    load_config_read_only,
    rule_path,
    sandbox_path,
)

APP = "bettermemory"


# ---------------------------------------------------------------------------
# Fixtures: a fake home and a platform's directory layout
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home directory, and no Claude Code config override: the
    block spells paths under it with ``~``, and nothing reads the real
    one."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.delenv(sandbox_settings.CLAUDE_CONFIG_DIR_ENV, raising=False)
    return fake


def _layout(
    monkeypatch: pytest.MonkeyPatch, *, config_dir: Path, state_dir: Path
) -> None:
    """Point `platformdirs` at one platform's layout and restore the
    shipped definitions the suite's autouse fixtures replace: the keys
    under the user config directory (`log.default_keys_dir`), the config
    file in it (`config.default_config_path`), and no override variable."""

    def user_config_dir(appname: str | None = None, *args: object, **kw: object) -> str:
        return str(config_dir)

    def user_state_dir(appname: str | None = None, *args: object, **kw: object) -> str:
        return str(state_dir)

    monkeypatch.setattr(platformdirs, "user_config_dir", user_config_dir)
    monkeypatch.setattr(platformdirs, "user_state_dir", user_state_dir)
    monkeypatch.setattr(
        _log,
        "default_keys_dir",
        lambda: Path(platformdirs.user_config_dir(APP)) / "keys",
    )
    monkeypatch.setattr(
        _config,
        "default_config_path",
        lambda: Path(platformdirs.user_config_dir(APP)) / _config.CONFIG_FILENAME,
    )
    for name in (
        _config.ENV_DIR_OVERRIDE,
        _config.KEYS_DIR_ENV,
        _daemon_client.STATE_DIR_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def linux(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _layout(
        monkeypatch,
        config_dir=home / ".config" / APP,
        state_dir=home / ".local" / "state" / APP,
    )
    return home


@pytest.fixture
def macos(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    support = home / "Library" / "Application Support" / APP
    _layout(monkeypatch, config_dir=support, state_dir=support)
    return home


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    """Every path under `roots`: its mode, size and content hash. Equal
    before and after means nothing was created, removed, chmodded or
    written."""
    seen: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted([root, *root.rglob("*")]):
            st = path.lstat()
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            )
            seen[str(path)] = (st.st_mode, st.st_size if path.is_file() else 0, digest)
    return seen


#: The block docs/installation.md section 5 documents, with the two
#: project-relative rules the command adds.
LINUX_BLOCK = {
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            "denyRead": [
                "~/.claude-memory",
                "~/.config/bettermemory",
                "~/.local/state/bettermemory",
            ]
        },
    },
    "permissions": {
        "deny": [
            "Read(~/.claude-memory/**)",
            "Edit(~/.claude-memory/**)",
            "Read(~/.config/bettermemory/**)",
            "Edit(~/.config/bettermemory/**)",
            "Read(~/.local/state/bettermemory/**)",
            "Edit(~/.local/state/bettermemory/**)",
            "Read(.claude-memory/**)",
            "Edit(.claude-memory/**)",
        ]
    },
}


# ---------------------------------------------------------------------------
# The platform layouts
# ---------------------------------------------------------------------------


def test_linux_layout_is_the_documented_block(linux: Path) -> None:
    """With no override, the Linux layout gives the documented three
    directories, the keys directory folded into the config directory that
    holds it."""
    plan = build_plan(Config(), platform="linux")
    assert plan.settings == LINUX_BLOCK
    roles = [loc.role for loc in plan.denied]
    assert roles == ["store", "config", "state"]
    keys = next(loc for loc in plan.locations if loc.role == "keys")
    assert keys.path == (linux / ".config" / APP / "keys").resolve()
    assert keys not in plan.denied


def test_macos_layout_names_application_support_once(macos: Path) -> None:
    """On macOS the config, keys and state directories are one directory
    under Application Support: the block names it once, where the Linux
    block's two paths would deny nothing."""
    plan = build_plan(Config(), platform="darwin")
    support = "~/Library/Application Support/bettermemory"
    assert plan.settings["sandbox"]["filesystem"]["denyRead"] == [
        "~/.claude-memory",
        support,
    ]
    assert plan.settings["permissions"]["deny"] == [
        "Read(~/.claude-memory/**)",
        "Edit(~/.claude-memory/**)",
        f"Read({support}/**)",
        f"Edit({support}/**)",
        "Read(.claude-memory/**)",
        "Edit(.claude-memory/**)",
    ]
    text = json.dumps(plan.settings)
    assert ".config/bettermemory" not in text
    assert ".local/state" not in text


def test_overrides_reach_both_grammars(
    linux: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store, keys or state directory outside the home directory is an
    absolute ``/x`` in ``denyRead`` and ``//x`` in the rules, where a
    single slash would be relative to the settings file. The home default
    store stays denied beside the named one."""
    store = tmp_path / "srv" / "bm"
    keys = tmp_path / "vault" / "keys"
    state = tmp_path / "run" / "bm"
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(store))
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(keys))
    monkeypatch.setenv(_daemon_client.STATE_DIR_ENV, str(state))
    plan = build_plan(Config(), platform="linux")
    sources = {loc.role: loc.source for loc in plan.locations}
    assert sources["store"] == "BETTERMEMORY_DIR"
    assert sources["keys"] == "BETTERMEMORY_KEYS_DIR"
    assert sources["state"] == "BETTERMEMORY_STATE_DIR"
    deny_read = plan.settings["sandbox"]["filesystem"]["denyRead"]
    rules = plan.settings["permissions"]["deny"]
    for directory in (store, keys, state):
        real = directory.resolve().as_posix()
        assert real in deny_read
        assert f"Read(/{real}/**)" in rules
        assert f"Edit(/{real}/**)" in rules
        assert real.startswith("/")
    assert "~/.claude-memory" in deny_read
    assert "~/.config/bettermemory" in deny_read


def test_storage_directory_from_the_config_file(linux: Path, tmp_path: Path) -> None:
    """``[storage] directory`` in config.toml chooses the store, and the
    plan says so."""
    store = linux / "notes" / "memory"
    config_path = _config.default_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f'[storage]\ndirectory = "{store.as_posix()}"\n', encoding="utf-8"
    )
    plan = build_plan(load_config_read_only(), platform="linux")
    assert plan.locations[0].source == "[storage] directory"
    assert "~/notes/memory" in plan.settings["sandbox"]["filesystem"]["denyRead"]


def test_project_store_is_named_and_noted(
    linux: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store chosen by ./.claude-memory in the working directory is
    denied by its own path, and a note says other projects' stores are
    their own directories."""
    project = tmp_path / "repo"
    (project / ".claude-memory").mkdir(parents=True)
    monkeypatch.chdir(project)
    plan = build_plan(Config(), platform="linux")
    assert plan.locations[0].source == "./.claude-memory"
    real = (project / ".claude-memory").resolve().as_posix()
    assert real in plan.settings["sandbox"]["filesystem"]["denyRead"]
    assert any("./.claude-memory in the working directory" in n for n in plan.notes)


def test_store_rule_reads_home_cwd_as_the_default(linux: Path) -> None:
    """A working directory that is the home directory resolves its project
    store to the home default, and reads as the default."""
    directory = (linux / ".claude-memory").resolve()
    assert sandbox_settings.store_rule(Config(), directory, cwd=linux) == "home default"


# ---------------------------------------------------------------------------
# Destination, ports and notes
# ---------------------------------------------------------------------------


def test_format_names_the_file(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = build_plan(Config(), fmt="settings", platform="linux")
    assert user.destination == linux / ".claude" / "settings.json"
    assert "Merge into" in user.notes[0]
    managed = build_plan(Config(), fmt="managed", platform="linux")
    assert managed.destination == Path("/etc/claude-code/managed-settings.json")
    assert "administrator rights" in managed.notes[0]
    assert managed.settings == user.settings
    with pytest.raises(ValueError, match="unknown format"):
        build_plan(Config(), fmt="project", platform="linux")


def test_managed_settings_path_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sandbox_settings.claude_managed_settings_path("darwin") == Path(
        "/Library/Application Support/ClaudeCode/managed-settings.json"
    )
    assert sandbox_settings.claude_managed_settings_path("linux") == Path(
        "/etc/claude-code/managed-settings.json"
    )
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    windows = sandbox_settings.claude_managed_settings_path("win32")
    assert windows.parts[-2:] == ("ClaudeCode", "managed-settings.json")
    assert str(windows).startswith(r"D:\Apps")


def test_user_settings_honour_claude_config_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        sandbox_settings.claude_user_settings_path()
        == home / ".claude" / "settings.json"
    )
    monkeypatch.setenv(sandbox_settings.CLAUDE_CONFIG_DIR_ENV, str(tmp_path / "cc"))
    assert (
        sandbox_settings.claude_user_settings_path()
        == tmp_path / "cc" / "settings.json"
    )


def test_ports_include_the_recorded_one(linux: Path) -> None:
    """A daemon that found the configured port held binds an ephemeral one
    and records it; the notes name both."""
    store_file = (linux / ".claude-memory" / "memory.sqlite").resolve()
    _daemon_client.write_state(
        _daemon_client.resolve_state_dir(),
        {
            "pid": os.getpid(),
            "port": 54321,
            "token": "t" * 64,
            "version": "0",
            "store": str(store_file),
            "started": "2026-01-01T00:00:00Z",
        },
    )
    plan = build_plan(Config(), platform="linux")
    assert plan.ports == (7397, 54321)
    assert any("port 7397, 54321" in note for note in plan.notes)


def test_windows_and_whole_home_notes(
    linux: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native Windows has no sandbox, and a store that resolves to the home
    directory would deny all of it: each gets a note."""
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(linux))
    plan = build_plan(Config(), platform="win32")
    assert any("does not run on native Windows" in note for note in plan.notes)
    assert any("denies everything under it" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_denied_locations_drop_duplicates_and_nested(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    found = [
        Location("store", a / "inner", "x"),
        Location("home store", b, "x"),
        Location("keys", b, "y"),
        Location("config", a, "x"),
    ]
    kept = denied_locations(found)
    assert [loc.role for loc in kept] == ["home store", "config"]


def test_spellings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert sandbox_path(home / ".claude-memory", home) == "~/.claude-memory"
    assert rule_path(home / ".claude-memory", home) == "~/.claude-memory"
    assert sandbox_path(home, home) == "~"
    assert rule_path(home, home) == "~"
    outside = Path("/srv/bm")
    assert sandbox_path(outside, None) == "/srv/bm"
    assert rule_path(outside, None) == "//srv/bm"
    assert rule_path(PureWindowsPath(r"C:\Users\x\bm"), None) == "//c/Users/x/bm"
    assert rule_path(PureWindowsPath("C:\\"), None) == "//c"


def test_to_json_reports_both_spellings(linux: Path) -> None:
    plan = build_plan(Config(), fmt="managed", platform="linux")
    report = plan.to_json(sandbox_settings.home_dir())
    assert report["format"] == "managed"
    assert report["settings"] == plan.settings
    keys = next(row for row in report["locations"] if row["role"] == "keys")
    assert keys["denied"] is False
    assert keys["sandbox_path"] == "~/.config/bettermemory/keys"
    assert report["ports"] == [7397]
    json.dumps(report)


# ---------------------------------------------------------------------------
# Reading the config, and writing nothing
# ---------------------------------------------------------------------------


def test_load_config_read_only_creates_nothing(linux: Path) -> None:
    """`load_config` writes a default config.toml where none exists; the
    read-only form returns the defaults and leaves the directory empty."""
    config_path = _config.default_config_path()
    assert not config_path.exists()
    config = load_config_read_only()
    assert config.storage.directory is None
    assert not config_path.parent.exists()


def test_load_config_read_only_raises_on_a_malformed_file(linux: Path) -> None:
    config_path = _config.default_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[storage\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config_read_only()


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int | str | None:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_prints_the_block_and_writes_nothing(
    linux: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdout is the block alone, so it pipes into a file; the notes and
    the directories go to stderr. The scratch tree is byte-for-byte the
    same afterwards."""
    before = _snapshot(tmp_path)
    assert _run_cli(["security", "sandbox"], monkeypatch) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == LINUX_BLOCK
    assert "# bettermemory security sandbox: for" in captured.err
    assert "user settings" in captured.err
    assert "covered by another entry" in captured.err
    assert _snapshot(tmp_path) == before


def test_cli_json_and_managed(
    linux: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _snapshot(tmp_path)
    code = _run_cli(
        ["security", "sandbox", "--format", "managed", "--json"], monkeypatch
    )
    assert code == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["format"] == "managed"
    assert report["destination"] == str(sandbox_settings.claude_managed_settings_path())
    assert report["settings"] == LINUX_BLOCK
    assert captured.err == ""
    assert _snapshot(tmp_path) == before


def test_cli_refuses_a_malformed_config(
    linux: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config.default_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[storage\n", encoding="utf-8")
    assert _run_cli(["security", "sandbox"], monkeypatch) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the config does not load" in captured.err


def test_cli_group_without_a_subcommand_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["security"], monkeypatch) == 2
    out = capsys.readouterr().out
    assert "audit" in out and "sandbox" in out
