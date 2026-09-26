"""The token cache in `search._memory_tokens`.

`_memory_tokens` memoises a pure function on its own inputs, the body
string and the scopes tuple, in a bounded LRU. The contract is exactness:
a hit returns what the uncached code computes, token for token, and only
the time changes. These tests pin the key, the bound, the clear, the reuse
of one object across calls, and the equality of a hit with the recompute
expressions the scorers evaluate when no tokens are threaded to them.
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

import bettermemory.search as search_module
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _expand_kebab,
    _scope_tokens,
    _strip_stopwords,
    clear_token_cache,
    compute_idf,
    score_memory,
    score_memory_bm25,
    search,
    tokenize,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

# The corpus and queries of test_search.py's
# `test_precomputed_candidate_tokens_byte_identical`: one memory per
# tokenizer branch (plurals, CJK, halfwidth, diacritics, stopwords,
# compounds, dotted numerics) and one query per shape.
CORPUS: list[tuple[str, list[str]]] = [
    ("The standups and retros moved to Tuesdays", ["projects:demo"]),
    ("docker_compose caches C++ builds for .NET 3.12.1", ["tools"]),
    ("東京オフィスは移転する 2026年に移転", ["projects:tokyo-move"]),
    ("ＧＰＵは２０２６年に交換予定 ｻｰﾊﾞｰ", ["infrastructure"]),
    ("Zürich café notes: Tjörn trip planning", ["personal-context"]),
    ("jag vill att den ska fungera på servern", ["projects:demo", "tools"]),
    ("der Server wird durch die Firewall blockiert", ["infrastructure"]),
]
QUERIES = [
    "standups zürich",
    "docker-compose caches",
    "東京 移転",
    "ｻｰﾊﾞｰ gpu",
    "vad har jag om servern",
    "firewall",
    "3.12",
    "what is the",
]


def _memory(
    body: str,
    scopes: list[str] | None = None,
    *,
    memory_id: str | None = None,
    stamp: datetime = NOW,
) -> Memory:
    return Memory(
        id=memory_id or generate_ulid(),
        created=stamp,
        updated=stamp,
        scopes=["tools"] if scopes is None else scopes,
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _fresh(text: str) -> str:
    """An equal string that is a different object, the way a body arrives
    when the store loads the record again."""
    out = "".join(list(text))
    assert out == text and out is not text
    return out


def _reloaded(memory: Memory) -> Memory:
    """The same record as a new object whose body and scopes are new
    strings of equal value."""
    return memory.model_copy(
        update={
            "body": _fresh(memory.body),
            "scopes": [_fresh(s) for s in memory.scopes],
        }
    )


def _recompute(memory: Memory) -> tuple[list[str], list[str], set[str]]:
    """The expressions the scorers evaluate when no tokens are threaded."""
    body = _expand_kebab(tokenize(memory.body))
    scope_set: set[str] = set()
    for scope in memory.scopes:
        scope_set.update(_scope_tokens(scope))
    return body, _strip_stopwords(body), scope_set


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    clear_token_cache()
    yield
    clear_token_cache()


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


def test_an_equal_body_and_scopes_return_the_same_object_untokenized(
    tokenize_calls: list[str],
) -> None:
    body = "Postgres 16 runs on the homelab box behind the docker-compose stack"
    scopes = ["projects:homelab", "infrastructure"]
    first = search_module._memory_tokens(_memory(body, scopes))
    # The miss tokenises the body once and each scope once.
    assert tokenize_calls == [body, *scopes]

    tokenize_calls.clear()
    again = _reloaded(_memory(body, scopes))
    assert search_module._memory_tokens(again) is first
    assert tokenize_calls == []


def test_a_different_body_misses(tokenize_calls: list[str]) -> None:
    first = search_module._memory_tokens(_memory("redis caches the session tokens"))
    tokenize_calls.clear()

    body = "redis caches the rendered pages"
    other = search_module._memory_tokens(_memory(body))
    assert other is not first
    assert tokenize_calls == [body, "tools"]
    assert other.body != first.body

    # The key is the input, not the output: a body that tokenises to the
    # same stream is still a different key, and still computes its own
    # entry, equal to the first.
    tokenize_calls.clear()
    plural = search_module._memory_tokens(_memory("redis caches the session token"))
    assert tokenize_calls == ["redis caches the session token", "tools"]
    assert plural is not first and plural == first


def test_a_different_scopes_tuple_misses(tokenize_calls: list[str]) -> None:
    body = "the deploy pipeline pins node 20"
    first = search_module._memory_tokens(_memory(body, ["projects:web"]))
    tokenize_calls.clear()

    other = search_module._memory_tokens(_memory(body, ["projects:web", "tools"]))
    assert other is not first
    assert tokenize_calls == [body, "projects:web", "tools"]
    assert other.body == first.body
    assert other.scope_set == first.scope_set | set(_scope_tokens("tools"))

    # Order is part of the key: the same scopes in another order miss,
    # and compute the same set.
    tokenize_calls.clear()
    swapped = search_module._memory_tokens(_memory(body, ["tools", "projects:web"]))
    assert tokenize_calls == [body, "tools", "projects:web"]
    assert swapped is not other and swapped == other


def test_shared_id_and_updated_with_different_bodies_miss() -> None:
    """The case an `(id, updated)` key gets wrong: two objects of one
    record, same id and same `updated` stamp, different bodies."""
    memory_id = generate_ulid()
    before = _memory("the backup job runs nightly at 02:00", memory_id=memory_id)
    after = _memory("the backup job moved to hourly snapshots", memory_id=memory_id)
    assert (before.id, before.updated) == (after.id, after.updated)

    tokens_before = search_module._memory_tokens(before)
    tokens_after = search_module._memory_tokens(after)
    assert tuple(tokens_before) == _recompute(before)
    assert tuple(tokens_after) == _recompute(after)
    assert tokens_before.body != tokens_after.body
    assert search_module._memory_tokens(before) is tokens_before
    assert search_module._memory_tokens(after) is tokens_after


def test_the_bound_evicts_the_least_recently_used(
    monkeypatch: pytest.MonkeyPatch, tokenize_calls: list[str]
) -> None:
    monkeypatch.setattr(search_module, "TOKEN_CACHE_ENTRIES", 3)
    memories = [_memory(f"entry number {i} about caches") for i in range(4)]
    t0, t1, t2 = (search_module._memory_tokens(m) for m in memories[:3])

    # A hit moves entry 0 to the most recent end, so the fourth insert
    # evicts entry 1, now the least recently used.
    assert search_module._memory_tokens(memories[0]) is t0
    search_module._memory_tokens(memories[3])
    assert len(search_module._TOKEN_CACHE) == 3

    tokenize_calls.clear()
    assert search_module._memory_tokens(memories[0]) is t0
    assert search_module._memory_tokens(memories[2]) is t2
    assert tokenize_calls == []

    recomputed = search_module._memory_tokens(memories[1])
    assert recomputed is not t1 and recomputed == t1
    assert tokenize_calls == [memories[1].body, "tools"]
    assert len(search_module._TOKEN_CACHE) == 3


def test_the_shipped_bound_holds_5000_entries() -> None:
    assert search_module.TOKEN_CACHE_ENTRIES == 5000
    oldest = _memory("entry 0")
    search_module._memory_tokens(oldest)
    template = _memory("entry template")
    for i in range(1, 5001):
        search_module._memory_tokens(template.model_copy(update={"body": f"entry {i}"}))
    assert len(search_module._TOKEN_CACHE) == 5000
    assert (oldest.body, tuple(oldest.scopes)) not in search_module._TOKEN_CACHE
    assert ("entry 1", ("tools",)) in search_module._TOKEN_CACHE


def test_clear_token_cache_empties_it(tokenize_calls: list[str]) -> None:
    memory = _memory("the staging database password rotates every 90 days")
    first = search_module._memory_tokens(memory)
    assert len(search_module._TOKEN_CACHE) == 1

    clear_token_cache()
    assert len(search_module._TOKEN_CACHE) == 0

    tokenize_calls.clear()
    again = search_module._memory_tokens(memory)
    assert again is not first and again == first
    assert tokenize_calls == [memory.body, "tools"]


def test_a_hit_equals_the_recompute_token_for_token() -> None:
    """test_search.py's precompute-equality check, run on entries served
    from the cache: every field equals its recompute expression, and the
    IDF maps, avgdl, scores and matched terms of all three scorers equal
    the recompute path's, stopword fallback included."""
    corpus = [_memory(body, scopes) for body, scopes in CORPUS]
    filled = [search_module._memory_tokens(m) for m in corpus]
    hits = [search_module._memory_tokens(_reloaded(m)) for m in corpus]
    assert all(hit is entry for hit, entry in zip(hits, filled))

    for memory, hit in zip(corpus, hits):
        body, content, scope_set = _recompute(memory)
        assert hit.body == body, memory.body
        assert hit.content == content, memory.body
        assert hit.scope_set == scope_set, memory.body

    idf_plain = compute_idf(corpus)
    assert compute_idf(corpus, tokens=hits) == idf_plain
    body_idf, scope_idf, avgdl = idf_plain
    for query in QUERIES:
        stripped = _strip_stopwords(tokenize(query))
        query_tokens = stripped or tokenize(query)
        fallback = not stripped
        for memory, hit in zip(corpus, hits):
            assert score_memory(memory, query_tokens, now=NOW) == score_memory(
                memory, query_tokens, now=NOW, tokens=hit
            ), (query, memory.body)
            plain = score_memory_bm25(
                memory,
                query_tokens,
                body_idf_map=body_idf,
                scope_idf_map=scope_idf,
                avgdl=avgdl,
                now=NOW,
                stopword_fallback=fallback,
            )
            threaded = score_memory_bm25(
                memory,
                query_tokens,
                body_idf_map=body_idf,
                scope_idf_map=scope_idf,
                avgdl=avgdl,
                now=NOW,
                tokens=hit,
                stopword_fallback=fallback,
            )
            assert plain == threaded, (query, memory.body)


def test_searches_never_mutate_a_cached_entry() -> None:
    """Entries are shared by every search after the one that built them,
    so a consumer that mutated a list or the set would change the next
    search's ranking. Every lane runs over the corpus and each entry must
    come out equal to a deep copy taken before, and still be the entry
    the cache serves."""
    corpus = [_memory(body, scopes) for body, scopes in CORPUS]
    entries = [search_module._memory_tokens(m) for m in corpus]
    snapshots = [copy.deepcopy(entry) for entry in entries]

    for query in QUERIES:
        for mode in ("keyword", "bm25", "hybrid"):
            for rescue in (False, True):
                search(
                    corpus,
                    query,
                    mode=mode,
                    now=NOW,
                    rescue_expansion=rescue,
                    conversational=True,
                )

    for memory, entry, snapshot in zip(corpus, entries, snapshots):
        assert search_module._memory_tokens(memory) is entry
        assert entry == snapshot, memory.body


def test_a_warm_search_tokenizes_only_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold property is `test_search_tokenizes_each_candidate_once`'s
    one call per body, per scope and for the query; warm, the candidates
    come from the cache, including for records loaded again as new
    objects, and the hits are equal."""
    memories = [
        _memory(f"memory number {i} about docker and redis caches") for i in range(20)
    ]
    cold = search(memories, "docker redis caches", mode="hybrid", now=NOW)
    assert cold

    calls = {"n": 0}
    real_impl = search_module._tokenize_impl

    def counting(text: str, *, stem: bool) -> list[str]:
        calls["n"] += 1
        return real_impl(text, stem=stem)

    monkeypatch.setattr(search_module, "_tokenize_impl", counting)
    reloaded = [_reloaded(m) for m in memories]
    warm = search(reloaded, "docker redis caches", mode="hybrid", now=NOW)
    assert calls["n"] == 1
    assert [h.model_dump() for h in warm] == [h.model_dump() for h in cold]


def test_a_hit_is_touched_atomically_against_a_concurrent_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit's look-up and its move to the recent end happen under one
    lock, so an insert on another thread cannot evict the entry between
    the two, where `move_to_end` would raise KeyError. The other thread is
    started from inside the look-up and given 100 ms to insert past a
    bound of one; under the lock it waits for the lock instead, and
    evicts only after the hit has returned."""
    monkeypatch.setattr(search_module, "TOKEN_CACHE_ENTRIES", 1)
    touched = _memory("the entry a search touches")
    evicting = _memory("the entry another thread inserts")
    entry = search_module._memory_tokens(touched)
    touched_key = (touched.body, tuple(touched.scopes))
    others: list[threading.Thread] = []

    class InterleavedLookup(OrderedDict[Any, Any]):
        def get(self, key: Any, default: Any = None) -> Any:
            value = super().get(key, default)
            if key == touched_key and not others:
                other = threading.Thread(
                    target=search_module._memory_tokens, args=(evicting,)
                )
                others.append(other)
                other.start()
                other.join(timeout=0.1)
            return value

    monkeypatch.setattr(
        search_module, "_TOKEN_CACHE", InterleavedLookup(search_module._TOKEN_CACHE)
    )
    assert search_module._memory_tokens(touched) is entry
    others[0].join()
    assert list(search_module._TOKEN_CACHE) == [(evicting.body, tuple(evicting.scopes))]
