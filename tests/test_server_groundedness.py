"""End-to-end tests for the memory_write groundedness gate (T1.3).

Covers the wire flow: opt-in via `groundedness_check=True`, the
`source_transcript` parameter, the `status: "ungrounded"` response
shape, and the `acknowledge_ungrounded=True` override.

Why most bodies here carry `acknowledge_user_claim=True`
-------------------------------------------------------
These fixtures predate `UserClaimGate`, and their bodies ("The user
lives in Tokyo…", "The user prefers terse code-driven explanations.")
are genuinely claims about the user filed as `fact` — exactly what
that gate refuses. The gate sits at index 2 of `_WRITE_GATES`, ahead
of `GroundednessGate` at index 4, so on those bodies it now answers
first and `status: "ungrounded"` becomes unreachable without the
acknowledgement.

The flag is the right repair rather than a reworded body because the
bodies are load-bearing for the assertions below (`"Tokyo" in
c["sentence"]`, the terse/purple-orange split), and because passing it
states at the call site the thing that is actually true: these are
user claims, and this module is not testing that axis.

What that costs, stated plainly: on THESE bodies the tests no longer
exercise the ungrounded path from a bare `memory_write`. What keeps
that from being a hole is `test_overlap_ratio_in_response` — its body
("The capital of Bhutan is Thimphu.") trips no user-claim shape, so it
drives `status: "ungrounded"` through the full chain with every
acknowledge flag False. It is the control for this file; do not add an
`acknowledge_*` flag to it.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


async def test_groundedness_check_off_by_default(server: Any) -> None:
    """Without `groundedness_check=True`, writes go through unchanged.
    The gate is opt-in — back-compat for every existing caller."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        acknowledge_user_claim=True,  # body is a user claim; see module docstring
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
        acknowledge_user_claim=True,  # body is a user claim; see module docstring
    )
    assert res["status"] == "committed"


async def test_ungrounded_body_blocks_write(server: Any) -> None:
    """A body with a sentence that doesn't anchor to the transcript
    returns `status: "ungrounded"` with the offending sentence
    listed. The write does NOT commit to disk.

    `acknowledge_user_claim=True` is what makes the groundedness verdict
    reachable at all on this body — `UserClaimGate` precedes
    `GroundednessGate` — so the test now proves something slightly
    STRONGER than before: acknowledging the user-claim axis does not
    also buy a pass on the grounding axis. The acknowledge flags are
    per-gate and don't cross-apply. The name still describes the
    assertion: an ungrounded body blocks the write."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        groundedness_check=True,
        source_transcript="user: please use terse code-driven explanations.",
        acknowledge_user_claim=True,  # body is a user claim; see module docstring
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
    that they have other grounding sources the gate can't see.

    Paired with `test_ungrounded_body_blocks_write` above, which passes
    ONLY `acknowledge_user_claim` and still gets `ungrounded`: together
    they show the second flag is doing the work here, not the first."""
    res = await _call(
        server,
        "memory_write",
        content="The user lives in Tokyo and owns three cats.",
        scopes=["personal-context"],
        groundedness_check=True,
        source_transcript="user: please use terse code-driven explanations.",
        acknowledge_ungrounded=True,
        acknowledge_user_claim=True,  # body is a user claim; see module docstring
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
        acknowledge_user_claim=True,  # body is a user claim; see module docstring
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
    line to rephrase.

    The two gates disagree about WHICH sentence is interesting here, and
    that is the point of keeping both live on one body:
    `UserClaimGate` matches sentence 1 ("The user prefers…"),
    `GroundednessGate` flags sentence 2 (purple-orange). Acknowledging
    the first must not blunt the second's per-sentence granularity —
    `len(flagged) == 1` below is what pins that."""
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
        acknowledge_user_claim=True,  # sentence 1 is a user claim; see module docstring
    )
    assert res["status"] == "ungrounded"
    # Exactly one sentence flagged (the colour one); the terse-
    # explanations sentence anchors to the transcript.
    flagged = [c["sentence"] for c in res["claims"]]
    assert len(flagged) == 1
    assert "purple-orange" in flagged[0].lower()
