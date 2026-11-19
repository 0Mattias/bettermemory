"""Test code run outside the suite does not reach the developer's own
directories: importing the `tests` package names scratch ones for the
state, the keys and the default store wherever the environment names
none (`tests/__init__.py`). Each case runs in a child process whose
environment drops the three variables, as a harness started from a shell
would, since this process is inside the suite."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tests
from bettermemory import _daemon_client
from bettermemory import config as _config

REPO = Path(__file__).resolve().parents[1]

_RESOLVE = """
import json, os, sys
{first}
from bettermemory import _daemon_client, gitdir_pin, store_schema
from bettermemory.config import Config
print(json.dumps({{
    "state": str(_daemon_client.resolve_state_dir()),
    "pins": str(gitdir_pin.pins_dir()),
    "keys": str(store_schema.resolve_keys_dir()),
    "store": str(Config().resolved_directory()),
    "env": {{n: os.environ.get(n) for n, _ in {guarded!r}}},
}}))
"""


def _resolved(home: Path, first: str, **extra: str) -> dict[str, object]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {name for name, _ in tests.GUARDED}
    }
    # A home of its own, so "the developer's" directories are this test's.
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"))
    env.pop("XDG_STATE_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "src")])
    env.update(extra)
    done = subprocess.run(
        [sys.executable, "-c", _RESOLVE.format(first=first, guarded=tests.GUARDED)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out: dict[str, object] = json.loads(done.stdout)
    return out


def test_the_guarded_names_are_the_products() -> None:
    names = [name for name, _ in tests.GUARDED]
    assert names[0] == _daemon_client.STATE_DIR_ENV
    assert names[1] == _config.KEYS_DIR_ENV
    assert names[2] == "BETTERMEMORY_DIR"


def test_without_the_package_the_defaults_are_the_users_own(tmp_path: Path) -> None:
    """The hazard itself, under a home of this test's: nothing imported
    the `tests` package, so each directory resolves under that home."""
    home = tmp_path / "home"
    home.mkdir()
    got = _resolved(home, "pass")
    real_home = Path(os.path.realpath(home))
    for which in ("state", "pins", "keys", "store"):
        path = Path(os.path.realpath(str(got[which])))
        assert path.is_relative_to(real_home), (which, path)


@pytest.mark.parametrize(
    "first",
    ["import tests", "import tests.contract", "from tests import contract"],
)
def test_importing_the_package_names_scratch_directories(
    tmp_path: Path, first: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    got = _resolved(home, first)
    env = got["env"]
    assert isinstance(env, dict)
    roots = {str(Path(str(v)).parent) for v in env.values()}
    assert len(roots) == 1, env
    root = Path(roots.pop())
    assert root.name.startswith("bettermemory-outside-suite-")
    for which in ("state", "pins", "keys", "store"):
        path = Path(os.path.realpath(str(got[which])))
        assert path.is_relative_to(root), (which, path)
        assert not path.is_relative_to(Path(os.path.realpath(home))), (which, path)
    assert not root.exists(), "the scratch root is removed when the process ends"


def test_a_directory_the_environment_names_is_kept(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    named = tmp_path / "named-keys"
    got = _resolved(home, "import tests", BETTERMEMORY_KEYS_DIR=str(named))
    assert Path(str(got["keys"])) == named
    env = got["env"]
    assert isinstance(env, dict)
    assert env["BETTERMEMORY_KEYS_DIR"] == str(named)
    assert Path(str(got["state"])).parent.name.startswith("bettermemory-outside-suite-")
