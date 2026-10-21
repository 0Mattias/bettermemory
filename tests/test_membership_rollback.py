"""The rollback of design 8.12: a store this build migrated, opened by
9.0.0rc3's code, then by this build again.

rc3 is its release commit (b16a5604) read by `git archive` and installed
with uv into a virtual environment the test makes. The store is written in
a repository under the test's directory through this build's store (the
records carry origins as rc3 recorded them, and a telemetry event), and
`membership_migrate.migrate` homes them. rc3's `bettermemory health`,
`bettermemory log verify` and the session-start hook (through rc3's own
daemon, on a port the system picks) all exit 0 on that store; rc3's health
counts the membership rows among the events it has seen (decision B8:
rc3 reads a kind it does not know as an event). This build then opens the
store again: there is nothing to home, every membership table and
membership log row is as the migration left it, and `log verify` passes.

Everything rc3 resolves lives under the test's directory: the store, the
keys, the daemon's state, the home directory and the user directories
under it. Git runs with no user or system configuration and a fixed
author. Skipped only when `uv` is not on PATH or `git archive` cannot read
the release commit (a clone without it, such as a shallow one).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import KEYS_DIR_ENV, Config
from bettermemory.log import CONTROL_KINDS
from bettermemory.machine import open_machine
from bettermemory.membership_migrate import ensure_migrated, migrate
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import (
    MEMBERSHIP_EVENT_KINDS,
    MEMBERSHIP_MUTATION_KINDS,
    MEMBERSHIP_TABLES,
    MUTATION_KINDS,
    STORE_FILENAME,
    Store,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: 9.0.0rc3's release commit.
RC3_COMMIT = "b16a5604"
RC3_VERSION = "9.0.0rc3"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
_REMOTE = "https://example.invalid/acme/rollback.git"
_MEMBERSHIP_KINDS = MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS


# ---------------------------------------------------------------------------
# rc3, installed
# ---------------------------------------------------------------------------


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


@pytest.fixture(scope="module")
def rc3_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The interpreter of a virtual environment with rc3 installed from its
    release commit."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH: rc3 cannot be installed")
    work = tmp_path_factory.mktemp("rc3")
    archive = work / "rc3.tar"
    done = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "archive",
            "--format=tar",
            "-o",
            str(archive),
            RC3_COMMIT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(
            f"git archive {RC3_COMMIT} failed in {_REPO_ROOT} (a clone without "
            f"the rc3 release commit, such as a shallow one): {done.stderr.strip()}"
        )
    source = work / "source"
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(source, filter="data")
        else:  # pragma: no cover - Python before the extraction filters
            tar.extractall(source)
    venv = work / "venv"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    for args in (
        [uv, "venv", "--quiet", "--python", sys.executable, str(venv)],
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(_venv_python(venv)),
            str(source),
        ],
    ):
        made = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False, timeout=600
        )
        assert made.returncode == 0, f"{args[:3]}: {made.stderr}"
    python = _venv_python(venv)
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import bettermemory; print(bettermemory.__version__); print(bettermemory.__file__)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    version, module = probe.stdout.split()
    assert version == RC3_VERSION
    assert Path(module).resolve().is_relative_to(venv.resolve())
    return python


# ---------------------------------------------------------------------------
# The store this build migrated
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No user or system configuration reaches git, a fixed author and
    committer; returns the empty global configuration file."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "fixture@example.invalid")
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(var, raising=False)
    return empty


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _memory(body: str, repo: Path | None, head: str | None = None) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
        origin=(
            Origin(cwd=str(repo), repo=_REMOTE, worktree_root=str(repo))
            if repo is not None
            else None
        ),
        verified_head=head,
    )


def _membership_state(path: Path) -> dict[str, list[str]]:
    """Every row of every membership table, and every membership log row
    (seq, kind, MAC), as text."""
    conn = sqlite3.connect(path)
    try:
        out = {
            table: sorted(
                repr(tuple(r)) for r in conn.execute(f"SELECT * FROM {table}")
            )
            for table in MEMBERSHIP_TABLES
        }
        kinds = sorted(_MEMBERSHIP_KINDS)
        marks = ",".join("?" for _ in kinds)
        out["log"] = [
            repr(tuple(r))
            for r in conn.execute(
                f"SELECT seq, kind, mac FROM log WHERE kind IN ({marks}) ORDER BY seq",
                kinds,
            )
        ]
        return out
    finally:
        conn.close()


def _kinds(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [str(r[0]) for r in conn.execute("SELECT kind FROM log ORDER BY seq")]
    finally:
        conn.close()


def _rc3_env(tmp_path: Path, store_dir: Path, gitconfig: Path) -> dict[str, str]:
    """rc3's environment: every directory it resolves under the test's own,
    and nothing of this build on its path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BETTERMEMORY_", "PYTHON", "CLAUDE_CODE_", "XDG_"))
        and k not in ("VIRTUAL_ENV",)
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "BETTERMEMORY_DIR": str(store_dir),
            "BETTERMEMORY_KEYS_DIR": os.environ[KEYS_DIR_ENV],
            "BETTERMEMORY_STATE_DIR": str(tmp_path / "rc3-state"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _rc3(
    python: Path,
    env: dict[str, str],
    *args: str,
    cwd: Path,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-I", "-m", "bettermemory", *args],
        env=env,
        cwd=cwd,
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _rc3_daemon_port_zero(python: Path, env: dict[str, str], home: Path) -> None:
    """rc3's config with ``[daemon] port = 0``, so the daemon its hook
    starts takes a port the system picks, where rc3 resolves its config
    under the test's home directory. Where it does not (Windows reads the
    profile's folders from the system, not the environment), no config is
    written and rc3's daemon falls back from its default port to one the
    system picks when that one is held."""
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from bettermemory import config\n"
        "path = config.default_config_path()\n"
        "if path.resolve().is_relative_to(Path(sys.argv[1]).resolve()):\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    text = config.DEFAULT_CONFIG.replace('\\nport = 7397\\n', '\\nport = 0\\n')\n"
        "    assert text != config.DEFAULT_CONFIG\n"
        "    path.write_text(text, encoding='utf-8')\n"
        "    assert config.load_config().daemon.port == 0\n"
        "    print('port 0')\n"
    )
    done = subprocess.run(
        [str(python), "-I", "-c", script, str(home)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_rc3_reads_a_migrated_store_and_this_build_reopens_it(
    rc3_python: Path, tmp_path: Path, neutral_git: Path
) -> None:
    """Design 8.12 and decision B8: rc3's health, log verify and
    session-start run on a store with membership rows and exit 0; rc3's
    health counts every membership row as an event; this build then finds
    nothing to home, the membership rows as they were, and the log
    verified."""
    tmp = tmp_path.resolve()
    repo = tmp / "Developer" / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("rollback fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "remote", "add", "origin", _REMOTE)
    head = _git(repo, "rev-parse", "HEAD")
    store_dir = tmp / "store"
    path = store_dir / STORE_FILENAME
    identity = open_machine(tmp / "state")
    with Store(store_dir) as store:
        store.put_memory(_memory("the release runs from the tag", repo, head))
        store.put_memory(_memory("the docs build with mkdocs", repo))
        store.put_memory(_memory("written outside any repository", None))
        store.write_episode(
            session_id="s-rollback",
            body="worked on the release",
            origin=Origin(cwd=str(repo), worktree_root=str(repo)),
            now=_NOW,
        )
        store.record_event("recall", query="the release")
        report = migrate(store, identity)
        assert report.homed == 4
        assert store.log_verify()["status"] == "ok"
    membership = _membership_state(path)
    kinds = _kinds(path)
    membership_rows = sum(1 for k in kinds if k in _MEMBERSHIP_KINDS)
    rc3_events = sum(
        1
        for k in kinds
        if k not in MUTATION_KINDS
        and k not in CONTROL_KINDS
        and k not in MEMBERSHIP_EVENT_KINDS
    )
    assert membership_rows > 0 and rc3_events == 1

    env = _rc3_env(tmp, store_dir, neutral_git)
    _rc3_daemon_port_zero(rc3_python, env, tmp / "home")

    health = _rc3(rc3_python, env, "health", cwd=repo)
    assert health.returncode == 0, health.stderr
    seen = re.search(r"^Events seen:\s+(\d+)$", health.stdout, re.MULTILINE)
    assert seen is not None, health.stdout
    assert int(seen.group(1)) == membership_rows + rc3_events

    verify = _rc3(rc3_python, env, "log", "verify", cwd=repo)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert verify.stdout.startswith("bettermemory log verify: ok"), verify.stdout

    payload = {
        "session_id": "rc3-rollback",
        "cwd": str(repo),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    try:
        hook = _rc3(
            rc3_python,
            env,
            "hook",
            "session-start",
            cwd=repo,
            stdin=json.dumps(payload),
        )
        assert hook.returncode == 0, hook.stderr
        # rc3's hook exits 0 whatever happens; a failure is one stderr line.
        assert "bettermemory hook session-start:" not in hook.stderr, hook.stderr
        states = list((tmp / "rc3-state").glob("daemon-*.json"))
        assert len(states) == 1
        state: dict[str, Any] = json.loads(states[0].read_text(encoding="utf-8"))
        assert state["version"] == RC3_VERSION
        assert Path(state["store"]).resolve() == path.resolve()
    finally:
        down = _rc3(rc3_python, env, "down", cwd=repo)
    assert down.returncode == 0, down.stdout + down.stderr

    # This build opens the store again.
    after = _kinds(path)
    assert after[: len(kinds)] == kinds
    assert all(k not in MUTATION_KINDS for k in after[len(kinds) :]), after[
        len(kinds) :
    ]
    assert _membership_state(path) == membership
    with Store.open(path) as store:
        assert ensure_migrated(store, identity, config=Config()) is None
        verified = store.log_verify()
        assert verified["status"] == "ok", verified
    assert _membership_state(path) == membership
