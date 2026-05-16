"""End-to-end tests for the memory_write groundedness gate (T1.3).

Covers the wire flow: opt-in via `groundedness_check=True`, the
`source_transcript` parameter, the `status: "ungrounded"` response
shape, and the `acknowledge_ungrounded=True` override.
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


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


async def test_groundedness_check_off_by_default(server: Any) -> None:
    """Without `groundedness_check=True`, writes go through unchanged.
    The gate is opt-in — back-compat for every existing caller."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
    )
    assert res["status"] == "committed"


async def test_grounded_body_commits(server: Any) -> None:
    """When `groundedness_check=True` and every sentence anchors to
    the transcript, the write commits normally — no ungrounded
    status, no override needed."""
    res = await _call(
        server,
        "memory_write",
        content="The user prefers terse code-driven explanations.",
        scopes=["learning-style"],
        groundedness_check=True,
        source_transcript=(
            "user: please give me terse code-driven explanations, no prose."
        ),
    )
    assert res["status"] == "committed"


async def test_ungrounded_body_blocks_write(server: Any) -> None:
    """A body with a sentence that doesn't anchor to the transcript
    returns `status: "ungrounded"` with the offending sentence
    listed. The write does NOT commit to disk."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        groundedness_check=True,
        source_transcript="user: please use terse code-driven explanations.",
    )
    assert res["status"] == "ungrounded"
    assert "claims" in res
    assert len(res["claims"]) >= 1
    # The offending sentence is surfaced verbatim.
    assert any("Tokyo" in c["sentence"] for c in res["claims"])

    # Confirm nothing landed on disk by searching for the body.
    hits = await _call(server, "memory_search", query="Tokyo cats")
    # Unwrap structured response.
    hits_list = (
        hits.get("result", hits)
        if isinstance(hits, dict) and "result" in hits
        else hits
    )
    assert hits_list == []


async def test_acknowledge_ungrounded_overrides_gate(server: Any) -> None:
    """`acknowledge_ungrounded=True` lets the writer commit despite the
    gate. Same shape as the existing acknowledge_transient /
    acknowledge_scope_mismatch overrides — the caller is asserting
    that they have other grounding sources the gate can't see."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        groundedness_check=True,
        source_transcript="user: please use terse code-driven explanations.",
        acknowledge_ungrounded=True,
    )
    assert res["status"] == "committed"


async def test_no_transcript_skips_gate(server: Any) -> None:
    """`groundedness_check=True` but no transcript is a no-op — the
    gate has no signal to check against, so it can't fire. The write
    proceeds. This is the legitimate "I want the gate but I don't
    have a transcript for this turn" case."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        groundedness_check=True,
        # source_transcript not passed.
    )
    assert res["status"] == "committed"


async def test_overlap_ratio_in_response(server: Any) -> None:
    """The response carries each ungrounded claim's overlap_ratio so
    the caller can see *how* ungrounded it was. Useful when tuning
    the gate or deciding whether to override."""
    res = await _call(
        server,
        "memory_write",
        content="The capital of Bhutan is Thimphu.",
        scopes=["tools"],
        groundedness_check=True,
        source_transcript="user: please tell me about python and rust.",
    )
    assert res["status"] == "ungrounded"
    for claim in res["claims"]:
        assert "overlap_ratio" in claim
        assert isinstance(claim["overlap_ratio"], (int, float))


async def test_mixed_body_only_flags_ungrounded_sentences(server: Any) -> None:
    """A body with one grounded sentence and one ungrounded sentence
    flags only the ungrounded one. The caller can see exactly which
    line to rephrase."""
    res = await _call(
        server,
        "memory_write",
        content=(
            "The user prefers terse code-driven explanations. "
            "Their favourite colour is purple-orange."
        ),
        scopes=["learning-style"],
        groundedness_check=True,
        source_transcript="user: please give terse code-driven explanations.",
    )
    assert res["status"] == "ungrounded"
    # Exactly one sentence flagged (the colour one); the terse-
    # explanations sentence anchors to the transcript.
    flagged = [c["sentence"] for c in res["claims"]]
    assert len(flagged) == 1
    assert "purple-orange" in flagged[0].lower()
