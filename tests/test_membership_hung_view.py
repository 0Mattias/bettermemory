"""A recorded path that does not answer holds no writer (ENG-156).

A view's liveness and the migration's probe of a recorded root read the
filesystem at paths recorded earlier. Read inside the resolution's (or the
migration's) one ``BEGIN IMMEDIATE`` and without a bound, a hard-mounted
NFS export or a dropped SMB share under one recorded path stalled every
writer of the store for as long as the mount hung. The reads now run
before the transaction, each bounded by `location_git.PROBE_BUDGET`
(`location_git.bounded_read`), and enter it as values; a path that does not
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

A batch of reads reads past a slow one (review M G3,
`location_reads.READ_HANDOFF`): read in turn at a budget each, thirty
views on one dropped share took the client's whole wait; now they take
about one budget, and every view is still read to its own, so what the
resolution decides is what it decided reading them one by one. The
client reads the views it is asked about the same way. Handed off one
at a time, three hundred hung paths still took the prompt hook's whole
budget (review 193c); a batch reads on more threads as its reads turn
out slow (`location_reads.READ_LANES`), with a bounded number of reads
in flight in the process (`location_reads.READ_SLOTS`), and waits on
another request's read of a path rather than leave it to its caller's
transaction.
"""

from __future__ import annotations

import bisect
import functools
import json
import math
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import (
    location,
    location_git,
    location_reads,
    membership,
    membership_declare,
    membership_resolve,
    project_file,
)
from bettermemory._daemon_client import hook_budget_seconds
from bettermemory.config import Config, MembershipConfig, StorageConfig
from bettermemory.location import path_liveness, resolve_caller
from bettermemory.machine import open_machine
from bettermemory.membership import NEW, Declaration, Membership
from bettermemory.membership_migrate import UNREACHABLE, migrate
from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_membership_security import _git, checkout, neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_NOW = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
#: The bound a read of a recorded path waits (`location_git.PROBE_BUDGET`),
#: read so the module loads where it is not defined (the fail-before run).
PROBE_BUDGET: float = getattr(location, "PROBE_BUDGET", 0.5)
#: How long a batch of reads waits on one before it reads on
#: (`location_reads.READ_HANDOFF`), read the same way.
READ_HANDOFF: float = getattr(location_reads, "READ_HANDOFF", 0.02)
#: The reads in flight and not stalled a batch starts no read beyond
#: (`location_reads.READ_SLOTS`), read the same way.
READ_SLOTS: int = getattr(location_reads, "READ_SLOTS", 64)
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


def watch_lock(db: Path, hung: Hang, *, hold: float = 5.0) -> Watch:
    """From another thread and connection: once the stat blocks, take the
    store's write lock (``BEGIN IMMEDIATE``, two seconds' wait) and let it
    go; then wait for the writer to finish (`Watch.done`) before releasing
    the stat, `hold` seconds at most (five by default), so a writer that
    holds the lock across the stat (before ENG-156) finishes and the test
    fails instead of hanging."""
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
        out.released_early = not out.done.wait(hold)
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


def test_a_read_a_waiter_gave_up_on_answers_the_next_read_at_once_by_no_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path whose read a waiter waited its budget out on answers the
    next `bounded_read` and `bounded_reads` of it `UNANSWERED` at once,
    reading nothing, even while the clock shows less than the budget
    elapsed since that read started: a wait can end a hair before the
    clock does (Windows), and the next read then boarded a read of its
    own and waited the budget out again (the config tip's CI run)."""
    gate = threading.Event()
    path = str(tmp_path / "hung")
    reads: list[str] = []

    def hung() -> str:
        gate.wait(60)
        return "late"

    def read() -> str:
        reads.append("read")
        return "read"

    # The module's clock stands still at the first read's start.
    monkeypatch.setattr(location_reads, "time", SimpleNamespace(monotonic=lambda: 0.0))
    budget = 0.05
    try:
        first = location_git.bounded_read(path, 1, hung, budget)
        start = time.monotonic()
        second = location_git.bounded_read(path, 2, read, budget)
        waited = time.monotonic() - start
        many = location_git.bounded_reads([(path, 3, read)], budget)
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while path in location_reads._FLIGHTS and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first is location_git.UNANSWERED
    assert second is location_git.UNANSWERED
    assert waited < budget, waited
    assert many == [location_git.UNANSWERED]
    assert reads == [], "the next read of the hung path read it again"


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


def test_a_batch_reads_past_hung_reads_and_waits_their_budgets_out_together(
    tmp_path: Path,
) -> None:
    """Review M G3: `bounded_reads` reads past a read that has not answered
    in `READ_HANDOFF`, so five hung reads cost about one budget together,
    not one each. Each hung read answers `UNANSWERED`; a second read of a
    hung path waits for the first's budget and answers `UNANSWERED`
    unread, the path stalled; a read slower than the hand-off that answers
    within its budget answers its value, and a second read of its path is
    read after it. Every read has its whole budget."""
    gate = threading.Event()
    here = str(tmp_path)
    reads: list[str] = []

    def hung() -> str:
        gate.wait(60)
        return "late"

    def reader(name: str, delay: float = 0.0) -> Callable[[], str]:
        def read() -> str:
            time.sleep(delay)
            reads.append(name)
            return name

        return read

    budget = 1.0
    jobs: list[tuple[str, Any, Callable[[], Any]]] = [
        (here + "/a", 1, reader("a")),
        *[(here + f"/hung{n}", 1, hung) for n in range(5)],
        (here + "/slow", 1, reader("slow", 5 * READ_HANDOFF)),
        (here + "/hung0", 2, reader("hung0 again")),
        (here + "/slow", 2, reader("slow again")),
        (here + "/b", 1, reader("b")),
    ]
    try:
        start = time.monotonic()
        got = location_reads.bounded_reads(jobs, budget)
        elapsed = time.monotonic() - start
    finally:
        gate.set()
    hung_paths = {here + f"/hung{n}" for n in range(5)}
    deadline = time.monotonic() + 5
    while location_reads._FLIGHTS.keys() & hung_paths and time.monotonic() < deadline:
        time.sleep(0.01)
    u = location_reads.UNANSWERED
    assert got == ["a", u, u, u, u, u, "slow", u, "slow again", "b"]
    assert "hung0 again" not in reads
    assert elapsed < 3 * budget, f"{elapsed:.2f} s; read in turn, five budgets"


def test_three_hundred_hung_paths_hold_the_prompt_hooks_budget(tmp_path: Path) -> None:
    """Review 193c's first finding: a commit-less first sight reads every
    view of every planned checkout (C), so 300 commit-less projects on a
    dropped share are 300 hung paths in one batch. Handed off one at a
    time, each cost a hand-off and the timer's lateness, about 27 ms on
    macOS, and the 300 took 8.7 s, past the prompt hook's 8 s. A batch now
    reads on more threads as its reads turn out slow and starts no read
    while `READ_SLOTS` reads are in flight and not stalled, so the 300
    cost a budget for each `READ_SLOTS` of them, well inside the hook's
    budget; the healthy path behind them answers; and no window of one
    budget holds more than `READ_SLOTS` starts."""
    gate = threading.Event()
    starts: list[float] = []
    here = str(tmp_path)

    def hung(path: str) -> Callable[[], str]:
        def read() -> str:
            with location_reads._FLIGHTS_LOCK:
                starts.append(location_reads._FLIGHTS[path][1].started)
            gate.wait(60)
            return "late"

        return read

    count = 300
    paths = [f"{here}/share/notes{i}" for i in range(count)]
    jobs: list[tuple[str, Any, Callable[[], Any]]] = [(p, 1, hung(p)) for p in paths]
    jobs.append((here + "/local/scratch", 1, lambda: "live"))
    try:
        start = time.monotonic()
        got = location_reads.bounded_reads(jobs, PROBE_BUDGET)
        elapsed = time.monotonic() - start
    finally:
        gate.set()
    assert got == [location_reads.UNANSWERED] * count + ["live"]
    starts.sort()
    most = max(
        bisect.bisect_left(starts, at + PROBE_BUDGET) - k for k, at in enumerate(starts)
    )
    assert most <= READ_SLOTS, f"{most} reads in flight within one budget"
    bound = math.ceil(count / READ_SLOTS) * PROBE_BUDGET + _MARGIN
    assert elapsed < bound < hook_budget_seconds("prompt"), f"{elapsed:.2f} s"


def test_a_healthy_batch_reads_as_fast_with_an_outages_stalled_reads_on_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 193 pass 2: a stalled read stays on the board for the outage
    (its path answers later reads at once), and a batch counted the reads
    in flight for each job it admitted by a scan of the board under its
    lock, a check of every stalled read for every job: 200 healthy views
    took 21 ms with 1,000 stalled reads on the board against 0.6 ms with
    none. Counted as reads board and land (`location_reads._Slots`), a job
    checks a stalled read once at most, and the batch reads about as fast
    with them as without."""
    here = str(tmp_path)
    jobs: list[tuple[str, Any, Callable[[], Any]]] = [
        (f"{here}/local/v{i}", 1, functools.partial(int, i)) for i in range(200)
    ]
    runs = 5

    def fastest() -> float:
        best = math.inf
        for _ in range(runs):
            start = time.perf_counter()
            got = location_reads.bounded_reads(jobs, PROBE_BUDGET)
            best = min(best, time.perf_counter() - start)
            assert got == list(range(200))
        return best

    empty = fastest()
    stalled = [f"{here}/share/notes{i}" for i in range(1000)]
    long_ago = time.monotonic() - 10 * PROBE_BUDGET
    checks = [0]
    real = location_reads._stalled

    def counted(flight: Any, now: float, budget: float) -> bool:
        checks[0] += 1
        return real(flight, now, budget)

    try:
        for path in stalled:
            assert location_reads._board(path, 1, long_ago, PROBE_BUDGET)[1]
        monkeypatch.setattr(location_reads, "_stalled", counted)
        loaded = fastest()
    finally:
        monkeypatch.undo()
        with location_reads._FLIGHTS_LOCK:
            for path in stalled:
                location_reads._FLIGHTS.pop(path, None)
    assert checks[0] < len(stalled) + 4 * runs * len(jobs), checks[0]
    assert loaded < 3 * empty + 0.005, f"{loaded * 1e3:.1f} ms, {empty * 1e3:.1f} ms"


def test_a_batch_waits_on_another_batchs_read_of_a_path_not_its_caller(
    tmp_path: Path,
) -> None:
    """Review 193c's second finding: two requests' batches at once, the
    second finding the first's reads of two paths in flight, one slow and
    one hung. It answered `NOT_READ` for them, and the caller read each
    again in its transaction, where `bounded_read` waited on the first's
    read with the store's write lock held. The second batch now waits on
    those reads as on slow reads of its own: the slow read answers both
    batches, read once, the hung one answers both `UNANSWERED` within the
    budget, and the second batch's own path is read. The slow read takes
    half a two-second budget, so a starved runner still finds it in
    flight."""
    here = str(tmp_path)
    gate = threading.Event()
    slow_in, hung_in = threading.Event(), threading.Event()
    reads: list[str] = []
    budget = 2.0

    def slow() -> str:
        reads.append("first slow")
        slow_in.set()
        time.sleep(budget / 2)
        return "slow"

    def hung() -> str:
        reads.append("first hung")
        hung_in.set()
        gate.wait(60)
        return "late"

    def reader(name: str) -> Callable[[], str]:
        def read() -> str:
            reads.append(name)
            return name

        return read

    first_jobs: list[tuple[str, Any, Callable[[], Any]]] = [
        (here + "/slow", 1, slow),
        (here + "/hung", 1, hung),
    ]
    second_jobs: list[tuple[str, Any, Callable[[], Any]]] = [
        (here + "/slow", 1, reader("second slow")),
        (here + "/hung", 1, reader("second hung")),
        (here + "/b", 1, reader("b")),
    ]
    first: list[Any] = []
    other = threading.Thread(
        target=lambda: first.extend(location_reads.bounded_reads(first_jobs, budget)),
        daemon=True,
    )
    try:
        other.start()
        assert slow_in.wait(5) and hung_in.wait(5)
        start = time.monotonic()
        second = location_reads.bounded_reads(second_jobs, budget)
        elapsed = time.monotonic() - start
        other.join(5)
    finally:
        gate.set()
    u = location_reads.UNANSWERED
    assert first == ["slow", u]
    assert second == ["slow", u, "b"]
    assert sorted(reads) == ["b", "first hung", "first slow"]
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


def test_thirty_hung_views_cost_one_budget_and_the_live_view_behind_is_read(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """Review M G3 and review 193b's first finding: thirty commit-less
    projects whose views sit on one dropped share (every view's stat
    hangs), one more, ``scratch``, on a local disk, then a second empty
    ``scratch`` elsewhere, whose first sighting reads the 31 views'
    liveness for its content candidates (C), the live one behind the
    thirty hung. Read in turn at a `PROBE_BUDGET` each they took 15 s, the
    client's whole wait (`HOOK_TIMEOUT_SECONDS`); a deadline shared across
    them read the live view unreachable unread, which made its project, an
    empty folder named alike, a content candidate. Read past each other
    (`location_reads.READ_HANDOFF`) they take about one budget, the write
    lock free while the stats block, and every view is read to its own
    budget: the live ``scratch`` is no candidate, so the second is a
    checkout of its own, as with no share hung, and every hung view is
    kept. No token is written, so each checkout keeps its content (C)."""
    store, _config = lab
    config = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        membership=MembershipConfig(write_token=False),
    )
    known: list[str] = []
    for i in range(30):
        repo = _commit_less(tmp_path / "share" / f"notes{i}")
        res = resolve_caller(store, config, str(repo)).resolution
        assert res.checkout is not None
        known.append(res.checkout)
    live = resolve_caller(
        store, config, str(_commit_less(tmp_path / "local" / "a" / "scratch"))
    ).resolution
    assert live.checkout is not None
    for i in range(30):
        hang.add(tmp_path / "share" / f"notes{i}")
    fresh = _commit_less(tmp_path / "local" / "b" / "scratch")
    watch = watch_lock(store.path, hang, hold=len(known) * PROBE_BUDGET + _MARGIN)

    caller, elapsed = _timed(lambda: resolve_caller(store, config, str(fresh)), watch)

    assert watch.entered, "the resolution never read the recorded views"
    assert watch.lock_free, f"the write lock was held across a stat: {watch.error}"
    res = caller.resolution
    assert (res.outcome, res.candidates) == (NEW, ()), res.rule
    assert res.checkout is not None and res.checkout not in {*known, live.checkout}
    model = Membership(store, location.process_machine().id)
    assert {v.path for v in model.checkout(res.checkout).views} == {
        os.path.realpath(fresh)
    }
    assert all(model.checkout(k).views for k in known), "a hung view was dropped"
    assert not watch.released_early, "the resolution waited out every hung stat"
    bound = len(known) * READ_HANDOFF + PROBE_BUDGET + _MARGIN
    assert elapsed < bound < hook_budget_seconds("prompt"), elapsed


def test_a_live_newest_view_behind_nine_hung_views_keeps_its_checkout(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """Review 193b's first finding (B): one repository seen at nine paths
    on a share that now hangs, then at P, then moved to Q, where it lives
    (its newest view); a fresh clone at P, with no token on either. The
    clone holds the history, but the checkout seen at P is live at Q, so
    it is not the re-clone of a checkout gone from P (B) and the clone
    joins as a checkout of its own, as with no share hung. A deadline
    across the eleven views read Q unread as not live and re-keyed the
    checkout to the clone, leaving its view at Q ``replaced``."""
    store, _config = lab
    config = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        membership=MembershipConfig(write_token=False),
    )
    share, local = tmp_path / "share", tmp_path / "local"
    local.mkdir()
    here = Path(checkout(share / "s0"))
    k = resolve_caller(store, config, str(here)).resolution.checkout
    assert k is not None
    for name in [*(f"s{i}" for i in range(1, 9)), "P", "Q"]:
        there = (local if name in ("P", "Q") else share) / name
        os.rename(here, there)
        here = there
        assert resolve_caller(store, config, str(here)).resolution.checkout == k
    for i in range(9):
        hang.add(share / f"s{i}")
    _git(local, "clone", "-q", str(here), "P")

    start = time.monotonic()
    res = resolve_caller(store, config, str(local / "P")).resolution
    elapsed = time.monotonic() - start

    assert res.checkout is not None and res.checkout != k, res.rule
    model = Membership(store, location.process_machine().id)
    after = model.checkout(k)
    (at_q,) = [v for v in after.views if v.path == os.path.realpath(here)]
    assert model.view_state(after, at_q) == membership.LIVE
    assert elapsed < 9 * READ_HANDOFF + PROBE_BUDGET + _MARGIN


def test_the_clients_answer_bounds_each_view_it_reads(
    tmp_path: Path, lab: tuple[Store, Config], hang: Hang
) -> None:
    """Review 193b's third finding: the client answers every view a
    question names in one round (`location.answer`, review M G4), so a
    view on a hung mount it was never asked about before is asked with
    the rest. Each is read bounded, as the daemon reads one: the hung view
    answers ``unreachable`` within `PROBE_BUDGET`, not when the mount
    answers, and the healthy one beside it ``live``."""
    store, config = lab
    model = Membership(store, location.process_machine().id)
    needs: list[location.NeedWitnesses] = []
    paths: dict[str, str] = {}
    for name in ("share", "local"):
        repo = checkout(tmp_path / name / "app")
        res = resolve_caller(store, config, str(repo)).resolution
        assert res.checkout is not None
        k = model.checkout(res.checkout)
        (view,) = k.views
        paths[name] = view.path
        with pytest.raises(location.NeedWitnesses) as need:
            location.AnsweredLiveness({})(k, view)
        needs.append(need.value)
    question = location.NeedWitnesses.joined(needs).question
    assert [v["path"] for v in question["views"]] == [paths["share"], paths["local"]]
    cap = location.capture(paths["local"], write_token=False)
    hang.add(tmp_path / "share" / "app")
    got: list[tuple[dict[str, Any], float]] = []

    def run() -> None:
        start = time.monotonic()
        got.append((location.answer(question, {}, cap), time.monotonic() - start))

    reader = threading.Thread(target=run, daemon=True)
    reader.start()
    reader.join(PROBE_BUDGET + 2 * _MARGIN)
    blocked = reader.is_alive()
    hang.release.set()
    reader.join(10)
    assert not blocked, "the client's answer waited on the hung stat"
    ((answers, waited),) = got
    assert answers["views"] == {paths["share"]: "unreachable", paths["local"]: "live"}
    assert waited < PROBE_BUDGET + _MARGIN


def test_the_clients_answer_reads_a_raising_view_once(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 193c's nit: a view read that raises in the client's answer
    was read in the batch and then read again alone, to raise. It is read
    once, and its exception is raised, as reading the view alone raised
    it."""
    cap = location.capture(str(checkout(tmp_path / "app")), write_token=False)
    gone = str(tmp_path / "gone")
    reads: list[str] = []

    def raising(path: str, *args: Any, **kwargs: Any) -> str:
        reads.append(path)
        raise PermissionError("the view's read raised")

    monkeypatch.setattr(location, "view_state", raising)
    with pytest.raises(PermissionError, match="the view's read raised"):
        location.answer({"views": [{"path": gone, "kind": "primary"}]}, {}, cap)
    assert reads == [gone]


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
