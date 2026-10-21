"""The document frequency memo behind `Store.document_frequencies(...,
memoised=True)`.

Every search above the prefilter threshold prices its BM25 legs with the
admitted collection's document frequencies: the per-prompt recall's probe,
`memory_search` and the review sample. The uncached call reads every
memory row and runs two FTS queries per term; the memo keeps the rows'
admission inputs and the terms' hits, keyed on the log's last memory row,
and runs the admission predicate on every call. These tests hold it to the
uncached call: reused while no memory changes (telemetry rows included),
read again after each operation that changes a memory, from this
connection or another, and the same ranked results with it on and off.
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


@pytest.mark.parametrize(
    "change",
    [_write, _update, _tombstone, _restore, _import, _rename_scope, _other_connection],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_each_memory_change_reads_the_memo_again(
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
    assert reads.inputs > inputs_before, "the memo served rows of an older store"
    assert sorted(reads.terms) == sorted(TERMS), "the memo served older hits"
    assert after != before, "premise: the change moves the counts"


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
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "20")
    _synthetic(Store(memory_dir))
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    def server() -> Any:
        return build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    real = Store.document_frequencies
    memoised_calls: list[bool] = []

    def spy(self: Store, terms: Any, *, admit: Any, memoised: bool = False) -> Any:
        memoised_calls.append(memoised)
        return real(self, terms, admit=admit, memoised=memoised)

    monkeypatch.setattr(Store, "document_frequencies", spy)
    on = await _ranked(server())
    # Twice, so the second pass is served from a warm memo.
    assert await _ranked(server()) == on
    assert memoised_calls and all(memoised_calls), "premise: the memo served"
    assert store_module._DOC_FREQS, "premise: the memo kept an entry"

    def off(self: Store, terms: Any, *, admit: Any, memoised: bool = False) -> Any:
        return real(self, terms, admit=admit, memoised=False)

    monkeypatch.setattr(Store, "document_frequencies", off)
    assert await _ranked(server()) == on
    assert sum(1 for hits in on if hits) >= 4, "premise: the queries rank hits"
