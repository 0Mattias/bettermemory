"""Shared pytest fixtures.

Each test gets a fresh temp memory directory so the tests stay hermetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

# Make `src/` importable without depending on the editable install. This is
# belt-and-suspenders: if the venv's editable .pth file is unreadable for any
# reason (macOS UF_HIDDEN propagation in iCloud-synced dirs, a stale env, a
# tooling bug), tests still pass. Child processes spawned with
# `sys.executable` need the same shield via their environment — see
# `shielded_child_env` below.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.session import SessionState
from bettermemory import _daemon_client
from bettermemory import config as _config
from bettermemory import log as _log
from bettermemory.store import Store

from ._event_helpers import EventLog


def set_git_discovery_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin git's upward repo discovery inside the pytest sandbox.

    For tests whose premise is "this directory is NOT inside any git
    repo": whenever ``tmp_path`` itself sits under a real checkout (a
    TMPDIR redirected into one, ``--basetemp`` pointed inside one — CI
    images with repo-rooted temp, developers aiming basetemp at a
    workspace), git's upward walk escapes the fixture and finds the
    enclosing repo, silently flipping the premise: ``capture()`` reads
    that repo's branch/worktree_root and the commit-log primitives
    return its history instead of the None/fallback the tests assert.

    ``GIT_CEILING_DIRECTORIES`` stops the walk, but git honours a
    ceiling only as a STRICT ancestor of the probe directory
    (``longest_ancestor_length`` requires ``path[len] == '/'``;
    verified on git 2.50.1: a probe launched FROM the ceiling directory
    itself escapes upward unhindered). An entry AT the probe dir is
    therefore inert — so the helper pins ``tmp_path.parent`` AND
    ``tmp_path.parent.parent``, strict ancestors of every probe these
    tests launch (from ``tmp_path`` or below, and from
    ``tmp_path.parent`` for store-IS-tmp_path shapes), while any repo a
    test inits at or under ``tmp_path`` stays strictly below both
    ceilings and still resolves normally.

    Deliberately a plain per-test opt-in, NOT an autouse fixture:
    outside-repo premises are the exception, and a blanket ceiling
    could mask real discovery behaviour in tests that *want* the
    upward walk exercised.

    Test-side control ONLY: production code never sets this variable —
    a real user's own ``GIT_CEILING_DIRECTORIES`` is their
    configuration to keep.

    The doctor nested-store checks opened this hermeticity class and
    carried the original local twin of this helper (since folded in
    here): their upward walks append phantom levels to
    ``scanned_parent_toplevels`` when unpinned — removing the ceiling
    while pointing ``--basetemp`` inside a scratch git repo
    reintroduces exactly those failures.
    """
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        os.pathsep.join([str(tmp_path.parent), str(tmp_path.parent.parent)]),
    )


def shielded_child_env() -> dict[str, str]:
    """``os.environ`` with the repo's ``src/`` prepended to ``PYTHONPATH``
    (existing entries preserved) — the child-process leg of the import
    shield documented at the top of this module.

    That shield patches only THIS interpreter's ``sys.path``, so a bare
    ``sys.executable`` child still depends on the venv's editable
    ``.pth`` hook — which Python 3.13's ``site`` skips when the file
    carries the hidden flag, and on iCloud-synced checkouts macOS
    fileproviderd asynchronously re-stamps ``UF_HIDDEN`` on freshly
    written ``.pth`` files. An unshielded child then loses the editable
    install and dies on ``import bettermemory`` before exercising
    whatever the test actually asserts (or, worse, trips an
    importability skip-guard and silently drops subprocess coverage).
    Every direct ``sys.executable`` spawn in the suite passes this env —
    uniformly, so stdlib-only children can't silently drift out from
    under the shield when someone extends them. Belt-and-suspenders like
    the in-process shield: inert when the ``.pth`` is healthy."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_SRC) if not existing else str(_SRC) + os.pathsep + existing
    return env


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def keys_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every store a test opens keeps its key under the test's own
    directory, never under the user's config directory."""
    keys = tmp_path / "keys"
    monkeypatch.setattr(_log, "default_keys_dir", lambda: keys)
    # A server or CLI the test spawns resolves the same directory through
    # the environment, so a subprocess leaves no key under the user's
    # config directory either.
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(keys))
    return keys


#: The developer's own global store, read once at import time, before any
#: test fakes the home directory. The autouse guard below redirects a
#: resolution that would land here; a faked home resolves elsewhere and
#: passes through, so the tests of the resolution rule are unaffected.
_DEVELOPER_GLOBAL_DIR = (Path.home() / _config.GLOBAL_DIR_NAME).resolve()


@pytest.fixture(autouse=True)
def storage_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """No test resolves the developer's own store or config.

    A resolution that would land on the developer's global store (no
    ``BETTERMEMORY_DIR``, no project directory, the real home) is
    redirected under the test's own directory. ``build_server()`` with
    no arguments was the call that reached it: with the keys directory
    above already redirected, every run of the suite opened the
    developer's live store without its key and rekeyed it. An explicit
    ``storage.directory``, the environment override and a faked home
    resolve exactly as before, so nothing a test asked for changes.

    ``default_config_path`` is pointed at the test's directory for the
    same reason: a test must not read the developer's ``config.toml``,
    and ``load_config`` creates a default one where none exists. The
    file is written up front with the shipped defaults so ``load_config``
    stays silent, which the tests of the hook commands' silence rely on.

    In-process only. A child process the test spawns resolves on its
    own; the spawn helpers in the suite set ``BETTERMEMORY_DIR`` for it.
    """
    # A sibling of the test's `tmp_path`, not a child: tests of the
    # atomic-write helpers count the entries under `tmp_path`.
    guard_dir = tmp_path_factory.mktemp("bettermemory-guard")
    fallback = (guard_dir / "store").resolve()
    original = _config.Config.resolved_directory

    def guarded(self: _config.Config, cwd: Path | None = None) -> Path:
        resolved = original(self, cwd=cwd)
        if resolved == _DEVELOPER_GLOBAL_DIR:
            return fallback
        return resolved

    monkeypatch.setattr(_config.Config, "resolved_directory", guarded)
    config_path = guard_dir / "config.toml"
    config_path.write_text(_config.DEFAULT_CONFIG, encoding="utf-8")
    monkeypatch.setattr(_config, "default_config_path", lambda: config_path)
    # A daemon a test starts (through the shim, a hook command or `up`)
    # keeps its state file under the test's directory, in this process
    # and in any child, and is stopped when the test ends.
    state_dir = guard_dir / "state"
    monkeypatch.setenv(_daemon_client.STATE_DIR_ENV, str(state_dir))
    yield fallback
    for state_file in state_dir.glob("daemon-*.json"):
        state = _daemon_client.read_state_file(state_file)
        if state is not None:
            _daemon_client.shutdown_daemon(state, timeout=10.0)
        state_file.unlink(missing_ok=True)


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def config(memory_dir: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState()


@pytest.fixture
def event_log(store: Store) -> EventLog:
    """A real recorder over the test's store for event-consumer tests."""
    return EventLog(store=store)
