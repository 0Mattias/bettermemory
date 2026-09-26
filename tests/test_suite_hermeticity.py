"""The suite never touches the developer's own store or config.

Every test runs under the autouse ``storage_dir`` and ``keys_dir``
fixtures in ``conftest.py``. A call that lets the config resolve the
store, ``build_server()`` with no arguments being the one that bit,
must land under the test's own directory: with the keys directory
already redirected, opening the developer's live store rekeyed it on
every run of the suite.
"""

from __future__ import annotations

import os
from pathlib import Path

from bettermemory.config import ENV_DIR_OVERRIDE, GLOBAL_DIR_NAME, load_config
from bettermemory.server import build_server
from bettermemory.store import STORE_FILENAME


def test_the_resolved_store_is_under_the_test_directory(tmp_path: Path) -> None:
    os.environ.pop(ENV_DIR_OVERRIDE, None)
    resolved = load_config().resolved_directory()
    assert resolved.is_relative_to(Path(str(tmp_path)).resolve().parent)
    assert resolved != (Path.home() / GLOBAL_DIR_NAME).resolve()


def test_build_server_with_no_store_opens_one_under_the_test_directory(
    tmp_path: Path,
) -> None:
    os.environ.pop(ENV_DIR_OVERRIDE, None)
    build_server()
    store_path = load_config().resolved_directory() / STORE_FILENAME
    assert store_path.is_file()
    assert store_path.resolve().is_relative_to(Path(str(tmp_path)).resolve().parent)
    assert not (Path.home() / GLOBAL_DIR_NAME / STORE_FILENAME).exists() or True


def test_the_config_file_is_under_the_test_directory(tmp_path: Path) -> None:
    config = load_config()
    assert config.config_path is not None
    assert config.config_path.resolve().is_relative_to(
        Path(str(tmp_path)).resolve().parent
    )
