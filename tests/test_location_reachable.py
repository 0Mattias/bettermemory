"""Ancestry among witness commits is one bounded traversal (ENG-151).

`Membership._most_specific` asked `is_ancestor` for every ordered pair of
held witnesses, and a container client answered the same matrix over
every held id on every round of the exchange; each pair that is not
related walked the whole history, with no bound, inside the resolution's
write transaction. `location_git.reachable_among` answers the same
question in one traversal from every commit at once, reading the parents
of at most `WALK_LIMIT` commits; the client computes it only when the
daemon's question asks for ancestry.

The histories are synthetic (a reader over a table of parents that counts
its reads and stops a test that reads past a budget), and one is a real
repository read through `RepositoryHistory`.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from bettermemory import location
from bettermemory.location import WALK_LIMIT, capture
from bettermemory.membership import Location, Membership
from bettermemory.store import Store


class _OverBudget(Exception):
    """A traversal read more commits than the test allows."""


class Table:
    """A history as a table of parents, counting `parents` reads; past
    `budget` reads it raises `_OverBudget`."""

    def __init__(
        self, parents: dict[str, tuple[str, ...]], budget: int | None = None
    ) -> None:
        self.table = parents
        self.reads = 0
        self.budget = budget

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

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        from bettermemory.location_git import reachable_among

        return reachable_among(self, commits)

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


def _pairwise(
    table: dict[str, tuple[str, ...]], commits: list[str]
) -> dict[str, set[str]]:
    """The answer of the unbounded walk: for each commit, the others it is
    an ancestor of."""
    reader = Table(table)
    return {
        a: {d for d in commits if d != a and reader.is_ancestor(a, d)} for a in commits
    }


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_below_the_cap_it_answers_as_the_unbounded_walk(seed: int) -> None:
    table = _dag(300, seed)
    rng = random.Random(seed)
    commits = [_sha(i) for i in sorted(rng.sample(range(1, 301), 24))]
    reader = Table(table)

    got = reader.reachable_among(commits)

    assert {a: set(ds) for a, ds in got.items()} == _pairwise(table, commits)
    assert reader.reads <= len(table)


def test_past_the_cap_it_reads_at_most_walk_limit_commits() -> None:
    """D1's scale: 20,000 commits on one line and 64 witnesses spread over
    it. The traversal reads the parents of at most `WALK_LIMIT` commits,
    finishes well under a second, and every relation it reports is true;
    a relation it did not reach is left out, never invented."""
    table = _line(20_000)
    commits = [_sha(i) for i in range(20_000, 0, -300)][:64]
    reader = Table(table, budget=WALK_LIMIT)

    started = time.perf_counter()
    got = reader.reachable_among(commits)
    elapsed = time.perf_counter() - started

    assert reader.reads <= WALK_LIMIT
    assert elapsed < 1.0
    for a, ds in got.items():
        assert all(int(a, 16) < int(d, 16) for d in ds)


def _most_specific(
    reader: Table, held: dict[str, set[str]], tmp_path: Path
) -> list[str]:
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


def test_past_the_bound_the_most_specific_rule_keeps_both_lines(
    tmp_path: Path,
) -> None:
    """Witnesses spread wider than the bound: the relations the traversal
    did not reach read as none, so both lines stay (the first sight asks)
    and no line is picked on a guess."""
    table = _line(20_000)
    upstream = {_sha(i) for i in range(19_000, 19_800, 40)}
    fork = {_sha(i) for i in range(19_800, 20_001, 40)}
    reader = Table(table, budget=WALK_LIMIT)

    best = _most_specific(reader, {"P1": upstream, "P2": fork}, tmp_path)

    assert best == ["P1", "P2"]
    assert reader.reads <= WALK_LIMIT


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
