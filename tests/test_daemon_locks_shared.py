"""One way to take a store's daemon locks (cold review L5, ENG-106).

`backup restore` and the commands that change a key ring (`key adopt`,
`key import`, `key rotate`, `log accept-history`) each hold the store's
spawn lock and daemon lock while they work, refusing with a reason that
names `bettermemory down` while a daemon serves the store or is starting
to. They were two copies of one loop, the key commands' written from the
restore's, so a fix to one could miss the other. Both now take
`_daemon_client.daemon_locks`, beside the locks it takes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from bettermemory import _daemon_client, backup
from bettermemory import key_lifecycle as lifecycle
from bettermemory.config import STORE_FILENAME
from bettermemory.store import Store


def _store(directory: Path, keys_dir: Path) -> Path:
    with Store(directory, keys_dir=keys_dir) as store:
        store.write(content="the deploy runs from the main branch", scopes=["infra"])
    return directory / STORE_FILENAME


def test_restore_and_the_key_commands_take_one_daemon_locks(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[tuple[list[Path], str]] = []
    shared = _daemon_client.daemon_locks

    @contextlib.contextmanager
    def recording(
        state_dir: Path, store_files: Sequence[Path], *, command: str
    ) -> Iterator[str | None]:
        taken.append(([Path(p) for p in store_files], command))
        with shared(state_dir, store_files, command=command) as why:
            yield why

    monkeypatch.setattr(_daemon_client, "daemon_locks", recording)
    path = _store(tmp_path / "store", keys_dir)
    bundle = Path(backup.create_backup(path, keys_dir=keys_dir)["bundle"])
    state_dir = tmp_path / "state"
    restored = backup.restore_bundle(
        bundle, path, apply=False, keys_dir=keys_dir, state_dir=state_dir
    )
    rotated = lifecycle.rotate_key(
        path, apply=True, keys_dir=keys_dir, state_dir=state_dir
    )
    assert restored["status"] == "planned", restored["detail"]
    assert rotated["status"] == "done", rotated["detail"]
    assert taken == [([path], "backup restore"), ([path], "key rotate --yes")]


@pytest.mark.parametrize("spawn", [False, True])
def test_a_held_lock_refuses_restore_and_rotate_in_the_same_words(
    tmp_path: Path, keys_dir: Path, spawn: bool
) -> None:
    path = _store(tmp_path / "store", keys_dir)
    bundle = Path(backup.create_backup(path, keys_dir=keys_dir)["bundle"])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = (
        _daemon_client.spawn_lock_file_for(state_dir, path)
        if spawn
        else _daemon_client.lock_file_for(state_dir, path)
    )
    held = _daemon_client.try_lock(lock)
    assert held is not None
    try:
        restored = backup.restore_bundle(
            bundle, path, apply=True, keys_dir=keys_dir, state_dir=state_dir
        )
        rotated = lifecycle.rotate_key(
            path, apply=True, keys_dir=keys_dir, state_dir=state_dir
        )
    finally:
        _daemon_client.release_lock(held)
    what = "is starting" if spawn else "serves"
    for report, command in (
        (restored, "backup restore"),
        (rotated, "key rotate --yes"),
    ):
        assert report["status"] == "refused"
        detail = report["detail"]
        assert detail.startswith(f"a bettermemory daemon {what} "), detail
        assert f"(it holds {lock})" in detail
        assert detail.endswith(
            "close every session using bettermemory (Claude Code, Codex, ...), "
            f"run `bettermemory down`, then run `bettermemory {command}` again"
        ), detail
