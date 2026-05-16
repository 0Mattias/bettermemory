"""Integration tests for the `claim_excerpts` provenance field on
memory_record_use (T1.1 of the 1.6 plan).

Companion to test_server_record_use.py — these specifically exercise
the new claim-level audit trail. The recorded excerpts must:

- be parallel to memory_ids (same length, one per id)
- accept None for "no specific claim for this id"
- reject empty strings (use None instead)
- enforce the 500-char cap (encourages quoting, discourages dumping bodies)
- land in the on-disk event log so a later audit can replay them
- be byte-stable on the event-log shape when the field isn't passed
  (existing log readers must not see a new null key on every old event)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _use_events(memory_dir: Path) -> list[dict[str, Any]]:
    return [e for e in iter_events(memory_dir) if e["kind"] == "use"]


async def _seed_two(server: Any) -> tuple[str, str]:
    a = await _call(server, "memory_write", content="alpha fact body", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta fact body", scopes=["tools"])
    return a["id"], b["id"]


async def test_claim_excerpts_land_in_event_log(
    server_with_events: tuple[Any, Path],
) -> None:
    """The whole point: the excerpt lands in the on-disk event log so an
    audit can replay which claim was applied. Without this round-trip,
    the field is informational only."""
    server, memory_dir = server_with_events
    a_id, b_id = await _seed_two(server)

    await _call(
        server,
        "memory_record_use",
        memory_ids=[a_id, b_id],
        outcome="applied",
        claim_excerpts=["alpha is durable", "beta is durable"],
    )

    events = _use_events(memory_dir)
    assert len(events) == 1
    assert events[0]["claim_excerpts"] == ["alpha is durable", "beta is durable"]
    assert events[0]["ids"] == [a_id, b_id]
    assert events[0]["outcome"] == "applied"


async def test_claim_excerpts_response_echoes_recorded_values(
    server_with_events: tuple[Any, Path],
) -> None:
    """The tool response includes the recorded excerpts so the caller
    can confirm what was stored — useful when the model trims whitespace
    or rejects something and a fallback is needed."""
    server, _ = server_with_events
    a_id, _ = await _seed_two(server)

    res = await _call(
        server,
        "memory_record_use",
        memory_ids=[a_id],
        outcome="applied",
        claim_excerpts=["the load-bearing phrase"],
    )
    assert res["claim_excerpts"] == ["the load-bearing phrase"]


async def test_claim_excerpts_omitted_when_not_passed(
    server_with_events: tuple[Any, Path],
) -> None:
    """Byte-stability: callers that never pass claim_excerpts should see
    event-log entries with no `claim_excerpts` key at all (not a key
    with a null value). Old log parsers / health rollups must keep
    working without seeing a new field on every event."""
    server, memory_dir = server_with_events
    a_id, _ = await _seed_two(server)

    await _call(
        server,
        "memory_record_use",
        memory_ids=[a_id],
        outcome="applied",
    )

    events = _use_events(memory_dir)
    assert len(events) == 1
    assert "claim_excerpts" not in events[0]


async def test_claim_excerpts_with_none_entries_for_partial_provenance(
    server_with_events: tuple[Any, Path],
) -> None:
    """The model may know the claim for one memory but not another in
    the same record_use call. None entries are allowed in the parallel
    list — they round-trip through the event log as null."""
    server, memory_dir = server_with_events
    a_id, b_id = await _seed_two(server)

    await _call(
        server,
        "memory_record_use",
        memory_ids=[a_id, b_id],
        outcome="applied",
        claim_excerpts=["alpha quote", None],
    )

    events = _use_events(memory_dir)
    assert events[0]["claim_excerpts"] == ["alpha quote", None]


async def test_claim_excerpts_length_mismatch_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    """Length must match memory_ids exactly. The alternative (sparse
    dict keyed by id) would be harder for the model to assemble and
    obscure the pairing."""
    server, _ = server_with_events
    a_id, b_id = await _seed_two(server)

    with pytest.raises(Exception, match="length"):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[a_id, b_id],
            outcome="applied",
            claim_excerpts=["only one excerpt for two ids"],
        )


async def test_claim_excerpts_empty_string_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    """An empty-string excerpt is ambiguous — "no claim" should be None,
    explicit so the audit log can distinguish. Reject loudly so the
    caller fixes the call rather than logging a useless empty record."""
    server, _ = server_with_events
    a_id, _ = await _seed_two(server)

    with pytest.raises(Exception, match="empty"):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[a_id],
            outcome="applied",
            claim_excerpts=[""],
        )


async def test_claim_excerpts_oversized_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    """Excerpts are quotes, not body dumps. Cap at 500 chars so the
    event log stays small and the model is encouraged to extract the
    load-bearing phrase rather than copy entire memories."""
    server, _ = server_with_events
    a_id, _ = await _seed_two(server)

    with pytest.raises(Exception, match="500"):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[a_id],
            outcome="applied",
            claim_excerpts=["x" * 501],
        )


async def test_claim_excerpts_strips_surrounding_whitespace(
    server_with_events: tuple[Any, Path],
) -> None:
    """The model often emits whitespace-padded excerpts. Strip on the
    way in so two semantically-identical excerpts hash to the same
    audit-log entry."""
    server, memory_dir = server_with_events
    a_id, _ = await _seed_two(server)

    await _call(
        server,
        "memory_record_use",
        memory_ids=[a_id],
        outcome="applied",
        claim_excerpts=["   the trimmed phrase   "],
    )

    events = _use_events(memory_dir)
    assert events[0]["claim_excerpts"] == ["the trimmed phrase"]


async def test_claim_excerpts_work_for_all_outcomes(
    server_with_events: tuple[Any, Path],
) -> None:
    """All four outcomes accept claim_excerpts — provenance is just as
    valuable when recording a contradiction or correction as when
    recording an applied claim. The audit log gets to record which
    specific claim was contradicted, not just that something was."""
    server, memory_dir = server_with_events
    for outcome in ("applied", "ignored", "contradicted", "corrected"):
        a = await _call(
            server,
            "memory_write",
            content=f"body for {outcome}",
            scopes=["tools"],
        )
        await _call(
            server,
            "memory_record_use",
            memory_ids=[a["id"]],
            outcome=outcome,
            claim_excerpts=[f"the {outcome} claim"],
        )

    events = _use_events(memory_dir)
    by_outcome = {e["outcome"]: e for e in events}
    for outcome in ("applied", "ignored", "contradicted", "corrected"):
        assert outcome in by_outcome, f"missing outcome {outcome}"
        assert by_outcome[outcome]["claim_excerpts"] == [f"the {outcome} claim"]


async def test_claim_excerpts_non_string_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    """Each entry must be str or None. A list of ints, dicts, etc. is a
    caller bug — FastMCP's pydantic validation layer rejects non-string
    inputs before our handler runs, surfacing a clearer "valid string"
    error to the caller. The handler-level isinstance() check is the
    backstop for paths that bypass pydantic (e.g. direct in-process
    invocation in tests)."""
    server, _ = server_with_events
    a_id, _ = await _seed_two(server)

    # Match "string" loosely — pydantic says "valid string", our backstop
    # says "must be a string or None". Either is acceptable for the
    # contract "non-string rejected".
    with pytest.raises(Exception, match="string"):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[a_id],
            outcome="applied",
            claim_excerpts=[42],
        )
