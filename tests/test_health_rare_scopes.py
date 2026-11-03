"""`rare_scopes` in one pass over the scopes (ENG-170, G4).

The bucket compared every singleton scope with every scope, so a store of
2,000 scopes took about 42 s in `memory_admin(health)` and `bettermemory
health`. `health_stats._rare_scopes` finds each singleton's candidates
through a symmetric-deletion index and the name-part index and lets the
predicate decide each: the bucket equals the pairwise check's, and the
predicate's work grows with the store, not with its square. A scope longer
than the index's cap is compared by length instead, since the index costs
the cube of a scope's length (review D4).

The whole-string index still paired dense short names, which share most
of their deletion variants (ENG-192, G5). The index now reads each tail
behind the segments a pair shares, with the keys rule 4 needs at the
tail's length, and leaves out the pairs that share their next segment or
their stem: the densest stores of short names are offered a bounded
number of candidates each, and the bucket still equals the pairwise
check's.
"""

from __future__ import annotations

import itertools
import random
import string
import tracemalloc
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

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
    indexes name, fewer times than there are scopes (50 and 202 here; 55
    and 519 under the whole-string index of ENG-170)."""
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

    def counted(text: str) -> tuple[set[str], set[str]]:
        nonlocal built
        out = real(text)
        built += sum(len(key) for keys in out for key in keys)
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


# ---------------------------------------------------------------------------
# Dense short names (ENG-192, G5)
# ---------------------------------------------------------------------------


def _every_name(
    letters: str, lengths: tuple[int, ...], space: str = "ns:"
) -> Counter[str]:
    """Every name of `lengths` over `letters` behind `space`, once each:
    the densest store of short names those letters make."""
    return Counter(
        space + "".join(name)
        for size in lengths
        for name in itertools.product(letters, repeat=size)
    )


def _counted_predicate(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the predicate's runs; the one-element list holds the count."""
    calls = [0]
    real = health_stats._scope_typo_neighbor

    def counted(scope: str, other: str) -> bool:
        calls[0] += 1
        return real(scope, other)

    monkeypatch.setattr(health_stats, "_scope_typo_neighbor", counted)
    return calls


def test_every_three_letter_name_behind_one_namespace_is_offered_no_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the old docstring named: all 2,197 names of three letters
    out of thirteen behind one namespace. Rule 4 never pairs two short
    tails of one length, and every pair of them shares its namespace, so
    no pair is offered and the bucket is empty, as the pairwise check's
    is. The whole-string index offered them about 2.46 million pairs
    (6.2 s, a 74 MB peak)."""
    calls = _counted_predicate(monkeypatch)
    counts = _every_name(string.ascii_lowercase[:13], (3,))
    assert len(counts) == 2197
    assert health_stats._rare_scopes(counts) == []
    assert calls[0] == 0


def test_a_numbered_family_is_offered_no_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two thousand sibling scopes (``ns:ticket-1000`` to ``ns:ticket-2999``)
    share their stem, which rule 3 exempts, so none is offered another;
    the whole-string index offered them about 1.58 million pairs."""
    calls = _counted_predicate(monkeypatch)
    counts = Counter(f"ns:ticket-{1000 + i}" for i in range(2000))
    assert health_stats._rare_scopes(counts) == []
    assert calls[0] == 0


def _offer_bound(size: int, letters: int) -> int:
    """The most candidates a tail of `size` characters (under 6) is offered
    at one depth over `letters` characters: the tails one insertion
    longer, one deletion shorter and, from 4 up, the tails of its length
    that share one of its one-deletions, one substitution or two edits
    away (`health_stats._rare_scopes`)."""
    same_length = size * size * letters if size >= 4 else 0
    return (size + 1) * letters + size + same_length


@pytest.mark.parametrize(
    ("letters", "lengths"),
    [("abcdef", (3, 4)), ("abcdefg", (4,)), ("abcde", (5,))],
    ids=["3-and-4-of-6", "4-of-7", "5-of-5"],
)
def test_the_densest_short_names_are_offered_a_bounded_number_each(
    monkeypatch: pytest.MonkeyPatch, letters: str, lengths: tuple[int, ...]
) -> None:
    """Every name of the lengths over the letters, behind one namespace:
    each is one edit from many others, so each is in the bucket, and each
    is offered no more candidates than its length and the letters allow,
    however many names the store holds. The whole scopes, behind no
    shared segment, are not indexed: they all share the namespace."""
    offered: dict[int, int] = {}
    real = health_stats._tail_candidates

    def recorded(
        scopes: Sequence[str],
        group: Sequence[health_stats._TailEntry],
        candidates: dict[int, set[int]],
    ) -> list[list[int]]:
        later = real(scopes, group, candidates)
        assert later == []  # no tail is long enough for the two-edit keys
        for entry in group:
            if entry.asks:
                size = len(scopes[entry.n]) - entry.start
                most = len(candidates.get(entry.n, ()))
                offered[size] = max(offered.get(size, 0), most)
        return later

    monkeypatch.setattr(health_stats, "_tail_candidates", recorded)
    counts = _every_name(letters, lengths)
    assert health_stats._rare_scopes(counts) == sorted(counts)
    assert set(offered) == set(lengths)
    for size, most in offered.items():
        assert 0 < most <= _offer_bound(size, len(letters)), (size, most)


def _entries_built(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the entries the index builds, one for each scope it reads at
    one depth; the one-element list holds the count."""
    built = [0]
    real = health_stats._TailEntry

    def counted(*args: Any) -> Any:
        built[0] += 1
        return real(*args)

    monkeypatch.setattr(health_stats, "_TailEntry", counted)
    return built


@pytest.mark.parametrize("leaves", [False, True], ids=["deep", "deep-with-leaves"])
def test_a_deep_store_is_indexed_where_its_scopes_part_only(
    monkeypatch: pytest.MonkeyPatch, leaves: bool
) -> None:
    """150 scopes of 101 segments that part only at the last, ``a:`` a
    hundred times and a name, and with a one-segment leaf off every depth
    besides (``a:z``, ``a:a:z``, ...). A depth whose scopes all go on with
    one segment offers no pair; at one where a leaf parts, the scopes that
    go on pair only with the leaf, and only a tail within two characters
    of its length. So the index builds fewer entries than twice the
    scopes (150 and 349). Read at every depth, as the first form of the
    index did (ENG-192, review F1), they took 15,150 and 20,300, and the
    report 21 times as long as the whole-string index on 1,000 such
    scopes."""
    built = _entries_built(monkeypatch)
    counts = Counter("a:" * 100 + f"{i}x" for i in range(150))
    if leaves:
        counts.update("a:" * k + "z" for k in range(100))
    rare = health_stats._rare_scopes(counts)
    assert built[0] < 2 * len(counts), (built[0], len(counts))
    assert rare == _pairwise(counts)
    assert rare


def test_the_bucket_equals_the_pairwise_check_on_dense_short_names() -> None:
    """Names of one to seven characters over two letters, a digit and a
    hyphen, behind one namespace and bare, three in a hundred of them
    drawn, beside one-edit typos of the namespace: siblings, insertions,
    deletions and substitutions at every length the index treats apart,
    across the two-edit length. The bucket names most of them, not all."""
    rng = random.Random(5)
    every = _every_name("ab1-", (1, 2, 3, 4, 5, 6, 7))
    every += _every_name("ab1", (1, 2, 3, 4), "")
    counts = Counter(scope for scope in sorted(every) if rng.random() < 0.03)
    counts.update(["nss:ab", "n:ab1", "ms:abba"])
    rare = health_stats._rare_scopes(counts)
    assert rare == _pairwise(counts)
    assert 0 < len(rare) < len(counts)


@pytest.mark.parametrize(
    ("pair", "bystanders"),
    [
        (("cc:cbca:b:a", "cc:cca:ba"), ()),
        (
            (
                "p:q:baaaaaabbabbbaaaabbbaabbab:baaabbaa",
                "p:q:baaaaabbabbbaaaabbbaabbabbaaabbaa",
            ),
            (),
        ),
        (
            (
                "a:babbbaaaabaaaabbaaabababbbbbbbbaaa",
                "ababbbaaaabaaabbbaaabababbbbbbbbaaa",
            ),
            ("a:" + "c" * 34, "a:" + "d" * 34),
        ),
    ],
    ids=["heavy-run-two-off", "long-tail-two-short", "one-segment-tail"],
)
def test_the_bucket_names_the_pairs_the_differential_run_shrank_to(
    pair: tuple[str, str], bystanders: tuple[str, ...]
) -> None:
    """Three stores the differential run against the pairwise check shrank
    to (ENG-192, review R2), each the smallest one a one-line slip in the
    index misses its pair on. In the first, the pair's tails behind
    ``cc`` are two characters apart, the farthest the heavy run's members
    are kept from a light entry; kept within one, neither is named. In
    the second, both tails behind ``p:q`` are over the length cap and
    the shorter is two characters short of the longer, the low end of
    the longer's near pools; read from one short, the longer is not
    named. Its tails also cross the heavy filter two apart, so it pins
    that bound beside the first. In the third, one scope is a single segment and the other a
    typo of it behind the namespace ``a``; the single segment's entry
    skips the class of the tails that begin with all of it, and two
    bystanders behind ``a`` of the typo's length make that pool one that
    reads skips. Skipping the class of its first character instead,
    which is ``a``'s, the single segment is not named."""
    counts = Counter([*pair, *bystanders])
    rare = health_stats._rare_scopes(counts)
    assert rare == _pairwise(counts)
    assert rare == sorted(pair)
