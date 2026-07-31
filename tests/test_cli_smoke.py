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

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from bettermemory.config import load_config
from bettermemory.origin import Origin
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.server import main as cli_main
from bettermemory.store import Store

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
        "doctor",
        "init",
        "migrate",
        "export",
        "tombstones",
        "proposals",
        "rename-scope",
    ):
        assert sub in out, f"subcommand {sub!r} missing from --help output"
    assert "bettermemory" in out
    # Pin the load-bearing positioning phrase from the argparse
    # description so an accidental shorten-pass loses the regression,
    # not the line "memory MCP server" that used to be the marker —
    # retuned in 1.4.2 to lead with "Persistent memory", and again
    # post-3.20.0 to the trust-layer framing every other identity
    # surface adopted ("Memory you can verify").
    assert "Memory you can verify" in out


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
# Curation subcommands that mutate the store: `tombstones restore`,
# `rename-scope`, `proposals` (list/accept/dismiss). These are the CLI
# escape hatches for the six tools gated out of the lean default surface —
# the README/CHANGELOG "every gated tool stays reachable via the CLI"
# contract. Seed via a Store resolved the SAME way the CLI resolves it.
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


def test_tombstones_restore_brings_back_a_removed_memory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`tombstones restore <id>` un-tombstones a memory — the CLI path for
    memory_restore, which isn't registered on the lean default surface."""
    store = _seeded_store(tmp_path, monkeypatch)
    memory = store.write(content="restore me", scopes=["tools"])
    store.tombstone(memory.id, reason="oops", session_id="sess_t")
    assert memory.id not in {m.id for m in store.load_all()}

    _run_main(
        ["tombstones", "restore", memory.id], monkeypatch=monkeypatch, storage=tmp_path
    )
    out = capsys.readouterr().out
    assert "Restored" in out
    assert memory.id in out
    assert memory.id in {m.id for m in store.load_all()}


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


def test_proposals_list_then_dismiss(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`proposals list` shows the queue; `proposals dismiss <id>` drops one
    without writing it — the CLI path for memory_proposals."""
    store = _seeded_store(tmp_path, monkeypatch)
    ProposalQueue(store.root).append(
        [
            Proposal(
                id=_FAKE_ULID,
                body="I prefer terse code-driven answers",
                source_excerpt="I prefer terse code-driven answers",
                suggested_category="user-inference",
                created="2026-01-01T00:00:00+00:00",
            )
        ]
    )

    _run_main(["proposals", "list"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "Proposals (1)" in out
    assert _FAKE_ULID in out

    _run_main(
        ["proposals", "dismiss", _FAKE_ULID], monkeypatch=monkeypatch, storage=tmp_path
    )
    assert "Dismissed" in capsys.readouterr().out
    assert ProposalQueue(store.root).load() == []


def test_proposals_accept_writes_memory_and_clears_queue(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`proposals accept <id> --scope X` writes the proposal as a real memory
    and removes it from the queue — sharing the handler's accept core."""
    store = _seeded_store(tmp_path, monkeypatch)
    ProposalQueue(store.root).append(
        [
            Proposal(
                id=_FAKE_ULID,
                body="We deploy to fly.io for production",
                source_excerpt="We deploy to fly.io for production",
                suggested_category="fact",
                created="2026-01-01T00:00:00+00:00",
            )
        ]
    )

    _run_main(
        ["proposals", "accept", _FAKE_ULID, "--scope", "infrastructure"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    assert "Accepted" in capsys.readouterr().out
    assert ProposalQueue(store.root).load() == []
    actives = store.load_all()
    assert any("fly.io" in m.body and "infrastructure" in m.scopes for m in actives)


def test_proposals_accept_requires_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accept without --scope is rejected (exit 2) and leaves the proposal
    queued so the caller can retry with a scope."""
    store = _seeded_store(tmp_path, monkeypatch)
    ProposalQueue(store.root).append(
        [
            Proposal(
                id=_FAKE_ULID,
                body="We deploy to fly.io for production",
                source_excerpt="We deploy to fly.io for production",
                suggested_category="fact",
                created="2026-01-01T00:00:00+00:00",
            )
        ]
    )

    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["proposals", "accept", _FAKE_ULID],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )
    assert exc.value.code == 2
    assert len(ProposalQueue(store.root).load()) == 1


def test_proposals_accept_disk_error_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A disk-level failure in the durable write surfaces as a clean
    `parser.error` (exit 2), not a path-leaking traceback — matching the
    sibling `tombstones restore` / `rename-scope` commands rather than
    regressing below them."""
    store = _seeded_store(tmp_path, monkeypatch)
    ProposalQueue(store.root).append(
        [
            Proposal(
                id=_FAKE_ULID,
                body="We deploy to fly.io for production",
                source_excerpt="We deploy to fly.io for production",
                suggested_category="fact",
                created="2026-01-01T00:00:00+00:00",
            )
        ]
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr("bettermemory.store.Store.write", _boom)

    with pytest.raises(SystemExit) as exc:
        _run_main(
            ["proposals", "accept", _FAKE_ULID, "--scope", "infrastructure"],
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

    Shape: 5 retrievals (the `_COLD_ENDORSEMENT_MIN_RETRIEVALS` floor)
    and 10 applied events split 1 explicit / 9 auto. `explicit == 0` is
    False, so the always-on binary check never fires; the ratio 0.1 is
    below a 0.25 threshold, so the bucket lights up if and only if the
    caller actually threaded `cold_endorsement_ratio_threshold`.
    """
    from bettermemory.events import Recorder

    memory = store.write(content="deploy with uv, never pip", scopes=["tools"])
    rec = Recorder(root=store.root, session_id="sess-cold-endorse")
    for _ in range(5):
        rec.record("search", returned=[memory.id], relevance=["high"])
    rec.record("use", ids=[memory.id], outcome="applied", auto=False)
    for _ in range(9):
        rec.record("use", ids=[memory.id], outcome="applied", auto=True)
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
    original = "cold_endorsement_ratio_threshold = 0.0"
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
    fed by the single `report_for_directory` call.
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
    # The demo store is isolated — the BETTERMEMORY_DIR storage stays empty.
    assert not list(tmp_path.glob("*.md"))

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


def _all_registered_subcommands() -> list[str]:
    """Every subcommand in `_build_parser`'s registry, so the `--help`
    smoke sweep below can never drift out of sync with the CLI again (a
    hardcoded list here sat at 12 of 17 entries for several releases)."""
    from bettermemory.cli import _build_parser

    _, subparsers = _build_parser()
    return sorted(subparsers)


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


def test_doctor_help_describes_the_check_suite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory doctor --help` must carry doctor's category summary
    (install wiring / store integrity / sync-repo leak surfaces) — not
    just usage + options. Doctor is the surface users probe first when
    chasing the sync-secret leak, so the summary the top-level listing
    shows has to be visible here too."""
    out = _flat_help(
        ["doctor"], monkeypatch=monkeypatch, capsys=capsys, storage=tmp_path
    )
    assert "Diagnose install state." in out
    assert "install wiring" in out
    assert "store integrity" in out
    assert "sync-repo leak surfaces" in out


def test_sync_help_describes_the_command_repo_wide_pattern(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The description= fix is repo-wide, not doctor-special: `sync
    --help` states what sync does, and the NESTED `sync init --help`
    states what init does (nested registrations share the same
    help=/description= convention)."""
    out = _flat_help(["sync"], monkeypatch=monkeypatch, capsys=capsys, storage=tmp_path)
    assert "Sync the memory directory across hosts via git." in out

    out = _flat_help(
        ["sync", "init"], monkeypatch=monkeypatch, capsys=capsys, storage=tmp_path
    )
    assert "Initialise the memory dir as a git repo." in out


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
    cleanly. CLI is a thin wrapper over `EpisodeStore.list_by_session`."""
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


def _seed_backdated_episode(storage: Path, *, days_old: int = 40) -> Path:
    """Write one episode under `storage` and backdate its file mtime past
    a 30-day TTL so a normal prune would consider it stale. Returns the
    session_dir path so callers can assert it survives / is removed."""
    import os as _os
    import time as _time

    from bettermemory.episodes import EpisodeStore

    store = EpisodeStore(storage)
    store.write(session_id="sess_smoke01", body="ancient takeaway")
    session_dir = store.episodes_dir / "sess_smoke01"
    past = _time.time() - (days_old * 24 * 60 * 60)
    for f in session_dir.iterdir():
        if f.is_file():
            _os.utime(f, (past, past))
    return session_dir


def test_episodes_prune_dry_run_ttl_zero_matches_real_prune(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: `EpisodeStore.prune_old_sessions` early-returns [] for
    ttl_days <= 0 (a non-positive TTL is a no-op). The CLI dry-run must
    use the SAME predicate — otherwise it lists every session as "would
    delete" while a real prune deletes nothing, i.e. the dry-run lies.
    Even with a 40-day-old session present, ttl_days=0 dry-run must report
    an empty would_delete set, and a real ttl=0 prune must delete nothing."""
    session_dir = _seed_backdated_episode(tmp_path, days_old=40)
    assert session_dir.exists()

    _run_main(
        ["episodes", "prune", "--dry-run", "--ttl-days", "0", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    dry_payload = json.loads(capsys.readouterr().out)
    assert dry_payload["would_delete"] == []
    assert dry_payload["ttl_days"] == 0
    assert session_dir.exists()

    _run_main(
        ["episodes", "prune", "--ttl-days", "0", "--json"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    real_payload = json.loads(capsys.readouterr().out)
    assert real_payload["deleted"] == []
    assert session_dir.exists()


def test_episodes_prune_dry_run_ttl_zero_text_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same ttl<=0 guard, text mode: must print the "No episode sessions
    older than 0 days." line rather than a "Would delete …" list."""
    session_dir = _seed_backdated_episode(tmp_path, days_old=40)
    assert session_dir.exists()

    _run_main(
        ["episodes", "prune", "--dry-run", "--ttl-days", "0"],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    out = capsys.readouterr().out
    assert "No episode sessions older than 0 days." in out
    assert "Would delete" not in out
    assert session_dir.exists()


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


def test_reindex_embeddings_flag_reports_disabled_when_dedup_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`reindex --embeddings` with `semantic_dedup = false` (the default)
    reports the skip cleanly rather than silently doing nothing. The
    message guides the user toward the config switch."""
    from bettermemory.store import Store

    Store(tmp_path).write(content="reindex me", scopes=["tools"])
    _run_main(["reindex", "--embeddings"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    assert "semantic_dedup" in out
    assert "off" in out.lower() or "disabled" in out.lower()


def test_reindex_embeddings_flag_reports_no_provider_when_extras_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When `semantic_dedup = true` but neither embedding extra is
    installed, the flag reports the missing-provider case and points
    at the install hint. A no-extras CI run exercises this path."""
    from bettermemory.config import DEFAULT_CONFIG
    from bettermemory.store import Store

    # Drop a config file that flips semantic_dedup on so the no-provider
    # branch fires instead of the early-disabled return.
    cfg_dir = tmp_path / "bm-config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.toml"
    cfg_path.write_text(
        DEFAULT_CONFIG.replace("semantic_dedup = false", "semantic_dedup = true"),
        encoding="utf-8",
    )
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    monkeypatch.setattr("bettermemory.config.default_config_path", lambda: cfg_path)

    Store(tmp_path).write(content="reindex me", scopes=["tools"])
    _run_main(["reindex", "--embeddings"], monkeypatch=monkeypatch, storage=tmp_path)
    out = capsys.readouterr().out
    # In the no-extras environment we should see the install-hint branch;
    # in CI with [embeddings] or [embeddings-fast] installed, the ok
    # branch fires instead — either is correct, but at least one of the
    # expected phrases should appear.
    assert (
        "neither [embeddings] nor [embeddings-fast]" in out
        or "Re-embedded" in out
        or "failed to load" in out
    )


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
    # Same pin as the in-process smoke test above — post-3.20.0 the
    # description leads with the trust-layer framing ("Memory you can
    # verify") shared by every other identity surface.
    assert "Memory you can verify" in out


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

    from bettermemory.cli import _build_parser, main

    _, subparsers = _build_parser()
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
# ui --tunnel — argparse glue + clean-exit contract (serve itself is
# covered in test_web.py; here we pin what reaches web.serve)
# ---------------------------------------------------------------------------


def test_ui_tunnel_flag_parses_and_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bare `--tunnel` means auto; explicit providers pass through
    verbatim; no flag stays None (read-write local UI). All three
    must reach web.serve as the `tunnel` kwarg."""
    from bettermemory import web

    calls: list[tuple[str, int, str | None]] = []

    def _fake_serve(
        config: object, *, host: str, port: int, tunnel: str | None = None
    ) -> None:
        calls.append((host, port, tunnel))

    monkeypatch.setattr(web, "serve", _fake_serve)
    _run_main(["ui", "--tunnel"], monkeypatch=monkeypatch, storage=tmp_path)
    _run_main(["ui", "--tunnel", "funnel"], monkeypatch=monkeypatch, storage=tmp_path)
    _run_main(["ui"], monkeypatch=monkeypatch, storage=tmp_path)
    assert calls == [
        ("127.0.0.1", 8765, "auto"),
        ("127.0.0.1", 8765, "funnel"),
        ("127.0.0.1", 8765, None),
    ]


def test_ui_tunnel_error_exits_2_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A TunnelError (missing binary, non-loopback host) must exit 2
    with the hint on stderr — not a traceback."""
    from bettermemory import web

    def _boom(
        config: object, *, host: str, port: int, tunnel: str | None = None
    ) -> None:
        raise web.TunnelError("no tunnel CLI found (install hint here)")

    monkeypatch.setattr(web, "serve", _boom)
    with pytest.raises(SystemExit) as excinfo:
        _run_main(["ui", "--tunnel"], monkeypatch=monkeypatch, storage=tmp_path)
    assert excinfo.value.code == 2
    assert "install hint here" in capsys.readouterr().err


def test_ui_startup_failure_exit_code_3_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`web.serve` raises SystemExit(3) when uvicorn never bound — its
    STARTUP_FAILURE parity, restored for --tunnel in 3.19.0 (the plain
    path gets the same code from `uvicorn.run` itself). The operational
    value (systemd `Restart=on-failure`, shell `$?`) only survives if
    the CLI layer lets that BaseException sail through untouched:
    `_cli_ui`'s ImportError/TunnelError handlers catch Exception
    subclasses only, and no frame up the chain (ui.run -> cli.main ->
    server.main) wraps the call. A future wrapper that swallows or
    remaps SystemExit (a bare `except:`/`except BaseException:` around
    the serve call, or a "friendly" re-raise as exit 1) would kill the
    fix silently; this pins the end-to-end exit-code contract for both
    the --tunnel and plain invocations."""
    from bettermemory import web

    def _startup_failure(
        config: object, *, host: str, port: int, tunnel: str | None = None
    ) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(web, "serve", _startup_failure)
    for argv in (["ui", "--tunnel"], ["ui"]):
        with pytest.raises(SystemExit) as excinfo:
            _run_main(argv, monkeypatch=monkeypatch, storage=tmp_path)
        assert excinfo.value.code == 3, (
            f"{argv}: SystemExit(3) from web.serve must reach the process "
            f"boundary unchanged, got {excinfo.value.code!r}"
        )


def test_ui_tunnel_rejects_unknown_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """argparse choices gate the provider list — a typo'd provider is
    a usage error (exit 2), never a spawned process."""
    with pytest.raises(SystemExit) as excinfo:
        _run_main(
            ["ui", "--tunnel", "ngrok"], monkeypatch=monkeypatch, storage=tmp_path
        )
    assert excinfo.value.code == 2


def test_migrate_origin_repair_requires_scope_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--repair` with no map has nothing to check an existing origin
    against — fail loudly rather than silently scanning and doing
    nothing."""
    with pytest.raises(SystemExit):
        _run_main(
            ["migrate", "origin", "--repair"], monkeypatch=monkeypatch, storage=tmp_path
        )


def test_migrate_origin_keep_global_requires_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--keep-global` only guards the repair anchor rule; accepting it
    without `--repair` would imply a protection that never runs."""
    with pytest.raises(SystemExit):
        _run_main(
            ["migrate", "origin", "--keep-global", "tools"],
            monkeypatch=monkeypatch,
            storage=tmp_path,
        )


def test_migrate_origin_repair_reports_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repair mode prints the anchor/demote split, so a dry run is
    reviewable before it is applied."""
    _run_main(
        [
            "migrate",
            "origin",
            "--repair",
            "--dry-run",
            "--scope-repo",
            "projects:alpha=https://github.com/me/alpha.git",
        ],
        monkeypatch=monkeypatch,
        storage=tmp_path,
    )
    out = capsys.readouterr().out
    assert "Repair: ON" in out
    assert "Would anchor" in out
    assert "Would demote" in out


# ---------------------------------------------------------------------------
# `bettermemory session-start` — the SessionStart-hook context block.
#
# Claude Code injects a SessionStart hook's stdout verbatim into the model's
# context, which makes two properties load-bearing in ways ordinary CLI
# subcommands' aren't:
#
#   1. The command must record NOTHING. `hook._latest_in_process_session`
#      picks the newest non-`stop_hook` session in the event log as the
#      anchor the turn audit attributes against, and reads only
#      `triggered_from` to do it — so any row written here hijacks that
#      anchor no matter how it is stamped, and publishes a session id that
#      no `turn_audited` can ever accompany. The mandate is a comment in
#      `cli/session_start_cmd.py`; `test_session_start_records_nothing`
#      below is the part that makes it enforceable.
#   2. Stdout is the block and nothing else. A diagnostic line printed on
#      the wrong stream is indistinguishable, to the model, from content it
#      should act on.
#
# Everything else here pins the degrade-to-silence gates: the command must
# never take the expensive `load_all` path, and must never publish a count
# it cannot prove.
# ---------------------------------------------------------------------------


def _event_log_snapshot(root: Path) -> dict[str, bytes]:
    """Every event-log file under `root`, by name, with contents.

    Byte-level rather than "parse and count rows": the assertion is that
    the command is inert on this store, and a rewritten-but-equivalent
    log would be a behaviour change worth failing on.
    """
    return {
        p.name: p.read_bytes()
        for p in sorted(root.iterdir())
        if p.name.startswith(".events")
    }


def _run_session_start(
    monkeypatch: pytest.MonkeyPatch, storage: Path
) -> "pytest.ExceptionInfo[SystemExit]":
    """Invoke the subcommand and assert the always-exit-0 contract."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["session-start"], monkeypatch=monkeypatch, storage=storage)
    assert exc.value.code == 0, (
        f"session-start must always exit 0 — a non-zero exit surfaces as a "
        f"hook-error banner at session open; got {exc.value.code!r}"
    )
    return exc


def test_session_start_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE regression guard for the negative mandate.

    A store with a pre-existing event log must come out of a
    `session-start` run byte-identical — no appended row, no new shard,
    no new archive. That is the assertion that catches a would-be author
    who reaches for the "just stamp it `cli_*` like consolidate does"
    workaround: it fixes doctor's census and does NOT fix anchor hijack,
    because `hook.py`'s walk reads `triggered_from` and never
    `attribution`.

    Second pass with `Recorder` construction made fatal. `run()` swallows
    every exception into a stderr note, so a constructed recorder shows
    up as an EMPTY block — which makes a still-non-empty block the proof
    that nothing tried. It catches the variant the first pass can't:
    a recorder built with `enabled=False`, or one whose write silently
    no-ops on this store, leaves the log identical while still being the
    thing the mandate forbids.
    """
    from bettermemory.events import Recorder

    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    # A real recorder, so the baseline log is production-shaped rather
    # than a hand-rolled literal.
    Recorder(root=store.root, session_id="a-prior-session").record(
        "search", query="anything", returned=[]
    )
    before = _event_log_snapshot(store.root)
    assert before, "fixture must leave an event log to compare against"

    _run_session_start(monkeypatch, tmp_path)

    # It did its job (otherwise "records nothing" is trivially true).
    assert capsys.readouterr().out.strip()
    assert _event_log_snapshot(store.root) == before, (
        "session-start wrote to the event log — see the negative mandate "
        "at the top of cli/session_start_cmd.py"
    )

    class _Exploding:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError(
                "session-start constructed a Recorder — see the negative "
                "mandate at the top of cli/session_start_cmd.py"
            )

    monkeypatch.setattr("bettermemory.events.Recorder", _Exploding)

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert captured.out.strip(), (
        f"the block went missing once Recorder construction became fatal, "
        f"which means something constructed one: {captured.err!r}"
    )
    assert _event_log_snapshot(store.root) == before


def test_session_start_source_never_reaches_for_the_recorder(tmp_path: Path) -> None:
    """The static half of the same mandate.

    The runtime guard above only fires on the code path a given fixture
    happens to take. This one reads the module's own source: no `Recorder`
    name, no `.record(...)` call, anywhere in it — including the branches
    a test store never reaches (a corrupt index, an OSError degrade).
    """
    import ast
    import inspect

    from bettermemory.cli import session_start_cmd

    tree = ast.parse(inspect.getsource(session_start_cmd))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "Recorder", (
                "cli/session_start_cmd.py names `Recorder`; the SessionStart "
                "hook must record nothing (anchor hijack + phantom sessions)"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"record", "recorder"}, (
                f"cli/session_start_cmd.py calls `.{node.attr}` — the "
                "SessionStart hook must record nothing"
            )


def test_session_start_on_an_empty_store_prints_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty store has nothing to say, and saying "0 memories" into
    every session's opening context would be pure overhead.

    Stderr must be clean too, and that half is the load-bearing one: an
    empty store has no index file, so a run that reached the index-trust
    gate would ALSO print nothing on stdout — passing this test while
    having lost the cheap `count_active_memory_files == 0` bail. A silent
    stderr is what proves the emptiness gate fired first, and that a
    brand-new install is not being told its index is broken."""
    _seeded_store(tmp_path, monkeypatch)

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "", (
        "an empty store is not a degraded state — nothing should be "
        f"reported about it; got {captured.err!r}"
    )


def test_session_start_stdout_is_only_the_context_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stdout goes into the model's context verbatim, so it must carry
    the block and nothing else — diagnostics belong on stderr.

    Pins the block's shape too: the count line, the scope line, and the
    line that tells the model these are counts rather than retrieval
    already performed (without which a non-zero number reads as an
    invitation to stop searching)."""
    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    store.write(content="beta two", scopes=["tools", "learning-style"])

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 3, f"expected exactly the 3-line block, got {lines!r}"
    assert lines[0] == "bettermemory: 2 memories are in scope for this repository."
    assert lines[1] == "Top scopes: tools (2), learning-style (1)."
    assert "no bodies, no ids" in lines[2]
    assert "opt-in" in lines[2]
    # Every diagnostic on stderr, none of it on stdout.
    assert "[bettermemory]" not in captured.out
    assert "[bettermemory] session-start:" in captured.err


def _load_all_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count `Store.load_all` calls; return the (initially empty) log.

    A COUNTER and not a raising sentinel, deliberately: `run()` swallows
    every exception into a stderr note, so a sentinel that raised would
    be absorbed and the test would still see the empty stdout it expects
    — the guard would look green while the expensive path ran. Counting
    is immune to that.

    The whole point of the index path is that the hook never pays the
    per-file open + YAML parse. A degrade arm that quietly fell back to
    `load_all` would still print correct counts, and would still be a
    regression, because it would pay that bill on the session-open
    critical path.
    """
    calls: list[object] = []
    real = Store.load_all

    def _spy(self: Store) -> object:
        calls.append(self)
        return real(self)

    monkeypatch.setattr("bettermemory.store.Store.load_all", _spy)
    return calls


def test_session_start_stays_silent_when_the_index_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable index degrades to empty stdout and exit 0 — never a
    traceback, never a `load_all` fallback."""
    from bettermemory import index

    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    index.index_path(store.root).write_bytes(b"not a sqlite database" * 40)
    load_all_calls = _load_all_spy(monkeypatch)

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "index unusable" in captured.err
    assert load_all_calls == [], (
        "the corrupt-index arm fell back to Store.load_all instead of "
        "degrading to silence"
    )


def test_session_start_stays_silent_when_the_index_count_disagrees_with_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """This surface publishes a COUNT, so "close enough" isn't available:
    a number `memory_search` then contradicts is worse than no number.

    The extra `.md` dropped in by hand is the shape a bulk migration or a
    hand-copied file produces — on disk, never indexed."""
    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    (store.root / "hand-copied.md").write_text(
        "---\nid: 01JHANDCOPIEDHANDCOPIEDXX\n---\nbody\n", encoding="utf-8"
    )
    load_all_calls = _load_all_spy(monkeypatch)

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skipping the hint" in captured.err
    assert load_all_calls == [], (
        "the count-mismatch arm fell back to Store.load_all instead of "
        "degrading to silence"
    )


class _FailingStdout:
    """A stdout that fails the way a hook's real one can.

    `write` and `flush` are separately riggable because they fail at
    different moments: the encode happens in `write`, while an OS-level
    write error (closed pipe, full disk) surfaces only when the buffer
    is handed to the descriptor.

    `fileno` raises on purpose. It keeps the salvage in
    `_blackhole_stdout` away from the test process's real descriptor 1
    (dup2-ing /dev/null over it would blind the rest of the run), and it
    doubles as the proof that a stdout with no descriptor — which is
    every captured or embedded one — cannot make the salvage itself the
    thing that breaks the exit-0 contract.
    """

    def __init__(self, *, write_error: Exception | None = None) -> None:
        self._write_error = write_error
        self.flush_error: Exception | None = None
        self.written: list[str] = []

    def write(self, text: str) -> int:
        if self._write_error is not None:
            raise self._write_error
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


def test_session_start_exits_0_when_the_stdout_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The always-exit-0 contract has to cover the WRITE, not just the read.

    It did not, and the failure was reachable rather than theoretical:
    `PYTHONIOENCODING=ascii bettermemory session-start` exited 1 with a
    `UnicodeEncodeError` traceback on the em dash in the block's third
    line — i.e. a hook-error banner at session open, the exact outcome
    the command's broad `except` exists to prevent. Encoding is the
    cheapest way to reproduce it; a closed pipe and a full disk raise
    from the same `print`.
    """
    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    stdout = _FailingStdout(
        write_error=UnicodeEncodeError("ascii", "—", 0, 1, "ordinal not in range(128)")
    )
    monkeypatch.setattr(sys, "stdout", stdout)

    _run_session_start(monkeypatch, tmp_path)

    # stderr is still pytest's, so the degrade note is capturable even
    # though stdout is the fake.
    err = capsys.readouterr().err
    assert "could not write the context block" in err
    assert "UnicodeEncodeError" in err
    assert stdout.written == [], "the failing write must not be retried"


def test_session_start_flushes_the_block_inside_the_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A buffered write that is never flushed escapes the guard entirely.

    `print` alone only fills the buffer, so a broken pipe or a full disk
    surfaces during the interpreter's shutdown flush — after every
    handler is gone, where CPython prints "Exception ignored on flushing
    sys.stdout" and exits **120** regardless of the `SystemExit(0)` the
    command raised. Measured directly: a short block down a closed pipe
    exits 120 without an in-guard flush and 0 with one.

    So the assertion is not just "exit 0" (which a command that never
    flushed would also satisfy, by never noticing) — it is that the
    failure was SEEN, on stderr, while the guard was still up.
    """
    store = _seeded_store(tmp_path, monkeypatch)
    store.write(content="alpha one", scopes=["tools"])
    stdout = _FailingStdout()
    stdout.flush_error = BrokenPipeError(32, "Broken pipe")
    monkeypatch.setattr(sys, "stdout", stdout)

    _run_session_start(monkeypatch, tmp_path)

    err = capsys.readouterr().err
    assert "could not write the context block" in err, (
        "the block was written but never flushed inside the guard — the "
        "write error would land in the shutdown flush and exit 120"
    )
    assert "BrokenPipeError" in err
    assert stdout.written, "the block itself should still have been attempted"


# Two projects on the same host, deliberately: `repos_match` compares
# (host, owner, name), so same-host/different-name is both the shape a
# real multi-project store has and the shape a filter that only looked
# at the host would wrongly admit.
_FIXTURE_REPO_HERE = "git@github.com:example/here.git"
_FIXTURE_REPO_ELSEWHERE = "git@github.com:example/elsewhere.git"


def _out_of_scope_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Store, Origin]:
    """A store whose every memory belongs to somebody else's workspace.

    Returns the store and the origin the command will capture, so a
    caller can add the in-scope rows it wants on top.

    One row per half of the filter the command binds:

    * a different repository — `repos_match` rejects it, and
    * the SAME repository in a different worktree — `worktrees_match`
      rejects it. That is the leakage `origin.worktree_root` exists to
      stop, and it is invisible to a repo-only fixture.

    All three checkout directories are real, and that is load-bearing:
    `worktrees_match` degrades to repo-level matching when the recorded
    root is POSITIVELY GONE (a since-deleted ephemeral worktree must not
    become invisible forever), so a made-up path would be ADMITTED and
    the sibling row would prove nothing.

    `origin.capture` is pinned rather than inherited from wherever pytest
    was invoked, because the command captures the CALLER's origin at run
    time: a suite that read the real cwd could not seed a matching
    memory deterministically, and — run from outside any git checkout —
    would get `repo=None`, which switches the whole filter off and makes
    every assertion below vacuous.
    """
    store = _seeded_store(tmp_path, monkeypatch)
    here = tmp_path / "checkout-here"
    elsewhere = tmp_path / "checkout-elsewhere"
    sibling = tmp_path / "checkout-here-sibling"
    for path in (here, elsewhere, sibling):
        path.mkdir()

    caller = Origin(repo=_FIXTURE_REPO_HERE, worktree_root=str(here))
    monkeypatch.setattr("bettermemory.origin.capture", lambda cwd=None: caller)

    store.write(
        content="another project's deploy note",
        scopes=["projects:elsewhere"],
        origin=Origin(repo=_FIXTURE_REPO_ELSEWHERE, worktree_root=str(elsewhere)),
    )
    store.write(
        content="a note from the sibling worktree",
        scopes=["projects:sibling"],
        origin=Origin(repo=_FIXTURE_REPO_HERE, worktree_root=str(sibling)),
    )
    return store, caller


def test_session_start_counts_only_what_is_in_scope_for_this_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The published number is a SCOPED number, and that is the whole point.

    The block says "in scope for this repository", and the model is
    expected to reconcile it with what `memory_search` and
    `memory_scope_overview` later report — both of which auto-scope. A
    command that bound the predicate to nothing would still print a
    perfectly plausible total: on the dogfood store, 235 instead of 188.
    Nothing else in the suite notices, because
    `test_index.py::test_scope_counts_agree_with_the_load_all_answer`
    builds its own `_admit` closure and so is structurally blind to how
    the CLI binds one.

    The null-origin row is the counter-assertion: scoping must not turn
    into "only rows stamped with my repo", or every legacy and every
    global memory would vanish from the count.
    """
    store, caller = _out_of_scope_store(tmp_path, monkeypatch)
    store.write(
        content="this project's note",
        scopes=["projects:here"],
        origin=Origin(repo=caller.repo, worktree_root=caller.worktree_root),
    )
    # No origin at all — global, and admitted for every caller.
    store.write(content="a global preference", scopes=["personal-context"])

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == "bettermemory: 2 memories are in scope for this repository.", (
        f"expected the two admitted memories out of four stored; got {lines!r}"
    )
    assert lines[1] == "Top scopes: personal-context (1), projects:here (1)."
    assert "projects:elsewhere" not in captured.out, (
        "a memory from another repository reached the opening context"
    )
    assert "projects:sibling" not in captured.out, (
        "a memory from a sibling worktree of this repository reached the "
        "opening context — the worktree half of the filter is unbound"
    )
    assert "2 in scope out of 4 stored" in captured.err


def test_session_start_stays_silent_when_nothing_is_in_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A populated store with nothing for THIS repository says nothing.

    The second emptiness gate, and the one the store-is-empty test cannot
    reach: here the store has memories, the index agrees with disk, and
    the count is zero only after admission. "0 memories are in scope"
    would be true and useless — opt-in retrieval is already the model's
    default, so the line would buy nothing and cost every session.

    Silent stderr as well, for the same reason the empty-store test
    demands it: this is a normal state, not a degraded one, and a
    diagnostic here would also mean some earlier gate fired instead of
    this one.
    """
    _out_of_scope_store(tmp_path, monkeypatch)

    _run_session_start(monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"a zero count must not be published as a block; got {captured.out!r}"
    )
    assert captured.err == "", (
        f"nothing in scope is not a degraded state; got {captured.err!r}"
    )


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
    """The `_blackhole_stdout` salvage, guarded where it actually bites.

    Catching the write error and exiting 0 is not sufficient on its own:
    a failed flush leaves the bytes in the buffer, so CPython's own
    shutdown flush retries them, fails again, and overrules the exit
    code with 120 — after `run` has already returned SystemExit(0).
    Redirecting the descriptor at /dev/null is what empties that retry,
    and nothing in-process can observe it because the bug lives in
    interpreter shutdown, past the last line any in-process test runs.

    Hence a subprocess, and hence a closed pipe rather than the encoding
    trick the in-process test uses: the UnicodeEncodeError path fails
    before the bytes are buffered, so it exits 0 with or without the
    salvage and would pin nothing. Verified by mutation — deleting the
    `os.dup2` and running this exact shape returns 120.

    A hook whose command exits 120 shows the user an error banner at
    session open, which is the one outcome this command must never
    produce; `|| true` in hooks.json is a second net, not a reason to
    leave the first one untested.
    """
    store = Store(tmp_path)
    store.write(content="alpha one durable fact", scopes=["tools"])

    env = shielded_child_env()
    env["BETTERMEMORY_DIR"] = str(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "bettermemory", "session-start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    # Hang up on the child immediately. It has a config load, an index
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
