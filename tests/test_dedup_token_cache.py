"""The raw token-set cache behind `find_similar` and `detect_supersession`.

`search.raw_content_tokens` memoises `_raw_content_token_set` on the text
in a bounded LRU registered with `_caches` (unit D1, DESIGN item 11). The
contract is exactness: a hit is the set the uncached tokeniser builds, so
the dedup hits and the supersession report are the same cold, warm, and
with the cache bypassed; only the time changes. These tests pin that on a
scratch store, pin that an edited body is tokenised again, and pin the
bound and the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

import bettermemory.search as search_module
import bettermemory.supersession as supersession_module
from bettermemory import _caches
from bettermemory.models import Memory
from bettermemory.search import (
    _raw_content_token_set,
    clear_raw_token_set_cache,
    find_similar,
    find_similar_tombstones,
    raw_content_tokens,
)
from bettermemory.store import Store
from bettermemory.supersession import SupersessionReport, detect_supersession

_STAMP = "\n\n_(consolidate --llm --from-transcript: turn 7 (of 9), excerpt)_"

# The fixture store: near-duplicates at both dedup bands, compounds in both
# notations, non-ASCII text through the NFKC and diacritic folds, a
# provenance-stamped body the detector strips, and claim-sized statements
# about one subject that the detector links or files.
_BODIES: list[tuple[str, list[str]]] = [
    (
        "The metrics exporter listens on port 9100 on every node of the "
        "homelab cluster.",
        ["projects:homelab"],
    ),
    (
        "The nightly backup job copies the postgres volume to the NAS at "
        "02:00 and keeps fourteen days of snapshots.",
        ["infrastructure"],
    ),
    (
        "The nightly backup job copies the postgres volume to the NAS at "
        "02:00 and keeps seven days of snapshots.",
        ["infrastructure"],
    ),
    (
        "The docker-compose stack for the homelab pins traefik and grafana "
        "to fixed image tags.",
        ["projects:homelab", "tools"],
    ),
    (
        "docker_compose files for the lab keep their image tags pinned, "
        "traefik and grafana included.",
        ["tools"],
    ),
    ("Zürich café notes: Tjörn trip planning for the ＧＰＵ upgrade", ["personal"]),
    (
        "The release pipeline signs every wheel with the project key before "
        "the upload to the package index." + _STAMP,
        ["projects:release"],
    ),
    (
        "Standups and retros for the platform team happen on Tuesdays in the "
        "small meeting room.",
        ["projects:platform"],
    ),
    (
        "The staging database runs postgres 15 on the shared cluster behind pgbouncer.",
        ["infrastructure"],
    ),
]

_NEW_BODIES = [
    "The nightly backup job copies the postgres volume to the NAS at 02:00 "
    "and keeps ten days of snapshots.",
    "Our docker-compose lab stack pins traefik and grafana image tags.",
    "The metrics exporter moved to port 9200 on every node of the homelab cluster.",
    "The staging database runs postgres 16 on the shared cluster behind pgbouncer.",
    "The release pipeline signs every wheel with the project key before the "
    "upload to the package index.",
    "zurich cafe notes about the GPU upgrade",
    "An unrelated note about sourdough starters and rye flour.",
]


@pytest.fixture
def fixture_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "store")
    for body, scopes in _BODIES:
        store.write(content=body, scopes=scopes)
    return store


@pytest.fixture
def tokenised(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text the uncached tokeniser is called on, in order."""
    calls: list[str] = []
    real = search_module._raw_content_token_set

    def counting(text: str) -> set[str]:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(search_module, "_raw_content_token_set", counting)
    return calls


def _uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route both callers around the cache, to the tokeniser itself."""

    def bypass(text: str) -> frozenset[str]:
        return frozenset(_raw_content_token_set(text))

    monkeypatch.setattr(search_module, "raw_content_tokens", bypass)
    monkeypatch.setattr(supersession_module, "raw_content_tokens", bypass)


_R = TypeVar("_R")


def _run_all(fn: Callable[[str, list[Memory]], _R], existing: list[Memory]) -> list[_R]:
    return [fn(body, existing) for body in _NEW_BODIES]


def _similar(body: str, existing: list[Memory]) -> list[dict[str, Any]]:
    return [h.model_dump() for h in find_similar(body, existing)]


def _supersession(body: str, existing: list[Memory]) -> SupersessionReport:
    return detect_supersession(body, existing)


def test_find_similar_identical_cold_and_warm_on_fixture_store(
    fixture_store: Store,
    tokenised: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = fixture_store.load_all()
    assert len(existing) == len(_BODIES)

    cold = _run_all(_similar, existing)
    # Cold: each distinct text was tokenised exactly once, however many
    # queries compared against it.
    assert sorted(tokenised) == sorted({m.body for m in existing} | set(_NEW_BODIES))

    tokenised.clear()
    warm = _run_all(_similar, fixture_store.load_all())
    # Warm, on freshly loaded objects: nothing was tokenised again.
    assert tokenised == []
    assert warm == cold

    with monkeypatch.context() as patch:
        _uncached(patch)
        clear_raw_token_set_cache()
        assert _run_all(_similar, existing) == cold

    # The fixture exercises both bands and the empty case, so the equality
    # above covers every branch of the scorer's output.
    relevances = {hit["relevance"] for hits in cold for hit in hits}
    assert relevances == {"high", "medium"}
    assert [] in cold

    # The tombstone scan reads its sets through the same scorer and cache.
    tokenised.clear()
    assert find_similar_tombstones(_NEW_BODIES[0], []) == []
    assert tokenised == []


def test_detect_supersession_identical_cold_and_warm(
    fixture_store: Store,
    tokenised: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = fixture_store.load_all()
    cold = _run_all(_supersession, existing)
    assert tokenised, "the cold pass tokenised nothing"

    tokenised.clear()
    warm = _run_all(_supersession, fixture_store.load_all())
    assert tokenised == []
    assert warm == cold

    # The stamped body is keyed stripped by the detector and whole by the
    # dedup scorer: two entries, each the uncached set of its own text.
    stamped = next(m.body for m in existing if _STAMP in m.body)
    stripped = supersession_module._strip_provenance(stamped)
    assert stripped != stamped
    assert search_module._RAW_TOKEN_SETS[stripped] == _raw_content_token_set(stripped)
    find_similar(_NEW_BODIES[0], existing)
    assert search_module._RAW_TOKEN_SETS[stamped] == _raw_content_token_set(stamped)

    with monkeypatch.context() as patch:
        _uncached(patch)
        clear_raw_token_set_cache()
        assert _run_all(_supersession, existing) == cold

    # A link and a filed pair are among the reports, so the equality covers
    # the matches and not only empty reports.
    assert any(r.supersedes for r in cold)
    assert any(r.conflicts for r in cold)


def test_rewritten_body_gets_new_entry(
    fixture_store: Store,
    tokenised: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _NEW_BODIES[0]
    before = find_similar(query, fixture_store.load_all())
    detect_supersession(query, fixture_store.load_all())
    target = next(m for m in fixture_store.load_all() if "fourteen days" in m.body)
    assert target.id in {h.id for h in before}

    edited = target.model_copy(
        update={"body": "The office plants are watered on Fridays by facilities."}
    )
    fixture_store.update(edited)
    reloaded = fixture_store.load_all()
    new_body = next(m.body for m in reloaded if m.id == target.id)
    assert new_body != target.body

    tokenised.clear()
    after = find_similar(query, reloaded)
    # Only the edited body was tokenised: same id, new text, new entry.
    assert tokenised == [new_body]
    assert search_module._RAW_TOKEN_SETS[new_body] == _raw_content_token_set(new_body)
    assert target.id not in {h.id for h in after}

    with monkeypatch.context() as patch:
        _uncached(patch)
        assert find_similar(query, reloaded) == after

    # The detector sees the edit the same way.
    tokenised.clear()
    report = detect_supersession(query, reloaded)
    assert target.id not in {m.memory_id for m in report.supersedes + report.conflicts}
    assert tokenised == []


def test_cache_bounded_and_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = 4
    monkeypatch.setattr(search_module, "RAW_TOKEN_SET_ENTRIES", cap)
    texts = [f"note {i} about the gateway-{i} canary rollout" for i in range(12)]
    for i, text in enumerate(texts):
        assert raw_content_tokens(text) == frozenset(_raw_content_token_set(text))
        assert len(search_module._RAW_TOKEN_SETS) <= cap
        if i >= 1:
            # Touch the first text each round: it is the most recently used
            # and survives every eviction.
            raw_content_tokens(texts[0])
            assert len(search_module._RAW_TOKEN_SETS) <= cap
    assert len(search_module._RAW_TOKEN_SETS) == cap
    assert texts[0] in search_module._RAW_TOKEN_SETS
    assert texts[1] not in search_module._RAW_TOKEN_SETS
    assert list(search_module._RAW_TOKEN_SETS)[-1] == texts[0]

    # A hit is the same object; the values cannot be changed by a reader.
    assert raw_content_tokens(texts[0]) is raw_content_tokens(texts[0])
    assert isinstance(raw_content_tokens(texts[0]), frozenset)

    assert clear_raw_token_set_cache in _caches._CLEARERS
    _caches.clear_all()
    assert len(search_module._RAW_TOKEN_SETS) == 0
