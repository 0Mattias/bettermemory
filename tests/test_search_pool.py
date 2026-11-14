"""The search pool (`handlers.search.resolve_search_pool`).

A search ranks a pool: the records the full-text index ranks best for the
query, among those the request admits, priced with the store's own
statistics. What is held here:

- A request admitted to few of the best matches still gets its own: rows
  are admitted before the pool is cut, and nothing loads the store to
  find them.
- No search reads every record: not for a query that matches nothing,
  one with no token, an empty one, or the changes since the prior
  session.
- The statistics are the collection's: document frequencies and the
  BM25 length average over every admitted record, not over the pool.
- Where every admitted match fits in the pool, the hits are those a
  ranking of the whole store returns, with the same scores.
- Only the returned hits are read in full.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import bettermemory.handlers.search as pool_module
import bettermemory.search as search_module
from bettermemory.config import Config, StorageConfig
from bettermemory.handlers.search import POOL_SIZE, resolve_search_pool
from bettermemory.hook import run_audit
from bettermemory.location import Caller, resolve_caller
from bettermemory.membership import GLOBAL, RecordHome
from bettermemory.models import Memory, generate_ulid
from bettermemory.search import search as run_search
from bettermemory.search_bm25 import compute_idf
from bettermemory.search_lexicon import _expand_kebab, fts_index_text, tokenize
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call
from .test_membership_security import checkout, stand_in_checkout

#: The home of a record written outside any project (design 2.1).
GLOBAL_HOME = RecordHome(GLOBAL, None, "outside_repository")
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def _build_server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


class _Projects:
    """The caller's checkout (``a``, the process cwd) and another
    repository's (``b``), each its own project, resolved over the test's
    store: a record is delivered by its home (design 3.5)."""

    def __init__(
        self, store: Store, base: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = Config(storage=StorageConfig(directory=str(store.path)))
        repo_a = stand_in_checkout(base, monkeypatch)
        self.a: Caller = resolve_caller(store, config, str(repo_a))
        repo_b = checkout(base / "b")
        self.b: Caller = resolve_caller(store, config, str(repo_b))


@pytest.fixture
def projects(
    store: Store,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> _Projects:
    return _Projects(store, tmp_path_factory.mktemp("projects"), monkeypatch)


def _write_dense(store: Store, n: int, *, scopes: list[str], home: RecordHome) -> None:
    """`n` short, term-dense "alpha" memories: the index ranks them first."""
    dense = "alpha " * 6
    for i in range(n):
        store.write(content=f"{dense}filler-{i}", scopes=scopes, home=home)


def _write_weak(
    store: Store, n: int, *, scopes: list[str], home: RecordHome
) -> list[str]:
    """`n` long memories mentioning "alpha" once: ranked past the dense
    block. Returns their ids."""
    padding = " ".join(f"pad{j}" for j in range(40))
    return [
        store.write(
            content=f"alpha appears once here {padding} doc-{i}",
            scopes=scopes,
            home=home,
        ).id
        for i in range(n)
    ]


@pytest.fixture
def whole_store_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the whole-store readers a test's calls reached."""
    reads: list[str] = []
    for name in ("load_all", "iter_memories"):
        real = getattr(Store, name)

        def spy(self: Store, *args: Any, _real: Any = real, _name: str = name) -> Any:
            reads.append(_name)
            return _real(self, *args)

        monkeypatch.setattr(Store, name, spy)
    return reads


# ---------------------------------------------------------------------------
# Rows are admitted before the pool is cut
# ---------------------------------------------------------------------------


async def test_a_caller_admitted_to_few_of_the_best_matches_gets_its_own(
    memory_dir: Path,
    store: Store,
    projects: _Projects,
    monkeypatch: pytest.MonkeyPatch,
    whole_store_reads: list[str],
) -> None:
    """Sixty term-dense matches homed in another project rank first; the
    caller's three, homed in its own, rank after them. A pool cut on
    relevance and admitted afterwards held none of the caller's and
    returned nothing while its matches stood in the store. The first read
    here is 20 rows, all of the other project, so the pool admits among
    every match, and reads no record but its own."""
    monkeypatch.setattr(pool_module, "_FIRST_READ", 20)
    _write_dense(store, 60, scopes=["tools"], home=projects.b.home())
    a_ids = _write_weak(store, 3, scopes=["tools"], home=projects.a.home())
    first = {cid for cid, _ in store.query_candidates("alpha", max_results=20)}
    assert not (set(a_ids) & first), "premise: the caller's rank past the first read"

    hits = _unwrap(
        await _call(_build_server(memory_dir), "memory_search", query="alpha")
    )

    assert {h["id"] for h in hits} == set(a_ids)
    assert whole_store_reads == []


async def test_a_disabled_scope_does_not_starve_the_pool(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, where what strips the best matches is the session's
    disabled scope: every record is homed ``global`` and the caller
    stands outside any project, so the admission strips nothing."""
    monkeypatch.setattr(pool_module, "_FIRST_READ", 20)
    _write_dense(store, 60, scopes=["noise"], home=GLOBAL_HOME)
    tool_ids = _write_weak(store, 3, scopes=["tools"], home=GLOBAL_HOME)

    server = _build_server(memory_dir)
    await _call(server, "memory_admin", action="disable_scope", scope="noise")
    hits = _unwrap(await _call(server, "memory_search", query="alpha"))

    assert {h["id"] for h in hits} == set(tool_ids)


def test_the_pool_holds_its_size_of_admitted_rows_in_the_indexes_order(
    store: Store,
) -> None:
    _write_dense(store, 30, scopes=["tools"], home=GLOBAL_HOME)
    ranked = [cid for cid, _ in store.query_candidates("alpha", max_results=30)]

    pool = resolve_search_pool(store, "alpha", size=10)

    assert [m.id for m in pool.memories] == ranked[:10]
    assert pool.corpus_stats_provider is not None
    assert len(resolve_search_pool(store, "alpha").memories) == 30


def test_a_row_the_statistics_do_not_know_is_admitted_from_its_own_columns(
    store: Store,
) -> None:
    """A row no log row put in the table (the store's statistics follow
    the log, so they do not hold it) is decided from its columns, so a
    search can still deliver it under the label its provenance read
    gives it."""
    kept = store.write(content="alpha kept", scopes=["tools"], home=GLOBAL_HOME)
    every = store.ranked_rows("alpha")
    admit_all = lambda scopes, home, actor: True  # noqa: E731
    assert store.admitted_rows(every, admit_all) == every

    columns = [
        row[1]
        for row in store.conn.execute("PRAGMA table_info(memories)")
        if row[1] != "rowid"
    ]
    planted = generate_ulid()
    values = ", ".join("?" if c == "id" else c for c in columns)
    with store._transaction() as tx:
        tx.execute(
            f"INSERT INTO memories ({', '.join(columns)}) "
            f"SELECT {values} FROM memories WHERE id = ?",
            (planted, kept.id),
        )
    now = store.ranked_rows("alpha")
    assert len(now) == 2

    assert store.admitted_rows(now, admit_all) == now
    only_tools = lambda scopes, home, actor: "tools" in scopes  # noqa: E731
    assert store.admitted_rows(now, only_tools) == now
    assert store.admitted_rows(now, lambda scopes, home, actor: False) == []
    assert {m.id for m in store.light_many(now)} == {kept.id, planted}


def test_inside_a_transaction_rows_are_admitted_from_their_columns(
    store: Store,
) -> None:
    _write_dense(store, 5, scopes=["tools"], home=GLOBAL_HOME)
    store.write(content="alpha noise", scopes=["noise"], home=GLOBAL_HOME)
    rows = store.ranked_rows("alpha")
    only_tools = lambda scopes, home, actor: "tools" in scopes  # noqa: E731
    outside = store.admitted_rows(rows, only_tools)
    assert len(outside) == 5

    with store._transaction():
        assert store.admitted_rows(rows, only_tools) == outside


# ---------------------------------------------------------------------------
# No search reads every record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "returns"),
    [
        ({"query": "alpha"}, True),
        ({"query": "nothing-holds-this-word"}, False),
        ({"query": "?!"}, False),
        ({"query": ""}, False),
        ({"query": "alpha", "since_prior_session": True}, False),
    ],
    ids=["a match", "no match", "no token", "empty", "since the prior session"],
)
async def test_no_search_reads_every_record(
    memory_dir: Path,
    store: Store,
    whole_store_reads: list[str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    returns: bool,
) -> None:
    _write_dense(store, 12, scopes=["tools"], home=GLOBAL_HOME)
    read: list[int] = []
    real_light = Store.light_many

    def light(self: Store, rowids: Any, **kwargs: Any) -> Any:
        read.append(len(list(rowids)))
        return real_light(self, rowids, **kwargs)

    monkeypatch.setattr(Store, "light_many", light)

    hits = _unwrap(await _call(_build_server(memory_dir), "memory_search", **arguments))

    assert bool(hits) is returns
    assert whole_store_reads == []
    if not returns and arguments["query"] not in ("", "alpha"):
        assert sum(read) == 0, "a query that matches nothing reads no record"


async def test_the_changes_since_the_prior_session_are_read_through_the_index(
    memory_dir: Path, store: Store, whole_store_reads: list[str]
) -> None:
    old = store.write(content="alpha before", scopes=["tools"], home=GLOBAL_HOME)
    boundary = datetime.now(timezone.utc) + timedelta(seconds=1)
    rows = store.rows_updated_since(boundary)
    assert rows == []

    newer = store.write(content="beta after", scopes=["tools"], home=GLOBAL_HOME)
    since = store.rows_updated_since(old.updated)
    assert {m.id for m in store.light_many(since)} == {old.id, newer.id}
    assert [m.id for m in store.light_many(since)][0] == newer.id
    assert whole_store_reads == []


def test_an_empty_query_pools_the_newest_admitted_records(
    store: Store, whole_store_reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pool_module, "_FIRST_READ", 4)
    written = [
        store.write(
            content=f"note {i}",
            scopes=["noise" if i % 2 else "tools"],
            home=GLOBAL_HOME,
        )
        for i in range(12)
    ]
    newest_first = sorted(written, key=lambda m: (m.updated, m.id), reverse=True)

    pool = resolve_search_pool(store, "", scopes=["tools"], size=3)

    wanted = [m.id for m in newest_first if "tools" in m.scopes][:3]
    assert [m.id for m in pool.memories] == wanted
    assert whole_store_reads == []
    everything = resolve_search_pool(store, "   ")
    assert [m.id for m in everything.memories] == [m.id for m in newest_first]


# ---------------------------------------------------------------------------
# The statistics are the collection's
# ---------------------------------------------------------------------------


def _spy_idf(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, float], float]]:
    seen: list[tuple[dict[str, float], float]] = []
    real = search_module.compute_idf

    def spy(memories: Any, **kwargs: Any) -> Any:
        body_idf, scope_idf, avgdl = real(memories, **kwargs)
        seen.append((body_idf, avgdl))
        return body_idf, scope_idf, avgdl

    monkeypatch.setattr(search_module, "compute_idf", spy)
    return seen


async def test_a_rare_term_is_priced_off_the_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IDF the scorer uses is the collection's, not the pool's. The
    query is one rare term, so the pool is its match set and every row of
    it holds the term: priced off the pool, 3 of 3 gives an IDF of about
    0.13; over the collection, 3 of 63 gives about 2.9."""
    store = Store(tmp_path)
    for i in range(60):
        store.write(
            content=f"alpha common filler-{i}", scopes=["tools"], home=GLOBAL_HOME
        )
    for i in range(3):
        store.write(content=f"alpha flock rare-{i}", scopes=["tools"], home=GLOBAL_HOME)
    seen = _spy_idf(monkeypatch)

    await _call(_build_server(tmp_path), "memory_search", query="flock", max_results=5)

    assert seen, "BM25 never ran"
    flock_idf = max(body_idf.get("flock", 0.0) for body_idf, _ in seen)
    assert flock_idf > 2.0, f"'flock' priced at IDF {flock_idf:.3f}: the pool's"


async def test_the_parts_of_a_joined_term_are_priced_off_the_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A joined query token that matches through the conjunctive fallback
    ('zephyr-quartz' against bodies spelling it 'zephyr quartz') is priced
    by its parts, so the statistics are fetched for the parts too."""
    store = Store(tmp_path)
    for i in range(60):
        store.write(
            content=f"alpha common filler-{i}", scopes=["tools"], home=GLOBAL_HOME
        )
    for i in range(3):
        store.write(
            content=f"zephyr quartz spaced-{i}", scopes=["tools"], home=GLOBAL_HOME
        )
    store.write(content="zephyr-quartz hyphenated", scopes=["tools"], home=GLOBAL_HOME)
    seen = _spy_idf(monkeypatch)

    await _call(
        _build_server(tmp_path), "memory_search", query="zephyr-quartz", max_results=5
    )

    assert seen, "BM25 never ran"
    zephyr_idf = max(body_idf.get("zephyr", 0.0) for body_idf, _ in seen)
    assert zephyr_idf > 2.0, f"part 'zephyr' priced at IDF {zephyr_idf:.3f}"


def _three_long_among_sixty_short(root: Path) -> Store:
    store = Store(root)
    for i in range(60):
        store.write(content=f"alpha short-{i}", scopes=["tools"], home=GLOBAL_HOME)
    padding = " ".join(f"word{j}" for j in range(80))
    for i in range(3):
        store.write(
            content=f"flock {padding} long-{i}", scopes=["tools"], home=GLOBAL_HOME
        )
    return store


async def test_the_length_average_is_the_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool for 'flock' is three long records among sixty short ones.
    BM25's length average over the pool would call each of them average;
    over the collection each is long, as it is."""
    store = _three_long_among_sixty_short(tmp_path)
    everything = store.load_all()
    _, _, collection = compute_idf(everything)
    _, _, pool_only = compute_idf([m for m in everything if "flock" in m.body])
    assert pool_only > 10 * collection
    seen = _spy_idf(monkeypatch)

    await _call(_build_server(tmp_path), "memory_search", query="flock")

    assert seen, "BM25 never ran"
    assert all(avgdl == pytest.approx(collection) for _, avgdl in seen)


async def test_a_store_with_no_digest_ranks_with_the_pools_average(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collection's lengths are the digest's. A search never builds
    it: where it is gone the ranking takes the pool's average, as it did
    before the digest carried lengths, and still answers."""
    store = _three_long_among_sixty_short(tmp_path)
    _, _, pool_only = compute_idf([m for m in store.load_all() if "flock" in m.body])
    for table in ("digest_records", "digest_tokens", "digest_state"):
        store.conn.execute(f"DROP TABLE {table}")
    store.close()
    seen = _spy_idf(monkeypatch)
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    reader = Store.open(tmp_path, allow_rekey=False)
    monkeypatch.setattr(Store, "digest_mark", lambda self, *, build=True: None)
    server = build_server(config=cfg, store=reader, state=SessionState())

    hits = _unwrap(await _call(server, "memory_search", query="flock"))

    assert len(hits) == 3
    assert seen and all(avgdl == pytest.approx(pool_only) for _, avgdl in seen)


# ---------------------------------------------------------------------------
# The pool ranks as the whole store does
# ---------------------------------------------------------------------------

_WORDS = (
    "harbor lantern quartz meadow copper violin anchor pepper orchid tunnel "
    "glacier saffron compass thistle walnut ember lagoon marble falcon cedar "
    "pylon ribbon sextant tundra velvet willow zenith basalt cobalt dahlia "
    "ferret garnet hammock ingot juniper kestrel"
).split()


def _ranked(memories: list[Memory], query: str, **kwargs: Any) -> list[Any]:
    return [
        (hit.id, pytest.approx(hit.score, rel=1e-9), list(hit.match_terms))
        for hit in run_search(memories, query, max_results=10, now=NOW, **kwargs)
    ]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_where_every_match_fits_the_hits_are_the_whole_stores(
    store: Store, seed: int
) -> None:
    """Random corpora of plain words: the pool, priced with the store's
    statistics, returns the ids, scores and matched terms a ranking of
    every record returns, priced with its own."""
    rng = random.Random(seed)
    for i in range(120):
        words = [rng.choice(_WORDS) for _ in range(rng.randint(4, 30))]
        store.write(
            content=" ".join(words) + f" entry{i}",
            scopes=[rng.choice(["tools", "notes", "infrastructure"])],
            home=GLOBAL_HOME,
        )
    everything = store.load_all()
    compared = 0
    for _ in range(25):
        query = " ".join(rng.sample(_WORDS, rng.randint(1, 3)))
        scopes = rng.choice([None, ["tools"], ["notes", "infrastructure"]])
        pool = resolve_search_pool(store, query, scopes=scopes)
        assert len(pool.memories) < POOL_SIZE, "premise: every match fits"
        got = _ranked(
            pool.memories,
            query,
            scopes=scopes,
            corpus_stats_provider=pool.corpus_stats_provider,
            hydrate=pool.hydrate,
        )
        assert got == _ranked(everything, query, scopes=scopes), (seed, query, scopes)
        compared += bool(got)
    assert compared >= 20, "premise: the queries rank hits"


@pytest.mark.parametrize(
    "text",
    [
        "Claude-Code reads feature_flags from the daemon's config on 3.12.1.",
        "naïve café déjà vu, and a C++ build under ~/src",
        "東京の天気 and ünïcödé tokens\tacross\nlines",
        "to be or not to be, that is the to-do",
        "",
    ],
)
def test_a_rows_index_text_splits_into_the_tokenizers_stream(text: str) -> None:
    """The pool reads each candidate's stream from its row's index text
    in place of tokenising its body."""
    stream = _expand_kebab(tokenize(text))
    stored = fts_index_text(text)
    assert (stored.split(" ") if stored else []) == stream

    search_module.clear_token_cache()
    memory = Memory(
        id=generate_ulid(),
        created=NOW,
        updated=NOW,
        scopes=["tools"],
        confidence="high",
        source="explicit-statement",
        body=text,
    )
    search_module.seed_memory_tokens(memory, stored)
    seeded = search_module._memory_tokens(memory)
    search_module.clear_token_cache()
    assert seeded == search_module._memory_tokens(memory)


# ---------------------------------------------------------------------------
# Only the returned hits are read in full
# ---------------------------------------------------------------------------


async def test_only_the_returned_hits_are_read_in_full(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_dense(store, 30, scopes=["tools"], home=GLOBAL_HOME)
    whole: list[list[str]] = []
    real = Store.load_many

    def load_many(self: Store, ids: list[str]) -> Any:
        whole.append(list(ids))
        return real(self, ids)

    monkeypatch.setattr(Store, "load_many", load_many)

    hits = _unwrap(
        await _call(
            _build_server(memory_dir), "memory_search", query="alpha", max_results=4
        )
    )

    assert len(hits) == 4
    assert whole == [[h["id"] for h in hits]]


# ---------------------------------------------------------------------------
# The probe ranks the pool a search ranks
# ---------------------------------------------------------------------------


async def test_the_stop_hook_and_a_search_draw_one_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-miss probe measures what production retrieval would
    have surfaced, so it draws its candidates as `memory_search` does:
    the same helper, the same size."""
    store = Store(tmp_path)
    for i in range(3):
        store.write(content=f"alpha beta filler-{i}", scopes=["tools"])
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    drawn: list[tuple[str, int]] = []
    real = pool_module.resolve_search_pool

    def spy(store_: Any, query: str, **kwargs: Any) -> Any:
        pool = real(store_, query, **kwargs)
        drawn.append((query, kwargs.get("size", POOL_SIZE)))
        return pool

    monkeypatch.setattr(pool_module, "resolve_search_pool", spy)

    run_audit(
        user_message="alpha beta",
        assistant_response="hi",
        session_id="sess-pool",
        config=cfg,
    )
    server = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    await _call(server, "memory_search", query="alpha beta")

    assert drawn == [("alpha beta", POOL_SIZE), ("alpha beta", POOL_SIZE)]
