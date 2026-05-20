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
    with pytest.raises(Exception):
        await _call(server, "memory_write", content="x", scopes=[])


async def test_write_rejects_invalid_scope(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(server, "memory_write", content="x", scopes=["With Space"])


async def test_show_unknown_id_errors(server: Any) -> None:
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        await _call(server, "memory_write_confirm", pending_id=pid)


async def test_confirm_unknown_id_errors(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    with pytest.raises(Exception):
        await _call(server, "memory_write_confirm", pending_id="pending_deadbeef0000")


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
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        await _call(server, "memory_update", id=written["id"])


async def test_update_rejects_empty_content(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
        await _call(server, "memory_update", id=written["id"], content="   ")


async def test_update_rejects_empty_scopes(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
        await _call(server, "memory_update", id=written["id"], scopes=[])


async def test_update_rejects_invalid_scope(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            scopes=["With Space"],
        )


async def test_update_rejects_invalid_confidence(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            confidence="extreme",
        )


async def test_update_unknown_id_errors(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_update",
            id="01HXYZNOTAREALIDOK000000ZZ",
            content="anything",
        )


async def test_update_tombstoned_id_errors(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="superseded")
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_update",
            id=written["id"],
            category="user-inference",
        )


async def test_update_rejects_unknown_category(server: Any) -> None:
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        await _call(server, "memory_verify", id="01HXYZNOTAREALIDOK000000ZZ")


async def test_memory_verify_tombstoned_errors(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="durable claim", scopes=["tools"]
    )
    await _call(server, "memory_remove", id=written["id"], reason="superseded")
    with pytest.raises(Exception):
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


async def test_memory_search_no_path_drift_when_top_not_expanded(
    server: Any, tmp_path: Path
) -> None:
    """Without expand_top, path_drift shouldn't appear — we haven't loaded
    the body, so we have nothing to scan against."""
    missing = tmp_path / "not-expanded.txt"
    await _call(
        server,
        "memory_write",
        content=f"something at `{missing}` reference",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="something reference"))
    assert len(hits) >= 1
    assert "path_drift" not in hits[0]


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
