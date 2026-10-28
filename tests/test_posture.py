"""`bettermemory security audit`: the read-only posture audit (`posture`).

Each test lays out a scratch store, keys and state directory under its own
temporary directory, with a fake home and a redirected managed-settings
path, so no test reads the developer's ``~/.claude/settings.json`` or the
system's managed file. What the tests pin:

* modes too open are graded by what the extra bit allows (write is high
  everywhere, read is high on a secret and a warning elsewhere), the
  shipped modes pass, and an owner other than this account is high;
* a missing or foreign key, keys inside the store directory, a store
  tracked by git and a store with its key in a synced folder are high; a
  store chosen by ./.claude-memory, an unignored working tree and a synced
  store alone are warnings;
* the daemon's recorded address, Claude Code's sandbox settings against
  `bettermemory security sandbox`, and the config's facts;
* the audit writes nothing: the store, the keys, the state directory and
  the home directory are byte-for-byte and mode-for-mode the same after
  it, and reading the store's meta under a live writer creates no file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import _daemon_client, posture, sandbox_settings
from bettermemory import config as _config
from bettermemory.config import Config
from bettermemory.log import KeyRing
from bettermemory.posture import Audit, Finding, audit, mode_finding, synced_root
from bettermemory.store import STORE_FILENAME, Store

from .conftest import set_git_discovery_ceiling

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX modes and owners"
)


@dataclass
class Layout:
    store_dir: Path
    store_file: Path
    keys_dir: Path
    state_dir: Path
    store_id: str
    home: Path
    managed: Path

    @property
    def keyring(self) -> KeyRing:
        return KeyRing(self.keys_dir, self.store_id)

    @property
    def state_file(self) -> Path:
        return _daemon_client.state_file_for(self.state_dir, self.store_file)


@pytest.fixture
def layout(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    """A fresh store named by BETTERMEMORY_DIR, its keys where the suite's
    autouse fixture keeps them, a state directory of its own, a fake home
    and the managed-settings path under the test's directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(sandbox_settings.CLAUDE_CONFIG_DIR_ENV, raising=False)
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(
        sandbox_settings,
        "claude_managed_settings_path",
        lambda platform=None: managed,
    )
    state_dir = tmp_path / "state"
    monkeypatch.setenv(_daemon_client.STATE_DIR_ENV, str(state_dir))
    store_dir = (tmp_path / "store").resolve()
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(store_dir))
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    with Store.create(store_dir) as store:
        store_id = store.store_id
    return Layout(
        store_dir=store_dir,
        store_file=store_dir / STORE_FILENAME,
        keys_dir=keys_dir.resolve(),
        state_dir=state_dir.resolve(),
        store_id=store_id,
        home=home,
        managed=managed,
    )


def _by_id(report: Audit, finding_id: str) -> Finding:
    matches = [f for f in report.findings if f.id == finding_id]
    assert matches, f"no {finding_id} in {[f.id for f in report.findings]}"
    return matches[0]


def _ids(report: Audit) -> set[str]:
    return {f.id for f in report.findings}


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    """Every path under `roots` with its mode, size and content hash."""
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


def _write_state(layout: Layout, **extra: Any) -> Path:
    state = {
        "pid": os.getpid(),
        "port": 7397,
        "token": "t" * 64,
        "version": "0",
        "store": str(layout.store_file),
        "started": "2026-01-01T00:00:00Z",
        **extra,
    }
    return _daemon_client.write_state(layout.state_dir, state)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# A healthy install
# ---------------------------------------------------------------------------


def test_healthy_store_is_clean_and_untouched(layout: Layout) -> None:
    """A store as `Store.create` leaves it audits clean, exit 0, and the
    audit leaves every file as it found it: no chmod, no rekey, no WAL
    files beside the store, no config.toml in the config directory."""
    roots = (layout.store_dir, layout.keys_dir, layout.home, layout.managed.parent)
    before = _snapshot(*roots)
    report = audit()
    assert report.status == "ok", [f.to_json() for f in report.findings]
    assert report.exit_code == 0
    assert report.store == layout.store_file
    assert report.store_rule == "BETTERMEMORY_DIR"
    assert _by_id(report, "key.match").status == "ok"
    assert _by_id(report, "key.placement").status == "ok"
    assert _by_id(report, "store.git").status == "ok"
    assert _by_id(report, "store.synced").status == "ok"
    assert _by_id(report, "daemon.bind").status == "info"
    # Native Windows has no Claude Code sandbox, and the audit says so.
    sandbox = "skip" if sys.platform == "win32" else "info"
    assert _by_id(report, "claude.sandbox").status == sandbox
    if sys.platform != "win32":
        for finding_id in (
            "mode.store_dir",
            "mode.store_file",
            "mode.key",
            "mode.head",
        ):
            assert _by_id(report, finding_id).status == "ok"
    assert _snapshot(*roots) == before
    assert not (layout.store_dir / (STORE_FILENAME + "-wal")).exists()


def test_json_shape(layout: Layout) -> None:
    report = audit().to_json()
    assert set(report) == {
        "status",
        "exit_code",
        "store",
        "store_rule",
        "platform",
        "counts",
        "findings",
    }
    assert set(report["counts"]) == set(posture.STATUSES)
    for finding in report["findings"]:
        assert set(finding) == {"id", "status", "summary", "path", "fix", "details"}
        assert finding["status"] in posture.STATUSES
    json.dumps(report)


def test_meta_read_under_a_live_writer_creates_nothing(layout: Layout) -> None:
    """With a connection open, the store has its ``-wal`` and ``-shm``; the
    audit's read sees the WAL's pages through ``mode=ro`` and adds no file."""
    store = Store.open(layout.store_file)
    try:
        store.meta()
        wal = layout.store_dir / (STORE_FILENAME + "-wal")
        assert wal.exists()
        names = sorted(p.name for p in layout.store_dir.iterdir())
        meta = posture.read_meta(layout.store_file)
        assert meta["store_id"] == layout.store_id
        assert sorted(p.name for p in layout.store_dir.iterdir()) == names
    finally:
        store.close()


def test_meta_read_without_wal_creates_nothing(layout: Layout) -> None:
    names = sorted(p.name for p in layout.store_dir.iterdir())
    assert posture.read_meta(layout.store_file)["store_id"] == layout.store_id
    assert sorted(p.name for p in layout.store_dir.iterdir()) == names


def test_meta_read_refuses_a_wal_without_its_shm(layout: Layout) -> None:
    """A ``-wal`` that arrived without its ``-shm`` (a copy that brought one
    and not the other): a read-only open would create the ``-shm``, and an
    immutable one would miss the WAL's pages, so the read is refused and
    the audit says why instead of writing a file."""
    wal = layout.store_dir / (STORE_FILENAME + "-wal")
    wal.write_bytes(b"\0" * 32)
    names = sorted(p.name for p in layout.store_dir.iterdir())
    with pytest.raises(sqlite3.Error, match="without"):
        posture.read_meta(layout.store_file)
    finding = _by_id(audit(), "key.match")
    assert finding.status == "warn"
    assert "-shm" in finding.summary
    assert sorted(p.name for p in layout.store_dir.iterdir()) == names


@posix_only
@pytest.mark.parametrize(
    ("which", "finding_id", "status"),
    [
        ("store", "key.match", "warn"),
        ("keys", "key.match", "high"),
        ("state", "daemon.bind", "warn"),
    ],
)
def test_unsearchable_directory_is_a_finding_not_a_crash(
    layout: Layout, which: str, finding_id: str, status: str
) -> None:
    """A store, keys or state directory this account cannot search, as one
    another account owns would be: `Path.exists` raises EACCES there on
    3.11 to 3.13, which ended the audit in a traceback. Each now reads as
    a finding about the path it could not reach."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root searches a 0o000 directory")
    _write_state(layout)
    directory = {
        "store": layout.store_dir,
        "keys": layout.keys_dir,
        "state": layout.state_dir,
    }[which]
    os.chmod(directory, 0o000)
    try:
        report = audit()
    finally:
        os.chmod(directory, 0o700)
    finding = _by_id(report, finding_id)
    assert finding.status == status, finding.to_json()
    assert "cannot be" in finding.summary
    assert report.exit_code == (1 if status == "high" else 2)


# ---------------------------------------------------------------------------
# Modes and owners
# ---------------------------------------------------------------------------


@posix_only
def test_open_modes_are_graded_and_left_alone(layout: Layout) -> None:
    """Read on a secret is high, write anywhere is high, read on a
    directory or the head checkpoint is a warning. The audit reports and
    does not heal: the modes are the same afterwards."""
    os.chmod(layout.store_dir, 0o755)
    os.chmod(layout.store_file, 0o644)
    os.chmod(layout.keyring.current_path(), 0o640)
    os.chmod(layout.keyring.head_path(), 0o644)
    os.chmod(layout.keys_dir, 0o770)
    report = audit()
    assert _by_id(report, "mode.store_dir").status == "warn"
    assert _by_id(report, "mode.store_file").status == "high"
    assert _by_id(report, "mode.key").status == "high"
    assert _by_id(report, "mode.head").status == "warn"
    assert _by_id(report, "mode.keys_dir").status == "high"
    key = _by_id(report, "mode.key")
    assert key.fix is not None and key.fix.startswith("chmod 600 ")
    assert key.details["mode"] == "0640"
    assert report.status == "high"
    assert report.exit_code == 1
    assert stat.S_IMODE(layout.store_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(layout.store_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(layout.keyring.current_path().stat().st_mode) == 0o640


@posix_only
def test_directory_bits_alone_are_a_warning(layout: Layout) -> None:
    os.chmod(layout.store_dir, 0o750)
    report = audit()
    assert _by_id(report, "mode.store_dir").status == "warn"
    assert report.status == "warn"
    assert report.exit_code == 2


@posix_only
def test_retired_key_modes_are_checked(layout: Layout) -> None:
    retired = layout.keys_dir / f"{layout.store_id}.0123456789abcdef.key"
    retired.write_bytes(b"x" * 32)
    os.chmod(retired, 0o644)
    report = audit()
    finding = _by_id(report, "mode.retired_key")
    assert finding.status == "high"
    assert finding.path == str(retired)
    assert "1 retired key(s)" in _by_id(report, "key.match").summary


@posix_only
def test_state_file_and_log_modes(layout: Layout) -> None:
    state_file = _write_state(layout)
    os.chmod(state_file, 0o644)
    os.chmod(layout.state_dir, 0o755)
    log_file = _daemon_client.log_file_for(layout.state_dir, layout.store_file)
    log_file.write_text("started\n", encoding="utf-8")
    os.chmod(log_file, 0o644)
    report = audit()
    assert _by_id(report, "mode.state_file").status == "high"
    assert _by_id(report, "mode.state_dir").status == "warn"
    assert _by_id(report, "mode.daemon_log").status == "warn"


def test_owner_other_than_this_account_is_high(tmp_path: Path) -> None:
    if not hasattr(os, "geteuid"):
        pytest.skip("no effective uid on this platform")
    target = tmp_path / "key"
    target.write_bytes(b"k")
    os.chmod(target, 0o600)
    finding = mode_finding(
        "mode.key",
        "key file",
        target,
        directory=False,
        secret=True,
        euid=os.geteuid() + 1,
    )
    assert finding is not None
    assert finding.status == "high"
    assert "not this account" in finding.summary
    assert (
        mode_finding(
            "mode.key",
            "key file",
            tmp_path / "absent",
            directory=False,
            secret=True,
            euid=os.geteuid(),
        )
        is None
    )


def test_windows_skips_modes_and_the_sandbox(layout: Layout) -> None:
    report = audit(platform="win32")
    assert _by_id(report, "mode").status == "skip"
    assert not any(f.id.startswith("mode.") for f in report.findings)
    assert _by_id(report, "claude.sandbox").status == "skip"
    assert report.platform == "win32"


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_missing_key_is_high_and_not_replaced(layout: Layout) -> None:
    key_path = layout.keyring.current_path()
    key_path.unlink()
    report = audit()
    finding = _by_id(report, "key.match")
    assert finding.status == "high"
    assert "next owner open" in finding.summary
    assert finding.fix is not None and key_path.name in finding.fix
    assert report.exit_code == 1
    assert not key_path.exists()


def test_foreign_key_is_high(layout: Layout) -> None:
    key_path = layout.keyring.current_path()
    key_path.write_bytes(b"\x01" * 32)
    finding = _by_id(audit(), "key.match")
    assert finding.status == "high"
    assert finding.details["retired_match"] is False
    assert "is not the store's" in finding.summary


@posix_only
def test_unreadable_key_is_high(layout: Layout) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")
    key_path = layout.keyring.current_path()
    os.chmod(key_path, 0o000)
    try:
        finding = _by_id(audit(), "key.match")
    finally:
        os.chmod(key_path, 0o600)
    assert finding.status == "high"
    assert "cannot read it" in finding.summary


def test_keys_inside_the_store_directory_are_high(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = (tmp_path / "bundled").resolve()
    keys = store_dir / "keys"
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(store_dir))
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(keys))
    Store.create(store_dir, keys_dir=keys).close()
    report = audit()
    assert _by_id(report, "key.placement").status == "high"
    assert _by_id(report, "key.match").status == "ok"


def test_unreadable_meta_is_a_warning(layout: Layout) -> None:
    layout.store_file.write_bytes(b"not a database" * 100)
    finding = _by_id(audit(), "key.match")
    assert finding.status == "warn"
    assert "meta cannot be read" in finding.summary


def test_no_store_yet_is_information(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(tmp_path / "empty"))
    report = audit()
    assert _by_id(report, "key.match").status == "info"
    assert not (tmp_path / "empty").exists()


# ---------------------------------------------------------------------------
# Where the store lives: the working directory, git, synced folders
# ---------------------------------------------------------------------------


def _git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git config, so a developer's global excludes
    cannot ignore the store for the test."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_project_store_in_a_working_tree(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store chosen by ./.claude-memory is a warning; inside a working
    tree it is a warning while unignored, information once ignored, and
    high once tracked."""
    _git_env(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.delenv(_config.ENV_DIR_OVERRIDE)
    monkeypatch.chdir(repo)
    Store.create(repo / ".claude-memory").close()

    report = audit()
    assert report.store_rule == "./.claude-memory"
    assert _by_id(report, "store.selection").status == "warn"
    assert _by_id(report, "store.git").status == "warn"
    assert _by_id(report, "keys.git").status == "ok"

    (repo / ".gitignore").write_text(".claude-memory/\n", encoding="utf-8")
    ignored = _by_id(audit(), "store.git")
    assert ignored.status == "info"
    assert ignored.details["ignored"] is True

    (repo / ".gitignore").unlink()
    _git(repo, "add", ".claude-memory")
    report = audit()
    tracked = _by_id(report, "store.git")
    assert tracked.status == "high"
    assert tracked.fix is not None and "git rm -r --cached" in tracked.fix
    assert report.exit_code == 1


def test_git_that_cannot_run_is_skipped(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posture, "run_git", lambda *a, **kw: None)
    assert _by_id(audit(), "store.git").status == "skip"


@pytest.mark.parametrize(
    ("path", "service"),
    [
        ("/Users/a/Library/Mobile Documents/com~apple~CloudDocs/bm", "iCloud Drive"),
        ("/Users/a/Library/CloudStorage/Dropbox/bm", "cloud storage (Dropbox)"),
        ("/home/a/Dropbox/bm", "Dropbox"),
        ("/home/a/Dropbox (Team)/bm", "Dropbox"),
        ("/Users/a/OneDrive - Contoso/bm", "OneDrive"),
        ("/Users/a/Google Drive/My Drive/bm", "Google Drive"),
        ("/Users/a/iCloudDrive/bm", "iCloud Drive"),
        ("/Users/a/Box Sync/bm", "Box"),
        ("/home/a/.claude-memory", None),
        ("/home/a/dropboxes/bm", None),
    ],
)
def test_synced_root(path: str, service: str | None) -> None:
    assert synced_root(Path(path)) == service


def test_store_and_keys_in_a_synced_folder(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synced store is a warning; its key synced with it is high."""
    synced = (tmp_path / "Dropbox").resolve()
    store_dir, keys = synced / "store", synced / "keys"
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(store_dir))
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(keys))
    Store.create(store_dir, keys_dir=keys).close()
    report = audit()
    assert _by_id(report, "store.synced").status == "warn"
    assert _by_id(report, "keys.synced").status == "high"
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(layout.keys_dir))
    assert _by_id(audit(), "keys.synced").status == "ok"


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


def test_daemon_recorded_on_loopback_is_ok(layout: Layout) -> None:
    _write_state(layout)
    finding = _by_id(audit(), "daemon.bind")
    assert finding.status == "ok"
    assert finding.details["host"] == "127.0.0.1"
    assert finding.details["host_recorded"] is False


def test_daemon_recorded_on_another_address_is_high(layout: Layout) -> None:
    _write_state(layout, host="0.0.0.0")
    finding = _by_id(audit(), "daemon.bind")
    assert finding.status == "high"
    assert "0.0.0.0:7397" in finding.summary


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("127.0.0.2", "ok"),
        ("::1", "ok"),
        ("[::1]", "ok"),
        ("LOCALHOST", "ok"),
        ("127.example.com", "high"),
        ("::", "high"),
        ("192.168.1.5", "high"),
    ],
)
def test_recorded_host_is_parsed_as_an_address(
    layout: Layout, host: str, status: str
) -> None:
    """Loopback is the address block, not a prefix: a name that starts
    with ``127.`` is not one."""
    _write_state(layout, host=host)
    assert _by_id(audit(), "daemon.bind").status == status


def test_stale_and_malformed_state(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(layout)
    monkeypatch.setattr(_daemon_client, "pid_alive", lambda pid: False)
    assert _by_id(audit(), "daemon.state").status == "info"
    layout.state_file.write_text("{", encoding="utf-8")
    assert _by_id(audit(), "daemon.bind").status == "warn"


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------


def test_malformed_config_is_a_warning(layout: Layout) -> None:
    config_path = _config.default_config_path()
    config_path.write_text("[storage\n", encoding="utf-8")
    report = audit()
    assert _by_id(report, "config.load").status == "warn"
    assert _by_id(report, "store.selection").status == "ok"


def test_settings_and_policy_facts(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy and security sections are read through getattr, so a
    build with them reports their values and one without reads the
    defaults."""
    report = audit()
    facts = _by_id(report, "config.settings")
    assert facts.details["telemetry.enabled"] is True
    assert facts.details["security.strict_provenance"] is False
    assert "at its defaults" in _by_id(report, "config.policy").summary

    config = Config()
    setattr(
        config,
        "policy",
        SimpleNamespace(forbid_force=True, forbid_acknowledge=["credential"]),
    )
    setattr(config, "security", SimpleNamespace(strict_provenance=True))
    setattr(config, "managed_keys", frozenset({"policy.forbid_force"}))
    monkeypatch.setattr(sandbox_settings, "load_config_read_only", lambda: config)
    report = audit()
    policy = _by_id(report, "config.policy")
    assert "force, acknowledge_credential" in policy.summary
    assert "1 key(s) pinned" in policy.summary
    assert (
        _by_id(report, "config.settings").details["security.strict_provenance"] is True
    )


def test_overrides_are_listed(layout: Layout) -> None:
    finding = _by_id(audit(), "env.overrides")
    assert finding.status == "info"
    assert _config.ENV_DIR_OVERRIDE in finding.details


# ---------------------------------------------------------------------------
# Claude Code's sandbox settings
# ---------------------------------------------------------------------------


def _block() -> dict[str, Any]:
    return sandbox_settings.build_plan(
        sandbox_settings.load_config_read_only(), platform="linux"
    ).settings


@posix_only
def test_generated_block_in_user_settings_passes(layout: Layout) -> None:
    _write_json(layout.home / ".claude" / "settings.json", _block())
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox").status == "ok"
    assert _by_id(report, "claude.sandbox.scope").status == "info"
    assert report.status == "ok"


@posix_only
def test_generated_block_in_managed_settings_passes(layout: Layout) -> None:
    _write_json(layout.managed, _block())
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox").status == "ok"
    assert "claude.sandbox.scope" not in _ids(report)


@posix_only
def test_managed_scalars_win_and_drop_ins_merge(layout: Layout) -> None:
    """A managed false over a user true is off; a drop-in's denies merge
    with the main file's."""
    block = _block()
    user = json.loads(json.dumps(block))
    _write_json(layout.home / ".claude" / "settings.json", user)
    _write_json(layout.managed, {"sandbox": {"enabled": False}})
    assert _by_id(audit(platform="linux"), "claude.sandbox").status == "info"

    rules = block["permissions"].pop("deny")
    _write_json(layout.managed, block)
    _write_json(
        layout.managed.parent / "managed-settings.d" / "10-deny.json",
        {"permissions": {"deny": rules}},
    )
    (layout.home / ".claude" / "settings.json").unlink()
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox").status == "ok"


@posix_only
def test_sandbox_gaps_are_warnings(layout: Layout) -> None:
    block = _block()
    sandbox = block["sandbox"]
    del sandbox["failIfUnavailable"]
    sandbox["allowUnsandboxedCommands"] = True
    home = sandbox_settings.home_dir()
    state = sandbox_settings.sandbox_path(layout.state_dir, home)
    sandbox["filesystem"]["denyRead"].remove(state)
    state_rule = sandbox_settings.rule_path(layout.state_dir, home)
    block["permissions"]["deny"] = [
        rule
        for rule in block["permissions"]["deny"]
        if f"({state_rule}/**)" not in rule
    ]
    sandbox["network"] = {
        "allowedDomains": [
            "127.0.0.1",
            "localhost:7397",
            "localhost:8080",
            "[::1]",
            "github.com",
        ],
        "allowLocalBinding": True,
    }
    sandbox["excludedCommands"] = ["docker *"]
    _write_json(layout.managed, block)
    report = audit(platform="linux")
    for finding_id in (
        "claude.sandbox.fail_if_unavailable",
        "claude.sandbox.unsandboxed_commands",
        "claude.sandbox.deny_read",
        "claude.sandbox.deny_rules",
        "claude.sandbox.network",
        "claude.sandbox.local_binding",
        "claude.sandbox.excluded_commands",
    ):
        assert _by_id(report, finding_id).status == "warn", finding_id
    assert state in _by_id(report, "claude.sandbox.deny_read").details["missing"]
    network = _by_id(report, "claude.sandbox.network").details["entries"]
    assert network == ["127.0.0.1", "localhost:7397", "[::1]"]
    assert "claude.sandbox" not in _ids(report)
    assert report.exit_code == 2


@posix_only
def test_broad_denies_cover_everything(layout: Layout) -> None:
    """A deny of the whole filesystem and the bare tool names cover every
    directory; the missing project rules are only information."""
    block = _block()
    block["sandbox"]["filesystem"]["denyRead"] = ["/"]
    block["permissions"]["deny"] = ["Read", "Edit"]
    _write_json(layout.managed, block)
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox").status == "ok"
    assert _by_id(report, "claude.sandbox.project_rules").status == "info"


@posix_only
def test_drop_ins_override_the_managed_file_in_name_order(layout: Layout) -> None:
    """Claude Code merges managed-settings.json first and the drop-ins
    after it in name order, a later file's value replacing an earlier
    one's; a hidden file in the directory is skipped."""
    _write_json(layout.managed, _block())
    drop_ins = layout.managed.parent / "managed-settings.d"
    _write_json(
        drop_ins / "20-loosen.json", {"sandbox": {"allowUnsandboxedCommands": True}}
    )
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox.unsandboxed_commands").status == "warn"

    _write_json(
        drop_ins / "30-tighten.json", {"sandbox": {"allowUnsandboxedCommands": False}}
    )
    _write_json(drop_ins / ".00-off.json", {"sandbox": {"enabled": False}})
    report = audit(platform="linux")
    assert "claude.sandbox.unsandboxed_commands" not in _ids(report)
    assert _by_id(report, "claude.sandbox").status == "ok"
    assert (
        str(drop_ins / ".00-off.json")
        not in (_by_id(report, "claude.sandbox").details["read"])
    )


@posix_only
def test_filesystem_isolation_switched_off_is_a_gap(layout: Layout) -> None:
    block = _block()
    _write_json(layout.managed, block)
    _write_json(
        layout.home / ".claude" / "settings.json",
        {"sandbox": {"filesystem": {"disabled": True}}},
    )
    report = audit(platform="linux")
    assert _by_id(report, "claude.sandbox.filesystem_disabled").status == "warn"
    assert "claude.sandbox" not in _ids(report)
    assert report.exit_code == 2


@posix_only
def test_allow_read_that_reopens_a_directory_is_a_gap(
    tmp_path: Path, layout: Layout
) -> None:
    """Where read rules overlap the narrower path wins, so an allowRead
    entry inside a denied directory, or between it and the deny above it,
    opens it again; a broader one does not. A managed
    allowManagedReadPathsOnly leaves the user's entries out."""
    block = _block()
    _write_json(layout.managed, block)
    user = layout.home / ".claude" / "settings.json"

    _write_json(user, {"sandbox": {"filesystem": {"allowRead": ["~/"]}}})
    assert _by_id(audit(platform="linux"), "claude.sandbox").status == "ok"

    _write_json(
        user,
        {"sandbox": {"filesystem": {"allowRead": ["~/.claude-memory/exports"]}}},
    )
    finding = _by_id(audit(platform="linux"), "claude.sandbox.allow_read")
    assert finding.status == "warn"
    assert finding.details["reopened"] == {
        "~/.claude-memory": ["~/.claude-memory/exports"]
    }

    wide = json.loads(json.dumps(block))
    wide["sandbox"]["filesystem"]["denyRead"] = ["/"]
    _write_json(layout.managed, wide)
    _write_json(user, {"sandbox": {"filesystem": {"allowRead": [str(tmp_path)]}}})
    reopened = _by_id(audit(platform="linux"), "claude.sandbox.allow_read")
    home = sandbox_settings.home_dir()
    assert (
        sandbox_settings.sandbox_path(layout.store_dir, home)
        in (reopened.details["reopened"])
    )

    wide["sandbox"]["filesystem"]["allowManagedReadPathsOnly"] = True
    _write_json(layout.managed, wide)
    assert _by_id(audit(platform="linux"), "claude.sandbox").status == "ok"


@posix_only
def test_unspecified_address_and_the_domains_lock(layout: Layout) -> None:
    """``0.0.0.0`` without a port reaches the daemon like 127.0.0.1 does;
    under a managed allowManagedDomainsOnly the user's entries do not
    apply."""
    block = _block()
    _write_json(layout.managed, block)
    user = layout.home / ".claude" / "settings.json"
    _write_json(
        user, {"sandbox": {"network": {"allowedDomains": ["0.0.0.0", "localhost"]}}}
    )
    finding = _by_id(audit(platform="linux"), "claude.sandbox.network")
    assert finding.details["entries"] == ["0.0.0.0", "localhost"]

    block["sandbox"]["network"] = {"allowManagedDomainsOnly": True}
    _write_json(layout.managed, block)
    assert "claude.sandbox.network" not in _ids(audit(platform="linux"))


@posix_only
def test_unreadable_settings_file_is_a_warning(layout: Layout) -> None:
    path = layout.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    finding = _by_id(audit(platform="linux"), "claude.settings")
    assert finding.status == "warn"
    assert finding.path == str(path)
    _write_json(path, ["a list"])
    assert (
        "not a JSON object"
        in _by_id(audit(platform="linux"), "claude.settings").summary
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int | str | None:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_json_exit_code_matches_the_report(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout.keyring.current_path().unlink()
    code = _run_cli(["security", "audit", "--json"], monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["exit_code"] == 1
    assert report["status"] == "high"


def test_cli_text(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["security", "audit"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.startswith("bettermemory security audit: ok\n")
    assert "key.match" in out
    assert "0 high, 0 warning(s)" in out

    layout.keyring.current_path().unlink()
    assert _run_cli(["security", "audit"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "fix: restore" in out
