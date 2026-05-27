"""Tests for server.py — tool registration and end-to-end behavior.

We exercise the registered tools via FastMCP's `call_tool` rather than
spinning up the full stdio transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


@pytest.fixture
def confirming_server(memory_dir: Path) -> tuple[Any, SessionState]:
    """A server with require_write_confirmation=True."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    return build_server(config=cfg, store=Store(memory_dir), state=state), state


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return the parsed JSON payload.

    FastMCP returns a list[ContentBlock]; for our tools the structured
    result is what we care about, available via `call_tool`'s second value.
    """
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    # Fallback: parse text content.
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


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
        "memory_list",
        "memory_remove",
        "memory_scope_disable",
        # Companion to scope_disable, mentioned in the spec.
        "memory_scope_enable",
    }
    assert expected <= names


async def test_each_tool_has_description_and_input_schema(server: Any) -> None:
    tools = await server.list_tools()
    for tool in tools:
        assert tool.description, f"{tool.name} missing description"
        assert tool.inputSchema, f"{tool.name} missing inputSchema"


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


async def test_search_finds_written_memory(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="The home lab is on subnet 10.42.",
        scopes=["infrastructure"],
    )
    hits = await _call(server, "memory_search", query="home lab subnet")
    # Structured returns under the "result" key for FastMCP — handle both.
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert len(hits) >= 1
    assert "home lab" in hits[0]["snippet"]


async def test_remove_excludes_from_list(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="Temporary fact.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_remove",
        id=written["id"],
        reason="not durable",
    )

    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert all(item["id"] != written["id"] for item in listing)


async def test_disabled_scope_hidden_from_search_and_list(server: Any) -> None:
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

    state = await _call(server, "memory_scope_disable", scope="projects:alpha")
    state = (
        state.get("result", state)
        if isinstance(state, dict) and "result" in state
        else state
    )
    assert "projects:alpha" in state["disabled_scopes"]

    hits = await _call(server, "memory_search", query="alpha")
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert all("projects:alpha" not in h["scopes"] for h in hits)

    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert all("projects:alpha" not in item["scopes"] for item in listing)

    # Re-enabling brings them back.
    re = await _call(server, "memory_scope_enable", scope="projects:alpha")
    re = re.get("result", re) if isinstance(re, dict) and "result" in re else re
    assert "projects:alpha" not in re["disabled_scopes"]


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
# Pending-write flow (require_write_confirmation = true)
# ---------------------------------------------------------------------------


async def test_write_returns_pending_when_confirmation_required(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, state = confirming_server
    res = await _call(
        server,
        "memory_write",
        content="might want to remember this",
        scopes=["tools"],
    )
    assert res["status"] == "pending"
    assert res["pending_id"].startswith("pending_")
    assert res["preview"]["scopes"] == ["tools"]
    assert state.pending_writes  # parked, not committed


async def test_pending_write_commits_on_confirm(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    pending = await _call(
        server,
        "memory_write",
        content="durable preference",
        scopes=["tools"],
    )
    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert committed["status"] == "committed"
    assert committed["id"]
    # Now visible via list
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert any(item["id"] == committed["id"] for item in listing)


async def test_pending_write_does_not_persist_until_confirmed(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    await _call(
        server,
        "memory_write",
        content="never to be committed",
        scopes=["tools"],
    )
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert listing == []


async def test_pending_write_can_be_cancelled(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, state = confirming_server
    pending = await _call(
        server,
        "memory_write",
        content="reconsider",
        scopes=["tools"],
    )
    pid = pending["pending_id"]
    res = await _call(server, "memory_write_cancel", pending_id=pid)
    assert res["existed"] is True
    assert pid not in state.pending_writes

    # Cannot confirm after cancel.
    with pytest.raises(Exception, match="no pending write"):
        await _call(server, "memory_write_confirm", pending_id=pid)


async def test_confirm_unknown_id_errors(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    with pytest.raises(Exception, match="no pending write"):
        await _call(server, "memory_write_confirm", pending_id="pending_deadbeef0000")


async def test_pending_write_expiry_emits_event_and_surfaces_in_confirm_error(
    confirming_server: tuple[Any, SessionState], memory_dir: Path
) -> None:
    """Pre-2.6.8 the 1h TTL silently dropped a pending write — a "yes
    save it" 61 minutes later would fail with "no pending write" and
    no signal that it had been evicted vs. typo'd. Now: a
    `pending_expired` event lands and `memory_write_confirm` returns a
    targeted error that says "expired, re-stage".
    """
    import time as _time

    from bettermemory import session as session_mod
    from bettermemory.events import iter_events

    server, state = confirming_server
    pending = await _call(
        server,
        "memory_write",
        content="durable preference",
        scopes=["tools"],
    )
    pid = pending["pending_id"]

    # Backdate the pending write past the TTL so the next _evict_expired
    # call drops it. Faster than waiting an hour and structurally exercises
    # the same code path.
    state.pending_writes[pid].created_at = (
        _time.time() - session_mod._PENDING_TTL_SECONDS - 1
    )

    # Any memory_* call advances the turn, which drains pending_expired.
    await _call(server, "memory_list")

    expired_events = [
        e for e in iter_events(memory_dir) if e["kind"] == "pending_expired"
    ]
    assert len(expired_events) == 1
    assert expired_events[0]["pending_id"] == pid
    assert expired_events[0]["ttl_seconds"] >= session_mod._PENDING_TTL_SECONDS

    # The confirm now distinguishes expired from missing.
    with pytest.raises(Exception, match="expired before confirmation"):
        await _call(server, "memory_write_confirm", pending_id=pid)


async def test_confirmation_disabled_writes_immediately(server: Any) -> None:
    """The default config commits immediately — no pending state."""
    res = await _call(server, "memory_write", content="immediate", scopes=["tools"])
    assert res["status"] == "committed"
    assert "pending_id" not in res


# ---------------------------------------------------------------------------
# memory_write category="user-inference" — structural confirmation tier
#
# A claim *about* the user (preferences, beliefs, working style) is always
# routed through the pending-write flow, regardless of the global
# `require_write_confirmation` config. Misattribution sticks; the user
# gets the veto.
# ---------------------------------------------------------------------------


async def test_user_inference_category_stages_even_without_global_flag(
    server: Any,
) -> None:
    """category='user-inference' triggers pending on a default-config server
    where global require_write_confirmation is off."""
    res = await _call(
        server,
        "memory_write",
        content="Prefers code-driven tutorials over walkthroughs.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "pending"
    assert res["pending_reason"] == "user-inference"
    assert res["pending_id"].startswith("pending_")
    assert res["preview"]["category"] == "user-inference"
    # Hint should explicitly tell the model to ask the user first.
    assert "ask the user" in res["hint"].lower()


async def test_user_inference_pending_commits_after_confirm(server: Any) -> None:
    pending = await _call(
        server,
        "memory_write",
        content="Prefers terse responses over verbose explanations.",
        scopes=["learning-style"],
        category="user-inference",
    )
    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert committed["status"] == "committed"
    assert committed["id"]


async def test_user_inference_pending_can_be_cancelled(server: Any) -> None:
    pending = await _call(
        server,
        "memory_write",
        content="Allegedly hates dark mode.",
        scopes=["learning-style"],
        category="user-inference",
    )
    res = await _call(server, "memory_write_cancel", pending_id=pending["pending_id"])
    assert res["existed"] is True
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert listing == []  # cancelled write never landed


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


async def test_global_config_flag_records_pending_reason_config(
    confirming_server: tuple[Any, SessionState],
) -> None:
    """When the pending flow fires because of the global config flag (not
    because of category), the response carries pending_reason='config'."""
    server, _ = confirming_server
    res = await _call(
        server,
        "memory_write",
        content="some durable fact",
        scopes=["tools"],
    )
    assert res["status"] == "pending"
    assert res["pending_reason"] == "config"
    assert res["preview"]["category"] == "fact"


# ---------------------------------------------------------------------------
# memory_list(with_bodies=True)
# ---------------------------------------------------------------------------


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_list_default_excludes_body(server: Any) -> None:
    await _call(server, "memory_write", content="alpha body content", scopes=["tools"])
    listing = _unwrap(await _call(server, "memory_list"))
    assert len(listing) == 1
    assert "body" not in listing[0]
    assert "summary" in listing[0]


async def test_list_with_bodies_inlines_body(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="full body content here for inlining",
        scopes=["tools"],
    )
    listing = _unwrap(await _call(server, "memory_list", with_bodies=True))
    assert len(listing) == 1
    assert listing[0]["id"] == written["id"]
    assert "full body content here for inlining" in listing[0]["body"]
    # Summary still surfaced for uniformity with body-less mode.
    assert "summary" in listing[0]


async def test_list_with_bodies_respects_scope_filter(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="alpha note",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "memory_write",
        content="tools note",
        scopes=["tools"],
    )
    listing = _unwrap(
        await _call(server, "memory_list", scopes=["tools"], with_bodies=True)
    )
    assert len(listing) == 1
    assert listing[0]["scopes"] == ["tools"]
    assert "tools note" in listing[0]["body"]


async def test_list_with_bodies_respects_disabled_scopes(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="should be hidden",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "memory_write",
        content="should be visible",
        scopes=["tools"],
    )
    await _call(server, "memory_scope_disable", scope="projects:alpha")

    listing = _unwrap(await _call(server, "memory_list", with_bodies=True))
    assert all("projects:alpha" not in m["scopes"] for m in listing)
    assert any("should be visible" in m["body"] for m in listing)


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


async def test_list_summary_includes_updated_timestamp(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    listing = _unwrap(await _call(server, "memory_list"))
    assert len(listing) == 1
    assert listing[0]["id"] == written["id"]
    assert "updated" in listing[0]
    # On first write, created == updated.
    assert listing[0]["updated"] == listing[0]["created"]


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
    otherwise match. The caller (a loop iteration entering for the
    first time) distinguishes this from 'nothing new' by also
    calling memory_scope_overview and checking that
    `curation_pending_new_since_last_session is None`."""
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

    events_path = memory_dir / ".events.jsonl"
    search_events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if json.loads(line)["kind"] == "search"
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
    auto-pull silently drops it. Inspect via memory_list_tombstones."""
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
    await _call(server, "memory_scope_disable", scope="projects:alpha")
    hits = _unwrap(await _call(server, "memory_search", query="beta dependent"))
    hit = next(h for h in hits if h["id"] == dependent["id"])
    resolved = hit.get("depends_on_resolved")
    if resolved is not None:
        assert target["id"] not in {r["id"] for r in resolved}


# ---------------------------------------------------------------------------
# memory_write — inline curation hint (one-shot per session)
# ---------------------------------------------------------------------------


async def test_write_curation_hint_absent_when_no_pressure(server: Any) -> None:
    """Fresh store with no events: pressure is zero, hint should not
    attach regardless of threshold. The default-on behaviour stays
    silent for sessions with a clean store."""
    res = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    assert res["status"] == "committed"
    assert "curation_hint" not in res


async def test_write_curation_hint_attaches_when_pressure_exceeds_threshold(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Monkeypatch curation_counts to simulate accumulated pressure.
    Hint should attach to the first write of the session with the
    expected fields."""
    from bettermemory import health as _health

    def fake_counts(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {
            "stale": 0,
            "never_verified": 0,
            "drifted": 2,
            "cold": 0,
            "dead": 4,
            "silent_misses": 0,
            "endorsement_debt": 1,
        }

    monkeypatch.setattr(_health, "curation_counts", fake_counts)

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_x = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_x, "memory_write", content="first", scopes=["tools"])
    assert res["status"] == "committed"
    assert "curation_hint" in res
    hint = res["curation_hint"]
    assert hint["pressure"] == 4 + 2 + 1
    assert hint["threshold"] == 5  # the BehaviorConfig default
    assert hint["counts"] == {
        "dead_weight": 4,
        "drifted": 2,
        "endorsement_debt": 1,
    }
    assert "message" in hint


async def test_write_curation_hint_is_one_shot_per_session(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """Only the first write of a session gets the hint. Subsequent
    writes stay quiet — the model gets one nudge, not a stream."""
    from bettermemory import health as _health

    monkeypatch.setattr(
        _health,
        "curation_counts",
        lambda *_a, **_k: {
            "stale": 0,
            "never_verified": 0,
            "drifted": 0,
            "cold": 0,
            "dead": 99,
            "silent_misses": 0,
            "endorsement_debt": 0,
        },
    )
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    server_x = build_server(config=cfg, store=Store(memory_dir), state=state)

    first = await _call(server_x, "memory_write", content="first", scopes=["tools"])
    second = await _call(server_x, "memory_write", content="second", scopes=["tools"])
    assert "curation_hint" in first
    assert "curation_hint" not in second
    assert state.curation_hint_checked is True


async def test_write_curation_hint_disabled_by_config_flag(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """`curation_hint_enabled = False` short-circuits the helper before
    the event-log walk happens. No hint attaches even at very high
    simulated pressure."""
    from bettermemory import health as _health
    from bettermemory.config import BehaviorConfig

    monkeypatch.setattr(
        _health,
        "curation_counts",
        lambda *_a, **_k: {
            "stale": 0,
            "never_verified": 0,
            "drifted": 0,
            "cold": 0,
            "dead": 999,
            "silent_misses": 0,
            "endorsement_debt": 0,
        },
    )
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(curation_hint_enabled=False),
    )
    server_x = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_x, "memory_write", content="x", scopes=["tools"])
    assert "curation_hint" not in res


# ---------------------------------------------------------------------------
# episode_write — sibling primitive for journal-shaped writes
# ---------------------------------------------------------------------------


async def test_episode_write_commits_with_minimum_payload(server: Any) -> None:
    """A body alone is enough — scopes/takeaway default to empty/None."""
    res = await _call(
        server,
        "episode_write",
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
        "episode_write",
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
    does, just without the durability gate. FastMCP wraps the
    underlying ValueError as a ToolError; both pass through `Exception`."""
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode_write", body="")
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode_write", body="   \n\t  ")


async def test_episode_write_is_invisible_to_memory_iterators(server: Any) -> None:
    """Episodes live in a sibling subtree — memory_list /
    memory_search must not surface them."""
    await _call(
        server,
        "episode_write",
        body="this is an episode, not a memory",
    )
    listing = _unwrap(await _call(server, "memory_list"))
    assert listing == []
    hits = _unwrap(await _call(server, "memory_search", query="episode memory not"))
    assert hits == []


async def test_episode_write_event_recorded_with_kind_episode_write(
    server: Any, memory_dir: Path
) -> None:
    """The recorder fires a dedicated `episode_write` event so the
    tool-usage rollup counts it independently from memory_write."""
    await _call(server, "episode_write", body="some takeaway")
    events_path = memory_dir / ".events.jsonl"
    lines = events_path.read_text().splitlines()
    ep_events = [
        json.loads(line)
        for line in lines
        if json.loads(line)["kind"] == "episode_write"
    ]
    assert ep_events
    assert ep_events[-1]["session"].startswith("sess_")


# ---------------------------------------------------------------------------
# episode_handoff — first call at loop-iteration entry
# ---------------------------------------------------------------------------


async def test_episode_handoff_empty_when_no_prior_session(server: Any) -> None:
    """Fresh store, current session has the only events. Handoff
    returns prior_session_id=None + episodes=[] so the caller can
    branch on 'no baseline' vs. 'baseline exists but is empty'."""
    res = await _call(server, "episode_handoff")
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
        "episode_write",
        body="iter 1 — tried A, broke at step 3",
        takeaway="A blocks on auth header",
    )
    await _call(
        server_a,
        "episode_write",
        body="iter 2 — tried B, partial success",
        takeaway="B partial; needs retry",
    )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode_handoff")
    assert res["prior_session_id"] is not None
    assert res["prior_session_id"].startswith("sess_")
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["A blocks on auth header", "B partial; needs retry"]
    # Body is included alongside the takeaway so the reader can drill in.
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
            "episode_write",
            body=f"iter {i}",
            takeaway=f"takeaway {i}",
        )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode_handoff", max_episodes=3)
    assert len(res["episodes"]) == 3
    # Most recent 3 episodes: iter 4, 5, 6 (oldest first within the slice).
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["takeaway 4", "takeaway 5", "takeaway 6"]


# ---------------------------------------------------------------------------
# episode_search — cross-session lookup
# ---------------------------------------------------------------------------


async def test_episode_search_returns_all_episodes_when_unfiltered(
    memory_dir: Path,
) -> None:
    """No filters → list every episode across every session,
    chronological (oldest first)."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A1", takeaway="a1")
    await _call(server_a, "episode_write", body="A2", takeaway="a2")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode_write", body="B1", takeaway="b1")

    res = _unwrap(await _call(server_b, "episode_search"))
    assert [e["takeaway"] for e in res] == ["a1", "a2", "b1"]


async def test_episode_search_filters_by_scope_intersection(
    memory_dir: Path,
) -> None:
    """Scope filter is an intersection — an episode passes when ANY
    of its scopes is in the filter set."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode_write", body="auth notes", scopes=["projects:auth"])
    await _call(
        server, "episode_write", body="ranker notes", scopes=["projects:ranker"]
    )
    await _call(server, "episode_write", body="no scopes")  # empty scopes

    res = _unwrap(await _call(server, "episode_search", scopes=["projects:auth"]))
    assert len(res) == 1
    assert "auth" in res[0]["body"]


async def test_episode_search_filters_by_parent_session_id(
    memory_dir: Path,
) -> None:
    """`parent_session_id` restricts the walk to one session's dir."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res_a = await _call(server_a, "episode_write", body="A's note", takeaway="from A")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode_write", body="B's note", takeaway="from B")

    res = _unwrap(
        await _call(
            server_b,
            "episode_search",
            parent_session_id=res_a["session_id"],
        )
    )
    assert len(res) == 1
    assert res[0]["takeaway"] == "from A"


async def test_episode_search_max_results_caps_output(
    memory_dir: Path,
) -> None:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    for i in range(25):
        await _call(server, "episode_write", body=f"entry {i}")
    res = _unwrap(await _call(server, "episode_search", max_results=5))
    assert len(res) == 5


# ---------------------------------------------------------------------------
# episode_promote — distill into durable memory
# ---------------------------------------------------------------------------


async def test_episode_promote_commits_takeaway_as_memory(server: Any) -> None:
    """Default flow: takeaway becomes a durable memory's body, source
    episode is deleted."""
    ep = await _call(
        server,
        "episode_write",
        body="iter 3 — found the bug in src/auth.py:42, missing null check",
        takeaway="auth.py:42 needs null check on session token",
    )
    res = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["projects:auth"],
    )
    assert res["status"] == "committed"
    assert "auth.py:42 needs null check" in res["body"] if "body" in res else True
    assert res["promoted_from_episode_id"] == ep["id"]

    # Source episode is gone now.
    remaining = _unwrap(await _call(server, "episode_search"))
    assert remaining == []  # nothing left

    # And the durable memory landed where it should — searchable.
    hits = _unwrap(await _call(server, "memory_search", query="null check auth"))
    assert any("null check" in h.get("snippet", "") for h in hits)


async def test_episode_promote_use_body_uses_full_body(server: Any) -> None:
    """`use_body=True` promotes the full body instead of the takeaway.
    Required when the episode has no takeaway."""
    ep = await _call(
        server,
        "episode_write",
        body="full body content with all the details",
    )
    res = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["tools"],
        use_body=True,
    )
    assert res["status"] == "committed"


async def test_episode_promote_without_takeaway_requires_use_body(
    server: Any,
) -> None:
    """Episode without a takeaway + use_body=False → clear error
    rather than silently promoting an empty body."""
    ep = await _call(server, "episode_write", body="only body, no takeaway")
    with pytest.raises(Exception, match="no takeaway"):
        await _call(
            server,
            "episode_promote",
            episode_id=ep["id"],
            scopes=["tools"],
        )


async def test_episode_promote_unknown_id_raises(server: Any) -> None:
    """Unknown episode_id raises a clear error so the caller doesn't
    silently no-op."""
    with pytest.raises(Exception, match="no episode with id"):
        await _call(
            server,
            "episode_promote",
            episode_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            scopes=["tools"],
        )


async def test_loop_iteration_end_to_end_pattern(memory_dir: Path) -> None:
    """End-to-end exercise of the loop-iteration pattern documented in
    SKILL.md and the FastMCP instructions block.

    Iteration A:
    1. Writes durable memory + a journal entry with a takeaway.

    Iteration B (a fresh server = fresh recorder session_id, simulating
    a /loop subprocess):
    2. Calls episode_handoff() — sees A's takeaway.
    3. Promotes A's takeaway via episode_promote — durable memory
       commits, source episode is deleted.
    4. Writes its own memory; memory_search(since_prior_session=True)
       returns only B's writes (not A's), pinning the filter's
       "what THIS session has changed since the last other-session
       activity" semantic.
    5. A second episode_handoff() sees prior_session_id still resolved
       but the episode list is empty (it was promoted out).

    Pins the contract the docs / SKILL.md describe so a future refactor
    of any of the four episode tools surfaces immediately."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

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
        "episode_write",
        body="iter 1 — tuned GC, gophers cleared",
        takeaway="GC tuning fixed gopher frame drops",
    )

    # Iteration B: new session, picks up the handoff.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    handoff = await _call(server_b, "episode_handoff")
    assert handoff["prior_session_id"] is not None
    assert len(handoff["episodes"]) == 1
    assert handoff["episodes"][0]["takeaway"] == "GC tuning fixed gopher frame drops"

    # Promote A's takeaway into a durable memory.
    promotion = await _call(
        server_b,
        "episode_promote",
        episode_id=ep_a["id"],
        scopes=["projects:alpha"],
    )
    assert promotion["status"] == "committed"
    assert promotion["promoted_from_episode_id"] == ep_a["id"]

    # B writes its own memory. since_prior_session restricts to
    # memories updated AFTER the latest other-session event — A's
    # memory falls before that cut, B's promoted memory and this new
    # write fall after.
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

    # Second handoff: prior session still resolves, but the episode is
    # gone (deleted by promote on commit).
    handoff_2 = await _call(server_b, "episode_handoff")
    assert handoff_2["prior_session_id"] is not None
    assert handoff_2["episodes"] == []


async def test_episode_promote_keeps_episode_when_durability_rejects(
    server: Any,
) -> None:
    """If the promotion's body trips the durability gate (a transient
    marker), memory_write returns transient_warning and the source
    episode is LEFT IN PLACE so the caller can rephrase and retry."""
    ep = await _call(
        server,
        "episode_write",
        body="full context body for retry",
        # A transient marker in the takeaway — it'd get rejected by
        # the durability gate on promotion.
        takeaway="currently failing on step 3",
    )
    res = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["tools"],
    )
    assert res["status"] == "transient_warning"
    # Source episode should still exist.
    listed = _unwrap(await _call(server, "episode_search"))
    assert any(e["id"] == ep["id"] for e in listed)


async def test_episode_handoff_respects_explicit_prior_session_id(
    memory_dir: Path,
) -> None:
    """When the caller passes `prior_session_id`, the handler skips the
    event-log walk and reads directly from that session's directory.
    Useful for subagent handoff where the parent's session_id is
    already known."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Sessions A and B both write episodes.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A's note", takeaway="from A")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "episode_write", body="B's note", takeaway="from B")

    # Session C explicitly asks for A's session id (not the most recent).
    server_c = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Resolve A's id from its episode file. The frontmatter persists
    # session_id; iterating the episode store and matching on the body
    # is a known-good way to identify it without poking into the
    # server's private session attributes.
    from bettermemory.episodes import EpisodeStore

    ep_store = EpisodeStore(memory_dir)
    a_session_id: str
    for sid in ep_store.iter_session_ids():
        eps = ep_store.list_by_session(sid)
        if any("A's note" in e.body for e in eps):
            a_session_id = sid
            break
    else:
        raise AssertionError("could not locate session A's id")

    res = await _call(server_c, "episode_handoff", prior_session_id=a_session_id)
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
    result. `memory_search` and `memory_scope_overview` already enforce
    this via `should_include_for_caller`; the handoff has to mirror
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
        "episode_write",
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
    res = await _call(server_b, "episode_handoff")
    # The cross-worktree case must look like "no prior session in this
    # worktree" from B's perspective — same shape as a fresh store.
    assert res["prior_session_id"] is None
    assert res["episodes"] == []


# ---------------------------------------------------------------------------
# memory_write — inline curation hint (continued)
# ---------------------------------------------------------------------------


async def test_write_curation_hint_threshold_zero_disables(
    memory_dir: Path,
    monkeypatch: Any,
) -> None:
    """`curation_hint_threshold = 0` is the documented disable knob.
    Sets a numerical disable that's symmetric with the boolean
    `curation_hint_enabled` so config-driven kill switches don't
    require touching the boolean."""
    from bettermemory import health as _health
    from bettermemory.config import BehaviorConfig

    monkeypatch.setattr(
        _health,
        "curation_counts",
        lambda *_a, **_k: {
            "stale": 0,
            "never_verified": 0,
            "drifted": 0,
            "cold": 0,
            "dead": 999,
            "silent_misses": 0,
            "endorsement_debt": 0,
        },
    )
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(curation_hint_threshold=0),
    )
    server_x = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_x, "memory_write", content="x", scopes=["tools"])
    assert "curation_hint" not in res


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
# memory_update — category retag (added 1.3.0)
#
# Pre-1.3 the only way to change a memory's category was remove+rewrite,
# which wasted the original `created` timestamp and littered .tombstones/
# with edits. The new `category` parameter on memory_update lets callers
# retag a `fact` memory as `ambient` (or back) without that round trip,
# which matters for legacy memories written before the `ambient` tier
# existed in 1.2.0. `user-inference` is deliberately rejected here:
# that category gates the pending-confirm WRITE flow, and there is no
# equivalent gate on update.
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
# in `tests/test_llm.py`). `user-inference` is deliberately excluded
# because that tier requires the pending-confirm flow and update has
# no equivalent gate. Existing coverage hits both members
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


async def test_dedup_blocks_under_require_confirmation(
    confirming_server: tuple[Any, SessionState],
) -> None:
    """High overlap should return status:"duplicate" *instead of* staging.
    Otherwise the staged-write flow would let a duplicate through on
    confirm."""
    server, _state = confirming_server
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    # First write goes through the staged-write path → confirm it.
    confirmed = await _call(
        server, "memory_write_confirm", pending_id=first["pending_id"]
    )
    assert confirmed["status"] == "committed"

    second = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert second["status"] == "duplicate"
    assert "pending_id" not in second  # didn't even stage.


async def test_dedup_passes_through_to_pending_with_related(
    confirming_server: tuple[Any, SessionState],
) -> None:
    """Medium overlap under require_confirmation: still stages, but the
    pending response carries `related` so the user can see what's adjacent
    before confirming."""
    server, _state = confirming_server

    first_pending = await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx tls",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_write_confirm",
        pending_id=first_pending["pending_id"],
    )

    second = await _call(
        server,
        "memory_write",
        content="kubernetes ingress nginx logging",
        scopes=["tools"],
    )
    assert second["status"] == "pending"
    assert "related" in second
    assert second["related"][0]["relevance"] == "medium"


# ---------------------------------------------------------------------------
# Ordered-tuple pin for `_WRITE_GATES` — the WriteGate strategy chain
# orchestrated by `handlers/write.py:545`. ORDER IS LOAD-BEARING and the
# comment at `handlers/write.py:474-481` explicitly documents the
# rationale: transient before dedup so a transient parent doesn't route
# the writer to `memory_update`; scope-mismatch before dedup so a write
# tagged for a different scope doesn't get a duplicate hit; groundedness
# before dedup because (the comment's load-bearing example) a
# hallucinated write being reported as a "duplicate" of a real one is
# misleading; dedup before pending so the user-inference confirmation
# flow doesn't ask about a write we'd already reject; PendingGate last
# because everything else either rejects or accepts.
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
# a transient-parent write before routing the writer to update? Still
# stage user-inference last so dedup doesn't ask the user about a
# write we'd reject? If yes, update both. If unsure, don't reorder.
#
# Negative-control: swapping `DedupActiveGate` and `GroundednessGate`
# in `_WRITE_GATES` (a plausible "performance" reorder that puts
# cheaper checks first) fails
# `test_write_gates_match_expected_types_in_order` (tuple inequality
# — sequences differ at index 2 / 3). Revert restores green.
def test_write_gates_match_expected_types_in_order() -> None:
    """Guard so additions, deletions, AND reorders of ``_WRITE_GATES``
    (the ordered WriteGate strategy chain orchestrated by
    ``handlers/write.py:545``) are caught — uses *tuple* equality
    rather than set equality because gate precedence is load-bearing.

    The comment at ``handlers/write.py:474-481`` documents the
    invariant: transient/scope-mismatch/groundedness gates fire
    BEFORE dedup so (a) a hallucinated write can't masquerade as a
    duplicate of a real one, (b) a transient-parent write isn't
    routed to ``memory_update``, (c) a scope-mismatched write doesn't
    get a misleading duplicate hit; dedup BEFORE pending so the
    user-inference confirmation flow doesn't ask about a write we'd
    already reject; ``PendingGate`` last because everything else
    either rejects or accepts. A silent reorder breaks the
    security/correctness invariant the source comment documents.

    A future contributor reordering this tuple for performance must
    update both the source AND this expected tuple in the same
    commit, AND re-read the write.py:474-481 rationale to confirm
    the new ordering still preserves: hallucinated-before-dedup,
    transient-before-dedup, scope-before-dedup, dedup-before-pending,
    and pending-last."""
    from bettermemory.handlers.write import (
        DedupActiveGate,
        DedupTombstoneGate,
        GroundednessGate,
        PendingGate,
        ScopeMismatchGate,
        TransientGate,
        _WRITE_GATES,
    )

    expected: tuple[type, ...] = (
        TransientGate,
        ScopeMismatchGate,
        GroundednessGate,
        DedupActiveGate,
        DedupTombstoneGate,
        PendingGate,
    )
    actual = tuple(type(g) for g in _WRITE_GATES)
    assert actual == expected, (
        f"_WRITE_GATES drifted from documented order at "
        f"handlers/write.py:474-481. Got {[t.__name__ for t in actual]}, "
        f"expected {[t.__name__ for t in expected]}. Re-read the source "
        f"comment before reordering — this guards a security/correctness "
        f"invariant, not a stylistic choice."
    )


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
        content="kubernetes ingress nginx tls termination",
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

    await _call(server, "memory_update", id=written["id"], content="rewritten body")
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] is None


async def test_memory_verify_rejects_oversized_note(server: Any) -> None:
    """The MCP entry-point cap on `note` matches the web /verify
    endpoint (500 chars). Without this, a hostile client could
    inflate the JSONL event log with multi-megabyte notes."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    with pytest.raises(Exception, match="cap is 500"):
        await _call(server, "memory_verify", id=written["id"], note="x" * 501)


async def test_memory_verify_accepts_max_length_note(server: Any) -> None:
    """Sanity check: 500 chars exactly is accepted (cap is inclusive)."""
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    res = await _call(server, "memory_verify", id=written["id"], note="x" * 500)
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
    server: Any,
) -> None:
    """Body edits invalidate the structured attestation in lockstep with
    last_verified_at. Carrying verified_paths / verified_commits /
    verified_versions forward across a body rewrite would let a later
    memory_search read e.g. verified_paths=['/etc/foo'] against new
    prose that no longer mentions /etc/foo, suppressing the path-drift
    signal it should have produced."""
    written = await _call(
        server, "memory_write", content="claim about /etc/foo", scopes=["tools"]
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        verified_paths=["/etc/foo"],
        verified_commits=["abc1234"],
        verified_versions=["1.2.3"],
    )
    pre = await _call(server, "memory_show", id=written["id"])
    assert pre["verified_paths"] == ["/etc/foo"]
    assert pre["verified_commits"] == ["abc1234"]
    assert pre["verified_versions"] == ["1.2.3"]

    await _call(server, "memory_update", id=written["id"], content="rewritten body")
    post = await _call(server, "memory_show", id=written["id"])
    assert post["last_verified_at"] is None
    assert post["verified_paths"] == []
    assert post["verified_commits"] == []
    assert post["verified_versions"] == []


async def test_memory_update_scope_only_preserves_verified_attestation(
    server: Any,
) -> None:
    """Scope / confidence / category / links edits don't touch the body's
    claims; the structured attestation must survive alongside
    last_verified_at."""
    written = await _call(
        server, "memory_write", content="claim about /etc/foo", scopes=["tools"]
    )
    await _call(
        server,
        "memory_verify",
        id=written["id"],
        verified_paths=["/etc/foo"],
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
    assert post["verified_paths"] == ["/etc/foo"]
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
# memory_list — last_verified_at threaded through
# ---------------------------------------------------------------------------


async def test_memory_list_summary_carries_last_verified_at(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_verify", id=written["id"])
    listing = _unwrap(await _call(server, "memory_list"))
    assert len(listing) == 1
    assert listing[0]["last_verified_at"] is not None


async def test_memory_list_with_bodies_carries_last_verified_at(
    server: Any,
) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_verify", id=written["id"])
    listing = _unwrap(await _call(server, "memory_list", with_bodies=True))
    assert listing[0]["last_verified_at"] is not None


# ---------------------------------------------------------------------------
# memory_scope_overview — counts only, respects auto_scope and disabled scopes
# ---------------------------------------------------------------------------


async def test_scope_overview_empty_store(server: Any) -> None:
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["total"] == 0
    assert overview["scopes"] == {}


async def test_scope_overview_counts_per_scope(server: Any) -> None:
    """auto_scope=False counts everything regardless of origin — useful for
    the cross-project view, and avoids needing a fake repo in test setup."""
    await _call(server, "memory_write", content="alpha 1", scopes=["projects:alpha"])
    await _call(server, "memory_write", content="alpha 2", scopes=["projects:alpha"])
    await _call(
        server,
        "memory_write",
        content="alpha + tools",
        scopes=["projects:alpha", "tools"],
    )
    await _call(server, "memory_write", content="just tools", scopes=["tools"])

    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["total"] == 4
    assert overview["scopes"] == {"projects:alpha": 3, "tools": 2}


async def test_scope_overview_orders_by_count_desc_then_name(server: Any) -> None:
    """Stable ordering: count desc, ties broken by name asc. The model
    should be able to count on `scopes.popitem()` returning the busiest
    scope across calls."""
    await _call(server, "memory_write", content="a", scopes=["tools"])
    await _call(server, "memory_write", content="b", scopes=["zeta"])
    await _call(server, "memory_write", content="c", scopes=["tools"])
    await _call(server, "memory_write", content="d", scopes=["alpha"])
    await _call(server, "memory_write", content="e", scopes=["alpha"])

    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    keys = list(overview["scopes"].keys())
    assert keys == ["alpha", "tools", "zeta"]


async def test_scope_overview_omits_no_bodies_or_ids(server: Any) -> None:
    """The whole point of this tool is that it's a cheap session-start
    hint — no bodies, no IDs, no summaries. Adding any of those would
    re-introduce the auto-context-load failure mode bettermemory exists
    to avoid."""
    await _call(server, "memory_write", content="secret body", scopes=["tools"])
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    payload = json.dumps(overview)
    assert "secret body" not in payload
    # Returned object never carries IDs or summaries on the per-scope rows.
    for value in overview["scopes"].values():
        assert isinstance(value, int)


async def test_scope_overview_respects_disabled_scopes(server: Any) -> None:
    await _call(server, "memory_write", content="alpha", scopes=["projects:alpha"])
    await _call(server, "memory_write", content="tools", scopes=["tools"])
    await _call(server, "memory_scope_disable", scope="projects:alpha")

    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert "projects:alpha" not in overview["scopes"]
    assert "projects:alpha" in overview["disabled_scopes"]
    assert overview["total"] == 1


async def test_scope_overview_returns_current_repo_field(server: Any) -> None:
    """The field is always present even when null, so the model can branch
    on `overview['current_repo']` without a KeyError."""
    overview = await _call(server, "memory_scope_overview", auto_scope=True)
    assert "current_repo" in overview
    assert "current_cwd" in overview


async def test_scope_overview_delta_field_present_in_return_shape(server: Any) -> None:
    """`curation_pending_new_since_last_session` is always part of the
    return dict — null when no prior session exists, a sibling dict to
    `curation_pending` once a baseline is established. Always-present
    fields let the model branch without a KeyError guard."""
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert "curation_pending_new_since_last_session" in overview


async def test_scope_overview_delta_null_on_first_session(server: Any) -> None:
    """First session on a fresh store has no prior boundary to delta
    against — surface `null` so the model treats it as "no baseline"
    and falls back to the absolute `curation_pending` view rather than
    interpreting an all-zero delta as "nothing has changed."""
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["curation_pending_new_since_last_session"] is None


async def test_scope_overview_delta_dict_when_prior_session_exists(
    memory_dir: Path,
) -> None:
    """Once events from a different session_id exist in the log, the
    delta dict materialises. Same key set as `curation_pending`."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    # First "session": write a memory and call scope_overview so the
    # event log carries something tagged with this session_id.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="x", scopes=["tools"])
    await _call(server_a, "memory_scope_overview", auto_scope=False)

    # Second "session": fresh SessionState gives a new session_id, so
    # the events from session A are now "prior" relative to session B.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    overview = await _call(server_b, "memory_scope_overview", auto_scope=False)
    delta = overview["curation_pending_new_since_last_session"]
    assert isinstance(delta, dict)
    # Same key set as the absolute view — the model should not need a
    # different branch for each.
    assert set(delta.keys()) == set(overview["curation_pending"].keys())


async def test_scope_overview_delta_uses_recorder_session_not_state(
    memory_dir: Path,
) -> None:
    """Multi-client regression: in SessionRegistry mode each request's
    `state.session_id` differs from the recorder's process-wide
    `session_id`. Every event the recorder writes carries the
    recorder's id, so the handler must use the recorder's id (not
    `state.session_id`) when locating the prior session boundary.
    Otherwise the registry-path delta misidentifies the current
    session's events as "prior" and collapses to a wrong value.

    This test pins the contract by passing an explicit mismatched
    state + recorder pair to `build_server`."""
    from bettermemory.events import Recorder

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    # Session A: seed the event log with a prior-session event.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="seed", scopes=["tools"])

    # Session B: deliberately mismatched ids to simulate the
    # SessionRegistry multi-client case.
    rec_b = Recorder(
        root=memory_dir,
        session_id="recorder-id-B",
    )
    state_b = SessionState(session_id="client-state-id-B")
    server_b = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state_b,
        recorder=rec_b,
    )
    overview = await _call(server_b, "memory_scope_overview", auto_scope=False)
    # The boundary should resolve against the prior session (server_a's
    # events) using the recorder's id — surface a delta dict, not None.
    assert overview["curation_pending_new_since_last_session"] is not None
    # And the recorded scope_overview event uses recorder-id-B as its
    # session tag (proves the audit-trail stays internally consistent).
    events_path = memory_dir / ".events.jsonl"
    last = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if json.loads(line).get("kind") == "scope_overview"
    ][-1]
    assert last["session"] == "recorder-id-B"


async def test_scope_overview_delta_event_recorded_carries_boundary(
    memory_dir: Path,
) -> None:
    """The `scope_overview` event in the log carries the prior boundary
    timestamp (or null), so a downstream `eval` pass can correlate the
    delta dict back to the cutoff that produced it without re-running
    `find_prior_session_boundary` against the live log state."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    # Seed a prior-session event so the boundary resolves to a non-null
    # value on the second session_overview call.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "memory_write", content="y", scopes=["tools"])

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "memory_scope_overview", auto_scope=False)

    # Read the events log directly and pull the most recent scope_overview
    # record. The field name on the recorded event mirrors the return-dict
    # key with a `prior_session_boundary` companion so an eval pass can
    # re-trace the math.
    events_path = memory_dir / ".events.jsonl"
    lines = events_path.read_text().splitlines()
    scope_events = [
        json.loads(line)
        for line in lines
        if json.loads(line)["kind"] == "scope_overview"
    ]
    assert scope_events, "expected at least one scope_overview event"
    latest = scope_events[-1]
    assert "prior_session_boundary" in latest
    assert latest["prior_session_boundary"] is not None
    assert "curation_pending_new_since_last_session" in latest


async def test_scope_overview_recently_removed_counts_recent_tombstones(
    server: Any,
) -> None:
    """A memory removed within the trailing 7-day window surfaces in
    `recently_removed_in_worktree` so the model knows it shouldn't
    re-cover that ground without a reason."""
    written = await _call(
        server,
        "memory_write",
        content="auth uses bcrypt",
        scopes=["projects:auth"],
    )
    await _call(server, "memory_remove", id=written["id"], reason="outdated")

    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["recently_removed_in_worktree"] >= 1


async def test_scope_overview_recently_removed_zero_when_clean(
    server: Any,
) -> None:
    """A fresh store has no removals; the field surfaces 0."""
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["recently_removed_in_worktree"] == 0


async def test_endorsement_debt_ratio_threshold_threaded_to_all_callsites(
    memory_dir: Path,
) -> None:
    """Regression: `BehaviorConfig.endorsement_debt_ratio_threshold` must
    drive every `curation_counts` callsite, not just `memory_health`.
    Earlier, only the deep `memory_health` surface read the knob — the
    `memory_scope_overview` rollup and the per-write `curation_hint`
    nudge fell back to the strict 0.0 default, so a user who configured
    `endorsement_debt_ratio_threshold=0.5` saw the loosened bucket on
    `memory_health` but the strict bucket on every session-start hint.

    We seed a memory with 5 retrievals and 4 applieds where exactly 1
    is explicit (ratio 1/4 = 0.25). At the configured 0.5 threshold the
    memory IS endorsement debt (count = 1); at the strict 0.0 default
    it is NOT (count = 0). Both `memory_scope_overview` and the
    `curation_hint` block must surface the 0.5 reading.
    """
    from bettermemory.config import BehaviorConfig

    # Build the server with the loosened threshold and a low
    # curation-hint threshold so a single endorsement_debt entry trips
    # the inline nudge on `memory_write`.
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(
            endorsement_debt_ratio_threshold=0.5,
            curation_hint_threshold=1,
        ),
    )
    server_x = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )

    # Write the candidate memory through the server so it lives in the
    # store with a real id. Use an `infrastructure` scope so it isn't
    # tagged ambient (ambient memories never land in endorsement_debt).
    written = await _call(
        server_x,
        "memory_write",
        content="postgres listens on 5432 in prod",
        scopes=["infrastructure"],
    )
    mem_id = written["id"]

    # Seed events directly into the active log so the retrieval /
    # applied counts cross the endorsement-debt floor (5 retrievals).
    # We bypass the recorder here because the recorder timestamps with
    # `_utcnow_iso()` and we want determinism; the on-disk format is
    # one JSON object per line, so a direct append matches what
    # `iter_all_events` consumes.
    events_path = memory_dir / ".events.jsonl"
    extra_lines: list[str] = []
    for i in range(5):
        extra_lines.append(
            json.dumps(
                {
                    "ts": f"2026-04-{i + 1:02d}T00:00:00Z",
                    "session": "sess_seed",
                    "kind": "search",
                    "returned": [mem_id],
                }
            )
        )
    # 3 auto applieds.
    for i in range(3):
        extra_lines.append(
            json.dumps(
                {
                    "ts": f"2026-04-{i + 10:02d}T00:00:00Z",
                    "session": "sess_seed",
                    "kind": "use",
                    "ids": [mem_id],
                    "outcome": "applied",
                    "auto": True,
                }
            )
        )
    # 1 explicit applied → ratio 1/4 = 0.25.
    extra_lines.append(
        json.dumps(
            {
                "ts": "2026-04-15T00:00:00Z",
                "session": "sess_seed",
                "kind": "use",
                "ids": [mem_id],
                "outcome": "applied",
            }
        )
    )
    with events_path.open("ab") as f:
        f.write(("\n".join(extra_lines) + "\n").encode("utf-8"))

    # --- Surface 1: memory_scope_overview ---
    overview = await _call(server_x, "memory_scope_overview", auto_scope=False)
    assert overview["curation_pending"]["endorsement_debt"] == 1, (
        "scope_overview must apply the configured threshold (0.5); the "
        "seeded memory has ratio 1/4 = 0.25 < 0.5 and should land in the "
        "endorsement_debt bucket."
    )

    # --- Surface 2: curation_hint on memory_write ---
    # Fresh SessionState so the one-shot `curation_hint_checked` flag
    # is False, and a real event store so the second callsite walks
    # the same seeded log. With curation_hint_threshold=1 and at least
    # one endorsement_debt entry, the hint must attach.
    server_hint = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )
    hint_res = await _call(
        server_hint,
        "memory_write",
        content="another write to trigger the curation_hint",
        scopes=["tools"],
    )
    assert "curation_hint" in hint_res, (
        "curation_hint on memory_write must also see the loosened "
        "threshold; otherwise the hint disagrees with the overview."
    )
    assert hint_res["curation_hint"]["counts"]["endorsement_debt"] == 1


async def test_endorsement_debt_ratio_threshold_default_still_strict(
    memory_dir: Path,
) -> None:
    """Back-compat: with the default 0.0 threshold, the same seeded
    state (one explicit applied present) must NOT count as
    endorsement_debt on any surface. This locks the default behaviour
    against accidental loosening when threading the knob through."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_x = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )
    written = await _call(
        server_x,
        "memory_write",
        content="postgres listens on 5432 in prod",
        scopes=["infrastructure"],
    )
    mem_id = written["id"]

    events_path = memory_dir / ".events.jsonl"
    extra_lines: list[str] = []
    for i in range(5):
        extra_lines.append(
            json.dumps(
                {
                    "ts": f"2026-04-{i + 1:02d}T00:00:00Z",
                    "session": "sess_seed",
                    "kind": "search",
                    "returned": [mem_id],
                }
            )
        )
    for i in range(3):
        extra_lines.append(
            json.dumps(
                {
                    "ts": f"2026-04-{i + 10:02d}T00:00:00Z",
                    "session": "sess_seed",
                    "kind": "use",
                    "ids": [mem_id],
                    "outcome": "applied",
                    "auto": True,
                }
            )
        )
    extra_lines.append(
        json.dumps(
            {
                "ts": "2026-04-15T00:00:00Z",
                "session": "sess_seed",
                "kind": "use",
                "ids": [mem_id],
                "outcome": "applied",
            }
        )
    )
    with events_path.open("ab") as f:
        f.write(("\n".join(extra_lines) + "\n").encode("utf-8"))

    overview = await _call(server_x, "memory_scope_overview", auto_scope=False)
    assert overview["curation_pending"]["endorsement_debt"] == 0


# ---------------------------------------------------------------------------
# Tools list — new tools registered
# ---------------------------------------------------------------------------


async def test_new_tools_registered(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_verify" in names
    assert "memory_scope_overview" in names


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


async def test_memory_list_summary_carries_verification_block(server: Any) -> None:
    """memory_list (cheap-triage view) must also expose the block —
    a curator scrolling the list shouldn't have to call memory_show
    just to see whether a row is fresh."""
    await _call(server, "memory_write", content="unverified entry", scopes=["tools"])
    listing = _unwrap(await _call(server, "memory_list"))
    assert len(listing) >= 1
    assert "verification" in listing[0]
    assert listing[0]["verification"]["status"] == "never"


async def test_memory_list_with_bodies_carries_verification_block(
    server: Any,
) -> None:
    """The with_bodies=True variant uses a different serialiser
    internally; assert the block appears there too."""
    await _call(
        server, "memory_write", content="another unverified entry", scopes=["tools"]
    )
    listing = _unwrap(await _call(server, "memory_list", with_bodies=True))
    assert len(listing) >= 1
    assert "verification" in listing[0]
    assert listing[0]["verification"]["status"] == "never"


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
# MCP `instructions` block — length budget regression
# ---------------------------------------------------------------------------


def test_instructions_block_fits_under_truncation_budget(server: Any) -> None:
    """Claude Code (empirically validated against 2.1.x) truncates the
    server-level MCP `instructions` block at roughly 1.8KB and renders
    "…" plus a `[truncated]` marker after the cut. The current copy is
    sized for ~1500 chars to leave headroom; re-growing it past 1700 is
    the regression this guard catches.

    The truncation is consumer-side (Claude Code, not bettermemory),
    so the only fix when this fires is to shorten the body — push the
    detail down into individual tool descriptions, which are NOT
    subject to the same truncation. The optional system-prompt
    addendum (`docs/system_prompt.md`) carries the long-form policy
    for clients whose users want to paste it into a project CLAUDE.md.

    Anything is accessible at `server.instructions` and is the same
    string FastMCP advertises over the wire."""
    body = server.instructions or ""
    # Hard ceiling: comfortably below the empirical 1830-char cut.
    assert len(body) <= 1700, (
        f"instructions block grew to {len(body)} chars; Claude Code's "
        f"~1.8KB truncation will cut mid-sentence. Trim or move detail "
        f"into tool descriptions."
    )
    # Soft floor: catch an accidental wipe of the policy.
    assert len(body) >= 800, (
        f"instructions block shrank to {len(body)} chars — the load-bearing "
        f"opt-in retrieval / verification policy is gone."
    )
    # Byte length matters too on clients that count bytes; the body uses
    # a few non-ASCII characters (em-dashes, ellipsis) so it's slightly
    # longer in UTF-8.
    assert len(body.encode("utf-8")) <= 1750


def test_instructions_block_carries_load_bearing_phrases(server: Any) -> None:
    """A trimming pass that drops one of these kills the policy. The
    list is deliberately short — the rules we cannot afford to lose if
    a future edit is told only "make it shorter"."""
    body = server.instructions or ""
    must_have = [
        "OPT-IN retrieval",
        "memory_search",
        "memory_scope_overview",
        "memory_record_use",
        "memory_verify",
        # The transparency rule — without it the user never knows when
        # stored context shaped a reply.
        "Using your stored preference",
        # The verification rule.
        "spot-check",
        # The proactive-writing axis. Retrieval is opt-in; writing is
        # the opposite — a routine reflex the model should reach for
        # whenever something durable lands in the conversation. Without
        # these phrases the block reads as if only retrieval is policed
        # and writing is left undefined, which the previous "lock writing
        # down further" regression demonstrated is misinterpreted as
        # "default to NOT writing either". Keeping these load-bearing
        # prevents a future trimming pass from silently un-doing the
        # write-side calibration.
        "memory_write",
        "PROACTIVE",
        "your job is to capture",
    ]
    missing = [p for p in must_have if p not in body]
    assert not missing, f"instructions lost load-bearing phrases: {missing}"


# ---------------------------------------------------------------------------
# Backward-scan early-exit in _already_recorded_pending_ids
# ---------------------------------------------------------------------------


def test_already_recorded_pending_ids_early_exits_on_old_events(
    memory_dir: Path,
) -> None:
    """The dedup scan walks the active log backward and bails as soon
    as event timestamps fall behind the oldest pending token's
    `issued_at`. Without the early-exit, every call to this function
    walks every event in the active log — O(N) per turn against a
    rotation cap of 10 MB / tens of thousands of events.

    Two assertions live here:

    1. Correctness — the function returns exactly the memory_ids whose
       use-events are timestamped at or after the corresponding token's
       `issued_at`. The 2.6.7 timestamp-guard semantics must survive
       the optimisation.
    2. Performance — a log of 10k old (pre-token) events plus a few
       recent use-events resolves in well under 100ms. Generous
       threshold so the test isn't flaky on slow CI; the early-exit
       brings the realistic wall-clock down by orders of magnitude
       relative to the full forward scan.
    """
    import time as _time

    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import Recorder
    from bettermemory.session import PendingUseToken, SessionState

    memory_dir.mkdir(parents=True, exist_ok=True)

    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id)

    # Phase 1: write 10_000 ancient events to the active log. Predates
    # any pending token; the early-exit must bail before scanning them.
    for i in range(10_000):
        recorder.record("turn_audited", session_id=state.session_id, hits=[], note=i)

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

    # Correctness: every minted token should be reported as
    # "already recorded" — the 5 use events all match.
    start = _time.perf_counter()
    result = _already_recorded_pending_ids(state, recorder)
    elapsed = _time.perf_counter() - start

    assert result == set(pending_mids), f"expected all pending ids back, got {result}"

    # Performance: with the backward early-exit, the scan touches a
    # handful of recent events (the 5 use events + the trailing
    # turn_audited barrier) before bailing — typically <10ms on a
    # warm runner. Threshold set at 500ms to absorb shared-CI noise
    # (observed 151ms on a slow ubuntu-latest slot during the 3.0.1
    # release run, well within optimisation-working territory).
    # Without the optimisation, the full forward scan over 10k+
    # events comfortably exceeds even this generous bound — the
    # gap stays large enough to detect regression.
    assert elapsed < 0.5, (
        f"_already_recorded_pending_ids took {elapsed:.3f}s for 10k-event "
        f"log; backward early-exit appears not to be triggering"
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

    memory_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id)

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
