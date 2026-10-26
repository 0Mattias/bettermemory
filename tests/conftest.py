"""Shared pytest fixtures.

Each test gets a fresh temp memory directory so the tests stay hermetic.

Two variables serve the membership legs of CI (design 8.7), which point
``--basetemp`` at a network mount so every ``tmp_path`` sits there.
``BETTERMEMORY_TEST_LOCAL_TMP`` names a directory on local disk for what a
test keeps beside the filesystem it exercises: its stores, keys and machine
state (`local_tmp_path`). SQLite's WAL needs shared memory a network
filesystem does not give, and the legs test the repositories, tokens and
project files on the mount, not the store. ``BETTERMEMORY_TEST_NO_BIRTH=1``
reads every object as a filesystem without birth time does (E15), on any
disk. With neither set, every file stays where it always was.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

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
from bettermemory import _caches
from bettermemory import _daemon_client
from bettermemory import config as _config
from bettermemory import log as _log
from bettermemory import review as _review
from bettermemory.store import Store

from ._event_helpers import EventLog

#: A directory on local disk for each test's stores, keys and machine state
#: (`local_tmp_path`); unset, they stay under the test's `tmp_path`.
LOCAL_TMP_ENV = "BETTERMEMORY_TEST_LOCAL_TMP"
#: "1": the whole session reads no birth time (`location.REPORT_BIRTH`), as
#: on NFSv3, whatever the disk under it reports.
NO_BIRTH_ENV = "BETTERMEMORY_TEST_NO_BIRTH"
#: Seconds a new file's times take to settle on the filesystem under
#: ``--basetemp`` (`settled`); unset or empty, none. A cifs client reports
#: its own write time for a file it wrote until its attribute cache
#: expires (``actimeo``) and the data is flushed (``closetimeo``); the
#: server's time, which every later read reports, can be some tens of
#: milliseconds later. The SMB leg of CI sets 3.
SETTLE_ENV = "BETTERMEMORY_TEST_SETTLE_SECONDS"


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


def child_tau_env(env: dict[str, str], site: Path, tau: float | None) -> dict[str, str]:
    """``env`` with a ``sitecustomize`` on ``PYTHONPATH`` that sets both
    review thresholds in every Python child at start, so a daemon the shim
    or the CLI spawns runs at ``tau`` whatever the package ships: the
    child-process leg of ``review_tau_unset``. ``None`` issues no review."""
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_bytes(
        (
            "import bettermemory.review as _review\n"
            f"_review.TAU_UNSAFE = {tau!r}\n"
            f"_review.TAU_SECRET = {tau!r}\n"
        ).encode("utf-8")
    )
    out = dict(env)
    existing = out.get("PYTHONPATH")
    out["PYTHONPATH"] = str(site) if not existing else str(site) + os.pathsep + existing
    return out


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    return d


def settle_seconds() -> float:
    """The seconds ``BETTERMEMORY_TEST_SETTLE_SECONDS`` names, 0 unset."""
    named = os.environ.get(SETTLE_ENV, "").strip()
    return float(named) if named else 0.0


def settled(path: Path) -> Path:
    """`path` once the times of the files just written under it are the
    ones every later read reports: a pause of `settle_seconds`, none on a
    local disk. For a test that records a file's times as a witness (the
    init stamps of a commit-less repository) and reads them again after a
    move: the production sighting comes well after the cache's lifetime,
    the test's within it."""
    pause = settle_seconds()
    if pause > 0:
        time.sleep(pause)
    return path


@pytest.fixture(scope="session")
def local_tmp_root() -> Path | None:
    """This session's own directory under ``BETTERMEMORY_TEST_LOCAL_TMP``,
    made fresh, or None when the variable is unset."""
    named = os.environ.get(LOCAL_TMP_ENV)
    if not named:
        return None
    parent = Path(named)
    parent.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="pytest-", dir=parent)))


@pytest.fixture
def local_tmp_path(
    request: pytest.FixtureRequest, tmp_path: Path, local_tmp_root: Path | None
) -> Path:
    """A fresh directory on local disk for the test's stores, keys and
    machine state, apart from the filesystem the test exercises: under
    ``BETTERMEMORY_TEST_LOCAL_TMP`` when it is set (the membership legs of
    CI, whose ``tmp_path`` is on a network mount), else the test's own
    `tmp_path`, so a run without the variable lays its files out as it
    always did."""
    if local_tmp_root is None:
        return tmp_path
    name = re.sub(r"\W", "_", request.node.name)[:30]
    return Path(tempfile.mkdtemp(prefix=f"{name}-", dir=local_tmp_root))


@pytest.fixture(autouse=True)
def no_harness_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits the harness session of the Claude Code process
    that may be running the suite: an in-process tool call would stamp
    it on its events (`events.HARNESS_STAMPED_KINDS`). A test of the
    join sets it explicitly."""
    from bettermemory.identity import ENV_HARNESS_SESSION

    monkeypatch.delenv(ENV_HARNESS_SESSION, raising=False)


@pytest.fixture(autouse=True)
def keys_dir(local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every store a test opens keeps its key under the test's own
    directory (on local disk, `local_tmp_path`), never under the user's
    config directory."""
    keys = local_tmp_path / "keys"
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
    tmp_path_factory: pytest.TempPathFactory,
    local_tmp_root: Path | None,
    monkeypatch: pytest.MonkeyPatch,
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
    Under ``BETTERMEMORY_TEST_LOCAL_TMP`` the directory is on local disk
    with the test's other stores.
    """
    # A sibling of the test's `tmp_path`, not a child: tests of the
    # atomic-write helpers count the entries under `tmp_path`.
    if local_tmp_root is None:
        guard_dir = tmp_path_factory.mktemp("bettermemory-guard")
    else:
        guard_dir = Path(
            tempfile.mkdtemp(prefix="bettermemory-guard-", dir=local_tmp_root)
        )
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
            _daemon_client.shutdown_daemon(state, state_dir=state_dir, timeout=10.0)
        state_file.unlink(missing_ok=True)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "shipped_tau: the test reads the review thresholds the package ships"
    )


@pytest.fixture(autouse=True)
def review_tau_unset(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test starts with the review off: both thresholds None, so
    ``memory_write`` issues no review and behaves as it did before the
    review existed. The package ships the sealed tau; a test that exercises
    the review sets tau explicitly (``review.tau_override`` or the two
    module attributes), and a test marked ``shipped_tau`` reads the shipped
    constants."""
    if request.node.get_closest_marker("shipped_tau") is None:
        monkeypatch.setattr(_review, "TAU_UNSAFE", None)
        monkeypatch.setattr(_review, "TAU_SECRET", None)


@pytest.fixture(autouse=True)
def clear_caches() -> None:
    """Every test starts with the package's module-level caches empty.

    A cache would otherwise hand a test the value an earlier test
    computed, and a test that counts git processes or patches what a
    cache reads would measure the earlier test's work instead of its
    own. Each module registers its caches' clearers with
    ``bettermemory._caches`` when it is imported.
    """
    _caches.clear_all()


def _outside(
    bases: Sequence[str], path: object, *, existing_only: bool = False
) -> str | None:
    """The real path of `path` when it lies outside every one of `bases`,
    else None. A value that is not a path is not checked; with
    `existing_only`, neither is a path that does not exist."""
    if not isinstance(path, (str, os.PathLike)):
        return None
    here = os.path.realpath(os.fspath(path))
    if existing_only and not os.path.lexists(here):
        return None
    if any(here == base or here.startswith(base + os.sep) for base in bases):
        return None
    return here


@pytest.fixture(scope="session", autouse=True)
def scratch_session(
    tmp_path_factory: pytest.TempPathFactory, local_tmp_root: Path | None
) -> Iterator[Path]:
    """The whole session runs inside pytest's base temporary directory: the
    environment names scratch directories before any fixture runs, and
    every call that would reach a directory of the developer's fails the
    test that made it.

    Session scope because a module- or session-scoped fixture runs before
    any function-scoped one: a bench fixture that wrote through
    `memory_write` before `storage_dir` named the state directory opened
    the machine (`location.process_machine`) in the developer's own state
    directory and minted a machine id there. Here the state directory is
    named and the process cwd is a scratch workspace for the whole session
    (each test then has its own; `workspace`), `BETTERMEMORY_WORKSPACE`
    from the shell is dropped, and the user's state and config directories
    (`platformdirs`), where the state directory, the keys directory and
    the config resolve when nothing names them, are the session's own: a
    test that drops a variable to read the default reads a scratch one.
    `BETTERMEMORY_DIR` and `BETTERMEMORY_KEYS_DIR` from the shell running
    the suite are dropped: the developer's store and keys are not a
    test's, and a test that keeps its keys where `log.default_keys_dir`
    points reads them back through the default, not through a variable
    the shell set.

    Under ``BETTERMEMORY_TEST_LOCAL_TMP`` the state and config directories
    are on local disk, in the session's directory there; the workspace
    stays beside every `tmp_path`. Under ``BETTERMEMORY_TEST_NO_BIRTH=1``
    `location.REPORT_BIRTH` is off for the whole session, the switch the
    `no_birth` fixture of the membership tests turns for one test.

    The guards, each checked against the session's base temporary
    directory, which holds every `tmp_path` (and the session's directory
    under ``BETTERMEMORY_TEST_LOCAL_TMP``, which holds every
    `local_tmp_path`), and each failing through `pytest.fail`, which no
    `except Exception` in the code under test absorbs:

    - a location capture (`location.capture` and the uncached read under
      it) of a path that exists outside it. A capture of a path that does
      not exist (an origin a test made up, such as ``/projects/foo``)
      stats it, finds nothing and reads nothing more;
    - a checkout token write or re-mint, whatever the path;
    - the state directory `_daemon_client.resolve_state_dir` resolves, and
      the one `machine.open_machine` is given or resolves;
    - the keys directory `store.resolve_keys_dir` and
      `log.default_keys_dir` resolve.

    A child process a test spawns inherits the environment named here or
    the test's own; the guards themselves are in-process.
    """
    import platformdirs

    from bettermemory import checkout_token, location, machine
    from bettermemory import store as _store
    from bettermemory.identity import ENV_WORKSPACE

    base = os.path.realpath(tmp_path_factory.getbasetemp())
    bases = [base] if local_tmp_root is None else [base, str(local_tmp_root)]
    root = tmp_path_factory.mktemp("session")
    workspace = root / "workspace"
    workspace.mkdir()
    kept = (
        root
        if local_tmp_root is None
        else Path(tempfile.mkdtemp(prefix="session-", dir=local_tmp_root))
    )

    def check(what: str, path: object, *, existing_only: bool = False) -> None:
        here = _outside(bases, path, existing_only=existing_only)
        if here is not None:
            pytest.fail(
                f"a test reached {what} at {here}, outside its scratch "
                f"directory {' or '.join(bases)}: give it a directory under "
                "tmp_path or local_tmp_path",
                pytrace=False,
            )

    real_capture = location.capture
    real_uncached = location._capture_uncached

    def capture(cwd: str, **kw: Any) -> location.Capture:
        check("a location capture", cwd, existing_only=True)
        return real_capture(cwd, **kw)

    def uncached(cwd: str, **kw: Any) -> location.Capture:
        check("a location capture", cwd, existing_only=True)
        return real_uncached(cwd, **kw)

    # The originals stay reachable for a test that measures the capture's
    # own cost (`test_membership_capture`, E16).
    capture.__wrapped__ = real_capture  # type: ignore[attr-defined]
    uncached.__wrapped__ = real_uncached  # type: ignore[attr-defined]
    real_write, real_remint = checkout_token.write, checkout_token.remint

    def write(git_dir: Any, **kw: Any) -> checkout_token.TokenWrite:
        check("a checkout token write", git_dir)
        return real_write(git_dir, **kw)

    def remint(git_dir: Any, **kw: Any) -> checkout_token.TokenWrite:
        check("a checkout token re-mint", git_dir)
        return real_remint(git_dir, **kw)

    real_state_dir = _daemon_client.resolve_state_dir

    def resolve_state_dir() -> Path:
        resolved = real_state_dir()
        check("the state directory", resolved)
        return resolved

    real_open_machine = machine.open_machine

    def open_machine(
        state_dir: Path | None = None, *args: Any, **kw: Any
    ) -> machine.MachineIdentity:
        check(
            "a machine opened",
            state_dir if state_dir is not None else _daemon_client.resolve_state_dir(),
        )
        return real_open_machine(state_dir, *args, **kw)

    real_resolve_keys, real_default_keys = (
        _store.resolve_keys_dir,
        _log.default_keys_dir,
    )

    def resolve_keys_dir() -> Path:
        resolved = real_resolve_keys()
        check("the keys directory", resolved)
        return resolved

    def default_keys_dir() -> Path:
        resolved = real_default_keys()
        check("the keys directory", resolved)
        return resolved

    def user_dir(kind: str) -> Any:
        def resolved(appname: str | None = None, *args: Any, **kw: Any) -> str:
            return str(kept / kind / (appname or ""))

        return resolved

    with pytest.MonkeyPatch.context() as mp:
        if os.environ.get(NO_BIRTH_ENV) == "1":
            mp.setattr(location, "REPORT_BIRTH", False)
        mp.setattr(platformdirs, "user_state_dir", user_dir("user-state"))
        mp.setattr(platformdirs, "user_config_dir", user_dir("user-config"))
        mp.setenv(_daemon_client.STATE_DIR_ENV, str(kept / "state"))
        mp.chdir(workspace)
        mp.delenv(ENV_WORKSPACE, raising=False)
        mp.delenv(_config.KEYS_DIR_ENV, raising=False)
        mp.delenv(_config.ENV_DIR_OVERRIDE, raising=False)
        mp.setattr(location, "capture", capture)
        mp.setattr(location, "_capture_uncached", uncached)
        mp.setattr(checkout_token, "write", write)
        mp.setattr(checkout_token, "remint", remint)
        mp.setattr(_daemon_client, "resolve_state_dir", resolve_state_dir)
        mp.setattr(machine, "open_machine", open_machine)
        mp.setattr(_store, "resolve_keys_dir", resolve_keys_dir)
        mp.setattr(_log, "default_keys_dir", default_keys_dir)
        yield root


@pytest.fixture(autouse=True)
def workspace(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every test runs in a scratch workspace of its own: a plain
    directory, outside any repository, made the process cwd.

    A caller that names no directory is resolved where it stands
    (`memory_write`, the hooks, `session-start`; design 3.1), and a first
    sight in a repository writes a checkout token into its git directory:
    resolved at the process cwd, a test run from a checkout wrote one into
    that checkout. The process cwd rather than `BETTERMEMORY_WORKSPACE`:
    a request over HTTP never reads the environment (`identity.resolve`),
    so a daemon a test serves in process resolves an undeclared caller at
    the process cwd whatever the variable says, and a test that stands in
    a repository of its own (`monkeypatch.chdir`) would have the variable
    answer ahead of it. A test that builds a repository names it (the cwd
    it stands in or passes, or the variable). A sibling of `tmp_path`
    rather than a child, since some tests count the entries under
    `tmp_path`.

    No caller an earlier test published survives into this one: an
    in-process tool call or hook publishes its caller
    (`identity.bind`, `identity.bind_transcript`) for the rest of the
    task, and a workspace that caller declared, an earlier test's
    directory, would answer `workspace_declaration` ahead of the cwd.
    """
    from bettermemory import identity

    identity._CURRENT.set(None)
    here = tmp_path_factory.mktemp("workspace")
    monkeypatch.chdir(here)
    return here


@pytest.fixture
def store(memory_dir: Path) -> Iterator[Store]:
    """The test's own Store, closed when the test ends. A test that
    closes it itself is fine: closing a closed connection is a no-op.
    Left open, each test leaked a sqlite connection to the garbage
    collector (1,914 ResourceWarnings in the rc7 release build)."""
    store = Store(memory_dir)
    try:
        yield store
    finally:
        store.close()


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
