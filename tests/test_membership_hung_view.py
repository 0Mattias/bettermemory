"""A recorded path that does not answer holds no writer (ENG-156).

A view's liveness and the migration's probe of a recorded root read the
filesystem at paths recorded earlier. Read inside the resolution's (or the
migration's) one ``BEGIN IMMEDIATE`` and without a bound, a hard-mounted
NFS export or a dropped SMB share under one recorded path stalled every
writer of the store for as long as the mount hung. The reads now run
before the transaction, each bounded by `location.PROBE_BUDGET`
(`location.bounded_read`), and enter it as values; a path that does not
answer in time reads ``unreachable``, unknown and never gone.

Each test hangs `os.stat` of one recorded path on an event (the fixture's
stat blocks until released) and checks, from another connection while the
stat is blocked, that the store's write lock can be taken, and that the
write completes while the stat still blocks. The repositories are real,
built with git under ``tmp_path``.

A declared folder is read the same way (review M D2): a folder project's
part declared before 9.0.0rc14 kept its folder as the cwd was spelled,
and the resolution read that spelling as a view records it inside its
transaction. It is read before the transaction now, bounded, and no
filesystem read of a declaration's folder runs while the store's write
lock is held, at a resolution or at the declaration itself (H5).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import (
    location,
    location_git,
    membership,
    membership_declare,
    membership_resolve,
    project_file,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.location import path_liveness, resolve_caller
from bettermemory.machine import open_machine
from bettermemory.membership import Declaration, Membership
from bettermemory.membership_migrate import UNREACHABLE, migrate
from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_membership_security import _git, checkout, neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
#: The bound a read of a recorded path waits (`location.PROBE_BUDGET`),
#: read so the module loads where it is not defined (the fail-before run).
PROBE_BUDGET: float = getattr(location, "PROBE_BUDGET", 0.5)
#: What a bounded read may take past its budget on a loaded machine.
_MARGIN = 3.0


@dataclass
class Hang:
    """`os.stat` of the paths in `paths` blocks until `release` is set;
    `entered` is set at the first such stat."""

    paths: set[str] = field(default_factory=set)
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def add(self, path: Path) -> None:
        self.paths.update({str(path), os.path.realpath(path)})


@pytest.fixture
def hang(monkeypatch: pytest.MonkeyPatch) -> Iterator[Hang]:
    """A stat that hangs on the paths added to it, released at teardown
    so no blocked reader outlives the test."""
    made = Hang()
    real = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in made.paths:
            made.entered.set()
            made.release.wait(60)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    try:
        yield made
    finally:
        made.release.set()


@dataclass
class Watch:
    """What a second connection saw while the stat was blocked."""

    entered: bool = False
    lock_free: bool = False
    error: str = ""
    released_early: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


def watch_lock(db: Path, hung: Hang) -> Watch:
    """From another thread and connection: once the stat blocks, take the
    store's write lock (``BEGIN IMMEDIATE``, two seconds' wait) and let it
    go; then wait for the writer to finish (`Watch.done`) before releasing
    the stat, five seconds at most, so a writer that holds the lock across
    the stat (before ENG-156) finishes and the test fails instead of
    hanging."""
    out = Watch()

    def run() -> None:
        out.entered = hung.entered.wait(20)
        if out.entered:
            conn = sqlite3.connect(str(db), timeout=2.0, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                out.lock_free = True
            except sqlite3.OperationalError as exc:
                out.error = str(exc)
            finally:
                conn.close()
        out.released_early = not out.done.wait(5)
        hung.release.set()

    out.thread = threading.Thread(target=run, name="lock-watch", daemon=True)
    out.thread.start()
    return out


def _timed(call: Callable[[], Any], watch: Watch) -> tuple[Any, float]:
    start = time.monotonic()
    try:
        return call(), time.monotonic() - start
    finally:
        watch.done.set()
        assert watch.thread is not None
        watch.thread.join(10)


def _commit_less(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


@pytest.fixture
def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Store, Config]]:
    """A store under `tmp_path` and its config, git set neutral."""
    neutral_git(tmp_path, monkeypatch)
    made = Store(tmp_path / "store")
    yield made, Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    made.close()


def test_a_view_on_a_hung_path_reads_unreachable_within_the_bound(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """`path_liveness` of a view whose stat hangs answers ``unreachable``
    (unknown, not ``gone``) within `PROBE_BUDGET`, and the next read of
    that path, while the first is still blocked, answers at once."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app")
    res = resolve_caller(store, config, str(repo)).resolution
    assert res.checkout is not None
    model = Membership(store, location.process_machine().id)
    k = model.checkout(res.checkout)
    (view,) = k.views
    assert path_liveness(k, view) == "live"
    hang.add(repo)

    got: list[tuple[str, float]] = []

    def read() -> None:
        for kk in (k, replace(k, token=None)):
            start = time.monotonic()
            got.append((path_liveness(kk, view), time.monotonic() - start))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(PROBE_BUDGET + 2 * _MARGIN)
    blocked = reader.is_alive()
    hang.release.set()
    reader.join(10)
    assert not blocked, "the liveness read waited on the hung stat"
    (first, waited), (second, waited_again) = got
    assert (first, second) == ("unreachable", "unreachable")
    assert waited < PROBE_BUDGET + _MARGIN
    assert waited_again < 0.25


def test_reads_in_turn_go_on_past_a_hung_read(tmp_path: Path) -> None:
    """`bounded_reads` (the model's batch read before its transaction)
    reads in turn on one thread: a read that hangs answers `UNANSWERED`
    after its budget, the reads after it are read on, and a later read of
    the hung path answers at once."""
    gate = threading.Event()
    here = str(tmp_path)

    def hung() -> str:
        gate.wait(60)
        return "late"

    jobs: list[tuple[str, Any, Callable[[], Any]]] = [
        (here + "/a", 1, lambda: "a"),
        (here + "/hung", 1, hung),
        (here + "/b", 1, lambda: "b"),
        (here + "/hung", 2, lambda: "never read"),
    ]
    budget = 0.2
    try:
        start = time.monotonic()
        got = location_git.bounded_reads(jobs, budget)
        elapsed = time.monotonic() - start
    finally:
        gate.set()
    unanswered = location_git.UNANSWERED
    assert got == ["a", unanswered, "b", unanswered]
    assert elapsed < budget + _MARGIN


def test_a_hung_view_does_not_hold_the_write_lock_and_the_write_completes(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """The review's scenario (G4): a commit-less repository's first
    sighting reads the liveness of a commit-less project's view for its
    content candidates (C). That view's stat hangs: the store's write lock
    is free while it blocks, and the resolution writes its checkout within
    the bound while the stat still blocks."""
    store, config = lab
    first = _commit_less(tmp_path / "a" / "notes")
    known = resolve_caller(store, config, str(first)).resolution
    assert known.checkout is not None
    second = _commit_less(tmp_path / "b" / "notes")
    hang.add(first)
    watch = watch_lock(store.path, hang)

    caller, elapsed = _timed(lambda: resolve_caller(store, config, str(second)), watch)

    assert watch.entered, "the resolution never read the recorded view"
    assert watch.lock_free, f"the write lock was held across the stat: {watch.error}"
    assert not watch.released_early, "the resolution waited on the hung stat"
    assert elapsed < PROBE_BUDGET + _MARGIN
    res = caller.resolution
    assert res.checkout is not None and res.checkout != known.checkout
    model = Membership(store, location.process_machine().id)
    assert {v.path for v in model.checkout(res.checkout).views} == {
        os.path.realpath(second)
    }
    assert model.checkout(known.checkout).views, "the hung view was dropped"


def test_the_migration_probes_a_hung_root_without_the_write_lock(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """The migration probes every recorded root (design 8.6): a root whose
    stat hangs is probed before its transaction, the write lock is free
    while it blocks, and the run homes its records with the root read as
    unreachable, unknown and never gone."""
    store, _config = lab
    hung = checkout(tmp_path / "work" / "stalled")
    fine = checkout(tmp_path / "work" / "fine")
    for root in (hung, fine):
        store.write_episode(
            session_id="s1",
            body="episode",
            origin=Origin(cwd=str(root), worktree_root=str(root)),
            now=_NOW,
        )
    identity = open_machine(tmp_path / "state")
    hang.add(hung)
    watch = watch_lock(store.path, hang)

    report, elapsed = _timed(lambda: migrate(store, identity, now=lambda: _NOW), watch)

    assert watch.entered, "the migration never probed the recorded root"
    assert watch.lock_free, f"the write lock was held across the stat: {watch.error}"
    assert not watch.released_early, "the migration waited on the hung stat"
    assert elapsed < PROBE_BUDGET + location.FETCH_BUDGET + 2 * _MARGIN
    roots = {r.path: r for r in report.roots}
    assert roots[str(hung)].state == UNREACHABLE
    assert roots[str(fine)].state == "live"
    assert report.homed == 2


# ---------------------------------------------------------------------------
# A declared folder (review M D2 and H5)
# ---------------------------------------------------------------------------


def _folder_with_a_part(
    tmp_path: Path, store: Store, config: Config
) -> tuple[Path, str, str]:
    """`Writing` a folder project and `Writing/novel` a part of it, declared
    as `project init` below its root declares it (I31), under
    ``tmp_path/real``; then `real` moves to `moved` and a symbolic link
    takes its name, so the folder the declaration stored is a spelling of
    the part's folder through a link, as a row a release before 9.0.0rc14
    wrote from a symlinked cwd is. The part's folder now, the stored folder
    and the part."""
    real = tmp_path / "real"
    outer = real / "Writing"
    inner = outer / "novel"
    inner.mkdir(parents=True)
    (outer / "style.md").write_text("style\n", encoding="utf-8")
    (inner / "ch1.md").write_text("one\n", encoding="utf-8")
    project_file.write(outer, name="writing")
    known = resolve_caller(store, config, str(outer)).resolution
    assert known.checkout is not None and known.project is not None
    model = Membership(store, location.process_machine().id)
    written = project_file.write(inner, name="novel")
    location.forget()
    model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=known.checkout,
            target=known.project,
            file_id=written.id,
            name="novel",
            directory=str(inner),
        )
    )
    part = model.file_binding(written.id)
    assert part is not None and part != known.project
    (stored,) = [
        str(b["dir"])
        for b in (
            json.loads(r.payload) for r in store.membership_log(["project_file_bound"])
        )
        if b.get("file_id") == written.id
    ]
    assert stored == os.path.realpath(inner)
    moved = tmp_path / "moved"
    real.rename(moved)
    try:
        real.symlink_to(moved, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform makes no symbolic link here")
    location.forget()
    return moved / "Writing" / "novel", stored, part


def test_a_declared_folder_is_read_before_the_resolutions_transaction(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M D2: the part's first sight compares the declaration's
    stored folder with the folder it stands in. Stored in another spelling
    (through a link), it is read as a view records it before the
    resolution's transaction, never with the store's write lock held, and
    the part's folder resolves by the declaration's rung."""
    store, config = lab
    here, stored, part = _folder_with_a_part(tmp_path, store, config)
    seen: list[tuple[str, bool]] = []
    spied = location.view_path

    def spy(path: str) -> str:
        seen.append((path, store.in_batch))
        return spied(path)

    monkeypatch.setattr(membership_resolve, "view_path", spy)

    res = resolve_caller(store, config, str(here)).resolution

    assert (res.project, res.rule) == (part, "declared_folder")
    assert (stored, False) in seen, "the stored folder was not read before"
    assert [p for p, held in seen if held] == []


def test_a_declared_folder_on_a_hung_path_holds_no_writer(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M D2: a declaration's stored folder whose read hangs (a
    hung mount under the spelling a release before 9.0.0rc14 stored) is
    read within `PROBE_BUDGET`, the store's write lock is free while it
    blocks, and the folder it names matches nothing."""
    store, config = lab
    here, stored, part = _folder_with_a_part(tmp_path, store, config)
    hung = Hang()
    spied = location.view_path

    def blocking(path: str) -> str:
        if path == stored:
            hung.entered.set()
            hung.release.wait(60)
        return spied(path)

    monkeypatch.setattr(membership_resolve, "view_path", blocking)
    try:
        watch = watch_lock(store.path, hung)
        caller, elapsed = _timed(
            lambda: resolve_caller(store, config, str(here)), watch
        )
    finally:
        hung.release.set()

    assert watch.entered, "the resolution never read the stored folder"
    assert watch.lock_free, f"the write lock was held across the read: {watch.error}"
    assert not watch.released_early, "the resolution waited on the hung read"
    assert elapsed < PROBE_BUDGET + _MARGIN
    assert caller.resolution.rule != "declared_folder"


def test_a_declaration_reads_its_folder_once_before_its_transaction(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M H5: `declare` reads its folder as a view records it before
    its transaction; the checks inside it (`below_root`, a part's
    question) take it as read, so no read of the folder runs with the
    store's write lock held."""
    store, config = lab
    outer = tmp_path / "Writing"
    outer.mkdir()
    project_file.write(outer, name="writing")
    known = resolve_caller(store, config, str(outer)).resolution
    assert known.checkout is not None and known.project is not None
    model = Membership(store, location.process_machine().id)
    seen: list[tuple[str, bool]] = []
    spied = location.view_path

    def spy(path: str) -> str:
        seen.append((path, store.in_batch))
        return spied(path)

    monkeypatch.setattr(membership, "view_path", spy)
    monkeypatch.setattr(membership_declare, "view_path", spy)

    for action in ("init", "init_new"):
        inner = outer / action
        inner.mkdir()
        written = project_file.write(inner, name=action)
        seen.clear()
        model.declare(
            Declaration(
                action,
                "os:tester",
                checkout=known.checkout,
                target=known.project,
                file_id=written.id,
                name=action,
                directory=str(inner),
            )
        )
        assert seen == [(str(inner), False)], action
