"""The snippet path's caches in `search._query_biased_snippet`.

The anchor scan normalises each raw token of a body's first
`_SNIPPET_SCAN_CHARS` characters with `_expand_kebab(tokenize(token))`,
one uncached `tokenize` per raw token and per returned hit. Two memos
remove that work: the scan of a body, kept per body (`_SNIPPET_TOKENS`),
and the normalised surfaces of a raw token, interned across every body the
snippet path scans (`_TOKEN_SURFACES`). Both memoise pure functions of their
keys, so a snippet served warm is the snippet the uncached code builds:
these tests hold the function to a copy of it taken before the memos, on
every shape the snippet tests in test_search.py pin, warm and cold.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

import bettermemory.search as search_module
from bettermemory import _caches
from bettermemory.models import (
    Confidence,
    Memory,
    Source,
    generate_ulid,
    snippet_for,
    snippet_window,
)
from bettermemory.search import search

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _reference_snippet(body: str, matched: list[str], max_chars: int = 200) -> str:
    """`_query_biased_snippet` as it stood before the memos (ef496de),
    statement for statement: the uncached answer every served snippet must
    equal."""
    text = body.strip()
    if len(text) <= max_chars or not matched:
        return snippet_for(text, max_chars)
    primary_terms = set(matched)
    part_terms = {
        p for tok in matched for p in search_module._kebab_parts(tok)
    } - primary_terms
    scan = text[: search_module._SNIPPET_SCAN_CHARS]
    starts: list[int] = []
    primary: list[int] = []
    secondary: list[int] = []
    for m in search_module._TOKEN_RE.finditer(scan):
        starts.append(m.start())
        surfaces = set(search_module._expand_kebab(search_module.tokenize(m.group())))
        if surfaces & primary_terms:
            primary.append(m.start())
        elif surfaces & part_terms:
            secondary.append(m.start())
    for pattern, alias in search_module._ALIAS_ANCHOR_PATTERNS:
        if alias in primary_terms:
            bucket = primary
        elif alias in part_terms:
            bucket = secondary
        else:
            continue
        for m in pattern.finditer(scan):
            bucket.append(m.start())
            starts.append(m.start())
    anchors = sorted(primary) or sorted(secondary)
    if not anchors:
        return snippet_for(text, max_chars)
    budget = max_chars - 3
    reach = budget - search_module._SNIPPET_LEAD_CHARS
    starts = sorted(set(starts))
    best_at, best_cover = anchors[0], -1
    for i, anchor in enumerate(anchors):
        cover = bisect_left(anchors, anchor + reach) - i
        if cover > best_cover:
            best_cover, best_at = cover, anchor
    if best_at <= search_module._SNIPPET_LEAD_CHARS:
        return snippet_for(text, max_chars)
    lead = best_at - search_module._SNIPPET_LEAD_CHARS
    start = starts[bisect_left(starts, lead)]
    return snippet_window(text, start, max_chars)


def _memory(body: str, scopes: list[str] | None = None) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=NOW,
        updated=NOW,
        scopes=["tools"] if scopes is None else scopes,
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _long(prefix: str, sentence: str, *, filler: int = 40, tail: int = 30) -> str:
    return prefix + "filler " * filler + sentence + " " + ("filler " * tail).strip()


# Bodies and queries covering every shape the snippet tests pin: an anchor
# deep in the body and one in the head window, kebab parts as the second
# tier and a literal compound that must not be dragged to them, a symbol
# alias found only by its own pattern, stemmed plurals, snake_case, dotted
# numerics, CJK bigrams, a leading blank line, a match past the scan cap,
# a scope-only match, and a body whose token and alias anchors coincide.
CASES: list[tuple[str, list[str], str]] = [
    (
        "Kickoff notes. "
        + "filler " * 560
        + "The staging database password rotates every 90 days. "
        + "filler " * 30,
        ["tools"],
        "staging database",
    ),
    (("python " * 200).strip(), ["tools"], "python"),
    (
        _long("Intro. ", "We standardised on Claude Code for the agent loop."),
        ["tools"],
        "claude-code",
    ),
    (
        "Intro paragraph. "
        + "filler " * 20
        + "We standardised on claude-code as the runner. "
        + "filler " * 40
        + "code code code code code review code owners code freeze code "
        + "filler " * 20,
        ["tools"],
        "claude-code",
    ),
    (_long("Intro. ", "The hot path is still C++ under the hood."), ["tools"], "C++"),
    (_long("Intro. ", "C++ C++ and cpp and C++ again, cpp."), ["tools"], "C++ cpp"),
    (
        _long("Intro. ", "Retention policy is 30 days for build logs."),
        ["tools"],
        "policies",
    ),
    (
        _long("Intro. ", "Edited docker_compose.yml on helios."),
        ["tools"],
        "docker-compose",
    ),
    (
        _long("Intro. ", "Postgres 16.3 replaced 15.2 on the box."),
        ["tools"],
        "postgres 16",
    ),
    (
        _long("Intro. ", "東京オフィスは2026年に移転する予定です。"),
        ["tools"],
        "東京 移転",
    ),
    (
        "\n\n   Kickoff notes. "
        + " ".join(f"word{i:03d}" for i in range(40))
        + " vaultwarden rotates monthly.",
        ["tools"],
        "vaultwarden",
    ),
    (
        "Kickoff notes. " + "filler " * 1500 + "vaultwarden rotates monthly.",
        ["tools"],
        "vaultwarden",
    ),
    ("Kickoff notes " + "filler " * 60, ["projects:zephyr"], "zephyr"),
    (
        "## Deploy runbook\n\nThe vaultwarden secret rotates monthly. "
        + "filler " * 60,
        ["tools"],
        "vaultwarden",
    ),
    (
        "Zürich café notes. " + "filler " * 50 + "Tjörn trip: café in Zürich again.",
        ["tools"],
        "zürich café",
    ),
]
CASE_IDS = [
    "deep anchor",
    "head anchor",
    "kebab parts",
    "literal compound",
    "symbol alias",
    "alias and token anchors",
    "stemmed plural",
    "snake case",
    "dotted numeric",
    "cjk bigrams",
    "leading blank line",
    "past the scan cap",
    "scope-only match",
    "markdown opening",
    "diacritics",
]
assert len(CASE_IDS) == len(CASES)


@pytest.fixture(autouse=True)
def _empty_caches() -> Iterator[None]:
    _caches.clear_all()
    yield
    _caches.clear_all()


@pytest.fixture
def tokenize_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text `search.tokenize` is called on, in order."""
    calls: list[str] = []
    real = search_module.tokenize

    def counting(text: str) -> list[str]:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(search_module, "tokenize", counting)
    return calls


def _matched(body: str, scopes: list[str], query: str) -> list[str]:
    hits = search([_memory(body, scopes)], query, now=NOW)
    assert hits, query
    return list(hits[0].match_terms)


@pytest.mark.parametrize(("body", "scopes", "query"), CASES, ids=CASE_IDS)
def test_a_snippet_is_the_uncached_one_cold_and_warm(
    body: str, scopes: list[str], query: str
) -> None:
    matched = _matched(body, scopes, query)
    expected = _reference_snippet(body, matched)
    _caches.clear_all()
    cold = search_module._query_biased_snippet(body, matched)
    warm = search_module._query_biased_snippet("".join(list(body)), matched)
    assert cold == expected
    assert warm == expected


def test_every_case_is_served_warm_after_every_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process scans every body in turn, twice, with the memos filling
    and evicting under a bound of three bodies: a hit, a miss after an
    eviction and a body whose tokens are all interned already each answer
    as the uncached code does."""
    pairs = [(body, _matched(body, scopes, query)) for body, scopes, query in CASES]
    expected = [_reference_snippet(body, matched) for body, matched in pairs]
    _caches.clear_all()
    monkeypatch.setattr(search_module, "SNIPPET_TOKEN_ENTRIES", 3)
    for _ in range(2):
        served = [
            search_module._query_biased_snippet(body, matched)
            for body, matched in pairs
        ]
        assert served == expected


def test_a_warm_snippet_tokenizes_nothing(tokenize_calls: list[str]) -> None:
    body, scopes, query = CASES[0]
    matched = _matched(body, scopes, query)
    _caches.clear_all()
    tokenize_calls.clear()
    first = search_module._query_biased_snippet(body, matched)
    assert tokenize_calls, "premise: the scan tokenizes the body's raw tokens"
    tokenize_calls.clear()
    assert search_module._query_biased_snippet("".join(list(body)), matched) == first
    assert tokenize_calls == []


def test_a_new_body_of_known_tokens_tokenizes_nothing(
    tokenize_calls: list[str],
) -> None:
    """The surfaces of a raw token are interned across bodies: a body the
    snippet path has not scanned, spelled with raw tokens it has, is
    scanned without a `tokenize` call."""
    words = "alpha beta gamma delta epsilon zeta eta theta".split()
    first = " ".join(words * 12) + " kappa marker"
    second = " ".join(reversed(words * 12)) + " marker kappa"
    assert len(first) > 200 and len(second) > 200
    search_module._query_biased_snippet(first, ["kappa"])
    tokenize_calls.clear()
    served = search_module._query_biased_snippet(second, ["kappa"])
    assert tokenize_calls == []
    assert served == _reference_snippet(second, ["kappa"])


def test_the_snippet_memos_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past its bound each memo drops its least recently used entry."""
    monkeypatch.setattr(search_module, "SNIPPET_TOKEN_ENTRIES", 2)
    monkeypatch.setattr(search_module, "TOKEN_SURFACE_ENTRIES", 5)
    bodies = [f"{'word ' * 50}needle{i} " + "tail " * 10 for i in range(4)]
    for i, body in enumerate(bodies):
        search_module._query_biased_snippet(body, [f"needle{i}"])
    assert list(search_module._SNIPPET_TOKENS) == [
        body.strip()[: search_module._SNIPPET_SCAN_CHARS] for body in bodies[2:]
    ]
    interned = list(search_module._TOKEN_SURFACES)
    assert len(interned) == 5
    assert "needle0" not in interned
    assert {"needle3", "tail"} <= set(interned)


def test_the_snippet_memo_keys_on_the_scanned_prefix() -> None:
    """The scan reads the first `_SNIPPET_SCAN_CHARS` characters of the
    stripped body and nothing past them, and the memo keys on exactly that
    slice: two bodies that share it share one entry and are served as the
    uncached code serves them, and a body of a megabyte keeps a key of the
    slice's length, not of its own."""
    head = "\n  " + "alpha beta gamma delta " * 400
    big = head + "zeta " * 200_000
    other = head + "eta theta"
    assert len(big) > 1_000_000
    assert len(head.strip()) > search_module._SNIPPET_SCAN_CHARS
    _caches.clear_all()
    for body in (big, other, big):
        assert search_module._query_biased_snippet(body, ["delta"]) == (
            _reference_snippet(body, ["delta"])
        )
    assert list(search_module._SNIPPET_TOKENS) == [
        big.strip()[: search_module._SNIPPET_SCAN_CHARS]
    ]


def test_the_snippet_memos_are_registered_with_the_cache_registry() -> None:
    body, scopes, query = CASES[0]
    search_module._query_biased_snippet(body, _matched(body, scopes, query))
    assert search_module._SNIPPET_TOKENS and search_module._TOKEN_SURFACES
    _caches.clear_all()
    assert not search_module._SNIPPET_TOKENS
    assert not search_module._TOKEN_SURFACES


def test_a_body_hit_is_touched_atomically_against_a_concurrent_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The look-up of a scanned body and its move to the recent end happen
    under one lock: another thread started from inside the look-up and
    given 100 ms to insert past a bound of one waits for the lock, and
    evicts only after the hit has returned."""
    monkeypatch.setattr(search_module, "SNIPPET_TOKEN_ENTRIES", 1)
    touched = "the body a snippet touches " * 12
    evicting = "the body another thread inserts " * 12
    entry = search_module._snippet_tokens(touched)
    others: list[threading.Thread] = []

    class InterleavedLookup(OrderedDict[Any, Any]):
        def get(self, key: Any, default: Any = None) -> Any:
            value = super().get(key, default)
            if key == touched and not others:
                other = threading.Thread(
                    target=search_module._snippet_tokens, args=(evicting,)
                )
                others.append(other)
                other.start()
                other.join(timeout=0.1)
            return value

    monkeypatch.setattr(
        search_module,
        "_SNIPPET_TOKENS",
        InterleavedLookup(search_module._SNIPPET_TOKENS),
    )
    assert search_module._snippet_tokens(touched) is entry
    others[0].join()
    assert list(search_module._SNIPPET_TOKENS) == [evicting]
