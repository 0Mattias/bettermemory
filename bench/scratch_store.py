"""The one way a bench builds or reopens a store (ENG-106; cold review
L4): `scratch_store` is `Store(root, keys_dir=...)` with the keys
directory always named, so no bench's key ring lands in the user's keys
directory. tests/test_bench_keys.py holds every bench under bench/ to it.

The keys directory is the one the caller names; else
``BETTERMEMORY_KEYS_DIR`` when the bench's scratch environment set it
(`capture/_common.scratch_env`); else ``keys`` beside the store's
directory, in the bench's scratch. The user's own keys directory (where
`bettermemory.log.default_keys_dir` points when nothing names one) is
refused while nothing names another.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bettermemory.store import Store


def scratch_keys_dir(root: Path | str) -> Path:
    """The keys directory of a bench store at ``root`` that names none:
    the scratch environment's, else ``keys`` beside ``root``."""
    from bettermemory.config import KEYS_DIR_ENV

    named = os.environ.get(KEYS_DIR_ENV)
    if named:
        return Path(named)
    return Path(root).parent / "keys"


def scratch_store(root: Path | str, keys_dir: Path | str | None = None) -> Store:
    """`Store(root, keys_dir=...)` with the keys directory named
    (`scratch_keys_dir` when ``keys_dir`` is None); SystemExit when it is
    the user's keys directory, the default one while nothing names another."""
    from bettermemory.config import KEYS_DIR_ENV
    from bettermemory.log import default_keys_dir
    from bettermemory.store import Store

    keys = Path(keys_dir) if keys_dir is not None else scratch_keys_dir(root)
    unnamed = not os.environ.get(KEYS_DIR_ENV)
    if unnamed and os.path.realpath(keys) == os.path.realpath(default_keys_dir()):
        raise SystemExit(
            f"a bench store's keys directory {keys} is the user's keys directory; "
            "give the bench a scratch one"
        )
    return Store(Path(root), keys_dir=keys)


__all__ = ["scratch_keys_dir", "scratch_store"]
