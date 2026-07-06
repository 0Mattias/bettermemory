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
    DEFAULT_SERVER_NAME,
    KNOWN_CLIENTS,
    LEGACY_SERVER_NAME,
    cli_init,
    find_binary,
    patch_client_config,
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
    assert entry["disabled"] is True
    assert entry["timeout"] == 90
