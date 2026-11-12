"""The store's digest answers what a full scan of the records answers.

`store_digest` exists so a write reads the few records that can score and
not the corpus. Two things are held here: every digest path returns exactly
what the scan it replaced returns, through every way a store changes; and a
write on a store with a current digest loads no corpus and tokenises no
stored body, which is what keeps its cost from growing with the store.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from bettermemory import scope_match, search_dedup, store_digest, supersession
from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool

WORDS = (
    "daemon port config store index token review recall hook search write "
    "claude-code feature_flags python-frontmatter retry timeout cache batch "
    "lock sqlite journal backup restore key ring signature verify drift"
).split()
STAMP = "\n\n_(consolidate --llm --from-transcript: turn 4)_"


def _body(rng: random.Random, words: int) -> str:
    picked = [rng.choice(WORDS) for _ in range(words)]
    picked.insert(rng.randrange(len(picked)), str(rng.choice((8080, 9443, 5432, 3.12))))
    return " ".join(picked).capitalize() + "."


def _fill(store: Store, rng: random.Random, count: int) -> list[str]:
    ids = []
    for index in range(count):
        body = _body(rng, rng.choice((6, 12, 30, 120)))
        if index % 17 == 0:
            body += STAMP
        scopes = [rng.choice(("projects:alpha", "projects:beta", "tools"))]
        ids.append(store.write(content=body, scopes=scopes).id)
    for memory_id in ids[::9]:
        store.tombstone(memory_id, reason="superseded")
    return ids


def _probes(store: Store, rng: random.Random) -> list[str]:
    stored = [m.body for m in store.load_all()] + [
        t.body for t in store.load_tombstones()
    ]
    probes = [_body(rng, words) for words in (5, 9, 14, 30, 90)]
    probes += [rng.choice(stored) for _ in range(4)]
    probes += [rng.choice(stored) + " It moved to port 9443 instead." for _ in range(4)]
    probes += [f"The daemon port changed from 8080 to {port}." for port in (9443, 5432)]
    # Past the claim size, so the supersession detector declines it.
    probes.append(" ".join(f"The {word} holds." for word in WORDS[:8]))
    return probes


def _dump(hits: list[Any]) -> list[dict[str, Any]]:
    return [hit.model_dump() for hit in hits]


def _assert_equal_to_scans(store: Store, probes: list[str]) -> None:
    active = store.load_all()
    removed = store.load_tombstones()
    assert scope_match.project_scopes_and_roots(store) == (
        scope_match.collect_project_scopes(active),
        scope_match.collect_project_roots(active),
    )
    for body in probes:
        assert _dump(search_dedup.find_similar_in(store, body)) == _dump(
            search_dedup.find_similar(body, active)
        )
        assert _dump(search_dedup.find_similar_tombstones_in(store, body)) == _dump(
            search_dedup.find_similar_tombstones(body, removed)
        )
        assert supersession.detect_supersession_in(
            store, body
        ) == supersession.detect_supersession(body, active)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_digest_paths_equal_the_scans(store: Store, seed: int) -> None:
    rng = random.Random(seed)
    _fill(store, rng, 160)
    assert store.digest_sync()
    _assert_equal_to_scans(store, _probes(store, rng))


def test_digest_follows_every_kind_of_change(store: Store) -> None:
    rng = random.Random(7)
    ids = _fill(store, rng, 60)
    probes = _probes(store, rng)
    _assert_equal_to_scans(store, probes)

    live = [m.id for m in store.load_all()]
    store.update(
        store.load_one(live[0]).model_copy(
            update={"body": "The daemon port moved from 8080 to 9443."}
        )
    )
    store.update(
        store.load_one(live[1]).model_copy(update={"scopes": ["projects:gamma"]})
    )
    store.tombstone(live[2], reason="wrong")
    store.restore(next(i for i in ids[::9]))
    store.write(
        content="A new record about the review and the recall hook.", scopes=["tools"]
    )
    _assert_equal_to_scans(store, probes + ["The daemon port moved from 8080 to 9443."])


def test_digest_catches_up_with_a_writer_that_does_not_keep_it(
    store: Store, memory_dir: Path
) -> None:
    """An older release writes records and never touches the digest. The
    digest reads what it missed from the log's mutation rows."""
    rng = random.Random(11)
    _fill(store, rng, 40)
    assert store.digest_sync()
    before = store.digest_mark()

    other = Store(memory_dir)
    written = other.write(content="The retry timeout is 30 seconds.", scopes=["tools"])
    other.tombstone(store.load_all()[0].id, reason="stale")
    other.close()

    assert store.digest_mark() not in (None, before)
    hits = search_dedup.find_similar_in(store, "The retry timeout is 30 seconds.")
    assert [hit.id for hit in hits if hit.relevance == "high"] == [written.id]
    _assert_equal_to_scans(store, _probes(store, rng))


def test_digest_is_rebuilt_when_dropped_or_built_under_other_rules(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(13)
    _fill(store, rng, 40)
    probes = _probes(store, rng)
    _assert_equal_to_scans(store, probes)

    with store._transaction() as conn:
        for table in store_digest._TABLES:
            conn.execute(f"DROP TABLE {table}")
    _assert_equal_to_scans(store, probes)

    monkeypatch.setattr(store_digest, "_FINGERPRINT", "another build's rules")
    with store._transaction() as conn:
        conn.execute("DELETE FROM digest_tokens")
    assert store.digest_sync()
    _assert_equal_to_scans(store, probes)


def test_unavailable_digest_falls_back_to_the_scans(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(17)
    _fill(store, rng, 40)
    monkeypatch.setattr(Store, "digest_mark", lambda self: None)
    assert (
        store.digest_candidates(frozenset({"daemon"}), removed=False, floor=0.4) is None
    )
    _assert_equal_to_scans(store, _probes(store, rng))


def test_candidates_are_a_superset_at_every_floor(store: Store) -> None:
    rng = random.Random(19)
    _fill(store, rng, 120)
    active = {m.id: m for m in store.load_all()}
    for body in _probes(store, rng):
        tokens = search_dedup.raw_content_tokens(body)
        for floor in (0.1, 0.15, 0.4, 0.75):
            docs = store.digest_candidates(tokens, removed=False, floor=floor)
            assert docs is not None
            found = {doc.id for doc in docs}
            scoring = {
                memory_id
                for memory_id, memory in active.items()
                if search_dedup._pairwise_content_jaccard(
                    tokens, search_dedup.raw_content_tokens(memory.body)
                )
                >= floor
            }
            assert scoring <= found


async def test_a_write_reads_no_corpus_and_tokenises_no_stored_body(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate on the write path's cost: with the digest current, a write
    loads neither every record nor every tombstone, and the bodies it
    tokenises do not grow with the store."""
    store = Store(memory_dir)
    rng = random.Random(23)
    _fill(store, rng, 300)
    assert store.digest_sync()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )

    def refuse(self: Store) -> Any:
        raise AssertionError("the write path loaded the corpus")

    monkeypatch.setattr(Store, "load_all", refuse)
    monkeypatch.setattr(Store, "load_tombstones", refuse)
    tokenised: list[str] = []
    real = search_dedup._raw_content_token_set

    def counting(text: str) -> set[str]:
        tokenised.append(text)
        return real(text)

    monkeypatch.setattr(search_dedup, "_raw_content_token_set", counting)
    search_dedup.clear_raw_token_set_cache()

    response = await call_tool(
        server,
        "memory_write",
        {
            "content": "The scheduler drains its queue every 45 seconds on the worker host.",
            "scopes": ["tools"],
        },
    )
    assert response["status"] == "committed"
    assert len(tokenised) < 30


def test_a_sessions_rows_are_read_through_the_session_index(store: Store) -> None:
    """Schema step 6: a session's recall state is rebuilt from its own
    rows, found through `log_by_session` and not by a walk of every row of
    those kinds. A store without the index reads the same rows."""
    for index in range(40):
        store.record_event("search", session=f"session-{index % 4}", query=f"q{index}")
    wanted = [
        (event["kind"], event.get("query"))
        for event in store.session_events_of_kinds("session-1", ["search"])
    ]
    assert len(wanted) == 10
    plan = " ".join(
        str(row[3])
        for row in store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT seq FROM log INDEXED BY log_by_session "
            "WHERE session = ? AND kind IN (?) ORDER BY seq",
            ("session-1", "search"),
        )
    )
    assert "log_by_session" in plan and "TEMP B-TREE" not in plan

    with store._transaction() as conn:
        conn.execute("DROP INDEX log_by_session")
    assert [
        (event["kind"], event.get("query"))
        for event in store.session_events_of_kinds("session-1", ["search"])
    ] == wanted
