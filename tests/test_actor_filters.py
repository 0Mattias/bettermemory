"""The `client` / `model` filters on memory_search and memory_list.

7.10.0 recorded who wrote a memory; these are the surfaces that select on
it. What the tests here pin, in the order the design leans on it:

* the rule itself lives in ONE place (`identity.actor_matches`), is exact
  and case-sensitive, and drops a record whose writer declared nothing;
* the SQL `WHERE` in `index.query` and that Python predicate return the
  SAME set, which is the property that lets the filter be spelled twice;
* the `WHERE` earns its place by spending the FTS candidate cap on rows
  that pass, rather than filtering a slice that already excluded them;
* the BM25 corpus-IDF denominator binds the filter too, so it stays the
  collection actually being ranked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory import index
from bettermemory.config import Config, StorageConfig
from bettermemory.identity import Actor, actor_matches
from bettermemory.search import candidate_admitted
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

CODE = Actor(client="claude-code", model="opus", sources={"client": "client-info"})
HERMES = Actor(client="hermes", model="sonnet", sources={"client": "env"})


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def server(memory_dir: Path, store: Store) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=store, state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_actor_matches_is_exact_and_case_sensitive() -> None:
    """No folding on either side. The SQL twin can only fold ASCII, so a
    case-insensitive rule here would make the two spellings disagree on
    precisely the non-ASCII names folding would be introduced for."""
    assert actor_matches(HERMES, client="hermes", model=None)
    assert not actor_matches(HERMES, client="Hermes", model=None)
    assert not actor_matches(HERMES, client="hermes ", model=None)


def test_actor_matches_ands_the_two_fields() -> None:
    assert actor_matches(HERMES, client="hermes", model="sonnet")
    assert not actor_matches(HERMES, client="hermes", model="opus")
    assert actor_matches(HERMES, client=None, model="sonnet")


def test_an_undeclared_writer_matches_no_filter_value() -> None:
    """The selection-vs-admission line. `repo_filter` passes an
    unlabelled memory because no origin means global; this must not,
    or "what did hermes write" answers with everything written before
    the writer was recorded at all."""
    assert not actor_matches(None, client="hermes", model=None)
    assert not actor_matches(None, client=None, model="sonnet")
    assert not actor_matches(Actor(), client="hermes", model=None)
    # …and an absent filter still admits it.
    assert actor_matches(None, client=None, model=None)


def test_candidate_admitted_reads_the_same_rule() -> None:
    def admitted(actor: Actor | None, client: str | None) -> bool:
        return candidate_admitted(
            ["tools"],
            None,
            actor,
            scope_filter=None,
            excluded=set(),
            repo_filter=None,
            worktree_filter=None,
            client_filter=client,
        )

    assert admitted(HERMES, "hermes")
    assert not admitted(CODE, "hermes")
    assert not admitted(None, "hermes")
    assert admitted(None, None)


# ---------------------------------------------------------------------------
# The index column and its WHERE
# ---------------------------------------------------------------------------


def test_the_row_carries_the_declared_actor(store: Store, memory_dir: Path) -> None:
    written = store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"])

    import sqlite3

    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        conn.row_factory = sqlite3.Row
        rows = {
            r["id"]: (r["actor_client"], r["actor_model"])
            for r in conn.execute("SELECT id, actor_client, actor_model FROM memories")
        }
    finally:
        conn.close()

    assert rows[written.id] == ("hermes", "sonnet")
    # The undeclared write stores NULL, never an empty string: "declared
    # no name" and "declared the empty name" must not become one state.
    other = [v for k, v in rows.items() if k != written.id]
    assert other == [(None, None)]


def test_query_selects_on_the_declared_client(store: Store, memory_dir: Path) -> None:
    a = store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    b = store.write(content="alpha two", scopes=["tools"], actor=CODE)
    c = store.write(content="alpha three", scopes=["tools"])

    def ids(**kwargs: Any) -> set[str]:
        return {mid for mid, _ in index.query(memory_dir, "alpha", **kwargs)}

    assert ids() == {a.id, b.id, c.id}
    assert ids(client="hermes") == {a.id}
    assert ids(client="claude-code") == {b.id}
    assert ids(model="sonnet") == {a.id}
    assert ids(client="hermes", model="opus") == set()
    # The undeclared row is in the unfiltered set and in no filtered one.
    assert c.id not in ids(client="hermes") | ids(client="claude-code")


def test_query_client_filter_is_case_sensitive(store: Store, memory_dir: Path) -> None:
    store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    assert index.query(memory_dir, "alpha", client="Hermes") == []


def test_corpus_document_frequencies_binds_the_actor_filter(
    store: Store, memory_dir: Path
) -> None:
    """The IDF denominator must be the collection about to be ranked. A
    filter the ranked set applies and this scan doesn't would price term
    rarity against memories the caller cannot retrieve."""
    store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"], actor=CODE)
    store.write(content="alpha three", scopes=["tools"])

    def _admit(scopes: list[str], origin: Any, actor: Actor | None) -> bool:
        return candidate_admitted(
            scopes,
            origin,
            actor,
            scope_filter=None,
            excluded=set(),
            repo_filter=None,
            worktree_filter=None,
            client_filter="hermes",
        )

    resolved = index.corpus_document_frequencies(memory_dir, ["alpha"], admit=_admit)
    assert resolved is not None
    size, body_df, _scope_df = resolved
    assert size == 1, "the denominator counts only what the filter admits"
    assert body_df["alpha"] == 1


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threshold", ["1", "10000"])
async def test_the_where_and_the_predicate_are_one_set(
    server: Any,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    threshold: str,
) -> None:
    """G1. `threshold=1` forces the FTS prefilter (the SQL `WHERE`);
    `threshold=10000` forces `load_all` (the Python predicate alone).
    Parametrised rather than asserted twice so the two paths cannot be
    given different expectations by hand."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", threshold)
    a = store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"], actor=CODE)
    store.write(content="alpha three", scopes=["tools"])

    hits = await _call(server, "memory_search", query="alpha", client="hermes")
    assert [h["id"] for h in hits] == [a.id]

    unfiltered = await _call(server, "memory_search", query="alpha")
    assert len(unfiltered) == 3


async def test_the_prefilter_cap_is_spent_on_eligible_rows(
    server: Any, store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4, and the whole reason the filter is also a SQL `WHERE`.

    The store is dominated by one writer whose bodies repeat the query
    term, so the 50-row candidate cap fills with that writer's memories
    on relevance alone. A post-cap filter would be handed 50 rows none of
    which are the rare writer's and return nothing, with the match
    sitting on disk. Reverting the `WHERE` in `index.query` turns this
    red, which is what makes it a test of the design and not of pytest.

    `auto_scope=False` is load-bearing here and not incidental. With it
    on, a repo filter is active, which arms `resolve_search_pool`'s
    cap-starvation guard; the guard then reloads the whole store and
    finds the rare row anyway. That second net is real and worth having,
    but it only exists when some OTHER post-cap filter happens to be
    active — so a gate that leaned on it would be measuring the guard
    rather than the `WHERE`, and would go on passing after the `WHERE`
    was deleted by anyone who ran it from outside a git checkout.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    for i in range(60):
        store.write(
            content=f"alpha alpha alpha alpha loud note {i}",
            scopes=["tools"],
            actor=CODE,
        )
    # One mention, buried in a long body: BM25 saturates term frequency and
    # penalises length, so this row sorts below all sixty and lands outside
    # a 50-row cap taken on relevance alone.
    filler = " ".join(f"padding{i}" for i in range(400))
    rare = store.write(
        content=f"quiet note alpha {filler}", scopes=["tools"], actor=HERMES
    )

    # The premise the gate rests on, asserted rather than assumed: without
    # the filter this row is NOT in the candidate slice.
    unfiltered = {mid for mid, _ in index.query(memory_dir, "alpha", max_results=50)}
    assert rare.id not in unfiltered, "the cap must actually exclude it"

    hits = await _call(
        server, "memory_search", query="alpha", client="hermes", auto_scope=False
    )
    assert [h["id"] for h in hits] == [rare.id]


async def test_search_is_unchanged_when_no_filter_is_passed(
    server: Any, store: Store
) -> None:
    store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"])

    hits = await _call(server, "memory_search", query="alpha")
    assert len(hits) == 2


async def test_search_undeclared_rows_match_no_client(
    server: Any, store: Store
) -> None:
    store.write(content="alpha one", scopes=["tools"])
    hits = await _call(server, "memory_search", query="alpha", client="hermes")
    assert hits == []


# ---------------------------------------------------------------------------
# memory_list
# ---------------------------------------------------------------------------


async def test_list_filters_by_client_on_both_branches(
    server: Any, store: Store
) -> None:
    a = store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"], actor=CODE)
    store.write(content="alpha three", scopes=["tools"])

    summaries = await _call(server, "memory_list", client="hermes")
    assert [row["id"] for row in summaries] == [a.id]

    bodies = await _call(server, "memory_list", client="hermes", with_bodies=True)
    assert [row["id"] for row in bodies] == [a.id]


async def test_list_row_carries_the_spellings_the_filter_matches(
    server: Any, store: Store
) -> None:
    """D8. An exact-match filter whose values cannot be discovered is a
    guessing game, so the cheap-triage row names the writer — and stays
    the shape it always was for a record that declared nobody."""
    store.write(content="alpha one", scopes=["tools"], actor=HERMES)
    store.write(content="alpha two", scopes=["tools"])

    rows = {row["summary"]: row for row in await _call(server, "memory_list")}
    assert rows["alpha one"]["actor"] == {"client": "hermes", "model": "sonnet"}
    assert "actor" not in rows["alpha two"]


async def test_list_undeclared_rows_match_no_client(server: Any, store: Store) -> None:
    store.write(content="alpha one", scopes=["tools"])
    assert await _call(server, "memory_list", client="hermes") == []
    assert await _call(server, "memory_list", model="opus") == []
