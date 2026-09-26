"""Tests for server.py — tool registration and end-to-end behavior.

We exercise the registered tools via the SDK's `call_tool` rather than
spinning up the full stdio transport. The served surface is the nine
tools of bettermemory 9: seven single-purpose tools plus the `episode`
and `memory_admin` dispatchers, whose actions are exercised here the
way their former stand-alone tools were.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.search import fts_index_text
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from ._mcp import input_schema as _input_schema


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _plant(store: Store, body: str, scopes: list[str]) -> str:
    """Insert a memories row past the store's write path, the way a hand
    with the file would: the FTS trigger indexes it, so it is a search
    candidate, and only the pointer check into the log tells it apart.
    Mirrors the helper in tests/test_store.py."""
    from datetime import datetime, timezone

    from bettermemory import store as _store
    from bettermemory.models import Confidence, Memory, Source, generate_ulid

    now = datetime.now(timezone.utc)
    planted = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes,
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )
    columns = _store._record_columns(planted)
    columns.update(
        {
            "body_fts": fts_index_text(planted.body),
            "scopes_text": _store._scopes_text(planted.scopes),
            "scopes_fts": fts_index_text(" ".join(planted.scopes)),
            "filename": None,
            "provenance": _store.LOCAL,
            "links_json": "[]",
            "corroborations": 0,
            "last_corroborated": None,
            "log_mac": None,
        }
    )
    store.conn.execute(
        _store._UPSERT_MEMORY, tuple(columns[c] for c in _store._MEMORY_COLUMNS)
    )
    store.conn.commit()
    return planted.id


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


async def test_tools_registered(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "memory_search",
        "memory_show",
        "memory_write",
        "memory_update",
        "memory_remove",
        "memory_verify",
        "memory_record_use",
        "episode",
        "memory_admin",
    }
    assert names == expected


async def test_each_tool_has_description_and_input_schema(server: Any) -> None:
    tools = await server.list_tools()
    for tool in tools:
        assert tool.description, f"{tool.name} missing description"
        assert _input_schema(tool), f"{tool.name} missing inputSchema"


async def test_write_and_update_schemas_include_acknowledge_credential(
    server: Any,
) -> None:
    """The REGISTERED memory_write and memory_update tools' input schemas
    must expose `acknowledge_credential`. The SDK derives the schema from
    the `ToolHandlers` wrapper signature, and its pydantic arg-model
    silently DROPS any key the signature doesn't declare, so a
    handler-core parameter the wrapper omits is dead at the tool boundary:
    a client passing acknowledge_credential=True still gets the refusal.
    That is exactly how the hatch shipped dead once; this schema-level pin
    catches the wrapper/handler drift the handler-level tests can't see."""
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    for tool_name in ("memory_write", "memory_update"):
        props = _input_schema(by_name[tool_name])["properties"]
        assert "acknowledge_credential" in props, (
            f"{tool_name} input schema lost the acknowledge_credential escape hatch"
        )


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


async def test_write_then_show_roundtrip(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="Prefer hands-on code tutorials.",
        scopes=["learning-style"],
    )
    assert written["scopes"] == ["learning-style"]

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["id"] == written["id"]
    assert "code tutorials" in shown["body"]
    assert shown["provenance"] == "local"


async def test_search_finds_written_memory(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="The home lab is on subnet 10.42.",
        scopes=["infrastructure"],
    )
    hits = await _call(server, "memory_search", query="home lab subnet")
    # Structured returns under the "result" key for the SDK — handle both.
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert len(hits) >= 1
    assert "home lab" in hits[0]["snippet"]
    assert hits[0]["provenance"] == "local"


async def test_read_surfaces_carry_provenance(server: Any, memory_dir: Path) -> None:
    """A memory written through the tool reads `local` on every surface;
    a row inserted past the write path reads `unaccounted` on the same
    two (search hit, show). The label is the store's pointer check into
    the log, never the row's own column, so the planted row's stored
    label has no say in it."""
    written = await _call(
        server,
        "memory_write",
        content="The staging cluster runs postgres sixteen.",
        scopes=["infrastructure"],
    )
    planted = _plant(
        Store(memory_dir),
        "The staging cluster mirrors to a warm standby.",
        ["infrastructure"],
    )

    hits = await _call(
        server, "memory_search", query="staging cluster", auto_scope=False
    )
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert {hit["id"]: hit["provenance"] for hit in hits} == {
        written["id"]: "local",
        planted: "unaccounted",
    }

    shown = await _call(server, "memory_show", id=planted)
    assert shown["provenance"] == "unaccounted"


async def test_remove_excludes_from_search(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="Temporary fact.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="temporary fact"))
    assert [h["id"] for h in hits] == [written["id"]]
    await _call(
        server,
        "memory_remove",
        id=written["id"],
        reason="not durable",
    )

    hits = _unwrap(await _call(server, "memory_search", query="temporary fact"))
    assert all(h["id"] != written["id"] for h in hits)


async def test_disabled_scope_hidden_from_search(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="This is about project alpha.",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "memory_write",
        content="Generic tooling preference.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert any("projects:alpha" in h["scopes"] for h in hits)

    state = await _call(
        server, "memory_admin", action="disable_scope", scope="projects:alpha"
    )
    state = (
        state.get("result", state)
        if isinstance(state, dict) and "result" in state
        else state
    )
    assert "projects:alpha" in state["disabled_scopes"]

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert all("projects:alpha" not in h["scopes"] for h in hits)

    # Re-enabling brings them back.
    back = await _call(
        server, "memory_admin", action="enable_scope", scope="projects:alpha"
    )
    back = _unwrap(back)
    assert "projects:alpha" not in back["disabled_scopes"]
    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert any("projects:alpha" in h["scopes"] for h in hits)


# ---------------------------------------------------------------------------
# Validation surfaces
# ---------------------------------------------------------------------------


async def test_write_rejects_empty_scopes(server: Any) -> None:
    with pytest.raises(Exception, match="scopes must contain at least one entry"):
        await _call(server, "memory_write", content="x", scopes=[])


async def test_write_rejects_invalid_scope(server: Any) -> None:
    with pytest.raises(Exception, match="invalid scope"):
        await _call(server, "memory_write", content="x", scopes=["With Space"])


async def test_write_rejects_oversized_content(memory_dir: Path) -> None:
    """A memory_write body exceeding [behavior] max_content_bytes is
    rejected at the handler. The cap protects against a runaway model
    or hostile client filling the store with a multi-gigabyte body —
    the event log is already capped at 10 MB rotation, but the memory
    file itself was previously unbounded."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_content_bytes=200),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    big_body = "x" * 500
    with pytest.raises(Exception, match="max_content_bytes"):
        await _call(server, "memory_write", content=big_body, scopes=["tools"])


async def test_write_cap_disabled_when_zero(memory_dir: Path) -> None:
    """max_content_bytes=0 disables the cap — the legacy behaviour
    before this knob existed. Tested so a downstream config picking 0
    explicitly (e.g. a corpus of curated long-form bodies) doesn't
    accidentally trip the validator."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_content_bytes=0),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    big_body = "x" * 50_000
    result = await _call(server, "memory_write", content=big_body, scopes=["tools"])
    assert result is not None


async def test_update_rejects_oversized_content(memory_dir: Path) -> None:
    """memory_update applies the same cap — otherwise a caller could
    bypass the bound by writing under-cap and then updating to a
    multi-megabyte body."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_content_bytes=200),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    small = await _call(server, "memory_write", content="ok body", scopes=["tools"])
    memory_id = small["id"]
    big_body = "x" * 500
    with pytest.raises(Exception, match="max_content_bytes"):
        await _call(server, "memory_update", id=memory_id, content=big_body)


async def test_show_unknown_id_errors(server: Any) -> None:
    # The fixture id contains `O` (not a valid Crockford-base32 character),
    # so the store's ULID-validity gate fires before the lookup — the
    # actual error message is `invalid id`. Match either shape so a
    # future test that passes a structurally-valid-but-absent id (the
    # `no memory with id` message) still satisfies the assertion.
    with pytest.raises(Exception, match="invalid id|no memory with id"):
        await _call(server, "memory_show", id="01HXYZNOTAREALIDOK000000ZZ")


# ---------------------------------------------------------------------------
# memory_write category="user-inference" — a label, not a confirmation tier
#
# A claim *about* the user (preferences, beliefs, working style) commits
# like a fact. It used to stage pending regardless of config, which put
# a confirmation round trip in front of every stated preference; the
# label is what now keeps a stored inference distinguishable and
# correctable.
# ---------------------------------------------------------------------------


async def test_user_inference_commits_and_is_searchable_immediately(
    server: Any,
) -> None:
    """The write lands on the first call — no pending id, nothing to
    confirm — and the very next search finds it under its own label."""
    res = await _call(
        server,
        "memory_write",
        content="Prefers code-driven tutorials over prose walkthroughs.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "committed"
    assert "pending_id" not in res
    assert res["category"] == "user-inference"
    hits = _unwrap(await _call(server, "memory_search", query="tutorials"))
    assert [h["id"] for h in hits] == [res["id"]]
    assert hits[0]["category"] == "user-inference"
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["category"] == "user-inference"


async def test_default_category_fact_commits_immediately(server: Any) -> None:
    """Explicit category='fact' is the same path as omitting the parameter."""
    res = await _call(
        server,
        "memory_write",
        content="Project uses Postgres in prod.",
        scopes=["projects:demo"],
        category="fact",
    )
    assert res["status"] == "committed"
    assert "pending_id" not in res


async def test_invalid_category_raises(server: Any) -> None:
    with pytest.raises(Exception, match="category must be one of"):
        await _call(
            server,
            "memory_write",
            content="something",
            scopes=["tools"],
            category="not-a-valid-category",
        )


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


# ---------------------------------------------------------------------------
# memory_search(expand_top=True)
# ---------------------------------------------------------------------------


async def test_search_expand_top_inlines_body_for_high_relevance(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking troubleshooting cheatsheet",
        scopes=["infrastructure"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking troubleshooting",
            expand_top=True,
        )
    )
    assert len(hits) >= 1
    assert hits[0]["id"] == written["id"]
    assert hits[0]["relevance"] == "high"
    assert "body" in hits[0]
    assert "cheatsheet" in hits[0]["body"]


async def test_search_expand_top_no_body_when_only_low_relevance(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="python list comprehension notes",
        scopes=["tools"],
    )
    # 5 content tokens, only "python" matches → "low".
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="python kubernetes networking docker terraform",
            expand_top=True,
        )
    )
    assert len(hits) == 1
    assert hits[0]["relevance"] == "low"
    assert "body" not in hits[0]


async def test_search_expand_top_only_first_hit_gets_body(server: Any) -> None:
    a = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes one",
        scopes=["infrastructure"],
    )
    b = await _call(
        server,
        "memory_write",
        content="kubernetes networking notes two",
        scopes=["infrastructure"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking",
            expand_top=True,
        )
    )
    assert len(hits) == 2
    # Top hit gets a body; second hit must not.
    assert "body" in hits[0]
    assert "body" not in hits[1]
    # Both ids are present (which one is "top" can vary by recency tiebreak).
    ids = {hits[0]["id"], hits[1]["id"]}
    assert ids == {a["id"], b["id"]}


async def test_search_expand_top_default_false_keeps_old_shape(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking",
        )
    )
    assert len(hits) == 1
    assert "body" not in hits[0]


# ---------------------------------------------------------------------------
# `updated` timestamp surfaced in list / search
# ---------------------------------------------------------------------------


async def test_search_hit_includes_updated_timestamp(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="kubernetes networking"))
    assert len(hits) == 1
    assert "updated" in hits[0]
    assert hits[0]["updated"] == hits[0]["created"]


# ---------------------------------------------------------------------------
# memory_search(since_prior_session=True)
# ---------------------------------------------------------------------------


async def test_search_since_prior_session_empty_on_first_session(
    server: Any,
) -> None:
    """Fresh store + first session: no prior session boundary exists,
    so the filter returns empty regardless of how many hits would
    otherwise match."""
    await _call(
        server,
        "memory_write",
        content="kubernetes networking notes",
        scopes=["infrastructure"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes",
            since_prior_session=True,
        )
    )
    assert hits == []


async def test_search_since_prior_session_filters_to_post_boundary(
    memory_dir: Path,
) -> None:
    """Memories written in a prior session don't appear; memories
    written in the current session do. The boundary is the latest
    event ts from a different recorder session_id."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A: write a memory and record an event so a prior boundary
    # exists in the log.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a, "memory_write", content="written in session A", scopes=["tools"]
    )

    # Session B: a new server (fresh recorder = new session_id) writes
    # another memory after the boundary. The since_prior_session search
    # should surface only the session-B memory.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    written_b = await _call(
        server_b,
        "memory_write",
        content="written in session B about beta gophers",
        scopes=["tools"],
    )
    hits = _unwrap(
        await _call(
            server_b,
            "memory_search",
            query="gophers session",
            since_prior_session=True,
        )
    )
    ids = [h["id"] for h in hits]
    assert written_b["id"] in ids
    # Session-A memory must NOT appear — its `updated` predates the
    # boundary recorded for session B.
    assert all(h["body"] != "written in session A" for h in hits if "body" in h)


async def test_search_since_prior_session_excludes_boundary_memory(
    memory_dir: Path,
) -> None:
    """`since_prior_session=True` is *exclusive* at the boundary: a memory
    whose `updated` equals `prior_boundary` belongs to the prior session
    (the boundary IS that session's last event ts, per
    `find_prior_session_boundary`) and must not surface in the current
    session's delta. Mirrors `test_curation_counts_since_filter_is_exclusive_at_boundary`
    in tests/test_health.py — same concept, same answer across both surfaces
    (memory_search + curation_counts) the api docs pair together as the
    "what's new since last session" workflow. A naive inclusive `>=` would
    double-count the boundary memory across the two surfaces."""
    from bettermemory.health import find_prior_session_boundary
    from bettermemory.store import _iso

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A: write one memory. The `memory_write` recorder event lands
    # in the log with its own ts, which becomes session B's
    # `prior_boundary` below. Memory A's `updated` is set by `Store.write`
    # via a *separate* `utcnow()` call, so the two timestamps usually
    # differ by microseconds — to pin the equality edge we re-stamp A's
    # `updated` to exactly match the boundary after the fact.
    state_a = SessionState()
    server_a = build_server(config=cfg, store=Store(memory_dir), state=state_a)
    written_a = await _call(
        server_a,
        "memory_write",
        content="boundary memory written in session A",
        scopes=["tools"],
    )

    # Session B: a fresh recorder establishes a new session_id so events
    # from session A become "prior". Derive `prior_boundary` from the live
    # event log the same way `search.py` does.
    state_b = SessionState()
    store_b = Store(memory_dir)
    server_b = build_server(config=cfg, store=store_b, state=state_b)
    prior_boundary = find_prior_session_boundary(
        store_b.iter_events(),
        # Mirror the handler: the boundary is "latest event ts from a
        # session other than the current one". `build_server` propagates
        # the bare-`SessionState` path's `state.session_id` to the
        # recorder, so reading `state_b.session_id` gives us the same
        # value the handler will use.
        state_b.session_id,
    )
    assert prior_boundary is not None, "session A's write should have seeded a boundary"

    # Force memory A's `updated` to exactly equal `prior_boundary`. The
    # row is what `Store.load_all` re-reads, so one UPDATE on it is
    # sufficient — we deliberately bypass `Store.update` because that
    # helper bumps `updated` to `utcnow()` and would defeat the test's pin.
    store_b.conn.execute(
        "UPDATE memories SET updated = ? WHERE id = ?",
        (_iso(prior_boundary), written_a["id"]),
    )
    store_b.conn.commit()

    # Sanity: the rewrite landed.
    reloaded_a = next(m for m in store_b.load_all() if m.id == written_a["id"])
    assert reloaded_a.updated == prior_boundary

    # Now run the since_prior_session search from session B. With the
    # pre-fix inclusive `>=` filter, A would still surface; the fix
    # makes the comparison strict so the boundary memory drops out.
    hits = _unwrap(
        await _call(
            server_b,
            "memory_search",
            query="boundary",
            since_prior_session=True,
        )
    )
    ids = [h["id"] for h in hits]
    assert written_a["id"] not in ids, (
        "memory whose `updated` equals `prior_boundary` belongs to the prior "
        "session and must not appear in the since_prior_session delta — "
        "must agree with curation_counts' strict `<=` exclusion at the boundary"
    )


async def test_search_since_prior_session_records_boundary_on_event(
    memory_dir: Path,
) -> None:
    """The recorded `search` event carries `since_prior_session` and
    `prior_session_boundary` so an eval pass can correlate the filter
    state back to the cutoff that produced the result list."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    # Seed a prior session.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="seed", scopes=["tools"])

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_b,
        "memory_search",
        query="anything",
        since_prior_session=True,
    )

    search_events = [
        e for e in Store(memory_dir).iter_events() if e["kind"] == "search"
    ]
    assert search_events, "expected at least one search event"
    latest = search_events[-1]
    assert latest["since_prior_session"] is True
    assert latest["prior_session_boundary"] is not None


async def test_search_since_prior_session_default_false_keeps_old_shape(
    server: Any,
) -> None:
    """Default-off: when the flag isn't set, the search event omits
    a non-null boundary and behaviour matches the pre-flag code path
    (no candidate filtering)."""
    await _call(server, "memory_write", content="alpha gophers", scopes=["tools"])
    hits = _unwrap(await _call(server, "memory_search", query="gophers"))
    assert len(hits) == 1


async def test_search_since_prior_session_bypasses_fts_prefilter_cap(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: on stores large enough to engage the FTS5 prefilter,
    `since_prior_session=True` must NOT route through the prefilter — it
    caps candidates at 50 by query relevance, which would silently drop
    a newly-written matching memory ranked outside the cap. Fix: when
    `since_prior_session=True`, the handler calls `load_all` directly
    and applies the boundary filter to the full corpus."""
    # Force the FTS prefilter into a tiny-cap regime so we can engage
    # it without writing thousands of memories. Threshold of 1 means
    # any non-empty index triggers the prefilter path.
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A: write a bunch of memories that all match "zzz" so they
    # crowd the FTS prefilter's top-50 rows. These all predate the
    # session boundary, so the boundary filter would drop them anyway —
    # they exist purely to fill the prefilter cap. `force=True` skips
    # the similarity-dedup check that would otherwise stage these as
    # pending writes (they're intentionally near-duplicates).
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    for i in range(60):
        await _call(
            server_a,
            "memory_write",
            content=f"zzz crowding memory {i} with extra zzz padding",
            scopes=["tools"],
            force=True,
        )

    # Session B: a fresh recorder establishes a new session_id, so the
    # latest session-A event becomes the prior boundary. Write one new
    # memory that also matches "zzz". With the buggy prefilter path, the
    # 50-row cap on FTS results is filled with session-A memories that
    # rank similarly on the query; the session-B write may not crack
    # the top 50 — and even if it does, the boundary filter would still
    # be applied to only those 50 rows, not the full corpus.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    written_b = await _call(
        server_b,
        "memory_write",
        content="zzz arrival from session B with bonus zzz keyword",
        scopes=["tools"],
        force=True,
    )
    # Sanity: write should commit, not stage pending. The dedup-bypass
    # `force=True` is paired with the test's deliberately near-duplicate
    # content; if a future write-pipeline change re-routes this through
    # a pending path, the test should fail loudly here, not at the hit
    # assertion below.
    assert written_b.get("status") == "committed", written_b
    hits = _unwrap(
        await _call(
            server_b,
            "memory_search",
            query="zzz",
            since_prior_session=True,
        )
    )
    ids = [h["id"] for h in hits]
    # The session-B write must surface despite the prefilter cap.
    assert written_b["id"] in ids
    # No session-A memory should leak through — they all predate the
    # boundary.
    assert ids == [written_b["id"]]


async def test_search_since_prior_session_empty_query_returns_filtered_set(
    memory_dir: Path,
) -> None:
    """Regression: `memory_search(query="", since_prior_session=True)` is
    the natural "what's new since last session" usage. Pre-fix, the
    stopword early-return in `search()` fired before the boundary filter
    could surface anything, so this returned `[]` unconditionally. Fix:
    when `since_prior_session=True`, the handler passes
    `allow_empty_query=True` so `search()` returns the post-boundary
    candidates sorted by `updated` desc."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A: seed an event so a prior session boundary exists.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="seed in A", scopes=["tools"])

    # Session B: write three memories with distinct `updated` ordering.
    # Bump `updated` between writes by calling `memory_update` so the
    # sort key is unambiguous (the ULID-shaped id is the tiebreaker, so
    # even creation order would suffice — but explicit updates make the
    # ordering assertion test the intended behaviour, not an
    # implementation accident).
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    m1 = await _call(server_b, "memory_write", content="first new", scopes=["tools"])
    m2 = await _call(server_b, "memory_write", content="second new", scopes=["tools"])
    m3 = await _call(server_b, "memory_write", content="third new", scopes=["tools"])
    hits = _unwrap(
        await _call(
            server_b,
            "memory_search",
            query="",
            since_prior_session=True,
        )
    )
    ids = [h["id"] for h in hits]
    # All three session-B memories must appear; the session-A seed must
    # not (it predates the boundary).
    assert set(ids) == {m1["id"], m2["id"], m3["id"]}
    # Sorted by `updated` desc — newest write first.
    assert ids == [m3["id"], m2["id"], m1["id"]]


# ---------------------------------------------------------------------------
# memory_search — depends_on_resolved auto-pull
# ---------------------------------------------------------------------------


async def test_search_attaches_depends_on_resolved_for_linked_hit(
    server: Any,
) -> None:
    """A hit whose memory has a depends_on link surfaces the target's
    summary inline so the model can see the dependency chain without
    a memory_show round-trip."""
    target = await _call(
        server,
        "memory_write",
        content="auth uses JWT with 24h rolling refresh tokens",
        scopes=["projects:auth"],
    )
    dependent = await _call(
        server,
        "memory_write",
        content="rate limiter relies on auth identity",
        scopes=["projects:auth"],
    )
    # Add a depends_on link on `dependent` pointing at `target`.
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[
            {
                "type": "depends_on",
                "target_id": target["id"],
                "note": "needs identity from auth",
            }
        ],
    )

    hits = _unwrap(await _call(server, "memory_search", query="rate limiter"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    assert "depends_on_resolved" in hit
    resolved = hit["depends_on_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["id"] == target["id"]
    assert "JWT" in resolved[0]["summary"]
    assert resolved[0]["link_note"] == "needs identity from auth"


async def test_search_omits_depends_on_resolved_when_no_links(
    server: Any,
) -> None:
    """A hit with no links must not carry the field — absence-as-signal,
    matching the path_drift / commit_drift / recent_negative_outcomes
    contracts."""
    await _call(server, "memory_write", content="lonely memory", scopes=["tools"])
    hits = _unwrap(await _call(server, "memory_search", query="lonely"))
    assert len(hits) == 1
    assert "depends_on_resolved" not in hits[0]


async def test_search_depends_on_resolved_caps_per_hit(server: Any) -> None:
    """No more than 3 resolved targets surface per hit. The 4th+ link
    still exists on the memory; the model can call memory_show to see
    the full graph."""
    targets = []
    for i in range(5):
        t = await _call(
            server,
            "memory_write",
            content=f"dependency {i} body",
            scopes=["projects:foo"],
        )
        targets.append(t)
    dependent = await _call(
        server,
        "memory_write",
        content="the dependent memory",
        scopes=["projects:foo"],
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": t["id"]} for t in targets],
    )

    hits = _unwrap(await _call(server, "memory_search", query="dependent"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    assert "depends_on_resolved" in hit
    assert len(hit["depends_on_resolved"]) == 3  # capped


async def test_search_depends_on_resolved_skips_tombstoned_target(
    server: Any,
) -> None:
    """A tombstoned dependency must not surface in the resolved list —
    the link's target_id is preserved on the source memory but the
    auto-pull silently drops it. Inspect via memory_admin(action="tombstones")."""
    target = await _call(
        server,
        "memory_write",
        content="will be removed",
        scopes=["projects:foo"],
    )
    dependent = await _call(
        server,
        "memory_write",
        content="depends on a memory we'll remove",
        scopes=["projects:foo"],
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )
    await _call(server, "memory_remove", id=target["id"], reason="superseded")

    hits = _unwrap(await _call(server, "memory_search", query="depends remove"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    # Either omitted (the only link's target is gone) or empty list.
    assert "depends_on_resolved" not in hit or hit["depends_on_resolved"] == []


async def test_search_depends_on_resolved_skips_cross_project_target(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """A depended-on memory in a different project must NOT be inlined
    via `depends_on_resolved` when the search is auto-scoped to the
    caller's repo. The hit itself is correctly scope-filtered upstream,
    but the dependency auto-pull built its side-map from the pre-filter
    loader output and would otherwise resolve cross-project targets —
    leaking memory that the caller's auto-scope was explicitly hiding.
    """
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    origin_foo = Origin(
        cwd="/work/foo",
        repo="git@github.com:example/foo.git",
        branch="main",
        worktree_root="/work/foo",
    )
    origin_bar = Origin(
        cwd="/work/bar",
        repo="git@github.com:example/bar.git",
        branch="main",
        worktree_root="/work/bar",
    )

    def make_capture(origin: Origin):
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Write the cross-project target (memory_B) as if we were in repo bar.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_bar))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_bar))
    server_bar = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    target = await _call(
        server_bar,
        "memory_write",
        content="bar-side secret detail nobody else should see",
        scopes=["projects:bar"],
        category="fact",
    )

    # Switch to repo foo and write the dependent (memory_A) with a
    # depends_on link pointing at the cross-project target.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_foo))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_foo))
    server_foo = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    dependent = await _call(
        server_foo,
        "memory_write",
        content="foo-side note about cross-project dependency",
        scopes=["projects:foo"],
        category="fact",
    )
    await _call(
        server_foo,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )

    # Search from repo foo (auto_scope defaults True). The hit list
    # must contain A; A's depends_on_resolved must NOT contain B.
    hits = _unwrap(
        await _call(server_foo, "memory_search", query="cross-project dependency")
    )
    hit = next(h for h in hits if h["id"] == dependent["id"])
    resolved = hit.get("depends_on_resolved")
    # Either omitted (only link's target was filtered out) or list
    # without the cross-project target — both shapes are acceptable;
    # the leak case would put `target["id"]` into `resolved`.
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


async def test_search_depends_on_resolved_skips_disabled_scope_target(
    server: Any,
) -> None:
    """A depended-on memory in a session-disabled scope must NOT be
    inlined via `depends_on_resolved`. The hit list itself is
    already filtered via `excluded_scopes`, but the dependency
    auto-pull would otherwise resolve targets from the pre-filter
    loader output — undoing the disable via the dependency edge."""
    target = await _call(
        server,
        "memory_write",
        content="alpha target body for dependency lookup",
        scopes=["projects:alpha"],
        category="fact",
    )
    dependent = await _call(
        server,
        "memory_write",
        content="dependent memory in projects:beta scope",
        scopes=["projects:beta"],
        category="fact",
        acknowledge_scope_mismatch=True,
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )

    # Disable the target's scope. Searching for the dependent should
    # still find it (it lives in projects:beta), but the resolved
    # depends_on must not surface the disabled-scope target.
    await _call(server, "memory_admin", action="disable_scope", scope="projects:alpha")
    hits = _unwrap(await _call(server, "memory_search", query="beta dependent"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    resolved = hit.get("depends_on_resolved")
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


async def test_search_depends_on_resolved_targeted_loads_cross_topic_target(
    server: Any, monkeypatch: Any
) -> None:
    """Cross-topic depends_on auto-pull. Pre-fix: the side-map built
    from the FTS prefilter (cap 50 query-relevant rows) silently
    skipped depended-on targets whose body didn't match the query —
    exactly the auto-pull case that exists because B depends_on A
    when A provides context the query for B won't surface. Post-fix:
    `attach_depends_on_resolved` calls `store.load_one` for missing
    target ids and merges them into the side-map.

    Forcing the FTS prefilter via `BETTERMEMORY_INDEX_THRESHOLD=1`
    is what actually exercises the targeted-load path: at default
    threshold (500) the store falls back to `load_all` which
    includes A in the side-map even when the query doesn't match.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    target = await _call(
        server,
        "memory_write",
        content="xylophone zebra unrelated phrasing nobody queries for",
        scopes=["projects:foo"],
        category="fact",
    )
    dependent = await _call(
        server,
        "memory_write",
        content="rate limiter relies on the xylophone identity service",
        scopes=["projects:foo"],
        category="fact",
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[
            {
                "type": "depends_on",
                "target_id": target["id"],
                "note": "needs xylophone for identity",
            }
        ],
    )

    # Query for B's distinctive phrase ("rate limiter") — the FTS
    # prefilter surfaces B but not A (A's body has no overlapping
    # tokens with the query). The targeted-load path must pull A
    # in via `load_one` and surface it in B's `depends_on_resolved`.
    hits = _unwrap(await _call(server, "memory_search", query="rate limiter"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    assert "depends_on_resolved" in hit
    resolved = hit["depends_on_resolved"]
    assert {r["id"] for r in resolved} == {target["id"]}
    assert resolved[0]["link_note"] == "needs xylophone for identity"


async def test_search_depends_on_resolved_targeted_load_honors_disabled_scope(
    server: Any, monkeypatch: Any
) -> None:
    """Counterpart to bf92912 for the targeted-load path. A
    cross-topic `depends_on` target whose scope is session-disabled
    must NOT surface via the targeted-load fallback either — the
    same `excluded_scopes` filter that the side-map path applies has
    to run at load time, otherwise the targeted-load reintroduces
    the scope-leak."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    target = await _call(
        server,
        "memory_write",
        content="kryptonite alpha-scope only secret",
        scopes=["projects:alpha"],
        category="fact",
    )
    dependent = await _call(
        server,
        "memory_write",
        content="rate limiter in beta needs kryptonite",
        scopes=["projects:beta"],
        category="fact",
        acknowledge_scope_mismatch=True,
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )

    # Disable the target's scope. Query for B's distinctive phrase
    # (so A is NOT in the FTS prefilter — pure targeted-load path).
    await _call(server, "memory_admin", action="disable_scope", scope="projects:alpha")
    hits = _unwrap(await _call(server, "memory_search", query="rate limiter"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    resolved = hit.get("depends_on_resolved")
    # The targeted-load must drop A at load time before it joins the
    # side-map; either the key is omitted (only link's target was
    # filtered) or the list is non-empty without A.
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


async def test_search_depends_on_resolved_targeted_load_honors_cross_project_origin(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Counterpart to bf92912 for the targeted-load path against the
    auto-scope filter. A cross-topic `depends_on` target written
    from a different repo must NOT surface via the targeted-load
    fallback when the caller is auto-scoped to their own repo —
    `should_include_for_caller` re-runs at load time, mirroring
    the side-map path."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    origin_foo = Origin(
        cwd="/work/foo",
        repo="git@github.com:example/foo.git",
        branch="main",
        worktree_root="/work/foo",
    )
    origin_bar = Origin(
        cwd="/work/bar",
        repo="git@github.com:example/bar.git",
        branch="main",
        worktree_root="/work/bar",
    )

    def make_capture(origin: Origin):
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Write the cross-project target as repo bar. Body is deliberately
    # NOT matching the eventual query so the FTS prefilter cannot
    # rescue it — pure targeted-load path.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_bar))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_bar))
    server_bar = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    target = await _call(
        server_bar,
        "memory_write",
        content="bar-only payload nobody queries for",
        scopes=["projects:bar"],
        category="fact",
    )

    # Switch to repo foo and write the dependent with a depends_on
    # link pointing at the cross-project target.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_foo))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_foo))
    server_foo = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    dependent = await _call(
        server_foo,
        "memory_write",
        content="rate limiter foo-side note",
        scopes=["projects:foo"],
        category="fact",
    )
    await _call(
        server_foo,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )

    # Search from repo foo (auto_scope defaults True). B's query
    # ("rate limiter") does not match A's body — the targeted-load
    # is the only path that could surface A, and it must drop A
    # because the cross-project origin filter is applied at load.
    hits = _unwrap(await _call(server_foo, "memory_search", query="rate limiter"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    resolved = hit.get("depends_on_resolved")
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


async def test_search_depends_on_resolved_max_total_caps_across_hits(
    server: Any, monkeypatch: Any
) -> None:
    """The cross-hit `max_total=10` cap on `depends_on_resolved`
    survives the targeted-load fallback: even when every hit has
    many distinct cross-topic missing targets, the SUM of the
    `depends_on_resolved` list lengths across the result set must
    not exceed 10. Pins the cap that the new targeted-load path
    must respect; closes the side observation that no test
    previously locked this cross-hit cap down."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    # Build 5 hits, each with 5 distinct cross-topic depends_on
    # targets (25 targets total). Pre-fix: most would silently drop
    # because they're cross-topic. Post-fix: targeted-load surfaces
    # them, but the cap clamps the response to <=10.
    target_ids: list[str] = []
    for i in range(25):
        t = await _call(
            server,
            "memory_write",
            content=f"obscure-target-{i} body nobody queries",
            scopes=["projects:targets"],
            category="fact",
        )
        target_ids.append(t["id"])

    dependent_ids: list[str] = []
    for i in range(5):
        d = await _call(
            server,
            "memory_write",
            content=f"rate limiter dependent number {i}",
            scopes=["projects:targets"],
            category="fact",
        )
        # 5 distinct depends_on per dependent, all cross-topic.
        chunk = target_ids[i * 5 : (i + 1) * 5]
        await _call(
            server,
            "memory_update",
            id=d["id"],
            links=[{"type": "depends_on", "target_id": tid} for tid in chunk],
        )
        dependent_ids.append(d["id"])

    hits = _unwrap(
        await _call(server, "memory_search", query="rate limiter", max_results=10)
    )
    total_resolved = sum(
        len(h.get("depends_on_resolved", []))
        for h in hits
        if h["id"] in set(dependent_ids)
    )
    assert total_resolved <= 10


async def test_search_depends_on_resolved_targeted_load_skips_deleted_target(
    server: Any, monkeypatch: Any
) -> None:
    """`store.load_one` raises for tombstoned / missing targets; the
    targeted-load fallback must absorb that exception silently and
    leave the resolved list empty (or omit it entirely) — same
    behaviour as the pre-existing prefilter-miss skip. No crash, no
    half-loaded entry."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    target = await _call(
        server,
        "memory_write",
        content="xylophone target slated for removal",
        scopes=["projects:foo"],
        category="fact",
    )
    dependent = await _call(
        server,
        "memory_write",
        content="rate limiter depends on the doomed target",
        scopes=["projects:foo"],
        category="fact",
    )
    await _call(
        server,
        "memory_update",
        id=dependent["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )
    await _call(server, "memory_remove", id=target["id"], reason="superseded")

    # Query for B's phrase only — A is tombstoned, the targeted-load
    # path `load_one`s it and must absorb the TombstonedError.
    hits = _unwrap(await _call(server, "memory_search", query="rate limiter"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    # Same silent-skip contract as the existing tombstoned-target
    # test (line ~1033): key omitted OR list without the dead target.
    resolved = hit.get("depends_on_resolved")
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


# ---------------------------------------------------------------------------
# episode_write — sibling primitive for journal-shaped writes
# ---------------------------------------------------------------------------


async def test_episode_write_commits_with_minimum_payload(server: Any) -> None:
    """A body alone is enough — scopes/takeaway default to empty/None."""
    res = await _call(
        server,
        "episode",
        action="write",
        body="iteration 1 tried strategy A, broke at step 3",
    )
    assert res["status"] == "committed"
    assert res["takeaway"] is None
    assert res["scopes"] == []
    assert res["pruned_sessions"] == []  # fresh store, nothing to prune
    # Returned session_id matches the recorder's process-wide id.
    assert res["session_id"].startswith("sess_")


async def test_episode_write_persists_takeaway_and_scopes(server: Any) -> None:
    res = await _call(
        server,
        "episode",
        action="write",
        body="ran the fix, all green",
        takeaway="fix landed, regression suite clean",
        scopes=["projects:loops"],
    )
    assert res["status"] == "committed"
    assert res["takeaway"] == "fix landed, regression suite clean"
    assert res["scopes"] == ["projects:loops"]


async def test_episode_write_rejects_empty_body(server: Any) -> None:
    """Empty/whitespace-only body raises a clear error — the
    write surface enforces the same non-empty invariant `memory_write`
    does, just without the durability gate. The SDK wraps the
    underlying ValueError as a ToolError; both pass through `Exception`."""
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode", action="write", body="")
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode", action="write", body="   \n\t  ")


async def test_episode_write_rejects_oversized_body(memory_dir: Path) -> None:
    """An episode_write body exceeding [behavior] max_content_bytes is
    rejected at the handler — same cap as memory_write / memory_update.
    Episodes share the same fsynced-file storage path as memories; without
    this check a multi-MB body would land on disk uncapped, exposing the
    same DoS/disk-fill surface the memory write path closes. The error
    message mirrors the memory_write path so the MCP error surface stays
    uniform across both write tiers."""
    from bettermemory.config import BehaviorConfig

    cap = 200
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_content_bytes=cap),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Body one byte past the configured cap — derive from the config
    # field rather than hardcoding so the test stays correct if the
    # default ever shifts.
    big_body = "x" * (cap + 1)
    with pytest.raises(Exception, match="max_content_bytes"):
        await _call(server, "episode", action="write", body=big_body)


async def test_episode_write_under_cap_still_commits(memory_dir: Path) -> None:
    """Small body still commits even with a tight cap in place — the
    new size check must not regress the happy path. Pairs with the
    oversize-reject test above to pin both sides of the boundary."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_content_bytes=1_000),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server, "episode", action="write", body="under the cap")
    assert res["status"] == "committed"
    assert res["session_id"].startswith("sess_")


async def test_episode_write_rejects_oversized_takeaway(memory_dir: Path) -> None:
    """An episode_write takeaway exceeding [behavior] max_takeaway_bytes
    is rejected at the handler — same ValueError shape as the body cap,
    but the message names the takeaway cap so the operator knows which
    knob to turn. The cap predates the store: in the file layout a
    takeaway over 64 KB corrupted the record's frontmatter and the
    episode vanished from every read surface despite a committed write.
    The handler-boundary cap bounds the row before it is written."""
    from bettermemory.config import BehaviorConfig

    cap = 200
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_takeaway_bytes=cap),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Takeaway one byte past the configured cap; body stays small so
    # only the takeaway check is exercised. Derive the takeaway size
    # from the config field rather than hardcoding so the test stays
    # correct if the default ever shifts.
    big_takeaway = "x" * (cap + 1)
    with pytest.raises(Exception, match="max_takeaway_bytes"):
        await _call(
            server,
            "episode",
            action="write",
            body="small body",
            takeaway=big_takeaway,
        )


async def test_episode_write_under_takeaway_cap_still_commits(memory_dir: Path) -> None:
    """A small takeaway under the cap commits unchanged — the new
    takeaway validator must not regress the happy path. Pairs with the
    oversize-reject test above to pin both sides of the boundary."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_takeaway_bytes=1_000),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(
        server,
        "episode",
        action="write",
        body="under both caps",
        takeaway="short summary",
    )
    assert res["status"] == "committed"
    assert res["takeaway"] == "short summary"


async def test_episode_write_no_takeaway_still_commits(memory_dir: Path) -> None:
    """`takeaway=None` (the common path — handoff falls back to body
    line 1) must NOT trip the takeaway cap. Pins the `if takeaway is
    not None` guard in the handler so a future refactor that drops the
    guard (and validates `None` as a zero-byte string) still passes
    this test, while still rejecting a hostile-large takeaway via the
    sibling test above."""
    from bettermemory.config import BehaviorConfig

    # Tight cap to make sure the guard, not the cap value, is what
    # lets None through.
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_takeaway_bytes=10),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server, "episode", action="write", body="no takeaway here")
    assert res["status"] == "committed"
    assert res["takeaway"] is None


async def test_episode_write_rejects_oversized_scope_list(memory_dir: Path) -> None:
    """A scopes list exceeding [behavior] max_scopes_per_write is rejected
    at the handler. Same silent-data-loss class as the takeaway cap (t16):
    in the file layout roughly 2200 short scope names pushed the record's
    frontmatter past its ceiling and the episode vanished from every read
    surface despite the write returning `status="committed"`. The
    handler-boundary cap bounds the row before it is written."""
    from bettermemory.config import BehaviorConfig

    cap = 5
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_scopes_per_write=cap),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # cap + 1 short scope names — derive from the config field rather than
    # hardcoding so the test stays correct if the default ever shifts.
    big_scopes = [f"scope-{i}" for i in range(cap + 1)]
    with pytest.raises(Exception, match="max_scopes_per_write"):
        await _call(
            server,
            "episode",
            action="write",
            body="small body",
            scopes=big_scopes,
        )


async def test_episode_write_under_scope_cap_still_commits(memory_dir: Path) -> None:
    """A small scope list under the cap commits unchanged — the new
    scope-count validator must not regress the happy path. Pairs with the
    oversize-reject test above to pin both sides of the boundary."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_scopes_per_write=64),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(
        server,
        "episode",
        action="write",
        body="under all caps",
        scopes=["tools", "infrastructure"],
    )
    assert res["status"] == "committed"
    assert res["scopes"] == ["tools", "infrastructure"]


async def test_memory_write_rejects_oversized_scope_list(memory_dir: Path) -> None:
    """memory_write applies the same scope-list cap as episode_write.
    Defense-in-depth: the same silent-data-loss class existed on the
    memory tier, so an unbounded scope list would have erased the record
    from search / show despite a committed-looking write. Mirrors the
    discipline `_validate_content_size` set for byte caps — every
    list-shaped record field gets a count cap."""
    from bettermemory.config import BehaviorConfig

    cap = 5
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_scopes_per_write=cap),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    big_scopes = [f"scope-{i}" for i in range(cap + 1)]
    with pytest.raises(Exception, match="max_scopes_per_write"):
        await _call(
            server,
            "memory_write",
            content="some durable fact",
            scopes=big_scopes,
        )


async def test_memory_update_rejects_oversized_scope_list(memory_dir: Path) -> None:
    """memory_update applies the same cap — otherwise a caller could
    bypass the bound by writing under-cap then retag-updating to ~2200
    scopes. Same class as the body cap on update (which closes the
    write-small-then-update-big bypass for the content axis)."""
    from bettermemory.config import BehaviorConfig

    cap = 5
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(max_scopes_per_write=cap),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    small = await _call(
        server,
        "memory_write",
        content="some durable fact",
        scopes=["tools"],
    )
    memory_id = small["id"]
    big_scopes = [f"scope-{i}" for i in range(cap + 1)]
    with pytest.raises(Exception, match="max_scopes_per_write"):
        await _call(server, "memory_update", id=memory_id, scopes=big_scopes)


async def test_episode_model_scopes_count_validator() -> None:
    """Direct Pydantic test: `Episode(scopes=[…] * 65)` raises
    ValidationError. Model-layer defense-in-depth — a programmatic caller
    that bypasses the handler (sync pull, future in-process API,
    migration) still can't smuggle an unbounded scope list onto disk.
    The model-layer cap is hardcoded at 64 to match the established
    verified_paths / verified_commits / verified_versions / links
    ceiling."""
    from datetime import datetime, timezone

    from bettermemory.models import Episode, generate_ulid
    from pydantic import ValidationError

    too_many_scopes = [f"s-{i}" for i in range(65)]
    with pytest.raises(ValidationError, match="scopes list capped at 64"):
        Episode(
            id=generate_ulid(),
            session_id="sess_test",
            created=datetime.now(timezone.utc),
            body="body",
            scopes=too_many_scopes,
        )


async def test_memory_model_scopes_count_validator() -> None:
    """Direct Pydantic test: `Memory(scopes=[…] * 65)` raises
    ValidationError. Same model-layer ceiling as Episode — applies
    symmetrically across the two tiers because the YAML-corruption
    failure mode is identical."""
    from datetime import datetime, timezone

    from bettermemory.models import Confidence, Memory, Source, generate_ulid
    from pydantic import ValidationError

    too_many_scopes = [f"s-{i}" for i in range(65)]
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="scopes list capped at 64"):
        Memory(
            id=generate_ulid(),
            created=now,
            updated=now,
            scopes=too_many_scopes,
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body="body",
        )


async def test_episode_model_scopes_at_cap_accepted() -> None:
    """Exactly 64 scopes is accepted — the cap is inclusive of the
    boundary. Pins the off-by-one: a regression that flips `>` to `>=`
    would reject 64-scope records, breaking the very ceiling
    verified_paths sets as the project's per-record list cap."""
    from datetime import datetime, timezone

    from bettermemory.models import Episode, generate_ulid

    at_cap_scopes = [f"s-{i}" for i in range(64)]
    ep = Episode(
        id=generate_ulid(),
        session_id="sess_test",
        created=datetime.now(timezone.utc),
        body="body",
        scopes=at_cap_scopes,
    )
    assert len(ep.scopes) == 64


async def test_memory_verify_caps_verified_lists_at_handler_boundary(
    server: Any,
) -> None:
    """Regression: `Store.mark_verified` writes via `model_copy(update=...)`,
    which Pydantic runs WITHOUT field validators — so the model-layer 64-entry
    cap on verified_* is bypassed on the verify path (the repro stored 500
    short paths). And per-entry length is uncapped, so long entries can
    bloat the record without bound. The memory_verify handler must enforce
    both a count cap and a per-item length cap itself, mirroring how
    `scopes` is guarded at the write handler. The memory must survive the
    rejections and still verify with a sane list.
    """
    res = await _call(
        server,
        "memory_write",
        content="a durable fact about the deployment pipeline",
        scopes=["tools"],
    )
    mid = res["id"]

    # Over the 64-entry count cap (model validator bypassed on this path).
    with pytest.raises(Exception, match="capped at|entries"):
        await _call(
            server,
            "memory_verify",
            id=mid,
            verified_paths=[f"/proj/file{i}.py" for i in range(65)],
        )

    # A single pathological over-length entry (would bloat the record).
    with pytest.raises(Exception, match="chars|cap"):
        await _call(
            server,
            "memory_verify",
            id=mid,
            verified_commits=["c" * 2000],
        )

    # The record survived both rejections and still verifies with a sane list.
    # The path must EXIST: `memory_verify` refuses attestations naming paths
    # this machine cannot stat, so a fabricated one would make this positive
    # control fail for a reason that has nothing to do with the caps.
    real = str(Path(__file__).resolve())
    ok = await _call(server, "memory_verify", id=mid, verified_paths=[real])
    assert ok["verified"] == mid
    assert ok["verified_paths"] == [real]


async def test_memory_verify_refuses_unstattable_attestation(server: Any) -> None:
    """`mark_verified` performed no verification of any kind — it stamped
    `last_verified_at` and copied the caller's lists verbatim — so a model
    could attest a path it never checked and the memory would then read
    `fresh` on evidence that did not exist. The read side cannot catch this
    alone: an absolute attested path is only existence-checked when the body
    also names it, so an attestation the prose never references is inert.

    The refusal is at the handler, not `Store.mark_verified`: the store is
    the persistence primitive, and `memory_verify` is the only production
    caller that passes attestations at all."""
    res = await _call(
        server, "memory_write", content="a fact about the build", scopes=["tools"]
    )
    mid = res["id"]

    with pytest.raises(Exception, match="do not exist on this machine"):
        await _call(
            server, "memory_verify", id=mid, verified_paths=["/no/such/path.py"]
        )

    # The refusal is total — no partial freshness bump on the record.
    shown = await _call(server, "memory_show", id=mid)
    assert shown.get("last_verified_at") in (None, "")

    # Control: a real path still verifies, so this is not a blanket refusal.
    ok = await _call(
        server, "memory_verify", id=mid, verified_paths=[str(Path(__file__).resolve())]
    )
    assert ok["verified"] == mid


async def test_memory_verify_absent_paths_exempt_from_existence_check(
    server: Any,
) -> None:
    """`verified_absent_paths` attests intentional ABSENCE, so non-existence
    IS the claim. Applying the existence check to it would invert the escape
    hatch the error message itself recommends into a permanent failure."""
    res = await _call(
        server, "memory_write", content="no vendor dir in this tree", scopes=["tools"]
    )
    ok = await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_absent_paths=["/deliberately/absent"],
    )
    assert ok["verified"] == res["id"]


async def test_episode_write_is_invisible_to_memory_iterators(
    server: Any, memory_dir: Path
) -> None:
    """Episodes live in a sibling table — memory_search and the memory
    iterators must not surface them."""
    await _call(
        server,
        "episode",
        action="write",
        body="this is an episode, not a memory",
    )
    assert Store(memory_dir).load_all() == []
    hits = _unwrap(await _call(server, "memory_search", query="episode memory not"))
    assert hits == []


async def test_episode_write_event_recorded_with_kind_episode_write(
    server: Any, memory_dir: Path
) -> None:
    """The recorder fires a dedicated `episode_write` event so the
    tool-usage rollup counts it independently from memory_write."""
    await _call(server, "episode", action="write", body="some takeaway")

    ep_events = [
        e for e in Store(memory_dir).iter_events() if e["kind"] == "episode_write"
    ]
    assert ep_events
    assert ep_events[-1]["session"].startswith("sess_")


async def test_episode_write_returns_swarm_id(server: Any) -> None:
    """episode(action="write") echoes swarm_id in its committed payload; a
    non-swarm write returns None."""
    res = await _call(
        server, "episode", action="write", body="x", swarm_id="sess_coord"
    )
    assert res["swarm_id"] == "sess_coord"
    res2 = await _call(server, "episode", action="write", body="y")
    assert res2["swarm_id"] is None


async def test_episode_write_swarm_id_is_readable_by_cohort(
    server: Any, memory_dir: Path
) -> None:
    """The cohort label lands on the row: the store's swarm read gathers
    every episode stamped with the coordinator's id and nothing else."""
    coord = "sess_coordinator1"
    a1 = await _call(
        server, "episode", action="write", body="a1", takeaway="one", swarm_id=coord
    )
    a2 = await _call(
        server, "episode", action="write", body="a2", takeaway="two", swarm_id=coord
    )
    await _call(server, "episode", action="write", body="unrelated")
    rows = Store(memory_dir).episodes_by_swarm(coord)
    assert [e.id for e in rows] == [a1["id"], a2["id"]]
    assert all(e.swarm_id == coord for e in rows)


# ---------------------------------------------------------------------------
# episode_handoff — first call at loop-iteration entry
# ---------------------------------------------------------------------------


async def test_episode_handoff_empty_when_no_prior_session(server: Any) -> None:
    """Fresh store, current session has the only events. Handoff
    returns prior_session_id=None + episodes=[] so the caller can
    branch on 'no baseline' vs. 'baseline exists but is empty'."""
    res = await _call(server, "episode", action="handoff")
    assert res["prior_session_id"] is None
    assert res["episodes"] == []


async def test_episode_handoff_surfaces_prior_session_takeaways(
    memory_dir: Path,
) -> None:
    """Two sessions: A writes episodes, B asks for a handoff. B sees
    A's takeaways and ids."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode",
        action="write",
        body="iter 1 — tried A, broke at step 3",
        takeaway="A blocks on auth header",
    )
    await _call(
        server_a,
        "episode",
        action="write",
        body="iter 2 — tried B, partial success",
        takeaway="B partial; needs retry",
    )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    assert res["prior_session_id"] is not None
    assert res["prior_session_id"].startswith("sess_")
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["A blocks on auth header", "B partial; needs retry"]
    # Takeaways by default (7.0.0): the body stays on disk unless asked
    # for, and every row names how the episode entered the store.
    assert all("body" not in e for e in res["episodes"])
    assert [e["provenance"] for e in res["episodes"]] == ["local", "local"]
    res = await _call(server_b, "episode", action="handoff", include_bodies=True)
    assert "iter 1" in res["episodes"][0]["body"]


async def test_episode_handoff_respects_max_episodes_cap(memory_dir: Path) -> None:
    """`max_episodes` defaults to 5 and caps at 50. When the prior
    session has more episodes than requested, surface the most recent
    slice (chronological within the surfaced window)."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    for i in range(7):
        await _call(
            server_a,
            "episode",
            action="write",
            body=f"iter {i}",
            takeaway=f"takeaway {i}",
        )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff", max_episodes=3)
    assert len(res["episodes"]) == 3
    # Most recent 3 episodes: iter 4, 5, 6 (oldest first within the slice).
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["takeaway 4", "takeaway 5", "takeaway 6"]


async def test_episode_handoff_walks_past_session_with_only_suppressed_scopes(
    memory_dir: Path,
) -> None:
    """When `disabled_scopes` hides every episode of the most-recent
    prior session, the auto-resolve walk treats that session as
    'wrote nothing' and adopts the next-older session instead. Mirrors
    the user mental model of `memory_admin(action="disable_scope")`:
    'rewind past the last X-session and surface what came before'."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Older session: tools episode (visible).
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode",
        action="write",
        body="A's tools work",
        takeaway="A on tools",
        scopes=["tools"],
    )

    # Most-recent session before the reader: all episodes in
    # projects:alpha (about to be suppressed).
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_b,
        "episode",
        action="write",
        body="B's alpha work A",
        takeaway="B on alpha A",
        scopes=["projects:alpha"],
    )
    await _call(
        server_b,
        "episode",
        action="write",
        body="B's alpha work B",
        takeaway="B on alpha B",
        scopes=["projects:alpha"],
    )

    # Reader session disables projects:alpha. The auto-resolve walk
    # should hop over server_b and adopt server_a's session.
    server_c = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_c, "memory_admin", action="disable_scope", scope="projects:alpha"
    )

    res = await _call(server_c, "episode", action="handoff")
    assert res["prior_session_id"] is not None
    assert len(res["episodes"]) == 1
    assert res["episodes"][0]["takeaway"] == "A on tools"


async def test_episode_handoff_filters_emit_under_explicit_prior_session_id(
    memory_dir: Path,
) -> None:
    """Explicit `prior_session_id` bypasses the candidate-walk, but the
    emit step must still gate episode bodies through `disabled_scopes`.
    A caller naming a session does NOT consent to override the
    per-session hide rule — that's the user's explicit declaration of
    what they want suppressed regardless of which session it lives in."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode",
        action="write",
        body="alpha-tagged",
        takeaway="alpha takeaway",
        scopes=["projects:alpha"],
    )
    await _call(
        server_a,
        "episode",
        action="write",
        body="tools-tagged",
        takeaway="tools takeaway",
        scopes=["tools"],
    )

    # Resolve A's session id from the store so we can pass it explicitly.
    ep_store = Store(memory_dir)
    a_session_id: str
    for sid in ep_store.episode_session_ids():
        eps = ep_store.episodes_by_session(sid)
        if any("alpha-tagged" in e.body for e in eps):
            a_session_id = sid
            break
    else:
        raise AssertionError("could not locate session A's id")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_b, "memory_admin", action="disable_scope", scope="projects:alpha"
    )

    res = await _call(
        server_b,
        "episode",
        action="handoff",
        prior_session_id=a_session_id,
    )
    # Explicit prior_session_id honored, but the alpha-tagged episode
    # is filtered out — only the tools-tagged one surfaces.
    assert res["prior_session_id"] == a_session_id
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["tools takeaway"]


async def test_episode_handoff_no_filter_when_disabled_scopes_empty(
    memory_dir: Path,
) -> None:
    """Regression pin: with no disabled scopes (default state), the
    auto-resolved session surfaces every episode, exactly as before
    the filter shipped."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode",
        action="write",
        body="alpha",
        takeaway="alpha take",
        scopes=["projects:alpha"],
    )
    await _call(
        server_a,
        "episode",
        action="write",
        body="tools",
        takeaway="tools take",
        scopes=["tools"],
    )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["alpha take", "tools take"]


async def test_episode_handoff_respects_explicit_prior_session_id(
    memory_dir: Path,
) -> None:
    """When the caller passes `prior_session_id`, the handler skips the
    event-log walk and reads directly from that session's episodes.
    Useful for subagent handoff where the parent's session_id is
    already known."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Sessions A and B both write episodes.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode", action="write", body="A's note", takeaway="from A")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode", action="write", body="B's note", takeaway="from B")

    # Session C explicitly asks for A's session id (not the most recent).
    server_c = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Resolve A's id from its episode row. The row persists session_id;
    # iterating the store's sessions and matching on the body is a
    # known-good way to identify it without poking into the server's
    # private session attributes.
    ep_store = Store(memory_dir)
    a_session_id: str
    for sid in ep_store.episode_session_ids():
        eps = ep_store.episodes_by_session(sid)
        if any("A's note" in e.body for e in eps):
            a_session_id = sid
            break
    else:
        raise AssertionError("could not locate session A's id")

    res = await _call(
        server_c, "episode", action="handoff", prior_session_id=a_session_id
    )
    assert res["prior_session_id"] == a_session_id
    assert len(res["episodes"]) == 1
    assert res["episodes"][0]["takeaway"] == "from A"


async def test_episode_handoff_filters_prior_session_by_caller_worktree(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Two worktrees of one repo sharing a memory root must not see each
    other's iteration takeaways through the auto-resolve path. Worktree
    A writes an event + episode; a fresh server in worktree B asks for
    a handoff with no explicit `prior_session_id` and gets the empty
    result. `memory_search` already enforces this via
    `should_include_for_caller`; the handoff has to mirror
    that or it becomes the cross-tree leak path.

    The caller's explicit-override semantic is preserved by
    `test_episode_handoff_respects_explicit_prior_session_id` above —
    this case only pins the auto-resolve filter."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/worktrees/repo-feature-x",
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root="/worktrees/repo-feature-x",
    )
    origin_b = Origin(
        cwd="/worktrees/repo-bug-fix",
        repo="git@github.com:example/repo.git",
        branch="bug-fix",
        worktree_root="/worktrees/repo-bug-fix",
    )

    # Patch capture_origin to return A's origin while server_a builds /
    # writes. The handlers re-resolve the symbol from the module on each
    # call, so re-assigning later for server_b is enough. `monkeypatch`
    # restores both bindings at test teardown so we don't leak the
    # fake into sibling tests.
    def make_capture(origin: Origin):
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode",
        action="write",
        body="iter 1 — A's worktree-local state",
        takeaway="feature-x branch: blocked on auth refactor",
    )

    # Now flip to worktree B and ask for the handoff. server_b inherits
    # the same memory root, the same event log (which carries A's
    # session_id), but its caller-origin says it's in a different
    # worktree of the same repo. The fix is what makes that case
    # surface NO prior session — without it, A's takeaway would leak in
    # as "what the prior session concluded" for B.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    # The cross-worktree case must look like "no prior session in this
    # worktree" from B's perspective — same shape as a fresh store.
    assert res["prior_session_id"] is None
    assert res["episodes"] == []


async def test_episode_handoff_skips_zero_episode_candidate_from_other_worktree(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """The zero-episode adoption branch must honor the same worktree
    contract as the episode-bearing branch. Worktree A records events
    (memory_write / memory_search) but never calls `episode_write` —
    its session_id surfaces in the event log with no episode files on
    disk. A fresh server in worktree B asks for a handoff; the tick-22
    fix says it must NOT adopt A's session_id as "the prior session
    in B's worktree".

    Since queue #28, A's events carry a `worktree_root` origin (here
    "/worktrees/repo-feature-x"), so this exercises the named-A !=
    named-B skip: `_worktrees_equal_strict` compares two distinct named
    worktrees of the same repo and rejects. (The legacy branch — a
    zero-episode candidate whose events lack `worktree_root` at all,
    which a named caller must also reject — is covered by
    test_episode_handoff_skips_zero_episode_legacy_candidate_no_worktree.)

    Pre-tick-22 the walk hit A's session, saw `candidate_eps == []`,
    and adopted unconditionally — a leak of A's session_id as B's
    "prior session", even though the bare ULID has no body to surface
    it still conflicts with the explicit "this worktree" contract
    that tick-2 established for sessions with episodes."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/worktrees/repo-feature-x",
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root="/worktrees/repo-feature-x",
    )
    origin_b = Origin(
        cwd="/worktrees/repo-bug-fix",
        repo="git@github.com:example/repo.git",
        branch="bug-fix",
        worktree_root="/worktrees/repo-bug-fix",
    )

    def make_capture(origin: Origin) -> Any:
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))

    # Worktree A: write a memory + run a search. Both record events
    # under A's session_id but neither creates an episode on disk. A
    # is therefore a "zero-episode session" from the episode_handoff
    # walk's perspective.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "memory_write",
        content="A wrote this fact",
        scopes=["tools"],
    )
    await _call(server_a, "memory_search", query="A's search")

    # Flip to worktree B and ask for the handoff. Pre-fix, the walk
    # would hit A's session_id from the event log, find zero episodes,
    # and adopt it unconditionally. Post-fix, the strict
    # None-only-matches-None rule treats A's unknown worktree as not
    # matching B's named worktree, so the walk continues past — and
    # since there's no older session, the result is the empty-store
    # shape.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    assert res["prior_session_id"] is None
    assert res["episodes"] == []


async def test_episode_handoff_skips_zero_episode_legacy_candidate_no_worktree(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Backward-compat fallback (queue #28): a zero-episode candidate
    whose events LACK `worktree_root` (legacy / pre-#28 events) must NOT
    be adopted by a caller in a named worktree.

    `Recorder.record` only stamps `worktree_root` when the captured
    origin has one, so an origin with `worktree_root=None` produces
    exactly the legacy event shape (no field on disk). The handoff then
    resolves the candidate's worktree to None and
    `_worktrees_equal_strict(None, named_B)` rejects — reproducing the
    conservative pre-#28 behavior. This is the branch whose dedicated
    guard evaporated when test 3042 moved A's events to carry a
    worktree; pin it explicitly so the fallback can't silently regress.
    """
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # A's origin has NO worktree_root -> its events carry no worktree
    # field, the legacy/pre-#28 shape.
    origin_a_legacy = Origin(
        cwd="/some/dir",
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root=None,
    )
    origin_b = Origin(
        cwd="/worktrees/repo-bug-fix",
        repo="git@github.com:example/repo.git",
        branch="bug-fix",
        worktree_root="/worktrees/repo-bug-fix",
    )

    def make_capture(origin: Origin) -> Any:
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    monkeypatch.setattr(
        handlers_module, "capture_origin", make_capture(origin_a_legacy)
    )
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a_legacy))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="A wrote this fact", scopes=["tools"])
    await _call(server_a, "memory_search", query="A's search")

    # Caller B is in a named worktree. The legacy candidate's worktree
    # resolves to None; the strict rule rejects None vs named, so B must
    # see the empty-store shape.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    assert res["prior_session_id"] is None
    assert res["episodes"] == []


async def test_episode_handoff_adopts_zero_episode_candidate_when_caller_has_no_worktree(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Companion to the cross-worktree skip test. A caller whose origin
    has no worktree_root (running outside any git checkout) DOES adopt
    a zero-episode candidate — under the strict None-only-matches-None
    rule, unknown == None matches when the caller is also None.

    This pins the "all-null state" branch tick-22 explicitly preserves:
    when neither side has worktree info, the legacy zero-episode
    adoption still fires so callers without a worktree get the
    `{prior_session_id: sess_xxx, episodes: []}` middle state the
    module docstring promises."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_none = Origin(
        cwd="/tmp/no-checkout",
        repo=None,
        branch=None,
        worktree_root=None,
    )

    def make_capture(origin: Origin) -> Any:
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_none))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_none))

    # Session A: records events under origin_none (no worktree) but
    # writes no episodes. Zero-episode session with caller-side None
    # worktree.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "memory_write",
        content="A's no-worktree fact",
        scopes=["tools"],
    )

    # A fresh server, still in the no-worktree state, asks for the
    # handoff. Pre-tick-22 this was the adopted behavior; tick-22
    # preserves it via `_worktrees_equal_strict(None, None) -> True`.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode", action="handoff")
    assert res["prior_session_id"] is not None
    assert res["prior_session_id"].startswith("sess_")
    # Zero episodes on disk means the "middle state" — session_id
    # surfaced, episodes empty.
    assert res["episodes"] == []


async def test_loop_iteration_end_to_end_pattern(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """End-to-end exercise of the loop-iteration pattern documented in
    SKILL.md and the server-level instructions block.

    Iteration A:
    1. Writes durable memory + a journal entry with a takeaway.

    Iteration B (a fresh server = fresh recorder session_id, simulating
    a /loop subprocess):
    2. Calls episode(action="handoff") — sees A's takeaway.
    3. Writes its own memory; memory_search(since_prior_session=True)
       returns only B's writes (not A's), pinning the filter's
       "what THIS session has changed since the last other-session
       activity" semantic.
    4. A second handoff still resolves A as the prior session and still
       surfaces A's takeaway: the journal is append-only, so reading it
       twice in one iteration returns the same rows.

    Pins the contract the docs / SKILL.md describe so a future refactor
    of either episode action surfaces immediately."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Pin both sessions to the SAME named worktree. Without this the test
    # captures the ambient cwd: inside a git checkout both servers get
    # the real (equal) toplevel, but from a non-git dir both get a None
    # worktree and the handoff adoption succeeds via None==None, which
    # would make the worktree-match assertion vacuous. Pinning a named
    # worktree forces the named == named match path.
    shared_origin = Origin(
        cwd="/worktrees/repo-loop",
        repo="git@github.com:example/repo.git",
        branch="loop",
        worktree_root="/worktrees/repo-loop",
    )

    def _capture(cwd: Any = None) -> Origin:
        return shared_origin

    monkeypatch.setattr(handlers_module, "capture_origin", _capture)
    monkeypatch.setattr(server_module, "capture_origin", _capture)

    # Iteration A: durable memory + journal entry.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "memory_write",
        content="alpha gophers run at 60fps when GC is tuned",
        scopes=["projects:alpha"],
    )
    ep_a = await _call(
        server_a,
        "episode",
        action="write",
        body="iter 1 — tuned GC, gophers cleared",
        takeaway="GC tuning fixed gopher frame drops",
    )

    # Iteration B: new session, picks up the handoff.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    handoff = await _call(server_b, "episode", action="handoff")
    assert handoff["prior_session_id"] == ep_a["session_id"]
    a_session_id = handoff["prior_session_id"]
    assert len(handoff["episodes"]) == 1
    assert handoff["episodes"][0]["id"] == ep_a["id"]
    assert handoff["episodes"][0]["takeaway"] == "GC tuning fixed gopher frame drops"

    # B writes its own memory. since_prior_session restricts to
    # memories updated AFTER the latest other-session event — A's
    # memory falls before that cut, this new write falls after.
    await _call(
        server_b,
        "memory_write",
        content="beta benchmark hits the same 60fps target post-tune",
        scopes=["projects:alpha"],
    )
    own_session_hits = _unwrap(
        await _call(
            server_b,
            "memory_search",
            query="alpha 60fps",
            since_prior_session=True,
        )
    )
    own_bodies = [h.get("snippet", "") for h in own_session_hits]
    # B's write is present.
    assert any("beta benchmark" in body for body in own_bodies)
    # A's pre-boundary memory is NOT — it sits before the boundary.
    assert not any("alpha gophers" in body for body in own_bodies)

    # Second handoff: A is still the prior session in this worktree and
    # its takeaway is still there to read.
    handoff_2 = await _call(server_b, "episode", action="handoff")
    assert handoff_2["prior_session_id"] == a_session_id
    assert [e["id"] for e in handoff_2["episodes"]] == [ep_a["id"]]


# ---------------------------------------------------------------------------
# E2 — session-tag floor episodes at episode_handoff entry
# ---------------------------------------------------------------------------


async def test_handoff_writes_floor_for_current_session(
    memory_dir: Path,
) -> None:
    """E2 regression: a fresh `episode_handoff` call writes a session-tag
    floor episode for the CURRENT session in the store. The floor anchors
    the worktree so a tick that crashes before `episode_write` is still
    discoverable by the next tick's handoff via the worktree filter."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode", action="handoff")

    ep_store = Store(memory_dir)
    # The current session's id is exposed via the recorder; resolve it
    # by walking the sessions with episodes. Exactly one floor should exist.
    session_ids = ep_store.episode_session_ids()
    assert len(session_ids) == 1, (
        f"expected exactly one session from a fresh handoff, got {session_ids}"
    )
    sid = session_ids[0]
    eps = ep_store.episodes_by_session(sid)
    assert len(eps) == 1, f"expected one floor episode, got {len(eps)}"
    assert eps[0].is_floor is True
    assert eps[0].takeaway is None
    assert eps[0].scopes == []


async def test_handoff_floor_write_is_idempotent_in_same_process(
    memory_dir: Path,
) -> None:
    """E2 idempotency: calling `episode_handoff` twice in the same
    process (same session_id) must NOT produce two floors. The second
    call sees the stored floor and skips the write."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode", action="handoff")
    await _call(server, "episode", action="handoff")

    ep_store = Store(memory_dir)
    session_ids = ep_store.episode_session_ids()
    assert len(session_ids) == 1
    eps = ep_store.episodes_by_session(session_ids[0])
    assert len(eps) == 1, (
        f"two handoffs in the same process should leave exactly one "
        f"floor; got {len(eps)} episodes"
    )
    assert eps[0].is_floor is True


async def test_handoff_skips_floor_when_real_takeaway_already_exists(
    memory_dir: Path,
) -> None:
    """E2 idempotency: a session that wrote a real takeaway via
    `episode_write` before calling `episode_handoff` should NOT get
    a floor — the real takeaway already anchors the session, and a
    floor would be redundant noise."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Write a real takeaway FIRST (unusual ordering — the protocol
    # documents handoff-first, but it's valid to write a takeaway
    # before issuing the handoff in a custom caller).
    await _call(
        server, "episode", action="write", body="real body", takeaway="real takeaway"
    )
    await _call(server, "episode", action="handoff")

    ep_store = Store(memory_dir)
    session_ids = ep_store.episode_session_ids()
    assert len(session_ids) == 1
    eps = ep_store.episodes_by_session(session_ids[0])
    # Just the real episode, no floor.
    assert len(eps) == 1
    assert eps[0].is_floor is False
    assert eps[0].takeaway == "real takeaway"


async def test_handoff_followed_by_episode_write_yields_floor_plus_real(
    memory_dir: Path,
) -> None:
    """E2 main flow: handoff writes a floor at entry; episode_write
    later appends a real takeaway. `episodes_by_session` returns both;
    the next tick's handoff filters the floor from its takeaway
    summary AND uses the floor's worktree to match."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: handoff (writes floor) → episode_write (real takeaway).
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode", action="handoff")
    await _call(
        server_t,
        "episode",
        action="write",
        body="real iteration body",
        takeaway="real iteration takeaway",
    )

    ep_store = Store(memory_dir)
    session_ids = ep_store.episode_session_ids()
    assert len(session_ids) == 1
    eps = ep_store.episodes_by_session(session_ids[0])
    # Floor + real — two episodes.
    assert len(eps) == 2
    assert eps[0].is_floor is True  # Floor written first
    assert eps[1].is_floor is False  # Real takeaway written second
    assert eps[1].takeaway == "real iteration takeaway"

    # Tick T+1: a fresh server in the same process. Handoff should
    # surface T's real takeaway and NOT the floor.
    server_t1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_t1, "episode", action="handoff")
    assert res["prior_session_id"] is not None
    takeaways = [e["takeaway"] for e in res["episodes"]]
    # Exactly the real takeaway — no floor leaked into the summary.
    assert takeaways == ["real iteration takeaway"]
    # No crash signal — the real takeaway is present.
    assert "note" not in res


async def test_handoff_crash_recovery_floor_only_session_adopted_as_prior(
    memory_dir: Path,
) -> None:
    """Rewind contract (episode-handoff-chain), no-older-real branch:
    tick T calls `episode_handoff` then crashes BEFORE `episode_write`,
    and there is NO older real session behind it. T+1 builds a fresh
    server (new recorder/session_id) and calls `episode_handoff`. T is
    the immediately-prior worktree session and it is floor-only, so the
    walk rewinds looking for an older takeaway — finds none — and falls
    back to surfacing T itself as `prior_session_id` with an EMPTY
    episodes list plus the honest soft note.

    Pre-E2 (historical): T had ZERO episodes on disk; T+1's handoff hit
    the zero-episode branch and (in a real worktree) walked past T,
    dropping it. The unconditional entry floor fixed that: T+1's
    `list_by_session(T)` now returns the floor and the worktree filter
    matches. The episode-handoff-chain rewind then keeps this test
    honest — when no older real takeaway exists, the floor-only session
    IS still adopted as the prior id with `episodes: []` and the note.

    (The complementary rewind branch — a floor-only tick sitting on TOP
    of an older real session, where the walk surfaces that older
    takeaway — is pinned in
    tests/test_episode_handoff_guard.py::test_episode_handoff_rewinds_past_floor_only_to_older_real_takeaway.)
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: handoff runs (writes the entry floor), then we simulate a
    # crash — no episode_write. There is no older session behind it.
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode", action="handoff")
    # Simulated crash: tick T ends here, never calls episode_write.

    # Tick T+1: a fresh server in the same worktree resolves the prior
    # session. T is floor-only; with no older real session to rewind to,
    # T itself is surfaced as the prior id.
    server_t_plus_1 = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = await _call(server_t_plus_1, "episode", action="handoff")

    # The floor-only session T is adopted as the prior id (it anchored
    # its worktree on disk via the floor).
    assert res["prior_session_id"] is not None
    # No older real session exists, so the episodes list is empty — the
    # floor is filtered out of the takeaway summary and there is nothing
    # to rewind to.
    assert res["episodes"] == []
    # The honest soft note IS surfaced — distinguishes "immediately-prior
    # session left no takeaway" from "no prior session existed at all".
    assert "note" in res, (
        f"floor-only prior session should surface a soft note; got: {res!r}"
    )
    assert "crashed" in res["note"].lower()


async def test_handoff_floor_distinguishes_crash_vs_normal_empty(
    memory_dir: Path,
) -> None:
    """Companion to the crash-recovery test: a fresh handoff with no
    prior session at all returns `{prior_session_id: None, episodes: []}`
    and NO crash note. The `note` key only fires for floor-only prior
    sessions — empty results from "no prior session ever existed"
    don't trip it."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server, "episode", action="handoff")
    assert res["prior_session_id"] is None
    assert res["episodes"] == []
    assert "note" not in res, (
        f"first-ever handoff should not surface crash-signal note; got: {res!r}"
    )


async def test_handoff_floor_carries_caller_worktree_for_filter_match(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """E2 worktree-filter test: a floor-only session in worktree A is
    adopted by a caller in worktree A, NOT by a caller in worktree B.
    The fix's whole point is that the floor's origin.worktree_root is
    what the filter matches against."""
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    origin_a = Origin(
        cwd="/worktrees/repo-feature-x",
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root="/worktrees/repo-feature-x",
    )
    origin_b = Origin(
        cwd="/worktrees/repo-bug-fix",
        repo="git@github.com:example/repo.git",
        branch="bug-fix",
        worktree_root="/worktrees/repo-bug-fix",
    )

    def make_capture(origin: Origin) -> Any:
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    # Tick T in worktree A: handoff → crash (no episode_write). The
    # handoff writes a floor with origin_a.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode", action="handoff")
    # No episode_write — simulated crash before takeaway.

    # T+1 in worktree B: handoff should NOT see A's floor — A's
    # worktree is /repo-feature-x, B's is /repo-bug-fix.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res_b = await _call(server_b, "episode", action="handoff")
    # B doesn't adopt A's session — different worktree.
    assert res_b["prior_session_id"] is None
    assert res_b["episodes"] == []
    assert "note" not in res_b

    # Now a peer in worktree A asks for handoff. It DOES adopt T's
    # floor-only session, surfacing the crash-signal note.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))
    server_a_peer = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res_a = await _call(server_a_peer, "episode", action="handoff")
    assert res_a["prior_session_id"] is not None
    assert res_a["episodes"] == []
    # Crash-signal note fires for the floor-only adoption.
    assert "note" in res_a
    assert "crashed" in res_a["note"].lower()


async def test_handoff_floor_written_before_handoff_event_recorded(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """E2 ordering invariant: the floor write MUST happen BEFORE the
    `episode_handoff` event is recorded. Crash-safety analysis: if we
    crash after the event but before the floor, T+1's handoff sees
    T's event in the log, calls `list_by_session(T)` → empty, hits
    the zero-episode branch — exactly the bug E2 closes.

    Pin by stubbing `Recorder.record` to raise when called with
    `kind='episode_handoff'`; verify the floor exists in the store
    afterwards (proving it landed BEFORE the record call)."""
    from bettermemory.events import Recorder

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    real_record = Recorder.record
    crash_msg = "simulated crash between floor write and event record"

    def crashing_record(self: Recorder, kind: str, **fields: Any) -> None:
        if kind == "episode_handoff":
            raise RuntimeError(crash_msg)
        real_record(self, kind, **fields)

    monkeypatch.setattr(Recorder, "record", crashing_record)

    # The handoff call raises (mimicking a crash after the floor
    # write succeeded but during/before the event-record stage).
    # The SDK wraps the underlying RuntimeError in a ToolError —
    # `_call` propagates whichever shape lands.
    with pytest.raises(Exception, match=crash_msg):
        await _call(server, "episode", action="handoff")

    # CRITICAL: the floor exists in the store. If it doesn't, the ordering
    # invariant is violated and the fix doesn't help under the most
    # important crash window.
    ep_store = Store(memory_dir)
    session_ids = ep_store.episode_session_ids()
    assert len(session_ids) == 1, (
        "ordering invariant broken: floor must be stored even when "
        "the handoff event-record stage raises"
    )
    eps = ep_store.episodes_by_session(session_ids[0])
    assert len(eps) == 1
    assert eps[0].is_floor is True


# ---------------------------------------------------------------------------
# memory_update
# ---------------------------------------------------------------------------


async def test_update_changes_content_and_bumps_updated(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="initial body",
        scopes=["tools"],
    )

    # Sleep a hair so the `updated` timestamp can move forward measurably.
    import asyncio

    await asyncio.sleep(0.01)

    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        content="refined body with more detail",
    )
    assert res["status"] == "committed"
    assert res["id"] == written["id"]
    assert res["created"] == written["created"]  # preserved
    assert res["updated"] > written["updated"]  # bumped

    # Disk reflects the change.
    shown = await _call(server, "memory_show", id=written["id"])
    assert "refined body" in shown["body"]


async def test_update_replaces_scopes_when_given(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="x",
        scopes=["tools"],
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        scopes=["tools", "learning-style"],
    )
    assert set(res["scopes"]) == {"tools", "learning-style"}


async def test_update_checks_allowed_scopes_against_the_delta_only(
    memory_dir: Path,
) -> None:
    """`[scopes] allowed` governs what an update INTRODUCES, not what it keeps.

    `scopes` has REPLACE semantics, so keeping a scope means resubmitting
    it. Enforcing the allowlist over the whole submitted list therefore
    froze every row carrying a scope a tool stamped itself (an importer's
    provenance scope and type tag, which the user never typed): the update
    that resubmitted them was refused by name, and the only way to re-tag
    such a row was to drop the stamp.

    Third assertion is the one that keeps this from being a blanket
    exemption: a stamp-looking scope that is NOT already on the record is
    still refused, so no caller can borrow the carve-out to plant a false
    provenance tag on a hand-written memory.
    """
    from bettermemory.config import ScopesConfig

    provenance_stamp = "imported-from-claude-code"
    stamped = sorted([provenance_stamp, "type:project"])
    imported = Store(memory_dir).write(
        content="the demo project pins its formatter version in CI",
        scopes=[*stamped, "projects:demo"],
    )
    home_grown = Store(memory_dir).write(
        content="the demo project runs its type checker in strict mode",
        scopes=["projects:demo"],
    )
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        scopes=ScopesConfig(allowed=["projects:demo", "tools"]),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    # Adding an allowlisted scope to the imported row: the stamps ride along
    # unchanged and the edit commits.
    res = await _call(
        server,
        "memory_update",
        id=imported.id,
        scopes=[*stamped, "projects:demo", "tools"],
    )
    assert res["status"] == "committed"
    assert set(res["scopes"]) == {*stamped, "projects:demo", "tools"}

    # A genuinely new scope outside the list is still refused, and the
    # refusal names only it — the preserved stamps are not in the message.
    with pytest.raises(Exception, match="not in allowed list") as excinfo:
        await _call(
            server,
            "memory_update",
            id=imported.id,
            scopes=[*stamped, "projects:demo", "career"],
        )
    assert "career" in str(excinfo.value)
    for stamp in stamped:
        assert stamp not in str(excinfo.value), excinfo.value

    # The exemption is keyed on the record's own scopes, not on the stamp
    # names: the same string is refused on a memory that never carried it.
    with pytest.raises(Exception, match="not in allowed list") as excinfo:
        await _call(
            server,
            "memory_update",
            id=home_grown.id,
            scopes=["projects:demo", provenance_stamp],
        )
    assert provenance_stamp in str(excinfo.value)


async def test_update_changes_confidence(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="x",
        scopes=["tools"],
        confidence="medium",
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        confidence="high",
    )
    assert res["confidence"] == "high"


async def test_update_preserves_source(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="x",
        scopes=["tools"],
        source="user-correction",
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        content="refined",
    )
    # source is durable — it describes how the memory came to exist, not
    # how it was last edited.
    assert res["source"] == "user-correction"


async def test_update_combines_multiple_fields(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="initial",
        scopes=["tools"],
        confidence="low",
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        content="combined edit",
        scopes=["tools", "infrastructure"],
        confidence="high",
    )
    assert res["confidence"] == "high"
    assert set(res["scopes"]) == {"tools", "infrastructure"}
    shown = await _call(server, "memory_show", id=written["id"])
    assert "combined edit" in shown["body"]


async def test_update_rejects_no_fields(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="memory_update needs at least one"):
        await _call(server, "memory_update", id=written["id"])


async def test_update_rejects_empty_content(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="content must be non-empty"):
        await _call(server, "memory_update", id=written["id"], content="   ")


async def test_update_rejects_empty_scopes(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="scopes must contain at least one entry"):
        await _call(server, "memory_update", id=written["id"], scopes=[])


async def test_update_rejects_invalid_scope(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="invalid scope"):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            scopes=["With Space"],
        )


async def test_update_rejects_invalid_confidence(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="confidence must be one of"):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            confidence="extreme",
        )


async def test_update_unknown_id_errors(server: Any) -> None:
    # Same as `test_show_unknown_id_errors`: the test id carries an `O`
    # (illegal in Crockford-base32 ULIDs), so the validity gate raises
    # `invalid id` rather than `no memory with id`. Match either shape.
    with pytest.raises(Exception, match="invalid id|no memory with id"):
        await _call(
            server,
            "memory_update",
            id="01HXYZNOTAREALIDOK000000ZZ",
            content="anything",
        )


async def test_update_tombstoned_id_errors(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="superseded")
    with pytest.raises(Exception, match="was removed"):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            content="cannot update a corpse",
        )


# ---------------------------------------------------------------------------
# memory_update — W2 concurrent-edit CAS surface
#
# `Store.update` raises `ConcurrentUpdateError` when the on-disk
# `updated` differs from the caller's snapshot. The handler converts
# that to a structured `status="stale"` payload (mirroring the
# soft-refusal shapes in handlers/write.py) rather than a stringified
# ValueError, so a programmatic caller can branch on the status and
# fast-path the rebase via the carried `current_updated` timestamp.
# Direct deterministic store-level coverage lives in
# tests/test_concurrency.py; the handler test below pins the
# response shape specifically.
# ---------------------------------------------------------------------------


async def test_update_stale_snapshot_returns_structured_stale_response(
    server: Any,
) -> None:
    """W2: when `Store.update` raises `ConcurrentUpdateError`, the
    handler surfaces a `status="stale"` response carrying the current
    on-disk `updated` and a retry hint. The store-level CAS is
    exercised directly in test_concurrency.py; here we pin only the
    handler-boundary translation.
    """
    from unittest.mock import patch

    from bettermemory.store import ConcurrentUpdateError

    written = await _call(server, "memory_write", content="initial", scopes=["tools"])

    # Pretend a concurrent writer landed between our load_one and the
    # store-level CAS. Patch `Store.update` to raise the exception that
    # path would produce.
    from datetime import datetime, timezone

    current = datetime(2026, 5, 27, 12, 34, 56, tzinfo=timezone.utc)
    with patch(
        "bettermemory.store.Store.update",
        side_effect=ConcurrentUpdateError(written["id"], current),
    ):
        res = await _call(
            server,
            "memory_update",
            id=written["id"],
            content="edit on stale snapshot",
        )

    assert res["status"] == "stale"
    assert res["memory_id"] == written["id"]
    # ISO-formatted timestamp, must round-trip back to the same instant.
    # Normalized to the canonical `Z` suffix (the same serializer every other
    # handler timestamp uses), not the raw `+00:00` offset form.
    assert res["current_updated"] == current.isoformat().replace("+00:00", "Z")
    assert "Re-fetch with memory_show" in res["hint"]


# ---------------------------------------------------------------------------
# memory_verify — W8 concurrent-attestation CAS surface
#
# `Store.mark_verified` raises `ConcurrentUpdateError` when the on-disk
# `last_verified_at` differs from the handler's snapshot. The handler
# converts that to a structured `status="stale"` payload that mirrors
# the W2 `memory_update` stale shape exactly — same keys, same
# semantics — so a programmatic caller can branch on the status with
# the same code path and rebase via the carried `current_updated`.
# Direct deterministic store-level coverage lives in
# tests/test_concurrency.py; the handler test below pins the
# response shape specifically.
# ---------------------------------------------------------------------------


async def test_verify_stale_snapshot_returns_structured_stale_response(
    server: Any,
) -> None:
    """W8: when `Store.mark_verified` raises `ConcurrentUpdateError`,
    the handler surfaces a `status="stale"` response carrying the
    current on-disk `updated` and a retry hint. Mirror of the W2
    `memory_update` handler-boundary translation; the store-level CAS
    is exercised directly in test_concurrency.py.
    """
    from unittest.mock import patch

    from bettermemory.store import ConcurrentUpdateError

    written = await _call(
        server, "memory_write", content="verify race target", scopes=["tools"]
    )

    from datetime import datetime, timezone

    current = datetime(2026, 5, 27, 12, 34, 56, tzinfo=timezone.utc)
    with patch(
        "bettermemory.store.Store.mark_verified",
        side_effect=ConcurrentUpdateError(written["id"], current),
    ):
        res = await _call(
            server,
            "memory_verify",
            id=written["id"],
            # A real path: the handler's attestation-existence check runs
            # before `mark_verified`, so a fabricated one would be refused
            # there and the patched CAS this test exercises would never run.
            verified_paths=[str(Path(__file__).resolve())],
        )

    assert res["status"] == "stale"
    assert res["memory_id"] == written["id"]
    # Normalized to the canonical `Z` suffix (the same serializer every other
    # handler timestamp uses), not the raw `+00:00` offset form.
    assert res["current_updated"] == current.isoformat().replace("+00:00", "Z")
    assert "Re-fetch with memory_show" in res["hint"]


# ---------------------------------------------------------------------------
# memory_update — category retag (added 1.3.0)
#
# Pre-1.3 the only way to change a memory's category was remove+rewrite,
# which wasted the original `created` timestamp and littered .tombstones/
# with edits. The new `category` parameter on memory_update lets callers
# retag a `fact` memory as `ambient` (or back) without that round trip,
# which matters for legacy memories written before the `ambient` tier
# existed in 1.2.0. `user-inference` is deliberately rejected here: a
# claim about the user is filed through memory_write, not relabelled in
# place (`models._PROPOSABLE_CATEGORIES` records why the rule outlived
# the pending gate it was first written for).
# ---------------------------------------------------------------------------


async def test_update_can_retag_to_ambient(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="user identity-ish ambient memory",
        scopes=["personal-context"],
    )
    # Default category for a fresh write is `fact`.
    shown_before = await _call(server, "memory_show", id=written["id"])
    assert shown_before["category"] == "fact"

    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        category="ambient",
    )
    assert res["status"] == "committed"
    assert res["category"] == "ambient"

    # Persists across reload.
    shown_after = await _call(server, "memory_show", id=written["id"])
    assert shown_after["category"] == "ambient"


async def test_update_can_retag_back_to_fact(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="started ambient by mistake",
        scopes=["tools"],
        category="ambient",
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        category="fact",
    )
    assert res["category"] == "fact"


async def test_update_category_change_preserves_last_verified_at(
    server: Any,
) -> None:
    # category is metadata, not a body claim — verification stays valid.
    written = await _call(
        server,
        "memory_write",
        content="will be retagged",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        note="spot-checked before retag",
    )
    shown_before = await _call(server, "memory_show", id=written["id"])
    verified_before = shown_before["last_verified_at"]
    assert verified_before is not None

    await _call(
        server,
        "memory_update",
        id=written["id"],
        category="ambient",
    )
    shown_after = await _call(server, "memory_show", id=written["id"])
    assert shown_after["last_verified_at"] == verified_before


async def test_update_omitting_category_preserves_existing(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="ambient from the start",
        scopes=["tools"],
        category="ambient",
    )
    # Update something else; category should stay `ambient`.
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        confidence="high",
    )
    assert res["category"] == "ambient"


async def test_update_rejects_user_inference_category(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="category must be one of"):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            category="user-inference",
        )


async def test_update_rejects_unknown_category(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="category must be one of"):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            category="nonsense",
        )


async def test_update_category_only_satisfies_at_least_one_field(
    server: Any,
) -> None:
    # `category` should count as a real field for the
    # "needs at least one of …" guard — passing only `category` must
    # commit, not raise.
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        category="ambient",
    )
    assert res["status"] == "committed"


# ---------------------------------------------------------------------------
# Pin {"fact", "ambient"} membership of the update-handler category gate
# ---------------------------------------------------------------------------
#
# `handlers.update.memory_update` rejects any `category` retag whose
# value is outside `models._PROPOSABLE_CATEGORIES` — the same closed-
# protocol whitelist that gates the LLM-consolidation validators
# (`_validate_demote` / `_validate_propose_new` in `llm.py`, pinned
# in `tests/test_llm.py`). `user-inference` is deliberately excluded;
# the constant's comment in `models.py` says why. Existing coverage hits both members
# tangentially (`test_update_can_retag_to_ambient` exercises
# `ambient`, `test_update_can_retag_back_to_fact` exercises `fact`)
# but the tests below pin the contract explicitly — a deletion from
# the source set fails the corresponding parametrise case loudly
# instead of looking like an unrelated regression on a retag test.
#
# Negative-control: temporarily replacing `_PROPOSABLE_CATEGORIES`
# in `models.py` with `frozenset({"ambient"})` fails the membership
# guard plus this file's `[fact]` parametrise case AND both of
# `tests/test_llm.py`'s `[fact]` validator parametrise cases (the
# constant is shared); replacing with `frozenset({"fact"})` mirrors
# the failure across the `[ambient]` cases. Reverted to
# `frozenset({Category.FACT.value, Category.AMBIENT.value})`.

# Hardcoded so a deletion from `_PROPOSABLE_CATEGORIES` causes the
# corresponding parametrise case to fail (parametrising off the
# frozenset itself would just drop the case, silently). The
# membership guard ensures additions still require touching this
# list. Mirrors the same constant in `tests/test_llm.py`; the two
# guards live independently because the two pinning surfaces
# (test_server's MCP-server fixture vs. test_llm's parse_and_validate
# unit) shouldn't share imports beyond the production module.
_EXPECTED_UPDATE_PROPOSABLE_CATEGORIES: tuple[str, ...] = ("fact", "ambient")


def test_update_proposable_categories_match_frozenset() -> None:
    """Guard so additions to ``_PROPOSABLE_CATEGORIES`` are mirrored
    in the parametrise list below — otherwise a new tier joining the
    proposable set could ship without regression coverage on the
    `memory_update` retag gate. Paired with the same-named guard in
    `tests/test_llm.py` so additions to the shared constant must
    land regression cases on every production site that consumes
    it."""
    from bettermemory.models import _PROPOSABLE_CATEGORIES

    assert set(_EXPECTED_UPDATE_PROPOSABLE_CATEGORIES) == set(_PROPOSABLE_CATEGORIES)


@pytest.mark.parametrize("category", _EXPECTED_UPDATE_PROPOSABLE_CATEGORIES)
async def test_update_accepts_every_proposable_category(
    server: Any, category: str
) -> None:
    """Every member of ``_PROPOSABLE_CATEGORIES`` must be accepted by
    `memory_update`'s category-retag gate at `handlers/update.py`.
    Routes through the ``in``-membership lookup against
    `_PROPOSABLE_CATEGORIES`. A silent drop of either member here
    lets the handler raise ``ValueError("category must be one of
    …")`` for a legitimately formed retag request — the user's
    ``memory_update id=… category=fact`` (or ``=ambient``) call
    bounces with a confusing "must be one of" error citing a list
    that *contains* the value they asked for."""
    # Seed with the *other* category so the update is a real retag,
    # not a no-op (which the handler would still commit but doesn't
    # exercise the gate's surface meaningfully).
    seed_category = "ambient" if category == "fact" else "fact"
    written = await _call(
        server,
        "memory_write",
        content=f"to be retagged to {category}",
        scopes=["tools"],
        category=seed_category,
    )
    res = await _call(
        server,
        "memory_update",
        id=written["id"],
        category=category,
    )
    assert res["status"] == "committed", (
        f"memory_update retag to category={category!r} was rejected — "
        f"the handler's category gate has drifted from "
        f"_PROPOSABLE_CATEGORIES"
    )
    assert res["category"] == category
    # Persisted to disk.
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["category"] == category


# ---------------------------------------------------------------------------
# Content dedup at write time
#
# memory_write runs find_similar against the current store before staging or
# committing. High overlap returns status:"duplicate" instead of a write;
# medium overlap is surfaced as `related` but does not block; force=True
# overrides the check entirely.
# ---------------------------------------------------------------------------


async def test_dedup_blocks_identical_second_write(server: Any) -> None:
    """A second write with byte-identical content should be refused, not
    silently duplicated."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert first["status"] == "committed"

    second = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert second["status"] == "duplicate"
    assert len(second["matches"]) == 1
    assert second["matches"][0]["id"] == first["id"]
    assert second["matches"][0]["relevance"] == "high"
    assert "force=True" in second["hint"]


async def test_dedup_blocks_near_duplicate(server: Any) -> None:
    """High but not 1.0 overlap should still block."""
    await _call(
        server,
        "memory_write",
        content=("vendored python-frontmatter to drop the deprecated codecs.open call"),
        scopes=["tools"],
    )
    second = await _call(
        server,
        "memory_write",
        content=(
            "vendored python-frontmatter so we can drop the deprecated codecs.open"
        ),
        scopes=["tools"],
    )
    assert second["status"] == "duplicate"


async def test_dedup_allows_low_overlap_write(server: Any) -> None:
    """Two memories that share only stopwords / nothing meaningful should
    coexist without dedup interference."""
    await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx tls termination notes",
        scopes=["tools"],
    )
    second = await _call(
        server,
        "memory_write",
        content="user prefers tabs over spaces in the editor",
        scopes=["learning-style"],
    )
    assert second["status"] == "committed"
    assert "related" not in second


async def test_dedup_medium_overlap_returns_committed_with_related(
    server: Any,
) -> None:
    """Medium overlap should surface as `related` on a successful write —
    not a hard refusal, but the writer learns the adjacent entry exists."""
    first = await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx tls",
        scopes=["tools"],
    )
    second = await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx logging",
        scopes=["tools"],
    )
    assert second["status"] == "committed"
    assert "related" in second
    assert second["related"][0]["id"] == first["id"]
    assert second["related"][0]["relevance"] == "medium"


async def test_dedup_force_override_creates_new_memory(server: Any) -> None:
    """force=True bypasses the check — the writer has already inspected the
    matches and decided this entry is meaningfully different."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    second = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["tools"],
        force=True,
    )
    assert second["status"] == "committed"
    assert second["id"] != first["id"]


async def test_dedup_warns_on_previously_removed_memory(server: Any) -> None:
    """A removed memory's body still informs dedup. Re-writing the same fact
    after tombstoning surfaces `status="previously_removed"` with the
    original removal_reason — the lesson encoded in the removal isn't lost.
    The writer can either drop the write, restore the tombstone, or pass
    force=True to override."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=first["id"], reason="turned out wrong")

    second = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert second["status"] == "previously_removed"
    assert "removed_matches" in second
    assert len(second["removed_matches"]) >= 1
    match = second["removed_matches"][0]
    assert match["id"] == first["id"]
    assert match["relevance"] == "high-removed"
    assert match["removed_reason"] == "turned out wrong"
    assert "removed_at" in match


async def test_dedup_force_overrides_previously_removed(server: Any) -> None:
    """force=True is the explicit "I've read the removal_reason and the new
    write is meaningfully different" override for the tombstone-aware path,
    just like for the active-side dedup."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=first["id"], reason="testing")

    second = await _call(
        server, "memory_write", content=body, scopes=["tools"], force=True
    )
    assert second["status"] == "committed"
    assert second["id"] != first["id"]


async def test_dedup_active_high_match_wins_over_tombstone(server: Any) -> None:
    """When an active memory and a tombstone both match, the active path
    wins — there's a live record to update, which is more actionable than
    discussing the removed one."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    removed = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=removed["id"], reason="testing")

    # Re-create the active form via force=True.
    active = await _call(
        server, "memory_write", content=body, scopes=["tools"], force=True
    )

    # Now a third write of the same body should hit the active dedup.
    third = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert third["status"] == "duplicate"
    matches = third["matches"]
    assert any(m["id"] == active["id"] for m in matches)
    # No removed_matches surfaced when active path short-circuited.
    assert "removed_matches" not in third


async def test_dedup_match_carries_metadata(server: Any) -> None:
    """The matches list should give the writer enough to act — id, snippet,
    similarity, scopes — without an extra memory_show round-trip."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(
        server, "memory_write", content=body, scopes=["tools", "infrastructure"]
    )
    dup = await _call(server, "memory_write", content=body, scopes=["tools"])

    match = dup["matches"][0]
    assert match["id"] == first["id"]
    assert match["scopes"] == ["tools", "infrastructure"]
    assert "snippet" in match and match["snippet"]
    assert match["similarity"] >= 0.99


# ---------------------------------------------------------------------------
# Ordered-tuple pin for `_WRITE_GATES` — the WriteGate strategy chain
# orchestrated by `handlers/write.py:545`. ORDER IS LOAD-BEARING and the
# comment at `handlers/write.py:474-481` explicitly documents the
# rationale: transient before dedup so a transient parent doesn't route
# the writer to `memory_update`; scope-mismatch before dedup so a write
# tagged for a different scope doesn't get a duplicate hit; groundedness
# before dedup because (the comment's load-bearing example) a
# hallucinated write being reported as a "duplicate" of a real one is
# misleading; the two dedup gates last because everything before them
# rejects on the body alone.
#
# This is the HIGH-HAZARD pin in the closed-protocol audit-loop sweep.
# Hazard surface: a silent reorder violates the security / correctness
# invariant the source comment documents — specifically:
#   * dedup-before-groundedness lets a hallucinated write masquerade as
#     a duplicate of a real one (the comment's literal example);
#   * dropping `GroundednessGate` from the tuple silently lets
#     ungrounded writes through (the gate fires only because it's in
#     the chain);
#   * any reorder changes which gate fires first when multiple would
#     reject, changing the user-visible response.
#
# Contrast with the basic-shape membership guards landed in bde7602
# (`_REDACTED_TEXT_FIELDS`, `_PLACEHOLDER_PREFIXES`, `_INDEX_FILENAMES`)
# and the prior tick's pins (`_USE_OUTCOMES`, `_VALID_TRIGGERED_FROM`,
# `_RETRIEVAL_EVENT_KINDS`): those use `set(...) == set(...)` because
# order isn't load-bearing for `in`-membership lookups. This guard
# uses *tuple* equality on the per-instance type sequence because
# precedence between gates IS load-bearing — a silent reorder would
# pass a set-equality assertion while corrupting the precedence the
# write.py:474-481 comment documents. Tuple equality catches
# additions, deletions, AND reorders in one assertion.
#
# A future contributor reordering for performance (or adding a new
# gate, or deleting one) must update both the source tuple AND this
# expected tuple in the same commit. Treat any drift as a deliberate
# security/correctness decision that requires re-reading the
# write.py:474-481 rationale: would the new ordering still bounce a
# hallucinated write before reporting it as a duplicate? Still bounce
# a transient-parent write before routing the writer to update? If
# yes, update both. If unsure, don't reorder.
#
# Negative-control: swapping `DedupActiveGate` and `GroundednessGate`
# in `_WRITE_GATES` (a plausible "performance" reorder that puts
# cheaper checks first) fails
# `test_write_gates_match_expected_types_in_order` (tuple inequality
# — sequences differ at index 4 / 5). Revert restores green.
def test_write_gates_match_expected_types_in_order() -> None:
    """Guard so additions, deletions, AND reorders of ``_WRITE_GATES``
    (the ordered WriteGate strategy chain orchestrated by
    ``handlers/write.py:545``) are caught — uses *tuple* equality
    rather than set equality because gate precedence is load-bearing.

    The comment at ``handlers/write.py:474-481`` documents the
    invariant: ``CredentialGate`` fires FIRST so a secret is refused
    before any later gate records body-derived data alongside it in
    the event log; transient/user-claim/scope-mismatch/groundedness
    gates fire BEFORE dedup so (a) a hallucinated write can't
    masquerade as a duplicate of a real one, (b) a transient-parent
    write isn't routed to ``memory_update``, (c) a scope-mismatched
    write doesn't get a misleading duplicate hit, (d) a body
    re-categorized as ``user-inference`` isn't routed to
    ``memory_update`` on a mis-filed parent; the two dedup gates last,
    active before tombstone, because everything before them rejects on
    the body alone. A silent reorder breaks the security/correctness
    invariant the source comment documents.

    A future contributor reordering this tuple for performance must
    update both the source AND this expected tuple in the same
    commit, AND re-read the write.py:474-481 rationale to confirm
    the new ordering still preserves: hallucinated-before-dedup,
    transient-before-dedup, user-claim-before-dedup,
    scope-before-dedup, and dedup-last."""
    from bettermemory.handlers.write import (
        CredentialGate,
        DedupActiveGate,
        DedupTombstoneGate,
        GroundednessGate,
        ScopeMismatchGate,
        TransientGate,
        UserClaimGate,
        _WRITE_GATES,
    )

    expected: tuple[type, ...] = (
        CredentialGate,
        TransientGate,
        UserClaimGate,
        ScopeMismatchGate,
        GroundednessGate,
        DedupActiveGate,
        DedupTombstoneGate,
    )
    actual = tuple(type(g) for g in _WRITE_GATES)
    assert actual == expected, (
        f"_WRITE_GATES drifted from documented order at "
        f"handlers/write.py:474-481. Got {[t.__name__ for t in actual]}, "
        f"expected {[t.__name__ for t in expected]}. Re-read the source "
        f"comment before reordering — this guards a security/correctness "
        f"invariant, not a stylistic choice."
    )


# ---------------------------------------------------------------------------
# acknowledge_user_claim is ONE gate's escape hatch, not a chain bypass
#
# Written because of the blast radius the flag acquired the moment
# `UserClaimGate` landed: ten pre-existing fixtures across
# test_server_groundedness.py / test_server_v12_features.py /
# test_server_negative_outcomes.py had bodies that are genuinely claims
# about the user, and each had to start passing
# `acknowledge_user_claim=True` to keep testing its own axis. Those ten
# call sites now assume, without saying so, that the flag opens exactly
# one gate. If a later refactor widened it — the plausible shape is a
# GateContext change that lets one acknowledge_* field satisfy several
# gates, or a "skip the body gates when the caller already vouched for
# the body" shortcut — all ten would stay green while a credential-
# bearing body sailed through. Nothing else pins the narrowness: the
# per-gate tests in test_server_user_claims.py drive UserClaimGate with
# clean bodies, and the other gates' tests never set this flag.
#
# The four gates are chosen to straddle the chain position:
# credential (index 0) and transient (index 1) come BEFORE UserClaimGate
# (index 2), scope-mismatch (index 3) comes after, and groundedness
# (index 4) is covered by test_server_groundedness.py's
# `test_ungrounded_body_blocks_write`, which passes only this flag and
# still gets `ungrounded`.
# ---------------------------------------------------------------------------


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal.

    Mirrors the helper in test_server_credentials.py — a literal AWS-key
    shape checked into a test file trips secret scanners on every clone.
    """
    return "".join(parts)


async def test_acknowledge_user_claim_does_not_bypass_earlier_gates(
    server: Any,
) -> None:
    """Credential and transient both fire ahead of `UserClaimGate`, so
    acknowledging the user-claim axis must not move their verdicts.

    The credential half is the one with teeth: a body that is BOTH a
    claim about the user and a live secret is a realistic model write
    ("the user's deploy key is AKIA…"), and the store is plain-text
    markdown that `sync` pushes across hosts."""
    aws = _shaped("AKIA", "IOSFODNN7EXAMPLE")

    secret_claim = await _call(
        server,
        "memory_write",
        content=f"The user prefers keeping the prod key {aws} in the shell profile.",
        scopes=["infrastructure"],
        acknowledge_user_claim=True,
    )
    assert secret_claim["status"] == "credential_warning", (
        "acknowledge_user_claim must not satisfy CredentialGate — it is a "
        "per-gate escape hatch, and this body embeds a live-shaped secret."
    )
    assert "id" not in secret_claim

    transient_claim = await _call(
        server,
        "memory_write",
        content="Currently the user prefers tabs over spaces.",
        scopes=["learning-style"],
        acknowledge_user_claim=True,
    )
    assert transient_claim["status"] == "transient_warning", (
        "acknowledge_user_claim must not satisfy TransientGate — the body "
        "carries a transient marker and is not durable in a week."
    )


async def test_acknowledge_user_claim_does_not_bypass_scope_mismatch(
    server: Any,
) -> None:
    """`ScopeMismatchGate` sits immediately AFTER `UserClaimGate`, which
    makes it the sharpest probe for a leaky flag: the acknowledgement
    hands control straight to it.

    Asserts both directions on ONE body, which is what makes this a
    narrowness pin rather than a smoke test — without the flag the body
    stops at `user_claim_warning` (so the body really does trip the
    user-claim gate), and with it the body advances exactly one gate to
    `scope_mismatch` (so the flag really did open only that one)."""
    # ScopeMismatchGate derives the known project scopes from the store,
    # so a project memory has to exist before a mismatch is detectable.
    seeded = await _call(
        server,
        "memory_write",
        content="alpha keeps its build script in scripts/build.sh",
        scopes=["projects:alpha"],
    )
    assert seeded["status"] == "committed"

    body = "The user prefers running alpha with the -x flag."

    without_flag = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["tools"],
    )
    assert without_flag["status"] == "user_claim_warning"

    with_flag = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["tools"],
        acknowledge_user_claim=True,
    )
    assert with_flag["status"] == "scope_mismatch", (
        "acknowledge_user_claim advanced the write past UserClaimGate but "
        "must not also satisfy ScopeMismatchGate — the body cites `alpha` "
        "while declaring only `tools`."
    )
    assert "projects:alpha" in with_flag["suggested_scopes"]


async def test_memory_update_unaffected_by_dedup(server: Any) -> None:
    """memory_update doesn't go through the dedup check — it edits an
    existing entry, so by definition there's no parallel entry to create.
    Even if the new content overlaps another memory heavily, the update
    should still succeed."""
    a = await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx tls termination notes",
        scopes=["tools"],
    )
    b = await _call(
        server,
        "memory_write",
        content="user prefers tabs over spaces in editor config",
        scopes=["learning-style"],
    )

    # Update b's body to overlap a's heavily — dedup mustn't block.
    updated = await _call(
        server,
        "memory_update",
        id=b["id"],
        # Trailing period so the shrink does not also read as a mid-sentence
        # cut and trip `memory_update`'s truncation gate — this test is about
        # dedup, and an unpunctuated shortening would refuse before it got there.
        content="kubernetes ingress nginx tls termination.",
    )
    assert updated["status"] == "committed"
    assert updated["id"] == b["id"]
    assert updated["id"] != a["id"]


# ---------------------------------------------------------------------------
# memory_verify — orthogonal verification timestamp
# ---------------------------------------------------------------------------


async def test_memory_verify_bumps_last_verified_at(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    assert written["last_verified_at"] is None

    verified = await _call(server, "memory_verify", id=written["id"])
    assert verified["verified"] == written["id"]
    assert verified["last_verified_at"] is not None

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["last_verified_at"] == verified["last_verified_at"]


async def test_memory_verify_does_not_bump_updated(server: Any) -> None:
    """Verification is the orthogonal axis: confirming reality matched the
    body should not make the body look edited."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    verified = await _call(server, "memory_verify", id=written["id"])
    assert verified["updated"] == written["updated"]


async def test_memory_verify_unknown_id_errors(server: Any) -> None:
    # As with the show/update unknown-id tests, this fixture id contains
    # `O` so the ULID validity gate fires first — match either shape.
    with pytest.raises(Exception, match="invalid id|no memory with id"):
        await _call(server, "memory_verify", id="01HXYZNOTAREALIDOK000000ZZ")


async def test_memory_verify_tombstoned_errors(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    await _call(server, "memory_remove", id=written["id"], reason="superseded")
    with pytest.raises(Exception, match="was removed"):
        await _call(server, "memory_verify", id=written["id"])


async def test_memory_verify_idempotent(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    first = await _call(server, "memory_verify", id=written["id"])
    second = await _call(server, "memory_verify", id=written["id"])
    assert first["last_verified_at"] is not None
    assert second["last_verified_at"] is not None
    # Second timestamp >= first (it slid forward, didn't go backwards).
    assert second["last_verified_at"] >= first["last_verified_at"]


async def test_memory_update_content_resets_last_verified_at(server: Any) -> None:
    """Editing the body invalidates the prior verification — your spot-check
    was for prose that no longer exists."""
    written = await _call(
        server, "memory_write", content="original body", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    pre = await _call(server, "memory_show", id=written["id"])
    assert pre["last_verified_at"] is not None

    await _call(server, "memory_update", id=written["id"], content="rewritten body.")
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] is None


async def test_memory_verify_rejects_oversized_note(server: Any) -> None:
    """The MCP entry-point caps `note` at 800 chars (500 before
    5.7.0 — raised on the T1 live-store census,
    the T3 note-cap decision). Without this, a hostile
    client could inflate the JSONL event log with multi-megabyte
    notes."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    with pytest.raises(Exception, match="cap is 800"):
        await _call(server, "memory_verify", id=written["id"], note="x" * 801)


async def test_memory_verify_accepts_max_length_note(server: Any) -> None:
    """Sanity check: 800 chars exactly is accepted (cap is inclusive)."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    res = await _call(server, "memory_verify", id=written["id"], note="x" * 800)
    assert res["last_verified_at"] is not None


async def test_memory_update_scope_only_preserves_last_verified_at(
    server: Any,
) -> None:
    """Scope changes don't touch the body's claims; verification stands."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    pre = await _call(server, "memory_show", id=written["id"])

    await _call(
        server,
        "memory_update",
        id=written["id"],
        scopes=["tools", "infrastructure"],
    )
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] == pre["last_verified_at"]


async def test_memory_update_confidence_only_preserves_last_verified_at(
    server: Any,
) -> None:
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    pre = await _call(server, "memory_show", id=written["id"])

    await _call(server, "memory_update", id=written["id"], confidence="high")
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] == pre["last_verified_at"]


async def test_memory_update_content_clears_verified_attestation(
    server: Any, tmp_path: Path
) -> None:
    """Body edits invalidate the structured attestation in lockstep with
    last_verified_at. Carrying verified_paths / verified_commits /
    verified_versions forward across a body rewrite would let a later
    memory_search read a stale attested path against new prose that no
    longer mentions it, suppressing the path-drift signal it should have
    produced.

    The attested path is a real file: `memory_verify` refuses paths it
    cannot stat, and `/etc/foo` — the previous literal — is a documentation
    PLACEHOLDER, which is now refused outright as a fabricated
    attestation."""
    extant = tmp_path / "claimed.conf"
    extant.write_text("k = v\n", encoding="utf-8")
    cited = extant.as_posix()
    written = await _call(
        server, "memory_write", content=f"claim about {cited}", scopes=["tools"]
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        verified_paths=[cited],
        verified_commits=["abc1234"],
        verified_versions=["1.2.3"],
        verified_absent_paths=["/data/remote-only"],
    )
    pre = await _call(server, "memory_show", id=written["id"])
    assert pre["verified_paths"] == [cited]
    assert pre["verified_commits"] == ["abc1234"]
    assert pre["verified_versions"] == ["1.2.3"]
    assert pre["verified_absent_paths"] == ["/data/remote-only"]

    await _call(server, "memory_update", id=written["id"], content="rewritten body.")
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] is None
    assert post["verified_paths"] == []
    assert post["verified_commits"] == []
    assert post["verified_versions"] == []
    assert post["verified_absent_paths"] == []


async def test_memory_update_scope_only_preserves_verified_attestation(
    server: Any, tmp_path: Path
) -> None:
    """Scope / confidence / category / links edits don't touch the body's
    claims; the structured attestation must survive alongside
    last_verified_at."""
    extant = tmp_path / "claimed.conf"
    extant.write_text("k = v\n", encoding="utf-8")
    cited = extant.as_posix()
    written = await _call(
        server, "memory_write", content=f"claim about {cited}", scopes=["tools"]
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        verified_paths=[cited],
        verified_versions=["1.2.3"],
    )
    pre = await _call(server, "memory_show", id=written["id"])

    await _call(
        server,
        "memory_update",
        id=written["id"],
        scopes=["tools", "infrastructure"],
    )
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] == pre["last_verified_at"]
    assert post["verified_paths"] == [cited]
    assert post["verified_versions"] == ["1.2.3"]


# ---------------------------------------------------------------------------
# memory_show response — last_verified_at + path_drift
# ---------------------------------------------------------------------------


async def test_memory_show_includes_last_verified_at_null_default(
    server: Any,
) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    shown = await _call(server, "memory_show", id=written["id"])
    assert "last_verified_at" in shown
    assert shown["last_verified_at"] is None


async def test_memory_show_omits_path_drift_when_no_paths(server: Any) -> None:
    """A body without filesystem paths should produce path_drift: null,
    not an empty dict — the consumer branches on `if path_drift is not
    None` to decide whether to surface drift to the user."""
    written = await _call(
        server, "memory_write", content="just prose, no paths.", scopes=["tools"]
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["path_drift"] is None


async def test_memory_show_surfaces_path_drift_when_path_missing(
    server: Any, tmp_path: Path
) -> None:
    missing = tmp_path / "definitely-not-here.txt"
    written = await _call(
        server,
        "memory_write",
        content=f"The script lived at `{missing}` for years.",
        scopes=["tools"],
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["path_drift"] is not None
    assert str(missing) in shown["path_drift"]["missing"]


async def test_memory_show_omits_path_drift_when_paths_healthy(
    server: Any, tmp_path: Path
) -> None:
    real = tmp_path / "alive.txt"
    real.write_text("x")
    written = await _call(
        server,
        "memory_write",
        content=f"Config at `{real}` on this box.",
        scopes=["tools"],
    )
    shown = await _call(server, "memory_show", id=written["id"])
    # Healthy paths still get checked, but path_drift is null because
    # nothing's actionable. The model shouldn't be nudged on healthy state.
    assert shown["path_drift"] is None


# ---------------------------------------------------------------------------
# memory_search response — last_verified_at on hits, path_drift on expanded
# ---------------------------------------------------------------------------


async def test_memory_search_hits_carry_last_verified_at(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="searchable durable fact", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])

    hits = _unwrap(await _call(server, "memory_search", query="searchable durable"))
    assert len(hits) >= 1
    assert hits[0]["last_verified_at"] is not None


async def test_memory_search_expand_top_surfaces_path_drift(
    server: Any, tmp_path: Path
) -> None:
    missing = tmp_path / "expand-target.txt"
    await _call(
        server,
        "memory_write",
        content=f"kubernetes networking config at `{missing}` reference",
        scopes=["infrastructure"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="kubernetes networking config",
            expand_top=True,
        )
    )
    assert len(hits) >= 1
    assert hits[0]["relevance"] == "high"
    assert hits[0].get("path_drift") is not None
    assert str(missing) in hits[0]["path_drift"]["missing"]


async def test_memory_search_surfaces_path_drift_without_expand_top(
    server: Any, tmp_path: Path
) -> None:
    """Per-hit path drift detail surfaces even without expand_top.

    The search pipeline runs `detect_path_drift` on every hit's body
    inside `_build_hit`; the missing-paths list rides through on
    `MemoryHit` so the response builder can expose `path_drift` directly.
    A `spot_check_recommended` hit is actionable without a memory_show
    round-trip — the model reads the missing paths and decides what to
    do.
    """
    missing = tmp_path / "not-expanded.txt"
    await _call(
        server,
        "memory_write",
        content=f"something at `{missing}` reference",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="something reference"))
    assert len(hits) >= 1
    assert hits[0].get("path_drift") is not None
    assert str(missing) in hits[0]["path_drift"]["missing"]
    # Healthy/absent path_drift cases still omit the field — only the
    # `has_drift or verified` cases surface it.


# ---------------------------------------------------------------------------
# Tools list — new tools registered
# ---------------------------------------------------------------------------


async def test_new_tools_registered(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_verify" in names
    assert "memory_admin" in names


# ---------------------------------------------------------------------------
# verification block on retrieval — the structural staleness signal
# ---------------------------------------------------------------------------
#
# These tests pin the contract that motivated the structural change: a
# `last_verified_at: null` timestamp was too easy for the consuming model
# to skim past, so retrieval responses now carry a structured
# `verification` block whose `recommendation` is non-null on never/stale
# memories. Asserting at the server boundary (rather than only at the
# unit level on compute_verification_status) catches plumbing regressions
# — a future refactor that drops the field from one of the three
# retrieval surfaces would otherwise pass the unit tests silently.


async def test_memory_show_includes_verification_block_never(server: Any) -> None:
    """A fresh write has never been verified — memory_show must
    surface the never-recommendation. This is the regression we're
    fixing: a model retrieving a memory like this should see an
    explicit prompt to spot-check, not a quiet null timestamp."""
    written = await _call(
        server,
        "memory_write",
        content="durable fact about the home lab",
        scopes=["tools"],
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert "verification" in shown
    block = shown["verification"]
    assert block["status"] == "never"
    assert block["last_verified_at"] is None
    assert block["age_days"] is None
    assert block["recommendation"] is not None
    assert "spot-check" in block["recommendation"].lower()


async def test_memory_show_includes_verification_block_fresh(server: Any) -> None:
    """After memory_verify, the same retrieval flips to fresh with
    a null recommendation — the absence of a recommendation is the
    "nothing to do" signal."""
    written = await _call(
        server, "memory_write", content="another durable fact", scopes=["tools"]
    )
    await _call(server, "memory_verify", id=written["id"])
    shown = await _call(server, "memory_show", id=written["id"])
    block = shown["verification"]
    assert block["status"] == "fresh"
    assert block["last_verified_at"] is not None
    assert block["recommendation"] is None


async def test_memory_search_hits_carry_verification_block(server: Any) -> None:
    """Every search hit carries the verification block. A hit on a
    never-verified memory must have a populated recommendation —
    otherwise a model could triage from search results without ever
    paying the spot-check cost."""
    await _call(
        server,
        "memory_write",
        content="searchable claim about kafka topics",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="kafka topics"))
    assert len(hits) >= 1
    block = hits[0]["verification"]
    assert block["status"] == "never"
    assert block["recommendation"] is not None


async def test_verification_block_uses_config_threshold(memory_dir: Path) -> None:
    """Wire-through: the per-server `verification_stale_days` config
    knob actually shapes the verdict. With threshold=0 every verified
    memory should immediately read as stale."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(verification_stale_days=0),
    )
    srv = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    written = await _call(
        srv,
        "memory_write",
        content="another durable fact about disk usage",
        scopes=["tools"],
    )
    await _call(srv, "memory_verify", id=written["id"])
    shown = await _call(srv, "memory_show", id=written["id"])
    block = shown["verification"]
    assert block["status"] == "stale"
    assert block["recommendation"] is not None
    assert block["stale_after_days"] == 0


async def test_verification_block_path_drift_coexist(
    server: Any, tmp_path: Path
) -> None:
    """The two staleness signals are independent. A never-verified
    memory whose body cites a missing path shows both — the model's
    payload carries `verification.status='never'` and a populated
    `path_drift.missing`."""
    missing = tmp_path / "drifted.txt"
    written = await _call(
        server,
        "memory_write",
        content=f"production config used to live at `{missing}` for years",
        scopes=["tools"],
    )
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["verification"]["status"] == "never"
    assert shown["path_drift"] is not None
    assert str(missing) in shown["path_drift"]["missing"]


# ---------------------------------------------------------------------------
# Backward-scan early-exit in _already_recorded_pending_ids
# ---------------------------------------------------------------------------


def test_already_recorded_pending_ids_early_exits_on_old_events(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedup scan walks the log backward and bails as soon as event
    timestamps fall behind the oldest pending token's `issued_at`.
    Without the early-exit, every call to this function walks every
    event in the log — O(N) per turn against tens of thousands of events.

    Two assertions live here:

    1. Correctness — the function returns exactly the memory_ids whose
       use-events are timestamped at or after the corresponding token's
       `issued_at`. The 2.6.7 timestamp-guard semantics must survive
       the optimisation.
    2. The early-exit itself — the backward scan examines a handful of
       recent events rather than all 10k. Asserted by counting the
       events the loop touches, not by wall clock: a clock threshold
       measures the runner's throughput, not the optimisation. See the
       comment at the assertion.
    """
    import time as _time

    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import Recorder
    from bettermemory.handlers import _shared
    from bettermemory.session import PendingUseToken, SessionState

    state = SessionState()
    store = Store(memory_dir)
    recorder = Recorder(store=store, session_id=state.session_id)

    # Phase 1: write 10_000 ancient events to the log, in one batch so
    # the fixture stays cheap. Predates any pending token; the early-exit
    # must bail before scanning them.
    with store.batch():
        for i in range(10_000):
            recorder.record(
                "turn_audited", session_id=state.session_id, hits=[], note=i
            )

    # Phase 2: mint pending tokens NOW. Every recorded `use` event below
    # will be timestamped strictly after the tokens' `issued_at`.
    now_ts = _time.time()
    pending_mids = [f"01J0000000000000000000{i:04d}" for i in range(5)]
    for mid in pending_mids:
        state.pending_use_tokens[mid] = PendingUseToken(
            token=f"use_{mid[-8:]}",
            memory_id=mid,
            issued_at=now_ts,
            issued_at_turn=1,
        )

    # Phase 3: record a `use` event for each pending mid AFTER minting.
    for mid in pending_mids:
        recorder.record(
            "use", ids=[mid], outcome="applied", auto=False, attribution="model"
        )

    # The early-exit is asserted STRUCTURALLY — by how many events the
    # backward scan examines — not by wall clock.
    #
    # A clock assertion here measured the wrong thing and flaked on it.
    # Through 3.40 the scan materialised the whole log plus a whole-log
    # stop-hook-session pre-pass before the loop — both O(N) over the
    # whole log and neither short-circuited — so parsing 10k events
    # dominated the call (measured locally at 14ms of a 15.6ms call,
    # with the backward loop examining exactly one event) whether or
    # not the early-exit worked. The old `elapsed < 0.5` was reading
    # the runner's parse throughput, and when a shared ubuntu-latest
    # slot returned 0.538s during the 3.37.0 release run it reported
    # "early-exit appears not to be triggering" about an early-exit
    # that was working perfectly. The scan now streams
    # `Store.iter_events_backward` with a lazy per-row decode, so the
    # decode cost is tail-bounded too (pinned separately:
    # test_already_recorded_pending_ids_parse_count_is_tail_bounded) —
    # but a clock threshold would still measure the runner, so the
    # structural count stays.
    #
    # Counting `_event_ts_epoch` calls measures the loop directly: it is
    # called once per event the backward scan examines and nowhere else
    # in this path. Delete the `break` and this count becomes 10_000+.
    examined = 0
    real_event_ts_epoch = _shared._event_ts_epoch

    def _counting_event_ts_epoch(raw: object) -> float | None:
        nonlocal examined
        examined += 1
        return real_event_ts_epoch(raw)

    monkeypatch.setattr(_shared, "_event_ts_epoch", _counting_event_ts_epoch)
    result = _already_recorded_pending_ids(state, recorder)

    assert result == set(pending_mids), f"expected all pending ids back, got {result}"
    assert examined < 50, (
        f"backward scan examined {examined} of 10_000 events; the early-exit "
        "is not bailing at the oldest pending token's issued_at"
    )


def test_already_recorded_pending_ids_respects_issued_at_guard(
    memory_dir: Path,
) -> None:
    """The 2.6.7 fix: a `use` event timestamped BEFORE the pending
    token's `issued_at` must not falsely purge the fresh token. This
    is the load-bearing invariant that the backward-scan optimisation
    must preserve.
    """
    import time as _time

    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import Recorder
    from bettermemory.session import PendingUseToken, SessionState

    state = SessionState()
    recorder = Recorder(store=Store(memory_dir), session_id=state.session_id)

    mid = "01J0000000000000000000ABCD"
    # Stale use event lands FIRST.
    recorder.record(
        "use", ids=[mid], outcome="applied", auto=False, attribution="model"
    )

    # Brief sleep to ensure the next token's wall-clock issued_at
    # comfortably exceeds the stale event's ts (sub-second resolution
    # on the ISO timestamp shouldn't matter in practice, but pin it).
    _time.sleep(0.01)

    state.pending_use_tokens[mid] = PendingUseToken(
        token="use_freshtok",
        memory_id=mid,
        issued_at=_time.time(),
        issued_at_turn=1,
    )

    result = _already_recorded_pending_ids(state, recorder)
    # The stale event is older than the fresh token — must NOT mark
    # the token as already-recorded.
    assert result == set(), (
        f"stale event falsely matched fresh token; got {result}. "
        "The event.ts >= token.issued_at guard regressed."
    )


def test_already_recorded_pending_ids_parse_count_is_tail_bounded(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedup scan's DECODE cost — not just its matching loop — is
    bounded by the examined tail. The early-exit above always bounded
    the loop, but through 3.40 the scan materialised the whole log
    (plus a whole-log stop-hook-session pre-pass) first, so every turn
    with pending tokens decoded the WHOLE log to examine a handful of
    events — 14.0ms of a 15.6ms call measured against 10k events, with
    the loop examining one. The scan now streams
    `Store.iter_events_backward`, which decodes a row only when the
    cursor pulls it.

    Counting `Store._event_from_raw` calls measures the decode
    directly: it is the single per-row decode seam every event reader
    on the store shares, and nothing else in this call path decodes log
    rows. Mutation property: revert the consumer to
    `list(store.iter_events())`, or make the reader decode eagerly, or
    delete the early-exit `break`, and this count becomes ~N (10_003
    rows here) instead of the examined tail.
    """
    import time as _time

    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import Recorder
    from bettermemory.session import PendingUseToken, SessionState

    state = SessionState()
    store = Store(memory_dir)
    recorder = Recorder(store=store, session_id=state.session_id)

    # Phase 1: a long ancient prefix, recorded in one batch so the
    # fixture stays cheap; the scan sees one 10_003-row log. All
    # timestamped before any pending token, so the scan's early-exit
    # crosses the boundary after the tail.
    with store.batch():
        for _ in range(10_000):
            recorder.record("noise", session_id=state.session_id)

    # Phase 2: mint pending tokens NOW, then record their `use` events
    # through the real Recorder (the consumed shape must be
    # production's — EventLog discipline).
    now_ts = _time.time()
    pending_mids = [f"01J0000000000000000000{i:04d}" for i in range(3)]
    for mid in pending_mids:
        state.pending_use_tokens[mid] = PendingUseToken(
            token=f"use_{mid[-8:]}",
            memory_id=mid,
            issued_at=now_ts,
            issued_at_turn=1,
        )
    for mid in pending_mids:
        recorder.record(
            "use", ids=[mid], outcome="applied", auto=False, attribution="model"
        )

    decodes = 0
    real_decode = Store._event_from_raw

    def _counting_decode(self: Store, raw: Any) -> dict[str, Any] | None:
        nonlocal decodes
        decodes += 1
        return real_decode(self, raw)

    monkeypatch.setattr(Store, "_event_from_raw", _counting_decode)
    result = _already_recorded_pending_ids(state, recorder)

    assert result == set(pending_mids), f"expected all pending ids back, got {result}"
    assert decodes < 50, (
        f"dedup scan decoded {decodes} of 10_003 log rows; the backward "
        "reader's lazy decode (or the early-exit that bounds it) regressed"
    )


def test_already_recorded_pending_ids_bridges_tagged_hook_use_event(
    memory_dir: Path,
) -> None:
    """Session-id bridge, per-event derivation: a hook-written `use`
    event lives under the Claude Code TRANSCRIPT id — a different id
    space from the server's `sess_<hex>` — and is recognised by the
    `triggered_from="stop_hook"` tag the hook stamps on every event it
    writes (both `use` shapes in `hook._emit_hook_attributions`). A
    tagged event emitted AFTER the token mint must purge the pending
    id. An UNTAGGED `use` event under some other foreign session must
    NOT, however fresh: non-hook foreign sessions (another window's
    server, the CLI acknowledge-debt path) never bridged under the
    derived-set shape and must not bridge now — the tag, not mere
    foreignness, is what crosses the id-space boundary.
    """
    import time as _time

    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import Recorder
    from bettermemory.session import PendingUseToken, SessionState

    from ._event_helpers import EventLog

    state = SessionState()
    store = Store(memory_dir)
    recorder = Recorder(store=store, session_id=state.session_id)

    mid_hook = "01J0000000000000000000HOOK"
    mid_foreign = "01J000000000000000000FORGN"
    now_ts = _time.time()
    for mid in (mid_hook, mid_foreign):
        state.pending_use_tokens[mid] = PendingUseToken(
            token=f"use_{mid[-8:]}",
            memory_id=mid,
            issued_at=now_ts,
            issued_at_turn=1,
        )

    # The Stop hook settles mid_hook: transcript-id session, tagged —
    # the production shape `hook.run_audit` emits (EventLog wraps the
    # real Recorder, so the event lands byte-for-byte as production's).
    hook_log = EventLog(store=store, session_id="claude-code-transcript-bridge")
    hook_log.emit(
        "use",
        ids=[mid_hook],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["A retrievable fact"],
        triggered_from="stop_hook",
    )
    # A DIFFERENT window's server settles mid_foreign under its own
    # sess_<hex> — untagged, and not this recorder's session.
    foreign_log = EventLog(store=store, session_id="sess_other_window")
    foreign_log.emit(
        "use", ids=[mid_foreign], outcome="applied", auto=False, attribution="model"
    )

    result = _already_recorded_pending_ids(state, recorder)
    assert result == {mid_hook}, (
        f"expected exactly the hook-tagged settlement to bridge; got {result}. "
        "Missing mid_hook means the stop_hook tag no longer bridges the "
        "transcript id space; a present mid_foreign means an untagged foreign "
        "session slipped through the bridge."
    )


# ---------------------------------------------------------------------------
# OSError-leak hardening at the MCP boundary
# ---------------------------------------------------------------------------
#
# Every store.write call site reachable from a tool MUST translate a
# disk-level OSError (ENOSPC/EIO/EACCES) into a structured ValueError —
# never leak the bare OSError, which carries the absolute store path —
# past `call_tool`. Mirrors test_remove_handler_converts_oserror_to_value_error
# (test_server_tombstones.py) and the rename_scope OSError regression.
# Caught by the post-3.6.0 whole-tree sweep: write.py's memory_write was
# an unguarded store.write sibling.


def _oserror_wrapped_as_value_error(excinfo: Any, marker: str) -> bool:
    """Walk the raised exception's cause/context chain; confirm a
    handler-emitted ValueError (containing `marker`) wraps the original
    OSError(ENOSPC=28). A regression (no `except OSError`) leaves the
    OSError as the direct cause with no intervening ValueError."""
    chain: list[BaseException] = []
    cur: BaseException | None = excinfo.value
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    has_value_error = any(isinstance(e, ValueError) and marker in str(e) for e in chain)
    has_oserror = any(isinstance(e, OSError) and e.errno == 28 for e in chain)
    return has_value_error and has_oserror


def _raising_write(*args: Any, **kwargs: Any) -> Any:
    raise OSError(28, "No space left on device")


async def test_memory_write_handler_converts_oserror_to_value_error(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_write -> _commit_write -> store.write. A disk-level OSError
    must surface as a structured ValueError, not leak the bare OSError's
    absolute path past the MCP boundary."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())
    monkeypatch.setattr(store, "write", _raising_write)

    with pytest.raises(Exception) as excinfo:
        await _call(server, "memory_write", content="x", scopes=["tools"])
    assert _oserror_wrapped_as_value_error(excinfo, "failed to write memory"), (
        f"regression: bare OSError leaked past memory_write. Got: {excinfo.value!r}"
    )


async def test_a_synced_stamp_this_host_never_made_reads_remote_on_every_surface(
    server: Any, memory_dir: Path
) -> None:
    """The trust rule (6.6.0). A `synced` record whose record carries a
    `last_verified_at` but whose row has no local verification reads
    `verification.status: "remote"` and `spot_check_required` on the
    search hit (expanded or not) and on `memory_show`; a local
    `memory_verify` lifts it. The row is put into that state directly
    here: the same shape a pull produces (the label from the pull, this
    host's stamp absent), without the sync round-trip the mirror suite
    already covers."""
    written = await _call(
        server,
        "memory_write",
        content="The staging cluster runs postgres sixteen behind pgbouncer.",
        scopes=["infrastructure"],
    )
    await _call(server, "memory_verify", id=written["id"])
    store = Store(memory_dir)
    memory = store.load_one(written["id"])
    assert memory.last_verified_at is not None
    store.put_memory(memory, provenance="synced")
    store.delete_verification(written["id"])

    hits = await _call(
        server, "memory_search", query="staging cluster postgres", auto_scope=False
    )
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    (hit,) = hits
    assert hit["provenance"] == "synced"
    assert hit["verification"]["status"] == "remote"
    assert hit["verification"]["age_days"] is None
    assert hit["verification"]["last_verified_at"] is not None
    assert "another host" in hit["verification"]["recommendation"]
    assert hit["staleness_verdict"] == "spot_check_required"
    assert hit["last_verified_at"] is not None

    expanded = await _call(
        server,
        "memory_search",
        query="staging cluster postgres",
        auto_scope=False,
        expand_top=True,
    )
    expanded = (
        expanded.get("result", expanded) if isinstance(expanded, dict) else expanded
    )
    assert "body" in expanded[0]
    assert expanded[0]["verification"]["status"] == "remote"
    assert expanded[0]["staleness_verdict"] == "spot_check_required"

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["provenance"] == "synced"
    assert shown["verification"]["status"] == "remote"
    assert shown["staleness_verdict"] == "spot_check_required"

    # A verify on this host is the one thing that lifts the rule.
    await _call(server, "memory_verify", id=written["id"])
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["provenance"] == "synced"
    assert shown["verification"]["status"] == "fresh"
    assert shown["staleness_verdict"] == "fresh"
    hits = await _call(
        server, "memory_search", query="staging cluster postgres", auto_scope=False
    )
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert hits[0]["verification"]["status"] == "fresh"


async def test_the_trust_rule_leaves_unstamped_and_local_records_alone(
    server: Any, memory_dir: Path
) -> None:
    """A synced record with no stamp already reads `never`; a local record
    with a stamp reads `fresh`. Neither is the rule's business."""
    unstamped = await _call(
        server,
        "memory_write",
        content="The warm standby replays WAL from the primary.",
        scopes=["infrastructure"],
    )
    stamped_local = await _call(
        server,
        "memory_write",
        content="The primary vacuums nightly at two.",
        scopes=["infrastructure"],
    )
    await _call(server, "memory_verify", id=stamped_local["id"])
    store = Store(memory_dir)
    memory = store.load_one(unstamped["id"])
    store.put_memory(memory, provenance="synced")

    shown = await _call(server, "memory_show", id=unstamped["id"])
    assert shown["provenance"] == "synced"
    assert shown["verification"]["status"] == "never"
    assert shown["staleness_verdict"] == "spot_check_required"
    shown = await _call(server, "memory_show", id=stamped_local["id"])
    assert shown["provenance"] == "local"
    assert shown["verification"]["status"] == "fresh"
    assert shown["staleness_verdict"] == "fresh"
