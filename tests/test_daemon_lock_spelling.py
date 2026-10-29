"""Two spellings of one store share one daemon lock (ENG-93).

The daemon's lock, spawn lock, state file and log are named after a hash
of the store path (`_daemon_client.state_file_for`). It hashed the path as
spelled, so two clients spelling one store apart (another case of a
component on a volume that folds case, a symbolic link to its directory)
looked for two state files and took two locks, and each could start a
daemon on the one store. The names now come from the path's canonical
spelling (`_daemon_client.canonical_store_path`). Pinned here, for a case
spelling where the volume folds case and for a symbolic link everywhere:
the four names are one, the lock one spelling holds is held for the other,
and a state file a daemon wrote under one spelling is read under the
other. A spelling that is already canonical keeps the name it had, so a
daemon of the release before is found under the same files.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from bettermemory import _daemon_client as client
from bettermemory.store import STORE_FILENAME

NAMES: tuple[Callable[[Path, Path | str], Path], ...] = (
    client.state_file_for,
    client.lock_file_for,
    client.spawn_lock_file_for,
    client.log_file_for,
)


def _folds_case(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


def _assert_one_daemon(state_dir: Path, first: Path, second: Path) -> None:
    for name in NAMES:
        assert name(state_dir, first) == name(state_dir, second), name.__name__
    state_dir.mkdir(exist_ok=True)
    held = client.try_lock(client.lock_file_for(state_dir, first))
    assert held is not None
    try:
        assert client.try_lock(client.lock_file_for(state_dir, second)) is None
    finally:
        client.release_lock(held)
    state = {
        "pid": os.getpid(),
        "port": 1,
        "token": "t",
        "version": "9.0.0",
        "store": str(first),
        "started": time.time(),
    }
    written = client.write_state(state_dir, state)
    found = client.read_state(state_dir, second)
    assert found is not None and found["store"] == str(first)
    client.remove_state(state_dir, second, pid=os.getpid())
    assert not written.exists()


@pytest.mark.parametrize("exists", [True, False])
def test_two_case_spellings_share_one_daemon(exists: bool, tmp_path: Path) -> None:
    if not _folds_case(tmp_path):
        pytest.skip(
            "this filesystem keeps case apart: `Proj` and `PROJ` are two "
            "directories here, so two stores, each with its own daemon"
        )
    store_dir = tmp_path / "Proj"
    store_dir.mkdir()
    # A store file that is not there yet keeps its name as spelled, under
    # its directory as the volume holds it: the two spellings below differ
    # in the directory's case only.
    name = STORE_FILENAME if exists else STORE_FILENAME.upper()
    if exists:
        (store_dir / name).touch()
    spelled = tmp_path / "PROJ" / STORE_FILENAME.upper()
    _assert_one_daemon(tmp_path / "state", store_dir / name, spelled)
    assert client.canonical_store_path(str(spelled)) == os.path.join(
        os.path.realpath(store_dir), name
    )


@pytest.mark.parametrize("exists", [True, False])
def test_a_symlinked_spelling_shares_one_daemon(exists: bool, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symbolic links here: {exc}")
    if exists:
        (real / STORE_FILENAME).touch()
    _assert_one_daemon(tmp_path / "state", real / STORE_FILENAME, link / STORE_FILENAME)
    assert client.canonical_store_path(str(link / STORE_FILENAME)) == os.path.join(
        os.path.realpath(real), STORE_FILENAME
    )


def test_a_canonical_spelling_keeps_the_name_it_had(tmp_path: Path) -> None:
    """What a client and a daemon of the release before agree on: a store
    path spelled as the volume holds it hashes as it did."""
    store_dir = Path(os.path.realpath(tmp_path)) / "store"
    store_dir.mkdir()
    path = store_dir / STORE_FILENAME
    path.touch()
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    state_dir = tmp_path / "state"
    assert client.state_file_for(state_dir, path) == state_dir / f"daemon-{digest}.json"
    assert client.state_file_for(state_dir, str(path)) == client.state_file_for(
        state_dir, path
    )
