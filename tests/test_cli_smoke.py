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
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from bettermemory import store as store_module
from bettermemory.config import Config, load_config
from bettermemory.models import utcnow
from bettermemory.server import main as cli_main
from bettermemory.store import STORE_FILENAME, Store

from .conftest import shielded_child_env


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
    for sub in (
        "health",
        "init",
        "migrate",
        "export",
        "tombstones",
        "rename-scope",
    ):
        assert sub in out, f"subcommand {sub!r} missing from --help output"
    assert "bettermemory" in out
    # Pin the load-bearing positioning phrase from the argparse
    # description so an accidental shorten-pass loses the regression.
    # Retuned in 1.4.2 to lead with "Persistent memory", after 3.20.0 to
    # "Memory you can verify", and in 9.0 to the one-line description
    # that server.json and the package docstring share.
    assert "MCP memory for coding agents" in out


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


# ---------------------------------------------------------------------------
# Curation subcommands that mutate the store: `tombstones restore` and
# `rename-scope`. These are the CLI escape hatches for the tools gated
# out of the lean default surface. Seed via a Store resolved the SAME way
# the CLI resolves it.
# ---------------------------------------------------------------------------


_FAKE_ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"


def _seeded_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    """A Store rooted at the SAME resolved directory the CLI will use.

    Cross-checking a directly-constructed ``Store(tmp_path)`` against a CLI
    invocation is unsafe on macOS, where ``resolved_directory`` realpaths
    ``BETTERMEMORY_DIR`` (``/var/...`` -> ``/private/var/...``) so the two
    would point at different absolute paths and never see each other's
    writes. Set the env var, then resolve the store the CLI's way.
    """
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    return Store(load_config().resolved_directory())


def _default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the CLI to the shipped defaults (telemetry on) so the event
    assertions below do not depend on the developer's own config.toml.
    `BETTERMEMORY_DIR` still routes the store, exactly as `_seeded_store`
    resolves it."""
    from bettermemory.cli import _common

    monkeypatch.setattr(_common, "load_config", lambda: Config())


def test_tombstones_restore_brings_back_a_removed_memory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`tombstones restore <id>` un-tombstones a memory — the CLI path for
    memory_restore, which isn't registered on the lean default surface.
    It records the same creation-side `restore` event the MCP tool does,
    and the restored row reads as a local re-admission to the store's
    provenance check."""
    store = _seeded_store(tmp_path, monkeypatch)
    memory = store.write(content="restore me", scopes=["tools"])
    store.tombstone(memory.id, reason="oops", session="sess_t")
    assert memory.id not in {m.id for m in store.load_all()}
    _default_config(monkeypatch)

    _run_main(
        ["tombstones", "restore", memory.id], monkeypatch=monkeypatch, storage=tmp_path
    )
    out = capsys.readouterr().out
    assert "Restored" in out
    assert memory.id in out
    assert memory.id in {m.id for m in store.load_all()}
    restores = [e for e in store.iter_events() if e.get("kind") == "restore"]
    assert [(e["id"], e["scopes"], e["attribution"]) for e in restores] == [
        (memory.id, ["tools"], "cli_tombstones_restore")
    ]
    assert store.provenance_for([memory.id]) == {memory.id: "local"}


def test_tombstones_restore_unknown_id_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unknown id surfaces as `parser.error` (exit 2), not a traceback."""
    _seeded_store(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["tombstones", "restore", _FAKE_ULID],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )
    assert exc.value.code == 2


def test_rename_scope_renames_across_memories(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`rename-scope OLD NEW` rewrites the scope on every carrier — the CLI
    path for memory_rename_scope."""
    store = _seeded_store(tmp_path, monkeypatch)
    memory = store.write(content="x", scopes=["infra"])
    _default_config(monkeypatch)

    _run_main(
        ["rename-scope", "infra", "infrastructure"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    out = capsys.readouterr().out
    assert "Renamed scope" in out
    reloaded = {m.id: m for m in store.load_all()}
    assert "infrastructure" in reloaded[memory.id].scopes
    assert "infra" not in reloaded[memory.id].scopes
    renames = [e for e in store.iter_events() if e.get("kind") == "rename_scope"]
    assert [
        (
            e["old"],
            e["new"],
            e["active_count"],
            e["tombstoned_count"],
            e["attribution"],
        )
        for e in renames
    ] == [("infra", "infrastructure", 1, 0, "cli_rename_scope")]


def test_rename_scope_rejects_identical_old_and_new(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OLD == NEW is a no-op masquerading as work; reject it cleanly."""
    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["rename-scope", "tools", "tools"],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )
    assert exc.value.code == 2


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


def _seed_weakly_endorsed_memory(store: Store) -> str:
    """Seed one memory that only the RATIO half of the cold-endorsement
    predicate can catch, and return its id.

    Shape: 30 retrievals (the `_COLD_ENDORSEMENT_MIN_RETRIEVALS` floor)
    and 10 applied events split 1 explicit / 9 auto. `explicit == 0` is
    False, so the always-on binary check never fires; the ratio 0.1 is
    below a 0.25 threshold, so the bucket lights up if and only if the
    caller actually threaded `cold_endorsement_ratio_threshold`. One
    Stop-hook audit event rides along as pure coverage — the CLI path
    arms the telemetry gate, the endorsement leg is gated alongside
    dead_weight, and a hookless fixture would suppress the very bucket
    these tests measure.
    """
    from bettermemory.events import Recorder

    memory = store.write(content="deploy with uv, never pip", scopes=["tools"])
    rec = Recorder(store=store, session_id="sess-cold-endorse")
    for _ in range(30):
        rec.record("search", returned=[memory.id], relevance=["high"])
    rec.record("use", ids=[memory.id], outcome="applied", auto=False)
    for _ in range(9):
        rec.record("use", ids=[memory.id], outcome="applied", auto=True)
    rec.record("turn_audited", triggered_from="stop_hook")
    return memory.id


def _config_with_ratio_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Point `default_config_path` at a temp config.toml whose
    `cold_endorsement_ratio_threshold` is `value`. Real file, real
    `load_config()` — the CLI reaches the knob the way a user's install
    does, so a regression that stops reading config is caught too."""
    from bettermemory.config import DEFAULT_CONFIG

    cfg_dir = tmp_path / "bm-config"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.toml"
    original = "# cold_endorsement_ratio_threshold = 0.0"
    assert original in DEFAULT_CONFIG, "DEFAULT_CONFIG key drifted"
    cfg_path.write_text(
        DEFAULT_CONFIG.replace(original, f"cold_endorsement_ratio_threshold = {value}"),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.config.default_config_path", lambda: cfg_path)


def test_health_honours_configured_cold_endorsement_ratio_threshold(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory health` must compute the same
    `cold_endorsement_memories` bucket `memory_health` computes on the
    same store. The CLI dropped `cold_endorsement_ratio_threshold`,
    pinning its bucket to the strict `explicit == 0` semantics — so a
    user reading the CLI got a different answer than the model read
    from the MCP tool whenever the knob was set.

    Both renderers are pinned because both are user-facing surfaces
    fed by the single `report_for_store` call.
    """
    storage = tmp_path / "store"
    storage.mkdir()
    _config_with_ratio_threshold(tmp_path, monkeypatch, "0.25")
    store = _seeded_store(storage, monkeypatch)
    memory_id = _seed_weakly_endorsed_memory(store)

    _run_main(["health", "--json"], monkeypatch=monkeypatch, storage=storage)
    payload = json.loads(capsys.readouterr().out)
    bucket = payload["cold_endorsement_memories"]
    assert bucket["total"] == 1, "ratio-only row missing — threshold was dropped"
    assert [row["id"] for row in bucket["rows"]] == [memory_id]

    _run_main(["health"], monkeypatch=monkeypatch, storage=storage)
    assert "Cold-endorsement memories (1)" in capsys.readouterr().out


def test_health_default_ratio_threshold_keeps_strict_bucket(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control for the test above: the same store under the default
    `0.0` threshold must NOT surface the row. Without this the ratio
    assertion would still pass if the bucket started flagging
    everything, and the threading fix would be indistinguishable from
    a broken predicate."""
    storage = tmp_path / "store"
    storage.mkdir()
    _config_with_ratio_threshold(tmp_path, monkeypatch, "0.0")
    store = _seeded_store(storage, monkeypatch)
    _seed_weakly_endorsed_memory(store)

    _run_main(["health", "--json"], monkeypatch=monkeypatch, storage=storage)
    payload = json.loads(capsys.readouterr().out)
    assert payload["cold_endorsement_memories"]["total"] == 0


def test_try_subcommand_reproduces_path_drift_offline(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory try` runs the verify→drift→verdict demo in an
    isolated temp store and exits 0 when it reproduces the staleness
    signal (it doubles as a self-test of that whole path). The demo must
    NOT touch the BETTERMEMORY_DIR store — it builds its own tempdir."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["try"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "spot_check_recommended" in out
    assert "path_drift.missing" in out
    # The demo store is isolated: no store file appears under
    # BETTERMEMORY_DIR.
    assert not (tmp_path / STORE_FILENAME).exists()

    # Narrated paths are rendered RELATIVE to the throwaway root. This is the
    # readability half of the demo: a raw
    # /var/folders/vn/…/bettermemory-try-3qjzln9f/src/auth/session.py buries
    # the one thing the output exists to show. Asserting the absence of the
    # tempdir prefix is what makes the rendering portable — the first
    # implementation stripped with a hardcoded "/" and was a silent no-op on
    # Windows, printing absolute paths under a line claiming they were
    # relative, and the matrix stayed green because nothing checked.
    assert "src/auth/session.py" in out
    assert tempfile.gettempdir() not in out
    assert "\\src\\auth" not in out  # no half-stripped Windows path either


def test_try_json_emits_the_raw_hit(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`bettermemory try --json` emits the exact MCP-shaped hit dict, with
    the path-drift-driven verdict and a populated path_drift.missing."""
    import json

    with pytest.raises(SystemExit) as exc:
        _run_main(["try", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    row = json.loads(capsys.readouterr().out)
    assert row["staleness_verdict"] == "spot_check_recommended"
    assert row["verification"]["status"] == "fresh"
    assert row["path_drift"]["missing"]


def test_try_closes_its_store_before_removing_the_directory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every store connection the demo opens is closed by the time its
    temporary directory is removed. Windows refuses to delete a file a
    process holds open: with the connection left open, the cleanup raised
    WinError 32 on `memory.sqlite` after the demo had printed its result."""
    connections: list[sqlite3.Connection] = []
    connect = store_module._connect

    def recording_connect(path: Path) -> sqlite3.Connection:
        conn = connect(path)
        connections.append(conn)
        return conn

    def is_open(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return False
        return True

    open_at_cleanup: list[bool] = []
    cleanup = tempfile.TemporaryDirectory.cleanup

    def checking_cleanup(self: tempfile.TemporaryDirectory[str]) -> None:
        open_at_cleanup.extend(is_open(conn) for conn in connections)
        cleanup(self)

    monkeypatch.setattr(store_module, "_connect", recording_connect)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", checking_cleanup)
    with pytest.raises(SystemExit) as exc:
        _run_main(["try", "--json"], monkeypatch=monkeypatch, storage=tmp_path)
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["path_drift"]["missing"]
    assert open_at_cleanup
    assert not any(open_at_cleanup)


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
    own `claude mcp add` produces — plus, since 7.10.0, the one
    declaration a stdio server can receive out of band: `env` names the
    client (`BETTERMEMORY_CLIENT`), so every write it records says which
    client made it."""
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
    assert entry["env"] == {"BETTERMEMORY_CLIENT": "claude-desktop"}


def test_init_patch_hermes_writes_the_yaml_shape(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: `init --client hermes --config-path Y` writes Hermes's
    YAML `mcp_servers` map — `command`, `args`, `env` and no `type`, the
    keys Hermes documents for a stdio server — with the client declared
    in `env` like every other target."""
    target = tmp_path / "config.yaml"
    _run_main(
        ["init", "--client", "hermes", "--config-path", str(target)],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["mcp_servers"][
        "bettermemory"
    ]
    assert set(entry) == {"command", "args", "env"}
    assert entry["args"] == []
    assert entry["env"] == {"BETTERMEMORY_CLIENT": "hermes"}


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


def _all_registered_subcommands() -> list[str]:
    """Every subcommand in `_build_parser`'s registry, so the `--help`
    smoke sweep below can never drift out of sync with the CLI again (a
    hardcoded list here sat at 12 of 17 entries for several releases).
    Less `hook`, which the registry lists for `bettermemory --help`
    alone: a hook word goes to the hook client before the parser runs
    (`_entry.route_hook_word`, tests/test_hook_words.py)."""
    from bettermemory._daemon_client import HOOK_WORDS
    from bettermemory.cli import _build_parser

    _, subparsers = _build_parser()
    return sorted(set(subparsers) - HOOK_WORDS)


@pytest.mark.parametrize("subcmd", _all_registered_subcommands())
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
# Subcommand --help must SAY WHAT THE COMMAND DOES. argparse renders a
# subparser's `help=` string only in the top-level `bettermemory -h`
# listing; the subparser's own `--help` prints `description=` — which
# every registration used to omit, so e.g. `bettermemory doctor --help`
# (the natural probe for the 3.19.0 sidecar-leak migration surface)
# showed usage + options with no statement of what doctor covers. Each
# add_parser call now passes the same string as both `help=` and
# `description=`; these tests pin the user-visible half of that contract.
#
# argparse wraps the description to the terminal width (COLUMNS-
# dependent), so assertions collapse whitespace first — a substring that
# happens to span a wrap point must not flake with the environment.
# ---------------------------------------------------------------------------


def _flat_help(
    argv: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    storage: Path,
) -> str:
    """Run `bettermemory <argv> --help`, return stdout with whitespace
    collapsed (wrap-point-immune) after asserting exit 0."""
    with pytest.raises(SystemExit) as exc:
        _run_main([*argv, "--help"], monkeypatch=monkeypatch, storage=storage)
    assert exc.value.code == 0
    return " ".join(capsys.readouterr().out.split())


# ---------------------------------------------------------------------------
# In-process coverage for CLI dispatch branches that the subprocess tests
# previously protected but didn't reach when the local checkout has no
# editable install. The argparse setup + the `_cli_*` dispatch functions
# live in `server.py` and were 41% covered before — these tests close the
# gap on the dispatch arms that don't need real network / git state.
# ---------------------------------------------------------------------------


def test_every_cli_recorder_attribution_carries_the_admin_prefix() -> None:
    """Every `cli_recorder(ctx, attribution="...")` in the CLI package
    names an attribution `eval.is_admin_recorded_event` reads as admin.
    The helper does not check the prefix itself (the classification has
    one definition, in `eval`, and its parity scan refuses a second copy
    in `src/`), so this scan holds the coupling from the test side: a
    CLI path that forgets the prefix would put its throwaway session
    into the client census and its rows into the tool tallies."""
    import ast

    from bettermemory.eval import ADMIN_RECORDED_ATTRIBUTION_PREFIX

    cli_root = Path(__file__).resolve().parents[1] / "src" / "bettermemory" / "cli"
    found: list[tuple[str, str]] = []
    for py_file in sorted(cli_root.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "cli_recorder"):
                continue
            literal = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "attribution"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ),
                None,
            )
            assert literal is not None, (
                f"{py_file.name}: cli_recorder call without a literal attribution"
            )
            found.append((py_file.name, literal))
    assert found, "no cli_recorder call sites discovered; the scan is broken"
    bad = [
        (name, value)
        for name, value in found
        if not value.startswith(ADMIN_RECORDED_ATTRIBUTION_PREFIX)
    ]
    assert not bad, f"CLI attributions without the admin prefix: {bad}"
    assert {name for name, _ in found} >= {"rename_scope.py", "tombstones.py"}


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


def test_tombstones_list_invalid_scope_exits_clean_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`tombstones list --scope BADSCOPE` must exit 2 with a clean
    `bettermemory: error: invalid scope …` message on stderr — NOT an
    uncaught `ValueError` traceback that leaks internal file paths.
    Parallel to `test_export_invalid_scope_via_cli_exits_clean_not_traceback`
    in test_export.py; pins that `tombstones list` threads its parser
    through to `parser.error(...)` like the sibling `export` command."""
    with pytest.raises(SystemExit) as excinfo:
        _run_main(
            ["tombstones", "list", "--scope", "BADSCOPE"],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "invalid scope" in err
    # No raw traceback should have leaked to the user.
    assert "Traceback (most recent call last)" not in err


def test_episodes_list_subcommand_runs_on_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`episodes list` on an empty store reports zero entries
    cleanly. CLI is a thin wrapper over `Store.episodes_by_session`."""
    _run_main(["episodes", "list"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "no episodes" in out.lower()


def test_episodes_list_json_subcommand_runs_on_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(
        ["episodes", "list", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload == []


def test_episodes_prune_dry_run_on_empty_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`episodes prune --dry-run` reports zero candidates on an empty
    store and does not touch disk."""
    _run_main(
        ["episodes", "prune", "--dry-run", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_delete"] == []
    assert "ttl_days" in payload


def _seed_backdated_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, days_old: int = 40
) -> Store:
    """Write one episode into the store the CLI will resolve, dated
    `days_old` days ago so a normal 30-day prune would consider its
    session stale. Returns the store so callers can assert the session
    survives or is removed."""
    store = _seeded_store(tmp_path, monkeypatch)
    store.write_episode(
        session_id="sess_smoke01",
        body="ancient takeaway",
        now=utcnow() - timedelta(days=days_old),
    )
    return store


def test_episodes_prune_dry_run_ttl_zero_matches_real_prune(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: `Store.prunable_episode_sessions` early-returns [] for
    ttl_days <= 0 (a non-positive TTL is a no-op). The CLI dry-run must
    use the SAME predicate — otherwise it lists every session as "would
    delete" while a real prune deletes nothing, i.e. the dry-run lies.
    Even with a 40-day-old session present, ttl_days=0 dry-run must report
    an empty would_delete set, and a real ttl=0 prune must delete nothing."""
    store = _seed_backdated_episode(tmp_path, monkeypatch, days_old=40)
    assert store.episodes_by_session("sess_smoke01")

    _run_main(
        ["episodes", "prune", "--dry-run", "--ttl-days", "0", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    dry_payload = json.loads(capsys.readouterr().out)
    assert dry_payload["would_delete"] == []
    assert dry_payload["ttl_days"] == 0
    assert store.episodes_by_session("sess_smoke01")

    _run_main(
        ["episodes", "prune", "--ttl-days", "0", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    real_payload = json.loads(capsys.readouterr().out)
    assert real_payload["deleted"] == []
    assert store.episodes_by_session("sess_smoke01")


def test_episodes_prune_dry_run_ttl_zero_text_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same ttl<=0 guard, text mode: must print the "No episode sessions
    older than 0 days." line rather than a "Would delete …" list."""
    store = _seed_backdated_episode(tmp_path, monkeypatch, days_old=40)
    assert store.episodes_by_session("sess_smoke01")

    _run_main(
        ["episodes", "prune", "--dry-run", "--ttl-days", "0"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    out = capsys.readouterr().out
    assert "No episode sessions older than 0 days." in out
    assert "Would delete" not in out
    assert store.episodes_by_session("sess_smoke01")


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


# ---------------------------------------------------------------------------
# Subprocess — pins the actual `python -m bettermemory` end-to-end path,
# the one Claude Code and downstream packagers invoke. Slower, but the
# only way to catch a packaging-level break (broken `__main__.py`,
# entry-point wiring, etc.) that the in-process harness can't see.
# ---------------------------------------------------------------------------


def _run_subprocess(*args: str, env_extra: dict[str, str] | None = None) -> str:
    env = shielded_child_env()
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
# in the subprocess Python. The probe and the actual invocations run
# under `shielded_child_env()` (the child-process leg of the conftest
# import shield), so a hidden editable `.pth` alone can no longer make
# them skip. The skip-guard stays as a fallback for genuinely broken
# installs (e.g. runtime deps missing in the subprocess Python); CI
# always passes the probe (it runs `uv sync` first), local fresh clones
# may not.
_PACKAGE_IMPORTABLE_IN_SUBPROCESS = (
    subprocess.run(
        [sys.executable, "-c", "import bettermemory"],
        capture_output=True,
        env=shielded_child_env(),
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
    # Same pin as the in-process smoke test above: the description leads
    # with the line server.json and the package docstring share.
    assert "MCP memory for coding agents" in out


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


# ---------------------------------------------------------------------------
# Registry-vs-dispatch parity for `cli/__init__.py` (Class 7 — closed by
# this commit).
#
# `_build_parser` returns a `subparsers: dict[str, ArgumentParser]` (one
# entry per `bettermemory <cmd>` subcommand) and `main()` sequentially
# dispatches with a chain of `if cmd == "<key>": ... return` arms. The
# two enumerations MUST agree:
#
#   - Registry-only (dict key without dispatch arm): `bettermemory foo`
#     parses cleanly (argparse accepts the subcommand) but falls through
#     to `parser.error(f"unknown subcommand: {cmd!r}")` — user-visible
#     failure that looks like a typo even though the subcommand was
#     just registered.
#   - Dispatch-only (arm without dict key): unreachable code; argparse
#     rejects the subcommand before dispatch ever sees it.
#
# Other coverage doesn't catch either drift: the per-subcommand
# `test_subcommand_help_works` parametrise above derives from the
# registry dict (so it follows registry drift rather than detecting it),
# `test_help_lists_all_subcommands` only asserts a fixed hand-picked
# subset of the names, and the direct-import smoke tests don't
# cross-check the two enumerations.
# Hazard tier: medium-high (user-visible CLI fallback on the
# registry-drift side; silent unreachable code on the dispatch-only
# side).
#
# Implementation note: the test AST-walks `main()`'s source rather than
# instrumenting the dispatch (no test-only hooks in production code).
# Filter requires `ast.Eq` ops specifically — `if cmd is None:` at the
# top of `main()` would otherwise match (`ast.Is` vs `ast.Eq`) and pull
# `None` into the arms set, which would always trip the assertion.
# Stringly-typed only (`isinstance(value, str)`) so a hypothetical
# `if cmd == 42:` doesn't crash the sort in the failure message.
#
# Negative-control verified at commit time (see commit message for
# detail).
# ---------------------------------------------------------------------------


def test_subparser_registry_matches_main_dispatch() -> None:
    """Every key in `_build_parser`'s `subparsers` dict MUST have a
    corresponding `if cmd == "<key>"` arm in `main()`, and vice versa.
    Drift on the registry side produces a user-visible
    `parser.error("unknown subcommand: ...")` fallback; drift on the
    dispatch side produces unreachable code that argparse never reaches.

    Closes Class 7 (same-file string-key registry-dict vs sequential
    dispatch-arm parity) from the tick-25 Branch B audit."""
    import ast
    import inspect

    from bettermemory._daemon_client import HOOK_WORDS
    from bettermemory.cli import _build_parser, main

    _, subparsers = _build_parser()
    # `hook` is registered for `--help` alone: `main` hands every hook
    # word to the hook client before the parser runs
    # (`_entry.route_hook_word`), so it has no arm.
    assert set(subparsers) & HOOK_WORDS == {"hook"}
    subparsers = {k: v for k, v in subparsers.items() if k not in HOOK_WORDS}
    main_src = inspect.getsource(main)
    tree = ast.parse(main_src)
    arms: set[str] = set()
    for node in ast.walk(tree):
        # Filter for `if cmd == "<literal-string>":` specifically — the
        # `if cmd is None:` early-return at the top of `main()` is an
        # `ast.Is` op, not `ast.Eq`, and would otherwise drag `None`
        # into the set and trip the assertion on every run.
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and len(node.test.comparators) == 1
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "cmd"
            and isinstance(node.test.comparators[0], ast.Constant)
            and isinstance(node.test.comparators[0].value, str)
        ):
            arms.add(node.test.comparators[0].value)
    assert set(subparsers) == arms, (
        "cli subparser registry / main() dispatch arms drifted; "
        "see cli/__init__.py:_build_parser (subparsers dict) and "
        "cli/__init__.py:main (if cmd == '<key>' chain). "
        f"registry-only={set(subparsers) - arms} (would fall through "
        f"to parser.error 'unknown subcommand'); "
        f"dispatch-only={arms - set(subparsers)} (unreachable code, "
        "argparse rejects the subcommand before dispatch sees it)."
    )


# ---------------------------------------------------------------------------
# `bettermemory session-start` from the console: the 8.x alias of
# `bettermemory hook session-start`, which the process entry point hands to
# the hook client (`_entry.route_hook_word`). The brief itself is pinned
# through `recall.session_brief` (tests/test_recall_brief.py).
# ---------------------------------------------------------------------------


@_skip_without_install
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the reproducer needs POSIX pipe semantics: closing the read end "
        "must make the child's stdout write fail. Windows signals a broken "
        "pipe differently and the 120 path is not the one under test."
    ),
)
def test_session_start_exits_0_when_the_reader_hung_up(tmp_path: Path) -> None:
    """The hook client's `_blackhole_stdout` salvage, guarded where it
    actually bites.

    Catching the write error and exiting 0 is not sufficient on its own:
    a failed flush leaves the bytes in the buffer, so CPython's own
    shutdown flush retries them, fails again, and overrules the exit
    code with 120 — after `hook_main` has already returned 0. Pointing
    stdout at /dev/null is what empties that retry, and nothing
    in-process can observe it because the bug lives in interpreter
    shutdown, past the last line any in-process test runs.

    Hence a subprocess, and hence a closed pipe rather than an encoding
    error: the UnicodeEncodeError path fails before the bytes are
    buffered, so it exits 0 with or without the salvage and would pin
    nothing.

    A hook whose command exits 120 shows the user an error banner at
    session open, which is the one outcome this command must never
    produce; `|| true` in hooks.json is a second net, not a reason to
    leave the first one untested.
    """
    from bettermemory.origin import capture

    from .test_verdict import _repo

    repo = _repo(tmp_path)
    store = Store(tmp_path)
    written = store.write(
        content="alpha one durable fact", scopes=["tools"], origin=capture(repo)
    )
    store.mark_verified(written.id)
    store.close()

    env = shielded_child_env()
    env["BETTERMEMORY_DIR"] = str(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "bettermemory", "session-start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"session_id": "transcript-hup", "cwd": str(repo)}))
    proc.stdin.close()
    # Hang up on the child immediately. It has a config load, a store
    # open and a scan to do first, so the close wins by several orders
    # of magnitude — but the stderr assertion below is what proves the
    # failure path was actually taken rather than assumed.
    assert proc.stdout is not None
    proc.stdout.close()
    assert proc.stderr is not None
    err = proc.stderr.read()
    proc.stderr.close()
    returncode = proc.wait()

    assert returncode == 0, (
        f"session-start exited {returncode} after its reader hung up — "
        f"120 means the shutdown flush retried the buffered block, i.e. "
        f"the /dev/null redirect is gone. stderr: {err!r}"
    )
    assert "could not write the context block" in err, (
        "the write unexpectedly succeeded, so this run proved nothing "
        f"about the salvage; stderr was {err!r}"
    )
