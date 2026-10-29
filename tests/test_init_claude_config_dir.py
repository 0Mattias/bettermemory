"""`bettermemory init` and the files Claude Code reads under CLAUDE_CONFIG_DIR.

Claude Code keeps its global config (``.claude.json``: the user-scope MCP
servers and the project registry) and its user settings file in the
directory ``CLAUDE_CONFIG_DIR`` names, when that is set: the global config
inside the directory, not beside it, and the settings file as the
directory's own ``settings.json``. `claude_paths` is the one place that
says so, for init, doctor, the audit and the sandbox block (ENG-176).

Every test here runs on a scratch home and a scratch configuration
directory; none reads or writes the developer's Claude Code files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from bettermemory import claude_paths, sandbox_settings
from bettermemory.init import (
    DECLARATION_DENY_RULES,
    DEFAULT_SERVER_NAME,
    KNOWN_CLIENTS,
    cli_init,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    return fake


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client": "claude-code",
        "print_only": False,
        "json_out": False,
        "name": None,
        "with_addendum": False,
        "config_path": None,
    }
    base.update(overrides)
    return base


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _deny(path: Path) -> list[str]:
    return list(json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"])


def test_the_paths_follow_claude_code_with_and_without_the_variable(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the variable: ``~/.claude``, ``~/.claude.json`` and
    ``~/.claude/settings.json``. With it: the directory, the global config
    inside it, and its own ``settings.json``, as Claude Code resolves them.
    A ``~`` in the value is kept as written, as Claude Code keeps it: a
    directory named ``~`` under the working directory."""
    assert claude_paths.config_dir() == home / ".claude"
    assert claude_paths.global_config_path() == home / ".claude.json"
    assert claude_paths.user_settings_path() == home / ".claude" / "settings.json"
    work = tmp_path / "claude-work"
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(work))
    assert claude_paths.config_dir() == work
    assert claude_paths.global_config_path() == work / ".claude.json"
    assert claude_paths.user_settings_path() == work / "settings.json"
    assert sandbox_settings.claude_user_settings_path() == work / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, "~/.claude-work")
    literal = Path.cwd() / "~" / ".claude-work"
    assert claude_paths.config_dir() == literal
    assert claude_paths.global_config_path() == literal / ".claude.json"
    project = tmp_path / "proj"
    assert claude_paths.project_settings_paths(project) == (
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
    )
    assert claude_paths.project_mcp_path(project) == project / ".mcp.json"


def test_the_settings_file_pairs_with_the_config_it_was_derived_from(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home layout's global config pairs with ``~/.claude/settings.json``;
    a global config in a configuration directory, named by this shell's
    variable or found elsewhere, with that directory's ``settings.json``;
    any other MCP file with the ``.claude/settings.json`` beside it."""
    pair = claude_paths.settings_for_mcp_config
    assert pair(home / ".claude.json") == home / ".claude" / "settings.json"
    other = tmp_path / "other"
    assert pair(other / ".claude.json") == other / "settings.json"
    project = tmp_path / "proj"
    assert pair(project / ".mcp.json") == project / ".claude" / "settings.json"
    assert pair(project / "custom.json") == project / ".claude" / "settings.json"
    work = tmp_path / "claude-work"
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(work))
    assert pair(work / ".claude.json") == work / "settings.json"
    assert pair(home / ".claude.json") == home / ".claude" / "settings.json"
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(home))
    assert pair(home / ".claude.json") == home / "settings.json"


@pytest.mark.parametrize("variable", [False, True])
def test_a_legacy_config_json_is_the_global_config_when_claude_code_reads_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: bool
) -> None:
    """Claude Code (2.1.296 to 2.1.309, ``getGlobalClaudeFile``) reads a
    legacy ``.config.json`` in its configuration directory before
    ``.claude.json``: when it is a regular file and non-empty, or empty,
    writable and with no ``.claude.json`` beside it. The resolver took
    ``.claude.json`` whatever was there, so init patched, and doctor read, a
    file Claude Code did not read on such a host."""
    if variable:
        config = tmp_path / "claude-work"
        monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(config))
        current = config / ".claude.json"
    else:
        config = home / ".claude"
        current = home / ".claude.json"
    config.mkdir(parents=True)
    legacy = config / ".config.json"
    assert claude_paths.global_config_path() == current
    legacy.write_text('{"mcpServers": {}}', encoding="utf-8")
    assert claude_paths.global_config_path() == legacy
    assert claude_paths.is_global_config(legacy)
    assert claude_paths.settings_for_mcp_config(legacy) == config / "settings.json"
    legacy.write_bytes(b"")
    assert claude_paths.global_config_path() == legacy
    current.write_text("{}", encoding="utf-8")
    assert claude_paths.global_config_path() == current
    current.unlink()
    if os.name == "posix":
        legacy.chmod(0o444)
        assert claude_paths.global_config_path() == current
        legacy.chmod(0o644)
    legacy.unlink()
    legacy.mkdir()
    assert claude_paths.global_config_path() == current


def test_init_and_doctor_use_the_legacy_config_json(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a non-empty legacy ``.config.json`` in ``~/.claude``, init's
    default target is that file, the deny rules leave ``~/.claude/settings.json``,
    and nothing lands in ``~/.claude.json``; doctor finds the registration
    there."""
    from bettermemory import doctor

    legacy = home / ".claude" / ".config.json"
    _write_json(legacy, {"projects": {}})
    settings = home / ".claude" / "settings.json"
    _write_json(settings, {"permissions": {"deny": list(DECLARATION_DENY_RULES)}})
    cli_init(**_kwargs())
    assert not (home / ".claude.json").exists()
    body = json.loads(legacy.read_text(encoding="utf-8"))
    assert body["projects"] == {}
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    assert _deny(settings) == []
    monkeypatch.setattr(
        doctor, "launches_bettermemory", lambda command, args, binary: True
    )
    assert doctor._claude_registrations(None, "/fake/bm") == [str(legacy)]


@pytest.mark.parametrize("variable", [False, True])
def test_a_custom_oauth_url_renames_the_global_config(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: bool
) -> None:
    """Under ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` Claude Code names its global
    config ``.claude-custom-oauth.json`` (the suffix ``GG()`` gives); init
    writes that file and pairs it with the user settings."""
    base = home
    if variable:
        base = tmp_path / "claude-work"
        monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(base))
    monkeypatch.setenv(claude_paths.CUSTOM_OAUTH_URL_ENV, "https://auth.example.test")
    target = base / ".claude-custom-oauth.json"
    assert claude_paths.global_config_path() == target
    assert claude_paths.is_global_config(target)
    assert claude_paths.settings_for_mcp_config(target) == (
        claude_paths.user_settings_path()
    )
    cli_init(**_kwargs())
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    assert not (base / ".claude.json").exists()
    monkeypatch.setenv(claude_paths.CUSTOM_OAUTH_URL_ENV, "")
    assert claude_paths.global_config_path() == base / ".claude.json"


@pytest.mark.parametrize("explicit", [False, True])
def test_init_writes_where_claude_config_dir_points(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    """ENG-176. With CLAUDE_CONFIG_DIR set, `init --client claude-code` wrote
    ``~/.claude.json``, which that Claude Code never reads, and said
    "added"; the deny rules rc1 to rc5 added stayed in the settings file it
    does read, and even ``--config-path $CLAUDE_CONFIG_DIR/.claude.json``
    looked for them in ``$CLAUDE_CONFIG_DIR/.claude/settings.json``. Now the
    default target is the directory's ``.claude.json``, the rules leave the
    directory's ``settings.json``, and nothing lands in the home layout."""
    work = tmp_path / "claude-work"
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, str(work))
    settings = work / "settings.json"
    _write_json(
        settings,
        {"permissions": {"deny": [*DECLARATION_DENY_RULES, "Read(./.env)"]}},
    )
    target = work / ".claude.json"
    cli_init(**_kwargs(config_path=target if explicit else None))
    assert not (home / ".claude.json").exists()
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    assert _deny(settings) == ["Read(./.env)"]
    assert not (home / ".claude").exists()
    assert not (work / ".claude").exists()
    assert KNOWN_CLIENTS["claude-code"]().paths[0] == target


def test_init_without_the_variable_keeps_the_home_layout(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default layout is unchanged: ``~/.claude.json`` and the rules in
    ``~/.claude/settings.json``."""
    settings = home / ".claude" / "settings.json"
    _write_json(settings, {"permissions": {"deny": list(DECLARATION_DENY_RULES)}})
    cli_init(**_kwargs())
    body = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    assert _deny(settings) == []
    assert str(settings) in capsys.readouterr().out


def test_a_relative_variable_names_a_directory_under_the_working_directory(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code joins a relative CLAUDE_CONFIG_DIR onto its working
    directory; the resolver names the same place, absolute."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, "rel-claude")
    found = claude_paths.global_config_path()
    assert found.is_absolute()
    assert os.path.samefile(found.parent.parent, tmp_path)
    assert found.parent.name == "rel-claude"
