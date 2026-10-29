"""A new store whose keys directory is a symbolic link to nothing is
refused in one line (ENG-93).

A keys directory kept on a volume that is not mounted right now is a
symbolic link to nothing. An existing store's owner's open already refuses
it (`log.KeyUnreadableError`, never a rekey); `Store.create` met it at
``mkdir`` and raised `FileExistsError`, a traceback naming the link as a
file that exists. Pinned here: `Store.create`, `open_or_create` and
`Store(...)` raise `store_keys.KeysDirUnreadableError` (a
`KeyUnreadableError`) naming the link and its target, whether the keys
directory is named or read from ``BETTERMEMORY_KEYS_DIR`` and whether the
link is the keys directory or a directory on the way to it; nothing is
written (no store directory, nothing at the link's target, the link as it
was); a command ends in one line with exit 1; and once the volume is back
the store is created with its key there.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from bettermemory import store_keys
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.log import KeyRing, KeyUnreadableError
from bettermemory.store import STORE_FILENAME, Store


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"no symbolic links here: {exc}")


Opener = Callable[[Path, Path | None], object]

_OPENERS: dict[str, Opener] = {
    "create": lambda path, keys: Store.create(path, keys_dir=keys),
    "open_or_create": lambda path, keys: Store.open_or_create(path, keys_dir=keys),
    "Store()": lambda path, keys: Store(path.parent, keys_dir=keys),
}


@pytest.mark.parametrize("named", ["argument", "environment"])
@pytest.mark.parametrize("opener", sorted(_OPENERS))
def test_a_keys_directory_linked_to_nothing_refuses_the_new_store(
    opener: str, named: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "volume-not-mounted" / "keys"
    keys = tmp_path / "keys"
    _link(keys, volume)
    directory = tmp_path / "store"
    path = directory / STORE_FILENAME
    if named == "environment":
        monkeypatch.setenv(KEYS_DIR_ENV, str(keys))
    with pytest.raises(KeyUnreadableError) as raised:
        _OPENERS[opener](path, keys if named == "argument" else None)
    exc = raised.value
    assert isinstance(exc, store_keys.KeysDirUnreadableError)
    text = str(exc)
    assert "\n" not in text
    assert text.startswith(f"the keys directory {keys} cannot be used (")
    assert f"a symbolic link to {volume}, which is not there" in text
    assert "no store is created" in text
    assert not directory.exists()
    assert not volume.parent.exists()
    assert keys.is_symlink() and os.readlink(keys) == str(volume)


def test_a_link_to_nothing_on_the_way_to_the_keys_directory_is_named(
    tmp_path: Path,
) -> None:
    volume = tmp_path / "volume-not-mounted"
    config = tmp_path / "config"
    _link(config, volume)
    keys = config / "bettermemory" / "keys"
    with pytest.raises(KeyUnreadableError) as raised:
        Store.create(tmp_path / "store" / STORE_FILENAME, keys_dir=keys)
    text = str(raised.value)
    assert text.startswith(f"the keys directory {keys} cannot be used (")
    assert f"{config}, on the way to it, is a symbolic link to {volume}" in text
    assert not (tmp_path / "store").exists()


def test_once_the_volume_is_back_the_store_is_created_with_its_key_there(
    tmp_path: Path,
) -> None:
    volume = tmp_path / "volume" / "keys"
    keys = tmp_path / "keys"
    _link(keys, volume)
    path = tmp_path / "store" / STORE_FILENAME
    with pytest.raises(KeyUnreadableError):
        Store.create(path, keys_dir=keys)
    volume.mkdir(parents=True)
    with Store.create(path, keys_dir=keys) as store:
        assert store.has_key
        store_id = store.store_id
    assert KeyRing(volume, store_id).current_path().is_file()


def test_a_command_ends_in_one_line_with_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`health` opens the store as its owner (`cli_context`), which creates
    it where there is none: one line on stderr, exit 1, no store."""
    from bettermemory.cli import main

    volume = tmp_path / "volume-not-mounted" / "keys"
    keys = tmp_path / "keys"
    _link(keys, volume)
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    monkeypatch.setenv(KEYS_DIR_ENV, str(keys))
    monkeypatch.setattr(sys, "argv", ["bettermemory", "health"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1, captured.err
    assert captured.err.startswith(f"bettermemory: the keys directory {keys} ")
    assert "Traceback" not in captured.err
    assert not (directory / STORE_FILENAME).exists()
