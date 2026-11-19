"""The test suite's package.

Importing it names scratch directories for the product's state, its keys
and its default store wherever the environment names none. Test code run
outside the suite (a copy of a test file kept elsewhere, a fixture
generator, a one-off harness) then cannot reach the developer's own
directories: without this, a test that clears the state directory's
records cleared the developer's, and every store it made left its key in
the developer's keys directory. The suite's own fixtures name their
directories per session and per test on top of this (`conftest.py`).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

#: The variables guarded, each with the folder it gets under the scratch
#: root: `_daemon_client.STATE_DIR_ENV`, `config.KEYS_DIR_ENV` and the
#: store's `BETTERMEMORY_DIR`, spelled here so that importing this package
#: imports nothing of the product.
GUARDED = (
    ("BETTERMEMORY_STATE_DIR", "state"),
    ("BETTERMEMORY_KEYS_DIR", "keys"),
    ("BETTERMEMORY_DIR", "store"),
)


def _name_scratch_directories() -> None:
    missing = [(name, leaf) for name, leaf in GUARDED if not os.environ.get(name)]
    if not missing:
        return
    # The real path, as the product resolves a store directory it is given.
    root = os.path.realpath(tempfile.mkdtemp(prefix="bettermemory-outside-suite-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    for name, leaf in missing:
        os.environ[name] = os.path.join(root, leaf)


_name_scratch_directories()
