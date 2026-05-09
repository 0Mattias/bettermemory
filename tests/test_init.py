"""Tests for `bettermemory init` — the onboarding subcommand.

The CLI lives in `src/bettermemory/init.py`; this exercises the unit
helpers directly (so we don't have to spawn a subprocess for every
case) plus a couple of `cli_init` invocations that go through the
full path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.init import (
    KNOWN_CLIENTS,
    cli_init,
    find_binary,
    patch_client_config,
    server_snippet,
)


# ---------------------------------------------------------------------------
# server_snippet
# ---------------------------------------------------------------------------


def test_server_snippet_default_shape() -> None:
    out = server_snippet(binary="/usr/local/bin/bettermemory")
    assert out == {
        "mcpServers": {
            "memory": {
                "command": "/usr/local/bin/bettermemory",
                "args": [],
            }
        }
    }


def test_server_snippet_custom_name() -> None:
    out = server_snippet(name="bettermemory", binary="/x/bm")
    assert "bettermemory" in out["mcpServers"]
    assert "memory" not in out["mcpServers"]


def test_server_snippet_uses_find_binary_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    out = server_snippet()
    assert out["mcpServers"]["memory"]["command"] == "/fake/bm"


# ---------------------------------------------------------------------------
# find_binary
# ---------------------------------------------------------------------------


def test_find_binary_uses_path_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Use a real on-disk path so `Path(...).resolve()` inside find_binary
    # behaves consistently across platforms — a hardcoded POSIX string like
    # "/usr/local/bin/bettermemory" gets rewritten to "D:\usr\local\bin\..."
    # on Windows because resolve() anchors to the current drive.
    fake = tmp_path / "bettermemory"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("bettermemory.init.shutil.which", lambda _name: str(fake))
    assert find_binary() == str(fake.resolve())


def test_find_binary_falls_back_to_argv_when_path_misses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("bettermemory.init.shutil.which", lambda _name: None)
    fake = tmp_path / "bettermemory"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("bettermemory.init.sys.argv", [str(fake)])
    assert find_binary() == str(fake.resolve())


def test_find_binary_last_resort_returns_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.shutil.which", lambda _name: None)
    monkeypatch.setattr("bettermemory.init.sys.argv", ["bettermemory"])  # not absolute
    assert find_binary() == "bettermemory"


# ---------------------------------------------------------------------------
# patch_client_config
# ---------------------------------------------------------------------------


def test_patch_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "subdir" / "claude_desktop_config.json"
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert target.exists()
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body == {"mcpServers": {"memory": {"command": "/x/bm", "args": []}}}


def test_patch_merges_into_existing_mcp_servers(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "filesystem": {"command": "fs-mcp", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    body = json.loads(target.read_text(encoding="utf-8"))
    # Existing entry untouched.
    assert body["mcpServers"]["filesystem"] == {"command": "fs-mcp", "args": []}
    # New entry added.
    assert body["mcpServers"]["memory"] == {"command": "/x/bm", "args": []}


def test_patch_preserves_non_mcp_keys(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"theme": "dark", "mcpServers": {}}),
        encoding="utf-8",
    )
    patch_client_config(target, binary="/x/bm")
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["theme"] == "dark"


def test_patch_noop_when_entry_matches(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    initial = {"mcpServers": {"memory": {"command": "/x/bm", "args": []}}}
    target.write_text(json.dumps(initial), encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "noop"
    # File should not have been rewritten — mtime stable.
    assert target.stat().st_mtime_ns == mtime_before


def test_patch_updates_when_binary_path_changed(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"mcpServers": {"memory": {"command": "/old/bm", "args": []}}}),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/new/bm")
    assert result["action"] == "updated"
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"]["memory"]["command"] == "/new/bm"


def test_patch_rejects_malformed_json(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{not valid json,,,", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        patch_client_config(target, binary="/x/bm")


def test_patch_rejects_non_object_root(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-object root"):
        patch_client_config(target, binary="/x/bm")


def test_patch_rejects_non_object_mcpservers(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"mcpServers": ["nope"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not an object"):
        patch_client_config(target, binary="/x/bm")


def test_patch_handles_empty_file(tmp_path: Path) -> None:
    """An existing-but-empty file should be treated like an absent one
    rather than crashing on json.loads(''). Some clients touch the file
    before populating it."""
    target = tmp_path / "config.json"
    target.write_text("", encoding="utf-8")
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"


def test_patch_uses_find_binary_when_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/auto/detected/bm")
    target = tmp_path / "config.json"
    patch_client_config(target)
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"]["memory"]["command"] == "/auto/detected/bm"


# ---------------------------------------------------------------------------
# KNOWN_CLIENTS registry
# ---------------------------------------------------------------------------


def test_known_clients_have_at_least_one_path() -> None:
    for key, getter in KNOWN_CLIENTS.items():
        cp = getter()
        assert cp.name == key
        assert len(cp.paths) >= 1, f"{key} has no candidate paths"
        assert cp.description, f"{key} has empty description"


def test_known_clients_paths_are_absolute() -> None:
    """Auto-patch writes wherever paths[0] points; if it's relative, the
    file ends up in $CWD, which is rarely what the user wants."""
    for key, getter in KNOWN_CLIENTS.items():
        cp = getter()
        # Project-scoped paths (like ./.mcp.json) are intentionally
        # relative to cwd — Path.cwd() is absolute, so cp.paths[1+] are
        # also absolute when constructed from Path.cwd(). We assert all
        # paths are absolute as the runtime invariant.
        for p in cp.paths:
            assert p.is_absolute(), f"{key}: {p} is not absolute"


# ---------------------------------------------------------------------------
# cli_init
# ---------------------------------------------------------------------------


def _shared_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client": None,
        "print_only": False,
        "json_out": False,
        "name": "memory",
        "with_addendum": False,
        "config_path": None,
    }
    base.update(overrides)
    return base


def test_cli_init_show_and_tell_prints_snippet_and_locations(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    cli_init(**_shared_kwargs())
    out = capsys.readouterr().out
    assert "/fake/bm" in out
    assert "mcpServers" in out
    assert "claude-code" in out
    assert "claude-desktop" in out
    assert "cursor" in out
    assert "continue" in out
    assert "cline" in out
    assert "--client" in out


def test_cli_init_show_and_tell_addendum_gated(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anchor on the addendum's opening line rather than a hard-coded
    # phrase — survives re-wordings of the addendum body.
    from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM

    sentinel = SYSTEM_PROMPT_ADDENDUM.splitlines()[0]

    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    cli_init(**_shared_kwargs())
    out = capsys.readouterr().out
    # The addendum body shouldn't appear by default — it's gated
    # behind --with-addendum.
    assert sentinel not in out

    cli_init(**_shared_kwargs(with_addendum=True))
    out = capsys.readouterr().out
    assert sentinel in out


def test_cli_init_json_output_is_machine_readable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    cli_init(**_shared_kwargs(json_out=True))
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["binary"] == "/fake/bm"
    assert parsed["snippet"]["mcpServers"]["memory"]["command"] == "/fake/bm"
    assert set(parsed["clients"].keys()) == {
        "claude-code",
        "claude-desktop",
        "cursor",
        "continue",
        "cline",
    }


def test_cli_init_patch_mode_writes_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "claude_desktop_config.json"
    cli_init(**_shared_kwargs(client="claude-desktop", config_path=target))
    out = capsys.readouterr().out
    assert str(target) in out
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"]["memory"]["command"] == "/fake/bm"


def test_cli_init_print_only_does_not_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.json"
    cli_init(
        **_shared_kwargs(
            client="claude-desktop",
            config_path=target,
            print_only=True,
        )
    )
    assert not target.exists()
    out = capsys.readouterr().out
    parsed = json.loads(out.split("\n#")[0])
    assert parsed["mcpServers"]["memory"]["command"] == "/fake/bm"


def test_cli_init_json_with_patch_includes_patch_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.json"
    cli_init(
        **_shared_kwargs(
            client="claude-desktop",
            config_path=target,
            json_out=True,
        )
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["patch"]["action"] == "added"
    assert parsed["patch"]["path"] == str(target)


def test_cli_init_patch_idempotent_says_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.json"
    cli_init(**_shared_kwargs(client="claude-desktop", config_path=target))
    capsys.readouterr()  # drain
    cli_init(**_shared_kwargs(client="claude-desktop", config_path=target))
    out = capsys.readouterr().out
    assert "no change" in out or "already configured" in out
