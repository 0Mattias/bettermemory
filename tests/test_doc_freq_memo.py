"""The document frequency memo behind `Store.document_frequencies(...,
memoised=True)`.

Every search above the prefilter threshold prices its BM25 legs with the
admitted collection's document frequencies: the per-prompt recall's probe,
`memory_search` and the review sample. The uncached call reads every
memory row and runs two FTS queries per term; the memo keeps the rows'
admission inputs and the terms' hits, keyed on the log's last memory row,
and runs the admission predicate on every call. These tests hold it to the
uncached call: reused while no memory changes (telemetry rows included),
brought up to date after each operation that changes a memory, from this
connection or another (by the rows the log names, or read again where it
cannot follow), and the same ranked results with it on and off.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bettermemory import store as store_module
from bettermemory.admission import Admission
from bettermemory.config import Config, StorageConfig
from bettermemory.membership import (
    GLOBAL,
    SEARCH_AUTO_SCOPE,
    SILENT,
    Membership,
    RecordHome,
    Resolution,
)
from bettermemory.models import Memory, generate_ulid
from bettermemory.origin import Origin
from bettermemory.search import candidate_admitted
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import IMPORTED, Store

from ._mcp import call_tool as _mcp_call
from ._internal_rows import raw_hits

REPO_A = "https://example.com/acme/alpha.git"
REPO_B = "https://example.com/acme/beta.git"
TERMS = ["port", "halden", "postgres", "absent", "notes"]

#: The home each origin's records are given: a project per repository,
#: ``global`` for none (design 2.1). The origin stays on the record as data.
HOMES = {
    REPO_A: RecordHome("P_ALPHA", None, "test"),
    REPO_B: RecordHome("P_BETA", None, "test"),
    None: RecordHome(GLOBAL, None, "outside_repository"),
}

#: A caller in the alpha project: its auto-scoped search admits alpha's
#: records and the global ones, not beta's (design 3.5).
ALPHA = Resolution(
    outcome=SILENT, project="P_ALPHA", checkout=None, rule="test", chain=("P_ALPHA",)
)


def _admit_in(store: Store) -> Callable[[list[str], str | None, Any], bool]:
    """The search's own predicate (`candidate_admitted`) for the alpha
    caller's auto-scoped search over `store`."""
    admission = Admission(SEARCH_AUTO_SCOPE, ALPHA, Membership(store, "machine-test"))

    def admit(scopes: list[str], home: str | None, actor: Any) -> bool:
        return candidate_admitted(
            scopes,
            home,
            actor,
            scope_filter=None,
            # No seeded memory carries it; `_rename_scope` gives one the scope.
            excluded={"postgres"},
            admission=admission,
        )

    return admit


def _seed(store: Store) -> list[str]:
    docs = [
        ("the halden port is 5432", ["projects:halden"], REPO_A),
        ("the halden port moved", ["projects:halden"], REPO_A),
        ("postgres port for verendo", ["projects:verendo"], REPO_B),
        ("port notes", ["tools"], None),
    ]
    return [
        store.write(
            content=body,
            scopes=scopes,
            origin=Origin(repo=repo) if repo else None,
            home=HOMES[repo],
        ).id
        for body, scopes, repo in docs
    ]


class _Reads:
    """Counts the memo's reads of the rows and of each term's hits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inputs = 0
        self.terms: list[str] = []
        real_inputs = Store._read_doc_freq_inputs
        real_terms = Store._term_masks

        def inputs(store: Store, head: Any) -> Any:
            self.inputs += 1
            return real_inputs(store, head)

        def terms(store: Store, term: str, entry: Any) -> Any:
            self.terms.append(term)
            return real_terms(store, term, entry)

        monkeypatch.setattr(Store, "_read_doc_freq_inputs", inputs)
        monkeypatch.setattr(Store, "_term_masks", terms)


def _same(store: Store, terms: list[str] = TERMS) -> Any:
    """The memoised answer, asserted equal to the uncached one."""
    admit = _admit_in(store)
    served = store.document_frequencies(terms, admit=admit, memoised=True)
    assert served == store.document_frequencies(terms, admit=admit)
    return served


def test_the_memo_is_reused_while_no_memory_changes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store)
    reads = _Reads(monkeypatch)
    first = _same(store)
    assert first is not None and first[0] == 3
    assert reads.inputs == 1 and sorted(reads.terms) == sorted(TERMS)

    # Telemetry rows, as every search appends, leave the memo warm.
    store.import_event(
        {"ts": "2026-10-12T00:00:00Z", "session": "s1", "kind": "search"},
        imported_from="test",
    )
    assert _same(store) == first
    # A subset of the terms, and the predicate still runs per call: a
    # different admission over the same kept rows answers its own counts.
    assert _same(store, ["port"]) is not None
    everyone = store.document_frequencies(TERMS, admit=lambda *_: True, memoised=True)
    assert everyone == store.document_frequencies(TERMS, admit=lambda *_: True)
    assert everyone is not None and everyone[0] == 4
    assert reads.inputs == 1 and sorted(reads.terms) == sorted(TERMS)


def _write(store: Store, ids: list[str]) -> None:
    store.write(
        content="halden postgres port",
        scopes=["projects:halden"],
        home=HOMES[REPO_A],
    )


def _update(store: Store, ids: list[str]) -> None:
    memory = store.get_memory(ids[3])
    store.update(memory.model_copy(update={"body": "postgres halden notes"}))


def _tombstone(store: Store, ids: list[str]) -> None:
    store.tombstone(ids[0], "superseded")


def _restore(store: Store, ids: list[str]) -> None:
    store.restore(ids[0])


# The restore's own tombstone, made before the memo is warmed.
_restore.setup = _tombstone  # type: ignore[attr-defined]


def _import(store: Store, ids: list[str]) -> None:
    memory = Memory.model_validate(
        {
            **store.get_memory(ids[3]).model_dump(mode="json"),
            "id": generate_ulid(),
            "body": "imported halden port",
        }
    )
    store.put_memory(memory, provenance=IMPORTED, home=HOMES[None])


def _rename_scope(store: Store, ids: list[str]) -> None:
    store.rename_scope("tools", "postgres")


def _other_connection(store: Store, ids: list[str]) -> None:
    other = store.reopen()
    other.write(
        content="halden port from a worker",
        scopes=["projects:halden"],
        home=HOMES[REPO_A],
    )


#: The changes whose log rows name the records they touched and leave each
#: its row number: the memo reads those rows and brings each term's hits up
#: to date for them.
FOLLOWED = [_write, _update, _restore, _import, _rename_scope, _other_connection]


@pytest.mark.parametrize("change", FOLLOWED, ids=lambda f: f.__name__.lstrip("_"))
def test_a_change_the_log_names_is_followed_without_reading_the_store(
    store: Store, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    ids = _seed(store)
    setup = getattr(change, "setup", None)
    if setup is not None:
        setup(store, ids)
    reads = _Reads(monkeypatch)
    before = _same(store)
    inputs_before = reads.inputs
    change(store, ids)
    reads.terms.clear()
    after = _same(store)
    assert reads.inputs == inputs_before, (
        "every row was read for a change the log names"
    )
    assert not reads.terms, "a term was read whole for a row the log names"
    assert after != before, "premise: the change moves the counts"


def test_a_removal_reads_the_memo_again(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed record's row number goes with its row, so the memo cannot
    find the bit to clear: it reads the rows and the terms again."""
    ids = _seed(store)
    reads = _Reads(monkeypatch)
    before = _same(store)
    inputs_before = reads.inputs
    _tombstone(store, ids)
    reads.terms.clear()
    after = _same(store)
    assert reads.inputs > inputs_before, "the memo served rows of an older store"
    assert sorted(reads.terms) == sorted(TERMS), "the memo served older hits"
    assert after != before, "premise: the change moves the counts"


def _statistics(store: Store, terms: list[str]) -> Any:
    """`ranking_statistics` through the memo as it stands, asserted equal
    to the same call on a memo emptied first (every row read anew)."""
    from bettermemory import store_index

    admit = _admit_in(store)
    followed = store.ranking_statistics(terms, admit=admit)
    kept = dict(store_index._DOC_FREQS)
    store_index._clear_doc_freqs()
    assert followed == store.ranking_statistics(terms, admit=admit)
    # The followed entry goes back, so the next change is followed from it.
    store_index._DOC_FREQS.clear()
    store_index._DOC_FREQS.update(kept)
    return followed


def test_a_followed_memo_answers_as_a_fresh_read_over_many_changes(
    store: Store,
) -> None:
    """Writes, updates, imports, scope renames and removals in a seeded
    order, with the statistics asked between them over changing
    terms: each answer is the uncached one, and the length average is the
    one a fresh read of every row gives."""
    import random

    rng = random.Random(20261114)
    words = [
        "port",
        "halden",
        "postgres",
        "notes",
        "verendo",
        "cache",
        "queue",
        "index",
    ]
    ids = _seed(store)
    homes = [HOMES[REPO_A], HOMES[REPO_B], HOMES[None]]
    _same(store)
    for step in range(60):
        roll = rng.random()
        body = " ".join(rng.choice(words) for _ in range(rng.randint(2, 9)))
        if roll < 0.45:
            ids.append(
                store.write(
                    content=f"{body} {step}",
                    scopes=[rng.choice(["projects:halden", "tools", "postgres"])],
                    home=rng.choice(homes),
                ).id
            )
        elif roll < 0.8:
            memory = store.get_memory(rng.choice(ids))
            store.update(memory.model_copy(update={"body": f"{body} again {step}"}))
        elif roll < 0.9:
            memory = Memory.model_validate(
                {
                    **store.get_memory(rng.choice(ids)).model_dump(mode="json"),
                    "id": generate_ulid(),
                    "body": f"imported {body} {step}",
                }
            )
            store.put_memory(memory, provenance=IMPORTED, home=rng.choice(homes))
            ids.append(memory.id)
        elif roll < 0.95 and len(ids) > 3:
            store.tombstone(ids.pop(rng.randrange(len(ids))), "superseded")
        else:
            store.rename_scope("tools", "postgres")
        terms = rng.sample(words, rng.randint(1, len(words)))
        _same(store, terms)
        if step % 7 == 0:
            _statistics(store, words)


def test_a_term_is_read_whole_past_the_rows_it_can_be_patched_for(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A term asked again after more rows changed than `_PATCH_ROWS` is
    read by its two queries, and one asked after more than `_ADVANCE_ROWS`
    finds the rows read again; both answer as the uncached call."""
    from bettermemory import store_index

    monkeypatch.setattr(store_index, "_PATCH_ROWS", 2)
    monkeypatch.setattr(store_index, "_ADVANCE_ROWS", 5)
    _seed(store)
    reads = _Reads(monkeypatch)
    _same(store)
    for i in range(3):
        store.write(content=f"halden port note {i}", scopes=["tools"], home=HOMES[None])
    reads.terms.clear()
    _same(store)
    assert reads.inputs == 1 and sorted(reads.terms) == sorted(TERMS)
    for i in range(4):
        store.write(content=f"halden notes {i}", scopes=["tools"], home=HOMES[None])
    _same(store)
    assert reads.inputs == 2


def test_an_old_record_written_again_reads_its_terms_whole(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A term's rows are asked from the lowest changed row number up. An
    update of an old record puts most of a common term's rows above it, so
    past `_PATCH_WINDOW` of them the term is read by its two queries; the
    rows are still followed from the log, and the answer is the uncached
    one."""
    from bettermemory import store_index

    monkeypatch.setattr(store_index, "_PATCH_WINDOW", 3)
    ids = _seed(store)
    for i in range(6):
        store.write(content=f"halden port note {i}", scopes=["tools"], home=HOMES[None])
    reads = _Reads(monkeypatch)
    _same(store)
    memory = store.get_memory(ids[0])
    store.update(memory.model_copy(update={"body": "the halden port is 9443"}))
    reads.terms.clear()
    _same(store)
    assert reads.inputs == 1, "every row was read for an update the log names"
    assert "port" in reads.terms and "halden" in reads.terms
    assert "absent" not in reads.terms


def test_worker_connections_share_the_memo(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store)
    reads = _Reads(monkeypatch)
    _same(store)
    worker = store.reopen()
    admit = _admit_in(store)
    assert worker.document_frequencies(TERMS, admit=admit, memoised=True) == (
        store.document_frequencies(TERMS, admit=admit)
    )
    assert reads.inputs == 1 and sorted(reads.terms) == sorted(TERMS)


def test_a_read_inside_a_transaction_is_served_uncached(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed(store)
    reads = _Reads(monkeypatch)
    with store.batch():
        store.tombstone(ids[0], "superseded")
        admit = _admit_in(store)
        inside = store.document_frequencies(TERMS, admit=admit, memoised=True)
        assert inside == store.document_frequencies(TERMS, admit=admit)
    assert reads.inputs == 0 and not reads.terms
    assert store_module._DOC_FREQS == {}
    _same(store)


# ---------------------------------------------------------------------------
# Ranked results, the memo on and forced off
# ---------------------------------------------------------------------------

QUERIES = [
    "halden port",
    "postgres connection pool settings",
    "which port does the verendo database listen on",
    "deploy notes for the alpha service",
    "kebab-case cache-invalidation",
    "absent words only zzyzx",
]


def _synthetic(store: Store) -> None:
    topics = ["port", "postgres", "deploy", "cache-invalidation", "pool", "notes"]
    projects = ["halden", "verendo", "alpha"]
    for i in range(72):
        project = projects[i % 3]
        words = " ".join(topics[(i + k) % len(topics)] for k in range(1 + i % 4))
        repo = (REPO_A, REPO_B, None)[i % 3]
        store.write(
            content=f"{project} {words} detail-{i} " + "filler " * (i % 7),
            scopes=[f"projects:{project}", "tools" if i % 2 else "infrastructure"],
            origin=Origin(repo=repo) if repo else None,
            home=HOMES[repo],
        )


async def _ranked(server: Any) -> list[list[tuple[str, float, str]]]:
    out = []
    for query in QUERIES:
        # Unscoped: the caller stands outside any project (the test's
        # workspace), where the unscoped search ranks every project's
        # records and the global ones (design 3.5), the whole corpus.
        res = await _mcp_call(
            server,
            "memory_search",
            {"query": query, "max_results": 10, "auto_scope": False},
        )
        hits = res.get("result", res) if isinstance(res, dict) else res
        out.append([(h["id"], h["score"], h["relevance"]) for h in hits])
    return out


async def test_ranked_results_are_identical_with_the_memo_on_and_off(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scores the ranking computed, as the search's working rows carry them.
    raw_hits(monkeypatch)
    _synthetic(Store(memory_dir))
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    def server() -> Any:
        return build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    real = Store.ranking_statistics
    served: list[int] = []

    def spy(self: Store, terms: Any, *, admit: Any) -> Any:
        served.append(len(terms))
        return real(self, terms, admit=admit)

    monkeypatch.setattr(Store, "ranking_statistics", spy)
    on = await _ranked(server())
    # Twice, so the second pass is served from a warm memo.
    assert await _ranked(server()) == on
    assert served, "premise: the ranking read its statistics from the store"
    assert store_module._DOC_FREQS, "premise: the memo kept an entry"

    def off(self: Store, terms: Any, *, admit: Any) -> Any:
        # The frequencies of the uncached read, beside the length average
        # the memo's inputs carry: what is compared is the frequencies.
        found = self.document_frequencies(terms, admit=admit, memoised=False)
        with_average = real(self, terms, admit=admit)
        if found is None or with_average is None:
            return None
        return (*found, with_average[3])

    monkeypatch.setattr(Store, "ranking_statistics", off)
    assert await _ranked(server()) == on
    assert sum(1 for hits in on if hits) >= 4, "premise: the queries rank hits"
