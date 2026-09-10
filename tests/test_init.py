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
import yaml

from bettermemory.identity import ENV_CLIENT
from bettermemory.init import (
    DEFAULT_SERVER_NAME,
    FORMAT_HERMES_YAML,
    FORMAT_MCP_SERVERS_JSON,
    HERMES_SERVERS_KEY,
    KNOWN_CLIENTS,
    LEGACY_SERVER_NAME,
    cli_init,
    find_binary,
    hermes_snippet,
    hermes_snippet_text,
    patch_client_config,
    patch_hermes_config,
    read_server_entries,
    server_snippet,
)

# The canonical entry shape patch_client_config writes. Stays in one
# place so tests don't all have to be edited when the shape evolves
# (e.g. when a future Claude Code version expects a new optional field).
CANONICAL_ENTRY_KEYS = {"type", "command", "args", "env"}


def _canonical_entry(binary: str) -> dict[str, Any]:
    return {"type": "stdio", "command": binary, "args": [], "env": {}}


# ---------------------------------------------------------------------------
# server_snippet
# ---------------------------------------------------------------------------


def test_server_snippet_default_shape() -> None:
    """Default shape includes `type: stdio` and `env: {}` even though
    both are optional in the MCP spec — they match what `claude mcp add`
    produces and what Claude Code 2.x writes by default, so the snippet
    looks the same as the user's hand-added entries."""
    out = server_snippet(binary="/usr/local/bin/bettermemory")
    assert out == {
        "mcpServers": {
            DEFAULT_SERVER_NAME: _canonical_entry("/usr/local/bin/bettermemory"),
        }
    }


def test_server_snippet_default_name_is_specific_not_generic() -> None:
    """1.0 used `memory` as the default key, which collided with other
    MCP servers and Claude Code's evolving built-in memory features.
    1.1 default is `bettermemory`. This guard catches an accidental
    revert."""
    out = server_snippet(binary="/x/bm")
    assert DEFAULT_SERVER_NAME in out["mcpServers"]
    assert DEFAULT_SERVER_NAME == "bettermemory"
    assert "memory" not in out["mcpServers"]


def test_server_snippet_custom_name() -> None:
    out = server_snippet(name="something-else", binary="/x/bm")
    assert "something-else" in out["mcpServers"]
    assert DEFAULT_SERVER_NAME not in out["mcpServers"]


def test_server_snippet_uses_find_binary_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    out = server_snippet()
    assert out["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"


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
    assert body == {"mcpServers": {DEFAULT_SERVER_NAME: _canonical_entry("/x/bm")}}


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
    # Existing entry untouched (we don't pad foreign entries with the
    # canonical shape).
    assert body["mcpServers"]["filesystem"] == {"command": "fs-mcp", "args": []}
    # New entry added with the canonical shape.
    assert body["mcpServers"][DEFAULT_SERVER_NAME] == _canonical_entry("/x/bm")


def test_patch_preserves_user_keys_on_bettermemory_entry(tmp_path: Path) -> None:
    """Regression: re-running init MERGES the canonical keys into an existing
    bettermemory entry instead of replacing it wholesale. A user-set `env`
    (notably BETTERMEMORY_DIR, which relocates the whole store), `disabled`,
    or `timeout` must survive the upgrade — clobbering them silently detaches
    the user's store ('my memory is suddenly empty/gone from this client').
    """
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    DEFAULT_SERVER_NAME: {
                        "type": "stdio",
                        "command": "/old/bm",
                        "args": [],
                        "env": {"BETTERMEMORY_DIR": "/custom/store", "BM_LOG": "debug"},
                        "disabled": True,
                        "timeout": 60,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/new/bm")
    assert result["action"] == "updated"
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"][
        DEFAULT_SERVER_NAME
    ]
    # Canonical key updated...
    assert entry["command"] == "/new/bm"
    # ...but the user's customizations are preserved, not clobbered.
    assert entry["env"] == {"BETTERMEMORY_DIR": "/custom/store", "BM_LOG": "debug"}
    assert entry["disabled"] is True
    assert entry["timeout"] == 60


def test_patch_noop_when_custom_env_and_binary_unchanged(tmp_path: Path) -> None:
    """A custom env with the SAME binary must NOT trigger a spurious rewrite.
    The old code rewrote on every re-run because the existing dict never
    equalled the bare 4-key canonical entry it compared against."""
    target = tmp_path / "config.json"
    initial = {
        "mcpServers": {
            DEFAULT_SERVER_NAME: {
                "type": "stdio",
                "command": "/x/bm",
                "args": [],
                "env": {"BETTERMEMORY_DIR": "/custom"},
            }
        }
    }
    target.write_text(json.dumps(initial), encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "noop"
    assert target.stat().st_mtime_ns == mtime_before


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
    initial = {"mcpServers": {DEFAULT_SERVER_NAME: _canonical_entry("/x/bm")}}
    target.write_text(json.dumps(initial), encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "noop"
    # File should not have been rewritten — mtime stable.
    assert target.stat().st_mtime_ns == mtime_before


def test_patch_updates_when_binary_path_changed(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"mcpServers": {DEFAULT_SERVER_NAME: _canonical_entry("/old/bm")}}),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/new/bm")
    assert result["action"] == "updated"
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/new/bm"


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
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/auto/detected/bm"


# ---------------------------------------------------------------------------
# Legacy `memory` → `bettermemory` migration (1.0 → 1.1 default rename)
# ---------------------------------------------------------------------------


def test_patch_migrates_legacy_memory_entry_with_matching_binary(
    tmp_path: Path,
) -> None:
    """A user upgrading from 1.0 has a `memory` entry pointing at our
    binary. Adding a `bettermemory` entry under the new default would
    leave both registered, doubling every tool in the model's tool
    list. Migrate by removing the legacy entry."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "/x/bm", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert result["migrated_from_legacy"] is True
    body = json.loads(target.read_text(encoding="utf-8"))
    # New entry is present under the new key…
    assert body["mcpServers"][DEFAULT_SERVER_NAME] == _canonical_entry("/x/bm")
    # …and the legacy entry is gone.
    assert LEGACY_SERVER_NAME not in body["mcpServers"]


def test_patch_migration_drops_remote_transport_keys_but_keeps_stdio_keys(
    tmp_path: Path,
) -> None:
    """A legacy entry carrying remote-transport keys (`url`/`headers`) must
    NOT carry them into the forced stdio entry: a hybrid stdio+url+headers
    entry can be rejected wholesale by a strict client schema, taking down
    every OTHER MCP server in the file. Legitimate stdio keys (`env` — which
    relocates the store — and `timeout`) MUST survive. Denylist, not
    allowlist: we shed only the transport-only keys."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {
                        "command": "/x/bm",
                        "args": [],
                        "url": "https://old.example/mcp",
                        "headers": {"Authorization": "Bearer xyz"},
                        "env": {"BETTERMEMORY_DIR": "/custom/store"},
                        "timeout": 45,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["migrated_from_legacy"] is True
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"][
        DEFAULT_SERVER_NAME
    ]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/x/bm"
    # Remote-transport-only keys are shed…
    assert "url" not in entry
    assert "headers" not in entry
    # …but legitimate stdio keys survive.
    assert entry["env"] == {"BETTERMEMORY_DIR": "/custom/store"}
    assert entry["timeout"] == 45


def test_patch_does_not_migrate_legacy_memory_with_different_binary(
    tmp_path: Path,
) -> None:
    """If the user is intentionally hosting a `memory` server pointing
    at something else (a different memory MCP), the migration does NOT
    fire — both entries coexist. Migration is binary-equality gated."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {
                        "command": "/some/other/memory-server",
                        "args": [],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert "migrated_from_legacy" not in result
    body = json.loads(target.read_text(encoding="utf-8"))
    # New entry added.
    assert body["mcpServers"][DEFAULT_SERVER_NAME] == _canonical_entry("/x/bm")
    # Legacy untouched — we don't second-guess the user's other server.
    assert body["mcpServers"][LEGACY_SERVER_NAME]["command"] == (
        "/some/other/memory-server"
    )


def test_patch_does_not_migrate_when_explicit_legacy_name_passed(
    tmp_path: Path,
) -> None:
    """Migration only triggers when writing under the new default name.
    A user who passes `--name memory` explicitly is opinionated; honor
    that and skip the migration."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "/old/bm", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/new/bm", name=LEGACY_SERVER_NAME)
    # Updates the legacy entry in place; doesn't introduce the new one
    # nor flag a migration.
    assert result["action"] == "updated"
    assert "migrated_from_legacy" not in result
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"][LEGACY_SERVER_NAME]["command"] == "/new/bm"
    assert DEFAULT_SERVER_NAME not in body["mcpServers"]


def test_patch_uses_atomic_write_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP-config writer must route through `_fsutil.atomic_write_bytes`
    so a power loss / process kill mid-write can't truncate the user's
    entire `~/.claude.json` — blast radius is every MCP server they had
    registered, not just bettermemory. Pre-3.1.0 this was a plain
    `target_path.write_text(...)`. This is a regression pin that bypassing
    the atomic helper would surface here."""
    target = tmp_path / "config.json"
    calls: list[tuple[Path, bytes]] = []
    real = patch_client_config.__globals__["_fsutil"].atomic_write_bytes

    def spy(path: Path, data: bytes, *, mode: int | None = None) -> None:
        calls.append((path, data))
        real(path, data, mode=mode)

    monkeypatch.setattr(
        patch_client_config.__globals__["_fsutil"],
        "atomic_write_bytes",
        spy,
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert len(calls) == 1, (
        f"expected exactly one atomic_write_bytes call; got {len(calls)}. "
        f"A regression to `target_path.write_text(...)` would surface as "
        f"zero calls here."
    )
    path, data = calls[0]
    assert path == target
    # The bytes written must round-trip to the same JSON shape the test
    # otherwise verifies via `target.read_text(...)`.
    body = json.loads(data.decode("utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME] == _canonical_entry("/x/bm")


def test_patch_migration_idempotent_after_first_run(tmp_path: Path) -> None:
    """Running init twice in a row should be a noop on the second call,
    even though the first call performed a legacy migration."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "/x/bm", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    first = patch_client_config(target, binary="/x/bm")
    assert first["action"] == "added"
    assert first["migrated_from_legacy"] is True

    second = patch_client_config(target, binary="/x/bm")
    assert second["action"] == "noop"
    assert "migrated_from_legacy" not in second


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
        # `None` means "use module default" — same shape as argparse
        # passing the flag's `default=None`.
        "name": None,
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
    assert "hermes" in out
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
    assert parsed["snippet"]["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    assert set(parsed["clients"].keys()) == {
        "claude-code",
        "claude-desktop",
        "cursor",
        "continue",
        "cline",
        "hermes",
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
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"


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
    assert parsed["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"


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


def test_cli_init_legacy_migration_surfaces_in_human_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the patch removes a legacy `memory` entry, the human-readable
    summary tells the user — otherwise a quiet rename of the tool prefix
    looks like a bug ("why are my tools named differently now?")."""
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/x/bm")
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "/x/bm", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    cli_init(**_shared_kwargs(client="claude-desktop", config_path=target))
    out = capsys.readouterr().out
    assert "legacy" in out.lower()
    assert LEGACY_SERVER_NAME in out


def test_init_via_cli_exits_clean_on_unwritable_config_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory init --client <c> --config-path <p>` must exit 2 with
    a clean `bettermemory init: error: …` message — NOT a raw
    PermissionError/NotADirectoryError traceback / exit 1 — when the
    --config-path parent is unwritable or a non-directory. Same
    missing-OSError-arm class the 3.6.0 self-audit fixed for
    proposals/tombstones-restore/rename-scope; init was the missed sibling
    (caught by the post-3.6.0 whole-tree sweep). A plain nonexistent path
    is auto-mkdir'd and does NOT trigger this."""
    import argparse

    from bettermemory.cli.init import add_subparser as init_add_subparser
    from bettermemory.cli.init import run as init_run

    # A regular FILE as an ancestor makes mkdir(parents=True) raise
    # NotADirectoryError — deterministic, no chmod (root-flaky in CI).
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    bad_config = blocker / "sub" / "cfg.json"

    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    init_add_subparser(sub)
    args = parser.parse_args(
        ["init", "--client", "claude-code", "--config-path", str(bad_config)]
    )

    with pytest.raises(SystemExit) as excinfo:
        init_run(args)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback (most recent call last)" not in err


def test_init_via_cli_exits_clean_on_malformed_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory init --client <c> --config-path <p>` must exit 2 with a
    clean `bettermemory init: error: …` message — NOT a raw ValueError
    traceback / exit 1 — when the existing config is malformed JSON.
    patch_client_config raises ValueError there, and the CLI arm previously
    caught only OSError (item 12a), so it escaped uncaught. A concurrent-
    write race raises the same ValueError family and gets the same clean
    exit."""
    import argparse

    from bettermemory.cli.init import add_subparser as init_add_subparser
    from bettermemory.cli.init import run as init_run

    bad_config = tmp_path / "cfg.json"
    bad_config.write_text("{not valid json,,,", encoding="utf-8")

    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    init_add_subparser(sub)
    args = parser.parse_args(
        ["init", "--client", "claude-code", "--config-path", str(bad_config)]
    )

    with pytest.raises(SystemExit) as excinfo:
        init_run(args)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback (most recent call last)" not in err


def test_patch_migration_carries_forward_user_keys_on_legacy_entry(
    tmp_path: Path,
) -> None:
    """Regression: the legacy `memory` → `bettermemory` rename must carry
    forward the user's keys that live on the LEGACY entry — most critically
    `env.BETTERMEMORY_DIR` (which relocates the whole store), but also
    `disabled`, `timeout`, and transport overrides. The pre-fix code seeded
    the new entry from `mcp_servers.get(name)`, which is None on the rename
    path (the config only has the entry under `memory`), so those keys were
    silently dropped when the legacy entry was deleted — a user who relocated
    their store then booted against the default dir and their store looked
    gone."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {
                        "type": "stdio",
                        "command": "/x/bm",
                        "args": [],
                        "env": {"BETTERMEMORY_DIR": "/custom/store"},
                        "disabled": True,
                        "timeout": 90,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert result["migrated_from_legacy"] is True
    body = json.loads(target.read_text(encoding="utf-8"))
    # Legacy entry gone…
    assert LEGACY_SERVER_NAME not in body["mcpServers"]
    entry = body["mcpServers"][DEFAULT_SERVER_NAME]
    # …canonical keys owned by us reflect the current binary…
    assert entry["command"] == "/x/bm"
    assert entry["type"] == "stdio"
    assert entry["args"] == []
    # …and the user's customizations survived the rename.
    assert entry["env"] == {"BETTERMEMORY_DIR": "/custom/store"}
    assert entry["timeout"] == 90
    # …but a `disabled: true` that lived ONLY on the legacy entry is a
    # STALE flag, not an opt-in on the surviving entry — the migrated
    # server must be born ENABLED (item 10c), else it comes up disabled
    # while the patch summary reports unqualified success.
    assert "disabled" not in entry


def test_patch_migration_both_exist_unions_legacy_only_keys(
    tmp_path: Path,
) -> None:
    """BOTH-EXIST branch: a config that has BOTH a legacy `memory` entry and
    a `bettermemory` entry must UNION their keys — the legacy entry's
    legacy-only keys (env.BETTERMEMORY_DIR, cwd, timeout, headers) survive
    the migration, while the new-name entry wins on conflicts. The pre-fix
    code only seeded from the legacy entry when NO new-name entry existed,
    yet deleted the legacy entry unconditionally, so in this branch every
    legacy-only key was silently discarded (a relocated store looked gone).

    Mutation-soundness: the env assertion pins the seed ORDER (new-name wins
    the BETTERMEMORY_DIR conflict) — a reversed union would yield
    `/legacy/store`; and the cwd/timeout/headers assertions catch the
    key-loss regression."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {
                        "type": "stdio",
                        "command": "/x/bm",
                        "args": [],
                        "env": {
                            "BETTERMEMORY_DIR": "/legacy/store",
                            "BM_LEGACY_ONLY": "1",
                        },
                        "cwd": "/work",
                        "timeout": 120,
                        "headers": {"X-Trace": "on"},
                        "disabled": True,
                    },
                    DEFAULT_SERVER_NAME: {
                        "type": "stdio",
                        "command": "/old/bm",
                        "args": [],
                        "env": {"BETTERMEMORY_DIR": "/canonical/store"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "updated"
    assert result["migrated_from_legacy"] is True
    body = json.loads(target.read_text(encoding="utf-8"))
    # Legacy entry gone; only the new-name entry remains.
    assert LEGACY_SERVER_NAME not in body["mcpServers"]
    entry = body["mcpServers"][DEFAULT_SERVER_NAME]
    # Canonical keys owned by us reflect the current binary.
    assert entry["command"] == "/x/bm"
    # Legacy-only stdio keys survived the union (not silently dropped).
    assert entry["cwd"] == "/work"
    assert entry["timeout"] == 120
    # …but a remote-transport-only key is shed, so the forced stdio entry
    # can't become a schema-rejected hybrid stdio+headers entry.
    assert "headers" not in entry
    # env deep-merged: new-name entry wins the BETTERMEMORY_DIR conflict,
    # the legacy-only var is preserved (pins seed order + no key loss).
    assert entry["env"] == {
        "BETTERMEMORY_DIR": "/canonical/store",
        "BM_LEGACY_ONLY": "1",
    }
    # `disabled: true` lived only on the legacy entry → born enabled.
    assert "disabled" not in entry


def test_patch_migrates_legacy_with_bare_command_name(tmp_path: Path) -> None:
    """Gate recognizer (item 10b): a legacy `memory` entry written as the
    bare `command: bettermemory` — the form docs/clients.md and
    docs/installation.md bless — must migrate. The pre-fix byte-exact
    `command == binary` gate no-op'd it because `"bettermemory"` never
    equals the resolved absolute binary path."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "bettermemory", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert result["migrated_from_legacy"] is True
    body = json.loads(target.read_text(encoding="utf-8"))
    assert LEGACY_SERVER_NAME not in body["mcpServers"]
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/x/bm"


def test_patch_migrates_legacy_with_uvx_runner_shape(tmp_path: Path) -> None:
    """Gate recognizer (item 10b): the `uvx` runner shape the plugin's
    `.mcp.json` ships (`command: uvx, args: [bettermemory]`) must migrate.
    The pre-fix byte-exact gate no-op'd it — command is `uvx`, not the
    binary path."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "uvx", "args": ["bettermemory"]},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert result["migrated_from_legacy"] is True
    body = json.loads(target.read_text(encoding="utf-8"))
    assert LEGACY_SERVER_NAME not in body["mcpServers"]


def test_patch_does_not_migrate_uvx_running_a_different_package(
    tmp_path: Path,
) -> None:
    """The `uvx` shape only counts as ours when its args actually launch
    the bettermemory package — `uvx some-other-mcp` under the `memory` key
    is a foreign server and must be left alone."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    LEGACY_SERVER_NAME: {"command": "uvx", "args": ["other-mcp"]},
                }
            }
        ),
        encoding="utf-8",
    )
    result = patch_client_config(target, binary="/x/bm")
    assert result["action"] == "added"
    assert "migrated_from_legacy" not in result
    body = json.loads(target.read_text(encoding="utf-8"))
    # Foreign `memory` server untouched.
    assert body["mcpServers"][LEGACY_SERVER_NAME]["command"] == "uvx"


def test_patch_aborts_when_config_changes_under_us(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrency guard (item 12b): `~/.claude.json` is RMW'd by the live
    Claude Code process. If the file changes on disk between our read and
    our atomic write, we must ABORT loudly (ValueError) rather than clobber
    the client's update. We simulate the race by mutating the file right
    after the baseline signature is captured.

    Mutation-soundness: drop the re-stat guard and the write proceeds, no
    ValueError is raised, and this `pytest.raises` fails."""
    from bettermemory import init as init_mod

    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "y", "args": []}}}),
        encoding="utf-8",
    )
    real_sig = init_mod._config_signature
    calls: list[int] = []

    def racing_sig(path: Path) -> tuple[int, int]:
        calls.append(1)
        sig = real_sig(path)
        if len(calls) == 1:
            # Baseline captured; now a concurrent (non-locking) writer lands
            # a bigger payload, so the size in the signature moves.
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {"other": {"command": "y", "args": []}},
                        "grew_under_us": "xxxxxxxxxxxxxxxxxxxx",
                    }
                ),
                encoding="utf-8",
            )
        return sig

    monkeypatch.setattr(init_mod, "_config_signature", racing_sig)
    with pytest.raises(ValueError, match="changed under us"):
        patch_client_config(target, binary="/x/bm")


def test_patch_aborts_on_write_between_read_and_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU (item A): the baseline signature MUST be snapshotted BEFORE
    `read_text()`, not after. If a non-locking client write lands in the
    read->stat window — after we read the bytes we're about to overwrite but
    before the signature is captured — capturing the signature after the read
    folds that write into the baseline, so the pre-write re-stat matches and
    the client's update is silently clobbered.

    We simulate the race by mutating the file DURING `read_text()` (returning
    the pre-mutation bytes, then landing a bigger payload on disk). With the
    baseline captured before the read, the pre-write re-stat sees the moved
    signature and aborts loudly.

    Mutation-soundness: revert the fix (snapshot after the read) and the
    concurrent write is captured into the baseline, no ValueError is raised,
    and this `pytest.raises` fails.
    """
    target = tmp_path / "config.json"
    original = json.dumps({"mcpServers": {"other": {"command": "y", "args": []}}})
    target.write_text(original, encoding="utf-8")

    real_read_text = Path.read_text
    real_write_text = Path.write_text
    state = {"fired": False}

    def racing_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        content = real_read_text(self, *args, **kwargs)
        if self == target and not state["fired"]:
            state["fired"] = True
            # Non-locking client write lands in the read->stat window: we have
            # the original bytes in hand; the file on disk now grows.
            bigger = json.dumps(
                {
                    "mcpServers": {"other": {"command": "y", "args": []}},
                    "client_wrote_this_between_read_and_stat": "x" * 40,
                }
            )
            real_write_text(self, bigger, encoding="utf-8")
        return content

    monkeypatch.setattr(Path, "read_text", racing_read_text)
    with pytest.raises(ValueError, match="changed under us"):
        patch_client_config(target, binary="/x/bm")


def test_init_via_cli_exits_clean_on_concurrent_write_race(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI exception reconciliation (item B): the concurrent-write-race abort
    (`patch_client_config` raising ValueError "changed under us") must reach
    the CLI's `(OSError, ValueError)` catch and exit 2 with a clean
    `bettermemory init: error: …` message — NOT a raw ValueError traceback /
    exit 1. Same clean-exit contract the malformed-JSON case already has.

    Mutation-soundness: narrow the CLI catch back to `except OSError` and the
    ValueError escapes as itself — `pytest.raises(SystemExit)` then fails.
    """
    import argparse

    from bettermemory import init as init_mod
    from bettermemory.cli.init import add_subparser as init_add_subparser
    from bettermemory.cli.init import run as init_run

    target = tmp_path / "cfg.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "y", "args": []}}}),
        encoding="utf-8",
    )

    real_sig = init_mod._config_signature
    calls: list[int] = []

    def racing_sig(path: Path) -> tuple[int, int]:
        calls.append(1)
        sig = real_sig(path)
        if len(calls) == 1:
            # Baseline captured; a concurrent (non-locking) writer lands a
            # bigger payload, moving the size in the signature.
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {"other": {"command": "y", "args": []}},
                        "grew_under_us": "x" * 40,
                    }
                ),
                encoding="utf-8",
            )
        return sig

    monkeypatch.setattr(init_mod, "_config_signature", racing_sig)

    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    init_add_subparser(sub)
    args = parser.parse_args(
        ["init", "--client", "claude-code", "--config-path", str(target)]
    )

    with pytest.raises(SystemExit) as excinfo:
        init_run(args)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "changed under us" in err
    assert "Traceback (most recent call last)" not in err


def test_cli_init_continue_client_warns_legacy_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continue schema (item C): current Continue reads `mcpServers` as a YAML
    LIST in `config.yaml`; the object-in-`config.json` shape this client target
    writes is a deprecated format current Continue ignores. Rather than
    silently write a shape that does nothing, `init --client continue` warns to
    stderr (so `--json` stdout stays clean) and points at the correct YAML list
    form, while still writing the legacy entry for backward compatibility.

    Mutation-soundness: drop the warning emit and the `config.yaml` assertion
    below fails.
    """
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.json"
    cli_init(**_shared_kwargs(client="continue", config_path=target))
    captured = capsys.readouterr()
    # Still writes the legacy entry (backward compat for old Continue)…
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["mcpServers"][DEFAULT_SERVER_NAME]["command"] == "/fake/bm"
    # …but warns that current Continue ignores this shape and points at YAML.
    assert "config.yaml" in captured.err
    assert "deprecated" in captured.err.lower()


def test_patch_locks_private_sidecar_not_client_lock_name(tmp_path: Path) -> None:
    """The RMW lock must live at the bettermemory-private
    `<target>.bettermemory.lock`, never at `<target>.lock`. The default name
    collides with the mkdir-style directory lock Claude Code takes on its own
    config: the 3.15.0 sidecar file there read as "lock held" to the client's
    mkdir and made its stale-lock rmdir fail ENOTDIR forever — config saves
    broken until the file was hand-deleted. Reverting to the default suffix
    recreates `<target>.lock` and fails both assertions."""
    from bettermemory.init import patch_client_config

    target = tmp_path / "config.json"
    patch_client_config(target, binary="/usr/local/bin/bettermemory")
    assert (tmp_path / "config.json.bettermemory.lock").exists()
    assert not (tmp_path / "config.json.lock").exists()


def test_patch_survives_client_dir_lock_and_leaves_it_alone(tmp_path: Path) -> None:
    """The reverse collision direction: while the owning client holds (or has
    crash-leaked) its mkdir-style lock DIRECTORY at `<target>.lock`,
    bettermemory init must still work — 3.15.0's `os.open` on that exact path
    died with EISDIR (exit 2) until the client cleaned its own lock — and must
    not touch the client's directory."""
    from bettermemory.init import patch_client_config

    target = tmp_path / "config.json"
    client_lock_dir = tmp_path / "config.json.lock"
    client_lock_dir.mkdir()

    result = patch_client_config(target, binary="/usr/local/bin/bettermemory")
    assert result["action"] == "added"
    # The client's lock directory is not ours to judge — left untouched.
    assert client_lock_dir.is_dir()
    assert "removed_stale_lockfile" not in result


def test_patch_heals_stale_regular_lockfile(tmp_path: Path) -> None:
    """A 0-byte REGULAR FILE at `<target>.lock` is the exact artifact 3.15.0
    left (its persistent flock sidecar) and wedges Claude Code's own config
    lock; init removes it and reports the healing. A NON-empty file at that
    name may be some other tool's lock with content — left alone."""
    from bettermemory.init import patch_client_config

    poisoned = tmp_path / "poisoned" / "config.json"
    poisoned.parent.mkdir()
    stale = tmp_path / "poisoned" / "config.json.lock"
    stale.touch()
    result = patch_client_config(poisoned, binary="/usr/local/bin/bettermemory")
    assert not stale.exists()
    assert result["removed_stale_lockfile"] == str(stale)

    foreign = tmp_path / "foreign" / "config.json"
    foreign.parent.mkdir()
    other_tools_lock = tmp_path / "foreign" / "config.json.lock"
    other_tools_lock.write_text("pid: 4242\n", encoding="utf-8")
    result = patch_client_config(foreign, binary="/usr/local/bin/bettermemory")
    assert other_tools_lock.exists()
    assert "removed_stale_lockfile" not in result


def test_patch_aborts_when_target_created_under_us(tmp_path: Path) -> None:
    """Create-path twin of the signature guard: when the target did not exist
    at read time, there is no baseline signature — 3.15.0 skipped the pre-write
    re-check entirely, so a client CREATING its config during init's window was
    silently replaced by the skeleton doc. The pre-write guard must abort
    loudly when a file has appeared. Deleting the create-path arm makes the
    write land and this test fail."""
    from bettermemory.init import patch_client_config

    calls = {"n": 0}

    class CreatedUnderUs(type(Path())):  # type: ignore[misc]
        def exists(self, **kwargs: Any) -> bool:
            # First probe (the read branch): absent. Every later probe (the
            # pre-write guard): present — as if the client created the file
            # in between.
            calls["n"] += 1
            return calls["n"] > 1

    target = CreatedUnderUs(tmp_path / "created" / "config.json")
    with pytest.raises(ValueError, match="created under us"):
        patch_client_config(target, binary="/usr/local/bin/bettermemory")
    assert calls["n"] >= 2
    # Nothing was written over the "client's" new file.
    assert not Path(str(target)).exists()


def test_recognizer_accepts_version_pinned_uv_shapes() -> None:
    """Version-pinned uvx idioms (`bettermemory@latest`, `bettermemory==X`)
    and the Windows `uvx.exe` spelling are blessed runner shapes; the
    byte-exact arg gate missed them, so doctor reported a healthy pinned
    install as absent and init re-registered it (the 1.0→1.1 duplicate).
    A DIFFERENT distribution that merely starts with the name must not
    match."""
    from bettermemory.init import command_launches_bettermemory as launches

    binary = "/usr/local/bin/bettermemory"
    assert launches("uvx", ["bettermemory@latest"], binary)
    assert launches("uvx", ["bettermemory==3.15.0"], binary)
    assert launches("uvx.exe", ["bettermemory"], binary)
    assert launches("uv", ["tool", "run", "bettermemory"], binary)
    assert launches("uvx", ["--from", "bettermemory", "bettermemory"], binary)
    assert not launches("uvx", ["bettermemory-evil@1.0"], binary)


def test_recognizer_ignores_with_dependency_position() -> None:
    """bettermemory appearing as a `--with` DEPENDENCY of a foreign server
    must not be recognized as ours — the any-arg scan matched it, and init
    would then delete and rewrite that unrelated entry to launch bettermemory
    instead of the server the user configured."""
    from bettermemory.init import command_launches_bettermemory as launches

    binary = "/usr/local/bin/bettermemory"
    assert not launches(
        "uv", ["run", "--with", "bettermemory", "other-mcp-server"], binary
    )
    assert not launches("uvx", ["--with", "bettermemory", "other-mcp-server"], binary)


# ---------------------------------------------------------------------------
# Hermes Agent — the YAML `mcp_servers` target (7.11.0)
# ---------------------------------------------------------------------------

# The shape of a real Hermes config: hand-ordered keys, an inline comment,
# and the commented tail Hermes's own installer leaves for the owner to
# uncomment. Everything outside the spliced region has to survive the
# patch byte for byte.
_HERMES_DOC = (
    "# Hermes Agent configuration\n"
    "model:\n"
    "  default: z-ai/glm-5.3-flash\n"
    "  provider: openrouter\n"
    "agent:\n"
    "  max_turns: 500   # keep\n"
    "memory:\n"
    "  memory_enabled: false\n"
    "\n"
    "# ── Security ────────────────────────────────\n"
    "# security:\n"
    "#   redact_secrets: true\n"
)


def _hermes_entry(binary: str, client: str = "hermes") -> dict[str, Any]:
    return {"command": binary, "args": [], "env": {ENV_CLIENT: client}}


def _hermes_block(binary: str, *, indent: int = 0) -> str:
    """The YAML lines `patch_hermes_config` writes for a fresh entry, at a
    given indentation — what a splice test compares its region to."""
    text = hermes_snippet_text({DEFAULT_SERVER_NAME: _hermes_entry(binary)})
    pad = " " * indent
    return "".join(pad + line for line in text.splitlines(keepends=True))


def _hermes_text(binary: str) -> str:
    return hermes_snippet_text(hermes_snippet(binary=binary, client="hermes"))


def test_hermes_client_is_registered_with_the_yaml_format() -> None:
    """`KNOWN_CLIENTS["hermes"]` points at `~/.hermes/config.yaml` and
    carries the YAML format; every other client keeps the JSON default,
    so the new field is additive for them."""
    hermes = KNOWN_CLIENTS["hermes"]()
    assert hermes.format == FORMAT_HERMES_YAML
    assert hermes.paths[0] == Path.home() / ".hermes" / "config.yaml"
    for key, getter in KNOWN_CLIENTS.items():
        if key != "hermes":
            assert getter().format == FORMAT_MCP_SERVERS_JSON, key


def test_hermes_snippet_declares_the_client_and_carries_no_type_key() -> None:
    """Hermes documents `command`, `args` and `env` for a stdio server and
    no `type`; the JSON snippet's `type: stdio` must not leak into the
    YAML. The env block carries the one out-of-band declaration."""
    snippet = hermes_snippet(binary="/x/bm", client="hermes")
    assert snippet == {
        HERMES_SERVERS_KEY: {DEFAULT_SERVER_NAME: _hermes_entry("/x/bm")}
    }
    text = hermes_snippet_text(snippet)
    assert text.startswith(f"{HERMES_SERVERS_KEY}:\n")
    assert "type" not in text
    assert "mcpServers" not in text
    assert yaml.safe_load(text) == snippet
    undeclared = hermes_snippet(binary="/x/bm")
    assert undeclared[HERMES_SERVERS_KEY][DEFAULT_SERVER_NAME]["env"] == {}


def test_patch_hermes_appends_a_block_and_keeps_every_owner_byte(
    tmp_path: Path,
) -> None:
    """G1. A config with no `mcp_servers` key gains one block after its
    last line — comments, ordering and the commented tail untouched."""
    target = tmp_path / "config.yaml"
    target.write_text(_HERMES_DOC, encoding="utf-8")
    result = patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert result["action"] == "added"
    assert result["binary"] == "/x/bm"
    assert result["path"] == str(target)
    patched = target.read_text(encoding="utf-8")
    assert patched == _HERMES_DOC + "\n" + _hermes_text("/x/bm")
    loaded = yaml.safe_load(patched)
    assert loaded[HERMES_SERVERS_KEY] == {DEFAULT_SERVER_NAME: _hermes_entry("/x/bm")}
    before = yaml.safe_load(_HERMES_DOC)
    assert {k: v for k, v in loaded.items() if k != HERMES_SERVERS_KEY} == before


def test_patch_hermes_inserts_into_an_existing_block_keeping_siblings(
    tmp_path: Path,
) -> None:
    """A block with other servers gains ours as its first child; the
    sibling, its comment, the blank line and the keys after the block
    are the same bytes as before."""
    doc = (
        "model:\n"
        "  default: z-ai/glm-5.3-flash\n"
        "mcp_servers:\n"
        "  # the filesystem server\n"
        "  filesystem:\n"
        "    command: npx\n"
        "    args: ['-y', 'some-server']\n"
        "\n"
        "# tail comment\n"
        "updates:\n"
        "  check: true\n"
    )
    target = tmp_path / "config.yaml"
    target.write_text(doc, encoding="utf-8")
    result = patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert result["action"] == "added"
    patched = target.read_text(encoding="utf-8")
    old = doc.splitlines(keepends=True)
    new = patched.splitlines(keepends=True)
    key_line = old.index("mcp_servers:\n")
    inserted = _hermes_block("/x/bm", indent=2).splitlines(keepends=True)
    assert new == old[: key_line + 1] + inserted + old[key_line + 1 :]
    loaded = yaml.safe_load(patched)
    assert loaded["mcp_servers"]["filesystem"] == {
        "command": "npx",
        "args": ["-y", "some-server"],
    }
    assert loaded["mcp_servers"][DEFAULT_SERVER_NAME] == _hermes_entry("/x/bm")
    assert loaded["updates"] == {"check": True}


def test_patch_hermes_replaces_an_existing_entry_in_place(tmp_path: Path) -> None:
    """Our entry is rewritten where it stands; the blank line, the
    comment and the sibling after it, and the keys before the block, do
    not move."""
    doc = (
        "model:\n"
        "  default: z-ai/glm-5.3-flash\n"
        "mcp_servers:\n"
        "  bettermemory:\n"
        "    command: /old/bm\n"
        "    args: []\n"
        "    env: {}\n"
        "\n"
        "  # the filesystem server\n"
        "  filesystem:\n"
        "    command: npx\n"
        "updates:\n"
        "  check: true\n"
    )
    target = tmp_path / "config.yaml"
    target.write_text(doc, encoding="utf-8")
    result = patch_hermes_config(target, binary="/new/bm", client="hermes")
    assert result["action"] == "updated"
    old = doc.splitlines(keepends=True)
    new = target.read_text(encoding="utf-8").splitlines(keepends=True)
    start = old.index("  bettermemory:\n")
    end = old.index("\n", start)
    replaced = _hermes_block("/new/bm", indent=2).splitlines(keepends=True)
    assert new == old[:start] + replaced + old[end:]


def test_patch_hermes_is_idempotent_and_updates_only_on_change(
    tmp_path: Path,
) -> None:
    """G2. A second run is a noop that leaves the mtime alone; a changed
    binary is an update that touches only the command line."""
    target = tmp_path / "config.yaml"
    target.write_text(_HERMES_DOC, encoding="utf-8")
    first = patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert first["action"] == "added"
    once = target.read_text(encoding="utf-8")
    mtime = target.stat().st_mtime_ns
    second = patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert second["action"] == "noop"
    assert target.stat().st_mtime_ns == mtime
    assert target.read_text(encoding="utf-8") == once
    third = patch_hermes_config(target, binary="/new/bm", client="hermes")
    assert third["action"] == "updated"
    expected = once.replace("command: /x/bm", "command: /new/bm")
    assert target.read_text(encoding="utf-8") == expected


def test_patch_hermes_keeps_user_keys_and_a_declared_client(tmp_path: Path) -> None:
    """G3. The user's `env` (a relocated store, a client they named
    themselves), Hermes-only keys and `enabled` survive; the HTTP-only
    `url` is shed so the stdio entry cannot become a hybrid."""
    doc = (
        "mcp_servers:\n"
        "  bettermemory:\n"
        "    command: /old/bm\n"
        "    args: []\n"
        "    env:\n"
        "      BETTERMEMORY_DIR: /custom/store\n"
        "      BETTERMEMORY_CLIENT: hermes-desk\n"
        "    idle_timeout_seconds: 600\n"
        "    enabled: true\n"
        "    url: https://old.example/mcp\n"
    )
    target = tmp_path / "config.yaml"
    target.write_text(doc, encoding="utf-8")
    result = patch_hermes_config(target, binary="/new/bm", client="hermes")
    assert result["action"] == "updated"
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["bettermemory"] == {
        "command": "/new/bm",
        "args": [],
        "env": {
            "BETTERMEMORY_DIR": "/custom/store",
            "BETTERMEMORY_CLIENT": "hermes-desk",
        },
        "idle_timeout_seconds": 600,
        "enabled": True,
    }
    again = patch_hermes_config(target, binary="/new/bm", client="hermes")
    assert again["action"] == "noop"


@pytest.mark.parametrize(
    "block",
    [
        "mcp_servers:\n",
        "mcp_servers: {}\n",
        "mcp_servers: {filesystem: {command: npx, args: []}}  # inline\n",
    ],
)
def test_patch_hermes_rewrites_null_and_flow_blocks_as_block_mappings(
    tmp_path: Path, block: str
) -> None:
    """A null or flow-style `mcp_servers` cannot take a child textually;
    the key's span becomes a block mapping carrying every entry it had
    plus ours, an inline comment kept on the key line, the neighbouring
    keys untouched."""
    doc = "a: 1\n" + block + "b: 2\n"
    target = tmp_path / "config.yaml"
    target.write_text(doc, encoding="utf-8")
    result = patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert result["action"] == "added"
    patched = target.read_text(encoding="utf-8")
    assert patched.startswith("a: 1\nmcp_servers:")
    assert patched.endswith("b: 2\n")
    loaded = yaml.safe_load(patched)
    assert loaded["a"] == 1
    assert loaded["b"] == 2
    assert loaded["mcp_servers"][DEFAULT_SERVER_NAME] == _hermes_entry("/x/bm")
    if "filesystem" in block:
        assert loaded["mcp_servers"]["filesystem"] == {"command": "npx", "args": []}
        assert "mcp_servers: # inline\n" in patched


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ("mcp_servers: [\n", "not valid YAML"),
        ("- a\n- b\n", "block mapping at its root"),
        ("{a: 1}\n", "block mapping at its root"),
        ("mcp_servers: [1, 2]\n", "not a mapping"),
        ("mcp_servers: text\n", "not a mapping"),
    ],
)
def test_patch_hermes_refuses_documents_it_cannot_splice(
    tmp_path: Path, doc: str, message: str
) -> None:
    """The JSON path's refusals, on YAML: a document that does not parse,
    a root that is not a block mapping, a `mcp_servers` that is not a
    mapping. Each is a ValueError and the file is left byte-identical."""
    target = tmp_path / "config.yaml"
    target.write_text(doc, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert target.read_text(encoding="utf-8") == doc


def test_patch_hermes_aborts_when_config_changes_under_us(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4. The JSON path's signature guard, on the YAML path: a write that
    lands between the baseline snapshot and the pre-write re-check aborts
    with ValueError and the racing writer's bytes are what remain."""
    from bettermemory import init as init_mod

    target = tmp_path / "config.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    real_sig = init_mod._config_signature
    calls: list[int] = []
    raced = "a: 1\ngrew_under_us: xxxxxxxxxxxxxxxxxxxx\n"

    def racing_sig(path: Path) -> tuple[int, int]:
        calls.append(1)
        sig = real_sig(path)
        if len(calls) == 1:
            path.write_text(raced, encoding="utf-8")
        return sig

    monkeypatch.setattr(init_mod, "_config_signature", racing_sig)
    with pytest.raises(ValueError, match="changed under us"):
        patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert target.read_text(encoding="utf-8") == raced


def test_patch_hermes_aborts_when_target_created_under_us(tmp_path: Path) -> None:
    """G4, create path: a file that appears between the read and the
    write is somebody else's brand-new config, not ours to replace."""
    calls = {"n": 0}

    class CreatedUnderUs(type(Path())):  # type: ignore[misc]
        def exists(self, **kwargs: Any) -> bool:
            calls["n"] += 1
            return calls["n"] > 1

    target = CreatedUnderUs(tmp_path / "created" / "config.yaml")
    with pytest.raises(ValueError, match="created under us"):
        patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert calls["n"] >= 2
    assert not Path(str(target)).exists()


@pytest.mark.parametrize(
    "bad_block",
    [
        "mcp_servers: [broken\n",
        "mcp_servers:\n  somebody_else:\n    command: x\n",
    ],
)
def test_patch_hermes_refuses_a_splice_that_does_not_parse_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_block: str
) -> None:
    """The splice is checked before the write: a rendering that fails to
    parse, or parses to the wrong entry, is refused and the file is left
    as it was. Only a monkeypatched renderer can make the check fire —
    which is the point of having it."""
    from bettermemory import init as init_mod

    monkeypatch.setattr(init_mod, "_dump_yaml_block", lambda *a, **k: bad_block)
    target = tmp_path / "config.yaml"
    target.write_text(_HERMES_DOC, encoding="utf-8")
    with pytest.raises(ValueError, match="did not parse back"):
        patch_hermes_config(target, binary="/x/bm", client="hermes")
    assert target.read_text(encoding="utf-8") == _HERMES_DOC


def test_patch_hermes_creates_and_fills_empty_or_comment_only_files(
    tmp_path: Path,
) -> None:
    """A missing file is created with just the block; a comment-only file
    keeps its comment and gains the block after a blank line."""
    fresh = tmp_path / "new" / "config.yaml"
    assert patch_hermes_config(fresh, binary="/x/bm", client="hermes")["action"] == (
        "added"
    )
    assert fresh.read_text(encoding="utf-8") == _hermes_text("/x/bm")
    commented = tmp_path / "config.yaml"
    commented.write_text("# nothing here yet\n", encoding="utf-8")
    result = patch_hermes_config(commented, binary="/x/bm", client="hermes")
    assert result["action"] == "added"
    assert commented.read_text(encoding="utf-8") == (
        "# nothing here yet\n\n" + _hermes_text("/x/bm")
    )


def test_read_server_entries_reads_each_document_shape(tmp_path: Path) -> None:
    """The loader doctor reads every client through: the JSON `mcpServers`
    object and the YAML `mcp_servers` map; nothing registered is an empty
    map, a file that does not parse is the caller's `unreadable`."""
    json_path = tmp_path / "c.json"
    json_path.write_text(
        json.dumps({"mcpServers": {"x": {"command": "y"}}}), encoding="utf-8"
    )
    assert read_server_entries(json_path) == {"x": {"command": "y"}}
    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text("mcp_servers:\n  x:\n    command: y\n", encoding="utf-8")
    assert read_server_entries(yaml_path, config_format=FORMAT_HERMES_YAML) == {
        "x": {"command": "y"}
    }
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert read_server_entries(empty, config_format=FORMAT_HERMES_YAML) == {}
    listy = tmp_path / "list.json"
    listy.write_text("[1]", encoding="utf-8")
    assert read_server_entries(listy) == {}
    json_path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_server_entries(json_path)
    yaml_path.write_text("mcp_servers: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        read_server_entries(yaml_path, config_format=FORMAT_HERMES_YAML)


def test_cli_init_hermes_print_only_prints_the_yaml_block(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.yaml"
    cli_init(**_shared_kwargs(client="hermes", config_path=target, print_only=True))
    assert not target.exists()
    out = capsys.readouterr().out
    body, _, note = out.partition("\n#")
    assert yaml.safe_load(body) == hermes_snippet(binary="/fake/bm", client="hermes")
    assert "mcpServers" not in out
    assert str(target) in note


def test_cli_init_hermes_json_view_carries_the_yaml_and_the_patch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.yaml"
    cli_init(**_shared_kwargs(client="hermes", config_path=target, json_out=True))
    parsed = json.loads(capsys.readouterr().out)
    assert yaml.safe_load(parsed["snippet_yaml"]) == hermes_snippet(
        binary="/fake/bm", client="hermes"
    )
    assert parsed["clients"]["hermes"]["format"] == FORMAT_HERMES_YAML
    assert parsed["clients"]["claude-code"]["format"] == FORMAT_MCP_SERVERS_JSON
    assert parsed["patch"]["action"] == "added"
    assert parsed["patch"]["path"] == str(target)
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["bettermemory"] == _hermes_entry("/fake/bm")


def test_cli_init_json_view_omits_the_yaml_for_json_clients(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    cli_init(
        **_shared_kwargs(
            client="cursor", config_path=tmp_path / "mcp.json", json_out=True
        )
    )
    parsed = json.loads(capsys.readouterr().out)
    assert "snippet_yaml" not in parsed


def test_cli_init_hermes_patch_mode_writes_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    target = tmp_path / "config.yaml"
    cli_init(**_shared_kwargs(client="hermes", config_path=target))
    out = capsys.readouterr().out
    assert str(target) in out
    assert "added" in out
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["bettermemory"] == _hermes_entry("/fake/bm")


def test_cli_client_choices_match_the_registry() -> None:
    """`cli/init.py` lists the `--client` choices by hand (the CLI module
    keeps `init.py` off its import path until dispatch); this pins them to
    `KNOWN_CLIENTS` so a client added on one surface cannot be missing
    from the other."""
    import argparse

    from bettermemory.cli.init import add_subparser

    init_parser = add_subparser(argparse.ArgumentParser().add_subparsers())
    choices = next(
        action.choices
        for action in init_parser._actions
        if "--client" in action.option_strings
    )
    assert choices is not None
    assert set(choices) == set(KNOWN_CLIENTS)
