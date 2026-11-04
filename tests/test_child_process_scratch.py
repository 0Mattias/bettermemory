"""A child process the suite starts resolves its default locations in
scratch (ENG-202, ENG-124).

A child runs none of conftest's in-process guards, so what it resolves
when nothing names a location, the store, keys, state and config
directories, follows its environment. A draft test that started children
with an environment of its own, naming neither the keys nor the state
directory, left six key rings in the developer's keys directory
(ENG-202). conftest completes every child's environment
(`scratch_child_env`, on `subprocess.Popen.__init__`); these tests start
real children the ways the suite does and read where each resolves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bettermemory import _daemon_client
from bettermemory import config as _config

from .conftest import _DEVELOPER_HOME, _outside, shielded_child_env

#: What the child prints: each default location as bettermemory resolves it.
_RESOLVE = """
import json
from pathlib import Path
from bettermemory import _daemon_client, config, store_schema
print(json.dumps({
    "home": str(Path.home()),
    "keys": str(store_schema.resolve_keys_dir()),
    "state": str(_daemon_client.resolve_state_dir()),
    "config": str(config.default_config_path()),
    "store": str(config.Config().resolved_directory()),
}))
"""

#: What a child needs to start at all, and nothing that names a location.
_CARRIED = ("PATH", "PYTHONPATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")


def _resolved(env: dict[str, str] | None, cwd: Path) -> dict[str, str]:
    done = subprocess.run(
        [sys.executable, "-c", _RESOLVE],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    resolved: dict[str, str] = json.loads(done.stdout)
    return resolved


def _bases(
    tmp_path_factory: pytest.TempPathFactory, local_tmp_root: Path | None
) -> list[str]:
    base = os.path.realpath(tmp_path_factory.getbasetemp())
    return [base] if local_tmp_root is None else [base, str(local_tmp_root)]


def _outside_scratch(resolved: dict[str, str], bases: list[str]) -> dict[str, str]:
    return {
        name: path
        for name, path in resolved.items()
        if _outside(bases, path) is not None
    }


def test_a_child_given_an_environment_naming_no_location_resolves_in_scratch(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    local_tmp_root: Path | None,
) -> None:
    """The draft's shape: an environment of its own with what Python needs
    and no location. The keys and state directories are the test's own,
    and the home, config and store fall under the test's scratch home."""
    shielded = shielded_child_env()
    env = {name: shielded[name] for name in _CARRIED if name in shielded}
    resolved = _resolved(env, tmp_path)
    assert _outside_scratch(resolved, _bases(tmp_path_factory, local_tmp_root)) == {}
    assert resolved["keys"] == os.environ[_config.KEYS_DIR_ENV]
    assert resolved["state"] == os.environ[_daemon_client.STATE_DIR_ENV]
    assert Path(resolved["store"]) == Path(resolved["home"]) / _config.GLOBAL_DIR_NAME
    # Written up front, so the child's `load_config` creates none and
    # prints nothing, as in process.
    assert Path(resolved["config"]).is_file()


def test_a_variable_the_test_sets_empty_stays_empty(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    local_tmp_root: Path | None,
) -> None:
    """Empty reads as unset, and a test that sets the state directory empty
    means the child to resolve its own: under its home, not the test's."""
    shielded = shielded_child_env()
    env = {name: shielded[name] for name in _CARRIED if name in shielded}
    env[_daemon_client.STATE_DIR_ENV] = ""
    resolved = _resolved(env, tmp_path)
    assert resolved["state"] != os.environ[_daemon_client.STATE_DIR_ENV]
    assert _outside([os.path.realpath(resolved["home"])], resolved["state"]) is None
    assert _outside_scratch(resolved, _bases(tmp_path_factory, local_tmp_root)) == {}


def test_a_child_inheriting_the_environment_resolves_in_scratch(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    local_tmp_root: Path | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child given no environment inherits this process's, whose home is
    the developer's: it counts as none, so the child's home is scratch.
    The inherited environment carries the import shield's `PYTHONPATH`, as
    every other spawn's does, so the child imports bettermemory where the
    venv's editable `.pth` is hidden."""
    monkeypatch.setenv("PYTHONPATH", shielded_child_env()["PYTHONPATH"])
    resolved = _resolved(None, tmp_path)
    assert _outside_scratch(resolved, _bases(tmp_path_factory, local_tmp_root)) == {}


def test_a_home_the_test_names_decides_the_childs_defaults(tmp_path: Path) -> None:
    """A home the test names is the child's, on Windows too, where the
    user's folders follow it only through `WIN_PD_OVERRIDE_*` (ENG-124,
    where the tests' `APPDATA` moved nothing): the config, keys and state
    the child resolves with nothing naming them lie under it, as a
    sandbox's do (bench/toolcost)."""
    home = tmp_path / "named-home"
    home.mkdir()
    shielded = shielded_child_env()
    env = {name: shielded[name] for name in _CARRIED if name in shielded}
    env["HOME"] = str(home)
    resolved = _resolved(env, tmp_path)
    assert Path(resolved["home"]) == home
    inside = [os.path.realpath(home)]
    for name in ("config", "keys", "state", "store"):
        assert _outside(inside, resolved[name]) is None, (name, resolved)


def test_the_developers_own_home_named_explicitly_counts_as_none(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    local_tmp_root: Path | None,
) -> None:
    """An environment that names the developer's home, as one copied from
    the shell would or as `Path.home()` spells it, under either name, is
    given the scratch home in its place."""
    env = shielded_child_env()
    env["HOME"] = env["USERPROFILE"] = _DEVELOPER_HOME + os.sep
    resolved = _resolved(env, tmp_path)
    assert _outside_scratch(resolved, _bases(tmp_path_factory, local_tmp_root)) == {}
