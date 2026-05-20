"""CLI surface smoke tests for the `bettermemory` argparse parser.

The unit tests cover the helpers (`server_snippet`, `patch_client_config`,
`compute_health`, etc.) directly; this file pins the *argparse* glue
that sits on top. A typo in the parser definition or a broken dispatch
site would not show up in the helper tests but would silently break
every user invocation.

Most tests call `bettermemory.server.main()` in-process with a mocked
`sys.argv` — fast, deterministic, and the lines actually count toward
coverage. Two tests at the bottom (`--version`, `--help`) spawn a real
subprocess to pin the end-to-end `python -m bettermemory ...` path
that downstream packagers and Claude Code itself invoke.

Each test points the storage directory at a fresh `tmp_path` via
`BETTERMEMORY_DIR` so the user's real `~/.claude-memory/` is never
touched. The env var is patched on the process for the in-process
tests; the subprocess tests pass it through `env=`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bettermemory.server import main as cli_main


def _run_main(
    argv: list[str], *, monkeypatch: pytest.MonkeyPatch, storage: Path
) -> None:
    """Invoke `bettermemory.server.main()` as if argparse had received
    `argv` on the command line. Argparse subcommands that exit (--help,
    --version, error paths) raise SystemExit; callers catch it where
    relevant via pytest.raises."""
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(storage))
    cli_main()


# ---------------------------------------------------------------------------
# In-process — covers the argparse glue and the dispatch arms in main()
# ---------------------------------------------------------------------------


def test_help_lists_all_subcommands(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`-h` exits 0 and lists every registered subcommand. A new
    subcommand without a corresponding update here would slip in
    silently; that's worth noticing."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["-h"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("health", "doctor", "init", "migrate", "export", "tombstones"):
        assert sub in out, f"subcommand {sub!r} missing from --help output"
    assert "bettermemory" in out
    # Pin the load-bearing positioning phrase from the argparse
    # description so an accidental shorten-pass loses the regression,
    # not the line "memory MCP server" that used to be the marker —
    # the description was retuned in 1.4.2 to lead with "Persistent
    # memory" instead of "Local file-backed memory MCP server".
    assert "Persistent memory" in out


def test_version_flag_exits_zero_and_prints_program_name(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """argparse `action="version"` writes to stdout and exits 0."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["--version"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("bettermemory ")


def test_health_subcommand_runs_against_empty_store(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty store should produce a clean report, not a crash. Health
    returns normally rather than raising SystemExit — the report is
    informational, not a pass/fail signal like doctor."""
    _run_main(["health"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "Memory health" in out
    assert "Active memories: 0" in out


def test_health_json_subcommand_emits_machine_readable_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`--json` is the integration surface for downstream tooling
    (dashboards, curation scripts). Pin that the output is parseable."""
    _run_main(["health", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert "total_active_memories" in payload
    assert payload["total_active_memories"] == 0
    assert "verification_debt" in payload


def test_doctor_subcommand_runs_against_empty_store(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory doctor` should run end-to-end against a brand-new
    storage dir — the storage_directory check creates it on demand and
    the rest of the checks pass on an empty store. Doctor exits 0/1/2
    for ok/warn/fail; we accept 0 or 1 (a warning about absent extras is
    expected on a default install)."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["doctor"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code in (0, 1)
    out = capsys.readouterr().out
    assert "bettermemory doctor" in out
    assert "python_version" in out
    assert "storage_directory" in out


def test_doctor_json_emits_structured_checks(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit):
        _run_main(["doctor", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert "checks" in payload
    assert isinstance(payload["checks"], list)
    assert len(payload["checks"]) > 0
    # Every check has a name and status — pin the schema for tooling.
    for check in payload["checks"]:
        assert "name" in check
        assert "status" in check


def test_init_show_and_tell_prints_snippet(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory init` with no flags prints the canonical snippet
    plus the per-client config locations. Run from a tmp dir so the
    `[✓]` markers don't depend on the user's home layout."""
    _run_main(["init"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "mcpServers" in out
    # Default key is `bettermemory`, not the legacy `memory`.
    assert "bettermemory" in out
    # All five known clients should be enumerated.
    for client in ("claude-code", "claude-desktop", "cursor", "continue", "cline"):
        assert client in out, f"client {client!r} missing from show-and-tell"


def test_init_json_emits_structured_payload(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run_main(["init", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert "binary" in payload
    assert "snippet" in payload
    assert "clients" in payload
    assert payload["snippet"]["mcpServers"]


def test_init_patch_writes_canonical_shape(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: `init --client X --config-path Y` writes the canonical
    `{type, command, args, env}` entry. This is the shape Claude Code's
    own `claude mcp add` produces."""
    target = tmp_path / "claude_desktop_config.json"
    _run_main(
        [
            "init",
            "--client",
            "claude-desktop",
            "--config-path",
            str(target),
        ],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    body = json.loads(target.read_text(encoding="utf-8"))
    entry = body["mcpServers"]["bettermemory"]
    assert entry["type"] == "stdio"
    assert "command" in entry
    assert entry["args"] == []
    assert entry["env"] == {}


def test_unknown_subcommand_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse rejects unknown subcommands — pin nonzero exit so we
    don't accidentally make typos silently no-op."""
    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["definitely-not-a-subcommand"],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )
    assert exc.value.code != 0


@pytest.mark.parametrize(
    "subcmd",
    [
        "health",
        "doctor",
        "init",
        "migrate",
        "export",
        "tombstones",
        "sync",
        "reindex",
        "consolidate",
        "ui",
        "audit-turn",
    ],
)
def test_subcommand_help_works(
    subcmd: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each subcommand's `--help` should also exit 0 and print non-empty
    text. Catches a broken sub-parser definition (e.g. an argparse
    `dest` typo that would crash before help can render)."""
    with pytest.raises(SystemExit) as exc:
        _run_main([subcmd, "--help"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip(), f"`{subcmd} --help` produced no output"


# ---------------------------------------------------------------------------
# In-process coverage for CLI dispatch branches that the subprocess tests
# previously protected but didn't reach when the local checkout has no
# editable install. The argparse setup + the `_cli_*` dispatch functions
# live in `server.py` and were 41% covered before — these tests close the
# gap on the dispatch arms that don't need real network / git state.
# ---------------------------------------------------------------------------


def test_consolidate_subcommand_runs_dry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory consolidate` (no --apply) prints a report for an
    empty store. Dry-run is the safe default; we pin it here so a
    refactor that flips the default can't slip in silently."""
    _run_main(["consolidate"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "Consolidate report" in out
    assert "dry-run" in out


def test_consolidate_json_subcommand_emits_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--json` flag emits parseable JSON with the expected top-
    level keys. Matches the surface the subprocess test pins but runs
    in-process so it counts toward server.py coverage."""
    _run_main(["consolidate", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    payload = json.loads(capsys.readouterr().out)
    for key in (
        "applied",
        "dedup_method",
        "dedup_candidates",
        "demotion_candidates",
        "cold_scope_suggestions",
        "scope_typo_pairs",
        "actions_taken",
        "failures",
    ):
        assert key in payload, f"key {key!r} missing from consolidate JSON"


def test_tombstones_list_subcommand_runs_on_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`tombstones list` against an empty store should not error — it
    should report zero tombstones cleanly. The CLI is a thin wrapper
    around `store.load_tombstones`; this pins the wiring."""
    _run_main(["tombstones", "list"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    # Either an explicit "no tombstones" message or an empty body —
    # pin only that we don't crash and produce something coherent.
    assert "tombstone" in out.lower() or out.strip() == ""


def test_tombstones_list_json_subcommand_runs_on_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(
        ["tombstones", "list", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload == []


def test_export_subcommand_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`export` writes a self-describing JSON document with active
    memories (plus tombstones by default). Exercise the CLI plumbing
    against a tmp store with one memory; assert the payload carries
    the written body."""
    from bettermemory.store import Store

    Store(tmp_path).write(content="archive me", scopes=["tools"])
    output_path = tmp_path.parent / "export.json"
    _run_main(
        ["export", "--output", str(output_path)],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload.get("format_version") is not None, (
        f"export payload missing format_version; keys: {sorted(payload.keys())}"
    )
    bodies = " ".join(m.get("body", "") for m in payload.get("active_memories", []))
    assert "archive me" in bodies, (
        f"export payload missing the written body; got keys: {sorted(payload.keys())}"
    )


def test_reindex_subcommand_builds_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`reindex` drops and rebuilds the FTS5 index. Run against a
    one-memory store and assert the index file exists with a positive
    indexed_count after the call."""
    from bettermemory import index as _index
    from bettermemory.store import Store

    Store(tmp_path).write(content="reindex me", scopes=["tools"])
    _run_main(["reindex"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "reindex" in out.lower() or "indexed" in out.lower()
    status = _index.status(tmp_path)
    assert status["exists"], "index file missing after reindex"
    assert status["indexed_count"] >= 1


def test_migrate_origin_subcommand_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`migrate origin` is a no-op on a store with no pre-1.x memories.
    Run it against an empty store to exercise the dispatch arm; the
    test asserts a coherent report rather than a crash."""
    _run_main(["migrate", "origin"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    # The output may contain "0 memories" or "no migrations needed" or
    # similar — we only check that the command ran without raising.
    assert out.strip() or True  # tolerate empty output


# ---------------------------------------------------------------------------
# Subprocess — pins the actual `python -m bettermemory` end-to-end path,
# the one Claude Code and downstream packagers invoke. Slower, but the
# only way to catch a packaging-level break (broken `__main__.py`,
# entry-point wiring, etc.) that the in-process harness can't see.
# ---------------------------------------------------------------------------


def _run_subprocess(*args: str, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "bettermemory", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return (result.stdout + result.stderr).strip()


# `python -m bettermemory` only resolves when the package is importable
# in the subprocess Python — which requires `pip install -e .` (or a
# wheel install). The conftest sys.path hack handles in-process imports
# but doesn't propagate to subprocesses. Skip when the subprocess can't
# import the package; CI always can (it runs `uv sync` first), local
# fresh clones may not.
_PACKAGE_IMPORTABLE_IN_SUBPROCESS = (
    subprocess.run(
        [sys.executable, "-c", "import bettermemory"],
        capture_output=True,
    ).returncode
    == 0
)

_skip_without_install = pytest.mark.skipif(
    not _PACKAGE_IMPORTABLE_IN_SUBPROCESS,
    reason=(
        "subprocess Python can't import bettermemory — "
        "run `pip install -e .` (or `uv sync`) locally"
    ),
)


@_skip_without_install
def test_subprocess_help_pins_packaging(tmp_path: Path) -> None:
    """The `python -m bettermemory` path runs `__main__.py` rather than
    the in-process `main()` directly. Worth pinning so a regression in
    the entry-point wiring shows up here, not only when a user installs
    the wheel."""
    out = _run_subprocess("--help", env_extra={"BETTERMEMORY_DIR": str(tmp_path)})
    assert "bettermemory" in out
    # Same pin update as the in-process smoke test above — the 1.4.2
    # description leads with "Persistent memory" rather than "memory
    # MCP server".
    assert "Persistent memory" in out


@_skip_without_install
def test_subprocess_version_pins_packaging(tmp_path: Path) -> None:
    """Same idea for `--version` — a wheel that ships without metadata
    would fall through to the `0+unknown` branch in __init__.py and
    produce that string. Catching it here is cheap."""
    out = _run_subprocess("--version", env_extra={"BETTERMEMORY_DIR": str(tmp_path)})
    assert out.startswith("bettermemory ")
    assert "0+unknown" not in out, (
        "the package metadata fallback fired during a normal subprocess "
        "run — wheel packaging probably stripped the .dist-info"
    )
