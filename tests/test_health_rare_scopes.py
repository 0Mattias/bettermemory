"""`rare_scopes` in one pass over the scopes (ENG-170, G4).

The bucket compared every singleton scope with every scope, so a store of
2,000 scopes took about 42 s in `memory_admin(health)` and `bettermemory
health`. `health_stats._rare_scopes` finds each singleton's candidates
through a symmetric-deletion index and the name-part index and lets the
predicate decide each: the bucket equals the pairwise check's, and the
predicate's work grows with the store, not with its square. A scope longer
than the index's cap is compared by length instead, since the index costs
the cube of a scope's length (review D4).
"""

from __future__ import annotations

import random
import string
import tracemalloc
from collections import Counter
from datetime import datetime, timezone

import pytest

from bettermemory import health_stats
from bettermemory.health import compute_health
from bettermemory.health_stats import _scope_typo_neighbor
from bettermemory.models import Confidence, Memory, Source, generate_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW = datetime(2026, 5, 1, tzinfo=timezone.utc)

#: One scope of every shape the predicate's rules name, and their typos.
SHAPES = [
    "projects:foo",
    "projct:foo",
    "projects:bettermemory",
    "bettermemory",
    "proj:bettermemory",
    "tool",
    "tools",
    "bug",
    "bugs",
    "aoc2023",
    "aoc2024",
    "blog-v2",
    "blog-v3",
    "foo2",
    "vim",
    "git",
    "gpu",
    "cpu",
    "ml",
    "sql",
    "just",
    "rust",
    "a:b",
    "a:b:c",
    "a:b:5",
    "projects:noxiousai",
    "projects:noxious-ai",
    "projects:verylongname",
    "projects:verylongnmae",
    "team:infra:deploy",
    "team:infra:deplyo",
    "team:infra",
    "lang:python",
    "lang:pyhton",
]


def _memory(scope: str) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_CREATED,
        updated=_CREATED,
        scopes=[scope],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="x\n",
    )


def _memories(counts: Counter[str]) -> list[Memory]:
    return [_memory(scope) for scope, n in counts.items() for _ in range(n)]


def _pairwise(counts: Counter[str], exempt: frozenset[str] = frozenset()) -> list[str]:
    """The bucket as the pairwise check computed it before the pass."""
    every = list(counts)
    return sorted(
        scope
        for scope, n in counts.items()
        if n == 1
        and scope not in exempt
        and any(
            other != scope and _scope_typo_neighbor(scope, other) for other in every
        )
    )


def _synthetic(size: int, seed: int) -> Counter[str]:
    """`size` distinct scopes, most of them namespaced, a few repeated and
    one in twenty with a planted one-edit typo beside it."""
    rng = random.Random(seed)
    letters = string.ascii_lowercase
    spaces = ["projects", "lang", "tools", "topic", "team"]
    names: set[str] = set()
    while len(names) < size:
        word = "".join(rng.choice(letters) for _ in range(rng.randint(4, 14)))
        if rng.random() < 0.15:
            word += "-" + "".join(rng.choice(letters) for _ in range(rng.randint(2, 6)))
        if rng.random() < 0.7:
            word = f"{rng.choice(spaces)}:{word}"
        names.add(word)
    ordered = sorted(names)
    counts = Counter(ordered)
    for scope in ordered[: max(5, size // 20)]:
        at = rng.randrange(1, len(scope))
        counts[scope[:at] + rng.choice(letters) + scope[at:]] += 1
    for scope in ordered[-10:]:
        counts[scope] += 2
    return counts


def test_the_bucket_equals_the_pairwise_check_on_every_shape() -> None:
    counts = Counter(SHAPES)
    counts["git"] += 2
    counts["tools"] += 3
    report = compute_health(_memories(counts), [], now=_NOW)
    assert report.rare_scopes == _pairwise(counts)
    assert "projct:foo" in report.rare_scopes
    assert "a:b" in report.rare_scopes


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_bucket_equals_the_pairwise_check_on_a_synthetic_store(seed: int) -> None:
    counts = _synthetic(250, seed)
    exempt = frozenset(sorted(counts)[:3])
    report = compute_health(
        _memories(counts), [], now=_NOW, typo_scope_exceptions=exempt
    )
    assert report.rare_scopes == _pairwise(counts, exempt)
    assert report.rare_scopes


def test_the_predicates_work_grows_with_the_store_not_its_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distance check the predicate runs, counted at 500 and 2,000
    scopes. The pairwise form ran it for nearly every pair, about 250,000
    and 4,000,000 times; the one pass runs it for the candidates the
    indexes name, fewer times than there are scopes (55 and 519 here)."""
    calls = 0
    real = health_stats._edit_distance_within

    def counted(a: str, b: str, max_dist: int) -> bool:
        nonlocal calls
        calls += 1
        return real(a, b, max_dist)

    monkeypatch.setattr(health_stats, "_edit_distance_within", counted)
    for size in (500, 2000):
        calls = 0
        report = compute_health(_memories(_synthetic(size, size)), [], now=_NOW)
        assert report.rare_scopes
        assert 0 < calls < size, (size, calls)


def _letters(rng: random.Random, size: int) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(size))


def _insert(scope: str, rng: random.Random) -> str:
    """`scope` with one letter inserted inside its name: one edit away."""
    at = rng.randrange(len(scope) // 2, len(scope))
    return scope[:at] + rng.choice(string.ascii_lowercase) + scope[at:]


def _long_scopes(long_size: int) -> Counter[str]:
    """Scopes across the index's cap: one `long_size`-character scope and
    its one-edit typo, scopes one under, at and one over the cap with a
    one-insertion typo each (so the pairs fall on both sides of it and
    across it), a scope one deletion short of a long one, and short
    scopes that match nothing."""
    rng = random.Random(long_size)
    cap = health_stats._DELETION_INDEX_MAX_LEN
    scopes: list[str] = []
    big = "projects:" + _letters(rng, long_size - len("projects:"))
    scopes += [big, _insert(big, rng)]
    for size in (cap - 1, cap, cap + 1):
        scope = "topic:" + _letters(rng, size - len("topic:"))
        scopes += [scope, _insert(scope, rng)]
    over = "team:" + _letters(rng, cap + 1 - len("team:"))
    scopes += [over, over[:-1]]
    scopes += ["lang:python", "tools", "projects:noxiousai"]
    return Counter(scopes)


def test_the_bucket_equals_the_pairwise_check_across_the_length_cap() -> None:
    counts = _long_scopes(4000)
    assert sorted(map(len, counts))[-2:] == [4000, 4001]
    rare = health_stats._rare_scopes(counts)
    assert rare == _pairwise(counts)
    # Every planted pair is found, the 4,000-character one included.
    assert len(rare) == len(counts) - 3
    report = compute_health(_memories(counts), [], now=_NOW)
    assert report.rare_scopes == rare


def test_a_long_scope_costs_the_report_little(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 1,009-character scope, which `memory_write` accepts, took the
    report to 2.63 s and 567 MB when every scope went into the deletion
    index (review D4). The work is counted, not timed, so a slow runner
    cannot trip it: the characters the deletion index builds, and the
    traced memory peak of the whole report."""
    counts = _long_scopes(1009)
    built = 0
    real = health_stats._deletion_variants

    def counted(text: str) -> set[str]:
        nonlocal built
        out = real(text)
        built += sum(map(len, out))
        return out

    monkeypatch.setattr(health_stats, "_deletion_variants", counted)
    report = compute_health(_memories(counts), [], now=_NOW)
    assert report.rare_scopes == _pairwise(counts)
    assert built < 1_000_000, built

    monkeypatch.setattr(health_stats, "_deletion_variants", real)
    memories = _memories(counts)
    tracemalloc.start()
    try:
        compute_health(memories, [], now=_NOW)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 50 * 1024 * 1024, peak


def _levenshtein(a: str, b: str) -> int:
    """The full table, for reference."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def test_the_banded_distance_check_answers_as_the_full_table() -> None:
    """The distance check computes only the cells within the threshold of
    the diagonal; every answer matches the full table's, on short strings
    over two letters, where every edit shape occurs."""
    rng = random.Random(7)
    for _ in range(3000):
        a = "".join(rng.choice("ab") for _ in range(rng.randint(0, 9)))
        b = "".join(rng.choice("ab") for _ in range(rng.randint(0, 9)))
        for max_dist in range(4):
            expected = _levenshtein(a, b) <= max_dist
            assert health_stats._edit_distance_within(a, b, max_dist) is expected, (
                a,
                b,
                max_dist,
            )


def _random_store(rng: random.Random) -> Counter[str]:
    """A small store of scopes drawn to collide: short names over few
    letters, shared namespaces, sibling suffixes, lengths around the
    deletion index's cap, and one-edit typos of some of them."""
    cap = health_stats._DELETION_INDEX_MAX_LEN
    spaces = ["", "projects:", "team:infra:", "a:"]
    scopes: list[str] = []
    for _ in range(rng.randint(5, 30)):
        if rng.random() < 0.2:
            size = rng.randint(cap - 3, cap + 3)
        else:
            size = rng.randint(1, 9)
        name = "".join(rng.choice("abcde") for _ in range(size))
        if rng.random() < 0.15:
            name += rng.choice(["2", "-v2", "s"])
        scopes.append(rng.choice(spaces) + name)
    for scope in list(scopes):
        if rng.random() < 0.3 and len(scope) > 1:
            at = rng.randrange(len(scope))
            edit = rng.randrange(3)
            letter = rng.choice("abcde")
            if edit == 0:
                scope = scope[:at] + scope[at + 1 :]
            elif edit == 1:
                scope = scope[:at] + letter + scope[at:]
            else:
                scope = scope[:at] + letter + scope[at + 1 :]
            scopes.append(scope)
    counts = Counter(s for s in scopes if s)
    for scope in rng.sample(sorted(counts), k=min(3, len(counts))):
        counts[scope] += 1
    return counts


def test_the_bucket_equals_the_pairwise_check_on_200_random_stores() -> None:
    """Two hundred small stores drawn to collide, with exemptions: the one
    pass and the pairwise check name the same scopes in every one."""
    rng = random.Random(200)
    flagged = 0
    for case in range(200):
        counts = _random_store(rng)
        exempt = frozenset(rng.sample(sorted(counts), k=min(2, len(counts))))
        expected = _pairwise(counts, exempt)
        assert health_stats._rare_scopes(counts, exempt=exempt) == expected, case
        flagged += len(expected)
    assert flagged > 200, "the stores must exercise the rule, not miss it"
