"""The bounded reads of recorded paths (ENG-156): a view's liveness, the
migration's probe of a recorded root, a declared folder's stored
spelling, the client's answer to a view question. Each read runs on a
worker thread and is waited on for its budget (`location_git.PROBE_BUDGET`);
a path that does not answer in time (a hard-mounted NFS export, a dropped
SMB share) reads `UNANSWERED`, unknown rather than gone, and while that
read is still in flight every later read of the path answers at once. A
batch of reads (`bounded_reads`) reads past a slow read rather than wait
it out (`READ_HANDOFF`, review M G3), on more threads as reads turn out
slow (`READ_LANES`) and with no more than `READ_SLOTS` reads in the
process not yet stalled, so its hung paths cost a budget for each
`READ_SLOTS` of them, not a budget each, and every read still has its
whole budget. A hung read keeps its thread until its path answers, one
thread a path, as before.

They live apart from `location_git` to keep it under the package's
2,000-line bound (ENG-96). `location_git` imports this module and
re-exports the names its callers read (`bounded_read`, `bounded_reads`,
`UNANSWERED`, `NOT_READ`), so no import path changes; it imports only the
standard library, so the hook client loads it with `location_git`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: How long `bounded_reads` waits on one read before it reads past it on
#: another thread, the slow read left running and waited on for the rest
#: of its budget once the others are read (review M G3). Read in turn,
#: each hung path cost a whole `location_git.PROBE_BUDGET` before the next
#: was read: 30 hung views on one dropped share took 15.26 s, past the
#: prompt hook's 8 s (`_daemon_client.hook_budget_seconds`) and the
#: client's 15 s wait. A hand-off costs this and the timer's lateness, 25
#: to 28 ms a hung path on one thread (macOS, over 30, 200 and 300 hung
#: paths); Windows wakes a timed wait on its 15.6 ms tick, so about 31 ms
#: there. A shorter hand-off saves little where the tick sets the cost and
#: gives more slow healthy reads a thread each, so the lanes (`READ_LANES`)
#: divide the cost instead. A healthy view reads far inside it: 200 local
#: views in 1.5 to 2.1 ms, about 10 µs each (macOS APFS), on one thread.
READ_HANDOFF = 0.02
#: The threads `bounded_reads` reads a batch's paths on at once: one while
#: each read answers within `READ_HANDOFF`, one more at each hand-off, to
#: this many, so 200 healthy views still read on one thread (1.7 ms). On
#: one thread 300 hung paths took 8.68 to 8.80 s, a hand-off each in turn,
#: past the prompt hook's 8 s; on eight, 1.54 s with no slot bound, eight
#: reads handed off in the time of one, so the slots (`READ_SLOTS`), not
#: the hand-offs, bound the batch (macOS).
READ_LANES = 8
#: The reads in flight in the process and not yet stalled (past their
#: budget) beyond which `bounded_reads` starts no read: a later job waits
#: for one to land or stall rather than take a thread, so a slow share
#: takes threads at this many a budget, not one a job at once (unbounded,
#: eight lanes held 160 reads in flight). A stalled read keeps its thread
#: until its path answers, one a path, as before. At `PROBE_BUDGET` this
#: reads 128 hung paths a second: 300 took 2.72 to 2.73 s, 200 took 2.09
#: to 2.10 s and 30 took 0.64 to 0.66 s over five runs, where 32 slots
#: took 5.12 s for 300 and 128 slots 1.72 s (macOS); the slots free on
#: the clock, so Windows' tick costs a tick a wave, not a path. The slots
#: are the process's, shared by every request, so the prompt hook's 8 s
#: holds about 960 hung paths a process, not a request: 8 requests at
#: once, each reading 80 hung paths of its own, took 5.56 to 6.04 s each,
#: and 4 reading 300 each took 9.66 s. Requests on one dead share read
#: the same paths and share their reads, and a healthy read in another
#: request waits for a slot about a budget at most (a median 0.29 s and
#: at most 0.77 s while those 4 × 300 contended).
READ_SLOTS = 64


@dataclass
class _Flight:
    """One bounded read in flight: when it started, the longest budget a
    waiter waited out on it (`_give_up`), whether it ended, and its outcome
    (``(True, value)`` or ``(False, exception)``); whether it landed and
    whether the slot count holds it (`_Slots`), both under the board's
    lock."""

    started: float
    overdue: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    outcome: list[tuple[bool, Any]] = field(default_factory=list)
    landed: bool = False
    queued: bool = False


class _Slots:
    """The reads a batch counts against `READ_SLOTS`, oldest first: each
    read boarded, until an admission finds it at the front landed or
    stalled and drops it, with a count of those that landed behind the
    front. A job's admission so costs O(1) amortized, however many stalled
    reads the board holds for an outage: 200 healthy views read in 0.6 ms
    with 1,000 stalled reads on the board, as with none, where a scan of
    the board for each job checked a million reads in 21 ms (macOS)."""

    def __init__(self) -> None:
        self.queue: deque[_Flight] = deque()
        self.landed = 0

    def add(self, flight: _Flight) -> None:
        flight.queued = True
        self.queue.append(flight)

    def land(self, flight: _Flight) -> None:
        flight.landed = True
        if flight.queued:
            self.landed += 1

    def taken(self, now: float, budget: float) -> int:
        """The reads in flight and not stalled at `now` for `budget`, the
        oldest of them, if any, at the front of `queue`."""
        queue = self.queue
        while queue and (queue[0].landed or _stalled(queue[0], now, budget)):
            gone = queue.popleft()
            gone.queued = False
            if gone.landed:
                self.landed -= 1
        return len(queue) - self.landed


_FLIGHTS_LOCK = threading.Lock()
#: Notified as each read lands, for a batch's read waiting for a slot.
_LANDED = threading.Condition(_FLIGHTS_LOCK)
#: The bounded reads in flight, by path and then by what each reads there.
_FLIGHTS: dict[str, dict[Any, _Flight]] = {}
#: The reads in flight a batch's read counts for a slot, under the lock.
_SLOTS = _Slots()
#: What `bounded_read` returns for a read that did not answer within its
#: budget, and `bounded_reads` for one that did not answer or whose path
#: has a read in flight past the budget.
UNANSWERED: Any = object()
#: What `bounded_reads` returns for a read that raised.
NOT_READ: Any = object()


def _stalled(flight: _Flight, now: float, budget: float) -> bool:
    """Whether `flight` is past `budget` at `now`: by the clock, or because
    a waiter waited that budget out on it (`_give_up`)."""
    return flight.overdue >= budget or now - flight.started >= budget


def _board(
    path: str, what: Any, now: float, budget: float, *, slot: bool = False
) -> tuple[_Flight | None, bool]:
    """The flight to wait on for a read of `path` and `what`, and whether
    the caller is to run it: the one in flight, else a new one; None when
    another read of `path` is in flight past `budget` (it is stalled). A
    new read for a batch (`slot`) waits, while `READ_SLOTS` reads in the
    process are in flight and not stalled (`_SLOTS`), for one to land or
    stall."""
    with _FLIGHTS_LOCK:
        while True:
            flights = _FLIGHTS.get(path, {})
            flight = flights.get(what)
            if flight is not None:
                return flight, False
            if any(_stalled(f, now, budget) for f in flights.values()):
                return None, False
            taken = _SLOTS.taken(now, budget)
            if slot and taken >= READ_SLOTS:
                _LANDED.wait(_SLOTS.queue[0].started + budget - now)
                now = time.monotonic()
                continue
            flight = _FLIGHTS.setdefault(path, {})[what] = _Flight(now)
            _SLOTS.add(flight)
            return flight, True


def _give_up(flight: _Flight, budget: float) -> None:
    """A waiter waited `budget` out on `flight`: the board reads the flight
    as past that budget from now on, whatever the clock says. A wait can
    end a hair before the clock shows the budget elapsed (Windows times its
    waits on a coarser timer than its clock), and the next read of the path
    then boarded a read of its own and waited the budget out again."""
    with _FLIGHTS_LOCK:
        flight.overdue = max(flight.overdue, budget)


def _fly(path: str, what: Any, flight: _Flight, read: Callable[[], Any]) -> None:
    """Run `read` for `flight` and land it: its outcome kept, the flight
    off the board, its waiters woken."""
    try:
        flight.outcome.append((True, read()))
    except BaseException as exc:  # noqa: BLE001 - raised in the caller
        flight.outcome.append((False, exc))
    finally:
        with _LANDED:
            _SLOTS.land(flight)
            mine = _FLIGHTS.get(path)
            if mine is not None and mine.get(what) is flight:
                del mine[what]
                if not mine:
                    del _FLIGHTS[path]
            _LANDED.notify_all()
        flight.done.set()


def bounded_read(path: str, what: Any, read: Callable[[], Any], budget: float) -> Any:
    """`read()` of the recorded path `path`, run on a worker thread and
    waited on for `budget` seconds (ENG-156): its value, or `UNANSWERED`
    when it did not answer in time; an exception it raised is raised here.
    A read that outruns the budget keeps running, as a provider's fetch
    does (`_fetch`). One read of a path and `what` is in flight at a time:
    a caller that finds it waits on it within its own budget, and a path
    with a read in flight past the budget answers `UNANSWERED` at once,
    so a hung mount costs one blocked thread and one budget, not one per
    prompt."""
    now = time.monotonic()
    flight, mine = _board(path, what, now, budget)
    if flight is None:
        return UNANSWERED
    if mine:
        threading.Thread(
            target=_fly,
            args=(path, what, flight, read),
            name="bettermemory-probe",
            daemon=True,
        ).start()
    if not flight.done.wait(max(0.0, flight.started + budget - now)):
        _give_up(flight, budget)
        return UNANSWERED
    ok, value = flight.outcome[0]
    if not ok:
        raise value
    return value


def bounded_reads(
    jobs: Sequence[tuple[str, Any, Callable[[], Any]]],
    budget: float,
    *,
    raises: bool = False,
) -> list[Any]:
    """`bounded_read` of each job (path, what, read), each waited on for
    `budget` seconds from its start: each job's value; `UNANSWERED` where
    the read did not answer in time or its path has a read in flight past
    the budget; `NOT_READ` where the read raised, or with `raises` the
    first such exception, in job order, raised once every job is read.

    A path's jobs are read in turn, and the paths in turn on one thread
    rather than one thread a read. A read that has not answered in
    `READ_HANDOFF` is left running, the paths after it read on another
    thread (one more at each hand-off, to `READ_LANES`), and it is waited
    on for the rest of its budget once they are read (review M G3); so is
    a read another thread has in flight for a job, as `bounded_read` waits
    on it, never left for the caller to read again. A job whose path has
    such a read is read after that wait: as the others once the path
    answered, else `UNANSWERED` at once, its path stalled. No read starts
    while `READ_SLOTS` reads in the process are in flight and not stalled.
    So hung paths cost a budget for each `READ_SLOTS` of them, not one
    each, a hung read stays on the board (its path answers every later
    read at once), and each job answers what reading the jobs one after
    another, each to its whole budget, answered."""
    out: list[Any] = [NOT_READ] * len(jobs)
    errors: dict[int, BaseException] = {}
    todo = list(range(len(jobs)))
    while todo:
        lanes = _Lanes(jobs, out, errors, budget)
        lanes.read(todo)
        for i, flight in lanes.slow.items():
            if flight.done.wait(max(0.0, flight.started + budget - time.monotonic())):
                lanes.land(i, flight)
            else:
                _give_up(flight, budget)
                out[i] = UNANSWERED
        todo = lanes.later
    if raises and errors:
        raise errors[min(errors)]
    return out


class _Lanes:
    """One pass of `bounded_reads` over some of its jobs: the threads that
    read them (lanes), the reads they handed off or found in flight
    (`slow`, by job), and the jobs whose path has such a read (`later`)."""

    def __init__(
        self,
        jobs: Sequence[tuple[str, Any, Callable[[], Any]]],
        out: list[Any],
        errors: dict[int, BaseException],
        budget: float,
    ) -> None:
        self.jobs = jobs
        self.out = out
        self.errors = errors
        self.budget = budget
        self.slow: dict[int, _Flight] = {}
        self.later: list[int] = []
        self._cond = threading.Condition(threading.Lock())
        # The paths not yet taken by a lane, each as its jobs in order.
        self._paths: deque[list[int]] = deque()
        # Each running lane's read: its job, its flight and the jobs of its
        # path after it; None between reads. A lane handed off is not here.
        self._reading: dict[int, tuple[int, _Flight, list[int]] | None] = {}

    def land(self, i: int, flight: _Flight) -> None:
        """Keep job `i`'s outcome, read by `flight`, which ended."""
        ok, value = flight.outcome[0]
        if ok:
            self.out[i] = value
        else:
            self.errors[i] = value

    def read(self, todo: list[int]) -> None:
        """Read the jobs `todo` names, handing off each read that has not
        answered in `READ_HANDOFF` (where the budget is shorter, giving it
        up at the budget); return once every lane not handed off ended."""
        by_path: dict[str, list[int]] = {}
        for i in todo:
            by_path.setdefault(self.jobs[i][0], []).append(i)
        self._paths.extend(by_path.values())
        step = min(READ_HANDOFF, self.budget)
        started = handed = 0
        with self._cond:
            while True:
                now = time.monotonic()
                wait = step
                for lane, held in list(self._reading.items()):
                    if held is None:
                        continue
                    i, flight, rest = held
                    left = flight.started + step - now
                    if left > 0:
                        wait = min(wait, left)
                        continue
                    del self._reading[lane]
                    handed += 1
                    self.later.extend(rest)
                    if step < self.budget:
                        self.slow[i] = flight
                    else:
                        _give_up(flight, self.budget)
                        self.out[i] = UNANSWERED
                while self._paths and len(self._reading) < min(READ_LANES, 1 + handed):
                    started += 1
                    self._reading[started] = None
                    threading.Thread(
                        target=self._lane,
                        args=(started, self._paths.popleft()),
                        name="bettermemory-probes",
                        daemon=True,
                    ).start()
                if not self._reading:
                    return
                self._cond.wait(wait)

    def _lane(self, lane: int, jobs: list[int]) -> None:
        """Read `jobs`, one path's, then the paths left, until none is left
        or the caller handed this lane's read off."""
        try:
            while True:
                for k, i in enumerate(jobs):
                    path, what, read = self.jobs[i]
                    flight, mine = _board(
                        path, what, time.monotonic(), self.budget, slot=True
                    )
                    with self._cond:
                        if flight is None:
                            self.out[i] = UNANSWERED
                            continue
                        if not mine:
                            self.slow[i] = flight
                            self.later.extend(jobs[k + 1 :])
                            break
                        self._reading[lane] = (i, flight, jobs[k + 1 :])
                    _fly(path, what, flight, read)
                    with self._cond:
                        if lane not in self._reading:
                            return
                        self._reading[lane] = None
                        self.land(i, flight)
                with self._cond:
                    if not self._paths:
                        return
                    jobs = self._paths.popleft()
        finally:
            with self._cond:
                self._reading.pop(lane, None)
                self._cond.notify()
