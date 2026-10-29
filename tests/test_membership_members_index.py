"""The members of every project are read in one pass (ENG-159).

`Membership._members` (a project and the projects merged into it) walked
every project per call, and the rules that ask it of every project, the
content candidates of a commit-less first sight (C) and health's record
counts, read it per project, each with its own store reads besides: a
store's projects read squared, inside the resolution's write transaction.
The members index is built in one pass, and those rules read the
witnesses, the checkouts and the homes of every project in one read each.

The stores are real; the repositories are commit-less ones made with git
under ``tmp_path``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location
from bettermemory.config import Config, StorageConfig
from bettermemory.location import capture, resolve_caller, to_location
from bettermemory.membership import INDETERMINATE, Location, Membership
from bettermemory.store import Store

from .test_membership_security import neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    neutral_git(tmp_path, monkeypatch)
    return Config(storage=StorageConfig(directory=str(tmp_path / "unused")))


def _commit_less(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _store_of(root: Path, config: Config, n: int, unresolved: int) -> Store:
    """A store with `n` commit-less projects seen here, and `unresolved`
    provisional projects of writes made where nothing could be read, a
    record each."""
    store = Store(root / "store")
    for i in range(n):
        resolve_caller(store, config, str(_commit_less(root / f"p{i}" / f"proj{i}")))
    model = Membership(store, location.process_machine().id)
    for i in range(unresolved):
        unread = Location(
            machine=model.machine,
            cwd=str(root / f"nowhere{i}"),
            state=INDETERMINATE,
            detail="synthetic",
            session=f"s{i}",
        )
        home = model.unresolved_home(unread, session=f"s{i}")
        store.write(content=f"note {i}", scopes=["tools"], home=home)
    return store


def _reads_of(
    store: Store, root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """The store reads a commit-less first sight's content candidates make
    there, and those of health."""
    model = Membership(store, location.process_machine().id)
    fresh = _commit_less(root / "new" / "fresh")
    loc = to_location(
        capture(str(fresh), write_token=False), machine=model.machine, history=None
    )
    count = [0]
    real = Store.membership_rows

    def counted(self: Store, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        count[0] += 1
        return real(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "membership_rows", counted)
        model._projects = None
        model._content_candidates(loc, None)
        candidates = count[0]
        count[0] = 0
        model.health()
        health = count[0]
    return candidates, health


def test_the_content_candidates_and_health_read_the_store_once_for_all_projects(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit-less first sight's content candidates make the same store
    reads with 3 commit-less projects as with 12, and health the same with
    2 provisional projects of unreadable writes as with 8: one read of the
    witnesses, of the checkouts and of the homes, not one per project."""
    small = _store_of(tmp_path / "small", config, 3, 2)
    large = _store_of(tmp_path / "large", config, 12, 8)
    try:
        few = _reads_of(small, tmp_path / "small", monkeypatch)
        many = _reads_of(large, tmp_path / "large", monkeypatch)
        model = Membership(large, location.process_machine().id)
        unresolved = model.health().unresolved
    finally:
        small.close()
        large.close()
    assert few == many, (few, many)
    assert many[0] <= 4 and many[1] <= 6, many
    assert [u.memories for u in unresolved] == [1] * 8


def test_the_members_index_is_the_members_of_each_project(tmp_path: Path) -> None:
    """The index gives each project the members `_members` names by its
    definition (every project whose merges end in it), chains of merges
    and a merged project's own members included."""
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "M-index")
        with model._call():
            ids = [
                model._mint_project(f"p{i}", reason="test", provisional=None).id
                for i in range(6)
            ]
            model._merge(ids[1], ids[0], evidence="test")
            model._merge(ids[2], ids[1], evidence="test")
            model._merge(ids[4], ids[3], evidence="test")
        model._projects = None
        projects = model._all_projects()
        for pid in [*ids, "P-unknown"]:
            target = model._canon(pid)
            assert model._members(pid) == sorted(
                p for p in projects if model.canonical(p) == target
            )
        assert model._members(ids[2]) == sorted(ids[:3])
        assert model._members(ids[5]) == [ids[5]]
    finally:
        store.close()
