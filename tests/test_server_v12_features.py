"""End-to-end tests for the v1.2 surface additions.

Covers the seven changes that landed together:

1. ``ambient`` memory category — write, persist, long-body warning,
   exclusion from dead-weight curation.
2. Dead-weight rule fix + ``cold_memories`` bucket.
3. ``staleness_verdict`` rollup field on every retrieval surface.
4. Auto-``record_use`` via response tokens.
5. ``curation_pending`` rollup in ``memory_scope_overview``.
6. ``scope_mismatch`` warning at ``memory_write`` time.
7. Structured ``verified_claims`` on ``memory_verify``.

Each section is grouped by the change it exercises so a future
maintainer can find the locking tests for one feature without
spelunking. The fixtures match the rest of `test_server.py` —
hermetic per-test memory dir, isolated SessionState.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


@pytest.fixture
def server_with_state(memory_dir: Path) -> tuple[Any, SessionState, Path]:
    """Variant fixture that exposes the SessionState so use-token tests
    can introspect what the auto-commit pass did. Mirrors `server`
    otherwise."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    srv = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
    )
    return srv, state, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    """FastMCP wraps `list[...]` tool responses as `{"result": [...]}` on
    the structured side. Mirror the helper from `test_server.py`."""
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


# ---------------------------------------------------------------------------
# Change 1 — ambient category
# ---------------------------------------------------------------------------


async def test_ambient_category_commits_immediately(server: Any) -> None:
    """Ambient skips the pending-write gate (same fast path as fact)."""
    res = await _call(
        server,
        "memory_write",
        content="The user prefers terse code-driven explanations.",
        scopes=["learning-style"],
        category="ambient",
    )
    assert res["status"] == "committed"
    assert res["category"] == "ambient"


async def test_ambient_category_persists_in_frontmatter(
    server: Any, memory_dir: Path
) -> None:
    """The new field round-trips through disk."""
    res = await _call(
        server,
        "memory_write",
        content="The user is based in Boston.",
        scopes=["personal-context"],
        category="ambient",
    )
    files = list(memory_dir.glob("*.md"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert "category: ambient" in raw

    # Round-trip via memory_show.
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["category"] == "ambient"


async def test_ambient_category_long_body_emits_warning(server: Any) -> None:
    """A body of >500 words gets an `ambient_body_long` warning attached
    to the otherwise-successful commit."""
    long_body = " ".join(["word"] * 600)
    res = await _call(
        server,
        "memory_write",
        content=long_body,
        scopes=["personal-context"],
        category="ambient",
    )
    assert res["status"] == "committed"
    assert res.get("warnings") == ["ambient_body_long"]


async def test_ambient_category_short_body_no_warning(server: Any) -> None:
    res = await _call(
        server,
        "memory_write",
        content="Brief ambient context.",
        scopes=["personal-context"],
        category="ambient",
    )
    assert "warnings" not in res or res["warnings"] == []


async def test_fact_category_long_body_does_not_warn(server: Any) -> None:
    """The long-body warning is ambient-specific — fact memories are
    free to be long."""
    long_body = " ".join(["word"] * 600)
    res = await _call(
        server,
        "memory_write",
        content=long_body,
        scopes=["tools"],
        category="fact",
    )
    assert res["status"] == "committed"
    assert "warnings" not in res or res["warnings"] == []


async def test_unknown_category_rejected(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_write",
            content="x",
            scopes=["tools"],
            category="not-a-category",
        )


async def test_ambient_excluded_from_dead_weight_via_health(server: Any) -> None:
    """An ambient memory with no use signal must not appear in dead_weight."""
    written = await _call(
        server,
        "memory_write",
        content="User prefers code-driven tutorials.",
        scopes=["learning-style"],
        category="ambient",
    )
    # Generate a search hit so the memory has retrieval_count > 0 — the
    # condition under which a non-ambient memory would land in dead_weight.
    await _call(server, "memory_search", query="code-driven tutorials")
    health = await _call(server, "memory_health", window_days=0)
    dead_ids = {m["id"] for m in health["dead_weight"]}
    cold_ids = {m["id"] for m in health["cold_memories"]}
    assert written["id"] not in dead_ids
    assert written["id"] not in cold_ids


# ---------------------------------------------------------------------------
# Change 2 — cold_memories bucket exposed via memory_health
# ---------------------------------------------------------------------------


async def test_cold_memories_field_returned_by_health(server: Any) -> None:
    res = await _call(server, "memory_health")
    assert "cold_memories" in res
    assert isinstance(res["cold_memories"], list)


# ---------------------------------------------------------------------------
# Change 3 — staleness_verdict rollup
# ---------------------------------------------------------------------------


async def test_memory_show_includes_staleness_verdict(server: Any) -> None:
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    shown = await _call(server, "memory_show", id=res["id"])
    # Never-verified memory → spot_check_required.
    assert shown["staleness_verdict"] == "spot_check_required"


async def test_memory_show_verdict_fresh_after_verify(server: Any) -> None:
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(server, "memory_verify", id=res["id"], note="checked")
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["staleness_verdict"] == "fresh"


async def test_memory_search_hit_includes_staleness_verdict(
    server: Any,
) -> None:
    await _call(
        server,
        "memory_write",
        content="The widget configuration lives in /etc/widget.toml.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="widget configuration"))
    assert hits, "expected at least one hit"
    for hit in hits:
        assert "staleness_verdict" in hit
        # Never-verified, so the verdict is required regardless of drift.
        assert hit["staleness_verdict"] == "spot_check_required"


async def test_memory_search_expand_top_recomputes_verdict_on_drift(
    server: Any,
) -> None:
    """The expanded top hit re-runs path_drift against the actual body
    and updates the verdict — a fresh-verified memory citing a missing
    path is `spot_check_recommended`, not `fresh`."""
    written = await _call(
        server,
        "memory_write",
        content="The script lives at `/this/path/does/not/exist-xyz`.",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"])
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="script lives at this path",
            expand_top=True,
        )
    )
    assert hits
    top = hits[0]
    if top.get("relevance") == "high":
        # Expanded path triggered.
        assert top["staleness_verdict"] == "spot_check_recommended"


# ---------------------------------------------------------------------------
# Change 4 — auto-record_use via use_token
# ---------------------------------------------------------------------------


async def test_search_response_includes_use_token_per_hit(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="A retrievable fact about widgets.",
        scopes=["tools"],
    )
    hits = _unwrap(await _call(server, "memory_search", query="widgets"))
    assert hits
    for hit in hits:
        assert "use_token" in hit
        assert isinstance(hit["use_token"], str)
        assert hit["use_token"].startswith("use_")
        # Token must NOT be the memory id (opaque correlation handle).
        assert hit["use_token"] != hit["id"]


async def test_show_response_includes_use_token(server: Any) -> None:
    res = await _call(server, "memory_write", content="x", scopes=["tools"])
    shown = await _call(server, "memory_show", id=res["id"])
    assert "use_token" in shown
    assert shown["use_token"].startswith("use_")


async def test_use_token_auto_commits_after_two_turns(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """Issue token at turn N; two more memory_* calls (turns N+1, N+2)
    later, the next call (turn N+3) sees the search ids logged as
    auto-applied in the event log."""
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )

    await _call(srv, "memory_search", query="retrievable fact")
    # Turn deltas: search at turn ~2 issued a token. Two more calls
    # advance the counter — by the third call, the auto-commit fires.
    await _call(srv, "memory_list")  # +1
    await _call(srv, "memory_list")  # +2
    await _call(srv, "memory_list")  # +3 — auto-commit fires here

    events = list(iter_events(memory_dir))
    auto_uses = [
        e
        for e in events
        if e.get("kind") == "use"
        and e.get("outcome") == "applied"
        and e.get("auto") is True
    ]
    assert auto_uses, f"expected an auto-applied event; got {events}"
    assert any(res["id"] in (e.get("ids") or []) for e in auto_uses)


async def test_explicit_record_use_overrides_pending_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """An explicit record_use(ignored) for a still-pending token must
    NOT produce an `applied` shadow event in the log."""
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="ignored")

    # Advance enough turns for any rogue auto-commit to fire.
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")
    await _call(srv, "memory_list")

    events = list(iter_events(memory_dir))
    use_events = [e for e in events if e.get("kind") == "use"]
    # Find any event citing the memory id.
    for e in use_events:
        if res["id"] in (e.get("ids") or []):
            # Should be the explicit `ignored`, never `applied`+auto.
            if e.get("outcome") == "applied" and e.get("auto") is True:
                pytest.fail(f"explicit override leaked an auto-applied event: {e}")


async def test_explicit_record_use_purges_token(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """After an explicit record_use, the pending token map should
    drop the id so a future auto-commit sweep doesn't double-fire."""
    srv, state, _memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    assert res["id"] in state.pending_use_tokens
    await _call(srv, "memory_record_use", memory_ids=[res["id"]], outcome="applied")
    assert res["id"] not in state.pending_use_tokens


async def test_use_token_within_ttl_does_not_auto_commit(
    server_with_state: tuple[Any, SessionState, Path],
) -> None:
    """One memory_* call after a search isn't enough for the token to age
    out; the auto-commit pass only fires after `ttl_turns` deltas."""
    srv, _state, memory_dir = server_with_state
    res = await _call(
        srv, "memory_write", content="A retrievable fact.", scopes=["tools"]
    )
    await _call(srv, "memory_search", query="retrievable fact")
    await _call(srv, "memory_list")  # one turn advance — still within TTL

    events = list(iter_events(memory_dir))
    auto_uses = [
        e
        for e in events
        if e.get("kind") == "use"
        and e.get("outcome") == "applied"
        and e.get("auto") is True
        and res["id"] in (e.get("ids") or [])
    ]
    assert not auto_uses, "auto-commit fired before TTL"


# ---------------------------------------------------------------------------
# Change 5 — curation_pending in memory_scope_overview
# ---------------------------------------------------------------------------


async def test_scope_overview_returns_curation_pending(server: Any) -> None:
    res = await _call(server, "memory_scope_overview")
    assert "curation_pending" in res
    assert set(res["curation_pending"].keys()) == {
        "stale",
        "never_verified",
        "drifted",
        "cold",
        "dead",
    }
    # All counts must be integers.
    for v in res["curation_pending"].values():
        assert isinstance(v, int)


async def test_scope_overview_curation_pending_zero_on_empty(server: Any) -> None:
    res = await _call(server, "memory_scope_overview")
    assert res["curation_pending"] == {
        "stale": 0,
        "never_verified": 0,
        "drifted": 0,
        "cold": 0,
        "dead": 0,
    }


async def test_scope_overview_curation_never_verified_increments(
    server: Any,
) -> None:
    """A freshly-written memory has no last_verified_at, so the
    `never_verified` count climbs by one."""
    await _call(server, "memory_write", content="A new fact.", scopes=["tools"])
    res = await _call(server, "memory_scope_overview")
    assert res["curation_pending"]["never_verified"] == 1


# ---------------------------------------------------------------------------
# Change 6 — scope_mismatch warning at write time
# ---------------------------------------------------------------------------


async def test_scope_mismatch_fires_when_body_cites_other_project_name(
    server: Any,
) -> None:
    """Seed a `projects:foo` memory, then write a body that mentions
    `foo` while declaring a different scope. The gate should fire."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content=("When working on foo, the build script lives at scripts/build.sh."),
        scopes=["tools"],
    )
    assert res["status"] == "scope_mismatch"
    assert "projects:foo" in res["suggested_scopes"]


async def test_scope_mismatch_does_not_persist(server: Any, memory_dir: Path) -> None:
    """A scope_mismatch return must not commit the body."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    # File count BEFORE the second write.
    before = len(list(memory_dir.glob("*.md")))
    res = await _call(
        server,
        "memory_write",
        content="Working on foo means setting FOO_DEBUG=1.",
        scopes=["tools"],
    )
    assert res["status"] == "scope_mismatch"
    after = len(list(memory_dir.glob("*.md")))
    assert before == after


async def test_acknowledge_scope_mismatch_overrides_and_commits(
    server: Any, memory_dir: Path
) -> None:
    """Setting `acknowledge_scope_mismatch=True` skips the gate; the
    write commits despite the cross-scope reference."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content="Working on foo, FOO_DEBUG=1 is the canonical toggle.",
        scopes=["tools"],
        acknowledge_scope_mismatch=True,
    )
    assert res["status"] == "committed"
    files = list(memory_dir.glob("*.md"))
    assert len(files) == 2


async def test_scope_mismatch_skipped_when_scope_already_declared(
    server: Any,
) -> None:
    """A multi-scope write that DOES carry the relevant project tag is
    fine — the body legitimately mentions another project."""
    await _call(
        server,
        "memory_write",
        content="A foo project fact.",
        scopes=["projects:foo"],
    )
    res = await _call(
        server,
        "memory_write",
        content="Working on foo with the canonical setup.",
        scopes=["projects:foo", "tools"],
    )
    assert res["status"] == "committed"


async def test_scope_mismatch_silent_when_no_project_scopes(server: Any) -> None:
    """Empty store has no `projects:*` scopes to lean on; the gate
    should pass through silently."""
    res = await _call(
        server,
        "memory_write",
        content="A first-write fact about foo and bar.",
        scopes=["tools"],
    )
    assert res["status"] == "committed"


# ---------------------------------------------------------------------------
# Change 7 — verified_claims on memory_verify
# ---------------------------------------------------------------------------


async def test_verify_accepts_structured_claims(server: Any, memory_dir: Path) -> None:
    res = await _call(
        server,
        "memory_write",
        content="The hosts file lives at /etc/hosts on macOS.",
        scopes=["tools"],
    )
    verified = await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=["/etc/hosts"],
        verified_versions=["macOS-15.0"],
    )
    assert verified["verified_paths"] == ["/etc/hosts"]
    assert verified["verified_versions"] == ["macOS-15.0"]


async def test_verify_persists_structured_claims(server: Any, memory_dir: Path) -> None:
    res = await _call(
        server,
        "memory_write",
        content="The hosts file lives at /etc/hosts.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=["/etc/hosts"],
    )
    files = list(memory_dir.glob("*.md"))
    raw = files[0].read_text(encoding="utf-8")
    assert "verified_paths" in raw
    assert "/etc/hosts" in raw


async def test_show_after_verify_marks_path_verified(
    server: Any, tmp_path: Path
) -> None:
    """A path the caller has attested AND that still exists shows up in
    `path_drift.verified` on memory_show."""
    extant = tmp_path / "exists.txt"
    extant.write_text("hello")
    res = await _call(
        server,
        "memory_write",
        content=f"The thing lives at `{extant}`.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=[str(extant)],
    )
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["path_drift"] is not None
    assert str(extant) in shown["path_drift"]["verified"]


async def test_verify_passing_none_preserves_prior_lists(
    server: Any,
) -> None:
    """Calling memory_verify a second time without verified_paths
    preserves the previously-attested list — None means 'no change',
    not 'clear'."""
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=["/etc/hosts"],
    )
    after_no_arg = await _call(server, "memory_verify", id=res["id"])
    assert after_no_arg["verified_paths"] == ["/etc/hosts"]


async def test_verify_passing_empty_list_clears_prior(server: Any) -> None:
    """An explicit empty list is the 'clear' signal — distinct from
    None."""
    res = await _call(server, "memory_write", content="A claim.", scopes=["tools"])
    await _call(
        server,
        "memory_verify",
        id=res["id"],
        verified_paths=["/etc/hosts"],
    )
    cleared = await _call(server, "memory_verify", id=res["id"], verified_paths=[])
    assert cleared["verified_paths"] == []
