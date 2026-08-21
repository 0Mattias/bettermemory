"""Tests for server.py — tool registration and end-to-end behavior.

We exercise the registered tools via the SDK's `call_tool` rather than
spinning up the full stdio transport.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import argparse
import json
import re
import warnings
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


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
        assert _input_schema(tool), f"{tool.name} missing inputSchema"


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
    # Structured returns under the "result" key for the SDK — handle both.
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


async def test_search_since_prior_session_excludes_boundary_memory(
    memory_dir: Path,
) -> None:
    """`since_prior_session=True` is *exclusive* at the boundary: a memory
    whose `updated` equals `prior_boundary` belongs to the prior session
    (the boundary IS that session's last event ts, per
    `find_prior_session_boundary`) and must not surface in the current
    session's delta. Mirrors `test_curation_counts_since_filter_is_exclusive_at_boundary`
    in tests/test_health.py — same concept, same answer across both surfaces
    (memory_search + memory_scope_overview/curation_counts) the api docs
    pair together as the "what's new since last session" workflow. A naive
    inclusive `>=` would double-count the boundary memory across the two
    surfaces."""
    from bettermemory.events import iter_all_events
    from bettermemory.health import find_prior_session_boundary

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A: write one memory. The `memory_write` recorder event lands
    # in the log with its own `_utcnow_iso()` ts, which becomes session B's
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
        iter_all_events(memory_dir),
        # Mirror the handler: the boundary is "latest event ts from a
        # session other than the current one". `build_server` propagates
        # the bare-`SessionState` path's `state.session_id` to the
        # recorder (`builder.py:125`), so reading `state_b.session_id`
        # gives us the same value the handler will use.
        state_b.session_id,
    )
    assert prior_boundary is not None, "session A's write should have seeded a boundary"

    # Force memory A's `updated` to exactly equal `prior_boundary`. The
    # on-disk path is what `Store.load_all` re-reads, so writing through
    # `_write_path` with a model_copy of A is sufficient — we deliberately
    # bypass `Store.update` because that helper bumps `updated` to
    # `utcnow()` and would defeat the test's pin.
    path_a = store_b._find_path_for_id(written_a["id"])
    assert path_a is not None
    # Load the on-disk Memory and re-stamp `updated`. Going through the
    # Store's own loader (rather than reconstructing from the dict) keeps
    # this resilient to future Memory-model fields we'd otherwise drop.
    loaded_a = next(m for m in store_b.load_all() if m.id == written_a["id"])
    pinned_a = loaded_a.model_copy(update={"updated": prior_boundary})
    store_b._write_path(path_a, pinned_a)

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

    from bettermemory.events import iter_events

    search_events = [e for e in iter_events(memory_dir) if e["kind"] == "search"]
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
    await _call(server, "memory_scope_disable", scope="projects:alpha")
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
            "unique_silent_miss_memories": 0,
            "cold_endorsement_memories": 1,
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
        "cold_endorsement_memories": 1,
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
            "unique_silent_miss_memories": 0,
            "cold_endorsement_memories": 0,
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
            "unique_silent_miss_memories": 0,
            "cold_endorsement_memories": 0,
        },
    )
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(curation_hint_enabled=False),
    )
    server_x = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_x, "memory_write", content="x", scopes=["tools"])
    assert "curation_hint" not in res


# Same shape as `tests/test_prompts.py`'s tool-reference regex: a
# `memory_*` / `episode_*` identifier not immediately followed by `=`
# (which would make it a keyword-argument name rather than a tool).
_HINT_TOOL_REF_RE = re.compile(r"\b((?:memory|episode)_[a-z_]+)\b(?!\s*=)")

# The other half of the same surface. Routing the lean install to the
# CLI only moves the exposure: `bettermemory health` and `bettermemory
# consolidate --acknowledge-debt` are as renameable as the tool names
# were, and a stale one misdirects exactly the same model. Backticked
# spans are the convention the message already uses for a command line,
# so that is what gets pulled out.
_HINT_CLI_REF_RE = re.compile(r"`bettermemory ([^`]+)`")


def _cli_root_parser() -> argparse.ArgumentParser:
    """The parser `bettermemory --help` is rendered from."""
    from bettermemory.cli import _build_parser

    parser, _registry = _build_parser()
    return parser


def _subcommand_choices(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser] | None:
    """The subcommands `parser` accepts, or None if it takes none.

    Read off the `_SubParsersAction` argparse itself consults at parse
    time, not off the registry dict `_build_parser` returns: that dict's
    keys are literals written beside each `add_subparser` call, so a
    route renamed inside its own module would leave them stale while the
    real CLI moved. `choices` is the mapping that actually decides
    whether `bettermemory <name>` runs.
    """
    actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(actions) <= 1, (
        f"{parser.prog} registered {len(actions)} subparser actions; the "
        f"resolution below would silently read only the first."
    )
    return dict(actions[0].choices) if actions else None


def _options(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    """Every flag string `parser` accepts, mapped to its action."""
    return {
        option: action for action in parser._actions for option in action.option_strings
    }


def _resolve_cli_span(span: str) -> list[str]:
    """Walk one `bettermemory …` span through the real parser tree.

    Returns the tokens that were checked, so a caller can assert the walk
    was not a no-op. Bare words descend into the subparser registered
    under that name; `--flags` are looked up on whichever parser is
    current, and a flag that takes a value consumes the token after it so
    the value is never mistaken for a subcommand.
    """
    parser = _cli_root_parser()
    trail = ["bettermemory"]
    checked: list[str] = []
    tokens = span.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("-"):
            flag, _, inline_value = token.partition("=")
            accepted = _options(parser)
            action = accepted.get(flag)
            assert action is not None, (
                f"curation_hint says `bettermemory {span}`, but "
                f"`{' '.join(trail)}` does not accept {flag}. Accepted "
                f"here: {sorted(accepted)}."
            )
            if action.nargs != 0 and not inline_value:
                index += 1  # the flag's value, not a subcommand
            checked.append(flag)
            continue
        choices = _subcommand_choices(parser)
        assert choices is not None, (
            f"curation_hint says `bettermemory {span}`, but "
            f"`{' '.join(trail)}` takes no subcommands, so {token!r} "
            f"cannot be one."
        )
        assert token in choices, (
            f"curation_hint routes users to `bettermemory {span}`, but "
            f"`{' '.join(trail)}` has no {token!r} subcommand — it "
            f"registers {sorted(choices)}. The hint reaches every "
            f"install; a route it names has to be a route that runs."
        )
        parser = choices[token]
        trail.append(token)
        checked.append(token)
    return checked


async def _lean_hint(
    memory_dir: Path, config_dir: Path, monkeypatch: Any
) -> tuple[dict[str, Any], set[str]]:
    """Fire the curation hint on a LEAN server; return the hint and its tools.

    The lean surface is derived from `load_config()` against a config
    file that doesn't exist yet — the exact path `bettermemory` takes on
    a fresh install — rather than by restating `full_tool_surface=False`
    here. If the loader default ever flips, this moves with it instead of
    silently continuing to test the old policy.
    """
    from bettermemory import health as _health
    from bettermemory.config import load_config

    loaded = load_config(config_dir / "config.toml")
    assert loaded.behavior.full_tool_surface is False, (
        "load_config() no longer defaults full_tool_surface to False, so "
        "this test is no longer building the surface a stock install gets."
    )

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=loaded.behavior,
    )
    lean = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    registered = {tool.name for tool in await lean.list_tools()}
    assert "memory_health" not in registered, (
        "the lean server registered memory_health — the surface under test "
        "is not the lean one and the assertion below would be vacuous."
    )

    # Pressure high enough to cross the default threshold on every axis,
    # so the message has to speak to all three.
    def fake_counts(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {
            "stale": 0,
            "never_verified": 0,
            "drifted": 3,
            "cold": 0,
            "dead": 3,
            "silent_misses": 0,
            "unique_silent_miss_memories": 0,
            "cold_endorsement_memories": 11,
        }

    monkeypatch.setattr(_health, "curation_counts", fake_counts)
    res = await _call(lean, "memory_write", content="first", scopes=["tools"])

    assert res["status"] == "committed"
    assert "curation_hint" in res, (
        "the curation hint did not fire, so there is no message to check."
    )
    return res["curation_hint"], registered


async def test_curation_hint_message_names_no_lean_absent_tool(
    memory_dir: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    """The `curation_hint` message may not name a tool the lean server lacks.

    This is the coverage ratchet applied to a runtime payload rather than
    a doc. The hint fires from `memory_write`, which is registered under
    both surfaces, but the message used to say "Call memory_health for
    full buckets" — and `memory_health` is gated behind `[behavior]
    full_tool_surface`, which `load_config()` defaults to False. A stock
    install was being told to call a tool it had not been given.

    The assertion is deliberately mechanical: extract every tool-shaped
    identifier from the message and require it to be registered. Prose
    review cannot catch a name that becomes lean-absent later; this can.
    """
    hint, registered = await _lean_hint(memory_dir, tmp_path, monkeypatch)

    named = set(_HINT_TOOL_REF_RE.findall(hint["message"]))
    assert named, (
        "the hint message names no tools at all. Either the message was "
        "rewritten to be tool-free (then delete this test) or the regex "
        "stopped matching and the check below is vacuous."
    )
    assert not named - registered, (
        f"curation_hint names tools absent from the lean surface: "
        f"{sorted(named - registered)}. The hint reaches every install, "
        f"so every route it names has to exist on every install — use the "
        f"`bettermemory` CLI for anything gated behind full_tool_surface."
    )


async def test_curation_hint_names_only_cli_routes_that_exist(
    memory_dir: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    """Every `bettermemory …` the hint names has to be a command that runs.

    The sibling test above pushed the lean install off `memory_health`
    and onto `bettermemory health` — which fixes the surface mismatch and
    then leaves the replacement unguarded, since `_HINT_TOOL_REF_RE` only
    matches `memory_*` / `episode_*` identifiers. Renaming or dropping a
    CLI subcommand would leave the hint pointing at nothing with nothing
    failing, which is the same defect one rename later.

    Resolution goes through `_build_parser()` — the parser `bettermemory
    --help` is built from — rather than a list of subcommand names
    restated here, because a restated list is that defect one level up:
    it would keep passing after the CLI moved.
    """
    hint, _registered = await _lean_hint(memory_dir, tmp_path, monkeypatch)

    spans = _HINT_CLI_REF_RE.findall(hint["message"])
    assert spans, (
        "the hint message names no `bettermemory …` command at all. Either "
        "the remedies moved back onto MCP tools (then delete this test and "
        "check they survive the lean surface) or the message stopped "
        "backticking its command lines and this check went vacuous."
    )
    for span in spans:
        checked = _resolve_cli_span(span)
        assert checked, (
            f"`bettermemory {span}` resolved to no tokens at all, so "
            f"nothing about it was actually checked."
        )


async def test_curation_hint_routes_cold_endorsements_to_acknowledge_debt(
    memory_dir: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    """The cold-endorsement remedy names the pass that actually clears it.

    `cold_endorsement_memories` is defined by `explicit_applied_count ==
    0` (`health._is_weakly_endorsed`). `memory_verify` writes a
    verification, not a use event, so it cannot decrement that counter —
    the message used to offer it anyway, aiming the drift remedy at a
    bucket it cannot move. The pass that does move it is `consolidate
    --acknowledge-debt`, which writes one explicit `use(applied)` row per
    cold row, and it is what `memory_health`'s own
    `cleanup_cold_endorsements` recommendation names.

    The flag itself is pinned by `_resolve_cli_span` in the CLI-route
    test above, which looks `--acknowledge-debt` up in the `consolidate`
    subparser's own option strings; this test only pins that the message
    still names it.
    """
    hint, _registered = await _lean_hint(memory_dir, tmp_path, monkeypatch)

    assert hint["counts"]["cold_endorsement_memories"] == 11, (
        "the seeded cold-endorsement count did not reach the hint, so the "
        "message under test was not produced by that axis firing."
    )
    message = hint["message"]
    assert "--acknowledge-debt" in message, (
        "curation_hint no longer names `consolidate --acknowledge-debt`; "
        "with cold endorsements in the pressure sum it is the only route "
        "that clears them."
    )
    assert "memory_verify does not touch that axis" in message, (
        "curation_hint dropped the note that memory_verify cannot clear "
        "cold endorsements. The tool is still named for the drift axis, so "
        "without the disclaimer the old wrong-axis reading returns."
    )


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
    does, just without the durability gate. The SDK wraps the
    underlying ValueError as a ToolError; both pass through `Exception`."""
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode_write", body="")
    with pytest.raises(Exception, match="non-empty"):
        await _call(server, "episode_write", body="   \n\t  ")


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
        await _call(server, "episode_write", body=big_body)


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
    res = await _call(server, "episode_write", body="under the cap")
    assert res["status"] == "committed"
    assert res["session_id"].startswith("sess_")


async def test_episode_write_rejects_oversized_takeaway(memory_dir: Path) -> None:
    """An episode_write takeaway exceeding [behavior] max_takeaway_bytes
    is rejected at the handler — same ValueError shape as the body cap,
    but the message names the takeaway cap so the operator knows which
    knob to turn. Pre-fix this was a silent data loss path: a takeaway
    over 64 KB corrupted the YAML frontmatter, every subsequent
    `_frontmatter.loads` raised ValueError, and `list_by_session`
    swallowed — the episode looked committed (status="committed"
    returned) but vanished from every read surface (search / handoff /
    promote). The handler-boundary cap closes the path before the file
    ever lands on disk."""
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
            "episode_write",
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
        "episode_write",
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
    res = await _call(server, "episode_write", body="no takeaway here")
    assert res["status"] == "committed"
    assert res["takeaway"] is None


async def test_episode_write_rejects_oversized_scope_list(memory_dir: Path) -> None:
    """A scopes list exceeding [behavior] max_scopes_per_write is rejected
    at the handler. Same silent-data-loss class as the takeaway cap (t16):
    scopes serialise into YAML frontmatter, which `_frontmatter._MAX_YAML_BYTES`
    caps at 64 KB to neutralise alias-expansion DoS. Roughly 2200 short
    scope names push the frontmatter past that ceiling — the loader then
    raises `ValueError` on every subsequent read and the episode vanishes
    from every read surface (search / handoff / promote) despite the write
    returning `status="committed"`. The handler-boundary cap closes the
    path before the file ever lands on disk."""
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
            "episode_write",
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
        "episode_write",
        body="under all caps",
        scopes=["tools", "infrastructure"],
    )
    assert res["status"] == "committed"
    assert res["scopes"] == ["tools", "infrastructure"]


async def test_memory_write_rejects_oversized_scope_list(memory_dir: Path) -> None:
    """memory_write applies the same scope-list cap as episode_write.
    Defense-in-depth: the same YAML-corruption silent-data-loss path
    exists on the memory tier (scopes land in frontmatter the same way),
    so an unbounded scope list would corrupt the memory file and erase
    the record from search / list / show despite a committed-looking
    write. Mirrors the discipline `_validate_content_size` set for
    byte caps — every list-shaped frontmatter field gets a count cap."""
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
    scopes, corrupting the frontmatter and erasing the record from every
    read surface. Same class as the body cap on update (which closes the
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
    overflow the 64 KB frontmatter ceiling and silently drop the record. The
    memory_verify handler must enforce both a count cap and a per-item length
    cap itself, mirroring how `scopes` is guarded at the write handler. The
    memory must survive the rejections and still verify with a sane list.
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

    # A single pathological over-length entry (would bloat frontmatter).
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
    from bettermemory.events import iter_events

    ep_events = [e for e in iter_events(memory_dir) if e["kind"] == "episode_write"]
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


async def test_episode_handoff_walks_past_session_with_only_suppressed_scopes(
    memory_dir: Path,
) -> None:
    """When `disabled_scopes` hides every episode of the most-recent
    prior session, the auto-resolve walk treats that session as
    'wrote nothing' and adopts the next-older session instead. Mirrors
    the user mental model of `memory_scope_disable`: 'rewind past the
    last X-session and surface what came before'."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Older session: tools episode (visible).
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_a,
        "episode_write",
        body="A's tools work",
        takeaway="A on tools",
        scopes=["tools"],
    )

    # Most-recent session before the reader: all episodes in
    # projects:alpha (about to be suppressed).
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_b,
        "episode_write",
        body="B's alpha work A",
        takeaway="B on alpha A",
        scopes=["projects:alpha"],
    )
    await _call(
        server_b,
        "episode_write",
        body="B's alpha work B",
        takeaway="B on alpha B",
        scopes=["projects:alpha"],
    )

    # Reader session disables projects:alpha. The auto-resolve walk
    # should hop over server_b and adopt server_a's session.
    server_c = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_c, "memory_scope_disable", scope="projects:alpha")

    res = await _call(server_c, "episode_handoff")
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
        "episode_write",
        body="alpha-tagged",
        takeaway="alpha takeaway",
        scopes=["projects:alpha"],
    )
    await _call(
        server_a,
        "episode_write",
        body="tools-tagged",
        takeaway="tools takeaway",
        scopes=["tools"],
    )

    # Resolve A's session id from disk so we can pass it explicitly.
    from bettermemory.episodes import EpisodeStore

    ep_store = EpisodeStore(memory_dir)
    a_session_id: str
    for sid in ep_store.iter_session_ids():
        eps = ep_store.list_by_session(sid)
        if any("alpha-tagged" in e.body for e in eps):
            a_session_id = sid
            break
    else:
        raise AssertionError("could not locate session A's id")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_b, "memory_scope_disable", scope="projects:alpha")

    res = await _call(
        server_b,
        "episode_handoff",
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
        "episode_write",
        body="alpha",
        takeaway="alpha take",
        scopes=["projects:alpha"],
    )
    await _call(
        server_a,
        "episode_write",
        body="tools",
        takeaway="tools take",
        scopes=["tools"],
    )

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode_handoff")
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["alpha take", "tools take"]


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


async def test_episode_search_swarm_fan_in_gathers_all_subagents(
    memory_dir: Path,
) -> None:
    """Multi-agent swarm fan-in: parallel sub-agents (distinct sessions)
    each stamp their episodes with the coordinator's id; the coordinator
    then gathers EVERY sub-agent's takeaway in one episode_search call —
    the N:1 cohort read that single-chain episode_handoff can't express."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    coord = "sess_coordinator1"

    # Two sub-agents, each a distinct session sharing the one store.
    agent1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        agent1, "episode_write", body="a1", takeaway="from agent1", swarm_id=coord
    )
    agent2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        agent2, "episode_write", body="a2", takeaway="from agent2", swarm_id=coord
    )

    # A session NOT in the swarm — must be excluded from the fan-in.
    outsider = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(outsider, "episode_write", body="unrelated", takeaway="not in swarm")

    # The coordinator gathers its cohort.
    coordinator = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = _unwrap(await _call(coordinator, "episode_search", swarm_id=coord))
    assert {e["takeaway"] for e in res} == {"from agent1", "from agent2"}
    assert all(e["swarm_id"] == coord for e in res)


async def test_episode_search_swarm_id_composes_with_parent_session(
    memory_dir: Path,
) -> None:
    """`swarm_id` + `parent_session_id` narrows a fan-in to one
    sub-agent's session within the cohort."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    coord = "sess_coordinator1"

    agent1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    a1 = await _call(
        agent1, "episode_write", body="a1", takeaway="from agent1", swarm_id=coord
    )
    agent2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        agent2, "episode_write", body="a2", takeaway="from agent2", swarm_id=coord
    )

    res = _unwrap(
        await _call(
            agent1,
            "episode_search",
            swarm_id=coord,
            parent_session_id=a1["session_id"],
        )
    )
    assert [e["takeaway"] for e in res] == ["from agent1"]


async def test_episode_write_returns_swarm_id(server: Any) -> None:
    """episode_write echoes swarm_id in its committed payload; a
    non-swarm write returns None."""
    res = await _call(server, "episode_write", body="x", swarm_id="sess_coord")
    assert res["swarm_id"] == "sess_coord"
    res2 = await _call(server, "episode_write", body="y")
    assert res2["swarm_id"] is None


async def test_episode_search_max_results_caps_output(
    memory_dir: Path,
) -> None:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    for i in range(25):
        await _call(server, "episode_write", body=f"entry {i}")
    res = _unwrap(await _call(server, "episode_search", max_results=5))
    assert len(res) == 5


async def test_episode_search_max_results_returns_most_recent(
    memory_dir: Path,
) -> None:
    """When more episodes match than `max_results`, the cap surfaces the
    MOST-RECENT N (sorted oldest-first within that window) — same pattern
    as `episode_handoff`'s `all_eps[-max_episodes:]`. Pin the slice
    direction so the contract can't silently drift back to "oldest N",
    which is the opposite of caller intuition for ad-hoc journal lookup.
    """
    import time as _time

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # ULIDs encode ms-resolution timestamps and `created` is `utcnow()`;
    # sleep ≥1ms between writes so the sort key (`e["created"]` ISO
    # string) yields stable, distinct ordering across all 5 episodes.
    for i in range(1, 6):
        await _call(server, "episode_write", body=f"body {i}", takeaway=f"T{i}")
        _time.sleep(0.01)

    res = _unwrap(await _call(server, "episode_search", max_results=3))
    assert [e["takeaway"] for e in res] == ["T3", "T4", "T5"]


async def test_episode_search_respects_disabled_scopes(server: Any) -> None:
    """Episodes are part of the read surface — `memory_scope_disable`
    has to hide them too, mirroring `memory_search` / `memory_list`.
    An episode whose scope set intersects the disabled set is dropped.
    Mixing scopes on one episode is enough to suppress it; an episode
    without any intersection passes through."""
    await _call(
        server,
        "episode_write",
        body="alpha-suppressed note",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "episode_write",
        body="multi-scope note",
        scopes=["projects:alpha", "tools"],
    )
    await _call(
        server,
        "episode_write",
        body="tools-only note",
        scopes=["tools"],
    )

    await _call(server, "memory_scope_disable", scope="projects:alpha")

    res = _unwrap(await _call(server, "episode_search"))
    bodies = sorted(e["body"] for e in res)
    # Both `projects:alpha`-tagged episodes hidden, even the mixed one
    # (intersection rule). The pure-tools episode survives.
    assert bodies == ["tools-only note"]


async def test_episode_search_no_filter_when_disabled_scopes_empty(
    server: Any,
) -> None:
    """Regression pin: without any disabled scopes (default state),
    every episode comes through. The new filter must be a no-op."""
    await _call(
        server,
        "episode_write",
        body="alpha note",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "episode_write",
        body="tools note",
        scopes=["tools"],
    )
    res = _unwrap(await _call(server, "episode_search"))
    assert len(res) == 2


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


async def test_loop_iteration_end_to_end_pattern(
    memory_dir: Path, monkeypatch: Any
) -> None:
    """End-to-end exercise of the loop-iteration pattern documented in
    SKILL.md and the server-level instructions block.

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
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Pin both sessions to the SAME named worktree. Without this the test
    # captures the ambient cwd: inside a git checkout both servers get
    # the real (equal) toplevel, but from a non-git dir both get a None
    # worktree and the step-5 zero-episode adoption succeeds via
    # None==None — which was ALSO true pre-#28, making the assertion
    # vacuous. Pinning a named worktree forces the post-#28 worktree-match
    # path (named == named), which a pre-#28 build would reject.
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
        "episode_write",
        body="iter 1 — tuned GC, gophers cleared",
        takeaway="GC tuning fixed gopher frame drops",
    )

    # Iteration B: new session, picks up the handoff.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    handoff = await _call(server_b, "episode_handoff")
    assert handoff["prior_session_id"] is not None
    a_session_id = handoff["prior_session_id"]
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

    # Second handoff: A's episode was promoted (and the source file
    # deleted) so A is now a zero-episode candidate. With queue #28
    # landed, A's events carry worktree_root; A and B share the same
    # pinned NAMED worktree here, so the zero-episode branch's worktree
    # match succeeds (named == named, the post-#28 path) and A's session
    # is still adopted as the prior session — just with an empty episode
    # list (its only episode was promoted out). This is the contract the
    # docstring describes: prior_session_id stays resolved, episodes go
    # empty.
    handoff_2 = await _call(server_b, "episode_handoff")
    assert handoff_2["prior_session_id"] == a_session_id
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


async def test_episode_promote_user_inference_returns_pending_keeps_episode(
    server: Any,
) -> None:
    """`category='user-inference'` routes through PendingGate — the
    handler returns `status='pending'` and the source episode is kept
    so memory_write_confirm can act on it later."""
    ep = await _call(
        server,
        "episode_write",
        body="Iter 2 — observed the user reaching for terse summaries.",
        takeaway="user prefers terse summaries",
    )
    res = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "pending"
    assert res["pending_reason"] == "user-inference"
    assert res["pending_id"].startswith("pending_")
    assert res["promoted_from_episode_id"] == ep["id"]
    # Source episode is still on disk — confirm/cancel hasn't happened.
    listed = _unwrap(await _call(server, "episode_search"))
    assert any(e["id"] == ep["id"] for e in listed)


async def test_episode_promote_user_inference_confirm_deletes_source(
    server: Any,
) -> None:
    """When the user confirms a promoted user-inference write, the
    durable memory commits AND the source episode is deleted. This
    pins the SessionState-stash hand-off between `episode_promote`
    and `memory_write_confirm` — without it, the pending round-trip
    leaks the journal entry past confirmation as a duplicate."""
    ep = await _call(
        server,
        "episode_write",
        body="Iter 3 — user reiterated terse-summary preference.",
        takeaway="user prefers terse summaries over verbose walkthroughs",
    )
    pending = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert pending["status"] == "pending"
    # User confirms.
    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert committed["status"] == "committed"
    # Source episode is gone — it was distilled into the durable memory.
    listed = _unwrap(await _call(server, "episode_search"))
    assert not any(e["id"] == ep["id"] for e in listed)


async def test_episode_promote_user_inference_cancel_keeps_source(
    server: Any,
) -> None:
    """When the user declines a promoted user-inference write, the
    pending is dropped but the source episode survives so the caller
    can rephrase and re-promote. The promotion link should be cleared
    so a redundant later cancel doesn't try to act on it."""
    ep = await _call(
        server,
        "episode_write",
        body="Iter 4 — possibly the user prefers terseness, ambiguous.",
        takeaway="user prefers terse summaries",
    )
    pending = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    cancel = await _call(
        server, "memory_write_cancel", pending_id=pending["pending_id"]
    )
    assert cancel["existed"] is True
    # Source episode is STILL on disk — user can adjust and retry.
    listed = _unwrap(await _call(server, "episode_search"))
    assert any(e["id"] == ep["id"] for e in listed)
    # Nothing landed in the durable store.
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert listing == []


def test_delete_source_episode_holds_flock(memory_dir: Path) -> None:
    """`_delete_source_episode` must respect the same per-session flock
    `EpisodeStore.write` and `prune_old_sessions` take. The contract
    (audit-2 finding A2-03 / tick-18): this is the third write-side
    path that touches a session_dir's contents, so it has to serialise
    on the t13/t17 anchor. The concrete race the lock prevents — a
    peer `shutil.rmtree(session_dir)` interleaving with this unlink —
    is absorbed today by the `FileNotFoundError` catch, but the
    contract drift would trip future refactors (fsync_dir on the delete
    path, audit telemetry, deletion-primitive migration).

    Mirror-shape of `test_prune_empty_dir_holds_flock_while_writer_runs`
    but reversed: a peer (here: a prune holder) holds the per-session
    flock; the concurrent `_delete_source_episode` must BLOCK on the
    flock-acquire (not race past it). When the holder releases, the
    delete completes.
    """
    import threading
    import time as _time
    from types import SimpleNamespace

    from bettermemory._fsutil import flock_excl
    from bettermemory.episodes import EpisodeStore
    from bettermemory.handlers.episode_promote import _delete_source_episode

    # Seed an episode whose file we want to unlink.
    ep_store = EpisodeStore(memory_dir)
    episode = ep_store.write(session_id="sess_delflock", body="body", takeaway="t")
    ep_path = ep_store.episodes_dir / "sess_delflock" / f"{episode.id}.md"
    assert ep_path.exists()

    # `_delete_source_episode` only touches `deps.episode_store`. A
    # SimpleNamespace stand-in lets the test focus on the lock contract
    # without spinning up the full ToolHandlers graph.
    deps = SimpleNamespace(episode_store=ep_store)

    lock_anchor = ep_store.episodes_dir / ".session-sess_delflock"
    holder_holding = threading.Event()
    holder_release = threading.Event()

    def hold_session_lock() -> None:
        with flock_excl(lock_anchor):
            holder_holding.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_session_lock)
    holder.start()
    try:
        holder_holding.wait(timeout=5.0)
        assert holder_holding.is_set()

        delete_done = threading.Event()

        def background_delete() -> None:
            # The helper only touches `deps.episode_store` — see the
            # SimpleNamespace stand-in built above. mypy can't see that
            # the structural subset is satisfied, so we narrow with a
            # `# type: ignore` rather than wiring the full ToolHandlers.
            _delete_source_episode(deps, "sess_delflock", episode.id)  # type: ignore[arg-type]
            delete_done.set()

        dt = threading.Thread(target=background_delete)
        dt.start()
        # Give the delete a generous window to (incorrectly) race past
        # the flock. If it completes here, `_delete_source_episode`
        # isn't serialising — that's the bug we're protecting against.
        _time.sleep(0.1)
        assert not delete_done.is_set(), (
            "_delete_source_episode raced through the per-session flock "
            "— the delete window is not serialised against peer prune"
        )
        assert ep_path.exists(), "delete completed before holder released"

        holder_release.set()
        dt.join(timeout=5.0)
        assert delete_done.is_set()
        # After the holder released, the delete acquires the lock and
        # unlinks the episode file. The 0-byte lockfile persists by
        # design — same rationale as t13/t17 (flock identity is
        # per-inode; unlinking would open a race window).
        assert not ep_path.exists()
        lock_path = ep_store.episodes_dir / ".session-sess_delflock.lock"
        assert lock_path.exists(), (
            "lockfile must persist so future acquirers share the inode"
        )
    finally:
        holder_release.set()
        holder.join(timeout=5.0)


async def test_delete_source_episode_filenotfound_still_succeeds(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `FileNotFoundError` catch in `_delete_source_episode` is
    preserved on purpose: a peer prune (or a duplicate confirm) that
    deletes the source episode first is a valid completion state. With
    the flock now wrapping the unlink, this pin makes sure the catch
    still fires inside the locked section so the confirm flow surfaces
    a normal committed status even when the file is already gone.

    Stage a user-inference promote (forces the `_delete_source_episode`
    path to fire from `memory_write_confirm`), monkeypatch
    `pathlib.Path.unlink` to raise `FileNotFoundError` ONLY on the
    episode file (so the durable store's writes aren't affected), and
    assert the confirm call returns the standard committed envelope."""
    import pathlib

    ep = await _call(
        server,
        "episode_write",
        body="Iter — user preference signal.",
        takeaway="user prefers terse summaries",
    )
    pending = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert pending["status"] == "pending"

    # Patch Path.unlink so the unlink targeting the episode file
    # specifically raises FileNotFoundError. We must NOT affect store
    # tempfile cleanup or other unlinks the confirm path performs.
    real_unlink = pathlib.Path.unlink
    target_name = f"{ep['id']}.md"

    def selective_unlink(self: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if self.name == target_name:
            raise FileNotFoundError(self)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", selective_unlink)

    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    # The FileNotFoundError absorption inside the locked section must
    # keep the confirm surface clean — the caller sees a normal
    # committed write, not an exception or status-degradation.
    assert committed["status"] == "committed"


async def test_delete_source_episode_fsyncs_session_dir(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit-3 A3-06: after `ep_path.unlink()` inside the per-session
    flock, `_delete_source_episode` must call `fsync_dir(session_dir)`
    so the dropped dirent survives a crash. Without this, a power
    cut between `memory_write_confirm` returning "committed" and the
    kernel flushing dirty pages can resurrect the episode file on
    reboot — the durable memory exists, the journal entry comes back
    as a duplicate that lives until the 30-day TTL or the next prune.
    Symmetric to the `fsync_dir(episodes_dir)` ceremony on the prune
    branches.

    Spy on the `fsync_dir` binding the `episode_promote` module
    imported. Run the full promote → confirm round-trip (user-inference
    forces the deferred delete path via `memory_write_confirm`). After
    confirm, assert the spy recorded a call against the session_dir.

    Note: `import bettermemory.handlers.episode_promote as m` would
    resolve to the FUNCTION `episode_promote` (re-exported from the
    handlers package `__init__.py` and bound as an attribute on the
    `handlers` package, shadowing the submodule on attribute lookup).
    Use `importlib.import_module` to get the actual module object so
    `monkeypatch.setattr(module, "fsync_dir", ...)` finds the rebound
    name.
    """
    import importlib

    promote_mod = importlib.import_module("bettermemory.handlers.episode_promote")

    ep = await _call(
        server,
        "episode_write",
        body="Iter — promotion fsync_dir signal.",
        takeaway="user prefers terse summaries over verbose walkthroughs",
    )
    pending = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert pending["status"] == "pending"

    fsync_dir_calls: list[Path] = []

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(promote_mod, "fsync_dir", spy_fsync_dir)

    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert committed["status"] == "committed"

    # The deferred delete path inside the locked section must fsync
    # the session_dir after unlink. Identify session_dir by suffix
    # match on the episode_session_id; the spy captured the exact
    # Path object passed in.
    assert any(p.name.startswith("sess_") for p in fsync_dir_calls), (
        f"_delete_source_episode must fsync_dir(session_dir) after the "
        f"unlink to persist the dropped dirent; saw: {fsync_dir_calls}"
    )


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
    res = await _call(server_b, "episode_handoff")
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
    res = await _call(server_b, "episode_handoff")
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
    res = await _call(server_b, "episode_handoff")
    assert res["prior_session_id"] is not None
    assert res["prior_session_id"].startswith("sess_")
    # Zero episodes on disk means the "middle state" — session_id
    # surfaced, episodes empty.
    assert res["episodes"] == []


# ---------------------------------------------------------------------------
# E2 — session-tag floor episodes at episode_handoff entry
# ---------------------------------------------------------------------------


async def test_handoff_writes_floor_for_current_session(
    memory_dir: Path,
) -> None:
    """E2 regression: a fresh `episode_handoff` call writes a session-tag
    floor episode for the CURRENT session on disk. The floor anchors
    the worktree on disk so a tick that crashes before `episode_write`
    is still discoverable by the next tick's handoff via the worktree
    filter."""
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode_handoff")

    ep_store = EpisodeStore(memory_dir)
    # The current session's id is exposed via the recorder; resolve it
    # by walking session dirs. Exactly one floor should exist.
    session_ids = list(ep_store.iter_session_ids())
    assert len(session_ids) == 1, (
        f"expected exactly one session dir from a fresh handoff, got {session_ids}"
    )
    sid = session_ids[0]
    eps = ep_store.list_by_session(sid)
    assert len(eps) == 1, f"expected one floor episode, got {len(eps)}"
    assert eps[0].is_floor is True
    assert eps[0].takeaway is None
    assert eps[0].scopes == []


async def test_handoff_floor_write_is_idempotent_in_same_process(
    memory_dir: Path,
) -> None:
    """E2 idempotency: calling `episode_handoff` twice in the same
    process (same session_id) must NOT produce two floors. The second
    call sees the floor on disk and skips the write."""
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode_handoff")
    await _call(server, "episode_handoff")

    ep_store = EpisodeStore(memory_dir)
    session_ids = list(ep_store.iter_session_ids())
    assert len(session_ids) == 1
    eps = ep_store.list_by_session(session_ids[0])
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
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    # Write a real takeaway FIRST (unusual ordering — the protocol
    # documents handoff-first, but it's valid to write a takeaway
    # before issuing the handoff in a custom caller).
    await _call(server, "episode_write", body="real body", takeaway="real takeaway")
    await _call(server, "episode_handoff")

    ep_store = EpisodeStore(memory_dir)
    session_ids = list(ep_store.iter_session_ids())
    assert len(session_ids) == 1
    eps = ep_store.list_by_session(session_ids[0])
    # Just the real episode, no floor.
    assert len(eps) == 1
    assert eps[0].is_floor is False
    assert eps[0].takeaway == "real takeaway"


async def test_handoff_followed_by_episode_write_yields_floor_plus_real(
    memory_dir: Path,
) -> None:
    """E2 main flow: handoff writes a floor at entry; episode_write
    later appends a real takeaway. `list_by_session` returns both;
    the next tick's handoff filters the floor from its takeaway
    summary AND uses the floor's worktree to match."""
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: handoff (writes floor) → episode_write (real takeaway).
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode_handoff")
    await _call(
        server_t,
        "episode_write",
        body="real iteration body",
        takeaway="real iteration takeaway",
    )

    ep_store = EpisodeStore(memory_dir)
    session_ids = list(ep_store.iter_session_ids())
    assert len(session_ids) == 1
    eps = ep_store.list_by_session(session_ids[0])
    # Floor + real — two episodes.
    assert len(eps) == 2
    assert eps[0].is_floor is True  # Floor written first
    assert eps[1].is_floor is False  # Real takeaway written second
    assert eps[1].takeaway == "real iteration takeaway"

    # Tick T+1: a fresh server in the same process. Handoff should
    # surface T's real takeaway and NOT the floor.
    server_t1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_t1, "episode_handoff")
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
    await _call(server_t, "episode_handoff")
    # Simulated crash: tick T ends here, never calls episode_write.

    # Tick T+1: a fresh server in the same worktree resolves the prior
    # session. T is floor-only; with no older real session to rewind to,
    # T itself is surfaced as the prior id.
    server_t_plus_1 = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = await _call(server_t_plus_1, "episode_handoff")

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
    res = await _call(server, "episode_handoff")
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
    await _call(server_a, "episode_handoff")
    # No episode_write — simulated crash before takeaway.

    # T+1 in worktree B: handoff should NOT see A's floor — A's
    # worktree is /repo-feature-x, B's is /repo-bug-fix.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res_b = await _call(server_b, "episode_handoff")
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
    res_a = await _call(server_a_peer, "episode_handoff")
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
    `kind='episode_handoff'`; verify the floor exists on disk
    afterwards (proving it landed BEFORE the record call)."""
    from bettermemory.episodes import EpisodeStore
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
        await _call(server, "episode_handoff")

    # CRITICAL: the floor exists on disk. If it doesn't, the ordering
    # invariant is violated and the fix doesn't help under the most
    # important crash window.
    ep_store = EpisodeStore(memory_dir)
    session_ids = list(ep_store.iter_session_ids())
    assert len(session_ids) == 1, (
        "ordering invariant broken: floor must be on disk even when "
        "the handoff event-record stage raises"
    )
    eps = ep_store.list_by_session(session_ids[0])
    assert len(eps) == 1
    assert eps[0].is_floor is True


async def test_episode_search_filters_out_floor_episodes(
    memory_dir: Path,
) -> None:
    """E2 consumer pin: `episode_search` (the journal-summary read
    surface) filters out floor episodes. A floor's body is a
    placeholder marker, not journal content; surfacing it in
    "what did I conclude across the last few sessions?" would be
    indistinguishable from a takeaway from the model's perspective."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: handoff writes a floor; episode_write writes a real.
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode_handoff")
    await _call(
        server_t,
        "episode_write",
        body="real body",
        takeaway="real takeaway",
    )

    # episode_search returns ONLY the real takeaway — floor filtered.
    server_t1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = _unwrap(await _call(server_t1, "episode_search"))
    takeaways = [e["takeaway"] for e in res]
    assert takeaways == ["real takeaway"]


async def test_episode_promote_rejects_floor_episode(
    memory_dir: Path,
) -> None:
    """E2 consumer pin: `episode_promote` rejects a floor with a
    clear error message. Floors aren't content — promoting one
    would either crash through the durability gate (transient
    marker) or land a junk memory. Explicit rejection at the
    promotion boundary surfaces the right "this isn't a takeaway"
    error rather than blaming the caller for a "transient phrase"
    in the body."""
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "episode_handoff")

    ep_store = EpisodeStore(memory_dir)
    sid = next(iter(ep_store.iter_session_ids()))
    floor = ep_store.list_by_session(sid)[0]
    assert floor.is_floor is True

    # Attempting to promote a floor raises with a floor-specific message.
    # The SDK wraps the underlying ValueError in a ToolError — match on
    # the message which carries through either way.
    with pytest.raises(Exception, match="floor"):
        await _call(
            server,
            "episode_promote",
            episode_id=floor.id,
            scopes=["tools"],
            use_body=True,  # bypasses the takeaway-None guard
        )


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
            "unique_silent_miss_memories": 0,
            "cold_endorsement_memories": 0,
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


async def test_update_checks_allowed_scopes_against_the_delta_only(
    memory_dir: Path,
) -> None:
    """`[scopes] allowed` governs what an update INTRODUCES, not what it keeps.

    `scopes` has REPLACE semantics, so keeping a scope means resubmitting
    it. Enforcing the allowlist over the whole submitted list therefore
    froze every row carrying a scope the tool stamped itself: ingest exempts
    its provenance scope and type tag from the same allowlist
    (`_scope_allowlist_reason`, ingest.py) because the user never typed
    them, and the update that resubmitted them was refused by name — the
    only way to re-tag an imported row was to drop the provenance stamp.

    Third assertion is the one that keeps this from being a blanket
    exemption: a stamp-looking scope that is NOT already on the record is
    still refused, so no caller can borrow ingest's carve-out to plant a
    false provenance tag on a hand-written memory.
    """
    from bettermemory.config import ScopesConfig
    from bettermemory.ingest import DEFAULT_PROVENANCE_SCOPE, _tool_stamped_scopes

    stamped = sorted(_tool_stamped_scopes("project"))
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
            scopes=["projects:demo", DEFAULT_PROVENANCE_SCOPE],
        )
    assert DEFAULT_PROVENANCE_SCOPE in str(excinfo.value)


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
    ``memory_update`` on a mis-filed parent; ``UserClaimGate`` also
    BEFORE pending, so that re-issue stages through ``PendingGate``
    normally; dedup BEFORE pending so the user-inference confirmation
    flow doesn't ask about a write we'd already reject;
    ``PendingGate`` last because everything else either rejects or
    accepts. A silent reorder breaks the security/correctness
    invariant the source comment documents.

    A future contributor reordering this tuple for performance must
    update both the source AND this expected tuple in the same
    commit, AND re-read the write.py:474-481 rationale to confirm
    the new ordering still preserves: hallucinated-before-dedup,
    transient-before-dedup, user-claim-before-dedup-and-pending,
    scope-before-dedup, dedup-before-pending, and pending-last."""
    from bettermemory.handlers.write import (
        CredentialGate,
        DedupActiveGate,
        DedupTombstoneGate,
        GroundednessGate,
        PendingGate,
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
    bench/rot/T3_NOTE_CAP_DECISION.md). Without this, a hostile
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
    from bettermemory.events import iter_events

    last = [e for e in iter_events(memory_dir) if e.get("kind") == "scope_overview"][-1]
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
    from bettermemory.events import iter_events

    scope_events = [e for e in iter_events(memory_dir) if e["kind"] == "scope_overview"]
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


async def test_scope_overview_recently_removed_filtered_by_worktree(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Two LIVE worktrees of one repo sharing a memory root must not see
    each other's tombstones counted into `recently_removed_in_worktree`
    under auto_scope=True. Worktree A writes and removes a memory; a
    fresh server in worktree B asks for a scope_overview and the
    recently-removed count for B is zero — A's tombstone is excluded
    because its `origin.worktree_root` is not B's.

    `memory_search`, `memory_scope_overview`'s active-counts, and
    `episode_handoff` all enforce worktree isolation through
    `should_include_for_caller`; `_count_recent_tombstones` routes
    through the same helper for the removal-activity surface. Without
    that, a sibling worktree's curation pass would look like rot
    belonging to the current worktree and the model would be nudged to
    avoid ground it never actually covered.

    Both worktree roots must EXIST on disk, which is why they are
    `tmp_path` directories rather than string literals: a recorded root
    that is gone triggers `worktrees_match`'s deliberate dead-worktree
    degrade, and the degrade's whole point is that a workspace nobody
    can be sitting in has nothing to be isolated from. The
    survives-a-move twin of that leg is
    `tests/test_server_origin.py`'s
    `test_tombstone_count_survives_a_checkout_move`.

    The companion `auto_scope=False` call confirms the tombstone still
    exists on disk (the worktree filter is the only thing hiding it
    from B), and the same-worktree positive control confirms a matching
    origin counts under auto_scope=True.
    """
    import bettermemory._handlers as handlers_module
    import bettermemory.server as server_module
    from bettermemory.origin import Origin

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    root_a = tmp_path / "worktrees" / "repo-feature-x"
    root_b = tmp_path / "worktrees" / "repo-bug-fix"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)

    origin_a = Origin(
        cwd=str(root_a),
        repo="git@github.com:example/repo.git",
        branch="feature-x",
        worktree_root=str(root_a),
    )
    origin_b = Origin(
        cwd=str(root_b),
        repo="git@github.com:example/repo.git",
        branch="bug-fix",
        worktree_root=str(root_b),
    )

    # Mirror the patch idiom from
    # `test_episode_handoff_filters_prior_session_by_caller_worktree`:
    # both the `_handlers` and `server` bindings get re-pointed so
    # every callsite resolves to the same fake. `monkeypatch` restores
    # both at teardown.
    def make_capture(origin: Origin) -> Any:
        def _capture(cwd: Any = None) -> Origin:
            return origin

        return _capture

    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))

    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    written = await _call(
        server_a,
        "memory_write",
        content="auth uses bcrypt",
        scopes=["projects:auth"],
    )
    await _call(server_a, "memory_remove", id=written["id"], reason="outdated")

    # Flip to worktree B. Same memory root, same tombstone on disk, but
    # the caller-origin now points at a sibling worktree. With the
    # filter in place server_b sees zero recent removals; the
    # auto_scope=False companion confirms the tombstone is still there.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_b))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_b))

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    overview_auto = await _call(server_b, "memory_scope_overview")
    assert overview_auto["recently_removed_in_worktree"] == 0

    overview_all = await _call(server_b, "memory_scope_overview", auto_scope=False)
    assert overview_all["recently_removed_in_worktree"] >= 1

    # Same-worktree positive control: a fresh server whose caller-origin
    # matches A's worktree_root must see the tombstone under
    # auto_scope=True. This pins the equality branch of the filter so
    # a regression that strips the worktree match entirely (counting
    # zero for every auto_scope=True call) would still be caught.
    monkeypatch.setattr(handlers_module, "capture_origin", make_capture(origin_a))
    monkeypatch.setattr(server_module, "capture_origin", make_capture(origin_a))

    server_a2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    overview_same = await _call(server_a2, "memory_scope_overview")
    assert overview_same["recently_removed_in_worktree"] >= 1


async def test_cold_endorsement_ratio_threshold_threaded_to_all_callsites(
    memory_dir: Path,
) -> None:
    """Regression: `BehaviorConfig.cold_endorsement_ratio_threshold`
    must drive every `curation_counts` callsite, not just
    `memory_health`. Earlier, only the deep `memory_health` surface
    read the knob — the `memory_scope_overview` rollup and the
    per-write `curation_hint` nudge fell back to the strict 0.0
    default, so a user who configured
    `cold_endorsement_ratio_threshold=0.5` saw the loosened bucket on
    `memory_health` but the strict bucket on every session-start hint.

    We seed a memory with 5 retrievals and 4 applieds where exactly 1
    is explicit (ratio 1/4 = 0.25). At the configured 0.5 threshold the
    memory IS a cold-endorsement memory (count = 1); at the strict 0.0
    default it is NOT (count = 0). Both `memory_scope_overview` and the
    `curation_hint` block must surface the 0.5 reading.
    """
    from bettermemory.config import BehaviorConfig

    # Build the server with the loosened threshold and a low
    # curation-hint threshold so a single cold-endorsement entry trips
    # the inline nudge on `memory_write`.
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(
            cold_endorsement_ratio_threshold=0.5,
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
    # tagged ambient (ambient memories never land in
    # cold_endorsement_memories).
    written = await _call(
        server_x,
        "memory_write",
        content="postgres listens on 5432 in prod",
        scopes=["infrastructure"],
    )
    mem_id = written["id"]

    # Seed events directly into the active log so the retrieval /
    # applied counts cross the cold-endorsement floor (5 retrievals).
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
    assert overview["curation_pending"]["cold_endorsement_memories"] == 1, (
        "scope_overview must apply the configured threshold (0.5); the "
        "seeded memory has ratio 1/4 = 0.25 < 0.5 and should land in the "
        "cold_endorsement_memories bucket."
    )

    # --- Surface 2: curation_hint on memory_write ---
    # Fresh SessionState so the one-shot `curation_hint_checked` flag
    # is False, and a real event store so the second callsite walks
    # the same seeded log. With curation_hint_threshold=1 and at least
    # one cold-endorsement entry, the hint must attach.
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
    assert hint_res["curation_hint"]["counts"]["cold_endorsement_memories"] == 1


async def test_cold_endorsement_ratio_threshold_default_still_strict(
    memory_dir: Path,
) -> None:
    """Back-compat: with the default 0.0 threshold, the same seeded
    state (one explicit applied present) must NOT count as
    cold_endorsement_memories on any surface. This locks the default
    behaviour against accidental loosening when threading the knob
    through."""
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
    assert overview["curation_pending"]["cold_endorsement_memories"] == 0


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
    string the SDK advertises over the wire."""
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
# Tool-description surface budget — the counter-valve to the instructions block
# ---------------------------------------------------------------------------
#
# `test_instructions_block_fits_under_truncation_budget` (above) caps the
# server `instructions` block at ~1.7KB and, by its own docstring, directs
# authors to "push the detail down into individual tool descriptions, which
# are NOT subject to the same truncation." That made tool descriptions the
# sanctioned overflow sink — with no counter-guard. The lean default-on
# surface had grown to ~27.9KB of descriptions resident on EVERY turn (~16x
# the 1.7KB policy home), with the opt-in / announce-on-search / proactive-
# write policy restated verbatim in the descriptions on top of the
# instructions block AND `SYSTEM_PROMPT_ADDENDUM`. For a project whose entire
# purpose is minimising per-turn context, that valve was leaking. These guards
# close it: a sum ceiling so the surface cannot silently re-bloat, and a
# de-duplication invariant so policy lives once (the instructions block) with
# at most ONE inline point-of-call cue per behaviour in a description.


async def _lean_descriptions(tmp_path: Path) -> dict[str, str]:
    """The SHIPPING-DEFAULT (lean, full_tool_surface=False) tool descriptions,
    keyed by tool name. The lean surface is what a typical client pays in
    context every turn, so it — not the 27-tool full surface
    (`test_tool_surface.py`'s `_FULL_COUNT` pins that count; this phrasing
    is unguarded prose) — is what these budget guards police."""
    from bettermemory.builder import build_server
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        ProposalsConfig,
        StorageConfig,
    )
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(full_tool_surface=False),
        proposals=ProposalsConfig(),
    )
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    return {t.name: (t.description or "") for t in await mcp.list_tools()}


# The budget, as named constants: the failure text, the pressure warning and
# the recorded measurement all read the same numbers, so none can drift from
# what the assert enforces.
_DESC_BUDGET_CEILING = 26_000
# Soft line. Crossing it warns instead of failing, so the pressure is visible
# to whoever caused it rather than only to whoever trips the ratchet later.
_DESC_BUDGET_PRESSURE = _DESC_BUDGET_CEILING - 100
# Per-tool `_lean_descriptions` lengths, re-measured live in the commit that
# last moved one. DIAGNOSTIC ONLY: nothing asserts these, so an entry that goes
# stale degrades the failure message and never the verdict, and a tool absent
# from here is reported as new rather than raising. The recorded total is a
# `sum()` over this table rather than a second literal, so the two cannot
# disagree. Re-measure in the same commit as any description edit — deferring
# it to the next ceiling recalibration is what let all four moved rows rot,
# and a stale row is exactly the map from "the total is over" to "the thing
# you just typed" that `_desc_budget_breakdown` exists to draw.
#
# Re-measured 2026-07-30 (table total 26,238 -> 26,334; the live total moved
# 26,336 -> 26,334 here). All FOUR rows below were stale, and only 2 chars of
# the 96-char correction is this commit's own edit: memory_search 3467 -> 3387
# is -78 of drift un-recorded since the table was measured at d1585d3 plus the
# -2 edit here, retiring the unbacked 10%->65% recall pair from the `query`
# cue. The other three moved without any edit in this commit at all:
# memory_verify +192, across two commits — +161 in 994ed48
# (verified_commits never fed the drift legs) and +31 in a59f640 (refusing path
# attestations the attesting machine cannot stat); memory_scope_overview +8;
# memory_write -24. Two commits' worth of drift behind one row is the argument
# for the rule above: attributing it after the fact took a bisect.
#
# Re-measured again in the write-path hole-closure commit (table total
# 26,334 -> 26,860, live total identical). Exactly the two rows this commit
# edits moved, which is what the rule above buys: memory_write +217 for the
# `user_claim_warning` status and its `acknowledge_user_claim` escape,
# memory_write_confirm +309 for the statuses confirm-time re-gating can now
# return and the `pending_retained` contract that says the staged write
# survives one. Both are new refusal shapes a caller cannot discover from a
# reject it has not hit yet, which is the reference the budget's slack is
# for; the gate hints still carry the remedies. The trust recut then moved
# memory_search +188, describing what the relevance label actually measures
# and what `matched_leg` reports, in place of the "treat low as noise"
# absolutism that was suppressing the semantic leg's only capability. That
# leaves 452 chars under `_DESC_BUDGET_CEILING` — the ceiling is deliberately
# NOT moved here, since the later footprint work ratchets it DOWN and more
# description edits land before then.
#
# Re-measured for the footprint phase's description cuts (E1) — the DOWN
# ratchet the note above was deferring to. Live total 27,398 -> 25,535, and
# exactly the four rows this commit edits moved:
#
#     memory_write       3,325 -> 2,729  (-596)
#     memory_audit_turn  1,365 ->   822  (-543)
#     memory_update      2,234 -> 1,782  (-452)
#     memory_verify      1,817 -> 1,545  (-272)
#
# Three different arguments, and they are not interchangeable. memory_write,
# memory_update and memory_verify shed prose that a gate's reject `hint`
# already teaches VERBATIM at the only moment it is actionable — the
# transient / credential / previously_removed / scope_mismatch remedies and
# the two `status="stale"` rebase paragraphs. What survives is the part a
# hint cannot carry, because a caller who never trips the gate still needs
# it: the status NAMES stay as a one-line index (this file's own
# `test_status_vocabulary_is_documented` in tests/test_server_user_claims.py
# records why — "a refusal status the model has never read about is a dead
# end"), the `duplicate` bullet keeps its corroboration semantics, which no
# hint states, and memory_verify's `verified_*` REPLACE rule MOVED onto the
# parameter bullets rather than being deleted with the concurrency paragraph
# it happened to sit in. That rule bites on a SUCCESSFUL verify, so no
# reject can ever teach it — deleting it would have been the one real
# subtraction in this commit.
#
# memory_audit_turn is the other argument: it is dispatched by the client's
# Stop hook and its own first sentence tells the model never to call it, so
# its caller reference was rent paid every turn by the one reader that
# cannot act on it. Banner, parameters, return shape, side-effects and the
# pinned retrieval-event set stay; the probe-construction and
# why-this-matters essay moved to docs/api.md, where the hook author who
# needs it already reads. Same trade `DESC_EPISODE_SEARCH` made.
#
# Re-measured 2026-08-02, and one row had rotted exactly the way the rule at
# the top of this comment warns: memory_update 1,892 -> 2,033 (+141), from two
# commits that edited `DESC_MEMORY_UPDATE` and left the table alone — ba6360e's
# `user_claim_warning` clause on the `content` bullet, then 0bf7a49's
# `acknowledge_user_claim` escape (part-funded by deleting a `scopes` bullet
# the leader paragraph already taught). Every OTHER row was re-measured in the
# same probe and none had moved, so the table sums to the live total again:
# 25,890 — 110 under `_DESC_BUDGET_CEILING`, 10 under the
# `_DESC_BUDGET_PRESSURE` warning. 0bf7a49 wrote that live total into its own
# commit message and still left the row behind it stale, which is the split
# the rule is about: the total is what fails the build, the row is the only
# map from that failure to the string you typed.
#
# Re-measured 2026-08-04 for the truncation write-gate, which had been deferred
# for two releases on this exact budget. Live total 25,890 -> 25,419, and one
# row moved: memory_update 2,033 -> 1,562 (-471). That is a -658 and a +187 in
# the same edit. The -658 collapsed `DESC_MEMORY_LINKS_TAIL` to a four-name type
# index: the mechanics it restated (REPLACE semantics — already verbatim on the
# `scopes` / `links` bullet six lines above it — self-link rejection, and how
# links surface at retrieval) now live only in docs/api.md's "Inter-memory
# links" section, which already carried every one of them. The four type
# GLOSSES stayed, because picking the right edge is the one thing a caller
# cannot infer from the schema. The +187 is the gate: one clause on the
# `content` bullet and the `acknowledge_truncation` escape.
#
# Two numbers worth writing down for whoever budgets the next field-pin. The
# precedent said this shape costs ~141 (ba6360e +120, 0bf7a49 +21) against 110
# of headroom, i.e. it overran the HARD ceiling, not merely the warning — the
# roadmap's "~112-150" low end was already too optimistic. And the schema half
# was never the constraint: `acknowledge_truncation` cost 60 characters against
# 371 of remainder headroom under `_REMAINDER_CEILING`. Description prose was
# the whole blocker, which is why reclamation and not a ceiling bump was the
# right unblock. 481 under `_DESC_BUDGET_PRESSURE` now.
_DESC_BASELINE = {
    "episode_handoff": 1560,
    # Re-measured 2026-07-31: 1597 -> 1700 (+103) for the state-channel
    # convention (Phase 7 / G2) — the routing rule (loop/working state
    # goes to episodes) and the minting moment (session close). Only
    # those two are resident; the rationale sits in docs/api.md and the
    # skill body, which cost nothing per turn. Deliberately NOT mirrored
    # into episode_write's DESC, so the policy is paid for once.
    "episode_promote": 1700,
    # Re-measured 2026-07-30: 3071 -> 2064 after the proportionality trim
    # (18 recorded calls across 544 sessions against ~3.2 KB billed every
    # turn). Rationale moved to docs/api.md; every pinned cue kept.
    # Re-measured 2026-07-31: 2064 -> 2311 (+247) for the takeaway-only
    # read. 239 of it is the two new parameter bullets; the other 8 add
    # `ids` to the WORKTREE SCOPING paragraph's enumeration of explicit
    # selectors, which is not optional prose — leaving that sentence
    # naming two of three would tell a model the exact opposite of what
    # the `ids` bullet says, in the paragraph a caller reads for the
    # isolation contract. Bought back on the read side: measured on a
    # 138-episode store, a 10-row page drops 50.2 KB -> 4.8 KB (the
    # fixture that asserts it is in tests/test_episode_search_scan_and_fetch.py).
    # NOT the plan's pre-measurement "~28 KB -> ~1 KB" estimate, which
    # this comment carried until the fixture existed to contradict it.
    "episode_search": 2311,
    "episode_write": 2350,
    # Re-measured 2026-07-31: 822 -> 798 (-24), the clause " through the MCP
    # channel" removed as false. The shipped Stop hook dispatches the CLI
    # (`plugin/hooks/hooks.json` runs `uvx bettermemory audit-turn --quiet`),
    # so the description was naming a transport the hook does not use. The
    # tool's registration is unchanged and deliberately so: no MCP dispatch
    # in one maintainer's event log is n=1, not evidence about other clients.
    "memory_audit_turn": 829,
    "memory_list": 454,
    "memory_record_use": 1556,
    "memory_remove": 463,
    "memory_scope_disable": 231,
    "memory_scope_enable": 55,
    "memory_scope_overview": 2820,
    "memory_search": 3575,
    "memory_show": 851,
    # Re-measured 2026-08-04: 2033 -> 1562 (-471). See the reclamation note
    # above — DESC_MEMORY_LINKS_TAIL collapsed to a type index (-658), the
    # truncation gate's clause and escape added back (+187).
    #
    # Re-measured 2026-08-04 again for claims-at-write: exactly the three
    # rows that commit edits moved. memory_write +245 and memory_verify
    # +161 are the `claims` parameter bullets — deliberately terse
    # (existence, the three-shape syntax, the false ⇒ refused contract)
    # because the declare-time gate's refusal teaches the full syntax
    # with per-defect messages at the only moment it is actionable, the
    # same hint-carries-the-remedy split the E1 cuts established.
    # memory_update +32 adds `claims` to the body-edit clearing clause —
    # that rule bites on a SUCCESSFUL edit, so no reject can teach it.
    # Total lands at 25,857: 43 under the pressure line, no reclamation
    # spent — the slack the 2026-08-04 links-tail collapse bought is
    # what absorbed the feature.
    "memory_update": 1594,
    "memory_verify": 1810,
    "memory_write": 2998,
    "memory_write_cancel": 216,
    "memory_write_confirm": 515,
}


def _desc_budget_breakdown(descs: dict[str, str]) -> str:
    """Which descriptions moved against `_DESC_BASELINE`, largest first.

    The reader who meets this budget is usually editing a DESC in some other
    file and has no map from "the total is over" to "the thing you just
    typed". One number cannot give them that map; this does."""
    rows: list[tuple[int, str]] = []
    for name, desc in descs.items():
        was = _DESC_BASELINE.get(name)
        now = len(desc)
        if was is None:
            rows.append((now, f"  {name}: {now} (not in the baseline)"))
        elif now != was:
            rows.append((now - was, f"  {name}: {was} -> {now} ({now - was:+d})"))
    for name, was in _DESC_BASELINE.items():
        if name not in descs:
            rows.append((-was, f"  {name}: {was} -> gone from the lean surface"))
    if not rows:
        return "  (every description matches the baseline)"
    rows.sort(key=lambda row: -row[0])
    return "\n".join(text for _, text in rows)


async def test_default_on_descriptions_fit_budget(tmp_path: Path) -> None:
    """Counter-valve to the instructions-block budget. The lean default-on
    tool descriptions are resident in context on every turn — including the
    90%+ of turns that never touch memory — so their total size is a per-turn
    tax the project exists to minimise.

    This is a RATCHET against SUSTAINED re-bloat: the surface grew by
    restating the opt-in / announce / proactive-write policy in description
    after description (the section comment above records that leak), and the
    ceiling locks in the collapse that closed it. Verbatim re-triplication
    has a sharper instrument —
    `test_policy_lives_once_not_triplicated_in_descriptions` below fails on
    the second copy of any phrase it tracks, at any size — so this guard is
    the aggregate backstop, not the precise one. When it trips, the first
    move is still to collapse
    duplicated POLICY prose into its canonical home, the `instructions`
    block. The slack exists for genuine field-discoverability reference
    (`tests/test_prompts.py`'s "pin each field in its DESC" philosophy), not
    for policy.

    Two rules on the ceiling itself. The earlier single rule — never raise it
    — was written while there was slack to spend, and once the slack ran out
    it started arbitrating edits that had nothing to do with policy:

    1. NEVER raise it to make a failing total pass by re-admitting collapsed
       policy. That is the regression this guard exists for, and no amount of
       rationale in the commit makes it a different move.
    2. A raise is legitimate only as a deliberate RECALIBRATION of the slack:
       the measured total does not move, `_DESC_BASELINE` is re-measured in
       the same commit, and the new ceiling is a round number rather than
       whatever the current total happens to need plus epsilon.
    3. LOWERING the ceiling is the same ceremony minus the first clause, and
       the clause has to be dropped explicitly: a ratchet-down exists BECAUSE
       the measured total moved, so "the measured total does not move" is
       rule 2 read in the only direction it was written for. What carries
       over is the rest — `_DESC_BASELINE` re-measured in the same commit,
       and a round number. What is added is the part a raise never needs: a
       lower ceiling is only earned if the prose that left is still taught
       somewhere a caller reaches, so the commit names where each cut span
       went. A ratchet-down that cannot answer that is a subtraction wearing
       a budget's clothes, and this guard would rubber-stamp it — the
       ceiling only ever measures size, never whether the surface still
       teaches what it must.

    The raise to `_DESC_BUDGET_CEILING` was the second kind, and the case for
    it is what the alternative was costing. The previous 27,250 was itself a
    3.8.0 raise to admit one field-pin; by the 3.28.0 release commit (8e12b99)
    the total had reached 27,248 — two characters of slack. At that width the
    guard stops being a budget and becomes a tripwire on unrelated work: the
    `DESC_MEMORY_AUDIT_TURN` correction in 8fc7afc landed net -3 characters,
    with +2 the most it could have spent. And the failure surfaced far from
    its cause, so
    it now names which descriptions moved and by how much, with
    `_DESC_BUDGET_PRESSURE` warning while there is still room to react."""
    descs = await _lean_descriptions(tmp_path)
    total = sum(len(d) for d in descs.values())
    # The recorded trail, totals and ceilings kept apart. Figures up to 3.6.4
    # are as recorded by the commits that took them; 27,248 is these same 18
    # constants read back out of 8e12b99; the current total is `_DESC_BASELINE`'s.
    #   totals:   27,930 pre-collapse -> 27,681 (3.6.2 collapsed the two
    #             policy-heaviest, memory_search / memory_write) -> 26,976
    #             (3.6.4 audited the remaining 16 default-on descriptions;
    #             residual policy-dup collapsed, all field reference kept) ->
    #             27,248 at 8e12b99 -> 27,398 (Phase 7's episode work) ->
    #             25,535 (the footprint phase's cuts) -> 25,773, once
    #             that phase's closing follow-ups added 238 back. The
    #             25,535 was taken before them, which is why the rows
    #             that commit landed sum to 25,773 and why three of its
    #             four per-tool cut figures above sit below the row
    #             printed beside them; tests/test_resident_footprint.py
    #             records the +238, and four published surfaces had to be
    #             corrected for the same snapshot. Then -> 25,749
    #             (memory_audit_turn's false clause) -> 25,890, measured
    #             live 2026-08-02 and equal to `_DESC_BASELINE`'s sum.
    #   ceilings: 27,800 -> 27,100 (3.6.4 ratcheted the sweep in) -> 27,250
    #             (3.8.0, for one field-pin: the credential gate's
    #             `credential_warning` status and its `acknowledge_credential`
    #             override, added to DESC_MEMORY_WRITE symmetrically with the
    #             existing transient pair) -> 27,500 -> `_DESC_BUDGET_CEILING`
    #             (the footprint phase ratcheted the cuts in, rule 3).
    # Slack held deliberately constant across the last two recalibrations —
    # 452 chars at 27,500, 465 here (the same pre-follow-up snapshot as the
    # 25,535 above; 227 against the 25,773 that actually landed, and 110
    # today) — so the ratchet-down tightened the
    # budget by 1,500 without also tightening the posture toward the next
    # legitimate field-pin. A ratchet that silently does both is how a
    # budget starts arbitrating edits that have nothing to do with it,
    # which is the failure rule 1 above was written against.
    assert total <= _DESC_BUDGET_CEILING, (
        f"lean default-on tool descriptions total {total} chars "
        f"(~{total // 4} tokens), over the {_DESC_BUDGET_CEILING} ceiling. "
        f"These are paid in context EVERY turn. Collapse duplicated policy "
        f"into the `instructions` block (the canonical home); a ceiling raise "
        f"is a recalibration with its own rules, in this test's docstring. "
        f"Against the recorded baseline:\n" + _desc_budget_breakdown(descs)
    )
    if total > _DESC_BUDGET_PRESSURE:
        warnings.warn(
            f"lean default-on tool descriptions are at {total} of the "
            f"{_DESC_BUDGET_CEILING}-char ceiling — "
            f"{_DESC_BUDGET_CEILING - total} chars left, which is under one "
            f"field-pin. Collapse duplicated policy into the `instructions` "
            f"block now, while this is still a warning. Against the recorded "
            f"baseline:\n" + _desc_budget_breakdown(descs),
            stacklevel=2,
        )


async def test_policy_lives_once_not_triplicated_in_descriptions(
    tmp_path: Path,
) -> None:
    """The load-bearing retrieval/write policy is canonical in the server
    `instructions` block (pinned by
    test_instructions_block_carries_load_bearing_phrases). A description may
    carry AT MOST ONE inline point-of-call cue for a given rule — the H8
    author hoisted these to the point of call deliberately, and that one copy
    may be behaviourally load-bearing by proximity. What this guard forbids is
    the rule reappearing across MULTIPLE descriptions, which is how the
    surface triplicated in the first place."""
    descs = await _lean_descriptions(tmp_path)
    # Exact policy phrasings (not generic words like "opt-in" that recur in
    # legitimate feature reference). Each must live in EXACTLY ONE default-on
    # description. These are verbatim, case-sensitive substrings, so the list
    # is wording-locked: it must be updated in lockstep with the DESC_* strings.
    policy_phrases = [
        "Using your stored preference",  # transparency / announce-on-search
        "do NOT call",  # opt-in retrieval restraint
        "non-negotiable",
        "PROACTIVELY",  # proactive-write reflex
        "aggressive writing is safe",
    ]
    # `!= 1` (not `> 1`) is load-bearing: >1 catches re-triplication across
    # descriptions (the regression this was written for), but 0 catches a
    # reword that silently drops a phrase from its home — without which the
    # guard would pass vacuously and stop tracking that rule. Three of these
    # phrases ("do NOT call", "non-negotiable", "aggressive writing is safe")
    # have no survival floor elsewhere in the suite, so this == 1 IS their floor.
    wrong = {
        p: names
        for p in policy_phrases
        if len(names := [n for n, d in descs.items() if p in d]) != 1
    }
    assert not wrong, (
        "each policy phrase must appear in exactly one default-on tool "
        "description (canonical policy lives in the `instructions` block, with "
        "one inline point-of-call cue). >1 = re-triplicated across "
        "descriptions; 0 = a reword dropped the phrase and silently un-pinned "
        f"it — update the wording-locked list in lockstep: {wrong}"
    )


async def test_point_of_call_cues_survive_in_descriptions(
    tmp_path: Path,
) -> None:
    """Dissent guard for the description-collapse. Collapsing policy toward the
    instructions block must NOT strip the single inline cue at the point of
    call: the announce-on-search rule in memory_search and the proactive-write
    reflex in memory_write may earn their compliance by sitting next to the
    tool the model is about to invoke. A future "just make it shorter" pass
    that removed them would be invisible to the offline eval (nothing in the
    harness exercises a live model's tool-description compliance), so it is
    pinned here instead."""
    descs = await _lean_descriptions(tmp_path)
    assert "Using your stored preference" in descs.get("memory_search", ""), (
        "memory_search lost its inline announce-on-search cue; the "
        "transparency behaviour may be earned by proximity-to-call. Keep one "
        "inline copy even though the full policy lives in the instructions "
        "block."
    )
    assert "PROACTIVELY" in descs.get("memory_write", ""), (
        "memory_write lost its inline proactive-write cue; keep one inline "
        "copy at the point of call."
    )


async def test_search_desc_tells_the_caller_how_to_word_a_query(
    tmp_path: Path,
) -> None:
    """The `query` cue is the only guidance on this whole surface that acts
    on retrieval's INPUT, and it is the highest-leverage line measured:
    against `bench/retrieval/results/v2-unpadded-2026-07-26.json` — a
    180-document synthetic corpus, 20 blind-authored questions per probe —
    the lexical arm retrieves 35% recall@1 on questions as asked and 80%
    re-queried in nouns the documents actually contain. That corpus is
    easier than a real store (the retrieval-bench notes say so of their own
    numbers), so the gap between the two probes is the finding and neither
    rate is a store's rate. The cue sits in `memory_search`'s description
    rather than the instructions block because it is written at the moment
    the caller types a query, and it survives here because every other force
    on this file pushes toward deleting it: it is the newest line, it is
    pure prose to a byte-counting reader, and the budget ratchet 30 lines up
    makes "shorten a description" a recurring move.

    The DESC carries the instruction alone. The numbers belong in
    `handlers/search.py`'s module docstring — they are only honest with the
    caveat above attached, and a per-turn surface cannot afford the caveat —
    so a future edit that helpfully restores a recall figure to the cue is
    re-opening a hole, not adding evidence. The pair this docstring used to
    quote, 10%->65%, had no committed artifact at all.

    Two halves are pinned, because the measurement says only one of them
    works. The lift comes from VOCABULARY: the control arm — question words
    stripped, content words kept — scores 35%, identical to asking outright,
    since the ranker already strips stopwords. A future reword that
    compressed this to "use keywords, not questions" would read as the same
    advice and buy exactly nothing, so `nouns` is pinned as the operative
    word. `re-query` is pinned separately: a weak first result is the only
    signal the caller ever gets that its wording missed, and dropping the
    retry turns a recoverable miss into a silent one."""
    desc = (await _lean_descriptions(tmp_path)).get("memory_search", "")
    assert "nouns" in desc, (
        "memory_search's `query` cue lost the word `nouns`. The measured "
        "lever is the vocabulary a memory would literally contain — NOT "
        "keyword-vs-question phrasing, which measured a 0-point control."
    )
    assert "re-query" in desc, (
        "memory_search's `query` cue lost the re-query instruction; a weak "
        "first hit is the caller's only signal that its wording missed."
    )
    assert "paraphrase recall" not in desc, (
        "memory_search's `mode` line is claiming paraphrase recall again. "
        "With no semantic leg configured (the package default) `hybrid` is "
        "RRF over keyword + BM25 — both lexical."
    )


# ---------------------------------------------------------------------------
# Backward-scan early-exit in _already_recorded_pending_ids
# ---------------------------------------------------------------------------


def test_already_recorded_pending_ids_early_exits_on_old_events(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    # The early-exit is asserted STRUCTURALLY — by how many events the
    # backward scan examines — not by wall clock.
    #
    # A clock assertion here measured the wrong thing and flaked on it.
    # Through 3.40 the scan materialised `list(iter_events(root))` plus
    # a whole-log stop-hook-session pre-pass before the loop — both
    # O(N) over the whole active log and neither short-circuited — so
    # parsing 10k events dominated the call (measured locally at 14ms
    # of a 15.6ms call, with the backward loop examining exactly one
    # event) whether or not the early-exit worked. The old
    # `elapsed < 0.5` was reading the runner's parse throughput, and
    # when a shared ubuntu-latest slot returned 0.538s during the
    # 3.37.0 release run it reported "early-exit appears not to be
    # triggering" about an early-exit that was working perfectly. The
    # scan now streams `iter_events_backward` with a lazy per-line
    # parse, so the parse cost is tail-bounded too (pinned separately:
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


def test_already_recorded_pending_ids_parse_count_is_tail_bounded(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedup scan's PARSE cost — not just its matching loop — is
    bounded by the examined tail. The early-exit above always bounded
    the loop, but through 3.40 the scan materialised
    `list(iter_events(root))` (plus a whole-log stop-hook-session
    pre-pass) first, so every turn with pending tokens json-parsed the
    WHOLE active log to examine a handful of events — 14.0ms of a
    15.6ms call measured against 10k events, with the loop examining
    one. The scan now streams `iter_events_backward`, which parses a
    line only when the merge pulls it.

    Counting `bettermemory.events._parse_event_line` calls measures
    the parse directly: it is the single per-line parse seam both
    readers share, and nothing else in this call path parses log
    lines. Mutation property: revert the consumer to
    `list(iter_events(root))`, or make the reader parse eagerly, or
    delete the early-exit `break`, and this count becomes ~N (10_003
    lines here) instead of the examined tail plus one merge-seed
    lookahead per active segment.
    """
    import time as _time

    from bettermemory import events as events_mod
    from bettermemory._handlers import _already_recorded_pending_ids
    from bettermemory.events import EVENT_LOG_FILENAME, Recorder
    from bettermemory.session import PendingUseToken, SessionState

    memory_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id)

    # Phase 1: a long ancient prefix, hand-written into the LEGACY
    # segment (one json.dumps line per event — no per-line fsync, so
    # the fixture stays cheap; the reader merges the legacy file with
    # the recorder's shard, so the scan sees one 10_003-line active
    # log). All timestamped before any pending token, so the scan's
    # early-exit crosses the boundary after the tail.
    ancient = json.dumps(
        {"ts": "2026-01-01T00:00:00Z", "session": state.session_id, "kind": "noise"}
    )
    (memory_dir / EVENT_LOG_FILENAME).write_text(
        "\n".join([ancient] * 10_000) + "\n", encoding="utf-8"
    )

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

    parses = 0
    real_parse = events_mod._parse_event_line

    def _counting_parse(raw: bytes) -> dict[str, Any] | None:
        nonlocal parses
        parses += 1
        return real_parse(raw)

    monkeypatch.setattr(events_mod, "_parse_event_line", _counting_parse)
    result = _already_recorded_pending_ids(state, recorder)

    assert result == set(pending_mids), f"expected all pending ids back, got {result}"
    assert parses < 50, (
        f"dedup scan parsed {parses} of 10_003 active-log lines; the backward "
        "reader's lazy parse (or the early-exit that bounds it) regressed"
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

    memory_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id)

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
    hook_log = EventLog(memory_dir, session_id="claude-code-transcript-bridge")
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
    foreign_log = EventLog(memory_dir, session_id="sess_other_window")
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
# memory_proposals — write-reflex review surface
# ---------------------------------------------------------------------------


def _seed_proposal(memory_dir: Path, *, pid: str, body: str, cat: str = "fact") -> None:
    from bettermemory.proposals import Proposal, ProposalQueue

    ProposalQueue(memory_dir).append(
        [
            Proposal(
                id=pid,
                body=body,
                source_excerpt=body,
                suggested_category=cat,
                created="2026-01-01T00:00:00Z",
            )
        ]
    )


async def test_memory_proposals_list_empty(server: Any) -> None:
    res = await _call(server, "memory_proposals")
    assert res["status"] == "ok"
    assert res["count"] == 0
    assert res["proposals"] == []


async def test_memory_proposals_list_returns_queued(
    server: Any, memory_dir: Path
) -> None:
    _seed_proposal(
        memory_dir, pid="p1", body="user prefers terse explanations over prose"
    )
    res = await _call(server, "memory_proposals", action="list")
    assert res["count"] == 1
    assert res["proposals"][0]["id"] == "p1"
    assert res["proposals"][0]["suggested_category"] == "fact"


async def test_memory_proposals_accept_writes_memory_and_removes(
    server: Any, memory_dir: Path
) -> None:
    from bettermemory.proposals import ProposalQueue
    from bettermemory.store import Store

    _seed_proposal(
        memory_dir,
        pid="p1",
        body="user prefers terse explanations over prose",
        cat="user-inference",
    )
    res = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="p1",
        scopes=["learning-style"],
    )
    assert res["status"] == "accepted"
    assert res["category"] == "user-inference"
    # Proposal consumed; a real memory now exists with that body.
    assert ProposalQueue(memory_dir).load() == []
    bodies = [m.body for m in Store(memory_dir).load_all()]
    assert any("terse explanations" in b for b in bodies)


async def test_memory_proposals_accept_rejects_oversized_body(
    server: Any, memory_dir: Path
) -> None:
    """Regression: the accept path wrote directly via store.write, bypassing
    the max_content_bytes guard every other write path enforces. An oversized
    body (>1 MiB) was written, then failed the 1 MiB bounded read on the next
    load — the accept reported success while the record silently vanished from
    every read surface. Accept must now reject it, leaving the proposal in the
    queue (per the retry contract) and the store intact.
    """
    from bettermemory.proposals import ProposalQueue
    from bettermemory.store import Store

    big_body = "I prefer " + ("verbose-context " * 130_000)  # > 1 MiB
    _seed_proposal(memory_dir, pid="big", body=big_body, cat="user-inference")

    with pytest.raises(Exception, match="max_content_bytes"):
        await _call(
            server,
            "memory_proposals",
            action="accept",
            proposal_id="big",
            scopes=["learning-style"],
        )

    # Nothing written-then-vanished: the store loads cleanly, and the proposal
    # is still queued for the caller to fix or dismiss.
    assert Store(memory_dir).load_all() == []
    assert [p.id for p in ProposalQueue(memory_dir).load()] == ["big"]


async def test_memory_proposals_accept_enforces_allowed_scopes(
    memory_dir: Path,
) -> None:
    """Whole-tree sweep (MEDIUM, fail-open): when `[scopes] allowed` is set,
    accepting a proposal into a non-allowed scope must be refused — just like
    memory_write / memory_update / memory_rename_scope. Previously accept wrote
    straight via store.write, bypassing both the whitelist and the
    max_scopes_per_write cap, so the proposal queue was an end-run around the
    policy. On refusal the proposal stays queued (the retry contract)."""
    from bettermemory.config import ScopesConfig
    from bettermemory.proposals import ProposalQueue

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        scopes=ScopesConfig(allowed=["tools", "infrastructure"]),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    _seed_proposal(memory_dir, pid="p1", body="user prefers terse explanations")

    with pytest.raises(Exception, match="not in allowed list"):
        await _call(
            server,
            "memory_proposals",
            action="accept",
            proposal_id="p1",
            scopes=["career"],
        )
    # Refused: proposal still queued, nothing written.
    assert [p.id for p in ProposalQueue(memory_dir).load()] == ["p1"]
    assert Store(memory_dir).load_all() == []

    # An allowed scope goes through.
    res = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="p1",
        scopes=["tools"],
    )
    assert res["status"] == "accepted"
    assert res["scopes"] == ["tools"]
    assert ProposalQueue(memory_dir).load() == []


async def test_memory_proposals_accept_requires_scopes(
    server: Any, memory_dir: Path
) -> None:
    _seed_proposal(
        memory_dir, pid="p1", body="user prefers terse explanations over prose"
    )
    with pytest.raises(Exception, match="scopes"):
        await _call(server, "memory_proposals", action="accept", proposal_id="p1")


async def test_memory_proposals_accept_unknown_id_not_found(server: Any) -> None:
    res = await _call(
        server, "memory_proposals", action="accept", proposal_id="nope", scopes=["x"]
    )
    assert res["status"] == "not_found"


async def test_memory_proposals_dismiss_removes(server: Any, memory_dir: Path) -> None:
    from bettermemory.proposals import ProposalQueue

    _seed_proposal(
        memory_dir, pid="p1", body="user prefers terse explanations over prose"
    )
    res = await _call(server, "memory_proposals", action="dismiss", proposal_id="p1")
    assert res["status"] == "dismissed"
    assert ProposalQueue(memory_dir).load() == []


async def test_memory_proposals_unknown_action_errors(server: Any) -> None:
    with pytest.raises(Exception, match="unknown action"):
        await _call(server, "memory_proposals", action="frobnicate")


async def test_scope_overview_reports_proposals_pending(
    server: Any, memory_dir: Path
) -> None:
    res0 = await _call(server, "memory_scope_overview")
    assert res0["proposals_pending"] == 0
    _seed_proposal(
        memory_dir, pid="p1", body="user prefers terse explanations over prose"
    )
    res1 = await _call(server, "memory_scope_overview")
    assert res1["proposals_pending"] == 1


async def test_memory_proposals_accept_claims_before_write(memory_dir: Path) -> None:
    """accept must CLAIM the proposal (remove it from the queue under the
    per-file flock) BEFORE writing the durable memory — that ordering is
    what makes a concurrent double-accept idempotent: the racer that loses
    the claim finds the queue empty and skips the write instead of landing
    a second store entry. Pinned deterministically by spying on
    ``store.write``: at the instant the durable write runs, the accepted
    proposal is already gone from the queue. The pre-fix order (write,
    then remove, across separate locks) left the proposal in the queue
    during the write, so a second accept could load and write it again.
    """
    from bettermemory.proposals import ProposalQueue

    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )

    _seed_proposal(memory_dir, pid="claim1", body="a durable fact worth keeping around")

    queued_at_write_time: list[list[str]] = []
    real_write = store.write

    def _spy_write(**kwargs: Any) -> Any:
        # Snapshot the queue as the durable write happens. With the fix the
        # proposal has already been claimed, so its id is absent here.
        queued_at_write_time.append([p.id for p in ProposalQueue(memory_dir).load()])
        return real_write(**kwargs)

    store.write = _spy_write  # type: ignore[method-assign]

    res = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="claim1",
        scopes=["projects:c"],
    )
    assert res["status"] == "accepted"
    # store.write ran exactly once and, at that instant, the proposal had
    # already been removed from the queue (claim-before-write ordering).
    assert queued_at_write_time == [[]]
    # Post-condition: queue drained, exactly one memory written.
    assert ProposalQueue(memory_dir).load() == []
    assert len(store.load_all()) == 1


async def test_memory_proposals_schema_includes_acknowledge_credential(
    server: Any,
) -> None:
    """The REGISTERED memory_proposals tool's input schema must expose
    `acknowledge_credential` — the same escape hatch memory_write /
    memory_update carry. The SDK derives the schema from the
    `ToolHandlers.memory_proposals` wrapper signature, and its pydantic
    arg-model silently DROPS any key the signature doesn't declare, so a
    handler-core parameter the wrapper omits is dead at the tool boundary:
    a client passing acknowledge_credential=True still gets the refusal.
    That is exactly how the hatch shipped dead once — this schema-level pin
    catches the wrapper/handler drift the handler-level tests can't see."""
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    for tool_name in ("memory_proposals", "memory_write", "memory_update"):
        props = _input_schema(by_name[tool_name])["properties"]
        assert "acknowledge_credential" in props, (
            f"{tool_name} input schema lost the acknowledge_credential escape hatch"
        )


async def test_memory_proposals_accept_acknowledge_credential_end_to_end(
    memory_dir: Path,
) -> None:
    """The acknowledge_credential escape hatch exercised THROUGH the MCP
    boundary (`mcp.call_tool`), not the handler function — the boundary is
    where it died before: the wrapper's signature didn't declare the
    parameter, so the SDK dropped the key and the flag never reached the
    core. A credential-bearing proposal is refused without the flag and
    accepted WITH acknowledge_credential=True; the forced override lands in
    the audit log exactly once (the accept core records it — the MCP
    handler must not double-log), detector kind only, value never."""
    from bettermemory.events import Recorder, iter_events
    from bettermemory.proposals import ProposalQueue

    # The documented public AWS example key — fragment-assembled so the
    # secret-shaped literal never appears in source (push-protection
    # scanners; see tests/test_server_credentials.py).
    aws_example = "".join(("AKIA", "IOSFODNN7EXAMPLE"))
    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=Store(memory_dir),
        state=state,
        recorder=Recorder(root=memory_dir, session_id=state.session_id),
    )
    _seed_proposal(
        memory_dir,
        pid="ack1",
        body=f"AWS access-key ids look like {aws_example} — a documented example.",
    )

    # Without the flag: refused with the SAME structured credential_warning
    # status memory_write rejects with (harmonized post-3.20.0 — the refusal
    # used to raise at the tool boundary); the proposal stays queued, nothing
    # is written, the markers name the kind but never the value.
    refused = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="ack1",
        scopes=["infrastructure"],
    )
    assert refused["status"] == "credential_warning"
    assert "aws-access-key-id" in {m["kind"] for m in refused["markers"]}
    assert aws_example not in str(refused)
    assert [p.id for p in ProposalQueue(memory_dir).load()] == ["ack1"]
    assert Store(memory_dir).load_all() == []

    # WITH acknowledge_credential=True over the same call path: accepted,
    # written, queue claimed, override kinds surfaced in the response.
    res = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="ack1",
        scopes=["infrastructure"],
        acknowledge_credential=True,
    )
    assert res["status"] == "accepted"
    assert res["credentials_acknowledged"] == ["aws-access-key-id"]
    assert ProposalQueue(memory_dir).load() == []
    assert len(Store(memory_dir).load_all()) == 1

    # The forced override is in the event log EXACTLY once — recorded by
    # the accept core, with no second event layered on by the MCP handler.
    accept_events = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert len(accept_events) == 1
    assert accept_events[0]["proposal_id"] == "ack1"
    assert accept_events[0]["credentials_acknowledged"] == ["aws-access-key-id"]
    # Kind only, never the value — the raw secret shape must not be
    # recoverable from the audit log.
    raw_log = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert aws_example not in raw_log


async def test_episode_promote_advances_turn_exactly_once(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """episode_promote routes through memory_write, which advances the
    session turn counter at its own entry. The promote handler must NOT
    advance it a second time — a double advance prematurely ages the
    ~2-turn record_use / pending-write TTL windows that key off the turn
    counter. Count SessionState.advance_turn across one promote and assert
    exactly one bump (pre-fix the promote handler called _advance_turn
    itself AND again via the nested memory_write -> two bumps).
    """
    from bettermemory.episodes import EpisodeStore
    from bettermemory.session import SessionState as _SessionState

    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )

    # Seed the source episode straight through the store so the
    # episode_write *handler* (which advances the turn on its own) can't
    # pollute the count — we measure only the promote path.
    ep = EpisodeStore(memory_dir).write(
        session_id="sess-promote",
        body="did the thing in detail",
        takeaway="the distilled durable fact worth keeping across sessions",
        scopes=["projects:foo"],
    )

    calls = {"n": 0}
    real_advance = _SessionState.advance_turn

    def _counting_advance(self: _SessionState) -> int:
        calls["n"] += 1
        return real_advance(self)

    monkeypatch.setattr(_SessionState, "advance_turn", _counting_advance)

    res = await _call(
        server,
        "episode_promote",
        episode_id=ep.id,
        scopes=["projects:foo"],
    )

    # The promote committed (the takeaway is a clean durable fact)...
    assert res["status"] == "committed"
    assert res["promoted_from_episode_id"] == ep.id
    # ...and the turn counter advanced exactly once, not twice.
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# OSError-leak hardening at the MCP boundary
# ---------------------------------------------------------------------------
#
# Every store.write call site reachable from a tool MUST translate a
# disk-level OSError (ENOSPC/EIO/EACCES) into a structured ValueError —
# never leak the bare OSError, which carries the absolute store path —
# past `call_tool`. Mirrors test_remove_handler_converts_oserror_to_value_error
# (test_server_tombstones.py) and the rename_scope OSError regression.
# Caught by the post-3.6.0 whole-tree sweep: write.py (memory_write /
# memory_write_confirm) and the memory_proposals accept path were the
# unguarded store.write siblings.


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


async def test_memory_write_confirm_handler_converts_oserror_to_value_error(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_write_confirm -> store.write, AFTER take_pending already
    consumed the staged write. The disk-level OSError must surface as a
    structured ValueError (flagging the pending id is consumed), not a
    bare path-leaking OSError. Staging does not call store.write, so the
    monkeypatch is installed after staging and only bites on confirm."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())
    pending = await _call(
        server,
        "memory_write",
        content="a durable fact about the user",
        scopes=["tools"],
        category="user-inference",
    )
    pid = pending["pending_id"]
    monkeypatch.setattr(store, "write", _raising_write)

    with pytest.raises(Exception) as excinfo:
        await _call(server, "memory_write_confirm", pending_id=pid)
    assert _oserror_wrapped_as_value_error(excinfo, "failed to write memory"), (
        f"regression: bare OSError leaked past memory_write_confirm. "
        f"Got: {excinfo.value!r}"
    )


async def test_memory_proposals_accept_handler_converts_oserror_to_value_error(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_proposals(action="accept") -> accept_proposal claims the
    proposal from the queue, THEN store.write. A disk-level OSError after
    the claim must surface as a structured ValueError noting the entry is
    already gone — not leak the bare OSError path. (full_tool_surface is
    True by dataclass default, so memory_proposals is registered.)"""
    from bettermemory.proposals import Proposal, ProposalQueue

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())
    ProposalQueue(store.root).append(
        [
            Proposal(
                id="01PROPOSALOSERR",
                body="a durable fact worth keeping in memory",
                source_excerpt="a durable fact worth keeping in memory",
                suggested_category="fact",
                created="2026-01-01T12:00:00+00:00",
            )
        ]
    )
    monkeypatch.setattr(store, "write", _raising_write)

    with pytest.raises(Exception) as excinfo:
        await _call(
            server,
            "memory_proposals",
            action="accept",
            proposal_id="01PROPOSALOSERR",
            scopes=["tools"],
        )
    assert _oserror_wrapped_as_value_error(excinfo, "failed to accept proposal"), (
        f"regression: bare OSError leaked past memory_proposals accept. Got: {excinfo.value!r}"
    )
