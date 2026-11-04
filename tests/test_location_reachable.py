"""Ancestry among witness commits is one bounded traversal (ENG-151).

`Membership._most_specific` asked `is_ancestor` for every ordered pair of
held witnesses, and a container client answered the same matrix over
every held id on every round of the exchange; each pair that is not
related walked the whole history, with no bound, inside the resolution's
write transaction. `location_git.reachable_among` answers the same
question in one traversal from every commit at once, reading the parents
of at most `WALK_LIMIT` commits; the client computes it only when the
daemon's question asks for ancestry.

The traversal spent its bound evenly below every witness, the oldest's
included, so a tie whose witnesses lay unevenly (a project's first
witnesses apart from its newest) asked where the unbounded walk decided
it; it now expands nothing committed before the oldest witness, less the
largest rise of a parent over a child it has seen, until all above it is
read (ENG-203), and a time, which any committer can write and any clock
can skew, only says where the bound is spent: a relation is still one it
read. Past the bound, `RepositoryHistory.independent` asks git, outside
the resolution's transaction, held to the parent lines the reader reads;
the traversal is spread once for each growth and bounded in the parent
edges it handles; and its answer says whether a bound cut it short, as
the client's does (ENG-203).

The histories are synthetic (a reader over a table of parents and
committer times that counts its reads and stops a test that reads past a
budget), and the rest are real repositories read through
`RepositoryHistory`.
"""

from __future__ import annotations

import hashlib
import random
import shutil
import struct
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import location, location_git, membership_records
from bettermemory.location import WALK_LIMIT, capture
from bettermemory.location_git import (
    EDGE_LIMIT,
    INDEPENDENT_MAX,
    Reach,
    RepositoryHistory,
    _committed_at,
    reachable_among,
)
from bettermemory.membership import Location, Membership
from bettermemory.store import Store


class _OverBudget(Exception):
    """A traversal read more commits than the test allows."""


class Table:
    """A history as a table of parents, counting `parents` reads; past
    `budget` reads it raises `_OverBudget`. `times` are the committer
    times the traversal orders by; a commit not in it reads as time 0, so
    a table without times is read breadth first, as ENG-151 read it."""

    def __init__(
        self,
        parents: dict[str, tuple[str, ...]],
        budget: int | None = None,
        times: dict[str, int] | None = None,
    ) -> None:
        self.table = parents
        self.reads = 0
        self.budget = budget
        self.times = times or {}

    def committed(self, commit: str) -> int:
        return self.times.get(commit.lower(), 0)

    def parents(self, commit: str) -> Sequence[str]:
        self.reads += 1
        if self.budget is not None and self.reads > self.budget:
            raise _OverBudget(self.reads)
        return self.table.get(commit.lower(), ())

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """The unbounded pairwise walk the model asked before ENG-151."""
        todo, seen = [descendant], {descendant}
        while todo:
            for p in self.parents(todo.pop()):
                if p == ancestor:
                    return True
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        return ancestor == descendant

    def reachable_among(
        self, commits: Sequence[str], *, limit: int = WALK_LIMIT
    ) -> Reach:
        return reachable_among(self, commits, limit=limit, committed=self.committed)

    def holds(self, commits: Sequence[str]) -> set[str]:
        return {c for c in commits if c in self.table}

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        return self.holds(ids)

    def fingerprint(self, commit: str) -> str | None:
        return None

    def blobs(self, commit: str) -> Sequence[str]:
        return ()


def _sha(i: int) -> str:
    return f"{i:040x}"


def _line(n: int) -> dict[str, tuple[str, ...]]:
    """A linear history of `n` commits, the first a root."""
    return {_sha(i): ((_sha(i - 1),) if i > 1 else ()) for i in range(1, n + 1)}


def _dag(n: int, seed: int) -> dict[str, tuple[str, ...]]:
    """`n` commits, each with one or two parents among the older ones."""
    rng = random.Random(seed)
    out: dict[str, tuple[str, ...]] = {_sha(1): ()}
    for i in range(2, n + 1):
        first = rng.randrange(max(1, i - 6), i)
        parents = {first}
        if rng.random() < 0.25:
            parents.add(rng.randrange(1, i))
        out[_sha(i)] = tuple(_sha(p) for p in sorted(parents))
    return out


def _times(
    table: dict[str, tuple[str, ...]], how: str | None, seed: int = 0
) -> dict[str, int]:
    """Committer times for the commits of `table`, whose parents are the
    lower numbers: none (each reads time 0), ``honest`` (a minute after the
    parent, as a clock that keeps time writes them), ``reversed`` (a minute
    before the parent, as a forged history may) or ``random`` (any time)."""
    if how is None:
        return {}
    rng = random.Random(seed)
    base = 1_600_000_000
    return {
        c: (
            base + 60 * int(c, 16)
            if how == "honest"
            else base - 60 * int(c, 16)
            if how == "reversed"
            else rng.randrange(0, 2 * base)
        )
        for c in table
    }


def _pairwise(
    table: dict[str, tuple[str, ...]], commits: list[str]
) -> dict[str, set[str]]:
    """The answer of the unbounded walk: for each commit, the others it is
    an ancestor of."""
    reader = Table(table)
    return {
        a: {d for d in commits if d != a and reader.is_ancestor(a, d)} for a in commits
    }


@pytest.mark.parametrize("how", [None, "honest", "random"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_below_the_cap_it_answers_as_the_unbounded_walk(
    seed: int, how: str | None
) -> None:
    """Below the cap the traversal reads every commit the given ones
    reach, so it answers as the unbounded walk whatever order the times
    give it (ENG-203)."""
    table = _dag(300, seed)
    rng = random.Random(seed)
    commits = [_sha(i) for i in sorted(rng.sample(range(1, 301), 24))]
    reader = Table(table, times=_times(table, how, seed))

    got = reader.reachable_among(commits)

    assert {a: set(ds) for a, ds in got.items()} == _pairwise(table, commits)
    assert reader.reads <= len(table)


@pytest.mark.parametrize("how", [None, "honest", "reversed", "random"])
def test_past_the_cap_it_reads_at_most_walk_limit_commits(how: str | None) -> None:
    """D1's scale: 20,000 commits on one line and 64 witnesses spread over
    it. The traversal reads the parents of at most `WALK_LIMIT` commits,
    finishes well under a second, and every relation it reports is true;
    a relation it did not reach is left out, never invented, and a time
    that lies (ENG-203) moves the reading, not the relations."""
    table = _line(20_000)
    commits = [_sha(i) for i in range(20_000, 0, -300)][:64]
    reader = Table(table, budget=WALK_LIMIT, times=_times(table, how))

    started = time.perf_counter()
    got = reader.reachable_among(commits)
    elapsed = time.perf_counter() - started

    assert reader.reads <= WALK_LIMIT
    assert elapsed < 1.0
    for a, ds in got.items():
        assert all(int(a, 16) < int(d, 16) for d in ds)


def _most_specific(reader: Any, held: dict[str, set[str]], tmp_path: Path) -> list[str]:
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "machine")
        loc = Location(machine="machine", cwd="/w/fork", state="repo", history=reader)
        return model._most_specific(loc, held)
    finally:
        store.close()


def test_most_specific_reads_the_bounded_traversal_in_the_transaction(
    tmp_path: Path,
) -> None:
    """A fork and its upstream tied at first sight in a 20,000-commit
    history (D1's daemon side), their witnesses within the newest 300
    commits: the rule that picks the most specific line reads at most
    `WALK_LIMIT` commits, where the pairwise walk read the history down to
    its root for every unrelated pair, and picks the line whose witnesses
    descend from the other's."""
    table = _line(20_000)
    upstream = {_sha(i) for i in range(19_700, 19_860, 20)}
    fork = {_sha(i) for i in range(19_860, 20_001, 20)}
    reader = Table(table, budget=WALK_LIMIT)

    best = _most_specific(reader, {"P1": upstream, "P2": fork}, tmp_path)

    assert best == ["P2"]
    assert reader.reads <= WALK_LIMIT


@pytest.mark.parametrize("how", [None, "honest"])
def test_past_the_bound_the_most_specific_rule_keeps_both_lines(
    tmp_path: Path, how: str | None
) -> None:
    """Witnesses spread wider than the bound: the relations the traversal
    did not reach read as none, so both lines stay (the first sight asks)
    and no line is picked on a guess. Held above the oldest witness's
    time (ENG-203), the traversal still reads 512 commits, too few to join
    witnesses 800 commits apart: the floor spends the bound, it does not
    lift it."""
    table = _line(20_000)
    upstream = {_sha(i) for i in range(19_000, 19_800, 40)}
    fork = {_sha(i) for i in range(19_800, 20_001, 40)}
    reader = Table(table, budget=WALK_LIMIT, times=_times(table, how))

    best = _most_specific(reader, {"P1": upstream, "P2": fork}, tmp_path)

    assert best == ["P1", "P2"]
    assert reader.reads <= WALK_LIMIT


def _uneven_tie() -> dict[str, set[str]]:
    """A fork beside its upstream on one line, tied at 128 witnesses, 64
    a project (`WITNESS_BOUND`), lying as a project's witnesses lie: the
    upstream's 16 first (`KEEP_OLDEST`) together, its 48 newest two apart
    from 165 commits above them, the fork's 64 three apart above those;
    480 commits from the oldest to the newest."""
    upstream = {_sha(i) for i in range(19_520, 19_536)} | {
        _sha(i) for i in range(19_700, 19_796, 2)
    }
    fork = {_sha(i) for i in range(19_810, 20_000, 3)}
    return {"P1": upstream, "P2": fork}


def test_the_floor_decides_a_tie_the_even_split_asked(tmp_path: Path) -> None:
    """ENG-203 on a 20,000-commit line. Shared evenly, the 512 commits
    gave each of the 128 witnesses four below it, so the upstream's first
    witnesses, 165 commits below the rest, read as a line no other
    descends from, and the first sight asked. Breadth first with nothing
    committed before the oldest witness expanded, the traversal reaches
    every witness within its 512 commits and keeps the fork alone, as the
    unbounded walk does: on one line every older commit is an ancestor of
    every newer one, so the unbounded walk keeps the project that holds
    the newest witness. Read by times that lie (each parent dated after
    its child), nothing is held back, the traversal reads as ENG-151's
    did, and the tie asks: never a join."""
    table = _line(20_000)
    held = _uneven_tie()
    assert len(held["P1"]) == len(held["P2"]) == 64
    newest = max(set().union(*held.values()), key=lambda c: int(c, 16))
    unbounded = sorted(p for p, ws in held.items() if newest in ws)
    even = Table(table, budget=WALK_LIMIT)
    ordered = Table(table, budget=WALK_LIMIT, times=_times(table, "honest"))
    lying = Table(table, budget=WALK_LIMIT, times=_times(table, "reversed"))

    assert unbounded == ["P2"]
    assert _most_specific(even, held, tmp_path / "even") == ["P1", "P2"]
    assert _most_specific(ordered, held, tmp_path / "ordered") == unbounded
    assert ordered.reads <= WALK_LIMIT
    assert _most_specific(lying, held, tmp_path / "lying") == ["P1", "P2"]
    assert lying.reads <= WALK_LIMIT


def test_the_floor_keeps_a_tie_the_even_split_decided(tmp_path: Path) -> None:
    """Review of ENG-203's first draft, which read newest first: a young
    upstream's 64 witnesses three apart, the fork's first 16 three apart
    just above them, its newest 48 two apart 400 commits higher. Newest
    first spent the 512 commits above the fork's first witnesses and
    asked, where ENG-151's even split, four commits below each witness,
    related the fork's first to the upstream's newest and joined. Breadth
    first, as the even split reads, with nothing committed before the
    oldest witness expanded, the traversal joins too: it reads what the
    even split read above the oldest witness, and spends below it only
    what is left."""
    table = _line(20_000)
    first = {_sha(i) for i in range(10_192, 10_240, 3)}
    held = {
        "P1": {_sha(i) for i in range(10_000, 10_192, 3)},
        "P2": first | {_sha(i) for i in range(10_640, 10_736, 2)},
    }
    assert len(held["P1"]) == len(held["P2"]) == 64
    even = Table(table, budget=WALK_LIMIT)
    floor = Table(table, budget=WALK_LIMIT, times=_times(table, "honest"))

    assert _most_specific(even, held, tmp_path / "even") == ["P2"]
    assert _most_specific(floor, held, tmp_path / "floor") == ["P2"]
    assert floor.reads <= WALK_LIMIT


def test_the_traversal_says_whether_a_bound_cut_it_short() -> None:
    """The question names the bound only where it stopped the traversal
    (review of ENG-203), so the traversal says so (`Reach.bounded`).
    Witnesses within 100 commits of each other, read whole above the
    oldest's time: not cut, though the second pass below it ends at the
    bound, and every relation the line makes is found. Witnesses 1,000
    commits apart: cut. The same near witnesses with ten parent edges to
    handle: cut. Without times every commit lies above the floor, so a
    history longer than the bound is cut."""
    table = _line(20_000)
    near = [_sha(i) for i in range(19_900, 20_001, 10)]
    far = [_sha(i) for i in range(19_000, 20_001, 200)]
    timed = Table(table, times=_times(table, "honest"))

    whole = timed.reachable_among(near)
    assert not whole.bounded
    assert {a: set(ds) for a, ds in whole.items()} == {
        a: {d for d in near if int(d, 16) > int(a, 16)} for a in near
    }
    assert timed.reachable_among(far).bounded
    assert reachable_among(timed, near, edges=10, committed=timed.committed).bounded
    assert Table(table).reachable_among(near).bounded


def test_a_wide_merge_is_spread_once_for_each_growth() -> None:
    """Review of ENG-203 (since ENG-151): a commit popped again was spread
    to every parent again whether or not its set had grown, inside the
    write transaction; 128 witnesses above 384 octopi of 2,000 parents
    took 4.8 s in 9.0.0rc17. 128 witnesses each holding the same 128
    octopi of 256 parents, read without times: every edge is read once
    and spread once, and the traversal ends within its bounds, uncut."""
    pool = tuple(_sha(1_000_000 + i) for i in range(256))
    octopi = [_sha(500_000 + i) for i in range(128)]
    given = [_sha(100 + i) for i in range(128)]
    table: dict[str, tuple[str, ...]] = dict.fromkeys(octopi, pool)
    table.update(dict.fromkeys(given, tuple(octopi)))
    reader = Table(table, budget=WALK_LIMIT)

    started = time.perf_counter()
    got = reader.reachable_among(given)
    elapsed = time.perf_counter() - started

    assert not got.bounded
    assert got.handled == 2 * (128 * 128 + 128 * 256)
    assert all(not ds for ds in got.values())
    assert elapsed < 1.0


def test_hostile_octopi_stop_at_the_edge_bound() -> None:
    """A commit's set grows once for each witness that reaches it at
    another depth, and an octopus holds as many parents as its object
    holds lines: 24 witnesses reaching 8 octopi of 20,000 parents at
    depths 1 to 24 spread 3.8 million edges. `EDGE_LIMIT` stops the
    traversal within a second, cut short (`Reach.bounded`), past the
    bound by at most the parents of the one commit read last, every
    relation it reports true."""
    pool = tuple(_sha(1_000_000 + i) for i in range(20_000))
    octopi = tuple(_sha(500_000 + i) for i in range(8))
    table: dict[str, tuple[str, ...]] = dict.fromkeys(octopi, pool)
    given: list[str] = []
    for depth in range(1, 25):
        chain = [_sha(depth * 1_000 + k) for k in range(depth + 1)]
        for child, parent in zip(chain, chain[1:], strict=False):
            table[child] = (parent,)
        table[chain[-1]] = octopi
        given.append(chain[0])
    reader = Table(table, budget=WALK_LIMIT)

    started = time.perf_counter()
    got = reader.reachable_among(given)
    elapsed = time.perf_counter() - started

    assert got.bounded
    assert got.handled <= EDGE_LIMIT + len(pool)
    assert all(not ds for ds in got.values())
    assert elapsed < 1.0


def _young_upstream(
    rng: random.Random,
) -> tuple[dict[str, tuple[str, ...]], dict[str, set[str]]]:
    """A young upstream's 64 witnesses and a fork's first 16 above them,
    one to six commits apart, on a 3,000-commit line, the fork's newest 48
    two apart up to 1,200 commits higher (review of ENG-203, round 2)."""
    table = _line(3_000)
    held: dict[str, set[str]] = {"P1": set(), "P2": set()}
    at, space = 1_000, rng.randint(1, 6)
    for i in range(80):
        held["P1" if i < 64 else "P2"].add(_sha(at))
        at += rng.randint(1, space)
    at += rng.randint(0, 1_200)
    held["P2"] |= {_sha(at + 2 * i) for i in range(48)}
    return table, held


def _merges(
    rng: random.Random,
) -> tuple[dict[str, tuple[str, ...]], dict[str, set[str]]]:
    """A line of 800 to 3,000 commits with a merge of a recent commit in
    one of seven, and two projects' 8 to 64 witnesses each among its newest
    200 to 1,200 (review of ENG-203, round 2)."""
    n = rng.randrange(800, 3_000)
    table = _line(n)
    for i in range(51, n + 1):
        if rng.random() < 0.15:
            table[_sha(i)] += (_sha(i - rng.randrange(2, 40)),)
    k = rng.randrange(8, 64)
    at = sorted(rng.sample(range(max(1, n - rng.randrange(200, 1_200)), n + 1), 2 * k))
    cut = rng.randrange(k // 2, 2 * k - k // 2)
    return table, {"P1": {_sha(i) for i in at[:cut]}, "P2": {_sha(i) for i in at[cut:]}}


def _skewed(
    table: dict[str, tuple[str, ...]], rng: random.Random, how: str
) -> dict[str, int]:
    """Committer times as machines with skewed clocks write them: an hour
    apart, each off by up to three hours (``jitter``), or a minute apart
    with one commit in 50 dated a year back (``backdated``)."""
    if how == "jitter":
        return {
            c: 1_600_000_000 + 3_600 * int(c, 16) + rng.randrange(-10_800, 10_800)
            for c in table
        }
    times = _times(table, "honest")
    for c in rng.sample(list(table), max(1, len(table) // 50)):
        times[c] -= 365 * 86_400
    return times


def _kept(reach: dict[str, frozenset[str]], held: dict[str, set[str]]) -> list[str]:
    top = {s for s in set().union(*held.values()) if not reach[s]}
    return sorted(p for p, ws in held.items() if ws & top)


@pytest.mark.parametrize("how", ["jitter", "backdated"])
@pytest.mark.parametrize("family", [_young_upstream, _merges])
def test_a_skewed_clock_keeps_every_tie_breadth_first_decides(
    family: Any, how: str
) -> None:
    """Review of ENG-203, round 2: a floor fixed at the oldest witness held
    back every commit a skewed clock dated below it, so a path through one
    waited for a second pass the bound seldom reached, and ties that
    ENG-151's breadth first decided asked (over 300 ties each, 24 lost to
    three hours' jitter on young upstreams, 50 to one commit in 50
    backdated a year). Where a parent is seen committed after a child that
    reached it, the floor now drops by the largest such rise: on 25 ties of
    each model, every tie breadth first decides (the same traversal given
    no times) is decided alike."""
    rng = random.Random(203)
    for _ in range(25):
        table, held = family(rng)
        times = _skewed(table, rng, how)
        witnesses = sorted(set().union(*held.values()))
        plain = _kept(Table(table).reachable_among(witnesses), held)
        timed = Table(table, budget=WALK_LIMIT, times=times)
        if len(plain) == 1:
            assert _kept(timed.reachable_among(witnesses), held) == plain


def test_a_cut_second_pass_counts_where_the_clock_rose() -> None:
    """Below the floor nothing relates two witnesses while times never
    fall from parent to child, so a second pass the bound cuts short is
    not counted (`Reach.bounded`). Where a parent was seen committed after
    a child, a cut second pass may have left a relation unread, and is
    counted (review of ENG-203, round 2)."""
    table = _line(20_000)
    near = [_sha(i) for i in range(19_900, 20_001, 10)]
    honest = _times(table, "honest")
    risen = {**honest, _sha(19_950): honest[_sha(19_951)] + 3_600}

    assert not Table(table, times=honest).reachable_among(near).bounded
    assert Table(table, times=risen).reachable_among(near).bounded


def test_a_commit_held_back_shows_the_rise_to_a_parent_read_already() -> None:
    """Two commits from machines whose clocks were behind: 10,002 dated two
    hours before the oldest witness, on the only path from the fork's
    10,003 down to it, and 10,004 a year back, whose parent 10,003 is a
    witness, read already. The rise from 10,004 to its parent is seen as
    10,004 is held back, so the floor drops a year and 10,002 is spread,
    and its parent read, in the first pass. Seen only from a commit
    spread, no rise showed: the bound went to the newest witness's line,
    5,000 commits up, the second pass could read no parent of 10,002, and
    the upstream's one witness read as no other's ancestor, where breadth
    first (no times) joined the fork (review of ENG-203, round 2)."""
    table = _line(20_000)
    fork = {_sha(10_003), _sha(10_005), _sha(15_000)}
    held = {"P1": {_sha(10_000)}, "P2": fork}
    times = _times(table, "honest")
    times[_sha(10_002)] = times[_sha(10_000)] - 7_200
    times[_sha(10_004)] -= 365 * 86_400
    witnesses = sorted(set().union(*held.values()))
    timed = Table(table, budget=WALK_LIMIT, times=times)

    assert _kept(Table(table).reachable_among(witnesses), held) == ["P2"]
    assert _kept(timed.reachable_among(witnesses), held) == ["P2"]
    assert timed.reads <= WALK_LIMIT


def test_a_second_witness_set_reads_the_traversal(tmp_path: Path) -> None:
    """Git is read once a resolution, for the witnesses the transaction it
    rolled back asked about; a second set (the store changed between the
    two transactions) reads the bounded traversal and does not roll back
    again (`Membership._independent`, ENG-203)."""
    table = _line(2_000)
    held = {"P1": {_sha(1_900)}, "P2": {_sha(1_950)}}
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "machine")
        loc = Location(
            machine="machine", cwd="/w/fork", state="repo", history=_Asking(table)
        )
        model._ancestry = {(_sha(1), _sha(2)): frozenset({_sha(2)})}
        assert model._most_specific(loc, held) == ["P2"]
    finally:
        store.close()


def test_a_tie_on_one_shared_witness_asks_no_git(tmp_path: Path) -> None:
    """Projects that share their one witness commit tie with nothing to
    order: the rule keeps both without rolling the transaction back for
    git (`Membership._independent`, review of ENG-203, round 2)."""
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "machine")
        loc = Location(
            machine="machine", cwd="/w/fork", state="repo", history=_Asking(_line(9))
        )
        model._ancestry = {}
        shared = {"P1": {_sha(9)}, "P2": {_sha(9)}}
        assert model._most_specific(loc, shared) == ["P1", "P2"]
    finally:
        store.close()


class _Asking(Table):
    """A history that can ask git (`IndependentReader`) but must not be
    asked."""

    def independent(self, commits: Sequence[str]) -> frozenset[str] | None:
        raise AssertionError("git asked")


_TREE = b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
_AUTHOR = b"author A <a> 1 +0000\n"
_C5 = b"committer C <c> 5 +0000"
_TIME_MAX = (1 << 64) - 1


def _body(header: bytes) -> bytes:
    return header + b"\n\nmessage\n"


def _dates() -> list[tuple[str, bytes, int]]:
    """Commit objects and the committer time git 2.54 reads from each
    (``git rev-list --no-walk --timestamp``); ``@@`` stands for a parent's
    id, ``@U@`` for it in capitals (`_filled`)."""
    lines = [
        ("normal", b"committer C <c> 1700000000 +0100", 1_700_000_000),
        ("tab and space", b"committer C <c>\t 42 -0500", 42),
        ("several >", b"committer C <c> <d> 7 +0000", 7),
        ("> in the zone", b"committer C <c> 7 +00>00", 0),
        ("no >", b"committer C c 5 +0000", 0),
        ("negative", b"committer C <c> -5 +0000", _TIME_MAX - 4),
        ("leading +", b"committer C <c> +5 +0000", 0),
        ("zone, no time", b"committer C <c> +0100", 0),
        ("spaces only", b"committer C <c>    ", 0),
        ("nothing after >", b"committer C <c>", 0),
        ("vertical tab", b"committer C <c>\x0b5 +0000", 0),
        ("form feed", b"committer C <c>\x0c5 +0000", 0),
        ("carriage return before", b"committer C <c>\r5 +0000", 5),
        ("carriage return after", b"committer C <c> 5\r", 5),
        ("2**64 - 1", b"committer C <c> 18446744073709551615 +0000", _TIME_MAX),
        ("2**64", b"committer C <c> 18446744073709551616 +0000", _TIME_MAX),
        ("20 nines", b"committer C <c> " + b"9" * 20 + b" +0000", _TIME_MAX),
        ("21 nines", b"committer C <c> " + b"9" * 21 + b" +0000", _TIME_MAX),
        ("5,000 digits", b"committer C <c> " + b"7" * 5_000 + b" +0000", _TIME_MAX),
        ("21 digits", b"committer C <c> " + b"0" * 20 + b"1 +0000", 1),
        ("30 digits", b"committer C <c> " + b"0" * 29 + b"5 +0000", 5),
        ("arabic-indic", "committer C <c> ١٢ +0000".encode(), 0),
        ("fullwidth", "committer C <c> １２ +0000".encode(), 0),
        ("superscript", "committer C <c> ² +0000".encode(), 0),
        ("hex", b"committer C <c> 0x10 +0000", 0),
        ("underscore", b"committer C <c> 1_000 +0000", 1),
        ("letters after", b"committer C <c> 123abc +0000", 123),
        ("minus, space", b"committer C <c> - 5 +0000", 0),
        ("two minuses", b"committer C <c> --5 +0000", 0),
        ("minus alone", b"committer C <c> - +0000", 0),
        ("minus zero", b"committer C <c> -0 +0000", 0),
        ("minus 2**64 - 1", b"committer C <c> -18446744073709551615 +0000", 1),
        ("minus 2**64", b"committer C <c> -18446744073709551616 +0000", _TIME_MAX),
        ("tab, minus", b"committer C <c> \t-5 +0000", _TIME_MAX - 4),
        ("committerX", b"committerX C <c> 5 +0000", 5),
    ]
    parent = b"parent @@\n"
    out = [(name, _body(_TREE + _AUTHOR + line), want) for name, line, want in lines]
    out += [
        ("no author line", _body(_TREE + _C5), 0),
        ("a header between", _body(_TREE + _AUTHOR + b"x-extra y\n" + _C5), 0),
        (
            "committer twice",
            _body(_TREE + _AUTHOR + _C5 + b"\ncommitter D <d> 9 +0000"),
            5,
        ),
        ("committer first", _body(_TREE + _C5 + b"\n" + _AUTHOR[:-1]), 0),
        ("authorship", _body(_TREE + b"authorship A <a> 1 +0000\n" + _C5), 5),
        ("one parent", _body(_TREE + parent + _AUTHOR + _C5), 5),
        ("two parents", _body(_TREE + parent * 2 + _AUTHOR + _C5), 5),
        ("parent in capitals", _body(_TREE + b"parent @U@\n" + _AUTHOR + _C5), 5),
        (
            "tree in capitals",
            _body(_TREE.upper().replace(b"TREE", b"tree") + _AUTHOR + _C5),
            5,
        ),
        ("parent after author", _body(_TREE + _AUTHOR + parent + _C5), 0),
        ("no newline after committer", _TREE + _AUTHOR + _C5, 0),
    ]
    return out


_DATES = _dates()


def _filled(template: bytes, parent: str) -> bytes:
    return template.replace(b"@@", parent.encode()).replace(
        b"@U@", parent.upper().encode()
    )


@pytest.mark.parametrize(
    ("body", "expected"), [pytest.param(b, e, id=n) for n, b, e in _DATES]
)
def test_a_committer_time_is_read_as_git_reads_it(body: bytes, expected: int) -> None:
    """The time the traversal orders by (ENG-203), read as git's
    ``parse_commit_date`` reads it (review of ENG-203): the ``author``
    line right after the tree and parent lines, the ``committer`` line
    right after it, ended by a newline; after its last ``>`` and any
    spaces, tabs and carriage returns, a digit or ``-``; the number as
    ``strtoumax`` reads it, wrapping a ``-`` and saturating at 2**64 - 1,
    never raising (a long number is not read by ``int`` whole). git's
    readings are the live test's below."""
    parent = "0123456789abcdef0123456789abcdef01234567"
    assert _committed_at(_filled(body, parent), 40) == expected


pytestmark_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytestmark_git
def test_the_client_answers_ancestry_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container client's rounds (D1's client side): an answer to a
    question about held ids computes no ancestry; an ancestry question is
    answered by the traversal over the commits it names, as git's own
    ``merge-base --is-ancestor`` answers each pair."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas: list[str] = []
    for i in range(6):
        (repo / "f.txt").write_text(f"{i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "-b", "side", shas[2])
    (repo / "g.txt").write_text("side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "side")
    shas.append(_git(repo, "rev-parse", "HEAD"))
    cap = capture(str(repo), write_token=False, use_cache=False)
    witnesses = [shas[1], shas[3], shas[5], shas[6]]

    held = location.answer({"held": witnesses}, {}, cap)

    assert "ancestry" not in held and "ancestry_of" not in held

    asked = location.answer({"ancestry": witnesses}, held, cap)

    expected = sorted(
        [a, d]
        for a in witnesses
        for d in witnesses
        if a != d
        and subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, d], cwd=repo, check=False
        ).returncode
        == 0
    )
    assert asked["ancestry"] == expected
    assert asked["ancestry_of"] == sorted(witnesses)
    answered = location.AnsweredHistory(asked)
    assert {a: set(ds) for a, ds in answered.reachable_among(witnesses).items()} == {
        a: {d for x, d in expected if x == a} for a in witnesses
    }


def _fast_line(repo: Path, n: int) -> list[str]:
    """A line of `n` commits written into `repo`'s ``main`` by ``git
    fast-import``, each committed a minute after its parent; their ids,
    oldest first."""
    _git(repo, "init", "-q", "-b", "main")
    stream: list[str] = []
    for i in range(1, n + 1):
        message = f"c{i}"
        stream += [
            "commit refs/heads/main",
            f"mark :{i}",
            f"committer Fixture <fixture@example.invalid> {1_600_000_000 + 60 * i}"
            " +0000",
            f"data {len(message)}",
            message,
            *([f"from :{i - 1}"] if i > 1 else []),
            "",
        ]
    marks = repo.parent / "marks"
    subprocess.run(
        ["git", "fast-import", "--quiet", f"--export-marks={marks}"],
        input=("\n".join(stream) + "\n").encode(),
        cwd=repo,
        capture_output=True,
        check=True,
    )
    ids = dict(line.split() for line in marks.read_text().splitlines())
    return [ids[f":{i}"] for i in range(1, n + 1)]


class _Counted(RepositoryHistory):
    """`RepositoryHistory` keeping the commits whose parents it read."""

    def __init__(self, common_dir: str) -> None:
        super().__init__(common_dir)
        self.read: set[str] = set()

    def parents(self, commit: str) -> Sequence[str]:
        self.read.add(commit.lower())
        return super().parents(commit)


class _Untimed(_Counted):
    """The same reader, its traversal given no times: ENG-151's even
    split."""

    def reachable_among(self, commits: Sequence[str]) -> Reach:
        return reachable_among(self, commits)


@pytestmark_git
def test_the_daemon_and_the_client_read_a_tie_above_its_oldest_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-203 read from a repository's own commits: 2,000 commits on one
    line written by git fast-import, a minute apart, and a fork and its
    upstream tied at 64 witnesses spanning 456 commits, the upstream's 16
    first witnesses 125 commits below its next. By the committer times
    `RepositoryHistory` reads with the parents, the daemon reading the
    repository and the client answering a container's ancestry question
    (`location.answer`, read back through `AnsweredHistory`) relate every
    witness as the line does, reading at most `WALK_LIMIT` commits, say
    alike that no bound cut them short (``ancestry_bounded``), and keep
    the fork alone; the same reader without its times shares the 512
    commits evenly, eight below each witness, and keeps both. Witnesses
    1,800 commits apart are cut short on both sides."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 2_000)
    position = {sha: i for i, sha in enumerate(line, 1)}

    def at(*spans: range) -> set[str]:
        return {line[i - 1] for span in spans for i in span}

    held = {
        "P1": at(range(1_500, 1_516), range(1_640, 1_672, 2)),
        "P2": at(range(1_800, 1_956, 5)),
    }
    witnesses = sorted(held["P1"] | held["P2"])
    assert len(witnesses) == 64
    cap = capture(str(repo), write_token=False, use_cache=False)
    assert cap.common_dir is not None
    daemon = _Counted(cap.common_dir)

    asked = location.answer({"ancestry": witnesses}, {}, cap)
    client = location.AnsweredHistory(asked)

    assert asked["ancestry"] == sorted(
        [a, d] for a in witnesses for d in witnesses if position[a] < position[d]
    )
    assert asked["ancestry_bounded"] is False
    ours, theirs = daemon.reachable_among(witnesses), client.reachable_among(witnesses)
    assert ours == theirs
    assert ours.bounded is theirs.bounded is False
    assert len(daemon.read) <= WALK_LIMIT
    assert _most_specific(daemon, held, tmp_path / "daemon") == ["P2"]
    assert _most_specific(client, held, tmp_path / "client") == ["P2"]
    even = _Untimed(cap.common_dir)
    assert _most_specific(even, held, tmp_path / "even") == ["P1", "P2"]
    assert len(even.read) <= WALK_LIMIT
    far = sorted(at(range(100, 116), range(1_900, 1_948)))
    cut = location.answer({"ancestry": far}, {}, cap)
    assert cut["ancestry_bounded"] is True
    assert location.AnsweredHistory(cut).reachable_among(far).bounded
    assert RepositoryHistory(cap.common_dir).reachable_among(far).bounded


def test_a_client_that_does_not_say_reads_as_cut_short() -> None:
    """A hook client before 9.0.0rc18 answers ancestry without
    ``ancestry_bounded``: its traversal could have been cut short, so the
    question names the bound (ENG-203)."""
    a, b = _sha(1), _sha(2)
    answers = {"ancestry": [[a, b]], "ancestry_of": [a, b]}

    got = location.AnsweredHistory(answers).reachable_among([a, b])

    assert got == {a: frozenset({b}), b: frozenset()}
    assert got.bounded
    said = location.AnsweredHistory({**answers, "ancestry_bounded": False})
    assert not said.reachable_among([a, b]).bounded


def _write_object(repo: Path, body: bytes, *, literally: bool = False) -> str:
    done = subprocess.run(
        [
            "git",
            "hash-object",
            "-t",
            "commit",
            "-w",
            *(["--literally"] if literally else []),
            "--stdin",
        ],
        cwd=repo,
        input=body,
        capture_output=True,
        check=True,
    )
    return done.stdout.decode().strip()


@pytestmark_git
def test_git_reads_each_committer_time_as_the_traversal_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table of `test_a_committer_time_is_read_as_git_reads_it`,
    written into a repository by ``hash-object --literally`` and read back
    by ``git rev-list --no-walk --timestamp``: git reads every time as
    `_committed_at` does (review of ENG-203)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    parent = _write_object(repo, _body(_TREE + _AUTHOR + b"committer C <c> 3 +0000"))

    for name, template, expected in _DATES:
        body = _filled(template, parent)
        oid = _write_object(repo, body, literally=True)
        listed = _git(repo, "rev-list", "--no-walk", "--timestamp", oid)
        assert (name, int(listed.split()[0])) == (name, expected)
        assert (name, _committed_at(body, len(oid))) == (name, expected)


def _apart(line: list[str]) -> tuple[dict[str, set[str]], list[str]]:
    """A fork and its upstream on one line, 64 witnesses each, further
    apart than the traversal's bound reads: the upstream's 16 first at
    commits 100 to 115, its 48 newest from 1,000; the fork's 64 from
    1,800. The unbounded walk keeps the fork alone."""
    held = {
        "P1": {line[i - 1] for i in [*range(100, 116), *range(1_000, 1_096, 2)]},
        "P2": {line[i - 1] for i in range(1_800, 1_992, 3)},
    }
    return held, sorted(held["P1"] | held["P2"])


@pytestmark_git
def test_git_orders_a_tie_past_the_bound_outside_the_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-203's headline: a tie whose witnesses lie further apart than
    the 512 commits the traversal reads is not ordered by it, and asks
    at every new clone. ``git merge-base --independent``
    (`RepositoryHistory.independent`) names the one witness no other
    descends from, with no bound. The model asks git for it once, before
    its write transaction (`NeedIndependent`), and keeps the fork."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 2_000)
    held, witnesses = _apart(line)
    common = str(repo / ".git")
    newest = max(witnesses, key=line.index)

    assert RepositoryHistory(common).independent(witnesses) == frozenset({newest})
    assert RepositoryHistory(common).reachable_among(witnesses).bounded
    assert _most_specific(RepositoryHistory(common), held, tmp_path / "walk") == [
        "P1",
        "P2",
    ]
    store = Store(tmp_path / "store")
    try:
        model = Membership(store, "machine")
        loc = Location(
            machine="machine",
            cwd="/w/fork",
            state="repo",
            history=RepositoryHistory(common),
        )
        model._ancestry = {}
        with pytest.raises(membership_records.NeedIndependent) as need:
            model._most_specific(loc, held)
        model._ancestry[need.value.commits] = RepositoryHistory(common).independent(
            need.value.commits
        )
        assert model._most_specific(loc, held) == ["P2"]
    finally:
        store.close()


@pytestmark_git
def test_git_reads_the_parents_the_objects_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RepositoryHistory.independent` holds git to the parent lines its
    own reader reads (ENG-203): a replace ref and a grafts file that cut
    the line below the fork's witnesses would make git's own walk keep the
    upstream too; git is run past both, and keeps the fork alone."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 2_000)
    held, witnesses = _apart(line)
    common = str(repo / ".git")
    newest = max(witnesses, key=line.index)
    cut = line[1_500 - 1]
    root = _write_object(
        repo, _body(_TREE + _AUTHOR + b"committer C <c> 1600090000 +0000")
    )
    _git(repo, "replace", cut, root)
    assert len(_git(repo, "merge-base", "--independent", *witnesses).split()) > 1

    assert RepositoryHistory(common).independent(witnesses) == frozenset({newest})

    _git(repo, "replace", "-d", cut)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(f"{cut}\n")
    assert len(_git(repo, "merge-base", "--independent", *witnesses).split()) > 1

    assert RepositoryHistory(common).independent(witnesses) == frozenset({newest})


@pytestmark_git
def test_where_git_does_not_answer_the_traversal_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RepositoryHistory.independent` answers None, and the bounded
    traversal decides, where git is not on PATH, where it does not answer
    within `INDEPENDENT_SECONDS`, where it exits non-zero (a commit it
    cannot read), and for more than `INDEPENDENT_MAX` commits, which it
    does not hand git (ENG-203)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 2_000)
    _held, witnesses = _apart(line)
    common = str(repo / ".git")
    missing = "f" * 40

    assert RepositoryHistory(common).independent([*witnesses, missing]) is None
    assert RepositoryHistory(common).independent(line[: INDEPENDENT_MAX + 1]) is None
    with monkeypatch.context() as patched:
        patched.setattr(location_git, "INDEPENDENT_SECONDS", 1e-6)
        assert RepositoryHistory(common).independent(witnesses) is None
    empty = tmp_path / "no-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert RepositoryHistory(common).independent(witnesses) is None


def _ordered(
    reader: Any, held: dict[str, set[str]], tmp_path: Path
) -> tuple[list[str], list[tuple[str, ...]]]:
    """The most-specific rule as a resolution outside a caller's transaction
    runs it: where it wants git (`NeedIndependent`), git's answer read
    from `reader` and the rule run again. The projects kept, and the sets
    of witnesses git was asked about."""
    store = Store(tmp_path / "store")
    asked: list[tuple[str, ...]] = []
    try:
        model = Membership(store, "machine")
        loc = Location(machine="machine", cwd="/w/fork", state="repo", history=reader)
        model._ancestry = {}
        try:
            return model._most_specific(loc, held), asked
        except membership_records.NeedIndependent as need:
            asked.append(need.commits)
            model._ancestry[need.commits] = reader.independent(need.commits)
        return model._most_specific(loc, held), asked
    finally:
        store.close()


def _before_rc19(answers: dict[str, Any]) -> dict[str, Any]:
    """`answers` as a hook client before 9.0.0rc19 posts them: nothing said
    of git's reading of tied witnesses."""
    return {k: v for k, v in answers.items() if not k.startswith("independent")}


@pytestmark_git
def test_a_container_client_orders_a_tie_past_the_bound_as_git_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-216: the tie of ENG-203's headline, a fork and its upstream whose
    witnesses lie further apart than the traversal reads, where the
    repository is one the daemon cannot read (a container's). The client
    says it answers git's reading (``independent_ok``); the daemon asks it
    of the tied witnesses once (``independent``), and the client runs the
    call the host runs (`RepositoryHistory.independent`), in its own
    namespace: its answer, read back through `AnsweredHistory`, names the
    witness the host's git names, and the rule keeps the fork as on the
    host. A client before 9.0.0rc19 says nothing, is never asked, and its
    traversal, cut short by the bound, keeps both lines and asks, as
    before."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 2_000)
    held, witnesses = _apart(line)
    cap = capture(str(repo), write_token=False, use_cache=False)
    assert cap.common_dir is not None
    host = RepositoryHistory(cap.common_dir)
    newest = max(witnesses, key=line.index)
    answers = location.answer({"held": witnesses}, {}, cap)
    assert answers["independent_ok"] is True and "independent" not in answers

    with pytest.raises(location.NeedWitnesses) as need:
        location.AnsweredHistory(answers).independent(witnesses)
    assert need.value.question == {"independent": sorted(witnesses)}
    answers = location.answer(need.value.question, answers, cap)
    client = location.AnsweredHistory(answers)

    assert client.independent(witnesses) == host.independent(witnesses)
    assert client.independent(witnesses) == frozenset({newest})
    on_host = _ordered(host, held, tmp_path / "host")
    in_container = _ordered(client, held, tmp_path / "container")
    assert on_host == in_container == (["P2"], [tuple(sorted(witnesses))])

    older = _before_rc19(answers)
    assert location.AnsweredHistory(older).independent(witnesses) is None
    with pytest.raises(location.NeedWitnesses) as need:
        _ordered(location.AnsweredHistory(older), held, tmp_path / "older")
    assert sorted(need.value.question) == ["ancestry"]
    older = _before_rc19(location.answer(need.value.question, older, cap))
    assert older["ancestry_bounded"] is True
    kept, asked = _ordered(location.AnsweredHistory(older), held, tmp_path / "old")
    assert kept == ["P1", "P2"]
    assert asked == [tuple(sorted(witnesses))]


def test_a_client_answer_that_names_no_commit_it_was_asked_about_is_none() -> None:
    """ENG-216: the daemon reads a client's git answer as it reads its own
    git's (`RepositoryHistory.independent`): one naming a commit it was
    not asked about, or none, is no answer, and the traversal decides;
    one set is never asked of more than `INDEPENDENT_MAX` commits, and a
    single commit is its own answer."""
    a, b, c = _sha(1), _sha(2), _sha(3)
    told = {
        "independent_ok": True,
        "independent": [
            {"of": [a, b], "top": [c]},
            {"of": [a, c], "top": []},
            {"of": [b, c], "top": [c]},
            {"of": "garbage"},
        ],
    }
    answered = location.AnsweredHistory(told)

    assert answered.independent([b, a]) is None
    assert answered.independent([c, a]) is None
    assert answered.independent([c, b, b.upper()]) == frozenset({c})
    assert answered.independent([a]) == frozenset({a})
    assert answered.independent([_sha(i) for i in range(INDEPENDENT_MAX + 1)]) is None
    with pytest.raises(location.NeedWitnesses):
        answered.independent([a, b, c])


@pytestmark_git
def test_the_client_runs_git_only_when_a_tie_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-216: the client spawns its ``merge-base --independent`` only for
    the daemon's ``independent`` question, once each time it is asked;
    every other answer says it can (``independent_ok``) and runs nothing
    more, and an answer given in an earlier round is kept beside the next
    one's."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    line = _fast_line(repo, 6)
    cap = capture(str(repo), write_token=False, use_cache=False)
    calls: list[tuple[str, ...]] = []
    real = RepositoryHistory.independent

    def counted(self: RepositoryHistory, commits: Sequence[str]) -> Any:
        calls.append(tuple(commits))
        return real(self, commits)

    monkeypatch.setattr(RepositoryHistory, "independent", counted)
    answers = location.answer({"held": line}, {}, cap)
    answers = location.answer({"ancestry": line[:3], "blobs": [line[0]]}, answers, cap)
    assert calls == []
    assert answers["independent_ok"] is True

    answers = location.answer({"independent": [line[1], line[4]]}, answers, cap)
    answers = location.answer({"independent": [line[0], line[2]]}, answers, cap)
    answers = location.answer({"held": [line[5]]}, answers, cap)

    assert calls == [tuple(sorted([line[1], line[4]])), tuple(sorted(line[0:3:2]))]
    assert answers["independent"] == [
        {"of": sorted([line[1], line[4]]), "top": [line[4]]},
        {"of": sorted([line[0], line[2]]), "top": [line[2]]},
    ]


def _two_roots(repo: Path) -> tuple[str, str]:
    """Two unrelated root commits in a new repository, P and X, X
    committed a hundred seconds after P."""
    _git(repo, "init", "-q")
    p = _write_object(repo, _body(_TREE + _AUTHOR + b"committer C <c> 100 +0000"))
    x = _write_object(repo, _body(_TREE + _AUTHOR + b"committer C <c> 200 +0000"))
    return p, x


def _graph(oids: dict[str, tuple[int, int]], parent: dict[str, str]) -> bytes:
    """A commit-graph file (version 1, SHA-1) over `oids`, {id: (generation,
    time)}, naming `parent`'s first parents, whatever the objects hold."""
    order = sorted(oids)
    none = 0x70000000
    fanout = b"".join(
        struct.pack(">I", sum(1 for o in order if int(o[:2], 16) <= b))
        for b in range(256)
    )
    lookup = b"".join(bytes.fromhex(o) for o in order)
    data = b"".join(
        bytes.fromhex(_TREE[5:45].decode())
        + struct.pack(">II", order.index(parent[o]) if o in parent else none, none)
        + struct.pack(">Q", (oids[o][0] << 34) | oids[o][1])
        for o in order
    )
    chunks = [(b"OIDF", fanout), (b"OIDL", lookup), (b"CDAT", data)]
    head = b"CGPH" + bytes([1, 1, len(chunks), 0])
    at = len(head) + (len(chunks) + 1) * 12
    table = b""
    for name, chunk in chunks:
        table += name + struct.pack(">Q", at)
        at += len(chunk)
    body = head + table + b"\0\0\0\0" + struct.pack(">Q", at)
    body += b"".join(chunk for _, chunk in chunks)
    return body + hashlib.sha1(body).digest()


@pytestmark_git
def test_git_is_run_past_a_commit_graph_naming_parents_no_object_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit-graph file is a cache git trusts: one naming P as the
    first parent of X, two unrelated roots, makes plain ``git merge-base
    --independent`` keep X alone. `RepositoryHistory.independent` pins
    ``core.commitGraph`` off and keeps both, as the traversal reads the
    objects (review of ENG-203, round 2)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    p, x = _two_roots(repo)
    graph = repo / ".git" / "objects" / "info" / "commit-graph"
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_bytes(_graph({p: (1, 100), x: (2, 200)}, {x: p}))
    assert _git(repo, "merge-base", "--independent", x, p).split() == [x]
    common = str(repo / ".git")

    assert RepositoryHistory(common).independent([x, p]) == frozenset({x, p})
    assert not any(RepositoryHistory(common).reachable_among([x, p]).values())


@pytestmark_git
def test_git_output_that_names_other_commits_is_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RepositoryHistory.independent` takes git's answer only where it
    names one or more of the commits it was given, and none else: an
    answer naming another commit, or none, is no answer, and the traversal
    decides (ENG-203)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    p, x = _two_roots(repo)
    common = str(repo / ".git")
    for said in (f"{x}\n{'e' * 40}\n", "\n"):
        done = subprocess.CompletedProcess(["git"], 0, stdout=said, stderr="")
        with monkeypatch.context() as patched:
            patched.setattr(git_runner, "run_git", lambda *a, **k: done)
            assert RepositoryHistory(common).independent([x, p]) is None


@pytestmark_git
def test_the_reader_takes_the_parent_lines_git_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git reads a commit's parents from the ``parent`` lines directly
    after its ``tree`` line; a correctly hashed commit X whose one
    ``parent`` line follows its ``author`` line has none. The reader reads
    it so too (`_parent_lines`), so the traversal, a container's client
    and git keep both of the tied projects whose witnesses are P and X,
    where the reader that took every ``parent`` line joined X's (review of
    ENG-203, round 2)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    p, _x = _two_roots(repo)
    x = _write_object(
        repo,
        _body(_TREE + _AUTHOR + f"parent {p}\n".encode() + _C5),
        literally=True,
    )
    common = str(repo / ".git")
    held = {"P1": {p}, "P2": {x}}

    assert _git(repo, "rev-list", "--parents", "-n1", x).split() == [x]
    assert RepositoryHistory(common).parents(x) == ()
    assert RepositoryHistory(common).independent([p, x]) == frozenset({p, x})
    walked = _most_specific(_Counted(common), held, tmp_path / "walk")
    assert walked == ["P1", "P2"]
